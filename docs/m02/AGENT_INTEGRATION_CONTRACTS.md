# ChangeGuard AI — Agent Integration Contracts & A4/A6 Handover

> **Project:** ChangeGuard AI | **Team:** BJM-SLIIT | **Competition:** Innodata AI Catalyst 2026 (University Track)  
> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch Audited:** `docs/m02-final-audit-and-handover` (`5e391d3`)  
> **Scope:** Authoritative callable interfaces for A1–A6 and M01–M04, role reconciliation, and concise implementation handover contracts for future production **A4** and **A6** owners.

---

## 1. Proposal Role vs. Repository Module Reconciliation

The original submitted proposal (`docs/Proposal.docx`, Sections 5 and 10) defined four team work areas (`M1`–`M4`) and six specialist agent roles (`A1`–`A6`). Actual implementation directories and Git ownership evolved slightly from the initial proposal table. The authoritative mapping is:

| Proposal Agent | Proposal Role | Proposal Work Area (`Proposal.docx` Sec. 10) | Actual Repository Module & Folder | Confirmed Owner (Git / Handover Evidence) | Current Production Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **A1** | Requirement Analyst | M1 (Orchestrator + Requirements) | **M02** — `project/marsadh_orchestrator/requirement_analyst.py` | **Marsadh** | Implemented (offline fixture verified; live Groq code implemented, live run unverified) |
| **Central Orchestrator** | Deterministic State Machine & Backend API | M1 (Orchestrator + Requirements) | **M02** — `project/marsadh_orchestrator/`, `project/api/`, `project/schemas/`, `project/main.py` | **Marsadh** | Implemented & verified (offline + live M03 HTTP path) |
| **A2** | Codebase Intelligence (AST + Git Diff) | M2 (Codebase Intelligence + Impact) | **M01** — `project/agents/mujahidh_codebase/{analyzer.py, git_diff_analyzer.py, codebase_agent.py}` | **Mujahidh** | Implemented & verified (integrated into M02 via `CodebaseAdapter`) |
| **A3** | Change Impact & Regression Risk | M2 (Codebase Intelligence + Impact) | **M01** — `project/agents/mujahidh_codebase/{impact_agent.py, risk_reasoning_agent.py}` | **Mujahidh** | Static BFS + relative imports integrated; AI `risk_reasoning_agent.py` standalone (not wired into M02); pre-change impact prediction missing |
| **A4** | Developer Agent (Patch Generation & Repair) | M3 (Developer Agent + Safe Git Changes) | **M02 Integration Only** — `project/marsadh_orchestrator/adapters/patch_adapter.py` + `patch_engine.py` + `repair_engine.py` | **Unassigned** (production AI agent missing; M02 owns adapter/engine only) | **Integration infrastructure only** (offline deterministic fixtures only; production AI missing) |
| **A5 (Part 1)** | Testing Agent — Test Generation | M4 (Testing / Reviewer / UI) | **M03** — `project/agents/munshif_testing/` | **Munshif** | Implemented & live-verified with Groq (has test import/criterion/purpose/token gaps) |
| **A5 (Part 2)** | Testing Agent — Verification Execution | M4 (Testing / Reviewer / UI) | **M04** — `project/nafris_verification/` | **Nafris** | Core runner/parser/analyzer/scorer integrated into M02; coverage/AI/benchmark scripts standalone & non-integrable |
| **A6** | Independent Reviewer | M4 (Testing / Reviewer / UI) | **M02 Integration Only** — `project/marsadh_orchestrator/adapters/review_adapter.py` + `review_reconciliation.py` | **Unassigned** (production AI reviewer missing; M02 owns adapter/reconciliation only) | **Integration infrastructure only** (offline deterministic fixtures only; production AI missing) |
| **Dashboard UI** | Web Evaluation UI | M4 (Testing / Reviewer / UI) | `frontend/` (currently `.gitkeep` only) | **Nafris** (per M02 handover docs; unimplemented) | **Missing** |

> **Ownership Honesty Rule:** Do not invent or assign ownership for **A4** or **A6** production AI implementations unless confirmed by the team. In the current repository, `project/agents/` contains only `mujahidh_codebase` (M01) and `munshif_testing` (M03); no teammate module exists yet for A4 or A6.

---

## 2. Consolidated Agent Integration Contracts (A1–A6)

### 2.1 A1 — Requirement Analyst

- **Exact File Path:** `project/marsadh_orchestrator/requirement_analyst.py`
- **Class / Functions:**
  - `RequirementAnalystAgent.analyze(requirements: str, *, execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST, repository_context: dict[str, Any] | None = None, model_callable: Callable[[str], str] | None = None, model_label: str | None = None) -> RequirementAnalysisResult`
  - `build_requirement_analysis_prompt(requirements: str, *, repository_context: dict[str, Any] | None = None) -> str`
  - `deterministic_offline_requirement_analyzer(prompt: str) -> str`
  - `format_requirements_for_downstream(original_requirements: str, analysis: RequirementAnalysisResult | None) -> str`
- **Input Parameters:** Natural-language `requirements` string (1–10,000 chars), optional `repository_context` dict, `ExecutionMode` (`offline_test` or `live_llm`), optional custom callable/label.
- **Output Schema:** `project.schemas.requirements.RequirementAnalysisResult` (`status` in `{"completed", "needs_clarification", "error"}`, `acceptance_criteria`, `business_constraints`, `assumptions`, `ambiguities`, `preserved_behaviors`, `edge_cases`, `has_mandatory_unresolved_ambiguities`, `is_deterministic_fixture`, `is_mock_output`, `model_label`, `error`).
- **Execution Mode Support:**
  - `offline_test`: Uses `deterministic_offline_requirement_analyzer` (`is_deterministic_fixture=True`, `is_mock_output=True`).
  - `live_llm`: Lazy-imports `groq.Groq` using `OrchestratorConfig.groq_api_key` / `GROQ_API_KEY` and `groq_model` / `GROQ_MODEL`. Fails closed with `LIVE_LLM_UNCONFIGURED` if credentials are missing; never silently falls back to offline fixture.
