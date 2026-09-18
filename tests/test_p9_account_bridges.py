"""P9 账号桥接回归测试（配置账号页面后端：表迁移 / 配置加解密 / 连通测试 / 私信接入 / agent 一键导入）。

覆盖（PLAN-配置账号与私信接入.md 第三节 + DoD）：
  1) 平台矩阵：7 平台且 ai_ready 正确；douyin 不可配置（422/禁止保存）
  2) save_config：敏感字段 Fernet 加密落库（config_enc 不含明文，解密后含）；status=configured
  3) 重复保存幂等 upsert；未知平台 404；ai_ready=false 平台 422
  4) test：zhihu 伪 cookie 结构校验 ok=false 但不崩；telegram 无 token → 422
  5) ingest_dm：zhihu 已配置 → detections pending + events(dm_received) + 返回 detection_id；
     未配置 → 409；空 text → 422
  6) delete_config → status=unconfigured 且 config_enc 清空（凭据不可恢复）
  7) agent-import 结构含 mcp_streamable_http / env_vars / ingest_endpoint
  8) 敏感端点无 key 401；有 key 200
  9) save_config 落 gate_logs 审计（哈希链写入）

运行：pytest tests/test_p9_account_bridges.py -q（项目根）；全量 pytest -q 保持绿。
"""

from __future__ import annotations

import json

import pytest

from app import db as dbmod
from app.config import get_settings

