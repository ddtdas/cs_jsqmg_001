<script setup>
// 命令输入面板：内嵌 DSH（DeepSeek Harness）作为 AI agent 命令输入。
// 窗口内带窗口：iframe 内嵌 DSH Web GUI。
// 默认指向**项目自包含独立实例**（dsh/，端口 3092，0.1.5 token 鉴权）——
// 不再连设备全局 3080。启动：项目根 `dsh\start.bat`（或 node dsh/start.mjs）。
// 地址仍可手动切换/保存（localStorage af_dsh_console_url）。
import { reactive, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'

const EMBEDDED_PORT = 3092
const DEFAULT_DSH = `http://127.0.0.1:${EMBEDDED_PORT}/`   // 裸入口（token 鉴权下 401，仅兜底）
const STORE_KEY = 'af_dsh_console_url'

const state = reactive({
  url: localStorage.getItem(STORE_KEY) || DEFAULT_DSH,
  iframeUrl: localStorage.getItem(STORE_KEY) || DEFAULT_DSH,
  loading: true,
  launching: false,  // 一键拉起进行中
  dshOnline: null,   // null=检测中 / true / false
  embedded: { running: false, tokenUrl: '' },
})

async function probeDsh() {
  state.dshOnline = null
  try {
    const base = (state.url || '').split('/#')[0] || DEFAULT_DSH.split('/#')[0]
    const ctrl = new AbortController()
    const t = setTimeout(() => ctrl.abort(), 4000)
    const res = await fetch(base + '/', { method: 'GET', signal: ctrl.signal })
    clearTimeout(t)
    // 0.1.5 token 鉴权下裸路径返回 401——视为"实例在线但需 token"，仍标记在线
    state.dshOnline = !!res || res.status === 401
  } catch {
    state.dshOnline = false
  }
}

/** 拉取后端提供的内嵌 DSH 状态：端口是否在跑 + 带 token 的完整 URL */
async function loadEmbedded() {
  try {
    const d = await api.embeddedDsh()
    state.embedded.running = !!d.running
    state.embedded.tokenUrl = d.token_url || ''
    if (d.token_url && (!localStorage.getItem(STORE_KEY) || localStorage.getItem(STORE_KEY) === DEFAULT_DSH)) {
      // 未手动改过地址 → 自动用带 token 的入口（0.1.5 鉴权）
      state.url = d.token_url
      state.iframeUrl = d.token_url
    } else if (d.running && !localStorage.getItem(STORE_KEY)) {
      state.iframeUrl = DEFAULT_DSH
    }
  } catch {
    /* 后端无此端点时忽略（旧版兼容） */
  }
}

/** 一键拉起 + 守护：调用时若内嵌 DSH 未运行则后台拉起（懒启动，无需心跳），
 *  然后轮询就绪态，就绪后自动加载带 token 入口。 */
let launchPollTimer = null
function clearLaunchPoll() {
  if (launchPollTimer) { clearTimeout(launchPollTimer); launchPollTimer = null }
}

async function launchDsh() {
  if (state.launching) return
  state.launching = true
  try {
    await api.startEmbeddedDsh()
    ElMessage.success('已在后台拉起内嵌 DSH，等待就绪…')
  } catch (e) {
    ElMessage.error('拉起失败：' + (e?.message || '请手动运行 dsh\\start.bat'))
    state.launching = false
    probeDsh()
    return
  }
  const deadline = Date.now() + 60000
  const poll = async () => {
    await loadEmbedded()
    if (state.embedded.running && state.embedded.tokenUrl) {
      // 尊重用户手动配置的自定义地址：仅未手动改过时自动切到带 token 入口
      const stored = localStorage.getItem(STORE_KEY)
      if (!stored || stored === DEFAULT_DSH) {
        state.url = state.embedded.tokenUrl
        state.iframeUrl = state.embedded.tokenUrl
      }
      clearLaunchPoll()
      state.launching = false
      probeDsh()
      return
    }
    if (Date.now() < deadline) launchPollTimer = setTimeout(poll, 3000)
    else { clearLaunchPoll(); state.launching = false; probeDsh() }
  }
  launchPollTimer = setTimeout(poll, 3000)
}

function applyUrl() {
  const u = (state.url || '').trim()
  if (!/^https?:\/\//.test(u)) {
    ElMessage.warning(`请输入完整地址，形如 http://127.0.0.1:${EMBEDDED_PORT}/`)
    return
  }
  state.iframeUrl = u
  try { localStorage.setItem(STORE_KEY, u) } catch { /* ignore */ }
  state.loading = true
  probeDsh()
  ElMessage.success('已切换 DSH 命令输入地址')
}

function openNewTab() {
  const u = state.iframeUrl || DEFAULT_DSH
  window.open(u, '_blank', 'noopener')
}

function onLoaded() {
  state.loading = false
}

onMounted(async () => {
  await loadEmbedded()
  // 调用即拉起（懒启动守护，无需心跳）：访问命令输入页时若内嵌 DSH 未运行则自动拉起
  if (!state.embedded.running) await launchDsh()
  else probeDsh()
})

onBeforeUnmount(clearLaunchPoll)
</script>

<template>
  <div class="af-page af-console">
    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap">
          <b>命令输入（内嵌 DSH Agent）</b>
          <span class="af-muted" style="font-size: 12px">在此直接与 AI agent 对话，下命令执行反诈侦查、供应链反查、证据整理等</span>
        </div>
      </template>
      <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px">
        <el-input v-model="state.url" placeholder="DSH 地址，如 http://127.0.0.1:3092/" style="flex: 1; min-width: 320px" @keyup.enter="applyUrl" />
        <el-button type="primary" @click="applyUrl">加载</el-button>
        <el-button plain @click="openNewTab">在新窗口打开 ↗</el-button>
      </div>
      <div class="af-muted" style="font-size: 12px; margin-bottom: 8px">
        提示：默认内嵌<b>项目自包含独立 DSH</b>（dsh/，端口 3092，全新 0.1.5 引擎 + 空白 dsh-home，
        与设备全局 3080 完全隔离）。启动：<code>dsh\start.bat</code>。若未启动将显示离线提示。
        地址可手动切换（会记住），浏览器策略限制时用「在新窗口打开」。
      </div>
    </el-card>

    <!-- P2 离线状态条：null=检测中；false 或内嵌未运行=warning+重试；在线=success -->
    <div style="margin-bottom: 10px">
      <el-alert v-if="state.dshOnline === null" type="info" :closable="false" show-icon title="正在检测内嵌 DSH 状态…" />
      <el-alert
        v-else-if="state.dshOnline === false || !state.embedded.running"
        type="warning"
        :closable="false"
        show-icon
        title="内嵌 DSH 未启动，请运行 dsh\start.bat"
      >
        <template #default>
          <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px">
            <span>提示：项目自包含独立 DSH（端口 {{ EMBEDDED_PORT }}）未就绪，命令输入暂不可用。</span>
            <div style="display: flex; gap: 8px; flex-shrink: 0">
              <el-button size="small" type="primary" plain :loading="state.launching" @click="launchDsh">一键拉起</el-button>
              <el-button size="small" type="warning" plain @click="probeDsh">重试</el-button>
            </div>
          </div>
        </template>
      </el-alert>
      <el-alert v-else type="success" :closable="false" show-icon title="内嵌 DSH 在线" />
    </div>

    <div class="af-console-frame" v-loading="state.loading">
      <iframe
        :src="state.iframeUrl"
        style="width: 100%; height: 100%; border: none"
        sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-downloads allow-modals"
        @load="onLoaded"
      />
    </div>
  </div>
</template>

<style scoped>
/* P1-1 修复：height:100% 会把 el-card 压成 3px（iframe min-height:620 抢占全部空间且无滚动）；
   改为 height:auto + min-height:100%，内容超高时页面随文档流增长、由 el-main 滚动；
   卡片与告警条 flex-shrink:0 保证不被压缩，地址输入/按钮/离线警告条恒可见。 */
.af-console { display: flex; flex-direction: column; height: auto; min-height: 100%; }
.af-console > div:not(.af-console-frame) { flex-shrink: 0; }
.af-console-frame { flex: 1; min-height: 620px; border: 1px solid #ebeef5; border-radius: 6px; overflow: hidden; background: #fff; }
</style>
