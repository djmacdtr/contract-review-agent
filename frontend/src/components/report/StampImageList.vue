<template>
  <section class="stamp-images" aria-label="印章影像">
    <el-alert :title="STAMP_IMAGE_DISCLAIMER" type="info" :closable="false" show-icon />
    <div v-if="safeItems.length" class="stamp-grid">
      <article v-for="item in safeItems" :key="`${item.file_name}-${item.page}-${item.data_uri.slice(-16)}`" class="stamp-card">
        <div class="stamp-card__meta">
          <strong>{{ item.file_name }}</strong>
          <span>{{ formatStampImageLocation(item).split(' · ')[1] }}</span>
        </div>
        <img :src="item.data_uri" :alt="`${item.file_name} ${item.page}页印章影像`" loading="lazy">
      </article>
    </div>
    <el-empty v-else description="未识别到可安全展示的印章影像" />
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { StampImage } from '../../api/types'
import { formatStampImageLocation, isSafeStampImageSource, STAMP_IMAGE_DISCLAIMER } from '../../utils/stampImages'

const props = defineProps<{ items: StampImage[] }>()
const safeItems = computed(() => props.items.filter(item => isSafeStampImageSource(item.data_uri)))
</script>

<style scoped>
.stamp-images{display:grid;gap:16px}.stamp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}.stamp-card{display:grid;gap:12px;padding:14px;border:1px solid var(--report-border);border-radius:12px;background:#fff;box-shadow:var(--report-shadow)}.stamp-card__meta{display:grid;gap:4px;color:var(--report-text-2);font-size:13px}.stamp-card__meta strong{overflow-wrap:anywhere;color:var(--report-text)}.stamp-card img{display:block;width:100%;max-height:300px;object-fit:contain;background:#f7f9fc;border-radius:8px}
</style>