- **Error Handling:** Returns structured `RequirementAnalysisResult(status="error", error=AgentError(...))` with secret redaction for empty input (`EMPTY_REQUIREMENT`), missing live credentials (`LIVE_LLM_UNCONFIGURED`), invalid JSON (`INVALID_REQUIREMENT_MODEL_JSON`), schema validation failure (`INVALID_REQUIREMENT_ANALYSIS_SCHEMA`), and provider exceptions (`A1_LIVE_LLM_ERROR`, marking transient timeout/rate-limit errors `retryable=True`).
- **Dependencies:** `pydantic>=2,<3`, `groq>=1.7,<2` (lazy-imported in `live_llm` mode only), `project.schemas.requirements`, `project.marsadh_orchestrator.store.redact_secrets`.
- **Current Integration Status:** Integrated into `OrchestratorService` (`ANALYZING_REQUIREMENTS` stage) and forwarded compatibly to M03 via `format_requirements_for_downstream()`. Offline-verified via unit/integration tests; **live Groq run not yet executed**.
- **Example Invocation:**
  ```python
  from project.marsadh_orchestrator import OrchestratorConfig, RequirementAnalystAgent
  from project.schemas import ExecutionMode

  analyst = RequirementAnalystAgent(config=OrchestratorConfig.from_env())
  result = analyst.analyze(
      "Apply a 10% discount to premium customers only, without changing regular checkout.",
      execution_mode=ExecutionMode.OFFLINE_TEST,
  )
  ```
- **Required Contract Changes:** Optional future addition of `token_usage: dict[str, int] | None` on `RequirementAnalysisResult` (capturing `response.usage` from Groq) for comparative benchmarking.
- **Assigned Owner:** **Marsadh** (M02).

---

### 2.2 A2 — Codebase Intelligence (AST Indexing & Git Diff Mapping)

- **Exact File Paths:**
  - Teammate module (M01): `project/agents/mujahidh_codebase/analyzer.py`, `git_diff_analyzer.py`, `codebase_agent.py`
  - M02 Adapter: `project/marsadh_orchestrator/adapters/codebase_adapter.py`
- **Classes / Functions:**
  - `analyze_repository(repository_path: str) -> dict` (`analyzer.py`)
  - `changed_functions(repository_path, base="HEAD") -> list[dict]` (`git_diff_analyzer.py`)
  - `run_codebase_agent(repository_path=".", source_path="sample_projects/mini_pos", base_ref="HEAD") -> dict` (`codebase_agent.py`)
  - `CodebaseAdapter.analyze(repository_path: str | Path, source_path: str = ".", base_ref: str = "HEAD") -> CodebaseAnalysisResult`
  - `CodebaseAdapter.collect_pre_change_context(repository_path: str | Path, source_path: str = ".", base_ref: str = "HEAD", *, target_modules: list[str] | None = None, max_files: int = 20, max_file_bytes: int = 32768, max_total_bytes: int = 131072) -> tuple[PreChangeCodebaseContext, dict[str, str]]`
- **Input Parameters:** Repository root path, repo-relative `source_path`, Git `base_ref` (validated commit ref).
- **Output Schemas:**
  - Raw M01 `analyze_repository`: `{"repository": str, "files": [{"path": str, "functions": [{"name", "line", "end_line", "kind"}], "classes": [{"name", "line", "end_line"}], "imports": [{"name", "line", "kind", "level"}], "error"?: str}], "total_files": int, "errors": list[dict]}`.
  - Raw M01 `run_codebase_agent`: `{"agent": "CodebaseIntelligenceAgent", "repository": str, "source_path": str, "base_ref": str, "analysis_type": "static_code_change_impact", "status": "completed", "repository_summary": {"total_files": int, "analysis_errors": list}, "repository_analysis": dict, "reports": list[dict]}`.
  - Normalized M02: `CodebaseAnalysisResult` (`status` in `{"completed", "no_changes", "error"}`, `file_impacts: list[ChangedFileImpact]`, `raw_report`, `raw_repository_analysis`) and `PreChangeCodebaseContext` (whose `indexed_files: list[PreChangeFileSummary]` preserves both flat symbol name lists `functions`/`classes`/`imports` — using leading-dot notation such as `.`, `.helper`, `..base` for relative imports — and structured `function_metadata`, `class_metadata`, `import_metadata`, plus explicit `metadata_warnings` for malformed entries).
- **Execution Mode Support:** Deterministic static Python AST + Git CLI (runs identically in both `offline_test` and `live_llm` modes; no LLM calls).
- **Error Handling:** M01 raises `ValueError` / `subprocess.CalledProcessError` / `subprocess.TimeoutExpired` and captures file parse errors inside `errors`. `CodebaseAdapter` catches all errors, redacts credential patterns, handles malformed AST entries explicitly via `metadata_warnings` and `limitation_notes` without discarding valid sibling symbols, and returns structured `CodebaseAnalysisResult(status="error", error=AgentError(...))` or `PreChangeCodebaseContext(status="unavailable", error=AgentError(...))`.
- **Dependencies:** Python standard library (`ast`, `pathlib`, `re`, `subprocess`, `json`) + host `git` CLI.
- **Current Integration Status:** **Implemented and verified** (post-change `analyze()` live-verified; pre-change `collect_pre_change_context()` **GAP-04 resolved** on `fix/m02-codebase-adapter-m01-ast-schema`). `CodebaseAdapter.collect_pre_change_context` now supports current M01 structured AST dicts (`name`, `line`, `end_line`, `kind`, `level`), preserves backward-compatible legacy string entries without fabricating metadata, captures relative imports (including `from . import ...` where `name=""` and `level>=1`), redacts secrets across adapter/store/API layers, and `CodebaseAdapter.analyze()` accurately reflects M01's static absolute + relative import support.
- **Example Invocation:**
  ```python
  from project.marsadh_orchestrator.adapters import CodebaseAdapter
  from project.marsadh_orchestrator.config import OrchestratorConfig

  adapter = CodebaseAdapter(config=OrchestratorConfig.from_env())
  impact = adapter.analyze(repo_path, source_path="mini_pos", base_ref="HEAD")
  pre_ctx, bounded_sources = adapter.collect_pre_change_context(repo_path, source_path="mini_pos", base_ref="HEAD")
  ```
