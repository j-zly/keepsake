/**
 * 「定时任务」页的**真实 DOM 断言**（scheduler_view_polish2）。
 *
 * 为什么不是静态扫描：用户实拍两条问题都在**渲染结果**上——
 *   ① 编辑框打开时「函数引用」回显的值必须与后端给的原值逐字节一致（形如 `模块:函数`）；
 *   ② 弹框刚打开时「取消」键不能带 `is-disabled`（看着像禁用 = 用户不敢点）。
 * 静态扫源码钉不住这两条（源码里既没有回显后的值，也没有 class），
 * 所以这里把**真页面**（.vue 原件编译后）挂进一个迷你 DOM 里跑一遍：
 *   点列表行的「编辑」→ 弹框真打开 → 读真 input 的 value / 真 button 的 class。
 *
 * 怎么做到不装 jsdom：
 *   - 页面本体用 `vue/compiler-sfc`（vue 自带的子路径，已是声明依赖）编译成 ESM；
 *   - 确认弹框（本地 .vue）同样编译；API 层用桩件顶掉（不打后端）；
 *   - 渲染用 `createRenderer` + 几十行迷你 DOM（只实现 EP/Vue 用到的那几个 API）；
 *   - Element Plus 是**真组件**（`el-input` 的 value、`el-button` 的 class 都是它自己算的）。
 *
 * 钉死的六件事：
 *   D1 编辑框里「函数引用」input 的 value === 后端 row.func（逐字节，含冒号，无字符替换）；
 *   D2 弹框刚打开时「取消」键不是禁用态（无 is-disabled、无 disabled 属性），且点它真能关掉弹框；
 *   D3 保存 / 取消两键视觉权重点开（保存=主色，取消不许改成主色）；
 *   D4 列表铺满右缘（名称列吃弹性 + 操作列贴右缘 + 表头与行同一套模板 + 窄屏不破版）；
 *   D5 暗色下次要按钮提到可读档（平台暗色样式单点，不新增颜色字面量）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, writeFileSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from 'vue/compiler-sfc'
import { createRenderer, h, nextTick } from 'vue'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const SRC = fileURLToPath(new URL('../src/', import.meta.url))
const read = (p) => readFileSync(`${SRC}${p}`, 'utf8')

/* ---------------------------------------------------------------- 迷你 DOM
 * 只实现 EP / Vue 用到的 API：够跑出真的 class、真的 input.value。
 * 刻意不模拟布局（没有真实的 clientWidth 等）——断言与布局无关。 */
class El {
  constructor(tag) {
    this.tag = tag
    this.children = []
    this.parentNode = null
    this.style = {}
    this.attrs = {}
    this.props = {}
    this._value = ''
    this.className = ''
    this.scrollTop = 0
    this.scrollLeft = 0
    this.clientWidth = 1200
    this.clientHeight = 800
    this.offsetWidth = 100
    this.offsetHeight = 30
    this.classList = {
      add: (c) => { this.className = (this.className + ' ' + c).trim() },
      remove: (c) => { this.className = this.className.split(' ').filter((x) => x && x !== c).join(' ') },
      contains: (c) => this.className.split(' ').includes(c),
    }
  }
  get value() { return this._value }
  set value(v) { this._value = v }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c }
  insertBefore(c, ref) {
    const i = ref ? this.children.indexOf(ref) : -1
    if (i < 0) this.children.push(c)
    else this.children.splice(i, 0, c)
    c.parentNode = this
    return c
  }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentNode = null }
  remove() { if (this.parentNode) this.parentNode.removeChild(this) }
  dispatchEvent() { return true }
  get ownerDocument() { return globalThis.document }
  get parentElement() { return this.parentNode }
  setAttribute(k, v) { this.attrs[k] = String(v) }
  getAttribute(k) { return this.attrs[k] ?? null }
  removeAttribute(k) { delete this.attrs[k] }
  addEventListener() {}
  removeEventListener() {}
  focus() {}
  blur() {}
  querySelector() { return null }
  contains(node) { return node === this || this.children.some((c) => (c.contains ? c.contains(node) : false)) }
  // 注释节点（v-if 之类）不算文案，只有文本节点与元素算。
  get textContent() {
    return this.children.map((c) => c.textContent ?? (c.nodeType === 3 ? c.text : '')).join('')
  }
}

const bodyEl = new El('body')

