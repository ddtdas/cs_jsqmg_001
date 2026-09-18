# 审计报告（队 B · 只读）

> 验收队 B 独立审计 · 结论双盲 · 只读（本地镜像与远程均未修改任何文件）
> 审计对象：金丝雀蜜罐 CanaryGuard（远程 192.168.10.110，本地镜像 `canary-mirror/`）
> 审计日期：2026-09-15
> 说明：本地镜像将远程 `mcp\server.py` 平铺为根级 `server.py`（经远程核实两者均为 22137 字节，内容一致），
> 审计结论对远程 `mcp\server.py` 同样适用；镜像中未含 `manual_acceptance.py` 之外的脚本与测试目录，其余文件与远程一致。

---

## 结论总览

**P0 × 1 / P1 × 7（含 1 项存疑）/ P2 × 19**

| 级别 | 编号 | 摘要 |
|---|---|---|
| P0 | P0-1 | `GET /api/v1/evidence/{pkg_id}` 匿名公开返回检测**原文**（未脱敏，含对话原文/PII） |
| P1 | P1-1 | `/alerts`、`/events` 无认证公开读，泄露内部事件/告警流（seed、key_sha256 等） |
| P1 | P1-2 | `payload LIKE '%"detection_id": {id}%'` 数字前缀误匹配 → 告警去重漏报 / 关联错乱 |
| P1 | P1-3 | WebUI 蜜饵「退役」按钮必然失败（client.js 不传 confirm+reason，后端 403） |
| P1 | P1-4 | AssetsView 证据包列表/详情按钮数据错配（列全空、详情 422） |
| P1 | P1-5 | CollectionView 导入来源含后端不接受的 `wechat` 选项（必 422） |
| P1 | P1-6 | ConsoleView 内嵌 DSH iframe 被主控 CSP `frame-src` 拦截（面板不可用） |
| P1⚠存疑 | P1-7 | `/scan/text`、`/accounts/check` 匿名写库 + 消耗 LLM 预算 / 污染画像（限流缓解） |
| P2 | P2-1 ~ P2-19 | 见下文明细 |

---

## P0 严重

### P0-1 `GET /api/v1/evidence/{pkg_id}` 匿名公开返回检测原文（未脱敏）—— 敏感数据泄露

- **位置**：`app/api/evidence.py:35-38`；`app/services/evidence.py:36-62, 157-168`；事故根源 `app/services/speech_engine.py:298`
- **证据链**：
  1. `app/api/evidence.py:35-38`：
     ```python
     @router.get("/{pkg_id}")
     def get_evidence(pkg_id: int, db=Depends(get_db)) -> dict:
         """证据包详情 + 完整性校验（公开只读）。"""
         return ok({"package": _service.export(db, pkg_id, "json")["content"], "verified": _service.verify(db, pkg_id)})
     ```
     该端点**未挂 `require_admin`**（对比同文件 `/export`、`/freeze`、`/build` 均挂 `dependencies=[Depends(require_admin)]`）。
  2. `app/services/evidence.py:167`：`export()` 的 `payload["items"]` 直接取 `self._detection_items(conn, ...)`；
  3. `app/services/evidence.py:54`：`_detection_items` 中 `"content": det["content"]` —— **对话原文**；
  4. `app/services/speech_engine.py:298`：`_persist` 以 `text[:4000]` **明文原文**入库（`detections.content` 未脱敏，"存储策略按配置，可脱敏"仅为注释）；
  5. 同文件头注释自称"单查与举报模板保留公开（只读，**脱敏后输出**）"（`api/evidence.py:5-6`）—— 实现未脱敏，与声明不符。
- **影响**：任何匿名访问者可 `GET /api/v1/evidence/<id>` 读取证据包内全部关联检测的**原始诈骗对话全文**（可能含受害者手机号/银行卡/微信号等 PII），无需任何凭据。违反复审要求的"敏感数据泄露 / 原文未脱敏"红线与数据最小化原则。
- **修复建议**（二选一）：
  1. 给 `GET /evidence/{pkg_id}` 增加 `require_admin`（与 `/export` 同级保护，推荐）；
  2. 或 `export()` 输出前对 `items[].content` 执行 `DesensitizeService().regex_redact()` 脱敏（与 `report_template` 的 `services/evidence.py:206` 一致）。

