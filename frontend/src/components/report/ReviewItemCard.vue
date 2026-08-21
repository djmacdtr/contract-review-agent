<template><article class="review"><div><strong>{{ item.title }}</strong><el-tag type="warning">{{ displayLabel(item.reason_code) }}</el-tag></div><p>{{ item.description }}</p></article></template>
<script setup lang="ts">
import type { ReviewItem } from '../../api/types'
import { displayLabel } from '../../utils/labels'
defineProps<{ item: ReviewItem }>()
</script>
<style scoped>.review{background:#fffbe6;border:1px solid #ffe58f;border-radius:9px;padding:15px;margin:10px 0}.review>div{display:flex;justify-content:space-between;gap:12px}.review p{margin-bottom:0;color:#614700}</style>
