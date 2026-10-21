/**
 * 「重新生成凭据」= approve + **显式 reissue**（重复批准不许静默换发）。
 *
 * **为什么单开一张单**：`web/tests/agents-ui.test.mjs` 是只读文件（属主 root），
 * 改不动 —— 换发那条新语义在这里补齐。
 *
 * 这条纪律钉的是「静默换发」这个误伤路径（2026-10-21 用户拍板修）：
 * 已批准的代理再 approve 一次 ⇒ 以前静默签发新凭据、旧凭据当场作废
 * ⇒ 线上代理瞬间全断，操作者却毫无感知。三个误伤路径：
 *   ① 审核页双击；② MCP 治理工具对已批准代理再批准；③ UI 状态未刷新重复点。
 *
 * 逐条钉死：
 *   1) **批准那条路不带 reissue**（pending / revoked 行照旧直接批准签发）；
 *   2) **换发那条路必须显式 reissue=true**（同一条端点）；
 *   3) 换发仍过**手输确认闸**（agent_id 前 8 位）—— 不许弱化；
 *   4) 审核页只渲染 pending 行 ⇒ 批准钮对 approved 行**无路径可达**；
 *   5) 弹框文案把后果说清（旧凭据立即失效）。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const VIEW = read('views/AgentsView.vue')
const REVIEW = read('views/AgentsReviewView.vue')
const API = read('api/index.js')

/** doAction 那个函数体（从函数头到文件尾）。 */
function doAction() {
  return VIEW.slice(VIEW.indexOf('async function doAction'))
}

test('api 层：批准不带 reissue，换发显式 reissue=true（同一条端点）', () => {
  // 批准：只有 confirm —— 后端对已批准的代理据此 409（不换发、不动凭据）
  assert.match(
    API,
    /approveAgent: \(agentId\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/approve`, \{ confirm: true \}\)/,
    'approveAgent 不许带 reissue（批准与换发必须分成两个意图）',
  )
  // 换发：同一条端点 + 显式 reissue=true
  assert.match(
    API,
    /reissueAgent: \(agentId\) =>\s*http\.post\(`\/api\/v1\/agents\/\$\{encodeURIComponent\(agentId\)\}\/approve`, \{ confirm: true, reissue: true \}\)/,
    '换发走同一条 approve 端点，但必须显式 reissue=true',
  )
})

test('登记页：批准与换发分两条路（approve 不带 reissue / rotate 带 reissue）', () => {
  const body = doAction()
  // 批准分支：只调 approveAgent（不碰 reissue）
  assert.match(body, /if \(kind === 'approve'\)[\s\S]{0,400}?api\.approveAgent\(row\.agent_id\)/,
    '批准分支走 approveAgent（不带 reissue）')
  // 换发分支：走 reissueAgent
  assert.match(body, /else if \(kind === 'rotate'\)[\s\S]{0,400}?api\.reissueAgent\(row\.agent_id\)/,
    '换发分支必须走 reissueAgent（显式 reissue）')
  // 反钉：换发那条不许再调不带 reissue 的 approveAgent
  const rotate = body.slice(body.indexOf("kind === 'rotate'"), body.indexOf("kind === 'revoke'"))
  assert.doesNotMatch(rotate, /api\.approveAgent\(/,
    '换发不许走不带 reissue 的批准口（否则后端 409，换发按钮直接失效）')
})

test('审核页：批准钮对 approved 行无路径可达（这页只渲染 pending）', () => {
  assert.match(REVIEW, /filter\(\(a\) => a\.status === 'pending'\)/,
    '审核页只列 pending 行')
  assert.match(REVIEW, /await api\.approveAgent\(row\.agent_id\)/,
    '审核页的批准走既有端点')
  assert.doesNotMatch(REVIEW, /reissueAgent/,
    '审核页没有换发入口（换发只在登记页，且要显式 reissue）')
})

test('换发仍过手输确认闸（agent_id 前 8 位）—— 不许弱化', () => {
  assert.match(VIEW, /if \(dlgKind\.value === 'rotate'\) return revokePrefix\.value/,
    '换发要手输 agent_id 前 8 位（与吊销同一道闸）')
  assert.match(VIEW, /const revokePrefix = computed\(\(\) => \(dlgRow\.value\?\.agent_id \|\| ''\)\.slice\(0, 8\)\)/,
    '确认串取前 8 位')
})

test('换发弹框把后果说清（旧凭据立即失效）', () => {
  assert.match(VIEW, /旧凭据立即作废/, '要说清旧凭据立即作废')
  assert.match(VIEW, /使用该凭据的客户端最迟 60 秒内被拒/, '要说清客户端最迟 60 秒内被拒')
  assert.match(VIEW, /代理状态与记忆归属不变/, '要说清状态与记忆归属不变')
})
