"""P2 知乎通道测试：CH-D 手动导入 / 令牌桶限速 / 指数退避 / Fernet cookie /
CH-A 降级探活 / REST 端点。

运行：pytest tests/test_p2_zhihu.py -q（项目根）。
对齐 §9 P2 DoD：
  ① CH-D 手动导入 → detections 落库跑通（真实文本）；
  ② 令牌桶超限请求被拒/排队；
  ③ Fernet 加解密往返一致；
  ④ GET /zhihu/channels 返回 4 通道健康结构；
  ⑤ 无 Playwright 环境下 CH-A 健康返回 ok=false 且标注，系统不崩。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.services.zhihu_bridge import (
    BackoffManager,
    ChannelUnavailableError,
    CookieVault,
    RateLimitExceeded,
    TokenBucket,
    ZhihuBridge,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def clean_db(tmp_path, monkeypatch):
    """隔离数据库：AF_DATA_DIR 指向临时目录 + 首次迁移 + 刷新配置单例。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    db.close_all()
    db.ensure_schema()
    conn = db.get_conn()
    yield conn
    db.close_all()
    get_settings.cache_clear()


@pytest.fixture()
def bridge(clean_db) -> ZhihuBridge:
    return ZhihuBridge(settings=get_settings(), conn=clean_db)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离的 API 客户端（AF_DATA_DIR 指向临时目录）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    from app.main import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# ① CH-D 手动导入 → detections 落库
# ---------------------------------------------------------------------------
def test_ingest_manual_plain_text_writes_detection(bridge):
    result = bridge.ingest_manual(
        "你好，我这边有个稳赚不赔的投资项目，需要先垫付一笔手续费。"
    )
    assert result["ok"] is True
    assert result["channel"] == "ch-d"
    assert result["ingested"] == 1 and result["duplicates"] == 0
    det_id = result["items"][0]["detection_id"]
    row = bridge._db().execute(
        "SELECT source, status, content FROM detections WHERE id = ?", (det_id,)
    ).fetchone()
    assert row is not None
    assert row["source"] == "manual"
    assert row["status"] == "pending"          # P3 检测流水线消费
    assert "稳赚不赔" in row["content"]


def test_ingest_manual_json_messages_splits(bridge):
    payload = json.dumps({
        "messages": [
            {"from": "a", "text": "第一条：刷单返利日结佣金"},
            {"from": "b", "text": "第二条：需要垫付保证金"},
            "第三条：点击链接领取奖品",
        ]
    }, ensure_ascii=False)
    result = bridge.ingest_manual(payload, source="import")
    assert result["ingested"] == 3
    rows = bridge._db().execute(
        "SELECT content FROM detections WHERE source='import' ORDER BY id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["content"].startswith(("第一条", "第二条", "第三条")) for r in rows)


def test_ingest_manual_deduplicates_by_hash(bridge):
    text = "重复的杀猪盘话术：带你赚钱稳赚不赔"
    first = bridge.ingest_manual(text)
    second = bridge.ingest_manual(text)
    assert second["ingested"] == 0 and second["duplicates"] == 1
    assert second["items"][0]["detection_id"] == first["items"][0]["detection_id"]


def test_ingest_manual_empty_rejected(bridge):
    with pytest.raises(ValueError):
        bridge.ingest_manual("   ")


# ---------------------------------------------------------------------------
# ② 令牌桶限速 + 指数退避（真实实现）
# ---------------------------------------------------------------------------
def test_token_bucket_rejects_overflow():
    bucket = TokenBucket(capacity=2, refill_per_second=0.0)  # 不补充
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is True
    assert bucket.try_acquire() is False                     # 超限被拒
    assert bucket.acquire(timeout=0.05) is False             # 排队超时失败


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=5, refill_per_second=100.0)
    assert bucket.try_acquire(5) is True
    assert bucket.try_acquire() is False
    import time
    time.sleep(0.15)                                        # 需 0.05s 回满；留足计时余量
    assert bucket.available >= 5                            # 已回满（封顶 capacity）
    assert bucket.try_acquire() is True


