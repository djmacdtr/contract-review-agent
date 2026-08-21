<template>
  <div v-if="items.length" class="source-evidence">
    <div v-for="(item, index) in items" :key="index">
      <span>{{ evidenceHeading(item) }}</span>
      <small>{{ evidenceLocation(item) }}</small>
      <p v-if="evidenceText(item)">{{ evidenceText(item) }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ items: Record<string, unknown>[] }>()

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}
function evidenceHeading(item: Record<string, unknown>) {
  const side = item.side === 'BASELINE' ? '基准证据' : item.side === 'TARGET' ? '当前证据' : '来源证据'
  return typeof item.file_id === 'string' ? `${side} · ${item.file_id}` : side
}
function evidenceText(item: Record<string, unknown>) { return typeof item.text === 'string' ? item.text : '' }
function evidenceLocation(item: Record<string, unknown>) {
  const direct = record(item.location)
  const values = Array.isArray(item.locations) ? item.locations.map(record).filter(Boolean) as Record<string, unknown>[] : []
  const locations = values.length ? values : direct ? [direct] : []
  if (!locations.length) return '未提供结构位置'
  const start = formatLocation(locations[0])
  const end = locations.length > 1 ? formatLocation(locations[locations.length - 1]) : ''
  return end && end !== start ? `${start} → ${end}` : start
}
function formatLocation(value: Record<string, unknown>) {
  const parts: string[] = []
  if (value.page != null) parts.push(`第 ${value.page} 页`)
  if (value.paragraph_index != null) parts.push(`段落 ${value.paragraph_index}`)
  if (value.table_index != null) parts.push(`表格 ${value.table_index}`)
  if (value.row != null) parts.push(`行 ${value.row}`)
  if (value.column != null) parts.push(`列 ${value.column}`)
  if (typeof value.section === 'string') parts.push(value.section)
  return parts.join(' · ') || '结构位置未知'
}
</script>

<style scoped>
.source-evidence{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-top:12px}.source-evidence>div{background:#fff;border:1px solid var(--report-border);border-radius:8px;padding:10px 12px;min-width:0}.source-evidence span,.source-evidence small{display:block}.source-evidence span{font-size:12px;font-weight:700;color:var(--report-text-2)}.source-evidence small{margin-top:3px;color:var(--report-text-3)}.source-evidence p{margin:7px 0 0!important;color:var(--report-text)!important;font-size:13px;overflow-wrap:anywhere}
</style>
