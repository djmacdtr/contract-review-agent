<template>
  <div class="page panel">
    <h2>创建放款阶段比对任务</h2>
    <el-alert type="info" :closable="false" title="执行确定性文字、数值和基础表格比对；不检查印章，不调用 OCR 或 LLM。" />
    <el-form label-position="top" style="margin-top: 16px">
      <el-form-item label="业务关联 ID"><el-input v-model="form.client_reference_id" /></el-form-item>
      <el-form-item label="原文件/申请版"><RemoteFileFields v-model="form.baseline_file" /></el-form-item>
      <el-form-item label="目标文件/盖章扫描件"><RemoteFileFields v-model="form.target_file" /></el-form-item>
      <div class="form-actions"><el-button type="primary" :loading="loading" @click="submit">创建任务</el-button></div>
    </el-form>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import RemoteFileFields from '../components/RemoteFileFields.vue'
import { api } from '../api/client'
const router = useRouter(); const loading = ref(false)
const form = reactive({ client_reference_id: '', baseline_file: { url: '', file_name: '' }, target_file: { url: '', file_name: '' } })
async function submit() { loading.value = true; try { const task = await api.createFinal({ ...form, client_reference_id: form.client_reference_id || undefined }); await router.push(`/tasks/${task.task_id}`) } catch (e) { ElMessage.error(String(e)) } finally { loading.value = false } }
</script>
