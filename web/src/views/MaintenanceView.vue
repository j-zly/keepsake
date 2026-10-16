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

    <!-- 常驻候选审核：后端写闸把「非主代理写 core」降级成 core_candidate，这里审。
         分级班车提名也是进同一个队列（不新造表），但**方向不同**：core_candidate 是
         「想进常驻」，tier:demote-candidate 是「班车建议退位」——批准/驳回的动作
         正好相反，所以两组各查各的、徽标上写清方向。 -->
    <section class="cand">
      <div class="filter-title">
        {{ candDir === 'demote' ? '退居建议（班车建议退出常驻层，待审）' : '常驻候选（申请进常驻层，待审）' }}
      </div>
      <el-radio-group v-model="candDir" class="dir-switch" size="small" @change="onCandDir">
        <el-radio-button value="promote">晋升候选</el-radio-button>
        <el-radio-button value="demote">退居建议</el-radio-button>
      </el-radio-group>
      <p class="filter-desc">
        {{
          candDir === 'demote'
            ? '常驻层吃掉太多注入预算时，班车按「最不活跃先走」提名退居；收到负反馈、或写侧管线判「这条与新事实冲突」时也会直接提名（手写条目只进建议、永不自动降，冲突证据会摆在卡上）。批准=退出常驻层（正文不删、不失效）；驳回=摘掉建议标，这条继续留在常驻层。'
            : '非主代理写 core 会被拦成候选，分级班车也会给够热的条目提名。批准后进入常驻层、每轮注入；驳回只摘掉候选标，条目仍留在检索层可召回。'
        }}
      </p>
      <div class="filter-result">
        <span>共 {{ candCount === null ? '—' : candCount }} 条待审</span>
      </div>

      <div v-if="candError" class="banner banner--danger">{{ candError }}</div>

      <div v-if="candLoading && !cands.length" class="panel pad">
        <el-skeleton :rows="3" animated />
      </div>
      <div v-else-if="!candError && !cands.length" class="panel">
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <circle cx="10.5" cy="10.5" r="6" />
            <path d="m15 15 4.5 4.5" stroke-linecap="round" />
          </svg>
          <p>
            <b>{{ candDir === 'demote' ? '没有退居建议' : '没有待审候选' }}</b>
            {{ candDir === 'demote' ? '常驻层水位没到告警线，或建议都已审完。' : '子代理写 core 的申请会出现在这里。' }}
          </p>
        </div>
      </div>
      <div v-else-if="cands.length" class="card-grid">
        <ItemCard v-for="row in cands" :key="row.key" :chip-tone="candDir === 'promote' ? 'accent' : ''">
          <template #tag>
            {{ candDir === 'demote' ? '退居建议' : `来源 ${agentOf(row.tags) || '未知'}` }}
          </template>
          <template #time>{{ toLocal(row.updated_at) }}</template>

          <span class="cell-key">
            <span class="k" :title="row.key">{{ row.key }}</span>
            <button class="copy-btn" title="复制 key" @click="doCopy(row.key)">
              <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
                stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
                <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
                <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
              </svg>
            </button>
          </span>
          <p class="card-body" :title="row.content">{{ row.content }}</p>

          <!-- 冲突证据：写侧管线判「这条常驻与新事实矛盾」时留下的 key（后端
               tier:evid:<key>）。审核的人得看得见跟哪条打架才判得了，所以这里
               按需懒加载正文；查不到就说查不到，不崩也不编。 -->
          <div v-if="candDir === 'demote' && evidKeysOf(row.tags).length" class="evid">
            <div v-for="k in evidKeysOf(row.tags)" :key="k" class="evid-row">
              <span class="evid-label">与这条新事实冲突：</span>
              <span class="evid-text" :title="evidText(k)">{{ evidText(k) }}</span>
            </div>
          </div>

          <template #tags>
            <span v-for="t in splitTags(row.tags)" :key="t" class="tag">{{ t }}</span>
          </template>

          <template #actions>
            <button class="act-btn" :disabled="busyC" @click="askApprove(row)">
              {{ candDir === 'demote' ? '批准退常驻' : '批准入常驻' }}
            </button>
            <button class="act-btn act-btn--danger" :disabled="busyC" @click="askReject(row)">驳回</button>
          </template>
        </ItemCard>
      </div>

      <!-- 分页：组件与「记忆条目」页同一个，props 口径也照抄那边（总数由接口给，不写死） -->
      <PagerBar
        v-if="cands.length"
        :total="candPagerTotal"
        :page="candPage"
        :page-size="candPageSize"
        :unknown="candCount === null"
        @update:page="onCandPage"
        @update:page-size="onCandSize"
      />
    </section>

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

    <!-- 候选审核确认框：批准/驳回共用一个实例，按 candKind 切标题与后果 -->
    <ConfirmDialog
      v-model="candOpen"
      :title="candTitle"
      :consequences="candConsequences"
      :confirm-text="candConfirmText"
      :tone="candKind === 'reject' ? 'danger' : 'primary'"
      :busy="busyC"
      @confirm="doCandidate"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
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
  loadCandidates()
  loadTiering()
})

