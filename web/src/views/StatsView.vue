<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">看板</h1>
      <p class="page-sub">三个域的存量与预算，加上定时任务。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <!-- 待审核卡：置顶，作为「有东西要处理」的强提示。点击跳 /review。
         0 时显示无待审（绿色 ok），不是把卡藏起来 —— 隐藏了用户找不到入口。
         失败时降级为「-」，跟其它卡同款处理，不让一行失败拖红整张看板。 -->
    <div class="stat-grid stat-grid--review">
      <button type="button" class="review-card-btn"
              :class="{ 'review-card-btn--zero': reviewTotal === 0 }"
              @click="gotoReview"
              :aria-label="reviewTotal === 0 ? '无待审，点击进入记忆审核' : `待审 ${reviewTotal} 条，点击进入记忆审核`">
        <div class="review-card">
          <div class="review-card-label">
            待审核
            <span class="review-card-hint">点击进入记忆审核 ›</span>
          </div>
          <div class="review-card-value num" :class="reviewTotal > 0 ? 'tone-danger' : 'tone-ok'">
            {{ reviewTotal === 0 && !reviewError ? '无待审' : (reviewError ? '-' : reviewTotal) }}
            <span v-if="reviewTotal > 0 && !reviewError" class="unit">条</span>
          </div>
          <div class="review-card-foot">
            <span class="review-card-pill"
                  :class="reviewTotal > 0 ? 'tone-danger-pill' : 'tone-ok-pill'">
              晋升 {{ reviewCounts.promote_candidates || 0 }} · 退居 {{ reviewCounts.demote_candidates || 0 }} · 知识库 {{ reviewCounts.kb_pending || 0 }}
            </span>
            <span v-if="reviewError" class="review-card-err">{{ reviewError }}</span>
          </div>
        </div>
      </button>
    </div>

    <div class="stat-grid">
      <StatCard label="常驻层条数" :value="data?.core?.count ?? '-'" unit="条" :hint="attributedHint" />
      <StatCard label="常驻层字符" :value="data?.core?.chars ?? '-'" unit="字符" />
      <StatCard
        label="记忆条目"
        :value="data?.retrieval?.count ?? '-'"
        unit="条"
        :hint="data?.retrieval?.truncated ? '已触扫描上限（5000），非全量' : 'keyset 扫描'"
      />
    </div>

    <!-- 常驻层按代理分列：**横向条形图**（每代理占用 vs 各自预算）。
         🔴 代理一多，文字卡堆不动（每张卡一行、十几张就滚过去了）⇒ 改成图：
         一眼看出「谁快满了」，悬停看字符数与百分比。
         口径写在下面的说明行里 —— 「谁的预算」不许让人猜。 -->
    <h3 class="section-title">常驻层 · 按代理分列</h3>
    <div class="panel pad">
      <AgentUsageBars v-if="coreAgentCards.length" :rows="coreAgentCards" :height="barsHeight" />
      <div v-else class="empty">
        <b>没有可分列的代理数据</b>
        <span>每个代理按自己生效的注入预算分列（core_budget_chars ?? 全局）。</span>
      </div>
    </div>
    <p class="src">
      分列口径：条目 <code>agent:&lt;id&gt;</code> 归属（与可见性同一把尺子）；
      预算 = 该代理自己的注入预算，没单设就用全局 {{ globalBudget }} 字符<template v-if="unattributedCount">；
      另有 {{ unattributedCount }} 条无归属常驻（{{ unattributedChars }} 字符）不计入分列</template>。
      占用 ≥90% 走告警色，≥100% 走危险色（与卡片口径同一把尺子）。
    </p>
    <div v-if="perAgentError" class="banner banner--danger">{{ perAgentError }}</div>

    <!-- 水位与趋势：环形仪表两块（总预算水位 / 待办完成率）+ 近 7 天增量折线。
         🔴 数字**全部来自后端**（前端不自己算比例）：预算水位 = tiering 给的 ratio，
         完成率 = 待办计数给的 done/obsolete/total（口径写在卡片 hint 里）。 -->
    <h3 class="section-title">水位与趋势</h3>
    <div class="chart-grid">
      <div class="panel pad">
        <div class="chart-title">水位</div>
        <RingGauge :metrics="gaugeMetrics" :height="gaugeHeight" />
        <p class="src">{{ gaugeHint }}</p>
      </div>
      <div class="panel pad">
        <div class="chart-title">近 7 天记忆增量</div>
        <SparkLine :points="dailyPoints" :height="sparkHeight" />
        <p class="src">{{ dailyHint }}</p>
      </div>
    </div>

    <!-- 近 7 天增量 · **按代理**：内联 SVG 堆叠柱（每个代理一条色带）。
         🔴 横轴 = `daily` 段（后端补齐了每一天），色带 = `daily_by_agent` 段
         （只给有写入的格子）—— 横轴只补一次（后端补一次、前端再补一次必漂）。
         序列 = 7 天总量降序前 6 + 「其他」桶；数字全部来自接口，零写死。 -->
    <div class="panel pad">
      <div class="chart-title">近 7 天记忆增量 · 按代理</div>
      <svg
        v-if="agentChart.segs.length"
        class="stack-chart"
        :viewBox="agentChart.viewBox"
        role="img"
        :aria-label="agentChartAria"
      >
        <!-- 网格线：0 / 中值 / 满值（值标在左侧） -->
        <g class="grid">
          <line
            v-for="g in agentChart.grid"
            :key="`g${g.v}`"
            :x1="agentChart.padL"
            :x2="agentChart.right"
            :y1="g.y"
            :y2="g.y"
          />
        </g>
        <!-- 堆叠柱：一格 = 一天 × 一个代理 -->
        <g class="bars">
          <rect
            v-for="seg in agentChart.segs"
            :key="seg.key"
            :x="seg.x"
            :y="seg.y"
            :width="seg.w"
            :height="seg.h"
            :fill="seg.color"
          />
        </g>
        <!-- 坐标文字 -->
        <g class="axis">
          <text
            v-for="g in agentChart.grid"
            :key="`t${g.v}`"
            :x="agentChart.padL - 6"
            :y="g.y + 3"
            text-anchor="end"
          >{{ g.v }}</text>
          <text
            v-for="x in agentChart.xLabels"
            :key="`x${x.text}`"
            :x="x.x"
            :y="x.y"
            text-anchor="middle"
          >{{ x.text }}</text>
        </g>
      </svg>
      <div v-else class="empty">
        <b>近 7 天没有可分列的增量</b>
        <span>这段时间没有新增的记忆条目（接口没给到任何按代理的格子）。</span>
      </div>
      <!-- 图例：色点 + 名字 + 7 天总量（数字来自接口） -->
      <ul v-if="agentChart.series.length" class="legend">
        <li v-for="s in agentChart.series" :key="s.key" :title="s.title">
          <span class="dot" :style="{ background: s.color }"></span>
          <span class="lg-label">{{ s.label }}</span>
          <span class="lg-num num">{{ s.total }}</span>
        </li>
      </ul>
      <p class="src">{{ dailyByAgentHint }}</p>
    </div>

    <!-- 待办小节：开放 N / 逾期 M，点进去就是待办页。 -->
    <h3 class="section-title">待办</h3>
    <div class="stat-grid">
      <button type="button" class="todo-jump" @click="gotoTodos" aria-label="进入待办页">
        <StatCard label="开放" :value="todoSummary.open" unit="条" hint="还没销掉的待办" />
      </button>
      <button type="button" class="todo-jump" @click="gotoTodos" aria-label="进入待办页">
        <StatCard
          label="逾期"
          :value="todoSummary.overdue"
          unit="条"
          :tone="todoSummary.overdue ? 'danger' : 'ok'"
          hint="到期日已过，还没销"
        />
      </button>
      <button type="button" class="todo-jump" @click="gotoTodos" aria-label="进入待办页">
        <StatCard label="已完成" :value="todoSummary.done" unit="条" hint="status = done" />
      </button>
    </div>
    <p class="src">待办独立成页（状态机 + 到期日 + 归属）：点任意一张卡进「待办」。</p>
    <div v-if="todoError" class="banner banner--danger">{{ todoError }}</div>

    <!-- 代理小节：在册 / 已批准 / 在线 + 每个代理的累计读写。
         「在线」的口径写在卡上（注册表 last_seen_at 5 分钟内），
         「读写」是**累计**计数（注册表只有累计列，没有按日打点）——
         不为了「今日」另造打点。 -->
    <h3 class="section-title">代理</h3>
    <div class="stat-grid">
      <StatCard label="在册代理" :value="agentRows.length" unit="个" hint="注册表里的全部代理" />
      <StatCard label="已批准" :value="approvedCount" unit="个" hint="status = approved" />
      <StatCard label="在线" :value="onlineCount" unit="个" :hint="ONLINE_HINT" />
    </div>
    <div v-if="agentError" class="banner banner--danger">{{ agentError }}</div>
    <div v-if="agentRows.length" class="panel pad agent-list">
      <div v-for="a in agentRows" :key="a.agent_id" class="agent-line">
        <span class="agent-name">{{ a.name || a.agent_id }}</span>
        <span class="agent-id mono">{{ a.agent_id }}</span>
        <span class="chip" :class="a.status === 'approved' ? 'chip--accent' : ''">{{ statusText(a.status) }}</span>
        <span class="chip">{{ a.online ? '在线' : '离线' }}</span>
        <span class="num agent-num">累计读 {{ a.reads }} · 累计写 {{ a.writes }}</span>
        <span class="num agent-num">最近活动 {{ toLocal(a.last_seen_at) }}</span>
      </div>
      <p class="src">
        读写是注册表的累计计数（注册表没有按日打点 ⇒ 不给「今日」增量）；
        「在线」= 最近活动落在 5 分钟内。
      </p>
    </div>

    <h3 class="section-title">分级班车</h3>
    <div class="stat-grid">
      <StatCard
        label="常驻预算水位"
        :value="tiering?.budget ? `${tiering.budget.ratio}%` : '-'"
        :percent="tiering?.budget?.ratio ?? null"
        :tone="tieringTone"
        :hint="tiering?.budget ? `已占 ${tiering.budget.used} / ${tiering.budget.total} 字符` : '读取失败'"
      />
      <StatCard
        label="班车状态"
        :value="tiering ? (tiering.enabled ? '已开' : '默认关') : '-'"
        :hint="tieringHint"
      />
      <StatCard
        label="上次运行"
        :value="tieringLastRun"
        :hint="tiering?.last_run ? (tiering.last_dry_run ? 'dry-run（只算不写）' : '实跑') : '班车还没跑过'"
      />
    </div>
    <div v-if="tieringError" class="banner banner--danger">{{ tieringError }}</div>

    <h3 class="section-title">知识库</h3>
    <div class="stat-grid">
      <StatCard label="skill_docs" :value="kb.skill_docs.value" :hint="kb.skill_docs.hint" />
      <StatCard label="documents" :value="kb.documents.value" :hint="kb.documents.hint" />
      <StatCard label="chunks" :value="kb.chunks.value" :hint="kb.chunks.hint" />
    </div>

    <h3 class="section-title">记忆类定时任务</h3>
    <div class="sched-line">
      <span class="sched-k">调度器：</span>
      <span class="sched-v">{{ schedulerStatus || '未知' }}</span>
    </div>
    <div v-if="cronError" class="banner banner--danger">{{ cronError }}</div>

    <div v-if="loading && !crons.length" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <div v-else-if="!crons.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="12" cy="12" r="8" />
          <path d="M12 7.4V12l3 1.8" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p><b>没有匹配到记忆类任务</b>它们来自 cron 的 jobs.json，不在这个文件里就查不到。</p>
      </div>
    </div>

    <div v-else class="card-grid cron-grid">
      <ItemCard
        v-for="row in crons"
        :key="row.name"
        :chip-tone="row.enabled ? 'accent' : ''"
        @lead="doCopy(row.script)"
      >
        <template #tag>{{ row.enabled ? '已启用' : '已停用' }}</template>
        <template #time>
          上次 {{ toLocal(row.last_run_at) }}<template v-if="row.next_run"> · 下次 {{ toLocal(row.next_run) }}</template>
        </template>

        <p class="card-title">{{ row.name }}</p>
        <!-- 一次性 / 事件触发的任务本来就没有 script：占位说明来由，
             复制钮与底部链接一并收掉，不留一个点下去复制空串的悬空图标。 -->
        <span v-if="row.script" class="cell-key">
          <span class="k mono" :title="row.script">{{ row.script }}</span>
          <button class="copy-btn" title="复制脚本路径" @click.stop="doCopy(row.script)">
            <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
              stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
              <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
            </svg>
          </button>
        </span>
        <span v-else class="no-script">（无脚本：事件/一次性任务）</span>

        <!-- 标签区固定成「cron 起头 + 状态紧随」的一行：全局 .card-tags 是
             可换行的流式排布，一排卡里 ok 的左缘会各按各的 cron 长度跑。 -->
        <template #tags>
          <span class="cron-tags">
            <span class="tag mono t-cron">{{ row.schedule }}</span>
            <span v-if="row.last_status" class="tag mono t-status">{{ row.last_status }}</span>
          </span>
        </template>

        <template v-if="row.script" #lead>复制脚本路径 ↗</template>

        <!-- 管理按钮**只对认得出 job_id 的行出现**：取不到 id 的行保持只读，
             宁可少一个按钮，也不去对一个没读懂的 jobs.json 乱发写请求。 -->
        <template #actions>
          <button v-if="row.job_id" class="act-btn" @click.stop="askToggle(row)">
            {{ row.enabled ? '暂停' : '恢复' }}
          </button>
          <button v-if="row.job_id" class="act-btn" @click.stop="openEdit(row)">改调度</button>
        </template>
      </ItemCard>
    </div>
    <div class="src">来源：{{ cronSource }}</div>

    <!-- 暂停 / 恢复：一句后果 + 确认（不写无信息量的那句问话） -->
    <ConfirmDialog
      v-model="toggleAsk"
      :title="toggleTitle"
      :consequences="toggleConsequences"
      :confirm-text="toggleConfirmText"
      tone="primary"
      :busy="busyCron"
      @confirm="doToggle"
    />

    <!-- 改调度：旧值 → 新值对比在正文里，确认按钮前就能看见改成了什么 -->
    <el-dialog
      v-model="editAsk"
      width="460px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyCron"
      :show-close="!busyCron"
      append-to-body
      title="改这条任务的调度"
    >
      <p class="edit-old mono">{{ row?.schedule || '（无）' }}</p>
      <label class="edit-label">
        <span>新调度（cron 表达式）</span>
        <el-input
          v-model="newSchedule"
          placeholder="例：30 4 * * *"
          autocomplete="off"
          spellcheck="false"
          @keyup.enter="doEdit"
        />
      </label>
      <p v-if="editError" class="edit-err">{{ editError }}</p>
      <p class="edit-tip">改完由官方 hermes cron CLI 执行并立即生效；表达式非法时后端会带例子回你。</p>
      <template #footer>
        <el-button :disabled="busyCron" @click="editAsk = false">取消</el-button>
        <el-button type="primary" :loading="busyCron" :disabled="!newSchedule.trim()" @click="doEdit">
          确认改调度
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AgentUsageBars from '../components/AgentUsageBars.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import RingGauge from '../components/RingGauge.vue'
import SparkLine from '../components/SparkLine.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { agentUsageRows, isOnline, loadAgentRows, ONLINE_HINT } from '../utils/agents'
import { chartPalette, usageColor } from '../utils/chart-theme.js'
import { bucketByAgent, OTHER_KEY, stackLayout, topSeries } from '../utils/daily-by-agent'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { completionPercent } from '../utils/todo'

