<script setup>
// 案例库：公开查询（分页/tag 过滤）+ 骗术图谱（ECharts，/cases/graph）+ 发布案例（脱敏前置）
import { reactive, ref, computed, onMounted, onBeforeUnmount } from 'vue'
import * as echarts from 'echarts'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

const state = reactive({ page: 1, pageSize: 20, tag: '', total: 0, items: [], graph: { by_tag: [], by_grade: [], total: 0 }, loading: false, error: '' })
const hits = ref([])
// 详情抽屉（四要素）：案例行关联的检测记录（case.det_id）
const detailDrawer = reactive({ show: false, detectionId: null })

function openCaseDetail(row) {
  const detId = row?.det_id
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}
// U1：发布为敏感操作 —— confirm+reason 双确认（后端恒校验：confirm=true 且 reason≥20 字）
const publish = reactive({ show: false, detId: null, redacted: '', tags: '', confirm: false, reason: '', submitting: false })

const REASON_MIN_LEN = 20

// 不满足双确认条件时禁用提交按钮（与 SettingsView 重置 key 交互模式一致）
const canPublish = computed(() => {
  return !!publish.detId
    && publish.redacted.trim().length > 0
    && publish.confirm
    && publish.reason.trim().length >= REASON_MIN_LEN
})

let pieChart = null

async function load() {
  state.loading = true
  state.error = ''
  try {
    const r = await api.listCases(state.page, state.pageSize, state.tag || undefined)
    state.total = r.total
    state.items = r.items
  } catch (e) {
    state.error = e.message
  } finally {
    state.loading = false
  }
}

async function loadGraph() {
  try {
    state.graph = (await api.caseGraph()) || { by_tag: [], by_grade: [], total: 0 }
    renderPie()
  } catch (e) {
    // 审查预检：图谱失败不再静默——在错误提示中标注，避免用户误以为无数据
    state.graph = { by_tag: [], by_grade: [], total: 0 }
    state.error = `骗术图谱加载失败：${e.message}（列表数据不受影响）`
  }
}

function renderPie() {
  const data = (state.graph.by_tag || []).map((t) => ({ name: t.name, value: t.value }))
  if (pieChart) pieChart.dispose()
  pieChart = echarts.init(document.getElementById('case-pie'))
  pieChart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, type: 'scroll' },
    series: [{ name: '骗术标签', type: 'pie', radius: ['35%', '60%'], data, label: { formatter: '{b}: {c}' } }],
  })
}

function onResize() {
  pieChart?.resize()
}

onMounted(async () => {
  load()
  await loadGraph()
  try {
    hits.value = (await api.scanHits(30)) || []
  } catch { /* ignore */ }
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  pieChart?.dispose()
})

function openPublish() {
  publish.show = true
  publish.detId = null
  publish.redacted = ''
  publish.tags = ''
  publish.confirm = false
  publish.reason = ''
}

// U1：后端错误码 → 友好提示（confirm_required / reason_too_short 因 UI 已前置校验不应出现，
// 仍兜底映射；sensitive_data 提示用户先去脱敏）
function publishErrorText(e) {
  const code = e?.code
  if (code === 'confirm_required') return '发布需勾选「确认发布」（敏感操作双确认）'
  if (code === 'reason_too_short') return `发布理由至少 ${REASON_MIN_LEN} 字，请补充后再试`
  if (code === 'sensitive_data') return '脱敏未通过：载荷仍含敏感信息（手机号/身份证/微信等），请先脱敏再发布'
  if (code === 'case_exists') return '该检测记录已发布过案例（同一检测仅一个案例），请选择其他检测记录'
  if (e?.status === 422) return '请求参数不符合接口约束，请检查后重试'
  return e?.message || '发布失败，请稍后重试'
}

async function submitPublish() {
  if (!canPublish.value) {
    ElMessage.warning('请完成：选择检测记录、填写脱敏载荷、勾选确认发布并填写理由（≥20 字）')
    return
  }
  publish.submitting = true
  try {
    const tags = publish.tags.trim() ? publish.tags.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : undefined
    const r = await api.publishCase(
      Number(publish.detId),
      publish.redacted.trim(),
      tags,
      publish.confirm,
      publish.reason.trim()
    )
    ElMessage.success(`案例 #${r.id ?? '?'} 已发布`)
    publish.show = false
    load()
    loadGraph()
  } catch (e) {
    ElMessage.error(publishErrorText(e))
  } finally {
    publish.submitting = false
  }
}
</script>

