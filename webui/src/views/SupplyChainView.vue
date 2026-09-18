<script setup>
// 供应链（T2 侧边栏⑦）· 双模式：
// ① 账号关联链：/accounts/check 团伙共现 + /accounts/{id}/timeline 踩饵链（原有）
// ② OSINT 供应链反查：输入骗子给出的网址/概念 → /supply-chain/query 轻量起链 +
//    加载 var/supply-chain/ 下 agent workflow 产物（完整多维反查图谱+证据+疑点）。
//    方法论：skills/supply-chain/SKILL.md（顺藤摸瓜 L1-L9）
import { onBeforeUnmount, onMounted, reactive, ref, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { fmtTime } from '../utils/time'

const route = useRoute()

// ---- 模式 ① 账号关联链 ----
const form = reactive({ urlName: '' })
const checking = ref(false)
const error = ref('')
const result = ref(null)
const timeline = ref([])
const timelineError = ref('')

// ---- 模式 ② OSINT 供应链反查 ----
const recon = reactive({ seed: '', mode: 'account' })   // mode: account|recon
const reconRunning = ref(false)
const reconError = ref('')
const reconResult = ref(null)     // /supply-chain/query 返回
const reports = ref([])           // /supply-chain/results 列表
const activeReport = ref(null)    // 当前加载的完整报告
const reportLoading = ref(false)

const riskMeta = {
  green: { type: 'success', label: '绿 · 低风险', color: '#67c23a' },
  yellow: { type: 'warning', label: '黄 · 需关注', color: '#e6a23c' },
  red: { type: 'danger', label: '红 · 高风险', color: '#f56c6c' },
}
const nodeColor = { domain: '#409eff', ip: '#67c23a', email: '#e6a23c', account: '#f56c6c', payment: '#b23b3b', concept: '#909399' }
const riskColor = { high: '#f56c6c', medium: '#e6a23c', low: '#909399' }

let graphChart = null

// ================= 模式①：账号关联链 =================
async function doCheck(name) {
  const urlName = (name ?? form.urlName).trim()
  if (!urlName) {
    ElMessage.warning('请输入知乎 url_token / 昵称')
    return
  }
  checking.value = true
  error.value = ''
  timeline.value = []
  resetRecon()
  try {
    result.value = await api.accountCheck(urlName)
    form.urlName = urlName
    loadTimeline(result.value.account_id)
    await nextTick()
    renderGraph()
  } catch (e) {
    error.value = e.message
    result.value = null
  } finally {
    checking.value = false
  }
}

async function loadTimeline(accountId) {
  timelineError.value = ''
  try {
    timeline.value = (await api.accountTimeline(accountId)) || []
  } catch (e) {
    timelineError.value = e.message
    timeline.value = []
  }
}

function renderGraph() {
  if (!result.value) return
  const r = result.value
  const nodes = []
  const edges = []
  const seen = new Set()

  const centerId = `acc-${r.account_id}`
  nodes.push({
    id: centerId, name: r.url_name, symbolSize: 42,
    itemStyle: { color: riskMeta[r.risk_level]?.color || '#909399' },
    category: 0, label: { show: true, fontSize: 12, fontWeight: 'bold' },
  })
  seen.add(centerId)

  for (const c of r.co_occurring_accounts || []) {
    const cid = `co-${typeof c === 'string' ? c : c.url_name}`
    if (!seen.has(cid)) {
      seen.add(cid)
      nodes.push({
        id: cid, name: typeof c === 'string' ? c : c.url_name, symbolSize: 28,
        itemStyle: { color: '#e6a23c' }, category: 1,
      })
      edges.push({ source: centerId, target: cid, label: { show: true, formatter: '共现', fontSize: 10 } })
    }
  }

  for (const t of timeline.value) {
    if (t.kind !== 'trap_hit') continue
    const m = String(t.note || '').match(/蜜饵#(\d+)/)
    const trapId = m ? m[1] : `t-${t.ts}`
    const tid = `trap-${trapId}`
    if (!seen.has(tid)) {
      seen.add(tid)
      nodes.push({
        id: tid, name: `蜜饵 #${trapId}`, symbolSize: 22,
        itemStyle: { color: '#f56c6c' }, category: 2,
      })
      edges.push({ source: centerId, target: tid, label: { show: true, formatter: '踩饵', fontSize: 10 } })
    }
  }

  const el = document.getElementById('sc-graph')
  if (!el) {
    if (graphChart) { graphChart.dispose(); graphChart = null }
    return
  }
  if (graphChart) graphChart.dispose()
  graphChart = echarts.init(el)
  graphChart.setOption({
    tooltip: { formatter: (p) => `${p.data.name || ''}<br/>${p.data.category === 0 ? '中心账号' : p.data.category === 1 ? '共现账号（团伙）' : '关联蜜饵（踩饵）'}` },
    legend: { bottom: 0, data: ['中心账号', '共现账号', '蜜饵'], textStyle: { fontSize: 11 } },
    series: [{
      type: 'graph', layout: 'force', data: nodes, edges,
      categories: [
        { name: '中心账号', itemStyle: { color: '#409eff' } },
        { name: '共现账号', itemStyle: { color: '#e6a23c' } },
        { name: '蜜饵', itemStyle: { color: '#f56c6c' } },
      ],
      roam: true, draggable: true, force: { repulsion: 320, edgeLength: 110 },
      label: { show: true, fontSize: 11 },
      lineStyle: { color: '#b3c0d1', width: 1.5, curveness: 0.1 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
    }],
  })
}

// ================= 模式②：OSINT 供应链反查 =================
function resetRecon() {
  reconError.value = ''
  reconResult.value = null
  activeReport.value = null
}

async function doRecon(seedValue) {
  const seed = (seedValue ?? recon.seed).trim()
  if (!seed) {
    ElMessage.warning('请输入要反查的网址 / 域名 / 概念')
    return
  }
  reconRunning.value = true
  reconError.value = ''
  result.value = null
  try {
    reconResult.value = await api.supplyChainQuery(seed)
    recon.seed = seed
    await nextTick()
    renderReconGraph(reconResult.value.graph.nodes, reconResult.value.graph.edges)
  } catch (e) {
    reconError.value = e.message
    reconResult.value = null
  } finally {
    reconRunning.value = false
  }
}

async function loadReports() {
  try {
    reports.value = (await api.supplyChainResults()) || []
  } catch { reports.value = [] }
}

async function openReport(name) {
  if (!name) return
  reportLoading.value = true
  reconError.value = ''
  try {
    activeReport.value = await api.supplyChainResult(name)
    result.value = null
    await nextTick()
    renderReconGraph(activeReport.value.graph.nodes, activeReport.value.graph.edges)
  } catch (e) {
    reconError.value = e.message
    activeReport.value = null
  } finally {
    reportLoading.value = false
  }
}

function renderReconGraph(nodes, edges) {
  const el = document.getElementById('sc-graph')
  if (!el) {
    if (graphChart) { graphChart.dispose(); graphChart = null }
    return
  }
  if (graphChart) graphChart.dispose()
  const data = (nodes || []).map((n) => ({
    id: n.id, name: n.name,
    symbolSize: n.risk === 'high' ? 40 : n.risk === 'medium' ? 30 : 20,
    itemStyle: { color: riskColor[n.risk] || '#909399' },
    label: { show: true, fontSize: 11, formatter: (p) => (String(p.name || '').length > 14 ? `${String(p.name).slice(0, 13)}…` : p.name) },
    layer: n.layer, type: n.type, risk: n.risk,
  }))
  const links = (edges || []).map((e) => ({
    source: e.source, target: e.target,
    label: { show: true, formatter: e.rel, fontSize: 9 },
    lineStyle: { width: e.rel === 'fund' ? 2.5 : 1.5 },
  }))
  const typeCats = Array.from(new Set((nodes || []).map((n) => n.type || 'domain')))
  graphChart = echarts.init(el)
  graphChart.setOption({
    tooltip: {
      formatter: (p) => {
        const d = p.data
        return `${d.name || ''}<br/>类型: ${d.type || ''} · 层: ${d.layer || ''}<br/>风险: ${d.risk || '-'}`
      },
    },
    legend: { bottom: 0, data: typeCats, textStyle: { fontSize: 11 } },
    series: [{
      type: 'graph', layout: 'force', data, edges: links,
      categories: typeCats.map((t) => ({ name: t, itemStyle: { color: nodeColor[t] || '#909399' } })),
      roam: true, draggable: true, force: { repulsion: 380, edgeLength: [80, 160] },
      lineStyle: { color: '#b3c0d1', curveness: 0.1 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
    }],
  })
}

function onResize() {
  graphChart?.resize()
}

onMounted(() => {
  window.addEventListener('resize', onResize)
  loadReports()
  const q = route.query.url_name
  if (q) doCheck(String(q))
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  graphChart?.dispose()
})

const levelType = (l) => ({ L3: 'warning', L4: 'danger', L5: 'danger' }[l] || 'info')
</script>

<template>
  <div class="af-page">
    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <b>供应链</b>
          <el-radio-group v-model="recon.mode" size="small">
            <el-radio-button value="account">账号关联链</el-radio-button>
            <el-radio-button value="recon">OSINT 反查（顺藤摸瓜）</el-radio-button>
          </el-radio-group>
          <span class="af-muted" style="font-size: 12px">
            {{ recon.mode === 'recon' ? '输入骗子/目标给出的网址或概念，沿域名→IP→WHOIS→证书→子域→账号→资金反向摸出整条供应链' : '/accounts/check 团伙共现 + timeline 踩饵链' }}
          </span>
        </div>
      </template>

      <!-- 模式①：账号关联链输入 -->
      <div v-if="recon.mode === 'account'" style="display: flex; gap: 8px; max-width: 640px">
        <el-input v-model="form.urlName" placeholder="输入知乎 url_token / 昵称，查看其供应链式关联" @keyup.enter="doCheck()" />
        <el-button type="primary" :loading="checking" @click="doCheck()">查询关联链</el-button>
      </div>

      <!-- 模式②：OSINT 反查输入 + 加载报告 -->
      <div v-else style="display: flex; gap: 8px; flex-wrap: wrap; max-width: 960px">
        <el-input v-model="recon.seed" placeholder="输入诈骗网址 / 域名 / 概念，如 https://xxx.com/invest 或 wxid_xxx" style="flex: 1; min-width: 320px" @keyup.enter="doRecon()" />
        <el-button type="primary" :loading="reconRunning" @click="doRecon()">顺藤摸瓜反查 🔍</el-button>
        <el-select v-model="activeReport" placeholder="加载已有反查报告（agent workflow 产物）" style="width: 260px" filterable @change="openReport(activeReport?.name)">
          <el-option v-for="r in reports" :key="r.name" :label="`${r.seed?.slice(0, 30) || r.name}（${r.nodes}节点/${r.evidence_total}证据）`" :value="r" />
        </el-select>
        <el-button size="small" plain @click="loadReports">刷新报告列表</el-button>
      </div>
    </el-card>

    <el-alert v-if="error || reconError" type="error" :title="error || reconError" :closable="false" style="margin-bottom: 16px" />

    <!-- 反查轻量结果提示 -->
    <el-alert v-if="reconResult && reconResult.hint === 'agent_workflow'" type="info" :title="`${reconResult.domain ? '本地起链完成' : '概念词已建根节点'}：完整多维 OSINT 请在命令输入（内嵌 DSH）中用 supply-chain skill + workflow 深挖，结果将出现在下方报告列表`" :closable="false" style="margin-bottom: 12px" />

    <!-- 工作流完整报告 -->
    <template v-if="activeReport">
      <el-card shadow="never" class="af-card">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <b>反查报告：{{ activeReport.seed }}</b>
            <el-tag size="small" type="info">{{ activeReport.nodes }} 节点 / {{ activeReport.edges }} 边</el-tag>
            <el-tag size="small" type="warning">{{ activeReport.evidence_counts?.total || 0 }} 条证据</el-tag>
            <span class="af-muted" style="font-size: 12px">{{ activeReport.run_at }}</span>
          </div>
        </template>
        <el-card shadow="never" style="margin-bottom: 12px">
          <b>摘要</b>
          <p class="af-desc">{{ activeReport.summary }}</p>
        </el-card>
        <div id="sc-graph" style="height: 440px; width: 100%" />
        <div class="af-muted" style="margin-top: 4px">节点大小=风险（高/中/低），连线类型：resolve 解析 / whois 注册 / cooccur 共现 / similar 相似 / fund 资金；可拖拽缩放，悬停看详情。</div>
      </el-card>

      <el-row :gutter="16">
        <el-col :span="12">
          <el-card shadow="never" class="af-card">
            <template #header><b>证据链</b><span class="af-muted" style="margin-left: 8px">（每层反查产出）</span></template>
            <el-table :data="Object.entries(activeReport.evidence_counts || {}).map(([k, v]) => ({ layer: k, count: v }))" size="small">
              <el-table-column prop="layer" label="层" />
              <el-table-column prop="count" label="证据数" width="90" />
            </el-table>
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never" class="af-card">
            <template #header><b>疑点信号</b><span class="af-muted" style="margin-left: 8px">（{{ (activeReport.suspicion_signals || []).length }}）</span></template>
            <el-timeline>
              <el-timeline-item v-for="(s, i) in activeReport.suspicion_signals || []" :key="i" type="danger">
                {{ s }}
              </el-timeline-item>
              <el-timeline-item v-if="!(activeReport.suspicion_signals || []).length" type="info">暂无</el-timeline-item>
            </el-timeline>
          </el-card>
        </el-col>
      </el-row>

      <el-card v-if="(activeReport.next_steps || []).length" shadow="never" class="af-card">
        <template #header><b>下一步深挖建议</b></template>
        <el-timeline>
          <el-timeline-item v-for="(s, i) in activeReport.next_steps" :key="i" type="primary">{{ s }}</el-timeline-item>
        </el-timeline>
      </el-card>
    </template>

    <!-- 轻量起链结果 -->
    <template v-else-if="reconResult && reconResult.mode === 'local_recon'">
      <el-card shadow="never" class="af-card">
        <template #header>
          <div style="display: flex; align-items: center; gap: 10px">
            <b>起链图谱：{{ reconResult.domain }}</b>
            <el-tag size="small" :type="reconResult.signals.length ? 'danger' : 'info'">{{ reconResult.signals.length }} 个疑点信号</el-tag>
          </div>
        </template>
        <div id="sc-graph" style="height: 380px; width: 100%" />
      </el-card>
      <el-card v-if="reconResult.evidence?.length" shadow="never" class="af-card">
        <template #header><b>证据（{{ reconResult.evidence.length }}）</b></template>
        <el-table :data="reconResult.evidence" size="small">
          <el-table-column prop="layer" label="层" width="130" />
          <el-table-column prop="type" label="类型" width="140" />
          <el-table-column prop="value" label="值" show-overflow-tooltip />
          <el-table-column prop="note" label="说明" show-overflow-tooltip />
        </el-table>
      </el-card>
    </template>

    <!-- 账号关联链结果（原有） -->
    <template v-else-if="result">
      <el-card shadow="never" class="af-card">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
            <b>画像：#{{ result.account_id }} · {{ result.url_name }}</b>
            <el-tag :type="riskMeta[result.risk_level]?.type || 'info'" size="small">
              {{ riskMeta[result.risk_level]?.label || result.risk_level }}
            </el-tag>
            <span class="af-muted">风险分 {{ result.score }}</span>
            <el-tag type="warning" effect="plain" size="small">共现 {{ (result.co_occurring_accounts || []).length }} 账号</el-tag>
            <el-tag type="danger" effect="plain" size="small">踩饵 {{ timeline.filter((t) => t.kind === 'trap_hit').length }} 条</el-tag>
          </div>
        </template>
        <div id="sc-graph" style="height: 380px; width: 100%" />
        <div class="af-muted" style="margin-top: 4px">节点：中心账号（蓝）→ 共现账号（橙，疑似团伙）→ 蜜饵（红，踩饵命中）；可拖拽/缩放。</div>
      </el-card>

      <el-row :gutter="16">
        <el-col :span="12">
          <el-card shadow="never" class="af-card">
            <template #header><b>证据链</b><span class="af-muted" style="margin-left: 8px">（{{ (result.evidence || []).length }} 条）</span></template>
            <el-table :data="result.evidence || []" size="small">
              <el-table-column prop="signal" label="信号" width="200" />
              <el-table-column prop="weight" label="权重" width="70" />
              <el-table-column prop="note" label="说明" show-overflow-tooltip />
            </el-table>
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card shadow="never">
            <template #header><b>账号时间线</b><span class="af-muted" style="margin-left: 8px">（画像更新 + 踩饵命中）</span></template>
            <el-alert v-if="timelineError" type="error" :title="timelineError" :closable="false" style="margin-bottom: 8px" />
            <el-table :data="timeline" size="small" max-height="300">
              <el-table-column label="时间" width="170">
                <template #default="{ row }">{{ fmtTime(row.ts) }}</template>
              </el-table-column>
              <el-table-column label="类型" width="110">
                <template #default="{ row }">
                  <el-tag :type="row.kind === 'trap_hit' ? 'danger' : 'info'" size="small">{{ row.kind === 'trap_hit' ? '踩饵命中' : '画像更新' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="note" label="说明" show-overflow-tooltip />
            </el-table>
          </el-card>
        </el-col>
      </el-row>
    </template>
    <el-empty v-else-if="!checking && !reconRunning && !reportLoading" :description="recon.mode === 'account' ? '输入账号开始查询供应链关联链' : '输入网址/概念开始顺藤摸瓜反查，或加载已有报告'" />
  </div>
</template>

<style scoped>
.af-desc { margin: 4px 0 0; font-size: 13px; color: #606266; line-height: 1.6; }
</style>