const data = ref(null)
const crons = ref([])
const cronSource = ref('')
const schedulerStatus = ref('')
const loading = ref(false)
const error = ref('')
const cronError = ref('')
const tiering = ref(null)
const tieringError = ref('')

// 待办小节（2026-10 ks_todo）：计数**全部由后端给**（前端不自己数）。
const todoSummary = ref({ open: 0, done: 0, obsolete: 0, total: 0, overdue: 0 })
const todoError = ref('')

/* ---------------------------------------------------------------- 图表
 * 三个图共用 `utils/chart-theme.js` 的色板与 `utils/chart-kit.js` 的 option 形状。
 * 🔴 图高按**视口宽度**给（窄屏图矮一点，横向不滚动 —— 容器宽 100%，
 *    ECharts 自己 resize；不许出现横向滚动条，那是最常见的看板事故）。 */
const chartWidth = ref(typeof window === 'undefined' ? 1200 : window.innerWidth)
function onViewportResize() {
  chartWidth.value = window.innerWidth
}
const barsHeight = computed(() => (chartWidth.value < 900 ? 220 : 300))
const gaugeHeight = computed(() => (chartWidth.value < 900 ? 200 : 240))
const sparkHeight = computed(() => (chartWidth.value < 900 ? 180 : 220))

// 待审条目计数（侧栏同一份数据，独立 fetch 让这页不至于因 review 端点挂了而红）
// — 顶部「待审核 N 条」卡专用。三个数全库口径，与 GET /api/v1/review/counts 一致。
const reviewCounts = ref({ promote_candidates: 0, demote_candidates: 0, kb_pending: 0 })
const reviewError = ref('')
/** 待审总数 = 晋升候选 + 退居建议 + 知识库待批（与侧栏徽标汇总口径一致）。
 *  0 时给绿色「无待审」正向反馈（不是隐藏 —— 用户会以为是 bug）；>0 走 danger。 */
