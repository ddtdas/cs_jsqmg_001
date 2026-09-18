# 代码审计验收（验收-1）· 队A 后端

> 时间：2026-09-17 · 审计对象：金丝雀蜜罐（远程 192.168.10.110，根 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`）
> 范围：话术库（speech-patterns）+ 知识库蒸馏（knowledge）新增后端 + 全项目代码健康 + MCP 透传 + 测试完整性
> 方式：只读审计（未改任何代码文件、未重启主控、未提交 git）；在线主控 9200 已重启加载全部新代码
> 审计人：backend-a（队A 后端API工程师）

---

## 〇、结论（TL;DR）

| 级别 | 数量 | 说明 |
|---|---|---|
| **P0（阻断交付）** | 0 | 无 |
| **P1（须修复）** | 0 | 无 |
| **P2（建议优化）** | 3 | ① `app/services/knowledge.py` 存在未引用 import `get_logger`；② 关键词抽取被掩码 IOC 残留数字污染（体验级）；③ MCP 未为新端点补工具（设计未要求，可选增强） |

**代码层可交付判定：✅ 可以交付。** 全量 446 测试通过、0 编译错误、0 SQL 注入风险、0 路由冲突、GateLog 哈希链校验通过（1820 行有效）、在线主控 12 端点抽查封包一致。P2 项均为体验/整洁性优化，不影响功能正确性与数据安全。

---

## 一、新增模块审计（speech-patterns / knowledge 后端）

### 1.1 约定一致性（统一 ok/err、require_admin、confirm+reason、gate_logs）

| 检查项 | 结果 | 证据 |
|---|---|---|
| 统一 `{ok:true,data}/{ok:false,error:{code,message}}` 封包 | ✅ | 12 端点在线抽查/TestClient 抽查全部一致（见 §二.4） |
| 全部端点 `require_admin` | ✅ | GET/POST/PUT/DELETE 无 key 一律 401 `missing_api_key`（测试 + 在线实测） |
| 敏感操作 `confirm=true + reason≥20` | ✅ | 编排：`speech-patterns` PUT/DELETE；`knowledge` harm/verified/to-pattern/delete → 403 `confirm_required` / 422 `reason_too_short`；`disable` 按任务规格仅 confirm |
| `gate_logs` 哈希链审计 | ✅ | `pattern.update/delete/import`、`knowledge.harm_adjust/verified/to-pattern/disable/delete` 均走 `write_gate_log`（链式）；在线 `audit-verify` = **valid, checked=1820** |
| 服务层防御式异常（不影响主流程） | ✅ | 在线学习 `hit_count+1`（speech_engine，try/except 吞异常）；案例回写 boost（cases，try/except）；知识蒸馏回写（cases._distill_case_knowledge，try/except） |

### 1.2 SQL 注入 / 参数化

- 新模块 `execute` 调用全部使用 `?` 占位 + 参数元组绑定；**0 处值拼接**。
- 动态 WHERE/ORDER 构造（`list_patterns`/`search`/`list_knowledge`）只拼接**固定字面片段**（`"subject LIKE ?"` 等），用户输入全部经绑定参数传入——与既有项目约定一致（`cases.py:213` 同款模式，前序代码已如此）。
- 结论：**无注入面**。

### 1.3 迁移幂等

- `knowledge_items` / `knowledge_events` 走 `TABLES` 的 `CREATE TABLE IF NOT EXISTS`（可重入）；无 `_MIGRATIONS` 增量需求（全新表）。
- `speech_patterns.hit_count` 走 `_MIGRATIONS`（`("hit_count","INTEGER NOT NULL DEFAULT 0")`），老库自动补列，重入安全。
- 在线主控库实测：23 张物理表（22 张业务表 + SQLite 内部 `sqlite_sequence`，**无多余/缺失**）；`knowledge_items` 15 列齐全；`speech_patterns.hit_count` 已在位且随命中递增（种子词 hit_count=2）。

### 1.4 死代码 / 整洁性

- 唯一发现：**`app/services/knowledge.py:19` 导入 `get_logger` 未被引用**（P2，纯整洁问题，无运行时影响，删除即可）。
- 其余新模块 import 全部有引用；无重复定义（AST 扫描 88 文件仅见合法嵌套/类内同名）。

---

## 二、全项目代码健康

### 2.1 语法编译

- `app/` `tests/` `mcp/` `scripts/` 全量 `py_compile`：**88 个 .py，0 失败**。

### 2.2 未引用 import / 重复定义（AST 扫描）

- 疑似未引用 import 共 22 处，其中 **21 处为历史既有文件**（含 re-export 模式 `middleware/__init__.py`、类型注解用、测试文件 fixture 噪声、ppb 布局误报）。
- 新模块仅上述 1 处（P2-①）。
- 重复函数名 7 个文件命中，全部为**跨类/跨作用域合法同名**（如多个类的 `__init__`、不同 job 内的 `_run`），无同作用域冲突。

### 2.3 API 路由

- 扁平化枚举 **110 条 API 路由**；`(path, method)` 重复挂载 = **0**。
- `openapi()`：**94 个 paths / 103 个 operations**（新增模块贡献 14 paths / 16 ops：speech-patterns 7、knowledge 9）。
- 新增模块路由逐条核对 OpenAPI 方法齐全，无缺失/冲突。

### 2.4 封包一致性抽查（12 端点，TestClient 隔离库）

| 端点 | 状态 | ok | error.code |
|---|---|---|---|
| GET /api/v1/system/health（公开） | 200 | true | - |
| GET /api/v1/cases（公开） | 200 | true | - |
| GET /api/v1/speech-patterns（auth） | 200 | true | - |
| POST /api/v1/speech-patterns（auth） | 200 | true | - |
| PUT /api/v1/speech-patterns/1 无 confirm | 403 | false | confirm_required |
| GET /api/v1/knowledge（auth） | 200 | true | - |
| POST /api/v1/knowledge/import（auth） | 200 | true | - |
| POST /api/v1/knowledge/1/harm 无 confirm | 403 | false | confirm_required |
| DELETE /api/v1/knowledge/999999 reason 不足 | 422 | false | reason_too_short |
| GET /api/v1/speech-patterns（无 key） | 401 | false | missing_api_key |
| GET /api/v1/config（auth） | 200 | true | - |
| POST /api/v1/scan/text（公开规则路径） | 200 | true | - |

---

## 三、MCP 透传审计

- `mcp/server.py`（24.5KB）：**55 个工具**（`@server.tool()` 逐一封装 REST 调用，纯 httpx 透传，D1 无业务逻辑），与任务描述一致。
- **新端点未在 MCP 侧暴露**：`speech-patterns` / `knowledge` 在 mcp/server.py 中出现 0 次。
- **确认：无需补工具。** 设计文档《话术库适应更新》《知识库蒸馏》均未提及 MCP 工具要求（两文件 grep "MCP" = 0 命中）；新能力是管理端（WebUI/curl/脚本）导向的接口，不属于 MCP 客户端高频操作面。
- 建议（P2-③，可选）：如后续要让 Agent 工作流直接驱动 `/speech-patterns`、`/knowledge`，可各补 1-2 个透传工具（`af_list_speech_patterns` / `af_import_knowledge` 等），成本极低。

---

## 四、测试完整性

- 测试文件：**28 个**（`tests/test_*.py`；26 个历史 + t1 新增 `test_p17_speech_lib.py` + t4 新增 `test_p18_knowledge.py`，符合预期）。
- `def test_*` 静态计数 374；`pytest --collect-only -q`：**446 tests collected**（差异来自参数化展开）。
- 全量 `pytest -q`：**446 passed, 1 warning（Starlette anyio 弃用提示，历史存在），58.18s**，日志 `var/pytest_full_audit.log`。
- 与 t7 任务预期"446+"一致；表数断言（p1/p16 =22）、job 数断言（p2 =9）已同步。

---

## 五、在线主控核验（9200，已加载全部新代码）

| 项 | 结果 |
|---|---|
| /api/v1/system/health | 200 `{"status":"ok","db":true,...}` |
| /api/v1/system/stats | 200（traps 1384 / detections 3121 / cases 1695 / alerts 1485 ...） |
| /api/v1/system/audit-verify | 200 **`{"valid":true,"checked":1820}`**（GateLog 全链完好） |
| /api/v1/speech-patterns?page_size=3 | 200 total=61，`hit_count` 字段在位且递增 |
| /api/v1/knowledge?page_size=3 | 200 total=4，content/IOC 均掩码落库（如 `138****8000`、`622****0202`），明文不落库（D7）✅ |
| /api/v1/config | 200 |
| 调度器 | 9 类 job 全部注册（含 `job_knowledge_decay` cron 03:30） |

---

## 六、P2 明细与建议

1. **P2-① 未引用 import**：`app/services/knowledge.py:19` `from ..utils import ApiError, get_logger` → 移除 `get_logger`（或后续加日志调用）。零功能影响。
2. **P2-② 关键词质量**：在线样本 keywords 出现 `["02","20","00","13","22","2手","38","62"]`——掩码 IOC 残留数字被 2-4 字片段统计拾取。建议在 `_extract_keywords` 前剥离纯数字片段或对 gram 做数字占比过滤。仅影响展示，不影响检索正确性（检索走 LIKE 全文本）。
3. **P2-③ MCP 扩展（可选）**：见 §三，按需补充 af_* 透传工具。

## 七、审计证据清单

- 全量编译：88 py 文件 0 失败（py_compile）
- AST 扫描：重复定义 7 文件（均合法）、疑似未引用 import 22 处（21 历史 + 1 新）
- 路由：110 条 / 0 重复挂载；openapi 94 paths / 103 ops
- 封装抽查：12/12 一致
- MCP：55 工具纯透传，0 新端点引用（设计无需）
- 测试：28 文件 / 446 collected / 446 passed
- 在线：health 200 / audit-verify valid 1820 / 新端点 200 / 9 job

> 结论：**代码层验收通过，可交付**。P2 项不阻断，建议在下一迭代顺手清理。