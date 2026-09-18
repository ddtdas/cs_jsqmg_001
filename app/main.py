"""FastAPI 主控入口。

对应主方案 §9 P0：lifespan 引导（数据目录 / bootstrap key / 建表钩子 / 调度器）
+ /api/v1 路由挂载 + 统一错误封包 + WebUI 静态托管（/ui/，P6.5）。
P2-6：logging 初始化 + 请求日志中间件；P2-2：APScheduler 9 类 job 启停。

形态关系（D1）：WebUI、MCP、curl 都是本 REST API 的客户端；
MCP Server 只做 httpx 透传，不在此引入第二套逻辑。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import __version__
from .api import chat_import as api_chat_import
from .api import accounts as api_accounts
from .api import account_bridges as api_account_bridges
from .api import alerts as api_alerts
from .api import auth as api_auth
from .api import cases as api_cases
from .api import collector as api_collector
from .api import cover as api_cover
from .api import config as api_config
from .api import detections as api_detections
from .api import events as api_events
from .api import evidence as api_evidence
from .api import grading as api_grading
from .api import persona_sim as api_persona
from .api import scan as api_scan
from .api import soc_lib as api_soc_lib
from .api import supply_chain as api_supply_chain
from .api import speech_patterns as api_speech_patterns
from .api import knowledge as api_knowledge
from .api import system as api_system
from .api import traps as api_traps
from .api import zhihu as api_zhihu
from .config import get_settings
from .db import data_dir, ensure_schema
from .deps import load_or_create_bootstrap_key
from .middleware.rate_limit import RateLimitMiddleware
from .services.scheduler import create_scheduler
from .utils import err, get_logger, register_error_handlers, setup_logging

_log = get_logger("canary.http")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动引导（对齐 Mellivora bootstrap 流程）：
    1. 确保 data 目录存在
    2. 生成/复用 bootstrap admin key（data/bootstrap_admin_key.txt，格式 af_admin_xxxxxxxx）
    3. 建表钩子（P1 实现 §8 全部表，幂等迁移）
    4. 调度器挂载（P2-2：APScheduler 启动 9 类 job）
    """
    settings = get_settings()
    data_dir(settings).mkdir(parents=True, exist_ok=True)
    load_or_create_bootstrap_key(settings)

    ensure_schema()

    # P2-2：调度器（唯一主动者）——防御式 job，无 LLM/无登录态时安全 no-op
    scheduler = create_scheduler()
    scheduler.start()
    _log.info("调度器已启动：%d 个 job", len(scheduler.get_jobs()))
    try:
        yield
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        _log.info("调度器已停止")


app = FastAPI(
    title="金丝雀蜜罐 CanaryGuard AntiFraud",
    description="蜜罐反诈双形态主控：REST API 同一内核，WebUI/MCP 均为客户端（D1）。",
    version=__version__,
    lifespan=lifespan,
)

setup_logging()          # P2-6：控制台 + var/logs/app.log
register_error_handlers(app)
app.add_middleware(RateLimitMiddleware)   # P2-5：公开热路径按 IP 限流


