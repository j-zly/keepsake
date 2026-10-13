/**
 * 控制台统一的提示 / 二次确认弹框。
 *
 * 🔴 禁原生 alert / confirm —— 原生弹框挡不住「随手按回车」，而本控制台的
 * 写操作会真改数据。所有写按钮都必须先过 showConfirm / showPrompt。
 *
 * 🔴 `closeOnClickModal: false`：误点遮罩 = 误确认，代价是改数据。
 * 取消 / ESC / 关 X 一律 resolve 成 false（或 null），调用方直接 return。
 */
import { ElMessage, ElMessageBox } from 'element-plus'

export function showToast(message, type = 'success') {
  ElMessage({ message, type, duration: 4000 })
}

export function showError(message) {
  showToast(message, 'error')
}

/** 是/否二次确认。返回 Promise<boolean>。 */
export function showConfirm(message, title = '二次确认', type = 'warning') {
  return ElMessageBox.confirm(message, title, {
    type,
    confirmButtonText: '确认执行',
    cancelButtonText: '取消',
    closeOnClickModal: false,
  })
    .then(() => true)
    .catch(() => false)
}

/**
 * 手输确认串（归档要 key 末 8 位；merge / forget 实跑要当前计数）。
 * 返回 Promise<string|null>；取消为 null。
 */
export function showPrompt(message, title, { expect = '', tip = '' } = {}) {
  return ElMessageBox.prompt(message, title, {
    confirmButtonText: '确认执行',
    cancelButtonText: '取消',
    closeOnClickModal: false,
    inputPlaceholder: tip || expect,
    inputValidator: (v) => {
      const got = (v || '').trim()
      if (!got) return '必须输入确认内容'
      if (got !== expect) return `与「${expect}」不一致，请照抄`
      return true
    },
  })
    .then(({ value }) => (value || '').trim())
    .catch(() => null)
}