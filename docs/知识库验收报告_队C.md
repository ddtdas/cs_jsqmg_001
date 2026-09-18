# 知识库验收报告（队C · 测试与双盲交叉验收）

- **验收对象**：知识库蒸馏（/knowledge 迁移 + KnowledgeDistiller + API + job_knowledge_decay + 案例回写 + test_p18 + 前端 KnowledgeView）
- **验收人**：tester-c（队C，双盲独立复核，不依赖队A/队B自述）
- **验收时间**：2026-09-17 12:55 – 13:10
- **环境**：远程 192.168.10.110（根 `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`）；9200 主控 health 200
- **设计基准**：`PLAN-知识库蒸馏.md`（§三-§十）
- **方法**：只读代码审查 + 隔离数据目录 API 实测（自写 22 项独立验收脚本 + 中文 UTF-8 字节 POST）+ 9200 线上实测 + 真实库只读核查 + 全量 pytest 独立运行；未改产品代码、未重启主控、未提交 git

---

## 一、总体结论

| 维度 | 结论 |
|---|---|
| 后端实现（代码级） | **PASS（独立验收 22/22 全绿）** |
| 单元测试 | test_p18 17 passed / 全量 **446 passed**（≥408+16+16=440 达标） |
| 前端契约 | **PASS**（client.js 9 方法与后端 9 端点一一对应） |
| 案例回写 | **PASS**（publish → ktype=案例复盘 + harm 按 grade 映射） |
| 部署状态 | **PASS**（9200 线上 /knowledge 可用；P17/P18 迁移均已生效） |
| 合规（脱敏） | **PASS**（手机/QQ/卡明文不落库，IOC 全掩码，线上实测确认） |

**交付判定：可交付。**（注：t3 报告的 P1「主控未加载新代码」已于 12:54 由部署方重启主控修复，本次验收基于**已生效的线上实例**复验通过。）

---

## 二、逐项验收矩阵

> 双盲方式：自行编写独立验收脚本 `var/t6c_independent_verify.py`（22 项，隔离数据目录 + TestClient 对磁盘最新代码执行）；与队A `tests/test_p18_knowledge.py`（17 项）相互独立。两套脚本结论一致：**22/22 与 17/17 全 PASS**。

### 后端独立验证（代码级实测）

| # | 验收项 | 期望 | 实测 | 判定 |
|---|---|---|---|---|
| 1 | 无 key 全部 401（9 端点） | 401 | 9/9 均 401（GET 列表/search、POST import/import-text/harm/verified/to-pattern/disable、DELETE） | ✅ PASS |
| 2 | GET /knowledge 结构 | {items,total} 含 weighted_score | 200，`{page,page_size,total,items}`，items 含 weighted_score/harm_score/decay_weight 全字段 | ✅ PASS |
| 3 | POST /import（3 条含手机号） | {imported,distilled} + 脱敏 | 200 `imported=3 distilled=3`；content 无手机/QQ/卡原文，含 `****`；iocs={phones,qq,bank_cards} 全掩码 | ✅ PASS |
| 4 | POST /import-text（大段文本） | 切分蒸馏多条目 | 200 `imported=3`，ktypes={新骗术,平台漏洞,法律} 自动分类正确 | ✅ PASS |
| 5 | POST /{id}/harm 无 confirm | 403 | 403 confirm_required | ✅ PASS |
| 6 | POST /{id}/harm delta | harm 变化 + gate_logs | 200，3.0→5.0（delta=2）；gate_logs(knowledge.harm_adjust) before/after 正确 | ✅ PASS |
| 7 | harm 钳制 ≤10 + delta 越界 | 钳制/422 | delta=10 从 5→15 钳制到 10.0；delta=20 越界 → 422（Pydantic ge=-10,le=10） | ✅ PASS |
| 8 | POST /{id}/verified confirm+reason | verified=1 | 200 `verified=1`；gate_logs(knowledge.verified) 落审计 | ✅ PASS |
| 9 | POST /{id}/to-pattern | speech_patterns 新增 + gate_logs | 200 `inserted=true` pattern_id=62，weight=min(10,harm)=9.0，category 正确；speech_patterns 行 source=knowledge enabled=1；gate_logs(knowledge.to_pattern) 落审计 | ✅ PASS |
| 10 | to-pattern 幂等 | 二次不重复 | 二次 `inserted=false`（pattern+category 冲突跳过） | ✅ PASS |
| 11 | 时间衰减：旧条目（≈90天前） | decay≈0.2 | created_at=2026-06-01 → refresh_decay 后 decay=0.2 | ✅ PASS |
| 12 | 时间衰减：新条目 | decay≈1.0 | 新条目 decay=1.0 | ✅ PASS |
| 13 | weighted_score = harm × decay | 公式正确 | 5.0 × 0.2 = 1.0 ✓ | ✅ PASS |
| 14 | 案例回写：publish → ktype=案例复盘 | 蒸馏条目 | publish 后 knowledge_items 出现 ktype=案例复盘 | ✅ PASS |
| 15 | 案例回写：harm 由 grade 映射 | L5→8 | harm_score=8.0（L5）；source=case；content 含脱敏正文 | ✅ PASS |

### 9200 线上实测（部署已生效后复验）

