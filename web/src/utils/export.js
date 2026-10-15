/**
 * 文件下载共用件（导出 JSONL/CSV、下载导入模板）。
 *
 * 🔴 为什么必须走 fetch 而不是裸 `<a href>`：这些端点要 `Authorization` 头，
 * 而裸链子只能带 URL —— token 进 URL 会被浏览器历史 / Referer / 代理日志留档。
 * 所以：fetch 带头 → blob → 临时 `<a download>` 点一下 → 立刻 revoke。
 *
 * baseURL 只从 http 实例取（`import.meta.env` 那份由 api/index.js 管），别在这里
 * 再读一遍环境变量 —— 两份 baseURL 迟早有一份是空的。
 */
import http, { getToken } from '../api'

/** 失败时把后端的 JSON 文案取出来（否则下载失败只会看到「status 400」） */
async function errorText(res) {
  try {
    const d = JSON.parse(await res.text())
    return d?.detail?.message || d?.detail?.error || `后端返回 ${res.status}`
  } catch {
    return `后端返回 ${res.status}`
  }
}

/**
 * 拉一个端点并把响应体存成文件下载，返回落到磁盘的文件名。
 * @param {string} path     端点路径（可带 query）
 * @param {string} filename 下载文件名
 */
export async function downloadFile(path, filename) {
  const token = getToken()
  const res = await fetch(`${http.defaults.baseURL || ''}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) throw new Error(await errorText(res))
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
  return filename
}

/**
 * 下载导入模板（`kind` = memory | kb | core）。字段名三域各不相同
 * （记忆条目 content / 知识库 title+content），猜错的表现是「导入 N 条全是 0」。
 */
export function downloadTemplate(kind) {
  return downloadFile(
    `/api/v1/import/template?kind=${encodeURIComponent(kind)}`,
    `keepsake-import-template-${kind}.jsonl`,
  )
}
