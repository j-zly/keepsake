<!--
  代理治理（keepsake_agent_registry 的控制台面）。

  子代理的 MCP 连上来就会在这里登记：新接入的一律 pending —— **未批准读写真全拒**，
  批准时才签发一条**一次性凭据**（明文只回来这一次，库里只存 sha256）。

  🔴 凭据纪律（本页唯一不可让步的一条）：明文只放在 cred 这一个 ref 里，
  弹框一关就清（@closed="forget"）。不进 URL、不进浏览器存储、不进日志；
  页面里的复制只到剪贴板为止，转交由人自己走别的渠道。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">代理治理</h1>
      <p class="page-sub">
        新接入的代理默认待审，批准时签发一次性凭据；未批准与已吊销的代理读写全拒。
      </p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="接入过的代理" :value="count" unit="个" hint="按 agent_id 排序，逐条审" />
      <StatCard
        label="待审核"
        :value="pendingCount"
        unit="个"
        tone="danger"
        hint="待审期间读写全拒，批准后才会发凭据"
      />
      <StatCard label="主代理" :value="primaryId || '—'" hint="全库唯一，只有它能改常驻层" />
    </div>

    <!-- 待审要一眼看见：待审的代理此刻正在被拒，这是这页唯一要盯的数 -->
    <div v-if="pendingCount > 0" class="banner banner--danger">
      {{ pendingCount }} 个代理待审核 —— 在这页批准之前，它们连不上、也写不进任何记忆。
    </div>

    <div v-if="loading && !agents.length" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>
    <div v-else-if="!agents.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="8.5" cy="8" r="3.4" />
          <path d="M2.8 19.4a5.8 5.8 0 0 1 11.4 0" stroke-linecap="round" />
          <path d="M15.4 5.2a3.4 3.4 0 0 1 0 6.4" stroke-linecap="round" />
          <path d="M16.6 14.2a5.8 5.8 0 0 1 4.6 4" stroke-linecap="round" />
        </svg>
        <p>
          <b>还没有代理接入过</b>
          让子代理的 MCP 连上来（带上 agent_id 与名称），它就会出现在这里等你审。
        </p>
      </div>
    </div>
    <div v-else class="card-grid">
      <ItemCard v-for="a in agents" :key="a.agent_id" :chip-tone="chipTone(a)">
        <template #tag>{{ STATUS_TEXT[a.status] || a.status }}</template>
        <template #time>{{ toLocal(a.first_seen_at) }}</template>

        <div class="cell-key">
          <span class="k" :title="a.agent_id">{{ a.agent_id }}</span>
          <button class="copy-btn" title="复制 agent_id" @click="doCopy(a.agent_id)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
              <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
            </svg>
          </button>
        </div>

        <div class="aname">
          <span class="aname-txt" :title="a.name || a.agent_id">{{ a.name || a.agent_id }}</span>
          <span class="chip">{{ a.role === 'primary' ? '主代理' : '子代理' }}</span>
        </div>

        <div class="kv">
          <div class="row">
            <span class="k">最后活动</span>
            <span class="v">{{ toLocal(a.last_seen_at) }}</span>
          </div>
          <div class="row">
            <span class="k">读 / 写</span>
            <span class="v num">{{ a.reads }} / {{ a.writes }}</span>
          </div>
          <div class="row">
            <span class="k">凭据</span>
            <span class="v mono">{{ hintText(a) }}</span>
          </div>
        </div>

        <template #tags>
          <span v-if="a.exclude_from_primary" class="tag">不进主脑上下文</span>
        </template>

        <template #actions>
          <!-- pending / revoked 都走批准；已吊销的走这条等于重新签发一条新凭据 -->
          <button
            v-if="a.status !== 'approved'"
            class="act-btn"
            :disabled="busy"
            @click="ask('approve', a)"
          >
            {{ a.status === 'revoked' ? '重新批准' : '批准' }}
          </button>
          <template v-if="a.status === 'approved'">
            <!-- 主代理行不给「设为主代理」：防的就是把自己降了、还没人批 -->
            <button v-if="a.role !== 'primary'" class="act-btn" :disabled="busy" @click="ask('role', a)">
              设为主代理
            </button>
            <button class="act-btn" :disabled="busy" @click="ask('exclude', a)">
              {{ a.exclude_from_primary ? '加入主脑上下文' : '移出主脑上下文' }}
            </button>
            <button class="act-btn act-btn--danger" :disabled="busy" @click="ask('revoke', a)">吊销</button>
          </template>
        </template>
      </ItemCard>
    </div>

    <!-- 四个动作共用一个确认框：kind 决定标题 / 后果 / 确认串 / 按钮色（照抄记忆维护页那套） -->
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

    <!-- 一次性凭据：全文 + 复制 + 红字提醒；关掉即从内存里抹掉（forget），拿不回来就重批 -->
    <el-dialog
      v-model="credOpen"
      title="一次性凭据"
      width="520px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busy"
      :show-close="!busy"
      append-to-body
      @closed="forget"
    >
      <p class="warn">只显示这一次，请立即转交给代理进程 —— 关掉这个弹框就再也拿不到了。</p>
      <pre class="pre">{{ cred }}</pre>
      <div class="cred-acts">
        <el-button @click="doCopy(cred)">复制凭据</el-button>
      </div>
      <p class="tip">{{ credNote }}</p>
      <template #footer>
        <el-button :disabled="busy" @click="credOpen = false">我已转交，关掉</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