const reviewTotal = computed(() =>
    (reviewCounts.value?.promote_candidates || 0)
  + (reviewCounts.value?.demote_candidates || 0)
  + (reviewCounts.value?.kb_pending || 0)
)
const reviewTone = computed(() => (reviewTotal.value > 0 ? 'danger' : 'ok'))
// 0 时也展示卡面：徽标是「记得处理一下」的提示，无事也让人安心（与 cron 那排
// 「无匹配任务」走空态同款 —— 总归有个说法）。
const router = useRouter()
function gotoReview() {
  // 整页只有这一个 chip；点哪都跳同一个目标：/review（晋升 + 退居都在那）。
  router.push('/review')
}

/* ---------------------------------------------------------------- 定时任务管理
 * 三条操作（暂停 / 恢复 / 改调度）都改的是网关那份 jobs.json —— 本端只发请求，
 * 真动作由后端调官方 `hermes cron` CLI 执行。成功后一律重拉列表：启停状态与
 * next_run 都由文件说了算，本地改一改很快就和真实调度对不上了。 */
const row = ref(null)            // 正在操作的那一行
const busyCron = ref(false)
const toggleAsk = ref(false)
const editAsk = ref(false)
const newSchedule = ref('')
const editError = ref('')

const pausing = computed(() => !!row.value && row.value.enabled)
const toggleTitle = computed(() => (pausing.value ? '暂停这条定时任务？' : '恢复这条定时任务？'))
const toggleConsequences = computed(() => [
  pausing.value ? '暂停后不再按计划执行（已跑的这一次不受影响）' : '恢复后按当前调度继续执行',
  '改的是网关的 jobs.json（官方 hermes cron CLI 执行），本控制台只留一条操作账',
])
const toggleConfirmText = computed(() => (pausing.value ? '确认暂停' : '确认恢复'))

