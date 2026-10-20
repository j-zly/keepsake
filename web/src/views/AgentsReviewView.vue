<!--
  代理审核（待审队列，独立成页 · 2026-10-20 从 AgentsView 拆出）。

  为什么拆：登记页一页要管改名 / 设角色 / 上下文过滤 / 吊销 / 登记，待审的那一摞
  埋在中间，批准这个动作要滚到很下面才看得见 —— 而待审的代理此刻**读写全拒**，
  恰恰是最该被一眼看见的那摞。现在这页只干这一件事（照知识库审核 / 记忆审核的先例）。

  数据源一字未改：仍是 GET /agents（items + count + pending_count）与
  POST /agents/{id}/approve|revoke（api 层那两条已有封装，页面不自己拼路径）。
  计数全部来自接口，前端零写死数字。

  🔴 pending_count 恒为**全量口径**（后端不随 q / page 变）：红条上的数永远等于
  全库待审数，不许在前端按当前页过滤后数。

  🔴 一次性凭据：明文只回来一次，两页（登记页 / 本页）共用同一个弹框组件
  （components/CredentialDialog.vue）—— **一份实现，不许复制第二份**。

  🔴 凭据纪律：明文只在本页的 cred ref 里活一次，弹框一关就清（@closed="forget"）。
  不进 URL、不进浏览器存储、不进日志。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">代理审核</h1>
      <p class="page-sub">
        新接入的代理默认待审，批准时签发一次性凭据；未批准期间它的每一次读写都被拒绝。
      </p>
      <p class="page-sub">
        批准 = 放行读写并签发凭据（明文只显示一次，当场转交）；吊销 = 判它不该进来。
      </p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard
        label="待审核"
        :value="pendingCount"
        unit="个"
        tone="danger"
        hint="待审期间读写全拒，批准后才会发凭据"
      />
    </div>

    <!-- 待审要一眼看见：待审的代理此刻正在被拒，这是这页唯一要盯的数 -->
    <div v-if="pendingCount > 0" class="banner banner--danger">
      {{ pendingCount }} 个代理待审核 —— 在这页批准之前，它们连不上、也写不进任何记忆。
    </div>

    <div v-if="loading && !rows.length" class="panel pad">
      <el-skeleton :rows="2" animated />
    </div>
    <div v-else-if="!rows.length" class="panel">
      <div class="empty">
        <p>
          <b>没有待审核的代理。</b>
          新代理连上来或你手动登记后会出现在这里。
        </p>
      </div>
    </div>
    <div v-else class="card-grid">
      <ItemCard v-for="a in rows" :key="a.agent_id" chip-tone="danger">
        <template #tag>待审核</template>

        <div class="cell-key">
          <span class="k" :title="a.agent_id">{{ a.agent_id }}</span>
          <button class="copy-btn" title="复制 agent_id" @click="doCopy(a.agent_id)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
              <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.7 1.7 0 0 0 4 10.6h1.4" />
            </svg>
          </button>
        </div>

        <div class="aname">
          <span class="aname-txt" :title="a.name || a.agent_id">{{ a.name || a.agent_id }}</span>
        </div>

        <!-- 审核页只有三样要看的东西：agent_id / 备注名 / 首次出现时间 -->
        <div class="kv">
          <div class="row">
            <span class="k">首次出现</span>
            <span class="v">{{ toLocal(a.first_seen_at) }}</span>
          </div>
        </div>

        <template #actions>
          <!-- 只有两件事：批准（放行 + 发凭据）/ 吊销（防误批：判它不该进来） -->
          <button class="act-btn" :disabled="busy" @click="ask('approve', a)">批准</button>
          <button class="act-btn act-btn--danger" :disabled="busy" @click="ask('revoke', a)">吊销</button>
        </template>
      </ItemCard>
    </div>

    <!-- 两个动作共用一个确认框：kind 决定标题 / 后果 / 确认串 / 按钮色（与登记页同款） -->
    <ConfirmDialog
      v-model="dlgOpen"
      :title="dlgTitle"
      :consequences="dlgConsequences"
      :expect="dlgExpect"
      :confirm-text="dlgConfirmText"
      :tone="dlgTone"
      :busy="busy"
      @confirm="doAction"
    />

    <!-- 一次性凭据：共用组件（登记页 / 审核页同一份实现）；关掉即从内存里抹掉 -->
    <CredentialDialog
      v-model="credOpen"
      :credential="cred"
      :note="credNote"
      :busy="busy"
      @closed="forget"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import CredentialDialog from '../components/CredentialDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { isStale } from '../utils/kb'

const data = ref(null)
const loading = ref(true)
const error = ref('')
const busy = ref(false)

/** 待审队列：只列 status === 'pending' 的行（已批准/已吊销去登记页）。 */
const rows = computed(() => (data.value?.items || []).filter((a) => a.status === 'pending'))
/** 🔴 待审数恒为全量口径：后端不随 q / page 变，红条上的数永远等于全库待审数。 */
const pendingCount = computed(() => data.value?.pending_count || 0)

// 后端代理列表端点不按 status 过滤（零后端改动约束）：pending 行可能落在第 2、3 页，
// 只取第一页就永远看不到待审的。所以这里把注册表翻页取全，再本地筛 pending。
// ponytail: 每页 100（后端 MAX_PAGE_SIZE）、最多翻 20 页 = 2000 行；注册表是本机
//   的 MCP 代理名单，量级很小。升级路径 = 后端加 status 参数后改回服务端分页。
const PAGE_SIZE = 100
const MAX_PAGES = 20

