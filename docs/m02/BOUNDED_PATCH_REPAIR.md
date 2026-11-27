# ChangeGuard AI — P0-8 Bounded Automated Patch Repair Orchestration (M02)

> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `feature/m02-bounded-patch-repair`  
> **Scope:** Configurable bounded automated repair policy, conservative deterministic repair eligibility, A4 repair request contracts, immutable-original-snapshot workspace restoration, approved test preservation, complete append-only attempt history, SQLite persistence & restart safety, API/OpenAPI parity, and offline end-to-end demonstrations.  
> **Important Honesty Disclosure:** Production AI patch generation/repair intelligence (A4) is **not** implemented here. All demonstrations in this document execute real local `pytest` through unmodified M04 (`run_reviewed_tests`) against isolated `mini_pos` Git fixtures using **explicitly labeled offline deterministic patch and test fixtures** (`is_deterministic_fixture=True`, `is_mock_output=True`), NOT actual AI self-repair results.

---

## 1. Repair Architecture

P0-8 extends the `generate_and_verify_change` workflow with a strictly bounded, conservative automated patch repair loop positioned between initial baseline-versus-modified verification comparison (`COMPARING_VERIFICATION_RESULTS`) and final change-package compilation (`COMPILING_REPORT` -> `AWAITING_FINAL_APPROVAL`).

```text
[AWAITING_TEST_REVIEW] Human Review Gate 1 (mandatory before test export/execution)
   │  (approved=True) -> Export ONLY selected scenarios + record Gate 1 SHA-256 hashes
   ▼
[PREPARING_BASELINE] -> [RUNNING_BASELINE_VERIFICATION] -> [RUNNING_MODIFIED_VERIFICATION]
   │
   ▼
[COMPARING_VERIFICATION_RESULTS] (Attempt #1 initial baseline-vs-modified comparison)
   │  (verify_existing_change -> directly to COMPILING_REPORT; automatic repair disabled)
   ▼
[EVALUATING_REPAIR_ELIGIBILITY] (generate_and_verify_change)
   ├── Verification passed (0 regressions)   -> COMPILING_REPORT (not_needed / repair_succeeded)
   ├── Repair disabled / budget exhausted    -> COMPILING_REPORT (repair_disabled / budget_exhausted)
   ├── Non-application failure (collection,  -> COMPILING_REPORT (ineligible_failure)
   │   syntax, test defect, missing dep,
   │   infra error, incomplete baseline,
   │   ambiguity, auth/rate-limit)
   ├── No meaningful progress (same failure  -> COMPILING_REPORT (no_progress_detected)
   │   signature across distinct attempts)
   └── Eligible & budget remaining (<= 2 repairs / <= 3 total proposals):
       │
       ▼
   [REPAIRING_PATCH]
       ├── Restore clean isolated repair workspace from immutable original commit
       ├── Verify original_source_snapshot_sha256 & original_git_diff_sha256
       ├── Persist consumed repair budget BEFORE invoking A4 provider
       ├── Invoke injectable A4 PatchGenerationAdapter.generate(repair_request)
       ├── Provider unavailable / auth / rate-limit -> COMPILING_REPORT (provider_unavailable / provider_error)
       ├── Duplicate patch SHA-256 detected         -> COMPILING_REPORT (duplicate_patch_detected)
       └── Distinct replacement patch returned:
           │
           ▼
       [VALIDATING_PATCH] (against clean original snapshot; forbids test/regression modifications)
           ├── Attempted test/regression edit -> COMPILING_REPORT (test_modification_rejected)
           ├── Invalid/unapplicable patch     -> consumes budget -> EVALUATING_REPAIR_ELIGIBILITY / COMPILING_REPORT
           └── Validated patch:
               │
               ▼
           [APPLYING_PATCH] (against clean original snapshot; rollback-safe)
               │
               ▼
           [ANALYZING_POST_PATCH_IMPACT] (M01 impact + AST scope/assumption preservation check)
               ├── Scope expanded or imported test symbol removed -> COMPILING_REPORT (test_assumptions_invalidated)
               └── Mirror exact Gate-1-approved tests (SHA-256 verified)
                   ├── Validated baseline reusable -> RUNNING_MODIFIED_VERIFICATION
                   └── Baseline not reusable       -> PREPARING_BASELINE -> RUNNING_BASELINE_VERIFICATION -> RUNNING_MODIFIED_VERIFICATION
                       │
                       ▼
                   [COMPARING_VERIFICATION_RESULTS] -> Append PatchAttemptRecord -> EVALUATING_REPAIR_ELIGIBILITY
   │
   ▼
[COMPILING_REPORT] -> [AWAITING_FINAL_APPROVAL] Human Review Gate 2 (NEVER auto-approves/commits/merges/pushes/deploys)
```

