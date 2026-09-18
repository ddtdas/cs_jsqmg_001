<script setup>
// 通用详情抽屉：任何事件/检测条目点击后打开，统一四块结构
// ① 告警原因（分级证据链 + 告警 payload）
// ② 上下文（原文 + 命中话术 + 关联账号/蜜饵）
// ③ 平台跳转（iframe 内嵌网页版平台跳转问题点 + 新窗口兜底）
// ④ 详细分析（话术明细表 + 关联案例/事件时间线）
import { computed, ref, watch } from 'vue'
import { api } from '../api/client'
import { fmtTime } from '../utils/time'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  detectionId: { type: [Number, String], default: null },
})
const emit = defineEmits(['update:modelValue'])

const PLATFORM_MAP = {
  zhihu: { label: '知乎', color: '#06f', tag: 'primary' },
  weibo: { label: '微博', color: '#e6162d', tag: 'danger' },
  wechat: { label: '微信', color: '#07c160', tag: 'success' },
  douyin: { label: '抖音', color: '#25f4ee', tag: 'info' },
  other: { label: '其他', color: '#909399', tag: 'info' },
}
const GRADE_COLOR = { L1: 'info', L2: 'info', L3: 'warning', L4: 'danger', L5: 'danger' }

const state = ref({ loading: false, error: '', detail: null })

async function load(detId) {
  if (!detId) return
  state.value.loading = true
  state.value.error = ''
  try {
    const d = await api.detectionFull(detId)
    state.value.detail = d
  } catch (e) {
    state.value.error = e.message
    state.value.detail = null
  } finally {
    state.value.loading = false
  }
}

watch(
  () => [props.modelValue, props.detectionId],
  ([open, id]) => {
    if (open) {
      // 切卡/换检测时清空上次主动核查结果，避免残留跨卡
      socState.value = { loading: false, error: '', result: null }
      if (id) load(id)
    }
  },
)

const platform = computed(() => PLATFORM_MAP[state.value.detail?.platform_link?.platform] || PLATFORM_MAP.other)
const iframeUrl = computed(() => state.value.detail?.platform_link?.url || '')
const gradeReason = computed(() => state.value.detail?.detection?.grade_reason || [])
const hits = computed(() => state.value.detail?.speech_hits || [])
const alerts = computed(() => state.value.detail?.alerts || [])
const cases = computed(() => state.value.detail?.cases || [])
const events = computed(() => state.value.detail?.events || [])
const accounts = computed(() => state.value.detail?.accounts || [])

// 队2 社会工程学分析：兼容两种情况——/detections/{id}/full 的 detection.se_factors，
// 或 /scan/text 返回的顶层 se_analysis。
const seAnalysis = computed(() => {
  if (!state.value.detail) return null
  return (
    (state.value.detail.detection && state.value.detail.detection.se_factors) ||
    state.value.detail.se_analysis ||
    null
  )
})

// ---- 社工库防御（主动式）：HIBP + 本地泄露库 IOC 核查 ----
const IOC_TYPE_LABELS = {
  phone: '手机', email: '邮箱', qq: 'QQ', wechat: '微信', bank_card: '银行卡',
}
const socState = ref({ loading: false, error: '', result: null }) // 主动触发后的核查结果

// 归一化：兼容 {result:{analyzed,hits}} 与直接 {analyzed,hits:[...]} 两种响应形态
function normalizeSocResult(raw) {
  if (!raw || typeof raw !== 'object') return null
  const obj = raw.result && typeof raw.result === 'object' ? raw.result : raw
  const hits = Array.isArray(obj.hits) ? obj.hits : []
  return { analyzed: obj.analyzed ?? hits.length, hits }
}

// 被动数据：/detections/{id}/full 已携带 se_factors.soc_analysis 时直接展示
const passiveSoc = computed(() =>
  normalizeSocResult(state.value.detail?.detection?.se_factors?.soc_analysis),
)
// 展示用：主动触发结果优先，否则被动数据
const socAnalysis = computed(() => socState.value.result || passiveSoc.value)

function socSourceLabel(source) {
  if (source === 'hibp') return 'HIBP'
  if (source === 'local') return '本地'
  return source || '未接入'
}

async function triggerSocDefense() {
  const detId = props.detectionId
  if (!detId || socState.value.loading) return
  socState.value.loading = true
  socState.value.error = ''
  try {
    const raw = await api.socLibAnalyze(detId)
    socState.value.result = normalizeSocResult(raw)
  } catch (e) {
    socState.value.error = e.message
    socState.value.result = null
  } finally {
    socState.value.loading = false
  }
}

