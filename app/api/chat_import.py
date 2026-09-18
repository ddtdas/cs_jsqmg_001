"""聊天记录一键导入 API（P15）：candidates / scan / import / import-file / 清空导入。

依赖服务层 app/services/chat_import.py（ChatImportService：微信/QQ 库探测、
TXT/CSV/JSON 通用解析、DesensitizeService 脱敏、落库 detections(source='chat_import')、
SpeechEngine 推理链分级；clear_imported 写 gate_logs 哈希链审计）。
全部端点 require_admin（D6）；统一 {ok,data}/{ok:false,error:{code,message}}
封包（MCP 透传协议 §7.1）。合规边界（D7 最小化）：导入内容先脱敏再落库；
清空导入必须显式 confirm=true 且 reason≥20 字，避免误删审计证据。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..db import get_db
from ..deps import require_admin
from ..services.chat_import import ChatImportService
from ..utils import ApiError, ok

router = APIRouter(prefix="/chat-import", tags=["chat-import"])


class ChatScanRequest(BaseModel):
    """扫描入参（必填）：{path} —— 微信/QQ 目录或 TXT/CSV/JSON 文件路径。"""

    path: str = Field(min_length=1)


class ChatImportRequest(BaseModel):
    """导入入参（必填）：{path, limit?=2000}。"""

    path: str = Field(min_length=1)
    limit: int = 2000


class ChatImportFileRequest(BaseModel):
    """通用文本文件导入入参（必填）：{file_path, platform?}。"""

    file_path: str = Field(min_length=1)
    platform: str | None = None


class ChatClearRequest(BaseModel):
    """清空导入入参（必填）：{confirm:bool, reason:str}。"""

    confirm: bool = False
    reason: str = ""


@router.get("/candidates", dependencies=[Depends(require_admin)])
def chat_candidates() -> dict:
    """探测微信/QQ 常见根路径（不扫全盘），返回 {candidates:{wechat,qq,custom}}。"""
    return ok({"candidates": ChatImportService().candidates()})


@router.post("/scan", dependencies=[Depends(require_admin)])
def chat_scan(payload: ChatScanRequest) -> dict:
    """识别平台与消息库文件（含 SQLCipher 加密探测），返回 {result:{platform,db_files,encrypted}}。"""
    return ok({"result": ChatImportService().scan(payload.path)})


@router.post("/import", dependencies=[Depends(require_admin)])
async def chat_import(payload: ChatImportRequest, db=Depends(get_db)) -> dict:
    """一键导入：解析 → 脱敏 → 落库 detections(chat_import) → 推理链分级。

    返回 {result:{imported,suspicious,preview}}；加密库 → error:'encrypted_db'，
    其它异常防御式回填 error 字段（服务层兜底，绝不 500）。
    """
    result = await ChatImportService().import_messages(payload.path, payload.limit, db)
    return ok({"result": result})


@router.post("/import-file", dependencies=[Depends(require_admin)])
async def chat_import_file(payload: ChatImportFileRequest, db=Depends(get_db)) -> dict:
    """通用文本文件导入：parse_generic 预解析校验 → 复用 import_messages 全流程。

    返回 {result:{imported,suspicious,preview}}；无法解析 → 422 parse_failed。
    """
    platform = (payload.platform or "").strip().lower()
    if platform and platform not in ("generic", "wechat", "qq"):
        raise ApiError("unsupported_platform", f"不支持的平台：{payload.platform}", 422)
    svc = ChatImportService()
    try:
        parsed = svc.parse_generic(payload.file_path)
    except Exception as exc:
        raise ApiError("parse_failed", f"解析失败：{exc}", 422)
    if not parsed:
        return ok({"result": {"imported": 0, "suspicious": 0, "preview": [],
                              "message": "文件中没有可导入的消息内容"}})
    result = await svc.import_messages(payload.file_path, conn=db)
    return ok({"result": result})


@router.delete("/imported", dependencies=[Depends(require_admin)])
def chat_clear_imported(payload: ChatClearRequest, db=Depends(get_db)) -> dict:
    """清空 chat_import 检测记录（写 gate_logs 审计），返回 {deleted:int}。

    confirm!=true → 403 confirm_required；reason < 20 字 → 422 reason_too_short。
    """
    if not payload.confirm:
        raise ApiError("confirm_required", "必须显式确认（confirm=true）才能清空导入记录", 403)
    reason = payload.reason.strip()
    if len(reason) < 20:
        raise ApiError("reason_too_short", "清空原因至少 20 字", 422)
    deleted = ChatImportService().clear_imported(db, reason)
    return ok({"deleted": deleted})