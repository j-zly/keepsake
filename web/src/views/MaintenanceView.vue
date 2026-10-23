<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">记忆维护</h1>
      <p class="page-sub">合并重复的记忆条目、遗忘过期条目；dry-run 零写入，实跑要手输当前计数。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="常驻层条数" :value="data?.core?.count ?? '-'" unit="条" hint="实跑「合并」要手输这个数" />
      <StatCard
        label="记忆条目"
        :value="data?.retrieval?.count ?? '-'"
        unit="条"
        :hint="data?.retrieval?.truncated ? '已触扫描上限（5000），非全量' : '实跑「遗忘」要手输这个数'"
      />
    </div>

    <!-- 常驻层水位：总量 + 每代理字符占比（多代理时代，水位是被几个代理分着占的） -->
    <h3 class="section-title">常驻层水位 · 按代理</h3>
    <div class="stat-grid">
      <StatCard
        label="常驻水位"
        :value="waterValue"
        :percent="waterPercent"
        :tone="waterTone"
        :hint="waterHint"
      />
      <StatCard
        label="按代理分列"
        :value="agentShareCount"
        unit="个"
        hint="每个代理一份独立注入预算；占比的分母是水位总量"
      />
    </div>
    <div v-if="agentShares.length" class="agent-cards">
      <div
        v-for="row in agentShares"
        :key="row.agent_id || 'none'"
        class="agent-card"
        :class="{ 'agent-card--warn': row.percent >= 90 }"
      >
        <div class="agent-card-top">
          <span class="agent-card-name" :title="row.agent_id || row.label">{{ row.label }}</span>
          <span class="agent-card-pct num">{{ row.percent }}%</span>
        </div>
        <div class="agent-card-bar">
          <i :style="{ width: barWidth(row.percent) + '%' }"></i>
        </div>
        <div class="agent-card-foot num">{{ row.chars }} 字符</div>
      </div>
    </div>
    <div v-else class="panel empty-mini">
      <span>没有可分列的代理数据 —— 常驻层里还没有带代理归属的条目。</span>
    </div>

    <!-- 退居提名（班车建议退出常驻层）：归属代理一眼可见 ——
         主脑名下的条目不挂 chip（默认就是它的），别的代理的挂上，
         免得「班车要把谁踢出去」看半天看不出来。批准/驳回在审核页做（这里只读）。 -->
    <h3 class="section-title">退居提名（班车建议退出常驻层）</h3>
    <div v-if="demoteError" class="banner banner--danger">{{ demoteError }}</div>
    <div v-if="!demotes.length" class="panel empty-mini">
      <span>没有退居提名 —— 常驻层水位没到告警线，或建议都已经审完了。</span>
    </div>
    <!-- 退居提名：按列排（条目 / 归属 / 提名时间）—— 关键列对齐，
         眼睛往下扫时时间那一列是一条直线，不用每行自己找。 -->
    <div v-else class="panel demote-list">
      <div class="demote-head">
        <span class="col-key">记忆条目</span>
        <span class="col-owner">归属代理</span>
        <span class="col-time">提名时间</span>
      </div>
      <div v-for="row in demotes" :key="row.key" class="demote-row">
        <span class="cell-key col-key">
          <span class="k" :title="row.key">{{ row.key }}</span>
        </span>
        <span class="col-owner">
          <span
            v-if="ownerChip(row)"
            class="tag tag--owner"
            :title="`归属代理：${ownerOf(row.tags)}`"
          >{{ ownerChip(row) }}</span>
        </span>
        <span class="num col-time">{{ toLocal(row.updated_at) }}</span>
      </div>
      <p class="src list-foot">
        共 {{ demoteCount }} 条<template v-if="demoteCount > demotes.length">（这里只列前 {{ demotes.length }} 条）</template>；
        批准 / 驳回在「记忆审核」页的「退居建议」里做（这里只读）。
      </p>
    </div>

    <!-- 待办「建议废弃」（2026-10 ks_todo 班车）：到期日过了 7 天还开着的待办，
         这里**只提名**（不改状态）—— 「这条到底还有没有人管」只有人知道，
         自动标废弃等于替人做决定。要销账去「待办」页做。 -->
    <h3 class="section-title">建议废弃的待办（到期过 7 天仍未销）</h3>
    <div v-if="obsoleteError" class="banner banner--danger">{{ obsoleteError }}</div>
    <div v-if="!obsoleteTodos.length" class="panel empty-mini">
      <span>没有建议废弃的待办 —— 没有过了 7 天还没销的开放待办。</span>
    </div>
    <div v-else class="panel pad agent-split">
      <div v-for="row in obsoleteTodos" :key="row.id" class="agent-line">
        <span class="cell-key">
          <span class="k">{{ row.title }}</span>
        </span>
        <span v-if="row.owner" class="tag tag--owner" :title="`归属代理：${row.owner}`">
          {{ row.owner }}
        </span>
        <span class="chip chip--danger">已过期 {{ row.overdue_days }} 天</span>
        <span class="num agent-num">到期 {{ row.due }}</span>
      </div>
      <p class="src">
        共 {{ obsoleteTodos.length }} 条；<b>这里只提名，不改状态</b> ——
        确认没人管了，去「待办」页标废弃或删除。
      </p>
    </div>

    <!-- 页脚说明：降为 secondary 小字（12px 灰）—— 它是脚注，不该跟正文抢视觉；
         安全要点在按钮文案（dry-run / 实跑）与确认弹框里都写着。 -->
    <p class="src foot-note foot-note--lead">
      dry-run 零写入，可放心点；实跑必须手输当前计数，且会先自动备份受影响记忆条目。
    </p>
    <div class="ops">
      <el-button :loading="busy === 'merge-dry'" @click="runMerge(true)">合并 · dry-run</el-button>
      <el-button :loading="busy === 'forget-dry'" @click="runForget(true)">遗忘 · dry-run</el-button>
      <span class="ops-gap"></span>
      <el-button type="danger" :loading="busy === 'merge'" @click="runMerge(false)">合并 · 实跑</el-button>
      <el-button type="danger" :loading="busy === 'forget'" @click="runForget(false)">遗忘 · 实跑</el-button>
      <span class="ops-gap"></span>
      <el-button :disabled="!tStatus" @click="openTiering">
        分级班车设置（{{ tStatus?.enabled ? '已开' : '默认关' }}）
      </el-button>
    </div>
    <p class="src foot-note">
      分级班车：每天一次 cron 跑三条规则（7 天窗 ≥ 3000 提名 / 提名后 7 天再 ≥ 5000 自动审核通过、否则自动驳回 / 预算吃紧提名退位）。
      <b>缺省关死</b>——先用 <code>scripts/tiering_cron.py --dry</code> 人肉观察几天，看清 stats 再在这里开总开关。
    </p>
    <pre v-if="opResult" class="pre">{{ opResult }}</pre>

    <!-- 分级班车设置：总开关 + 阈值。confirm 要手输当前生效的 enabled（防手滑） -->
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
        <el-form-item label="总开关（默认关：先用 --dry 观察，确认无误再开）">
          <el-switch v-model="tDraft.enabled" />
        </el-form-item>
        <el-form-item label="升提名：窗口天数 / 最少命中数（默认 3000，7 天窗总打点≈7k 时基本关死本轨）">
          <el-input-number v-model="tDraft.promote_nominate.window_days" :min="1" :max="365" />
          <el-input-number v-model="tDraft.promote_nominate.nominate_min_hits" :min="1" :max="100000" />
        </el-form-item>
        <el-form-item label="自动审核：候选挂够天数 / 同一窗通过命中数（默认 7 天 / 5000 次；不满窗不动）">
          <el-input-number v-model="tDraft.promote_auto.review_window_days" :min="1" :max="365" />
          <el-input-number v-model="tDraft.promote_auto.promote_approve_hits" :min="1" :max="100000" />
        </el-form-item>
        <el-form-item label="降级：告警水位 / 释放水位（0.90 / 0.80），水位满时强降 1 条">
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
        开关写的是 config.json 的 <code>tiering</code> 节（只动这一节、其余键与键序不变，写前整份备份）。
        手写的常驻条目（带 <code>memory</code> 标）永不被自动降，只进退居建议。
      </p>
      <template #footer>
        <el-button :disabled="busyT" @click="tieringOpen = false">取消</el-button>
        <el-button type="primary" :loading="busyT" :disabled="!canSaveT" @click="saveTiering">
          保存
        </el-button>
      </template>
    </el-dialog>

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
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { agentUsageRows, loadAgentRows, ownerLabel, ownerOf, sharePercent } from '../utils/agents'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

