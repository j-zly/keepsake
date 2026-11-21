<!--
  凭据清单抽屉（ks_multi_credential，2026-11-14 用户拍板：一个代理可以有多把钥匙）。

  为什么是抽屉而不是把清单摊在卡面上：一个代理以后可能有 5~10 把（每台机器一把），
  卡面是网格布局，塞进去会把卡片撑成一堵墙；而"看哪台在跑 / 掐掉某一台"是一次
  有意图的动作，值得一个独立版面。

  🔴 三件事在这里做，别处不许再做一份（同一件事两个入口 = 迟早漂）：
     列出各把 / 新建一把 / 作废单把（+ 改标签、取出明文、登记已有串）。
  🔴 明文只在"签发"与"取出"两个瞬间进过一次内存：不落任何浏览器持久存储、
     不写地址栏、不进日志；关掉抽屉就没了（要再取得重新点「复制」，那会留审计行）。
-->
<template>
  <el-drawer
    :model-value="modelValue"
    :title="`凭据 · ${agent?.name || agent?.agent_id || ''}`"
    size="min(760px, 94vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    @update:model-value="(v) => emit('update:modelValue', v)"
  >
    <p v-if="loadErr" class="tip tip--bad">{{ loadErr }}</p>

    <div v-if="rows.length === 0 && !loading" class="tip">
      {{ agent?.status === 'approved' ? '这个代理名下还没有凭据。' : '未批准的代理没有凭据（批准时才签发）。' }}
    </div>

    <ul v-else class="cl-list">
      <li v-for="r in rows" :key="r.cred_id" class="cl-row" :class="{ 'cl-row--off': r.status !== 'active' }">
        <div class="cl-main">
          <input
            v-if="editing === r.cred_id"
            v-model="editingLabel"
            class="cl-label-input"
            maxlength="40"
            placeholder="用途名"
            @keyup.enter="saveLabel(r)"
            @keyup.esc="editing = ''"
          />
          <button v-else class="cl-label" :disabled="r.status !== 'active'" title="改标签" @click="startEdit(r)">
            {{ r.label || '未命名' }}
          </button>
          <span class="cl-hint mono">{{ r.credential_hint }}…</span>
          <span v-if="r.status !== 'active'" class="cl-tag cl-tag--off">已作废</span>
          <span v-else-if="!r.recoverable" class="cl-tag cl-tag--warn">不可还原</span>
        </div>
        <div class="cl-meta">
          <!-- 授权档位（11-14 用户口径：只授记忆就不能用知识库，反之亦然）。
               直接在行内勾，改完立刻生效——闸门每次工具调用都现查注册表。 -->
          <span class="cl-scopes">
            <el-checkbox-group v-model="r._scopes" size="small" @change="saveScopes(r)">
              <el-checkbox value="memory" label="记忆" />
              <el-checkbox value="kb" label="知识库" />
            </el-checkbox-group>
            <span v-if="r._scopesError" class="cl-scopes-err">没改动（{{ r._scopesError }}）</span>
          </span>
          <span>签于 {{ fmt(r.created_at) }}</span>
          <span>{{ r.status === 'active' ? (r.used_at ? `最近 ${since(r.used_at)}` : '还没用过') : `废于 ${fmt(r.revoked_at || r.used_at)}` }}</span>
          <span>读 {{ r.reads }} · 写 {{ r.writes }}</span>
        </div>
        <div class="cl-acts">
          <el-button
            v-if="r.status === 'active' && r.recoverable"
            size="small"
            :loading="busyId === r.cred_id"
            @click="copyOne(r)"
          >
            复制
          </el-button>
          <el-button v-if="r.status === 'active'" size="small" :disabled="!!busyId" @click="askRevoke(r)">
            作废
          </el-button>
          <el-button v-if="r.status !== 'active'" size="small" :disabled="!!busyId" @click="doEnable(r)">
            启用
          </el-button>
          <el-button v-if="r.status === 'active'" size="small" plain :disabled="!!busyId" @click="askRotate(r)">
            重新生成
          </el-button>
          <el-button size="small" link type="danger" :disabled="!!busyId" @click="askDelete(r)">
            删除
          </el-button>
        </div>
      </li>
    </ul>

    <!-- 不可还原的老凭据：把"登记已有串"这条路摆在这里，而不是让用户去撞 409 -->
    <div v-if="hasUnrecoverable" class="cl-backfill">
      <el-input
        v-model="backfillText"
        type="password"
        show-password
        maxlength="64"
        placeholder="把这个代理正在用的 ksk_ 串粘进来（只登记，不换发）"
      />
      <el-button :disabled="busy || !backfillText.trim()" @click="submitBackfill">登记已有凭据</el-button>
      <p class="tip">对不上账会被整体拒绝，也不会签发新串。</p>
    </div>

    <div class="cl-new">
      <el-input v-model="newLabel" maxlength="40" placeholder="这把给谁用（如 108 执行器 / win 笔记本）" />
      <el-button type="primary" :disabled="!canAdd || busy" @click="addOne">新建一把</el-button>
    </div>
    <p v-if="agent?.status !== 'approved'" class="tip">只有已批准的代理能加凭据（批准时本来就会发一把）。</p>
    <p v-else class="tip">新建不会顶掉任何旧钥匙 —— 要止血请用「重新生成凭据」（旧的当场作废）。</p>

    <ConfirmDialog
      v-model="revokeOpen"
      title="作废这把凭据？"
      :confirm-text="`作废「${revokeRow?.label || '未命名'}」`"
      tone="danger"
      :busy="busy"
      @confirm="doRevoke"
    >
      <p>用它的客户端会<strong>立刻</strong>连不上；这个代理的其余凭据与状态都不受影响。</p>
      <p class="tip">作废后取不出明文（密文一并清掉），历史与用量仍留在清单里。</p>
    </ConfirmDialog>

    <ConfirmDialog
      v-model="rotateOpen"
      title="重新生成这把凭据？"
      :confirm-text="`换发「${rotateRow?.label || '未命名'}」`"
      tone="danger"
      :busy="busy"
      @confirm="doRotate"
    >
      <p>旧钥匙<strong>当场作废</strong>（用它的客户端立刻连不上），并补发一把同标签、同授权的新钥匙。</p>
      <p class="tip">只动这一把 —— 这个代理的其他凭据一概不牵连。新明文只在下面的弹框里显示一次。</p>
    </ConfirmDialog>

    <ConfirmDialog
      v-model="deleteOpen"
      title="删除这把凭据？"
      :confirm-text="`删除「${deleteRow?.label || '未命名'}」`"
      tone="danger"
      :busy="busy"
      @confirm="doDelete"
    >
      <p>这把钥匙将从凭据账上<strong>永久删除，不可恢复</strong>；用它的客户端会立刻连不上。</p>
      <p class="tip">只是想临时停用？用「作废」——留痕且随时可启用。删除前系统会自动留一份快照备份。</p>
    </ConfirmDialog>

    <CredentialDialog
      v-model="credOpen"
      :credential="cred"
      :mode="credMode"
      :agent-id="agent?.agent_id || ''"
      :busy="busy"
    />
  </el-drawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'

