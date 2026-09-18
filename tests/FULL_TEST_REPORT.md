# 金丝雀蜜罐 — 功能全量测试报告（队1）

- **测试工程师**：队1（全面测试）
- **被测主机**：192.168.10.110（Windows，免密 SSH），主控 9200（health=200，`{"status":"ok","db":true,"llm":"not_configured","zhihu":"idle"}`）
- **项目根**：`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`
- **测试时间**：2026-09-16（本地 UTC+8 时段）
- **方式**：远程 PowerShell（Base64 EncodedCommand）+ 远程 Python 脚本（`var\test_admin_key.txt` 读取 admin key，`X-API-Key` 头），只测不改产品代码；未重启主控；未触碰 dsh/ 内嵌实例与全局 3080
- **测试脚本留档**：远程 `C:\Users\Administrator\team1_smoke.py`、`probe2.py`、`var\pytest_full_run.log`、`var\openapi_dump.json`；结构化结果 `C:\Users\Administrator\team1_smoke_results.json`

---

## 一、pytest 全量

| 项 | 结果 |
|---|---|
| 命令 | `cd /d "C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐" && .venv\Scripts\python.exe -m pytest -q` |
| 结果 | **273 passed, 1 warning in 43.86s**（退出码 0） |
| 基线 | 273 passed —— **与基线一致，PASS** |

---

## 二、API 冒烟逐项结果

> 判定规则：状态码 + 响应体关键字段；响应摘要为实际返回截断。

### 1. system 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /system/health（无 key） | ✅ PASS 200 | `{"ok":true,"data":{"status":"ok","version":"1.0.0","db":true,...}}` |
| GET /system/stats（有 key） | ✅ PASS 200 | traps=1371, detections=3050, alerts=1461, cases=1688, accounts=25, events=15042 |
| GET /system/audit-verify | ✅ PASS 200 | `{"valid":true,"checked":1787}` |

### 2. auth 域 ✅（鉴权缺陷见 §四）
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /auth/bootstrap-info | ✅ PASS 200 | `{"initialized":true,"port":9200,...}` |
| POST /auth/local-login（本机） | ✅ PASS 200 | 返回 key `af_admin_5343...`（注意：无 key 也可调，见 P1-1） |
| GET /auth/current-key | ✅ PASS 200 | `{"key":"af_admin_5343...","source":"file"}`（注意：无 key 也可调，见 P1-1） |
| POST /auth/reset-key（无 confirm 负例） | ✅ PASS 403 | `{"code":"confirm_required","message":"敏感操作必须 confirm=true"}` |

### 3. scan 域 ⚠️（部分通过，见 P2-6 漏检）
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /scan/text 诈骗话术#1（中奖+保证金） | ✅ PASS 200 | `verdict:"suspicious", severity:"high", grade_hint:"L4"`，命中 pattern 保证金/job_scam |
| POST /scan/text 诈骗话术#2（刷单垫付） | ✅ PASS 200 | `verdict:"suspicious", severity:"high"`，命中 刷单/垫付/job_scam |
| POST /scan/text 诈骗话术#3（钓鱼URL+银行卡密码） | ⚠️ **漏检** | `verdict:"normal", rule_score:0.0`（详见 P2-6） |
| POST /scan/text 正常语料 | ✅ PASS 200 | `verdict:"normal", severity:"none", grade:"L1"` |
| GET /scan/hits | ✅ PASS 200 | 命中列表含 id/rule_score/grade 等 |

### 4. grading 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /grading/levels | ✅ PASS 200 | L1~L5 全部返回（L1 min0 / L2 min0.01 / L3 min10 / L4 LLM / L5 闭环） |
| GET /grading/explain/{detection_id} | ✅ PASS 200 | det=3053 → `grade:"L3", score:12.0`，证据链 grade_reason（rule_hit 权重明细） |

### 5. traps 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /traps/generate-draft（zhihu） | ✅ PASS 200 | 生成 trap id=1374，fingerprint=04581ec3-…，disguise_score=100 |
| POST /traps/{id}/deploy | ✅ PASS 200 | status draft→**active**，deployed_at 写入 |
| POST /traps/{id}/check-hit（正向命中，probe2） | ✅ PASS 200 | 用 bait 原文命中：`hit:true, transition:"active→monitored→hit→retired", hit_count:1`，exact/substring/simhash/overlap 四算法全中；再次 check-hit → 409 trap_retired（正确） |
| GET /traps | ✅ PASS 200 | 列表含 trap 1374（active） |

