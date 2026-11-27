# ChangeGuard AI — Team Integration Handover Guide

> **To:** Mujahidh (M01), Munshif (M03), Nafris (M04 + UI), Marsadh (M02)  
> **From:** M02 Final Backend Audit (`docs/m02-final-audit-and-handover`, commit `5e391d3`)  
> **Current Status:** **145/145 automated tests passing**, `pip check` clean, CI Ruff clean, OpenAPI parity verified.  
> **Verdict:** **`M02 BACKEND READY WITH CRITICAL LIMITATIONS.`**

---

## 1. What Is Ready Right Now (M02 Backend)

M02's orchestration backend (`project/main.py`, `project/api/`, `project/schemas/`, `project/marsadh_orchestrator/`) is ready for team integration:
- **7 Stable REST Endpoints** (full contract in `docs/m02/API_CONTRACT.md` & `docs/m02/openapi.json`):
  - `GET /health`
  - `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/logs`
  - `POST /api/runs/{run_id}/test-review` (Gate 1)
  - `POST /api/runs/{run_id}/decision` (Gate 2)
- **Wired Teammate Modules:** M01 (`run_codebase_agent`, `analyze_repository`), M03 (`generate_tests`, `export_scenarios`, `groq_model.generate`), and M04 (`run_reviewed_tests`, `analyze_failure`, `calculate_risk`).
- **Ready Integration Seams (Zero Orchestrator Redesign Needed):**
  - **A4 Patch Generator:** Implement `PatchGeneratorProvider.generate_patch(request)` and pass `patch_production_provider=...` into `create_app(...)`.
  - **A6 Independent Reviewer:** Implement `IndependentReviewerProvider.review_change(request)` and pass `review_production_provider=...` into `create_app(...)`.
  - **M03 Traceability Fields:** `TestingAdapter` **already parses & validates** `criterion_ids` and `test_purpose` as soon as M03 emits them.
- **Local Startup Command (Windows PowerShell):**
  ```powershell
  $env:PYTHONPATH = "."
  .\.venv\Scripts\python.exe -m uvicorn project.main:app --host 127.0.0.1 --port 8000
  ```

---

## 2. Critical Team Decisions Needed Immediately (Unassigned Roles)

Two P0 proposal agents currently have **M02 integration adapters & offline test fixtures ONLY** — **no production AI implementation and no confirmed owner**:

1. **Production A4 Developer Agent (Patch Generation & Repair)** — *Unassigned* (`GAP-01`, **P0**)
   - **Contract:** `docs/m02/AGENT_INTEGRATION_CONTRACTS.md` Section 3 (`PatchGeneratorProvider.generate_patch`).
   - **Key rule:** On repair attempts (`attempt_number` 2 or 3), generate a **complete replacement unified Git diff against the immutable original source files** in `request.relevant_source_files`, never an incremental diff on top of attempt 1.
   - **Done when:** `GET /health` shows `patch_generator_configured: true`, CI mocked tests pass, and a live `generate_and_verify_change` run patches a clean `mini_pos` copy end-to-end.

2. **Production A6 Independent Reviewer Agent** — *Unassigned* (`GAP-02`, **P0**)
   - **Contract:** `docs/m02/AGENT_INTEGRATION_CONTRACTS.md` Section 4 (`IndependentReviewerProvider.review_change`).
   - **Key rule:** Strictly read-only; echo exact `reviewed_run_id`, `reviewed_candidate_proposal_id`, `reviewed_candidate_patch_sha256`, `reviewed_baseline_snapshot_sha256`, `reviewed_modified_snapshot_sha256`; cite ONLY IDs from `request.known_artifact_ids` (fabricated citations fail with `INVALID_REVIEW_EVIDENCE_REFERENCE`).
   - **Done when:** `GET /health` shows `independent_reviewer_configured: true`, CI mocked tests pass, and live review reconciles cleanly on verified `mini_pos` runs.

> **Action:** Team must assign owners for **A4** and **A6** immediately (or explicitly document scope prioritization per `Proposal.docx` Sec. 5 if constrained by deadline).

---

## 3. Member-Specific Actionable Tasks & Acceptance Conditions

### Marsadh (M02 — Orchestrator, Backend & Integration Lead)

1. **Merge M02 P0-9 + P0-10 into `develop`:** Open PR from `feat/marshad_ahamed` / `docs/m02-final-audit-and-handover` -> `develop` and pass `CI Quality Gate`.
2. **Fix confirmed M01 AST dict-schema defect (`fix/m02-codebase-adapter-m01-ast-schema`, P0 — `GAP-04`):**
   - Update `project/marsadh_orchestrator/adapters/codebase_adapter.py:625-627` so `collect_pre_change_context` extracts `fn["name"]` / `cl["name"]` / `im["name"]` from M01 PR #20's dict entries (instead of filtering `isinstance(..., str)` which currently empties all three lists).
   - Update stale `"resolves absolute Python imports only"` assumption text at `codebase_adapter.py:415`.
   - **Acceptance:** `collect_pre_change_context('.', 'sample_projects/mini_pos', 'HEAD')` returns populated `functions`, `classes`, and `imports`; regression test added; full pytest passes.