// 计数口径与看板同一接口（/api/v1/stats）：实跑时用户手输的就是这里显示的两个数
const data = ref(null)
const error = ref('')
const busy = ref('')
const opResult = ref('')

/** 危险实跑（合并 / 遗忘）的确认态 */
const confirmOpen = ref(false)
const confirmKind = ref('')
const confirmExpect = ref('')
const confirmTitle = ref('')
const confirmConsequences = ref([])

async function load() {
  error.value = ''
  try {
    data.value = await api.stats()
  } catch (e) {
    error.value = `读取计数失败：${e.message}`
  }
}

/* ---------------------------------------------------------------- 代理维度
 * 多代理时代，常驻层的水位是**几个代理分着占**的 —— 只报一个总数看不出
 * 「谁把预算吃掉了」。下面两段各读一个只读端点，挂了只掉自己那一段。 */

/** 班车提名的退居建议标（后端 `keepsake/core/tiering.py` 的 DEMOTE_CANDIDATE_TAG；
 *  审核页 ReviewView 里同一个标 —— 两处各写一个字面量，改的时候一起改）。 */
const DEMOTE_CANDIDATE_TAG = 'tier:demote-candidate'

const perAgent = ref({})       // GET /api/v1/stats/core-per-agent
const agentRows = ref([])      // 注册表（要显示名 + 谁是 primary）
const demotes = ref([])        // 退居提名条目
const demoteCount = ref(0)     // 退居提名总条数（这一页只显示前 20 条）
const demoteError = ref('')