import ConfirmDialog from '../components/ConfirmDialog.vue'
import CredentialDialog from '../components/CredentialDialog.vue'
import { api } from '../api'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'
import { since, toLocal } from '../utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 代理行（`GET /api/v1/agents` 的一项）；抽屉只用 agent_id / status / name */
  agent: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'changed'])

const rows = ref([])
const loading = ref(false)
const loadErr = ref('')
const busy = ref(false)
const busyId = ref('')
const newLabel = ref('')
const backfillText = ref('')
const editing = ref('')
const editingLabel = ref('')
const cred = ref('')
const credMode = ref('issue')
const credOpen = ref(false)
const revokeOpen = ref(false)
const revokeRow = ref(null)
const rotateOpen = ref(false)
const rotateRow = ref(null)
const deleteOpen = ref(false)
const deleteRow = ref(null)

const canAdd = computed(() => props.agent?.status === 'approved')
const hasUnrecoverable = computed(() =>
  rows.value.some((r) => r.status === 'active' && !r.recoverable))

function fmt(iso) {
  if (!iso) return '—'
  const text = toLocal(iso)
  return text && text !== 'Invalid Date' ? text : '—'
}

async function load() {
  const aid = props.agent?.agent_id
  if (!aid) return
  loading.value = true
  loadErr.value = ''
  try {
    const r = await api.agentCredentials(aid)
    // `_scopes` 是行内的编辑态副本（el-checkbox-group 要可写数组）；
    // 每次开抽屉都从服务端重读 ⇒ 不在本地留旧值，避免"界面勾着、实际没生效"。
    rows.value = (r.items || []).map((c) => ({ ...c, _scopes: [...(c.scopes || [])], _scopesError: '' }))
  } catch (e) {
    loadErr.value = `读不到凭据清单（${e?.message || '未知错误'}）`
    rows.value = []
  } finally {
    loading.value = false
  }
}

