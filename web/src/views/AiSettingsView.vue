<!--
  AI 配置页（2026-10 ai_settings_page）。

  五处模型调用的**唯一配置源都是 config.json** —— 这一页把它集中到五张卡：
    llm / embedder / screening 可写；query_expansion / tiering_review
    没有独立配置（复用 llm 节）⇒ 只读说明卡，**不摆编辑入口**。

  三条纪律：
    1. **数字全部来自 GET**（连输入框的上下限都是后端给的 min/max）—— 前端零写死；
    2. **凭据不显示**：secret 字段只显示「已设置 / 未设置」徽标 + 路径，
       输入框留空 = 不改（新凭据由后端写进 600 权限的密钥文件）；
    3. **一张卡一个保存 + 一个检测**：保存走既有重确认件（后果逐条列明），
       检测即时调 probe，结果显示在卡内状态行（绿点 = 通，红字 = 失败 + 原因摘要）。
-->
<template>
  <div>
    <div class="page-head">
      <h1 class="page-title">AI 配置</h1>
      <p class="page-sub">
        记忆提炼/合并、向量检索、检索筛选三处调用的唯一配置源都是 config.json；
        这里直接改它，保存后热生效，并可逐节做连通检测。
      </p>
    </div>

    <div v-if="error" class="banner banner--danger">{{ error }}</div>

    <p class="src foot-note">
      配置写在 {{ meta.path || 'config.json' }}；写前整份备份到 {{ meta.backup_dir || '备份目录' }}；
      新凭据写进 {{ meta.keys_dir || '密钥目录' }}（600 权限，明文不落 config.json）。
    </p>

    <div v-for="card in cards" :key="card.id" class="panel pad ai-card">
      <div class="ai-head">
        <span class="ai-title">{{ card.label }}</span>
        <span class="chip" :class="card.enabled ? 'chip--accent' : ''">
          {{ card.enabled ? '已开' : '未开' }}
        </span>
        <!-- enabled 开关在卡头：screening 是真开关，llm/embedder 是派生状态 -->
        <el-switch v-if="card.enabled_editable" v-model="card.draft.enabled" />
        <span v-else class="src ai-derived">开关由配置派生（接口 / 模型 / 凭据齐了就是开的）</span>
      </div>

      <p v-if="card.note" class="src">{{ card.note }}</p>
      <p v-if="card.migrate_hint" class="banner banner--danger ai-migrate">
        {{ card.migrate_hint }}
      </p>

      <el-form v-if="card.fields.length" label-position="top" class="ai-form">
        <div class="ai-grid">
          <el-form-item v-for="f in card.fields" :key="f.key" :label="labelOf(card, f)">
            <!-- 数字：min/max 全部来自 GET（前端零写死） -->
            <el-input-number
              v-if="f.type === 'number'"
              v-model="card.draft[f.key]"
              :min="f.min"
              :max="f.max"
              :step="1"
            />
            <!-- 布尔 -->
            <el-switch v-else-if="f.type === 'bool'" v-model="card.draft[f.key]" />
            <!-- 凭据文件路径：徽标 + 路径（路径可改，文件内容永不显示） -->
            <template v-else-if="f.type === 'secret_path'">
              <div class="ai-secret">
                <span class="chip" :class="f.value && f.value.set ? '' : 'chip--warn'">
                  <i v-if="f.value && f.value.set" class="dot-ok"></i>
                  {{ f.value && f.value.set ? '已设置' : '未设置' }}
                </span>
                <code v-if="f.value && f.value.path" class="ai-path">{{ f.value.path }}</code>
              </div>
              <el-input
                v-model="card.draft[f.key]"
                :placeholder="f.placeholder"
                autocomplete="off"
                spellcheck="false"
              />
            </template>
            <!-- 内联凭据：只显示「有没有」，真值永不回显；留空 = 不改 -->
            <template v-else-if="f.type === 'secret_value'">
              <div class="ai-secret">
                <span class="chip" :class="f.value && f.value.set ? '' : 'chip--warn'">
                  <i v-if="f.value && f.value.set" class="dot-ok"></i>
                  {{ f.value && f.value.set ? '已设置' : '未设置' }}
                </span>
              </div>
              <el-input
                v-model="card.secret[f.key]"
                type="password"
                :placeholder="f.placeholder"
                autocomplete="new-password"
                spellcheck="false"
              />
            </template>
            <!-- 普通文本 -->
            <el-input
              v-else
              v-model="card.draft[f.key]"
              :placeholder="f.placeholder"
              autocomplete="off"
              spellcheck="false"
            />
          </el-form-item>
        </div>
      </el-form>
      <p v-else class="src">这一节没有独立配置 —— 通道、模型、凭据都在上面那张「记忆提炼/合并」卡里。</p>

      <!-- 卡底：一个保存 + 一个检测（两张只读卡不摆编辑入口） -->
      <div v-if="card.editable" class="ops">
        <el-button type="primary" :loading="card.saving" @click="askSave(card)">保存</el-button>
        <el-button :loading="card.probing" :disabled="card.probing" @click="probe(card)">
          检测
        </el-button>
        <span class="src ai-status" :class="{ 'ai-status--err': card.status.tone === 'err' }">
          <i v-if="card.status.tone === 'ok'" class="dot-ok"></i>
          {{ card.status.text }}
        </span>
      </div>
    </div>

    <!-- 保存前的重确认件：后果逐条列明（改哪一节 / 备份去哪 / 怎么生效） -->
    <ConfirmDialog
      v-model="confirmOpen"
      :title="confirmTitle"
      :consequences="confirmConsequences"
      confirm-text="确认保存"
      tone="primary"
      :busy="saving"
      @confirm="doSave"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import { showError, showToast } from '../utils/dialog'

