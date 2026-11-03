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
      <div class="title">eBay 智能客服 · 工作台</div>
      <div class="sub">请使用管理员分配的账号登录</div>
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
  background: #1d2433;
  padding: 16px;
}
.login-card {
  width: 100%;
  max-width: 360px;
  background: #fff;
  border-radius: 10px;
  padding: 28px 24px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.25);
}
.title {
  font-size: 20px;
  font-weight: 600;
  text-align: center;
}
.sub {
  text-align: center;
  color: var(--muted);
  font-size: 13px;
  margin-top: -6px;
}
.hint {
  font-size: 12px;
  color: var(--muted);
  text-align: center;
}
</style>
