<script setup>
// 信息收集（T2 侧边栏⑥）：话术检测入口（/scan/text）+ 导入入口（/zhihu/import）
// + 导入队列状态（/scan/inbox）+ 最近检测记录（/scan/hits）。全部真实 API。
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

// 详情抽屉（四要素）：最近检测记录行 / inbox 行（均为 detections 记录，id 即 detection_id）
const detailDrawer = reactive({ show: false, detectionId: null })

function openHitDetail(row) {
  const detId = row?.id ?? row?.detection_id
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}

const form = reactive({ text: '', source: 'manual' })
const scanning = ref(false)
const scanResult = ref(null)
const scanError = ref('')

const importForm = reactive({ text: '', fromUrlName: '', source: 'manual', submitting: false })
const inbox = ref(null)
const inboxLoading = ref(false)
const inboxError = ref('')

const hits = ref([])
const hitsLoading = ref(false)
const hitsError = ref('')

async function doScan() {
  if (!form.text.trim()) {
    ElMessage.warning('请输入待检测文本')
    return
  }
  scanning.value = true
  scanError.value = ''
  try {
    scanResult.value = await api.scanText(form.text.trim(), form.source)
    loadHits()
  } catch (e) {
    scanError.value = e.message
    scanResult.value = null
  } finally {
    scanning.value = false
  }
}

async function submitImport() {
  if (!importForm.text.trim()) {
    ElMessage.warning('请输入要导入的对话文本')
    return
  }
  importForm.submitting = true
  try {
    const r = await api.zhihuImport(importForm.text.trim(), importForm.source, importForm.fromUrlName)
    ElMessage.success(`导入成功（detection_id=${r.detection_id ?? r.id ?? '?'}），进入检测流水线`)
    importForm.text = ''
    loadInbox()
    loadHits()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    importForm.submitting = false
  }
}

async function consumeInbox() {
  inboxLoading.value = true
  try {
    const r = await api.scanInbox()
    ElMessage.success(`已消费 ${r?.consumed ?? 0} 条 pending 导入`)
    loadInbox()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    inboxLoading.value = false
  }
}

async function loadInbox() {
  inboxLoading.value = true
  inboxError.value = ''
  try {
    inbox.value = await api.scanInboxResult(20)
  } catch (e) {
    inboxError.value = e.message
  } finally {
    inboxLoading.value = false
  }
}

async function loadHits() {
  hitsLoading.value = true
  hitsError.value = ''
  try {
    hits.value = (await api.scanHits(20)) || []
  } catch (e) {
    hitsError.value = e.message
  } finally {
    hitsLoading.value = false
  }
}

function verdictMeta(v) {
  return { normal: { type: 'success', label: '正常' }, suspicious: { type: 'warning', label: '可疑' }, fraud: { type: 'danger', label: '诈骗' } }[v] || { type: 'info', label: v }
}

function inboxRows() {
  const raw = inbox.value
  if (!raw) return []
  if (Array.isArray(raw)) return raw
  if (raw.items && Array.isArray(raw.items)) return raw.items
  return []
}
const inboxCount = () => {
  const raw = inbox.value
  if (!raw) return 0
  return typeof raw === 'number' ? raw : raw.pending ?? raw.total ?? inboxRows().length
}

onMounted(() => {
  loadHits()
  loadInbox()
})
</script>

