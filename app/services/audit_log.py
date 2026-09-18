"""GateLog 哈希链审计（P1-C6）：payload_hash = sha256(prev + action + before + after + reason + ts + payload_json)。

承诺语义：
  - 每行记录 prev_hash（上一行的 payload_hash），首行用固定 GENESIS_HASH；
  - payload_hash 是"上一行哈希 + 本行全部审计字段"的 sha256 —— 链式承诺，篡改任一行
    （before/after/reason/action/ts/payload_json/prev_hash）即后续全部断裂；
  - verify_gate_chain() 全链重算校验，供 GET /api/v1/system/audit-verify 与测试使用。

局限（与证据哈希链一致，README 口径）：链与内容同库共存，持库者可整体重算并重写，
防"误改/意外篡改"可检出，不防"持库者主动重算"；外部信任锚点（签名/RFC3161）为后续增强项。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..utils import ApiError, ok

# 首行固定前驱哈希（链锚点）
GENESIS_HASH = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _chain_input(prev_hash: str, *, action: str, before: str, after: str,
                 reason: str, ts: str, payload_json: str) -> str:
    return prev_hash + str(action) + str(before) + str(after) + str(reason) + str(ts) + str(payload_json)


def write_gate_log(conn, *, action: str, payload_json: str,
                   before: str = "{}", after: str = "{}",
                   reason: str = "", ts: str | None = None) -> int:
    """链式写一条 GateLog（代替旧的单行独立哈希）。返回 gate_log_id。

    payload_json / before / after 传 JSON 字符串（写入 payload_json 列，
    审计可复核；旧行迁移后 payload_json 为空串，见 models._MIGRATIONS）。
    """
    prev = conn.execute("SELECT payload_hash FROM gate_logs ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = GENESIS_HASH if prev is None else prev["payload_hash"]
    ts = ts or _now()
    chained = hashlib.sha256(
        _chain_input(prev_hash, action=action, before=before, after=after,
                     reason=reason, ts=ts, payload_json=payload_json).encode("utf-8")
    ).hexdigest()
    cur = conn.execute(
        "INSERT INTO gate_logs (action, payload_hash, prev_hash, before, after, reason, ts, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (action, chained, prev_hash, before, after, reason, ts, payload_json),
    )
    return int(cur.lastrowid)


def verify_gate_chain(conn) -> dict:
    """全链重算校验：返回 {valid, checked, broken_at?}。任一行断裂即 valid=False。"""
    rows = conn.execute("SELECT * FROM gate_logs ORDER BY id").fetchall()
    prev = GENESIS_HASH
    for r in rows:
        if r["prev_hash"] != prev:
            return {"valid": False, "checked": int(r["id"]) - 1, "broken_at": int(r["id"])}
        expected = hashlib.sha256(
            _chain_input(prev, action=str(r["action"]), before=str(r["before"]),
                         after=str(r["after"]), reason=str(r["reason"]),
                         ts=str(r["ts"]), payload_json=str(r["payload_json"] or "")).encode("utf-8")
        ).hexdigest()
        if r["payload_hash"] != expected:
            return {"valid": False, "checked": int(r["id"]) - 1, "broken_at": int(r["id"])}
        prev = r["payload_hash"]
    return {"valid": True, "checked": len(rows)}


def audit_verify_endpoint(conn) -> dict:
    """GET /api/v1/system/audit-verify 响应体（统一封包）。"""
    v = verify_gate_chain(conn)
    if not v["valid"]:
        raise ApiError("audit_chain_broken",
                       f"GateLog 哈希链在第 {v['broken_at']} 行断裂（篡改或迁移异常）", 409)
    return ok(v)