### Core Components Added/Extended

| File | Responsibility |
| :--- | :--- |
| `project/schemas/repair.py` | Validated Pydantic v2 schemas: `RepairPolicy`, `RepairEligibilityCategory`, `RepairEligibilityResult`, `PatchAttemptRecord`, `RepairAttemptHistory`, `RepairTerminationReason`. |
| `project/schemas/patch.py` | Backward-compatible repair fields on `PatchGenerationRequest` (`attempt_type`, `repair_attempt_number`, `previous_proposal_id`, `previous_proposed_patch`, `previous_patch_sha256`, `prior_patch_hashes`, `failure_diagnostics`, `baseline_comparison_evidence`, `failed_criterion_ids`). |
| `project/schemas/workflow.py` & `agents.py` | New states (`EVALUATING_REPAIR_ELIGIBILITY`, `REPAIRING_PATCH`), optional `CreateRunRequest` overrides (`repair_enabled`, `max_repair_attempts`), persisted repair fields on `WorkflowRunRecord` and `ChangePackageReport`. |
| `project/marsadh_orchestrator/repair_engine.py` | `RepairEligibilityEvaluator`, `compute_failure_signature`, `check_approved_test_assumptions_preserved`, `build_repair_request_evidence`, `is_provider_auth_or_rate_limit_error`. |
| `project/marsadh_orchestrator/patch_engine.py` | `PatchValidator.validate(..., forbidden_paths=..., forbid_test_modifications=True)` rejecting edits to approved generated tests or established regression suites (`PATCH_TEST_MODIFICATION_FORBIDDEN`). |
| `project/marsadh_orchestrator/workspace.py` | `WorkspaceManager.prepare_clean_repair_workspace(...)` recreating a clean isolated workspace from `resolved_base_commit` per repair attempt. |
| `project/marsadh_orchestrator/orchestrator.py` | Bounded repair orchestration loop (`_run_bounded_repair_loop`), baseline reuse validation (`_is_baseline_reusable`), approved test hash verification, report enrichment, Gate 2 approval enforcement, and restart recovery. |
| `project/marsadh_orchestrator/store.py` & `project/api/schemas.py` | Secret redaction across all repair records before SQLite persistence and API serialization; safe dashboard DTOs (`PatchAttemptResponse`, `RepairAttemptHistoryResponse`). |

---

## 2. Repair Eligibility Rules (`RepairEligibilityEvaluator`)

Automated repair is permitted **only** when **all eight** conservative conditions hold simultaneously:

1. **Workflow mode:** `workflow_mode == "generate_and_verify_change"` (`verify_existing_change` is unconditionally ineligible via `RepairPolicy.allow_for_verify_existing_change: Literal[False] = False`).
2. **Repair enabled:** `policy.enabled is True` and `policy.effective_max_repair_attempts > 0`.
3. **Applied patch exists:** `patch_application is not None and patch_application.applied is True`.
4. **No blocking ambiguities:** `requirement_analysis` has zero unresolved mandatory or blocking ambiguities.
5. **Reliable application-patch failure attribution:** Baseline-vs-modified comparison completed cleanly (`comparison.status == "completed"`, `approved_test_hashes_verified == True`, zero `not_comparable`/`unknown`/`unavailable` items), and at least one comparable failing test is either:
   - a confirmed newly introduced regression (`TestComparisonClassification.NEWLY_INTRODUCED_FAILURE`: `baseline=PASSED -> modified=FAILED`, verified matching SHA-256), or
   - an unmet new-requirement acceptance test (`TestComparisonClassification.PERSISTENT_FAILURE` with `suite_kind == GENERATED_ACCEPTANCE` and `test_purpose == NEW_REQUIREMENT_ACCEPTANCE`).
