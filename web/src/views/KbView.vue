<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">知识库</h1>
      <p class="page-sub">PG 里的文档切片；检索有 tsvector 列走 tsquery，否则退化成 ILIKE。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" />
      <StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" />
      <StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" />
    </div>

    <div class="filter-panel">
      <div class="filter-title">检索文档切片</div>
      <p class="filter-desc">命中的切片以卡片铺开：左边标题、右边命中片段，chunk_id 一键复制。</p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="全文检索文档标题与切片正文"
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
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </div>
      <div v-if="result" class="filter-result">
        <span>共 {{ result.items.length }} 条命中 · 每页 {{ pageSize }}</span>
      </div>
    </div>

    <div v-if="loading && !result" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <div v-else-if="result && result.items.length" class="card-grid">
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

    <div v-else-if="result" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
          <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 1 1.8-1.8h4.8Z" stroke-linejoin="round" />
        </svg>
        <p><b>知识库里没有命中这条检索</b>换个更短的核心词试试 —— 切片检索比整篇全文更容易漏召回。</p>
      </div>
    </div>

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
import ItemCard from '../components/ItemCard.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'

// 与接口口径一致：知识库检索一次一页，不给页码切换
const pageSize = 20

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
    result.value = await api.kbSearch({ q: q.value.trim(), page: 1, page_size: pageSize })
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
.pad {
  padding: 20px;
}
</style>
