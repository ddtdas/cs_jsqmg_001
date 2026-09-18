# 数据完整性与异常恢复验证报告（队C·数据完整性）

- 测试工程师：队C（金丝雀蜜罐项目）
- 测试对象：远程 Windows 192.168.10.110，`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`
- 执行时间：2026-09-16
- 基线：9200 主控健康（llm not_configured，db ok）；`pytest 317 passed`；gate_logs 哈希链 1803 行 valid
- 原则：只验证/诊断，未改任何产品代码；DB 先备份（`data\backup_teamC_baseline\`）；测试产生的数据已全部清理；主控已恢复并持久运行

---

## 一、结论总览

**P0 × 0 / P1 × 2 / P2 × 5**

9 项验证全部执行完毕：

| 编号 | 验证项 | 结论 |
|---|---|---|
| 1 | gate_logs 哈希链 | ✅ 通过（篡改→409，恢复→valid） |
| 2 | 证据包防篡改 | ✅ 通过（build→freeze→篡改→detected→export 仍可导） |
| 3 | 迁移幂等 | ✅ 通过（ensure_schema ×3 零差异） |
| 4 | 半写事务原子性 | ⚠️ 现状确认（P2-10：三写非事务，部分提交存在） |
| 5 | 主控重启恢复 | ✅ 通过（停→起→4s 健康，stats 计数不变；另发现运维级会话回收问题） |
| 6 | 并发同记录写 | ⚠️ 部分通过（单行落库无损坏，但事件流重复） |
| 7 | 空库首次启动 | ✅ 通过（16 表 + bootstrap key + 种子 + 健康） |
| 8 | 外键/孤儿 | ⚠️ 现状确认（全库零 FK，删主记录留孤儿） |
| 9 | 数据截断 | ✅ 通过（content→4000；se_factors 结构级有界） |

一句话总评：**数据存取完整性、哈希链防篡改、迁移幂等、重启恢复与空库自举均达到设计预期；主要风险集中在"多写无事务包裹（P1-1）"与"证据包依赖源行存在（P1-2）"两项**，并发/孤儿/事件重复为次要问题（P2）。

---

## 二、逐项验证

### 1. gate_logs 哈希链 ✅

- **操作**：`GET /api/v1/system/audit-verify`（admin key）→ 记录基线；备份 DB；SQL `UPDATE gate_logs SET reason='TEAMC_TAMPER_MARK' WHERE id=<max>`；再调 audit-verify；恢复原值；三调 audit-verify。
- **预期**：基线 valid；篡改后 409 `audit_chain_broken`；恢复后再次 valid。
- **实测**：
  - 基线：`{"ok":true,"data":{"valid":true,"checked":1803}}`
  - 篡改尾行 reason 后：HTTP 409，body `{"ok":false,"error":{"code":"audit_chain_broken","message":"GateLog 哈希链在第 1808 行断裂（篡改或迁移异常）"}}`（broken_at 精确定位到被改行）
  - 恢复原值后：`{"ok":true,"data":{"valid":true,"checked":1804}}`（期间其他队并发新增 1 行，链仍完整）
- **结论**：✅ 通过。改任一行（before/after/reason/action/ts/payload_json/prev_hash）即断裂并可精确定位；`write_gate_log` 覆盖 auth.reset-key / cases.publish / config 修改 / traps 部署与退役 / account_bridge 等敏感操作。
- **证据**：见上；链校验实现 `verify_gate_chain()`（app/services/audit_log.py）。

### 2. 证据包防篡改 ✅

- **操作**：`POST /scan/text`（含"杀猪盘 稳赚不赔 包赔 刷单返利"，rule_score=37）造检测 det 3092 → `POST /evidence/build`（pkg 732，frozen=0）→ `POST /evidence/732/freeze`（frozen=1）→ 直改 DB `detections.content` → `GET /evidence/732`（verify）→ `GET /evidence/732/export?fmt=json` → 恢复 content → 再 verify → 清理。
- **预期**：build 链建立；freeze 后内容变更 → verify intact=false；export 仍可导出（带 verified 标注）；恢复后 intact=true。
- **实测**：
  - build：`pkg_id=732 frozen=0 nodes=1`；freeze：`frozen=True intact=True`
  - 篡改 content 后：`intact=False`，detail=`TAMPERED：内容与哈希链不一致`
  - 篡改后 export：`export_ok=true fmt=json verified_intact=False items=1`（**仍可导出**，完整内容+链+校验结果）
  - 恢复 content 后：`intact=True`
- **结论**：✅ 通过。哈希链重算比对有效；export 不受篡改阻断（可导出供人工核验，符合存证语义）。
- **证据**：EvidenceService.build_chain/verify（app/services/evidence.py）。

### 3. 迁移幂等 ✅

- **操作**：远程 venv Python 调 `ensure_schema()` 连续 3 次，前后对 16 张表做 `PRAGMA table_info` 列集合 + 行数快照比对。
- **预期**：3 次无报错；列结构、行数不变。
- **实测**：`run1/2/3: OK`，`ERRORS: []`，`DIFFS: {}`；speech_patterns 61→61、api_keys 41→41、gate_logs 1804→1804。16 张表（14 DDL + configs + gate_logs）零差异。
- **结论**：✅ 通过。`CREATE TABLE IF NOT EXISTS` + `INSERT OR IGNORE` + 增量迁移（PRAGMA 探测补列）+ 一次性链回填（`_gate_chain_v2_repaired` 标记）双保险，任意重入安全。

### 4. 半写事务原子性 ⚠️（P2-10 现状确认）

- **操作**：直插临时 detection，monkeypatch `AlertService.evaluate` 抛 `RuntimeError`（模拟第三写中断），调 `GradingService().finalize()`，检查三写落库状态。
- **预期**：P2-10 已知非事务——三写（① `UPDATE detections SET grade/grade_reason` + commit；② `INSERT events('detection_graded')` + commit；③ `AlertService.evaluate` 写 alerts）各自独立提交；中断后前序写保留。
- **实测**：`finalize raised: RuntimeError`；落库状态 `grade_committed=True grade=L3 event_detection_graded_committed=1 alert_evaluate_calls=1`——**grade 已提交、事件已提交、告警缺失，部分提交实锤**。
- **结论**：⚠️ 确认现状。中途失败会产生"已分级但无告警/事件不完整"的中间态；无回滚/补偿机制。风险等级 P1（见缺陷清单 P1-1）。

### 5. 主控重启恢复 ✅

- **操作**：记录 stats 基线 → 停 9200 双进程（PID 12660 监听者 + 其父 10496 venv stub；venv launcher 会派生基础解释器形成父子结构）→ 确认端口释放 → 以原命令 `.venv\Scripts\python.exe -m app.run --host 0.0.0.0 --port 9200` 重启 → 探活 → stats/audit-verify 对比。DB 已预先备份。
- **预期**：重启后数据完好，stats 计数不变。
- **实测**：
  - 重启 4 秒内 health 200，`db=true`
  - stats 对比：detections 3100→3100、alerts 1475→1475、unread_alerts 1439→1439、cases 1692→1692、accounts 30→30、grades 分布 L1:586/L2:1039/L3:1470/L4:3 **完全一致**（traps/events 的 ±5 为其他队并发写入，非重启产生）
  - audit-verify：`{valid:true, checked:1809}`（重启不破坏哈希链）
- **运维级发现（额外 P2）**：ssh 会话内用 `Start-Process` 拉起的 9200 在**会话结束后进程被 Job Object 回收**（实测中断）；改用 WMI `Win32_Process.Create`（父进程为 WMI 服务，脱离会话 job）后持久存活，新会话探活 200、scan 冒烟通过。→ 远程/CI 场景启动主控必须脱离 ssh 会话（WMI / schtasks / 服务化）。
- **结论**：✅ 通过（含恢复过程运维坑的定位与规避）。

### 6. 并发同记录写 ⚠️

- **操作**：两个 PowerShell Job **同时** `POST /api/v1/scan/text` 同一全新文本（TEAMC-CONC-* + 命中词）。
- **预期**：sha256 去重生效且无损坏（任务口径：去重/各自落库均无损坏）。
- **实测**：
  - 两线程均返回 `det=3105`（同一 id）；DB `rows_with_same_hash=1`——**去重成功，无重复行、无损坏** ✅
  - 但事件流重复：`event 16092/16095 detection_scanned ×2`、`16093/16096 detection_graded ×2`、`alert_created ×1`
  - alerts 表靠 `json_extract(payload,'$.detection_id')` 幂等（仅 1 条）✅
- **结论**：⚠️ 数据层去重 OK；事件层不幂等（P2-1），`_persist` 去重命中分支仍重复写 scanned/graded 事件。

### 7. 空库首次启动 ✅

- **操作**：`AF_DATA_DIR=.tmp_teamc\emptydb`（空目录）+ `AF_PORT=9210` 独立起临时实例（venv python `-m app.run`）→ 探活 → 查表/种子/boot key → 停止 → 确认 9200 无扰。
- **预期**：全表创建 + bootstrap key 生成与登记 + 正常健康。
- **实测**：
  - health 200（db:true；调度器日志"已启动 7 个 job"）
  - `bootstrap_admin_key.txt` 自动生成（43B）且在 `api_keys` 登记 1 条（role=admin）
  - **16 张表全部创建**：account_bridges/accounts/alert_rules/alerts/api_keys/cases/collector_matrix/configs/detections/events/evidence_packages/gate_logs/honey_facts/speech_hits/speech_patterns/zhihu_sessions
  - speech_patterns 种子 61 条；configs 含 `_gate_chain_v2_repaired` 标记；gate_logs 空（空链 valid）
  - 停止后 9200 主控 health 200 不受影响
- **结论**：✅ 通过。零预置自举完备，可作为部署基线。

### 8. 外键/孤儿 ⚠️

- **操作**：`PRAGMA foreign_key_list` 逐表检查；插入临时 det + 2 条 speech_hits + 1 个 evidence_packages → SQL `DELETE detections` → 查残留；插入临时 account → 删除 → 查关联。
- **预期**：项目无 FK（任务口径：记录现状）。
- **实测**：
  - **全库零 FOREIGN KEY**：speech_hits / evidence_packages / cases / alerts / detections / accounts 的 `PRAGMA foreign_key_list` 全部为空（`PRAGMA foreign_keys=ON` 已开，但无 FK 定义则不生效）
  - 删 detection 3108 后：`speech_hits_left=2 evidence_packages_left=1`——**孤儿残留实锤**
  - 删 account：无子表 FK 引用（accounts 无子表；trap_hit_ids/signals 为表内 JSON 文本），无孤儿风险 ✅
- **结论**：⚠️ 现状确认。`speech_hits.det_id`、`cases.det_id`、`evidence_packages.det_ids`（JSON 数组）、`alerts.rule_id` 等全部无 DB 级约束；删主记录不级联。若孤儿可接受（如证据包存证需保留）应显式设计，否则建议补 FK/CASCADE（P2-2）。

### 9. 数据截断 ✅

- **操作**：`POST /scan/text` 11989 字符文本（前缀 + "杀猪盘 稳赚不赔" + 11950×"测"）→ 查落库 content 与 se_factors。
- **预期**：content 截 4000；se_factors 超长是否截断（记录现状）。
- **实测**：
  - 输入 len=11989 → 落库 `content_len=4000`（`text[:4000]` 切片，head/tail 正确）✅
  - `text_hash` 为**完整输入**的 sha256（去重基于全文，截断仅影响存储）✅
  - `se_factors_len=665`（JSON）：结构有界——`evidence` 每条 `text` 经 `_snippet` 截断至 **200 字符+省略号**；12000 字输入只产出 2 条证据、665B
  - 现状：se_factors **无整体长度硬上限**，但受"因子数×200/片段"自然约束（最坏 ~23 因子×201 ≈ 4.6KB 量级）
- **结论**：✅ content 截断符合预期；se_factors 有界但无 DB 兜底上限（P2-4 建议）。

---

## 三、缺陷清单

| 级别 | 位置 | 现象 | 复现 | 修复建议 |
|---|---|---|---|---|
| **P1-1** | `app/services/grading.py::finalize` | 三写（grade 落库 / events / alerts）各自独立 commit，无事务包裹；第三写失败时 grade+event 已提交、alert 缺失，产生"已分级未告警"中间态 | monkeypatch evaluate 抛异常后观察：grade=L3 与 detection_graded 事件已落库，alert 缺失 | 三写合并单事务（一个 commit），失败整体回滚；或引入业务补偿。最小改动：去掉前两次显式 commit，把 `AlertService.evaluate` 纳入同一提交点 |
| **P1-2** | `app/services/evidence.py::verify/_detection_items` | 冻结证据包强依赖源 detections 行存在；源行被删（当前无 FK 保护）后 verify/export 抛 `detection_not_found`，已冻结存证无法校验/导出（存证失效） | 测试8 已演示：删 det 后 evidence_packages 残留，verify 将 404 | build 时将检测明细**快照**入 evidence_packages（如 `items_json`），verify/export 基于快照而非实时行；或禁止删除已被 evidence 引用的 detection |
| **P2-1** | `app/services/speech_engine.py::scan_text/analyze_detection` + `app/services/grading.py::finalize` | 并发相同文本：detections 去重成功（单行），但 events 流重复写入（2× detection_scanned + 2× detection_graded），events 计数膨胀 | 两线程同文本并发 POST /scan/text（实测 id 相同、事件 5 条） | 事件写入按 (kind, detection_id) 幂等（如 UNIQUE 索引或写入前查重）；alerts 已有的 json_extract 幂等模式可推广到 events |
| **P2-2** | 全库 DDL（app/models/__init__.py） | 16 表零 FOREIGN KEY（fks 已开但无定义）；删 detection 后 speech_hits×2、evidence_packages×1 孤儿残留 | 测试8 实锤 | 决策后再落地：speech_hits.det_id 加 `FOREIGN KEY ... ON DELETE CASCADE`（或应用层随删随清）；evidence 保留（存证语义）配套 P1-2 快照化 |
| **P2-3** | `gate_logs` 表 | 存在 id 空洞（1803 行时 max id=1807，4 个缺失），疑似先前测试直接删除过行；不影响链校验（按 id 序 + prev_hash），但删除尾部行会改变空洞 | `SELECT COUNT(*), MAX(id) FROM gate_logs` | 审计链应禁止物理 DELETE（或 DELETE 本身记一条审计）；归档采用标记式 |
| **P2-4** | `detections.se_factors` / `speech_hits.matched_text` | se_factors 无整体长度上限（实测 665B，受 _snippet 200/片段约束；极端多因子最坏 ~4.6KB）；matched_text 无截断 | 12000 字文本扫描 | 建议 se_factors 加整体上限（如 4096，超限截断或压缩证据片段）；matched_text 加长度截断 |
| **P2-5**（运维/部署） | 远程启动方式 | ssh 会话内 Start-Process 启动的 9200 在会话结束后被 Job Object 回收（实测中断）；非产品代码缺陷 | 测试5 实测 | 远程/CI 启动须脱离会话：WMI `Win32_Process.Create`（已验证有效）/ schtasks / Windows 服务化；本地 start.bat 手动窗口不受影响 |

---

## 四、对标差异（与 ScamIntelli 11 层引擎对比）

| 维度 | 本项目现状 | ScamIntelli 11 层类引擎 | 差异评估 |
|---|---|---|---|
| 审计链锚点 | gate_logs 哈希链与内容**同库共存**，防误改/意外篡改，不防持库者重算（代码注释已声明局限） | 常见做法为外置链根（HMAC/数字签名）或可信时间戳（RFC3161） | 建议证据/审计导出时附带外置校验文件（HMAC(secret, chain_root)）作为外部锚点（已有 P2-C5 说明，属增强项） |
| 写入事务性 | SQLite WAL + 请求级连接；业务**多写未包裹事务**（P1-1） | 分层引擎通常有明确的写入层/事务边界 | P1-1 修复后可对齐 |
| 引用完整性 | 无 DB 级 FK，靠应用层 join + JSON（det_ids） | 模型层建议引用完整性 | P2-2 |
| 幂等 | alerts 有 json_extract 幂等；detections text_hash 去重；events **无幂等** | 事件/审计流幂等是标配 | P2-1 |
| 恢复与自举 | WAL + 幂等迁移 + 空库自举完备（16 表 + bootstrap + 种子），重启无损 | — | ✅ 无差距（已验证） |
| 截断与容量 | content 4000、se_factors 片段 200 有界 | — | ✅ 达标，补整体上限更稳 |

---

## 五、更新/修复方案建议

1. **（P1-1，高优先）** `GradingService.finalize()` 三写事务化：移除前两次显式 `conn.commit()`，将 grade UPDATE、event INSERT、alert evaluate 收敛为请求级单事务（get_db 结束时统一 commit），失败整体回滚；或包 try/except 补偿。预计改动 <10 行、回归面小（scan/inbox/traps 检测主链路均经 finalize）。
2. **（P1-2，高优先）** 证据包快照化：`EvidenceService.build()` 时把检测明细（内容/hits/评分）快照写入 `evidence_packages`（新增 `items_json` 列，迁移幂等），`verify/export/report_template` 基于快照；配合 P2-2 允许 evidence 引用的 det 被删而不失效。
3. **（P2-1）** events 幂等化：`detection_scanned`、`detection_graded` 按 detection_id 去重写入（写前查重或唯一索引），与 alerts 的 json_extract 幂等对齐。
4. **（P2-2）** FK 策略决策：`speech_hits.det_id` 建议 `REFERENCES detections(id) ON DELETE CASCADE`（或应用层联动删除）；`evidence_packages.det_ids` 保持 JSON 但依赖 P1-2 快照；`cases.det_id` 同理评估。改 DDL 需走迁移（ensure_migrations 模式）。
5. **（P2-4）** se_factors 提交前加整体长度守卫（>4096 截断/降采样），matched_text 加长度上限；防止异常输入在审计/导出链路膨胀。
6. **（P2-3）** gate_logs 增删管控：禁止物理 DELETE（改为 `superseded` 标记）或删除操作本身记链。
7. **（P2-5，运维）** 远程/CI 启动主控统一用"脱离会话"方式（WMI / schtasks / NSSM 服务），文档化该坑；本地手动 start.bat 不受影响。
8. **回归建议**：上述修复后重跑 `pytest`（基线 317 passed）并复测本报告 9 项中的 1/2/4/6。

---

## 附：测试环境澄清与清理说明

- 测试产生的全部 TEAMC-* 数据（detections/speech_hits/events/alerts/evidence_packages）已精确清理（含并发/冒烟/半写/孤儿用例），主控终态：health 200、audit-verify `{valid:true, checked:1809}`、9200 监听 PID 15108（WMI 启动，持久存活）。
- DB 基线备份保留于远程 `data\backup_teamC_baseline\`（af.db + wal + shm）。
- 测试脚本保留于远程 `.tmp_teamc\`（tamper_restore_gate.py / manage_evidence.py / ensure_schema_x3.py / finalize_atomic2.py / concurrent_check.py / orphan_check.py / trunc_check.py / clean_all.py 等），供队内复核。
- 测试期间主控曾出现 1 次约 2 分钟的短暂中断（P2-5 运维发现所致），已恢复并验证持久性；期间其他队并发写入数据未受损。