6. **Approved test suite valid and unchanged:** Every Gate-1-approved test file matches its recorded SHA-256 digest.
7. **Workspace integrity verified:** Source snapshot and Git diff SHA-256 match recorded post-patch values.
8. **Repair budget available:** `repair_attempts_consumed < policy.effective_max_repair_attempts` and `next_attempt_number <= policy.max_total_proposals`.

### Failures That NEVER Trigger Automated Application Repair

| Failure Category | `RepairEligibilityCategory` | Behavior |
| :--- | :--- | :--- |
| Generated-test syntax, indentation, fixture, or test-body `NameError` defect | `test_defect_or_syntax_error` | Stops repair (`ineligible_failure`), 0 repair invocations |
| Pytest collection or import error (`exit_code == 2`, `ImportError`) | `collection_or_import_error` | Stops repair (`ineligible_failure`), 0 repair invocations |
| Missing external Python dependency (`ModuleNotFoundError`) | `missing_dependency` | Stops repair (`ineligible_failure`), 0 repair invocations |
| Provider authentication or rate-limit error (HTTP 401/403/429) | `provider_auth_or_rate_limit` | Stops repair (`provider_error`), no retries |
| Timeout, runner error, zero tests collected, or unexecuted verification | `verification_infrastructure_error` | Stops repair (`ineligible_failure`), 0 repair invocations |
| Unavailable/incomplete baseline or aggregate pass count `< 100%` alone | `unavailable_or_incomparable_evidence` | Stops repair (`ineligible_failure`), 0 repair invocations |
| Pre-existing repository failure (`baseline=FAILED, modified=FAILED`) | `persistent_preexisting_failure` | Stops repair (`ineligible_failure`), 0 repair invocations |
| Workspace or approved test hash tampering | `workspace_or_test_integrity_violation` | Fails run immediately (`FAILED`) |
| Unresolved mandatory business ambiguity | `unresolved_mandatory_ambiguity` | Stops repair (`ineligible_failure`), 0 repair invocations |

---

## 3. Maximum Attempt Semantics & Persistent Budget Enforcement

| Parameter | Default | Hard Bound | Env Variable |
| :--- | :--- | :--- | :--- |
| `auto_repair_enabled` | `True` | `bool` | `CHANGEGUARD_AUTO_REPAIR_ENABLED` |
| `max_repair_attempts` | `2` | `0..2` | `CHANGEGUARD_MAX_REPAIR_ATTEMPTS` |
| `max_total_proposals` | `3` (`1 + effective_max_repair_attempts`) | `1..3` | Derived & validated by `RepairPolicy` |

- **Initial generation** is always `attempt_number = 1`, `attempt_type = "initial"`, `repair_attempt_index = 0`, `consumed_repair_budget = False`.
- **Automated repairs** are `attempt_number = 2` (`repair_attempt_index = 1`) and `attempt_number = 3` (`repair_attempt_index = 2`).
- **Pre-invocation durable accounting:** Before invoking `PatchGenerationAdapter.generate(...)` for a repair attempt, the orchestrator atomically persists `repair_attempts_consumed += 1`, `total_proposals_attempted += 1`, and `state = REPAIRING_PATCH` in SQLite via `RunStore.update_run_atomic`.
- **No budget resets:** Invalid proposals, validation failures, application failures, duplicate patches, or unavailable providers still count against `repair_attempts_consumed` and never reset the counter.
- **No unbounded loops:** The orchestrator enforces both persisted budget checks and a structural loop cap (`effective_max_repair_attempts + 1`).

---

## 4. A4 Repair Request Contract

P0-8 reuses the existing `PatchGeneratorProvider` protocol (`generate_patch(request: PatchGenerationRequest) -> PatchGenerationResult | dict[str, Any]`) without breaking changes. For repair attempts (`attempt_type="repair"`), `PatchGenerationRequest` carries:

- `attempt_number` (`2` or `3`), `attempt_type="repair"`, `repair_attempt_number` (`1` or `2`).
- `original_requirement`, `acceptance_criteria`, `business_constraints`, `unresolved_ambiguities`.
- `repository_snapshot_ref`, `base_ref`, `resolved_base_commit`, `source_snapshot_sha256` (always pointing to the **immutable original snapshot**).
- `relevant_source_files` (bounded clean original source files under `source_path`, stripped before SQLite persistence and API serialization).
- `allowed_modification_paths` (excluding `.changeguard_generated_tests/` and repository test suites).
- `previous_proposal_id`, `previous_proposed_patch` (redacted unified diff), `previous_patch_sha256`, `prior_patch_hashes`.
- `failure_diagnostics` (sanitized actual M04 failure evidence), `baseline_comparison_evidence` (structured comparison summary + failing items), `failed_criterion_ids` (only explicitly mapped criterion IDs).

---

## 5. Immutable Original Snapshot Workspace Strategy

Rather than accumulating incremental diffs on top of a broken patch:

1. Prior attempt evidence is preserved permanently in `repair_history.attempts`.
2. `WorkspaceManager.prepare_clean_repair_workspace(...)` creates a fresh isolated working copy (`<workspace_root>/<run_id>_repair{N}_{hex}/repo`) checked out at `resolved_base_commit` with Git hooks disabled and tracked `.env*` secrets stripped.
3. Both `original_source_snapshot_sha256` and `original_git_diff_sha256` are re-verified on the clean workspace before validation and application.
4. `PatchValidator` validates the replacement diff against the immutable original snapshot and enforces allowed paths, protected paths, and `forbid_test_modifications=True`.
5. `PatchApplicator` applies only the exact SHA-256-verified diff and recomputes resulting snapshot and Git diff hashes.
6. **Duplicate & No-Progress Guards:**
   - Identical `patch_sha256` already present in `seen_patch_hashes` terminates immediately with `duplicate_patch_detected` before application.
   - Distinct patches producing an identical normalized `failure_signature` terminate immediately with `no_progress_detected`.
7. The original repository is never modified.

---

## 6. Approved Test Integrity & Baseline Reuse

- Gate-1-approved test files are mirrored into each clean repair workspace via `mirror_approved_tests_to_workspace(...)`, verifying triple SHA-256 parity (`approved_spec == source_workspace == repair_workspace`).
- Tests are **never** silently regenerated, removed, or weakened during repair.
- `PatchValidator` rejects any repair patch touching `.changeguard_generated_tests/`, `tests/`, `test_*.py`, `*_test.py`, `conftest.py`, or any discovered existing regression test file (`PATCH_TEST_MODIFICATION_FORBIDDEN` -> `test_modification_rejected`).
- **Scope / Assumption Check:** `check_approved_test_assumptions_preserved(...)` verifies via M01 diff scope and Python `ast` parsing that the replacement patch neither expands beyond Gate-1-analyzed files/impacted modules nor removes symbols imported by approved generated tests; otherwise repair stops with `test_assumptions_invalidated` and blocks ordinary approval until fresh human test review occurs.
- **Baseline Reuse Validation:** Prior `BaselineVerificationResult` is reused (`baseline_reused=True`) **only** when `policy.reuse_validated_baseline` is enabled AND `baseline_commit`, `original_source_snapshot_sha256`, `approved_test_hashes`, and discovered existing regression test file hashes are all verified identical. Otherwise baseline verification is re-executed cleanly under identical approved conditions.

---

## 7. Workflow State Transitions & Restart Recovery

### New Active States (`WorkflowState` & `ACTIVE_EXECUTION_STATES`)
- `EVALUATING_REPAIR_ELIGIBILITY`
- `REPAIRING_PATCH`

