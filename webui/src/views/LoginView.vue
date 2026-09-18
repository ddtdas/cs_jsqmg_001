<script setup>
// 登录页：两种方式
//   1. 一键登录（本机）：POST /api/v1/auth/local-login —— 仅回环来源（127.0.0.1/::1/localhost）
//      可调，直接返回 bootstrap admin key，免读文件/免配环境变量（P7/R1 开门即用）。
//      成功后把返回的 key 写入 auth store（sessionStorage 存储逻辑不变），跳转 /dashboard。
//   2. 手动登录：粘贴 admin key 调用受保护端点验证（供非本机/手动场景）。
import { reactive, ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { api } from '../api/client'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const form = reactive({ key: '' })
const bootInfo = ref(null)
const loading = ref(false)
const localLoading = ref(false)

onMounted(async () => {
  try {
    bootInfo.value = await api.bootstrapInfo()
  } catch (e) {
    bootInfo.value = { error: e.message }
  }
})

// local-login 仅本机可用：403 loopback_only → 引导手动输入 / 提示在本机浏览器打开
function describeLocalLoginError(e) {
  if (e && (e.status === 403 || e.code === 'loopback_only' || e.code === 'forbidden')) {
    return '一键登录仅限本机（127.0.0.1）使用：请在本机浏览器打开本页面，或改用下方手动输入 admin key'
  }
  return e?.message || '一键登录失败，请改用手动输入 admin key'
}

async function onLocalLogin() {
  localLoading.value = true
  try {
    // 1) 本机端点直接拿 key（前端拿不到任何 key 凭据，全部来自主控）
    const data = await api.localLogin()
    if (!data || !data.key) throw new Error('主控未返回 admin key')
    // 2) 复用原登录流程：key 写入 auth store（sessionStorage）+ 受保护端点验证
    const res = await auth.login(data.key)
    if (res.ok) {
      ElMessage.success('一键登录成功')
      router.push(route.query.redirect || '/dashboard')
    } else {
      ElMessage.error(res.error || '登录验证失败')
    }
  } catch (e) {
    ElMessage.error(describeLocalLoginError(e))
  } finally {
    localLoading.value = false
  }
}

async function onSubmit() {
  if (!form.key.trim()) {
    ElMessage.warning('请输入 API Key')
    return
  }
  loading.value = true
  const res = await auth.login(form.key)
  loading.value = false
  if (res.ok) {
    ElMessage.success('登录成功')
    router.push(route.query.redirect || '/dashboard')
  } else {
    ElMessage.error(res.error || '登录失败')
  }
}
</script>

<template>
  <div style="height: 100%; display: flex; align-items: center; justify-content: center; background: linear-gradient(135deg, #1f2a3d 0%, #2b3a55 100%)">
    <el-card style="width: 440px">
      <template #header>
        <div style="text-align: center">
          <div style="font-size: 28px">🛡️</div>
          <h2 style="margin: 8px 0 4px">金丝雀蜜罐 · CanaryGuard</h2>
          <div class="af-muted">蜜罐反诈双形态主控 WebUI</div>
        </div>
      </template>

      <el-alert
        v-if="bootInfo && bootInfo.hint"
        type="info"
        :closable="false"
        style="margin-bottom: 16px"
        show-icon
      >
        <div>{{ bootInfo.hint }}</div>
      </el-alert>
      <el-alert
        v-else-if="bootInfo && bootInfo.error"
        type="warning"
        :closable="false"
        style="margin-bottom: 16px"
        show-icon
        :title="`无法连接主控：${bootInfo.error}`"
      />

      <!-- 一键登录（本机） -->
      <el-alert type="success" :closable="false" style="margin-bottom: 12px" show-icon>
        本地项目打开即用，点击「一键登录」即可；admin key 也可在
        <router-link to="/settings" style="color: #67c23a; font-weight: 600">设置页</router-link>
        查看 / 重置。
      </el-alert>
      <el-button
        type="primary"
        size="large"
        style="width: 100%; margin-bottom: 4px"
        :loading="localLoading"
        @click="onLocalLogin"
      >
        ⚡ 一键登录（本机）
      </el-button>
      <div class="af-muted" style="margin: 6px 0 14px; font-size: 12px; text-align: center">
        仅本机回环（127.0.0.1 / localhost）可用；若从其它电脑访问：请让管理员把登录口令发给你，粘贴到下面输入框；口令在设置页可查看 / 重置。
      </div>

      <el-divider style="margin: 10px 0 16px">
        <span class="af-muted" style="font-size: 12px">或手动输入 admin key</span>
      </el-divider>

      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="API Key（bootstrap admin key）">
          <el-input
            v-model="form.key"
            type="password"
            show-password
            placeholder="af_admin_xxxxxxxx"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <div class="af-muted" style="margin-bottom: 16px">
          Key 位于主控 data/bootstrap_admin_key.txt（首次启动自动生成），
          通过 X-API-Key / Bearer 请求头注入。
        </div>
        <el-button type="primary" style="width: 100%" :loading="loading" @click="onSubmit">
          登 录
        </el-button>
      </el-form>
    </el-card>
  </div>
</template>