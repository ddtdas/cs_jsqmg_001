"""拟真账号 API（P11）：generate / schedule / 查询 / 启停 / 立即发布。

依赖服务层 app/services/persona_sim.py（PersonaSimService：模板池生成 +
persona_schedules / persona_posts 计划调度，两张表已由 ensure_schema 建好）。
全部端点 require_admin（D6）；统一 {ok,data}/{ok:false,error:{code,message}}
封包（MCP 透传协议 §7.1）。合规边界（D7 最小化）：只对拟真账号生成内容，
不采集真实用户数据。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.persona_sim import PersonaSimService
from ..utils import ApiError, ok

router = APIRouter(prefix="/persona", tags=["persona"])


class PersonaGenerateRequest(BaseModel):
    """生成拟真动态入参（可选）：{type}（life/work/study/social/hobby，缺省随机）。"""

    type: str | None = None


class PersonaScheduleRequest(BaseModel):
    """新建发布计划入参（必填）：{account, type, interval_minutes, total}。

    P2：interval_minutes ∈ [1,10080]，total ∈ [1,1000]，account/type 非空；
    <=0 / 空串由 pydantic 校验拒绝 → 422 validation_error。
    """

    account: str = Field(min_length=1)
    type: str = Field(min_length=1)
    interval_minutes: int = Field(ge=1, le=10080)
    total: int = Field(ge=1, le=1000)


class PersonaPublishNowRequest(BaseModel):
    """立即发布入参（必填）：{schedule_id}。"""

    schedule_id: int


@router.post("/generate", dependencies=[Depends(require_admin)])
def persona_generate(payload: PersonaGenerateRequest) -> dict:
    """随机生成一条拟真动态：type 可选（缺省/不在模板池时自动随机选类），返回 {post}。"""
    work = PersonaSimService().generate_work(payload.type)
    return ok({"post": work})


@router.post("/schedule", dependencies=[Depends(require_admin)])
def persona_schedule(payload: PersonaScheduleRequest, db=Depends(get_db)) -> dict:
    """新建发布计划（next_run_at=now，enabled=1），返回 {schedule}。"""
    svc = PersonaSimService()
    return ok({"schedule": svc.schedule(
        payload.account, payload.type, payload.interval_minutes, payload.total, db)})


@router.get("/schedules", dependencies=[Depends(require_admin)])
def persona_schedules(db=Depends(get_db)) -> dict:
    """全部发布计划（id 升序），返回 {schedules}。"""
    return ok({"schedules": PersonaSimService().list_schedules(db)})


@router.get("/posts", dependencies=[Depends(require_admin)])
def persona_posts(account: str | None = None, db=Depends(get_db)) -> dict:
    """已发布动态：query account 可选（缺省返回全部，最新在前），返回 {posts}。"""
    svc = PersonaSimService()
    if account:
        posts = svc.list_posts(account.strip(), db)
    else:
        rows = db.execute("SELECT * FROM persona_posts ORDER BY id DESC").fetchall()
        posts = [dict(r) for r in rows]
    return ok({"posts": posts})


def _set_enabled(schedule_id: int, enabled: int, db) -> dict:
    try:
        row = PersonaSimService().set_enabled(schedule_id, enabled, db)
    except ValueError:
        raise ApiError("schedule_not_found", f"发布计划不存在: id={schedule_id}", 404)
    return ok({"schedule": row})


@router.post("/schedules/{schedule_id}/pause", dependencies=[Depends(require_admin)])
def persona_pause(schedule_id: int, db=Depends(get_db)) -> dict:
    """暂停发布计划（enabled=0），返回 {schedule}。"""
    return _set_enabled(schedule_id, 0, db)


@router.post("/schedules/{schedule_id}/resume", dependencies=[Depends(require_admin)])
def persona_resume(schedule_id: int, db=Depends(get_db)) -> dict:
    """恢复发布计划（enabled=1），返回 {schedule}。"""
    return _set_enabled(schedule_id, 1, db)


@router.post("/publish-now", dependencies=[Depends(require_admin)])
def persona_publish_now(payload: PersonaPublishNowRequest, db=Depends(get_db)) -> dict:
    """手动立即发布一条并推进计划，返回 {post}（含发布记录与计划最新状态）。"""
    try:
        post = PersonaSimService().publish_now(payload.schedule_id, db)
    except ValueError:
        raise ApiError("schedule_not_found", f"发布计划不存在: id={payload.schedule_id}", 404)
    return ok({"post": post})


# ---------------------------------------------------------------------------
# P16：实战化应对层 —— 应对规则 CRUD / respond / 实战演练（drills / drill）
# ---------------------------------------------------------------------------
class PersonaReplyCreateRequest(BaseModel):
    """新增应对规则入参（必填）：{account, trigger_keyword, reply_type, content}。

    reply_type 缺省 'stall'；trigger_keyword 为 substring 匹配关键词
    （如 '投资'、'刷单'、'加微信'、'安全账户'）。
    """

    account: str = Field(min_length=1)
    trigger_keyword: str = Field(min_length=1)
    reply_type: str = Field(default="stall")
    content: str = Field(min_length=1)


class PersonaRespondRequest(BaseModel):
    """实战应对入参（必填）：{account, incoming_text}——骗子私信/评论原文。"""

    account: str = Field(min_length=1)
    incoming_text: str = Field(min_length=1)


class PersonaDrillRequest(BaseModel):
    """实战演练入参（必填 scenario；可选 incoming_text 提取 IOC）。"""

    scenario: str = Field(min_length=1)
    incoming_text: str | None = None


@router.post("/replies", dependencies=[Depends(require_admin)])
def persona_reply_create(payload: PersonaReplyCreateRequest, db=Depends(get_db)) -> dict:
    """新增应对规则（trigger_keyword substring 匹配 incoming_text），返回 {reply_id}。"""
    reply_id = PersonaSimService().add_reply(
        payload.account, payload.trigger_keyword, payload.reply_type, payload.content, db
    )["reply_id"]
    return ok({"reply_id": reply_id})


@router.get("/replies", dependencies=[Depends(require_admin)])
def persona_reply_list(db=Depends(get_db)) -> dict:
    """全部应对规则（id 升序），返回 {replies}。"""
    return ok({"replies": PersonaSimService().list_replies(db)})


@router.delete("/replies/{reply_id}", dependencies=[Depends(require_admin)])
def persona_reply_delete(reply_id: int, db=Depends(get_db)) -> dict:
    """删除应对规则；不存在抛 404 reply_not_found，返回 {deleted:true}。"""
    if not PersonaSimService().delete_reply(reply_id, db):
        raise ApiError("reply_not_found", f"应对规则不存在: id={reply_id}", 404)
    return ok({"deleted": True})


@router.post("/replies/{reply_id}/toggle", dependencies=[Depends(require_admin)])
def persona_reply_toggle(reply_id: int, db=Depends(get_db)) -> dict:
    """翻转应对规则启停状态（0<->1），返回 {enabled}。"""
    row = db.execute("SELECT enabled FROM persona_replies WHERE id=?", (reply_id,)).fetchone()
    if row is None:
        raise ApiError("reply_not_found", f"应对规则不存在: id={reply_id}", 404)
    enabled = 0 if row["enabled"] else 1
    updated = PersonaSimService().set_reply_enabled(reply_id, enabled, db)
    return ok({"enabled": updated["enabled"]})


@router.post("/respond", dependencies=[Depends(require_admin)])
def persona_respond(payload: PersonaRespondRequest, db=Depends(get_db)) -> dict:
    """实战应对：匹配启用规则生成回复 + 提取 IOC，返回 {result}。"""
    return ok({"result": PersonaSimService().respond(
        payload.account, payload.incoming_text, db)})


@router.get("/drills", dependencies=[Depends(require_admin)])
def persona_drills() -> dict:
    """内置 4 类实战演练剧本列表，返回 {drills}。"""
    return ok(PersonaSimService().drills())


@router.post("/drill", dependencies=[Depends(require_admin)])
def persona_drill(payload: PersonaDrillRequest, db=Depends(get_db)) -> dict:
    """实战演练：按 scenario 返回建议话术/警告；可选 incoming_text 提取 IOC。"""
    try:
        result = PersonaSimService().drill(payload.scenario, payload.incoming_text, db)
    except ValueError:
        raise ApiError("drill_scenario_unknown", f"未知演练场景: {payload.scenario}", 404)
    return ok({"result": result})
