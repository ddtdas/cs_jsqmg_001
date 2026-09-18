"""存量重复数据一次性幂等清洗（严格验收 R2 · P1-B）。

背景（宏观推演 R2 实证）：cases 表存在同 det 重复（det 345×1125、150×373 等），
evidence_packages 存在完全冗余重复包（[150]×9、[1]×4、[130,128]×2 等 9 组）。
R1 修复已从应用层阻止新增重复，本脚本负责清理存量，使后续可安全加
UNIQUE(det_id) 约束（P2-8/P2-9 运维项的前置步骤）。

规则：
  - cases：同一 det_id 只保留最早一条（MIN(id)），其余删除（幂等）；
  - evidence_packages：每个 det 只保留"最早包含它"的包，仅含已被更早包覆盖
    det 的包整体删除（完全冗余）；[130,128]×2 这类保留首包、删除次包；
    已冻结包（frozen=1，存证语义）**永不自动删除**——冗余冻结包保留；
  - 脏数据防护：evidence det_ids 非合法 JSON 的行跳过（json_valid 门控），不中断；
  - 幂等：重复执行第二次删除 0 行；
  - 安全：执行前用 SQLite backup API 备份到 var/backups/（WAL 一致快照）；
    删除与审计写在单个事务内；审计写 events(kind='data_cleanup') 与
    gate_logs(action='data.cleanup_duplicates')（哈希链可验证）。

用法（项目根，.venv）：
  python -m scripts.cleanup_duplicates --dry-run   # 只统计不删除
  python -m scripts.cleanup_duplicates             # 备份 + 清洗 + 审计

说明：本脚本由 fix-engineer 于严格验收 R2 编写并执行，属运维/迁移操作。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

sys.path.insert(0, ".")  # 兼容 `python scripts/cleanup_duplicates.py` 直跑


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def dedup_cases(conn, *, dry_run: bool = False) -> dict:
    """cases：同 det 保留最早一条。返回统计。"""
    total = conn.execute("SELECT COUNT(*) AS n FROM cases").fetchone()["n"]
    dup_rows = conn.execute(
        "SELECT det_id, COUNT(*) AS n FROM cases "
        "WHERE det_id IS NOT NULL GROUP BY det_id HAVING n > 1"
    ).fetchall()
    deleted = 0
    for r in dup_rows:
        keep = conn.execute(
            "SELECT MIN(id) AS m FROM cases WHERE det_id = ?", (r["det_id"],)
        ).fetchone()["m"]
        if dry_run:
            deleted += r["n"] - 1
        else:
            cur = conn.execute(
                "DELETE FROM cases WHERE det_id = ? AND id <> ?", (r["det_id"], keep)
            )
            deleted += cur.rowcount
    return {
        "table": "cases",
        "total": total,
        "dup_det_groups": len(dup_rows),
        "deleted": deleted,
    }


def dedup_evidence_packages(conn, *, dry_run: bool = False) -> dict:
    """evidence_packages：删除"每个 det 都已被更早包覆盖"的完全冗余**未冻结**包。

    判定：包 p 冗余 ⟺ 对 p 的每个 det d，都存在一个 id < p.id 且包含 d 的包。
    已冻结包（frozen=1，存证语义）永不删除——冗余冻结包保留，避免破坏法律存证。
    json_valid 门控保证脏行不进入 json_each（不中断、不清除、由 P2-1 防御）。
    """
    total = conn.execute("SELECT COUNT(*) AS n FROM evidence_packages").fetchone()["n"]
    rows = conn.execute(
        """
        SELECT p.id FROM evidence_packages p
        WHERE p.frozen = 0
          AND json_valid(p.det_ids) = 1
          AND NOT EXISTS (
              SELECT 1 FROM json_each(p.det_ids) je
              WHERE NOT EXISTS (
                  SELECT 1 FROM evidence_packages q
                  WHERE q.id < p.id
                    AND json_valid(q.det_ids) = 1
                    AND EXISTS (
                        SELECT 1 FROM json_each(q.det_ids) qe
                        WHERE CAST(qe.value AS TEXT) = CAST(je.value AS TEXT)
                    )
              )
          )
        """
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    deleted = 0
    if ids:
        if dry_run:
            deleted = len(ids)
        else:
            marks = ",".join("?" * len(ids))
            cur = conn.execute(
                f"DELETE FROM evidence_packages WHERE id IN ({marks})", ids
            )
            deleted = cur.rowcount
    return {
        "table": "evidence_packages",
        "total": total,
        "redundant_pkgs": len(ids),
        "deleted": deleted,
    }


def _write_audit(conn, results: list[dict], dry_run: bool) -> None:
    """审计：events + gate_logs（哈希链）记录清洗结果。"""
    payload = {
        "action": "data.cleanup_duplicates",
        "dry_run": dry_run,
        "ts": _now(),
        "results": results,
    }
    conn.execute(
        "INSERT INTO events (kind, payload) VALUES ('data_cleanup', ?)",
        (json.dumps(payload, ensure_ascii=False),),
    )
    from app.services.audit_log import write_gate_log

    write_gate_log(
        conn,
        action="data.cleanup_duplicates",
        payload_json=json.dumps(
            {"action": "data.cleanup_duplicates", "dry_run": dry_run},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps({r["table"]: r["total"] for r in results}, ensure_ascii=False),
        after=json.dumps({r["table"]: r["deleted"] for r in results}, ensure_ascii=False),
        reason="严格验收R2 P1-B：存量重复案例/证据包一次性幂等清洗",
    )


def backup_db() -> str:
    """SQLite backup API 做 WAL 一致快照（主控在线亦可安全备份）。"""
    from app.db import db_path
    from app.config import get_settings

    src = db_path(get_settings())
    import sqlite3
    from pathlib import Path

    backup_dir = Path(src).parent.parent / "var" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"af.db.r2cleanup_{ts}.bak"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return str(dest)


def run(conn, *, dry_run: bool = False) -> dict:
    """执行两表清洗（单个事务 + 审计），返回统计。"""
    try:
        results = [
            dedup_cases(conn, dry_run=dry_run),
            dedup_evidence_packages(conn, dry_run=dry_run),
        ]
        if not dry_run:
            _write_audit(conn, results, dry_run=False)
        conn.commit()
        return {"results": results, "dry_run": dry_run}
    except Exception:
        conn.rollback()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="存量重复数据清洗（P1-B，幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不删除")
    args = parser.parse_args(argv)

    from app import db as dbmod

    dbmod.close_all()
    conn = dbmod.get_conn()
    dbmod.ensure_schema()  # 确保表存在（对生产库为 no-op）
    if args.dry_run:
        out = run(conn, dry_run=True)
        print("DRY_RUN:", json.dumps(out, ensure_ascii=False, indent=2))
    else:
        bak = backup_db()
        print("BACKUP:", bak)
        out = run(conn, dry_run=False)
        print("CLEANED:", json.dumps(out, ensure_ascii=False, indent=2))
    dbmod.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
