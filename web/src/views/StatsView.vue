<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">看板</h1>
      <p class="page-sub">三个域的存量与预算，加上记忆维护（合并 / 遗忘）与定时任务。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="常驻层条数" :value="data?.core?.count ?? '-'" unit="条" />
      <StatCard label="常驻层字符" :value="data?.core?.chars ?? '-'" unit="字符" />
      <StatCard
        label="预算占用"
        :value="data?.core ? `${data.core.percent}%` : '-'"
        :percent="data?.core?.percent ?? null"
        :tone="budgetTone"
        :hint="`预算 ${data?.core?.budget ?? '-'} 字符`"
      />
      <StatCard
        label="记忆条目"
        :value="data?.retrieval?.count ?? '-'"
        unit="条"
        :hint="data?.retrieval?.truncated ? '已触扫描上限（5000），非全量' : 'keyset 扫描'"
      />
    </div>

    <h3 class="section-title">知识库</h3>
    <div class="stat-grid">
      <StatCard label="skill_docs" :value="kb.skill_docs.value" :hint="kb.skill_docs.hint" />
      <StatCard label="documents" :value="kb.documents.value" :hint="kb.documents.hint" />
      <StatCard label="chunks" :value="kb.chunks.value" :hint="kb.chunks.hint" />
    </div>

    <h3 class="section-title">记忆维护（合并 / 遗忘）</h3>
    <div class="banner">
      dry-run 零写入，可放心点；实跑必须手输当前计数，且会先自动备份受影响记忆条目。
    </div>
    <div class="ops">
      <el-button :loading="busy === 'merge-dry'" @click="runMerge(true)">合并 · dry-run</el-button>
      <el-button :loading="busy === 'forget-dry'" @click="runForget(true)">遗忘 · dry-run</el-button>
      <span class="ops-gap"></span>
      <el-button type="danger" :loading="busy === 'merge'" @click="runMerge(false)">合并 · 实跑</el-button>
      <el-button type="danger" :loading="busy === 'forget'" @click="runForget(false)">遗忘 · 实跑</el-button>
    </div>
    <pre v-if="opResult" class="pre">{{ opResult }}</pre>

    <h3 class="section-title">记忆类定时任务</h3>
    <div v-if="cronError" class="banner banner--danger">{{ cronError }}</div>

    <div v-if="loading && !crons.length" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <div v-else-if="!crons.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="12" cy="12" r="8" />
          <path d="M12 7.4V12l3 1.8" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p><b>没有匹配到记忆类任务</b>它们来自 cron 的 jobs.json，不在这个文件里就查不到。</p>
      </div>
    </div>

    <div v-else class="card-grid">
      <ItemCard
        v-for="row in crons"
        :key="row.name"
        :chip-tone="row.enabled ? 'accent' : ''"
        @lead="doCopy(row.script)"
      >
        <template #tag>{{ row.enabled ? '已启用' : '已停用' }}</template>
        <template #time>{{ toLocal(row.last_run_at) }}</template>

        <p class="card-title">{{ row.name }}</p>
        <span class="cell-key">
          <span class="k mono" :title="row.script">{{ row.script }}</span>
          <button class="copy-btn" title="复制脚本路径" @click.stop="doCopy(row.script)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
              <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
            </svg>
          </button>
        </span>

        <template #tags>
          <span class="tag mono">{{ row.schedule }}</span>
          <span v-if="row.last_status" class="tag mono">{{ row.last_status }}</span>
        </template>

        <template #lead>复制脚本路径 ↗</template>
      </ItemCard>
    </div>
    <div class="src">来源：{{ cronSource }}</div>

    <ConfirmDialog
      v-model="confirmOpen"
      :title="confirmTitle"
      :consequences="confirmConsequences"
      :expect="confirmExpect"
      confirm-text="确认实跑"
      :busy="busy === confirmKind"
      @confirm="doOps"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

const data = ref(null)
const crons = ref([])
const cronSource = ref('')
const loading = ref(false)
const error = ref('')
const cronError = ref('')
const busy = ref('')
const opResult = ref('')

/** 危险实跑（合并 / 遗忘）的确认态 */
const confirmOpen = ref(false)
const confirmKind = ref('')
const confirmExpect = ref('')
const confirmTitle = ref('')
const confirmConsequences = ref([])

const budgetTone = computed(() => {
  const p = data.value?.core?.percent
  if (p === undefined || p === null) return ''
  if (p >= 100) return 'danger'
  if (p >= 80) return 'accent'
  return 'ok'
})

/**
 * kb 段不可用时后端给的是 {available:false, error:...} —— 显示成可见的报错，不是 0。
 * 报错文案太长，放不进 32px 的数字位，所以数字位写「不可用」、原文进 hint。
 */
function kbCard(field) {
  const kb = data.value?.kb
  if (!kb || kb.available === false) {
    return { value: '不可用', hint: kb?.message || kb?.error || '未知' }
  }
  return { value: kb[field] ?? '-', hint: '' }
}
const kb = computed(() => ({
  skill_docs: kbCard('skill_docs'),
  documents: kbCard('documents'),
  chunks: kbCard('chunks'),
}))

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

async function doCopy(text) {
  const ok = await copyText(text)
  ok ? showToast('已复制') : showError('复制失败，请手动选中复制')
}

/**
 * 合并 / 遗忘。**默认 dry-run**（零写入）；实跑要手输「当前计数」——
 * 与后端 confirm 串同口径（core.count / retrieval.count），防手滑。
 * 实跑前后都会把受影响记忆条目全量导出到备份目录，路径回显在结果里。
 */
function runMerge(dryRun) {
  const n = data.value?.core?.count
  if (dryRun) return runOps('merge', true)
  confirmKind.value = 'merge'
  confirmExpect.value = String(n)
  confirmTitle.value = '合并 · 实跑'
  confirmConsequences.value = [
    `会按相似度合并重叠的记忆条目，当前常驻层共 ${n} 条。`,
    '被合并掉的条目正文会被改写，原始全文先导出到备份目录。',
    '手输的计数会作为 confirm 串发给后端，对不上直接被拒。',
  ]
  confirmOpen.value = true
}

function runForget(dryRun) {
  const n = data.value?.retrieval?.count
  if (dryRun) return runOps('forget', true)
  confirmKind.value = 'forget'
  confirmExpect.value = String(n)
  confirmTitle.value = '遗忘 · 实跑'
  confirmConsequences.value = [
    `会物理删除过期记忆条目，当前记忆条目共 ${n} 条。`,
    '这一步是真删：备份目录里留有全文，但控制台没有恢复入口。',
    '手输的计数会作为 confirm 串发给后端，对不上直接被拒。',
  ]
  confirmOpen.value = true
}

/** ConfirmDialog 确认回调：把用户手输的串原样当 confirm 发给后端。 */
async function doOps(typed) {
  confirmOpen.value = false
  await runOps(confirmKind.value, false, typed)
}

async function runOps(kind, dryRun, confirm) {
  busy.value = dryRun ? `${kind}-dry` : kind
  try {
    const r = await api[kind]({ dry_run: dryRun, confirm })
    opResult.value = JSON.stringify(r, null, 2)
    showToast(
      `${kind} ${dryRun ? 'dry-run' : '实跑'}完成：${JSON.stringify(r.stats)}`,
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
.ops {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.ops-gap {
  width: 1px;
  height: 20px;
  margin: 0 4px;
  background: var(--border-2);
}
.pre {
  margin-top: 20px;
}
.pad {
  padding: 20px;
}
.src {
  margin-top: 12px;
  color: var(--text-faint);
  font-size: 12px;
}
</style>
