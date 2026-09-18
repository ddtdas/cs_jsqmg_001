# MCP透传与WebUI深度交互验证报告

- **执行人**：金丝雀蜜罐项目 · MCP与WebUI深度测试工程师（队D）
- **被测对象**：远程 Windows 192.168.10.110，主控 http://127.0.0.1:9200（FastAPI v1.0.0，health 200，pytest 收集 317 条与基线一致）；MCP 层 mcp/server.py（mcp SDK 2.2.0 / httpx 0.28.1，venv 运行）；WebUI webui/dist（生产构建，/ui/ 托管）
- **验证时间**：2026-09-16（探针运行于 11:03 / 复查 11:2x；浏览器 E2E 于 11:2x-11:35）
- **合规**：只验证/诊断，未改动任何产品代码；未重启主控；未触碰 dsh/ 内嵌实例与全局 3080；未提交 git。测试产生的数据副作用：1 条话术检测（det 3095/3107/3113 区段，均为常规 scan 操作）、1 个蜜饵草稿 #1378（已用 confirm+reason 正规退役并写入 GateLog 审计）、1 个证据包 #733（det_ids=[1]）、1 个账号建档 team_d_probe_user_01（acct 30）。

---

## 一、结论总览

- **P0 × 0**（无阻断性缺陷）
- **P1 × 1**
  - **P1-M1**：MCP 透传层 None 参数被 httpx 编码为空串 → `af_grade_explain`/`af_list_alerts` 两工具的**默认参数调用必 422**（REST 对照正常，缺陷定位在 mcp/server.py 透传层而非主控）
- **P2 × 8**
  - P2-M1：任务书点名的 `af_collector_matrix` 工具不存在于 MCP 工具清单（主控 /collector/matrix 端点已落地，MCP 未封装，工具与主控端点清单不对齐）
  - P2-M2：`af_config_update` "无 confirm → 403" 仅在 reason 非空时成立；缺省 reason 命中 pydantic min_length=1 先返 422 validation_error（语义可解释，需文档/调用方注意）
  - P2-W1：前端 client.js 对 429 无专门处理分支（无 Retry-After/冷却提示；限流中间件对有效 admin key 默认豁免，实际暴露面小）
  - P2-W2：App.vue 布局壳未按登录态条件渲染——未登录时侧边栏/顶栏（健康灯、退出按钮）仍可见
  - P2-W3：DetailDrawer `PLATFORM_MAP` 无 `home` 字段 → "打开平台首页"兜底按钮恒不显示（死代码）
  - P2-W4：DetailDrawer 社会工程学分析 v-if 缺括号（逻辑正确，可读性差）
  - P2-W5：ConsoleView `dshOnline`/`embedded` 状态已计算但模板未渲染（"离线提示"功能未实现）
  - P2-W6：CollectionView 的 scan/import 来源下拉缺 `import` 选项（ScanView 齐全 3 项，两页不一致）
- **通过项**：MCP 实工具 38 个全部列出并逐一验证；错误注入（422/403/404/主控不可达）全链路透传语义 100% 符合设计（R1-A8：isError=true + `[code] message` 原文）；WebUI 21 个视图 + 组件静态全查、SPA fallback 7 例、路由/chunk 3 项、浏览器 E2E 8 项全部通过。

> ⚠️ 测试环境注意：admin key 于 19:04 被重置、主控 19:12 重启（本次会话期间）。早前读取的 key（af_admin_5343f5…）随即失效——本报告所有结论以重置后的 key（af_admin_e1e5c4…）复核为准；请队长知会各队：**当前有效 key 为 data/bootstrap_admin_key.txt 现值，勿再用旧 key**。

---

## 二、逐项验证

### A. MCP 透传验证（mcp/server.py stdio 子进程 + mcp.client，真实主控 9200）

