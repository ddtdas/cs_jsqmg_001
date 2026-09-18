"""P4 蜜饵引擎测试：状态机全链路 / 改写召回 / 伪装度可解释 / 非法迁移 / API。

运行：pytest tests/test_p4_traps.py -q（项目根）。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.trap_engine import TrapError, TrapEngine

BAIT = "你好呀，我在网上看到一个很不错的兼职项目，感觉超适合你，我们加个V聊聊怎么样"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据库（含 41 条种子词库）+ 重置 provider（no_key 降级模式）。"""
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


@pytest.fixture()
def engine() -> TrapEngine:
    return TrapEngine()


# ---- 状态机全链路：draft→deployed→monitored→hit→retired ----

@pytest.mark.asyncio
async def test_state_machine_full_chain(env, engine):
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = d["id"]
    assert d["status"] == "draft"
    assert re.match(r"^[0-9a-f-]{36}$", d["fingerprint"])

    active = engine.deploy(env, trap_id, target_url="https://www.zhihu.com/question/1")
    assert active["status"] == "active"
    assert active["deployed_at"]

    monitored = engine.monitor(env, trap_id)
    assert monitored["status"] == "monitored"

    r = await engine.check_hit(env, trap_id, BAIT)
    assert r["hit"] is True
    assert r["transition"] == "monitored→hit→retired"
    assert r["status"] == "retired"
    assert r["hit_count"] == 1

    final = engine.get(env, trap_id)
    assert final["status"] == "retired"
    assert final["retired_reason"] == "hit"
    assert final["hit_count"] == 1

    # 事件落库核对
    kinds = [row["kind"] for row in env.execute(
        "SELECT kind FROM events WHERE kind LIKE 'trap_%' ORDER BY id")]
    assert "trap_draft_created" in kinds
    assert "trap_deployed" in kinds
    assert "trap_monitored" in kinds
    assert "trap_hit" in kinds
    assert "trap_retired" in kinds


# ---- 改写 5/10 字 + 加标点仍召回 ----

@pytest.mark.asyncio
async def test_rewrite_variants_still_hit(env, engine):
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = d["id"]
    engine.deploy(env, trap_id)
    engine.monitor(env, trap_id)

    variants = [
        BAIT + "？好不好？",                                   # 加标点/语气词
        "你好呀，我在网上看到一个挺好的兼职项目，感觉蛮适合你，咱们加个V聊聊怎么样",  # 改 ~8 字
        "嗨，我最近在网上发现一个很不错的兼职项目，感觉超适合你，加个V聊聊呗",        # 改 ~10 字
        "喂？是某某吗？" + BAIT,                                 # 前缀拼接（子串命中）
    ]
    for v in variants:
        r = await engine.check_hit(env, trap_id, v)
        assert r["hit"] is True, f"改写后未召回: {v}"
        assert r["status"] == "retired"          # 命中即退役
        assert r["hit_count"] >= 1
        break  # 第一个命中即退役，后续无需再测同一 trap


@pytest.mark.asyncio
async def test_distinct_text_not_hit(env, engine):
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = d["id"]
    engine.deploy(env, trap_id)
    engine.monitor(env, trap_id)
    r = await engine.check_hit(env, trap_id, "今天天气很好我们去公园散步吧")
    assert r["hit"] is False
    assert r["status"] == "monitored"            # 未命中不改变状态
    assert r["similarity"]["matched_by"] == []
    assert r["evidence"] == []


# ---- 伪装度可解释 ----

def test_disguise_score_explainable(env, engine):
    d = engine.create_draft(env, bait_text=BAIT)
    assert 0.0 <= d["disguise_score"] <= 100.0
    assert isinstance(d["disguise_reason"], list) and d["disguise_reason"], "伪装度必须给出理由"
    assert d["disguise_method"] == "heuristic"
    # 明显诈骗味文案应显著低分
    scammy = engine.create_draft(env, bait_text="稳赚不赔高回报，导师带你内幕消息，转账到安全账户")
    assert scammy["disguise_score"] < d["disguise_score"]


# ---- 非法迁移被拒 ----

