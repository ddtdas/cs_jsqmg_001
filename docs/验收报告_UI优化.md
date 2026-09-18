# 验收报告 · UI 优化（T2 侧边栏重构 + T3 验收）

- 验收人：qa-tester（验收工程师：构建 / 导航可达 / 数据链路 / 回归）
- 验收时间：2026-09-13
- 项目根：`C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐`（WebUI 在 `webui/`）
- 验收对象：T2 侧边栏 7 导航重构产物（`webui/src` 全部改动）

---

## 1. 构建结果 —— PASS

在 `webui/` 下执行：

```
npm run build
```

| 项目 | 结果 |
|---|---|
| 退出码 | `0`（成功） |
| 模块转换 | ✓ 2183 modules transformed |
| 耗时 | 23.43s |
| 产物 | `webui/dist/index.html`（602 B）+ `dist/assets/` 20 个 chunk |
| 新增 error | 无 |
| 警告 | 仅 chunk 体积警告（vendor-element 1.09MB / vendor-echarts 1.03MB > 900kB）——**预置阈值警告，非本次改动引入，非 error** |

`dist/index.html` 时间戳 2026-09-13 00:53（本次构建），引用 `index-C0NtSX7v.js`，与构建输出一致。

## 2. 主控集成 / SPA fallback —— PASS

本机 127.0.0.1:9200 已有主控进程在运行（PID 9012，`python -m app.run --host 127.0.0.1 --port 9200`，启动于验收前，非本次启动；验收直接复用，未停止他人进程）。我尝试的 `uvicorn app.main:app` 因端口占用自动退出，未造成影响。

`app/main.py` 静态托管逻辑核对：`/ui/` 返回 `webui/dist/index.html`；`/ui/{path}` 真实文件直接返回、含 `.` 的未知资源 404、其余全部 SPA fallback 到 index.html。

实际 HTTP 探测（全部 200）：

| 路径 | 状态 | 内容 |
|---|---|---|
| `/ui/` | 200 | index.html（text/html, 592 B） |
| `/ui/sessions` | 200 | SPA fallback ✓ |
| `/ui/assets` | 200 | SPA fallback ✓ |
| `/ui/board` | 200 | SPA fallback ✓ |
| `/ui/risk` | 200 | SPA fallback ✓ |
| `/ui/collection` | 200 | SPA fallback ✓ |
| `/ui/supply-chain` | 200 | SPA fallback ✓ |
| `/ui/dashboard` | 200 | SPA fallback ✓ |
| `/ui/login`、`/ui/traps`（旧路由抽查） | 200 | SPA fallback ✓ |
| `/ui/assets/index-C0NtSX7v.js`（真实资源） | 200 | 22331 B |
| `/ui/assets/does-not-exist.js`（不存在资源） | 404 | 正确不吞进 SPA |
| `/api/v1/system/health` | 200 | JSON |

## 3. 侧边栏导航核对 —— 7/7 PASS（源码 + 产物 + HTTP 三层核对）

> 说明：验收环境无可用浏览器二进制（ego-linux 运行时不带 Chrome/Edge），按任务约定改用「源码路由表 + client.js + 编译产物 + HTTP」核对，未做浏览器 DOM 实测。

### 3.1 router/index.js 路由表（新旧）

**新增 7 条（T2，`webui/src/router/index.js` L11-17）**：

| 路径 | name | 组件 | 侧边栏标题 |
|---|---|---|---|
| `/assets` | assets | AssetsView.vue | 资产管理 |
| `/board` | board | BoardView.vue | 看板 |
| `/sessions` | sessions | SessionsView.vue | 会话历史 |
| `/dashboard` | dashboard | DashboardView.vue | 仪表盘 |
| `/risk` | risk | RiskView.vue | 风险信息 |
| `/collection` | collection | CollectionView.vue | 信息收集 |
| `/supply-chain` | supply-chain | SupplyChainView.vue | 供应链 |

**保留旧路由（L7-28，均未被删除）**：`/login`、`/traps`、`/scan`、`/accounts`、`/evidence`、`/cases`、`/events`、`/alerts`、`/zhihu`、`/ops-center`（10 条）+ `/` 重定向 → `/dashboard` + 通配兜底 → `/dashboard`。
> 备注：任务描述称「11 个旧路由」，实际路由定义 10 条 + 根重定向 1 条 = 11 项入口，全部保留；登录守卫（`router.beforeEach`，校验 `sessionStorage.af_api_key`，未登录跳 `/login?redirect=`）仍然生效，`/login` 标记 `meta.public`。

### 3.2 侧边栏（App.vue）