**方法**：`.venv\Scripts\python.exe` 启动 `mcp\server.py` 子进程（env：AF_MASTER_URL=http://127.0.0.1:9200、AF_API_KEY=文件 key），用 mcp SDK 2.2.0 `ClientSession.call_tool` 逐工具调用；结果 JSON 落盘远程 TEMP 后取回核对。**探针脚本自身缺陷澄清**：SDK 2.2.0 `CallToolResult` 的错误标志字段是 **`is_error`（snake_case）而非 `isError`**，初版探针用 `getattr(res,"isError")` 恒取 False，曾误报"错误未置 isError"；已用探针#4 以 `model_dump()` 内省纠正——**产品行为正常，非缺陷**（P2 级测试资产修正项，见缺陷清单）。

| # | 操作（工具/参数） | 预期 | 实测 | 结论 |
|---|---|---|---|---|
| A01 | list_tools | ≥38 工具、含任务点名工具 | 38 个工具列出；**af_collector_matrix 缺失**（主控有 /collector/matrix 端点）；含 af_register_agent（本机自描述） | 通过（附 P2-M1） |
| A02 | af_health | {status:ok,db,llm,zhihu} | `{"status":"ok","version":"1.0.0","db":true,"llm":"not_configured","zhihu":"idle"}` 11ms | 通过 |
| A03 | af_stats | 真实计数 | traps=1376→1380、detections=3092、alerts=1473、cases=1691、events=16042（全字段） | 通过 |
| A04 | af_scan_text(中文刷单话术, source=manual) | 规则判定+五级分级+SE 链 | detection_id=3095、verdict=suspicious、severity=medium、grade=L2、hits(pattern=刷单返利/category=job_scam)、se_analysis{attack_vector:baiting, psych:[reciprocity], lifecycle:groom, confidence:0.167}——**中文 UTF-8 请求/响应完整回显无损** | 通过 |
| A05 | af_scan_text(text="") | 422 封包透传 | is_error=True，文本 `Error executing tool af_scan_text: [validation_error] [{'loc':('body','text'),'msg':'String should have at least 1 character',...}]`——FastAPI 422 已由主控 register_error_handlers 统一封包、P0-E3 剥离 input/ctx | 通过 |
| A06 | af_scan_text(text="   ") | 422 empty_text | `[empty_text] 文本不能为空`（strip 后空走业务 422，与空串走 pydantic 422 两条路径均验证） | 通过 |
| A07 | af_scan_text(source="bogus") | 422 枚举校验 | `[validation_error] …'^(zhihu\|import\|manual)$' string_pattern_mismatch` | 通过 |
| A08 | af_list_traps(limit=5) | 列表+limit 生效 | items 前 5 条（含新建 #1378 → 退役 #1376 等真实状态机数据） | 通过 |
| A09 | af_get_trap(999999) | 404 透传 | `[trap_not_found] 蜜饵不存在: id=999999` | 通过 |
| A10 | af_list_events(page=1,page_size=5) | 分页 | total=16042、page/page_size 回显、items 倒序 | 通过 |
| A11 | af_list_events(kind="config_updated") | 参数透传（过滤） | total=56 且 items 全为 kind=config_updated——**证明 query 参数真实到达主控** | 通过 |
| A12 | af_get_event(999999) | 404 | `[event_not_found] 事件不存在: id=999999` | 通过 |
| A13 | af_grade_explain(3095, account_risk="red") | L3+证据链 | detection_id=3095、grade=L3、score=19、grade_reason 含 rule_hit('刷单返利'命中 job_scam)+account_red | 通过 |
| A14 | af_grade_explain(3095)（默认 account_risk） | docstring 称可选→应可用 | **失败**：`[validation_error] query account_risk pattern`——见 **P1-M1** | 失败→缺陷 |
| A15 | af_detection_full(1) | 四要素聚合 | detection+speech_hits+platform_link{platform:other,url:""}+（alerts/cases/traps/accounts/events 数组）2069ms | 通过 |
| A16 | af_detection_platform_link(1) | 跳转 URL | {url:"", home_fallback:true}（other 平台无问题点 URL，兜底语义正确） | 通过 |
| A17 | af_grading_levels | 五档定义 | L1-L5（min_score 0/0.01/10/null/null + desc） | 通过 |
| A18 | af_list_cases(page=1,page_size=5) | 脱敏载荷分页 | total=1691、items 含 redacted_payload/graph_tags（实证脱敏：138\*\*\*\*8000） | 通过 |
| A19 | af_list_cases(page=0) | 422 ge=1 | `[validation_error] …'greater_than_equal'` | 通过 |
| A20 | af_config | 合成配置不泄密钥 | llm{api_key_set:false,configured:false}、degrade{fence_open}、grading{l3_min:10,levels,alertable_grades}、overrides——无任何密钥明文 | 通过 |
| A21 | af_zhihu_channels | 四通道健康 | ch-a ok=false（Playwright 未装，降级）、ch-b/ch-c/ch-d ok=true 带说明 | 通过 |
| A22 | af_zhihu_status | 限速/退避/会话 | rate_limit{capacity:20,available:20}=AF_ZHIHU_RATE_LIMIT=20、backoff{}、sessions 4 通道（ch-d active，均无明文 cookie） | 通过 |
| A23 | af_list_alerts(level="L5") | 告警列表 | items=[]（显式 level 正常），**默认 level 调用 → [validation_error] level pattern（P1-M1 同源）** | 部分通过 |
| A24 | af_account_check("team_d_probe_user_01") | 绿黄红+证据链+persist | account_id=30、risk_level=green、score=0、no_signal 证据、persisted=true | 通过 |
| A25 | af_account_get("no_such_user_zzz") | 404 | `[account_not_found] 账号不存在: …` | 通过 |
| A26 | af_build_evidence(det_ids=[1]) | 哈希链证据包 | pkg_id=733、hash_chain[0]{content_sha256,hash,prev_hash:""}+frozen=0 | 通过 |
| A27 | af_export_evidence(pkg_id=1, fmt="yaml") | 422 枚举 | `[validation_error] …'^(json\|text)$'` | 通过 |
| A28 | af_register_agent | 本地自描述 JSON | mcpServers.af-honeypot{command=.venv python,args=[server.py],env 模板}，Windows 中文路径双反斜杠正确 | 通过 |
| A29 | af_scan_inbox | 消费 pending | processed=0、items=[]、zhihu_channels 快照 | 通过 |
| A30 | **af_retire_trap(999999, confirm=False)** | **403 敏感闸控** | `[confirm_required] 敏感操作必须 confirm=true（双确认）`——闸控先于资源存在性检查 | 通过 |
| A31 | af_retire_trap(1378, confirm=True, reason≥20) | 成功+GateLog | status=retired（draft→retired）+审计链路（主控 traps.py 写 gate_logs action=trap.retire） | 通过 |
| A32 | **af_publish_case(det_id=1, confirm=False)** | **403 敏感闸控** | `[confirm_required] 敏感操作必须 confirm=true（双确认）` | 通过 |
| A33 | af_publish_case(confirm=True, reason="短") | 422 理由长度 | `[reason_too_short] reason 至少 20 字，当前 1 字` | 通过 |
| A34 | **af_config_update(key=grading.l3_min, confirm=False, reason="x")** | **403 敏感闸控** | `[confirm_required] 敏感操作必须 confirm=true（双确认）`（reason 非空时才达 403 分支，见 P2-M2） | 通过 |
| A35 | af_config_update(key="totally.bogus.key", confirm=True, reason≥20) | 422 白名单拒绝（且不写入） | `[config_key_not_allowed] 配置键 'totally.bogus.key' 不在白名单…`——confirm+reason 通过后仍被白名单拦截，全程零写入 | 通过 |
| A36 | af_check_trap_hit(999999, text) | 404 | `[trap_not_found] 蜜饵不存在: id=999999` | 通过 |
| A37 | **主控不可达**：第二实例 AF_MASTER_URL=http://127.0.0.1:59999 调 af_health | ProxyError 干净报错 | is_error=True，`Error executing tool af_health: [master_unreachable] 主控不可达（http://127.0.0.1:59999）: All connection attempts failed`，2267ms，无堆栈炸屏 | 通过 |
| A38 | **is_error 语义精查**（探针#4） | 错误结果标志真实 | ok_health is_error=False；422/403/404/主控不可达 is_error 全部 True，result_type=complete，message 原文进入 content——R1-A8 落地 | 通过 |
| A39 | REST 对照：GET /grading/explain/3095（不带 account_risk） | 200 | `{"ok":true,"data":{...grade:L2…}}` ——证明 **422 系 MCP 透传层把 None 编为空串所致，主控本身无此问题**（P1-M1） | 对照通过 |

