<!--
  代理登记（keepsake_agent_registry 的控制台面 · 业务页）。

  2026-10-20 拆页：待审队列从本页整段搬去 /agents-review（代理审核）。
  本页只管**已批准代理的日常维护**（编辑 ID 与备注名 / 设角色 / 上下文过滤 / 吊销 / 重新批准 / 换发凭据）
  +「登记新代理」；pending 的行不在本页出现（去审核页看）。
  两页共用一次性凭据弹框（components/CredentialDialog.vue）—— **一份实现，不复制**。

  🔴 凭据纪律（不可让步的一条）：明文只放在 cred 这一个 ref 里，弹框一关就清
  （<CredentialDialog @closed="forget" />）。不进 URL、不进浏览器存储、不进日志；
  页面里的复制只到剪贴板为止，转交由人自己走别的渠道。

  🔴 主代理保护：primary 行**不给任何治理动作按钮**（approve/revoke/role/exclude 全无），
  只留一句「不可吊销 / 不可排除」+ 一个「编辑」（能改备注名，id 被后端锁死）。
  换主的唯一路径是给别的行点「设为主代理」—— 那次会自动把旧主降级。
  后端同口径 409，绕开 UI 打 API 也一样被挡。

  ⚠️ 唯一的例外是「重新生成凭据」：换发就是**再 approve 一次**（approve 本就是
  换发语义 —— 旧的那条立即作废），主代理行同样给。主脑走本机 bootstrap，
  不吃这把凭据，泄露时它也得能自己换一把。

  ponytail: 后端 GET /api/v1/agents 不按 status 过滤（零后端改动约束），所以本页的
  分页与计数是**全状态口径** —— pending 行占着页位却不渲染（它去审核页）。
  升级路径 = 后端加一个 status 参数，这里改回服务端过滤，前端这层拆组即可删掉。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">代理登记</h1>
      <p class="page-sub">
        已批准代理的日常维护台：改名、设角色、上下文过滤、吊销都在这里。
      </p>
      <p class="page-sub">
        新接入的代理默认待审 —— 待审那一摞在「代理审核」页；未批准与已吊销的代理读写全拒。
      </p>
      <p class="page-sub">
        三级语义别混：吊销 = 停权限、留一条吊销记录；停凭据 = 只废那把钥匙（代理照旧在册）；
        删除 = 整行抹掉（删前会备份一份，可人工复原）。
      </p>
      <!-- 页头操作区：登记 = 「还没连上就先占个位」的那一步（批准在审核页） -->
      <div class="head-acts">
        <el-button type="primary" @click="openRegister">登记新代理</el-button>
      </div>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="接入过的代理" :value="count" unit="个" hint="按 agent_id 排序；待审的在「代理审核」页" />
      <StatCard label="主代理" :value="primaryId || '—'" hint="全库唯一，只有它能改常驻层" />
    </div>

    <!-- 搜索 + 分页：与常驻层/知识库同一套 filter-panel 口径（回车查、清空回全量、seq 防串） -->
    <div class="filter-panel">
      <div class="filter-title">代理列表</div>
      <p class="filter-desc">
        按 agent_id 或名称搜索；清空回全量。🔴 主代理行没有治理动作按钮（只留一个「编辑」改备注名）
        —— 吊销它等于把主脑降级，要换主请用别行的「设为主代理」。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="搜索 agent_id 或名称（清空回全量）"
          clearable
          @keyup.enter="search"
          @clear="onClear"
        >
          <template #prefix>
            <span class="search-ico">
              <svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor"
                stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="7" cy="7" r="4.3" />
                <path d="m10.3 10.3 3.4 3.4" />
              </svg>
            </span>
          </template>
        </el-input>
        <el-button type="primary" :loading="loading" @click="search">查询</el-button>
      </div>
      <div class="filter-result">
        <!-- count 是后端口径（全状态）：待审的行不在这页渲染，见页头说明 -->
        <span>共 {{ count }} 个匹配 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <button class="soft-btn" :disabled="!dirty" @click="onClear">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="load">刷新</button>
        </div>
      </div>
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
        <!-- 空态三分支：筛选无结果 vs 全库为空，说的不是同一件事 -->
        <p v-if="dirty">
          <b>没有匹配到这个词</b>换个更短的 agent_id 或名称试试，或点「清除筛选」回全量列表。
        </p>
        <p v-else>
          <b>还没有代理接入过</b>
          让子代理的 MCP 连上来（带上 agent_id 与名称），它就会出现在这里等你审。
        </p>
      </div>
    </div>
    <!-- 这一页有行、但一行都不是已批准/已吊销（整页都是待审）：说清去哪看 -->
    <div v-else-if="!groups.length" class="panel">
      <div class="empty">
        <p>
          <b>这一页没有已批准或已吊销的代理</b>
          待审的代理都在「代理审核」页 —— 未批准之前它们读写全拒。
        </p>
      </div>
    </div>
    <!-- 两个分组：已批准在上、已吊销在下；待审的行不在本页（去审核页） -->
    <template v-else>
      <template v-for="grp in groups" :key="grp.key">
        <div class="group-head">
          <span class="group-title">{{ grp.title }}</span>
          <span class="group-count num">{{ grp.rows.length }} 个</span>
        </div>
        <div class="card-grid">
          <ItemCard v-for="a in grp.rows" :key="a.agent_id" :chip-tone="chipTone(a)">
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
              <!--
                行内只留复制钮（复制 id 仍是最常用的操作）；改 id 与改备注名
                一起收进卡片操作区的「编辑」弹框 —— 一张卡一个编辑入口。
              -->
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
              <!--
                🔴 主代理行**只留一句话**，四个动作一个都不给（approve/revoke/role/exclude）：
                吊销它 = 写闸 fail-closed 把主脑降成 sub，常驻写入全降级、候选链瘫痪；
                排除它更没有意义（过滤是给别人的看的，它自己每轮就在上下文里）。
                换主的唯一安全路径是「先把另一个设为主代理」，那次会自动把它降级。
                后端同口径 409（`_need_not_primary`）—— 这里只是不让人看见那个钮。
              -->
              <span v-if="a.role === 'primary'" class="act-note">主代理 · 不可吊销 / 不可排除</span>
              <template v-else>
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
                  <!-- 内层这道 v-if 是本地护栏：外层已经按 role 分流，这里再钉一次 -->
                  <button v-if="a.role !== 'primary'" class="act-btn" :disabled="busy" @click="ask('role', a)">
                    设为主代理
                  </button>
                  <button class="act-btn" :disabled="busy" @click="ask('exclude', a)">
                    {{ a.exclude_from_primary ? '加入主脑上下文' : '移出主脑上下文' }}
                  </button>
                  <button class="act-btn act-btn--danger" :disabled="busy" @click="ask('revoke', a)">吊销</button>
                  <!--
                    停凭据：**只废那把钥匙**（凭据哈希与提示清掉；状态 / 角色 / 备注一个都不动）——
                    与「吊销」的分界就在状态上：吊销 = 停权限并留一条吊销记录，
                    停凭据 = 代理照旧在册、只是再也连不上（凭据泄露时先止血用这个）。
                    🔴 门只开在「有凭据提示」的行上（credential_hint 非空）：没凭据的行没有可停的东西。
                  -->
                  <button
                    v-if="a.credential_hint"
                    class="act-btn"
                    :disabled="busy"
                    @click="ask('clearcred', a)"
                  >
                    停凭据
                  </button>
                </template>
                <!--
                  删除：**整行抹掉**（删前会备份一份，可人工复原）。
                  🔴 只给**已吊销**的行 —— approved 的行不给这个钮：先吊销（停权限）
                  再删，两步隔离「心误删活代理」（那个正在跑的）。
                -->
                <button
                  v-if="a.status === 'revoked'"
                  class="act-btn act-btn--danger"
                  :disabled="busy"
                  @click="ask('delete', a)"
                >
                  删除
                </button>
              </template>
              <!--
                编辑：一张卡**一个**编辑入口（代理 ID + 备注名同框）。
                放在分流链**外面** —— primary 行不给的是「危险治理动作」，
                改备注名它照样要能改（后端同口径：primary 只锁 id）。
              -->
              <button class="act-btn" :disabled="busy" @click="openEdit(a)">编辑</button>

              <!--
                重新生成凭据：**换发**（再 approve 一次 —— 旧的那条立即作废，新的一条顶上来）。
                放在 primary / 非 primary 的分流链**外面** —— 换发对主代理无副作用
                （主脑走本机 bootstrap，不吃这把凭据），泄露场景下主代理同样得能换。
                门只开在「已批准」行上：待审的去审核页，已吊销的走「重新批准」。
                🔴 不是危险色那一档：吊销 / 排除 / 改角色会毁掉东西，这里只是**换一把钥匙**
                （代理照旧能读写），所以走普通 act-btn。
              -->
              <button
                v-if="a.status === 'approved'"
                class="act-btn"
                :disabled="busy"
                @click="ask('rotate', a)"
              >
                重新生成凭据
              </button>

              <!--
                私信开关（dm_enabled）：关掉之后，别人往它信箱里投的消息一律拒投
                （投递闸门在后端，与 MCP 的 message_send 同一条规矩）。
                🔴 放在 primary 的分流链**外面** —— 关了自己也收不到私信，合法
                （用户明确要过）；它是**投递偏好**，不是治理动作（不碰状态 / 角色 / 凭据）。
                失败回滚：乐观值丢掉即回到服务器口径，并报红条（不许留下「以为改成了」）。
              -->
              <span class="dm-toggle">
                <span class="dm-label">私信</span>
                <el-switch
                  :model-value="dmOn(a)"
                  size="small"
                  :loading="dmBusy === a.agent_id"
                  :title="dmOn(a) ? '已开启：接收私信' : '已关闭：别人投不进来'"
                  @change="toggleDm(a, $event)"
                />
              </span>

              <!--
                记忆共享开关（share_memory）：打开之后，这个代理名下的记忆进入
                **别的已批准代理**的检索可见范围（关着就只有它自己 + 主脑看得见）。
                🔴 与私信开关**同一档**：只改**读可见性**，不是治理动作
                （不碰状态 / 角色 / 凭据 / 记忆归属）—— 所以同样放在 primary
                的分流链**外面**（主代理也可以共享自己的记忆，合法）。
                失败回滚：乐观值丢掉即回到服务器口径，并报红条。
              -->
              <span class="dm-toggle">
                <span class="dm-label">共享记忆</span>
                <el-switch
                  :model-value="shareOn(a)"
                  size="small"
                  :loading="shareBusy === a.agent_id"
                  :title="shareOn(a) ? '已共享：别的代理能检索到它的记忆' : '未共享：只有它自己和主脑看得见'"
                  @change="toggleShare(a, $event)"
                />
              </span>
            </template>
          </ItemCard>
        </div>
      </template>
    </template>

    <PagerBar
      :total="count"
      :page="page"
      :page-size="pageSize"
      @update:page="onPage"
      @update:page-size="onSize"
    />

    <!-- 四个动作共用一个确认框（换发 rotate 也走这一个）：kind 决定标题 / 后果 / 确认串 / 按钮色 -->
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

    <!-- 一次性凭据：共用组件（登记页 / 审核页同一份实现，不许复制第二份）；关掉即从内存里抹掉 -->
    <CredentialDialog
      v-model="credOpen"
      :credential="cred"
      :note="credNote"
      :busy="busy"
      @closed="forget"
    />

    <!-- 登记新代理：手工占个位（pending）；勾「立即批准」当场发一次性凭据 -->
    <el-dialog
      v-model="regOpen"
      title="登记新代理"
      width="480px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busy"
      :show-close="!busy"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="agent_id（必填）">
          <el-input
            v-model="regId"
            placeholder="如 worker-2"
            autocomplete="off"
            spellcheck="false"
            @keyup.enter="doRegister"
          />
        </el-form-item>
        <el-form-item label="备注名（可选）">
          <el-input
            v-model="regName"
            placeholder="给人看的名字；留空则与 agent_id 同名"
            @keyup.enter="doRegister"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="regApproveNow">立即批准并签发一次性凭据</el-checkbox>
        </el-form-item>
      </el-form>
      <p class="tip">
        agent_id 只允许小写字母、数字与连字符，且必须以字母或数字开头、总长 2~40 位。
        登记只是占位（pending）—— 不批准的代理读写全拒；已存在的同名代理会被直接拒绝，绝不覆盖。
      </p>
      <template #footer>
        <el-button :disabled="busy" @click="regOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busy"
          :disabled="!regId.trim()"
          @click="doRegister"
        >
          登记
        </el-button>
      </template>
    </el-dialog>

    <!--
      编辑代理：**代理 ID + 备注名同框**（一张卡一个编辑入口，两个铅笔合并到这儿）。
      🔴 改 ID 是换主键（凭据与权限不变，历史记忆的归属跟着一起搬）；
      改备注名只动人看的标签（状态 / 角色 / 已发的凭据一律不碰）。
      primary 行的 ID 输入框锁死 —— 主脑的引导配置绑在这个 id 上（后端同口径 400）。
    -->
    <el-dialog
      v-model="editOpen"
      title="编辑代理"
      width="460px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busy"
      :show-close="!busy"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="代理 ID">
          <el-input
            v-model="editId"
            :disabled="editPrimary"
            placeholder="如 worker-2"
            autocomplete="off"
            spellcheck="false"
            @keyup.enter.prevent="doEdit"
          />
          <p v-if="editPrimary" class="tip">主代理 ID 不可改（备注名照常可以改）。</p>
        </el-form-item>
        <el-form-item label="备注名（必填）">
          <el-input
            v-model="editName"
            placeholder="给人看的名字"
            autocomplete="off"
            spellcheck="false"
            @keyup.enter.prevent="doEdit"
          />
        </el-form-item>
      </el-form>
      <p class="tip">
        改 ID：凭据与权限不变 —— 已经发出去的凭据继续有效（客户端不用重配）；
        历史记忆的归属会一起搬过去（它写过的记忆仍归它）。
        agent_id 只允许小写字母、数字与连字符，且必须以字母或数字开头、总长 2~40 位。
        <br />
        改备注名：状态、角色与已发的凭据都不动（改名不会作废它正在用的凭据）。
      </p>
      <template #footer>
        <el-button :disabled="busy" @click="editOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busy"
          :disabled="!editId.trim()"
          @click="doEdit"
        >
          保存
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import CredentialDialog from '../components/CredentialDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import http, { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { isStale } from '../utils/kb'

