<template>
  <div class="page panel">
    <el-page-header title="返回任务列表" @back="$router.push('/tasks')" />
    <template v-if="detail">
      <h2>{{ detail.task_id }} <el-tag>{{ displayLabel(detail.status) }}</el-tag></h2>
      <p>{{ displayLabel(detail.stage) }} · {{ detail.stage_message }}</p>
      <el-progress :percentage="detail.progress" />
      <el-alert v-if="detail.error" type="error" :title="`${detail.error.code}: ${detail.error.message}`" />
      <el-button v-if="detail.status === 'FAILED'" type="warning" @click="retry">创建重试任务</el-button>
    </template>
    <el-tabs v-if="result" style="margin-top: 20px">
      <el-tab-pane label="概览">
        <el-alert
          :type="result.metadata.execution_mode === 'RULE_BASED' ? 'success' : 'warning'"
          :title="`执行模式：${displayLabel(result.metadata.execution_mode)}`"
          :closable="false"
        />
        <h3>{{ result.summary.title }}</h3><p>{{ result.summary.description }}</p>
        <el-alert
          v-if="diagnostics"
          :type="diagnostics.reliable ? 'success' : 'warning'"
          :title="diagnostics.reliable ? '文档对齐可靠' : '文档对齐需要人工复核'"
          :description="`对齐方式：${displayLabel(diagnostics.fallback_mode)}；候选 ${diagnostics.candidate_diff_count} 项，最终输出 ${diagnostics.emitted_diff_count} 项`"
          :closable="false"
          class="item"
        />
        <el-descriptions :column="2" border>
          <el-descriptions-item label="结论">{{ displayLabel(result.conclusion) }}</el-descriptions-item>
          <el-descriptions-item label="差异数量">{{ result.summary.statistics.total }}</el-descriptions-item>
          <template v-if="diagnostics">
            <el-descriptions-item label="基准覆盖率">{{ percent(diagnostics.alignment_coverage_baseline) }}</el-descriptions-item>
            <el-descriptions-item label="目标覆盖率">{{ percent(diagnostics.alignment_coverage_target) }}</el-descriptions-item>
            <el-descriptions-item label="全局相似度">{{ percent(diagnostics.global_text_similarity) }}</el-descriptions-item>
            <el-descriptions-item label="兼容表格">{{ diagnostics.compatible_table_count }}</el-descriptions-item>
          </template>
          <el-descriptions-item v-for="file in result.files" :key="file.file_id" :label="displayLabel(file.role)">
            {{ file.file_name }} · {{ displayLabel(file.parser_name) }} · {{ displayLabel(file.parse_status) }}
            <el-tag v-if="file.parser_metadata?.ocr" size="small" type="warning">OCR</el-tag>
            <div v-if="file.parser_metadata?.engine_version" class="muted">引擎：{{ file.parser_metadata.engine_version }}</div>
            <div v-if="file.parser_metadata?.parse_mode" class="muted">解析模式：{{ file.parser_metadata.parse_mode }}</div>
            <div v-if="file.parser_metadata?.confidence_min != null" class="muted">最低置信度：{{ file.parser_metadata.confidence_min }}</div>
          </el-descriptions-item>
        </el-descriptions>
        <h4>处理警告</h4>
        <el-alert v-for="warning in result.warnings" :key="warning.code" type="warning" :title="`${displayLabel(warning.code)}${warning.details?.count && Number(warning.details.count) > 1 ? `（${warning.details.count} 次）` : ''}`" :description="warning.message" :closable="false" class="item" />
        <h4>固定规则建议</h4><pre>{{ JSON.stringify(result.advice, null, 2) }}</pre>
      </el-tab-pane>
      <el-tab-pane label="风险项"><el-empty v-if="!result.risk_items.length" /><el-card v-for="(item, i) in result.risk_items" :key="i" class="item"><pre>{{ JSON.stringify(item, null, 2) }}</pre></el-card></el-tab-pane>
      <el-tab-pane label="差异项">
        <el-empty v-if="!result.diff_items.length" description="未发现确定性内容差异" />
        <h4 v-if="businessDiffs.length">业务差异（{{ businessDiffs.length }}）</h4>
        <el-card v-for="item in pagedBusinessDiffs" :key="item.diff_id" class="item diff-card">
          <template #header>
            <div class="diff-header"><span>{{ item.title }}</span><span><el-tag>{{ displayLabel(item.diff_type) }}</el-tag> <el-tag :type="severityType(item.severity)">{{ displayLabel(item.severity) }}</el-tag></span></div>
          </template>
          <div class="diff-columns">
            <div><strong>基准文件</strong><p class="location">{{ formatLocations(item.baseline?.locations, item.baseline?.location) }}</p><p>{{ item.baseline?.text || '—' }}</p></div>
            <div><strong>目标文件</strong><p class="location">{{ formatLocations(item.target?.locations, item.target?.location) }}</p><p>{{ item.target?.text || '—' }}</p></div>
          </div>
          <p class="muted">置信度：{{ item.confidence }} · 需要人工复核：{{ item.requires_manual_review ? '是' : '否' }}</p>
        </el-card>
        <el-pagination
          v-if="businessDiffs.length > pageSize"
          v-model:current-page="diffPage"
          :page-size="pageSize"
          :total="businessDiffs.length"
          layout="prev, pager, next, total"
          class="pagination"
        />
        <el-collapse v-if="reviewDiffs.length" class="review-section">
          <el-collapse-item :title="`OCR 人工复核项（${reviewDiffs.length}）`" name="ocr-review">
            <el-alert title="以下差异疑似来自 OCR 单字符、占位符或阅读顺序波动，仍需人工查看原件。" type="warning" :closable="false" />
            <el-card v-for="item in reviewDiffs" :key="item.diff_id" class="item diff-card">
              <template #header>
                <div class="diff-header"><span>{{ item.title }}</span><span><el-tag>{{ displayLabel(item.review_reason) }}</el-tag> <el-tag type="info">{{ displayLabel(item.severity) }}</el-tag></span></div>
              </template>
              <div class="diff-columns">
                <div><strong>基准文件</strong><p class="location">{{ formatLocations(item.baseline?.locations, item.baseline?.location) }}</p><p>{{ item.baseline?.text || '—' }}</p></div>
                <div><strong>目标文件</strong><p class="location">{{ formatLocations(item.target?.locations, item.target?.location) }}</p><p>{{ item.target?.text || '—' }}</p></div>
              </div>
              <p class="muted">复核原因：{{ displayLabel(item.review_reason) }} · 置信度：{{ item.confidence }}</p>
            </el-card>
          </el-collapse-item>
        </el-collapse>
      </el-tab-pane>
      <el-tab-pane label="原始 JSON"><pre>{{ JSON.stringify(result, null, 2) }}</pre></el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import type { TaskDetail, TaskResult } from '../api/types'
