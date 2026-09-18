<script setup>
// 蜜饵管理：列表 + 创建草稿 + generate-draft（HITL）+ deploy/monitor/disable/retire + 命中详情。
// 实时链路：后端 WS 未落地 → 5s 轮询刷新列表（新命中 hit_count 变化即见），命中详情面板可 check-hit（POST）。
import { reactive, ref, computed } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'
import { usePolling } from '../composables/usePolling'

const filter = ref('')
const list = ref([])

// 5s 轮询实时（审查预检：轮询错误不再静默——error 由页面 alert 展示；首轮拉取由 usePolling immediate 承担，
// 不再与 onMounted 手动 load() 重复请求）
const { refresh, error: pollError, loading: pollLoading } = usePolling(async () => {
  list.value = (await api.listTraps(filter.value || undefined)) || []
}, 5000)

function onFilter(v) {
  filter.value = v
  refresh()
}

const statusMeta = {
  draft: { type: 'info', label: '草稿' },
  active: { type: 'success', label: '已部署' },
  monitored: { type: 'warning', label: '监控中' },
  hit: { type: 'danger', label: '已命中' },
  retired: { type: 'info', label: '已退役' },
}
function stMeta(s) {
  return statusMeta[s] || { type: 'info', label: s }
}

// ---------------- 创建 / 生成草稿 ----------------
// R4-F6：generate-draft platform 后端限枚举（api/traps.py _ALLOWED_PLATFORMS），
// 前端改为下拉选择，避免自由文本触发 422 invalid_platform。
const platformOptions = ['评论区', '私信', '动态', '问答', '想法', '文章', '视频', '直播', '微信群', 'QQ群', '群聊', '邮件', '短信', '其他']
const draftDialog = reactive({ show: false, mode: 'manual', baitText: '', note: '', platform: '评论区', templateId: '', generated: false, submitting: false })

function openManual() {
  draftDialog.show = true
  draftDialog.mode = 'manual'
  draftDialog.baitText = ''
  draftDialog.note = ''
}
function openGenerate() {
  draftDialog.show = true
  draftDialog.mode = 'generate'
  draftDialog.baitText = ''
  draftDialog.note = ''
  draftDialog.platform = '评论区'
  draftDialog.templateId = ''
  draftDialog.generated = false
}

