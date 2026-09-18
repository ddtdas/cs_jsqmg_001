"""反钓鱼伪装专项测试（P19）：CoverIdentityService + /api/v1/cover。

运行：cd /d 项目根 && .venv\\Scripts\\python.exe -m pytest tests/test_p19_cover.py -q

覆盖（>=14 条真实运行）：
 1) create 顶层伪装 → layer=1 + enabled=1
 2) create 子伪装 parent_id → layer=parent 层+1（三层嵌套）
 3) create 重复 name → 409 identity_name_exists
 4) create parent_id 不存在 → 404 parent_not_found
 5) create persona_type 非法 → 422 invalid_persona_type
 6) list_identities 含被接触次数
 7) toggle 无 confirm → 403；confirm+reason → enabled 翻转 + gate_logs
 8) delete 无 confirm → 403；confirm+reason → 删除 + gate_logs + 二次删除 404
 9) expose → 返回假信息脱敏清单 + events(kind='cover_expose')
10) register_contact → contact_value 已脱敏落库 + layer_at
11) register_contact 手机号 → IOC 提取 events(kind='cover_contact') 掩码
12) 同 contact_value 第 2 次 → escalated=1 + events(kind='cover_locked')
13) contacts 过滤（身份/类型/锁定）
14) cover_map 嵌套树 + 每层被接触数
15) /cover 无 key → 401
16) register_contact 非法 contact_type → 422
17) 表数 24（cover_identities + contact_events 迁移）+ stats 联动
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app
from app.models import TABLE_NAMES

GOOD_REASON = "双确认测试理由，长度足够二十字以上用于敏感操作审计"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据目录 + 空 LLM/HIBP 密钥，返回临时库连接（服务层直连）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_API_KEY", "")
    monkeypatch.setenv("AF_LLM_BASE_URL", "")
    monkeypatch.setenv("AF_LLM_MODEL", "")
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.delenv("AF_HIBP_API_KEY", raising=False)
    get_settings.cache_clear()
    dbmod.close_all()
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    get_settings.cache_clear()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """隔离数据目录 + TestClient 上下文（lifespan 生成 bootstrap key / 建表 / 种子库）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_API_KEY", "")
    monkeypatch.setenv("AF_LLM_BASE_URL", "")
    monkeypatch.setenv("AF_LLM_MODEL", "")
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.delenv("AF_HIBP_API_KEY", raising=False)
    get_settings.cache_clear()
    dbmod.close_all()

    with TestClient(app) as client:
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        yield client, key

    dbmod.close_all()
    get_settings.cache_clear()


def _auth(key: str) -> dict:
    return {"X-API-Key": key}


def _create_identity(client, key: str, name: str, **over) -> dict:
    """经 API 创建伪装，返回 identity dict。"""
    body = {
        "name": name,
        "persona_type": "受害者A",
        "fake_phone": "13800138000",
        "fake_wechat": "wx_canary_cover",
        "fake_email": "cover@example.com",
    }
    body.update(over)
    r = client.post("/api/v1/cover/identities", headers=_auth(key), json=body)
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["data"]["identity"]


