<template>
  <div class="upload-field">
    <input ref="fileInput" class="file-input" type="file" accept=".docx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" @change="selectFile" />
    <div class="upload-actions">
      <el-button type="primary" plain :loading="uploading" @click="fileInput?.click()">
        {{ uploading ? '上传中…' : model.url ? '替换文件' : '选择文件' }}
      </el-button>
      <el-button v-if="model.url || selectedName" plain :disabled="uploading" @click="clearFile">移除</el-button>
    </div>
    <div v-if="selectedName || model.file_name" class="file-status">
      <span class="file-name">{{ selectedName || model.file_name }}</span>
      <span v-if="selectedSize">{{ formatBytes(selectedSize) }}</span>
      <el-tag v-if="model.url && !uploading" type="success" size="small">已上传</el-tag>
      <el-tag v-else-if="uploading" size="small">上传中</el-tag>
      <el-tag v-else-if="errorMessage" type="danger" size="small">上传失败</el-tag>
    </div>
    <el-progress v-if="uploading" :percentage="progress" :show-text="true" />
    <el-alert v-if="errorMessage" class="upload-error" type="error" :closable="false" :title="errorMessage" />
    <span class="upload-hint">支持 DOCX、PDF，单文件不超过 200MB</span>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import type { RemoteFile } from '../api/types'
import { api } from '../api/client'

const model = defineModel<RemoteFile>({ required: true })
const fileInput = ref<HTMLInputElement>()
const uploading = ref(false)
const progress = ref(0)
const selectedName = ref('')
const selectedSize = ref(0)
const errorMessage = ref('')

const emptyFile = (): RemoteFile => ({ url: '', file_name: '', mime_type: undefined })
const formatBytes = (bytes: number) => {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function selectFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  model.value = emptyFile()
  selectedName.value = file.name
  selectedSize.value = file.size
  errorMessage.value = ''
  progress.value = 0
  uploading.value = true
  try {
    const uploaded = await api.upload(file, (value) => { progress.value = value })
    model.value = { url: uploaded.url, file_name: uploaded.file_name, mime_type: uploaded.mime_type }
    selectedName.value = uploaded.file_name
    selectedSize.value = uploaded.size_bytes
    progress.value = 100
  } catch (error) {
    model.value = emptyFile()
    errorMessage.value = error instanceof Error ? error.message : '文件上传失败'
  } finally {
    uploading.value = false
    input.value = ''
  }
}

function clearFile() {
  model.value = emptyFile()
  selectedName.value = ''
  selectedSize.value = 0
  errorMessage.value = ''
  progress.value = 0
  if (fileInput.value) fileInput.value.value = ''
}
</script>

<style scoped>
.upload-field { display: grid; gap: 8px; }
.file-input { display: none; }
.upload-actions { display: flex; gap: 8px; }
.file-status { display: flex; align-items: center; gap: 10px; color: var(--text-muted, #64748b); font-size: 13px; }
.file-name { color: var(--text-primary, #1f2937); font-weight: 600; word-break: break-all; }
.upload-error { margin-top: 2px; }
.upload-hint { color: var(--text-muted, #94a3b8); font-size: 12px; }
</style>