const nodeOps = {
  createElement: (tag) => new El(tag),
  createText: (text) => ({ text, nodeType: 3 }),
  createComment: (text) => ({ text, nodeType: 8 }),
  setText: (n, text) => { n.text = text },
  setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (child, parent, anchor) => { parent.insertBefore(child, anchor) },
  remove: (child) => {
    if (typeof child.remove === 'function') child.remove()
    else if (child.parentNode) child.parentNode.removeChild(child)
  },
  parentNode: (n) => n.parentNode,
  nextSibling: (n) => {
    if (!n.parentNode) return null
    const i = n.parentNode.children.indexOf(n)
    return n.parentNode.children[i + 1] || null
  },
  querySelector: (sel) => (sel === 'body' ? bodyEl : null),
  // 静态内容（编译期拼好的 HTML 片段）：迷你 DOM 不解析 HTML，当文本节点塞进去即可
  // （断言只看元素标签 / class / value，不看静态文案）。
  insertStaticContent: (content, parent, anchor) => {
    const node = { text: content, nodeType: 3 }
    parent.insertBefore(node, anchor)
    return [node, node]
  },
  setScopeId: (el, id) => { el.scopeId = id },
  patchProp: (el, key, prev, next) => {
    if (key === 'class') el.className = Array.isArray(next) ? next.filter(Boolean).join(' ') : (next || '')
    else if (key === 'style') Object.assign(el.style, next || {})
    else if (key === 'value') el.value = next
    else { el.props[key] = next; el.attrs[key] = next }
  },
}

/** 全局浏览器垫片（EP 的锁滚动 / 焦点陷阱、axios 的 location 都要）。 */
function installGlobals() {
  globalThis.document = {
    body: bodyEl,
    documentElement: new El('html'),
    activeElement: null,
    addEventListener() {},
    removeEventListener() {},
    createElement: (t) => new El(t),
    querySelector: () => null,
    querySelectorAll: () => [],
  }
  globalThis.window = {
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    navigator: { userAgent: 'node-test' },
    innerWidth: 1600,
    innerHeight: 900,
    document: globalThis.document,
    addEventListener() {},
    removeEventListener() {},
    getComputedStyle: () => ({}),
    performance: { now: () => Date.now() },
    requestAnimationFrame: (cb) => setTimeout(cb, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
  }
  globalThis.getComputedStyle = () => ({})
  globalThis.performance = globalThis.performance ?? { now: () => Date.now() }
  globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0)
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id)
  // EP 的按钮点击会 new MouseEvent（垫片：只要类型与几个字段）
  // EP 的焦点陷阱会做 instanceof 判断（HTMLInputElement 等）
  globalThis.HTMLElement = class HTMLElement {}
  globalThis.HTMLInputElement = class HTMLInputElement extends HTMLElement {}
  globalThis.MouseEvent = class MouseEvent {
    constructor(type, init = {}) { this.type = type; Object.assign(this, init) }
  }
  globalThis.Event = globalThis.MouseEvent
}

/* ------------------------------------------------------- 编译真页面（.vue） */
const TMP = [
  `${HERE}.probe-scheduler-view.mjs`,
  `${HERE}.probe-confirm-dialog.mjs`,
  `${HERE}.probe-api.mjs`,
]

/** 把编译产物里的 import 改写到 web/tests/ 下能解析的路径。 */
function rewriteImports(code, map) {
  for (const [from, to] of Object.entries(map)) {
    assert.ok(code.includes(from), `编译产物里没有预期的 import ${from}（页面改过依赖？测试要先更新）`)
    code = code.replaceAll(from, to)
  }
  return code
}

const VIEW = read('views/SchedulerView.vue')
const CONFIRM = read('components/ConfirmDialog.vue')

/** 编译一个 .vue 成可直接 import 的 ESM（client 形态，模板内联进 setup）。 */
function compileSfc(source, filename, importMap) {
  const { descriptor, errors } = parse(source, { filename })
  assert.deepEqual(errors, [], `${filename} 解析失败`)
  const out = compileScript(descriptor, { id: filename, inlineTemplate: true })
  return rewriteImports(out.content, importMap)
}

/** 后端给的一行（回显断言的原值：模块:函数，冒号分隔）。 */
const ROW = {
  job_id: 'ks-demo',
  name: '记忆提炼',
  schedule: '30 4 * * *',
  next_run: null,
  enabled: true,
  func: 'keepsake.buses.distill:run',
  timeout: 1800,
}

