<template><div class="checks"><div v-for="item in items" :key="item.check_id"><span>✓</span><p><strong>{{ item.title }}</strong><small>{{ formatPassedCheckDescription(item) }}</small></p></div></div></template>
<script setup lang="ts">
import type { PassedCheck } from '../../api/types'
import { formatPassedCheckDescription } from '../../utils/passedCheckDescription'
defineProps<{ items: PassedCheck[] }>()
</script>
<style scoped>.checks>div{display:flex;gap:12px;align-items:flex-start;padding:10px 12px;margin-top:8px;background:var(--report-success-soft);border:1px solid #c0e6cd;border-radius:8px}.checks span{color:var(--report-success);font-weight:800}.checks p{margin:0}.checks strong{color:#167443}.checks small{display:block;color:var(--report-text-2);margin-top:3px}</style>
