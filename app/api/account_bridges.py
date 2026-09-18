"""账号桥接端点（P9，前缀 /account-bridges，统一封包 {ok,data}|{ok:false,error}）。

对应设计《PLAN-配置账号与私信接入.md》第三节路由表：
- 读端点（矩阵/详情/agent-import）需 admin key（D6：X-API-Key 或 Bearer）
- 敏感写端点（config/test/ingest/delete）require_admin，写操作落 gate_logs 哈希链审计
- 未知平台 404 unknown_platform + 枚举错误码（not_configurable/not_configured/field_required/...）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import require_admin
from ..services.account_bridge import AccountBridgeService
from ..utils import ApiError, ok

router = APIRouter(prefix="/account-bridges", tags=["account-bridges"])


def _service(settings: Settings, db) -> AccountBridgeService:
    return AccountBridgeService(settings=settings, conn=db)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class SaveConfigRequest(BaseModel):
    fields: dict[str, str] = Field(description="平台配置字段（config_fields 白名单内）")
    reason: str = Field(default="配置账号桥接", max_length=200, description="审计理由（gate_logs）")


class TestRequest(BaseModel):
    reason: str = Field(default="测试账号桥接", max_length=200, description="审计理由（可选）")


class IngestDMRequest(BaseModel):
    """私信接入 webhook body（桥接器调用）。字段名 from 为 Python 关键字，用别名。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: str | None = Field(default=None, alias="from", max_length=120,
                              description="对方账号标识（知乎 url_token / 微信昵称 / QQ 号 / TG 用户名等）")
    text: str = Field(min_length=1, max_length=200_000, description="私信原文")
    dm_id: str | None = Field(default=None, max_length=120, description="桥接器侧消息 id（去重用，可选）")
    payload: dict | None = Field(default=None, description="桥接器附加载荷（原样存入事件，可选）")


class DeleteConfigRequest(BaseModel):
    reason: str = Field(default="删除账号桥接配置", max_length=200, description="审计理由（可选）")


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("", dependencies=[Depends(require_admin)])
def list_matrix(
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """平台矩阵：7 平台调研结论（名称/AI 可接入/接入方式/开源项目/状态），不含任何明文凭据。"""
    return ok(_service(settings, db).list_matrix())


@router.get("/{platform}", dependencies=[Depends(require_admin)])
def get_detail(
    platform: str,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """单平台详情：矩阵字段 + config_fields（动态表单） + has_config（不含明文）。"""
    return ok(_service(settings, db).get_detail(platform))


@router.put("/{platform}/config", dependencies=[Depends(require_admin)])
def save_config(
    platform: str,
    payload: SaveConfigRequest,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """保存配置：敏感字段（cookie/token/password/secret）Fernet 双层加密落库，status=configured，
    写 events(account_bridge_configured) + gate_logs 审计。"""
    return ok(_service(settings, db).save_config(platform, payload.fields, reason=payload.reason))


@router.post("/{platform}/test", dependencies=[Depends(require_admin)])
def test(
    platform: str,
    payload: TestRequest | None = None,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """测试连接：结构校验 + URL/token 的 http GET 连通（timeout 5s）；cookie 仅解密+解析。
    返回 {ok, detail, latency_ms}；失败 status='error'。"""
    result = _service(settings, db).test(platform)
    if not result["ok"]:
        raise ApiError("test_failed", result.get("detail", "连接测试失败"), 400)
    return ok({k: v for k, v in result.items() if k != "ok"})


@router.post("/{platform}/ingest", dependencies=[Depends(require_admin)])
def ingest_dm(
    platform: str,
    payload: IngestDMRequest,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """私信接入核心 webhook：桥接器（WeChatFerry/OneBot/长轮询等）推送私信 →
    detections(pending) + account_intel upsert + events(dm_received) → 反向侦察流水线。"""
    return ok(_service(settings, db).ingest_dm(
        platform,
        from_name=payload.from_,
        text=payload.text,
        dm_id=payload.dm_id,
        payload=payload.payload,
    ))


@router.get("/{platform}/agent-import", dependencies=[Depends(require_admin)])
def agent_import(
    platform: str,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """本地 agent（DSH/Claude Code/Codex）一键导入配置：MCP streamable-http + 环境变量 + 桥脚本提示。"""
    return ok(_service(settings, db).agent_import(platform))


@router.delete("/{platform}", dependencies=[Depends(require_admin)])
def delete_config(
    platform: str,
    payload: DeleteConfigRequest | None = None,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """删除配置：清空 config_enc、status=unconfigured、写事件 + gate_logs 审计（凭据不可恢复）。"""
    reason = payload.reason if payload else "删除账号桥接配置"
    return ok(_service(settings, db).delete_config(platform, reason=reason))