<template>
  <div class="af-page">
    <el-row :gutter="16">
      <el-col :span="15">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
              <div style="display: flex; align-items: center; gap: 10px">
                <b>脱敏案例（公开查询）</b>
                <el-input v-model="state.tag" placeholder="骗术标签过滤" clearable style="width: 180px" @change="() => { state.page = 1; load() }" />
              </div>
              <el-button type="primary" size="small" @click="openPublish">发布案例</el-button>
            </div>
          </template>
          <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 12px" />
          <el-table :data="state.items" v-loading="state.loading" size="small" highlight-current-row @row-click="openCaseDetail" style="cursor: pointer">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column prop="det_id" label="检测ID" width="70" />
            <el-table-column prop="redacted_payload" label="脱敏载荷" show-overflow-tooltip />
            <el-table-column label="标签" width="160">
              <template #default="{ row }">
                <el-tag v-for="t in row.graph_tags || []" :key="t" size="small" style="margin-right: 4px">{{ t }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="published_at" label="发布时间" width="160" />
          </el-table>
          <el-pagination
            style="margin-top: 12px; justify-content: flex-end"
            background
            layout="total, prev, pager, next"
            :total="state.total"
            :page-size="state.pageSize"
            v-model:current-page="state.page"
            @current-change="load"
          />
        </el-card>
      </el-col>

      <el-col :span="9">
        <el-card shadow="never" class="af-card">
          <template #header><b>骗术图谱</b><span class="af-muted" style="margin-left: 8px">（/cases/graph 真实聚合，共 {{ state.graph.total }} 条）</span></template>
          <div id="case-pie" style="height: 300px" />
        </el-card>
        <el-card shadow="never">
          <template #header><b>按分级分布</b></template>
          <el-table :data="state.graph.by_grade || []" size="small">
            <el-table-column prop="name" label="级别" width="90" />
            <el-table-column prop="value" label="案例数" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 发布案例 -->
    <el-dialog v-model="publish.show" title="发布脱敏案例（脱敏强制校验前置）" width="560px">
      <el-form label-position="top">
        <el-form-item label="关联检测记录">
          <el-select v-model="publish.detId" placeholder="选择 detection_id" filterable style="width: 100%">
            <el-option v-for="h in hits" :key="h.id" :label="`#${h.id} · 规则分 ${h.rule_score} · 命中 ${h.hit_count}`" :value="h.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="脱敏后载荷（姓名/手机/ID 须已抹除）">
          <el-input v-model="publish.redacted" type="textarea" :rows="3" placeholder="如：加我微信 138****5678" />
        </el-form-item>
        <el-form-item label="骗术标签（逗号分隔，可空自动推导）">
          <el-input v-model="publish.tags" placeholder="fake_investment, 刷单返利" />
        </el-form-item>
        <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 16px">
          <div>发布为敏感操作：需确认并填写理由（≥20 字），将写入审计日志 GateLog（action=case.publish）。</div>
        </el-alert>
        <el-form-item>
          <el-checkbox v-model="publish.confirm">
            我确认要发布该脱敏案例
          </el-checkbox>
        </el-form-item>
        <el-form-item
          label="发布理由（≥20 字，写入审计日志 GateLog）"
          :error="publish.reason.trim().length > 0 && publish.reason.trim().length < REASON_MIN_LEN ? `至少 ${REASON_MIN_LEN} 字，当前 ${publish.reason.trim().length} 字` : ''"
        >
          <el-input
            v-model="publish.reason"
            type="textarea"
            :rows="3"
            maxlength="200"
            show-word-limit
            placeholder="例如：该话术样本已确认为刷单返利骗局，脱敏后发布供公开查询与图谱聚合"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="publish.show = false">取消</el-button>
        <el-button type="primary" :loading="publish.submitting" :disabled="!canPublish" @click="submitPublish">发布</el-button>
      </template>
    </el-dialog>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>