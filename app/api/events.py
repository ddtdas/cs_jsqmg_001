"""事件流端点：GET /api/v1/events、GET /api/v1/events/{id}。

对应主方案 §4.2 事件表（events 落库，WS 同源）与 MCP af_list_events / af_get_event
透传目标。统一封包 {ok,data}（MCP 依赖）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query

from ..db import get_db
from ..deps import require_admin
from ..utils import ApiError, ok

router = APIRouter(prefix="/events", tags=["events"])


def _row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["payload"] = json.loads(d["payload"] or "{}")
    except json.JSONDecodeError:
        pass
    return d


@router.get("", dependencies=[Depends(require_admin)])
def list_events(
    db=Depends(get_db),
    kind: str | None = Query(default=None, description="按事件类型过滤（hit/trap_hit/alert/...）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """事件流（分页/按 kind 过滤）。事件 payload 可能含原文，读取需 admin（P1-1）。"""
    conds: list[str] = []
    args: list = []
    if kind:
        conds.append("kind=?")
        args.append(kind)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    total = db.execute(f"SELECT COUNT(*) AS n FROM events{where}", tuple(args)).fetchone()["n"]
    rows = db.execute(
        f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ? OFFSET ?",
        tuple(args + [page_size, (page - 1) * page_size]),
    ).fetchall()
    return ok({
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": [_row_to_dict(r) for r in rows],
    })


@router.get("/{event_id}", dependencies=[Depends(require_admin)])
def get_event(event_id: int, db=Depends(get_db)) -> dict:
    row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise ApiError("event_not_found", f"事件不存在: id={event_id}", 404)
    return ok(_row_to_dict(row))