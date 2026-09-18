<script setup>
// 知识库蒸馏管理页（知识库蒸馏）：条目列表（分页 + ktype/keyword/min_harm/sort 过滤）+ 高危害 Top5
// + 检索 + 三种导入通道（单条表单 / 批量 JSON / 大文本 import-text，展示蒸馏结果含 weighted_score）
// + 行操作：调分 / 复核 / 蒸馏成语条 / 下架 / 删除（敏感操作 confirm+reason 审计）。
// 契约（后端 A 队并行实现）：GET /knowledge → {items,total}；GET /search?q=；POST /import、/import-text；
// POST /{id}/harm（{delta}）、/{id}/verified（confirm+reason）、/{id}/to-pattern（confirm+reason+category）、
// /{id}/disable（confirm）、DELETE /{id}（confirm+reason）。
// 加权：weighted_score = harm_score × decay_weight（时间衰减，每日 job_knowledge_decay 重算落库）。
// 防御式：全请求 try/catch，端点未就绪时页面给出错误态不崩；加载/错误/空态齐全。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'

// ---------- ktype 元信息（6 类中文，对齐设计文档 §二；兼容后端可能返回的英文枚举） ----------
const KTYPE_OPTIONS = [
  { value: 'scam_pattern', label: '骗术模式' },
  { value: '新骗术', label: '新骗术' },
  { value: '平台漏洞', label: '平台漏洞' },
  { value: '法律', label: '法律' },
  { value: '线索', label: '线索' },
  { value: '案例复盘', label: '案例复盘' },
]
const KTYPE_LABELS = {
  scam_pattern: '骗术模式', new_scam: '新骗术', platform_vuln: '平台漏洞', law: '法律', lead: '线索', case_review: '案例复盘',
  '骗术模式': '骗术模式', '新骗术': '新骗术', '平台漏洞': '平台漏洞', '法律': '法律', '线索': '线索', '案例复盘': '案例复盘', '其他': '其他',
}
const KTYPE_TAGS = {
  scam_pattern: 'info', '骗术模式': 'info',
  new_scam: 'danger', '新骗术': 'danger',
  platform_vuln: 'warning', '平台漏洞': 'warning',
  law: 'success', '法律': 'success',
  lead: 'warning', '线索': 'warning',
  case_review: 'info', '案例复盘': 'info',
}
function ktypeLabel(v) {
  return KTYPE_LABELS[v] || v || '—'
}
function ktypeTag(v) {
  return KTYPE_TAGS[v] || 'info'
}

// 来源中文
const SOURCE_LABELS = {
  manual: '人工', import: '导入', case: '案例', collector: '采集', web: '网络',
}
function sourceLabel(s) {
  return SOURCE_LABELS[s] || s || '—'
}

// 综合分 ≥7 高亮红色
function weightedClass(w) {
  return Number(w) >= 7 ? 'text-danger' : Number(w) >= 5 ? 'text-warning' : 'af-muted'
}
function weightedTag(w) {
  const n = Number(w) || 0
  if (n >= 7) return { type: 'danger', text: n.toFixed(1) }
  if (n >= 5) return { type: 'warning', text: n.toFixed(1) }
  return { type: 'info', text: n.toFixed(1) }
}

// tags/keywords 可能是数组或 JSON 字符串 / 逗号串，统一为数组
function toArr(v) {
  if (v == null || v === '') return []
  if (Array.isArray(v)) return v
  if (typeof v === 'string') {
    const t = v.trim()
    if (!t) return []
    if (t.startsWith('[')) {
      try {
        const a = JSON.parse(t)
        return Array.isArray(a) ? a : []
      } catch {
        return [t]
      }
    }
    return t.split(/[,，;；]/).map((s) => s.trim()).filter(Boolean)
  }
  return []
}

function fmtTime(t) {
  if (!t) return '—'
  const s = String(t).replace('T', ' ').replace('Z', '')
  return s.length > 19 ? s.slice(0, 19) : s
}

