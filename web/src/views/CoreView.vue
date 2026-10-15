<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">常驻层</h1>
      <p class="page-sub">带 core 标签的记忆条目，每轮 system_prompt 全量注入，占用注入预算。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="条数" :value="stats?.count ?? '-'" unit="条" />
      <StatCard label="正文字符" :value="stats?.chars ?? '-'" unit="字符" />
      <StatCard label="注入预算" :value="stats?.budget ?? '-'" unit="字符" />
      <!-- 包一层：StatCard 的 hint 是纯字符串 prop，放不下可点的「设置」入口，
           而 StatCard.vue 不在本次改动范围内。卡撑满格高，入口贴在卡下沿。 -->
      <div class="budget-cell">
        <StatCard
          label="预算占用"
          :value="stats ? `${stats.percent}%` : '-'"
          :percent="stats ? stats.percent : null"
          :tone="budgetTone"
          :hint="stats && stats.percent >= 100 ? '已超预算，注入时会截断' : ''"
        />
        <button class="soft-btn" @click="openBudget">设置注入预算…</button>
      </div>
    </div>

    <div class="filter-panel">
      <div class="filter-title">常驻记忆</div>
      <p class="filter-desc">
        卡片给 3 行摘要，悬停看完整正文；点 key 旁的图标复制，「查看全文 ↗」进抽屉看全文与编辑。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="搜索常驻正文或标签（清空回全量）"
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
        <span>共 {{ count }} 条匹配 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <button class="soft-btn" :disabled="!dirty" @click="onClear">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="load">刷新</button>
          <button class="ghost-btn" :disabled="loading" @click="doExport('jsonl')">导出 JSONL</button>
          <button class="ghost-btn" :disabled="loading" @click="doExport('csv')">导出 CSV</button>
          <el-button @click="openImport">导入</el-button>
        </div>
      </div>
    </div>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!items.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M12 3.5 3.5 7.5 12 11.5l8.5-4-8.5-4Z" stroke-linejoin="round" />
          <path d="M4.6 12 12 15.6 19.4 12M4.6 16.5 12 20l7.4-3.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p v-if="dirty"><b>没匹配到常驻记忆</b>换个更短的核心词试试，或点「清除筛选」回全量常驻层。</p>
        <p v-else><b>常驻层还是空的</b>到「记忆条目」把要每轮注入的记忆点「升常驻」，它们就会出现在这里。</p>
      </div>
    </div>

    <div v-else class="card-grid">
      <ItemCard v-for="row in items" :key="row.key" chip-tone="accent" @lead="open(row)">
        <template #tag>core</template>
        <template #time>{{ toLocal(row.updated_at) }}</template>

        <span class="cell-key">
          <span class="k" :title="row.key">{{ row.key }}</span>
          <button class="copy-btn" title="复制 key" @click.stop="doCopy(row.key)">
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

        <template #lead>查看全文 ↗</template>
        <template #actions>
          <button class="act-btn act-btn--danger" @click="demote(row)">降常驻</button>
        </template>
      </ItemCard>
    </div>

    <PagerBar
      :total="count"
      :page="page"
      :page-size="pageSize"
      @update:page="onPage"
      @update:page-size="onSize"
    />

    <DetailDrawer
      v-model="drawer"
      :detail="detail"
      :allow-archive="false"
      @saved="load"
    />

    <!-- 设置注入预算：数字 + 手输当前生效值 confirm（改的是全局阈值，回退影响每轮注入） -->
    <el-dialog
      v-model="budgeting"
      title="设置常驻层注入预算"
      width="460px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyBudget"
      :show-close="!busyBudget"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="注入预算（字符，可设 1000 ~ 500000）">
          <el-input v-model="draftBudget" type="number" :min="1000" :max="500000" step="1000" />
        </el-form-item>
        <el-form-item :label="`手输当前生效值 ${stats?.budget ?? '-'} 以确认`">
          <el-input v-model="draftConfirm" placeholder="见上方「注入预算」卡片" />
        </el-form-item>
      </el-form>
      <p class="tip">
        预算即每轮 system_prompt 注入常驻层的字符上限，超出的老条目从尾部截断（并标注截断）。
        改完立刻热生效，不必重启服务。
      </p>
      <template #footer>
        <el-button :disabled="busyBudget" @click="budgeting = false">取消</el-button>
        <el-button type="primary" :loading="busyBudget" :disabled="!canSaveBudget" @click="saveBudget">
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- 常驻层导入：先解析 → 预演（零写入）回显计数 → 手输 N 才实跑；默认「导入即升常驻」 -->
    <el-dialog
      v-model="importing"
      title="导入常驻记忆（JSONL）"
      width="560px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyPlan && !busyRun"
      :show-close="!busyPlan && !busyRun"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item>
          <span class="fld">
            每行一条 {content, tags?}
            <a class="tpl-link" href="#" @click.prevent="getTemplate">下载模板</a>
          </span>
          <el-input
            v-model="importText"
            type="textarea"
            :rows="7"
            placeholder='{"content": "这条常驻记忆是什么", "tags": "ops"}'
          />
        </el-form-item>
        <el-form-item>
          <!-- 改 promote 就要作废已有预演：would_promote 是按 promote 值算出来的，
               拿着上一次的数去实跑，弹框里「其中 N 条会升常驻」会与实际不符 -->
          <el-checkbox v-model="importPromote" @change="importPlan = null">
            导入即升常驻（每条落库后打 core 标签 —— 与在记忆条目页逐条点「升常驻」同一效果）
          </el-checkbox>
        </el-form-item>
      </el-form>
      <p v-if="importParse.broken" class="banner banner--danger">
        有 {{ importParse.broken }} 行不是合法的 {"content": ...}，未计入下方条目 —— 不静默丢行，请修正后重试。
      </p>
      <p v-else-if="importParse.items.length" class="tip">
        已解析 {{ importParse.items.length }} 条，点「预演」后端会按内容去重、剥治理标签，并回显将新增多少条。
      </p>
      <p v-if="importPlan" class="tip">
        预演结果：将新增 {{ importPlan.would_add }} 条
        <template v-if="importPromote">（含升常驻 {{ importPlan.would_promote ?? 0 }} 条）</template>
        · 跳过重复 {{ importPlan.skipped_duplicate }} 条 · 拒收治理标签 {{ importPlan.rejected_reserved }} 条
        · 空正文 {{ importPlan.rejected_empty }} 条（共 {{ importPlan.total }} 条）。
      </p>
      <template #footer>
        <el-button :disabled="busyPlan || busyRun" @click="importing = false">取消</el-button>
        <el-button :loading="busyPlan" :disabled="busyRun || !importParse.items.length" @click="planImport">
          预演（不写入）
        </el-button>
        <el-button
          type="primary"
          :loading="busyRun"
          :disabled="busyPlan || !importPlan || !importPlan.would_add"
          @click="importRunAsk = true"
        >
          实跑导入
        </el-button>
      </template>
    </el-dialog>

    <!-- 实跑确认：expect = 预演给出的「将新增 N 条」，与记忆条目页同一口径 -->
    <ConfirmDialog
      v-model="importRunAsk"
      title="按预演结果实跑导入？"
      :consequences="importConsequences"
      :expect="importExpect"
      confirm-text="确认导入"
      :busy="busyRun"
      tone="primary"
      @confirm="doImportRun"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DetailDrawer from '../components/DetailDrawer.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showConfirm, showError, showToast } from '../utils/dialog'
