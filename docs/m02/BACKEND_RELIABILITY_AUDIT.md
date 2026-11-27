# ChangeGuard AI — M02 Backend Reliability Audit & Finalization (P0-9)

> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `feature/m02-independent-review-and-reliability`  
> **Audit Scope:** Source-level inspection of `project/schemas/`, `project/marsadh_orchestrator/`, `project/api/`, `project/main.py`, `docs/m02/`, and the original ChangeGuard AI proposal.  
> **Non-Modification Guarantee:** Teammate-owned directories `project/agents/`, `project/nafris_verification/`, and `frontend/` were inspected read-only and were **not** modified.

---

## 1. Executive Summary

A targeted source-level reliability audit of Module 02 was performed across six risk areas: (1) candidate evidence integrity, (2) repair eligibility attribution, (3) test manipulation and execution-config protection, (4) workspace isolation and cleanup integrity, (5) persistence, secret redaction, concurrency, and interruption recovery, and (6) API reliability and policy consistency.

**Eight confirmed reliability defects** were identified, fixed with minimal targeted changes preserving existing working behavior, and covered by deterministic regression tests. Six additional risk areas were inspected and verified to already behave safely. Remaining operational limitations (including the local unauthenticated API and lack of an OS sandbox) and deferred teammate integrations are documented explicitly below.

---

## 2. Confirmed Reliability Defects & Corrections Implemented

### Defect 1 — Missing Candidate Evidence Identity Verification at Gate 2 & Review
- **Location (Before):** `project/marsadh_orchestrator/orchestrator.py` (`_compile_change_package`, `submit_final_decision`), `project/api/schemas.py` (`_allowed_actions`).
- **Confirmed Risk:** Gate 2 approval and report compilation verified `verification.verification_passed` and `baseline_comparison.summary.has_confirmed_regression`, but did not cross-check that `modified_verification.source_snapshot_sha256` / `git_diff_sha256`, `baseline_comparison.modified_source_snapshot_sha256`, `patch_application.resulting_source_snapshot_sha256`, `repair_history.final_candidate_patch_sha256`, approved test hashes, and `independent_review.reviewed_candidate_*` all bound to the **current active candidate**. A stale passing result from an earlier attempt could theoretically authorize a later unverified candidate if persisted state diverged.
- **Correction Implemented:** Added `verify_candidate_evidence_integrity(run)` in `project/marsadh_orchestrator/review_reconciliation.py` and wired it into both `_execute_independent_review_and_reconciliation` (before A6 invocation) and `evaluate_approval_policy(run)` (blocking ordinary approval with `CANDIDATE_VERIFICATION_MISMATCH` or `STALE_REVIEW_CANDIDATE`).
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_candidate_verification_evidence_integrity_and_audit_fixes`.

### Defect 2 — Missing Post-Execution Approved Test Hash Re-Verification
- **Location (Before):** `project/marsadh_orchestrator/baseline_verification.py` (`execute_snapshot_verification`).
- **Confirmed Risk:** `execute_snapshot_verification` verified approved test file SHA-256 hashes before calling M04 `run_reviewed_tests`, and computed post-execution hashes afterward, but did not compare post-execution hashes against `spec.expected_sha256` to immediately abort on mid-execution self-modification or test-file tampering.
- **Correction Implemented:** Added post-execution SHA-256 comparison in `execute_snapshot_verification` returning a hard integrity error (`AgentError(code="TEST_CONTENT_MODIFIED")`) immediately if any approved test file is missing or altered during pytest execution.
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_candidate_verification_evidence_integrity_and_audit_fixes` (self-modifying test case).

