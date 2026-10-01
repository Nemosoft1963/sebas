# Local Cowork Build Report

## 最新検証サマリー（2026-09-24 実装単位9）

過去節の件数（冒頭の63件、2026-09-20の403件、後続の414/434件、2026-09-23のfocused 27件など）は**当時の記録**として残す。上書きしない。本ブロックが現時点の最新実測である。

- datetime_utc: 2026-09-23 20:22:50 UTC
- datetime_jst: 2026-09-24 05:22:50 JST
- source_revision: `26f4f17a7e0f6320b3ee843df6188e299fb676d9`
- extractor REVISION (`app/vehicle_auto.py`): `20260923.3`
- container_image: 該当なし（Jenkinsワークスペースのみ。本番Docker未反映）
- scope: full（全体 pytest）
- command: `pytest -q`
- passed / failed / skipped / warnings: **528 passed, 0 failed, 1 skipped, 1 warning**（本セッション実測、25.18秒）
- runtime environment: Jenkinsコンテナ内。本番Dockerではない
- artifact log path: 本セッションのテスト出力のみ、ログファイル未保存
- healthcheck.ps1: **未実施**（本エージェントは powershell を実行できない）
- Docker build/deploy: **未実施**
- 実原本受入（単位6）: **未実施**
- 36項目の業務確認事項: **未回答**。実装は「推測しない・issueにして止める」の安全側既定

単位0〜8の実装範囲要約は末尾「2026-09-24 実装単位9」節を参照。再開manifest: `docs/verification/unit9_resume_manifest_2026-09-24.md`

## 最新検証サマリー（2026-09-23追記）

過去節の件数（冒頭の63件、2026-09-20の403件、後続の414/434件など）は**当時の記録**として残す。日時・版・対象範囲が異なる結果を混同しないこと。全体未実行の focused test を全体合格と読まない。

- datetime_jst: 2026-09-23（実装単位0+1。本番DB・起動中サービスは未変更）
- source_revision: `app/vehicle_auto.py` REVISION `20260923.1`（実装前確認値は `20260920.3`）
- container_image: 未確認（本セッションはコード編集のみ。Docker未実行）
- scope: focused（単位0記録 + 単位1 P0-1。全体回帰・healthcheckは未実施）
- command: `python -m pytest tests/test_vehicle_auto.py tests/test_vehicle_workflow.py -q`
- passed / failed / skipped: 27 passed, 0 failed, 0 skipped（実測。venv の pytest）
- runtime environment: Jenkinsワークスペース。システムPythonにpytest無し。`.venv` あり
- 記録同期: `docs/CHANGELOG_2026-09-21_triz_import.md` に2026-09-22確認時点の74件 defined 訂正を追記。旧ZIP一括復元はしない
- 注意: 独立原本照合（P0-2）は未実装。本単位の成果を確定用途へ配備しない

## 2026-09-09 Live draft placement recovery

- Confirmed stale content script was still running after file copy/reload; injected the current script into the identified extension context without restarting the browser.
- Fixed content-script storage.session access failure by using storage.local with explicit lastError handling.
- Added nonce/version-correlated render acknowledgement from the embedded document (expected origin, title, inquiry link, visible content; dialog previews excluded). Added full mouse-event sequence and explicit manual Insert confirmation status where synthetic events are ineffective.
- On the exact approved draft, verified the Insert dialog target and hit-test before trusted mouse input. Dialog closed; version-specific content verification passed, a body custom embed existed outside dialogs (367.7 by 326.8 CSS pixels), and Google reported all changes saved.
- Live draft asset version e3430bab0b16 is inserted. Public-page layout and publication are not verified; no Publish click performed.
- Node syntax/guard tests and image build PASS. Running extension file updated; browser profile retained.

## 2026-09-09 Draft placement verification

- Replaced unconditional success after a 1.2-second delay with dialog-dismissal, version-marker and Drive-save checks. Inaccessible embedded documents are reported as unverified.
- Always replace stale preview code with the current approved payload. Require enabled, unambiguous action buttons and check textarea contents.
- Persist insertion checkpoints before clicking Insert to prevent blind duplicate retries. Added request timeout, stage messages and single-flight control. Removed automatic blank-site creation from the placement path and automatic resumed writes.
- Node syntax check and placement guard tests PASS (absent/stale/preview-only/hidden/inaccessible/disabled/ambiguous cases). Updated running extension file and built next-start image.
- Real Google Sites end-to-end placement is not yet verified; open editor pages need reload to run updated content scripts.

## 2026-09-09 Google Sites helper panel obstruction

- Moved the extension helper to the bottom left and added collapse and hide controls; a page reload restores a hidden panel.
- Hid the existing helper on the currently open Sites page and verified hidden=true (one panel). No page reload, publication action, or container restart.
- Copied the updated extension into the running container and built the next-start image successfully.

## 2026-09-09 Browser control ownership correction

- Managed viewer assigned control only to the first connected tab; later peers had their input ignored even though their stream connected successfully.
- Added control transfer on managed-viewer focus and visibility, notifying the old and new peers. Authentication remains in the existing websocket route.
- Rebuilt and recreated only google-publisher-browser. Mocked two-peer acceptance test passed: focus transfers control, current peer input dispatches, old peer input is ignored, and focus can return.
- scripts/healthcheck.ps1: all PASS. Actual user-screen click behavior remains unverified; connection success alone is not evidence of end-to-end recovery.
- Existing Google browser profile volume retained.

## Date

2026-09-01

## Host

- OS: Microsoft Windows 11 Home 64-bit, 10.0.26200 (build 26200)
- GPU: NVIDIA GeForce RTX 4070 Ti SUPER, 16376 MiB
- NVIDIA Driver: 595.97
- Docker Client / Engine: 28.4.0 / 28.4.0
- Docker Compose: 2.39.4-desktop.1
- Ollama: 0.32.15

## Integrated Components

- Local Voice AI integrated front
- Project context folders with original-file retention and text extraction for text/code, PDF and Office documents
- Persistent project goals, multi-AI reviewed local-LLM plans, dependency-aware parallel agents and UI progress reports
- Claude / ChatGPT / Gemini / Grok / Meta research orchestrator
- Browser voice input/output and Windows microphone bridge
- Open WebUI
- Open WebUI Computer
- Dedicated persistent Workspace: `C:\Users\kanto\LocalCowork\workspace`
- Windows native Ollama controller

## Models

| Model | Size | Smoke test |
|---|---:|---|
| gpt-oss:20b | 13 GB | PASS: `LOCAL_COWORK_OK`, 100% GPU, context 8192 |
| qwen3.5:9b | 6.6 GB | PASS: `LOCAL_COWORK_OK`, context 8192 |

The integrated front uses `gpt-oss:20b` as the local controller. The Web start operation loads it for 30 minutes; Web stop unloads it.

## Acceptance Tests

| ID | Result | Note |
|---|---|---|
| T-01 Integrated Front | PASS | HTTP 200, Local Ollama controller |
| T-02 Open WebUI | PASS | HTTP 200 |
| T-03 Computer | PASS | HTTP 200 |
| T-04 Workspace file read/write | PASS | inbox read and output/acceptance.md write |
| T-05 Terminal/Python | PASS | Python 3.12.12, 123 × 456 = 56088 |
| T-06 Workspace boundary | PASS | Only /workspace bind and /data volume |
| T-07 Git | PASS | git 2.39.5, status and staged diff |
| T-08 Local model/GPU | PASS | Both models responded; gpt-oss 100% GPU |
| T-09 Restart persistence | PASS | output/acceptance.md remained after compose restart |
| T-10 Voice I/O | PASS | Browser MediaRecorder/speechSynthesis retained; C920 mic bridge health 200 at 44.1kHz |
| T-11 Multiple external AI | PASS | Claude, ChatGPT, Gemini, Grok, Meta settings retained; regression tests passed |
| T-12 Goal-driven project execution | PASS | Live Docker test: local plan 5 tasks, 5/5 completed, 15 persisted events, 1,868-character final report, Markdown download HTTP 200 |
| T-13 Context folder upload | PASS | Recursive all-file picker; CP932 CSV text extracted; PNG original retained and byte-identical on download; safe relative paths persisted |
| T-14 Project memory Markdown | PASS | Goal, plan/procedure, execution log and final report are automatically updated under `__project_memory__/`; memo files are downloadable |
| T-15 Project Workspace operations | PASS | Dedicated web mount, safe mkdir/write/append/copy/move, automatic overwrite backups, upload/download API and UI |
| T-16 Dependency-aware parallel agents | PASS | Stable task keys, dependency validation, parallel limit 1-4, blocked descendants and retry support |
| T-17 Multi-AI plan review | PASS | Selected configured AIs review concurrently; local controller LLM alone refines and finalizes; local draft fallback on review failure |
| T-18 Capability-gap remediation loop | PASS | Structured gap detection, selected external AI reviews, local-controller integration, one bounded retry, unresolved report in UI/MD |
| T-19 Yayoi Accounting Excel | PASS | Local XLSX/XLSM parsing; journal period/debit/credit/difference and trial-balance sheet/table preview; macros never executed |
| T-20 Safe local table execution | PASS | Declarative CSV/TSV/XLSX/XLSM profile, filter, date derivation, aggregate, one-to-one join and CSV/Markdown output; no arbitrary code |
| S-01 Localhost ports | PASS | 127.0.0.1:8099, 3000, 8000 |
| S-02 No Docker socket | PASS | Mount inspection |
| S-03 No host-drive mount | PASS | Only C:/Users/kanto/LocalCowork/workspace |
| S-04 Secrets ignored | PASS | .env and secret patterns ignored |
| S-05 Destructive operation policy | PASS | AGENTS.md / SECURITY.md |
| D-01 AI handoff / sales description | PASS | `SYSTEM_DESCRIPTION_FOR_AI.md` documents architecture, functions, APIs, data, security boundaries, operations, limitations and sales checks without secret values |

## Automated Validation

- Python tests: 63 passed, 1 skipped (Windows symlink privilege unavailable)
- Yayoi Accounting focused tests: 6 passed (journal/headerless import/trial balance/XLSX/XLSM/error handling)
- Project mission JavaScript syntax: PASS
- PowerShell syntax: PASS
- Docker Compose config: PASS
- Integrated healthcheck: PASS
- Open WebUI to Ollama: HTTP 200
- Computer to Ollama: HTTP 200
- Cowork dashboard/API: PASS

## Known Issues

- The original project is on a network share. Docker Desktop cannot bind mount UNC or the mapped Z drive, so the restricted runtime Workspace uses the local dedicated path above.
- The first gpt-oss load after switching from another large model can encounter a transient CUDA initialization error. Start retries once; the final stop/start test passed.
- Open WebUI and Computer require first-run human admin account creation.

## Manual Steps Remaining

1. Open WebUI first admin account
2. Computer first admin account using its one-time setup URL
3. Register `/workspace` in Computer
4. Register `http://host.docker.internal:11434` in Computer
5. If needed, create a Computer Gateway key and register `http://cptr:8000/v1` in Open WebUI

Passwords, setup tokens and Gateway keys are not recorded.

## 2026-09-02 Incident Fix: Registered Spreadsheet Execution

### Root cause

- The vehicle-evaluation project had an empty Workspace setting, so execution used the UUID fallback folder while the source Excel/PDF files were stored under `/workspace/projects/車両別評価データの作成`.
- Generated project memos were ordered before uploaded source documents and consumed much of the prompt budget. Later payroll workbooks could be present in storage but absent from the task-visible excerpt.
- Workspace extraction and registered-context extraction duplicated large workbook text. With Ollama configured for 8,192 context tokens, the combined prompt could exceed the practical input budget.
- The local executor consequently treated the no-arbitrary-program rule as if the already extracted tables were unreadable and incorrectly reported a missing Python runtime.

### Improvement

- Added a local-only execution source bundle with a hard 20,000-character limit.
- User-uploaded sources are prioritized over generated memos, and every source receives a fair excerpt so one large workbook cannot displace later files.
- Extracted payroll/financial contents are appended only after any external-research step, preventing them from being sent to external reviewers.
- When registered extracted sources exist, the Workspace contributes its file manifest only; duplicate file bodies are omitted.
- The existing vehicle-evaluation project now points to its actual dedicated Workspace folder.

### Validation

- Python tests: 55 passed, 1 skipped.
- New regression coverage: fair multi-file source delivery, generated-memo exclusion, local-only payroll-data confinement, and manifest-only Workspace snapshots.
- Live Docker source coverage: all six registered source groups were present (fuel, vehicle master, profit/loss, two payroll workbooks, insurance PDF), 19,146 characters total.
- Integrated healthcheck: PASS for Ollama, Integrated Front, Open WebUI, Computer, containers, Workspace, localhost bindings, and configured models.

## 2026-09-02 Incident Fix: Empty Local LLM Response

### Root cause

- The production-equivalent task prompt was 24,607 characters and 15,329 input tokens, exceeding the previous 8,192-token context window.
- The local `gpt-oss:20b` response used its entire 1,536-token output allowance for internal reasoning and returned no visible content with `done_reason=length`.
- A partial response stopped by the length limit is not safe to execute as a structured task result.

### Improvement

- Project-task completion now validates both visible content and the Ollama completion reason.
- Empty or length-truncated output is retried locally with low reasoning, a 16,384-token context window, and a 6,144-token output allowance.
- A final bounded retry compacts only the task prompt while preserving the system instruction and source tail.
- Retry diagnostics record only sizes and completion reasons; prompts, source data, secrets, and model reasoning are not logged.
- Interactive chat streaming keeps its existing settings. No data is sent to an external AI by this recovery path.

### Validation

- Python tests: 58 passed, 1 skipped.
- Production-equivalent live task: first attempt correctly rejected (`length`, 0 visible characters); automatic retry completed with `stop` and 1,129 visible characters.
- Successful retry used 16,384 context tokens and a 6,144-token output allowance.
- Integrated healthcheck: PASS for Ollama, Integrated Front, Open WebUI, Computer, containers, Workspace, localhost bindings, and configured models.
- The affected mission was moved from `failed` to `paused`; its completed task remains completed and pending tasks are ready for an explicit resume.

## 2026-09-02 Feature: Safe Local Table Execution

### Problem addressed

- The project executor could inspect extracted spreadsheet text but could not perform full-row calculation or save verified aggregate results.
- It therefore reported a missing Python/pandas capability even though the required operation was structured table processing.

### Implementation

- Added a declarative, local-only table executor for CSV, TSV, XLSX and XLSM.
- Supported operations are profile, filter, select/rename, sort, year/month/date derivation, bounded arithmetic, grouped aggregation, one-to-one equality join, and CSV/Markdown output.
- Multi-step operations can aggregate one table, save a local CSV, and use that result in a later join.
- Context sources are restricted to the active project; Workspace sources and outputs use the existing traversal/symlink-safe project scope and atomic backup-aware writes.
- Arbitrary Python, imports, shell, delete, macro execution and formula execution remain unavailable.
- The mission UI receives metadata-only `table_operation` events, and generated results appear in the existing project Workspace list for download.
- If gpt-oss finishes with an empty visible body even after bounded low-reasoning retries, the last local fallback disables hidden reasoning and requests only the final visible answer.

### Validation

- Python tests: 63 passed, 1 skipped.
- New acceptance coverage: CP932 driver/month aggregation, chained payroll aggregation and profitability join, cross-project source rejection, traversal rejection, unknown-code rejection, and mission-event integration.
- Docker live test: 3 source rows were grouped into 2 result rows (`A=350`, `B=80`) and written as CSV inside an automatically removed container temporary directory.
- Production-equivalent dry run: the local LLM returned a 996-character structured response with one `transform` operation targeting `成果フォルダ/driver_monthly_data.csv`, zero capability gaps, and no Python-runtime complaint. The dry run did not write user files.
- `/api/health` reports `safe_table_executor=true`; integrated healthcheck PASS.

## 2026-09-02 Feature: Evidence-Based Verification and Correction

### Problem addressed

- A local LLM could previously describe a CSV or Excel transformation as completed even when no table operation ran and no output file existed.
- Task self-reports were accepted as completion, and the final report could still leave the mission at 100% even when it explicitly described the goal as incomplete.

### Implementation

- Every table or artifact-producing task is now checked against backend-owned evidence: recorded safe operations, a real non-empty file inside the project Workspace, and positive output row/column counts for table results.
- Claims that Python/pandas executed are rejected because arbitrary Python execution is not an available capability.
- When evidence is insufficient, the local executor receives the exact failed checks and may perform one declarative correction. The correction is verified again before the task can become completed.
- If the correction still has no evidence, the task becomes failed with a verification report instead of being counted as complete.
- Final reporting now returns a strict goal status. Partial or failed goal conditions mark the mission and the final task failed; they no longer produce a misleading completed event.
- Added `POST /api/projects/{project_id}/mission/verify` and the UI action **完了検証・補正**. It audits already completed work, reopens invalid tasks and their dependents, and prepares them for an explicit local resume.
- Verification and automatic correction are local-only. This path does not call external AI providers.

### Validation

- Python tests: 66 passed, 1 skipped.
- JavaScript syntax check: PASS.
- New acceptance coverage: missing evidence corrected once by a safe table operation; corrected file/rows verified; unsupported Python claim rejected; legacy completion reopened; dependent task reset; partial final goal rejected.
- Docker web image rebuilt and `/api/health` reports `safe_table_executor=true` with `gpt-oss:20b` ready.
- Integrated `scripts/healthcheck.ps1`: PASS for Ollama, all three web services, Workspace, localhost-only ports and installed local models.
- Existing vehicle-evaluation mission audit found all 7 historical tasks lacked sufficient backend evidence or depended on invalid work. It was changed from misleading `completed 7/7` to `paused 0/7`, with all tasks ready for verified correction and re-execution.

## 2026-09-03 Full Review: Source-Aware Planning and Schema Validation

### Problems found

- Generated plans could cite spreadsheet/PDF filenames without a stable project-scoped source reference.
- The planner could invent unsupported Python/pandas steps, XLSX/ZIP outputs, absolute `/workspace` paths, placeholder values or nonexistent schemas.
- A plan could state row/column counts that disagreed with the inspected workbook.
- A profitability plan could omit a registered revenue/profit source even when the project constraint required every usable original.
- Generated project memos could displace user originals from the bounded planning context.

### Improvements

- Added stable `context:<id>` source references and backend verification that each reference belongs to the active project, is an original rather than a memo, contains source data and remains inside the allowed Workspace.
- Safe table inventory now records each source, sheet, detected header row, data row count, exact column preview and `column_count`. A bad/empty sheet is isolated so later valid sheets remain available.
- Planning and correction prompts receive the local-only source/table inventory. User originals are prioritized ahead of generated memos.
- Added pre-save plan gates for arbitrary programs/shell, unsupported XLSX or ZIP output, placeholder assumptions, nonexistent dependencies and absolute Workspace paths.
- Added source-schema validation: numeric row/column claims tied to a registered ref must match an inspected sheet before the plan can replace the previous version.
- Added required-source coverage: goals involving profit/cost and constraints requiring all usable materials must cite every applicable registered original. Missing refs trigger bounded local repair; failed repair preserves the previous plan.
- Capability-resolution events are recorded only after backend evidence verification succeeds. PDF/document tasks require a validated source reference and extracted content; table tasks require an executed declarative table operation.
- External AI review payloads were not expanded with local file metadata. Local filenames, table schemas and original contents stay local.

### Live project correction

- Restored the vehicle-evaluation mission goal, success criteria and constraints after detecting a client-side response-decoding issue during validation.
- Created plan version 7 with seven local tasks. It uses all six registered originals: fuel, vehicle master, vehicle profit/loss, two payroll workbooks and the insurance PDF.
- Five source-processing tasks can run independently, followed by a dependency-aware integration task and an independent verification/report task.
- Unsupported assumptions are prohibited: missing 2026 month/vehicle mappings are left unresolved and reported instead of being silently replaced with zero.
- Plan status is `ready`; execution has not been started.
- Original external provider settings were restored to Claude, ChatGPT and Gemini. No external AI or web service received project data during this validation.

### Validation

- Related source/schema/plan tests: 30 passed.
- Full Python suite: 83 passed, 1 skipped.
- Docker image rebuilt successfully.
- Live plan checks: all six required refs present; no absolute Workspace path; no Python/pandas/DataFrame/shell/ZIP dependency; all tasks are local.
- Integrated `scripts/healthcheck.ps1`: PASS for Ollama, Integrated Front, Open WebUI, Computer, all containers, Workspace, localhost-only ports and configured models.

## Rollback

Run `scripts/stop.ps1`. Named volumes and Workspace are preserved. Never use `docker compose down -v` for normal operation.

## 2026-09-03 Feature: Workspace Source Materialization and Safe Table Action Compatibility

### Improvements

