<script setup lang="ts">
// 处理做不了时明确写原因：eBay 没开放 / eBay 现在不允许 / 店铺没对接的给出 eBay 后台跳转；只是角色不够的只写原因
defineProps<{ reason: string | null; url?: string | null; role?: boolean }>()
</script>

<template>
  <div v-if="reason" class="blocked-note">
    <span>{{ reason }}</span>
    <template v-if="!role && url">
      <a :href="url" target="_blank" rel="noopener">去 eBay 后台处理 ↗</a>
    </template>
  </div>
</template>

<style scoped>
.blocked-note { color: var(--el-color-warning-dark-2); background: var(--el-color-warning-light-9); padding: 6px 10px; margin: 6px 0 10px; }
.blocked-note a { margin-left: 10px; color: var(--el-color-primary); white-space: nowrap; }
</style>
