/**
 * cron 表达式 → 人话副标（定时任务页的「调度」中文解释）。
 *
 * 为什么单独一个文件：这张表上写错一个字的代价是**用户以为班车一天只跑一次**
 * （`15 * * * *` 是每小时第 15 分，不是每天 00:15）。护栏要能**真跑一遍**这个
 * 函数钉住口径，纯函数才好测（web/tests/cron-hint.test.mjs）。
 *
 * 纪律：**认不出来的形状一律退「自定义调度」（不许猜语义）** ——
 * 宁可让用户去看 cron 原文，也不给一句编出来的中文。
 */

const WEEK_CN = ['日', '一', '二', '三', '四', '五', '六']

/** 两位补零（04:30 这种写法）。 */
const hh = (s) => String(s).padStart(2, '0')

/** 是不是一个普通的十进制数（0-59 / 0-23 这类单值字段）。 */
const isNum = (s) => /^\d+$/.test(s)

/**
 * @param {string} expr 五段 cron（分 时 日 月 周）
 * @returns {string} 人话副标
 */
export function cronHint(expr) {
  const p = String(expr || '').trim().split(/\s+/)
  if (p.length !== 5) return '自定义调度'
  const [min, hour, dom, mon, dow] = p

  if (dom === '*' && mon === '*' && dow === '*') {
    // 每 N 分钟（小时域全开）：`*/5 * * * *`
    if (hour === '*' && min.startsWith('*/')) return `每 ${min.slice(2)} 分钟`
    // 🔴 小时域是 `*` ⇒ **每小时都触发**（不是每天）：`15 * * * *` = 每小时第 15 分
    if (hour === '*') {
      if (!isNum(min)) return '自定义调度'
      return Number(min) === 0 ? '每小时整点' : `每小时第 ${min} 分`
    }
    // 每 N 小时（`0 */2 * * *` = 每 2 小时；分位非 0 时把分位带上）
    if (hour.startsWith('*/')) {
      const n = hour.slice(2)
      if (!isNum(n) || Number(n) < 1 || !isNum(min)) return '自定义调度'
      return Number(min) === 0 ? `每 ${n} 小时` : `每 ${n} 小时第 ${min} 分`
    }
    // 每天某个钟点（`30 4 * * *` = 每天 04:30）
    if (isNum(min) && isNum(hour)) return `每天 ${hh(hour)}:${hh(min)}`
    // 分位是 `*/N`、小时是定值：这一小时里每 N 分钟
    if (min.startsWith('*/') && isNum(hour)) {
      return `每天 ${hh(hour)}:00 起每 ${min.slice(2)} 分钟`
    }
    return '自定义调度'
  }

  // 每周（周字段 0 与 7 都是周日）
  if (dom === '*' && mon === '*' && /^\d$/.test(dow) && isNum(min) && isNum(hour)) {
    return `每周${WEEK_CN[Number(dow) % 7]} ${hh(hour)}:${hh(min)}`
  }
  // 每月某天（`30 4 * 5 *` = 5 月 04:30）
  if (dom === '*' && isNum(mon) && isNum(min) && isNum(hour)) {
    return `${Number(mon)} 月 ${hh(hour)}:${hh(min)}`
  }
  return '自定义调度'
}
