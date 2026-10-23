<!--
  知识库审核（批2 的待审队列，独立成页）。

  为什么从 KbView 里搬出来：KbView 一页里塞着文档浏览 / 检索 / 新增 / 导入导出，
  待审队列只能靠点顶部那颗 chip 才展开、还固定 20 条一页。审核是**逐条判**的动作
  （看正文预览 → 判断来源可不可信 → 批准或驳回），挤在别页的抽屉里既看不清也翻不动。
  现在这页只干这一件事，分页与每页条数都摆在明面上（与「记忆审核」/review 同款配方）。

  数据源一字未改：仍是 GET /kb/pending（列表 + count + pending_count）与
  POST /kb/documents/{id}/approve|reject。计数全部来自接口，前端零写死数字。

  🆕 **勾选批量**（与记忆条目页同一口径）：跨页保留的选中集合（Set）+ 共用的
  SelectionBar 组件 → POST /kb/documents/bulk-approve | bulk-reject
  （确认串 = 选中条数；驳回整批一个理由且必填；逐条成败回包，单条失败不拖累其余）。
  审完立刻重拉队列，并喊一声让侧栏那枚「待审 N」徽标跟上。

  🆕 **真删**（2026-10 kb_del_stats_r2）：行操作「删除」→ POST /kb/documents/{id}/delete
  （确认串 = 手输**文档标题全文**），批量条「批量删除」→ POST /kb/documents/bulk-delete
  （确认串 = 选中条数）。**待审与已驳回都给**这一条：这两类行本来就还没生效
  （没切过块、没进过检索），删掉它 = 「这条我不收」，不是把它偷偷放行；
  删前逐篇备份、审计照记。删完刷新列表与页头计数，并喊一声刷侧栏徽标。

  🔴 语义照旧，搬过来一个字没改：pending 文档此刻**一块切片都没有**，正文只能看
  review_meta 的预览（这正是「未审正文不进检索索引」的另一面：它连自己都搜不到）；
  批准 = 此刻才切块进检索；驳回 = 只标状态不删正文，随时可以再审。
  🔴 站内手建与主代理写入不经过这一关 —— 后端按「谁在写」判，前端不设第二道闸。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">知识库审核</h1>
      <p class="page-sub">
        子代理（非主代理）提交的知识文档一律先进这里，批准之后才切块进检索；驳回只标状态、不删正文，随时可以再审。站内手建与主代理写入不经过这一关。
      </p>
    </div>

    <section>
      <!-- 计数行：两个数都来自 /kb/pending（pending_count = 真待审，count = 待审+已驳回
           的队列全长）。没拉到之前显示 '—'，不拿 0 冒充「已审完」。 -->
      <div class="filter-result">
        <span>待审 {{ num(pendCount) }} · 队列共 {{ num(pendTotal) }}</span>
        <!-- 每页条数：审核是逐条判的动作，默认 10（一屏不看太多）；页上分段控件与
             PagerBar 的下拉写的是同一个 pendSize，谁改都生效。
             ponytail: PagerBar 自己的下拉固定 20/50/100，认不出 10，选了 10 之后
               那个下拉会显示空白。上限：给 PagerBar 加个 sizes prop（默认 [20,50,100]），
               本页传 [10,20,50]，然后删掉这个分段控件。 -->
        <div class="size-switch">
          每页
          <el-radio-group v-model="pendSize" size="small" @change="onPendSize">
            <el-radio-button v-for="n in PAGE_SIZES" :key="n" :value="n">{{ n }}</el-radio-button>
          </el-radio-group>
        </div>
      </div>

      <div v-if="pendError" class="banner banner--danger">{{ pendError }}</div>

      <!-- 勾选批量：跨页保留的选中集合 + 批量批准 / 批量驳回（与记忆条目页同一口径，
           同一个 SelectionBar 组件）。勾了 20 篇再翻页，勾选**不清空** ——
           批量动作最怕「以为只审了本页」。 -->
      <SelectionBar
        v-if="pendRows.length"
        :count="selCount"
        :page-count="pendRows.length"
        :page-selected="pageSelected"
        @select-all="selectAll"
        @clear="clearSel"
      >
        <el-button :disabled="!selCount || busyBulk" @click="askBulk('approve')">批量批准</el-button>
        <el-button
          type="danger"
          plain
          :disabled="!selCount || busyBulk"
          @click="openBulkReject()"
        >
          批量驳回
        </el-button>
        <!-- 批量真删：走既有 bulk-delete（确认串 = 选中条数），逐篇备份、
             单条失败不拖累其余 —— 与「知识库」页的批量删除同一套尺子。 -->
        <el-button
          type="danger"
          :disabled="!selCount || busyBulk"
          @click="askBulkDelete()"
        >
          批量删除
        </el-button>
      </SelectionBar>

      <div v-if="pendLoading && !pendRows.length" class="panel pad">
        <el-skeleton :rows="2" animated />
      </div>
      <div v-else-if="!pendError && !pendRows.length" class="panel">
        <div class="empty">
          <p><b>没有待审的文档。</b>子代理提交的知识文档会出现在这里；<br />子代理经 MCP 提交的知识文档会进入这里。</p>
        </div>
      </div>
      <div v-else class="card-grid">
        <ItemCard v-for="row in pendRows" :key="row.doc_id" chip-tone="accent">
          <template #tag>{{ row.status === 'rejected' ? '已驳回' : '待审' }}</template>
          <template v-if="row.submitted_at" #time>{{ toLocal(row.submitted_at) }}</template>

          <!-- 勾选框：跨页累积（批量批准/批量驳回用） -->
          <div class="pick-row">
            <el-checkbox
              class="pick"
              :model-value="isOn(row.doc_id)"
              title="勾选这篇（可跨页累积）"
              @change="toggle(row.doc_id)"
              @click.stop
            />
          </div>

          <p class="card-title" :title="row.title">{{ row.title || '（无标题）' }}</p>
          <p class="card-body" :title="row.preview">{{ row.preview || '（没有提交正文）' }}</p>

          <template #tags>
            <span class="tag mono">{{ row.doc_id }}</span>
            <span class="tag" :class="`tag--${typeOf(row)}`">
              {{ typeLabel(row) }}<template v-if="typeOf(row) === 'link'"> ↗</template>
            </span>
            <span v-for="t in tagsOf(row)" :key="t" class="tag" :title="`标签：${t}`">{{ t }}</span>
            <span class="tag">来源 {{ row.submitted_by || '未知' }}</span>
            <!-- 查重提示：同名/近重复不拒收，但这篇得让人看见 -->
            <span v-if="row.dup_of" class="tag tag--link" :title="`与《${row.dup_of_title}》重复`">
              {{ row.dup_exact ? '同名重复' : '近重复' }} · {{ row.dup_of_title }}
            </span>
            <span v-if="row.reject_reason" class="tag">驳回理由：{{ row.reject_reason }}</span>
          </template>

          <template #actions>
            <button class="act-btn" :disabled="busyReview" @click="askApprove(row)">
              {{ row.status === 'rejected' ? '重新批准' : '批准入库' }}
            </button>
            <button
              v-if="row.status !== 'rejected'"
              class="act-btn act-btn--danger"
              :disabled="busyReview"
              @click="openReject(row)"
            >
              驳回
            </button>
            <!-- 真删：待审与已驳回**都**给这一条（这篇从没生效过，删掉 =
                 「这条我不收」，不是把它偷偷放行）。走既有单条 delete 端点，
                 确认串 = 手输文档标题全文（后端还要再校验一次）。 -->
            <button
              class="act-btn act-btn--danger"
              :disabled="busyReview || busyDelete"
              @click="askDelete(row)"
            >
              删除
            </button>
          </template>
        </ItemCard>
      </div>
      <PagerBar
        v-if="pendRows.length"
        :total="pendTotal"
        :page="pendPage"
        :page-size="pendSize"
        @update:page="onPendPage"
        @update:page-size="onPendSize"
      />
    </section>

    <!-- 驳回理由：必填（后端同样 400 reason_required）。驳回不删正文，理由是日后
         重新审核的唯一线索 —— 所以这里不给「跳过」，只能取消。
         单条与批量共用这一个弹框（批量 = 整批一个理由）。 -->
    <el-dialog
      v-model="rejectAsk"
      :title="rejectBulk ? '批量驳回选中的待审文档？' : '驳回这篇待审文档？'"
      width="520px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyReview && !busyBulk"
      :show-close="!busyReview && !busyBulk"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="驳回理由（必填）">
          <el-input
            v-model="rejectReason"
            type="textarea"
            :rows="4"
            placeholder="为什么这篇不能进知识库？（例如：与已有手册重复、来源不可信、没有出处）"
          />
        </el-form-item>
      </el-form>
      <p class="tip">
        驳回只是把它标成「已驳回」，正文不删（子代理随时能改好重提，你也能在队列里重新批准）。
        理由会记进该文档的审核台账<template v-if="rejectBulk">；批量驳回时这一条理由覆盖整批</template>。
      </p>
      <template #footer>
        <el-button :disabled="busyReview || busyBulk" @click="rejectAsk = false">取消</el-button>
        <el-button
          type="danger"
          :disabled="!rejectReason.trim()"
          @click="askRejectConfirm"
        >
          下一步：确认驳回
        </el-button>
      </template>
    </el-dialog>

    <!-- 审核动作二次确认：批准/驳回共用一个实例（与记忆候选审核同款，不造第三种交互） -->
    <ConfirmDialog
      v-model="reviewOpen"
      :title="reviewTitle"
      :consequences="reviewConsequences"
      :confirm-text="reviewConfirmText"
      :tone="reviewKind === 'reject' ? 'danger' : 'primary'"
      :busy="busyReview"
      @confirm="doReview"
    />

    <!-- 勾选批量的二次确认：expect = 选中条数（与后端 confirm 同一口径）；
         语气按动作分（批准=primary 蓝，驳回/删除=danger 红）。 -->
    <ConfirmDialog
      v-model="bulkOpen"
      :title="bulkTitle"
      :consequences="bulkConsequences"
      :expect="bulkExpect"
      :confirm-text="bulkConfirmText"
      :tone="bulkKind === 'approve' ? 'primary' : 'danger'"
      :busy="busyBulk"
      @confirm="doBulk"
    />

    <!-- 真删单条：确认串 = 文档标题全文（与「知识库」页的真删同一档防手滑）。 -->
    <ConfirmDialog
      v-model="deleteOpen"
      :title="deleteTitle"
      :consequences="deleteConsequences"
      :expect="deleteExpect"
      confirm-text="确认删除"
      tone="danger"
      :busy="busyDelete"
      @confirm="doDelete"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import SelectionBar from '../components/SelectionBar.vue'
