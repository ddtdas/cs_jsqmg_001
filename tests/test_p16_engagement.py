"""实战化改造专项测试（P16）：persona_replies 应对规则 CRUD / respond 匹配引擎 /
实战演练剧本（drills / drill）/ 对应 API 端点。

运行：cd /d 项目根 && .venv\\Scripts\\python.exe -m pytest tests/test_p16_engagement.py -q

覆盖（>=12 条真实运行）：
  1) add_reply 入库 → {reply_id} + DB 行可见（enabled=1）
  2) list_replies 返回全部规则
  3) delete_reply → deleted True，且列表不再含该规则
  4) set_reply_enabled 启停切换（enabled 0<->1）
  5) respond 命中（trigger 含'投资'）→ matched=true + reply 非空 + iocs 提取手机号
  6) respond 未命中 → matched=false + reply='' + reasoning='未命中应对规则'（不崩）
  7) respond 停用规则不命中（enabled=0 跳过）
  8) drills 返回 4 剧本结构（scenario/description/typical_incoming/suggested_reply/warning/iocs_hint）
  9) drill(scenario) 返回 suggested_reply/warning/reasoning
 10) drill 未知场景 → ValueError
 11) API /persona/respond 无 key → 401
 12) API /persona/respond 有 key → 200 matched
 13) API /persona/drills → 200 含 4 剧本
 14) API /persona/drill 已知场景 → 200 含 suggested_reply/warning
 15) API /persona/replies POST/GET/DELETE/toggle → 200 系列
 16) 表数 20：TABLE_NAMES 含 persona_replies 且 sqlite_master 实际建表
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app
from app.models import TABLE_NAMES
from app.services.persona_sim import REPLY_TEMPLATES, DRILL_SCRIPTS, PersonaSimService
from app.services.soc_lib import SocLibService


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据目录 + 空 LLM 密钥，返回临时库连接（服务层直连）。"""
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


def _add_reply(env, account="trap_acct", keyword="投资", rtype="cautious_prober",
               content="这个收益靠谱吗？能先发我公司资质核实一下吗？") -> dict:
    return PersonaSimService().add_reply(account, keyword, rtype, content, env)


# ===========================================================================
# 服务层：应对规则 CRUD
# ===========================================================================

def test_add_reply_inserts_row(env):
    result = _add_reply(env, account="trap_acct", keyword="投资", rtype="cautious_prober")
    rid = result["reply_id"]
    assert rid > 0
    row = env.execute("SELECT * FROM persona_replies WHERE id=?", (rid,)).fetchone()
    assert row["account"] == "trap_acct"
    assert row["trigger_keyword"] == "投资"
    assert row["reply_type"] == "cautious_prober"
    assert row["enabled"] == 1
    assert row["content"]


def test_list_replies_returns_all(env):
    _add_reply(env, keyword="投资")
    _add_reply(env, keyword="刷单")
    replies = PersonaSimService().list_replies(env)
    assert len(replies) == 2
    assert {r["trigger_keyword"] for r in replies} == {"投资", "刷单"}


def test_delete_reply_removes_row(env):
    rid = _add_reply(env)["reply_id"]
    deleted = PersonaSimService().delete_reply(rid, env)
    assert deleted is True
    replies = PersonaSimService().list_replies(env)
    assert all(r["id"] != rid for r in replies)


def test_delete_reply_missing_returns_false(env):
    assert PersonaSimService().delete_reply(999999, env) is False


def test_set_reply_enabled_toggles(env):
    rid = _add_reply(env)["reply_id"]
    svc = PersonaSimService()
    row = svc.set_reply_enabled(rid, 0, env)
    assert row["enabled"] == 0
    row = svc.set_reply_enabled(rid, 1, env)
    assert row["enabled"] == 1


# ===========================================================================
# 服务层：respond 匹配引擎
# ===========================================================================

def test_respond_hit_with_iocs(env):
    _add_reply(env, account="trap_acct", keyword="投资", rtype="cautious_prober")
    result = PersonaSimService().respond(
        "trap_acct",
        "老师带你投资稳赚不赔，加我微信 vx_trader01，或联系 13800138000",
        env,
    )
    assert result["matched"] is True
    assert result["reply"]
    assert result["reasoning"]
    assert result["extracted_iocs"]["phones"] == ["13800138000"]
    assert any("vx_trader01" in w for w in result["extracted_iocs"]["wechat"])


def test_respond_miss_defensive(env):
    _add_reply(env, account="trap_acct", keyword="投资")
    result = PersonaSimService().respond("trap_acct", "今天天气不错，出来喝杯茶？", env)
    assert result["matched"] is False
    assert result["reply"] == ""
    assert result["reasoning"] == "未命中应对规则"
    assert isinstance(result["extracted_iocs"], dict)
    assert result["extracted_iocs"]["phones"] == []


def test_respond_disabled_rule_skipped(env):
    rid = _add_reply(env, account="trap_acct", keyword="投资")["reply_id"]
    PersonaSimService().set_reply_enabled(rid, 0, env)
    result = PersonaSimService().respond("trap_acct", "带你投资稳赚不赔", env)
    assert result["matched"] is False, "停用规则不应命中"


# ===========================================================================
# 服务层：实战演练剧本
# ===========================================================================

_DRILL_KEYS = {"scenario", "description", "typical_incoming",
               "suggested_reply", "warning", "iocs_hint"}


