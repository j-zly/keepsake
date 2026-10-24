/**
 * 「AI 配置」页的前端护栏（纯静态扫描，node --test 直接跑）。
 *
 * 盯的是**接线**与**红线**（盯不住的：后端返回什么形状 —— 那由
 * tests/test_settings_ai.py 钉）：
 *   1) **五张卡**：五节各一张；后两张（query_expansion / tiering_review）
 *      没有独立配置 ⇒ 只读说明卡，**不摆编辑入口**（不许出现保存/检测按钮）；
 *   2) **secret 字段**：只显示「已设置 / 未设置」徽标 + 路径，
 *      **凭据真值永不回显**（输入框是空的，留空 = 不改）；
 *   3) **每卡一个保存 + 一个检测**：保存走既有重确认件（ConfirmDialog），
 *      检测即时调 probe，结果落在卡内状态行（绿点 = 通 / 红字 = 失败摘要）；
 *   4) **数字零写死**：输入框上下限照抄后端给的 min/max，前端不另抄一份；
 *   5) **不引新依赖 / 不用原生弹框**：提示走 utils/dialog。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AiSettingsView.vue')
const API = read('api/index.js')
const ROUTER = read('router/index.js')
const APP = read('App.vue')

// ---------------------------------------------------------------------------
// 接线：路由 + 侧栏 + api 方法
// ---------------------------------------------------------------------------

test('路由与侧栏都接上了「AI 配置」', () => {
  assert.match(ROUTER, /import AiSettingsView from '\.\.\/views\/AiSettingsView\.vue'/)
  assert.match(ROUTER, /path: '\/ai-settings'.*AiSettingsView/)
  assert.match(APP, /\{ path: '\/ai-settings', label: 'AI 配置'/)
})

test('api/index.js 三条方法都在，且都走 http 实例（不硬编码 fetch）', () => {
  assert.match(API, /aiSettings: \(\) => http\.get\('\/api\/v1\/settings\/ai'\)/)
  assert.match(API, /setAiSettings: \(sectionId, fields\) =>/)
  assert.match(API, /probeAiSettings: \(sectionId\) =>/)
  // confirm 恒为 true（后端要显式二次确认）
  assert.match(API, /confirm: true/)
  assert.doesNotMatch(VIEW, /\bfetch\(/)
})

// ---------------------------------------------------------------------------
// 五张卡
// ---------------------------------------------------------------------------

test('五张卡：每节一张（v-for 遍历后端给的 sections）', () => {
  assert.match(VIEW, /v-for="card in cards"/)
  assert.match(VIEW, /function buildCards\(sections\)/)
  assert.match(VIEW, /cards\.value = buildCards\(data\.sections\)/)
})

test('后两张只读卡：不摆编辑入口（保存/检测按钮都挂在 v-if="card.editable" 上）', () => {
  // 卡底的按钮区整体挂在 editable 上 —— 只读卡连按钮都不渲染
  assert.match(VIEW, /<div v-if="card\.editable" class="ops">/)
  // 只读卡有明确的说明文案（指向 llm 节）
  assert.match(VIEW, /这一节没有独立配置/)
  // 编辑入口只有一处保存 + 一处检测（不许每张字段一个按钮）
  assert.equal((VIEW.match(/@click="askSave\(card\)"/g) || []).length, 1)
  assert.equal((VIEW.match(/@click="probe\(card\)"/g) || []).length, 1)
})

test('enabled 开关在卡头（screening 是真开关，其余是派生状态）', () => {
  assert.match(VIEW, /<el-switch v-if="card\.enabled_editable" v-model="card\.draft\.enabled" \/>/)
  assert.match(VIEW, /开关由配置派生/)
})

// ---------------------------------------------------------------------------
// secret 字段
// ---------------------------------------------------------------------------

test('secret 字段：只显示徽标 + 路径，凭据真值永不回显', () => {
  // 两种 secret 形态各有分支
  assert.match(VIEW, /f\.type === 'secret_path'/)
  assert.match(VIEW, /f\.type === 'secret_value'/)
  // 徽标文案
  assert.match(VIEW, /'已设置' : '未设置'/)
  // 路径型显示路径
  assert.match(VIEW, /\{\{ f\.value\.path \}\}/)
  // 内联凭据的输入框是空的（留空 = 不改），且是密码框
  assert.match(VIEW, /v-model="card\.secret\[f\.key\]"/)
  assert.match(VIEW, /type="password"/)
  // 🔴 页面里不许出现「把凭据回显出来」的写法
  assert.doesNotMatch(VIEW, /f\.value\.key|f\.value\.api_key/)
})

// ---------------------------------------------------------------------------
// 保存 / 检测
// ---------------------------------------------------------------------------

test('保存走既有重确认件（ConfirmDialog），检测即时调 probe', () => {
  assert.match(VIEW, /<ConfirmDialog/)
  assert.match(VIEW, /@confirm="doSave"/)
  assert.match(VIEW, /closeOnClickModal|ConfirmDialog/)       // 复用组件自带禁遮罩关闭
  assert.match(VIEW, /api\.setAiSettings\(card\.id, payload\)/)
  assert.match(VIEW, /api\.probeAiSettings\(card\.id\)/)
  // 探测中按钮禁用 + 转圈
  assert.match(VIEW, /:loading="card\.probing" :disabled="card\.probing"/)
})

test('检测结果落在卡内状态行：通 = 绿点 + 细节，失败 = 红字 + 原因摘要', () => {
  assert.match(VIEW, /class="src ai-status"/)
  assert.match(VIEW, /card\.status\.tone === 'ok'/)
  assert.match(VIEW, /'ai-status--err'/)
  assert.match(VIEW, /finish_reason=/)          // llm 的细节
  assert.match(VIEW, /维度 \$\{r\.dimension\}/)  // embedder 的细节
  assert.match(VIEW, /选中 \$\{r\.picked\.length\}\/\$\{r\.of\}/)  // screening 的细节
})

// ---------------------------------------------------------------------------
// 纪律
// ---------------------------------------------------------------------------

test('数字零写死：输入框上下限照抄后端给的 min/max', () => {
  assert.match(VIEW, /:min="f\.min"/)
  assert.match(VIEW, /:max="f\.max"/)
  // 前端不许自己写死上下限（带数字的 :min= / :max= 一律不许）
  assert.doesNotMatch(VIEW, /:min="\d/)
  assert.doesNotMatch(VIEW, /:max="\d/)
})

test('提示走 utils/dialog（禁原生 alert / confirm）', () => {
  assert.match(VIEW, /from '\.\.\/utils\/dialog'/)
  assert.doesNotMatch(VIEW, /\balert\(/)
  assert.doesNotMatch(VIEW, /window\.confirm\(/)
})
