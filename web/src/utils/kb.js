/**
 * 知识库检索的三个纯函数（node --test 直接跑，不必挂载组件）：
 *
 *  - mergeCounts  两路计数合成「文档 N 条 · 切片 M 条」（N/M 来自各自接口的 count，不写死）
 *  - hasAnyHit    「任一有结果就算找得到」——只有两路都 0 才算没找到
 *  - isStale      请求序号过期判定 —— 旧响应一律丢弃，绝不覆盖新响应
 *
 * 这三条是「编译能过、上线才发现」的逻辑（计数写死、串响应、空态判错），
 * 所以抽成纯函数钉在单测里，页面上只留拼装。
 */

/** 两路计数合并成一行文案 */
export function mergeCounts(docCount, chunkCount) {
  return `文档 ${docCount || 0} 条 · 切片 ${chunkCount || 0} 条`
}

/** 任一有结果就算找得到 */
export function hasAnyHit(docCount, chunkCount) {
  return (docCount || 0) > 0 || (chunkCount || 0) > 0
}

/** 请求序号落后于当前序号 ⇒ 过期，响应直接丢弃 */
export function isStale(seq, current) {
  return seq !== current
}