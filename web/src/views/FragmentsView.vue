<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">记忆条目</h1>
      <p class="page-sub">全库记忆条目，按 BM25 全文检索召回；点卡片进抽屉看全文与编辑。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="filter-panel">
      <div class="filter-title">检索记忆条目</div>
      <p class="filter-desc">正文走 BM25 全文检索（keepsake storage.search）；留空即全库浏览。</p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="搜索正文（BM25 全文检索）"
          clearable
          @keyup.enter="search"
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
        <el-select
          v-model="tag"
          class="sel"
          placeholder="标签"
          filterable
          allow-create
          default-first-option
          clearable
        />
        <el-button type="primary" :loading="loading" @click="search">查询</el-button>
      </div>
      <div class="filter-result">
        <span>共 {{ count === null ? '—' : count }} 条匹配 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <!-- 清除筛选 = 清空 q/tag 再查；重置 = 保留条件、回到第 1 页重查 -->
          <button class="soft-btn" :disabled="!dirty" @click="reset">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="search">重置</button>
        </div>
      </div>
    </div>

    <div v-if="count === null && !items.length" class="banner">
      当前是全库浏览：存储层没有 count 原语，总条数未知，按 has_more 翻页。
    </div>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!items.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="10.5" cy="10.5" r="6" />
          <path d="m15 15 4.5 4.5" stroke-linecap="round" />
        </svg>
        <p v-if="dirty"><b>没召回到记忆条目</b>换个词试试，或把标签清空做全库浏览。</p>
        <p v-else><b>这里什么都没有</b>上方输入关键词召回，或直接翻页浏览全库。</p>
      </div>
    </div>

    <div v-else class="card-grid">
      <ItemCard v-for="row in items" :key="row.key" chip-tone="accent" @lead="open(row)">
        <template #tag>score {{ fmtScore(row.score) }}</template>
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
          <button class="act-btn" @click="promote(row)">升常驻</button>
          <button class="act-btn act-btn--danger" @click="archive(row)">归档</button>
        </template>
      </ItemCard>
    </div>

    <PagerBar
      :total="pagerTotal"
      :page="page"
      :page-size="pageSize"
      :unknown="count === null"
      @update:page="onPage"
      @update:page-size="onSize"
    />

    <DetailDrawer v-model="drawer" :detail="detail" @saved="load" />

    <!-- 归档确认（危险操作）：红竖条 + 逐条后果 + 手输 key 末 8 位 -->
    <ConfirmDialog
      v-model="archiving"
      title="归档这条记忆条目？"
      :consequences="archiveConsequences"
      :expect="archiveExpect"
      confirm-text="归档"
      :busy="busyArchive"
      @confirm="doArchive"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DetailDrawer from '../components/DetailDrawer.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showConfirm, showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

// count 未知（全库浏览）时按 has_more 撑出「下一页」：给一个假的下一页总数，
// 有下一页才显示 total=page*page_size+1，翻到那儿再由 has_more=false 收口。
const pagerTotal = computed(() =>
  count.value ?? (hasMore.value ? page.value * pageSize.value + 1 : items.value.length),
)

const q = ref('')
const tag = ref('')
const items = ref([])
const count = ref(0)
const hasMore = ref(false)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')
const drawer = ref(false)
const detail = ref(null)

// 归档确认（危险操作）走 ConfirmDialog：红色竖条 + 逐条后果 + 手输 key 末 8 位
const archiving = ref(false)
const archiveRow = ref(null)
const busyArchive = ref(false)
const archiveExpect = computed(() => (archiveRow.value ? archiveRow.value.key.slice(-8) : ''))
const archiveConsequences = computed(() =>
  archiveRow.value
    ? [
        `把 ${archiveRow.value.key} 置为失效：不再被召回，也不占注入预算。`,
        '不物理删除，备份留在 /root/.hermes/backup/console_ops/，需要时可以回滚。',
        '写操作会落审计日志，记录是谁在什么时候归档的。',
      ]
    : [],
)

/** 有筛选条件在生效吗：空态文案与「清除筛选」按钮都看它 */
const dirty = computed(() => !!(q.value || tag.value))

/** tags 是逗号分隔的一串，拆成一个个 chip 展示 */
function splitTags(s) {
  return (s || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

/** score 是浮点，卡片上对齐到 4 位，别铺一串尾零。 */
function fmtScore(v) {
  return typeof v === 'number' ? v.toFixed(4) : (v ?? '—')
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.fragments({
      // el-select 被清空时给的是 undefined 而不是 ''，统一兜一层再 trim
      q: (q.value || '').trim() || undefined,
      tag: (tag.value || '').trim() || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    items.value = data.items
    count.value = data.count
    hasMore.value = data.has_more
  } catch (e) {
    error.value = `读取记忆条目失败：${e.message}`
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}
function reset() {
  q.value = ''
  tag.value = ''
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
async function open(row) {
  try {
    detail.value = await api.fragment(row.key)
    drawer.value = true
  } catch (e) {
    ElMessage.error(`读取详情失败：${e.message}`)
  }
}

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** 升常驻：打上 core 标签（每轮 system_prompt 全量注入）。 */
async function promote(row) {
  const ok = await showConfirm(
    `确认把 ${row.key} 升为常驻？之后每轮都会注入这条，占用注入预算。`,
    '升常驻',
  )
  if (!ok) return
  try {
    await api.promote(row.key)
    showToast(`已升常驻：${row.key}`)
    load()
  } catch (e) {
    showError(`升常驻失败：${e.message}`)
  }
}

/** 归档：置失效（不物理删）。要求手输 key 末 8 位 —— 防手滑。 */
function archive(row) {
  archiveRow.value = row
  archiving.value = true
}

async function doArchive(typed) {
  const row = archiveRow.value
  if (!row) return
  busyArchive.value = true
  try {
    await api.archive(row.key, typed)
    archiving.value = false
    showToast(`已归档：${row.key}`)
    load()
  } catch (e) {
    showError(`归档失败：${e.message}`)
  } finally {
    busyArchive.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pad {
  padding: 20px;
}
</style>
