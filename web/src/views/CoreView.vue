<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">常驻层</h1>
      <p class="page-sub">带 core 标签的记忆条目，每轮 system_prompt 全量注入，占用注入预算。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="条数" :value="stats?.count ?? '-'" unit="条" />
      <StatCard label="正文字符" :value="stats?.chars ?? '-'" unit="字符" />
      <StatCard label="注入预算" :value="stats?.budget ?? '-'" unit="字符" />
      <StatCard
        label="预算占用"
        :value="stats ? `${stats.percent}%` : '-'"
        :percent="stats ? stats.percent : null"
        :tone="budgetTone"
        :hint="stats && stats.percent >= 100 ? '已超预算，注入时会截断' : ''"
      />
    </div>

    <div class="filter-panel">
      <div class="filter-title">常驻记忆</div>
      <p class="filter-desc">
        卡片给 3 行摘要，悬停看完整正文；点 key 旁的图标复制，「查看全文 ↗」进抽屉看全文与编辑。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="搜索常驻正文或标签（清空回全量）"
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
        <span>共 {{ count }} 条匹配 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <button class="soft-btn" :disabled="!dirty" @click="onClear">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="load">刷新</button>
        </div>
      </div>
    </div>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!items.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M12 3.5 3.5 7.5 12 11.5l8.5-4-8.5-4Z" stroke-linejoin="round" />
          <path d="M4.6 12 12 15.6 19.4 12M4.6 16.5 12 20l7.4-3.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p v-if="dirty"><b>没匹配到常驻记忆</b>换个更短的核心词试试，或点「清除筛选」回全量常驻层。</p>
        <p v-else><b>常驻层还是空的</b>到「记忆条目」把要每轮注入的记忆点「升常驻」，它们就会出现在这里。</p>
      </div>
    </div>

    <div v-else class="card-grid">
      <ItemCard v-for="row in items" :key="row.key" chip-tone="accent" @lead="open(row)">
        <template #tag>core</template>
        <template #time>{{ toLocal(row.updated_at) }}</template>

        <span class="cell-key">
          <span class="k" :title="row.key">{{ row.key }}</span>
          <button class="copy-btn" title="复制 key" @click.stop="doCopy(row.key)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
              <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
            </svg>
          </button>
        </span>
        <p class="card-body" :title="row.content">{{ row.content }}</p>

        <template #tags>
          <span v-for="t in splitTags(row.tags)" :key="t" class="tag">{{ t }}</span>
        </template>

        <template #lead>查看全文 ↗</template>
        <template #actions>
          <button class="act-btn act-btn--danger" @click="demote(row)">降常驻</button>
        </template>
      </ItemCard>
    </div>

    <PagerBar
      :total="count"
      :page="page"
      :page-size="pageSize"
      @update:page="onPage"
      @update:page-size="onSize"
    />

    <DetailDrawer
      v-model="drawer"
      :detail="detail"
      :allow-archive="false"
      @saved="load"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import DetailDrawer from '../components/DetailDrawer.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showConfirm, showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { isStale } from '../utils/kb'

const q = ref('')
const items = ref([])
const stats = ref(null)
const count = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')
const drawer = ref(false)
const detail = ref(null)

/** 有搜索词在生效吗：空态分支与「清除筛选」按钮都看它 */
const dirty = computed(() => !!(q.value || '').trim())

/** 占用越高越红：80% 起转蓝（与主色同一套），过 100% 红 —— 超预算注入会截断。 */
const budgetTone = computed(() => {
  const p = stats.value?.percent
  if (p === undefined || p === null) return ''
  if (p >= 100) return 'danger'
  if (p >= 80) return 'accent'
  return 'ok'
})

/** tags 是逗号分隔的一串，拆成一个个 chip 展示 */
function splitTags(s) {
  return (s || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

// 请求序号：连续改词时旧响应只丢弃，不覆盖新结果（照抄 KbView 的收口口径）
let reqSeq = 0

async function load() {
  const seq = ++reqSeq
  loading.value = true
  error.value = ''
  try {
    const [list, st] = await Promise.all([
      // q 只透传，匹配语义在服务端（正文+标签子串，大小写不敏感）
      api.coreList({
        q: (q.value || '').trim() || undefined,
        page: page.value,
        page_size: pageSize.value,
      }),
      api.coreStats(),
    ])
    if (isStale(seq, reqSeq)) return
    items.value = list.items
    count.value = list.count
    stats.value = st
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    error.value = `读取常驻层失败：${e.message}`
    ElMessage.error(error.value)
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}
/** 清空 ✕ / 「清除筛选」：无条件回全量常驻层（不是空态），与 KbView 的清空口径一致 */
function onClear() {
  q.value = ''
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

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** 打开全文抽屉：详情按 key 单查，与记忆条目页同一个接口。 */
async function open(row) {
  try {
    detail.value = await api.fragment(row.key)
    drawer.value = true
  } catch (e) {
    showError(`读取详情失败：${e.message}`)
  }
}

/** 降常驻：去掉 core 标签（不再每轮注入）。二次确认 + 成功后刷新 + toast。 */
async function demote(row) {
  const ok = await showConfirm(
    `确认把 ${row.key} 移出常驻层？之后每轮不再注入这条记忆（写前会自动备份，可回滚）。`,
    '降常驻',
  )
  if (!ok) return
  try {
    const r = await api.demote(row.key)
    showToast(`已降常驻：${row.key}（tags=${r.tags || '空'}）`)
    load()
  } catch (e) {
    showError(`降常驻失败：${e.message}`)
  }
}

onMounted(load)
</script>

<style scoped>
.pad {
  padding: 20px;
}
</style>
