/**
 * 「定时任务」页的**执行方式单形态**护栏（scheduler_cmd_mode_retire）。
 *
 * 旧 shell 执行形态（argv 列表 + 起子进程）**整体退役**（迭代决策）—— 页面
 * 上不许再留「二选一」的选择框：单形态还让用户选，选错的那条路后端会拒，
 * 静默失败模式不是报错，而是「保存成功了，页面上还是老的命令行」。
 *
 * 钉死的五件事：
 *   F1 **没有**执行方式选择控件，且函数引用框**常显**（不再藏在某个分支里）；
 *   F2 函数引用框在编辑态与创建态都在场，旧的命令文本域**已删除**；
 *   F3 提交时**只发 func**（旧形态字段绝不发）；
 *   F4 函数引用形状本地校验（模块:函数），后端 400 原文回显进弹框；
 *   F5 列表行对 func 形态显示「代码班车」徽标。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const SCHED = read('views/SchedulerView.vue')

/** 模板段与脚本段分开取（断言要能指明是模板缺了还是逻辑缺了）。 */
const TEMPLATE = SCHED.slice(0, SCHED.indexOf('<script setup>'))
const SCRIPT = SCHED.slice(SCHED.indexOf('<script setup>'), SCHED.indexOf('<style scoped>'))

test('F1 没有「执行方式」选择控件，函数引用框常显', () => {
  assert.doesNotMatch(TEMPLATE, /label="执行方式"/, '不该再有「执行方式」这一项')
  assert.doesNotMatch(TEMPLATE, /el-radio-group/, '不该再有二选一控件')
  assert.doesNotMatch(TEMPLATE, /value="cmd"/, '不该再有旧 shell 班车选项')
  assert.match(TEMPLATE, /v-model="form\.func"/, '函数引用框常显')
})

test('F2 函数引用框常显，旧的命令文本域已删除', () => {
  assert.doesNotMatch(TEMPLATE, /v-if="form\.mode === 'func'"/, '函数输入框不该再被分支藏起来')
  assert.match(TEMPLATE, /v-model="form\.func"/, '函数引用输入框')
  assert.doesNotMatch(TEMPLATE, /v-model="form\.cmdText"/, '命令文本域已删除')
  assert.match(TEMPLATE, /placeholder="keepsake\.buses\.distill:run"/, '给一个函数引用例子')
})

test('F3 提交时只发 func（旧形态字段绝不发）', () => {
  assert.doesNotMatch(SCRIPT, /f\.mode/, '执行方式模式变量已不存在')
  assert.doesNotMatch(SCRIPT, /parseCmd/, '旧形态的解析函数已删除')
  const body = SCRIPT.slice(SCRIPT.indexOf('async function saveForm'))
  assert.match(body, /payload\.func = fn/, '只发 func')
  assert.doesNotMatch(body, /payload\.cmd/, '绝不发旧形态字段')
})

test('F4 函数引用本地校验 + 后端报错回显', () => {
  assert.match(SCRIPT, /const FUNC_RE = \/\^\[A-Za-z_\]/, '有函数引用形状的正则')
  assert.match(SCRIPT, /if \(!FUNC_RE\.test\(fn\)\) \{/, '形状不对就地拦下')
  assert.match(SCRIPT, /formError\.value = e\.message/, '后端 400 原文回显进弹框')
  assert.match(TEMPLATE, /v-if="formError"/, '错误要真的显示出来')
})

test('F5 列表行对 func 形态显示「代码班车」徽标', () => {
  assert.match(TEMPLATE, /v-if="row\.func"[^>]*class="chip[^"]*"/, '徽标按 row.func 分流')
  assert.match(TEMPLATE, /代码班车/, '徽标文案是「代码班车」')
})
