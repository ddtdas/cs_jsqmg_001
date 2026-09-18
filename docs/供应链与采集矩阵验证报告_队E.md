# 供应链反查与采集矩阵深度验证报告

- **测试队**：队E（供应链与采集矩阵深度测试工程师）
- **被测对象**：金丝雀蜜罐 CanaryGuard AntiFraud（192.168.10.110，9200 主控）
- **基线**：`GET /api/v1/system/health` → 200 `{ok:true,data:{status:"ok",db:true,llm:"not_configured",zhihu:"idle"}}`；pytest 317 passed（沿用队长基线，未重跑）
- **测试时间**：2026-09-16 19:00–19:10（主控本地时间）
- **方式**：SSH + PowerShell EncodedCommand，X-API-Key 鉴权，UTF-8 字节 POST；**未改任何产品代码、未重启主控、未触碰 dsh/ 与全局 3080、未提交 git**；测试单元格已全部清理，系统恢复初始状态
- **证据归档**：远程 `data/_teamE/*.json`（35 个用例响应快照）+ 本地 `_teamE_evidence/`（scp 副本）

## 结论总览

**P0 × 0 ／ P1 × 1 ／ P2 × 7 ／ 通过 21 项 ／ 部分通过 2 项 ／ 失败 1 项（即 P1）**

| 模块 | 结论 |
|---|---|
| 供应链反查 query（合法/多级/未知域名/422 封包/循环防护/鉴权） | ✅ 主体通过；超长 host 触发 **P1 500 空体**；IP 节点续查受限（P2） |
| 供应链 results 列表/详情/404/路径穿越 | ✅ 通过；报告**无独立 timeline 字段**（P2） |
| 前端 /ui/supply-chain（账号关联链 + OSINT 反查两 tab） | ✅ 通过（SPA 可达 + 双模式交互点齐全） |
| 采集矩阵 CRUD / 校验 / run-cell 降级 / run-all 节流 / 调度触发器 / tech-stack | ✅ 主体通过；zhihu 降级无 note（P2）、PUT 响应 strategy 未解析（P2）、job_snapshot 未暴露到 API（P2） |

**附注（重要）**：测试期间 `data/bootstrap_admin_key.txt` 的 admin key 被轮换（`af_admin_5343…` → `af_admin_e1e5…`，中间出现过一批 401）。疑似运维/其他队伍操作或密钥轮换机制，非本测试造成；请队长确认是否有意为之（观察项 OBS-1）。

---

## 逐项验证

### 一、供应链 supply-chain

#### 1. POST /supply-chain/query：种子查询 + 多级续查

**T1 合法 URL 种子**
- 操作：`POST /api/v1/supply-chain/query {"seed":"https://www.example.com"}`
- 预期：200，图节点/证据/信号齐全
- 实测：**200（583ms）**，`mode=local_recon`，`domain=www.example.com`
  - graph：**15 节点 / 14 边**（1×L1 域名 + 2×L2 IP `104.20.23.154/172.66.147.243` + 12×L6 相似域名；2 条 `resolve` + 12 条 `similar` 边）
  - evidence：`L1/L2_DNS-IP(a_record)` + `L6_相似域名(similar_family)`；signals 数组；`hint=agent_workflow`
  - 事件落库：events 表新增 `supply_chain_query`（`{"seed":"…","domain":"www.example.com","resolved":true}`）
- 结果：✅ **通过**（证据：`SC_T1_valid_url.json`）

**T2 裸域名续查（无协议）**
- 操作：`{"seed":"example.com"}` → **200**，`local_recon`，正常解析（无协议裸域名被 `_DOMAIN_RE` 接受）✅ 通过

**T4 图内子节点（相似域名）续查（多级）**
- 操作：`{"seed":"https://example.net"}`（T1 图谱中的 L6 子节点）→ **200**，`local_recon`，15 节点/14 边，且其相似族包含 `example.com`（**跨级回链**，多级续查成立）✅ 通过（证据：`SC_T4_child_domain.json`）

