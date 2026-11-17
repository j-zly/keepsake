<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">看板</h1>
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

    <!-- 水位与趋势：**按代理水位条组** + 待办完成率环形 + 近 7 天增量图。
         🔴 数字**全部来自后端**（前端不自己算比例）：每代理水位 = 该代理的常驻字符
         ÷ 它**自己**生效的注入预算（`stats/core-per-agent` 的 budget_effective）；
         完成率 = 待办计数给的 done/obsolete/total（口径写在卡片 hint 里）。
         🔴 **总预算环删了** —— 总量放在几十个代理上是准的，放在几百个代理上
         就是一句「整体还行」，看不出是谁快满了；每个代理一份独立预算，
         要看的就是「谁的条快满了」。总量仍在下面「分级班车」那张卡上（同一份数）。 -->
    <h3 class="section-title">按代理水位与趋势</h3>
    <div class="chart-grid">
      <!-- 按代理水位：**每代理一行**（名字 + 百分比 + 细条 + 字符/预算）。
           内联 CSS 条（零新依赖）；按占用降序，前 8 个 + 「其余 N 个」折叠。
           ≥90% 走警示色、≥100% 走危险色（与全站占用条同一把尺子）。 -->
      <div class="panel pad">
        <div class="chart-title">水位 · 按代理</div>
        <div v-if="waterRows.length" class="water-list">
          <div v-for="r in waterShown" :key="r.agent_id" class="water-row">
            <div class="water-head">
              <span class="water-name" :title="r.agent_id">{{ r.label }}</span>
              <span class="water-pct num" :class="r.toneClass">{{ r.percent }}%</span>
              <span class="water-num num">{{ r.chars }} / {{ r.budget }} 字符</span>
            </div>
            <div class="water-track">
              <i :class="r.toneClass" :style="{ width: r.width + '%' }"></i>
            </div>
          </div>
          <button
            v-if="waterRest.length"
            type="button"
            class="ghost-btn water-more"
            @click="waterRestOpen = !waterRestOpen"
          >
            {{ waterRestOpen ? '收起其余代理' : `其余 ${waterRest.length} 个代理（最高 ${waterRest[0].percent}%）` }}
          </button>
        </div>
        <div v-else class="empty">
          <b>没有可分列的代理水位</b>
          <span>常驻层里还没有带代理归属的条目。</span>
        </div>
        <!-- 图例压成**条组下方一行灰字**（阈值就在条上，不必另开一块地方说） -->
        <p class="water-legend">≥90% 走警示色 · ≥100% 走危险色</p>
        <p class="src">{{ waterHint }}</p>
      </div>

      <!-- 待办完成率：**全局总览的环**（缩小居中）+ 环下计数小字 +
           **按代理小条组**（谁完成得好，一行一个代理）。
           🔴 数字全部来自后端 `by_agent` 段（前端不自己数、不自己算比例）。 -->
      <div class="panel pad">
        <div class="chart-title">待办完成率</div>
        <div class="gauge-wrap">
          <RingGauge :metrics="gaugeMetrics" :height="gaugeHeight" />
          <p class="gauge-count num">已销掉 {{ todoClosed }} / 全部 {{ todoSummary.total }} 条</p>
        </div>
        <div v-if="todoAgentRows.length" class="todo-agents">
          <div v-for="r in todoAgentShown" :key="r.key" class="todo-agent-row">
            <div class="todo-agent-head">
              <span class="todo-agent-name" :title="r.agent_id">{{ r.agent }}</span>
              <span class="todo-agent-pct num">{{ r.ratio }}%</span>
            </div>
            <div class="todo-agent-track">
              <i :class="r.tone" :style="{ width: r.width + '%' }"></i>
            </div>
          </div>
          <button
            v-if="todoAgentRest.length"
            type="button"
            class="ghost-btn water-more"
            @click="todoAgentRestOpen = !todoAgentRestOpen"
          >
            {{ todoAgentRestOpen ? '收起其余代理' : `其余 ${todoAgentRest.length} 个代理（最高 ${todoAgentRest[0].ratio}%）` }}
          </button>
        </div>
        <div v-else class="todo-agents-empty">
          <span>没有可分列的代理待办 —— 这些待办还没记到代理名下。</span>
        </div>
        <p class="src">{{ gaugeHint }}</p>
      </div>

      <!-- 近 7 天记忆增量：**一张图** = 按代理堆叠柱 + 柱顶当日总量数字
           （原先「总量折线小图」已删 —— 同一件事不画两遍）。
           🔴 横轴 = `daily` 段（后端补齐了每一天），色带 = `daily_by_agent` 段
           （只给有写入的格子）—— 横轴只补一次（后端补一次、前端再补一次必漂）。
           序列 = 7 天总量降序前 6 + 「其他」桶；数字全部来自接口，零写死。
           悬停**整列**（不必精准点到某一段）出浮层：日期 / 当日总量 / 各代理分项。
           内联 SVG + 绝对定位浮层 —— 不引任何新依赖。 -->
      <div class="panel pad">
        <div class="chart-title">近 7 天记忆增量</div>
        <div class="chart-wrap" @mouseleave="hoverDay = ''">
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
            <!-- 柱顶：当日总量（数字来自接口，零写死） -->
            <g class="totals">
              <text
                v-for="c in agentChart.cols"
                :key="`n${c.day}`"
                v-show="c.total > 0"
                :x="c.cx"
                :y="c.topY - 5"
                text-anchor="middle"
              >{{ c.total }}</text>
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
            <!-- 整列命中：透明 rect 逐日捕获 mouseenter（不必精准点到某一段） -->
            <g class="hit">
              <rect
                v-for="c in agentChart.cols"
                :key="`h${c.day}`"
                :x="c.hitX"
                :y="agentChart.padT"
                :width="c.hitW"
                :height="agentChart.plotH"
                fill="transparent"
                @mouseenter="hoverDay = c.day"
              />
            </g>
          </svg>
          <div v-else class="empty">
            <b>近 7 天没有可分列的增量</b>
            <span>这段时间没有新增的记忆条目（接口没给到任何按代理的格子）。</span>
          </div>
          <!-- 悬停浮层：绝对定位在柱子旁边（左右自动翻转 ⇒ 永不出图） -->
          <div v-if="hoverTip" class="chart-tip" :style="hoverTip.style" role="tooltip">
            <div class="tip-head">
              <span class="tip-date">{{ hoverTip.date }}</span>
              <span class="num tip-total">{{ hoverTip.total }} 条</span>
            </div>
            <ul v-if="hoverTip.parts.length" class="tip-list">
              <li v-for="p in hoverTip.parts" :key="p.key">
                <span class="dot" :style="{ background: p.color }"></span>
                <span class="tip-name">{{ p.label }}</span>
                <span class="num tip-num">{{ p.count }}</span>
              </li>
            </ul>
            <p v-else class="tip-empty">当天没有新增</p>
          </div>
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

    <!-- 调度展示面已摘除（dashboard_bus_block_remove）：班车调度与执行的单一
         事实源是「定时任务」页，看板只留一行指路。两处各写一份文案必漂，
         且旧文案里那个脚本路径早就不是真相了。 -->
    <p class="src cron-hint">
      班车调度与执行统一在「定时任务」页管理
      <router-link class="card-link" to="/scheduler">→ 去定时任务 ›</router-link>
    </p>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import AgentUsageBars from '../components/AgentUsageBars.vue'
