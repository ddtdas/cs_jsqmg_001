"""严格验收 R2 修复回归测试（fix-engineer，t8）。

覆盖：
  1) P1-A：trap 升级路径补偿——finalize 失败写入 trap_hit_escalation_pending，
     job_trap_recheck 每轮先重放未决升级（幂等、recovered 标记、不重复升级）；
  2) P3-1：同检测被多蜜饵命中 → 只升级一次（无重复 detection_graded /
     trap_hit_escalated 事件噪声）；
  3) P1-C：手工 POST /traps/{id}/check-hit 命中后联动升级检测（L5+告警+事件），
     支持显式 detection_id 与按 text 反查；无匹配检测 → escalation.status=skipped；
  4) P2-1：evidence json_each 去重对 malformed JSON 脏数据免疫（build/job 不 500 不中止）；
  5) P1-B：scripts/cleanup_duplicates 存量重复案例/证据包幂等清洗（二次执行 0 删除）。

运行：pytest tests/test_r7_r2fixes.py -q（项目根）。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

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
    text_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cur = conn.execute(
        "INSERT INTO detections (source, text_hash, content, rule_score, grade, status) "
        "VALUES ('manual', ?, ?, ?, ?, ?)",
        (text_hash, content, rule_score, grade, status),
    )
    conn.commit()
    return int(cur.lastrowid)


def _mk_trap(conn, bait_text: str = BAIT) -> int:
    from app.services.trap_engine import TrapEngine

    engine = TrapEngine()
    d = engine.create_draft(conn, bait_text=bait_text)
    trap_id = int(d["id"])
    engine.deploy(conn, trap_id)
    engine.monitor(conn, trap_id)
    return trap_id


def _event_count(conn, kind: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE kind=?", (kind,)
    ).fetchone()["n"]


# ===========================================================================
# P1-A：升级路径补偿（未决事件重放）
# ===========================================================================

def test_trap_recheck_pending_compensation(env):
    """finalize 失败路径的补偿：未决事件在下一轮 job 重放并完成 L5 升级（幂等）。"""
    from app.services.scheduler import job_trap_recheck

    det_id = _mk_det(env, BAIT, status="scanned")
    # 模拟一次升级失败留下的未决事件（无对应蜜饵，仅未决记录）
    env.execute(
        "INSERT INTO events (kind, payload) VALUES ('trap_hit_escalation_pending', ?)",
        (json.dumps({"trap_id": 999, "detection_id": det_id, "grade": "L5"},
                    ensure_ascii=False),),
    )
    env.commit()

    # 无任何蜜饵 → job 仅执行补偿重放
    job_trap_recheck(limit=10)

    det = env.execute("SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
    assert det["grade"] == "L5", "未决升级应在 job 重放时完成"

    rows = env.execute(
        "SELECT payload FROM events WHERE kind='trap_hit_escalated' ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    assert payload["detection_id"] == det_id and payload.get("recovered") is True

    # 幂等：再跑一轮，不再产生新的升级事件
    job_trap_recheck(limit=10)
    assert _event_count(env, "trap_hit_escalated") == 1


# ===========================================================================
# P3-1：多蜜饵命中同一检测 → 只升级一次
# ===========================================================================

def test_trap_recheck_multi_trap_hit_single_escalation(env):
    from app.services.scheduler import job_trap_recheck

    _mk_trap(env, BAIT)          # trap A
    _mk_trap(env, BAIT)          # trap B（同 bait，都会命中同一检测）
    det_id = _mk_det(env, BAIT, status="scanned")

    job_trap_recheck(limit=10)

    det = env.execute("SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
    assert det["grade"] == "L5"
    assert _event_count(env, "trap_hit_escalated") == 1, "同检测只应升级一次"
    assert _event_count(env, "detection_graded") == 1, "不应重复 finalize"
    # 两个蜜饵都应命中即退役
    retired = env.execute(
        "SELECT COUNT(*) AS n FROM honey_facts WHERE status='retired'"
    ).fetchone()["n"]
    assert retired == 2


# ===========================================================================
# P1-C：手工 check-hit 命中联动升级
# ===========================================================================

def test_manual_check_hit_escalates_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AF_LLM_API_KEY", "")
    get_settings.cache_clear()
    dbmod.close_all()
    llm_provider.set_provider(None)

    from app.main import app

    with TestClient(app) as client:
        key = (tmp_path / "bootstrap_admin_key.txt").read_text(encoding="utf-8").strip()
        auth = {"X-API-Key": key}

        # 蜜饵 + 匹配检测（scanned）
        trap_id = _mk_trap(dbmod.get_conn(), BAIT)
        det_id = _mk_det(dbmod.get_conn(), BAIT, status="scanned")

        r = client.post(f"/api/v1/traps/{trap_id}/check-hit",
                        json={"text": BAIT}, headers=auth)
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["hit"] is True and body["status"] == "retired"
        assert body["escalation"]["status"] == "escalated"
        assert body["escalation"]["detection_id"] == det_id

        det = dbmod.get_conn().execute(
            "SELECT grade FROM detections WHERE id=?", (det_id,)).fetchone()
        assert det["grade"] == "L5", "手工 check-hit 命中应联动升级检测"
        assert _event_count(dbmod.get_conn(), "trap_hit_escalated") == 1

        # 显式 detection_id：指向与 text 不同的检测
        trap2 = _mk_trap(dbmod.get_conn(), BAIT)
        det2 = _mk_det(dbmod.get_conn(), "另一条待升级检测内容", status="scanned")
        r2 = client.post(f"/api/v1/traps/{trap2}/check-hit",
                         json={"text": BAIT, "detection_id": det2}, headers=auth)
        assert r2.json()["data"]["escalation"]["detection_id"] == det2
        assert dbmod.get_conn().execute(
            "SELECT grade FROM detections WHERE id=?", (det2,)).fetchone()["grade"] == "L5"

        # 命中但无匹配检测 → skipped（不影响命中主结果）
        trap3 = _mk_trap(dbmod.get_conn(), "无对应检测的蜜饵文案AAA")
        r3 = client.post(f"/api/v1/traps/{trap3}/check-hit",
                         json={"text": "无对应检测的蜜饵文案AAA"}, headers=auth)
        esc = r3.json()["data"]["escalation"]
        assert esc["status"] == "skipped" and esc["reason"] == "no_matching_detection"

    dbmod.close_all()
    get_settings.cache_clear()


# ===========================================================================
# P2-1：json_each 对 malformed JSON 脏数据免疫
# ===========================================================================

def test_evidence_json_valid_guard_dirty_rows(env):
    from app.services.evidence import EvidenceService
    from app.services.scheduler import job_evidence

    # 历史脏数据：det_ids 非合法 JSON
    env.execute(
        "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) "
        "VALUES ('not-json[[[', '[]', '[]', 0)"
    )
    env.commit()

    det_a = _mk_det(env, "脏数据免疫A", status="processed", grade="L4")
    det_b = _mk_det(env, "脏数据免疫B", status="processed", grade="L4")

    # service.build 不因脏行 500（且能正常建包）
    pkg = EvidenceService().build(env, [det_a])
    assert pkg["pkg_id"] > 0
    # job_evidence 不因脏行中止（det_b 应被建包）
    job_evidence(limit=10)
    n = env.execute(
        "SELECT COUNT(*) AS n FROM evidence_packages WHERE json_valid(det_ids)=1"
    ).fetchone()["n"]
    assert n == 2, "两个合法包都应存在（脏行被跳过且未中断）"


# ===========================================================================
# P1-B：存量重复数据幂等清洗
# ===========================================================================

def test_cleanup_duplicates_idempotent(env):
    from app.services.evidence import EvidenceService
    from scripts.cleanup_duplicates import dedup_cases, dedup_evidence_packages

    # ---- cases：det1×3、det2×2 ----
    det1 = _mk_det(env, "案例重复1", status="processed", grade="L5")
    det2 = _mk_det(env, "案例重复2", status="processed", grade="L5")
    # R3（P2-2）：新库已带 cases UNIQUE(det_id) 索引，直插重复会被 DB 层拒绝；
    # 本测试针对"索引加装前的遗留脏数据"场景，先临时摘下索引再构造重复行
    #（与线上 legacy 库清洗前置一致），验证 dedup_cases 后恢复索引语义。
    env.execute("DROP INDEX IF EXISTS idx_cases_det_unique")
    for det_id in (det1, det2):
        for i in range(3 if det_id == det1 else 2):
            env.execute(
                "INSERT INTO cases (det_id, redacted_payload) VALUES (?, ?)",
                (det_id, f"脱敏载荷{det_id}-{i}"),
            )
    env.commit()

    # ---- evidence：pkg1=[det1] 后再直插两条重复包；多 det 场景 [det1,det2] ----
    EvidenceService().build(env, [det1])
    env.execute(
        "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) "
        "VALUES (?, '[]', '[]', 0)", (json.dumps([det1]),),
    )
    env.execute(
        "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) "
        "VALUES (?, '[]', '[]', 0)", (json.dumps([det1, det2]),),
    )
    env.execute(
        "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) "
        "VALUES (?, '[]', '[]', 0)", (json.dumps([det2]),),
    )
    env.commit()

    before_cases = env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
    before_pkgs = env.execute("SELECT COUNT(*) AS n FROM evidence_packages").fetchone()["n"]
    assert before_cases == 5
    assert before_pkgs == 4  # [det1] ×2 + [det1,det2] + [det2]

    r_cases = dedup_cases(env, dry_run=False)
    r_pkgs = dedup_evidence_packages(env, dry_run=False)
    env.commit()

    assert r_cases["deleted"] == 3, "det1 删 2 + det2 删 1"
    assert r_pkgs["deleted"] == 2, "[det1] 重复包 + [det2]（被 [det1,det2] 覆盖）各删 1"
    assert env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 2
    assert env.execute("SELECT COUNT(*) AS n FROM evidence_packages").fetchone()["n"] == 2

    # 保留的是每组第一条（MIN(id)）
    keep_cases = [r["det_id"] for r in env.execute(
        "SELECT det_id, COUNT(*) AS n FROM cases GROUP BY det_id").fetchall()]
    assert sorted(keep_cases) == sorted([det1, det2])

    # 幂等：二次执行 0 删除
    assert dedup_cases(env, dry_run=False)["deleted"] == 0
    assert dedup_evidence_packages(env, dry_run=False)["deleted"] == 0


def test_cleanup_evidence_never_deletes_frozen(env):
    """存证保护：冗余但已冻结（frozen=1）的证据包永不删除。"""
    from app.services.evidence import EvidenceService
    from scripts.cleanup_duplicates import dedup_evidence_packages

    det = _mk_det(env, "冻结存证保护", status="processed", grade="L5")
    pkg1 = EvidenceService().build(env, [det])
    env.execute("UPDATE evidence_packages SET frozen=1, frozen_at=datetime('now') WHERE id=?",
                (pkg1["pkg_id"],))
    # 更晚的同 det 未冻结重复包
    env.execute(
        "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) "
        "VALUES (?, '[]', '[]', 0)", (json.dumps([det]),),
    )
    # 更晚的同 det 已冻结重复包
    env.execute(
        "INSERT INTO evidence_packages (det_ids, screenshots, hash_chain, frozen) "
        "VALUES (?, '[]', '[]', 1)", (json.dumps([det]),),
    )
    env.commit()

    r = dedup_evidence_packages(env, dry_run=False)
    env.commit()
    assert r["deleted"] == 1, "只删未冻结冗余包（中间那条）"
    n_frozen = env.execute(
        "SELECT COUNT(*) AS n FROM evidence_packages WHERE frozen=1"
    ).fetchone()["n"]
    assert n_frozen == 2, "两个冻结包（最早 + 冗余冻结）都必须保留"


def test_cleanup_duplicates_dry_run_no_delete(env):
    from scripts.cleanup_duplicates import dedup_cases, dedup_evidence_packages

    det = _mk_det(env, "dryrun案例", status="processed", grade="L5")
    env.execute("DROP INDEX IF EXISTS idx_cases_det_unique")   # R3 P2-2：摘索引构造遗留重复
    env.execute(
        "INSERT INTO cases (det_id, redacted_payload) VALUES (?, ?)", (det, "载荷A"))
    env.execute(
        "INSERT INTO cases (det_id, redacted_payload) VALUES (?, ?)", (det, "载荷B"))
    env.commit()

    r = dedup_cases(env, dry_run=True)
    assert r["deleted"] == 1
    assert env.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"] == 2, "dry-run 不删除"
