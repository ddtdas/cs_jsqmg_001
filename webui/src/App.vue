<script setup>
// 主布局（T2 重构）：侧边栏 7 导航（会话历史内嵌最近会话列表）+ 旧版功能分组折叠
// + 折叠开关 + 顶栏健康指示 + 退出。样式按 uiskill（skills/ui/SKILL.md §4/§5/§8）。
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from './stores/auth'
import { api, getApiKey } from './api/client'
import { usePolling } from './composables/usePolling'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const collapsed = ref(false)

// ---------- 侧边栏 7 导航（顺序按用户字面要求） ----------
const mainMenus = [
  { path: '/assets', title: '资产管理', icon: 'Box' },
  { path: '/board', title: '看板', icon: 'DataBoard' },
  { path: '/sessions', title: '会话历史', icon: 'ChatDotRound' },
  { path: '/dashboard', title: '仪表盘', icon: 'Odometer' },
  { path: '/risk', title: '风险信息', icon: 'WarningFilled' },
  { path: '/collection', title: '信息收集', icon: 'FolderOpened' },
  { path: '/supply-chain', title: '供应链', icon: 'Share' },
]

// 旧版功能分组（保留原 11 路由中未进 7 导航的页面，可折叠分组）
const legacyMenus = [
  { path: '/traps', title: '蜜饵管理', icon: 'Aim' },
  // 伪装账号（反钓鱼伪装层：多层身份档案 + 假信息主动暴露 + 接触登记）——与蜜饵同属防御功能
  { path: '/cover', title: '伪装账号', icon: 'Suitcase' },
  { path: '/scan', title: '话术检测', icon: 'Search' },
  // 话术库（词条管理，与话术检测同域；词库变更即时生效）
  { path: '/speech-patterns', title: '话术库', icon: 'Notebook' },
  // 知识库（情报知识蒸馏管理：导入/检索/调分/复核/转话术）
  { path: '/knowledge', title: '知识库', icon: 'Reading' },
  { path: '/accounts', title: '账号速查', icon: 'User' },
  { path: '/evidence', title: '证据包', icon: 'Document' },
  { path: '/cases', title: '案例库', icon: 'Files' },
  { path: '/events', title: '事件流', icon: 'Bell' },
  { path: '/alerts', title: '告警', icon: 'Warning' },
  { path: '/zhihu', title: '知乎通道', icon: 'Connection' },
  // P15：聊天导入（微信/QQ 聊天记录一键扫描/导入）——置于知乎通道之后
  { path: '/chat-import', title: '聊天导入', icon: 'Upload' },
]

// 运维/设置分组（R2：/settings 设置页 = key 查看/重置 + MCP 接入；/ops-center 运维中心；
// /console 命令输入 = 内嵌 DSH Agent 控制台）
const opsMenus = [
  // P9：配置账号（账号桥接矩阵 + 私信自动接入）置于运维/设置分组首位
  { path: '/accounts-config', title: '配置账号', icon: 'Key' },
  // P10：采集矩阵（多平台 × 账号 × 策略 编排）排在「配置账号」之后
  { path: '/collector-matrix', title: '采集矩阵', icon: 'Grid' },
  { path: '/console', title: '命令输入', icon: 'Promotion' },
  { path: '/ops-center', title: '运维中心', icon: 'SetUp' },
  { path: '/settings', title: '设置', icon: 'Setting' },
]

// 激活态：/sessions 及其子项（内嵌最近列表）都算会话历史激活
const activeMenu = computed(() => {
  const p = route.path
  if (p.startsWith('/sessions')) return '/sessions'
  return p
})