**MCP 小节**：参数透传（query/body、中文 UTF-8）、统一解包（{ok:true,data}→data；{ok:false,error}→ProxyError`[code] message`）、敏感操作 confirm+reason 闸控、错误传播（422/403/404/主控不可达）全部符合主方案 §7.1/§11 与 R1-A8 设计。

### B. 敏感工具 403 透传（专项，任务点名）

| 工具 | 无 confirm 调用 | 实测 | 结论 |
|---|---|---|---|
| af_retire_trap | confirm=False | `[confirm_required]` 403 | 通过 |
| af_publish_case | confirm=False | `[confirm_required]` 403 | 通过 |
| af_config_update | confirm=False + reason 非空 | `[confirm_required]` 403 | 通过 |
| af_config_update | confirm=False + reason 缺省 | `[validation_error]` 422（见 P2-M2） | 附注 |

### C. WebUI 源码静态审查（webui/src/views 全部 21 视图 + api/client.js + components/DetailDrawer.vue + router + stores + main）

**C1. 各视图主要交互点 / DetailDrawer 接入 / 空态错误态 / 校验一致性一览**

| 视图 | 主要交互点 | DetailDrawer 接入 | 空态/错误态 | 表单校验 vs 后端 schema |
|---|---|---|---|---|
| LoginView | 一键登录(local-login)/手动 key | — | loopback_only 403 降级提示 ✓ | — |
| DashboardView | 统计卡/趋势/最近事件(行点击→抽屉) | ✓（hit/alert 行 detId） | allSettled 逐项失败聚合+el-alert ✓ | — |
| AssetsView | 蜜饵资产/证据包资产(evidence_built 事件聚合) | —（证据包专属抽屉） | 失败聚合 ✓、自定义空态 ✓ | — |
| BoardView | 案例/图谱/趋势 + 明细行→抽屉 | ✓（case.det_id） | 失败聚合 ✓ | — |
| SessionsView | 4 tab 历史 + 分页 + 深链定位(?tab&id) | ✓（hit/alert/event；trap 回退 JSON 抽屉） | error 提示 + 空态 ✓ | events page_size≤100 已按 50 走服务端分页（A10 注释）✓ |
| RiskView | 风险资产/告警/分级 | ✓（资产+告警行） | 失败聚合 ✓、空态 ✓ | level L3/L4/L5 与后端 pattern 一致 ✓ |
| CollectionView | scan/import/inbox 消费 | ✓（hits/inbox 行） | 3 组独立错误态 ✓ | source 下拉**缺 import 选项**（P2-W6） |
| SupplyChainView | 账号关联链图/OSINT 反查图/报告加载 | — | timelineError/error/reconError + el-empty ✓ | — |
| TrapsView | 增删查部署监控退役 + check-hit | 专属蜜饵抽屉（合理） | pollError 展示 ✓、弹窗前置校验 ✓ | platform 下拉与后端 _ALLOWED_PLATFORMS 完全一致 ✓；bait≥4 字=min_length=4 ✓；退役 confirm+reason≥20 与后端双确认一致 ✓ |
| ScanView | 粘贴检测/命中列表/CH-D 导入 | ✓（hits 行 id） | scanError/hitsError + el-empty ✓ | source 单选 3 项=后端枚举 ✓ |
| AccountsView | 账号速查结果 | — | error ✓、空态 ✓ | url_name 非空 ✓ |
| EvidenceView | 构建/冻结/导出/举报模板/校验 | ✓（pkg det_ids 逐条） | error/detailError ✓ | fmt json/text 与后端 pattern 一致 ✓；kind 96110/platform/pibyao ✓ |
| CasesView | 分页查询/图谱/发布 | ✓（case.det_id） | error ✓、图谱失败聚合 ✓ | 发布 confirm+reason≥20 前置校验+后端错误码友好映射（confirm_required/reason_too_short/sensitive_data）✓ |
| EventsView | 事件流+kind 过滤+5s 轮询+降级链 | ✓（payload.detection_id，实测事件 payload 含该键） | error/空态 ✓ | KIND_CN 缺 detection_graded/detection_scanned（P2-W7，显示原始英文 kind） |
| AlertsView | 级别/未读过滤+标记已读 | ✓（payload.detection_id） | error ✓ | level L3/L4/L5 ✓ |
| ZhihuView | 四通道/状态/cookie 注入校验 | — | error/空态 ✓ | channel/scheme 与后端一致 ✓ |
| OpsCenterView | 健康四灯/降级链/分级档位 | — | 失败聚合 ✓ | — |
| SettingsView | key 查看(脱敏)/复制/重置/MCP 配置 | — | loopback_only 整页降级 ✓ | 重置 confirm+reason≥20 与后端一致 ✓ |
| AccountsConfigView | 平台矩阵/动态配置表单/测试/一键导入/私信 ingest | — | error/detailError/el-empty ✓ | 动态字段按 config_fields 渲染 ✓ |
| CollectorMatrixView | 矩阵 CRUD/启停/立即执行/技术栈 | — | error/stackError + empty-text 自定义 ✓ | 平台/策略/频率枚举与后端(未逐项比对 hardcode) |
| ConsoleView | 内嵌 DSH iframe + 地址切换 | — | loading 状态 ✓（离线提示未实现 P2-W5） | URL http(s) 前缀校验 ✓ |

