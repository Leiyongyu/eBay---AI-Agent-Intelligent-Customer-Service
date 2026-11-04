<script setup lang="ts">
// 会话里的售后附件区：这个买家通过售后邮箱或上传页发来的照片、视频；可以生成上传链接（贴进发给买家的邮件），
// 一键把“已收到”的确认回复填进回复框
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { errorText } from '../format'
import AftersalesMailCard from './AftersalesMailCard.vue'
import type { AftersalesMailItem } from '../types'

const props = defineProps<{ convId: number; marketplace?: string; refreshKey?: string }>()
const emit = defineEmits<{ insert: [text: string] }>()

const items = ref<AftersalesMailItem[]>([])
const open = ref(true)
const uploadReady = ref(false)
const uploadReasons = ref<string[]>([])
const pendingLinks = ref<{ id: number; order_id: string; expires_at: string | null; created_by: string | null }[]>([])
const fileCount = computed(() => items.value.reduce((n, m) => n + (m.kind === 'upload' ? m.files.filter((f) => f.status === 'STORED').length : m.files_saved), 0))

async function load() {
  try {
    const r = await get<{ allowed: boolean; items: AftersalesMailItem[]; uploads: AftersalesMailItem[]; upload_ready: boolean;
                          upload_not_ready_reasons: string[]; pending_links: typeof pendingLinks.value }>(
      `/internal/conversations/${props.convId}/aftersales-files`)
    items.value = r.allowed ? [...(r.uploads || []), ...r.items] : []
    uploadReady.value = r.upload_ready
    uploadReasons.value = r.upload_not_ready_reasons || []
    pendingLinks.value = r.pending_links || []
  } catch {
    items.value = []
  }
}

// 生成上传链接：客服复制后贴进发给买家的邮件（链接不能放进 eBay 消息）
const linkDialog = ref(false)
const linkOrder = ref('')
const linkUrl = ref('')
const linkExpires = ref('')
const orders = ref<{ order_id: string; created_at: string | null; title: string | null }[]>([])
async function openLink() {
  linkUrl.value = ''
  linkDialog.value = true
  try {
    const r = await get<{ orders: typeof orders.value }>(`/internal/conversations/${props.convId}/invoice`)
    orders.value = r.orders || []
    if (!linkOrder.value && orders.value.length === 1) linkOrder.value = orders.value[0].order_id
  } catch {
    orders.value = []
  }
}
async function makeLink() {
  try {
    const r = await post<{ url: string; expires_at: string }>(`/internal/conversations/${props.convId}/upload-link`, { order_id: linkOrder.value.trim() })
    linkUrl.value = r.url
    linkExpires.value = r.expires_at
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}
async function copyLink() {
  try {
    await navigator.clipboard.writeText(linkUrl.value)
    ElMessage.success('已复制，贴到发给买家的邮件里')
  } catch {
    ElMessage.warning('复制失败，请手动选中复制')
  }
}
watch(() => [props.convId, props.refreshKey], load, { immediate: true })

const RECEIVED: Record<string, string> = {
  de: 'Guten Tag,\n\nvielen Dank. Wir haben Ihre Fotos/Videos erhalten und prüfen sie jetzt. Wir melden uns so schnell wie möglich bei Ihnen.\n\nMit freundlichen Grüßen',
  en: 'Hello,\n\nThank you. We have received your photos/videos and are checking them now. We will get back to you as soon as possible.\n\nKind regards',
}

function insertReceived() {
  const lang = ['EBAY_DE', 'EBAY_AT', 'EBAY_CH'].includes(props.marketplace || '') ? 'de' : 'en'
  emit('insert', RECEIVED[lang])
  ElMessage.success('已把“已收到”回复填进回复框，核对后发送')
}

async function claim(mail: AftersalesMailItem) {
  try {
    const { value } = await ElMessageBox.prompt('改成关联到哪个订单？', '改关联', {
      inputValue: mail.order_id || '',
      inputValidator: (v) => /^\d{2}-\d{5}-\d{5}$/.test((v || '').trim()) || '订单号格式如 12-34567-89012' })
    await post(`/internal/aftersales-mails/${mail.id}/claim`, { order_id: value.trim() })
    load()
  } catch (e) {
    if (e !== 'cancel' && e !== 'close') ElMessage.error(errorText(e))
  }
}
</script>

<template>
  <div class="as-box" :class="{ empty: !items.length }">
    <div class="as-head">
      <el-tag size="small" :type="items.length ? 'primary' : 'info'" :effect="items.length ? 'dark' : 'plain'">售后附件</el-tag>
      <span v-if="items.length">{{ fileCount }} 个文件 · {{ items.length }} 批（邮件 / 上传页）</span>
      <span v-else class="muted">还没有买家发来的照片、视频</span>
      <span v-for="l in pendingLinks" :key="l.id" class="muted small">· 已发上传链接（订单 {{ l.order_id }}，{{ l.created_by }}）</span>
      <span class="spacer" />
      <el-tooltip :disabled="uploadReady" :content="'上传页还没开通：' + uploadReasons.join('；')" placement="bottom">
        <span><el-button size="small" plain :disabled="!uploadReady" @click="openLink">生成上传链接</el-button></span>
      </el-tooltip>
      <el-button v-if="items.length" size="small" type="primary" plain @click="insertReceived">填入“已收到”回复</el-button>
      <el-button v-if="items.length" link size="small" @click="open = !open">{{ open ? '收起' : '展开' }}</el-button>
    </div>
    <div v-if="open && items.length" class="as-body">
      <AftersalesMailCard v-for="m in items" :key="`${m.kind}-${m.id}`" :mail="m" @claim="claim" />
    </div>

    <el-dialog v-model="linkDialog" title="生成上传链接" width="520px" append-to-body>
      <el-form label-width="70px">
        <el-form-item label="订单">
          <el-select v-model="linkOrder" filterable allow-create default-first-option placeholder="选择买家的订单，或输入订单号" style="width: 100%">
            <el-option v-for="o in orders" :key="o.order_id" :value="o.order_id"
              :label="`${o.order_id} · ${(o.created_at || '').slice(0, 10)} · ${o.title || ''}`" />
          </el-select>
        </el-form-item>
      </el-form>
      <template v-if="linkUrl">
        <el-input :model-value="linkUrl" readonly />
        <p class="muted small">
          {{ linkExpires ? `有效期到 ${linkExpires.slice(0, 10)}。` : '' }}买家点开就能直接传，不用再填订单校验。
          <b>链接不能放进 eBay 消息</b>，请贴到发给买家的邮件里。
        </p>
      </template>
      <template #footer>
        <el-button @click="linkDialog = false">关闭</el-button>
        <el-button v-if="!linkUrl" type="primary" :disabled="!linkOrder" @click="makeLink">生成</el-button>
        <el-button v-else type="primary" @click="copyLink">复制链接</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.as-box {
  border-bottom: 1px solid var(--line);
  background: var(--accent-soft);
  padding: 6px 12px;
}
.as-box.empty {
  background: transparent;
  padding: 3px 12px;
}
.small {
  font-size: var(--fs-xs);
}
.as-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
}
.spacer {
  flex: 1;
}
.as-body {
  margin-top: 6px;
  max-height: 360px;
  overflow: auto;
}
</style>
