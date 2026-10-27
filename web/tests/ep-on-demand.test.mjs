/**
 * Element Plus **按需引入**的护栏（2026-10 ks_maint_ui_dedup · G5 产物体积门）。
 *
 * 整包 `app.use(ElementPlus)` 会把全库组件 + 全量 CSS 打进产物（≈2.29 MB，
 * 超 1.6 MB 上限）。改成按需注册后 ≈1.50 MB，代价是**两件事必须手写对**：
 *
 *   1) **组件登记**：模板里出现的每一个 `<el-xxx>` 都要在 main.js 里注册，
 *      漏一个 = 那个组件渲染不出来（Vue 只在控制台 warn，页面直接缺一块）；
 *   2) **样式登记**：element-plus 的 es 模块**不带样式**，样式在
 *      `theme-chalk/*.css` 里，漏一个 = 组件没样式（同样只有 warn）。
 *
 * 两条都在这里钉死 —— 静态扫描，不用起浏览器。
 *
 * 🔴 2026-10 ks_radio_reg_fix：静态扫描是**闸盲区**。它只看得出 main.js
 * **导入**了哪些组件名，看不出「导入 ≠ 注册」。element-plus 的 radio 族是
 * 全库唯一 install = NOOP 的一族（withNoopInstall）：
 *
 *     ElRadioGroup  = withNoopInstall(...)   ← install 是 NOOP
 *     ElRadioButton = withNoopInstall(...)   ← install 是 NOOP
 *
 * 所以 `app.use(ElRadioGroup)` **一个组件都没注册**，模板里的
 * `<el-radio-group>` 运行时直接 "Failed to resolve component"，
 * 页面上的切换器退化成裸文字连排（生产事故，实锤，非猜测）。
 *
 * 因此下面除静态断言外，补两道**运行时**断言：
 *   A) 拿 main.js 里**原样的注册代码**注册一遍，断言注册表里每个组件都在；
 *   B) createSSRApp + renderToString 真渲染每个 `<el-xxx>`，
 *      断言输出里没有 "Failed to resolve component"、且出现组件自己的根类名。
 *
 * 护栏必须跑 **main.js 的原代码**，不能在护栏里另写一套注册 —— 否则
 * main.js 改回 app.use(c) 时护栏照样绿，等于没抓这次的病。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createApp, createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import * as EP from 'element-plus'
import { provideGlobalConfig, ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const MAIN = readFileSync(join(SRC, 'main.js'), 'utf8')
const THEME = fileURLToPath(new URL('../node_modules/element-plus/theme-chalk', import.meta.url))

/** src 下所有 .vue（模板里的 <el-xxx> 都从这里扫）。 */
function vues(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) vues(p, out)
    else if (name.endsWith('.vue')) out.push(p)
  }
  return out
}
const TAGS = new Set()
for (const f of vues(SRC)) {
  for (const m of readFileSync(f, 'utf8').matchAll(/<(el-[a-z0-9-]+)/g)) TAGS.add(m[1])
}

/** main.js 里从 'element-plus' 具名导入的组件名。 */
const REGISTERED = new Set(
  (MAIN.match(/import\s*\{([^}]*)\}\s*from\s*'element-plus'/) || ['', ''])[1]
    .split(',')
    .map((s) => s.trim())
    .filter((s) => /^El[A-Z]/.test(s)),
)

/** <el-input-number> → ElInputNumber */
function pascal(tag) {
  return 'El' + tag.slice(3).split('-').map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join('')
}

test('模板里用到的每个 element-plus 组件都在 main.js 登记了', () => {
  const missing = [...TAGS].filter((t) => !REGISTERED.has(pascal(t)))
  assert.deepEqual(missing, [], `这些组件没登记 = 页面渲染不出来：${missing.join(', ')}`)
})

