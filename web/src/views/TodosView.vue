<!--
  待办（2026-10 ks_todo 一等公民的控制台页）。

  待办此前一直用 `kind:pending` 记忆条目凑合 —— 盘点 12 条里 9 条烂账
  （做完没销、过期没人知）。这页给它一个正经的去处：**状态机 + 归属 + 到期日**。

  三条口径：
  1) **状态三态**：开放 / 已完成 / 已废弃 —— 页面按状态分三段卡片，
     「已完成」和「已废弃」分开摆：一个是干完了，一个是这条作废了。
  2) **到期两态**：3 天内到期 = 橙（`chip--warn`）、已过期 = 红（`chip--danger`）。
     🔴 判据在**后端**（`due_state`），页面只按它上色 —— 两边各算一次必然漂。
  3) **归属 chip**：待办归谁，一眼看见（`.tag--owner`）。

  写操作（新增 / 改状态 / 真删）一律带 confirm —— 后端那道闸是硬的，
  页面这边再叠一层界面确认（真删手输标题防手滑）。

  2026-10-22 UI 整改（用户截图点名）：
  - **新增收进弹框**（components/TodoDialog.vue）：页头只留一个主按钮「＋ 新待办」，
    表单与那句归属/到期日提示长句一起进弹框 —— 不再裸铺在页面顶部占一整屏。
  - **筛选面板化**：筛选区只管筛（归属 / 状态），不再与新增区混成一团。
  - **按钮不许折字**：操作行按钮一律 `white-space: nowrap`，容器 `flex-wrap`
    （放不下整组换行，不许把字竖排成两行）。插槽内容用 `:deep` 才够得着。

  样式守 web/DESIGN.md：卡片 1px 描边 + --shadow-card、chip 走既有档、
  零硬编码色值。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">待办</h1>
      <p class="page-sub">
        谁欠谁一件事，写在这儿才算数。开放 {{ summary.open }} 条，其中
        <b :class="summary.overdue ? 'tone-danger' : ''">{{ summary.overdue }}</b> 条已过期。
      </p>
      <!-- 页头只有**一个**主动作：新增走弹框（表单不再裸铺在页面顶部） -->
      <div class="head-acts">
        <el-button type="primary" class="pill-btn" @click="openCreate">＋ 新待办</el-button>
      </div>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <!-- 计数条：数字全部来自后端（前端不自己数） -->
    <div class="stat-grid">
      <StatCard label="开放" :value="summary.open" unit="条" hint="还没销掉的" />
      <StatCard
        label="逾期"
        :value="summary.overdue"
        unit="条"
        :tone="summary.overdue ? 'danger' : 'ok'"
        hint="到期日已过，还没销"
      />
      <StatCard label="已完成" :value="summary.done" unit="条" hint="status = done" />
      <StatCard label="已废弃" :value="summary.obsolete" unit="条" hint="status = obsolete" />
    </div>

    <!-- 筛选面板：只管筛（新增在弹框里，不在这儿） -->
    <div class="filter-panel">
      <div class="filter-title">筛选待办</div>
      <p class="filter-desc">
        按归属代理与状态筛；清空回全量。三态（开放 / 已完成 / 已废弃）分三段摆。
      </p>
      <div class="filter-ctl">
        <el-select v-model="owner" class="sel" placeholder="全部归属" clearable @change="search">
          <el-option
            v-for="a in agentOptions"
            :key="a.agent_id"
            :label="a.name ? `${a.agent_id}（${a.name}）` : a.agent_id"
            :value="a.agent_id"
          />
        </el-select>
        <el-select v-model="status" class="sel" placeholder="全部状态" clearable @change="search">
          <el-option v-for="s in TODO_STATUSES" :key="s" :label="STATUS_LABEL[s]" :value="s" />
        </el-select>
        <button class="soft-btn" :disabled="loading" @click="search">查询</button>
      </div>
      <div class="filter-result">
        <span>共 {{ total }} 条 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <button class="soft-btn" :disabled="!dirty || loading" @click="clearFilter">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="refresh">刷新</button>
        </div>
      </div>
    </div>

    <!-- 批量销账：SelectionBar 与记忆条目页 / 常驻层页同一份组件（勾选口径逐字一致） -->
    <SelectionBar
      :count="selCount"
      :page-count="items.length"
      :page-selected="pageSelected"
      @select-all="selectAll"
      @clear="clearSel"
    >
      <el-button :disabled="!selCount || busy" :loading="busy" @click="batchDone">
        批量标完成 {{ selCount }} 条
      </el-button>
    </SelectionBar>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <div v-else-if="!items.length" class="panel pad">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M4.2 6.6h15.6M4.2 12h15.6M4.2 17.4h9.4" stroke-linecap="round" />
          <circle cx="19" cy="17.4" r="3.1" />
          <path d="m21.3 19.7 1.5 1.5" stroke-linecap="round" />
        </svg>
        <!-- 空态两分支：筛完没结果 vs 全库为空，说的不是同一件事 -->
        <p v-if="dirty">
          <b>没有匹配到这个筛选</b>
          换个归属或状态试试，或点「清除筛选」回全量。
        </p>
        <p v-else>
          <b>还没有待办</b>
          点右上角「＋ 新待办」写下第一件事，或让代理用 MCP 的 todo_add 记一笔。
        </p>
      </div>
    </div>

    <!-- 三段分组：开放 / 已完成 / 已废弃 -->
    <template v-for="group in groups" :key="group.key">
      <h3 v-if="group.rows.length" class="section-title">
        {{ group.label }} · {{ group.rows.length }}
      </h3>
      <div v-if="group.rows.length" class="card-grid">
        <div v-for="row in group.rows" :key="row.id" class="card">
          <div class="card-top">
            <el-checkbox
              :model-value="isSel(row.id)"
              :aria-label="`选中 ${row.title}`"
              @change="toggleSel(row.id, $event)"
            />
            <span class="card-time">{{ row.created ? row.created.slice(0, 10) : '' }}</span>
          </div>
          <div class="card-title">{{ row.title }}</div>
          <div v-if="row.detail" class="card-body">{{ row.detail }}</div>
          <div class="card-tags">
            <span class="tag tag--owner" :title="row.owner">
              {{ ownerName(row.owner) }}
            </span>
            <span v-if="dueChipText(row)" class="chip" :class="dueChipClass(row)">
              {{ dueChipText(row) }}
            </span>
            <span v-if="row.done_at" class="chip">完成于 {{ row.done_at.slice(0, 10) }}</span>
          </div>
          <!-- 操作行：左文字链接（标完成 / 标废弃）+ 右危险按钮（删除） -->
          <div class="card-foot">
            <div class="card-links">
              <button v-if="row.status !== 'done'" class="card-link" @click="markDone(row)">
                标完成
              </button>
              <button v-else class="card-link" @click="markOpen(row)">重新打开</button>
              <button
                v-if="row.status !== 'obsolete'"
                class="card-link card-link--muted"
                @click="markObsolete(row)"
              >
                标废弃
              </button>
            </div>
            <div class="card-acts">
              <button class="act-btn act-btn--danger" @click="askDelete(row)">删除</button>
            </div>
          </div>
        </div>
      </div>
    </template>

    <PagerBar
      v-if="total > pageSize"
      :page="page"
      :page-size="pageSize"
      :total="total"
      @update:page="onPage"
      @update:page-size="onPageSize"
    />

    <!-- 新增待办：表单在弹框里（页头那个「＋ 新待办」开它） -->
    <TodoDialog
      v-model="createOpen"
      :agents="agentOptions"
      :busy="creating"
      @submit="create"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import PagerBar from '../components/PagerBar.vue'
