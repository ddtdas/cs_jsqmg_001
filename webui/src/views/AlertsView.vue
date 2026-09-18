<script setup>
// 告警：级别过滤（L3/L4/L5）+ 未读过滤 + 标记已读（/alerts）
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import DetailDrawer from '../components/DetailDrawer.vue'

const state = reactive({ level: '', unreadOnly: false, items: [], loading: false, error: '' })
const detailDrawer = reactive({ show: false, detectionId: null })

async function load() {
  state.loading = true
  state.error = ''
  try {
    state.items = (await api.listAlerts({
      level: state.level || undefined,
      unreadOnly: state.unreadOnly || undefined,
      limit: 200,
    })) || []
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

const levelType = (l) => ({ L3: 'warning', L4: 'danger', L5: 'danger' }[l] || 'info')

// 告警行点击 → 打开检测详情抽屉（payload.detection_id）
function openAlertDetail(row) {
  const detId = row?.payload?.detection_id
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}
</script>

<template>
  <div class="af-page">
    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <b>告警（L3+ 推送记录）</b>
          <el-select v-model="state.level" placeholder="全部级别" clearable style="width: 120px" @change="load">
            <el-option label="L3" value="L3" />
            <el-option label="L4" value="L4" />
            <el-option label="L5" value="L5" />
          </el-select>
          <el-checkbox v-model="state.unreadOnly" @change="load">仅未读</el-checkbox>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>

      <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 12px" />
      <el-table :data="state.items" v-loading="state.loading" size="small" highlight-current-row @row-click="openAlertDetail">
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
            <el-button v-if="row.unread" size="small" type="primary" plain @click="markRead(row)">标记已读</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>