- **Required Contract Changes:** None for GAP-04 (resolved and verified via `tests/test_m02_adapters.py` and `tests/test_m02_patch_generation_integration.py`). Remaining M01 enhancement is pre-change impact prediction (`GAP-12`).
- **Assigned Owner:** **Mujahidh** (M01) + **Marsadh** (M02 `CodebaseAdapter`).

---

### 2.3 A3 — Change Impact Analysis & Risk Reasoning

- **Exact File Paths:**
  - `project/agents/mujahidh_codebase/impact_agent.py`
  - `project/agents/mujahidh_codebase/risk_reasoning_agent.py`
- **Functions:**
  - `build_dependency_graph(repository_path) -> dict[str, list[dict]]` (`impact_agent.py`)
  - `analyze_impact(repository_path, changed_file) -> dict` (`impact_agent.py`)
  - `validate_report(report) -> dict` (`risk_reasoning_agent.py`)
  - `analyze_regression_risk(codebase_report: dict, requirement: str, code_diff: str) -> dict` (`risk_reasoning_agent.py`)
  - `run_risk_agent(repository_path=".", source_path="sample_projects/mini_pos", requirement=...) -> dict` (`risk_reasoning_agent.py`)
- **Input Parameters:**
  - `analyze_impact`: `repository_path` (source root), relative `changed_file` path.
  - `analyze_regression_risk`: M01 `codebase_report` dict, non-empty `requirement` string, non-empty `code_diff` string.
- **Output Schemas:**
  - `analyze_impact`: `{"changed_file", "analysis_method": "static_reverse_dependency_bfs", "potentially_impacted_files": [{"file", "impact_level", "dependency_depth", "via_file", "import_line", "import", "reason"}], "total_impacted", "direct_count", "indirect_count", "analysis_limitations", "evidence_disclaimer"}`.
  - `analyze_regression_risk`: `{"summary", "affected_files", "potential_risks": [{"file", "issue", "evidence", "severity"}], "recommended_tests", "requires_verification": True, "agent": "RiskReasoningAgent", "model": str, "status": "analysis_complete"}`.
- **Execution Mode Support:**
  - `impact_agent.py`: Pure deterministic static AST BFS (supports both **absolute and relative imports**, cycle-safe, verified in `tests/test_m01_codebase_intelligence.py`).
  - `risk_reasoning_agent.py`: Live Groq SDK only (requires `GROQ_API_KEY`; note: imports `from groq import Groq` at module top-level and reads `GROQ_MODEL` at import time).
- **Error Handling:** `impact_agent` raises `ValueError` on missing directory or absolute/`..` paths, and notes deleted/absent files in `analysis_limitations`. `risk_reasoning_agent` raises `RuntimeError` if `GROQ_API_KEY` missing or Groq request fails (sanitized status code), and `ValueError` on empty inputs or schema validation errors.
- **Dependencies:** `impact_agent.py` uses Python stdlib only. `risk_reasoning_agent.py` requires `groq>=1.7,<2`.
- **Current Integration Status:**
  - `impact_agent.py` is **integrated and verified** via `run_codebase_agent` -> `CodebaseAdapter.analyze` (post-change / diff-driven only).
  - `risk_reasoning_agent.py` is **standalone / not integrated into M02** (mock-tested in `tests/test_m01_codebase_intelligence.py`; not called by `CodebaseAdapter` or `OrchestratorService`).
  - **Pre-change impact prediction** (predicting impacted modules from a requirement + candidate modules *before* a Git diff exists) is **missing**.
- **Example Invocation:**
  ```python
  from project.agents.mujahidh_codebase.impact_agent import analyze_impact
  impact = analyze_impact("sample_projects/mini_pos", "billing.py")
  ```
- **Required Contract Changes:**
  1. Add a pre-change impact prediction helper in M01 (e.g., `predict_pre_change_impact(source_path, candidate_files) -> dict`) that builds the reverse dependency graph and ranks target modules without requiring a Git diff.
  2. Lazy-import `groq` inside `analyze_regression_risk` (instead of top-level import) and read `GROQ_MODEL` at call time if M02 is to integrate `risk_reasoning_agent.py` cleanly.
  3. Optionally expose `token_usage` from Groq responses in `risk_reasoning_agent.py`.
- **Assigned Owner:** **Mujahidh** (M01).

---

### 2.4 A4 — Developer Agent (`PatchGeneratorProvider`)

- **Exact File Paths:**
  - `project/marsadh_orchestrator/adapters/patch_adapter.py`
  - `project/marsadh_orchestrator/patch_engine.py`
  - `project/marsadh_orchestrator/repair_engine.py`
  - `project/schemas/patch.py`, `project/schemas/repair.py`
- **Classes / Protocols:**
  - `PatchGeneratorProvider.generate_patch(request: PatchGenerationRequest) -> PatchGenerationResult | dict[str, Any]` (Protocol)
  - `PatchGenerationAdapter.generate(request: PatchGenerationRequest) -> PatchGenerationResult`
  - `PatchValidator.validate(...) -> PatchValidationResult`
  - `PatchApplicator.apply(...) -> PatchApplicationResult`
