import type { Envelope, TaskAccepted, TaskDetail, TaskList, TaskListQuery, TaskResult } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...init })
  const body = await response.json() as Envelope<T>
  if (!response.ok) throw new Error(`${body.code}: ${body.message}`)
  return body.data
}

export const api = {
  list: (query: TaskListQuery = {}) => {
    const params = new URLSearchParams()
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== '') params.set(key, String(value))
    })
    const suffix = params.size ? `?${params.toString()}` : ''
    return request<TaskList>(`/api/v1/tasks${suffix}`)
  },
  detail: (id: string) => request<TaskDetail>(`/api/v1/tasks/${id}`),
  result: (id: string) => request<TaskResult>(`/api/v1/tasks/${id}/result`),
  createDraft: (payload: unknown) => request<TaskAccepted>('/api/v1/draft-reviews', { method: 'POST', body: JSON.stringify(payload) }),
  createFinal: (payload: unknown) => request<TaskAccepted>('/api/v1/final-comparisons', { method: 'POST', body: JSON.stringify(payload) }),
  retry: (id: string) => request<TaskAccepted>(`/api/v1/tasks/${id}/retry`, { method: 'POST' }),
}
