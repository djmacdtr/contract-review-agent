<template>
  <header class="report-header">
    <div class="report-heading"><span class="eyebrow">{{ stageLabel }}</span><h1>{{ title }}</h1><p>{{ subtitle }}</p></div>
  </header>
</template>

<script setup lang="ts">
defineProps<{ title: string; subtitle: string; stageLabel: string }>()
</script>

<style scoped>
.report-header{padding:28px 32px;background:linear-gradient(118deg,#1f3a5f,#2b6cb0);color:#fff;border-radius:14px;box-shadow:var(--report-shadow);position:relative;overflow:hidden}.report-header:after{content:"";position:absolute;right:-48px;top:-72px;width:220px;height:220px;background:#ffffff12;border-radius:50%}.report-heading{position:relative;z-index:1}.eyebrow{font-size:12px;letter-spacing:.14em;color:#d8e7f5}.report-header h1{margin:7px 0;font-size:27px}.report-header p{margin:0;color:#e5eef7}@media(max-width:700px){.report-header{padding:24px}.report-header h1{font-size:24px}}
</style>
