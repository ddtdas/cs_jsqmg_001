# 话术库验收报告（队C · 测试与双盲交叉验收）

- **验收对象**：话术库适应更新（speech-patterns CRUD/import/export/toggle + 在线学习回写 + P17 测试 + 前端 SpeechPatternsView）
- **验收人**：tester-c（队C，双盲独立复核，不依赖队A/队B自述）
- **验收时间**：2026-09-17 12:25 – 12:55
- **环境**：远程 192.168.10.110（根 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`）；9200 主控 health 200
- **方法**：只读代码审查 + 隔离数据目录 API 实测（TestClient + 中文 UTF-8 字节 POST）+ 真实库只读回归 + 全量 pytest 独立运行；未改产品代码、未重启主控、未提交 git

---

## 一、总体结论

| 维度 | 结论 |
|---|---|
| 代码实现正确性 | **PASS（21/21 独立验收项全绿）** |
| 单元测试 | test_p17 21 passed / 全量 429 passed（≥424 达标） |
| 前端契约 | **PASS**（client.js 7 方法与后端 7 端点一一对应） |
| 在线学习回写 | **PASS**（weight+1 封顶10、pattern.boost 审计、hit_count 递增） |
| 回归风险 | **PASS**（原 61 条 seed 词条完好，正常语料 0 误命中） |
| **部署状态** | **FAIL / P1**：9200 运行实例为 09:09 启动的旧代码，**未加载 speech-patterns 新路由**（OpenAPI 无该路由、实测 404）；真实库 speech_patterns 缺 `hit_count` 列（P17 迁移未执行）。**新功能代码正确但未上线生效** |

**交付判定：代码层可交付；部署层不可交付（需重启主控使新代码生效后才能验收线上功能）。**

> ⚠️ 队A/队B 声称"已实现"的是**磁盘上的代码**，运行中的主控仍是旧版本。前端 dist 已构建并已被 9200 提供（`/ui/assets/SpeechPatternsView-xXoljqhL.js` 200），页面会加载但调用 `/api/v1/speech-patterns` 将得到 404。**前端已上线、后端未上线，前后端部署不同步。**

---

## 二、逐项验收矩阵（代码级实测，磁盘最新代码）

> 双盲方式：自行编写独立验收脚本 `var/t3c_independent_verify.py`（21 项），在隔离数据目录（AF_DATA_DIR=temp）起 TestClient 对**磁盘最新代码**执行，中文请求体走 JSON UTF-8 字节（ensure_ascii=False + utf-8 encode）；与队A `tests/test_p17_speech_lib.py`（21 项）相互独立。两套脚本结论一致：**21/21 与 21/21 全 PASS**。

| # | 验收项 | 期望 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | GET /speech-patterns 列表结构 | `{items,total}` 含 hit_count | 200，`{total:61, items:[…]}`，字段含 id/pattern/regex/weight/category/source/enabled/hit_count/hit_history/created_at | ✅ PASS |
| 2 | GET 分页+category+keyword | 过滤生效 | page_size=3 生效、category=fake_investment 精确过滤、keyword=稳赚 命中 pattern/regex | ✅ PASS |
| 3 | POST / 新建（中文 UTF-8） | 200 落库 | 200，source=manual，enabled=1，pattern 精确保存 | ✅ PASS |
| 4 | POST 重复 pattern | 拒绝 | **409** duplicate_pattern（队A测试同断言 409；任务书写 422，见 P2-1） | ✅ PASS* |
| 5 | POST weight 越界（0/11） | 422 | 0 与 11 均 422（Pydantic ge=1,le=10） | ✅ PASS |
| 6 | POST category 非法 | 422 | 422 invalid_category，枚举提示完整 | ✅ PASS |
| 7 | PUT /{id} 无 confirm | 403 | 403 confirm_required | ✅ PASS |
| 8 | PUT confirm=true reason<20 | 422 | 422 reason_too_short | ✅ PASS |
| 9 | PUT confirm+reason≥20 | 200 + 审计 | 200 更新生效；gate_logs 有 `pattern.update`，before/after 完整 | ✅ PASS |
| 10 | POST /{id}/toggle | enabled 翻转 | 200；关 → rule_match 不再命中；开 → 恢复命中（热更新，实时查库） | ✅ PASS |
| 11 | DELETE 无 confirm | 403 | 403 | ✅ PASS |
| 12 | DELETE confirm+reason≥20 | 200 + 审计 | 200 `{deleted:true}`，行已删，gate_logs `pattern.delete` 含 before | ✅ PASS |
| 13 | POST /import（3 条）幂等 | {imported,skipped} | 首次 `{imported:3,skipped:0}`；重复导入 `{imported:0,skipped:3}` | ✅ PASS |
| 14 | GET /export | 含刚建词条 | 200，total=64 items 含新建/导入词条（结构为 `{ok,data:{total,items}}`，见 P2-2） | ✅ PASS |
| 15 | 无 key 全部 401（7 端点） | 401 | 7/7 均 401（代码级 TestClient；**9200 线上实例因未加载路由返回 404，见 P1**） | ✅ PASS* |
| 16 | 在线学习：发布案例 → weight+1 | weight 9→10 | 刷单返利 9.0→10.0；gate_logs `pattern.boost`（reason=案例发布回写，before/after 正确） | ✅ PASS |
| 17 | 在线学习封顶 | weight=10 不再上调 | 杀猪盘 10.0 保持 10.0 | ✅ PASS |
| 18 | 在线学习 hit_count 递增 | scan 命中 +1 | scan_text 后 0→1，再次 rule_match 再 +1 | ✅ PASS |
| 19 | 前端契约 7 方法一一对应 | client.js ↔ 后端端点 | speechPatterns(CREATE_LIST/..) 7 方法全覆盖：GET列表/POST/PUT/toggle/DELETE/import/export，参数与端点完全对齐；字段 pattern/regex/weight/category/enabled/hit_count 前后端一致 | ✅ PASS |
| 20 | pytest tests/test_p17_speech_lib.py -q（队C独立） | 全绿 | **21 passed in 3.27s**（日志 var/pytest_p17_teamC.log） | ✅ PASS |
| 21 | 全量 pytest -q | ≥424 | **429 passed in 53.17s**（日志 var/pytest_full_teamC.log） | ✅ PASS |
| 22 | 原 61 条 seed 词条回归 | 不受影响 | 真实库 af.db：61 seed 全在、enabled=61；正常语料 5 条 0 命中；骗术语料（刷单/退款/冒充/杀猪盘）全部命中 | ✅ PASS |

`*` 标注项：功能语义 PASS，但见 P1/P2 备注。

---

## 三、问题清单

### P0（阻断交付）：无

### P1（高，影响线上可用性）—— 新功能未部署生效

- **现象**：`GET/POST /api/v1/speech-patterns*` 对 9200 线上实例一律 **404**（无 key 时也是 404 而非 401）；主控 `/openapi.json` 无任何 speech 路径（共 80 条路径，0 条含 speech）。
- **根因证据链**：
  1. 主控进程 PID 5796（`C:\...\Python312\python.exe -m app.run --host 0.0.0.0 --port 9200`）`CreationDate=2026-09-17 09:09:34`；
  2. `app/api/speech_patterns.py` mtime=**12:25**、`app/main.py` mtime=**12:45**（含 `app.include_router(api_speech_patterns.router, prefix=API_V1)`）、`app/models/__init__.py` mtime=**12:45**（P17 迁移定义）——**全部晚于进程启动时间**；
  3. `app/run.py`：`uvicorn.run(..., reload=False, ...)` —— 无热重载，代码改动不生效；
  4. 真实库 `data/af.db` 的 `speech_patterns` 表列：`[id,pattern,regex,weight,category,source,enabled,hit_history,created_at]` —— **无 `hit_count` 列**，P17 增量迁移（`_MIGRATIONS["speech_patterns"]=[("hit_count",...)]`）从未执行（迁移在 `ensure_schema()` 启动路径中，旧进程未跑）。
- **影响**：线上管理端"话术库"页面能加载（dist 已含新视图）但所有 API 404；在线学习回写（case 发布 → boost）在运行进程中也无效（旧 cases.py 无 `_boost_hit_patterns` 调用点）。
- **处置**：需**重启主控**（start.ps1 或 `python -m app.run`）加载新代码并触发 `ensure_schema()` 迁移补 `hit_count` 列；重启后应对 9200 复跑本报告第二节矩阵。队C 按任务约束未自行重启，请队长协调部署方执行。

### P2（低，规范/表述差异，不影响功能）

| # | 项 | 任务书表述 | 实现实测 | 建议 |
|---|---|---|---|---|
| P2-1 | 重复 pattern | 422 | **409** duplicate_pattern（`speech_patterns.py` 显式 409，P17 测试同断言） | 409 Conflict 语义更正确；建议同步任务书/API 文档，保持验收基准一致 |
| P2-2 | GET /export 结构 | "JSON 数组" | `{ok:true,data:{total,items:[...]}}` 封包 | 前端 `doExport()` 已兼容（取 res.data 导出对象）；建议文档明确 export 返回封包对象而非裸数组 |
| P2-3 | 无 key 401 | 期望 401 | 代码级 401 ✓；线上 404 系 P1 部署缺口所致 | 重启后复验 |

---

## 四、前端契约核对明细

文件：`webui/src/api/client.js`（L290-300）与 `webui/src/views/SpeechPatternsView.vue`、`webui/src/router/index.js`、`webui/src/App.vue`。

| 前端方法 | HTTP | 端点 | 后端实现 | 一致 |
|---|---|---|---|---|
| `speechPatterns(q)` | GET | `/speech-patterns?page&page_size&category&keyword` | `list_patterns`（L105） | ✅ |
| `speechPatternCreate(p)` | POST | `/speech-patterns` | `create_pattern`（L139） | ✅ |
| `speechPatternUpdate(id,p)` | PUT | `/speech-patterns/{id}`（body 含 confirm+reason） | `update_pattern`（L166） | ✅ |
| `speechPatternToggle(id)` | POST | `/speech-patterns/{id}/toggle` | `toggle_pattern`（L203） | ✅ |
| `speechPatternDelete(id,p)` | DELETE | `/speech-patterns/{id}`（body 含 confirm+reason；前端 prompt≥20 字校验与后端一致） | `delete_pattern`（L216） | ✅ |
| `speechPatternImport(p)` | POST | `/speech-patterns/import`（{items} → {imported,skipped}） | `import_patterns`（L239），INSERT OR IGNORE 幂等 | ✅ |
| `speechPatternExport()` | GET | `/speech-patterns/export` | `export_patterns`（L277） | ✅ |

- 字段契约：pattern/regex/weight/category/enabled/hit_count 前后端命名完全一致；视图展示 hit_count、enabled 开关、weight 1-10 标签、6 类 category 下拉（与后端 `ALLOWED_CATEGORIES` 对齐）。
- 路由/导航：`/speech-patterns` 路由已注册，App 导航含"话术库"入口。
- 边界校验：前端 import 前校验 pattern 必填/weight 1-10/category 枚举，与后端 422 语义对齐，错误统一 `{ok:false,error:{code,message}}` 封包。

---

## 五、独立验证证据（日志/脚本）

| 证据 | 路径（远程） | 内容 |
|---|---|---|
| 队C 独立验收脚本 | `var/t3c_independent_verify.py` | 21 项全量 API + 在线学习 + 回归（自写） |
| 队C 独立验收输出 v1/v2 | `var/t3c_verify_output.txt` / `var/t3c_verify_output_v2.txt` | 21/21 PASS（两轮，v2 覆盖最新代码） |
| test_p17 队C 独立运行 | `var/pytest_p17_teamC.log` | 21 passed in 3.27s |
| 全量 pytest | `var/pytest_full_teamC.log` | 429 passed in 53.17s |
| 真实库只读回归 | 见本报告二-22（af.db 61 seed 完好；正常语料 0 命中；骗术全命中） | — |
| 线上 404 / OpenAPI 缺失 | 9200 实测 | P1 证据 |

---

## 六、结论

1. **代码质量**：后端 7 端点、在线学习回写（weight+1 封顶10 + pattern.boost + hit_count）、前端 7 方法契约、P17 测试（21 项）与全量（429 项）全部通过；未发现功能缺陷。
2. **回归安全**：原 61 条 seed 词条与规则引擎行为完全不受影响。
3. **阻塞项（P1）**：新代码未部署到运行实例（进程早于代码修改、reload=False、真实库缺 hit_count 列），线上功能不可用。
4. **交付判定**：**代码层可交付；部署后需由本队（或指定验收方）对 9200 复验后转可交付**。建议重启主控 → 复跑本报告第二节 → 关闭 P1。

（完）