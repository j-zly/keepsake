<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">记忆条目</h1>
      <p class="page-sub">全库记忆条目，按 BM25 全文检索召回；点卡片进抽屉看全文与编辑。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="filter-panel">
      <div class="filter-title">检索记忆条目</div>
      <p class="filter-desc">正文走 BM25 全文检索（keepsake storage.search）；留空即全库浏览。</p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="搜索正文（BM25 全文检索）"
          clearable
          @keyup.enter="search"
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
        <el-select
          v-model="tag"
          class="sel"
          placeholder="标签"
          filterable
          allow-create
          default-first-option
          clearable
        />
        <el-button type="primary" :loading="loading" @click="search">查询</el-button>
      </div>
      <div class="filter-result">
        <span>共 {{ count === null ? '—' : count }} 条匹配 · 每页 {{ pageSize }}</span>
        <div class="acts">
          <!-- 清除筛选 = 清空 q/tag 再查；重置 = 保留条件、回到第 1 页重查 -->
          <button class="soft-btn" :disabled="!dirty" @click="reset">清除筛选</button>
          <button class="ghost-btn" :disabled="loading" @click="search">重置</button>
          <!-- 导出沿用当前 q/tag 筛选；格式只影响文件后缀与内容 -->
          <el-select v-model="exportFormat" class="fmt" title="导出格式">
            <el-option label="JSONL" value="jsonl" />
            <el-option label="CSV" value="csv" />
          </el-select>
          <el-button :loading="busyExport" @click="doExport">导出</el-button>
          <el-button @click="openImport">导入</el-button>
          <el-button type="primary" @click="openCreate">新增条目</el-button>
        </div>
      </div>
    </div>

    <div v-if="count === null && !items.length" class="banner">
      当前是全库浏览：存储层没有 count 原语，总条数未知，按 has_more 翻页。
    </div>

    <div v-if="loading && !items.length" class="panel pad">
      <el-skeleton :rows="5" animated />
    </div>

    <div v-else-if="!items.length" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <circle cx="10.5" cy="10.5" r="6" />
          <path d="m15 15 4.5 4.5" stroke-linecap="round" />
        </svg>
        <p v-if="dirty"><b>没召回到记忆条目</b>换个词试试，或把标签清空做全库浏览。</p>
        <p v-else><b>这里什么都没有</b>上方输入关键词召回，或直接翻页浏览全库。</p>
      </div>
    </div>

    <div v-else class="card-grid">
      <ItemCard v-for="row in items" :key="row.key" chip-tone="accent" @lead="open(row)">
        <template #tag>score {{ fmtScore(row.score) }}</template>
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
          <button class="act-btn" @click="promote(row)">升常驻</button>
          <button class="act-btn act-btn--danger" @click="archive(row)">归档</button>
          <button class="act-btn act-btn--danger" @click="askDelete(row)">删除</button>
        </template>
      </ItemCard>
    </div>

    <PagerBar
      :total="pagerTotal"
      :page="page"
      :page-size="pageSize"
      :unknown="count === null"
      @update:page="onPage"
      @update:page-size="onSize"
    />

    <DetailDrawer v-model="drawer" :detail="detail" @saved="load" />

    <!-- 归档确认（危险操作）：红竖条 + 逐条后果 + 手输 key 末 8 位 -->
    <ConfirmDialog
      v-model="archiving"
      title="归档这条记忆条目？"
      :consequences="archiveConsequences"
      :expect="archiveExpect"
      confirm-text="归档"
      :busy="busyArchive"
      @confirm="doArchive"
    />

    <!-- 真删确认：比归档更重（物理删除），所以后果里点名备份路径与手输要求 -->
    <ConfirmDialog
      v-model="deleting"
      title="彻底删除这条记忆条目？"
      :consequences="deleteConsequences"
      :expect="deleteExpect"
      confirm-text="确认删除"
      :busy="busyDelete"
      @confirm="doDelete"
    />

    <!-- 导入实跑确认：expect = 预演给出的「将新增 N 条」，与 merge/forget 同一口径 -->
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

    <!-- 新增：正文必填 + 标签可选（治理保留标签由后端拒/剥，错误文案原样透出） -->
    <el-dialog
      v-model="creating"
      title="新增记忆条目"
      width="440px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyCreate"
      :show-close="!busyCreate"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="正文（必填）">
          <el-input
            v-model="draftContent"
            type="textarea"
            :rows="6"
            placeholder="一句话写清这条记忆是什么、能用来做什么"
          />
        </el-form-item>
        <el-form-item label="标签（可选，逗号分隔）">
          <el-input v-model="draftTags" placeholder="如 ops,deploy；core / agent:* 等治理标签不许手填" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="busyCreate" @click="creating = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busyCreate"
          :disabled="!draftContent.trim()"
          @click="doCreate"
        >
          新增
        </el-button>
      </template>
    </el-dialog>

    <!-- 导入：先 JSONL 解析 → 预演（零写入）回显计数 → 手输 N 才实跑 -->
    <el-dialog
      v-model="importing"
      title="导入记忆条目（JSONL）"
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
            placeholder='{"content": "这条记忆是什么", "tags": "ops"}'
          />
        </el-form-item>
      </el-form>
      <p v-if="importParse.broken" class="banner banner--danger">
        有 {{ importParse.broken }} 行不是合法的 {"content": ...}，未计入下方条目 —— 不静默丢行，请修正后重试。
      </p>
      <p v-else-if="importParse.items.length" class="tip">
        已解析 {{ importParse.items.length }} 条，点「预演」后端会按内容去重、剥治理标签，并回显将新增多少条。
      </p>
      <p v-if="importPlan" class="tip">
        预演结果：将新增 {{ importPlan.would_add }} 条 · 跳过重复 {{ importPlan.skipped_duplicate }} 条 ·
        拒收治理标签 {{ importPlan.rejected_reserved }} 条 · 空正文 {{ importPlan.rejected_empty }} 条
        （共 {{ importPlan.total }} 条）。
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
          @click="askImportRun"
        >
          实跑导入
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import DetailDrawer from '../components/DetailDrawer.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import http, { api, getToken } from '../api'
import { copyText } from '../utils/clipboard'
import { showConfirm, showError, showToast } from '../utils/dialog'
import { downloadTemplate } from '../utils/export'
import { toLocal } from '../utils/format'
import { isStale } from '../utils/kb'