# ---------------------------------------------------------------------------
# fixtures（对齐 test_r2_backend_fixes.py：临时 data 目录 + 独立 bootstrap key）
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    get_settings.cache_clear()


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """循环 TestClient（Host=127.0.0.1，满足 P1-A4 Host 白名单）。"""
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    monkeypatch.setenv("AF_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as c:
        yield c
    dbmod.close_all()
    get_settings.cache_clear()
    from app.middleware.rate_limit import reset_rate_limiters

    reset_rate_limiters()


def _admin_key(tmp_path) -> str:
    return (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()


# ===========================================================================
# 1) 平台矩阵：7 平台 + ai_ready 正确 + douyin 不可配置
# ===========================================================================


def test_matrix_returns_7_platforms_with_ai_ready(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.get("/api/v1/account-bridges", headers=auth)
    assert r.status_code == 200 and r.json()["ok"] is True
    rows = {p["platform"]: p for p in r.json()["data"]}
    assert set(rows) == {"zhihu", "wechat", "qq", "weibo", "douyin", "telegram", "discord"}
    for pid in ("zhihu", "wechat", "qq", "weibo", "telegram", "discord"):
        assert rows[pid]["ai_ready"] is True, pid
        assert rows[pid]["name"]
        assert rows[pid]["access"]
        assert rows[pid]["projects"]
        assert rows[pid]["status"] in ("unconfigured", "configured", "testing", "error")
    assert rows["douyin"]["ai_ready"] is False
    assert rows["douyin"]["projects"] == []
    # 矩阵不含明文/config_enc
    body = r.text
    assert "config_enc" not in body


def test_douyin_not_configurable_422(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.put("/api/v1/account-bridges/douyin/config",
                json={"fields": {"cookie": "x=1"}}, headers=auth)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "not_configurable"
    # douyin 也不可 test
    rt = api.post("/api/v1/account-bridges/douyin/test", headers=auth)
    assert rt.status_code == 422


# ===========================================================================
# 2) save_config：Fernet 加密落库（明文不落库、解密后含）
# ===========================================================================


def test_save_config_zhihu_encrypts_cookie(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    cookie = "d_c0=abc123; z_c0=xyz789; q_c1=foo"
    r = api.put("/api/v1/account-bridges/zhihu/config",
                json={"fields": {"cookie": cookie, "from_url_name": "sniper_x"}},
                headers=auth)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "configured"
    assert "cookie" in data["encrypted_fields"]
    assert data["has_config"] is True

    conn = dbmod.get_conn()
    row = conn.execute("SELECT * FROM account_bridges WHERE platform='zhihu'").fetchone()
    assert row is not None
    assert row["status"] == "configured"
    enc = row["config_enc"]
    assert cookie not in enc, "config_enc 密文不得包含明文 cookie"
    assert "abc123" not in enc

    # 解密后含原文：外层解密得 JSON（敏感字段为内层 Fernet token），内层再解密得明文 cookie
    from app.services.zhihu_bridge import CookieVault

    vault = CookieVault(get_settings())
    outer = json.loads(vault.decrypt(enc))
    assert set(outer) == {"cookie", "from_url_name"}
    assert outer["from_url_name"] == "sniper_x"          # 非敏感字段明文存于 JSON
    assert cookie not in outer["cookie"]                 # 内层仍是密文
    assert vault.decrypt(outer["cookie"]) == cookie      # 内层解密还原原文
    assert cookie.split(";")[0] in vault.decrypt(outer["cookie"])

    # 事件落库
    ev = conn.execute(
        "SELECT payload FROM events WHERE kind='account_bridge_configured' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev is not None and "zhihu" in ev["payload"]


def test_save_config_repeat_upsert_idempotent(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    for i in range(2):
        r = api.put("/api/v1/account-bridges/zhihu/config",
                    json={"fields": {"cookie": f"d_c0=key{i}; z_c0=zz"}}, headers=auth)
        assert r.status_code == 200
    conn = dbmod.get_conn()
    rows = conn.execute("SELECT * FROM account_bridges WHERE platform='zhihu'").fetchall()
    assert len(rows) == 1, "重复保存必须幂等 upsert（platform UNIQUE 仅一行）"
    from app.services.zhihu_bridge import CookieVault

    vault = CookieVault(get_settings())
    outer = json.loads(vault.decrypt(rows[0]["config_enc"]))
    assert "key1" in vault.decrypt(outer["cookie"]), "第二次保存应覆盖前值"


# ===========================================================================
# 3) 未知平台 404 / ai_ready=false 422 / 未知字段 422
# ===========================================================================


def test_unknown_platform_404(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.get("/api/v1/account-bridges/linkedin", headers=auth)
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_platform"
    r2 = api.put("/api/v1/account-bridges/linkedin/config",
                 json={"fields": {"cookie": "x=1"}}, headers=auth)
    assert r2.status_code == 404 and r2.json()["error"]["code"] == "unknown_platform"


def test_save_config_unknown_field_422(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.put("/api/v1/account-bridges/zhihu/config",
                json={"fields": {"cookie": "a=1", "hack_key": "evil"}}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_field"


def test_save_config_empty_fields_422(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.put("/api/v1/account-bridges/zhihu/config", json={"fields": {}}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_fields"


# ===========================================================================
# 4) test：zhihu 伪 cookie → ok=false 不崩；telegram 无 token → 422
# ===========================================================================


def test_test_zhihu_fake_cookie_ok_false_no_crash(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    api.put("/api/v1/account-bridges/zhihu/config",
            json={"fields": {"cookie": "this_is_not_a_real_cookie"}}, headers=auth)
    r = api.post("/api/v1/account-bridges/zhihu/test", headers=auth)
    assert r.status_code == 400, "结构校验失败应返回 400 + {ok:false,error} 封包（新契约，消除双层 ok）"
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "test_failed"
    assert "失败" in body["error"]["message"] or "结构校验失败" in body["error"]["message"]
    # 失败 → status='error'
    conn = dbmod.get_conn()
    row = conn.execute("SELECT status FROM account_bridges WHERE platform='zhihu'").fetchone()
    assert row["status"] == "error"


def test_test_telegram_no_token_422(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    api.put("/api/v1/account-bridges/telegram/config",
            json={"fields": {"bot_token": ""}}, headers=auth)
    r = api.post("/api/v1/account-bridges/telegram/test", headers=auth)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "field_required"


def test_test_not_configured_409(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.post("/api/v1/account-bridges/discord/test", headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_configured"


# ===========================================================================
# 5) ingest_dm：已配置 → pending + dm_received + detection_id；未配置 → 409
# ===========================================================================


def test_ingest_dm_zhihu_configured_pending_event(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    api.put("/api/v1/account-bridges/zhihu/config",
            json={"fields": {"cookie": "d_c0=ok; z_c0=ok"}}, headers=auth)

    r = api.post("/api/v1/account-bridges/zhihu/ingest",
                 json={"from": "fraudster_88", "text": "稳赚不赔的理财项目，导师带你内幕消息",
                       "dm_id": "zh-1001"},
                 headers=auth)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["platform"] == "zhihu"
    assert data["status"] == "pending"
    assert isinstance(data["detection_id"], int) and data["detection_id"] > 0
    assert data["from"] == "fraudster_88"

    conn = dbmod.get_conn()
    det = conn.execute(
        "SELECT * FROM detections WHERE id=?", (data["detection_id"],)
    ).fetchone()
    assert det is not None
    assert det["source"] == "zhihu"
    assert det["platform"] == "zhihu"          # platform 列注入平台值
    assert det["status"] == "pending"
    assert "理财" in det["content"]

    ev = conn.execute(
        "SELECT payload FROM events WHERE kind='dm_received' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(ev["payload"])
    assert payload["detection_id"] == data["detection_id"]
    assert payload["from"] == "fraudster_88"
    assert payload["platform"] == "zhihu"
    assert payload["dm_id"] == "zh-1001"

    # from 非空 → account_intel upsert（accounts 画像）
    acc = conn.execute("SELECT * FROM accounts WHERE url_name='fraudster_88'").fetchone()
    assert acc is not None and acc["risk_level"] in ("green", "yellow", "red")

    # 桥接行 last_sync 更新
    br = conn.execute("SELECT last_sync FROM account_bridges WHERE platform='zhihu'").fetchone()
    assert br["last_sync"]


def test_ingest_dm_not_configured_409(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.post("/api/v1/account-bridges/weibo/ingest",
                 json={"from": "u1", "text": "hello"}, headers=auth)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_configured"


def test_ingest_dm_empty_text_422(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    api.put("/api/v1/account-bridges/zhihu/config",
            json={"fields": {"cookie": "d_c0=ok"}}, headers=auth)
    r = api.post("/api/v1/account-bridges/zhihu/ingest",
                 json={"from": "u1", "text": "   "}, headers=auth)
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_text"


# ===========================================================================
# 6) delete_config → unconfigured + config_enc 清空
# ===========================================================================


def test_delete_config_clears_credentials(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    api.put("/api/v1/account-bridges/zhihu/config",
            json={"fields": {"cookie": "d_c0=secret_value"}}, headers=auth)
    r = api.request("DELETE", "/api/v1/account-bridges/zhihu",
                    json={"reason": "删除账号桥接配置"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "unconfigured"

    conn = dbmod.get_conn()
    row = conn.execute("SELECT * FROM account_bridges WHERE platform='zhihu'").fetchone()
    assert row["status"] == "unconfigured"
    assert not (row["config_enc"] or "").strip(), "delete 后 config_enc 必须清空"
    # 删除后 ingest → 409
    r2 = api.post("/api/v1/account-bridges/zhihu/ingest",
                  json={"from": "u1", "text": "x"}, headers=auth)
    assert r2.status_code == 409
    # 删除事件
    ev = conn.execute(
        "SELECT payload FROM events WHERE kind='account_bridge_deleted' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert ev is not None and "zhihu" in ev["payload"]


# ===========================================================================
# 7) agent-import 结构
# ===========================================================================


def test_agent_import_structure(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    r = api.get("/api/v1/account-bridges/wechat/agent-import", headers=auth)
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["platform"] == "wechat"
    mcp = d["mcp_streamable_http"]
    assert mcp["server_name"] == "canary-wechat"
    assert mcp["url"].startswith("http://")
    assert "Authorization" in mcp["headers"] and "AF_API_KEY" in mcp["headers"]["Authorization"]
    assert d["env_vars"] == ["AF_API_KEY"]
    assert d["ingest_endpoint"] == "/api/v1/account-bridges/wechat/ingest"
    assert "WeChatFerry" in d["bridge_script_hint"]

    rz = api.get("/api/v1/account-bridges/zhihu/agent-import", headers=auth)
    assert rz.status_code == 200
    assert rz.json()["data"]["ingest_endpoint"] == "/api/v1/account-bridges/zhihu/ingest"


# ===========================================================================
# 8) 敏感端点鉴权：无 key 401 / 有 key 200 + 详情
# ===========================================================================


def test_sensitive_endpoints_401_without_key(api):
    assert api.get("/api/v1/account-bridges").status_code == 401
    assert api.get("/api/v1/account-bridges/zhihu").status_code == 401
    assert api.put("/api/v1/account-bridges/zhihu/config",
                   json={"fields": {"cookie": "a=1"}}).status_code == 401
    assert api.post("/api/v1/account-bridges/zhihu/test").status_code == 401
    assert api.post("/api/v1/account-bridges/zhihu/ingest",
                    json={"from": "u", "text": "x"}).status_code == 401
    assert api.get("/api/v1/account-bridges/zhihu/agent-import").status_code == 401
    assert api.delete("/api/v1/account-bridges/zhihu").status_code == 401


def test_with_key_200_and_detail(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    assert api.get("/api/v1/account-bridges", headers=auth).status_code == 200
    r = api.get("/api/v1/account-bridges/zhihu", headers=auth)
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["platform"] == "zhihu"
    assert d["has_config"] is False
    names = [f["name"] for f in d["config_fields"]]
    assert names == ["cookie", "from_url_name"]
    assert "config_enc" not in r.text


# ===========================================================================
# 9) save_config 落 gate_logs 审计（哈希链写入）
# ===========================================================================


def test_save_config_writes_gate_log_audit(api, tmp_path):
    auth = {"X-API-Key": _admin_key(tmp_path)}
    api.put("/api/v1/account-bridges/zhihu/config",
            json={"fields": {"cookie": "d_c0=audit_test"},
                  "reason": "配置知乎账号桥接以接入私信"},
            headers=auth)
    conn = dbmod.get_conn()
    row = conn.execute(
        "SELECT * FROM gate_logs WHERE action='account_bridge.configure' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row["reason"] == "配置知乎账号桥接以接入私信"
    assert "zhihu" in row["payload_json"]
    after = json.loads(row["after"])
    assert after["status"] == "configured" and after["secrets"] == ["cookie"]
    # 哈希链仍完整
    from app.services.audit_log import verify_gate_chain

    assert verify_gate_chain(conn)["valid"] is True
