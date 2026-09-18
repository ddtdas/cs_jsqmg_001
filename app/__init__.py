"""金丝雀蜜罐 CanaryGuard AntiFraud —— FastAPI 主控应用包。

双形态同一内核（D1）：全部业务逻辑在 REST API 内，WebUI 与 MCP Server
都是本 API 的客户端。版本号与 __version__ 对齐主方案 P0/R9。
"""

__version__ = "1.0.0"