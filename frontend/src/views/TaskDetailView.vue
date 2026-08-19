<template>
  <div class="page panel">
    <el-page-header title="返回任务列表" @back="$router.push('/tasks')" />
    <template v-if="detail">
      <h2>{{ detail.task_id }} <el-tag>{{ detail.status }}</el-tag></h2>
      <p>{{ detail.stage }} · {{ detail.stage_message }}</p>
      <el-progress :percentage="detail.progress" />
      <el-alert v-if="detail.error" type="error" :title="`${detail.error.code}: ${detail.error.message}`" />
      <el-button v-if="detail.status === 'FAILED'" type="warning" @click="retry">创建重试任务</el-button>
    </template>
    <el-tabs v-if="result" style="margin-top: 20px">
      <el-tab-pane label="概览">
        <el-alert
          :type="result.metadata.execution_mode === 'RULE_BASED' ? 'success' : 'warning'"
          :title="`执行模式：${result.metadata.execution_mode}`"
          :closable="false"
        />
        <h3>{{ result.summary.title }}</h3><p>{{ result.summary.description }}</p>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="结论">{{ result.conclusion }}</el-descriptions-item>
          <el-descriptions-item label="差异数量">{{ result.summary.statistics.total }}</el-descriptions-item>
          <el-descriptions-item v-for="file in result.files" :key="file.file_id" :label="file.role">
            {{ file.file_name }} · {{ file.parser_name }} · {{ file.parse_status }}
          </el-descriptions-item>
        </el-descriptions>
        <h4>处理警告</h4>
        <el-alert v-for="warning in result.warnings" :key="warning.code" type="warning" :title="warning.code" :description="warning.message" :closable="false" class="item" />
        <h4>固定规则建议</h4><pre>{{ JSON.stringify(result.advice, null, 2) }}</pre>
      </el-tab-pane>
      <el-tab-pane label="风险项"><el-empty v-if="!result.risk_items.length" /><el-card v-for="(item, i) in result.risk_items" :key="i" class="item"><pre>{{ JSON.stringify(item, null, 2) }}</pre></el-card></el-tab-pane>
      <el-tab-pane label="差异项">
        <el-empty v-if="!result.diff_items.length" description="未发现确定性内容差异" />
        <el-card v-for="item in result.diff_items" :key="item.diff_id" class="item diff-card">
          <template #header>
            <div class="diff-header"><span>{{ item.title }}</span><span><el-tag>{{ item.diff_type }}</el-tag> <el-tag :type="severityType(item.severity)">{{ item.severity }}</el-tag></span></div>
          </template>
          <div class="diff-columns">
            <div><strong>基准文件</strong><p class="location">{{ formatLocation(item.baseline?.location) }}</p><p>{{ item.baseline?.text || '—' }}</p></div>
            <div><strong>目标文件</strong><p class="location">{{ formatLocation(item.target?.location) }}</p><p>{{ item.target?.text || '—' }}</p></div>
          </div>
          <p class="muted">置信度：{{ item.confidence }} · 需要人工复核：{{ item.requires_manual_review ? '是' : '否' }}</p>
        </el-card>
      </el-tab-pane>
      <el-tab-pane label="原始 JSON"><pre>{{ JSON.stringify(result, null, 2) }}</pre></el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import type { TaskDetail, TaskResult } from '../api/types'
import type { DocumentLocation } from '../api/types'
const route = useRoute(); const router = useRouter(); const taskId = String(route.params.taskId)
const detail = ref<TaskDetail>(); const result = ref<TaskResult>(); let timer: number | undefined
async function load() { try { detail.value = await api.detail(taskId); if (detail.value.status === 'SUCCEEDED') { result.value = await api.result(taskId); stop() } else if (['FAILED', 'CANCELLED'].includes(detail.value.status)) stop() } catch (e) { ElMessage.error(String(e)); stop() } }
function stop() { if (timer) window.clearInterval(timer); timer = undefined }
async function retry() { try { const next = await api.retry(taskId); await router.push(`/tasks/${next.task_id}`); window.location.reload() } catch (e) { ElMessage.error(String(e)) } }
function formatLocation(location?: DocumentLocation) { if (!location) return '无对应位置'; const parts = []; if (location.page != null) parts.push(`第 ${location.page} 页`); if (location.paragraph_index != null) parts.push(`段落 ${location.paragraph_index}`); if (location.table_index != null) parts.push(`表格 ${location.table_index}`); if (location.row != null) parts.push(`行 ${location.row}`); if (location.column != null) parts.push(`列 ${location.column}`); if (location.section) parts.push(location.section); return parts.join(' · ') || '结构位置未知' }
function severityType(severity: string): 'danger' | 'warning' | 'info' | 'success' { return severity === 'HIGH' ? 'danger' : severity === 'MEDIUM' ? 'warning' : severity === 'LOW' ? 'info' : 'success' }
onMounted(async () => { await load(); if (!result.value && detail.value?.status !== 'FAILED') timer = window.setInterval(load, 2000) }); onBeforeUnmount(stop)
</script>

<style scoped>.item { margin: 12px 0; }.diff-header { display:flex; justify-content:space-between; gap:16px; }.diff-columns { display:grid; grid-template-columns:1fr 1fr; gap:20px; }.diff-columns > div { background:#f8fafc; padding:14px; border-radius:8px; }.location { color:#64748b; font-size:13px; } @media (max-width: 800px) { .diff-columns { grid-template-columns:1fr; } }</style>