// ---------------------------------------------------------------------------
// 常驻候选审核区
// ---------------------------------------------------------------------------

/** 后端写闸把非 primary 的 `core` 写入降级成的标签（常量与 provider 的 CORE_CANDIDATE_TAG 同名） */
const CANDIDATE_TAG = 'core_candidate'

/**
 * 分级班车提名的**退居**建议标（`keepsake/core/tiering.py` 的 DEMOTE_CANDIDATE_TAG）。
 * 刻意不是 core_candidate：那是「想进常驻」，这是「建议退位」，两个方向的批准动作
 * 正好相反（升 / 降），混在一个队列里审核页就分不清自己在批什么。
 */
const DEMOTE_CANDIDATE_TAG = 'tier:demote-candidate'

// ------------------------------------------------------------------
// 标签串的四个纯函数。**这一段必须零依赖**（只用 JS 内建）：
// tests/candidate-review.test.mjs 会原样抽出这段直接跑，改成 import 任何东西
// 测试就抽不出来 —— 所以想复用别处的话，先把它挪去 utils/ 再改测试。
//
// 标签判据是**逗号边界的完整标签相等**，与后端 `_has_tag` / `tag_match_sql`
// 同一套口径。这里是「按 , 切开、trim、整串比较」而不是 includes：
// `core_candidate_v2`、`xcore_candidate` 都不含 `core_candidate`，
// 写成 `s.includes(tag)` 会把别人的标签一起误伤。
// ------------------------------------------------------------------

