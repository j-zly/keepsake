<!--
  知识库：默认视图是**文档列表**（不输词就有数据，P9 起）；P10 起检索是**两路并行** ——
  同一个词同时打 /kb/documents（按标题过滤下面的文档列表）和 /kb/search（切片正文命中）。
  两路各带各的分页与计数，结果行合成「文档 N 条 · 切片 M 条」；**任一有结果就算找得到**。
  清空 ✕（或删光后回车）两路都清、无条件回全量文档列表（不是空态）；点文档 → 抽屉拉它的切片。

  口径全部来自接口，页面不另算：count / page / page_size 都是后端给的。
  防串用请求序号（utils/kb.js 的 isStale）：快速改词时旧响应只丢弃、不覆盖新结果。

  批2 加了写操作：新增 / 编辑 / 真删 / 导入 / 导出，全部只对**手动域**文档开放
  （skill 域归夜间灌入器，前端不渲染按钮，后端 _manual_only 409 兜底）。
  手动域判定读列表接口真投影出来的 source_type，**严格判据**：字段缺席/未知一律
  按只读渲染（缺省放行会让 skill 文档看着也能改）。筛选区加了来源下拉，值原样
  透传后端 `source_type`（精确匹配，与导出端点同一套口径与校验）。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">知识库</h1>
      <p class="page-sub">PG 里的文档与切片；默认按文档浏览，输了词则文档标题与切片正文一起查。</p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <div class="stat-grid">
      <StatCard label="skill_docs" :value="summary?.skill_docs ?? '-'" unit="条" />
      <StatCard label="documents" :value="summary?.documents ?? '-'" unit="条" />
      <StatCard label="chunks" :value="summary?.chunks ?? '-'" unit="条" />
    </div>

    <div class="filter-panel">
      <div class="filter-title">检索</div>
      <p class="filter-desc">
        一个词两路同时查：下面的文档列表按标题过滤，切片区按正文命中；任一有结果就算找得到。清空即回到全量文档列表。来源下拉按来源类型精确筛选（手动 / 灌入器两档，另细分到 skill_hk）。
      </p>
      <div class="filter-ctl">
        <el-input
          v-model="q"
          class="grow"
          placeholder="输入检索词：文档标题与切片正文同时查"
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
        <el-select v-model="sourceType" class="sel" title="来源" @change="onSource">
          <el-option label="全部来源" value="" />
          <el-option label="手动（可改删）" value="manual" />
          <el-option label="技能 skill（灌入器）" value="skill" />
          <el-option label="技能手册 skill_hk（灌入器）" value="skill_hk" />
        </el-select>
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </div>
      <!-- 操作区：新增走弹框（只进手动域）；导入先预演再手输条数；导出走后端流式端点（带 Authorization 头） -->
      <div class="acts">
        <el-button :loading="busyExport" @click="doExport">导出</el-button>
        <el-button @click="openImport">导入</el-button>
        <el-button type="primary" @click="openCreate">新增文档</el-button>
      </div>
      <!-- 两路计数合并：N/M 来自各自接口的 count，不写死 -->
      <div v-if="searching && !loading" class="filter-result">
        <span>{{ mergeCounts(docCount, hitCount) }}</span>
      </div>
    </div>

    <div v-if="loading" class="panel pad">
      <el-skeleton :rows="4" animated />
    </div>

    <!-- 两路都为 0 才算没找到（anyHit）；任一有结果往下走；出错时只留上方横幅，不冒充「没找到」 -->
    <div v-else-if="searching && !anyHit && !error" class="panel">
      <div class="empty">
        <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
          <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
          <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
        </svg>
        <p><b>没找到「{{ q.trim() }}」</b>文档标题和切片正文都没命中 —— 换个更短的核心词试试。</p>
      </div>
    </div>

    <template v-else>
      <!-- 第一路：文档列表（检索态下按标题过滤，原地更新 doc-grid） -->
      <div v-if="searching && docs.length" class="section-title">文档</div>
      <div v-if="docs.length" class="card-grid">
        <ItemCard v-for="doc in docs" :key="doc.doc_id" @lead="openDoc(doc)">
          <template #tag>切片 {{ doc.chunk_count }}</template>
          <template v-if="doc.updated_at" #time>{{ toLocal(doc.updated_at) }}</template>

          <p class="card-title" :title="doc.title">{{ doc.title || '（无标题）' }}</p>
          <p class="card-body mono">{{ doc.doc_id }}</p>

          <template #tags>
            <span class="tag mono">{{ doc.doc_id }}</span>
            <span v-if="!isManual(doc)" class="tag">只读 · 灌入器维护</span>
          </template>

          <template #lead>查看切片 →</template>
          <!-- 只有手动域能改/删；skill 域不渲染按钮（后端 409 兜底，这里先挡一道） -->
          <template v-if="isManual(doc)" #actions>
            <button class="act-btn" @click="openEdit(doc)">编辑</button>
            <button class="act-btn act-btn--danger" @click="askDelete(doc)">删除</button>
          </template>
        </ItemCard>
      </div>

      <!-- 「知识库里还没有文档」只在**请求成功且返回 0 条**时出现：加载中有骨架屏、出错有上方横幅 -->
      <div v-if="!docs.length && !searching && docLoaded && !error" class="panel">
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <path d="M4.5 4h4.6a2 2 0 0 1 2 2v13a1.8 1.8 0 0 0-1.8-1.8H4.5Z" stroke-linejoin="round" />
            <path d="M19.5 4h-4.6a2 2 0 0 0-2 2v13a1.8 1.8 0 0 0 1.8 1.8h4.8Z" stroke-linejoin="round" />
          </svg>
          <p v-if="!sourceType"><b>知识库里还没有文档</b>往 documents 表灌入文档后，这里会自动列出标题、更新时间和切片数。</p>
          <p v-else><b>该来源下还没有文档</b>当前只列 source_type={{ sourceType }} 的文档，换个来源或选「全部来源」再看看。</p>
        </div>
      </div>

      <PagerBar
        v-if="docCount"
        :total="docCount"
        :page="page"
        :page-size="pageSize"
        @update:page="onPage"
        @update:page-size="onSize"
      />

      <!-- 第二路：切片命中区（分页独立，走 /kb/search 的 page） -->
      <template v-if="searching">
        <div v-if="hits.length" class="section-title">切片</div>
        <div v-if="hits.length" class="card-grid">
          <ItemCard v-for="row in hits" :key="row.chunk_id" chip-tone="accent" @lead="doCopy(row.chunk_id)">
            <template #tag>rank {{ row.rank }}</template>

            <p class="card-title" :title="row.title">{{ row.title || '（无标题）' }}</p>
            <p class="card-body" :title="row.snippet">{{ row.snippet }}</p>

            <template #tags>
              <span class="tag mono">{{ row.chunk_id }}</span>
            </template>

            <template #lead>复制 chunk_id ↗</template>
          </ItemCard>
        </div>

        <PagerBar
          v-if="hitCount"
          :total="hitCount"
          :page="hitPage"
          :page-size="hitSize"
          @update:page="onHitPage"
          @update:page-size="onHitSize"
        />
      </template>
    </template>

    <!-- 某个文档的切片 -->
    <el-drawer
      v-model="drawer"
      :title="`切片 · ${curDoc?.title || curDoc?.doc_id || ''}`"
      size="min(620px, 92vw)"
    >
      <div class="chunk-box">
        <el-skeleton v-if="chunkLoading" :rows="3" animated />
        <template v-else>
        <p class="chunk-count">
          共 {{ chunkCount }} 条切片 · 第 {{ chunkPage }} 页
        </p>
        <ol class="chunk-list">
          <li v-for="c in chunks" :key="c.chunk_id">
            <div class="chunk-top">
              <span class="tag mono">{{ c.chunk_id }}</span>
              <button class="card-link" @click="doCopy(c.chunk_id)">复制 ↗</button>
            </div>
            <p class="chunk-text">{{ c.snippet || '（空切片）' }}</p>
          </li>
        </ol>
        <p v-if="!chunks.length" class="chunk-count">这个文档下还没有切片。</p>
        <PagerBar
          v-if="chunkCount"
          :total="chunkCount"
          :page="chunkPage"
          :page-size="chunkSize"
          @update:page="onChunkPage"
          @update:page-size="onChunkSize"
        />
        </template>
      </div>
    </el-drawer>

    <!-- 新增：标题与正文都必填（后端任一为空直 400）；只进手动域，错误文案原样透出 -->
    <el-dialog
      v-model="creating"
      title="新增知识库文档"
      width="560px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyCreate"
      :show-close="!busyCreate"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="标题（必填）">
          <el-input v-model="draftTitle" placeholder="一句话说清这篇文档是什么" />
        </el-form-item>
        <el-form-item label="正文（必填）">
          <el-input
            v-model="draftContent"
            type="textarea"
            :rows="9"
            placeholder="文档全文；保存时后端会切块并同事务落库"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button :disabled="busyCreate" @click="creating = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busyCreate"
          :disabled="!draftTitle.trim() || !draftContent.trim()"
          @click="doCreate"
        >
          新增
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑：标题预填现值；正文**故意不回显**（读接口只给 LEFT(text,300) 的截断切片，
         拿它回显再保存就是把原文截断）—— 留空即只改标题，确实要改正文才粘贴完整全文。 -->
    <el-dialog
      v-model="editing"
      title="编辑知识库文档"
      width="620px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyEdit"
      :show-close="!busyEdit"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="标题（必填）">
          <el-input v-model="editTitle" placeholder="标题" />
        </el-form-item>
        <el-form-item label="正文（留空 = 只改标题）">
          <el-input
            v-model="editContent"
            type="textarea"
            :rows="9"
            placeholder="要改正文就粘贴完整原文；留空只改标题。改正文时后端会删旧切片重切。"
          />
        </el-form-item>
      </el-form>
      <p class="tip">
        正文不回显：列表接口只返回标题，正文只存在于切片里（且读接口每条只给前 300 字），
        拿截断的切片回显再保存会把原文截断。要改正文请粘贴完整全文。
      </p>
      <template #footer>
        <el-button :disabled="busyEdit" @click="editing = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busyEdit"
          :disabled="!editTitle.trim()"
          @click="doEdit"
        >
          保存
        </el-button>
      </template>
    </el-dialog>

    <!-- 导入：JSONL 逐行 {title, content} → 预演（零写入）回显计数 → 手输 N 才实跑 -->
    <el-dialog
      v-model="importing"
      title="导入知识库文档（JSONL）"
      width="560px"
      align-center
      :close-on-click-modal="false"
      :close-on-press-escape="!busyPlan && !busyRun"
      :show-close="!busyPlan && !busyRun"
      append-to-body
    >
      <el-form label-position="top">
        <el-form-item label="每行一篇 {title, content}">
          <el-input
            v-model="importText"
            type="textarea"
            :rows="7"
            placeholder='{"title": "文档标题", "content": "文档全文"}'
          />
        </el-form-item>
      </el-form>
      <p v-if="importParse.broken" class="banner banner--danger">
        有 {{ importParse.broken }} 行不是合法的 {"title", "content"}（缺标题/缺正文/不是 JSON），未计入下方条目 —— 不静默丢行，请修正后重试。
      </p>
      <p v-else-if="importParse.items.length" class="tip">
        已解析 {{ importParse.items.length }} 篇，点「预演」后端会按内容去重并回显将新增多少篇；导入**只进手动域**。
      </p>
      <p v-if="importPlan" class="tip">
        预演结果：将新增 {{ importPlan.would_add }} 篇 · 已存在跳过 {{ importPlan.unchanged }} 篇 ·
        空标题或空正文 {{ importPlan.rejected_empty }} 篇（共 {{ importPlan.total }} 篇）。
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

    <!-- 真删确认：比编辑重（物理删除），后果里点名备份、手输全文与域隔离 -->
    <ConfirmDialog
      v-model="deleting"
      title="彻底删除这篇知识库文档？"
      :consequences="deleteConsequences"
      :expect="deleteExpect"
      confirm-text="确认删除"
      :busy="busyDelete"
      @confirm="doDelete"
    />

    <!-- 导入实跑确认：expect = 预演给出的「将新增 N 篇」，与记忆导入同口径 -->
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
import ConfirmDialog from '../components/ConfirmDialog.vue'
import ItemCard from '../components/ItemCard.vue'
import PagerBar from '../components/PagerBar.vue'
import StatCard from '../components/StatCard.vue'
import http, { api, getToken } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'
import { hasAnyHit, isStale, mergeCounts } from '../utils/kb'

