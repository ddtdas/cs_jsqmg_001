"""第 1 轮后端修复回归测试（backend-core，R1-BE）。

覆盖（docs/第1轮_实战测试报告.md 问题清单）：
  1) P0-D1  bootstrap-info 不泄露 key 文件路径/文件名
  2) P1-B3  check-hit 空/空白/纯符号候选不再误命中与退役
  3) P1-A6/C7  脱敏变体（全角数字/零宽/字母混淆/中文数字/分隔符/+86）被抓并抹除
  4) P1-D2/D8  XFF 伪造不再放行回环端点、不再打穿限流；uvicorn proxy_headers=False
  5) P1-A1  扫描/分级/告警/已读产生 events（detection_scanned/detection_graded/
            alert_created/alert_read）

运行：pytest tests/test_r1_backend_fixes.py -q（项目根）。
"""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.utils import ApiError

BAIT = "你好呀，我在网上看到一个很不错的兼职项目，感觉超适合你，我们加个V聊聊怎么样"
REASON_20 = "这是一条超过二十个字的敏感操作理由说明文本"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离 DB + 清空单例（provider/限流桶）+ 种子词库。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()

    from app.main import app

    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


def _mk_det(conn, *, content: str = "稳赚不赔的理财项目", rule_score: float = 11.0) -> int:
    h = hashlib.sha256(content.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, llm_verdict, "
        "judge_confidence, grade, status) VALUES ('manual', ?, ?, ?, 'fraud', 0.9, 'L4', 'processed')",
        (h, content, rule_score),
    )
    det_id = int(cur.lastrowid)
    row = conn.execute("SELECT id FROM speech_patterns WHERE pattern='稳赚不赔' LIMIT 1").fetchone()
    if row:
        conn.execute(
            "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?,?,?,?)",
            (det_id, row["id"], "稳赚不赔", rule_score),
        )
    conn.commit()
    return det_id


# ===========================================================================
# 1) P0-D1：bootstrap-info 不泄露 key 文件路径/文件名
# ===========================================================================


def test_bootstrap_info_no_key_file_path_or_filename(api):
    r = api.post("/api/v1/auth/bootstrap-info")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["initialized"] is True
    assert data["port"] > 0
    assert "key_file" not in data, "bootstrap-info 不得返回 key_file 字段（含绝对路径）"
    blob = json.dumps(data, ensure_ascii=False)
    assert "bootstrap_admin_key" not in blob, "hint/字段不得出现 key 文件名"
    assert ".txt" not in blob, "hint/字段不得出现文件扩展名"
    assert "\\" not in blob and "C:" not in blob, "hint/字段不得出现盘符/绝对路径"


# ===========================================================================
# 2) P1-B3：check-hit 空/空白/纯符号候选不误命中、不退役
# ===========================================================================

EMPTY_CANDIDATES = [
    "",                 # 空文本
    "   ",              # 纯空白
    "\t\r\n  ",         # 控制空白
    "。。。",           # 纯中文符号
    "！！！？？？",      # 纯标点
    "😀😀😀😀😀",         # 纯 emoji
    "••••••",           # 纯项目符号
    "１２３",           # 全角数字（归一化后为空）
    "。。。  ！！！",    # 符号组合
]


@pytest.mark.asyncio
async def test_check_hit_blank_and_pure_symbols_not_hit(env):
    from app.services.trap_engine import TrapEngine

    engine = TrapEngine()
    d = engine.create_draft(env, bait_text=BAIT)
    tid = d["id"]
    engine.deploy(env, tid)
    engine.monitor(env, tid)

    for cand in EMPTY_CANDIDATES:
        r = await engine.check_hit(env, tid, cand)
        assert r["hit"] is False, f"候选 {cand!r} 不得误命中"
        assert r["status"] == "monitored", f"候选 {cand!r} 不得触发状态迁移"
        assert r["similarity"]["matched_by"] == []
        assert r["similarity"].get("reason") == "candidate_too_short"

    row = engine.get(env, tid)
    assert row["status"] == "monitored", "空白候选不得导致退役"
    assert row["hit_count"] == 0
    n = env.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind IN ('trap_hit','trap_retired')"
    ).fetchone()["n"]
    assert n == 0, "未命中不得产生 trap_hit / trap_retired 事件"


