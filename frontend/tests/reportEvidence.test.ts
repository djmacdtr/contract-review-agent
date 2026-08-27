import {
  formatBusinessLocation,
  formatBusinessLocations,
  formatDiffLocation,
  formatMissingBaselineLocation,
  formatMissingTargetLocation,
} from '../src/utils/reportEvidence.ts'
import type { DiffItem, ResultFile } from '../src/api/types.ts'

function assertEqual(actual: unknown, expected: unknown) {
  if (actual !== expected) throw new Error(`expected ${String(expected)}, got ${String(actual)}`)
}
function assert(condition: unknown) {
  if (!condition) throw new Error('assertion failed')
}

const names = { template: '合同模板.docx', target: '当前合同.docx' }
function file(file_id: string, role: string, file_name: string): ResultFile {
  return { file_id, role, file_name, safe_url: '', parser_name: 'fixture', parse_status: 'SUCCEEDED' }
}

assertEqual(
  formatBusinessLocation(
    { page: 4, paragraph_index: 12, table_index: 2, row: 3, column: 1 },
    'template',
    names,
  ),
  '《合同模板.docx》 · 第 4 页',
)
assertEqual(
  formatBusinessLocations(
    [{ page: 4, paragraph_index: 1 }, { page: 5, table_index: 0, row: 1, column: 2 }],
    'template',
    names,
  ),
  '《合同模板.docx》 · 第 4–5 页',
)
assertEqual(
  formatMissingBaselineLocation(
    {
      diff_id: 'missing',
      diff_type: 'CONTENT_BLOCK_MISSING',
      title: '缺失',
      baseline: { file_id: 'template', location: { page: 4 }, text: '缺失' },
      target: { file_id: 'target', location: { page: 3 }, text: '' },
      missing_detail: {
        boundary: 'MIDDLE',
        baseline_page_start: 4,
        baseline_page_end: 5,
        structure_unit_count: 2,
        aggregated_diff_count: 1,
        content_summary: '连续内容',
      },
      segments: [],
      confidence: 1,
      requires_manual_review: false,
    },
    names,
  ),
  '《合同模板.docx》 · 第 4–5 页',
)
assertEqual(
  formatMissingTargetLocation(
    {
      diff_id: 'missing',
      diff_type: 'CONTENT_BLOCK_MISSING',
      title: '缺失',
      baseline: { file_id: 'template', location: { page: 4 }, text: '缺失' },
      target: undefined,
      missing_detail: {
        boundary: 'MIDDLE',
        target_anchor_before_page: 4,
        target_anchor_after_page: 6,
        structure_unit_count: 2,
        aggregated_diff_count: 1,
        content_summary: '连续内容',
      },
      segments: [],
      confidence: 1,
      requires_manual_review: false,
    },
    names,
    'target',
  ),
  '《当前合同.docx》 · 第 4 页与第 6 页之间',
)

const missingTarget: DiffItem = {
  diff_id: 'missing-target',
  diff_type: 'DELETED',
  title: '目标文件缺少内容',
  baseline: { file_id: 'template', location: { page: 4 }, text: '缺失内容' },
  target: undefined,
  segments: [],
  confidence: 1,
  requires_manual_review: false,
}
assertEqual(
  formatDiffLocation(
    missingTarget,
    'target',
    [file('target', 'TARGET', '当前合同.docx'), file('template', 'TEMPLATE', '合同模板.docx')],
    names,
  ),
  '《当前合同.docx》',
)

const missingBaseline: DiffItem = {
  ...missingTarget,
  diff_id: 'missing-baseline',
  diff_type: 'CONTENT_BLOCK_MISSING',
  baseline: undefined,
  target: { file_id: 'target', location: { page: 5 }, text: '新增内容' },
  missing_detail: {
    boundary: 'MIDDLE',
    baseline_page_start: 2,
    baseline_page_end: 3,
    structure_unit_count: 2,
    aggregated_diff_count: 1,
    content_summary: '连续内容',
  },
}
assertEqual(
  formatDiffLocation(
    missingBaseline,
    'baseline',
    [file('baseline', 'BASELINE', '基准合同.docx'), file('target', 'TARGET', '当前合同.docx')],
    { baseline: '基准合同.docx', target: '当前合同.docx' },
  ),
  '《基准合同.docx》 · 第 2–3 页',
)

const visibleText = formatBusinessLocation(
  { page: 4, paragraph_index: 12, table_index: 2, row: 3, column: 1 },
  'template',
  names,
)
assert(!/[段表格行列]/u.test(visibleText))
