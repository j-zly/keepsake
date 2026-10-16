<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">看板</h1>
      <p class="page-sub">三个域的存量与预算，加上定时任务。</p>
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

    <h3 class="section-title">分级班车</h3>
    <div class="stat-grid">
      <StatCard
        label="常驻预算水位"
        :value="tiering?.budget ? `${tiering.budget.ratio}%` : '-'"
        :percent="tiering?.budget?.ratio ?? null"
        :tone="tieringTone"
        :hint="tiering?.budget ? `已占 ${tiering.budget.used} / ${tiering.budget.total} 字符` : '读取失败'"
      />
      <StatCard
        label="班车状态"
        :value="tiering ? (tiering.enabled ? '已开' : '默认关') : '-'"
        :hint="tieringHint"
      />
      <StatCard
        label="上次运行"
        :value="tieringLastRun"
        :hint="tiering?.last_run ? (tiering.last_dry_run ? 'dry-run（只算不写）' : '实跑') : '班车还没跑过'"
      />
    </div>
    <div v-if="tieringError" class="banner banner--danger">{{ tieringError }}</div>

    <h3 class="section-title">知识库</h3>
    <div class="stat-grid">
      <StatCard label="skill_docs" :value="kb.skill_docs.value" :hint="kb.skill_docs.hint" />
      <StatCard label="documents" :value="kb.documents.value" :hint="kb.documents.hint" />
      <StatCard label="chunks" :value="kb.chunks.value" :hint="kb.chunks.hint" />
    </div>

    <h3 class="section-title">记忆类定时任务</h3>
    <div class="sched-line">
      <span class="sched-k">调度器</span>
      <span class="sched-v">{{ schedulerStatus || '未知' }}</span>
    </div>
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
        <template #time>
          上次 {{ toLocal(row.last_run_at) }}<template v-if="row.next_run"> · 下次 {{ toLocal(row.next_run) }}</template>
        </template>

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

        <!-- 管理按钮**只对认得出 job_id 的行出现**：取不到 id 的行保持只读，
             宁可少一个按钮，也不去对一个没读懂的 jobs.json 乱发写请求。 -->
        <template #actions>
          <button v-if="row.job_id" class="act-btn" @click.stop="askToggle(row)">
            {{ row.enabled ? '暂停' : '恢复' }}
          </button>
          <button v-if="row.job_id" class="act-btn" @click.stop="openEdit(row)">改调度</button>
        </template>
      </ItemCard>
    </div>
    <div class="src">来源：{{ cronSource }}</div>

    <!-- 暂停 / 恢复：一句后果 + 确认（不写无信息量的那句问话） -->
    <ConfirmDialog
      v-model="toggleAsk"
      :title="toggleTitle"
      :consequences="toggleConsequences"
      :confirm-text="toggleConfirmText"
      tone="primary"
      :busy="busyCron"
      @confirm="doToggle"
    />

    <!-- 改调度：旧值 → 新值对比在正文里，确认按钮前就能看见改成了什么 -->
    <el-dialog
      v-model="editAsk"
      width="460px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyCron"
      :show-close="!busyCron"
      append-to-body
      title="改这条任务的调度"
    >
      <p class="edit-old mono">{{ row?.schedule || '（无）' }}</p>
      <label class="edit-label">
        <span>新调度（cron 表达式）</span>
        <el-input
          v-model="newSchedule"
          placeholder="例：30 4 * * *"
          autocomplete="off"
          spellcheck="false"
          @keyup.enter="doEdit"
        />
      </label>
      <p v-if="editError" class="edit-err">{{ editError }}</p>
      <p class="edit-tip">改完由官方 hermes cron CLI 执行并立即生效；表达式非法时后端会带例子回你。</p>
      <template #footer>
        <el-button :disabled="busyCron" @click="editAsk = false">取消</el-button>
        <el-button type="primary" :loading="busyCron" :disabled="!newSchedule.trim()" @click="doEdit">
          确认改调度
        </el-button>
      </template>
    </el-dialog>
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
const schedulerStatus = ref('')
const loading = ref(false)
const error = ref('')
const cronError = ref('')
const tiering = ref(null)
const tieringError = ref('')

/* ---------------------------------------------------------------- 定时任务管理
 * 三条操作（暂停 / 恢复 / 改调度）都改的是网关那份 jobs.json —— 本端只发请求，
 * 真动作由后端调官方 `hermes cron` CLI 执行。成功后一律重拉列表：启停状态与
 * next_run 都由文件说了算，本地改一改很快就和真实调度对不上了。 */
const row = ref(null)            // 正在操作的那一行
const busyCron = ref(false)
const toggleAsk = ref(false)
const editAsk = ref(false)
const newSchedule = ref('')
const editError = ref('')