- Context files added individually or from a selected browser folder are now persisted under the active project's `originals/` Workspace tree while retaining their safe project-scoped context references.
- Added a local-only endpoint to materialize already registered originals into the project Workspace; generated memos are excluded.
- Materialized all six registered originals for the vehicle profitability project, including `年間燃料費シート.xlsx` (10,980 bytes).
- The safe table executor now normalizes bounded declarative aliases (`extract`, `select`, `filter`, `aggregate`, `group`, `join`, `export`, `convert`) to `transform`, and inspection aliases to `profile`.
- Arbitrary actions such as Python or shell remain rejected.
- Workspace operations may accept `/workspace/<active-project>/...` paths only when they resolve to the current project scope. Cross-project and traversal paths remain rejected.
- Reset the cancelled vehicle profitability mission to `paused`; all seven tasks are `pending` and ready for explicit resume.

### Validation

- Changed-module Python compilation: PASS.
- Related tests: 28 passed, 1 skipped.
- Full Python suite: 86 passed, 1 skipped.
- Docker web image rebuilt and deployed successfully.
- Registered-original materialization: 6 files stored in the project-local `originals/` tree.
- Integrated `scripts/healthcheck.ps1`: PASS for Ollama, all three web services, containers, Workspace, localhost bindings, and configured models.
## 2026-09-03 Fix: Execution JSON Compatibility Correction

- Paused the active vehicle profitability mission before deployment.
- `source_references` now accepts safe object forms (`reference`, `ref`, context IDs, or Workspace paths), ignores empty/null entries, and still validates every resulting reference against the active project boundary.
- Unknown table action labels are accepted only when the payload contains a registered source, an output path, no code/script/shell/command/SQL keys, and only declarative transform fields. Arbitrary execution remains rejected.
- Added regression coverage for inferred extraction actions and object-form source references.
- Related tests: 25 passed, 1 skipped. Full suite: 88 passed, 1 skipped.
- Docker web image rebuilt and deployed. Integrated healthcheck: PASS.
- Failed tasks were reset and the mission was resumed; T1 and T2 started as attempt 4.
## 2026-09-03 Fix: Legacy Executor JSON Repair

- Diagnosed five failed source-processing tasks caused by prose-only correction responses, legacy operation arrays, legacy field names and `/workspace/...` output paths.
- Added decoding for JSON operation arrays embedded in prose/code fences.
- Added safe normalization for `operation`, `source_reference`, `file_path` and known legacy table operation names.
- `/workspace/...` paths are resolved inside the active project scope; traversal protection remains active.
- Added one bounded final JSON-format repair only for table/source tasks that returned no executable evidence.
- Arbitrary Python, shell, script, command and SQL fields remain rejected.
- Full tests: 88 passed, 1 skipped.
- Docker web image rebuilt and deployed; integrated healthcheck PASS.
- Failed mission tasks were reset and execution resumed with T1/T2 on attempt 5.
## 2026-09-03 Unresolved Mission Report Correction

- Investigated mission `4c81ad7ca3114317a3781c676b81089a` and preserved completed task artifacts.
- Scoped source inventories and verification to task-declared context IDs.
- Added safe compatibility for legacy/nested table operation schemas, source-reference objects, sheet/output aliases, and project-scoped workspace paths.
- Ignored unsupported generated workspace operations without executing them; explicit cross-project context/workspace references remain rejected.
- Bound generated table-operation sources to each task's declared registered originals.
- Security regression: arbitrary code/script/shell/SQL keys are rejected before action alias normalization.
- Tests: `91 passed, 1 skipped`.
- Health check: PASS for Ollama, integrated front, Open WebUI, Computer, containers, workspace, localhost ports, and required models.
- Deployment: rebuilt and restarted the `web` service; failed mission tasks were retried.
## 2026-09-03 Excel Table Output Support

- Added `.xlsx` as an allowed SafeTableExecutor output format alongside `.csv` and `.md`.
- Implemented a dependency-free OOXML workbook writer with a single `Result` worksheet.
- Added atomic, project-scoped binary artifact writes with backup preservation.
- Verified generated workbooks by reading them back through the existing workbook parser.
- Tests: `92 passed, 1 skipped`.
- Health check: PASS.
- Deployment: rebuilt/restarted `web`; mission retry resumed from T1 while preserving T2-T5.
## 2026-09-03 Downstream Table Integration Correction

- Added safe aliases for `read_csv`, `read_xlsx`, `load_csv`, `load_excel`, and `load_table`.
- Prevented dependency tasks without explicit context IDs from inheriting every registered original; downstream integration now uses prior Workspace artifacts instead of unrelated PDFs.
- Tests: `93 passed, 1 skipped`.
- Health check: PASS.
- Deployment: rebuilt/restarted `web`; T6 and T7 retry started while T1-T5 remain completed.
## 2026-09-03 Merge Operation Compatibility

- Confirmed the mission was stopped, not actively running: T6 failed on generated `merge/inputs/keys` schema and T7 was blocked.
- Added bounded conversion of `merge` with up to five inputs into safe one-to-one joins.
- Added collision-safe inclusion of right-side columns and support for nested source objects.
- Tests: `94 passed, 1 skipped`.
- Deployment: rebuilt/restarted `web`; T6/T7 retry started while T1-T5 remain completed.
## 2026-09-03 Dependency Table Evidence Correction

- Diagnosed T6 verification failure: downstream integration was not classified as requiring table operations after direct source contexts were intentionally removed.
- Dependency tasks containing aggregation/integration terms now require audited table operations and a real artifact.
- Tests: `94 passed, 1 skipped`.
- Deployment: rebuilt/restarted `web`; T6/T7 retry started.
## 2026-09-03 Downstream Context Boundary Correction

- Diagnosed T6 failure caused by a hallucinated, nonexistent `context:` path during format repair.
- Generated context references are now constrained to the exact context IDs supplied to the task.
- Dependency-only T6 receives an empty context allowlist and must use Workspace artifacts; direct-source tasks retain their supplied registered originals.
- Cross-project explicit references remain rejected.
- Tests: `94 passed, 1 skipped`.
- Deployment: rebuilt/restarted `web`; T6/T7 retry started.
## 2026-09-03 Table Operation Limit Expansion

- Diagnosed T6 failure caused by the safe table-operation count exceeding the previous limit of 10.
- Increased the bounded per-task limit to 20 for multi-source integration; 21 or more operations remain rejected.
- Tests: `95 passed, 1 skipped`.
- Deployment: rebuilt/restarted `web`; T6/T7 retry started.
## 2026-09-03 Dependency Artifact Source Binding

- Diagnosed T6 selecting a source Excel workbook with a concatenated/nonexistent sheet name during format repair.
- Downstream table operations now bind generated non-Workspace inputs to the latest audited outputs of their declared dependency tasks.
- Direct source tasks retain their explicit registered-context scope; cross-project access remains rejected.
- Tests: `95 passed, 1 skipped`.
- Deployment: rebuilt/restarted `web`; T6/T7 retry started.
## 2026-09-03 T6 common execution hardening

- Normalized safe table-operation aliases before dispatch, including read/load, merge/join, and write/save/export variants.
- Added pipeline source chaining and bound dependency integration inputs to audited dependency artifacts.
- Added mandatory task artifact-path contract verification for folder-qualified CSV/XLSX/Markdown outputs; unrelated artifacts can no longer satisfy completion.
- Reopened previously completed tasks whose evidence did not match their declared outputs and restarted the mission.
- Tests: 96 passed, 1 skipped.
- Healthcheck: PASS (Ollama, integrated front, Open WebUI, Computer, containers, workspace, localhost-only ports, local models).
## 2026-09-03 Mission artifact download

- Added `GET /api/projects/{project_id}/mission/artifacts/download` for an in-memory ZIP of project-scoped `成果フォルダ/` and `output/` files.
- Added the mission-screen `成果物ZIP保存` button, enabled after mission completion.
- Preserved Workspace path validation; files outside the project scope are not included.
- Validation: 96 passed, 1 skipped; healthcheck PASS; live ZIP response HTTP 200, application/zip, 35,265 bytes, PK signature.
## 2026-09-03 — Table-source plan execution guard

- Diagnosed the failed vehicle-profit mission: a table operation retained a PDF context reference inside inputs/nested operations even though the primary source correction handled only a single source field.
- Added _constrain_table_operation_sources to normalize source aliases, inputs, joins, and nested operations against the task's assigned CSV/TSV/XLSX/XLSM sources.
- Table execution now fails before file parsing with a clear assignment error when a task has no compatible tabular source, instead of forwarding a PDF/document to the table executor.
- Added regression coverage for mixed PDF/XLSX multi-source operations and document-only assignments.
- Validation:
  - Python syntax compilation: PASS
  - Direct host regression assertions: PASS
  - Rebuilt and restarted web: PASS
  - In-container regression assertion: PASS
  - scripts/healthcheck.ps1: PASS (all checks)
  - Full pytest suite was not run because the host environment does not have the optional pytest dependency installed; no package installation was performed.
- Existing failed mission state and prior artifacts were preserved; no automatic retry was triggered.
## 2026-09-03 — Path runtime failure fix

- Fixed the mission runtime failure name 'Path' is not defined by importing Path from pathlib in app/project_manager.py.
- Confirmed that hallucinated bare table filenames are normalized by the table-source guard to an assigned registered tabular context before execution.
- Validation:
  - Python syntax compilation: PASS
  - Host Path/source-guard runtime assertion: PASS
  - Rebuilt and restarted web: PASS
  - In-container Path/source-guard assertion: PASS
  - scripts/healthcheck.ps1: PASS (all checks)
- Existing failed mission state was preserved; retry was not started automatically.
## 2026-09-03 — Complete table source-alias normalization

- Diagnosed the remaining annual-mileage retry failure: SafeTableExecutor accepts input, input_file, and file as legacy source aliases, while the source guard previously normalized only source/source_reference/source_ref/input_source/source_file.
- Extended the guard to normalize and remove every accepted source alias before table execution.
- Bare paths, hallucinated CSV names, document context references, inputs, joins, and nested operations are now constrained to assigned registered CSV/TSV/XLSX/XLSM context sources; workspace references remain allowed.
- Added regression coverage for input/input_file/file alias bypass attempts.
- Validation:
  - Python syntax compilation: PASS
  - Host regression assertions covering all aliases, inputs, joins, and nested operations: PASS
  - Rebuilt and restarted web: PASS
  - In-container alias regression assertion: PASS
  - scripts/healthcheck.ps1: PASS (all checks)
- Existing failed mission and completed artifacts were preserved; retry was not automatically triggered.
## 2026-09-03 参照原本・Excelシート補正の強化

- 対象: 車両別評価データ作成ミッション（5/9完了、T3「年間走行距離集計」で停止）
- 直接原因: 表原本refの補正後も、LLMが生成した存在しないシート名「給与」が残り、SafeTableExecutorが `Excelシートが見つかりません` で停止した。
- 修正:
  - CSV/TSV/XLSX/XLSM以外を除外する既存制約を維持。
  - XLSX/XLSMの実在シートを `read_workbook` で実行直前に取得。
  - 指定シートが別の許可済み原本に実在する場合は、その原本を優先。
  - 選択原本に指定シートが存在しない場合は、実在する非空シート（なければ実在シート）へ補正。
  - `sheet_name` / `table_name` エイリアスも正規化し、ネスト操作とjoinにも同じ制約を適用。
- 再発防止テスト:
  - 指定シートを含む原本の優先選択。
  - 存在しないシート名から実在する非空シートへの補正。
- 検証:
  - `python -m py_compile app/project_manager.py tests/test_project_manager.py`: PASS
  - ホスト直接回帰確認: PASS
  - webコンテナ内回帰確認: PASS
  - `docker compose up -d --build web`: PASS
  - `scripts/healthcheck.ps1`: PASS（全項目）
- 現在状態: ミッションは自動再試行せず failed のまま。T3再実行前のコード側抑制は反映済み。

## 2026-09-05 Controlled Alternate-AI Failure Recovery

- Added a bounded fallback path for ordinary task execution errors and backend-verification failures.
- When the project explicitly allows external AI and has configured selected providers, the orchestrator sends only the goal, success criteria, constraints, task contract and bounded failure detail for independent recovery advice.
- Registered source originals, extracted source contents, Workspace snapshots and credentials are excluded from the alternate-AI recovery payload.
- The local controller treats alternate-AI responses as critique data, creates the final recovery guidance, and retries the task once with the existing safety boundaries and backend evidence verification.
- Capability-gap handling remains on its existing bounded remediation path. Cancellation never triggers failover, and projects without explicit external-AI consent retain the previous fail-closed behavior.
- Added audit events for failover start, review completion, retry, success, failure and policy-based skip.
- Added regression coverage for successful permitted failover and prevention of external contact without consent.
- Related tests: `51 passed`.
- Full Python suite: `103 passed, 1 skipped`.
- Docker web image rebuilt and deployed successfully.
- Integrated `scripts/healthcheck.ps1`: PASS for Ollama, Integrated Front, Open WebUI, Computer, containers, Workspace, localhost-only ports and configured models.

## 2026-09-05 Bounded Adaptive Task Retry

- Added backend failure classification for unsupported operations, verification/artifact failures, format/schema failures, transient provider failures, execution-quality failures, missing inputs/configuration, and safety/permission failures.
- Added normalized SHA-256 failure fingerprints so repeated equivalent failures can be detected despite changing IDs, numbers or whitespace.
- Added two bounded recovery strategies per failed task. Strategies clarify the execution contract, substitute unsupported operations, or reduce the execution unit without weakening the approved success criteria.
- `create_table` and similar unsupported table actions are explicitly redirected: narrative/list artifacts must use `write_text`; registered tabular inputs may use only the safe `profile` or `transform` operations.
- Local recovery now remains available when external AI is not permitted. When external AI is explicitly permitted and selected, one review round may advise the local controller; original files, extracted contents, Workspace snapshots and credentials remain excluded.
- Automatic retry stops for missing inputs/configuration, permission/authentication failures, safety-boundary violations, repeated fingerprints, cancellation, or exhausted strategy budget.
- Added audit events for classification, strategy selection, attempt failure, duplicate-stop, human-required stop and budget exhaustion.
- Related tests: `52 passed`.
- Full Python suite: `104 passed, 1 skipped`.
- Docker web image rebuilt and deployed successfully.
- Integrated healthcheck: PASS for Ollama, Integrated Front, Open WebUI, Computer, containers, Workspace, localhost-only ports and configured models.
- Sales mission `2a39815e16e4422581aa29b777a92910` was reset for retry and resumed. Task A3 is running as attempt 5; completed tasks A1/A2 were preserved.

## 2026-09-06 Markdown Table Verification Recovery

- Fixed a false-positive verifier rule that treated any dependent task containing the English word `table` as a structured CSV/Excel operation.
- Markdown documents containing presentation tables, such as a contract price table, now use normal `write_text` artifact verification unless a real CSV/TSV/XLSX/XLSM source or output requires the safe table executor.
- Explicit CSV/TSV/XLSX/XLSM outputs continue to require a `table_operation` audit record.
- Added regression coverage for both the Markdown price-table case and the structured CSV-output case.
- Targeted suite: `24 passed`.
- Full Python suite: `106 passed, 1 skipped`.
- Docker web image rebuilt and deployed successfully.
- Integrated `scripts/healthcheck.ps1`: PASS for all checks.
- Sales mission `2a39815e16e4422581aa29b777a92910` was reset from failed to retry-ready and resumed; its seven completed tasks were preserved.

## 2026-09-06 Required Markdown Artifact Enforcement

- Artifact verification now extracts exact `workspace:result/...` contract paths and compares them with actual audited writes.
- Automatic correction now includes artifact-only failures in the final JSON-format repair path, not only table/source failures.
- Markdown deliverables require an `operations/write_text` item for every exact path, with complete non-placeholder content satisfying the acceptance criteria.
- Added regression coverage for `workspace:` Markdown and text artifact-path extraction.
- Targeted suite: `25 passed`.
- Full Python suite: `107 passed, 1 skipped`.
- Docker image rebuilt; integrated healthcheck passed in full.
- Mission `2a39815e16e4422581aa29b777a92910` resumed. Task 8 completed after writing and verifying `result/contract_plans.md` (5,193 bytes); task 9 started automatically.

## 2026-09-06 Safe Source-Free Table Creation

- Added bounded `create_table` support for new CSV/XLSX/Markdown tables that do not transform an existing source.
- The operation accepts only declared columns, scalar row values and a Workspace output path; arbitrary code, nested values and out-of-bound row/column counts remain rejected.
- Internal `generated:inline` audit provenance is no longer misinterpreted as an external `source_reference`.
- Exact CSV/XLSX/TSV paths now force one `create_table` operation per required artifact, even when an unrelated file was written.
- Exact TXT paths now force audited `write_text` output.
- Full Python suite: `109 passed, 1 skipped`.
- Docker image rebuilt and integrated healthcheck passed in full.
- Mission task 12 generated and verified `result/sales_plan.csv` and `result/kpi_dashboard.csv`; task 13 generated `result/deal_tracker.csv`; task 14 generated `result/validation_log.txt`.
- The final goal evaluator correctly returned `partial`: all planned execution tasks reached artifact verification, but the broader mission still lacks some goal-level deliverables such as the post-contract implementation standard procedure. The mission was not falsely marked complete.

## 2026-09-06 Bounded Goal-Supplement Loop

- Added automatic goal supplementation when all planned tasks pass but the final evaluator returns `partial` or `failed` with actionable unmet conditions.
- Completed tasks and verified artifacts are preserved; only new pending tasks are appended to the existing plan.
- Generic partial-result messages are expanded from the final report's residual-issues section when available.
- Safety bounds: maximum 3 supplementation rounds, maximum 5 tasks per round, and stop after the same unmet-condition fingerprint repeats twice.
- Permission, authentication, secret/personal-data, missing-original-input, contract-signing and external-send conditions remain human-controlled hard stops.
- Added persistent append-only plan support and audit events for planned, exhausted, duplicate-stop and human-required outcomes.
- Full Python suite: `110 passed, 1 skipped`.
- Docker image rebuilt and integrated healthcheck passed in full.
- Mission `2a39815e16e4422581aa29b777a92910`: original 14 tasks completed; 5 residual goal tasks were appended automatically (19 total), and supplement task 15 started without user intervention.

## 2026-09-06 Quality-Gated Plan Regeneration

- Strengthened plan generation so every success criterion is assigned, every deliverable uses an exact Workspace path, and acceptance criteria include machine-checkable headings, keywords, columns, and minimum row counts.
- Added content-quality validation for Markdown/text/tabular artifacts, including minimum useful content and rejection of deferral prompts or unrelated story responses.
- Added minimum-row enforcement for tabular acceptance criteria.
- Changed malformed/non-JSON final assessments to fail closed instead of being treated as passed.
- Added actual Workspace inventory to the final goal-assessment prompt and reject irrelevant or deferred final reports.
- Updated final-assessment fixtures to the strict JSON contract.
- Full Python suite: `112 passed`.
- Docker image rebuilt and integrated healthcheck passed in full.
## 2026-09-06 Plan Dependency and Final-Verification Gate

- Added ordered input/output dependency validation for result artifacts.
- Added hard rejection for missing upstream inputs, unsupported audit-log files, Excel formula/sort requirements, and missing final-verification tasks.
- Added planner instructions to use system audit events and require result/final_verification.md for plans with five or more tasks.
- Added tests for missing-input and fake-audit-log rejection.
- Full Python suite: `115 passed`.
- Docker image rebuilt and integrated healthcheck passed in full.
- A post-change regeneration candidate scored 90/100 but was correctly rejected due to two missing upstream inputs and one unsupported custom audit-log requirement; plan version 11 was preserved and not approved.
## 2026-09-06 Task-Level Plan Repair

- Added task-level plan repair for quality-gate failures. Only returned task_key descriptions and acceptance criteria are merged; valid tasks, order, and dependencies are retained.
- Added a maximum of three bounded partial-repair rounds with audit events for each success or failure.
- Added deterministic insertion of a final-verification task for plans with five or more tasks when missing, depending on all leaf tasks.
- Revalidates the complete plan structure, dependencies, capabilities, and quality score after every partial repair.
- Full Python suite: `116 passed`.
- Docker image rebuilt and integrated healthcheck passed in full.
- The first live regeneration after deployment was rejected before partial repair because the model returned an invalid task count; existing plan version 11 remained untouched and unapproved.
## 2026-09-07 Structured Planning Reconstruction

