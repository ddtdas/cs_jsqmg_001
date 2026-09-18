# 验收B队_代码审计报告（金丝雀蜜罐 · 双盲只读审计）

- 审计方：B队（代码审计，双盲；结论与 A 队互不可见）
- 审计方式：只读。经 `ssh 192.168.10.110` + PowerShell `-EncodedCommand`（Unicode-Base64）+ `[IO.File]::ReadAllText/ReadAllBytes` 读取远程文件（UTF-8 回传），**未修改任何项目文件、未运行测试、未重启任何服务**。唯一写入物为本报告（docs/验收B队_代码审计.md 远程 + 本地副本）。
- 远程根目录：`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`
- 审计基线：任务清单 7 项 + 回归审计
- 审计日期：2026-09-17

---

## 一、总体结论

代码整体质量高：统一封包、防御式降级、敏感操作双确认 + GateLog 链式审计、脱敏强制、单事务写入等设计落地扎实。**未发现 P0**。发现 **P1×1、P2×6、P3×若干**，均为可修复项，不构成阻断验收的硬伤（除 P1 外均不影响运行安全）。

P1（唯一）：
- **公开无鉴权 `GET /api/v1/cases` 会完整回显 `desensitize_log`**，而该字段可携带脱敏前的**原文**（如手机号/身份证）——违反 D7「公开查询只暴露脱敏载荷」。当前 WebUI 发布路径不传该字段（存 `[]`），但 MCP `af_publish_case` 透传该参数、`DesensitizeService.regex_redact` 的 `replaced` 日志格式即为 `{from: 原文, to: 掩码}`，一旦任何调用方按服务层约定携带日志即构成公开泄露。属设计缺陷，建议一行修复（公开响应剥离该字段或入库前移除 from 明文）。

分项结论速览：

| 审计对象 | 结论 |
|---|---|
| 1. soc_lib.py 社工库 | 通过（降级链/掩码/明文不落库全部达标；IOC 正则存在 QQ↔手机重叠误报） |
| 2. persona_sim 服务+API+调度 | 通过（模板无残留、时间推进/total 上限/幂等正确；API 入参缺校验） |
| 3. chat_import.py | **不存在**（全工程 0 命中；若属本期验收范围则为缺项） |
| 4. se_analysis.py + detections | 通过（坏 JSON 全链路容错；divergence/soc_analysis 无生产者，前端死代码） |
| 5. webui BoardView/DetailDrawer/client.js | 通过（无 v-html、防御式加载、来源标注真实；若干 P3） |
| 6. dsh/ 干净性 | 部分通过（cordis.patch.yml=3092 ✓、CANARY_DSH_PORT/WMI/UTF-8 ✓、无全局 junction ✓；**dsh-home 非空白且残留授权凭据** ✗） |
| 7. 回归审计 | 通过（grading 单事务 ✓、evidence freeze 双确认 ✓、cases 脱敏 ✓、supply_chain 超长域名 ✓、MCP=55 工具 ✓、openapi=59 paths ✓；cases desensitize_log 公开回显 ✗ P1） |

---

## 二、分项审计明细

### 1. app/services/soc_lib.py（9765B）+ app/api/soc_lib.py（3135B）