// 每页条数跟着各自 PagerBar 的下拉走 —— 两路各自独立：文档列表 / 切片命中 / 文档内切片
const pageSize = ref(20)
const hitSize = ref(20)
const chunkSize = ref(20)

const summary = ref(null)
const docs = ref([])
const docCount = ref(0)
const page = ref(1)
const hits = ref([])
const hitCount = ref(0)
const hitPage = ref(1)
const q = ref('')
/** 来源筛选：''=全部（默认视图不变）；否则原样透传后端 `source_type`（精确匹配，同导出端点口径） */
const sourceType = ref('')
const loading = ref(false)
const error = ref('')
/** 三分状态用：请求中（骨架屏）/ 成功且真的 0 条（空态）/ 失败（错误横幅）——「零文档」只在成功后出现 */
const docLoaded = ref(false)

const drawer = ref(false)
const curDoc = ref(null)
const chunks = ref([])
const chunkCount = ref(0)
const chunkPage = ref(1)
const chunkLoading = ref(false)

// ---- 写操作（批2：新增 / 编辑 / 真删 / 导入 / 导出），口径与记忆条目页一致 ----
const creating = ref(false)
const busyCreate = ref(false)
const draftTitle = ref('')
const draftContent = ref('')

const editing = ref(false)
const busyEdit = ref(false)
const editDoc = ref(null)
const editTitle = ref('')
const editContent = ref('')

