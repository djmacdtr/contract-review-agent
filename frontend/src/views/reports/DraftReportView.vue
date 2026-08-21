<template>
  <main class="report-page">
    <ReportHeader
      title="合同起草检查报告"
      stage-label="合同起草阶段 · 签约前"
      subtitle="模板固定内容、允许填写区域和基础必填项检查"
      :task-id="taskId"
      :client-reference="detail?.client_reference_id"
      :status="detail?.status"
    >
      <template #actions><el-button plain @click="$router.push('/tasks')">返回任务中心</el-button><el-button plain @click="$router.push(`/tasks/${taskId}`)">调试详情</el-button></template>
    </ReportHeader>

    <section v-if="error" class="report-state panel">
      <el-result icon="error" title="无法展示起草检查报告" :sub-title="error">
        <template #extra><el-button @click="$router.push('/tasks')">返回任务中心</el-button><el-button v-if="detail?.status !== 'FAILED'" type="primary" @click="reload">重新加载</el-button><el-button v-if="detail?.status === 'FAILED'" type="primary" :loading="retrying" @click="retry">创建重试任务</el-button></template>
      </el-result>
    </section>
    <section v-else-if="!result" class="report-state panel">
      <span class="state-kicker">{{ displayLabel(detail?.stage) }}</span><h2>{{ detail?.stage_message || '正在加载任务状态' }}</h2><el-progress :percentage="detail?.progress ?? 0" :stroke-width="10" /><p>报告将在任务完成后自动显示，无需手工刷新。</p>
    </section>
    <template v-else>
      <div class="report-legend"><span><i class="risk-dot" />确认风险</span><span><i class="delete-dot" />删除 / 缺失</span><span><i class="add-dot" />新增 / 变更</span><span><i class="review-dot" />人工复核</span><span><i class="pass-dot" />校验通过</span></div>
      <ConclusionBanner :conclusion="result.conclusion" :description="result.summary.description" />
      <ResultStatistics :statistics="result.summary.statistics" />
      <CheckModule title="本次检查文件" eyebrow="FILES"><FileSummary :files="result.files" /></CheckModule>

      <CheckModule v-for="group in riskGroups" :key="group.code" :title="group.title" :eyebrow="group.code" :count="group.items.length">
        <RiskItemCard v-for="item in group.items" :key="item.risk_id" :item="item" :evidence="relatedDiffs(item.related_diff_ids)" left-label="合同模板" />
      </CheckModule>
      <CheckModule v-if="unlinkedDiffs.length" title="其他模板差异证据" eyebrow="OTHER_EVIDENCE" :count="unlinkedDiffs.length"><DiffEvidence v-for="item in unlinkedDiffs" :key="item.diff_id" :item="item" left-label="合同模板" /></CheckModule>
      <CheckModule v-if="result.rule_checks.length" title="基础必填规则" eyebrow="RULE_CHECKS" :count="result.rule_checks.length">
        <article v-for="item in result.rule_checks" :key="item.rule_id" class="rule-result"><div><strong>{{ item.rule_name }}</strong><el-tag :type="item.status === 'PASSED' ? 'success' : 'danger'">{{ displayLabel(item.status) }}</el-tag></div><p>{{ item.message }}</p><small>期望：{{ item.expected || '—' }} · 实际：{{ item.actual || '—' }}</small></article>
      </CheckModule>
      <CheckModule v-for="group in reviewGroups" :key="group.code" :title="group.title" :eyebrow="group.code" :count="group.items.length"><ReviewItemCard v-for="item in group.items" :key="item.review_id" :item="item" :evidence="relatedDiffs(item.related_diff_ids)" left-label="合同模板" /></CheckModule>
      <CheckModule v-for="group in passedGroups" :key="group.code" :title="group.title" :eyebrow="group.code" :count="group.items.length"><PassedCheckList :items="group.items" /></CheckModule>
      <CapabilityLimitations :items="limitations" />
    </template>
  </main>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { DiffItem, PassedCheck, ReviewItem, RiskItem } from '../../api/types'
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
import { displayLabel } from '../../utils/labels'

const { taskId, detail, result, error, retrying, retryTask, reload } = useTaskReport('DRAFT_REVIEW')
const moduleOrder = ['TEMPLATE_INTEGRITY', 'TEMPLATE_COMPLETENESS', 'TEMPLATE_RELIABILITY']
const riskGroups = computed(() => group(result.value?.risk_items ?? [], '风险'))
const reviewGroups = computed(() => group(result.value?.review_items ?? [], '人工复核'))
const passedGroups = computed(() => group(result.value?.passed_checks ?? [], '校验通过'))
const linkedDiffIds = computed(() => new Set([
  ...(result.value?.risk_items.flatMap(item => item.related_diff_ids) ?? []),
  ...(result.value?.review_items.flatMap(item => item.related_diff_ids) ?? []),
]))
const unlinkedDiffs = computed(() => result.value?.diff_items.filter(item => !linkedDiffIds.value.has(item.diff_id) && !item.review_reason) ?? [])
const limitations = computed(() => {
  const value = result.value?.advice.limitations
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
})

function group<T extends RiskItem | ReviewItem | PassedCheck>(items: T[], suffix: string) {
  const values = new Map<string, T[]>()
  items.forEach(item => values.set(item.module_code, [...(values.get(item.module_code) ?? []), item]))
  return [...values.entries()].sort(([left], [right]) => order(left) - order(right)).map(([code, grouped]) => ({ code, title: `${moduleTitle(code)}${suffix}`, items: grouped }))
}
function order(code: string) { const index = moduleOrder.indexOf(code); return index === -1 ? 999 : index }
function moduleTitle(code: string) { const label = displayLabel(code); return label === code ? `其他检查模块（${code}）` : label }
function relatedDiffs(ids: string[]): DiffItem[] { return result.value?.diff_items.filter(item => ids.includes(item.diff_id)) ?? [] }
async function retry() { try { await retryTask() } catch (caught) { error.value = String(caught) } }
</script>

<style scoped>
.rule-result{border-left:4px solid var(--report-danger);background:var(--report-danger-soft);padding:14px 16px;margin:10px 0;border-radius:8px}.rule-result>div{display:flex;justify-content:space-between;gap:12px}.rule-result p{color:var(--report-text-2);margin:7px 0}.rule-result small{color:var(--report-text-3)}
</style>