| Current State | Permitted Target States |
| :--- | :--- |
| `COMPARING_VERIFICATION_RESULTS` | `EVALUATING_REPAIR_ELIGIBILITY`, `COMPILING_REPORT`, `FAILED` |
| `EVALUATING_REPAIR_ELIGIBILITY` | `REPAIRING_PATCH`, `COMPILING_REPORT`, `FAILED` |
| `REPAIRING_PATCH` | `VALIDATING_PATCH`, `EVALUATING_REPAIR_ELIGIBILITY`, `COMPILING_REPORT`, `FAILED` |
| `VALIDATING_PATCH` | `APPLYING_PATCH`, `EVALUATING_REPAIR_ELIGIBILITY`, `COMPILING_REPORT`, `FAILED` |
| `APPLYING_PATCH` | `ANALYZING_POST_PATCH_IMPACT`, `EVALUATING_REPAIR_ELIGIBILITY`, `COMPILING_REPORT`, `FAILED` |
| `ANALYZING_POST_PATCH_IMPACT` | `GENERATING_TESTS`, `PREPARING_BASELINE`, `RUNNING_MODIFIED_VERIFICATION`, `EVALUATING_REPAIR_ELIGIBILITY`, `COMPILING_REPORT`, `FAILED` |

**Restart Recovery:** Both `EVALUATING_REPAIR_ELIGIBILITY` and `REPAIRING_PATCH` belong to `ACTIVE_EXECUTION_STATES`. On process restart, `OrchestratorService.recover_interrupted_runs()` transitions interrupted runs to `FAILED` (`PROCESS_INTERRUPTED`), records `repair_termination_reason = "process_interrupted"`, preserves persisted `repair_attempts_consumed` without resetting the budget, and **never** replays external A4 calls, patch applications, or test runs.

---

## 8. Attempt History Schemas (`project/schemas/repair.py`)

- `RepairPolicy`: bounded configuration (`enabled`, `max_repair_attempts`, `max_total_proposals`, `allow_for_verify_existing_change=False`, `patch_strategy="immutable_original_snapshot"`, duplicate/no-progress/test-integrity/baseline-reuse toggles).
- `RepairEligibilityResult`: `eligible`, `category`, `decision_reason`, `blocking_reasons`, `attributable_test_identities`, `failed_criterion_ids`, `confirmed_regression_identities`, `failure_signature`, `next_attempt_number`, `remaining_repair_budget`, `evaluated_at`.
- `PatchAttemptRecord`: complete immutable record per attempt (`attempt_number`, `attempt_type`, `repair_attempt_index`, `original_requirement`, `acceptance_criterion_ids`, `previous_proposal_id`, `previous_patch_sha256`, `base_snapshot_sha256`, `resulting_source_snapshot_sha256`, `resulting_git_diff_sha256`, `patch_generation`, `patch_validation`, `patch_application`, `approved_test_ids`, `approved_test_hashes`, `baseline_reused`, `baseline_verification`, `modified_verification`, `failure_diagnostics`, `failure_signature`, `baseline_comparison`, `repair_eligibility`, `provider_invoked`, `consumed_repair_budget`, `is_deterministic_fixture`, `is_mock_output`, `model_label`, `durations_ms`, `started_at`, `completed_at`, `outcome`, `errors`, `notes`).
- `RepairAttemptHistory`: append-only ledger (`policy`, `total_proposals_attempted`, `repair_attempts_consumed`, `current_attempt_number`, `remaining_repair_budget`, `attempts`, `seen_patch_hashes`, `seen_failure_signatures`, `last_eligibility`, `termination_reason`, `termination_summary`, `final_candidate_attempt_number`, `final_candidate_proposal_id`, `final_candidate_patch_sha256`).
- `RepairTerminationReason`: `not_needed`, `repair_disabled`, `workflow_mode_unsupported`, `repair_succeeded`, `budget_exhausted`, `ineligible_failure`, `duplicate_patch_detected`, `no_progress_detected`, `provider_unavailable`, `provider_error`, `patch_validation_failed`, `patch_application_failed`, `test_modification_rejected`, `test_assumptions_invalidated`, `workspace_integrity_error`, `process_interrupted`.

---

## 9. Actual Offline End-to-End Demonstration Results (Phase 11)

> **Honesty Disclosure:** All three demonstrations below executed real local `pytest` through unmodified M04 (`run_reviewed_tests`) against isolated `mini_pos` Git fixtures (`tests/test_m02_bounded_patch_repair.py`). Every patch and generated test was produced by **explicitly labeled deterministic offline fixtures** (`is_deterministic_fixture=True`, `is_mock_output=True`), NOT actual AI self-repair.

