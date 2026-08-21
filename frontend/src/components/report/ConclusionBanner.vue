<template>
  <section class="banner" :class="conclusion.toLowerCase()">
    <span class="status-light">{{ conclusion === 'PASS' ? '✓' : conclusion === 'RISK_FOUND' ? '!' : '?' }}</span><div><strong>{{ displayLabel(conclusion) }}</strong><span>{{ description }}</span></div>
  </section>
</template>
<script setup lang="ts">
import { displayLabel } from '../../utils/labels'
defineProps<{ conclusion: string; description: string }>()
</script>
<style scoped>
.banner{display:flex;gap:14px;align-items:center;padding:16px 20px;border-radius:12px;border:1px solid;box-shadow:var(--report-shadow)}.banner>div{display:flex;flex-direction:column;gap:2px}.banner strong{font-size:18px}.banner span:not(.status-light){color:var(--report-text-2);font-size:13px}.status-light{width:30px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:50%;font-weight:800;font-size:18px;background:#fff}.risk_found{background:#fff1f0;border-color:#f6c9c2}.risk_found .status-light{color:#d8392b}.review_required{background:#fdf3e6;border-color:#f7dca6}.review_required .status-light{color:#e8820c}.pass{background:#e8f6ed;border-color:#c0e6cd}.pass .status-light{color:#1f9d55}@media(max-width:650px){.banner{align-items:flex-start}}
</style>