---

## P1 重要

### P1-1 `/api/v1/alerts`、`/api/v1/events` 无认证公开读（内部活动流泄露）

- **位置**：`app/api/alerts.py:18-26, 29-32`；`app/api/events.py:28-52, 55-60`
- **证据**：`list_alerts` 与 `list_events`/`get_event` 均无 `require_admin`；`app/api/system.py:48` 的 `/system/stats` 反而是受保护的 —— 口径不一致。
- **泄露面**：
  - incidents 事件 payload 含供应链反查输入 `seed`（`app/api/supply_chain.py:131-134`）、admin key 重置的 `key_sha256`（`app/api/auth.py:210-213`）、`detection_scanned` 的判定明细（`app/services/speech_engine.py:335-345`）；
  - alerts payload 含 `detection_id` + 分级证据 `reason[:6]`（`app/services/alerting.py:32-36`）—— 内部研判信息对匿名可读。
- **修复建议**：`/alerts`、`/events` 读取端点统一 `require_admin`；若确需公开，至少从 payload 剥离 `seed`/`key_sha256` 等敏感字段。

### P1-2 `payload LIKE '%"detection_id": {id}%'` 数字前缀误匹配（告警去重漏报 / 关联错乱）

- **位置**：`app/services/alerting.py:26-28`；`app/api/detections.py:82-84, 130-131`；`app/api/events.py` 无此问题（events 用 kind 过滤）
- **证据**：
  - `app/services/alerting.py:26-28`：
    ```python
    dup = conn.execute(
        "SELECT COUNT(*) AS n FROM alerts WHERE payload LIKE ?",
        (f'%"detection_id": {detection_id}%',),
    ).fetchone()["n"]
    ```
    模式 `"detection_id": 5%` 会命中 `"detection_id": 50`、`500` 等**任何以 5 开头**的更大编号（子串前缀匹配）。
  - **后果**：若先有 `detection_id=5` 的告警，后续 `detection_id=50` 的 L3+ 检测会被误判为 duplicate 而不生成正式告警（**告警漏报**）；反过来 `detection_id=50` 存在时 `500` 也被吞。
  - 同类问题：
    - `app/api/detections.py:82-84`（`full` 端点关联告警）；
    - `app/api/detections.py:130-131`（关联事件，且同时用 `%"detection_id": {id}%` 与 `"detection_id": {id}%` 两个模式，仍有前缀误配）。
  - 另：payload 由 `json.dumps` 生成（默认 `": "` 分隔、`", "` 连接，`app/services/alerting.py:32-36`），若将来序列化格式变化（无空格 / 键序变化），LOOSE LIKE 也会漏匹配。
- **修复建议**：改用 SQLite JSON 精确提取：`json_extract(payload, '$.detection_id') = ?`（或 `json_each`），消除前缀误配与格式耦合。

### P1-3 WebUI 蜜饵「退役」操作必然失败（前端未实现 confirm+reason 双确认）

- **位置**：`src/api/client.js:153`；`src/views/TrapsView.vue:89-90`；后端 `app/api/traps.py:118-127`
- **证据**：
  - `src/api/client.js:153`：`retireTrap: (id) => request(`/traps/${id}/retire`, { method: 'POST' })` —— **不携带 body**；
  - `src/views/TrapsView.vue:89-90`：`ElMessageBox.confirm('退役该蜜饵？…').then(() => api.retireTrap(trap.id))` —— 仅普通确认框，无 confirm 勾选、无 reason 输入；
  - 后端 `app/api/traps.py:125-127`：`if payload is None: raise ApiError("confirm_required", …, 403)`，且 `_require_confirm`（traps.py:57-65）要求 `confirm=true` 且 `reason≥20`。
- **后果**：点击「退役」必然收到 403 `confirm_required`，功能**完全不可用**；退役是蜜饵"命中即退役防反查"的关键人工操作，也是核心敏感操作。
- **修复建议**：`client.js` 的 `retireTrap(id, reason)` 带 `body: { confirm: true, reason }`；`TrapsView` 退役改为弹窗（勾选确认 + reason≥20 输入，参照 `SettingsView.vue:259-295` 重置 key 交互与 `CasesView.vue` 发布交互）。

