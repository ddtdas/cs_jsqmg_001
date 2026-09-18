"""采集矩阵端点：矩阵 CRUD + 立即执行 + 技术栈调研。

前缀 /collector，统一封包 {ok,data}|{ok:false,error}；读写均 require_admin
（采集涉及真实网络与账号，属敏感操作；与 account-bridges 同级保护）。

对应 MCP 透传层可映射 af_collector_matrix / af_collector_run 等（D1：只透传不写业务逻辑）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import require_admin
from ..services.collector import MATRIX_PLATFORMS, CollectorService
from ..utils import ok

router = APIRouter(prefix="/collector", tags=["collector"])


def _svc(settings: Settings, db) -> CollectorService:
    return CollectorService(settings=settings, conn=db)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class StrategyModel(BaseModel):
    collect: str = Field(default="dm", pattern="^(dm|comments|posts|follows)$")
    frequency: str = Field(default="hourly", pattern="^(minute|hourly|daily)$")
    depth: int = Field(default=5, ge=1, le=100)
    keyword: str = Field(default="", max_length=200)
    near_dup: float = Field(default=0.85, ge=0.5, le=1.0)


class AddCellRequest(BaseModel):
    platform: str = Field(min_length=1, max_length=32)
    account: str = Field(default="", max_length=120)
    strategy: StrategyModel = Field(default_factory=StrategyModel)


class UpdateCellRequest(BaseModel):
    strategy: StrategyModel | None = None
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/matrix", dependencies=[Depends(require_admin)])
def list_matrix(settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok({"cells": _svc(settings, db).list_matrix()})


@router.post("/matrix", dependencies=[Depends(require_admin)])
def add_cell(payload: AddCellRequest, settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok(_svc(settings, db).add_cell(payload.platform, payload.account, payload.strategy.model_dump()))


@router.put("/matrix/{cell_id}", dependencies=[Depends(require_admin)])
def update_cell(cell_id: int, payload: UpdateCellRequest, settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok(_svc(settings, db).update_cell(cell_id, payload.strategy.model_dump() if payload.strategy else None, payload.enabled))


@router.delete("/matrix/{cell_id}", dependencies=[Depends(require_admin)])
def remove_cell(cell_id: int, settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok(_svc(settings, db).remove_cell(cell_id))


@router.post("/matrix/{cell_id}/run", dependencies=[Depends(require_admin)])
def run_cell(cell_id: int, settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok(_svc(settings, db).run_cell(cell_id))


@router.post("/matrix/run-all", dependencies=[Depends(require_admin)])
def run_all(settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok(_svc(settings, db).run_all())


@router.get("/tech-stack", dependencies=[Depends(require_admin)])
def tech_stack(settings: Settings = Depends(get_settings), db=Depends(get_db)) -> dict:
    return ok({"stack": _svc(settings, db).tech_stack()})


# 供前端诊断：暴露允许的平台集合
@router.get("/platforms", dependencies=[Depends(require_admin)])
def platforms() -> dict:
    return ok({"platforms": list(MATRIX_PLATFORMS)})
