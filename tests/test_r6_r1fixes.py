"""严格验收 R1 修复回归测试（fix-engineer，t4）。

覆盖：
  1) P0-1/P1-1/P1-2：job_trap_recheck 真正 await check_hit；蜜饵实锤命中 →
     退役 + 触发检测升级 L5（grade/grade_reason/告警/事件）+ trap_hit_escalated；
  2) P1-3：job_evidence 去重由 LIKE 子串改 json_each 精确判定（det 1 已有包
     不再误判 det 11）；EvidenceService.build 对已打包检测拒绝重复建包（409）；
  3) P1-4：cases.publish 同一检测仅一个案例——同载荷幂等返回已存在案例，
     异载荷 409 case_exists，不再连锁重复在线学习/蒸馏。

运行：pytest tests/test_r6_r1fixes.py -q（项目根）。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider
from app.utils import ApiError

BAIT = "你好呀，我在网上看到一个很不错的兼职项目，感觉超适合你，我们加个V聊聊怎么样"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据库（含种子词库/社工库）+ 重置 provider（no_key 降级模式）。"""
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


def _mk_det(conn, content: str, *, status: str = "scanned",
            grade: str | None = None, rule_score: float = 0.0) -> int:
    """直插一条检测（默认 status=scanned，供蜜饵复查/证据/案例测试）。"""
    text_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, grade, status) "
        "VALUES ('manual', ?, ?, ?, ?, ?)",
        (text_hash, content, rule_score, grade, status),
    )
    conn.commit()
    return int(cur.lastrowid)


# ===========================================================================
# P0-1 / P1-1 / P1-2：job_trap_recheck await + 命中升级 L5
# ===========================================================================

def test_job_trap_recheck_awaits_hit_and_escalates_l5(env):
    """修复前：job 内 check_hit 协程从未执行 → 蜜饵不退役、检测不升级。
    修复后：蜜饵 hit→retired，触发检测升级 L5，且告警/事件/关联事件齐全。"""
    from app.services.scheduler import job_trap_recheck
    from app.services.trap_engine import TrapEngine

    engine = TrapEngine()
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = int(d["id"])
    engine.deploy(env, trap_id)
    engine.monitor(env, trap_id)

    # 与蜜饵文本一致的 scanned 检测（踩饵匹配源）
    det_id = _mk_det(env, BAIT, status="scanned")

    job_trap_recheck(limit=10)

    # 蜜饵命中即退役（P0-1：await 后 check_hit 真正执行）
    row = env.execute("SELECT * FROM honey_facts WHERE id=?", (trap_id,)).fetchone()
    assert row["status"] == "retired"
    assert row["retired_reason"] == "hit"
    assert row["hit_count"] == 1

    # P1-2：触发检测升级 L5 + 证据链含 trap_hit 信号
    det = env.execute("SELECT * FROM detections WHERE id=?", (det_id,)).fetchone()
    assert det["grade"] == "L5", f"蜜饵实锤应升级 L5，实际 {det['grade']}"
    reason = json.loads(det["grade_reason"])
    assert any(s["signal"] == "trap_hit" for s in reason), reason

    # L5 → 正式告警（L3+ 闸控）
    alert = env.execute(
        "SELECT * FROM alerts WHERE json_extract(payload, '$.detection_id') = ?",
        (det_id,),
    ).fetchone()
    assert alert is not None and alert["level"] == "L5"

    # 事件流：trap_hit / trap_retired / trap_hit_escalated / detection_graded / alert_created
    kinds = [r["kind"] for r in env.execute(
        "SELECT kind FROM events WHERE kind IN "
        "('trap_hit','trap_retired','trap_hit_escalated','detection_graded','alert_created')"
    ).fetchall()]
    for k in ("trap_hit", "trap_retired", "trap_hit_escalated",
              "detection_graded", "alert_created"):
        assert k in kinds, f"事件流缺 {k}：{kinds}"
    esc = [r for r in env.execute(
        "SELECT payload FROM events WHERE kind='trap_hit_escalated'").fetchall()]
    payload = json.loads(esc[0]["payload"])
    assert payload["trap_id"] == trap_id and payload["detection_id"] == det_id