import { downloadFile, downloadTemplate } from '../utils/export'
import { toLocal } from '../utils/format'
import { isStale } from '../utils/kb'

const q = ref('')
const items = ref([])
const stats = ref(null)
const count = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')
const drawer = ref(false)
const detail = ref(null)

// ---- 设置注入预算 ----
const budgeting = ref(false)
const busyBudget = ref(false)
const draftBudget = ref('')
const draftConfirm = ref('')
/** 与后端同一区间（1000~500000）；confirm 必须逐字等于当前生效值 */
const canSaveBudget = computed(() => {
  const n = Number(draftBudget.value)
  return Number.isInteger(n) && n >= 1000 && n <= 500000
    && String(draftConfirm.value).trim() === String(stats.value?.budget ?? '')
})

// ---- 常驻层导入 ----
const importing = ref(false)
const importText = ref('')
const importParse = reactive({ items: [], broken: 0 })
const importPlan = ref(null)
const busyPlan = ref(false)
const busyRun = ref(false)
const importPromote = ref(true)     // 常驻层页的导入，默认就是「导入即升常驻」
const importRunAsk = ref(false)
const importExpect = computed(() => (importPlan.value ? String(importPlan.value.would_add) : ''))
const importConsequences = computed(() =>
  importPlan.value
    ? [
        `将写入 ${importPlan.value.would_add} 条新常驻记忆（重复 ${importPlan.value.skipped_duplicate} 条会跳过，不覆盖旧条目）。`,
        importPromote.value
          ? `其中 ${importPlan.value.would_promote ?? 0} 条会同时打上 core 标签，之后每轮都注入。`
          : '未勾「导入即升常驻」：这些条目只进检索层，不会被注入。',
        `请照抄「${importExpect.value}」确认 —— 这个数就是预演出来的将新增条数，写进去一条都不会多。`,
      ]
    : [],
)

