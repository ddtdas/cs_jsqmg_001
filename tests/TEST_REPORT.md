# 测试报告（TEST_REPORT）—— 金丝雀蜜罐 CanaryGuard AntiFraud v1.0.0

> 阶段：P9 测试与交付 ｜ 执行人：qa-tester ｜ 执行日期：2026-09-12
> 位置：`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`

---

## 1. 结论摘要

| 项 | 结果 |
|---|---|
| **pytest 全量** | ✅ **112 passed，0 failed，0 error，0 skip**（13.2s，含 P8 真实主控集成三连） |
| **openapi.json 导出** | ✅ `docs/openapi.json`（OpenAPI 3.1，37 paths / 40 operations / 12 schemas，结构自检全 OK） |
| **真实主控冒烟** | ✅ health / docs / /ui/ / openapi.json / 认证闸控（401）全部实测通过 |
| **遗留失败** | 无（期间修复 1 个测试数据笔误，见 §7） |

---

## 2. 运行环境

| 项 | 值 |
|---|---|
| OS | Windows（项目 venv 内运行） |
| Python | 3.12.10（`.venv\Scripts\python.exe`） |
| pytest | 9.1.1（pytest-asyncio 1.4.0，anyio 4.15.1） |
| FastAPI | 0.141.1 ｜ uvicorn（[standard]）｜ Pydantic 2.13.5 ｜ httpx 0.28.1 ｜ mcp 2.2.0 |
| 运行命令 | `.venv\Scripts\python.exe -m pytest -v`（项目根，pytest.ini 配置 testpaths=tests） |

> 依赖按 `requirements.txt` 安装（fastapi/uvicorn/pydantic-settings/APScheduler/httpx/mcp/jsonschema/cryptography/pytest/pytest-asyncio）。

---

## 3. 用例分布与结果（按阶段）

| 阶段 | 测试文件 | 用例数 | 结果 | 覆盖要点（DoD 对应） |
|---|---|---|---|---|
| P0 骨架 | `tests/test_p0_skeleton.py` | 8 | ✅ 全过 | health 200 / bootstrap key 生成与复用 / stats 认证（缺失/错误/合法/Bearer） |
| P1 模型 | `tests/test_p1_models.py` | 7 | ✅ 全过 | 14 表建齐 / 话术词库种子 ≥20 / 分类合法 / 权重范围 / schema 幂等迁移 / 保数据 / api_keys 双角色 |
| P2 知乎通道 | `tests/test_p2_zhihu.py` | 23 | ✅ 全过 | 手动导入（纯文本/JSON 拆分/去重/空拒绝）/ 令牌桶限速与补充 / 指数退避 / cookie 加密存取 / CH-A 探活降级 / 四通道 health / login 认证与校验 / API 导入 / 限速状态 |
| P3 话术引擎 | `tests/test_p3_speech.py` | 11 | ✅ 全过 | 10 条已知话术 100% 命中 / 正常语料 0 误报 / LLM 复核升级 / 无 key 与失败降级纯规则 / schema 钳制与枚举白名单 / 预算熔断 / /scan/text API |
| P4 蜜饵引擎 | `tests/test_p4_traps.py` | 8 | ✅ 全过 | 状态机全链（draft→active→monitored→hit→retired）/ 改写 5-10 字仍召回 / 无关文本不命中 / 伪装度可解释 / 非法迁移拒绝 / retired 后禁查 / 草稿 HITL 不发布 / traps API 全流程 |
| P5 分级账号 | `tests/test_p5_grading.py` | 20 | ✅ 全过 | L1–L5 五级融合与边界 / 可解释证据链 / L3+ 告警闸控 / 去重已读 / 账号绿黄红 / 私有信号白名单丢弃 / 共现团伙提示 / timeline / API 集成 |
| P6 证据案例 | `tests/test_p6_evidence.py` | 14 | ✅ 全过 | 证据包哈希链 / 冻结后篡改即败 / 导出 JSON/TXT / 三类举报模板合规（96110/平台/辟谣）/ 正则脱敏全类型 / LLM 降级 / 敏感扫描 / 案例发布拒绝敏感 / 脱敏后发布 OK / 图谱聚合 / API 集成 |
| P6.5 收尾端点 | `tests/test_p6_5_endpoints.py` | 13 | ✅ 全过 | `/events` 分页/kind 过滤/单查/404 ｜ `/config` GET 合成结构（密钥屏蔽）+ PUT 双确认闸控（403 confirm_required / 422 reason_too_short / invalid_config_key / invalid_config_value）+ gate_logs 哈希链审计 + 事件落库 ｜ `/scan/inbox` 批量消费 pending→scanned + 规则分/分级 ｜ `/ui/` 静态托管 + SPA fallback + 路径穿越防护 + API 封包不受影响 |
| P8 MCP | `tests/test_mcp_mount.py` | 8 | ✅ 全过 | 36 工具全挂载（af_ 前缀/唯一/description/schema）/ mock 三连透传 / 参数透传 / 错误封包透传 / 主控不可达干净报错 / af_register_agent 自描述 / CLI 解析 / **真实主控集成三连（uvicorn 子进程实测 af_health/af_scan_text/af_list_traps）** |
| **合计** | 9 个文件 | **112** | ✅ **112 passed** | — |