import { api } from '../api'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

onMounted(() => {
  loadPending()
})

// ---------------------------------------------------------------------------
// 待审队列（批2：子代理提交的知识文档先审后用）
// ---------------------------------------------------------------------------

/** 队列里的行（pending + rejected 一起给；pending_count 是页头与侧栏徽标上那个数） */
const pendRows = ref([])
/** null = 还没拉到，页头显示 '—'（不拿 0 冒充「已审完」） */
const pendCount = ref(null)
const pendTotal = ref(null)
const pendPage = ref(1)
/** 默认 10 条/页：审核是逐条判的动作，一屏铺 20 张卡要滚很久才看完一眼。可选 10/20/50 */
const PAGE_SIZES = [10, 20, 50]
const pendSize = ref(10)
const pendLoading = ref(false)
const pendError = ref('')

/** null → '—'；其余原样（数字由接口给，前端不另算） */
function num(v) {
  return v === null || v === undefined ? '—' : v
}

/** 类型徽标判据与「知识库」页同一套：🔴 字段缺席（旧库未迁移）按 note 缺省，不假装是技能 */
const TYPE_LABEL = {
  skill: '技能', note: '手记', article: '文章', link: '链接', image: '图文',
}
function typeOf(row) {
  return row?.content_type || 'note'
}
function typeLabel(row) {
  return TYPE_LABEL[typeOf(row)] || typeOf(row)
}