const deleting = ref(false)
const deleteDoc = ref(null)
const busyDelete = ref(false)
const deleteExpect = computed(() => (deleteDoc.value ? deleteDoc.value.title : ''))
const deleteConsequences = computed(() =>
  deleteDoc.value
    ? [
        `真删《${deleteDoc.value.title}》：连同它的全部切片一起移除，检索与切片浏览立刻不再返回它，不可恢复。`,
        '删前自动备份：文档行与全部切片正文落在 console_ops 备份目录的 document.<id>.before.json（写不成则整次删除中止，这是唯一的回退路径）。',
        '只有手动文档能删：skill/skill_hk 由夜间灌入器维护，改动会被覆盖、被删的文档也会被清理扫掉，后端对它们直回 409。',
        '请照抄上面的标题全文确认 —— 一个字都不能差。',
      ]
    : [],
)

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
        `将写入 ${importPlan.value.would_add} 篇新文档（已存在的 ${importPlan.value.unchanged} 篇会跳过，不覆盖旧文档）。`,
        '导入只进手动域：条目里带 source_type 会直接 422，不给调用方往 skill 域灌数据的路。',
        `请照抄「${importExpect.value}」确认 —— 这个数就是预演出来的将新增篇数，写进去一篇都不会多。`,
      ]
    : [],
)