/** API 桩件：不打后端，只喂一行带 func 的任务。 */
const API_STUB = `export const api = {
  crons: async () => ({ jobs: ${JSON.stringify([ROW])}, scheduler_status: { text: '' } }),
  tieringStatus: async () => null,
}
`

function writeTmp() {
  writeFileSync(TMP[1], compileSfc(CONFIRM, 'ConfirmDialog.vue', {}))
  writeFileSync(TMP[2], API_STUB)
  writeFileSync(TMP[0], compileSfc(VIEW, 'SchedulerView.vue', {
    "'../components/ConfirmDialog.vue'": "'./.probe-confirm-dialog.mjs'",
    "'../api'": "'./.probe-api.mjs'",
    "'../utils/dialog'": "'../src/utils/dialog.js'",
    "'../utils/format'": "'../src/utils/format.js'",
    "'../utils/cron'": "'../src/utils/cron.js'",
  }))
}

/* ------------------------------------------------------------ 挂载 + 交互
 * 🔴 本文件**不许有顶层 await**：tests/index.js 是用 require() 挂每个测试文件的，
 * 带 TLA 的 ESM 走 require 会直接 ERR_REQUIRE_ASYNC_MODULE（整个套件挂掉）。
 * 所以编译 / 装载都放在 boot() 里惰性做。 */
let booting = null

/** 装垫片 → 编译真页面 → 拿真组件（只做一次）。 */
function boot() {
  if (booting) return booting
  booting = (async () => {
    installGlobals()
    writeTmp()
    const [{ default: SchedulerView }, ep] = await Promise.all([
      import(new URL('./.probe-scheduler-view.mjs', import.meta.url).href),
      import('element-plus'),
    ])
    return { SchedulerView, ElementPlus: ep.default, ID_INJECTION_KEY: ep.ID_INJECTION_KEY, ZINDEX_INJECTION_KEY: ep.ZINDEX_INJECTION_KEY }
  })()
  return booting
}

const renderer = createRenderer(nodeOps)

/** 收集一棵 DOM 树里的所有节点。 */
function findAll(...roots) {
  const out = []
  const visit = (node) => {
    if (!node) return
    if (Array.isArray(node)) { node.forEach(visit); return }
    out.push(node)
    if (node.children) node.children.forEach(visit)
  }
  roots.forEach(visit)
  return out
}

/** 挂页面 → 等首屏 → 点「编辑」→ 等弹框。 */
async function openEditDialog() {
  const { SchedulerView, ElementPlus, ID_INJECTION_KEY, ZINDEX_INJECTION_KEY } = await boot()
  bodyEl.children = []
  const root = new El('div')
  const app = renderer.createApp({ render: () => h(SchedulerView) })
  app.use(ElementPlus)
  app.provide(ID_INJECTION_KEY, { prefix: 1024, current: 0 })
  app.provide(ZINDEX_INJECTION_KEY, { current: 0 })
  app.mount(root)
  await nextTick()
  // onMounted 里的 load() 是异步的（读后端拿列表），等它把行渲染出来
  await new Promise((r) => setTimeout(r, 0))
  await nextTick()

  const nodes = findAll(root, bodyEl)
  const editBtn = nodes.find((n) => n.tag === 'button' && n.textContent.trim() === '编辑')
  assert.ok(editBtn, '列表行里没有「编辑」按钮')
  editBtn.props.onClick(new MouseEvent('click'))
  await nextTick()
  await nextTick()
  return { root, app, nodes: findAll(root, bodyEl) }
}

/** 弹框里那个按钮（按文案找）。 */
const buttonByText = (nodes, text) =>
  nodes.find((n) => n.tag === 'button' && n.textContent.trim() === text)

/* ------------------------------------------------------------------ 用例 */

test('D1 编辑框里的「函数引用」回显 === 后端 row.func（逐字节，含冒号）', async () => {
  const { root, app } = await openEditDialog()
  const input = findAll(root, bodyEl)
    .find((n) => n.tag === 'input' && n.attrs.placeholder === 'keepsake.buses.distill:run')
  assert.ok(input, '编辑弹框里没有「函数引用」输入框')
  assert.equal(input.value, ROW.func, '回显值必须与后端原值逐字节一致')
  assert.ok(input.value.includes(':'), '函数引用必须是「模块:函数」（冒号分隔）')
  assert.notEqual(input.value, ROW.func.replace(':', '.'), '冒号不许被替换成点号')
  app.unmount()
})