3. **Live-verify A1 Requirement Analyst (`test/m02-live-a1-verification`, P1 — `GAP-13`) & capture A1 `token_usage` (`GAP-09`):**
   - **Acceptance:** Authorized live Groq call passes Pydantic validation on clear + ambiguous + prompt-injection requests and records `response.usage` tokens.
4. **Coordinate final release & submission docs (`GAP-07`):** Lead `README.md` refresh, `docs/Workflow.md`, `docs/PROJECT_DETAILS.md` consolidation, and `develop` -> `main` release PR.

---

### Mujahidh (M01 — Codebase Intelligence & Impact Analysis)

*(Note: PR #20 relative imports, `A`/`M`/`R`/`D` diff tracking, and 7 M01 tests are already merged and passing!)*

1. **Add pre-change impact prediction (`feature/m01-pre-change-impact-prediction`, P1 — `GAP-12`):**
   - Add a callable helper (e.g., `predict_impact_for_files(source_path: str, candidate_files: list[str]) -> dict`) in `impact_agent.py` reusing `build_dependency_graph()` + BFS so A4 can see direct/indirect dependents *before* any Git diff exists.
   - **Acceptance:** Calling it on clean `sample_projects/mini_pos` with `["billing.py"]` returns `refunds.py`, `reports.py`, `customer_service.py`, `dashboard.py` with zero Git diff; unit tests pass in CI.
2. **Harden `risk_reasoning_agent.py` for clean M02 importability (P1 — `GAP-12`, `GAP-09`):**
   - Move `from groq import Groq` inside `analyze_regression_risk()`, read `GROQ_MODEL` at call time instead of import time, and include `token_usage` from `completion.usage` when present.
   - **Acceptance:** Importing `risk_reasoning_agent` performs zero Groq SDK import/env side effects; existing `test_risk_json_validation_and_mock_llm` passes.
3. **Add your AI tool/model disclosures** to `docs/PROJECT_DETAILS.md`.

---

### Munshif (M03 — Regression Test Generation)

1. **Fix import compatibility & syntax reliability of generated tests (`feature/m03-import-compatible-tests`, P0 — `GAP-05`):**
   - Update prompt in `project/agents/munshif_testing/agent.py` to instruct the model on flat vs. package imports (adding `source_path` to `sys.path` when no `__init__.py` exists, matching `mini_pos`'s `from billing import ...` style) and validate imports statically before returning proposals (`executed=False`).
   - **Acceptance:** Live Groq run on `mini_pos` produces syntax-valid, pytest-collectible tests for `billing.py` AND downstream modules (`refunds.py`, `reports.py`, `customer_service.py`, `dashboard.py`) with zero `ModuleNotFoundError`.
2. **Emit `criterion_ids` and `test_purpose` per scenario (`feature/m03-criterion-and-purpose-metadata`, P1 — `GAP-08`):**
   - Request and preserve `criterion_ids: list[str]` (referencing A1 `AC-*` IDs passed in `requirements`) and `test_purpose: str` (`"new_requirement_acceptance"` | `"existing_behavior_preservation"` | `"unknown"`) on each scenario dict.
   - **Acceptance:** M02 `TestingAdapter` accepts them cleanly, populating `RequirementTraceability` (`mapping_status="explicit"`) and `BaselineComparisonEngine` (`newly_satisfied_acceptance_criterion` vs `preserved_behavior`).
3. **Expose Groq token usage (`feature/provider-token-usage-telemetry`, P1 — `GAP-09`):**
   - Provide a backward-compatible way to capture `response.usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`) from `groq_model.py` without breaking `(prompt: str) -> str` callers.
   - **Acceptance:** Token counts logged on live/mocked M03 calls without fabricating numbers in offline mode.

---

### Nafris (M04 — Verification, Benchmarking & React Dashboard)

1. **Build React + Tailwind Dashboard (`feature/frontend-dashboard`, P0 — `GAP-03`):**
   - Implement UI in `frontend/` consuming `docs/m02/API_CONTRACT.md` / `docs/m02/openapi.json` (CORS already allows `http://localhost:5173`).
   - Cover: run creation (`verify_existing_change` + `generate_and_verify_change`), live stage polling, A1 criteria/ambiguities, patch diff & repair attempt history, Gate 1 scenario selector, baseline-vs-modified regression table, A6 findings & reconciliation banner, Gate 2 decision panel honoring `allowed_actions` (disable ordinary `approve` when blocked and display `approval_restrictions`), and `GET /api/runs/{run_id}/logs` timeline.
   - **Acceptance:** `frontend/package.json` + lockfile committed; CI `frontend-validation` (`lint`, `typecheck`, `test`, `build`) passes; full browser walkthrough succeeds against local backend.
2. **Extend `report_parser.py` for itemized tests & collection errors (`feature/m04-itemized-report-parser`, P1 — `GAP-10`):**
   - Preserve existing keys and add `tests` (all outcomes: `passed`, `failed`, `skipped`, `error` with `nodeid`/`duration`/`longrepr`) and `collection_errors` from `pytest-json-report` `collectors`.
   - **Acceptance:** `tests/test_generated_handoff.py` and M02 baseline comparison verify per-node outcomes cleanly.
3. **Refactor standalone M04 scripts (`feature/m04-integrable-coverage-and-analysis`, P1 — `GAP-11`):**
   - Remove import-time execution in `test_runner.py`, hardcoded `sample_projects/refund_demo` paths, bare non-package imports in `verification_agent.py`, and unlisted `python-dotenv` import in `ai_analyzer.py`. Add optional `collect_coverage=False` to `run_reviewed_tests` (adding `pytest-cov` to `project/requirements.txt` if used).
   - **Acceptance:** Every file in `project/nafris_verification/` imports cleanly in `.venv` without side effects; `pip check` & CI pass.
4. **Build fair comparative benchmark runner (`feature/comparative-benchmark-suite`, P0 — `GAP-06`, with All Members):**
   - Replace/extend `benchmark_engine.py` per `docs/m02/INTEGRATION_TEST_PLAN.md` Part B to compare **Multi-Agent vs. Single-Agent vs. Human** across `>= 5` curated tasks spanning `mini_pos` + a 2nd independent Python repo under identical model/budgets/tests.
   - **Acceptance:** Measured JSON + Markdown benchmark artifacts committed (zero fictional scores).
5. **Add your AI tool/model disclosures** to `docs/PROJECT_DETAILS.md`.

---

## 4. Recommended Team Implementation Order

Execute in this order to avoid merge conflicts and unblock judging deliverables fastest:

1. **Step 1 (Immediate — Day 1):**
   - Marsadh merges M02 P0-9 + P0-10 -> `develop`, then lands `fix/m02-codebase-adapter-m01-ast-schema` (`GAP-04`).
   - Team formally assigns owners for **A4 (`GAP-01`)** and **A6 (`GAP-02`)**.
2. **Step 2 (Parallel Core Build):**
   - **A4 owner:** Implement production `PatchGeneratorProvider` (`GAP-01`).
   - **A6 owner:** Implement production `IndependentReviewerProvider` (`GAP-02`).
   - **Nafris:** Build React + Tailwind dashboard (`GAP-03`) & itemized `report_parser.py` (`GAP-10`).
   - **Munshif:** Fix M03 import compatibility (`GAP-05`), `criterion_ids`/`test_purpose` (`GAP-08`), and token usage (`GAP-09`).
   - **Mujahidh:** Add M01 pre-change impact helper & lazy `risk_reasoning_agent` (`GAP-12`).
3. **Step 3 (Integration & Testbeds):**
   - Add baseline tests to `mini_pos` + create 2nd independent sample repository (`GAP-14`).
   - Wire production A4 + A6 into `project/main.py::create_app`, run `docs/m02/INTEGRATION_TEST_PLAN.md` Part A (Scenarios 1–15), including live A1 verification (`GAP-13`).
4. **Step 4 (Benchmarking & Final Submission):**
   - Execute fair Multi-Agent vs. Single-Agent vs. Human benchmark (`GAP-06`) and commit measured artifacts.
   - Update `README.md`, write `docs/Workflow.md`, complete `docs/PROJECT_DETAILS.md`, record `project-demo.mp4` (`GAP-07`).
   - Open release PR `develop` -> `main`, verify `CI Quality Gate` passes on `main`, and verify clean-checkout reproducibility before the 29 November 2026 deadline.

---

## 5. Reference Map (M02 Final Documentation Set)

- `docs/m02/M02_FINAL_STATUS.md` — Repo state, M02 capability audit, teammate audit, confirmed defect, 145-test breakdown.
- `docs/m02/AGENT_INTEGRATION_CONTRACTS.md` — Exact callable contracts for A1–A6 + dedicated A4 & A6 handover specs.
- `docs/m02/TEAM_INTEGRATION_GAPS.md` — Deduplicated P0/P1/P2 backlog with acceptance conditions & branch names.
- `docs/m02/PROPOSAL_COMPLIANCE_MATRIX.md` — Proposal-to-implementation traceability without fabricated scores.
- `docs/m02/INTEGRATION_TEST_PLAN.md` — 15-scenario post-merge integration plan + comparative benchmarking protocol.
- `docs/m02/SUBMISSION_READINESS.md` — Required deliverables checklist, `main` promotion gap, AI disclosures, reproducibility.
- `docs/m02/API_CONTRACT.md` & `docs/m02/openapi.json` — Authoritative REST API reference for frontend integration.
