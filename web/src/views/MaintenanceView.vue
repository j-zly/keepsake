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
    </div>
    <pre v-if="opResult" class="pre">{{ opResult }}</pre>

    <!-- 常驻候选审核：后端写闸把「非主代理写 core」降级成 core_candidate，这里审 -->
    <section class="cand">
      <div class="filter-title">常驻候选（子代理申请进常驻层，待审）</div>
      <p class="filter-desc">
        非主代理写 core 会被拦成候选：批准后进入常驻层、每轮注入；驳回只摘掉候选标，条目仍留在检索层可召回。
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
          <p><b>没有待审候选</b>子代理写 core 的申请会出现在这里。</p>
        </div>
      </div>
      <div v-else-if="cands.length" class="card-grid">
        <ItemCard v-for="row in cands" :key="row.key" chip-tone="accent">
          <template #tag>来源 {{ agentOf(row.tags) || '未知' }}</template>
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

          <template #tags>
            <span v-for="t in splitTags(row.tags)" :key="t" class="tag">{{ t }}</span>
          </template>

          <template #actions>
            <button class="act-btn" :disabled="busyC" @click="askApprove(row)">批准入常驻</button>
            <button class="act-btn act-btn--danger" :disabled="busyC" @click="askReject(row)">驳回</button>
          </template>
        </ItemCard>
      </div>
    </section>

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
})

// ---------------------------------------------------------------------------
// 常驻候选审核区
// ---------------------------------------------------------------------------

/** 后端写闸把非 primary 的 `core` 写入降级成的标签（常量与 provider 的 CORE_CANDIDATE_TAG 同名） */
const CANDIDATE_TAG = 'core_candidate'

// ------------------------------------------------------------------
// 标签串的三个纯函数。**这一段必须零依赖**（只用 JS 内建）：
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
 * 从逗号串里摘掉一个**完整标签**，其余原样保留、顺序不变。
 * 幂等：标签不存在时原样返回（重复驳回同一条不会把别的标签吃掉）。
 * 本标签是唯一标签时返回空串 —— 正文不受影响，条目仍在检索层。
 */
function dropTag(s, tag) {
  const t = String(tag ?? '').trim()
  if (!t) return String(s ?? '')
  return splitTags(s)
    .filter((x) => x !== t)
    .join(',')
}

/** 找 `agent:xxx` 标签里的 xxx；没有或格式不对返回空串（页面据此显示「未知」）。 */
function agentOf(s) {
  for (const t of splitTags(s)) {
    if (t.startsWith('agent:') && t.slice(6).trim()) return t.slice(6).trim()
  }
  return ''
}

const cands = ref([])
/** 来自接口 count，不写死；扫描触顶时后端给 null（显示 '—'） */
const candCount = ref(null)
const candLoading = ref(false)
const candError = ref('')

// 防串：请求序号（同 KbView 的 seq 写法）。批准后立刻重拉，旧响应不许覆盖新列表。
let candSeq = 0

/** 批准 / 驳回共用一个确认框实例：kind 决定标题、后果与按钮色 */
const candOpen = ref(false)
const candKind = ref('')
const candRow = ref(null)
const busyC = ref(false)

const candTitle = computed(() =>
  candKind.value === 'reject' ? '驳回这条常驻候选？' : '批准这条进常驻层？',
)
const candConfirmText = computed(() => (candKind.value === 'reject' ? '驳回' : '批准入常驻'))
const candConsequences = computed(() => {
  const row = candRow.value
  if (!row) return []
  if (candKind.value === 'reject') {
    // 剩余标签直接从行里的 tags 算（FragmentItem 带 tags 字段，不必再打一次详情接口）
    const rest = splitTags(dropTag(row.tags, CANDIDATE_TAG)).join('、') || '（无）'
    return [
      `摘掉 ${CANDIDATE_TAG} 标签，其余标签原样保留：${rest}。`,
      '正文不删、条目不失效 —— 它仍留在检索层，照样能被召回。',
      '只是想换个时机进常驻层的话，日后重新「升常驻」就行。',
    ]
  }
  return [
    `把 ${row.key} 提为常驻（候选标同时摘掉，这张卡片从待审列表消失）：之后每轮 system_prompt 都会全量注入这条。`,
    '每条常驻条目都占注入预算，常驻层条数越多，每轮 prompt 越长。',
    '随时可以在「记忆条目」页降回检索层，正文不会丢。',
  ]
})

/** 候选列表：`tag=core_candidate` 走存储层的按标签列（不是 BM25 检索）。 */
async function loadCandidates() {
  const seq = ++candSeq
  candLoading.value = true
  try {
    const data = await api.fragments({ tag: CANDIDATE_TAG, page: 1, page_size: 50 })
    if (seq !== candSeq) return
    cands.value = data.items
    candCount.value = data.count
    candError.value = ''
  } catch (e) {
    if (seq !== candSeq) return
    cands.value = []
    candCount.value = null
    candError.value = `读取常驻候选失败：${e.message}`
  } finally {
    if (seq === candSeq) candLoading.value = false
  }
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

/** ConfirmDialog 确认回调：批准=打 core 标签，驳回=只摘候选标（正文与别的标签都不动）。 */
async function doCandidate() {
  const row = candRow.value
  if (!row) return
  const reject = candKind.value === 'reject'
  busyC.value = true
  try {
    if (reject) {
      await api.updateFragment(row.key, { tags: dropTag(row.tags, CANDIDATE_TAG) })
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
    showToast(reject ? `已驳回，仍在检索层：${row.key}` : `已批准入常驻层：${row.key}`)
    // 常驻层条数会变，候选列表也会变 —— 两边一起刷
    load()
    loadCandidates()
  } catch (e) {
    showError(`${reject ? '驳回' : '批准'}失败：${e.message}`)
  } finally {
    busyC.value = false
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
</style>