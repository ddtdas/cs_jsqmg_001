# 🛡️ 金丝雀蜜罐 · CanaryGuard AntiFraud

> **一句话定位**：面向普通用户（个人第一人称）的反诈工具——把坏人的诈骗套路降维成五个可执行环节（**蜜饵诱捕 → 话术识别 → 账号速查 → 五级分级 → 证据案例**），只做**侦查取证与分级提醒**，不做任何处置执法。
>
> **双形态**：① 独立软件（FastAPI 主控 + WebUI 管理台 + 知乎通道）；② MCP Server（stdio/HTTP 双传输，dsh / Claude Code / Codex 等 agent 直接调用）。
> **版本**：v1.0.0 ｜ 仓库：`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐` ｜ 技术方案蓝本：`执行方案_反诈蜜罐\01_蜜罐反诈V2_双形态技术方案与实施计划.md`

---

## 目录

- [⚠️ 合规声明（必读）](#️-合规声明必读)
- [1. 这是什么 / 不是什么](#1-这是什么--不是什么)
- [2. 架构：双形态，同一内核](#2-架构双形态同一内核)
- [3. 功能主线 R1–R6](#3-功能主线-r1r6)
- [4. 快速开始（5 分钟）](#4-快速开始5-分钟)
- [5. API 摘要](#5-api-摘要)
- [6. MCP 接入（agent 形态）](#6-mcp-接入agent-形态)
- [7. 目录结构](#7-目录结构)
- [8. 测试与质量](#8-测试与质量)
- [9. 版本与变更](#9-版本与变更)

---

## ⚠️ 合规声明（必读）

本项目是**社区成员个人开发的第一人称反诈工具**，**不是**知乎、公安机关、反诈中心或任何官方机构发布的软件；也**不是**官方 API 的客户端，与知乎官方无任何合作关系。

使用本项目即表示你理解并同意以下边界（对应主方案 §11 安全合规清单）：

1. **非官方工具**：项目不冒充任何官方身份，输出物中不会伪造 96110/平台官方名义；如被要求"官方核实"请一律通过官方渠道。
2. **只侦查取证，不处置执法**：系统只做数据采集（只读自有/公开信息）、话术判定、分级提醒、证据包与举报**模板**生成；**不做**冻结、拦截、封锁、点名曝光、人肉、诱导转账或任何执法/处置动作。
3. **处置权归官方**：举报与处置一律走官方通道——**96110（反诈专线）/ 国家反诈中心 APP / 知乎平台举报**。本工具生成的是给官方通道使用的材料，不是处置本身。
4. **HITL 红线（人类在环）**：系统**永不自动发布**——蜜饵只产草稿不自动部署，举报只出模板不自动提交；任何对外动作必须由**你本人**在知乎/官方渠道手动完成。
5. **数据最小化**：只采集完成反诈侦查所需的最少信息（自有账号数据/公开信号）；cookie 加密存储、明文不回显；案例库强制脱敏（姓名/手机号/身份证号全抹）后才会入库。
6. **遵守平台与服务条款**：使用须遵守知乎服务条款、反爬政策与《中华人民共和国个人信息保护法》；不扫描手机号/定位/关注图谱等越界数据，访问频率受全局令牌桶限制。
7. **无保证**：本项目按"现状"提供，判定结果仅供参考，不构成法律/财务建议；因使用本项目产生的任何后果由使用者自行承担。

> 细节见 `docs/00-项目概述.md` §合规红线。

---

## 1. 这是什么 / 不是什么

| 是 ✅ | 不是 ❌ |
|---|---|
| 个人反诈侦查取证工具箱（第一人称） | 执法/处置平台，不冻结、不拦截、不点名 |
| 蜜饵（诱饵话术）草稿生成器（HITL，只产不投） | 自动发布器、自动钓鱼工具 |
| 话术识别 + 账号速查 + 五级分级提醒 | 官方 APP / 官方渠道的替代品 |
| 证据包（哈希链防篡改）+ 举报模板生成 | 自动向平台/公安提交举报的机器人 |
| MCP 工具集，供 dsh/Claude Code/Codex 调用 | 面向公众的多用户 SaaS |

**五环降维**（把复杂反诈场景拆成五个可执行环节）：`蜜饵踩饵（R1）→ 话术识别（R2）→ 账号速查（R3）→ 五级分级（R4）→ 证据案例（R5/R6）`，全流程确定性优先、人类在环。

---

## 2. 架构：双形态，同一内核

```
┌────────────────── 独立软件形态 ──────────────────┐   ┌──────────── MCP 形态 ────────────┐
│  WebUI (Vue3)  http://127.0.0.1:9200/ui/        │   │  dsh / Claude Code / Codex        │
│        │ (REST 客户端，D1)                        │   │        │ (MCP 客户端)              │
│        ▼                                         │   │        ▼                          │
│  ┌─ FastAPI 主控 127.0.0.1:9200 ──────────────┐  │   │  mcp/server.py（纯透传代理，无     │
│  │ app/api/*       认证/蜜饵/话术/账号/分级/    │  │   │  业务逻辑；55 个 MCP 工具透传 101  │
│  │                证据/案例/知乎通道/告警/系统  │  │   │  REST 端点（101 paths，httpx 透传）│
│  │ app/services/*  zhihu_bridge · speech_engine│  │   │  env: AF_MASTER_URL / AF_API_KEY  │
│  │                · trap_engine · llm_provider │  │   └───────────────────────────────┘
│  │                · grading · evidence · cases │  │
│  │ SQLite (data/af.db) · 降级链（LLM→规则）     │  │
│  └─────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

- **D1 铁律**：**WebUI、MCP、curl 全部是主控 REST API 的客户端**，业务逻辑只在主控一份。MCP 是"透传代理"不是第二套实现——独立可用和 agent 可用天然一致。
- **降级链（D5）**：LLM 不可用 → 规则 + ML（若有）→ 纯规则词库，系统不瘫；LLM 未配置时主控照常启动（health 显示 `not_configured`）。
- **认证（D6）**：bootstrap admin key（首次启动生成 `data/bootstrap_admin_key.txt`）+ 敏感操作 `confirm+reason` 闸控。

---

## 3. 功能主线 R1–R6

| 环 | 主线 | 实现 | 关键 DoD 证据 |
|---|---|---|---|
| R1 | **蜜饵诱捕** | `trap_engine.py`：草稿生成（指纹 UUID + 伪装度校验）→ deployed → monitored → hit → retired；踩饵检测（SimHash 预筛 + 改写归一化；embedding 精排为可选扩展，P3-1）；命中即退役并联动检测升级 L5（R1 修复：trap_hit_escalated 事件） | 改写 5-10 字仍召回；命中即退役 |
| R2 | **话术识别** | `speech_engine.py`：规则词库预筛 →（可选 LLM 复核）→ judge 置信度；`/scan/text` | 10 条已知话术 100% 命中；正常语料 0 误报 |
| R3 | **账号速查** | `account_intel.py`：公开信号评分 + 踩饵记录 + 共现团伙提示 | 只用公开信号（私有信号白名单丢弃） |
| R4 | **五级分级** | `grading.py`：话术/踩饵/账号信号融合 → L1–L5；L3+ 才推告警 | 分级可解释（证据链）；≤L3 无正式报告 |
| R5 | **证据包** | `evidence.py`：截图/哈希链/时间戳/冻结/导出/举报模板（96110/平台/辟谣） | 冻结后哈希不可变（篡改即败） |
| R6 | **案例库** | `desensitize.py` + `cases.py`：正则+LLM 双层脱敏 → 脱敏案例入库/查询/骗术图谱 | 强制脱敏；含敏感信息拒绝入库 |
| P19 | **反钓鱼伪装** | `cover.py`：伪装身份档案（parent/layer 嵌套 + 假信息字段）→ 主动暴露（HITL 脱敏清单）→ 骗子接触登记（脱敏落库 + IOC 掩码 + 同号二次锁定 escalated）→ 伪装地图 | 全部端点 require_admin；toggle/delete confirm+reason 审计；明文不落库 |

---

## 4. 快速开始（5 分钟）

### 4.1 前置条件

- Windows（本说明）或 Linux/macOS；Python **3.11+**（开发环境 3.12.10 验证）
- Node.js 18+（仅 WebUI 构建需要；不构建可直接用 `webui/dist/` 现成产物）

### 4.2 安装

```bat
:: ① 创建虚拟环境并装依赖
cd /d C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

:: ② （可选）构建 WebUI 生产包（已有 webui/dist/ 可跳过）
cd webui
npm install
npm run build
cd ..
```

### 4.3 启动主控（独立软件形态）

```bat
:: 方式一：一键脚本 start.bat（cmd，纯 ASCII、任意代码页可靠运行，建议）
start.bat

:: 方式二：一键脚本 start.ps1（PowerShell 版，中文提示，UTF-8）
powershell -ExecutionPolicy Bypass -File start.ps1

:: 方式三：手动（不经过一键脚本）
.venv\Scripts\python.exe -m app.run --host 127.0.0.1 --port 9200
```

**start.bat 三态**：① 主控未运行 → 新窗口启动 + 健康探测（20s）→ 自动打开浏览器到
`http://127.0.0.1:9200/ui/`；② 已运行 → 不重复启动，只多开一个浏览器标签页；③ 健康
探测失败 → 提示查看主控窗口日志，不静默退出。

R1 修复（P1-5）：start.bat / start.ps1 增加**单实例互斥**——按 9200 监听 PID 检测已运行实例，
并清理命令行含 `app.run --port 9200` 但**非监听 PID、非监听父进程**的僵尸实例（保护 venv 重定向
父进程，CIM 不可用时跳过清理、绝不阻塞启动）。
R3 修复（P3-5）：僵尸匹配精确为 `--port 9200`（start.bat 原 `-match '9200'` 过宽）；两脚本启动后
等待 ~2s 复核端口归属，同秒双启动中落败进程被清理，收敛为单实例。

配套脚本：`stop.bat`（按 PID 停止主控及其子进程树）、`open-console.bat`（打开项目
命令行：自动 cd 到项目根 + 激活 venv + 常用命令提示，窗口保持）。

首次启动自动完成：建 `data/` 目录 → 生成 admin key → 幂等建表（24 张表，含 P19 反钓鱼伪装）→ 种子话术词库。

### 4.4 登录：本地打开即用，无需手工配 key

**推荐路径：`start.bat` → 浏览器自动打开 `/ui/` → 登录页点"一键登录（本机）"按钮**——
WebUI 调用 `POST /api/v1/auth/local-login`（仅本机回环可调）直接拿到 admin key 完成登录，
无需读文件、无需配环境变量。

需要手动拿 key 时：

```bat
:: admin key 在 data/bootstrap_admin_key.txt（格式 af_admin_xxxxxxxx）
type data\bootstrap_admin_key.txt

:: 或本机命令行直接取（仅回环可调）
curl -X POST http://127.0.0.1:9200/api/v1/auth/local-login
```

设置页（本机）可查看当前 key（`GET /api/v1/auth/current-key`）与重置 key
（`POST /api/v1/auth/reset-key`：confirm=true + reason≥20 字，写 GateLog 审计）。

| 入口 | 地址 | 说明 |
|---|---|---|
| WebUI 管理台 | http://127.0.0.1:9200/ui/ | 登录页支持**一键登录（本机）**按钮（免输 key，仅本机生效）或粘贴 admin key；进入仪表盘/蜜饵/话术检测/账号/证据/案例/伪装账号等 25 页面路由（含 /cover）（凭据**会话级存储**，关闭浏览器即失效；顶栏脱敏显示，点击可复制） |
| Swagger UI | http://127.0.0.1:9200/docs | 交互式 API 文档 |
| OpenAPI JSON | http://127.0.0.1:9200/openapi.json | 机器可读（交付副本 `docs/openapi.json`） |
| 健康检查 | http://127.0.0.1:9200/api/v1/system/health | 四灯：主控/DB/LLM/知乎通道 |

示例（curl）：

```bash
# 健康
curl http://127.0.0.1:9200/api/v1/system/health

# 话术检测（无需 key）
curl -X POST http://127.0.0.1:9200/api/v1/scan/text \
  -H "Content-Type: application/json" \
  -d '{"text":"杀猪盘带你投资稳赚不赔，加老师微信"}'

# 受保护端点（需 key）
curl http://127.0.0.1:9200/api/v1/system/stats -H "X-API-Key: af_admin_xxxxxxxx"
```

### 4.5 MCP 接入（agent 形态）

见 [§6 MCP 接入](#6-mcp-接入agent-形态) 与 `docs/06-MCP集成.md`。

> 详细步骤见 `docs/01-快速开始.md`、`docs/03-部署.md`。

---

## 5. API 摘要

统一前缀 `/api/v1`；响应统一封包：成功 `{ok:true, data}`，失败 `{ok:false, error:{code, message}}`。
认证：`X-API-Key` 请求头（或 `Authorization: Bearer`），admin key 见 `data/bootstrap_admin_key.txt`。

| 分组 | 端点 | 说明 |
|---|---|---|
| 系统 | `GET /system/health`、`GET /system/stats` 🔒、`GET /system/embedded-dsh` 🔒、`GET /system/audit-verify` 🔒 | 四灯健康 / 运行统计 / 内嵌 DSH 实例状态 / 审计哈希链自校验 |
| 认证 | `POST /auth/bootstrap-info`、`POST /auth/local-login`（仅回环）、`GET /auth/current-key`（仅回环）、`POST /auth/reset-key`（仅回环，confirm+reason） | bootstrap 信息 / 本机一键登录 / 查看、重置 key |
| 蜜饵 R1 | `GET|POST /traps`(写 🔒)、`POST /traps/generate-draft` 🔒、`POST /traps/{id}/deploy|disable|retire` 🔒（retire 需 confirm+reason）、`POST /traps/{id}/check-hit`（R2：命中即退役 + 联动检测升级 L5，可选 `detection_id`） | 蜜饵全生命周期（草稿 HITL 不发布；手工 check-hit 命中联动升级，R2 修复） |
| 话术 R2 | `POST /scan/text`、`GET /scan/hits`、`POST /scan/inbox` 🔒、`GET /scan/inbox` 🔒、`POST /zhihu/import` 🔒 | 单条判定 / 历史命中 / inbox 批量消费 pending（读取含原文需认证） / CH-D 手动导入 |
| 账号 R3 | `POST /accounts/check`、`GET /accounts/{url_name}`、`GET /accounts/{id}/timeline` | 绿黄红速查 / 画像 / 时间线 |
| 分级 R4 | `GET /grading/levels`、`GET /grading/explain/{id}` | 级别定义 / 可解释证据链 |
| 证据 R5 | `POST /evidence/build` 🔒、`GET /evidence/{id}` 🔒、`POST /evidence/{id}/freeze` 🔒、`GET /evidence/{id}/export` 🔒、`GET /evidence/{id}/report-template` | 构建/查看/冻结/导出/举报模板 |
| 案例 R6 | `GET /cases`、`POST /cases/publish` 🔒（脱敏前置）、`GET /cases/graph` | 查询 / 脱敏入库 / 骗术图谱 |
| 事件 | `GET /events` 🔒、`GET /events/{id}` 🔒 | 事件流（分页/kind 过滤） |
| 配置 | `GET /config`、`PUT /config` 🔒（confirm+reason 闸控） | 合成配置（密钥屏蔽）/ 敏感更新（gate_logs 审计） |
| 知乎 | `GET /zhihu/channels`、`POST /zhihu/channels/{id}/login|verify`、`GET /zhihu/status`、`POST /zhihu/import` | 通道健康 / cookie 注入（用户手动导出）/ 状态 |
| 告警 | `GET /alerts` 🔒、`POST /alerts/{id}/read` 🔒 | L3+ 告警 / 已读 |
| 账号桥接 P9 | `GET /account-bridges` 🔒、`GET /account-bridges/{platform}` 🔒、`PUT /account-bridges/{platform}/config` 🔒、`POST /account-bridges/{platform}/test|ingest` 🔒、`GET /account-bridges/{platform}/agent-import` 🔒、`DELETE /account-bridges/{platform}` 🔒 | 平台矩阵/详情/配置（Fernet 加密）/测试/私信接入/删除 |
| 采集矩阵 P10 | `GET|POST /collector/matrix` 🔒、`PUT|DELETE /collector/matrix/{id}` 🔒、`POST /collector/matrix/{id}/run` 🔒、`POST /collector/matrix/run-all` 🔒、`GET /collector/tech-stack` 🔒、`GET /collector/platforms` 🔒 | 格子 CRUD / 立即执行（真实网络采集）/ 技术栈调研 / 平台清单 |
| 供应链 P10 | `POST /supply-chain/query` 🔒、`GET /supply-chain/results` 🔒、`GET /supply-chain/results/{name}` 🔒 | 反查起链（DNS/相似域）/ agent workflow 报告列表/详情 |
| 聊天导入 P11 | `GET /chat-import/candidates` 🔒、`POST /chat-import/scan` 🔒、`POST /chat-import/import` 🔒、`POST /chat-import/import-file` 🔒、`DELETE /chat-import/imported` 🔒 | 聊天记录候选 / 扫描 / 导入（含文件）/ 清空已导入 |
| 人设 P12 | `POST /persona/generate` 🔒、`POST /persona/schedule` 🔒、`GET /persona/schedules` 🔒、`POST /persona/schedules/{id}/pause|resume` 🔒、`POST /persona/publish-now` 🔒、`GET /persona/posts` 🔒、`GET|POST /persona/replies` 🔒、`POST /persona/replies/{id}/toggle` 🔒、`DELETE /persona/replies/{id}` 🔒、`POST /persona/respond` 🔒、`POST /persona/drill` 🔒、`GET /persona/drills` 🔒 | 人设话术生成 / 排期 / 发布 / 自动回复 / 演练（HITL，发布需本人确认） |
| 安全运营 | `POST /soc-lib/query` 🔒、`POST /soc-lib/analyze/{det_id}` 🔒、`GET /soc-lib/status` 🔒 | SOC-Lib 安全运营词库查询 / 案件分析 / 状态 |
| 检测详情 | `GET /detections/{det_id}/full` 🔒、`GET /detections/{det_id}/platform-link` 🔒 | 单条检测完整证据链 / 平台链接 |
| 反钓鱼伪装 P19 | `POST|GET /cover/identities` 🔒、`POST /cover/identities/{id}/toggle` 🔒、`DELETE /cover/identities/{id}` 🔒（confirm+reason）、`POST /cover/identities/{id}/expose` 🔒、`POST /cover/contact` 🔒、`GET /cover/contacts` 🔒、`GET /cover/map` 🔒 | 伪装身份档案 / 假信息 HITL 暴露 / 接触登记（脱敏+IOC+同号锁定）/ 伪装地图（7 paths / 8 ops） |

🔒 = 需 admin key（仅回环端点单独注明）。完整结构见 `docs/openapi.json`（101 paths / 111 operations）与 `docs/02-API.md`。

---

## 6. MCP 接入（agent 形态）

MCP Server = 主控 REST API 的**纯透传代理**（`mcp/server.py`，无业务逻辑）。**55 个 MCP 工具**（`af_` 前缀）透传主控 101 个 REST 端点中已映射的 54 个（cover 反钓鱼伪装端点与 persona 等一致未映射 MCP 工具，仅 REST/WebUI 可用）：`af_health` / `af_scan_text` / `af_list_traps` / `af_generate_trap_draft` / `af_build_evidence` / `af_publish_case` …（完整清单见 `docs/06-MCP集成.md` §3）

### 6.1 接入配置（dsh / Claude Code / Codex 通用 `mcpServers` 格式）

复制以下 JSON，**替换占位路径为你的真实路径**：

```json
{
  "mcpServers": {
    "af-honeypot": {
      "command": "<项目根目录>\\.venv\\Scripts\\python.exe",
      "args": ["<项目根目录>\\mcp\\server.py"],
      "env": {
        "AF_MASTER_URL": "http://127.0.0.1:9200",
        "AF_API_KEY": "<admin_key>"
      }
    }
  }
}
```

**占位符替换规则**（真实路径 = `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`）：

| 占位符 | 替换为（Windows 示例） | 说明 |
|---|---|---|
| `<项目根目录>` | `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐` | JSON 中反斜杠需转义为 `\\`（双反斜杠） |
| `<admin_key>` | `data\bootstrap_admin_key.txt` 内容（`af_admin_xxxxxxxx`） | 主控 admin key，经 `X-API-Key` 透传 |

- **本机自动生成**：接入后调用工具 `af_register_agent` 即可返回带真实路径的完整配置 JSON，免手工替换。
- **streamable-http 方式**：另开终端 `python mcp\server.py --http --port 9201`，客户端连接 `http://127.0.0.1:9201/mcp`。
- **验证三连**：接入后依次调用 `af_health` → `af_scan_text`（传一句反诈话术）→ `af_list_traps`。

### 6.2 MCP 挂载要点（排障）

- 项目内 `mcp/` 目录**刻意不建 `__init__.py`**（命名空间段），避免遮蔽官方 `mcp` SDK 包——请勿添加该文件。
- 工具清单 / 敏感操作闸控 / HITL 约定见 `docs/06-MCP集成.md`。

### 6.3 敏感操作闸控（agent 调用前置条件）

以下 MCP 工具对应主控的**敏感写操作**，必须携带 `confirm=true` 且 `reason≥20` 字说明，否则被主控强制拒绝（403 `confirm_required` / 422 `reason_too_short`），成功操作会写入 `gate_logs` 哈希链审计：

| 工具 | 对应端点 | 说明 |
|---|---|---|
| `af_retire_trap` | `POST /traps/{id}/retire` 🔒 | 退饵（含 confirm+reason 闸控） |
| `af_freeze_evidence` | `POST /evidence/{id}/freeze` 🔒 | 冻结证据包（含 confirm+reason 闸控；二次冻结 409 already_frozen） |
| `af_publish_case` | `POST /cases/publish` 🔒 | 案例入库发布（脱敏前置 + confirm+reason 闸控；同检测重复发布：同载荷幂等返回、异载荷 409 case_exists） |
| `af_config_update` | `PUT /config` 🔒 | 敏感配置更新（confirm+reason + GateLog 审计） |

调用示例：`af_publish_case(det_id=1, redacted_payload="…", confirm=true, reason="该案例已完成脱敏，申请入库备查")`。
认证（D6）：以上写操作与 `GET /system/stats`、`POST /zhihu/channels/{id}/login|verify`、`POST /zhihu/import`、`POST /scan/inbox`、`GET /scan/inbox`、`POST /evidence/build`、`POST /evidence/{id}/freeze`、`GET /evidence/{id}/export`、`/account-bridges/*`、`/collector/*`、`/supply-chain/*`、`/system/embedded-dsh` 均需 admin key（`X-API-Key` 透传，MCP 端经 `AF_API_KEY` 注入）。

---

## 7. 目录结构

```
金丝雀蜜罐/
├── app/                 # FastAPI 主控（唯一业务内核，D1）
│   ├── main.py run.py config.py db.py deps.py utils.py
│   ├── api/             # auth traps scan accounts grading evidence cases zhihu alerts system
│   ├── models/          # 24 张表（幂等 ensure_schema，含 P19 反钓鱼伪装）
│   └── services/        # zhihu_bridge speech_engine trap_engine llm_provider grading
│                        # account_intel evidence desensitize cases alerting
├── mcp/server.py        # MCP 透传代理（stdio + --http；55 工具）
├── webui/               # Vue3 + Vite + Element Plus + ECharts（25 路由 / 25 视图，含 /cover）
│   └── dist/            # 生产构建（主控 /ui/ 静态托管）
├── docs/                # 文档索引（00-项目概述 01-快速开始 02-API 03-部署 04-知乎通道 05-WebUI 06-MCP集成 07-测试）
├── data/                # 运行时：af.db · bootstrap_admin_key.txt · secret.key（勿提交）
├── scripts/             # export_openapi.py + cleanup_duplicates.py（存量去重清洗）+ 各阶段检查脚本
├── tests/               # pytest（479 用例）+ TEST_REPORT.md / FULL_TEST_REPORT.md
├── requirements.txt · pytest.ini · start.bat · .env.example
├── VERSION · CHANGELOG.md · README.md
```

---

## 8. 测试与质量

- 全量测试：`pytest -q`（项目根）→ **479 passed**（32 个测试文件，P0–P19 全量 + 桥接/采集/供应链/聊天导入/人设/话术库/知识库/反钓鱼伪装 + MCP 55 工具透传 + 安全与工程修复用例 + 严格验收 R1/R2/R3 修复回归 test_r6_r1fixes/test_r7_r2fixes/test_r8_r3fixes + P19 专项 test_p19_cover，含真实主控集成）
- 存量数据维护：`scripts/cleanup_duplicates.py`（幂等，`--dry-run` 预演；清洗重复案例/冗余证据包，冻结包永不删除；执行前自动备份到 `var/backups/` 并写审计，R2 已线上执行：cases 1695→80、evidence 738→103）
- 报告：`tests/TEST_REPORT.md` ｜ OpenAPI 导出：`scripts/export_openapi.py` → `docs/openapi.json`

```bash
.venv\Scripts\python.exe -m pytest -q        # 全量
.venv\Scripts\python.exe -m pytest tests/test_mcp_mount.py -v   # MCP 专项
.venv\Scripts\python.exe scripts\export_openapi.py              # 重新导出 OpenAPI
```

---

## 9. 版本与变更

- 当前版本：**v1.0.0**（见 `VERSION`、`CHANGELOG.md`、`app/__init__.py::__version__`，三者一致）
- P19 反钓鱼伪装（`/api/v1/cover` + `/ui/cover` 伪装账号页）已随 v1.0.0 交付：全量 pytest 479 passed（2026-09-17 队C 复验，基线 462 + 新增 17）。
- 依据《03_封装规范与版本管理.md》维护版本：内容变更 → CHANGELOG 记录 → VERSION 提升。