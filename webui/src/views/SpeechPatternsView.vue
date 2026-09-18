<script setup>
// 话术库管理界面（话术库适应更新）：词条列表（分页 + 分类 + 关键词过滤）+ 新增/编辑弹窗
// + 启停开关 + 删除（confirm+reason 审计）+ 批量导入（JSON）+ 全量导出（下载）+ 词库统计。
// 契约（后端 A 队并行实现）：GET /speech-patterns → {items,total}；POST /、PUT /{id}（confirm+reason）、
// POST /{id}/toggle、DELETE /{id}（confirm+reason）、POST /import（{items:[...]} → {imported,skipped}）、GET /export。
// 热更新：rule_match 每次查询实时读库，管理端增删改后下一次检测立即生效，无需重启主控。
// 防御式：所有请求 try/catch，端点未就绪时页面给出错误态不崩；加载/错误/空态齐全。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'

// ---------- 分类元信息（与后端 ALLOWED_CATEGORIES / speech_seed.py 对齐） ----------
const CATEGORY_OPTIONS = [
  { value: 'fake_investment', label: '投资' },
  { value: 'impersonation', label: '冒充' },
  { value: 'job_scam', label: '刷单' },
  { value: 'pig_butchering', label: '杀猪盘' },
  { value: 'refund_scam', label: '退款' },
  { value: 'other', label: '其他' },
]
const CATEGORY_LABEL = Object.fromEntries(CATEGORY_OPTIONS.map((o) => [o.value, o.label]))
const CATEGORY_TAG_TYPE = {
  fake_investment: 'danger',
  impersonation: 'danger',
  job_scam: 'warning',
  pig_butchering: 'danger',
  refund_scam: 'warning',
  other: 'info',
}

function categoryLabel(v) {
  return CATEGORY_LABEL[v] || v || '—'
}

// 权重 1-10 标签色：越高越危险
function weightMeta(w) {
  const n = Number(w) || 0
  if (n >= 7) return { type: 'danger', text: n }
  if (n >= 4) return { type: 'warning', text: n }
  return { type: 'info', text: n }
}

// ---------- 列表加载（分页 + 过滤） ----------
const items = ref([])
const total = ref(0)
const loading = ref(false)
const error = ref('')

const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')
const category = ref('')

function buildQuery() {
  const qs = new URLSearchParams()
  qs.set('page', String(page.value))
  qs.set('page_size', String(pageSize.value))
  if (category.value) qs.set('category', category.value)
  if ((keyword.value || '').trim()) qs.set('keyword', keyword.value.trim())
  return '?' + qs.toString()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.speechPatterns(buildQuery())
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
  keyword.value = ''
  category.value = ''
  page.value = 1
  load()
}

// ---------- 词库统计：总条数 + 每类计数（逐类取 total，防御式降级） ----------
const stats = ref({ total: 0, byCategory: {} })
const statsLoading = ref(false)

async function loadStats() {
  statsLoading.value = true
  try {
    const totals = await Promise.all(
      CATEGORY_OPTIONS.map(async (c) => {
        try {
          const res = await api.speechPatterns(`?page=1&page_size=1&category=${c.value}`)
          const data = res?.data || res || {}
          return { category: c.value, n: Number(data.total != null ? data.total : 0) }
        } catch {
          return { category: c.value, n: null }
        }
      })
    )
    const byCategory = {}
    for (const t of totals) byCategory[t.category] = t.n
    stats.value = { total: total.value, byCategory }
  } catch {
    // 统计失败不阻塞主列表
  } finally {
    statsLoading.value = false
  }
}

function catCount(c) {
  const n = stats.value.byCategory[c]
  return n == null ? '—' : n
}

// ---------- 新增 / 编辑弹窗（编辑为敏感操作：confirm+reason≥20 写审计） ----------
const dialog = reactive({
  show: false,
  isEdit: false,
  id: null,
  pattern: '',
  regex: '',
  weight: 5,
  category: 'other',
  enabled: true,
  reason: '',
  submitting: false,
})

function resetDialog() {
  dialog.id = null
  dialog.pattern = ''
  dialog.regex = ''
  dialog.weight = 5
  dialog.category = 'other'
  dialog.enabled = true
  dialog.reason = ''
  dialog.submitting = false
}

function openAdd() {
  resetDialog()
  dialog.isEdit = false
  dialog.show = true
}

