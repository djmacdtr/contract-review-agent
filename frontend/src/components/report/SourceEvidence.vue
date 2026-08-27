<template>
  <div v-if="items.length" class="source-evidence">
    <div v-for="(item, index) in items" :key="index">
      <small>{{ evidenceLocation(item) }}</small>
      <p v-if="evidenceText(item)">{{ evidenceText(item) }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { DocumentLocation, ResultFile } from '../../api/types'
import { fileNameMap, formatBusinessLocations } from '../../utils/reportEvidence'

const props = withDefaults(defineProps<{ items: Record<string, unknown>[]; files?: ResultFile[] }>(), { files: () => [] })
const names = computed(() => fileNameMap(props.files))

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}
function evidenceText(item: Record<string, unknown>) { return typeof item.text === 'string' ? item.text : '' }
function evidenceLocation(item: Record<string, unknown>) {
  const direct = record(item.location)
  const values = Array.isArray(item.locations) ? item.locations.map(record).filter(Boolean) as Record<string, unknown>[] : []
  const locations = values.length ? values : direct ? [direct] : []
  const fileId = typeof item.file_id === 'string' ? item.file_id : undefined
  return formatBusinessLocations(locations as DocumentLocation[], fileId, names.value)
}
</script>

<style scoped>
.source-evidence{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px;margin-top:12px}.source-evidence>div{background:#fff;border:1px solid var(--report-border);border-radius:8px;padding:10px 12px;min-width:0}.source-evidence span,.source-evidence small{display:block}.source-evidence span{font-size:12px;font-weight:700;color:var(--report-text-2)}.source-evidence small{margin-top:3px;color:var(--report-text-3)}.source-evidence p{margin:7px 0 0!important;color:var(--report-text)!important;font-size:13px;overflow-wrap:anywhere}
</style>