def test_gate_raises_rate_limit_when_empty(bridge):
    bridge.limiter = TokenBucket(capacity=1, refill_per_second=0.0)
    bridge._gate("ch-a")                                     # 第 1 次放行
    with pytest.raises(RateLimitExceeded):
        bridge._gate("ch-a")                                 # 第 2 次被拒


def test_backoff_cooldown_blocks_then_recovers(bridge):
    delay = bridge.backoff.record_failure("ch-a")
    assert delay >= 0.5                                       # base=1.0 * jitter(0.5-1)
    assert bridge.backoff.due("ch-a") is False                # 冷却中
    with pytest.raises(RateLimitExceeded):
        bridge._gate("ch-a")
    bridge.backoff.record_success("ch-a")
    assert bridge.backoff.due("ch-a") is True                 # 恢复
    bridge._gate("ch-a")                                      # 不再抛错


def test_backoff_exponential_growth():
    bm = BackoffManager(base=1.0, factor=2.0, cap=60.0, jitter=False)
    d1 = bm.record_failure("x")
    d2 = bm.record_failure("x")
    d3 = bm.record_failure("x")
    assert d1 == 1.0 and d2 == 2.0 and d3 == 4.0              # 1→2→4 指数


# ---------------------------------------------------------------------------
# ③ Fernet cookie 加解密
# ---------------------------------------------------------------------------
def test_cookie_vault_roundtrip_with_env_key(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_ZHIHU_ENCRYPT_KEY", key)
    get_settings.cache_clear()
    vault = CookieVault(get_settings())
    plain = "_zap=abc; d_c0=xyz; q_c1=1; z_c0=Mi4x"
    token = vault.encrypt(plain)
    assert token != plain                                    # 密文 ≠ 明文
    assert plain not in token                                # 明文不泄漏
    assert vault.decrypt(token) == plain                     # 往返一致
    get_settings.cache_clear()


def test_cookie_vault_auto_generates_secret_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AF_ZHIHU_ENCRYPT_KEY", raising=False)
    get_settings.cache_clear()
    settings = get_settings()
    vault1 = CookieVault(settings)
    key_file = tmp_path / "secret.key"
    assert key_file.exists()                                 # 自动生成
    vault2 = CookieVault(get_settings())                     # 二次实例复用同一密钥
    token = vault1.encrypt("d_c0=ABC")
    assert vault2.decrypt(token) == "d_c0=ABC"               # 跨实例可解
    get_settings.cache_clear()


def test_set_cookie_stores_encrypted_only(bridge):
    bridge.set_cookie("ch-a", "_zap=a; d_c0=b; q_c1=1", scheme="header")
    sess = bridge.get_session("ch-a")
    assert sess is not None
    assert sess["status"] == "active"
    enc = sess["cookie_enc"]
    assert "d_c0" not in enc                                  # 明文不落库
    decrypted = json.loads(bridge.vault.decrypt(enc))
    names = {c["name"] for c in decrypted}
    assert names == {"_zap", "d_c0", "q_c1"}


# ---------------------------------------------------------------------------
# ④ CH-A 降级探活：无 Playwright → ok=false 带标注，系统不崩
# ---------------------------------------------------------------------------
def test_ch_a_probe_degraded_without_playwright(bridge, monkeypatch):
    # 无论本机是否装了 playwright，都强制模拟“不可用”
    monkeypatch.setattr(
        ZhihuBridge, "_playwright_importable", staticmethod(lambda: False)
    )
    h = bridge.ch_a_probe()                                   # 不抛异常 = 系统不崩
    assert h.channel == "ch-a"
    assert h.ok is False
    assert h.degraded is True
    assert "playwright" in h.note.lower() or "Playwright" in h.note


def test_ch_a_probe_ok_with_browser(bridge, monkeypatch, tmp_path):
    """模拟 playwright 已安装且 chromium 存在 → ok=True（cookie 未注入时给提示）。"""
    exe = tmp_path / "fake-chrome.exe"
    exe.write_text("", encoding="utf-8")

    fake_pkg = types.ModuleType("playwright")
    fake_sync = types.ModuleType("playwright.sync_api")

    class _FakeChromium:
        executable_path = str(exe)

    class _FakeP:
        chromium = _FakeChromium()

    class _Ctx:
        def __enter__(self):
            return _FakeP()

        def __exit__(self, *a):
            return False

    fake_sync.sync_playwright = lambda: _Ctx()
    fake_pkg.sync_api = fake_sync
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync)
    monkeypatch.setattr(
        ZhihuBridge, "_playwright_importable", staticmethod(lambda: True)
    )

    h = bridge.ch_a_probe()
    assert h.ok is True
    assert h.degraded is False
    assert "未注入 cookie" in h.note


