# 02 · REST API

> 版本 v1.0.0 ｜ 机器可读文档：`docs/openapi.json`（OpenAPI 3.1，101 paths / 111 operations / 49 schemas，含 /api/v1/cover 反钓鱼伪装 7 paths）
> 交互式：启动主控后访问 `http://127.0.0.1:9200/docs`；标准 JSON 入口 `/openapi.json`。

## 1. 约定

- 前缀：所有端点位于 **`/api/v1`** 之下。
- 统一封包：成功 `{ok:true, data:...}`；失败 `{ok:false, error:{code, message}}`（`app/utils.py`）。
- 错误码：`401 unauthorized`、`403 forbidden`、`404 not_found`、`409 conflict`（trap_retired / trap_disabled / evidence_exists / case_exists / already_frozen 等幂等与状态冲突，R1 修复新增 evidence_exists / case_exists）、`422 validation_error`、`429 rate_limited`、`500 internal_error`。
- 认证：受保护端点需要 admin key——请求头 `X-API-Key: af_admin_xxxxxxxx` 或 `Authorization: Bearer af_admin_xxxxxxxx`。
- 本机端点（P7/R1）：`POST /auth/local-login`、`GET /auth/current-key`、`POST /auth/reset-key`
  仅允许**回环来源**（`127.0.0.1` / `::1` / `localhost`）调用，其余来源一律 `403 loopback_only`
  ——key 明文只在本机浏览器/本机命令行可见，不从网络侧泄出。
- 限流（P2-5）：`POST /scan/text`、`POST /accounts/check`、`POST /traps/{id}/check-hit` 按 IP 令牌桶限流
  （默认 `AF_RATE_LIMIT_PER_MIN=60`，可配 0 关闭）；携带有效 admin key 的请求豁免。

## 2. 端点速查

