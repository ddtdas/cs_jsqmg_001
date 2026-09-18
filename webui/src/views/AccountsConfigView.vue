<script setup>
// 配置账号：平台矩阵（/account-bridges）+ 配置弹窗（config_fields 动态表单）
// + 测试连接 + 一键导入（agent-import MCP JSON/env/webhook）+ 私信接入测试（ingest）
// + 删除配置。合规（D7）：只读自有账号、凭据加密存储、HITL 不代登录。
import { nextTick, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api/client'

const platforms = ref([])
const loading = ref(false)
const error = ref('')

// ---------- 状态元信息 ----------
function statusMeta(s) {
  return (
    {
      unconfigured: { type: 'info', label: '未配置' },
      configured: { type: 'success', label: '已配置' },
      testing: { type: 'warning', label: '连接中' },
      error: { type: 'danger', label: '错误' },
    }[s] || { type: 'info', label: s || '未知' }
  )
}

function aiBadge(aiReady) {
  return aiReady ? { text: '✅ 可接入', cls: 'text-success' } : { text: '⚠️ 受限', cls: 'text-warning' }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.accountBridges()
    platforms.value = res?.platforms || res || []
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
load()

// ---------- 配置弹窗（config_fields 动态表单） ----------
const dialog = reactive({
  show: false,
  platform: '',
  hasConfig: false,
  fields: [],
  values: {},
  loading: false,
  submitting: false,
  detailError: '',
})

async function openConfig(row) {
  dialog.platform = row.platform
  dialog.hasConfig = !!row.has_config || row.status === 'configured'
  dialog.fields = []
  dialog.values = {}
  dialog.detailError = ''
  dialog.show = true
  dialog.loading = true
  try {
    const d = await api.accountBridgeDetail(row.platform)
    const detail = d?.platforms?.[0] || d || {}
    dialog.fields = detail.config_fields || []
    dialog.hasConfig = !!detail.has_config
    const v = {}
    for (const f of dialog.fields) v[f.key] = ''
    dialog.values = v
  } catch (e) {
    dialog.detailError = e.message
  } finally {
    dialog.loading = false
  }
}

async function saveConfig() {
  const missing = dialog.fields.filter((f) => !(dialog.values[f.key] || '').trim())
  if (missing.length) {
    ElMessage.warning(`请填写：${missing.map((f) => f.label).join('、')}`)
    return
  }
  const fields = {}
  for (const f of dialog.fields) fields[f.key] = (dialog.values[f.key] || '').trim()
  dialog.submitting = true
  try {
    await api.accountBridgeSave(dialog.platform, fields)
    ElMessage.success(`「${dialog.platform}」配置已加密保存`)
    dialog.show = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    dialog.submitting = false
  }
}

// ---------- 测试连接 ----------
const testingPlatform = ref('')
async function testBridge(row) {
  testingPlatform.value = row.platform
  try {
    const r = await api.accountBridgeTest(row.platform)
    const detail = r?.detail || r?.reason || ''
    if (r?.ok) ElMessage.success(`「${row.platform}」连接测试通过${r.latency_ms != null ? `（${r.latency_ms}ms）` : ''}${detail ? '：' + detail : ''}`)
    else ElMessage.warning(`「${row.platform}」测试未通过${r.latency_ms != null ? `（${r.latency_ms}ms）` : ''}：${detail || '未知原因'}`)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    testingPlatform.value = ''
    load() // 状态可能变为 testing/error，刷新
  }
}

// ---------- 一键导入（agent-import：MCP JSON + env + webhook） ----------
const importDlg = reactive({ show: false, platform: '', data: null, loading: false, err: '' })

async function openImport(row) {
  importDlg.platform = row.platform
  importDlg.data = null
  importDlg.err = ''
  importDlg.show = true
  importDlg.loading = true
  try {
    importDlg.data = await api.accountBridgeAgentImport(row.platform)
  } catch (e) {
    importDlg.err = e.message
  } finally {
    importDlg.loading = false
  }
}

async function copyText(text, tip) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(tip || '已复制')
  } catch {
    ElMessage.error('复制失败，请手动选择复制')
  }
}