// count 未知（全库浏览）时按 has_more 撑出「下一页」：给一个假的下一页总数，
// 有下一页才显示 total=page*page_size+1，翻到那儿再由 has_more=false 收口。
const pagerTotal = computed(() =>
  count.value ?? (hasMore.value ? page.value * pageSize.value + 1 : items.value.length),
)

const q = ref('')
const tag = ref('')
const items = ref([])
const count = ref(0)
const hasMore = ref(false)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)
const error = ref('')
const drawer = ref(false)
const detail = ref(null)

// 归档确认（危险操作）走 ConfirmDialog：红色竖条 + 逐条后果 + 手输 key 末 8 位
const archiving = ref(false)
const archiveRow = ref(null)
const busyArchive = ref(false)
const archiveExpect = computed(() => (archiveRow.value ? archiveRow.value.key.slice(-8) : ''))
const archiveConsequences = computed(() =>
  archiveRow.value
    ? [
        `把 ${archiveRow.value.key} 置为失效：不再被召回，也不占注入预算。`,
        '不物理删除，备份留在 /root/.hermes/backup/console_ops/，需要时可以回滚。',
        '写操作会落审计日志，记录是谁在什么时候归档的。',
      ]
    : [],
)

// ---- 真删（危险操作，同 ConfirmDialog 三层防手滑）----
const deleting = ref(false)
const deleteRow = ref(null)
const busyDelete = ref(false)
const deleteExpect = computed(() => (deleteRow.value ? deleteRow.value.key.slice(-8) : ''))
const deleteConsequences = computed(() =>
  deleteRow.value
    ? [
        `真删 ${deleteRow.value.key}：从存储层物理移除，检索与常驻层立刻不再返回它，不可恢复。`,
        '与归档不同 —— 归档只是失效可回溯，这里是这条记忆不该再存在（误打、含敏感信息）。',
        '删前会自动备份到 /root/.hermes/backup/console_ops/（唯一的回退路径）；请照抄上面的 key 末 8 位确认。',
      ]
    : [],
)