- Added app/structured_planning.py: criterion extraction, small local planning responses, deterministic output contracts, artifact manifest, ancestry validation, and actual Markdown/CSV verification.
- Numbered-criteria Workspace projects use the new path; legacy unnumbered projects retain their existing path. New-path planning is local-only; it does not invoke external plan reviewers.
- Includes a source-review/needs-discovery preparation task and a final verification task, with up to 18 criteria inside the existing 20-task limit. Conditions are never silently dropped.
- Contracts persist in existing task acceptance_criteria JSON and the plan summary. No database reset or migration was needed.
- Runtime expected outputs and operation requirements now read contracts directly. Input artifacts are no longer incorrectly treated as new outputs.
- Legacy partial repair now edits only flagged task keys on a copy and commits only a structurally valid candidate.
- Prevents overlapping generation, approval, and start in one server process; checks mission version and conditions again before saving.
- Regression suite: 126 passed. Final build deployed. Healthcheck initially ran before front startup completed; subsequent full healthcheck passed.
- Live generation completed: plan version 13, 13 tasks, all 11 criteria assigned, 14 planned artifacts (13 Markdown and one CSV). Persisted structure revalidated after restart, with zero structural issues.
- An old plan-12 run was resumed during generation before the new exclusion guard was deployed. Its late completion referenced an obsolete task ID and set plan 13 failed. With all 13 tasks still pending, restored planning status transactionally; kept failure events and added plan_state_recovered.
- Final state: planning, 0/13 completed. Structural validation is not evidence of business-goal completion; full live artifact execution remains unverified.
- Detailed design and limitations: PLANNING_RESTRUCTURE.md.

## 2026-09-07 Approval-Gated Plan Execution

- Added a persistent external-action queue with `pending_approval`, `approved`, `executed`, `failed`, and `cancelled` states.
- Added UI and APIs to propose, approve, execute, and record evidence for email, proposal, meeting, PoC, contract, and manual actions.
- Approved email actions can be sent through configured SMTP; empty SMTP settings keep outbound sending disabled.
- Goals requiring real sales or customer activity no longer pass from document artifacts alone. At least one approved, executed action with evidence is required.
- New structured plans persist `action_requirements` when real-world execution is part of the criteria.
- Removed obsolete supplement tasks created from legacy final-assessment JSON parse errors and prevented them from being reactivated.
- Revalidated the live mission: SC00-SC11 retained as completed, obsolete supplement skipped, final verification pending external execution evidence.
- Python and JavaScript syntax checks passed. Full regression suite: `129 passed, 1 skipped`; final focused suite: `42 passed`.
- Docker web image rebuilt. `scripts/healthcheck.ps1`: PASS. Start-without-execution-evidence rejection: PASS.

## 2026-09-07 Self-Contained Premarketing Lead Loop

- Added project-scoped persistent campaigns and consented leads.
- Added automatic campaign brief, landing-page copy, and four-week content-calendar generation in the project Workspace.
- Added tokenized lead-capture pages with consent and honeypot fields.
- Added deterministic 0-100 qualification scoring without an external AI dependency.
- Qualified leads (60+) automatically create approval-pending email actions; nothing is sent before human approval.
- Added dashboard controls for campaign creation, capture URLs, lead counts, scores, and statuses.
- Added `app/premarketing.py` to keep qualification and action queuing independently testable.
- Related suite: `25 passed`; full regression suite: `133 passed, 1 skipped`; Python/JavaScript syntax checks passed.
- Live campaign created for the consulting-sales project. Capture page returned HTTP 200, campaign count 1, real lead count 0.
- Post-deployment `scripts/healthcheck.ps1`: PASS.
# 2026-09-07 Googleプレマーケティング外部公開機能

## 実装結果

- Google Forms APIによるフォーム作成、質問設定、公開、回答差分取得を追加した。
- 公開状態を `local_only`、`awaiting_approval`、`approved`、`publishing_form`、`awaiting_site`、`monitoring`、`reauth_required`、`failed` としてSQLiteへ永続化した。
- 人間の公開承認が記録されるまでGoogleリソースを作成しない。
- Google回答IDを一意キーとして重複取込と重複フォローアクションを防止した。
- 同意・メールアドレスを検証した回答だけを既存リード評価へ渡し、60点以上を連絡承認待ちへ接続した。
- APIのネットワーク障害、429、5xxへ最大3回の指数バックオフを追加し、401/403は `reauth_required` で停止する。
- Google Sites用に独立した `google-publisher-browser` コンテナ、専用Dockerボリューム、localhost限定ポート8010を追加した。
- 再利用可能な `.codex/skills/google-premarketing-publisher` スキルと認証、Sites操作、再試行方針を追加した。
- Google Sitesのログイン、2FA、CAPTCHA、OAuth再同意と公開前承認は自動化対象外とした。

## 検証

- 全テスト: `136 passed, 1 skipped`
- Google公開・リード同期関連: `6 passed`
- Python構文検査: PASS
- JavaScript構文検査: PASS
- スキル検証: `Skill is valid!`
- Docker Compose構文検査: PASS
- Docker再ビルド・起動: PASS
- `scripts/healthcheck.ps1`: PASS
- Integrated Front、Open WebUI、Computer、Google Publisher Browser、全4コンテナ、localhost限定ポート、Ollamaモデル: PASS

## 現在状態

- 既存キャンペーン1件と実リード0件を保持した。
- Google認証情報は未設定で、外部フォームおよびGoogle Sitesは作成していない。
- 専用ブラウザは稼働し、独立プロファイルの初期セットアップ待ち（HTTP 401）として正常判定している。

## 2026-09-07 Googleフォーム実公開検証

- ユーザーの明示承認後、Local Supporter自身のAPIから公開申請、承認記録、Googleフォーム作成・公開を順に実行した。
- 対象キャンペーン: `機密業務向けローカルAI導入相談`
- 公開処理は1回目で成功し、フォームID、回答URL、質問IDマッピングをSQLiteへ永続化した。
- 状態は `local_only` から `awaiting_approval`、`approved`、`publishing_form`、`awaiting_site` へ遷移した。
- 公開URL: https://docs.google.com/forms/d/e/1FAIpQLSfn2BXLlEiJ92kOH9J4XZxpAaoM6q3KDWgrj697oUMuxJGvGw/viewform
- 匿名アクセス検証: HTTPS、HTTP 200、想定タイトル、回答送信フォームあり、受付停止表示なし。
- 回答同期API: PASS（取得0、取込0、重複0、拒否0）。テスト回答は送信していない。
- Google Sites作成およびリードへの連絡は未実行。
- 公開後の `scripts/healthcheck.ps1`: PASS。

## 2026-09-07 獲得スキル登録

- `google-premarketing-publisher` を「Googleフォームを作成・公開し、回答を同期できる実証済みスキル」として登録した。
- 判定根拠は、Local Supporter自身のAPIによる申請・承認・作成・公開の成功、公開URLの匿名検証、回答同期APIの正常終了である。
- 認証情報を記録対象から除外し、人間承認、再認証停止、重複防止、外部連絡の個別承認を安全境界として維持する。
- 他AI向けの恒久的な能力記録を `SYSTEM_DESCRIPTION_FOR_AI.md` の「実証済み獲得スキル」へ追加した。

## 2026-09-07 Googleフォーム連携ランディングページ機能

- キャンペーンからレスポンシブHTMLプレビュー、Google Sites転記原稿、公開検証manifestを生成する `app/landing_page.py` を追加した。
- CTAはキャンペーンに保存済みのHTTPS Googleフォームだけを使用し、別フォームや任意外部URLへ差し替えられない。
- LP専用の `draft_ready`、`awaiting_approval`、`approved`、`published`、`failed` 状態と承認日時、エラー、成果物パスをSQLiteへ追加した。
- LP資材生成、公開申請、公開承認、公開結果検証・登録APIを追加した。
- Google Sites URLをHTTPSかつ `sites.google.com` に限定し、HTTP 200、タイトル、CTA、フォームIDを検証した場合だけ監視状態へ進める。
- UIへ「LP資材生成」「LP公開申請」「LP公開承認」「LPプレビュー」「専用ブラウザ」「公開結果を検証・登録」を追加した。
- Python構文検査: PASS。JavaScript構文検査: PASS。
- LP・DB関連テスト: `16 passed`。全回帰テスト: `143 passed, 1 skipped`。
- Docker Web再ビルド・起動: PASS。`scripts/healthcheck.ps1`: PASS。
- 実キャンペーンでLP資材3点を生成し、プレビューHTTP 200、タイトル、CTA、GoogleフォームURL、新規タブ遷移、フォームHTTP 200・回答受付を確認した。
- 現在状態は `draft_ready`。Google Sitesへの外部公開は未承認・未実行。

## 2026-09-07 承認制SNSリード獲得機能

- X、LinkedIn、Facebook、LINE向けに、チャネル別の承認済み文案、公開LPへのUTM付きURL、共有画面URLを生成する機能を追加した。
- InstagramとThreadsはブラウザ共有仕様の不安定さを避け、本文と計測URLのコピー方式とした。
- 状態を `draft_ready`、`awaiting_approval`、`approved`、`composer_opened`、`evidence_registered` としてSQLiteへ永続化した。
- 投稿画面を開く操作は承認後に限定し、SNS API、SNSパスワード、自動投稿を使用しない。
- 投稿画面を開いた回数と日時は記録するが、公開投稿URLが手動登録されるまで投稿成功とは扱わない。
- 公開投稿URLは選択SNSのHTTPSドメインに限定した。
- Google Sitesの公開URLが検証・登録されるまでSNS投稿キットを生成できないため、ローカルプレビューURLの誤発信を防止する。
- Python・JavaScript構文検査: PASS。SNS・DB・LP関連テスト: `21 passed`。全回帰テスト: `148 passed, 1 skipped`。
- Docker Web再ビルド・起動: PASS。`scripts/healthcheck.ps1`: PASS。
- 実キャンペーンはGoogle Sites未公開の `draft_ready` のため、SNS投稿キット生成APIがHTTP 409で安全停止することを確認した。公開LP登録後に管理画面から生成・承認できる。

## 2026-09-07 プレマーケティング実行モニターUI

- キャンペーン、Googleフォーム、公開LP、SNS文案、SNS発信、リード獲得の6段階を一画面で監視できる進行パネルを追加した。
- 進捗率、各段階の完了・進行中・待機・停止・要確認、SNS媒体数、投稿操作回数、公開投稿数、リード数、有望リード数、最終活動時刻、次の操作を表示する。
- 15秒間隔の自動更新と「今すぐ更新」を追加し、非表示タブでは更新を停止する。多重取得とプロジェクト切替後の古い応答反映を防止した。
- 投稿画面を開いただけでは公開成功とせず、公開投稿URLの証拠登録とリード獲得を別段階として表示する。
- 実キャンペーンで進捗33%、キャンペーン・Googleフォーム完了、LP公開待ち、SNSは公開LP登録まで停止、リード0件、次の操作「Google SitesのLPを公開し、公開URLを検証・登録する」を確認した。
- 関連テスト: `17 passed`。全回帰テスト: `151 passed, 1 skipped`。Python・JavaScript構文検査、Docker再ビルド、`scripts/healthcheck.ps1`: PASS。
- UI自動操作による画像確認は、UNC作業ディレクトリに対するWindows UIサンドボックス起動エラーのため未実施。同一画面APIの実レスポンスと静的構文は確認済み。

## 2026-09-07 外部実行証拠ゲートの循環停止修正

- 実営業を含む計画に対し、開始前から外部実行証拠を要求していた循環ゲートを撤去した。
- 承認済み計画は外部実行証拠がなくてもローカル準備タスクを開始できる。
- `action_requirements` を持つ最終検証タスクへ到達した時点で証拠がなければ、タスクを未実行のまま `paused` とし、人間承認・外部実行・証拠登録を待つ。
- 非構造化計画も、ローカルタスク完了後かつ最終目標評価前に同じ証拠待ち停止を行う。
- 承認後に実行済みとなった外部アクションの証拠に加え、SNSの `evidence_registered` 公開投稿URLも正式な外部実行証拠として扱う。
- 外部送信、Google公開、SNS投稿、連絡をシステムが自動実行する変更は行っていない。
- 証拠待ち停止イベントには、承認待ち・承認済み・実行済み件数、SNS操作・投稿証拠件数、必要な次の操作を記録する。
- 関連テスト: `50 passed`。全回帰テスト: `154 passed, 1 skipped`。Python構文検査、Docker再ビルド: PASS。
- 再ビルド後の `scripts/healthcheck.ps1`: PASS。確認時点の最新計画はVersion 17、`ready`、13タスクすべて未開始、失敗0件として保持されている。

## 2026-09-08 Google Sites専用ブラウザ修正

- 白画面の原因を、専用Computerコンテナ内に管理対象ブラウザ実行ファイルと画面サーバーが存在せず、ブラウザタブがプロキシ表示へフォールバックしていたことと特定した。
- `ghcr.io/open-webui/computer` を基礎に Chromium、Xvfb、xauth、日本語・絵文字フォントを追加する専用イメージを実装した。
- 起動時にブラウザタブの既定モードを `chrome` に設定し、Xvfb画面 `:99` 上でComputerを起動する構成に変更した。
- 共有メモリを1GBへ拡張し、Chromium、Xvfbソケット、公開設定APIを確認するコンテナヘルスチェックへ強化した。
- Local Supporter側のブラウザ判定を認証必須APIから公開設定APIへ変更し、初期設定済みの専用ブラウザを誤って `setup_required` と判定しないよう修正した。
- 既存の `google_publisher_browser_data` ボリュームは保持し、保存済みのComputer設定やGoogle用プロファイルを削除・初期化していない。
- Googleへのログイン、2FA、CAPTCHA、Google Sitesの最終公開は引き続き人間が専用ブラウザ上で実施する。
- Docker Compose構文検査: PASS。専用ブラウザとWebコンテナ: healthy。
- 公開設定API: HTTP 200、`needs_setup=false`。Chromium検出: `/usr/bin/chromium`。Xvfbソケット: PASS。
- 保存済みGoogleプロファイルを使用しない一時プロファイルで、Chromiumから `https://sites.google.com/new` のGoogleログインHTMLを取得: PASS。
- 全回帰テスト: `155 passed, 1 skipped`。`scripts/healthcheck.ps1`: PASS。

## 2026-09-08 Google Sites承認済み下書き配置の自動化

- 専用ChromiumへGoogle Sites限定のManifest V3拡張を追加した。
- Google Sites編集画面に `Local Supporter：下書きを配置` ボタンを表示し、明示クリック時だけ処理する。
- 内部APIは、フォーム公開・Google公開承認・Sites公開承認が揃い、未公開であるキャンペーンがちょうど1件の場合だけ下書きを返す。0件は404、複数件は409で停止する。
- 下書きは承認済みタイトル、対象、オファー、CTA、プライバシー文、免責文、既存GoogleフォームURLから生成したHTMLをGoogle Sitesへ埋め込む。
- Googleログイン、2FA、CAPTCHA、OAuth同意を自動操作しない。Google Sitesの「公開」ボタンも拡張から操作せず、配置完了後に人間のプレビューと手動公開を要求する。
- Google Sitesを新規Browserの初期URLとし、空の `about:blank` を表示しない。
- 専用ブラウザから内部下書きAPIへHTTP 200、対象キャンペーン1件、`manual_publish_required=true`、既存GoogleフォームURLを確認した。
- Chrome拡張ファイル・読込設定: PASS。Python・JavaScript構文検査: PASS。関連テスト: `11 passed`。
- 全回帰テスト: `156 passed, 1 skipped`。`scripts/healthcheck.ps1`: PASS。更新スキル検証: PASS。

### 409 Conflict追加修正

- 実際に画面からBrowserタブを作成すると `POST /api/browser/sessions` がHTTP 409となる事象を確認した。
- 原因は、Docker内のDebian Chromiumがユーザー名前空間sandboxを利用できず、Computerの管理対象Chrome起動直後に終了していたことだった。
- 専用・非特権・localhost限定コンテナの隔離を維持したまま、`google-chrome` ラッパーからChromiumへコンテナ用 `--no-sandbox` オプションを付与した。
- Computer自身の管理対象Chrome起動処理で `MANAGED_CHROME_OK`、実行ファイル `/usr/local/bin/google-chrome`、source `managed`、User-Agent取得を確認した。
- 修正後の全回帰テスト: `155 passed, 1 skipped`。`scripts/healthcheck.ps1`: PASS。専用ブラウザ: healthy。

### 白画面・プロファイル競合追加修正

- Chrome映像ストリーム接続後にChromium 152が `maxTouchPoints=0` を拒否し、viewport処理が例外終了して白画面となるComputer 0.9.21の互換性問題を特定した。
- 専用イメージのComputer viewerへ、タッチ無効時もChromiumが受理する最小値1を渡す限定互換パッチをビルド時に適用した。
- コンテナ再作成後、前のホスト名とPIDを含む `SingletonLock`、`SingletonSocket`、`SingletonCookie` が残り、Chromeがプロファイル使用中ダイアログで停止する問題も確認した。
- 起動時に上記3種類の一時プロセスロックだけを削除する処理を追加した。Cookie、Googleログイン情報、Chromeプロファイル本体は保持する。
- 再起動後に残存Singletonロック0件、専用ブラウザhealthy、関連テスト4件PASSを確認した。
- 全回帰テスト: `155 passed, 1 skipped`。`scripts/healthcheck.ps1`: PASS。
## 2026-09-08 Google Sites home-to-editor recovery

- Confirmed the reported `サイト名欄が見つかりません` message occurred because the dedicated browser was on the Google Sites home screen (`/new`), where the editor fields do not exist.
- Updated the Local Supporter Sites extension to open a blank site through `sites.new`, retain a short-lived pending flag, and resume draft placement automatically after the editor loads.
- Aligned field detection with the live Japanese Google Sites editor: document name, `サイト名`, page-title textbox, and `埋め込む` menu item are detected separately.
- The extension still never selects or clicks `公開`; final preview and publication remain manual.
- Validation: JavaScript syntax check PASS; rebuilt `google-publisher-browser`; deployed extension markers verified; `scripts/healthcheck.ps1` PASS for all checks. The host and runtime image do not currently include the optional `pytest` package, so pytest was not re-run and no package installation was performed.

### 途中状態からの再開修正

- 実画面で、タイトル配置とHTMLプレビュー生成まで完了した後、拡張機能がGoogle Sites本文側の「挿入」タブを選び、ダイアログ内の「挿入」ボタンを確定できず停止していたことを確認した。
- ページタイトルが既に置換済みでも `role=textbox` と `aria-label=テキスト` から再検出できるようにした。
- 開いている埋め込みダイアログ、選択済みコードタブ、生成済みプレビューを認識し、ダイアログ内に限定して「次へ」「挿入」を選ぶ状態判定型の再開処理へ変更した。
- 現在の下書きは残存ダイアログから本文挿入を確定し、ダイアログ終了、埋め込み1件、Google Drive保存完了を確認した。公開操作は行っていない。
- JavaScript構文検査: PASS。現在のブラウザセッションを維持したまま、次回起動用の専用ブラウザ・イメージを再構築した。
## 2026-09-08 プロジェクト画面の4タブ化

- 共通のプロジェクト選択を維持し、「プロジェクト・要求」「計画内容」「計画フロー」「実行モニタリング」へ既存UIを分離。
- 計画内容には工程の説明・担当・完了条件・先行工程を表示。フローは task_key / depends_on から生成し、実行モニタリングでは同じ構造に状態名と色を表示する。
- 工程クリックで結果・エラーを表示。実行・停止・再試行・成果物保存・外部実行・プレマーケティング操作はモニタリング側へ移動。会話と外部AI調査は要求タブの折りたたみ領域へ配置。
- キーボードによるタブ切替、選択タブの保持、空計画表示に対応。HTMLを複製せず要素を移動して既存イベントを保持。
- 検証: node --check PASS。一時Chromeで4タブ/4パネル、各タブ単独表示、既存9工程/11依存辺、2フロー、工程詳細、ID重複なし、要求/実行ボタン配置を確認。既存計画・実行状態への変更なし。
- scripts/healthcheck.ps1 全項目PASS。稼働中WebにJS/CSSを反映し、Webイメージもビルド済み。コンテナ再起動なし。

## 2026-09-08 人向け成果物のデザイン品質向上

