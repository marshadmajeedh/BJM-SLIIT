# ChangeGuard AI — P0-6 Patch Generation Integration Infrastructure (A4 / M02)

> **Module:** M02 — Central Orchestration & Backend (Owner: Marsadh)  
> **Branch:** `feature/m02-patch-generation-integration`  
> **Scope:** Integration contracts, injectable A4 adapter, controlled patch validator/applicator, workflow-mode state machine, persistence, and API contracts.  
> **Important:** Production AI patch generation intelligence (A4) is **not** implemented here; this layer defines the safe, typed boundary so the future A4 owner can connect their agent without redesigning M02.

---

## 1. Architecture & Workflow Modes

P0-6 separates **workflow purpose** (`workflow_mode`) from **provider execution mode** (`execution_mode`):

| Axis | Values | Purpose |
| :--- | :--- | :--- |
| **`workflow_mode`** (`WorkflowMode`) | `verify_existing_change` (default) | Existing diff-driven verification pipeline for repositories with pre-existing tracked changes. Clean repositories still terminate cleanly in `COMPLETED_NO_CHANGES`. |
| | `generate_and_verify_change` | Accepts a **clean repository** plus a natural-language change request, collects pre-change static context, invokes an injectable A4 adapter, validates and applies the patch inside an isolated workspace, runs post-patch M01 impact analysis, and passes the patched workspace through M03, Gate 1, M04, and Gate 2. |
| **`execution_mode`** (`ExecutionMode`) | `offline_test` (default) | Uses local deterministic fixtures/stubs (explicitly labeled `is_deterministic_fixture=True` / `is_mock_output=True`). Zero external API calls. |
| | `live_llm` | Requires configured live providers; **never** silently falls back to offline fixture generation. Missing production A4 fails explicitly with `PATCH_GENERATOR_UNAVAILABLE`. |

### End-to-End `generate_and_verify_change` Architecture

```text
POST /api/runs (workflow_mode="generate_and_verify_change")
   │
   ▼
[CREATED] WorkspaceManager.prepare_workspace (isolated Git copy + original snapshot/diff SHA-256)
   │
   ▼
[ANALYZING_REQUIREMENTS] A1 RequirementAnalystAgent.analyze(...)
   │
   ▼
[COLLECTING_PRE_CHANGE_CONTEXT] CodebaseAdapter.collect_pre_change_context(...)
   ├── Static AST indexing via M01 analyze_repository (scope="static_repository_index_only")
   ├── Bounded non-sensitive source file discovery under source_path
   └── Blocking Ambiguity Check -> if mandatory ambiguities unresolved: FAILED (UNRESOLVED_REQUIREMENT_AMBIGUITY)
   │
   ▼
[GENERATING_PATCH] PatchGenerationAdapter.generate(PatchGenerationRequest)
   ├── No provider -> FAILED (PATCH_GENERATOR_UNAVAILABLE)
   ├── Invalid schema / unknown criterion_id / live-mode mock -> FAILED
   └── Validated PatchProposal + SHA-256
   │
   ▼
[VALIDATING_PATCH] PatchValidator.validate(...)
   ├── Syntax, binary, symlink mode, path traversal, absolute paths, protected paths
   ├── Allowed scope, symlink escape, size/file limits, affected_files parity
   ├── Workspace integrity, criterion refs, git apply --check
   └── Any violation -> FAILED (zero files modified)
   │
   ▼
[APPLYING_PATCH] PatchApplicator.apply(...)
   ├── Revalidates patch SHA-256, workspace snapshot/diff, symlink containment
   ├── Executes safe `git apply -` (no shell, no --reject, bounded timeout)
   ├── Rolls back cleanly on any failure (prevents partial changes reaching verification)
   └── Recomputes post-patch source_snapshot_sha256 & git_diff_sha256 (preserving original_*)
   │
   ▼
[ANALYZING_POST_PATCH_IMPACT] M01 CodebaseAdapter.analyze(...) on patched workspace
   └── Zero tracked Python changes after patch -> FAILED (POST_PATCH_NO_CHANGES_DETECTED)
   │
   ▼
[GENERATING_TESTS] M03 TestingAdapter.generate_proposals(...)
   │
   ▼
[AWAITING_TEST_REVIEW] Human Review Gate 1 (mandatory before export/execution)
   │
   ▼
[RUNNING_VERIFICATION] M04 VerificationAdapter.verify(...)
   │
   ▼
[COMPILING_REPORT] ChangePackageReport (with patch provenance, snapshot evidence, fixture warnings)
   │
   ▼
[AWAITING_FINAL_APPROVAL] Human Review Gate 2 (never auto-commits, merges, pushes, or deploys)
```