/** 状态徽标的显示词；后端给什么就显示什么（未知值也不吞，退回原串）。 */
const STATUS_TEXT = { approved: '已批准', pending: '待审核', revoked: '已吊销' }

const data = ref(null)
const loading = ref(true)
const error = ref('')
const busy = ref(false)

const agents = computed(() => data.value?.items || [])
const count = computed(() => data.value?.count ?? agents.value.length)
const pendingCount = computed(() => data.value?.pending_count || 0)
const primaryId = computed(() => agents.value.find((a) => a.role === 'primary')?.agent_id || '')

/** approved 走蓝底、pending 红底（此刻它正在被拒，要醒目）、revoked 中性灰。 */
function chipTone(a) {
  if (a.status === 'approved') return 'accent'
  if (a.status === 'pending') return 'danger'
  return ''
}

/** 凭据只回显前 8 位；后端把空串同时用作「未发放」和「已轮换/已清」，一句话说清。 */
function hintText(a) {
  return a.credential_hint ? `${a.credential_hint}…（只显示前 8 位）` : '未发放 / 已轮换不显示'
}

/** 静默重拉的失败提示：连着几轮都失败只报第一次，免得 10 秒刷一次屏。 */
let lastErr = ''

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    data.value = await api.listAgents()
    error.value = ''
    lastErr = ''
  } catch (e) {
    // 保留旧数据不清屏：一次抖动不该把正在审的列表抽走
    error.value = `读取代理列表失败：${e.message}`
    if (lastErr !== error.value) showError(error.value)
    lastErr = error.value
  } finally {
    loading.value = false
  }
}

// 代理侧是「随时在动」的（子代理启动/退出都会改计数），所以列表每 10 秒自拉一次；
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
// 四个动作的确认态：approve / revoke / role / exclude 共用一个 ConfirmDialog
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

const wasRevoked = computed(() => dlgRow.value?.status === 'revoked')
const revokePrefix = computed(() => (dlgRow.value?.agent_id || '').slice(0, 8))

const dlgTitle = computed(() => {
  const id = dlgRow.value?.agent_id || ''
  if (dlgKind.value === 'approve') return wasRevoked.value ? `重新批准 ${id}？` : `批准 ${id}？`
  if (dlgKind.value === 'revoke') return `吊销 ${id}？`
  if (dlgKind.value === 'role') return `把 ${id} 设为主代理？`
  return `${dlgRow.value?.exclude_from_primary ? '加入' : '移出'}主脑上下文？`
})