// ---- 新增 ----
const creating = ref(false)
const busyCreate = ref(false)
const draftContent = ref('')
const draftTags = ref('')

// ---- 导入：解析 → 预演 → 手输 N 实跑 ----
const importing = ref(false)
const importText = ref('')
const importParse = reactive({ items: [], broken: 0 })
const importPlan = ref(null)
const busyPlan = ref(false)
const busyRun = ref(false)
const importRunAsk = ref(false)
const importExpect = computed(() => (importPlan.value ? String(importPlan.value.would_add) : ''))
const importConsequences = computed(() =>
  importPlan.value
    ? [
        `将写入 ${importPlan.value.would_add} 条新记忆（重复 ${importPlan.value.skipped_duplicate} 条会被跳过，不覆盖旧条目）。`,
        '打 core 等治理标签的条目会被拒收（不进常驻层），常驻请走候选审核。',
        `请照抄「${importExpect.value}」确认 —— 这个数就是预演出来的将新增条数，写进去一条都不会多。`,
      ]
    : [],
)

// ---- 导出 ----
const exportFormat = ref('jsonl')
const busyExport = ref(false)

/** 有筛选条件在生效吗：空态文案与「清除筛选」按钮都看它 */
const dirty = computed(() => !!(q.value || tag.value))

/** tags 是逗号分隔的一串，拆成一个个 chip 展示 */
function splitTags(s) {
  return (s || '')
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
}

/** score 是浮点，卡片上对齐到 4 位，别铺一串尾零。 */
function fmtScore(v) {
  return typeof v === 'number' ? v.toFixed(4) : (v ?? '—')
}

// 请求序号：写操作成功后接着刷新、用户同时又改词时，旧响应只丢弃不覆盖新结果
let reqSeq = 0

