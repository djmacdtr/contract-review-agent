<template>
  <article class="risk-card"><div class="card-head"><strong>{{ item.title }}</strong><div><el-tag type="danger">{{ displayLabel(item.risk_type) }}</el-tag> <el-tag>{{ displayLabel(item.change_type) }}</el-tag></div></div><p>{{ item.description }}</p><small>模块：{{ displayLabel(item.module_code) }}</small><SourceEvidence v-if="!evidence?.length" :items="item.source_evidence" /><DiffEvidence v-for="diff in evidence" :key="diff.diff_id" :item="diff" :left-label="leftLabel" compact /></article>
</template>
<script setup lang="ts">
import type { DiffItem, RiskItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'
import DiffEvidence from './DiffEvidence.vue'
import SourceEvidence from './SourceEvidence.vue'
defineProps<{ item: RiskItem; evidence?: DiffItem[]; leftLabel?: string }>()
</script>
<style scoped>.risk-card{border-left:4px solid var(--report-danger);background:var(--report-danger-soft);padding:16px;margin:10px 0;border-radius:8px}.card-head{display:flex;justify-content:space-between;gap:12px}.risk-card p{color:var(--report-text-2);margin:8px 0}.risk-card small{color:var(--report-text-3)}@media(max-width:650px){.card-head{flex-direction:column}}</style>
