# P8 MCP Server 预习笔记（mcp-dev · t9 开工前）

> 预习完成时间：P8 任务认领前。技术路线已用真实 SDK 冒烟验证，t9 可直接照此实施。

## 0. 关键结论速览

| 项 | 结论 |
|---|---|
| 官方 SDK | ✅ 可联网安装。全局 Python 已装 **mcp 2.2.0 + mcp-types 2.2.0**（`pip install "mcp>=2.0"` 成功） |
| SDK API | **mcp 2.x：`FastMCP` 已改名 `MCPServer`**（`from mcp.server.mcpserver import MCPServer`）。`mcp.server.fastmcp` 模块已删除，**禁止 import**（会抛 ModuleNotFoundError） |
| add_request_handler | 在低层 `mcp.server.lowlevel.Server.add_request_handler(method, params_type, handler)`；高层 MCPServer 主要用 `@server.tool()` 装饰器，无需手写 handler |
| 项目 venv | `.venv`（Python 3.12.10）**尚未装 mcp**，requirements.txt 也无 mcp → t9 需 `pip install mcp` 进 venv 并补 requirements |
| 主控状态 | 127.0.0.1:9200 **未启动**（t1 未完成）；P0 骨架已在交付目录落地（health/stats/bootstrap-info 可用） |
| 参考项目 | `D:\dsh_gzq_1\honeyprompt-v2` 本机不存在（只剩 logs）→ 按主方案 §7.1 规格自实现，不依赖 Mellivora 源码 |
| 降级方案 | 网络已验证可用，官方 SDK 路线成立；仍预留"自实现 JSON-RPC stdio"兜底方案（见 §5），但不启用 |

## 1. mcp 2.2.0 实测 API 面（已用探针+冒烟验证）

```python
from mcp.server.mcpserver import MCPServer  # 2.x 入口（原名 FastMCP）

server = MCPServer(name="af-honeypot", version="0.1.0", instructions="...")

@server.tool()                       # 装饰器：注解自动生成 JSON Schema
async def af_scan_text(text: str, source: str = "manual") -> dict:
    """单条文本话术判定（规则+LLM 双确认）。"""   # docstring 第一行成为工具描述
    ...

# 显式注册：server.add_tool(fn, name=..., description=...)
# 列出工具：tools = await server.list_tools()   ← 注意是协程！
#   Tool 对象字段：t.name / t.description / t.input_schema（snake_case，无 inputSchema）
# 启动：
server.run(transport="stdio")                  # 默认 stdio
await server.run_streamable_http_async(host="127.0.0.1", port=9201,
                                       streamable_http_path="/mcp")   # --http 可选
```

实测生成的 schema 示例（af_list_traps）：`{"properties":{"status":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null},"limit":{"type":"integer","default":20}},"type":"object"}` —— `str | None` 自动 anyOf null，满足透传工具参数需求。

## 2. 主控对接约定（已读 P0 源码确认）

- **认证**（`app/deps.py`）：`X-API-Key: af_admin_xxxxxxxx` 或 `Authorization: Bearer af_admin_xxxxxxxx`。MCP 层统一发 `X-API-Key` 头。
- **封包**（`app/utils.py`，D1 铁律）：成功 `{"ok": true, "data": ...}`；失败 `{"ok": false, "error": {"code": ..., "message": ...}}`（错误也走同一封包，HTTP 状态码 401/422/500 等仍保留）。
  → MCP 层唯一解包点：`_unwrap(body)` 帮助函数，`ok=False` 时抛 ToolError(code/message)，`ok=True` 返回 `data`。**不得复制任何业务逻辑，只透传**。
- **路由前缀**：`/api/v1`。现有端点：`GET /system/health`(公开) / `GET /system/stats`(admin) / `POST /auth/bootstrap-info`(公开)。
- **主方案 §4.2 完整路由表**（t9 时与 openapi.json 核对）：traps/scan/accounts/grading/evidence/cases/zhihu/alerts/config/keys/system。
- **MCP env**（§7.1）：`AF_MASTER_URL`（默认 http://127.0.0.1:9200）、`AF_API_KEY`（透传用 admin key）。`--http --port 9201` 可选。
- **mcp接入配置示例.json**：`command=<venv>/Scripts/python.exe, args=[<项目根>/mcp/server.py], env={AF_MASTER_URL, AF_API_KEY}`；Windows JSON 路径需双反斜杠。