---

## 2. State Machine Transitions

### New Active Workflow States (`project/schemas/workflow.py`)
1. `COLLECTING_PRE_CHANGE_CONTEXT`
2. `GENERATING_PATCH`
3. `VALIDATING_PATCH`
4. `APPLYING_PATCH`
5. `ANALYZING_POST_PATCH_IMPACT`

All five states are registered in `ACTIVE_EXECUTION_STATES`: if the backend process restarts while a run is in any of these stages, `OrchestratorService.recover_interrupted_runs()` transitions the run to `FAILED` (`PROCESS_INTERRUPTED`) without replaying external A4 calls or reapplying patches.

### Transition Table (`ALLOWED_TRANSITIONS`)

| Current State | Permitted Target States |
| :--- | :--- |
| `CREATED` | `ANALYZING_REQUIREMENTS`, `FAILED` |
| `ANALYZING_REQUIREMENTS` | `ANALYZING_CODEBASE` (`verify_existing_change`), `COLLECTING_PRE_CHANGE_CONTEXT` (`generate_and_verify_change`), `FAILED` |
| `ANALYZING_CODEBASE` | `GENERATING_TESTS`, `COMPLETED_NO_CHANGES`, `FAILED` |
| `COLLECTING_PRE_CHANGE_CONTEXT` | `GENERATING_PATCH`, `FAILED` |
| `GENERATING_PATCH` | `VALIDATING_PATCH`, `FAILED` |
| `VALIDATING_PATCH` | `APPLYING_PATCH`, `FAILED` |
| `APPLYING_PATCH` | `ANALYZING_POST_PATCH_IMPACT`, `FAILED` |
| `ANALYZING_POST_PATCH_IMPACT` | `GENERATING_TESTS`, `FAILED` |
| `GENERATING_TESTS` | `AWAITING_TEST_REVIEW`, `FAILED` |
| `AWAITING_TEST_REVIEW` | `RUNNING_VERIFICATION`, `REJECTED`, `FAILED` |
| `RUNNING_VERIFICATION` | `COMPILING_REPORT`, `FAILED` |
| `COMPILING_REPORT` | `AWAITING_FINAL_APPROVAL`, `FAILED` |
| `AWAITING_FINAL_APPROVAL` | `APPROVED`, `REJECTED`, `NEEDS_REVIEW`, `FAILED` |

---

## 3. A4 Input & Output Contracts (`project/schemas/patch.py`)

All schemas enforce Pydantic v2 `ConfigDict(extra="forbid")`.