// ---------- 会话历史内嵌最近列表（侧边栏一部分） ----------
// 仅保留检测记录（/scan/hits）6 条：点击跳 /sessions 并定位详情。
// （需求：会话历史只保留 6 条检测，不展开事件/蜜饵条目）
const recentSessions = ref([])
const recentLoading = ref(false)
let recentFirstRun = true
const { refresh: refreshRecent } = usePolling(async () => {
  // 首轮置 loading（初始加载提示）；后续轮询静默刷新，避免侧边栏提示反复闪烁
  if (recentFirstRun) recentLoading.value = true
  try {
    const hits = await api.scanHits(200)
    const items = []
    for (const h of hits || []) {
      // 会话历史侧边栏只展示有意义的真实通道检测，过滤掉 manual 测试残留（无意义条目）
      if (h.source === 'manual') continue
      items.push({
        key: `hit-${h.id}`,
        tab: 'hit',
        id: h.id,
        title: `检测 #${h.id} · ${h.verdict || h.source || '话术'}`,
        time: h.created_at,
        tag: { type: (h.rule_score || 0) >= 10 ? 'danger' : 'success', text: `分${h.rule_score}` },
      })
    }
    items.sort((a, b) => (b.time || '').localeCompare(a.time || ''))
    recentSessions.value = items.slice(0, 6)
  } catch {
    recentSessions.value = []
  } finally {
    recentLoading.value = false
    recentFirstRun = false
  }
}, 15000)

function goSession(item) {
  router.push({ path: '/sessions', query: { tab: item.tab, id: item.id } })
}

// 会话历史标题点击 → 跳 /sessions 页面；箭头 → 展开内嵌列表
function goSessionsPage() {
  router.push('/sessions')
}

// ---------- 顶栏 ----------
const { data: health } = usePolling(() => api.health(), 15000)

const lights = computed(() => {
  const h = health.value?.data || health.value
  if (!h) return []
  return [
    { label: '主控', ok: h.status === 'ok', extra: `v${h.version}` },
    { label: 'DB', ok: !!h.db, extra: '' },
    { label: 'LLM', ok: h.llm === 'configured', extra: h.llm === 'configured' ? '已配置' : '降级' },
    { label: '知乎', ok: h.zhihu !== 'idle' && !!h.zhihu, extra: h.zhihu },
  ]
})

async function onLogout() {
  try {
    await ElMessageBox.confirm('确定退出登录？将清除本地 API Key。', '退出', { type: 'warning', autofocus: false })
  } catch {
    return
  }
  auth.logout()
  router.push('/login')
}

// 审查 P2-8：admin key 不再明文回显——顶栏仅显示脱敏形式（af_admin_****），
// 点击复制按钮可拿到完整 key（navigator.clipboard 需要 HTTPS 或 localhost）。
const maskKey = computed(() => {
  const k = auth.key || ''
  if (!k) return ''
  if (k.length <= 8) return '****'
  return `${k.slice(0, 8)}****`
})

async function copyKey() {
  const k = getApiKey()
  if (!k) {
    ElMessage.warning('当前无有效 API Key')
    return
  }
  try {
    await navigator.clipboard.writeText(k)
    ElMessage.success('API Key 已复制（仅本机会话内使用）')
  } catch {
    ElMessage.error('复制失败，请手动从 data/bootstrap_admin_key.txt 读取')
  }
}

onMounted(refreshRecent)
</script>