| 分组 | 端点 | 认证 | 说明 |
|---|---|---|---|
| system | `GET /system/health` | 公开 | 主控/DB/LLM/知乎四灯 |
| system | `GET /system/stats` | 🔒 | 运行统计（真实计数：蜜饵/检测/分级/告警/案例/账号/事件，P2-1） |
| auth | `POST /auth/bootstrap-info` | 公开 | bootstrap key 信息（实现为 POST；无 /auth/login 端点，README 侧同步移除） |
| auth | `POST /auth/local-login` | 🔒本机 | 本机一键登录：仅回环来源，直接返回 admin key（WebUI"一键登录"按钮调用） |
| auth | `GET /auth/current-key` | 🔒本机 | 本机查看当前 admin key 明文（设置页展示用）；非回环 403 |
| auth | `POST /auth/reset-key` | 🔒本机 | 本机重置 admin key：仅回环 + confirm=true + reason≥20 字 + GateLog 审计（action='auth.reset_key'） |
| traps | `GET /traps` | 公开 | 蜜饵列表（状态过滤） |
| traps | `POST /traps` | 🔒 | 创建蜜饵 |
| traps | `POST /traps/generate-draft` | 🔒 | 生成草稿（HITL：只产文案+指纹，不发布） |
| traps | `POST /traps/{id}/deploy` | 🔒 | 用户手动发布后标记 deployed |
| traps | `POST /traps/{id}/disable` | 🔒 | 停用 |
| traps | `POST /traps/{id}/retire` | 🔒 | 退役（敏感操作：confirm+reason 双确认 + GateLog 审计） |
| traps | `POST /traps/{id}/check-hit` | 公开 | 检查踩饵命中（P2-4：POST body `{text}`，可选 `detection_id`；命中即退役 + **联动检测升级 L5**（R2 修复：按显式 detection_id 或 text 反查，escalation 字段 escalated/skipped/failed，失败不影响命中主结果） |
| scan | `POST /scan/text` | 公开 | 单条文本话术判定（返回分级建议） |
| scan | `GET /scan/hits` | 公开 | 历史命中列表 |
| scan | `POST /scan/inbox` | 🔒 | 批量消费 pending 检测（pending→scanned + 规则分 + 五级分级） |
| scan | `GET /scan/inbox` | 🔒 | inbox 扫描结果（含对话原文，读取需 admin，P2-7） |
| accounts | `POST /accounts/check` | 公开 | 账号风险速查（绿黄红） |
| accounts | `GET /accounts/{url_name}` | 公开 | 账号画像 |
| accounts | `GET /accounts/{id}/timeline` | 公开 | 账号时间线 |
| grading | `GET /grading/levels` | 公开 | 五级定义 |
| grading | `GET /grading/explain/{detection_id}` | 公开 | 分级可解释证据链 |
| evidence | `POST /evidence/build` | 🔒 | 构建证据包（同一检测重复建包 → 409 `evidence_exists`，R1 修复） |
| evidence | `GET /evidence/{id}` | 🔒 | 查看证据包（实际 require_admin，P1-C4/C21 修正） |
| evidence | `POST /evidence/{id}/freeze` | 🔒 | 冻结（哈希链锁定；confirm+reason 双确认；二次冻结 409 `already_frozen`） |
| evidence | `GET /evidence/{id}/export` | 🔒 | 导出（json/text） |
| evidence | `GET /evidence/{id}/report-template` | 公开 | 举报模板（96110/平台/辟谣） |
| cases | `GET /cases` | 公开 | 案例库查询（分页/tag 过滤） |
| cases | `POST /cases/publish` | 🔒 | 脱敏校验后入库（敏感操作；同一检测仅一个案例：应用层同载荷幂等返回、异载荷 409 `case_exists`（R1），**DB 层 UNIQUE(det_id) 索引双保险（R3）**） |
| cases | `GET /cases/graph` | 公开 | 骗术图谱聚合数据 |
| zhihu | `GET /zhihu/channels` | 公开 | 四通道健康 |
| zhihu | `POST /zhihu/channels/{id}/login` | 🔒 | cookie 注入（用户手动导出粘贴） |
| zhihu | `POST /zhihu/channels/{id}/verify` | 🔒 | 通道验证 |
| zhihu | `GET /zhihu/status` | 公开 | 通道状态（含全局限速器水位） |
| zhihu | `POST /zhihu/import` | 🔒 | CH-D 手动导入（粘贴文本/JSON → 检测流水线；写入操作需 admin） |
| alerts | `GET /alerts` | 🔒 | 告警列表（级别/未读过滤；L3+） |
| alerts | `POST /alerts/{id}/read` | 🔒 | 标记已读 |
| events | `GET /events` | 🔒 | 事件流（分页/kind 过滤；kind 含 trap_hit / trap_retired / trap_hit_escalated / trap_hit_escalation_pending（R2 补偿未决；R3 起重放成功标记 resolved=true、检测已删标记 terminal=true 终态收敛）/ detection_graded / alert_created / case_ready / data_cleanup 等） |
| events | `GET /events/{id}` | 🔒 | 单事件详情（404 event_not_found） |
| config | `GET /config` | 公开 | 合成配置（阈值/LLM 路由/降级链 + overrides；密钥屏蔽） |
| config | `PUT /config` | 🔒 | 敏感更新：confirm=true + reason≥20 字；写 configs + gate_logs 哈希链审计 |
| system | `GET /system/embedded-dsh` | 🔒 | 内嵌 DSH 实例状态（port/running/url/token_url，读 dsh/cordis.patch.yml） |
| account-bridges | `GET /account-bridges`、`GET /account-bridges/{platform}` | 🔒 | 平台桥接矩阵 / 单平台详情（config_fields + has_config，不含明文） |
| account-bridges | `PUT /account-bridges/{platform}/config` | 🔒 | 保存配置（敏感字段 Fernet 双层加密 + gate_logs 审计） |
| account-bridges | `POST /account-bridges/{platform}/test` | 🔒 | 测试连接（结构校验 + URL/token 连通） |
| account-bridges | `POST /account-bridges/{platform}/ingest` | 🔒 | 私信接入 webhook（→ detections pending + 账号情报 upsert + 事件） |
| account-bridges | `GET /account-bridges/{platform}/agent-import` | 🔒 | 本地 agent 一键导入配置指引 |
| account-bridges | `DELETE /account-bridges/{platform}` | 🔒 | 删除配置（清空加密凭据，不可恢复） |
| collector | `GET /collector/matrix`、`POST /collector/matrix` | 🔒 | 采集矩阵列表 / 新增格子 |
| collector | `PUT /collector/matrix/{id}`、`DELETE /collector/matrix/{id}` | 🔒 | 更新格子（strategy/enabled）/ 删除格子 |
| collector | `POST /collector/matrix/{id}/run`、`POST /collector/matrix/run-all` | 🔒 | 立即执行单格 / 全部格子（真实网络采集） |
| collector | `GET /collector/tech-stack`、`GET /collector/platforms` | 🔒 | 技术栈调研 / 平台白名单 |
| supply-chain | `POST /supply-chain/query` | 🔒 | 反查起链（域名提取 + DNS 解析 + 相似域名族 L6） |
| supply-chain | `GET /supply-chain/results`、`GET /supply-chain/results/{name}` | 🔒 | agent workflow 反查报告列表 / 单份详情 |

