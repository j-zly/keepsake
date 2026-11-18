<template>
  <div>
    <div class="page-head head-row">
      <div>
        <h1 class="page-title">定时任务</h1>
        <p class="page-sub">
          调度的正源是 PG 里的 jobstore（代码里的班车表只作首次种子）——
          这里加的、改的、删的都直接落在调度器上，重启后照旧。
        </p>
      </div>
      <el-button type="primary" @click="openCreate">添加任务</el-button>
    </div>

    <!-- 调度器停摆：红条（调度器挂了就一条都发不出去，不能只让用户自己发现） -->
    <div v-if="schedulerDown" class="banner banner--danger">
      调度器停止：{{ schedulerStatus }} —— 下面的任务一条都发不出去。
    </div>
    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <!-- 任务列表：名称 / 任务 ID / 调度 / 下次执行 / 状态 / 操作 -->
    <div class="panel job-list">
      <div class="job-head">
        <span class="col-name">名称</span>
        <span class="col-id">任务 ID</span>
        <span class="col-cron">调度</span>
        <span class="col-next">下次执行</span>
        <span class="col-state">状态</span>
        <span class="col-acts">操作</span>
      </div>
      <div v-for="row in jobs" :key="row.job_id" class="job-row">
        <span class="col-name">
          <span class="job-name" :title="row.name || row.job_id">{{ row.name || row.job_id }}</span>
          <span class="job-sub">{{ cronHint(row.schedule) }}</span>
        </span>
        <span class="col-id mono">{{ row.job_id }}</span>
        <span class="col-cron mono">{{ row.schedule || '—' }}</span>
        <span class="col-next num">{{ row.next_run ? toLocal(row.next_run) : '—' }}</span>
        <!-- 状态列**只有徽标**：动作一律在操作列（一个对象一处入口，
             状态列里塞按钮会让人以为「状态」也能点）。 -->
        <span class="col-state">
          <span class="chip" :class="row.enabled ? 'chip--ok' : 'chip--warn'">
            {{ row.enabled ? '运行中' : '已暂停' }}
          </span>
          <span v-if="isTiering(row)" class="chip" :class="tStatus?.enabled ? 'chip--ok' : ''">
            {{ tStatus?.enabled ? '分级已开' : '分级默认关' }}
          </span>
          <!-- 代码班车徽标：新形态（func，同进程调函数）替命令行摘要 ——
               那一行的「执行体」不是一个命令行，看命令没意义。 -->
          <span v-if="row.func" class="chip chip--code" title="同进程调用的代码班车">
            代码班车
          </span>
        </span>
        <!-- 操作列：五枚常驻按钮一行排齐（不许 3+2 换行）；
             分级班车那行多一枚「设置」（它的阈值入口跟着它自己那一行走）。
             色彩语义：破坏性=红（删除）、触发执行=主色（运行一次）、查看=中性描边。 -->
        <span class="col-acts">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" @click="askToggle(row)">
            {{ row.enabled ? '暂停' : '恢复' }}
          </el-button>
          <el-button size="small" type="primary" @click="askTrigger(row)">运行一次</el-button>
          <el-button size="small" @click="openRuns(row)">执行记录</el-button>
          <el-button v-if="isTiering(row)" size="small" :disabled="!tStatus" @click="openTiering">
            设置
          </el-button>
          <el-button size="small" type="danger" @click="askDelete(row)">删除</el-button>
        </span>
      </div>
      <div v-if="!jobs.length" class="empty-mini">调度器里没有任务。</div>
    </div>

    <!-- 新建 / 编辑：同一套字段（id 只有新建时可填） -->
    <el-dialog
      v-model="formOpen"
      :title="formMode === 'create' ? '添加定时任务' : '编辑定时任务'"
      width="560px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busy"
      :show-close="!busy"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="任务 ID（ks- 前缀，创建后不可改）">
          <el-input v-model="form.id" :disabled="formMode !== 'create'" placeholder="ks-my-task" />
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="记忆提炼" />
        </el-form-item>
        <el-form-item label="调度（五段 cron，如 30 4 * * *）">
          <el-input v-model="form.schedule" placeholder="30 4 * * *" />
        </el-form-item>
        <el-form-item label="函数引用（模块:函数，如 keepsake.buses.distill:run）">
          <el-input v-model="form.func" placeholder="keepsake.buses.distill:run" />
        </el-form-item>
        <el-form-item label="超时（秒）">
          <el-input-number v-model="form.timeout" :min="1" :max="86400" />
        </el-form-item>
      </el-form>
      <p v-if="formError" class="form-error">{{ formError }}</p>
      <template #footer>
        <el-button :disabled="busy" @click="formOpen = false">取消</el-button>
        <el-button type="primary" :loading="busy === 'form'" @click="saveForm">保存</el-button>
      </template>
    </el-dialog>

    <!-- 执行记录（近 10 次；失败标红） -->
    <el-dialog
      v-model="runsOpen"
      :title="`执行记录 · ${runsTitle}`"
      width="560px"
      align-center
      :close-on-click-modal="false"
      append-to-body
    >
      <div v-if="!runs.length" class="empty-mini">还没有执行记录。</div>
      <ul v-else class="run-list">
        <li v-for="(r, i) in runs" :key="i" class="run-row" :class="{ 'run-row--fail': !r.ok }">
          <span class="num run-when">{{ toLocal(r.ran_at) }}</span>
          <span class="chip" :class="r.ok ? 'chip--ok' : 'chip--danger'">
            {{ r.ok ? '成功' : `失败 rc=${r.rc ?? '—'}` }}
          </span>
          <span class="run-detail">{{ r.detail || '—' }}</span>
        </li>
      </ul>
    </el-dialog>

    <!-- 分级班车设置（总开关 + 阈值）：从记忆维护页迁来，入口在 ks-tiering 那一行。
         confirm 仍要手输当前生效的 enabled（改的是全局开关，防手滑）。 -->
    <el-dialog
      v-model="tieringOpen"
      title="常驻层分级班车设置"
      width="520px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyT"
      :show-close="!busyT"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="总开关（默认关）">
          <el-switch v-model="tDraft.enabled" />
        </el-form-item>
        <el-form-item label="升提名：窗口天数 / 最少命中数（默认 3000）">
          <el-input-number v-model="tDraft.promote_nominate.window_days" :min="1" :max="365" />
          <el-input-number v-model="tDraft.promote_nominate.nominate_min_hits" :min="1" :max="100000" />
        </el-form-item>
        <el-form-item label="自动审核：候选挂够天数 / 同一窗通过命中数（默认 7 天 / 5000 次）">
          <el-input-number v-model="tDraft.promote_auto.review_window_days" :min="1" :max="365" />
          <el-input-number v-model="tDraft.promote_auto.promote_approve_hits" :min="1" :max="100000" />
        </el-form-item>
        <el-form-item label="降级：告警水位 / 释放水位（0.90 / 0.80）">
          <el-input-number v-model="tDraft.demote_pressure.warn_ratio" :min="0.01" :max="1" :step="0.05" />
          <el-input-number v-model="tDraft.demote_pressure.release_ratio" :min="0" :max="1" :step="0.05" />
        </el-form-item>
        <el-form-item label="每轮提名 / 降级条数上限">
          <el-input-number v-model="tDraft.batch_cap" :min="1" :max="10000" />
        </el-form-item>
        <el-form-item :label="`手输当前生效值 ${tStatus?.enabled ? 'true' : 'false'} 以确认`">
          <el-input v-model="tConfirm" :placeholder="tStatus?.enabled ? 'true' : 'false'" />
        </el-form-item>
      </el-form>
      <p class="tip">
        开总开关前建议先观察几天提名走势（关闭时班车不提名、不升降）。开关写的是 config.json 的 <code>tiering</code> 节（只动这一节、其余键与键序不变，写前整份备份）。
        手写的常驻条目（带 <code>memory</code> 标）永不被自动降，只进退居建议。
      </p>
      <template #footer>
        <el-button :disabled="busyT" @click="tieringOpen = false">取消</el-button>
        <el-button type="primary" :loading="busyT" :disabled="!canSaveT" @click="saveTiering">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- 危险 / 一般动作的统一确认框：**不键入任何字符**（点确认即执行） -->
    <ConfirmDialog
      v-model="confirmOpen"
      :title="confirmTitle"
      :consequences="confirmConsequences"
      :tone="confirmTone"
      :confirm-text="confirmText"
      :busy="busy === confirmKind"
      @confirm="doConfirm"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { api } from '../api'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { cronHint } from '../utils/cron'

