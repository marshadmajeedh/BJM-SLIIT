# ChangeGuard AI — Consolidated Team Integration Gaps Backlog

> **Project:** ChangeGuard AI | **Team:** BJM-SLIIT | **Competition:** Innodata AI Catalyst 2026 (University Track)  
> **Audit Branch:** `docs/m02-final-audit-and-handover` (`5e391d3`)  
> **Scope:** Deduplicated, source-verified integration backlog across M01–M04, A1–A6, frontend, benchmarking, and submission deliverables.

---

## 1. Previously Reported Gaps Verified as RESOLVED (Removed from Backlog)

Direct inspection of the current merged repository (`5e391d3`, including PR #20 M01 hardening and M02 PRs #17–#21) confirms that the following earlier gaps are already resolved and have been removed from the active backlog:

| Earlier Gap | Resolution Evidence in Current Repository |
| :--- | :--- |
| M01 relative import resolution (`from . import ...`) | **Resolved (PR #20, `da7526a`):** `project/agents/mujahidh_codebase/impact_agent.py` (lines 51–61) resolves relative `ast.ImportFrom` levels; verified by `tests/test_m01_codebase_intelligence.py::test_absolute_relative_and_indirect_dependencies`. |
| M01 added/renamed/deleted Python file tracking & explicit base commit | **Resolved (PR #20, `da7526a`):** `project/agents/mujahidh_codebase/git_diff_analyzer.py` parses `git diff --name-status -z` for `A`/`M`/`R`/`D`; verified by `tests/test_m01_codebase_intelligence.py::test_added_deleted_and_modified_git_changes`. |
| Malformed `project/requirements.txt │` filename | **Resolved:** File is cleanly named `project/requirements.txt` with 8 pinned dependencies (`groq`, `pytest`, `pytest-json-report`, `PyYAML`, `pydantic`, `fastapi`, `uvicorn`, `httpx`). |
| M02 baseline-vs-modified verification orchestration | **Resolved (PR #18):** `project/marsadh_orchestrator/baseline_verification.py` & `project/schemas/verification.py`. |
| M02 bounded automated patch repair orchestration | **Resolved (PR #19):** `project/marsadh_orchestrator/repair_engine.py` & `project/schemas/repair.py`. |
| M02 independent review integration & evidence reconciliation | **Resolved (PR #21):** `project/marsadh_orchestrator/adapters/review_adapter.py`, `review_reconciliation.py`, `project/schemas/review.py`. |

---

## 2. Active Consolidated Backlog (Deduplicated & Prioritized)

Priority scale:
- **P0 — Critical submission blocker** (blocks a complete end-to-end live MVP demonstration or required submission deliverable)
- **P1 — Important judging or integration requirement** (needed for full proposal compliance, strong traceability, or rigorous benchmarking)
- **P2 — Useful enhancement that can be deferred**

---

### GAP-01 — Production A4 Automated Patch Generation & Repair Agent

- **Priority:** `P0` | **Prevents Complete Demo:** **Yes**
- **Responsible Member:** **Unassigned** *(Proposal Sec. 10 listed M3 for Developer Agent; no teammate module exists in `project/agents/`)*
- **Exact Missing Capability:** Concrete LLM-backed implementation of `PatchGeneratorProvider.generate_patch(request: PatchGenerationRequest) -> PatchGenerationResult` supporting both initial patch generation (`attempt_number=1`) and bounded repair (`attempt_number in {2, 3}`) against the immutable original snapshot.
- **Why It Matters:** Without production A4, ChangeGuard AI cannot generate or repair code changes from a clean repository in `live_llm` mode (`PATCH_GENERATOR_UNAVAILABLE`), reducing the live demo to `verify_existing_change` only.
- **Actual Source-Level Evidence:** `project/marsadh_orchestrator/adapters/patch_adapter.py` contains only the `PatchGeneratorProvider` Protocol, `PatchGenerationAdapter`, and deterministic test fixtures (`DeterministicPremiumDiscountPatchFixture`, etc.); `project/main.py` defaults `patch_production_provider=None`.
- **Required Implementation Contract:** Implement `PatchGeneratorProvider` per `docs/m02/AGENT_INTEGRATION_CONTRACTS.md` Section 3 (unified Git diff, matching `patch_sha256`, allowed scope, valid A1 `related_criterion_ids`, replacement diffs against original snapshot on repair, honest `PatchModelMetadata`, `token_usage`, fail-closed auth/rate-limit handling) and inject via `create_app(..., patch_production_provider=...)`.
- **Dependencies:** `GAP-04` (M02 `CodebaseAdapter` AST symbol fix so A4 receives populated `PreChangeFileSummary` functions/classes/imports); optional `GAP-06` (M01 pre-change impact prediction).
- **Acceptance Conditions:**
  1. `GET /health` reports `patch_generator_configured: true`.
  2. Mocked-LLM unit tests pass in CI covering initial generation, repair attempt 2 with failure diagnostics, invalid-diff/auth-error handling, and `token_usage`.
  3. Live `generate_and_verify_change` run succeeds on a clean copy of `mini_pos` and a second independent repo without silent offline fallback.
- **Suggested Feature Branch:** `feature/a4-patch-generation-agent`

---

### GAP-02 — Production A6 Independent Reviewer Agent

- **Priority:** `P0` | **Prevents Complete Demo:** **Yes** *(for full 6-agent proposal demonstration & `complete_requirement_assurance=True`)*
- **Responsible Member:** **Unassigned** *(Proposal Sec. 10 grouped reviewer under M4, but `project/nafris_verification/` has no A6 reviewer)*
- **Exact Missing Capability:** Concrete read-only LLM-backed implementation of `IndependentReviewerProvider.review_change(request: IndependentReviewRequest) -> IndependentReviewResult`.
- **Why It Matters:** Independent adversarial evidence review is cited in `docs/Proposal.docx` (Sec. 1 & 5) as one of ChangeGuard AI's three distinctive contributions. Without it, `independent_review.availability` is `"unavailable"` and `complete_requirement_assurance` can never become `True`.
- **Actual Source-Level Evidence:** `project/marsadh_orchestrator/adapters/review_adapter.py` contains only `IndependentReviewerProvider` Protocol, `IndependentReviewAdapter`, and `DeterministicIndependentReviewerFixture`; `project/main.py` defaults `review_production_provider=None`.
- **Required Implementation Contract:** Implement `IndependentReviewerProvider` per `docs/m02/AGENT_INTEGRATION_CONTRACTS.md` Section 4 (strict candidate/snapshot identity binding, artifact-grounded `ReviewEvidenceReference` entries matching `request.known_artifact_ids`, structured `ReviewFinding` list, `support`/`reject`/`needs_review`, real `ReviewModelMetadata` + `token_usage`) and inject via `create_app(..., review_production_provider=...)`.
- **Dependencies:** M02 P0-9 infrastructure (already merged in `5e391d3`).
- **Acceptance Conditions:**
  1. `GET /health` reports `independent_reviewer_configured: true`.
  2. Mocked-LLM CI tests verify `support`, `needs_review` (material disagreement), `reject`, prompt-injection resistance, and valid artifact references.
  3. Live review executes cleanly on verified `mini_pos` change without `INVALID_REVIEW_EVIDENCE_REFERENCE` or `STALE_REVIEW_CANDIDATE`.
- **Suggested Feature Branch:** `feature/a6-independent-reviewer-agent`

---

### GAP-03 — React + Tailwind Web Dashboard (`frontend/`)

- **Priority:** `P0` | **Prevents Complete Demo:** **Yes** *(MVP scope in `Proposal.docx` Sec. 3 & 6 includes a web dashboard)*
- **Responsible Member:** **Nafris** *(M04 / UI per `docs/m02/BACKEND_RELIABILITY_AUDIT.md` & `Proposal.docx` Sec. 10)*
- **Exact Missing Capability:** React + Tailwind CSS web frontend implementing change request submission, workflow stage timeline, A1 criteria/ambiguities display, patch diff & repair history viewer, Gate 1 test review selection, baseline-vs-modified verification & regression comparison view, A6 independent review findings & reconciliation banner, and Gate 2 human approval/rejection controls honoring `allowed_actions`.
- **Why It Matters:** Judges and human reviewers interact with ChangeGuard AI through the web dashboard; `frontend/` currently contains only `.gitkeep`, causing CI's `frontend-validation` job to report `not_applicable`.
- **Actual Source-Level Evidence:** `frontend/.gitkeep` is the only file under `frontend/`; `.github/workflows/ci.yml` line 161 checks for `frontend/package.json`.
- **Required Implementation Contract:** Consume the 7 canonical endpoints documented in `docs/m02/API_CONTRACT.md` and `docs/m02/openapi.json` (`GET /health`, `POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/logs`, `POST /api/runs/{run_id}/test-review`, `POST /api/runs/{run_id}/decision`). Honor `RunDetailResponse.allowed_actions` to disable ordinary `approve` when blocked, display fixture/mock provenance badges (`is_mock_output`, `is_deterministic_fixture`), and use `http://localhost:5173` (already allowlisted in M02 CORS).
- **Dependencies:** M02 REST API & OpenAPI contract (ready).
- **Acceptance Conditions:**
  1. `frontend/package.json` + lockfile present; CI `frontend-validation` job runs `lint`/`typecheck`/`test`/`build` and passes (`frontend_status=validated`).
  2. User can run a complete workflow through Gate 1 and Gate 2 in the browser against `uvicorn project.main:app` with zero CORS/schema errors.
  3. Blocked approval state disables or clearly handles `409 Conflict` with the server's `approval_restrictions`.
- **Suggested Feature Branch:** `feature/frontend-dashboard`

---

### GAP-04 — M02 `CodebaseAdapter.collect_pre_change_context` Schema Drift After M01 PR #20 (Confirmed Defect)

- **Priority:** `P0` | **Prevents Complete Demo:** **No** *(degrades A4 context quality severely)*
- **Responsible Member:** **Marsadh** (M02)
- **Exact Missing Capability:** `CodebaseAdapter.collect_pre_change_context()` fails to extract function, class, and import names from M01's upgraded `analyzer.py` output (`list[dict]` instead of `list[str]`), returning `functions=[]`, `classes=[]`, `imports=[]` for every indexed file. Additionally, `CodebaseAdapter.analyze()` line 415 still emits a stale assumption claiming M01 supports absolute imports only.
- **Why It Matters:** When A4 runs in `generate_and_verify_change` mode, `PreChangeCodebaseContext.indexed_files` loses all AST symbol summaries, depriving A4 of function/class/import metadata.
- **Actual Source-Level Evidence:**
  - `project/agents/mujahidh_codebase/analyzer.py:18-37, 70-84` returns dicts (`{"name": ..., "line": ...}`).
  - `project/marsadh_orchestrator/adapters/codebase_adapter.py:625-627` checks `if isinstance(fn, str)`.
  - Verified live via Python execution: `collect_pre_change_context('.', 'sample_projects/mini_pos', 'HEAD')` returned `functions= [] classes= [] imports= []` for `billing.py`, `customer_service.py`, `dashboard.py`.
- **Required Implementation Contract:** Normalize both `dict` (`entry["name"]`) and legacy `str` entries in `codebase_adapter.py:625-627`, update the stale assumption text at `codebase_adapter.py:415` to reflect M01's current absolute+relative import support (while keeping dynamic-import caveat), and add a regression test in `tests/test_m02_patch_generation_integration.py`.
- **Dependencies:** None.
- **Acceptance Conditions:** `collect_pre_change_context('.', 'sample_projects/mini_pos', 'HEAD')` populates `functions=['calculate_total', 'apply_discount', 'checkout']` for `billing.py` and `imports=['billing']` for `refunds.py`; full pytest suite passes.
- **Suggested Feature Branch:** `fix/m02-codebase-adapter-m01-ast-schema`

---

### GAP-05 — M03 Import-Compatible & Syntax-Reliable Test Generation

- **Priority:** `P0` | **Prevents Complete Demo:** **Yes** *(degrades live verification coverage)*
- **Responsible Member:** **Munshif** (M03)
- **Exact Missing Capability:** Live Groq test generation in `project/agents/munshif_testing/agent.py` does not instruct the LLM on target repository import conventions (flat `source_path` imports vs package imports) and lacks a pre-return import/collection sanity guard or structured syntax-repair retry.
- **Why It Matters:** During P0-4 live Groq verification (`docs/m02/LIVE_LLM_VERIFICATION.md`), 1 of 3 generation runs failed with unparseable Python `SyntaxError` in `test_code`, and 3 of 5 scenarios in both successful runs (`refunds`, `dashboard`, `reports`) imported `mini_pos.<module>` without putting `mini_pos/` on `sys.path`, forcing manual exclusion at Gate 1 and leaving downstream modules unverified.
- **Actual Source-Level Evidence:** `project/agents/munshif_testing/agent.py:47-65` provides no guidance on `source_path` vs `sys.path` import setup; lines 76–82 check `ast.parse` only without import-path compatibility checks.
- **Required Implementation Contract:** Enhance M03 prompt with explicit `source_path` / package-vs-flat import instructions (e.g., adding `source_path` to `sys.path` when no `__init__.py` exists in `source_path`) and optional static AST import validation against `impact_report["repository_analysis"]` before returning proposals (still `executed=False`).
- **Dependencies:** None.
- **Acceptance Conditions:** Live Groq generation on `mini_pos` produces syntax-valid, import-compatible tests for both `billing.py` and downstream modules (`refunds.py`, `reports.py`, `customer_service.py`, `dashboard.py`) that collect cleanly under `run_reviewed_tests`.
- **Suggested Feature Branch:** `feature/m03-import-compatible-tests`

---

### GAP-06 — Fair Comparative Benchmarking Suite (Multi-Agent vs. Single-Agent vs. Human)

- **Priority:** `P0` | **Prevents Complete Demo:** **Yes** *(Proposal Sec. 8 & 11 explicitly require a documented fair single-agent comparison + human evaluation for Phase 1 success)*
- **Responsible Member:** **Nafris** (benchmark runner) + **All Members** (curated tasks, single-agent baseline runner, human evaluation protocol)
- **Exact Missing Capability:** A reproducible comparative benchmark harness evaluating (1) ChangeGuard AI multi-agent pipeline, (2) a comparable single-agent baseline using the same LLM model, repository snapshots, allowed tools, token/retry/timeout budgets, and acceptance/regression tests, and (3) documented human evaluation on identical tasks.
- **Why It Matters:** Answering the core research question (*"Can a requirement-traceable multi-agent workflow produce more verifiably correct changes than a comparable single-agent workflow under similar task, model and resource conditions?"*) is central to judging and proposal compliance (`docs/Proposal.docx` Sec. 1, 8, 11).
- **Actual Source-Level Evidence:** `project/nafris_verification/benchmark_engine.py` runs `verification_agent.verify()` 5 times in a loop on hardcoded `sample_projects/refund_demo` and only measures `verify()` wall-clock time — it has no single-agent baseline, no multi-agent end-to-end orchestration harness, no token/cost tracking, no second repository, and no human evaluation log.
- **Required Implementation Contract:** Build a benchmark runner consuming pinned task definitions (`>= 5` curated scenarios across `mini_pos` and a second independent Python repository), executing both multi-agent and single-agent pipelines under matched budgets, and emitting structured JSON/Markdown artifacts covering all 9 metrics in `docs/m02/INTEGRATION_TEST_PLAN.md` Section 5.
- **Dependencies:** `GAP-01` (A4), `GAP-02` (A6), `GAP-09` (token usage), `GAP-13` (second repo + `mini_pos` baseline tests).
- **Acceptance Conditions:** Measured (never fabricated) experiment logs committed under `docs/benchmarks/` containing task IDs, commit hashes, diffs, test logs, runtime, token counts, estimated API cost, and human evaluation records across `>= 5` tasks.
- **Suggested Feature Branch:** `feature/comparative-benchmark-suite`

---

### GAP-07 — Submission Documentation, Branch Promotion & Demo Video

- **Priority:** `P0` | **Prevents Complete Demo:** **Yes** *(required final submission deliverables)*
- **Responsible Member:** **Marsadh** + **All Members**
- **Exact Missing Capability:**
  1. `docs/Workflow.md` is currently **0 bytes (empty)**.
  2. `README.md` is severely outdated (still describes `main` as having 0-byte `project/main.py`, malformed `requirements.txt │`, and unmerged M01 only).
  3. `docs/PROJECT_DETAILS.md` (741 bytes) only discloses OpenAI Codex for Munshif and Groq `openai/gpt-oss-120b`; missing full team AI coding assistant disclosures, all model versions, and third-party acknowledgments.
  4. `project-demo.mp4` is **missing**.
  5. `origin/main` (`77d0dda`) still contains only initial empty scaffolds; `origin/develop` (`b4946ab`) is missing M02 P0-9 (PR #21) and this P0-10 audit branch.
- **Why It Matters:** `docs/Proposal.docx` Section 11 explicitly lists `project/main.py`, `project/requirements.txt`, `README.md`, `docs/Proposal.docx`, `docs/Workflow.md`, `docs/PROJECT_DETAILS.md`, and `project-demo.mp4` as required repository outputs at final submission.
- **Actual Source-Level Evidence:** Verified via `Get-ChildItem docs` (`Workflow.md` = 0 bytes), `README.md` lines 22, 262–263, 456–478, `git log origin/main`, and absence of `project-demo.mp4`.
- **Required Implementation Contract:** Populate `docs/Workflow.md`, refresh `README.md`, complete `docs/PROJECT_DETAILS.md` with full team disclosures, record `project-demo.mp4` from actual working runs, and promote verified `develop` -> `main` via PR passing `CI Quality Gate`.
- **Dependencies:** Integration of `GAP-01`, `GAP-02`, `GAP-03`, `GAP-06` prior to final demo recording and release PR to `main`.
- **Acceptance Conditions:** Every required deliverable in `docs/m02/SUBMISSION_READINESS.md` is complete, verified on `main`, and passes CI.
- **Suggested Feature Branch:** `docs/submission-final-package` (plus release PR `develop` -> `main`)

---

### GAP-08 — M03 Structured `criterion_ids` and `test_purpose` Metadata

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(degrades requirement traceability and fail-to-pass classification)*
- **Responsible Member:** **Munshif** (M03)
- **Exact Missing Capability:** M03 `generate_tests()` prompt and validation schema do not request or preserve `criterion_ids: list[str]` or `test_purpose: str` per scenario.
- **Why It Matters:**
  - Without `criterion_ids`, M02's `RequirementTraceability` cannot mark criteria `VERIFIED` (remains `mapping_status="unavailable"`, `verification_status="unverified"`), preventing `complete_requirement_assurance=True`.
  - Without `test_purpose`, M02's `BaselineComparisonEngine` cannot distinguish `newly_satisfied_acceptance_criterion` from `fixed_existing_failure` (falls back to `fail_to_pass_unclassified`), and `RepairEligibilityEvaluator` cannot attribute persistent acceptance failures to new requirements.
- **Actual Source-Level Evidence:** `project/agents/munshif_testing/agent.py:49-51` requests only `{"title","rationale","assumptions","test_code"}`. M02 `project/marsadh_orchestrator/adapters/testing_adapter.py:356-467` **already parses and validates** `criterion_ids` and `test_purpose` if M03 returns them!
- **Required Implementation Contract:** Request optional/recommended `criterion_ids` (subset of A1 IDs present in `requirements`) and `test_purpose` (`"new_requirement_acceptance"` | `"existing_behavior_preservation"` | `"unknown"`) in M03's prompt and pass them through in each returned scenario dict.
- **Dependencies:** None (M02 already supports these fields).
- **Acceptance Conditions:** Unit tests in `tests/test_munshif_generation.py` and `tests/test_m02_requirement_analysis.py` confirm M03 emits valid `criterion_ids` and `test_purpose` that propagate cleanly into `RequirementTraceability` and `BaselineComparisonEvidence`.
- **Suggested Feature Branch:** `feature/m03-criterion-and-purpose-metadata`

---

### GAP-09 — Provider Token Usage Reporting (M03, A1, M01 Risk Agent, A4, A6)

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(required for benchmark efficiency & cost metrics)*
- **Responsible Member:** **Munshif** (M03), **Marsadh** (A1/M02), **Mujahidh** (M01 risk agent), future **A4/A6** owners
- **Exact Missing Capability:** `project/agents/munshif_testing/groq_model.py::generate` and `project/marsadh_orchestrator/requirement_analyst.py::_invoke_groq` return `response.choices[0].message.content` (plain `str`), discarding `response.usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`). `risk_reasoning_agent.py` similarly discards `completion.usage`.
- **Why It Matters:** `docs/Proposal.docx` Section 8 mandates measuring total model tokens and estimated API spend per task for the multi-agent vs. single-agent benchmark. M02 already has `token_usage: dict[str, int] | None` fields on `PatchModelMetadata` and `ReviewModelMetadata`.
- **Actual Source-Level Evidence:** `project/agents/munshif_testing/groq_model.py:22`, `project/marsadh_orchestrator/requirement_analyst.py:897`, `project/agents/mujahidh_codebase/risk_reasoning_agent.py:163`.
- **Required Implementation Contract:** Expose token usage metadata (via optional metadata-returning helper or usage sink callback) without breaking existing `(prompt: str) -> str` test stubs, and add optional `token_usage: dict[str, int] | None = None` to `RequirementAnalysisResult` and `TestGenerationResult`.
- **Dependencies:** None.
- **Acceptance Conditions:** Live/mocked Groq calls record exact `prompt_tokens`, `completion_tokens`, and `total_tokens` in run artifacts without fabricating values in offline mode (`None`).
- **Suggested Feature Branch:** `feature/provider-token-usage-telemetry`

---

### GAP-10 — M04 Itemized Per-Test Outcomes & Structured Collection Errors

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(improves verification speed & granularity)*
- **Responsible Member:** **Nafris** (M04)
- **Exact Missing Capability:** `project/nafris_verification/report_parser.py::parse_report` only iterates `if test["outcome"] == "failed"` and ignores passed/skipped test node IDs and `report["collectors"]` collection errors.
- **Why It Matters:** Because M04 omits passed node IDs, M02's `baseline_verification.py` must run one `pytest` subprocess per scenario file and still cannot distinguish individual tests inside multi-test files (`evidence_granularity="single_scenario_file"` instead of `"per_test_node"`). Collection errors are currently regex-extracted from stdout/stderr by M02 as a workaround.
- **Actual Source-Level Evidence:** `project/nafris_verification/report_parser.py:10-25` returns only `total_tests`, `passed`, `failed`, `exit_code`, `duration`, `failures`.
- **Required Implementation Contract:** Preserve all existing keys in `parse_report` and add `tests: list[{"nodeid", "outcome", "duration", "longrepr"}]` (all outcomes) and `collection_errors: list[{"nodeid", "longrepr"}]` parsed from `pytest-json-report`'s `collectors` array where `outcome == "failed"`.
- **Dependencies:** None.
- **Acceptance Conditions:** `tests/test_generated_handoff.py` and M02 baseline comparison tests verify per-node pass/fail/skip and structured collection-error parsing on real `pytest-json-report` output.
- **Suggested Feature Branch:** `feature/m04-itemized-report-parser`

---

### GAP-11 — M04 Integrable Coverage, Risk & AI Failure Analysis Refactoring

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(currently reported honestly as `coverage_status="unavailable"` and `ai_root_cause_status="unavailable"`)*
- **Responsible Member:** **Nafris** (M04)
- **Exact Missing Capability:**
  1. `test_runner.py` executes `pytest --cov` at module import time against hardcoded `sample_projects/refund_demo`.
  2. `verification_agent.py` uses bare local imports (`from report_parser import ...`) and hardcodes `sample_projects/refund_demo`.
  3. `ai_analyzer.py` imports `dotenv` (`python-dotenv` is NOT in `project/requirements.txt` or `.venv`), hardcodes `refund_demo/{refunds.py, billing.py}`, and lacks lazy Groq loading.
  4. `regression_detector.py` hardcodes `refund_baseline` vs `refund_demo`.
  5. `risk_scorer.py` matches only 7 static financial/security keywords, returning `Unknown` for most failures.
  6. `pytest-cov` is not installed or listed in `project/requirements.txt`.
- **Why It Matters:** M02 intentionally isolates these non-integrable files (`verification_adapter.py:8-9`), leaving coverage and AI root-cause unavailable in the API.
- **Actual Source-Level Evidence:** Direct inspection of `project/nafris_verification/{test_runner.py, verification_agent.py, ai_analyzer.py, regression_detector.py, coverage_analyzer.py, risk_scorer.py}` and `project/requirements.txt`.
- **Required Implementation Contract:** Add optional `collect_coverage: bool = False` support to `run_reviewed_tests` (using `coverage_analyzer.py` + `pytest-cov` in `project/requirements.txt`), convert `ai_analyzer.py` to accept parameterized source files without `python-dotenv` or import-time side effects, and fix package-relative imports in `verification_agent.py`.
- **Dependencies:** None (coordinate `project/requirements.txt` addition with Marsadh/CI).
- **Acceptance Conditions:** `run_reviewed_tests(..., collect_coverage=True)` returns real measured `coverage_percentage` inside isolated workspaces; `python -m pip check` and CI pass; no file under `project/nafris_verification/` executes code on import.
- **Suggested Feature Branch:** `feature/m04-integrable-coverage-and-analysis`

---

### GAP-12 — M01 Pre-Change Impact Prediction & Optional Risk Reasoning Wiring

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(M02 currently uses static AST indexing pre-patch and full M01 impact analysis post-patch)*
- **Responsible Member:** **Mujahidh** (M01) + **Marsadh** (M02 adapter wiring)
- **Exact Missing Capability:**
  1. M01 `run_codebase_agent()` is diff-driven (`changed_functions` reads `git diff`); there is no callable pre-change impact prediction function taking candidate target modules/symbols from A1 before a patch is written.
  2. M01's new `risk_reasoning_agent.py` (added in PR #20) is standalone, imports `groq` at top level, and is not wired into M02's `CodebaseAdapter` or `WorkflowRunRecord`.
- **Why It Matters:** `docs/Proposal.docx` Section 5 places A3 impact prediction *before* A4 patch generation so A4 knows which downstream modules are at risk before editing.
- **Actual Source-Level Evidence:** `project/agents/mujahidh_codebase/codebase_agent.py:31` calls `changed_functions`; `project/marsadh_orchestrator/adapters/codebase_adapter.py:105` sets `pre_change_impact_prediction_available: Literal[False] = False`; `docs/m01/M01_HANDOVER.md:24` confirms `risk_reasoning_agent.py` has no M02 schema extension yet.
- **Required Implementation Contract:** Add `predict_impact_for_files(source_path: str, candidate_files: list[str]) -> dict` in `impact_agent.py` reusing `build_dependency_graph()` + BFS; lazy-import `groq` in `risk_reasoning_agent.py` and read `GROQ_MODEL` at call time.
- **Dependencies:** None.
- **Acceptance Conditions:** Given target modules `["billing.py"]` on a clean repo, M01 returns direct/indirect dependents (`refunds.py`, `reports.py`, `customer_service.py`, `dashboard.py`) with zero Git diff required; M02 forwards that graph into `PatchGenerationRequest.codebase_context`.
- **Suggested Feature Branch:** `feature/m01-pre-change-impact-prediction`

---

### GAP-13 — Live Verification of A1 Requirement Analyst Against Groq

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(code implemented & offline-verified; live Groq execution unconfirmed)*
- **Responsible Member:** **Marsadh** (M02)
- **Exact Missing Capability:** Authorized live execution of `RequirementAnalystAgent.analyze(..., execution_mode=ExecutionMode.LIVE_LLM)` against Groq (`GROQ_MODEL`) covering clear, ambiguous, and prompt-injection requirements.
- **Why It Matters:** P0-4 live LLM verification ran before P0-5 added A1; A1's live Groq prompt and strict Pydantic schema validation have only been verified with mocked callables so far.
- **Actual Source-Level Evidence:** `docs/m02/LIVE_LLM_VERIFICATION.md` was recorded at commit `c7a5ecf` (pre-P0-5); `tests/test_m02_requirement_analysis.py` runs offline with deterministic/mocked callables.
- **Required Implementation Contract:** Execute authorized live Groq verification script (no code changes unless prompt/schema tuning is needed for real model JSON compliance) and record observed results.
- **Dependencies:** User authorization + valid `GROQ_API_KEY` and `GROQ_MODEL`.
- **Acceptance Conditions:** Live Groq A1 call returns valid `RequirementAnalysisResult` (`status="completed"` on clear POS request, `status="needs_clarification"` on ambiguous request, zero schema rejections).
- **Suggested Feature Branch:** `test/m02-live-a1-verification`

---

### GAP-14 — Complete `mini_pos` Baseline Suite & Second Independent Demo Repository

- **Priority:** `P1` | **Prevents Complete Demo:** **No** *(for single-repo demo)* / **Yes** *(for proposal generalization & benchmark claims)*
- **Responsible Member:** **All Members / Shared**
- **Exact Missing Capability:**
  1. `sample_projects/mini_pos/` has 5 modules (`billing.py`, `refunds.py`, `reports.py`, `customer_service.py`, `dashboard.py`), **zero pre-existing `tests/test_*.py` files**, and does not include `customers.py`, `inventory.py`, or `payments.py` described in `Proposal.docx` Sec. 7.
  2. No second independent small Python repository (e.g., low-stock alert or order-shipping calculator) exists under `sample_projects/`.
- **Why It Matters:** `docs/Proposal.docx` Executive Summary, Section 7, and Section 8 explicitly commit to evaluating on both the POS repository (with pre-existing regression tests) and an independent second Python repository (`>= 5` curated tasks) to prove ChangeGuard AI is not hardcoded to `mini_pos`.
- **Actual Source-Level Evidence:** `git ls-files sample_projects/` shows only `mini_pos/` (5 `.py` files, no `tests/`), `refund_baseline/` (2 files + 1 test), and `refund_demo/` (2 files + 1 test).
- **Required Implementation Contract:** Add baseline unit/regression tests for `mini_pos` (preserving existing M01/M02 test compatibility or adding a dedicated benchmark fixture dir) and create a clean, unrelated second Python sample repository with pre-existing `pytest` tests and documented change tasks.
- **Dependencies:** None (must not break existing `sample_projects/mini_pos` tests in `tests/test_m02_*.py`).
- **Acceptance Conditions:** Both repositories run cleanly through M01, M02, M03, M04 with real pre-existing regression suites discovered by `discover_existing_regression_tests()`.
- **Suggested Feature Branch:** `feature/benchmark-sample-repositories`

---

### GAP-15 — Operational Hardening Enhancements (OS Sandbox & API Auth)

- **Priority:** `P2` | **Prevents Complete Demo:** **No** *(explicitly bounded in `Proposal.docx` Sec. 3 & 6 as optional/local MVP)*
- **Responsible Member:** **Marsadh / Shared** (post-MVP or finalist phase)
- **Exact Missing Capability:** Optional Docker/container OS sandbox for running untrusted repository `pytest` suites and optional local API authentication/RBAC for non-loopback deployments.
- **Why It Matters:** `Proposal.docx` Section 6 mentions *"Docker-based sandbox if available; resource/time limits"*. Current M02 restricts execution safely to server-configured `CHANGEGUARD_TRUSTED_EXECUTION_ROOTS` and loopback `127.0.0.1`, and discloses both limitations explicitly in `GET /health`.
- **Actual Source-Level Evidence:** `project/marsadh_orchestrator/config.py:65` (`allow_untrusted_execution: Literal[False] = False`), `project/api/routes.py:87-92`.
- **Required Implementation Contract:** Wrap `run_reviewed_tests` execution inside a network-disabled, read-only-root Docker container with CPU/memory/timeout limits if untrusted external repos must be supported.
- **Dependencies:** Docker daemon availability in evaluation environment.
- **Acceptance Conditions:** Untrusted repo pytest runs inside isolated container with zero host filesystem or network access.
- **Suggested Feature Branch:** `feature/docker-test-sandbox`
