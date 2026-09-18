# WebUI 交互与详情增强方案（看板/仪表盘/会话历史全可交互 + 平台跳转 + 分类）

## 目标
1. 看板/仪表盘/会话历史/告警等**所有事件条目、图表节点、卡片可点击**查看详情
2. 事件详情统一四要素：**告警原因 + 上下文 + 平台跳转(iframe 内嵌网页版平台) + 详细分析**
3. **平台分类**（知乎/微博/微信/抖音/其他）
4. 系统作为**外部接入插件**：详情内嵌平台页 + 新窗口兜底

## 后端增强
1. `GET /api/v1/detections/{id}/full` 详情聚合端点：
   返回 detection 原文 + 命中话术明细(speech_hits×speech_patterns) + 分级证据链(grade_reason) +
   关联告警(alerts) + 关联账号(accounts) + 关联蜜饵(honey_facts) + 关联案例(cases) +
   关联事件(events) + 平台跳转 URL
2. detections 表新增 `platform` 列（zhihu/weibo/wechat/douyin/other，迁移，从 source 派生）
3. detections 表新增 `target_url` 列（平台问题点 URL，供 iframe 跳转）
4. 平台跳转 URL 生成：`GET /api/v1/detections/{id}/platform-link`

## 前端增强
1. 新增通用详情抽屉组件 `src/components/DetailDrawer.vue`（四块：告警原因/上下文/平台跳转/详细分析）
2. 新增平台分类组件/标签（按 platform 分组）
3. 接入页面：BoardView(图表节点点击/表格行点击/统计卡点击)、DashboardView、
   SessionsView(会话列表点击)、AlertsView(告警点击)、RiskView、SupplyChainView
4. client.js 新增 detectionFull/detectionPlatformLink

## 实施顺序
Step1 后端（full 端点 + platform/target_url 迁移）→ Step2 前端 DetailDrawer → Step3 接入各页面 + 平台分类 → Step4 构建验证
