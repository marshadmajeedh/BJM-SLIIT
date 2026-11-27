# ChangeGuard AI — P0-7 Baseline Verification Integration Infrastructure (M02)

> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `feature/m02-baseline-verification-integration`  
> **Scope:** Baseline-versus-modified verification orchestration, isolated baseline workspace preparation, identical approved test execution, deterministic regression comparison engine, report/API contracts, P0-6 rollback hardening, and offline end-to-end demonstration.  
> **Teammate Boundaries Respected:** Zero modifications to `project/agents/`, `project/nafris_verification/`, or `frontend/`. Production A4, automated repair loops, and comparative AI benchmarking remain out of scope.

---

## 1. Architecture Overview

P0-7 extends the M02 verification pipeline so that after Human Review Gate 1 approves exact test scenarios, ChangeGuard AI executes the **exact same SHA-256-verified test files** against two separate isolated Git working copies:

1. **Baseline workspace** checked out at the exact resolved original Git base commit (`resolved_base_commit`).
2. **Modified workspace** containing either the tracked existing change (`verify_existing_change`) or the validated and applied patch (`generate_and_verify_change`).

```text
[AWAITING_TEST_REVIEW] Human Review Gate 1
   ├── (approved=False) -> REJECTED (zero export, zero baseline prep, zero test execution)
   └── (approved=True)  -> Export ONLY selected scenarios + record SHA-256 hashes
   │
   ▼
[PREPARING_BASELINE]
   ├── Re-verify modified workspace source snapshot & Git diff SHA-256
   ├── WorkspaceManager.prepare_baseline_workspace(...) -> separate clone at resolved_base_commit
   ├── Compute baseline_source_snapshot_sha256 (excludes .git, caches, generated tests)
   ├── mirror_approved_tests_to_baseline(...) -> copy exact bytes, verify approved == modified == baseline SHA-256
   └── If baseline commit/clone unavailable -> preserve modified verification (transition -> RUNNING_MODIFIED_VERIFICATION, comparison=incomplete)
   │
   ▼
[RUNNING_BASELINE_VERIFICATION]
   ├── Re-verify baseline_source_snapshot_sha256 right before execution (tamper -> FAILED: BASELINE_WORKSPACE_TAMPERED)
   ├── Execute identical approved scenarios via M04 VerificationAdapter (per scenario file)
   └── Execute discovered pre-existing repository regression tests (tests/test_*.py) separately
   │
   ▼
[RUNNING_MODIFIED_VERIFICATION]
   ├── Re-verify modified source_snapshot_sha256 & git_diff_sha256 right before execution
   ├── Execute identical approved scenarios via M04 VerificationAdapter (per scenario file)
   └── Execute discovered pre-existing repository regression tests separately
   │
   ▼
[COMPARING_VERIFICATION_RESULTS]
   ├── BaselineComparisonEngine.compare(...) -> deterministic per-test & aggregate comparison
   ├── Verify SHA-256 test hash parity, execution contexts, collection errors, and test_purpose
   └── Clean up temporary baseline workspace directory safely (finally block)
   │
   ▼
[COMPILING_REPORT]
   ├── ChangePackageReport + baseline/modified summaries + per-test comparisons
   ├── Requirement-level traceability enriched with baseline-vs-modified evidence
   └── Explicit distinction between ordinary human approval and complete_requirement_assurance
   │
   ▼
[AWAITING_FINAL_APPROVAL] Human Review Gate 2
   └── Ordinary approve blocked (HTTP 409) on verification failure, confirmed regression, or blocking ambiguity
```

---

## 2. Baseline & Modified Workspace Lifecycle

