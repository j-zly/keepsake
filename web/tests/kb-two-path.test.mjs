/**
 * P10 的两条护栏：知识库检索是**两路并行**（文档按标题 + 切片按正文），一次输入两处结果。
 *   1) 两路计数合并显示 —— 「文档 N 条 · 切片 M 条」，N/M 来自各自接口的 count，且任一有结果就算找到；
 *   2) 过期响应被丢弃 —— 快速改词时旧响应绝不覆盖新结果。
 * 都是「跑起来才发现」的事（计数写死、串响应），所以钉在纯函数 + 源码上。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { hasAnyHit, isStale, mergeCounts } from '../src/utils/kb.js'

const KB = readFileSync(fileURLToPath(new URL('../src/views/KbView.vue', import.meta.url)), 'utf8')

test('两路计数合并成一行动显示：N/M 来自各自 count，任一为 0 也不写死成 20', () => {
  assert.equal(mergeCounts(1, 5), '文档 1 条 · 切片 5 条')
  assert.equal(mergeCounts(25, 0), '文档 25 条 · 切片 0 条')
  assert.equal(mergeCounts(0, 0), '文档 0 条 · 切片 0 条')
  // 后端没给 count（undefined）时退 0，别渲染成 NaN/undefined
  assert.equal(mergeCounts(undefined, undefined), '文档 0 条 · 切片 0 条')

  // 「任一有结果就算找得到」：文档命中或切片命中任一不为 0 就算数
  assert.equal(hasAnyHit(1, 0), true)
  assert.equal(hasAnyHit(0, 5), true)
  assert.equal(hasAnyHit(0, 0), false)
  assert.equal(hasAnyHit(undefined, undefined), false)

  // 页面用的是这个合并函数 + 这个判定，空态才只在两路都为 0 时出现
  assert.match(KB, /\{\{ mergeCounts\(docCount, hitCount\) \}\}/)
  assert.match(KB, /const anyHit = computed\(\(\) => hasAnyHit\(docCount\.value, hitCount\.value\)\)/)
  // 空态还要排除 error：请求失败时出横幅，不冒充「没找到」（三分支：加载中/真的 0 条/失败）
  assert.match(KB, /v-else-if="searching && !anyHit && !error"/)
})

test('过期响应被丢弃：请求序号落后的一律不落到界面上', () => {
  // isStale 就是页面里那道闸：序号不等 ⇒ 旧响应直接扔
  assert.equal(isStale(1, 1), false)
  assert.equal(isStale(1, 2), true)

  // 模拟「先发 a 词、没回来就改成 b 词」：a 的响应回来必须被丢弃，b 的才落屏
  const state = { seq: 0, applied: null }
  const land = (seq, payload) => {
    if (!isStale(seq, state.seq)) state.applied = payload
  }
  state.seq = 1
  state.seq = 2 // 用户改词 ⇒ 序号前进，1 号请求当场过期
  land(1, '旧词的结果')
  assert.equal(state.applied, null, '旧响应不得覆盖新响应')
  land(2, '新词的结果')
  assert.equal(state.applied, '新词的结果')

  // 页面里两路查询的写回前都必须过这道闸
  assert.match(KB, /const seq = \+\+reqSeq/)
  assert.match(KB, /if \(isStale\(seq, reqSeq\)\) return/)
})

test('检索：一次输入两路并行，回车与搜索按钮等价，清空回默认列表', () => {
  // 两路并行：Promise.all 一次打 /kb/documents 与 /kb/search
  assert.match(KB, /Promise\.all\(\[\s*api\.kbDocuments\(\{ q: term/)
  assert.match(KB, /api\.kbSearch\(\{ q: term, page: hitPage\.value, page_size: hitSize\.value \}\)/)
  // 回车与按钮同一条路
  assert.match(KB, /@keyup\.enter="search"/)
  assert.match(KB, /@click="search"/)
  // 空输入回车 ⇒ 不发 /kb/search，只回全量文档列表；清空 ✕ 两路都清、走同一条路（不是空态）
  assert.match(KB, /const term = q\.value\.trim\(\)\s*\n\s*page\.value = 1\s*\n\s*hitPage\.value = 1\s*\n\s*\/\/ 空输入回车[\s\S]{0,120}?if \(!term\) return loadDocs\(\)/)
  assert.match(KB, /@clear="onClear"/)
  assert.match(KB, /function onClear\(\) \{[\s\S]{0,300}?hits\.value = \[\][\s\S]{0,160}?return search\(\)/)
  // 两路分页各自独立
  assert.match(KB, /:page="page"[\s\S]{0,120}?@update:page="onPage"/)
  assert.match(KB, /:page="hitPage"[\s\S]{0,120}?@update:page="onHitPage"/)
})