<script setup lang="ts">
// 一封售后邮件（或一次买家上传页提交）及其文件：图片、PDF 直接预览，MP4/WebM 在线播放，其余（MOV/HEVC 等）下载后用本机播放器看
import { ref } from 'vue'
import { fmtTime } from '../format'
import type { AftersalesFileItem, AftersalesMailItem } from '../types'

defineProps<{ mail: AftersalesMailItem; storeName?: string; showConversation?: boolean }>()
const emit = defineEmits<{ claim: [mail: AftersalesMailItem]; open: [convId: number] }>()

const preview = ref<AftersalesFileItem | null>(null)
const showBody = ref(false)
const VEHICLE_LABEL: Record<string, string> = { make: '品牌', model: '车型', year: '年份', engine: '发动机', vin: 'VIN' }
const url = (f: AftersalesFileItem, download = false) =>
  `/internal/aftersales-files/${f.id}/content${download ? '?download=true' : ''}`

function size(n: number | null) {
  if (n == null) return ''
  if (n >= 1024 ** 3) return `${(n / 1024 ** 3).toFixed(2)} GB`
  if (n >= 1024 ** 2) return `${(n / 1024 ** 2).toFixed(1)} MB`
  return `${Math.max(1, Math.round(n / 1024))} KB`
}
const STATUS_TYPE: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
  STORED: 'success', INFECTED: 'danger', TOO_LARGE: 'warning', BLOCKED_TYPE: 'warning', DOWNLOAD_FAILED: 'warning',
  PURGED: 'info', UPLOADING: 'info', WAITING_FETCH: 'info',
}
const MATCH_LABEL: Record<string, string> = {
  order_local: '按订单号', order_api: '按订单号（eBay 查询）', invoice_email: '按发票邮箱', manual: '人工认领',
}
</script>

