<!--
  一次性凭据弹框（代理登记 / 代理审核**两页共用同一份**）。

  为什么抽成组件：批准动作在两页都有（登记页 = 已吊销的重新批准，审核页 = 待审的批准），
  凭据明文**只回来一次**、只显示一次 —— 这段逻辑复制两份就一定漂移（一份多一个「复制」、
  一份忘了关窗抹掉），所以只此一份实现，两页都调它。

  🔴 凭据纪律（不可让步）：明文只在父页的 cred ref 里活一次，弹框一关就 emit('closed')，
  父页随即把明文抹掉；不进 URL、不进浏览器存储、不进日志。
  复制只到剪贴板为止，转交由人自己走别的渠道。

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
    <p class="warn">只显示这一次，请立即转交给代理进程 —— 关掉这个弹框就再也拿不到了。</p>
    <pre class="pre">{{ credential }}</pre>
    <div class="cred-acts">
      <el-button @click="doCopy">复制凭据</el-button>
    </div>
    <p class="tip">{{ note }}</p>
    <template #footer>
      <el-button :disabled="busy" @click="close">我已转交，关掉</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
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
})

const emit = defineEmits(['update:modelValue', 'closed'])

/** 复制到剪贴板；失败要说人话（凭据形态长，只靠手抄容易错） */
async function doCopy() {
  const ok = await copyText(props.credential)
  ok ? showToast(`已复制：${props.credential}`) : showError('复制失败，请手动选中复制')
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
