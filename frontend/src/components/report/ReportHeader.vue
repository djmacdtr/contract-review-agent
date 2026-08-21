<template>
  <header class="report-header">
    <div class="report-heading"><span class="eyebrow">{{ stageLabel }}</span><h1>{{ title }}</h1><p>{{ subtitle }}</p><div class="meta-line"><span v-if="clientReference">业务关联：{{ clientReference }}</span><span>任务：{{ taskId }}</span></div></div>
    <div class="report-header-actions"><el-tag size="large" effect="dark">{{ displayLabel(status) }}</el-tag><slot name="actions" /></div>
  </header>
</template>

<script setup lang="ts">
import { displayLabel } from '../../utils/labels'
defineProps<{ title: string; subtitle: string; stageLabel: string; status?: string; taskId: string; clientReference?: string }>()
</script>

<style scoped>
.report-header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;padding:28px 32px;background:linear-gradient(118deg,#1f3a5f,#2b6cb0);color:#fff;border-radius:14px;box-shadow:var(--report-shadow);position:relative;overflow:hidden}.report-header:after{content:"";position:absolute;right:-48px;top:-72px;width:220px;height:220px;background:#ffffff12;border-radius:50%}.report-heading,.report-header-actions{position:relative;z-index:1}.report-header-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}.eyebrow{font-size:12px;letter-spacing:.14em;color:#d8e7f5}.report-header h1{margin:7px 0;font-size:27px}.report-header p{margin:0;color:#e5eef7}.meta-line{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:15px;color:#c9d9e8;font-size:12px}.meta-line span{border:1px solid #ffffff35;border-radius:999px;padding:3px 9px}.report-header-actions :deep(.el-button){color:#fff;border-color:#ffffff85;background:#ffffff14}.report-header-actions :deep(.el-button:hover){background:#ffffff2b}@media(max-width:700px){.report-header{padding:24px;flex-direction:column}.report-header-actions{justify-content:flex-start}.report-header h1{font-size:24px}}
</style>