| Property | Modified Workspace | Baseline Workspace |
| :--- | :--- | :--- |
| **Prepared By** | `WorkspaceManager.prepare_workspace(...)` at run start | `WorkspaceManager.prepare_baseline_workspace(...)` after Gate 1 approval |
| **Location** | `<workspace_root>/<run_id>_<hex>/repo` | `<workspace_root>/<run_id>_baseline_<hex>/repo` |
| **Git State (`verify_existing_change`, `working_tree`)** | Detached `HEAD` + tracked working-tree modifications | Detached `resolved_base_commit` (`HEAD`) with zero working-tree overlays |
| **Git State (`verify_existing_change`, `committed_range`)** | Detached `HEAD` | Detached `resolved_base_commit` (e.g., `HEAD~1` or commit SHA) |
| **Git State (`generate_and_verify_change`)** | Detached `HEAD` + validated & applied patch | Detached `resolved_base_commit` (`HEAD`, matching `original_source_snapshot_sha256`) |
| **Git Hooks** | Disabled (`-c core.hooksPath=<empty_dir>`) | Disabled (`-c core.hooksPath=<empty_dir>`) |
| **Secret / Untracked Isolation** | Rejects untracked files under `source_path`; removes tracked `.env*` secrets | Zero untracked or working-tree overlays; removes tracked `.env*` secrets |
| **Test Files** | Exported once from Gate-1-approved scenarios into `.changeguard_generated_tests/` | Exact bytes mirrored from verified modified workspace files; SHA-256 verified identical |
| **Original Repo Impact** | Read-only clone (`--no-hardlinks`); original repo never modified | Read-only clone (`--no-hardlinks`); original repo & modified workspace never modified |
| **Cleanup** | Managed via `WorkspaceManager.cleanup_workspace` (strictly inside `workspace_root`) | Automatically deleted in `finally` block after comparison completes |

---

## 3. Baseline Comparison Schemas (`project/schemas/verification.py`)

All schemas enforce Pydantic v2 `ConfigDict(extra="forbid")`, lossless JSON roundtripping, and backward-compatible defaults on `WorkflowRunRecord`, `VerificationResult`, and `ChangePackageReport`. Existing `BaselineComparisonEvidence` is reused and extended in-place.

| Schema | Purpose | Key Fields & Truthfulness Invariants |
| :--- | :--- | :--- |
| `TestPurpose` | Explicit test intent | `new_requirement_acceptance`, `existing_behavior_preservation`, `existing_repository_regression`, `unknown`. Never invented when intent is unavailable. |
| `TestSuiteKind` | Suite separation | `generated_acceptance` vs `existing_regression`. |
| `IndividualTestOutcome` | Per-test outcome | `passed`, `failed`, `skipped`, `collection_error`, `timeout`, `execution_error`, `not_executed`, `unavailable`, `unknown`. |
| `TestComparisonClassification` | Deterministic comparison verdict | `preserved_behavior`, `newly_introduced_failure`, `newly_satisfied_acceptance_criterion`, `fixed_existing_failure`, `fail_to_pass_unclassified`, `persistent_failure`, `not_comparable`, `unknown`, `unavailable`. |
| `VerificationComparisonStatus` | Overall comparison status | `completed`, `incomplete`, `unavailable`. |
| `TestExecutionEvidence` | Single-snapshot item evidence | `test_identity`, `scenario_id`, `node_id`, `relative_path`, `suite_kind`, `test_purpose`, `criterion_ids`, `test_source_sha256`, `outcome`, `exit_code`, `total_tests`, `passed`, `failed`, `duration_seconds`, `collection_errors`, `failure_message`, `evidence_granularity`, `evidence_limitations`. |
| `SuiteExecutionSummary` | Existing repo regression suite | Separate execution counts, file SHA-256 hashes, failures, and `test_evidence` for tracked `tests/test_*.py` files. |
| `BaselineVerificationResult` | Baseline snapshot summary | `baseline_ref`, `baseline_commit`, `source_snapshot_sha256`, `approved_test_hashes`, aggregate counts, `test_evidence`, `existing_regression_suite`, `evidence_limitations`, `error`. |
| `ModifiedVerificationResult` | Modified snapshot summary | `verification_passed`, `source_snapshot_sha256`, `git_diff_sha256`, `approved_test_hashes`, aggregate counts, `test_evidence`, `existing_regression_suite`, `evidence_limitations`, `error`. |
| `TestComparisonResult` | Per-test comparison record | `test_identity`, hashes (`approved`/`baseline`/`modified`, `test_hashes_match`), `baseline_outcome`, `modified_outcome`, `classification`, `is_mandatory_regression`, `comparable`, `rationale`, `limitations`. Rejects collection errors classified as regressions or mismatched-hash passes/regressions. |
| `RegressionComparisonSummary` | Aggregate comparison summary | Counts and identity lists per classification, `has_confirmed_regression`, `blocks_ordinary_approval`, `regression_safety_established`, separate `generated_suite_aggregate` & `existing_regression_suite_aggregate`, `limitations`. Forbids `regression_safety_established=True` unless `comparison_status="completed"` with zero regressions and zero uncomparable/unknown/unavailable items. |
| `BaselineComparisonEvidence` | Extended backward-compatible envelope | Preserves `status`, `baseline_ref`, `baseline_passed`, `baseline_failed`, `modified_passed`, `modified_failed`, `newly_failing_tests`, `notes` alongside `baseline_commit`, snapshot hashes, `approved_test_hashes_verified`, `baseline_result`, `modified_result`, `test_comparisons`, `summary`, and `evidence_limitations`. |

