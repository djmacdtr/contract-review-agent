<template>
  <section class="result-tabs">
    <nav class="tab-list" aria-label="报告结果分类">
      <button v-for="tab in tabs" :key="tab.key" type="button" :class="['result-tab', tab.key, { active: active === tab.key }]" @click="active = tab.key">
        <span>{{ tab.label }}</span><b>{{ tab.count }}</b>
      </button>
    </nav>

    <div v-if="active === 'passed'" class="tab-content">
      <CheckModule v-for="group in passedGroups" :key="group.code" :title="group.title" :count="group.items.length">
        <PassedCheckList :items="group.items" />
      </CheckModule>
      <el-empty v-if="!passedGroups.length" description="本次没有形成校验通过记录" />
    </div>
    <div v-else class="tab-content">
      <CheckModule v-for="group in riskGroups" :key="group.code" :title="group.title" :count="group.items.length">
        <RiskItemCard
          v-for="item in group.items"
          :key="item.risk_id"
          :item="item"
          :evidence="relatedDiffs(item.related_diff_ids)"
          :files="files"
          :left-label="leftLabel"
        />
      </CheckModule>
      <el-empty v-if="!riskGroups.length" description="当前分类没有检出风险" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { DiffItem, PassedCheck, ResultFile, RiskItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'
import CheckModule from './CheckModule.vue'
import PassedCheckList from './PassedCheckList.vue'
import RiskItemCard from './RiskItemCard.vue'

type TabKey = 'all' | 'deletion' | 'addition' | 'passed'
const props = withDefaults(defineProps<{
  riskItems: RiskItem[]
  passedChecks: PassedCheck[]
  diffItems: DiffItem[]
  files: ResultFile[]
  moduleOrder?: string[]
  leftLabel?: string
}>(), { moduleOrder: () => [], leftLabel: '基准文件' })

const active = ref<TabKey>('all')
const tabs = computed(() => [
  { key: 'all' as const, label: '检出风险', count: props.riskItems.length },
  { key: 'deletion' as const, label: '删除 / 缺失', count: props.riskItems.filter(item => item.risk_type === 'DELETION_OR_MISSING').length },
  { key: 'addition' as const, label: '新增 / 变更', count: props.riskItems.filter(item => item.risk_type === 'ADDITION_OR_CHANGE').length },
  { key: 'passed' as const, label: '校验通过', count: props.passedChecks.length },
])
const currentRisks = computed(() => {
  if (active.value === 'deletion') return props.riskItems.filter(item => item.risk_type === 'DELETION_OR_MISSING')
  if (active.value === 'addition') return props.riskItems.filter(item => item.risk_type === 'ADDITION_OR_CHANGE')
  return active.value === 'all' ? props.riskItems : []
})
const riskGroups = computed(() => group(currentRisks.value, '风险'))
const passedGroups = computed(() => group(props.passedChecks, ''))

function group<T extends RiskItem | PassedCheck>(items: T[], suffix: string) {
  const grouped = new Map<string, T[]>()
  items.forEach(item => grouped.set(item.module_code, [...(grouped.get(item.module_code) || []), item]))
  return [...grouped.entries()]
    .sort(([left], [right]) => order(left) - order(right))
    .map(([code, values]) => ({ code, title: `${moduleTitle(code)}${suffix}`, items: values }))
}
function order(code: string) { const index = props.moduleOrder.indexOf(code); return index < 0 ? 999 : index }
function moduleTitle(code: string) { const label = displayLabel(code); return label === code ? '其他检查' : label }
function relatedDiffs(ids: string[]) { return props.diffItems.filter(item => ids.includes(item.diff_id)) }
</script>

<style scoped>
.result-tabs{display:grid;gap:14px}.tab-list{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.result-tab{display:flex;align-items:center;justify-content:center;gap:10px;border:1px solid var(--report-border);background:#fff;color:var(--report-text-2);border-radius:10px;padding:14px 12px;font:inherit;font-weight:700;cursor:pointer;box-shadow:var(--report-shadow)}.result-tab b{min-width:25px;border-radius:999px;padding:2px 8px;background:var(--report-surface-2);font-size:12px}.result-tab:hover{border-color:#bcd6f0}.result-tab.active{background:var(--report-primary);border-color:var(--report-primary);color:#fff}.result-tab.active b{background:#ffffff2b}.result-tab.deletion:not(.active) b,.result-tab.all:not(.active) b{color:var(--report-danger)}.result-tab.addition:not(.active) b{color:var(--report-warning)}.result-tab.passed:not(.active) b{color:var(--report-success)}.tab-content{display:grid;gap:14px}@media(max-width:760px){.tab-list{grid-template-columns:repeat(2,1fr)}}
</style>
