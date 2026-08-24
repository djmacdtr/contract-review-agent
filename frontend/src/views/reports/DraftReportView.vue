<template>
  <main class="report-page">
    <ReportHeader title="合同起草检查报告" stage-label="合同起草阶段 · 签约前" subtitle="模板固定内容、动态事实、数值和基础必填项检查" />

    <section v-if="error" class="report-state panel">
      <el-result icon="error" title="本次检查未完成" :sub-title="error"><template #extra><el-button type="primary" @click="reload">重新加载</el-button></template></el-result>
    </section>
    <section v-else-if="!result" class="report-state panel">
      <span class="state-kicker">{{ displayLabel(detail?.stage) }}</span><h2>{{ detail?.stage_message || '正在加载检查状态' }}</h2><el-progress :percentage="detail?.progress ?? 0" :stroke-width="10" /><p>检查完成后将在此展示正式报告。</p>
    </section>
    <template v-else>
      <ConclusionBanner :conclusion="result.conclusion" :description="result.summary.description" />
      <CheckModule title="本次检查文件"><FileSummary :files="result.files" /></CheckModule>
      <ReportResultTabs
        :risk-items="result.risk_items"
        :passed-checks="result.passed_checks"
        :diff-items="result.diff_items"
        :files="result.files"
        :module-order="moduleOrder"
        left-label="合同模板"
      />
    </template>
  </main>
</template>

<script setup lang="ts">
import CheckModule from '../../components/report/CheckModule.vue'
import ConclusionBanner from '../../components/report/ConclusionBanner.vue'
import FileSummary from '../../components/report/FileSummary.vue'
import ReportHeader from '../../components/report/ReportHeader.vue'
import ReportResultTabs from '../../components/report/ReportResultTabs.vue'
import { useTaskReport } from '../../composables/useTaskReport'
import { displayLabel } from '../../utils/labels'

const { detail, result, error, reload } = useTaskReport('DRAFT_REVIEW')
const moduleOrder = ['TEMPLATE_INTEGRITY', 'TEMPLATE_COMPLETENESS', 'FACT_CONSISTENCY', 'NUMERIC_CONSISTENCY']
</script>