// ---------- 私信接入测试（ingest → detection pending → 反向侦察） ----------
const ingest = reactive({
  platform: '',
  from: '',
  text: '',
  submitting: false,
  last: null,
})

function setIngestPlatform(row) {
  ingest.platform = row.platform
  nextTick(() => {
    const el = document.getElementById('dm-test-card')
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  })
}

async function doIngest() {
  if (!ingest.platform) {
    ElMessage.warning('请选择平台')
    return
  }
  if (!ingest.from.trim()) {
    ElMessage.warning('请输入来源账号')
    return
  }
  if (!ingest.text.trim()) {
    ElMessage.warning('请输入私信文本')
    return
  }
  ingest.submitting = true
  try {
    const r = await api.accountBridgeIngest(ingest.platform, {
      from: ingest.from.trim(),
      text: ingest.text.trim(),
    })
    ingest.last = r
    ElMessage.success(`已接入：检测 #${r.detection_id}（${r.status || 'pending'}）`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    ingest.submitting = false
  }
}

// ---------- 删除配置 ----------
async function removeBridge(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除「${row.platform}」的账号桥接配置？已保存的凭据将被清除，且无法恢复。`,
      '删除配置',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', autofocus: false }
    )
  } catch {
    return
  }
  try {
    await api.accountBridgeDelete(row.platform)
    ElMessage.success(`已删除「${row.platform}」配置`)
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}
</script>

<template>
  <div class="af-page">
    <!-- 顶部说明卡 -->
    <el-card shadow="never" class="af-card">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap">
        <div>
          <b>配置账号 · 私信自动接入 + 反向侦察</b>
          <div class="af-muted" style="margin-top: 6px; max-width: 860px; line-height: 1.8">
            本页用于配置各平台自有账号，使<b>私信自动接入本系统并展开反向侦察</b>（ingest → 检测流水线 →
            分级 → 账号速查/供应链，检测 #id 自动进入侧边栏「会话历史」）。合规：只读自有账号、不自动发布；
            凭据 Fernet 加密存储、明文不回显；HITL —— 由你从自己浏览器导出 cookie 后粘贴，系统不代登录。
          </div>
        </div>
        <div>
          <router-link :to="{ path: '/zhihu' }">
            <el-button size="small" type="primary" plain>知乎通道说明</el-button>
          </router-link>
          <el-button size="small" style="margin-left: 8px" @click="load">刷新</el-button>
        </div>
      </div>
    </el-card>

    <!-- 平台列表 -->
    <el-card shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <b>平台接入矩阵</b>
          <span class="af-muted">（/account-bridges）</span>
        </div>
      </template>
      <el-alert v-if="error" type="error" :title="error" :closable="false" style="margin-bottom: 12px" />
      <el-table :data="platforms" v-loading="loading" size="small" empty-text="暂无平台数据">
        <el-table-column label="平台" min-width="130">
          <template #default="{ row }">
            <div style="display: flex; align-items: center; gap: 6px">
              <b>{{ row.name || row.platform }}</b>
              <el-tooltip :content="row.ai_ready ? 'AI 可接入，可配置账号桥接' : 'AI 接入受限（如抖音），只读展示不可配置'" placement="top">
                <span :class="aiBadge(row.ai_ready).cls" style="font-size: 12px">{{ aiBadge(row.ai_ready).text }}</span>
              </el-tooltip>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="access" label="接入方式" min-width="140" show-overflow-tooltip />
        <el-table-column label="开源项目" min-width="180">
          <template #default="{ row }">
            <template v-if="(row.projects || []).length">
              <el-tooltip v-for="p in row.projects" :key="p.name" :content="p.note || p.name" placement="top" :show-after="300">
                <el-tag
                  size="small"
                  effect="plain"
                  style="margin: 2px 6px 2px 0; cursor: pointer"
                  @click="p.url && copyText(p.url, `${p.name} 项目链接已复制`)"
                >
                  {{ p.name }}
                </el-tag>
              </el-tooltip>
            </template>
            <span v-else class="af-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusMeta(row.status).type" size="small">{{ statusMeta(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_sync" label="最近同步" width="160" show-overflow-tooltip />
        <el-table-column label="操作" min-width="300">
          <template #default="{ row }">
            <template v-if="row.ai_ready">
              <el-button size="small" type="primary" plain @click="openConfig(row)">
                {{ row.has_config || row.status === 'configured' ? '修改配置' : '配置' }}
              </el-button>
              <el-button
                size="small"
                type="success"
                plain
                :loading="testingPlatform === row.platform"
                @click="testBridge(row)"
              >
                测试连接
              </el-button>
              <el-button size="small" @click="openImport(row)">一键导入</el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :disabled="row.status === 'unconfigured'"
                @click="removeBridge(row)"
              >
                删除
              </el-button>
              <el-button size="small" text type="primary" @click="setIngestPlatform(row)">私信测试</el-button>
            </template>
            <span v-else class="af-muted">⚠️ 只读展示，暂不可配置</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 私信接入测试区 -->
    <el-card id="dm-test-card" shadow="never" class="af-card">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <b>私信接入测试</b>
          <span class="af-muted">（POST /account-bridges/{platform}/ingest → 反向侦察闭环）</span>
        </div>
      </template>
      <el-row :gutter="16">
        <el-col :span="8">
          <el-form label-position="top">
            <el-form-item label="平台">
              <el-select v-model="ingest.platform" placeholder="选择平台" style="width: 100%">
                <el-option v-for="p in platforms.filter((x) => x.ai_ready)" :key="p.platform" :label="p.name || p.platform" :value="p.platform" />
              </el-select>
            </el-form-item>
            <el-form-item label="来源账号（from）">
              <el-input v-model="ingest.from" placeholder="如 wx_张三 / qq_12345 / zhihu_user_abc" />
            </el-form-item>
          </el-form>
        </el-col>
        <el-col :span="12">
          <el-form label-position="top">
            <el-form-item label="私信文本">
              <el-input v-model="ingest.text" type="textarea" :rows="5" placeholder="粘贴一条测试私信文本，模拟桥接器推送到主控…" />
            </el-form-item>
          </el-form>
        </el-col>
        <el-col :span="4" style="display: flex; align-items: flex-end">
          <el-button type="primary" :loading="ingest.submitting" style="width: 100%; margin-bottom: 18px" @click="doIngest">
            接入并侦察
          </el-button>
        </el-col>
      </el-row>
      <el-alert
        v-if="ingest.last"
        type="success"
        :closable="false"
        style="margin-top: 8px"
        :title="`已生成检测 #${ingest.last.detection_id}`"
      >
        <div>
          平台 {{ ingest.last.platform }} · 来源 {{ ingest.last.from }} · 状态
          <el-tag size="small" type="warning" style="margin: 0 4px">{{ ingest.last.status || 'pending' }}</el-tag>
          检测 #{{ ingest.last.detection_id }} 已进入反向侦察流水线，可在侧边栏「会话历史」/ 检测详情中查看；成功后本页列表已刷新。
        </div>
      </el-alert>
      <div class="af-muted mt8">提示：私信接入后由扫描/分级流水线消费（scan → grading → account_intel → supply_chain）。</div>
    </el-card>

    <!-- 配置弹窗 -->
    <el-dialog v-model="dialog.show" :title="`配置账号 · ${dialog.platform}`" width="620px" :close-on-click-modal="false">
      <div v-loading="dialog.loading" style="min-height: 80px">
        <el-alert v-if="dialog.detailError" type="error" :title="dialog.detailError" :closable="false" />
        <template v-else-if="dialog.fields.length">
          <el-form label-position="top">
            <el-form-item v-for="f in dialog.fields" :key="f.key" :label="f.label">
              <el-input
                v-if="f.type === 'password'"
                v-model="dialog.values[f.key]"
                type="password"
                show-password
                :placeholder="f.tip || f.label"
                autocomplete="new-password"
              />
              <el-input
                v-else-if="f.type === 'cookie'"
                v-model="dialog.values[f.key]"
                type="textarea"
                :rows="5"
                :placeholder="f.tip || '粘贴 cookie…（HITL：从自己浏览器导出）'"
              />
              <el-input
                v-else
                v-model="dialog.values[f.key]"
                :placeholder="f.tip || f.label"
              />
              <div v-if="f.tip" class="af-muted" style="margin-top: 4px">{{ f.tip }}</div>
            </el-form-item>
          </el-form>
          <div class="af-muted">
            合规：只读自有账号；敏感字段（cookie/token/password）Fernet 加密落库、明文不回显；保存后可在本页测试连接。
          </div>
        </template>
        <el-empty v-else-if="!dialog.loading && !dialog.detailError" description="该平台无配置字段" />
      </div>
      <template #footer>
        <el-button @click="dialog.show = false">取消</el-button>
        <el-button type="primary" :loading="dialog.submitting" @click="saveConfig">加密保存</el-button>
      </template>
    </el-dialog>

    <!-- 一键导入弹窗 -->
    <el-dialog v-model="importDlg.show" :title="`一键导入 · ${importDlg.platform}（本地 agent 接入）`" width="760px">
      <div v-loading="importDlg.loading" style="min-height: 100px">
        <el-alert v-if="importDlg.err" type="error" :title="importDlg.err" :closable="false" />
        <template v-else-if="importDlg.data">
          <div class="af-muted" style="margin: 4px 0 6px">DSH MCP（streamable-http）配置：</div>
          <div style="position: relative">
            <el-button
              size="small"
              style="position: absolute; right: 8px; top: 8px; z-index: 2"
              @click="copyText(JSON.stringify(importDlg.data.mcp_streamable_http || {}, null, 2), 'MCP JSON 已复制')"
            >
              复制
            </el-button>
            <pre class="af-mono" style="background: #f5f7fa; border: 1px solid #e4e7ed; border-radius: 6px; padding: 12px; font-size: 12px; max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all">{{ JSON.stringify(importDlg.data.mcp_streamable_http || {}, null, 2) }}</pre>
          </div>

          <template v-if="(importDlg.data.env_vars || []).length">
            <div class="af-muted" style="margin: 12px 0 6px">环境变量：</div>
            <div>
              <el-tag v-for="ev in importDlg.data.env_vars" :key="ev" size="small" effect="plain" style="margin: 2px 6px 2px 0" class="af-mono">{{ ev }}</el-tag>
            </div>
          </template>

          <template v-if="importDlg.data.ingest_endpoint">
            <div class="af-muted" style="margin: 12px 0 6px">私信接入端点（桥接器 webhook）：</div>
            <div style="display: flex; gap: 8px; align-items: center">
              <code class="af-mono" style="flex: 1; background: #f5f7fa; border: 1px solid #e4e7ed; border-radius: 4px; padding: 6px 8px; word-break: break-all">{{ importDlg.data.ingest_endpoint }}</code>
              <el-button size="small" @click="copyText(importDlg.data.ingest_endpoint, '接入端点已复制')">复制</el-button>
            </div>
          </template>

          <template v-if="importDlg.data.bridge_script_hint">
            <div class="af-muted" style="margin: 12px 0 6px">本地桥脚本说明：</div>
            <pre class="af-mono" style="background: #f5f7fa; border: 1px solid #e4e7ed; border-radius: 6px; padding: 12px; font-size: 12px; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-all">{{ importDlg.data.bridge_script_hint }}</pre>
          </template>
        </template>
        <el-empty v-else-if="!importDlg.loading && !importDlg.err" description="暂无导入信息" />
      </div>
      <template #footer>
        <el-button @click="importDlg.show = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>
