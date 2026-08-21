<template><section class="module"><div class="module-title"><div><span v-if="eyebrow" class="module-eyebrow">{{ eyebrow }}</span><h2>{{ title }}</h2></div><span v-if="count != null" class="module-count">{{ count }}</span></div><slot /></section></template>
<script setup lang="ts">
defineProps<{ title: string; count?: number; eyebrow?: string }>()
</script>
<style scoped>.module{background:#fff;border:1px solid var(--report-border);border-radius:12px;padding:20px;box-shadow:var(--report-shadow)}.module-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.module-title h2{font-size:17px;margin:2px 0 0}.module-eyebrow{font-size:11px;color:var(--report-text-3);letter-spacing:.08em}.module-count{background:var(--report-surface-2);border:1px solid var(--report-border);border-radius:999px;padding:3px 10px;color:var(--report-text-2);font-size:12px;font-weight:700}</style>