### P1-4 AssetsView 证据包资产列表 / 详情按钮数据错配

- **位置**：`src/api/client.js:172`；`src/views/AssetsView.vue:52-56, 66-77, 126-138`
- **证据**：
  - 后端 `GET /evidence/{pkg_id}` 返回 `{ package: <包体>, verified: <校验> }`（`app/api/evidence.py:38`）；
  - 但 `AssetsView.vue:127-133` 表格列直接用 `row.pkg_id` / `row.det_ids` / `row.frozen` —— 实际在 `row.package.*` 下，**三列恒为空**；
  - `AssetsView.vue:136` `openPkg(row.pkg_id)` 传 `undefined` → 请求 `GET /api/v1/evidence/undefined` → 后端 `pkg_id: int` 422 validation_error，**详情按钮不可用**；
  - 行点击目标 `state.evPkgs = pkgs`（`AssetsView.vue:57`）存的是 `{package, verified}` 整体，与列渲染对象不一致。
- **修复建议**：`AssetsView` 改用 `row.package.pkg_id` / `row.package.det_ids` / `row.package.frozen` 或在上游 `pkgs` 时解包；或后端 `get_evidence` 直接返回包体（不嵌套）。

### P1-5 CollectionView 导入来源含后端不接受的 `wechat` 选项（必 422）

- **位置**：`src/views/CollectionView.vue:137, 170`；后端 `app/api/zhihu.py:44-48`
- **证据**：
  - `CollectionView.vue:137`（话术检测）与 `:170`（导入）：`<el-option label="微信" value="wechat" />`；
  - 后端 `ImportRequest.source`：`pattern="^(zhihu|import|manual)$"`（`app/api/zhihu.py:44-48`）—— `wechat` 不在枚举内；
  - 对比 `ScanView.vue:88-92` 的 radio 只有 manual/zhihu/import（正确），两页不一致。
- **后果**：用户选「微信」提交 → 422 `validation_error`（client.js:105-108 统一映射为笼统提示，用户难定位）。
- **修复建议**：删除 wechat 选项，或后端放开 `wechat`（并同步 `speech_engine._platform_of` 平台映射 `app/services/speech_engine.py:91-93`）。

### P1-6 ConsoleView 内嵌 DSH iframe 被主控 CSP `frame-src` 拦截（命令输入面板不可用）

- **位置**：`src/views/ConsoleView.vue:8, 77-82`；`app/main.py:96-97`
- **证据**：
  - `ConsoleView.vue:8` 默认 `DEFAULT_DSH = 'http://127.0.0.1:3080/#deepseek'`，`:78` `<iframe :src="state.iframeUrl" ...>`；
  - `app/main.py:96-97` CSP：`frame-src 'self' https://www.zhihu.com https://zhuanlan.zhihu.com https://weibo.com https://m.weibo.cn https://www.douyin.com https://*.douyin.com` —— **不含 `http://127.0.0.1:3080`**。
- **后果**：浏览器按 CSP 拒绝加载 `127.0.0.1:3080` 的 iframe（Chrome 报 refused to connect），命令输入面板黑屏，功能不可用。
- **修复建议**：CSP `frame-src` 增加 `http://127.0.0.1:3080`（做成配置项更稳）；并注意 `ConsoleView.vue:80` sandbox `allow-same-origin allow-scripts` 组合等于无沙箱（对外嵌不受信内容时的已知警告点，此处内嵌自有 DSH 可接受，但建议同时去掉 `allow-same-origin` 若 DSH 登录态不需要 iframe 内持久化）。

### P1-7（存疑）公开热路径写库端点无认证：`/scan/text`、`/accounts/check`