**C2. SPA fallback 运行时验证（curl 实测 9200）**

| 路径 | 实测 | 预期 | 结论 |
|---|---|---|---|
| /ui/xxx | 200 text/html，含 `<div id="app">`（index.html） | SPA fallback | 通过 |
| /ui/foo/bar/baz | 200 index.html | 任意深路径回退 | 通过 |
| /ui/assets/not-exist-123.js | 404（{ok:false,error:{code:not_found}}） | 带点资源 404 | 通过 |
| /ui.json | 404 | 带点路径不吞进 SPA | 通过 |
| /ui/assets/x.css | 404 | 同上 | 通过 |
| /ui/console、/ui/collector-matrix、/ui/accounts-config | 200 index.html（前端路由接管） | 路由可达 | 通过 |
| /ui/、/ui | 200 index.html | 根与裸路径 | 通过 |

源码佐证（app/main.py ui_spa）：真实文件→FileResponse；含`.`→404；其余→index.html；目录穿越防护（resolve+parents 校验）✓。dist/assets 存在 ConsoleView-D1vYK94l.js / CollectorMatrixView--Io7TNx8.js / AccountsConfigView-CUL7Jsv7.js / index-BhDk3BDV.js（与 index.html 引用一致）✓。

**C3. /ui/console iframe 与路由（浏览器实测）**

