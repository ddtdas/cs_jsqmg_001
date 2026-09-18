<script setup>
// 聊天记录一键导入（P15）：微信/QQ 候选路径探测 → 扫描识别（平台/库文件/加密） → 一键导入
// （脱敏落库 detections(chat_import) + 推理链分级） → 通用文件导入（TXT/CSV/JSON） → 清空导入记录。
// 合规：仅导入本机授权范围内自有/已授权聊天记录；展示内容一律脱敏展示（content_masked）；
// 记录可一键清空，清空必须显式确认（confirm=true）且 reason≥20 字，操作写 gate_logs 哈希链审计。
import { ref, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'

// ---------- 平台 / 分级元信息 ----------
const FILE_PLATFORM_OPTIONS = [
  { value: '', label: '自动检测' },
  { value: 'wechat', label: '微信' },
  { value: 'qq', label: 'QQ' },
  { value: 'generic', label: '其他（TXT/CSV/JSON）' },
]
const FILE_PLATFORM_LABEL = Object.fromEntries(FILE_PLATFORM_OPTIONS.map((o) => [o.value, o.label]))

const SCAN_PLATFORM_LABEL = {
  wechat: '微信',
  qq: 'QQ',
  generic: '通用文本',
  unknown: '未知',
}

// 分级展示：服务端 grade 可能是 grade_hint 或 severity（英文/中文），统一兜底
function gradeMeta(g) {
  const s = String(g || '').toLowerCase()
  return (
    {
      critical: { type: 'danger', label: '严重' },
      high: { type: 'danger', label: '高危' },
      medium: { type: 'warning', label: '中危' },
      suspicious: { type: 'warning', label: '可疑' },
      low: { type: 'info', label: '低危' },
      normal: { type: 'success', label: '正常' },
    }[s] || { type: 'info', label: g || '—' }
  )
}

// ---------- 探测候选（/chat-import/candidates） ----------
const candidates = ref({ wechat: [], qq: [], custom: '' })
const candidatesLoading = ref(false)
const candidatesError = ref('')

async function loadCandidates() {
  candidatesLoading.value = true
  candidatesError.value = ''
  try {
    const res = await api.chatCandidates()
    const c = res?.candidates || res || {}
    candidates.value = {
      wechat: Array.isArray(c.wechat) ? c.wechat : [],
      qq: Array.isArray(c.qq) ? c.qq : [],
      custom: c.custom || '',
    }
    if (!candidates.value.wechat.length && !candidates.value.qq.length) {
      ElMessage.warning('未探测到微信/QQ 候选路径，可手动输入路径')
    }
  } catch (e) {
    candidatesError.value = e.message
    ElMessage.error(`加载候选失败：${e.message}`)
  } finally {
    candidatesLoading.value = false
  }
}
loadCandidates()

// 当前操作路径（候选选择 / 手动输入 / 扫描 / 导入共用）
const path = ref('')
function onPickCandidate(p) {
  if (p) path.value = p
}

// ---------- 扫描（/chat-import/scan） ----------
const scanning = ref(false)
const scanError = ref('')
const scanResult = ref(null) // {platform, db_files, encrypted}

async function doScan() {
  const p = (path.value || '').trim()
  if (!p) {
    ElMessage.warning('请先选择或输入要扫描的路径')
    return
  }
  scanning.value = true
  scanError.value = ''
  try {
    const res = await api.chatScan(p)
    scanResult.value = res?.result || res || null
    ElMessage.success(`扫描完成：平台 ${scanResult.value?.platform || 'unknown'}`)
  } catch (e) {
    scanError.value = e.message
    scanResult.value = null
    ElMessage.error(`扫描失败：${e.message}`)
  } finally {
    scanning.value = false
  }
}

// ---------- 一键导入分析（/chat-import/import） ----------
const limit = ref(2000)
const importing = ref(false)
const importError = ref('')
const importResult = ref(null) // {imported, suspicious, preview, error?, message?}

async function doImport() {
  const p = (path.value || '').trim()
  if (!p) {
    ElMessage.warning('请先填写要导入的路径')
    return
  }
  const lim = Number(limit.value) > 0 ? Math.floor(Number(limit.value)) : 2000
  importing.value = true
  importError.value = ''
  try {
    const res = await api.chatImport(p, lim)
    importResult.value = res?.result || res || null
    const r = importResult.value
    if (r?.error) {
      ElMessage.error(r.message || '导入失败')
    } else {
      ElMessage.success(`导入完成：${r?.imported ?? 0} 条，可疑 ${r?.suspicious ?? 0} 条`)
    }
  } catch (e) {
    importError.value = e.message
    importResult.value = null
    ElMessage.error(`导入失败：${e.message}`)
  } finally {
    importing.value = false
  }
}

// ---------- 通用文件导入（/chat-import/import-file） ----------
const fileImport = reactive({ file_path: '', platform: '' })
const importingFile = ref(false)
const fileImportError = ref('')
const fileImportResult = ref(null)

async function doImportFile() {
  const fp = (fileImport.file_path || '').trim()
  if (!fp) {
    ElMessage.warning('请填写要导入的文件路径（本机授权导出文件）')
    return
  }
  importingFile.value = true
  fileImportError.value = ''
  try {
    const res = await api.chatImportFile(fp, fileImport.platform || null)
    fileImportResult.value = res?.result || res || null
    const r = fileImportResult.value
    if (r?.error) {
      ElMessage.error(r.message || '文件导入失败')
    } else {
      ElMessage.success(`文件导入完成：${r?.imported ?? 0} 条，可疑 ${r?.suspicious ?? 0} 条`)
    }
  } catch (e) {
    fileImportError.value = e.message
    fileImportResult.value = null
    ElMessage.error(`文件导入失败：${e.message}`)
  } finally {
    importingFile.value = false
  }
}

// ---------- 清空导入记录（/chat-import/imported · DELETE） ----------
const clearForm = reactive({ confirm: false, reason: '' })
const clearing = ref(false)
const clearError = ref('')
const clearedCount = ref(null)

async function doClear() {
  if (!clearForm.confirm) return // 按钮已禁用，双保险
  const reason = (clearForm.reason || '').trim()
  if (reason.length < 20) {
    ElMessage.warning(`清空原因至少 20 字（当前 ${reason.length} 字），用于审计留痕`)
    return
  }
  clearing.value = true
  clearError.value = ''
  try {
    const res = await api.chatClearImported(true, reason)
    clearedCount.value = res?.deleted ?? 0
    ElMessage.success(`已清空 ${clearedCount.value} 条导入记录`)
    clearForm.confirm = false
    clearForm.reason = ''
  } catch (e) {
    clearError.value = e.message
    ElMessage.error(`清空失败：${e.message}`)
  } finally {
    clearing.value = false
  }
}
</script>

<template>
  <div class="af-page">
    <!-- 顶部合规声明 -->
    <el-alert type="info" :closable="false" show-icon title="合规与数据安全声明" style="margin-bottom: 16px">
      <div style="line-height: 1.8">
        1. <b>仅限本机授权范围内</b>自有/已授权聊天记录的导入（微信/QQ 本地库或导出文件），不扫描全盘；
        2. 导入内容先脱敏再展示与落库（<b>content_masked</b>），原始内容不直接呈现；
        3. 导入记录可<b>一键清空</b>（需勾选显式确认并填写 ≥20 字审计原因，操作写入哈希链审计日志）。
      </div>
    </el-alert>

    <!-- 探测候选 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>探测候选</b>
            <span class="af-muted">（/chat-import/candidates + /scan）</span>
          </div>
          <el-button size="small" :loading="candidatesLoading" @click="loadCandidates">刷新候选</el-button>
        </div>
      </template>

      <el-alert v-if="candidatesError" type="error" :title="candidatesError" :closable="false" style="margin-bottom: 12px" />

      <div class="af-muted" style="margin-bottom: 8px; line-height: 1.7">
        仅探测固定候选目录（不扫全盘）。点击候选路径自动填入下方输入框，也可手动输入任意目录或导出文件后进行扫描识别。
        <span v-if="!candidates.wechat.length && !candidates.qq.length">当前未探测到候选路径。</span>
      </div>

      <el-select
        :model-value="path"
        placeholder="选择探测到的微信/QQ 候选路径"
        style="width: 100%"
        clearable
        filterable
        @change="onPickCandidate"
      >
        <el-option-group v-if="candidates.wechat.length" label="微信候选路径">
          <el-option v-for="p in candidates.wechat" :key="'wx-' + p" :label="p" :value="p" />
        </el-option-group>
        <el-option-group v-if="candidates.qq.length" label="QQ 候选路径">
          <el-option v-for="p in candidates.qq" :key="'qq-' + p" :label="p" :value="p" />
        </el-option-group>
        <el-option v-if="candidates.custom" :label="`AF_CHAT_ROOT（自定义）：${candidates.custom}`" :value="candidates.custom" />
      </el-select>

      <div style="display: flex; gap: 8px; margin-top: 10px; align-items: center">
        <el-input
          v-model="path"
          placeholder="或手动输入微信/QQ 目录或导出文件路径"
          clearable
          style="flex: 1"
          @keyup.enter="doScan"
        />
        <el-button type="primary" :loading="scanning" :disabled="!(path || '').trim()" @click="doScan">扫描</el-button>
      </div>

      <el-alert v-if="scanError" type="error" :title="scanError" :closable="false" style="margin-top: 12px" />

      <!-- 扫描结果 -->
      <template v-if="scanResult">
        <el-descriptions :column="1" size="small" border style="margin-top: 14px">
          <el-descriptions-item label="平台">
            <el-tag size="small" :type="scanResult.platform === 'wechat' || scanResult.platform === 'qq' ? 'primary' : 'info'">
              {{ SCAN_PLATFORM_LABEL[scanResult.platform] || scanResult.platform || '未知' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="数据库文件">
            <div v-if="(scanResult.db_files || []).length">
              <div v-for="(f, i) in scanResult.db_files" :key="i" class="af-mono" style="font-size: 12px; line-height: 1.8">{{ f }}</div>
            </div>
            <span v-else class="af-muted">未发现消息库文件</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="scanResult.encrypted"
          type="warning"
          :closable="false"
          show-icon
          title="检测到加密数据库（SQLCipher）"
          description="该消息库已加密保护，无法直接读取解析。请在对应客户端中导出明文聊天记录（TXT/CSV/JSON），再通过下方「通用文件导入」导入。"
          style="margin-top: 12px"
        />
      </template>
    </el-card>

    <!-- 导入分析 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>导入分析</b>
            <span class="af-muted">（/chat-import/import · 解析 → 脱敏 → 落库 → 推理链分级）</span>
          </div>
          <el-button
            type="primary"
            :loading="importing"
            :disabled="!(path || '').trim()"
            @click="doImport"
          >
            导入
          </el-button>
        </div>
      </template>

      <el-form inline style="margin-bottom: 4px">
        <el-form-item label="路径">
          <el-input :model-value="path" readonly placeholder="先在上方探测/扫描或手动输入路径" style="width: 460px" />
        </el-form-item>
        <el-form-item label="条数上限">
          <el-input-number v-model="limit" :min="1" :max="50000" :step="500" />
        </el-form-item>
      </el-form>

      <el-alert v-if="importError" type="error" :title="importError" :closable="false" style="margin-bottom: 12px" />

      <!-- 导入结果 -->
      <template v-if="importResult">
        <el-alert
          v-if="importResult.error"
          type="error"
          :closable="false"
          show-icon
          :title="importResult.error === 'encrypted_db' ? '数据库已加密，无法直接导入' : '导入失败'"
          :description="importResult.message || '请在客户端导出明文聊天记录后使用「通用文件导入」'"
          style="margin-bottom: 12px"
        />
        <div v-else style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px">
          <el-tag type="success" effect="plain">导入 {{ importResult.imported ?? 0 }} 条</el-tag>
          <el-tag :type="(importResult.suspicious || 0) > 0 ? 'danger' : 'info'" effect="plain">
            可疑 {{ importResult.suspicious ?? 0 }} 条
          </el-tag>
          <span class="af-muted">预览（最多 3 条 · 已脱敏）</span>
        </div>

        <el-table :data="importResult.preview || []" v-loading="importing" size="small" empty-text="暂无预览条目">
          <el-table-column label="#" type="index" width="50" />
          <el-table-column prop="sender" label="发送者" min-width="120" show-overflow-tooltip>
            <template #default="{ row }">{{ row.sender || '—' }}</template>
          </el-table-column>
          <el-table-column prop="time" label="时间" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.time || '—' }}</template>
          </el-table-column>
          <el-table-column prop="content_masked" label="内容（已脱敏）" min-width="320" show-overflow-tooltip />
          <el-table-column label="分级" width="110">
            <template #default="{ row }">
              <el-tag :type="gradeMeta(row.grade).type" size="small">{{ gradeMeta(row.grade).label }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <!-- 通用文件导入 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>通用文件导入</b>
            <span class="af-muted">（/chat-import/import-file · TXT / CSV / JSON）</span>
          </div>
        </div>
      </template>

      <div class="af-muted" style="margin-bottom: 10px; line-height: 1.7">
        适用于客户端导出的明文聊天记录：TXT（每行一条）、CSV（header 含 content/sender/time）、
        JSON（数组 [{sender,time,content}]）。平台可自动检测，也可显式指定。
      </div>

      <div style="display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 10px">
        <el-form-item label="文件路径" style="margin-bottom: 0">
          <el-input
            v-model="fileImport.file_path"
            placeholder="本机授权导出文件，如 D:\export\chat.json"
            clearable
            style="width: 480px"
            @keyup.enter="doImportFile"
          />
        </el-form-item>
        <el-form-item label="平台" style="margin-bottom: 0">
          <el-select v-model="fileImport.platform" style="width: 220px">
            <el-option v-for="o in FILE_PLATFORM_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-button
          type="primary"
          :loading="importingFile"
          :disabled="!(fileImport.file_path || '').trim()"
          @click="doImportFile"
        >
          导入文件
        </el-button>
      </div>

      <el-alert v-if="fileImportError" type="error" :title="fileImportError" :closable="false" style="margin-bottom: 12px" />

      <template v-if="fileImportResult">
        <el-alert
          v-if="fileImportResult.error"
          type="error"
          :closable="false"
          show-icon
          :title="fileImportResult.error === 'encrypted_db' ? '数据库已加密，无法直接导入' : '导入失败'"
          :description="fileImportResult.message || '请检查文件格式或改用平台导出功能'"
        />
        <div v-else style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
          <el-tag type="success" effect="plain">导入 {{ fileImportResult.imported ?? 0 }} 条</el-tag>
          <el-tag :type="(fileImportResult.suspicious || 0) > 0 ? 'danger' : 'info'" effect="plain">
            可疑 {{ fileImportResult.suspicious ?? 0 }} 条
          </el-tag>
          <span v-if="fileImportResult.message" class="af-muted">{{ fileImportResult.message }}</span>
          <span class="af-muted">平台：{{ FILE_PLATFORM_LABEL[fileImport.platform] || '自动检测' }}</span>
        </div>
      </template>
    </el-card>

    <!-- 清空导入记录 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div class="af-table-toolbar" style="justify-content: space-between; align-items: center">
          <div>
            <b>清空导入记录</b>
            <span class="af-muted">（/chat-import/imported · DELETE）</span>
          </div>
        </div>
      </template>

      <div class="af-muted" style="margin-bottom: 10px; line-height: 1.7">
        一键清空 chat_import 来源的全部检测记录。该操作
        <el-tag size="small" type="danger" effect="plain">不可恢复</el-tag>
        ，须先勾选显式确认并填写不少于 20 字的审计原因；操作将写入 gate_logs 哈希链审计日志。
      </div>

      <el-checkbox v-model="clearForm.confirm">我已确认：清空全部导入记录（不可恢复）</el-checkbox>
      <el-input
        v-model="clearForm.reason"
        type="textarea"
        :rows="2"
        :maxlength="200"
        show-word-limit
        placeholder="清空原因（不少于 20 字，用于审计留痕）"
        style="margin: 10px 0"
      />

      <el-alert v-if="clearError" type="error" :title="clearError" :closable="false" style="margin-bottom: 12px" />

      <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
        <el-button type="danger" plain :disabled="!clearForm.confirm" :loading="clearing" @click="doClear">
          清空导入记录
        </el-button>
        <el-alert
          v-if="clearedCount !== null"
          type="success"
          :closable="true"
          :title="`已清空 ${clearedCount} 条导入记录（审计已留痕）`"
          style="flex: 1"
        />
      </div>
    </el-card>
  </div>
</template>