test('main.js 不再整包引入 element-plus（那会把 2MB 全量 CSS/JS 打进产物）', () => {
  assert.doesNotMatch(MAIN, /element-plus\/dist\/index\.css/, '不许再引全量 CSS')
  assert.doesNotMatch(MAIN, /app\.use\(ElementPlus/, '不许再整包注册')
  assert.doesNotMatch(MAIN, /import\s+ElementPlus\s+from\s+'element-plus'/, '不许再默认导入整包')
})

test('每个登记组件都有对应的 theme-chalk 样式（漏了 = 组件没样式）', () => {
  const missing = []
  for (const c of REGISTERED) {
    const kebab = 'el-' + c.slice(2).replace(/([a-z0-9])([A-Z])/g, '$1-$2').toLowerCase()
    if (!existsSync(join(THEME, `${kebab}.css`))) continue   // 没有独立样式的组件（如 ElMessageBox 走 el-message-box）
    if (!MAIN.includes(`theme-chalk/${kebab}.css`)) missing.push(kebab)
  }
  assert.deepEqual(missing, [], `这些组件的 CSS 没引 = 没样式：${missing.join(', ')}`)
})

test('子组件 / 面板层的样式也在（日期面板 / 下拉面板 / 骨架项 / 折叠动画）', () => {
  for (const f of ['el-date-picker-panel', 'el-select-dropdown', 'el-skeleton-item',
    'el-collapse-transition', 'el-time-picker', 'el-time-select', 'el-overlay',
    'el-popper', 'el-scrollbar', 'el-tooltip', 'el-icon']) {
    assert.ok(MAIN.includes(`theme-chalk/${f}.css`), `缺 ${f}.css`)
  }
})

test('语言包走 provideGlobalConfig（按需引入后中文不能丢）', () => {
  assert.match(MAIN, /provideGlobalConfig\(\{ locale: zhCn \}, app, true\)/)
  assert.match(MAIN, /import zhCn from 'element-plus\/es\/locale\/lang\/zh-cn'/)
})

// ─────────────────────────────────────────────────────────────
// 🔴 运行时护栏（ks_radio_reg_fix）
// ─────────────────────────────────────────────────────────────

const LOOP_RE = /for\s*\(\s*const\s+([A-Za-z_$][\w$]*)\s+of\s*\[/

/** 去掉注释和字符串字面量（保持长度不变），只留结构字符，用来数花括号。 */
function stripNoise(src) {
  const blank = (m) => ' '.repeat(m.length)
  return src
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/\/\/[^\n]*/g, blank)
    .replace(/'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`(?:[^`\\]|\\.)*`/g, blank)
}

/**
 * 抠出 main.js 里「组件注册循环」的原文。
 * 抠不出来就抛（护栏必须响），不许静默跳过。
 */
function extractRegisterLoop(src) {
  const m = LOOP_RE.exec(src)
  if (!m) throw new Error('main.js 里找不到组件注册循环（形如 for (const c of [...] {...}）')
  const varName = m[1]
  const arrStart = m.index + m[0].length - 1            // '[' 的下标
  const arrEnd = src.indexOf(']', arrStart)
  if (arrEnd < 0) throw new Error('main.js 注册循环的数组没闭合')
  const items = src.slice(arrStart + 1, arrEnd)
    .split(',').map((s) => s.trim()).filter(Boolean)
  const bodyStart = src.indexOf('{', arrEnd)
  const skel = stripNoise(src.slice(bodyStart))
  let depth = 0
  let end = -1
  for (let i = 0; i < skel.length; i++) {
    if (skel[i] === '{') depth++
    else if (skel[i] === '}') {
      depth--
      if (depth === 0) { end = i; break }
    }
  }
  if (end < 0) throw new Error('main.js 注册循环的循环体没闭合')
  return { varName, items, body: src.slice(bodyStart + 1, bodyStart + end) }
}

/** 用 main.js 的**原样注册代码**把组件装进一个 app（护栏跑的是 main.js 的代码本身）。 */
function registerLikeMain(app) {
  const { varName, items, body } = extractRegisterLoop(MAIN)
  const names = items.filter((s) => /^El[A-Z]/.test(s))
  for (const n of names) {
    if (!EP[n]) throw new Error(`main.js 要注册 ${n}，但 element-plus 没导出它`)
  }
  // eslint-disable-next-line no-new-func
  const fn = new Function('app', ...names, `for (const ${varName} of [${names.join(',')}]) {${body}\n}`)
  fn(app, ...names.map((n) => EP[n]))
  return names
}

/**
 * 每个 `<el-xxx>` 在 element-plus 里真正的根类名。
 * 绝大多数就是 kebab 化的组件名；例外是 element-plus 自己的命名：
 *   el-date-picker → el-date-editor    （日期选择器的根是 ElInput + el-date-editor）
 *   el-dialog     → el-modal-dialog    （弹层遮罩类；el-dialog 只在弹层真正打开后
 *                                       才出现在 DOM 里，SSR 阶段拿不到）
 *   el-option     → el-select-dropdown__item（选项是下拉面板里的一条 li）
 */
const ROOT_CLASS = {
  'el-date-picker': 'el-date-editor',
  'el-dialog': 'el-modal-dialog',
  'el-option': 'el-select-dropdown__item',
}

/** 有些组件必须挂在父组件里 / 需要额外 prop 才渲染得出内容。 */
const SNIPPET = {
  'el-option': '<el-select persistent :teleported="false"><el-option label="x" value="x"></el-option></el-select>',
  'el-collapse-item': '<el-collapse><el-collapse-item title="t"></el-collapse-item></el-collapse>',
}

const rootClassOf = (tag) => ROOT_CLASS[tag] || tag

/** 取出渲染结果里出现过的所有 class 名（按空白切分）。 */
function classTokens(html) {
  const out = new Set()
  for (const m of html.matchAll(/class="([^"]*)"/g)) {
    for (const t of m[1].split(/\s+/)) if (t) out.add(t)
  }
  return out
}

/** 真渲染一个标签：返回 HTML + Vue 的告警。 */
async function renderTag(tag) {
  const app = createSSRApp({ template: SNIPPET[tag] || `<${tag}></${tag}>` })
  const warns = []
  app.config.warnHandler = (msg) => { warns.push(String(msg)) }
  // element-plus 的 SSR 需要 id / zIndex 注入（否则 debugWarn 刷屏；不影响判据）
  app.provide(ID_INJECTION_KEY, { prefix: 1024, current: 0 })
  app.provide(ZINDEX_INJECTION_KEY, { current: 0 })
  provideGlobalConfig({ locale: zhCn }, app, true)
  registerLikeMain(app)
  const html = await renderToString(app)
  return { html, warns }
}

test('🔴 运行时：main.js 的注册代码真能把每个组件装进注册表（radio 族曾经一个都装不上）', () => {
  const app = createApp({ render: () => null })
  provideGlobalConfig({ locale: zhCn }, app, true)
  registerLikeMain(app)
  const registry = Object.keys(app._context.components)
  const missing = [...TAGS].map(pascal).filter((n) => !registry.includes(n))
  assert.deepEqual(missing, [], `这些组件运行时没注册 = 页面渲染不出来：${missing.join(', ')}`)
})

test('🔴 运行时：模板里每个 <el-xxx> 都能被解析并渲染出组件自己的根类名', async () => {
  for (const tag of TAGS) {
    let html
    let warns
    try {
      ({ html, warns } = await renderTag(tag))
    } catch (e) {
      assert.fail(`<${tag}> SSR 渲染直接炸了：${e.message}`)
    }
    const unresolved = warns.filter((w) => w.includes('Failed to resolve component'))
    assert.deepEqual(unresolved, [],
      `<${tag}> 没能解析成组件（main.js 导入了但没注册？）：${unresolved.join(' | ')}`)
    assert.ok(!html.includes('Failed to resolve component'),
      `<${tag}> 渲染输出里出现 "Failed to resolve component"：${html.slice(0, 300)}`)
    const cls = rootClassOf(tag)
    assert.ok(classTokens(html).has(cls),
      `<${tag}> 渲染结果里没有根类名 ${cls}：${html.slice(0, 300)}`)
  }
})