import SelectionBar from '../components/SelectionBar.vue'
import StatCard from '../components/StatCard.vue'
import TodoDialog from '../components/TodoDialog.vue'
import { api } from '../api'
import { loadAgentRows } from '../utils/agents'
import { showError, showPrompt, showToast } from '../utils/dialog'
import { STATUS_LABEL, TODO_STATUSES, dueChipText, dueTone, groupByStatus } from '../utils/todo'

const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const owner = ref('')
const status = ref('')
const loading = ref(false)
const busy = ref(false)
const creating = ref(false)
const error = ref('')
const summary = ref({ open: 0, done: 0, obsolete: 0, total: 0, overdue: 0 })
const agentOptions = ref([])

/** 新增弹框的开合（页头那个「＋ 新待办」按钮改它）。 */
const createOpen = ref(false)

/** 三段分组（顺序：开放 → 已完成 → 已废弃）。 */
const grouped = computed(() => groupByStatus(items.value))
const groups = computed(() => [
  { key: 'open', label: '开放', rows: grouped.value.open },
  { key: 'done', label: '已完成', rows: grouped.value.done },
  { key: 'obsolete', label: '已废弃', rows: grouped.value.obsolete },
])

/** 有筛选在生效吗：空态分支与「清除筛选」按钮都看它。 */
const dirty = computed(() => !!(owner.value || status.value))

