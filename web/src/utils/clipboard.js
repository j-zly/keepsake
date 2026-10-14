/**
 * 复制到剪贴板（key / hash 一键复制用）。
 *
 * navigator.clipboard 只在**安全上下文**可用：控制台经后端同源挂在
 * https 或 127.0.0.1 下没问题，但局域网 http://192.168.x.x 访问时是 undefined，
 * 所以保留 textarea + execCommand 兜底。返回是否成功，调用方据此 toast。
 */
export async function copyText(text) {
  const s = String(text ?? '')
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(s)
      return true
    }
  } catch {
    // 权限被拒/剪贴板不可用 → 落到下面的兜底
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = s
    ta.setAttribute('readonly', '')
    ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;pointer-events:none'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(ta)
    return ok
  } catch {
    return false
  }
}