def test_drills_returns_four_scripts():
    drills = PersonaSimService().drills()["drills"]
    assert len(drills) == 4
    scenarios = {d["scenario"] for d in drills}
    assert scenarios == {"杀猪盘", "刷单", "冒充公检法", "投资理财"}
    for d in drills:
        assert _DRILL_KEYS <= set(d.keys())
        assert d["suggested_reply"] and d["warning"] and d["typical_incoming"]


def test_drill_returns_suggested_reply_and_warning():
    result = PersonaSimService().drill("杀猪盘")
    assert result["scenario"] == "杀猪盘"
    assert result["suggested_reply"]
    assert result["warning"]
    assert result["reasoning"]


def test_drill_unknown_scenario_raises():
    with pytest.raises(ValueError):
        PersonaSimService().drill("不存在的剧本")


def test_drill_with_incoming_extracts_iocs():
    result = PersonaSimService().drill("刷单", "加微信 vx_job01 领任务，返利打到 13800138000")
    assert result["extracted_iocs"]["phones"] == ["13800138000"]
    assert any("vx_job01" in w for w in result["extracted_iocs"]["wechat"])


def test_reply_templates_three_roles():
    assert set(REPLY_TEMPLATES.keys()) == {"confused_elder", "curious_newbie", "cautious_prober"}
    for pool in REPLY_TEMPLATES.values():
        assert len(pool) >= 3, "每个角色模板池应 >= 3 条"


def test_drill_scripts_defined_static():
    assert len(DRILL_SCRIPTS) == 4


# ===========================================================================
# API：/api/v1/persona/*
# ===========================================================================

def test_persona_respond_requires_auth(api):
    client, _ = api
    r = client.post(
        "/api/v1/persona/respond",
        json={"account": "a", "incoming_text": "投资"},
    )
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_persona_respond_with_key(api):
    client, key = api
    client.post(
        "/api/v1/persona/replies",
        headers={"X-API-Key": key},
        json={"account": "api_acct", "trigger_keyword": "投资",
              "reply_type": "cautious_prober", "content": "这个收益靠谱吗？"},
    )
    r = client.post(
        "/api/v1/persona/respond",
        headers={"X-API-Key": key},
        json={"account": "api_acct", "incoming_text": "带你投资稳赚不赔 13800138000"},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["matched"] is True
    assert result["reply"]
    assert result["extracted_iocs"]["phones"] == ["13800138000"]


def test_persona_drills_endpoint(api):
    client, key = api
    r = client.get("/api/v1/persona/drills", headers={"X-API-Key": key})
    assert r.status_code == 200
    drills = r.json()["data"]["drills"]
    assert len(drills) == 4
    assert all(d["suggested_reply"] and d["warning"] for d in drills)


def test_persona_drill_endpoint(api):
    client, key = api
    r = client.post(
        "/api/v1/persona/drill",
        headers={"X-API-Key": key},
        json={"scenario": "投资理财", "incoming_text": "加我微信 vx_fund 领体验金"},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["suggested_reply"] and result["warning"]
    assert any("vx_fund" in w for w in result["extracted_iocs"]["wechat"])


def test_persona_drill_unknown_404(api):
    client, key = api
    r = client.post(
        "/api/v1/persona/drill",
        headers={"X-API-Key": key},
        json={"scenario": "不存在的剧本"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "drill_scenario_unknown"


def test_persona_replies_api_crud(api):
    client, key = api
    # POST 新建
    r = client.post(
        "/api/v1/persona/replies",
        headers={"X-API-Key": key},
        json={"account": "api_acct", "trigger_keyword": "刷单",
              "reply_type": "curious_newbie", "content": "这个返佣怎么算的？"},
    )
    assert r.status_code == 200
    rid = r.json()["data"]["reply_id"]
    assert rid > 0
    # GET 列表
    r = client.get("/api/v1/persona/replies", headers={"X-API-Key": key})
    assert r.status_code == 200
    replies = r.json()["data"]["replies"]
    assert any(x["id"] == rid for x in replies)
    # toggle 停用
    r = client.post(f"/api/v1/persona/replies/{rid}/toggle", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["data"]["enabled"] == 0
    # toggle 恢复
    r = client.post(f"/api/v1/persona/replies/{rid}/toggle", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["data"]["enabled"] == 1
    # DELETE
    r = client.delete(f"/api/v1/persona/replies/{rid}", headers={"X-API-Key": key})
    assert r.status_code == 200
    assert r.json()["data"]["deleted"] is True
    # 重复删除 → 404
    r = client.delete(f"/api/v1/persona/replies/{rid}", headers={"X-API-Key": key})
    assert r.status_code == 404


def test_persona_replies_requires_auth(api):
    client, _ = api
    r = client.post("/api/v1/persona/replies", json={
        "account": "a", "trigger_keyword": "投资", "content": "x"})
    assert r.status_code == 401


# ===========================================================================
# 表结构：persona_replies 已建，表数 20 -> 22（+ knowledge_items/knowledge_events）
# ===========================================================================

def test_table_count_22(env):
    assert "persona_replies" in TABLE_NAMES
    assert "knowledge_items" in TABLE_NAMES
    assert len(TABLE_NAMES) == 24  # 22 基线 + cover_identities + contact_events（P19）
    row = env.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='persona_replies'"
    ).fetchone()
    assert row is not None, "persona_replies 应已实际建表"