/** 分级班车那条任务的 id（设置入口挂在它自己那一行）。 */
const TIERING_JOB_ID = 'ks-tiering'

const jobs = ref([])
const schedulerStatus = ref('')
const error = ref('')
const busy = ref('')

/** 调度器停摆（读面把原因写在 scheduler_status.text 里）。 */
const schedulerDown = computed(() => schedulerStatus.value.includes('调度器停止'))

async function load() {
  error.value = ''
  try {
    const res = await api.crons()
    jobs.value = res?.jobs || []
    schedulerStatus.value = res?.scheduler_status?.text || ''
  } catch (e) {
    error.value = `读取定时任务失败：${e.message}`
  }
}

/* ---------------------------------------------------------------- 调度人话副标
 * 「30 4 * * *」→「每天 04:30」：cron 表达式给不熟 cron 的人看是没有意义的。
 * 口径与实现都在 utils/cron.js（纯函数，护栏能真跑一遍钉住）。 */

/* ---------------------------------------------------------------- 新建 / 编辑 */
const formOpen = ref(false)
const formMode = ref('create')        // 'create' | 'edit'
const formError = ref('')
/** 执行方式只剩一种：func（模块:函数，同进程调函数）。 */
const form = ref({ id: '', name: '', schedule: '', func: '', timeout: 1800 })