def _gate_logs(conn, action: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM gate_logs WHERE action=?", (action,))]


# ===========================================================================
# 创建（layer 自动 / name 唯一 / parent 校验）
# ===========================================================================

def test_create_top_layer_defaults(api):
    client, key = api
    ident = _create_identity(client, key, "受害者A_001")
    assert ident["layer"] == 1
    assert ident["enabled"] == 1
    assert ident["persona_type"] == "受害者A"
    assert ident["parent_id"] is None
    assert ident["contact_count"] == 0
    assert ident["fake_phone"] == "13800138000"


def test_create_child_layer_parent_plus_1(api):
    client, key = api
    a = _create_identity(client, key, "受害者A_root")
    b = _create_identity(client, key, "转介绍B_child", parent_id=a["id"])
    assert b["layer"] == a["layer"] + 1 == 2
    c = _create_identity(client, key, "朋友C_grand", parent_id=b["id"])
    assert c["layer"] == 3
    assert c["parent_id"] == b["id"]


def test_create_duplicate_name_409(api):
    client, key = api
    _create_identity(client, key, "同名伪装")
    r = client.post("/api/v1/cover/identities", headers=_auth(key),
                    json={"name": "同名伪装", "fake_phone": "13800138001"})
    assert r.status_code == 409, (r.status_code, r.text)
    body = r.json()
    assert body["ok"] is False and body["error"]["code"] == "identity_name_exists"


def test_create_parent_not_found_404(api):
    client, key = api
    r = client.post("/api/v1/cover/identities", headers=_auth(key),
                    json={"name": "孤儿伪装", "parent_id": 999999})
    assert r.status_code == 404, (r.status_code, r.text)
    assert r.json()["error"]["code"] == "parent_not_found"


def test_create_invalid_persona_type_422(api):
    client, key = api
    r = client.post("/api/v1/cover/identities", headers=_auth(key),
                    json={"name": "非法类型", "persona_type": "路人D"})
    assert r.status_code == 422, (r.status_code, r.text)
    assert r.json()["error"]["code"] == "invalid_persona_type"


# ===========================================================================
# 列表（含被接触次数）
# ===========================================================================

def test_list_identities_with_contact_count(api):
    client, key = api
    ident = _create_identity(client, key, "列表测试伪装")
    r = client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": ident["id"], "contact_type": "phone_call",
        "contact_value": "13800138000",
    })
    assert r.status_code == 200, r.text
    r = client.get("/api/v1/cover/identities", headers=_auth(key))
    assert r.status_code == 200
    items = r.json()["data"]["identities"]
    assert isinstance(items, list) and items
    me = next(i for i in items if i["id"] == ident["id"])
    assert me["contact_count"] == 1
    assert me["enabled"] in (0, 1)


# ===========================================================================
# toggle / delete（confirm+reason+gate_logs）
# ===========================================================================

def test_toggle_requires_confirm_and_logs(api, env):
    client, key = api
    ident = _create_identity(client, key, "启停测试伪装")
    url = f"/api/v1/cover/identities/{ident['id']}/toggle"

    r0 = client.post(url, headers=_auth(key))  # 无 body
    assert r0.status_code == 403 and r0.json()["error"]["code"] == "confirm_required"
    r1 = client.post(url, headers=_auth(key), json={"confirm": False, "reason": GOOD_REASON})
    assert r1.status_code == 403
    r2 = client.post(url, headers=_auth(key), json={"confirm": True, "reason": "太短"})
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "reason_too_short"

    r3 = client.post(url, headers=_auth(key),
                     json={"confirm": True, "reason": GOOD_REASON})
    assert r3.status_code == 200, r3.text
    assert r3.json()["data"]["identity"]["enabled"] == 0
    r4 = client.post(url, headers=_auth(key),
                     json={"confirm": True, "reason": GOOD_REASON})
    assert r4.json()["data"]["identity"]["enabled"] == 1

    logs = _gate_logs(env, "cover.identity.toggle")
    assert len(logs) == 2, "每次成功 toggle 应写一条 gate_logs"
    assert logs[0]["before"].endswith('"enabled": 1}') or '"enabled": 1' in logs[0]["before"]
    assert '"enabled": 0' in logs[0]["after"]
    assert logs[0]["reason"] == GOOD_REASON


def test_delete_requires_confirm_and_removes(api, env):
    client, key = api
    ident = _create_identity(client, key, "删除测试伪装")
    url = f"/api/v1/cover/identities/{ident['id']}"

    r0 = client.request("delete", url, headers=_auth(key))
    assert r0.status_code == 403
    r1 = client.request("delete", url, headers=_auth(key),
                        json={"confirm": True, "reason": "太短"})
    assert r1.status_code == 422

    r2 = client.request("delete", url, headers=_auth(key),
                        json={"confirm": True, "reason": GOOD_REASON})
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["deleted"] is True

    items = client.get("/api/v1/cover/identities", headers=_auth(key)
                       ).json()["data"]["identities"]
    assert all(i["id"] != ident["id"] for i in items), "删除后列表不应再含该伪装"

    r3 = client.request("delete", url, headers=_auth(key),
                        json={"confirm": True, "reason": GOOD_REASON})
    assert r3.status_code == 404

    logs = _gate_logs(env, "cover.identity.delete")
    assert len(logs) == 1
    assert logs[0]["after"] == '{"deleted": true}'


