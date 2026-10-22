/**
 * 图表主题 —— 全站**唯一**的图表色值出处（2026-10 ks_todo 看板图表化）。
 *
 * 🔴 **为什么这里要写死色值**：ECharts 画在 canvas 上，读不到 CSS 变量
 *   （`var(--accent)` 在 canvas 里是无效颜色）。所以色值必须有一份「十六进制」
 *   的副本 —— 那就**集中放在这一个文件里**，别处引用。
 *
 * 🔴 **色值来源**：`styles/tokens.css`（= DESIGN.md 的令牌表）的**原样取值**，
 *   一处不差：`tests/chart-ui.test.mjs` 会逐个核对「本文件的每个 hex 都能在
 *   tokens.css 里找到」—— 改了令牌忘了改这里，测试先红。
 *
 * 🔴 **散在图表配置里的色值 = 违规**：`tests/chart-ui.test.mjs` 会扫三个图表
 *   组件里的裸 hex / rgba，一个都不许有。
 *
 * 🔴 **明暗两套**：深色主题下画布底色、网格线、文字色都要换。主题判定读
 *   `<html>` 上的 `data-theme`（`utils/theme.js` 写的那一个）与 `.dark` 类
 *   （element-plus 那套）—— 两处都认，谁先变都跟着变。
 *
 * 本模块**不 import Vue**（纯函数 + 纯数据）⇒ node --test 直接跑。
 */

/** 字体栈（与 tokens.css 的 `--font` 逐字相同）。 */
export const CHART_FONT =
  'Inter, system-ui, -apple-system, "Segoe UI", "PingFang SC", sans-serif'

/** 两套主题的色板（键名 = 用途，不是令牌名）。 */
export const CHART_PALETTE = {
  light: {
    bg: '#ffffff',
    panel: '#f5f5f6',
    grid: '#ececee',
    track: '#f1f1f2',
    text: '#1a1a1a',
    textDim: '#555555',
    textFaint: '#666d78',
    accent: '#2f6bff',
    accentSoft: '#eaf1ff',
    onAccent: '#ffffff',
    ok: '#22c55e',
    warn: '#b45309',
    danger: '#dc2626',
  },
  dark: {
    bg: '#0b0d10',
    panel: '#12151a',
    grid: '#242a33',
    track: '#1b1f27',
    text: '#eef1f6',
    textDim: '#a7b0be',
    textFaint: '#7c8697',
    accent: '#5b8cff',
    accentSoft: '#16233d',
    onAccent: '#ffffff',
    ok: '#22c55e',
    warn: '#f59e0b',
    danger: '#f05252',
  },
}

/**
 * 当前主题名（`'light'` / `'dark'`）。
 *
 * 🔴 判据 = `<html>` 上的 `data-theme` 属性 **或** `.dark` 类 —— 两处都认：
 *   `utils/theme.js` 切主题时两个都写（前者选令牌，后者给 element-plus），
 *   但外部脚本可能只改其中一个。
 *
 * @param {object} doc  document（默认取全局；测试可注入假 doc）
 */
export function currentTheme(doc) {
  const d = doc ?? (typeof document !== 'undefined' ? document : null)
  const el = d?.documentElement
  if (!el) return 'light'
  const attr = el.getAttribute?.('data-theme')
  if (attr === 'dark' || attr === 'light') return attr
  const cls = el.classList
  if (cls?.contains?.('dark')) return 'dark'
  return 'light'
}

/**
 * 当前主题的色板（拿不到 document 时回落浅色 —— 宁可浅色，也不要一块黑底黑字）。
 * @param {object} doc document（可注入）
 */
export function chartPalette(doc) {
  return CHART_PALETTE[currentTheme(doc)]
}

/**
 * 三个图表共用的**基础 option**（字体 / 背景 / 网格线 / 文字色）。
 *
 * 🔴 网格线用 `--chart-grid`（**不透明**的一档），**不是** `--border`：
 *   canvas 的线条只吃实色，半透明白描边在画布上画不出来；
 *   也不用 `--text-ghost`（那是装饰专用色，网格线不该跟它一个档）。
 *
 * @param {object} palette 色板（默认取当前主题）
 * @param {object} extra    要合并进 option 的字段（浅合并）
 */
export function baseOption(palette, extra = {}) {
  const p = palette || chartPalette()
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: CHART_FONT, color: p.textDim },
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    ...extra,
  }
}

/**
 * 占用率的配色档（与 `utils/agents.js` 的 `usageTone` 同一套阈值）：
 * ≥100% 危险 / ≥90% 告警 / 其余正常。
 *
 * 🔴 90% 这条线是任务书定的（看板「超 90% 用告警色」）；与
 *   `usageTone` 的 80% 档**故意不同** —— 那条管的是卡片上的细进度条，
 *   这条管的是图表上的整根柱子，颗粒度不同。
 *
 * @param {number} percent 占用百分比
 * @param {object} palette  色板
 */
export function usageColor(percent, palette) {
  const p = palette || chartPalette()
  const v = Number(percent)
  if (!Number.isFinite(v)) return p.accent
  if (v >= 100) return p.danger
  if (v >= 90) return p.warn
  return p.accent
}

/**
 * 订阅主题变化（`<html>` 的 `data-theme` / `class` 变了就回调）。
 *
 * 🔴 用 `MutationObserver` 而不是让每个组件各自 import 主题 store ——
 *   主题那一份真身在 `utils/theme.js`，图表这边只管「变了重画一次」。
 *
 * @param {Function} cb 回调（无参）
 * @param {object}   opt `{doc, MutationObserver}`（测试可注入）
 * @returns {Function} 取消订阅
 */
export function onThemeChange(cb, opt = {}) {
  const d = opt.doc ?? (typeof document !== 'undefined' ? document : null)
  const MO = opt.MutationObserver ?? (typeof MutationObserver !== 'undefined' ? MutationObserver : null)
  const el = d?.documentElement
  if (!el || !MO) return () => {}
  const obs = new MO(() => cb())
  obs.observe(el, { attributes: true, attributeFilter: ['data-theme', 'class'] })
  return () => obs.disconnect()
}
