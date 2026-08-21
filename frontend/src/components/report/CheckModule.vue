<template><section class="module"><div class="module-title"><h2>{{ title }}</h2><span v-if="count != null">{{ count }}</span></div><slot /></section></template>
<script setup lang="ts">
defineProps<{ title: string; count?: number }>()
</script>
<style scoped>.module{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:22px}.module-title{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}.module-title h2{font-size:19px;margin:0}.module-title span{background:#edf2f7;border-radius:999px;padding:4px 10px;color:#486581}</style>
