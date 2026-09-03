<template>
  <article class="risk-card"><div class="card-head"><strong>{{ item.title }}</strong><span class="head-tags"><el-tag type="danger">{{ displayLabel(item.risk_type) }}</el-tag><el-tag v-if="item.validation_status === 'REVIEW_REQUIRED'" type="warning">待人工复核</el-tag></span></div><p>{{ item.description }}</p><SourceEvidence v-if="!evidence?.length" :items="item.source_evidence" :files="files" /><DiffEvidence v-for="diff in evidence" :key="diff.diff_id" :item="diff" :files="files" compact /><div v-if="item.analysis_advice" class="analysis-advice"><strong>AI 分析建议：</strong>{{ item.analysis_advice }}</div></article>
</template>
<script setup lang="ts">
import type { DiffItem, ResultFile, RiskItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'
import DiffEvidence from './DiffEvidence.vue'
import SourceEvidence from './SourceEvidence.vue'
defineProps<{ item: RiskItem; evidence?: DiffItem[]; files: ResultFile[] }>()
</script>
<style scoped>.risk-card{border-left:4px solid var(--report-danger);background:var(--report-danger-soft);padding:16px;margin:10px 0;border-radius:8px}.card-head{display:flex;justify-content:space-between;gap:12px}.head-tags{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}.risk-card p{color:var(--report-text-2);margin:8px 0}.analysis-advice{margin-top:12px;padding:11px 13px;border-left:3px solid var(--report-primary);border-radius:0 7px 7px 0;background:#fff;color:var(--report-text-2);line-height:1.65}.analysis-advice strong{color:var(--report-primary)}@media(max-width:650px){.card-head{flex-direction:column}.head-tags{justify-content:flex-start}}
</style>
