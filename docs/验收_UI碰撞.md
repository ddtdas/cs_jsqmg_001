# 验收·UI 碰撞检查（重叠遮挡）—— 金丝雀蜜罐

检查员：ui-collision-checker（UI碰撞检查员）
日期：2026-09-17（第四轮验收）
目标：192.168.10.110 · `C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`（WebUI http://192.168.10.110:9200/ui/）
基线：9200 主控 `/` 200；`/ui/` SPA 200；admin key 正常登录；未改代码、未重启、未造数据（只读）

---

## 一、方法

- **浏览器实测（primary）**：ego 浏览器直连远程 WebUI，登录后对全部路由逐页做 **DOM 几何碰撞检测**：
  - 可见元素两两相交（≥30% 面积）且非父子、非同层 → INTERACTIVE_OVERLAP / TEXT_OVERLAP
  - 元素被另一元素遮挡 ≥85%（z-index/定位更高）→ OCCLUSION
  - `scrollWidth > clientWidth` 文本溢出 → CLIP_H；`docW > vw` → H_OVERFLOW
  - 侧边栏 vs 主内容几何；弹窗几何（视口内/侧边栏间距/字段/页脚按钮）
- **双宽度**：1280×805 与 900×800（`@media (max-width:900px)` 响应式档）。
- **稳定态复核**：对可疑项重载后延时复测，排除渲染时序伪影（loading mask、路由切换、表格高度动画）。
- **源码/CSS 交叉分析**：本地镜像（remote_webui / cover_work/remote_src）+ 远程实测。
- **伪阳性剔除**：`el-table` 固定列内部结构、modal overlay 遮盖（设计行为）、`el-loading-mask`（瞬时）、toast（瞬时）、`col/colgroup`、`el-select` 输入框与占位符同框（EP 内部结构）。

## 二、逐页结论（1280px / 900px）

| 页面 | 1280px | 900px | 说明 |
|---|---|---|---|
| dashboard | ✅ 0 | ⚠️ P2-3 | 900px 下"五级分布"卡标题与副标题重叠 |
| board | ⚠️ P1-2 | ⚠️ P1/P2 | 检测速览"时间"列被固定操作列遮挡（1280 65px+/104px 覆盖，900 命中列 100%）；900px 3 个图表卡标题与副标题重叠 |
| scan | ⚠️ P1-2 | ⚠️ P1 | 最近检测记录"时间"列被固定操作列遮挡（1280 68px 覆盖 + 92px 越界不可滚；900 命中列 91% 被盖） |
| cover | ⚠️ P2-5 | ✅ 0 | 伪装名单"操作"列 280px 超出可视区约 120px（空表，需有数据复验）；新建伪装弹窗/接触记录/图谱 Tab 全部正常 |
| speech-patterns | ✅（仅 select 内部） | ✅ | 无真实碰撞 |
| knowledge | ✅（仅 select 内部） | ✅ | 无真实碰撞 |
| traps | ✅ | ⚠️ P2-6 | 900px 下"创建时间"列 100% 被固定操作列覆盖 |
| evidence | ✅ 0 | ✅ 0 | 干净 |
| cases | ✅ 0 | ⚠️ P2-3 | 900px 下"骗术图谱"卡标题与副标题重叠 |
| settings | ✅ 0 | ✅ 0 | 干净 |
| chat-import | ✅（toast 瞬时） | ✅ | 无真实碰撞 |
| sessions | ⚠️ P3-7 | ✅ 0 | 状态列 el-tag 溢出相邻列 9-14px |
| risk | ✅ 0（稳定态复核） | ✅（稳定态复核） | 首扫疑似重叠，稳定态复核无卡片/表格重叠 |
| collection | ✅ 0（稳定态复核） | ✅（稳定态复核） | 同上 |
| assets / supply-chain / accounts / zhihu / ops-center | ✅ 0 | — | 干净 |
| events / alerts | ✅（仅 select 内部） | ✅ | 干净 |
| accounts-config | ⚠️ P3（表头/单元格裁剪） | ✅ | "Playwright桥(复用zhihu模式)" 198>180px 裁剪；表头 wrapper 溢出（滚动条占位，低危） |
| collector-matrix | ⚠️ P3-8 | ⚠️ P3-8 | repo 标签溢出单元格压到"★ stars"列 |
| console | 🔴 P1-1 | — | 卡片被压缩至 3px，离线告警条压盖卡片头部/主体，地址输入与按钮不可见不可用 |
| login（未登录态） | ⚠️ P2-4 | — | 未登录即显示完整侧边栏导航（含检测 ID 等运营信息） |

