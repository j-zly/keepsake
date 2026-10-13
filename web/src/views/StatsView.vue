<template>
  <div>
    <h2>看板</h2>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
      class="alert"
    />

    <el-row :gutter="12" class="cards">
      <el-col :span="6"><StatCard label="常驻层条数" :value="data?.core?.count ?? '-'" unit="条" /></el-col>
      <el-col :span="6"><StatCard label="常驻层字符" :value="data?.core?.chars ?? '-'" unit="字符" /></el-col>
      <el-col :span="6">
        <StatCard
          label="预算占用"
          :value="data?.core ? `${data.core.percent}%` : '-'"
          :hint="`预算 ${data?.core?.budget ?? '-'} 字符`"
        />
      </el-col>
      <el-col :span="6">
        <StatCard
          label="检索层碎片"
          :value="data?.retrieval?.count ?? '-'"
          unit="条"
          :hint="data?.retrieval?.truncated ? '已触扫描上限（5000），非全量' : 'keyset 扫描'"
        />
      </el-col>
    </el-row>

    <h3>知识库</h3>
    <el-descriptions :column="3" border size="small" class="kb">
      <el-descriptions-item label="skill_docs">{{ kbText('skill_docs') }}</el-descriptions-item>
      <el-descriptions-item label="documents">{{ kbText('documents') }}</el-descriptions-item>
      <el-descriptions-item label="chunks">{{ kbText('chunks') }}</el-descriptions-item>
    </el-descriptions>

    <h3>记忆类定时任务</h3>
    <el-alert
      v-if="cronError"
      :title="cronError"
      type="error"
      show-icon
      :closable="false"
      class="alert"
    />
    <el-table v-loading="loading" :data="crons" border size="small">
      <el-table-column prop="name" label="name" width="180" />
      <el-table-column prop="script" label="script" width="200" show-overflow-tooltip />
      <el-table-column prop="schedule" label="schedule" width="140" />
      <el-table-column label="enabled" width="90">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
            {{ row.enabled ? '开' : '关' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="last_run_at" label="last_run_at" width="240" />
      <el-table-column prop="last_status" label="last_status" width="140" />
    </el-table>
    <div class="src">来源：{{ cronSource }}</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'

const data = ref(null)
const crons = ref([])
const cronSource = ref('')
const loading = ref(false)
const error = ref('')
const cronError = ref('')

/** kb 段不可用时后端给的是 {available:false, error:...} —— 显示成可见的报错，不是 0。 */
function kbText(field) {
  const kb = data.value?.kb
  if (!kb || kb.available === false) return `不可用：${kb?.message || kb?.error || '未知'}`
  return kb[field] ?? '-'
}

async function load() {
  loading.value = true
  try {
    data.value = await api.stats()
    error.value = ''
  } catch (e) {
    error.value = `读取总览失败：${e.message}`
  }
  try {
    const c = await api.crons()
    crons.value = c.jobs
    cronSource.value = c.source
    cronError.value = c.count ? '' : `没有匹配到记忆类任务（jobs.json：${c.source}）`
  } catch (e) {
    cronError.value = `读取定时任务失败：${e.message}`
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.alert {
  margin-bottom: 12px;
}
.cards {
  margin-bottom: 12px;
}
.kb {
  margin-bottom: 16px;
}
.src {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
