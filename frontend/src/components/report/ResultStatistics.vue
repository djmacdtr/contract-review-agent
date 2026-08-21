<template>
  <div class="statistics">
    <div class="stat-risk"><strong>{{ statistics.risk_count }}</strong><span>检出风险</span></div>
    <div class="stat-delete"><strong>{{ statistics.deletion_or_missing_count }}</strong><span>删除 / 缺失</span></div>
    <div class="stat-add"><strong>{{ statistics.addition_or_change_count }}</strong><span>新增 / 变更</span></div>
    <div class="stat-review"><strong>{{ statistics.review_count }}</strong><span>人工复核</span></div>
    <div class="stat-pass"><strong>{{ statistics.passed_check_count }}</strong><span>校验通过</span></div>
  </div>
  <el-alert v-if="statistics.legacy_statistics" type="info" :closable="false" title="该任务使用旧版统计，已转换为无等级展示。" />
</template>
<script setup lang="ts">
import type { ResultStatistics } from '../../api/types'
defineProps<{ statistics: ResultStatistics }>()
</script>
<style scoped>
.statistics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.statistics div{background:#fff;border:1px solid var(--report-border);border-radius:12px;padding:16px;box-shadow:var(--report-shadow)}.statistics strong{display:block;font-size:28px;color:var(--report-text)}.statistics span{color:var(--report-text-2);font-size:13px}.stat-risk strong{color:var(--report-danger)}.stat-delete strong{color:var(--report-danger)}.stat-add strong{color:var(--report-warning)}.stat-review strong{color:var(--report-info)}.stat-pass strong{color:var(--report-success)}@media(max-width:800px){.statistics{grid-template-columns:repeat(2,1fr)}}
</style>