import RingGauge from '../components/RingGauge.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { agentUsageRows, isOnline, loadAgentRows, ONLINE_HINT } from '../utils/agents'
import { chartPalette, completionColor } from '../utils/chart-theme.js'
import { bucketByAgent, dayBreakdown, OTHER_KEY, stackLayout, topSeries } from '../utils/daily-by-agent'
import { toLocal } from '../utils/format'
import { completionPercent, todoAgentRows as todoAgentRowsOf } from '../utils/todo'

const data = ref(null)
const error = ref('')
const tiering = ref(null)
const tieringError = ref('')

// 待办小节（2026-10 ks_todo）：计数**全部由后端给**（前端不自己数）。
const todoSummary = ref({ open: 0, done: 0, obsolete: 0, total: 0, overdue: 0, by_agent: [] })
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
/** 完成率环：**缩小**（环下面还要摆按代理的条组，环不许把那张卡撑高）。 */
const gaugeHeight = computed(() => (chartWidth.value < 900 ? 140 : 160))

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
// 0 时也展示卡面：徽标是「记得处理一下」的提示，无事也让人安心。
const router = useRouter()
function gotoReview() {
  // 整页只有这一个 chip；点哪都跳同一个目标：/review（晋升 + 退居都在那）。
  router.push('/review')
}

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