/** 标签 chip：字段缺席（老库未迁移）或空数组 ⇒ 空数组（当「没打标」，不编标签） */
function tagsOf(row) {
  return Array.isArray(row?.tags) ? row.tags : []
}

// ---------------------------------------------------------------------------
// 勾选批量（批准 / 驳回）—— 与记忆条目页同一口径：Set 存 id、翻页不清空
// ---------------------------------------------------------------------------
// 🔴 选中集合存 id 的 Set，**翻页不清空**：批量动作最怕「以为只审了本页」，
// 而翻页清空会让人在第 3 页以为「已选 0 条」而漏掉前两页的勾选。
// 每次改动都整体换一个新 Set（ref 追的是引用），省掉 Vue 集合代理的坑。
const sel = ref(new Set())
const selIds = computed(() => [...sel.value])
const selCount = computed(() => sel.value.size)
const pageSelected = computed(() => pendRows.value.filter((r) => sel.value.has(r.doc_id)).length)

function isOn(id) {
  return sel.value.has(id)
}
function toggle(id) {
  const next = new Set(sel.value)
  next.has(id) ? next.delete(id) : next.add(id)
  sel.value = next
}
/** 「全选本页」只动当前页：跨页累积靠逐页勾选，全选的口径不许含糊 */
function selectAll(on) {
  const next = new Set(sel.value)
  pendRows.value.forEach((r) => (on ? next.add(r.doc_id) : next.delete(r.doc_id)))
  sel.value = next
}
function clearSel() {
  sel.value = new Set()
}

