"""P1 修复回归测试（审查报告 P1-1 ~ P1-4）。

覆盖：
  1) P1-1 敏感写操作端点认证：无 key / 错误 key → 401（每类端点负例）
  2) P1-2 退饵/发布案例 confirm+reason 双确认 + gate_logs 审计
  3) P1-3 LLM Provider 单例化：熔断/预算状态跨请求保留 + 自然日预算滚动
  4) P1-4 知乎全局限速/退避单例：跨 Bridge 共享 + 超配额拒绝

运行：pytest tests/test_p1_security.py -q（项目根）。
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider, zhihu_bridge
from app.services.llm_provider import DegradedError, LLMProvider
from app.services.trap_engine import TrapEngine
from app.services.zhihu_bridge import RateLimitExceeded, ZhihuBridge

REASON_20 = "这是一条超过二十个字的敏感操作理由说明文本"

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离 API 客户端：临时 data 目录 + 清空 provider/限流单例（P1-3/P1-4 隔离）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.reset_provider()
    zhihu_bridge.reset_global_limits()

    from app.main import app

    with TestClient(app) as c:
        yield c
    dbmod.close_all()
    llm_provider.reset_provider()
    zhihu_bridge.reset_global_limits()
    get_settings.cache_clear()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


def _mk_det(conn, content: str = "稳赚不赔的理财项目") -> int:
    h = hashlib.sha256(content.encode()).hexdigest()
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, llm_verdict, "
        "judge_confidence, grade, status) VALUES ('manual', ?, ?, 9.0, 'fraud', 0.9, 'L4', 'processed')",
        (h, content),
    )
    conn.commit()
    return int(cur.lastrowid)


# ===========================================================================
# P1-1：敏感写操作端点认证
# ===========================================================================

# 每类受保护端点一个代表请求（无 key / 错误 key 必须 401）
PROTECTED_WRITE_CALLS = [
    ("post", "/api/v1/cases/publish",
     {"det_id": 1, "redacted_payload": "脱敏案例载荷", "confirm": True, "reason": REASON_20}),
    ("put", "/api/v1/config",
     {"key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": REASON_20}),
    ("post", "/api/v1/traps", {"bait_text": "测试蜜饵文案测试蜜饵文案"}),
    ("post", "/api/v1/traps/generate-draft", {"template_id": "t_invest_flow"}),
    ("post", "/api/v1/traps/1/deploy", {"target_url": "https://x.test/a"}),
    ("post", "/api/v1/traps/1/monitor", None),
    ("post", "/api/v1/traps/1/disable", None),
    ("post", "/api/v1/traps/1/retire", {"confirm": True, "reason": REASON_20}),
    ("post", "/api/v1/evidence/build", {"det_ids": [1]}),
    ("post", "/api/v1/evidence/1/freeze", None),
    ("get", "/api/v1/evidence/1/export", None),
    ("post", "/api/v1/scan/inbox", None),
]


@pytest.mark.parametrize("method,path,body", PROTECTED_WRITE_CALLS,
                         ids=[f"{m}-{p}" for m, p, _ in PROTECTED_WRITE_CALLS])
def test_write_endpoint_rejects_missing_key(api, method, path, body):
    r = api.request(method, path, json=body)
    assert r.status_code == 401, f"{method.upper()} {path} 无 key 应 401，实际 {r.status_code}"
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"] == "missing_api_key"


@pytest.mark.parametrize("method,path,body", PROTECTED_WRITE_CALLS,
                         ids=[f"{m}-{p}" for m, p, _ in PROTECTED_WRITE_CALLS])
def test_write_endpoint_rejects_wrong_key(api, method, path, body):
    r = api.request(method, path, json=body, headers={"X-API-Key": "af_admin_deadbeef"})
    assert r.status_code == 401, f"{method.upper()} {path} 错误 key 应 401，实际 {r.status_code}"
    assert r.json()["error"]["code"] == "invalid_api_key"


# ===========================================================================
# P1-2：退饵/发布案例 confirm+reason 双确认 + GateLog 审计
# ===========================================================================


def _mk_active_trap(conn) -> int:
    d = TrapEngine().create_draft(conn, bait_text="你好，我有个不错的兼职项目，加微信聊聊怎么样")
    tid = d["id"]
    TrapEngine().deploy(conn, tid, target_url="https://x.test/a")
    return tid


def test_retire_requires_confirm(api, tmp_path):
    conn = dbmod.get_conn()
    tid = _mk_active_trap(conn)
    auth = {"X-API-Key": _admin_key(tmp_path)}

    # 无请求体 → confirm_required 403
    r0 = api.post(f"/api/v1/traps/{tid}/retire", headers=auth)
    assert r0.status_code == 403 and r0.json()["error"]["code"] == "confirm_required"

    # confirm=false → 403
    r1 = api.post(f"/api/v1/traps/{tid}/retire", json={"confirm": False, "reason": REASON_20}, headers=auth)
    assert r1.status_code == 403 and r1.json()["error"]["code"] == "confirm_required"

    # reason<20 → 422
    r2 = api.post(f"/api/v1/traps/{tid}/retire", json={"confirm": True, "reason": "短理由"}, headers=auth)
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "reason_too_short"

    # 蜜饵未被上述失败请求退役
    assert conn.execute("SELECT status FROM honey_facts WHERE id=?", (tid,)).fetchone()["status"] == "active"


def test_retire_with_confirm_success_and_gate_log(api, tmp_path):
    conn = dbmod.get_conn()
    tid = _mk_active_trap(conn)
    auth = {"X-API-Key": _admin_key(tmp_path)}

    r = api.post(f"/api/v1/traps/{tid}/retire", json={"confirm": True, "reason": REASON_20}, headers=auth)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "retired" and data["retired_reason"] == "user"

    # gate_logs 审计（P1-2）
    log = conn.execute("SELECT * FROM gate_logs WHERE action='trap.retire' ORDER BY id DESC").fetchone()
    assert log is not None
    assert log["reason"] == REASON_20
    assert len(log["payload_hash"]) == 64
    before = __import__("json").loads(log["before"])
    after = __import__("json").loads(log["after"])
    assert before["status"] == "active"
    assert after["status"] == "retired"
    # 事件流落库（engine.retire 已写 trap_retired）
    ev = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='trap_retired' AND payload LIKE ?",
                      (f'%{tid}%',)).fetchone()["n"]
    assert ev >= 1


def test_publish_requires_confirm(api, tmp_path):
    conn = dbmod.get_conn()
    det_id = _mk_det(conn)
    auth = {"X-API-Key": _admin_key(tmp_path)}
    body = {"det_id": det_id, "redacted_payload": "脱敏后的案例内容"}

    r1 = api.post("/api/v1/cases/publish", json=body, headers=auth)
    assert r1.status_code == 403 and r1.json()["error"]["code"] == "confirm_required"

    r2 = api.post("/api/v1/cases/publish", json={**body, "confirm": True, "reason": "短"}, headers=auth)
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "reason_too_short"

    assert conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 0


def test_publish_with_confirm_success_and_gate_log(api, tmp_path):
    conn = dbmod.get_conn()
    det_id = _mk_det(conn)
    auth = {"X-API-Key": _admin_key(tmp_path)}

    r = api.post("/api/v1/cases/publish", json={
        "det_id": det_id, "redacted_payload": "脱敏后的案例内容",
        "graph_tags": ["fake_investment"], "confirm": True, "reason": REASON_20,
    }, headers=auth)
    assert r.status_code == 200
    case_id = r.json()["data"]["id"]

    log = conn.execute("SELECT * FROM gate_logs WHERE action='case.publish' ORDER BY id DESC").fetchone()
    assert log is not None and log["reason"] == REASON_20
    assert len(log["payload_hash"]) == 64
    after = __import__("json").loads(log["after"])
    assert after["case_id"] == case_id
    ev = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='case_published'").fetchone()["n"]
    assert ev >= 1


# ===========================================================================
# P1-3：LLM Provider 单例（熔断/预算状态跨请求保留）
# ===========================================================================

def test_get_provider_returns_same_instance():
    llm_provider.reset_provider()
    try:
        p1 = llm_provider.get_provider()
        p2 = llm_provider.get_provider()
        assert p1 is p2, "P1-3：get_provider() 必须返回同一单例（状态跨请求保留）"
        # 配置指纹变化 → 重建（测试注入/改配置后不拿旧实例）
        assert p1._budget_date  # 日期字段存在
    finally:
        llm_provider.reset_provider()


class _FailingClient:
    """httpx.AsyncClient 替身：post 恒抛连接错误，计数跨实例累计。"""

    post_calls = 0

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        type(self).post_calls += 1
        raise httpx.ConnectError("boom")


@pytest.mark.asyncio
async def test_fail_streak_accumulates_and_trips_breaker(monkeypatch, tmp_path):
    """同一进程连续失败 MAX_FAIL_STREAK 次 → 第 N+1 次直接抛 DegradedError 且不再发网络。"""
    llm_provider.reset_provider()
    try:
        monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))  # R3-X1：熔断事件写隔离库
        monkeypatch.setenv("AF_LLM_BASE_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("AF_LLM_API_KEY", "k")
        monkeypatch.setenv("AF_LLM_MODEL", "m")
        monkeypatch.setenv("AF_LLM_TIMEOUT", "1")
        get_settings.cache_clear()
        monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)

        p = llm_provider.get_provider()
        p2 = llm_provider.get_provider()
        assert p is p2

        for i in range(3):
            with pytest.raises(DegradedError):
                await p.complete(system="s", user="u")
        # 第 4 次：已熔断 → 冷却期内不发网络直接抛（含"熔断"）
        with pytest.raises(DegradedError) as ei:
            await p.complete(system="s", user="u")
        assert "熔断" in str(ei.value)
        assert p.degraded is True
        assert _FailingClient.post_calls == 3, "熔断后不得再发网络请求"
    finally:
        llm_provider.reset_provider()
        get_settings.cache_clear()


def test_budget_rolls_on_natural_day_change():
    p = LLMProvider(base_url="x", api_key="k", model="m", daily_budget=1)
    p.used_today = 1
    p._budget_date = "2000-01-01"  # 伪造"昨天"
    p._roll_budget()
    assert p.used_today == 0, "P1-3：自然日变更后当日计数自动重置"
    assert p._budget_date != "2000-01-01"


# ===========================================================================
# P1-4：知乎全局限速/退避单例
# ===========================================================================

def test_global_limiter_and_backoff_shared_across_bridges(monkeypatch):
    zhihu_bridge.reset_global_limits()
    try:
        monkeypatch.setenv("AF_ZHIHU_RATE_LIMIT", "2")
        get_settings.cache_clear()
        s = get_settings()
        b1 = ZhihuBridge(settings=s)
        b2 = ZhihuBridge(settings=s)
        assert b1.limiter is b2.limiter, "P1-4：所有 Bridge 必须共享同一全局令牌桶"
        assert b1.backoff is b2.backoff, "P1-4：所有 Bridge 必须共享同一全局退避"

        # 共享桶：容量 2 → 第 3 次跨实例被拒
        b1._gate("ch-c")
        b2._gate("ch-c")
        with pytest.raises(RateLimitExceeded):
            b1._gate("ch-c")

        # 退避共享：b1 记失败 → b2 看到冷却
        b2.backoff.record_failure("ch-a")
        assert b1.backoff.due("ch-a") is False
    finally:
        zhihu_bridge.reset_global_limits()
        get_settings.cache_clear()


def test_global_limiter_rebuilds_when_capacity_changes(monkeypatch):
    zhihu_bridge.reset_global_limits()
    try:
        monkeypatch.setenv("AF_ZHIHU_RATE_LIMIT", "5")
        get_settings.cache_clear()
        lim1 = zhihu_bridge.get_global_limiter(get_settings())
        assert lim1.capacity == 5

        monkeypatch.setenv("AF_ZHIHU_RATE_LIMIT", "10")
        get_settings.cache_clear()
        lim2 = zhihu_bridge.get_global_limiter(get_settings())
        assert lim2 is not lim1, "容量配置变更后应重建单例"
        assert lim2.capacity == 10
    finally:
        zhihu_bridge.reset_global_limits()
        get_settings.cache_clear()


def test_status_endpoint_reads_shared_limiter(api, monkeypatch):
    """GET /zhihu/status 返回的是全局共享限速器的实时水位。"""
    r = api.get("/api/v1/zhihu/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["rate_limit"]["capacity"] == 20  # 默认 AF_ZHIHU_RATE_LIMIT
    assert data["rate_limit"]["available"] >= 0


# ===========================================================================
# P2-8 补充：安全响应头（CSP / X-Frame-Options / nosniff / Referrer-Policy）
# ===========================================================================

def test_security_headers_present_on_all_responses(api):
    """正常、错误（401/404）响应都应带安全头（中间件最外层，覆盖 429/4xx/5xx）。"""
    r = api.get("/api/v1/system/health")
    assert r.status_code == 200
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("referrer-policy") == "no-referrer"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp   # Element Plus/Vite 内联样式
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp

    # 未认证 401 与 404 同样带安全头（中间件在最外层）
    r401 = api.get("/api/v1/system/stats")
    assert r401.status_code == 401
    assert r401.headers.get("x-frame-options") == "DENY"
    r404 = api.get("/api/v1/nonexistent")
    assert r404.status_code == 404
    assert r404.headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'self'" in r404.headers.get("content-security-policy", "")