**T3 图内 IP 节点续查**
- 操作：`{"seed":"93.184.216.34"}`（T1 解析出的 IP）→ **200**，但 `mode=concept_only`：仅 1 个 `concept` 根节点，无 IP/L2 情报层，evidence 为 L0 概念说明，提示走 agent workflow
- 判定：**部分通过** — IP 节点无法本地续查（设计如此：`_extract_domain` 不支持 IPv4），降级行为安全不崩；但"IP 节点续查"能力缺失 → **P2-2**

#### 2. 输错格式 → 422/400 封包；未知域名 → 200 小图 + 提示

| 用例 | 操作 | 实测 | 判定 |
|---|---|---|---|
| T5 空字符串 | `{"seed":""}` | **422** `{ok:false,error:{code:"seed_required",message:"请输入要反查的网址/域名/概念"}}` | ✅ 通过 |
| T6 缺字段 | `{}` | **422** seed_required | ✅ 通过 |
| T7 纯空格 | `{"seed":"   "}` | **422** seed_required | ✅ 通过 |
| T11 无协议带路径 | `{"seed":"www.example.com/path"}` | **200** `concept_only`（1 概念节点 + agent_workflow 提示） | ⚠️ 与"422/400 封包"预期不符，但行为合理（URL 缺协议但仍是可反查概念词）→ **P2-7** |
| T9 超长 host | `{"seed":"http://" + "a"*300 + ".com"}` | **500，响应体为空**（无 JSON 错误封包） | ❌ **失败 → P1-1** |
| T10 乱码非网址 | `{"seed":"not a url at all 中文测试"}` | **200** concept_only + 中文提示 | ✅ 通过（合理降级） |
| T8 未知域名 | `{"seed":"https://nonexistent-abc123xyz999.invalid"}` | **200**，`local_recon`，图小（13 节点：1×L1 risk=high + 12×L6），evidence 含 `L1_DNS(nxdomain)`，signals 含"查无此站/打一枪换域名"风险信号 | ✅ 通过（证据：`SC_T8_unknown_domain.json`） |

**P1-1 根因（已本地复现）**：`socket.getaddrinfo("a"*300+".com")` 在 IDNA 编码阶段抛 `UnicodeError("label empty or too long")`；`app/api/supply_chain.py` 的 `_resolve()` 仅 `except OSError`，未捕获 `UnicodeError`（ValueError 分支）→ 未处理异常逃逸为 500 空体。见 `data/_tmp_repro.py` 复现输出。

#### 3. GET /supply-chain/results 列表 + /results/{name} 详情 + 循环防护

- **B1 列表**：`GET /api/v1/supply-chain/results` → **200**，1 条（`hgrz.pro`）：`seed="https://hgrz.pro/invest (示例诈骗投资网址…)"`、`nodes=17`、`edges=18`、`evidence_total=51`、`updated_at=2026-09-14T00:00:00+08:00` ✅ 通过（证据：列表字段齐全）
- **B2 详情**：`GET /api/v1/supply-chain/results/hgrz.pro` → **200**，完整报告：
  - `graph.nodes=17`（**≥15 ✓**）、`graph.edges=18`
  - `evidence_counts.total=51`、`layer_results` 5 层（L2/L3/L4/L5/L7）、`summary` 中文结论、`suspicion_signals`/`next_steps`/`compliance_note` 齐全
  - **缺 `timeline` 字段**（顶层仅 `seed/run_at/method/summary/evidence_counts/graph/suspicion_signals/next_steps/compliance_note/layer_results`）→ **P2-3**