/** 勾选集合（跨页保留，与记忆条目页同款口径）。 */
const selected = ref(new Set())
const selCount = computed(() => selected.value.size)
const pageSelected = computed(() => items.value.filter((r) => selected.value.has(r.id)).length)

function isSel(id) {
  return selected.value.has(id)
}

function toggleSel(id, on) {
  const next = new Set(selected.value)
  if (on) next.add(id)
  else next.delete(id)
  selected.value = next
}

function selectAll(on) {
  const next = new Set(selected.value)
  for (const r of items.value) {
    if (on) next.add(r.id)
    else next.delete(r.id)
  }
  selected.value = next
}

function clearSel() {
  selected.value = new Set()
}

/** 代理 id → 显示名（注册表备注名；取不到就显示裸 id）。 */
const agentNames = computed(() =>
  Object.fromEntries(agentOptions.value.map((a) => [a.agent_id, a.name || a.agent_id]))
)

function ownerName(id) {
  const key = String(id || '')
  return key ? agentNames.value[key] || key : '未指派'
}

/** 到期 chip 的类：soon → 橙（warn）、overdue → 红（danger）。 */
function dueChipClass(row) {
  const tone = dueTone(row?.due_state)
  return tone ? `chip--${tone}` : ''
}

async function loadSummary() {
  try {
    const data = await api.todoSummary()
    summary.value = {
      open: Number(data?.open || 0),
      done: Number(data?.done || 0),
      obsolete: Number(data?.obsolete || 0),
      total: Number(data?.total || 0),
      overdue: Number(data?.overdue || 0),
    }
  } catch (e) {
    // 计数读失败：留 0（页面照常显示列表），**不把整页拖红**
    error.value = `读取待办计数失败：${e.message}`
  }
}

