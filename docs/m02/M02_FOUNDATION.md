# ChangeGuard AI — Module 02 Foundation Integration Handoff

> **Status:** Foundation layer implemented (`feature/m02-schemas-and-adapters`). FastAPI HTTP endpoints (`project/main.py`) and the full orchestration state machine are **not implemented yet** and are scheduled for **P0-2**.

---

## 1. Shared Schemas (`project/schemas/`)

All shared contracts use Pydantic v2 (`ConfigDict(extra="forbid")`) and support lossless JSON serialization via `.model_dump_json()` / `.model_validate_json()`.

### Workflow & Governance Schemas (`project/schemas/workflow.py`)

| Schema | Purpose |
| :--- | :--- |
| `WorkflowState` | 12 deterministic lifecycle states (`CREATED` through `APPROVED` / `REJECTED` / `NEEDS_REVIEW` / `COMPLETED_NO_CHANGES` / `FAILED`). No patch-repair state exists until patch generation is implemented. |
| `ExecutionMode` | `offline_test` (default) or `live_llm` (never silently falls back to mock output). |
| `CreateRunRequest` | Run initiation DTO validating `repository_path` / `repository_id`, relative `source_path`, safe `base_ref`, `requirements`, and `execution_mode`. |
| `AgentLogEntry` | Structured inter-agent communication log entry with UTC timestamp, duration, bounded summaries, and optional `AgentError`. |
| `TestReviewRequest` / `TestReviewRecord` | Human approval/rejection of generated test proposals using stable scenario identifiers (`SCN-001`, ...), rejecting arbitrary filesystem paths. |
| `HumanDecision` / `HumanDecisionRequest` / `HumanDecisionRecord` | Final human approval gate (`approve`, `reject`, `needs_review`) requiring comments for non-approval decisions. |
| `RepositoryInfo` | Resolved repository metadata (`repository_path`, `source_path`, `base_ref`, `resolved_base_commit`, `change_mode`). |
| `WorkflowRunRecord` | Complete persisted workflow run record linking repository metadata, workspace ID, agent results, review decisions, verification evidence, and errors. |

### Normalized Agent Schemas (`project/schemas/agents.py`)

| Schema | Purpose |
| :--- | :--- |
| `AgentExecutionStatus` | `succeeded`, `completed_no_changes`, `failed`, `skipped`, `timeout`. |
| `AgentError` | Structured non-secret error payload (`code`, `message`, `details`, `retryable`). |
| `AgentExecutionRecord` | Per-agent invocation audit metadata. |
| `ImpactedFile` / `ChangedFileImpact` | Normalized M01 impact entries with separate `repo_relative_path` and `source_relative_path`. |
| `CodebaseAnalysisResult` | Normalized M01 result preserving `raw_report` and `raw_repository_analysis`. Explicitly distinguishes `completed`, `no_changes`, and `error`. |
| `GeneratedTestScenario` / `ApprovedTestSpec` | Stable scenario ID (`SCN-001`), content SHA-256, exported repo-relative path, and exported file SHA-256 for tamper verification. |
| `TestGenerationResult` | Normalized M03 result preserving `raw_proposals`, enforcing `executed=False`, and explicitly labeling `is_mock_output`. |
| `VerificationOutcome` | `passed`, `failed_tests`, `collection_error`, `no_tests_collected`, `timeout`, `execution_error`, `unavailable`. |
| `FailureDiagnostic` / `RiskSummary` / `BaselineComparisonEvidence` | Enriched failure evidence (via M04 `failure_analyzer` and `risk_scorer`) with explicit `Unknown` / `unavailable` defaults instead of fabricated metrics. |
| `VerificationResult` | Normalized M04 verification result enforcing truthful `verification_passed` invariants and preserving `raw_result`. |
| `ChangePackageReport` | Auditable change package DTO compiled for final human review. |

---

## 2. Workflow State Definitions

| State | Meaning |
| :--- | :--- |
| `CREATED` | Run record initialized and persisted; workspace ready or pending preparation. |
| `ANALYZING_CODEBASE` | M01 static Git diff and reverse-dependency analysis in progress. |
| `GENERATING_TESTS` | M03 regression test proposal generation and export in progress. |
| `AWAITING_TEST_REVIEW` | Human review gate before any generated test file may be executed. |
| `RUNNING_VERIFICATION` | M04 reviewed-test execution in progress on a trusted isolated working copy. |
| `COMPILING_REPORT` | Aggregating normalized evidence into a `ChangePackageReport`. |
| `AWAITING_FINAL_APPROVAL` | Final human decision gate (`Approve` / `Reject` / `Needs Review`). |
| `COMPLETED_NO_CHANGES` | Terminal non-failure state when M01 detects zero tracked Python changes. Not verification proof. |
| `APPROVED` | Terminal state: human reviewer approved the change package. |
| `REJECTED` | Terminal state: human reviewer rejected the change package or test proposals. |
| `NEEDS_REVIEW` | Terminal escalation state: unresolved risk, verification failure, or reviewer uncertainty. |
| `FAILED` | Terminal error state: infrastructure, validation, timeout, or unrecoverable adapter error. |