function openCreate() {
  formMode.value = 'create'
  form.value = { id: '', name: '', schedule: '0 4 * * *', func: '', timeout: 1800 }
  formError.value = ''
  formOpen.value = true
}

function openEdit(row) {
  formMode.value = 'edit'
  form.value = {
    id: row.job_id,
    name: row.name || '',
    schedule: row.schedule || '',
    func: row.func || '',
    timeout: row.timeout || 1800,
  }
  formError.value = ''
  formOpen.value = true
}

/** 函数引用形状：模块:函数（后端注册时还会再验一遍「可 import 且 callable」）。 */
const FUNC_RE = /^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$/

async function saveForm() {
  const f = form.value
  const payload = {
    name: f.name,
    schedule: f.schedule,
    timeout: Number(f.timeout),
  }
  // 执行方式只有 func：形状不对就地拦下（后端还会再验一遍「可 import 且 callable」）。
  const fn = String(f.func || '').trim()
  if (!FUNC_RE.test(fn)) {
    formError.value = '函数引用形状不合法（应形如 keepsake.buses.distill:run）'
    return
  }
  payload.func = fn
  if (formMode.value === 'create') payload.id = f.id
  busy.value = 'form'
  formError.value = ''
  try {
    if (formMode.value === 'create') await api.cronCreate(payload)
    else await api.cronUpdate(f.id, payload)
    formOpen.value = false
    showToast(formMode.value === 'create' ? '定时任务已创建' : '定时任务已更新')
    await load()
  } catch (e) {
    formError.value = e.message
  } finally {
    busy.value = ''
  }
}

/* ---------------------------------------------------------------- 确认弹框
 * 全部是**简单确认**：点确认即执行，不键入任何字符。
 * 危险动作（删除）用红色主按钮 + 逐条后果。 */
const confirmOpen = ref(false)
const confirmKind = ref('')
const confirmTarget = ref('')
const confirmTitle = ref('')
const confirmConsequences = ref([])
const confirmTone = ref('danger')
const confirmText = ref('确认执行')

function askToggle(row) {
  confirmKind.value = row.enabled ? 'pause' : 'resume'
  confirmTarget.value = row.job_id
  confirmTitle.value = row.enabled ? `暂停「${row.name || row.job_id}」？` : `恢复「${row.name || row.job_id}」？`
  confirmConsequences.value = row.enabled
    ? ['暂停后不再按计划执行（jobstore 里保留，随时可恢复）。']
    : ['恢复后按当前调度继续执行。']
  confirmTone.value = 'primary'
  confirmText.value = row.enabled ? '暂停' : '恢复'
  confirmOpen.value = true
}

