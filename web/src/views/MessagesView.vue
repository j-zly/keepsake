<!--
  消息中心（代理互聊信箱的控制台面）。

  2026-10 ks_messages_console。信箱存储与 MCP 工具由前序单交付，本页只做
  **治理面**：所有代理之间的往来消息在这里**全量可查** ——
  用户铁律：绝不被背着开小会。所以这页只有读，没有「清空 / 删除」这种动作。

  三条口径：
  1) **只读**：列表端点一个字段都不改（尤其不动 `read_at`）——
     在控制台里「看一眼历史」不许把对方的信标成已读（收信即消费是收件人的动作）。
  2) **按线程分组**：同一个 thread_id 的往来收在一组，组头点开/收起；
     组头写清**双方**（谁跟谁在聊）与**条数**（不让一屏消息看不出规模）。
  3) **正文超 160 字截断，点开看全文** —— 长消息不把一屏撑爆，但全文永远可达。

  样式守 web/DESIGN.md：浅色默认、卡片（1px 描边 + --shadow-card）、
  徽标走 .chip（未读用 .chip--accent 强调，已读用中性灰）、零硬编码色值。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">消息中心</h1>
      <p class="page-sub">
        代理之间的往来消息全在这里 —— 一封都不藏。代理用 MCP 的 message_send 互传消息后，这里立刻可见。
      </p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="filter-panel">
      <div class="filter-title">往来消息</div>
      <p class="filter-desc">
        按线程分组：同一件事的往来收在一处，组头写清双方与条数，点组头收起 / 展开。
      </p>
      <div class="filter-ctl">
        <el-select
          v-model="agent"
          class="sel"
          placeholder="全部代理"
          filterable
          clearable
          title="只看某一个代理（发件或收件都算）"
          @change="search"
        >
          <el-option
            v-for="a in agentOptions"
            :key="a.id"
            :label="a.name ? `${a.id}（${a.name}）` : a.id"
            :value="a.id"
          />
        </el-select>
        <el-input
          v-model="q"
          class="grow"
          placeholder="搜索正文关键词（清空回全量）"
          clearable
          @keyup.enter="search"
          @clear="onClear"
        >
          <template #prefix>
            <span class="search-ico">
              <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="7" cy="7" r="4.3" />
                <path d="m10.3 10.3 3.4 3.4" />
              </svg>
            </span>
          </template>
        </el-input>
        <el-button type="primary" :loading="loading" @click="search">查询</el-button>
      </div>
      <div class="filter-result">
        <span>共 {{ total }} 条消息 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <button class="soft-btn" :disabled="!dirty" @click="onClear">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="refresh">刷新</button>
        </div>
      </div>
    </div>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <div v-else-if="!items.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <rect x="2.8" y="5.2" width="18.4" height="13.6" rx="2" />
          <path d="m3.4 6.4 8.6 6 8.6-6" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p v-if="dirty"><b>没匹配到消息</b>换个更短的关键词试试，或点「清除筛选」回全部消息。</p>
        <p v-else><b>还没有代理往来消息</b>代理之间用 MCP 的 message_send 互传消息后，这里就会出现 —— 一条都不藏。</p>
      </div>
    </div>

    <div v-else class="thread-list">
      <section v-for="g in groups" :key="g.key" class="thread">
        <button class="thread-head" :aria-expanded="!isCollapsed(g.key)" @click="toggleGroup(g.key)">
          <span class="thread-caret" :class="{ open: !isCollapsed(g.key) }" aria-hidden="true">▸</span>
          <span class="thread-title">{{ g.title }}</span>
          <span class="tag" v-if="!g.threadId">未标注线程</span>
          <span class="chip">{{ g.count }} 条</span>
        </button>
        <div v-show="!isCollapsed(g.key)" class="thread-body">
          <div v-for="m in g.items" :key="m.seq" class="msg">
            <span class="msg-time">{{ toLocal(m.created) }}</span>
            <span class="msg-who" :title="`${m.from_id} → ${m.to_id}`">{{ m.from_id }} → {{ m.to_id }}</span>
            <span class="chip" :class="m.read ? '' : 'chip--accent'">{{ m.read ? '已读' : '未读' }}</span>
            <p class="msg-body" :title="isOpen(m.seq) ? '点一下收起' : '点一下看全文'" @click="toggleMsg(m.seq)">
              {{ isOpen(m.seq) ? m.content : preview(m.content) }}
            </p>
          </div>
        </div>
      </section>
    </div>

    <PagerBar
      :total="total"
      :page="page"
      :page-size="pageSize"
      @update:page="onPage"
      @update:page-size="onSize"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import PagerBar from '../components/PagerBar.vue'
import { api } from '../api'
import { showError } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { isStale } from '../utils/kb'

/** 正文截断长度（超过就收成一行摘要，点开看全文）。 */
const PREVIEW_LEN = 160

const data = ref(null)
const loading = ref(true)
const error = ref('')

const q = ref('')
const agent = ref('')
const page = ref(1)
const pageSize = ref(20)
const agentOptions = ref([])

const items = computed(() => data.value?.items || [])
/** `total` 是**过滤后**的总数（后端口径），PagerBar 拿它算页数，前端不另算。 */
const total = computed(() => data.value?.total ?? 0)
/** 有筛选在生效吗：空态分支与「清除筛选」按钮都看它。 */
const dirty = computed(() => !!(q.value || '').trim() || !!agent.value)

