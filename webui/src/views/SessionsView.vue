<script setup>
// 会话历史（T2 侧边栏③）：检测历史（/scan/hits）、事件历史（/events）、
// 蜜饵历史（/traps）、告警历史（/alerts）分页列表 + 详情抽屉。
// 支持侧边栏内嵌最近列表跳转：?tab=hit|event|trap|alert&id=xxx 自动定位并打开详情。
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'
import { fmtTime } from '../utils/time'

const route = useRoute()

const tabs = [
  { name: 'event', label: '事件历史' },
  { name: 'hit', label: '检测历史' },
  { name: 'trap', label: '蜜饵历史' },
  { name: 'alert', label: '告警历史' },
]

const state = reactive({
  tab: 'event',
  eventItems: [], eventTotal: 0, eventPage: 1,
  hitItems: [], hitTotal: 0,
  trapItems: [], trapTotal: 0,
  alertItems: [], alertTotal: 0,
  page: 1,
  pageSize: 15,
  loading: false,
  error: '',
})

// A10：/events 后端 page_size 上限 100（le=100），取 50 走真实服务端分页；
// hit/trap/alert 三个 tab 仍用客户端分页（各自接口无 total 字段，限额均在接口约束内）。
const EVENT_PAGE_SIZE = 50

const detail = reactive({ show: false, title: '', json: {}, grading: null, gradingLoading: false, tab: '' })
// 新版四要素详情抽屉（告警原因+上下文+平台跳转+详细分析）
const detailDrawer = reactive({ show: false, detectionId: null })

const currentItems = computed(() => {
  const map = { event: state.eventItems, hit: state.hitItems, trap: state.trapItems, alert: state.alertItems }
  return map[state.tab] || []
})
const currentTotal = computed(() => {
  const map = { event: state.eventTotal, hit: state.hitTotal, trap: state.trapTotal, alert: state.alertTotal }
  return map[state.tab] || 0
})

// 客户端分页切片（仅 hit/trap/alert 用；event 由服务端分页，直接渲染 state.eventItems）
const pagedItems = computed(() => {
  if (state.tab === 'event') return state.eventItems
  const start = (state.page - 1) * state.pageSize
  return currentItems.value.slice(start, start + state.pageSize)
})

// 分页控件绑定：event 走服务端分页（eventPage/eventTotal），其余走客户端分页
const pager = computed(() => {
  if (state.tab === 'event') {
    return { page: state.eventPage, total: state.eventTotal, size: EVENT_PAGE_SIZE }
  }
  return { page: state.page, total: currentTotal.value, size: state.pageSize }
})

// A10：失败分支显示友好提示，不再渲染原始 pydantic/校验错误文本
function friendlyError(e) {
  if (e?.code === 'validation_error' || e?.status === 422) {
    return '事件历史加载失败：单页数量超出接口上限，请刷新重试'
  }
  return e?.message || '加载失败，请稍后重试'
}

async function loadEvents(page = state.eventPage) {
  try {
    const r = await api.listEvents({ page, pageSize: EVENT_PAGE_SIZE })
    state.eventItems = r?.items || []
    state.eventTotal = r?.total ?? state.eventItems.length
    state.eventPage = r?.page ?? page
  } catch (e) {
    if (state.tab === 'event') state.error = friendlyError(e)
  }
}

async function onPagerChange(p) {
  if (state.tab === 'event') {
    state.eventPage = p
    await loadEvents(p)
  } else {
    state.page = p
  }
}

async function loadHits() {
  try {
    state.hitItems = (await api.scanHits(100)) || []
    state.hitTotal = state.hitItems.length
  } catch (e) {
    if (state.tab === 'hit') state.error = e.message
  }
}

async function loadTraps() {
  try {
    state.trapItems = (await api.listTraps(undefined, 200)) || []
    state.trapTotal = state.trapItems.length
  } catch (e) {
    if (state.tab === 'trap') state.error = e.message
  }
}

async function loadAlerts() {
  try {
    state.alertItems = (await api.listAlerts({ limit: 200 })) || []
    state.alertTotal = state.alertItems.length
  } catch (e) {
    if (state.tab === 'alert') state.error = e.message
  }
}

const LOADERS = { event: loadEvents, hit: loadHits, trap: loadTraps, alert: loadAlerts }

async function load() {
  state.loading = true
  state.error = ''
  try {
    await Promise.allSettled([loadEvents(), loadHits(), loadTraps(), loadAlerts()])
  } finally {
    state.loading = false
  }
}

