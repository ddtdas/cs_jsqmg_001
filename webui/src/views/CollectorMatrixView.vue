<script setup>
// 采集矩阵（P10）：多平台 × 账号 × 采集策略 的编排表（/collector/matrix）
// + 立即执行（单格/全部）+ 新增/编辑/启停/删除 + GitHub 技术栈调研结论（/collector/tech-stack）。
// 合规：只读自有/公开信息、限速节流、不自动发布；采集内容经 ingest → 检测流水线 → 反向侦察。
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'

// ---------- 选项元信息 ----------
const PLATFORM_OPTIONS = [
  { value: 'zhihu', label: '知乎' },
  { value: 'weibo', label: '微博' },
  { value: 'telegram', label: 'Telegram' },
  { value: 'wechat', label: '微信' },
  { value: 'qq', label: 'QQ' },
  { value: 'rsshub', label: 'RSSHub' },
]
const PLATFORM_LABEL = Object.fromEntries(PLATFORM_OPTIONS.map((o) => [o.value, o.label]))

const COLLECT_OPTIONS = [
  { value: 'dm', label: '私信' },
  { value: 'comments', label: '评论' },
  { value: 'posts', label: '帖子' },
  { value: 'follows', label: '关注' },
]
const COLLECT_LABEL = Object.fromEntries(COLLECT_OPTIONS.map((o) => [o.value, o.label]))

const FREQ_OPTIONS = [
  { value: 'minute', label: '每分钟' },
  { value: 'hourly', label: '每小时' },
  { value: 'daily', label: '每天' },
]
const FREQ_LABEL = Object.fromEntries(FREQ_OPTIONS.map((o) => [o.value, o.label]))

function statusMeta(s) {
  return (
    {
      pending: { type: 'info', label: '待执行' },
      running: { type: 'warning', label: '执行中' },
      ok: { type: 'success', label: '成功' },
      error: { type: 'danger', label: '错误' },
    }[s] || { type: 'info', label: s || '未知' }
  )
}

// strategy 可能是对象或 JSON 字符串（后端落库），统一解析
function parseStrategy(raw) {
  if (!raw) return {}
  if (typeof raw === 'string') {
    try {
      return JSON.parse(raw)
    } catch {
      return {}
    }
  }
  return raw
}

// 策略摘要：如 '私信·每小时·深度5'
function strategySummary(raw) {
  const s = parseStrategy(raw)
  const parts = []
  parts.push(COLLECT_LABEL[s.collect] || s.collect || '—')
  parts.push(FREQ_LABEL[s.frequency] || s.frequency || '—')
  if (s.depth != null) parts.push(`深度${s.depth}`)
  let text = parts.join('·')
  if (s.keyword) text += ` · 关键词:${s.keyword}`
  if (s.near_dup != null) text += ` · 近重复${s.near_dup}`
  return text
}

function platformName(row) {
  return row.platform_name || PLATFORM_LABEL[row.platform] || row.platform || '—'
}

// ---------- 数据加载 ----------
const cells = ref([])
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    // 兼容两种响应形态：{ cells:[...] } 或直接数组
    const res = await api.collectorMatrix()
    cells.value = (res?.cells || res || []).map((c) => ({ ...c }))
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
load()

// ---------- 爬虫技术栈（GitHub 调研结论） ----------
const techStack = ref([])
const stackLoading = ref(false)
const stackError = ref('')

async function loadStack() {
  stackLoading.value = true
  stackError.value = ''
  try {
    const res = await api.collectorTechStack()
    techStack.value = res?.stack || res || []
  } catch (e) {
    stackError.value = e.message
  } finally {
    stackLoading.value = false
  }
}
loadStack()

function starsText(n) {
  const k = Number(n) || 0
  return k >= 1000 ? `★ ${(k / 1000).toFixed(1)}k` : `★ ${k}`
}

async function copyText(text, tip) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(tip || '已复制')
  } catch {
    ElMessage.error('复制失败，请手动选择复制')
  }
}