- **Input Parameters:** `PatchGenerationRequest` (see Section 3 for full contract).
- **Output Schema:** `PatchGenerationResult` (`status` in `{"proposed", "unavailable", "blocked", "error"}`, `attempt_number`, `proposal: PatchProposal | None`, `model_metadata: PatchModelMetadata`, `is_deterministic_fixture`, `is_mock_output`, `error`).
- **Execution Mode Support:**
  - `offline_test`: Uses injected `offline_fixture_provider` (e.g. `DeterministicPremiumDiscountPatchFixture`) or falls back to `production_provider` if provided; otherwise returns `PATCH_GENERATOR_UNAVAILABLE`.
  - `live_llm`: Uses `production_provider` ONLY; returns `PATCH_GENERATOR_UNAVAILABLE` if unconfigured and rejects any fixture/mock metadata (`LIVE_MODE_MOCK_REJECTED`).
- **Error Handling:** Strict schema validation, SHA-256 verification, blocking ambiguity guard (`UNRESOLVED_REQUIREMENT_AMBIGUITY`), attempt number check (`INVALID_PATCH_ATTEMPT_NUMBER`), criterion reference check (`INVALID_CRITERION_REFERENCE`), secret-redacted exception trapping (`A4_PROVIDER_EXCEPTION`).
- **Dependencies:** `pydantic>=2,<3`, host `git` CLI (for `git apply --check` / `git apply` in `patch_engine.py`), plus whatever LLM SDK the future A4 implementation uses (e.g., `groq>=1.7,<2`).
- **Current Integration Status:** **Integration infrastructure only.** M02 orchestrator, validator, applicator, rollback, and bounded repair loop (max 2 repair attempts / 3 total proposals) are implemented and offline-verified with deterministic fixtures, **but no production AI A4 implementation exists**.
- **Required Contract Changes:** None required on M02; implement a concrete `PatchGeneratorProvider` (see Section 3).
- **Assigned Owner:** **Unassigned** (critical submission blocker; proposal Section 10 originally designated M3 for Developer Agent, not yet mapped to a contributor module).

---

### 2.5 A5 (Part 1) — Regression Test Generation (M03)

- **Exact File Paths:**
  - Teammate module (M03): `project/agents/munshif_testing/agent.py`, `export.py`, `groq_model.py`, `__main__.py`
  - M02 Adapter: `project/marsadh_orchestrator/adapters/testing_adapter.py`
- **Classes / Functions:**
  - `generate_tests(repository_path, impact_report, model, requirements="") -> dict` (`agent.py`)
  - `export_scenarios(proposals, output_dir) -> list[str]` (`export.py`)
  - `generate(prompt) -> str` (`groq_model.py`)
  - `TestingAdapter.generate_proposals(repository_path, codebase_result, requirements, *, requirement_analysis=None, execution_mode=ExecutionMode.OFFLINE_TEST, model_callable=None, model_label=None) -> TestGenerationResult`
  - `TestingAdapter.export_selected_scenarios(repository_path, generation_result, selected_scenario_ids=None, *, output_subdir=".changeguard_generated_tests") -> TestGenerationResult`
- **Input Parameters:** Repository root path, M01 `impact_report` dict, `model` callable `(prompt: str) -> str`, `requirements: str` (M02 enriches this with structured A1 criteria via `format_requirements_for_downstream`).
- **Output Schemas:**
  - Raw M03: `{"agent": "RegressionTestGenerationAgent", "status": "proposed", "executed": False, "scenarios": [{"title", "rationale", "assumptions", "test_code"}]}`.
  - Normalized M02: `TestGenerationResult` (`status` in `{"proposed", "skipped_no_changes", "error"}`, `executed=False`, `generation_mode`, `is_mock_output`, `model_label`, `scenarios: list[GeneratedTestScenario]`, `exported_files`, `exported_hashes`, `raw_proposals`, `error`).
- **Execution Mode Support:**
  - `offline_test`: Requires deterministic `model_callable` stub (`is_mock_output=True`).
  - `live_llm`: Lazy-imports `project.agents.munshif_testing.groq_model.generate` guarded by `_GROQ_ENV_LOCK` for thread-safe env scoping. Blocks sensitive file paths (`SENSITIVE_FILES_BLOCKED`) and fails closed if `GROQ_API_KEY` or `GROQ_MODEL` is missing (`LIVE_LLM_UNCONFIGURED`).
- **Error Handling:** M03 validates JSON, non-empty scenario list, required string fields, and AST-parses `test_code` requiring at least one `def test_*`. M02 additionally validates optional `criterion_ids` against A1 criteria (`INVALID_CRITERION_REFERENCE`), optional `test_purpose` against `TestPurpose` (`INVALID_TEST_PURPOSE`), and sanitizes errors.
- **Dependencies:** Python stdlib (`ast`, `json`, `pathlib`); `groq>=1.7,<2` in `groq_model.py` (lazy-imported).
- **Current Integration Status:** **Implemented and live-verified** (P0-4 verified live Groq `openai/gpt-oss-120b` through M02 HTTP API). However, four concrete integration gaps remain:
  1. Generated tests frequently fail imports on flat-import repos like `mini_pos` (`ModuleNotFoundError: No module named 'mini_pos'` or `'billing'`) and occasionally emit syntax errors inside JSON strings.
  2. M03 does not emit `criterion_ids` per scenario (M02 already accepts and validates them if present).
  3. M03 does not emit `test_purpose` (`new_requirement_acceptance` vs `existing_behavior_preservation`) per scenario (M02 already accepts and validates it if present).
  4. `groq_model.generate()` returns raw `str`, discarding `response.usage` token counts.
- **Example Invocation:**
  ```python
  from project.marsadh_orchestrator.adapters import TestingAdapter
  from project.schemas import ExecutionMode

  adapter = TestingAdapter(config)
  gen = adapter.generate_proposals(
      ws_repo, codebase_result, requirements,
      requirement_analysis=req_result,
      execution_mode=ExecutionMode.LIVE_LLM,
  )
  ```
- **Required Contract Changes (Munshif — backward-compatible additions):**
  1. Prompt and validate optional `criterion_ids: list[str]` and `test_purpose: str` on each scenario dict (exact values supported by `TestingAdapter` today: `new_requirement_acceptance`, `existing_behavior_preservation`, `existing_repository_regression`, `unknown`).
  2. Include target repo layout/import-path guidance in M03's prompt and validate generated imports/syntax cleanly before returning proposals.
  3. Provide an optional metadata-preserving Groq helper (or return usage via callback/envelope) so M02/benchmarks can record exact `prompt_tokens`, `completion_tokens`, and `total_tokens`.