| 检查点 | 结论 | 问题（文件:行） | 严重级 |
|---|---|---|---|
| extract_iocs 正则误报（QQ vs 手机重叠） | **存在** | `soc_lib.py:12` `_RE_QQ = r"[1-9]\d{4,10}"`（5-11 位数字）与 `:10` `_RE_PHONE = r"1[3-9]\d{9}"`（11 位）重叠：任何手机号同时被提为 QQ；任意 5-11 位数字串（时间戳/订单号/邮箱数字段/日期）均命中 QQ；手机正则无 `(?<!\d)(?!\d)` 边界，可命中长数字串内部子串（对照 desensitize.py 同型正则均带边界）。后果：analyze() 对同一号码以 phones 与 qq 双类型重复查询，本地命中时写两条 `soc_lib_hit` 事件（重复/误标情报） | P2 |
| 同上（wechat 过宽） | 存在 | `soc_lib.py:13` `_RE_WECHAT = r"[a-zA-Z][a-zA-Z0-9_-]{5,19}"` 命中任意 6-20 位字母开头串（token/长单词/hex）→ 误报 | P3 |
| query 降级路径全捕获（HIBP 失败/无 key/本地表缺/限流/网络） | **通过** | `query()` 外层 try/except 兜底（`soc_lib.py` query()）；`_query_hibp` 对无 key、非 email、httpx 缺失、429/5xx/网络异常一律返回 None（`_query_hibp`）；`_query_local` 对表缺失/异常返回 None（`_query_local`）；最终统一 `{source:'none', found:false, detail:'社工库不可用(降级)'}`。HIBP 200 坏 JSON 也落入 query 兜底 | — |
| analyze 掩码 + events | 通过 | 逐类取首个 IOC、`_mask` 掩码（手机/卡 首2尾2、邮箱 首1@域名、QQ/微信 首2尾1）；命中仅写 `events(kind='soc_lib_hit')`，payload 只含掩码（`_record_hit`）；单类异常降级不中断（analyze 内 try/except） | — |
| 明文不落库（只 sha256） | 通过 | 本地库仅按 `sha256(value)` 匹配 `leak_records.ioc_value_hash`（`_query_local`）；表结构无明文列且 UNIQUE（`models/__init__.py:230-240`）；演示种子仅存哈希（`models/__init__.py:305-326`） | — |
| API 层 | 通过 | 三端点全 `require_admin`；`/query` 三选一入参校验；缺参 400；`/analyze/{det_id}` 检测不存在 404 | — |
| 次要 | 存在 | `/query` 的 email/phone/ioc_value 无长度上限（长串进 HIBP URL 由 httpx 异常兜底，不崩）；`ioc_type` 未枚举校验（任意值走 phone 分支） | P3 |

### 2. app/services/persona_sim.py（12234B）+ app/api/persona_sim.py（4143B）+ scheduler job_persona_sim

| 检查点 | 结论 | 问题（文件:行） | 严重级 |
|---|---|---|---|
| 模板占位符残留 | 通过 | 5 类 38 条模板全部含且仅含 `{time}{place}{weather}{mood}`，`generate_work` 全量 `.format()` 填充；type 不在池内自动随机 | — |
| publish_due 时间推进 / total 上限 / 幂等 | 通过 | `publish_due`：`WHERE enabled=1 AND next_run_at<=now AND published<total`（上限在查询内）；每条 INSERT persona_posts + `published=published+1` + `next_run_at=now+interval` 同事务一次 commit；单条异常 rollback 跳过；job 每分钟轮询仅处理到期行 → interval≥1 时不会每分钟重复发布（`scheduler.py` job_persona_sim，max_instances=1+coalesce） | — |
| 迁移幂等 | 通过 | persona_schedules/persona_posts 位于 ALL_DDL（`models/__init__.py:242-265`）`CREATE TABLE IF NOT EXISTS`；ensure_schema 可重入（`app/db.py`） | — |
| API 入参校验 | **缺失** | `api/persona_sim.py` PersonaScheduleRequest（account/type/interval_minutes/total）无任何约束：`interval_minutes=0/负数` → `next_run_at` 恒到期 → **每分钟重复发布直至 total**；`total=0/负数` → 永不发布；`total` 无上限。WebUI 端 el-input-number 有 min=1 约束（BoardView.vue:558-565），但 curl/MCP 可绕过。建议 `Field(ge=1)` / 上限 | P2 |
| publish_now 越界 | 存在 | `persona_sim.py publish_now()` 不检查 `published<total`，手动立即发布可超过 total 上限（与计划语义不一致；若为设计取舍建议文档化） | P2 |
| 并发双发 | 存在 | 无 SELECT 锁/占用标记；单进程单调度器（max_instances=1）下安全，多实例/多人并发 publish_now 可重复发布（persona_posts 无幂等约束） | P3 |

