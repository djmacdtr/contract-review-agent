<template>
  <div class="files">
    <article v-for="file in files" :key="file.file_id">
      <span>{{ displayLabel(file.role) }}</span><strong>{{ file.file_name }}</strong>
      <small>{{ displayLabel(file.parser_name) }} · {{ displayLabel(file.parse_status) }}<template v-if="file.page_count"> · {{ file.page_count }} 页</template></small>
    </article>
  </div>
</template>
<script setup lang="ts">
import type { ResultFile } from '../../api/types'
import { displayLabel } from '../../utils/labels'
defineProps<{ files: ResultFile[] }>()
</script>
<style scoped>
.files{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.files article{display:flex;flex-direction:column;gap:7px;padding:16px;border:1px solid #e5e7eb;border-radius:10px;background:#fff}.files span,.files small{color:#66788a}.files strong{overflow-wrap:anywhere}
</style>