/* ---------------------------------------------------------------- 近 7 天增量图
 * `daily_by_agent`（`GET /api/v1/stats?days=7` 新增的一段）= 每日增量**按代理分列**
 * 的格子（`{d, agent, n}`，**只给有写入的格子**）。横轴用 `daily` 段（后端补齐了
 * 每一天），色带用这一段 —— 横轴是图表口径，只补一次（后端补一次、前端补一次必漂）。
 * 数值全部来自接口 —— 前端零写死。
 *
 * 🔴 图表是**内联 SVG**（不是 ECharts）：SVG 里的 `var(--*)` 直接吃 CSS 变量，
 *    切主题不用重画（canvas 那套得订阅主题变化再重画一次，见 components/RingGauge.vue）。
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

/** 按代理分桶（`daily_by_agent` 汇总）—— 堆叠柱与悬停浮层共用这一份。 */
const agentBuckets = computed(() => bucketByAgent(data.value?.daily_by_agent || []))

/** 序列 = 7 天总量降序前 6 + 「其他」桶（其余代理合并进去）。
 *  分组 / 排序 / 归并 / 摆位置都在 `utils/daily-by-agent.js`（纯函数，可单测）。 */
const agentSeries = computed(() => topSeries(agentBuckets.value, DAILY_TOP_N, agentLabel))

/** 内联 SVG 的画布尺寸（viewBox 坐标系，与实际像素解耦 ⇒ 窄屏不横向滚动）。
 *  🔴 padT 留出 22px：柱顶要写当日总量数字（数字压在柱顶上会糊）。 */
const CHART = { w: 720, h: 240, padL: 38, padR: 12, padT: 22, padB: 30 }

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

/** 悬停浮层：当前悬停的那天（整列命中，不必精准点到某一段）。 */
const hoverDay = ref('')

/** 代理 → 柱子里那条色带（浮层里的色点跟柱子同色，视线不用来回找）。
 *  被归并进「其他」桶的代理没有自己的色带 ⇒ 用「其他」那档中性灰。 */
function seriesColorOf(agent) {
  const s = agentChart.value.series.find((x) => x.key === agent)
  return s && s.key !== OTHER_KEY ? s.color : OTHER_COLOR
}

/**
 * 浮层内容 + 落位：日期 / 当日总量 / 各代理分项（按条数降序，含「未标注」桶）。
 *
 * 🔴 落位 = 挂在柱子**旁边**（左半屏挂右、右半屏挂左 ⇒ 永不出图）：
 *    浮层宽度固定，左半边放右边、右半边放左边，两种情况都塞得进图里。
 */
const hoverTip = computed(() => {
  const day = hoverDay.value
  if (!day) return null
  const col = agentChart.value.cols.find((c) => c.day === day)
  if (!col) return null
  const info = dayBreakdown(agentBuckets.value, day, agentLabel)
  const flip = col.cx > CHART.w / 2
  return {
    date: day,
    total: info.total,
    parts: info.parts.map((p) => ({ ...p, color: seriesColorOf(p.key) })),
    style: {
      left: `${((flip ? col.cx - 8 : col.cx + 8) / CHART.w) * 100}%`,
      top: `${(CHART.padT / CHART.h) * 100}%`,
      transform: flip ? 'translate(-100%, 0)' : 'translate(0, 0)',
    },
  }
})

