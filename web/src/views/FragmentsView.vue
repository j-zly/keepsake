<template>
  <div>
    <h2>检索层（记忆条目）</h2>
    <el-form :inline="true" class="bar" @submit.prevent="search">
      <el-form-item label="检索词">
        <el-input
          v-model="q"
          placeholder="BM25 全文检索（走 keepsake storage.search）"
          clearable
          style="width: 260px"
          @keyup.enter="search"
        />
      </el-form-item>
      <el-form-item label="标签">
        <el-input
          v-model="tag"
          placeholder="如 core / demo（逗号边界精确匹配）"
          clearable
          style="width: 220px"
          @keyup.enter="search"
        />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="search">查询</el-button>
        <el-button @click="reset">清空</el-button>
      </el-form-item>
    </el-form>

    <el-alert
      v-if="error"
      :title="error"
      type="error"
      show-icon
      :closable="false"
      class="alert"
    />
    <el-alert
      v-else-if="count === null"
      title="当前是全库浏览：存储层没有 count 原语，总条数未知，按 has_more 翻页。"
      type="info"
      show-icon
      :closable="false"
      class="alert"
    />

    <el-table :data="items" v-loading="loading" border size="small" @row-click="open">
      <el-table-column prop="key" label="key" width="220" show-overflow-tooltip />
      <el-table-column prop="tags" label="tags" width="140" show-overflow-tooltip />
      <el-table-column prop="content" label="正文（截断）" show-overflow-tooltip />
      <el-table-column prop="score" label="score" width="100" />
      <el-table-column prop="updated_at" label="updated_at" width="230" show-overflow-tooltip />
      <el-table-column label="操作" width="170" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click.stop="promote(row)">升常驻</el-button>
          <el-button link type="danger" @click.stop="archive(row)">归档</el-button>
          <el-button link @click.stop="open(row)">编辑</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      class="pager"
      layout="total, sizes, prev, pager, next"
      :total="pagerTotal"
      :page-size="pageSize"
      :current-page="page"
      :page-sizes="[20, 50, 100]"
      @current-change="onPage"
      @size-change="onSize"
    />

    <el-drawer v-model="drawer" title="记忆条目全文 / 编辑" size="50%">
      <div v-if="detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="key">{{ detail.key }}</el-descriptions-item>
          <el-descriptions-item label="tags">{{ detail.tags || '—' }}</el-descriptions-item>
          <el-descriptions-item label="created">{{ detail.created_at || '—' }}</el-descriptions-item>
          <el-descriptions-item label="updated">{{ detail.updated_at || '—' }}</el-descriptions-item>
        </el-descriptions>

        <h4>编辑</h4>
        <el-form label-width="60px" size="small">
          <el-form-item label="tags">
            <el-input v-model="editTags" placeholder="逗号分隔，如 core,ops" />
          </el-form-item>
          <el-form-item label="正文">
            <el-input v-model="editContent" type="textarea" :rows="6" />
          </el-form-item>
        </el-form>
        <el-button type="primary" :loading="saving" @click="save">保存修改</el-button>
        <div class="tip">
          改前会自动备份整条原文到 <code>/root/.hermes/backup/console_ops/</code>；
          正文与 tags 同时为空会被后端拒掉（400），想下线请用「归档」。
        </div>

        <el-collapse>
          <el-collapse-item title="其余字段" name="raw">
            <pre class="full">{{ JSON.stringify(detail.raw, null, 2) }}</pre>
          </el-collapse-item>
        </el-collapse>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { showConfirm, showError, showPrompt, showToast } from '../utils/dialog'

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

async function load() {
  loading.value = true
  error.value = ''
  try {
    const data = await api.fragments({
      q: q.value.trim() || undefined,
      tag: tag.value.trim() || undefined,
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
async function archive(row) {
  const expect = row.key.slice(-8)
  const got = await showPrompt(
    `归档 ${row.key} 会把它置为失效（不物理删除，仍可回滚）。请手输 key 末 8 位以确认。`,
    '归档确认',
    { expect, tip: expect },
  )
  if (!got) return
  try {
    await api.archive(row.key, got)
    showToast(`已归档：${row.key}`)
    load()
  } catch (e) {
    showError(`归档失败：${e.message}`)
  }
}

onMounted(load)
</script>

<style scoped>
.bar {
  margin-bottom: 8px;
}
.alert {
  margin-bottom: 12px;
}
.pager {
  margin-top: 12px;
  justify-content: flex-end;
}
.full {
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 4px;
}
.tip {
  margin-top: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
