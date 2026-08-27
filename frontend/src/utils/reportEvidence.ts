import type { DiffItem, DocumentLocation, ResultFile } from '../api/types'

type LooseLocation = DocumentLocation & { file_id?: string }

export function fileNameMap(files: ResultFile[]): Record<string, string> {
  return Object.fromEntries(files.map(file => [file.file_id, file.file_name]))
}

export function formatBusinessLocation(
  location: LooseLocation | undefined,
  fileId: string | undefined,
  names: Record<string, string>,
): string {
  const resolvedFileId = fileId || location?.file_id
  const parts = [`《${resolvedFileId ? names[resolvedFileId] || '相关文件' : '相关文件'}》`]
  if (location?.page != null) parts.push(`第 ${location.page} 页`)
  return parts.join(' · ')
}

export function formatBusinessLocations(
  locations: LooseLocation[],
  fileId: string | undefined,
  names: Record<string, string>,
): string {
  if (!locations.length) return `《${fileId ? names[fileId] || '相关文件' : '相关文件'}》`
  const groups = new Map<string, { fileId?: string; pages: number[]; hasPage: boolean }>()
  locations.forEach(location => {
    const resolvedFileId = fileId || location.file_id
    const key = resolvedFileId || '__unknown__'
    const group = groups.get(key) || { fileId: resolvedFileId, pages: [], hasPage: false }
    if (location.page != null) {
      group.pages.push(location.page)
      group.hasPage = true
    }
    groups.set(key, group)
  })
  return [...groups.values()].map(group => {
    const prefix = `《${group.fileId ? names[group.fileId] || '相关文件' : '相关文件'}》`
    if (!group.hasPage) return prefix
    return `${prefix} · ${businessRange(Math.min(...group.pages), Math.max(...group.pages), '页')}`
  }).join('；')
}

type DiffSideName = 'baseline' | 'target'

export function fallbackDiffFileId(files: ResultFile[], sideName: DiffSideName): string | undefined {
  const roles = sideName === 'target' ? ['TARGET'] : ['BASELINE', 'TEMPLATE']
  return roles
    .map(role => files.find(file => file.role === role)?.file_id)
    .find((fileId): fileId is string => Boolean(fileId))
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
  baselineFileId?: string,
): string {
  const side = item.baseline
  const detail = item.missing_detail
  if (!side) {
    const prefix = namedFile(baselineFileId, names)
    if (detail?.baseline_page_start != null && detail.baseline_page_end != null) {
      return `${prefix} · ${businessRange(detail.baseline_page_start, detail.baseline_page_end, '页')}`
    }
    return prefix
  }
  const prefix = namedFile(side.file_id, names)
  if (detail?.baseline_page_start != null && detail.baseline_page_end != null) {
    return `${prefix} · ${businessRange(detail.baseline_page_start, detail.baseline_page_end, '页')}`
  }
  const locations = side.locations?.length ? side.locations : [side.location]
  const pages = locations
    .map(location => location.page)
    .filter((value): value is number => value != null)
  if (pages.length) return `${prefix} · ${businessRange(Math.min(...pages), Math.max(...pages), '页')}`
  return formatBusinessLocations(locations, side.file_id, names)
}

export function formatMissingTargetLocation(
  item: DiffItem,
  names: Record<string, string>,
  targetFileId?: string,
): string {
  const side = item.target
  const resolvedFileId = side?.file_id || targetFileId
  const prefix = namedFile(resolvedFileId, names)
  const detail = item.missing_detail
  const before = detail?.target_anchor_before_page
  const after = detail?.target_anchor_after_page
  if (detail?.boundary === 'START' && after != null) return `${prefix} · 第 ${after} 页之前`
  if (detail?.boundary === 'END' && before != null) return `${prefix} · 第 ${before} 页之后`
  if (before != null && after != null) return `${prefix} · 第 ${before} 页与第 ${after} 页之间`
  const locations = side?.locations?.length ? side.locations : side?.location ? [side.location] : []
  return formatBusinessLocations(locations, resolvedFileId, names)
}

export function formatDiffLocation(
  item: DiffItem,
  sideName: DiffSideName,
  files: ResultFile[],
  names: Record<string, string>,
): string {
  const fallbackFileId = fallbackDiffFileId(files, sideName)
  if (item.diff_type === 'PAGE_MISSING' || item.diff_type === 'CONTENT_BLOCK_MISSING') {
    return sideName === 'baseline'
      ? formatMissingBaselineLocation(item, names, fallbackFileId)
      : formatMissingTargetLocation(item, names, fallbackFileId)
  }
  const side = sideName === 'baseline' ? item.baseline : item.target
  const locations = side?.locations?.length ? side.locations : side?.location ? [side.location] : []
  return formatBusinessLocations(locations, side?.file_id || fallbackFileId, names)
}
