---
name: ui
description: Vue3 + Element Plus UI 优化与侧边栏导航设计指南（金丝雀蜜罐 CanaryGuard 反诈安全工具适配版）。基于 Anthropic 官方 frontend-design skill 本地化，用于 Web 组件/页面/应用的前端设计与实现。
license: Complete terms in LICENSE.txt (Anthropic claude-plugins-official)
---

> 本文件由 Anthropic 官方 frontend-design skill（https://github.com/anthropics/claude-plugins-official/blob/b392f51899343f35a203260a4b344803de236d13/plugins/frontend-design/skills/frontend-design/SKILL.md）下载并本地化适配，适用于「金丝雀蜜罐 CanaryGuard」项目（项目根：`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`，前端位于 `webui/` 目录）。核心设计思想保留原文，后半部分为项目适配约束与 Element Plus 工程化指南。

This skill guides creation of distinctive, production-grade frontend interfaces that avoid generic "AI slop" aesthetics. Implement real working code with exceptional attention to aesthetic details and creative choices.

The user provides frontend requirements: a component, page, application, or interface to build. They may include context about the purpose, audience, or technical constraints.

## Design Thinking

Before coding, understand the context and commit to a BOLD aesthetic direction:
- **Purpose**: What problem does this interface solve? Who uses it?
- **Tone**: Pick an extreme: brutally minimal, maximalist chaos, retro-futuristic, organic/natural, luxury/refined, playful/toy-like, editorial/magazine, brutalist/raw, art deco/geometric, soft/pastel, industrial/utilitarian, etc. There are so many flavors to choose from. Use these for inspiration but design one that is true to the aesthetic direction.
- **Constraints**: Technical requirements (framework, performance, accessibility).
- **Differentiation**: What makes this UNFORGETTABLE? What's the one thing someone will remember?

**CRITICAL**: Choose a clear conceptual direction and execute it with precision. Bold maximalism and refined minimalism both work - the key is intentionality, not intensity.

Then implement working code (HTML/CSS/JS, React, Vue, etc.) that is:
- Production-grade and functional
- Visually striking and memorable
- Cohesive with a clear aesthetic point-of-view
- Meticulously refined in every detail

## Frontend Aesthetics Guidelines

Focus on:
- **Typography**: Choose fonts that are beautiful, unique, and interesting. Avoid generic fonts like Arial and Inter; opt instead for distinctive choices that elevate the frontend's aesthetics; unexpected, characterful font choices. Pair a distinctive display font with a refined body font.
- **Color & Theme**: Commit to a cohesive aesthetic. Use CSS variables for consistency. Dominant colors with sharp accents outperform timid, evenly-distributed palettes.
- **Motion**: Use animations for effects and micro-interactions. Prioritize CSS-only solutions for HTML. Use Motion library for React when available. Focus on high-impact moments: one well-orchestrated page load with staggered reveals (animation-delay) creates more delight than scattered micro-interactions. Use scroll-triggering and hover states that surprise.
- **Spatial Composition**: Unexpected layouts. Asymmetry. Overlap. Diagonal flow. Grid-breaking elements. Generous negative space OR controlled density.
- **Backgrounds & Visual Details**: Create atmosphere and depth rather than defaulting to solid colors. Add contextual effects and textures that match the overall aesthetic. Apply creative forms like gradient meshes, noise textures, geometric patterns, layered transparencies, dramatic shadows, decorative borders, custom cursors, and grain overlays.

NEVER use generic AI-generated aesthetics like overused font families (Inter, Roboto, Arial, system fonts), cliched color schemes (particularly purple gradients on white backgrounds), predictable layouts and component patterns, and cookie-cutter design that lacks context-specific character.

Interpret creatively and make unexpected choices that feel genuinely designed for the context. No design should be the same. Vary between light and dark themes, different fonts, different aesthetics. NEVER converge on common choices (Space Grotesk, for example) across generations.

