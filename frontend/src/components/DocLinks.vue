<script setup lang="ts">
// 条款引用的参考文档：编号 + 标题，点开即看（pdf、图片在新标签页预览，其他下载）。
// 敏感文档对非管理员、看不到的店铺文档没有链接，只显示标题 / 编号；已删除的标红
export interface DocRef {
  doc_no: string
  title: string | null
  file_url: string | null
  missing?: boolean
  sensitive?: boolean
  archived?: boolean
}
defineProps<{ docs: DocRef[] | undefined; inline?: boolean }>()
</script>

<template>
  <span v-if="docs?.length" class="doc-links" :class="{ inline }">
    <span v-for="d in docs" :key="d.doc_no" class="doc">
      <span class="no">{{ d.doc_no }}</span>
      <a v-if="d.file_url" :href="d.file_url" target="_blank" rel="noopener" :title="`打开 ${d.doc_no}`">{{ d.title }}</a>
      <span v-else-if="d.missing" class="bad">文档已删除</span>
      <span v-else-if="d.title" class="muted" :title="d.sensitive ? '敏感文档：只有管理员能打开' : ''">{{ d.title }}<template v-if="d.sensitive">（敏感，仅管理员可看）</template></span>
      <span v-else class="muted">不在你负责的店铺</span>
      <span v-if="d.archived" class="muted">（已归档）</span>
    </span>
  </span>
</template>

<style scoped>
.doc-links {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
}
.doc-links.inline {
  flex-direction: row;
  flex-wrap: wrap;
  gap: 4px 10px;
}
.doc {
  display: inline-flex;
  gap: 4px;
  align-items: baseline;
  min-width: 0;
}
.no {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--text-2);
}
.muted {
  color: var(--muted);
}
.bad {
  color: var(--danger);
}
</style>