/**
 * 班车卡片的水位配色：≥100% danger（水已经溢出，注入在截断）、≥90% accent
 * （快到告警线，班车下一轮就要提名退位）。口径与 `routers/ops.tiering_status`
 * 给的 ratio 同一个（percent）。
 */
const tieringTone = computed(() => {
  const r = tiering.value?.budget?.ratio
  if (typeof r !== 'number') return ''
  if (r >= 100) return 'danger'
  if (r >= 90) return 'accent'
  return 'ok'
})

const tieringHint = computed(() => {
  if (!tiering.value) return ''
  const t = tiering.value
  if (t.enabled) return `提名 ≥${t.settings.promote_nominate.min_hits} 次/窗口，每轮上限 ${t.settings.batch_cap} 条`
  return t.note || '总开关关着，班车只算不写'
})

const tieringLastRun = computed(() => (tiering.value?.last_run ? toLocal(tiering.value.last_run) : '—'))

/* ---------------------------------------------------------------- 代理维度
 * 多代理时代，看板要看得见「谁在跑、谁占了常驻层、谁的预算快满了」。
 * 数据从两个只读端点来（各自独立 fetch，挂了就挂自己那一段，不拖红整页）：
 *   * `GET /api/v1/agents` —— 注册表（在册 / 已批准 / 最近活动 / 累计读写）；
 *   * `GET /api/v1/stats/core-per-agent` —— 常驻层按代理分列（条数 / 字符 / 各自预算）。 */
const agentRows = ref([])          // 注册表每行 + online 判定
const perAgent = ref({})           // 常驻层按代理分列
const agentError = ref('')         // 注册表读失败
const perAgentError = ref('')      // 分列读失败（与上面**分开**：一段挂了不许擦掉另一段的报错）

/** agent_id → 注册表备注名（分列卡上的标签用名字，鼠标悬停能看 id）。 */
const agentNames = computed(() =>
    Object.fromEntries(agentRows.value.map((a) => [a.agent_id, a.name || a.agent_id])),
)

const approvedCount = computed(() => agentRows.value.filter((a) => a.status === 'approved').length)
const onlineCount = computed(() => agentRows.value.filter((a) => a.online).length)