| Demonstration | Fixture / Scenario | Attempts Executed | Repair Budget Consumed | Termination Reason | Final Verification Outcome | Gate 2 Human Approval |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Demo A** (`test_demo_a_regressive_initial_patch_repaired_with_preserved_history_and_gate2`) | Attempt 1 applies 10% discount to *all* customers (`SCN-002` & `SCN-003` newly introduced regressions); Attempt 2 returns corrected premium-only diff against immutable original snapshot | `2` (initial + 1 repair) | `1 / 2` (`1` remaining) | `repair_succeeded` | `passed` (0 regressions, 2 preserved, 2 newly satisfied; `baseline_reused=True`; identical approved test SHA-256) | Paused at `AWAITING_FINAL_APPROVAL` (never auto-approved); `complete_requirement_assurance=False` (offline fixture); human `approve` succeeded (`200 OK` -> `APPROVED`); original repo unchanged |
| **Demo B** (`test_demo_b_two_failed_repairs_exhaust_budget_and_block_approval`) | Attempt 1 (`0.90` all), Attempt 2 (`0.85` all), Attempt 3 (`0.80` all) — distinct regressive diffs failing verification | `3` (initial + 2 repairs; **0** further calls) | `2 / 2` (`0` remaining) | `budget_exhausted` | `failed_tests` (confirmed regressions remain; all 3 attempts preserved) | Ordinary `approve` **blocked** (`409 Conflict`, `VERIFICATION_APPROVAL_BLOCKED`); human `needs_review` escalation succeeded (`200 OK` -> `NEEDS_REVIEW`) |
| **Demo C** (`test_demo_c_collection_or_import_error_does_not_trigger_repair`) | Generated test imports nonexistent external module (`nonexistent_demo_c_package_xyz`) | `1` (initial only; **0** repair calls) | `0 / 2` (`2` remaining) | `ineligible_failure` (`missing_dependency` / `collection_or_import_error`) | `collection_error` (`recommendation_label="verification_incomplete"`) | Ordinary `approve` **blocked** (`409 Conflict`) |

---

## 10. API Contract Changes

- **`GET /health`:** Added `auto_repair_enabled` (`bool`) and `max_repair_attempts` (`int`).
- **`POST /api/runs` (`CreateRunRequest`):** Added optional backward-compatible `repair_enabled` (`bool | null`) and `max_repair_attempts` (`0..2 | null`). Existing clients omitting these fields continue working identically.
- **`GET /api/runs` (`RunSummaryResponse`):** Added `repair_enabled`, `repair_attempts_consumed`, `repair_termination_reason`.
- **`GET /api/runs/{run_id}` (`RunDetailResponse`) & `ChangePackageResponse`:** Added `repair_enabled`, `max_repair_attempts`, `repair_attempts_consumed`, `remaining_repair_budget`, `repair_policy` (on detail), `repair_eligibility`, `repair_termination_reason`, `repair_history` (sanitized `RepairAttemptHistoryResponse` without host paths or raw source blobs), `final_candidate_proposal_id`, `final_candidate_patch_sha256`, `final_verification_outcome`, plus `complete_requirement_assurance`.
- **Active polling states:** Frontend clients should add `EVALUATING_REPAIR_ELIGIBILITY` and `REPAIRING_PATCH` to the polling state set.
- **Gate 2 `allowed_actions`:** Omits `submit_final_decision:approve` (and backend returns `409 Conflict` with `REPAIR_REVIEW_REQUIRED` / `VERIFICATION_APPROVAL_BLOCKED` / `REGRESSION_APPROVAL_BLOCKED`) whenever verification failed, confirmed regressions remain, mandatory ambiguities remain, or repair terminated due to `test_modification_rejected` or `test_assumptions_invalidated`.
- **OpenAPI Spec:** Regenerated and parity-tested in [`docs/m02/openapi.json`](./openapi.json) and updated in [`docs/m02/API_CONTRACT.md`](./API_CONTRACT.md).

---

## 11. Security Limitations

