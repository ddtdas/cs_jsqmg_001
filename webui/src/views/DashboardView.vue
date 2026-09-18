<script setup>
// 仪表盘：统计卡片 + 趋势 ECharts + 五级分布 + 最近事件（全部来自真实 API 聚合，
// 对齐 P7 DoD「Dashboard 图表真实数据」。/system/stats 为后端占位，聚合端点兜底。）
import { onMounted, onBeforeUnmount, reactive, computed, nextTick } from 'vue'
import * as echarts from 'echarts'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

const detailDrawer = reactive({ show: false, detectionId: null })

const state = reactive({
  traps: [],
  hits: [],
  alerts: [],
  cases: { total: 0, items: [] },
  graph: { by_tag: [], by_grade: [], total: 0 },
  stats: null,
  levels: [],
  loading: true,
  error: '',
})

const statCards = computed(() => {
  const hitTotal = state.hits.reduce((s, h) => s + (h.hit_count || 0), 0)
  return [
    { label: '蜜饵', value: state.traps.length, icon: 'Aim', color: '#409eff' },
    { label: '检测记录', value: state.hits.length, icon: 'Search', color: '#67c23a' },
    { label: '话术命中条数', value: hitTotal, icon: 'Flag', color: '#e6a23c' },
    { label: '告警(L3+)', value: state.alerts.length, icon: 'Warning', color: '#f56c6c' },
    { label: '案例', value: state.cases.total, icon: 'Files', color: '#909399' },
  ]
})

// 最近事件：按时间倒序聚合 traps/hits/alerts 的真实变更
const recentEvents = computed(() => {
  const evts = []
  for (const t of state.traps) {
    evts.push({ time: t.created_at, kind: 'trap', level: t.status, text: `蜜饵 #${t.id} 状态=${t.status}`, payload: t.bait_text, detId: null })
  }
  for (const h of state.hits) {
    evts.push({ time: h.created_at, kind: 'hit', level: h.rule_score >= 10 ? 'L3+' : 'info', text: `检测 #${h.id} 规则分=${h.rule_score} 命中=${h.hit_count}`, payload: `source=${h.source}`, detId: h.id })
  }
  for (const a of state.alerts) {
    evts.push({ time: a.created_at, kind: 'alert', level: a.level, text: `告警 #${a.id} ${a.level}`, payload: a.payload?.grade || '', detId: a.payload?.detection_id || null })
  }
  return evts.sort((a, b) => (b.time || '').localeCompare(a.time || '')).slice(0, 12)
})

let trendChart = null
let gradeChart = null

async function loadAll() {
  state.loading = true
  state.error = ''
  try {
    const [traps, hits, alerts, cases, graph, levels, stats] = await Promise.allSettled([
      api.listTraps(),
      api.scanHits(50),
      api.listAlerts({ limit: 100 }),
      api.listCases(1, 100),
      api.caseGraph(),
      api.gradingLevels(),
      api.stats(),
    ])
    // 审查预检：allSettled 的 rejected 不再静默吞掉——逐项收集失败原因并展示
    const failed = []
    if (traps.status === 'fulfilled') state.traps = traps.value || []
    else failed.push(`蜜饵(${traps.reason?.message || '请求失败'})`)
    if (hits.status === 'fulfilled') state.hits = hits.value || []
    else failed.push(`检测记录(${hits.reason?.message || '请求失败'})`)
    if (alerts.status === 'fulfilled') state.alerts = alerts.value || []
    else failed.push(`告警(${alerts.reason?.message || '请求失败'})`)
    if (cases.status === 'fulfilled') state.cases = cases.value || { total: 0, items: [] }
    else failed.push(`案例(${cases.reason?.message || '请求失败'})`)
    if (graph.status === 'fulfilled') state.graph = graph.value || { by_tag: [], by_grade: [], total: 0 }
    else failed.push(`图谱(${graph.reason?.message || '请求失败'})`)
    if (levels.status === 'fulfilled') state.levels = levels.value || []
    else failed.push(`分级(${levels.reason?.message || '请求失败'})`)
    if (stats.status === 'fulfilled') state.stats = stats.value
    else failed.push(`统计(${stats.reason?.message || '请求失败'})`)
    if (failed.length) {
      state.error = `部分数据源加载失败（已展示其余可用数据）：${failed.join('；')}`
    }
  } catch (e) {
    state.error = e.message
  } finally {
    state.loading = false
    // 图表容器位于 <template v-if="!state.loading"> 内：必须先等 DOM 渲染出容器再初始化，
    // 否则 echarts.init(null) 会抛 "Cannot read properties of null (reading 'getAttribute')"。
    await nextTick()
    renderCharts()
  }
}