function askTrigger(row) {
  confirmKind.value = 'trigger'
  confirmTarget.value = row.job_id
  confirmTitle.value = `立即运行一次「${row.name || row.job_id}」？`
  confirmConsequences.value = [
    '立刻执行一次（不影响原有的按点调度）。',
    '执行结果落在执行记录里（失败会标红）。',
  ]
  confirmTone.value = 'primary'
  confirmText.value = '立即运行'
  confirmOpen.value = true
}

function askDelete(row) {
  confirmKind.value = 'delete'
  confirmTarget.value = row.job_id
  confirmTitle.value = `删除「${row.name || row.job_id}」？`
  confirmConsequences.value = [
    '删除不会保留，可重新添加。',
    '重启后不会复活（已写墓碑）。',
    '执行记录保留（那是「跑过什么」的账）。',
  ]
  confirmTone.value = 'danger'
  confirmText.value = '删除'
  confirmOpen.value = true
}

async function doConfirm() {
  const kind = confirmKind.value
  const id = confirmTarget.value
  confirmOpen.value = false
  busy.value = kind
  try {
    if (kind === 'pause') await api.cronPause(id)
    else if (kind === 'resume') await api.cronResume(id)
    else if (kind === 'trigger') {
      await api.cronTrigger(id)
      showToast('已排入立即执行（稍后在执行记录里看结果）')
    } else if (kind === 'delete') {
      await api.cronDelete(id)
      showToast('定时任务已删除（重启后不会复活）')
    }
    await load()
  } catch (e) {
    showError(`操作失败：${e.message}`)
  } finally {
    busy.value = ''
  }
}

/* ---------------------------------------------------------------- 执行记录 */
const runsOpen = ref(false)
const runsTitle = ref('')
const runs = ref([])

async function openRuns(row) {
  runsTitle.value = row.name || row.job_id
  runs.value = []
  runsOpen.value = true
  try {
    const res = await api.cronRuns(row.job_id)
    runs.value = res?.runs || []
  } catch (e) {
    showError(`读取执行记录失败：${e.message}`)
  }
}

/* ---------------------------------------------------------------- 分级班车设置 */
const tieringOpen = ref(false)
const busyT = ref(false)
const tStatus = ref(null)
const tDraft = ref({
  enabled: false,
  promote_nominate: { window_days: 7, nominate_min_hits: 3000 },
  promote_auto: { review_window_days: 7, promote_approve_hits: 5000 },
  demote_pressure: { warn_ratio: 0.9, release_ratio: 0.8, force_at_full: true },
  demote_exempt: { explicit_core: true },
  batch_cap: 50,
})
const tConfirm = ref('')

function isTiering(row) {
  return row.job_id === TIERING_JOB_ID
}

/** 保存条件：阈值都是正数 + confirm 逐字等于当前生效 enabled（后端同一口径） */
const canSaveT = computed(() => {
  const d = tDraft.value
  if (!d) return false
  const nums = [d.promote_nominate.window_days, d.promote_nominate.nominate_min_hits,
    d.promote_auto.review_window_days, d.promote_auto.promote_approve_hits, d.batch_cap]
  const ratios = [d.demote_pressure.warn_ratio, d.demote_pressure.release_ratio]
  const okNums = nums.every((n) => Number.isFinite(Number(n)) && Number(n) >= 1)
  const okRatios = ratios.every((n) => Number(n) > 0 && Number(n) <= 1)
  return okNums && okRatios && Number(d.demote_pressure.release_ratio) <= Number(d.demote_pressure.warn_ratio)
    && String(tConfirm.value).trim() === String(!!tStatus.value?.enabled)
})

async function loadTiering() {
  try {
    tStatus.value = await api.tieringStatus()
  } catch (e) {
    tStatus.value = null
  }
}

function openTiering() {
  const s = tStatus.value?.settings
  if (!s) {
    showError('分级班车状态还没读到，稍后再试')
    return
  }
  tDraft.value = JSON.parse(JSON.stringify(s))
  tConfirm.value = ''
  tieringOpen.value = true
}