- **位置**：`app/api/scan.py:32-40`；`app/api/accounts.py:27-30`；限流白名单 `app/middleware/rate_limit.py:24-28`
- **证据**：
  - `POST /scan/text` 无 `require_admin`，落库 `detections` + `speech_hits` + `events` + `grading.finalize`（可能触发 `AlertService.evaluate` 写告警），且规则命中时调用 LLM 复核（`app/services/speech_engine.py:126-149`）——**匿名可消耗 LLM 预算**（`llm_daily_budget` 守卫在 provider 内，`app/config.py:38`）；
  - `POST /accounts/check` 无 `require_admin`，`AccountIntelService.check` 直接 upsert `accounts` 表（`app/services/account_intel.py:66-81`），`url_name` 任意 —— **匿名可污染画像数据**（影响共现团伙分析、detections 关联兜底）；
  - 缓解：两者均在限流白名单（`rate_limit.py:25-26`，默认 60 req/min/IP，admin key 豁免）。
- **存疑说明**：若"公开 API 供外部/agent 调用"是产品设计（限流白名单与文档 D1 均暗示如此），则此条为接受的设计权衡；但审计仍建议：
  - `/accounts/check` 的**写库（upsert）与读评估分离**：无凭据请求只读评估，不落库；
  - `/scan/text` 对 LLM 触发做按 IP 预算配额（复用 `llm.daily_budget` 之外的短窗口配额）。

---

## P2 建议

| 编号 | 位置 | 问题 | 建议 |
|---|---|---|---|
| P2-1 | `app/api/detections.py:124-125` | 无关联账号时兜底 `related_accounts = accounts[:3]`，把与检测**无关**的最近账号展示成"关联账号"（DetailDrawer ②上下文），误导研判 | 无关联时返回空列表并显示"暂无关联账号" |
| P2-2 | `app/api/detections.py:119` | `det_id in a["trap_hit_ids"]` 类型不匹配：`det_id` 为 int，而 `trap_hit_ids` 若存字符串（匿名经 /accounts/check 注入 `["123"]`）则恒 False，漏关联 | 统一 `int(x)` 归一后比较 |
| P2-3 | `src/components/DetailDrawer.vue:132-134` | `platform.home` 在 `PLATFORM_MAP`（:16-22）无此字段 → `v-if="iframeUrl === '' && platform.home"` 恒 false，「打开平台首页」按钮永不显示 | PLATFORM_MAP 增加 `home` 或改用 `platform_link` 兜底 |
| P2-4 | `src/views/SessionsView.vue:35-36, 142-144, 360-364` | 分级解释（`/grading/explain`）：`detail.grading` 恒 null、`gradingLoading` 恒 false，代码从未调用 `api.gradingExplain` —— 死代码 | openDetail('hit') 时真调 `gradingExplain(row.id)` 填充 |
| P2-5 | `ScanView.vue`、`CollectionView.vue`、`RiskView.vue`、`AssetsView.vue` | 有检测/告警列表却未接入 `DetailDrawer`（AlertsView/Dashboard/Board/Sessions 已接入），DetailDrawer「四要素」在这些视图缺失 | 检测记录/告警行点击打开 DetailDrawer（detection_id=row.id 或 payload.detection_id） |
| P2-6 | `src/views/CollectionView.vue:63` | `r?.consumed ?? 0` —— 后端 `POST /scan/inbox` 返回 `{processed, items, zhihu_channels}`（`app/api/scan.py:83-87`），无 `consumed` → 恒显示"已消费 0 条" | 改用 `r.processed` |
| P2-7 | `app/api/zhihu.py:61-65` | `channels()` 同步端点内 `asyncio.run(bridge.health())`：每次请求新建事件循环；若被 async 上下文（测试客户端/调用方）调用会 RuntimeError（存疑） | 端点改 `async def` 直接 `await bridge.health()` |
| P2-8 | `app/api/zhihu.py:98-100` | `ok({**result, "ok": False})` 在 data 内嵌顶层同名词 `ok`，与统一封包 `{ok,data}` 语义混杂（ZhihuView.vue:59 依赖 r.ok） | data 内改用 `valid` 布尔 |
| P2-9 | `app/api/accounts.py:33-36` | `GET /accounts/{url_name}` 会捕获 `GET /accounts/check`（把 "check" 当 url_name → 404 account_not_found），本应 405 | 显式注册 `GET /accounts/check` → 405，或给 `{url_name}` 加 pattern |
| P2-10 | `app/services/grading.py:139-159` | `finalize` 分 3 次 commit（UPDATE grade → INSERT event → AlertService.evaluate 内再 commit），非事务原子：中途异常则 grade 已落库但告警/事件缺失 | 单事务包裹 |
| P2-11 | `app/services/cases.py:120-121` | `graph_tags LIKE '%"{tag}"%'` 参数化无注入，但 `%`/`_` 通配符未转义，tag 传 `%` 可匹配全部 | 转义 `%`/`_` 或用 JSON 包含查询 |
| P2-12 | `src/composables/usePolling.js:12-28` | 轮询无 in-flight 并发 guard：慢请求时 setInterval 会堆叠重叠请求 | refresh 内加进行中标记（skip/排队） |
| P2-13 | `src/views/OpsCenterView.vue:67` | `s.boot?.key_file` —— 后端 `bootstrap-info` 只返回 initialized/port/hint（`app/api/auth.py:88-112`，P0-D1 特意**不**返回路径），恒显示 '—'（字段不存在） | 删除该展示或改为"见 data 目录"提示 |
| P2-14 | `app/api/auth.py:144-148` | `current-key` 返回 `key_file` 完整绝对路径（虽仅回环可读，仍泄露盘符/文件名；bootstrap-info 已修过同类问题） | 改为相对提示（如"data 目录下"） |
| P2-15 | `mcp/server.py:404-409` | `af_zhihu_login(channel, cookie, scheme)` 把**明文 cookie** 作为 MCP 工具参数：stdio/streamable-http（默认无 TLS）传输链路与客户端日志可能记录；主控侧 Fernet 加密落库不受影响 | MCP 服务器说明中提示勿在共享/多租户环境注入 cookie；streamable-http 建议走 TLS |
| P2-16 | `mcp/server.py:152` 注释"36 个" | 实际注册 38 个工具（traps 7 + scan/detections/accounts/grading 9 + evidence/cases 8 + events/alerts 4 + system/zhihu/config/register 10）；另后端 `GET /system/audit-verify`（`app/api/system.py:75-83`）在 client.js 与 MCP 均未暴露 | 更新注释；如需可在 MCP 补 `af_audit_verify` |
| P2-17 | `app/main.py:131-166` | `/ui` 静态 FileResponse 未设 `Cache-Control`（SPA 资源每次回源；存疑） | 对 assets/ 设 immutable 长缓存，index.html no-cache |
| P2-18 | `app/api/alerts.py:29-32` | `POST /alerts/{id}/read` 无认证：匿名可改已读状态（次要写面） | 与 P1-1 一并加 `require_admin` |
| P2-19 | `app/api/detections.py:119-121` | `any(str(det_id) in json.dumps(...) for _ in [0])` 用单元素迭代套 any，写法怪异（功能等价单次判断），可读性差 | 改为直接 if 判断 |

