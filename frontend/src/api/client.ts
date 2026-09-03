import type { Envelope, TaskAccepted, TaskDetail, TaskList, TaskListQuery, TaskResult, UploadResult } from './types'

let uploadQueue: Promise<void> = Promise.resolve()

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
  upload: (file: File, onProgress?: (percentage: number) => void) => {
    const queuedUpload = uploadQueue.then(() => uploadFile(file, onProgress))
    uploadQueue = queuedUpload.then(() => undefined, () => undefined)
    return queuedUpload
  },
}

function uploadFile(file: File, onProgress?: (percentage: number) => void) {
  return new Promise<UploadResult>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/v1/console/uploads')
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100))
    }
    xhr.onerror = () => reject(new Error('UPLOAD_NETWORK_ERROR: 上传失败，请稍后重试'))
    xhr.onload = () => {
      let body: Envelope<UploadResult> | undefined
      try { body = JSON.parse(xhr.responseText) as Envelope<UploadResult> } catch { /* handled below */ }
      if (xhr.status >= 200 && xhr.status < 300 && body?.data) return resolve(body.data)
      reject(new Error(`${body?.code ?? 'UPLOAD_FAILED'}: ${body?.message ?? '文件上传失败'}`))
    }
    const formData = new FormData()
    formData.append('file', file, file.name)
    xhr.send(formData)
  })
}
