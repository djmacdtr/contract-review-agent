<template><footer class="limitations"><strong>能力边界</strong><ul><li v-for="item in items" :key="item">{{ item }}</li></ul><p>本报告用于辅助检查，最终判断应由业务及法律人员结合原件作出。</p></footer></template>
<script setup lang="ts">
defineProps<{ items: string[] }>()
</script>
<style scoped>.limitations{background:#edf2f7;color:#52667a;border-radius:12px;padding:18px 22px}.limitations ul{margin:8px 0}.limitations p{margin:8px 0 0;font-weight:600}</style>
