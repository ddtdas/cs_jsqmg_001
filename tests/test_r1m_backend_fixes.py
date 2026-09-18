"""A4 修复回归测试：gate_logs 审计链断裂的幂等修复 + 篡改检测。"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from app.models import ensure_migrations, TABLES, INDEXES
from app.services.audit_log import GENESIS_HASH, _chain_input, verify_gate_chain


def _make_conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    for ddl in list(TABLES.values()) + list(INDEXES.values()):
        conn.execute(ddl)
    conn.commit()
    return conn


def _insert_row(conn, *, action, before="{}", after="{}", reason="", ts=None,
                payload_json="{}", prev_hash="", payload_hash="") -> int:
    ts = ts or "2026-09-14 00:00:00"
    cur = conn.execute(
        "INSERT INTO gate_logs (action, payload_hash, prev_hash, before, after, reason, ts, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (action, payload_hash, prev_hash, before, after, reason, ts, payload_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_repair_broken_chain_once(tmp_path):
    """历史断裂链（第 2 行 prev_hash 错误）在 ensure_migrations 后被修复为 valid。"""
    conn = _make_conn(tmp_path)
    # 插入 3 行，其中第 2 行的 prev_hash 故意写错（模拟历史断裂）
    r1 = _insert_row(conn, action="config.put", reason="a" * 20, prev_hash=GENESIS_HASH,
                     payload_hash="a" * 64, payload_json='{"k":"1"}')
    _insert_row(conn, action="case.publish", reason="b" * 20, prev_hash="f" * 64,  # 错误 prev
                payload_hash="b" * 64, payload_json='{"k":"2"}')
    _insert_row(conn, action="trap.retire", reason="c" * 20, prev_hash="c" * 64,
                payload_hash="d" * 64, payload_json='{"k":"3"}')

    assert verify_gate_chain(conn)["valid"] is False  # 修复前断裂

    n = ensure_migrations(conn)
    assert n >= 3  # 修复了链

    v = verify_gate_chain(conn)
    assert v["valid"] is True
    assert v["checked"] == 3


def test_repair_idempotent(tmp_path):
    """修复幂等：二次 ensure_migrations 不再改动，链仍 valid。"""
    conn = _make_conn(tmp_path)
    _insert_row(conn, action="config.put", reason="a" * 20, prev_hash=GENESIS_HASH,
                payload_hash="x" * 64, payload_json="{}")
    _insert_row(conn, action="case.publish", reason="b" * 20, prev_hash="wrong",
                payload_hash="y" * 64, payload_json="{}")

    ensure_migrations(conn)
    first = conn.execute("SELECT payload_hash FROM gate_logs ORDER BY id").fetchall()
    second_run = ensure_migrations(conn)
    after = conn.execute("SELECT payload_hash FROM gate_logs ORDER BY id").fetchall()
    assert second_run == 0  # 标记已存在，跳过
    assert [r["payload_hash"] for r in first] == [r["payload_hash"] for r in after]
    assert verify_gate_chain(conn)["valid"] is True


def test_tamper_detected_after_repair(tmp_path):
    """修复后篡改一行 → verify 断裂（篡改检测能力保留）。"""
    conn = _make_conn(tmp_path)
    _insert_row(conn, action="config.put", reason="a" * 20, prev_hash=GENESIS_HASH,
                payload_hash="x" * 64, payload_json="{}")
    _insert_row(conn, action="case.publish", reason="b" * 20, prev_hash="wrong",
                payload_hash="y" * 64, payload_json="{}")
    ensure_migrations(conn)
    assert verify_gate_chain(conn)["valid"] is True

    # 篡改最新一行的 before
    conn.execute("UPDATE gate_logs SET before='{\"tampered\":1}' WHERE id=(SELECT MAX(id) FROM gate_logs)")
    conn.commit()
    assert verify_gate_chain(conn)["valid"] is False


def test_already_valid_no_change(tmp_path):
    """链已完整且已有标记时，ensure_migrations 不触发回填（保留篡改检测）。"""
    conn = _make_conn(tmp_path)
    # 手工构造一条链式合法的链
    prev = GENESIS_HASH
    for i, action in enumerate(["config.put", "case.publish"]):
        reason = f"reason {i} " + "x" * 20
        ts = f"2026-09-14 00:00:0{i}"
        payload_json = f'{{"k":{i}}}'
        h = hashlib.sha256(
            _chain_input(prev, action=action, before="{}", after="{}",
                         reason=reason, ts=ts, payload_json=payload_json).encode("utf-8")
        ).hexdigest()
        conn.execute(
            "INSERT INTO gate_logs (action, payload_hash, prev_hash, before, after, reason, ts, payload_json) "
            "VALUES (?, ?, ?, '{}', '{}', ?, ?, ?)",
            (action, h, prev, reason, ts, payload_json),
        )
        prev = h
    conn.commit()
    assert verify_gate_chain(conn)["valid"] is True

    # 首次 ensure_migrations：打标记，不回填（链已 valid）
    ensure_migrations(conn)
    before = conn.execute("SELECT payload_hash FROM gate_logs ORDER BY id").fetchall()

    # 篡改
    conn.execute("UPDATE gate_logs SET after='{\"bad\":1}' WHERE id=(SELECT MAX(id) FROM gate_logs)")
    conn.commit()
    assert verify_gate_chain(conn)["valid"] is False

    # 再次 ensure_migrations：标记已存在，不应自动修复篡改（篡改仍可被检出）
    ensure_migrations(conn)
    assert verify_gate_chain(conn)["valid"] is False