**IMPORTANT**: Match implementation complexity to the aesthetic vision. Maximalist designs need elaborate code with extensive animations and effects. Minimalist or refined designs need restraint, precision, and careful attention to spacing, typography, and subtle details. Elegance comes from executing the vision well.

---

# 项目适配（金丝雀蜜罐 CanaryGuard）

## 1. 技术栈与构建约束

- **框架**：Vue 3（`<script setup>` 组合式 API）+ Vite 5 + Element Plus 2.9 + ECharts 5 + Pinia 2 + vue-router 4
- **构建**：`npm run build`（在 `webui/` 目录执行，产物输出到 `webui/dist/`）
- **开发**：`npm run dev`（Vite dev server）
- **目录约定**：视图放 `webui/src/views/`，组件放 `webui/src/components/`，状态放 `webui/src/stores/`（Pinia），路由放 `webui/src/router/`
- **语言**：界面文案使用简体中文；注释与命名建议英文

## 2. 后端 API 契约

- 主控 REST API 统一封包：成功 `{ "ok": true, "data": ... }`，失败 `{ "ok": false, "error": "..." }`。前端 axios/fetch 封装必须解包 `ok` 字段，失败时把 `error` 展示给用户并抛错。
- **认证**：请求头 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`；API Key 存于 `sessionStorage`（键名与后端约定一致），请求拦截器统一注入，响应 401/403 时跳转登录或清 key。
- 所有请求统一走封装模块（如 `webui/src/api/`），禁止在组件里裸写 fetch 并散落错误处理。

## 3. 主题基调（CanaryGuard 反诈安全工具）

- 定位：专业、克制的安全运营工具界面，**不花哨、不游戏化**；"BOLD 美学"体现在信息密度、数据可视化和状态色的精准运用上，而非装饰。
- 配色：
  - 中性底色为主（浅色：灰白 `#f5f7fa` 系 / 深色：`#1d2129` 系），统一走 Element Plus 主题变量（`--el-color-primary` 等 CSS 变量）。
  - 警示色用于**风险分级**：红（高危/封禁/告警）、橙（中危/待确认）、黄（低危/提示）、绿（安全/正常）。风险分级色必须全局一致，禁止随意换色。
  - 主色建议沉稳的蓝/青（信任、专业），避免紫色渐变等 clichéd 配色。
- 字体：中文场景下避免纯西文字体依赖；正文可用系统字体栈 + 等宽字体（`ui-monospace`/Consolas）用于 IP、哈希、时间戳等数据。
- 数据可视化（ECharts）：图表配色与风险分级色一致；告警/风险类图表优先用红橙警示色系，普通趋势用中性蓝青；图表要带图例、tooltip、单位。

## 4. 侧边栏导航规划（T2 实现依据）

- 布局：左侧固定侧边栏 + 右侧内容区；**会话历史是侧边栏的一部分**（不要做成独立页面或独立抽屉，除非另有明确要求）。
- 七个导航项（顺序固定）：
  1. 资产管理
  2. 看板
  3. 会话历史
  4. 仪表盘
  5. 风险信息
  6. 信息收集
  7. 供应链
- 路由 path 与菜单一一对应，当前激活项高亮；侧边栏可折叠（保留图标），小屏可收起为抽屉。

## 5. 核心 UI 设计原则（本项目的落地口径）

- **布局**：统一 8px 栅格，区块间距 16/24/32px；内容区最大宽度约 1400px 居中；卡片化分组（Element Plus `el-card`），卡片间距一致。
- **间距**：严格使用 4/8/12/16/24/32 间距阶梯，不出现零散奇数间距；同一层级元素间距一致。
- **色彩**：全部颜色走 Element Plus CSS 变量 + 项目 `:root` 自定义变量（`--cg-*`），禁止魔法色值散落组件。
- **字体**：字号阶梯 12/13/14/16/20/24，正文 14px；数据密集场景 12px+等宽字体；行高 1.5-1.6。
- **可访问性**：文字对比度 ≥ 4.5:1（WCAG AA，正文）；交互元素有 hover/focus 态；纯图标按钮带 `aria-label`/`el-tooltip`；表单错误有明确提示。