- キャンペーン基本データから、Canva等へ渡せる制作仕様、ブランド定義、成果物マニフェスト、品質レポートを生成する creative_quality 機能を追加した。
- ブランド名、配色、書体、トーン、訴求内容、CTA、対象者、Googleフォーム導線を一つの制作パッケージへ統合。色コード、Canva URL、公開画像URLを検証し、秘密情報や根拠のない主張を制作物へ含めない制約を明記した。
- campaign_creatives を永続化し、not_started → brief_ready → reviewed → approved の品質工程を追加した。レビュー得点70点未満は承認できない。
- プロジェクト画面へ「デザイン品質向上」「制作仕様を再生成・コピー」「Canvaを開く」「デザイン結果を登録」「品質承認」を追加。Canvaでの制作は明示操作で開始し、デザインURL・任意の公開画像URL・レビュー所見・得点を記録する。
- 承認済みのブランド配色と画像だけをレスポンシブLPへ反映するようランディングページを強化した。品質未承認時はLP資材生成、Google Sites公開承認、Sites自動配置候補への登録を拒否する。
- デザイン変更またはLP資材再生成時は古いSites公開承認を解除し、品質レビュー後の資材だけが公開工程へ進むよう整合性を強化した。
- プレマーケティング・モニタを7段階化し、デザイン工程の待機・レビュー中・承認済み・要確認を表示する。段数に応じて自動折返しするレスポンシブ表示へ変更した。
- 再利用可能スキル .codex/skills/marketing-creative-enhancer を追加し、Googleプレマーケティング公開スキルにも品質承認ゲートを組み込んだ。
- 既存キャンペーンで制作パッケージ4成果物を生成し、状態 brief_ready、得点0、旧Sites承認時刻なし、モニタ7段階、次操作「デザイン品質向上を実施し、レビュー結果を承認する」を確認した。
- 受入確認: Python py_compile PASS、JavaScript node --check PASS、品質未承認LP生成 HTTP 409、Sites自動化候補 HTTP 404、Docker再ビルド・Web healthy、scripts/healthcheck.ps1 全項目PASS。
- ホストに任意の pytest とスキル検証用 PyYAML がないため、追加インストールは行わず、追加単体テストの実行と自動スキル検証は保留した。品質モジュールの直接スモークテスト、API受入確認、画面DOM確認はPASS。
- Canva上のデザイン作成・公開およびGoogle Sitesの最終公開は外部状態を変えるため自動実行せず、人間の明示操作とレビュー承認を維持する。

## 2026-09-08 デザイン作成の自己完結型自動化

- キャンペーンの公開用基本データとブランド設定から、ローカルでレスポンシブなヒーロービジュアル、説明カード、CTA、モバイル構成を自動生成するAPIを追加した。
- プロジェクト画面へ「AIデザイン自動作成」「自動デザイン再生成」「自動デザイン確認」を追加。生成と同時に自動品質レビューを実行し、専用プレビューを開く。
- 自動評価は公開コピー充足、本文・CTAコントラスト、HTMLテキスト保持、レスポンシブ構成、未確認実績の非追加を検査する。自動採点を人間承認として扱わず、品質承認は従来どおり明示操作を必要とする。
- ローカル生成結果はCanva障害時にも保持される。Canvaは任意の高度化経路とし、外部デザインのURLとレビューを同じ品質工程へ登録できる。
- 実案件「機密業務向けローカルAI導入相談」で自動生成を実行し、状態 reviewed、provider local、品質スコア90、成果物3件、プレビューHTTP 200、CTA一致を確認した。
- 品質承認前のGoogle Sites資材生成はHTTP 409で拒否されることを確認。外部公開、SNS投稿、フォーム変更は実行していない。
- Canva連携は利用可能だが、ブランドキット一覧だけ brandkit:read スコープ不足。ローカル自動生成はこれに依存せず正常完了した。
- Python・JavaScript構文検査PASS、コンテナ内自動品質スモークテストPASS、Web再ビルド・healthy、scripts/healthcheck.ps1 全項目PASS。

## 2026-09-08 Codex非依存の外部AI・Canva部品制作

- 実行時の制作責任をCodexからLocal Supporterへ移し、API応答に runtime=local_supporter、codex_required=false、各制作結果にcodex_used=falseを記録する構成へ変更した。
- 計画で明示許可され、API設定済みの外部AIを最大5社の候補として扱い、訴求文編集、ビジュアル構成、独立品質批評の部品タスクを直接APIで実行する creative_orchestrator を追加した。各部品は第一候補が失敗すると別AIへ1回切り替える。
- 外部へ渡す情報をキャンペーンのtitle、audience、offer、call_to_actionと公開ブランド設定だけに固定した。認証情報、リード、顧客資料、プロジェクト内部情報は送信対象に含めない。
- AI応答は component_production.json に証拠として保存するだけで、コードやシステム命令として実行しない。
- Canva Connect APIクライアントを追加し、ブランドテンプレートのデータ項目確認、TITLE/AUDIENCE/OFFER/CTAの対応付け、Autofill非同期ジョブ、完了確認、デザインURL取得を実装した。
- CanvaはCANVA_ACCESS_TOKENとCANVA_BRAND_TEMPLATE_IDが揃った場合だけ直接実行する。408、429、5xxは指数待機で上限付き再試行し、権限エラー、テンプレート不一致、試行上限到達時は理由を保持してローカル組版へ移る。
- UIへ「各AI＋Canvaで自動制作」を追加し、外部送信前に公開項目と送信先を明示確認する。接続欄には「Local Supporter（Codex不要）」とCanva設定状態を表示する。
- 外部プロバイダー省略時は計画の許可済みAIを利用し、空配列指定時は外部AIを一切呼ばない仕様として、運用と受入テストを分離した。不許可プロバイダーはHTTP 403で拒否する。
- Canva未設定の実環境で外部AI空配列の受入テストを実行し、Codex未使用、外部AI呼出し0件、ローカルフォールバック、reviewed、90点、プレビューHTTP 200を確認した。外部公開は実行していない。
- テストを追加: 公開データ封筒から秘密・リード情報が除外されること、Canvaのトークン・テンプレート両方を要求すること、モックAutofillで一致する公開項目だけを送ること。
- Python・JavaScript構文検査、Docker Compose構成検査、公開データ／Canva設定ガード、UI配信確認、Web再ビルド: PASS。scripts/healthcheck.ps1: 全項目PASS。
- 最終フェイルオーバー反映直後の初回healthcheckはWeb起動競合でIntegrated Frontだけ一時失敗したが、5秒後の再実行で全項目PASSを確認した。
- ホストにはpytestとhttpxがないため、追加インストールは行わず、コンテナ内スモークテストと実API受入テストで検証した。追加pytestファイルは依存環境が整った時点で全回帰実行できる。

## 2026-09-08 追加指示チャット

- 「1. プロジェクト・要求」タブへ、明示的な追加指示入力欄と送信ボタンを追加した。
- 利用者の指示とシステムの受付結果をプロジェクト単位で永続保存し、左右のチャット吹き出しと日時で再表示する。
- 追加指示は既存タスクと完了実績を削除せず、制約・処理条件へ追記する。計画構成を変更する場合だけ、利用者が「AIで計画生成」を選べる案内を返す。
- 追加指示は4000文字、蓄積後の処理条件は20000文字を上限とし、空欄・未設定目標・上限超過をAPIで拒否する。
- 回帰テスト44件、Python構文検査、JavaScript構文検査、Web再ビルド、API/UI配信確認: PASS。
- scripts/healthcheck.ps1 はComposeプロジェクト名を明示して全項目PASS。現在の販売実行計画が一時停止12/13のまま保持されていることを確認した。

## 2026-09-09 LP公開申請ボタン復旧

- 承認済みクリエイティブとLP資材が存在し、site_publication_status=draft_readyである一方、画面クリックが公開申請APIへ到達していないことをログで確認した。
- LP公開申請ボタンへ明示的なbutton種別、クリックイベント抑止、二重押下防止、処理中表示、成功メッセージ、APIエラー表示を追加した。
- JavaScript構文検査、Web再ビルド、修正版配信確認、scripts/healthcheck.ps1全項目: PASS。公開申請・承認・外部公開は検証中に代行していない。

## 2026-09-09 Google Sites最新版デザイン再配置

- 公開承認済み・未公開のキャンペーンについて、品質90点の最新LP資材とGoogle Sites自動配置候補が正常に存在することを確認した。
- 古い下書きの再利用を防ぐため、自動配置候補APIへCache-Control no-storeと資材ハッシュ版番号を追加した。
- 公開前に最新版を再配置する必要があることを画面上で警告し、操作名を「最新デザインをGoogle Sitesへ配置」へ変更した。
- Google Sites公開操作は行っていない。Python/JavaScript構文検査、Google公開テスト3件、Web再ビルド、下書きAPI、全体ヘルスチェック: PASS。
- 公開申請HTTP 200後も承認ボタンを見つけにくい事象に対し、awaiting_approval時はキャンペーンカード最上部へ黄色の「LP公開を承認する」欄を表示するよう改善した。承認中・成功・APIエラーも画面表示し、再ビルドと全項目ヘルスチェックPASS、状態awaiting_approval維持を確認した。

## 2026-09-09 計画生成エラー対策と追加指示の再計画

- ローカルAIがscopeへURL、成果物パス、処理ライブラリ名を混入し、バックエンド検証で3回失敗していたことを特定した。
- AI候補のscopeだけを保存前に正規化し、URL、バックエンド所有パス、PDF・DOCX・XLSX名、shell・pandas表現を内容記述へ置換するようにした。厳格な契約検証自体は維持した。
- 追加指示チャットの利用者メッセージを達成条件へ追加し、計画生成と承認前検証で同じ条件集合を使うようにした。
- 再計画時は、task_keyと達成条件が一致するcompleted・skippedタスクのID、結果、試行履歴、担当、時刻を保持する。新規条件と最終検証だけをpendingにする。
- スモークテストで禁止表現の正規化、SC01完了状態の保持、追加SC02と最終検証のpending化を確認した。ホストにpytestがないためパッケージ追加は行っていない。
- Webサービスを再ビルドし、scripts/healthcheck.ps1は全項目PASS。
- 実プロジェクトをVersion 18からVersion 19へ再計画し、既存12件の完了状態を保持したまま、Google Sites公開をSC12、運営会社・機能情報の公開資料反映をSC13として追加した。合計15件、進捗12/15、計画生成結果successを確認した。
- 公開、計画承認、実行開始、SNS投稿、外部送信は行っていない。

## 2026-09-09 Google Sites公開停止の原因対策

- SC12が公開手順書のMarkdown作成だけで完了となり、Google Sites公開URLが空のまま最終検証へ進んでいたことを確認した。外部実行キューも0件だったため、画面上は公開で停止して見える矛盾状態だった。
- Google Sites・ランディングページ公開条件へgoogle_site_publication証拠契約を自動付与し、site_publication_status=publishedかつ公開URL登録済みになるまでタスクを実行・完了させないようにした。
- 証拠を種類別に照合し、別の外部操作証拠でGoogle Sites公開条件を誤通過しないようにした。完了再検証でも公開証拠不足を検出する。
- 公開工程を計画の最後へ安定配置し、全先行成果物へ依存させた。新しい証拠条件または上流変更がある再計画では、旧完了状態と下流完了状態を未完了へ戻す。
- AIが未来・不明なtask_keyをdepends_onへ出した場合は、バックエンドが既知の先行工程だけへ正規化するよう補強した。
- LPマニフェスト、HTML、Google Sitesコピー、配置ペイロードへ弘和運輸有限会社、公式URL、所在地を必須表示するよう追加した。公開検証も運営会社名・公式URLを必須とした。
- Googleフォーム管理IDと回答用URLのIDが異なる実データへ対応し、公開導線は管理IDではなく実際のgoogle_form_urlで検証するよう修正した。
- 最新LP資材を再生成し、運営会社名、公式URL、Googleフォーム回答URL、タイトル、CTAの検証に合格した。旧公開承認を解除し、site_publication_status=draft_readyへ安全に戻した。
- 計画をVersion 20へ再生成し、既存12件完了を保持、SC12運営会社資料をpending、SC13 Google Sites公開を証拠待ちpending、最終検証をpendingとした。
- Python構文検査、依存関係・状態解除・LP公開検証スモーク、コンテナ内スモーク、Docker再ビルド、scripts/healthcheck.ps1全項目: PASS。
- Google Sitesの公開クリック、計画承認、SNS投稿、外部送信は行っていない。

## 2026-09-09 専用ブラウザ操作不能の復旧

- 8010の画面が再起動前のBrowserセッションIDへ接続し続け、映像・マウス入力ストリームが403 Forbiddenになっていたことを特定した。Google Sitesの公開ボタンや拡張パネルの重なりが原因ではなかった。
- 認証トークンの直接生成による復旧案は安全基準で却下し、採用していない。
- 有効な既存ログインCookieがある場合に限り、失われたセッションを同じIDで一度だけ復元し、保存済みChromiumプロフィールでビューアを再起動する互換パッチを追加した。
- Google公開用サービスだけを再ビルド・再作成し、監査ログで `Recovered stale authenticated Browser tab`、encoder接続成功、viewer stream接続成功を確認した。
- Googleログイン情報、Google Sites下書き、Dockerボリュームは保持した。専用ブラウザはhealthy、`scripts/healthcheck.ps1`は全項目PASS。
- Google Sitesの「公開」確定操作は行っていない。
## 2026-09-09 SNS投稿キットの操作中断対策

- 6媒体のSNS文案はapproved、投稿操作開始は全て0回。公開LPは登録済み。
- loadMissionの毎秒更新がプレマーケティング全体を再描画し、detailsを閉じるコードを確認。通常の取得を既存15秒タイマーへ限定し、同一データはDOMを置換せず、変更時も同一プロジェクトの開閉状態を保持するよう修正。操作ボタンのtypeをbuttonへ統一。
- Node構文検査、tests/test_premarketing_refresh.cjs（DOM保持・開閉保持・プロジェクト分離・毎秒取得抑止）はPASS。
- 稼働コンテナへ静的ファイルを反映し、HTTP配信内容で修正を確認。次回用Webイメージもビルド済み。healthcheck.ps1はプロセス限定ExecutionPolicy Bypassで全項目PASS。
- 実ブラウザでのクリック完了は未確認。既存タブでは再読込が必要。SNS投稿や承認変更は行っていない。
- 継続調査で、既存ブラウザが旧版JavaScriptをキャッシュし、修正後も毎秒API取得を続けていたことをアクセスログで確認。index.htmlへ版付きURLを設定し、JS/CSS応答へno-store/no-cacheを追加した。
- webコンテナのみ再ビルド・再作成。版付き画面・修正済みJS・Cache-ControlをHTTP 200で確認し、全ヘルスチェックと回帰テストはPASS。
## 2026-09-09 公開済みLPの指定工程再開・再構成機能

- 公開中Google Sites、既存Googleフォーム、リード、SNS設定を保持したままLP改訂を開始する `landing/recompose` APIを追加した。
- 改訂番号、再構成指示、制作方式、改訂状態を永続化。開始時に公開URL・編集URL・キャンペーン基本情報・クリエイティブ状態を改訂別JSONへ保存する。
- UIへ「LPをAI＋Canvaで再構成」「LPをローカルで再構成」を追加。AI＋Canva方式は公開可能情報の外部送信確認を必須化し、失敗時は既存のローカル組版へフォールバックする。
- 工程を `再構成案生成 → プレビュー → デザイン品質承認 → LP更新資材 → 更新申請 → 更新承認 → 既存Google Sitesへ配置 → 手動公開 → 公開検証` として接続した。
- 専用ブラウザの下書き取得APIは承認済み改訂を既存サイト更新として返し、編集URL・改訂番号・更新フラグを含める。Google Sitesの公開操作は引き続き人間が行う。
- 公開結果登録時の編集URL・公開URLは既存値を自動入力する。公開中 `https://sites.google.com/view/kouwaai` は変更していない。
- Python/JavaScript構文、UI回帰テスト、操作安定化テスト、一時DBによる公開サイト保持・改訂状態遷移・更新下書き取得スモークはPASS。ホストにpytestがないためpytest一式は未実行。healthcheck.ps1は全項目PASS。
## 2026-09-09 Canva連携未動作の診断と誤実行防止

- 稼働コンテナと状態APIを秘密値を表示せず確認し、`CANVA_ACCESS_TOKEN` と `CANVA_BRAND_TEMPLATE_ID` が両方未設定であることを特定した。
- 「各AI＋Canva」処理はHTTP 200だったが、実際にはCanvaを呼ばずローカル組版へフォールバックしていた。改訂1はreview_ready、provider=local、品質90点で、公開LPは保持されている。
- Canva状態APIへ不足項目と説明を追加。Canva未設定時のLP AI＋Canva再構成は処理開始前にHTTP 409で停止し、改訂状態を変更しないようにした。
- UIはCanva未設定を明示し、AI＋Canva再構成ボタンを無効化。外部AI制作は「各AI＋ローカル」と表示し、`use_canva=false`で実行するよう変更した。
- Python/JavaScript構文、UI回帰テスト、未設定事前検査、改訂・公開URL保持、healthcheck.ps1全項目はPASS。Canva実API試験は認証情報とブランドテンプレートID未設定のため未実施。

## 2026-09-09 ローカルLLM高品質化

- RTX 4070 Ti SUPER 16GB向け推奨構成として `mistral-small3.2:24b-instruct-2506-q4_K_M`（24B、Q4_K_M、15GB）をOllamaへ追加した。既存モデルは削除していない。
- 地域事業者向けLP計画を厳密JSONで生成する比較試験を実施。新24Bは必須キー、3工程、Google Sites、Google Forms、人間の公開承認条件を満たした。現行 `gpt-oss:20b` は同条件で不完全な応答となった。
- 新24Bの実測は初回ロード31.99秒、生成14.24 token/s、8Kコンテキスト時85% GPU・15% CPU。品質優先用途で利用可能と判定した。
- `.env` の既定モデルを新24Bへ切り替え、Webサービスだけを再作成した。`/api/health` は既定モデルとローカルフォールバックモデルの両方で新24Bを認識した。
- `scripts/healthcheck.ps1` に `.env` 指定モデルの存在確認を追加した。新モデル指定スモークテストは `LOCAL_COWORK_OK`、更新後ヘルスチェックは全項目PASS。

## 2026-09-09 LP原案・公開版の一致保証

- 公開中 `https://sites.google.com/view/kouwaai` と承認済み原案を照合した。主要文言、Googleフォーム、運営会社、免責は含まれていたが、Google Sitesの外枠・標準テーマ・埋込み領域による見た目の差があり、従来はタイトル、CTA、フォーム等の部分一致だけで公開登録できる状態だった。
- キャンペーン公開情報、承認済みブランド、画像、CTA、フォーム、会社情報から12桁の正本LP版IDを生成し、HTML、マニフェスト、Google Sites配置データへ同一IDを埋め込むようにした。
- 公開登録時は、版IDが1種類だけ存在すること、対象者、提供内容、CTA、Googleフォーム、個人情報文、免責、運営会社名・住所・URL、必須セクション構成の全項目一致を必須化した。不一致項目は画面に具体的に表示する。
- DBへ原案版と公開版を分離保存し、UIへ「LP版: 原案 / 公開 / 一致状態」を表示した。旧版を残したまま新版を追加すると、専用ブラウザが警告し、公開検証も重複版として拒否する。
- 改訂1の正本資材を再生成し、新原案版は `286b1c8920c6`、状態は `draft_ready`。現公開ページの旧版 `e3430bab0b16` は不一致として拒否されることを実データで確認した。公開URLとGoogleログイン情報は保持し、外部公開操作は実行していない。
- Python/JavaScript構文検査、版生成・完全一致・旧版重複拒否・DB永続化スモーク、配置ガード、UI配信確認、`scripts/healthcheck.ps1` 全項目はPASS。ホストにpytestがないためpytest一式は未実行。

## 2026-09-10 Google公開用Chrome軽量化

- 8010操作不能時の専用コンテナを調査し、Chrome本体ではなく画面転送エンコーダが約52% CPU、コンテナ全体が55～68% CPUを継続消費していたことを確認した。
- Xvfb解像度を1440×900から1024×720へ縮小し、Computerの既定転送品質をlow、最大解像度720p、最大ビットレート2Mbpsへ制限した。設定値がランタイムDBから実際に読み取れることを確認した。
- 通常のコンテナ再起動で `/tmp/.X99-lock` が残り再起動ループになる問題に対し、起動時にX99の一時ロックとソケットだけを除去する復旧処理を追加した。Googleプロフィール用ボリュームは保持した。
- 再作成後の待機時負荷はCPU 0.69%、メモリ111MB。軽量化・再起動復旧スモークと `scripts/healthcheck.ps1` は全項目PASS。
- 専用ChromeはGoogleログイン画面で停止しており、認証は自動化していない。Google Sitesの旧LP本文削除・新版配置・公開操作は未実行。

## 2026-09-10 Google Sites転記処理の検証強化

- 旧版削除を確認ダイアログの自己申告だけで通過できる処理を廃止した。旧埋込みからの版証拠が3.5秒間観測されないことを確認してから新版配置へ進み、旧版を検出した場合だけ停止する。
- 「挿入」をプログラムが実行した後に手動クリックを求めていた矛盾を解消し、自動挿入と結果確認へ統一した。Google Sitesの「公開」は引き続き自動操作しない。
- 配置証拠を版ID・タイトル・フォームだけから、対象者、提供内容、CTA、個人情報文、免責、運営会社名・住所を含む本文8項目と、Googleフォーム・運営会社URLのリンク2項目へ拡張した。
- WebとGoogle公開用ブラウザを再ビルド・再作成し、既存データとブラウザプロフィールを保持した。転記APIは原案版 `286b1c8920c6`、本文8項目、リンク2項目、`manual_publish_required=true` を返すことを確認した。
- JavaScript構文、配置ガード、Python構文、転記ペイロードの受入スモーク、`scripts/healthcheck.ps1` は全項目PASS。Google Sitesへの実転記と公開操作は行っていない。