def test_health_returns_four_channels(bridge):
    healths = _run(bridge.health())
    assert [h.channel for h in healths] == ["ch-a", "ch-b", "ch-c", "ch-d"]
    for h in healths:
        assert isinstance(h.ok, bool)
        assert isinstance(h.note, str)
        assert isinstance(h.degraded, bool)
    # CH-D 永远可用；CH-A 在本机（无 playwright 或模拟缺失）标记不可用但不崩
    assert healths[3].ok is True


def test_fetch_inbox_degrades_chain(bridge, monkeypatch):
    """无 cookie/无 Playwright 时 fetch_inbox 抛 ChannelUnavailableError 且提示 CH-D。"""
    monkeypatch.setattr(
        ZhihuBridge, "_playwright_importable", staticmethod(lambda: False)
    )
    with pytest.raises(ChannelUnavailableError) as ei:
        _run(bridge.fetch_inbox())
    assert "CH-D" in ei.value.note or "ch-d" in ei.value.note


# ---------------------------------------------------------------------------
# ⑤ REST 端点
# ---------------------------------------------------------------------------
def test_zhihu_channels_endpoint_returns_four(client):
    r = client.get("/api/v1/zhihu/channels")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    channels = body["data"]["channels"]
    assert [c["channel"] for c in channels] == ["ch-a", "ch-b", "ch-c", "ch-d"]
    for c in channels:
        for field in ("ok", "note", "degraded"):
            assert field in c


def test_zhihu_login_requires_admin(client):
    r = client.post(
        "/api/v1/zhihu/channels/ch-a/login",
        json={"cookie": "_zap=a; d_c0=b"},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_api_key"


def test_zhihu_login_verify_flow(client, tmp_path):
    key = _admin_key(tmp_path)
    headers = {"X-API-Key": key}
    r = client.post(
        "/api/v1/zhihu/channels/ch-a/login",
        json={"cookie": "_zap=z; d_c0=dc0val; q_c1=1"},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["stored"] is True and data["encrypted"] is True
    assert data["cookie_count"] == 3

    v = client.post("/api/v1/zhihu/channels/ch-a/verify", headers=headers)
    assert v.status_code == 200
    vd = v.json()["data"]
    assert vd["valid"] is True
    assert vd["has_dc0"] is True


def test_zhihu_login_rejects_garbage_cookie(client, tmp_path):
    key = _admin_key(tmp_path)
    r = client.post(
        "/api/v1/zhihu/channels/ch-a/login",
        json={"cookie": "!!!not-a-cookie!!!"},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_cookie"


def test_zhihu_login_rejects_non_cookie_channel(client, tmp_path):
    key = _admin_key(tmp_path)
    r = client.post(
        "/api/v1/zhihu/channels/ch-d/login",
        json={"cookie": "_zap=a"},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "no_cookie_channel"


def test_zhihu_import_via_api_writes_detection(client, tmp_path):
    key = _admin_key(tmp_path)
    headers = {"X-API-Key": key}
    r = client.post(
        "/api/v1/zhihu/import",
        json={"text": "客服主动退款，点击理赔链接填写银行卡信息", "source": "manual"},
        headers=headers,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ingested"] == 1
    assert data["channel"] == "ch-d"
    n = client.get("/api/v1/zhihu/status").json()["data"]["sessions"]
    assert n["ch-d"]["status"] == "active"


def test_zhihu_status_reports_rate_limiter(client):
    r = client.get("/api/v1/zhihu/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["rate_limit"]["capacity"] == 20              # AF_ZHIHU_RATE_LIMIT 默认
    assert data["rate_limit"]["available"] >= 0
    assert set(data["sessions"].keys()) == {"ch-a", "ch-b", "ch-c", "ch-d"}