function openDetail(tab, row) {
  // 优先用新版四要素详情抽屉：有 detection_id 的场景（hit 直接用行 id，alert/event 从 payload 提取）
  let detId = null
  if (tab === 'hit') detId = row.id
  else if (tab === 'alert') detId = row.payload?.detection_id
  else if (tab === 'event') detId = row.payload?.detection_id
  if (detId) {
    detailDrawer.detectionId = detId
    detailDrawer.show = true
    return
  }
  // 无 detection_id（如 trap）回退到原始 JSON 抽屉
  detail.tab = tab
  detail.show = true
  detail.json = row
  detail.grading = null
  if (tab === 'trap') detail.title = `蜜饵 #${row.id}`
  else detail.title = `记录 #${row.id}`
}

function onTabChange() {
  state.page = 1
  state.eventPage = 1
  state.error = ''
  // event tab 由服务端分页：切回 event 时重载第 1 页，保证分页控件与列表一致
  if (state.tab === 'event' && !state.loading) loadEvents(1)
}

// 事件 tab 深链定位：按 id DESC 顺序向后扫描分页（上限 30 页），找到即切页并打开详情
async function locateEvent(id) {
  const maxPage = Math.min(Math.ceil((state.eventTotal || 0) / EVENT_PAGE_SIZE), 30)
  for (let p = 1; p <= maxPage; p++) {
    const r = await api.listEvents({ page: p, pageSize: EVENT_PAGE_SIZE })
    const items = r?.items || []
    const idx = items.findIndex((x) => Number(x.id) === id)
    if (idx >= 0) {
      state.eventItems = items
      state.eventTotal = r?.total ?? state.eventTotal
      state.eventPage = p
      openDetail('event', items[idx])
      return
    }
    if (!items.length) break
  }
}

// 侧边栏跳转定位：?tab=xxx&id=yyy
function handleRouteQuery() {
  const q = route.query
  const tab = ['event', 'hit', 'trap', 'alert'].includes(String(q.tab)) ? String(q.tab) : null
  if (!tab) return
  state.tab = tab
  state.page = 1
  const id = Number(q.id)
  if (!id) return
  // 等列表加载后定位
  const tryLocate = () => {
    const idx = currentItems.value.findIndex((r) => Number(r.id) === id)
    if (idx >= 0) {
      if (tab !== 'event') state.page = Math.floor(idx / state.pageSize) + 1
      openDetail(tab, currentItems.value[idx])
      return true
    }
    return false
  }
  if (tab === 'event') {
    // 事件 tab：先看已加载页，找不到则向后扫描服务端分页
    if (!tryLocate()) locateEvent(id)
    return
  }
  if (!tryLocate()) {
    const unwatch = watchImmediateHelper(tryLocate)
    setTimeout(() => { if (unwatch) unwatch() }, 8000)
  }
}

// 简单的一次性 watch 工具（列表异步加载完成后定位）
function watchImmediateHelper(fn) {
  const iv = setInterval(() => {
    if (fn()) clearInterval(iv)
  }, 300)
  setTimeout(() => clearInterval(iv), 8000)
  return () => clearInterval(iv)
}

const KIND_CN = {
  trap_hit: '踩饵命中', trap_draft_created: '蜜饵草稿', trap_deployed: '蜜饵部署',
  trap_monitored: '进入监控', trap_disabled: '蜜饵停用', trap_retired: '蜜饵退役',
  alert: '告警', case_published: '案例发布', case_ready: '案例就绪',
  evidence_built: '证据构建', evidence_frozen: '证据冻结',
  config_updated: '配置更新', account_updated: '账号更新',
}
const kindCn = (k) => KIND_CN[k] || k || ''

const statusMeta = {
  draft: { type: 'info', label: '草稿' }, active: { type: 'success', label: '已部署' },
  monitored: { type: 'warning', label: '监控中' }, hit: { type: 'danger', label: '已命中' },
  retired: { type: 'info', label: '已退役' },
}
const stMeta = (s) => statusMeta[s] || { type: 'info', label: s }
const levelType = (l) => ({ L3: 'warning', L4: 'danger', L5: 'danger' }[l] || 'info')
const PLATFORM_CN = { zhihu: '知乎', weibo: '微博', wechat: '微信', douyin: '抖音', rsshub: 'RSSHub', telegram: 'Telegram', discord: 'Discord', other: '其他' }
const PLATFORM_TAG = { zhihu: 'primary', weibo: 'danger', wechat: 'success', douyin: 'info', other: 'info' }
const platformLabel = (p) => PLATFORM_CN[p] || PLATFORM_CN.other
const platformTag = (p) => PLATFORM_TAG[p] || PLATFORM_TAG.other
const verdictMeta = (v) => ({ normal: 'success', suspicious: 'warning', fraud: 'danger' }[v] || 'info')

onMounted(() => {
  load()
  handleRouteQuery()
})
</script>