- 导航 /ui/console：iframe `src="http://127.0.0.1:3092/?token=5XwYcOi37KMImyGZwVukxDt7RuMjc8dLQC-dQtcG-d0"`——**默认内嵌项目自包含 DSH（端口 3092，非 3080）**，token URL 来自 GET /system/embedded-dsh（读 dsh/dsh-web.out.log 正则提取）✓；main.py CSP frame-src 白名单含 `http://127.0.0.1:3092` ✓；手动切换地址存 localStorage(af_dsh_console_url) ✓
- /ui/collector-matrix 实测渲染 8 个矩阵单元格 + "立即执行全部"按钮，无错误 ✓
- /ui/accounts-config 实测渲染平台矩阵（微信/知乎等 42 单元格字段）+ 私信接入测试区 ✓

**C4. client.js 401/429/网络错误统一处理**

- 401：`res.status===401` → setApiKey('') + onUnauthorized()（main.js 挂 logout+跳 /login）→ **浏览器 E2E 实测**：注入假 key 后加载 /ui/dashboard → probeSession(stats 401) → 清 sessionStorage → 自动跳 `/ui/login?redirect=/`，登录页呈现 ✓
- 网络错误：fetch 异常 → ApiError('network_error', …, status=0) ✓
- 422：payload.ok===false 且 code==='validation_error' → 统一友好文案（不渲染 pydantic 原文）✓
- **429：无专门分支**（P2-W1）。429 封包（middleware/rate_limit.py）为 `{ok:false,error:{code:"rate_limited",message:"请求过于频繁，请稍后再试"}}`，会走 payload.ok===false 通用分支展示 message；但无 Retry-After/冷却倒计时差异化提示。缓解因素：限流仅作用于 /scan/text、/accounts/check、/traps/{id}/check-hit，且携带有效 admin key 的请求默认豁免（rate_limit_admin_bypass=true）——WebUI 登录态下几乎不触发
- 附加观察：client.js 的 query 序列化**丢弃 null/undefined/''**（`if (v!==undefined && v!==null && v!=='')`），与 MCP 层 None→空串（P1-M1）形成鲜明对照——修 MCP 时直接对齐此语义即可

