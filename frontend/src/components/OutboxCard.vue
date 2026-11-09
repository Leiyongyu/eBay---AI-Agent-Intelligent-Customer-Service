<script setup lang="ts">
// 发件箱里的消息：倒计时结束前可以撤回；发出后进聊天记录，这张卡片消失
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { post } from '../api'
import { errorText, fmtTime } from '../format'
import type { OutboxItem } from '../types'

const props = defineProps<{ item: OutboxItem }>()
const emit = defineEmits<{ recalled: [text: string] }>()

const SOURCE: Record<string, string> = {
  manual: '人工回复', auto: 'AI 自动回复', ack: '已收到回执', ticket: '查件结果', logistics_notice: '查件提醒',
  ship_notice: '发货通知', invoice_notice: '发票提醒',
}
const now = ref(Date.now())
let timer: number | undefined
onMounted(() => (timer = window.setInterval(() => (now.value = Date.now()), 1000)))
onUnmounted(() => clearInterval(timer))

const left = computed(() => Math.max(0, Math.round((Date.parse(props.item.send_after) - now.value) / 1000)))
const countdown = computed(() => `${Math.floor(left.value / 60)}:${String(left.value % 60).padStart(2, '0')}`)
const busy = ref(false)

async function recall() {
  try {
    await ElMessageBox.confirm('撤回这条还没发出的消息？撤回后不会发给买家，内容会放回输入框，可以改了再发。', '撤回消息',
                               { type: 'warning', confirmButtonText: '撤回' })
  } catch {
    return
  }
  busy.value = true
  try {
    await post(`/internal/outbox/${props.item.id}/recall`)
    ElMessage.success('已撤回，没有发出')
    emit('recalled', props.item.body)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="msg-row seller">
    <div class="outbox" :class="item.state">
      <div class="head">
        <el-tag size="small" :type="item.state === 'PENDING' ? 'warning' : item.state === 'RECALLED' ? 'info' : item.state === 'SENDING' ? 'primary' : 'danger'">
          {{ item.state === 'PENDING' ? `待发送 · ${countdown} 后发出` : item.state === 'SENDING' ? '正在发送'
            : item.state === 'RECALLED' ? '已撤回（没有发出）' : item.state === 'FAILED' ? '没有发出' : '结果不确定' }}
        </el-tag>
        <span class="muted small">{{ SOURCE[item.source] || item.source }} · {{ item.actor }} · {{ fmtTime(item.created_at) }}</span>
        <el-button v-if="item.state === 'PENDING'" size="small" type="danger" plain :loading="busy" @click="recall">撤回</el-button>
      </div>
      <div class="body">{{ item.body }}</div>
      <div v-if="item.zh_translation" class="muted small zh">{{ item.zh_translation }}</div>
      <div v-if="item.error && item.state !== 'RECALLED'" class="err small">{{ item.error }}</div>
      <div v-if="item.state === 'UNCERTAIN'" class="err small">请到 eBay 后台这个会话里看买家是否收到，没收到再手动重发（系统不会自动重发，免得发两遍）。</div>
    </div>
  </div>
</template>

<style scoped>
.outbox {
  max-width: 78%;
  border: 1px dashed var(--el-color-warning, #e6a23c);
  border-radius: 8px;
  padding: 8px 12px;
  background: var(--el-color-warning-light-9, #fdf6ec);
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-left: auto;
}
.outbox.RECALLED {
  border-color: var(--line);
  background: var(--panel-soft, #f7f9fb);
  opacity: 0.75;
}
.outbox.FAILED,
.outbox.UNCERTAIN {
  border-color: var(--el-color-danger, #f56c6c);
  background: var(--el-color-danger-light-9, #fef0f0);
}
.head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.body {
  white-space: pre-wrap;
}
.zh {
  white-space: pre-wrap;
}
.small {
  font-size: var(--fs-xs, 12px);
}
.err {
  color: var(--el-color-danger, #f56c6c);
}
</style>