## 三、碰撞问题清单

### 🔴 P1（核心工作流被遮挡/不可用）

**P1-1 `/ui/console` 页面布局塌陷 + 告警条遮挡卡片**
- 文件/元素：`webui/src/views/ConsoleView.vue`（`.af-console` flex 列 + `.af-console-frame{flex:1;min-height:620px}`）+ `styles.css`
- 复现：登录后打开 `/ui/console`（内嵌 DSH 3092 未启动态）。
- 证据（实测几何）：`.el-card` 盒高 **3px**（overflow:hidden 裁剪内部），header 73-131 / body 131-171 溢出盒外；离线警告条 `.el-alert` 91-159 **压盖**卡片头部(73-131)与主体(131-159)；地址输入行/「加载」/「在新窗口打开」按钮在裁剪区不可见不可用。
- 影响：命令输入面板核心控件不可用；告警条与卡片文字视觉重叠。
- 建议：`.af-console-frame` 改为 `min-height: calc(100% - 190px)` 或去掉 `height:100%` 硬约束，卡片 `flex-shrink:0`；或在 `.af-console` 外层改为正常文档流 + 视口滚动。

**P1-2 scan / board 表格固定"操作"列遮挡相邻列且无横向滚动**
- 文件/元素：`ScanView.vue`（最近检测记录表）、`BoardView.vue`（检测速览表，`fixed="right"` 操作列）；Element Plus el-table。
- 复现（1280px）：/ui/scan 最近检测记录 —— "时间"列 th/td 左 1151-1311，固定"操作"列 1149-1219，**重叠 68px**，且时间列 1311-1219=92px **越出表格右缘**；bodyWrap `sw==cw==452`（**未进入横向滚动**）。/ui/board 检测速览 —— "时间" 733-893 vs 固定"操作" 687-837，**重叠 104px**、56px 越界，`scrollableX=false`。
- 复现（900px）：/ui/scan "命中"列 91% 被操作列覆盖；/ui/board "命中"列 100% 覆盖；/ui/traps "创建时间"列 100% 覆盖（见 P2-6）。
- 影响：核心研判字段（时间/命中）被操作列遮住且无法横向滚动查看；900px 下更严重。
- 建议：操作列改用紧凑按钮/图标+下拉收起（当前 3 个按钮过宽），或给相邻列 min-width 并开启 `scrollable-x`，或移除 fixed 改用表格内操作列。

### 🟠 P2（视觉重叠/布局不当，不影响主操作）

**P2-3 900px 下卡片标题与"（数据来源…）"副标题重叠**
- 文件/元素：各视图 `.el-card__header`（标题 `<b>` + `.af-muted` 副标题）。
- 复现（900×800）：/ui/dashboard「五级分布」、/ui/board「骗术图谱」「案例分级」「近 7 天检测趋势」、/ui/cases「骗术图谱」共 5 个卡片头。
- 证据（实测几何）：副标题与标题**同左起点**（如 board 骗术图谱 均为 l=197）、副标题换行为 2 行（h37）其首行与标题带（t326-347 vs 330-347）重叠，标题被覆盖 **81-82%**（ovPx 1088-1955）。
- 影响：窄屏下卡片标题文字与副标题互相压字，可读性差。
- 建议：header 用 `flex-wrap:wrap` + 副标题 `min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap`，或副标题独立换行堆叠（margin-top），或 ≤900px 隐藏副标题。