### Defect 3 — Initial Patch Test-Manipulation & Execution-Config Protection Gap
- **Location (Before):** `project/marsadh_orchestrator/orchestrator.py` (initial patch validation), `project/marsadh_orchestrator/patch_engine.py` (`PatchValidator`).
- **Confirmed Risk:**
  1. Initial patch validation in `execute_analysis_and_generation` did not pass `forbid_test_modifications=True` or discovered existing regression test paths, so an initial A4 patch could alter existing test files before Gate 1.
  2. `PatchValidator` did not block pytest/import-hook execution configuration files (`pytest.ini`, `.pytest.ini`, `tox.ini`, `conftest.py`, `sitecustomize.py`, `usercustomize.py`, `*.pth`) from being added or modified.
- **Correction Implemented:**
  1. Extended `PatchValidator` protected filenames/extensions to block `pytest.ini`, `.pytest.ini`, `tox.ini`, `conftest.py`, `sitecustomize.py`, `usercustomize.py`, and `*.pth` files unconditionally (`PATCH_TEST_MODIFICATION_FORBIDDEN` / `PATCH_PROTECTED_PATH`).
  2. Passed `forbid_test_modifications=True` and discovered existing regression test paths (`_discover_existing_test_paths`) into initial patch validation in `orchestrator.py`.
- **Honesty Note:** Static path/diff checks harden against obvious test/config manipulation, **but static checks cannot guarantee safe execution of arbitrary Python code**.
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_candidate_verification_evidence_integrity_and_audit_fixes`.

### Defect 4 — Unjustified Repair Retries on Incomplete or Test-Assumption Acceptance Failures
- **Location (Before):** `project/marsadh_orchestrator/repair_engine.py` (`RepairEligibilityEvaluator.evaluate`).
- **Confirmed Risk:** A `PERSISTENT_FAILURE` on a `NEW_REQUIREMENT_ACCEPTANCE` test could trigger application patch repair even when failure diagnostics were empty/unattributable or when both baseline and modified snapshots failed with the identical non-assertion test-assumption error (e.g., `TypeError`, `AttributeError`, `NameError`, `UnboundLocalError` from a hallucinated function signature in a generated test).
- **Correction Implemented:** Hardened `RepairEligibilityEvaluator.evaluate` so `PERSISTENT_FAILURE` acceptance tests require explicit failure diagnostic attribution and `criterion_ids` (`UNATTRIBUTABLE_FAILURE` otherwise), and identical baseline/modified non-assertion test-assumption errors without an `AssertionError` are classified as `TEST_DEFECT_OR_SYNTAX_ERROR` instead of triggering application repair. The two-repair-attempt hard cap (`max_repair_attempts <= 2`, max 3 total proposals) is strictly preserved.
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_candidate_verification_evidence_integrity_and_audit_fixes`.

### Defect 5 — Temporary Workspace Symlink Check & Exception-Path Cleanup Leaks
- **Location (Before):** `project/marsadh_orchestrator/workspace.py` (`cleanup_workspace`), `project/marsadh_orchestrator/orchestrator.py` (`execute_analysis_and_generation`, `_run_bounded_repair_loop`).
- **Confirmed Risk:**
  1. `cleanup_workspace` checked path containment after resolving, but did not explicitly reject symlink `delete_root` targets before calling `shutil.rmtree`.
  2. Unexpected errors during `execute_analysis_and_generation` (after workspace creation but before persistence) or inside `_run_bounded_repair_loop` could leave temporary repair workspaces uncleaned on disk.
