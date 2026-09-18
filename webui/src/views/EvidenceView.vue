<script setup>
// 证据包：构建（选择检测记录）→ 详情/哈希链 → 冻结 → 导出(json/text) → 举报模板
import { reactive, ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

const hits = ref([])
const selectedIds = ref([])
const building = ref(false)
const error = ref('')
const built = ref(null)

// 详情抽屉（四要素）：证据包关联检测 det_ids 可逐条打开检测详情
const detailDrawer = reactive({ show: false, detectionId: null })

function openDet(detId) {
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}

// 证据包关联检测 id 列表：构建结果 built.det_ids 或详情 detail.package.det_ids
function pkgDetIds() {
  const ids = []
  for (const src of [built.value?.det_ids, detail.value?.package?.det_ids]) {
    for (const i of src || []) {
      const n = Number(i)
      if (Number.isFinite(n) && !ids.includes(n)) ids.push(n)
    }
  }
  return ids
}

const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const pkgQuery = ref('')

onMounted(async () => {
  try {
    hits.value = (await api.scanHits(30)) || []
  } catch (e) {
    error.value = e.message
  }
})

async function build() {
  if (!selectedIds.value.length) {
    ElMessage.warning('请至少选择一条检测记录')
    return
  }
  building.value = true
  error.value = ''
  try {
    built.value = await api.buildEvidence(selectedIds.value.map(Number))
    // P2 防呆：构建成功后自动回填详情/校验输入框，消除强制二次输入
    pkgQuery.value = String(built.value.pkg_id)
    ElMessage.success(`证据包 #${built.value.pkg_id} 已构建`)
  } catch (e) {
    error.value = e.message
  } finally {
    building.value = false
  }
}

async function loadDetail(pkgId) {
  const q = pkgId ?? pkgQuery.value
  if (!q) {
    ElMessage.warning('请输入证据包 ID')
    return
  }
  detailLoading.value = true
  detailError.value = ''
  try {
    detail.value = await api.getEvidence(Number(q))
    built.value = { pkg_id: Number(q) }
  } catch (e) {
    detailError.value = e.message
    detail.value = null
  } finally {
    detailLoading.value = false
  }
}

// P1-A：冻结为敏感操作（confirm=true + reason≥20，GateLog 审计）——弹窗二次确认
const freezeVisible = ref(false)
const freezeConfirm = ref(false)
const freezeReason = ref('')
const freezing = ref(false)
const freezeTarget = ref(null)

const canFreeze = computed(() => freezeConfirm.value && freezeReason.value.trim().length >= 20)

function openFreeze(pkgId) {
  freezeTarget.value = pkgId
  freezeConfirm.value = false
  freezeReason.value = ''
  freezeVisible.value = true
}

async function onFreeze() {
  if (!canFreeze.value || freezeTarget.value == null) return
  freezing.value = true
  try {
    const r = await api.freezeEvidence(freezeTarget.value, freezeReason.value.trim())
    ElMessage.success(`冻结成功：${r.detail || "证据包已锁定"}`)
    freezeVisible.value = false
    freezeReason.value = ''
    freezeConfirm.value = false
    loadDetail()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    freezing.value = false
  }
}

async function exportPkg(pkgId, fmt) {
  try {
    const r = await api.exportEvidence(pkgId, fmt)
    const blob = new Blob([fmt === 'text' ? r.content : JSON.stringify(r.content, null, 2)], { type: fmt === 'text' ? 'text/plain' : 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `evidence_${pkgId}.${fmt === 'text' ? 'txt' : 'json'}`
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success('已导出')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function tpl(pkgId, kind) {
  try {
    const r = await api.reportTemplate(pkgId, kind)
    const blob = new Blob([r.text], { type: 'text/plain' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `report_template_${kind}_pkg${pkgId}.txt`
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success(`已导出 ${kind} 举报模板`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <div class="af-page">
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header><b>1 · 构建证据包</b><span class="af-muted" style="margin-left: 8px">（从检测记录选择）</span></template>
          <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-bottom: 12px" />
          <el-table :data="hits" size="small" max-height="360" @selection-change="(rows) => (selectedIds = rows.map((r) => r.id))">
            <el-table-column type="selection" width="44" />
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="rule_score" label="规则分" width="70" />
            <el-table-column label="命中" width="70">
              <template #default="{ row }">{{ row.hit_count }}</template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" />
          </el-table>
          <el-button type="primary" style="margin-top: 12px" :loading="building" @click="build">构建证据包（哈希链）</el-button>
        </el-card>

        <el-card v-if="built" shadow="never" class="af-card">
          <template #header><b>2 · 证据包 #{{ built.pkg_id }} 操作</b></template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="证据包编号">#{{ built.pkg_id }}</el-descriptions-item>
            <el-descriptions-item label="检测记录">{{ (built.det_ids || []).join(', ') }}</el-descriptions-item>
            <el-descriptions-item label="截图">{{ (built.screenshots || []).length }} 张</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ built.created_at }}</el-descriptions-item>
            <el-descriptions-item label="哈希链节点" :span="2">{{ (built.hash_chain || []).length }}</el-descriptions-item>
          </el-descriptions>
          <!-- DetailDrawer 接入点：证据包关联检测逐条查看详情 -->
          <div v-if="pkgDetIds().length" style="margin-top: 10px">
            <span class="af-muted" style="font-size: 12px; margin-right: 8px">关联检测详情：</span>
            <el-tag
              v-for="did in pkgDetIds()"
              :key="did"
              size="small"
              type="primary"
              effect="plain"
              style="cursor: pointer; margin-right: 6px"
              @click="openDet(did)"
            >检测 #{{ did }} →</el-tag>
          </div>
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button size="small" type="primary" @click="loadDetail(built.pkg_id)">详情/校验</el-button>
            <el-button size="small" type="warning" @click="openFreeze(built.pkg_id)">冻结（哈希不可变）</el-button>
            <el-button size="small" @click="exportPkg(built.pkg_id, 'json')">导出 JSON</el-button>
            <el-button size="small" @click="exportPkg(built.pkg_id, 'text')">导出文本</el-button>
            <el-button size="small" type="success" @click="tpl(built.pkg_id, '96110')">96110 模板</el-button>
            <el-button size="small" type="success" plain @click="tpl(built.pkg_id, 'platform')">平台举报</el-button>
            <el-button size="small" type="success" plain @click="tpl(built.pkg_id, 'pibyao')">辟谣</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header><b>证据包详情 / 完整性校验</b></template>
          <div style="display: flex; gap: 8px; max-width: 420px; margin-bottom: 12px">
            <el-input v-model="pkgQuery" placeholder="证据包 ID" />
            <el-button :loading="detailLoading" @click="loadDetail">查询</el-button>
          </div>
          <el-alert v-if="detailError" type="error" :title="detailError" :closable="false" style="margin-bottom: 12px" />
          <template v-if="detail">
            <el-alert
              :type="detail.verified?.intact ? 'success' : 'error'"
              :closable="false"
              show-icon
              style="margin-bottom: 12px"
              :title="detail.verified?.intact ? '哈希链完整 ✓' : '内容已被改动！'"
            >
              {{ detail.verified?.detail }} · 节点数 {{ detail.verified?.nodes }} · 冻结 {{ detail.verified?.frozen ? '是' : '否' }}
            </el-alert>
            <pre class="af-mono" style="max-height: 460px; overflow: auto; background: #f5f7fa; padding: 12px; border-radius: 6px">{{ JSON.stringify(detail, null, 2) }}</pre>
          </template>
        </el-card>
      </el-col>
    </el-row>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />

    <!-- P1-A：冻结二次确认弹窗（confirm=true + reason≥20，GateLog 审计） -->
    <el-dialog
      v-model="freezeVisible"
      title="冻结证据包（二次确认）"
      width="480px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 16px">
        <div>冻结后证据包哈希<strong>不可变</strong>（frozen=1），冻结前内容任何变更将导致完整性校验失败；本操作为敏感操作，将写入 GateLog 审计链。</div>
      </el-alert>
      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="freezeConfirm">
            我确认要冻结证据包 #{{ freezeTarget }}
          </el-checkbox>
        </el-form-item>
        <el-form-item
          label="冻结理由（≥20 字，写入审计日志 GateLog）"
          :error="freezeReason.trim().length > 0 && freezeReason.trim().length < 20 ? `至少 20 字，当前 ${freezeReason.trim().length} 字` : ''"
        >
          <el-input
            v-model="freezeReason"
            type="textarea"
            :rows="3"
            maxlength="200"
            show-word-limit
            placeholder="例如：证据包内容确认无误，冻结固定哈希链作为存证"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="freezeVisible = false">取消</el-button>
        <el-button type="warning" :loading="freezing" :disabled="!canFreeze" @click="onFreeze">
          确认冻结
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>