const cards = ref([])
const meta = ref({ path: '', backup_dir: '', keys_dir: '' })
const error = ref('')
const confirmOpen = ref(false)
const saveTarget = ref(null)
const saving = ref(false)

/** GET 的五节 → 页面卡片（字段/数值/上下限**全部来自后端**，前端零写死）。 */
function buildCards(sections) {
  return (sections || []).map((s) => {
    const draft = {}
    const secret = {}
    for (const f of s.fields || []) {
      if (f.type === 'secret_value') {
        secret[f.key] = ''          // 留空 = 不改
        continue
      }
      if (f.type === 'secret_path') {
        draft[f.key] = (f.value && f.value.path) || ''
        continue
      }
      draft[f.key] = f.value
    }
    if (s.enabled_editable) draft.enabled = !!s.enabled
    return {
      id: s.id,
      label: s.label,
      note: s.note || '',
      migrate_hint: s.migrate_hint || '',
      enabled: !!s.enabled,
      enabled_editable: !!s.enabled_editable,
      editable: !!s.editable,
      fields: s.fields || [],
      draft,
      secret,
      saving: false,
      probing: false,
      status: { tone: '', text: '' },
    }
  })
}

async function load() {
  error.value = ''
  try {
    const data = await api.aiSettings()
    cards.value = buildCards(data.sections)
    meta.value = {
      path: data.path || '',
      backup_dir: data.backup_dir || '',
      keys_dir: data.keys_dir || '',
    }
  } catch (e) {
    error.value = `读取 AI 配置失败：${e.message}`
  }
}

/** 字段的中文标签（后端只给 key，标签在前端一处翻译，别处不重写一遍）。 */
const LABELS = {
  base_url: '接口地址',
  model: '模型',
  provider: '服务商（openai / dashscope / 自定义）',
  key_file: '凭据文件路径',
  api_key_file: '凭据文件路径',
  api_key: '凭据',
  default_on: '默认开（调用方没显式传 screen 时按筛处理）',
  max_pick: '最多保留条数',
  timeout_s: '单次超时（秒）',
  max_tokens: '单次最多 token',
}
function labelOf(card, f) {
  return LABELS[f.key] || f.key
}

/** 本次要提交的凭据字段（留空的不算）。 */
function secretFilled(card) {
  return Object.entries(card.secret).filter(([, v]) => v && v.trim()).map(([k]) => k)
}

