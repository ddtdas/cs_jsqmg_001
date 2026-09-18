<script setup>
// 设置页（R2）：admin key 查看 / 复制 / 重置 + MCP 接入配置示意 + 安全提示。
// 后端端点（P7/R1 本机模式，仅回环来源可调，非回环 403）：
//   GET  /api/v1/auth/current-key  当前 key 明文
//   POST /api/v1/auth/reset-key    confirm=true + reason>=20 字（GateLog 审计）
// 非本机访问（403 loopback_only）时本页一律降级为"仅本机可查看/可用"提示，不泄露任何 key 内容。
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

// ---------- 当前 key ----------
const keyState = ref('loading') // loading | ok | loopback_only | error
const keyInfo = ref(null) // { key, source }（P2-A：不再返回 key_file 绝对路径）
const keyError = ref('')
const bootInfo = ref(null)

// 重置后展示的新 key（el-alert 高亮 + 复制）
const newKey = ref('')

// key 脱敏显示：af_admin_****（审查 P2-8：不明文回显）
function maskKey(k) {
  if (!k) return ''
  if (k.length <= 8) return '****'
  return `${k.slice(0, 8)}****`
}

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(okMsg || '已复制')
  } catch {
    ElMessage.error('复制失败，请手动复制')
  }
}

async function loadKey() {
  keyState.value = 'loading'
  try {
    const info = await api.currentKey()
    keyInfo.value = info || {}
    keyState.value = 'ok'
  } catch (e) {
    if (e.status === 403 || e.code === 'loopback_only' || e.code === 'forbidden') {
      keyState.value = 'loopback_only'
    } else if (e.status === 401) {
      keyState.value = 'error'
      keyError.value = '登录态失效，请重新登录'
    } else {
      keyState.value = 'error'
      keyError.value = e.message || '获取 key 失败'
    }
  }
}

// ---------- 重置 key ----------
const resetVisible = ref(false)
const resetConfirm = ref(false)
const resetReason = ref('')
const resetting = ref(false)

const canReset = computed(() => resetConfirm.value && resetReason.value.trim().length >= 20)

function openReset() {
  resetConfirm.value = false
  resetReason.value = ''
  resetVisible.value = true
}

async function onReset() {
  if (!canReset.value) return
  resetting.value = true
  try {
    const data = await api.resetKey(resetReason.value)
    newKey.value = data?.key || ''
    // 旧 key 已失效：用新 key 重登保持会话（auth.login 内部写 sessionStorage + 验证，逻辑不变）
    const res = await auth.login(data.key)
    if (res.ok) {
      ElMessage.success('已重置：新 key 生效，旧 key 已失效，本会话已自动切换新 key')
    } else {
      auth.logout()
      ElMessage.warning('已重置，但自动切换会话失败，请复制新 key 后重新登录')
      await router.push('/login')
    }
    resetVisible.value = false
    resetReason.value = ''
    resetConfirm.value = false
    // 重置后 key 文件/来源可能变化（始终以主控为准）
    await loadKey()
  } catch (e) {
    if (e.code === 'env_key_locked') {
      ElMessage.error('当前 key 由环境变量 AF_API_KEY 提供，请直接修改环境变量（本页不能重置）')
    } else {
      ElMessage.error(e.message || '重置失败')
    }
  } finally {
    resetting.value = false
  }
}

// ---------- MCP 接入配置 ----------
const masterUrl = computed(() => {
  const port = bootInfo.value?.port || 9200
  return `http://127.0.0.1:${port}`
})

const mcpConfig = computed(() => {
  const k = keyInfo.value?.key || ''
  return `AF_MASTER_URL=${masterUrl.value}\nAF_API_KEY=${k}`
})

function copyMcpConfig() {
  if (keyState.value !== 'ok' || !keyInfo.value?.key) return
  copyText(mcpConfig.value, 'MCP 接入配置已复制（AF_MASTER_URL + AF_API_KEY）')
}

// 仅本机可用提示（非回环访问时整页 show）
const localOnly = computed(() => keyState.value === 'loopback_only')

onMounted(async () => {
  // bootstrap-info 公开端点：拿真实端口拼 MCP 配置
  try {
    bootInfo.value = await api.bootstrapInfo()
  } catch { /* 端口取默认 9200 */ }
  loadKey()
})
</script>

