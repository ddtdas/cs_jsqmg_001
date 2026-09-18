<script setup>
// 伪装账号 · 反钓鱼伪装档案页（反钓鱼伪装层 Cover Identity）
// 需求（设计文档 v1 §一/§五）：用户配置多层伪装身份（像特工档案），主动暴露假联系信息诱导骗子接触，
//   通过接触记录锁定骗子范围。本页 = 伪装档案（嵌套树表）+ 新建伪装 + 主动暴露 + 登记接触 + 
//   接触记录表（脱敏/层/锁定态）+ 伪装嵌套图谱。
// 契约：GET/POST /cover/identities、POST /{id}/toggle、DELETE /{id}（confirm+reason）、
//   POST /{id}/expose（返回脱敏假信息）、POST /cover/contact、GET /cover/contacts、GET /cover/map。
// 防御式：全请求 try/catch；加载/错误/空态齐全；数组与 {items,total} 双形态兼容；
//   未知枚举原样展示；假信息展示兜底脱敏（含 * 视为已脱敏不再重复处理）。
// 合规（设计 §六）：假信息仅用于防御验证，不冒充真实个人实施诈骗；暴露/接触由用户手动触发（HITL）；
//   真实骗子号码脱敏展示；敏感操作 confirm+reason 写 gate_logs 审计。
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'

// ---------- 元信息 ----------
// persona_type / expose_strategy 对齐设计文档中文值；后端若返回其他枚举则原样兜底显示
const PERSONA_OPTIONS = [
  { value: '受害者A', label: '受害者A' },
  { value: '转介绍B', label: '转介绍B' },
  { value: '朋友C', label: '朋友C' },
]
const PERSONA_META = {
  受害者A: { label: '受害者A', type: 'danger' },
  转介绍B: { label: '转介绍B', type: 'warning' },
  朋友C: { label: '朋友C', type: 'info' },
  victim_a: { label: '受害者A', type: 'danger' },
  referral_b: { label: '转介绍B', type: 'warning' },
  friend_c: { label: '朋友C', type: 'info' },
}
function personaMeta(v) {
  return PERSONA_META[v] || { label: v || '未知', type: 'info' }
}

const EXPOSE_OPTIONS = [
  { value: '主动', label: '主动暴露（对话中主动抛出假手机号）' },
  { value: '被动', label: '被动暴露（诱导骗子索要后再给）' },
  { value: '埋饵', label: '埋饵（假信息埋入蜜饵文案/演示）' },
]
const EXPOSE_META = {
  主动: { label: '主动暴露', type: 'danger' },
  被动: { label: '被动暴露', type: 'warning' },
  埋饵: { label: '埋饵', type: 'info' },
  active: { label: '主动暴露', type: 'danger' },
  passive: { label: '被动暴露', type: 'warning' },
  bait: { label: '埋饵', type: 'info' },
}
function exposeMeta(v) {
  return EXPOSE_META[v] || { label: v || '—', type: 'info' }
}

// 接触类型（contact_type：phone_call/wechat_add/qq_add/email）
const CONTACT_TYPES = [
  { value: 'phone_call', label: '📞 电话来电', type: 'warning' },
  { value: 'wechat_add', label: '💬 微信添加', type: 'success' },
  { value: 'qq_add', label: '👥 QQ 添加', type: 'primary' },
  { value: 'email', label: '✉️ 邮件', type: 'info' },
]
function contactMeta(v) {
  return CONTACT_TYPES.find((c) => c.value === v) || { label: v || '未知', type: 'info' }
}

function layerTagType(layer) {
  const n = Number(layer) || 1
  if (n >= 3) return 'danger'
  if (n === 2) return 'warning'
  return 'info'
}

function fmtTime(t) {
  if (!t) return '—'
  const s = String(t).replace('T', ' ').replace('Z', '')
  return s.length > 19 ? s.slice(0, 19) : s
}

// 假信息兜底脱敏：含 * 视为后端已脱敏直接展示；否则按类型掩码（展示用，不下发）
function safeMask(v, kind) {
  const s = String(v == null ? '' : v).trim()
  if (!s) return '—'
  if (s.includes('*')) return s
  if (kind === 'phone') return s.length > 7 ? `${s.slice(0, 3)}****${s.slice(-4)}` : `${s.slice(0, 1)}****`
  if (kind === 'email') {
    const i = s.indexOf('@')
    if (i > 1) return `${s.slice(0, 2)}***${s.slice(i)}`
  }
  return s.length > 6 ? `${s.slice(0, 3)}****${s.slice(-2)}` : `${s.slice(0, 2)}***`
}

// 通用响应解包：兼容数组 / {items} / {identities|contacts|nodes|layers} 形态
function unwrapList(res, key) {
  const d = res?.data ?? res ?? {}
  if (Array.isArray(d)) return d
  if (d && Array.isArray(d.items)) return d.items
  if (d && Array.isArray(d[key])) return d[key]
  return []
}

function toNum(v, fallback = 0) {
  const n = Number(v)
  return Number.isFinite(n) ? n : fallback
}

// ---------- 状态 ----------
const activeTab = ref('identities')
const loading = ref(false)
const error = ref('')
const contactsLoading = ref(false)
const contactsError = ref('')
const mapLoading = ref(false)
const mapError = ref('')

const identities = ref([]) // 扁平列表（后端返回）
const treeRows = ref([]) // 树表数据
const contacts = ref([])
const mapNodes = ref([])

const identityById = computed(() => {
  const m = new Map()
  for (const it of identities.value) m.set(String(it.id), it)
  return m
})

