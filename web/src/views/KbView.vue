<template>
  <div>
    <h2>知识库</h2>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
      class="alert"
    />
    <el-row :gutter="12" class="cards">
      <el-col :span="8"><StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" /></el-col>
      <el-col :span="8"><StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" /></el-col>
      <el-col :span="8"><StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" /></el-col>
    </el-row>

    <el-form :inline="true" class="bar" @submit.prevent="search">
      <el-form-item label="全文检索">
        <el-input
          v-model="q"
          placeholder="有 tsvector 列走 tsquery，否则 ILIKE"
          clearable
          style="width: 320px"
          @keyup.enter="search"
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </el-form-item>
    </el-form>

    <el-table v-if="result" :data="result.items" border size="small">
      <el-table-column prop="rank" label="rank" width="100" />
      <el-table-column prop="title" label="文档标题" width="220" show-overflow-tooltip />
      <el-table-column prop="snippet" label="命中片段" show-overflow-tooltip />
      <el-table-column prop="chunk_id" label="chunk_id" width="180" show-overflow-tooltip />
    </el-table>
    <el-empty v-else-if="!loading" description="输入检索词查看知识库命中" />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'

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

onMounted(loadSummary)
</script>

<style scoped>
.alert {
  margin-bottom: 12px;
}
.cards {
  margin-bottom: 12px;
}
.bar {
  margin-top: 8px;
}
</style>