async function saveTiering() {
  if (!canSaveT.value) return
  busyT.value = true
  try {
    const r = await api.setTiering(tDraft.value, String(!!tStatus.value?.enabled))
    tieringOpen.value = false
    showToast(`已保存分级班车设置（enabled=${r.enabled}）`)
    loadTiering()
  } catch (e) {
    showError(`保存分级设置失败：${e.message}`)
  } finally {
    busyT.value = false
  }
}

onMounted(async () => {
  await load()
  loadTiering()
})
</script>

<style scoped>
/* 页头：左标题右按钮（同一行）。 */
.head-row {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
}

/* ---------------------------------------------------------------- 任务列表
 * 六列对齐（名称 / 任务 ID / 调度 / 下次执行 / 状态 / 操作）——
 * 表头与行共用同一套列宽，眼睛往下扫时那一列是一条直线。
 *
 * 列宽口径（scheduler_page_ui_fix）：
 *   名称 240px 定宽（**两行**：名字 + 人话副标，副标不许被挤没）；
 *   调度 / 下次执行 收窄（它们本来就是短串）；
 *   操作列 380px —— 五枚常驻按钮（编辑 / 暂停 / 运行一次 / 执行记录 / 删除）
 *   一行排齐，分级班车那行第六枚「设置」也塞得下（**不许 3+2 换行**）。 */
.job-list {
  padding: 0;
  overflow-x: auto;          /* 列宽定死 ⇒ 窄屏横滚，不许把行挤变形 */
  overflow-y: hidden;
  margin-bottom: 20px;
}
.job-head,
.job-row {
  display: grid;
  grid-template-columns: 240px 130px 120px 130px 160px 380px;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
}
.job-head {
  padding: 10px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 12px;
}
.job-row {
  border-top: 1px solid var(--line-3);
}
.job-head + .job-row {
  border-top: none;
}
.col-name {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.job-name {
  color: var(--text);
  font-size: 14px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 人话副标：cron 表达式的中文解释（认不出来的形状写「自定义调度」）。 */
.job-sub {
  color: var(--text-faint);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.col-id,
.col-cron {
  color: var(--text-dim);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.col-next {
  color: var(--text-dim);
  font-size: 12px;
}
.col-state {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: nowrap;
}
/* 操作列：一行排齐（不许换行成 3+2）—— 按钮自己排版，
   Element Plus 默认给相邻按钮加的 margin-left 在这里归零（间距统一交给 gap）。 */
.col-acts {
  display: flex;
  gap: 6px;
  align-items: center;
  flex-wrap: nowrap;
}
.col-acts :deep(.el-button + .el-button) {
  margin-left: 0;
}
@media (max-width: 1100px) {
  .job-head,
  .job-row {
    grid-template-columns: minmax(0, 1fr);
    gap: 6px;
  }
  .job-head {
    display: none;
  }
  .col-acts {
    flex-wrap: wrap;         /* 单列布局下允许换行（横滚已不需要） */
  }
}

/* 「已开」状态 chip：绿字描边（--ok 令牌；关闭态退默认 chip）。 */
.chip--ok {
  color: var(--ok);
  border-color: currentColor;
}

/* 代码班车徽标（func 形态）：中性描边 —— 不跟「运行中 / 已暂停」的绿黄语义混。 */
.chip--code {
  color: var(--text-dim);
  border-color: currentColor;
}

/* ---------------------------------------------------------------- 执行记录 */
.run-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.run-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  padding: 10px 0;
  border-top: 1px solid var(--line-3);
  font-size: 13px;
}
.run-row:first-child {
  border-top: none;
}
.run-when {
  flex: none;
  color: var(--text-dim);
  font-size: 12px;
}
.run-detail {
  min-width: 0;
  color: var(--text-faint);
  font-size: 12px;
  word-break: break-all;
}
/* 失败那行字色转红（一眼看见「上一次跑挂了」）。 */
.run-row--fail .run-detail {
  color: var(--danger);
}

/* ---------------------------------------------------------------- 表单错误 */
.form-error {
  margin: 0;
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.7;
}

/* 空态：矮卡片 + 一句灰字 */
.empty-mini {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  color: var(--text-faint);
  font-size: 13px;
  line-height: 1.7;
  text-align: center;
}
</style>