- **B3 404**：`/results/no_such_report_xyz` → **404** `{ok:false,error:{code:"report_not_found",message:"反查报告不存在: …"}}` ✅ 通过
- **B4 路径穿越**：`/results/..%2F..%2F..%2FWindows%2Fwin.ini`、`/results/..%2Fbootstrap_admin_key` → 均 **404**（`Path(name).name` 消毒生效，日志可见 FastAPI 规范化后 404）✅ 通过
- **T12 循环防护（自引用域名）**：`{"seed":"hgrz.pro"}` → 200，相似族为 `hgrz.com/.net/.org/.top/.cc/.xyz/.vip/.site/.cn/.info/hgrzpro.com/hgrz-pro`，**不含 `hgrz.pro` 自身**（`_similar_domains` 换 TLD + 变体生成，天然无自环）✅ 通过（证据：`SC_T12_selfref.json`）
- **T13 鉴权**：无 X-API-Key → **401** `{ok:false,error:{code:"invalid_api_key"}}` ✅ 通过

#### 4. 前端 /ui/supply-chain 交互点

- `GET /ui/supply-chain` → **200**（SPA index.html，602B）；`GET /ui/collector-matrix` → **200** ✅ 路由可达
- `webui/src/views/SupplyChainView.vue`（417 行）确认**双模式**（`el-radio-button`）：
  - ① **账号关联链**（`mode=account`）：/accounts/check 团伙共现 + /accounts/{id}/timeline 踩饵链
  - ② **OSINT 反查（顺藤摸瓜）**（`mode=recon`）：输入网址/域名/概念 → `/supply-chain/query` 轻量起链；`agent_workflow` 命中时弹 `el-alert` 提示"本地起链完成/概念词已建根节点，完整多维 OSINT 请在命令输入中用 supply-chain skill + workflow 深挖"；报告下拉加载 `/supply-chain/results`，证据链按层/类型/值/说明表格展示
- ✅ **通过**（证据：`SupplyChainView.vue` L3-6、L260-261、L278-289、L313-358）

### 二、采集矩阵 collector

#### 5. POST /collector/matrix 建单元格 + 列表含策略 JSON

- 建 4 单元格（zhihu dm / rsshub posts（keyword=诈骗）/ telegram dm / wechat 登记）→ 全部 **200**，返回 `{id, platform, account, strategy{...}, status:"pending"}`（id 26/27/28/29）✅
- `GET /collector/matrix` → **200**，列表含**解析后的 strategy 对象** + `platform_name`（知乎/rsshub/…）✅ 通过
- `GET /collector/platforms` → 6 平台：`zhihu, weibo, telegram, wechat, qq, rsshub` ✅ 通过
- 校验负例全部 **422**（`{ok:false,error:{code,message}}` 规范封包）：
  - 非法平台 `qqqq` → `invalid_platform`（中文提示列出可选平台）
  - `collect:"bogus"` / `depth:0` / `near_dup:2.0` / 账号 200 字符 / 空 platform / 40 字符 platform → 全部 422（pydantic pattern/ge/le/max_length）✅ 通过

#### 6. run-cell：未配置平台降级 / rsshub 不可达 → error

| 单元格 | 操作 | 实测 | 判定 |
|---|---|---|---|
| wechat(id29) 未配置 | `POST /matrix/29/run` | 200 `{collected:0,error:null}`，状态 `ok`，**note="由本地桥(WeChatFerry/NapCat)推送私信，矩阵只登记状态"**（降级 note 落库） | ✅ 通过 |
| zhihu(id26) 未配 cookie | `POST /matrix/26/run` | 200 `{collected:0,error:null}`，状态 `ok`（`ChannelUnavailableError` 静默吞掉） | ⚠️ 部分通过 — 无降级 note，与 wechat/qq 行为不一致 → **P2-5** |
| telegram(id28) 未配 token | `POST /matrix/28/run` | 200 `{collected:0,error:"Telegram 未配置 bot_token"}`，状态 `error` | ✅ 通过 |
| rsshub(id27) 不可达 | `POST /matrix/27/run` | 200 `{collected:0,error:"RSSHub 采集失败: timed out"}`（**8391ms = 8s 超时**），状态 `error`，`last_error` 落库 | ✅ 通过（rsshub.app 从该网络不可达，错误状态符合预期） |
| 不存在单元格 | `POST /matrix/99999/run` | **404** `cell_not_found` | ✅ 通过 |

