/**
 * 知识库「按内容类型」这一层的前端接线护栏（keepsake_kb_content_types，纯静态扫描）：
 *   1) 类型 Tab 真的接到后端：选中即 GET /kb/documents?content_type=，两路都要带；
 *   2) Tab 计数**来自后端**（/kb/type-counts），前端一个数字都不许写死；
 *   3) 新增弹框：类型可选（手记/文章/链接），选链接时 URL 必填、正文标签变「摘要」；
 *   4) 编辑弹框**不给改类型的入口**，保存请求也不带 content_type（创建后不可变）；
 *   5) 类型徽标按 content_type 渲染，字段缺席按 note 缺省（不假装是技能）。
 * 「编译能过、上线跑起来才发现」的接线断链都钉在这五条上。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const API = read('api/index.js')

test('类型 Tab 接线：选中即带 content_type 重查，且两路都带', () => {
  // Tab 由内容驱动渲染（不逐个手写按钮 = 漏一个 Tab 的筛选入口）
  assert.match(KB, /v-for="t in TYPE_TABS"/, '类型 Tab 必须遍历 TYPE_TABS 渲染')
  assert.match(KB, /@click="onType\(t\.value\)"/, 'Tab 点选要接到 onType')
  assert.match(KB, /function onType\(v\)/, '缺 onType 接线')
  assert.match(KB, /contentType\.value = v[\s\S]{0,160}?return search\(\)/,
    '换类型必须走 search() 重查（页码各回第 1 页）')
  // 浏览态与检索态两路都透传：漏一路 = Tab 只对一半场景生效
  assert.equal(
    (KB.match(/content_type: contentType\.value/g) || []).length,
    2,
    'loadDocs 与 loadBoth 都要透传 content_type',
  )
})

test('Tab 计数来自后端接口，前端不写死任何数字', () => {
  assert.match(API, /kbTypeCounts: \(\) => http\.get\('\/api\/v1\/kb\/type-counts'\)/)
  assert.match(KB, /api\.kbTypeCounts\(\)/, '页面要真的调这个接口')
  assert.match(KB, /async function loadTypeCounts\(\)/)
  // 计数渲染挂在后端给的字典上；取不到就**不渲染数字**（v-if），而不是显示编出来的 0
  assert.match(KB, /v-if="typeCounts\[t\.value\] !== undefined" class="tab-n"/,
    '计数只在后端给了这个类型时才显示')
  // 🔴 写死判据：Tab 定义里出现裸数字（: 20 / : 0 之类）就是编数字
  const tabs = KB.match(/const TYPE_TABS = \[[\s\S]*?\n\]/)
  assert.ok(tabs, '缺 TYPE_TABS')
  assert.ok(!/\d/.test(tabs[0]), 'TYPE_TABS 里不许出现数字（计数只能来自后端）')
  assert.ok(!/:\s*\d+\b/.test(KB.match(/const CREATE_TYPES = \[[\s\S]*?\n\]/)[0]),
    'CREATE_TYPES 里不许出现数字')
})

test('新增：类型可选（手记/文章/链接），选链接时 URL 必填、正文标签变摘要', () => {
  assert.match(KB, /<el-select v-model="draftType"/, '新增弹框要有类型下拉')
  assert.match(KB, /v-for="t in CREATE_TYPES"/)
  // 创建端点只开放 note/article/link —— skill 归灌入器、image 后端 400，页面不给入口
  const types = KB.match(/const CREATE_TYPES = \[([\s\S]*?)\n\]/)[1]
  for (const v of ["'note'", "'article'", "'link'"]) assert.ok(types.includes(v), `缺 ${v}`)
  assert.ok(!types.includes("'skill'"), 'skill 只能由灌入器建，页面不给入口')
  assert.ok(!types.includes("'image'"), 'image 后端暂未开放，页面不给入口')
  // URL 框只在链接类型下出现
  assert.match(KB, /v-if="draftType === 'link'" label="链接地址（必填）"/)
  assert.match(KB, /draftType === 'link' \? '摘要（可选）' : '正文（必填）'/,
    '选链接时正文标签必须变「摘要（可选）」')
  // 提交闸：链接必须有 URL；其余类型必须有正文（口径与后端一致）
  assert.match(KB, /const canCreate = computed\(\(\) => \{/)
  assert.match(KB, /\? !!draftUrl\.value\.trim\(\)/, 'link 的提交闸要看 URL')
  assert.match(KB, /:disabled="!canCreate"/, '新增按钮的禁用判据要走 canCreate')
  // 提交时按类型带上 payload
  assert.match(KB, /content_type: draftType\.value/)
  assert.match(KB, /if \(draftType\.value === 'link'\) payload\.url = draftUrl\.value\.trim\(\)/)
})

test('编辑：类型只读展示、保存请求不带 content_type', () => {
  // 编辑弹框里类型是**静态展示**，没有可改控件
  assert.match(KB, /label="内容类型（创建后不可改）"/)
  assert.match(KB, /<span class="fld-static">[\s\S]{0,200}?typeOf\(editDoc\)/)
  assert.ok(!/<el-select v-model="editType"/.test(KB), '编辑弹框不许有类型下拉（类型不可变）')
  // 保存请求里绝不能出现 content_type —— 后端带异值直接 400
  // （判据只钉 payload 的构造，不钉整个函数体：函数里的**注释**要解释为什么不带）
  const edit = KB.match(/async function doEdit\(\) \{[\s\S]*?\n\}/)[0]
  const payloads = edit.match(/payload\s*=\s*[^;]+/g) || []
  assert.ok(payloads.length >= 1, 'doEdit 里没有 payload 构造')
  assert.ok(payloads.every((p) => !/content_type/.test(p)),
    `保存请求不许带 content_type：${payloads.join(' | ')}`)
  assert.match(edit, /api\.kbUpdateDocument\(doc\.doc_id, payload\)/)
  // 链接文档改 URL 仍单块重存（后端拿 meta 里的原值兜底，不会丢）
  assert.match(edit, /if \(typeOf\(doc\) === 'link' && url\) payload\.url = url/)
})

test('类型徽标：按 content_type 渲染，字段缺席按 note 缺省', () => {
  // 文档卡 / 切片命中卡 / 编辑弹框 / 待审卡，四处都挂同一个判据
  // （批2 加了待审面板那张卡 —— 它也要说清这篇是什么类型，批准后才按类型切块）
  assert.equal((KB.match(/tag--\$\{typeOf\(/g) || []).length, 4,
    '文档卡、切片命中卡、编辑弹框、待审卡都要有类型徽标')
  // 🔴 严格判据：字段缺席按 note 缺省，**不许**「没有就是技能/按 source_type 猜」
  assert.match(KB, /function typeOf\(row\) \{\s*\n\s*return row\?\.content_type \|\| 'note'/,
    'typeOf 必须读 content_type 且缺席按 note')
  assert.ok(!/content_type \|\| 'skill'/.test(KB), '缺席不许默认成 skill')
  // 链接徽标带 ↗（点得进去的意思），技能压低存在感
  assert.match(KB, /\.tag--link \{[^}]*--accent/)
  assert.equal((KB.match(/typeOf\((doc|row)\) === 'link'"> ↗/g) || []).length, 3,
    '文档卡、切片命中卡、待审卡的链接徽标都要带 ↗')
})

test('读路径：老库未迁移时按 note 渲染、不编计数、不假装筛过', () => {
  // 计数接口失败时清空（不保留上一个库的旧数字冒充现状）
  assert.match(KB, /async function loadTypeCounts\(\) \{[\s\S]{0,320}?typeCounts\.value = \{\}/)
  // 打开页面与写操作之后都刷一次计数
  assert.match(KB, /onMounted\([\s\S]{0,220}loadTypeCounts\(\)[\s\S]{0,120}loadDocs\(\)/)
  assert.match(KB, /function refresh\(\) \{[\s\S]{0,200}loadTypeCounts\(\)/)
  // 空态说清当前筛的是啥（source_type 与 content_type 都算）
  assert.match(KB, /const filterLabel = computed\(\(\) => \{[\s\S]{0,300}?contentType\.value/)
})