/* 待办「建议废弃」（2026-10 ks_todo）：只提名，不改状态 —— 这一页是只读面，
   改状态去「待办」页（那边有状态机与确认闸）。 */
const obsoleteTodos = ref([])
const obsoleteError = ref('')

/** agent_id → 显示名（注册表备注名）。 */
const agentNames = computed(() =>
    Object.fromEntries(agentRows.value.map((a) => [a.agent_id, a.name || a.agent_id])),
)
/** 现役主代理（role=primary）：它名下的条目默认就是「主脑的」，不挂归属 chip。 */
const primaryAgentId = computed(() => {
    const p = agentRows.value.find((a) => a.role === 'primary')
    return p ? p.agent_id : ''
})

/** 常驻水位总量：优先班车状态端点的 budget（与看板同一口径），退到 stats 的 core。 */
const water = computed(() => {
    const b = tStatus.value?.budget
    if (b && typeof b.used === 'number') return b
    const chars = data.value?.core?.chars
    if (typeof chars === 'number') {
        return { used: chars, total: data.value?.core?.budget || 0, ratio: data.value?.core?.percent || 0 }
    }
    return null
})
const waterValue = computed(() => (water.value ? `${water.value.ratio}%` : '-'))
const waterPercent = computed(() => (water.value ? water.value.ratio : null))
const waterTone = computed(() => {
    const r = water.value?.ratio
    if (typeof r !== 'number') return ''
    if (r >= 100) return 'danger'
    if (r >= 90) return 'accent'
    return 'ok'
})
const waterHint = computed(() =>
    (water.value ? `已占 ${water.value.used} / ${water.value.total} 字符` : '读取失败'))

/**
 * 每代理字符占比（占**当前常驻水位总量**，不是占它自己的预算 ——
 * 这一页说的是「水位被谁分着占」）。
 * 无归属的条目（老库没打 `agent:` 标签的）算一行「无归属」，占比补齐到 100%。
 */
