<!--
  知识库：默认视图是**文档列表**（不输词就有数据，P9 起）；P10 起检索是**两路并行** ——
  同一个词同时打 /kb/documents（按标题过滤下面的文档列表）和 /kb/search（切片正文命中）。
  两路各带各的分页与计数，结果行合成「文档 N 条 · 切片 M 条」；**任一有结果就算找得到**。
  清空 ✕ 两路都清、回到默认文档列表（不是空态）；点文档 → 抽屉拉它的切片。

  口径全部来自接口，页面不另算：count / page / page_size 都是后端给的。
  防串用请求序号（utils/kb.js 的 isStale）：快速改词时旧响应只丢弃、不覆盖新结果。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">知识库</h1>
      <p class="page-sub">PG 里的文档与切片；默认按文档浏览，输了词则文档标题与切片正文一起查。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" />
      <StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" />
      <StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" />
    </div>

    <div class="filter-panel">
      <div class="filter-title">检索</div>
      <p class="filter-desc">
        一个词两路同时查：下面的文档列表按标题过滤，切片区按正文命中；任一有结果就算找得到。清空即回到全量文档列表。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="输入检索词：文档标题与切片正文同时查"
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
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </div>
      <!-- 两路计数合并：N/M 来自各自接口的 count，不写死 -->
      <div v-if="searching && !loading" class="filter-result">
        <span>{{ mergeCounts(docCount, hitCount) }}</span>
      </div>
    </div>

    <div v-if="loading" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <!-- 两路都为 0 才算没找到（anyHit）；任一有结果往下走 -->
    <div v-else-if="searching && !anyHit" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
          <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
        </svg>
        <p><b>没找到「{{ q.trim() }}」</b>文档标题和切片正文都没命中 —— 换个更短的核心词试试。</p>
      </div>
    </div>

    <template v-else>
      <!-- 第一路：文档列表（检索态下按标题过滤，原地更新 doc-grid） -->
      <div v-if="searching && docs.length" class="section-title">文档</div>
      <div v-if="docs.length" class="card-grid">
        <ItemCard v-for="doc in docs" :key="doc.doc_id" @lead="openDoc(doc)">
          <template #tag>切片 {{ doc.chunk_count }}</template>
          <template v-if="doc.updated_at" #time>{{ toLocal(doc.updated_at) }}</template>

          <p class="card-title" :title="doc.title">{{ doc.title || '（无标题）' }}</p>
          <p class="card-body mono">{{ doc.doc_id }}</p>

          <template #tags>
            <span class="tag mono">{{ doc.doc_id }}</span>
          </template>

          <template #lead>查看切片 →</template>
        </ItemCard>
      </div>

      <div v-if="!docs.length && !searching" class="panel">
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
            <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
          </svg>
          <p><b>知识库里还没有文档</b>往 documents 表灌入文档后，这里会自动列出标题、更新时间和切片数。</p>
        </div>
      </div>

      <PagerBar
        v-if="docCount"
        :total="docCount"
        :page="page"
        :page-size="pageSize"
        @update:page="onPage"
        @update:page-size="onSize"
      />

      <!-- 第二路：切片命中区（分页独立，走 /kb/search 的 page） -->
      <template v-if="searching">
        <div v-if="hits.length" class="section-title">切片</div>
        <div v-if="hits.length" class="card-grid">
          <ItemCard v-for="row in hits" :key="row.chunk_id" chip-tone="accent" @lead="doCopy(row.chunk_id)">
            <template #tag>rank {{ row.rank }}</template>

            <p class="card-title" :title="row.title">{{ row.title || '（无标题）' }}</p>
            <p class="card-body" :title="row.snippet">{{ row.snippet }}</p>

            <template #tags>
              <span class="tag mono">{{ row.chunk_id }}</span>
            </template>

            <template #lead>复制 chunk_id ↗</template>
          </ItemCard>
        </div>

        <PagerBar
          v-if="hitCount"
          :total="hitCount"
          :page="hitPage"
          :page-size="hitSize"
          @update:page="onHitPage"
          @update:page-size="onHitSize"
        />
      </template>
    </template>

    <!-- 某个文档的切片 -->
    <el-drawer
      v-model="drawer"
      :title="`切片 · ${curDoc?.title || curDoc?.doc_id || ''}`"
      size="min(620px, 92vw)"
    >
      <div class="chunk-box">
        <el-skeleton v-if="chunkLoading" :rows="3" animated />
        <template v-else>
        <p class="chunk-count">
          共 {{ chunkCount }} 条切片 · 第 {{ chunkPage }} 页
        </p>
        <ol class="chunk-list">
          <li v-for="c in chunks" :key="c.chunk_id">
            <div class="chunk-top">
              <span class="tag mono">{{ c.chunk_id }}</span>
              <button class="card-link" @click="doCopy(c.chunk_id)">复制 ↗</button>
            </div>
            <p class="chunk-text">{{ c.snippet || '（空切片）' }}</p>
          </li>
        </ol>
        <p v-if="!chunks.length" class="chunk-count">这个文档下还没有切片。</p>
        <PagerBar
          v-if="chunkCount"
          :total="chunkCount"
          :page="chunkPage"
          :page-size="chunkSize"
          @update:page="onChunkPage"
          @update:page-size="onChunkSize"
        />
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { hasAnyHit, isStale, mergeCounts } from '../utils/kb'

// 每页条数跟着各自 PagerBar 的下拉走 —— 两路各自独立：文档列表 / 切片命中 / 文档内切片
const pageSize = ref(20)
const hitSize = ref(20)
const chunkSize = ref(20)