<template>
  <div class="af-page">
    <el-row :gutter="16">
      <!-- 话术检测 -->
      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>话术检测</b>
              <span class="af-muted">（POST /scan/text 实时判定）</span>
            </div>
          </template>
          <el-input v-model="form.text" type="textarea" :rows="5" placeholder="粘贴来自评论区/私信的疑似诈骗话术…" />
          <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px">
            <el-select v-model="form.source" style="width: 120px">
              <el-option label="手动" value="manual" />
              <el-option label="知乎" value="zhihu" />
            </el-select>
            <el-button type="primary" :loading="scanning" @click="doScan">检测</el-button>
          </div>
          <el-alert v-if="scanError" type="error" :title="scanError" :closable="false" style="margin-top: 12px" />
          <el-card v-if="scanResult" shadow="never" style="margin-top: 12px; border: 1px solid #e4e7ed">
            <div style="display: flex; align-items: center; gap: 12px">
              <b>判定：</b>
              <el-tag :type="verdictMeta(scanResult.verdict).type" size="medium">{{ verdictMeta(scanResult.verdict).label }}</el-tag>
              <span class="af-mono">规则分 {{ scanResult.rule_score }}</span>
              <el-tag v-if="scanResult.hit_count" type="danger" effect="plain" size="small">踩饵命中 ×{{ scanResult.hit_count }}</el-tag>
            </div>
            <pre class="af-mono" style="margin: 12px 0 0; font-size: 12px; white-space: pre-wrap; max-height: 200px; overflow: auto">{{ JSON.stringify(scanResult, null, 2) }}</pre>
          </el-card>
        </el-card>
      </el-col>

      <!-- 导入入口 -->
      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>对话导入（inbox）</b>
              <div style="display: flex; gap: 8px">
                <el-button size="small" @click="loadInbox">刷新</el-button>
                <el-button size="small" type="warning" plain :loading="inboxLoading" @click="consumeInbox">消费 pending</el-button>
              </div>
            </div>
          </template>
          <el-input v-model="importForm.text" type="textarea" :rows="4" placeholder="粘贴要导入检测流水线的完整对话文本（来源知乎/微信/QQ…）" />
          <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px">
            <el-input v-model="importForm.fromUrlName" placeholder="来源账号 url_token/昵称（可选）" style="width: 200px" />
            <el-select v-model="importForm.source" style="width: 110px">
              <el-option label="手动" value="manual" />
              <el-option label="知乎" value="zhihu" />
            </el-select>
            <el-button type="primary" plain :loading="importForm.submitting" @click="submitImport">导入</el-button>
          </div>
          <el-alert v-if="inboxError" type="error" :title="inboxError" :closable="false" style="margin-top: 12px" />
          <div style="margin-top: 12px">
            <span class="af-muted">inbox 队列：pending {{ inboxCount() }} 条，最近 20 条：</span>
          </div>
          <el-table :data="inboxRows()" size="small" max-height="200" v-loading="inboxLoading" style="margin-top: 8px; cursor: pointer" highlight-current-row @row-click="openHitDetail">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column label="来源" width="100">
              <template #default="{ row }">{{ row.source || row.from_url_name || '—' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'done' ? 'success' : row.status === 'pending' ? 'warning' : 'info'" size="small" effect="plain">{{ row.status || '—' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="内容摘要" show-overflow-tooltip>
              <template #default="{ row }">{{ row.text || row.content || JSON.stringify(row.payload || {}) }}</template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" width="150" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 最近检测记录 -->
    <el-card shadow="never">
      <template #header><b>最近检测记录</b><span class="af-muted" style="margin-left: 8px">（/scan/hits 最近 20 条）</span></template>
      <el-alert v-if="hitsError" type="error" :title="hitsError" :closable="false" style="margin-bottom: 12px" />
      <el-table :data="hits" v-loading="hitsLoading" size="small" highlight-current-row @row-click="openHitDetail" style="cursor: pointer">
        <el-table-column prop="id" label="ID" width="70" />
        <el-table-column label="判定" width="90">
          <template #default="{ row }"><el-tag :type="verdictMeta(row.verdict).type" size="small">{{ row.verdict || '—' }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="source" label="来源" width="100" />
        <el-table-column label="规则分" width="80">
          <template #default="{ row }">
            <span :class="(row.rule_score || 0) >= 10 ? 'text-danger' : (row.rule_score || 0) >= 6 ? 'text-warning' : 'text-success'">{{ row.rule_score }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="hit_count" label="命中" width="70" />
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ row.created_at }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>