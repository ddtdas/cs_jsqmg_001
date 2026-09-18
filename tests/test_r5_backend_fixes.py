"""R5 修复回归测试（金丝雀蜜罐修复工程师第 5 轮 · 队A/C/D 验证 P1 + 高价值 P2）。

覆盖修复项：
  1. [P1-A] evidence freeze 缺 confirm+reason → 403/422；补齐后 200 且 gate_logs
     落 action='evidence.freeze' 审计（reason 原样入库）。
  2. [P1-B/C] supply_chain 超长域名（单标签>63 / 总长>253）→ 422 seed_invalid（不再 500
     UnicodeError）；概念词回归 200 concept_only。
  3. [P1-D] MCP 透传层过滤 None 参数：af_grade_explain（account_risk=None 缺省）、
     af_list_alerts（level=None 缺省）请求中不得出现空串参数。
  4. [P1-C] grading.finalize 三写单事务：正常路径 grade+event+alert 一次性落库；
     告警写入抛异常时整体 rollback（不留部分提交）。
  5. [P2-A] current-key 响应不再含 key_file 绝对路径（live 主控 127.0.0.1 实测）。
  6. [P2-D] MCP 补齐 af_collector_matrix / af_collector_add_cell / af_collector_run
     透传工具（挂载 + mock 透传断言）。
  7. [P2-C] 冻结证据源 detection 缺失：verify/export 不 404，标注 source_deleted；
     build 严格路径仍 404（回归）。
  8. [P1-A 联动] MCP af_freeze_evidence 同步 confirm+reason 透传。

运行：pytest tests/test_r5_backend_fixes.py -q（项目根）。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.alerting import AlertService
from app.services.evidence import EvidenceService
from app.services.grading import GradingService
from app.utils import ApiError


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据库（AF_DATA_DIR=tmp）+ 无 LLM（降级路径）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.set_provider(None)
    get_settings.cache_clear()


def _mk_det(conn, *, content: str = "稳赚不赔的理财项目，导师带你内幕消息",
            rule_score: float = 15.0, llm_verdict: str | None = None,
            hits: list[tuple[str, float]] | None = None) -> int:
    """建一条检测记录（可带规则命中明细）。"""
    text = content or "test"
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, llm_verdict, "
        "judge_confidence, grade, status) VALUES ('manual', ?, ?, ?, ?, NULL, NULL, 'processed')",
        (hashlib.sha256(text.encode()).hexdigest(), text, rule_score, llm_verdict),
    )
    det_id = int(cur.lastrowid)
    for pattern, score in (hits or [("稳赚不赔", 9.0), ("投资理财高回报", 6.0)]):
        row = conn.execute("SELECT id FROM speech_patterns WHERE pattern=? LIMIT 1", (pattern,)).fetchone()
        if row is None:
            continue
        conn.execute(
            "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?,?,?,?)",
            (det_id, row["id"], pattern, score),
        )
    conn.commit()
    return det_id


def _fresh_app(tmp_path, monkeypatch):
    """TestClient 用：隔离 DB + 无 LLM 的 FastAPI 应用。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    return app


