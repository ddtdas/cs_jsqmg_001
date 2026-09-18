// 认证 store：bootstrap key（X-API-Key）登录态。
// 后端 P6 现状无 /auth/login，认证 = admin key 校验；登录即验证 key 可用性后持久化。
// 凭据存储（审查 P2-8）：sessionStorage 会话级存储——关闭标签页/浏览器即失效。
import { defineStore } from 'pinia'
import { api } from '../api/client'
import { setApiKey } from '../api/client'

const KEY_NAME = 'af_api_key'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    key: '',
    verified: false,
    verifying: false,
    error: '',
  }),
  getters: {
    isAuthed: (s) => s.verified && !!s.key,
  },
  actions: {
    init() {
      try {
        this.key = sessionStorage.getItem(KEY_NAME) || ''
      } catch {
        this.key = ''
      }
      if (this.key) this.verified = true
    },
    // P2-D5：路由守卫只校验 key 存在性（避免每次导航打 API）；此处做一次性真实校验：
    // App 挂载时对受保护端点 /system/stats 探活一次。key 过期/伪造 → 401 →
    // client.js 统一清 sessionStorage 并跳登录；非 401 错误（网络等）静默，不打断使用。
    async probeSession() {
      if (!this.key) return false
      try {
        await api.stats()
        this.verified = true
        return true
      } catch (e) {
        if (e?.code === 'unauthorized') this.verified = false
        return false
      }
    },
    async login(key) {
      this.verifying = true
      this.error = ''
      const trimmed = (key || '').trim()
      try {
        // 先持久化再验证：X-API-Key 注入依赖 sessionStorage（原实现 bootstrapInfo 先于
        // setApiKey 调用、用旧凭据请求，顺序修正为「先存 key 再验证」）
        this.key = trimmed
        setApiKey(trimmed)
        // bootstrap-info 为公开端点（端口/key 文件提示，无需认证），失败不阻塞登录
        let hint = ''
        try {
          const info = await api.bootstrapInfo()
          hint = info?.hint || ''
        } catch { /* 提示信息非关键路径 */ }
        // 受保护端点验证 key：401 即无效
        await api.stats()
        this.verified = true
        return { ok: true, hint }
      } catch (e) {
        this.key = ''
        setApiKey('')
        this.error = e.message
        return { ok: false, error: e.message }
      } finally {
        this.verifying = false
      }
    },
    logout() {
      this.key = ''
      this.verified = false
      setApiKey('')
    },
  },
})