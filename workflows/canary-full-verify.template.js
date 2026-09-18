#!/usr/bin/env -S node --input-type=module
// 金丝雀蜜罐 · 可复用全量验证工作流（DSH workflow 脚本模板）
// 用法：在 DSH 会话中用 workflow 工具执行本脚本（或按段复制到 workflow 调用的 JS 体）。
// 目的：一键跑「安全纵深/性能/数据完整性/MCP+WebUI/供应链采集/内嵌DSH」六方向 + 双盲交叉 + 修复指引。
// 参数：args.project_root（远程项目根，默认 C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐）、
//       args.models（显式 provider/model，如 {provider:'newapi', model:'deepseek-v4-flash'}）
// 说明：这是「工作流编排参考模板」——实际执行时把每一队内容替换为 agent(prompt, {...}) 调用
//       （prompt 需含完整远程操作指令，见 docs/验证工作流指南.md）。

// ==================== 模板常量 ====================
const MODELS = args.models || { provider: 'newapi', model: 'deepseek-v4-flash' };
const PROJ = args.project_root || 'C:\\Users\\Administrator\\Desktop\\知乎黑客松\\金丝雀蜜罐';

// 公共工作方式段（每队 prompt 前缀）
const COMMON = [
`你是金丝雀蜜罐项目验证工程师。项目根：${PROJ}（远程 192.168.10.110 免密 SSH，中文路径单引号）。9200 主控 health 200。admin key 读 data\\bootstrap_admin_key.txt（勿硬编码）。`,
`工作方式：ssh -o BatchMode=yes -o StrictHostKeyChecking=no 192.168.10.110 "powershell -NoProfile -EncodedCommand $b64"（$b64=Unicode-Base64，脚本首行 [Console]::OutputEncoding=[Text.Encoding]::UTF8）；中文请求体用 [Text.Encoding]::UTF8.GetBytes(body) 字节 POST；只验证不改产品代码；不重启主控/不碰 dsh/ 与全局 3080/不提交 git；报告双写远程 docs/ + 本地工作区。`,
].join('\n');

// ==================== 六方向模板 ====================
// 每队 = 完整 prompt（含具体验证清单），此处给骨架，实跑时按 PLAN 填充
const TEAMS = {
  security:   { name: '队A-安全纵深',     prompt: COMMON + '\n验证：SQL注入/XSS/路径穿越/鉴权矩阵/越权/限流/封包/安全头/密钥落库/Secrets 泄露 → docs/安全纵深验证报告_队A.md' },
  perf:       { name: '队B-性能并发',     prompt: COMMON + '\n验证：scan并发压测/大分页/慢查询/蜜饵高频/多实例/调度并发/内存/并发写 → docs/性能与并发验证报告_队B.md' },
  data:       { name: '队C-数据完整性',   prompt: COMMON + '\n验证：gate_logs哈希链/证据冻结防篡改/迁移幂等/半写事务/重启恢复/并发同记录/空库首启/孤儿/截断 → docs/数据完整性验证报告_队C.md' },
  mcp_ui:     { name: '队D-MCP+WebUI',    prompt: COMMON + '\n验证：MCP 38工具透传(参数/错误/敏感confirm/主控不可达) + WebUI 21视图/SPA fallback/console指向3092/client统一处理 → docs/MCP与WebUI深度验证报告_队D.md' },
  supply:     { name: '队E-供应链采集',   prompt: COMMON + '\n验证：供应链多级续查/非法seed/循环防护/采集矩阵CRUD/run降级/调度/tech-stack → docs/供应链与采集矩阵验证报告_队E.md' },
  dsh:        { name: '队F-内嵌DSH',      prompt: COMMON + '\n验证：3092隔离/空白home/无全局插件/token鉴权/启停循环/换环境自包含/主控集成 → docs/内嵌DSH隔离验证报告_队F.md' },
};

// ==================== 阶段编排 ====================
phase(`阶段1-六方向并行（${PROJ}）`);
// 实际执行时：
// const results = await parallel(Object.entries(TEAMS).map(([k, t]) =>
//   () => agent(t.prompt, { label: t.name, phase: '阶段1', ...MODELS })));
// 阶段2-双盲抽查 + 阶段3-修复闭环 + 阶段4-全量回归 + 阶段5-报告汇总
// （按 PLAN-其它方向深度验证与对标升级.md 执行）

return {
  template: 'canary-full-verify',
  project: PROJ,
  teams: Object.keys(TEAMS),
  guide: 'docs/验证工作流指南.md（完整执行手册）',
  note: '本文件为编排模板；实跑时把 TEAMS 各 prompt 展开为完整验证清单（每队 8-12 项，含操作/预期/实测/证据），并按阶段串并行。',
};