### 6. accounts 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /accounts/check | ✅ PASS 200 | account_id=25, risk_level=green（无信号默认） |
| GET /accounts/{url_name} | ✅ PASS 200 | url_name=team1_test_account → id=25 |
| GET /accounts/{account_id}/timeline（数字 id=25，probe2） | ✅ PASS 200 | `[{"ts":...,"kind":"account_updated"}]`（首次用 url_name 报 422 为路径参数类型要求，非缺陷） |

### 7. evidence 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /evidence/build | ✅ PASS 200 | pkg_id=728, det_ids=[3053]，hash_chain 含 content_sha256 与链式 hash |
| GET /evidence/{pkg_id} | ✅ PASS 200 | 返回 package（hash_chain 完整） |
| POST /evidence/{pkg_id}/freeze | ✅ PASS 200 | `{"intact":true,"frozen":true,"nodes":1,"detail":"OK"}` |
| GET /evidence/{pkg_id}/export | ✅ PASS 200 | fmt=json，内容含 hash_chain |
| GET /evidence/{pkg_id}/report-template | ✅ PASS 200 | kind=96110，生成报案指引文本（含时间/分级/命中模式） |

### 8. cases 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /cases/publish 含手机号 | ✅ PASS 422 | `{"code":"sensitive_data","message":"脱敏未通过，拦截到 ['phone']"}` |
| POST /cases/publish 脱敏后 | ✅ PASS 200 | case id=1691，graph_tags=[job_scam] |
| GET /cases | ✅ PASS 200 | total=1689，分页正常 |
| GET /cases/graph | ✅ PASS 200 | by_tag 聚合（pig_butchering 1607 / fake_investment 69 / job_scam 8…） |

### 9. alerts 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /alerts | ✅ PASS 200 | 列表含 id=1463（L3, webui, payload 含 grade_reason） |
| POST /alerts/{id}/read | ✅ PASS 200 | id=1463 → payload 回显，事件流新增 alert_read |

### 10. zhihu 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /zhihu/channels | ✅ PASS 200 | ch-a(Playwright 未装,degraded) / ch-b(httpx 无 cookie) / ch-c(RSSHub)… |
| GET /zhihu/status | ✅ PASS 200 | rate_limit capacity=20/available=20，sessions 各通道 inactive |

### 11. events 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /events | ✅ PASS 200 | total=15058，items 含 alert_read / case_published / evidence_frozen |
| GET /events/{id} | ✅ PASS 200 | id=15064 → `{"kind":"alert_read","payload":{"alert_id":1463}}` |

### 12. config 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /config | ✅ PASS 200 | llm/degrade/zhihu/speech/trap/grading/overrides 合成配置，密钥屏蔽 |
| PUT /config confirm=false | ✅ PASS 403 | `{"code":"confirm_required"}`（任务口径"无 confirm→403"成立：字段缺失时 422，显式 false 时 403，均拒绝） |
| PUT /config reason<20 字 | ✅ PASS 422 | `{"code":"reason_too_short","message":"reason 至少 20 字，当前 2 字"}` |
| PUT /config confirm+reason≥20 白名单键 | ✅ PASS 200 | key=grading.l3_min value=10.0（原值回写，无行为变更），`audit.gate_log_id=1793` 哈希链审计；白名单外键（llm.*、zhihu.rate_limit 等）→ 422 config_key_not_allowed（安全设计有效） |

### 13. detections 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /detections/{id}/full | ✅ PASS 200 | det=3053 完整内容/rule_score/grade/grade_reason |
| GET /detections/{id}/platform-link | ✅ PASS 200 | `{"platform":"other","url":"","home_fallback":true}` |

### 14. account-bridges 域 ✅（注意副作用 §五）
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /account-bridges | ✅ PASS 200 | 平台清单（zhihu/wechat…），zhihu ai_ready=true |
| GET /account-bridges/zhihu | ✅ PASS 200 | 配置字段 cookie（type=password）、encrypted_fields |
| PUT /account-bridges/zhihu/config | ✅ PASS 200 | status configured，`encrypted_fields:["cookie"]`（Fernet 加密存储） |
| POST /account-bridges/zhihu/ingest | ✅ PASS 200 | 生成 detection_id=3057, status=pending |
| GET /account-bridges/zhihu/agent-import | ✅ PASS 200 | mcp_streamable_http 配置（127.0.0.1:9201/mcp + Bearer） |
| POST /account-bridges/zhihu/test | ✅ PASS 200 | `{"ok":false,"detail":"cookie 结构校验失败（空）"}`（占位 cookie 预期失败，接口工作正常） |
| DELETE /account-bridges/zhihu | ✅ PASS 200 | `{"status":"unconfigured","config_cleared":true}` |