/** 批量动作：'approve' | 'reject' | 'delete'（语气与文案按它分） */
const bulkKind = ref('')
const bulkOpen = ref(false)
const busyBulk = ref(false)
/** 确认串 = 当前选中条数（与后端 confirm 同一口径，后端还会再校验一次） */
const bulkExpect = computed(() => String(selCount.value))
const bulkTitle = computed(() => ({
  reject: '批量驳回选中的待审文档？',
  delete: '批量删除选中的待审文档？',
}[bulkKind.value] || '批量批准选中的待审文档？'))
const bulkConfirmText = computed(() => ({
  reject: '批量驳回',
  delete: '批量删除',
}[bulkKind.value] || '批量批准'))

/** 后果文案：只列前 10 个 id，多的折成一句（弹框不是 id 清单打印器） */
function _previewIds() {
  const ids = selIds.value
  return ids.slice(0, 10).join('、') + (ids.length > 10 ? `（共 ${ids.length} 篇，其余略）` : '')
}

const bulkConsequences = computed(() => {
  const n = selCount.value
  if (!n) return []
  const base = [`本次操作 ${n} 篇：${_previewIds()}`,
    `请照抄「${n}」确认 —— 这个数就是当前勾选条数，写进去一篇都不会多。`]
  if (bulkKind.value === 'reject') {
    base.push(`整批用同一个理由驳回：${rejectReason.value.trim() || '（还没填）'}`)
    base.push('驳回只是标状态、正文不删（子代理随时能改好重提，你也能在队列里重新批准）。')
  } else if (bulkKind.value === 'delete') {
    base.push('物理删除，不可恢复：每篇连同它的全部切片一起移除，检索与切片浏览立刻不再返回它们。')
    base.push('删前逐篇备份：每篇的文档行与全部切片正文落在 console_ops 备份目录的 document.<id>.before.json（写不进去那篇宁删不成，会单独报失败）。')
  } else {
    base.push('批准 = 此刻才按内容类型切块落 chunks，切完立刻进检索。')
  }
  base.push('单条失败（已生效 / 不在队列 / 不存在）只跳过那一条，其余照常执行 —— 结果会报「成功 X · 失败 Y」。')
  base.push('写操作会落审计日志（逐条各一行 + 批次汇总一行）。')
  return base
})