**C5. 浏览器 E2E 深度交互（本机直连 http://192.168.10.110:9200/ui/）**

1. 未认证访问 /ui/ → 路由守卫 302 到 /ui/login?redirect=/dashboard ✓
2. 手动输入 admin key 登录 → 落 /ui/dashboard ✓（一键登录从非回环来源实测返回 403 loopback_only，符合设计）
3. 仪表盘：5 统计卡（蜜饵 200/检测记录 50/话术命中 44/告警 100/案例 1692 为即时真实值）+ 双图表 + 最近事件表 + 分级档位表 ✓
4. 顶栏：健康四灯（主控/DB 绿，LLM 红=未配置降级）+ 脱敏 key（af_admin****）✓
5. 点击最近事件行 → **DetailDrawer 五区块完整加载**（①告警原因 ②上下文 ③平台跳转 ④社会工程学分析 置信度0.208 ⑤详细分析），标题"事件详情 #3113" ✓（无 detId 的行如 trap 不加载，符合设计）
6. /ui/console iframe=3092 token URL ✓；/ui/collector-matrix、/ui/accounts-config 渲染 ✓
7. 假 key → 自动登出跳登录 ✓

---

## 三、缺陷清单

| # | 级别 | 位置 | 现象 | 复现 | 修复建议 |
|---|---|---|---|---|---|
| P1-M1 | P1 | mcp/server.py（call() / MasterProxy.request 透传层） | None 默认参数被 httpx 编码为空串：`af_grade_explain()` 与 `af_list_alerts()` **默认/省略可选参数调用必 422**（`query account_risk= & level=`）；docstring 宣称参数可选与实际行为矛盾 | 任意会话 `call_tool("af_list_alerts",{})` 或 `call_tool("af_grade_explain",{"detection_id":3095})`；REST 对照 GET /grading/explain/3095 不带参数返回 200，证明缺陷在主控之外 | 在 call()/request() 对 params 做 None 过滤（`{k:v for k,v in params.items() if v is not None}`），或对齐前端 client.js 丢 None 语义；为该两工具补默认参数透传回归测试 |
| P2-M1 | P2 | mcp/server.py 工具清单 vs 主控 app/api/collector.py | 任务书点名 `af_collector_matrix` 不存在（list_tools 38 个中无此工具），而主控 /collector/matrix（GET/POST）与 /{cell_id}（PUT/DELETE/run）已落地——能力未暴露给 agent | `call_tool("af_collector_matrix",{})` → 工具不存在错误 | 按主控端点补充 af_collector_matrix（GET）及可选 af_collector_add_cell/update/run 透传封装，保持同一解包协议 |
| P2-M2 | P2 | app/api/config.py ConfigUpdateRequest | `af_config_update(confirm=False)` 缺省 reason 时返回 422 validation_error（reason min_length=1）而非 403 confirm_required——"无 confirm→403"分支需 reason 非空才可达；三层（pydantic→confirm→reason→白名单）校验顺序造成语义前置 | `call_tool("af_config_update",{"key":"grading.l3_min","value":0.42,"confirm":False})` | 建议主控将 reason 默认 "" 并去掉 min_length（确认仍由 confirm 分支兜底），或在工具 docstring 明确"reason 必填"；前端 updateConfig 恒 confirm:true+reason 必填，不受影响 |
| P2-W1 | P2 | webui/src/api/client.js request() | 429 无专门分支：无 Retry-After/冷却提示，仅通用错误封包展示；参考 401/validation_error 均有专门处理 | 匿名高频调 /scan/text 触发 RateLimitMiddleware | 增加 `res.status===429` 分支：提示"请求过于频繁"+按 Retry-After（可选）倒计时；同时可对 rate_limited 错误码做 toast 特化 |
| P2-W2 | P2 | webui/src/App.vue 布局壳 | 未登录（/login）时侧边栏全量菜单、顶栏健康四灯与「退出」按钮仍渲染（含最近会话列表拉公开 /scan/hits 数据） | 退出登录后观察页面 | 布局容器按 `auth.isAuthed`（router 登录态）条件渲染；或至少隐藏顶栏操作区 |
| P2-W3 | P2 | webui/src/components/DetailDrawer.vue | PLATFORM_MAP 无 `home` 字段，模板 `platform.home` 恒 undefined → 「打开平台首页」兜底按钮永不显示（死代码） | 打开任意 other 平台检测详情、无平台 URL 时 | 为各平台补 home URL（知乎/微博/抖音首页），或删除该按钮与占位文案 |
| P2-W4 | P2 | DetailDrawer.vue L183 | 社会工程学分析 v-if 缺括号：`seAnalysis && seAnalysis.attack_vector !== 'unknown' || (…)`（逻辑结果正确，属可读性/健壮性风险） | 静态阅读 | 补括号：`(seAnalysis && …) || (…)`，并加 `seAnalysis &&` 前置短路即可 |
| P2-W5 | P2 | webui/src/views/ConsoleView.vue | `state.dshOnline`/`state.embedded` 已计算但模板未渲染——"若未启动将显示离线提示"文案未实现；321 行 probeDsh 的 401 分支被 `!!res` 短路（res 恒 truthy），dshOnline 判定退化为"能连上即在线" | 打开 /ui/console，停掉 dsh/ 实例观察（无任何离线提示） | 模板补 dshOnline/embedded 状态提示；修正 `!!res ||` 运算符优先级冗余 |
| P2-W6 | P2 | webui/src/views/CollectionView.vue | scan/import 来源下拉仅 manual/zhihu 两项，缺后端枚举 'import'（ScanView 三项齐全）——两页行为不一致 | 打开 /ui/collection 观察来源选择 | 两页统一为 manual/zhihu/import 三选项 |
| P2-W7 | P2 | webui/src/views/EventsView.vue | KIND_CN/evtToView 未收录 detection_graded/detection_scanned（事件库主力事件），列表显示原始英文 kind | 事件流前几条即 detection_graded/scanned | 补中文映射（检测分级/检测入库） |
| P2-T1 | P2 | 测试资产（非产品） | 探针初版用 `isError`(camelCase) 读取 SDK 2.2.0 的 `is_error`(snake_case) 字段 → 误报"错误结果标志恒 False"；已用 model_dump 内省纠正 | — | 测试脚本固定用 `res.model_dump()['is_error']` 读取；此类 SDK 字段命名差异行为已记入小组资产 |