/** 状态徽标的显示词；后端给什么就显示什么（未知值也不吞，退回原串）。 */
const STATUS_TEXT = { approved: '已批准', pending: '待审核', revoked: '已吊销' }

const data = ref(null)
const loading = ref(true)
const error = ref('')
const busy = ref(false)

/** 私信开关的乐观值（agent_id → 开/关）：只活到下一次成功刷新；失败即丢掉（回滚）。 */
const dmState = ref({})
/** 正在提交的那一行（同一时刻只允许一个，避免连点连发）。 */
const dmBusy = ref('')
/** 记忆共享开关的乐观值（agent_id → 开/关）：与私信开关同款（乐观 + 失败回滚）。 */
const shareState = ref({})
/** 正在提交共享开关的那一行。 */
const shareBusy = ref('')

// 搜索 + 服务端分页（口径照抄 CoreView：回车查、清空回全量、seq 防串）
const q = ref('')
const page = ref(1)
const pageSize = ref(20)

const agents = computed(() => data.value?.items || [])
/** `count` 是**过滤后**的总数（后端口径），PagerBar 拿它算页数，前端不另算。 */
const count = computed(() => data.value?.count ?? agents.value.length)
const primaryId = computed(() => agents.value.find((a) => a.role === 'primary')?.agent_id || '')
/** 本页只渲染已批准 + 已吊销两摞：pending 的行去「代理审核」页。 */
const approvedRows = computed(() => agents.value.filter((a) => a.status === 'approved'))
const revokedRows = computed(() => agents.value.filter((a) => a.status === 'revoked'))
/** 页面要渲染的分组（空组不占地方；顺序 = 已批准在上、已吊销在下）。 */
const groups = computed(() =>
  [
    { key: 'approved', title: '已批准', rows: approvedRows.value },
    { key: 'revoked', title: '已吊销', rows: revokedRows.value },
  ].filter((g) => g.rows.length),
)
/** 有搜索词在生效吗：空态分支与「清除筛选」按钮都看它。 */
const dirty = computed(() => !!(q.value || '').trim())