# ===========================================================================
# 主动暴露（脱敏清单 + 事件）
# ===========================================================================

def test_expose_returns_masked_checklist(api, env):
    client, key = api
    ident = _create_identity(client, key, "暴露测试伪装")
    r = client.post(f"/api/v1/cover/identities/{ident['id']}/expose",
                    headers=_auth(key))
    assert r.status_code == 200, r.text
    exposure = r.json()["data"]["exposure"]
    assert exposure["identity_id"] == ident["id"]
    assert exposure["event"] == "cover_expose"
    checklist = {c["field"]: c for c in exposure["checklist"]}
    assert "fake_phone" in checklist
    assert "*" in checklist["fake_phone"]["masked"], "假手机号应脱敏展示"
    assert checklist["fake_phone"]["masked"] == "13*******00"
    assert checklist["fake_phone"]["value"] == "13800138000"  # 用户自有配置原文回显

    rows = env.execute("SELECT payload FROM events WHERE kind='cover_expose'").fetchall()
    assert rows, "暴露应写 events(kind='cover_expose')"
    payload = json.loads(rows[0]["payload"])
    assert payload["identity_id"] == ident["id"]
    assert payload["layer"] == 1


# ===========================================================================
# 接触登记（落库+脱敏+IOC+锁定）
# ===========================================================================

def test_register_contact_masked_and_layer_at(api, env):
    client, key = api
    ident = _create_identity(client, key, "接触落库伪装")
    r = client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": ident["id"], "contact_type": "phone_call",
        "contact_value": "13800138000",
    })
    assert r.status_code == 200, r.text
    event = r.json()["data"]["event"]
    assert event["event_id"] > 0
    assert event["layer_at"] == 1
    assert event["contact_value_mask"] == "138****8000", "骗子号码应脱敏落库"

    row = env.execute(
        "SELECT * FROM contact_events WHERE id=?", (event["event_id"],)).fetchone()
    assert row is not None
    assert row["contact_value"] == "138****8000"
    assert row["identity_id"] == ident["id"]
    assert row["contact_type"] == "phone_call"
    assert row["layer_at"] == 1
    assert row["escalated"] == 0


def test_register_contact_extracts_ioc_event(api, env):
    client, key = api
    ident = _create_identity(client, key, "IOC提取伪装")
    r = client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": ident["id"], "contact_type": "phone_call",
        "contact_value": "骗子来电 13800138000 让我加 QQ 88888888",
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["event"]["ioc_count"] >= 2

    rows = env.execute(
        "SELECT payload FROM events WHERE kind='cover_contact'").fetchall()
    assert rows, "IOC 命中应写 events(kind='cover_contact')"
    payload = json.loads(rows[0]["payload"])
    assert payload["identity_id"] == ident["id"]
    assert payload["layer"] == 1
    masks = {i["ioc_type"]: i["mask"] for i in payload["iocs"]}
    assert masks.get("phones") == "13*******00"
    assert masks.get("qq") is not None


def test_register_contact_twice_locks(api, env):
    client, key = api
    ident = _create_identity(client, key, "锁定测试伪装")
    body = {"identity_id": ident["id"], "contact_type": "phone_call",
            "contact_value": "13900139000"}
    r1 = client.post("/api/v1/cover/contact", headers=_auth(key), json=body)
    assert r1.status_code == 200
    assert r1.json()["data"]["event"]["escalated"] == 0

    r2 = client.post("/api/v1/cover/contact", headers=_auth(key), json=body)
    assert r2.status_code == 200
    assert r2.json()["data"]["event"]["escalated"] == 1, "同号第二次接触应锁定"

    rows = env.execute(
        "SELECT escalated FROM contact_events WHERE identity_id=?", (ident["id"],)
    ).fetchall()
    assert len(rows) == 2
    assert all(r["escalated"] == 1 for r in rows), "同号全部接触行 escalated=1"

    locked = env.execute(
        "SELECT payload FROM events WHERE kind='cover_locked'").fetchall()
    assert locked, "锁定应写 events(kind='cover_locked')"
    lp = json.loads(locked[0]["payload"])
    assert lp["identity_id"] == ident["id"]
    assert lp["count"] == 2
    assert lp["contact_value_mask"] == "139****9000"


