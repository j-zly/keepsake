<!--
  知识库审核（批2 的待审队列，独立成页）。

  为什么从 KbView 里搬出来：KbView 一页里塞着文档浏览 / 检索 / 新增 / 导入导出，
  待审队列只能靠点顶部那颗 chip 才展开、还固定 20 条一页。审核是**逐条判**的动作
  （看正文预览 → 判断来源可不可信 → 批准或驳回），挤在别页的抽屉里既看不清也翻不动。
  现在这页只干这一件事，分页与每页条数都摆在明面上（与「记忆审核」/review 同款配方）。

  数据源一字未改：仍是 GET /kb/pending（列表 + count + pending_count）与
  POST /kb/documents/{id}/approve|reject。计数全部来自接口，前端零写死数字。

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
         重新审核的唯一线索 —— 所以这里不给「跳过」，只能取消。 -->
    <el-dialog
      v-model="rejectAsk"
      title="驳回这篇待审文档？"
      width="520px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyReview"
      :show-close="!busyReview"
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
        理由会记进该文档的审核台账。
      </p>
      <template #footer>
        <el-button :disabled="busyReview" @click="rejectAsk = false">取消</el-button>
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
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
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

/** 批准/驳回共用一个确认框实例（与记忆候选审核同款） */
const reviewOpen = ref(false)
const reviewKind = ref('')
const reviewRow = ref(null)
const busyReview = ref(false)
/** 驳回理由：先在弹框里填（必填），再走 ConfirmDialog 二次确认 */
const rejectAsk = ref(false)
const rejectReason = ref('')

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
  rejectReason.value = ''
  rejectAsk.value = true
}

function askRejectConfirm() {
  if (!rejectReason.value.trim()) return
  rejectAsk.value = false
  reviewOpen.value = true
}

/**
 * 批准 = 此刻才切块进检索；驳回 = 只标状态、正文留着。审完只重拉本页队列 ——
 * 侧栏徽标有自己的 60s 轮询兜底（「知识库 › 审核」那个数），最坏滞后一轮。
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
    await loadPending()
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
</style>