🔒 = 需 admin key；🔒本机 = 仅本机回环可调用（127.0.0.1/::1），无需 key。

## 2.1 新功能端点（聊天导入 / 人设实战 / 社工库 / 话术库 / 知识库）

| 模块 | 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|---|
| 聊天导入 | GET | `/chat-import/candidates` | 🔒 | 探测微信/QQ 候选路径（不扫全盘）|
| 聊天导入 | POST | `/chat-import/scan` | 🔒 | 扫描路径识别平台/库/加密状态 |
| 聊天导入 | POST | `/chat-import/import` | 🔒 | 导入聊天记录→脱敏→检测流水线 |
| 聊天导入 | POST | `/chat-import/import-file` | 🔒 | 通用文件（TXT/CSV/JSON）导入 |
| 聊天导入 | DELETE | `/chat-import/imported` | 🔒 | 清空导入（confirm+reason≥20）|
| 人设实战 | POST | `/persona/replies` | 🔒 | 新建应对规则（trigger/reply_type/content）|
| 人设实战 | GET | `/persona/replies` | 🔒 | 应对规则列表 |
| 人设实战 | DELETE | `/persona/replies/{id}` | 🔒 | 删除规则 |
| 人设实战 | POST | `/persona/replies/{id}/toggle` | 🔒 | 启用/停用规则 |
| 人设实战 | POST | `/persona/respond` | 🔒 | 骗子私信→生成应对回复+提取 IOC |
| 人设实战 | GET | `/persona/drills` | 🔒 | 实战演练剧本列表（4 类）|
| 人设实战 | POST | `/persona/drill` | 🔒 | 执行一轮剧本演练 |
| 社工库 | GET | `/soc-lib/status` | 🔒 | HIBP 配置 + 本地泄露索引数 |
| 社工库 | POST | `/soc-lib/query` | 🔒 | 按 email/phone 查泄露库 |
| 社工库 | POST | `/soc-lib/analyze/{det_id}` | 🔒 | 对检测提取 IOC 并主动查库 |
| 话术库 P17 | GET | `/speech-patterns` | 🔒 | 话术词条列表（分页/搜索/启停/命中统计）|
| 话术库 P17 | POST | `/speech-patterns` | 🔒 | 新建话术词条（pattern/weight/category）|
| 话术库 P17 | PUT | `/speech-patterns/{pattern_id}` | 🔒 | 更新词条 |
| 话术库 P17 | POST | `/speech-patterns/{pattern_id}/toggle` | 🔒 | 启用/停用 |
| 话术库 P17 | DELETE | `/speech-patterns/{pattern_id}` | 🔒 | 删除 |
| 话术库 P17 | POST | `/speech-patterns/import` | 🔒 | 批量导入 |
| 话术库 P17 | GET | `/speech-patterns/export` | 🔒 | 批量导出 |
| 知识库 P18 | GET | `/knowledge` | 🔒 | 知识条目列表（加权排序/过滤）|
| 知识库 P18 | GET | `/knowledge/search` | 🔒 | 语义检索 |
| 知识库 P18 | POST | `/knowledge/import` | 🔒 | 批量导入 |
| 知识库 P18 | POST | `/knowledge/import-text` | 🔒 | 纯文本导入蒸馏 |
| 知识库 P18 | POST | `/knowledge/{knowledge_id}/harm` | 🔒 | 标记有害/降权 |
| 知识库 P18 | POST | `/knowledge/{knowledge_id}/verified` | 🔒 | 人工复核通过 |
| 知识库 P18 | POST | `/knowledge/{knowledge_id}/to-pattern` | 🔒 | 转话术词条 |
| 知识库 P18 | POST | `/knowledge/{knowledge_id}/disable` | 🔒 | 停用 |
| 知识库 P18 | DELETE | `/knowledge/{knowledge_id}` | 🔒 | 删除 |