function copyRepo(repo) {
  if (!repo) return
  const url = /^https?:\/\//.test(repo) ? repo : `https://github.com/${repo}`
  copyText(url, `${repo} 仓库链接已复制`)
}

// ---------- 新增 / 编辑弹窗 ----------
const dialog = reactive({
  show: false,
  isEdit: false,
  id: null,
  platform: '',
  account: '',
  enabled: true,
  collect: 'dm',
  frequency: 'hourly',
  depth: 5,
  keyword: '',
  near_dup: 0.85,
  submitting: false,
})

function resetDialog() {
  dialog.id = null
  dialog.platform = ''
  dialog.account = ''
  dialog.enabled = true
  dialog.collect = 'dm'
  dialog.frequency = 'hourly'
  dialog.depth = 5
  dialog.keyword = ''
  dialog.near_dup = 0.85
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
  dialog.platform = row.platform
  dialog.account = row.account || ''
  dialog.enabled = !!row.enabled
  const s = parseStrategy(row.strategy)
  dialog.collect = COLLECT_OPTIONS.some((o) => o.value === s.collect) ? s.collect : 'dm'
  dialog.frequency = FREQ_OPTIONS.some((o) => o.value === s.frequency) ? s.frequency : 'hourly'
  dialog.depth = s.depth != null ? s.depth : 5
  dialog.keyword = s.keyword || ''
  dialog.near_dup = s.near_dup != null ? s.near_dup : 0.85
  dialog.show = true
}

function buildStrategyPayload() {
  return {
    collect: dialog.collect,
    frequency: dialog.frequency,
    depth: dialog.depth,
    keyword: (dialog.keyword || '').trim(),
    near_dup: dialog.near_dup,
  }
}

async function saveCell() {
  if (!dialog.platform) {
    ElMessage.warning('请选择平台')
    return
  }
  if (!dialog.isEdit && dialog.platform !== 'rsshub' && !(dialog.account || '').trim()) {
    ElMessage.warning('请输入账号（rsshub 无需账号）')
    return
  }
  dialog.submitting = true
  try {
    const strategy = buildStrategyPayload()
    if (dialog.isEdit) {
      await api.collectorUpdateCell(dialog.id, { strategy, enabled: dialog.enabled ? 1 : 0 })
      ElMessage.success('单元格已更新')
    } else {
      await api.collectorAddCell(dialog.platform, (dialog.account || '').trim(), strategy)
      ElMessage.success('单元格已新增，进入采集矩阵')
    }
    dialog.show = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    dialog.submitting = false
  }
}

// ---------- 立即执行（单格 / 全部） ----------
const runningId = ref(null)
const runningAll = ref(false)

