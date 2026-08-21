<template>
  <main class="embedded-report">
    <ReportHeader title="合同起草检查报告" stage-label="签约前检查" :subtitle="`任务 ${taskId}`" :status="detail?.status" />
    <el-result v-if="error" icon="error" title="无法展示起草检查报告" :sub-title="error" />
    <section v-else-if="!result" class="loading-panel"><el-progress :percentage="detail?.progress ?? 0" /><p>{{ detail?.stage_message || '正在加载任务状态' }}</p></section>
    <template v-else>
      <ConclusionBanner :conclusion="result.conclusion" :description="result.summary.description" />
      <ResultStatistics :statistics="result.summary.statistics" />
      <CheckModule title="本次检查文件"><FileSummary :files="result.files" /></CheckModule>
      <CheckModule v-if="result.risk_items.length" title="确认风险" :count="result.risk_items.length"><RiskItemCard v-for="item in result.risk_items" :key="item.risk_id" :item="item" /></CheckModule>
      <CheckModule v-if="result.diff_items.length" title="模板差异证据" :count="result.diff_items.length"><DiffEvidence v-for="item in result.diff_items" :key="item.diff_id" :item="item" left-label="合同模板" /></CheckModule>
      <CheckModule v-if="result.review_items.length" title="人工复核事项" :count="result.review_items.length"><ReviewItemCard v-for="item in result.review_items" :key="item.review_id" :item="item" /></CheckModule>
      <CheckModule v-if="result.passed_checks.length" title="校验通过" :count="result.passed_checks.length"><PassedCheckList :items="result.passed_checks" /></CheckModule>
      <CapabilityLimitations :items="limitations" />
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import CapabilityLimitations from '../../components/report/CapabilityLimitations.vue'
import CheckModule from '../../components/report/CheckModule.vue'
import ConclusionBanner from '../../components/report/ConclusionBanner.vue'
import DiffEvidence from '../../components/report/DiffEvidence.vue'
import FileSummary from '../../components/report/FileSummary.vue'
import PassedCheckList from '../../components/report/PassedCheckList.vue'
import ReportHeader from '../../components/report/ReportHeader.vue'
import ResultStatistics from '../../components/report/ResultStatistics.vue'
import ReviewItemCard from '../../components/report/ReviewItemCard.vue'
import RiskItemCard from '../../components/report/RiskItemCard.vue'
import { useTaskReport } from '../../composables/useTaskReport'

const { taskId, detail, result, error } = useTaskReport('DRAFT_REVIEW')
const limitations = computed(() => {
  const value = result.value?.advice.limitations
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
})
</script>