---

## 4. Workflow State Transitions

### New Active States (`WorkflowState` & `ACTIVE_EXECUTION_STATES`)
1. `PREPARING_BASELINE`
2. `RUNNING_BASELINE_VERIFICATION`
3. `RUNNING_MODIFIED_VERIFICATION`
4. `COMPARING_VERIFICATION_RESULTS`

`RUNNING_VERIFICATION` is retained for backward compatibility with pre-P0-7 persisted runs and legacy transition paths.

| Current State | Permitted Target States |
| :--- | :--- |
| `AWAITING_TEST_REVIEW` | `PREPARING_BASELINE`, `RUNNING_VERIFICATION` *(legacy)*, `REJECTED`, `FAILED` |
| `PREPARING_BASELINE` | `RUNNING_BASELINE_VERIFICATION`, `RUNNING_MODIFIED_VERIFICATION` *(degraded when baseline unavailable)*, `FAILED` |
| `RUNNING_BASELINE_VERIFICATION` | `RUNNING_MODIFIED_VERIFICATION`, `FAILED` |
| `RUNNING_MODIFIED_VERIFICATION` | `COMPARING_VERIFICATION_RESULTS`, `FAILED` |
| `COMPARING_VERIFICATION_RESULTS` | `COMPILING_REPORT`, `FAILED` |
| `RUNNING_VERIFICATION` *(legacy)* | `PREPARING_BASELINE`, `COMPILING_REPORT`, `FAILED` |
| `COMPILING_REPORT` | `AWAITING_FINAL_APPROVAL`, `FAILED` |

**Restart Recovery:** All four new states belong to `ACTIVE_EXECUTION_STATES`. On backend restart, `OrchestratorService.recover_interrupted_runs()` marks any run interrupted in these stages as `FAILED` (`PROCESS_INTERRUPTED`) without replaying test execution or running unapproved tests. Paused `AWAITING_TEST_REVIEW` and `AWAITING_FINAL_APPROVAL` runs remain resumable.

---

## 5. Deterministic Regression Classification Rules

| Baseline Outcome | Modified Outcome | Conditions | Classification | Mandatory Regression? |
| :--- | :--- | :--- | :--- | :--- |
| `passed` | `passed` | Identical verified SHA-256 | `preserved_behavior` | No |
| `passed` | `failed` | Identical verified SHA-256 | `newly_introduced_failure` | **Yes** (blocks ordinary approval) |
| `failed` | `passed` | Identical SHA-256 & `test_purpose == new_requirement_acceptance` | `newly_satisfied_acceptance_criterion` | No |
| `failed` | `passed` | Identical SHA-256 & `test_purpose in {existing_repository_regression, existing_behavior_preservation}` | `fixed_existing_failure` | No |
| `failed` | `passed` | Identical SHA-256 & `test_purpose == unknown` | `fail_to_pass_unclassified` *(never auto-labeled repaired regression)* | No |
| `failed` | `failed` | Identical verified SHA-256 | `persistent_failure` | No |
| Any | Any | Collection error on either side | `not_comparable` *(never classified as application regression)* | No |
| Any | Any | Hash mismatch / missing hash / skipped | `not_comparable` | No |
| `unknown` / Any | Any / `unknown` | Aggregate-only M04 output lacking passed node IDs | `unknown` | No |
| Missing / `timeout` / `unavailable` / `not_executed` / `execution_error` | Any | Missing identity or interrupted execution | `unavailable` | No |

