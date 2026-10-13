<template>
  <div>
    <h2>常驻层（core）</h2>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
      class="alert"
    />
    <el-row :gutter="12" class="cards">
      <el-col :span="6"><StatCard label="条数" :value="stats?.count ?? '-'" unit="条" /></el-col>
      <el-col :span="6"><StatCard label="正文字符" :value="stats?.chars ?? '-'" unit="字符" /></el-col>
      <el-col :span="6"><StatCard label="注入预算" :value="stats?.budget ?? '-'" unit="字符" /></el-col>
      <el-col :span="6">
        <StatCard
          label="预算占用"
          :value="stats ? `${stats.percent}%` : '-'"
          :hint="stats && stats.percent >= 100 ? '已超预算，注入时会截断' : ''"
        />
      </el-col>
    </el-row>

    <el-progress
      v-if="stats"
      :percentage="Math.min(stats.percent, 100)"
      :status="stats.percent >= 100 ? 'exception' : stats.percent >= 80 ? 'warning' : 'success'"
    />

    <el-table :data="items" v-loading="loading" border size="small" class="table">
      <el-table-column prop="key" label="key" width="220" show-overflow-tooltip />
      <el-table-column prop="tags" label="tags" width="140" show-overflow-tooltip />
      <el-table-column prop="content" label="正文（截断）" show-overflow-tooltip />
      <el-table-column prop="updated_at" label="updated_at" width="230" show-overflow-tooltip />
    </el-table>

    <el-pagination
      class="pager"
      layout="total, sizes, prev, pager, next"
      :total="count"
      :page-size="pageSize"
      :current-page="page"
      :page-sizes="[20, 50, 100]"
      @current-change="onPage"
      @size-change="onSize"
    />
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'

const items = ref([])
const stats = ref(null)
const count = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [list, st] = await Promise.all([
      api.coreList({ page: page.value, page_size: pageSize.value }),
      api.coreStats(),
    ])
    items.value = list.items
    count.value = list.count
    stats.value = st
  } catch (e) {
    error.value = `读取常驻层失败：${e.message}`
    ElMessage.error(error.value)
  } finally {
    loading.value = false
  }
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

onMounted(load)
</script>

<style scoped>
.alert {
  margin-bottom: 12px;
}
.cards {
  margin-bottom: 12px;
}
.table {
  margin-top: 12px;
}
.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
</style>
