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

    <h3>记忆维护（合并 / 遗忘）</h3>
    <el-alert
      type="warning"
      show-icon
      :closable="false"
      class="alert"
      title="dry-run 零写入，可放心点；实跑必须手输当前计数，且会先自动备份受影响碎片。"
    />
    <el-space wrap class="ops">
      <el-button :loading="busy === 'merge-dry'" @click="runMerge(true)">合并 · dry-run</el-button>
      <el-button type="warning" :loading="busy === 'merge'" @click="runMerge(false)">合并 · 实跑</el-button>
      <el-button :loading="busy === 'forget-dry'" @click="runForget(true)">遗忘 · dry-run</el-button>
      <el-button type="danger" :loading="busy === 'forget'" @click="runForget(false)">遗忘 · 实跑</el-button>
    </el-space>
    <pre v-if="opResult" class="opresult">{{ opResult }}</pre>

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
import { showConfirm, showError, showPrompt, showToast } from '../utils/dialog'

const data = ref(null)
const crons = ref([])
const cronSource = ref('')
const loading = ref(false)
const error = ref('')
const cronError = ref('')
const busy = ref('')
const opResult = ref('')

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

/**
 * 合并 / 遗忘。**默认 dry-run**（零写入）；实跑要手输「当前计数」——
 * 与后端 confirm 串同口径（core.count / retrieval.count），防手滑。
 * 实跑前后都会把受影响碎片全量导出到备份目录，路径回显在结果里。
 */
async function runMerge(dryRun) {
  const n = data.value?.core?.count
  await runOps('merge', dryRun, String(n), `合并 · 实跑会改写碎片（当前常驻 ${n} 条）`)
}

async function runForget(dryRun) {
  const n = data.value?.retrieval?.count
  await runOps('forget', dryRun, String(n), `遗忘 · 实跑会物理删除过期碎片（当前共 ${n} 条）`)
}

async function runOps(kind, dryRun, expect, warnText) {
  let confirm = undefined
  if (!dryRun) {
    const ok = await showConfirm(warnText, '二次确认', 'warning')
    if (!ok) return
    const got = await showPrompt('请手输当前计数以确认（防手滑）：', '确认实跑', {
      expect,
      tip: expect,
    })
    if (!got) return
    confirm = got
  }
  busy.value = dryRun ? `${kind}-dry` : kind
  try {
    const r = await api[kind]({ dry_run: dryRun, confirm })
    opResult.value = JSON.stringify(r, null, 2)
    showToast(
      dryRun
        ? `${kind} dry-run 完成：${JSON.stringify(r.stats)}`
        : `${kind} 实跑完成：${JSON.stringify(r.stats)}`,
    )
    load()
  } catch (e) {
    showError(`${kind} ${dryRun ? 'dry-run' : '实跑'}失败：${e.message}`)
  } finally {
    busy.value = ''
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
.ops {
  margin-bottom: 8px;
}
.opresult {
  white-space: pre-wrap;
  word-break: break-all;
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 4px;
  max-height: 320px;
  overflow: auto;
  font-size: 12px;
}
.src {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