---

## 6. Actual M04 Contract Limitations & Required Teammate Extension (Nafris)

### Observed M04 Limitations (`project/nafris_verification/` — Unmodified)
Auditing `generated_test_runner.py` and `report_parser.py` confirms:
1. **`parse_report` returns failing node IDs only:** It iterates `report.get("tests", [])` and appends only tests where `test["outcome"] == "failed"`. Passed, skipped, xfailed, xpassed, and setup/teardown error test node IDs are **omitted**.
2. **No per-test durations:** Only suite-level `duration` is returned.
3. **Collection errors omitted from structured `failures`:** `parse_report` ignores pytest `collectors` entries; M02 detects collection errors from `exit_code == 2` and `stdout`/`stderr`.
4. **Temporary JSON report deleted immediately:** `generated_test_runner.py` writes `report.json` inside `tempfile.TemporaryDirectory()`, which is deleted before returning.

### How M02 Handles This Safely Today
- M02 executes each approved scenario file individually through `VerificationAdapter.verify(...)` so that an isolated scenario file with `exit_code == 0, total_tests >= 1, passed == total_tests, failed == 0` yields unambiguous scenario-level `PASSED` evidence (`evidence_granularity="single_scenario_file"`) while explicitly noting M04 omits passed node IDs.
- Whenever M04 returns an **aggregate multi-test batch** without per-test passing identities (e.g., `build_test_evidence_from_aggregate_result`), M02 **never infers passing tests by subtraction**; unattributed items remain `IndividualTestOutcome.UNKNOWN` and force `comparison_status = "incomplete"`.

### Required Future M04 Contract Extension (Handover for Nafris)
Without changing existing keys (`total_tests`, `passed`, `failed`, `exit_code`, `duration`, `failures`), `project/nafris_verification/report_parser.py` should add an itemized `tests` list and `collectors` errors from `pytest-json-report`:

```json
{
  "total_tests": 4,
  "passed": 3,
  "failed": 1,
  "skipped": 0,
  "errors": 0,
  "exit_code": 1,
  "duration": 0.24,
  "tests": [
    {
      "nodeid": ".changeguard_generated_tests/test_generated_001.py::test_premium_discount",
      "outcome": "passed",
      "duration": 0.012,
      "setup_outcome": "passed",
      "call_outcome": "passed",
      "teardown_outcome": "passed"
    },
    {
      "nodeid": ".changeguard_generated_tests/test_generated_002.py::test_regular_unchanged",
      "outcome": "failed",
      "duration": 0.015,
      "longrepr": "AssertionError: assert 180.0 == 200.0"
    }
  ],
  "collection_errors": [
    {
      "nodeid": "tests/test_broken.py",
      "outcome": "failed",
      "longrepr": "ModuleNotFoundError: ..."
    }
  ]
}
```

Once Nafris emits itemized `tests` and `collection_errors`, M02 can upgrade `evidence_granularity` from `"single_scenario_file"` to `"per_test_node"` for multi-test files without breaking existing schemas or APIs.

---

## 7. Approval Rules & Ordinary Approval vs. Complete Requirement Assurance

### Gate 2 Approval Enforcement (`submit_final_decision` & `allowed_actions`)
Ordinary `approve` (`POST /api/runs/{run_id}/decision`) is **blocked with HTTP `409 Conflict`** whenever ANY of the following holds:
1. Modified verification did not pass (`not verification.verification_passed`) -> `VERIFICATION_APPROVAL_BLOCKED` (includes `has_confirmed_regression` & `newly_introduced_failures` in `details` when regressions occurred).
2. Baseline-vs-modified comparison confirmed a newly introduced mandatory regression (`blocks_ordinary_approval == True`, including regressions caught by the separate pre-existing repository test suite even when generated tests passed!) -> `REGRESSION_APPROVAL_BLOCKED`.
3. Mandatory unresolved requirement ambiguities remain open -> `UNRESOLVED_REQUIREMENT_AMBIGUITY`.
4. A `generate_and_verify_change` run lacks confirmed isolated-workspace patch application -> `PATCH_APPLICATION_REQUIRED`.

