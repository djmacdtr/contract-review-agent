<template>
  <div class="page workspace-page">
    <div class="page-heading task-heading">
      <div>
        <span class="page-kicker">开发控制台</span>
        <h1>统一任务中心</h1>
        <p>创建、跟踪并查看起草检查和放款比对结果，业务报告与调试详情保持独立。</p>
      </div>
      <div class="heading-actions">
        <el-button @click="$router.push('/tasks/new/draft')">新建起草检查</el-button>
        <el-button type="primary" @click="$router.push('/tasks/new/final')">新建放款比对</el-button>
      </div>
    </div>

    <section class="panel filter-panel">
      <el-form :inline="true" class="filters" label-position="top">
        <el-form-item label="任务类型">
          <el-select v-model="filters.taskType" clearable placeholder="全部类型" style="width: 170px">
            <el-option label="起草检查" value="DRAFT_REVIEW" />
            <el-option label="放款阶段比对" value="FINAL_COMPARE" />
          </el-select>
        </el-form-item>
        <el-form-item label="任务状态">
          <el-select v-model="filters.status" clearable placeholder="全部状态" style="width: 150px">
            <el-option v-for="status in statuses" :key="status" :label="displayLabel(status)" :value="status" />
          </el-select>
        </el-form-item>
        <el-form-item label="业务关联 ID">
          <el-input v-model.trim="filters.clientReferenceId" clearable placeholder="精确匹配" style="width: 210px" @keyup.enter="search" />
        </el-form-item>
        <el-form-item label="创建时间">
          <el-date-picker
            v-model="filters.createdRange"
            type="datetimerange"
            value-format="YYYY-MM-DDTHH:mm:ss"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
            range-separator="至"
            style="width: 360px"
          />
        </el-form-item>
        <el-form-item label=" ">
          <el-button type="primary" :loading="loading" @click="search">查询</el-button>
          <el-button :disabled="loading" @click="reset">重置</el-button>
        </el-form-item>
      </el-form>
    </section>

    <section class="panel task-table-panel">
      <div class="table-toolbar">
        <div><strong>任务记录</strong><span>共 {{ total }} 条</span></div>
        <el-button text :loading="loading" @click="load">刷新</el-button>
      </div>
      <el-alert v-if="error" type="error" :closable="false" title="任务列表加载失败" :description="error" show-icon class="table-alert" />
      <el-table v-loading="loading" :data="items" empty-text="当前筛选条件下暂无任务">
        <el-table-column label="任务" min-width="255">
          <template #default="scope">
            <strong class="task-id">{{ scope.row.task_id }}</strong>
            <span class="task-reference">{{ scope.row.client_reference_id || '未填写业务关联 ID' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="145">
          <template #default="scope"><el-tag effect="plain">{{ displayLabel(scope.row.task_type) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="状态 / 进度" width="175">
          <template #default="scope">
            <div class="status-line"><el-tag :type="statusType(scope.row.status)">{{ displayLabel(scope.row.status) }}</el-tag><span>{{ scope.row.progress }}%</span></div>
            <el-progress :percentage="scope.row.progress" :show-text="false" :stroke-width="5" />
          </template>
        </el-table-column>
        <el-table-column label="结果" width="150">
          <template #default="scope">
            <span v-if="scope.row.conclusion" class="conclusion" :class="scope.row.conclusion.toLowerCase()">{{ displayLabel(scope.row.conclusion) }}</span>
            <span v-else class="muted">尚未生成</span>
            <small v-if="scope.row.legacy_statistics" class="legacy-label">历史统计</small>
          </template>
        </el-table-column>
        <el-table-column label="风险 / 复核" width="125">
          <template #default="scope"><span class="count risk-count">{{ scope.row.risk_count }}</span><span class="count review-count">{{ scope.row.review_count }}</span></template>
        </el-table-column>
        <el-table-column label="创建时间" width="175">
          <template #default="scope"><span class="date-cell">{{ formatDate(scope.row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="scope">
            <el-button link type="primary" @click="$router.push(reportPath(scope.row))">查看报告</el-button>
            <el-button link @click="$router.push(`/tasks/${scope.row.task_id}`)">调试详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        class="task-pagination"
        @current-change="load"
        @size-change="handleSizeChange"
      />
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api } from '../api/client'
import type { TaskStatus, TaskSummary, TaskType } from '../api/types'
import { displayLabel } from '../utils/labels'
import { reportPath } from '../utils/routes'

const statuses: TaskStatus[] = ['PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED']
const loading = ref(false)
const error = ref('')
const items = ref<TaskSummary[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const filters = reactive<{
  taskType: TaskType | ''
  status: TaskStatus | ''
  clientReferenceId: string
  createdRange: string[]
}>({ taskType: '', status: '', clientReferenceId: '', createdRange: [] })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const result = await api.list({
      page: page.value,
      page_size: pageSize.value,
      task_type: filters.taskType || undefined,
      status: filters.status || undefined,
      client_reference_id: filters.clientReferenceId || undefined,
      created_from: filters.createdRange[0] || undefined,
      created_to: filters.createdRange[1] || undefined,
    })
    items.value = result.items
    total.value = result.total
  } catch (caught) {
    error.value = String(caught)
    items.value = []
    total.value = 0
  } finally {
    loading.value = false
  }
}

function search() { page.value = 1; void load() }
function reset() {
  filters.taskType = ''
  filters.status = ''
  filters.clientReferenceId = ''
  filters.createdRange = []
  page.value = 1
  void load()
}
function handleSizeChange() { page.value = 1; void load() }
function formatDate(value: string) { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) }
function statusType(status: TaskStatus): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'SUCCEEDED') return 'success'
  if (status === 'FAILED' || status === 'CANCELLED') return 'danger'
  if (status === 'RUNNING') return 'warning'
  return 'info'
}

onMounted(load)
</script>

<style scoped>
.task-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:24px}.heading-actions{display:flex;gap:10px}.filter-panel{padding:18px 22px}.filters{display:flex;align-items:flex-end;gap:0 12px}.filters :deep(.el-form-item){margin-bottom:0}.task-table-panel{padding:0;overflow:hidden}.table-toolbar{display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--report-border)}.table-toolbar strong{font-size:17px}.table-toolbar span{margin-left:10px;color:var(--report-text-3);font-size:13px}.table-alert{margin:16px 22px 0;width:auto}.task-id,.task-reference{display:block}.task-id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--report-text)}.task-reference{margin-top:6px;color:var(--report-text-3);font-size:12px}.status-line{display:flex;align-items:center;justify-content:space-between;margin-bottom:7px}.status-line span{font-size:12px;color:var(--report-text-3)}.conclusion{font-weight:700;font-size:13px}.conclusion.risk_found{color:var(--report-danger)}.conclusion.review_required{color:var(--report-warning)}.conclusion.pass{color:var(--report-success)}.legacy-label{display:block;color:var(--report-text-3);margin-top:5px}.count{display:inline-flex;min-width:30px;height:28px;align-items:center;justify-content:center;border-radius:7px;font-weight:800;margin-right:6px}.risk-count{background:var(--report-danger-soft);color:var(--report-danger)}.review-count{background:var(--report-warning-soft);color:var(--report-warning)}.date-cell{font-size:12px;color:var(--report-text-2)}.task-pagination{justify-content:flex-end;padding:18px 22px;border-top:1px solid var(--report-border)}
@media(max-width:1200px){.filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}.filters :deep(.el-select),.filters :deep(.el-input),.filters :deep(.el-date-editor){width:100%!important}}
</style>
