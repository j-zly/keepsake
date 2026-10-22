/**
 * 待办页 DOM 探针（2026-10-22 UI 整改 · 判据 2 的浏览器那一半）。
 *
 * 为什么是**脚本**而不是 `*.test.mjs`：它要真浏览器（Playwright + Chromium），
 * 而 `node --test tests/` 那一套是零依赖的静态扫描 —— 两者不混在一起。
 *
 * 它干什么：
 *   1) 两个视口（1280 / 1920）各截两张：页面打开态 + 弹框展开态 → /tmp/todo_ui_*.png；
 *   2) 量每一个操作按钮的 `getBoundingClientRect().height` ——
 *      **不许超过单行高**（超过 = 文字折行成两行 = 用户截图里那个「变形」）。
 *
 * 怎么跑（需要能连上控制台）：
 *   npm i -D playwright && npx playwright install chromium
 *   BASE_URL=http://127.0.0.1:5173 TOKEN=<会话 token> node tests/todo-dom-probe.mjs
 *
 * 🔴 没装 playwright 就直接退出（不假装通过）：本仓库的静态护栏在 tests/*.test.mjs 里，
 *    那套不需要浏览器也一直跑着。
 */
import { existsSync } from 'node:fs'

const BASE_URL = process.env.BASE_URL || 'http://127.0.0.1:5173'
const TOKEN = process.env.TOKEN || ''
const OUT_PREFIX = '/tmp/todo_ui_'
const VIEWPORTS = [
  { name: '1280', width: 1280, height: 900 },
  { name: '1920', width: 1920, height: 1080 },
]
/** 单行高上限（px）：40 = el-button（.el-button 默认高），32 = soft/ghost，28 = act-btn。 */
const MAX_H = { '.el-button': 40, '.soft-btn': 32, '.ghost-btn': 32, '.act-btn': 28 }

let chromium
try {
  ;({ chromium } = await import('playwright'))
} catch {
  console.error('SKIP：本机没有 playwright（`npm i -D playwright && npx playwright install chromium`）')
  process.exit(2)
}

const browser = await chromium.launch()
const problems = []
const shots = []

for (const vp of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: vp.width, height: vp.height } })
  const page = await ctx.newPage()
  if (TOKEN) {
    await page.addInitScript((t) => window.localStorage.setItem('keepsake.console.token', t), TOKEN)
  }
  await page.goto(`${BASE_URL}/#/todos`, { waitUntil: 'networkidle' })
  await page.waitForSelector('.page-title', { timeout: 15000 })

  // ① 页面打开态：默认**不该**有裸表单（新增在弹框里）
  const bare = await page.locator('text=加一条').count()
  if (bare > 0) problems.push(`${vp.name}: 页面打开态仍有「加一条」裸表单按钮`)
  await page.screenshot({ path: `${OUT_PREFIX}${vp.name}_open.png`, fullPage: false })
  shots.push(`${OUT_PREFIX}${vp.name}_open.png`)

  // ② 弹框展开态：点「新待办」开框
  await page.getByRole('button', { name: /新待办/ }).click()
  await page.waitForSelector('.el-dialog', { state: 'visible', timeout: 10000 })
  await page.screenshot({ path: `${OUT_PREFIX}${vp.name}_dialog.png`, fullPage: false })
  shots.push(`${OUT_PREFIX}${vp.name}_dialog.png`)

  // ③ 按钮不许折字：高度不许超过单行高
  const heights = await page.evaluate((max) => {
    const out = []
    for (const [sel, limit] of Object.entries(max)) {
      for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect()
        if (r.height > limit) {
          out.push({ sel, text: (el.textContent || '').trim().slice(0, 20), height: r.height, limit })
        }
      }
    }
    return out
  }, MAX_H)
  for (const h of heights) {
    problems.push(`${vp.name}: 按钮「${h.text}」高 ${h.height}px > 单行高 ${h.limit}px（折行了）`)
  }

  await ctx.close()
}

await browser.close()

console.log(JSON.stringify({ shots, problems }, null, 2))
if (!shots.every((s) => existsSync(s))) {
  console.error('截图没落盘')
  process.exit(1)
}
process.exit(problems.length ? 1 : 0)