/** 常驻层分列卡（每代理一张：占用率 + 条数 + 字符/预算）。 */
const coreAgentCards = computed(() => agentUsageRows(perAgent.value, agentNames.value))

/** 已归属到某个代理的条数 / 字符（分列之外的差额 = 无归属的那些）。 */
const attributed = computed(() => coreAgentCards.value.reduce(
    (acc, r) => ({ count: acc.count + r.count, chars: acc.chars + r.chars }),
    { count: 0, chars: 0 },
))

const unattributedCount = computed(() => Math.max(0, (data.value?.core?.count ?? 0) - attributed.value.count))
const unattributedChars = computed(() => Math.max(0, (data.value?.core?.chars ?? 0) - attributed.value.chars))
/** 全局注入预算（没给代理单设预算时它生效）。 */
const globalBudget = computed(() => data.value?.core?.budget ?? '-')
const attributedHint = computed(() =>
    unattributedCount.value
      ? `其中 ${attributed.value.count} 条有代理归属，${unattributedCount.value} 条无归属`
      : '全部有代理归属',
)

/** 注册表状态的显示词（枚举外的值显示原样，不编一个）。 */
function statusText(status) {
    return { approved: '已批准', pending: '待审', revoked: '已吊销' }[status] || status || '未知'
}

async function fetchAgentRows() {
  try {
    const rows = await loadAgentRows(api.listAgents)
    agentRows.value = rows.map((r) => ({ ...r, online: isOnline(r) }))
    agentError.value = ''
  } catch (e) {
    agentRows.value = []
    agentError.value = `读取代理注册表失败：${e.message}`
  }
}

async function fetchCorePerAgent() {
  try {
    perAgent.value = await api.corePerAgent()
    perAgentError.value = ''
  } catch (e) {
    perAgent.value = {}
    perAgentError.value = `读取常驻层代理分列失败：${e.message}`
  }
}

/**
 * kb 段不可用时后端给的是 {available:false, error:...} —— 显示成可见的报错，不是 0。
 * 报错文案太长，放不进 32px 的数字位，所以数字位写「不可用」、原文进 hint。
 */
function kbCard(field) {
  const kb = data.value?.kb
  if (!kb || kb.available === false) {
    return { value: '不可用', hint: kb?.message || kb?.error || '未知' }
  }
  return { value: kb[field] ?? '-', hint: '' }
}
const kb = computed(() => ({
  skill_docs: kbCard('skill_docs'),
  documents: kbCard('documents'),
  chunks: kbCard('chunks'),
}))

/**
 * 调度器那一行的显示值。
 *
 * 后端已经把 `hermes cron status` 末行拆成结构化两段（{text, next_run}，
 * 见 crons.py `_parse_scheduler_status`）：`text` 是人话，`next_run` 是 ISO
 * 串。这里只做「把 ISO 显示成本地时间」这一件事 —— 时间戳曾经以
 * `Next run: 2026-10-17T04:30:00.123456+08:00` 混在自由文本里一路透传，
 * 界面上渲成「调度器：下次 X · Next run:」，同一件事说两遍。
 * 两段都空才退「未知」，缺哪段都只显示有的那半边。
 */
function schedLabel(raw) {
  // 兼容旧实例回的还是裸字符串（发版错配时不至于整行变「未知」）
  const o = typeof raw === 'string' ? { text: raw, next_run: '' } : (raw || {})
  const bits = []
  if (o.text) bits.push(o.text)
  if (o.next_run) bits.push(`下次 ${toLocal(o.next_run)}`)
  return bits.join(' · ') || '未知'
}

/* ---------------------------------------------------------------- 图表数据
 * 近 7 天每日新增（`?days=7` 那段 `daily`）—— 后端补齐了没写入的那天（= 0，
 * 不是空白），前端只画。 */
const dailyPoints = computed(() => (data.value?.daily || []).map((d) => ({
  date: d.date, count: Number(d.count || 0),
})))

const dailyHint = computed(() => {
  const sum = dailyPoints.value.reduce((acc, p) => acc + p.count, 0)
  return `近 7 天新增 ${sum} 条（每天一格，没写入的那天是 0）`
})

/* ---------------------------------------------------------------- 近 7 天 · 按代理
 * `daily_by_agent`（`GET /api/v1/stats?days=7` 新增的一段）= 每日增量**按代理分列**
 * 的格子（`{d, agent, n}`，**只给有写入的格子**）。横轴用 `daily` 段（后端补齐了
 * 每一天），色带用这一段 —— 横轴是图表口径，只补一次（后端补一次、前端补一次必漂）。
 * 数值全部来自接口 —— 前端零写死。
 *
 * 🔴 图表是**内联 SVG**（不是 ECharts）：SVG 里的 `var(--*)` 直接吃 CSS 变量，
 *    切主题不用重画（canvas 那套得订阅主题变化再重画一次，见 components/SparkLine.vue）。
 * 🔴 色板只用 tokens.css 的既有令牌（DESIGN.md：组件里不许散落硬编码色值）。 */
const DAILY_TOP_N = 6

/** 序列色板：**只用 tokens.css 里的既有令牌**。
 *  令牌表里没有「分类色板」这一档，凑不出七种不撞色的彩色 ⇒ 四色 + 两灰：
 *  四个状态色各给一个序列，剩下的用两档中性灰。
 *  灰的那两档天然适合放最小的序列与「其他」桶（归并项不该抢眼）。 */