function askBulk(kind) {
  if (!selCount.value) return
  bulkKind.value = kind
  if (kind === 'reject') {
    openBulkReject()
    return
  }
  bulkOpen.value = true
}

/** 批量真删：直接进二次确认（确认串 = 选中条数，与后端 bulk-delete 同一口径）。 */
function askBulkDelete() {
  if (!selCount.value) return
  bulkKind.value = 'delete'
  bulkOpen.value = true
}

/** 批量驳回第一步：先填理由（必填），填完才进二次确认 */
function openBulkReject() {
  if (!selCount.value) return
  rejectBulk.value = true
  rejectReason.value = ''
  rejectAsk.value = true
}

/** 后端回的原因码 → 人话（后端给的是码，界面要说人话） */
const FAIL_REASON = {
  kb_not_owner: '不是你的文档',
  kb_doc_immutable: '只读域（灌入器维护）',
  kb_doc_missing: '文档不存在',
  kb_doc_not_pending: '不在待审队列',
  kb_doc_not_effective: '还在审核队列',
  kb_doc_id_invalid: 'id 不是整数',
  kb_review_no_body: '没有可入库的正文',
  kb_not_found: '不存在',
}
function reasonText(code) {
  return FAIL_REASON[code] || code || '未知原因'
}

/** 批量动作的中文名（提示语里要用） */
const KIND_TEXT = { approve: '批准', reject: '驳回', delete: '删除' }

/**
 * 批量实跑：走 bulk-approve / bulk-reject / bulk-delete 三个端点（api 层纯透传）。
 * 确认串原样传给后端 —— 后端还会再校验一次选中数，前端不算权威。
 * 成功的清出勾选、失败的**留在勾选里**（用户下一步就该逐条处理它们）。
 */
async function doBulk(typed) {
  const ids = selIds.value
  const kind = bulkKind.value
  if (!ids.length) return
  busyBulk.value = true
  try {
    const r = kind === 'reject'
      ? await api.kbBulkReject({ ids, confirm: typed, reason: rejectReason.value.trim() })
      : (kind === 'delete'
        ? await api.kbBulkDelete({ ids, confirm: typed })
        : await api.kbBulkApprove({ ids, confirm: typed }))
    bulkOpen.value = false
    const bad = new Set((r.failed || []).map((f) => f.id))
    sel.value = new Set([...sel.value].filter((id) => bad.has(id)))
    if (r.failed?.length) {
      // 失败明细列前 10 条（id + 原因），多了折成一句
      const why = r.failed.slice(0, 10).map((f) => `${f.id}（${reasonText(f.reason)}）`).join('、')
      showError(`成功 ${r.succeeded} · 失败 ${r.failed.length}：${why}${r.failed.length > 10 ? '…' : ''}`)
    } else {
      showToast(`批量${KIND_TEXT[kind] || '批准'}完成：成功 ${r.succeeded} 篇`)
    }
    rejectReason.value = ''
    await loadPending()
    notifyCounts()
  } catch (e) {
    showError(`批量${KIND_TEXT[kind] || '批准'}失败：${e.message}`)
  } finally {
    busyBulk.value = false
  }
}

