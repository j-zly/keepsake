<!--
  一次性凭据弹框（代理登记 / 代理审核**两页共用同一份**）。

  为什么抽成组件：批准动作在两页都有（登记页 = 已吊销的重新批准，审核页 = 待审的批准），
  凭据明文**只回来一次**、只显示一次 —— 这段逻辑复制两份就一定漂移（一份多一个「复制」、
  一份忘了关窗抹掉），所以只此一份实现，两页都调它。

  🔴 凭据纪律（不可让步）：明文只在父页的 cred ref 里活一次，弹框一关就 emit('closed')，
  父页随即把明文抹掉；不进 URL、不进浏览器存储、不进日志。
  复制只到剪贴板为止，转交由人自己走别的渠道。

  2026-11-14 加可还原档（用户拍板「不能换发，要直接能复制」）之后，本组件多一个
  `mode`：`issue`（签发/换发，回到旧语义时要说"关掉就没了"）与 `reveal`（从服务端加密
  存档取出，随时能再来一次，每次取出都有审计）。两档**说的是不同的事实**，所以文案分档，
  不许把旧口径那句话原样贴在新档上（那会让用户以为没复制成功就完蛋，进而乱点换发）。

  closeOnClickModal: false —— 误点遮罩不该等于「凭据作废」。
-->
<template>
  <el-dialog
    :model-value="modelValue"
    title="一次性凭据"
    width="520px"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="!busy"
    :show-close="!busy"
    append-to-body
    @update:model-value="onDismiss"
    @closed="onClosed"
  >
    <p class="warn">{{ warnText }}</p>
    <pre class="pre">{{ credential }}</pre>
    <!-- 🔴 同时给一条**可选中**的只读输入框：`<pre>` 里的长串缩放后可能选不全，
         而剪贴板 API 在非安全上下文里会整体不可用（clipboard.js 的 execCommand 降级
         也只是尽力而为）—— 手抄也必须有一条错不了的路。绑 prop 渲染，不另存副本。 -->
    <input
      class="cred-plain"
      type="text"
      readonly
      :value="credential"
      spellcheck="false"
      autocomplete="off"
      aria-label="凭据明文"
      @focus="$event.target.select()"
    />
    <div class="cred-acts">
      <el-button type="primary" @click="doCopy">复制凭据</el-button>
    </div>
    <p class="tip">{{ note }}</p>
    <template #footer>
      <el-button :disabled="busy" @click="close">{{ mode === 'reveal' ? '我已复制，关掉' : '我已转交，关掉' }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed } from 'vue'
import { copyText } from '../utils/clipboard'
import { showError, showToast } from '../utils/dialog'

const props = defineProps({
  /** 弹框开合（父页 v-model 绑自己那个 credOpen） */
  modelValue: { type: Boolean, default: false },
  /** 凭据明文：只从父页的 cred ref 传进来，不在本组件里另存一份 */
  credential: { type: String, default: '' },
  /** 随凭据一起回来的那句提示（后端给的 note） */
  note: { type: String, default: '' },
  /** 父页正在写库时禁掉关闭（免得半路关窗把状态搞花） */
  busy: { type: Boolean, default: false },
  /** `issue` = 签发 / 换发；`reveal` = 从加密存档取出（可重复）。只影响文案口径。 */
  mode: { type: String, default: 'issue' },
})

/** 两档说的是不同的事实 —— 文案跟着事实走（见文件头的口径说明）。 */
const warnText = computed(() =>
  props.mode === 'reveal' ? '服务端解密出来的明文，可随时再取。' : '请立即转交给代理进程。',
)

const emit = defineEmits(['update:modelValue', 'closed'])

/** 复制到剪贴板；失败要说人话（凭据形态长，只靠手抄容易错） */
async function doCopy() {
  const ok = await copyText(props.credential)
  // 🔴 成功提示**不回显凭据**：旧写法 `已复制：${credential}` 会把整把钥匙打进
  //    屏幕上的 toast —— 截图 / 录屏 / 投屏都收得到，与"明文不进日志"是同一条纪律。
  ok ? showToast('已复制到剪贴板') : showError('复制失败，请用下面那个输入框手动全选复制')
}

function close() {
  emit('update:modelValue', false)
}

/** ESC / 点 X 传来的 false 也要拦住 busy 期间的状态变化 */
function onDismiss(v) {
  if (!v) close()
}

/** 弹框关完（动画结束）才通知父页抹掉明文 —— 免得关窗瞬间先清空、用户没抄完 */
function onClosed() {
  emit('closed')
}
</script>

<style scoped>
/* 一次性凭据弹框：红色提醒 + 全文 + 复制 */
.warn {
  margin: 0 0 12px;
  color: var(--danger);
  font-size: 13px;
  font-weight: 500;
  line-height: 1.65;
}
.cred-plain {
  display: block;
  box-sizing: border-box;
  width: 100%;
  margin-top: 8px;
  padding: 8px 10px;
  font-family: var(--mono);
  font-size: 12px;
  color: var(--text);
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 8px;
}
.cred-acts {
  margin-top: 12px;
}
.tip {
  margin: 12px 0 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}
</style>
