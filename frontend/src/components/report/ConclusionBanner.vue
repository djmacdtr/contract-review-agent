<template>
  <section class="banner" :class="conclusion.toLowerCase()">
    <strong>{{ displayLabel(conclusion) }}</strong><span>{{ description }}</span>
  </section>
</template>
<script setup lang="ts">
import { displayLabel } from '../../utils/labels'
defineProps<{ conclusion: string; description: string }>()
</script>
<style scoped>
.banner{display:flex;gap:18px;align-items:center;padding:18px 22px;border-radius:12px;border:1px solid}.banner strong{font-size:20px;white-space:nowrap}.risk_found{background:#fff1f0;border-color:#ffccc7;color:#a8071a}.review_required{background:#fffbe6;border-color:#ffe58f;color:#874d00}.pass{background:#f6ffed;border-color:#b7eb8f;color:#237804}@media(max-width:650px){.banner{align-items:flex-start;flex-direction:column;gap:6px}}
</style>