**通过项汇总**：MCP 38 工具 + 38 用例（其中 A23 部分通过、A14 失败=缺陷 P1-M1）；敏感工具 403（B 表 3/3 + 1 附注）；WebUI 静态 21 视图 + SPA fallback 7 例 + 浏览器 E2E 8 项全部通过。**全部验证用例无 P0**。

---

## 四、对标差异（与 ScamIntelli 系 11 层引擎对比，如有适用）

本项目 SE 能力（队2 社会工程学分析模块，探针 A04 实测）为**确定性规则层推理链**：输出 `attack_vector`（baiting/phishing/…）+ `psych_techniques[]`（reciprocity/urgency/…）+ `seadm_stage` + `scam_lifecycle`（groom/…）+ `evidence[]`（factor/matched/text 三元组）+ `confidence`（0.167/0.208 量级）五要素，明确标注"确定性规则层推理链，不依赖 LLM"（DetailDrawer 卡片副标题）。

对比 11 层全引擎（若指攻击向量/心理技巧/SEADM/生命周期等分级多层的完整引擎）：**本项目为上述要素的规则判定简化版**，不做逐层概率推断/多引擎投票/置信度归一化校准，层级内 dsh 或 agent 未接 LLM 复核（llm=not_configured）。差异点如实记录：① 置信度偏保守（<0.21）；② SEADM 常为 unknown（样本证据不足时缺省）；③ 无独立 11 层结构 schema，字段为扁平 JSON，MCP/WebUI 共用同一结构。若后续对标 ScamIntelli 11 层，建议在 se_analysis 层做字段对齐/映射层（本报告不展开，按任务要求仅记录差异）。