<template>
  <el-container style="height: 100%">
    <el-aside class="af-aside" :width="collapsed ? '64px' : '220px'" style="background: var(--af-sidebar-bg); transition: width 0.25s">
      <div class="af-sidebar-logo" style="padding: 16px 14px; color: #fff; font-weight: 700; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px; height: 56px">
        <span style="font-size: 20px">🛡️</span>
        <span v-if="!collapsed">金丝雀蜜罐</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        :collapse="collapsed"
        :collapse-transition="false"
        class="af-sidebar-menu"
        background-color="transparent"
        text-color="#cfd6e4"
        active-text-color="#ffffff"
        popper-class="cg-sidebar-popper"
      >
        <!-- 7 导航（顺序固定：资产管理/看板/会话历史/仪表盘/风险信息/信息收集/供应链；
             会话历史 = 菜单项 + 下方直接平铺最近会话（不再折叠，点击项直达对应记录） -->
        <template v-for="m in mainMenus" :key="m.path">
          <el-menu-item :index="m.path === '/sessions' ? '/sessions' : m.path" @click="m.path === '/sessions' ? goSessionsPage() : null">
            <el-icon><component :is="m.icon" /></el-icon>
            <template #title>
              <span style="flex: 1">{{ m.title }}</span>
              <span v-if="m.path === '/sessions' && !collapsed" class="af-muted" style="color: #7a8aa5; font-size: 11px">{{ recentSessions.length }}</span>
            </template>
          </el-menu-item>
          <!-- 最近会话列表：会话历史下方直接平铺（取消无意义折叠，保持侧边栏快速跳转） -->
          <template v-if="m.path === '/sessions' && !collapsed">
            <div v-if="recentLoading" style="padding: 4px 20px 2px; color: #7a8aa5; font-size: 12px">加载最近会话…</div>
            <el-menu-item
              v-for="s in recentSessions"
              :key="s.key"
              :index="`/sessions?tab=${s.tab}&id=${s.id}`"
              @click="goSession(s)"
              style="padding-left: 40px; height: 34px"
            >
              <span class="af-ellipsis" style="flex: 1; font-size: 12px">{{ s.title }}</span>
              <el-tag :type="s.tag.type" size="small" effect="plain">{{ s.tag.text }}</el-tag>
            </el-menu-item>
          </template>
        </template>

        <!-- 旧版功能分组（保留原页面，折叠不消失） -->
        <el-menu-item-group v-if="!collapsed" title="旧版功能">
          <el-menu-item v-for="m in legacyMenus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <template #title>{{ m.title }}</template>
          </el-menu-item>
        </el-menu-item-group>
        <template v-else>
          <el-menu-item v-for="m in legacyMenus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <template #title>{{ m.title }}</template>
          </el-menu-item>
        </template>

        <!-- 运维/设置分组（R2：设置页 = key 查看/重置 + MCP 接入提示） -->
        <el-menu-item-group v-if="!collapsed" title="运维 / 设置">
          <el-menu-item v-for="m in opsMenus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <template #title>{{ m.title }}</template>
          </el-menu-item>
        </el-menu-item-group>
        <template v-else>
          <el-menu-item v-for="m in opsMenus" :key="m.path" :index="m.path">
            <el-icon><component :is="m.icon" /></el-icon>
            <template #title>{{ m.title }}</template>
          </el-menu-item>
        </template>

        <!-- 折叠开关 -->
        <div style="padding: 8px 14px; border-top: 1px solid rgba(255,255,255,0.08); margin-top: 8px">
          <el-tooltip :content="collapsed ? '展开侧边栏' : '折叠侧边栏'" placement="right">
            <el-button size="small" text style="color: #cfd6e4; width: 100%" @click="collapsed = !collapsed">
              <el-icon><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
              <span v-if="!collapsed" style="margin-left: 6px">折叠</span>
            </el-button>
          </el-tooltip>
        </div>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="af-header">
        <div class="af-header-title">
          <span v-if="collapsed" style="margin-right: 4px">🛡️</span>
          {{ route.meta.title || '金丝雀蜜罐' }}
        </div>
        <div style="display: flex; align-items: center; gap: 20px">
          <div style="display: flex; gap: 14px; align-items: center">
            <el-tooltip v-for="l in lights" :key="l.label" :content="`${l.label}${l.extra ? ' · ' + l.extra : ''}`" placement="bottom">
              <span :class="l.ok ? 'text-success' : 'text-danger'" style="display: inline-flex; align-items: center; gap: 4px; cursor: default">
                <span :style="{ width: 8, height: 8, borderRadius: '50%', background: l.ok ? '#67c23a' : '#f56c6c', display: 'inline-block' }" />
                {{ l.label }}
              </span>
            </el-tooltip>
          </div>
          <el-tooltip content="API Key（脱敏显示，点击复制完整 key）" placement="bottom">
            <span class="af-muted af-mono" style="max-width: 180px; word-break: break-all; cursor: pointer; display: inline-flex; align-items: center; gap: 4px" @click="copyKey">
              {{ maskKey }} 📋
            </span>
          </el-tooltip>
          <el-button size="small" @click="onLogout">退出</el-button>
        </div>
      </el-header>

      <el-main style="padding: 0; overflow: auto">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
