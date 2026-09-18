"""知乎通道端点：健康 / 登录（cookie 注入）/ 校验 / 状态 / 手动导入。

对应主方案 §4.2 zhihu.py 路由表与 MCP af_zhihu_channels / af_zhihu_login /
af_zhihu_verify 透传目标（D1：MCP 工具 = 本 REST 端点经 httpx 透传）。

安全（D6）：cookie 注入/校验与手动导入为敏感操作，require_admin；
健康/状态为只读公开（对齐 /system/health 探活风格）。
合规（D7 / §5.4）：cookie 注入后 Fernet 加密落库，明文不落库、不回显。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import require_admin
from ..services.zhihu_bridge import CHANNELS, ZhihuBridge
from ..utils import ApiError, ok

router = APIRouter(prefix="/zhihu", tags=["zhihu"])


def _bridge(settings: Settings, db) -> ZhihuBridge:
    """构造适配器实例：settings + 请求级 DB 连接（事务由 get_db 管理）。"""
    return ZhihuBridge(settings=settings, conn=db)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    cookie: str = Field(min_length=1, max_length=64_000, description="知乎登录态 cookie")
    scheme: str = Field(
        default="header",
        pattern="^(header|json)$",
        description="cookie 格式：header=『name=value; ...』字符串；json=浏览器导出 JSON",
    )


class ImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000, description="粘贴的对话文本或 JSON")
    source: str = Field(
        default="manual",
        pattern="^(zhihu|import|manual)$",
        description="来源标记：zhihu/import/manual",
    )
    from_url_name: str = Field(default="", max_length=120, description="对方知乎 url_token（可空）")


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("/channels")
def channels(
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """四通道健康：CH-A/CH-B/CH-C/CH-D（对齐 MCP af_zhihu_channels）。"""
    import asyncio

    bridge = _bridge(settings, db)
    healths = asyncio.run(bridge.health())
    return ok({"channels": [h.to_dict() for h in healths]})


@router.post("/channels/{channel}/login", dependencies=[Depends(require_admin)])
def login(
    channel: str,
    payload: LoginRequest,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """注入并加密存储 cookie（HITL：cookie 由用户从自己浏览器导出后粘贴，系统不代登录）。

    仅 ch-a / ch-b 需要登录态；cookie 经 Fernet 加密后写入 zhihu_sessions.cookie_enc。
    """
    _validate_channel(channel, need_cookie=True)
    bridge = _bridge(settings, db)
    try:
        result = bridge.set_cookie(channel, payload.cookie, scheme=payload.scheme)
    except ValueError as exc:
        raise ApiError("invalid_cookie", f"cookie 解析失败: {exc}", 422) from exc
    return ok(result)


@router.post("/channels/{channel}/verify", dependencies=[Depends(require_admin)])
def verify(
    channel: str,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """校验已存 cookie（结构校验：解密+解析+关键字段；登录态有效性由实际抓取探明）。"""
    _validate_channel(channel)
    bridge = _bridge(settings, db)
    result = bridge.verify_cookie(channel)
    if not result["valid"]:
        raise ApiError("verify_failed", result.get("note", "cookie 校验失败"), 400)
    return ok(result)


@router.get("/status")
def status(
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """通道综合状态：限速器水位 / 退避冷却 / 会话（含是否已注入 cookie，不含明文）。"""
    bridge = _bridge(settings, db)
    return ok(bridge.status_snapshot())


@router.post("/import", dependencies=[Depends(require_admin)])
def import_manual(
    payload: ImportRequest,
    settings: Settings = Depends(get_settings),
    db=Depends(get_db),
) -> dict:
    """CH-D 手动导入：粘贴文本/JSON → 写入 detections（status='pending'），
    由 P3 检测流水线（/scan）消费。合规：只读自有/公开内容；不自动发布任何东西。
    """
    bridge = _bridge(settings, db)
    try:
        result = bridge.ingest_manual(
            payload.text, source=payload.source, from_url_name=payload.from_url_name
        )
    except ValueError as exc:
        raise ApiError("empty_text", str(exc), 422) from exc
    return ok(result)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _validate_channel(channel: str, need_cookie: bool = False) -> None:
    if channel not in CHANNELS:
        raise ApiError("unknown_channel", f"未知通道 {channel!r}（可选 {', '.join(CHANNELS)}）", 404)
    if need_cookie and channel not in ("ch-a", "ch-b"):
        raise ApiError(
            "no_cookie_channel",
            f"{channel} 无需登录态（只读公开/手动导入），仅 ch-a/ch-b 支持 cookie 注入",
            422,
        )