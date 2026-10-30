<!--
  记忆条目全文 / 编辑抽屉（常驻层与记忆条目共用）。

  卡片只给 3 行摘要，全文、tags、其余字段都在这里；写操作照旧走二次确认，
  保存与归档成功后 emit('saved')，由页面刷新列表 —— 抽屉自己不碰列表数据。
-->
<template>
  <el-drawer
    :model-value="modelValue"
    title="记忆条目全文 / 编辑"
    size="min(620px, 92vw)"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
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
        <!-- rev（2026-10 ks_memory_rev_cas）：乐观并发版本号。改写时要带它当
             expected_rev；版本对不上会被拒绝并回最新内容。 -->
        <div class="row">
          <span class="k">版本</span>
          <span class="v mono">v{{ detail.rev || '1' }}</span>
        </div>
      </div>

      <!-- 演化链（2026-10 ks_memory_rev_cas）：改写一条记忆 = 新条目 + 旧条目
           转链身，所以「这条被改过几版」是一条链，不是一堆散条目。 -->
      <template v-if="chain.length">
        <h3 class="section-title">演化链（{{ chain.length }} 版）</h3>
        <ol class="chain">
          <li v-for="c in chain" :key="c.key" :class="{ 'chain--old': !c.is_latest }">
            <span class="chain-ver mono">v{{ c.rev || '1' }}</span>
            <span class="chain-text" :title="c.content">{{ c.content || '（空）' }}</span>
            <span v-if="c.is_latest" class="chain-cur">当前</span>
          </li>
        </ol>
      </template>

      <h3 class="section-title">编辑</h3>
      <el-form label-position="top">
        <el-form-item label="tags">
          <el-input v-model="editTags" placeholder="逗号分隔，如 core,ops" />
        </el-form-item>
        <el-form-item label="正文">
          <el-input v-model="editContent" type="textarea" :rows="8" />
        </el-form-item>
      </el-form>
      <div class="drawer-acts">
        <el-button type="primary" :loading="saving" @click="save">保存修改</el-button>
      </div>
      <p class="tip">
        改前会自动备份整条原文到 <code>/root/.hermes/backup/console_ops/</code>；
        正文与 tags 同时为空会被后端拒掉（400），想下线请用列表卡片上的「归档」。
      </p>

      <el-collapse class="rawbox">
        <el-collapse-item title="其余字段" name="raw">
          <pre class="pre">{{ JSON.stringify(detail.raw, null, 2) }}</pre>
        </el-collapse-item>
      </el-collapse>
    </div>
  </el-drawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showConfirm, showError, showToast } from '../utils/dialog'
import { toLocal } from '../utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 由页面查好的条目详情；为空则抽屉里没有内容 */
  detail: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const editContent = ref('')
const editTags = ref('')
const saving = ref(false)

/**
 * 演化链（2026-10 ks_memory_rev_cas）：后端已按「最老 → 最新」排好序。
 * 这里只做**展示层**的排序兜底（后端没给 chain 就不显示这一段）。
 */
const chain = computed(() => {
  const rows = Array.isArray(props.detail?.chain) ? props.detail.chain : []
  return rows.map((r) => ({ ...r, is_latest: r.is_latest !== false }))
})

// 每次拿到新详情就把编辑框填上，避免上一条的残留内容
watch(
  () => props.detail,
  (d) => {
    if (!d) return
    editContent.value = d.content || ''
    editTags.value = d.tags || ''
  },
)

async function doCopy(key) {
  const ok = await copyText(key)
  ok ? showToast(`已复制 key：${key}`) : showError('复制失败，请手动选中复制')
}

/** 保存修改：正文 / tags。二次确认后提交，成功通知页面刷新列表。 */
async function save() {
  if (!props.detail) return
  const ok = await showConfirm(
    `确认覆盖 ${props.detail.key} 的正文与标签？写前会自动备份改前原文。`,
    '保存修改',
  )
  if (!ok) return
  saving.value = true
  try {
    const r = await api.updateFragment(props.detail.key, {
      content: editContent.value,
      tags: editTags.value,
    })
    showToast(`已保存 ${props.detail.key}（改动字段：${r.updated.join('、')}）`)
    emit('update:modelValue', false)
    emit('saved')
  } catch (e) {
    showError(`保存失败：${e.message}`)
  } finally {
    saving.value = false
  }
}
</script>

<style scoped>
.drawer-acts {
  display: flex;
  gap: 8px;
}
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.rawbox {
  margin-top: 20px;
}
/* 演化链：竖排版本序列（最老在上、当前在最下） */
.chain {
  margin: 0;
  padding-left: 18px;
  color: var(--text-dim);
  font-size: 13px;
  line-height: 1.8;
}
.chain li {
  margin: 2px 0;
}
.chain--old {
  color: var(--text-faint);
}
.chain-ver {
  margin-right: 6px;
  color: var(--text-faint);
  font-size: 12px;
}
.chain-text {
  word-break: break-all;
}
.chain-cur {
  margin-left: 6px;
  color: var(--accent);
  font-size: 12px;
}
</style>
