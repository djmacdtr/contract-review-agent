<template><div class="checks"><div v-for="item in items" :key="item.check_id"><span>✓</span><p><strong>{{ item.title }}</strong><small>{{ item.description }}</small></p></div></div></template>
<script setup lang="ts">
import type { PassedCheck } from '../../api/types'
defineProps<{ items: PassedCheck[] }>()
</script>
<style scoped>.checks>div{display:flex;gap:12px;align-items:flex-start;padding:11px 0;border-bottom:1px solid #edf2f7}.checks span{color:#389e0d;font-weight:700}.checks p{margin:0}.checks small{display:block;color:#718096;margin-top:3px}</style>
