<template>
  <div class="files">
    <article v-for="file in files" :key="file.file_id">
      <div class="file-title"><span>{{ displayLabel(file.role) }}</span></div><strong>{{ file.file_name }}</strong>
      <small v-if="file.page_count">共 {{ file.page_count }} 页</small>
    </article>
  </div>
</template>
<script setup lang="ts">
import type { ResultFile } from '../../api/types'
import { displayLabel } from '../../utils/labels'
defineProps<{ files: ResultFile[] }>()
</script>
<style scoped>
.files{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.files article{display:flex;flex-direction:column;gap:7px;padding:16px;border:1px solid var(--report-border);border-radius:10px;background:var(--report-surface-2)}.file-title{display:flex;align-items:center;justify-content:space-between;gap:8px}.files span,.files small{color:var(--report-text-2)}.files strong{overflow-wrap:anywhere;color:var(--report-text)}
</style>
