<script setup>
// 话术检测：粘贴检测（POST /scan/text 实时返回）+ 命中列表 + CH-D 手动导入
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

const form = reactive({ text: '', source: 'manual' })
const scanning = ref(false)
const result = ref(null)
const scanError = ref('')

const hits = ref([])
const hitsLoading = ref(false)
const hitsError = ref('')

// 详情抽屉（四要素：告警原因+上下文+平台跳转+详细分析）
const detailDrawer = reactive({ show: false, detectionId: null })

const importDialog = reactive({ show: false, text: '', fromUrlName: '', source: 'manual', submitting: false })

async function doScan() {
  if (!form.text.trim()) {
    ElMessage.warning('请输入待检测文本')
    return
  }
  scanning.value = true
  scanError.value = ''
  try {
    result.value = await api.scanText(form.text.trim(), form.source)
    loadHits()
  } catch (e) {
    scanError.value = e.message
    result.value = null
  } finally {
    scanning.value = false
  }
}

async function loadHits() {
  hitsLoading.value = true
  hitsError.value = ''
  try {
    hits.value = (await api.scanHits(30)) || []
  } catch (e) {
    hitsError.value = e.message
  } finally {
    hitsLoading.value = false
  }
}
loadHits()

function verdictMeta(v) {
  return { normal: { type: 'success', label: '正常' }, suspicious: { type: 'warning', label: '可疑' }, fraud: { type: 'danger', label: '诈骗' } }[v] || { type: 'info', label: v }
}

// 命中行「详情」按钮 → 打开检测详情抽屉（hits 行 id 即 detection_id /scan/hits）
function openHitDetail(row) {
  const detId = row?.id ?? row?.detection_id
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}

async function submitImport() {
  if (!importDialog.text.trim()) {
    ElMessage.warning('请输入要导入的对话文本')
    return
  }
  importDialog.submitting = true
  try {
    const r = await api.zhihuImport(importDialog.text.trim(), importDialog.source, importDialog.fromUrlName)
    ElMessage.success(`导入成功（detection_id=${r.detection_id ?? r.id ?? '?'}），进入检测流水线`)
    importDialog.show = false
    loadHits()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    importDialog.submitting = false
  }
}
</script>

<template>
  <div class="af-page">
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>话术检测</b>
              <el-button size="small" @click="importDialog.show = true">CH-D 手动导入</el-button>
            </div>
          </template>
          <el-form label-position="top">
            <el-form-item label="待检测文本（私信/评论/对话）">
              <el-input v-model="form.text" type="textarea" :rows="6" placeholder="粘贴内容，点击检测…（规则预筛 + LLM 复核可用时）" />
            </el-form-item>
            <el-form-item label="来源">
              <el-radio-group v-model="form.source">
                <el-radio value="manual">手动</el-radio>
                <el-radio value="zhihu">知乎</el-radio>
                <el-radio value="import">导入</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-button type="primary" :loading="scanning" @click="doScan">开始检测</el-button>
          </el-form>
        </el-card>

        <el-card v-if="result" shadow="never" class="af-card">
          <template #header><b>检测结果</b></template>
          <el-alert v-if="scanError" type="error" :title="scanError" :closable="false" style="margin-bottom: 12px" />
          <template v-else>
            <el-descriptions :column="3" border size="small">
              <el-descriptions-item label="判定">
                <el-tag :type="verdictMeta(result.verdict).type" size="small">{{ verdictMeta(result.verdict).label }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="严重度">{{ result.severity }}</el-descriptions-item>
              <el-descriptions-item label="分级">
                <el-tag :type="result.grade?.startsWith('L') ? (result.grade >= 'L4' ? 'danger' : 'warning') : 'info'" size="small">{{ result.grade }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="规则分">{{ result.rule_score }}</el-descriptions-item>
              <el-descriptions-item label="置信度">{{ result.judge_confidence }}</el-descriptions-item>
              <el-descriptions-item label="LLM 复核">{{ result.llm_used ? '已用' : '未用(降级)' }}</el-descriptions-item>
              <el-descriptions-item label="分级理由" :span="3">{{ result.grade_reason || result.grade_hint || '—' }}</el-descriptions-item>
            </el-descriptions>

            <el-divider content-position="left">话术命中（{{ (result.hits || []).length }} 条）</el-divider>
            <el-table :data="result.hits || []" size="small">
              <el-table-column prop="category" label="分类" width="140" />
              <el-table-column prop="pattern" label="命中话术" />
              <el-table-column prop="matched_text" label="匹配文本" show-overflow-tooltip />
              <el-table-column prop="score" label="权重" width="70" />
            </el-table>

            <template v-if="result.evidence_tokens?.length">
              <el-divider content-position="left">证据片段</el-divider>
              <el-tag v-for="t in result.evidence_tokens" :key="t" size="small" style="margin-right: 6px">{{ t }}</el-tag>
            </template>
          </template>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header><b>最近检测记录</b><span class="af-muted" style="margin-left: 8px">（/scan/hits）</span></template>
          <el-alert v-if="hitsError" type="error" :title="hitsError" :closable="false" style="margin-bottom: 12px" />
          <!-- P1-2 修复：移除 fixed="right"（固定列在窄容器下绝对定位覆盖相邻列且不可横滚），
               外层 overflow-x:auto + 表格 min-width，窄屏（900px）可横向滚动查看全部列 -->
          <div style="overflow-x: auto">
            <el-table :data="hits" v-loading="hitsLoading" size="small" max-height="640" style="min-width: 700px">
              <el-table-column prop="id" label="ID" width="64" />
              <el-table-column prop="source" label="来源" width="80" />
              <el-table-column prop="rule_score" label="规则分" width="80" />
              <el-table-column label="命中" width="70">
                <template #default="{ row }">
                  <el-tag :type="row.hit_count > 0 ? 'danger' : 'info'" size="small">{{ row.hit_count }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="judge_confidence" label="置信度" width="90" />
              <el-table-column prop="created_at" label="时间" width="160" />
              <el-table-column label="操作" width="70">
                <template #default="{ row }">
                  <el-button
                    v-if="row?.id ?? row?.detection_id"
                    size="small"
                    type="primary"
                    plain
                    @click.stop="openHitDetail(row)"
                  >详情</el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- CH-D 手动导入 -->
    <el-dialog v-model="importDialog.show" title="CH-D 手动导入（粘贴对话文本/JSON → 检测流水线）" width="600px">
      <el-form label-position="top">
        <el-form-item label="对话文本">
          <el-input v-model="importDialog.text" type="textarea" :rows="8" placeholder="粘贴知乎私信/评论对话文本或 JSON" />
        </el-form-item>
        <el-form-item label="对方 url_token（可空）">
          <el-input v-model="importDialog.fromUrlName" placeholder="如 some-user-123" />
        </el-form-item>
      </el-form>
      <div class="af-muted">合规：只读自有/公开内容，不自动发布任何东西。</div>
      <template #footer>
        <el-button @click="importDialog.show = false">取消</el-button>
        <el-button type="primary" :loading="importDialog.submitting" @click="submitImport">导入</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>