# P2-8（补充·CSP/安全响应头）：所有 HTTP 响应补安全头。
# - CSP 放宽点：style-src 'unsafe-inline'（Element Plus/Vite 内联样式）；
#   script/style/img 加 https://cdn.jsdelivr.net（FastAPI /docs Swagger UI/Redoc 的 CDN 资源）；
# - 该中间件在 RateLimitMiddleware 之后注册（Starlette 逆序执行 → 最外层），
#   保证 429/4xx/5xx 等所有响应都带安全头。
# P1-6（修复）：ConsoleView 内嵌 DSH（DeepSeek Harness Web GUI）。
# - 项目自包含内嵌实例：dsh/（0.1.5-rc.1 全新引擎 + 空白 dsh-home，独立端口 3092）
#   默认指向它（干净独立，不依赖设备全局 3080）；
# - 保留 http://127.0.0.1:3080 兼容旧部署/手动切回。
# 详情面板 iframe 内嵌平台网页（知乎/微博/抖音等）跳转到问题点。
# 允许列表做成常量，便于后续扩展内嵌来源。
_CSP_FRAME_SRC_ALLOW = (
    "'self'",
    "https://www.zhihu.com",
    "https://zhuanlan.zhihu.com",
    "https://weibo.com",
    "https://m.weibo.cn",
    "https://www.douyin.com",
    "https://*.douyin.com",
    "http://127.0.0.1:3092",  # 内嵌 DSH Agent 命令输入面板（项目自包含独立实例，默认）
    "http://127.0.0.1:3080",  # 兼容：设备全局 DSH（手动切回用）
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "img-src 'self' data: https://cdn.jsdelivr.net; "
        "connect-src 'self'; font-src 'self' data:; object-src 'none'; "
        "frame-src " + " ".join(_CSP_FRAME_SRC_ALLOW) + "; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """安全响应头（P2-8 补充）：CSP / X-Frame-Options / nosniff / Referrer-Policy。"""
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    """请求日志（P2-6）：method / path / status / duration；API 路径才记，避免 /ui 资源刷屏。"""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    if request.url.path.startswith("/api/"):
        _log.info("%s %s -> %s (%.1fms)", request.method, request.url.path,
                  response.status_code, duration_ms)
    return response

# P7/P9：WebUI 生产构建静态托管（双形态之一——独立软件形态的界面）。
# 前缀隔离：/ui/** 与 /api/v1/** 完全分离；SPA fallback 仅对 /ui/ 生效，不影响 API 封包。
_WEBUI_DIST = Path(__file__).resolve().parents[1] / "webui" / "dist"


def _ui_index() -> FileResponse | JSONResponse:
    index = _WEBUI_DIST / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        err("ui_not_built", "WebUI 未构建：请先在 webui/ 执行 npm run build（产物 webui/dist/index.html）"),
        status_code=404,
    )


@app.get("/", include_in_schema=False, response_model=None)
async def root() -> RedirectResponse | JSONResponse:
    """根路径 → WebUI。"""
    if _WEBUI_DIST.is_dir():
        return RedirectResponse("/ui/")
    return JSONResponse(err("ui_not_built", "WebUI 未构建，API 见 /api/v1/... 与 /docs"), status_code=404)


@app.get("/ui", include_in_schema=False, response_model=None)
async def ui_root() -> FileResponse | JSONResponse:
    return _ui_index()


@app.get("/ui/{path:path}", include_in_schema=False, response_model=None)
async def ui_spa(path: str) -> FileResponse | JSONResponse:
    """静态文件 + SPA fallback：真实文件直接返回；未知路径（非文件扩展名）回退 index.html。"""
    if not _WEBUI_DIST.is_dir():
        return _ui_index()
    candidate = (_WEBUI_DIST / path).resolve()
    if candidate != _WEBUI_DIST and _WEBUI_DIST not in candidate.parents:
        return JSONResponse(err("not_found", "404 Not Found"), status_code=404)  # 目录穿越防护
    if path and candidate.is_file():
        return FileResponse(candidate)
    if "." in path:  # 形如 assets/xxx.js 的资源请求不存在 → 404（不吞进 SPA）
        return JSONResponse(err("not_found", "404 Not Found"), status_code=404)
    return _ui_index()


API_V1 = "/api/v1"
app.include_router(api_system.router, prefix=API_V1)
app.include_router(api_auth.router, prefix=API_V1)
app.include_router(api_scan.router, prefix=API_V1)
app.include_router(api_traps.router, prefix=API_V1)
app.include_router(api_zhihu.router, prefix=API_V1)
app.include_router(api_accounts.router, prefix=API_V1)
app.include_router(api_account_bridges.router, prefix=API_V1)
app.include_router(api_grading.router, prefix=API_V1)
app.include_router(api_persona.router, prefix=API_V1)
app.include_router(api_alerts.router, prefix=API_V1)
app.include_router(api_evidence.router, prefix=API_V1)
app.include_router(api_cases.router, prefix=API_V1)
app.include_router(api_events.router, prefix=API_V1)
app.include_router(api_config.router, prefix=API_V1)
app.include_router(api_collector.router, prefix=API_V1)
app.include_router(api_detections.router, prefix=API_V1)
app.include_router(api_supply_chain.router, prefix=API_V1)
app.include_router(api_soc_lib.router, prefix=API_V1)
app.include_router(api_chat_import.router, prefix=API_V1)
app.include_router(api_speech_patterns.router, prefix=API_V1)
app.include_router(api_knowledge.router, prefix=API_V1)
app.include_router(api_cover.router, prefix=API_V1)
