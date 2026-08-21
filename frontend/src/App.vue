<template>
  <el-container class="shell">
    <el-header class="console-header">
      <div class="header-inner">
        <div class="brand-block">
          <span class="brand-mark">AI</span>
          <div><strong>合同智能检查 Agent</strong><small>规则驱动的合同检查与版本比对</small></div>
        </div>
        <el-menu mode="horizontal" :default-active="activeMenu" :ellipsis="false">
        <el-menu-item index="/tasks" @click="$router.push('/tasks')">任务中心</el-menu-item>
        <el-menu-item index="/tasks/new/draft" @click="$router.push('/tasks/new/draft')">起草检查</el-menu-item>
        <el-menu-item index="/tasks/new/final" @click="$router.push('/tasks/new/final')">放款比对</el-menu-item>
        <el-menu-item index="/docs" @click="openDocs">API 文档</el-menu-item>
        </el-menu>
      </div>
    </el-header>
    <el-main><router-view :key="$route.fullPath" /></el-main>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const activeMenu = computed(() => {
  if (route.path.startsWith('/reports/') || /^\/tasks\/[^/]+$/.test(route.path)) return '/tasks'
  return route.path
})
const openDocs = () => window.open('/docs', '_blank')
</script>
