"""认证端点：bootstrap-info + 本机一键登录 / key 管理。

对应主方案 §4.2 auth.py 路由表。bootstrap-info 公开可访问（仅返回是否已初始化，
不泄露 key 内容），供 WebUI 首启引导与 MCP 接入配置检测使用。

本机模式（P7/R1 开门即用）新增三个仅回环（127.0.0.1 / ::1 / localhost）可调端点：
  - POST /auth/local-login   一键登录：直接返回 bootstrap admin key（本机浏览器免读文件）
  - GET  /auth/current-key   设置页展示：返回当前 admin key 明文（仅本机）
  - POST /auth/reset-key     重置 key：confirm=true + reason>=20 字 + GateLog 审计（D6）
非回环来源一律 403（loopback_only），避免 key 从网络侧泄出。

R4-F2（队1 P1-1 复核结论）：即使主控以 --host 0.0.0.0 监听（局域网可达），
本模块 _require_loopback 依据真实 TCP 对端（uvicorn proxy_headers=False，
scope['client'] 即 socket 对端，run.py 已显式禁用转发头信任）判定来源：
局域网客户端连接时 request.client.host 为外部 IP → 403 loopback_only，
X-Forwarded-For 等伪造头一律不参与判定。因此无 key 时本机 200 属设计行为
（本机一键登录），外部不可达；无需改绑 127.0.0.1。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import bootstrap_key_path, get_admin_key
from ..services.audit_log import write_gate_log
from ..utils import ApiError, ok

router = APIRouter(prefix="/auth", tags=["auth"])

# 敏感操作 reason 最短字数（与 config PUT 对齐）
REASON_MIN_LEN = 20

# 允许的来源：本机回环地址（IPv4 / IPv6 / localhost / IPv4 映射）
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}

# P1-A2：reset-key 进程内串行锁（文件写 + api_keys 表更新必须原子；uvicorn 单 worker 足够，
# 多 worker 部署建议仅保留一个 worker 处理本端点或再加文件锁）
_RESET_LOCK = threading.Lock()


def _require_loopback(request: Request, settings: Settings) -> None:
    """仅允许本机回环来源；其余 403（P7/R1：local-login / key 管理端点安全边界）。

    P1-D2 加固：回环判定只信任真实 TCP 对端 —— request.client 即 scope['client']，
    由 uvicorn 在 proxy_headers=False 下直取 socket 对端（见 app/run.py），
    X-Forwarded-For / X-Real-IP / Forwarded 等转发头一律不参与判定（不读取）。

    P1-A4（DNS-rebinding/CSRF 纵深）：回环通过后再校验
      - Host 头白名单：127.0.0.1 / localhost / ::1（可带端口）——防 rebinding 后
        恶意域名（解析到 127.0.0.1）以自己为 Host 请求本端点；
      - Origin 头：若携带，必须为本机 http://127.0.0.1:<port> 或 http://localhost:<port>；
      - Sec-Fetch-Site：若携带，必须为 same-origin / none（curl 无此头放行）。
    """
    host = (request.client.host if request.client else "") or ""
    normalized = host.lower().split("%")[0]  # 去掉 IPv6 zone id（如 fe80::1%12）
    if normalized not in _LOOPBACK_HOSTS:
        raise ApiError("loopback_only", "该端点仅允许本机（127.0.0.1 / ::1）调用", 403)

    # ---- P1-A4：Host 白名单（DNS-rebinding 核心防线）----
    host_hdr = request.headers.get("host", "")
    if host_hdr:
        hostname = host_hdr
        if hostname.startswith("["):  # IPv6 字面量 [::1]:9200
            hostname = hostname[1:].split("]")[0]
        elif hostname.count(":") == 1:
            hostname = hostname.split(":")[0]
        if hostname.lower() not in _LOOPBACK_HOSTS:
            raise ApiError("host_forbidden", "Host 头不在本机白名单，拒绝访问", 403)

    # ---- P1-A4：Origin 白名单 ----
    origin = request.headers.get("origin", "")
    if origin:
        allowed_origins = {
            f"http://127.0.0.1:{settings.port}",
            f"http://localhost:{settings.port}",
        }
        if origin not in allowed_origins:
            raise ApiError("origin_forbidden", "Origin 不在本机白名单，拒绝访问", 403)

    # ---- P1-A4：Sec-Fetch-Site（浏览器跨站标记）----
    sfs = request.headers.get("sec-fetch-site", "")
    if sfs and sfs not in ("same-origin", "none"):
        raise ApiError("origin_forbidden", "跨站请求被拒绝（Sec-Fetch-Site 校验）", 403)


@router.post("/bootstrap-info")
def bootstrap_info(settings: Settings = Depends(get_settings)) -> dict:
    """返回 bootstrap admin key 是否已生成 + 端口提示（不含 key 本身、不含路径/文件名）。

    P0-D1 修复：不再返回 key_file 绝对路径（曾泄露盘符与文件名）；
    hint 文案也不出现文件名，仅提示 key 存放于 data 目录下配置文件。
    """
    path = bootstrap_key_path(settings)
    initialized = settings.api_key != "" or path.exists()

    if initialized:
        hint = (
            f"主控运行于 http://{settings.host}:{settings.port}；"
            "admin key 已初始化（首次启动自动生成，见 data 目录下配置文件）。"
        )
    else:
        hint = (
            f"主控运行于 http://{settings.host}:{settings.port}；"
            "尚未初始化 admin key，请重启主控完成引导。"
        )
    return ok({
        "initialized": initialized,
        "port": settings.port,
        "hint": hint,
    })


class ResetKeyRequest(BaseModel):
    confirm: bool = Field(default=False, description="敏感操作双确认：必须为 true")
    reason: str = Field(default="", description="操作理由（≥20 字）")


@router.post("/local-login")
def local_login(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """本机一键登录：仅回环来源，返回当前 bootstrap admin key（浏览器直接可用）。

    无需用户读文件/配环境变量：start.bat 打开 /ui/ 后，登录页"一键登录（本机）"
    按钮调用本端点拿 key 完成登录。
    """
    _require_loopback(request, settings)
    key = get_admin_key(settings)
    if not key:
        raise ApiError("no_admin_key", "admin key 尚未生成，请重启主控", 500)
    return ok({"key": key, "hint": "本机模式，直接登录"})


@router.get("/current-key")
def current_key(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> dict:
    """本机查看当前 admin key 明文（设置页展示用）；非回环 403。

    P2-A 修复：响应不再返回 key_file 绝对路径（曾泄露盘符/目录/文件名）；
    source 已足够区分 env/文件来源。前端展示无需路径。
    """
    _require_loopback(request, settings)
    return ok({
        "key": get_admin_key(settings),
        "source": "env" if settings.api_key else "file",
    })


@router.post("/reset-key")
def reset_key(
    payload: ResetKeyRequest,
    request: Request,
    db=Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    """重置 bootstrap admin key：仅回环 + confirm=true + reason>=20 字（D6 审计）。

    流程（P1-A2：_RESET_LOCK 串行化，文件写 + api_keys 表状态原子一致）：
    校验通过 → 生成新 key（>=128bit）写 data/bootstrap_admin_key.txt →
    api_keys 全部 bootstrap 行停用 + 新哈希启用（enabled=1 恒为 1 行）→
    gate_logs 链式审计（action='auth.reset_key'，P1-C6）+ events 事件。
    环境变量 AF_API_KEY 显式提供时不允许重置（key 由 env 托管，改 env 而非文件）。
    """
    _require_loopback(request, settings)
    if not payload.confirm:
        raise ApiError("confirm_required", "敏感操作必须 confirm=true（双确认）", 403)
    reason = payload.reason.strip()
    if len(reason) < REASON_MIN_LEN:
        raise ApiError("reason_too_short",
                       f"reason 至少 {REASON_MIN_LEN} 字，当前 {len(reason)} 字", 422)
    if settings.api_key:
        raise ApiError("env_key_locked",
                       "当前 key 由环境变量 AF_API_KEY 提供，请直接修改环境变量，不能经本端点重置", 403)

    with _RESET_LOCK:
        old_key = get_admin_key(settings)
        # P1-A5：>=128bit（32 hex）；兼容旧 8-hex key 文件读取（deps._ADMIN_KEY_RE 8-64）
        new_key = "af_admin_" + secrets.token_hex(16)
        path = bootstrap_key_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_key + "\n", encoding="utf-8")

        old_digest = (
            hashlib.sha256(old_key.encode("utf-8")).hexdigest() if old_key else None
        )
        new_digest = hashlib.sha256(new_key.encode("utf-8")).hexdigest()
        # 原子化：先停用全部 bootstrap 行，再登记/启用新行（防并发漂移 & 历史残留）
        db.execute("UPDATE api_keys SET enabled=0 WHERE role='admin' AND name='bootstrap'")
        db.execute(
            "INSERT OR IGNORE INTO api_keys (key_hash, role, name) VALUES (?, 'admin', 'bootstrap')",
            (new_digest,),
        )
        db.execute("UPDATE api_keys SET enabled=1 WHERE key_hash=?", (new_digest,))

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        before = {"key_sha256": old_digest} if old_digest else {}
        after = {"key_sha256": new_digest}
        payload_json = json.dumps({"reset_key": True}, ensure_ascii=False, sort_keys=True)
        gate_log_id = write_gate_log(
            db,
            action="auth.reset_key",
            payload_json=payload_json,
            before=json.dumps(before, ensure_ascii=False),
            after=json.dumps(after, ensure_ascii=False),
            reason=reason,
            ts=now,
        )
        db.execute(
            "INSERT INTO events (kind, payload) VALUES ('admin_key_reset', ?)",
            (json.dumps({"key_sha256": new_digest}, ensure_ascii=False),),
        )
        db.commit()
    return ok({
        "key": new_key,
        "updated_at": now,
        "audit": {"gate_log_id": gate_log_id, "action": "auth.reset_key"},
    })