### 15. collector 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| GET /collector/matrix | ✅ PASS 200 | `{"cells":[]}`（初始空矩阵） |
| POST /collector/matrix（合法） | ✅ PASS 200 | cell id=19, platform=zhihu, strategy 默认化（collect=dm / hourly / depth=5） |
| GET /collector/tech-stack | ✅ PASS 200 | Crawlee/Scrapy/… 技术栈清单 |
| GET /collector/platforms | ✅ PASS 200 | `["zhihu","weibo","telegram","wechat","qq","rsshub"]` |

### 16. supply-chain 域 ✅
| 用例 | 结果 | 关键证据 |
|---|---|---|
| POST /supply-chain/query（空 body） | ✅ PASS 422 | `{"code":"seed_required","message":"需要提供域名/URL/关键词"}` |
| POST /supply-chain/query（带 seed） | ✅ PASS 200 | seed=example-scam-probe.com → mode=local_recon，图节点/风险分层返回 |
| GET /supply-chain/results | ✅ PASS 200 | 历史结果（hgrz.pro 样例，51 条证据摘要） |

---

## 三、鉴权负例（无 X-API-Key 应 401）

| 端点 | 期望 | 实际 | 判定 |
|---|---|---|---|
| GET /system/stats | 401 | 401 | ✅ |
| POST /evidence/build | 401 | 401 | ✅ |
| GET /alerts | 401 | 401 | ✅ |
| GET /events | 401 | 401 | ✅ |
| GET /collector/matrix | 401 | 401 | ✅ |
| POST /supply-chain/query | 401 | 401 | ✅ |
| GET /account-bridges | 401 | 401 | ✅ |
| POST /traps | 401 | 401 | ✅ |
| GET /auth/current-key | 401 | **200（返回 admin key）** | ❌ P1-1 |
| GET /traps | 401 | **200（匿名读蜜罐列表）** | ❌ P2-1 |
| GET /cases | 401 | **200（匿名读案例列表）** | ❌ P2-2 |
| GET /config | 401 | **200（匿名读配置）** | ❌ P2-3 |
| GET /zhihu/status | 401 | **200** | ❌ P2-4 |
| GET /grading/levels | 401 | **200** | ❌ P2-5 |
| POST /scan/text | 401 | **200（匿名写检测）** | ❌ P2-7 |
| POST /accounts/check | 401 | **200（匿名写账号）** | ❌ P2-8 |

> 8/16 端点鉴权生效；8 个端点无 key 直接放行，其中 current-key 直接泄露管理员密钥（P1）。

---

## 四、边界 / 负面用例

| 用例 | 期望 | 实际 | 判定 |
|---|---|---|---|
| POST /scan/text 空文本 | 422 | 422（string_too_short） | ✅ |
| POST /scan/text 纯空白 "   " | — | 200 normal（detection_id=75） | ⚠️ P2-9（空白串未被拒，且落入既有记录 id=75） |
| POST /scan/text 非法 source | 422 | 422（须 zhihu/import/manual） | ✅ |
| POST /scan/text 超长文本（12000 字） | 200/截断 | 200；**存储截断为 4000 字**（probe2 实测 stored_len=4000） | ✅（截断行为确认） |
| POST /traps/generate-draft 非法 platform | 422 | **200（任意 platform 接受）** | ❌ P2-10 |
| POST /collector/matrix 非法 platform | 422 | 422（invalid_platform，提示可选平台） | ✅ |
| GET /traps/999999999（未知 id） | 404 | 404（trap_not_found） | ✅ |
| GET /evidence/888888888（未知 id） | 404 | 404（evidence_not_found） | ✅ |
| GET /events/777777777（未知 id） | 404 | 404（event_not_found） | ✅ |
| GET /accounts/不存在 | 404 | 404（account_not_found） | ✅ |
| GET /traps/{非数字} | — | 422（int_parsing，FastAPI 标准行为） | ✅（符合预期） |

---

## 五、发现问题清单

### P1（高，建议尽快修复）
- **P1-1 未鉴权泄露管理员密钥**：`GET /auth/current-key` 与 `POST /auth/local-login` **无任何 API Key 即返回 200 并回显 admin key `af_admin_5343f5b12a6609b779001f61733c9e10`**；且 9200 监听 **0.0.0.0**（netstat 实测）。任何可达该端口的主机无需凭据即可窃取管理员密钥 → 可完全接管（调用所有 admin 写接口）。若主机暴露到不可信网络，此问题应视为 P0。