const busyExport = ref(false)

/**
 * 只有手动域（source_type='manual'）的文档能改/删 —— skill/skill_hk 域归灌入器，
 * 后端 _manual_only 409 兜底。
 *
 * 🔴 **判据严格**：字段缺席/值未知一律算**非手动域**（只读徽标、不渲染写按钮）。
 * 反过来（缺省当手动）会在读接口没投出 source_type 时给 skill 文档渲染出
 * 「编辑/删除」，看着能改、点下去才被 409 挡回 —— 保守方向才是安全方向。
 */
function isManual(doc) {
  return doc.source_type === 'manual'
}

// 防串：请求序号。发请求前 ++，回来时序号对不上 ⇒ 过期响应直接丢弃（utils/kb.js 的 isStale）。
// 不用 AbortController 是因为 axios 层不接 signal（api 客户端本轮只增不改），序号已足够防覆盖。
let reqSeq = 0

/** 检索框有词 ⇒ 检索态；没词 ⇒ 浏览文档列表（默认视图）。空词判定与 search() 里那处同义：都要 trim */
const searching = computed(() => !!q.value.trim())
/** 「任一有结果就算找得到」：文档命中或切片命中任一不为 0 就算数（两路都 0 才显示空态） */
const anyHit = computed(() => hasAnyHit(docCount.value, hitCount.value))

async function loadSummary() {
  try {
    summary.value = await api.kbSummary()
    error.value = ''
  } catch (e) {
    summary.value = null
    error.value = `知识库不可用：${e.message}（需要 KEEPSAKE_PG_DSN 指向只读实例）`
  }
}

/** 浏览态：全量文档列表（不带 q）—— 默认视图 / 清空后 / 未输词翻页都走这里 */
async function loadDocs() {
  const seq = ++reqSeq
  loading.value = true
  try {
    const data = await api.kbDocuments({ source_type: sourceType.value, page: page.value, page_size: pageSize.value })
    if (isStale(seq, reqSeq)) return
    docs.value = data.items
    docCount.value = data.count
    docLoaded.value = true
    error.value = ''
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    docs.value = []
    docCount.value = 0
    error.value = `读取文档列表失败：${e.message}`
  } finally {
    if (!isStale(seq, reqSeq)) loading.value = false
  }
}

