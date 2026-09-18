"""严格验收 R3 修复回归测试（fix-engineer，t12）。

覆盖：
  1) P2-1：trap_hit_escalation_pending 重放后终态收敛——成功标记 resolved、
     检测已删除标记 terminal（停止重试），events 不再无界累积/每轮重扫；
  2) P2-2：cases(det_id) UNIQUE 索引由 ensure_schema 建出（迁移幂等），
     直插重复行 IntegrityError，应用层幂等发布不受影响；
  3) P3-1：GradingService.is_trap_escalated 共享幂等判定（scheduler/traps 共用）。

运行：pytest tests/test_r8_r3fixes.py -q（项目根）。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app import db as dbmod
from app.config import get_settings
from app.services import llm_provider

BAIT = "你好呀，我在网上看到一个很不错的兼职项目，感觉超适合你，我们加个V聊聊怎么样"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据库 + 重置 provider（no_key 降级模式）。"""
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
            grade: str | None = None) -> int:
    text_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, grade, status) "
        "VALUES ('manual', ?, ?, ?, ?)",
        (text_hash, content, grade, status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _pending_rows(conn):
    return conn.execute(
        "SELECT id, payload FROM events WHERE kind='trap_hit_escalation_pending' "
        "ORDER BY id"
    ).fetchall()


# ===========================================================================
# P2-1：pending 终态收敛
# ===========================================================================

def test_pending_marked_resolved_after_replay(env):
    """重放成功后 pending 行标记 resolved（不删除、不再重扫、不再重复升级）。"""
    from app.services.scheduler import job_trap_recheck

    det_id = _mk_det(env, BAIT, status="scanned")
    env.execute(
        "INSERT INTO events (kind, payload) VALUES ('trap_hit_escalation_pending', ?)",
        (json.dumps({"trap_id": 999, "detection_id": det_id, "grade": "L5"},
                    ensure_ascii=False),),
    )
    env.commit()

    job_trap_recheck(limit=10)   # 无蜜饵 → 仅重放

    det = env.execute("SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
    assert det["grade"] == "L5"
    rows = _pending_rows(env)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload.get("resolved") is True
    assert payload.get("resolved_at")
    assert payload.get("recovered") is True

    # 第二轮：已 resolved 的 pending 行被跳过 → 无新升级事件
    n_before = env.execute(
        "SELECT COUNT(*) n FROM events WHERE kind='trap_hit_escalated'"
    ).fetchone()["n"]
    job_trap_recheck(limit=10)
    n_after = env.execute(
        "SELECT COUNT(*) n FROM events WHERE kind='trap_hit_escalated'"
    ).fetchone()["n"]
    assert n_after == n_before == 1


def test_pending_terminal_on_deleted_det(env):
    """检测已被删除 → pending 行标记 terminal（终态，停止重试，不再每轮报错）。"""
    from app.services.scheduler import job_trap_recheck

    env.execute(
        "INSERT INTO events (kind, payload) VALUES ('trap_hit_escalation_pending', ?)",
        (json.dumps({"trap_id": 42, "detection_id": 999999, "grade": "L5"},
                    ensure_ascii=False),),
    )
    env.commit()

    job_trap_recheck(limit=10)   # 不应抛异常

    rows = _pending_rows(env)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload.get("terminal") is True
    assert payload.get("reason") == "detection_deleted"
    assert payload.get("resolved_at")

    # 再跑一轮：终态行被跳过，无异常、无新增升级
    job_trap_recheck(limit=10)
    assert env.execute(
        "SELECT COUNT(*) n FROM events WHERE kind='trap_hit_escalated'"
    ).fetchone()["n"] == 0


# ===========================================================================
# P2-2：cases UNIQUE(det_id) 索引
# ===========================================================================

def test_cases_unique_index_created_and_enforced(env):
    from app.services.cases import CaseService

    # ensure_schema（env fixture 已执行）应建出唯一索引
    idx = [r for r in env.execute("PRAGMA index_list('cases')").fetchall()
           if r["name"] == "idx_cases_det_unique"]
    assert idx, "cases 唯一索引未创建"
    assert idx[0]["unique"] == 1

    det_id = _mk_det(env, "唯一约束测试", status="processed", grade="L5")

    # 直插重复 det_id → IntegrityError（DB 层双保险）
    env.execute(
        "INSERT INTO cases (det_id, redacted_payload) VALUES (?, ?)",
        (det_id, "载荷A"),
    )
    env.commit()
    with pytest.raises(Exception) as e:
        env.execute(
            "INSERT INTO cases (det_id, redacted_payload) VALUES (?, ?)",
            (det_id, "载荷B"),
        )
        env.commit()
    assert "UNIQUE" in str(e.value)

    # 应用层幂等发布不受影响：同载荷返回已存在案例（不触发 IntegrityError）
    c1 = CaseService().publish(env, det_id=det_id, redacted_payload="载荷A")
    c2 = CaseService().publish(env, det_id=det_id, redacted_payload="载荷A")
    assert c1["id"] == c2["id"]


# ===========================================================================
# P3-1：共享幂等判定 GradingService.is_trap_escalated
# ===========================================================================

def test_is_trap_escalated_shared_check(env):
    from app.services.grading import GradingService

    # L5 + trap_hit 信号 → True
    det_hit = _mk_det(env, "已升级检测", status="processed", grade="L5")
    env.execute(
        "UPDATE detections SET grade_reason=? WHERE id=?",
        (json.dumps([{"signal": "trap_hit", "weight": 20.0, "note": "蜜饵实锤"}],
                    ensure_ascii=False), det_hit),
    )
    env.commit()
    assert GradingService.is_trap_escalated(env, det_hit) is True

    # L5 但无 trap_hit 信号（LLM+账号红路径）→ False
    det_llm = _mk_det(env, "LLM升级检测", status="processed", grade="L5")
    env.execute(
        "UPDATE detections SET grade_reason=? WHERE id=?",
        (json.dumps([{"signal": "llm_fraud", "weight": 12.0}], ensure_ascii=False), det_llm),
    )
    env.commit()
    assert GradingService.is_trap_escalated(env, det_llm) is False

    # 低级别 / 不存在 → False
    det_l3 = _mk_det(env, "低级别检测", status="processed", grade="L3")
    assert GradingService.is_trap_escalated(env, det_l3) is False
    assert GradingService.is_trap_escalated(env, 999999) is False