import type { DocumentLocation } from '../api/types'
import { displayLabel } from '../utils/labels'
const route = useRoute(); const router = useRouter(); const taskId = String(route.params.taskId)
const detail = ref<TaskDetail>(); const result = ref<TaskResult>(); let timer: number | undefined
const pageSize = 20; const diffPage = ref(1)
const diagnostics = computed(() => result.value?.metadata.comparison_diagnostics)
const businessDiffs = computed(() => result.value?.diff_items.filter(item => !item.review_reason) ?? [])
const reviewDiffs = computed(() => result.value?.diff_items.filter(item => Boolean(item.review_reason)) ?? [])
const pagedBusinessDiffs = computed(() => { const start = (diffPage.value - 1) * pageSize; return businessDiffs.value.slice(start, start + pageSize) })
async function load() { try { detail.value = await api.detail(taskId); if (detail.value.status === 'SUCCEEDED') { result.value = await api.result(taskId); stop() } else if (['FAILED', 'CANCELLED'].includes(detail.value.status)) stop() } catch (e) { ElMessage.error(String(e)); stop() } }
function stop() { if (timer) window.clearInterval(timer); timer = undefined }
async function retry() { try { const next = await api.retry(taskId); await router.push(`/tasks/${next.task_id}`); window.location.reload() } catch (e) { ElMessage.error(String(e)) } }
function formatLocation(location?: DocumentLocation) { if (!location) return '无对应位置'; const parts = []; if (location.page != null) parts.push(`第 ${location.page} 页`); if (location.paragraph_index != null) parts.push(`段落 ${location.paragraph_index}`); if (location.table_index != null) parts.push(`表格 ${location.table_index}`); if (location.row != null) parts.push(`行 ${location.row}`); if (location.column != null) parts.push(`列 ${location.column}`); if (location.section) parts.push(location.section); if (location.source === 'OCR') parts.push('OCR'); if (location.confidence != null) parts.push(`置信度 ${location.confidence}`); return parts.join(' · ') || '结构位置未知' }
function formatLocations(locations?: DocumentLocation[], primary?: DocumentLocation) { const values = locations?.length ? locations : (primary ? [primary] : []); if (!values.length) return '无对应位置'; if (values.length === 1) return formatLocation(values[0]); return `${formatLocation(values[0])} → ${formatLocation(values[values.length - 1])}（共 ${values.length} 处）` }
function percent(value: number) { return `${(value * 100).toFixed(1)}%` }
function severityType(severity: string): 'danger' | 'warning' | 'info' | 'success' { return severity === 'HIGH' ? 'danger' : severity === 'MEDIUM' ? 'warning' : severity === 'LOW' ? 'info' : 'success' }
onMounted(async () => { await load(); if (!result.value && detail.value?.status !== 'FAILED') timer = window.setInterval(load, 2000) }); onBeforeUnmount(stop)
</script>

<style scoped>.item { margin: 12px 0; }.diff-header { display:flex; justify-content:space-between; gap:16px; }.diff-columns { display:grid; grid-template-columns:1fr 1fr; gap:20px; }.diff-columns > div { background:#f8fafc; padding:14px; border-radius:8px; }.location { color:#64748b; font-size:13px; }.pagination { justify-content:center; margin-top:20px; }.review-section { margin-top:20px; } @media (max-width: 800px) { .diff-columns { grid-template-columns:1fr; } }</style>
