<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { post, setUser } from '../api'
import { errorText } from '../format'
import type { Me } from '../types'

// 登录页：账号只有管理员能建，这里不提供注册
const username = ref('')
const password = ref('')
const loading = ref(false)

async function submit() {
  if (!username.value.trim() || !password.value) return ElMessage.warning('请输入账号和密码')
  loading.value = true
  try {
    setUser(await post<Me>('/api/auth/login', { username: username.value.trim(), password: password.value }))
  } catch (e) {
    ElMessage.error(errorText(e))
  } finally {
    loading.value = false
    password.value = ''
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="submit">
      <div class="title"><img class="brand-mark" src="/logo-512.png" alt="" aria-hidden="true" />Ebay智能AI客服</div>
      <div class="sub">使用管理员分配的账号登录</div>
      <el-input v-model="username" size="large" placeholder="账号" autocomplete="username" autofocus />
      <el-input v-model="password" size="large" type="password" show-password placeholder="密码" autocomplete="current-password" />
      <el-button type="primary" size="large" native-type="submit" :loading="loading" style="width: 100%">登录</el-button>
      <div class="hint">忘记密码请联系管理员重置；连续输错 5 次会锁定 10 分钟。</div>
    </form>
  </div>
</template>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
  padding: 16px;
}
.login-card {
  width: 100%;
  max-width: 360px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-top: 3px solid var(--brand);
  border-radius: 6px;
  padding: 30px 26px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  box-shadow: 0 8px 28px rgba(45, 58, 58, 0.06);
}
.title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 19px;
  font-weight: 600;
  color: var(--brand);
  letter-spacing: 0.02em;
}
.brand-mark {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
}
.sub {
  color: var(--muted);
  font-size: var(--fs-sm);
  margin: -6px 0 6px;
}
.hint {
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--muted);
  padding-top: 12px;
  border-top: 1px solid var(--line-soft);
}
</style>
