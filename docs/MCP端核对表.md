# MCP 工具 × 主控端点核对表（R4 产物 · 历史快照；当前状态见下方 R1 复核更新）

> 文档编号：docs/MCP端核对表.md（R4 修复产物，配套 docs/06-MCP集成.md）
> 事实源：`docs/openapi.json`（37 paths / 40 operations，v1.0.0）+ `app/api/*.py` + `mcp/server.py`
> 核对结论（R4 时点）：**36/36 工具方法/路径/参数名与主控一一对应（无透传 422 的字段名漂移）；5 个端点无对应工具（见 §3）**

---

## 0. R1 复核更新（2026-09-17，严格验收第 1 轮）

> 本表原为 R4 时点快照（36 工具 × 37 paths），**当前代码已演进**，本表 §2–§5 明细为历史记录，不再逐行修订。
> **当前事实（R1 实测）**：

| 维度 | R4 时点（本表） | 当前实际（R1 复核） |
|---|---|---|
| openapi paths | 37 | **94**（103 operations / 46 schemas，R4 时点线上与 docs/openapi.json 一致；当前为 101 paths，见上注） |
| MCP 工具 | 36 | **55**（mcp/server.py 实测 55 个 `@server.tool()`，全量清单见 `docs/06-MCP集成.md` §3） |
| 工具覆盖端点 | 31/37 | **50/94**（55 工具覆盖 50 个路径；44 个端点无工具） |
| 无工具端点 | 5 | **44**（含 auth×4、persona×12、chat-import×5、soc-lib×3、knowledge×10、speech-patterns×5、collector/platforms、supply-chain/results/{name}、accounts/timeline、traps/{id}/monitor、system/audit-verify 等；auth 引导与全部本机端点豁免） |
| 敏感操作 confirm+reason | 3/3 | 仍为 3/3（retire/publish/config_update）+ **af_freeze_evidence 亦携带 confirm+reason 闸控参数** |
| af_check_trap_hit 方法 | GET（本表 §2.1 注：待 t3 同步 POST） | **已改 POST**（`POST /api/v1/traps/{id}/check-hit`，body `{text}`，主控 P2-4 落地） |

> 下表（§2–§5）保留 R4 原文作历史对照；当前权威清单 = `docs/06-MCP集成.md` §3（55 工具）+ `docs/openapi.json`（101 paths）。
> P19 反钓鱼伪装后补充注：主控 OpenAPI 已更新为 **101 paths / 111 ops / 49 schemas**（+ /api/v1/cover 7 paths），表数 24（+ cover_identities/contact_events），WebUI 25 路由/25 视图（+ /cover）；MCP 55 工具不变（cover 无 MCP 工具，仅 REST/WebUI）。

---

## 1. 核对结果总览

| 维度 | 结果 |
|---|---|
| openapi paths | 37（/api/v1 前缀） |
| MCP 工具 | 36（`af_` 前缀） |
| 方法一致 | 36/36 ✅ |
| 路径一致 | 36/36 ✅ |
| query/body 参数名一致 | 36/36 ✅（R4 修正 2 处：af_list_events、af_scan_inbox） |
| 敏感操作 confirm+reason 透传 | 3/3 ✅（retire/publish/config_update，真实主控实测拦截/放行） |
| 无工具端点 | 5（§3，其中 1 个为认证引导端点豁免） |

---

## 2. 逐工具核对明细

### 2.1 蜜饵（R1，/traps）— 7 工具

| MCP 工具 | 方法 | 路径 | 参数（MCP → 后端字段） | 状态 |
|---|---|---|---|---|
| `af_list_traps` | GET | `/api/v1/traps` | status→query:status，limit→query:limit | ✅ |
| `af_get_trap` | GET | `/api/v1/traps/{trap_id}` | trap_id→path | ✅ |
| `af_generate_trap_draft` | POST | `/api/v1/traps/generate-draft` | template_id/platform/note→body | ✅ |
| `af_mark_trap_deployed` | POST | `/api/v1/traps/{trap_id}/deploy` | target_url→body | ✅ |
| `af_disable_trap` | POST | `/api/v1/traps/{trap_id}/disable` | — | ✅ |
| `af_retire_trap` | POST | `/api/v1/traps/{trap_id}/retire` | confirm/reason→body（敏感闸控） | ✅ R4 实测 |
| `af_check_trap_hit` | GET | `/api/v1/traps/{trap_id}/check-hit` | text→query | ✅（见 §4 注） |