### 3. app/services/chat_import.py —— **不存在**

- 远程全工程 grep `chat_import|chat-import|chatImport|导入聊天|聊天记录`：**0 命中**（app/webui/scripts/tests/mcp 均无）；`app/services/` 与 `app/api/` 文件清单中无任何聊天导入实现。
- 本地工作区存在 `PLAN-聊天记录一键导入.md`（计划文档），但**远程未落地**。若该功能属本期验收范围 → 属缺项（建议与验收基线核对，定 P1 缺项或 N/A）；无脱敏/加密库降级/防御代码可审。

### 4. app/services/se_analysis.py（9123B）+ detections se_factors

| 检查点 | 结论 | 问题（文件:行） | 严重级 |
|---|---|---|---|
| 坏 JSON 容错 | 通过 | `scan.py:73-78`（json.loads try/except → {}）、`evidence.py:72-77`、`detections.py:169` `_json_or(...,{})`；列 `NOT NULL DEFAULT '{}'`（`models/__init__.py:82`）；测试覆盖空串/坏 JSON（test_p6_evidence.py:318-325） | — |
| 结构不破坏 | 通过 | se_factors 六键稳定（attack_vector/psych_techniques/seadm_stage/scam_lifecycle/evidence/confidence）；空文本返回 `_EMPTY_RESULT`（se_analysis.py 顶部常量 + analyze 入口 `text or ""`），不抛异常 | — |
| divergence.playbook | **无生产者** | `BoardView.vue:156` 读 `row?.se_factors?.divergence?.playbook`，但写入方 `speech_engine.py:221/309` 的 se_factors 来自 `SEAnalyzer.analyze()`（**无 divergence 键**）→ 前端「话术套路」列恒空（可选链安全不崩，属死代码/功能缺口） | P2 |
| se_factors.soc_analysis | **无生产者** | `DetailDrawer.vue:87-89` 注释称「/full 已携带 se_factors.soc_analysis」并做被动展示，但后端从未写入该键（soc_lib 命中只写 events）→ 被动展示恒空，注释不真实（主动触发路径正常） | P3 |

### 5. webui BoardView.vue（601 行，非任务所述 407 行）/ DetailDrawer.vue / client.js

| 检查点 | 结论 | 问题（文件:行） | 严重级 |
|---|---|---|---|
| XSS（v-html） | 通过 | 全工程 grep `v-html` **0 命中**；全部 `{{ }}` 插值；JSON 展示走 `<pre>{{ JSON.stringify(...) }}</pre>`；无 innerHTML | — |
| iframe 平台跳转 | 通过（注意） | `DetailDrawer.vue:224` iframe 用 `sandbox="allow-same-origin allow-scripts allow-forms allow-popups"`；CSP frame-src 白名单（main.py `_CSP_FRAME_SRC_ALLOW`：self/知乎/微博/抖音/127.0.0.1:3092,3080）；`openInNewTab` 用 `noopener`（DetailDrawer.vue:146）。注意：allow-same-origin+allow-scripts 组合仅对外部受信平台 URL 可接受；target_url 源自检测行（可被攻击者影响），若未来同源部署则组合风险上升 | P3 |
| 打开平台首页无 noopener | 存在 | `DetailDrawer.vue:264` `window.open(platform.home, '_blank')` 未带 noopener（platform.home 为静态常量，风险低） | P3 |
| 防御式 try/catch | 通过 | `BoardView.vue` loadAll 用 `Promise.allSettled` 逐源降级并汇总失败原因（:256-277）；enrichGrades 分块 `Promise.allSettled`+try/catch（:201-225）；loadSocLib 双源兜底（:128-140）；persona 独立防御式加载（:44-57）；`DetailDrawer.vue` load/triggerSocDefense 均 try/catch 置 error 态（:44-55/:104-116） | — |
| 来源标注真实 | 通过 | 统计卡/图表「来源 xxx」标注与 `client.js` 实际路径一一对应：/cases、/cases/graph、/scan/hits、/alerts、/system/stats、/soc-lib/status、/persona/schedules 均真实 | — |
| 模板强假设 | 存在 | `DetailDrawer.vue` `state.detail.detection.grade`（:139 等）、`e.kind.includes('alert')`（:279）假设后端字段恒存在/恒为字符串（后端保证，风险低） | P3 |
| client.js | 通过 | sessionStorage 会话级存 key（P2-8）、写入前 trim、401 统一清凭据跳登录、validation_error 友好文案（不渲染 pydantic 堆栈） | — |

