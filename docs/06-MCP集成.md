# MCP 集成说明（金丝雀蜜罐 CanaryGuard AntiFraud · P8）

> 文档编号：docs/06-MCP集成（索引见 docs/README.md）；另见根 `README.md` §6「MCP 接入」。
>
> MCP Server = 主控 REST API 的**纯透传代理**（主方案 §7.1，D1 铁律）：
> 所有业务逻辑都在 FastAPI 主控内，MCP 层只做 httpx 调用封装与 `{ok,data}|{ok:false,error}` 统一解包。
> dsh / Claude Code / Codex 等 MCP 客户端接入后，即可用自然语言驱动蜜饵/检测/证据/案例等全部能力。

---

## 1. 运行前提

| 项 | 要求 |
|---|---|
| 主控 | 已启动：`python app/run.py`（或 `start.bat`），默认 `http://127.0.0.1:9200` |
| Python | 项目 venv：`<项目根目录>\.venv\Scripts\python.exe`（已含 `mcp>=2.0` 依赖） |
| 认证 | 主控 admin key：`<项目根目录>\data\bootstrap_admin_key.txt`（格式 `af_admin_xxxxxxxx`） |
| 传输 | 默认 **stdio**（客户端拉起）；`--http --port 9201` 可选 **streamable-http** |

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AF_MASTER_URL` | `http://127.0.0.1:9200` | 主控 REST 地址 |
| `AF_API_KEY` | （空） | 主控 admin key，经 `X-API-Key` 头透传；留空则只能调只读公开端点 |

---

## 2. 客户端接入配置

### 2.1 dsh / Claude Code / Codex（通用 `mcpServers` 格式）

将以下 JSON 写入对应客户端的 MCP 配置（dsh：`~/.dsh/mcp.json`；Claude Code：`~/.claude.json` 的 `mcpServers`；Codex：`~/.codex/config.toml` 或等价配置）：

```json
{
  "mcpServers": {
    "af-honeypot": {
      "command": "C:\\Users\\Administrator\\Desktop\\知乎黑客松\\金丝雀蜜罐\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Users\\Administrator\\Desktop\\知乎黑客松\\金丝雀蜜罐\\mcp\\server.py"],
      "env": {
        "AF_MASTER_URL": "http://127.0.0.1:9200",
        "AF_API_KEY": "af_admin_xxxxxxxx"
      }
    }
  }
}
```

> **Windows 注意**：JSON 内路径必须双反斜杠转义（`\\`）；Linux/macOS 用正斜杠。
> 也可以直接运行 `python mcp/server.py --http --port 9201`，再让客户端以 streamable-http 方式连接
> `http://127.0.0.1:9201/mcp`。

### 2.2 快速获取本机接入配置

MCP 内已提供工具 `af_register_agent`：直接调用即可返回**当前机器可用的接入配置 JSON**
（含真实 venv 解释器路径与 server.py 路径），无需手工替换占位符。

### 2.3 验证接入

接入后先调用三个工具确认链路：

1. `af_health` —— 主控/DB/LLM/知乎通道四灯；
2. `af_scan_text` —— 传一句话术（如"杀猪盘带你投资稳赚不赔"）看分级结果；
3. `af_list_traps` —— 蜜饵列表。

---

## 3. 工具清单（55 个 af_ 前缀工具，透传主控 101 个 REST 端点中已映射的 54 个）

| 分组 | 工具 | 后端端点 |
|---|---|---|
| 蜜饵 | `af_list_traps` / `af_get_trap` | `GET /api/v1/traps` / `GET /api/v1/traps/{id}` |
| | `af_generate_trap_draft` | `POST /api/v1/traps/generate-draft`（HITL，只产草稿） |
| | `af_mark_trap_deployed` / `af_disable_trap` / `af_retire_trap` | `POST /api/v1/traps/{id}/deploy|disable|retire` |
| | `af_check_trap_hit` | `POST /api/v1/traps/{id}/check-hit`（命中即退役 + 联动检测升级 L5，R2 修复；可选 `detection_id` 由 REST 直传，MCP 工具签名未暴露该参数） |
| 检测 | `af_scan_text` | `POST /api/v1/scan/text` |
| | `af_scan_inbox` | `POST /api/v1/scan/inbox`（批量消费 pending→scanned；`GET` 查结果） |
| | `af_list_scan_hits` | `GET /api/v1/scan/hits` |
| | `af_detection_full` / `af_detection_platform_link` | `GET /api/v1/detections/{id}/full` / `GET /api/v1/detections/{id}/platform-link` |
| | `af_account_check` / `af_account_get` | `POST /api/v1/accounts/check` / `GET /api/v1/accounts/{url_name}` |
| | `af_grade_explain` / `af_grading_levels` | `GET /api/v1/grading/explain/{id}` / `GET /api/v1/grading/levels` |
| 情报 | `af_build_evidence` / `af_get_evidence` | `POST /api/v1/evidence/build` / `GET /api/v1/evidence/{id}` |
| | `af_freeze_evidence` / `af_export_evidence` / `af_report_template` | `POST .../freeze` / `GET .../export` / `GET .../report-template` |
| | `af_list_cases` / `af_publish_case` / `af_case_graph` | `GET /api/v1/cases` / `POST /api/v1/cases/publish` / `GET /api/v1/cases/graph` |
| 事件告警 | `af_list_events` / `af_get_event` | `GET /api/v1/events` / `GET /api/v1/events/{id}` |
| | `af_list_alerts` / `af_mark_alert_read` | `GET /api/v1/alerts` / `POST /api/v1/alerts/{id}/read` |
| 采集矩阵 P10 | `af_collector_matrix` / `af_collector_add_cell` | `GET /api/v1/collector/matrix` / `POST /api/v1/collector/matrix` |
| | `af_collector_update_cell` / `af_collector_delete_cell` | `PUT /api/v1/collector/matrix/{id}` / `DELETE /api/v1/collector/matrix/{id}` |
| | `af_collector_run` / `af_collector_run_all` | `POST /api/v1/collector/matrix/{id}/run` / `POST /api/v1/collector/matrix/run-all` |
| | `af_collector_tech_stack` | `GET /api/v1/collector/tech-stack` |
| 账号桥接 P9 | `af_account_bridges` / `af_account_bridge_detail` | `GET /api/v1/account-bridges` / `GET /api/v1/account-bridges/{platform}` |
| | `af_account_bridge_save` | `PUT /api/v1/account-bridges/{platform}/config`（敏感字段 Fernet 加密） |
| | `af_account_bridge_test` / `af_account_bridge_ingest` | `POST /api/v1/account-bridges/{platform}/test|ingest` |
| | `af_account_bridge_agent_import` / `af_account_bridge_delete` | `GET /api/v1/account-bridges/{platform}/agent-import` / `DELETE /api/v1/account-bridges/{platform}` |
| 供应链 P10 | `af_supply_chain_query` / `af_supply_chain_results` | `POST /api/v1/supply-chain/query` / `GET /api/v1/supply-chain/results` |
| 运维 | `af_health` / `af_stats` / `af_embedded_dsh` | `GET /api/v1/system/health|stats|embedded-dsh` |
| | `af_zhihu_channels` / `af_zhihu_status` | `GET /api/v1/zhihu/channels` / `GET /api/v1/zhihu/status` |
| | `af_zhihu_login` / `af_zhihu_verify` / `af_zhihu_import` | `POST /api/v1/zhihu/channels/{ch}/login|verify` / `POST /api/v1/zhihu/import` |
| | `af_config` / `af_config_update` | `GET /api/v1/config` / `PUT /api/v1/config`（confirm+reason 闸控） |
| | `af_register_agent` | （本机自描述，不发主控请求） |