### P2（中，建议修复/确认设计意图）
- **P2-1 匿名读蜜罐列表**：`GET /traps` 无 key 200，诱饵文本/状态可匿名读取。
- **P2-2 匿名读案例列表**：`GET /cases` 无 key 200，可匿名查看脱敏后案例（含受害者信息脱敏文本、graph_tags）。
- **P2-3 匿名读配置**：`GET /config` 无 key 200（虽已屏蔽密钥明文，但泄露阈值与降级策略）。
- **P2-4 匿名读知乎状态**：`GET /zhihu/status` 无 key 200。
- **P2-5 匿名读分级定义**：`GET /grading/levels` 无 key 200。
- **P2-6 钓鱼话术漏检**：话术"请登录钓鱼链接 www.fake-bank-verify.com 输入银行卡号和密码完成验证" → `verdict:"normal"`（rule_score=0）。规则库对"钓鱼URL+银行卡+密码"组合无命中（同批 保证金/刷单垫付 均命中且 severity=high）。→ 规则覆盖缺口，建议补充 URL 钓鱼/银行卡密组合模式。
- **P2-7 匿名写检测**：`POST /scan/text` 无 key 200 且创建检测记录（detection_id 递增，可被刷库/投毒）。
- **P2-8 匿名写账号**：`POST /accounts/check` 无 key 200 且创建账号记录（可匿名灌入账号库）。
- **P2-9 纯空白文本未拦截**：`scan/text` 对 `"   "` 返回 200 并写库（空文本已 422，空白串未走同一校验）。
- **P2-10 traps 生成接口不校验 platform 枚举**：`POST /traps/generate-draft` 传任意 platform 字符串均 200（collector.matrix 同场景正确 422），建议与 collector 一致校验。

### 观察项（非缺陷，记录备查）
- LLM 未配置（`llm:"not_configured"`），判级为 rule-only，`grades` 无 L5、L4 仅 3 条；`llm_verdict` 恒 null。建议配置 LLM 后复测 L4/L5 分级与证据链。
- `PUT /config` 无 confirm 字段 → 422（字段缺失，Pydantic），显式 false → 403；任务口径"无 confirm→403"仅在 confirm=false 时成立，缺失时为 422 —— 均正确拒绝，无安全问题。
- 未知 id 传非数字 → 422（int_parsing）而非 404，属 FastAPI 标准路径参数行为，符合预期。
- 测试期间 9200 主控持续稳定，health 全程 200，未发生崩溃/重启。

---

## 六、测试副作用（已产生测试数据，需队长知悉）

1. **zhihu 桥接配置被覆盖并删除**：测试前 zhihu 桥接为 `configured`（原含 cookie，Fernet 加密存储）；按任务要求执行了 PUT config（占位 cookie）→ DELETE，当前状态 `unconfigured`。**原 cookie 配置已被清除，如需恢复请由产品侧重新注入。**
2. 新增测试数据：检测记录 ~3053-3058（含匿名写入 3051/3058/75）、trap 1372-1375（1374 已命中退役、1375 draft、1373 draft 非法 platform 测试产生）、evidence 727/728（已冻结）、case 1691、账号 25/26（team1_test_account / anon_probe_acct）、collector cell 19、bridge ingest 检测 3050/3057（pending）。
3. `var\test_admin_key.txt` 为测试用 admin key 副本（已含于项目 var/ 下，安全起见可删除）。

---

## 七、结论汇总

| 维度 | 结果 |
|---|---|
| pytest 全量 | **273 passed / 273**（= 基线，无回归） |
| API 冒烟（16 功能域） | 全部域可达且主链路可用；逐项见 §二 |
| 检查总数 | 83 项（冒烟 + 鉴权负例 + 边界负面；follow-up 探针证据已并入对应功能项：timeline 数字 id 200 / check-hit 正向命中全生命周期 / 超长文本截断 4000 字） |
| PASS | 74 |
| FAIL（真实缺陷） | 9（P1×1、P2×8） |
| 附加标注缺陷（check 记 PASS 但另立缺陷） | 2（P2-6 钓鱼话术漏检、P2-9 纯空白文本未拦截） |
| 缺陷清单 | P1: 1 项；P2: 10 项 |
| 主控稳定性 | 全程 200，未重启，未触碰 dsh/ 与 3080 |

> 队长交接要点：**P1-1（无鉴权泄露 admin key，9200 监听 0.0.0.0）建议优先修复**；P2 鉴权策略不一致（8 个端点无 key 放行）需确认是否 no_key_mode 降级设计的一部分；zhihu 桥接原配置已被测试清除，需要恢复。
