"""FastAPI 依赖：get_settings / get_db / require_admin。

认证（D6）：bootstrap admin key 校验。支持两种携带方式：
  - Header: X-API-Key: af_admin_xxxxxxxx
  - Header: Authorization: Bearer af_admin_xxxxxxxx
key 优先级：环境变量 AF_API_KEY > data/bootstrap_admin_key.txt（首次启动自动生成）。
比较使用 secrets.compare_digest 常量时间比较，防时序侧信道。
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from fastapi import Depends, Header, Request

from .config import Settings, get_settings
from .utils import ApiError

# P1-A5：允许旧版 8-hex（32bit）与新版 32-hex（128bit）key 文件；上限 64-hex 兜底
_ADMIN_KEY_RE = re.compile(r"^af_admin_[0-9a-f]{8,64}$")


def bootstrap_key_path(settings: Settings) -> Path:
    """bootstrap admin key 文件路径：{data_dir}/bootstrap_admin_key.txt。"""
    from .db import data_dir

    return data_dir(settings) / "bootstrap_admin_key.txt"


def load_or_create_bootstrap_key(settings: Settings) -> str:
    """首次启动生成 admin key 并写入 data/；已存在则复用（幂等）。

    格式：af_admin_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx（32 位十六进制 = 128bit 熵，
    P1-A5）；旧版 8-hex（32bit）key 文件仍可读取（兼容迁移，旧 key 登录不受影响）。
    环境变量 AF_API_KEY 显式设置时直接采用，不再生成文件（仍会保留文件以便查询）。
    """
    if settings.api_key:
        return settings.api_key

    path = bootstrap_key_path(settings)
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if _ADMIN_KEY_RE.match(key):
            return key

    key = "af_admin_" + secrets.token_hex(16)  # 32 hex chars = 128 bit
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return key


def get_admin_key(settings: Settings) -> str:
    """读取当前生效的 admin key（env 优先，其次文件）。"""
    if settings.api_key:
        return settings.api_key
    path = bootstrap_key_path(settings)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def require_admin(
    request: Request,
    settings: Settings = Depends(get_settings),
    x_api_key: str | None = Header(default=None),
) -> str:
    """受保护端点依赖：校验 X-API-Key 或 Bearer，返回校验通过的 key。"""
    token: str | None = None
    if x_api_key:
        token = x_api_key.strip()
    else:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

    if not token:
        raise ApiError("missing_api_key", "缺少 API Key（X-API-Key 或 Bearer）", 401)

    expected = get_admin_key(settings)
    if not expected or not secrets.compare_digest(token, expected):
        raise ApiError("invalid_api_key", "API Key 无效", 401)
    return token


def is_admin_request(request: Request, settings: Settings | None = None) -> bool:
    """宽松判定：请求是否携带**有效** admin key（不抛错）。

    R4-F3：供公开热路径（scan/text、accounts/check）区分匿名与已认证调用——
    匿名调用降级为纯规则（scan 不送 LLM）与只读评估（accounts 不落库）；
    与 RateLimitMiddleware._valid_admin 口径一致（X-API-Key 或 Bearer，常量时间比较）。
    """
    settings = settings or get_settings()
    expected = get_admin_key(settings)
    if not expected:
        return False
    token = (request.headers.get("x-api-key", "") or "").strip()
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    return bool(token) and secrets.compare_digest(token, expected)
