<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">知识库</h1>
        <p class="page-sub">PG 里的文档切片；检索有 tsvector 列走 tsquery，否则退化成 ILIKE。</p>
      </div>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="cards">
      <StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" />
      <StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" />
      <StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" />
    </div>

    <div class="toolbar">
      <el-input
        v-model="q"
        class="grow search"
        placeholder="全文检索文档标题与切片正文"
        clearable
        @keyup.enter="search"
      />
      <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
    </div>

    <div v-if="loading && !result" class="panel skeleton-wrap">
      <el-skeleton :rows="4" animated />
    </div>

    <el-table v-else-if="result" :data="result.items" max-height="560" class="table">
      <el-table-column label="rank" width="80">
        <template #default="{ row }"><span class="mono num dim">{{ row.rank }}</span></template>
      </el-table-column>
      <el-table-column label="文档标题" width="240">
        <template #default="{ row }">
          <span class="cell-clip" :title="row.title">{{ row.title }}</span>
        </template>
      </el-table-column>
      <el-table-column label="命中片段">
        <template #default="{ row }">
          <span class="cell-clip" :title="row.snippet">{{ row.snippet }}</span>
        </template>
      </el-table-column>
      <el-table-column label="chunk_id" width="200">
        <template #default="{ row }">
          <span class="cell-key">
            <span class="k mono faint" :title="row.chunk_id">{{ row.chunk_id }}</span>
            <button class="copy-btn" title="复制 chunk_id" @click="doCopy(row.chunk_id)">
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
                stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
                <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
                <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
              </svg>
            </button>
          </span>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
            <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 1 1.8-1.8h4.8Z" stroke-linejoin="round" />
          </svg>
          <p><b>知识库里没有命中这条检索</b>换个更短的核心词试试 —— 切片检索比整篇全文更容易漏召回。</p>
        </div>
      </template>
    </el-table>

    <div v-else-if="!loading" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="10.5" cy="10.5" r="6" />
          <path d="m15 15 4.5 4.5" stroke-linecap="round" />
        </svg>
        <p><b>输入一个检索词开始</b>上方三个计数是知识库的存量规模，搜完的结果会直接铺在下面。</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'

const summary = ref(null)
const result = ref(null)
const q = ref('')
const loading = ref(false)
const error = ref('')

async function loadSummary() {
  try {
    summary.value = await api.kbSummary()
    error.value = ''
  } catch (e) {
    summary.value = null
    error.value = `知识库不可用：${e.message}（需要 KEEPSAKE_PG_DSN 指向只读实例）`
  }
}

async function search() {
  if (!q.value.trim()) {
    result.value = null
    return
  }
  loading.value = true
  try {
    result.value = await api.kbSearch({ q: q.value.trim(), page: 1, page_size: 20 })
  } catch (e) {
    error.value = `知识库检索失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function doCopy(id) {
  const ok = await copyText(id)
  ok ? showToast(`已复制 chunk_id：${id}`) : showError('复制失败，请手动选中复制')
}

onMounted(loadSummary)
</script>

<style scoped>
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}
.search {
  max-width: 460px;
}
.toolbar {
  padding: 10px 12px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  box-shadow: var(--inset);
}
.table {
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  box-shadow: var(--inset);
}
.skeleton-wrap {
  padding: 16px;
}
.panel .empty {
  padding: 48px 16px;
}
</style>