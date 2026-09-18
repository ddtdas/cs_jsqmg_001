<script setup>
// 事件流：主数据源 = 真实 REST 端点 GET /events（事件落库，P6.5 已实现；分页/kind 过滤），
// 5s 轮询 refresh 作为实时补充；/events 不可用时降级为 traps/hits/alerts 聚合时间线（D5 降级链）。
import { computed, reactive, ref, watch } from 'vue'
import { api } from '../api/client'
import { usePolling } from '../composables/usePolling'
import DetailDrawer from '../components/DetailDrawer.vue'

const KIND_OPTIONS = [
  { value: '', label: '全部事件' },
  { value: 'trap_hit', label: '踩饵命中' },
  { value: 'trap_draft_created', label: '蜜饵草稿' },
  { value: 'trap_deployed', label: '蜜饵部署' },
  { value: 'trap_monitored', label: '进入监控' },
  { value: 'trap_disabled', label: '蜜饵停用' },
  { value: 'trap_retired', label: '蜜饵退役' },
  { value: 'alert', label: '告警' },
  { value: 'case_published', label: '案例发布' },
  { value: 'case_ready', label: '案例就绪' },
  { value: 'evidence_built', label: '证据构建' },
  { value: 'evidence_frozen', label: '证据冻结' },
  { value: 'config_updated', label: '配置更新' },
  { value: 'account_updated', label: '账号更新' },
]

const kindFilter = ref('')
const sourceLabel = ref('事件库')
const total = ref(0)

// 详情抽屉（四要素）：事件 payload 中带 detection_id 时可打开检测详情
const detailDrawer = reactive({ show: false, detectionId: null })

function openEventDetail(e) {
  const detId = e?.detId
  if (!detId) return
  detailDrawer.detectionId = detId
  detailDrawer.show = true
}

const KIND_CN = {
  trap_hit: '踩饵命中', trap_draft_created: '蜜饵草稿', trap_deployed: '蜜饵部署',
  trap_monitored: '进入监控', trap_disabled: '蜜饵停用', trap_retired: '蜜饵退役',
  alert: '告警', case_published: '案例发布', case_ready: '案例就绪',
  evidence_built: '证据构建', evidence_frozen: '证据冻结',
  config_updated: '配置更新', account_updated: '账号更新',
}

function evtToView(e) {
  const p = e.payload || {}
  let meta = JSON.stringify(p)
  if (meta.length > 140) meta = `${meta.slice(0, 140)}…`
  return {
    id: e.id,
    time: e.ts || e.created_at || '',
    kind: KIND_CN[e.kind] || e.kind,
    level: e.kind === 'trap_hit' || e.kind === 'alert' ? 'L3+' : e.kind,
    text: `#${e.id} ${KIND_CN[e.kind] || e.kind}`,
    meta,
    // DetailDrawer 接入点：事件 payload 中携带的检测记录 id（无则不可点）
    detId: p.detection_id ?? null,
  }
}

// 降级聚合：/events 不可用时用 traps/hits/alerts 真实端点合成时间线（原实现逻辑）
async function buildAgg() {
  const [traps, hits, alerts] = await Promise.allSettled([
    api.listTraps(), api.scanHits(50), api.listAlerts({ limit: 100 }),
  ])
  const evts = []
  if (traps.status === 'fulfilled') {
    for (const t of traps.value || []) {
      evts.push({ time: t.created_at, kind: '蜜饵', level: t.status, text: `#${t.id} ${t.bait_text}`, meta: `状态=${t.status} 伪装度=${t.disguise_score} 命中=${t.hit_count}`, detId: null })
    }
  }
  if (hits.status === 'fulfilled') {
    for (const h of hits.value || []) {
      evts.push({ time: h.created_at, kind: '检测', level: h.hit_count > 0 ? 'L3+' : 'info', text: `#${h.id} 话术检测（${h.source}）`, meta: `规则分=${h.rule_score} 命中=${h.hit_count} 置信=${h.judge_confidence}`, detId: h.id })
    }
  }
  if (alerts.status === 'fulfilled') {
    for (const a of alerts.value || []) {
      evts.push({ time: a.created_at, kind: '告警', level: a.level, text: `#${a.id} ${a.level} 告警`, meta: `未读=${a.unread} 渠道=${a.channel} grade=${a.payload?.grade || ''}`, detId: a.payload?.detection_id || null })
    }
  }
  return evts.sort((a, b) => (b.time || '').localeCompare(a.time || '')).slice(0, 100)
}

const { data, error, loading, refresh } = usePolling(async () => {
  try {
    // 主路径：真实 /events 端点（P6.5 已实现，MCP af_list_events 同源）
    const r = await api.listEvents({ kind: kindFilter.value || undefined, pageSize: 100 })
    sourceLabel.value = '事件库'
    total.value = r?.total ?? 0
    return (r?.items || []).map(evtToView)
  } catch (e) {
    // 降级链：/events 不可用 → 真实端点聚合时间线（不抛错，避免整页错误）
    sourceLabel.value = '聚合降级'
    total.value = 0
    return await buildAgg()
  }
}, 5000)

watch(kindFilter, () => refresh())

const events = computed(() => data.value || [])

const levelType = (l) => (['L4', 'L5', 'L3+', 'red', 'hit', 'trap_hit', 'alert'].includes(l) ? 'danger' : ['L3', 'yellow', 'active', 'monitored'].includes(l) ? 'warning' : 'info')
</script>

<template>
  <div class="af-page">
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
          <div style="display: flex; align-items: center; gap: 10px">
            <b>事件流</b>
            <el-select v-model="kindFilter" placeholder="全部事件" clearable style="width: 140px" @change="refresh">
              <el-option v-for="o in KIND_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-tag type="warning" size="small">5s 轮询实时（/events 真实端点）</el-tag>
          </div>
          <span class="af-muted">{{ loading ? '刷新中…' : `共 ${events.length} 条（${sourceLabel}${total ? '，总 ' + total + ' 条' : ''}）` }}</span>
        </div>
      </template>
      <el-alert v-if="error" type="error" :title="error.message" :closable="false" style="margin-bottom: 12px" />
      <el-timeline v-if="events.length">
        <el-timeline-item
          v-for="(e, i) in events"
          :key="`${e.id || e.kind}-${i}`"
          :timestamp="e.time"
          :color="e.level === 'L5' || e.kind === '告警' || e.kind === '踩饵命中' ? '#f56c6c' : e.level === 'L3' ? '#e6a23c' : '#409eff'"
          placement="top"
        >
          <el-card
            shadow="never"
            style="margin-bottom: 4px"
            :style="{ cursor: e.detId ? 'pointer' : 'default' }"
            @click="openEventDetail(e)"
          >
            <div style="display: flex; gap: 10px; align-items: center">
              <el-tag :type="levelType(e.level)" size="small">{{ e.kind }}</el-tag>
              <el-tag size="small" effect="plain">{{ e.level }}</el-tag>
              <span style="font-weight: 600">{{ e.text }}</span>
            </div>
            <div class="af-muted mt8">{{ e.meta }}</div>
          </el-card>
        </el-timeline-item>
      </el-timeline>
      <el-empty v-else description="暂无事件（事件库为空或后端未返回）" />
    </el-card>

    <!-- 详情抽屉（四要素） -->
    <DetailDrawer v-model="detailDrawer.show" :detection-id="detailDrawer.detectionId" />
  </div>
</template>