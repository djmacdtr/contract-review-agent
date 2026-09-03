<template>
  <div class="page workspace-page">
    <div class="page-heading"><div><span class="page-kicker">签约前检查</span><h1>创建起草检查任务</h1><p>提交目标合同、合同模板和辅助资料，完成模板固定内容及基础必填检查。</p></div></div>
    <section class="panel form-panel">
    <el-alert type="info" :closable="false" title="直接上传 DOCX 或 PDF；辅助资料类型由系统自动识别，无需调用方选择。" />
    <el-form label-position="top" class="task-form">
      <el-form-item label="业务关联 ID"><el-input v-model="form.client_reference_id" /></el-form-item>
      <el-form-item label="目标合同"><RemoteFileFields v-model="form.target_file" /></el-form-item>
      <el-form-item label="合同模板"><RemoteFileFields v-model="form.template_file" /></el-form-item>
      <el-divider>辅助资料</el-divider>
      <div v-for="(item, index) in form.reference_files" :key="index" class="reference-row">
        <RemoteFileFields v-model="form.reference_files[index]" />
        <el-button type="danger" plain @click="remove(index)" :disabled="form.reference_files.length === 1">删除</el-button>
      </div>
      <el-button plain @click="add">增加辅助资料</el-button>
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
const form = reactive({ client_reference_id: '', target_file: blank(), template_file: blank(), reference_files: [blank()] })
const canSubmit = computed(() => !loading.value && [form.target_file, form.template_file, ...form.reference_files].every((file) => Boolean(file.url && file.file_name)))
const add = () => form.reference_files.push(blank()); const remove = (i: number) => form.reference_files.splice(i, 1)
async function submit() { if (!canSubmit.value) return; loading.value = true; try { const payload = { ...form, client_reference_id: form.client_reference_id || undefined }; const task = await api.createDraft(payload); await router.push(reportPath(task)) } catch (e) { ElMessage.error(String(e)) } finally { loading.value = false } }
</script>

<style scoped>.reference-row { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; margin-bottom: 12px; }</style>