<template>
  <div class="mail">
    <div class="mail-head">
      <b v-if="mail.order_id" class="mono">{{ mail.order_id }}</b>
      <el-tag v-else size="small" type="warning">未认出订单号</el-tag>
      <span v-if="storeName" class="muted">{{ storeName }}</span>
      <template v-if="mail.kind === 'upload'">
        <el-tag size="small" type="primary">上传页</el-tag>
        <b>{{ mail.code }}</b>
        <el-tag size="small" type="warning">{{ mail.issue_label || '未选问题' }}</el-tag>
        <span class="subject">{{ mail.item_title || '' }}</span>
        <span class="spacer" />
        <span class="muted small">{{ mail.buyer_key }} · {{ fmtTime(mail.submitted_at) }} · {{ mail.status_label }}</span>
      </template>
      <template v-else>
        <span class="subject">{{ mail.subject || '（无主题）' }}</span>
        <span class="spacer" />
        <span class="muted small">{{ mail.sender }} · {{ fmtTime(mail.received_at || mail.processed_at) }}</span>
        <el-tag v-if="mail.match" size="small" effect="plain">{{ MATCH_LABEL[mail.match] || mail.match }}</el-tag>
      </template>
      <el-button v-if="showConversation && mail.conversation_id" link size="small" type="primary"
        @click="emit('open', mail.conversation_id)">打开会话</el-button>
      <el-button v-if="mail.kind !== 'upload'" link size="small" @click="showBody = !showBody">{{ showBody ? '收起正文' : '正文' }}</el-button>
      <el-button v-if="mail.kind !== 'upload'" link size="small" type="warning" @click="emit('claim', mail)">{{ mail.store_id ? '改关联' : '认领' }}</el-button>
    </div>
    <div v-if="mail.kind === 'upload'" class="body">
      <div v-if="mail.vehicle || mail.installed" class="small">
        <template v-for="(v, k) in mail.vehicle || {}" :key="k">{{ VEHICLE_LABEL[k] || k }}：{{ v }}　</template>
        <template v-if="mail.installed">安装情况：{{ mail.installed }}</template>
      </div>
      <div>{{ mail.description || '（买家没写描述，或已过保留期清除）' }}</div>
    </div>
    <div v-if="showBody" class="body">{{ mail.body_excerpt || '（正文为空或已过保留期清除）' }}</div>
    <div v-if="!mail.files.length" class="muted small">{{ mail.kind === 'upload' ? '没有文件' : '这封邮件没有附件或文件链接' }}</div>
    <div class="files">
      <div v-for="f in mail.files" :key="f.id" class="file" :class="f.status">
        <template v-if="f.status === 'STORED'">
          <img v-if="f.kind === 'image'" :src="url(f)" class="thumb" loading="lazy" @click="preview = f" />
          <div v-else class="thumb icon" @click="f.kind === 'download' ? undefined : (preview = f)">
            {{ f.kind === 'video' ? '▶ 视频' : f.kind === 'pdf' ? 'PDF' : '文件' }}
          </div>
        </template>
        <div v-else class="thumb icon gray">{{ f.status_label }}</div>
        <div class="meta">
          <div class="name" :title="f.original_name || ''">{{ f.original_name || f.file_name || '（无文件名）' }}</div>
          <div class="muted small">
            {{ size(f.size_bytes) }}
            <template v-if="f.source === 'WETRANSFER'"> · WeTransfer</template>
            <template v-if="f.days_left != null"> · 还剩 {{ f.days_left }} 天</template>
          </div>
          <div v-if="f.status !== 'STORED'" class="small">
            <el-tag size="small" :type="STATUS_TYPE[f.status]">{{ f.status_label }}</el-tag>
            <span v-if="f.status_reason" class="muted"> {{ f.status_reason }}</span>
          </div>
          <div v-else-if="f.status_reason || f.scan_result === 'SKIPPED'" class="warn small">
            {{ f.status_reason || '未做病毒扫描' }}
          </div>
          <div class="small">
            <a v-if="f.status === 'STORED' && f.kind !== 'download'" @click="preview = f">预览</a>
            <a v-if="f.status === 'STORED'" :href="url(f, true)" class="ml">下载</a>
            <a v-if="f.link_url && f.status !== 'STORED'" :href="f.link_url" target="_blank" rel="noopener noreferrer">原链接 ↗</a>
          </div>
        </div>
      </div>
    </div>

    <el-dialog :model-value="!!preview" :title="preview?.original_name || '预览'" width="80%" append-to-body
      @close="preview = null">
      <template v-if="preview">
        <img v-if="preview.kind === 'image'" :src="url(preview)" class="big" />
        <video v-else-if="preview.kind === 'video'" :src="url(preview)" controls autoplay class="big" />
        <iframe v-else-if="preview.kind === 'pdf'" :src="url(preview)" class="pdf" />
        <p class="muted small">
          视频放不出来（如 iPhone 拍的 MOV/HEVC）请 <a :href="url(preview, true)">下载</a> 后用本机播放器看。
        </p>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mail {
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--panel);
}
.mail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.subject {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 360px;
}
.spacer {
  flex: 1;
}
.body {
  white-space: pre-wrap;
  font-size: var(--fs-xs);
  background: var(--sunken);
  border-radius: 4px;
  padding: 6px 8px;
  margin-bottom: 6px;
  max-height: 200px;
  overflow: auto;
}
.files {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.file {
  display: flex;
  gap: 8px;
  width: 300px;
}
.thumb {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: 4px;
  cursor: pointer;
  flex: none;
}
.icon {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: var(--fs-xs);
  text-align: center;
}
.icon.gray {
  background: var(--sunken);
  color: var(--muted);
  cursor: default;
}
.meta {
  min-width: 0;
}
.name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
}
.small {
  font-size: var(--fs-xs);
}
.warn {
  color: var(--warn-text);
}
.ml {
  margin-left: 8px;
}
a {
  cursor: pointer;
}
.big {
  max-width: 100%;
  max-height: 70vh;
  display: block;
  margin: 0 auto;
}
.pdf {
  width: 100%;
  height: 70vh;
  border: 0;
}
</style>
