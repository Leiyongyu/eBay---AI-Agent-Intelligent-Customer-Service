<script setup lang="ts">
// 系统配置（只有管理员）：在页面上改发票邮箱、售后邮箱、自动回复、查件、eBay 拉取等配置；页面上改的优先于 .env，改完立即生效
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post } from '../api'
import { errorText } from '../format'
import type { AppSettingItem } from '../types'

const items = ref<AppSettingItem[]>([])
const draft = ref<Record<string, string | number | boolean | null>>({})
const loading = ref(false)
const saving = ref(false)
const testTo = ref('')
const testing = ref(false)

const groups = computed(() => {
  const out: { name: string; items: AppSettingItem[] }[] = []
  for (const it of items.value) {
    let g = out.find((x) => x.name === it.group)
    if (!g) out.push((g = { name: it.group, items: [] }))
    g.items.push(it)
  }
  return out
})

function reset(list: AppSettingItem[]) {
  items.value = list
  draft.value = Object.fromEntries(list.map((i) => [i.key, i.type === 'secret' ? '' : i.value]))
}

async function load() {
  loading.value = true
  try {
    reset((await get<{ items: AppSettingItem[] }>('/internal/admin/settings')).items)
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
  }
}

const changed = computed(() =>
  items.value.filter((i) => (i.type === 'secret' ? !!draft.value[i.key] : (draft.value[i.key] ?? null) !== (i.value ?? null))),
)

async function save() {
  const list = changed.value
  if (!list.length) return
  const warns = list.filter((i) => i.warn && draft.value[i.key] === true)
  try {
    await ElMessageBox.confirm(
      [
        `要保存这 ${list.length} 项：${list.map((i) => i.label).join('、')}。`,
        ...warns.map((i) => `「${i.label}」：${i.warn}`),
        list.some((i) => i.restart) ? '其中标了“需重启”的要重启 main.py 才生效。' : '',
      ].filter(Boolean).join('\n'),
      '保存系统配置',
      { type: warns.length ? 'warning' : 'info' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    const values = Object.fromEntries(list.map((i) => [i.key, draft.value[i.key]]))
    const r = await api<{ changed: string[]; items: AppSettingItem[] }>('PUT', '/internal/admin/settings', { values })
    reset(r.items)
    ElMessage.success(r.changed.length ? `已保存 ${r.changed.length} 项，立即生效` : '没有改动')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

async function backToEnv(it: AppSettingItem) {
  try {
    await ElMessageBox.confirm(`「${it.label}」回到 .env 里的值？`, '恢复 .env 配置', { type: 'info' })
  } catch {
    return
  }
  try {
    reset((await api<{ items: AppSettingItem[] }>('DELETE', `/internal/admin/settings/${it.key}`)).items)
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function mailTest() {
  if (!testTo.value.trim()) return
  testing.value = true
  try {
    await post('/api/invoices/mail-test', { to: testTo.value.trim() })
    ElMessage.success('测试邮件已发出，请到收件箱查看')
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="settings" v-loading="loading">
    <div class="panel intro">
      <b>系统配置</b>
      <span class="muted">只有管理员能看和修改。页面上改过的优先于 .env、保存后立即生效；标了“需重启”的要重启 main.py。点“恢复 .env”回到 .env 里的值。密码只能改、不显示。</span>
      <span class="spacer" />
      <el-button type="primary" :disabled="!changed.length" :loading="saving" @click="save">
        保存{{ changed.length ? `（${changed.length} 项）` : '' }}
      </el-button>
    </div>

    <div v-for="g in groups" :key="g.name" class="panel group">
      <h3>{{ g.name }}</h3>
      <div v-for="it in g.items" :key="it.key" class="row">
        <div class="label">
          <div>
            {{ it.label }}
            <el-tag v-if="it.restart" size="small" type="warning" effect="plain">需重启</el-tag>
          </div>
          <div class="muted mono env">{{ it.env }}</div>
        </div>
        <div class="field">
          <el-switch v-if="it.type === 'bool'" v-model="draft[it.key] as boolean" />
          <el-select v-else-if="it.choices" v-model="draft[it.key] as string" style="width: 120px">
            <el-option v-for="c in it.choices" :key="c" :label="c" :value="c" />
          </el-select>
          <el-input-number v-else-if="it.type === 'int'" v-model="draft[it.key] as number" :min="0" controls-position="right" />
          <el-input v-else-if="it.type === 'secret'" v-model="draft[it.key] as string" type="password" show-password
                    :placeholder="it.is_set ? '已设置（留空不改）' : '未设置'" style="max-width: 360px" />
          <el-input v-else v-model="draft[it.key] as string" clearable placeholder="未设置" style="max-width: 360px" />
          <div v-if="it.help" class="muted small">{{ it.help }}</div>
          <div v-if="it.warn" class="warn small">⚠ {{ it.warn }}</div>
        </div>
        <div class="source small">
          <template v-if="it.source === 'page'">
            <span>页面配置 · {{ it.updated_by }}</span>
            <el-button link size="small" @click="backToEnv(it)">恢复 .env</el-button>
          </template>
          <span v-else class="muted">.env / 默认值</span>
        </div>
      </div>
      <div v-if="g.name === '发票'" class="row">
        <div class="label">发送测试邮件</div>
        <div class="field inline">
          <el-input v-model="testTo" placeholder="收件邮箱" style="max-width: 260px" />
          <el-button :loading="testing" :disabled="!testTo.trim()" @click="mailTest">发送</el-button>
          <span class="muted small">用当前保存的发件配置发一封测试邮件（先保存再测）</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings {
  height: 100%;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding-bottom: 24px;
}
/* 全局 .panel 是可压缩的纵向 flex：这里各块按内容撑开、不被压扁，整页滚动 */
.settings > .panel {
  flex-shrink: 0;
}
.intro {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  position: sticky;
  top: 0;
  z-index: 2;
}
.spacer {
  flex: 1;
}
.group {
  display: block;
  padding: 4px 16px 8px;
}
.group h3 {
  margin: 10px 0 6px;
  font-size: var(--fs-md, 15px);
}
.row {
  display: grid;
  grid-template-columns: 220px 1fr 200px;
  gap: 12px;
  align-items: start;
  padding: 10px 0;
  border-top: 1px solid var(--line);
}
.label .env {
  font-size: 11px;
  margin-top: 2px;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}
.field.inline {
  flex-direction: row;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.source {
  text-align: right;
}
.small {
  font-size: var(--fs-xs, 12px);
}
.warn {
  color: var(--warn-text, #b45309);
}
@media (max-width: 900px) {
  .row {
    grid-template-columns: 1fr;
  }
  .source {
    text-align: left;
  }
}
</style>
