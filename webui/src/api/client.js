// API 客户端：WebUI 只是主控 REST API 的客户端（D1，不复制业务逻辑）。
//
// 约定（对齐 app/utils.py 统一封包）：
//   - 成功: {ok:true, data}
//   - 失败: {ok:false, error:{code, message}}
//   - 认证: X-API-Key / Authorization: Bearer（bootstrap admin key）
//   - 401/unauthorized → 清空凭据并跳登录
//   - 凭据存储（审查 P2-8）：sessionStorage 会话级存储——关闭标签页/浏览器即失效，
//     避免 admin key 常驻 localStorage 被持久化窃取；刷新页面会话内保持。
const API_BASE = '/api/v1'

export class ApiError extends Error {
  constructor(code, message, status) {
    super(message)
    this.code = code
    this.status = status
  }
}

let onUnauthorized = null
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

const KEY_STORE = 'sessionStorage' // 审查 P2-8：会话级存储，关闭标签页/浏览器即失效
const KEY_NAME = 'af_api_key'

function storeGet() {
  try {
    return sessionStorage.getItem(KEY_NAME) || ''
  } catch {
    return ''
  }
}

function storeSet(key) {
  try {
    // P2-D7：存储前统一 trim —— 任何入口（手动输入/粘贴/一键登录回填）写入的 key 均不含首尾空白，
    // 与后端 deps.require_admin 的 strip 后比较语义对齐，避免带空格 key 绕过输入校验。
    const trimmed = typeof key === 'string' ? key.trim() : key
    if (trimmed) sessionStorage.setItem(KEY_NAME, trimmed)
    else sessionStorage.removeItem(KEY_NAME)
  } catch {
    /* ignore */
  }
}

export function getApiKey() {
  return storeGet()
}

export function setApiKey(key) {
  storeSet(key)
}

export async function request(path, { method = 'GET', body, query, raw = false } = {}) {
  let url = `${API_BASE}${path}`
  if (query && Object.keys(query).length > 0) {
    const qs = new URLSearchParams()
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') qs.set(k, v)
    }
    const s = qs.toString()
    if (s) url += `?${s}`
  }

  const headers = {}
  const key = getApiKey()
  if (key) headers['X-API-Key'] = key
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  let res
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch (e) {
    throw new ApiError('network_error', `无法连接主控（${url}）: ${e.message}`, 0)
  }

  // 401：统一清凭据跳登录
  if (res.status === 401) {
    setApiKey('')
    if (onUnauthorized) onUnauthorized()
    throw new ApiError('unauthorized', 'API Key 无效或已过期，请重新登录', 401)
  }

  let payload = null
  try {
    payload = await res.json()
  } catch {
    /* non-json */
  }
  // openapi.json 等原始响应
  if (raw) return payload

  if (payload && payload.ok === true) return payload.data
  if (payload && payload.ok === false) {
    const code = payload.error?.code || 'api_error'
    // A10：后端 FastAPI 422 由 app/utils.py 封包为 {ok:false, error:{code:'validation_error',
    // message: str(exc.errors()[:1])}} —— message 是原始 pydantic 文本，直接展示给用户不友好。
    // 这里统一转为可读提示（具体定位靠修复参数本身，不把 pydantic 堆栈渲染进页面）。
    const message =
      code === 'validation_error'
        ? '请求参数不符合接口约束（如单页数量超上限），请调整后重试'
        : payload.error?.message || '请求失败'
    throw new ApiError(code, message, res.status)
  }
  throw new ApiError('bad_response', `主控返回异常响应（HTTP ${res.status}）`, res.status)
}

