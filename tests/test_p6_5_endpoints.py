"""P6.5 收尾端点测试：/events /config /scan/inbox + /ui/ 静态托管。

覆盖 DoD：
  1) events 分页/按 kind 过滤/单查
  2) config GET 返回合成结构；PUT 无 confirm 拒绝（403）、reason<20 字拒绝（422）、
     confirm+reason 成功并写 configs + gate_logs 审计
  3) scan/inbox 批量消费 pending detections（status pending→scanned、落规则分与分级）
  4) /ui/ 静态托管 200（SPA fallback；资源 404 不吞；/api/ 封包不受影响）
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as c:
        yield c
    dbmod.close_all()
    llm_provider.set_provider(None)
    get_settings.cache_clear()


def _admin_key(tmp_path) -> str:
    """P1-1：写操作端点（PUT /config、POST /scan/inbox）需 admin key。"""
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


def _insert_event(conn, kind: str, payload: dict | None = None):
    conn.execute(
        "INSERT INTO events (kind, payload) VALUES (?, ?)",
        (kind, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


def _mk_pending(conn, content: str, source: str = "zhihu"):
    import hashlib

    h = hashlib.sha256(content.encode()).hexdigest()
    conn.execute(
        "INSERT INTO detections (source, text_hash, content, status) VALUES (?, ?, ?, 'pending')",
        (source, h, content),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]


# ================= /events =================

def _events_auth(tmp_path) -> dict:
    """/events 为受保护端点（P1-1）：读取需 admin key。"""
    return {"X-API-Key": _admin_key(tmp_path)}


def test_events_empty_and_insert(client, tmp_path):
    auth = _events_auth(tmp_path)
    r = client.get("/api/v1/events", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["data"] == {"page": 1, "page_size": 20, "total": 0, "items": []}

    conn = dbmod.get_conn()
    e1 = _insert_event(conn, "trap_hit", {"trap_id": 7})
    e2 = _insert_event(conn, "alert")
    r2 = client.get("/api/v1/events", headers=auth)
    data = r2.json()["data"]
    assert data["total"] == 2 and len(data["items"]) == 2
    assert data["items"][0]["kind"] == "alert"          # 倒序
    assert data["items"][1]["payload"]["trap_id"] == 7  # payload JSON 解析


def test_events_kind_filter_and_get(client, tmp_path):
    auth = _events_auth(tmp_path)
    conn = dbmod.get_conn()
    _insert_event(conn, "trap_hit", {"trap_id": 1})
    _insert_event(conn, "case_published", {"case_id": 2})
    r = client.get("/api/v1/events", params={"kind": "trap_hit"}, headers=auth)
    data = r.json()["data"]
    assert data["total"] == 1 and data["items"][0]["kind"] == "trap_hit"

    eid = client.get("/api/v1/events", params={"kind": "case_published"}, headers=auth).json()["data"]["items"][0]["id"]
    r2 = client.get(f"/api/v1/events/{eid}", headers=auth)
    assert r2.json()["data"]["kind"] == "case_published"
    assert client.get("/api/v1/events/99999", headers=auth).status_code == 404


def test_events_pagination(client, tmp_path):
    auth = _events_auth(tmp_path)
    conn = dbmod.get_conn()
    for i in range(5):
        _insert_event(conn, "ping", {"i": i})
    p1 = client.get("/api/v1/events", params={"page": 1, "page_size": 2}, headers=auth).json()["data"]
    p3 = client.get("/api/v1/events", params={"page": 3, "page_size": 2}, headers=auth).json()["data"]
    assert p1["total"] == 5 and len(p1["items"]) == 2
    assert len(p3["items"]) == 1
    assert p1["items"][0]["id"] != p3["items"][0]["id"]


# ================= /config =================

def test_config_get_structure(client):
    r = client.get("/api/v1/config")
    assert r.status_code == 200
    d = r.json()["data"]
    for key in ("llm", "degrade", "zhihu", "speech", "trap", "grading", "overrides"):
        assert key in d
    assert "api_key_set" in d["llm"] and "configured" in d["llm"]
    assert d["grading"]["l3_min"] == 10.0
    assert "config_put" not in d  # 不暴露密钥


def test_config_put_without_confirm_rejected(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = client.put("/api/v1/config", json={
        "key": "grading.l3_min", "value": 9.5,
        "confirm": False, "reason": "这是一条超过二十个字的配置修改理由说明",
    }, headers=auth)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "confirm_required"


def test_config_put_short_reason_rejected(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = client.put("/api/v1/config", json={
        "key": "grading.l3_min", "value": 9.5,
        "confirm": True, "reason": "改一下",
    }, headers=auth)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "reason_too_short"


def test_config_put_success_with_audit(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    reason = "这是一条超过二十个字的配置修改理由说明文本"
    r = client.put("/api/v1/config", json={
        "key": "grading.l3_min", "value": 9.5, "confirm": True, "reason": reason,
    }, headers=auth)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["key"] == "grading.l3_min" and data["value"] == 9.5
    assert data["audit"]["gate_log_id"] >= 1

    # configs 表落库 + GET overrides 可见
    r2 = client.get("/api/v1/config")
    assert r2.json()["data"]["overrides"]["grading.l3_min"] == 9.5
    # gate_logs 审计行
    conn = dbmod.get_conn()
    log = conn.execute("SELECT * FROM gate_logs WHERE action='config.put'").fetchone()
    assert log is not None and log["reason"] == reason
    assert log["payload_hash"] and len(log["payload_hash"]) == 64
    assert json.loads(log["after"])["grading.l3_min"] == 9.5
    # 事件流落库
    ev = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='config_updated'").fetchone()["n"]
    assert ev >= 1


def test_config_put_invalid_key_rejected(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    # 注意：reason 必须 ≥20 字（先于 key 格式校验），此处用与成功用例相同的 21 字理由
    r = client.put("/api/v1/config", json={
        "key": "BAD KEY", "value": 1, "confirm": True, "reason": "这是一条超过二十个字的配置修改理由说明文本",
    }, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_config_key"


# ================= /scan/inbox =================

def test_scan_inbox_processes_pending(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    conn = dbmod.get_conn()
    pid = _mk_pending(conn, "稳赚不赔的理财项目，导师带你内幕消息")
    r = client.post("/api/v1/scan/inbox", headers=auth)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["processed"] == 1
    assert data["items"][0]["detection_id"] == pid
    assert data["items"][0]["verdict"] in ("suspicious", "fraud")
    assert data["items"][0]["rule_score"] > 0

    row = conn.execute("SELECT * FROM detections WHERE id=?", (pid,)).fetchone()
    assert row["status"] == "scanned"
    assert row["rule_score"] > 0 and row["grade"] in ("L2", "L3", "L4")
    hits = conn.execute("SELECT COUNT(*) AS n FROM speech_hits WHERE det_id=?", (pid,)).fetchone()["n"]
    assert hits >= 1


def test_scan_inbox_empty_when_no_pending(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = client.post("/api/v1/scan/inbox", headers=auth)
    assert r.status_code == 200
    assert r.json()["data"]["processed"] == 0 and r.json()["data"]["items"] == []


def test_scan_inbox_result_list(client, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    conn = dbmod.get_conn()
    _mk_pending(conn, "刷单返利，垫付解锁任务")
    client.post("/api/v1/scan/inbox", headers=auth)
    # P2-7：GET /scan/inbox 返回对话原文，读取需 admin
    r_no_key = client.get("/api/v1/scan/inbox")
    assert r_no_key.status_code == 401
    r = client.get("/api/v1/scan/inbox", headers=auth)
    assert r.status_code == 200
    items = r.json()["data"]
    assert len(items) >= 1
    assert items[0]["status"] == "scanned" and items[0]["grade"] is not None


# ================= /ui/ 静态托管 =================

def test_ui_static_hosting(client):
    from pathlib import Path

    dist = Path(__file__).resolve().parents[1] / "webui" / "dist"
    if not dist.is_dir():
        pytest.skip("webui/dist 不存在，跳过 /ui/ 托管用例")
    # 根路径 → 重定向到 /ui/
    r0 = client.get("/", follow_redirects=False)
    assert r0.status_code in (307, 308)
    # /ui/ 与 /ui 返回 index.html
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    r2 = client.get("/ui")
    assert r2.status_code == 200
    # SPA fallback：未知路由回退 index.html
    r3 = client.get("/ui/some/deep/route")
    assert r3.status_code == 200 and "text/html" in r3.headers.get("content-type", "")
    # 已知资源（assets 下的真实文件）返回文件
    assets = list((dist / "assets").glob("*.js"))
    if assets:
        r4 = client.get(f"/ui/assets/{assets[0].name}")
        assert r4.status_code == 200 and "javascript" in r4.headers.get("content-type", "")
    # 不存在的资源文件 → 404 且保持封包结构（不破坏 /api/ 404 语义）
    r5 = client.get("/ui/assets/not-exist-xyz.js")
    assert r5.status_code == 404 and r5.json()["ok"] is False


def test_ui_does_not_break_api_envelope(client):
    # /api/ 404 仍为统一封包
    r = client.get("/api/v1/nonexistent")
    assert r.status_code == 404 and r.json() == {"ok": False, "error": {"code": "not_found", "message": "Not Found"}}
    # 受保护端点仍 401 封包
    r2 = client.get("/api/v1/system/stats")
    assert r2.status_code == 401 and r2.json()["ok"] is False
    # health 仍正常
    assert client.get("/api/v1/system/health").status_code == 200