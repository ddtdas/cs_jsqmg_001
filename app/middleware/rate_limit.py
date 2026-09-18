"""全局请求级限流中间件（P2-5：/scan/text、/accounts/check 等公开热路径防刷）。

设计：
  - 按客户端 IP 的令牌桶（复用 zhihu_bridge.TokenBucket），
    配额默认 60 req/min（AF_RATE_LIMIT_PER_MIN，0=关闭）；
  - 只对白名单公开热路径生效（扫描/账号速查/踩饵检测），写操作已由 require_admin 保护；
  - 携带有效 admin key 的请求豁免（AF_RATE_LIMIT_ADMIN_BYPASS=true，默认开）；
  - 超限返回 429 统一封包 {ok:false,error:{code:"rate_limited"}}；
  - 配置变更（容量变化）自动重建对应 IP 的桶；reset_rate_limiters() 供测试隔离。
"""

from __future__ import annotations

import re
import secrets
import threading
import time

from starlette.responses import JSONResponse

from ..config import get_settings
from ..utils import err

_LIMITED_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^/api/v1/scan/text$"),
    re.compile(r"^/api/v1/accounts/check$"),
    re.compile(r"^/api/v1/traps/\d+/check-hit$"),
)

_instances: list["RateLimitMiddleware"] = []


def reset_rate_limiters() -> None:
    """清空全部限流桶（测试隔离钩子，P2-5）。"""
    for m in list(_instances):
        with m._lock:
            m._buckets.clear()


class RateLimitMiddleware:
    """按 IP 令牌桶限流中间件（纯 ASGI）。"""

    def __init__(self, app) -> None:
        self.app = app
        self._buckets: dict[str, tuple[int, object, float]] = {}  # ip -> (capacity, bucket, last_seen)
        self._lock = threading.Lock()
        _instances.append(self)

    # ------------------------------------------------------------------ 鉴权豁免
    @staticmethod
    def _valid_admin(headers: dict[str, str], settings) -> bool:
        from ..deps import get_admin_key

        expected = get_admin_key(settings)
        if not expected:
            return False
        token = headers.get("x-api-key", "") or ""
        if not token:
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        return bool(token) and secrets.compare_digest(token, expected)

    # ------------------------------------------------------------------ 桶
    @staticmethod
    def _client_ip(scope) -> str:
        """限流键来源：仅真实 TCP 对端（scope['client']）。

        P1-D8：忽略 X-Forwarded-For / X-Real-IP / Forwarded 等转发头 ——
        这些头可由客户端伪造，绝不作为限流键；配合 run.py 的
        proxy_headers=False，scope['client'] 恒为 socket 实际对端。
        """
        client = scope.get("client") or ("127.0.0.1", 0)
        return client[0] if client and client[0] else "127.0.0.1"

    def _acquire(self, ip: str, quota: int) -> bool:
        from ..services.zhihu_bridge import TokenBucket

        now = time.monotonic()
        with self._lock:
            entry = self._buckets.get(ip)
            if entry is None or entry[0] != quota:
                bucket = TokenBucket(capacity=quota, refill_per_second=quota / 60.0)
                self._buckets[ip] = (quota, bucket, now)
            else:
                bucket = entry[1]
            ok = bucket.try_acquire(1)
            if ok or entry is not None:
                self._buckets[ip] = (quota, bucket, now)
            # 防内存膨胀：桶数量过大时清理超时条目（>10min 未访问）
            if len(self._buckets) > 1024:
                for k, (_, _b, ts) in list(self._buckets.items()):
                    if now - ts > 600:
                        del self._buckets[k]
            return ok

    # ------------------------------------------------------------------ ASGI
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not any(p.match(path) for p in _LIMITED_PATTERNS):
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        quota = int(settings.rate_limit_per_min or 0)
        if quota <= 0:
            await self.app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or []
        headers = {
            (k.decode("latin-1") if isinstance(k, bytes) else k).lower():
                (v.decode("latin-1") if isinstance(v, bytes) else v)
            for k, v in raw_headers
        }
        if settings.rate_limit_admin_bypass and self._valid_admin(headers, settings):
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        if not self._acquire(ip, quota):
            response = JSONResponse(
                status_code=429,
                content=err("rate_limited", "请求过于频繁，请稍后再试"),
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)