### Ordinary Human Approval vs. Complete Requirement Assurance
- **Ordinary Human Approval (`WorkflowState.APPROVED`):** Records a human reviewer's sign-off on the compiled evidence package when modified verification passed and zero regressions or blocking ambiguities were detected. It **never** commits, merges, pushes, or deploys.
- **Complete Requirement Assurance (`report.complete_requirement_assurance` / `traceability.requirement_verification_proven`):** Remains `False` unless **all** of the following are true simultaneously:
  1. All mandatory acceptance criteria have explicit scenario mappings, human approval, passing execution, and zero unresolved mandatory ambiguities (`mandatory_criteria_coverage_complete == True`).
  2. Modified verification passed cleanly (`verification_passed == True`).
  3. Baseline comparison finished with `comparison_status == "completed"`, `regression_safety_established == True`, and zero confirmed regressions.
  4. All upstream evidence (A1, M03, and A4 if used) is non-mock and non-deterministic-fixture.

---

## 8. Security Boundaries & P0-6 Rollback Hardening

1. **Git workspace isolation is NOT an OS security sandbox:** Separate baseline and modified working copies under `config.workspace_root` prevent mutation of the original repository and cross-contamination between baseline and modified runs, but they do not provide container, namespace, seccomp, or VM process isolation. Pytest executes strictly on server-configured `trusted_execution_roots` (`allow_untrusted_execution=False`).
2. **Pre-execution tamper guards:** Both `baseline_source_snapshot_sha256` and modified `source_snapshot_sha256` + `git_diff_sha256` are re-verified right before test execution (`BASELINE_WORKSPACE_TAMPERED`, `SOURCE_SNAPSHOT_MODIFIED`, `GIT_DIFF_MODIFIED`). Approved test SHA-256 digests are verified across Gate 1 approval, baseline mirroring, and M04 execution (`TEST_CONTENT_MODIFIED`).
3. **No raw host path or secret leakage:** `workspace_path` and `baseline_workspace_path` are excluded from API DTOs; all baseline/modified/comparison errors, failure evidence, notes, and limitations pass through `redact_secrets` before SQLite persistence and API serialization.
4. **P0-6 Rollback Defect Fixes (Phase 1 Audit):**
   - `PatchApplicator._rollback_files` now checks the return code of `git reset --quiet HEAD -- <added_paths>`, falls back to `git rm --cached --force --ignore-unmatch`, confirms via `git ls-files` that zero stale intent-to-add index entries remain, and prunes empty newly created subdirectories inside the workspace.
   - `PatchApplicator._rollback_and_verify_clean` verifies **both** `source_snapshot_sha256` and `git_diff_sha256` after rollback across all failure/timeout/no-op paths rather than checking only the file snapshot or hardcoding `workspace_clean_after_rollback=True`.

---

## 9. Frontend Integration Changes (`frontend/`, Owner: Nafris)

1. **Active Polling States:** Add `PREPARING_BASELINE`, `RUNNING_BASELINE_VERIFICATION`, `RUNNING_MODIFIED_VERIFICATION`, and `COMPARING_VERIFICATION_RESULTS` to the polling state set.
2. **Baseline vs. Modified Comparison Panel:** Render `baseline_verification`, `modified_verification`, and `baseline_comparison` (including `status`, `approved_test_hashes_verified`, `summary` counts, `test_comparisons` table with `test_purpose`, `suite_kind`, `baseline_outcome`, `modified_outcome`, `classification`, and `evidence_limitations`).
3. **Regression & Assurance Badges:** Display prominent warning/block indicators when `baseline_comparison.summary.has_confirmed_regression === true`, distinguish `complete_requirement_assurance === false` from ordinary human approval, and honor `allowed_actions` (which removes `submit_final_decision:approve` when `REGRESSION_APPROVAL_BLOCKED` applies).

