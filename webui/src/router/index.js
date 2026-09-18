// 全局路由：7 项侧边栏导航（T2 重构）+ 旧版功能路由全部保留兼容
// 新增（T2）: /assets /board /sessions /risk /collection /supply-chain
// 保留（旧版）: /login /traps /scan /accounts /evidence /cases /events /alerts /zhihu /ops-center
import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/LoginView.vue'), meta: { public: true } },
  { path: '/', redirect: '/dashboard' },

  // ---- T2 侧边栏 7 导航 ----
  { path: '/assets', name: 'assets', component: () => import('../views/AssetsView.vue'), meta: { title: '资产管理' } },
  { path: '/board', name: 'board', component: () => import('../views/BoardView.vue'), meta: { title: '看板' } },
  { path: '/sessions', name: 'sessions', component: () => import('../views/SessionsView.vue'), meta: { title: '会话历史' } },
  { path: '/dashboard', name: 'dashboard', component: () => import('../views/DashboardView.vue'), meta: { title: '仪表盘' } },
  { path: '/risk', name: 'risk', component: () => import('../views/RiskView.vue'), meta: { title: '风险信息' } },
  { path: '/collection', name: 'collection', component: () => import('../views/CollectionView.vue'), meta: { title: '信息收集' } },
  { path: '/supply-chain', name: 'supply-chain', component: () => import('../views/SupplyChainView.vue'), meta: { title: '供应链' } },

  // ---- 旧版功能路由（导航分组保留，不删除）----
  { path: '/traps', name: 'traps', component: () => import('../views/TrapsView.vue'), meta: { title: '蜜饵管理' } },
  // ---- 伪装账号（反钓鱼伪装层：多层身份档案 + 假信息主动暴露 + 接触登记锁定骗子范围）----
  { path: '/cover', name: 'cover', component: () => import('../views/CoverView.vue'), meta: { title: '伪装账号' } },
  { path: '/scan', name: 'scan', component: () => import('../views/ScanView.vue'), meta: { title: '话术检测' } },
  // ---- 话术库（词条 CRUD / 启停 / 导入导出 / 命中统计，与 /scan 同域）----
  { path: '/speech-patterns', name: 'speech-patterns', component: () => import('../views/SpeechPatternsView.vue'), meta: { title: '话术库' } },
  // ---- 知识库（情报知识蒸馏管理：导入/检索/调分/复核/转话术，加权排序）----
  { path: '/knowledge', name: 'knowledge', component: () => import('../views/KnowledgeView.vue'), meta: { title: '知识库' } },
  { path: '/accounts', name: 'accounts', component: () => import('../views/AccountsView.vue'), meta: { title: '账号速查' } },
  { path: '/evidence', name: 'evidence', component: () => import('../views/EvidenceView.vue'), meta: { title: '证据包' } },
  { path: '/cases', name: 'cases', component: () => import('../views/CasesView.vue'), meta: { title: '案例库' } },
  { path: '/events', name: 'events', component: () => import('../views/EventsView.vue'), meta: { title: '事件流' } },
  { path: '/alerts', name: 'alerts', component: () => import('../views/AlertsView.vue'), meta: { title: '告警' } },
  { path: '/zhihu', name: 'zhihu', component: () => import('../views/ZhihuView.vue'), meta: { title: '知乎通道' } },
  // ---- 聊天导入（P15：微信/QQ 聊天记录一键扫描/导入）----
  { path: '/chat-import', name: 'chat-import', component: () => import('../views/ChatImportView.vue'), meta: { title: '聊天导入' } },
  { path: '/ops-center', name: 'ops-center', component: () => import('../views/OpsCenterView.vue'), meta: { title: '运维中心' } },

  // ---- 运维/设置（R2：key 查看/重置 + MCP 接入提示；需登录，非 public）----
  { path: '/settings', name: 'settings', component: () => import('../views/SettingsView.vue'), meta: { title: '设置' } },

  // ---- 配置账号（P9：账号桥接矩阵 + 私信自动接入 + 反向侦察）----
  { path: '/accounts-config', name: 'accounts-config', component: () => import('../views/AccountsConfigView.vue'), meta: { title: '配置账号' } },

  // ---- 采集矩阵（P10：平台 × 账号 × 策略 编排 + 立即执行 + GitHub 技术栈）----
  { path: '/collector-matrix', name: 'collector-matrix', component: () => import('../views/CollectorMatrixView.vue'), meta: { title: '采集矩阵' } },

  // ---- 命令输入（内嵌 DSH Agent 控制台）----
  { path: '/console', name: 'console', component: () => import('../views/ConsoleView.vue'), meta: { title: '命令输入' } },

  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]

const router = createRouter({
  history: createWebHistory('/ui/'),
  routes,
})

// 登录守卫：未登录访问受保护页 → 跳 /login（凭据会话级存储，审查 P2-8）
router.beforeEach((to) => {
  let authed = false
  try {
    authed = !!sessionStorage.getItem('af_api_key')
  } catch {
    authed = false
  }
  if (!to.meta.public && !authed) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.path === '/login' && authed) return { path: '/dashboard' }
  return true
})

export default router