// 接触次数汇总：优先后端 contact_count，否则用已加载接触记录统计
const contactCountMap = computed(() => {
  const m = new Map()
  for (const c of contacts.value) {
    const k = String(c.identity_id)
    m.set(k, (m.get(k) || 0) + 1)
  }
  return m
})
function contactCountOf(row) {
  if (toNum(row.contact_count, -1) >= 0) return toNum(row.contact_count)
  return contactCountMap.value.get(String(row.id)) || 0
}

// 扁平 → 树（parent_id 缩进，像特工档案多层身份；自引用/环引用防御）
function buildTree(flat) {
  const nodes = (flat || []).map((x) => ({ ...x, children: Array.isArray(x.children) ? x.children : [] }))
  const byId = new Map()
  for (const n of nodes) byId.set(String(n.id), n)
  const roots = []
  const seen = new Set()
  for (const n of nodes) {
    if (seen.has(n.id)) {
      roots.push(n)
      continue
    }
    const pid = n.parent_id
    const p = pid != null && String(pid) !== String(n.id) ? byId.get(String(pid)) : null
    if (p) p.children.push(n)
    else roots.push(n)
    seen.add(n.id)
  }
  return roots
}

async function loadIdentities() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.coverIdentities()
    identities.value = unwrapList(res, 'identities')
    treeRows.value = buildTree(identities.value)
  } catch (e) {
    error.value = e.message
    identities.value = []
    treeRows.value = []
  } finally {
    loading.value = false
  }
}

// ---------- 统计条 ----------
const stats = computed(() => {
  const list = identities.value
  const enabled = list.filter((x) => x.enabled !== false && x.enabled !== 0).length
  const maxLayer = list.reduce((mx, x) => Math.max(mx, toNum(x.layer, 1)), 0)
  return { total: list.length, enabled, maxLayer, contacts: contacts.value.length }
})

// ---------- 新建伪装弹窗 ----------
const createDialog = reactive({ show: false, busy: false })
const createForm = reactive({
  name: '',
  persona_type: '受害者A',
  parent_id: null,
  fake_phone: '',
  fake_wechat: '',
  fake_qq: '',
  expose_strategy: '主动',
})
const layerChangedMsg = ref('')

// layer 自动：选 parent 则 +1，无 parent = L1
const computedLayer = computed(() => {
  if (createForm.parent_id == null || createForm.parent_id === '') return 1
  const p = identityById.value.get(String(createForm.parent_id))
  return p ? (toNum(p.layer, 1) + 1) : 2
})

function resetCreateForm() {
  createForm.name = ''
  createForm.persona_type = '受害者A'
  createForm.parent_id = null
  createForm.fake_phone = ''
  createForm.fake_wechat = ''
  createForm.fake_qq = ''
  createForm.expose_strategy = '主动'
  layerChangedMsg.value = ''
}

function openCreate() {
  resetCreateForm()
  createDialog.show = true
}

function onParentChange() {
  layerChangedMsg.value = createForm.parent_id
    ? `已选上层「${identityById.value.get(String(createForm.parent_id))?.name || ''}」→ 自动为第 ${computedLayer.value} 层`
    : ''
}

async function submitCreate() {
  const name = (createForm.name || '').trim()
  if (!name) {
    ElMessage.warning('请填写伪装名')
    return
  }
  if (!createForm.fake_phone.trim() && !createForm.fake_wechat.trim() && !createForm.fake_qq.trim()) {
    ElMessage.warning('请至少填写一个假联系信息（手机号/微信/QQ），便于暴露给骗子')
    return
  }
  createDialog.busy = true
  try {
    // 契约（A队）：layer 由后端自动计算（选 parent_id 则 +1，不传 = 最外层 L1），请求体无需 layer 字段
    const payload = {
      name,
      persona_type: createForm.persona_type,
      expose_strategy: createForm.expose_strategy,
    }
    if (createForm.parent_id != null && createForm.parent_id !== '') payload.parent_id = createForm.parent_id
    if (createForm.fake_phone.trim()) payload.fake_phone = createForm.fake_phone.trim()
    if (createForm.fake_wechat.trim()) payload.fake_wechat = createForm.fake_wechat.trim()
    if (createForm.fake_qq.trim()) payload.fake_qq = createForm.fake_qq.trim()
    await api.coverCreate(payload)
    ElMessage.success(`伪装「${name}」已创建（L${computedLayer.value}）`)
    createDialog.show = false
    await loadIdentities()
    loadMap()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    createDialog.busy = false
  }
}

// ---------- 启用开关（toggle：contract 需 {confirm:true, reason≥20字} 审计） ----------
const toggleDialog = reactive({ show: false, busy: false, identity: null, targetEnabled: false, reason: '' })
const togglingId = ref(null)

function openToggle(row, val) {
  toggleDialog.identity = row
  toggleDialog.targetEnabled = !!val
  toggleDialog.reason = ''
  toggleDialog.show = true
}

async function submitToggle() {
  const reason = (toggleDialog.reason || '').trim()
  if (reason.length < 20) {
    ElMessage.warning(`启停属敏感操作，请填写原因（至少 20 字，当前 ${reason.length} 字，GateLog 审计）`)
    return
  }
  toggleDialog.busy = true
  togglingId.value = toggleDialog.identity?.id
  try {
    await api.coverToggle(toggleDialog.identity.id, { confirm: true, reason })
    ElMessage.success(
      `伪装「${toggleDialog.identity.name}」已${toggleDialog.targetEnabled ? '启用' : '停用'}`
    )
    toggleDialog.show = false
    await loadIdentities()
  } catch (e) {
    ElMessage.error(e.message)
    await loadIdentities() // 失败回滚开关状态
  } finally {
    toggleDialog.busy = false
    togglingId.value = null
  }
}

// ---------- 主动暴露（expose → 脱敏假信息可复制，提示发给骗子） ----------
const exposeDialog = reactive({ show: false, busy: false, identity: null, items: [], tip: '' })