async function submitDraft() {
  const needSave = draftDialog.mode === 'manual' || draftDialog.generated
  if (needSave && draftDialog.baitText.trim().length < 4) {
    ElMessage.warning('蜜饵文案至少 4 字')
    return
  }
  draftDialog.submitting = true
  try {
    if (needSave) {
      const d = await api.createTrap(draftDialog.baitText.trim(), draftDialog.note || undefined)
      ElMessage.success(`草稿 #${d.id} 已保存（伪装度 ${d.disguise_score}）`)
      draftDialog.show = false
      refresh()
    } else {
      const d = await api.generateTrapDraft(draftDialog.templateId || undefined, draftDialog.platform, draftDialog.note || undefined)
      draftDialog.baitText = d.bait_text
      draftDialog.generated = true
      ElMessage.success('AI 草稿已生成，可编辑后保存')
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    draftDialog.submitting = false
  }
}

async function regenerateDraft() {
  draftDialog.submitting = true
  try {
    const d = await api.generateTrapDraft(draftDialog.templateId || undefined, draftDialog.platform, draftDialog.note || undefined)
    draftDialog.baitText = d.bait_text
    ElMessage.success('已重新生成，可编辑后保存')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    draftDialog.submitting = false
  }
}

// ---------------- 状态操作（对应对齐后端状态机）----------------
// P1-3：退役为敏感操作（confirm=true + reason≥20 字双确认），改用弹窗交互
//（对齐 SettingsView reset-key 模式），不再直接 confirm → 403。
const retireDialog = reactive({ show: false, trap: null, confirm: false, reason: '', submitting: false })

function openRetire(trap) {
  retireDialog.trap = trap
  retireDialog.confirm = false
  retireDialog.reason = ''
  retireDialog.show = true
}

async function submitRetire() {
  if (!retireDialog.confirm) {
    ElMessage.warning('请先勾选确认框')
    return
  }
  const reason = (retireDialog.reason || '').trim()
  if (reason.length < 20) {
    ElMessage.warning(`退役理由至少 20 字，当前 ${reason.length} 字`)
    return
  }
  retireDialog.submitting = true
  try {
    await api.retireTrap(retireDialog.trap.id, reason)
    ElMessage.success('退役成功（已写入 GateLog 审计）')
    retireDialog.show = false
    refresh()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    retireDialog.submitting = false
  }
}

async function doAction(trap, action) {
  try {
    if (action === 'deploy') {
      const { value } = await ElMessageBox.prompt('系统不会替你发布：请先去平台手动发布，再把链接粘贴回来（可选）', '标记为已手动发布', {
        inputPlaceholder: 'https://www.zhihu.com/...',
      })
      await api.deployTrap(trap.id, value || undefined)
    } else if (action === 'monitor') {
      await ElMessageBox.confirm('标记进入监控（active→monitored）？', '进入监控', { type: 'warning', autofocus: false }).then(() => api.monitorTrap(trap.id))
    } else if (action === 'disable') {
      await ElMessageBox.confirm('停用该蜜饵？停用后不再参与踩饵检测。', '停用', { type: 'warning', autofocus: false }).then(() => api.disableTrap(trap.id))
    } else if (action === 'retire') {
      openRetire(trap)
      return  // 由弹窗提交，退出此处，不显示下方统一成功提示
    }
    ElMessage.success({ deploy: '已部署', monitor: '已进入监控', disable: '已停用' }[action] || '操作成功')
    refresh()
  } catch (e) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e.message || '操作失败')
  }
}

// ---------------- 命中详情（对齐 Mellivora TrapDetailPanel）----------------
const detail = ref(null)
const detailLoading = ref(false)
const checkText = ref('')
const checkResult = ref(null)
const checkLoading = ref(false)

// el-drawer 可见性计算属性（v-model 需可写表达式）
const detailVisible = computed({
  get: () => !!detail.value,
  set: (v) => {
    if (!v) detail.value = null
  },
})

async function openDetail(trap) {
  detailLoading.value = true
  checkResult.value = null
  try {
    detail.value = await api.getTrap(trap.id)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    detailLoading.value = false
  }
}

async function runCheckHit() {
  if (!checkText.value.trim()) {
    ElMessage.warning('请输入候选文本')
    return
  }
  checkLoading.value = true
  try {
    checkResult.value = await api.checkTrapHit(detail.value.id, checkText.value.trim())
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    checkLoading.value = false
  }
}

const canAction = (trap, action) => {
  const s = trap.status
  if (action === 'deploy') return s === 'draft'
  if (action === 'monitor') return s === 'active'
  if (action === 'disable') return s === 'active' || s === 'monitored'
  if (action === 'retire') return s !== 'retired'
  return false
}
</script>

