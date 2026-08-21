<template>
  <div class="page panel">
    <el-page-header title="开发控制台" content="任务列表" />
    <el-alert class="notice" type="warning" :closable="false" title="开发控制台仅用于任务调试；检查结果仍需最终人工判断。" />
    <el-button type="primary" :loading="loading" @click="load">刷新</el-button>
    <el-table :data="items" style="margin-top: 16px" empty-text="暂无任务">
      <el-table-column prop="task_id" label="任务 ID" min-width="230" />
      <el-table-column prop="task_type" label="类型" width="160" />
      <el-table-column label="状态" width="120"><template #default="s"><el-tag>{{ s.row.status }}</el-tag></template></el-table-column>
      <el-table-column label="进度" width="160"><template #default="s"><el-progress :percentage="s.row.progress" /></template></el-table-column>
      <el-table-column prop="conclusion" label="结论" width="140" />
      <el-table-column prop="risk_count" label="风险" width="80" />
      <el-table-column prop="review_count" label="复核" width="80" />
      <el-table-column prop="created_at" label="创建时间" min-width="190" />
      <el-table-column label="操作" width="100"><template #default="s"><el-button link type="primary" @click="$router.push(`/tasks/${s.row.task_id}`)">查看</el-button></template></el-table-column>
    </el-table>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import type { TaskSummary } from '../api/types'
const loading = ref(false)
const items = ref<TaskSummary[]>([])
async function load() { loading.value = true; try { items.value = (await api.list()).items } catch (e) { ElMessage.error(String(e)) } finally { loading.value = false } }
onMounted(load)
</script>

<style scoped>.notice { margin: 18px 0; }</style>
