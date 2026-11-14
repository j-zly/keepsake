/**
 * 凭据功能授权（scope）的前端形状 —— 2026-11-14 用户口径：
 * 「只授权记忆的话知识库是不能使用，只授权知识库的话记忆功能不能使用」。
 * 只用字符串包含判断（上一版正则转义把自己绊了三次，教训记在技能里）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const read = (rel) => readFileSync(path.join(HERE, '..', 'src', rel), 'utf-8')
const CLD = read('components/CredentialListDrawer.vue')
const API = read('api/index.js')
const MAIN = read('main.js')

const has = (s, t, m) => assert.ok(s.includes(t), m || `缺少：${t}`)
const no = (s, t, m) => assert.ok(!s.includes(t), m || `不该出现：${t}`)

test('清单每行都能勾「记忆 / 知识库」，改完立刻提交', () => {
  has(CLD, '<el-checkbox value="memory" label="记忆" />', '记忆档位')
  has(CLD, '<el-checkbox value="kb" label="知识库" />', '知识库档位')
  has(CLD, '@change="saveScopes(r)"', '勾上就提交（不额外要求点保存）')
  has(CLD, 'api.setCredentialScopes(', '走 api 层，不手写 URL')
  // element-plus 按需注册：漏一个组件，模板会退化成裸文字连排（radio 族的生产事故）
  has(MAIN, 'ElCheckboxGroup,', 'main.js 导入里有 ElCheckboxGroup')
  assert.ok(MAIN.split('ElCheckboxGroup').length - 1 >= 2,
    '导入清单与注册循环两处都要有（少一处就是运行时解析不到组件）')
  has(MAIN, "import 'element-plus/theme-chalk/el-checkbox-group.css'", '组件样式也要单独引')
})

test('档位值必须与后端词汇表一致（这里漂了就是"勾了个不被认识的档"）', () => {
  const values = [...CLD.matchAll(/<el-checkbox value="([^"]+)"/g)].map((m) => m[1])
  assert.deepEqual(values.sort(), ['kb', 'memory'],
    '前端的档位必须正好是后端的 SCOPES_ALL（memory / kb）')
})

test('授权是行内动作，不再另开一个弹框（同一件事两个入口迟早漂）', () => {
  no(CLD, 'scopesOpen', '不该有独立的授权弹框开关')
  no(CLD, 'askFromEdit', '不该绕回编辑弹框的确认流程')
})

test('保存失败必须回滚勾选态并说人话', () => {
  const at = CLD.indexOf('async function saveScopes')
  const body = CLD.slice(at, at + 1400)
  has(body, 'r._scopes = [...(r.scopes || [])]', '失败回滚勾选（留着勾上的样子就是骗人）')
  has(body, '_scopesError', '失败原因就地显示在这一行')
  has(body, 'showError(', '同时给红条')
  // 只有真变了才发请求（否则每次点一下都是一条无意义审计行）
  has(body, "if (next.join(',') === prev.join(',')) return", '无变化不发请求')
})

test('api 层带意图位，用户不输入任何东西', () => {
  has(API, 'setCredentialScopes: (agentId, credId, scopes) =>', 'api 方法在')
  has(API, '{ scopes, confirm: true }', '意图位由代码自动带')
})
