"""uvicorn 启动入口：python -m app.run [--host HOST] [--port PORT]

端口/主机默认值取自配置（AF_HOST / AF_PORT，默认 127.0.0.1:9200），
命令行参数可覆盖。
"""

from __future__ import annotations

import argparse

import uvicorn

from .config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.run", description="启动金丝雀蜜罐 FastAPI 主控")
    parser.add_argument("--host", default=None, help="监听地址（默认取 AF_HOST）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取 AF_PORT）")
    args = parser.parse_args()

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    print(f"[af] 主控启动 http://{host}:{port}  (docs: http://{host}:{port}/docs)")
    # P1-D2/D8：显式关闭 uvicorn ProxyHeadersMiddleware —— 不再信任回环对端注入的
    # X-Forwarded-For / X-Real-IP 改写 scope['client']。回环判定（auth._require_loopback）
    # 与限流键（RateLimitMiddleware）都必须基于真实 TCP 对端，头部一律不作为依据。
    uvicorn.run(
        "app.main:app", host=host, port=port, reload=False,
        proxy_headers=False,
        forwarded_allow_ips=[],
    )


if __name__ == "__main__":
    main()
