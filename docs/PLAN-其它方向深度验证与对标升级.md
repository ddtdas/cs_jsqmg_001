# 其它方向深度验证计划 + 类似项目对标升级方案（v1）

> 目标：从**安全/性能/数据完整性/MCP/WebUI/供应链/采集/内嵌 DSH** 等其它方向
> 拉子代理持续验证，确保覆盖足够广；参考类似项目（ScamIntelli 等）给出
> 修复方案与更新方案；搭建可复用验证工作流。
> 基线：pytest 317 passed；主控 0.0.0.0:9200（health 200）；推理链 se_analysis 已上线。

---

## 一、类似项目对标（参考基准）

### ScamIntelli（★2，蜜罐反诈，11 层混合引擎）—— 主要对标对象
| 层 | ScamIntelli | 金丝雀蜜罐现状 | 差距 → 更新方案 |
|---|---|---|---|
| 1 关键词 | 200+ 词/16 类加权 | 59 种子（6 类）+ 推理链 24 因子 | 扩类（威胁/凭证/数字逮捕→公安/账户/验证码 已部分覆盖）；**扩量至 120+** |
| 2 硬指标正则 | UPI/银行/OTP 即时触发 conf≥0.70 | 手机号/银行卡正则脱敏有；**无"硬指标即时触发+置信度下限"** | 新增 `HARD_INDICATORS`（银行卡/手机/验证码/钓鱼链接 regex → 最低 suspicious + se 证据） |
| 3 ML 集成 | 5 模型软投票（F1=0.978） | 无 ML | **可选路线**：收集标注样本 → 训练物轻量分类器（sklearn），作为 R7 可选层（D5 降级仍纯规则） |
| 4 TF-IDF 相似 | 语料余弦 | 无 | 可选：种子语料向量化，作为 rule_score 补充 |
| 6 紧迫语言 | 时间压力/CAPS/感叹号 | urgency 关键词（已补） | 补**信号强度**：多个紧迫词 → 提高权重 |
| 9 会话累积 | 跨轮聚合持久追踪 | 单条检测独立 | **更新方案**：same from_url_name 多检测聚合 → 会话意图评分（suspicious 累加→L3+） |
| 10 在线学习 | learned_patterns.json 实时更新 | configs 表可热更，无自动学习 | **更新方案**：人工确认 case 后回写词库（confirm+reason 审计） |
| 11 元检测 | 全层加权 | 分级 finalize 融合 | 已对齐（L1-L5 + 证据链） |
| 情报提取 | 13 类 regex | 脱敏 regex（手机/ID）有，**无情报提取层** | **更新方案**：新增 `intel_extract`（URL/邮箱/IP/银行卡/账号，存入 evidence_items） |
| 人设互动 | 3 人设 × 19 类 × 红旗追踪 | 蜜饵模板池 + 伪装度；**无红旗追踪器** | **更新方案**：蜜饵对话场景增加红旗信号计数（12 类行为指标） |
| 图谱 | Neo4j | 供应链反查（sqlite 图） | 已对齐部分；补**跨检测账号共现图** |

### 其它参考
- INVICTUS（LLM 迷途卫兵：prompt 注入/tripwire 检测）→ 金丝雀暂无 prompt 注入防护；**更新方案**：MCP/chat 入口加 fence（已有 <untrusted_data> + schema，可强化）
- Pig-Butchering-Scammer-LLM（杀猪盘数据集）→ 可作样本扩展

## 二、其它方向验证计划（拉子代理执行）

### 验证方向 A：安全纵深（队 A）
- SQL 注入（LIKE/拼接/参数化）、XSS（WebUI 渲染）、路径穿越（/ui SPA fallback）、
  鉴权矩阵（每端点×有无 key×方法×payload）、越权（敏感操作无 confirm）、
  限流（429）、封包一致性（{ok,data}|{ok:false,error}）、CSP/安全头、密钥加密落库抽查
- 产出：安全测试报告 + P0/P1/P2 清单

### 验证方向 B：性能与并发（队 B）
- scan/text 压测（50 并发）、大分页（events 15k/列表 100 页）、慢查询（PRAGMA explain）、
  内存占用、蜜饵 hit 高频、多实例边界（双 9200 端口）、调度 job 并发
- 产出：压测数据 + 瓶颈清单 + 优化建议

### 验证方向 C：数据完整性与异常恢复（队 C）
- gate_logs 哈希链篡改检测、证据包 freeze 后篡改、迁移幂等（重复 ensure_schema）、
  半写事务（grading finalize 3 commit 原子性——P2-10 已知）、主控重启恢复、
  并发写同一 detection、空库首次启动
- 产出：数据完整性报告 + 漏洞清单

### 验证方向 D：MCP 透传 + WebUI 深度交互（队 D）
- MCP 36+38 工具逐一透传真实主控（含参数组合/错误传播/敏感操作 confirm）
- WebUI：全 20 视图逐一（Element Plus 组件交互、表单校验、SSR/SPA fallback、404、
  资源缺失、空态/错误态）
- 产出：MCP/WebUI 索引报告 + 缺陷清单

### 验证方向 E：供应链与采集矩阵深度（队 E）
- supply-chain：种子 URL 反查（IP/域名/相似域名族/证据/时间线）、多节点图、循环防护
- collector：多平台单元格、调度节流、去重、RSSHub 降级
- 产出：功能深度报告 + 边界缺陷

### 验证方向 F：内嵌 DSH 隔离 + 换环境（队 F）
- 内嵌 3092 实例：token 鉴权、空白 home、无全局插件、端口隔离、重启/stop/start 循环、
  换机器（模拟无全局 DSH 环境启动）
- 产出：隔离验证报告

### 验证方向 G：双盲交叉（队 G，独立）
- 与 A-F 结论互通前，从最终行为独立抽查 20 项（每方向 2-3 项），交叉印证

## 三、更新方案（对标 ScamIntelli 落地，待 A-F 报告后实施）
1. **硬指标层**：`HARD_INDICATORS` regex（银行卡/手机号/验证码/钓鱼 URL → 最低 suspicious + 推理链证据 + conf 下限）
2. **情报提取层**：`intel_extract`（URL/邮箱/IP/银行卡/账号 → evidence_items 落库 + DetailDrawer 展示）
3. **会话聚合**：同 from 多检测聚合 → 会话意图评分（多轮 suspicious → 升级 L3/L4）
4. **在线学习**：人工确认案例后回写词库（confirm+reason 审计闭环）
5. **红旗追踪**：蜜饵对话 12 类行为指标计数（可选增强）
6. **词库扩容**：59 → 120+（威胁/凭证/数字逮捕/投资/兼职/退款等 16 类对齐）
7. **Prompt 注入防护强化**：MCP/chat 入口 fence 强化（对齐 INVICTUS 思路）

## 四、可复用验证工作流（搭建）
- 产出 `workflows/canary-full-verify.script`（workflow 脚本模板：A-F 六队并行 +
  修复闭环 + 双盲交叉 + 报告生成，参数化 project_root/admin_key）
- 产出 `docs/验证工作流指南.md`：如何一键跑全量验证、如何加新方向、结果如何汇总

## 五、执行顺序（workflow 编排）
1. 阶段1：A/B/C/D/E/F 六队并行（各自报告 + 缺陷清单）
2. 阶段2：G 双盲抽查 + 汇总（交叉 A-F）
3. 阶段3：更新方案落地（据报告实施硬指标/情报提取/会话聚合/在线学习，补测试）
4. 阶段4：最终回归（pytest 全量）+ 浏览器验收 + 工作流沉淀