| Schema | Role | Key Fields & Invariants |
| :--- | :--- | :--- |
| `PreChangeCodebaseContext` | Pre-change static index | `status`, `scope` (`"static_repository_index_only"` / `"unavailable"`), `pre_change_impact_prediction_available: Literal[False]`, `total_indexed_files`, `indexed_file_paths`, `indexed_files`, `relevant_file_paths`, `limitation_notes`, `error`. |
| `PatchGenerationRequest` | Typed A4 input | `run_id`, `original_requirement`, `acceptance_criteria`, `business_constraints`, `unresolved_ambiguities`, `repository_snapshot_ref`, `base_ref`, `resolved_base_commit`, `source_snapshot_sha256`, `source_path`, `codebase_context`, `allowed_modification_paths`, `relevant_source_files` (stripped before SQLite persistence and API serialization), `relevant_source_file_paths`, `attempt_number`, `execution_mode`, `previous_verification_evidence`. |
| `PatchFileChange` | Per-file diff metadata | `path`, `change_type` (`modify`/`add`/`delete`/`rename`), `old_path`, `added_lines`, `removed_lines`, `summary`, `related_criterion_ids`. |
| `PatchProposal` | Proposed unified diff | `proposal_id`, `unified_diff`, `patch_sha256` (validated against `sha256(unified_diff)`), `affected_files` (unique paths), `change_rationale`, `related_criterion_ids`, `assumptions`, `risk_indicators`, `notes`. |
| `PatchModelMetadata` | Honest model provenance | `provider`, `model_name`, `model_label`, `generation_mode`, `is_deterministic_fixture`, `is_mock_output`, `token_usage` (`None` when unavailable; never fabricated). Rejects `live_llm` with fixture/mock flags. |
| `PatchGenerationResult` | Validated A4 output | `status` (`proposed`/`unavailable`/`blocked`/`error`), `attempt_number`, `proposal`, `model_metadata`, `is_deterministic_fixture`, `is_mock_output`, `error`, `notes`. Enforces `proposed <=> proposal present and error None`. |
| `PatchValidationResult` | Pre-apply safety result | `valid`, `status` (`passed`/`failed`), `patch_sha256`, `expected_snapshot_sha256`, `checked_files`, `patch_size_bytes`, `changed_file_count`, `git_apply_check_passed`, `checks_passed`, `violations`, `validated_at`. |
| `PatchApplicationResult` | Isolated apply result | `applied`, `status` (`applied`/`failed`/`reverted`), `patch_sha256`, `original_source_snapshot_sha256`, `original_git_diff_sha256`, `resulting_source_snapshot_sha256`, `resulting_git_diff_sha256`, `applied_files`, `rollback_performed`, `workspace_clean_after_rollback`, `error`, `applied_at`, `isolation_note`. |

---

## 4. Adapter Interface & Connecting the Future A4 Agent

### Protocol (`project/marsadh_orchestrator/adapters/patch_adapter.py`)

```python
from project.marsadh_orchestrator.adapters import PatchGenerationAdapter, PatchGeneratorProvider
from project.schemas import PatchGenerationRequest, PatchGenerationResult

class MyA4PatchAgent:
    """Future teammate-owned A4 implementation."""

    def generate_patch(
        self,
        request: PatchGenerationRequest,
    ) -> PatchGenerationResult:
        # Inspect request.acceptance_criteria, request.business_constraints,
        # request.codebase_context, request.relevant_source_files, and
        # request.allowed_modification_paths.
        # Return a validated PatchGenerationResult (never touch disk directly).
        ...
```

### Dependency Injection in FastAPI / Orchestrator

```python
from project.main import create_app

app = create_app(
    config,
    patch_production_provider=MyA4PatchAgent(),
)
```

Or directly via `OrchestratorService`:

```python
adapter = PatchGenerationAdapter(
    config=config,
    production_provider=MyA4PatchAgent(),
)
orchestrator = OrchestratorService(..., patch_adapter=adapter)
```

### Built-In Offline Test Fixture (Testing / Offline Demo Only)
- `DeterministicPremiumDiscountPatchFixture` is provided **solely** for automated tests and offline demos.
- It is **never** enabled by default and **never** invoked in `execution_mode="live_llm"`.
- Every fixture result explicitly sets `is_deterministic_fixture=True`, `is_mock_output=True`, `model_metadata.provider="offline_fixture"`, and `model_label="deterministic_offline_patch_fixture"`.

---

## 5. Patch Safety Rules (`project/marsadh_orchestrator/patch_engine.py`)

