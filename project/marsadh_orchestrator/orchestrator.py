"""Deterministic central orchestration engine for ChangeGuard AI (Module 02).

Responsibilities:
- Enforce validated, persisted workflow state transitions with atomic expected-state
  compare-and-set updates via ``RunStore``.
- Coordinate M01 (CodebaseAdapter), M03 (TestingAdapter), and M04 (VerificationAdapter)
  inside isolated Git working copies managed by ``WorkspaceManager``.
- Enforce human-in-the-loop review gates before test execution and before final
  change-package sign-off.
- Verify generated scenario content hashes and workspace source/diff snapshots
  before exporting or executing reviewed tests.
- Compile auditable ``ChangePackageReport`` artifacts without fabricating coverage,
  baseline regression deltas, or live AI metrics.
- Never automatically commit, merge, push, deploy, or mutate the source repository.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePath
from typing import Any

from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
    build_approved_test_specs,
)
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import RunStore, generate_run_id, utc_now
from project.marsadh_orchestrator.workspace import (
    EXCLUDED_DIR_NAMES,
    UnsupportedWorkspaceStateError,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceSecurityError,
)
from project.schemas import (
    AgentError,
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentLogEntry,
    ApprovedTestSpec,
    ChangePackageReport,
    CodebaseAnalysisResult,
    CreateRunRequest,
    ExecutionMode,
    HumanDecision,
    HumanDecisionRecord,
    HumanDecisionRequest,
    RepositoryInfo,
    RiskSummary,
    TestGenerationResult,
    TestReviewRecord,
    TestReviewRequest,
    VerificationOutcome,
    VerificationResult,
    WorkflowRunRecord,
    WorkflowState,
)

GENERATED_TESTS_SUBDIR = ".changeguard_generated_tests"

TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.COMPLETED_NO_CHANGES,
        WorkflowState.APPROVED,
        WorkflowState.REJECTED,
        WorkflowState.NEEDS_REVIEW,
        WorkflowState.FAILED,
    }
)

PAUSED_REVIEW_STATES: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.AWAITING_TEST_REVIEW,
        WorkflowState.AWAITING_FINAL_APPROVAL,
    }
)

ACTIVE_EXECUTION_STATES: frozenset[WorkflowState] = frozenset(
    {
        WorkflowState.CREATED,
        WorkflowState.ANALYZING_CODEBASE,
        WorkflowState.GENERATING_TESTS,
        WorkflowState.RUNNING_VERIFICATION,
        WorkflowState.COMPILING_REPORT,
    }
)

ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset(
        {
            WorkflowState.ANALYZING_CODEBASE,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.ANALYZING_CODEBASE: frozenset(
        {
            WorkflowState.GENERATING_TESTS,
            WorkflowState.COMPLETED_NO_CHANGES,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.GENERATING_TESTS: frozenset(
        {
            WorkflowState.AWAITING_TEST_REVIEW,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.AWAITING_TEST_REVIEW: frozenset(
        {
            WorkflowState.RUNNING_VERIFICATION,
            WorkflowState.REJECTED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.RUNNING_VERIFICATION: frozenset(
        {
            WorkflowState.COMPILING_REPORT,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.COMPILING_REPORT: frozenset(
        {
            WorkflowState.AWAITING_FINAL_APPROVAL,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.AWAITING_FINAL_APPROVAL: frozenset(
        {
            WorkflowState.APPROVED,
            WorkflowState.REJECTED,
            WorkflowState.NEEDS_REVIEW,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.COMPLETED_NO_CHANGES: frozenset(),
    WorkflowState.APPROVED: frozenset(),
    WorkflowState.REJECTED: frozenset(),
    WorkflowState.NEEDS_REVIEW: frozenset(),
    WorkflowState.FAILED: frozenset(),
}


# ---------------------------------------------------------------------------
# Typed Orchestrator Exceptions (mapped to safe HTTP status codes in API layer)
# ---------------------------------------------------------------------------


class OrchestratorError(Exception):
    """Base structured error raised by the orchestration service."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "ORCHESTRATOR_ERROR",
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.run_id = run_id


class RunNotFoundError(OrchestratorError):
    """Raised when a requested workflow run ID does not exist (HTTP 404)."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"Workflow run '{run_id}' was not found.",
            code="RUN_NOT_FOUND",
            run_id=run_id,
        )


class InvalidStateTransitionError(OrchestratorError):
    """Raised when a workflow state transition violates ALLOWED_TRANSITIONS (HTTP 409)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_STATE_TRANSITION",
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, run_id=run_id)


class ReviewConflictError(InvalidStateTransitionError):
    """Raised on duplicate reviews, concurrent approvals, or blocked decisions (HTTP 409)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "REVIEW_CONFLICT",
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, run_id=run_id)


class WorkflowIntegrityError(InvalidStateTransitionError):
    """Raised when generated test content or workspace source snapshot was modified (HTTP 409)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "WORKFLOW_INTEGRITY_ERROR",
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, run_id=run_id)


class RepositoryAccessDeniedError(OrchestratorError):
    """Raised when a repository path or identifier is outside permitted roots (HTTP 403)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "REPOSITORY_ACCESS_DENIED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class ValidationInputError(OrchestratorError):
    """Raised when request references invalid run-scoped data such as unknown scenario IDs (HTTP 422)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INVALID_INPUT",
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, run_id=run_id)


