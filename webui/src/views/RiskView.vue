<script setup>
// 风险信息（T2 侧边栏⑤）：告警列表（/alerts）+ 分级档位说明（/grading/levels）
// + 风险资产视图（高危蜜饵 + 高分检测记录聚合）。
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

// 详情抽屉（四要素）：风险资产（检测记录）与告警行均可打开检测详情
const detailDrawer = reactive({ show: false, detectionId: null })

function openAssetDetail(row) {
  // 风险资产仅「检测」行携带 detection id（蜜饵行为 trap id，不打开）
  const detId = row?.detId
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}

function openAlertDetail(row) {
  const detId = row?.payload?.detection_id
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}

const state = reactive({
  alerts: [],
  levels: [],
  traps: [],
  hits: [],
  levelFilter: '',
  unreadOnly: false,
  loading: false,
  error: '',
})

async function load() {
  state.loading = true
  state.error = ''
  try {
    const [alerts, levels, traps, hits] = await Promise.allSettled([
      api.listAlerts({ level: state.levelFilter || undefined, unreadOnly: state.unreadOnly || undefined, limit: 200 }),
      api.gradingLevels(),
      api.listTraps(),
      api.scanHits(50),
    ])
    const failed = []
    if (alerts.status === 'fulfilled') state.alerts = alerts.value || []
    else failed.push(`告警(${alerts.reason?.message || '请求失败'})`)
    if (levels.status === 'fulfilled') state.levels = levels.value || []
    else failed.push(`分级(${levels.reason?.message || '请求失败'})`)
    if (traps.status === 'fulfilled') state.traps = traps.value || []
    else failed.push(`蜜饵(${traps.reason?.message || '请求失败'})`)
    if (hits.status === 'fulfilled') state.hits = hits.value || []
    else failed.push(`检测(${hits.reason?.message || '请求失败'})`)
    if (failed.length) state.error = `部分数据源加载失败（已展示其余可用数据）：${failed.join('；')}`
  } catch (e) {
    state.error = e.message
  } finally {
    state.loading = false
  }
}
load()

async function markRead(alert) {
  try {
    await api.markAlertRead(alert.id)
    ElMessage.success(`告警 #${alert.id} 已读`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// 风险资产：已命中蜜饵 + 规则分≥10 的检测记录（红/橙分级语义）
const riskAssets = computed(() => {
  const traps = state.traps.filter((t) => t.status === 'hit').map((t) => ({
    kind: '蜜饵', id: t.id, detId: null, level: t.hit_count >= 3 ? 'L4' : 'L3', note: `蜜饵#${t.id} 命中${t.hit_count} 次（${t.bait_text}）`,
  }))
  const hits = state.hits.filter((h) => (h.rule_score || 0) >= 6).map((h) => ({
    kind: '检测', id: h.id, detId: h.id, level: (h.rule_score || 0) >= 10 ? 'L4' : 'L3', note: `检测#${h.id} 规则分${h.rule_score} 判定${h.llm_verdict || h.verdict || '—'}（来源 ${h.source}）`,
  }))
  return [...traps, ...hits].sort((a, b) => (b.level || '').localeCompare(a.level || '')).slice(0, 30)
})

const levelType = (l) => ({ L3: 'warning', L4: 'danger', L5: 'danger' }[l] || 'info')
const LEVEL_MAP = { L1: 'success', L2: 'success', L3: 'warning', L4: 'danger', L5: 'danger' }
</script>

<template>
  <div class="af-page" v-loading="state.loading">
    <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 16px" />

    <!-- 风险资产视图 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px">
          <b>风险资产</b>
          <span class="af-muted">（已命中蜜饵 + 规则分≥6 检测记录，真实 /traps + /scan/hits 聚合）</span>
          <el-tag type="danger" size="small" effect="plain">{{ riskAssets.filter((r) => r.level === 'L4').length }} 高危</el-tag>
          <el-tag type="warning" size="small" effect="plain">{{ riskAssets.filter((r) => r.level === 'L3').length }} 中危</el-tag>
        </div>
      </template>
      <el-table :data="riskAssets" size="small" max-height="300" highlight-current-row @row-click="openAssetDetail" style="cursor: pointer">
        <el-table-column label="级别" width="80">
          <template #default="{ row }"><el-tag :type="levelType(row.level)" size="small">{{ row.level }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="kind" label="类型" width="80" />
        <el-table-column prop="note" label="说明" show-overflow-tooltip />
      </el-table>
      <div v-if="!riskAssets.length" class="af-muted" style="padding: 16px 0">暂无风险资产（无命中蜜饵与高分检测）</div>
    </el-card>

    <el-row :gutter="16">
      <!-- 告警列表 -->
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="af-table-toolbar">
              <b>告警（L3+ 推送记录）</b>
              <el-select v-model="state.levelFilter" placeholder="全部级别" clearable style="width: 110px" @change="load">
                <el-option label="L3" value="L3" />
                <el-option label="L4" value="L4" />
                <el-option label="L5" value="L5" />
              </el-select>
              <el-checkbox v-model="state.unreadOnly" @change="load">仅未读</el-checkbox>
              <el-button size="small" @click="load">刷新</el-button>
            </div>
          </template>
          <el-table :data="state.alerts" size="small" max-height="430" highlight-current-row @row-click="openAlertDetail" style="cursor: pointer">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column label="级别" width="80">
              <template #default="{ row }"><el-tag :type="levelType(row.level)" size="small">{{ row.level }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="channel" label="渠道" width="90" />
            <el-table-column label="未读" width="70">
              <template #default="{ row }">
                <el-tag :type="row.unread ? 'danger' : 'info'" size="small" effect="plain">{{ row.unread ? '未读' : '已读' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="内容" show-overflow-tooltip>
              <template #default="{ row }">
                <template v-for="(r, i) in row.payload?.reason || []" :key="i">
                  {{ r.note }}<span v-if="i < (row.payload?.reason || []).length - 1">；</span>
                </template>
                <span v-if="!(row.payload?.reason || []).length">{{ row.payload?.grade || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="时间" width="160" />
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button v-if="row.unread" size="small" type="primary" plain @click.stop="markRead(row)">标记已读</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <!-- 分级档位说明 -->
      <el-col :span="8">
        <el-card shadow="never">
          <template #header><b>分级档位说明</b><span class="af-muted" style="margin-left: 8px">（/grading/levels）</span></template>
          <el-table :data="state.levels" size="small" max-height="430">
            <el-table-column label="级别" width="70">
              <template #default="{ row }"><el-tag :type="LEVEL_MAP[row.level] || 'info'" size="small">{{ row.level }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="desc" label="定义" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>