## 2026-09-10 Google Sites配置ボタン導線の復旧

- 8010の画面配信サービスと下書きAPIは正常だったが、内部Chromiumプロセスが未起動で、8099の「既存Google Sitesを更新」が通常ブラウザへ編集URLを直接開いていたため、専用拡張の配置ボタンが表示されない状態を確認した。
- 8010へ `intent=newBrowser` とURLを渡す専用Chrome深いリンクを追加し、Computerの既存ログインセッション内でGoogle Sites編集URLを直接開けるようにした。
- 8099の導線を「専用Chromeで既存Google Sitesを更新」へ変更し、通常ブラウザへ編集URLを開く旧導線を廃止した。
- UI回帰、JavaScript/Python構文、配置ガード、実配信内容、8010深いリンク実装、`scripts/healthcheck.ps1` は全項目PASS。Google認証、転記ボタンのクリック、Google Sites公開は実行していない。

## 2026-09-10 8010ローカルパスワード再設定

- 8010のComputerローカル認証だけを標準のbcrypt更新処理で再設定した。パスワード値はチャット、ファイル、コマンド引数、ログへ保存していない。
- 現在の認証ハッシュが事前バックアップと異なることを、値を表示せず確認し、ユーザー本人によるログイン成功を確認した。
- Googleログイン済みChromeプロフィール、Google Sites下書き、Local Supporterデータ、ユーザーIDは保持した。
- 再設定完了後、一時再設定ツールと今回作成した旧認証DBバックアップを削除した。
- `tests/test_cowork.py` の構文確認と `scripts/healthcheck.ps1` は全項目PASS。

## 2026-09-10 Google Sitesページタイトル編集失敗の復旧

- 専用Chromeの現行Google Sites DOMを読み取り専用で調査し、補助パネルの失敗状態が「タイトルを配置：編集欄への入力に失敗しました」であることを確認した。
- Google Sitesが「ページのタイトル」を最初は `contenteditable=false` のプレースホルダーとして表示するのに対し、配置拡張が編集可能欄と誤認して直接入力していたことが原因だった。
- プレースホルダーを選択して `contenteditable=true` の実編集欄へ切り替わったことを確認してから入力する処理を追加し、非編集要素への入力を明示的に拒否するガードを追加した。
- 専用Chromeだけを再ビルド・再作成し、GoogleプロフィールとSites下書きは保持した。稼働中コンテナに修正版が反映され、8010がhealthyであることを確認した。
- JavaScript構文、ページタイトル編集可否を含む配置ガード回帰、Python構文、`scripts/healthcheck.ps1` は全項目PASS。Google Sitesへの再配置と公開操作は実行していない。

## 2026-09-10 Google Sites挿入ダイアログ停止の復旧

- 現行Google Sites画面を読み取り専用で確認し、HTMLプレビュー生成と「挿入」ボタンの有効化までは成功していたが、拡張機能の疑似クリックをGoogle Sitesが受け付けず、ダイアログが閉じないままタイムアウトしていたことを確認した。
- 疑似クリックを廃止し、Googleダイアログ右下の「挿入」をユーザーが1回押すまで最大5分待機する方式へ変更した。ダイアログが閉じた後の本文証拠とGoogle Drive保存完了は自動検証する。
- 待機状態を `awaiting_user_insert` として保存し、補助パネルに押す場所と、その後は自動確認されることを明示する。
- 専用Chromeだけを再ビルド・再作成し、Googleプロフィール、Sites下書き、承認済み原稿は保持した。稼働中拡張が手動挿入待機版であることを確認した。
- JavaScript構文、配置ガード回帰、`scripts/healthcheck.ps1` は全項目PASS。Google Sitesの「挿入」および「公開」は実行していない。

## 2026-09-10 Google Sitesタイトル・プレビュー表示対策

- 承認済み下書きAPIとGoogle Sites編集画面を照合し、正しいタイトルがともに「機密業務向けローカルAI導入相談」で一致していることを確認した。
- 現行プレビューを画面取得して確認したところ、大見出しは古い「ページのタイトル」のままで、モバイル幅表示と補助パネルの重なりにより全体確認が困難だった。
- プレビューを開いたまま配置ボタンを実行できていたため、古いプレビューの背後にある編集画面だけが更新される状態だった。プレビュー中の配置を明示的に停止するガードを追加した。
- プレビュー表示中はLocal Supporter補助パネルを自動的に非表示にし、確認案内を「新しくプレビューを開く」「デスクトップ表示を選ぶ」「縦にスクロールする」へ変更した。
- 専用Chromeだけを再ビルド・再作成し、Googleプロフィールと保存済みSites下書きは保持した。JavaScript構文、配置ガード回帰、Python構文、`scripts/healthcheck.ps1` は全項目PASS。
- 調査用に作成した画面画像2点は確認後に削除した。Google Sitesの公開操作は実行していない。

## 2026-09-10 Google Sites挿入後の誤エラー対策

- エラー停止した現行Google Sitesを読み取り専用で調査し、埋め込みダイアログが閉じ、Google Drive保存完了も表示されていることを確認した。挿入自体は完了していた。
- Google Sites編集画面ではカスタム埋め込み内部が隔離され、配置拡張から版マーカーを読み取れないため、マーカー未検出を挿入失敗と誤判定していた。
- ダイアログ閉鎖とDrive保存を独立して確認し、内部マーカーだけが取得できない場合はエラーではなく `inserted_pending_preview` として「プレビュー確認待ち」に遷移するよう変更した。
- 保存済みの `inserting` / `unverified` 状態からも再挿入せずに復旧できる分岐を追加した。重複防止は維持している。
- 専用Chromeだけを再ビルド・再作成し、Googleプロフィール、Sites下書き、配置チェックポイントを保持した。JavaScript構文、配置ガード回帰、`scripts/healthcheck.ps1` は全項目PASS。プレビュー確認と公開操作は実行していない。

## 2026-09-10 Google Sites公開URL・編集URLの整合性修正

- ユーザー指定の公開URL `https://sites.google.com/view/localai2/%E3%83%9B%E3%83%BC%E3%83%A0` を未ログイン状態で取得し、HTTP 200、承認済みタイトル、既存Googleフォーム導線を確認した。
- 公開URLは既に登録されていたが、編集URLが以前のGoogle Sites下書きを指していたため、専用Chromeで現在開いている対応編集URLへ更新した。
- Google Site登録APIで公開ページを再検証し、タイトル、CTA、フォーム、承認済みLP版 `286b1c8920c6` の一致を確認して両URLを保存した。
- 保存後のAPI再読込で公開URL・編集URLの一致、`site_publication_status=published`、`publication_status=monitoring` を確認した。
- `scripts/healthcheck.ps1` は全項目PASS。Google Sitesの再公開操作や外部送信は実行していない。

## 2026-09-10 SNS投稿キット安全再生成機能

- 既存のSNS投稿キットがある場合にも「SNS投稿キット再生成」ボタンを表示するよう8099のプレマーケティングUIを変更した。
- 再生成前に確認ダイアログを表示し、現在の文案、承認状態、投稿画面起動回数を版付きアーカイブへ保存してから、公開中LPのURLで6媒体の新版を生成する。
- 新版は `draft_ready` へ戻り、自動投稿せず「文案確認 → 承認申請 → 承認 → 投稿画面」の手順を再実行する。
- 公開投稿URLが証拠登録済みのキットは上書きを禁止し、新しいキャンペーン版の作成を案内するガードを追加した。
- Python構文、JavaScript構文、UI回帰、SNS生成・再生成・証拠保護テスト7件、実配信確認、`scripts/healthcheck.ps1` は全項目PASS。既存キットの再生成やSNS投稿は実行していない。

## 2026-09-11 NOTE掲載用システム紹介資料

- 公開記事向けの機能概要 `NOTE_SYSTEM_FEATURE_OVERVIEW.md` を新規作成した。
- 業務別の利用シーンを示す `NOTE_USE_CASES.md` を新規作成した。
- 現行実装と既存の公開用概要、README、システム説明、検証履歴を照合し、ローカル統制AI、プロジェクト管理、計画・承認・実行、複数AIレビュー、安全なWorkspace、表・会計データ、音声、プレマーケティング機能を反映した。
- Canva連携は認証設定時のみ利用可能、Google Forms・Google Sites・SNSの外部公開は人の承認と最終操作を要することを明記した。
- 内部IP、localhost URL、個人Workspaceパス、APIキー、パスワード、秘密値、顧客固有情報が両資料に含まれないことを確認した。
- 両ファイルはUTF-8として正常に読み取れ、NUL文字・文字化け置換文字なし。機能概要は190行、利用シーンは308行。
- 文書追加のみで、アプリケーションコード、設定、データ、稼働サービス、外部公開状態は変更していない。

## 2026-09-14 ローカルLLMのWeb切替

- 統合フロントへ「ローカルLLM」パネルを追加し、Ollamaへインストール済みの会話生成モデルを一覧表示して切り替えられるようにした。
- Ollamaのモデル一覧と能力情報を照合し、会話生成対応モデルだけを候補にした。埋め込み専用モデルは表示・選択できない。
- 処理実行中の切替を拒否し、稼働中は旧モデル解放、新モデル読込、設定保存を順番に行う。読込または保存に失敗した場合は旧モデルへ戻す。
- 選択モデルを /data/memory/local_model.json に保存し、Docker再起動後も保持する。.env の OLLAMA_MODEL は初期値・フォールバックとして維持した。
- Ollamaの能力情報からthinking対応を判定し、非対応モデルでは think=false へ自動補正するようにした。これによりMistral使用時の does not support thinking 400エラーを防止した。
- 実機で qwen3.5:9b へ切替後、既定の mistral-small3.2:24b-instruct-2506-q4_K_M へ復帰できることを確認した。Webコンテナ再起動後もMistral選択が保持された。
- Mistralの能力は completion, tools, vision、実効thinkingは False と判定され、実回答「動作確認OK」の生成に成功した。
- 対象回帰テストは9件PASS。正式な tests 全体は177件PASS、3件FAIL、1件SKIPで、FAILは既存のクリエイティブ公開項目、landing recomposeのテスト初期化、計画置換状態保持に関する今回の変更範囲外の既存課題だった。
- Docker再構築後、モデル一覧APIは4モデルを返し、埋め込みモデルを除外、UIスクリプト配信、同一モデル選択、実モデル切替、選択永続化を確認した。scripts/healthcheck.ps1 は全項目PASS。

## 2026-09-14 計画・実行の妥当性検証と監査表示

- 失敗案件を読み取り調査し、登録原本の抽出本文は存在する一方、ローカルLLMが `source_references` を返さず原本証拠なしで失敗していたこと、補正時に別案件の従業員・年休内容や根拠のない「全原本が最新」とする断定が混入したことを特定した。
- 計画案とタスク実行にJSON Schemaによる構造化出力を追加した。空本文・長さ上限時はコンテキストを安全に縮約して再試行する。
- `gpt-oss:20b` は構造化出力で `think=false` を指定すると本文を空で返す実機挙動があったため、thinking対応モデルでは低推論を許可し、内部推論欄を保存・表示せず最終JSON本文だけを利用する互換処理へ補正した。
- 実行プロンプトへ正確な `ref=context:<id>` を付け、バックエンドが実際に抽出本文を提示した原本だけを監査証拠として確定するようにした。モデルの自己申告だけには依存しない。
- UIイベントへ計画判断要約、実行判断要約、バックエンド原本証拠、候補成果物差戻し、ミッション状態整合の記録を追加した。逐語的な内部思考過程は記録せず、判断対象、根拠数、操作数、能力不足、検証・補正理由を監査可能にした。
- 検証不合格時にAIが書き換えたファイルを正式成果物から外し、`.local_cowork_rejected/<時刻>/` へ回収した上で直前版を復元するロールバックを追加した。新規ファイルも回収後に正式パスから除外する。
- 成果物品質検査へ別案件由来の従業員サンプル文と、原本の版・更新日では立証できない最新性断定の拒否を追加した。
- `completed` ミッションに未完了タスクが残る不整合を起動時・完了検証時に検出し、データを保持したまま `paused` へ戻すよう修正した。実データの「車両別評価データの作成」は completed 5件、pending 4件として paused へ整合した。
- 選択中モデルを起動時に非同期ウォームアップし、`/api/health` へ warming / ready / error を表示するようにした。実環境では `gpt-oss:20b` が ready。
- 既存テスト収集が一時バックアップまで走査していたため、pytest対象を `tests/` に限定した。既存の3失敗も修正し、全回帰は `186 passed, 1 skipped`。
- Docker Webサービス再構築、`gpt-oss:20b` の構造化JSON実応答、全4サービス、localhost限定ポート、Workspace、Ollamaモデルを確認し、`scripts/healthcheck.ps1` は全項目PASS。
- 外部AIへの送信、既存失敗案件の自動再試行、既存の失敗成果物の削除は行っていない。新しい差戻し・回収処理は次回の明示的な再試行から適用される。

## 2026-09-15 公開Web調査と公募要領作業の安全化

- 追加指示にWeb検索が明記されたタスクだけを公開Web調査として扱い、HTTPS標準ポート、公開IP、リダイレクト回数、取得容量、本文量を制限する収集機能を追加した。ローカル・プライベート・予約済みIP、認証情報付きURL、非標準ポートを拒否する。
- 補助金・公募要領では中小企業庁、ものづくり補助金公式ポータル、Jグランツ等の公式候補を優先し、取得URL、取得日時、本文SHA256、抽出本文をプロジェクト原本と作業フォルダへ保存する。
- 旧来の日本語公開ページに対応するため、HTTP charset、HTML meta charset、UTF-8、CP932、Shift_JIS、EUC-JPの順で安全に文字コードを判定する。
- 公開Web資料の分析と最終判断はローカルLLMを優先する。外部AIを明示許可していても、公開Web調査時に送る内容を公開タスク名・公開条件に限定し、試算表、社内原本、個人情報、認証情報を送らない。
- 補助率・補助額・期限・効果・市場規模・売上予測等の高リスク主張は、登録原本または取得済みURLの根拠が同じ段落にない限り、仮説・例示・要確認の明記を必須とした。
- 最新試算表、必須見積書等がないタスクは推測で進めず、必要原本を示して停止する入力ゲートを追加した。Web検索を明示した公募要領収集は公式一次資料を取得してから進める。
- 見出し番号だけが異なる成果物を誤差戻ししないよう、見出し検証を意味内容で照合するよう補正した。
- 再利用可能な `.codex/skills/public-web-evidence/SKILL.md` を追加し、公募要領の版・発行主体・対象・経費・補助率・上限・期限・添付・申請経路・改訂確認を定義した。
- スキル形式検証、公開Web対象テスト31件、全回帰テストを実行し、全回帰は `194 passed, 1 skipped`。実際の公式ポータル2ページを取得し、日本語文字列に置換文字がないことと公式ドメイン判定を確認した。
- UIの追加指示欄へ、安全なWeb調査の書き方と公式一次資料を保存する入力例を追加した。「モノづくり補助金」プロジェクトへ同方針の追加指示を保存し、既存計画・成果物を消去せず、再計画時に反映できる状態にした。
- Webサービスだけを再構築し、コンテナ内でも公式ポータル2ページの取得、公式判定、本文抽出、日本語置換文字0件を確認した。全4サービスはhealthy、localhost限定ポート、標準ヘルスチェックは全項目PASS。

## 2026-09-15 Web調査の工程順・入力ゲート補正

- 実プロジェクト第5版が、Web調査タスクにも目標全体の試算表要件を適用し、Web原本0件のまま停止していたことを確認した。
- 「情報検索スキル」「最新・現行・公式情報の取得」「公募要領・公式サイト・公式PDFの確認」を公開Web調査として認識するよう判定語を拡張した。
- 構造化計画では公開Web調査を原本確認直後へ移動し、Webタスクの依存を原本確認だけに限定した。その他の工程は取得済みWeb成果物へ依存させ、資料作成より後に調査が置かれる逆転をバックエンドで補正する。
- 試算表の必須判定を目標全体からタスク単位へ変更した。財務分析、経営数値、売上・利益、ROI、投資効果、補助申請額の算定だけで要求し、公開Web調査と非財務の構成図作成では要求しない。
- Web調査完了時は、参照イベントだけでなく保存原本中のHTTPS URL、取得日時、64桁SHA256、抽出本文を再検証する。公式一次資料が必要な場合は公式候補フラグも必須とした。
- 再計画時の完了状態引継ぎ条件を強化し、同じSC番号・達成条件でもタイトル、実施内容、モード、出力契約、Web調査契約が変わった場合は古い完了結果を再利用しない。
- 同一のものづくり補助金・公募要領に対する複数のWeb指示と、主題を伴わない「情報検索タスクを最優先」を1つの調査工程へ統合する。別主題のWeb調査は統合しない。
- 公開検索サービスへ追加指示全文を転送せず、ものづくり補助金では固定の公開用検索語「ものづくり補助金 最新 公募要領 公式 PDF」を使用するようにした。
- 対象テスト47件、追加補正後のWeb・計画対象テスト38件および全回帰を実行し、最終全回帰は `206 passed, 1 skipped`。
- Webサービスをデータ保持のまま再構築し、標準ヘルスチェックは全項目PASS。Docker内でも「情報検索スキル」のresearch判定と、Webタスクに対する試算表不足なしを確認した。
- 実プロジェクト「モノづくり補助金」を第7版へ安全に再計画した。旧第5版の8工程・Web原本0件・誤完了2件から、原本確認1件、公開Web調査1件、後続処理3件、最終検証1件の計6工程へ整理した。公開Web調査はSC00だけに依存し、固定公開検索語を持つresearchタスクとして未実行・承認待ちである。既存原本と成果物は削除していない。

## 2026-09-15 決定論的Web証拠レポート

- 第7版の公開Web収集は成功し、公式ポータル等のWeb原本6件を保存できた。一方、ローカルLLMが成果物の必須見出しを欠落させ、自動補正後もWeb調査工程が失敗したことを確認した。
- 公開Web調査タスクでは、LLMへ成果物の自由形式生成を依頼せず、取得済みWeb原本からバックエンドがMarkdown成果物を生成する専用経路を追加した。
- 計画契約の全必須見出しを必ず生成し、取得原本ref、URL、タイトル、取得日時、本文SHA256、公式候補状態を一覧化する。取得原本だけでは確定できない事例、効果、構成、制度要件は捏造せず、後続工程の確認対象とする。
- 生成後は既存のWorkspace操作監査、原本参照監査、実ファイル検証、Webメタデータ検証を通す。不合格時は既存の隔離・ロールバックを使用する。
- Web成果物生成経路ではローカルLLMと外部AIを呼ばない。外部AIへのプロジェクト原本送信、任意シェル、契約外ファイル操作も行わない。
- 専用テスト18件および全回帰を実行し、全回帰は `208 passed, 1 skipped`。
- Webサービスをデータ保持のまま再構築し、`scripts/healthcheck.ps1` は全項目PASS。4サービスはhealthyで、公開ポートはlocalhost限定であることを確認した。
- 実プロジェクト「モノづくり補助金」第7版を再実行し、公開Web調査工程SC01は取得済みWeb原本4件から `result/sc01.md` を決定論的に生成して完了した。イベント `deterministic_web_report_generated` と、必須見出し・URL・取得日時・SHA256の実証成功を確認した。
- 後続の一般計画工程SC02は、ローカルLLMの生成文が契約上の必須見出しを満たさず、既存の安全な再試行上限で停止した。不合格候補2件は隔離して変更前へ復元されており、これは今回追加したWeb証拠レポート工程とは別の未解決事項である。

## 2026-09-15 SC02計画書の固定見出し・本文検証