- `mainMenus`（L17-25）：7 项按用户字面顺序排列——资产管理 / 看板 / 会话历史 / 仪表盘 / 风险信息 / 信息收集 / 供应链，图标齐全（Box/DataBoard/ChatDotRound/Odometer/WarningFilled/FolderOpened/Share）。
- **会话历史内嵌展开逻辑（L48-111, L184-207）**：`/sessions` 渲染为 `el-sub-menu`（侧边栏一部分）；展开内容为「最近会话列表」——`usePolling`（15s）聚合真实 API 数据：`/scan/hits`（6 条）+ `/events`（6 条）+ `/traps?status=hit`（6 条），按时间倒序取前 8；点击条目 `router.push('/sessions?tab=xxx&id=yyy')` 跳转定位；底部「全部会话历史 →」跳 `/sessions` 页。折叠态退化为普通菜单项。
- **SessionsView 定位联动（L118-149）**：读取 `?tab=hit|event|trap|alert&id=`，切 tab、翻页、`openDetail` 自动打开详情抽屉（含 hit 的分级解释 `/grading/explain/{id}`），异步轮询定位兜底 8s。
- 旧版 9 页收纳进「旧版功能」分组（可折叠），折叠态仍可访问，未被删除。
- 编译产物核对：`dist/assets/index-C0NtSX7v.js`（主 chunk）中 7 个标题全部存在（资产管管理×2、看板×2、会话历史×4、仪表盘×2、风险信息×2、信息收集×2、供应链×2、旧版功能×1）。

## 4. 数据链路核查 —— PASS（12/12 端点实测 200）

### 4.1 端点存在性（client.js ↔ docs/openapi.json）

前端 `webui/src/api/client.js` 全部请求端点与 `docs/openapi.json` 37 条路径逐一比对，**无「调用了不存在端点」的硬编码**。

### 4.2 7 个新页面数据源映射 + 实测

| 新页面 | 依赖端点 | 实测结果 |
|---|---|---|
| 资产管理 AssetsView | `GET /traps`、`GET /events?kind=evidence_built`、`GET /evidence/{pkg_id}` | 200 ok ✓ |
| 看板 BoardView | `GET /cases`、`GET /cases/graph`、`GET /scan/hits`、`GET /alerts`、`GET /system/stats` | 200 ok ✓ |
| 会话历史 SessionsView | `GET /events`、`GET /scan/hits`、`GET /traps`、`GET /alerts`、`GET /grading/explain/{detection_id}` | 200 ok ✓ |
| 仪表盘 DashboardView | `GET /traps`、`/scan/hits`、`/alerts`、`/cases`、`/cases/graph`、`/grading/levels`、`/system/stats` | 200 ok ✓（同源聚合） |
| 风险信息 RiskView | `GET /alerts`、`GET /grading/levels`、`GET /traps`、`GET /scan/hits`、`POST /alerts/{id}/read` | 200 ok ✓ |
| 信息收集 CollectionView | `POST /scan/text`、`POST /zhihu/import`、`POST/GET /scan/inbox`、`GET /scan/hits` | 200 ok ✓ |
| 供应链 SupplyChainView | `POST /accounts/check`、`GET /accounts/{id}/timeline` | 200 ok ✓（account_id=4 实测） |

实测样例（带 bootstrap admin key）：`/api/v1/traps`→ok、`/api/v1/events?kind=evidence_built`→total 14 条、`/api/v1/cases/graph`→by_tag[12,4]、`/api/v1/scan/hits`、`/api/v1/alerts`→33 条、`/api/v1/grading/levels`、`/api/v1/scan/inbox`→pending 队列、`/api/v1/system/stats`、`/api/v1/accounts/check`→account_id=4、`/api/v1/accounts/4/timeline`→200、`/api/v1/grading/explain/66`→200。全部 `{"ok":true}`。

## 5. 回归 —— PASS

```
.venv\Scripts\python.exe -m pytest -q
```

**163 passed, 1 warning in 21.73s（exit 0）**。后端未改动；WebUI 改动不影响后端测试。唯一警告为 starlette 的 anyio 弃用提示（环境依赖，非本仓库代码）。

## 6. 问题清单

| # | 级别 | 问题 | 处置 |
|---|---|---|---|
| 1 | 信息 | 构建有 vendor chunk >900kB 体积警告（element-plus 1.09MB / echarts 1.03MB），非 error、非本次引入 | 不阻塞，可后续 manualChunks 继续拆分 |
| 2 | 信息 | 任务描述「11 个旧路由」与代码实际（10 条旧路由定义 + 根重定向）表述略有出入，功能上全部保留 | 无需修复，记录 |
| 3 | 信息 | 验收环境无浏览器二进制，侧边栏 DOM 展开/点击未做真机实测，已用源码 + 编译产物 + HTTP fallback 三层核对替代 | 如需可后续在带 Chrome 环境补 Playwright 实测 |

## 7. 验收结论

**通过（PASS）**——DoD 全部满足：

- ✅ 构建成功（npm run build exit 0，dist 产物存在）
- ✅ 主控集成正常（/ui/ 200 + 7 个新路由 SPA fallback 200 + 资源 200/404 正确）
- ✅ 7 导航 all pass（路由表 + 侧边栏 + 会话历史内嵌展开逻辑 + 编译产物核对）
- ✅ ≥3 新页面真实数据链路复核通过（实际抽查 7/7 页面、12 端点全部 200 ok）
- ✅ pytest 无回归（163 passed）
- ✅ 验收报告产出（本文件）
