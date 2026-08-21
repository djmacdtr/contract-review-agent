<template>
  <header class="report-header">
    <div><span class="eyebrow">{{ stageLabel }}</span><h1>{{ title }}</h1><p>{{ subtitle }}</p></div>
    <el-tag size="large" effect="dark">{{ displayLabel(status) }}</el-tag>
  </header>
</template>

<script setup lang="ts">
import { displayLabel } from '../../utils/labels'
defineProps<{ title: string; subtitle: string; stageLabel: string; status?: string }>()
</script>

<style scoped>
.report-header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;padding:30px 34px;background:#102a43;color:#fff;border-radius:18px}.eyebrow{font-size:13px;letter-spacing:.14em;color:#9fb3c8}.report-header h1{margin:8px 0;font-size:30px}.report-header p{margin:0;color:#d9e2ec}@media(max-width:700px){.report-header{padding:24px;flex-direction:column}.report-header h1{font-size:24px}}
</style>