<template>
  <div style="max-width: 860px; margin: 0 auto; padding: 24px">
    <h2 style="margin: 0 0 4px">设置</h2>
    <div class="af-muted" style="margin-bottom: 20px">
      admin key 管理（仅本机可查看 / 重置）与 MCP 接入配置
    </div>

    <!-- 非本机访问：整页降级提示，不展示任何 key 内容 -->
    <el-alert
      v-if="localOnly"
      type="warning"
      :closable="false"
      show-icon
      style="margin-bottom: 16px"
      title="仅本机可查看"
    >
      <div>当前访问来源非本机回环（127.0.0.1 / ::1 / localhost），key 内容与重置操作不可用（后端 403 loopback_only）。请在本机浏览器打开 http://127.0.0.1:9200/ui/ 后查看 / 重置。</div>
    </el-alert>

    <!-- 1. 当前 admin key -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between">
          <span>🔑 当前 admin key</span>
          <el-tag v-if="keyState === 'ok'" size="small" type="success">本机可见</el-tag>
          <el-tag v-else-if="keyState === 'loopback_only'" size="small" type="warning">仅本机</el-tag>
          <el-tag v-else-if="keyState === 'error'" size="small" type="danger">获取失败</el-tag>
        </div>
      </template>

      <div v-if="keyState === 'loading'" class="af-muted">加载中…</div>

      <div v-else-if="keyState === 'ok'" style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
        <el-input
          :model-value="maskKey(keyInfo.key)"
          readonly
          style="max-width: 260px; font-family: var(--af-mono, monospace)"
        />
        <el-button type="primary" plain @click="copyText(keyInfo.key, 'admin key 已复制（仅本机会话内使用）')">
          复制完整 key
        </el-button>
        <span class="af-muted" style="font-size: 12px">
          来源：{{ keyInfo.source === 'env' ? '环境变量 AF_API_KEY' : '文件 data/bootstrap_admin_key.txt' }}
        </span>
      </div>

      <div v-else-if="localOnly" class="af-muted">
        非本机访问：'仅本机可查看'（key 内容不展示）——请在本机浏览器打开设置页。
      </div>

      <div v-else class="af-muted">key 获取失败：{{ keyError || '请检查主控是否运行' }}</div>

      <!-- 重置成功后的新 key 高亮提示 -->
      <el-alert
        v-if="newKey"
        type="success"
        :closable="false"
        show-icon
        style="margin-top: 14px"
      >
        <div style="margin-bottom: 8px">
          已重置，请妥善保存：<strong class="af-mono">{{ newKey }}</strong>；旧 key 已失效。
        </div>
        <el-button size="small" type="success" plain @click="copyText(newKey, '新 admin key 已复制')">
          复制新 key
        </el-button>
      </el-alert>
    </el-card>

    <!-- 2. 重置 key -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header><span>♻️ 重置 admin key</span></template>
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
        <span class="af-muted" style="font-size: 13px">
          重置后旧 key 立即失效，需二次确认并填写理由（≥20 字，写入审计日志 GateLog）。
        </span>
        <el-button type="danger" plain :disabled="localOnly" @click="openReset">重置 admin key</el-button>
        <el-tag v-if="keyInfo?.source === 'env'" type="info" size="small">env 托管：不可经页面重置</el-tag>
      </div>
    </el-card>

    <!-- 3. MCP 接入提示卡 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between">
          <span>🤖 MCP 接入配置（dsh / Claude Code / Codex）</span>
          <el-button
            v-if="keyState === 'ok'"
            size="small"
            type="primary"
            plain
            @click="copyMcpConfig"
          >
            一键复制
          </el-button>
        </div>
      </template>
      <template v-if="keyState === 'ok'">
        <div class="af-muted" style="margin-bottom: 10px; font-size: 13px">
          将以下环境变量填入 dsh / Claude Code / Codex 的 MCP 服务端配置，即指向本主控（仅本机回环安全）。
        </div>
        <pre style="background: #1f2a3d; color: #d8e2f2; border-radius: 6px; padding: 14px; margin: 0 0 12px; font-size: 13px; line-height: 1.8; overflow: auto"><code>{{ mcpConfig }}</code></pre>
      </template>
      <template v-else>
        <div class="af-muted">
          {{ localOnly ? '非本机访问：MCP 配置含当前 key，仅本机可查看。' : '获取 key 后可查看 MCP 接入配置。' }}
        </div>
      </template>
    </el-card>

    <!-- 4. 安全提示 -->
    <el-alert
      type="warning"
      :closable="false"
      show-icon
      title="安全提示"
    >
      <div>
        本主控为本地项目，仅建议本机使用（key 类端点仅回环可调）。若需对外暴露访问，请务必先在设置页
        <router-link to="/settings" style="color: inherit; font-weight: 600">重置 key</router-link>
        并配合防火墙 / 反代限制来源 IP。
      </div>
    </el-alert>

    <!-- 重置二次确认弹窗 -->
    <el-dialog
      v-model="resetVisible"
      title="重置 admin key（二次确认）"
      width="480px"
      :close-on-click-modal="false"
      append-to-body
    >
      <el-alert type="error" :closable="false" show-icon style="margin-bottom: 16px">
        <div>重置后当前 admin key <strong>立即失效</strong>（api_keys 哈希停用 + GateLog 审计），所有使用旧 key 的会话 / MCP 客户端将断开。请确认已备份或可接受重新登录。</div>
      </el-alert>
      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="resetConfirm">
            我确认要重置 admin key
          </el-checkbox>
        </el-form-item>
        <el-form-item
          label="重置理由（≥20 字，写入审计日志 GateLog）"
          :error="resetReason.trim().length > 0 && resetReason.trim().length < 20 ? `至少 20 字，当前 ${resetReason.trim().length} 字` : ''"
        >
          <el-input
            v-model="resetReason"
            type="textarea"
            :rows="3"
            maxlength="200"
            show-word-limit
            placeholder="例如：怀疑 key 已泄露，为对外暴露访问前做安全加固"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="danger" :loading="resetting" :disabled="!canReset" @click="onReset">
          确认重置
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>