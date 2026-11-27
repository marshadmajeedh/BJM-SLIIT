# ChangeGuard AI — M02 Final Status & Architecture Audit (P0-10)

> **Project:** ChangeGuard AI | **Team:** BJM-SLIIT | **Competition:** Innodata AI Catalyst 2026 (University Track)  
> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `docs/m02-final-audit-and-handover` (`5e391d3`)  
> **Final M02 Verdict:** **`M02 BACKEND READY WITH CRITICAL LIMITATIONS.`**

---

## 1. Actual Repository State (Phase 1 Audit)

Inspected read-only without modifying Git history or teammate source code:

| Field | Observed Repository State |
| :--- | :--- |
| **Current Branch & `HEAD`** | `docs/m02-final-audit-and-handover` at `5e391d3d83d347c99a292020d7511ca19a85659e` (matches local `feat/marshad_ahamed`) |
| **Latest `origin/develop`** | `b4946ab` (*"Merge pull request #20 from marshadmajeedh/feature/mujahidh-codebase"*) — merged into `HEAD` |
| **Local `develop`** | `6831f32` (behind `origin/develop` by 24 commits; local ref not yet fast-forwarded) |
| **`origin/feat/marshad_ahamed`** | `e7dcb84` (*"Merge pull request #21 from marshadmajeedh/feature/m02-independent-review-and-reliability"*) — merged into `HEAD` |
| **`origin/main` / `main`** | `77d0dda` (*"Create team development folders"*) — **contains only initial empty scaffolds** |
| **Unmerged Local/Remote Branches vs. `HEAD`** | **None** (`git branch -a --no-merged HEAD` is empty; all fetched teammate and M02 feature branches are merged into `HEAD`) |
| **Branches Not Yet Merged into `origin/develop`** | `feat/marshad_ahamed` (`5e391d3`), `origin/feat/marshad_ahamed` (`e7dcb84`), `feature/m02-independent-review-and-reliability` (`88ca11d`), and `docs/m02-final-audit-and-handover` |
| **Working Tree Before P0-10 Docs** | Clean (`nothing to commit, working tree clean`; untracked gitignored runtime DB at `.changeguard/changeguard.sqlite3`) |
| **Dependencies (`project/requirements.txt`)** | 8 pinned packages: `groq>=1.7,<2`, `pytest>=8,<10`, `pytest-json-report>=1.5,<2`, `PyYAML>=6,<7`, `pydantic>=2,<3`, `fastapi>=0.115,<1`, `uvicorn>=0.32,<1`, `httpx>=0.27,<1`. (`pip check`: **No broken requirements found.** Missing `python-dotenv` & `pytest-cov` used only by standalone non-integrated M04 scripts.) |
| **CI Configuration (`.github/workflows/ci.yml`)** | `ChangeGuard CI` with 5 jobs: `python-validation` (Python 3.12, `pip check`, AST syntax, Ruff critical rules, `pytest`), `frontend-validation` (`not_applicable` until `frontend/package.json` exists), `security-scan` (secrets & `.env`), `branch-policy` (`develop`/`release/*`/`hotfix/*` -> `main`), `ci-quality-gate`. |
| **Automated Test Baseline (`.venv`, Python 3.13.14)** | **`145 passed, 0 failed, 1 warning`** in 375.00s (up from 138 after PR #20 added 7 M01 tests); CI Ruff check: **`All checks passed!`**; OpenAPI parity (`docs/m02/openapi.json` == live `app.openapi()`): **`True`**. |

### Directory Inventory Summary

- `project/schemas/`: 8 Pydantic v2 modules (`_common`, `agents`, `patch`, `repair`, `requirements`, `review`, `verification`, `workflow`).
- `project/marsadh_orchestrator/`: 10 core modules + 5 adapters (`codebase_adapter`, `patch_adapter`, `review_adapter`, `testing_adapter`, `verification_adapter`).
- `project/api/`: `routes.py` (7 canonical endpoints), `schemas.py` (safe dashboard DTOs), `executor.py` (`BoundedRunExecutor`), plus `project/main.py` (`create_app`, `app`).
- `project/agents/mujahidh_codebase/`: 5 M01 modules (`analyzer`, `git_diff_analyzer`, `impact_agent`, `codebase_agent`, `risk_reasoning_agent`).
- `project/agents/munshif_testing/`: 4 M03 modules (`agent`, `export`, `groq_model`, `__main__`).
- `project/nafris_verification/`: 10 M04 modules (4 integrated into M02; 6 standalone/hardcoded).
- `frontend/`: `.gitkeep` only (unimplemented).
- `sample_projects/`: `mini_pos/` (5 `.py` files, no `tests/`), `refund_baseline/` (2 `.py` + 1 test), `refund_demo/` (2 `.py` + 1 test).
- `tests/`: 18 test modules (145 passing tests total).

---

## 2. Final M02 Capability Audit (Phase 3)

Classification taxonomy:
1. **Implemented and live verified**
2. **Implemented and offline verified**
3. **Implemented but unverified**
4. **Integration infrastructure only**
5. **Partially implemented**
6. **Missing**
7. **Blocked by teammate dependency**

| # | Capability | Classification | Supporting Source Files & Interfaces | Verification / Test Evidence |
| :-: | :--- | :--- | :--- | :--- |
| 1 | **Shared Pydantic Schemas** | **Implemented and offline verified** | `project/schemas/{_common,agents,patch,repair,requirements,review,verification,workflow}.py` (`extra="forbid"`, truthfulness validators) | `tests/test_m02_schemas.py` (6), `test_m02_requirement_analysis.py`, `test_m02_patch_generation_integration.py`, `test_m02_baseline_verification_integration.py`, `test_m02_bounded_patch_repair.py`, `test_m02_independent_review_and_reliability.py` |
| 2 | **FastAPI Endpoints** | **Implemented and live verified** | `project/main.py` (`create_app`, `app`), `project/api/{routes,schemas,executor}.py` (7 endpoints, CORS, safe 403/404/409/422/500/503 handlers) | `tests/test_m02_api.py` (4), `test_m02_end_to_end_verification.py` (5), live HTTP + Groq run in `docs/m02/LIVE_LLM_VERIFICATION.md` |
| 3 | **Central Orchestrator** | **Implemented and live verified** | `project/marsadh_orchestrator/orchestrator.py` (`OrchestratorService` — 13 public methods, atomic CAS transitions, audit logging) | `tests/test_m02_orchestrator.py` (14), `test_m02_end_to_end_verification.py` (5), live HTTP verification (`docs/m02/LIVE_LLM_VERIFICATION.md`) |
| 4 | **Workflow State Machine** | **Implemented and offline verified** | `WorkflowState` (26 states), `ALLOWED_TRANSITIONS`, `ACTIVE_EXECUTION_STATES`, `PAUSED_REVIEW_STATES`, `TERMINAL_STATES`, `validate_transition()` | `tests/test_m02_schemas.py`, `test_m02_orchestrator.py`, `test_m02_patch_generation_integration.py`, `test_m02_baseline_verification_integration.py`, `test_m02_bounded_patch_repair.py`, `test_m02_independent_review_and_reliability.py` |
| 5 | **Requirement Analyst (A1)** | **Implemented and offline verified** *(live Groq path: **Implemented but unverified**)* | `project/marsadh_orchestrator/requirement_analyst.py` (`RequirementAnalystAgent.analyze`, `deterministic_offline_requirement_analyzer`, lazy `groq.Groq`) | `tests/test_m02_requirement_analysis.py` (14 offline/mocked tests); **live Groq execution not yet performed** |
| 6 | **Codebase Intelligence Adapters (M01)** | **Partially implemented** *(post-change `analyze()`: **Implemented and live verified**; pre-change `collect_pre_change_context()`: **confirmed AST dict-schema defect**)* | `project/marsadh_orchestrator/adapters/codebase_adapter.py` (`CodebaseAdapter.analyze`, `collect_pre_change_context`) | `tests/test_m02_adapters.py` (6), `test_m02_patch_generation_integration.py`; **audit confirmed** `collect_pre_change_context` drops M01 PR #20 dict `functions`/`classes`/`imports` to `[]` (see Section 4) |
| 7 | **Patch Generation Adapter (A4)** | **Integration infrastructure only** | `project/marsadh_orchestrator/adapters/patch_adapter.py` (`PatchGeneratorProvider` Protocol, `PatchGenerationAdapter`, deterministic offline fixtures) | `tests/test_m02_patch_generation_integration.py` (25 offline tests); **no production AI A4 provider exists** |
| 8 | **Patch Validation & Application** | **Implemented and offline verified** | `project/marsadh_orchestrator/patch_engine.py` (`parse_and_check_unified_diff`, `PatchValidator.validate`, `PatchApplicator.apply`, rollback) | `tests/test_m02_patch_generation_integration.py` (25), `test_m02_baseline_verification_integration.py` (rollback), `test_m02_independent_review_and_reliability.py` (config/test manipulation guards) |
| 9 | **Workspace Isolation** | **Implemented and live verified** | `project/marsadh_orchestrator/workspace.py` (`WorkspaceManager`: `prepare_workspace`, `prepare_baseline_workspace`, `prepare_clean_repair_workspace`, `cleanup_workspace`) | `tests/test_m02_workspace.py` (4), `test_m02_baseline_verification_integration.py`, `test_m02_bounded_patch_repair.py`, `test_m02_independent_review_and_reliability.py`; live P0-4 runs. *(Working-copy isolation only; NOT an OS sandbox.)* |
| 10 | **Human Test Review (Gate 1)** | **Implemented and live verified** | `POST /api/runs/{run_id}/test-review`, `OrchestratorService.accept_test_review`, `build_approved_test_specs` (stable scenario IDs + SHA-256) | `tests/test_m02_api.py`, `test_m02_orchestrator.py`, `test_m02_independent_review_and_reliability.py` (4-thread concurrency), live P0-4 runs |
| 11 | **Verification Adapter (M04)** | **Implemented and live verified** | `project/marsadh_orchestrator/adapters/verification_adapter.py` (`VerificationAdapter.verify` wrapping `run_reviewed_tests`, `analyze_failure`, `calculate_risk`) | `tests/test_m02_adapters.py`, `test_m02_orchestrator.py`, live P0-4 runs. *(Coverage & per-node pass IDs blocked by M04 parser limitations.)* |
| 12 | **Baseline Comparison** | **Implemented and offline verified** | `project/marsadh_orchestrator/baseline_verification.py` (`mirror_approved_tests_to_baseline`, `discover_existing_regression_tests`, `execute_snapshot_verification`, `BaselineComparisonEngine.compare`) | `tests/test_m02_baseline_verification_integration.py` (11 offline real-pytest tests) |
| 13 | **Repair Orchestration** | **Integration infrastructure only** *(orchestrator loop & eligibility engine: **Implemented and offline verified**; production AI repair: **Missing**)* | `project/marsadh_orchestrator/repair_engine.py` (`RepairEligibilityEvaluator`, `compute_failure_signature`, `check_approved_test_assumptions_preserved`), `orchestrator.py::_run_bounded_repair_loop` | `tests/test_m02_bounded_patch_repair.py` (11 offline fixture tests); requires production A4 for live AI repair |
| 14 | **Independent Reviewer Adapter (A6)** | **Integration infrastructure only** | `project/marsadh_orchestrator/adapters/review_adapter.py` (`IndependentReviewerProvider` Protocol, `IndependentReviewAdapter`, `DeterministicIndependentReviewerFixture`) | `tests/test_m02_independent_review_and_reliability.py` (8 offline tests); **no production AI A6 provider exists** |
| 15 | **Evidence Reconciliation** | **Implemented and offline verified** | `project/marsadh_orchestrator/review_reconciliation.py` (`verify_candidate_evidence_integrity`, `EvidenceReconciliationEngine.reconcile`, `evaluate_approval_policy`) | `tests/test_m02_independent_review_and_reliability.py` (8 offline tests covering Cases A, B, C, stale candidates, unsubstantiated claims) |
| 16 | **SQLite Persistence** | **Implemented and live verified** | `project/marsadh_orchestrator/store.py` (`RunStore` with WAL, `BEGIN IMMEDIATE`, CAS `update_run_atomic`, parameterized SQL, legacy backward compat) | `tests/test_m02_store.py` (2), `test_m02_independent_review_and_reliability.py`, live P0-4 restart persistence |
| 17 | **Secret Redaction** | **Implemented and offline verified** | `store.py` (`redact_secrets`, `redact_nested_secrets`, `_sanitize_run_record`), `review_adapter.py::sanitize_review_result`, `main.py::_error_response` | `tests/test_m02_live_llm_integration.py` (nested secrets), `test_m02_independent_review_and_reliability.py` (`report.verification` + raw excerpt stripping), `test_ci_smoke.py` |
| 18 | **Restart Recovery** | **Implemented and live verified** | `OrchestratorService.recover_interrupted_runs()` triggered in `project/main.py::_lifespan` (marks `ACTIVE_EXECUTION_STATES` as `FAILED`/`PROCESS_INTERRUPTED`; preserves paused Gate 1/2 runs & completed repair termination reasons) | `tests/test_m02_orchestrator.py`, `test_m02_end_to_end_verification.py`, `test_m02_independent_review_and_reliability.py`, live P0-4 restart check |
| 19 | **API Approval Enforcement (Gate 2)** | **Implemented and live verified** | Unified `evaluate_approval_policy(run)` shared by `OrchestratorService.submit_final_decision` and `project/api/schemas.py::_allowed_actions` | `tests/test_m02_api.py`, `test_m02_independent_review_and_reliability.py` (including 4-thread race), live P0-4 `409 Conflict` block |
| 20 | **OpenAPI Contract Parity** | **Implemented and offline verified** | `docs/m02/openapi.json` vs `project.main:app.openapi()` (6 paths, 7 operations, all A1/A4/repair/baseline/A6 DTOs) | `tests/test_m02_api.py`, `test_m02_end_to_end_verification.py`, `test_m02_independent_review_and_reliability.py`, plus live audit parity check (`True`) |

---

## 3. Team Module Audit Summary (Phase 4)

| Module & Owner | Inspected Source Files | Verified Working Capabilities | Confirmed Gaps / Limitations |
| :--- | :--- | :--- | :--- |
| **M01 — Mujahidh** | `project/agents/mujahidh_codebase/{analyzer,git_diff_analyzer,impact_agent,codebase_agent,risk_reasoning_agent}.py` | AST indexing with qualified names/line numbers/parse errors (`analyzer.py`); tracked Git `A`/`M`/`R`/`D` + explicit base commit (`git_diff_analyzer.py`); reverse dependency BFS with **both absolute and relative imports** + cycle safety (`impact_agent.py`, 7 passing tests in `test_m01_codebase_intelligence.py`); standalone Groq `risk_reasoning_agent.py` (mock-tested). | 1. **Pre-change impact prediction missing** (`run_codebase_agent` requires a Git diff). 2. `risk_reasoning_agent.py` imports `groq` at top level, reads `GROQ_MODEL` at import time, discards `usage`, and is not wired into M02. 3. PR #20 dict output format in `analyzer.py` exposed M02's `collect_pre_change_context` defect (`GAP-04`). |
| **M02 — Marsadh** | `project/schemas/`, `project/marsadh_orchestrator/`, `project/api/`, `project/main.py` | Full deterministic orchestrator, 7 REST endpoints, SQLite persistence + WAL + CAS, workspace isolation, A1 requirement analyst, A4/A6 injectable contracts & offline fixtures, patch validator/applicator/rollback, baseline verification & comparison, bounded repair engine (max 2 retries), evidence reconciliation, unified Gate 2 policy, secret redaction, restart recovery, OpenAPI parity (127 M02 tests passing). | 1. **Confirmed defect (`GAP-04`)**: `CodebaseAdapter.collect_pre_change_context` drops M01 PR #20 AST dict symbols to `[]` & keeps stale absolute-only assumption string. 2. A1 live Groq path not yet live-verified. 3. Production A4 & A6 not implemented (injectable contracts only). 4. Local unauthenticated API & no OS sandbox (explicitly disclosed). |
| **M03 — Munshif** | `project/agents/munshif_testing/{agent,export,groq_model,__main__}.py` | `generate_tests` (unexecuted JSON scenarios + AST `test_*` check), `export_scenarios` (empty-dir safe write), lazy `groq_model.generate` (live-verified on Groq `openai/gpt-oss-120b` in P0-4 & `docs/examples/`), 2 unit + 3 handoff tests passing. | 1. No `criterion_ids` emitted per scenario. 2. No `test_purpose` emitted per scenario. 3. Live tests had import mismatches (`mini_pos.<mod>` vs flat `billing`) and occasional syntax errors. 4. `groq_model.generate` discards `response.usage` token counts. |
| **M04 — Nafris** | `project/nafris_verification/{generated_test_runner,report_parser,failure_analyzer,risk_scorer,coverage_analyzer,regression_detector,ai_analyzer,test_runner,verification_agent,benchmark_engine}.py`, `frontend/` | `run_reviewed_tests`, `parse_report`, `analyze_failure`, `calculate_risk` work cleanly and are integrated into M02 (`VerificationAdapter` + `BaselineComparisonEngine`). | 1. `report_parser.py` omits passed/skipped node IDs and structured collection errors. 2. `test_runner.py` executes pytest on import; `verification_agent.py` uses bare imports & hardcodes `refund_demo`; `ai_analyzer.py` imports unlisted `dotenv` & hardcodes `refund_demo`; `regression_detector.py` hardcodes `refund_baseline`/`refund_demo`. 3. `coverage_analyzer.py` is unintegrated (`pytest-cov` missing from `requirements.txt`). 4. `benchmark_engine.py` benchmarks only `verify()` latency (not multi-agent vs single-agent vs human). 5. `frontend/` has only `.gitkeep`. |

---

## 4. Confirmed Critical Defect Discovered During P0-10 Audit

> Per P0-10 scope restrictions, source code was **not** modified in this audit branch; a focused fix PR is proposed below.

- **Defect (`GAP-04`):** `project/marsadh_orchestrator/adapters/codebase_adapter.py` lines 625–627 (`CodebaseAdapter.collect_pre_change_context`):
  ```python
  functions=[str(fn) for fn in item.get("functions", []) if isinstance(fn, str)],
  classes=[str(cl) for cl in item.get("classes", []) if isinstance(cl, str)],
  imports=[str(im) for im in item.get("imports", []) if isinstance(im, str)],
  ```
- **Root Cause:** M01 PR #20 (`da7526a`) upgraded `project/agents/mujahidh_codebase/analyzer.py` so `functions`, `classes`, and `imports` are lists of structured dicts (`{"name": "apply_discount", "line": 6, ...}`) rather than lists of strings.
- **Observed Impact:** Direct verification on `sample_projects/mini_pos` returned `functions=[]`, `classes=[]`, `imports=[]` for every indexed file in `PreChangeCodebaseContext`, depriving A4 of symbol metadata. Additionally, line 415 emits a stale assumption string claiming M01 supports absolute imports only.
- **Proposed Separate Focused Fix (`fix/m02-codebase-adapter-m01-ast-schema`):** Extract `entry["name"]` when `isinstance(entry, dict)` (falling back to `str` for legacy mocks), update line 415's assumption text, and add a regression assertion in `tests/test_m02_patch_generation_integration.py`.

---

## 5. Observed Automated Test Totals (Full Suite Breakdown)

Executed via `.venv\Scripts\python.exe -m pytest -v --tb=short` (`PYTHONPATH=.`, Python 3.13.14, pytest 9.2.0, zero external LLM calls):

| Test File | Tests Passed | Scope |
| :--- | :---: | :--- |
| `tests/test_agent_handoff_integration.py` | 1 | M01 -> M03 -> M04 direct handoff |
| `tests/test_ci_smoke.py` | 6 | Repo layout, AST syntax, CI security contract, secret scan |
| `tests/test_generated_handoff.py` | 2 | M03 export -> M04 `run_reviewed_tests` |
| `tests/test_m01_codebase_intelligence.py` | 7 | M01 AST, relative/cycle BFS, git A/M/D, mocked Groq risk agent |
| `tests/test_m02_adapters.py` | 6 | M02 `CodebaseAdapter`, `TestingAdapter`, `VerificationAdapter` |
| `tests/test_m02_api.py` | 4 | FastAPI endpoints, CORS, OpenAPI, pagination, error handlers |
| `tests/test_m02_baseline_verification_integration.py` | 11 | Baseline vs modified comparison, existing regression suite, tampering |
| `tests/test_m02_bounded_patch_repair.py` | 11 | Repair policy, eligibility, budget cap, Demo A/B/C, scope checks |
| `tests/test_m02_end_to_end_verification.py` | 5 | Live HTTP lifecycle, concurrency, restart recovery, OpenAPI parity |
| `tests/test_m02_independent_review_and_reliability.py` | 8 | A6 review contract, Cases A/B/C, reconciliation, 8 reliability fixes |
| `tests/test_m02_live_llm_integration.py` | 17 | Groq wiring, fail-closed errors, secret redaction, thread env lock |
| `tests/test_m02_orchestrator.py` | 14 | State machine, Gate 1/2, tamper checks, failure outcomes, restart |
| `tests/test_m02_patch_generation_integration.py` | 25 | A4 contract, patch validator/applicator/rollback, security guards |
| `tests/test_m02_requirement_analysis.py` | 14 | A1 decomposition, ambiguity blocking, schema invariants, traceability |
| `tests/test_m02_schemas.py` | 6 | Shared Pydantic v2 schemas, roundtrip, config secret protection |
| `tests/test_m02_store.py` | 2 | SQLite persistence, concurrency, reload |
| `tests/test_m02_workspace.py` | 4 | Isolated clones, working_tree/committed_range, symlink/untracked guards |
| `tests/test_munshif_generation.py` | 2 | M03 prompt context & validation |
| **Total** | **145** | **145 passed, 0 failed, 1 warning (`StarletteDeprecationWarning`)** |

---

## 6. Final Verdict Rationale

**Verdict: `M02 BACKEND READY WITH CRITICAL LIMITATIONS.`**

- **Why Ready for Team Integration:** M02's entire deterministic backend, persistence, API, workspace isolation, validation/application, baseline comparison, bounded repair orchestration, evidence reconciliation, and unified Gate 2 enforcement infrastructure are complete, cleanly tested (145/145 passing), Ruff/pip/OpenAPI-clean, and expose typed dependency-injection hooks (`create_app`) ready for teammates to connect A4, A6, M03/M04 upgrades, and the React frontend without redesigning M02.
- **Why Critical Limitations Must Be Disclosed:**
  1. Confirmed M01-to-M02 AST dict-schema drift defect in `CodebaseAdapter.collect_pre_change_context` (`GAP-04`) requires a small focused fix PR before A4 integration.
  2. Production **A4** and **A6** AI implementations are not yet built (injectable contracts + deterministic offline fixtures only).
  3. **A1** live Groq mode is implemented and unit-tested with mocks, but not yet live-verified against Groq.
  4. MVP operational bounds remain intentionally local/trusted-root (no API auth, no OS container sandbox, single-process thread pool).
  5. M02 P0-9/P0-10 are not yet merged into `origin/develop`, and `origin/main` still contains only empty scaffolds.