- **Correction Implemented:**
  1. Hardened `WorkspaceManager.cleanup_workspace` to explicitly check `is_symlink()` on both the target and derived `delete_root` and verify resolved `delete_root` remains strictly inside resolved `workspace_root` (raising `WorkspaceSecurityError` otherwise).
  2. Added defensive `cleanup_workspace` handling on failure paths in `execute_analysis_and_generation` and `_run_bounded_repair_loop` while preserving active workspaces required for Gate 1/Gate 2 reproducibility.
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_candidate_verification_evidence_integrity_and_audit_fixes`.

### Defect 6 — Interruption Recovery Overwriting Completed Repair Termination Reason
- **Location (Before):** `project/marsadh_orchestrator/orchestrator.py` (`recover_interrupted_runs`).
- **Confirmed Risk:** If the backend process crashed after bounded patch repair had already finished (e.g., during `RUNNING_INDEPENDENT_REVIEW`, `RECONCILING_REVIEW_EVIDENCE`, or `COMPILING_REPORT`), `recover_interrupted_runs()` unconditionally overwrote `repair_termination_reason` with `PROCESS_INTERRUPTED`, destroying the accurate `REPAIR_SUCCEEDED` / `BUDGET_EXHAUSTED` audit record.
- **Correction Implemented:** Updated `recover_interrupted_runs()` to set `repair_termination_reason = PROCESS_INTERRUPTED` only when `repair_termination_reason is None` and the run was interrupted in a repair-relevant state, while marking all active execution states (`RUNNING_INDEPENDENT_REVIEW`, `RECONCILING_REVIEW_EVIDENCE`, etc.) `FAILED` (`PROCESS_INTERRUPTED`) without replaying any provider or pytest calls.
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_persistence_legacy_compat_restart_recovery_secret_redaction_and_openapi`.

### Defect 7 — Incomplete Secret Redaction on `report.verification` & Unbounded Excerpt Persistence
- **Location (Before):** `project/marsadh_orchestrator/store.py` (`_sanitize_run_record`).
- **Confirmed Risk:** `_sanitize_run_record` sanitized `run.verification`, `run.baseline_verification`, and `run.modified_verification`, but did not apply `_sanitize_verification` to the nested copy in `run.report.verification` prior to SQLite persistence. Additionally, new `independent_review_request.relevant_source_excerpts` should not persist unrestricted raw source blobs into SQLite.
- **Correction Implemented:** Extended `_sanitize_run_record` in `store.py` to sanitize `run.report.verification`, `run.independent_review`, and `run.review_reconciliation`, and to strip `run.independent_review_request.relevant_source_excerpts = {}` before SQLite storage.
- **Regression Coverage:** `tests/test_m02_independent_review_and_reliability.py::test_persistence_legacy_compat_restart_recovery_secret_redaction_and_openapi`.

### Defect 8 — Duplicated Gate 2 Approval Enforcement Policy Across Orchestrator & API
- **Location (Before):** `project/marsadh_orchestrator/orchestrator.py` (`submit_final_decision`) and `project/api/schemas.py` (`_allowed_actions`).
- **Confirmed Risk:** Gate 2 approval rules (verification passed, confirmed regressions, unresolved ambiguities, repair stop reasons, missing patch evidence) were implemented separately in `orchestrator.py` and `api/schemas.py`, creating drift risk between advertised `allowed_actions` and enforced `409 Conflict` rejections.
- **Correction Implemented:** Unified all Gate 2 approval enforcement into a single deterministic function `evaluate_approval_policy(run)` in `project/marsadh_orchestrator/review_reconciliation.py`, called by both `OrchestratorService.submit_final_decision` and `project/api/schemas.py._allowed_actions`.
- **Regression Coverage:** Verified across Cases A, B, C, rejection, uncertainty, stale candidate, and legacy record tests in `tests/test_m02_independent_review_and_reliability.py`.

---

## 3. Inspected Risk Areas Confirmed Sound (No Defect Found)