#### 7. run-all 节流 + job_collector_matrix 触发器

- **run-all（4 个启用单元格）**：`POST /collector/matrix/run-all` → 200 `{ran:4, collected:0}`，**总耗时 10716ms**（rsshub 8s 超时 + 3×0.5s 节流 sleep 可观测，代码 `time.sleep(0.5)`）✅ 节流存在
- **job_collector_matrix 触发器**：
  - 代码：`app/services/scheduler.py` — `@_safe("job_collector_matrix")`，`IntervalTrigger(minutes=1)`、`max_instances=1`、`coalesce=True`、`replace_existing=True`，仅查 `enabled=1` 单元格 ✅
  - **存活证据**：`var/logs/app.log` 每分钟 `canary.scheduler job_collector_matrix 轮询 N 个启用单元格`（19:00:46→19:07:46 连续）；19:04:55 自动轮询到**本测试创建的 4 个单元格**并执行（cells 26/27 的 `last_run_at=19:04:46/19:04:55` 早于本队手动 run）；19:06:46 自动轮询 **2 个**（禁用/删除后计数同步下降）✅ **调度器活体 + enabled 过滤双重验证通过**
  - `job_snapshot()` 函数存在（scheduler.py L310，docstring"供运维面板与测试断言"），`tests/test_p2_engineering.py` L199 断言注册 **7 类 job**（含 `job_collector_matrix`）✅
  - **缺口**：`GET /api/v1/system/stats` 返回 `{traps,active_traps,detections,grades,alerts,unread_alerts,cases,accounts,events,generated_at}`，**未暴露 job_snapshot** → **P2-4**

#### 8. 编辑 / 禁用 / 删除

- **PUT 编辑**：`PUT /matrix/29 {"enabled":false,"strategy":{"collect":"comments","frequency":"daily","depth":10,"keyword":"代购","near_dup":0.75}}` → 200 生效（列表确认 `enabled=0`、strategy 更新）✅
  - ⚠️ 注意：**PUT 响应中 `strategy` 为 JSON 字符串**（`"strategy":"{\"collect\": \"comments\",…}"`），而 `GET /matrix` 返回解析对象 —— 不一致 → **P2-6**（前端 `CollectorMatrixView.vue` 的 `parseStrategy()` 已兼容两种情况）
- **enabled=false 不参与 run-all**：禁用 cell29 后再 `run-all` → `{ran:3}`（26/27/28 参与，29 排除）✅ 通过；调度器侧同样排除（见上）
- **DELETE**：`DELETE /matrix/28` → 200 `{deleted:true,cell_id:28}`，列表即时更新 ✅ 通过
- 清理：测试单元格 26/27/29 已删除，最终 `GET /collector/matrix` → `{"ok":true,"data":{"cells":[]}}`（**系统恢复初始空态**）✅

#### 9. tech-stack 8 项完整

`GET /collector/tech-stack` → **200，8 项**，每项含 `name/repo/stars/use/integrate` 全字段：Crawlee、Scrapy、DecryptLogin、Wechaty、WeChatFerry、zhihu_spider、NapCatQQ、python-telegram-bot ✅ **通过**

---

## 缺陷清单

