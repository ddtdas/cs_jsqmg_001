# 变更记录（CHANGELOG）

> 版本规范见《03_封装规范与版本管理.md》（SemVer：MAJOR.MINOR.PATCH）。版本号与 `VERSION`、`app/__init__.py::__version__` 保持一致。

## [1.0.0] - 2026-09-12

### 新增（P9 测试与交付，本版本）
- 全量测试通过：pytest **112 passed / 0 failed**（P0–P8 全覆盖 + P6.5 收尾端点，含 P8 真实主控集成三连），报告 `tests/TEST_REPORT.md`
- OpenAPI 导出：`scripts/export_openapi.py` → `docs/openapi.json`（OpenAPI 3.1，37 paths / 40 operations / 12 schemas，内置结构自检）
- 根 `README.md`：双形态使用说明 + R1–R6 功能主线 + API 摘要 + MCP 接入（真实路径占位替换规则）+ **合规声明**（非官方/只侦查取证/处置权归官方/HITL/数据最小化）
- `docs/` 文档体系：`docs/README.md` 索引 + 00-项目概述 / 01-快速开始 / 02-API / 03-部署 / 04-知乎通道 / 05-WebUI / 06-MCP集成 / 07-测试（8 篇）
- `start.bat`：内置真实路径，补充 WebUI / MCP 启动说明
- `VERSION` = 1.0.0；`app/__init__.py::__version__` 0.1.0 → 1.0.0
- P6.5 收尾端点并入（backend-core 合入，P9 纳入测试与文档）：`/api/v1/events`（分页/kind 过滤/单查）、`/api/v1/config`（GET 合成配置 + PUT confirm+reason 闸控与 gate_logs 审计）、`/api/v1/scan/inbox`（批量消费 pending→scanned）、`/ui/` 静态托管（SPA fallback + 路径穿越防护）

### 变更
- `docs/MCP集成说明.md` 更名并入编号体系：`docs/06-MCP集成.md`
- 文档/测试同步 P6.5：OpenAPI 33→37 paths；全量用例 99→112

### 修复
- `tests/test_p6_5_endpoints.py::test_config_put_invalid_key_rejected` 测试数据笔误：reason 19 字（<20）被 `reason_too_short` 先行拦截，补足为 21 字使其真正覆盖 `invalid_config_key` 分支（实现校验顺序为合理设计，未改动）

---

## [0.1.0] - 2026-09-11（历史基线，由 P0–P8 各阶段累计形成）

- P0 主控骨架：lifespan 引导 / bootstrap key / 统一封包 / pytest 骨架
- P1 数据模型：14 表幂等迁移 + 话术词库种子
- P2 知乎通道：ZhihuBridge 四通道骨架 + CH-D 手动导入 + 令牌桶/退避 + cookie 加密
- P3 话术引擎：规则预筛 → LLM 复核 → judge 置信度；/scan/text；D5 降级链
- P4 蜜饵引擎：草稿生成（指纹+伪装度）→ deployed → monitored → hit → retired；SimHash→embedding 踩饵检测
- P5 分级与账号：L1–L5 五级融合 + 可解释 + L3+ 告警闸控；账号速查
- P6 证据与案例：证据包哈希链/冻结/举报模板；正则+LLM 双层脱敏；案例库/图谱
- P7 WebUI：Vue3 11 页（认证/仪表盘/蜜饵/话术/账号/证据/案例/事件/告警/知乎/运维）+ 5s 轮询实时降级
- P8 MCP Server：mcp/server.py 透传代理（36 工具）+ stdio/HTTP 双传输 + test_mcp_mount.py