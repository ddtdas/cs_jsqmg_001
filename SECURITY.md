# 安全策略（Security Policy）

## 支持范围

以下版本/分支接受安全修复：

| 版本 | 支持 |
|---|---|
| 1.0.x（main 分支） | ✅ 积极维护 |
| < 1.0.0 | ❌ 不再支持 |

## 报告漏洞（Reporting a Vulnerability）

请**不要**在公开 Issue 中提交含敏感信息的安全漏洞细节。优先方式：

1. **私有报告**：向维护者发送邮件（见仓库 README 联系方式；若尚未公开邮箱，
   请使用 GitHub 的 [Security Advisory / Private vulnerability reporting](https://github.com/advisories/new)
   功能提交私有漏洞报告）。
2. **GitHub Issue**（仅限不含 PII/凭据的概述）：标题加 `[SECURITY]` 前缀，
   描述影响面与复现条件，但**不得**包含真实 cookie、admin key、脱敏前数据。

请在报告中说明：受影响版本、漏洞类型、复现步骤、影响评估、以及
（可选）建议的修复方式。我们承诺在 72 小时内回复确认，并在确认后尽快
发布修复与安全公告。

## 安全边界（本项目明示）

- **本工具只做侦查取证与分级提醒，不处置执法**：不冻结/拦截/点名/冒充官方。
- **蜜饵只产草稿、举报只出模板（HITL 红线）**：任何对外动作必须由用户手动完成。
- **数据最小化**：cookie 以 Fernet 加密存储；admin key 与 LLM key 不进代码库；
  `data/`、`.env` 一律被 `.gitignore` 排除；案例库入库前强制脱敏。
- **本地优先**：主控默认绑定 `127.0.0.1`；如需对外暴露，请务必：
  - 为所有写端点保留 admin 认证（`require_admin`），
  - 配置 `AF_RATE_LIMIT_PER_MIN` 全局限流，
  - 将 `AF_ZHIHU_KEY_FILE` 指向数据目录之外的路径并收紧文件权限
    （Windows ACL / `chmod 600`，见 `docs/03-部署.md`），
  - 关闭或限制 `/docs`、`/openapi.json` 的暴露面（如经反代白名单）。

## 已知缓解措施（纵深防御）

- LLM 输入一律 `<untrusted_data>` fence + schema 校验/钳制（D4）。
- 敏感写操作 require_admin + confirm+reason 双确认 + gate_logs 哈希链审计（D6）。
- 全局请求级限流（按 IP 令牌桶）与知乎通道全局限速/退避（P2-5 / P1-4）。
- LLM 熔断与日预算护栏（进程级状态，P1-3）。