const dailyByAgentHint = computed(() => {
  const c = agentChart.value
  if (!c.series.length) return '接口没有给到任何按代理的增量格子（库里这 7 天没有新增条目）。'
  const other = c.series.find((s) => s.key === OTHER_KEY)
  return `近 7 天新增 ${c.total} 条，按代理分 ${c.series.length} 个序列`
    + (other ? `（「其他」= ${other.restCount} 个代理合并，共 ${other.total} 条）` : '')
    + '；没打归属标签的老条目归「未标注」一栏，不摊到别人头上。'
})

/* ---------------------------------------------------------------- 按代理水位
 * 水位**按代理**看：每个代理一份独立的注入预算（`core_budget_chars` ?? 全局），
 * 总量环在几百代理规模下没有信息量 —— 要看的是「谁的条快满了」。
 *
 * 🔴 口径与上面那张 ECharts 分列图**同一把尺子**（同一个 `agentUsageRows`）：
 *   占比 = 该代理常驻字符 ÷ **它自己**生效的预算（不是占全库总量 ——
 *   那另一个口径在「记忆维护」页，写在那一页的说明行里）。
 * 🔴 数字全部来自接口，前端零写死。 */

/** 水位条组最多列几个（其余折叠成一行「其余 N 个」）。 */
const WATER_TOP_N = 8

/** 水位行：按占用降序；≥90% 警示色、≥100% 危险色。 */
const waterRows = computed(() =>
  coreAgentCards.value
    .filter((r) => r.chars > 0)          // 没占常驻层的代理没有水位可言
    .slice()
    .sort((a, b) => b.percent - a.percent)
    .map((r) => ({
      ...r,
      width: Math.min(100, Math.max(0, r.percent)),   // 条长封顶 100%（超预算另走危险色）
      toneClass: r.percent >= 100 ? 'is-danger' : (r.percent >= 90 ? 'is-warn' : ''),
    })),
)

/** 折叠：前 8 个 + 「其余 N 个」（点开才列全）。 */
const waterTop = computed(() => waterRows.value.slice(0, WATER_TOP_N))
const waterRest = computed(() => waterRows.value.slice(WATER_TOP_N))
const waterRestOpen = ref(false)
const waterShown = computed(() => (waterRestOpen.value ? waterRows.value : waterTop.value))

const waterHint = '占比 = 该代理的常驻字符 ÷ 它自己生效的注入预算；'
  + '只列有常驻条目的代理，其余折叠在「其余 N 个」里。'

/** 待办完成率（%）—— 已销掉（done + obsolete）占全部（口径写在 hint 里）。 */
const todoPercent = computed(() => completionPercent(todoSummary.value))

/**
 * 已销掉的条数（done + obsolete）—— 环下面那行 `已销掉 N / 全部 M` 的 N。
 * 🔴 数字全部来自 `todoSummary`（后端给的），前端不自己数。
 */
const todoClosed = computed(() =>
    Number(todoSummary.value.done || 0) + Number(todoSummary.value.obsolete || 0))

/** 环形仪表只剩一块：待办完成率（全局单一指标，环形合理）。 */
const gaugeMetrics = computed(() => {
  const p = chartPalette()
  return [{
    label: '待办完成率',
    percent: todoPercent.value,
    // 🔴 **完成率用 `completionColor`**，不用 `usageColor` —— 后者是**占用**
    //    色档（越高越危险），会把「100% 全销掉」画成红色（用户实锤过的 bug）。
    color: completionColor(todoPercent.value, p),
  }]
})

const gaugeHint = computed(() =>
  `待办完成率 ${todoPercent.value}%（已销掉的 done + obsolete ÷ 全部）`)

/* ---------------------------------------------------------------- 待办按代理分列
 * 「谁完成得好」要看**按代理**的完成率（全局一个 97.56% 看不出是谁拖后腿）。
 * 🔴 数字**全部来自后端** `by_agent` 段（一条 GROUP BY 聚合出来的）——
 *    前端不自己数、不自己算比例（口径两处各算一次必然漂）。
 *    `agent` 是注册表里的显示名；`-` 桶 = 「未标注」（owner 查不到注册表）。 */