// 打开一次拉一次：凭据的"最近使用"是遥测，缓存 5 分钟就会让人误判"这台机器还活着"。
watch(() => props.modelValue, (open) => {
  if (open) load()
})

async function addOne() {
  const aid = props.agent?.agent_id
  busy.value = true
  try {
    const r = await api.addCredential(aid, newLabel.value.trim())
    if (!r?.credential) {
      showError('没拿到新凭据（服务端未返回）')
      return
    }
    newLabel.value = ''
    cred.value = r.credential
    credMode.value = 'issue'
    credOpen.value = true
    showToast(`已新建「${r.label || '未命名'}」，旧钥匙一把都没动`)
    await load()
    emit('changed')
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '新建失败')
  } finally {
    busy.value = false
  }
}

// 显示明文的弹框一关就把这份串从内存里抹掉（不指望用户"记得关掉"）
watch(credOpen, (open) => {
  if (!open) cred.value = ''
})

async function copyOne(r) {
  busyId.value = r.cred_id
  try {
    const res = await api.revealCredential(props.agent.agent_id, r.cred_id)
    if (!res?.credential) {
      showError(res?.note || '取不出明文')
      return
    }
    const ok = await copyText(res.credential)
    if (ok) {
      showToast(`已复制「${r.label || '未命名'}」的完整凭据`)
    } else {
      // 剪贴板被浏览器挡了要给一条活路：显示出来让人手选，别让人以为功能坏了
      cred.value = res.credential
      credMode.value = 'reveal'
      credOpen.value = true
      showToast('剪贴板不可用，已在弹框里显示，可手动全选复制')
    }
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '取出失败')
  } finally {
    busyId.value = ''
  }
}

function askRevoke(r) {
  revokeRow.value = r
  revokeOpen.value = true
}

async function doRevoke() {
  const r = revokeRow.value
  if (!r) return
  busy.value = true
  try {
    await api.revokeCredential(props.agent.agent_id, r.cred_id)
    revokeOpen.value = false
    showToast(`已作废「${r.label || '未命名'}」`)
    await load()
    emit('changed')
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '作废失败')
  } finally {
    busy.value = false
  }
}

async function doEnable(r) {
  busy.value = true
  try {
    await api.enableCredential(props.agent.agent_id, r.cred_id)
    showToast(`已启用「${r.label || '未命名'}」——客户端手里的旧串立刻可用`)
    await load()
    emit('changed')
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '启用失败')
  } finally {
    busy.value = false
  }
}

function askRotate(r) {
  rotateRow.value = r
  rotateOpen.value = true
}

function askDelete(r) {
  deleteRow.value = r
  deleteOpen.value = true
}

async function doRotate() {
  const r = rotateRow.value
  if (!r) return
  busy.value = true
  try {
    const out = await api.rotateCredential(props.agent.agent_id, r.cred_id)
    rotateOpen.value = false
    cred.value = String(out.credential || '')
    credMode.value = 'new'
    credOpen.value = true
    await load()
    emit('changed')
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '换发失败')
  } finally {
    busy.value = false
  }
}

async function doDelete() {
  const r = deleteRow.value
  if (!r) return
  busy.value = true
  try {
    await api.deleteCredential(props.agent.agent_id, r.cred_id, true)
    deleteOpen.value = false
    showToast(`已删除「${r.label || '未命名'}」（删前快照已落审计备份）`)
    await load()
    emit('changed')
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '删除失败')
  } finally {
    busy.value = false
  }
}

