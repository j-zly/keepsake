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
  页面这边再叠一层界面确认（手输标题防手滑）。

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

    <!-- 新增 + 筛选 -->
    <div class="filter-panel">
      <div class="filter-title">新增 / 筛选</div>
      <p class="filter-desc">
        归属必须是**在册且已批准**的代理；到期日填 ISO 日期（YYYY-MM-DD），留空表示不限期。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="form.title"
          class="grow"
          placeholder="要做什么（标题）"
          maxlength="200"
        />
        <el-select v-model="form.owner" class="sel" placeholder="归属代理" filterable>
          <el-option
            v-for="a in agentOptions"
            :key="a.agent_id"
            :label="a.name ? `${a.agent_id}（${a.name}）` : a.agent_id"
            :value="a.agent_id"
          />
        </el-select>
        <el-date-picker
          v-model="form.due"
          class="sel"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="到期日（可空）"
        />
        <el-button type="primary" :loading="creating" @click="create">加一条</el-button>
      </div>
      <el-input
        v-model="form.detail"
        class="detail-input"
        type="textarea"
        :rows="2"
        maxlength="4000"
        placeholder="正文（可空）"
      />
      <div class="filter-result">
        <span>共 {{ total }} 条 · 每页 {{ pageSize }}</span>
        <div class="acts">
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
      <el-button :disabled="!selCount || busy" :loading="busy" @click="askBatchDone">
        批量标完成 {{ selCount }} 条
      </el-button>
    </SelectionBar>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <div v-else-if="!items.length" class="panel pad">
      <div class="empty">
        <b>还没有待办</b>
        <span>上面「加一条」写下第一件事，或让代理用 MCP 的 todo_add 记。</span>
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
          <div class="card-foot">
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
            <button class="act-btn act-btn--danger" @click="askDelete(row)">删除</button>
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
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import PagerBar from '../components/PagerBar.vue'
import SelectionBar from '../components/SelectionBar.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { loadAgentRows } from '../utils/agents'
import { showConfirm, showError, showPrompt, showToast } from '../utils/dialog'
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

const form = ref({ title: '', detail: '', owner: '', due: '' })

/** 三段分组（顺序：开放 → 已完成 → 已废弃）。 */
const grouped = computed(() => groupByStatus(items.value))
const groups = computed(() => [
  { key: 'open', label: '开放', rows: grouped.value.open },
  { key: 'done', label: '已完成', rows: grouped.value.done },
  { key: 'obsolete', label: '已废弃', rows: grouped.value.obsolete },
])

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

function onPage(p) {
  page.value = p
  load()
}

function onPageSize(n) {
  pageSize.value = n
  page.value = 1
  load()
}

async function create() {
  const title = (form.value.title || '').trim()
  if (!title) {
    showError('标题不能为空')
    return
  }
  creating.value = true
  try {
    await api.createTodo({
      title,
      detail: form.value.detail || '',
      owner: form.value.owner || '',
      due: form.value.due || '',
    })
    showToast('已加上')
    form.value = { title: '', detail: '', owner: form.value.owner, due: '' }
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

/** 批量标完成：先弹框说明影响面，再逐条改（一条失败不吞，失败条数如实回报）。 */
async function askBatchDone() {
  const ids = [...selected.value]
  if (!ids.length) return
  const ok = await showConfirm(
    `把选中的 ${ids.length} 条待办标成「已完成」？`,
    '批量标完成',
  )
  if (!ok) return
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
/* 新增表单的正文框：占满一行，不跟上面的控件挤在一排 */
.detail-input {
  margin-top: 10px;
}
/* 「标废弃」是次要动作：弱化，但不做成 link（点了就改状态，得看得见） */
.card-link--muted {
  color: var(--text-faint);
}
.tone-danger {
  color: var(--danger);
}
</style>
