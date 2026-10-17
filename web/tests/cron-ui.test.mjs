/**
 * 定时任务卡的**卡内版式**护栏（keepsake_cron_ui_fix，纯静态扫描 StatsView）。
 *
 * 病灶是 1920 宽下这一排卡排成 5 列 ~320px 窄卡之后连锁出来的六处版式错乱。
 * 这类问题的静默失败模式不是「报错」，而是「样式悄悄退回去」，所以逐条钉死：
 *   1) 列数：任务网格抬到 420px 下限（不再 5 列窄卡），900px 以下单列；
 *   2) 时间行：左贴徽标 + 单行省略，超宽不再硬裁成「2026-10-1」；
 *   3) 任务名单行 clamp，长名不再把整卡行高顶歪；
 *   4) 无脚本名时出灰字占位并收起复制钮（脚本行不许只剩一个悬空图标）；
 *   5) 标签区固定成「cron 起头 + 状态紧随」的一行，状态标签不再各按各的长度跑；
 *   6) 操作行文字链接与 28px 描边钮同高；
 *   7) 调度器行走 toLocal，任务区模板里**不许出现裸 ISO**（带 +08:00 尾巴那种），
 *      且不许在本地时间后面再挂一段没值的英文标签（`Next run` 那种）。
 *
 * 范围：本单只改 StatsView 的卡内类，ItemCard / tokens.css 是共享面 —— 这里
 * 顺带钉一句「别把共享组件当自家后院」，免得下次图省事去改全局。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = `${ROOT}src`
const read = (p) => readFileSync(`${SRC}/${p}`, 'utf8')

const STATS = read('views/StatsView.vue')

/** 模板段与样式段分开取：断言要能指明是模板缺了还是样式缺了。 */
const TEMPLATE = STATS.slice(0, STATS.indexOf('<script setup>'))
const CARD_TPL = TEMPLATE.slice(TEMPLATE.indexOf('class="card-grid cron-grid"'))
const STYLE = STATS.slice(STATS.indexOf('<style scoped>'))

/** 取某个选择器的规则体（scoped 样式里没有嵌套，够用）。 */
const rule = (selector) => {
  const m = STYLE.match(new RegExp(`(?:^|\\n)${selector}\\s*\\{([^}]*)\\}`))
  return m ? m[1] : ''
}

test('列数：任务网格抬到 420px 下限，不再是 300px 的 5 列窄卡', () => {
  assert.match(TEMPLATE, /class="card-grid cron-grid"/, '任务网格要挂本页自己的网格类')
  const cols = rule('\\.cron-grid')
  assert.match(cols, /repeat\(auto-fill,\s*minmax\(420px,\s*1fr\)\)/,
    'auto-fill + minmax(420px, 1fr)：1920 下 3 列，与上方 4/3/3 列区块同节奏')
  // 窄屏仍要能退到单列，否则 900 以下横向滚动
  assert.match(STYLE, /@media \(max-width: 900px\)[\s\S]{0,160}?\.cron-grid[\s\S]{0,160}?minmax\(0,\s*1fr\)/,
    '900px 以下退回单列')
})

test('时间行：左贴徽标 + 单行省略，不硬裁切', () => {
  const t = rule('\\.cron-grid :deep\\(\\.card-time\\)')
  assert.match(t, /flex:\s*1 1 auto/, '时间块要拉满整行，短文才不会被 space-between 甩到右缘')
  assert.match(t, /min-width:\s*0/, 'flex 子项收缩的前提')
  assert.match(t, /text-align:\s*left/, '始终左贴徽标')
  assert.match(t, /white-space:\s*nowrap/, '不许折行')
  assert.match(t, /overflow:\s*hidden/, '')
  assert.match(t, /text-overflow:\s*ellipsis/, '超宽省略（原来直接硬裁成「2026-10-1」）')
})

test('任务名：单行 clamp，长名不顶歪整卡行高', () => {
  const t = rule('\\.cron-grid \\.card-title')
  assert.match(t, /-webkit-line-clamp:\s*1/, '单行（全局 .card-title 是 2 行）')
  assert.match(t, /line-clamp:\s*1/, '标准属性一起写')
})

