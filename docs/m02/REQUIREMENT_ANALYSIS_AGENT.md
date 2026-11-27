# ChangeGuard AI — A1 Structured Requirement Analyst Agent (P0-5)

> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `feature/m02-requirement-analysis-agent`  
> **Components:** `project/schemas/requirements.py`, `project/marsadh_orchestrator/requirement_analyst.py`, `project/marsadh_orchestrator/orchestrator.py`, `project/marsadh_orchestrator/adapters/testing_adapter.py`, `project/api/schemas.py`

---

## 1. Architecture

The **A1 Requirement Analyst Agent** converts raw natural-language software change requests into structured, validated, traceable acceptance criteria **before** M01 static codebase analysis and M03 test generation run.

```text
User Request (POST /api/runs)
   │
   ▼
[CREATED] WorkspaceManager.prepare_workspace (isolated Git copy + snapshot/diff hashes)
   │
   ▼
[ANALYZING_REQUIREMENTS] A1 RequirementAnalystAgent.analyze(...)
   ├── Explicit AcceptanceCriteria (AC-001, AC-002, ...)
   ├── BusinessConstraints (BC-001, ..., including behavior preservation)
   ├── RequirementAssumptions (ASM-001, ..., requires_human_confirmation=True)
   ├── RequirementAmbiguities (AMB-001, ..., blocking vs non-blocking)
   └── Preserved Behaviors & Edge Cases
   │
   ▼ (Structured criteria persisted in RunStore & logged)
[ANALYZING_CODEBASE] M01 CodebaseAdapter.analyze(...)
   │
   ▼ (Structured criteria forwarded compatibly via TestingAdapter)
[GENERATING_TESTS] M03 TestingAdapter.generate_proposals(..., requirement_analysis=...)
   │
   ▼
[AWAITING_TEST_REVIEW] Human Review Gate 1 (mandatory before test export/execution)
   │
   ▼
[RUNNING_VERIFICATION] M04 VerificationAdapter.verify(...)
   │
   ▼
[COMPILING_REPORT] ChangePackageReport + Per-Criterion RequirementTraceability
   │
   ▼
[AWAITING_FINAL_APPROVAL] Human Review Gate 2
   └── Ordinary `approve` blocked (HTTP 409) if verification failed OR mandatory unresolved ambiguity exists
```

### Core Architectural Principles

1. **No fabricated business rules:** Underspecified rules (missing discount percentages, vague eligibility qualifiers such as `"eligible customers"` or `"appropriate"`, open questions) are captured as **blocking `RequirementAmbiguity`** records (`severity="blocking"`, `mandatory_to_resolve=True`), forcing `status="needs_clarification"`.
2. **Explicit vs. inferred separation:** Every `AcceptanceCriterion` declares `origin="explicit"` (directly stated) or `origin="inferred"` (derived edge case / boundary check). Inferred assumptions live in `RequirementAssumption` with `requires_human_confirmation=True`.
3. **Fail-closed live LLM execution:** In `execution_mode="live_llm"`, A1 uses a lazy-imported Groq SDK client (`groq.Groq`) honoring `OrchestratorConfig.groq_api_key` / `groq_model` (or `GROQ_API_KEY` / `GROQ_MODEL`). It **never** silently falls back to deterministic offline output and **never** retries failed provider calls silently.
4. **Clear offline fixture labeling:** In `execution_mode="offline_test"`, A1 runs `deterministic_offline_requirement_analyzer` and explicitly flags `is_deterministic_fixture=True`, `is_mock_output=True`, and `model_label="deterministic_offline_requirement_analyst"`.
5. **Zero teammate source mutations:** M01 (`project/agents/mujahidh_codebase/`), M03 (`project/agents/munshif_testing/`), M04 (`project/nafris_verification/`), and `frontend/` are untouched.

---

## 2. Schema Definitions (`project/schemas/requirements.py`)

All schemas enforce Pydantic v2 `ConfigDict(extra="forbid")` and lossless JSON roundtripping.