<template>
  <div class="af-page">
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <b>会话历史</b>
          <el-tabs v-model="state.tab" style="flex: 1" @tab-change="onTabChange">
            <el-tab-pane v-for="t in tabs" :key="t.name" :label="t.label" :name="t.name" />
          </el-tabs>
          <el-button size="small" :loading="state.loading" @click="load">刷新</el-button>
        </div>
      </template>

      <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 12px" />

      <!-- 事件历史 -->
      <el-table v-if="state.tab === 'event'" :data="pagedItems" v-loading="state.loading" size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="类型" width="120">
          <template #default="{ row }">
            <el-tag :type="['alert', 'trap_hit'].includes(row.kind) ? 'danger' : 'info'" size="small">{{ kindCn(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.ts || row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="载荷摘要" show-overflow-tooltip>
          <template #default="{ row }">{{ JSON.stringify(row.payload || {}) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }"><el-button size="small" type="primary" plain @click="openDetail('event', row)">详情</el-button></template>
        </el-table-column>
      </el-table>

      <!-- 检测历史 -->
      <el-table v-else-if="state.tab === 'hit'" :data="pagedItems" v-loading="state.loading" size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="判定" width="90">
          <template #default="{ row }"><el-tag :type="verdictMeta(row.llm_verdict || row.verdict)" size="small">{{ row.llm_verdict || row.verdict || '—' }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="90" />
        <el-table-column label="平台" width="90">
          <template #default="{ row }">
            <el-tag :type="platformTag(row.platform || (row.source === 'zhihu' ? 'zhihu' : 'other'))" size="small">{{ platformLabel(row.platform || (row.source === 'zhihu' ? 'zhihu' : 'other')) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="规则分" width="80">
          <template #default="{ row }">
            <span :class="(row.rule_score || 0) >= 10 ? 'text-danger' : (row.rule_score || 0) >= 6 ? 'text-warning' : 'text-success'">{{ row.rule_score }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="hit_count" label="命中" width="70" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }"><el-button size="small" type="primary" plain @click="openDetail('hit', row)">详情</el-button></template>
        </el-table-column>
      </el-table>

      <!-- 蜜饵历史 -->
      <el-table v-else-if="state.tab === 'trap'" :data="pagedItems" v-loading="state.loading" size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }"><el-tag :type="stMeta(row.status).type" size="small">{{ stMeta(row.status).label }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="bait_text" label="蜜饵文案" show-overflow-tooltip />
        <el-table-column prop="disguise_score" label="伪装度" width="80" />
        <el-table-column prop="hit_count" label="命中" width="70" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }"><el-button size="small" type="primary" plain @click="openDetail('trap', row)">详情</el-button></template>
        </el-table-column>
      </el-table>

      <!-- 告警历史 -->
      <el-table v-else :data="pagedItems" v-loading="state.loading" size="small">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="级别" width="80">
          <template #default="{ row }"><el-tag :type="levelType(row.level)" size="small">{{ row.level }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="channel" label="渠道" width="90" />
        <el-table-column label="内容" show-overflow-tooltip>
          <template #default="{ row }">{{ row.payload?.grade || JSON.stringify(row.payload || {}) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }"><el-button size="small" type="primary" plain @click="openDetail('alert', row)">详情</el-button></template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <el-pagination
          :current-page="pager.page"
          :page-size="pager.size"
          :total="pager.total"
          layout="total, prev, pager, next"
          background
          small
          @current-change="onPagerChange"
        />
      </div>
      <div v-if="!currentItems.length && !state.loading" class="af-muted" style="padding: 24px 0; text-align: center">
        暂无{{ tabs.find((t) => t.name === state.tab)?.label }}数据
      </div>
    </el-card>

    <!-- 新版四要素详情抽屉（告警原因+上下文+平台跳转+详细分析） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />

    <!-- 详情抽屉：原始记录 + 分级解释（检测记录） -->
    <el-drawer v-model="detail.show" :title="detail.title" size="52%">
      <div v-if="detail.tab === 'hit' && detail.gradingLoading" v-loading="true" style="height: 60px" />
      <el-card v-if="detail.tab === 'hit' && detail.grading" shadow="never" style="margin-bottom: 16px">
        <template #header><b>分级解释（/grading/explain）</b></template>
        <pre class="af-mono" style="margin: 0; font-size: 12px; white-space: pre-wrap">{{ JSON.stringify(detail.grading, null, 2) }}</pre>
      </el-card>
      <pre class="af-mono" style="margin: 0; font-size: 12px; white-space: pre-wrap">{{ JSON.stringify(detail.json, null, 2) }}</pre>
    </el-drawer>
  </div>
</template>