def test_job_trap_recheck_no_hit_keeps_state(env):
    """未命中：蜜饵保持 monitored、检测分级不受影响、job 不抛异常。"""
    from app.services.scheduler import job_trap_recheck
    from app.services.trap_engine import TrapEngine

    engine = TrapEngine()
    d = engine.create_draft(env, bait_text=BAIT)
    trap_id = int(d["id"])
    engine.deploy(env, trap_id)
    engine.monitor(env, trap_id)

    det_id = _mk_det(env, "今天天气很好我们去公园散步吧", status="scanned")

    job_trap_recheck(limit=10)  # 不应抛异常

    row = env.execute("SELECT * FROM honey_facts WHERE id=?", (trap_id,)).fetchone()
    assert row["status"] == "monitored"
    det = env.execute("SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
    assert det["grade"] is None


# ===========================================================================
# P1-3：job_evidence 精确去重 + build 重复建包拦截
# ===========================================================================

def test_job_evidence_exact_dedup_avoids_like_collision(env):
    """det 1 已有包时，det 11 不再被 LIKE '%1%' 误判为已建包而漏建。"""
    from app.services.evidence import EvidenceService
    from app.services.scheduler import job_evidence

    id1 = _mk_det(env, "证据一", status="processed", grade="L4")
    assert id1 == 1
    for i in range(2, 11):          # 填充 2..10，让目标 id 为 11
        _mk_det(env, f"填充{i:02d}", status="processed", grade="L4")
    id11 = _mk_det(env, "证据十一", status="processed", grade="L4")
    assert id11 == 11

    EvidenceService().build(env, [id1])   # 先为 det 1 建包

    job_evidence(limit=20)

    rows = env.execute("SELECT id, det_ids FROM evidence_packages ORDER BY id").fetchall()
    packaged = [json.loads(r["det_ids"])[0] for r in rows]
    assert packaged.count(1) == 1, "det 1 不应被重复建包"
    assert 11 in packaged, "det 11 应被精确去重放过并建包（LIKE '%1%' 会漏建）"
    assert len(packaged) == 11, "det 1..11 每个检测都应恰好一个包"


def test_evidence_build_rejects_duplicate_det(env):
    """服务层去重约束：同一检测重复建包 → 409 evidence_exists。"""
    from app.services.evidence import EvidenceService

    det_id = _mk_det(env, "重复建包测试", status="processed", grade="L4")
    svc = EvidenceService()
    assert svc.build(env, [det_id])["pkg_id"] > 0
    with pytest.raises(ApiError) as e:
        svc.build(env, [det_id])
    assert e.value.code == "evidence_exists"
    assert e.value.status_code == 409
    n = env.execute("SELECT COUNT(*) AS n FROM evidence_packages").fetchone()["n"]
    assert n == 1


# ===========================================================================
# P1-4：cases.publish 同一检测仅一个案例
# ===========================================================================

def test_case_publish_dedup_idempotent_and_conflict(env):
    from app.services.cases import CaseService

    det_id = _mk_det(env, "案例去重测试", status="processed", grade="L5")
    svc = CaseService()

    c1 = svc.publish(env, det_id=det_id, redacted_payload="同一脱敏载荷A")
    # 同载荷重复发布 → 幂等返回已存在案例，不再重复入库
    c2 = svc.publish(env, det_id=det_id, redacted_payload="同一脱敏载荷A")
    assert c1["id"] == c2["id"]
    assert env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 1

    # 异载荷重复发布 → 409 case_exists
    with pytest.raises(ApiError) as e:
        svc.publish(env, det_id=det_id, redacted_payload="不同脱敏载荷B")
    assert e.value.code == "case_exists"
    assert e.value.status_code == 409
    assert env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 1