| 级别 | 编号 | 位置 | 现象 | 复现 | 修复建议 |
|---|---|---|---|---|---|
| **P1** | P1-1 | `app/api/supply_chain.py` `_resolve()` L42-51 | 超长 host（label>63 或超长总长）输入返回 **500 空体**（无 JSON 封包），主控稳定性受影响 | `POST /supply-chain/query {"seed":"http://"+ "a"*300 +".com"}` | ① `except OSError` 扩为 `except (OSError, UnicodeError)`（getaddrinfo 的 IDNA 编码会抛 UnicodeError）；② 增加 host/label 长度校验（label≤63、总长≤253）提前返回 422；③ 兜底全局异常捕获 |
| P2 | P2-1 | `app/api/supply_chain.py` query L75 | seed 无长度上限（无 max_length），超长输入触发 P1-1 | 同上 | `payload` schema 增加 `seed` 长度约束（≤512）并返回 422 |
| P2 | P2-2 | `app/api/supply_chain.py` `_extract_domain` L31-39 | IP 节点无法本地续查（`93.184.216.34` → concept_only），任务要求"IP 节点可续查" | `{"seed":"93.184.216.34"}` | 增加 IPv4 正则分支 → 走 L2 IP 情报（归属/反查同 IP 域名占位） |
| P2 | P2-3 | `var/supply-chain/*.json`（agent workflow 产物） | 报告无独立 `timeline`（时间线）字段，仅 `run_at`；前端无时间线视图 | `GET /supply-chain/results/hgrz.pro` | workflow 产物增加 `timeline:[{ts,event,evidence_id}]` 数组 |
| P2 | P2-4 | `app/services/scheduler.py` job_snapshot L310 / `app/api/system.py` stats | `job_snapshot()` 存在但**未暴露到任何 REST 端点**（/system/stats 无此字段），运维面板看不到调度状态 | `GET /api/v1/system/stats` | stats 增加 `scheduler_jobs`（id/trigger/next_run）或独立 `/system/scheduler` 端点 |
| P2 | P2-5 | `app/services/collector.py` `_execute_cell` zhihu 分支 L277-300 | zhihu 未配置 cookie 时静默降级（status=ok、无 note），与 wechat/qq 的登记 note 行为不一致，run-all 后无法区分"正常空采"与"未配置" | `POST /matrix/{zhihu_cell}/run`（无 cookie） | ChannelUnavailableError 分支写入降级 note（如"未配置 cookie，已降级跳过"） |
| P2 | P2-6 | `app/api/collector.py` update_cell L62-64 / `app/services/collector.py` update_cell L177-187 | `PUT /matrix/{id}` 响应中 strategy 为 JSON 字符串，与 GET 列表的解析对象不一致（API 自身不一致） | `PUT /matrix/29 {...strategy...}` 后对比响应与列表 | update_cell 复用 list_matrix 的行处理（json.loads 后返回）；前端已兼容，低危 |
| P2 | P2-7 | `app/api/supply_chain.py` query L84-97 | 无协议带路径输入（`www.example.com/path`）返回 200 concept_only 而非 422/400（任务预期偏差）；行为安全但提示语未区分"URL 格式错误"与"概念词" | `{"seed":"www.example.com/path"}` | 检测到疑似 URL（含 . 与 / 但无 scheme）时返回 422 `invalid_url`，或补充"疑似 URL 缺协议"提示 |
| 观察 | OBS-1 | `data/bootstrap_admin_key.txt` | 测试期间 admin key 被轮换（`5343…`→`e1e5…`），期间旧 key 全部 401 | 对比两次读取文件 | 确认是否有意轮换机制；测试脚本建议每次动态读取 key 文件 |

## 对标差异（与 ScamIntelli 11 层引擎对比）