async function openExpose(row) {
  exposeDialog.identity = row
  exposeDialog.items = []
  exposeDialog.tip = '正在获取脱敏假信息…'
  exposeDialog.show = true
  exposeDialog.busy = true
  try {
    const res = await api.coverExpose(row.id)
    const d = res?.data ?? res ?? {}
    exposeDialog.items = normalizeExposed(d, row)
    exposeDialog.tip = exposeDialog.items.length
      ? '以下为脱敏后的假联系信息（脱敏展示 / 复制时下发原文）：复制发送给骗子即可完成一次主动暴露（HITL，由你手动触发）。'
      : '后端未返回可暴露的假信息（该伪装可能未配置假联系方式）。'
  } catch (e) {
    exposeDialog.tip = ''
    exposeDialog.items = []
    ElMessage.error(e.message)
  } finally {
    exposeDialog.busy = false
  }
}

// 归一化 expose 返回：契约主形态 data.exposure.checklist[{field,label,value,masked}]；
// 兼容 {masked:{fake_phone}} / 顶级 fake_* / {exposures:[{type,value,masked_value}]} / 身份行兜底。
// 每项 {label, display(脱敏展示), copy(原文供复制"泄露"给骗子)}
const EXPOSE_FIELD_LABELS = {
  fake_phone: '手机号', fake_wechat: '微信号', fake_qq: 'QQ号', fake_email: '邮箱',
  phone: '手机号', wechat: '微信号', qq: 'QQ号', email: '邮箱',
  phone_call: '手机号', wechat_add: '微信号', qq_add: 'QQ号',
}
function exposeFieldLabel(ex) {
  return ex.label || EXPOSE_FIELD_LABELS[ex.field] || EXPOSE_FIELD_LABELS[ex.contact_type || ex.type] || ex.field || '暴露信息'
}
function normalizeExposed(d, row) {
  const items = []
  const push = (label, display, copy) => {
    const s = String(display == null ? '' : display).trim()
    const c = copy === undefined ? s : String(copy == null ? '' : copy)
    if (s && s !== '—') items.push({ label, display: s, copy: c || s })
  }
  // 主形态：data.exposure.checklist
  const exposure = d.exposure || d
  const checklist = Array.isArray(exposure.checklist) ? exposure.checklist : []
  for (const ex of checklist) {
    push(exposeFieldLabel(ex), ex.masked ?? safeMask(ex.value, ex.field === 'fake_phone' ? 'phone' : 'text'), ex.value)
  }
  // 兼容形态
  const masked = exposure.masked || exposure.mask || exposure.redacted || {}
  if (typeof masked === 'object' && !Array.isArray(masked)) {
    push('假手机号', masked.fake_phone ?? masked.phone, masked.fake_phone ?? masked.phone)
    push('假微信', masked.fake_wechat ?? masked.wechat, masked.fake_wechat ?? masked.wechat)
    push('假QQ', masked.fake_qq ?? masked.qq, masked.fake_qq ?? masked.qq)
    push('假邮箱', masked.fake_email ?? masked.email, masked.fake_email ?? masked.email)
  }
  push('假手机号', safeMask(exposure.fake_phone, 'phone'), exposure.fake_phone)
  push('假微信', safeMask(exposure.fake_wechat, 'text'), exposure.fake_wechat)
  push('假QQ', safeMask(exposure.fake_qq, 'text'), exposure.fake_qq)
  push('假邮箱', safeMask(exposure.fake_email, 'email'), exposure.fake_email)
  const exps = Array.isArray(exposure.exposures) ? exposure.exposures : Array.isArray(exposure.items) ? exposure.items : []
  for (const ex of exps) {
    const label = exposeFieldLabel(ex)
    const raw = ex.value
    push(label, ex.masked_value ?? ex.mask ?? safeMask(raw, 'text'), raw)
  }
  // 兜底：身份行自身假字段
  if (!items.length) {
    push('假手机号', safeMask(row.fake_phone, 'phone'), row.fake_phone)
    push('假微信', safeMask(row.fake_wechat, 'text'), row.fake_wechat)
    push('假QQ', safeMask(row.fake_qq, 'text'), row.fake_qq)
    push('假邮箱', safeMask(row.fake_email, 'email'), row.fake_email)
  }
  return items
}

async function copyExposed(item) {
  try {
    await navigator.clipboard.writeText(item.copy)
    ElMessage.success(`已复制：${item.copy}（假信息仅用于泄露给骗子防御验证）`)
  } catch {
    ElMessage.error('复制失败，请手动选择复制')
  }
}

// ---------- 登记接触（coverContact） ----------
const contactDialog = reactive({ show: false, busy: false })
const contactForm = reactive({
  identity_id: null,
  contact_type: 'phone_call',
  contact_value: '',
  trap_id: null,
  det_id: null,
})
const traps = ref([])
const dets = ref([])

async function loadOptions() {
  // 蜜饵来源（trap_id）与检测来源（det_id）下拉：失败静默降级（防御式，不阻塞登记）
  try {
    const t = await api.listTraps()
    traps.value = Array.isArray(t) ? t : []
  } catch {
    traps.value = []
  }
  try {
    const h = await api.scanHits(30)
    dets.value = Array.isArray(h) ? h : []
  } catch {
    dets.value = []
  }
}

function openContact(row) {
  contactForm.identity_id = row ? row.id : null
  contactForm.contact_type = 'phone_call'
  contactForm.contact_value = ''
  contactForm.trap_id = null
  contactForm.det_id = null
  contactDialog.show = true
  loadOptions()
}