**P2-4 登录页未登录态显示完整侧边栏导航**
- 文件/元素：`App.vue` 主布局 + `LoginView.vue`（登录页仍嵌在带 `aside` 的主布局中）。
- 复现：清 `sessionStorage` 后打开 `/ui/login`。
- 证据：`aside` 宽 220，含 资产管理/看板/会话历史 6/**检测 #3126 · manual 分0**… 等 24+ 项导航（侧边栏动态展示了最近检测条目）。
- 影响：未授权访客可见系统导航结构与检测 ID 等运营信息（蜜罐管理后台的信息暴露面）；登录表单与侧边栏同屏。
- 建议：登录页使用独立布局（不渲染 aside/header），或路由 meta.public 时隐藏侧边栏。

**P2-5 CoverView 伪装名单表"操作"列超出表格可视区**
- 文件/元素：`CoverView.vue` 伪装档案 Tab 表格（列：档案 260 / 假信息 220 / 被接触 90 / 启用 90 / 创建时间 150 / **操作 280**）。
- 证据（1280px）：列宽合计 ≈1090px > 表宽 970px；"操作"列头 l1067-**r1347**（右缘超出表右 1227 约 **120px**）；空表态 bodyWrapper `sw==cw` 未滚动。
- 影响：有数据时最右操作按钮（复制/主动暴露/登记接触/删除）可能被裁切/需横向滚动；当前空表无法完全确认，**建议有数据后复验**。
- 建议：操作列改紧凑布局（图标按钮或 dropdown），或减"假联系信息"列宽。

**P2-6 traps 900px 下"创建时间"列被固定操作列完全覆盖**
- 复现：900×800 `/ui/traps`：`el-table_1_column_6`（创建时间）与固定 `el-table_1_column_7`（操作）重叠 ovPct=100；"命中"列 66% 被盖。1280px 下 traps 正常 → 响应式档固定列挤压。
- 建议：与 P1-2 同源，按 P1-2 修复方案一并处理。

### 🟡 P3（轻微溢出/建议）

**P3-7 sessions 状态列 el-tag 溢出单元格**：`collector_cell_deleted`(129px)/`collector_cell_updated`(134px) 标签 > 单元格 120px，压到相邻列 9-14px（实测 tag l335-r464 vs td l327-r447）。
**P3-8 collector-matrix repo 标签溢出压到"★ stars"列**：`python-telegram-bot/…` 标签宽 273px > 单元格 234px，覆盖 ★ 列 31-87%（1264 与 900 均现）；建议该列放宽或标签 ellipsis。
**P3-9 长文本省略号截断（带 tooltip）**：risk/alerts 等页"话术命中"列（400px）长文本以 `cell el-tooltip` 省略（sw 593-1130 vs cw 400）——EP `show-overflow-tooltip` 设计行为，悬停可见全文；建议抽查 tooltip 正常即可，非缺陷。
**P3-10 accounts-config**："Playwright桥(复用zhihu模式)" 198>180px 单元格裁剪；表头 wrapper 溢出（横向滚动条占位所致，低危）。
**P3-11 el-select 输入框与占位符"重叠"**：EP 内部结构（placeholder 与 input 同框），**非缺陷**，勿误修。

## 四、CoverView（反钓鱼伪装）专项结论

- **三 Tab 全部正常**：伪装档案（空态+提示正常，无碰撞 0 issues）、接触记录（7 行，7 列宽合计 970px 恰满无挤压）、嵌套图谱（图谱卡片正常渲染）。
- **弹窗**：新建伪装账号（620×549）实测——完全在视口内（l322-r942 / t121-b669），距侧边栏 102px，5 个表单字段+10 个输入控件+页脚「取消/创建伪装」齐全，overlay 正常。**未发现弹窗遮挡操作区问题**。
- 主动暴露/登记接触/删除确认 3 个弹窗（width 560/600/520px）由行内按钮触发，当前档案表为空（0 条），**按只读规则未造数据实测**；源码结构为标准 el-dialog（label-position=top + 页脚按钮），与新建弹窗同构，风险低，建议有数据后补一次实测。
- 已知问题：P2-5（操作列宽越界，空表态）；嵌套缩进挤压（按 parent_id 缩进）因无嵌套数据未能实测，建议造 2-3 层档案后复验。

## 五、结论

- **无 P0**；**2 项 P1**（console 布局塌陷+告警遮挡；scan/board 固定列遮挡核心列且不可横滚）、**4 项 P2**（900px 卡片头标题/副标题重叠、登录页泄漏侧边栏、CoverView 操作列越界、traps 900px 固定列全覆盖）、**5 项 P3**（sessions/collector-matrix 标签溢出、长文本省略、accounts-config 裁剪、select 内部结构非缺陷）。
- 说明：risk/collection/cases/board 首扫的"表格行与相邻卡重叠"在**稳定态复核下不存在**（渲染时序伪影，已排除）；dashboard/evidence/cases/settings/supply-chain/accounts/zhihu/ops-center 在 1280px 下完全干净。
- 修复优先级建议：P1-2（表格固定列）影响研判主流程，建议最先处理；P1-1（console）独立页面但完全不可用，一并处理；P2-3/P2-4 为响应式与信息面问题，可随 UI 迭代修。

## 六、截图证据（本地工作区）

- `E:\dsh3080工作区存放位置\dsh_gzq1\_ui_collision_check\shots\01-login-page.png`（登录页带侧边栏）
- `02-console-offline.png`（console 卡片塌陷+告警遮挡）
- `03-dashboard-900.png`、`04-board-900.png`（900px 卡片头标题重叠）
- `05-cover-identities-1264.png`（CoverView 档案树）