---

## 一致性核对表（前端 client.js ↔ openapi ↔ MCP）

> 静态核对（以源码路由为准，未运行 /openapi.json）。

### A. client.js 方法 ↔ 后端端点（共 45 个方法，43 个一一对应，2 个异常）

| client.js | 后端端点 | 一致 |
|---|---|---|
| health/stats/bootstrapInfo | GET /system/health、GET /system/stats、POST /auth/bootstrap-info | ✅ |
| localLogin / currentKey / resetKey | POST /auth/local-login、GET /auth/current-key、POST /auth/reset-key | ✅ |
| scanText / scanHits | POST /scan/text、GET /scan/hits | ✅ |
| detectionFull / detectionPlatformLink | GET /detections/{id}/full、/platform-link | ✅ |
| zhihuImport | POST /zhihu/import | ✅ |
| listTraps / getTrap / createTrap / generateTrapDraft / deployTrap / monitorTrap / disableTrap | /traps 系列 | ✅ |
| **retireTrap** | POST /traps/{id}/retire | ⚠️ **缺 body{confirm,reason} → 403（P1-3）** |
| checkTrapHit | POST /traps/{id}/check-hit | ✅ |
| accountCheck / getAccount / accountTimeline | POST /accounts/check、GET /accounts/{url_name}、/{id}/timeline | ✅ |
| gradingLevels / gradingExplain | GET /grading/levels、/explain/{id} | ✅ |
| buildEvidence / getEvidence / freezeEvidence / exportEvidence / reportTemplate | /evidence 系列 | ✅（getEvidence 返回结构被 AssetsView 误用 → P1-4，client 本身正确） |
| listCases / publishCase / caseGraph | GET /cases、POST /cases/publish、GET /cases/graph | ✅ |
| listEvents / getEvent | GET /events、GET /events/{id} | ✅ |
| getConfig / updateConfig | GET /config、PUT /config | ✅ |
| scanInbox / scanInboxResult | POST /scan/inbox、GET /scan/inbox | ✅ |
| zhihuChannels / zhihuStatus / zhihuLogin / zhihuVerify | /zhihu/channels、/zhihu/status、channels/{id}/login、channels/{id}/verify | ✅ |
| supplyChainQuery / supplyChainResults / supplyChainResult | POST /supply-chain/query、GET /results、GET /results/{name} | ✅ |
| listAlerts / markAlertRead | GET /alerts、POST /alerts/{id}/read | ✅ |