| # | 验收项 | 实测 | 判定 |
|---|---|---|---|
| 16 | GET /knowledge 线上可用 | 200，真实库 4 条（3 演示 + 1 import） | ✅ PASS |
| 17 | GET /knowledge/search?q= | 200 返回相关条目（刷单 → 演示条目） | ✅ PASS |
| 18 | POST /import 线上（含手机号） | 200 `imported=1`；真实库 content=`嫌疑人电话 139****9000 联系 QQ:***`，iocs={phones:["139****9000"],qq:["8****21"]} —— 明文不落库 | ✅ PASS |
| 19 | POST /verified 线上 | 200 verified=1 | ✅ PASS |
| 20 | POST /harm 线上 | 200 harm 7→8（含 IOC 后 7+1） | ✅ PASS |
| 21 | DELETE 线上（confirm+reason） | 200 `{deleted:true,id:5}`；测试数据已清理（库回 4 条） | ✅ PASS |
| 22 | 迁移生效 | 真实库 knowledge_items/knowledge_events 表已建（15 列齐）；speech_patterns 已补 hit_count 列；gate_logs 链 valid（checked=1820） | ✅ PASS |

### 前端契约核对

文件：`webui/src/api/client.js`（L302-315）、`webui/src/views/KnowledgeView.vue`（928 行）、`router/index.js`、`App.vue`。

| 前端方法 | HTTP | 端点 | 后端实现 | 一致 |
|---|---|---|---|---|
| `knowledgeList(q)` | GET | `/knowledge?page&page_size&ktype&keyword&min_harm&sort` | `list_knowledge` | ✅ |
| `knowledgeSearch(q)` | GET | `/knowledge/search?q=` | `search_knowledge` | ✅ |
| `knowledgeImport(p)` | POST | `/knowledge/import`（{items}） | `import_knowledge` | ✅ |
| `knowledgeImportText(p)` | POST | `/knowledge/import-text`（{text,source}） | `import_knowledge_text` | ✅ |
| `knowledgeHarm(id,p)` | POST | `/knowledge/{id}/harm`（{delta,confirm,reason}） | `adjust_harm` | ✅ |
| `knowledgeVerified(id,p)` | POST | `/knowledge/{id}/verified`（{confirm,reason}） | `set_verified` | ✅ |
| `knowledgeToPattern(id,p)` | POST | `/knowledge/{id}/to-pattern`（{confirm,reason,category}） | `to_pattern` | ✅ |
| `knowledgeDisable(id,p)` | POST | `/knowledge/{id}/disable`（{confirm}） | `disable_knowledge` | ✅ |
| `knowledgeDelete(id,p)` | DELETE | `/knowledge/{id}`（{confirm,reason}） | `delete_knowledge` | ✅ |

- 字段契约：subject/content/ktype/harm_score/decay_weight/weighted_score/verified/enabled 前后端命名一致；前端展示加权公式（weighted_score = harm_score × decay_weight）、衰减档位、高危害 Top5 统计。
- 路由/导航/dist：`/knowledge` 路由已注册、App 导航"知识库"入口已加、dist 已含 `KnowledgeView-CnD2Fidj.js`（9200 可访问）。

---

## 三、问题清单

### P0（阻断交付）：无

### P1（高）：无

> 说明：t3 报告的唯一 P1（9200 运行实例未加载新代码）已由部署方在 12:54 重启主控修复——本次验收直接对线上实例复验（16-22 项）全部通过，**P1 已关闭**。

### P2（低，不影响功能）

| # | 项 | 现象 | 建议 |
|---|---|---|---|
| P2-1 | GET /knowledge sort 参数语义 | 前端传 `sort=harm_score`（SORT_OPTIONS 含"危害分"），后端仅区分 `created` 与默认（默认按 weighted_score DESC）——**选"危害分"排序时实际仍按综合分排序**，选项不生效 | 后端增加 `sort=harm` 分支（`ORDER BY harm_score DESC`）或前端去掉该选项 |
| P2-2 | import 幂等语义 | 设计 §三/§十 称"幂等 upsert"，实现为 subject+ktype 冲突时**整行更新**（content/harm 覆盖），符合 upsert 定义；但重复导入同名不同内容会覆盖旧情报 | 文档明确 upsert 行为；如需防覆盖可加 version/history 表 |
| P2-3 | disable 前端传 reason | 前端 `knowledgeDisable` 传了 reason，后端 `ConfirmOnlyRequest` 仅接收 confirm（多余字段被 Pydantic 忽略，无副作用） | 前后端对齐：disable 不要求 reason 属设计（§七），可删前端冗余字段 |

---

## 四、测试证据（日志/脚本）

| 证据 | 路径（远程） | 内容 |
|---|---|---|
| 队C 独立验收脚本 | `var/t6c_independent_verify.py` | 22 项全量 API + 脱敏 + 衰减 + 案例回写（自写） |
| 队C 独立验收输出 | `var/t6c_verify_output.txt` | 22/22 PASS |
| test_p18 队C 独立运行 | `var/pytest_p18_teamC.log` | 17 passed in 3.22s |
| 全量 pytest | `var/pytest_full_teamC_p18.log` | 446 passed in 58.66s |
| 线上实测 | 9200 /knowledge 系列 | 见二-16~21 |
| 真实库核查 | data/af.db | 迁移生效、脱敏落地、测试数据已清理 |

---

## 五、结论

1. **代码质量**：后端 9 端点、蒸馏流水线（清洗/分类/脱敏/抽取/加权）、时间衰减（job_knowledge_decay 每日 3:30）、案例回写、前端 9 方法契约全部通过；test_p18（17 项）与全量（446 项）全绿。
2. **合规**：D7 明文不落库验证通过（线上实测 content/iocs 全掩码）；敏感操作（调分/复核/转话术/删除）confirm+reason + gate_logs 审计完整；gate_logs 链 valid。
3. **回归安全**：全量 446 通过，无回归；speech-patterns（P17）在真实库已补 hit_count 列，61 条 seed 完好。
4. **交付判定**：**可交付**。P1 已关闭，P2 项（sort=harm 不生效等）不阻塞交付，建议后续迭代处理。

（完）