/** 逗号串 → 标签数组（去空白、丢空项）。`" a , ,b "` ⇒ `['a','b']` */
function splitTags(s) {
  return String(s ?? '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

/**
 * 冲突证据标前缀（后端 `keepsake/core/tiering.py` 的 EVID_PREFIX + ':'）。
 * 退居建议卡上会带 `tier:evid:<新事实 key>`：写侧管线判「这条常驻与新事实冲突」
 * 时打的 —— 审核的人得看得见「它到底跟哪条打架」才判得了。
 */
const EVID_PREFIX = 'tier:evid:'

/**
 * 从逗号串里摘掉一个**完整标签**，其余原样保留、顺序不变。
 * 幂等：标签不存在时原样返回（重复驳回同一条不会把别的标签吃掉）。
 * 本标签是唯一标签时返回空串 —— 正文不受影响，条目仍在检索层。
 *
 * 🔴 摘 `tier:demote-candidate` 时**连带摘掉它带的 `tier:evid:` 证据标**：
 * 证据标是「这条建议为什么被提」的从属物，方向标一摘它就成了没人看得见的孤儿
 * （列表按方向标过滤 ⇒ 永远不显示 ⇒ 白留）。两条路径（驳回 = 摘标、批准 =
 * demote 后按返回的 tags 摘标）都走这一个函数，所以不漏。
 */
function dropTag(s, tag) {
  const t = String(tag ?? '').trim()
  if (!t) return String(s ?? '')
  // 这里的字面量是**故意**不引 DEMOTE_CANDIDATE_TAG 的：上面那段纯函数要被单测
  // 原样抽出来单跑，段外的常量在那个作用域里不存在（抽出来就 ReferenceError）。
  // 代价是同一个标在页面里出现两次 —— 测试正反两面都钉着它（tiering-ui 钉常量、
  // 本段的测试钉联动），漂了会红。
  const alsoEvid = t === 'tier:demote-candidate'
  return splitTags(s)
    .filter((x) => x !== t && !(alsoEvid && x.startsWith(EVID_PREFIX)))
    .join(',')
}

/** 取出证据 key 列表（`tier:evid:<key>` 的 key，逗号边界口径同 splitTags）。无则 []。 */
function evidKeysOf(s) {
  return splitTags(s)
    .filter((t) => t.startsWith(EVID_PREFIX))
    .map((t) => t.slice(EVID_PREFIX.length).trim())
    .filter(Boolean)
}

/** 找 `agent:xxx` 标签里的 xxx；没有或格式不对返回空串（页面据此显示「未知」）。 */
function agentOf(s) {
  for (const t of splitTags(s)) {
    if (t.startsWith('agent:') && t.slice(6).trim()) return t.slice(6).trim()
  }
  return ''
}

const cands = ref([])
/** 当前审核方向：promote=晋升候选（core_candidate）/ demote=退居建议 */
const candDir = ref('promote')
/** 本方向对应的标签串（两个方向各查各的：`fragments?tag=` 走存储层按标签列） */
const dirTag = computed(() => (candDir.value === 'demote' ? DEMOTE_CANDIDATE_TAG : CANDIDATE_TAG))
/** 来自接口 count，不写死；扫描触顶时后端给 null（显示 '—'） */
const candCount = ref(null)
const candHasMore = ref(false)
const candPage = ref(1)
const candPageSize = ref(20)
const candLoading = ref(false)
const candError = ref('')

/**
 * 冲突证据正文缓存：key → `null`（读取中）/ 文本 / `''`（查不到：条目被删或无权限）。
 * 一页 20 张卡 ⇒ 最多 20 次既有读接口调用，翻回本页不再重拉。
 * 懒加载而不是列表接口直接带：证据条目**不在**这一页里，让列表接口按 key 反查
 * 等于让分页接口替人做 N+1；而漏查一条也只是显示「证据已不可查」，不崩。
 */
const evidCache = ref({})

/** 证据展示文案：读取中 / 正文 / 证据已不可查（三态都摆出来，绝不空白让人猜） */
function evidText(key) {
  const v = evidCache.value[key]
  if (v === undefined || v === null) return '读取中…'
  return v || '证据已不可查'
}

/** 拉这批卡的证据正文；逐条独立失败（一条 404 不许拖垮整卡）。只退居方向要拉。 */
async function loadEvid(rows) {
  await Promise.all(
    rows
      .flatMap((row) => (candDir.value === 'demote' ? evidKeysOf(row.tags) : []))
      .map(async (k) => {
        if (k in evidCache.value) return
        evidCache.value[k] = null
        try {
          const d = await api.fragment(k)
          evidCache.value[k] = String(d?.content || '')
        } catch {
          evidCache.value[k] = ''
        }
      }),
  )
}

/**
 * 分页条总数，口径与「记忆条目」页（FragmentsView）完全一致：
 * 接口给了 count 就用它；没给（count=null）时按 has_more 撑出「下一页」——
 * 有下一页才报 page*page_size+1，翻到那儿再由 has_more=false 收口。
 */
const candPagerTotal = computed(() =>
  candCount.value ?? (candHasMore.value ? candPage.value * candPageSize.value + 1 : cands.value.length),
)

// 防串：请求序号（同 KbView 的 seq 写法）。批准后立刻重拉，旧响应不许覆盖新列表。
let candSeq = 0

/** 批准 / 驳回共用一个确认框实例：kind 决定标题、后果与按钮色 */
const candOpen = ref(false)
const candKind = ref('')
const candRow = ref(null)
const busyC = ref(false)

const candTitle = computed(() => {
  if (candKind.value === 'reject') return candDir.value === 'demote' ? '驳回这条退居建议？' : '驳回这条常驻候选？'
  return candDir.value === 'demote' ? '批准这条退出常驻层？' : '批准这条进常驻层？'
})
const candConfirmText = computed(() => {
  if (candKind.value === 'reject') return '驳回'
  return candDir.value === 'demote' ? '批准退常驻' : '批准入常驻'
})
const candConsequences = computed(() => {
  const row = candRow.value
  if (!row) return []
  const tag = dirTag.value
  if (candKind.value === 'reject') {
    // 剩余标签直接从行里的 tags 算（FragmentItem 带 tags 字段，不必再打一次详情接口）
    const rest = splitTags(dropTag(row.tags, tag)).join('、') || '（无）'
    if (candDir.value === 'demote') {
      const lines = [
        `摘掉 ${DEMOTE_CANDIDATE_TAG} 标签，其余标签原样保留：${rest}。`,
        '这条继续留在常驻层、继续每轮注入 —— 驳回只是告诉班车「别再提它」。',
        '常驻层水位仍高时，下一轮班车还会重新提名（自动升进来的条目更容易被提名）。',
      ]
      if (evidKeysOf(row.tags).length) {
        lines.push(`连同 ${evidKeysOf(row.tags).length} 条冲突证据标一起摘掉 —— 证据标只是「这条建议为什么被提」的从属物，留着就成了没人看得见的孤儿。`)
      }
      return lines
    }
    return [
      `摘掉 ${tag} 标签，其余标签原样保留：${rest}。`,
      '正文不删、条目不失效 —— 它仍留在检索层，照样能被召回。',
      '只是想换个时机进常驻层的话，日后重新「升常驻」就行。',
    ]
  }
  if (candDir.value === 'demote') {
    return [
      `把 ${row.key} 退出常驻层（建议标同时摘掉，这张卡片从待审列表消失）：之后每轮注入不再包含它。`,
      '正文不删、条目不失效 —— 它回到检索层，仍能被召回，随时可以重新升回来。',
      `腾出来的注入预算是实打实的：常驻层每条都在每轮 prompt 里占字符。`,
    ]
  }
  return [
    `把 ${row.key} 提为常驻（候选标同时摘掉，这张卡片从待审列表消失）：之后每轮 system_prompt 都会全量注入这条。`,
    '每条常驻条目都占注入预算，常驻层条数越多，每轮 prompt 越长。',
    '随时可以在「记忆条目」页降回检索层，正文不会丢。',
  ]
})

/** 切方向：页码与计数都得重置，否则从第 5 页切过去会是空列表。 */
function onCandDir() {
  candPage.value = 1
  candCount.value = null
  cands.value = []
  loadCandidates()
}

/** 候选列表：按方向的标签走存储层的按标签列（不是 BM25 检索）。服务端分页。 */
async function loadCandidates() {
  const seq = ++candSeq
  candLoading.value = true
  try {
    const data = await api.fragments({
      tag: dirTag.value,
      page: candPage.value,
      page_size: candPageSize.value,
    })
    if (seq !== candSeq) return
    // 把末页最后一条审完后本页会空掉 —— 空态文案是「没有待审候选」，留着就是撒谎。
    // 退一页重拉，页码单调递减所以不会递归不止。
    if (!data.items.length && candPage.value > 1 && !data.has_more) {
      candPage.value -= 1
      return loadCandidates()
    }
    cands.value = data.items
    candCount.value = data.count
    candHasMore.value = data.has_more
    candError.value = ''
    // 证据懒加载：不 await（列表先出，证据后到），失败全在 loadEvid 内部兜住
    loadEvid(data.items)
  } catch (e) {
    if (seq !== candSeq) return
    cands.value = []
    candCount.value = null
    candHasMore.value = false
    candError.value = `读取常驻候选失败：${e.message}`
  } finally {
    if (seq === candSeq) candLoading.value = false
  }
}

function onCandPage(p) {
  candPage.value = p
  loadCandidates()
}

function onCandSize(s) {
  candPageSize.value = s
  candPage.value = 1
  loadCandidates()
}

function askApprove(row) {
  candRow.value = row
  candKind.value = 'approve'
  candOpen.value = true
}

function askReject(row) {
  candRow.value = row
  candKind.value = 'reject'
  candOpen.value = true
}

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** ConfirmDialog 确认回调：方向决定批准动作（升/降），驳回一律只摘方向标。 */
async function doCandidate() {
  const row = candRow.value
  if (!row) return
  const reject = candKind.value === 'reject'
  const demoteDir = candDir.value === 'demote'
  const tag = dirTag.value
  busyC.value = true
  try {
    if (reject) {
      await api.updateFragment(row.key, { tags: dropTag(row.tags, tag) })
    } else if (demoteDir) {
      // 与晋升方向同一套纪律：先 demote 再摘建议标。反过来的话 demote 失败时建议标
      // 已经被摘掉 ⇒ 这条既没退常驻、也不再出现在待审列表里，白审一场。
      // demote 只会「摘 core」、不认识 tier:demote-candidate ⇒ 不补这一步卡片赖着不走。
      const r = await api.demote(row.key)
      await api.updateFragment(row.key, { tags: dropTag(r.tags, DEMOTE_CANDIDATE_TAG) })
    } else {
      // 🔴 顺序不能反：先 promote 再摘候选标。反过来的话，万一 promote 失败，
      // 候选标已经被摘掉 ⇒ 这条申请既没进常驻层、也不再出现在待审列表里，白审一场。
      // promote 只会「加 core」、不摘 core_candidate（后端 _with_core 不管这个标签）
      // ⇒ 不补这一步，卡片批准完还赖在待审列表里。摘标以 promote **返回的** tags 为准
      // （那是后端 _with_core 刚算出的权威值，比拿列表行里可能已过期的 tags 重推更稳）。
      const r = await api.promote(row.key)
      await api.updateFragment(row.key, { tags: dropTag(r.tags, CANDIDATE_TAG) })
    }
    candOpen.value = false
    const done = reject
      ? `已驳回，仍在检索层：${row.key}`
      : demoteDir
        ? `已批准退常驻层：${row.key}`
        : `已批准入常驻层：${row.key}`
    showToast(done)
    // 常驻层条数会变，候选列表也会变 —— 两边一起刷
    load()
    loadCandidates()
  } catch (e) {
    showError(`${reject ? '驳回' : '批准'}失败：${e.message}`)
  } finally {
    busyC.value = false
  }
}

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
.cand {
  margin-top: 20px;
}
.cand .filter-result {
  margin: 10px 0 14px;
}
.pad {
  padding: 20px;
}
.dir-switch {
  margin: 8px 0 4px;
}
/* 冲突证据：两行夹住（label 不折、正文超长截断），窄卡里也不会把卡片撑变形 */
.evid {
  margin: 6px 0 2px;
  padding: 6px 8px;
  border-left: 2px solid var(--border-2);
  background: var(--panel);
  font-size: 12px;
  color: var(--text-faint);
}
.evid-row {
  display: flex;
  gap: 6px;
  align-items: baseline;
  min-width: 0;
}
.evid-label {
  flex: none;
}
.evid-text {
  flex: 1;
  min-width: 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--text-dim);
}
.src {
  margin-top: 10px;
  color: var(--text-faint);
  font-size: 12px;
}
</style>