class InfrastructureUnavailableError(OrchestratorError):
    """Raised when SQLite storage or required local infrastructure is unavailable (HTTP 503)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INFRASTRUCTURE_UNAVAILABLE",
        details: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details, run_id=run_id)


# ---------------------------------------------------------------------------
# Helpers: Transition validation, snapshots, diff hashing, offline stub
# ---------------------------------------------------------------------------


def validate_transition(
    current: WorkflowState,
    target: WorkflowState,
    *,
    run_id: str | None = None,
) -> None:
    """Validate that ``current -> target`` is permitted by ALLOWED_TRANSITIONS."""
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransitionError(
            f"Transition from '{current.value}' to '{target.value}' is not permitted.",
            details={
                "current_state": current.value,
                "target_state": target.value,
                "allowed_targets": sorted(state.value for state in allowed),
            },
            run_id=run_id,
        )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _truncate_summary(text: str, limit: int = 1800) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 15] + "...[truncated]"


def compute_source_snapshot_sha256(
    workspace_repo_path: str | Path,
    source_path: str = ".",
) -> str:
    """Compute a deterministic SHA-256 fingerprint of source files under source_path.

    Excludes ``.git``, caches, virtual environments, and ``.changeguard_generated_tests``
    so exporting approved tests does not mutate the recorded source snapshot hash.
    """
    repo_root = Path(workspace_repo_path).resolve(strict=True)
    target_dir = repo_root if source_path == "." else (repo_root / source_path).resolve(strict=True)
    if not target_dir.is_relative_to(repo_root) or not target_dir.is_dir():
        raise WorkspaceSecurityError(
            f"Source snapshot target escapes or is missing inside workspace: '{source_path}'"
        )

    excluded = set(EXCLUDED_DIR_NAMES) | {GENERATED_TESTS_SUBDIR}
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(target_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        base = Path(dirpath)
        for fname in sorted(filenames):
            fpath = base / fname
            if fpath.is_symlink():
                continue
            rel = fpath.relative_to(repo_root).as_posix()
            entries.append((rel, fpath))

    digest = hashlib.sha256()
    for rel, fpath in sorted(entries, key=lambda item: item[0]):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(fpath.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def compute_git_diff_sha256(
    workspace_repo_path: str | Path,
    base_ref: str,
    source_path: str = ".",
    *,
    timeout_seconds: int = 30,
) -> str:
    """Compute a deterministic SHA-256 digest of the workspace Git diff under source_path."""
    repo_root = Path(workspace_repo_path).resolve(strict=True)
    pathspec = "." if source_path == "." else source_path
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--no-ext-diff",
                base_ref,
                "--",
                pathspec,
            ],
            capture_output=True,
            timeout=timeout_seconds,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"Git diff snapshot timed out after {timeout_seconds}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or exc)
        ).strip()
        raise WorkspaceError(f"Failed to hash workspace Git diff: {stderr}") from exc
    except OSError as exc:
        raise WorkspaceError(f"Failed to execute git for diff snapshot: {exc}") from exc

    return hashlib.sha256(proc.stdout).hexdigest()


def default_offline_model_stub(prompt: str) -> str:
    """Deterministic offline mock model stub grounded in the M01 impact payload.

    Explicitly labeled as mock output (``is_mock_output=True``) and intended solely
    for offline testing and local demonstrations without live LLM credentials.
    """
    marker = "\nDATA:\n"
    data: dict[str, Any] = {}
    if marker in prompt:
        raw_json = prompt.split(marker, 1)[1]
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            data = {}

    impact_report = data.get("impact_report", {})
    source_path = str(impact_report.get("source_path", ".")).replace("\\", "/")
    reports = impact_report.get("reports", []) if isinstance(impact_report, dict) else []

    scenarios: list[dict[str, str]] = []
    for report in reports:
        changed_file = str(report.get("changed_file", "")).replace("\\", "/")
        changed_funcs = [
            str(fn) for fn in report.get("changed_functions", []) if isinstance(fn, str)
        ]
        if not changed_file:
            continue

        rel_from_source = changed_file
        if source_path not in {"", "."} and changed_file.startswith(f"{source_path}/"):
            rel_from_source = changed_file[len(source_path) + 1 :]
        module_name = PurePath(rel_from_source).with_suffix("").as_posix().replace("/", ".")
        safe_stem = "".join(ch if ch.isalnum() else "_" for ch in PurePath(changed_file).stem)
        path_expr = (
            "Path(__file__).resolve().parents[1]"
            if source_path in {"", "."}
            else f"Path(__file__).resolve().parents[1] / {source_path!r}"
        )

        if changed_funcs:
            fn_name = changed_funcs[0]
            safe_fn = "".join(ch if ch.isalnum() else "_" for ch in fn_name)
            test_code = (
                "import importlib\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                f"sys.path.insert(0, str({path_expr}))\n\n"
                f"def test_offline_stub_{safe_stem}_{safe_fn}_callable():\n"
                f"    mod = importlib.import_module({module_name!r})\n"
                f"    assert hasattr(mod, {fn_name!r})\n"
                f"    assert callable(getattr(mod, {fn_name!r}))\n"
            )
            scenarios.append(
                {
                    "title": f"Verify modified function {module_name}.{fn_name} is importable and callable",
                    "rationale": (
                        f"M01 detected modifications in '{changed_file}' affecting "
                        f"function '{fn_name}'. Smoke-checks module import and symbol availability."
                    ),
                    "assumptions": (
                        "Generated by deterministic offline stub (not a live LLM); "
                        "checks symbol importability only and does not prove full requirement compliance. "
                        "Human review required."
                    ),
                    "test_code": test_code,
                }
            )
        else:
            test_code = (
                "import importlib\n"
                "import sys\n"
                "from pathlib import Path\n\n"
                f"sys.path.insert(0, str({path_expr}))\n\n"
                f"def test_offline_stub_{safe_stem}_imports():\n"
                f"    mod = importlib.import_module({module_name!r})\n"
                "    assert mod is not None\n"
            )
            scenarios.append(
                {
                    "title": f"Verify modified module {module_name} imports cleanly",
                    "rationale": f"M01 detected changes in '{changed_file}'.",
                    "assumptions": (
                        "Generated by deterministic offline stub (not a live LLM); "
                        "human review required."
                    ),
                    "test_code": test_code,
                }
            )

    if not scenarios:
        scenarios.append(
            {
                "title": "Deterministic offline placeholder scenario",
                "rationale": "Fallback offline stub when impact report had no function metadata.",
                "assumptions": "Deterministic offline mock output; human review required.",
                "test_code": "def test_offline_stub_placeholder():\n    assert True\n",
            }
        )

    return json.dumps({"scenarios": scenarios})


# ---------------------------------------------------------------------------
# Central Orchestration Service
# ---------------------------------------------------------------------------


class OrchestratorService:
    """Deterministic multi-agent workflow orchestrator for ChangeGuard AI (M02)."""

    def __init__(
        self,
        config: OrchestratorConfig,
        store: RunStore,
        workspace_manager: WorkspaceManager,
        codebase_adapter: CodebaseAdapter,
        testing_adapter: TestingAdapter,
        verification_adapter: VerificationAdapter,
        *,
        offline_model_callable: Callable[[str], str] | None = None,
        offline_model_label: str = "deterministic_offline_stub",
        live_model_callable: Callable[[str], str] | None = None,
        live_model_label: str | None = None,
        use_default_offline_stub: bool = True,
    ) -> None:
        self.config = config
        self.store = store
        self.workspace_manager = workspace_manager
        self.codebase_adapter = codebase_adapter
        self.testing_adapter = testing_adapter
        self.verification_adapter = verification_adapter
        self._offline_model_callable = (
            offline_model_callable
            if offline_model_callable is not None
            else (default_offline_model_stub if use_default_offline_stub else None)
        )
        self._offline_model_label = offline_model_label
        self._live_model_callable = live_model_callable
        self._live_model_label = live_model_label

    # ------------------------------------------------------------------
    # Persistence & structured logging helpers
    # ------------------------------------------------------------------

    def _log(
        self,
        *,
        run_id: str,
        agent_name: str,
        stage: WorkflowState,
        status: AgentExecutionStatus,
        duration_ms: int = 0,
        input_summary: str = "",
        output_summary: str = "",
        error: AgentError | None = None,
    ) -> None:
        entry = AgentLogEntry(
            run_id=run_id,
            agent_name=agent_name,
            stage=stage,
            status=status,
            duration_ms=max(0, int(duration_ms)),
            input_summary=_truncate_summary(input_summary),
            output_summary=_truncate_summary(output_summary),
            error=error,
        )
        try:
            self.store.append_log(entry)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to persist audit log entry: {exc}",
                run_id=run_id,
            ) from exc

    def transition_state(
        self,
        run_id: str,
        expected_state: WorkflowState,
        target_state: WorkflowState,
        *,
        updater: Callable[[WorkflowRunRecord], WorkflowRunRecord] | None = None,
        error: AgentError | None = None,
        reason: str = "",
    ) -> WorkflowRunRecord:
        """Atomically validate and persist a single workflow state transition."""
        validate_transition(expected_state, target_state, run_id=run_id)

        def _apply(current: WorkflowRunRecord) -> WorkflowRunRecord:
            validate_transition(current.state, target_state, run_id=run_id)
            base = updater(current) if updater is not None else current
            return base.model_copy(
                update={
                    "state": target_state,
                    "error": error if error is not None else base.error,
                }
            )

        try:
            updated = self.store.update_run_atomic(
                run_id,
                expected_state=expected_state,
                updater=_apply,
            )
        except KeyError as exc:
            raise RunNotFoundError(run_id) from exc
        except ValueError as exc:
            raise InvalidStateTransitionError(
                str(exc),
                details={
                    "expected_state": expected_state.value,
                    "target_state": target_state.value,
                },
                run_id=run_id,
            ) from exc
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Database error during state transition: {exc}",
                run_id=run_id,
            ) from exc

        log_status = (
            AgentExecutionStatus.FAILED
            if target_state == WorkflowState.FAILED
            else (
                AgentExecutionStatus.COMPLETED_NO_CHANGES
                if target_state == WorkflowState.COMPLETED_NO_CHANGES
                else AgentExecutionStatus.SUCCEEDED
            )
        )
        self._log(
            run_id=run_id,
            agent_name="OrchestratorService",
            stage=target_state,
            status=log_status,
            input_summary=f"transition:{expected_state.value}->{target_state.value}",
            output_summary=reason or f"State transitioned to {target_state.value}",
            error=error,
        )
        return updated

    def _fail_run(
        self,
        run_id: str,
        expected_state: WorkflowState,
        error: AgentError,
        *,
        updater: Callable[[WorkflowRunRecord], WorkflowRunRecord] | None = None,
        reason: str = "",
    ) -> WorkflowRunRecord:
        return self.transition_state(
            run_id,
            expected_state,
            WorkflowState.FAILED,
            updater=updater,
            error=error,
            reason=reason or error.message,
        )

    # ------------------------------------------------------------------
    # Read / Query methods
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> WorkflowRunRecord:
        """Retrieve a workflow run record by ID or raise RunNotFoundError."""
        try:
            run = self.store.get_run(run_id)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to read workflow run from database: {exc}",
                run_id=run_id,
            ) from exc
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    def list_runs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        state: WorkflowState | None = None,
    ) -> list[WorkflowRunRecord]:
        """List persisted workflow runs with bounded pagination and optional state filter."""
        try:
            return self.store.list_runs(limit=limit, offset=offset, state=state)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to list workflow runs from database: {exc}"
            ) from exc

    def count_runs(self, *, state: WorkflowState | None = None) -> int:
        """Count persisted runs matching an optional workflow state."""
        try:
            return self.store.count_runs(state=state)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to count workflow runs from database: {exc}"
            ) from exc

    def get_run_logs(self, run_id: str) -> list[AgentLogEntry]:
        """Return structured communication logs for an existing run in deterministic order."""
        # Confirm run exists first so unknown run IDs return 404.
        self.get_run(run_id)
        try:
            return self.store.get_logs(run_id)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to read communication logs from database: {exc}",
                run_id=run_id,
            ) from exc

    # ------------------------------------------------------------------
    # Conservative Restart Recovery (Phase 11)
    # ------------------------------------------------------------------

    def recover_interrupted_runs(self) -> list[WorkflowRunRecord]:
        """Mark runs interrupted mid-execution as FAILED without replaying side effects.

        Paused human-review runs (AWAITING_TEST_REVIEW, AWAITING_FINAL_APPROVAL)
        and terminal runs are left untouched so reviewers can resume after restart.
        """
        try:
            interrupted = self.store.list_runs_by_states(ACTIVE_EXECUTION_STATES)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to inspect runs during startup recovery: {exc}"
            ) from exc

        recovered: list[WorkflowRunRecord] = []
        for run in interrupted:
            err = AgentError(
                code="PROCESS_INTERRUPTED",
                message=(
                    f"Backend restarted while run was in active stage '{run.state.value}'. "
                    "External model calls and test execution were not replayed automatically."
                ),
                details={"interrupted_stage": run.state.value},
                retryable=False,
            )
            try:
                updated = self._fail_run(
                    run.run_id,
                    run.state,
                    err,
                    reason=f"Recovered interrupted run from stage {run.state.value}",
                )
                recovered.append(updated)
            except InvalidStateTransitionError:
                # Another worker already transitioned this run.
                continue
        return recovered

    # ------------------------------------------------------------------
    # Phase 3, 4, 5: Creation, Workspace Preparation, M01 Analysis, M03 Generation
    # ------------------------------------------------------------------

    def create_run(self, request: CreateRunRequest) -> WorkflowRunRecord:
        """Validate repository access, generate run ID, and persist CREATED run record."""
        try:
            resolved_repo = self.config.resolve_repository(
                repository_path=request.repository_path,
                repository_id=request.repository_id,
            )
        except ValueError as exc:
            raise RepositoryAccessDeniedError(
                str(exc),
                details={
                    "repository_id": request.repository_id,
                },
            ) from exc

        run_id = generate_run_id()
        record = WorkflowRunRecord(
            run_id=run_id,
            state=WorkflowState.CREATED,
            requirements=request.requirements,
            execution_mode=request.execution_mode,
            repository=RepositoryInfo(
                repository_id=request.repository_id,
                repository_path=str(resolved_repo),
                source_path=request.source_path,
                base_ref=request.base_ref,
                change_mode="unknown",
            ),
        )
        try:
            saved = self.store.save_run(record)
        except sqlite3.Error as exc:
            raise InfrastructureUnavailableError(
                f"Failed to persist newly created run: {exc}",
                run_id=run_id,
            ) from exc

        self._log(
            run_id=run_id,
            agent_name="OrchestratorService",
            stage=WorkflowState.CREATED,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"repo_id={request.repository_id or '<path>'}, "
                f"source_path={request.source_path}, base_ref={request.base_ref}, "
                f"mode={request.execution_mode.value}"
            ),
            output_summary="Workflow run created and persisted.",
        )
        return saved

    def start_run(
        self,
        request: CreateRunRequest,
        *,
        execute_immediately: bool = True,
    ) -> WorkflowRunRecord:
        """Create a workflow run and optionally execute analysis + test generation immediately."""
        run = self.create_run(request)
        if not execute_immediately:
            return run
        return self.execute_analysis_and_generation(run.run_id)

    def execute_analysis_and_generation(self, run_id: str) -> WorkflowRunRecord:
        """Run workspace preparation, M01 codebase analysis, and M03 test generation.

        Always stops at ``AWAITING_TEST_REVIEW`` (or ``COMPLETED_NO_CHANGES`` / ``FAILED``).
        Never executes generated tests.
        """
        run = self.get_run(run_id)
        if run.state != WorkflowState.CREATED:
            raise InvalidStateTransitionError(
                f"Run '{run_id}' cannot start analysis from state '{run.state.value}'.",
                run_id=run_id,
            )

        # ---- Stage 0: Workspace Preparation ----
        ws_started = utc_now()
        t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="WorkspaceManager",
            stage=WorkflowState.CREATED,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Preparing isolated workspace for source_path={run.repository.source_path}, "
                f"base_ref={run.repository.base_ref}"
            ),
            output_summary="Started workspace preparation.",
        )
        try:
            ws_ctx = self.workspace_manager.prepare_workspace(
                run_id,
                run.repository.repository_path,
                source_path=run.repository.source_path,
                base_ref=run.repository.base_ref,
            )
            snapshot_sha = compute_source_snapshot_sha256(
                ws_ctx.workspace_repo_path,
                ws_ctx.source_path,
            )
            diff_sha = compute_git_diff_sha256(
                ws_ctx.workspace_repo_path,
                ws_ctx.effective_base_ref,
                ws_ctx.source_path,
                timeout_seconds=self.config.git_timeout_seconds,
            )
        except (WorkspaceSecurityError, UnsupportedWorkspaceStateError, WorkspaceError) as exc:
            dur_ms = int((time.perf_counter() - t0) * 1000)
            err_code = (
                "WORKSPACE_SECURITY_ERROR"
                if isinstance(exc, WorkspaceSecurityError)
                else (
                    "UNSUPPORTED_WORKSPACE_STATE"
                    if isinstance(exc, UnsupportedWorkspaceStateError)
                    else "WORKSPACE_PREPARATION_FAILED"
                )
            )
            err = AgentError(code=err_code, message=str(exc))
            self._log(
                run_id=run_id,
                agent_name="WorkspaceManager",
                stage=WorkflowState.CREATED,
                status=AgentExecutionStatus.FAILED,
                duration_ms=dur_ms,
                input_summary=f"source_path={run.repository.source_path}, base_ref={run.repository.base_ref}",
                output_summary="Workspace preparation failed.",
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.CREATED, err)
        except Exception as exc:  # noqa: BLE001
            dur_ms = int((time.perf_counter() - t0) * 1000)
            err = AgentError(
                code="WORKSPACE_UNEXPECTED_ERROR",
                message=f"Unexpected workspace preparation error: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="WorkspaceManager",
                stage=WorkflowState.CREATED,
                status=AgentExecutionStatus.FAILED,
                duration_ms=dur_ms,
                output_summary="Workspace preparation raised unexpected exception.",
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.CREATED, err)

        ws_dur_ms = int((time.perf_counter() - t0) * 1000)
        ws_record = AgentExecutionRecord(
            agent_name="WorkspaceManager",
            stage=WorkflowState.CREATED.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=ws_started,
            completed_at=utc_now(),
            duration_ms=ws_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=(
                f"Prepared isolated workspace '{ws_ctx.workspace_id}' "
                f"(change_mode={ws_ctx.change_mode}, base={ws_ctx.resolved_base_commit[:12]})."
            ),
            assumptions=list(ws_ctx.warnings),
        )
        self._log(
            run_id=run_id,
            agent_name="WorkspaceManager",
            stage=WorkflowState.CREATED,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=ws_dur_ms,
            input_summary=f"source_path={ws_ctx.source_path}, base_ref={ws_ctx.base_ref}",
            output_summary=(
                f"Workspace ready: change_mode={ws_ctx.change_mode}, "
                f"base_commit={ws_ctx.resolved_base_commit[:12]}, "
                f"snapshot={snapshot_sha[:12]}, diff={diff_sha[:12]}"
            ),
        )

        # Persist workspace context & snapshot hashes and transition CREATED -> ANALYZING_CODEBASE
        run = self.transition_state(
            run_id,
            WorkflowState.CREATED,
            WorkflowState.ANALYZING_CODEBASE,
            updater=lambda current: current.model_copy(
                update={
                    "workspace_id": ws_ctx.workspace_id,
                    "workspace_path": ws_ctx.workspace_repo_path,
                    "source_snapshot_sha256": snapshot_sha,
                    "git_diff_sha256": diff_sha,
                    "repository": current.repository.model_copy(
                        update={
                            "source_path": ws_ctx.source_path,
                            "base_ref": ws_ctx.base_ref,
                            "effective_base_ref": ws_ctx.effective_base_ref,
                            "resolved_base_commit": ws_ctx.resolved_base_commit,
                            "head_commit": ws_ctx.head_commit,
                            "change_mode": ws_ctx.change_mode,
                        }
                    ),
                    "agent_records": [*current.agent_records, ws_record],
                }
            ),
            reason="Workspace prepared; starting M01 codebase analysis.",
        )

        # ---- Stage 1: M01 Codebase Analysis ----
        m01_started = utc_now()
        m01_t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_CODEBASE,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Analyzing workspace source_path={ws_ctx.source_path} "
                f"against effective_base_ref={ws_ctx.effective_base_ref}"
            ),
            output_summary="Invoked M01 CodebaseAdapter.analyze.",
        )
        try:
            cb_result = self.codebase_adapter.analyze(
                ws_ctx.workspace_repo_path,
                source_path=ws_ctx.source_path,
                base_ref=ws_ctx.effective_base_ref,
            )
        except Exception as exc:  # noqa: BLE001
            m01_dur_ms = int((time.perf_counter() - m01_t0) * 1000)
            err = AgentError(
                code="M01_UNEXPECTED_ERROR",
                message=f"CodebaseAdapter raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m01_dur_ms,
                output_summary="M01 codebase analysis failed with exception.",
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.ANALYZING_CODEBASE, err)

        m01_dur_ms = int((time.perf_counter() - m01_t0) * 1000)
        if cb_result.status == "error":
            err = cb_result.error or AgentError(
                code="M01_ANALYSIS_ERROR",
                message="M01 codebase analysis returned an error status.",
            )
            m01_rec = AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE.value,
                status=AgentExecutionStatus.FAILED,
                started_at=m01_started,
                completed_at=utc_now(),
                duration_ms=m01_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=err.message,
                error=err,
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m01_dur_ms,
                input_summary=f"source_path={ws_ctx.source_path}, base_ref={ws_ctx.effective_base_ref}",
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(
                run_id,
                WorkflowState.ANALYZING_CODEBASE,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "codebase_analysis": cb_result,
                        "agent_records": [*current.agent_records, m01_rec],
                    }
                ),
            )

        if cb_result.status == "no_changes" or not cb_result.has_relevant_changes:
            m01_rec = AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE.value,
                status=AgentExecutionStatus.COMPLETED_NO_CHANGES,
                started_at=m01_started,
                completed_at=utc_now(),
                duration_ms=m01_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    "No tracked Python changes detected under source_path relative to base_ref; "
                    "M03 test generation and M04 verification were skipped."
                ),
                assumptions=list(cb_result.assumptions),
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE,
                status=AgentExecutionStatus.COMPLETED_NO_CHANGES,
                duration_ms=m01_dur_ms,
                input_summary=f"source_path={ws_ctx.source_path}, base_ref={ws_ctx.effective_base_ref}",
                output_summary=(
                    "Zero tracked Python changes detected. Workflow stopped without "
                    "invoking M03 or M04; this is not proof of verification."
                ),
            )
            return self.transition_state(
                run_id,
                WorkflowState.ANALYZING_CODEBASE,
                WorkflowState.COMPLETED_NO_CHANGES,
                updater=lambda current: current.model_copy(
                    update={
                        "codebase_analysis": cb_result,
                        "agent_records": [*current.agent_records, m01_rec],
                    }
                ),
                reason=(
                    "No relevant tracked Python changes detected relative to base_ref. "
                    "Skipped M03 test generation and M04 verification (not classified as passing verification)."
                ),
            )

        # M01 succeeded with changes
        m01_summary = (
            f"Detected {len(cb_result.changed_files_repo_relative)} changed file(s) "
            f"and {len(cb_result.impacted_modules_repo_relative)} impacted module(s)."
        )
        m01_rec = AgentExecutionRecord(
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_CODEBASE.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=m01_started,
            completed_at=utc_now(),
            duration_ms=m01_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=m01_summary,
            assumptions=list(cb_result.assumptions),
        )
        self._log(
            run_id=run_id,
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_CODEBASE,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=m01_dur_ms,
            input_summary=f"source_path={ws_ctx.source_path}, base_ref={ws_ctx.effective_base_ref}",
            output_summary=m01_summary,
        )

        run = self.transition_state(
            run_id,
            WorkflowState.ANALYZING_CODEBASE,
            WorkflowState.GENERATING_TESTS,
            updater=lambda current: current.model_copy(
                update={
                    "codebase_analysis": cb_result,
                    "agent_records": [*current.agent_records, m01_rec],
                }
            ),
            reason="M01 analysis completed with tracked changes; starting M03 test generation.",
        )

        # ---- Stage 2: M03 Test Generation ----
        m03_started = utc_now()
        m03_t0 = time.perf_counter()
        model_callable: Callable[[str], str] | None
        model_label: str | None
        if run.execution_mode == ExecutionMode.OFFLINE_TEST:
            model_callable = self._offline_model_callable
            model_label = self._offline_model_label
        else:
            # Never substitute an offline mock callable when LIVE_LLM is requested.
            model_callable = self._live_model_callable
            model_label = self._live_model_label

        self._log(
            run_id=run_id,
            agent_name="TestingAdapter",
            stage=WorkflowState.GENERATING_TESTS,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Generating unexecuted test proposals for "
                f"{len(cb_result.changed_files_repo_relative)} changed file(s) "
                f"in mode={run.execution_mode.value}."
            ),
            output_summary="Invoked M03 TestingAdapter.generate_proposals.",
        )
        try:
            gen_result = self.testing_adapter.generate_proposals(
                ws_ctx.workspace_repo_path,
                cb_result,
                run.requirements,
                execution_mode=run.execution_mode,
                model_callable=model_callable,
                model_label=model_label,
            )
        except Exception as exc:  # noqa: BLE001
            m03_dur_ms = int((time.perf_counter() - m03_t0) * 1000)
            err = AgentError(
                code="M03_UNEXPECTED_ERROR",
                message=f"TestingAdapter raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="TestingAdapter",
                stage=WorkflowState.GENERATING_TESTS,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m03_dur_ms,
                output_summary="M03 test generation failed with exception.",
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.GENERATING_TESTS, err)

        m03_dur_ms = int((time.perf_counter() - m03_t0) * 1000)
        if gen_result.status != "proposed" or not gen_result.scenarios:
            err = gen_result.error or AgentError(
                code="M03_GENERATION_FAILED",
                message=f"M03 test generation did not produce proposals (status='{gen_result.status}').",
            )
            m03_rec = AgentExecutionRecord(
                agent_name="TestingAdapter",
                stage=WorkflowState.GENERATING_TESTS.value,
                status=AgentExecutionStatus.FAILED,
                started_at=m03_started,
                completed_at=utc_now(),
                duration_ms=m03_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=err.message,
                error=err,
            )
            self._log(
                run_id=run_id,
                agent_name="TestingAdapter",
                stage=WorkflowState.GENERATING_TESTS,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m03_dur_ms,
                input_summary=f"mode={run.execution_mode.value}",
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(
                run_id,
                WorkflowState.GENERATING_TESTS,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "test_generation": gen_result,
                        "agent_records": [*current.agent_records, m03_rec],
                    }
                ),
            )

        # Validate scenario IDs and content fingerprints before pausing for human review
        seen_ids: set[str] = set()
        for scenario in gen_result.scenarios:
            if scenario.scenario_id in seen_ids:
                err = AgentError(
                    code="DUPLICATE_SCENARIO_ID",
                    message=f"Duplicate scenario identifier returned by M03: '{scenario.scenario_id}'",
                )
                return self._fail_run(run_id, WorkflowState.GENERATING_TESTS, err)
            seen_ids.add(scenario.scenario_id)
            expected_sha = _sha256_text(scenario.test_code)
            if not hmac.compare_digest(expected_sha, scenario.content_sha256):
                err = AgentError(
                    code="INVALID_SCENARIO_HASH",
                    message=f"Scenario '{scenario.scenario_id}' content hash mismatch.",
                )
                return self._fail_run(run_id, WorkflowState.GENERATING_TESTS, err)

        mock_note = "mock/offline" if gen_result.is_mock_output else "live_llm"
        m03_summary = (
            f"Generated {len(gen_result.scenarios)} unexecuted test proposal(s) "
            f"(model={gen_result.model_label}, source={mock_note}). "
            "Awaiting mandatory human review before export or execution."
        )
        m03_rec = AgentExecutionRecord(
            agent_name="TestingAdapter",
            stage=WorkflowState.GENERATING_TESTS.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=m03_started,
            completed_at=utc_now(),
            duration_ms=m03_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=m03_summary,
            assumptions=list(gen_result.assumptions),
        )
        self._log(
            run_id=run_id,
            agent_name="TestingAdapter",
            stage=WorkflowState.GENERATING_TESTS,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=m03_dur_ms,
            input_summary=f"mode={run.execution_mode.value}, model={gen_result.model_label}",
            output_summary=m03_summary,
        )

        # Pause workflow at Human Review Gate 1. No tests have been exported or executed.
        return self.transition_state(
            run_id,
            WorkflowState.GENERATING_TESTS,
            WorkflowState.AWAITING_TEST_REVIEW,
            updater=lambda current: current.model_copy(
                update={
                    "test_generation": gen_result,
                    "agent_records": [*current.agent_records, m03_rec],
                }
            ),
            reason="Test proposals generated; paused at Human Review Gate 1 (no tests executed).",
        )

    # ------------------------------------------------------------------
    # Integrity Verification Helpers (Phase 6 & 7)
    # ------------------------------------------------------------------

    def _verify_workspace_snapshot(self, run: WorkflowRunRecord) -> None:
        """Verify workspace still exists and matches the recorded source and diff fingerprints."""
        if not run.workspace_path:
            raise WorkflowIntegrityError(
                "Run workspace path is missing; cannot proceed safely.",
                code="WORKSPACE_MISSING",
                run_id=run.run_id,
            )
        ws_path = Path(run.workspace_path)
        if not ws_path.is_dir():
            raise WorkflowIntegrityError(
                "Prepared workspace directory no longer exists.",
                code="WORKSPACE_MISSING",
                run_id=run.run_id,
            )

        effective_base_ref = run.repository.effective_base_ref or run.repository.base_ref
        try:
            current_snapshot = compute_source_snapshot_sha256(
                ws_path, run.repository.source_path
            )
            current_diff = compute_git_diff_sha256(
                ws_path,
                effective_base_ref,
                run.repository.source_path,
                timeout_seconds=self.config.git_timeout_seconds,
            )
        except WorkspaceError as exc:
            raise WorkflowIntegrityError(
                f"Failed to verify workspace snapshot integrity: {exc}",
                code="SNAPSHOT_VERIFICATION_ERROR",
                run_id=run.run_id,
            ) from exc

        if run.source_snapshot_sha256 and not hmac.compare_digest(
            current_snapshot, run.source_snapshot_sha256
        ):
            raise WorkflowIntegrityError(
                "Target source snapshot changed after test generation; refusing to execute tests.",
                code="SOURCE_SNAPSHOT_MODIFIED",
                details={
                    "expected_snapshot_sha256": run.source_snapshot_sha256,
                    "actual_snapshot_sha256": current_snapshot,
                },
                run_id=run.run_id,
            )

        if run.git_diff_sha256 and not hmac.compare_digest(
            current_diff, run.git_diff_sha256
        ):
            raise WorkflowIntegrityError(
                "Workspace Git diff changed after test generation; refusing to execute tests.",
                code="GIT_DIFF_MODIFIED",
                details={
                    "expected_diff_sha256": run.git_diff_sha256,
                    "actual_diff_sha256": current_diff,
                },
                run_id=run.run_id,
            )

    def _verify_selected_scenarios_integrity(
        self,
        run: WorkflowRunRecord,
        selected_ids: list[str],
    ) -> None:
        """Verify selected scenario IDs belong to this run and match recorded SHA-256 hashes."""
        gen = run.test_generation
        if gen is None or gen.status != "proposed" or not gen.scenarios:
            raise ReviewConflictError(
                "Run has no proposed test scenarios available for review.",
                code="NO_PROPOSED_SCENARIOS",
                run_id=run.run_id,
            )
        by_id = {sc.scenario_id: sc for sc in gen.scenarios}
        unknown = [sc_id for sc_id in selected_ids if sc_id not in by_id]
        if unknown:
            raise ValidationInputError(
                f"Selected scenario identifier(s) do not belong to run '{run.run_id}': {unknown}",
                code="UNKNOWN_SCENARIO_ID",
                details={"unknown_scenario_ids": unknown},
                run_id=run.run_id,
            )
        for sc_id in selected_ids:
            scenario = by_id[sc_id]
            actual_sha = _sha256_text(scenario.test_code)
            if not hmac.compare_digest(actual_sha, scenario.content_sha256):
                raise WorkflowIntegrityError(
                    f"Generated test content for scenario '{sc_id}' was modified since generation; "
                    "refusing to approve or execute without renewed generation and review.",
                    code="TEST_CONTENT_MODIFIED",
                    details={
                        "scenario_id": sc_id,
                        "expected_sha256": scenario.content_sha256,
                        "actual_sha256": actual_sha,
                    },
                    run_id=run.run_id,
                )

    # ------------------------------------------------------------------
    # Phase 6: Human Review Gate 1
    # ------------------------------------------------------------------

    def accept_test_review(
        self,
        run_id: str,
        request: TestReviewRequest,
    ) -> tuple[WorkflowRunRecord, dict[str, ApprovedTestSpec] | None]:
        """Validate Gate 1 review, atomically claim transition, and export approved tests."""
        run = self.get_run(run_id)
        if run.state != WorkflowState.AWAITING_TEST_REVIEW:
            raise ReviewConflictError(
                f"Test review is only permitted in state 'AWAITING_TEST_REVIEW'; "
                f"run '{run_id}' is currently in state '{run.state.value}'.",
                code="INVALID_REVIEW_STATE",
                details={"current_state": run.state.value},
                run_id=run_id,
            )

        # Rejection path: atomically transition AWAITING_TEST_REVIEW -> REJECTED without export/execution
        if not request.approved:
            review_record = TestReviewRecord(
                approved=False,
                selected_test_ids=[],
                approved_test_hashes={},
                reviewer=request.reviewer,
                notes=request.notes,
            )
            try:
                updated = self.transition_state(
                    run_id,
                    WorkflowState.AWAITING_TEST_REVIEW,
                    WorkflowState.REJECTED,
                    updater=lambda current: current.model_copy(
                        update={"test_review": review_record}
                    ),
                    reason=(
                        f"Human reviewer '{request.reviewer}' rejected generated test proposals. "
                        "No tests were exported or executed."
                    ),
                )
            except InvalidStateTransitionError as exc:
                raise ReviewConflictError(
                    f"Conflicting or duplicate test review for run '{run_id}'.",
                    code="DUPLICATE_TEST_REVIEW",
                    run_id=run_id,
                ) from exc

            self._log(
                run_id=run_id,
                agent_name="HumanTestReviewGate",
                stage=WorkflowState.REJECTED,
                status=AgentExecutionStatus.SUCCEEDED,
                input_summary=f"reviewer={request.reviewer}, approved=False",
                output_summary=f"Generated tests rejected. Notes: {request.notes or '(none)'}",
            )
            return updated, None

        # Approval path: validate scenario IDs (422 if invalid) and integrity (409 + FAILED if tampered)
        self._verify_selected_scenarios_integrity_or_fail(run, request.selected_test_ids)
        self._verify_workspace_snapshot_or_fail(run, WorkflowState.AWAITING_TEST_REVIEW)

        initial_review = TestReviewRecord(
            approved=True,
            selected_test_ids=list(request.selected_test_ids),
            approved_test_hashes={},
            reviewer=request.reviewer,
            notes=request.notes,
        )

        # Atomically claim AWAITING_TEST_REVIEW -> RUNNING_VERIFICATION so concurrent approvals cannot race!
        try:
            claimed = self.transition_state(
                run_id,
                WorkflowState.AWAITING_TEST_REVIEW,
                WorkflowState.RUNNING_VERIFICATION,
                updater=lambda current: current.model_copy(
                    update={"test_review": initial_review}
                ),
                reason=(
                    f"Human reviewer '{request.reviewer}' approved "
                    f"{len(request.selected_test_ids)} test scenario(s); claimed verification slot."
                ),
            )
        except InvalidStateTransitionError as exc:
            raise ReviewConflictError(
                f"Duplicate or concurrent test review approval rejected for run '{run_id}'.",
                code="DUPLICATE_TEST_REVIEW",
                run_id=run_id,
            ) from exc

        self._log(
            run_id=run_id,
            agent_name="HumanTestReviewGate",
            stage=WorkflowState.RUNNING_VERIFICATION,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"reviewer={request.reviewer}, approved=True, "
                f"selected={request.selected_test_ids}"
            ),
            output_summary="Approved scenarios accepted; exporting selected tests for verification.",
        )

        # Export ONLY selected approved scenarios into isolated workspace
        assert claimed.workspace_path is not None
        assert claimed.test_generation is not None
        export_t0 = time.perf_counter()
        exported = self.testing_adapter.export_selected_scenarios(
            claimed.workspace_path,
            claimed.test_generation,
            selected_scenario_ids=request.selected_test_ids,
            output_subdir=GENERATED_TESTS_SUBDIR,
        )
        export_dur_ms = int((time.perf_counter() - export_t0) * 1000)
        if exported.status != "proposed" or exported.error is not None:
            err = exported.error or AgentError(
                code="TEST_EXPORT_FAILED",
                message="Failed to export approved test scenarios into isolated workspace.",
            )
            self._log(
                run_id=run_id,
                agent_name="TestingAdapter",
                stage=WorkflowState.RUNNING_VERIFICATION,
                status=AgentExecutionStatus.FAILED,
                duration_ms=export_dur_ms,
                input_summary=f"export selected={request.selected_test_ids}",
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(run_id, WorkflowState.RUNNING_VERIFICATION, err)
            return failed, None

        # Mark selected scenarios approved and build hash-verified ApprovedTestSpec registry
        selected_set = set(request.selected_test_ids)
        marked_scenarios = [
            sc.model_copy(update={"approved": sc.scenario_id in selected_set})
            for sc in exported.scenarios
        ]
        exported_marked = exported.model_copy(update={"scenarios": marked_scenarios})
        completed_review = initial_review.model_copy(
            update={"approved_test_hashes": dict(exported_marked.exported_hashes)}
        )

        try:
            approved_specs = build_approved_test_specs(exported_marked, completed_review)
        except ValueError as exc:
            err = AgentError(code="APPROVED_SPEC_BUILD_ERROR", message=str(exc))
            self._log(
                run_id=run_id,
                agent_name="TestingAdapter",
                stage=WorkflowState.RUNNING_VERIFICATION,
                status=AgentExecutionStatus.FAILED,
                duration_ms=export_dur_ms,
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(run_id, WorkflowState.RUNNING_VERIFICATION, err)
            return failed, None

        self._log(
            run_id=run_id,
            agent_name="TestingAdapter",
            stage=WorkflowState.RUNNING_VERIFICATION,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=export_dur_ms,
            input_summary=f"export selected={request.selected_test_ids}",
            output_summary=(
                f"Exported {len(approved_specs)} approved scenario file(s) with SHA-256 hashes."
            ),
        )

        updated = self.store.update_run_atomic(
            run_id,
            expected_state=WorkflowState.RUNNING_VERIFICATION,
            updater=lambda current: current.model_copy(
                update={
                    "test_generation": exported_marked,
                    "test_review": completed_review,
                }
            ),
        )
        return updated, approved_specs

    def _verify_selected_scenarios_integrity_or_fail(
        self,
        run: WorkflowRunRecord,
        selected_ids: list[str],
    ) -> None:
        try:
            self._verify_selected_scenarios_integrity(run, selected_ids)
        except WorkflowIntegrityError as exc:
            err = AgentError(code=exc.code, message=exc.message, details=exc.details)
            self._log(
                run_id=run.run_id,
                agent_name="IntegrityVerifier",
                stage=run.state,
                status=AgentExecutionStatus.FAILED,
                output_summary=exc.message,
                error=err,
            )
            self._fail_run(run.run_id, run.state, err, reason=exc.message)
            raise

    def _verify_workspace_snapshot_or_fail(
        self,
        run: WorkflowRunRecord,
        expected_state: WorkflowState,
    ) -> None:
        try:
            self._verify_workspace_snapshot(run)
        except WorkflowIntegrityError as exc:
            err = AgentError(code=exc.code, message=exc.message, details=exc.details)
            self._log(
                run_id=run.run_id,
                agent_name="IntegrityVerifier",
                stage=expected_state,
                status=AgentExecutionStatus.FAILED,
                output_summary=exc.message,
                error=err,
            )
            self._fail_run(run.run_id, expected_state, err, reason=exc.message)
            raise

    def submit_test_review(
        self,
        run_id: str,
        request: TestReviewRequest,
        *,
        execute_immediately: bool = True,
    ) -> WorkflowRunRecord:
        """Process Human Review Gate 1 and optionally execute M04 verification immediately."""
        updated, approved_specs = self.accept_test_review(run_id, request)
        if not request.approved or approved_specs is None or updated.state != WorkflowState.RUNNING_VERIFICATION:
            return updated
        if not execute_immediately:
            return updated
        return self.execute_verification_and_report(run_id, approved_specs)

    # ------------------------------------------------------------------
    # Phase 7 & 8: M04 Verification Execution & Change Package Compilation
    # ------------------------------------------------------------------

    def execute_verification_and_report(
        self,
        run_id: str,
        approved_specs: Mapping[str, ApprovedTestSpec] | None = None,
    ) -> WorkflowRunRecord:
        """Execute M04 verification for approved scenarios and compile ChangePackageReport."""
        run = self.get_run(run_id)
        if run.state != WorkflowState.RUNNING_VERIFICATION:
            raise InvalidStateTransitionError(
                f"Cannot execute verification when run '{run_id}' is in state '{run.state.value}'.",
                run_id=run_id,
            )

        if run.test_generation is None or run.test_review is None or not run.test_review.approved:
            err = AgentError(
                code="MISSING_TEST_APPROVAL",
                message="Approved test generation and human review records are required before verification.",
            )
            return self._fail_run(run_id, WorkflowState.RUNNING_VERIFICATION, err)

        # Re-verify workspace source snapshot right before executing tests
        try:
            self._verify_workspace_snapshot(run)
        except WorkflowIntegrityError as exc:
            err = AgentError(code=exc.code, message=exc.message, details=exc.details)
            self._log(
                run_id=run_id,
                agent_name="IntegrityVerifier",
                stage=WorkflowState.RUNNING_VERIFICATION,
                status=AgentExecutionStatus.FAILED,
                output_summary=exc.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.RUNNING_VERIFICATION, err)

        specs: Mapping[str, ApprovedTestSpec]
        if approved_specs is not None:
            specs = approved_specs
        else:
            try:
                specs = build_approved_test_specs(run.test_generation, run.test_review)
            except ValueError as exc:
                err = AgentError(code="APPROVED_SPEC_BUILD_ERROR", message=str(exc))
                return self._fail_run(run_id, WorkflowState.RUNNING_VERIFICATION, err)

        # Derive execution trust strictly from server configuration & validated source repository path.
        is_trusted_workspace = self.config.is_trusted_for_execution(
            run.repository.repository_path
        )
        assert run.workspace_path is not None

        m04_started = utc_now()
        m04_t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="VerificationAdapter",
            stage=WorkflowState.RUNNING_VERIFICATION,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Executing {len(run.test_review.selected_test_ids)} approved scenario(s) "
                f"(timeout={self.config.verification_timeout_seconds}s, "
                f"trusted={is_trusted_workspace})."
            ),
            output_summary="Invoked M04 VerificationAdapter.verify.",
        )

        try:
            ver_result = self.verification_adapter.verify(
                run.workspace_path,
                run.test_review.selected_test_ids,
                specs,
                timeout_seconds=self.config.verification_timeout_seconds,
                is_trusted_workspace=is_trusted_workspace,
                baseline_ref=run.repository.resolved_base_commit or run.repository.base_ref,
            )
        except Exception as exc:  # noqa: BLE001
            m04_dur_ms = int((time.perf_counter() - m04_t0) * 1000)
            err = AgentError(
                code="M04_UNEXPECTED_ERROR",
                message=f"VerificationAdapter raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="VerificationAdapter",
                stage=WorkflowState.RUNNING_VERIFICATION,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m04_dur_ms,
                output_summary="M04 verification failed with unexpected exception.",
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.RUNNING_VERIFICATION, err)

        m04_dur_ms = int((time.perf_counter() - m04_t0) * 1000)
        m04_exec_status = (
            AgentExecutionStatus.TIMEOUT
            if ver_result.verification_outcome == VerificationOutcome.TIMEOUT
            else (
                AgentExecutionStatus.SUCCEEDED
                if ver_result.execution_status == "completed"
                else AgentExecutionStatus.FAILED
            )
        )
        m04_summary = (
            f"Verification outcome={ver_result.verification_outcome.value}, "
            f"passed_flag={ver_result.verification_passed}, "
            f"exit_code={ver_result.exit_code}, "
            f"total={ver_result.total_tests}, passed={ver_result.passed}, failed={ver_result.failed}."
        )
        m04_rec = AgentExecutionRecord(
            agent_name="VerificationAdapter",
            stage=WorkflowState.RUNNING_VERIFICATION.value,
            status=m04_exec_status,
            started_at=m04_started,
            completed_at=utc_now(),
            duration_ms=m04_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=m04_summary,
            error=ver_result.error,
            assumptions=list(ver_result.assumptions),
        )
        self._log(
            run_id=run_id,
            agent_name="VerificationAdapter",
            stage=WorkflowState.RUNNING_VERIFICATION,
            status=m04_exec_status,
            duration_ms=m04_dur_ms,
            input_summary=f"scenarios={run.test_review.selected_test_ids}",
            output_summary=m04_summary,
            error=ver_result.error,
        )

        # Hard integrity failure on exported test files stops the workflow immediately.
        if ver_result.error is not None and ver_result.error.code in {
            "TEST_CONTENT_MODIFIED",
            "UNAUTHORIZED_TEST_PATH",
            "TEST_FILE_MISSING",
            "WORKSPACE_NOT_FOUND",
        }:
            return self._fail_run(
                run_id,
                WorkflowState.RUNNING_VERIFICATION,
                ver_result.error,
                updater=lambda current: current.model_copy(
                    update={
                        "verification": ver_result,
                        "agent_records": [*current.agent_records, m04_rec],
                    }
                ),
                reason=ver_result.error.message,
            )

        # Transition RUNNING_VERIFICATION -> COMPILING_REPORT
        run = self.transition_state(
            run_id,
            WorkflowState.RUNNING_VERIFICATION,
            WorkflowState.COMPILING_REPORT,
            updater=lambda current: current.model_copy(
                update={
                    "verification": ver_result,
                    "agent_records": [*current.agent_records, m04_rec],
                }
            ),
            reason="Verification completed; compiling auditable change package report.",
        )

        # ---- Stage 4: Compile Auditable Change Package ----
        report_t0 = time.perf_counter()
        try:
            logs = self.store.get_logs(run_id)
            report = self._compile_change_package(run, ver_result, logs)
        except Exception as exc:  # noqa: BLE001
            err = AgentError(
                code="REPORT_COMPILATION_ERROR",
                message=f"Failed to compile auditable change package: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="ChangePackageCompiler",
                stage=WorkflowState.COMPILING_REPORT,
                status=AgentExecutionStatus.FAILED,
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.COMPILING_REPORT, err)

        report_dur_ms = int((time.perf_counter() - report_t0) * 1000)
        self._log(
            run_id=run_id,
            agent_name="ChangePackageCompiler",
            stage=WorkflowState.COMPILING_REPORT,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=report_dur_ms,
            input_summary=f"verification_outcome={ver_result.verification_outcome.value}",
            output_summary=(
                f"Compiled change package '{report.package_id}' "
                f"(recommendation_label={report.recommendation_label})."
            ),
        )

        return self.transition_state(
            run_id,
            WorkflowState.COMPILING_REPORT,
            WorkflowState.AWAITING_FINAL_APPROVAL,
            updater=lambda current: current.model_copy(update={"report": report}),
            reason=(
                f"Auditable change package compiled ({report.recommendation_label}); "
                "awaiting final human decision."
            ),
        )

    def _compile_change_package(
        self,
        run: WorkflowRunRecord,
        verification: VerificationResult,
        logs: list[AgentLogEntry],
    ) -> ChangePackageReport:
        """Aggregate verified run evidence into an auditable ChangePackageReport."""
        cb = run.codebase_analysis
        gen = run.test_generation
        review = run.test_review

        changed_files = cb.changed_files_repo_relative if cb else []
        impacted_modules = cb.impacted_modules_repo_relative if cb else []
        proposed_ids = [sc.scenario_id for sc in gen.scenarios] if gen else []
        approved_ids = list(review.selected_test_ids) if review and review.approved else []
        executed_ids = list(verification.executed_test_ids)

        outcome = verification.verification_outcome
        if verification.verification_passed and outcome == VerificationOutcome.PASSED:
            recommendation = "needs_review"
            recommendation_label = "ready_for_human_review"
            recommendation_summary = (
                "All approved generated tests passed against the modified working copy. "
                "Ready for human review; passing generated tests alone is not proof of full regression safety."
            )
        elif outcome == VerificationOutcome.FAILED_TESTS:
            recommendation = "reject"
            recommendation_label = "verification_failed"
            recommendation_summary = (
                f"Verification failed: {verification.failed or len(verification.failures)} test(s) failed. "
                "Inspect failure diagnostics before considering any change acceptance."
            )
        elif outcome in {
            VerificationOutcome.COLLECTION_ERROR,
            VerificationOutcome.NO_TESTS_COLLECTED,
            VerificationOutcome.TIMEOUT,
            VerificationOutcome.EXECUTION_ERROR,
            VerificationOutcome.UNAVAILABLE,
        }:
            recommendation = "needs_review"
            recommendation_label = "verification_incomplete"
            recommendation_summary = (
                f"Verification is incomplete (outcome='{outcome.value}'). "
                "Ordinary approval is blocked until valid test verification completes."
            )
        else:
            recommendation = "needs_review"
            recommendation_label = "additional_review_required"
            recommendation_summary = "Additional human review is required."

        limitations: list[str] = [
            "Baseline-versus-modified regression comparison was not executed; passing generated tests on the modified working copy alone does not guarantee complete regression safety.",
            "Structured requirement decomposition (A1), automated patch generation (A4), independent adversarial review (A6), and code coverage measurement are not executed in this M02 MVP.",
            "Reviewer identity labels are unauthenticated local MVP inputs and do not constitute cryptographic or SSO identity proof.",
        ]
        if gen is not None and gen.is_mock_output:
            limitations.append(
                f"Proposed tests were produced by an offline mock/stub model ('{gen.model_label}', is_mock_output=True) and must not be presented as live LLM performance."
            )
        if verification.coverage_status == "unavailable":
            limitations.append("Code coverage percentage is unavailable (not measured).")
        if verification.ai_root_cause_status == "unavailable":
            limitations.append("AI root-cause analysis is unavailable (deterministic keyword diagnostics only).")
        if outcome == VerificationOutcome.UNAVAILABLE and verification.error is not None:
            limitations.append(f"Verification unavailable: {verification.error.message}")

        unverified_requirements: list[str] = [
            "Full end-to-end fulfillment of supplied natural-language requirements is not automatically proven because structured acceptance criteria decomposition and baseline regression comparison are unavailable.",
        ]
        unapproved_ids = [sc_id for sc_id in proposed_ids if sc_id not in set(approved_ids)]
        if unapproved_ids:
            unverified_requirements.append(
                f"Proposed scenario(s) not selected during human test review were not executed: {', '.join(unapproved_ids)}."
            )
        if not verification.verification_passed:
            unverified_requirements.append(
                f"Approved scenario(s) did not achieve passing verification (outcome='{outcome.value}')."
            )

        traceability = {
            "user_supplied_requirements": run.requirements,
            "changed_files": changed_files,
            "impacted_modules": impacted_modules,
            "proposed_scenario_ids": proposed_ids,
            "human_approved_scenario_ids": approved_ids,
            "executed_scenario_ids": executed_ids,
            "observed_verification_outcome": outcome.value,
            "verification_passed": verification.verification_passed,
            "unverified_scenario_ids": unapproved_ids,
        }

        comm_summary = [
            {
                "log_id": entry.log_id,
                "agent_name": entry.agent_name,
                "stage": entry.stage.value,
                "timestamp": entry.timestamp.isoformat(),
                "status": entry.status.value,
                "duration_ms": entry.duration_ms,
                "output_summary": entry.output_summary,
                "error_code": entry.error.code if entry.error else None,
            }
            for entry in logs
        ]

        package_id = f"pkg_{run.run_id}_{uuid.uuid4().hex[:8]}"
        draft = ChangePackageReport(
            package_id=package_id,
            run_id=run.run_id,
            requirements=run.requirements,
            repository_id=run.repository.repository_id,
            repository_path=run.repository.repository_path,
            source_path=run.repository.source_path,
            base_ref=run.repository.base_ref,
            resolved_base_commit=run.repository.resolved_base_commit,
            change_mode=run.repository.change_mode,
            execution_mode=run.execution_mode.value,
            model_label=gen.model_label if gen else None,
            is_mock_output=gen.is_mock_output if gen else None,
            changed_files=changed_files,
            impacted_modules=impacted_modules,
            approved_scenario_ids=approved_ids,
            executed_scenario_ids=executed_ids,
            codebase_analysis=cb,
            test_generation=gen,
            verification=verification,
            risk_summary=verification.risk_summary if verification else RiskSummary(),
            recommendation=recommendation,
            recommendation_label=recommendation_label,
            recommendation_summary=recommendation_summary,
            unverified_requirements=unverified_requirements,
            traceability=traceability,
            agent_communication_summary=comm_summary,
            evidence_limitations=limitations,
            human_approval_required=True,
        )
        package_sha = _sha256_text(draft.model_dump_json())
        return draft.model_copy(update={"package_sha256": package_sha})

    # ------------------------------------------------------------------
    # Phase 9: Human Review Gate 2 (Final Decision)
    # ------------------------------------------------------------------

    def submit_final_decision(
        self,
        run_id: str,
        request: HumanDecisionRequest,
    ) -> WorkflowRunRecord:
        """Record final human decision on the compiled change package.

        Never commits, merges, pushes, deploys, or modifies the original repository.
        """
        run = self.get_run(run_id)
        if run.state != WorkflowState.AWAITING_FINAL_APPROVAL:
            raise ReviewConflictError(
                f"Final decision is only permitted in state 'AWAITING_FINAL_APPROVAL'; "
                f"run '{run_id}' is currently in state '{run.state.value}'.",
                code="INVALID_DECISION_STATE",
                details={"current_state": run.state.value},
                run_id=run_id,
            )

        if request.decision == HumanDecision.APPROVE:
            if run.verification is None or not run.verification.verification_passed:
                outcome_val = (
                    run.verification.verification_outcome.value
                    if run.verification is not None
                    else "missing"
                )
                raise ReviewConflictError(
                    "Cannot approve a change package when mandatory verification did not pass "
                    f"(verification_outcome='{outcome_val}'). "
                    "Submit 'needs_review' or 'reject' instead.",
                    code="VERIFICATION_APPROVAL_BLOCKED",
                    details={"verification_outcome": outcome_val},
                    run_id=run_id,
                )

        target_state_map = {
            HumanDecision.APPROVE: WorkflowState.APPROVED,
            HumanDecision.REJECT: WorkflowState.REJECTED,
            HumanDecision.NEEDS_REVIEW: WorkflowState.NEEDS_REVIEW,
        }
        target_state = target_state_map[request.decision]

        decision_record = HumanDecisionRecord(
            decision=request.decision,
            reviewer=request.reviewer,
            comments=request.comments,
            evidence_package_id=run.report.package_id if run.report else None,
            evidence_package_sha256=run.report.package_sha256 if run.report else None,
        )

        try:
            updated = self.transition_state(
                run_id,
                WorkflowState.AWAITING_FINAL_APPROVAL,
                target_state,
                updater=lambda current: current.model_copy(
                    update={"final_decision": decision_record}
                ),
                reason=(
                    f"Final human decision '{request.decision.value}' recorded by "
                    f"'{request.reviewer}' for evidence package "
                    f"'{decision_record.evidence_package_id or 'unknown'}'. "
                    "No Git commit, merge, push, or deployment was performed."
                ),
            )
        except InvalidStateTransitionError as exc:
            raise ReviewConflictError(
                f"Conflicting or duplicate final decision rejected for run '{run_id}'.",
                code="DUPLICATE_FINAL_DECISION",
                run_id=run_id,
            ) from exc

        self._log(
            run_id=run_id,
            agent_name="HumanFinalDecisionGate",
            stage=target_state,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"reviewer={request.reviewer}, decision={request.decision.value}, "
                f"package_id={decision_record.evidence_package_id}"
            ),
            output_summary=(
                f"Recorded final decision '{request.decision.value}'. "
                f"Comments: {request.comments or '(none)'}"
            ),
        )
        return updated
