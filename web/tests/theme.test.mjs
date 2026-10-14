/**
 * theme.js 的最小自检：纯函数 + 可注入 storage/doc，`node --test tests/` 直接跑。
 * 盯三件容易静默出错的事：缺省必须是 light、非法值不能放行、切换必须落盘。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { THEME_KEY, readTheme, setTheme, toggleTheme, applyTheme } from '../src/utils/theme.js'

/** 假 localStorage：只存字符串，和真的一样按 key 取 */
function fakeStorage(seed = {}) {
  const m = new Map(Object.entries(seed))
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    dump: () => Object.fromEntries(m),
  }
}

/** 假 document：只留 theme.js 真正碰到的两个成员 */
function fakeDoc() {
  const classes = new Set()
  const attrs = {}
  const el = {
    attrs,
    classes,
    setAttribute: (k, v) => {
      attrs[k] = v
    },
    classList: {
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
  }
  return { documentElement: el }
}

test('缺省即浅色：没存过、存了空串、存了非法值，一律 light', () => {
  assert.equal(readTheme({ storage: fakeStorage() }), 'light')
  assert.equal(readTheme({ storage: fakeStorage({ [THEME_KEY]: '' }) }), 'light')
  assert.equal(readTheme({ storage: fakeStorage({ [THEME_KEY]: 'dark ' }) }), 'light')
  assert.equal(readTheme({ storage: fakeStorage({ [THEME_KEY]: 'solarized' }) }), 'light')
})

test('读出已存的暗色', () => {
  assert.equal(readTheme({ storage: fakeStorage({ [THEME_KEY]: 'dark' }) }), 'dark')
})

test('applyTheme 写 data-theme，并同步 element-plus 的 html.dark 类', () => {
  const doc = fakeDoc()
  assert.equal(applyTheme('dark', { doc }), 'dark')
  assert.equal(doc.documentElement.attrs['data-theme'], 'dark')
  assert.ok(doc.documentElement.classes.has('dark'))

  assert.equal(applyTheme('light', { doc }), 'light')
  assert.equal(doc.documentElement.attrs['data-theme'], 'light')
  assert.equal(doc.documentElement.classes.has('dark'), false)
})

test('切换写 localStorage，且与 token 键互不干扰', () => {
  const storage = fakeStorage({ 'keepsake.console.token': 'keep-me' })
  const doc = fakeDoc()

  assert.equal(toggleTheme({ storage, doc }), 'dark')
  assert.equal(storage.dump()[THEME_KEY], 'dark')
  assert.equal(storage.dump()['keepsake.console.token'], 'keep-me')

  assert.equal(toggleTheme({ storage, doc }), 'light')
  assert.equal(storage.dump()[THEME_KEY], 'light')
  assert.equal(storage.dump()['keepsake.console.token'], 'keep-me')
})

test('setTheme 非法值也落盘成 light，不把脏值留在 localStorage 里', () => {
  const storage = fakeStorage()
  assert.equal(setTheme('neon', { storage, doc: fakeDoc() }), 'light')
  assert.equal(storage.dump()[THEME_KEY], 'light')
})

/**
 * 令牌表本身的自检：CSS 一旦被手改错主题，这里先红。
 * 只查两件事 —— 无 data-theme 时 --bg 必须是浅色、暗色只能在 [data-theme='dark'] 下生效。
 */
const CSS = readFileSync(new URL('../src/styles/tokens.css', import.meta.url), 'utf8')

/** 取某个选择器块里的 `--bg` 值 */
function bgOf(selector) {
  const at = CSS.indexOf(selector + ' {')
  assert.ok(at >= 0, `tokens.css 里找不到 ${selector}`)
  const m = /--bg:\s*(#[0-9a-f]{3,8})/.exec(CSS.slice(at, CSS.indexOf('}', at)))
  assert.ok(m, `${selector} 块里没有 --bg`)
  return m[1]
}

/** WCAG 相对亮度 */
function lum(hex) {
  const v = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
  return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
}
const contrast = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}

const DARK_SEL = ':root[data-theme="dark"]'

test('tokens.css：html 无 data-theme 时就是浅色底', () => {
  const bg = bgOf(':root')
  assert.match(bg, /^#f[0-9a-f]{5}$/, `默认 --bg 应是浅色，实际 ${bg}`)
})

test('tokens.css：暗色只在 [data-theme="dark"] 下生效', () => {
  assert.match(CSS, /:root\[data-theme=['"]dark['"]\]\s*\{/)
  // 全表只允许一处裸 :root（浅色默认）；再多一处就说明有人把暗色写回默认块了
  const bare = [...CSS.matchAll(/(^|[\s,])root(?=[\s,{])/g)].length
  assert.ok(bare <= 1, `裸 :root 出现 ${bare} 次`)
  // element-plus 接管块也挂在 :root 上，但那一块不定义 --bg，不影响首屏底色
  assert.equal(bgOf(DARK_SEL), '#0b0d10')
})

test('对比度：浅色正文/次要文字 ≥4.5:1（WCAG AA）', () => {
  const light = CSS.slice(CSS.indexOf(':root {'), CSS.indexOf(DARK_SEL + ' {'))
  const g = (name) => new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`).exec(light)[1]
  assert.ok(contrast(g('text'), g('bg-elev')) >= 4.5)
  assert.ok(contrast(g('text-dim'), g('bg-elev')) >= 4.5)
})

test('对比度：暗色正文/次要文字 ≥4.5:1（WCAG AA）', () => {
  const dark = CSS.slice(CSS.indexOf(DARK_SEL + ' {'))
  const g = (name) => new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`).exec(dark)[1]
  assert.ok(contrast(g('text'), g('bg-elev')) >= 4.5)
  assert.ok(contrast(g('text-dim'), g('bg-elev')) >= 4.5)
})