### 2.2 检测（R2/R4，/scan /accounts /grading）— 7 工具

| MCP 工具 | 方法 | 路径 | 参数（MCP → 后端字段） | 状态 |
|---|---|---|---|---|
| `af_scan_text` | POST | `/api/v1/scan/text` | text/source→body | ✅ |
| `af_scan_inbox` | POST | `/api/v1/scan/inbox` | **无 body**（R4 修正：原 channel/limit 为后端不存在的字段） | ✅ R4 |
| `af_list_scan_hits` | GET | `/api/v1/scan/hits` | limit→query | ✅ |
| `af_account_check` | POST | `/api/v1/accounts/check` | url_name/signals→body | ✅ |
| `af_account_get` | GET | `/api/v1/accounts/{url_name}` | url_name→path | ✅ |
| `af_grade_explain` | GET | `/api/v1/grading/explain/{detection_id}` | trap_hit_count/account_risk→query | ✅ |
| `af_grading_levels` | GET | `/api/v1/grading/levels` | — | ✅ |

### 2.3 情报（R5/R6，/evidence /cases）— 8 工具

| MCP 工具 | 方法 | 路径 | 参数（MCP → 后端字段） | 状态 |
|---|---|---|---|---|
| `af_build_evidence` | POST | `/api/v1/evidence/build` | det_ids/screenshots→body | ✅ |
| `af_get_evidence` | GET | `/api/v1/evidence/{pkg_id}` | pkg_id→path | ✅ |
| `af_freeze_evidence` | POST | `/api/v1/evidence/{pkg_id}/freeze` | — | ✅ |
| `af_export_evidence` | GET | `/api/v1/evidence/{pkg_id}/export` | fmt→query | ✅ |
| `af_report_template` | GET | `/api/v1/evidence/{pkg_id}/report-template` | kind→query | ✅ |
| `af_list_cases` | GET | `/api/v1/cases` | page/page_size/tag→query | ✅ |
| `af_publish_case` | POST | `/api/v1/cases/publish` | det_id/redacted_payload/desensitize_log/graph_tags/confirm/reason→body（敏感闸控） | ✅ R4 实测 |
| `af_case_graph` | GET | `/api/v1/cases/graph` | — | ✅ |

### 2.4 事件与告警（/events /alerts）— 4 工具

| MCP 工具 | 方法 | 路径 | 参数（MCP → 后端字段） | 状态 |
|---|---|---|---|---|
| `af_list_events` | GET | `/api/v1/events` | kind/page/page_size→query（**R4 修正：原 limit 后端不存在**） | ✅ R4 |
| `af_get_event` | GET | `/api/v1/events/{event_id}` | event_id→path | ✅ |
| `af_list_alerts` | GET | `/api/v1/alerts` | level/unread_only/limit→query | ✅ |
| `af_mark_alert_read` | POST | `/api/v1/alerts/{alert_id}/read` | alert_id→path | ✅ |

### 2.5 运维（/system /zhihu /config）— 10 工具

| MCP 工具 | 方法 | 路径 | 参数（MCP → 后端字段） | 状态 |
|---|---|---|---|---|
| `af_health` | GET | `/api/v1/system/health` | — | ✅ |
| `af_stats` | GET | `/api/v1/system/stats` | — | ✅ |
| `af_zhihu_channels` | GET | `/api/v1/zhihu/channels` | — | ✅ |
| `af_zhihu_status` | GET | `/api/v1/zhihu/status` | — | ✅ |
| `af_zhihu_login` | POST | `/api/v1/zhihu/channels/{channel}/login` | cookie/scheme→body | ✅ |
| `af_zhihu_verify` | POST | `/api/v1/zhihu/channels/{channel}/verify` | — | ✅ |
| `af_zhihu_import` | POST | `/api/v1/zhihu/import` | text/source/from_url_name→body | ✅ |
| `af_config` | GET | `/api/v1/config` | — | ✅ |
| `af_config_update` | PUT | `/api/v1/config` | key/value/confirm/reason→body（敏感闸控） | ✅ R4 实测 |
| `af_register_agent` | — | （本机自描述，不发主控请求） | — | ✅ |