# ============================ 1. freeze confirm+reason ============================
def test_freeze_requires_confirm_and_reason(tmp_path, monkeypatch):
    """freeze 无 confirm → 403；confirm 但 reason<20 → 422；补齐后 200 + gate_logs 落库。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
            auth = {"X-API-Key": key}
            reason = "这是一条超过二十个字长度的证据冻结理由说明文本"

            r = client.post("/api/v1/scan/text", json={"text": "稳赚不赔的理财项目，导师带你内幕消息"})
            det_id = r.json()["data"]["detection_id"]
            r1 = client.post("/api/v1/evidence/build", json={"det_ids": [det_id]}, headers=auth)
            pkg_id = r1.json()["data"]["pkg_id"]

            # 无 confirm（空 body）→ 403 confirm_required
            r403 = client.post(f"/api/v1/evidence/{pkg_id}/freeze", json={}, headers=auth)
            assert r403.status_code == 403
            assert r403.json()["error"]["code"] == "confirm_required"

            # confirm=false → 403
            r403b = client.post(f"/api/v1/evidence/{pkg_id}/freeze",
                                json={"confirm": False, "reason": reason}, headers=auth)
            assert r403b.status_code == 403
            assert r403b.json()["error"]["code"] == "confirm_required"

            # confirm=true 但 reason 过短 → 422 reason_too_short
            r422 = client.post(f"/api/v1/evidence/{pkg_id}/freeze",
                               json={"confirm": True, "reason": "太短了"}, headers=auth)
            assert r422.status_code == 422
            assert r422.json()["error"]["code"] == "reason_too_short"

            # 补齐 confirm+reason → 200 frozen
            r200 = client.post(f"/api/v1/evidence/{pkg_id}/freeze",
                               json={"confirm": True, "reason": reason}, headers=auth)
            assert r200.status_code == 200
            assert r200.json()["data"]["frozen"] is True

            # gate_logs 审计落库（action=evidence.freeze + reason 原样）
            log = dbmod.get_conn().execute(
                "SELECT * FROM gate_logs WHERE action='evidence.freeze' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            assert log is not None, "缺少 evidence.freeze 审计"
            assert log["reason"] == reason
            assert json.loads(log["after"])["frozen"] is True
    finally:
        dbmod.close_all()
        get_settings.cache_clear()


# ============================ 2. supply_chain 超长域名 ============================
def test_supply_chain_long_domain_422(tmp_path, monkeypatch):
    """超长单标签 / 超长域名 → 422 seed_invalid（原 500 UnicodeError）。"""
    app = _fresh_app(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
            auth = {"X-API-Key": key}

            # 单标签 70 字符（>63）——触发 getaddrinfo idna UnicodeError 的复现场景
            r = client.post("/api/v1/supply-chain/query",
                            json={"seed": "https://" + "a" * 70 + ".com/x"}, headers=auth)
            assert r.status_code == 422, f"应为 422，实际 {r.status_code}: {r.text}"
            assert r.json()["error"]["code"] == "seed_invalid"

            # 域名总长 >253
            long_domain = ("a." * 130) + "com"   # 263 字符
            r2 = client.post("/api/v1/supply-chain/query", json={"seed": long_domain}, headers=auth)
            assert r2.status_code == 422
            assert r2.json()["error"]["code"] == "seed_invalid"

            # 概念词回归：无域名 → 200 concept_only
            r3 = client.post("/api/v1/supply-chain/query", json={"seed": "杀猪盘常见套路"}, headers=auth)
            assert r3.status_code == 200
            assert r3.json()["data"]["mode"] == "concept_only"

            # 正常域名 → 200（DNS 成败都回 local_recon 骨架）
            r4 = client.post("/api/v1/supply-chain/query",
                             json={"seed": "https://example.com/xx"}, headers=auth)
            assert r4.status_code == 200
            assert r4.json()["data"]["mode"] == "local_recon"
    finally:
        dbmod.close_all()
        get_settings.cache_clear()


# ============================ 3. MCP None 参数过滤 ============================
def _load_mcp_server():
    """按文件路径加载本项目 mcp/server.py（mcp/ 是命名空间段，不能 import mcp.server）。"""
    spec = importlib.util.spec_from_file_location("af_mcp_server", ROOT / "mcp" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mcp_mod = _load_mcp_server()


def _mock_master(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(
                404,
                json={"ok": False, "error": {"code": "mock_unmapped", "message": f"mock 未注册 {(request.method, request.url.path)}"}},
            )
        if callable(route):
            return route(request)
        status, body = route
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _run(coro):
    return asyncio.run(coro)


def test_mcp_none_params_not_encoded(tmp_path):
    """af_grade_explain / af_list_alerts 缺省 None 参数不落入 query（防空串 422）。"""
    captured = {}

    def grade_route(request: httpx.Request) -> httpx.Response:
        captured["grade_query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "data": {"grade": "L2", "score": 15.0}})

    def alerts_route(request: httpx.Request) -> httpx.Response:
        captured["alerts_query"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True, "data": {"items": [], "total": 0}})

    def freeze_route(request: httpx.Request) -> httpx.Response:
        captured["freeze_body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "data": {"frozen": True}})

    routes = {
        ("GET", "/api/v1/grading/explain/3"): grade_route,
        ("GET", "/api/v1/alerts"): alerts_route,
        ("POST", "/api/v1/evidence/5/freeze"): freeze_route,
    }

    async def go():
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(routes))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            # 缺省调用：account_risk=None / level=None 必须被过滤
            await srv.call_tool("af_grade_explain", {"detection_id": 3})
            await srv.call_tool("af_list_alerts", {})
            # Fix1 联动：af_freeze_evidence 透传 confirm+reason body
            await srv.call_tool(
                "af_freeze_evidence",
                {"pkg_id": 5, "confirm": True, "reason": "这是一条超过二十个字长度的证据冻结理由说明文本"},
            )
        finally:
            await proxy.aclose()

    _run(go())
    assert "account_risk" not in captured["grade_query"], captured
    assert "trap_hit_count" in captured["grade_query"], captured
    assert "level" not in captured["alerts_query"], captured
    assert captured["alerts_query"]["unread_only"] == "false" and captured["alerts_query"]["limit"] == "50", captured
    assert captured["freeze_body"] == {
        "confirm": True, "reason": "这是一条超过二十个字长度的证据冻结理由说明文本"}, captured


# ============================ 4. grading.finalize 单事务 ============================
def test_finalize_single_transaction_ok(env):
    """正常路径：grade + detection_graded 事件 + L3+ 告警一次性落库。"""
    det_id = _mk_det(env, content="稳赚不赔的理财项目", rule_score=15.0, llm_verdict=None)
    r = GradingService().finalize(env, detection_id=det_id)
    assert r["grade"] == "L3"  # rule-only 15 分 ≥ l3_min 10
    row = env.execute("SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
    assert row["grade"] == "L3"
    ev = env.execute("SELECT COUNT(*) AS n FROM events WHERE kind='detection_graded'").fetchone()["n"]
    alert = env.execute("SELECT COUNT(*) AS n FROM alerts WHERE json_extract(payload, '$.detection_id')=?" ,
                        (det_id,)).fetchone()["n"]
    assert ev == 1, "应落 detection_graded 事件"
    assert alert == 1, "L3 应生成正式告警"


def test_finalize_rollback_on_alert_failure(env, monkeypatch):
    """告警写入抛异常 → 整体回滚：grade 不落、无事件、无告警（不留部分提交）。"""
    det_id = _mk_det(env, content="稳赚不赔的理财项目", rule_score=15.0, llm_verdict=None)

    def _boom(self, *a, **k):  # noqa: ANN001
        raise RuntimeError("alert-boom")

    monkeypatch.setattr(AlertService, "evaluate", _boom)
    with pytest.raises(RuntimeError):
        GradingService().finalize(env, detection_id=det_id)

    row = env.execute("SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
    assert row["grade"] is None, "rollback 后 grade 不应落库"
    ev = env.execute("SELECT COUNT(*) AS n FROM events WHERE kind='detection_graded'").fetchone()["n"]
    alert = env.execute("SELECT COUNT(*) AS n FROM alerts WHERE json_extract(payload, '$.detection_id')=?" ,
                        (det_id,)).fetchone()["n"]
    assert ev == 0, "rollback 后不应有 detection_graded 事件"
    assert alert == 0, "rollback 后不应有告警"


# ============================ 5. current-key 不再泄露 key_file ============================
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def test_current_key_no_key_file(tmp_path):
    """GET /auth/current-key（真实回环）不再返回 key_file 绝对路径。"""
    port = _free_port()
    env = os.environ.copy()
    env["AF_DATA_DIR"] = str(tmp_path / "data")
    env["AF_PORT"] = str(port)
    env["AF_API_KEY"] = "af_admin_r5_test"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        if not _wait_http(f"{base}/api/v1/system/health"):
            pytest.skip("真实主控未能启动，跳过 current-key 泄露验证")
        r = httpx.get(f"{base}/api/v1/auth/current-key", timeout=5.0)
        assert r.status_code == 200, r.text
        body = r.json()["data"]
        assert "key" in body and body["key"] == "af_admin_r5_test"
        assert "key_file" not in body, f"current-key 不得泄露 key_file 路径: {body}"
        assert body["source"] == "env"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# ============================ 6. MCP 采集矩阵工具 ============================
def test_mcp_collector_tools_mounted_and_passthrough():
    async def go():
        captured = {}

        def matrix_route(request: httpx.Request) -> httpx.Response:
            captured["matrix"] = (request.method, request.url.path)
            return httpx.Response(200, json={"ok": True, "data": {"cells": []}})

        def add_route(request: httpx.Request) -> httpx.Response:
            captured["add"] = (request.method, request.url.path, json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "data": {"id": 1}})

        def run_route(request: httpx.Request) -> httpx.Response:
            captured["run"] = (request.method, request.url.path)
            return httpx.Response(200, json={"ok": True, "data": {"started": True}})

        routes = {
            ("GET", "/api/v1/collector/matrix"): matrix_route,
            ("POST", "/api/v1/collector/matrix"): add_route,
            ("POST", "/api/v1/collector/matrix/7/run"): run_route,
        }
        proxy = mcp_mod.MasterProxy("http://mock", "af_admin_test", transport=_mock_master(routes))
        srv = mcp_mod.build_mcp_server(proxy=proxy)
        try:
            tools = await srv.list_tools()
            names = [t.name for t in tools]
            for must in ("af_collector_matrix", "af_collector_add_cell", "af_collector_run"):
                assert must in names, f"{must} 未挂载"

            await srv.call_tool("af_collector_matrix", {})
            await srv.call_tool("af_collector_add_cell", {
                "platform": "zhihu", "account": "acct1", "collect": "comments",
                "frequency": "daily", "depth": 3, "keyword": "刷单", "near_dup": 0.8,
            })
            await srv.call_tool("af_collector_run", {"cell_id": 7})
        finally:
            await proxy.aclose()

        assert captured["matrix"] == ("GET", "/api/v1/collector/matrix"), captured
        method, path, body = captured["add"]
        assert (method, path) == ("POST", "/api/v1/collector/matrix")
        assert body["platform"] == "zhihu" and body["account"] == "acct1"
        assert body["strategy"] == {"collect": "comments", "frequency": "daily",
                                    "depth": 3, "keyword": "刷单", "near_dup": 0.8}, body
        assert captured["run"] == ("POST", "/api/v1/collector/matrix/7/run"), captured
        return True

    assert _run(go())


# ============================ 7. 冻结证据源缺失兜底 ============================
def test_evidence_verify_export_source_deleted_tolerant(env):
    """源 detection 被删除：verify/export 不 404，标注 source_deleted；build 仍严格 404。"""
    det_id = _mk_det(env, content="稳赚不赔的理财项目")
    svc = EvidenceService()
    pkg = svc.build(env, [det_id])
    assert pkg["pkg_id"] > 0

    # 删除源检测（模拟数据被清理/归档）
    env.execute("DELETE FROM detections WHERE id=?", (det_id,))
    env.commit()

    ver = svc.verify(env, pkg["pkg_id"])
    assert ver["source_deleted"] is True
    assert ver["missing_det_ids"] == [det_id]
    assert ver["intact"] is False  # 源内容不可重建 → 链断裂（不 404 崩溃）

    js = svc.export(env, pkg["pkg_id"], "json")
    assert js["content"]["items"][0]["source_deleted"] is True
    assert js["verified"]["source_deleted"] is True

    tx = svc.export(env, pkg["pkg_id"], "text")
    assert "源记录已删除" in tx["content"]

    # 回归：build 严格路径（tolerate_missing=False）缺行仍 404
    with pytest.raises(ApiError) as e:
        svc.build(env, [det_id])
    assert e.value.code == "detection_not_found"