test('D2 弹框刚打开时「取消」键不是禁用态（无 is-disabled、无 disabled），且点它真能关掉弹框', async () => {
  const { root, app, nodes } = await openEditDialog()
  const cancel = buttonByText(nodes, '取消')
  assert.ok(cancel, '编辑弹框里没有「取消」按钮')
  assert.ok(!cancel.className.includes('is-disabled'), `取消键带上了 is-disabled：${cancel.className}`)
  assert.notEqual(cancel.attrs.disabled, true, '取消键是禁用态')
  // 不是「看着能点」而已：真点一下，弹框真关（编辑弹框从 DOM 里退场）
  cancel.props.onClick(new MouseEvent('click'))
  await new Promise((r) => setTimeout(r, 50))
  await nextTick()
  // 弹框关了 = 它的遮罩层被隐藏（v-show → display:none）
  const dialog = findAll(bodyEl).find((n) => n.attrs && n.attrs['aria-label'] === '编辑定时任务')
  assert.ok(dialog, '编辑弹框不在 DOM 里')
  assert.equal(dialog.parentNode && dialog.parentNode.style.display, 'none',
    '点了「取消」弹框没关 —— 那就是真的点不动')
  app.unmount()
})

test('D3 保存 / 取消两键视觉权重点开（保存=主色，取消不许改成主色）', async () => {
  const { root, app, nodes } = await openEditDialog()
  const cancel = buttonByText(nodes, '取消')
  const save = buttonByText(nodes, '保存')
  assert.ok(cancel && save, '编辑弹框里没有「保存 / 取消」两键')
  assert.ok(save.className.includes('el-button--primary'), '保存键必须是主色')
  assert.ok(!cancel.className.includes('el-button--primary'), '取消键不许改成主色（权重要点开）')
  app.unmount()
})

test('D4 列表铺满右缘：名称列吃弹性 + 操作列贴右缘 + 表头与行同一套模板 + 窄屏不破版', () => {
  const css = VIEW.slice(VIEW.indexOf('<style scoped>'))
  // 表头与行共用同一条规则（不许各写一套，列会漂）
  assert.match(css, /\.job-head,\s*\.job-row\s*\{[^}]*grid-template-columns/, '表头与行必须共用一套列模板')
  const cols = [...css.matchAll(/grid-template-columns:\s*([^;]+);/g)].map((m) => m[1].trim())
  assert.equal(cols.length, 2, `grid-template-columns 只该有「宽屏一套 + 窄屏一套」两处（现在 ${cols.length} 处）`)
  assert.ok(cols[0].startsWith('minmax(240px, 1fr)'), '名称列必须吃弹性：minmax(240px, 1fr)')
  const rest = cols[0].slice('minmax(240px, 1fr)'.length).trim().split(/\s+/)
  assert.deepEqual(rest, ['130px', '120px', '130px', '160px', '380px'], '其余列定宽不许变')
  // 操作列贴容器右缘
  assert.match(css, /\.col-acts\s*\{[^}]*justify-self:\s*end/, '操作列要贴容器右缘（justify-self: end）')
  // 窄屏（<1000px）不出横向滚动：断点不低于 1000px，且窄屏单列
  const mq = css.match(/@media \(max-width: (\d+)px\)/)
  assert.ok(mq && Number(mq[1]) >= 1000, '窄屏断点不许低于 1000px（<1000px 必须单列，不许横滚破版）')
  assert.equal(cols[1], 'minmax(0, 1fr)', '窄屏必须是单列（minmax(0, 1fr)）')
})

test('D5 暗色下次要按钮提到可读档（平台暗色样式单点，不新增颜色字面量）', () => {
  const tokens = read('styles/tokens.css')
  const rules = [...tokens.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter(([, sel]) => sel.includes(':root[data-theme="dark"]') && sel.includes('.el-button'))
  assert.equal(rules.length, 1, '平台暗色样式里要恰好一处次要按钮的可读档规则')
  const body = rules[0][2]
  assert.match(body, /--el-button-text-color:\s*var\(--text\)/, '次要按钮文字要提到 --text 档')
  assert.match(body, /--el-button-border-color:\s*var\(--text-ghost\)/, '次要按钮描边要提到 --text-ghost 档')
  assert.doesNotMatch(body, /#[0-9a-f]{3,8}/i, '禁新增颜色字面量（只许用既有令牌）')
})

test.after(() => { for (const f of TMP) rmSync(f, { force: true }) })
