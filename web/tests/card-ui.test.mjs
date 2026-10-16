/**
 * 卡片操作区/勾选行版式护栏（keepsake_card_ui_fix，纯静态扫描）：
 * 用户实拍「UI 乱了」的三类毛病都是**编译能过、窄卡才炸**的样式退化，
 * 钉成断言，免得哪天有人改样式时无声还原：
 *   1) 窄卡（网格 3~5 列）下按钮必须 nowrap，放不下整组换行 —— 绝不能
 *      逐字折成「升常/驻」竖排；
 *   2) 勾选框只能在 key 行行尾：占最左会把整条 key 右推，卡内左基准线与
 *      正文/chip/按钮不齐；
 *   3) 两个列表页的选择/批量接线关键字计数不许降 —— 本次只动版式，
 *      行为逻辑（跨页 Set、清空、批量端点）一个字都不许顺手改。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const CARD = read('components/ItemCard.vue')
const FRAG = read('views/FragmentsView.vue')
const CORE = read('views/CoreView.vue')
const PAGES = [['FragmentsView', FRAG], ['CoreView', CORE]]

/** 取出某条 CSS 规则（含选择器行）的正文 */
function rule(src, selector) {
  const i = src.indexOf(selector)
  assert.notEqual(i, -1, `ItemCard.vue 里找不到规则 ${selector}`)
  return src.slice(i, src.indexOf('}', i))
}

test('操作按钮不折字：act-btn/card-link nowrap + card-acts 整组换行', () => {
  for (const sel of [':deep(.act-btn)', '.card-link']) {
    assert.match(rule(CARD, sel), /white-space:\s*nowrap/, `${sel} 必须 nowrap，否则窄卡下逐字折行`)
    assert.match(rule(CARD, sel), /flex:\s*none/, `${sel} 必须 flex:none，不能被 flex 压扁`)
  }
  const acts = rule(CARD, '.card-acts')
  assert.match(acts, /flex-wrap:\s*wrap/, '.card-acts 必须能整组换到第二行')
  assert.match(acts, /justify-content:\s*flex-end/, '.card-acts 右对齐，别散开')
})

test('勾选框在 key 行行尾，且不吃自带外边距', () => {
  for (const [name, src] of PAGES) {
    const keyAt = src.indexOf('<span class="cell-key">')
    const pickAt = src.indexOf('class="pick"')
    assert.ok(keyAt > -1 && pickAt > -1, `${name} 找不到 cell-key / pick`)
    assert.ok(keyAt < pickAt, `${name}:勾选框必须在 key 之后（占最左会右推整条 key）`)
    assert.match(src, /\.pick \{\s*flex: none;/, `${name} 勾选框必须 flex:none`)
    assert.match(src, /\.pick \{\s*flex: none;\s*margin: 0;/, `${name} 勾选框必须清掉自带外边距`)
  }
})

test('两个列表页行为接线零改动（关键字命中数不许降）', () => {
  // 基线 = 改版式之前的逐字命中数；这次只动版式，这些数一个都不该掉
  const BASE = {
    FragmentsView: ['ops/bulk', '<SelectionBar', 'toggle(row.key)', 'isOn(row.key)', 'function toggle'],
    CoreView: ['ops/bulk', '<SelectionBar', 'toggle(row.key)', 'isOn(row.key)', 'function toggle'],
  }
  for (const [name, src] of PAGES) {
    for (const kw of BASE[name]) {
      assert.equal(
        src.split(kw).length - 1,
        1,
        `${name} 的 ${kw} 命中数掉了（基线 1）—— 行为逻辑不许跟着版式一起改`,
      )
    }
  }
})