const summary = ref(null)
const docs = ref([])
const docCount = ref(0)
const page = ref(1)
const hits = ref([])
const hitCount = ref(0)
const hitPage = ref(1)
const q = ref('')
const loading = ref(false)
const error = ref('')

const drawer = ref(false)
const curDoc = ref(null)
const chunks = ref([])
const chunkCount = ref(0)
const chunkPage = ref(1)
const chunkLoading = ref(false)

// 防串：请求序号。发请求前 ++，回来时序号对不上 ⇒ 过期响应直接丢弃（utils/kb.js 的 isStale）。
// 不用 AbortController 是因为 axios 层不接 signal（api 客户端本轮只增不改），序号已足够防覆盖。
let reqSeq = 0

/** 检索框有词 ⇒ 检索态；没词 ⇒ 浏览文档列表（默认视图） */
const searching = computed(() => !!q.value.trim())
/** 「任一有结果就算找得到」：文档命中或切片命中任一不为 0 就算数（两路都 0 才显示空态） */
const anyHit = computed(() => hasAnyHit(docCount.value, hitCount.value))

async function loadSummary() {
  try {
    summary.value = await api.kbSummary()
    error.value = ''
  } catch (e) {
    summary.value = null
    error.value = `知识库不可用：${e.message}（需要 KEEPSAKE_PG_DSN 指向只读实例）`
  }
}

/** 浏览态：全量文档列表（不带 q）—— 默认视图 / 清空后 / 未输词翻页都走这里 */
async function loadDocs() {
  const seq = ++reqSeq
  loading.value = true
  try {
    const data = await api.kbDocuments({ page: page.value, page_size: pageSize.value })
    if (isStale(seq, reqSeq)) return
    docs.value = data.items
    docCount.value = data.count
    error.value = ''
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    docs.value = []
    docCount.value = 0
    error.value = `读取文档列表失败：${e.message}`
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

/**
 * 检索态：一个词两路并行 —— /kb/documents 按标题过滤下面的文档列表，
 * /kb/search 命切片正文。两路各拿各的 page/page_size，结果行再合成计数。
 * 序号不匹配（用户已经改词/清空了）⇒ 整个响应丢弃，绝不覆盖新结果。
 */
async function loadBoth(term) {
  const seq = ++reqSeq
  loading.value = true
  let docRes, hitRes
  try {
    [docRes, hitRes] = await Promise.all([
      api.kbDocuments({ q: term, page: page.value, page_size: pageSize.value }),
      api.kbSearch({ q: term, page: hitPage.value, page_size: hitSize.value }),
    ])
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    error.value = `知识库检索失败：${e.message}`
    loading.value = false
    return
  }
  if (isStale(seq, reqSeq)) return
  docs.value = docRes.items
  docCount.value = docRes.count
  hits.value = hitRes.items
  hitCount.value = hitRes.count
  error.value = ''
  loading.value = false
}

/** 回车与点「搜索」按钮同一条路：两路都重查，页码各回第 1 页 */
async function search() {
  const term = q.value.trim()
  // 空输入直接回车 ⇒ 不发请求、保持当前列表
  if (!term) return
  page.value = 1
  hitPage.value = 1
  await loadBoth(term)
}

/** 清空 ✕：两路都清（切片清零），回到默认文档列表而不是空态 */
function onClear() {
  q.value = ''
  hits.value = []
  hitCount.value = 0
  hitPage.value = 1
  page.value = 1
  loadDocs()
}

function onPage(p) {
  page.value = p
  searching.value ? loadBoth(q.value.trim()) : loadDocs()
}
function onSize(s) {
  pageSize.value = Number(s)
  page.value = 1
  searching.value ? loadBoth(q.value.trim()) : loadDocs()
}
function onHitPage(p) {
  hitPage.value = p
  loadBoth(q.value.trim())
}
function onHitSize(s) {
  hitSize.value = Number(s)
  hitPage.value = 1
  loadBoth(q.value.trim())
}

async function openDoc(doc) {
  curDoc.value = doc
  chunks.value = []
  chunkCount.value = 0
  chunkPage.value = 1
  drawer.value = true
  await loadChunks()
}

async function loadChunks() {
  if (!curDoc.value) return
  chunkLoading.value = true
  try {
    const data = await api.kbDocumentChunks(curDoc.value.doc_id, {
      page: chunkPage.value,
      page_size: chunkSize.value,
    })
    chunks.value = data.items
    chunkCount.value = data.count
  } catch (e) {
    showError(`读取切片失败：${e.message}`)
  } finally {
    chunkLoading.value = false
  }
}

function onChunkPage(p) {
  chunkPage.value = p
  loadChunks()
}
function onChunkSize(s) {
  chunkSize.value = Number(s)
  chunkPage.value = 1
  loadChunks()
}

async function doCopy(id) {
  const ok = await copyText(id)
  ok ? showToast(`已复制 chunk_id：${id}`) : showError('复制失败，请手动选中复制')
}

onMounted(() => {
  loadSummary()
  loadDocs()
})
</script>

<style scoped>
.pad {
  padding: 20px;
}
.chunk-box {
  min-height: 120px;
}
.chunk-count {
  margin: 0 0 12px;
  color: var(--text-faint);
  font-size: 12.5px;
}
.chunk-list {
  margin: 0 0 16px;
  padding-left: 20px;
  color: var(--text);
  font-size: 13px;
}
.chunk-list li + li {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.chunk-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}
.chunk-text {
  margin: 0;
  color: var(--text-dim);
  line-height: 1.7;
  white-space: pre-wrap;
}
</style>