## 6. Element Plus 组件最佳实践

- **表单**：统一用 `el-form` + `rules` 校验 + `el-form-item`；提交前 `validate()`；禁用态/加载态（`loading`）齐全；数字/时间输入用对应 `el-input-number`/`el-date-picker` 等专用组件。
- **表格**：`el-table` 统一列宽策略（固定操作列 `fixed="right"`、超长文本 `show-overflow-tooltip`、数值右对齐）；分页用 `el-pagination`（服务端分页参数 page/size 与后端约定一致）；空态给 `empty-text`。
- **弹窗**：`el-dialog` 统一 `width`（默认 600px）、`destroy-on-close` 防脏状态；确认类操作（删除/封禁/高危动作）必须 `ElMessageBox.confirm` 二次确认；反馈统一 `ElMessage`（成功/错误），错误消息展示后端 `error` 字段。
- **导航**：侧边栏用 `el-menu`（`router` 模式，`default-active` 绑定当前路由）；顶部可选 `el-breadcrumb`。
- **状态标签**：风险/状态展示统一 `el-tag`（`type` 按风险分级映射 success/warning/danger/info），附 `effect="light"` 或 `plain`，文字明确不缩写。
- **图标**：使用 `@element-plus/icons-vue`，不引入额外图标库。

## 7. 导航与信息架构

- 七个侧边栏项即一级信息架构；每页聚焦单一职责：列表页 = 筛选区 + 工具栏 + 表格 + 分页；详情/概览页 = 统计卡 + 图表 + 明细表。
- 页面内锚点/页签（`el-tabs`）用于二级组织，避免无限嵌套路由。
- 面包屑 + 当前菜单高亮双重定位，保证用户随时知道身处何处。
- 会话历史在侧边栏中可带会话列表/搜索；点击会话进入该会话详情（路由 `query` 或子路由传 id）。

## 8. 响应式与可访问性

- 断点：≥1200px 全展开侧边栏（可折叠）；768–1200px 默认折叠为图标栏；<768px 侧边栏变抽屉（`el-drawer`），内容区单列。
- 表格在小屏允许横向滚动（`el-table` 默认支持），关键操作不放最后列之外。
- 键盘可达：所有交互元素可用 Tab 聚焦 + Enter 触发；`el-dialog` 聚焦陷阱 Element Plus 内置。
- 颜色不唯一传递信息：风险分级除颜色外同时有文字标签（`el-tag` 文本），图表色盲安全（红/橙/绿尽量用不同明度与形状区分）。
- 减少动效对弱视用户干扰：`prefers-reduced-motion` 时关闭非必要动画。

## 9. 性能要点

- **按需引入 Element Plus**：使用 `unplugin-vue-components` + `unplugin-auto-import` 自动按需加载组件与样式（或在 `main.js` 手动 `app.use(ElementPlus)` 时配合 `element-plus/dist/locale/zh-cn` 与样式按需）；避免全量引入未用组件。
- **路由懒加载**：`() => import('@/views/xxx.vue')`，每个视图独立 chunk，首屏只加载当前页。
- **ECharts 按需引入**：只 `import { use } from 'echarts/core'` 注册用到的图表/组件/渲染器（`CanvasRenderer`、`LineChart`、`PieChart` 等），避免全量 echarts 打进主包。
- **chunk 体积目标**：`npm run build` 后单 chunk 尽量 < 300KB（gzip 前），首屏资源 < 1MB；用 `vite build --report` 或 manualChunks 检查 vendor 拆分。
- 图片/静态资源走 `src/assets` 经 Vite 处理；大列表用虚拟滚动或分页；`v-for` 必须带 `:key`。
- 长列表/高频刷新（如实时告警）用节流/防抖，组件卸载时清理定时器与 ECharts 实例（`dispose`）。
