/**
 * 晋升候选「复核进度」的前端形状（2026-11-14 用户："显示一下进度，已经被命中多少次了"）。
 * 只用朴素字符串判断（正则转义在这个仓里绊过我三次，教训已入技能）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(path.join(HERE, '..', 'src', rel), 'utf-8')
const RV = read('views/ReviewView.vue')
const API = read('api/index.js')

const has = (s, t, m) => assert.ok(s.includes(t), m || `缺少：${t}`)
const no = (s, t, m) => assert.ok(!s.includes(t), m || `不该出现：${t}`)

test('进度条只在「晋升候选」方向画，退居建议那侧不打这个请求', () => {
  has(RV, `<div v-if="candDir === 'promote'" class="prog">`, '卡内块只给晋升方向')
  const at = RV.indexOf('async function loadProg')
  const body = RV.slice(at, at + 900)
  has(body, "if (candDir.value !== 'promote') return", '退居方向直接返回（不白打接口）')
})

test('🔴 阈值与天数一个都不写在界面里（判据漂了页面必须跟着后端漂）', () => {
  no(RV, '5000', '界面里不许出现批准阈值字面量')
  no(RV, '3000', '界面里不许出现提名阈值/基数字面量')
  has(RV, '{{ progOf(row.key).hits }} / {{ progApproveHits }}', '分母来自接口')
  has(RV, '近 ${p.window_days} 天', '窗口天数来自接口')
  has(RV, '复核 ${p.review_at}', '复核日期来自接口（不是前端 +7 天算的）')
})

test('人工提名不画进度条（没有自动复核这回事，画了就是骗人）', () => {
  const at = RV.indexOf('<div v-if="candDir === \'promote\'" class="prog">')
  const block = RV.slice(at, at + 1200)
  has(block, `progOf(row.key).source === 'auto'`, '条与百分比只在自动提名时出现')
  no(block.slice(block.indexOf('prog-bar')), "v-if=\"progOf(row.key).source === 'manual'\"",
    '不该给人工提名另画一条假进度')
})

test('预告四种状态都有文案，且不许出现空白态', () => {
  has(RV, "const PROG_VERDICT = { waiting: '未满窗', approve: '到线', reject: '会驳回', manual: '人工' }",
    '四个 verdict 全覆盖（后端加第五个就会红在这里）')
  has(RV, "return PROG_VERDICT[p.verdict] || p.verdict", '认不出的值原样显示，不吞')
  has(RV, "'进度不可用'", '取数失败要说话，不许永远转"读取中"')
})

test('一页一次批量取数 + 防串（翻页后旧响应不许盖新页）', () => {
  const at = RV.indexOf('async function loadProg')
  const body = RV.slice(at, at + 1100)
  has(body, 'api.candidatesProgress(keys)', '一次批量，不逐卡问')
  has(body, 'const seq = candSeq', '取请求号')
  has(body, 'if (seq !== candSeq) return', '过期响应丢弃（出现两次：成功与失败路径都要挡）')
  assert.equal((body.match(/if \(seq !== candSeq\) return/g) || []).length, 2,
    '失败分支也要挡串，否则旧页的错误会把新页的进度清掉')
  has(RV, '    loadProg(data.items)', '与列表同一节奏拉（不 await，列表先出）')
})

test('自动复核关着时必须看得见（"会驳回"这类预告才有意义）', () => {
  has(RV, 'const progEnabled = ref(true)', '状态位存在')
  has(RV, `v-if="candDir === 'promote' && !progEnabled"`, '只在关着时出现，正常态不占地方')
  has(RV, '自动复核 关', '状态文案')
})

test('api 层：URL 与后端一致，参数是逗号串', () => {
  has(API, "'/api/v1/tiering/candidates-progress'", '路径逐字对上路由')
  has(API, "{ keys: (keys || []).join(',') }", '数组转逗号串在 api 层做，视图不操心')
})
