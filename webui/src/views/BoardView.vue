<script setup>
// 看板（T2 侧边栏②）：案例库 + 骗术图谱 + 统计看板 ECharts + 检测速览 + 社工库指标。
// 聚合 /cases /cases/graph /scan/hits /alerts /system/stats /soc-lib/status /detections/{id}/full，
// 全部真实 API 数据；任一数据源失败仅降级对应卡片，不阻塞整页。
// 检测速览说明：/scan/hits 不返回 grade → 前端惰性并行补拉 /detections/{id}/full
// 提取 grade / grade_reason / se_factors（含 divergence.playbook），失败静默跳过（不改后端）。
import { computed, nextTick, onBeforeUnmount, onMounted, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'
import { fmtTime } from '../utils/time'

const router = useRouter()
const detailDrawer = reactive({ show: false, detectionId: null })

const state = reactive({
  cases: { total: 0, items: [] },
  graph: { by_tag: [], by_grade: [], total: 0 },
  hits: [],
  alerts: [],
  stats: null,
  socLib: null, // /soc-lib/status → { hibp_configured, local_records }；取不到保持 null → '—'
  enriching: false, // 检测分级惰性增强中
  loading: true,
  error: '',
  // 拟真账号（/persona/schedules）：全部防御式 try/catch，API 不可用时显示 '—'
  persona: { schedules: [], loading: false, error: '' },
  personaDialog: { show: false, saving: false, error: '', form: { account: '', type: 'life', interval_minutes: 120, total: 10 } },
  // 应对骗子弹窗（/persona/respond）：result={matched,reply,reasoning,extracted_iocs}
  respondDialog: { show: false, saving: false, error: '', form: { account: '', incoming_text: '' }, result: null },
  // 实战演练弹窗（/persona/drills + /persona/drill）：suggestion={suggested_reply,warning,reasoning,...}
  drillDialog: { show: false, saving: false, drillsLoading: false, error: '', drills: [], form: { account: '', scenario: '', incoming_text: '' }, suggestion: null },
})

// ---- 拟真账号 ----
const PERSONA_TYPES = [
  { value: 'life', label: '生活' },
  { value: 'work', label: '工作' },
  { value: 'study', label: '学习' },
  { value: 'social', label: '社交' },
  { value: 'hobby', label: '兴趣' },
]
const personaTypeLabel = (t) => (PERSONA_TYPES.find((x) => x.value === t) || {}).label || t || '—'

async function loadPersonaSchedules() {
  state.persona.loading = true
  state.persona.error = ''
  try {
    if (typeof api.personaSchedules !== 'function') return
    const raw = await api.personaSchedules()
    const obj = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
    state.persona.schedules = Array.isArray(obj.schedules) ? obj.schedules : []
  } catch (e) {
    state.persona.error = e.message
  } finally {
    state.persona.loading = false
  }
}

function openPersonaDialog() {
  state.personaDialog.form = { account: '', type: 'life', interval_minutes: 120, total: 10 }
  state.personaDialog.error = ''
  state.personaDialog.show = true
}

async function createPersonaSchedule() {
  const f = state.personaDialog.form
  if (!f.account || !String(f.account).trim()) {
    state.personaDialog.error = '请填写拟真账号名'
    return
  }
  state.personaDialog.saving = true
  state.personaDialog.error = ''
  try {
    await api.personaSchedule({
      account: String(f.account).trim(),
      type: f.type,
      interval_minutes: Number(f.interval_minutes) || 120,
      total: Number(f.total) || 10,
    })
    state.personaDialog.show = false
    ElMessage.success('定时发布计划已创建')
    loadPersonaSchedules()
  } catch (e) {
    state.personaDialog.error = e.message
  } finally {
    state.personaDialog.saving = false
  }
}

async function publishPersonaNow(row) {
  if (!row?.id) return
  // P1 防呆：立即发布不可撤回，先确认再调 API；取消则不发送
  try {
    await ElMessageBox.confirm(
      `将立即发布一条动态到 ${row.account || ''}，不可撤回。确认继续？`,
      '确认发布',
      { type: 'warning', confirmButtonText: '确认发布', cancelButtonText: '取消', autofocus: false },
    )
  } catch {
    return // 用户取消确认，不发布
  }
  try {
    await api.personaPublishNow(row.id)
    ElMessage.success(`计划 #${row.id} 已立即发布`)
    loadPersonaSchedules()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// ---- 拟真账号 · 应对骗子（POST /persona/respond）----
function openRespondDialog(row) {
  state.respondDialog.form = { account: row?.account || '', incoming_text: '' }
  state.respondDialog.result = null
  state.respondDialog.error = ''
  state.respondDialog.show = true
}

async function submitRespond() {
  const f = state.respondDialog.form
  const account = String(f.account || '').trim()
  const incoming = String(f.incoming_text || '').trim()
  if (!account) {
    state.respondDialog.error = '缺少拟真账号'
    return
  }
  if (!incoming) {
    state.respondDialog.error = '请填写骗子私信内容'
    return
  }
  state.respondDialog.saving = true
  state.respondDialog.error = ''
  state.respondDialog.result = null
  try {
    if (typeof api.personaRespond !== 'function') throw new Error('personaRespond 接口不可用')
    const res = await api.personaRespond({ account, incoming_text: incoming })
    // 后端统一封包 {result:{matched,reply,reasoning,extracted_iocs}}；防御式兼容直接返回 result 的情形
    state.respondDialog.result =
      res && typeof res === 'object' && res.result && typeof res.result === 'object' ? res.result : res
    // P1-4：LLM 未配置时 reply 为空 → 不再谎报"应对回复已生成"，明确降级提示
    const reply = (state.respondDialog.result || {}).reply
    if (reply && String(reply).trim()) {
      ElMessage.success('应对回复已生成')
    } else {
      ElMessage.warning('LLM 未配置，未生成应对（可用模板池）')
    }
  } catch (e) {
    state.respondDialog.error = e.message
    ElMessage.error(e.message)
  } finally {
    state.respondDialog.saving = false
  }
}

// ---- 拟真账号 · 实战演练（GET /persona/drills + POST /persona/drill）----
const currentDrill = computed(
  () => state.drillDialog.drills.find((d) => d.scenario === state.drillDialog.form.scenario) || null,
)

async function openDrillDialog(row) {
  state.drillDialog.form = { account: row?.account || '', scenario: '', incoming_text: '' }
  state.drillDialog.suggestion = null
  state.drillDialog.error = ''
  state.drillDialog.drills = []
  state.drillDialog.show = true
  await loadDrills()
}

async function loadDrills() {
  state.drillDialog.drillsLoading = true
  try {
    if (typeof api.personaDrills !== 'function') throw new Error('personaDrills 接口不可用')
    const raw = await api.personaDrills()
    const obj = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
    state.drillDialog.drills = Array.isArray(obj.drills) ? obj.drills : []
    if (state.drillDialog.drills.length && !state.drillDialog.form.scenario) {
      // 默认选中第一个剧本；incoming_text 留空 → 提交时省略，由后端使用剧本典型私信
      state.drillDialog.form.scenario = state.drillDialog.drills[0].scenario
    }
  } catch (e) {
    state.drillDialog.error = e.message
    ElMessage.error(e.message)
  } finally {
    state.drillDialog.drillsLoading = false
  }
}

// 切换剧本：清空自定义私信（占位提示换成新剧本 typical_incoming，留空即用后端缺省）
function onDrillScenarioChange() {
  state.drillDialog.form.incoming_text = ''
}

async function submitDrill() {
  const f = state.drillDialog.form
  const account = String(f.account || '').trim()
  const scenario = String(f.scenario || '').trim()
  if (!account) {
    state.drillDialog.error = '缺少拟真账号'
    return
  }
  if (!scenario) {
    state.drillDialog.error = '请选择演练剧本'
    return
  }
  const payload = { scenario }
  const incoming = String(f.incoming_text || '').trim()
  if (incoming) payload.incoming_text = incoming
  state.drillDialog.saving = true
  state.drillDialog.error = ''
  state.drillDialog.suggestion = null
  try {
    if (typeof api.personaDrill !== 'function') throw new Error('personaDrill 接口不可用')
    const res = await api.personaDrill(payload)
    state.drillDialog.suggestion =
      res && typeof res === 'object' && res.result && typeof res.result === 'object' ? res.result : res
    ElMessage.success('演练完成')
  } catch (e) {
    state.drillDialog.error = e.message
    ElMessage.error(e.message)
  } finally {
    state.drillDialog.saving = false
  }
}

// IOC 文本：演练结果无 extracted_iocs 时用剧本 iocs_hint 兜底
function drillIocsText() {
  const s = state.drillDialog.suggestion
  const v = s ? (s.extracted_iocs ?? s.iocs_hint ?? currentDrill.value?.iocs_hint) : null
  if (v == null) return '（无）'
  if (Array.isArray(v)) return v.length ? v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join('\n') : '（无）'
  if (typeof v === 'object') return JSON.stringify(v, null, 2)
  return String(v)
}

const levelType = (l) => ({ L3: 'warning', L4: 'danger', L5: 'danger' }[l] || 'info')
const verdictMeta = (v) => ({ normal: 'success', suspicious: 'warning', fraud: 'danger' }[v] || 'info')
const ruleScoreCls = (s) => ((s || 0) >= 10 ? 'text-danger' : (s || 0) >= 6 ? 'text-warning' : 'text-success')

// ---- 统计卡（每张标注数据来源）----
const socLibValue = computed(() => {
  // 优先 /soc-lib/status.local_records（真实泄露索引数）；退化 /system/stats.soc_hits；都没有 → '—'（防御式）
  if (state.socLib && typeof state.socLib.local_records === 'number') return state.socLib.local_records
  if (state.stats && typeof state.stats.soc_hits === 'number') return state.stats.soc_hits
  return '—'
})

const statCards = computed(() => [
  { label: '案例总数', value: state.cases.total, icon: 'Files', color: '#409eff', source: '案例库' },
  { label: '骗术标签', value: (state.graph.by_tag || []).length, icon: 'DataAnalysis', color: '#67c23a', source: '案例库' },
  { label: '检测记录', value: state.hits.length, icon: 'Search', color: '#e6a23c', source: '检测记录' },
  { label: '告警(L3+)', value: state.alerts.length, icon: 'Warning', color: '#f56c6c', source: '告警记录' },
  { label: '社工库索引', value: socLibValue.value, icon: 'Aim', color: '#9c27b0', source: '泄露信息查询库' }, // local_records = 本地泄露索引记录数（3 demo + 7 真实事件）
])

// ---- 社工库指标：/soc-lib/status.local_records（真实泄露索引数）优先，stats.soc_hits 兜底，均失败显示 '—' 且不报错 ----
async function loadSocLib() {
  try {
    if (typeof api.socLibStatus === 'function') {
      const raw = await api.socLibStatus()
      const obj = raw && typeof raw === 'object' && !Array.isArray(raw)
        ? (raw.status && typeof raw.status === 'object' ? raw.status : raw)
        : {}
      state.socLib = obj
    }
  } catch {
    state.socLib = null
  }
  if (!state.socLib || typeof state.socLib.local_records !== 'number') {
    if (state.stats && typeof state.stats.soc_hits === 'number') {
      state.socLib = { local_records: state.stats.soc_hits }
    }
  }
}

// ---- 检测速览 ----
const GRADE_RANK = { L5: 5, L4: 4, L3: 3, L2: 2, L1: 1 }
const gradeRank = (h) => GRADE_RANK[h?.grade] || 0

// 按 grade 降序（L5 → L4 → … → 未分级），同级按 id 降序（最新在前）
const sortedHits = computed(() =>
  [...state.hits].sort((a, b) => (gradeRank(b) - gradeRank(a)) || ((b.id || 0) - (a.id || 0))),
)

// L4/L5 红色高亮行
function hitRowClass({ row }) {
  return ['L4', 'L5'].includes(row.grade) ? 'af-row-danger' : ''
}

// divergence.playbook：中奖 / 刷单 / 冒充 等（无 → 隐藏）
const PLAYBOOK_TAG = { 中奖: 'success', 刷单: 'warning', 冒充: 'danger', 兼职: 'info', 杀猪盘: 'danger' }
function playbooks(row) {
  const pb = row?.se_factors?.divergence?.playbook
  if (pb == null) return []
  return (Array.isArray(pb) ? pb : [pb]).map((x) => String(x)).filter(Boolean)
}
const playbookType = (p) => Object.entries(PLAYBOOK_TAG).find(([k]) => String(p).includes(k))?.[1] || 'info'

// 推理链按钮 → 触发既有 DetailDrawer（告警原因证据链 + 社会工程学分析推理链速览）
function openHitDetail(row) {
  if (!row?.id) return
  detailDrawer.detectionId = row.id
  detailDrawer.show = true
}

// 会话深链 → SessionsView 的 ?tab=hit&id= 自动定位并打开详情
function openHitSession(row) {
  if (!row?.id) return
  router.push({ path: '/sessions', query: { tab: 'hit', id: row.id } })
}

// ---- 惰性分级增强：/scan/hits 无 grade → 逐行补拉 /detections/{id}/full ----
async function enrichGrades() {
  const targets = state.hits.slice(0, 30).filter((h) => !h.grade)
  if (!targets.length) return
  state.enriching = true
  const CHUNK = 6
  for (let i = 0; i < targets.length; i += CHUNK) {
    await Promise.allSettled(
      targets.slice(i, i + CHUNK).map(async (h) => {
        try {
          const d = await api.detectionFull(h.id)
          const det = d?.detection
          if (det && typeof det === 'object') {
            if (det.grade) h.grade = det.grade
            if (det.grade_reason) h.grade_reason = det.grade_reason
            if (det.se_factors && typeof det.se_factors === 'object' && Object.keys(det.se_factors).length) {
              h.se_factors = det.se_factors
            }
          }
        } catch {
          /* 单行增强失败静默：该行保持原样（grade 显示 '—'），不报错不阻塞 */
        }
      }),
    )
  }
  state.enriching = false
}

// ---- 主加载 ----
async function loadAll() {
  state.loading = true
  state.error = ''
  try {
    const [cases, graph, hits, alerts, stats] = await Promise.allSettled([
      api.listCases(1, 100),
      api.caseGraph(),
      api.scanHits(50),
      api.listAlerts({ limit: 100 }),
      api.stats(),
    ])
    const failed = []
    if (cases.status === 'fulfilled') state.cases = cases.value || { total: 0, items: [] }
    else failed.push(`案例(${cases.reason?.message || '请求失败'})`)
    if (graph.status === 'fulfilled') state.graph = graph.value || { by_tag: [], by_grade: [], total: 0 }
    else failed.push(`图谱(${graph.reason?.message || '请求失败'})`)
    if (hits.status === 'fulfilled') state.hits = hits.value || []
    else failed.push(`检测(${hits.reason?.message || '请求失败'})`)
    if (alerts.status === 'fulfilled') state.alerts = alerts.value || []
    else failed.push(`告警(${alerts.reason?.message || '请求失败'})`)
    if (stats.status === 'fulfilled') state.stats = stats.value
    else failed.push(`统计(${stats.reason?.message || '请求失败'})`)
    if (failed.length) state.error = `部分数据源加载失败（已展示其余可用数据）：${failed.join('；')}`
    await loadSocLib()
    loadPersonaSchedules() // 拟真账号：独立加载不阻塞首屏，失败仅降级卡片
    enrichGrades() // 不阻塞首屏：分级/话术套路标签就绪后自动上屏
  } catch (e) {
    state.error = e.message
  } finally {
    state.loading = false
    await nextTick() // 空态 v-if 切换完成后再初始化图表
    renderCharts()
  }
}

// ---- ECharts ----
const tagData = computed(() => (state.graph.by_tag || []).map((t) => ({ name: t.name, value: t.value })))
const gradeData = computed(() => (state.graph.by_grade || []).map((g) => ({ name: g.name, value: g.value })))

let tagChart = null
let gradeChart = null
let trendChart = null

function renderCharts() {
  if (tagChart) { tagChart.dispose(); tagChart = null }
  if (gradeChart) { gradeChart.dispose(); gradeChart = null }
  if (trendChart) { trendChart.dispose(); trendChart = null }

  const tagEl = document.getElementById('board-tag-chart')
  if (tagEl && tagData.value.length) {
    tagChart = echarts.init(tagEl)
    tagChart.setOption({
      tooltip: { trigger: 'item' },
      legend: { bottom: 0, type: 'scroll' },
      series: [{ name: '骗术标签', type: 'pie', radius: ['32%', '58%'], data: tagData.value, label: { formatter: '{b}: {c}' } }],
    })
  }

  const gradeEl = document.getElementById('board-grade-chart')
  if (gradeEl && gradeData.value.length) {
    const gradeColors = { L1: '#67c23a', L2: '#67c23a', L3: '#e6a23c', L4: '#f56c6c', L5: '#f56c6c' }
    gradeChart = echarts.init(gradeEl)
    gradeChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      xAxis: { type: 'category', data: gradeData.value.map((g) => g.name) },
      yAxis: { type: 'value', minInterval: 1 },
      series: [{
        name: '案例', type: 'bar', data: gradeData.value.map((g) => g.value),
        itemStyle: { color: (p) => gradeColors[p.name] || '#409eff' }, barWidth: '45%',
      }],
    })
  }

  const trendEl = document.getElementById('board-trend-chart')
  if (trendEl && state.hits.length) {
    const dayCount = {}
    const now = new Date()
    for (let i = 6; i >= 0; i--) {
      const d = new Date(now)
      d.setDate(d.getDate() - i)
      dayCount[d.toISOString().slice(0, 10)] = 0
    }
    for (const h of state.hits) {
      const day = String(h.created_at || '').slice(0, 10)
      if (day in dayCount) dayCount[day] += 1
    }
    const days = Object.keys(dayCount)
    trendChart = echarts.init(trendEl)
    trendChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      xAxis: { type: 'category', data: days.map((d) => d.slice(5)) },
      yAxis: { type: 'value', minInterval: 1 },
      series: [{ name: '检测记录', type: 'line', smooth: true, data: days.map((d) => dayCount[d]), itemStyle: { color: '#409eff' }, areaStyle: { opacity: 0.08 } }],
    })
  }
}