// ---------------------------------------------------------------------------
// 真删单条（待审 / 已驳回都行）—— 走既有 POST /kb/documents/{id}/delete
// ---------------------------------------------------------------------------
// 🔴 确认串 = **手输文档标题全文**（与「知识库」页的真删同一档防手滑；
//    后端还会拿标题再校验一次，前端不算权威）。
const deleteRow = ref(null)
const deleteOpen = ref(false)
const busyDelete = ref(false)
const deleteTitle = computed(() => '彻底删除这篇知识库文档？')
const deleteExpect = computed(() => (deleteRow.value ? deleteRow.value.title : ''))
const deleteConsequences = computed(() => {
  const row = deleteRow.value
  if (!row) return []
  return [
    `真删《${row.title || '（无标题）'}》：连同它的全部切片一起移除，检索与切片浏览立刻不再返回它，不可恢复。`,
    '这篇还没生效过（待审 / 已驳回）—— 删掉它等于「这条我不收」，不是把它偷偷放进检索。',
    '删前自动备份：文档行与全部切片正文落在 console_ops 备份目录的 document.<id>.before.json（写不成则整次删除中止，这是唯一的回退路径）。',
    '请照抄上面的标题全文确认 —— 一个字都不能差。',
  ]
})

function askDelete(row) {
  deleteRow.value = row
  deleteOpen.value = true
}

async function doDelete(typed) {
  const row = deleteRow.value
  if (!row) return
  busyDelete.value = true
  try {
    await api.kbDeleteDocument(row.doc_id, typed)
    deleteOpen.value = false
    showToast(`已删除：${row.title || row.doc_id}`)
    // 删掉的那篇从勾选里去掉（它已经不在队列里了）
    const next = new Set(sel.value)
    next.delete(row.doc_id)
    sel.value = next
    await loadPending()
    notifyCounts()
  } catch (e) {
    showError(`删除失败：${e.message}`)
  } finally {
    busyDelete.value = false
  }
}

/** 审完立刻刷侧栏那枚「待审 N」徽标（不等 App.vue 的 60s 轮询那一轮） */
function notifyCounts() {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('ks:review-counts'))
  }
}

/** 批准/驳回共用一个确认框实例（与记忆候选审核同款） */
const reviewOpen = ref(false)
const reviewKind = ref('')
const reviewRow = ref(null)
const busyReview = ref(false)
/** 驳回理由：先在弹框里填（必填），再走 ConfirmDialog 二次确认 */
const rejectAsk = ref(false)
const rejectReason = ref('')
/** 这个理由弹框是**批量**用的（整批一个理由）还是单条用的 */
const rejectBulk = ref(false)

const reviewTitle = computed(() =>
  reviewKind.value === 'reject'
    ? '确认驳回这篇知识库文档？'
    : (reviewRow.value?.status === 'rejected' ? '重新批准已驳回的文档？' : '批准这篇知识库文档？'))
const reviewConfirmText = computed(() => (reviewKind.value === 'reject' ? '驳回' : '批准入库'))
const reviewConsequences = computed(() => {
  const row = reviewRow.value
  if (!row) return []
  if (reviewKind.value === 'reject') {
    return [
      `《${row.title}》标记为「已驳回」，立刻从待审队列沉底，并从列表/检索/导出里继续不可见。`,
      `驳回理由会记进审核台账：${rejectReason.value.trim()}`,
      '正文不删 —— 子代理可以改好重提，你也可以稍后在队列里重新批准。',
    ]
  }
  return [
    `《${row.title}》标记为已生效，按内容类型（${typeLabel(row)}）切块后落进 chunks。`,
    '此刻起它才会出现在「知识库」页的文档列表、被 /kb/search 检索到、也进入导出文件。',
    '切块与状态改动在同一个事务里：要么都成，要么都不成。',
    row.dup_of ? `注意：库里已有一篇同名/近重复的《${row.dup_of_title}》（#${row.dup_of}），批准后两篇都会留在检索里。` : '',
  ].filter(Boolean)
})