@pytest.mark.asyncio
async def test_check_hit_real_text_still_hits(env):
    """回归：有效候选仍正常命中（守卫不得误伤真实踩饵检测）。"""
    from app.services.trap_engine import TrapEngine

    engine = TrapEngine()
    d = engine.create_draft(env, bait_text=BAIT)
    tid = d["id"]
    engine.deploy(env, tid)
    engine.monitor(env, tid)
    r = await engine.check_hit(env, tid, BAIT + "？加你详聊可以吗")
    assert r["hit"] is True
    assert r["status"] == "retired"


def test_check_hit_api_blank_symbols_no_retire(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    tid = api.post("/api/v1/traps", json={"bait_text": BAIT}, headers=auth).json()["data"]["id"]
    api.post(f"/api/v1/traps/{tid}/deploy", headers=auth)
    api.post(f"/api/v1/traps/{tid}/monitor", headers=auth)

    for cand in ["   ", "。。。", "😀😀😀"]:
        r = api.post(f"/api/v1/traps/{tid}/check-hit", json={"text": cand})
        assert r.status_code == 200
        assert r.json()["data"]["hit"] is False, f"API 候选 {cand!r} 不得命中"

    row = api.get(f"/api/v1/traps/{tid}").json()["data"]
    assert row["status"] == "monitored" and row["hit_count"] == 0


# ===========================================================================
# 3) P1-A6/C7：脱敏变体（全角/零宽/混淆/中文数字/分隔符/+86）被抓并抹除
# ===========================================================================

PHONE_VARIANTS = [
    "13812345678",                # 明文基线
    "１３８１２３４５６７８",       # ④a 全角数字
    "138\u200b1234\u200b5678",    # ④b 零宽字符
    "138l2345678",                # ④c 字母混淆（l→1）
    "13８l2345678",               # 混合全角+字母
    "一三八一二三四五六七八",       # 中文小写数字
    "壹叁捌壹贰叁肆伍陆柒捌",       # 中文大写数字
    "138 1234 5678",              # ⑤b 拆词空格
    "138-1234-5678",              # ⑤a 拆词连字符
    "138.1234.5678",              # 拆词点号
    "+86 138 1234 5678",          # ⑤c +86 前缀
    "8613812345678",              # 86 前缀（无 +）
]

ID_VARIANTS = [
    "110101199001011234",                       # 明文基线（格式合法）
    "110101 1990 0101 1234",                    # 拆词空格
    "１１０１０１１９９００１０１１２３４",          # 全角数字
    "110101\u200b1990\u200b0101\u200b1234",     # 零宽字符
    "一一零一零一一九九零零一零一一二三四",          # 中文数字
]


def _assert_variant_rejected(masked: str, variant: str) -> None:
    """断言：扫得出 + assert_clean 拒绝 + regex_redact 抹除。"""
    from app.services.desensitize import DesensitizeService

    hits = DesensitizeService.scan_sensitive(variant)
    assert hits, f"变体 {variant!r} 必须被 scan_sensitive 检出"

    with pytest.raises(ApiError) as e:
        DesensitizeService.assert_clean(variant)
    assert e.value.code == "sensitive_data", f"变体 {variant!r} 必须被 assert_clean 拒绝"

    red = DesensitizeService().regex_redact(variant)
    assert red["redacted"] != variant, f"变体 {variant!r} 必须被抹除"
    assert red["replaced"], f"变体 {variant!r} 必须产生 replaced 审计记录"
    assert masked not in red["redacted"], f"抹除后不得残留明文号码 {masked!r}"


def test_phone_variants_all_rejected_and_masked():
    for v in PHONE_VARIANTS:
        _assert_variant_rejected("13812345678", v)


def test_id_card_variants_all_rejected_and_masked():
    for v in ID_VARIANTS:
        # 抹除后不得残留 18 位明文
        _assert_variant_rejected("110101199001011234", v)


@pytest.mark.asyncio
async def test_phone_variant_desensitize_double_layer(env):
    """desensitize()（正则+LLM 降级）对变体同样抹除。"""
    from app.services.desensitize import DesensitizeService

    svc = DesensitizeService()
    for v in PHONE_VARIANTS:
        r = await svc.desensitize(v)
        assert "13812345678" not in r["redacted"], f"变体 {v!r} 经 desensitize 后仍残留明文"
        assert r["replaced"], f"变体 {v!r} 经 desensitize 后无 replaced 记录"
    for v in ID_VARIANTS:
        r = await svc.desensitize(v)
        assert "110101199001011234" not in r["redacted"], f"身份证变体 {v!r} 仍残留明文"


def test_desensitize_clean_text_unchanged():
    from app.services.desensitize import DesensitizeService

    text = "今天天气很好，我们去公园散步吧，顺便聊聊最近的投资机会"
    assert DesensitizeService.scan_sensitive(text) == []
    r = DesensitizeService().regex_redact(text)
    assert r["redacted"] == text and r["replaced"] == []


def test_cases_publish_rejects_phone_variant(env):
    """端到端：case publish 前置 assert_clean 拒绝变体（A6/C7 实测入库路径）。"""
    from app.services.cases import CaseService

    det_id = _mk_det(env)
    svc = CaseService()
    with pytest.raises(ApiError) as e:
        svc.publish(env, det_id=det_id, redacted_payload="骗子电话 138 1234 5678 大家小心")
    assert e.value.code == "sensitive_data"
    assert env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0


# ===========================================================================
# 4) P1-D2/D8：XFF 伪造不再放行回环端点、不再打穿限流
# ===========================================================================


def _scope(client_host: str, *, xff: str | None = None, x_real: str | None = None) -> dict:
    headers = [(b"host", b"127.0.0.1:9200")]
    if xff:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    if x_real:
        headers.append((b"x-real-ip", x_real.encode("latin-1")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/scan/text",
        "raw_path": b"/api/v1/scan/text",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (client_host, 12345),
        "server": ("127.0.0.1", 9200),
    }


def test_require_loopback_rejects_forged_xff():
    """非回环对端 + XFF=127.0.0.1 必须 403；回环对端 + 伪造 XFF 头不参与判定。"""
    from fastapi import Request as FastAPIRequest

    from app.api.auth import _require_loopback

    settings = get_settings()

    # 伪造：真实对端 10.0.0.7（非回环）携带 XFF=127.0.0.1 / X-Real-IP=127.0.0.1
    req1 = FastAPIRequest(_scope("10.0.0.7", xff="127.0.0.1", x_real="127.0.0.1"))
    with pytest.raises(ApiError) as e1:
        _require_loopback(req1, settings)
    assert e1.value.code == "loopback_only", "伪造 XFF 不得放行回环端点"

    # 真实回环对端 + 伪造外部 XFF → 仍放行（判定只看真实 TCP 对端）
    req2 = FastAPIRequest(_scope("127.0.0.1", xff="203.0.113.9", x_real="8.8.8.8"))
    _require_loopback(req2, settings)  # 不应抛异常


def test_local_login_rejects_forged_xff(tmp_path, monkeypatch):
    """API 层：非回环对端 + 伪造 XFF → 403（XFF 不参与回环判定）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()

    from app.main import app

    # 先以回环客户端跑一次 lifespan：生成 bootstrap key 与表结构
    with TestClient(app, client=("127.0.0.1", 50000)):
        pass
    # 非回环对端（TestClient 默认 client="testclient"）+ 伪造 XFF / X-Real-IP
    with TestClient(app) as c2:
        r = c2.post("/api/v1/auth/local-login",
                    headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "loopback_only"
        r2 = c2.get("/api/v1/auth/current-key", headers={"X-Forwarded-For": "127.0.0.1"})
        assert r2.status_code == 403
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()


class _Sink:
    """吞掉请求的桩 ASGI 应用：统计进入 app 的次数。"""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send) -> None:
        self.calls += 1


class _DummySend:
    def __init__(self) -> None:
        self.messages: list = []

    async def __call__(self, message) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_rate_limit_ignores_xff_uses_real_peer(tmp_path, monkeypatch):
    """限流键 = 真实 TCP 对端：换 XFF 不换桶，容量 2 时第 3 次（换 XFF）仍 429。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.setenv("AF_RATE_LIMIT_PER_MIN", "2")
    get_settings.cache_clear()
    dbmod.close_all()
    from app.middleware.rate_limit import RateLimitMiddleware, reset_rate_limiters

    reset_rate_limiters()
    sink = _Sink()
    mw = RateLimitMiddleware(sink)
    send = _DummySend()
    try:
        for xff in ("127.0.0.1", "8.8.8.8", "203.0.113.9"):
            await mw(_scope("10.0.0.7", xff=xff), None, send)
        keys = set(mw._buckets.keys())
        assert keys == {"10.0.0.7"}, f"限流键必须为真实 TCP 对端（忽略 XFF），实际 {keys}"
        assert sink.calls == 2, "容量 2：换 XFF 的第 3 次必被 429（不得打穿限流）"
        assert any(m.get("type") == "http.response.start" and m.get("status") == 429
                   for m in send.messages)
    finally:
        reset_rate_limiters()
        get_settings.cache_clear()


def test_rate_limit_xff_cannot_rotate_buckets(tmp_path, monkeypatch):
    """API 层：桶枯竭后更换 X-Forwarded-For 不得换桶打穿 429。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.setenv("AF_RATE_LIMIT_PER_MIN", "3")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()
    from app.main import app

    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        body = {"text": "稳赚不赔，导师带你内幕消息"}
        for i, xff in enumerate(["203.0.113.1", "203.0.113.2", "203.0.113.3"]):
            r = c.post("/api/v1/scan/text", json=body, headers={"X-Forwarded-For": xff})
            assert r.status_code == 200, f"第 {i + 1} 次应放行"
        r4 = c.post("/api/v1/scan/text", json=body, headers={"X-Forwarded-For": "203.0.113.4"})
        assert r4.status_code == 429, "更换 XFF 不得打穿限流（限流键=真实对端）"
    dbmod.close_all()
    llm_provider.reset_provider()
    get_settings.cache_clear()
    reset_rate_limiters()


def test_run_disables_proxy_headers(monkeypatch):
    """启动路径必须显式 uvicorn proxy_headers=False：XFF 不再改写 scope['client']。"""
    import sys

    import app.run as run_mod

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(run_mod.uvicorn, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["app.run"])
    with pytest.raises(SystemExit):
        run_mod.main()
    assert captured.get("proxy_headers") is False, "必须显式关闭 proxy_headers"
    assert captured.get("forwarded_allow_ips") == [], "不得信任任何转发头来源"


# ===========================================================================
# 5) P1-A1：扫描/分级/告警/已读产生事件
# ===========================================================================


@pytest.mark.asyncio
async def test_scan_text_writes_detection_scanned_event(env):
    from app.services.speech_engine import SpeechEngine

    r = await SpeechEngine().scan_text(
        "稳赚不赔的理财项目，导师带你内幕消息", source="manual", conn=env
    )
    det_id = r["detection_id"]
    ev = env.execute(
        "SELECT payload FROM events WHERE kind='detection_scanned' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev is not None, "扫描后必须有 detection_scanned 事件"
    payload = json.loads(ev["payload"])
    assert payload["detection_id"] == det_id
    assert payload["source"] == "manual" and "verdict" in payload


def test_scan_endpoint_writes_event(api):
    r = api.post("/api/v1/scan/text", json={"text": "稳赚不赔的理财项目，导师带你内幕消息"})
    assert r.status_code == 200
    conn = dbmod.get_conn()
    n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='detection_scanned'").fetchone()["n"]
    assert n >= 1


def test_grading_alert_and_read_write_events(env):
    """L3+ 分级 → detection_graded + alert_created；mark_read → alert_read。"""
    from app.services.alerting import AlertService
    from app.services.grading import GradingService

    det_id = _mk_det(env)
    r = GradingService().finalize(env, det_id, trap_hit_count=1)  # 蜜饵实锤 → L5 必出告警
    assert r["grade"] == "L5"

    ev_graded = env.execute(
        "SELECT payload FROM events WHERE kind='detection_graded' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev_graded is not None, "分级后必须有 detection_graded 事件"
    assert json.loads(ev_graded["payload"])["detection_id"] == det_id

    ev_created = env.execute(
        "SELECT payload FROM events WHERE kind='alert_created' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev_created is not None, "告警生成后必须有 alert_created 事件"
    alert_id = json.loads(ev_created["payload"])["alert_id"]
    assert alert_id >= 1

    AlertService().mark_read(env, alert_id)
    ev_read = env.execute(
        "SELECT payload FROM events WHERE kind='alert_read' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev_read is not None, "标记已读后必须有 alert_read 事件"
    assert json.loads(ev_read["payload"])["alert_id"] == alert_id