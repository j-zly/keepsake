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

    <div class="banner">
      dry-run 零写入，可放心点；实跑必须手输当前计数，且会先自动备份受影响记忆条目。
    </div>
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
    <div class="src">
      分级班车：每天一次 cron 跑三条规则（够热提名 / 挂够天数自动升 / 预算吃紧提名退位）。
      <b>缺省关死</b>——先用 <code>scripts/tiering_cron.py --dry</code> 人肉观察几天，看清 stats 再在这里开总开关。
    </div>
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
        <el-form-item label="升提名：窗口天数 / 最少命中数">
          <el-input-number v-model="tDraft.promote_nominate.window_days" :min="1" :max="365" />
          <el-input-number v-model="tDraft.promote_nominate.min_hits" :min="1" :max="100000" />
        </el-form-item>
        <el-form-item label="自动升：候选挂够天数 / 近窗天数 / 近窗最少命中数">
          <el-input-number v-model="tDraft.promote_auto.age_days" :min="1" :max="3650" />
          <el-input-number v-model="tDraft.promote_auto.recent_window_days" :min="1" :max="365" />
          <el-input-number v-model="tDraft.promote_auto.recent_min_hits" :min="1" :max="100000" />
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
import { showError, showToast } from '../utils/dialog'

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
  promote_nominate: { window_days: 7, min_hits: 5 },
  promote_auto: { age_days: 15, recent_window_days: 7, recent_min_hits: 2 },
  demote_pressure: { warn_ratio: 0.9, release_ratio: 0.8, force_at_full: true },
  demote_exempt: { explicit_core: true },
  batch_cap: 50,
})
const tConfirm = ref('')

/** 保存条件：阈值都是正数 + confirm 逐字等于当前生效 enabled（后端同一口径） */
const canSaveT = computed(() => {
  const d = tDraft.value
  if (!d) return false
  const nums = [d.promote_nominate.window_days, d.promote_nominate.min_hits,
    d.promote_auto.age_days, d.promote_auto.recent_window_days,
    d.promote_auto.recent_min_hits, d.batch_cap]
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
</style>