/** 队列：计数全部来自后端（前端零写死）；出错清空，不拿上一次的旧队列冒充现状。 */
async function loadPending() {
  pendLoading.value = true
  try {
    const r = await api.kbPending({ page: pendPage.value, page_size: pendSize.value })
    pendRows.value = Array.isArray(r?.items) ? r.items : []
    pendTotal.value = Number(r?.count) || 0
    pendCount.value = Number(r?.pending_count) || 0
    pendError.value = ''
  } catch (e) {
    pendRows.value = []
    pendTotal.value = 0
    pendCount.value = 0
    pendError.value = `读取待审队列失败：${e.message}`
  } finally {
    pendLoading.value = false
  }
}

function onPendPage(p) {
  pendPage.value = p
  loadPending()
}

/** 改每页条数回第 1 页（否则会停在一个不存在的页码上）。页上分段控件与 PagerBar 共用。 */
function onPendSize(s) {
  pendSize.value = Number(s)
  pendPage.value = 1
  loadPending()
}

function askApprove(row) {
  reviewRow.value = row
  reviewKind.value = 'approve'
  reviewOpen.value = true
}

/** 驳回第一步：先填理由（必填），填完才进二次确认 */
function openReject(row) {
  reviewRow.value = row
  reviewKind.value = 'reject'
  rejectBulk.value = false
  rejectReason.value = ''
  rejectAsk.value = true
}

function askRejectConfirm() {
  if (!rejectReason.value.trim()) return
  rejectAsk.value = false
  // 批量走批量那个确认框（expect = 选中条数），单条走单条那个
  if (rejectBulk.value) {
    bulkKind.value = 'reject'
    bulkOpen.value = true
    return
  }
  reviewOpen.value = true
}

/**
 * 批准 = 此刻才切块进检索；驳回 = 只标状态、正文留着。审完重拉本页队列，
 * 并喊一声让侧栏那枚「待审 N」徽标立刻跟上（不等 60s 轮询那一轮）。
 */
async function doReview() {
  const row = reviewRow.value
  if (!row) return
  const reject = reviewKind.value === 'reject'
  busyReview.value = true
  try {
    if (reject) {
      await api.kbRejectDocument(row.doc_id, rejectReason.value.trim())
      showToast(`已驳回：${row.title}（正文保留，可再审）`)
    } else {
      const r = await api.kbApproveDocument(row.doc_id)
      showToast(`已批准：${row.title}（切成 ${r.chunks_count} 块切片，已进检索）`)
    }
    reviewOpen.value = false
    rejectReason.value = ''
    // 审完的那篇从勾选里去掉（它已经不在队列里了）
    const next = new Set(sel.value)
    next.delete(row.doc_id)
    sel.value = next
    await loadPending()
    notifyCounts()
  } catch (e) {
    showError(`${reject ? '驳回' : '批准'}失败：${e.message}`)
  } finally {
    busyReview.value = false
  }
}
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 计数行与卡片之间留白（.filter-result 是 space-between 的两栏容器，margin-top 自带） */
.filter-result {
  margin-bottom: 14px;
}
/* 每页条数：跟计数同一行右挂，控件沿用方向切换那套 el-radio-button，不新造控件也不新配色 */
.size-switch {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
/* 弹框里的说明行：与 DetailDrawer / KbView 的 .tip 同一份配方（12px/1.7 --text-faint）——
   scoped 样式跨组件不生效，只能各写一份，数值不许自创 */
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
/* 类型徽标的变体：技能=灰描边（同现有 tag，只压低存在感）；链接=描边带 ↗（点得进去）。
   与 KbView 的同名 scoped 类同一份配方（scoped 不跨组件，各写一份，数值不新创）。 */
.tag--skill {
  opacity: 0.85;
}
.tag--link {
  border-color: var(--accent);
  color: var(--accent);
}
/* 卡上的勾选行：与记忆条目页同一份配方（勾选框在正文上方一行，不挤标题） */
.pick-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
</style>
