"""拟真账号 + 社工库专项测试（P14）：PersonaSimService / SocLibService 与 /persona/*、/soc-lib/* API。

运行：cd /d 项目根 && .venv\\Scripts\\python.exe -m pytest tests/test_p14_persona_soc.py -q

覆盖（>=14 条真实运行）：
  1) generate_work 五类型（life/work/study/social/hobby）各返回非空 content 且 source=template
  2) 未知类型回退随机类型（兜底不崩）
  3) schedule 入库计划 next_run_at 非空
  4) publish_due 到期发布 + published=1 + next_run_at 推后
  5) 未到期不发布
  6) total 达上限不再发布
  7) pause 后 publish_due 跳过
  8) resume 后恢复发布
  9) API /persona/generate 无 key → 401
 10) API /persona/generate 有 key → 200 返回 post
 11) API /persona/schedule 有 key → 200 返回 schedule
 12) API /persona/publish-now → 200 返回 post
 13) API /persona/schedules/{id}/pause → 200 enabled=0
 14) API /persona/schedules/{id}/resume → 200 enabled=1
 15) API /persona/schedules 列表 → 200 含新建计划
 16) API /soc-lib/status → 200 含 hibp_configured / local_records
 17) API /soc-lib/query email 命中演示泄露库（found:true source:local）
 18) API /soc-lib/query 未知 ioc → found:false
 19) API /soc-lib/query 手机号（ioc_type/ioc_value）→ found:true source:local
 20) API /soc-lib/analyze/{det_id} 含手机号文本 → analyzed + hits + events(kind='soc_lib_hit')
 21) API /soc-lib/analyze 不存在的检测 → 404
 22) extract_iocs 提取手机/邮箱/QQ 正确（去重保序）
 23) API /persona/schedule interval_minutes<=0 / total 越界 / account|type 空串 → 422 validation_error
 24) API /persona/publish-now 达 total 上限后 → 409 persona_limit_reached（不再超发）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.main import app
from app.services.persona_sim import PERSONA_TEMPLATES, PersonaSimService
from app.services.soc_lib import SocLibService
from app.utils import ApiError

_FMT = "%Y-%m-%d %H:%M:%S"
PERSONA_TYPES = ("life", "work", "study", "social", "hobby")


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


# ===========================================================================
# 服务层：generate_work
# ===========================================================================

@pytest.mark.parametrize("ptype", PERSONA_TYPES, ids=lambda t: f"type_{t}")
def test_generate_work_all_types(ptype):
    work = PersonaSimService().generate_work(ptype)
    assert work["type"] == ptype
    assert isinstance(work["content"], str) and work["content"]
    assert work["source"] == "template"
    assert "{" not in work["content"], "模板占位符应全部被变量填充"


def test_generate_work_unknown_type_falls_back():
    work = PersonaSimService().generate_work("bogus")
    assert work["type"] in PERSONA_TEMPLATES
    assert work["content"] and work["source"] == "template"


# ===========================================================================
# 服务层：计划 / 到期发布 / 启停
# ===========================================================================

def test_schedule_inserts_next_run_at(env):
    s = PersonaSimService().schedule("acct_plan", "life", 30, 5, env)
    assert s["id"] > 0
    assert s["published"] == 0 and s["enabled"] == 1
    assert s["next_run_at"]
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (s["id"],)
    ).fetchone()
    assert row["next_run_at"] == s["next_run_at"]
    assert row["account"] == "acct_plan" and row["type"] == "life"


def test_publish_due_publishes_and_advances(env):
    svc = PersonaSimService()
    s = svc.schedule("acct_due", "work", 30, 5, env)
    old_next = s["next_run_at"]
    assert svc.publish_due(env) == 1
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (s["id"],)
    ).fetchone()
    assert row["published"] == 1
    assert row["next_run_at"] > old_next, "next_run_at 应推后一个 interval"
    post = env.execute(
        "SELECT * FROM persona_posts WHERE account='acct_due'"
    ).fetchone()
    assert post is not None
    assert post["content"] and post["source"] == "template"
    assert post["type"] == "work"


def test_publish_due_skips_not_yet_due(env):
    future = (datetime.now() + timedelta(minutes=30)).strftime(_FMT)
    cur = env.execute(
        "INSERT INTO persona_schedules "
        "(account,type,interval_minutes,total,published,next_run_at,enabled) "
        "VALUES (?,?,?,?,0,?,1)",
        ("acct_future", "life", 60, 5, future),
    )
    env.commit()
    assert PersonaSimService().publish_due(env) == 0
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    assert row["published"] == 0
    n = env.execute(
        "SELECT COUNT(*) AS c FROM persona_posts WHERE account='acct_future'"
    ).fetchone()["c"]
    assert n == 0


def test_publish_due_stops_at_total(env):
    svc = PersonaSimService()
    s = svc.schedule("acct_cap", "hobby", 0, 1, env)  # interval=0 → 始终到期
    assert svc.publish_due(env) == 1
    assert svc.publish_due(env) == 0, "published 达 total 上限后不再发布"
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (s["id"],)
    ).fetchone()
    assert row["published"] == 1
    n = env.execute(
        "SELECT COUNT(*) AS c FROM persona_posts WHERE account='acct_cap'"
    ).fetchone()["c"]
    assert n == 1


def test_publish_now_stops_at_total(env):
    svc = PersonaSimService()
    s = svc.schedule("acct_now_cap", "life", 0, 1, env)
    post = svc.publish_now(s["id"], env)
    assert post["published"] == 1
    with pytest.raises(ApiError) as ei:
        svc.publish_now(s["id"], env)
    assert ei.value.code == "persona_limit_reached"
    assert ei.value.status_code == 409
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (s["id"],)
    ).fetchone()
    assert row["published"] == 1, "达上限后 published 不应再增长"
    n = env.execute(
        "SELECT COUNT(*) AS c FROM persona_posts WHERE account='acct_now_cap'"
    ).fetchone()["c"]
    assert n == 1, "达上限后不应再插入新动态"


def test_pause_skips_publish_due(env):
    svc = PersonaSimService()
    s = svc.schedule("acct_pause", "study", 0, 5, env)
    svc.set_enabled(s["id"], 0, env)
    assert svc.publish_due(env) == 0
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (s["id"],)
    ).fetchone()
    assert row["enabled"] == 0 and row["published"] == 0


def test_resume_continues(env):
    svc = PersonaSimService()
    s = svc.schedule("acct_resume", "social", 0, 5, env)
    svc.set_enabled(s["id"], 0, env)
    assert svc.publish_due(env) == 0
    svc.set_enabled(s["id"], 1, env)
    assert svc.publish_due(env) == 1
    row = env.execute(
        "SELECT * FROM persona_schedules WHERE id=?", (s["id"],)
    ).fetchone()
    assert row["enabled"] == 1 and row["published"] == 1


# ===========================================================================
# API：/api/v1/persona/*
# ===========================================================================

def _create_schedule(client, key, account="api_acct", ptype="life",
                     interval_minutes=60, total=5) -> dict:
    r = client.post(
        "/api/v1/persona/schedule",
        headers={"X-API-Key": key},
        json={
            "account": account,
            "type": ptype,
            "interval_minutes": interval_minutes,
            "total": total,
        },
    )
    assert r.status_code == 200
    return r.json()["data"]["schedule"]


def test_persona_generate_requires_auth(api):
    client, _ = api
    r = client.post("/api/v1/persona/generate", json={"type": "life"})
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_persona_generate_with_key(api):
    client, key = api
    r = client.post(
        "/api/v1/persona/generate",
        json={"type": "life"},
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200
    post = r.json()["data"]["post"]
    assert post["type"] == "life"
    assert post["content"] and post["source"] == "template"


def test_persona_schedule_with_key(api):
    client, key = api
    sched = _create_schedule(client, key, account="api_acct_sch")
    assert sched["id"] > 0
    assert sched["next_run_at"]
    assert sched["published"] == 0 and sched["enabled"] == 1


def test_persona_publish_now(api):
    client, key = api
    sched = _create_schedule(client, key, account="api_acct_now")
    r = client.post(
        "/api/v1/persona/publish-now",
        headers={"X-API-Key": key},
        json={"schedule_id": sched["id"]},
    )
    assert r.status_code == 200
    post = r.json()["data"]["post"]
    assert post["post_id"] > 0
    assert post["content"] and post["source"] == "template"
    assert post["published"] == 1
    assert post["next_run_at"] > post["published_at"]


def test_persona_schedule_bounds_422(api):
    client, key = api
    bad_cases = [
        {"interval_minutes": 0},
        {"interval_minutes": -5},
        {"interval_minutes": 10081},
        {"total": 0},
        {"total": -1},
        {"total": 1001},
        {"account": ""},
        {"type": ""},
    ]
    for bad in bad_cases:
        payload = {"account": "api_bounds", "type": "life",
                   "interval_minutes": 60, "total": 5}
        payload.update(bad)
        r = client.post(
            "/api/v1/persona/schedule",
            headers={"X-API-Key": key},
            json=payload,
        )
        assert r.status_code == 422, (bad, r.status_code, r.text)
        body = r.json()
        assert body["ok"] is False
        assert body["error"]["code"] == "validation_error"


def test_persona_publish_now_limit_409(api):
    client, key = api
    sched = _create_schedule(client, key, account="api_acct_cap", total=1)
    r1 = client.post(
        "/api/v1/persona/publish-now",
        headers={"X-API-Key": key},
        json={"schedule_id": sched["id"]},
    )
    assert r1.status_code == 200
    assert r1.json()["data"]["post"]["published"] == 1
    r2 = client.post(
        "/api/v1/persona/publish-now",
        headers={"X-API-Key": key},
        json={"schedule_id": sched["id"]},
    )
    assert r2.status_code == 409, (r2.status_code, r2.text)
    body = r2.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "persona_limit_reached"
    posts = client.get(
        "/api/v1/persona/posts?account=api_acct_cap",
        headers={"X-API-Key": key},
    ).json()["data"]["posts"]
    assert len(posts) == 1, "达上限后不应再插入动态"


def test_persona_pause_endpoint(api):
    client, key = api
    sched = _create_schedule(client, key)
    r = client.post(
        f"/api/v1/persona/schedules/{sched['id']}/pause",
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200
    assert r.json()["data"]["schedule"]["enabled"] == 0


def test_persona_resume_endpoint(api):
    client, key = api
    sched = _create_schedule(client, key)
    client.post(
        f"/api/v1/persona/schedules/{sched['id']}/pause",
        headers={"X-API-Key": key},
    )
    r = client.post(
        f"/api/v1/persona/schedules/{sched['id']}/resume",
        headers={"X-API-Key": key},
    )
    assert r.status_code == 200
    assert r.json()["data"]["schedule"]["enabled"] == 1


def test_persona_schedules_list(api):
    client, key = api
    sid = _create_schedule(client, key, account="api_acct_list")["id"]
    r = client.get("/api/v1/persona/schedules", headers={"X-API-Key": key})
    assert r.status_code == 200
    scheds = r.json()["data"]["schedules"]
    assert isinstance(scheds, list) and scheds
    assert any(s["id"] == sid for s in scheds)


# ===========================================================================
# API：/api/v1/soc-lib/*
# ===========================================================================

def test_soc_lib_status(api):
    client, key = api
    r = client.get("/api/v1/soc-lib/status", headers={"X-API-Key": key})
    assert r.status_code == 200
    status = r.json()["data"]["status"]
    assert "hibp_configured" in status and "local_records" in status
    assert isinstance(status["hibp_configured"], bool)
    assert status["local_records"] >= 3, "演示泄露库种子应 >= 3 条"


def test_soc_lib_query_email_hit(api):
    client, key = api
    r = client.post(
        "/api/v1/soc-lib/query",
        headers={"X-API-Key": key},
        json={"email": "victim@example.com"},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["found"] is True and result["source"] == "local"
    assert result["breach_name"]


def test_soc_lib_query_unknown_miss(api):
    client, key = api
    r = client.post(
        "/api/v1/soc-lib/query",
        headers={"X-API-Key": key},
        json={"email": "unknown-user-xyz@nowhere.invalid"},
    )
    assert r.status_code == 200
    assert r.json()["data"]["result"]["found"] is False


def test_soc_lib_query_phone_ioc_hit(api):
    client, key = api
    r = client.post(
        "/api/v1/soc-lib/query",
        headers={"X-API-Key": key},
        json={"ioc_type": "phone", "ioc_value": "13800138000"},
    )
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["found"] is True and result["source"] == "local"


def test_soc_lib_analyze_phone_hit(api):
    client, key = api
    conn = dbmod.get_conn()
    conn.execute(
        "INSERT INTO detections (source, text_hash, content) VALUES ('import', ?, ?)",
        ("p14_hash_phone", "对方要求我拨打 13800138000 找客服退款，说账户异常"),
    )
    conn.commit()
    det_id = conn.execute(
        "SELECT id FROM detections ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]

    r = client.post(f"/api/v1/soc-lib/analyze/{det_id}", headers={"X-API-Key": key})
    assert r.status_code == 200
    result = r.json()["data"]["result"]
    assert result["analyzed"] >= 1
    assert isinstance(result["hits"], list) and result["hits"]
    phone_hit = next(h for h in result["hits"] if h["ioc_type"] == "phones")
    assert phone_hit["found"] is True and phone_hit["source"] == "local"
    assert phone_hit["ioc_value_mask"] == "13*******00"

    rows = conn.execute(
        "SELECT payload FROM events WHERE kind='soc_lib_hit'"
    ).fetchall()
    phone_events = [
        json.loads(r["payload"])
        for r in rows
        if json.loads(r["payload"]).get("ioc_type") == "phones"
    ]
    assert phone_events, "命中应写入 events(kind='soc_lib_hit') 且含 phones 命中"
    assert phone_events[0]["ioc_value_mask"] == "13*******00"


def test_soc_lib_analyze_detection_missing_404(api):
    client, key = api
    r = client.post("/api/v1/soc-lib/analyze/999999", headers={"X-API-Key": key})
    assert r.status_code == 404
    assert r.json()["ok"] is False


# ===========================================================================
# 服务层：IOC 提取
# ===========================================================================

def test_extract_iocs_phone_email_qq():
    iocs = SocLibService().extract_iocs(
        "联系我：手机 13800138000，邮箱 victim@example.com，QQ 88888"
    )
    assert iocs["phones"] == ["13800138000"]
    assert iocs["emails"] == ["victim@example.com"]
    assert "88888" in iocs["qq"]
    assert isinstance(iocs["wechat"], list) and isinstance(iocs["bank_cards"], list)


def test_extract_iocs_dedup_keep_order():
    """"重复 IOC 去重且保序（dedup keep order）。"""
    iocs = SocLibService().extract_iocs(
        "13800138000 与 13800138000 重复；victim@example.com"
    )
    assert iocs["phones"] == ["13800138000"]
    assert iocs["emails"] == ["victim@example.com"]