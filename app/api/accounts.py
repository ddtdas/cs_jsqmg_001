"""账号速查端点（R3）：/api/v1/accounts/check、/{url_name}、/{id}/timeline。

对应主方案 §4.2 accounts.py 与 MCP af_account_check 透传目标。
只用公开/自有信号（D7），输入信号经白名单过滤。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import is_admin_request
from ..services.account_intel import AccountIntelService
from ..utils import ok

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountCheckRequest(BaseModel):
    url_name: str = Field(min_length=1, max_length=200, description="知乎 url_token/昵称")
    signals: dict | None = Field(
        default=None,
        description="公开信号（白名单过滤）：registered_at/activity_level/content_consistency/reported_count/trap_hit_ids 等",
    )


@router.post("/check")
def account_check(payload: AccountCheckRequest, request: Request, db=Depends(get_db)) -> dict:
    """账号风险速查：绿黄红 + 证据链 + 共现账号。

    R4-F3：匿名调用（无有效 admin key）→ 只读评估不落库（persist=False，
    防匿名灌入账号库；已有档案仍可读）；已认证调用 → 维持画像 upsert。
    """
    return ok(AccountIntelService().check(
        db, payload.url_name, payload.signals, persist=is_admin_request(request)
    ))


@router.get("/{url_name}")
def get_account(url_name: str, db=Depends(get_db)) -> dict:
    """查询已建档账号画像。"""
    return ok(AccountIntelService().get_account(db, url_name))


@router.get("/{account_id}/timeline")
def account_timeline(account_id: int, db=Depends(get_db)) -> dict:
    """账号时间线（画像更新 + 踩饵命中记录）。"""
    return ok(AccountIntelService().timeline(db, account_id))