const SERIES_COLORS = [
  'var(--accent)',    // 蓝：7 天总量最大的那个代理
  'var(--ok)',        // 绿
  'var(--warn)',      // 琥珀
  'var(--danger)',    // 红
  'var(--accent-5)',  // 浅蓝（accent 的浅一档）
  'var(--text-dim)',  // 深灰：第 6 个序列
]
/** 「其他」桶固定用一档中性灰 —— 它是**归并项**，不该抢眼。 */
const OTHER_COLOR = 'var(--text-faint)'

/** 横轴：后端补齐的连续 N 天（`daily` 段）。 */
const dailyDays = computed(() => (data.value?.daily || []).map((d) => d.date))

/** 归属标签的显示名：注册表备注名 → 代理 id → 「未标注」（无归属的老条目）。 */
function agentLabel(agent) {
  if (!agent || agent === '-') return '未标注'
  return agentNames.value[agent] || agent
}

/** 序列 = 7 天总量降序前 6 + 「其他」桶（其余代理合并进去）。
 *  分组 / 排序 / 归并 / 摆位置都在 `utils/daily-by-agent.js`（纯函数，可单测）。 */
const agentSeries = computed(() =>
  topSeries(bucketByAgent(data.value?.daily_by_agent || []), DAILY_TOP_N, agentLabel))

/** 内联 SVG 的画布尺寸（viewBox 坐标系，与实际像素解耦 ⇒ 窄屏不横向滚动）。 */
const CHART = { w: 720, h: 240, padL: 38, padR: 12, padT: 14, padB: 30 }

/** 堆叠柱的几何：每一格（天 × 代理）一个矩形 + 网格线 + 坐标文字。
 *  🔴 颜色**挂在序列上**（图例色点与柱子里的是同一份，不会两处各画一份）。 */
const agentChart = computed(() => {
  const series = agentSeries.value.map((s, i) => ({
    ...s,
    color: s.key === OTHER_KEY
      ? OTHER_COLOR
      : SERIES_COLORS[i % SERIES_COLORS.length],
  }))
  const out = stackLayout(dailyDays.value, series, { ...CHART, colorOf: (s) => s.color })
  return { ...out, series }
})

const agentChartAria = computed(() =>
  `近 7 天记忆增量按代理分列，共 ${agentChart.value.total} 条`)

const dailyByAgentHint = computed(() => {
  const c = agentChart.value
  if (!c.series.length) return '接口没有给到任何按代理的增量格子（库里这 7 天没有新增条目）。'
  const other = c.series.find((s) => s.key === OTHER_KEY)
  return `近 7 天新增 ${c.total} 条，按代理分 ${c.series.length} 个序列`
    + (other ? `（「其他」= ${other.restCount} 个代理合并，共 ${other.total} 条）` : '')
    + '；没打归属标签的老条目归「未标注」一栏，不摊到别人头上。'
})

/** 总预算水位（%）—— tiering 给的 ratio（与「常驻预算水位」卡同一份数）。 */
const budgetPercent = computed(() => {
  const r = tiering.value?.budget?.ratio
  return typeof r === 'number' ? r : null
})

/** 待办完成率（%）—— 已销掉（done + obsolete）占全部（口径写在 hint 里）。 */
const todoPercent = computed(() => completionPercent(todoSummary.value))

/** 两块环形仪表：总预算水位 + 待办完成率（颜色取自 chart-theme 的色板）。 */
const gaugeMetrics = computed(() => {
  const p = chartPalette()
  const metrics = []
  if (budgetPercent.value !== null) {
    metrics.push({
      label: '总预算水位',
      percent: budgetPercent.value,
      color: usageColor(budgetPercent.value, p),
    })
  }
  metrics.push({
    label: '待办完成率',
    percent: todoPercent.value,
    color: usageColor(todoPercent.value, p),
  })
  return metrics
})

const gaugeHint = computed(() => {
  const bits = []
  if (budgetPercent.value !== null) {
    bits.push(`总预算水位 ${budgetPercent.value}%`)
  } else {
    bits.push('总预算水位未读到')
  }
  bits.push(`待办完成率 ${todoPercent.value}%（已销掉的 done + obsolete ÷ 全部）`)
  return bits.join(' · ')
})

async function fetchTodoSummary() {
  // 单独 fetch：待办端点挂了不许把整张看板拖红（只掉这一小节）
  try {
    const s = await api.todoSummary()
    todoSummary.value = {
      open: Number(s?.open || 0),
      done: Number(s?.done || 0),
      obsolete: Number(s?.obsolete || 0),
      total: Number(s?.total || 0),
      overdue: Number(s?.overdue || 0),
    }
    todoError.value = ''
  } catch (e) {
    todoSummary.value = { open: 0, done: 0, obsolete: 0, total: 0, overdue: 0 }
    todoError.value = `读取待办计数失败：${e.message}`
  }
}

/** 进待办页（待办小节的卡点哪儿都跳同一个地方）。 */
function gotoTodos() {
  router.push('/todos')
}