- **Assigned Owner:** **Munshif** (M03).

---

### 2.6 A5 (Part 2) — Verification Execution & Baseline Comparison (M04)

- **Exact File Paths:**
  - Integrable M04 helpers (wrapped by M02): `project/nafris_verification/generated_test_runner.py`, `report_parser.py`, `failure_analyzer.py`, `risk_scorer.py`
  - Standalone/non-integrable M04 files (NOT imported by M02): `coverage_analyzer.py`, `regression_detector.py`, `ai_analyzer.py`, `test_runner.py`, `verification_agent.py`, `benchmark_engine.py`
  - M02 Wrappers: `project/marsadh_orchestrator/adapters/verification_adapter.py`, `project/marsadh_orchestrator/baseline_verification.py`
- **Classes / Functions:**
  - `run_reviewed_tests(repository_path, test_files, timeout=60) -> dict` (`generated_test_runner.py`)
  - `parse_report(report_path) -> dict` (`report_parser.py`)
  - `analyze_failure(evidence) -> dict` (`failure_analyzer.py`)
  - `calculate_risk(ai_analysis) -> dict` (`risk_scorer.py`)
  - `VerificationAdapter.verify(workspace_path, approved_test_ids, test_registry, *, timeout_seconds=None, is_trusted_workspace=False, baseline_ref=None) -> VerificationResult`
  - `execute_snapshot_verification(...)` & `BaselineComparisonEngine.compare(...) -> BaselineComparisonEvidence` (`baseline_verification.py`)
- **Input Parameters:** Trusted isolated workspace path, list of stable approved scenario IDs, `ApprovedTestSpec` registry with SHA-256 digests, integer timeout (1–600s).
- **Output Schemas:**
  - Raw M04 `run_reviewed_tests`: `{"status": "completed"|"timeout"|"error", "exit_code": int|None, "total_tests"?: int, "passed"?: int, "failed"?: int, "duration"?: float, "failures": [{"test_name", "status", "evidence"}], "stdout"?: str, "stderr"?: str}`.
  - Normalized M02: `VerificationResult`, `BaselineVerificationResult`, `ModifiedVerificationResult`, `BaselineComparisonEvidence`.
- **Execution Mode Support:** Deterministic local `pytest --json-report` subprocess execution (`shell=False`, explicit timeout). Runs only on server-trusted roots (`CHANGEGUARD_TRUSTED_EXECUTION_ROOTS`). Not an OS sandbox.
- **Error Handling:** Inspects `exit_code` explicitly (`2` -> `COLLECTION_ERROR`, `5` or 0 tests -> `NO_TESTS_COLLECTED`, `1` -> `FAILED_TESTS`, timeout -> `TIMEOUT`). Verifies SHA-256 of approved tests both **before and after** execution. Never infers individual passing test node IDs from multi-file aggregate counts.
- **Dependencies:** `pytest>=8,<10`, `pytest-json-report>=1.5,<2`. (Note: standalone M04 files `ai_analyzer.py` and `test_runner.py` require `python-dotenv` and `pytest-cov`, neither of which is in `project/requirements.txt`.)
- **Current Integration Status:** `run_reviewed_tests`, `parse_report`, `analyze_failure`, and `calculate_risk` are **implemented and verified** inside M02's verification and baseline comparison pipeline. However, `report_parser.py` omits passed/skipped test node IDs and structured collection errors; coverage, AI root-cause analysis, and comparative benchmarking are NOT integrable in their current hardcoded state.
- **Example Invocation:**
  ```python
  from project.marsadh_orchestrator.adapters import VerificationAdapter, build_approved_test_specs

  specs = build_approved_test_specs(gen_result, review_record)
  ver = VerificationAdapter(config).verify(
      ws_repo, ["SCN-001"], specs, is_trusted_workspace=True, baseline_ref="HEAD"
  )
  ```
- **Required Contract Changes (Nafris):**
  1. Extend `report_parser.py` to emit itemized `tests: list[{"nodeid", "outcome", "duration", "longrepr"}]` (including `passed`, `failed`, `skipped`, `error`) and `collectors` / `collection_errors` from `pytest-json-report` JSON while preserving existing keys (`total_tests`, `passed`, `failed`, `exit_code`, `duration`, `failures`).
  2. Add optional parameterized coverage collection to `run_reviewed_tests` (with `pytest-cov` added to `project/requirements.txt` if enabled) and refactor `ai_analyzer.py` / `regression_detector.py` / `test_runner.py` to remove hardcoded `sample_projects/refund_demo` paths, top-level execution side effects, bare non-package imports, and unlisted `python-dotenv` import.
- **Assigned Owner:** **Nafris** (M04).

---

### 2.7 A6 — Independent Reviewer (`IndependentReviewerProvider`)

- **Exact File Paths:**
  - `project/marsadh_orchestrator/adapters/review_adapter.py`
  - `project/marsadh_orchestrator/review_reconciliation.py`
  - `project/schemas/review.py`
- **Classes / Protocols:**
  - `IndependentReviewerProvider.review_change(request: IndependentReviewRequest) -> IndependentReviewResult | dict[str, Any]` (Protocol)
  - `IndependentReviewAdapter.review(request: IndependentReviewRequest) -> IndependentReviewResult`
  - `EvidenceReconciliationEngine.reconcile(...) -> tuple[EvidenceReconciliationResult, IndependentReviewResult | None]`
  - `evaluate_approval_policy(run: WorkflowRunRecord) -> ApprovalPolicyDecision`