async function submitContact() {
  if (contactForm.identity_id == null) {
    ElMessage.warning('请选择被接触的伪装账号')
    return
  }
  if (!(contactForm.contact_value || '').trim()) {
    ElMessage.warning('请填写骗子号码/账号（contact_value）')
    return
  }
  contactDialog.busy = true
  try {
    const payload = {
      identity_id: contactForm.identity_id,
      contact_type: contactForm.contact_type,
      contact_value: contactForm.contact_value.trim(),
    }
    if (contactForm.trap_id != null && contactForm.trap_id !== '') payload.trap_id = contactForm.trap_id
    if (contactForm.det_id != null && contactForm.det_id !== '') payload.det_id = contactForm.det_id
    const r = await api.coverContact(payload)
    // 契约（A队）：data.event={event_id, contact_value_mask, ioc_count, escalated, trap_link}
    const ev = r?.event ?? r ?? {}
    const bits = []
    if (ev.ioc_count != null) bits.push(`提取 IOC ${ev.ioc_count} 条`)
    if (ev.escalated) bits.push('已升级锁定范围')
    if (ev.event_id != null) bits.push(`事件#${ev.event_id}`)
    ElMessage.success(`接触已登记${bits.length ? '：' + bits.join('，') : ''}`)
    contactDialog.show = false
    await Promise.all([loadContacts(true), loadIdentities()])
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    contactDialog.busy = false
  }
}

// ---------- 删除（confirm+reason ≥20 审计） ----------
const deleteDialog = reactive({ show: false, busy: false, identity: null, reason: '' })

function openDelete(row) {
  deleteDialog.identity = row
  deleteDialog.reason = ''
  deleteDialog.show = true
}

async function submitDelete() {
  const reason = (deleteDialog.reason || '').trim()
  if (reason.length < 20) {
    ElMessage.warning(`删除属敏感操作，请填写原因（至少 20 字，当前 ${reason.length} 字，GateLog 审计）`)
    return
  }
  deleteDialog.busy = true
  try {
    await api.coverDelete(deleteDialog.identity.id, { confirm: true, reason })
    ElMessage.success(`伪装「${deleteDialog.identity.name}」已删除`)
    deleteDialog.show = false
    await loadIdentities()
    loadMap()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    deleteDialog.busy = false
  }
}

// ---------- 接触记录（GET /cover/contacts?identity_id&contact_type&escalated&limit） ----------
const contactFilters = reactive({ identity_id: null, contact_type: '', escalated: '' })

function contactQuery() {
  const qs = new URLSearchParams()
  if (contactFilters.identity_id != null && contactFilters.identity_id !== '') qs.set('identity_id', String(contactFilters.identity_id))
  if (contactFilters.contact_type) qs.set('contact_type', contactFilters.contact_type)
  if (contactFilters.escalated === 'true') qs.set('escalated', 'true')
  else if (contactFilters.escalated === 'false') qs.set('escalated', 'false')
  qs.set('limit', '200')
  const s = qs.toString()
  return s ? '?' + s : ''
}

// 契约（A队）：锁定字段为 escalated（int 0/1）；兼容历史 locked/status 形态
function isLocked(c) {
  return c.locked === true || c.locked === 1 || c.locked === '1' || c.escalated === true || c.escalated === 1 || c.escalated === '1' || c.status === 'locked'
}

async function loadContacts(silent = false) {
  if (!silent) contactsLoading.value = true
  contactsError.value = ''
  try {
    const res = await api.coverContacts(contactQuery())
    contacts.value = unwrapList(res, 'contacts')
  } catch (e) {
    contactsError.value = e.message
    contacts.value = []
  } finally {
    if (!silent) contactsLoading.value = false
  }
}

function sourceText(c) {
  const parts = []
  if (c.trap_id != null) parts.push(`蜜饵#${c.trap_id}`)
  if (c.det_id != null) parts.push(`检测#${c.det_id}`)
  return parts.join(' · ') || '—'
}

// ---------- 嵌套图谱（GET /cover/map → data.tree 嵌套树） ----------
// 契约（A队）：data.tree 为嵌套树（节点含 children）；兼容数组 / {nodes} / {identities} / {layers} 形态。
// 统一拍平为扁平节点，并携带 __depth / __parentName（嵌套深度优先，parent_id 兜底）。
function flattenTree(nodes, parent = null, depth = 1, out = []) {
  for (const n of nodes || []) {
    const kids = Array.isArray(n.children) ? n.children : []
    const row = { ...n }
    delete row.children
    row.__depth = depth
    row.__parentName = parent ? `${parent.name}` : ''
    out.push(row)
    if (kids.length) flattenTree(kids, { name: n.name, id: n.id }, depth + 1, out)
  }
  return out
}

function normalizeMap(res) {
  const d = res?.data ?? res ?? {}
  let raw = []
  if (Array.isArray(d)) raw = d
  else if (Array.isArray(d.tree)) raw = flattenTree(d.tree)
  else if (Array.isArray(d.nodes)) raw = d.nodes
  else if (Array.isArray(d.identities)) raw = d.identities
  else if (Array.isArray(d.layers)) raw = d.layers
  // 去重（防止 map 同时被 identity 列表重复返回）
  const seen = new Set()
  const uniq = []
  for (const n of raw) {
    const k = String(n.id ?? n.identity_id ?? Math.random())
    if (seen.has(k)) continue
    seen.add(k)
    uniq.push({ ...n, id: n.id ?? n.identity_id })
  }
  return uniq
}

async function loadMap(silent = false) {
  if (!silent) mapLoading.value = true
  mapError.value = ''
  try {
    const res = await api.coverMap()
    mapNodes.value = normalizeMap(res)
  } catch (e) {
    mapError.value = e.message
    mapNodes.value = []
  } finally {
    if (!silent) mapLoading.value = false
  }
}