const agentShares = computed(() => {
    const total = Number(water.value?.used || 0)
    const rows = agentUsageRows(perAgent.value, agentNames.value).map((r) => ({
        ...r,
        percent: sharePercent(r.chars, total),
    }))
    const sum = rows.reduce((n, r) => n + r.chars, 0)
    const rest = Math.max(0, total - sum)
    if (rest > 0) {
        rows.push({
            agent_id: '',
            label: '无归属',
            chars: rest,
            percent: sharePercent(rest, total),
        })
    }
    return rows
})

/** 分列里的代理数（不含「无归属」那一行）。 */
const agentShareCount = computed(() =>
    agentShares.value.filter((r) => r.agent_id).length,
)

/** 退居提名条目的归属 chip：不是主脑的才挂（挂了就看得见「班车要踢谁」）。 */
function ownerChip(row) {
    const id = ownerOf(row?.tags)
    if (!id || id === primaryAgentId.value) return ''
    return ownerLabel(row.tags, agentNames.value)
}

/** 读注册表（显示名 + 谁是 primary）与按代理分列。 */
async function loadAgentDimension() {
    try {
        agentRows.value = await loadAgentRows(api.listAgents)
    } catch (e) {
        agentRows.value = []
    }
    try {
        perAgent.value = (await api.corePerAgent()) || {}
    } catch (e) {
        perAgent.value = {}
    }
}

/** 退居提名列表（只读；批准/驳回在审核页）。 */
async function loadDemotes() {
    try {
        const res = await api.fragments({ tag: DEMOTE_CANDIDATE_TAG, page: 1, page_size: 20 })
        demotes.value = res?.items || []
        demoteCount.value = Number(res?.count || demotes.value.length)
        demoteError.value = ''
    } catch (e) {
        demotes.value = []
        demoteCount.value = 0
        demoteError.value = `读取退居提名失败：${e.message}`
    }
}

/** 待办「建议废弃」清单（只提名）。 */
async function loadObsoleteTodos() {
    try {
        const res = await api.todoObsoleteSuggestions()
        obsoleteTodos.value = res?.items || []
        obsoleteError.value = ''
    } catch (e) {
        obsoleteTodos.value = []
        obsoleteError.value = `读取建议废弃清单失败：${e.message}`
    }
}

/**
 * 卡片进度条宽度（百分比）：封顶 100 —— 超预算另走警示/危险色，
 * 条不许画出卡片（条溢出 = 看起来像 300%）。
 */
function barWidth(percent) {
  const p = Number(percent)
  if (!Number.isFinite(p)) return 0
  return Math.min(100, Math.max(0, p))
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
    const payload = { dry_run: dryRun, confirm }
    const r = kind === 'merge' ? await api.merge(payload) : await api.forget(payload)
    opResult.value = JSON.stringify(r, null, 2)
    showToast(`${kind} ${dryRun ? 'dry-run' : '实跑'}完成：${JSON.stringify(r.stats)}`)
    load()
  } catch (e) {
    showError(`${kind} ${dryRun ? 'dry-run' : '实跑'}失败：${e.message}`)
  } finally {
    busy.value = ''
  }
}

onMounted(() => {
  load()
  loadTiering()
  loadAgentDimension()
  loadDemotes()
  loadObsoleteTodos()
})

// ---------------------------------------------------------------------------
// 分级班车设置（开关 + 阈值）
// ---------------------------------------------------------------------------

/** 与后端 `keepsake/core/tiering.py` 的缺省表一一对应；打开弹框时按 status 回填 */
const tieringOpen = ref(false)
const busyT = ref(false)
const tStatus = ref(null)
/** 表单初值 = 后端缺省表（`keepsake/core/tiering.py` DEFAULTS 同一组值）：
    status 读回来后整份覆盖。给初值而不是 null，弹框的 v-model 才有对象可绑。 */
const tDraft = ref({
  enabled: false,
  promote_nominate: { window_days: 7, nominate_min_hits: 3000 },
  promote_auto: { review_window_days: 7, promote_approve_hits: 5000 },
  demote_pressure: { warn_ratio: 0.9, release_ratio: 0.8, force_at_full: true },
  demote_exempt: { explicit_core: true },
  batch_cap: 50,
})
const tConfirm = ref('')

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
    showError(`读取分级班车状态失败：${e.message}`)
  }
}

