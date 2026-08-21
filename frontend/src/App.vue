<template>
  <router-view v-if="embedded" />
  <el-container v-else class="shell">
    <el-header>
      <div class="brand">合同智能检查 Agent <el-tag type="success">FINAL Rule-based</el-tag> <el-tag type="success">DRAFT Rule-based</el-tag></div>
      <el-menu mode="horizontal" router :default-active="$route.path">
        <el-menu-item index="/tasks">任务列表</el-menu-item>
        <el-menu-item index="/tasks/new/draft">起草检查</el-menu-item>
        <el-menu-item index="/tasks/new/final">放款比对</el-menu-item>
        <el-menu-item index="/docs" @click="openDocs">API 文档</el-menu-item>
      </el-menu>
    </el-header>
    <el-main><router-view /></el-main>
  </el-container>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
const route = useRoute()
const embedded = computed(() => route.meta.embedded === true)
const openDocs = () => window.open('/docs', '_blank')
</script>