- `app/planning_document.py`を追加。ローカルの単一Markdown計画では見出し・保存先をバックエンドで固定し、本文の欠落、空欄・仮置き、文字数超過、未実施操作の完了表現を検証する。任意出力操作はモデルに生成させない。
- 取得済みWeb原本のURL・取得日時・抽出本文SHA256と取得器の設定状態を区別して提示する。未取得の版と取得機能の不存在を混同せず、本当に必要な原本不足は保持する。
- 公募要領調査計画で、単純な詳細計画作成の要求、既知の7見出し、取得日時・URL・SHA256付きWeb原本が揃う場合に限定して、具体的な調査手順を決定論的に生成する。相対日程、担当役割、確認項目、仮説の構成案、人間の承認、各工程の完了条件を記載し、現行制度の数値・実績・実行済み操作を捏造しない。
- 一般計画の本文はローカルLLMで構造化生成する。既存の必須入力検査・原本監査・成果物検証・隔離と復元を維持する。すべての内容の意味的正確性を自動保証するものではない。
- コピー上の実モデル検証では、長文による出力打切り、根拠のない期限、未実施作業の完了表現、計画に不要な手続き資料の要求を確認。最終のSC02専用適用条件ではLLMを使わず、取得済み原本から計画本文を生成し、既存の原本・成果物検証に合格した。
- 最終テスト: `219 passed, 1 skipped`。追加テストは固定見出し、空欄拒否、必須資料不足保持、対象範囲、外部AI不使用、完了表現拒否、取得済み資料を用いたLLM不要の計画生成を確認。
- 本番DBはSQLiteの読取専用接続から検証用DBへバックアップし、検証用WorkspaceでSC02を実行。本番SC00/SC01の完了状態、SC02のfailed、後続のblockedは保持した。本番タスクの再実行は未実施。
- 通常Dockerfileのネットワークなしビルドは依存キャッシュ不足で失敗したため、稼働済みイメージ`localsaporter-web:before-sc02-20260915`を保持し、そのイメージへ変更した2ファイルのみをCOPYして反映。新規依存インストールなし。Webサービスのみ再作成、データボリューム保持。
- 反映後の`scripts/healthcheck.ps1`は全項目PASS。ホストとコンテナ内の2ファイルのSHA256一致を確認。
- ロールバック用ソース・検証DB・生成候補・反映用Dockerfile: `C:/Users/kanto/LocalCowork/workspace/temp/sc02-fix-20260915/`。
## 2026-09-16 能力向上設計・初回B01〜B05

- SC03の整合DB、全タスク、Workspace44ファイルを固定。登録15資料にPDF原本バイト列なし。保存本文には必要語句があり、従来の先頭提示文を再構成すると補助率・上限が欠落。必要箇所選択後には含まれることを確認。過去の実送信内容と意味的な充足は未確定。
- 能力定義/タスク別確認/原本保持/抽出/提示を分離。project-scoped source_versions/units、attempt、失敗分類、実HTTP推論直前の提示監査を追加。内部再試行の圧縮後とstreamも記録する。
- 新契約、主張・根拠検査、共有回復予算、内容確認待ちの基礎コードも追加。ただし本番の新契約登録なし。B06以降の実案件での意味・版・要求充足の評価、原因別自動回復、人間確認ワークフローは継続課題。
- 全回帰238 passed, 1 skipped。UI node --check合格。ローカルモデル読取り診断3回、各1要求、33.355/17.199/17.168秒。応答取得をSC03合格とは扱わない。
- 対象プロジェクトのみshadow（他はoff）で反映。事前観測1件のみ記録し、本番推論とSC03再実行は行わない。SC00〜SC02 completed、SC03 failed、後続blockedを保持。対象全タスク行と44ファイルのハッシュ一致。
- アプリ12ファイルと追加テスト1ファイルを反映。コンテナの12ハッシュ一致。既存依存のアプリ差分イメージをネットワークなしで構築、Webのみ更新、データ保持。標準healthcheck全項目PASS。
- 新image: sha256:edb546039a4c1c9dc28a4c5fd974b49e40a3a2a32f3b40baa1d023adabe46acf。切戻し: localsaporter-web:before-capability-upgrade-20260916 (9e194be099bbfe0bc9ed368f057cdade63c273f9e008b0c3de3b3532cd6874b5)。新契約がある場合は旧コードで再開しない。
- 証拠: C:/Users/kanto/LocalCowork/workspace/projects/capability_upgrade_implementation_20260916/。説明: output/Local_Cowork_能力向上_初回実装報告_2026-09-16.md。

## 2026-09-16 SC03新契約のコピー検証（稼働反映は承認待ち）

- 文書専用指示、最大3要求の分割生成、原本区間3,400文字制限、主題一致の優先、型付き部品・接続からのMermaid生成を実装。内容不合格の確認待ちへの誤変更、提示不明の未提示断定、空欄のタイムアウト例外、条件文の誤判定を補正。
- 全回帰247 passed, 1 skipped。変更6ファイル（アプリ5・テスト1）。ソースは更新済み。
- 現行24Bモデルは長文候補取得・タイムアウトの問題が継続。既存qwen3.5:9bをコピー上で使用した最終3回は32.77/37.43/30.89秒、各3要求。見出しと図の形式は3/3合格、引用・未観測の完了表現等の内容は3/3不合格。最終検証器で保存候補を再照合。SC03を完了にしない。
- 候補イメージsha256:e47e3c90344cdf6574165582206743251d65a62a5df3ced67ed425f12861abb5を既存依存のCOPYのみで構築。切戻しタグlocalsaporter-web:before-sc03-contract-20260916。
- 共有Webサービスの再起動は自動承認レビューが拒否。理由は、内容検査に不合格の候補がある状態での共有サービス更新。迂回はせず、観測モードを維持した更新の明示承認を依頼した。稼働中はedb546039a4c1c9dc28a4c5fd974b49e40a3a2a32f3b40baa1d023adabe46acfのまま。ソースと稼働版は異なる。
- 既存稼働版のhealthcheck全項目PASS。本番6タスク行・45ファイルの保全一致。対象shadow・本番既定モデルmistral-small3.2を保持。新契約登録・本番SC03再実行・外部AI送信・依存追加なし。
- 証拠: C:/Users/kanto/LocalCowork/workspace/projects/sc03_contract_validation_20260916/。報告: output/Local_Cowork_SC03_新経路検証結果_2026-09-16.md。

## 2026-09-16 08:56 JST 修正版の稼働反映完了

- 前回の自動承認レビューによる保留後、ユーザーが「最新化して」と明示指示。対象プロジェクトの観測モードを維持してWebサービスのみ更新した。
- 稼働image: sha256:e47e3c90344cdf6574165582206743251d65a62a5df3ced67ed425f12861abb5。旧版edb546...はlocalsaporter-web:before-sc03-contract-20260916として保持。
- 更新直前に実行中タスク0件を確認し、整合DB・全タスク行・対象Workspaceハッシュ・設定を固定。更新後、全30タスク行と対象47ファイル、設定の一致を確認。アプリ5ファイルのSHA256がrelease_manifestと一致。
- 更新後のscripts/healthcheck.ps1は全項目PASS。対象shadow、他off、既定mistral-small3.2を保持。SC00〜SC02 completed、SC03 failed、SC04/最終検証 blockedを維持。SC03の新契約有効化・再実行は行っていない。
- 承認待ちは解消。証拠: C:/Users/kanto/LocalCowork/workspace/projects/sc03_contract_validation_20260916/activation_20260915T235632Z/production.db、before.json、verification.json。


## 2026-09-16 原子的主張・経路表示の適用準備（本番更新未実施）

作業コピー: C:/Users/kanto/LocalCowork/workspace/projects/upgrade_application_20260916。257 passed, 1 skipped。現行本番healthcheck全PASS。SC03コピー: qwen3.5:9b、67.96秒、3実要求、21主張、完全一致引用12、引用不一致0、形式passed、内容needs_review。検証済み改善版イメージatomic-20260916を作成。本番ソース上書き・latest切替・再起動は自動承認レビューで拒否され未実施。現行e47e3c90344cとソースが未変更であることを確認。タスク限定enforce/新契約の本番登録も未実施。詳細はworkspace/output/Local_Cowork_改善適用検証結果_2026-09-16.md。


## 2026-09-16 明示承認後の改善版本番反映完了

ユーザー「変更実施」の承認に基づき、検証済み8ファイルとatomic-20260916イメージを適用しWebを再起動。稼働イメージ6e73128a72474fb6174f8af39418d3af6737d95c7f83558b47e2932b92454735、healthy。アプリ7ファイルのハッシュ一致、30タスク・51ファイル保全、更新後healthcheck全PASS。SC03だけにatomic-v1 case_analysis契約とタスク別enforceを設定し、画面APIでdocument経路を確認。他5タスクはshadow/legacy。本番タスク再実行は未実施。回帰テスト257 passed, 1 skippedの検証済みコードから変更なし。証拠: workspace/projects/upgrade_application_20260916/activation_20260916T015323Z/{verification.json,sc03_activation.json}。初回設定スクリプトはPath型不一致で書き込み前に停止し、型を修正した再実行で正常完了。


## 2026-09-17 2段階計画・SC03限定適用

全体計画の親要求を保持した詳細6工程、順次実行、中間成果ハッシュ照合、部分再開、親予算12要求/900秒、版管理、対象固定の内容確認API/UIを実装。回帰267 passed, 1 skipped。最終監査・表示調整後の関連受入10件全成功。SC03コピー最終版: qwen3.5:9b、133.46秒、8実要求、D01-D05完了/D06 needs_review。再開6.82秒、追加推論0、全成果再利用。本番イメージ3f405b700f96e1811f5731dcb6ee50e1d2404816c8c7acc495b3599ab8e9b110へ反映、アプリ9ファイルのハッシュ一致、healthcheck全PASS。30タスク・51ファイル保持。SC03だけexecution_strategy=two-stage-v1、enforce/detailedをAPI確認。本番SC03再実行は未実施。バックアップ: workspace/projects/two_stage_planning_20260917/activation_20260917T014100Z。詳細報告: workspace/output/Local_Cowork_2段階計画_実装適用報告_2026-09-17.md。

## 2026-09-17 全6工程への2段階計画適用・第8版へ再計画

ユーザー「状況確認して、全体に適用、計画作成からやり直します」に基づき、対象プロジェクト全6工程を enforce / detailed へ切り替え、第8版へ再計画。番号なし達成条件の取り込み、目標別の全体計画生成、公開Web先行、経営計画→補助金提案の依存関係、版別成果物パス、全契約の一括登録と版競合検査を追加。Web収集・最終検証にも中間保存付き専用詳細経路を実装。主題がずれたローカルモデル候補は不採用とし、採用した全体計画は目標に対応する登録テンプレートから生成した。

回帰273 passed, 1 skipped。最終の旧レビュー履歴リセット調整後、関連18 passed。更新後healthcheck全PASS。稼働イメージ62d68806cce94f448d74a3626cced7add6ea7f9943f9bc36ae9762598a88162b、アプリ6ファイル照合成功。更新前DB・設定・旧計画・53ファイル実体を退避。計画置換後、他プロジェクト24タスク行と対象既存53ファイルの不変を確認。本番は第8版 planning、0/6、全工程pending。業務タスク実行・今回の公開Web収集・外部AI送信は未実施。

証拠: C:/Users/kanto/LocalCowork/workspace/projects/project_wide_planning_20260917/activation_20260917T141936Z/。切戻しイメージlocalsaporter-web:before-project-wide-20260917（旧3f405b70...）。復旧には契約・DB・設定の整合確認が必要。詳細報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_全工程適用と計画再作成_2026-09-17.md。

## 2026-09-18 停止環境でのローカルLLM予備比較

既存主要3モデルの短問45回、長文9回、Qwen推論有効3回、Llama3のOllama/公式llama.cpp比較30回、計87回を架空資料で実施。短問の厳密合格はQwen6/15、gpt-oss13/15、Mistral15/15。資料ID表記だけの正規化後は12/15、14/15、15/15。Qwenは金額単位を3回誤り、推論有効では2/3回が最終回答なし。長文は主要3モデル各3/3合格。上流llama.cpp b11026は既存Qwen/gpt-oss/MistralのGGUFと互換性エラー、Llama3は動作しOllamaとの速度は同等程度。指定forkのmoe-cache/35Bはビルド環境とRAM余裕の前提未充足で未実行。

本番コード・設定・モデル変更なし。公式CUDAバイナリとruntime DLLは検証領域のみへ展開し公開SHA256を照合。終了時Ollamaモデル0件・検証ポート待受なし・Local Cowork4コンテナexitedを確認。本番停止維持のためhealthcheck起動確認は未実施。証拠: C:/Users/kanto/LocalCowork/workspace/projects/local_llm_benchmark_20260918/。報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_ローカルLLM実測結果_2026-09-18.md。


## 2026-09-18 ローカルLLM継続検証・35B MoE実機推論

指定fork 925933801を専用CUDA Dockerイメージとしてビルド。Qwen3.6-35B-A3B Q4_K_M（20.42GB）をSHA256照合し、専用volumeでも一致確認。cache128/RAM6GiB/pinned512MiBで基本5件完走、起動35.0秒、応答31.8〜64.9秒、厳密2/5、コードフェンスのみ除去した参考4/5。cache64は6件回答後に意図的中止。初期試行は低RAMで保護停止し、開始RAM条件を強化。既存モデルの確定20課題×3回はQwen50/60、gpt-oss low50/60、Mistral38/60、追加gpt-oss medium53/60。診断用プロトコルの結果は確定比較から除外。本番変更なし、主要4コンテナ停止維持、全検証コンテナ停止、Ollamaロードモデル0。停止維持のため稼働healthcheckは未実施。報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_ローカルLLM継続検証結果_2026-09-18.md。証拠: workspace/projects/moe_validation_20260918/、終了確認final_verification.json。

## 2026-09-18 経験RAG・外部AI回答再利用の実装

設計をworkspace/output/Local_Cowork_経験RAGと外部AI再利用_設計保存_2026-09-18.mdへ保存。経験SQLite、LangChain/Chroma/Ollama検索、検証付き完全一致回答キャッシュ、project境界、失効、HTTP試行単位の予算予約・重複防止、ローカルCLIを実装。計画・工程・通常会話・外部調査へ接続。共有リポジトリ13ファイルへ反映、既存6ファイル退避、反映ハッシュ一致。全回帰286 passed, 1 skipped、compileall/pip check成功。既存nomic-embed-textとQwenの架空1問で検索・生成成功（300/700）。有料外部AI呼び出しなし。新依存は専用venvのみ。DockerのRAG用ビルド引数追加、本番イメージ再構築・起動・機能有効化は未実施、初期off。healthcheck反映前後実施、停止中サービス・コンテナ・ポートの12FAILを記録、Ollama/workspace/モデルPASS。コード原本・証拠: C:/Users/kanto/LocalCowork/workspace/projects/experience_rag_20260918/。実装報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_経験RAGと外部AI再利用_実装報告_2026-09-18.md。費用は予約見積り管理で実請求上限保証ではない。改善率・削減率は未測定。

## 2026-09-18 実行例取り込み・再現機能の本番実装と起動

前回の「全て本番環境へ実装、本番も起動」の続きとして13ファイルをバックアップ付き反映。第5タブ、私有原文DB、節・行・12工程/6課題抽出、資料添付と文字抽出、課題確認、版管理・失効、正規化JSONからの車両損益Excel、LibreOffice再計算・独立照合、再現ジョブ・取消、採用レビュー、計画への参照指示、確認済み教訓のRAG登録を実装。任意の元Excel/PDFから計算条件を自動確定する機能は未実装で、確認済み正規化入力が必要。

最終全回帰298 passed, 1 skipped。隔離Dockerの架空再現・API一連検証合格。headless Chromeの5タブ・キーボード・モバイル幅検証合格。標準healthcheck全PASS、4サービス起動。既存30タスク全列ハッシュ保全、コンテナ内アプリハッシュ一致。原文の本番登録と専用プロジェクトRAG enforce設定は自動承認レビュー拒否により未実施・ユーザーへ承認確認中。本番機能の導入・起動は完了。作業証拠: workspace/projects/agent_example_production_20260918/。報告: workspace/output/Local_Cowork_実行例再利用_本番実装報告_2026-09-18.md。切戻し: localsaporter-web:before-agent-examples-20260918。

## 2026-09-18 追加承認後の実行例本番登録・RAG有効化完了

ユーザー「承認しますので反映を完了させて」の明示承認により、専用プロジェクト「車両別損益・実行例再利用」（cb073c829740404d9d00fe588de4da69）へ指定MDをneeds_reviewで登録。報告上の12工程・6課題を確認。このprojectだけ経験RAG enforce、Chroma索引初期化成功。未検証原文は共有コンテキスト・検証済み教訓へ追加せず、外部AI送信なし。既存30タスク全列ハッシュ保全、実行例・6課題の画面表示、project切替時の詳細消去、最終healthcheck全PASS。承認待ちは解消。証拠: workspace/projects/agent_example_production_20260918/activation/approved_activation.json、approved_ui_result.json。車両損益の実業務再現は入力原本・条件確認が必要で、今回の原文取り込みを業務完了とは扱わない。

## 2026-09-19 手順学習・模倣機能の実装と本番反映

ユーザー「その内容で実装」「続けよ」に基づき11ファイルをバックアップ付き反映。ローカルLLMによる根拠行付き工程抽出、原本列対応・単位・定数の確認と提案、8種の処理を組み合わせる手順生成・編集、工程途中までの試行、独立期待結果による元条件・異なる入力の転用検証、修正前後・一般/案件限定範囲の蓄積、採用時の版切替、失効・切戻し候補、project内ライブラリ、計画参照と検証済み経験RAGを実装。採用済みで現行検証を満たす一般修正のみを次回生成へ利用。不足能力・未確認条件は採用を阻止。画像OCR・任意ツール追加やモデル重み学習は含まない。

全回帰316 passed, 1 skipped（依存ライブラリ非推奨警告1件）。実ローカルモデルの手順抽出→生成→異なる列名/件数/金額の再現・転用→採用を架空データで確認。Excelマスタ+CSV売上/給与/配分+文字表PDF燃料から損益Excel生成、LibreOffice再計算、2ケースの独立期待値照合PASS。本番UI・390px幅・JavaScriptエラー検査PASS。最終 scripts/healthcheck.ps1 全PASS。既存30タスク・原文・モデル/RAG/能力設定ハッシュ保持、コンテナ内アプリハッシュ一致。原文の実業務再現を架空テスト成功と同一視しない。

報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_手順学習と模倣_実装報告_2026-09-19.md。証拠: workspace/projects/procedure_learning_20260918/、activation_20260919/。本番 localsaporter-web:procedure-learning-20260918、切戻し localsaporter-web:before-procedure-learning-20260919。既存データ保持、外部AI送信なし。

## 登録済み実記録の最終確認

登録済みの車両損益作業記録から、ローカルOllamaで11工程の候補を抽出し、本番へ needs_review として保存した。根拠引用は全件で原文に完全一致。最初の試行では空配列、次の試行では引用言い換えを検出して失敗扱いにし、指示・行番号根拠方式を修正後に完了した。候補11件を元記録の番号付き12項目との完全対応や実業務成功の証明とは扱わない。工程の意味・網羅性・計算条件は画面で確認する。実原本の再現・転用テストを通した稼働手順はまだ0件。証拠: activation_20260919/actual_source_learning.json。

検証完了後、今回専用の local-cowork-learning-validation コンテナを停止。検証データは保存し、本番サービスは稼働を維持した。

## 2026-09-19 TRIZ手順発明機能の本番実装

ユーザー「実装せよ」に基づき11ファイルをバックアップ付き反映。実行例の行き詰まり検知、技術的/物理的矛盾・理想・資源・制約定義、ローカルOllamaによる複数案生成、12発明原理+4分離原理、有限表操作への変換と型検査、同一構造案の除外、元手順/候補の固定基準比較、制約/副作用レビュー、再現/転用後の採用、失敗手順を含む発明履歴の再利用を実装。発明/比較各最大3回、時間上限・取消・再起動復旧あり。完全なARIZ/矛盾マトリクス、任意ツール自動追加、モデル重み訓練は対象外。

最終全回帰327 passed, 1 skipped（依存非推奨警告1件）。実ローカルgpt-oss:20bの架空データ検証は初回3案不合格、失敗手順/理由と処理仕様の改善を反映した2回目で新しい1案が元条件と別車両/別金額の独立期待値に一致し、確認後採用まで成功。隔離/本番ブラウザ、入力保持、project切替消去、390px幅、JavaScriptエラー検査PASS。scripts/healthcheck.ps1全PASS。既存30タスク・実行例2件・学習1件・モデル/RAG/能力設定ハッシュ保持、コンテナアプリハッシュ一致。本番には架空試験データを登録せず、外部AI送信なし。

public-web-evidenceスキルでTRIZ公開一次資料を確認し、取得情報・支持する主張・要約ハッシュをapp/triz_sources.jsonへ保存。報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_TRIZ手順発明_実装報告_2026-09-19.md。証拠: workspace/projects/triz_invention_20260919/、release/。本番localsaporter-web:triz-20260919、切戻しlocalsaporter-web:before-triz-20260919。

## 2026-09-19 分野横断TRIZの実装・本番反映