// 标签映射（攻击向量 / 心理技巧 / SEADM 阶段 / 生命周期）
const VECTOR_LABELS = {
  pretexting: '伪装身份', phishing: '钓鱼诱导', baiting: '诱饵',
  quid_pro_quo: '利益交换', tailgating: '信任链', unknown: '未识别',
}
const TECHNIQUE_LABELS = {
  urgency: '制造紧迫', authority: '权威施压', scarcity: '稀缺施压',
  social_proof: '社会认同', reciprocity: '互惠诱导', commitment: '承诺锁死',
}
const SEADM_LABELS = {
  relationship: '建立关系', exploitation: '利用', disclosure: '索取披露',
  execution: '诱导执行', completion: '完成目的', unknown: '未知',
}
const LIFECYCLE_LABELS = {
  break_ice: '破冰', persona: '人设', groom: '甜头', small_bait: '小额试水',
  big_invest: '大额投入', withdraw_block: '提现受阻', second_harvest: '二次收割',
  exit: '跑路',
}
const FACTOR_LABELS = {
  ...VECTOR_LABELS, ...TECHNIQUE_LABELS, ...SEADM_LABELS, ...LIFECYCLE_LABELS,
}

function openInNewTab() {
  if (iframeUrl.value) window.open(iframeUrl.value, '_blank', 'noopener')
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    :size="'52%'"
    direction="rtl"
    :title="`检测详情 #${detectionId ?? ''}`"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
    <div v-loading="state.loading" class="af-detail">
      <el-alert v-if="state.error" type="error" :title="state.error" :closable="false" style="margin-bottom: 12px" />

      <template v-if="state.detail">
        <!-- 顶部：平台分类 + 分级 -->
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 14px; flex-wrap: wrap">
          <el-tag :type="platform.tag" effect="dark">{{ platform.label }}</el-tag>
          <el-tag v-if="state.detail.detection.grade" :type="GRADE_COLOR[state.detail.detection.grade]" effect="dark">
            分级 {{ state.detail.detection.grade }}
          </el-tag>
          <el-tag v-if="state.detail.detection.llm_verdict" type="info">
            复核 {{ state.detail.detection.llm_verdict }}
          </el-tag>
          <span class="af-muted" style="font-size: 12px">{{ fmtTime(state.detail.detection.created_at) }}</span>
        </div>

        <!-- ① 告警原因 -->
        <el-card shadow="never" class="af-detail-card">
          <template #header><b>① 告警原因</b><span class="af-muted">（五级分级证据链）</span></template>
          <div v-if="gradeReason.length" class="af-reason-list">
            <div v-for="(r, i) in gradeReason" :key="i" class="af-reason-item">
              <el-tag size="small" :type="GRADE_COLOR[state.detail.detection.grade]" effect="plain">{{ r.signal }}</el-tag>
              <span class="af-reason-note">{{ r.note }}</span>
              <span class="af-muted" style="font-size: 12px">权重 {{ r.weight }}</span>
            </div>
          </div>
          <el-empty v-else description="无分级证据（未触发 L3+ 或纯规则未命中）" :image-size="60" />
        </el-card>

        <!-- ② 上下文 -->
        <el-card shadow="never" class="af-detail-card">
          <template #header><b>② 上下文</b><span class="af-muted">（原文 + 命中话术 + 关联）</span></template>
          <div class="af-mono" style="white-space: pre-wrap; margin-bottom: 10px; padding: 10px; background: #f5f7fa; border-radius: 4px">
            {{ state.detail.detection.content || '（无原文，可能已脱敏或来自事件聚合）' }}
          </div>
          <div v-if="hits.length" style="margin-bottom: 8px">
            <div class="af-muted" style="font-size: 12px; margin-bottom: 4px">命中话术（{{ hits.length }} 条）：</div>
            <el-tag v-for="h in hits" :key="h.id" size="small" style="margin: 0 6px 6px 0">
              {{ h.pattern || h.matched_text }}<span v-if="h.category" class="af-muted">（{{ h.category }}）</span>
            </el-tag>
          </div>
          <div v-if="accounts.length" style="margin-top: 8px">
            <div class="af-muted" style="font-size: 12px; margin-bottom: 4px">关联账号（{{ accounts.length }}）：</div>
            <el-tag v-for="a in accounts" :key="a.id" size="small" :type="a.risk_level === 'red' ? 'danger' : a.risk_level === 'yellow' ? 'warning' : 'success'" style="margin: 0 6px 6px 0">
              {{ a.url_name }}（{{ a.risk_level }}）
            </el-tag>
          </div>
        </el-card>

        <!-- ③ 平台跳转 -->
        <el-card shadow="never" class="af-detail-card">
          <template #header>
            <b>③ 平台跳转</b>
            <span class="af-muted">（内嵌 {{ platform.label }} 网页跳转问题点）</span>
            <el-button size="small" type="primary" plain style="float: right" @click="openInNewTab" :disabled="!iframeUrl">
              在新窗口打开 ↗
            </el-button>
          </template>
          <div v-if="iframeUrl" style="height: 360px; border: 1px solid #ebeef5; border-radius: 4px; overflow: hidden">
            <iframe :src="iframeUrl" style="width: 100%; height: 100%; border: none" sandbox="allow-same-origin allow-scripts allow-forms allow-popups" loading="lazy" />
          </div>
          <el-empty v-else :description="`${platform.label}暂无问题点 URL，可手动前往平台搜索或在新窗口打开平台首页`" :image-size="60">
            <el-button v-if="iframeUrl === '' && platform.home" size="small" @click="window.open(platform.home, '_blank')">打开{{ platform.label }}首页</el-button>
          </el-empty>
          <div class="af-muted" style="font-size: 12px; margin-top: 6px">
            ⚠ 若平台禁止内嵌（X-Frame-Options），请使用「在新窗口打开」按钮跳转。
          </div>
        </el-card>

        <!-- ③’ 社会工程学分析（队2：攻击向量 / 心理技巧 / SEADM 阶段 / 生命周期 / 证据） -->
        <el-card shadow="never" class="af-detail-card">
          <template #header>
            <b>社会工程学分析</b>
            <span class="af-muted">（确定性规则层推理链，不依赖 LLM）</span>
            <el-tag v-if="seAnalysis && seAnalysis.confidence" size="small" type="warning" plain style="float: right">
              置信度 {{ seAnalysis.confidence.toFixed(3) }}
            </el-tag>
          </template>

          <template v-if="seAnalysis && seAnalysis.attack_vector !== 'unknown' || (seAnalysis && seAnalysis.evidence && seAnalysis.evidence.length)">
            <div class="af-se-row">
              <span class="af-muted af-se-col">攻击向量</span>
              <el-tag size="small" type="danger">{{ VECTOR_LABELS[seAnalysis.attack_vector] || seAnalysis.attack_vector }}</el-tag>
            </div>
            <div class="af-se-row">
              <span class="af-muted af-se-col">心理技巧</span>
              <template v-if="seAnalysis.psych_techniques && seAnalysis.psych_techniques.length">
                <el-tag v-for="t in seAnalysis.psych_techniques" :key="t" size="small" type="warning" style="margin: 0 6px 6px 0">
                  {{ TECHNIQUE_LABELS[t] || t }}
                </el-tag>
              </template>
              <span v-else class="af-muted" style="font-size: 12px">无</span>
            </div>
            <div class="af-se-row">
              <span class="af-muted af-se-col">SEADM 阶段</span>
              <el-tag size="small" type="primary">{{ SEADM_LABELS[seAnalysis.seadm_stage] || seAnalysis.seadm_stage }}</el-tag>
            </div>
            <div class="af-se-row">
              <span class="af-muted af-se-col">生命周期</span>
              <el-tag size="small" type="success">{{ LIFECYCLE_LABELS[seAnalysis.scam_lifecycle] || seAnalysis.scam_lifecycle }}</el-tag>
            </div>

            <div v-if="seAnalysis.evidence && seAnalysis.evidence.length" style="margin-top: 8px">
              <div class="af-muted" style="font-size: 12px; margin-bottom: 4px">证据（{{ seAnalysis.evidence.length }} 条）：</div>
              <div v-for="(e, i) in seAnalysis.evidence" :key="i" class="af-evidence-item">
                <el-tag size="small" effect="plain" style="margin-right: 6px">{{ FACTOR_LABELS[e.factor] || e.factor }}</el-tag>
                <code class="af-se-matched">{{ e.matched }}</code>
                <span class="af-se-text">“{{ e.text }}”</span>
              </div>
            </div>
          </template>
          <el-empty v-else description="未识别到社会工程学信号" :image-size="60" />
        </el-card>

        <!-- ③’’ 社工库防御（主动式）：HIBP + 本地泄露库 IOC 核查 -->
        <el-card shadow="never" class="af-detail-card">
          <template #header>
            <b>社工库防御（主动式）</b>
            <span class="af-muted">（HIBP + 本地泄露库 IOC 核查）</span>
            <el-button
              size="small"
              type="warning"
              plain
              style="float: right"
              :loading="socState.loading"
              :disabled="socState.loading || !detectionId"
              @click="triggerSocDefense"
            >
              触发主动防御
            </el-button>
          </template>

          <div class="af-muted" style="font-size: 12px; margin-bottom: 8px">
            ⚠ 合规声明：仅用于安全防御自查，IOC 明文不出库、仅展示掩码；不对外披露、不用于任何非法用途。
          </div>

          <el-alert v-if="socState.error" type="error" :title="socState.error" :closable="false" style="margin-bottom: 8px" />

          <template v-if="socAnalysis && socAnalysis.hits.length">
            <div class="af-muted" style="font-size: 12px; margin-bottom: 6px">
              共分析 {{ socAnalysis.analyzed ?? socAnalysis.hits.length }} 类 IOC，{{ socAnalysis.hits.length }} 条结果：
            </div>
            <div v-for="(h, i) in socAnalysis.hits" :key="i" class="af-soc-row">
              <el-tag size="small" type="info" effect="plain" style="margin-right: 6px">
                {{ IOC_TYPE_LABELS[h.ioc_type] || h.ioc_type || '未知' }}
              </el-tag>
              <code class="af-se-matched">{{ h.ioc_value_mask || '—' }}</code>
              <el-tag size="small" :type="h.found ? 'danger' : 'success'">{{ h.found ? '命中' : '未命中' }}</el-tag>
              <span class="af-muted" style="font-size: 12px; margin-left: auto">
                {{ socSourceLabel(h.source) }}<template v-if="h.breach"> · {{ h.breach }}</template>
              </span>
            </div>
          </template>
          <el-empty v-else description="暂无社工库核查结果，可点击「触发主动防御」立即核查" :image-size="60" />
        </el-card>

        <!-- ④ 详细分析 -->
        <el-card shadow="never" class="af-detail-card">
          <template #header><b>④ 详细分析</b><span class="af-muted">（话术明细 + 案例 + 事件时间线）</span></template>
          <el-table :data="hits" size="small" max-height="200" style="margin-bottom: 12px">
            <el-table-column prop="pattern" label="命中词库" />
            <el-table-column prop="category" label="分类" width="130" />
            <el-table-column prop="score" label="得分" width="70" />
            <el-table-column prop="weight" label="权重" width="70" />
          </el-table>
          <div v-if="cases.length" style="margin-bottom: 8px">
            <div class="af-muted" style="font-size: 12px; margin-bottom: 4px">关联案例（{{ cases.length }}）：</div>
            <el-tag v-for="c in cases" :key="c.id" size="small" type="success" style="margin: 0 6px 6px 0">
              案例 #{{ c.id }}<span class="af-muted">（{{ (c.graph_tags || []).join('、') }}）</span>
            </el-tag>
          </div>
          <el-timeline v-if="events.length" style="padding-left: 4px">
            <el-timeline-item v-for="e in events" :key="e.id" :timestamp="fmtTime(e.ts)" :type="e.kind.includes('alert') ? 'danger' : 'primary'">
              {{ e.kind }}
            </el-timeline-item>
          </el-timeline>
          <el-empty v-if="!hits.length && !cases.length && !events.length" description="无更多分析数据" :image-size="60" />
        </el-card>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.af-detail { padding: 0 4px; }
.af-detail-card { margin-bottom: 14px; }
.af-reason-list { display: flex; flex-direction: column; gap: 6px; }
.af-reason-item { display: flex; align-items: center; gap: 8px; }
.af-reason-note { flex: 1; font-size: 13px; }
.af-mono { font-family: Consolas, Monaco, monospace; }
.af-muted { color: #606266; }
.af-se-row { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; }
.af-se-col { width: 70px; flex-shrink: 0; font-size: 12px; }
.af-evidence-item { display: flex; align-items: baseline; gap: 6px; margin-bottom: 5px; flex-wrap: wrap; }
.af-se-matched { font-size: 12px; color: #b88230; background: #fdf6ec; padding: 1px 5px; border-radius: 3px; }
.af-se-text { font-size: 12px; color: #606266; flex: 1; }
.af-soc-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
</style>