/** approved 走蓝底、revoked 中性灰（pending 红底那一摞在审核页）。 */
function chipTone(a) {
  if (a.status === 'approved') return 'accent'
  return ''
}

/** 凭据只回显前 8 位；后端把空串同时用作「未发放」和「已轮换/已清」，一句话说清。 */
function hintText(a) {
  return a.credential_hint ? `${a.credential_hint}…（只显示前 8 位）` : '未发放 / 已轮换不显示'
}

/** 静默重拉的失败提示：连着几轮都失败只报第一次，免得 10 秒刷一次屏。 */
let lastErr = ''

// 请求序号：连续改词/翻页时旧响应只丢弃，不覆盖新结果（照抄 CoreView 的收口口径）
let reqSeq = 0

async function load(silent = false) {
  const seq = ++reqSeq
  if (!silent) loading.value = true
  try {
    // q / page / page_size 全透传：过滤与分页在服务端做，匹配语义也在服务端
    const list = await api.listAgents({
      q: (q.value || '').trim() || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    if (isStale(seq, reqSeq)) return
    data.value = list
    error.value = ''
    lastErr = ''
    // 服务器口径为准：没有正在提交的行时，乐观值一律丢掉（页面回到真实状态）
    if (!dmBusy.value) dmState.value = {}
    if (!shareBusy.value) shareState.value = {}
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    // 保留旧数据不清屏：一次抖动不该把正在审的列表抽走
    error.value = `读取代理列表失败：${e.message}`
    if (lastErr !== error.value) showError(error.value)
    lastErr = error.value
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

/** 回车 / 「查询」：改词一律回第 1 页（在第 5 页搜词却停在第 5 页 = 空列表）。 */
function search() {
  page.value = 1
  load()
}

/** 清空 ✕ / 「清除筛选」：无条件回全量（不是空态），与 CoreView/KbView 同一口径。 */
function onClear() {
  q.value = ''
  search()
}

function onPage(p) {
  page.value = p
  load()
}

function onSize(s) {
  pageSize.value = s
  page.value = 1
  load()
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
// 私信开关（dm_enabled）：投递偏好，不是治理动作
// ---------------------------------------------------------------------------

/** 当前开关状态：乐观值优先；没有乐观值就看服务器给的（缺字段按**开** —— 老数据兼容）。 */
function dmOn(a) {
  const s = dmState.value
  if (a.agent_id in s) return s[a.agent_id]
  return a.dm_enabled !== false
}

// ---------------------------------------------------------------------------
// 记忆共享开关（share_memory）：只改读可见性，不是治理动作
// ---------------------------------------------------------------------------

/** 当前共享状态：乐观值优先；没有乐观值就看服务器给的（缺字段按**关** —— 老数据兼容）。 */
function shareOn(a) {
  const s = shareState.value
  if (a.agent_id in s) return s[a.agent_id]
  return a.share_memory === true
}

/**
 * 切换记忆共享开关：乐观更新 → 打端点 → 失败回滚（丢掉乐观值）+ 红条。
 *
 * 🔴 失败**必须**回滚：不回滚的话，界面显示「已共享」而服务端根本没改 ——
 * 那正是这套治理要拔掉的「以为改成了」。
 */
async function toggleShare(a, val) {
  const id = a.agent_id
  shareState.value = { ...shareState.value, [id]: !!val }
  shareBusy.value = id
  try {
    await api.setAgentShare(id, !!val)
    showToast(`${id}：记忆已${val ? '共享' : '收回'}`)
    // 状态以服务器为准（本地改了很快就和注册表对不上）
    await load(true)
  } catch (e) {
    const next = { ...shareState.value }
    delete next[id]
    shareState.value = next
    error.value = `记忆共享开关切换失败：${e.message}`
    showError(error.value)
  } finally {
    shareBusy.value = ''
  }
}

/**
 * 切换私信开关：乐观更新 → 打端点 → 失败回滚（丢掉乐观值）+ 红条。
 *
 * 🔴 失败**必须**回滚：不回滚的话，界面显示「已关」而服务端根本没改 ——
 * 那正是这套治理要拔掉的「以为改成了」。
 */
async function toggleDm(a, val) {
  const id = a.agent_id
  dmState.value = { ...dmState.value, [id]: !!val }
  dmBusy.value = id
  try {
    await api.setAgentDm(id, !!val)
    showToast(`${id}：私信已${val ? '开启' : '关闭'}`)
    // 状态以服务器为准（本地改了很快就和注册表对不上）
    await load(true)
  } catch (e) {
    const next = { ...dmState.value }
    delete next[id]
    dmState.value = next
    error.value = `私信开关切换失败：${e.message}`
    showError(error.value)
  } finally {
    dmBusy.value = ''
  }
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

/**
 * 把后端回的一次性凭据摆进共用弹框（两页共用 CredentialDialog，这里是本页**唯一**入口）。
 * 没有明文就**不开框**并说红条 —— 弹一个空框 = 让人以为凭据已经换到手。
 * 明文只在这一刻落到前端内存态：弹框一关（@closed）就抹掉。
 */
function showCred(r) {
  if (!r?.credential) {
    showError('后端没有返回新凭据，操作未生效，请重试')
    return false
  }
  cred.value = r.credential
  credNote.value = r.note || ''
  credOpen.value = true
  return true
}

// ---------------------------------------------------------------------------
// 登记新代理 / 改名：两条新的治理动作（后端 /api/v1/agents 与 .../rename）
// ---------------------------------------------------------------------------

// 登记：agent_id + 备注名 + 「立即批准并签发一次性凭据」
const regOpen = ref(false)
const regId = ref('')
const regName = ref('')
const regApproveNow = ref(false)

function openRegister() {
  regId.value = ''
  regName.value = ''
  regApproveNow.value = false
  regOpen.value = true
}

async function doRegister() {
  const agentId = (regId.value || '').trim()
  if (!agentId) return
  busy.value = true
  try {
    const r = await http.post('/api/v1/agents', {
      agent_id: agentId,
      name: (regName.value || '').trim(),
      confirm: true,
      approve_now: regApproveNow.value,
    }).then((res) => res.data)
    regOpen.value = false
    showToast(`已登记：${r.agent_id}（${r.status === 'approved' ? '已批准' : '待审核'}）`)
    // 🔴 凭据明文只在这一刻落到前端内存态：**复用「批准」那一个一次性弹框**
    // （同一份 cred / credNote / @closed=forget），不另写一套显示逻辑。
    if (r.credential) showCred(r)
    // 状态 / 角色 / 计数都由后端说了算，本地改了很快就和注册表对不上
    load(true)
  } catch (e) {
    showError(`登记失败：${e.message}`)
  } finally {
    busy.value = false
  }
}

// ---------------------------------------------------------------------------
// 编辑：代理 ID + 备注名**同框**（一张卡一个编辑入口）
// ---------------------------------------------------------------------------

const editOpen = ref(false)
const editRow = ref(null)
const editId = ref('')
const editName = ref('')
/** 主代理不能改 id（后端同口径 400）：输入框直接锁死，备注名照改。 */
const editPrimary = computed(() => editRow.value?.role === 'primary')

function openEdit(row) {
  editRow.value = row
  editId.value = row?.agent_id || ''
  editName.value = row?.name || ''
  editOpen.value = true
}

async function doEdit() {
  const row = editRow.value
  if (!row) return
  const oldId = row.agent_id
  const newId = (editId.value || '').trim()
  const oldName = (row.name || '').trim()
  const newName = (editName.value || '').trim()
  const idChanged = !editPrimary.value && newId !== oldId
  const nameChanged = newName !== oldName
  // 🔴 备注名非空护栏（原改名弹框有、合并时不许丢）：后端 bad_name 400 会拦，
  // 但若 ID 侧先成功生效，空备注名报错会落成「改了一半」的 warning toast —— 前端先拦。
  if (nameChanged && !newName) {
    showError('备注名不能为空（留空就分不清是哪一个代理了）')
    return
  }
  // 什么都没改：关窗，一个请求都不发
  if (!idChanged && !nameChanged) {
    editOpen.value = false
    return
  }
  busy.value = true
  const done = []   // 已经生效的部分（toast 按实际发生的拼）
  const notes = []  // 后端的话（同名提醒 / 归属没搬动）
  let targetId = oldId
  try {
    if (idChanged) {
      let r
      try {
        r = await http
          .post(`/api/v1/agents/${encodeURIComponent(oldId)}/set_id`, {
            new_id: newId,
            confirm: true,
          })
          .then((res) => res.data)
      } catch (e) {
        // 🔴 set_id 失败（409 已被占用 / 400 形态不合规）：红条透出后端 message 并**中止** ——
        // 旧 id 这时还在，接着 rename 只会改了个名字却把 id 留在半路。
        showError(`改 ID 失败：${e.message}`)
        return
      }
      // 换 id **已生效**（不可回滚）：新值即当前主键，后面的 rename 必须打新 id
      targetId = r.agent_id || newId
      done.push(`ID 已改为 ${targetId}（搬动 ${r.moved_memories ?? 0} 条记忆）`)
      if (r.note) notes.push(r.note)
    }
    if (nameChanged) {
      let r
      try {
        r = await http
          .post(`/api/v1/agents/${encodeURIComponent(targetId)}/rename`, {
            name: newName,
            confirm: true,
          })
          .then((res) => res.data)
      } catch (e) {
        if (idChanged) {
          // ID 已经改了 —— 说清「改了一半」，别让人以为全没生效
          editOpen.value = false
          showToast(`已更新：${done.join('，')}；但备注名没改：${e.message}`, 'warning')
          load(true)
        } else {
          showError(`改名失败：${e.message}`)
        }
        return
      }
      done.push('备注名已保存')
      // 🔴 同名提醒**原样透出**（后端只在真撞名时给 note）—— 备注名不拦，
      // 但列表里两个一样的名字要看得见（唯一性反馈要在页面上看得见）。
      if (r.note) notes.push(r.note)
    }
    editOpen.value = false
    showToast(`已更新：${done.join('，')}${notes.length ? ` · ${notes.join(' ')}` : ''}`)
    // 状态 / 角色 / 计数都由后端说了算，本地改了很快就和注册表对不上
    load(true)
  } finally {
    busy.value = false
  }
}

const dlgOpen = ref(false)
const dlgKind = ref('')
const dlgRow = ref(null)

const wasRevoked = computed(() => dlgRow.value?.status === 'revoked')
/** 手输闸的串：agent_id 前 8 位（吊销与换发共用这一道 —— 都是作废旧凭据）。 */
const revokePrefix = computed(() => (dlgRow.value?.agent_id || '').slice(0, 8))

const dlgTitle = computed(() => {
  const id = dlgRow.value?.agent_id || ''
  if (dlgKind.value === 'approve') return wasRevoked.value ? `重新批准 ${id}？` : `批准 ${id}？`
  if (dlgKind.value === 'rotate') return `重新生成 ${id} 的凭据？`
  if (dlgKind.value === 'revoke') return `吊销 ${id}？`
  if (dlgKind.value === 'role') return `把 ${id} 设为主代理？`
  if (dlgKind.value === 'clearcred') return `停掉 ${id} 的凭据？`
  if (dlgKind.value === 'delete') return `删除 ${id}？`
  return `${dlgRow.value?.exclude_from_primary ? '加入' : '移出'}主脑上下文？`
})

const dlgConfirmText = computed(() => {
  if (dlgKind.value === 'approve') return wasRevoked.value ? '重新批准' : '批准'
  if (dlgKind.value === 'rotate') return '重新生成凭据'
  if (dlgKind.value === 'revoke') return '确认吊销'
  if (dlgKind.value === 'role') return '设为主代理'
  if (dlgKind.value === 'clearcred') return '停凭据'
  if (dlgKind.value === 'delete') return '确认删除'
  return '确认修改'
})

/** 手输闸的口径：吊销 / 换发 / 停凭据 = 前 8 位，改角色 / 删除 = 完整 agent_id。 */
const dlgExpect = computed(() => {
  if (dlgKind.value === 'revoke') return revokePrefix.value
  if (dlgKind.value === 'rotate') return revokePrefix.value
  if (dlgKind.value === 'clearcred') return revokePrefix.value
  if (dlgKind.value === 'role') return dlgRow.value?.agent_id || ''
  if (dlgKind.value === 'delete') return dlgRow.value?.agent_id || ''
  return ''
})

/** 成功后的提示语：换发与批准各说各的（dlgRow 在这一刻仍指当前那一行）。 */
const dlgDoneText = computed(() => {
  const id = dlgRow.value?.agent_id || ''
  return dlgKind.value === 'rotate' ? `已重新生成凭据：${id}` : `已批准：${id}`
})

const dlgTone = computed(() =>
  dlgKind.value === 'revoke' || dlgKind.value === 'role' || dlgKind.value === 'delete'
    ? 'danger'
    : 'primary',
)

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
  if (dlgKind.value === 'rotate') {
    return [
      '旧凭据立即作废：使用该凭据的客户端最迟 60 秒内被拒（哈希一换即失配）。',
      '代理状态与记忆归属不变 —— 还是已批准、角色照旧、它写过的记忆照旧归它。',
      '新凭据明文只在这个响应里出现一次，关掉弹框就再也拿不到了 —— 请当场转交。',
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
  if (dlgKind.value === 'clearcred') {
    return [
      `凭据哈希与提示一并清掉：${id} 手里那条凭据立刻作废，连不上也写不进任何记忆。`,
      '代理行留在册，状态 / 角色 / 备注一个都不动（还是「已批准」，只是没钥匙了）。',
      '要恢复：重新生成凭据（换一把新的）即可 —— 这一步不吊销、不改权限。',
    ]
  }
  if (dlgKind.value === 'delete') {
    return [
      '整行抹掉：这个代理从注册表里消失（列表里也查不到了）。',
      '它此前写入的记忆条目不删除 —— 只删这一行。',
      '删前会备份这一行（备份目录里那份 JSON 就是原行），可人工复原。',
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
    if (kind === 'approve' || kind === 'rotate') {
      // 换发与批准是**同一个端点**（approve 本就是换发语义：旧的立即作废）
      const r = await api.approveAgent(row.agent_id)
      // 明文只在这一刻落到内存态（关框即抹）；没回明文就没有成功可言（红条已报）
      if (showCred(r)) showToast(dlgDoneText.value)
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
    } else if (kind === 'exclude') {
      await api.setAgentExclude(row.agent_id, !row.exclude_from_primary)
      showToast(`${row.exclude_from_primary ? '已加入' : '已移出'}主脑上下文：${row.agent_id}`)
    } else if (kind === 'clearcred') {
      // 🔴 停凭据 = 只废那把钥匙：confirm 是 true（后端那侧才敢承诺状态不动）
      const r = await http
        .post(`/api/v1/agents/${encodeURIComponent(row.agent_id)}/revoke_credential`, { confirm: true })
        .then((res) => res.data)
      if (r?.ok) showToast(`已停凭据：${row.agent_id}（代理行仍在册，状态没动）`)
      else showToast(r?.note || '这个代理没有凭据可停', 'warning')
    } else if (kind === 'delete') {
      // 🔴 整行抹掉：确认串 = 完整 agent_id（比吊销高一档），后端 primary 拒删
      await http
        .delete(`/api/v1/agents/${encodeURIComponent(row.agent_id)}`, { data: { confirm: typed } })
        .then((res) => res.data)
      showToast(`已删除：${row.agent_id}（备份已落盘，可人工复原）`)
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
/* 页头操作区：按钮靠右一行（窄屏自动换行），不放进下面那块筛选面板里 */
.head-acts {
  display: flex;
  justify-content: flex-end;
  margin-top: 12px;
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
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
/* 分组标题：已批准 / 已吊销 两摞各带一行小标题（空组不渲染） */
.group-head {
  display: flex;
  gap: 8px;
  align-items: baseline;
  margin: 18px 0 10px;
}
.group-title {
  color: var(--text);
  font-size: 14px;
  font-weight: 600;
}
.group-count {
  color: var(--text-faint);
  font-size: 12px;
}
/* 主代理行的那句说明：不是按钮，是「这里本来该有按钮、但按规矩不给」的人话 */
.act-note {
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
/* 私信开关：文字 + 开关并排（投递偏好，不是危险动作 —— 不用危险色那一档） */
.dm-toggle {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  flex: none;
  font-size: 12px;
  white-space: nowrap;
}
.dm-label {
  color: var(--text-dim);
}
</style>