- **Input Parameters:** Bounded, frozen, read-only `IndependentReviewRequest` (see Section 4).
- **Output Schema:** `IndependentReviewResult` (`status`, `availability`, `recommendation`, identity fields, `findings`, concern lists, `evidence_references`, `model_metadata`, `error`).
- **Execution Mode Support:**
  - `offline_test`: Uses injected `offline_fixture_provider` (e.g., `DeterministicIndependentReviewerFixture`) or `production_provider`; otherwise returns `status="unavailable"`, `availability="unavailable"`, `INDEPENDENT_REVIEWER_UNAVAILABLE`.
  - `live_llm`: Uses `production_provider` ONLY; never falls back to offline fixture (`INDEPENDENT_REVIEWER_UNAVAILABLE` if unconfigured; `LIVE_MODE_MOCK_REJECTED` if mock/fixture metadata returned).
- **Error Handling:** Validates candidate run/proposal/patch/snapshot identity (`STALE_REVIEW_CANDIDATE`, `STALE_REVIEW_SNAPSHOT`), validates every artifact citation against `build_known_artifact_registry(request)` (`INVALID_REVIEW_EVIDENCE_REFERENCE`), redacts secrets, and deterministically reconciles against verification & baseline evidence.
- **Dependencies:** `pydantic>=2,<3` (plus whatever LLM SDK the future A6 owner uses).
- **Current Integration Status:** **Integration infrastructure only.** Adapter, schema validation, reconciliation engine, and unified Gate 2 approval policy are implemented and offline-verified, **but no production AI A6 implementation exists**.
- **Required Contract Changes:** None required on M02; implement a concrete `IndependentReviewerProvider` (see Section 4).
- **Assigned Owner:** **Unassigned** (proposal Section 10 originally grouped testing/reviewer/UI under M4; no A6 reviewer implementation exists in `project/nafris_verification/` or elsewhere).

---

## 3. Production A4 Integration Handover Contract (Critical Dependency)

> **Status: MISSING / CRITICAL BLOCKER.** Production A4 patch generation & repair is **not implemented**. Without a real `PatchGeneratorProvider`, `workflow_mode="generate_and_verify_change"` cannot run in `live_llm` mode (`PATCH_GENERATOR_UNAVAILABLE`).

### 3.1 Callable Interface to Implement

Implement `PatchGeneratorProvider` (`project/marsadh_orchestrator/adapters/patch_adapter.py`):

```python
from project.schemas.patch import PatchGenerationRequest, PatchGenerationResult

class ProductionA4PatchAgent:
    def generate_patch(self, request: PatchGenerationRequest) -> PatchGenerationResult:
        ...
```

A plain callable `(PatchGenerationRequest) -> PatchGenerationResult | dict[str, Any]` is also accepted. **Never modify files on disk directly**; A4 operates strictly on the serialized `PatchGenerationRequest` and returns a unified Git diff proposal.

### 3.2 Input Contract — `PatchGenerationRequest` (`project/schemas/patch.py`)

| Field | Type / Semantics |
| :--- | :--- |
| `run_id`, `execution_mode` | Workflow correlation ID and `"offline_test"` / `"live_llm"`. |
| `original_requirement` | Verbatim natural-language change request. |
| `acceptance_criteria`, `business_constraints`, `unresolved_ambiguities` | Validated A1 structured records (`AcceptanceCriterion`, `BusinessConstraint`, `RequirementAmbiguity`). Blocking ambiguities are already blocked upstream by M02. |
| `repository_snapshot_ref`, `base_ref`, `resolved_base_commit`, `source_snapshot_sha256`, `source_path` | Immutable original baseline snapshot identity. |
| `codebase_context` | `PreChangeCodebaseContext` (static M01 AST file list with populated `PreChangeFileSummary` `functions`/`classes`/`imports` and structured `function_metadata`/`class_metadata`/`import_metadata`/`metadata_warnings`; `pre_change_impact_prediction_available=False` until M01 adds pre-change prediction). |
| `allowed_modification_paths` | Repo-relative path prefixes/files A4 is allowed to edit (must stay within `source_path`). |
| `relevant_source_files`, `relevant_source_file_paths` | Bounded UTF-8 source contents (`dict[repo_rel_path, source_text]`, max 20 files / 32 KB per file / 128 KB total, sensitive files excluded). |
| `attempt_number`, `attempt_type`, `repair_attempt_number` | Attempt `1` (`"initial"`, `repair_attempt_number=0`) or repair attempts `2` / `3` (`"repair"`, `repair_attempt_number=1` or `2`). Hard cap: max 2 repairs / 3 total proposals. |
| **Repair Context** (populated when `attempt_type == "repair"`) | `previous_proposal_id`, `previous_proposed_patch`, `previous_patch_sha256`, `prior_patch_hashes` (do NOT return a duplicate diff!), `failure_diagnostics: list[FailureDiagnostic]`, `baseline_comparison_evidence: dict`, `failed_criterion_ids: list[str]`, `previous_verification_evidence: dict`. |

### 3.3 Output Contract — `PatchGenerationResult` (`project/schemas/patch.py`)

Return `PatchGenerationResult` with:
- **`status`:** `"proposed"` (must include `proposal` and `error=None`) or `"error"` / `"blocked"` / `"unavailable"` (must include `error=AgentError(...)` and `proposal=None`).
- **`attempt_number`:** MUST equal `request.attempt_number` (`INVALID_PATCH_ATTEMPT_NUMBER` otherwise).
- **`proposal` (`PatchProposal`):**
  - `proposal_id`: Stable ID matching `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$` (e.g., `f"PATCH-{request.attempt_number:03d}"`).
  - `unified_diff`: Valid standard unified Git diff (`diff --git a/<repo_rel> b/<repo_rel>`, `--- a/...`, `+++ b/...`, valid `@@ -old +new @@` hunks, matching target file line endings, zero NUL/binary bytes, zero symlink/submodule modes `120xxx`/`160xxx`).
  - `patch_sha256`: Exact lowercase hex SHA-256 via `compute_patch_sha256(unified_diff)`.
  - `affected_files`: `list[PatchFileChange]` matching the exact files in `unified_diff` (repo-relative POSIX paths).
  - `related_criterion_ids`: Subset of `[ac.criterion_id for ac in request.acceptance_criteria]` ONLY (unknown IDs fail with `INVALID_CRITERION_REFERENCE`).
  - `change_rationale`, `assumptions`, `risk_indicators`, `notes`.