1. **Git workspace isolation is NOT an operating-system security sandbox:** Clean repair working copies prevent original-repository mutation and incremental patch contamination, but do not provide container, namespace, seccomp, or VM process isolation. Test execution remains restricted to server-configured `trusted_execution_roots` (`allow_untrusted_execution=False`).
2. **Secret redaction & zero host-path leakage:** All repair history fields, diagnostics, errors, eligibility reasons, and `previous_proposed_patch` pass through `redact_secrets` before SQLite persistence and API serialization; `relevant_source_files` is stripped before SQLite persistence; `workspace_path` and `baseline_workspace_path` are excluded from API DTOs.
3. **No automated Git mutation or deployment:** Even when bounded repair succeeds, M02 pauses at Gate 2 (`AWAITING_FINAL_APPROVAL`) and never automatically approves, commits, merges, pushes, or deploys code.
4. **Live mode honesty:** In `execution_mode="live_llm"`, missing production A4 fails explicitly with `PATCH_GENERATOR_UNAVAILABLE` and never falls back to offline fixture providers.

---

## 12. Production A4 Integration Instructions

Connect the future teammate-owned production A4 patch generation & repair agent by implementing `PatchGeneratorProvider` and inspecting `request.attempt_type`:

```python
from project.main import create_app
from project.marsadh_orchestrator import OrchestratorConfig
from project.schemas import PatchGenerationRequest, PatchGenerationResult


class ProductionA4PatchAgent:
    """Future teammate-owned A4 patch generation & bounded repair agent."""

    def generate_patch(self, request: PatchGenerationRequest) -> PatchGenerationResult:
        if request.attempt_type == "repair":
            # Generate a replacement unified diff against request.relevant_source_files
            # (the immutable original snapshot) using:
            # - request.previous_proposed_patch & request.previous_patch_sha256
            # - request.prior_patch_hashes (avoid duplicates)
            # - request.failure_diagnostics & request.baseline_comparison_evidence
            # - request.failed_criterion_ids
            # - request.original_requirement, acceptance_criteria, business_constraints
            # - request.allowed_modification_paths (never touch test suites)
            ...
        else:
            # Initial patch generation (attempt_number == 1)
            ...


app = create_app(
    OrchestratorConfig.from_env(),
    patch_production_provider=ProductionA4PatchAgent(),
)
```

---

## 13. Remaining Teammate Requirements

- **Production A4 Agent (Patch Generation & Repair):** Implement live LLM unified-diff generation and failure-guided repair conforming to `PatchGeneratorProvider`, setting `is_deterministic_fixture=False` and `is_mock_output=False` in `live_llm` mode.
- **M03 Testing Agent (Munshif):** Emit structured `criterion_ids` and explicit `test_purpose` per generated scenario so acceptance vs preservation attribution is automatic without fallback to `unknown`.
- **M04 Verification & Reporting (Nafris):** Extend `report_parser.py` to emit itemized per-node `tests` (including passed/skipped node IDs) and `collectors` errors so M02 can upgrade from single-scenario-file to per-test-node granularity.
- **Frontend Dashboard (`frontend/`, Nafris):** Poll `EVALUATING_REPAIR_ELIGIBILITY` and `REPAIRING_PATCH`, render `repair_history.attempts` timeline, budget indicator (`repair_attempts_consumed / max_repair_attempts`), `repair_termination_reason`, offline-fixture disclosures, and respect `allowed_actions`.
- **A6 Independent Adversarial Reviewer (Future Milestone):** Independent review stage before Gate 2.

---

## 14. Reproducibility Instructions

Run the dedicated P0-8 Bounded Automated Patch Repair test suite & Demonstrations A/B/C (100% offline, zero API keys required):

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_m02_bounded_patch_repair.py -v --tb=short
```

Run the complete repository verification checks (full pytest suite, dependency check, CI-equivalent Ruff lint, OpenAPI parity, and Git diff hygiene):

```powershell
.\.venv\Scripts\python.exe -m pytest -v --tb=short
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\ruff.exe check --select E9,F63,F7,F82,F811,F404,F406,F407,F601,F602 --exclude .git,.venv,venv,env,node_modules,__pycache__,.pytest_cache,build,dist,tests/fixtures/invalid .
git diff --check
```