function openEdit(row) {
  resetDialog()
  dialog.isEdit = true
  dialog.id = row.id
  dialog.pattern = row.pattern || ''
  dialog.regex = row.regex || ''
  dialog.weight = row.weight != null ? Number(row.weight) : 5
  dialog.category = CATEGORY_OPTIONS.some((o) => o.value === row.category) ? row.category : 'other'
  dialog.enabled = !!row.enabled
  dialog.show = true
}

async function savePattern() {
  const pattern = (dialog.pattern || '').trim()
  if (!pattern) {
    ElMessage.warning('话术关键词（pattern）必填')
    return
  }
  const weight = Number(dialog.weight) || 5
  if (weight < 1 || weight > 10) {
    ElMessage.warning('权重必须在 1-10 之间')
    return
  }
  // 编辑为敏感操作：confirm + reason≥20 字（与后端校验对齐，写 gate_logs）
  if (dialog.isEdit && (dialog.reason || '').trim().length < 20) {
    ElMessage.warning('编辑话术为敏感操作，请填写变更原因（至少 20 字）')
    return
  }
  dialog.submitting = true
  try {
    const payload = {
      pattern,
      regex: (dialog.regex || '').trim() || null,
      weight,
      category: dialog.category,
    }
    if (dialog.isEdit) {
      await api.speechPatternUpdate(dialog.id, {
        ...payload,
        enabled: dialog.enabled ? 1 : 0,
        confirm: true,
        reason: (dialog.reason || '').trim(),
      })
      ElMessage.success('话术已更新，下一次检测即时生效')
    } else {
      await api.speechPatternCreate({ ...payload, enabled: dialog.enabled ? 1 : 0 })
      ElMessage.success('话术已新增，下一次检测即时生效')
    }
    dialog.show = false
    load()
    loadStats()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    dialog.submitting = false
  }
}

// ---------- 启停（toggle：停用后不参与命中） ----------
const togglingId = ref(null)
async function toggleEnabled(row, v) {
  if (togglingId.value !== null && togglingId.value !== row.id) return
  togglingId.value = row.id
  const prev = row.enabled
  row.enabled = v ? 1 : 0
  try {
    await api.speechPatternToggle(row.id)
    ElMessage.success(v ? '已启用' : '已停用（不参与命中）')
  } catch (e) {
    row.enabled = prev
    ElMessage.error(e.message)
  } finally {
    togglingId.value = null
  }
}

