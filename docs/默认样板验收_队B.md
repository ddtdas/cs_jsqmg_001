# 默认样板功能检查 验收报告（队B frontend-b）

> 验收对象：金丝雀蜜罐「开箱即用」默认功能（验收-2）
> 验收人：frontend-b（话术库前端界面工程师）
> 时间：2026-09-17
> 环境：远程 192.168.10.110:9200 主控（已重启加载全部新代码，health 200）；admin key 读 data\bootstrap_admin_key.txt
> 方式：API 实测（HTTP/JSON，中文 UTF-8 字节 POST）+ 浏览器实测（6 默认页渲染 + console 采集）
> 约束：只读不改（演示写入除外：导入 3 条知识演示、1 次扫描、1 次蜜饵草稿生成，均为任务指定验收动作）

## 一、默认种子词库 ✓ 通过

| 检查项 | 结果 |
|---|---|
| GET /api/v1/speech-patterns | 200，`total=61`，items=61，enabled=61（种子词库齐全） |
| 6 类分类 | fake_investment 19 / pig_butchering 6 / job_scam 9 / refund_scam 8 / impersonation 8 / other 11 = **61** ✓ |
| 字段完整性 | 含 id/pattern/regex/weight/category/enabled/hit_count（契约字段齐全） |
| 开箱扫描 | POST /scan/text 典型骗术「导师带你稳赚不赔，扫码下载APP投资高回报，保证收益包赔本金，加微信私聊导师带单」→ 200，**grade=L3 / verdict=suspicious / severity=high / rule_score=54** |
| 命中明细 | 稳赚不赔9.0、投资理财高回报7.0（regex「高回报」）、导师带你8.0、包赔9.0、保证收益7.0… 共 7 个话术命中，hit_count 联动 |

**结论：61 条种子词库开箱即用，典型骗术一键命中分级正常（L3）。**

## 二、默认知识库样本 ✓ 通过（空白起步）

| 检查项 | 结果 |
|---|---|
| 导入前 | GET /knowledge → 200，`total=0`（空库，空白起步） |
| 批量导入 | POST /knowledge/import（3 条演示）→ 200，蒸馏入库 |
| 蒸馏加权 | ①新型仿冒APP投资骗局：ktype=新骗术，harm=9.0（8+IOC 手机+1），decay=1.0，**weighted=9.0**；②冒充客服屏幕共享理赔：新骗术，harm=8.0，**weighted=8.0**；③兼职刷单返利陷阱：线索，harm=6.0，**weighted=6.0** —— **weighted_score = harm_score × decay_weight 全部正确** |
| 脱敏/IOC | iocs 掩码落库：phones `138****8000`、bank_cards `622****0202`、qq `6****20`（明文不落库 D7 ✓） |
| 大文本导入 | POST /knowledge/import-text（含卡号/手机号文本）→ 200，imported=1，蒸馏为「新骗术」weighted=9.0 |
| 检索 | GET /knowledge/search?q=刷单 → 200 命中（按加权分排序） |

**结论：知识库空库可导入、蒸馏加权/IOC 脱敏/分类全部正确，空白起步可用。**

## 三、默认蜜饵模板 ✓ 通过

- POST /traps/generate-draft（不填自定义模板，template_id=null）→ **200**
- 走默认模板 `template:t_question_link`，生成草稿：bait_text「打扰了，想请教一个问题，看到你似乎懂这个，方便的话能聊聊吗」、disguise_score=100、指纹 fingerprint、status=draft
- 附 HITL 提示（D3：只生成文案与指纹，绝不自动发布）—— 开箱可用且合规

## 四、默认页面/路由 ✓ 通过

- 7 条路由 curl 全部 **HTTP 200** 且返回 SPA 根：/ui/ /ui/dashboard /ui/board /ui/scan /ui/speech-patterns /ui/knowledge /ui/settings
- 浏览器实测（注入 admin key 后走查，采集 window error/unhandledrejection/fetch 失败）：