export const api = {
  // --- system ---
  health: () => request('/system/health'),
  stats: () => request('/system/stats'),
  embeddedDsh: () => request('/system/embedded-dsh'),
  // 一键拉起内嵌 DSH（懒启动守护：调用即拉起，已运行则直接返回 token_url）
  startEmbeddedDsh: () => request('/system/embedded-dsh/start', { method: 'POST' }),
  bootstrapInfo: () => request('/auth/bootstrap-info', { method: 'POST' }),

  // --- soc-lib（社工库防御：服务状态 / 查询 / 分析）---
  // 防御式：端点不存在 / 未授权时 request 抛 ApiError，由调用方 try/catch 降级，前端不崩。
  socLibStatus: () => request('/soc-lib/status'),
  // 主动式分析：对检测内容提取 IOC 并查 HIBP/本地泄露库，返回 {analyzed, hits:[{ioc_type,ioc_value_mask,found,source,breach}]}
  socLibAnalyze: (detId) => request('/soc-lib/analyze/' + detId, { method: 'POST' }),

  // --- persona（拟真账号：定时发布计划 / 立即发布 / 应对骗子 / 实战演练）---
  personaSchedules: () => request('/persona/schedules'),
  personaSchedule: (p) => request('/persona/schedule', { method: 'POST', body: p }),
  personaPublishNow: (scheduleId) =>
    request('/persona/publish-now', { method: 'POST', body: { schedule_id: scheduleId } }),
  // 应对骗子：骗子私信喂给拟真账号 → {result:{matched,reply,reasoning,extracted_iocs}}
  personaRespond: (payload) => request('/persona/respond', { method: 'POST', body: payload }),
  // 实战演练：剧本列表（scenario/description/typical_incoming/suggested_reply/warning/iocs_hint）
  personaDrills: () => request('/persona/drills'),
  // 实战演练：按剧本执行一轮（incoming_text 可选，缺省用剧本典型私信）
  personaDrill: (payload) => request('/persona/drill', { method: 'POST', body: payload }),

  // --- auth（P7/R1 本机模式：仅回环来源可调，非本机一律 403 loopback_only）---
  // 一键登录：直接返回当前 bootstrap admin key（免读文件/免配环境变量）
  localLogin: () => request('/auth/local-login', { method: 'POST' }),
  // 设置页展示：当前 admin key 明文
  currentKey: () => request('/auth/current-key'),
  // 设置页重置：confirm=true + reason>=20 字（GateLog 审计，D6）；成功后旧 key 失效
  resetKey: (reason) =>
    request('/auth/reset-key', { method: 'POST', body: { confirm: true, reason } }),

  // --- scan ---
  scanText: (text, source = 'manual') =>
    request('/scan/text', { method: 'POST', body: { text, source } }),
  scanHits: (limit = 20) => request('/scan/hits', { query: { limit } }),
  // --- detections（详情聚合 + 平台跳转，供详情抽屉使用）---
  detectionFull: (id) => request(`/detections/${id}/full`),
  detectionPlatformLink: (id) => request(`/detections/${id}/platform-link`),
  zhihuImport: (text, source = 'manual', fromUrlName = '') =>
    request('/zhihu/import', { method: 'POST', body: { text, source, from_url_name: fromUrlName } }),

  // --- traps ---
  listTraps: (status, limit = 200) => request('/traps', { query: { status, limit } }),
  getTrap: (id) => request(`/traps/${id}`),
  createTrap: (baitText, note) =>
    request('/traps', { method: 'POST', body: { bait_text: baitText, note } }),
  generateTrapDraft: (templateId, platform, note) =>
    request('/traps/generate-draft', {
      method: 'POST',
      body: { template_id: templateId || null, platform: platform || '评论区', note },
    }),
  deployTrap: (id, targetUrl) =>
    request(`/traps/${id}/deploy`, { method: 'POST', body: { target_url: targetUrl || null } }),
  monitorTrap: (id) => request(`/traps/${id}/monitor`, { method: 'POST' }),
  disableTrap: (id) => request(`/traps/${id}/disable`, { method: 'POST' }),
  // P1-3：退役为敏感操作，必须 confirm=true + reason≥20 字（后端 RetireRequest 双确认闸控）
  retireTrap: (id, reason) =>
    request(`/traps/${id}/retire`, { method: 'POST', body: { confirm: true, reason: reason || '' } }),
  checkTrapHit: (id, text) => request(`/traps/${id}/check-hit`, { method: 'POST', body: { text } }),

  // --- accounts ---
  accountCheck: (urlName, signals) =>
    request('/accounts/check', { method: 'POST', body: { url_name: urlName, signals } }),
  getAccount: (urlName) => request(`/accounts/${encodeURIComponent(urlName)}`),
  accountTimeline: (id) => request(`/accounts/${id}/timeline`),

  // --- grading ---
  gradingLevels: () => request('/grading/levels'),
  gradingExplain: (detectionId, { trapHitCount, accountRisk } = {}) =>
    request(`/grading/explain/${detectionId}`, {
      query: { trap_hit_count: trapHitCount, account_risk: accountRisk },
    }),

  // --- evidence ---
  buildEvidence: (detIds, screenshots) =>
    request('/evidence/build', { method: 'POST', body: { det_ids: detIds, screenshots } }),
  getEvidence: (pkgId) => request(`/evidence/${pkgId}`),
  // P1-A：冻结为敏感操作，主控强制 confirm=true + reason≥20（GateLog 审计）
  freezeEvidence: (pkgId, reason) =>
    request(`/evidence/${pkgId}/freeze`, { method: 'POST', body: { confirm: true, reason } }),
  exportEvidence: (pkgId, fmt = 'json') =>
    request(`/evidence/${pkgId}/export`, { query: { fmt } }),
  reportTemplate: (pkgId, kind = '96110') =>
    request(`/evidence/${pkgId}/report-template`, { query: { kind } }),

  // --- cases ---
  listCases: (page, pageSize, tag) =>
    request('/cases', { query: { page, page_size: pageSize, tag } }),
  publishCase: (detId, redactedPayload, graphTags, confirm, reason) =>
    request('/cases/publish', {
      method: 'POST',
      body: {
        det_id: detId,
        redacted_payload: redactedPayload,
        graph_tags: graphTags,
        // U1：敏感操作双确认（后端 PublishCaseRequest：confirm 必须 true，reason≥20 字）
        confirm: !!confirm,
        reason: reason || '',
      },
    }),
  caseGraph: () => request('/cases/graph'),

  // --- events（P6.5 已落地：事件落库 + REST 查询；MCP af_list_events/af_get_event 同源）---
  listEvents: ({ kind, page = 1, pageSize = 100 } = {}) =>
    request('/events', { query: { kind, page, page_size: pageSize } }),
  getEvent: (id) => request(`/events/${id}`),

  // --- config（P2-1 配置热更新：GET 合成只读；PUT 敏感写 confirm+reason + GateLog 审计）---
  getConfig: () => request('/config'),
  updateConfig: (key, value, reason) =>
    request('/config', { method: 'PUT', body: { key, value, confirm: true, reason } }),

  // --- scan inbox（CH-D 批量消费：POST 消费 pending；GET 读取结果含原文，均需 admin）---
  scanInbox: () => request('/scan/inbox', { method: 'POST' }),
  scanInboxResult: (limit = 50) => request('/scan/inbox', { query: { limit } }),

  // --- zhihu ---
  zhihuChannels: () => request('/zhihu/channels'),
  zhihuStatus: () => request('/zhihu/status'),
  zhihuLogin: (channel, cookie, scheme = 'header') =>
    request(`/zhihu/channels/${channel}/login`, { method: 'POST', body: { cookie, scheme } }),
  zhihuVerify: (channel) => request(`/zhihu/channels/${channel}/verify`, { method: 'POST' }),

  // --- supply-chain（供应链反查：顺藤摸瓜，技能见 skills/supply-chain/SKILL.md）---
  supplyChainQuery: (seed) =>
    request('/supply-chain/query', { method: 'POST', body: { seed } }),
  supplyChainResults: () => request('/supply-chain/results'),
  supplyChainResult: (name) => request(`/supply-chain/results/${encodeURIComponent(name)}`),

  // --- alerts ---
  listAlerts: ({ level, unreadOnly, limit } = {}) =>
    request('/alerts', { query: { level, unread_only: unreadOnly, limit } }),
  markAlertRead: (id) => request(`/alerts/${id}/read`, { method: 'POST' }),

  // --- account-bridges（配置账号 · 私信自动接入 + 反向侦察，P9）---
  // 平台矩阵：{ platforms: [{platform,name,ai_ready,access,projects,status,last_sync}] }
  accountBridges: () => request('/account-bridges'),
  // 单平台详情：同上 + config_fields:[{key,label,type,text?}] + has_config
  accountBridgeDetail: (platform) => request(`/account-bridges/${platform}`),
  accountBridgeSave: (platform, fields) =>
    request(`/account-bridges/${platform}/config`, {
      method: 'PUT',
      body: { fields, reason: '配置账号桥接' },
    }),
  accountBridgeTest: (platform) => request(`/account-bridges/${platform}/test`, { method: 'POST' }),
  accountBridgeIngest: (platform, { from, text }) =>
    request(`/account-bridges/${platform}/ingest`, { method: 'POST', body: { from, text } }),
  accountBridgeAgentImport: (platform) => request(`/account-bridges/${platform}/agent-import`),
  accountBridgeDelete: (platform) => request(`/account-bridges/${platform}`, { method: 'DELETE' }),

  // --- collector（采集矩阵 · 多平台 × 账号 × 策略 编排，P10）---
  // 矩阵全表：可能返回 { cells:[...] } 或直接数组，视图层兼容两种形态
  collectorMatrix: () => request('/collector/matrix'),
  collectorAddCell: (platform, account, strategy) =>
    request('/collector/matrix', { method: 'POST', body: { platform, account, strategy } }),
  collectorUpdateCell: (id, { strategy, enabled }) =>
    request(`/collector/matrix/${id}`, { method: 'PUT', body: { strategy, enabled } }),
  collectorDeleteCell: (id) => request(`/collector/matrix/${id}`, { method: 'DELETE' }),
  collectorRunCell: (id) => request(`/collector/matrix/${id}/run`, { method: 'POST' }),
  collectorRunAll: () => request('/collector/matrix/run-all', { method: 'POST' }),
  // GitHub 技术栈调研结论（Scrapy/Crawlee/DecryptLogin/Wechaty/WeChatFerry/zhihu_spider/NapCatQQ/python-telegram-bot）
  collectorTechStack: () => request('/collector/tech-stack'),

  // --- chat-import（P15：聊天记录一键导入：候选路径探测 / 扫描 / 导入 / 文件导入 / 清空）---
  chatCandidates: () => request('/chat-import/candidates'),
  chatScan: (path) => request('/chat-import/scan', { method: 'POST', body: { path } }),
  chatImport: (path, limit = 2000) =>
    request('/chat-import/import', { method: 'POST', body: { path, limit } }),
  chatImportFile: (filePath, platform = null) =>
    request('/chat-import/import-file', { method: 'POST', body: { file_path: filePath, platform } }),
  chatClearImported: (confirm, reason) =>
    request('/chat-import/imported', { method: 'DELETE', body: { confirm, reason } }),

  // --- speech-patterns（话术库：词条 CRUD / 启停 / 导入导出 / 命中统计）---
  // 契约（话术库适应更新）：GET /speech-patterns?page&page_size&category&keyword → {items,total}（含 hit_count）
  //   列表端点/导入/导出/启停为管理端高频遍历，不做 confirm（响应式开关）；编辑/删除为敏感操作，后端强制 confirm+reason≥20 写 gate_logs。
  // speechPatterns(q)：q 为完整查询串，如 '?page=1&page_size=20&category=fake_investment&keyword=xx'（由视图层组装）。
  speechPatterns: (q) => request('/speech-patterns' + (q || '')),
  speechPatternCreate: (p) => request('/speech-patterns', { method: 'POST', body: p }),
  speechPatternUpdate: (id, p) => request('/speech-patterns/' + id, { method: 'PUT', body: p }),
  speechPatternToggle: (id) => request('/speech-patterns/' + id + '/toggle', { method: 'POST' }),
  speechPatternDelete: (id, p) => request('/speech-patterns/' + id, { method: 'DELETE', body: p }),
  speechPatternImport: (p) => request('/speech-patterns/import', { method: 'POST', body: p }),
  speechPatternExport: () => request('/speech-patterns/export'),

  // --- knowledge（知识库蒸馏：条目列表 / 检索 / 导入 / 调分 / 复核 / 转话术 / 下架 / 删除）---
  // 契约（知识库蒸馏）：GET /knowledge?page&page_size&ktype&keyword&min_harm&sort → {items,total}（含 weighted_score）；
  //   POST /import（{items}）→ {imported, distilled:[{id,subject,ktype,harm_score,decay_weight,weighted_score}]}；
  //   POST /import-text（{text,source?}）→ 蒸馏条目；search 按 weighted_score 排序。
  // 调分/复核/蒸馏成语条/下架/删除 为敏感操作，后端强制 confirm(+reason) 写 gate_logs 审计。
  knowledgeList: (q) => request('/knowledge' + (q || '')),
  knowledgeSearch: (q) => request('/knowledge/search?q=' + (q || '')),
  knowledgeImport: (p) => request('/knowledge/import', { method: 'POST', body: p }),
  knowledgeImportText: (p) => request('/knowledge/import-text', { method: 'POST', body: p }),
  knowledgeHarm: (id, p) => request('/knowledge/' + id + '/harm', { method: 'POST', body: p }),
  knowledgeVerified: (id, p) => request('/knowledge/' + id + '/verified', { method: 'POST', body: p }),
  knowledgeToPattern: (id, p) => request('/knowledge/' + id + '/to-pattern', { method: 'POST', body: p }),
  knowledgeDisable: (id, p) => request('/knowledge/' + id + '/disable', { method: 'POST', body: p }),
  knowledgeDelete: (id, p) => request('/knowledge/' + id, { method: 'DELETE', body: p }),

  // --- cover（反钓鱼伪装：多层伪装身份 + 假信息主动暴露 + 接触登记锁定骗子范围）---
  // 契约（A队 cover-backend 实际实现）：GET/POST /cover/identities（data.identities 纯数组，layer 后端自动算无需传）；
  //   POST /{id}/toggle、DELETE /{id} 均需 {confirm:true, reason≥20字}（GateLog 审计）；
  //   POST /{id}/expose → data.exposure.checklist[{field,label,value,masked}]（value 原文供复制泄露，masked 供展示）；
  //   POST /cover/contact（{identity_id,contact_type,contact_value,trap_id?,det_id?}）→ data.event；
  //   GET /cover/contacts?identity_id&contact_type&escalated&limit → data.contacts 纯数组（锁定字段 escalated 0/1）；
  //   GET /cover/map → data.tree 嵌套树。
  coverIdentities: () => request('/cover/identities'),
  coverCreate: (p) => request('/cover/identities', { method: 'POST', body: p }),
  coverToggle: (id, p) => request('/cover/identities/' + id + '/toggle', { method: 'POST', body: p }),
  coverDelete: (id, p) => request('/cover/identities/' + id, { method: 'DELETE', body: p }),
  coverExpose: (id) => request('/cover/identities/' + id + '/expose', { method: 'POST' }),
  coverContact: (p) => request('/cover/contact', { method: 'POST', body: p }),
  coverContacts: (q) => request('/cover/contacts' + (q || '')),
  coverMap: () => request('/cover/map'),
}