| Schema | Role | Key Fields & Invariants |
| :--- | :--- | :--- |
| `AcceptanceCriterion` | Single verifiable requirement unit | `criterion_id` (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`), `requirement_reference`, `description`, `priority` (`must`/`should`/`could`), `mandatory`, `expected_behavior`, `verification_strategy` (`unit_test`/`integration_test`/`regression_test`/`manual_review`), `origin` (`explicit`/`inferred`), `is_edge_case`, `preserves_existing_behavior`, `target_modules`, `assumption_ids`, `ambiguity_ids`. Enforces `priority="must" => mandatory=True`. |
| `BusinessConstraint` | Business rule or preservation constraint | `constraint_id`, `description`, `category` (`functional_rule`/`behavior_preservation`/`boundary`/`non_functional`/`compliance`/`other`), `origin`, `preserves_existing_behavior`, `source_reference`, `related_criterion_ids`. |
| `RequirementAssumption` | Explicit model/analyst inference | `assumption_id`, `description`, `rationale`, `requires_human_confirmation` (default `True`), `related_criterion_ids`. |
| `RequirementAmbiguity` | Open or resolved ambiguity | `ambiguity_id`, `description`, `clarification_question`, `severity` (`blocking`/`non_blocking`), `mandatory_to_resolve`, `resolved`, `resolution_notes`, `related_criterion_ids`. Enforces `blocking => mandatory_to_resolve=True` and `resolved => non-empty resolution_notes`. |
| `RequirementAnalysisResult` | Complete validated A1 output | `status` (`completed`/`needs_clarification`/`error`), `analysis_mode` (`offline_test`/`live_llm`), `is_deterministic_fixture`, `is_mock_output`, `model_label`, `original_requirement`, `requirement_summary`, `acceptance_criteria`, `business_constraints`, `assumptions`, `ambiguities`, `preserved_behaviors`, `edge_cases`, `has_mandatory_unresolved_ambiguities`, `error`, `notes`. Rejects duplicate IDs across all collections, empty criteria on non-error results, dangling cross-references, `status="completed"` when mandatory unresolved ambiguities exist, and `analysis_mode="live_llm"` with `is_deterministic_fixture=True`. |
| `RequirementTraceability` | Per-criterion evidence trace | `criterion_id`, `requirement_reference`, `description`, `origin`, `mandatory`, `affected_source_files`, `file_mapping_status` (`explicit`/`repository_scope_only`/`unavailable`), `generated_scenario_ids`, `approved_test_ids`, `executed_test_ids`, `mapping_status` (`explicit`/`unavailable`), `execution_evidence_summary`, `verification_status` (`verified`/`failed`/`unverified`/`needs_clarification`), `unresolved_ambiguity_ids`, `assumption_ids`, `notes`. Rejects `verification_status="verified"` unless `mapping_status=="explicit"`, `executed_test_ids` is non-empty, subset invariants hold, and `unresolved_ambiguity_ids` is empty. |

Supporting extension on `GeneratedTestScenario` (`project/schemas/agents.py`):
- Added optional `criterion_ids: list[str] = Field(default_factory=list)` (backward compatible; defaults to `[]` when M03 does not supply mappings).

---

## 3. Agent Input / Output Contracts

### Class Signature (`project/marsadh_orchestrator/requirement_analyst.py`)

```python
class RequirementAnalystAgent:
    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        *,
        offline_analyzer_callable: Callable[[str], str] | None = None,
        offline_model_label: str = "deterministic_offline_requirement_analyst",
        live_model_callable: Callable[[str], str] | None = None,
        live_model_label: str | None = None,
    ) -> None: ...

    def analyze(
        self,
        requirements: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST,
        repository_context: dict[str, Any] | None = None,
        model_callable: Callable[[str], str] | None = None,
        model_label: str | None = None,
    ) -> RequirementAnalysisResult: ...