<template>
  <div class="af-page">
    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
          <div style="display: flex; align-items: center; gap: 12px">
            <b>蜜饵管理</b>
            <el-select :model-value="filter" placeholder="全部状态" clearable style="width: 130px" @change="onFilter">
              <el-option v-for="(m, k) in statusMeta" :key="k" :label="m.label" :value="k" />
            </el-select>
            <el-tag type="warning" size="small">5s 轮询实时（后端无 WS，降级）</el-tag>
          </div>
          <div>
            <el-button type="primary" @click="openManual">创建草稿</el-button>
            <el-button type="success" @click="openGenerate">AI 生成草稿</el-button>
          </div>
        </div>
      </template>

      <el-alert v-if="pollError" type="error" :title="pollError.message" :closable="false" style="margin-bottom: 12px" />
      <!-- P1-2 同源修复：移除 fixed="right"（900px 下"创建时间"列被操作列 100% 覆盖），
           外层 overflow-x:auto + min-width，窄屏可横向滚动查看全部列 -->
      <div style="overflow-x: auto">
        <el-table :data="list" v-loading="pollLoading" size="small" @row-click="openDetail" style="cursor: pointer; min-width: 900px">
          <el-table-column prop="id" label="ID" width="64" />
          <el-table-column prop="bait_text" label="蜜饵文案" show-overflow-tooltip />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag :type="stMeta(row.status).type" size="small">{{ stMeta(row.status).label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="disguise_score" label="伪装度" width="90">
            <template #default="{ row }"><span :class="row.disguise_score >= 70 ? 'text-success' : 'text-warning'">{{ row.disguise_score }}</span></template>
          </el-table-column>
          <el-table-column prop="hit_count" label="命中" width="70" />
          <el-table-column prop="created_at" label="创建时间" width="160" />
          <el-table-column label="操作" width="300">
            <template #default="{ row }">
              <el-button v-if="canAction(row, 'deploy')" size="small" type="primary" @click.stop="doAction(row, 'deploy')">标记为已手动发布</el-button>
              <el-button v-if="canAction(row, 'monitor')" size="small" type="warning" @click.stop="doAction(row, 'monitor')">监控</el-button>
              <el-button v-if="canAction(row, 'disable')" size="small" @click.stop="doAction(row, 'disable')">停用</el-button>
              <el-button v-if="canAction(row, 'retire')" size="small" type="danger" plain @click.stop="doAction(row, 'retire')">退役</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <!-- 创建/生成草稿对话框 -->
    <el-dialog v-model="draftDialog.show" :title="draftDialog.mode === 'manual' ? '创建蜜饵草稿' : 'AI 生成蜜饵草稿（HITL：不自动发布）'" width="560px">
      <el-form label-position="top">
        <template v-if="draftDialog.mode === 'generate'">
          <el-form-item label="生成模板（可空，默认随机离线模板）">
            <el-input v-model="draftDialog.templateId" placeholder="t_invest_flow 等（留空自动）" />
          </el-form-item>
          <el-form-item label="埋设位置提示">
            <el-select v-model="draftDialog.platform" style="width: 100%">
              <el-option v-for="p in platformOptions" :key="p" :label="p" :value="p" />
            </el-select>
          </el-form-item>
        </template>
        <el-form-item :label="draftDialog.mode === 'manual' ? '蜜饵文案（≥4 字）' : '生成结果（生成后回填，可再编辑保存）'">
          <el-input v-model="draftDialog.baitText" type="textarea" :rows="4" placeholder="请输入蜜饵文案…" />
        </el-form-item>
        <el-form-item label="备注（可选）">
          <el-input v-model="draftDialog.note" placeholder="用途/埋设点说明" />
        </el-form-item>
      </el-form>
      <div class="af-muted mb16">⚠️ HITL：系统只产草稿与指纹，发布由你手动执行后再调用「标记为已手动发布」标记。</div>
      <template #footer>
        <el-button @click="draftDialog.show = false">取消</el-button>
        <el-button v-if="draftDialog.mode === 'generate' && draftDialog.generated" type="warning" plain :loading="draftDialog.submitting" @click="regenerateDraft">重新生成</el-button>
        <el-button type="primary" :loading="draftDialog.submitting" @click="submitDraft">
          {{ draftDialog.mode === 'manual' || draftDialog.generated ? '保存草稿' : '生成草稿' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 退役双确认弹窗（P1-3：confirm=true + reason≥20 字，GateLog 审计） -->
    <el-dialog
      v-model="retireDialog.show"
      title="退役蜜饵（二次确认）"
      width="480px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 16px">
        <div>退役后该蜜饵不再参与踩饵检测（记录保留）。该操作为敏感操作，将写入 GateLog 审计日志。</div>
      </el-alert>
      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="retireDialog.confirm">
            我确认要退役蜜饵 #{{ retireDialog.trap?.id }}
          </el-checkbox>
        </el-form-item>
        <el-form-item
          label="退役理由（≥20 字，写入审计日志 GateLog）"
          :error="retireDialog.reason.trim().length > 0 && retireDialog.reason.trim().length < 20 ? `至少 20 字，当前 ${retireDialog.reason.trim().length} 字` : ''"
        >
          <el-input
            v-model="retireDialog.reason"
            type="textarea"
            :rows="3"
            maxlength="200"
            show-word-limit
            placeholder="例如：该蜜饵文案已被广泛识破，继续埋设价值有限且可能污染检测统计"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="retireDialog.show = false">取消</el-button>
        <el-button
          type="danger"
          :loading="retireDialog.submitting"
          :disabled="!retireDialog.confirm || retireDialog.reason.trim().length < 20"
          @click="submitRetire"
        >
          确认退役
        </el-button>
      </template>
    </el-dialog>

    <!-- 蜜饵命中详情面板 -->
    <el-drawer v-model="detailVisible" title="蜜饵命中详情" size="46%" :loading="detailLoading">
      <template v-if="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="ID">#{{ detail.id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="stMeta(detail.status).type" size="small">{{ stMeta(detail.status).label }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="指纹" :span="2"><span class="af-mono">{{ detail.fingerprint }}</span></el-descriptions-item>
          <el-descriptions-item label="伪装度">{{ detail.disguise_score }}</el-descriptions-item>
          <el-descriptions-item label="命中次数">{{ detail.hit_count }}</el-descriptions-item>
          <el-descriptions-item label="目标 URL" :span="2">{{ detail.target_url || '—' }}</el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ detail.created_at }}</el-descriptions-item>
          <el-descriptions-item label="退役原因">{{ detail.retired_reason || '—' }}</el-descriptions-item>
          <el-descriptions-item label="文案" :span="2">{{ detail.bait_text }}</el-descriptions-item>
        </el-descriptions>

        <el-divider content-position="left">踩饵检测（候选文本）</el-divider>
        <el-input v-model="checkText" type="textarea" :rows="3" placeholder="粘贴疑似踩饵的评论/私信文本，检测是否命中本蜜饵（命中即退役）" />
        <el-button type="primary" size="small" style="margin-top: 8px" :loading="checkLoading" @click="runCheckHit">检测踩饵</el-button>

        <template v-if="checkResult">
          <el-alert :type="checkResult.hit ? 'danger' : 'success'" :closable="false" style="margin-top: 12px" show-icon>
            <template #title>{{ checkResult.hit ? '命中！' : '未命中' }}</template>
            {{ checkResult.hit ? `匹配方式: ${(checkResult.matched_by || []).join(' / ')}` : '未检测到踩饵特征' }}
          </el-alert>
          <el-descriptions v-if="checkResult" :column="2" border size="small" style="margin-top: 8px">
            <el-descriptions-item label="SimHash 汉明距离">{{ checkResult.hamming }}</el-descriptions-item>
            <el-descriptions-item label="重叠度">{{ checkResult.overlap?.toFixed?.(2) ?? checkResult.overlap }}</el-descriptions-item>
            <el-descriptions-item label="精确匹配">{{ checkResult.exact }}</el-descriptions-item>
            <el-descriptions-item label="子串匹配">{{ checkResult.substring }}</el-descriptions-item>
          </el-descriptions>
        </template>

        <el-divider content-position="left">实时推送说明</el-divider>
        <div class="af-muted">
          后端当前未提供 /ws/events WebSocket（ws_hub 未落地），本页以 5 秒轮询刷新列表实现准实时：
          新命中（hit_count 增加）或状态迁移会在 ≤5s 内反映到列表与详情。
        </div>
      </template>
    </el-drawer>
  </div>
</template>