<script setup>
// 知乎通道：通道健康（/zhihu/channels）+ 综合状态（/zhihu/status）
// + cookie 注入（HITL：用户从自己浏览器导出）/ 校验 + 手动导入入口
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'

const channels = ref([])
const status = ref(null)
const loading = ref(false)
const error = ref('')

const login = reactive({ show: false, channel: 'ch-a', cookie: '', scheme: 'header', submitting: false })
const verify = reactive({ submitting: false, channel: 'ch-a' })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [chs, st] = await Promise.all([api.zhihuChannels(), api.zhihuStatus()])
    channels.value = chs.channels || chs || []
    status.value = st
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
load()

function openLogin(channel) {
  login.show = true
  login.channel = channel
  login.cookie = ''
}

async function submitLogin() {
  if (!login.cookie.trim()) {
    ElMessage.warning('请粘贴 cookie')
    return
  }
  login.submitting = true
  try {
    const r = await api.zhihuLogin(login.channel, login.cookie.trim(), login.scheme)
    ElMessage.success(`cookie 已注入 ${login.channel}（${r.channel || 'ok'}）`)
    login.show = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    login.submitting = false
  }
}

async function doVerify(channel) {
  verify.submitting = true
  try {
    const r = await api.zhihuVerify(channel)
    if (r.ok) ElMessage.success(`${channel} cookie 校验通过`)
    else ElMessage.warning(`${channel} 校验未通过：${r.note || r.reason || 'cookie 无效'}`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    verify.submitting = false
  }
}
</script>

<template>
  <div class="af-page">
    <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-bottom: 16px" />

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>四通道健康</b>
              <el-button size="small" @click="load">刷新</el-button>
            </div>
          </template>
          <el-table :data="channels" v-loading="loading" size="small">
            <el-table-column prop="channel" label="通道" width="80" />
            <el-table-column label="状态" width="100">
              <template #default="{ row }">
                <el-tag :type="row.ok ? (row.degraded ? 'warning' : 'success') : 'danger'" size="small">
                  {{ row.ok ? (row.degraded ? '降级' : '正常') : '不可用' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="note" label="说明" show-overflow-tooltip />
          </el-table>
          <div class="af-muted mt8">CH-A 主通道（Playwright 会话）/ CH-B（httpx）/ CH-C（RSSHub）/ CH-D（手动导入）</div>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never" class="af-card">
          <template #header><b>综合状态</b></template>
          <template v-if="status">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="限速容量">{{ status.rate_limit?.capacity }}</el-descriptions-item>
              <el-descriptions-item label="可用额度">{{ status.rate_limit?.available }}</el-descriptions-item>
              <el-descriptions-item label="退避冷却">{{ Object.keys(status.backoff?.channels || {}).length }} 通道</el-descriptions-item>
              <el-descriptions-item label="配置">{{ status.rate_limit?.config }}</el-descriptions-item>
            </el-descriptions>
            <el-table :data="Object.entries(status.sessions || {}).map(([k, v]) => ({ channel: k, ...v }))" size="small" style="margin-top: 12px">
              <el-table-column prop="channel" label="会话" width="80" />
              <el-table-column prop="status" label="状态" width="100" />
              <el-table-column prop="health" label="健康" width="90" />
              <el-table-column label="cookie" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.has_cookie ? 'success' : 'info'" size="small">{{ row.has_cookie ? '已注入' : '未注入' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="last_sync" label="最近同步" />
            </el-table>
          </template>
          <el-empty v-else-if="!loading" description="暂无状态" />
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="af-card">
      <template #header><b>会话管理（cookie 注入 / 校验）</b></template>
      <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center">
        <span class="af-muted" style="max-width: 420px">
          HITL：cookie 由你从自己浏览器导出后粘贴（系统不代登录），Fernet 加密落库、明文不回显。仅 ch-a/ch-b 需要登录态。
        </span>
        <el-button size="small" type="primary" @click="openLogin('ch-a')">注入 CH-A cookie</el-button>
        <el-button size="small" type="primary" plain @click="openLogin('ch-b')">注入 CH-B cookie</el-button>
        <el-button size="small" type="success" plain :loading="verify.submitting" @click="doVerify('ch-a')">校验 CH-A</el-button>
        <el-button size="small" type="success" plain @click="doVerify('ch-b')">校验 CH-B</el-button>
      </div>
    </el-card>

    <el-dialog v-model="login.show" :title="`注入 cookie（${login.channel}）`" width="620px">
      <el-form label-position="top">
        <el-form-item label="cookie 格式">
          <el-radio-group v-model="login.scheme">
            <el-radio value="header">header 字符串（name=value; ...）</el-radio>
            <el-radio value="json">浏览器导出 JSON</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="login.scheme === 'header' ? 'cookie 内容' : 'JSON 内容'">
          <el-input v-model="login.cookie" type="textarea" :rows="7" placeholder="粘贴 cookie…" />
        </el-form-item>
      </el-form>
      <div class="af-muted">合规：只读自有/公开数据；令牌桶限速（AF_ZHIHU_RATE_LIMIT=20/min）。</div>
      <template #footer>
        <el-button @click="login.show = false">取消</el-button>
        <el-button type="primary" :loading="login.submitting" @click="submitLogin">加密保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>