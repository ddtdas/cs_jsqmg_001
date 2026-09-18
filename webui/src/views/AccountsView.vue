<script setup>
// 账号速查：输入 URL/昵称 → 绿黄红 + 证据链 + 共现账号（POST /accounts/check）
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'

const form = reactive({ urlName: '' })
const checking = ref(false)
const error = ref('')
const result = ref(null)

const riskMeta = {
  green: { type: 'success', label: '绿 · 低风险', color: '#67c23a' },
  yellow: { type: 'warning', label: '黄 · 需关注', color: '#e6a23c' },
  red: { type: 'danger', label: '红 · 高风险', color: '#f56c6c' },
}

async function doCheck() {
  if (!form.urlName.trim()) {
    ElMessage.warning('请输入知乎 url_token / 昵称')
    return
  }
  checking.value = true
  error.value = ''
  try {
    result.value = await api.accountCheck(form.urlName.trim())
  } catch (e) {
    error.value = e.message
    result.value = null
  } finally {
    checking.value = false
  }
}
</script>

<template>
  <div class="af-page">
    <el-card shadow="never" class="af-card">
      <template #header><b>账号速查</b><span class="af-muted" style="margin-left: 8px">（公开/自有信号，D7 不越权）</span></template>
      <div style="display: flex; gap: 8px; max-width: 560px">
        <el-input v-model="form.urlName" placeholder="输入知乎 url_token / 昵称" @keyup.enter="doCheck" />
        <el-button type="primary" :loading="checking" @click="doCheck">速查</el-button>
      </div>
    </el-card>

    <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-bottom: 16px" />

    <template v-if="result">
      <el-card shadow="never" class="af-card">
        <template #header>
          <div style="display: flex; align-items: center; gap: 12px">
            <b>画像：#{{ result.account_id }} · {{ result.url_name }}</b>
            <el-tag :type="riskMeta[result.risk_level]?.type || 'info'" size="small">
              {{ riskMeta[result.risk_level]?.label || result.risk_level }}
            </el-tag>
            <span class="af-muted">风险分 {{ result.score }}</span>
          </div>
        </template>

        <el-divider content-position="left">证据链（{{ (result.evidence || []).length }} 条）</el-divider>
        <el-table :data="result.evidence || []" size="small" border>
          <el-table-column prop="signal" label="信号" width="220" />
          <el-table-column prop="weight" label="权重" width="80" />
          <el-table-column prop="note" label="说明" show-overflow-tooltip />
        </el-table>

        <el-divider content-position="left">共现账号（{{ (result.co_occurring_accounts || []).length }}）</el-divider>
        <el-tag v-for="c in result.co_occurring_accounts || []" :key="typeof c === 'string' ? c : c.url_name" size="small" style="margin-right: 6px">
          {{ typeof c === 'string' ? c : c.url_name }}
        </el-tag>
        <div v-if="!(result.co_occurring_accounts || []).length" class="af-muted">无共现记录</div>

        <el-divider content-position="left">信号明细</el-divider>
        <pre class="af-mono" style="margin: 0">{{ JSON.stringify(result.signals || {}, null, 2) }}</pre>
      </el-card>
    </template>
  </div>
</template>