/** 收起的线程（默认全展开 —— 治理面要一眼看得见消息）。 */
const collapsed = ref(new Set())
/** 展开了全文的消息（按 seq）。 */
const opened = ref(new Set())

/**
 * 按线程分组（同一个 thread_id 归一组）。
 * 🔴 没有 thread_id 的消息按**双方**归组（`pair:a|b`，与方向无关）：
 * 同一对代理的往来本来就在聊一件事，散成两组反而看不懂。
 */
const groups = computed(() => {
  const map = new Map()
  for (const m of items.value) {
    const key = m.thread_id
      ? `t:${m.thread_id}`
      : `pair:${[m.from_id, m.to_id].sort().join('|')}`
    if (!map.has(key)) {
      map.set(key, { key, threadId: m.thread_id || '', ids: [], items: [] })
    }
    const g = map.get(key)
    g.items.push(m)
    for (const id of [m.from_id, m.to_id]) {
      if (id && !g.ids.includes(id)) g.ids.push(id)
    }
  }
  return [...map.values()].map((g) => ({
    ...g,
    count: g.items.length,
    // 组头写清**双方**：一方 = 单聊自己（少见），两方 = 谁跟谁在聊
    title: g.ids.length >= 2 ? `${g.ids[0]} ⇄ ${g.ids[1]}` : (g.ids[0] || '未知代理'),
  }))
})

/** 正文摘要：超 PREVIEW_LEN 字截断（全文永远可达 —— 点一下展开）。 */
function preview(text) {
  const s = text || ''
  return s.length > PREVIEW_LEN ? `${s.slice(0, PREVIEW_LEN)}…` : s
}

function isCollapsed(key) {
  return collapsed.value.has(key)
}

function toggleGroup(key) {
  const next = new Set(collapsed.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  collapsed.value = next
}

function isOpen(seq) {
  return opened.value.has(seq)
}

function toggleMsg(seq) {
  const next = new Set(opened.value)
  if (next.has(seq)) next.delete(seq)
  else next.add(seq)
  opened.value = next
}

// 请求序号：连续改词/翻页时旧响应只丢弃，不覆盖新结果（照抄 AgentsView 的收口口径）
let reqSeq = 0

async function load(silent = false) {
  const seq = ++reqSeq
  if (!silent) loading.value = true
  try {
    // 过滤与分页都在服务端做：agent / q 空即 undefined（不带这个参数）
    const list = await api.listMessages({
      agent: (agent.value || '').trim() || undefined,
      q: (q.value || '').trim() || undefined,
      page: page.value,
      size: pageSize.value,
    })
    if (isStale(seq, reqSeq)) return
    data.value = list
    error.value = ''
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    // 保留旧数据不清屏：一次抖动不该把正在看的消息抽走
    error.value = `读取消息失败：${e.message}`
    showError(error.value)
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

/** 代理下拉的选项（来自 /api/v1/agents；只取 id 与备注名，别的字段用不上）。 */
async function loadAgents() {
  try {
    const list = await api.listAgents({ page: 1, page_size: 100 })
    agentOptions.value = (list?.items || []).map((a) => ({ id: a.agent_id, name: a.name || '' }))
  } catch (e) {
    // 下拉空着也能用（不筛代理照样看全部消息）—— 不在这里报错打断页面
    agentOptions.value = []
  }
}

/** 「刷新」：代理名单与消息一起重拉（代理刚注册完时下拉要跟上）。 */
function refresh() {
  loadAgents()
  load()
}

/** 回车 / 「查询」：改条件一律回第 1 页（在第 5 页搜却停在第 5 页 = 空列表）。 */
function search() {
  page.value = 1
  load()
}

/** 清空 ✕ / 「清除筛选」：无条件回全量（不是空态）。 */
function onClear() {
  q.value = ''
  agent.value = ''
  search()
}

function onPage(p) {
  page.value = p
  load()
}

function onSize(s) {
  pageSize.value = s
  page.value = 1
  load()
}

onMounted(() => {
  load()
  loadAgents()
})
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 线程组：卡片（1px 描边 + 一档阴影），组头可点收起 / 展开 */
.thread-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.thread {
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 16px;
  box-shadow: var(--shadow-card);
}
.thread-head {
  display: flex;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 12px 16px;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--line-3);
  border-radius: 16px;
  color: var(--text);
  font-family: inherit;
  font-size: 14px;
  text-align: left;
  cursor: pointer;
}
.thread-head:hover {
  background: var(--panel-hover);
}
.thread-caret {
  color: var(--text-ghost);
  transform: rotate(0deg);
  transition: transform 0.15s ease;
}
.thread-caret.open {
  transform: rotate(90deg);
}
.thread-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.thread-body {
  padding: 0 16px;
}
/* 一条消息：时间 | 发件 → 收件 | 已读徽标 | 正文（正文占剩余宽度，可换行） */
.msg {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 10px 0;
  border-bottom: 1px solid var(--line-3);
}
.msg:last-child {
  border-bottom: 0;
}
.msg-time {
  flex: none;
  width: 112px;
  color: var(--text-faint);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.msg-who {
  flex: none;
  width: 200px;
  overflow: hidden;
  color: var(--text-dim);
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.msg-body {
  flex: 1 1 auto;
  min-width: 0;
  margin: 0;
  color: var(--text-dim);
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  cursor: pointer;
}
</style>
