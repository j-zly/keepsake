<!--
  知识库：默认视图是**文档列表**（不输词就有数据，P9 起）；
  检索框改为可选 —— 输了词走既有 /kb/search 展示切片命中，清空即回到列表。
  点某个文档 → 右侧抽屉拉该文档的切片（走 /kb/documents/{doc_id}/chunks，带分页）。

  口径全部来自接口，页面不另算：count / page / page_size 都是后端给的。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">知识库</h1>
      <p class="page-sub">PG 里的文档与切片；默认按文档浏览，输了词则检索切片正文。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" />
      <StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" />
      <StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" />
    </div>

    <div class="filter-panel">
      <div class="filter-title">{{ searching ? '检索文档切片' : '文档列表' }}</div>
      <p class="filter-desc">
        {{
          searching
            ? '命中的切片以卡片铺开：左边标题、右边命中片段，chunk_id 一键复制。'
            : '不输词也能看：下面是文档列表，点开任一文档可以看到它的切片。'
        }}
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="输入检索词查切片；清空回到文档列表"
          clearable
          @keyup.enter="search"
          @clear="search"
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
      <div v-if="searching && result" class="filter-result">
        <span>共 {{ result.count }} 条命中 · 第 {{ result.page }} 页 · 每页 {{ pageSize }}</span>
      </div>
    </div>

    <!-- 检索态：切片命中卡片 -->
    <div v-if="loading" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <template v-else-if="searching">
      <div v-if="result && result.items.length" class="card-grid">
        <ItemCard v-for="row in result.items" :key="row.chunk_id" chip-tone="accent" @lead="doCopy(row.chunk_id)">
          <template #tag>rank {{ row.rank }}</template>

          <p class="card-title" :title="row.title">{{ row.title || '（无标题）' }}</p>
          <p class="card-body" :title="row.snippet">{{ row.snippet }}</p>

          <template #tags>
            <span class="tag mono">{{ row.chunk_id }}</span>
          </template>

          <template #lead>复制 chunk_id ↗</template>
        </ItemCard>
      </div>

      <div v-else class="panel">
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
            <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
          </svg>
          <p><b>知识库里没有命中这条检索</b>换个更短的核心词试试 —— 切片检索比整篇全文更容易漏召回。</p>
        </div>
      </div>
    </template>

    <!-- 浏览器：文档列表（默认视图） -->
    <template v-else>
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

      <div v-else class="panel">
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
            <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
          </svg>
          <p><b>知识库里还没有文档</b>往 documents 表灌入文档后，这里会自动列出标题、更新时间和切片数。</p>
        </div>
      </div>

      <PagerBar
        :total="docCount"
        :page="page"
        :page-size="pageSize"
        @update:page="onPage"
        @update:page-size="onSize"
      />
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

// 每页条数跟着 PagerBar 的下拉走，所以是 ref（两个独立状态：文档列表 / 文档切片）
const pageSize = ref(20)
const chunkSize = ref(20)

const summary = ref(null)
const docs = ref([])
const docCount = ref(0)
const page = ref(1)
const result = ref(null)
const q = ref('')
const loading = ref(false)
const error = ref('')

const drawer = ref(false)
const curDoc = ref(null)
const chunks = ref([])
const chunkCount = ref(0)
const chunkPage = ref(1)
const chunkLoading = ref(false)

/** 检索框有词 ⇒ 检索态；没词 ⇒ 浏览文档列表（默认视图） */
const searching = computed(() => !!q.value.trim())

async function loadSummary() {
  try {
    summary.value = await api.kbSummary()
    error.value = ''
  } catch (e) {
    summary.value = null
    error.value = `知识库不可用：${e.message}（需要 KEEPSAKE_PG_DSN 指向只读实例）`
  }
}

/** 文档列表：q 丢了就不带（后端 q 可选，不带就是全量浏览） */
async function loadDocs() {
  loading.value = true
  try {
    const data = await api.kbDocuments({
      q: q.value.trim() || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    docs.value = data.items
    docCount.value = data.count
    error.value = ''
  } catch (e) {
    docs.value = []
    docCount.value = 0
    error.value = `读取文档列表失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function search() {
  page.value = 1
  result.value = null
  if (!searching.value) {
    // 清空检索词 ⇒ 回到文档列表（不再是空态）
    await loadDocs()
    return
  }
  loading.value = true
  try {
    result.value = await api.kbSearch({ q: q.value.trim(), page: 1, page_size: pageSize.value })
    error.value = ''
  } catch (e) {
    error.value = `知识库检索失败：${e.message}`
  } finally {
    loading.value = false
  }
}

function onPage(p) {
  page.value = p
  loadDocs()
}
function onSize(s) {
  pageSize.value = Number(s)
  page.value = 1
  loadDocs()
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