---

## 3. Exact Adapter & Core Foundation Signatures

### Configuration (`project/marsadh_orchestrator/config.py`)
```python
class OrchestratorConfig(BaseModel):
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OrchestratorConfig: ...
    def is_repository_allowed(self, repository_path: str | Path) -> bool: ...
    def is_trusted_for_execution(self, path: str | Path) -> bool: ...
    def resolve_repository(self, repository_path: str | None = None, repository_id: str | None = None) -> Path: ...
    def get_groq_api_key(self) -> str | None: ...
    def safe_summary(self) -> dict[str, Any]: ...
```

### SQLite Persistence (`project/marsadh_orchestrator/store.py`)
```python
class RunStore:
    def __init__(self, database_path: str | Path) -> None: ...
    def initialize(self) -> None: ...
    def save_run(self, run: WorkflowRunRecord) -> WorkflowRunRecord: ...
    def get_run(self, run_id: str) -> WorkflowRunRecord | None: ...
    def list_runs(self, *, limit: int = 100, offset: int = 0, state: WorkflowState | None = None) -> list[WorkflowRunRecord]: ...
    def update_state(self, run_id: str, new_state: WorkflowState, *, error: AgentError | None = None, expected_state: WorkflowState | None = None) -> WorkflowRunRecord: ...
    def append_log(self, entry: AgentLogEntry) -> AgentLogEntry: ...
    def get_logs(self, run_id: str) -> list[AgentLogEntry]: ...
```

### Working-Copy Management (`project/marsadh_orchestrator/workspace.py`)
```python
class WorkspaceManager:
    def __init__(self, config: OrchestratorConfig) -> None: ...
    def prepare_workspace(self, run_id: str, repository_path: str | Path, source_path: str = ".", base_ref: str = "HEAD") -> WorkspaceContext: ...
    def cleanup_workspace(self, workspace_path: str | Path) -> bool: ...
```

### M01 Codebase Adapter (`project/marsadh_orchestrator/adapters/codebase_adapter.py`)
```python
class CodebaseAdapter:
    def __init__(self, config: OrchestratorConfig | None = None) -> None: ...
    def analyze(self, repository_path: str | Path, source_path: str = ".", base_ref: str = "HEAD") -> CodebaseAnalysisResult: ...
```

### M03 Testing Adapter (`project/marsadh_orchestrator/adapters/testing_adapter.py`)
```python
class TestingAdapter:
    def __init__(self, config: OrchestratorConfig | None = None) -> None: ...
    def generate_proposals(
        self,
        repository_path: str | Path,
        codebase_result: CodebaseAnalysisResult,
        requirements: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST,
        model_callable: Callable[[str], str] | None = None,
        model_label: str | None = None,
    ) -> TestGenerationResult: ...
    def export_selected_scenarios(
        self,
        repository_path: str | Path,
        generation_result: TestGenerationResult,
        selected_scenario_ids: list[str] | None = None,
        *,
        output_subdir: str = ".changeguard_generated_tests",
    ) -> TestGenerationResult: ...
```

### M04 Verification Adapter (`project/marsadh_orchestrator/adapters/verification_adapter.py`)
```python
def build_approved_test_specs(
    generation_result: TestGenerationResult,
    review: TestReviewRequest | TestReviewRecord,
) -> dict[str, ApprovedTestSpec]: ...

class VerificationAdapter:
    def __init__(self, config: OrchestratorConfig) -> None: ...
    def verify(
        self,
        workspace_path: str | Path,
        approved_test_ids: list[str],
        test_registry: Mapping[str, ApprovedTestSpec],
        *,
        timeout_seconds: int | None = None,
        is_trusted_workspace: bool = False,
        baseline_ref: str | None = None,
    ) -> VerificationResult: ...
```

---

## 4. SQLite Storage Format

Default local path: `.changeguard/changeguard.sqlite3` (gitignored). Uses WAL journal mode, `PRAGMA foreign_keys = ON`, `PRAGMA busy_timeout = 5000`, and `BEGIN IMMEDIATE` write transactions.

- **`workflow_runs`**: `(run_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload_json TEXT NOT NULL)`
- **`agent_logs`**: `(seq INTEGER PRIMARY KEY AUTOINCREMENT, log_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE, agent_name TEXT NOT NULL, stage TEXT NOT NULL, timestamp TEXT NOT NULL, status TEXT NOT NULL, duration_ms INTEGER NOT NULL, payload_json TEXT NOT NULL)` with index `idx_agent_logs_run_seq (run_id, seq)`.

All log text fields pass through secret redaction prior to persistence.

---

## 5. Working-Copy Assumptions & Security Restrictions

