"""分级端点：levels / explain。

对应主方案 §4.2 grading.py 与 MCP af_grade_explain 透传目标。
explain 返回可解释证据链（P5 DoD 核心）；蜜饵/账号上下文经查询参数注入（可选）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..db import get_db
from ..services.grading import GRADE_LEVELS, GradingService
from ..utils import ok

router = APIRouter(prefix="/grading", tags=["grading"])


@router.get("/levels")
def grading_levels() -> dict:
    """五级分级档位定义。"""
    return ok(GRADE_LEVELS)


@router.get("/explain/{detection_id}")
def grading_explain(
    detection_id: int,
    trap_hit_count: int = Query(default=0, ge=0, description="关联蜜饵实锤命中次数（可选上下文）"),
    account_risk: str | None = Query(
        default=None, pattern="^(green|yellow|red)$", description="关联账号风险等级（可选上下文）"),
    db=Depends(get_db),
) -> dict:
    """某次检测的五级分级与可解释证据链（实时重算，不落库）。"""
    return ok(GradingService().grade_detection(
        db, detection_id, trap_hit_count=trap_hit_count, account_risk=account_risk))