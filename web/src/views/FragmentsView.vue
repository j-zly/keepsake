<template>
  <div>
    <div class="page-head">
      <div>
        <h1 class="page-title">检索层</h1>
        <p class="page-sub">全库记忆条目，按 BM25 全文检索召回；点行看全文与编辑。</p>
      </div>
    </div>

    <div class="toolbar">
      <el-input
        v-model="q"
        class="grow search"
        placeholder="搜索正文（BM25 全文检索，走 keepsake storage.search）"
        clearable
        @keyup.enter="search"
      />
      <el-select
        v-model="tag"
        class="tagsel"
        placeholder="标签"
        filterable
        allow-create
        default-first-option
        clearable
      />
      <el-button type="primary" @click="search">查询</el-button>
      <el-button @click="reset">清空</el-button>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>
    <div v-else-if="count === null" class="banner">
      当前是全库浏览：存储层没有 count 原语，总条数未知，按 has_more 翻页。
    </div>

    <div v-if="loading && !items.length" class="panel skeleton-wrap">
      <el-skeleton :rows="5" animated />
    </div>

    <el-table v-else :data="items" max-height="560" class="table" @row-click="open">
      <el-table-column label="key" width="240">
        <template #default="{ row }">
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
        </template>
      </el-table-column>
      <el-table-column label="tags" width="140">
        <template #default="{ row }">
          <span class="mono faint cell-clip" :title="row.tags">{{ row.tags || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="正文（截断）">
        <template #default="{ row }">
          <span class="cell-clip" :title="row.content">{{ row.content }}</span>
        </template>
      </el-table-column>
      <el-table-column label="score" width="90">
        <template #default="{ row }">
          <span class="mono num dim">{{ fmtScore(row.score) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="更新时间" width="150">
        <template #default="{ row }">
          <span class="mono dim" :title="row.updated_at">{{ toLocal(row.updated_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="promote(row)">升常驻</el-button>
          <el-button link type="danger" @click.stop="archive(row)">归档</el-button>
          <el-button link @click.stop="open(row)">编辑</el-button>
        </template>
      </el-table-column>

      <template #empty>
        <div class="empty">
          <svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="currentColor" stroke-width="1.2">
            <circle cx="10.5" cy="10.5" r="6" />
            <path d="m15 15 4.5 4.5" stroke-linecap="round" />
          </svg>
          <p v-if="q || tag"><b>没召回到记忆条目</b>换个词试试，或把标签清空做全库浏览。</p>
          <p v-else><b>这里什么都没有</b>点「查询」按关键词召回，或直接翻页浏览全库。</p>
        </div>
      </template>
    </el-table>

    <PagerBar
      :total="pagerTotal"
      :page="page"
      :page-size="pageSize"
      :unknown="count === null"
      @update:page="onPage"
      @update:page-size="onSize"
    />

    <!-- 全文 / 编辑抽屉：表格里长正文只给一行省略号，全文在这里 -->
    <el-drawer v-model="drawer" title="记忆条目全文 / 编辑" size="min(620px, 92vw)">
      <div v-if="detail">
        <div class="kv">
          <div class="row">
            <span class="k">key</span>
            <span class="cell-key">
              <span class="v mono">{{ detail.key }}</span>
              <button class="copy-btn" title="复制 key" @click="doCopy(detail.key)">
                <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor"
                  stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="5.4" y="5.4" width="8" height="8" rx="1.6" />
                  <path d="M10.6 5.4V4A1.4 1.4 0 0 0 9.2 2.6H4A1.4 1.4 0 0 0 2.6 4v5.2A1.4 1.4 0 0 0 4 10.6h1.4" />
                </svg>
              </button>
            </span>
          </div>
          <div class="row"><span class="k">tags</span><span class="v mono">{{ detail.tags || '—' }}</span></div>
          <div class="row">
            <span class="k">创建</span>
            <span class="v mono" :title="detail.created_at">{{ toLocal(detail.created_at) }}</span>
          </div>
          <div class="row">
            <span class="k">更新</span>
            <span class="v mono" :title="detail.updated_at">{{ toLocal(detail.updated_at) }}</span>
          </div>
        </div>

        <h3 class="section-title">编辑</h3>
        <el-form label-position="top">
          <el-form-item label="tags">
            <el-input v-model="editTags" placeholder="逗号分隔，如 core,ops" />
          </el-form-item>
          <el-form-item label="正文">
            <el-input v-model="editContent" type="textarea" :rows="8" />
          </el-form-item>
        </el-form>
        <el-button type="primary" :loading="saving" @click="save">保存修改</el-button>
        <p class="tip">
          改前会自动备份整条原文到 <code>/root/.hermes/backup/console_ops/</code>；
          正文与 tags 同时为空会被后端拒掉（400），想下线请用「归档」。
        </p>

        <el-collapse class="rawbox">
          <el-collapse-item title="其余字段" name="raw">
            <pre class="full">{{ JSON.stringify(detail.raw, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-drawer>

    <ConfirmDialog
      v-model="archiving"
      title="归档这条记忆条目？"
      :consequences="archiveConsequences"
      :expect="archiveExpect"
      confirm-text="归档"
      :busy="busyArchive"
      @confirm="doArchive"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import PagerBar from '../components/PagerBar.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showConfirm, showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

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
const editContent = ref('')
const editTags = ref('')
const saving = ref(false)

// 归档确认（危险操作）走 ConfirmDialog：红色竖条 + 逐条后果 + 手输 key 末 8 位
const archiving = ref(false)
const archiveRow = ref(null)
const busyArchive = ref(false)
const archiveExpect = computed(() => (archiveRow.value ? archiveRow.value.key.slice(-8) : ''))
const archiveConsequences = computed(() =>
  archiveRow.value
    ? [
        `把 ${archiveRow.value.key} 置为失效：检索层不再召回它，也不占注入预算。`,
        '不物理删除，备份留在 /root/.hermes/backup/console_ops/，需要时可以回滚。',
        '写操作会落审计日志，记录是谁在什么时候归档的。',
      ]
    : [],
)

/** score 是浮点，表格里对齐到 4 位，别铺一串尾零。 */
function fmtScore(v) {
  return typeof v === 'number' ? v.toFixed(4) : (v ?? '—')
}

async function load() {
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
    items.value = data.items
    count.value = data.count
    hasMore.value = data.has_more
  } catch (e) {
    error.value = `读取记忆条目失败：${e.message}`
    ElMessage.error(error.value)
  } finally {
    loading.value = false
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
    editContent.value = detail.value.content || ''
    editTags.value = detail.value.tags || ''
    drawer.value = true
  } catch (e) {
    ElMessage.error(`读取详情失败：${e.message}`)
  }
}

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** 保存修改：正文 / tags。二次确认后提交，成功刷新列表 + toast。 */
async function save() {
  if (!detail.value) return
  const ok = await showConfirm(
    `确认覆盖 ${detail.value.key} 的正文与标签？写前会自动备份改前原文。`,
    '保存修改',
  )
  if (!ok) return
  saving.value = true
  try {
    const r = await api.updateFragment(detail.value.key, {
      content: editContent.value,
      tags: editTags.value,
    })
    showToast(`已保存 ${detail.value.key}（改动字段：${r.updated.join('、')}）`)
    drawer.value = false
    load()
  } catch (e) {
    showError(`保存失败：${e.message}`)
  } finally {
    saving.value = false
  }
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

onMounted(load)
</script>

<style scoped>
.search {
  max-width: 420px;
}
.tagsel {
  width: 170px;
}
.toolbar {
  padding: 10px 12px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  box-shadow: var(--inset);
}
.table {
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--r-card);
  box-shadow: var(--inset);
}
.skeleton-wrap {
  padding: 16px;
}
.kv {
  border: 1px solid var(--border);
  border-radius: var(--r-ctl);
  overflow: hidden;
}
.kv .row {
  display: flex;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
}
.kv .row:last-child {
  border-bottom: 0;
}
.kv .k {
  flex: none;
  width: 52px;
  color: var(--text-faint);
}
.kv .v {
  min-width: 0;
  color: var(--text);
  word-break: break-all;
}
.tip {
  margin: 10px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.rawbox {
  margin-top: 18px;
}
.full {
  margin: 0;
  padding: 12px;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--r-ctl);
  color: var(--text-dim);
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 320px;
  overflow: auto;
}
</style>