**异常/缺口**：① `retireTrap` 缺敏感操作 body（P1-3）；② client.js 未暴露 `GET /system/audit-verify`（P2-16）；③ `OpsCenterView` 引用 `bootstrapInfo().key_file` 不存在（P2-13）；④ `CollectionView` 传入后端不接受的 `wechat` source（P1-5）。

### B. MCP 工具 ↔ 后端端点（38 个工具）

全部 38 个 `af_*` 工具与后端 `/api/v1` 端点一一对应（`mcp/server.py:188-456`），方法/路径/参数对齐：
- 蜜饵 7 个 → `/traps`：list/get/generate-draft/deploy/disable/retire/check-hit（`server.py:189-232`）✅；
- 检测/账号/分级 9 个 → `/scan`、`/detections`、`/accounts`、`/grading`（`server.py:235-287`）✅；
- 证据/案例 8 个 → `/evidence`、`/cases`（`server.py:290-354`）✅；
- 事件/告警 4 个 → `/events`、`/alerts`（`server.py:357-380`）✅；
- 运维/知乎/配置 10 个，含 `af_register_agent` 本地自描述不发请求（`server.py:383-456`）✅；
- 敏感操作透传：`af_retire_trap`/`af_publish_case`/`af_config_update` 均透传 `confirm`+`reason`（`server.py:222-227,330-349,430-436`）✅；
- 差异备注：`af_zhihu_login` 明文 cookie 参数（P2-15）；注释"36 个工具"与实际 38 不符（P2-16）；未暴露 `af_audit_verify`（P2-16）。

### C. MCP 透传纯度（D1）

- `mcp/server.py:70-85` `unwrap()` 只解封包：`{ok:true,data}→data`，`{ok:false,error}→抛 ProxyError`；✅ 无业务逻辑。
- `mcp/server.py:123-143` 透传 `params→query`、`body→json`，非 JSON 响应/网络错误映射为 ProxyError（code/message 保留）。✅
- `mcp/server.py:53-67` `ProxyError` 继承 SDK `ToolError`（R1-A8：错误原文进入 isError content，不被吞）。✅
- `mcp/server.py:168-176` 仅对非 dict 返回值做 `{"items":...}/{“value”:...}` 展示归一化，不加工业务数据。✅ — **结论：D1 纯透传成立**。

---

## 合规红线检查（HITL / 不冒充 / 脱敏 / confirm+reason / cookie 加密 / fence）