/** 小条组最多列几个（其余折叠成一行「其余 N 个」）。 */
const TODO_AGENT_TOP_N = 6

/** 按代理的待办完成率行（按完成率降序；条长封顶 100%）。 */
const todoAgentRows = computed(() => todoAgentRowsOf(todoSummary.value.by_agent))

/** 折叠：前 6 个 + 「其余 N 个」（点开才列全）。 */
const todoAgentTop = computed(() => todoAgentRows.value.slice(0, TODO_AGENT_TOP_N))
const todoAgentRest = computed(() => todoAgentRows.value.slice(TODO_AGENT_TOP_N))
const todoAgentRestOpen = ref(false)
const todoAgentShown = computed(() =>
  (todoAgentRestOpen.value ? todoAgentRows.value : todoAgentTop.value))

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
      // 🆕 按代理分列（后端给的聚合结果；老后端没这一段 ⇒ 空数组，页面不显示条组）
      by_agent: Array.isArray(s?.by_agent) ? s.by_agent : [],
    }
    todoError.value = ''
  } catch (e) {
    todoSummary.value = { open: 0, done: 0, obsolete: 0, total: 0, overdue: 0, by_agent: [] }
    todoError.value = `读取待办计数失败：${e.message}`
  }
}

/** 进待办页（待办小节的卡点哪儿都跳同一个地方）。 */
function gotoTodos() {
  router.push('/todos')
}

