import type { DocumentLocation, ResultFile } from '../api/types'

type LooseLocation = DocumentLocation & { file_id?: string }

export function fileNameMap(files: ResultFile[]): Record<string, string> {
  return Object.fromEntries(files.map(file => [file.file_id, file.file_name]))
}

function oneBased(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value + 1 : undefined
}

export function formatBusinessLocation(
  location: LooseLocation | undefined,
  fileId: string | undefined,
  names: Record<string, string>,
): string {
  const resolvedFileId = fileId || location?.file_id
  const parts = [`《${resolvedFileId ? names[resolvedFileId] || '相关文件' : '相关文件'}》`]
  if (location?.page != null) parts.push(`第 ${location.page} 页`)
  const paragraph = oneBased(location?.paragraph_index)
  const table = oneBased(location?.table_index)
  const row = oneBased(location?.row)
  const column = oneBased(location?.column)
  if (paragraph != null) parts.push(`第 ${paragraph} 段`)
  if (table != null) parts.push(`第 ${table} 个表格`)
  if (row != null) parts.push(`第 ${row} 行`)
  if (column != null) parts.push(`第 ${column} 列`)
  if (location?.section && parts.length === 1) parts.push(location.section)
  return parts.join(' · ')
}

export function formatBusinessLocations(
  locations: LooseLocation[],
  fileId: string | undefined,
  names: Record<string, string>,
): string {
  const values = locations.map(item => formatBusinessLocation(item, fileId, names))
  return [...new Set(values)].join('；') || `《${fileId ? names[fileId] || '相关文件' : '相关文件'}》`
}