## 2.2 反钓鱼伪装端点（P19：伪装身份 / 接触登记 / 伪装地图）
| 模块 | 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|---|
| 伪装身份 | POST | `/cover/identities` | 🔒 | 创建伪装身份（layer 自动 = parent 层 +1；name 唯一 409 `identity_name_exists`；假信息字段 fake_phone/fake_wechat/fake_qq/fake_email） |
| 伪装身份 | GET | `/cover/identities` | 🔒 | 全部伪装身份（layer,id 升序；含层数/启用态/被接触次数） |
| 伪装身份 | POST | `/cover/identities/{identity_id}/toggle` | 🔒 | 翻转启停（敏感操作：confirm=true + reason≥20 字 + gate_logs 哈希链审计） |
| 伪装身份 | DELETE | `/cover/identities/{identity_id}` | 🔒 | 删除伪装（敏感操作双确认；下层 parent 置空，接触事件保留） |
| 伪装身份 | POST | `/cover/identities/{identity_id}/expose` | 🔒 | 主动暴露：返回假信息脱敏清单（HITL：供回复骗子时"泄露"）；写 `events(kind=cover_expose)` |
| 接触登记 | POST | `/cover/contact` | 🔒 | 登记骗子接触：脱敏落库（明文不落库）+ IOC 提取（掩码）+ 同值二次锁定 `escalated=1`（events `cover_contact`/`cover_locked`）+ 蜜饵/检测关联 |
| 接触记录 | GET | `/cover/contacts` | 🔒 | 接触记录（id 倒序；按 identity_id / contact_type / escalated / limit 过滤） |
| 伪装地图 | GET | `/cover/map` | 🔒 | 伪装嵌套图：identity→layer→parent + 每层被接触数（total_contacts/layers/tree） |
> 全部 cover 端点 require_admin（D6）；toggle/delete 为敏感操作（confirm=true + reason≥20 字，写 gate_logs 审计）。
> 表迁移：`cover_identities` + `contact_events` + 3 索引；`TABLE_NAMES=24`（22→24）；`GET /system/stats` 返回 `cover_identities`/`contact_events` 真实计数。
## 3. 示例

```bash
BASE=http://127.0.0.1:9200/api/v1
KEY=af_admin_xxxxxxxx

# 健康
curl $BASE/system/health

# 话术检测
curl -X POST $BASE/scan/text -H "Content-Type: application/json" \
  -d '{"text":"在吗，我这边有内部渠道带你炒币稳赚"}'

# 蜜饵草稿（HITL）
curl -X POST $BASE/traps/generate-draft -H "Content-Type: application/json" \
  -H "X-API-Key: $KEY" -d '{"target_url":"https://www.zhihu.com/people/xxx"}'

# 案例发布（脱敏前置，敏感操作）
curl -X POST $BASE/cases/publish -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
  -d '{"det_id":1,"confirm":true,"reason":"该案例已完成脱敏，申请入库备查"}'

# 本机一键登录（仅回环；直接拿 admin key，无需读文件）
curl -X POST $BASE/auth/local-login

# 本机重置 admin key（confirm + reason≥20 字；写 gate_logs 审计）
curl -X POST $BASE/auth/reset-key -H "Content-Type: application/json" \
  -d '{"confirm":true,"reason":"本机密钥例行轮换，旧 key 停用并登记审计记录"}'
```

## 4. MCP 对应关系

MCP 提供 55 个 `af_*` 工具，透传主控 101 个 REST 端点中已映射的 54 个业务端点（映射见 [06-MCP集成](06-MCP集成.md) §3 工具清单表）；
其余端点（auth 引导/本机登录、persona 人设、chat-import 聊天导入、soc-lib 社工库、knowledge 知识库、speech-patterns 话术库、cover 反钓鱼伪装、collector/platforms、supply-chain/results/{name}、accounts timeline、traps monitor、audit-verify 等）无对应 MCP 工具，仅可通过 REST 直连或 WebUI 使用。