/**
 * 检索态：一个词两路并行 —— /kb/documents 按标题过滤下面的文档列表，
 * /kb/search 命切片正文。两路各拿各的 page/page_size，结果行再合成计数。
 * 序号不匹配（用户已经改词/清空了）⇒ 整个响应丢弃，绝不覆盖新结果。
 */
async function loadBoth(term) {
  const seq = ++reqSeq
  loading.value = true
  let docRes, hitRes
  try {
    [docRes, hitRes] = await Promise.all([
      api.kbDocuments({ q: term, source_type: sourceType.value, page: page.value, page_size: pageSize.value }),
      api.kbSearch({ q: term, page: hitPage.value, page_size: hitSize.value }),
    ])
  } catch (e) {
    if (isStale(seq, reqSeq)) return
    error.value = `知识库检索失败：${e.message}`
    loading.value = false
    return
  }
  if (isStale(seq, reqSeq)) return
  docs.value = docRes.items
  docCount.value = docRes.count
  hits.value = hitRes.items
  hitCount.value = hitRes.count
  docLoaded.value = true
  error.value = ''
  loading.value = false
}

/** 回车与点「搜索」按钮同一条路：有词两路重查、页码各回第 1 页；空词只回全量文档列表 */
async function search() {
  const term = q.value.trim()
  page.value = 1
  hitPage.value = 1
  // 空输入回车 ⇒ 只发 /kb/documents（不带 q、page=1）回全量列表，且**不发 /kb/search**（没词可搜）
  if (!term) return loadDocs()
  await loadBoth(term)
}

/** 清空 ✕：两路都清（切片清零），回到默认文档列表而不是空态 —— 与空输入回车同一条路 */
function onClear() {
  q.value = ''
  hits.value = []
  hitCount.value = 0
  return search()
}

/** 换来源：文档列表与检索两路都按新来源重查（页码各回第 1 页，否则第 5 页多半是空页） */
function onSource() {
  page.value = 1
  hitPage.value = 1
  return search()
}

function onPage(p) {
  page.value = p
  searching.value ? loadBoth(q.value.trim()) : loadDocs()
}
function onSize(s) {
  pageSize.value = Number(s)
  page.value = 1
  searching.value ? loadBoth(q.value.trim()) : loadDocs()
}
function onHitPage(p) {
  hitPage.value = p
  loadBoth(q.value.trim())
}
function onHitSize(s) {
  hitSize.value = Number(s)
  hitPage.value = 1
  loadBoth(q.value.trim())
}

async function openDoc(doc) {
  curDoc.value = doc
  chunks.value = []
  chunkCount.value = 0
  chunkPage.value = 1
  drawer.value = true
  await loadChunks()
}

async function loadChunks() {
  if (!curDoc.value) return
  chunkLoading.value = true
  try {
    const data = await api.kbDocumentChunks(curDoc.value.doc_id, {
      page: chunkPage.value,
      page_size: chunkSize.value,
    })
    chunks.value = data.items
    chunkCount.value = data.count
  } catch (e) {
    showError(`读取切片失败：${e.message}`)
  } finally {
    chunkLoading.value = false
  }
}

function onChunkPage(p) {
  chunkPage.value = p
  loadChunks()
}
function onChunkSize(s) {
  chunkSize.value = Number(s)
  chunkPage.value = 1
  loadChunks()
}

async function doCopy(id) {
  const ok = await copyText(id)
  ok ? showToast(`已复制 chunk_id：${id}`) : showError('复制失败，请手动选中复制')
}

/** 写操作之后刷新：计数与列表各走页面原有路径（检索态仍两路并行），防串序号照旧 */
function refresh() {
  loadSummary()
  return searching.value ? loadBoth(q.value.trim()) : loadDocs()
}

// ---- 新增 ----
function openCreate() {
  draftTitle.value = ''
  draftContent.value = ''
  creating.value = true
}

async function doCreate() {
  const title = draftTitle.value.trim()
  const content = draftContent.value.trim()
  if (!title || !content) return
  busyCreate.value = true
  try {
    const r = await api.kbCreateDocument({ title, content })
    creating.value = false
    // 内容级幂等：同标题同内容提交后端判 unchanged（连 INSERT 都不发），别谎报新增
    showToast(r.status === 'unchanged'
      ? `已存在同名同内容的一篇，未重复新增：${title}`
      : `已新增：${title}（${r.chunks_count} 块切片）`)
    refresh()
  } catch (e) {
    showError(`新增失败：${e.message}`)
  } finally {
    busyCreate.value = false
  }
}

