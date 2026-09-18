// fmtTime：ISO 时间 → 本地 'YYYY-MM-DD HH:mm'（Date 解析 + padStart 格式化）。
// 空值/无效值原样回退，保证与旧渲染行为一致。
export function fmtTime(iso) {
  if (iso == null || iso === '') return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