const confirmTitle = computed(() => `保存「${saveTarget.value?.label || ''}」配置？`)

/** 确认弹框里逐条列后果（不是一句「确定吗」）。 */
const confirmConsequences = computed(() => {
  const c = saveTarget.value
  if (!c) return []
  const out = [
    `只改 config.json 的 ${c.id} 一节 —— 存储后端、班车站等其余配置一字不动。`,
    `写前会把原 config 整份备份到 ${meta.value.backup_dir || '备份目录'}（可人工回退）。`,
  ]
  if (secretFilled(c).length) {
    out.push('新凭据会写进 600 权限的密钥文件，config.json 里只留路径（明文不落盘）。')
  }
  out.push('热生效：改完下一轮调用就用新值，llm / 筛选不需重启任何进程。')
  if (c.id === 'embedder') {
    out.push('向量通道在 MCP 进程启动时建：改完 embedder 要重启 MCP 服务才生效。')
  }
  return out
})

function askSave(card) {
  saveTarget.value = card
  confirmOpen.value = true
}

async function doSave() {
  const card = saveTarget.value
  if (!card) return
  confirmOpen.value = false
  saving.value = true
  card.saving = true
  try {
    const payload = { ...card.draft }
    for (const [k, v] of Object.entries(card.secret)) {
      if (v && v.trim()) payload[k] = v.trim()
    }
    const r = await api.setAiSettings(card.id, payload)
    showToast(`已保存「${card.label}」配置`)
    if (r && r.notes && r.notes.length) showToast(r.notes.join(' '))
    await load()
  } catch (e) {
    showError(`保存「${card.label}」配置失败：${e.message}`)
  } finally {
    saving.value = false
    card.saving = false
  }
}

/** 成功时的状态文案：各节细节不同（finish_reason / 维度 / 选中条数）。 */
function okText(r) {
  const bits = []
  if (r.latency_ms !== undefined) bits.push(`${r.latency_ms}ms`)
  if (r.finish_reason) bits.push(`finish_reason=${r.finish_reason}`)
  if (r.dimension !== undefined && r.dimension !== null) bits.push(`维度 ${r.dimension}`)
  if (r.picked) bits.push(`选中 ${r.picked.length}/${r.of}`)
  return `连通正常${bits.length ? ' · ' + bits.join(' · ') : ''}`
}

/** 失败时的状态文案：原因摘要（**不含凭据** —— 后端已经抹过一遍）。 */
function failText(r) {
  const bits = []
  if (r.status) bits.push(`HTTP ${r.status}`)
  if (r.error) bits.push(r.error)
  if (r.message) bits.push(r.message)
  return `失败${bits.length ? ' · ' + bits.join(' · ') : ''}`
}

async function probe(card) {
  card.probing = true
  card.status = { tone: '', text: '检测中…' }
  try {
    const r = await api.probeAiSettings(card.id)
    card.status = r && r.ok
      ? { tone: 'ok', text: okText(r) }
      : { tone: 'err', text: failText(r || {}) }
  } catch (e) {
    card.status = { tone: 'err', text: `失败 · ${e.message}` }
  } finally {
    card.probing = false
  }
}

onMounted(load)
</script>

<style scoped>
.ai-card {
  margin-bottom: 16px;
}
.pad {
  padding: 18px 20px;
}
.ai-head {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
}
.ai-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.ai-derived {
  margin-top: 0;
}
/* 迁移提示：红色告警条（语义不可替代处才用色） */
.ai-migrate {
  margin: 10px 0 0;
}
.ai-form {
  margin-top: 14px;
}
/* 两列栅格：短字段（模型/开关）并排，长字段（接口地址）自己占一整行 */
.ai-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 0 20px;
}
.ai-secret {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.ai-path {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11.5px;
  word-break: break-all;
}
.ops {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin-top: 14px;
}
.ai-status {
  margin-top: 0;
  color: var(--text-dim);
}
.ai-status--err {
  color: var(--danger);
}
.src {
  margin-top: 10px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.foot-note {
  margin: 0 0 16px;
}
</style>
