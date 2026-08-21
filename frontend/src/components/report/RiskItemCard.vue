<template>
  <article class="risk-card"><div class="card-head"><strong>{{ item.title }}</strong><div><el-tag type="danger">{{ displayLabel(item.risk_type) }}</el-tag> <el-tag>{{ displayLabel(item.change_type) }}</el-tag></div></div><p>{{ item.description }}</p><small>模块：{{ displayLabel(item.module_code) }}</small></article>
</template>
<script setup lang="ts">
import type { RiskItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'
defineProps<{ item: RiskItem }>()
</script>
<style scoped>.risk-card{border-left:4px solid #cf1322;background:#fff7f6;padding:16px;margin:10px 0;border-radius:8px}.card-head{display:flex;justify-content:space-between;gap:12px}.risk-card p{color:#44546a}.risk-card small{color:#718096}@media(max-width:650px){.card-head{flex-direction:column}}</style>
