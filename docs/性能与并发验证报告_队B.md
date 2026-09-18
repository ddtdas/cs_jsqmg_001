# 性能与并发验证报告

- **报告方**：队B（性能与并发测试工程师）
- **验证对象**：金丝雀蜜罐 CanaryGuard AntiFraud 主控（FastAPI，端口 9200）
- **验证环境**：远程 Windows 192.168.10.110，项目根 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`
- **验证时间**：2026-09-16 18:40 – 19:20（UTC+8）
- **基线**：9200 health 200（llm: not_configured → rule-only），pytest 317 passed，events 15k+，DB SQLite WAL 模式
- **认证**：X-API-Key（验证期间 admin key 于 19:04:19 被外部 `reset-key` 轮换为 `af_admin_e1e5…`，全程以文件内当前有效 key 执行）
- **原则**：只验证/诊断，未改产品代码，未重启主控（注：主控于 19:12:22 被外部脚本重启，见缺陷清单 P2-4），未碰 dsh/ 内嵌实例与全局 3080，未提交 git

---

## 一、结论总览

**P0 × 0 / P1 × 1 / P2 × 4（观察项） / 通过项 8/8**

| 验证项 | 结论 |
|---|---|
| 1. scan/text 并发压测（20/50） | ✅ 通过（无错误、限流按预期） |
| 2. 大分页 | ✅ 通过（分页正确；page_size 上限 100 为产品约束） |
| 3. 慢查询计时 | ✅ 通过（全部 <20ms，索引覆盖良好） |
| 4. 蜜饵高频命中 | ✅ 通过（命中即退休，无死循环） |
| 5. 多实例边界 | ✅ 通过（同 data 可共存；独立 data 完全隔离） |
| 6. 调度并发 | ✅ 通过（7 job，双触发去重） |
| 7. 内存增量 | ✅ 通过（WS 几乎零增长） |
| 8. 并发写 | ✅ 通过（无丢失、无 IntegrityError） |

**P1（1）**：`supply-chain/query` 对超长/非法域名输入抛未捕获 `UnicodeError` → HTTP 500 + 异常组刷屏。
**P2（4）**：alerts/events json_extract 全表扫描（扩展性隐患）；events/cases page_size 产品上限 100 与任务参数 500/200 不一致（设计约束，需文档对齐）；主控双 python 进程并存（启动脚本残留）；主控被外部脚本重启导致个别数据含冷启动扰动（多队环境协调）。

---

## 二、逐项验证

### 1. scan/text 并发压测（rule-only，LLM 未配置）

**操作**：本地 Python threading 脚本（`perf_load_test.py`）对 `POST /api/v1/scan/text` 并发打满；admin（带 key，限流豁免）与匿名（触发 60/min 限流）两组；20/50 并发各跑；文本唯一后缀防近重合并。

**预期**：admin 组 100% 200；匿名组出现 429（rate_limit_per_min=60，admin bypass 默认开）；无 5xx/无网络错误。

**实测**：

| 场景 | 请求数 | 200 | 429 | 错误 | avg ms | p50 | p95 | p99 | max | RPS |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 并发 admin | 100 | 100 | 0 | 0 | 330 | 319 | 410 | 457 | 457 | 49.1 |
| 50 并发 admin | 100 | 100 | 0 | 0 | 1302 | 1258 | 2143 | 2198 | 2198 | 31.9 |
| 20 并发匿名 | 100 | 61 | 39 | 0 | 352 | 224 | 1116 | 1172 | 1172 | 46.7 |
| 50 并发匿名 | 100 | 14 | 86 | 0 | 617 | 906 | 1233 | 1276 | 1276 | 66.2 |
| 50 并发 admin（重压 200） | 200 | 200 | 0 | 0 | 977 | 1003 | 1264 | 1279 | 1290 | 43.4 |

**判定**：✅ 通过。
- 无任何 5xx/超时/连接错误，error_count=0 全部场景。
- 匿名组 429 严格按 60 req/min 令牌桶生效（20 并发≈61/39 分界，50 并发≈14/86，符合令牌桶容量 60 + 每秒 refill 1 行为），429 封包为 `{ok:false,error:{code:"rate_limited"}}`。
- 50 并发 admin 延迟显著抬升（p95 2143ms vs 20 并发 p95 410ms），RPS 不升反降（49→32），主要瓶颈为 SQLite WAL 单写者串行化 + 每条检测的 simhash/bigram 近重扫描 + 事件落库；在 15k 级数据量下仍在可接受范围，但高并发写入时延呈线性恶化（见更新建议）。

### 2. 大分页

**操作**：带 admin key GET `/events?page_size=500|100`、`/cases?page_size=200|100`、`/scan/hits?limit=200`、`/traps?limit=500`，记录响应时间与分页正确性（total 一致、id 连续、无重复）。

**实测**：

| 接口 | 状态 | 耗时 ms | total | 返回条数 | id 区间 | 重复 id |
|---|---|---|---|---|---|---|
| /events?page_size=500 | **422** validation_error（le=100） | 60 | - | - | - | - |
| /events?page_size=100 | 200 | 102 | 16322 | 100 | 16241–16340 | 0 |
| /events?page_size=100&page=2 | 200 | 22 | 16322 | 100 | 16141–16240 | 0 |
| /cases?page_size=200 | **422** validation_error（le=100） | 13 | - | - | - | - |
| /cases?page_size=100 | 200 | 43 | 1692 | 100 | 1595–1694 | 0 |
| /scan/hits?limit=200 | 200 | 33 | - | 200 | - | - |
| /traps?limit=500 | 200 | 52 | - | 500 | - | - |

**判定**：✅ 通过（含一处参数对齐标注）。
- 分页正确性全部 OK：page1/page2 id 连续无缝隙、无重复、total 恒定 16322。
- `page_size=500/200` 返回 422 属**产品设计约束**（FastAPI Query le=100，防深分页），非缺陷；但任务参数（500/200）与产品上限不一致，建议任务文档/接口文档对齐（P2-3）。
- traps limit 上限 500 与任务参数完全吻合；scan/hits limit 上限 200 吻合。

### 3. 慢查询计时

**操作**：远程 .venv Python 对 SQLite `data/af.db` 直接执行主要查询 5 次取 min/avg/max，并 EXPLAIN QUERY PLAN。

**实测**（avg/min/max ms，rows）：

| 查询 | avg | min | max | rows |
|---|---|---|---|---|
| events 列表 LIMIT 100 | 0.20 | 0.16 | 0.28 | 100 |
| events COUNT(*) | 0.04 | 0.02 | 0.09 | 1 |
| events WHERE kind=?（走 idx_events_kind_ts） | 18.01 | 15.97 | 21.77 | 100 |
| events LIMIT 5000 | 9.46 | 8.20 | 10.31 | 5000 |
| speech_hits by det_id（覆盖索引） | 0.14 | 0.01 | 0.67 | 0 |
| detections+hit_count 相关子查询（scan/hits 核心） | 0.47 | 0.43 | 0.52 | 200 |
| detections by id | 0.03 | 0.02 | 0.03 | 1 |
| detections by text_hash | 0.01 | 0.01 | 0.02 | 1 |
| 近重扫描（近 100 条） | 0.17 | 0.17 | 0.19 | 100 |
| alerts json_extract(payload) | 2.86 | 2.69 | 2.99 | 0 |
| cases 列表 LIMIT 100 | 0.23 | 0.23 | 0.24 | 100 |
| traps 列表 LIMIT 500 | 1.80 | 1.66 | 2.03 | 500 |

**EXPLAIN 关键点**：
- `events WHERE kind=?`：SEARCH idx_events_kind_ts + `USE TEMP B-TREE FOR ORDER BY`（ORDER BY id 需临时排序，1.5 万行 kind 过滤 18ms）。
- `speech_hits`：SEARCH idx_speech_hits_det（覆盖）+ pattern 主键 LEFT-JOIN，快。
- `detections + hit_count 子查询`：SCAN d + 相关子查询（每行二次查 speech_hits 覆盖索引），200 行 0.47ms，当前数据量无碍。
- **`alerts json_extract(payload,'$.detection_id')`：SCAN alerts（全表扫描，无索引）** — 当前 1471 行 2.9ms，随 alerts/events 增长将线性恶化（P2-1）。events 表同类 `json_extract(payload,'$.detection_id')` 查询同样全表扫。

**判定**：✅ 通过（当前量级全部 <20ms）；附 P2 扩展性建议。

### 4. 蜜饵高频命中（同蜜饵 10 次 check-hit）

**操作**：创建蜜饵 → deploy（active）→ 同一 bait 文本连续 10 次 `POST /traps/{id}/check-hit`。

**实测**：

| 步骤 | 状态 | 耗时 ms | 结果 |
|---|---|---|---|
| create trap | 200 | 7172（异常，见 P2-4） | trap_id=1379 |
| deploy | 200 | 25 | status=active |
| check-hit #1 | 200 | 52 | **hit=True，status=retired，hit_count=1** |
| check-hit #2 | **409** | 81 | trap_retired |
| check-hit #3–#10 | 409（×9） | 15–41 | trap_retired |

**判定**：✅ 通过。
- 状态机符合设计：`active → monitored(隐式) → hit → retired`，命中即退休（D2 防反查）。
- 第 1 次命中后后续 9 次全部 409 `trap_retired`，**无死循环、无 5xx、无重复命中计数**（hit_count 停在 1）。
- 复测 create trap 3 次：7943ms / 507ms / 394ms → 首笔异常为冷启动/外部重启扰动（P2-4），非业务缺陷。

### 5. 多实例边界（9201 端口另起实例）

**操作**：WMI 方式在远程另起 `python -m app.run --port 9201`（**同 data**，af.db WAL）与 `--port 9202`（**独立 data** `data_perf9202/`），验证互不影响与锁冲突，测试后已清理两实例。

**实测**：

| 验证 | 结果 |
|---|---|
| 9201 同 data：health | 200（62ms），llm: not_configured |
| 9201 同 data：POST /scan/text 写 | 200（107ms），detection_id=3112 |
| 9201 同 data：stats | events=16336 detections=3103，与 9200 **完全一致**（同一 WAL 库） |
| 9202 独立 data：health/stats | 200；traps=0 detections=0 events=0（全新空库，独立 bootstrap key af_admin_f484…） |
| 9202 独立 data：POST /scan/text 写 | 200，detection_id=1（独立库从 1 起） |
| 9202 写后 9200 stats | traps=1380 detections=3103 events=16336（**零影响**） |
| DB 锁冲突 | 未触发 database is locked（WAL + busy_timeout=5000 生效） |

**判定**：✅ 通过。
- 同 data 双实例可共存读写（WAL 多进程语义正确），**但两个实例各自注册并运行一套 7-job 调度器**：job_collector_matrix 等将双份执行同一矩阵/任务 → 重复采集/重复事件风险（观察项，建议单实例运行或调度器加进程锁，见更新建议）。
- 独立 data 实例完全隔离（独立库/独立 key/独立 id 空间）。
- 注：首次用 `Start-Process` 启动的 9201 进程在 SSH 会话结束后被环境清理/进程树回收（PID 消失、日志无异常），改用 WMI `Win32_Process.Create` 后稳定——环境对 SSH 子进程有回收机制（观察项，P2-5）。

### 6. 调度并发

**操作**：远程 .venv 独立进程 `create_scheduler()` + `job_snapshot()`；手动触发 `job_collector_matrix`/`job_scan_dm` 各 2 次观察去重。

**实测**：

| job id | 触发器 | max_instances | coalesce | 说明 |
|---|---|---|---|---|
| job_scan_dm | interval 10min | 1 | True | pending→scanned |
| job_zhihu_sync | interval 5min | 1 | True | 知乎拉取（no-op 安全） |
| job_trap_recheck | interval 6h | 1 | True | 蜜饵复查 |
| job_account_scan | interval 2h | 1 | True | 账号画像 |
| job_evidence | interval 12h | 1 | True | 证据包 |
| job_case_publish | cron 3:00 | 1 | True | 案例就绪事件 |
| job_collector_matrix | interval 1min | 1 | True | 采集矩阵轮询 |

- `job_snapshot()` 返回 7 条，与蓝图 §二 6+1 类一致（P2-2 声称"6 类"实为 7 个注册 job，文档口径差异，P2-6）。
- 手动触发 job_collector_matrix 两次：run1=5.2ms（轮询），run2=0.2ms（节流/幂等，无重复单元格执行）。
- 手动触发 job_scan_dm 两次：run1=137.4ms（消费 pending），run2=3.4ms（无 pending 直接返回，**不重复处理**）。
- 运行中主控日志确认 job_collector_matrix 每 60s 轮询且仅记「轮询 0 个启用单元格」。

**判定**：✅ 通过。max_instances=1 + coalesce=True + 任务内幂等三保险，手动双触发无重复工作。

### 7. 内存增量（压测后主控进程 WS）

**操作**：Get-Process 对比 200 并发 admin + 5 并发写压测前后 PID 15108（当前监听 9200 的进程）。

**实测**：

| 时刻 | WS | PM | CPU |
|---|---|---|---|
| 压测前 | 67.5 MB | 55.9 MB | 3.5s |
| 压测后（250 请求） | 67.6 MB | 62.0 MB | 7.4s |
| **增量** | **+0.1 MB** | +6.1 MB | +3.9s |

**判定**：✅ 通过。WS 几乎零增长，PM +6.1MB 为 Python 堆正常浮动，无泄漏迹象。

### 8. 并发写（5 线程同时 POST /scan/text）

**操作**：5 线程同时 POST 不同文本（两轮：强差异文本组 / 相似前缀组）。

**实测**：
- 强差异文本组（A–E 五种骗术类型）：5/5 全部 200，detection_id=3097–3101 **各自落库，无丢失、无 IntegrityError**，耗时约 504ms/请求。
- 相似前缀组（并发写测试文本{0-4}…+时间戳）：5/5 全部 200，但返回 detection_id 合并为 {3113,3113,3113,3096,3113} —— **P3-9 近重合并（bigram overlap ≥0.6 / SimHash ≤8）按设计复用已有检测行**，非丢写、非缺陷；任务要求"不同文本全部落库"在**文本显著不同**时完全满足。
- 压测后 `PRAGMA integrity_check` = **ok**，`quick_check` = ok；最终计数 detections=3104（基线 3078，+26 净增，去重生效），events=16746（基线 15221，+1525），speech_hits=4099（+19）。

**判定**：✅ 通过（无丢失、无 IntegrityError、DB 完整性 OK）。

---

## 三、缺陷清单

| 级别 | 位置 | 现象 | 复现 | 修复建议 |
|---|---|---|---|---|
| **P1** | `app/api/supply_chain.py` `_resolve()`（L46 起，仅捕获 `OSError`） | 超长/非法域名经 `socket.getaddrinfo` 触发 `UnicodeError: label empty or too long`（idna 编码），未捕获 → HTTP 500 `internal_error` + Starlette ExceptionGroup 异常刷屏 `var/logs/app.log`（本次复现 3+ 次，19:01/19:07 各一条） | `POST /api/v1/supply-chain/query` body `{"seed":"https://<70个a>.com/x"}`（带 admin key）→ 500 | `_resolve` 改为捕获 `(OSError, UnicodeError)`；或在 `getaddrinfo` 前用 idna 预编码 try/except，失败按 NXDOMAIN 处理（不抛 500）；建议单测补非法域名用例 |
| P2-1 | `app/api/detections.py` L~100 alerts 查询、L~120 events 查询（`json_extract(payload,'$.detection_id')`） | 全表扫描（EXPLAIN: SCAN alerts/events），当前 1.5k–16k 行 3ms 无感，随数据增长线性恶化 | 数据量扩大后观察 | payload 增加冗余 `detection_id` 列 + 索引，或建 SQLite 表达式索引 `CREATE INDEX … ON alerts(json_extract(payload,'$.detection_id'))` |
| P2-2 | `app/api/events.py`/`cases.py` page_size le=100 | 任务要求测 page_size=500/200 直接 422，与文档参数不一致（产品防深分页设计合理，但需对齐口径） | `GET /events?page_size=500` | 接口文档/任务书标注上限 100；如确需 500 可分页聚合或放宽 |
| P2-3 | 主控启动脚本（start.bat/start.ps1 相关） | 同一时刻存在双 python 进程均带 `--port 9200`（`.venv` 10608 WS≈0.5MB 未监听 + 系统 Python 15108 监听），疑似启动脚本双解释器竞争残留，日志多次「调度器已启动」 | `Get-CimInstance` 观察 | 启动脚本统一解释器，启动前清理旧 PID 文件，增加端口占用检测 |
| P2-4 | 环境协作 | 主控于 19:12:22 被外部脚本重启（日志 5 次「调度器已启动」：18:32/18:40/19:09/19:10/19:12），admin key 于 19:04:19 被 `reset-key` 轮换；导致本次 create trap 首笔 7.9s、部分延迟数据含冷启动扰动 | 多队共享同一远程实例 | 各队操作前 `system/health`+`auth/current-key` 确认，避免并发 reset-key/重启；如需重启请在报告交接 |
| P2-5 | SSH/进程生命周期 | `Start-Process` 启动的 9201 子进程随 SSH 会话被回收（进程消失无日志）；WMI `Win32_Process.Create` 启动稳定 | 远程另起实例 | 文档建议用 WMI/schtasks 方式启动后台实例；或改用 `nohup` 等价物 |
| P2-6 | `app/services/scheduler.py` 注释 | 注释写「6 类 job」，实际注册 7 个（含 job_collector_matrix），口径不一致 | 代码阅读 | 注释更新为 7 个 |

---

## 四、对标差异（ScamIntelli 11 层引擎）

ScamIntelli 以 11 层多源研判引擎为卖点（域名/黑灰产/资金流/社交图谱等分层），本蜜罐为**轻量自包含反诈蜜罐**，两者定位不同，仅在性能层面对标：

- **查询深度**：ScamIntelli 分层查询每层独立网络/数据源，单条查询秒级至分钟级（OSINT 网络往返）；本系统本地 SQLite 单条检测 <20ms，重查询全部本地完成，**延迟优势显著**，适合高频实时护栏场景。
- **并发模型**：ScamIntelli 通常面向低频深查（人工研判），本系统 50 并发 admin 压测全部 200（avg 1.3s 内），可支撑蜜饵高频命中与批量导入。
- **扩展性差距**：ScamIntelli 的 11 层适合数据稀疏但维度深；本系统 SQLite 在 detections 十万级以下表现良好，json_extract 全表扫描（P2-1）为随规模恶化的第一风险点，与 ScamIntelli 的分层索引设计存在差距，建议补表达式索引/冗余列。
- **限流/防刷**：本系统 60/min 令牌桶 + admin 豁免优于多数蜜罐默认配置，匿名高频路径 429 严格生效（实测 50 并发匿名 86% 429）。

---

## 五、更新/修复方案建议

1. **P1 立即修**：`supply_chain._resolve` 捕获 `(OSError, UnicodeError)` 并将非法域名归为 NXDOMAIN 证据（不打 500）；补单测（超长 label、中文域名、空 label）。
2. **P2-1 本迭代修**：为 alerts/events 的 `json_extract(payload,'$.detection_id')` 建表达式索引或冗余列；`scan/hits` 的核心相关子查询在 detections 超过 5 万行后建议改为 JOIN 聚合。
3. **并发写入优化（若需更高 RPS）**：当前 50 并发时 SQLite 单写者成为瓶颈（p95 2.1s）；可评估 WAL checkpoint 频率、写批量合并（同一事务批量插 events）或写侧队列；对蜜饵场景（低频写、高频 check-hit 读）当前足够。
4. **调度器单例化**：多实例部署时调度器应仅主实例运行（进程锁或环境变量开关），避免 job_collector_matrix/job_scan_dm 双份执行；同 data 多实例建议只在故障转移场景使用。
5. **运维口径**：文档/任务参数 page_size 上限 100、调度 job 数 7；启动脚本清理双解释器残留；多队共享环境操作前确认 key 与进程指纹。

---

*报告结束。所有压测数据与复现均来自真实 HTTP 请求与 DB 直查，未修改产品代码。*