/** 有搜索词在生效吗：空态分支与「清除筛选」按钮都看它 */
const dirty = computed(() => !!(q.value || '').trim())

/** 占用越高越红：80% 起转蓝（与主色同一套），过 100% 红 —— 超预算注入会截断。 */
const budgetTone = computed(() => {
  const p = stats.value?.percent
  if (p === undefined || p === null) return ''
  if (p >= 100) return 'danger'
  if (p >= 80) return 'accent'
  return 'ok'
})

/** tags 是逗号分隔的一串，拆成一个个 chip 展示 */
function splitTags(s) {
  return (s || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

// 请求序号：连续改词时旧响应只丢弃，不覆盖新结果（照抄 KbView 的收口口径）
let reqSeq = 0

async function load() {
  const seq = ++reqSeq
  loading.value = true
  error.value = ''
  try {
    const [list, st] = await Promise.all([
      // q 只透传，匹配语义在服务端（正文+标签子串，大小写不敏感）
      api.coreList({
        q: (q.value || '').trim() || undefined,
        page: page.value,
        page_size: pageSize.value,
      }),
      api.coreStats(),
    ])
    if (isStale(seq, reqSeq)) return
    items.value = list.items
    count.value = list.count
    stats.value = st
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    error.value = `读取常驻层失败：${e.message}`
    ElMessage.error(error.value)
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}
/** 清空 ✕ / 「清除筛选」：无条件回全量常驻层（不是空态），与 KbView 的清空口径一致 */
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

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** 打开全文抽屉：详情按 key 单查，与记忆条目页同一个接口。 */
async function open(row) {
  try {
    detail.value = await api.fragment(row.key)
    drawer.value = true
  } catch (e) {
    showError(`读取详情失败：${e.message}`)
  }
}

/** 降常驻：去掉 core 标签（不再每轮注入）。二次确认 + 成功后刷新 + toast。 */
async function demote(row) {
  const ok = await showConfirm(
    `确认把 ${row.key} 移出常驻层？之后每轮不再注入这条记忆（写前会自动备份，可回滚）。`,
    '降常驻',
  )
  if (!ok) return
  try {
    const r = await api.demote(row.key)
    showToast(`已降常驻：${row.key}（tags=${r.tags || '空'}）`)
    load()
  } catch (e) {
    showError(`降常驻失败：${e.message}`)
  }
}

// ---- 设置注入预算 ----
/** 打开时把当前生效值预填进输入框，confirm 留空手输（防手滑：改全局阈值） */
function openBudget() {
  draftBudget.value = stats.value?.budget ?? 60000
  draftConfirm.value = ''
  budgeting.value = true
}

async function saveBudget() {
  if (!canSaveBudget.value) return
  busyBudget.value = true
  try {
    const r = await api.setCoreBudget(Number(draftBudget.value), draftConfirm.value.trim())
    budgeting.value = false
    showToast(`已设置注入预算：${r.budget} 字符（写入 ${r.path}）`)
    load()
  } catch (e) {
    showError(`设置预算失败：${e.message}`)
  } finally {
    busyBudget.value = false
  }
}

// ---- 导出 ----
/**
 * 常驻层导出：固定 `?tag=core`（**不带 q**）—— 导出的必须是「升常驻的那批」本身，
 * 不受页面上那个搜索词影响；搜索词只管屏幕上这页列表。
 */
async function doExport(fmt) {
  try {
    const name = await downloadFile(`/api/v1/export?tag=core&format=${fmt}`, `keepsake-core.${fmt}`)
    showToast(`导出已开始下载：${name}`)
  } catch (e) {
    showError(`导出失败：${e.message}`)
  }
}

// ---- 导入 ----
/** JSONL 逐行解析成 items；坏行**计数返回**，绝不静默丢（与记忆条目页同一口径） */
function parseJsonl(text) {
  const items = []
  let broken = 0
  for (const line of (text || '').split('\n')) {
    const s = line.trim()
    if (!s) continue
    try {
      const o = JSON.parse(s)
      if (o && typeof o.content === 'string') items.push({ content: o.content, tags: o.tags || '' })
      else broken += 1
    } catch {
      broken += 1
    }
  }
  return { items, broken }
}

function openImport() {
  importText.value = ''
  importParse.items = []
  importParse.broken = 0
  importPlan.value = null
  importing.value = true
}

/** 模板下载走 fetch+blob（端点要 Authorization 头，裸 href 会把 token 丢给浏览器历史） */
async function getTemplate() {
  try {
    await downloadTemplate('core')
    showToast('已下载导入模板（keepsake-import-template-core.jsonl）')
  } catch (e) {
    showError(`下载模板失败：${e.message}`)
  }
}

function currentItems() {
  const p = parseJsonl(importText.value)
  importParse.items = p.items
  importParse.broken = p.broken
  return p.items
}

/** 预演：后端零写入，回显「将新增 / 其中升常驻 / 跳过重复 / 拒收」 */
async function planImport() {
  busyPlan.value = true
  try {
    importPlan.value = await api.importFragments({
      items: currentItems(),
      dry_run: true,
      promote: importPromote.value,
    })
  } catch (e) {
    showError(`导入预演失败：${e.message}`)
  } finally {
    busyPlan.value = false
  }
}

async function doImportRun(typed) {
  busyRun.value = true
  try {
    const r = await api.importFragments({
      items: importParse.items,
      dry_run: false,
      confirm: typed,
      promote: importPromote.value,
    })
    importRunAsk.value = false
    importing.value = false
    showToast(
      `导入完成：新增 ${r.would_add} 条 · 升常驻 ${r.promoted} 条 · 跳过重复 ${r.skipped_duplicate} 条`,
    )
    load()
  } catch (e) {
    showError(`导入失败：${e.message}`)
  } finally {
    busyRun.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 预算格：卡 + 「设置」入口同一列（StatCard 的 hint 是纯字符串，放不下按钮） */
.budget-cell {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.budget-cell :deep(.stat) {
  flex: 1;
}
/* 字段名旁的「下载模板」：和正文标题同一行，不另起一行抢视线 */
.fld {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 6px;
}
.tpl-link {
  color: var(--accent);
  font-size: 12px;
  text-decoration: none;
}
.tpl-link:hover {
  text-decoration: underline;
}
/* 弹框里的说明行：与 FragmentsView 的 .tip 同一份配方（scoped 跨组件不生效，
   只能各写一份，数值不许自创） */
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
</style>
