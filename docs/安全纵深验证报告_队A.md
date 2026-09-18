# 安全纵深验证报告（队A）

- 报告人：队A 安全测试工程师
- 目标：192.168.10.110（免密 SSH），根目录 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`
- 基线：9200 主控运行中（health 200，se_analysis 推理链加载），pytest 317 基线
- 测试方法：仅验证/诊断，未改产品代码；X-API-Key 鉴权；中文请求体 UTF-8 字节 POST
- 测试时间：2026-09-16

---

## 结论总览

| 级别 | 数量 | 说明 |
|---|---|---|
| P0 | 0 | 无可直接利用的高危缺陷 |
| P1 | 1 | evidence freeze 敏感操作缺 confirm+reason 双确认护栏 |
| P2 | 3 | OpenAPI 鉴权元数据空白 / current-key 泄露 key_file 绝对路径 / CSP 引入第三方 CDN |
| 通过项 | 10/10 | SQLi / XSS / 路径穿越 / 鉴权矩阵 / 越权（除 P1）/ 限流 / 封包一致性 / 安全头 / 密钥落库 / Secrets |

**总评**：核心安全纵深（参数化查询、统一错误封包、鉴权全覆盖、敏感操作双确认、密钥仅存 sha256/Fernet、CSP 含内嵌 DSH frame-src）整体达标，无 P0。发现 1 个 P1 护栏遗漏（evidence freeze）与 3 个 P2 纵深/元数据问题，建议修复后复验。

---

## 逐项验证

### 1. SQL 注入 —— 通过（14 探针全安全）

**操作**：对查询参数（`/scan/hits?limit=`）与路径参数（`/supply-chain/results/{name}`、`/accounts/{url_name}`、`/grading/explain/{det_id}`、`/evidence/{pkg_id}/report-template`、`/detections/{id}/full`）注入 `' OR 1=1 --`、`" OR "1"="1`、`1;DROP TABLE detections;--`、`%27`；POST 体（`/scan/text`、`/accounts/check`）注入同类 payload。

**预期**：参数化或 422/404，不报 SQL 错误、不越权、不破坏数据。

**实测**：
- `/scan/hits?limit=` 3 探针 → 422（Pydantic int 校验，未触 SQL）
- `/supply-chain/results/{name}` 3 探针 → 401（鉴权先于参数处理）
- `/accounts/{url_name}` → 200 但注入串被**当作普通字符串**查询（`url_name:"' OR 1=1 --"` 字面入库），`" OR "1"="1` → 404；无 SQL 报错、无越权返回
- `/grading/explain/{det_id}`、`/evidence/{pkg_id}/report-template`、`/detections/{id}/full` → 422（类型校验）
- `/scan/text`、`/accounts/check` POST 注入串 → 200，作为纯文本/字符串处理，`DROP TABLE` 未执行
- 事后 `GET /scan/hits` 正常返回，detections 表完好

**结论**：通过。所有 SQL 均走参数化（`WHERE id=?`），注入只被当作无害字符串。

### 2. XSS Stored/Reflected —— 通过（存储安全，前端转义）

**操作**：POST `/scan/text` 以 UTF-8 字节提交 `<script>alert(1)</script>`（detection_id=3079）与 `<img src=x onerror=alert(1)>`（detection_id=3080）；检查 API 回读与前端渲染。

**实测**：
- API 层：`GET /detections/3079/full` 返回 `content:"<script>alert(1)</script>"` —— JSON 传输层**原样存储/回显**（API 语义正确，非缺陷；XSS 风险取决于渲染端）
- 前端层：`webui/src/` 业务源码**无** `v-html` / `dangerouslySetInnerHTML` / `innerHTML` / `document.write`；DetailDrawer 原文与命中话术均用 `{{ }}` 插值（Vue 默认转义）；`<el-tag>` 展示 `h.pattern` 同属插值
- grep 命中的 innerHTML 全部位于 vendor 产物（echarts/element-plus/vue runtime），非业务调用点

**结论**：通过。存储型 XSS 不成立——API 原样返回属正常 JSON 语义，前端对原文/话术全部走框架转义渲染，无可执行路径。

### 3. 路径穿越 —— 通过（5 探针全 404）

**操作**：`/ui/../etc/passwd`、`/ui/..%2f..%2f..%2f..%2fWindows/win.ini`、`/ui/%2e%2e/%2e%2e/etc/passwd`、`/ui/assets/..%5c..%5c..%5c..%5cWindows%5cwin.ini`、`/api/v1/../system/health`。

