<template>
  <article class="diff"><div class="diff-title"><strong>{{ item.title }}</strong><el-tag>{{ displayLabel(item.diff_type) }}</el-tag></div><div class="sides"><div><b>{{ leftLabel }}</b><small>{{ location(item.baseline?.location) }}</small><p class="before">{{ item.baseline?.text || '—' }}</p></div><div><b>当前文件</b><small>{{ location(item.target?.location) }}</small><p class="after">{{ item.target?.text || '—' }}</p></div></div></article>
</template>
<script setup lang="ts">
import type { DiffItem, DocumentLocation } from '../../api/types'
import { displayLabel } from '../../utils/labels'
withDefaults(defineProps<{ item: DiffItem; leftLabel?: string }>(), { leftLabel: '基准文件' })
function location(value?: DocumentLocation){if(!value)return '无对应位置';return [value.page&&`第 ${value.page} 页`,value.paragraph_index!=null&&`段落 ${value.paragraph_index}`,value.table_index!=null&&`表格 ${value.table_index}`,value.row!=null&&`行 ${value.row}`,value.column!=null&&`列 ${value.column}`].filter(Boolean).join(' · ')||'结构位置未知'}
</script>
<style scoped>.diff{border:1px solid #d9e2ec;border-radius:10px;padding:16px;margin:10px 0}.diff-title{display:flex;justify-content:space-between}.sides{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}.sides>div{background:#f8fafc;padding:13px;border-radius:8px}.sides small{display:block;color:#718096;margin-top:5px}.before{text-decoration-color:#cf1322}.after{background:#fff1b8}@media(max-width:700px){.sides{grid-template-columns:1fr}}</style>
