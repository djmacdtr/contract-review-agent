import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../api/client'
import type { TaskDetail, TaskResult, TaskType } from '../api/types'

export function useTaskReport(expectedType: TaskType) {
  const route = useRoute()
  const taskId = String(route.params.taskId)
  const detail = ref<TaskDetail>()
  const result = ref<TaskResult>()
  const error = ref('')
  let timer: number | undefined

  function stop() {
    if (timer) window.clearInterval(timer)
    timer = undefined
  }

  async function load() {
    try {
      detail.value = await api.detail(taskId)
      if (detail.value.task_type !== expectedType) {
        error.value = `任务类型不匹配：当前页面只接受 ${expectedType}`
        stop()
        return
      }
      if (detail.value.status === 'SUCCEEDED') {
        result.value = await api.result(taskId)
        stop()
      } else if (['FAILED', 'CANCELLED'].includes(detail.value.status)) {
        error.value = detail.value.error?.message ?? '任务未成功完成'
        stop()
      }
    } catch (caught) {
      error.value = String(caught)
      stop()
    }
  }

  onMounted(async () => {
    await load()
    if (!result.value && !error.value) timer = window.setInterval(load, 2000)
  })
  onBeforeUnmount(stop)

  return { taskId, detail, result, error }
}
