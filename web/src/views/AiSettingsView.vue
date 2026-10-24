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
       检测即时调 probe，结果显示在卡内状态行（绿 = 通，红 = 失败 + 原因摘要）。

  2026-10-24 视觉层级重做（**纯样式层**）：卡头（节名大字 + 右侧开关位 +
  一行用途副标）、卡内 label-in 字段栅格、数值三件一行三列、凭据做成整行
  状态条（默认收起，点开才出输入框）、保存=主按钮 / 检测=描边次按钮、
  probe 结果=绿/红状态块（失败可展开看全）。
  **零数据流改动**：接口调用、事件处理函数体、提交载荷一字未动 ——
  展开/收起走原生 <details>，没有新增任何 JS 状态。
-->
<template>
  <div class="ai-page">
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

    <div
      v-for="card in cards"
      :key="card.id"
      class="panel ai-card"
      :class="{ 'ai-card--ro': !card.editable }"
    >
      <!-- 卡头：节名（大字）+ 右侧固定位（开关 / 状态）+ 一行用途副标 -->
      <div class="ai-head">
        <div class="ai-head-main">
          <h2 class="ai-title">{{ card.label }}</h2>
          <!-- 用途副标：GET 给的 note（原样，不另编文案） -->
          <p v-if="card.note" class="ai-sub">{{ card.note }}</p>
          <!-- llm / embedder / 两张只读卡：开关是派生状态，不是能拨的开关 -->
          <p v-if="!card.enabled_editable" class="ai-derived">
            开关由配置派生（接口 / 模型 / 凭据齐了就是开的）
          </p>
        </div>
        <div class="ai-head-side">
          <span v-if="!card.editable" class="chip ai-chip-follow">跟随 llm 节</span>
          <span v-if="card.enabled_editable" class="ai-switch-cap">启用</span>
          <!-- enabled 开关在卡头：screening 是真开关，llm/embedder 是派生状态 -->
          <el-switch v-if="card.enabled_editable" v-model="card.draft.enabled" />
          <span v-else class="chip" :class="card.enabled ? 'chip--accent' : ''">
            {{ card.enabled ? '已开' : '未开' }}
          </span>
        </div>
      </div>

      <p v-if="card.migrate_hint" class="banner banner--danger ai-migrate">
        {{ card.migrate_hint }}
      </p>

      <div v-if="card.fields.length" class="ai-form">
        <div class="ai-grid">
          <div
            v-for="f in card.fields.filter((x) => x.type !== 'number')"
            :key="f.key"
            class="ai-field"
            :class="f.type.startsWith('secret') ? 'ai-field--full' : ''"
          >
            <span class="ai-label">{{ labelOf(card, f) }}</span>
            <!-- 布尔 -->
            <el-switch v-if="f.type === 'bool'" v-model="card.draft[f.key]" />
            <!-- 凭据文件路径：状态条（默认收起，点开才出输入框） -->
            <details v-else-if="f.type === 'secret_path'" class="ai-secret">
              <summary class="ai-secret-bar">
                <code v-if="f.value && f.value.path" class="ai-path">{{ f.value.path }}</code>
                <span class="ai-secret-side">
                  <span class="chip" :class="f.value && f.value.set ? 'ai-chip--ok' : 'chip--warn'">
                    <i v-if="f.value && f.value.set" class="dot-ok"></i>
                    {{ f.value && f.value.set ? '已设置' : '未设置' }}
                  </span>
                  <span class="ai-link">修改</span>
                </span>
              </summary>
              <div class="ai-secret-body">
                <el-input
                  v-model="card.draft[f.key]"
                  :placeholder="f.placeholder"
                  autocomplete="off"
                  spellcheck="false"
                />
              </div>
            </details>
            <!-- 内联凭据：只显示「有没有」，真值永不回显；留空 = 不改 -->
            <details v-else-if="f.type === 'secret_value'" class="ai-secret">
              <summary class="ai-secret-bar">
                <span class="ai-secret-side">
                  <span class="chip" :class="f.value && f.value.set ? 'ai-chip--ok' : 'chip--warn'">
                    <i v-if="f.value && f.value.set" class="dot-ok"></i>
                    {{ f.value && f.value.set ? '已设置' : '未设置' }}
                  </span>
                  <span class="ai-link">重填</span>
                </span>
              </summary>
              <div class="ai-secret-body">
                <el-input
                  v-model="card.secret[f.key]"
                  type="password"
                  :placeholder="f.placeholder"
                  autocomplete="new-password"
                  spellcheck="false"
                />
              </div>
            </details>
            <!-- 普通文本 -->
            <el-input
              v-else
              v-model="card.draft[f.key]"
              :placeholder="f.placeholder"
              autocomplete="off"
              spellcheck="false"
            />
          </div>
        </div>

        <!-- 数值三件：单独一行三列的小网格（上下限全部来自 GET） -->
        <div v-if="card.fields.some((x) => x.type === 'number')" class="ai-nums">
          <div
            v-for="f in card.fields.filter((x) => x.type === 'number')"
            :key="f.key"
            class="ai-field"
          >
            <span class="ai-label">{{ labelOf(card, f) }}</span>
            <el-input-number
              v-model="card.draft[f.key]"
              :min="f.min"
              :max="f.max"
              :step="1"
            />
          </div>
        </div>
      </div>
      <p v-else class="src ai-ro-note">
        这里不编辑 —— 这一节没有独立配置，通道、模型、凭据都在上面那张「记忆提炼/合并」卡里。
      </p>

      <!-- probe 结果：空 = 不占位；通 = 绿边浅底，失败 = 红边浅底（可展开看全） -->
      <details
        v-if="card.editable && card.status.text"
        class="src ai-status"
        :class="{
          'ai-status--ok': card.status.tone === 'ok',
          'ai-status--err': card.status.tone === 'err',
        }"
      >
        <summary class="ai-status-sum">
          <span class="ai-status-ico">{{
            card.status.tone === 'ok' ? '✓' : card.status.tone === 'err' ? '✕' : ''
          }}</span>
          <span class="ai-status-text">{{ card.status.text }}</span>
          <span v-if="card.status.tone === 'err'" class="ai-link ai-caret">▾ 详情</span>
        </summary>
      </details>

      <!-- 卡底：一个保存 + 一个检测（两张只读卡不摆编辑入口） -->
      <div v-if="card.editable" class="ops">
        <el-button :loading="card.probing" :disabled="card.probing" @click="probe(card)">
          检测
        </el-button>
        <el-button type="primary" :loading="card.saving" @click="askSave(card)">保存</el-button>
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
/* ============================================================================
   AI 配置页 · 视觉层级（只写样式与模板结构，不碰数据流）

   令牌全部来自 styles/tokens.css：颜色只写 var(--*)，圆角只用既有六档，
   字号只用 11/12/13/14/16。唯一的例外是「已设置」的浅绿底 ——
   令牌表里没有 ok-soft 这一档，用 --ok 推一档浅底（color-mix，非色值字面量）。
   ========================================================================== */

