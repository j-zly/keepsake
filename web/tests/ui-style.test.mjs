/**
 * 控制台视觉规范的自检（纯静态扫描，node --test 直接跑）：
 * 盯三件「改了看不出来、但会慢慢烂掉」的事 ——
 *   1) 圆角五档（20/16/12/10/6）必须还在 tokens.css 里；
 *   2) 颜色不许散落到组件里（全 src 扫 #hex / rgba()，排除 tokens.css
 *      与 utils/chart-theme.js —— 后者是**任务书指定的唯一图表色值出处**：
 *      ECharts 画在 canvas 上读不到 CSS 变量，必须有一份十六进制副本，
 *      那就集中放在这一个文件里，别处引用。见 tests/chart-ui.test.mjs）；
 *   3) 术语：界面里不再出现「碎片」。
 * 注意 (2) 会把 el-table 的 #default 插槽也算成颜色 —— 列表已全部改成卡片网格，
 * 哪天谁又插回一张表格，这里先红。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const TOKENS = join(SRC, 'styles/tokens.css')

/**
 * 递归列出 src 下所有 .vue / .js。
 *
 * 两处豁免：
 *   * `tokens.css` —— 令牌表本身（色值的家）；
 *   * `utils/chart-theme.js` —— **唯一**的图表色值出处（canvas 读不到 CSS 变量，
 *     必须有一份十六进制副本；散在图表配置里的色值才是违规，见 chart-ui.test.mjs）。
 */
const CHART_THEME = join(SRC, 'utils', 'chart-theme.js')

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(vue|js)$/.test(name) && p !== TOKENS && p !== CHART_THEME) out.push(p)
  }
  return out
}
const FILES = walk(SRC)
const CSS = readFileSync(TOKENS, 'utf8')

test('tokens.css：圆角五档齐全（容器20 > 卡片16 > 内嵌12 > 控件10 > chip6）', () => {
  // 档位写在 border-radius 那一行的行尾注释里，所以按「同一行」匹配
  for (const r of ['20', '16', '12', '10', '6']) {
    assert.match(CSS, new RegExp(`border-radius:[^\\n]*\\b${r}\\b`), `圆角 ${r} 这一档不见了`)
  }
})

test('颜色不散落：src 里除 tokens.css 与 chart-theme.js 外没有硬编码色值', () => {
  const bad = []
  for (const f of FILES) {
    for (const [i, line] of readFileSync(f, 'utf8').split('\n').entries()) {
      if (/#[0-9a-fA-F]{3,8}\b|rgba?\(/.test(line)) bad.push(`${relative(SRC, f)}:${i + 1} ${line.trim()}`)
    }
  }
  assert.deepEqual(bad, [], '色值必须回到 tokens.css')
})

test('术语：界面里不再出现「碎片」', () => {
  for (const f of FILES) {
    assert.ok(!readFileSync(f, 'utf8').includes('碎片'), `${relative(SRC, f)} 里还有「碎片」`)
  }
})

test('列表走卡片网格：.card-grid 在 tokens.css 且三档断点都在', () => {
  assert.match(CSS, /\.card-grid\s*\{[^}]*grid-template-columns/)
  assert.match(CSS, /@media \(max-width: 1200px\)[\s\S]{0,120}card-grid/)
  assert.match(CSS, /@media \(max-width: 900px\)[\s\S]{0,120}card-grid/)
})

test('要读的元素不用装饰色：.card-time 挂 --text-faint 而不是 --text-ghost', () => {
  // --text-ghost 白底只有 2.80:1，是纯装饰/占位色。时间是要读的，掉了就看不清。
  const rule = CSS.match(/\.card-time\s*\{([^}]*)\}/)
  assert.ok(rule, '.card-time 规则不见了')
  const color = rule[1].match(/color:\s*var\((--[\w-]+)\)/)
  assert.ok(color, '.card-time 的 color 必须走令牌')
  assert.notEqual(color[1], '--text-ghost', `.card-time 是要读的时间，不能用装饰色 ${color[1]}`)
  assert.equal(color[1], '--text-faint')
})

test('同排卡片等高：grid stretch + .card height:100% + .card-foot 压底三件套都在', () => {
  assert.match(CSS, /\.card-grid\s*\{[^}]*align-items:\s*stretch/, '.card-grid 必须 stretch')
  assert.match(CSS, /\.card\s*\{[^}]*height:\s*100%/, '.card 必须 height:100% 撑满行高')
  assert.match(CSS, /\.card\s*\{[^}]*flex-direction:\s*column/, '.card 仍是 flex column')
  assert.match(CSS, /\.card-foot\s*\{[^}]*margin-top:\s*auto/, '.card-foot 必须靠 margin-top:auto 压底')
  // 卡内不许 align-self:start —— 那等于把 stretch 又关掉，等高会静默退化
  assert.ok(
    !/align-self:\s*start/.test(CSS.match(/\.card\s*\{([^}]*)\}/)[1]),
    '.card 内不许 align-self:start，会破坏等高'
  )
})