function onResize() {
  tagChart?.resize()
  gradeChart?.resize()
  trendChart?.resize()
}

onMounted(() => {
  loadAll()
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  tagChart?.dispose()
  gradeChart?.dispose()
  trendChart?.dispose()
})

// 案例行点击 → 打开检测详情抽屉（det_id 关联）
function openCaseDetail(row) {
  if (!row?.det_id) return
  detailDrawer.detectionId = row.det_id
  detailDrawer.show = true
}
</script>

<template>
  <div class="af-page" v-loading="state.loading">
    <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 16px" />

    <!-- 统计卡（信息明了：值 + 数据来源） -->
    <div style="display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap">
      <el-card v-for="c in statCards" :key="c.label" shadow="hover" style="flex: 1; min-width: 190px">
        <div style="display: flex; align-items: center; gap: 12px">
          <el-icon :size="28" :color="c.color"><component :is="c.icon" /></el-icon>
          <div>
            <div style="font-size: 22px; font-weight: 700; line-height: 1.2">{{ c.value }}</div>
            <div class="af-muted" style="font-size: 12px">{{ c.label }}</div>
            <div class="af-muted" style="font-size: 11px; opacity: 0.8; margin-top: 2px">数据来自：{{ c.source }}</div>
          </div>
        </div>
      </el-card>
    </div>

    <!-- 三个图表（空态占位） -->
    <el-row :gutter="16" style="margin-bottom: 16px">
      <el-col :span="8">
        <el-card shadow="never" class="af-card">
          <template #header><b>骗术图谱</b><span class="af-muted" style="margin-left: 8px">（数据来自：案例库）</span></template>
          <div v-if="tagData.length" id="board-tag-chart" style="height: 280px" />
          <div v-else style="height: 280px; display: flex; align-items: center; justify-content: center">
            <el-empty description="暂无骗术标签数据" :image-size="60" />
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="af-card">
          <template #header><b>案例分级</b><span class="af-muted" style="margin-left: 8px">（数据来自：案例库）</span></template>
          <div v-if="gradeData.length" id="board-grade-chart" style="height: 280px" />
          <div v-else style="height: 280px; display: flex; align-items: center; justify-content: center">
            <el-empty description="暂无分级数据" :image-size="60" />
          </div>
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never" class="af-card">
          <template #header><b>近 7 天检测趋势</b><span class="af-muted" style="margin-left: 8px">（数据来自：检测记录）</span></template>
          <div v-if="state.hits.length" id="board-trend-chart" style="height: 280px" />
          <div v-else style="height: 280px; display: flex; align-items: center; justify-content: center">
            <el-empty description="暂无检测记录" :image-size="60" />
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 检测速览（按 grade 排序，L4/L5 红色高亮，每行「推理链」按钮） -->
    <el-row :gutter="16" style="margin-bottom: 16px">
      <el-col :span="15">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
              <div>
                <b>检测速览（最近 {{ state.hits.length }} 条）</b>
                <span class="af-muted" style="margin-left: 8px">（数据来自：检测记录）</span>
              </div>
              <div style="display: flex; align-items: center; gap: 8px">
                <el-tag v-if="state.enriching" size="small" type="info" effect="plain">分级增强中…</el-tag>
                <el-button size="small" type="primary" plain @click="router.push({ path: '/sessions', query: { tab: 'hit' } })">会话历史 →</el-button>
              </div>
            </div>
          </template>
          <!-- P1-2 修复：检测速览表移除 fixed="right"（1280px 下固定列重叠 104px 且不可横滚），
               外层 overflow-x:auto + min-width，900px 下可横向滚动查看全部列 -->
          <div style="overflow-x: auto">
            <el-table
              :data="sortedHits"
              size="small"
              max-height="380"
              :row-class-name="hitRowClass"
              v-loading="state.enriching"
              empty-text="暂无检测记录"
              style="min-width: 800px"
              @row-click="openHitDetail"
            >
              <el-table-column prop="id" label="ID" width="60" />
              <el-table-column label="分级" width="80">
                <template #default="{ row }">
                  <el-tag v-if="row.grade" :type="levelType(row.grade)" effect="dark" size="small">{{ row.grade }}</el-tag>
                  <span v-else class="af-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="判定" width="80">
                <template #default="{ row }">
                  <el-tag v-if="row.llm_verdict || row.verdict" :type="verdictMeta(row.llm_verdict || row.verdict)" size="small">{{ row.llm_verdict || row.verdict }}</el-tag>
                  <span v-else class="af-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="规则分" width="76">
                <template #default="{ row }">
                  <span v-if="row.rule_score != null" :class="ruleScoreCls(row.rule_score)">{{ row.rule_score }}</span>
                  <span v-else class="af-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column prop="hit_count" label="命中" width="60" />
              <el-table-column label="话术套路" min-width="120">
                <template #default="{ row }">
                  <el-tag v-for="p in playbooks(row)" :key="p" size="small" :type="playbookType(p)" style="margin-right: 4px">{{ p }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="时间" width="160">
                <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
              </el-table-column>
              <el-table-column label="操作" width="150">
                <template #default="{ row }">
                  <el-button size="small" type="primary" plain @click.stop="openHitDetail(row)">详情</el-button>
                  <el-button size="small" text @click.stop="openHitSession(row)">会话</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
      <el-col :span="9">
        <el-card shadow="never">
          <template #header>
            <b>系统统计</b><span class="af-muted" style="margin-left: 8px">（数据来自：系统统计）</span>
          </template>
          <pre v-if="state.stats" class="af-mono" style="margin: 0; font-size: 12px; white-space: pre-wrap; max-height: 380px; overflow: auto">{{ JSON.stringify(state.stats, null, 2) }}</pre>
          <el-empty v-else description="暂无统计数据" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <!-- 案例明细 -->
    <el-row :gutter="16">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
              <div>
                <b>案例明细（前 {{ state.cases.items.length }} 条）</b>
                <span class="af-muted" style="margin-left: 8px">（数据来自：案例库）</span>
              </div>
              <el-button size="small" type="primary" plain @click="$router.push('/cases')">进入案例库 →</el-button>
            </div>
          </template>
          <el-table :data="state.cases.items" size="small" max-height="320" empty-text="暂无案例数据" highlight-current-row @row-click="openCaseDetail">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="det_id" label="检测ID" width="70" />
            <el-table-column prop="redacted_payload" label="脱敏载荷" show-overflow-tooltip />
            <el-table-column label="标签" width="180">
              <template #default="{ row }">
                <el-tag v-for="t in row.graph_tags || []" :key="t" size="small" style="margin-right: 4px">{{ t }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 拟真账号（定时发布计划 + 立即发布 + 应对骗子 + 实战演练；来源 /persona/schedules · /persona/respond · /persona/drills · /persona/drill） -->
    <el-row :gutter="16">
      <el-col :span="24">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
              <div>
                <b>拟真账号</b>
                <span class="af-muted" style="margin-left: 8px">（数据来自：拟真账号计划）</span>
              </div>
              <el-button size="small" type="primary" plain @click="openPersonaDialog">新建定时发布</el-button>
            </div>
          </template>
          <el-alert v-if="state.persona.error" type="error" :title="state.persona.error" :closable="false" style="margin-bottom: 8px" />
          <!-- 同源修复（P1-2）：拟真账号计划表同样移除 fixed="right"，外层横滚容器保证 900px 可滚动 -->
          <div style="overflow-x: auto">
            <el-table
              :data="state.persona.schedules"
              size="small"
              max-height="280"
              v-loading="state.persona.loading"
              empty-text="暂无发布计划"
              style="min-width: 900px"
            >
              <el-table-column prop="id" label="ID" width="60" />
              <el-table-column prop="account" label="账号" min-width="130" show-overflow-tooltip />
              <el-table-column label="类型" width="90">
                <template #default="{ row }">
                  <el-tag size="small" effect="plain">{{ personaTypeLabel(row.type) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="间隔(分)" width="90">
                <template #default="{ row }">{{ row.interval_minutes ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="进度" width="160">
                <template #default="{ row }">
                  <div style="display: flex; align-items: center; gap: 8px">
                    <span>{{ row.published ?? 0 }} / {{ row.total ?? '—' }}</span>
                    <el-progress
                      v-if="row.total"
                      :percentage="Math.min(100, Math.round(((row.published || 0) / row.total) * 100))"
                      :stroke-width="6"
                      style="width: 90px"
                    />
                  </div>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="290">
                <template #default="{ row }">
                  <el-button size="small" type="warning" plain @click="publishPersonaNow(row)">立即发布</el-button>
                  <el-button size="small" type="primary" plain @click="openRespondDialog(row)">应对骗子</el-button>
                  <el-button size="small" type="primary" plain @click="openDrillDialog(row)">实战演练</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 新建定时发布弹窗 -->
    <el-dialog v-model="state.personaDialog.show" title="新建定时发布" width="460px">
      <el-alert v-if="state.personaDialog.error" type="error" :title="state.personaDialog.error" :closable="false" style="margin-bottom: 10px" />
      <el-form label-width="90px" @submit.prevent>
        <el-form-item label="账号">
          <el-input v-model="state.personaDialog.form.account" placeholder="拟真账号名，如 zhihu_persona_01" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="state.personaDialog.form.type" style="width: 100%">
            <el-option v-for="t in PERSONA_TYPES" :key="t.value" :label="t.label" :value="t.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="间隔(分)">
          <el-input-number v-model="state.personaDialog.form.interval_minutes" :min="1" :max="10080" style="width: 100%" />
        </el-form-item>
        <el-form-item label="总数（条）">
          <el-input-number v-model="state.personaDialog.form.total" :min="1" :max="1000" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="state.personaDialog.show = false">取消</el-button>
        <el-button type="primary" :loading="state.personaDialog.saving" @click="createPersonaSchedule">创建</el-button>
      </template>
    </el-dialog>

    <!-- 应对骗子弹窗（/persona/respond：骗子私信 → 拟真回复 + 推理 + 提取 IOC） -->
    <el-dialog v-model="state.respondDialog.show" title="应对骗子（拟真回复生成）" width="600px">
      <el-alert v-if="state.respondDialog.error" type="error" :title="state.respondDialog.error" :closable="false" style="margin-bottom: 10px" />
      <el-form label-width="86px" @submit.prevent>
        <el-form-item label="拟真账号">
          <el-input v-model="state.respondDialog.form.account" placeholder="拟真账号名（来自发布计划）" />
        </el-form-item>
        <el-form-item label="骗子私信">
          <el-input
            v-model="state.respondDialog.form.incoming_text"
            type="textarea"
            :rows="4"
            placeholder="粘贴骗子发来的私信原文，AI 生成拟真应对回复并提取 IOC"
          />
        </el-form-item>
      </el-form>
      <div v-if="state.respondDialog.result" class="af-result-box">
        <el-alert
          v-if="state.respondDialog.result.matched === true"
          type="warning"
          :title="`识别为诈骗：${state.respondDialog.result.reasoning || '（无推理）'}`"
          :closable="false"
          show-icon
          style="margin-bottom: 10px"
        />
        <el-alert
          v-else-if="state.respondDialog.result.matched === false"
          type="success"
          :title="`未匹配诈骗套路：${state.respondDialog.result.reasoning || '（无推理）'}`"
          :closable="false"
          show-icon
          style="margin-bottom: 10px"
        />
        <div class="af-result-item"><b>拟真回复（reply）</b><pre>{{ state.respondDialog.result.reply || '—' }}</pre></div>
        <div class="af-result-item"><b>推理（reasoning）</b><pre>{{ state.respondDialog.result.reasoning || '—' }}</pre></div>
        <div class="af-result-item">
          <b>提取 IOC（extracted_iocs）</b>
          <div v-if="Array.isArray(state.respondDialog.result.extracted_iocs) && state.respondDialog.result.extracted_iocs.length" style="margin-top: 4px">
            <el-tag
              v-for="(ioc, i) in state.respondDialog.result.extracted_iocs"
              :key="i"
              size="small"
              effect="plain"
              style="margin: 2px 6px 2px 0"
            >{{ typeof ioc === 'string' ? ioc : JSON.stringify(ioc) }}</el-tag>
          </div>
          <pre v-else class="af-muted">{{ state.respondDialog.result.extracted_iocs ? JSON.stringify(state.respondDialog.result.extracted_iocs) : '（无）' }}</pre>
        </div>
      </div>
      <template #footer>
        <el-button @click="state.respondDialog.show = false">关闭</el-button>
        <el-button type="danger" :loading="state.respondDialog.saving" @click="submitRespond">生成应对</el-button>
      </template>
    </el-dialog>

    <!-- 实战演练弹窗（/persona/drills 剧本下拉 + /persona/drill 执行 → 建议回复 + 风险 + 推理） -->
    <el-dialog v-model="state.drillDialog.show" title="实战演练（剧本模拟）" width="640px">
      <el-alert v-if="state.drillDialog.error" type="error" :title="state.drillDialog.error" :closable="false" style="margin-bottom: 10px" />
      <el-form label-width="86px" @submit.prevent>
        <el-form-item label="拟真账号">
          <el-input v-model="state.drillDialog.form.account" placeholder="拟真账号名（来自发布计划）" />
        </el-form-item>
        <el-form-item label="剧本">
          <el-select
            v-model="state.drillDialog.form.scenario"
            style="width: 100%"
            :loading="state.drillDialog.drillsLoading"
            placeholder="选择演练剧本…"
            empty-text="未加载到剧本（接口不可用或暂无剧本）"
            @change="onDrillScenarioChange"
          >
            <el-option v-for="d in state.drillDialog.drills" :key="d.scenario" :value="d.scenario" :label="d.scenario">
              <span style="float: left">{{ d.scenario }}</span>
              <span class="af-muted" style="float: right; font-size: 12px">{{ d.description }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="私信(可选)">
          <el-input
            v-model="state.drillDialog.form.incoming_text"
            type="textarea"
            :rows="3"
            :placeholder="currentDrill && currentDrill.typical_incoming ? `可留空（缺省剧本私信）：${currentDrill.typical_incoming}` : '可留空（缺省使用剧本典型私信）；也可手动改写演练输入'"
          />
        </el-form-item>
        <el-form-item v-if="currentDrill" label="剧本提示">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            style="width: 100%"
            :title="currentDrill.warning || '（无特别警告）'"
          >{{ currentDrill.description }}</el-alert>
        </el-form-item>
      </el-form>
      <div v-if="state.drillDialog.suggestion" class="af-result-box">
        <div class="af-result-item"><b>剧本建议回复（suggested_reply）</b><pre>{{ state.drillDialog.suggestion.suggested_reply || '—' }}</pre></div>
        <div class="af-result-item"><b>风险警告（warning）</b><pre>{{ state.drillDialog.suggestion.warning || '—' }}</pre></div>
        <div class="af-result-item"><b>推理（reasoning）</b><pre>{{ state.drillDialog.suggestion.reasoning || '—' }}</pre></div>
        <div class="af-result-item"><b>IOC 提示（extracted_iocs / iocs_hint）</b><pre class="af-mono">{{ drillIocsText() }}</pre></div>
      </div>
      <template #footer>
        <el-button @click="state.drillDialog.show = false">关闭</el-button>
        <el-button type="primary" :loading="state.drillDialog.saving" @click="submitDrill">开始演练</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉（四要素：告警原因 + 上下文 + 平台跳转 + 详细分析；含社会工程学推理链） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>

<style scoped>
:deep(.el-table .af-row-danger td.el-table__cell) {
  background: #fef0f0 !important;
}
:deep(.el-table .af-row-danger:hover > td.el-table__cell) {
  background: #fde2e2 !important;
}
.af-result-box {
  margin-top: 12px;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 6px;
  padding: 12px;
  background: var(--el-fill-color-lighter, #f5f7fa);
}
.af-result-item {
  margin-bottom: 10px;
}
.af-result-item:last-child {
  margin-bottom: 0;
}
.af-result-item pre {
  margin: 4px 0 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--el-font-family-mono, Consolas, monospace);
  font-size: 12px;
  line-height: 1.6;
}
</style>
