# 05 · WebUI

> Vue3 + Vite + Element Plus + ECharts + Pinia + vue-router。WebUI 只是主控 REST API 的客户端（D1），业务逻辑全在后端。

## 1. 访问

| 模式 | 地址 | 说明 |
|---|---|---|
| 生产（推荐） | `http://127.0.0.1:9200/ui/` | 主控静态托管 `webui/dist/`（已构建则自动挂载） |
| 开发 | `http://127.0.0.1:5173/ui/` | `cd webui && npm run dev`（Vite 代理 `/api` → 127.0.0.1:9200） |

## 2. 登录

- 首次进入 /login，粘贴 **bootstrap admin key**（`data\bootstrap_admin_key.txt`，`af_admin_*`）。
- 验证方式：调用受保护端点 `/api/v1/system/stats`，通过即有效；key 持久化于 `sessionStorage('af_api_key')`。
- 所有请求自动携带 `X-API-Key`；401 时自动清凭据并跳回 /login。

## 3. 页面清单（25 路由 / 25 视图，含 /cover 伪装账号）

| 路由 | 页面 | 数据来源（真实 API） |
|---|---|---|
| /dashboard | 仪表盘 | traps/scan/hits/alerts/cases/cases/graph/grading/levels 聚合 + ECharts 趋势与五级分布 |
| /traps | 蜜饵管理 | GET/POST /traps、/traps/generate-draft、/{id}/deploy|monitor|disable|retire|check-hit |
| /cover | 伪装账号 | GET/POST /cover/identities、/{id}/toggle、/{id}/expose、/cover/contact、/cover/contacts、/cover/map（P19 反钓鱼伪装，全部端点 🔒 require_admin） |
| /scan | 话术检测 | POST /scan/text、GET /scan/hits、POST /zhihu/import（CH-D 手动导入） |
| /accounts | 账号速查 | POST /accounts/check、GET /accounts/{url_name}、/{id}/timeline |
| /evidence | 证据包 | POST /evidence/build、GET /evidence/{id}、/freeze、/export、/report-template |
| /cases | 案例库 | GET /cases、/cases/graph、POST /cases/publish |
| /events | 事件流 | 5s 轮询聚合 traps/scan/hits/alerts（后端无 /events/WS，降级） |
| /alerts | 告警 | GET /alerts（级别/未读过滤）、POST /alerts/{id}/read |
| /zhihu | 知乎通道 | GET /zhihu/channels、/zhihu/status、channels/{id}/login|verify |
| /ops-center | 运维中心 | /system/health 四灯、/zhihu/status 降级链、/grading/levels |
| /login | 登录 | /auth/bootstrap-info + /system/stats 验证 |
| /assets | 资产管理 | 账号/资产情报聚合（P9 桥接矩阵） |
| /board | 看板 | traps/detections/events 聚合看板（T2） |
| /sessions | 会话历史 | 检测/会话历史查询 |
| /risk | 风险信息 | 风险账号/检测风险聚合 |
| /collection | 信息收集 | 采集入口（CH-D 导入/链接采集） |
| /supply-chain | 供应链 | POST /supply-chain/query、GET /supply-chain/results |
| /speech-patterns | 话术库 | GET|POST /speech-patterns、/{id}/toggle、/import、/export（P17） |
| /knowledge | 知识库 | GET /knowledge、/search、/import、/{id}/verified|to-pattern（P18） |
| /chat-import | 聊天导入 | /chat-import/candidates、/scan、/import、/import-file、/imported（P15） |
| /settings | 设置 | /auth/current-key、/auth/reset-key、MCP 接入提示（R2） |
| /accounts-config | 配置账号 | /account-bridges 矩阵 + /{platform}/config|test|ingest（P9） |
| /collector-matrix | 采集矩阵 | /collector/matrix CRUD + /{id}/run + /run-all + /tech-stack（P10） |
| /console | 命令输入 | 内嵌 DSH Agent 控制台 |

## 4. 构建与开发

```bash
cd webui
npm install          # 首次
npm run dev          # 开发：127.0.0.1:5173（代理 /api → 127.0.0.1:9200）
npm run build        # 生产 → dist/（主控 /ui/ 托管）
```

- `vite.config.js`：`base: '/ui/'` 对齐后端托管前缀；代理无路径重写。
- 前端只依赖统一封包协议 `{ok:data}|{ok:false,error}`，不解析后端内部结构。

## 5. 实时性说明

- 后端已提供 `/events` REST 端点（app/api/events.py），`/ws/events` WebSocket 未提供，实时性由 5 秒轮询承担（ws_hub/event_bus 未落地）。
- 「蜜饵命中 <1s 推送」由 **5 秒轮询**降级承担（/traps、/events 页面轮询真实端点），新命中 ≤5s 可见。
- 后端 ws_hub 就绪后可在 EventsView/TrapsView 无缝替换为 WS 订阅。

## 6. 合规红线（前端侧）

- 蜜饵只产草稿不自动发布（HITL）；部署按钮仅标记状态，发布动作由用户在知乎侧手动完成。
- 案例发布强制脱敏校验（后端拒绝含敏感信息载荷）。
- 知乎 cookie 由用户自行导出粘贴，明文不回显；只读自有/公开数据。
- 系统只输出证据包/举报模板，不冻结/不拦截/不处置（处置权归官方）。