/** 打开时按后端回的**完整生效值**预填（缺省键后端已补齐），confirm 留空手输 */
function openTiering() {
  const s = tStatus.value?.settings
  if (!s) return
  tDraft.value = JSON.parse(JSON.stringify(s))
  tConfirm.value = ''
  tieringOpen.value = true
}

async function saveTiering() {
  if (!canSaveT.value) return
  busyT.value = true
  try {
    // confirm 传 'true'/'false' 字符串：开这个开关等于让班车每天动常驻层，
    // 确认串是「现在是不是开着」而不是条数（条数在另一张卡上，不防这种手滑）。
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
.src {
  margin-top: 10px;
  color: var(--text-faint);
  font-size: 12px;
}
/* 页脚说明：secondary 小字（脚注不跟正文抢视觉）。 */
.foot-note {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.foot-note--lead {
  margin: 0 0 12px;
}

/* ---------------------------------------------------------------- 代理占用卡
 * 原来是一行行裸文字（「名字 + 字符数 · 占 X%」），一屏看下去全是字 ——
 * 改成一张代理一张卡：名字 + 大号占比 + 细进度条 + 字符数小字。
 * 零新色：进度条走 --accent，≥90% 转 --warn（临期告警色）。 */
.agent-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}
@media (max-width: 900px) {
  .agent-cards {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;
  }
}
.agent-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  box-shadow: var(--shadow-card);
}
.agent-card-top {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.agent-card-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: 13px;
  font-weight: 500;
}
.agent-card-pct {
  flex: none;
  color: var(--text);
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.5px;
  line-height: 1.1;
}
.agent-card--warn .agent-card-pct {
  color: var(--warn);
}
/* 细进度条：6px 高 + chip 圆角（令牌圆角，不写裸 px） */
.agent-card-bar {
  height: 6px;
  border-radius: var(--r-chip);
  background: var(--chip);
  overflow: hidden;
}
.agent-card-bar i {
  display: block;
  height: 100%;
  border-radius: var(--r-chip);
  background: var(--accent);
}
.agent-card--warn .agent-card-bar i {
  background: var(--warn);
}
.agent-card-foot {
  color: var(--text-faint);
  font-size: 11px;
}

/* 空态：矮卡片 + 一句灰字（不再留一整块 44px padding 的空区）。 */
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

/* ---------------------------------------------------------------- 退居提名表
 * 三列对齐：条目（自适应）/ 归属代理（定宽）/ 提名时间（定宽、右对齐）。
 * 行分隔线走 --line-3（卡内分隔线那一档），表头带一条浅灰底。 */
.demote-list {
  padding: 0;
  overflow: hidden;
  margin-bottom: 20px;
}
.demote-head,
.demote-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 160px 150px;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
}
.demote-head {
  padding: 10px 20px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 12px;
}
.demote-row {
  border-top: 1px solid var(--line-3);
}
/* 表头下边线已经画了一道，第一行别再叠一道 */
.demote-head + .demote-row {
  border-top: none;
}
.col-key {
  min-width: 0;
}
.col-owner {
  min-width: 0;
}
.col-time {
  color: var(--text-faint);
  font-size: 12px;
  text-align: right;
}
.list-foot {
  margin: 0;
  padding: 12px 20px;
  border-top: 1px solid var(--line-3);
}
@media (max-width: 600px) {
  /* 窄屏：表头收起，三列各占一行（定宽列在窄屏里会挤掉条目 key）。 */
  .demote-head {
    display: none;
  }
  .demote-row {
    grid-template-columns: minmax(0, 1fr);
    gap: 4px;
  }
  .col-time {
    text-align: left;
  }
}

/* 「建议废弃的待办」清单：一行一条。零新色 —— 归属 chip 走全局 .tag--owner，
   数字走 .num，行分隔线走 --border-2。 */
.agent-split {
  margin-bottom: 20px;
}
.agent-line {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 0;
}
.agent-line + .agent-line {
  border-top: 1px solid var(--border-2);
}
.agent-num {
  color: var(--text-dim);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}
</style>
