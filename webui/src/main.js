// CanaryGuard WebUI 入口：Vue3 + Element Plus + ECharts + Pinia + vue-router
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler } from './api/client'
import { useAuthStore } from './stores/auth'
import './styles.css'

const app = createApp(App)
const pinia = createPinia()
app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: zhCn })

// 全局注册 Element Plus 图标：App.vue 菜单 / Dashboard 统计卡等以字符串组件名
// （<component :is="'Odometer'">）引用，未注册会导致运行时 resolve 失败（console warning）。
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

// 401 → 清凭据跳登录（登录守卫对未认证访问重定向）
const auth = useAuthStore(pinia)
auth.init()
// P2-D5：挂载时对受保护端点做一次探活，过期/伪造 key → 401 → client.js 统一跳登录
if (auth.key) auth.probeSession()
setUnauthorizedHandler(() => {
  auth.logout()
  const current = router.currentRoute.value
  if (current.path !== '/login') router.push({ path: '/login', query: { redirect: current.fullPath } })
})

app.mount('#app')