| 页面 | 标题 | 渲染 | console 报错 |
|---|---|---|---|
| /ui/dashboard | 仪表盘 | ✓（含最近会话/健康指示） | 0 |
| /ui/board | 看板 | ✓ | 0 |
| /ui/scan | 话术检测 | ✓ | 0 |
| /ui/speech-patterns（新） | 话术库 | ✓ 20 行/页，6 类计数，总 61 | 0 |
| /ui/knowledge（新） | 知识库 | ✓ 4 条演示数据渲染、Top5/统计卡 | 0 |
| /ui/settings | 设置 | ✓ | 0 |

- 全程 **0 console error、0 pageerror、0 fetch 失败**；侧边栏「旧版功能」分组可见新入口「话术库」「知识库」
- 截图：dashboard.png / speech-patterns.png / knowledge.png（E:\dsh3080工作区存放位置\dsh_gzq1\_t8_screens\）

## 五、默认样例数据 ✓ 通过

| 数据源 | 结果 |
|---|---|
| cases | total=1695（含脱敏案例、graph_tags 分类） |
| events | total=16964（检测/告警/蜜饵事件齐全） |
| alerts | L3 告警大量（含 rule_hit 命中明细，unread 状态正常） |
| traps | 200+（含 draft/deployed/retired=hit 等生命周期样例） |
| scan/hits | 有历史检测数据（含本次演示 3130：rule_score 54 / L3） |

## 六、默认配置 ✓ 通过

- GET /config（带 key 与不带 key 均 200）：`degrade.no_key_mode=true`、`grading.l3_min=10`、`alertable_grades=[L3,L4,L5]`、`speech.rule_min_llm=6`、`trap.simhash_threshold=8` —— 默认值合理（规则优先、无 key 降级浏览）
- 无 key 模式：受保护 API（/speech-patterns 等）→ **401 missing_api_key**；公开端点（/system/health、/config）→ 200；UI 受保护路由 → 跳 `/login?redirect=…`（回跳地址保留）—— 主要功能可浏览、写操作有鉴权

## 七、结论与分级

**总评：6 项默认功能全部验收通过，无 P0/P1 阻断项，开箱即用。**

| 项 | 结论 |
|---|---|
| 1 种子词库 | ✅ 可用（61 条/6 类/命中分级 L3） |
| 2 知识库 | ✅ 可用（空库起步、导入蒸馏加权正确） |
| 3 蜜饵模板 | ✅ 可用（默认模板出稿） |
| 4 页面/路由 | ✅ 可用（6 页渲染、零报错） |
| 5 样例数据 | ✅ 充足（cases/events/alerts/traps） |
| 6 默认配置 | ✅ 合理（no_key_mode、l3_min=10） |

**P2 建议（非阻断，按需采纳）：**
1. 知识库设计为空白起步（已验证）；如需演示丰富度，可考虑加少量种子样例（可选）。
2. `llm` 未配置 → L4/L5 分级与 LLM 复核降级为规则封顶（L3），属预期降级；配置 LLM key 后自动启用。
3. 本验收产生的演示数据留库可作样例：知识条 4 条（3 导入 + 1 文本蒸馏）、检测 3130 与告警 1489、蜜饵草稿 1386；如需纯净环境可经管理界面删除。
4. 话术库每类计数/知识库统计由前端逐类请求实现（契约无聚合端点）；后端提供聚合端点可减少请求（可选优化）。

## 附录：关键证据（API 摘要）

- `GET /speech-patterns?page=1&page_size=200` → total=61；cats={fake_investment:19,pig_butchering:6,job_scam:9,refund_scam:8,impersonation:8,other:11}
- `POST /scan/text`（中文骗术文本）→ grade=L3, rule_score=54, hits=[稳赚不赔9.0,投资理财高回报7.0,导师带你8.0,包赔9.0,保证收益7.0,…]
- `GET /knowledge` → 导入前 total=0；导入后 items=[(1,新骗术,9.0,1.0,9.0,{phones:[138****8000]}),(2,新骗术,8.0,1.0,8.0,{qq,bank_cards}),(3,线索,6.0,1.0,6.0,{})]
- `POST /traps/generate-draft {template_id:null}` → 200, generation=template:t_question_link
- `GET /config` → no_key_mode=true, l3_min=10；无 key 访问 /speech-patterns → 401
- 浏览器走查 6 页：console error 0 / pageerror 0 / fetch 失败 0
