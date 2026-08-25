<template>
  <article class="diff" :class="{ compact }">
    <div class="diff-title"><strong>{{ item.title }}</strong><el-tag>{{ displayLabel(item.diff_type) }}</el-tag></div>
    <div class="sides">
      <div>
        <b>{{ leftLabel }}</b><small>{{ baselineLocation }}</small>
        <p v-if="isMissing"><span class="deleted">{{ item.missing_detail?.content_summary || item.baseline?.text }}</span></p>
        <p v-else><template v-if="item.segments?.length"><span v-for="(segment, index) in baselineSegments" :key="index" :class="{ deleted: segment.operation === 'DELETE' }">{{ segment.text }}</span></template><span v-else :class="{ deleted: item.baseline && !item.target }">{{ item.baseline?.text || '—' }}</span></p>
      </div>
      <div>
        <b>当前文件</b><small>{{ targetLocation }}</small>
        <p v-if="isMissing" class="missing-message">当前文件中未找到基准文件对应的连续内容。</p>
        <p v-else><template v-if="item.segments?.length"><span v-for="(segment, index) in targetSegments" :key="index" :class="{ inserted: segment.operation === 'INSERT' }">{{ segment.text }}</span></template><span v-else :class="{ inserted: item.target && !item.baseline }">{{ item.target?.text || '—' }}</span></p>
      </div>
    </div>
  </article>
</template>
<script setup lang="ts">
import { computed } from 'vue'
import type { DiffItem, DiffSide, ResultFile } from '../../api/types'
import { displayLabel } from '../../utils/labels'
import { fileNameMap, formatBusinessLocations, formatMissingBaselineLocation, formatMissingTargetLocation } from '../../utils/reportEvidence'
const props = withDefaults(defineProps<{ item: DiffItem; files?: ResultFile[]; leftLabel?: string; compact?: boolean }>(), { files: () => [], leftLabel: '基准文件', compact: false })
const names = computed(() => fileNameMap(props.files))
const isMissing = computed(() => ['PAGE_MISSING', 'CONTENT_BLOCK_MISSING'].includes(props.item.diff_type))
const baselineSegments = computed(() => (props.item.segments ?? []).filter(item => item.operation !== 'INSERT'))
const targetSegments = computed(() => (props.item.segments ?? []).filter(item => item.operation !== 'DELETE'))
const baselineLocation = computed(() => isMissing.value ? formatMissingBaselineLocation(props.item, names.value) : locations(props.item.baseline))
const targetLocation = computed(() => isMissing.value ? formatMissingTargetLocation(props.item, names.value) : locations(props.item.target))
function locations(side?: DiffSide) {
  if (!side) return '无对应位置'
  const values = side.locations?.length ? side.locations : [side.location]
  return formatBusinessLocations(values, side.file_id, names.value)
}
</script>
<style scoped>
.diff{border:1px solid var(--report-border);border-radius:9px;padding:14px;margin:10px 0;background:var(--report-surface)}.diff.compact{margin-top:12px;padding:11px}.diff-title{display:flex;justify-content:space-between;gap:10px}.diff-title strong{font-size:13px}.sides{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}.sides>div{background:var(--report-surface-2);padding:11px;border-radius:7px;min-width:0}.sides b{font-size:12px}.sides small{display:block;color:var(--report-text-3);margin-top:4px;font-size:11px}.sides p{white-space:pre-wrap;overflow-wrap:anywhere;margin:7px 0 0;font-size:13px;line-height:1.75}.deleted{color:var(--report-danger);background:var(--report-danger-soft);text-decoration:line-through;text-decoration-thickness:1.5px}.inserted{color:#9a5708;background:#fff1b8;border-radius:3px}.missing-message{color:var(--report-text-2)}@media(max-width:700px){.sides{grid-template-columns:1fr}}
</style>