async function load() {
  const seq = ++reqSeq
  loading.value = true
  error.value = ''
  try {
    const data = await api.fragments({
      // el-select 被清空时给的是 undefined 而不是 ''，统一兜一层再 trim
      q: (q.value || '').trim() || undefined,
      tag: (tag.value || '').trim() || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    if (isStale(seq, reqSeq)) return
    items.value = data.items
    count.value = data.count
    hasMore.value = data.has_more
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    error.value = `读取记忆条目失败：${e.message}`
    ElMessage.error(error.value)
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}
function reset() {
  q.value = ''
  tag.value = ''
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
async function open(row) {
  try {
    detail.value = await api.fragment(row.key)
    drawer.value = true
  } catch (e) {
    ElMessage.error(`读取详情失败：${e.message}`)
  }
}

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** 升常驻：打上 core 标签（每轮 system_prompt 全量注入）。 */
async function promote(row) {
  const ok = await showConfirm(
    `确认把 ${row.key} 升为常驻？之后每轮都会注入这条，占用注入预算。`,
    '升常驻',
  )
  if (!ok) return
  try {
    await api.promote(row.key)
    showToast(`已升常驻：${row.key}`)
    load()
  } catch (e) {
    showError(`升常驻失败：${e.message}`)
  }
}

/** 归档：置失效（不物理删）。要求手输 key 末 8 位 —— 防手滑。 */
function archive(row) {
  archiveRow.value = row
  archiving.value = true
}

async function doArchive(typed) {
  const row = archiveRow.value
  if (!row) return
  busyArchive.value = true
  try {
    await api.archive(row.key, typed)
    archiving.value = false
    showToast(`已归档：${row.key}`)
    load()
  } catch (e) {
    showError(`归档失败：${e.message}`)
  } finally {
    busyArchive.value = false
  }
}

/** 真删：物理删除，不可恢复。要求手输 key 末 8 位 —— 与归档同款防手滑。 */
function askDelete(row) {
  deleteRow.value = row
  deleting.value = true
}

async function doDelete(typed) {
  const row = deleteRow.value
  if (!row) return
  busyDelete.value = true
  try {
    await api.deleteFragment(row.key, typed)
    deleting.value = false
    showToast(`已删除：${row.key}`)
    load()
  } catch (e) {
    showError(`删除失败：${e.message}`)
  } finally {
    busyDelete.value = false
  }
}

// ---- 新增 ----
function openCreate() {
  draftContent.value = ''
  draftTags.value = ''
  creating.value = true
}

async function doCreate() {
  const content = draftContent.value.trim()
  if (!content) return
  busyCreate.value = true
  try {
    // 后端 400（打 core 等保留标签）时 e.message 就是 detail.message，不吞
    const r = await api.createFragment({ content, tags: draftTags.value.trim() })
    creating.value = false
    showToast(`已新增：${r.key}`)
    load()
  } catch (e) {
    showError(`新增失败：${e.message}`)
  } finally {
    busyCreate.value = false
  }
}

// ---- 导入 ----
/** JSONL 逐行解析成 items；坏行**计数返回**，绝不静默丢 */
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

/**
 * 下载导入模板：走 fetch+blob（端点要 Authorization 头，裸 `<a href>` 带不了，
 * token 会漏进浏览器历史）。共用 utils/export.js，与导出同一套下载器。
 */
async function getTemplate() {
  try {
    await downloadTemplate('memory')
    showToast('已下载导入模板（keepsake-import-template-memory.jsonl）')
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

/** 预演：后端零写入，只回显「将新增 / 跳过重复 / 拒收」三个数 */
async function planImport() {
  busyPlan.value = true
  try {
    importPlan.value = await api.importFragments({ items: currentItems(), dry_run: true })
  } catch (e) {
    showError(`导入预演失败：${e.message}`)
  } finally {
    busyPlan.value = false
  }
}

function askImportRun() {
  if (!importPlan.value || !importPlan.value.would_add) return
  importRunAsk.value = true
}

async function doImportRun(typed) {
  busyRun.value = true
  try {
    const r = await api.importFragments({ items: importParse.items, dry_run: false, confirm: typed })
    importRunAsk.value = false
    importing.value = false
    showToast(`导入完成：新增 ${r.would_add} 条 · 跳过重复 ${r.skipped_duplicate} 条`)
    load()
  } catch (e) {
    showError(`导入失败：${e.message}`)
  } finally {
    busyRun.value = false
  }
}

// ---- 导出 ----
/** 失败时把后端的 JSON 文案取出来（否则只能看到「status 400」） */
async function exportError(res) {
  try {
    const d = JSON.parse(await res.text())
    return d?.detail?.message || d?.detail?.error || `后端返回 ${res.status}`
  } catch {
    return `后端返回 ${res.status}`
  }
}

/**
 * 导出：走 fetch 而不是裸 `<a href>` —— 端点要 Authorization 头，裸链子带不了
 * （token 只进请求头，绝不进 URL）。baseURL 取自 http 实例，不另写一份环境变量。
 * q/tag 沿用页面当前筛选：导出的就是屏幕上这一批，不是「全库」。
 */
async function doExport() {
  busyExport.value = true
  try {
    const qs = new URLSearchParams()
    const kw = (q.value || '').trim()
    const tg = (tag.value || '').trim()
    if (kw) qs.set('q', kw)
    if (tg) qs.set('tag', tg)
    qs.set('format', exportFormat.value)
    const token = getToken()
    const res = await fetch(`${http.defaults.baseURL || ''}/api/v1/export?${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) throw new Error(await exportError(res))
    const url = URL.createObjectURL(await res.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = `keepsake-export.${exportFormat.value}`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
    showToast(`导出已开始下载：${a.download}`)
  } catch (e) {
    showError(`导出失败：${e.message}`)
  } finally {
    busyExport.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 导出格式下拉：只跟 .sel 一样是筛选区控件的宽度档，窄一点不抢按钮的视线 */
.fmt {
  width: 110px;
}
/* 弹框里的说明行：与 DetailDrawer 的 .tip 同一份配方（12px/1.7 --text-faint）——
   scoped 样式跨组件不生效，只能各写一份，数值不许自创 */
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
/* 字段名旁的「下载模板」：与正文标题同一行，不另起一行抢视线 */
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
</style>