async function load() {
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
  // 分级班车状态单独读：它挂了就挂这一段，不许把上面两张卡一起拖成报错
  try {
    tiering.value = await api.tieringStatus()
    tieringError.value = ''
  } catch (e) {
    tiering.value = null
    tieringError.value = `读取分级班车状态失败：${e.message}`
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
  /* 🔴 三卡**等高**（默认的 stretch）：条组 / 环 / 堆叠柱三块各按自己的内容长，
     不拉齐就会一高一低、留白难看。卡内改成 flex 列 ⇒ 内容自己往下排。 */
  align-items: stretch;
  gap: 14px;
}
/* 卡内布局：标题 / 主体 / 脚注竖着排，卡内区块间距 12px（DESIGN.md 那一档） */
.chart-grid > .panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.chart-grid .chart-title {
  margin-bottom: 0;
}
/* 脚注推到卡底（`margin-top: auto` 吃掉多余留白）—— 三卡的脚注在一条线上 */
.chart-grid .src {
  margin-top: auto;
}
/* 水位图例：条组下方**一行灰字**（阈值就在条上，不必另开一块地方说） */
.water-legend {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
/* 完成率那张卡：环（缩小）+ 计数小字竖着居中 */
.gauge-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.gauge-count {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
}
/* 按代理的待办完成率条组（与水位条组同一把尺子：名字 + 百分比 + 细条） */
.todo-agents {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
/* 空态：矮卡片 + 一句灰字（不留 44px padding 的大空区） */
.todo-agents-empty {
  padding: 12px 0;
  color: var(--text-faint);
  font-size: 13px;
  line-height: 1.7;
}
.todo-agent-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.todo-agent-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.todo-agent-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: 13px;
}
.todo-agent-pct {
  flex: none;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
}
.todo-agent-track {
  height: 6px;
  border-radius: var(--r-chip);
  background: var(--chip);
  overflow: hidden;
}
.todo-agent-track i {
  display: block;
  height: 100%;
  border-radius: var(--r-chip);
  background: var(--accent);
}
/* 🔴 100% 的代理条走**绿**（`--ok`）—— 全销掉了。
   其余（0 ~ 100 之间）维持蓝（上面的默认档）：小条组是满屏多行，
   逐行标红会变成「满屏红」，看不出谁是谁。 */
.todo-agent-track i.is-ok {
  background: var(--ok);
}
/* ---------------------------------------------------------------- 按代理水位条组
 * 每代理一行：名字 + 百分比 + 细条 + 「字符 / 预算」。
 * 内联 CSS 条（零新依赖 —— 本页不引 ECharts）；颜色全走 tokens.css 的既有令牌。 */
.water-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.water-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.water-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.water-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: 13px;
}
.water-pct {
  flex: none;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
}
.water-pct.is-warn {
  color: var(--warn);
}
.water-pct.is-danger {
  color: var(--danger);
}
.water-num {
  flex: none;
  color: var(--text-faint);
  font-size: 12px;
}
/* 细条：6px 高 + chip 圆角（令牌圆角，不写裸 px） */
.water-track {
  height: 6px;
  border-radius: var(--r-chip);
  background: var(--chip);
  overflow: hidden;
}
.water-track i {
  display: block;
  height: 100%;
  border-radius: var(--r-chip);
  background: var(--accent);
}
.water-track i.is-warn {
  background: var(--warn);
}
.water-track i.is-danger {
  background: var(--danger);
}
.water-more {
  align-self: flex-start;
  height: 28px;
  font-size: 12px;
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
/* ---------------------------------------------------------------- 面板内边距
 * 面板的通用内边距（scoped 一份，纯排版）。 */
.pad {
  padding: 20px;
}

.src {
  margin-top: 12px;
  color: var(--text-faint);
  font-size: 12px;
}

/* 调度块的指路行：链接沿用全局 .card-link（不新造颜色），
   只去掉 <a> 的默认下划线（跟记忆维护页那两行指路同款）。 */
.cron-hint a {
  text-decoration: none;
}

/* ---------------------------------------------------------------- 按代理堆叠柱
 * 内联 SVG：viewBox 720×240 + `width:100%` ⇒ 窄屏不横向滚动；
 * `max-width` 封顶（宽屏上别把一张图拉成 1500px 宽的稀疏柱阵）。
 * 颜色全在 :fill 里给 var(--*)，CSS 变量直接生效 ⇒ 切主题不用重画。 */
.chart-wrap {
  position: relative;      /* 浮层按柱子横向跟随（绝对定位的参照） */
  max-width: 760px;        /* 与 svg 同宽 ⇒ 浮层的百分比落位与图内坐标对齐 */
  /* 🔴 右卡与左中两卡**等高**：这块占满卡里剩下的高度，柱子**垂直居中**
     （svg 的 viewBox 保持比例 ⇒ 拉高盒子不会把图拉变形）。 */
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  min-height: 0;
}
.stack-chart {
  display: block;
  width: 100%;
  max-width: 100%;
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
/* 柱顶当日总量：弱一档的字（它压着柱子，不必抢眼） */
.stack-chart .totals text {
  fill: var(--text-dim);
  font-size: 10px;
  font-family: var(--font);
  font-variant-numeric: tabular-nums;
}
/* 整列命中区：透明 rect（只吃鼠标事件，不画东西） */
.stack-chart .hit rect {
  cursor: crosshair;
}

/* 悬停浮层：白底细边框圆角（DESIGN.md：靠边框分层，不靠阴影堆）。
   高度封顶 + 内部滚动：代理多的时候不让浮层长到把图压塌。 */
.chart-tip {
  position: absolute;
  z-index: 5;
  width: 180px;
  max-height: 150px;
  overflow-y: auto;
  padding: 8px 10px;
  background: var(--bg-elev);
  border: 1px solid var(--border-2);
  border-radius: var(--r-inset);
  box-shadow: var(--shadow-pop);
  font-size: 12px;
  color: var(--text-dim);
}
.tip-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 6px;
  margin-bottom: 6px;
  border-bottom: 1px solid var(--line-3);
}
.tip-date {
  color: var(--text);
  font-weight: 600;
}
.tip-total {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.tip-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.tip-list li {
  display: flex;
  align-items: center;
  gap: 6px;
}
.tip-list .dot {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  flex: none;
}
.tip-name {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tip-num {
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.tip-empty {
  margin: 0;
  color: var(--text-faint);
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

</style>
