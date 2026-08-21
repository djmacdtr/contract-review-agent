<template>
  <div class="page panel">
    <h2>创建起草检查任务</h2>
    <p class="muted">仅录入 URL。系统将真实下载并逐份解析，辅助资料类型无需人工选择。</p>
    <el-form label-position="top">
      <el-form-item label="业务关联 ID"><el-input v-model="form.client_reference_id" /></el-form-item>
      <el-form-item label="目标合同"><RemoteFileFields v-model="form.target_file" /></el-form-item>
      <el-form-item label="合同模板"><RemoteFileFields v-model="form.template_file" /></el-form-item>
      <el-divider>辅助资料</el-divider>
      <div v-for="(item, index) in form.reference_files" :key="index" class="reference-row">
        <RemoteFileFields v-model="form.reference_files[index]" />
        <el-button type="danger" plain @click="remove(index)" :disabled="form.reference_files.length === 1">删除</el-button>
      </div>
      <el-button plain @click="add">增加辅助资料</el-button>
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
const blank = () => ({ url: '', file_name: '' })
const form = reactive({ client_reference_id: '', target_file: { url: '', file_name: '' }, template_file: { url: '', file_name: '' }, reference_files: [blank()] })
const add = () => form.reference_files.push(blank()); const remove = (i: number) => form.reference_files.splice(i, 1)
async function submit() { loading.value = true; try { const payload = { ...form, client_reference_id: form.client_reference_id || undefined }; const task = await api.createDraft(payload); await router.push(`/tasks/${task.task_id}`) } catch (e) { ElMessage.error(String(e)) } finally { loading.value = false } }
</script>

<style scoped>.reference-row { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; margin-bottom: 12px; }</style>
