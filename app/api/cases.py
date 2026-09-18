"""案例库端点（R6）：/api/v1/cases。

对应主方案 §4.2 cases.py 与 MCP af_list_cases / af_publish_case / af_case_graph 透传目标。
公开查询只暴露脱敏载荷（D7）。
发布为敏感操作（§11 / P1-1/P1-2）：require_admin + confirm=true + reason≥20 双确认，
成功后写 gate_logs（action='case.publish'，before/after/payload_hash）+ 事件。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.audit_log import write_gate_log
from ..services.cases import CaseService
from ..utils import ApiError, ok

router = APIRouter(prefix="/cases", tags=["cases"])

_service = CaseService()

REASON_MIN_LEN = 20


class PublishCaseRequest(BaseModel):
    det_id: int = Field(ge=1, description="关联检测记录 id")
    redacted_payload: str = Field(min_length=1, max_length=10000, description="脱敏后载荷")
    desensitize_log: list[dict] | None = Field(default=None, description="脱敏记录 [{from,to}]")
    graph_tags: list[str] | None = Field(default=None, description="骗术类型标签（缺省自动从命中推导）")
    confirm: bool = Field(default=False, description="敏感操作双确认：必须为 true")
    reason: str = Field(default="", description="操作理由（≥20 字）")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _require_confirm(confirm: bool, reason: str) -> str:
    """敏感操作闸控：confirm=true 且 reason.strip()≥20 字，否则 403/422。返回去空格后的 reason。"""
    if not confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = reason.strip()
    if len(reason) < REASON_MIN_LEN:
        raise ApiError("reason_too_short",
                       f"reason 至少 {REASON_MIN_LEN} 字，当前 {len(reason)} 字", 422)
    return reason


@router.post("/publish", dependencies=[Depends(require_admin)])
def publish_case(payload: PublishCaseRequest, db=Depends(get_db)) -> dict:
    """发布案例：脱敏强制校验前置（含敏感信息拒绝入库）+ confirm+reason 双确认 + GateLog 审计。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    det = db.execute("SELECT id, grade FROM detections WHERE id=?", (payload.det_id,)).fetchone()
    if det is None:
        raise ApiError("detection_not_found", f"检测记录不存在: id={payload.det_id}", 404)

    result = _service.publish(
        db, det_id=payload.det_id, redacted_payload=payload.redacted_payload,
        desensitize_log=payload.desensitize_log, graph_tags=payload.graph_tags)

    now = _now()
    payload_json = json.dumps(
        {"action": "case.publish", "det_id": payload.det_id, "reason": reason},
        ensure_ascii=False, sort_keys=True,
    )
    # P1-C6：GateLog 链式审计
    write_gate_log(
        db,
        action="case.publish",
        payload_json=payload_json,
        before=json.dumps({"det_id": payload.det_id}, ensure_ascii=False),
        after=json.dumps({"case_id": result["id"], "det_id": payload.det_id,
                          "graph_tags": result.get("graph_tags")}, ensure_ascii=False),
        reason=reason,
        ts=now,
    )
    db.commit()
    return ok(result)


@router.get("")
def list_cases(
    db=Depends(get_db),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    tag: str | None = Query(default=None),
) -> dict:
    """公开查询（分页/按骗术标签过滤）。

    D7 最小化（P0-修复2）：公开响应剥离 desensitize_log —— 该字段可携带脱敏前
    原文（{from: 原文, to: 掩码}），公开查询只暴露脱敏载荷；写库保留（审计需要），
    仅响应层剥离。MCP af_list_cases 透传本端点，同样只拿到脱敏载荷。
    """
    data = _service.list_cases(db, page=page, page_size=page_size, tag=tag)
    for item in data.get("items", []):
        item.pop("desensitize_log", None)
    return ok(data)


@router.get("/graph")
def case_graph(db=Depends(get_db)) -> dict:
    """骗术图谱聚合数据（供 ECharts 渲染）。"""
    return ok(_service.graph(db))