async function loadAgents() {
  try {
    const rows = await loadAgentRows(api.listAgents)
    // 🔴 只列**已批准**的代理：后端对未批准的 owner 直接 404（治理面：查无此代理），
    //   下拉里摆一个选了就报错的选项是自找麻烦。
    agentOptions.value = rows.filter((a) => a.status === 'approved')
  } catch (e) {
    agentOptions.value = []
  }
}

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (owner.value) params.owner = owner.value
    if (status.value) params.status = status.value
    const data = await api.listTodos(params)
    items.value = data?.items || []
    total.value = Number(data?.total || 0)
    error.value = ''
  } catch (e) {
    items.value = []
    total.value = 0
    error.value = `读取待办失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function refresh() {
  await Promise.all([load(), loadSummary()])
}

function search() {
  page.value = 1
  load()
}

/** 清空筛选：无条件回全量（不是空态），与常驻层/知识库同一口径。 */
function clearFilter() {
  owner.value = ''
  status.value = ''
  search()
}

function onPage(p) {
  page.value = p
  load()
}

function onPageSize(n) {
  pageSize.value = n
  page.value = 1
  load()
}

/** 开新增弹框（表单在弹框里，页面顶部不再裸铺表单）。 */
function openCreate() {
  createOpen.value = true
}

/**
 * 新增一条待办：打后端 → 关框 → toast → 刷新列表。
 *
 * 🔴 失败时**不关框**：人填了一整张表单，不该因为一次抖动白填。
 */
async function create(payload) {
  creating.value = true
  try {
    await api.createTodo(payload)
    createOpen.value = false
    showToast('已加上')
    page.value = 1
    await refresh()
  } catch (e) {
    showError(`新增失败：${e.message}`)
  } finally {
    creating.value = false
  }
}

async function setStatus(row, status) {
  try {
    await api.setTodoStatus(row.id, status)
    showToast(status === 'done' ? '已标完成' : status === 'obsolete' ? '已标废弃' : '已重新打开')
    await refresh()
  } catch (e) {
    showError(`改状态失败：${e.message}`)
  }
}

function markDone(row) {
  setStatus(row, 'done')
}

function markOpen(row) {
  setStatus(row, 'open')
}

function markObsolete(row) {
  setStatus(row, 'obsolete')
}

/**
 * 批量标完成：**不是危险动作**（标错了每条都能「重新打开」回来），
 * 所以不弹二次确认 —— 一按就改，改完 toast 报结果，失败条数如实回报。
 *
 * 🔴 真删才危险：那才走手输标题那道闸（askDelete）。
 */
async function batchDone() {
  const ids = [...selected.value]
  if (!ids.length) return
  busy.value = true
  let failed = 0
  for (const id of ids) {
    try {
      await api.setTodoStatus(id, 'done')
    } catch (e) {
      failed += 1
    }
  }
  busy.value = false
  selected.value = new Set()
  if (failed) showError(`${failed} 条没改成（其余已标完成）`)
  else showToast(`已标完成 ${ids.length} 条`)
  await refresh()
}

/** 真删：手输标题（后端比对标题原文，防手滑）。 */
async function askDelete(row) {
  const typed = await showPrompt(
    `删除后不可恢复。输入待办标题「${row.title}」确认：`,
    '删除待办',
    { expect: row.title, tip: row.title },
  )
  if (!typed) return
  try {
    await api.deleteTodo(row.id, typed)
    showToast('已删除')
    await refresh()
  } catch (e) {
    showError(`删除失败：${e.message}`)
  }
}

onMounted(async () => {
  await loadAgents()
  await refresh()
})
</script>

<style scoped>
/* 页头操作区：按钮靠右一行（窄屏自动换行） */
.head-acts {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
}
/* 主按钮：强调蓝「胶囊」（圆角取 --r-ctl 那一档，与侧栏选中胶囊同口径） */
.pill-btn {
  border-radius: var(--r-ctl);
}
/* 卡片操作行左侧那组文字链接：成组换行，不许把字竖排 */
.card-links {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  min-width: 0;
}
/* 「标废弃」是次要动作：弱化，但不做成 link（点了就改状态，得看得见） */
.card-link--muted {
  color: var(--text-faint);
}
.tone-danger {
  color: var(--danger);
}
/* ---------------------------------------------------------------- 按钮防折字
 * 窄卡 / 窄屏下按钮放不下就**整组换行**，绝不把字竖排成两行（截图里的折字）。
 * tokens.css 的 .act-btn 没有 nowrap 兜底，这里补上（与 ItemCard 同款口径）。 */
.card-foot {
  flex-wrap: wrap;
  row-gap: 6px;
}
.card-links,
.card-acts {
  flex-wrap: wrap;
  row-gap: 6px;
}
.card-link,
.act-btn {
  white-space: nowrap;
  flex: none;
  display: inline-flex;
  align-items: center;
}
/* SelectionBar 的插槽内容在子组件里，父页的 scoped 选择器够不着 —— :deep 才够得着 */
:deep(.sel-acts) {
  flex-wrap: wrap;
}
:deep(.sel-acts .el-button) {
  white-space: nowrap;
}
</style>