/* ---- 小字说明（弱，但要看） ---- */
.src {
  margin-top: 10px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
.foot-note {
  margin: 0 0 20px;
}

/* ---- 卡：白底 + 描边 + 16px 圆角（.panel 已给），卡间 20px ---- */
.ai-card {
  margin-bottom: 20px;
  padding: 20px;
}
/* 只读卡整体降饱和：灰底灰边，节名也跟着退一档 */
.ai-card--ro {
  background: var(--panel);
  border-color: var(--border-2);
}
.ai-card--ro .ai-title {
  color: var(--text-dim);
}

/* ---- 卡头：节名（大字）+ 右侧固定位 + 一行用途副标 ---- */
.ai-head {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
}
.ai-head-main {
  min-width: 0;
}
.ai-title {
  margin: 0;
  color: var(--text);
  font-size: 16px;
  font-weight: 600;
  line-height: 1.4;
}
.ai-sub {
  margin: 4px 0 0;
  color: var(--text-faint);
  font-size: 13px;
  line-height: 1.6;
}
/* 派生状态说明：再退一档的小字 */
.ai-derived {
  margin: 4px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.6;
}
.ai-head-side {
  display: flex;
  flex: none;
  gap: 8px;
  align-items: center;
}
.ai-switch-cap {
  color: var(--text-faint);
  font-size: 12px;
}
/* 「跟随 llm 节」：描边小粒，和可编辑卡的实心 chip 区分开 */
.ai-chip-follow {
  background: var(--bg-elev);
  border: 1px solid var(--border-2);
  color: var(--text-faint);
}

/* ---- 迁移提示：红色告警条（语义不可替代处才用色） ---- */
.ai-migrate {
  margin: 16px 0 0;
}

/* ---- 表单区：label 小字在上、控件在下 ---- */
.ai-form {
  margin-top: 16px;
}
/* 字段栅格：列宽下限 320px（窄屏自动收成单列） */
.ai-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 16px 20px;
}
/* 数值三件：一行三列的小网格 */
.ai-nums {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px 20px;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--line-3);
}
.ai-field {
  min-width: 0;
}
/* 凭据字段整行（状态条横铺一整行） */
.ai-field--full {
  grid-column: 1 / -1;
}
.ai-label {
  display: block;
  margin-bottom: 6px;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.5;
}
/* 数字控件占满小格，但不超过一格该有的宽度 */
.ai-nums :deep(.el-input-number) {
  width: 100%;
  max-width: 220px;
}