- **Supported change modes:**
  1. `working_tree`: Tracked staged/unstaged modifications relative to `base_ref` (typically `HEAD`).
  2. `committed_range`: Committed changes relative to `base_ref` (e.g., `HEAD~1`, commit SHA, or branch ref).
- **Untracked files:** Any untracked non-ignored files under `source_path` raise `UnsupportedWorkspaceStateError` rather than being silently ignored, because M01 only diffs tracked Python files.
- **Isolation boundary:** `WorkspaceManager` clones Git objects without hooks (`--no-hardlinks --no-checkout`), checks out the resolved `HEAD`, ensures `base_ref` resolves identically, and overlays tracked modifications while excluding `.env*` secrets, `.venv`/`venv`/`env`, `__pycache__`, `.pytest_cache`, and `node_modules`. The original repository is never modified.
- **Not a security sandbox:** Temporary working copies isolate files only. Actual `pytest` execution is blocked unless `is_trusted_workspace=True` and `config.trusted_execution_roots` explicitly authorizes the fixture root. Untrusted repository execution remains disabled (`allow_untrusted_execution=False`).

---

## 6. Known Limitations

1. **No FastAPI endpoints or orchestrator service yet:** HTTP routes (`project/main.py`) and state-machine transitions will be implemented in **P0-2**.
2. **No patch generation / repair loop:** `max_repair_attempts` is capped at `2` in configuration for future use, but no active patch-repair state exists yet.
3. **Static import scope (M01):** Reverse-dependency analysis resolves absolute Python imports only.
4. **No baseline-vs-modified execution or coverage yet:** `VerificationResult` exposes explicit `unavailable` fields (`coverage_status`, `ai_root_cause_status`, `baseline_comparison`) so future benchmark/regression runs plug in cleanly without fabricating numbers.
5. **Untrusted execution disabled:** Running arbitrary external repositories requires an OS/container sandbox (e.g., Docker) outside this MVP foundation.

---

## 7. Preliminary API DTO Examples for Frontend Parallel Development (Nafris)

> **Note:** These JSON DTOs match the implemented Pydantic v2 schemas in `project/schemas/`. FastAPI endpoints do **not** exist yet and will expose these payloads in P0-2.

### Preliminary `POST /api/runs` Request (`CreateRunRequest`)
```json
{
  "repository_id": "mini-pos-demo",
  "source_path": "sample_projects/mini_pos",
  "base_ref": "HEAD",
  "requirements": "Apply a 10% discount to premium customers only without affecting regular checkout or refunds.",
  "execution_mode": "offline_test"
}
```

### Preliminary `POST /api/runs/{run_id}/test-review` Request (`TestReviewRequest`)
```json
{
  "approved": true,
  "selected_test_ids": ["SCN-001", "SCN-002"],
  "reviewer": "nafris",
  "notes": "Verified premium and regular checkout expectations."
}
```

### Preliminary `POST /api/runs/{run_id}/decision` Request (`HumanDecisionRequest`)
```json
{
  "decision": "approve",
  "reviewer": "nafris",
  "comments": "Evidence reviewed; discount logic verified on trusted fixture."
}
```

### Preliminary `GET /api/runs/{run_id}` Response Excerpt (`WorkflowRunRecord`)
```json
{
  "run_id": "run_20261127T043000Z_a1b2c3d4e5f6",
  "state": "AWAITING_FINAL_APPROVAL",
  "execution_mode": "offline_test",
  "repository": {
    "repository_id": "mini-pos-demo",
    "repository_path": "/path/to/repo",
    "source_path": "sample_projects/mini_pos",
    "base_ref": "HEAD",
    "resolved_base_commit": "6831f32e47ba43e7ff6c1d3c3808d1dbb318b903",
    "change_mode": "working_tree"
  },
  "verification": {
    "execution_status": "completed",
    "verification_outcome": "passed",
    "verification_passed": true,
    "exit_code": 0,
    "total_tests": 2,
    "passed": 2,
    "failed": 0,
    "coverage_status": "unavailable",
    "coverage_percent": null,
    "baseline_comparison": {
      "status": "unavailable"
    }
  }
}
```

---

## 8. Required Next Integration Steps (P0-2)

1. Implement deterministic `OrchestratorService` state-machine runner in `project/marsadh_orchestrator/orchestrator.py` enforcing valid state transitions and logging every stage via `RunStore`.
2. Wire FastAPI routes in `project/main.py` (`POST /api/runs`, `GET /api/runs`, `GET /api/runs/{run_id}`, `GET /api/runs/{run_id}/logs`, `POST /api/runs/{run_id}/test-review`, `POST /api/runs/{run_id}/decision`, `GET /health`).
3. Add HTTPX / FastAPI `TestClient` integration tests covering the complete API lifecycle (including `COMPLETED_NO_CHANGES`, test rejection, verification failure, and final approval).
4. Add CORS configuration for the local React dashboard (`frontend/`) and align DTOs with Nafris's UI views.