- **`model_metadata` (`PatchModelMetadata`):** Honest `provider`, `model_name`, `model_label`, `generation_mode="live_llm"`, `is_deterministic_fixture=False`, `is_mock_output=False`, and real `token_usage` (`{"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}` or `None` if unavailable — never fabricate!).
- **`is_deterministic_fixture` & `is_mock_output`:** Must match `model_metadata` and MUST be `False` in `live_llm` mode.

### 3.4 Mandatory Safety & Repair Invariants Enforced by M02

1. **Allowed Scope & Protected Paths (`PatchValidator`):** Diffs must stay within `source_path` and `allowed_modification_paths`, within `max_patch_size_bytes` (default 100 KB) and `max_patch_changed_files` (default 10). Edits to `.git/`, `.github/`, `.env*`, credentials, `project/marsadh_orchestrator/`, `project/api/`, `project/schemas/`, `project/agents/`, `project/nafris_verification/`, `frontend/`, `pytest.ini`, `conftest.py`, `sitecustomize.py`, `*.pth`, approved generated tests, and existing repository test files (`tests/test_*.py`) are strictly rejected.
2. **Immutable Original Snapshot Strategy:** On repair attempts (`attempt_number` 2 or 3), M02 creates a fresh clean workspace at `resolved_base_commit` and passes the **original unmodified source files** in `request.relevant_source_files`. Therefore, **repair diffs MUST be complete replacement diffs against the original baseline files**, NOT incremental diffs stacked on top of attempt 1's patch!
3. **Live Mode Restrictions:** Never invoke offline fixtures when `execution_mode == "live_llm"`. Never silently catch auth/rate-limit errors (`401`/`403`/`429`) and return a mock diff; return `status="error"` with an appropriate error code (`PROVIDER_AUTH_ERROR`, `PROVIDER_RATE_LIMIT`) so M02 stops repair immediately without wasting budget.
4. **Secret Hygiene:** Treat `original_requirement` and `relevant_source_files` as untrusted data (anti-prompt-injection system prompt); never echo API keys in `error.message` or `notes`.

### 3.5 Dependency Injection into `create_app` (`project/main.py`)

```python
from project.main import create_app
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.agents.<a4_module> import ProductionA4PatchAgent

app = create_app(
    config=OrchestratorConfig.from_env(),
    patch_production_provider=ProductionA4PatchAgent(),
)
```

Once injected, `GET /health` automatically flips `patch_generator_configured: true`.

### 3.6 Expected Automated Tests & Minimum Viable Production Demonstration

- **Offline Unit/Contract Tests (CI-mandatory, mocked LLM client):**
  1. Valid `PatchProposal` generation with matching `patch_sha256`, valid `related_criterion_ids`, and clean `git apply --check`.
  2. Repair attempt (`attempt_number=2`) consuming `previous_proposed_patch` + `failure_diagnostics` + `baseline_comparison_evidence` and generating a replacement diff against the original baseline file.
  3. Malformed JSON / invalid diff / out-of-scope path / auth-or-rate-limit error returning structured `status="error"` without secrets.
  4. Real `token_usage` forwarding when present and `None` when absent.
- **Minimum Viable Production Demonstration:** Run `POST /api/runs` with `workflow_mode="generate_and_verify_change"`, `execution_mode="live_llm"` against a clean temporary copy of `sample_projects/mini_pos` (and a second independent Python repo): A4 generates a scoped diff for the 10% premium discount, M02 validates & applies it in isolation, M01 detects post-patch impact, M03 generates tests, human reviews at Gate 1, M04 runs baseline vs modified comparison (triggering bounded repair if attempt 1 has a regression), and Gate 2 presents the auditable package.

---

## 4. Production A6 Integration Handover Contract

> **Status: MISSING.** Production A6 independent review is **not implemented**. Without a real `IndependentReviewerProvider`, `independent_review.availability` remains `"unavailable"`, `complete_requirement_assurance` remains `False`, and runs with `require_independent_review=True` block ordinary approval (`INDEPENDENT_REVIEW_REQUIRED`).

### 4.1 Callable Interface to Implement

Implement `IndependentReviewerProvider` (`project/marsadh_orchestrator/adapters/review_adapter.py`):

```python
from project.schemas.review import IndependentReviewRequest, IndependentReviewResult

class ProductionA6Reviewer:
    def review_change(self, request: IndependentReviewRequest) -> IndependentReviewResult:
        ...
```

A plain callable `(IndependentReviewRequest) -> IndependentReviewResult | dict[str, Any]` is also accepted. **A6 is strictly read-only** — it receives frozen evidence only and can never modify source files, tests, Git state, or workflow decisions.

### 4.2 Input Contract — `IndependentReviewRequest` (`project/schemas/review.py`)

| Field Group | Fields Provided by M02 |
| :--- | :--- |
| **Run & Mode** | `run_id`, `workflow_mode`, `execution_mode`. |
| **Requirements** | `original_requirement`, `acceptance_criteria`, `business_constraints`, `unresolved_assumptions`, `unresolved_ambiguities`. |
| **Frozen Final Candidate Identity** | `candidate_proposal_id`, `candidate_patch_sha256`, `candidate_attempt_number`, `original_source_snapshot_sha256`, `baseline_source_snapshot_sha256`, `modified_source_snapshot_sha256`, `modified_git_diff_sha256`. |
| **Bounded Diff & Code Evidence** | `candidate_unified_diff` (max 200 KB), `relevant_source_excerpts` (bounded dict, stripped before SQLite persistence), `relevant_source_file_paths`, `changed_files`, `impacted_modules`, `impact_summary`. |
| **Verification & Traceability** | `approved_test_ids`, `approved_test_hashes`, `verification_summary`, `baseline_comparison_summary`, `requirement_traceability`. |
| **Repair & Artifact Registry** | `repair_attempt_summary`, `repair_termination_reason`, `existing_evidence_limitations`, `known_artifact_ids: dict[str, list[str]]`. |