const pausing = computed(() => !!row.value && row.value.enabled)
const toggleTitle = computed(() => (pausing.value ? '暂停这条定时任务？' : '恢复这条定时任务？'))
const toggleConsequences = computed(() => [
  pausing.value ? '暂停后不再按计划执行（已跑的这一次不受影响）' : '恢复后按当前调度继续执行',
  '改的是网关的 jobs.json（官方 hermes cron CLI 执行），本控制台只留一条操作账',
])
const toggleConfirmText = computed(() => (pausing.value ? '确认暂停' : '确认恢复'))

/**
 * 班车卡片的水位配色：≥100% danger（水已经溢出，注入在截断）、≥90% accent
 * （快到告警线，班车下一轮就要提名退位）。口径与 `routers/ops.tiering_status`
 * 给的 ratio 同一个（percent）。
 */
const tieringTone = computed(() => {
  const r = tiering.value?.budget?.ratio
  if (typeof r !== 'number') return ''
  if (r >= 100) return 'danger'
  if (r >= 90) return 'accent'
  return 'ok'
})

const tieringHint = computed(() => {
  if (!tiering.value) return ''
  const t = tiering.value
  if (t.enabled) return `提名 ≥${t.settings.promote_nominate.min_hits} 次/窗口，每轮上限 ${t.settings.batch_cap} 条`
  return t.note || '总开关关着，班车只算不写'
})

const tieringLastRun = computed(() => (tiering.value?.last_run ? toLocal(tiering.value.last_run) : '—'))

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
    // 无 hermes CLI 时后端给 null —— 这一行显示「未知」，不许把整页拖成报错
    schedulerStatus.value = c.scheduler_status || ''
    cronError.value = c.count ? '' : `没有匹配到记忆类任务（jobs.json：${c.source}）`
  } catch (e) {
    cronError.value = `读取定时任务失败：${e.message}`
  }
  // 分级班车状态单独读：它挂了就挂这一段，不许把上面两张卡一起拖成报错
  try {
    tiering.value = await api.tieringStatus()
    tieringError.value = ''
  } catch (e) {
    tiering.value = null
    tieringError.value = `读取分级班车状态失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function doCopy(text) {
  const ok = await copyText(text)
  ok ? showToast('已复制') : showError('复制失败，请手动选中复制')
}

/** 暂停 / 恢复：先开确认框，确认后才发请求（后端还会再要一次 confirm）。 */
function askToggle(r) {
  if (!r.job_id) return
  row.value = r
  toggleAsk.value = true
}

async function doToggle() {
  const r = row.value
  busyCron.value = true
  try {
    const res = r.enabled ? await api.cronPause(r.job_id) : await api.cronResume(r.job_id)
    toggleAsk.value = false
    showToast(res.action === 'pause' ? '已暂停' : '已恢复')
    await load()
  } catch (e) {
    showError(`${toggleTitle.value.replace('？', '')}失败：${e.message}`)
  } finally {
    busyCron.value = false
  }
}

/** 改调度：弹框里预填当前表达式，正文就是「旧值 → 新值」的对比。 */
function openEdit(r) {
  if (!r.job_id) return
  row.value = r
  newSchedule.value = r.schedule || ''
  editError.value = ''
  editAsk.value = true
}

async function doEdit() {
  const r = row.value
  const expr = newSchedule.value.trim()
  if (!expr || busyCron.value) return
  busyCron.value = true
  editError.value = ''
  try {
    await api.cronEdit(r.job_id, expr)
    editAsk.value = false
    showToast('调度已改')
    await load()
  } catch (e) {
    // 表达式非法（400）由后端带 example 回来，原样显示在弹框里，不弹 toast
    editError.value = e.message || '改调度失败'
  } finally {
    busyCron.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pad {
  padding: 20px;
}
.src {
  margin-top: 12px;
  color: var(--text-faint);
  font-size: 12px;
}
/* 调度器状态一行：与 stat-grid 同色板，不新造颜色 */
.sched-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 14px;
  font-size: 13px;
}
.sched-k {
  color: var(--text-faint);
}
.sched-v {
  color: var(--text-dim);
}
/* 改调度弹框：旧值在上（弱），新值在下（可编辑），错误回显贴在输入框下 */
.edit-old {
  margin: 0 0 14px;
  color: var(--text-faint);
  font-size: 13px;
}
.edit-label {
  display: block;
  cursor: text;
}
.edit-label > span {
  display: block;
  margin-bottom: 7px;
  color: var(--text-faint);
  font-size: 12px;
}
.edit-err {
  margin: 10px 0 0;
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.6;
}
.edit-tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}
</style>
