/**
 * 「定时任务」页的**执行方式双模式**护栏（bus_inprocess_pure_code）。
 *
 * 班车从「壳脚本起子进程」换成「同进程调函数（func 形态）」后，编辑面必须
 * 能两种方式都编辑 —— 静默失败模式不是报错，而是「保存成功了，页面上还是
 * 老的命令行」「切了模式，提交的还是另一种字段」。
 *
 * 钉死的五件事：
 *   F1 编辑框有「执行方式」二选一（代码班车 / 旧 shell 班车）；
 *   F2 代码班车模式编辑 `模块:函数` 字符串，旧模式编辑 cmd 文本域（原样保留）；
 *   F3 提交时**只发当前那一种**执行方式（两个都发后端会拒）；
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

test('F1 编辑框有「执行方式」二选一（代码班车 / 旧 shell 班车）', () => {
  assert.match(TEMPLATE, /label="执行方式"/, '要有「执行方式」这一项')
  assert.match(TEMPLATE, /<el-radio-group v-model="form\.mode">/, '二选一绑 form.mode')
  assert.match(TEMPLATE, /<el-radio value="func">代码班车/, '代码班车选项')
  assert.match(TEMPLATE, /<el-radio value="cmd">旧 shell 班车/, '旧 shell 班车选项')
})

test('F2 两种模式各编辑各的字段（cmd 文本域原样保留）', () => {
  assert.match(TEMPLATE, /v-if="form\.mode === 'func'"/, '函数输入框只在代码班车模式出现')
  assert.match(TEMPLATE, /v-model="form\.func"/, '函数引用输入框')
  assert.match(TEMPLATE, /v-model="form\.cmdText" type="textarea"/, 'cmd 文本域原样保留')
  assert.match(TEMPLATE, /placeholder="keepsake\.buses\.distill:run"/, '给一个函数引用例子')
})

test('F3 提交时只发当前那一种执行方式', () => {
  assert.match(SCRIPT, /if \(f\.mode === 'func'\) \{/, '按模式分支组装 payload')
  const body = SCRIPT.slice(SCRIPT.indexOf('async function saveForm'))
  assert.match(body, /payload\.func = fn/, '代码班车模式发 func')
  assert.match(body, /payload\.cmd = parseCmd\(f\.cmdText\)/, 'shell 模式发 cmd')
  // 两个都发 ⇒ 后端「二选一」闸门会拒 ⇒ 前端不许同时带上
  const funcIdx = body.indexOf('payload.func = fn')
  const cmdIdx = body.indexOf('payload.cmd = parseCmd')
  assert.ok(funcIdx > 0 && cmdIdx > 0)
  assert.match(body, /if \(f\.mode === 'func'\) \{[\s\S]*?payload\.func = fn[\s\S]*?\} else \{/,
    'func 与 cmd 必须是**互斥**的两条分支（不许同时发）')
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
