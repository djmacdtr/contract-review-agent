import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '../api/client'
import type { TaskDetail, TaskResult, TaskType } from '../api/types'
import { reportPath } from '../utils/routes'

export function useTaskReport(expectedType: TaskType) {
  const route = useRoute()
  const router = useRouter()
  const taskId = String(route.params.taskId)
  const detail = ref<TaskDetail>()
  const result = ref<TaskResult>()
  const error = ref('')
  const retrying = ref(false)
  let timer: number | undefined

  function stop() {
    if (timer) window.clearTimeout(timer)
    timer = undefined
  }

  function schedule() {
    stop()
    timer = window.setTimeout(async () => {
      await load()
      if (!result.value && !error.value) schedule()
    }, 2000)
  }

  async function load() {
    try {
      detail.value = await api.detail(taskId)
      if (detail.value.task_type !== expectedType) {
        error.value = '当前链接与本报告类型不一致，请从原检查入口重新打开。'
        stop()
        return
      }
      error.value = ''
      if (detail.value.status === 'SUCCEEDED') {
        result.value = await api.result(taskId)
        stop()
      } else if (['FAILED', 'CANCELLED'].includes(detail.value.status)) {
        error.value = detail.value.error?.message ?? '任务未成功完成'
        stop()
      }
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : ''
      error.value = message && message !== 'Failed to fetch'
        ? message.replace(/^[A-Z][A-Z0-9_]*:\s*/, '')
        : '报告数据暂时无法加载，请稍后重试。'
      stop()
    }
  }

  onMounted(async () => {
    await load()
    if (!result.value && !error.value) schedule()
  })
  onBeforeUnmount(stop)

  async function retryTask() {
    retrying.value = true
    try {
      const next = await api.retry(taskId)
      await router.replace(reportPath(next))
    } finally {
      retrying.value = false
    }
  }

  async function reload() {
    error.value = ''
    await load()
    if (!result.value && !error.value) schedule()
  }

  return { taskId, detail, result, error, retrying, retryTask, reload }
}
