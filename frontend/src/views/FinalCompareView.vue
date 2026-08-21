<template>
  <div class="page workspace-page">
    <div class="page-heading"><div><span class="page-kicker">放款前检查</span><h1>创建放款阶段比对任务</h1><p>对拟定审批合同和最终合同执行可追溯的确定性版本比对。</p></div></div>
    <section class="panel form-panel">
    <el-alert type="info" :closable="false" title="文本 PDF 和 DOCX 本地解析；扫描 PDF 在服务已配置时回退 OCR。结果不调用 LLM，也不构成法律判断。" />
    <el-form label-position="top" class="task-form">
      <el-form-item label="业务关联 ID"><el-input v-model="form.client_reference_id" /></el-form-item>
      <el-form-item label="原文件/申请版"><RemoteFileFields v-model="form.baseline_file" /></el-form-item>
      <el-form-item label="目标文件/盖章扫描件"><RemoteFileFields v-model="form.target_file" /></el-form-item>
      <div class="form-actions"><el-button type="primary" :loading="loading" @click="submit">创建任务</el-button></div>
    </el-form>
    </section>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import RemoteFileFields from '../components/RemoteFileFields.vue'
import { api } from '../api/client'
import { reportPath } from '../utils/routes'
const router = useRouter(); const loading = ref(false)
const form = reactive({ client_reference_id: '', baseline_file: { url: '', file_name: '' }, target_file: { url: '', file_name: '' } })
async function submit() { loading.value = true; try { const task = await api.createFinal({ ...form, client_reference_id: form.client_reference_id || undefined }); await router.push(reportPath(task)) } catch (e) { ElMessage.error(String(e)) } finally { loading.value = false } }
</script>