ユーザー「その設計を実装」に基づき12ファイル反映。TRIZの問題整理を共通化し、文書/資料調査/ソフトウェア/業務/障害/ブラウザ/その他の7分野に対応。文書Markdown生成、保存資料の引用付き報告、Python修正コピーと構文検査、業務手順書、ログ根拠付き診断計画の5アダプターを接続。ブラウザ操作・新規Web取得・コード実行テスト・実システム変更などの未接続工程は実験計画と必要能力を保存し、実行済みとはしない。独立ケース、分野別機械検査、人の証拠・任意の測定基準、試験に結び付いた採用、条件変更/再生成時の再確認、改ざん検出、project内再利用、上限・取消・再起動復旧を実装。

最終全回帰340 passed, 1 skipped（既存依存警告1件）。新規受入13件で5アダプターと拒否・保全経路を検証。実ローカルgpt-oss:20bの架空文書問題では、入力抜粋の追加後の2回目に実行可能案を得て、2案が元条件と別条件で合格。最終アダプターでも1案を再試行して両ケース合格、原文の受付時間・締切・変更条件・申請方法の保持を目視確認。実業務効果・本採用とは区別。UI7分野・入力保持・切替消去・390px幅・JavaScriptエラー検査PASS。scripts/healthcheck.ps1全PASS。30タスク・実行例2件・学習1件・モデル/RAG/能力設定ハッシュ保全、コンテナアプリハッシュ一致。架空試験は隔離環境のみ、外部送信なし。

報告: C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_分野横断TRIZ_実装報告_2026-09-19.md。証拠: workspace/projects/triz_general_20260919/、release/。本番localsaporter-web:triz-general-20260919、切戻しlocalsaporter-web:before-triz-general-20260919。

## 2026-09-19 低コスト改修：要確認一覧と状態ガイド

次期開発計画のR2初期版を6ファイルで実装・本番反映。実行例/学習/分野別TRIZの保存状態をproject単位に読取専用で集約し、区分絞り込み、ページ、既存確認画面への遷移、状態説明を追加。既存の採用・版検査は維持。依存追加・DB移行なし。全体342 passed/1 skippedと作業コピー資料不足2件、資料補完後の当該再実行2 passed（合計344成功）。新規4件・画面境界/エラー/390px・本番遷移確認PASS。healthcheck全PASS。30タスク/実行例2/学習1/設定ハッシュ保全、コンテナアプリ照合成功。本番localsaporter-web:review-queue-20260919、切戻しbefore-review-queue-20260919。証拠workspace/projects/low_cost_review_20260919/、報告workspace/output/Local_Cowork_低コスト改修_実装報告_2026-09-19.md。一般業務タスクの承認待ち集約・一覧での承認・R0/R1/R3全体は未実装。

## 2026-09-19 準備工程とRAG制御の補強

準備工程の領域選択、一般CSVの扱い、登録目録に基づく文書生成、存在しない原本IDの保存前拒否を実装・本番反映。経験RAGを対象プロジェクトで有効化。最終回帰359 passed、1 skipped。追加の準備・引用制約テスト15件合格。本番アプリ照合、画面確認、healthcheck PASS。入力原本と他プロジェクトの業務状態を保全。Bonsai 2の比較試験は継続中のため、採用・比較完了は本項では宣言しない。詳細はローカルworkspaceの実施報告へ保存する。

## 2026-09-19 Planning progress recovery
- Planning records unresolved input conditions instead of failing solely on missing_inputs.
- PDF extraction detects unreadable text and preserves original extraction alongside reversible candidates.
- Regression: 362 passed, 1 skipped; focused follow-up: 29 passed.
- Release import and healthcheck: PASS (healthcheck repeated after startup).
- Actual financial workflow execution remains unverified.


## 2026-09-19 Artifact download UI
- Added persistent artifact shelf with filename/type filters, text preview, individual download and ZIP download.
- ZIP includes result/ alongside existing output directories.
- Regression: 362 passed, 1 skipped. Live browser: listing, search, preview, individual and ZIP download PASS. Healthcheck PASS.


## 2026-09-19 Vehicle execution and goal verification
- Preserve unnumbered criteria; route vehicle-profit goals to typed source, calculation and verification steps.
- Reuse workbook engine with required coverage, missing-data handling, input/source versions and explicit review gates.
- Add calculation-input UI and current versus previous artifact labels.
- Regression: 371 passed, 1 skipped. Isolated 2-vehicle/7-month Excel acceptance: PASS. Live UI and healthcheck: PASS.
- Real accounting input remains under review; no claim of completed business calculations.


## 2026-09-19 Human-reviewed RAG and common plan gate
- Added version-bound external plan gates for parent and detailed plans.
- Added explicit human result review, local RAG registration, expiry, revocation and source/artifact integrity checks.
- Regression: 379 passed, 1 skipped; focused final checks: 14 passed. Live UI, import and healthcheck: PASS.
- Actual external review transmission was blocked by automatic approval review; no production human approval or success record was fabricated.


## 2026-09-19 Plan feedback revision workflow

Implemented version-bound external review feedback import, local Ollama amendment drafts, issue-to-change mapping, before/after preview, and explicit application followed by re-review. Goal, acceptance contracts and permissions remain fixed. Missing capabilities remain development blockers. Added budget visibility and exact-packet response reuse for pending-provider retries. Closed review DB connections reliably.

Validation: full regression 386 passed / 1 skipped; final focused acceptance 15 passed; browser feedback flow PASS; actual local Ollama amendment acceptance PASS; deployed UI gate checks PASS; scripts/healthcheck.ps1 PASS.

Release: localsaporter-web:plan-feedback-20260919. Rollback: localsaporter-web:before-plan-feedback-20260919. No external budget increase or production mission rewrite performed.

## 2026-09-19 Legacy feedback and normal regeneration bridge

Connected saved legacy reviews to current feedback with explicit unknown source-version provenance and deduplication. Normal regeneration preserves and supplies feedback to local planning, then produces a response matrix; unresolved development gaps remain visible in the plan summary. Fixed real-model omission of issue IDs with required object keys and reduced redundant prompt data.

Validation: regression 388 passed / 1 skipped; final focused tests 17 passed; production regeneration carried 2 saved responses and recorded 2 unresolved dispositions; deployed UI gate test PASS; scripts/healthcheck.ps1 PASS. No external requests or budget increases.

Release: localsaporter-web:feedback-bridge-20260919. Rollback: localsaporter-web:before-feedback-bridge-20260919. Workflow integration is verified; domain calculation capability gaps remain unresolved.

## 2026-09-19 Separate human plan approval from execution clearance

Human plan approval now preserves structural validation and records approval even when external review is pending. UI shows approval pending external validation and disables execution with an explicit reason. Server execution and result-RAG gates remain enforced. No production approval was recorded on the user's behalf.

Validation: 389 passed / 1 skipped; approval-state browser test PASS; scripts/healthcheck.ps1 PASS. Release localsaporter-web:approval-state-20260919; rollback localsaporter-web:before-approval-state-20260919.

## 2026-09-20 車両別月次損益の自動生成改修

- ユーザー承認範囲の実装を反映。原本自動抽出、4工程の車両計画、暫定Excel、原本・不足・未配賦表示、限定した配賦回答UI、外部計画検証の公開草案と予算待ちキュー、車両計画の構造修正、TRIZ課題自動準備、確認済み実行手順参照を追加。
- 403 tests passed, 1 skipped。`scripts/healthcheck.ps1`: PASS。ブラウザで進捗・37確認事項・原本リンクを確認しJSエラー0。
- 実原本49件から台帳32台×2026年1〜7月=224行を生成。実額748件、仮定ゼロ1,165件。LibreOffice再計算、独立利益計算、車両シート、配賦算術照合に合格。数式エラー0。
- 成果物は受入試験の暫定Excel。未配賦52,290,029円、税基準混在等を残し、業務目標達成・人間結果承認・成功RAG登録を代行していない。外部AIへの新規送信なし。
- 改修報告: `C:/Users/kanto/LocalCowork/workspace/output/Local_Cowork_車両別月次損益_自動生成改修報告_2026-09-20.md`
- 暫定Excel: `C:/Users/kanto/LocalCowork/workspace/output/車両別月次損益_2026年1月-7月_自動生成受入試験_暫定_20260920.xlsx`
- 証拠: `C:/Users/kanto/LocalCowork/workspace/projects/vehicle_auto_20260920/` の tests_full.log, healthcheck.log, ui_result.json, live_prepare.json, acceptance_output/result.json, release_manifest.json。
- 稼働イメージ: `localsaporter-web:vehicle-auto-20260920`。rollback: `localsaporter-web:before-vehicle-auto-20260920`。変更前ソースは証拠フォルダ source_backup に保持。既存volume・原本・モデル・API設定を維持。
- 対応範囲: 定型原本の抽出・車両処理は実証済み。一般TRIZの試験は接続済みの隔離成果物に限定し、任意コードの本番適用や未接続能力を実行済みとしない。新形式の完全自動アダプター開発、未根拠の配賦率・社会保険率推測は行わない。

## 2026-09-24 実装単位9 — 検証記録（healthcheckとBUILD_REPORT最終パス）

文書のみの単位。コード変更なし。過去節（403/414/434件等）は履歴として残す。本節が現時点の最新実測である。

### 検証メタデータ

- datetime_utc: 2026-09-23 20:22:50 UTC
- datetime_jst: 2026-09-24 05:22:50 JST
- source_revision: `26f4f17a7e0f6320b3ee843df6188e299fb676d9`（2026-09-23 20:13:40 +0000）
- extractor REVISION (`app/vehicle_auto.py`): `20260923.3`
- container_image: 該当なし（Jenkinsワークスペース内の作業コピー。本番未反映）
- scope: full — 全体pytest実行
- command: `pytest -q`
- passed / failed / skipped / warnings: **528 passed, 0 failed, 1 skipped, 1 warning**
- runtime: 25.18秒
- runtime environment: Jenkinsコンテナ内。本番Dockerではない
- artifact log path: 本セッションのテスト出力のみ、ログファイル未保存
- 再開manifest: `docs/verification/unit9_resume_manifest_2026-09-24.md`

### 単位0〜8で実装された範囲の要約

- 単位0: 保全と記録。TRIZ CHANGELOGへ2026-09-22確認時点の74件 defined 訂正を追記。コード変更なし。
- 単位1 P0-1 正確性: 金額の欠損・読取失敗・実額ゼロを分離。数値0は actual、非数値は read issue。仮定ゼロは真正欠損のみ。
- 単位2 P0-2/P0-3 正確性: 独立原本照合（空の source_controls を合格にしない）、請求単位の重複判定と部分抽出。鑑は control として扱う。
- 単位3 P1-1 UI: workflow-readiness（停止理由・次操作・ボタン可否）。永続ジョブ表は単位4へ。
- 単位4 P1-2 永続ジョブ: 計画修正→再検証→承認→実行の接続。文章追加だけで車両正確性を閉じない。
- 単位5 P1-3 配賦: 月別配賦・社会保険根拠・判断継承。料率推測禁止。
- 単位6 実原本受入: **未実施**（明示指示後の別セッション）。
- 単位7 P2-1: TRIZ課題から業務回復接続。既知P0欠陥はTRIZに回さない。74件 defined を回復件数に数えない。
- 単位8 P2-2: 人間承認済みレシピの実行再利用。金額非再利用、条件不一致は明示拒否（黙った fallback 禁止）。

### 本単位で未実施であること（合格と書かない）

- `scripts/healthcheck.ps1`: **未実施**（powershell 実行不可）
- Docker build / deploy / 起動確認: **未実施**（本エージェントは docker 不可。Web UI「🚀 デプロイ&動作テスト」が別途ある）
- 実原本受入（単位6）: **未実施**
- 本番DB読取、外部AI送信、サービス起動: **未実施**

### 業務確認事項

設計書第13章の36項目は未回答のまま。実装は「足りない業務事実は推測しない・issueにして止める」の安全側既定で進めている。料率・日割り・均等配賦の推定、読取失敗・未配賦・照合不能の0円解消は行わない。

### TRIZ CHANGELOG

`docs/CHANGELOG_2026-09-21_triz_import.md` 最新節（§8 訂正 2026-09-23追記）は、既に「本番未投入」が最新状態ではないと訂正済み。本単位では追加訂正不要。


## 2026-09-25 車両損益修正(単位0〜9)とOCR Phase 1〜4 を本番へ反映

- 詳細: `docs/CHANGELOG_2026-09-25_vehicle_pnl_and_ocr.md`
- 対象版: イメージ `localsaporter-web:vehicle-ocr-20260925`(ID a7eef93375f1)。`extractor` の `REVISION` は `20260923.3`。旧イメージは `localsaporter-web:before-vehicle-ocr-20260925`。
- テスト: **本番イメージ内で全体 656 passed**(scope=full、`pytest -q`。`tests/` と `docker-compose*.yml` 等は NAS を直接マウントできないためローカルへコピーして読み取り専用でマウント、`.env` は含めない)。ジェンキンス側は 655 passed, 1 skipped。
- `scripts/healthcheck.ps1`: 統合フロント(8099)・`web` コンテナ・ワークスペース・設定済みモデル・`gpt-oss:20b`・`qwen3.5:9b` は PASS。Open WebUI・Computer・Google Publisher Browser は停止中のため FAIL(9件。今回の変更とは無関係)。
- 動作確認(本番実データ): 8プロジェクトが読める(車両別評価データの作成 = ファイル53件)。新しい判定で実案件は `accuracy_blocked`(原本統制値不足で確定できない=暫定、未配賦225件)。OCR要求は機能が既定無効のため拒否される。
- **未実施**: OCRの実PDF受入(Phase 5)、PaddleOCRサービスの実起動とGPU確認、実案件を通す受入(単位6)。業務確認事項36項目は未回答。
- 巻き戻し: 上記旧イメージを `latest` に付け替え、`docker compose up -d --no-build --no-deps web`。ソースの退避は `.jenkins_backup\20260925-020324\`、データの退避は `C:\Users\kanto\Short-video-local\backup_prod_20260925\localsaporter_app_data.tar.gz`。

## 2026-09-27 セバス goal_completion・宇佐美OCR補正 配布物の本番反映

- ユーザー指定配布物 `LOCALSAPORTER_deploy_20260927-211758.zip` を内容監査後に反映した。
- ZIP SHA-256: `92100B66D3570F685E5600687205F459DEBF47C96051BEF849321E60F0EAD67B`
- 手順書 SHA-256: `057F73F7C46F0E98E077430ED90F92DB636A6DEEF59A0C33F18547712F8683EB`
- ZIPは290ファイル。危険な相対パス、`.env`、DB、data、workspace、backups、Git内部情報を含まないことを確認。
- 現行との差分は新規62、変更23、同一205ファイル。上書き後、ZIP全290ファイルのSHA-256一致を確認。
- 事前バックアップ:
  - `backups/20260927-211758/app_data.tar.gz`
  - `backups/20260927-211758/source_before_deploy.tar.gz`
  - `SHA256SUMS.txt` と両ファイルのSHA-256一致を確認。
- 反映後イメージ: `localsaporter-web:latest` / `sha256:a03eeaee5b394bfa3a714f802ca97c6f7a5ae8107850a633ca253497fcc4dc7d`
- ロールバックイメージ: `localsaporter-web:before-goal-completion-20260927-211758` / `sha256:a7eef93375f1f6c78ff13f93bda6d39093b7e9d20284ed35f376ea246ee1b002`
- webだけを `docker compose up -d --no-build --no-deps web` で再作成。他サービス、named volume、原本、履歴、設定は保持。
- `app/vehicle_auto.py` の `REVISION`: `20260927.2`。

### 主な追加・変更

- GoalContract、要求保持検査、計画被覆、Completion Gate。
- goal state machine、Next Action Controller、再起動収束。
- 車両12条件テンプレート、目標検査、最小質問ビュー、facts履歴。
- 読み取り専用証拠グラフ、目標指標、現行状態・release manifest生成。
- TRIZ除外規則。
- OCR runtime、PaddleX layout client、宇佐美御買上明細の人間承認済みOCR補完。
- OCRレビュー画面、workflow readiness、成果物表示の改善。

### 検証

- 候補イメージのPython全構文検査: PASS。
- 新規機能重点テスト: **158 passed、1 deselected**。
- 配布物だけを候補イメージへ渡した初回全体試験: 835 passed / 13 failed / 1 skipped。13件は配布対象外の本番compose、TRIZ入力、scriptsを隔離試験rootへ渡していないことが原因。
- 本番補助ファイルを隔離環境へ追加した全体試験: 847 passed / 1 failed / 1 skipped。唯一の失敗は `test_oa08_compose_structure_and_base_compose_unchanged` の固定SHA-1が、現行本番 `docker-compose.yml` と異なること。ZIPは本番composeを含まず、反映でもcomposeを変更していない。
- 更新後の本番イメージ全回帰（上記固定SHA-1試験1件を明示除外）: **848 passed / 1 deselected / 1 warning**。
- `scripts/healthcheck.ps1`: 全項目PASS。
- web / Open WebUI / Computer / Google Publisher Browser: 稼働確認。webはhealthy。
- `/api/health`: status ok、Ollama接続、engine稼働。
- 既存8プロジェクトを読取可能。
- 車両案件は従来どおり `accuracy_blocked`、暫定、未配賦225件、原本統制値不足を保持。未達を確定扱いにしていない。
- 新APIのroute登録を確認: goal-contract、completion-gate、next-action、evidence、questions。
- 目標達成機能の設定ファイルなし、global/projectとも無効。OCRも無効。今回の反映では有効化していない。

### 未完了・未実証

- goal_completionを有効化した本番案件の実行。
- 実Ollamaによる汎用目標分解品質。
- 実ブラウザでの新UI受入。
- OCR実サービス・GPU・実PDFでのPhase 5受入。
- 実案件を通す単位6。
- 業務確認事項36項目の完了確認。
- 配布元固定SHA-1と現行本番composeの一致。現行composeは保持し、互換性をhealthcheckと本番APIで確認した。



## 2026-09-27 目標達成機能・OCRの有効化

- ユーザー指示によりGoal Completionを全体有効化。`/data/memory/goal_completion.json` の実効値をtrueで確認。
- OCR profileを有効化し、webへ `LOCALSAPORTER_OCR_ENABLED=1`、`LOCALSAPORTER_OCR_URL=http://127.0.0.1:8080` を適用。
- PaddleX文書解析(8080)とPaddleOCR-VL-1.6-0.9B VLM(8081)をwebの共有network namespaceで起動。外部公開ポートなし。
- NASの相対bind mountはDocker Desktopが拒否したため、同一SHA-256のpipeline設定を `C:/Users/kanto/LocalCowork/workspace/temp/sebas_ocr_runtime_20260927/` からread-only mountするoverrideを使用。
- PaddleX `/health` HTTP 200、VLM `/v1/models` HTTP 200、標準healthcheck全項目PASS、webログにERROR/Traceback/Exceptionなし。
- 既存車両案件: planned、achieved=false、provisional、PASS 5 / FAIL 6 / UNTESTABLE 1、未配賦225、人間未承認。安全側判定を維持。
- 実PDF OCR、Phase 5精度測定、OCR経路の車両単位6、目標達成機能による実案件自動実行は未実施。
- 標準 `scripts/start.ps1` はOCR profileを起動しない。OCR有効再起動にはbase + OCR + local overrideの3 composeファイルを使用する。


## 2026-09-28 追加APIアップデート本番反映

