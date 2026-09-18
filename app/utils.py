"""统一 API 封包与异常处理。

双形态同一内核（D1）：{ok,data}|{ok:false,error:{code,message}} 是 MCP 透传层的
解包协议（主方案 §7.1），必须严格一致 —— 所有 REST 响应（含 4xx/5xx 错误）都走
本模块的封包与异常处理器。
"""

from __future__ import annotations

import logging
import sqlite3
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def setup_logging(log_dir: str | Path | None = None) -> logging.Logger:
    """应用日志初始化（P2-6）：控制台 + var/logs/app.log 滚动文件（5MB×3）。幂等。

    log_dir 缺省为项目根 var/logs/（对齐主方案 §10 目录结构）；文件不可写时仅控制台。
    """
    logger = logging.getLogger("canary")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    try:
        base = Path(__file__).resolve().parent.parent
        d = Path(log_dir) if log_dir else (base / "var" / "logs")
        d.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(str(d / "app.log"), maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass  # 文件不可写时仅控制台输出
    return logger


def get_logger(name: str = "canary") -> logging.Logger:
    return logging.getLogger(name)


class ApiError(Exception):
    """业务异常：携带对外可见的 code/message 与 HTTP 状态码。"""

    def __init__(
        self,
        code: str = "internal_error",
        message: str = "内部错误",
        status_code: int = 500,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def ok(data: Any = None) -> dict:
    """成功封包：{ok:true, data}。"""
    return {"ok": True, "data": data}


def err(code: str, message: str) -> dict:
    """失败封包：{ok:false, error:{code, message}}。"""
    return {"ok": False, "error": {"code": code, "message": message}}


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=err(code, message),
    )


def register_error_handlers(app: FastAPI) -> None:
    """把 FastAPI/Starlette 的默认错误响应统一为 {ok:false,error} 封包。

    P0-E3：validation_error 不再回显请求体 input/ctx（防 admin key 等敏感输入外泄）；
    P1-B7：sqlite3 绑定溢出 OverflowError → 422 invalid_integer（不 500、不含类名）；
    P1-D4：database is locked → 503 db_busy（不 500/不误报 404）；
    P2-E3：500 通用文案不再拼接异常类名（类名/堆栈只进日志）。
    """

    @app.exception_handler(ApiError)
    async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 401/403 统一对外为 unauthorized / forbidden，保持封包结构稳定
        code = {
            401: "unauthorized",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
        }.get(exc.status_code, "http_error")
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # P0-E3：仅保留 loc/msg/type，剥离 input/ctx（可能含请求体明文，如 admin key）
        items = []
        for e in exc.errors()[:1]:
            items.append({k: e[k] for k in ("loc", "msg", "type") if k in e})
        return _error_response(
            422,
            "validation_error",
            str(items) if items else "请求参数校验失败",
        )

    @app.exception_handler(OverflowError)
    async def _overflow_error_handler(_: Request, exc: OverflowError) -> JSONResponse:
        # P1-B7：超 int64 整数绑定 sqlite3 抛 OverflowError → 422（含错误码，不含类名）
        return _error_response(
            422, "invalid_integer", "整数参数超出允许范围（最大 9223372036854775807）"
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def _db_operational_handler(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        # P1-D4：database is locked / busy → 503 明确错误码
        msg = str(exc)
        if "locked" in msg.lower() or "busy" in msg.lower():
            return _error_response(503, "db_busy", "数据库繁忙，请稍后重试")
        return _error_response(500, "internal_error", "服务器内部错误")

    @app.exception_handler(sqlite3.InterfaceError)
    async def _db_interface_handler(_: Request, exc: sqlite3.InterfaceError) -> JSONResponse:
        return _error_response(500, "internal_error", "服务器内部错误")

    @app.exception_handler(sqlite3.DatabaseError)
    async def _db_error_handler(_: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        # 兜底：其他 sqlite 异常（如并发下的 DatabaseError）一律通用文案，不泄露类名
        get_logger().error("数据库异常 %s: %s", type(exc).__name__, exc, exc_info=exc)
        return _error_response(500, "internal_error", "服务器内部错误")

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
        # P2-6：未捕获异常带堆栈落盘（var/logs/app.log）；P2-E3：对外不泄露类名/内部细节
        get_logger().error(
            "未捕获异常 %s: %s", type(exc).__name__, exc, exc_info=exc
        )
        return _error_response(500, "internal_error", "服务器内部错误")
