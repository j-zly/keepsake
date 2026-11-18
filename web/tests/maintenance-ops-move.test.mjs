/**
 * 「维护班车手动执行」搬回「定时任务」页（maintenance_ops_move_to_scheduler）。
 *
 * 背景（用户铁律，11-18 暴怒重申）：**班车的一切入口（调度 + 手动执行 + 预演）
 * 只准存在于「定时任务」页**。违反史两段：11-09 看板塞调度块（已删）；
 * 11-16 把「维护班车 · 手动执行」面板从定时任务页迁回维护页 —— 本单纠正这步。
 *
 * 钉死的四件事：
 *   M1 维护页整块手动执行面板摘净（零「手动执行 / 预演 / 实跑」字样）；
 *   M2 维护页的三个**展示** section（常驻层水位 / 退居提名 / 建议废弃的待办）零接触；
 *   M3 维护页顶部与 StatCard 的口径改口：执行入口在「定时任务」页（不许还说「在本页下面」）；
 *   M4 定时任务页的预演入口：白名单**单源** + 只对命中行渲染 + 只走 dry-run（零实跑）。
 *
 * 静态扫描够用的部分放这里；**渲染结果**（按钮真的只出现在白名单行）由
 * scheduler-view-dom.test.mjs 的 D9 用真 DOM 钉住。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const MAINT = read('views/MaintenanceView.vue')
const SCHED = read('views/SchedulerView.vue')

/** 模板段（<template> 到 </template>）—— 文案断言只看模板里的。 */
const templateOf = (src) => src.slice(src.indexOf('<template>'), src.indexOf('</template>'))

/** 剥掉注释再查红线 —— 否则源码里自己的注释就能把护栏顶红。 */
const noComment = (s) => s
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

test('M1 维护页：手动执行面板摘净（模板与脚本零「手动执行 / 预演 / 实跑」）', () => {
  const tpl = templateOf(MAINT)
  for (const s of ['手动执行', '预演', '实跑', 'runMerge', 'runForget', 'askOps', 'opResult']) {
    assert.ok(!tpl.includes(s), `维护页模板里还留着「${s}」`)
  }
  // 整份文件也不许有（G4 闸：grep -c "手动执行\|· 实跑\|预演" = 0）
  for (const s of ['手动执行', '预演', '实跑']) {
    assert.ok(!MAINT.includes(s), `MaintenanceView 里还留着「${s}」—— 手动执行面板已搬到「定时任务」页`)
  }
  // 面板本体（四按钮 + 确认框 + 上次执行时间）连根拔：连类名都不许剩
  for (const s of ['ops-grid', 'ops-card', 'ops-head', 'ops-meta', 'ops-group', 'ops-label',
    'maintLastRun', 'loadMaintLastRun', 'isMaintJob']) {
    assert.ok(!MAINT.includes(s), `MaintenanceView 里还留着「${s}」的残骸`)
  }
  // 确认框组件只服务那个面板 ⇒ 一起摘（页面里不该再挂 ConfirmDialog）
  assert.ok(!MAINT.includes('ConfirmDialog'), '维护页不该再挂 ConfirmDialog（实跑面板已摘）')
})

test('M2 维护页：三个展示 section 零接触（水位 / 退居提名 / 建议废弃的待办）', () => {
  for (const s of ['常驻层水位 · 按代理', '退居提名（班车建议退出常驻层）', '建议废弃的待办（到期过 7 天仍未销）']) {
    assert.ok(MAINT.includes(s), `维护页少了「${s}」—— 展示面不许动`)
  }
  // 水位的读数口径与分列也还在
  for (const s of ['api.corePerAgent()', 'agentShares', 'DEMOTE_CANDIDATE_TAG', 'PagerBar']) {
    assert.ok(MAINT.includes(s), `维护页少了「${s}」`)
  }
})

test('M3 维护页：口径改口 —— 执行入口在「定时任务」页', () => {
  const tpl = templateOf(MAINT)
  assert.match(tpl, /合并 \/ 遗忘的执行入口在「定时任务」页/, '顶部说明要指向「定时任务」页')
  assert.ok(!tpl.includes('在本页下面'), '不许还说「手动执行在本页下面」')
  // 顶部与两处 StatCard 提示同一口径（三处都在模板里）
  assert.equal((tpl.match(/合并 \/ 遗忘的执行入口在「定时任务」页/g) || []).length, 3,
    '顶部 1 处 + StatCard 2 处 = 3 处同一口径')
  // 维护页仍留一句指路链接（不许让人以为功能没了）
  assert.match(MAINT, /to="\/scheduler"/, '维护页要有指向「定时任务」页的链接')
  // AI 配置页那行迁移指引保留（它不是本页面板）
  assert.match(MAINT, /to="\/ai-settings"/, 'AI 配置页的迁移指引保留')
})

test('M4 定时任务页：预演入口 = 白名单单源 + 只对命中行渲染 + 只走 dry-run', () => {
  // 白名单单源（视图常量，别处不许再抄一份）
  assert.equal((SCHED.match(/const DRYRUN_BUS_FUNCS/g) || []).length, 1, '白名单必须单源')
  const block = SCHED.slice(SCHED.indexOf('const DRYRUN_BUS_FUNCS'))
  const list = block.slice(0, block.indexOf('}'))
  for (const f of ['keepsake.buses.maintenance:run', 'keepsake.buses.distill:run']) {
    assert.ok(list.includes(f), `白名单少了「${f}」`)
  }
  // 只对命中白名单的行渲染（跟着那一行走，不许全员都有）
  assert.match(SCHED, /function canDryRun\(row\)/, '要有 canDryRun 这把尺子')
  assert.match(SCHED, /Object\.keys\(DRYRUN_BUS_FUNCS\)\.includes\(/, 'canDryRun 必须按白名单判')
  assert.match(SCHED, /v-if="canDryRun\(row\)"[^>]*@click="openDryRun\(row\)"/, '「预演」按钮跟着白名单走')
  // 按钮在「运行一次」旁边（次序钉住：运行一次 → 预演 → 执行记录）
  const acts = SCHED.slice(SCHED.indexOf('<span class="col-acts">'), SCHED.indexOf('</span>', SCHED.indexOf('openRuns(row)')))
  const order = ['askTrigger(row)', 'openDryRun(row)', 'openRuns(row)'].map((s) => acts.indexOf(s))
  assert.ok(order.every((i) => i >= 0), '操作列要有「运行一次 / 预演 / 执行记录」三个动作')
  assert.ok(order[0] < order[1] && order[1] < order[2], '「预演」要落在「运行一次」之后、「执行记录」之前')
  // 只走 dry-run（零写入）：后端端点零新增，实跑不在本页
  assert.match(SCHED, /api\.merge\(\{ dry_run: true \}\)/, '合并只走 dry-run')
  assert.match(SCHED, /api\.forget\(\{ dry_run: true \}\)/, '遗忘只走 dry-run')
  assert.doesNotMatch(noComment(SCHED), /confirm:\s*true/, '本页不许有实跑位（confirm:true）')
  assert.doesNotMatch(SCHED, /合并 · 实跑|遗忘 · 实跑/, '本页不许有实跑按钮')
  // 结果展示沿用维护页的形状：一块 <pre> JSON（全局 .pre 样式），不新造组件
  assert.match(SCHED, /<pre[^>]*class="pre"/, '结果用现成的 <pre> JSON 块')
})