### 6. dsh/ 干净性

| 检查点 | 结论 | 问题（文件:行） | 严重级 |
|---|---|---|---|
| cordis.patch.yml = 3092 | 通过 | `dsh/cordis.patch.yml`（181B）= webserver patch：`host: 127.0.0.1 / port: 3092`（注释注明与全局 3080 隔离）；start.mjs:25 显式加载；与验收口径一致 | — |
| start-embedded.ps1（CANARY_DSH_PORT/WMI/UTF-8） | 通过 | `start-embedded.ps1:8` 端口优先 `$env:CANARY_DSH_PORT` 否则 3092；WMI `Win32_Process.Create` 分离常驻（:66-69）；日志 UTF-8 落盘（`$PSDefaultParameterValues['Out-File:Encoding']='utf8'`，:61，P1-2 修复）；junction 自愈（:38-55，P1-3）；node 定位三级回退（:57-64） | — |
| **dsh-home 空白** | **违反** | `dsh\dsh-home\` 含运行留痕：`settings.yaml`（`permission.defaultPreset: danger-full-access` + ui-onboarding）、**`.credentials.yaml`（残留 grant secret：`FizXRK1-Y4kiye0yycoFidh8hSVuUPX0aUfrRS-eAJA`，client-connection/browser-session 授权）**、`.anonymous-user-id`、`storages/workspace.json`（initialized:true）、`profiles/web/*`。与 start.mjs:14「空白 home」设计意图相悖；凭据为本地回环绑定（127.0.0.1）风险受限，但「空白」验收项不满足，且残留授权物建议清理 | P2 |
| 无全局 junction | 通过 | 项目内 junction 全部位于 `dsh\dsh-home\profiles\node_modules\** → dsh\node_modules\**`（约 260 个，P1-3 换环境复制防护的自然结果）；全局探测（USERPROFILE\node_modules、USERPROFILE\dsh、AppData\Local\dsh、D:\dsh_3080、D:\nodejs）均无 canary 相关 reparse point | — |
| system.py embedded-dsh 读 CANARY_DSH_PORT | 通过 | `system.py:113` `os.environ.get("CANARY_DSH_PORT")` → 否则正则解析 `dsh/cordis.patch.yml` 的 port（:117-126）→ 默认 3092；running 按端口 TCP 探测；token_url 解析 `dsh/dsh-web.out.log` 的 `?token=` URL（:134-146）；与 client.js `embeddedDsh()` 路径一致 | — |
| 根目录整洁度（附注） | 存在 | 项目根残留调试/验证产物：`_a_openapi_dump.json`（69 paths，与官方 59 不符的过期产物）、`.tmp_teamc/`、`var/qa_round1..3/`、`var/qa_roundM/`、`_a_curl_test/`、`data/backup_teamC_baseline/` 等大量 QA 脚本与备份——不影响运行，属整洁度问题 | P3 |

### 7. 回归审计

| 检查点 | 结论 | 问题（文件:行） | 严重级 |
|---|---|---|---|
| grading finalize 事务 | 通过 | `grading.py finalize()`：UPDATE detections + INSERT events + `AlertService().evaluate(commit=False)` 三写同连接**单事务一次 commit**，异常 rollback 重抛（P1-C）；`alerting.py` evaluate 尊重 commit 开关、仅 L3+ 生成告警、`json_extract` 幂等去重 | — |
| evidence freeze confirm | 通过 | service 层冻结幂等（已冻结 409）；API 层 `confirm=true` + `reason.strip()≥20 字`（`_require_confirm` 403/422）+ `write_gate_log(action='evidence.freeze', before/after/payload_hash)` + `evidence_frozen` 事件，同连接提交 | — |
| cases 脱敏 | 部分通过 | `assert_clean` 代码层强制（含敏感 422 拒绝入库，cases.py `_publish_tx`）；publish 双确认+GateLog；公开查询只查 cases 表（不含 detections.content）。**但见 P1：desensitize_log 公开回显原文** | P1（下） |
| **cases desensitize_log 公开回显** | **P1** | `cases.py:96-101` 原样入库客户端提供的 `desensitize_log`；`cases.py:117-120` list_cases 返回全部列；**`cases/api:95-101` GET /cases 无 require_admin（公开）**；`desensitize.py` regex_redact/llm_redact 的 replaced 格式为 `{from: 原文, to: 掩码}`（脱敏审计可追溯设计）；MCP `af_publish_case` 显式透传该参数（mcp/server.py:346-365）。当前 WebUI 发布不传（存 `[]`），但任意调用方按服务层约定传日志 → 公开端点泄露手机/身份证等原文，直接违反 D7「公开查询只暴露脱敏载荷」。修复：公开响应剥离 desensitize_log，或入库前仅存 to/类型不存 from 明文 | **P1** |
| /scan/hits 原文片段泄露 | 存在 | `scan.py:56` GET /scan/hits **无鉴权**，返回解析后的 `se_factors`（scan.py:73-78），其中 `evidence[].text` 为原文句段（≤200 字符，se_analysis.py `_snippet`），`matched` 为命中原文片段——与 P2-7「inbox 结果含原文需 admin」的最小化策略相悖；有 IP 限流（60/min）缓解。建议：公开响应剥离 evidence[].text 或整字段仅 admin 可见 | P2 |
| supply_chain 超长域名 | 通过 | `supply_chain.py`：seed≤512（:76）、`_validate_domain` 总长≤253/单标签≤63 → 422 seed_invalid（:60-70）、`_resolve` 捕获 `(OSError, UnicodeError)`（:33-41，P1-B/C 修复）；`results/{name}` 用 `Path(name).name` 防路径穿越（:171）；results 列表坏 JSON 跳过（:155-158） | — |
| MCP 55 工具 | 通过 | `mcp/server.py` 统计 `def af_*` = **55**，与验收口径一致 | — |
| openapi 59 paths | 通过 | `docs/openapi.json` = **59** paths、`var/openapi_dump.json` = 59（一致）；根目录 `_a_openapi_dump.json` = 69（过期产物，见上 P3） | — |
| 附注 | 存在 | `cases` 表无 `UNIQUE(det_id)`（models/__init__.py:100-109）→ 同一检测可重复发布多条案例；公开 `GET /cases?tag=` 的 `LIKE '%"tag"%'` 未转义 `%`/`_` 通配符（cases.py:132，低风险）；`alerting.mark_read` 先 commit 再插事件（两段提交，alerting.py:97-108），事件插入失败时已读状态与事件不一致 | P3 |

---

## 三、问题清单（P0 / P1 / P2）

### P0（阻断级）
- 无。

### P1（高）
1. **cases desensitize_log 公开回显原文**（`app/api/cases.py` GET /cases 无鉴权 + `app/services/cases.py` 原样存储 + `app/services/desensitize.py` replaced 含 from 原文 + `mcp/server.py af_publish_case` 透传）——D7 违例，可致公开端点泄露手机/身份证等明文。修复方向：公开响应剥离该字段 / 入库前移除 from 明文。

### P2（中）
1. **soc_lib IOC 正则误报**：`soc_lib.py:12` QQ 正则与 `:10` 手机正则重叠（手机号被双提为 phone+qq，analyze 重复查询、本地命中重复写 `soc_lib_hit` 事件）；手机正则无数字边界可命中长串内部子串。
2. **persona schedule API 入参无校验**（`api/persona_sim.py` PersonaScheduleRequest）：`interval_minutes<=0` 时 `next_run_at` 恒到期 → job 每分钟重复发布直至 total；`total<=0/无上限` 不受约束（WebUI 有 min 约束，API 可绕过）。
3. **publish_now 可超 total 上限**（`persona_sim.py publish_now()` 无 `published<total` 检查）。
4. **divergence.playbook 无后端生产者**：`BoardView.vue:156` 话术套路列恒空（se_factors 六键中无 divergence；speech_engine.py:221/309 写入源确认无此键）。
5. **dsh-home 非空白 + 残留授权凭据**：`.credentials.yaml` 含 browser-session grant secret、settings.yaml 默认 `danger-full-access` 预置、.anonymous-user-id/storages 留痕——「dsh-home 空白」验收项不满足。
6. **公开 `GET /scan/hits` 返回原文片段**（se_factors.evidence[].text，与原文最小化策略相悖）。

### P3（低，摘录）
- soc_lib wechat 正则过宽；`/soc-lib/query` 无入参长度上限、ioc_type 未枚举。
- DetailDrawer iframe sandbox allow-same-origin+allow-scripts 组合（外部平台 URL 场景可接受）；`window.open(platform.home)` 缺 noopener；模板对后端字段强假设（detection/kind 恒在）。
- DetailDrawer `se_factors.soc_analysis` 被动展示为死代码且注释不真实。
- cases 表无 UNIQUE(det_id)；`/cases?tag=` LIKE 通配符未转义；`alerting.mark_read` 两段提交。
- 项目根/var 大量调试 QA 产物（`_a_openapi_dump.json`=69 paths、.tmp_teamc、var/qa_round*、backup_teamC_baseline 等）。
- `models/__init__.py` docstring「14 张表」与实际 19 张表不一致（文档陈旧）。

---

## 四、独立结论（与 A 队无涉，纯 B 队判断）

1. **验收结论**：代码层面**通过**（无 P0、无功能性崩溃点）。唯一 P1 为「cases desensitize_log 公开回显」的设计缺陷，修复成本极低（剥离字段/不存原文），建议修复后视为达标。
2. **合规红线**：D7「明文不落库/公开只暴露脱敏载荷」在 soc_lib、leak_records、evidence、api_keys、zhihu_sessions（Fernet 加密）等链路均落实；**唯一破口是 cases.desensitize_log 的公开回显（P1）与 /scan/hits 的 se_factors 原文片段（P2）**。
3. **拟真账号**：模板/调度/total/幂等实现正确；缺 API 校验与 publish_now 越界属一致性缺口。
4. **chat_import**：远程未实现（若在验收范围则为缺项，请与基线核对）。
5. **dsh 隔离**：端口隔离、CANARY_DSH_PORT 口径统一、无全局 junction、CSP frame-src 白名单均达标；「空白 home」未达（运行留痕 + 残留凭据），不影响功能但需清理。
6. 建议后续（不在本期验收阻断范围）：公开 /cases、/scan/hits 的字段最小化；soc_lib 正则加数字边界与类型互斥；persona API 入参 bounds；清理根目录 QA 产物与 .credentials.yaml。

（本报告由 B 队独立远程只读取证完成；未读取任何 A 队结论文件，未改动任何项目文件。）