## 3. t9 实施清单（mcp/server.py）

1. `mcp/server.py`：
   - 工厂函数 `build_mcp_server(transport=None)` —— transport 可注入（httpx.MockTransport）供测试，默认 `httpx.AsyncClient(base_url=AF_MASTER_URL, headers={"X-API-Key": AF_API_KEY})`。
   - `_unwrap(body)` 统一解包；`_call(method, path, **params)` 透传封装。
   - ~28 个 `@server.tool()` 透传工具，与 §7.2 清单一一对应（af_list_traps / af_generate_trap_draft / af_mark_trap_deployed / af_disable_trap / af_retire_trap / af_scan_text / af_scan_inbox / af_account_check / af_grade_explain / af_build_evidence / af_freeze_evidence / af_export_evidence / af_report_template / af_list_cases / af_publish_case / af_case_graph / af_list_events / af_get_event / af_list_alerts / af_mark_alert_read / af_health / af_stats / af_zhihu_channels / af_zhihu_login / af_zhihu_verify / af_config / af_config_update / af_register_agent）。
   - `if __name__ == "__main__":` 解析 `--http` / `--port`（默认 stdio），启动。
   - 敏感工具（af_config_update / af_publish_case 等）参数带 `confirm: bool` + `reason: str`，透传主控的 confirm+reason 闸控（HITL/D6）。
2. requirements.txt 追加 `mcp>=2.0`；venv 安装。
3. `docs/06-MCP集成.md`（或 README 章节）：接入配置示例 + 工具清单 + 合规声明（HITL 红线）。
4. `tests/test_mcp_mount.py`（见 §4）。

## 4. test_mcp_mount.py 规划

- **全工具挂载测试**（核心 DoD）：
  - `build_mcp_server(transport=mock)` → `tools = await server.list_tools()`
  - 断言：工具数 ≥28；全部 `af_` 前缀；name 无重复；description 非空；`input_schema` 是合法 JSON Schema（type=object + properties）；关键工具（af_health/af_scan_text/af_list_traps）必在。
- **三连实测（透传逻辑验证，mock 主控）**：用 `httpx.MockTransport` 按 URL 路径返回固定 `{ok,data}`：
  - `af_health()` → GET /api/v1/system/health → 返回 data（解包正确）；
  - `af_scan_text("...")` → POST /api/v1/scan/text，断言透传了 body 参数；
  - `af_list_traps(status="active")` → GET /api/v1/traps，断言 query 参数透传；
  - 错误路径：mock 返回 `{ok:false,error}`（HTTP 200 与 4xx 各一例）→ 断言抛出的错误带 code/message，不吞不造。
- **真实主控集成测试（可选/条件）**：若 9200 已起（t9 时 t1–t7 应已完成），起 uvicorn 子进程或用 TestClient 同进程起主控（AF_DATA_DIR 指临时目录、AF_PORT 随机端口），把 AF_MASTER_URL/AF_API_KEY 指向它跑三连；主控未起时该用例自动 skip 并记录（不让测试红）。
- 对齐现有测试风格：pytest + fixtures（参考 `tests/test_p0_skeleton.py` 的 tmp_path/monkeypatch 用法）。

## 5. 兜底方案（网络受限时启用，当前不启用）

网络实测可用，官方 SDK 已装。若部署机离线：
- 自实现最小 MCP stdio server：读 stdin JSON-RPC（initialize / tools/list / tools/call / notifications/initialized），按 MCP 协议返回（协议版本 2025-06-18，serverInfo，tools 清单 JSON Schema，call 结果 `{"content":[{"type":"text","text":json.dumps(data)}],"isError":false}`）。
- 验证方式不变：test_mcp_mount.py 只依赖 `build_mcp_server()` 返回的对象与 `list_tools()`，换实现不改测试。

## 6. 待 t9 开工时核对

- [ ] `GET http://127.0.0.1:9200/openapi.json`（t1 完成后）核对真实端点路径/参数与 §7.2 工具映射
- [ ] 与 backend-core 对齐：POST 体字段名、分页参数名、错误码清单
- [ ] venv 装 mcp 后重跑本笔记 §1 冒烟
- [ ] 确认主控 AF_API_KEY 取值方式（env 优先 > data/bootstrap_admin_key.txt）
