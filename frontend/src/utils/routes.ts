import type { TaskAccepted, TaskSummary, TaskType } from '../api/types'

export function reportPath(task: Pick<TaskAccepted | TaskSummary, 'task_id' | 'task_type'>): string {
  return reportPathFor(task.task_type, task.task_id)
}

export function reportPathFor(taskType: TaskType, taskId: string): string {
  const stage = taskType === 'DRAFT_REVIEW' ? 'draft' : 'final'
  return `/reports/${stage}/${taskId}`
}
