<script setup>
// 资产管理（T2 侧边栏①）：蜜饵资产（/traps）+ 证据包资产（/events evidence_built 聚合 → /evidence/{id}）
// 全部真实 API 数据；证据包列表来自事件库（后端无独立 list 端点，用 evidence_built 事件聚合，D5 降级链同源）。
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'

const router = useRouter()
const state = reactive({ traps: [], evPkgs: [], loading: true, error: '' })
const pkgDetail = ref(null)
const pkgVisible = ref(false)
const pkgLoading = ref(false)

const statusMeta = {
  draft: { type: 'info', label: '草稿' },
  active: { type: 'success', label: '已部署' },
  monitored: { type: 'warning', label: '监控中' },
  hit: { type: 'danger', label: '已命中' },
  retired: { type: 'info', label: '已退役' },
}
const stMeta = (s) => statusMeta[s] || { type: 'info', label: s }

const trapStat = computed(() => {
  const c = { total: state.traps.length }
  for (const t of state.traps) c[t.status] = (c[t.status] || 0) + 1
  return c
})

async function load() {
  state.loading = true
  state.error = ''
  try {
    const [traps, evts] = await Promise.allSettled([
      api.listTraps(),
      api.listEvents({ kind: 'evidence_built', pageSize: 50 }),
    ])
    const failed = []
    if (traps.status === 'fulfilled') state.traps = traps.value || []
    else failed.push(`蜜饵(${traps.reason?.message || '请求失败'})`)
    // 证据包：从 evidence_built 事件提取 pkg_id，逐个拉取详情（限量 10，避免 N+1 过重）
    const pkgIds = []
    if (evts.status === 'fulfilled') {
      for (const e of evts.value?.items || []) {
        const pid = e.payload?.pkg_id ?? e.payload?.pkgId
        if (pid && !pkgIds.includes(pid)) pkgIds.push(pid)
      }
    } else {
      failed.push(`证据事件(${evts.reason?.message || '请求失败'})`)
    }
    const pkgs = []
    for (const pid of pkgIds.slice(0, 10)) {
      try {
        // P1-4：GET /evidence/{id} 返回 { package, verified }，行数据在 package 下，加载时解包
        const res = await api.getEvidence(pid)
        pkgs.push(res?.package ?? res)
      } catch { /* 单个证据包拉取失败不阻塞整体 */ }
    }
    state.evPkgs = pkgs
    if (failed.length) state.error = `部分数据源加载失败（已展示其余可用数据）：${failed.join('；')}`
  } catch (e) {
    state.error = e.message
  } finally {
    state.loading = false
  }
}

async function openPkg(pid) {
  pkgLoading.value = true
  pkgDetail.value = null
  try {
    pkgDetail.value = await api.getEvidence(pid)
    pkgVisible.value = true
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    pkgLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="af-page" v-loading="state.loading">
    <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 16px" />

    <!-- 资产统计（真实 /traps 聚合） -->
    <el-row :gutter="16" style="margin-bottom: 16px">
      <el-col :span="4"><el-card shadow="hover"><div class="af-muted">蜜饵总数</div><div style="font-size: 24px; font-weight: 700">{{ trapStat.total || 0 }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="hover"><div class="af-muted">已部署</div><div style="font-size: 24px; font-weight: 700; color: var(--cg-success)">{{ trapStat.active || 0 }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="hover"><div class="af-muted">监控中</div><div style="font-size: 24px; font-weight: 700; color: var(--cg-warning)">{{ trapStat.monitored || 0 }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="hover"><div class="af-muted">已命中</div><div style="font-size: 24px; font-weight: 700; color: var(--cg-danger)">{{ trapStat.hit || 0 }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="hover"><div class="af-muted">草稿/退役</div><div style="font-size: 24px; font-weight: 700">{{ (trapStat.draft || 0) + (trapStat.retired || 0) }}</div></el-card></el-col>
      <el-col :span="4"><el-card shadow="hover"><div class="af-muted">证据包</div><div style="font-size: 24px; font-weight: 700; color: var(--cg-primary)">{{ state.evPkgs.length }}</div></el-card></el-col>
    </el-row>

    <el-row :gutter="16">
      <!-- 蜜饵资产 -->
      <el-col :span="14">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>蜜饵资产</b><span class="af-muted">（/traps 真实数据）</span>
            </div>
          </template>
          <el-table :data="state.traps" size="small" max-height="520">
            <el-table-column prop="id" label="ID" width="60" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }"><el-tag :type="stMeta(row.status).type" size="small">{{ stMeta(row.status).label }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="bait_text" label="蜜饵文案" show-overflow-tooltip />
            <el-table-column prop="disguise_score" label="伪装度" width="80" />
            <el-table-column prop="hit_count" label="命中" width="70" />
            <el-table-column prop="created_at" label="创建时间" width="160" />
          </el-table>
        </el-card>
      </el-col>

      <!-- 证据包资产 -->
      <el-col :span="10">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>证据包资产</b><span class="af-muted">（evidence_built 事件聚合）</span>
            </div>
          </template>
          <el-table :data="state.evPkgs" size="small" max-height="520">
            <el-table-column prop="pkg_id" label="包ID" width="70" />
            <el-table-column label="检测数" width="70">
              <template #default="{ row }">{{ (row.det_ids || []).length }}</template>
            </el-table-column>
            <el-table-column label="冻结" width="70">
              <template #default="{ row }"><el-tag :type="row.frozen ? 'warning' : 'info'" size="small" effect="plain">{{ row.frozen ? '是' : '否' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="操作" width="80" fixed="right">
              <template #default="{ row }">
                <el-button size="small" type="primary" plain :loading="pkgLoading" @click="openPkg(row.pkg_id)">详情</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!state.evPkgs.length && !state.loading" class="af-muted" style="padding: 12px 0">暂无证据包（可在旧版「证据包」页面构建）</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 证据包详情抽屉 -->
    <el-drawer v-model="pkgVisible" title="证据包详情" size="46%">
      <pre class="af-mono" style="margin: 0; font-size: 12px; white-space: pre-wrap">{{ JSON.stringify(pkgDetail || {}, null, 2) }}</pre>
    </el-drawer>
  </div>
</template>
