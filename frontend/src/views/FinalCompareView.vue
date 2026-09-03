<template>
  <div class="page workspace-page">
    <div class="page-heading"><div><span class="page-kicker">放款前检查</span><h1>创建放款阶段比对任务</h1><p>对拟定审批合同和最终合同执行可追溯的确定性版本比对。</p></div></div>
    <section class="panel form-panel">
    <el-alert type="info" :closable="false" title="上传起草/申请版和盖章版本；系统按文件类型执行本地解析或 OCR。结果不构成法律判断。" />
    <el-form label-position="top" class="task-form">
      <el-form-item label="业务关联 ID"><el-input v-model="form.client_reference_id" /></el-form-item>
      <el-form-item label="原文件/申请版"><RemoteFileFields v-model="form.baseline_file" /></el-form-item>
      <el-form-item label="目标文件/盖章扫描件"><RemoteFileFields v-model="form.target_file" /></el-form-item>
      <div class="form-actions"><el-button type="primary" :loading="loading" :disabled="!canSubmit" @click="submit">创建任务</el-button></div>
    </el-form>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import RemoteFileFields from '../components/RemoteFileFields.vue'
import { api } from '../api/client'
import { reportPath } from '../utils/routes'
import type { RemoteFile } from '../api/types'
const router = useRouter(); const loading = ref(false)
const blank = (): RemoteFile => ({ url: '', file_name: '' })
const form = reactive({ client_reference_id: '', baseline_file: blank(), target_file: blank() })
const canSubmit = computed(() => !loading.value && [form.baseline_file, form.target_file].every((file) => Boolean(file.url && file.file_name)))
async function submit() { if (!canSubmit.value) return; loading.value = true; try { const task = await api.createFinal({ ...form, client_reference_id: form.client_reference_id || undefined }); await router.push(reportPath(task)) } catch (e) { ElMessage.error(String(e)) } finally { loading.value = false } }
</script>