def test_illegal_transitions_rejected(env, engine):
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = d["id"]

    # draft → monitored 非法
    with pytest.raises(TrapError) as e1:
        engine.monitor(env, trap_id)
    assert e1.value.code == "invalid_transition"

    engine.deploy(env, trap_id)
    # active → hit 直接迁移非法（必须经 check_hit 踩饵检测）
    with pytest.raises(TrapError) as e1b:
        engine._transition({"status": "active"}, "hit")
    assert e1b.value.code == "invalid_transition"

    engine.retire(env, trap_id, reason="user")
    # retired 终态：deploy / monitor / retire 均被拒
    for fn in (engine.deploy, engine.monitor, engine.retire):
        with pytest.raises(TrapError) as e2:
            fn(env, trap_id)
        assert e2.value.code == "invalid_transition"

    # 不存在
    with pytest.raises(TrapError) as e3:
        engine.get(env, 99999)
    assert e3.value.status_code == 404


@pytest.mark.asyncio
async def test_check_hit_rejected_when_retired_or_disabled(env, engine):
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = d["id"]
    engine.deploy(env, trap_id)
    engine.retire(env, trap_id, reason="user")
    with pytest.raises(TrapError) as e1:
        await engine.check_hit(env, trap_id, BAIT)
    assert e1.value.code == "trap_retired"

    d2 = engine.create_draft(env, bait_text=BAIT)
    tid2 = d2["id"]
    engine.deploy(env, tid2)
    engine.disable(env, tid2)
    with pytest.raises(TrapError) as e2:
        await engine.check_hit(env, tid2, BAIT)
    assert e2.value.code == "trap_disabled"


# ---- HITL：generate-draft 永不自动发布 ----

@pytest.mark.asyncio
async def test_generate_draft_hitl_not_published(env, engine):
    d = await engine.generate_draft(env, template_id="t_parttime_job")
    assert d["status"] == "draft"                 # 生成即草稿
    assert d["generation"].startswith("template:")
    assert "hitl_hint" in d and "绝不自动发布" in d["hitl_hint"]
    assert d["fingerprint_note"]
    assert 0.0 <= d["disguise_score"] <= 100.0

    # 无 deploy 调用前，状态绝不能离开 draft
    row = engine.get(env, d["id"])
    assert row["status"] == "draft"
    assert row["deployed_at"] is None


# ---- API 集成：创建草稿→deploy→check-hit 命中→retired ----

def test_traps_api_full_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        # 写操作（创建/deploy/disable/generate-draft）需 admin（P1-1）；key 于 lifespan 启动时生成
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        auth = {"X-API-Key": key}

        # 创建草稿
        r = client.post("/api/v1/traps", json={"bait_text": BAIT, "note": "p4测试"}, headers=auth)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        trap_id = body["data"]["id"]
        assert body["data"]["status"] == "draft"
        assert "disguise_reason" in body["data"]

        # 列表（draft 过滤）—— 公开只读
        lst = client.get("/api/v1/traps", params={"status": "draft"}).json()["data"]
        assert any(t["id"] == trap_id for t in lst)

        # deploy
        r2 = client.post(f"/api/v1/traps/{trap_id}/deploy", json={"target_url": "https://x.test/a"}, headers=auth)
        assert r2.json()["data"]["status"] == "active"

        # check-hit 命中 → retired（P2-4：POST body 传文本）
        r3 = client.post(f"/api/v1/traps/{trap_id}/check-hit", json={"text": BAIT})
        assert r3.status_code == 200
        hit = r3.json()["data"]
        assert hit["hit"] is True
        assert hit["status"] == "retired"
        assert hit["hit_count"] == 1
        assert hit["evidence"]

        # retired 后再次 check-hit → 409
        r4 = client.post(f"/api/v1/traps/{trap_id}/check-hit", json={"text": BAIT})
        assert r4.status_code == 409
        assert r4.json()["ok"] is False
        assert r4.json()["error"]["code"] == "trap_retired"

        # 原 GET 语义：带状态迁移副作用 → 405（P2-4 回归）
        r405 = client.get(f"/api/v1/traps/{trap_id}/check-hit")
        assert r405.status_code == 405

        # generate-draft 端点（HITL）
        r5 = client.post("/api/v1/traps/generate-draft", json={"template_id": "t_invest_flow"}, headers=auth)
        assert r5.status_code == 200
        g = r5.json()["data"]
        assert g["status"] == "draft" and g["generation"].startswith("template:")

        # disable 流程
        tid6 = client.post("/api/v1/traps", json={"bait_text": BAIT}, headers=auth).json()["data"]["id"]
        client.post(f"/api/v1/traps/{tid6}/deploy", headers=auth)
        client.post(f"/api/v1/traps/{tid6}/disable", headers=auth)
        r6 = client.post(f"/api/v1/traps/{tid6}/check-hit", json={"text": BAIT})
        assert r6.status_code == 409 and r6.json()["error"]["code"] == "trap_disabled"

    dbmod.close_all()
    get_settings.cache_clear()
