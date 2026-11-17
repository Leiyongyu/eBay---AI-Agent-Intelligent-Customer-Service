<script setup lang="ts">
// 菜单里先列出来、还没有做的功能（按参考 ERP 保留全部菜单，不需要的在 nav.ts 里删掉）
defineProps<{ title: string; path: string[] }>()
</script>

<template>
  <div class="ph panel">
    <div class="ph-inner">
      <svg width="64" height="64" viewBox="0 0 64 64" fill="none" aria-hidden="true">
        <rect x="10" y="14" width="44" height="36" rx="4" stroke="var(--line)" stroke-width="2" />
        <path d="M10 24h44" stroke="var(--line)" stroke-width="2" />
        <circle cx="17" cy="19" r="1.6" fill="var(--accent)" />
        <circle cx="22" cy="19" r="1.6" fill="var(--warn)" />
        <path d="M22 36h20M22 42h12" stroke="var(--accent-line)" stroke-width="2.4" stroke-linecap="round" />
      </svg>
      <h2>{{ title }}</h2>
      <p class="muted">{{ path.join(' / ') }} · 功能建设中</p>
      <p class="hint">这个菜单已按 ERP 布局预留，接入对应的 eBay 数据后即可使用。</p>
    </div>
  </div>
</template>

<style scoped>
.ph {
  height: 100%;
  align-items: center;
  justify-content: center;
}
.ph-inner {
  text-align: center;
}
h2 {
  margin: 14px 0 4px;
  font-size: 18px;
  font-weight: 600;
  color: var(--text);
}
.hint {
  margin-top: 10px;
  font-size: 13px;
  color: var(--text-2);
}
</style>
