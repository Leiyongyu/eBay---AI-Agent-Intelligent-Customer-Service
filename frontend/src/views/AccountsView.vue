<script setup lang="ts">
// 账号管理（只有管理员）：新建账号、分配负责店铺、改角色、重置密码、停用/启用、解锁
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, get, post, session } from '../api'
import { errorText } from '../format'
import type { Store, UserRow } from '../types'

const users = ref<UserRow[]>([])
const stores = ref<Store[]>([])
const loading = ref(false)
const storeName = computed(() => Object.fromEntries(stores.value.map((s) => [s.id, s.name])))
const ROLE_OPTIONS = [
  { value: 'CS', label: '客服', desc: '处理负责店铺的全部来信；回复不了的分类标记转售后' },
  { value: 'AFTERSALES', label: '售后', desc: '处理客服标记转售后的会话（退货、退款、赔偿、不适配等）' },
  { value: 'ADMIN', label: '管理员', desc: '全部店铺；账号、知识库、系统设置' },
]

async function load() {
  loading.value = true
  try {
    ;[users.value, stores.value] = await Promise.all([get<UserRow[]>('/api/admin/users'), get<Store[]>('/internal/stores')])
  } finally {
    loading.value = false
  }
}

// 新建 / 编辑
const dialog = ref(false)
const editing = ref<UserRow | null>(null)
const form = reactive({ username: '', display_name: '', role: 'CS', password: '', all_stores: false, store_ids: [] as string[],
                        can_aftersales_files: false })
const saving = ref(false)

function openCreate() {
  editing.value = null
  Object.assign(form, { username: '', display_name: '', role: 'CS', password: '', all_stores: false, store_ids: [],
                        can_aftersales_files: false })
  dialog.value = true
}

function openEdit(u: UserRow) {
  editing.value = u
  Object.assign(form, { username: u.username, display_name: u.display_name, role: u.role, password: '',
                        all_stores: u.role !== 'ADMIN' && u.all_stores, store_ids: [...u.store_ids],
                        can_aftersales_files: !!u.can_aftersales_files })
  dialog.value = true
}

async function save() {
  saving.value = true
  try {
    if (editing.value) {
      await api('PATCH', `/api/admin/users/${editing.value.id}`, {
        display_name: form.display_name, role: form.role, all_stores: form.all_stores, store_ids: form.store_ids,
        can_aftersales_files: form.can_aftersales_files,
      })
      ElMessage.success('已保存')
    } else {
      await post('/api/admin/users', { ...form })
      ElMessage.success(`已创建账号 ${form.username}，对方首次登录需修改密码`)
    }
    dialog.value = false
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    saving.value = false
  }
}

async function patch(u: UserRow, body: Record<string, unknown>, done: string) {
  try {
    await api('PATCH', `/api/admin/users/${u.id}`, body)
    ElMessage.success(done)
    load()
  } catch (e) {
    ElMessage.error(errorText(e))
  }
}

async function resetPassword(u: UserRow) {
  try {
    const { value } = await ElMessageBox.prompt(`给「${u.display_name}」设置新的临时密码（至少 8 位，含字母和数字），对方下次登录须修改。`,
                                                '重置密码', { inputType: 'password', confirmButtonText: '重置' })
    await patch(u, { new_password: value }, '已重置，对方原来的登录已全部退出')
  } catch {
    /* 取消 */
  }
}

async function toggleActive(u: UserRow) {
  if (u.is_active) {
    try {
      await ElMessageBox.confirm(`停用「${u.display_name}」？停用后不能登录，已登录的立即退出。`, '停用账号', { type: 'warning' })
    } catch {
      return
    }
  }
  patch(u, { is_active: !u.is_active }, u.is_active ? '已停用' : '已启用')
}

const fmt = (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '—')
onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="panel-head">
      账号管理
      <span class="muted">账号只能由管理员创建；客服、售后只看得到分配给自己的店铺</span>
      <el-button type="primary" size="small" style="margin-left: auto" @click="openCreate">新建账号</el-button>
    </div>
    <div class="panel-body">
      <el-table :data="users" v-loading="loading" size="small" border>
        <el-table-column label="账号" min-width="150">
          <template #default="{ row }">
            <b>{{ row.display_name }}</b> <span class="muted mono">{{ row.username }}</span>
            <el-tag v-if="row.id === session.user?.id" size="small" type="info">我</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="80"><template #default="{ row }">{{ row.role_label }}</template></el-table-column>
        <el-table-column label="负责店铺" min-width="240">
          <template #default="{ row }">
            <span v-if="row.all_stores">全部店铺</span>
            <span v-else-if="!row.store_ids.length" class="warn">未分配（看不到任何会话）</span>
            <span v-else>{{ row.store_ids.map((id: string) => storeName[id] || id).join('、') }}</span>
          </template>
        </el-table-column>
        <el-table-column label="售后附件" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.can_aftersales_files" size="small" type="success">可看</el-tag>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="150">
          <template #default="{ row }">
            <el-tag v-if="!row.is_active" size="small" type="info">已停用</el-tag>
            <el-tag v-else-if="row.locked" size="small" type="danger">已锁定</el-tag>
            <el-tag v-else size="small" type="success">正常</el-tag>
            <el-tag v-if="row.must_change_password" size="small" type="warning">待改初始密码</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近登录" width="150"><template #default="{ row }">{{ fmt(row.last_login_at) }}</template></el-table-column>
        <el-table-column label="操作" width="250">
          <template #default="{ row }">
            <el-button link size="small" type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button link size="small" @click="resetPassword(row)">重置密码</el-button>
            <el-button v-if="row.locked" link size="small" @click="patch(row, { unlock: true }, '已解锁')">解锁</el-button>
            <el-button v-if="row.id !== session.user?.id" link size="small" :type="row.is_active ? 'danger' : 'success'" @click="toggleActive(row)">
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="dialog" :title="editing ? '编辑账号' : '新建账号'" width="560px">
      <el-form label-width="90px">
        <el-form-item label="登录名">
          <el-input v-model="form.username" :disabled="!!editing" placeholder="字母、数字、_ . @ -" />
        </el-form-item>
        <el-form-item label="显示名"><el-input v-model="form.display_name" placeholder="工作台署名，如 客服小王" /></el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="form.role">
            <el-radio v-for="r in ROLE_OPTIONS" :key="r.value" :value="r.value">{{ r.label }}</el-radio>
          </el-radio-group>
          <div class="muted" style="width: 100%">{{ ROLE_OPTIONS.find((r) => r.value === form.role)?.desc }}</div>
        </el-form-item>
        <el-form-item v-if="!editing" label="初始密码">
          <el-input v-model="form.password" type="password" show-password placeholder="至少 8 位，含字母和数字；对方首次登录须修改" />
        </el-form-item>
        <el-form-item v-if="form.role !== 'ADMIN'" label="负责店铺">
          <el-checkbox v-model="form.all_stores">全部店铺</el-checkbox>
          <el-select v-if="!form.all_stores" v-model="form.store_ids" multiple filterable collapse-tags collapse-tags-tooltip
                     placeholder="选择店铺" style="width: 100%">
            <el-option v-for="s in stores" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="form.role !== 'ADMIN'" label="售后附件">
          <el-switch v-model="form.can_aftersales_files" />
          <span class="muted" style="margin-left: 8px">可看售后邮箱收到的买家照片、视频（只看负责的店铺）</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