- **层级数**：本项目 `skills/supply-chain/SKILL.md` 定义 **L1-L9 九层**链路（L1 DNS → L2 IP → L3 WHOIS → L4 CT → L5 子域名 → L6 关联域名 → L7 账号社交 → L8 资金 → L9 团伙聚合）；ScamIntelli 为 **11 层**引擎，差 2 层（推测为注册局/资产指纹细分、链上/资金渠道细分等）。
- **执行方式**：本项目 query 端点**本地即时**只落 L1/L2/L6（DNS 解析 + IP + 相似域名），其余层由 `hint=agent_workflow` 转交 DSH 内嵌 agent 按 skill + workflow 执行（最大 3 轮深度、每层要求 `next_queries` 输出），产物写入 `var/supply-chain/*.json` 供前端加载；ScamIntelli 11 层全自动串联。
- **能力差异**：本地即时反查是"轻量起链"（秒级、无外部 API 依赖、可离线），完整多维反查依赖 agent workflow（需命令输入触发）；ScamIntelli 假设全自动化外部数据源。
- **循环/深度控制**：本项目同源去重 + 最多 3 层深度的队列机制（SKILL.md），自引用域名天然无环（实测 hgrz.pro 相似族不含自身）；ScamIntelli 11 层无明确深度上限，靠证据聚类收敛。
- **结论**：作为"本地起链 + agent 深挖"的混合架构与 ScamIntelli 全自动 11 层引擎各有取舍；差距集中在 L3/L4/L5/L8/L9 的自动化落地与层数，建议后续以 ScamIntelli 的层模型为基准补齐本地 L8（资金线索）与 L9（团伙聚合）占位。

## 更新/修复方案建议

1. **P1 优先（1 天内）**：P1-1 超长 host 500 —— 捕获 `UnicodeError` + 入参长度校验（422），并补一条 pytest（`test_p10_collector.py` 同风格：超长 seed → 422/400，非 500）。
2. **P2 快速项**：P2-5（zhihu 降级 note）、P2-6（PUT 响应 strategy 解析统一）、P2-4（stats 暴露调度快照）——各 1 处小改动。
3. **P2 增强项**：P2-2（IPv4 节点续查 L2 情报占位）、P2-3（报告 timeline 字段 + 前端时间线）、P2-7（URL 缺协议 422 或提示区分）。
4. **回归**：改动后跑 `pytest tests/test_p10_collector.py tests/test_p2_engineering.py`（含 job_snapshot 7 类 job 断言）+ 全量 317 基线，保持 health 200。
5. **运维观察**：确认 admin key 轮换机制是否有意；建议测试脚本统一改为每次请求前动态读取 key 文件。

## 收尾附注（提交时主控状态）

1. **主控当前在线**：收尾复核 `GET /api/v1/system/health` → **200** `{status:"ok",db:true}`；`GET /collector/matrix` → `{"cells":[]}`（测试单元格已全部清理，系统恢复初始空态）。
2. **期间主控被 TeamC 重启**：`var/logs/restart_teamc.{out,err}.log`（19:10:39-41）显示他队重启主控（`[af] … http://0.0.0.0:9200`，调度器 7 job）；本队在收尾健康检查时恰逢该重启窗口出现过短暂"无法连接"，随后恢复。**非本队操作**。
3. **admin key 轮换（OBS-1 复核）**：测试期间 `data/bootstrap_admin_key.txt` 由 `af_admin_5343…` 变为 `af_admin_e1e5…`，与 TeamC 重启时段吻合，疑似他队/运维有意操作；本队脚本已改为每次动态读取。
4. **OBS-2（建议队长关注）**：当前存在两个 `python -m app.run --host 0.0.0.0 --port 9200` 进程（PID 10608 = 项目 `.venv` python、PID 15108 = 系统 Python），LISTENING 套接字归 PID 15108。疑似 TeamC 重启时残留了 venv 实例；建议确认单实例部署，避免端口/DB 双写歧义。
5. 本队测试期间产生的临时文件（`data/_tmp_*.py`、`_openapi_dump.json`、`_hgrz_report_copy.json`、`_teamE_report.md`）已清理；证据快照保留在 `data/_teamE/*.json`（35 个用例）。

---
*报告双写：远程 `docs/供应链与采集矩阵验证报告_队E.md` ＋ 本地 `供应链采集_队E.md`；证据快照见远程 `data/_teamE/*.json`。*