def test_contacts_filters(api):
    client, key = api
    ident = _create_identity(client, key, "过滤测试伪装")
    c1 = {"identity_id": ident["id"], "contact_type": "phone_call",
          "contact_value": "13900139000"}
    client.post("/api/v1/cover/contact", headers=_auth(key), json=c1)
    client.post("/api/v1/cover/contact", headers=_auth(key), json=c1)  # 第二次 → 锁定
    client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": ident["id"], "contact_type": "wechat_add",
        "contact_value": "wx_scammer_abc",
    })

    all_ = client.get("/api/v1/cover/contacts", headers=_auth(key)).json()["data"]["contacts"]
    assert len(all_) == 3
    by_id = client.get("/api/v1/cover/contacts", headers=_auth(key),
                       params={"identity_id": ident["id"]}).json()["data"]["contacts"]
    assert len(by_id) == 3
    by_type = client.get("/api/v1/cover/contacts", headers=_auth(key),
                         params={"contact_type": "phone_call"}).json()["data"]["contacts"]
    assert len(by_type) == 2
    locked = client.get("/api/v1/cover/contacts", headers=_auth(key),
                        params={"escalated": 1}).json()["data"]["contacts"]
    assert len(locked) == 2
    assert all(c["escalated"] == 1 for c in locked)
    assert all(c["identity_name"] == "过滤测试伪装" for c in by_id)


def test_cover_map_nested_layers(api):
    client, key = api
    a = _create_identity(client, key, "地图根伪装")
    b = _create_identity(client, key, "地图子伪装", parent_id=a["id"])
    client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": b["id"], "contact_type": "phone_call",
        "contact_value": "13700137000",
    })
    r = client.get("/api/v1/cover/map", headers=_auth(key))
    assert r.status_code == 200
    m = r.json()["data"]
    assert m["total_contacts"] == 1
    layers = {l["layer"]: l["contacts"] for l in m["layers"]}
    assert layers.get(2) == 1, "第 2 层被接触 1 次"
    roots = [t for t in m["tree"] if t["id"] == a["id"]]
    assert roots and roots[0]["layer"] == 1
    child_ids = [c["id"] for c in roots[0]["children"]]
    assert b["id"] in child_ids
    by_id = {i["id"]: i for i in m["identities"]}
    assert by_id[b["id"]]["contact_count"] == 1


# ===========================================================================
# 认证 / 校验 / 表迁移
# ===========================================================================

def test_cover_endpoints_require_admin_401(api):
    client, _ = api
    r1 = client.post("/api/v1/cover/identities", json={"name": "无key伪装"})
    assert r1.status_code == 401
    r2 = client.post("/api/v1/cover/contact", json={
        "identity_id": 1, "contact_type": "phone_call", "contact_value": "13800138000"})
    assert r2.status_code == 401
    r3 = client.get("/api/v1/cover/map")
    assert r3.status_code == 401
    r4 = client.get("/api/v1/cover/contacts")
    assert r4.status_code == 401


def test_register_contact_invalid_type_422(api):
    client, key = api
    ident = _create_identity(client, key, "非法接触类型伪装")
    r = client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": ident["id"], "contact_type": "sms",
        "contact_value": "13800138000",
    })
    assert r.status_code == 422, (r.status_code, r.text)
    assert r.json()["error"]["code"] == "invalid_contact_type"


def test_models_tables_24_and_stats_linkage(api, env):
    """表迁移：TABLES 22→24；cover 表已建；stats 联动计数。"""
    assert len(TABLE_NAMES) == 24
    assert "cover_identities" in TABLE_NAMES
    assert "contact_events" in TABLE_NAMES
    existing = {
        r["name"] for r in env.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    assert "cover_identities" in existing and "contact_events" in existing

    client, key = api
    ident = _create_identity(client, key, "看板联动伪装")
    client.post("/api/v1/cover/contact", headers=_auth(key), json={
        "identity_id": ident["id"], "contact_type": "email",
        "contact_value": "scammer@evil.invalid",
    })
    r = client.get("/api/v1/system/stats", headers=_auth(key))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["cover_identities"] >= 1
    assert data["contact_events"] >= 1
