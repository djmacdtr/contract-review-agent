<template>
  <section class="banner" :class="conclusion.toLowerCase()">
    <span class="status-light">{{ conclusion === 'PASS' ? '✓' : conclusion === 'RISK_FOUND' ? '!' : '—' }}</span><div><strong>{{ title }}</strong><span>{{ safeDescription }}</span></div>
  </section>
</template>
<script setup lang="ts">
import { computed } from 'vue'
const props = defineProps<{ conclusion: string; description: string }>()
const title = computed(() => props.conclusion === 'PASS' ? '校验通过' : props.conclusion === 'RISK_FOUND' ? '检出风险' : '历史任务未形成正式分类结果')
const safeDescription = computed(() => props.conclusion === 'REVIEW_REQUIRED' ? '该历史任务的检查结果不满足当前正式报告分类规则。' : props.description)
</script>
<style scoped>
.banner{display:flex;gap:14px;align-items:center;padding:16px 20px;border-radius:12px;border:1px solid;box-shadow:var(--report-shadow)}.banner>div{display:flex;flex-direction:column;gap:2px}.banner strong{font-size:18px}.banner span:not(.status-light){color:var(--report-text-2);font-size:13px}.status-light{width:30px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:50%;font-weight:800;font-size:18px;background:#fff}.risk_found{background:#fff1f0;border-color:#f6c9c2}.risk_found .status-light{color:#d8392b}.review_required{background:#f4f6f8;border-color:#dce2e8}.review_required .status-light{color:#788494}.pass{background:#e8f6ed;border-color:#c0e6cd}.pass .status-light{color:#1f9d55}@media(max-width:650px){.banner{align-items:flex-start}}
</style>
