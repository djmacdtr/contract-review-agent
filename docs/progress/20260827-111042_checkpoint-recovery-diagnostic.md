# Checkpoint recovery diagnostic — 2026-08-27 11:10

## Status

PARTIAL / REAL ACCEPTANCE STOPPED. The only permitted recovery task was run once
and failed during fact extraction. No retry endpoint was called after the
failure, no second task was created, and no commit or push was made.

This record contains operational counts and stable identifiers only. It does
not contain contract text, complete upstream responses, file URLs, or secrets.

## Offline verification

- The cross-task recovery regression with different task file IDs passed.
- The legacy source-identity checkpoint regression passed.
- The safe bottom-code propagation regression passed, including checkpoint-read
  failure propagation.
- Targeted local tests: `34 passed`.
- Compose targeted extraction and Worker integration tests: `43 passed`.
- Ruff, compileall, and `git diff --check` passed.
- The full Compose suite produced `364 passed, 2 failed`; both failures are the
  existing physical-page tests in `test_document_router.py` and
  `test_docx_page_locations.py`, unrelated to this checkpoint change. Their
  page-location behavior was not modified in this run.

## Unique real recovery

- Source task: `tsk_01M10EJTDQ8S62YEYHME3T5VBE`.
- New task: `tsk_01M10HF8CBD9HSJXYNCAZ7989E`.
- The retry response reported the correct source task and the new task was
  polled with GET only.
- Runtime path: host Python Worker, Docker PostgreSQL, and the local fixture
  file server. Docker Worker was stopped while the task ran.
- Progress reached `FACT_EXTRACTION / 80%` after parsing completed, then the
  task entered `FAILED`.
- Source checkpoint aggregate: profile-v2 3, numeric-v2 23, text-v4 18
  (44 total).
- Failed-task checkpoint aggregate: profile-v2 3, numeric-v2 21, text-v4 43
  (67 total). This shows that the expected full source reuse was not proven by
  the real run; the remaining planner/checkpoint-layout discrepancy requires
  offline investigation before another real task.

## First safe failure observed

The task GET returned:

- top-level code: `DYNAMIC_CHECK_INCOMPLETE`;
- stage: `FACT_EXTRACTION`;
- progress: `80`;
- `error.details`: `null`.

Consequently the persisted task did not expose the required `failure_stage`,
`chain`, `file_id`, `batch_depth`, `unit_count`, or bottom `failure_code` for
this run. The code now has an explicit map-level checkpoint-read/error
propagation path and an offline regression for it, but this failed task was
not rerun in accordance with the one-call rule.

## Runtime restoration

- Host Worker and local port-18081 file server were stopped after the failed
  task.
- Docker Worker was rebuilt from the current working tree and started.
- Docker Worker configuration was read-only checked: page-location enabled,
  HTTP downloads enabled, and the formal甲方 host allowlist retained.
- `.real-diagnostic-temp/` was not cleaned or modified deliberately.
- Console visual inspection was not performed by the agent; the task is not
  accepted as a completed console/report milestone.

## Remaining work

1. Offline-only investigation of why the historical 44-row source layout did
   not all match the current planner/checkpoint layout, without calling OCR or
   LLM services.
2. If a new code defect is confirmed, add a focused regression and obtain
   explicit authorization for a future unique real task. Do not reuse the
   failed task through retry in this run.