> P9 阶段另加的真实运行验证（非 pytest 用例，见 §5 冒烟记录）。

---

## 4. 警告说明

- 1 条 `DeprecationWarning`：`starlette.testclient` 引用 `anyio.abc.BlockingPortal` 旧别名（第三方库内部，非本项目代码），不影响任何用例结果，无需处理。

---

## 5. 真实运行冒烟记录（P9 端到端）

启动真实主控（uvicorn 子进程，独立端口，指向项目 data 目录）：

| 检查项 | 结果 |
|---|---|
| `GET /api/v1/system/health` | ✅ 200：`{ok:true, data:{status:ok, version:1.0.0, db:true, llm:not_configured, zhihu:idle}}` |
| `GET /ui/`（WebUI 静态托管，vite 构建产物） | ✅ 200，页面含应用标题（CanaryGuard） |
| `GET /docs`（Swagger UI） | ✅ 200 |
| `GET /openapi.json`（FastAPI 标准入口） | ✅ 200（28.6 KB，与 docs/openapi.json 同源） |
| bootstrap admin key | ✅ `data/bootstrap_admin_key.txt` 存在（`af_admin_*`，17 字符） |
| `GET /api/v1/system/stats`（带 X-API-Key） | ✅ 200 `{traps:0, detections:0, ...}` |
| `GET /api/v1/system/stats`（不带 key） | ✅ 401 拒绝（D6 认证闸控实测） |

> 注：主方案 §4.2 规划的 `/api/v1/system/openapi.json` 端点后端未落地（404），
> OpenAPI 文档以 FastAPI 标准 `/openapi.json` 与交付文件 `docs/openapi.json` 为准。

---

## 6. 遗留问题与说明

- **无测试失败、无遗留缺陷**。
- LLM 未配置（`.env` 无 AF_LLM_* 实值）时按 D5 降级链运行（health 显示 not_configured），已由 P3 测试覆盖（no_key / failing_provider 降级用例）。
- 规划端点（`/events`、`/config`、`/scan/inbox`）已由 P6.5 收尾阶段补齐并纳入测试；MCP 对应工具（af_list_events / af_get_event / af_config / af_config_update / af_scan_inbox）现为真实可用端点，不再 404。

## 7. 测试期间修复记录（P9）

| # | 问题 | 根因 | 处置 |
|---|---|---|---|
| 1 | `test_p6_5_endpoints.py::test_config_put_invalid_key_rejected` 失败 | 测试数据笔误：reason 仅 19 字（<20），被 `reason_too_short` 先行拦截，未走到 key 格式校验分支 | 测试数据补足为与成功用例一致的 21 字理由，使其真正覆盖 `invalid_config_key` 分支（实现校验顺序 confirm→reason→key 为合理设计，未改动） |

---

*测试证据保留：`scripts/_p9_pytest_run1.txt`（P6.5 合并前的首轮详细输出，99 passed）。*