---

## 3. 无对应工具的端点（5 个）

| 端点 | 方法 | 说明 | 处置建议 |
|---|---|---|---|
| `/api/v1/auth/bootstrap-info` | POST | 认证引导端点（返回 key 是否生成，不含 key） | **豁免**：非业务能力，MCP 无必要暴露 |
| `/api/v1/scan/inbox`（查结果） | GET | inbox 扫描结果查询（limit query） | 建议后续补 `af_scan_inbox_result`（或复用 af_list_scan_hits） |
| `/api/v1/traps`（创建） | POST | 用户自拟文案创建蜜饵草稿（bait_text/note） | 建议后续补 `af_create_trap`（HITL 只产草稿） |
| `/api/v1/traps/{trap_id}/monitor` | POST | 进入监控（active→monitored） | 建议后续补 `af_monitor_trap`（蜜饵状态机一环） |
| `/api/v1/accounts/{account_id}/timeline` | GET | 账号时间线（画像更新+踩饵记录） | 建议后续补 `af_account_timeline` |

> 以上 5 个端点缺工具不影响既有 36 工具的正确性（未挂载≠透传错误），列为后续增强候选。

---

## 4. R4 修正记录

| 位置 | 修正前 | 修正后 | 原因 |
|---|---|---|---|
| `af_list_events` 签名 | `(kind, limit=50)` | `(kind, page=1, page_size=20)` | 主控 GET /events 参数为 kind/page/page_size（openapi.json），limit 被 FastAPI 静默忽略 |
| `af_scan_inbox` 签名 | `(channel="ch-a", limit=20)` 且 POST body | 无参 POST（无 body） | 主控 POST /scan/inbox 无请求体（app/api/scan.py:60），channel/limit 为后端不存在的字段 |
| 5 个工具 docstring「【规划】尚未落地」 | 标注规划 | 改为真实行为说明 | /events、/config、/scan/inbox 已随 P6.5 落地（docs/06-MCP集成.md §3） |
| `af_retire_trap` / `af_publish_case` docstring | 「confirm+reason 透传闸控」 | 「主控强制 confirm=true + reason≥20，GateLog 审计」 | 后端 t2 已实现强制闸控（app/api/traps.py RetireRequest、app/api/cases.py PublishCaseRequest） |

> **注：af_check_trap_hit 方法待同步**。审查报告 P2-4 建议 GET→POST（避免 GET 状态迁移副作用）；
> backend-core 任务 t3 若将主控端点改为 POST，`mcp/server.py` 的 `af_check_trap_hit` 需同步由 GET 改 POST。
> 当前（R4 交付时）主控仍为 GET，MCP 保持一致；t3 落地后由复查确认。

---

## 5. 联动验证证据（R4 · 真实主控）

测试：`tests/test_mcp_mount.py::test_live_master_gate_confirm`（uvicorn 子进程 + 真实 HTTP）

| 工具 | confirm=false | confirm=true + reason<20 | confirm=true + reason≥20 |
|---|---|---|---|
| `af_config_update` | 403 `confirm_required` | 422 `reason_too_short` | 200 成功（gate_logs 落 `config.put`） |
| `af_retire_trap` | 403 `confirm_required` | 422 `reason_too_short` | 200 成功（status=retired，gate_logs 落 `trap.retire`） |
| `af_publish_case` | 403 `confirm_required` | 422 `reason_too_short` | 200 成功（gate_logs 落 `case.publish`） |

gate_logs 哈希链核对：`{config.put, trap.retire, case.publish} ⊆ gate_logs.action`，reason 原样落库。
错误经 `ProxyError` 原样透传（code/message 不加工不吞并，符合 D1 铁律）。

---

## 6. 结论

- MCP 层纯透传契约成立：36 工具的方法/路径/参数名与主控 openapi.json 全量一致，无字段名漂移导致 422。
- 敏感操作三工具（retire/publish/config_update）的 confirm+reason 已由主控强制闸控并产生 GateLog 审计，MCP 透传实测通过。
- 5 个端点缺工具为增强候选（非缺陷）；af_check_trap_hit 的 GET→POST 跟随 t3 落地后同步。
