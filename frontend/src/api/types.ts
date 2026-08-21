export type TaskType = 'DRAFT_REVIEW' | 'FINAL_COMPARE'
export type TaskStatus = 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'

export interface Envelope<T> { code: string; message: string; request_id: string; data: T; error?: unknown }
export interface RemoteFile { url: string; file_name: string }
export interface TaskAccepted {
  task_id: string; task_type: TaskType; status: TaskStatus; progress: number
  created_at?: string; status_url?: string; result_url?: string; source_task_id?: string
}
export interface TaskSummary {
  task_id: string; task_type: TaskType; client_reference_id?: string; status: TaskStatus; progress: number
  conclusion?: string; risk_count: number; review_count: number; legacy_statistics: boolean
  created_at: string; finished_at?: string
}
export interface TaskDetail {
  task_id: string; task_type: TaskType; client_reference_id?: string; status: TaskStatus; progress: number
  stage: string; stage_message?: string; attempt_count: number; created_at: string; started_at?: string
  updated_at: string; finished_at?: string; error?: { code: string; message: string; details?: Record<string, unknown> }
}
export interface TaskList { items: TaskSummary[]; page: number; page_size: number; total: number }
export interface TaskListQuery {
  page?: number; page_size?: number; task_type?: TaskType; status?: TaskStatus
  client_reference_id?: string; created_from?: string; created_to?: string
}
export interface DocumentLocation {
  page?: number; paragraph_index?: number; table_index?: number; row?: number; column?: number; section?: string
  bbox?: number[]; source?: 'LOCAL' | 'OCR'; confidence?: number
}
export interface DiffSide { file_id: string; location: DocumentLocation; locations?: DocumentLocation[]; text: string }
export interface DiffSegment { operation: 'EQUAL' | 'DELETE' | 'INSERT'; text: string }
export interface DiffItem {
  diff_id: string
  diff_type: 'ADDED' | 'DELETED' | 'MODIFIED' | 'NUMERIC_CHANGED' | 'TABLE_ROW_ADDED' | 'TABLE_ROW_DELETED' | 'TABLE_CELL_CHANGED'
  title: string; baseline?: DiffSide; target?: DiffSide; segments: DiffSegment[]; confidence: number; requires_manual_review: boolean
  review_reason?: 'OCR_SINGLE_CHAR_VARIANCE' | 'OCR_PLACEHOLDER_VARIANCE' | 'OCR_READING_ORDER_VARIANCE' | 'OCR_LOW_CONFIDENCE_VARIANCE'
}
export interface ResultFile {
  file_id: string; role: string; file_name: string; safe_url: string; sha256?: string; page_count?: number
  parser_name: string; parse_status: string; parse_warnings?: Record<string, unknown>[]; parser_metadata?: Record<string, unknown>
  document_profile?: { document_kind: string; title?: string; confidence: number; generated_by: string; evidence_locations: DocumentLocation[] }
  content_structure?: { block_count: number; table_count: number; sample_locations: DocumentLocation[] }
}
export interface ComparisonDiagnostics {
  reliable: boolean
  baseline_unit_count: number; target_unit_count: number; aligned_unit_count: number
  unmatched_baseline_count: number; unmatched_target_count: number
  alignment_coverage_baseline: number; alignment_coverage_target: number
  unmatched_ratio_baseline: number; unmatched_ratio_target: number
  global_text_similarity: number; candidate_diff_count: number; emitted_diff_count: number
  compatible_table_count: number; fallback_mode: string; reasons: string[]
}
export interface FilteredTemplateDiff { filter_reason: 'TEMPLATE_FILL_ALLOWED'; diff: DiffItem }
export interface TemplateDiagnostics {
  comparison: ComparisonDiagnostics; raw_diff_count: number; retained_diff_count: number
  filtered_diff_count: number; filtered_diff_items: FilteredTemplateDiff[]
  expanded_table_count?: number; coalesced_fill_count?: number
}
export interface RuleCheck {
  rule_id: string; rule_name: string; status: 'PASSED' | 'FAILED'
  location?: DocumentLocation & { file_id?: string }; inputs?: Record<string, unknown>
  expected?: string; actual?: string; message: string
}
export interface ResultStatistics {
  risk_count: number; deletion_or_missing_count: number; addition_or_change_count: number
  review_count: number; passed_check_count: number; legacy_statistics?: boolean
}
export interface RiskItem {
  risk_id: string; module_code: string; risk_type: 'DELETION_OR_MISSING' | 'ADDITION_OR_CHANGE'
  change_type: string; title: string; description: string; source_evidence: Record<string, unknown>[]
  related_diff_ids: string[]; related_rule_ids: string[]; requires_manual_action: boolean
}
export interface ReviewItem {
  review_id: string; module_code: string; reason_code: string; title: string; description: string
  source_evidence: Record<string, unknown>[]; related_diff_ids: string[]; requires_manual_action: boolean
}
export interface PassedCheck { check_id: string; module_code: string; title: string; description: string }
export interface TaskResult {
  schema_version: string; task_id: string; task_type: TaskType; conclusion: string; mock: boolean
  summary: { title: string; description: string; statistics: ResultStatistics }
  files: ResultFile[]; risk_items: RiskItem[]; review_items: ReviewItem[]; passed_checks: PassedCheck[]; diff_items: DiffItem[]
  fact_matrix: Record<string, unknown>[]; rule_checks: RuleCheck[]
  warnings: { code: string; message: string; requires_manual_review?: boolean; page?: number; confidence?: number; details?: Record<string, unknown> }[]
  advice: Record<string, unknown>
  metadata: { execution_mode: 'MOCK' | 'PARSER_ONLY' | 'RULE_BASED'; workflow_version: string; rules_version: string; primary_model: string | null; model_runs: Record<string, unknown>[]; comparison_diagnostics?: ComparisonDiagnostics; template_diagnostics?: TemplateDiagnostics }
}
