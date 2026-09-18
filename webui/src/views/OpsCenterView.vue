<script setup>
// 运维中心：健康四灯（/system/health）+ 降级链状态（/zhihu/status + /system/health llm）+
// 分级档位（/grading/levels）+ 主控版本/端口（bootstrap-info）
import { reactive, onMounted } from 'vue'
import { api } from '../api/client'

const s = reactive({ health: null, zhihu: null, levels: [], boot: null, error: '', loading: false })

async function load() {
  s.error = ''
  s.loading = true
  try {
    const [health, zhihu, levels, boot] = await Promise.allSettled([
      api.health(), api.zhihuStatus(), api.gradingLevels(), api.bootstrapInfo(),
    ])
    // 审查预检：allSettled 的 rejected 不再静默吞掉——逐项收集并展示
    const failed = []
    if (health.status === 'fulfilled') s.health = health.value
    else failed.push(`健康(${health.reason?.message || '请求失败'})`)
    if (zhihu.status === 'fulfilled') s.zhihu = zhihu.value
    else failed.push(`知乎状态(${zhihu.reason?.message || '请求失败'})`)
    if (levels.status === 'fulfilled') s.levels = levels.value
    else failed.push(`分级(${levels.reason?.message || '请求失败'})`)
    if (boot.status === 'fulfilled') s.boot = boot.value
    else failed.push(`bootstrap(${boot.reason?.message || '请求失败'})`)
    if (failed.length) {
      s.error = `部分数据源加载失败（已展示其余可用数据）：${failed.join('；')}`
    }
  } catch (e) {
    s.error = e.message
  } finally {
    s.loading = false
  }
}
onMounted(load)

const lights = [
  { key: '主控', ok: () => s.health?.status === 'ok', desc: () => `版本 v${s.health?.version}` },
  { key: 'DB', ok: () => !!s.health?.db, desc: () => (s.health?.db ? 'SQLite 连接正常' : '连接失败') },
  { key: 'LLM', ok: () => s.health?.llm === 'configured', desc: () => (s.health?.llm === 'configured' ? '已配置（DeepSeek/OpenAI 兼容）' : '未配置 → D5 降级（纯规则）') },
  // 知乎通道三态灯：红=error、绿=正常、黄=空闲/降级（idle 不再误标红）
  { key: '知乎通道', tone: () => {
      const z = s.health?.zhihu
      if (z === 'idle') return 'warn' // 空闲 → 黄
      if (z === 'error') return 'err' // 异常 → 红
      return 'ok' // 正常/其他状态 → 绿
    }, desc: () => (s.health?.zhihu === 'idle' ? '空闲（通道未启用）' : String(s.health?.zhihu ?? '—')) },
]

// 三态灯配色（红=err 绿=ok 黄=warn）
const LIGHT_TONE = {
  ok: { border: '#67c23a', bg: '#f0f9eb', color: '#67c23a' },
  warn: { border: '#e6a23c', bg: '#fdf6ec', color: '#e6a23c' },
  err: { border: '#f56c6c', bg: '#fef0f0', color: '#f56c6c' },
}
const lightStyle = (l) => {
  const t = l.tone ? LIGHT_TONE[l.tone()] : (l.ok() ? LIGHT_TONE.ok : LIGHT_TONE.err)
  return { border: `1px solid ${t.border}`, borderRadius: 8, padding: 16, textAlign: 'center', background: t.bg }
}
const lightDot = (l) => {
  const t = l.tone ? LIGHT_TONE[l.tone()] : (l.ok() ? LIGHT_TONE.ok : LIGHT_TONE.err)
  return { width: 14, height: 14, borderRadius: '50%', background: t.color, margin: '0 auto 8px' }
}

const LEVEL_MAP = { L1: 'success', L2: 'success', L3: 'warning', L4: 'danger', L5: 'danger' }
</script>

<template>
  <div class="af-page" v-loading="s.loading">
    <el-alert v-if="s.error" type="error" :title="s.error" :closable="false" style="margin-bottom: 16px" />

    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <b>健康四灯</b>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-row :gutter="16">
        <el-col v-for="l in lights" :key="l.key" :span="6">
          <div :style="lightStyle(l)">
            <div :style="lightDot(l)" />
            <div style="font-weight: 700">{{ l.key }}</div>
            <div class="af-muted">{{ l.desc() }}</div>
          </div>
        </el-col>
      </el-row>
      <div class="af-muted mt16">主控地址：http://{{ s.boot?.port ? '127.0.0.1:' + s.boot.port : '—' }}　key 文件：{{ s.boot?.key_file || '—' }}</div>
    </el-card>

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header><b>降级链状态（D5）</b></template>
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="LLM 降级">{{ s.health?.llm === 'configured' ? 'LLM 可用；不可用时自动退纯规则' : 'LLM 未配置 → 当前为纯规则模式' }}</el-descriptions-item>
            <el-descriptions-item label="知乎退避冷却">{{ Object.keys(s.zhihu?.backoff?.channels || {}).length ? JSON.stringify(s.zhihu.backoff.channels) : '无（全部通道可用）' }}</el-descriptions-item>
            <el-descriptions-item label="限速器">容量 {{ s.zhihu?.rate_limit?.capacity }} / 可用 {{ s.zhihu?.rate_limit?.available }}（{{ s.zhihu?.rate_limit?.config }}）</el-descriptions-item>
            <el-descriptions-item label="蜜饵生成">LLM 不可用时自动降级离线模板池（template:*）</el-descriptions-item>
            <el-descriptions-item label="蜜饵伪装度">LLM 校验不可用时降级启发式评分（heuristic）</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header><b>分级档位</b><span class="af-muted" style="margin-left: 8px">（/grading/levels）</span></template>
          <el-table :data="s.levels" size="small">
            <el-table-column prop="level" label="级别" width="80">
              <template #default="{ row }"><el-tag :type="LEVEL_MAP[row.level] || 'info'" size="small">{{ row.level }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="min_score" label="最低分" width="80" />
            <el-table-column prop="desc" label="定义" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>
        <b>WebUI 降级说明</b>
      </template>
      <ul class="af-muted" style="line-height: 2; padding-left: 20px; margin: 0">
        <li>后端未提供 /ws/events 与 /events 端点（ws_hub 未落地）→ 事件流与蜜饵命中推送采用 5s 轮询降级。</li>
        <li>/system/stats 当前为后端占位返回（traps=0/detections=0）→ 仪表盘统计改为由 /traps、/scan/hits、/alerts、/cases 真实聚合。</li>
        <li>LLM 未配置时 /scan/text 与蜜饵生成自动降级纯规则 / 离线模板（D5，后端实现）。</li>
      </ul>
    </el-card>
  </div>
</template>