---

## 五、更新 / 修复方案建议

1. **【P1-M1 · 建议本轮修复】** mcp/server.py 在 `call()` 或 `MasterProxy.request()` 中过滤 None 参数；回归用例：`af_list_alerts()`、`af_grade_explain(detection_id)` 默认调用返回 200 语义。
2. **【P2-M1 · 顺手补齐】** 按主控 /collector/matrix 补充 MCP 工具封装（含 run-all/单格执行可选），保持 unwrap 协议不变；同步更新 docs/ 工具清单与预习笔记 P8_MCP_预习笔记.md。
3. **【P2-M2 · 文档级】** config_update 工具 docstring 注明"reason 必填且 ≥20 字，confirm 分支在 reason 通过 pydantic 后触发"；若允许改主控，去掉 ConfigUpdateRequest.reason 的 min_length（默认 ""，由 confirm 分支统一 403/422）。
4. **【WebUI 小组件批修】** P2-W3/W4/W5/W6/W7 均为 5-20 行级小改（平台 home、运算符括号、console 提示、source 选项、kind 中文映射），可随下次 webui 构建一并合入（vite build 后 dist 替换，注意 start.bat 的 /ui/ 托管不重启主控）。
5. **【P2-W1/W2 · 交互体验】** 429 分支 + App 布局登录态条件渲染，建议与前端 401 处理同批改动（client.js 已有 onUnauthorized 通道可复用）。
6. **【环境·队长协调】** 本次会话期间 admin key 被重置（文件 mtime 19:04）且主控 19:12 重启——请队长确认是否有成员在跑 key 重置/环境重装流程，避免各队凭据不一致；后续接入统一以 `data/bootstrap_admin_key.txt` 实时读取为准。

---

*验证产物（远程）：docs/MCP与WebUI深度验证报告_队D.md；探针脚本与结果位于远端 %TEMP%（mcp_probe{1..4}.py / *_result.json）与本地工作区镜像 _mirror/，可复跑复核。*