// ---- 编辑 ----
/** 标题预填现值；正文不回显（切片读接口只给前 300 字，回显保存会把原文截断） */
function openEdit(doc) {
  editDoc.value = doc
  editTitle.value = doc.title || ''
  editContent.value = ''
  editing.value = true
}

async function doEdit() {
  const doc = editDoc.value
  const title = editTitle.value.trim()
  if (!doc || !title) return
  const content = editContent.value.trim()
  busyEdit.value = true
  try {
    // 正文留空 = 只改标题（后端 title/content 至少给一个）；填了才改正文并同事务重切切片
    await api.kbUpdateDocument(doc.doc_id, content ? { title, content } : { title })
    editing.value = false
    showToast(`已保存：${title}`)
    refresh()
  } catch (e) {
    showError(`保存失败：${e.message}`)
  } finally {
    busyEdit.value = false
  }
}

// ---- 真删 ----
function askDelete(doc) {
  deleteDoc.value = doc
  deleting.value = true
}

async function doDelete(typed) {
  const doc = deleteDoc.value
  if (!doc) return
  busyDelete.value = true
  try {
    await api.kbDeleteDocument(doc.doc_id, typed)
    deleting.value = false
    showToast(`已删除：${doc.title}`)
    refresh()
  } catch (e) {
    showError(`删除失败：${e.message}`)
  } finally {
    busyDelete.value = false
  }
}

// ---- 导入 ----
/**
 * JSONL 逐行解析成 items；坏行**计数返回**，绝不静默丢。
 * KB 条目必须有 title：缺标题/缺正文/不是 JSON 都算坏行（前端先挡一道，
 * 后端也会把空的计入 rejected_empty，但那时说不清是哪一种）。
 */
function parseJsonl(text) {
  const items = []
  let broken = 0
  for (const line of (text || '').split('\n')) {
    const s = line.trim()
    if (!s) continue
    try {
      const o = JSON.parse(s)
      if (o && typeof o.title === 'string' && o.title.trim()
          && typeof o.content === 'string' && o.content.trim()) {
        items.push({ title: o.title.trim(), content: o.content })
      } else broken += 1
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

function currentItems() {
  const p = parseJsonl(importText.value)
  importParse.items = p.items
  importParse.broken = p.broken
  return p.items
}

/** 预演：后端零写入，只回显「将新增 / 已存在跳过 / 空标题正文」三个数 */
async function planImport() {
  busyPlan.value = true
  try {
    importPlan.value = await api.kbImportDocuments({ items: currentItems(), dry_run: true })
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
    const r = await api.kbImportDocuments({ items: importParse.items, dry_run: false, confirm: typed })
    importRunAsk.value = false
    importing.value = false
    showToast(`导入完成：新增 ${r.would_add} 篇 · 跳过 ${r.unchanged} 篇`)
    refresh()
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
 * 后端导出只有 JSONL 一档；带来源筛选时按同一口径只导那一类（空=全量）。
 */
async function doExport() {
  busyExport.value = true
  try {
    const token = getToken()
    const qs = sourceType.value ? `?source_type=${encodeURIComponent(sourceType.value)}` : ''
    const res = await fetch(`${http.defaults.baseURL || ''}/api/v1/kb/export${qs}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!res.ok) throw new Error(await exportError(res))
    const url = URL.createObjectURL(await res.blob())
    const a = document.createElement('a')
    a.href = url
    a.download = 'keepsake-kb-export.jsonl'
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

onMounted(() => {
  loadSummary()
  loadDocs()
})
</script>

<style scoped>
.pad {
  padding: 20px;
}
/* 操作行：与 tokens 里 `.filter-result .acts` 同一份配方（flex + 8px 间距）——
   那条挂在 .filter-result 容器下，本页的操作行不是那个容器，故 scoped 复刻一份，数值不新创 */
.acts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
/* 弹框里的说明行：与 DetailDrawer 的 .tip 同一份配方（12px/1.7 --text-faint）——
   scoped 样式跨组件不生效，只能各写一份，数值不许自创 */
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.chunk-box {
  min-height: 120px;
}
.chunk-count {
  margin: 0 0 12px;
  color: var(--text-faint);
  font-size: 12.5px;
}
.chunk-list {
  margin: 0 0 16px;
  padding-left: 20px;
  color: var(--text);
  font-size: 13px;
}
.chunk-list li + li {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}
.chunk-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 6px;
}
.chunk-text {
  margin: 0;
  color: var(--text-dim);
  line-height: 1.7;
  white-space: pre-wrap;
}
</style>