// ---------- 删除（敏感操作：confirm + reason≥20） ----------
async function removePattern(row) {
  let reason = ''
  try {
    const { value } = await ElMessageBox.prompt(
      `确定删除话术「${row.pattern}」？删除后不可恢复，规则引擎将不再命中该词条。`,
      '删除话术 · 需填写原因',
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
    await api.speechPatternDelete(row.id, { confirm: true, reason })
    ElMessage.success('话术已删除')
    load()
    loadStats()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// ---------- 批量导入（JSON：{items:[{pattern,regex?,weight,category}]}） ----------
const importDialog = reactive({ show: false, text: '', importing: false })
const importResult = ref(null) // {imported, skipped, message?}

const IMPORT_EXAMPLE = JSON.stringify(
  {
    items: [
      { pattern: '示例新骗术话术', regex: '', weight: 7, category: 'fake_investment' },
      { pattern: '另一条新骗术', weight: 6, category: 'other' },
    ],
  },
  null,
  2
)

function openImport() {
  importDialog.text = ''
  importResult.value = null
  importDialog.show = true
}

function fillExample() {
  importDialog.text = IMPORT_EXAMPLE
}

async function doImport() {
  const raw = (importDialog.text || '').trim()
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
    const it = items[i]
    if (!it || typeof it.pattern !== 'string' || !it.pattern.trim()) {
      ElMessage.warning(`第 ${i + 1} 条缺少 pattern（必填）`)
      return
    }
    const w = Number(it.weight)
    if (it.weight != null && (isNaN(w) || w < 1 || w > 10)) {
      ElMessage.warning(`第 ${i + 1} 条权重需在 1-10 之间`)
      return
    }
    if (it.category && !CATEGORY_OPTIONS.some((o) => o.value === it.category)) {
      ElMessage.warning(`第 ${i + 1} 条分类「${it.category}」不合法`)
      return
    }
  }
  importDialog.importing = true
  importResult.value = null
  try {
    const normalized = items.map((it) => ({
      pattern: (it.pattern || '').trim(),
      regex: it.regex != null ? String(it.regex).trim() || null : null,
      weight: it.weight != null ? Number(it.weight) : 5,
      category: it.category || 'other',
    }))
    const res = await api.speechPatternImport({ items: normalized })
    const r = res?.result || res || {}
    const imported = r.imported != null ? r.imported : r.inserted != null ? r.inserted : null
    const skipped = r.skipped != null ? r.skipped : r.duplicated != null ? r.duplicated : null
    importResult.value = {
      imported: imported == null ? normalized.length : imported,
      skipped: skipped == null ? 0 : skipped,
      message: r.message || '',
    }
    ElMessage.success(`导入完成：新增 ${importResult.value.imported} 条，跳过 ${importResult.value.skipped} 条`)
    load()
    loadStats()
  } catch (e) {
    importResult.value = null
    ElMessage.error(`导入失败：${e.message}`)
  } finally {
    importDialog.importing = false
  }
}

// ---------- 导出（触发浏览器下载 JSON） ----------
const exporting = ref(false)
async function doExport() {
  exporting.value = true
  try {
    const res = await api.speechPatternExport()
    const data = res?.data !== undefined ? res.data : res
    const payload = data && typeof data === 'object' ? data : { items: data || [] }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const ts = new Date()
    const pad = (n) => String(n).padStart(2, '0')
    a.href = url
    a.download = `speech-patterns-export-${ts.getFullYear()}${pad(ts.getMonth() + 1)}${pad(ts.getDate())}-${pad(ts.getHours())}${pad(ts.getMinutes())}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    ElMessage.success('已导出词库 JSON（全量）')
  } catch (e) {
    ElMessage.error(`导出失败：${e.message}`)
  } finally {
    exporting.value = false
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
    <!-- 顶部说明卡：总条数 + 每类计数 + 热更新提示 -->
    <el-card shadow="never" class="af-card">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap">
        <div style="flex: 1; min-width: 320px">
          <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
            <b>话术库 · 词库管理</b>
            <el-tag v-if="!statsLoading" type="success" size="small" effect="plain">共 {{ stats.total || total }} 条</el-tag>
            <el-tag v-else size="small" effect="plain" type="info">统计中…</el-tag>
            <el-tag size="small" effect="plain" type="warning">⚡ 词库变更即时生效，无需重启</el-tag>
          </div>
          <div class="af-muted" style="margin-top: 8px; line-height: 1.8">
            检测分级 L3+ 人工确认为诈骗发布案例时自动权重 +1（封顶 10）；判误报可编辑/降权。
            编辑与删除为敏感操作，需填写原因（≥20 字）并写入审计链。rule_match 实时查库，管理端改动下一次检测立即生效。
          </div>
          <div style="margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap; align-items: center">
            <span class="af-muted" style="font-size: 12px">每类计数：</span>
            <el-tag v-for="c in CATEGORY_OPTIONS" :key="c.value" :type="CATEGORY_TAG_TYPE[c.value]" size="small" effect="plain">
              {{ c.label }} {{ catCount(c.value) }}
            </el-tag>
          </div>
        </div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap">
          <el-button size="small" @click="load">刷新</el-button>
          <el-button size="small" :loading="exporting" @click="doExport">导出 JSON</el-button>
          <el-button size="small" type="warning" plain @click="openImport">批量导入</el-button>
          <el-button size="small" type="primary" @click="openAdd">新增话术</el-button>
        </div>
      </div>
    </el-card>

    <!-- 过滤 + 列表 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
          <div>
            <b>词条列表</b>
            <span class="af-muted">（/speech-patterns）</span>
          </div>
          <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
            <el-select
              v-model="category"
              placeholder="全部分类"
              clearable
              style="width: 130px"
              size="small"
              @change="search"
            >
              <el-option v-for="c in CATEGORY_OPTIONS" :key="c.value" :label="c.label" :value="c.value" />
            </el-select>
            <el-input
              v-model="keyword"
              placeholder="关键词搜索（pattern/regex）"
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

      <el-table :data="items" v-loading="loading" size="small" empty-text="暂无词条，可点击「新增话术」或「批量导入」沉淀新骗术">
        <el-table-column label="话术关键词（pattern）" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <b>{{ row.pattern || '—' }}</b>
          </template>
        </el-table-column>
        <el-table-column label="正则（regex）" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.regex" class="af-mono" style="font-size: 12px">{{ row.regex }}</span>
            <span v-else class="af-muted" style="font-size: 12px">—（关键词包含匹配）</span>
          </template>
        </el-table-column>
        <el-table-column label="权重" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="weightMeta(row.weight).type" size="small">{{ weightMeta(row.weight).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="分类" width="110" align="center">
          <template #default="{ row }">
            <el-tag :type="CATEGORY_TAG_TYPE[row.category] || 'info'" size="small" effect="plain">
              {{ categoryLabel(row.category) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" width="90" align="center">
          <template #default="{ row }">
            <el-switch
              :model-value="!!row.enabled"
              :loading="togglingId === row.id"
              :disabled="togglingId !== null && togglingId !== row.id"
              inline-prompt
              active-text="启"
              inactive-text="停"
              @change="(v) => toggleEnabled(row, v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="命中次数" width="100" align="center">
          <template #default="{ row }">
            <span :class="Number(row.hit_count) > 0 ? 'text-warning' : 'af-muted'">
              {{ row.hit_count != null ? row.hit_count : '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" align="center">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" type="danger" plain @click="removePattern(row)">删除</el-button>
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

    <!-- 新增 / 编辑弹窗 -->
    <el-dialog
      v-model="dialog.show"
      :title="dialog.isEdit ? `编辑话术 #${dialog.id}` : '新增话术'"
      width="620px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-form-item label="话术关键词（pattern）· 必填">
          <el-input v-model="dialog.pattern" placeholder="如：稳赚不赔 / 注销校园贷" maxlength="200" show-word-limit />
        </el-form-item>
        <el-form-item label="正则（regex）· 可空">
          <el-input v-model="dialog.regex" placeholder="留空 = 按关键词包含匹配；填写则优先于 pattern，如：公检法|检察院" class="af-mono" />
          <div class="af-muted" style="font-size: 12px; line-height: 1.6; margin-top: 4px">
            留空时引擎按 pattern 做子串包含匹配；填写正则后引擎优先使用 regex 匹配。
          </div>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="权重（1-10）">
              <el-input-number v-model="dialog.weight" :min="1" :max="10" :step="1" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="分类">
              <el-select v-model="dialog.category" style="width: 100%">
                <el-option v-for="c in CATEGORY_OPTIONS" :key="c.value" :label="c.label" :value="c.value" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item v-if="dialog.isEdit" label="启用">
          <el-switch v-model="dialog.enabled" inline-prompt active-text="启" inactive-text="停" />
          <div class="af-muted" style="font-size: 12px; margin-left: 10px">停用后该词条不参与命中（等效于 toggle）</div>
        </el-form-item>
        <el-form-item v-if="dialog.isEdit" label="变更原因（敏感操作 · 至少 20 字，写入审计日志）">
          <el-input
            v-model="dialog.reason"
            type="textarea"
            :rows="3"
            maxlength="200"
            show-word-limit
            placeholder="如：案例 #123 确认为新话术变体，调整正则覆盖范围…"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.show = false">取消</el-button>
        <el-button type="primary" :loading="dialog.submitting" @click="savePattern">
          {{ dialog.isEdit ? '保存修改' : '新增话术' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 批量导入弹窗 -->
    <el-dialog
      v-model="importDialog.show"
      title="批量导入话术（JSON）"
      width="640px"
      :close-on-click-modal="false"
    >
      <div class="af-muted" style="line-height: 1.7; margin-bottom: 8px">
        粘贴 JSON：数组 <span class="af-mono">[{...}]</span> 或对象 <span class="af-mono">{"items":[{...}]}</span>。
        每项字段：<b>pattern</b>（必填）、regex（可空）、weight（1-10，缺省 5）、category（缺省 other）。
        导入按 pattern+category 幂等 UPSERT，重复词条自动跳过。可点「填入示例」查看格式。
      </div>
      <el-input
        v-model="importDialog.text"
        type="textarea"
        :rows="12"
        class="af-mono"
        placeholder='{"items":[{"pattern":"...","regex":"","weight":7,"category":"fake_investment"}]}'
      />
      <el-alert v-if="importResult" :type="importResult.message ? 'warning' : 'success'" :closable="false" style="margin-top: 10px">
        <template #title>
          导入完成：新增 <b>{{ importResult.imported }}</b> 条，跳过 <b>{{ importResult.skipped }}</b> 条
          <span v-if="importResult.message">（{{ importResult.message }}）</span>
        </template>
      </el-alert>
      <template #footer>
        <el-button @click="fillExample">填入示例</el-button>
        <el-button @click="importDialog.show = false">关闭</el-button>
        <el-button type="primary" :loading="importDialog.importing" @click="doImport">开始导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>