function startEdit(r) {
  editing.value = r.cred_id

  editingLabel.value = r.label || ''
}

async function saveLabel(r) {
  const text = editingLabel.value.trim()
  if (!text || text === (r.label || '')) {
    editing.value = ''
    return
  }
  try {
    await api.renameCredential(props.agent.agent_id, r.cred_id, text)
    showToast('标签已改（不影响这把钥匙能不能用）')
    await load()
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '改标签失败')
  } finally {
    editing.value = ''
  }
}

async function saveScopes(r) {
  // 只有勾法真变了才发请求（否则一次点击就是一条无意义的审计行）
  const next = [...(r._scopes || [])].sort()
  const prev = [...(r.scopes || [])].sort()
  if (next.join(',') === prev.join(',')) return
  const wanted = ['memory', 'kb'].filter((s) => next.includes(s))
  try {
    const res = await api.setCredentialScopes(props.agent.agent_id, r.cred_id, wanted)
    r.scopes = [...(res.scopes || wanted)]
    r._scopes = [...r.scopes]
    r._scopesError = ''
    const names = { memory: '记忆', kb: '知识库' }
    showToast(`授权已更新：${r.label || '未命名'} → ${r.scopes.map((s) => names[s] || s).join('、') || '无'}`)
    emit('changed')
  } catch (e) {
    // 失败要**回滚勾选态**：界面留着勾上的样子 = 骗人
    r._scopes = [...(r.scopes || [])]
    r._scopesError = e?.response?.data?.detail?.message || e?.message || '保存失败'
    showError(r._scopesError)
  }
}

async function submitBackfill() {
  const aid = props.agent?.agent_id
  const text = backfillText.value.trim()
  if (!text) return
  busy.value = true
  try {
    await api.backfillCredential(aid, text)
    showToast('已登记，这把现在可以重复复制了')
    await load()
    emit('changed')
  } catch (e) {
    showError(e?.response?.data?.detail?.message || e?.message || '登记失败')
  } finally {
    // 成功与失败都要擦：粘错一次不该让明文继续挂在输入框里
    backfillText.value = ''
    busy.value = false
  }
}
</script>

<style scoped>
.cl-list {
  list-style: none;
  margin: 0 0 16px;
  padding: 0;
  display: grid;
  gap: 10px;
}

.cl-row {
  border: 1px solid var(--border);
  border-radius: var(--r-12, 12px);
  background: var(--panel);
  padding: 12px 14px;
  display: grid;
  gap: 6px;
}

.cl-row--off {
  opacity: 0.62;
}

.cl-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.cl-label {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  font-weight: 600;
  color: var(--text);
  cursor: pointer;
  border-bottom: 1px dashed var(--border);
}

.cl-label:disabled {
  cursor: default;
  border-bottom: none;
}

.cl-label-input {
  border: 1px solid var(--accent);
  border-radius: 8px;
  padding: 2px 8px;
  font: inherit;
  color: var(--text);
  background: var(--panel);
}

.cl-hint {
  color: var(--text-2);
  font-size: 13px;
}

.mono {
  font-family: var(--mono);
}

.cl-tag {
  border-radius: var(--r-6, 6px);
  padding: 1px 8px;
  font-size: 12px;
  border: 1px solid var(--border);
  color: var(--text-2);
}

.cl-tag--warn {
  border-color: var(--warn);
  color: var(--warn);
}

.cl-tag--off {
  border-color: var(--border);
  color: var(--text-3, var(--text-2));
}

.cl-scopes {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.cl-scopes-err {
  color: var(--danger);
  font-size: 12px;
}

.cl-meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-3, var(--text-2));
}

.cl-acts {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.cl-new {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-top: 6px;
}

.cl-new .el-input {
  max-width: 360px;
}

.cl-backfill {
  display: grid;
  gap: 8px;
  border: 1px dashed var(--border);
  border-radius: var(--r-12, 12px);
  padding: 12px 14px;
  margin-bottom: 14px;
}

.tip {
  color: var(--text-2);
  font-size: 12px;
  margin: 6px 0 0;
}

.tip--bad {
  color: var(--danger);
}
</style>
