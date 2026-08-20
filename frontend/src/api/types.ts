export type TaskType = 'DRAFT_REVIEW' | 'FINAL_COMPARE'
export type TaskStatus = 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'

export interface Envelope<T> { code: string; message: string; request_id: string; data: T; error?: unknown }
export interface RemoteFile { url: string; file_name: string; reference_type?: string }
export interface TaskAccepted { task_id: string; task_type: TaskType; status: TaskStatus; progress: number }
export interface TaskSummary {
  task_id: string; task_type: TaskType; client_reference_id?: string; status: TaskStatus; progress: number
  conclusion?: string; high_risk_count: number; medium_risk_count: number; low_risk_count: number
  info_count: number; created_at: string; finished_at?: string
}
export interface TaskDetail extends TaskSummary {
  stage: string; stage_message?: string; attempt_count: number; started_at?: string; updated_at: string
  error?: { code: string; message: string }
}
export interface TaskList { items: TaskSummary[]; page: number; page_size: number; total: number }
export interface DocumentLocation {
  page?: number; paragraph_index?: number; table_index?: number; row?: number; column?: number; section?: string
  bbox?: number[]; source?: 'LOCAL' | 'OCR'; confidence?: number
}
export interface DiffSide { file_id: string; location: DocumentLocation; locations?: DocumentLocation[]; text: string }
export interface DiffSegment { operation: 'EQUAL' | 'DELETE' | 'INSERT'; text: string }
export interface DiffItem {
  diff_id: string
  diff_type: 'ADDED' | 'DELETED' | 'MODIFIED' | 'NUMERIC_CHANGED' | 'TABLE_ROW_ADDED' | 'TABLE_ROW_DELETED' | 'TABLE_CELL_CHANGED'
  severity: 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO'
  title: string; baseline?: DiffSide; target?: DiffSide; segments: DiffSegment[]; confidence: number; requires_manual_review: boolean
}
export interface ResultFile {
  file_id: string; role: string; file_name: string; safe_url: string; sha256?: string; page_count?: number
  parser_name: string; parse_status: string; parse_warnings?: Record<string, unknown>[]; parser_metadata?: Record<string, unknown>
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
export interface TaskResult {
  schema_version: string; task_id: string; task_type: TaskType; conclusion: string; mock: boolean
  summary: { title: string; description: string; statistics: Record<string, number> }
  files: ResultFile[]; risk_items: Record<string, unknown>[]; diff_items: DiffItem[]
  fact_matrix: Record<string, unknown>[]; rule_checks: Record<string, unknown>[]
  warnings: { code: string; message: string; requires_manual_review?: boolean; page?: number; confidence?: number; details?: Record<string, unknown> }[]
  advice: Record<string, unknown>
  metadata: { execution_mode: 'MOCK' | 'RULE_BASED'; workflow_version: string; rules_version: string; primary_model: string | null; model_runs: Record<string, unknown>[]; comparison_diagnostics?: ComparisonDiagnostics }
}
