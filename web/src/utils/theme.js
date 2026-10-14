/**
 * 控制台主题：浅色默认、暗色可切，选择记住。
 *
 * 落点两处：
 * 1) `<html data-theme="light|dark">` —— tokens.css 靠它选令牌；
 * 2) `<html class="dark">` —— element-plus 自带的深色变量挂在这个 class 上，
 *    不跟着翻的话弹层/下拉会留一套亮色底。
 *
 * 与 api/index.js 的 `keepsake.console.token` 是两个独立键，互不影响。
 * 全部逻辑写成纯函数（storage / doc 都可注入），node --test 直接跑，不碰真实 DOM。
 */
export const THEME_KEY = 'keepsake.console.theme'
export const DEFAULT_THEME = 'light'

const isDark = (v) => v === 'dark'

/** 缺省 / 非法值一律回落 light —— 宁可浅色，也不要一屏没配色的裸 html */
export function readTheme({ storage } = {}) {
  const s = storage ?? globalThis.localStorage
  const v = s?.getItem?.(THEME_KEY)
  return isDark(v) ? 'dark' : DEFAULT_THEME
}

/** 把主题写到 html 上；返回归一化后的主题名 */
export function applyTheme(theme, { doc } = {}) {
  const d = doc ?? globalThis.document
  const t = isDark(theme) ? 'dark' : DEFAULT_THEME
  const el = d?.documentElement
  if (el) {
    el.setAttribute('data-theme', t)
    el.classList.toggle('dark', t === 'dark')
  }
  return t
}

/** 切主题：写 DOM + 落盘，返回新主题 */
export function setTheme(theme, opts = {}) {
  const t = applyTheme(theme, opts)
  const s = (opts ?? {}).storage ?? globalThis.localStorage
  s?.setItem?.(THEME_KEY, t)
  return t
}

/** 在 light / dark 之间来回切 —— 侧边栏那个按钮就调它 */
export function toggleTheme(opts = {}) {
  return setTheme(readTheme(opts) === 'dark' ? 'light' : 'dark', opts)
}
