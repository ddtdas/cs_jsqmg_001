"""告警端点：GET /alerts、POST /alerts/{id}/read。

对应主方案 §4.2 alerts.py 与 MCP af_list_alerts / af_mark_alert_read 透传目标。
告警生成由 grading.finalize 的 L3+ 闸控触发（alerting.AlertService.evaluate）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import get_db
from ..deps import require_admin
from ..services.alerting import AlertService
from ..utils import ok

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("", dependencies=[Depends(require_admin)])
def list_alerts(
    db=Depends(get_db),
    level: str | None = Query(default=None, pattern="^(L3|L4|L5)$"),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict:
    """告警列表（级别/未读过滤）。含告警 payload（检测 id/分级），读取需 admin（P1-1）。"""
    return ok(AlertService().list_alerts(db, level=level, unread_only=unread_only, limit=limit))


@router.post("/{alert_id}/read", dependencies=[Depends(require_admin)])
def mark_alert_read(alert_id: int, db=Depends(get_db)) -> dict:
    """标记已读。写操作需 admin（P1-1）。"""
    return ok(AlertService().mark_read(db, alert_id))