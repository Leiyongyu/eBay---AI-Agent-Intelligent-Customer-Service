<script setup lang="ts">
// 会话情况标签：AI 看整段会话给出的阶段（售前 / 物流 / 售后 / 发票 / 其他）+ 一句话概括，让客服一眼看出这个会话怎么了
import { STAGE_LABELS } from '../format'
import type { SituationInfo } from '../types'

defineProps<{ s: SituationInfo; size?: 'small' | 'default' }>()
</script>

<template>
  <span class="situation" :class="[s.stage, size || 'small']" :title="s.at ? `AI 判断（${new Date(s.at).toLocaleString('zh-CN', { hour12: false })}）` : 'AI 判断'">
    <b class="stage">{{ STAGE_LABELS[s.stage] || '其他' }}</b>
    <span v-if="s.label" class="label">{{ s.label }}</span>
  </span>
</template>

<style scoped>
.situation {
  --c: #8c8c8c;
  display: inline-flex;
  align-items: stretch;
  max-width: 100%;
  border: 1px solid var(--c);
  border-radius: 4px;
  overflow: hidden;
  font-size: 12px;
  line-height: 20px;
  vertical-align: middle;
}
.situation.default {
  font-size: 13px;
  line-height: 24px;
}
.stage {
  flex: none;
  padding: 0 6px;
  background: var(--c);
  color: #fff;
  font-weight: 600;
}
.label {
  padding: 0 7px;
  color: var(--c);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.presale { --c: #1d5fc4; }
.shipping { --c: #c45a00; }
.aftersales { --c: #a8071a; }
.invoice { --c: #237804; }
.other { --c: #7a8584; }
</style>
