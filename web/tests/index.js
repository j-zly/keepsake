/**
 * 测试目录的入口。
 *
 * 为什么需要它：`node --test tests/` 这种「传目录」的写法在 Node 22 上不再递归展开
 * （内部改成了 globSync，`tests/` 只匹配到目录本身，于是把目录当模块 require 报
 * MODULE_NOT_FOUND）。这里把 tests/ 下所有 *.test.mjs 挂一遍，目录形式也能跑全套。
 * 文件名必须是 index.js：CJS 的目录解析只认 index.js，不认 index.mjs。
 *
 * 用 createRequire 同步挂载（不用顶层 await —— 这个文件是被 require 进来的，
 * 带 TLA 的 ESM 走 require 会直接 ERR_REQUIRE_ASYNC_MODULE）。
 *
 * `npm test`（`node --test tests/*.test.mjs`）不匹配本文件，跑法与结果不变。
 * 自动发现、不写死清单：新增测试文件不用回来改这里。
 */
import { readdirSync } from 'node:fs'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

for (const name of readdirSync(new URL('.', import.meta.url)).filter((n) => n.endsWith('.test.mjs')).sort()) {
  require(`./${name}`)
}