> v1.0.0（P6.5 收尾）：`/events`、`/config`、`/scan/inbox` 已全部落地，上表**不再有规划端点**。
> 若调用仍返回 `ProxyError [not_found]`，表示连接的是旧版主控（对应端点未实现），原样透传不吞不造。
> 注：主控现有 101 个 REST 端点中，auth 引导/本机登录、persona 人设、chat-import 聊天导入、soc-lib 社工库、
> knowledge 知识库、speech-patterns 话术库、cover 反钓鱼伪装、collector/platforms、supply-chain/results/{name}、accounts timeline、
> traps monitor、audit-verify 等端点未映射 MCP 工具（仅 REST/WebUI 可用），上表为全部 55 个工具。

## 4. 关键行为约定

### 4.1 统一解包（D1）

主控所有响应（含 4xx/5xx 错误）均为统一封包，MCP 层只解包不加工：

```
{ok:true,  data: ...}                      -> 工具直接返回 data
{ok:false, error:{code, message}}          -> 工具报错，code/message 原样透传
```

### 4.2 敏感操作（D6 闸控）

以下工具透传 `confirm` + `reason` 参数，由主控 GateLog 哈希链审计：
`af_retire_trap`（退饵）、`af_freeze_evidence`（冻结证据包，R2 复核补录）、`af_publish_case`（入库发布）、`af_config_update`（改配置）。
**主控强制闸控（R4 修复后实测生效）**：`confirm=false` 或缺失 → 403 `confirm_required`；
`reason < 20` 字 → 422 `reason_too_short`。agent 调用必须携带 `confirm=true` 且 `reason≥20` 字说明，
成功操作会写入 gate_logs（action=`trap.retire` / `evidence.freeze` / `case.publish` / `config.put`）哈希链审计。
R1 修复（P1-3/P1-4）行为补充：`af_build_evidence` 对已打包检测重复建包 → 409 `evidence_exists`；
`af_publish_case` 同一检测重复发布 → 同载荷幂等返回、异载荷 409 `case_exists`；`af_freeze_evidence` 二次冻结 → 409 `already_frozen`。

### 4.3 HITL 红线（D3）

系统只产蜜饵草稿/证据包/举报模板，**永不自动发布、永不自动钓鱼**。发布动作必须由用户在知乎侧手动完成，
随后用 `af_mark_trap_deployed` 标记。cookie 注入（`af_zhihu_login`）同样由用户自行导出粘贴。

---

## 5. 开发与测试

```bash
# 单元 + 集成测试（集成用例会自行拉起真实主控 uvicorn 子进程）
pytest tests/test_mcp_mount.py -v

# 手动以 stdio 模式启动（供 MCP 客户端拉起）
.venv\Scripts\python.exe mcp\server.py

# 手动以 streamable-http 模式启动
.venv\Scripts\python.exe mcp\server.py --http --port 9201
```

### 5.1 命名空间警示（重要）

项目目录 `mcp/` 是**命名空间段（刻意不建 `__init__.py`）**：若创建 `mcp/__init__.py`，
会遮蔽 site-packages 中的官方 `mcp` SDK 包（常规包优先于命名空间段），导致
`from mcp.server.mcpserver import MCPServer` 失败。请勿添加该文件。
测试中本项目模块按文件路径加载（见 `tests/test_mcp_mount.py::_load_mcp_server`）。

---

## 6. 合规声明

本项目为社区成员第一人称反诈工具，**非官方软件**；MCP 能力仅供个人侦查取证与分级提醒，
只读自有/公开数据，处置权（冻结/拦截/举报受理）一律归官方通道。使用时须遵守知乎服务条款与《个人信息保护法》。