---

## 10. Offline End-to-End Engineering Demonstration Results (Phase 9)

> **Honesty Disclosure:** The demonstration below uses a **deterministic offline test fixture** (`tests/test_m02_baseline_verification_integration.py::test_phase9_offline_e2e_premium_discount_demo_correct_vs_regressive`) executing real local `pytest` through unmodified M04 (`run_reviewed_tests`) against isolated Git fixtures (`mini_pos`). It is an **engineering integration demonstration**, NOT a statistically significant AI benchmark or live LLM performance evaluation.

### Fixture Setup
- **Requirement:** *"Apply a 10% discount to premium customers only, without changing regular customer checkout or breaking dependent refund, dashboard, and report modules."*
- **Baseline commit:** `billing.apply_discount(total, customer_type)` returns `total` unchanged.
- **Implementation A (Correct):** Applies 10% discount (`total * 0.90`) only when `customer_type == 'premium'`.
- **Implementation B (Incorrect / Regressive):** Applies `total * 0.90` unconditionally to every customer.
- **Common Deterministic Suite (identical SHA-256 across baseline and modified):**
  - `SCN-001` (`AC-001`, `new_requirement_acceptance`): Premium checkout receives 10% discount.
  - `SCN-002` (`AC-002`, `existing_behavior_preservation`): Regular customer checkout unchanged.
  - `SCN-003` (`AC-003`, `existing_behavior_preservation`): Dependent refund/dashboard/report preserve regular totals.
  - `SCN-004` (`AC-001`, `new_requirement_acceptance`): Dependent refund/report reflect 10% premium discount.

### Actual Observed Outcomes

| Scenario | Purpose | Baseline | Impl A (Correct) | Impl A Classification | Impl B (Regressive) | Impl B Classification |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `SCN-001` | `new_requirement_acceptance` | `failed` | `passed` | `newly_satisfied_acceptance_criterion` | `passed` | `newly_satisfied_acceptance_criterion` |
| `SCN-002` | `existing_behavior_preservation` | `passed` | `passed` | `preserved_behavior` | `failed` | **`newly_introduced_failure`** |
| `SCN-003` | `existing_behavior_preservation` | `passed` | `passed` | `preserved_behavior` | `failed` | **`newly_introduced_failure`** |
| `SCN-004` | `new_requirement_acceptance` | `failed` | `passed` | `newly_satisfied_acceptance_criterion` | `passed` | `newly_satisfied_acceptance_criterion` |

- **Implementation A Decision Outcome:** `comparison_status="completed"`, `baseline: 2 passed / 2 failed`, `modified: 4 passed / 0 failed`, `has_confirmed_regression=False`, `recommendation_label="ready_for_human_review"`, `complete_requirement_assurance=False` (offline fixture), Gate 2 `approve` permitted (`200 OK` -> `APPROVED`).
- **Implementation B Decision Outcome:** `comparison_status="completed"`, `baseline: 2 passed / 2 failed`, `modified: 2 passed / 2 failed`, `has_confirmed_regression=True` (`["SCN-002", "SCN-003"]`), `blocks_ordinary_approval=True`, `recommendation="reject"`, `recommendation_label="verification_failed"`, Gate 2 `approve` **blocked (`409 Conflict`)**, Gate 2 `reject` succeeded (`200 OK` -> `REJECTED`).

---

## 11. Reproducible Verification Instructions

Run the dedicated P0-7 Baseline Verification Integration test suite (100% offline, zero API keys required):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_m02_baseline_verification_integration.py -v --tb=short
```

Run the full repository test suite, dependency check, CI-equivalent Ruff checks, and diff hygiene checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -v --tb=short
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\ruff.exe check --select E9,F63,F7,F82,F811,F404,F406,F407,F601,F602 --exclude .git,.venv,venv,env,node_modules,__pycache__,.pytest_cache,build,dist,tests/fixtures/invalid .
git diff --check
```