/** 静默重拉的失败提示：连着几轮都失败只报第一次，免得 10 秒刷一次屏。 */
let lastErr = ''

// 请求序号：翻页补拉途中用户可能已离开本页，旧响应只丢弃，不覆盖新结果
let reqSeq = 0

async function load(silent = false) {
  const seq = ++reqSeq
  if (!silent) loading.value = true
  try {
    const first = await api.listAgents({ page: 1, page_size: PAGE_SIZE })
    if (isStale(seq, reqSeq)) return
    let items = Array.isArray(first?.items) ? first.items : []
    const total = Number(first?.count) || items.length
    // 注册表超过一页就把剩下的补齐（pending 行不能被切到别的页去）
    for (let page = 2; page <= MAX_PAGES && items.length < total; page += 1) {
      const next = await api.listAgents({ page, page_size: PAGE_SIZE })
      if (isStale(seq, reqSeq)) return
      const chunk = Array.isArray(next?.items) ? next.items : []
      if (!chunk.length) break
      items = items.concat(chunk)
    }
    data.value = {
      items,
      count: total,
      pending_count: Number(first?.pending_count) || 0,
    }
    error.value = ''
    lastErr = ''
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    // 保留旧数据不清屏：一次抖动不该把正在审的队列抽走
    error.value = `读取待审队列失败：${e.message}`
    if (lastErr !== error.value) showError(error.value)
    lastErr = error.value
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

// 代理侧是「随时在动」的（子代理启动/退出都会改状态），所以队列每 10 秒自拉一次；
// 卸载时必须清定时器，否则离开这页还在偷偷打接口。
const REFRESH_MS = 10000
let timer = null
onMounted(() => {
  load()
  timer = setInterval(() => load(true), REFRESH_MS)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  timer = null
  forget()
})

async function doCopy(text) {
  const ok = await copyText(text)
  ok ? showToast(`已复制：${text}`) : showError('复制失败，请手动选中复制')
}

// ---------------------------------------------------------------------------
// 两个动作的确认态：approve / revoke 共用一个 ConfirmDialog（与登记页同款口径）
// ---------------------------------------------------------------------------

/** 一次性凭据弹框的内存态。明文只活在这里：关掉弹框就抹掉。 */
const credOpen = ref(false)
const cred = ref('')
const credNote = ref('')

function forget() {
  cred.value = ''
  credNote.value = ''
}

const dlgOpen = ref(false)
const dlgKind = ref('')
const dlgRow = ref(null)

const dlgTitle = computed(() => {
  const id = dlgRow.value?.agent_id || ''
  if (dlgKind.value === 'approve') return `批准 ${id}？`
  return `吊销 ${id}？`
})

const dlgConfirmText = computed(() => (dlgKind.value === 'approve' ? '批准' : '确认吊销'))

/** 吊销要手输 agent_id 前 8 位（与登记页逐字一致的后端闸门，不许降级）。 */
const dlgExpect = computed(() =>
  dlgKind.value === 'revoke' ? (dlgRow.value?.agent_id || '').slice(0, 8) : '',
)

const dlgTone = computed(() => (dlgKind.value === 'revoke' ? 'danger' : 'primary'))

const dlgConsequences = computed(() => {
  const row = dlgRow.value
  if (!row) return []
  const id = row.agent_id
  if (dlgKind.value === 'approve') {
    return [
      '签发一条新凭据并把这个代理置为已批准 —— 批准之后它才会被放行读写。',
      '凭据明文只在这个响应里出现一次，关掉弹框就再也拿不到了 —— 请当场转交。',
      '批准后它才会被放行读写；未批准期间它的每一次调用都被拒绝。',
    ]
  }
  return [
    `凭据哈希一并清掉：${id} 之后不会被放行，连不上也写不进任何记忆。`,
    '它此前已经写入的记忆条目不删除，只是不再被它改动。',
    '随时可以在登记页重新登记再批准。',
  ]
})

function ask(kind, row) {
  dlgKind.value = kind
  dlgRow.value = row
  dlgOpen.value = true
}

/** 确认回调：用户手输的串原样当 confirm 发给后端（口径同登记页的吊销）。 */
async function doAction(typed) {
  const row = dlgRow.value
  const kind = dlgKind.value
  if (!row) return
  dlgOpen.value = false
  busy.value = true
  try {
    if (kind === 'approve') {
      const r = await api.approveAgent(row.agent_id)
      // 明文只在这一刻落到前端内存态：弹框一关（@closed）就抹掉
      cred.value = r.credential || ''
      credNote.value = r.note || ''
      credOpen.value = true
      showToast(`已批准：${row.agent_id}`)
    } else {
      await api.revokeAgent(row.agent_id, typed)
      showToast(`已吊销：${row.agent_id}`)
    }
    // 状态 / 计数都由后端说了算，本地改了很快就和注册表对不上
    load(true)
  } catch (e) {
    showError(`${dlgConfirmText.value}失败：${e.message}`)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 名字行：名字可截断（与登记页同一份配方，scoped 不跨组件，各写一份，数值不新创） */
.aname {
  display: flex;
  gap: 8px;
  align-items: center;
  min-width: 0;
  margin-top: -4px;
}
.aname-txt {
  min-width: 0;
  overflow: hidden;
  color: var(--text-dim);
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