// 图谱渲染：扁平列表按 layer/深度排序 + 缩进（简单树，任意层数不漏显）
function mapDepthOf(node, depth = 1, trail = new Set()) {
  const pid = node.parent_id
  if (pid == null || String(pid) === String(node.id) || trail.has(String(node.id))) return depth
  const p = mapNodes.value.find((n) => String(n.id) === String(pid))
  if (!p) return depth
  const t = new Set(trail)
  t.add(String(node.id))
  return mapDepthOf(p, depth + 1, t)
}

const mapRows = computed(() => {
  const rows = mapNodes.value.map((n) => ({
    ...n,
    // 深度优先顺序：tree 拍平深度(__depth) → layer 字段 → parent_id 链推算
    depth: Math.max(1, toNum(n.__depth, 0) || toNum(n.layer, 0) || (n.parent_id != null ? mapDepthOf(n) : 1)),
    parentName: n.__parentName || (() => {
      const pid = n.parent_id
      if (pid == null || String(pid) === String(n.id)) return ''
      const p = mapNodes.value.find((x) => String(x.id) === String(pid))
      return p ? `L${toNum(p.layer, 1)} ${p.name}` : `#${pid}`
    })(),
  }))
  rows.sort((a, b) => (toNum(a.depth) - toNum(b.depth)) || (String(a.id) < String(b.id) ? -1 : 1))
  return rows
})

function reloadAll() {
  loadIdentities()
  loadContacts()
  loadMap()
}

onMounted(() => {
  loadIdentities()
  loadContacts()
  loadMap()
})
</script>

