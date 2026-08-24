<template>
  <div class="fact-matrix">
    <article v-for="item in items" :key="item.field_key" class="fact-item">
      <header>
        <div><strong>{{ item.display_name }}</strong><small>{{ item.field_key }}</small></div>
        <el-tag :type="tagType(item.status)">{{ displayLabel(item.status) }}</el-tag>
      </header>
      <div v-if="item.target_candidate" class="fact-target">
        <small>目标合同</small>
        <p>{{ item.target_candidate.raw_value }}</p>
        <span>规范化值：{{ item.target_candidate.normalized_value }}</span>
      </div>
      <div class="fact-sources">
        <div v-for="relation in item.reference_results || []" :key="relation.source_file_id" class="fact-source">
          <small>来源 {{ relation.source_file_id }}</small>
          <el-tag :type="tagType(relation.status)" size="small">{{ displayLabel(relation.status) }}</el-tag>
          <template v-if="relation.candidate">
            <p>{{ relation.candidate.raw_value }}</p>
            <span>规范化值：{{ relation.candidate.normalized_value }}</span>
          </template>
          <p v-else class="muted">没有可可靠比较的对应事实。</p>
        </div>
        <p v-if="!(item.reference_results || []).length" class="muted">当前没有辅助资料比较结果。</p>
      </div>
      <small v-if="item.missing_source_file_ids.length" class="missing">未抽取来源：{{ item.missing_source_file_ids.join('、') }}</small>
    </article>
  </div>
</template>

<script setup lang="ts">
import type { FactMatrixItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'

defineProps<{ items: FactMatrixItem[] }>()
function tagType(status: FactMatrixItem['status']) {
  if (status === 'CONSISTENT') return 'success'
  if (status === 'CONFLICT') return 'danger'
  return 'warning'
}
</script>

<style scoped>
.fact-matrix{display:grid;gap:12px}.fact-item{border:1px solid var(--report-border);border-radius:10px;padding:15px;background:#fff}.fact-item header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.fact-item header div{display:grid;gap:3px}.fact-item header small,.fact-target small,.fact-target span,.fact-source small,.fact-source span,.missing{color:var(--report-text-3)}.fact-target{margin-top:12px;border-left:3px solid var(--report-primary);padding:8px 12px;background:var(--report-surface)}.fact-target p{margin:5px 0}.fact-sources{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-top:12px}.fact-source{min-width:0;background:var(--report-surface);border-radius:8px;padding:10px 12px}.fact-source .el-tag{margin-left:8px}.fact-source p{margin:6px 0;color:var(--report-text-1);overflow-wrap:anywhere}.missing{display:block;margin-top:10px}
</style>