// 趋势 + 五级分布两个图表：容器存在才初始化（空值保护），避免 getElementById 返回 null 时崩溃
function renderCharts() {
  // 趋势：近 7 天检测记录分布（按 created_at 日期聚合，真实数据）
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

  const trendEl = document.getElementById('trend-chart')
  if (trendEl) {
    if (trendChart) trendChart.dispose()
    trendChart = echarts.init(trendEl)
    trendChart.setOption({
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      xAxis: { type: 'category', data: days.map((d) => d.slice(5)) },
      yAxis: { type: 'value', minInterval: 1 },
      series: [{ name: '检测记录', type: 'bar', data: days.map((d) => dayCount[d]), itemStyle: { color: '#409eff' }, barWidth: '55%' }],
    })
  } else if (trendChart) {
    trendChart.dispose()
    trendChart = null
  }

  // 五级分布：告警级别计数（真实）；告警为空时用案例 by_grade
  let dist = {}
  for (const a of state.alerts) dist[a.level] = (dist[a.level] || 0) + 1
  let usedSource = '告警级别'
  if (Object.keys(dist).length === 0 && state.graph.by_grade?.length) {
    usedSource = '案例分级'
    dist = {}
    for (const g of state.graph.by_grade) dist[g.name] = g.value
  }
  const names = Object.keys(dist)

  const gradeEl = document.getElementById('grade-chart')
  if (gradeEl) {
    if (gradeChart) gradeChart.dispose()
    gradeChart = echarts.init(gradeEl)
    gradeChart.setOption({
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      series: [{
        name: usedSource,
        type: 'pie',
        radius: ['38%', '62%'],
        data: names.map((n) => ({ name: n, value: dist[n] })),
        label: { formatter: '{b}: {c}' },
      }],
    })
  } else if (gradeChart) {
    gradeChart.dispose()
    gradeChart = null
  }
}

function onResize() {
  trendChart?.resize()
  gradeChart?.resize()
}

onMounted(() => {
  loadAll()
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  trendChart?.dispose()
  gradeChart?.dispose()
})

function kindTag(k) {
  return { trap: 'info', hit: 'success', alert: 'danger' }[k] || 'info'
}
function levelTag(level) {
  return { L3: 'warning', L4: 'danger', L5: 'danger' }[level] || 'info'
}

// 最近事件行点击 → 打开检测详情抽屉（hit/alert 有 detId）
function openEventDetail(row) {
  if (!row?.detId) return
  detailDrawer.detectionId = row.detId
  detailDrawer.show = true
}
</script>

<template>
  <div class="af-page" v-loading="state.loading">
    <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 16px" />
    <template v-if="!state.loading">
    <el-row :gutter="16">
      <el-col v-for="c in statCards" :key="c.label" :span="4" style="margin-bottom: 16px">
        <el-card shadow="hover">
          <div style="display: flex; align-items: center; gap: 12px">
            <el-icon :size="30" :color="c.color"><component :is="c.icon" /></el-icon>
            <div>
              <div style="font-size: 22px; font-weight: 700">{{ c.value }}</div>
              <div class="af-muted">{{ c.label }}</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :span="14">
        <el-card class="af-card" shadow="never">
          <template #header><b>近 7 天检测记录趋势</b><span class="af-muted" style="margin-left: 8px">（/scan/hits 真实数据）</span></template>
          <div id="trend-chart" style="height: 300px" />
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card class="af-card" shadow="never">
          <template #header><b>五级分布</b><span class="af-muted" style="margin-left: 8px">（/alerts + /cases/graph 真实数据）</span></template>
          <div id="grade-chart" style="height: 300px" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header><b>最近事件</b><span class="af-muted" style="margin-left: 8px">（traps/hits/alerts 聚合，倒序 12 条）</span></template>
          <el-table :data="recentEvents" size="small" max-height="360" highlight-current-row @row-click="openEventDetail">
            <el-table-column prop="time" label="时间" width="160" />
            <el-table-column label="类型" width="110">
              <template #default="{ row }"><el-tag :type="kindTag(row.kind)" size="small">{{ row.kind }}</el-tag></template>
            </el-table-column>
            <el-table-column label="级别" width="80">
              <template #default="{ row }"><el-tag :type="levelTag(row.level)" size="small">{{ row.level }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="text" label="事件" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <b>分级档位</b>
            <span class="af-muted" style="margin-left: 8px">（/grading/levels）</span>
          </template>
          <el-table :data="state.levels" size="small" max-height="360">
            <el-table-column prop="level" label="级别" width="70" />
            <el-table-column prop="desc" label="定义" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-collapse v-if="state.stats" style="margin-top: 8px">
      <el-collapse-item title="原始 /system/stats（后端当前占位返回，见 ops-center 说明）">
        <pre class="af-mono" style="margin: 0">{{ JSON.stringify(state.stats, null, 2) }}</pre>
      </el-collapse-item>
    </el-collapse>
    </template>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>