// ---------- 列表加载（分页 + 过滤） ----------
const items = ref([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const page = ref(1)
const pageSize = ref(20)
const ktype = ref('')
const keyword = ref('')
const minHarm = ref(null) // null = 不限
const sort = ref('weighted_score')

const SORT_OPTIONS = [
  { value: 'weighted_score', label: '综合分（默认）' },
  { value: 'harm_score', label: '危害分' },
  { value: 'created_at', label: '最新' },
]

function buildQuery() {
  const qs = new URLSearchParams()
  qs.set('page', String(page.value))
  qs.set('page_size', String(pageSize.value))
  if (ktype.value) qs.set('ktype', ktype.value)
  if ((keyword.value || '').trim()) qs.set('keyword', keyword.value.trim())
  if (minHarm.value != null) qs.set('min_harm', String(minHarm.value))
  if (sort.value) qs.set('sort', sort.value)
  return '?' + qs.toString()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.knowledgeList(buildQuery())
    const data = res?.data || res || {}
    items.value = Array.isArray(data.items) ? data.items : Array.isArray(data) ? data : []
    total.value = Number(data.total != null ? data.total : items.value.length)
  } catch (e) {
    error.value = e.message
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function resetFilters() {
  ktype.value = ''
  keyword.value = ''
  minHarm.value = null
  sort.value = 'weighted_score'
  page.value = 1
  load()
}

// ---------- 顶部统计：总条数 + 高危害（weighted_score≥7）Top5 ----------
const stats = ref({ total: 0, highHarmTotal: 0 })
const top5 = ref([])
const top5Loading = ref(false)

async function loadStats() {
  top5Loading.value = true
  try {
    // 高危害总数（min_harm=7，page_size=1 只取 total）
    let highHarmTotal = 0
    try {
      const r1 = await api.knowledgeList('?page=1&page_size=1&min_harm=7')
      const d1 = r1?.data || r1 || {}
      highHarmTotal = Number(d1.total != null ? d1.total : 0)
    } catch {
      highHarmTotal = 0
    }
    // 高危害 Top5（按加权分排序）
    let top = []
    try {
      const r2 = await api.knowledgeList('?page=1&page_size=5&min_harm=7&sort=weighted_score')
      const d2 = r2?.data || r2 || {}
      top = Array.isArray(d2.items) ? d2.items : []
    } catch {
      top = []
    }
    stats.value = { total: total.value, highHarmTotal }
    top5.value = top
  } finally {
    top5Loading.value = false
  }
}

// ---------- 检索（/knowledge/search?q=） ----------
const searchQ = ref('')
const searchResults = ref([])
const searching = ref(false)
const searchError = ref('')
const searchDone = ref(false)

async function doSearch() {
  const q = (searchQ.value || '').trim()
  if (!q) {
    ElMessage.warning('请输入检索关键词')
    return
  }
  searching.value = true
  searchError.value = ''
  searchDone.value = false
  try {
    const res = await api.knowledgeSearch(encodeURIComponent(q))
    const r = res?.data || res || {}
    searchResults.value = Array.isArray(res) ? res : Array.isArray(r.items) ? r.items : Array.isArray(r) ? r : []
    searchDone.value = true
    if (!searchResults.value.length) ElMessage.info('未检索到相关条目')
  } catch (e) {
    searchError.value = e.message
    searchResults.value = []
    searchDone.value = true
  } finally {
    searching.value = false
  }
}

// ---------- 导入（三种通道：单条表单 / 批量 JSON / 大文本） ----------
const importDialog = reactive({ show: false, tab: 'single', busy: false })
const singleForm = reactive({ subject: '', content: '', ktype: '新骗术', harm: null, source: 'manual' })
const jsonText = ref('')
const textInput = reactive({ text: '', source: 'import' })

const importResult = ref(null) // {imported, distilled:[...], message?}

const IMPORT_JSON_EXAMPLE = JSON.stringify(
  {
    items: [
      { subject: '示例：新型仿冒 App 骗局', content: '骗子诱导下载仿冒 App 并充值，提现时以流水不足为由拒绝', ktype: '新骗术', harm: 8 },
      { subject: '示例：冒充客服要求屏幕共享', content: '冒充平台客服，以理赔为由诱导开启屏幕共享骗取验证码', ktype: '新骗术', harm: 7 },
    ],
  },
  null,
  2
)

function openImport() {
  importResult.value = null
  jsonText.value = ''
  textInput.text = ''
  textInput.source = 'import'
  importDialog.tab = 'single'
  importDialog.show = true
}

function fillJsonExample() {
  jsonText.value = IMPORT_JSON_EXAMPLE
}

// 蒸馏结果行统一展示
function normalizeDistilled(rows) {
  return Array.isArray(rows) ? rows : []
}

async function doImportSingle() {
  const subject = (singleForm.subject || '').trim()
  const content = (singleForm.content || '').trim()
  if (!subject) {
    ElMessage.warning('主题（subject）必填')
    return
  }
  if (!content) {
    ElMessage.warning('内容（content）必填')
    return
  }
  importDialog.busy = true
  importResult.value = null
  try {
    const payload = { items: [{ subject, content, ktype: singleForm.ktype }] }
    if (singleForm.harm != null) payload.items[0].harm = Number(singleForm.harm)
    if (singleForm.source) payload.items[0].source = singleForm.source
    const res = await api.knowledgeImport(payload)
    const r = res?.result || res || {}
    importResult.value = {
      imported: r.imported != null ? r.imported : 1,
      distilled: normalizeDistilled(r.distilled),
      message: r.message || '',
    }
    ElMessage.success(`已导入并蒸馏 ${importResult.value.imported} 条`)
    load()
    loadStats()
  } catch (e) {
    importResult.value = null
    ElMessage.error(`导入失败：${e.message}`)
  } finally {
    importDialog.busy = false
  }
}

async function doImportJson() {
  const raw = (jsonText.value || '').trim()
  if (!raw) {
    ElMessage.warning('请粘贴要导入的 JSON')
    return
  }
  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch (e) {
    ElMessage.error(`JSON 解析失败：${e.message}`)
    return
  }
  const items = Array.isArray(parsed) ? parsed : parsed?.items
  if (!Array.isArray(items) || items.length === 0) {
    ElMessage.warning('JSON 需为数组或 {"items":[...]}，且至少 1 条')
    return
  }
  for (let i = 0; i < items.length; i++) {
    const it = items[i] || {}
    if (!it.subject || !String(it.subject).trim()) {
      ElMessage.warning(`第 ${i + 1} 条缺少 subject（必填）`)
      return
    }
    if (!it.content || !String(it.content).trim()) {
      ElMessage.warning(`第 ${i + 1} 条缺少 content（必填）`)
      return
    }
    if (it.harm != null && (Number(it.harm) < 1 || Number(it.harm) > 10)) {
      ElMessage.warning(`第 ${i + 1} 条 harm 需在 1-10 之间`)
      return
    }
  }
  importDialog.busy = true
  importResult.value = null
  try {
    const normalized = items.map((it) => ({
      subject: String(it.subject).trim(),
      content: String(it.content).trim(),
      ktype: it.ktype || singleForm.ktype || '新骗术',
      harm: it.harm != null ? Number(it.harm) : undefined,
      source: (it.source || 'import') || singleForm.source,
    }))
    const res = await api.knowledgeImport({ items: normalized })
    const r = res?.result || res || {}
    importResult.value = {
      imported: r.imported != null ? r.imported : normalized.length,
      distilled: normalizeDistilled(r.distilled),
      message: r.message || '',
    }
    ElMessage.success(`批量导入完成：${importResult.value.imported} 条已蒸馏`)
    load()
    loadStats()
  } catch (e) {
    importResult.value = null
    ElMessage.error(`批量导入失败：${e.message}`)
  } finally {
    importDialog.busy = false
  }
}

async function doImportText() {
  const text = (textInput.text || '').trim()
  if (!text) {
    ElMessage.warning('请粘贴要蒸馏的大段情报文本')
    return
  }
  importDialog.busy = true
  importResult.value = null
  try {
    const res = await api.knowledgeImportText({ text, source: textInput.source || 'import' })
    const r = res?.result || res || {}
    const distilled = normalizeDistilled(r.distilled || r.items || (Array.isArray(r) ? r : []))
    importResult.value = {
      imported: r.imported != null ? r.imported : distilled.length,
      distilled,
      message: r.message || '',
    }
    ElMessage.success(`文本蒸馏完成：生成 ${importResult.value.imported} 条知识`)
    load()
    loadStats()
  } catch (e) {
    importResult.value = null
    ElMessage.error(`文本蒸馏失败：${e.message}`)
  } finally {
    importDialog.busy = false
  }
}

// ---------- 调分（/{id}/harm，delta ±） ----------
const harmingId = ref(null)
async function adjustHarm(row) {
  let delta = 0
  try {
    const { value } = await ElMessageBox.prompt(
      `当前危害分：${row.harm_score ?? '—'}（1-10）。输入调整量，正数上调、负数下调（如 +1 / -2），与现有危害分相加后钳制在 1-10。`,
      `调危害分 #${row.id} · ${row.subject}`,
      {
        confirmButtonText: '提交调分',
        cancelButtonText: '取消',
        autofocus: false,
        inputPlaceholder: '如：+1 或 -1',
        inputValidator: (v) => {
          const n = Number(v)
          if (v == null || String(v).trim() === '' || isNaN(n)) return '请输入整数调整量（如 +1 / -1）'
          if (!Number.isInteger(n) || n === 0) return '调整量需为非零整数'
          if (n < -9 || n > 9) return '调整量超出范围（-9 ~ +9）'
          return true
        },
      }
    )
    delta = Number(value)
  } catch {
    return
  }
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(`调分原因（至少 20 字，写入审计）：危害分 ${row.harm_score ?? '—'} → ${Math.min(10, Math.max(1, (Number(row.harm_score) || 0) + delta))}`, '调分原因', {
      confirmButtonText: '确认',
      cancelButtonText: '取消',
      autofocus: false,
      inputType: 'textarea',
      inputPlaceholder: '如：该话术近期高发且有升级变体，上调危害分…',
      inputValidator: (v) => ((v || '').trim().length >= 20 ? true : '调分为敏感操作，原因至少 20 字'),
    })
    reason = (value || '').trim()
  } catch {
    return
  }
  harmingId.value = row.id
  try {
    await api.knowledgeHarm(row.id, { delta, confirm: true, reason })
    ElMessage.success(`已调整危害分 ${delta > 0 ? '+' : ''}${delta}`)
    load()
    loadStats()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    harmingId.value = null
  }
}

// ---------- 复核（/{id}/verified，confirm+reason） ----------
const verifyingId = ref(null)
async function markVerified(row) {
  if (row.verified) {
    ElMessage.info('该条目已复核')
    return
  }
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(`确认复核「${row.subject}」？复核后该条进入人工确认情报流。请填写复核说明（至少 20 字，写入审计）。`, '复核知识条目', {
      confirmButtonText: '确认复核',
      cancelButtonText: '取消',
      autofocus: false,
      inputType: 'textarea',
      inputPlaceholder: '复核说明（至少 20 字）：如：与近期案例 #1234 交叉比对一致，确认为真实…',
      inputValidator: (v) => ((v || '').trim().length >= 20 ? true : '复核为敏感操作，说明至少 20 字'),
    })
    reason = (value || '').trim()
  } catch {
    return
  }
  verifyingId.value = row.id
  try {
    await api.knowledgeVerified(row.id, { confirm: true, reason })
    ElMessage.success('已标记为人工复核')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    verifyingId.value = null
  }
}

// ---------- 蒸馏成语条（/{id}/to-pattern，confirm+reason+category） ----------
const toPatternDialog = reactive({ show: false, id: null, subject: '', category: 'other', reason: '', busy: false })

function openToPattern(row) {
  toPatternDialog.id = row.id
  toPatternDialog.subject = row.subject || ''
  toPatternDialog.category = 'other'
  toPatternDialog.reason = ''
  toPatternDialog.show = true
}

async function doToPattern() {
  if ((toPatternDialog.reason || '').trim().length < 20) {
    ElMessage.warning('蒸馏成语条为敏感操作，请填写原因（至少 20 字）')
    return
  }
  toPatternDialog.busy = true
  try {
    const res = await api.knowledgeToPattern(toPatternDialog.id, {
      confirm: true,
      reason: (toPatternDialog.reason || '').trim(),
      category: toPatternDialog.category,
    })
    const r = res?.result || res || {}
    ElMessage.success(r.message || `已蒸馏成语条「${toPatternDialog.subject}」（${toPatternDialog.category}）`)
    toPatternDialog.show = false
  } catch (e) {
    // 幂等冲突等业务提示直接透出
    ElMessage.error(e.message)
  } finally {
    toPatternDialog.busy = false
  }
}

// ---------- 下架（/{id}/disable，confirm） ----------
const disablingId = ref(null)
async function disableItem(row) {
  try {
    await ElMessageBox.confirm(
      `确定下架「${row.subject}」？下架后不再参与加权消费与检索排序（不影响已删除，可后续恢复策略由后端决定）。`,
      '下架知识条目',
      { type: 'warning', confirmButtonText: '确认下架', cancelButtonText: '取消', autofocus: false }
    )
  } catch {
    return
  }
  disablingId.value = row.id
  try {
    await api.knowledgeDisable(row.id, {
      confirm: true,
      reason: `下架知识条目「${row.subject}」，不再参与加权消费（人工确认后操作）`,
    })
    ElMessage.success('已下架（enabled=0）')
    load()
    loadStats()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    disablingId.value = null
  }
}

// ---------- 删除（DELETE /{id}，confirm+reason） ----------
async function removeItem(row) {
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(
      `确定删除知识条目「${row.subject}」？删除后不可恢复。`,
      '删除知识条目 · 需填写原因',
      {
        type: 'warning',
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        autofocus: false,
        inputType: 'textarea',
        inputPlaceholder: '删除原因（至少 20 字，将写入审计日志）',
        inputValidator: (v) => ((v || '').trim().length >= 20 ? true : '删除为敏感操作，原因至少 20 字'),
      }
    )
    reason = (value || '').trim()
  } catch {
    return
  }
  try {
    await api.knowledgeDelete(row.id, { confirm: true, reason })
    ElMessage.success('知识条目已删除')
    load()
    loadStats()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// ---------- 挂载 ----------
onMounted(() => {
  load()
  loadStats()
})
</script>

<template>
  <div class="af-page">
    <!-- 顶部说明卡：总条数 + 高危害统计 + 加权提示 -->
    <el-card shadow="never" class="af-card">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap">
        <div style="flex: 1; min-width: 320px">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <b>知识库 · 蒸馏管理</b>
            <el-tag type="success" size="small" effect="plain">共 {{ stats.total || total }} 条</el-tag>
            <el-tag type="danger" size="small" effect="plain">
              高危害（加权≥7）{{ stats.highHarmTotal }} 条
            </el-tag>
            <el-tag size="small" effect="plain" type="warning">⏳ 按时间衰减与危害加权，每日自动重算</el-tag>
          </div>
          <div class="af-muted" style="margin-top: 8px; line-height: 1.8">
            持续导入情报知识点 → 蒸馏（清洗/分类/脱敏/关键词与 IOC 抽取）→
            <b>weighted_score = harm_score × decay_weight</b>（时间衰减：&lt;7 天 1.0，7-30 天衰减至 0.8，30-90 天至 0.5，&gt;90 天 0.2）。
            高危害（加权≥7）条目供看板预警与话术库联动；知识条目可一键「蒸馏成语条」沉淀为规则。
            调分/复核/蒸馏成语条/下架/删除均为敏感操作，写入审计链。
          </div>
        </div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap">
          <el-button size="small" @click="load">刷新</el-button>
          <el-button size="small" type="primary" @click="openImport">导入知识</el-button>
        </div>
      </div>
    </el-card>

    <!-- 高危害 Top5 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>高危害 Top5</b>
            <span class="af-muted">（weighted_score ≥ 7，按综合分排序）</span>
          </div>
        </div>
      </template>
      <div v-loading="top5Loading" :style="{ minHeight: '40px' }">
        <el-alert v-if="!top5Loading && !top5.length" title="暂无加权分 ≥7 的高危害条目" type="info" :closable="false" />
        <div v-for="(t, i) in top5" :key="t.id || 'top' + i" style="display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 6px 0; border-bottom: 1px dashed #e4e7ed">
          <div style="flex: 1; min-width: 0">
            <span style="margin-right: 8px; color: #f56c6c; font-weight: 700">{{ i + 1 }}</span>
            <b style="margin-right: 8px">{{ t.subject || '—' }}</b>
            <el-tag :type="ktypeTag(t.ktype)" size="small" effect="plain">{{ ktypeLabel(t.ktype) }}</el-tag>
          </div>
          <div style="display: flex; gap: 14px; align-items: center; flex-shrink: 0">
            <span class="af-muted">{{ sourceLabel(t.source) }} · {{ fmtTime(t.created_at) }}</span>
            <el-tag :type="weightedTag(t.weighted_score).type" size="small">{{ weightedTag(t.weighted_score).text }}</el-tag>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 列表 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
          <div>
            <b>知识条目</b>
            <span class="af-muted">（/knowledge）</span>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
            <el-select v-model="ktype" placeholder="全部分类" clearable size="small" style="width: 130px" @change="search">
              <el-option v-for="c in KTYPE_OPTIONS" :key="c.value" :label="c.label" :value="c.value" />
            </el-select>
            <el-select v-model="minHarm" placeholder="危害分不限" clearable size="small" style="width: 130px" @change="search">
              <el-option v-for="n in [5, 6, 7, 8, 9, 10]" :key="n" :label="`危害分 ≥ ${n}`" :value="n" />
            </el-select>
            <el-select v-model="sort" size="small" style="width: 140px" @change="search">
              <el-option v-for="s in SORT_OPTIONS" :key="s.value" :label="s.label" :value="s.value" />
            </el-select>
            <el-input
              v-model="keyword"
              placeholder="关键词搜索（subject/content/tags）"
              clearable
              size="small"
              style="width: 220px"
              @keyup.enter="search"
              @clear="search"
            >
              <template #append>
                <el-button @click="search">搜索</el-button>
              </template>
            </el-input>
            <el-button size="small" @click="resetFilters">重置</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-bottom: 12px" />

      <el-table :data="items" v-loading="loading" size="small" empty-text="暂无知识条目，可点击右上角「导入知识」沉淀情报">
        <el-table-column label="主题（subject）" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <b>{{ row.subject || '—' }}</b>
            <el-tag v-if="row.verified" type="success" size="small" effect="plain" style="margin-left: 6px">✓ 已复核</el-tag>
            <el-tag v-else type="info" size="small" effect="plain" style="margin-left: 6px">未复核</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="ktypeTag(row.ktype)" size="small" effect="plain">{{ ktypeLabel(row.ktype) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="危害分" width="80" align="center">
          <template #default="{ row }">
            <span class="af-mono">{{ row.harm_score ?? '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="时效权重" width="90" align="center">
          <template #default="{ row }">
            <span class="af-mono">{{ row.decay_weight != null ? Number(row.decay_weight).toFixed(2) : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="综合分" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="weightedTag(row.weighted_score).type" size="small">{{ weightedTag(row.weighted_score).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="标签" min-width="150">
          <template #default="{ row }">
            <el-tag v-for="(t, i) in toArr(row.tags).slice(0, 3)" :key="i" size="small" effect="plain" type="info" style="margin: 1px 3px 1px 0">{{ t }}</el-tag>
            <span v-if="!toArr(row.tags).length" class="af-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="关键词" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-for="(k, i) in toArr(row.keywords).slice(0, 3)" :key="i" size="small" effect="plain" style="margin: 1px 3px 1px 0">{{ k }}</el-tag>
            <span v-if="!toArr(row.keywords).length" class="af-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="80" align="center">
          <template #default="{ row }">{{ sourceLabel(row.source) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="330" align="center">
          <template #default="{ row }">
            <el-button size="small" :loading="harmingId === row.id" @click="adjustHarm(row)">调分</el-button>
            <el-button size="small" type="success" plain :loading="verifyingId === row.id" :disabled="!!row.verified" @click="markVerified(row)">
              {{ row.verified ? '已复核' : '复核' }}
            </el-button>
            <el-button size="small" type="warning" plain @click="openToPattern(row)">蒸馏成语条</el-button>
            <el-button size="small" :disabled="!row.enabled" :loading="disablingId === row.id" @click="disableItem(row)">
              {{ !row.enabled ? '已下架' : '下架' }}
            </el-button>
            <el-button size="small" type="danger" plain @click="removeItem(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div style="display: flex; justify-content: flex-end; margin-top: 12px">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          small
          @current-change="load"
          @size-change="(s) => { pageSize = s; page = 1; load() }"
        />
      </div>
    </el-card>

    <!-- 检索 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>知识检索</b>
            <span class="af-muted">（/knowledge/search?q=，按加权分排序）</span>
          </div>
        </div>
      </template>
      <div style="display: flex; gap: 8px; max-width: 560px; margin-bottom: 12px">
        <el-input v-model="searchQ" placeholder="输入检索词，如：仿冒 / 屏幕共享" clearable size="small" @keyup.enter="doSearch" />
        <el-button size="small" type="primary" :loading="searching" @click="doSearch">检索</el-button>
      </div>
      <el-alert v-if="searchError" type="error" :title="searchError" :closable="false" style="margin-bottom: 12px" />
      <el-table v-if="searchDone && searchResults.length" :data="searchResults" size="small" empty-text="未检索到相关条目">
        <el-table-column label="主题" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <b>{{ row.subject || '—' }}</b>
            <el-tag v-if="row.verified" type="success" size="small" effect="plain" style="margin-left: 6px">✓</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="ktypeTag(row.ktype)" size="small" effect="plain">{{ ktypeLabel(row.ktype) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="危害分" width="80" align="center">
          <template #default="{ row }">{{ row.harm_score ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="综合分" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="weightedTag(row.weighted_score).type" size="small">{{ weightedTag(row.weighted_score).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="关键词" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">
            <el-tag v-for="(k, i) in toArr(row.keywords).slice(0, 3)" :key="i" size="small" effect="plain" style="margin: 1px 3px 1px 0">{{ k }}</el-tag>
            <span v-if="!toArr(row.keywords).length" class="af-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="80" align="center">
          <template #default="{ row }">{{ sourceLabel(row.source) }}</template>
        </el-table-column>
        <el-table-column label="时间" width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <div v-if="searchDone && !searchResults.length && !searchError" class="af-muted" style="padding: 8px 0">
        {{ searchQ ? '未检索到相关条目' : '输入关键词后点击「检索」' }}
      </div>
    </el-card>

    <!-- 导入弹窗（三种通道） -->
    <el-dialog v-model="importDialog.show" title="导入知识 · 蒸馏入库" width="720px" :close-on-click-modal="false">
      <el-tabs v-model="importDialog.tab">
        <!-- 单条录入 -->
        <el-tab-pane label="单条录入" name="single">
          <el-form label-position="top">
            <el-form-item label="主题（subject）· 必填">
              <el-input v-model="singleForm.subject" placeholder="如：新型仿冒 App 骗局" maxlength="200" show-word-limit />
            </el-form-item>
            <el-form-item label="内容（content）· 必填">
              <el-input v-model="singleForm.content" type="textarea" :rows="4" placeholder="知识点正文，蒸馏时自动清洗/分类/脱敏/抽取关键词与 IOC" maxlength="4000" show-word-limit />
            </el-form-item>
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="类型（ktype）">
                  <el-select v-model="singleForm.ktype" style="width: 100%">
                    <el-option v-for="c in KTYPE_OPTIONS" :key="c.value" :label="c.label" :value="c.value" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="危害分（harm，1-10；缺省按类型默认）">
                  <el-input-number v-model="singleForm.harm" :min="1" :max="10" :step="1" style="width: 100%" placeholder="缺省按类型" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="来源">
              <el-select v-model="singleForm.source" style="width: 100%">
                <el-option label="人工（manual）" value="manual" />
                <el-option label="导入（import）" value="import" />
              </el-select>
            </el-form-item>
          </el-form>
        </el-tab-pane>

        <!-- 批量 JSON -->
        <el-tab-pane label="批量 JSON" name="json">
          <div class="af-muted" style="line-height: 1.7; margin-bottom: 8px">
            粘贴 JSON：数组或 <span class="af-mono">{"items":[...]}</span>。每项：<b>subject</b>（必填）、<b>content</b>（必填）、
            ktype（6 类之一，缺省新骗术）、harm（1-10，缺省按类型）。按主题幂等 UPSERT，重复自动跳过。
          </div>
          <el-input v-model="jsonText" type="textarea" :rows="9" class="af-mono" :placeholder="IMPORT_JSON_EXAMPLE" />
        </el-tab-pane>

        <!-- 大文本导入 -->
        <el-tab-pane label="大文本导入（import-text）" name="text">
          <div class="af-muted" style="line-height: 1.7; margin-bottom: 8px">
            粘贴大段情报文本，后端自动切分/蒸馏为多条知识条目（清洗、分类、脱敏、关键词/IOC 抽取、加权计算）。
          </div>
          <el-input v-model="textInput.text" type="textarea" :rows="9" placeholder="粘贴大段情报文本…" maxlength="20000" show-word-limit />
          <el-form-item label="来源" style="margin-top: 10px">
            <el-select v-model="textInput.source" style="width: 160px">
              <el-option label="导入（import）" value="import" />
              <el-option label="人工（manual）" value="manual" />
              <el-option label="采集（collector）" value="collector" />
            </el-select>
          </el-form-item>
        </el-tab-pane>
      </el-tabs>

      <!-- 蒸馏结果 -->
      <div v-if="importResult" style="margin-top: 12px">
        <el-alert :type="importResult.message ? 'warning' : 'success'" :closable="false" style="margin-bottom: 8px">
          <template #title>
            蒸馏完成：生成 <b>{{ importResult.imported }}</b> 条
            <span v-if="importResult.message">（{{ importResult.message }}）</span>
          </template>
        </el-alert>
        <el-table v-if="importResult.distilled.length" :data="importResult.distilled" size="small" max-height="260">
          <el-table-column label="主题" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <b>{{ row.subject || '—' }}</b>
              <span class="af-muted" style="margin-left: 6px">#{{ row.id }}</span>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="ktypeTag(row.ktype)" size="small" effect="plain">{{ ktypeLabel(row.ktype) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="危害分" width="80" align="center">
            <template #default="{ row }">{{ row.harm_score ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="时效权重" width="90" align="center">
            <template #default="{ row }">{{ row.decay_weight != null ? Number(row.decay_weight).toFixed(2) : '—' }}</template>
          </el-table-column>
          <el-table-column label="综合分" width="90" align="center">
            <template #default="{ row }">
              <el-tag :type="weightedTag(row.weighted_score).type" size="small">{{ weightedTag(row.weighted_score).text }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <template #footer>
        <el-button @click="importDialog.show = false">关闭</el-button>
        <el-button v-if="importDialog.tab === 'json'" @click="fillJsonExample">填入示例</el-button>
        <el-button
          v-if="importDialog.tab === 'single'"
          type="primary"
          :loading="importDialog.busy"
          @click="doImportSingle"
        >
          导入并蒸馏
        </el-button>
        <el-button v-else-if="importDialog.tab === 'json'" type="primary" :loading="importDialog.busy" @click="doImportJson">
          批量导入
        </el-button>
        <el-button v-else type="primary" :loading="importDialog.busy" @click="doImportText">
          文本蒸馏
        </el-button>
      </template>
    </el-dialog>

    <!-- 蒸馏成语条弹窗 -->
    <el-dialog
      v-model="toPatternDialog.show"
      :title="`蒸馏成语条：${toPatternDialog.subject}`"
      width="520px"
      :close-on-click-modal="false"
    >
      <div class="af-muted" style="line-height: 1.7; margin-bottom: 12px">
        将知识条目蒸馏为话术库词条（speech_patterns 新增，weight = min(10, harm)）。
        以知识条目主题为 pattern，关键词为 regex 候选；按主题幂等，重复时后端返回冲突提示。
      </div>
      <el-form label-position="top">
        <el-form-item label="话术分类（category）">
          <el-select v-model="toPatternDialog.category" style="width: 100%">
            <el-option v-for="c in [
              { value: 'fake_investment', label: '投资' },
              { value: 'impersonation', label: '冒充' },
              { value: 'job_scam', label: '刷单' },
              { value: 'pig_butchering', label: '杀猪盘' },
              { value: 'refund_scam', label: '退款' },
              { value: 'other', label: '其他' },
            ]" :key="c.value" :label="c.label" :value="c.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="原因（至少 20 字，写入审计）">
          <el-input v-model="toPatternDialog.reason" type="textarea" :rows="3" maxlength="200" show-word-limit placeholder="如：知识条目 #id 与近期高发案例一致，蒸馏为规则词条强化命中…" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="toPatternDialog.show = false">取消</el-button>
        <el-button type="primary" :loading="toPatternDialog.busy" @click="doToPattern">确认蒸馏成语条</el-button>
      </template>
    </el-dialog>
  </div>
</template>