### 4.3 Output Contract — `IndependentReviewResult` (`project/schemas/review.py`)

Return `IndependentReviewResult` with:
- **`status` & `availability`:** For a successful live review, `status="completed"`, `availability=ReviewAvailabilityStatus.COMPLETED`, `error=None`. For failures, `status="error"`, `availability=ReviewAvailabilityStatus.FAILED`, `recommendation=None`, `error=AgentError(...)`.
- **`recommendation` (`ReviewRecommendation`):** Exactly one of `"support"`, `"reject"`, or `"needs_review"` (mandatory when `status == "completed"`).
- **Mandatory Final Candidate Identity Binding:** Echo these exact values from `request` so M02 rejects stale or cross-run reviews (`STALE_REVIEW_CANDIDATE` / `STALE_REVIEW_SNAPSHOT`):
  - `reviewed_run_id = request.run_id`
  - `reviewed_candidate_proposal_id = request.candidate_proposal_id`
  - `reviewed_candidate_patch_sha256 = request.candidate_patch_sha256`
  - `reviewed_baseline_snapshot_sha256 = request.baseline_source_snapshot_sha256`
  - `reviewed_modified_snapshot_sha256 = request.modified_source_snapshot_sha256`
- **Structured `findings: list[ReviewFinding]`:**
  - Unique `finding_id` (e.g., `REV-001`), `category` (`ReviewFindingCategory`: `requirement_compliance`, `behavioral_regression`, `security`, `maintainability`, `missing_verification_evidence`, `unsupported_assumption`, `evidence_conflict`, `other`), `severity` (`ReviewFindingSeverity`: `critical`, `high`, `medium`, `low`, `info`), `title`, `description`, `is_material` (auto-enforced `True` for `critical`/`high`), `related_criterion_ids`, `affected_files`, `evidence_references`.
- **Artifact-Grounded `evidence_references: list[ReviewEvidenceReference]`:**
  - Every top-level and finding-level `ReviewEvidenceReference(reference_type, reference_id, description)`, plus every `related_criterion_ids` and `affected_files` entry, is deterministically verified against `build_known_artifact_registry(request)` (covering `criterion`, `constraint`, `assumption`, `ambiguity`, `file`, `patch_proposal`, `patch_sha256`, `snapshot_sha256`, `scenario`, `test_identity`, `repair_attempt`, `baseline_comparison`, `verification_outcome`).
  - **Any fabricated or unknown artifact ID causes M02 to reject the entire review with `INVALID_REVIEW_EVIDENCE_REFERENCE`.**
  - Any finding without verified `evidence_references` is flagged `substantiated=False` and logged in `unsubstantiated_claims`.
  - No fabricated confidence percentages or numerical severity scores are permitted.
- **Model Provenance (`ReviewModelMetadata`):** Honest `provider`, `model_name`, `model_label`, `execution_mode="live_llm"`, `is_deterministic_fixture=False`, `is_mock_output=False`, and real `token_usage` (`dict[str, int] | None`).

### 4.4 Material Disagreement & Reconciliation Semantics

M02's `EvidenceReconciliationEngine.reconcile(...)` enforces these rules automatically after A6 runs:
1. **Deterministic verification always overrides reviewer optimism:** If mandatory verification failed, a regression was confirmed, a blocking ambiguity is open, or candidate integrity failed, an A6 `support` recommendation is flagged in `unsubstantiated_claims` and `material_disagreements` and ordinary approval stays blocked (`409 Conflict`).
2. **Material disagreement blocks ordinary approval:** If deterministic verification passed, but A6 emits `recommendation="reject"` or any `critical`/`high`/`is_material=True` finding, M02 sets `reconciliation_status="reconciled_with_disagreement"`, `assurance_classification="reviewer_disagreement"`, and blocks ordinary `approve` (`INDEPENDENT_REVIEW_DISAGREEMENT` / `INDEPENDENT_REVIEW_REJECTED`) while allowing `needs_review` and `reject`.
3. **No extra automatic repair on reviewer disagreement:** Reviewer disagreement NEVER triggers extra automated patch repair loops.
4. **Human Gate 2 remains mandatory:** A6 never approves, commits, merges, pushes, or deploys changes.

### 4.5 Dependency Injection into `create_app` (`project/main.py`)

```python
from project.main import create_app
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.agents.<a6_module> import ProductionA6Reviewer

app = create_app(
    config=OrchestratorConfig.from_env(),
    review_production_provider=ProductionA6Reviewer(),
)
```

Once injected, `GET /health` automatically flips `independent_reviewer_configured: true`.

### 4.6 Expected Automated Tests & Minimum Viable Production Demonstration

- **Offline Unit/Contract Tests (CI-mandatory, mocked LLM client):**
  1. Valid `support` review with exact identity binding and verified `evidence_references` against `build_known_artifact_registry(request)`.
  2. Material disagreement (`needs_review` with `HIGH` finding) and `reject` review citing valid criterion/file artifacts.
  3. Graceful error handling on malformed JSON, provider timeouts/auth errors, and secret redaction.
  4. Prompt-injection resistance (untrusted instructions inside `candidate_unified_diff` or `relevant_source_excerpts` cannot coerce `support`).
- **Minimum Viable Production Demonstration:** Inject `ProductionA6Reviewer` into `create_app`, run a live verified workflow on `mini_pos` (clean pass case -> A6 `support` with valid citations -> `complete_requirement_assurance=True` when all upstream stages are live & mapped; incomplete/edge-case gap case -> A6 `needs_review` with material finding -> Gate 2 blocks ordinary approval with HTTP `409 Conflict` and presents structured findings to the human reviewer).