test('无脚本名：出灰字占位并收起复制钮', () => {
  assert.match(CARD_TPL, /<span v-if="row\.script" class="cell-key">/, '脚本行按 script 有无分流')
  assert.match(CARD_TPL, /<span v-else class="no-script">（无脚本：事件\/一次性任务）<\/span>/,
    '没脚本要说明来由，不是留个空行')
  assert.match(CARD_TPL, /v-if="row\.script" #lead/, '复制链接同样收掉，不许点下去复制空串')
  assert.match(rule('\\.no-script'), /color:\s*var\(--text-ghost\)/,
    '--text-ghost 按 tokens.css 约定就是给占位符用的，不新造颜色')
})

test('标签区：cron 起头 + 状态紧随，一行固定不换行', () => {
  assert.match(CARD_TPL, /class="cron-tags"/, '两个标签要有一个固定结构的容器')
  const wrap = rule('\\.cron-tags')
  assert.match(wrap, /display:\s*flex/, '')
  assert.match(wrap, /flex-wrap|gap/, '标签之间留既定 gap 令牌')
  assert.match(rule('\\.cron-tags \\.t-cron'), /flex:\s*0 1 auto/, 'cron 表达式可省略让位')
  assert.match(rule('\\.cron-tags \\.t-status'), /flex:\s*none/, '状态标签不压缩，一排卡对齐')
})

test('操作行：文字链接与 28px 描边钮同高', () => {
  assert.match(rule('\\.cron-grid :deep\\(\\.card-link\\)'), /height:\s*28px/,
    '与 .act-btn 既有高度一致；换行与 8px 间距由 ItemCard 保证')
})

test('调度器行走 toLocal，模板里不出现裸 ISO', () => {
  assert.match(STATS, /import \{ toLocal \} from '\.\.\/utils\/format'/, '复用现成 toLocal')
  assert.match(STATS, /function schedLabel\(raw\)/, '调度器那行必须过一道格式化')
  const fn = STATS.slice(STATS.indexOf('function schedLabel'), STATS.indexOf('async function load'))
  assert.match(fn, /toLocal\(/, 'schedLabel 里要走 toLocal，不许裸 new Date')
  assert.doesNotMatch(fn, /new Date\(/, '时区交给 toLocal，别自己造')
  assert.match(TEMPLATE, /class="sched-v">\{\{ schedulerStatus \|\| '未知' \}\}/,
    '显示值 = 规整后的调度器状态（schedLabel 已在 load 里算好）')
  assert.match(STATS, /schedulerStatus\.value = schedLabel\(schedulerStatus\.value\)/,
    '后端给的结构化两段必须过 schedLabel 才进模板')
  // 裸 ISO（`04:30:00.123456+08:00` 这种）不许写死在模板里
  assert.doesNotMatch(TEMPLATE, /\d{2}:\d{2}:\d{2}\.\d+\+/, '模板里不许出现裸 ISO 串')
  // 🔴 主脑真机复验的病灶：`Next run` 段是后端原样透传过来的重复标签，
  // 界面这一侧只准消费结构化的 next_run，不许再拼一段出来
  assert.doesNotMatch(fn, /Next run/, '渲染路径不许再拼 Next run 标签')
  assert.doesNotMatch(TEMPLATE, /Next run/, '调度器行模板里不出现 Next run')
})

test('schedLabel 真跑一遍：结构化两段各归各位，Next run 不再漏进界面', async () => {
  const { toLocal } = await import(`${ROOT}src/utils/format.js`)
  // 从 SFC 里原样抠出这段逻辑再执行 —— 纯静态断言盯不住「摘错了一段」的活
  const src = STATS.slice(STATS.indexOf('function schedLabel'),
    STATS.indexOf('async function load')).trim()
  // eslint-disable-next-line no-new-func
  const schedLabel = new Function('toLocal', `${src}\nreturn schedLabel`)(toLocal)

  const ISO = '2026-10-17T04:30:00.123456+08:00'
  // 后端拆好的两段：人话 + ISO。ISO 过 toLocal，绝不再与英文标签重复
  assert.equal(schedLabel({ text: 'daemon running, 6 jobs', next_run: ISO }),
    `daemon running, 6 jobs · 下次 ${toLocal(ISO)}`, '两段各归各位')
  // 整行就是个 Next run 的场合：只剩「下次 X」，不许留半截悬空标签
  assert.equal(schedLabel({ text: '', next_run: ISO }), `下次 ${toLocal(ISO)}`)
  const one = schedLabel({ text: 'daemon running, 6 jobs', next_run: ISO })
  assert.doesNotMatch(one, /Next run/, '语义重复的英文标签不许再出现')
  assert.doesNotMatch(one, /\d{2}:\d{2}:\d{2}\.\d+\+/, '回显里不许漏出裸 ISO')
  assert.equal(schedLabel(null), '未知', '没 CLI 时后端给 null')
  assert.equal(schedLabel({ text: '', next_run: '' }), '未知', '两段都空也是「未知」')
  assert.equal(schedLabel('cron daemon running'), 'cron daemon running',
    '旧实例回裸字符串时原样透传，不至于整行变「未知」')
})

test('共享面不许动：修复只落在 StatsView 的卡内类上', () => {
  // ItemCard / tokens.css 是常驻层、记忆条目、知识库几页共用的
  const cards = read('components/ItemCard.vue')
  assert.match(cards, /class="card-time"/, 'ItemCard 结构未变（时间位仍在卡顶行）')
  assert.match(read('styles/tokens.css'), /\.card-grid\s*\{[^}]*minmax\(300px, 1fr\)/,
    '全局 .card-grid 保持原样，任务页自己抬下限')
  assert.doesNotMatch(STATS, /!\s*important/, '别用 !important 硬压共享样式')
})
