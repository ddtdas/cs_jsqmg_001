"""证据包端点（R5）：/api/v1/evidence/*。

对应主方案 §4.2 evidence.py 与 MCP af_build_evidence / af_freeze_evidence /
af_export_evidence / af_report_template 透传目标。
写操作（build/freeze/export）需 require_admin（D6 / P1-1）；
详情单查（GET /evidence/{pkg_id}）含检测原文（content），读取需 require_admin（P0-1）；
举报模板保留公开（只读，模板内嵌内容经正则脱敏后输出，D7）。
冻结为敏感操作（§11 / P1-A 修复）：require_admin + confirm=true + reason≥20 双确认
（与 traps.retire / cases.publish / config PUT 同一闸控形态），
成功后写 gate_logs（action='evidence.freeze'，before/after/payload_hash）+ 事件。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.audit_log import write_gate_log
from ..services.evidence import EvidenceService
from ..utils import ApiError, ok

router = APIRouter(prefix="/evidence", tags=["evidence"])

_service = EvidenceService()

# 敏感操作 reason 最短字数（与 traps/cases/config/auth 对齐）
REASON_MIN_LEN = 20


class BuildEvidenceRequest(BaseModel):
    det_ids: list[int] = Field(min_length=1, description="关联检测记录 id 列表")
    screenshots: list[str] | None = Field(default=None, description="截图文件路径列表（Playwright 截图表占位）")


class FreezeEvidenceRequest(BaseModel):
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


@router.post("/build", dependencies=[Depends(require_admin)])
def build_evidence(payload: BuildEvidenceRequest, db=Depends(get_db)) -> dict:
    """组装证据包（文本/命中详情/时间戳/来源 + 哈希链）。写操作需 admin（P1-1）。"""
    return ok(_service.build(db, payload.det_ids, payload.screenshots))


@router.get("/{pkg_id}", dependencies=[Depends(require_admin)])
def get_evidence(pkg_id: int, db=Depends(get_db)) -> dict:
    """证据包详情 + 完整性校验。含检测原文（content），读取需 admin（P0-1）。"""
    return ok({"package": _service.export(db, pkg_id, "json")["content"], "verified": _service.verify(db, pkg_id)})


@router.post("/{pkg_id}/freeze", dependencies=[Depends(require_admin)])
def freeze_evidence(pkg_id: int, payload: FreezeEvidenceRequest, db=Depends(get_db)) -> dict:
    """冻结：哈希不可变。敏感操作（P1-A）：confirm=true + reason≥20 双确认 + GateLog 审计。"""
    reason = _require_confirm(payload.confirm, payload.reason)
    result = _service.freeze(db, pkg_id)

    now = _now()
    payload_json = json.dumps(
        {"action": "evidence.freeze", "pkg_id": pkg_id, "reason": reason},
        ensure_ascii=False, sort_keys=True,
    )
    # P1-C6：GateLog 链式审计（与 trap.retire/case.publish/config.put 同一机制）
    write_gate_log(
        db,
        action="evidence.freeze",
        payload_json=payload_json,
        before=json.dumps({"pkg_id": pkg_id, "frozen": False}, ensure_ascii=False),
        after=json.dumps({"pkg_id": pkg_id, "frozen": True,
                          "frozen_at": result.get("frozen_at"),
                          "intact": result.get("intact")}, ensure_ascii=False),
        reason=reason,
        ts=now,
    )
    db.commit()
    return ok(result)


@router.get("/{pkg_id}/export", dependencies=[Depends(require_admin)])
def export_evidence(pkg_id: int, fmt: str = Query(default="json", pattern="^(json|text)$"), db=Depends(get_db)) -> dict:
    """导出证据包（json 结构化 / text 可读文本）。写操作需 admin（P1-1）。"""
    return ok(_service.export(db, pkg_id, fmt))


@router.get("/{pkg_id}/report-template")
def report_template(pkg_id: int, kind: str = Query(default="96110", pattern="^(96110|platform|pibyao)$"), db=Depends(get_db)) -> dict:
    """举报/辟谣模板（仅个人署名 + 处置引导，D7 不处置不冒充官方；公开只读）。"""
    return ok(_service.report_template(db, pkg_id, kind))