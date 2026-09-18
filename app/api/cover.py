"""反钓鱼伪装 API（P19）：/api/v1/cover —— 伪装身份 + 接触事件 + 伪装地图。

对应《蜜罐反钓鱼伪装》设计 §四：全部端点 require_admin（D6）；统一
{ok,data}/{ok:false,error:{code,message}} 封包（MCP 透传协议 §7.1）。
toggle/delete 为敏感操作（§11）：confirm=true + reason≥20 双确认，
成功后写 gate_logs 哈希链审计；暴露/接触事件落 events 可审计。
合规（设计 §六）：假信息仅用于防御验证（HITL，暴露由用户手动触发）；
真实骗子号码脱敏 + IOC 掩码，明文不落库。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.audit_log import write_gate_log
from ..services.cover import CoverIdentityService
from ..utils import ApiError, ok

router = APIRouter(prefix="/cover", tags=["cover"])

REASON_MIN_LEN = 20
_service = CoverIdentityService()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _require_confirm(confirm: bool, reason: str) -> str:
    """敏感操作闸控：confirm=true 且 reason.strip()≥20 字，否则 403/422。返回去空格后的 reason。"""
    if not confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = reason.strip()
    if len(reason) < REASON_MIN_LEN:
        raise ApiError(
            "reason_too_short",
            f"reason 至少 {REASON_MIN_LEN} 字，当前 {len(reason)} 字",
            422,
        )
    return reason


class IdentityCreateRequest(BaseModel):
    """创建伪装入参：{name, persona_type?, parent_id?, avatar_desc?, bio_desc?,
    fake_phone?, fake_wechat?, fake_qq?, fake_email?, expose_strategy?}。"""

    name: str = Field(min_length=1, max_length=100)
    persona_type: str | None = None
    parent_id: int | None = Field(default=None, ge=1)
    avatar_desc: str | None = Field(default=None, max_length=500)
    bio_desc: str | None = Field(default=None, max_length=1000)
    fake_phone: str | None = Field(default=None, max_length=50)
    fake_wechat: str | None = Field(default=None, max_length=100)
    fake_qq: str | None = Field(default=None, max_length=50)
    fake_email: str | None = Field(default=None, max_length=200)
    expose_strategy: str | None = None


class ConfirmReasonRequest(BaseModel):
    """敏感操作双确认：{confirm:true, reason:≥20 字}。"""

    confirm: bool = False
    reason: str = Field(default="")


class ContactRegisterRequest(BaseModel):
    """登记骗子接触入参：{identity_id, contact_type, contact_value, trap_id?, det_id?}。"""

    identity_id: int = Field(ge=1)
    contact_type: str = Field(min_length=1)
    contact_value: str = Field(min_length=1, max_length=500)
    trap_id: int | None = Field(default=None, ge=1)
    det_id: int | None = Field(default=None, ge=1)


# ---------------------------------------------------------------------------
# 伪装身份
# ---------------------------------------------------------------------------

@router.post("/identities", dependencies=[Depends(require_admin)])
def cover_create_identity(payload: IdentityCreateRequest, db=Depends(get_db)) -> dict:
    """创建伪装身份（layer 自动=parent 层+1；name 唯一校验 409）。"""
    return ok({"identity": _service.create_identity(payload.model_dump(), db)})


@router.get("/identities", dependencies=[Depends(require_admin)])
def cover_list_identities(db=Depends(get_db)) -> dict:
    """全部伪装身份（layer,id 升序），含层数/启用态/被接触次数。"""
    return ok({"identities": _service.list_identities(db)})


@router.post("/identities/{identity_id}/toggle", dependencies=[Depends(require_admin)])
def cover_toggle_identity(
    identity_id: int,
    payload: ConfirmReasonRequest | None = None,
    db=Depends(get_db),
) -> dict:
    """翻转伪装启停（敏感操作：confirm+reason≥20 + gate_logs 审计）。"""
    if payload is None:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _service.get_identity(identity_id, db)
    updated = _service.toggle(identity_id, db)
    write_gate_log(
        db,
        action="cover.identity.toggle",
        payload_json=json.dumps(
            {"action": "cover.identity.toggle", "identity_id": identity_id,
             "name": before["name"], "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps({"enabled": before["enabled"]}, ensure_ascii=False),
        after=json.dumps({"enabled": updated["enabled"]}, ensure_ascii=False),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok({"identity": updated})


@router.delete("/identities/{identity_id}", dependencies=[Depends(require_admin)])
def cover_delete_identity(
    identity_id: int,
    payload: ConfirmReasonRequest | None = None,
    db=Depends(get_db),
) -> dict:
    """删除伪装（敏感操作：confirm+reason≥20 + gate_logs 审计；下层 parent 置空）。"""
    if payload is None:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = _require_confirm(payload.confirm, payload.reason)
    before = _service.get_identity(identity_id, db)
    result = _service.delete(identity_id, db)
    write_gate_log(
        db,
        action="cover.identity.delete",
        payload_json=json.dumps(
            {"action": "cover.identity.delete", "identity_id": identity_id,
             "name": before["name"], "reason": reason},
            ensure_ascii=False, sort_keys=True,
        ),
        before=json.dumps(
            {"name": before["name"], "persona_type": before["persona_type"],
             "layer": before["layer"]},
            ensure_ascii=False, sort_keys=True,
        ),
        after=json.dumps({"deleted": True}, ensure_ascii=False),
        reason=reason,
        ts=_now(),
    )
    db.commit()
    return ok(result)


@router.post("/identities/{identity_id}/expose", dependencies=[Depends(require_admin)])
def cover_expose_identity(identity_id: int, db=Depends(get_db)) -> dict:
    """主动暴露：登记暴露事件，返回假信息脱敏清单（供回复骗子时"泄露"）。"""
    return ok({"exposure": _service.expose(identity_id, db)})


# ---------------------------------------------------------------------------
# 接触事件 / 地图
# ---------------------------------------------------------------------------

@router.post("/contact", dependencies=[Depends(require_admin)])
def cover_register_contact(payload: ContactRegisterRequest, db=Depends(get_db)) -> dict:
    """登记骗子接触：脱敏落库 + IOC 提取 + 同值多次锁定 + 蜜饵/检测关联。"""
    return ok({"event": _service.register_contact(
        payload.identity_id, payload.contact_type, payload.contact_value,
        payload.trap_id, payload.det_id, db)})


@router.get("/contacts", dependencies=[Depends(require_admin)])
def cover_contacts(
    identity_id: int | None = Query(default=None, ge=1),
    contact_type: str | None = Query(default=None),
    escalated: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db=Depends(get_db),
) -> dict:
    """接触记录（id 倒序）：按身份/类型/是否锁定过滤。"""
    return ok({"contacts": _service.contacts(
        db, identity_id=identity_id, contact_type=contact_type,
        escalated=escalated, limit=limit)})


@router.get("/map", dependencies=[Depends(require_admin)])
def cover_map(db=Depends(get_db)) -> dict:
    """伪装嵌套图：identity→layer→parent + 每层被接触数。"""
    return ok(_service.cover_map(db))
