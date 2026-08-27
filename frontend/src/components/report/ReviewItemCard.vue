<template><article class="review"><div><strong>{{ item.title }}</strong><el-tag type="warning">{{ displayLabel(item.reason_code) }}</el-tag></div><p>{{ item.description }}</p><SourceEvidence v-if="!evidence?.length" :items="item.source_evidence" /><DiffEvidence v-for="diff in evidence" :key="diff.diff_id" :item="diff" compact /></article></template>
<script setup lang="ts">
import type { DiffItem, ReviewItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'
import DiffEvidence from './DiffEvidence.vue'
import SourceEvidence from './SourceEvidence.vue'
defineProps<{ item: ReviewItem; evidence?: DiffItem[] }>()
</script>
<style scoped>.review{background:var(--report-warning-soft);border:1px solid #f7dca6;border-radius:9px;padding:15px;margin:10px 0}.review>div{display:flex;justify-content:space-between;gap:12px}.review p{margin:7px 0;color:#614700}.review small{color:var(--report-warning)}</style>