Before any byte is modified inside the isolated workspace, `PatchValidator` enforces:
1. **Patch hash verification:** `patch_sha256 == sha256(unified_diff)`.
2. **Size & file limits:** Default `max_patch_size_bytes=100_000` and `max_patch_changed_files=10` (configurable via `OrchestratorConfig` / `CHANGEGUARD_MAX_PATCH_SIZE_BYTES` / `CHANGEGUARD_MAX_PATCH_CHANGED_FILES`).
3. **Strict unified diff syntax:** Valid `diff --git` / `---` / `+++` headers, header path parity (prevents header smuggling), valid `@@` hunk headers, exact hunk line counts, no NUL bytes.
4. **Binary & special mode rejection:** Rejects `GIT binary patch`, `Binary files ... differ`, symlink modes (`120000`), and gitlink/submodule modes (`160000`).
5. **Path traversal & absolute paths:** Rejects `..`, leading `/` or `\`, Windows drive prefixes (`C:`), and empty/`.` paths.
6. **Protected files & directories:** Rejects `.git/**`, `.github/**`, `.changeguard/**`, `.changeguard_generated_tests/**`, `.env*`, `*.pem`, `*.key`, `*secret*`, `*credential*`, `id_rsa*`, `id_ed25519*`, `.ssh/**`, `.aws/**`, `project/marsadh_orchestrator/**`, `project/api/**`, `project/schemas/**`, `project/main.py`, `project/agents/**`, `project/nafris_verification/**`, `frontend/**`, `pyproject.toml`, `Dockerfile`, `Makefile`, etc.
7. **Allowed scope & `affected_files` parity:** Every touched file must lie inside `source_path` and `allowed_modification_paths`, and `PatchProposal.affected_files` must exactly match the files parsed from `unified_diff`.
8. **Symlink escape prevention:** Every parent component and target file is checked with `is_symlink()` and resolved containment inside `workspace_repo` and `source_root`.
9. **Workspace containment & integrity:** Refuses to operate outside `config.workspace_root`; verifies workspace snapshot and diff match expected pre-patch hashes.
10. **Acceptance criterion references:** Rejects any `criterion_id` not present in A1's `RequirementAnalysisResult`.
11. **Applicability check:** Runs `git -C <workspace> apply --check -` with safe list args (no `shell=True`), explicit `cwd`, and bounded `git_timeout_seconds`.

Before applying, `PatchApplicator` **revalidates** the patch SHA-256, workspace containment, pre-patch snapshot/diff hashes, and symlink safety, captures exact pre-apply file backups, runs `git apply -` (never `--reject`), registers newly added files with `git add -N` so M01 sees them, and recomputes resulting snapshot and diff hashes. If application fails for any reason, backups are restored immediately so partially applied changes never reach verification.

---

## 6. Workspace Isolation & M01 Limitations

1. **Git workspace isolation is NOT an OS security sandbox:** `WorkspaceManager` copies tracked files into a run-specific temporary directory inside `workspace_root` to protect the original repository from mutation. It does not provide container, namespace, seccomp, or VM process isolation. Test execution remains restricted to explicitly configured `trusted_execution_roots`.
2. **M01 pre-change impact limitation:** Mujahidh's `project/agents/mujahidh_codebase/codebase_agent.py` is diff-driven (`git diff`) and does not predict impact before code changes exist. In `generate_and_verify_change`, M02 uses M01's `analyze_repository()` for pre-change static AST indexing (`scope="static_repository_index_only"`, `pre_change_impact_prediction_available=False`) and runs full reverse-dependency impact analysis (`run_codebase_agent`) **after** the patch is validated and applied.
3. **No production A4, bounded repair loop, or baseline comparison yet:** `max_repair_attempts` remains reserved for a future milestone; P0-6 executes a single controlled patch attempt (`attempt_number=1`) without automatic repair retries or automated Git commit/merge/push/deploy.

---

## 7. Example Request & Response

### Request (`POST /api/runs`)
```json
{
  "repository_id": "clean-mini-pos",
  "source_path": "mini_pos",
  "base_ref": "HEAD",
  "requirements": "Apply a 10% discount to premium customers only, without changing regular customer checkout or breaking dependent refund, dashboard, and report modules.",
  "workflow_mode": "generate_and_verify_change",
  "execution_mode": "offline_test"
}
```

### Response Excerpt (`GET /api/runs/{run_id}` at Gate 1 — Offline Fixture Demonstration)
```json
{
  "run_id": "run_20261127T093000Z_a1b2c3d4e5f6",
  "state": "AWAITING_TEST_REVIEW",
  "workflow_mode": "generate_and_verify_change",
  "execution_mode": "offline_test",
  "original_source_snapshot_sha256": "<pre-patch sha256>",
  "source_snapshot_sha256": "<post-patch sha256>",
  "patch_proposal_sha256": "<patch sha256>",
  "patch_attempt_number": 1,
  "allowed_actions": ["submit_test_review"],
  "pre_change_context": {
    "status": "completed",
    "scope": "static_repository_index_only",
    "pre_change_impact_prediction_available": false,
    "total_indexed_files": 5
  },
  "patch_generation": {
    "status": "proposed",
    "attempt_number": 1,
    "is_deterministic_fixture": true,
    "is_mock_output": true,
    "model_metadata": {
      "provider": "offline_fixture",
      "model_name": "deterministic_premium_discount_fixture",
      "model_label": "deterministic_offline_patch_fixture",
      "generation_mode": "offline_test",
      "is_deterministic_fixture": true,
      "is_mock_output": true
    }
  },
  "patch_validation": {
    "valid": true,
    "status": "passed",
    "git_apply_check_passed": true,
    "checked_files": ["mini_pos/billing.py"],
    "violations": []
  },
  "patch_application": {
    "applied": true,
    "status": "applied",
    "applied_files": ["mini_pos/billing.py"],
    "rollback_performed": false
  }
}
```

---

## 8. Required Frontend Integration Changes (`frontend/`, Owner: Nafris)

1. **Workflow Mode Selector:** Add an explicit `workflow_mode` selector (`verify_existing_change` vs `generate_and_verify_change`) on the New Run form, kept separate from `execution_mode` (`offline_test` vs `live_llm`). Check `GET /health -> patch_generator_configured` to show whether production A4 is connected.
2. **Active Polling States:** Add `COLLECTING_PRE_CHANGE_CONTEXT`, `GENERATING_PATCH`, `VALIDATING_PATCH`, `APPLYING_PATCH`, and `ANALYZING_POST_PATCH_IMPACT` to the active polling state set.
3. **Patch Review & Provenance Panel:** Render `pre_change_context` (with explicit static-index scope badge), `patch_generation` (unified diff viewer, rationale, `related_criterion_ids`, `assumptions`, `risk_indicators`, and prominent **Offline Deterministic Fixture** badge whenever `is_deterministic_fixture === true`), `patch_validation` (`checks_passed` / `violations`), and `patch_application` (`original_source_snapshot_sha256` -> `resulting_source_snapshot_sha256`, `isolation_note`).
4. **Error & Block Handling:** Surface structured error codes cleanly (`PATCH_GENERATOR_UNAVAILABLE`, `UNRESOLVED_REQUIREMENT_AMBIGUITY`, `PATCH_PROTECTED_PATH`, `PATCH_NOT_APPLICABLE`, `PATCH_APPLICATION_FAILED`).

---

## 9. Testing & Reproducibility Instructions

Run the full automated test suite offline (no API keys or network access required):

```powershell
.\.venv\Scripts\python.exe -m pytest -v --tb=short
```

Run only the P0-6 Patch Generation Integration suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_m02_patch_generation_integration.py -v --tb=short
```

Run CI-equivalent dependency, lint, and diff hygiene checks:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\ruff.exe check --select E9,F63,F7,F82,F811,F404,F406,F407,F601,F602 --exclude .git,.venv,venv,env,node_modules,__pycache__,.pytest_cache,build,dist,tests/fixtures/invalid .
git diff --check
```