- 入力ZIP: LOCALSAPORTER_deploy_20260927-2.zip
- ZIP SHA-256: 949570B9186728BD490D6CB544A9942488B20A3CEC64A010F860CA8C5A192F3E
- 本番反映: agent-examplesアーカイブAPI、成功事例の経験RAG取り込みAPI
- 安全補正: 経験RAG取り込みに confirm_rag=true を必須化。未指定は RAG_NOT_CONFIRMED (HTTP 409)。直接関数呼び出しも拒否。
- 非採用差分: 	ests/test_triz_import.py と 	ests/test_triz_vehicle.py の145件前提変更。ZIPに対応入力がなく、現行入力74件と不整合のため反映前版を維持。
- ロールバック: ackups/pre_update_agent_api_20260928-015912
- Web image: $image
- 追加API対象テスト: 3 passed, 25 deselected
- GitHub Actions: 851 passed, 1 skipped (https://github.com/Nemosoft1963/sebas/actions/runs/36335517379)
- GitHub commit: 01ba2418e4a4b40791d3c931e122f40209e5b1b
- 反映後 healthcheck: PASS
- OCRオーバーレイと既定安全ゲートを維持。

## 2026-09-28 成功事例RAG取込UIアップデート

- 更新元: `LOCALSAPORTER_deploy_20260928.zip`
- ZIP SHA-256: `B3A81891DD5A7EF0DB342F243090198E92FF186FC2728F006C874EC742D1AD15`
- 反映内容: 成功事例JSONのプレビュー、回答者名、明示確認チェック、経験RAG登録結果表示
- 安全補正: サーバー側の `confirm_rag=true` 必須判定を維持。更新ZIP内の削除変更は不採用
- 除外: TRIZ 145件期待値への変更。同梱データは74件のため不採用
- バックアップ: `backups/pre_update_experience_ui_20260928-100323`
- Docker image: `sha256:fb0b3ec756e8ae58f3b7720ce74156233409c9fcc419f70922fbff3625047ccb`
- 事前 healthcheck: PASS
- 事後 healthcheck: PASS
- 関連テスト: 19 passed, 1 skipped
- HTTP確認: UI JavaScript 200、未承認登録 409
- 事例データの取得・登録、経験RAG設定変更は未実施
- GitHub commit: `ac8a73b7af76949a9e2d1d95a686c4c0a9babf71`
- GitHub Actions: PASS (`856 passed, 1 skipped`; run 36364772768)

## 2026-09-29 全体状況スナップショット

- `scripts/healthcheck.ps1`: 全項目PASS
- 稼働確認: web、Open WebUI、Computer、Google Publisher Browser、PaddleOCR document parser、PaddleOCR VLM、Ollama
- 設定モデル: `mistral-small3.2:24b-instruct-2506-q4_K_M`
- 本番ルートはGit管理外。GitHub公開版とのファイル一致は未証明
- 実案件の変更・受入試験・デプロイは未実施
- 全体状況と再開手順: `docs/SEBAS_CURRENT_STATUS_2026-09-29.md`


## 2026-09-30 完走不能問題の修正

- 添付更新を隔離検証し、誤ったTRIZ件数・個人パス固定・compose全体ハッシュ固定を是正。
- P0-1〜P0-6、汎用Completion Gate、Ver.23回復プレビュー/APIを本番反映。
- 追加是正: 旧「計画草案の評価と改善提案」をVer.23実行タスクへ流用しない。
- テスト: 全体 910 passed、集中 54 passed。
- healthcheck: PASS。`/api/health`: ok。
- 本番確認: Ver.22維持、Ver.23候補13タスク、coverage PASS、旧評価タスク名0、saved=false。
- 未実施: Ver.23適用、計画承認、外部AI送信、営業実行、最終人間確認。
- 詳細: `docs/CHANGELOG_2026-09-30_completion_recovery.md`。

- 追加是正: 古いDockerfileによるセキュリティ回帰を除外。RAG依存導入後にpip/setuptoolsを削除する順序へ修正。最終イメージはpip/setuptools不在、msgpack 1.2.3。公開選択差分 911 passed。

## 2026-09-30 汎用計画の外部AI指摘反映経路

- `app/goal_review_queue.py`: 車両損益以外でも、秘密情報や原本本文を転記せず、工程数・成果物契約・依存関係・達成条件対応・承認点から公開用概要を生成するよう変更。
- `app/plan_feedback.py`: 汎用の全体計画でローカルLLMが `rebuild_vehicle` と誤分類した場合だけ、`rebuild_generic` へ安全に正規化。車両案件の保護と詳細計画の制約は維持。
- 回帰テスト: `tests/test_plan_feedback_p0_3.py tests/test_plan_feedback.py` = 24 passed。
- 構文検査: `app/goal_review_queue.py`, `app/plan_feedback.py`, 対象テスト = PASS。
- `scripts/healthcheck.ps1` = 全項目 PASS。Webコンテナは再ビルド・反映済み。
- 実案件「本システムを販売実行」: 外部AI指摘8件から汎用再構成案を生成（8件対応、blocker 0）し、新計画版へ反映。署名 `ee4143cb14692a95fa3554b1b778d93d16046d1539b7c7d589ae2d50267e89e3`、状態 `revalidation_pending`。再外部検証は未実施。
- 再外部検証（署名 `ee4143...`）: Claude解析不能、ChatGPT 429、Gemini/Grokから計8件の構造化指摘、Meta 401。結果 `not_passed`。
- 取得した8件を汎用再構成案へ変換（`rebuild_generic` 8件、blocker 0）して反映。新署名 `cfba88dbb6b56b289d6bfc842ce6efe51bbef12712a47bfadb544da6f2eddc92`、状態 `revalidation_pending`。この新しい計画版の外部送信は未承認・未実施。

- 最新計画 cfba88db... の外部再検証: `not_passed`。Grokから6件の指摘。主因は公開概要が構造情報だけで目標本文を検証できないこと、全13工程がdocument_or_legacy、起点・最終工程のcriteria_count=0、実処理工程・実行主体・差戻し設計不足。Claude解析不能、ChatGPT 429、Gemini 503、Meta 401。計画は未合格のまま停止。


## 2026-09-30 完走経路・ローカルLLM切替修正

- モデル切替を「切替先の最小ウォームアップ（thinking無効・1 token）→設定保存→選択反映→旧モデル解放」の順へ変更。切替失敗時は旧選択と永続設定を保持。
- `qwen3.5:9b` の構造化JSON生成ではthinkingを無効化。推論だけで出力枠を消費し本文0文字になる現象を解消。
- 実機往復切替: `qwen3.5:9b → qwen3:8b → qwen3.5:9b` は両方向 `switched`。API・永続設定・Ollama実ロードが一致。
- 外部操作契約へ `execution_kind` を付与。最終検証は `final_verification` とし、全達成条件IDを割当。ただし達成条件保持検査では最終検証を実行工程の代用にしない。
- `criteria_count`、`document_or_legacy`、依存不足などの構造指摘を、説明追記や未解決開発ではなく `rebuild_generic` へ安全に分類。
- テスト: モデル切替・構造計画29 passed、汎用再構成11 passed、関連完了ゲート群も集中実行で既知回帰を修正。
- 実案件をVer.26へ反映。署名 `09ca77337bb35043e71fd85431d110dfa836f17387d384185981e41459ad3602`。13工程、SC10/SC11=`approved_external_action`、最終工程=`final_verification`、blocker 0、再外部検証待ち。

## 2026-09-30 Completion gates and local-model recovery

- Corrected the generic public review draft: approval count now includes actual external-action tasks only.
- Expanded the privacy-safe public structure with task role, input count, heading count, action kind, approval/evidence gates, human semantic confirmation, preparation exit checks, and estimated duration.
- Generic external actions now depend on all preceding plan outputs, so publication or outreach cannot run before preparation and content work.
- Preparation now has explicit exit checks. Final verification now requires human semantic confirmation and uses stop-and-report failure handling.
- Structural feedback classification now includes public-goal visibility, goal alignment, approval boundaries, final completion checks, and period metadata.
- Fixed validation order so a local-model whole-plan issue mislabeled as an amendment is normalized before amendment-target validation.
- Added one bounded recovery attempt only when all three normal attempts failed with the known target/change validation error.
- Rebuilt and deployed the web service.
- Rebuilt project 2a39815e16e4422581aa29b777a92910 from plan signature 09ca77337bb35043e71fd85431d110dfa836f17387d384185981e41459ad3602 to 40d57ddc8602d6eb700586c8f5a8d106691e1f08956dc707c0d0bea0ed396023.
- New plan: 13 tasks, 1 preparation task, 2 approved external-action tasks, 1 final-verification task, and 1 human semantic-confirmation gate.
- Local model selection and actual loaded model: qwen3.5:9b.
- Validation: 53 related regression tests passed; follow-up plan-feedback suite 13 passed; final healthcheck PASS.
- External revalidation is still pending. The new signature has not been sent to external AI.

Final regression rerun after all recovery changes: 54 passed in 17.69s. Maximum dependency count in the applied plan: 12.

## 2026-09-30 External revalidation of plan 40d57ddc

Result: not_passed.

Content findings:
- The privacy-safe packet is still too semantic-light for independent goal-alignment review.
- External action tasks need human semantic confirmation before execution, not only at final verification.
- Final verification incorrectly inherits an approved_external_action requirement.
- Ordinary execution tasks expose no exit checks or evidence requirement.
- Public structure needs criterion IDs, responsible role, source-reference count, completion-evidence type, and failure policy.
- Generic external actions need a safe operation-category code so an approver can understand the action class.

Provider outcomes:
- Claude: response received but parser marked it unverified.
- Grok: content findings, verdict unverifiable.
- ChatGPT: HTTP 429 insufficient quota.
- Gemini: HTTP 503 temporary high demand.
- Meta: HTTP 401 invalid API key.

No new plan was sent or applied after this result. A new packet requires a new signature-bound approval.

## 2026-09-30 Semantic completion gates, applied plan e51ba8b8

Implemented after external verdict not_passed:
- Every ordinary execution contract now requires evidence and three observable exit checks.
- Every external action requires human confirmation and semantic review before execution.
- Final verification no longer inherits an external-action requirement.
- Privacy-safe packets now expose SC criterion IDs, purpose codes, artifact categories, responsible roles, source-reference counts, completion-evidence types, and failure policies.
- Added safe business-purpose categories for market definition, service package design, commercial terms, sales assets, sales plan, prospect prioritization, outreach, pipeline tracking, delivery process, and execution status.
- Generic external actions are classified into publication, outbound communication, contract confirmation, or customer engagement.
- Applied project plan signature: e51ba8b8c2fb0d95250f446eaa0c37b08097ce8a2cf5d49c7f3cbc8bd2ae7e72.
- Applied plan has 13 evidence-required tasks, 39 exit checks, 2 approved_customer_engagement tasks, 3 human semantic-confirmation gates, and a final verifier covering SC01-SC11 with no action kind.
- Final related regression run: 57 passed in 17.95s.
- scripts/healthcheck.ps1: PASS.
- Selected and loaded local model: qwen3.5:9b.
- External revalidation of this new signature is pending signature-bound approval.

## 2026-10-01 Additional-requirement recovery and 20-task plan

External review of e51ba8b8 returned not_passed. The reviewers confirmed the new evidence and approval fields but could not verify goal alignment because the seven leading additional requirements were missing from the SC set.

Root cause:
- extract_criteria read only the later Markdown Achievement Criteria section.
- The numbered Additional Requirements block was ignored.
- rebuild_generic preferred an older 11-criterion GoalContract over the newer mission text.

Implemented:
- Extract numbered Additional Requirements before the main Achievement Criteria.
- Merge 7 additional requirements + 10 business criteria + 1 execution success criterion, within the 18-criterion limit.
- Prefer the newer mission criterion set when it is more complete than the saved GoalContract.
- Do not reuse an old SC task when its criterion text no longer matches.
- Add semantic dependencies among market, package, terms, assets, sales plan, prospect, outreach, pipeline, and delivery tasks.
- Separate external-control requirements from executable external actions.
- Add safe public purpose summaries and a purpose catalog without source text or private identifiers.
- Expand deterministic structural-feedback normalization while preserving business-fact blockers.

Applied plan:
- Signature: 560fbb38c8aa2e38aea899ed406221974f9089ceed7521a7bb560bff1a6811f8
- 20 tasks: preparation + 18 criteria + final verification.
- 21 artifact contracts, 71 dependency edges, 2 approved external-action tasks, 3 human semantic gates.
- All 20 tasks require evidence and expose 60 exit checks.
- External review packet preview: docs/EXTERNAL_REVIEW_PACKET_PREVIEW_2026-10-01.md.

Validation:
- Final related regression run: 62 passed.
- scripts/healthcheck.ps1: PASS.
- External revalidation for this packet is pending new signature-bound approval.

## 2026-10-01 External revalidation of plan 560fbb38

Result: not_passed.

The semantic purpose catalog and 18-criterion coverage were transmitted successfully. Grok identified remaining execution-order and granularity problems:
- Campaign publication precedes market, package, commercial-term, and sales-asset completion.
- Publication, posting-kit generation, approval, manual posting, response synchronization, and lead evaluation are grouped into one task.
- Sales assets, pricing, and outreach copy lack an earlier semantic approval gate.
- The final approved customer-engagement task depends on every prior task and is over-serialized.
- SC17 still uses the generic criterion_delivery category.
- Preparation source references are visible but are not counted as task inputs.

Provider availability:
- Claude: HTTP 400, credit balance too low.
- ChatGPT: HTTP 429, insufficient quota.
- Gemini: HTTP 503, temporary high demand.
- Meta: HTTP 401, invalid API key.
- Grok: content review returned, verdict unverifiable.

The execution gate remains closed. No task execution was started.

## 2026-10-01 Campaign execution order and semantic approvals

Implemented from the external findings for plan 560fbb38:
- Campaign execution is now one governed sequence with seven explicit action records: Google Sites publication, posting-kit generation, copy approval, manual social posting, post-URL registration, form-response synchronization, and lead evaluation.
- Campaign execution is placed after failure policy, approval boundaries, market definition, service packages, commercial terms, sales assets, and outreach content.
- Commercial terms, sales assets, and outreach content require human semantic confirmation before use.
- Approved customer engagement depends only on approval boundaries, prospect prioritization, outreach content, pipeline tracking, and the campaign sequence.
- SC17 is classified as execution_status and consumes campaign, engagement, and pipeline evidence.
- Preparation counts registered sources as inputs in the public packet; final verification inherits source references.

Applied project plan:
- Previous signature: 560fbb38c8aa2e38aea899ed406221974f9089ceed7521a7bb560bff1a6811f8
- Current signature: a843657a61106de63d124429342fe33f64775916144223cf08af0fb31166cf0b
- 20 tasks, 21 artifact contracts, 63 dependency edges, 5 approval-gated steps, and 6 semantic-review steps.
- Campaign is step 17 with 7 ordered action kinds; customer engagement is step 18 with 5 selected parents.
- Preparation input_count is 2; final source_reference_count is 2.

Validation:
- 66 related regression tests passed.
- scripts/healthcheck.ps1: PASS.
- Healthcheck reports configured Ollama model qwen3:8b; qwen3.5:9b and gpt-oss:20b are available in the local model list.
- New privacy-safe packet: docs/EXTERNAL_REVIEW_PACKET_PREVIEW_2026-10-01_a843657a.md.
- External revalidation has not been sent. The new signature requires fresh signature-bound approval.
## 2026-10-01 GitHub CI completion recovery

- Cause: purpose-priority sorting could move a task before its declared dependency, making project-wide plans fail validation.
- Fix: replaced the flat sort with dependency-aware priority ordering in app/structured_planning.py.
- Updated the structured-output compatibility test to match the implemented `think=false` behavior.
- Updated docker/Dockerfile to apply Debian security upgrades during image build.
- Focused regression suite: 70 passed, 1 warning.
- Rebuilt the web image and verified libssl3t64, openssl, and openssl-provider-legacy at 3.5.7-1~deb13u3.
- scripts/healthcheck.ps1: PASS for Ollama, integrated front, Open WebUI, browser services, containers, ports, and configured models.
- Remaining validation: GitHub Actions pytest, CodeQL, and container scan after push.
- GitHub commit ddd721f218f2af340d01101e40f6e932747b0571 pushed to main.
- GitHub Actions final result: test=success; security=success; Dependabot docker update workflow=success.
- The CI failure that prompted this repair is resolved.

## 2026-10-01 ChromaDB security replacement

- Replaced ChromaDB/LangChain vector storage with a project-scoped SQLite cosine index.
- ExperienceStore remains authoritative; approval, revocation, expiry, evidence checks, and project separation are unchanged.
- Removed vulnerable chromadb 1.5.9 and its vector integration dependencies from package and container definitions.
- Related validation: 33 passed, 1 deprecation warning.
- Rebuilt production web container: chromadb_installed=False; LocalVectorIndex import succeeded.
- scripts/healthcheck.ps1: PASS.
- GitHub commit d2064e6d66b6ce056d9db825076c207313d974c1 pushed to main.
- GitHub Actions: pytest=success; dependency graph update=success; CodeQL/container security workflow=success.
- Dependabot open-alert count verified after graph refresh.
- User explicitly approved closing the four stale ChromaDB Dependabot alerts after verified removal.
- Alerts #1-#4 dismissed with remediation evidence referencing commit d2064e6.
- Dependabot open-alert count after closure: 0.
- Correction: GitHub had already auto-closed alerts #1-#4 as fixed after dependency graph refresh. Manual dismissal returned HTTP 409 (fixed alerts cannot be dismissed); no dismissal state was applied.
- Verified final Dependabot open-alert count: 0.

## 2026-10-01 dependency maintenance

- Merged Dependabot PR #3 (sounddevice >=0.5.6) and #7 (setuptools >=84.0.0).
- PR #4 and #6 conflicted after sequential pyproject updates; applied their reviewed requirements together on current main: python-multipart >=0.0.32 and numpy >=2.4.6.
- Focused regression suite: 72 passed, 1 deprecation warning.
- Pytest 9 and Python 3.14 updates remain intentionally unmerged pending compatibility work.
- Dependency update commit 017958e1fb6d7d023a8390d9915ef78db4d99460 passed GitHub pytest and security workflows.

## 2026-10-01 pytest 9 and GitHub Actions maintenance

- Updated actions/checkout and actions/setup-python to v7; GitHub pytest succeeded.
- Updated pytest to >=9.1.1,<10 and pytest-asyncio to >=1.4,<2.
- Fixed a Windows-only test handle leak by explicitly closing the SQLite setup connection.
- Full isolated pytest 9 validation: 931 passed, 1 skipped, 1 deprecation warning.
- Python 3.14 container update remains rejected because project metadata explicitly supports Python <3.14; runtime qualification is required first.
- GitHub Actions v7 commit 9e50864 passed pytest; superseded PRs #1 and #2 were resolved.
- Pytest 9 compatibility commit 3ee365d passed GitHub pytest and security workflows.
- Python 3.14 PR #8 closed with compatibility rationale; pytest PR #5 closed as superseded by the coordinated fix.
- Final open pull request count: 0.
- Correction/finalization: PR #2 was still open after the combined Actions update; closed as superseded by commit 9e50864 after successful CI.
- Verified final open pull request count: 0.

## 2026-10-01 Sales project completion recovery

- Fixed external review JSON parsing for fenced text and multiple JSON objects.
- Limited goal-review responses to six compact issues and raised only the goal-review output budget to 3200 tokens.
- Added mandatory human semantic confirmation for market definition, service packages, commercial terms, sales assets, sales plans, prospect prioritization, outreach content, and delivery process.
- Enforced market -> service package -> commercial terms semantic dependencies.
- Prevented historical mission instructions from expanding an already complete 18-criterion mission beyond the supported limit.
- Added final topological ordering and input-contract synchronization after all semantic and external-action dependencies are known; conflicting model-authored forward edges are removed in favor of backend domain order.
- Focused regression validation: 68 passed. Structured planning validation after final adjustment: 37 passed.
- Production web image rebuilt and container recreated. Health check passed before the final planning fixes; API remained healthy during Version 33 generation.
- Sales project `2a39815e16e4422581aa29b777a92910` regenerated successfully as plan Version 33 with 20 tasks.
- External review parsing now works: Claude=conditional, ChatGPT=unverifiable, Grok=fail. Gemini remains HTTP 503 and Meta remains HTTP 401 invalid_api_key. Policy remains 5/5; it was not weakened.
- Remaining plan blockers: explicit OAuth reauthorization stop gate, existing-site evidence verification instead of republication, lead-count gate before prospect evaluation, and per-action approval/evidence within campaign execution. No plan approval or execution start was performed.

## 2026-10-01 MMI production update

- Reviewed the MMI ZIP and deployment guide. Adopted UI-only files and the presenter static route to preserve newer model-switching, review parsing, and planning fixes.
- Project bar, five workflow tabs, overview guidance, and readable result presenters deployed to `local-voice-ai-web`; current container is healthy.
- MMI tests: 31 passed; combined MMI/external-review UI tests after fixing initial expansion and button recovery: 41 passed. JavaScript syntax and presenter data-shape checks passed.
- Broader local pytest: 817 passed, 2 skipped, 19 failed; 18 failures were caused by missing `openpyxl`/`pypdf` in the local `.venv`, and the one UI regression was fixed and rechecked in the 41-test suite. This is not a full-suite pass.
- `scripts/healthcheck.ps1`: all PASS. HTTP health: ok; MMI presenter route: 200. Browser visual check unavailable due Windows sandbox startup error.
- Full deployment and rollback details: `docs/CHANGELOG_2026-10-01_mmi_deployment.md`.

## 2026-10-02 GitHub sync and work memory

- Confirmed `origin/main` at MMI commit `68c0a1122662767d0a0ac875e989cbe1ad9cdb77`; GitHub Actions `test` and `security` both succeeded.
- Rechecked production: Web container healthy and `scripts/healthcheck.ps1` all PASS.
- Sales project remains plan Version 33 with 20 tasks; `plan_issues_open`, external review `not_passed` (0/5). No approval or execution completion is claimed.
- Updated the MMI design status and recorded restart guidance in `docs/SEBAS_WORK_MEMORY.md`.
