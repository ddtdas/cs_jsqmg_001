# 金丝雀蜜罐 WebUI（CanaryGuard AntiFraud 前端）

对齐主方案 §3 / §4.3。Vue3 + Vite + Element Plus + ECharts + Pinia + vue-router。
WebUI 只是主控 REST API 的客户端（**D1**：业务逻辑全在后端，前端不做任何重复实现）。

## 快速开始

```bash
cd webui
npm install          # 首次（依赖已全部验证可安装：vue/element-plus/echarts/pinia/vue-router/vite）
npm run dev          # 开发：http://127.0.0.1:5173/ui/ （代理 /api → 127.0.0.1:9200）
npm run build        # 生产构建 → dist/（FastAPI 静态托管于 http://127.0.0.1:9200/ui/）
```

> 开发服务器与主控在不同端口时跨域由 Vite 代理解决：`/api/* → http://127.0.0.1:9200`（无路径重写）。
> 生产部署将 `webui/dist/` 用 FastAPI `StaticFiles(html=True)` 挂载到 `/ui/`（需后端在 main.py 挂载，
> 或在 FastAPI 路由表中增加 `/ui/{path:path}` 兜底并回退 index.html 以支持 history 路由）。

## 认证（D6）

- 登录页输入 bootstrap admin key（`data/bootstrap_admin_key.txt`，首次启动自动生成，格式 `af_admin_xxxxxxxx`）。
- 验证方式：调用受保护端点 `/api/v1/system/stats`，通过即视为有效；key 保存于 **`sessionStorage('af_api_key')`**（会话级：关闭标签页/浏览器即失效，刷新页面会话内保持；审查 P2-8 修复，替代原 localStorage）。
- 所有请求自动携带 `X-API-Key` 请求头；也兼容 `Authorization: Bearer`（后端 deps.require_admin 支持）。
- 收到 401/`unauthorized` 时自动清除凭据并跳转 /login。
- 顶栏不再明文回显完整 key：脱敏显示 `af_admin_****`，点击可复制完整 key（仅本机会话内使用）。

## 页面清单（对齐 §4.3 路由表）

| 路由 | 页面 | 数据来源（真实 API） |
|---|---|---|
| /login | 登录 | POST /auth/bootstrap-info + GET /system/stats（验证 key） |
| /dashboard | 仪表盘 | /traps、/scan/hits、/alerts、/cases、/cases/graph、/grading/levels 聚合 + ECharts 趋势/五级分布 |
| /traps | 蜜饵管理 | GET/POST /traps、/traps/generate-draft、/{id}/deploy、/monitor、/disable、/retire（写操作均 🔒 confirm+reason）、/check-hit（POST body {text}，命中即退役） |
| /cover | 伪装账号 | GET/POST /cover/identities、/{id}/toggle、/{id}/expose、/cover/contact、/cover/contacts、/cover/map（P19 反钓鱼伪装，全部 🔒 require_admin） |
| /scan | 话术检测 | POST /scan/text（实时返回判定+命中+分级）、GET /scan/hits、POST /zhihu/import（CH-D 手动导入 🔒） |
| /accounts | 账号速查 | POST /accounts/check（绿黄红 + 证据链 + 共现）、GET /accounts/{url_name}、/{id}/timeline |
| /evidence | 证据包 | POST /evidence/build、GET /evidence/{id}、/freeze、/export、/report-template |
| /cases | 案例库 | GET /cases（分页/tag 过滤）、/cases/graph（骗术图谱 ECharts）、POST /cases/publish |
| /events | 事件流 | GET /events（事件库，kind 过滤/分页；P6.5 已实现）+ 5s 轮询实时；/events 不可用时降级聚合 /traps + /scan/hits + /alerts |
| /alerts | 告警 | GET /alerts（级别/未读过滤）、POST /alerts/{id}/read |
| /zhihu | 知乎通道 | GET /zhihu/channels、/zhihu/status、POST /channels/{id}/login（cookie 注入）、/verify |
| /ops-center | 运维中心 | GET /system/health 四灯、/zhihu/status 降级链、/grading/levels、POST /auth/bootstrap-info |

## 实时性说明（重要）

- 后端**已实现** `/events` REST 端点（P6.5：事件落库 events 表，GET /events 分页/kind 过滤、GET /events/{id}；
  MCP `af_list_events` / `af_get_event` 同源透传），`/ws/events` WebSocket 与 `event_bus` 仍**未落地**。
- 因此「蜜饵命中 <1s 推送」的目标由 **5 秒轮询**承担：EventsView 主数据源直接用 `GET /events`（真实事件库），
  轮询刷新提供实时性；`/events` 请求失败时自动降级为 /traps + /scan/hits + /alerts 聚合时间线（D5 降级链），
  新命中（hit_count 增加）或状态迁移在 ≤5s 内可见。后端 ws_hub 就绪后，可在 EventsView/TrapsView 中
  无缝替换为 WS 订阅（事件消息协议：`{kind: hit|alert|trap, payload}` 映射自 events 表）。
- 仪表盘统计：/system/stats 已返回真实计数（traps/detections/cases/cover_identities/contact_events 等），Dashboard 聚合真实端点为主，
  stats 实时计数与真实端点聚合一致。

## 目录结构

```
webui/
├── index.html / vite.config.js / package.json
├── src/
│   ├── main.js            # 入口（ElementPlus zh-CN + Pinia + Router + 401 钩子）
│   ├── App.vue            # 布局（侧边栏导航 + 顶栏健康四灯 + 退出）
│   ├── styles.css
│   ├── api/client.js      # fetch 封装：统一封包 {ok,data}/{ok:false,error} 解包 + X-API-Key 注入 + sessionStorage 凭据；端点覆盖全部 101 paths（含 /events、/config、/scan/inbox、/cover）
│   ├── router/index.js    # 路由 + 登录守卫（sessionStorage 会话级判定）
│   ├── stores/auth.js     # Pinia 认证 store（key 会话级持久化/校验/登出）
│   ├── composables/usePolling.js  # 轮询组合式函数（5s 实时降级）
│   └── views/             # 25 个页面视图组件
└── dist/                  # 构建产物（npm run build）
```

## 接口约定

所有 REST 响应为统一封包：成功 `{ok:true, data}`；失败 `{ok:false, error:{code,message}}`
（app/utils.py）。错误码：401 unauthorized、403 forbidden、404 not_found、422 validation_error、
429 rate_limited（P2-5 按 IP 限流，携带有效 admin key 豁免）、500 internal_error。
WebUI 仅依赖此协议，不解析后端内部结构（D1）。

## 合规红线

- 蜜饵只产草稿不自动发布（HITL）；发布后仍需用户手动执行并显式「部署」。
- 案例发布强制脱敏校验（后端拒绝含敏感信息的载荷）。
- 知乎 cookie 由用户自行导出粘贴，系统加密存储、明文不回显；只读自有/公开数据。
- 本系统只输出证据包/举报模板，不冻结/不拦截/不处置（处置权归官方）。