| Area | Verification Outcome |
| :--- | :--- |
| **Atomic State Transitions & Concurrency** | `RunStore.compare_and_swap_state` uses SQLite `BEGIN IMMEDIATE` transactions with `WHERE run_id = ? AND state = ?`. Confirmed under concurrent 4-thread Gate 1 and Gate 2 submission races: exactly 1 request succeeds and 3 fail cleanly with `409 Conflict`. |
| **Terminal State Immutability** | `ALLOWED_TRANSITIONS` defines empty target sets for `COMPLETED_NO_CHANGES`, `APPROVED`, `REJECTED`, `NEEDS_REVIEW`, and `FAILED`; terminal runs cannot resume or transition unexpectedly. |
| **Original Repository Immutability** | All analysis, patching, verification, repair, and independent review run against isolated copies under `workspace_root`; `git rev-parse HEAD` and full file-tree byte snapshots remain identical before and after runs. |
| **Patch Rollback Integrity** | `PatchApplicator.rollback` verifies both restored source file snapshot SHA-256 and restored `git diff` / index state (`rollback_verified=True`). |
| **Repair Budget Persistence** | `repair_attempts_consumed` is persisted via `store.save_run` **before** invoking the A4 provider on every repair attempt, preventing budget bypass across crashes. |
| **Subprocess Execution & Redaction** | All Git and pytest invocations use argument lists (`shell=False`) with explicit timeouts; API responses omit host absolute paths (`workspace_path`, `repository_path`) and redact API keys/tokens. |

---

## 4. Remaining Limitations (Honest Disclosure)

1. **Local Unauthenticated API:** Module 02 exposes a local loopback-default FastAPI service with no authentication, session, or RBAC layer. `reviewer` fields are unauthenticated local metadata. Introducing an auth platform was intentionally excluded from M02 MVP scope; deployment beyond localhost requires a reverse proxy / authentication layer.
2. **No OS or Container Execution Sandbox:** Isolated Git workspaces prevent modifying the user's original repository, but they do **not** isolate OS, filesystem, or network syscalls during `pytest` execution. Therefore, execution remains strictly limited to server-configured `CHANGEGUARD_TRUSTED_EXECUTION_ROOTS`.
3. **Static Patch Checks Are Best-Effort:** Blocking protected paths, test files, and `pytest.ini`/`conftest.py`/`sitecustomize.py`/`*.pth` prevents obvious test/runner manipulation, but static diff inspection cannot guarantee safe execution of arbitrary Python code.
4. **In-Process Bounded Thread Pool:** Background workflow stages run in-process (`ThreadPoolExecutor`, not a durable broker/worker queue). Process restarts mark interrupted active runs as `FAILED` (`PROCESS_INTERRUPTED`) without replaying provider calls; paused Gate 1 / Gate 2 runs remain resumable.
5. **M04 Report Parser Granularity:** Unmodified `project/nafris_verification/report_parser.py` reports failing test diagnostics but omits individual passed pytest node IDs, coverage percentages, and AI root-cause analysis. M02 reports these gaps honestly (`coverage_status="unavailable"`, `ai_root_cause_status="unavailable"`).

---

## 5. Deferred Production Integrations (Outside M02 P0-9 Scope)

| Component | Current Status | Owner / Next Step |
| :--- | :--- | :--- |
| **A4 Production Patch Generator & Repair AI** | Injectable `PatchGenerationProvider` + bounded repair orchestration ready; `PATCH_GENERATOR_UNAVAILABLE` until connected. | Future A4 owner: inject `patch_production_provider` into `create_app`. |
| **A6 Production Independent Reviewer AI** | Injectable `IndependentReviewerProvider` + reconciliation ready; `availability="unavailable"` in live mode until connected. | Future A6 owner: inject `review_production_provider` into `create_app`. |
| **M03 Criterion ID Mapping** | M02 schemas & reconciliation support explicit `criterion_ids` on scenarios, but unmodified M03 does not emit them yet. | M03 owner: emit `criterion_ids` per generated scenario. |
| **M04 Itemized Pass Reporting & Coverage** | Baseline-vs-modified file-level comparison works; per-node pass reporting and coverage require M04 parser updates. | M04 owner (Nafris): extend `report_parser.py` and verification runner. |
| **React Dashboard UI (`frontend/`)** | OpenAPI contract (`docs/m02/openapi.json`) and `API_CONTRACT.md` updated for A1/A4/A6/repair/reconciliation. | Frontend owner (Nafris): consume updated endpoints & `allowed_actions`. |
