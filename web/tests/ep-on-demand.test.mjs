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
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

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
