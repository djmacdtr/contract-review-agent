<template>
  <main class="report-page">
    <ReportHeader title="放款前合同版本比对报告" stage-label="放款阶段 · 最终合同检查" subtitle="拟定审批合同与最终合同的确定性版本差异" />

    <section v-if="error" class="report-state panel">
      <el-result icon="error" title="本次比对未完成" :sub-title="error"><template #extra><el-button type="primary" @click="reload">重新加载</el-button></template></el-result>
    </section>
    <section v-else-if="!result" class="report-state panel">
      <span class="state-kicker">{{ displayLabel(detail?.stage) }}</span><h2>{{ detail?.stage_message || '正在加载比对状态' }}</h2><el-progress :percentage="detail?.progress ?? 0" :stroke-width="10" /><p>比对完成后将在此展示正式报告。</p>
    </section>
    <template v-else>
      <ConclusionBanner :conclusion="result.conclusion" :description="result.summary.description" />
      <CheckModule title="本次比对文件"><FileSummary :files="result.files" /></CheckModule>
      <ReportResultTabs
        :risk-items="result.risk_items"
        :passed-checks="result.passed_checks"
        :diff-items="result.diff_items"
        :files="result.files"
        :module-order="moduleOrder"
        :stamp-images="result.stamp_images ?? []"
        :show-stamp-images="true"
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

const { detail, result, error, reload } = useTaskReport('FINAL_COMPARE')
const moduleOrder = ['VERSION_CHANGE', 'DOCUMENT_ALIGNMENT']
</script>