/* ---- 凭据状态条：左路径、右状态 chip；点开才出输入框（默认收起） ---- */
.ai-secret {
  border: 1px solid var(--border-2);
  border-radius: var(--r-ctl);
  background: var(--panel);
  overflow: hidden;
}
.ai-secret-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  cursor: pointer;
  list-style: none;
}
.ai-secret-bar::-webkit-details-marker {
  display: none;
}
.ai-secret-bar::-moz-details-marker {
  display: none;
}
.ai-secret-side {
  display: flex;
  flex: none;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}
.ai-path {
  min-width: 0;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 12px;
  word-break: break-all;
}
.ai-secret-body {
  padding: 10px 12px 12px;
  border-top: 1px solid var(--border);
  background: var(--bg-elev);
}
/* 绿「已设置」：令牌表无 ok-soft 这一档，用 --ok 推一档浅底 */
.ai-chip--ok {
  background: color-mix(in srgb, var(--ok) 12%, var(--bg-elev));
  color: var(--ok);
}
/* 点得动的小字（原生 details 的 summary，不是按钮） */
.ai-link {
  color: var(--accent);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
}

/* ---- probe 状态块：空 = 不占位；通 = 绿边浅底；失败 = 红边浅底 ---- */
.ai-status {
  margin-top: 16px;
  padding: 10px 14px;
  border: 1px solid var(--border-2);
  border-radius: var(--r-inset);
  background: var(--panel);
  color: var(--text-dim);
  font-size: 13px;
  line-height: 1.6;
}
.ai-status--ok {
  border-color: var(--ok);
  background: color-mix(in srgb, var(--ok) 10%, var(--bg-elev));
}
.ai-status--err {
  border-color: var(--danger);
  background: var(--danger-soft);
}
.ai-status-sum {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  cursor: default;
  list-style: none;
}
.ai-status-sum::-webkit-details-marker {
  display: none;
}
.ai-status-sum::-moz-details-marker {
  display: none;
}
/* 只有失败行可展开（成功行点不动） */
.ai-status--err .ai-status-sum {
  cursor: pointer;
}
.ai-status-ico {
  flex: none;
  color: var(--text-faint);
  font-weight: 600;
}
.ai-status--ok .ai-status-ico {
  color: var(--ok);
}
.ai-status--err .ai-status-ico {
  color: var(--danger);
}
.ai-status-text {
  flex: 1 1 auto;
  min-width: 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
/* 展开 = 不再截断（失败原因看全） */
.ai-status[open] .ai-status-text {
  -webkit-line-clamp: unset;
  line-clamp: unset;
  overflow: visible;
}
.ai-caret {
  flex: none;
  transition: transform 0.15s;
}
.ai-status[open] .ai-caret {
  transform: rotate(180deg);
}

/* ---- 卡底操作行：保存（主按钮）+ 检测（描边次按钮），右对齐一行 ---- */
.ops {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--line-3);
}

/* ---- 只读说明卡 ---- */
.ai-ro-note {
  margin-top: 16px;
}

/* ---- 窄屏：单列不错位（数值三件也收成单列） ---- */
@media (max-width: 900px) {
  .ai-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }
  .ai-nums {
    grid-template-columns: minmax(0, 1fr);
  }
}
@media (max-width: 600px) {
  .ai-head {
    flex-direction: column;
    gap: 8px;
  }
  .ai-head-side {
    align-self: flex-start;
  }
}
</style>
