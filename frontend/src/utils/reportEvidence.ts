import type { DiffItem, DocumentLocation, ResultFile } from '../api/types'

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

function businessRange(start: number, end: number, unit: string): string {
  return start === end ? `第 ${start} ${unit}` : `第 ${start}–${end} ${unit}`
}

function namedFile(fileId: string | undefined, names: Record<string, string>): string {
  return `《${fileId ? names[fileId] || '相关文件' : '相关文件'}》`
}

export function formatMissingBaselineLocation(
  item: DiffItem,
  names: Record<string, string>,
): string {
  const side = item.baseline
  if (!side) return '基准文件对应位置'
  const detail = item.missing_detail
  const prefix = namedFile(side.file_id, names)
  if (detail?.baseline_page_start != null && detail.baseline_page_end != null) {
    return `${prefix} · ${businessRange(detail.baseline_page_start, detail.baseline_page_end, '页')}`
  }
  const locations = side.locations?.length ? side.locations : [side.location]
  const paragraphs = locations
    .map(location => oneBased(location.paragraph_index))
    .filter((value): value is number => value != null)
  if (paragraphs.length) {
    return `${prefix} · ${businessRange(Math.min(...paragraphs), Math.max(...paragraphs), '段')}`
  }
  const pages = locations
    .map(location => location.page)
    .filter((value): value is number => value != null)
  if (pages.length) return `${prefix} · ${businessRange(Math.min(...pages), Math.max(...pages), '页')}`
  return formatBusinessLocations(locations, side.file_id, names)
}

export function formatMissingTargetLocation(
  item: DiffItem,
  names: Record<string, string>,
): string {
  const side = item.target
  if (!side) return '当前文件对应缺口'
  const prefix = namedFile(side.file_id, names)
  const detail = item.missing_detail
  const before = detail?.target_anchor_before_page
  const after = detail?.target_anchor_after_page
  if (detail?.boundary === 'START' && after != null) return `${prefix} · 第 ${after} 页之前`
  if (detail?.boundary === 'END' && before != null) return `${prefix} · 第 ${before} 页之后`
  if (before != null && after != null) return `${prefix} · 第 ${before} 页与第 ${after} 页之间`
  const locations = side.locations?.length ? side.locations : [side.location]
  return formatBusinessLocations(locations, side.file_id, names)
}