<template>
  <div class="af-page">
    <!-- 顶部说明 + 合规声明 -->
    <el-card shadow="never" class="af-card">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap">
        <div style="min-width: 0">
          <b>伪装账号 · 反钓鱼多层身份档案</b>
          <div class="af-muted" style="margin-top: 6px; max-width: 900px; line-height: 1.8">
            创建<b>多层伪装身份</b>（像特工档案：受害者A → 被骗后转介绍的B → B 的朋友C），配置假联系信息
            （虚拟手机号/微信/QQ）用于<b>主动暴露给骗子</b>；骗子来电/添加即登记为<b>接触事件</b>，提取 IOC 并
            锁定其号码范围。<b>合规声明</b>：假信息仅用于防御验证（诱导骗子自证），<b>不冒充真实个人实施诈骗</b>；
            暴露与回复均由你手动触发（HITL，系统不自动打电话/发消息）；真实骗子号码脱敏存储与展示，全程可审计。
          </div>
        </div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap">
          <el-button size="small" type="primary" @click="openCreate">
            <el-icon style="margin-right: 4px"><component :is="'Plus'" /></el-icon>新建伪装
          </el-button>
          <el-button size="small" @click="reloadAll">刷新</el-button>
        </div>
      </div>
      <el-alert
        v-if="error"
        type="error"
        :title="error"
        :closable="false"
        style="margin-top: 12px"
        show-icon
      >
        <template #default>
          <div>接口可能尚未就绪（后端 /cover 部署中）——请确认主控已包含 cover 模块后重试。</div>
          <el-button size="small" style="margin-top: 8px" @click="loadIdentities">重试</el-button>
        </template>
      </el-alert>
    </el-card>

    <el-tabs v-model="activeTab">
      <!-- ============ Tab1 伪装档案 ============ -->
      <el-tab-pane label="伪装档案" name="identities">
        <!-- 统计条 -->
        <el-row :gutter="12" style="margin-bottom: 16px">
          <el-col :span="6"><el-card shadow="never" class="af-card" style="margin-bottom: 0"><div class="af-muted">伪装总数</div><div style="font-size: 22px; font-weight: 700">{{ stats.total }}</div></el-card></el-col>
          <el-col :span="6"><el-card shadow="never" class="af-card" style="margin-bottom: 0"><div class="af-muted">启用中</div><div style="font-size: 22px; font-weight: 700; color: var(--cg-success)">{{ stats.enabled }}</div></el-card></el-col>
          <el-col :span="6"><el-card shadow="never" class="af-card" style="margin-bottom: 0"><div class="af-muted">最大伪装层</div><div style="font-size: 22px; font-weight: 700; color: var(--cg-warning)">L{{ stats.maxLayer }}</div></el-card></el-col>
          <el-col :span="6"><el-card shadow="never" class="af-card" style="margin-bottom: 0"><div class="af-muted">接触总数</div><div style="font-size: 22px; font-weight: 700; color: var(--cg-danger)">{{ stats.contacts }}</div></el-card></el-col>
        </el-row>

        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>伪装名单（嵌套档案，按 parent_id 缩进）</b>
              <span class="af-muted">（/cover/identities）</span>
            </div>
          </template>
          <el-table
            :data="treeRows"
            v-loading="loading"
            size="small"
            row-key="id"
            :tree-props="{ children: 'children' }"
            default-expand-all
            empty-text="暂无伪装账号 —— 点击右上角「新建伪装」开始建档"
          >
            <el-table-column label="伪装档案（层级）" min-width="220">
              <template #default="{ row }">
                <div style="display: flex; align-items: center; gap: 8px">
                  <el-tag :type="layerTagType(row.layer)" size="small" effect="dark" style="min-width: 34px; text-align: center">L{{ row.layer || 1 }}</el-tag>
                  <b>{{ row.name }}</b>
                  <el-tag :type="personaMeta(row.persona_type).type" size="small" effect="plain">{{ personaMeta(row.persona_type).label }}</el-tag>
                  <el-tag :type="exposeMeta(row.expose_strategy).type" size="small" effect="plain">{{ exposeMeta(row.expose_strategy).label }}</el-tag>
                </div>
                <div class="af-muted" style="margin-top: 2px">
                  {{ row.bio_desc || row.avatar_desc || '—' }}
                </div>
              </template>
            </el-table-column>
            <el-table-column label="假联系信息（脱敏）" min-width="180">
              <template #default="{ row }">
                <div class="af-mono" style="display: flex; flex-direction: column; gap: 2px; font-size: 12px">
                  <span>📱 {{ safeMask(row.fake_phone, 'phone') }}</span>
                  <span>💬 {{ safeMask(row.fake_wechat, 'text') }}</span>
                  <span>👥 {{ safeMask(row.fake_qq, 'text') }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="被接触" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="contactCountOf(row) > 0 ? 'danger' : 'info'" size="small" effect="plain">
                  {{ contactCountOf(row) }} 次
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="启用" width="90" align="center">
              <template #default="{ row }">
                <el-switch
                  :model-value="row.enabled !== false && row.enabled !== 0"
                  :loading="togglingId === row.id"
                  @change="(v) => openToggle(row, v)"
                />
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <!-- P2-5 修复：操作列由 min-width:280 改为固定 230px（三按钮紧凑排布），
                 1280px 下列宽合计约 960px ≤ 表宽 970px，不再越界 -->
            <el-table-column label="操作" width="230">
              <template #default="{ row }">
                <el-button size="small" type="danger" plain @click="openExpose(row)">主动暴露</el-button>
                <el-button size="small" type="primary" plain @click="openContact(row)">登记接触</el-button>
                <el-button size="small" type="danger" text @click="openDelete(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="af-muted mt8">提示：嵌套关系由「上层伪装 parent_id」决定，新建时选择上层（如受害者A）会自动生成下一层（转介绍B = L2、朋友C = L3）。</div>
        </el-card>
      </el-tab-pane>

      <!-- ============ Tab2 接触记录 ============ -->
      <el-tab-pane label="接触记录" name="contacts">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px">
              <b>反钓鱼接触记录（骗子号码脱敏）</b>
              <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
                <el-select v-model="contactFilters.identity_id" placeholder="按伪装筛选" clearable size="small" style="width: 160px">
                  <el-option v-for="it in identities" :key="String(it.id)" :label="`L${it.layer || 1} ${it.name}`" :value="it.id" />
                </el-select>
                <el-select v-model="contactFilters.contact_type" placeholder="接触类型" clearable size="small" style="width: 120px">
                  <el-option v-for="c in CONTACT_TYPES" :key="c.value" :label="c.label.replace(/^[^ ]+ /, '')" :value="c.value" />
                </el-select>
                <el-select v-model="contactFilters.escalated" placeholder="锁定状态" clearable size="small" style="width: 120px">
                  <el-option label="已锁定" value="true" />
                  <el-option label="未锁定" value="false" />
                </el-select>
                <el-button size="small" type="primary" plain @click="loadContacts(false)">查询</el-button>
                <el-button size="small" @click="openContact(null)">登记接触</el-button>
              </div>
            </div>
          </template>
          <el-alert v-if="contactsError" type="error" :title="contactsError" :closable="false" style="margin-bottom: 12px" />
          <el-table :data="contacts" v-loading="contactsLoading" size="small" empty-text="暂无接触记录——骗子尚未接触伪装，或还没有登记">
            <el-table-column label="伪装身份" min-width="150">
              <template #default="{ row }">
                <template v-if="identityById.get(String(row.identity_id))">
                  <el-tag :type="layerTagType(identityById.get(String(row.identity_id)).layer)" size="small" effect="dark" style="margin-right: 6px">
                    L{{ identityById.get(String(row.identity_id)).layer || 1 }}
                  </el-tag>
                  {{ identityById.get(String(row.identity_id)).name }}
                </template>
                <template v-else-if="row.identity_name">
                  <el-tag :type="layerTagType(row.identity_layer)" size="small" effect="dark" style="margin-right: 6px">L{{ row.identity_layer || 1 }}</el-tag>
                  {{ row.identity_name }}
                </template>
                <span v-else class="af-muted">伪装 #{{ row.identity_id }}</span>
              </template>
            </el-table-column>
            <el-table-column label="接触类型" width="110">
              <template #default="{ row }">
                <el-tag :type="contactMeta(row.contact_type).type" size="small">{{ contactMeta(row.contact_type).label }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="骗子号码/账号（脱敏）" min-width="180">
              <template #default="{ row }">
                <span class="af-mono">{{ row.contact_value_mask || row.contact_value || '—' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="接触层" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="layerTagType(row.layer_at)" size="small" effect="plain">L{{ row.layer_at || 1 }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="来源" min-width="120">
              <template #default="{ row }">{{ sourceText(row) }}</template>
            </el-table-column>
            <el-table-column label="锁定状态" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="isLocked(row) ? 'danger' : 'info'" size="small">
                  {{ isLocked(row) ? '🔒 已锁定' : '未锁定' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
          </el-table>
          <div class="af-muted mt8">后端子自动提取 contact_value 为 IOC、关联供应链图谱并做锁定范围分析（escalated/locked 由后端判定）。</div>
        </el-card>
      </el-tab-pane>

      <!-- ============ Tab3 嵌套图谱 ============ -->
      <el-tab-pane label="嵌套图谱" name="map">
        <el-card shadow="never" class="af-card">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <b>伪装嵌套图谱（层与层关系 + 每层被接触情况）</b>
              <span class="af-muted">（/cover/map）</span>
            </div>
          </template>
          <el-alert v-if="mapError" type="error" :title="mapError" :closable="false" style="margin-bottom: 12px" />
          <div v-loading="mapLoading" style="min-height: 120px">
            <el-empty v-if="!mapLoading && !mapError && !mapNodes.length" description="暂无伪装节点——先新建伪装，图谱将按 parent_id 展示多层链条" />
            <div
              v-for="n in mapRows"
              :key="`map-${n.id}`"
              style="display: flex; align-items: center; gap: 10px; padding: 8px 10px; border: 1px solid #e4e7ed; border-radius: 6px; background: #fff; margin-top: 4px"
              :style="{ marginLeft: `${(toNum(n.depth) - 1) * 28}px` }"
            >
              <el-tag :type="layerTagType(n.depth)" size="small" effect="dark" style="min-width: 34px; text-align: center">L{{ n.depth }}</el-tag>
              <b>{{ n.name }}</b>
              <el-tag :type="personaMeta(n.persona_type).type" size="small" effect="plain">{{ personaMeta(n.persona_type).label }}</el-tag>
              <span class="af-mono af-muted">📱 {{ safeMask(n.fake_phone, 'phone') }}</span>
              <template v-if="n.parentName">
                <span class="af-muted" style="font-size: 12px">↳ 上层：{{ n.parentName }}</span>
              </template>
              <template v-if="n.trap_id != null || n.det_id != null">
                <el-tag size="small" effect="plain" type="warning" v-if="n.trap_id != null">蜜饵#{{ n.trap_id }}</el-tag>
                <el-tag size="small" effect="plain" type="warning" v-if="n.det_id != null">检测#{{ n.det_id }}</el-tag>
              </template>
              <span style="flex: 1" />
              <span class="af-muted">被接触</span>
              <el-tag :type="toNum(n.contact_count, 0) > 0 ? 'danger' : 'info'" size="small" effect="plain">{{ toNum(n.contact_count, 0) }} 次</el-tag>
            </div>
            <div class="af-muted mt8">说明：图谱节点按 parent_id 形成「受害者A → 转介绍B → 朋友C」的多层链条（缩进表示层深）；节点右侧显示该层被骗子接触的次数（越深越危险，越接近骗子核心）。</div>
          </div>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 新建伪装弹窗 ============ -->
    <el-dialog v-model="createDialog.show" title="新建伪装账号（多层特工档案）" width="620px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="伪装名（虚构昵称）" required>
          <el-input v-model="createForm.name" placeholder="如：受害者小美 / 转介绍的老王 / 王总的朋友" maxlength="50" show-word-limit />
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="Persona 类型">
              <el-select v-model="createForm.persona_type" style="width: 100%">
                <el-option v-for="o in PERSONA_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="伪装层（自动计算）">
              <el-select v-model="createForm.parent_id" placeholder="无上层 = 最外层 L1" clearable style="width: 100%" @change="onParentChange">
                <el-option v-for="it in identities" :key="String(it.id)" :label="`L${it.layer || 1} ${it.name}`" :value="it.id" />
              </el-select>
              <div class="af-muted">
                层数：<el-tag size="small" :type="layerTagType(computedLayer)" effect="dark">L{{ computedLayer }}</el-tag>
                <span v-if="layerChangedMsg" style="margin-left: 6px">{{ layerChangedMsg }}</span>
              </div>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="假联系信息（仅防御用，展示时脱敏）" required>
          <el-row :gutter="12" style="width: 100%">
            <el-col :span="8">
              <el-input v-model="createForm.fake_phone" placeholder="假手机号，如 13800008000" maxlength="20" />
              <div class="af-muted" style="margin-top: 2px">展示为 138****8000</div>
            </el-col>
            <el-col :span="8">
              <el-input v-model="createForm.fake_wechat" placeholder="假微信号" maxlength="30" />
              <div class="af-muted" style="margin-top: 2px">用于泄露给骗子添加</div>
            </el-col>
            <el-col :span="8">
              <el-input v-model="createForm.fake_qq" placeholder="假QQ号" maxlength="20" />
              <div class="af-muted" style="margin-top: 2px">用于泄露给骗子添加</div>
            </el-col>
          </el-row>
        </el-form-item>
        <el-form-item label="暴露策略">
          <el-select v-model="createForm.expose_strategy" style="width: 100%">
            <el-option v-for="o in EXPOSE_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="af-muted" style="border-top: 1px dashed #e4e7ed; padding-top: 8px">
        合规：假信息仅用于防御验证（诱导骗子暴露自己），不冒充真实个人实施诈骗；暴露操作由你在「主动暴露」手动触发（HITL）。
      </div>
      <template #footer>
        <el-button @click="createDialog.show = false">取消</el-button>
        <el-button type="primary" :loading="createDialog.busy" @click="submitCreate">创建伪装</el-button>
      </template>
    </el-dialog>

    <!-- ============ 主动暴露弹窗（脱敏假信息可复制） ============ -->
    <el-dialog v-model="exposeDialog.show" :title="`主动暴露 · ${exposeDialog.identity?.name || ''}`" width="560px" :close-on-click-modal="false">
      <div v-loading="exposeDialog.busy" style="min-height: 90px">
        <el-alert v-if="exposeDialog.tip" type="warning" :closable="false" style="margin-bottom: 12px" :title="exposeDialog.tip" />
        <div v-if="exposeDialog.items.length" style="display: flex; flex-direction: column; gap: 8px">
          <div
            v-for="(item, i) in exposeDialog.items"
            :key="i"
            style="display: flex; align-items: center; justify-content: space-between; gap: 10px; border: 1px solid #e4e7ed; border-radius: 6px; padding: 8px 12px; background: #fafbfc"
          >
            <span class="af-muted" style="min-width: 90px">{{ item.label }}</span>
            <code class="af-mono" style="flex: 1; word-break: break-all">{{ item.display }}</code>
            <el-button size="small" @click="copyExposed(item)">复制原文</el-button>
          </div>
          <div class="af-muted">
            复制后<b>发给骗子</b>（在对话/私信中把假手机号"泄露"出去）——骗子来电/添加时在「登记接触」记录其号码，
            系统将提取 IOC 并锁定其身份范围。注意：假信息仅用于防御验证，不对真实个人实施诈骗；请勿将真实号码外泄。
          </div>
        </div>
        <el-empty v-else-if="!exposeDialog.busy" description="无可暴露的假信息（该伪装未配置假联系方式或已全部脱敏）" />
      </div>
      <template #footer>
        <el-button @click="exposeDialog.show = false">关闭</el-button>
        <el-button
          type="primary"
          plain
          v-if="exposeDialog.identity"
          @click="exposeDialog.show = false; openContact(exposeDialog.identity)"
        >
          去登记接触
        </el-button>
      </template>
    </el-dialog>

    <!-- ============ 登记接触弹窗 ============ -->
    <el-dialog v-model="contactDialog.show" title="登记骗子接触（锁定范围）" width="600px" :close-on-click-modal="false">
      <el-form label-position="top">
        <el-form-item label="被接触的伪装账号" required>
          <el-select v-model="contactForm.identity_id" placeholder="选择伪装" style="width: 100%">
            <el-option v-for="it in identities" :key="String(it.id)" :label="`L${it.layer || 1} ${it.name}（${personaMeta(it.persona_type).label}）`" :value="it.id" />
          </el-select>
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="10">
            <el-form-item label="接触类型">
              <el-select v-model="contactForm.contact_type" style="width: 100%">
                <el-option v-for="c in CONTACT_TYPES" :key="c.value" :label="c.label" :value="c.value" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="14">
            <el-form-item label="骗子号码/账号" required>
              <el-input v-model="contactForm.contact_value" placeholder="如 13812345678 / wx_scam_001 / qq_8888" maxlength="100" />
              <div class="af-muted" style="margin-top: 2px">后端提取 IOC 并脱敏存储</div>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="来源蜜饵 trap（可选）">
              <el-select v-model="contactForm.trap_id" placeholder="无" clearable filterable style="width: 100%">
                <el-option v-for="t in traps" :key="String(t.id)" :label="`#${t.id} ${(t.bait_text || '').slice(0, 24)}`" :value="t.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="来源检测 det（可选）">
              <el-select v-model="contactForm.det_id" placeholder="无" clearable filterable style="width: 100%">
                <el-option v-for="h in dets" :key="String(h.id)" :label="`#${h.id} ${h.verdict || h.source || '检测'}`" :value="h.id" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <div class="af-muted" style="border-top: 1px dashed #e4e7ed; padding-top: 8px">
        提交后：contact_value 自动入 IOC 提取链路（SocLibService）+ 反哺供应链图谱；同一骗子号码多次接触将升级锁定。
      </div>
      <template #footer>
        <el-button @click="contactDialog.show = false">取消</el-button>
        <el-button type="primary" :loading="contactDialog.busy" @click="submitContact">登记接触</el-button>
      </template>
    </el-dialog>

    <!-- ============ 删除弹窗（confirm+reason≥20 审计） ============ -->
    <el-dialog v-model="deleteDialog.show" :title="`删除伪装 · ${deleteDialog.identity?.name || ''}`" width="520px" :close-on-click-modal="false">
      <el-alert type="error" :closable="false" style="margin-bottom: 12px" title="敏感操作：删除后不可恢复，需填写原因写入审计日志（gate_logs）。" />
      <el-form label-position="top">
        <el-form-item label="删除原因（至少 20 字）" required>
          <el-input v-model="deleteDialog.reason" type="textarea" :rows="4" placeholder="如：该伪装身份已失效，停止使用（接触审计记录保留（设计），不级联删除）…" maxlength="200" show-word-limit />
          <div class="af-muted" style="margin-top: 2px">
            当前 {{ (deleteDialog.reason || '').trim().length }} 字（{{ (deleteDialog.reason || '').trim().length >= 20 ? '已满足' : '还需 ' + (20 - (deleteDialog.reason || '').trim().length) + ' 字' }}）
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deleteDialog.show = false">取消</el-button>
        <el-button type="danger" :loading="deleteDialog.busy" @click="submitDelete">确认删除</el-button>
      </template>
    </el-dialog>

    <!-- ============ 启停弹窗（toggle：contract 需 {confirm:true, reason≥20字} 审计） ============ -->
    <el-dialog v-model="toggleDialog.show" :title="`${toggleDialog.targetEnabled ? '启用' : '停用'}伪装 · ${toggleDialog.identity?.name || ''}`" width="520px" :close-on-click-modal="false">
      <el-alert
        type="warning"
        :closable="false"
        style="margin-bottom: 12px"
        :title="`敏感操作：${toggleDialog.targetEnabled ? '启用' : '停用'}后将影响该伪装是否参与暴露/接触，需填写原因写入审计日志（gate_logs）。`"
      />
      <el-form label-position="top">
        <el-form-item label="操作原因（至少 20 字）" required>
          <el-input v-model="toggleDialog.reason" type="textarea" :rows="4" placeholder="如：本轮行动结束，暂停该层伪装参与新暴露以收缩攻击面…" maxlength="200" show-word-limit />
          <div class="af-muted" style="margin-top: 2px">
            当前 {{ (toggleDialog.reason || '').trim().length }} 字（{{ (toggleDialog.reason || '').trim().length >= 20 ? '已满足' : '还需 ' + (20 - (toggleDialog.reason || '').trim().length) + ' 字' }}）
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="toggleDialog.show = false">取消</el-button>
        <el-button type="primary" :loading="toggleDialog.busy" @click="submitToggle">{{ toggleDialog.targetEnabled ? '确认启用' : '确认停用' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>