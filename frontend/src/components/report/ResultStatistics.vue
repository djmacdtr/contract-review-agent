<template>
  <div class="statistics">
    <div><strong>{{ statistics.risk_count }}</strong><span>检出风险</span></div>
    <div><strong>{{ statistics.deletion_or_missing_count }}</strong><span>删除 / 缺失</span></div>
    <div><strong>{{ statistics.addition_or_change_count }}</strong><span>新增 / 变更</span></div>
    <div><strong>{{ statistics.review_count }}</strong><span>人工复核</span></div>
    <div><strong>{{ statistics.passed_check_count }}</strong><span>校验通过</span></div>
  </div>
  <el-alert v-if="statistics.legacy_statistics" type="info" :closable="false" title="该任务使用旧版统计，已转换为无等级展示。" />
</template>
<script setup lang="ts">
import type { ResultStatistics } from '../../api/types'
defineProps<{ statistics: ResultStatistics }>()
</script>
<style scoped>
.statistics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.statistics div{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:18px}.statistics strong{display:block;font-size:28px;color:#102a43}.statistics span{color:#66788a;font-size:13px}@media(max-width:800px){.statistics{grid-template-columns:repeat(2,1fr)}}
</style>