const dlgConfirmText = computed(() => {
  if (dlgKind.value === 'approve') return wasRevoked.value ? '重新批准' : '批准'
  if (dlgKind.value === 'revoke') return '确认吊销'
  if (dlgKind.value === 'role') return '设为主代理'
  return '确认修改'
})

/** 两道手输闸的口径与后端逐字一致：吊销 = 前 8 位，改角色 = 完整 agent_id。 */
const dlgExpect = computed(() => {
  if (dlgKind.value === 'revoke') return revokePrefix.value
  if (dlgKind.value === 'role') return dlgRow.value?.agent_id || ''
  return ''
})

const dlgTone = computed(() => (dlgKind.value === 'revoke' || dlgKind.value === 'role' ? 'danger' : 'primary'))

const dlgConsequences = computed(() => {
  const row = dlgRow.value
  if (!row) return []
  const id = row.agent_id
  if (dlgKind.value === 'approve') {
    return [
      '签发一条新凭据并把这个代理置为已批准：之前发的那条（若有）立即作废。',
      '凭据明文只在这个响应里出现一次，关掉弹框就再也拿不到了 —— 请当场转交。',
      '批准后它才会被放行读写；未批准期间它的每一次调用都被拒绝。',
    ]
  }
  if (dlgKind.value === 'revoke') {
    return [
      `凭据哈希一并清掉：${id} 手里那条凭据立刻作废，连不上也写不进任何记忆。`,
      '它此前已经写入的记忆条目不删除，只是不再被它改动。',
      '随时可以「重新批准」换一条新凭据回来。',
    ]
  }
  if (dlgKind.value === 'role') {
    return [
      '全库只有一个主代理：原来的主代理会自动降为子代理（可在列表里核对）。',
      '只有主代理能改常驻层；被降下去的那位之后写常驻会被拦成候选，等人审。',
      '改前会备份整份注册表（含被降级的那一行），可人工复原。',
    ]
  }
  return row.exclude_from_primary
    ? [
        '这个代理之后写入的条目重新进入主脑上下文（每轮注入与检索都能看见）。',
        '已在库里的条目不受影响 —— 这个开关只挡它之后写的东西。',
        '随时可以再关回去。',
      ]
    : [
        '这个代理之后写入的条目不进主脑上下文（每轮注入看不到、检索也过滤掉）。',
        '已在库里的条目不受影响 —— 这个开关只挡它之后写的东西。',
        '随时可以在这里改回来。',
      ]
})

function ask(kind, row) {
  dlgKind.value = kind
  dlgRow.value = row
  dlgOpen.value = true
}

/** 确认回调：用户手输的串原样当 confirm 发给后端（口径同 ops 的归档/真删）。 */
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
    } else if (kind === 'revoke') {
      await api.revokeAgent(row.agent_id, typed)
      showToast(`已吊销：${row.agent_id}`)
    } else if (kind === 'role') {
      const r = await api.setAgentRole(row.agent_id, 'primary', typed)
      showToast(
        r.demoted?.length
          ? `已设为主代理：${row.agent_id}（原主 ${r.demoted.join('、')} 已降为子代理）`
          : `已设为主代理：${row.agent_id}`,
      )
    } else {
      await api.setAgentExclude(row.agent_id, !row.exclude_from_primary)
      showToast(`${row.exclude_from_primary ? '已加入' : '已移出'}主脑上下文：${row.agent_id}`)
    }
    // 状态 / 角色 / 计数都由后端说了算，本地改了很快就和注册表对不上
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
/* 第二行：名字（可截断）+ 角色徽标，名字太长时徽标不许被挤掉 */
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
.kv .row .v.mono {
  font-family: var(--mono);
  font-size: 12px;
}
/* 一次性凭据弹框：红色提醒 + 全文 + 复制 */
.warn {
  margin: 0 0 12px;
  color: var(--danger);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.65;
}
.cred-acts {
  margin-top: 12px;
}
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
</style>