async function load() {
  loading.value = true
  try {
    // ?days=7：顺带取近 7 天每日新增（折线图那段）
    data.value = await api.stats({ days: 7 })
    error.value = ''
  } catch (e) {
    error.value = `读取总览失败：${e.message}`
  }
  // 代理维度两段各自独立取数：任一挂了只掉自己那一段，不把整张看板拖红
  await Promise.all([loadReview(), fetchAgentRows(), fetchCorePerAgent(),
    fetchTodoSummary()])
  try {
    const c = await api.crons()
    crons.value = c.jobs
    cronSource.value = c.source
    // 无 hermes CLI 时后端给 null —— 这一行显示「未知」，不许把整页拖成报错
    schedulerStatus.value = c.scheduler_status || ''
    schedulerStatus.value = schedLabel(schedulerStatus.value)
    cronError.value = c.count ? '' : `没有匹配到记忆类任务（jobs.json：${c.source}）`
  } catch (e) {
    cronError.value = `读取定时任务失败：${e.message}`
  }
  // 分级班车状态单独读：它挂了就挂这一段，不许把上面两张卡一起拖成报错
  try {
    tiering.value = await api.tieringStatus()
    tieringError.value = ''
  } catch (e) {
    tiering.value = null
    tieringError.value = `读取分级班车状态失败：${e.message}`
  } finally {
    loading.value = false
  }
}

async function loadReview() {
  // 单独 fetch：失败不让整张看板翻红（侧栏徽标挂了就挂，这页「待审核」卡退成「-」即可）
  try {
    const data = await api.reviewCounts()
    reviewCounts.value = {
      promote_candidates: Number(data?.promote_candidates || 0),
      demote_candidates: Number(data?.demote_candidates || 0),
      kb_pending: Number(data?.kb_pending || 0),
    }
    reviewError.value = ''
  } catch (e) {
    reviewError.value = `读取待审数失败：${e.message}`
  }
}

async function doCopy(text) {
  const ok = await copyText(text)
  ok ? showToast('已复制') : showError('复制失败，请手动选中复制')
}

/** 暂停 / 恢复：先开确认框，确认后才发请求（后端还会再要一次 confirm）。 */
function askToggle(r) {
  if (!r.job_id) return
  row.value = r
  toggleAsk.value = true
}

async function doToggle() {
  const r = row.value
  busyCron.value = true
  try {
    const res = r.enabled ? await api.cronPause(r.job_id) : await api.cronResume(r.job_id)
    toggleAsk.value = false
    showToast(res.action === 'pause' ? '已暂停' : '已恢复')
    await load()
  } catch (e) {
    showError(`${toggleTitle.value.replace('？', '')}失败：${e.message}`)
  } finally {
    busyCron.value = false
  }
}

/** 改调度：弹框里预填当前表达式，正文就是「旧值 → 新值」的对比。 */
function openEdit(r) {
  if (!r.job_id) return
  row.value = r
  newSchedule.value = r.schedule || ''
  editError.value = ''
  editAsk.value = true
}

async function doEdit() {
  const r = row.value
  const expr = newSchedule.value.trim()
  if (!expr || busyCron.value) return
  busyCron.value = true
  editError.value = ''
  try {
    await api.cronEdit(r.job_id, expr)
    editAsk.value = false
    showToast('调度已改')
    await load()
  } catch (e) {
    // 表达式非法（400）由后端带 example 回来，原样显示在弹框里，不弹 toast
    editError.value = e.message || '改调度失败'
  } finally {
    busyCron.value = false
  }
}

onMounted(() => {
  // 图高跟着视口宽度走（窄屏图矮一点）：只改高度，**不许**改宽度 ——
  // 容器宽 100% + ECharts 自 resize，横向滚动条是最常见的看板事故。
  window.addEventListener('resize', onViewportResize)
  load()
})
onUnmounted(() => window.removeEventListener('resize', onViewportResize))
</script>

<style scoped>
/* 图表两栏：窄屏自动落成一列（不许横向滚动 —— 看板最常见的事故） */
.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
}
.chart-title {
  font-size: 13px;
  color: var(--text-dim);
  margin-bottom: 6px;
}
/* 待办小节的卡：整张卡可点（进待办页）—— 按钮壳不带默认边框/底色 */
.todo-jump {
  display: block;
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  cursor: pointer;
  text-align: left;
}
.todo-jump:hover .stat {
  border-color: var(--accent);
}
/* ---------------------------------------------------------------- 任务卡内布局
 * 这一节的全部修复都落在 StatsView 的卡内类上：ItemCard 与 tokens.css 是
 * 常驻层 / 记忆条目 / 知识库几页共用的，动它们会把别的视图一起带歪。 */
.pad {
  padding: 20px;
}

/* 列数对齐上方区块：全局 .card-grid 是 300px 下限，1920 下排成 5 列 ~320px 窄卡，
   时间行/任务名/操作行三处错乱都是从这里来的。抬到 420px 后 1920 下 3 列、
   1440 下 2-3 列，与上面的 4/3/3 列节奏接得上。scoped 的属性选择器权重
   高于 tokens.css 的裸类，覆盖成立；900px 以下退回单列（那边有 grid-template
 * 也压不过 scoped，只能自己再写一档）。 */
.cron-grid {
  grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
}
@media (max-width: 900px) {
  .cron-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}

/* 时间行：始终左贴徽标，超宽省略。全局 .card-time 是 flex:none，
   只有「上次 … · 下次 …」短文时会被 space-between 甩到卡片右缘，和长文行
   的左对齐策略打架；这里拉满整行并给上省略，不再硬裁切成「2026-10-1」。 */