| 红线 | 结论 | 证据 |
|---|---|---|
| **HITL（只产草稿不自动发布）** | ✅ | `app/services/trap_engine.py:356-374` generate_draft 状态恒 `draft`；发布需用户手动后显式 `POST /traps/{id}/deploy`（`app/api/traps.py:100-103`）；调度器 `job_trap_recheck` 只复查不发布（`app/services/scheduler.py:112-139`） |
| **不冒充官方/不代替处置** | ✅ | 举报/辟谣模板仅个人署名"普通用户（个人）"+ 处置引导（`app/services/evidence.py:184-245`）；`REVIEW_SYSTEM`/`BAIT_SYSTEM` 为内部引擎提示，无对外冒充文本 |
| **脱敏强制（cases 入库前置）** | ✅ | `app/services/cases.py:85` `DesensitizeService.assert_clean(redacted_payload)` 代码层拒绝（422）；正则层+变体层+LLM 复核层（`app/services/desensitize.py:24-41, 192-199, 287-362`） |
| **脱敏（公开只读面）** | ❌ | `GET /evidence/{id}` 公开返回原文未脱敏（**P0-1**）；`/events`、`/alerts` 公开泄露内部信息（**P1-1**） |
| **confirm + reason 双确认** | ✅（后端） | config PUT（`app/api/config.py:110-127`）、case publish（`app/api/cases.py:57`）、trap retire（`app/api/traps.py:125-127`）、reset-key（`app/api/auth.py:167-175`）全部强制 confirm=true + reason≥20，并对接 GateLog 链（`app/services/audit_log.py:33-53`） |
| **confirm + reason（前端/MCP）** | ⚠️ | MCP 侧 3 个敏感工具均透传 confirm+reason ✅（`mcp/server.py:222-227,330-349,430-436`）；WebUI「蜜饵退役」未实现双确认 → 功能不可用（**P1-3**） |
| **cookie 加密存储** | ✅ | Fernet 加密落库 `zhihu_sessions.cookie_enc`，明文不落库、不回显（`app/services/zhihu_bridge.py:276-329, 523-540`；`app/api/zhihu.py:6-8`）；`/zhihu/status` 仅返回 `has_cookie` 布尔（`zhihu_bridge.py:1266-1269`） |
| **LLM 输入 fence（D4）** | ✅ | `FENCE_OPEN/FENCE_CLOSE`（`app/services/llm_provider.py:30-31`），一切外部文本经 `fence()` 包裹（`llm_provider.py:137-147, 298`） |
| **X-API-Key 处理** | ✅ | Header `X-API-Key` + Bearer 双通道；`secrets.compare_digest` 常量时间比较（`app/deps.py:64-84`）；key 明文不落库（api_keys 存 sha256，`app/models/__init__.py:142-147, 201-219`） |
| **CSP / 安全头** | ⚠️ | 全套安全头 ✅（`app/main.py:88-112`：CSP/X-Frame-Options DENY/nosniff/no-referrer）；但 `frame-src` 与 ConsoleView iframe 冲突（**P1-6**） |
| **目录穿越防护** | ✅ | `/ui` SPA 用 `resolve()+parents` 校验（`app/main.py:159-161`）；`supply-chain/results/{name}` 用 `Path(name).name`（`app/api/supply_chain.py:173`） |
| **敏感操作审计链（GateLog）** | ✅ | `gate_logs` 哈希链：payload_hash=sha256(prev+action+before+after+reason+ts+payload_json)（`app/services/audit_log.py:28-53`）；幂等迁移/一次性断链回填 `_repair_gate_chain_once`（`app/models/__init__.py:272-343`）；校验端点 `/system/audit-verify`（`app/api/system.py:75-83`）；局限已注明"不防持库者整体重算"（`audit_log.py:9-10`，可接受） |
| **限流防护** | ✅ | 公开热路径 IP 令牌桶 + admin 豁免（`app/middleware/rate_limit.py:24-28, 50-95`）；429 统一封包 `{ok:false,error:{code:'rate_limited'}}` |
| **数据最小化/原文读取需 admin** | ⚠️ | `/detections/{id}/full`、`GET /scan/inbox` 原文读取有 admin ✅（`app/api/detections.py:58`、`app/api/scan.py:90`）；但 `/evidence/{id}` 原文公开 ❌（P0-1）、`/events`/`/alerts` 公开 ❌（P1-1） |

---

## 附：审计方法与范围说明

- 审计方式：只读静态审计（本地镜像 + 远程核实目录/文件大小与 mcp 平铺关系）；**未运行测试、未修改任何代码、未重启任何服务**。
- 远程核实：`mcp\server.py` 22137 字节与镜像根级 `server.py` 一致；`docs\` 目录已存在。
- 存疑标注：P1-7（公开写路径是否属设计权衡）、P2-7（asyncio.run 部署形态）、P2-14（路径泄露影响面）、P2-17（缓存头）等条目已分别注明。