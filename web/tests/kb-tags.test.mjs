/**
 * 知识库「自由标签（tags）」这一层的前端接线护栏（keepsake_kb_tags，纯静态扫描）：
 *   1) 筛选 chip 行真的接到后端：数据来自 /kb/tags，两路列表请求都带 tag=；
 *   2) 点选即筛、**再点取消**（取消不接线 = 一选就再也回不去全量）；
 *   3) chip 计数**来自后端**，前端一个数字都不许写死；库里没标签时整行隐藏；
 *   4) 文档卡 / 切片命中卡渲染 tags chip（复用现有 .tag，零新色）；
 *   5) 新增/编辑弹框：逗号或回车成 chip，超 12 个 / 单项超 32 字**即时提示**；
 *   6) 编辑保存**总是**带 tags（后端是整体替换，不带 = 删不掉标签）；
 *   7) 导入解析把 tags 原样带给后端（导出/导入列对齐）。
 * 「编译能过、上线跑起来才发现」的接线断链都钉在这七条上。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SRC = fileURLToPath(new URL('../src', import.meta.url))
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const KB = read('views/KbView.vue')
const API = read('api/index.js')

test('chip 行接线：数据来自 /kb/tags，两路列表请求都带 tag=', () => {
  assert.match(API, /kbTags: \(\) => http\.get\('\/api\/v1\/kb\/tags'\)/,
    'api 层要有 kbTags（前端不自己拼标签集合）')
  assert.match(KB, /api\.kbTags\(\)/, '页面要真的调这个接口')
  assert.match(KB, /async function loadTags\(\)/)
  // chip 由后端返回的数据驱动渲染（不逐个手写 = 库里加个标就少一个入口）
  assert.match(KB, /v-for="row in tagRows"/, 'chip 行必须遍历 tagRows 渲染')
  // 浏览态与检索态两路都透传：漏一路 = 标签筛选只对一半场景生效
  assert.equal(
    (KB.match(/tag: activeTag\.value/g) || []).length,
    2,
    'loadDocs 与 loadBoth 都要透传 tag',
  )
  // 打开页面与写操作之后都刷一次（打完标签立刻能在 chip 行里看到）
  assert.match(KB, /onMounted\([\s\S]{0,260}loadTags\(\)[\s\S]{0,120}loadDocs\(\)/)
  assert.match(KB, /function refresh\(\) \{[\s\S]{0,240}loadTags\(\)/)
})

test('点选即筛、再点取消：取消不接线就回不去全量', () => {
  assert.match(KB, /@click="onTag\(row\.tag\)"/, 'chip 点选要接到 onTag')
  assert.match(KB, /function onTag\(t\)/, '缺 onTag 接线')
  assert.match(KB, /activeTag\.value = activeTag\.value === t \? '' : t[\s\S]{0,160}?return search\(\)/,
    '再点同一个标签必须取消选中，并走 search() 重查（页码各回第 1 页）')
  // 空态那句话要说清当前筛的是啥（含标签）
  assert.match(KB, /const filterLabel = computed\(\(\) => \{[\s\S]{0,400}?activeTag\.value/)
})

test('chip 计数来自后端、前端零写死数字', () => {
  // 计数直接渲染后端给的数；老库/接口失败 ⇒ 空列表 ⇒ 整行隐藏，不编一个假 chip
  assert.match(KB, /tagRows\.value = Array\.isArray\(r\?\.items\) \? r\.items : \[\]/)
  assert.match(KB, /catch \(e\) \{\s*\n\s*tagRows\.value = \[\]/,
    '接口失败要清空（不拿上一个库的旧标签冒充现状）')
  assert.match(KB, /<template v-if="tagRows\.length">[\s\S]{0,900}?<\/template>/,
    '标签为空时整行隐藏')
  // 🔴 写死判据：chip 行的计数不许来自任何本地常量
  assert.ok(!/const TAG_CHIPS\s*=/.test(KB), '标签集合只能来自后端，不许在前端定义')
  assert.match(KB, /class="tab-n">\{\{ row\.count \}/,
    'chip 上的数字必须直接是后端给的 row.count')
})

test('卡片渲染 tags chip：复用现有 .tag，零新色', () => {
  // 文档卡 / 切片命中卡 / 待审卡各一处（批2 的待审面板那张卡也要渲染标签）；
  // 字段缺席（老库未迁移）⇒ 空数组（当「没打标」）
  assert.equal((KB.match(/v-for="t in tagsOf\((doc|row)\)"/g) || []).length, 3,
    '文档卡、切片命中卡、待审卡都要渲染标签 chip')
  assert.match(KB, /function tagsOf\(row\) \{\s*\n\s*return Array\.isArray\(row\?\.tags\) \? row\.tags : \[\]/,
    'tagsOf 必须守住「字段缺席 = 没打标」，不能把 undefined 渲成标签')
  // 样式只用现成的 .tag（tokens.css 里那份），不许为标签另造一套颜色
  const style = KB.match(/<style scoped>([\s\S]*)<\/style>/)[1]
  assert.ok(!/\.tag-(quant|my|kb)/i.test(style), '不许为知识库标签新造样式类')
  assert.ok(!/#[0-9a-fA-F]{3,6}\b/.test(style), 'scoped 样式里不许出现硬编码色值（零新色）')
})

test('新增/编辑弹框：逗号或回车成 chip，超限即时提示', () => {
  // el-select multiple + allow-create + default-first-option：逗号与回车各成一个项
  assert.equal((KB.match(/v-model="(draft|edit)Tags"[\s\S]{0,320}?allow-create/g) || []).length, 2,
    '新增与编辑弹框都要有可输入成 chip 的标签控件')
  assert.match(KB, /default-first-option/, '回车要能直接成 chip（不给 default-first-option 会先选中建议项）')
  // 上限常量与后端同值（12 / 32），提示文案把它们说给用户听
  assert.match(KB, /const TAG_MAX_ITEMS = 12/)
  assert.match(KB, /const TAG_MAX_LEN = 32/)
  assert.match(KB, /function tagsInvalid\(list\)/)
  assert.match(KB, /function tagsHint\(list\)/)
  assert.match(KB, /最多 \$\{TAG_MAX_ITEMS\} 个、单项 ≤\$\{TAG_MAX_LEN\} 字/,
    '合规时要说出上限口径（用户不用猜）')
  assert.match(KB, /已 \$\{arr\.length\} 个，最多 \$\{TAG_MAX_ITEMS\} 个标签/,
    '超数要即时说清超了几个')
  assert.match(KB, /超过 \$\{TAG_MAX_LEN\} 字/,
    '单项超长要即时说清超了哪个')
  // 提示挂在两个弹框上，且超限时提交按钮被禁用（不让用户填完再吃 400）
  // 新增弹框：提示 v-if + 编辑弹框：提示 v-if + 保存按钮禁用（canCreate 走 .value，
  // 上面那条判据单钉）—— 超限即时提示且**不让提交**，别让用户填完再吃一个 400
  assert.equal((KB.match(/tagsInvalid\((draftTags|editTags)\)/g) || []).length, 3,
    '两个弹框的即时提示 + 编辑保存都要挂 tagsInvalid')
  assert.match(KB, /:disabled="!canCreate"/)
  assert.match(KB, /!editTitle\.trim\(\) \|\| tagsInvalid\(editTags\)/,
    '编辑按钮的禁用判据要带上标签超限')
  assert.match(KB, /const canCreate = computed\(\(\) => \{[\s\S]{0,200}?tagsInvalid\(draftTags\.value\)/,
    '新增按钮的禁用判据要带上标签超限')
})

test('编辑保存总是带 tags：后端是整体替换，不带就删不掉标签', () => {
  assert.match(KB, /editTags\.value = \[\.\.\.tagsOf\(doc\)\]/,
    '编辑弹框要预填现值（列表接口投影了 tags）')
  const edit = KB.match(/async function doEdit\(\) \{[\s\S]*?\n\}/)[0]
  assert.match(edit, /payload\.tags = \[\.\.\.editTags\.value\]/,
    '保存必须带 tags（空数组也要带：那是「清空所有标签」）')
  assert.ok(!/payload\.tags = editTags\.value\.length \?/.test(edit),
    '不许「非空才带」—— 那会让「删光标签」变成什么都没发生')
})

test('导入：tags 原样带给后端，导出列与后端对齐', () => {
  const parse = KB.match(/function parseJsonl\(text\) \{[\s\S]*?\n\}/)[0]
  assert.match(parse, /tags: raw\.filter\(\(t\) => typeof t === 'string'\)/,
    '非字符串项就地丢掉，不把类型噪声送到后端')
  assert.match(parse, /typeof o\.tags === 'string' \? o\.tags\.split\(\/\[,，\]\/\) : \[\]/,
    '手写逗号串也要收（导出的是数组，手写常是串，两种都得吃得下）')
  // 导入模板提示里带 tags（字段名只能靠猜，猜错的表现是「导入了 N 条全是 0」）
  assert.match(KB, /每行一篇 \{title, content, tags\}/)
  assert.match(KB, /"tags": \["量化", "部署"\]/)
})