.cron-grid :deep(.card-time) {
  flex: 1 1 auto;
  min-width: 0;
  text-align: left;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 任务名单行：全局是 2 行 clamp，长名（memory-pointer-mode 重启后核验）
   折两行会把整卡行高顶歪，同排卡片的操作行跟着错开。 */
.cron-grid .card-title {
  -webkit-line-clamp: 1;
  line-clamp: 1;
  min-width: 0;
}

/* 无脚本的占位：--text-ghost 按 tokens.css 的约定就是给纯装饰与占位符用的，
   这里不是要读的内容，不占对比度。 */
.no-script {
  color: var(--text-ghost);
  font-size: 12px;
}

/* 标签区固定结构：cron 表达式起头（可省略让位），ok/失败状态紧随且不换行，
   一排卡里状态标签的左缘按等宽字宽对齐，不再各按各的 cron 长度参差。 */
.cron-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.cron-tags .t-cron {
  flex: 0 1 auto;
  min-width: 0;
}
.cron-tags .t-status {
  flex: none;
}

/* 操作行：文字链接与 28px 描边钮同高（28 是 .act-btn 的既有高度，不是新令牌），
   窄卡换行时 ItemCard 已有 flex-wrap + row-gap:6px 与 8px 间距。 */
.cron-grid :deep(.card-link) {
  height: 28px;
}

.src {
  margin-top: 12px;
  color: var(--text-faint);
  font-size: 12px;
}

/* ---------------------------------------------------------------- 按代理堆叠柱
 * 内联 SVG：viewBox 720×240 + `width:100%` ⇒ 窄屏不横向滚动；
 * `max-width` 封顶（宽屏上别把一张图拉成 1500px 宽的稀疏柱阵）。
 * 颜色全在 :fill 里给 var(--*)，CSS 变量直接生效 ⇒ 切主题不用重画。 */
.stack-chart {
  display: block;
  width: 100%;
  max-width: 760px;
  height: auto;
  overflow: visible;
}
/* 网格线：与 ECharts 那套同一个令牌（--chart-grid，不透明的一档） */
.stack-chart .grid line {
  stroke: var(--chart-grid);
  stroke-width: 1;
}
/* 堆叠段之间留一道底色的缝：两段挨着才分得出谁是谁 */
.stack-chart .bars rect {
  stroke: var(--bg-elev);
  stroke-width: 1;
  shape-rendering: crispEdges;
}
.stack-chart .axis text {
  fill: var(--text-faint);
  font-size: 10px;
  font-family: var(--font);
}
/* 图例：色点 + 名字 + 7 天总量（数字来自接口） */
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}
.legend li {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.legend .dot {
  width: 9px;
  height: 9px;
  border-radius: 2px;
  flex: none;
}
.legend .lg-label {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.legend .lg-num {
  color: var(--text-faint);
  font-size: 12px;
}

/* 代理小节的每行：一个代理 + 状态/在线 chip + 累计读写 + 最近活动。
   一行一个代理，横着排；窄屏允许折行（不裁字）。零新色：chip 走全局
   .chip / .chip--accent，行内数字走 .num。 */
.agent-list {
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
.agent-name {
  font-weight: 600;
  color: var(--text);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agent-num {
  color: var(--text-dim);
  font-size: 12.5px;
}
/* agent_id：备注名之外的第二身份（同名代理靠它区分），弱化显示。 */
.agent-id {
  color: var(--text-faint);
  font-size: 12.5px;
}
/* 待审核卡：自成一类（不是普通 StatCard —— 整卡可点击跳转 + 三色分项 pill）。
   零新色：danger / on-accent / ok / chip 全部走既有 token。 */
.review-card-btn {
  width: 100%;
  text-align: left;
  padding: 0;
  background: transparent;
  border: 0;
  cursor: pointer;
  font: inherit;
  color: inherit;
}
.review-card-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: var(--r-card);
}
.review-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 18px 20px;
  border-radius: var(--r-card);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  box-shadow: var(--shadow-card);
  transition: box-shadow 0.12s, transform 0.12s;
}
.review-card-btn:hover .review-card {
  box-shadow: var(--shadow-hover);
  transform: translateY(-1px);
}
.review-card-btn--zero .review-card {
  border-color: var(--border);
}
.review-card-label {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  color: var(--text-faint);
  font-size: 13px;
}
.review-card-hint {
  color: var(--accent);
  font-size: 12px;
}
.review-card-value {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: -0.5px;
  line-height: 1.1;
}
.review-card-value.tone-danger {
  color: var(--danger);
}
.review-card-value.tone-ok {
  color: var(--ok);
}
.review-card-value .unit {
  font-size: 14px;
  font-weight: 500;
  margin-left: 4px;
  color: var(--text-faint);
}
.review-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.review-card-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--r-chip);
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}
.review-card-pill.tone-danger-pill {
  background: var(--danger-soft);
  color: var(--danger);
}
.review-card-pill.tone-ok-pill {
  background: var(--chip);
  color: var(--text-dim);
}
.review-card-err {
  font-size: 12px;
  color: var(--danger);
}

/* 调度器状态一行：与 stat-grid 同色板，不新造颜色 */
.sched-line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 14px;
  font-size: 13px;
}
.sched-k {
  color: var(--text-faint);
}
.sched-v {
  color: var(--text-dim);
}
/* 改调度弹框：旧值在上（弱），新值在下（可编辑），错误回显贴在输入框下 */
.edit-old {
  margin: 0 0 14px;
  color: var(--text-faint);
  font-size: 13px;
}
.edit-label {
  display: block;
  cursor: text;
}
.edit-label > span {
  display: block;
  margin-bottom: 7px;
  color: var(--text-faint);
  font-size: 12px;
}
.edit-err {
  margin: 10px 0 0;
  color: var(--danger);
  font-size: 12.5px;
  line-height: 1.6;
}
.edit-tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}
</style>