**实测**：全部 404，响应为标准封包 `{ok:false,error:{code:"not_found"}}`。源码确认 `candidate = (_WEBUI_DIST / path).resolve()` + `_WEBUI_DIST not in candidate.parents` 目录穿越防护；`/ui` 与 `/api/v1` 前缀完全隔离。

**结论**：通过。SPA fallback 目录穿越防护有效（含 URL 编码绕过尝试）。

### 4. 鉴权矩阵 —— 通过（62 端点对实测，鉴权全覆盖）

**操作**：62 个 method×path 组合逐一无 key 与带 key 实测。要点：system/embedded-dsh、scan/text、accounts/check、collector、account-bridges、auth/local-login、current-key、cases/publish、evidence freeze、config PUT 等。

**实测摘要**（无 key → 有 key）：
- 设计内公开（200/200）：system/health、auth/bootstrap-info、scan/text、scan/hits、traps GET、traps/{id}、zhihu/channels、zhihu/status、accounts GET 系列、grading/*、cases GET、cases/graph、config GET、evidence report-template
- 应保护且已保护（401 → 正常）：system/stats、system/audit-verify、system/embedded-dsh、scan/inbox、collector/*、account-bridges/*（全部含 admin GET）、alerts/*、events/*、evidence build/freeze/export、cases/publish、config PUT、supply-chain/*、zhihu login/verify/import、traps 全部写操作、detections full/platform-link
- 回环专属（200 仅限 127.0.0.1 来源，设计内）：auth/local-login（返回 key）、auth/current-key（返回 key）；外部非回环会被 403 loopback_only（源码确认依据真实 TCP 对端，X-Forwarded-For 不参与判定）
- `auth/reset-key` 无 key 且无 body → 422（body 校验先于逻辑）；带 key → 走 confirm 门（见越权项）

**说明**：OpenAPI `/openapi.json` 的 security 字段全为空（FastAPI `dependencies=[Depends(require_admin)]` 不写入 OpenAPI security），因此 OpenAPI 全部标 OPEN、实际鉴权需实测——列为 P2 元数据缺陷。

**结论**：通过。鉴权实际全覆盖，无"应保护未保护"端点。

### 5. 越权（敏感操作双确认）—— 1 项通过、1 项失败（P1）

| 敏感操作 | 无 confirm | 短 reason | 带 confirm+reason |
|---|---|---|---|
| auth/reset-key | 403 confirm_required ✓ | 422 reason_too_short ✓ | 200（key 轮换成功，gate_log 1809） |
| cases publish | 403 confirm_required ✓ | 422 reason_too_short ✓ | 200（case id=1694） |
| traps/{id}/retire | 403 confirm_required ✓ | 422 reason_too_short ✓ | 200（trap 1343 retired） |
| config PUT | 403 confirm_required ✓ | 422 reason_too_short ✓ | 200（白名单键 gate_log 1812） |
| config PUT（非白名单键） | — | — | 422 config_key_not_allowed（白名单精确匹配 + 敏感键名拒写） |
| **evidence/{pkg_id}/freeze** | **200 直接冻结成功 ❌** | — | 200 already_frozen（409，二次冻结） |

**结论**：**基本通过，1 个 P1 漏洞**——`/evidence/{pkg_id}/freeze` 仅 `require_admin`，**无 confirm+reason 双确认**（`def freeze_evidence(pkg_id, db)` 无请求体校验），带 key 匿名 body 直接冻结成功；与任务书"evidence freeze 无 confirm → 403/422"要求不符。证据包冻结为不可逆哈希固化操作，应纳入敏感操作双确认。

### 6. 限流 —— 通过

**操作**：匿名连续 65 次 POST `/scan/text`；admin（X-API-Key）连续 65 次对照。

**实测**：
- 匿名：第 1–61 次 200，第 62 次起 **429**（合计 200×61 + 429×4）
- admin：65 次全部 200（豁免）✓

**结论**：通过。匿名触发 429，admin 豁免，符合设计。

### 7. 封包一致性 —— 通过（10 端点抽查）

**操作**：10 种错误场景（404 未知路径 / 404 trap / 404 detection / 401 无 key / 401 坏 key / 422 空 body / 422 类型错 / 409 已冻结 / 403 confirm / 422 reason）。

**实测**：所有错误响应均为 `{"ok":false,"error":{"code":"...","message":"..."}}` 统一封包，无裸异常、无堆栈泄露。401 坏 key 返回 `invalid_api_key`（补充验证，首次误传正确 key 曾得 200，重测坏 key 得 401）。

**结论**：通过。

### 8. 安全头 —— 通过（API 与 UI 一致）

**实测**（`/ui/` 与 `/api/v1/system/health` 头部一致）：
- `Content-Security-Policy: default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src ...; connect-src 'self'; object-src 'none'; frame-src 'self' https://www.zhihu.com https://zhuanlan.zhihu.com https://weibo.com https://m.weibo.cn https://www.douyin.com https://*.douyin.com http://127.0.0.1:3092 http://127.0.0.1:3080; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `X-Frame-Options: DENY`
- CSP **frame-src 含 `http://127.0.0.1:3092`（内嵌 DSH）✓**；另含 3080（本地 dsh web，属本机工具）
- 注意：`script-src`/`style-src`/`img-src` 允许 `https://cdn.jsdelivr.net` 第三方 CDN —— 供应链关注点（P2）

**结论**：通过。CSP 完整且包含内嵌 DSH 的 frame-src；XCTO/RFP/XFO 齐备。

### 9. 密钥落库 —— 通过（reset-key 轮换 + sha256 + Fernet 抽查）

**操作**：`auth/reset-key`（confirm+reason）真实轮换；轮换前后用新旧 key 实测；直查 SQLite `data/af.db`。

**实测**：
- key 轮换：旧 `af_admin_5343f5b1...` → 新 `af_admin_e1e5c4cd...`；**旧 key 立即 401，新 key 与 Bearer 均 200** ✓
- `api_keys` 表：42 行历史 bootstrap 行，**全部 `enabled=0`**，仅 1 行 `enabled=1`（新 key）；**key_hash 全部 64 位 sha256**，无明文；新 key 计算 sha256 与 DB 存储值精确匹配 ✓
- `account_bridges.config_enc` 与 `zhihu_sessions.cookie_enc` 均为 Fernet 加密字段（当前未配置为 NULL）；源码确认敏感字段（cookie/token/password/secret）经 **Fernet 双层加密**落库（`data/secret.key` 44 字符，Fernet key 格式）✓
- gate_logs 完整记录 `auth.reset_key`（action/ts/reason/before/after 存 key_sha256 而非明文）✓

**结论**：通过。明文 cookie/token 不落库，key 仅存 sha256（含历史行禁用）。

### 10. Secrets in repo —— 通过

**操作**：检查 `/system/stats`、`/config` 响应是否含 API key / Fernet key 明文。

**实测**：
- `/system/stats`：统计信息，不含任何 key 明文
- `/config`：`llm.api_key_set:false` 布尔态，**不含 api_key 明文**；config PUT 白名单明确拒写敏感键名（api_key/secret/password/token/key）
- `/auth/current-key`（仅回环）：返回 key 明文 — 设计内（设置页展示），但**返回 `key_file` 绝对路径**（含盘符+中文路径名）为 P2
- `/system/embedded-dsh`：返回 `log_path` 绝对路径（含盘符+中文目录），同为 P2 信息暴露点（需 admin）

**结论**：通过（设计内暴露除外，见 P2）。

---

## 缺陷清单

| # | 级别 | 位置 | 现象 | 复现 | 修复建议 |
|---|---|---|---|---|---|
| 1 | **P1** | `app/api/evidence.py` `@router.post("/{pkg_id}/freeze")` | 证据包冻结仅 `require_admin`，**无 confirm+reason 双确认**；任务书要求无 confirm → 403/422 | 带 key POST `/api/v1/evidence/731/freeze` body `{}` → 200 冻结成功（应 403 confirm_required） | 参照 cases/publish、traps/retire、auth/reset-key：引入 `confirm:bool=False` + `reason:str`（≥20 字），未确认 403；成功写 gate_logs（action=`evidence.freeze`，before/after 哈希） |
| 2 | P2 | `app/api/auth.py` `GET /auth/current-key` | 回环接口返回 `key_file` **绝对路径**（`C:\Users\...\金丝雀蜜罐\data\bootstrap_admin_key.txt`），泄露盘符与目录结构 | 本机 GET `/api/v1/auth/current-key` | 仿 `bootstrap-info` 的 P0-D1 修复：仅返回 key 与 source，不再返回绝对路径（提示"见 data 目录配置"即可） |
| 3 | P2 | OpenAPI `/openapi.json` | 62 端点 **security 字段全空**（标 OPEN），与真实鉴权（401）不符 | 打开 openapi.json 任意受保护端点 | FastAPI 中为 `require_admin` 依赖声明 OpenAPI security（如 `security=[{"ApiKeyAuth":[]}]` + 全局/逐路由），使 Swagger/扫描器/客户端生成器反映真实鉴权 |
| 4 | P2 | 安全头 CSP | `script-src`/`style-src`/`img-src` 允许 `https://cdn.jsdelivr.net` 第三方 CDN | 查看任意响应 CSP 头 | 将前端依赖（如 echarts/CDN 引入的库）本地打包，移除第三方 CDN 源，收紧为 `script-src 'self'`（或加 SRI `integrity` + `strict-dynamic`） |
| 5 | P2（备注） | `app/api/system.py` `/system/embedded-dsh` | 返回 `log_path` 绝对路径（含中文根目录），需 admin 才能读，风险低但同属路径泄露 | 带 key GET `/api/v1/system/embedded-dsh` | 同 #2：返回相对路径或仅返回 port/url/token_url |

---

## 对标差异（ScamIntelli 11 层引擎）

> 注：本项目为"金丝雀蜜罐 + 社会工程学分析"主控，非 ScamIntelli 本体；以下为纵深防护层级的对应关系（如任务要求对标其 11 层引擎：输入 → 归一化 → 模式匹配 → 语义 → 证据链 → 分级 → …）：

| ScamIntelli 层级 | 本项目对应 | 验证结论 |
|---|---|---|
| 输入层（注入防护） | Pydantic 校验 + 参数化查询 | ✅ 422/参数化兜底（§1、§7） |
| 会话/鉴权层 | bootstrap key + require_admin 全覆盖 | ✅ 62 端点实测（§4） |
| 敏感操作层（变更确认） | confirm+reason + gate_logs | ⚠️ 5 类中 4 类达标，evidence freeze 遗漏（P1） |
| 输出/渲染层（XSS） | Vue 插值默认转义，无 v-html | ✅（§2） |
| 传输/资源层 | 统一封包 + CSP/XCTO/RFP/XFO + SPA 穿越防护 | ✅（§3、§7、§8） |
| 数据保护层 | key 仅 sha256、cookie/token Fernet 双层加密 | ✅（§9） |
| 审计层 | gate_logs 链式审计（before/after） | ✅（§9） |
| 限流/抗滥用 | RateLimitMiddleware 匿名 429 + admin 豁免 | ✅（§6） |
| 元数据/可观测性 | OpenAPI 鉴权标注缺失 | ⚠️ P2（#3） |
| 供应链层 | CSP 含第三方 CDN | ⚠️ P2（#4） |
| 信息泄露层 | 回环接口返回绝对路径 | ⚠️ P2（#2、#5） |

---

## 更新/修复方案建议

**P1（建议立即修复，单人 0.5 天）**
1. `evidence.py` freeze 端点：加 `FreezeRequest{confirm:bool=False, reason:str}`，未 confirm→403 `confirm_required`，reason<20→422；成功后 `write_gate_log(action="evidence.freeze", before=已冻结前的 pkg 摘要哈希, after=冻结哈希)`；补单测覆盖（断言无 confirm 403、短 reason 422、正常 200）。

**P2（建议随下一迭代修复）**
2. `auth.current-key` 去掉 `key_file` 绝对路径返回（对齐 bootstrap-info P0-D1 修复方式），`embedded-dsh` 同理改相对路径。
3. OpenAPI 增加 security 声明（`securitySchemes.ApiKeyAuth` + 受保护路由 `security=[...]`），保证 Swagger 与自动化工具鉴权提示准确。
4. CSP 移除 `cdn.jsdelivr.net`，前端依赖本地化（vite build 打包），或为 CDN 脚本加 SRI。
5. 复验建议：修复后重跑本报告 §5 越权矩阵 + §9 落库抽查，并跑 pytest 全量确认无回归（基线 317 例）。

**测试副作用说明（如实向队长报告）**
- admin key 已通过真实 `auth/reset-key` 轮换：旧 key 作废（现用 `data/bootstrap_admin_key.txt` 内新值，报告内不再明文展示新 key）
- 测试产生的数据：trap 1343 已 retire、case 1694 已发布、evidence 包 731 已冻结、限流测试产生约 65 条 normal 检测记录、XSS 探测产生 detection 3079/3080（含 `<script>` 字样原文，L1 正常判定，未触发告警）
- 全流程未改产品代码、未重启主控、未碰 dsh/ 内嵌实例与全局 3080、未提交 git

---

*报告双写：远程 `docs/安全纵深验证报告_队A.md` + 本地 `安全验证_队A.md`（本文件）*