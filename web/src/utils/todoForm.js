/**
 * 新增待办的表单口径（**纯 JS，不 import Vue** ⇒ `node --test` 可直接跑真函数）。
 *
 * 为什么单独一份：弹框（components/TodoDialog.vue）与页面（views/TodosView.vue）
 * 要对「什么算一条合法的待办」有**同一份**说法 —— 两处各写一遍校验，
 * 迟早漂成「弹框放行、后端 400」（用户填了个空格标题，提交后才报错）。
 *
 * 字段口径与后端 `TodoCreateBody` 对齐：title 必填、detail / owner / due 可空
 * （owner 空 = 主代理，due 空 = 不限期）。
 */

/** 标题上限（后端同口径：≤200 字）。 */
export const MAX_TITLE = 200

/** 正文上限（后端同口径：≤4000 字）。 */
export const MAX_DETAIL = 4000

/**
 * 一张空白表单。
 * @param {string} owner 归属代理 id（传上一次的选中项 ⇒ 连着派活不用重选）
 */
export function blankForm(owner = '') {
  return { title: '', detail: '', owner: owner || '', due: '' }
}

/**
 * 表单校验：返回**人话错误串**，空串 = 通过。
 *
 * 🔴 空标题前端先拦：后端也会拒，但那时用户已经填完一整张表单了。
 *
 * @param {{title?: string, detail?: string, owner?: string, due?: string}} form
 * @returns {string} 错误说明（空串 = 没问题）
 */
export function todoFormError(form) {
  const title = String(form?.title ?? '').trim()
  if (!title) return '标题不能为空'
  if (title.length > MAX_TITLE) return `标题最长 ${MAX_TITLE} 字`
  const detail = String(form?.detail ?? '')
  if (detail.length > MAX_DETAIL) return `详情最长 ${MAX_DETAIL} 字`
  return ''
}

/**
 * 表单 → 提交载荷（后端 `POST /api/v1/todos` 的 body）。
 *
 * 标题去掉首尾空白（后端也会 trim，但前端先交干净的，省得存进去带空格）。
 * detail / owner / due 空串 = 不给正文 / 交给主代理 / 不限期。
 *
 * @param {{title?: string, detail?: string, owner?: string, due?: string}} form
 */
export function todoPayload(form) {
  return {
    title: String(form?.title ?? '').trim(),
    detail: String(form?.detail ?? ''),
    owner: String(form?.owner ?? ''),
    due: String(form?.due ?? ''),
  }
}
