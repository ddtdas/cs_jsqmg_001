"""P5 五级分级 + 账号速查 + L3+ 告警闸控测试。

覆盖 DoD：
  1) 各等级场景分级正确（L1/L2/L3/L4/L5）且 explain 返回结构化证据链
  2) L1/L2/L3 边界不越级（单条 9 分命中 → L2；多命中 → L3）
  3) L3+ 生成告警、≤L3 不生成（闸控）；告警去重与已读
  4) 账号速查只用公开信号（白名单），风险分级 + 证据链 + 账号簇共现
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.services.account_intel import AccountIntelService
from app.services.alerting import AlertService
from app.services.grading import GradingService


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)
    dbmod.ensure_schema()
    yield dbmod.get_conn()
    dbmod.close_all()
    llm_provider.set_provider(None)
    get_settings.cache_clear()


def _mk_det(conn, *, rule_score: float = 0.0, llm_verdict: str | None = None,
            judge_conf: float | None = None, hits: list[tuple[str, float]] | None = None) -> int:
    """构造一条检测记录 + 命中明细（pattern 名必须来自种子词库）。"""
    import hashlib

    text = "test-" + str(abs(hash(str((rule_score, llm_verdict, hits)))))
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, llm_verdict, judge_confidence, status) "
        "VALUES ('manual', ?, ?, ?, ?, ?, 'processed')",
        (hashlib.sha256(text.encode()).hexdigest(), text, rule_score, llm_verdict, judge_conf),
    )
    det_id = int(cur.lastrowid)
    for pattern, score in (hits or []):
        row = conn.execute(
            "SELECT id FROM speech_patterns WHERE pattern=? LIMIT 1", (pattern,)
        ).fetchone()
        assert row, f"种子词库缺少 pattern: {pattern}"
        conn.execute(
            "INSERT INTO speech_hits (det_id, pattern_id, matched_text, score) VALUES (?,?,?,?)",
            (det_id, row["id"], pattern, score),
        )
    conn.commit()
    return det_id


def _count_alerts(conn, level: str | None = None) -> int:
    if level:
        return conn.execute("SELECT COUNT(*) AS n FROM alerts WHERE level=?", (level,)).fetchone()["n"]
    return conn.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"]


# ---- 各等级分级 ----

def test_grade_l1_no_signal(env):
    det_id = _mk_det(env, rule_score=0.0)
    r = GradingService().grade_detection(env, det_id)
    assert r["grade"] == "L1"
    assert r["score"] == 0
    assert r["grade_reason"] == []
    # 闸控：L1 不生成告警
    AlertService().evaluate(env, detection_id=det_id, grade=r["grade"], grade_reason=r["grade_reason"])
    assert _count_alerts(env) == 0


def test_grade_l2_weak_signal(env):
    det_id = _mk_det(env, rule_score=5.0, hits=[("内测资格", 5.0)])
    r = GradingService().grade_detection(env, det_id)
    assert r["grade"] == "L2"
    assert r["score"] == 5.0
    AlertService().evaluate(env, detection_id=det_id, grade="L2", grade_reason=[])
    assert _count_alerts(env) == 0


def test_grade_l3_rules_only_capped(env):
    """多信号/高权重命中但无 LLM 确认 → L3（rule-only 封顶，不越级到 L4）。"""
    det_id = _mk_det(env, rule_score=25.0, hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0), ("导师带你", 8.0)])
    r = GradingService().grade_detection(env, det_id)
    assert r["grade"] == "L3"
    assert r["score"] == 25.0


def test_grade_l4_llm_fraud(env):
    det_id = _mk_det(env, rule_score=25.0, llm_verdict="fraud", judge_conf=0.7,
                     hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0), ("导师带你", 8.0)])
    r = GradingService().grade_detection(env, det_id)
    assert r["grade"] == "L4"
    assert any(s["signal"] == "llm_fraud" for s in r["grade_reason"])


def test_grade_l5_trap_hit_evidence(env):
    det_id = _mk_det(env, rule_score=9.0, hits=[("稳赚不赔", 9.0)])
    r = GradingService().grade_detection(env, det_id, trap_hit_count=2)
    assert r["grade"] == "L5"
    assert any(s["signal"] == "trap_hit" for s in r["grade_reason"])


def test_grade_l5_llm_fraud_high_conf_red_account(env):
    det_id = _mk_det(env, rule_score=17.0, llm_verdict="fraud", judge_conf=0.9,
                     hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0)])
    r = GradingService().grade_detection(env, det_id, account_risk="red")
    assert r["grade"] == "L5"
    # 无红信号时同输入只到 L4
    r2 = GradingService().grade_detection(env, det_id)
    assert r2["grade"] == "L4"


# ---- L1/L2/L3 边界不越级 ----

def test_grade_boundary_l2_not_l3(env):
    """单条高权重命中（9 分）→ L2，不得越级 L3。"""
    det_id = _mk_det(env, rule_score=9.0, hits=[("稳赚不赔", 9.0)])
    r = GradingService().grade_detection(env, det_id)
    assert r["grade"] == "L2"


def test_grade_boundary_l3_min(env):
    """两条命中（9+8=17）→ L3。"""
    det_id = _mk_det(env, rule_score=17.0, hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0)])
    r = GradingService().grade_detection(env, det_id)
    assert r["grade"] == "L3"


# ---- explain 证据链 ----

def test_explain_returns_evidence_chain(env):
    det_id = _mk_det(env, rule_score=17.0, llm_verdict="suspicious", judge_conf=0.85,
                     hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0)])
    r = GradingService().grade_detection(env, det_id)
    reason = r["grade_reason"]
    assert isinstance(reason, list) and len(reason) >= 3
    for item in reason:
        assert set(item) == {"signal", "weight", "note"}
    assert any(i["signal"] == "rule_hit" for i in reason)
    assert any(i["signal"] == "llm_suspicious" for i in reason)
    assert any(i["signal"] == "llm_confidence_high" for i in reason)


# ---- 告警闸控 ----

def test_alert_gate_l3_plus_only(env):
    svc = GradingService()
    l3 = _mk_det(env, rule_score=17.0, hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0)])
    l4 = _mk_det(env, rule_score=9.0, llm_verdict="fraud", judge_conf=0.6, hits=[("稳赚不赔", 9.0)])
    svc.finalize(env, detection_id=l3)   # L3 → 告警
    svc.finalize(env, detection_id=l4)   # L4 → 告警
    assert _count_alerts(env) == 2
    assert _count_alerts(env, level="L3") == 1
    assert _count_alerts(env, level="L4") == 1

    # 去重：再次 finalize 同一检测不重复告警
    svc.finalize(env, detection_id=l3)
    assert _count_alerts(env) == 2


def test_alert_dedupe_mark_read(env):
    det_id = _mk_det(env, rule_score=25.0, hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0)])
    a1 = AlertService().evaluate(env, detection_id=det_id, grade="L3", grade_reason=[])
    assert a1["alerted"] is True
    a2 = AlertService().evaluate(env, detection_id=det_id, grade="L3", grade_reason=[])
    assert a2.get("duplicate") is True

    alerts = AlertService().list_alerts(env, unread_only=True)
    assert len(alerts) == 1 and alerts[0]["unread"] == 1
    AlertService().mark_read(env, alerts[0]["id"])
    assert _count_alerts(env, ) == 1
    assert AlertService().list_alerts(env, unread_only=True) == []


def test_alert_dedupe_no_prefix_misjudge(env):
    """P1-2：去重必须精确匹配 detection_id —— det_id=5 已告警时，det_id=50 不得被误判 duplicate。

    旧实现 payload LIKE '%"detection_id": 5%' 会命中 '"detection_id": 50'（数字前缀误匹配），
    导致 det_id=50 的 L3+ 告警被静默丢弃；json_extract 精确提取后两者独立告警。
    """
    env.execute(
        "INSERT INTO alerts (rule_id, level, channel, payload) VALUES (NULL, 'L3', 'webui', ?)",
        (json.dumps({"detection_id": 5, "grade": "L3", "reason": []}, ensure_ascii=False),),
    )
    env.commit()

    # det_id=50：与 5 前缀冲突，必须能独立生成告警
    r50 = AlertService().evaluate(env, detection_id=50, grade="L3", grade_reason=[])
    assert r50["alerted"] is True and r50.get("duplicate") is not True, r50

    # det_id=5 本身仍被精确去重
    r5 = AlertService().evaluate(env, detection_id=5, grade="L3", grade_reason=[])
    assert r5.get("duplicate") is True, r5

    # 落库两条告警（5 与 50 各自一条）
    assert _count_alerts(env) == 2


def test_grade_persisted_by_finalize(env):
    det_id = _mk_det(env, rule_score=25.0, hits=[("稳赚不赔", 9.0), ("内幕消息", 8.0), ("导师带你", 8.0)])
    GradingService().finalize(env, detection_id=det_id)
    row = env.execute("SELECT grade, grade_reason FROM detections WHERE id=?", (det_id,)).fetchone()
    assert row["grade"] == "L3"
    reason = json.loads(row["grade_reason"])
    assert any(i["signal"] == "rule_hit" for i in reason)


# ---- 账号速查（只用公开信号）----

def test_account_green_no_signals(env):
    r = AccountIntelService().check(env, "user_a")
    assert r["risk_level"] == "green"
    assert r["score"] == 0
    assert r["evidence"][0]["signal"] == "no_signal"


def test_account_yellow_young_account(env):
    r = AccountIntelService().check(env, "user_b", signals={"registered_at": "2026-09-01"})
    assert r["risk_level"] == "yellow"
    assert any(e["signal"] == "account_age_young" for e in r["evidence"])


def test_account_red_reported_and_trap(env):
    r = AccountIntelService().check(env, "user_c", signals={
        "registered_at": "2026-09-01",
        "reported_count": 3,
        "trap_hit_ids": [1, 2],
    })
    assert r["risk_level"] == "red"
    signals = {e["signal"] for e in r["evidence"]}
    assert {"account_age_young", "reported", "trap_hit_evidence"} <= signals


def test_account_red_trap_evidence_only(env):
    r = AccountIntelService().check(env, "user_d", signals={"trap_hit_ids": [7]})
    assert r["risk_level"] == "red"
    assert r["score"] == 10.0


def test_account_signal_whitelist_drops_private(env):
    """D7：私有字段（手机号/身份证/密码）不进入画像。"""
    r = AccountIntelService().check(env, "user_e", signals={
        "registered_at": "2026-09-01",
        "phone": "13800000000",
        "id_card": "110101199001011234",
        "password": "hunter2",
    })
    assert "phone" not in r["signals"]
    assert "id_card" not in r["signals"]
    assert "password" not in r["signals"]
    assert r["signals"]["registered_at"]


def test_account_co_occurrence_gang_hint(env):
    svc = AccountIntelService()
    svc.check(env, "gang_a", signals={"trap_hit_ids": [11, 12]})
    svc.check(env, "gang_b", signals={"trap_hit_ids": [11]})
    r = svc.check(env, "gang_a", signals={"trap_hit_ids": [11, 12]})
    assert "gang_b" in r["co_occurring_accounts"]
    # 无关联账号共现为空
    svc.check(env, "lone_wolf", signals={})
    r2 = svc.check(env, "lone_wolf")
    assert r2["co_occurring_accounts"] == []


def test_account_timeline(env):
    # 构造一条命中即退役的真实蜜饵记录
    env.execute(
        "INSERT INTO honey_facts (id, bait_text, fingerprint, disguise_score, status, hit_count, retired_reason) "
        "VALUES (99, '蜜饵99', 'fp-timeline-99', 80, 'retired', 1, 'hit')",
    )
    env.commit()
    svc = AccountIntelService()
    r = svc.check(env, "tl_user", signals={"trap_hit_ids": [99]})
    tl = svc.timeline(env, r["account_id"])
    assert tl
    assert any(i["kind"] == "trap_hit" and "hit_count=1" in i["note"] for i in tl)


# ---- API 集成 ----

def test_p5_api_integration(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        # /alerts 为受保护端点（P1-1），需 admin key
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        auth = {"X-API-Key": key}
        # /scan/text 现在返回真实 grade + 证据链，且 L3+ 触发告警
        r = client.post("/api/v1/scan/text", json={"text": "稳赚不赔，导师带你内幕消息"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["grade"] in ("L3", "L4")
        assert isinstance(data["grade_reason"], list) and data["grade_reason"]
        det_id = data["detection_id"]

        # /grading/explain 证据链
        r2 = client.get(f"/api/v1/grading/explain/{det_id}")
        assert r2.status_code == 200
        expl = r2.json()["data"]
        assert expl["grade"] == data["grade"]
        assert all(set(i) == {"signal", "weight", "note"} for i in expl["grade_reason"])

        # explain 带蜜饵上下文 → L5
        r2b = client.get(f"/api/v1/grading/explain/{det_id}", params={"trap_hit_count": 1})
        assert r2b.json()["data"]["grade"] == "L5"

        # /grading/levels
        r3 = client.get("/api/v1/grading/levels")
        assert [l["level"] for l in r3.json()["data"]] == ["L1", "L2", "L3", "L4", "L5"]

        # /scan/text 的 L3+ 已落告警（/alerts 读取需 admin）
        r4 = client.get("/api/v1/alerts", headers=auth)
        alerts = r4.json()["data"]
        assert len(alerts) >= 1 and alerts[0]["level"] in ("L3", "L4")
        # 已读（写操作需 admin）
        r5 = client.post(f"/api/v1/alerts/{alerts[0]['id']}/read", headers=auth)
        assert r5.json()["data"]["unread"] == 0

        # /accounts/check 全流程（R4-F3：画像 upsert 需已认证；匿名只读不落库）
        r6 = client.post("/api/v1/accounts/check", json={
            "url_name": "api_scammer",
            "signals": {"registered_at": "2026-09-01", "reported_count": 2},
        }, headers=auth)
        b6 = r6.json()["data"]
        assert b6["risk_level"] == "red" and b6["evidence"] and b6["persisted"] is True

        # GET /accounts/{url_name}
        r7 = client.get("/api/v1/accounts/api_scammer")
        assert r7.json()["data"]["url_name"] == "api_scammer"

        # GET /accounts/{id}/timeline
        r8 = client.get(f"/api/v1/accounts/{b6['account_id']}/timeline")
        assert r8.status_code == 200 and isinstance(r8.json()["data"], list)

        # 404
        assert client.get("/api/v1/accounts/ghost_user").status_code == 404

    dbmod.close_all()
    get_settings.cache_clear()