async function runCell(row) {
  if (row.status === 'running') return
  runningId.value = row.id
  try {
    const r = await api.collectorRunCell(row.id)
    ElMessage.success(`已执行 ${r.platform || row.platform}：采集 ${r.collected ?? 0} 条${r.error ? `（${r.error}）` : ''}`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    runningId.value = null
    load()
  }
}

async function runAll() {
  runningAll.value = true
  try {
    const r = await api.collectorRunAll()
    ElMessage.success(`执行全部完成：${r.ran ?? 0} 格，采集 ${r.collected ?? 0} 条`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    runningAll.value = false
    load()
  }
}

// ---------- 启停 ----------
const togglingId = ref(null)
async function toggleEnabled(row, v) {
  if (togglingId.value) return
  togglingId.value = row.id
  const prev = row.enabled
  row.enabled = v ? 1 : 0
  try {
    await api.collectorUpdateCell(row.id, { enabled: row.enabled })
    ElMessage.success(v ? '已启用' : '已停用')
  } catch (e) {
    row.enabled = prev
    ElMessage.error(e.message)
  } finally {
    togglingId.value = null
  }
}

// ---------- 删除 ----------
async function removeCell(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除单元格「${platformName(row)}${row.account ? ' · ' + row.account : ''}」？该单元格及其调度将一并移除，无法恢复。`,
      '删除单元格',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', autofocus: false }
    )
  } catch {
    return
  }
  try {
    await api.collectorDeleteCell(row.id)
    ElMessage.success('单元格已删除')
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <div class="af-page">
    <!-- 顶部说明卡 -->
    <el-card shadow="never" class="af-card">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap">
        <div>
          <b>采集矩阵 · 平台 × 账号 × 策略</b>
          <div class="af-muted" style="margin-top: 6px; max-width: 860px; line-height: 1.8">
            矩阵化多平台采集编排：每格 = 平台 + 账号 + 采集策略（类型 / 频率 / 深度 / 关键词 / 近重复）。
            采集内容经 <b>ingest → 检测流水线 → 反向侦察</b>，喂给既有扫描/分级/情报链路；APScheduler
            按频率节流轮询调度，也可单格「立即执行」。合规：只读自有/公开信息、限速、不自动发布。
          </div>
        </div>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </el-card>

    <!-- 操作栏 + 矩阵表格 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>采集矩阵</b>
            <span class="af-muted">（/collector/matrix）</span>
          </div>
          <div style="display: flex; gap: 8px; align-items: center">
            <el-tooltip content="仅执行 enabled=1 的单元格，后端限速节流" placement="top">
              <el-button type="success" plain :loading="runningAll" :disabled="cells.some((c) => c.status === 'running')" @click="runAll">
                立即执行全部
              </el-button>
            </el-tooltip>
            <el-button type="primary" @click="openAdd">新增单元格</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-bottom: 12px" />

      <el-table :data="cells" v-loading="loading" size="small" empty-text="暂无单元格，点击右上角「新增单元格」创建">
        <el-table-column label="平台" min-width="120">
          <template #default="{ row }">
            <b>{{ platformName(row) }}</b>
            <div v-if="row.platform === 'rsshub'" class="af-muted">公开聚合 · 免账号</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tooltip v-if="row.last_error" :content="`最近错误：${row.last_error}`" placement="top">
              <el-tag :type="statusMeta(row.status).type" size="small" style="cursor: pointer">
                {{ statusMeta(row.status).label }}
              </el-tag>
            </el-tooltip>
            <el-tag v-else :type="statusMeta(row.status).type" size="small">{{ statusMeta(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="账号" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">{{ row.account || '—' }}</template>
        </el-table-column>
        <el-table-column label="策略" min-width="240">
          <template #default="{ row }">
            <el-tooltip :content="JSON.stringify(parseStrategy(row.strategy))" placement="top" :show-after="300">
              <span class="af-mono" style="font-size: 12px; cursor: default">{{ strategySummary(row.strategy) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="最近运行" min-width="160" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_run_at || '—' }}</template>
        </el-table-column>
        <el-table-column label="最近条目" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ row.last_item_id || '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" min-width="260">
          <template #default="{ row }">
            <el-button
              size="small"
              type="success"
              plain
              :loading="runningId === row.id"
              :disabled="row.status === 'running'"
              @click="runCell(row)"
            >
              立即执行
            </el-button>
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-switch
              :model-value="!!row.enabled"
              :loading="togglingId === row.id"
              :disabled="togglingId !== null && togglingId !== row.id"
              inline-prompt
              active-text="启"
              inactive-text="停"
              style="margin: 0 8px"
              @change="(v) => toggleEnabled(row, v)"
            />
            <el-button size="small" type="danger" plain @click="removeCell(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 爬虫技术栈卡片（GitHub 调研结论，嵌入式集成） -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>GitHub 技术栈（嵌入式集成）</b>
            <span class="af-muted">（/collector/tech-stack）</span>
          </div>
          <el-button size="small" @click="loadStack">刷新</el-button>
        </div>
      </template>
      <div class="af-muted" style="margin-bottom: 10px; line-height: 1.7">
        以下开源项目经调研选定，以 <b>适配器 / 文档 / 可选依赖</b> 嵌入本系统（非独立部署）：
        采集引擎借鉴 Crawlee 模式（httpx + 自动重试 + 指纹去重 + 限速令牌桶）；微信/QQ/Telegram 以桥适配器接入；
        DecryptLogin / zhihu_spider 仅借鉴签名思路（不引入第三方凭据处理）。
      </div>
      <el-alert v-if="stackError" type="error" :title="stackError" :closable="false" style="margin-bottom: 12px" />
      <el-table :data="techStack" v-loading="stackLoading" size="small" empty-text="暂无技术栈数据">
        <el-table-column label="项目" min-width="200">
          <template #default="{ row }">
            <div style="display: flex; align-items: center; gap: 6px">
              <b>{{ row.name }}</b>
              <el-tooltip :content="'点击复制仓库链接'" placement="top">
                <el-tag
                  v-if="row.repo"
                  size="small"
                  effect="plain"
                  class="af-mono"
                  style="cursor: pointer"
                  @click="copyRepo(row.repo)"
                >
                  {{ row.repo }}
                </el-tag>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="Stars" width="100">
          <template #default="{ row }">
            <span class="text-warning" style="font-weight: 600">{{ starsText(row.stars) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="use" label="用途" min-width="240" show-overflow-tooltip />
        <el-table-column prop="integrate" label="集成方式" min-width="300" show-overflow-tooltip />
      </el-table>
    </el-card>

    <!-- 新增 / 编辑弹窗 -->
    <el-dialog
      v-model="dialog.show"
      :title="dialog.isEdit ? `编辑单元格 #${dialog.id}` : '新增单元格'"
      width="640px"
      :close-on-click-modal="false"
    >
      <el-form label-position="top">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="平台">
              <el-select v-model="dialog.platform" placeholder="选择平台" style="width: 100%" :disabled="dialog.isEdit">
                <el-option v-for="p in PLATFORM_OPTIONS" :key="p.value" :label="p.label" :value="p.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="账号">
              <el-input
                v-model="dialog.account"
                :disabled="dialog.platform === 'rsshub' || dialog.isEdit"
                :placeholder="dialog.platform === 'rsshub' ? 'RSSHub 公开聚合，无需账号' : '自有账号（对应配置账号页）'"
              />
            </el-form-item>
          </el-col>
        </el-row>
        <div v-if="dialog.isEdit" class="af-muted" style="margin: -8px 0 12px">
          平台 / 账号不可修改；仅可调整策略与启停状态。
        </div>

        <el-form-item label="采集类型">
          <el-select v-model="dialog.collect" style="width: 100%">
            <el-option v-for="o in COLLECT_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="频率">
          <el-select v-model="dialog.frequency" style="width: 100%">
            <el-option v-for="o in FREQ_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="深度（页面/条数上限）">
              <el-input-number v-model="dialog.depth" :min="1" :max="100" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="近重复阈值（0.5–1，复用 ingest 近重复判定）">
              <el-slider v-model="dialog.near_dup" :min="0.5" :max="1" :step="0.05" show-input style="margin-top: 14px" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="关键词（可空，命中才采集）">
          <el-input v-model="dialog.keyword" placeholder="如：诈骗 / 兼职 / 加微信（留空则全量）" />
        </el-form-item>

        <div v-if="dialog.isEdit" style="margin-bottom: 4px">
          <span style="font-size: 13px; font-weight: 600; margin-right: 10px">启停</span>
          <el-switch v-model="dialog.enabled" inline-prompt active-text="启" inactive-text="停" />
        </div>
      </el-form>
      <template #footer>
        <el-button @click="dialog.show = false">取消</el-button>
        <el-button type="primary" :loading="dialog.submitting" @click="saveCell">
          {{ dialog.isEdit ? '保存修改' : '新增单元格' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>