```

### Error Codes Returned on Failure (`status="error"`)

| Error Code | Trigger | `retryable` |
| :--- | :--- | :--- |
| `EMPTY_REQUIREMENT` | Blank or empty requirement text | `False` |
| `LIVE_LLM_UNCONFIGURED` | `execution_mode="live_llm"` without `GROQ_API_KEY` or `GROQ_MODEL` | `False` |
| `INVALID_REQUIREMENT_MODEL_OUTPUT` | `None`, blank, or non-object JSON returned by model | `False` |
| `INVALID_REQUIREMENT_MODEL_JSON` | Malformed JSON returned by model | `True` |
| `INVALID_REQUIREMENT_ANALYSIS_SCHEMA` | Pydantic validation failure (duplicate IDs, empty criteria, dangling references, invalid fields) | `False` |
| `A1_LIVE_LLM_ERROR` | Live provider exception (e.g., `AuthenticationError`, `RateLimitError`, `APITimeoutError`, `NotFoundError`) | `True` for timeout/connection/rate-limit/5xx; `False` otherwise |
| `A1_ANALYSIS_ERROR` | Unexpected exception in offline analyzer callable | `False` |

All error messages and nested details are redacted via `redact_secrets` / `redact_nested_secrets` before persistence and API serialization.

---

## 4. Workflow Integration

- **Workflow State:** Added `WorkflowState.ANALYZING_REQUIREMENTS` between `CREATED` and `ANALYZING_CODEBASE`.
  - Allowed transitions: `CREATED -> {ANALYZING_REQUIREMENTS, FAILED}` and `ANALYZING_REQUIREMENTS -> {ANALYZING_CODEBASE, FAILED}`.
  - Included in `ACTIVE_EXECUTION_STATES`: if the backend restarts while a run is in `ANALYZING_REQUIREMENTS`, `recover_interrupted_runs()` transitions it to `FAILED` (`PROCESS_INTERRUPTED`) without replaying external LLM calls.
- **Persistence & Logging:**
  - `WorkflowRunRecord.requirement_analysis` stores the validated `RequirementAnalysisResult` in SQLite (`RunStore`).
  - `AgentLogEntry` records both invocation and completion/failure under `agent_name="RequirementAnalystAgent"`, `stage="ANALYZING_REQUIREMENTS"`.
  - `AgentExecutionRecord` captures timing, mode, summary, assumptions, and any structured error.
- **Downstream Handoff:**
  - `OrchestratorService` passes `requirement_analysis=run.requirement_analysis` to `TestingAdapter.generate_proposals(...)`.
  - `TestingAdapter` formats structured acceptance criteria, business constraints, preserved behaviors, assumptions, and unresolved ambiguities into M03's existing `requirements: str` argument via `format_requirements_for_downstream(...)` without altering M03 code.
- **Human Gate Preservation:**
  - Gate 1 (`AWAITING_TEST_REVIEW`) and Gate 2 (`AWAITING_FINAL_APPROVAL`) remain mandatory.
  - Gate 2 additionally blocks ordinary `approve` with HTTP `409 Conflict` (`code="UNRESOLVED_REQUIREMENT_AMBIGUITY"`) whenever `requirement_analysis.has_mandatory_unresolved_ambiguities` is `True`; reviewers can still submit `needs_review` or `reject`.

---

## 5. Example Requirement Decomposition

### Input Requirement (Mini POS Demonstration)
> `"Apply a 10% discount to premium customers only, without changing regular customer checkout or breaking dependent refund, dashboard, and report modules."`

### Deterministic Offline A1 Output Summary (`is_deterministic_fixture=True`)

```json
{
  "status": "completed",
  "analysis_mode": "offline_test",
  "is_deterministic_fixture": true,
  "is_mock_output": true,
  "model_label": "deterministic_offline_requirement_analyst",
  "has_mandatory_unresolved_ambiguities": false,
  "acceptance_criteria": [
    {
      "criterion_id": "AC-001",
      "requirement_reference": "Apply a 10% discount to premium customers",
      "description": "Apply an exact 10% discount to premium customer orders during billing.",
      "priority": "must",
      "mandatory": true,
      "expected_behavior": "For customer_type == 'premium', discounted total equals original total * 0.9 (10% reduction).",
      "verification_strategy": "unit_test",
      "origin": "explicit",
      "is_edge_case": false,
      "preserves_existing_behavior": false,
      "target_modules": ["billing"],
      "assumption_ids": [],
      "ambiguity_ids": []
    },
    {
      "criterion_id": "AC-002",
      "requirement_reference": "premium customers only / regular customer checkout unchanged",
      "description": "Preserve existing undiscounted checkout calculation for regular (non-premium) customers.",
      "priority": "must",
      "mandatory": true,
      "expected_behavior": "For customer_type == 'regular', billing and checkout return the original undiscounted subtotal (0% discount).",
      "verification_strategy": "regression_test",
      "origin": "explicit",
      "is_edge_case": false,
      "preserves_existing_behavior": true,
      "target_modules": ["billing"],
      "assumption_ids": [],
      "ambiguity_ids": []
    },
    {
      "criterion_id": "AC-003",
      "requirement_reference": "without breaking dependent refunds, dashboard, reports modules",
      "description": "Preserve operational integrity across dependent modules (refunds, dashboard, reports) affected by billing changes.",
      "priority": "must",
      "mandatory": true,
      "expected_behavior": "Dependent modules (refunds, dashboard, reports) execute without errors and produce consistent totals reflecting applicable customer pricing rules.",
      "verification_strategy": "integration_test",
      "origin": "explicit",
      "is_edge_case": false,
      "preserves_existing_behavior": true,
      "target_modules": ["refunds", "dashboard", "reports"],
      "assumption_ids": ["ASM-002"],
      "ambiguity_ids": []
    },
    {
      "criterion_id": "AC-004",
      "requirement_reference": "Inferred boundary conditions for billing discount calculation",
      "description": "Verify boundary inputs (zero total, unknown customer_type, decimal prices) behave safely under the inferred default policy.",
      "priority": "should",
      "mandatory": false,
      "expected_behavior": "Zero totals return 0.0 without error; unrecognized customer types do not receive the premium discount under inferred default behavior.",
      "verification_strategy": "unit_test",
      "origin": "inferred",
      "is_edge_case": true,
      "preserves_existing_behavior": false,
      "target_modules": ["billing"],
      "assumption_ids": ["ASM-001"],
      "ambiguity_ids": ["AMB-001"]
    }
  ],
  "business_constraints": [
    {"constraint_id": "BC-001", "category": "functional_rule", "preserves_existing_behavior": false, "related_criterion_ids": ["AC-001"]},
    {"constraint_id": "BC-002", "category": "behavior_preservation", "preserves_existing_behavior": true, "related_criterion_ids": ["AC-002"]},
    {"constraint_id": "BC-003", "category": "behavior_preservation", "preserves_existing_behavior": true, "related_criterion_ids": ["AC-003"]}
  ],
  "assumptions": [
    {"assumption_id": "ASM-001", "requires_human_confirmation": true, "related_criterion_ids": ["AC-004"]},
    {"assumption_id": "ASM-002", "requires_human_confirmation": true, "related_criterion_ids": ["AC-003"]}
  ],
  "ambiguities": [
    {"ambiguity_id": "AMB-001", "severity": "non_blocking", "mandatory_to_resolve": false, "resolved": false, "related_criterion_ids": ["AC-004"]}
  ]
}
```

---

## 6. Traceability Behavior

`ChangePackageReport` exposes both `criterion_traceability: list[RequirementTraceability]` and aggregate counters in `traceability: dict[str, Any]`.

| Condition | `mapping_status` | `verification_status` | Notes |
| :--- | :--- | :--- | :--- |
| Criterion linked to any unresolved ambiguity | `"explicit"` or `"unavailable"` | **`needs_clarification`** | Never marked `verified`, even if selected tests pass. |
| M03 did not return explicit `criterion_ids` (current M03 default) | **`"unavailable"`** | **`unverified`** | M02 **never** fabricates mappings from similar test names or titles. |
| Mapped scenario(s) not approved or not executed | `"explicit"` | **`unverified`** | Unapproved proposals do not count as verification evidence. |
| Mapped scenario(s) approved & executed, `verification_passed=True`, zero unresolved ambiguities | `"explicit"` | **`verified`** | Reflects modified working copy execution; baseline regression status remains tracked separately. |
| Mapped scenario(s) failed under M04 `pytest` (`FAILED_TESTS`) | `"explicit"` | **`failed`** | Matched via exported scenario file path against M04 failure diagnostics. |
| Overall run failed on a different scenario file (aggregate parser lacks per-file pass list) | `"explicit"` | **`unverified`** | Conservatively unverified rather than guessing pass status. |
| Collection error, zero tests collected, timeout, or execution error | `"explicit"` | **`unverified`** | Never treated as verified. |

`traceability["requirement_verification_proven"]` remains `False` whenever any mandatory criterion is not `verified`, any mandatory ambiguity is unresolved, baseline regression comparison is unavailable, or offline mock/deterministic fixtures were used.

---

## 7. API Compatibility Notes

- **All 7 canonical endpoints are unchanged.**
- **Request payloads are 100% backward compatible** (`CreateRunRequest`, `TestReviewRequest`, `HumanDecisionRequest` have zero breaking changes).
- **Historical persisted runs are 100% backward compatible:** legacy `WorkflowRunRecord` and `ChangePackageReport` JSON records without `requirement_analysis` or `criterion_traceability` load cleanly with `requirement_analysis=None` and `criterion_traceability=[]`.
- **Additive response changes** (reflected in `docs/m02/openapi.json` and `docs/m02/API_CONTRACT.md`):
  - `WorkflowState` enum adds `ANALYZING_REQUIREMENTS`.
  - `RunDetailResponse` adds `requirement_analysis: RequirementAnalysisResponse | None = None`.
  - `ChangePackageResponse` adds `requirement_analysis` and `criterion_traceability: list[RequirementTraceability]`.
  - `RunSummaryResponse` adds `acceptance_criteria_count: int = 0` and `has_unresolved_ambiguities: bool = False`.
  - `GeneratedTestScenario` adds `criterion_ids: list[str] = []`.
  - `POST /api/runs/{run_id}/decision` may now return `409 Conflict` (`UNRESOLVED_REQUIREMENT_AMBIGUITY`) when attempting `approve` with open mandatory ambiguities.

---

## 8. Limitations

1. **M03 criterion mapping unavailable by default:** Munshif's unmodified `project/agents/munshif_testing/agent.py` prompt requests `{title, rationale, assumptions, test_code}` without `criterion_ids`. Until M03 is updated, `criterion_mapping_status` truthfully reports `"unavailable"` and criteria stay `unverified`.
2. **Per-criterion file mapping is repository-scoped:** M01 produces changed/impacted files for the whole Git diff rather than per-criterion file links, so `file_mapping_status` reports `"repository_scope_only"`.
3. **M04 aggregate pass reporting:** `project/nafris_verification/report_parser.py` returns aggregate pass/fail counts and failure items, not an itemized list of passing test node IDs. When a run has mixed pass/fail across multiple scenarios, non-failing scenarios cannot be independently confirmed as passing from M04's parser output alone and are conservatively marked `unverified`.
4. **Offline analyzer is rule-based:** `deterministic_offline_requirement_analyzer` is a transparent deterministic fixture for offline testing and demos, not a substitute for live semantic LLM analysis.
5. **No interactive ambiguity resolution endpoint yet:** Blocking ambiguities block Gate 2 `approve` and require escalation (`needs_review` / `reject`) or re-submission with clarified requirements.
6. **Live Groq execution not run in CI:** Live A1 Groq wiring is verified offline via controlled mocks; real external API calls require explicit authorization and configured `GROQ_API_KEY` + `GROQ_MODEL`.

---

## 9. Teammate Integration Requirements (Proposed Interface Extensions)

No teammate files were modified in P0-5. The following coordinated interface updates are recommended:

### 9.1 M03 — Regression Test Generation (Owner: Munshif, `project/agents/munshif_testing/`)
- **Proposed contract extension:** Extend the JSON prompt/validation in `agent.py` so each generated scenario optionally emits `"criterion_ids": ["AC-001", ...]` referencing only acceptance criterion IDs supplied in the `requirements` context, without inventing IDs.
- **Note:** `TestingAdapter` already validates and propagates `criterion_ids` whenever M03 emits them, and rejects unknown IDs (`INVALID_CRITERION_REFERENCE`).

### 9.2 M04 — Verification & Report Parser (Owner: Nafris, `project/nafris_verification/`)
- **Proposed contract extension:** Extend `report_parser.py` to return a `passed_tests: list[str]` (pytest node IDs) alongside `failures` so M02 can confirm per-scenario pass evidence even when another scenario in the same run fails.

### 9.3 Frontend — React Dashboard (Owner: Nafris, `frontend/`)
- Include `ANALYZING_REQUIREMENTS` in the active polling states list.
- Render `requirement_analysis` (criteria with explicit/inferred badges, constraints, assumptions requiring confirmation, and blocking ambiguity warnings) before Gate 1 and Gate 2.
- Render `report.criterion_traceability` table showing `criterion_id`, `mapping_status`, `verification_status` (`verified`, `failed`, `unverified`, `needs_clarification`), and `execution_evidence_summary`.
- Respect `allowed_actions` (which automatically disables `approve` when `has_mandatory_unresolved_ambiguities=True`).

### 9.4 M01 — Codebase Intelligence (Owner: Mujahidh, `project/agents/mujahidh_codebase/`)
- No immediate changes required. Optional future enhancement: accept criterion hints (`target_modules`) to prioritize or tag file impact entries per criterion.

---

## 10. Testing Instructions

Run all offline tests (zero external network calls or Groq credentials required):

```powershell
.\.venv\Scripts\python.exe -m pytest -v --tb=short
```

Run only the P0-5 Requirement Analyst test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_m02_requirement_analysis.py -v --tb=short
```

Run CI-equivalent lint & dependency checks:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\ruff.exe check --select E9,F63,F7,F82,F811,F404,F406,F407,F601,F602 --exclude .git,.venv,venv,env,node_modules,__pycache__,.pytest_cache,build,dist,tests/fixtures/invalid .
```
