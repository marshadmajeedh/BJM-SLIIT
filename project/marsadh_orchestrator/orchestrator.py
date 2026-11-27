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
    PatchGenerationAdapter,
    TestingAdapter,
    VerificationAdapter,
    build_approved_test_specs,
)
from project.marsadh_orchestrator.baseline_verification import (
    BaselineComparisonEngine,
    BaselineTestMirrorError,
    discover_existing_regression_tests,
    execute_snapshot_verification,
    mirror_approved_tests_to_baseline,
    mirror_approved_tests_to_workspace,
    resolve_scenario_test_purpose,
)
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.patch_engine import PatchApplicator, PatchValidator
from project.marsadh_orchestrator.repair_engine import (
    RepairEligibilityEvaluator,
    build_repair_request_evidence,
    check_approved_test_assumptions_preserved,
    compute_failure_signature,
    is_provider_auth_or_rate_limit_error,
)
from project.marsadh_orchestrator.requirement_analyst import (
    DEFAULT_OFFLINE_REQUIREMENT_LABEL,
    RequirementAnalystAgent,
)
from project.marsadh_orchestrator.store import (
    RunStore,
    generate_run_id,
    redact_secrets,
    utc_now,
)
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
    AmbiguitySeverity,
    ApprovedTestSpec,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    ChangePackageReport,
    CodebaseAnalysisResult,
    CreateRunRequest,
    ExecutionMode,
    HumanDecision,
    HumanDecisionRecord,
    HumanDecisionRequest,
    IndividualTestOutcome,
    ModifiedVerificationResult,
    PatchAttemptRecord,
    PatchGenerationRequest,
    PatchGenerationResult,
    PatchModelMetadata,
    RepairAttemptHistory,
    RepairEligibilityCategory,
    RepairEligibilityResult,
    RepairPolicy,
    RepairTerminationReason,
    RepositoryInfo,
    RequirementAnalysisResult,
    RequirementTraceability,
    RiskSummary,
    TestComparisonClassification,
    TestGenerationResult,
    TestReviewRecord,
    TestReviewRequest,
    TraceabilityEvidenceStatus,
    VerificationComparisonStatus,
    VerificationOutcome,
    VerificationResult,
    WorkflowMode,
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
        WorkflowState.ANALYZING_REQUIREMENTS,
        WorkflowState.ANALYZING_CODEBASE,
        WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
        WorkflowState.GENERATING_PATCH,
        WorkflowState.VALIDATING_PATCH,
        WorkflowState.APPLYING_PATCH,
        WorkflowState.ANALYZING_POST_PATCH_IMPACT,
        WorkflowState.GENERATING_TESTS,
        WorkflowState.PREPARING_BASELINE,
        WorkflowState.RUNNING_BASELINE_VERIFICATION,
        WorkflowState.RUNNING_MODIFIED_VERIFICATION,
        WorkflowState.COMPARING_VERIFICATION_RESULTS,
        WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
        WorkflowState.REPAIRING_PATCH,
        WorkflowState.RUNNING_VERIFICATION,
        WorkflowState.COMPILING_REPORT,
    }
)

ALLOWED_TRANSITIONS: dict[WorkflowState, frozenset[WorkflowState]] = {
    WorkflowState.CREATED: frozenset(
        {
            WorkflowState.ANALYZING_REQUIREMENTS,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.ANALYZING_REQUIREMENTS: frozenset(
        {
            WorkflowState.ANALYZING_CODEBASE,
            WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
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
    WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT: frozenset(
        {
            WorkflowState.GENERATING_PATCH,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.GENERATING_PATCH: frozenset(
        {
            WorkflowState.VALIDATING_PATCH,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.VALIDATING_PATCH: frozenset(
        {
            WorkflowState.APPLYING_PATCH,
            WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
            WorkflowState.COMPILING_REPORT,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.APPLYING_PATCH: frozenset(
        {
            WorkflowState.ANALYZING_POST_PATCH_IMPACT,
            WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
            WorkflowState.COMPILING_REPORT,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.ANALYZING_POST_PATCH_IMPACT: frozenset(
        {
            WorkflowState.GENERATING_TESTS,
            WorkflowState.PREPARING_BASELINE,
            WorkflowState.RUNNING_MODIFIED_VERIFICATION,
            WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
            WorkflowState.COMPILING_REPORT,
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
            WorkflowState.PREPARING_BASELINE,
            WorkflowState.RUNNING_VERIFICATION,
            WorkflowState.REJECTED,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.PREPARING_BASELINE: frozenset(
        {
            WorkflowState.RUNNING_BASELINE_VERIFICATION,
            WorkflowState.RUNNING_MODIFIED_VERIFICATION,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.RUNNING_BASELINE_VERIFICATION: frozenset(
        {
            WorkflowState.RUNNING_MODIFIED_VERIFICATION,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.RUNNING_MODIFIED_VERIFICATION: frozenset(
        {
            WorkflowState.COMPARING_VERIFICATION_RESULTS,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.COMPARING_VERIFICATION_RESULTS: frozenset(
        {
            WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
            WorkflowState.COMPILING_REPORT,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY: frozenset(
        {
            WorkflowState.REPAIRING_PATCH,
            WorkflowState.COMPILING_REPORT,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.REPAIRING_PATCH: frozenset(
        {
            WorkflowState.VALIDATING_PATCH,
            WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
            WorkflowState.COMPILING_REPORT,
            WorkflowState.FAILED,
        }
    ),
    WorkflowState.RUNNING_VERIFICATION: frozenset(
        {
            WorkflowState.PREPARING_BASELINE,
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
        requirement_analyst: RequirementAnalystAgent | None = None,
        patch_adapter: PatchGenerationAdapter | None = None,
        patch_validator: PatchValidator | None = None,
        patch_applicator: PatchApplicator | None = None,
        requirement_offline_callable: Callable[[str], str] | None = None,
        requirement_offline_label: str = DEFAULT_OFFLINE_REQUIREMENT_LABEL,
        requirement_live_callable: Callable[[str], str] | None = None,
        requirement_live_label: str | None = None,
        offline_model_callable: Callable[[str], str] | None = None,
        offline_model_label: str = "deterministic_offline_stub",
        live_model_callable: Callable[[str], str] | None = None,
        live_model_label: str | None = None,
        use_default_offline_stub: bool = True,
        cleanup_baseline_workspace: bool = True,
        before_baseline_execution_hook: Callable[[WorkflowRunRecord, Path], None] | None = None,
        before_repair_provider_hook: Callable[[WorkflowRunRecord, int], None] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.workspace_manager = workspace_manager
        self.requirement_analyst = requirement_analyst or RequirementAnalystAgent(
            config=config,
            offline_analyzer_callable=requirement_offline_callable,
            offline_model_label=requirement_offline_label,
            live_model_callable=requirement_live_callable,
            live_model_label=requirement_live_label,
        )
        self.codebase_adapter = codebase_adapter
        self.patch_adapter = patch_adapter or PatchGenerationAdapter(config=config)
        self.patch_validator = patch_validator or PatchValidator(config=config)
        self.patch_applicator = patch_applicator or PatchApplicator(config=config)
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
        self._cleanup_baseline_workspace = cleanup_baseline_workspace
        self._before_baseline_execution_hook = before_baseline_execution_hook
        self._before_repair_provider_hook = before_repair_provider_hook

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
            def _mark_interrupted_repair(current: WorkflowRunRecord) -> WorkflowRunRecord:
                if current.repair_history is None and current.state not in {
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    WorkflowState.REPAIRING_PATCH,
                }:
                    return current
                hist_updated = (
                    current.repair_history.model_copy(
                        update={
                            "termination_reason": RepairTerminationReason.PROCESS_INTERRUPTED,
                            "termination_summary": (
                                f"Interrupted during active stage '{current.state.value}'; "
                                "repair provider calls and patch applications were not replayed."
                            ),
                        }
                    )
                    if current.repair_history is not None
                    else None
                )
                return current.model_copy(
                    update={
                        "repair_termination_reason": RepairTerminationReason.PROCESS_INTERRUPTED,
                        "repair_history": hist_updated,
                    }
                )

            try:
                updated = self._fail_run(
                    run.run_id,
                    run.state,
                    err,
                    updater=_mark_interrupted_repair,
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
        if request.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE:
            repair_policy = self.config.build_repair_policy(
                enabled_override=request.repair_enabled,
                max_repair_attempts_override=request.max_repair_attempts,
            )
        else:
            repair_policy = RepairPolicy(enabled=False, max_repair_attempts=0)
        record = WorkflowRunRecord(
            run_id=run_id,
            state=WorkflowState.CREATED,
            requirements=request.requirements,
            workflow_mode=request.workflow_mode,
            execution_mode=request.execution_mode,
            repair_policy=repair_policy,
            repair_attempts_consumed=0,
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
                f"workflow_mode={request.workflow_mode.value}, "
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

        # Persist workspace context & snapshot hashes and transition CREATED -> ANALYZING_REQUIREMENTS
        run = self.transition_state(
            run_id,
            WorkflowState.CREATED,
            WorkflowState.ANALYZING_REQUIREMENTS,
            updater=lambda current: current.model_copy(
                update={
                    "workspace_id": ws_ctx.workspace_id,
                    "workspace_path": ws_ctx.workspace_repo_path,
                    "original_source_snapshot_sha256": snapshot_sha,
                    "original_git_diff_sha256": diff_sha,
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
            reason="Workspace prepared; starting A1 structured requirement analysis.",
        )

        # ---- Stage 1: A1 Structured Requirement Analysis ----
        a1_started = utc_now()
        a1_t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="RequirementAnalystAgent",
            stage=WorkflowState.ANALYZING_REQUIREMENTS,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Decomposing natural-language requirement ({len(run.requirements)} chars) "
                f"in mode={run.execution_mode.value}."
            ),
            output_summary="Invoked A1 RequirementAnalystAgent.analyze.",
        )
        try:
            req_result = self.requirement_analyst.analyze(
                run.requirements,
                execution_mode=run.execution_mode,
                repository_context={
                    "repository_id": run.repository.repository_id,
                    "source_path": ws_ctx.source_path,
                    "base_ref": ws_ctx.base_ref,
                },
            )
        except Exception as exc:  # noqa: BLE001
            a1_dur_ms = int((time.perf_counter() - a1_t0) * 1000)
            err = AgentError(
                code="A1_UNEXPECTED_ERROR",
                message=f"RequirementAnalystAgent raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="RequirementAnalystAgent",
                stage=WorkflowState.ANALYZING_REQUIREMENTS,
                status=AgentExecutionStatus.FAILED,
                duration_ms=a1_dur_ms,
                output_summary="A1 requirement analysis failed with unexpected exception.",
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.ANALYZING_REQUIREMENTS, err)

        a1_dur_ms = int((time.perf_counter() - a1_t0) * 1000)
        if req_result.status == "error" or not req_result.acceptance_criteria:
            err = req_result.error or AgentError(
                code="A1_REQUIREMENT_ANALYSIS_FAILED",
                message="A1 requirement analysis did not produce valid acceptance criteria.",
            )
            a1_rec = AgentExecutionRecord(
                agent_name="RequirementAnalystAgent",
                stage=WorkflowState.ANALYZING_REQUIREMENTS.value,
                status=AgentExecutionStatus.FAILED,
                started_at=a1_started,
                completed_at=utc_now(),
                duration_ms=a1_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=err.message,
                error=err,
            )
            self._log(
                run_id=run_id,
                agent_name="RequirementAnalystAgent",
                stage=WorkflowState.ANALYZING_REQUIREMENTS,
                status=AgentExecutionStatus.FAILED,
                duration_ms=a1_dur_ms,
                input_summary=f"mode={run.execution_mode.value}",
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(
                run_id,
                WorkflowState.ANALYZING_REQUIREMENTS,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "requirement_analysis": req_result,
                        "agent_records": [*current.agent_records, a1_rec],
                    }
                ),
            )

        a1_source = (
            "deterministic_fixture" if req_result.is_deterministic_fixture else "live_llm"
        )
        a1_summary = (
            f"Extracted {len(req_result.acceptance_criteria)} acceptance criterion(s), "
            f"{len(req_result.business_constraints)} constraint(s), "
            f"{len(req_result.assumptions)} assumption(s), "
            f"{len(req_result.ambiguities)} ambiguity item(s) "
            f"(status={req_result.status}, model={req_result.model_label}, source={a1_source})."
        )
        a1_rec = AgentExecutionRecord(
            agent_name="RequirementAnalystAgent",
            stage=WorkflowState.ANALYZING_REQUIREMENTS.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=a1_started,
            completed_at=utc_now(),
            duration_ms=a1_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=a1_summary,
            assumptions=[asm.description for asm in req_result.assumptions],
        )
        self._log(
            run_id=run_id,
            agent_name="RequirementAnalystAgent",
            stage=WorkflowState.ANALYZING_REQUIREMENTS,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=a1_dur_ms,
            input_summary=f"mode={run.execution_mode.value}, model={req_result.model_label}",
            output_summary=a1_summary,
        )

        if run.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE:
            run, cb_result = self._execute_patch_generation_pipeline(
                run_id=run_id,
                run=run,
                ws_ctx=ws_ctx,
                req_result=req_result,
                a1_rec=a1_rec,
                original_snapshot_sha=snapshot_sha,
                original_diff_sha=diff_sha,
            )
            if cb_result is None or run.state != WorkflowState.GENERATING_TESTS:
                return run
        else:
            run = self.transition_state(
                run_id,
                WorkflowState.ANALYZING_REQUIREMENTS,
                WorkflowState.ANALYZING_CODEBASE,
                updater=lambda current: current.model_copy(
                    update={
                        "requirement_analysis": req_result,
                        "agent_records": [*current.agent_records, a1_rec],
                    }
                ),
                reason=(
                    f"A1 requirement analysis completed ({req_result.status}); "
                    "starting M01 codebase analysis."
                ),
            )

            # ---- Stage 2 (verify_existing_change): M01 Codebase Analysis ----
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
                requirement_analysis=run.requirement_analysis,
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

    def _execute_patch_generation_pipeline(
        self,
        *,
        run_id: str,
        run: WorkflowRunRecord,
        ws_ctx: Any,
        req_result: RequirementAnalysisResult,
        a1_rec: AgentExecutionRecord,
        original_snapshot_sha: str,
        original_diff_sha: str,
    ) -> tuple[WorkflowRunRecord, CodebaseAnalysisResult | None]:
        """Execute stages 3..9 of ``generate_and_verify_change`` inside the isolated workspace.

        Transitions:
        ANALYZING_REQUIREMENTS
          -> COLLECTING_PRE_CHANGE_CONTEXT
          -> GENERATING_PATCH
          -> VALIDATING_PATCH
          -> APPLYING_PATCH
          -> ANALYZING_POST_PATCH_IMPACT
          -> GENERATING_TESTS
        (or FAILED on any unavailable context/provider, blocking ambiguity,
        validation violation, or patch application failure).
        """
        run = self.transition_state(
            run_id,
            WorkflowState.ANALYZING_REQUIREMENTS,
            WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
            updater=lambda current: current.model_copy(
                update={
                    "requirement_analysis": req_result,
                    "agent_records": [*current.agent_records, a1_rec],
                }
            ),
            reason=(
                f"A1 requirement analysis completed ({req_result.status}); "
                "collecting available pre-change repository context."
            ),
        )

        # ---- Stage 3: Pre-change static codebase context ----
        pre_started = utc_now()
        pre_t0 = time.perf_counter()
        target_modules: list[str] = []
        for ac in req_result.acceptance_criteria:
            for mod in ac.target_modules:
                if mod not in target_modules:
                    target_modules.append(mod)

        self._log(
            run_id=run_id,
            agent_name="CodebaseAdapter",
            stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Collecting static pre-change context for source_path={ws_ctx.source_path}, "
                f"base_ref={ws_ctx.effective_base_ref}, target_modules={target_modules}"
            ),
            output_summary="Invoked CodebaseAdapter.collect_pre_change_context.",
        )
        try:
            pre_ctx, relevant_source_files = (
                self.codebase_adapter.collect_pre_change_context(
                    ws_ctx.workspace_repo_path,
                    source_path=ws_ctx.source_path,
                    base_ref=ws_ctx.effective_base_ref,
                    target_modules=target_modules,
                )
            )
        except Exception as exc:  # noqa: BLE001
            pre_dur_ms = int((time.perf_counter() - pre_t0) * 1000)
            err = AgentError(
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=f"Unexpected error collecting pre-change codebase context: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
                status=AgentExecutionStatus.FAILED,
                duration_ms=pre_dur_ms,
                output_summary=err.message,
                error=err,
            )
            return (
                self._fail_run(run_id, WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT, err),
                None,
            )

        pre_dur_ms = int((time.perf_counter() - pre_t0) * 1000)
        if pre_ctx.status != "completed" or not relevant_source_files:
            err = pre_ctx.error or AgentError(
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=(
                    "Required pre-change codebase context is unavailable for "
                    f"source_path='{ws_ctx.source_path}'."
                ),
            )
            pre_rec = AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT.value,
                status=AgentExecutionStatus.FAILED,
                started_at=pre_started,
                completed_at=utc_now(),
                duration_ms=pre_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=err.message,
                error=err,
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
                status=AgentExecutionStatus.FAILED,
                duration_ms=pre_dur_ms,
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "pre_change_context": pre_ctx,
                        "agent_records": [*current.agent_records, pre_rec],
                    }
                ),
            )
            return failed, None

        pre_summary = (
            f"Indexed {pre_ctx.total_indexed_files} Python file(s) "
            f"(scope={pre_ctx.scope}, pre_change_impact_prediction_available=False, "
            f"relevant_files={len(relevant_source_files)})."
        )
        pre_rec = AgentExecutionRecord(
            agent_name="CodebaseAdapter",
            stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=pre_started,
            completed_at=utc_now(),
            duration_ms=pre_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=pre_summary,
            assumptions=list(pre_ctx.limitation_notes),
        )
        self._log(
            run_id=run_id,
            agent_name="CodebaseAdapter",
            stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=pre_dur_ms,
            input_summary=f"source_path={ws_ctx.source_path}",
            output_summary=pre_summary,
        )

        # ---- Stage 4: Check mandatory unresolved ambiguities BEFORE invoking A4 ----
        blocking_ambiguity_ids = [
            amb.ambiguity_id
            for amb in req_result.ambiguities
            if (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
            and not amb.resolved
        ]
        if req_result.has_mandatory_unresolved_ambiguities or blocking_ambiguity_ids:
            err = AgentError(
                code="UNRESOLVED_REQUIREMENT_AMBIGUITY",
                message=(
                    "Cannot automatically generate or apply a patch while mandatory "
                    "business requirement ambiguities remain unresolved "
                    f"({', '.join(blocking_ambiguity_ids) or 'unspecified'})."
                ),
                details={
                    "unresolved_ambiguity_ids": blocking_ambiguity_ids,
                    "blocked_stage": WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT.value,
                },
                retryable=False,
            )
            blocked_patch_result = PatchGenerationResult(
                status="blocked",
                attempt_number=1,
                proposal=None,
                model_metadata=PatchModelMetadata(
                    provider=None,
                    model_label="blocked_ambiguity",
                    generation_mode=run.execution_mode.value,
                    is_deterministic_fixture=False,
                    is_mock_output=False,
                ),
                is_deterministic_fixture=False,
                is_mock_output=False,
                error=err,
                notes=[
                    "A4 PatchGenerationAdapter was not invoked because mandatory requirement ambiguities remain unresolved.",
                ],
            )
            self._log(
                run_id=run_id,
                agent_name="OrchestratorService",
                stage=WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
                status=AgentExecutionStatus.FAILED,
                input_summary=f"blocking_ambiguities={blocking_ambiguity_ids}",
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "pre_change_context": pre_ctx,
                        "patch_generation": blocked_patch_result,
                        "patch_attempt_number": 1,
                        "agent_records": [*current.agent_records, pre_rec],
                    }
                ),
            )
            return failed, None

        allowed_mod_paths = (
            [ws_ctx.source_path]
            if ws_ctx.source_path not in {"", "."}
            else list(pre_ctx.indexed_file_paths)
        )
        attempt_number = 1
        patch_request = PatchGenerationRequest(
            run_id=run_id,
            original_requirement=run.requirements,
            acceptance_criteria=list(req_result.acceptance_criteria),
            business_constraints=list(req_result.business_constraints),
            unresolved_ambiguities=[
                amb for amb in req_result.ambiguities if not amb.resolved
            ],
            repository_snapshot_ref=ws_ctx.resolved_base_commit,
            base_ref=ws_ctx.base_ref,
            resolved_base_commit=ws_ctx.resolved_base_commit,
            source_snapshot_sha256=original_snapshot_sha,
            source_path=ws_ctx.source_path,
            codebase_context=pre_ctx,
            allowed_modification_paths=allowed_mod_paths,
            relevant_source_files=relevant_source_files,
            relevant_source_file_paths=sorted(relevant_source_files.keys()),
            attempt_number=attempt_number,
            attempt_type="initial",
            repair_attempt_number=0,
            execution_mode=run.execution_mode.value,
            previous_verification_evidence=None,
        )

        run = self.transition_state(
            run_id,
            WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
            WorkflowState.GENERATING_PATCH,
            updater=lambda current: current.model_copy(
                update={
                    "pre_change_context": pre_ctx,
                    "patch_generation_request": patch_request,
                    "patch_attempt_number": attempt_number,
                    "agent_records": [*current.agent_records, pre_rec],
                }
            ),
            reason=(
                "Pre-change repository context collected and no blocking ambiguities "
                "detected; invoking A4 patch generation adapter."
            ),
        )

        # ---- Stage 5: Invoke injectable A4 PatchGenerationAdapter ----
        a4_started = utc_now()
        a4_t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="PatchGenerationAdapter",
            stage=WorkflowState.GENERATING_PATCH,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"attempt={attempt_number}, mode={run.execution_mode.value}, "
                f"criteria={len(patch_request.acceptance_criteria)}, "
                f"relevant_files={len(patch_request.relevant_source_file_paths)}"
            ),
            output_summary="Invoked A4 PatchGenerationAdapter.generate.",
        )
        try:
            patch_result = self.patch_adapter.generate(patch_request)
        except Exception as exc:  # noqa: BLE001
            a4_dur_ms = int((time.perf_counter() - a4_t0) * 1000)
            err = AgentError(
                code="A4_UNEXPECTED_ERROR",
                message=f"PatchGenerationAdapter raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="PatchGenerationAdapter",
                stage=WorkflowState.GENERATING_PATCH,
                status=AgentExecutionStatus.FAILED,
                duration_ms=a4_dur_ms,
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.GENERATING_PATCH, err), None

        a4_dur_ms = int((time.perf_counter() - a4_t0) * 1000)
        if patch_result.status != "proposed" or patch_result.proposal is None:
            err = patch_result.error or AgentError(
                code="PATCH_GENERATION_FAILED",
                message=(
                    "A4 patch generation did not produce a valid patch proposal "
                    f"(status='{patch_result.status}')."
                ),
            )
            a4_rec = AgentExecutionRecord(
                agent_name="PatchGenerationAdapter",
                stage=WorkflowState.GENERATING_PATCH.value,
                status=AgentExecutionStatus.FAILED,
                started_at=a4_started,
                completed_at=utc_now(),
                duration_ms=a4_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=err.message,
                error=err,
            )
            self._log(
                run_id=run_id,
                agent_name="PatchGenerationAdapter",
                stage=WorkflowState.GENERATING_PATCH,
                status=AgentExecutionStatus.FAILED,
                duration_ms=a4_dur_ms,
                input_summary=f"attempt={attempt_number}, mode={run.execution_mode.value}",
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.GENERATING_PATCH,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "patch_generation": patch_result,
                        "patch_attempt_number": patch_result.attempt_number,
                        "agent_records": [*current.agent_records, a4_rec],
                    }
                ),
            )
            return failed, None

        proposal = patch_result.proposal
        patch_source = (
            "deterministic_offline_fixture"
            if patch_result.is_deterministic_fixture
            else (
                "mock_output" if patch_result.is_mock_output else "injected_provider"
            )
        )
        a4_summary = (
            f"Generated patch proposal '{proposal.proposal_id}' "
            f"(sha256={proposal.patch_sha256[:12]}, files={len(proposal.affected_files)}, "
            f"attempt={patch_result.attempt_number}, model={patch_result.model_metadata.model_label}, "
            f"source={patch_source})."
        )
        a4_rec = AgentExecutionRecord(
            agent_name="PatchGenerationAdapter",
            stage=WorkflowState.GENERATING_PATCH.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=a4_started,
            completed_at=utc_now(),
            duration_ms=a4_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=a4_summary,
            assumptions=list(proposal.assumptions),
        )
        self._log(
            run_id=run_id,
            agent_name="PatchGenerationAdapter",
            stage=WorkflowState.GENERATING_PATCH,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=a4_dur_ms,
            input_summary=f"attempt={patch_result.attempt_number}, mode={run.execution_mode.value}",
            output_summary=a4_summary,
        )

        run = self.transition_state(
            run_id,
            WorkflowState.GENERATING_PATCH,
            WorkflowState.VALIDATING_PATCH,
            updater=lambda current: current.model_copy(
                update={
                    "patch_generation": patch_result,
                    "patch_proposal_sha256": proposal.patch_sha256,
                    "patch_attempt_number": patch_result.attempt_number,
                    "agent_records": [*current.agent_records, a4_rec],
                }
            ),
            reason="Patch proposal received from A4; running controlled patch safety validation.",
        )

        # ---- Stage 6: Controlled Patch Validation ----
        val_started = utc_now()
        val_t0 = time.perf_counter()
        valid_cids = {ac.criterion_id for ac in req_result.acceptance_criteria}
        self._log(
            run_id=run_id,
            agent_name="PatchValidator",
            stage=WorkflowState.VALIDATING_PATCH,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Validating proposal '{proposal.proposal_id}' "
                f"(sha256={proposal.patch_sha256[:12]}, files={len(proposal.affected_files)})"
            ),
            output_summary="Invoked PatchValidator.validate.",
        )
        try:
            validation_result = self.patch_validator.validate(
                workspace_repo_path=ws_ctx.workspace_repo_path,
                proposal=proposal,
                source_path=ws_ctx.source_path,
                base_ref=ws_ctx.effective_base_ref,
                allowed_modification_paths=allowed_mod_paths,
                expected_source_snapshot_sha256=original_snapshot_sha,
                expected_git_diff_sha256=original_diff_sha,
                valid_criterion_ids=valid_cids,
            )
        except Exception as exc:  # noqa: BLE001
            val_dur_ms = int((time.perf_counter() - val_t0) * 1000)
            err = AgentError(
                code="PATCH_VALIDATION_ERROR",
                message=f"PatchValidator raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="PatchValidator",
                stage=WorkflowState.VALIDATING_PATCH,
                status=AgentExecutionStatus.FAILED,
                duration_ms=val_dur_ms,
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.VALIDATING_PATCH, err), None

        val_dur_ms = int((time.perf_counter() - val_t0) * 1000)
        if not validation_result.valid:
            primary_err = (
                validation_result.violations[0]
                if validation_result.violations
                else AgentError(
                    code="PATCH_VALIDATION_FAILED",
                    message="Proposed patch failed safety or applicability validation.",
                )
            )
            val_rec = AgentExecutionRecord(
                agent_name="PatchValidator",
                stage=WorkflowState.VALIDATING_PATCH.value,
                status=AgentExecutionStatus.FAILED,
                started_at=val_started,
                completed_at=utc_now(),
                duration_ms=val_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=primary_err.message,
                error=primary_err,
            )
            self._log(
                run_id=run_id,
                agent_name="PatchValidator",
                stage=WorkflowState.VALIDATING_PATCH,
                status=AgentExecutionStatus.FAILED,
                duration_ms=val_dur_ms,
                input_summary=f"patch_sha256={proposal.patch_sha256[:12]}",
                output_summary=(
                    f"Validation failed with {len(validation_result.violations)} violation(s): "
                    f"{primary_err.code}: {primary_err.message}"
                ),
                error=primary_err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.VALIDATING_PATCH,
                primary_err,
                updater=lambda current: current.model_copy(
                    update={
                        "patch_validation": validation_result,
                        "agent_records": [*current.agent_records, val_rec],
                    }
                ),
            )
            return failed, None

        val_summary = (
            f"Patch validation passed ({len(validation_result.checks_passed)} checks, "
            f"files={validation_result.checked_files}, "
            f"bytes={validation_result.patch_size_bytes}, git_apply_check=True)."
        )
        val_rec = AgentExecutionRecord(
            agent_name="PatchValidator",
            stage=WorkflowState.VALIDATING_PATCH.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=val_started,
            completed_at=utc_now(),
            duration_ms=val_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=val_summary,
        )
        self._log(
            run_id=run_id,
            agent_name="PatchValidator",
            stage=WorkflowState.VALIDATING_PATCH,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=val_dur_ms,
            input_summary=f"patch_sha256={proposal.patch_sha256[:12]}",
            output_summary=val_summary,
        )

        run = self.transition_state(
            run_id,
            WorkflowState.VALIDATING_PATCH,
            WorkflowState.APPLYING_PATCH,
            updater=lambda current: current.model_copy(
                update={
                    "patch_validation": validation_result,
                    "agent_records": [*current.agent_records, val_rec],
                }
            ),
            reason="Patch passed safety and applicability checks; applying inside isolated workspace.",
        )

        # ---- Stage 7 & 8: Controlled Patch Application + Snapshot/Diff Recomputation ----
        app_started = utc_now()
        app_t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="PatchApplicator",
            stage=WorkflowState.APPLYING_PATCH,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=f"Applying validated patch sha256={proposal.patch_sha256[:12]}",
            output_summary="Invoked PatchApplicator.apply.",
        )
        try:
            app_result = self.patch_applicator.apply(
                workspace_repo_path=ws_ctx.workspace_repo_path,
                proposal=proposal,
                validation_result=validation_result,
                source_path=ws_ctx.source_path,
                base_ref=ws_ctx.effective_base_ref,
                expected_source_snapshot_sha256=original_snapshot_sha,
                expected_git_diff_sha256=original_diff_sha,
            )
        except Exception as exc:  # noqa: BLE001
            app_dur_ms = int((time.perf_counter() - app_t0) * 1000)
            err = AgentError(
                code="PATCH_APPLICATION_ERROR",
                message=f"PatchApplicator raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="PatchApplicator",
                stage=WorkflowState.APPLYING_PATCH,
                status=AgentExecutionStatus.FAILED,
                duration_ms=app_dur_ms,
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.APPLYING_PATCH, err), None

        app_dur_ms = int((time.perf_counter() - app_t0) * 1000)
        if (
            not app_result.applied
            or app_result.resulting_source_snapshot_sha256 is None
            or app_result.resulting_git_diff_sha256 is None
        ):
            err = app_result.error or AgentError(
                code="PATCH_APPLICATION_FAILED",
                message="Patch application did not complete cleanly; aborted before verification.",
            )
            app_rec = AgentExecutionRecord(
                agent_name="PatchApplicator",
                stage=WorkflowState.APPLYING_PATCH.value,
                status=AgentExecutionStatus.FAILED,
                started_at=app_started,
                completed_at=utc_now(),
                duration_ms=app_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=err.message,
                error=err,
            )
            self._log(
                run_id=run_id,
                agent_name="PatchApplicator",
                stage=WorkflowState.APPLYING_PATCH,
                status=AgentExecutionStatus.FAILED,
                duration_ms=app_dur_ms,
                input_summary=f"patch_sha256={proposal.patch_sha256[:12]}",
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.APPLYING_PATCH,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "patch_application": app_result,
                        "agent_records": [*current.agent_records, app_rec],
                    }
                ),
            )
            return failed, None

        app_summary = (
            f"Applied patch inside isolated workspace "
            f"(files={app_result.applied_files}, "
            f"orig_snap={original_snapshot_sha[:12]} -> "
            f"new_snap={app_result.resulting_source_snapshot_sha256[:12]}, "
            f"new_diff={app_result.resulting_git_diff_sha256[:12]})."
        )
        app_rec = AgentExecutionRecord(
            agent_name="PatchApplicator",
            stage=WorkflowState.APPLYING_PATCH.value,
            status=AgentExecutionStatus.SUCCEEDED,
            started_at=app_started,
            completed_at=utc_now(),
            duration_ms=app_dur_ms,
            execution_mode=run.execution_mode.value,
            output_summary=app_summary,
            assumptions=[app_result.isolation_note],
        )
        self._log(
            run_id=run_id,
            agent_name="PatchApplicator",
            stage=WorkflowState.APPLYING_PATCH,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=app_dur_ms,
            input_summary=f"patch_sha256={proposal.patch_sha256[:12]}",
            output_summary=app_summary,
        )

        # Persist post-patch snapshot/diff hashes while preserving original_* hashes
        run = self.transition_state(
            run_id,
            WorkflowState.APPLYING_PATCH,
            WorkflowState.ANALYZING_POST_PATCH_IMPACT,
            updater=lambda current: current.model_copy(
                update={
                    "patch_application": app_result,
                    "source_snapshot_sha256": app_result.resulting_source_snapshot_sha256,
                    "git_diff_sha256": app_result.resulting_git_diff_sha256,
                    "repository": current.repository.model_copy(
                        update={"change_mode": "working_tree"}
                    ),
                    "agent_records": [*current.agent_records, app_rec],
                }
            ),
            reason=(
                "Patch applied inside isolated workspace and snapshot/diff evidence "
                "recomputed; starting post-patch M01 impact analysis."
            ),
        )

        # ---- Stage 9: Post-Patch M01 Impact Analysis ----
        m01_started = utc_now()
        m01_t0 = time.perf_counter()
        self._log(
            run_id=run_id,
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"Running post-patch M01 impact analysis for source_path={ws_ctx.source_path} "
                f"against effective_base_ref={ws_ctx.effective_base_ref}"
            ),
            output_summary="Invoked M01 CodebaseAdapter.analyze (post-patch).",
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
                code="M01_POST_PATCH_UNEXPECTED_ERROR",
                message=f"Post-patch CodebaseAdapter raised an unexpected exception: {exc}",
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m01_dur_ms,
                output_summary=err.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.ANALYZING_POST_PATCH_IMPACT, err), None

        m01_dur_ms = int((time.perf_counter() - m01_t0) * 1000)
        if cb_result.status == "error":
            err = cb_result.error or AgentError(
                code="M01_POST_PATCH_ANALYSIS_ERROR",
                message="Post-patch M01 codebase impact analysis returned an error.",
            )
            m01_rec = AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
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
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m01_dur_ms,
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "codebase_analysis": cb_result,
                        "agent_records": [*current.agent_records, m01_rec],
                    }
                ),
            )
            return failed, None

        if cb_result.status == "no_changes" or not cb_result.has_relevant_changes:
            err = AgentError(
                code="POST_PATCH_NO_CHANGES_DETECTED",
                message=(
                    "Post-patch M01 impact analysis detected zero tracked Python changes "
                    "after patch application; refusing to silently complete."
                ),
            )
            m01_rec = AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
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
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                status=AgentExecutionStatus.FAILED,
                duration_ms=m01_dur_ms,
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(
                run_id,
                WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                err,
                updater=lambda current: current.model_copy(
                    update={
                        "codebase_analysis": cb_result,
                        "agent_records": [*current.agent_records, m01_rec],
                    }
                ),
            )
            return failed, None

        m01_summary = (
            f"Post-patch M01 detected {len(cb_result.changed_files_repo_relative)} changed file(s) "
            f"and {len(cb_result.impacted_modules_repo_relative)} impacted module(s)."
        )
        m01_rec = AgentExecutionRecord(
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
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
            stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=m01_dur_ms,
            input_summary=f"source_path={ws_ctx.source_path}, base_ref={ws_ctx.effective_base_ref}",
            output_summary=m01_summary,
        )

        run = self.transition_state(
            run_id,
            WorkflowState.ANALYZING_POST_PATCH_IMPACT,
            WorkflowState.GENERATING_TESTS,
            updater=lambda current: current.model_copy(
                update={
                    "codebase_analysis": cb_result,
                    "agent_records": [*current.agent_records, m01_rec],
                }
            ),
            reason="Post-patch M01 impact analysis completed; starting M03 test generation.",
        )
        return run, cb_result

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

        # Atomically claim AWAITING_TEST_REVIEW -> PREPARING_BASELINE so concurrent approvals cannot race!
        try:
            claimed = self.transition_state(
                run_id,
                WorkflowState.AWAITING_TEST_REVIEW,
                WorkflowState.PREPARING_BASELINE,
                updater=lambda current: current.model_copy(
                    update={"test_review": initial_review}
                ),
                reason=(
                    f"Human reviewer '{request.reviewer}' approved "
                    f"{len(request.selected_test_ids)} test scenario(s); claimed baseline-and-modified verification slot."
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
            stage=WorkflowState.PREPARING_BASELINE,
            status=AgentExecutionStatus.SUCCEEDED,
            input_summary=(
                f"reviewer={request.reviewer}, approved=True, "
                f"selected={request.selected_test_ids}"
            ),
            output_summary="Approved scenarios accepted; exporting selected tests for baseline and modified verification.",
        )

        # Export ONLY selected approved scenarios into isolated modified workspace
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
                stage=WorkflowState.PREPARING_BASELINE,
                status=AgentExecutionStatus.FAILED,
                duration_ms=export_dur_ms,
                input_summary=f"export selected={request.selected_test_ids}",
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(run_id, WorkflowState.PREPARING_BASELINE, err)
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
                stage=WorkflowState.PREPARING_BASELINE,
                status=AgentExecutionStatus.FAILED,
                duration_ms=export_dur_ms,
                output_summary=err.message,
                error=err,
            )
            failed = self._fail_run(run_id, WorkflowState.PREPARING_BASELINE, err)
            return failed, None

        self._log(
            run_id=run_id,
            agent_name="TestingAdapter",
            stage=WorkflowState.PREPARING_BASELINE,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=export_dur_ms,
            input_summary=f"export selected={request.selected_test_ids}",
            output_summary=(
                f"Exported {len(approved_specs)} approved scenario file(s) with SHA-256 hashes."
            ),
        )

        updated = self.store.update_run_atomic(
            run_id,
            expected_state=WorkflowState.PREPARING_BASELINE,
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
        """Process Human Review Gate 1 and optionally execute baseline+modified verification."""
        updated, approved_specs = self.accept_test_review(run_id, request)
        if (
            not request.approved
            or approved_specs is None
            or updated.state
            not in {WorkflowState.PREPARING_BASELINE, WorkflowState.RUNNING_VERIFICATION}
        ):
            return updated
        if not execute_immediately:
            return updated
        return self.execute_verification_and_report(run_id, approved_specs)

    # ------------------------------------------------------------------
    # Phase 7 & 8: Baseline + Modified Verification, Comparison & Report Compilation
    # ------------------------------------------------------------------

    def execute_verification_and_report(
        self,
        run_id: str,
        approved_specs: Mapping[str, ApprovedTestSpec] | None = None,
    ) -> WorkflowRunRecord:
        """Prepare baseline, execute identical approved tests on baseline & modified, compare, and compile report."""
        run = self.get_run(run_id)
        if run.state == WorkflowState.RUNNING_VERIFICATION:
            run = self.transition_state(
                run_id,
                WorkflowState.RUNNING_VERIFICATION,
                WorkflowState.PREPARING_BASELINE,
                reason="Transitioning legacy RUNNING_VERIFICATION run into baseline preparation.",
            )
        elif run.state != WorkflowState.PREPARING_BASELINE:
            raise InvalidStateTransitionError(
                f"Cannot execute verification when run '{run_id}' is in state '{run.state.value}'.",
                run_id=run_id,
            )

        if run.test_generation is None or run.test_review is None or not run.test_review.approved:
            err = AgentError(
                code="MISSING_TEST_APPROVAL",
                message="Approved test generation and human review records are required before verification.",
            )
            return self._fail_run(run_id, WorkflowState.PREPARING_BASELINE, err)

        # Re-verify modified workspace source snapshot before baseline preparation
        try:
            self._verify_workspace_snapshot(run)
        except WorkflowIntegrityError as exc:
            err = AgentError(code=exc.code, message=exc.message, details=exc.details)
            self._log(
                run_id=run_id,
                agent_name="IntegrityVerifier",
                stage=WorkflowState.PREPARING_BASELINE,
                status=AgentExecutionStatus.FAILED,
                output_summary=exc.message,
                error=err,
            )
            return self._fail_run(run_id, WorkflowState.PREPARING_BASELINE, err)

        specs: Mapping[str, ApprovedTestSpec]
        if approved_specs is not None:
            specs = approved_specs
        else:
            try:
                specs = build_approved_test_specs(run.test_generation, run.test_review)
            except ValueError as exc:
                err = AgentError(code="APPROVED_SPEC_BUILD_ERROR", message=str(exc))
                return self._fail_run(run_id, WorkflowState.PREPARING_BASELINE, err)

        # Derive execution trust strictly from server configuration & validated source repository path.
        is_trusted_workspace = self.config.is_trusted_for_execution(
            run.repository.repository_path
        )
        assert run.workspace_path is not None

        selected_ids = list(run.test_review.selected_test_ids)
        scenarios_by_id = {sc.scenario_id: sc for sc in run.test_generation.scenarios}
        test_purposes = {
            sid: resolve_scenario_test_purpose(scenarios_by_id[sid], run.requirement_analysis)
            for sid in selected_ids
            if sid in scenarios_by_id
        }
        criterion_ids_by_test = {
            sid: list(scenarios_by_id[sid].criterion_ids)
            for sid in selected_ids
            if sid in scenarios_by_id
        }
        baseline_ref_label = run.repository.resolved_base_commit or run.repository.base_ref

        baseline_workspace_path: str | None = None
        baseline_snapshot_sha: str | None = None
        baseline_result: BaselineVerificationResult | None = None

        try:
            # ---- Stage 4A: PREPARING_BASELINE ----
            base_prep_started = utc_now()
            base_prep_t0 = time.perf_counter()
            resolved_base_commit = run.repository.resolved_base_commit
            baseline_ready = False

            if not resolved_base_commit:
                prep_err = AgentError(
                    code="BASELINE_COMMIT_UNAVAILABLE",
                    message=(
                        "Resolved baseline Git commit is unavailable; proceeding with modified "
                        "verification only and marking baseline comparison incomplete."
                    ),
                )
                baseline_result = BaselineVerificationResult(
                    execution_status="unavailable",
                    verification_outcome=VerificationOutcome.UNAVAILABLE,
                    baseline_ref=baseline_ref_label,
                    baseline_commit=None,
                    evidence_limitations=[prep_err.message],
                    error=prep_err,
                )
                self._log(
                    run_id=run_id,
                    agent_name="WorkspaceManager",
                    stage=WorkflowState.PREPARING_BASELINE,
                    status=AgentExecutionStatus.FAILED,
                    duration_ms=int((time.perf_counter() - base_prep_t0) * 1000),
                    output_summary=prep_err.message,
                    error=prep_err,
                )
            else:
                try:
                    base_ws_ctx = self.workspace_manager.prepare_baseline_workspace(
                        run_id,
                        run.repository.repository_path,
                        resolved_base_commit,
                        source_path=run.repository.source_path,
                        base_ref=run.repository.base_ref,
                    )
                    baseline_workspace_path = base_ws_ctx.workspace_repo_path
                    baseline_snapshot_sha = compute_source_snapshot_sha256(
                        baseline_workspace_path,
                        base_ws_ctx.source_path,
                    )
                    mirrored_hashes = mirror_approved_tests_to_baseline(
                        run.workspace_path,
                        baseline_workspace_path,
                        specs,
                        selected_ids,
                    )
                    baseline_ready = True
                except BaselineTestMirrorError as exc:
                    err = AgentError(code=exc.code, message=exc.message, details=exc.details)
                    self._log(
                        run_id=run_id,
                        agent_name="IntegrityVerifier",
                        stage=WorkflowState.PREPARING_BASELINE,
                        status=AgentExecutionStatus.FAILED,
                        duration_ms=int((time.perf_counter() - base_prep_t0) * 1000),
                        output_summary=exc.message,
                        error=err,
                    )
                    return self._fail_run(
                        run_id,
                        WorkflowState.PREPARING_BASELINE,
                        err,
                        reason=exc.message,
                    )
                except (WorkspaceError, WorkspaceSecurityError, Exception) as exc:  # noqa: BLE001
                    prep_err = AgentError(
                        code="BASELINE_WORKSPACE_UNAVAILABLE",
                        message=(
                            f"Baseline workspace could not be prepared for commit "
                            f"'{resolved_base_commit[:12]}': {exc}. Proceeding with modified "
                            "verification only and marking baseline comparison incomplete."
                        ),
                    )
                    baseline_result = BaselineVerificationResult(
                        execution_status="unavailable",
                        verification_outcome=VerificationOutcome.UNAVAILABLE,
                        baseline_ref=baseline_ref_label,
                        baseline_commit=resolved_base_commit,
                        evidence_limitations=[prep_err.message],
                        error=prep_err,
                    )
                    self._log(
                        run_id=run_id,
                        agent_name="WorkspaceManager",
                        stage=WorkflowState.PREPARING_BASELINE,
                        status=AgentExecutionStatus.FAILED,
                        duration_ms=int((time.perf_counter() - base_prep_t0) * 1000),
                        input_summary=f"baseline_commit={resolved_base_commit[:12]}",
                        output_summary=prep_err.message,
                        error=prep_err,
                    )

            base_prep_dur_ms = int((time.perf_counter() - base_prep_t0) * 1000)

            if baseline_ready:
                assert baseline_workspace_path is not None
                assert baseline_snapshot_sha is not None
                assert resolved_base_commit is not None
                prep_summary = (
                    f"Prepared separate baseline workspace '{base_ws_ctx.workspace_id}' at commit "
                    f"{resolved_base_commit[:12]} (snapshot={baseline_snapshot_sha[:12]}) and "
                    f"mirrored {len(mirrored_hashes)} identical approved test file(s) with verified SHA-256 hashes."
                )
                prep_rec = AgentExecutionRecord(
                    agent_name="WorkspaceManager",
                    stage=WorkflowState.PREPARING_BASELINE.value,
                    status=AgentExecutionStatus.SUCCEEDED,
                    started_at=base_prep_started,
                    completed_at=utc_now(),
                    duration_ms=base_prep_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=prep_summary,
                    assumptions=[
                        *list(base_ws_ctx.warnings),
                        "Git baseline workspace isolation protects the original repository and modified workspace; it is not an OS security sandbox.",
                    ],
                )
                self._log(
                    run_id=run_id,
                    agent_name="WorkspaceManager",
                    stage=WorkflowState.PREPARING_BASELINE,
                    status=AgentExecutionStatus.SUCCEEDED,
                    duration_ms=base_prep_dur_ms,
                    input_summary=f"baseline_commit={resolved_base_commit[:12]}, scenarios={selected_ids}",
                    output_summary=prep_summary,
                )
                run = self.transition_state(
                    run_id,
                    WorkflowState.PREPARING_BASELINE,
                    WorkflowState.RUNNING_BASELINE_VERIFICATION,
                    updater=lambda current: current.model_copy(
                        update={
                            "baseline_workspace_id": base_ws_ctx.workspace_id,
                            "baseline_workspace_path": baseline_workspace_path,
                            "baseline_source_snapshot_sha256": baseline_snapshot_sha,
                            "agent_records": [*current.agent_records, prep_rec],
                        }
                    ),
                    reason="Baseline workspace prepared with identical approved tests; executing baseline verification.",
                )

                # ---- Stage 4B: RUNNING_BASELINE_VERIFICATION ----
                if self._before_baseline_execution_hook is not None:
                    self._before_baseline_execution_hook(run, Path(baseline_workspace_path))

                # Re-verify baseline workspace source snapshot integrity right before running tests
                try:
                    actual_base_snap = compute_source_snapshot_sha256(
                        baseline_workspace_path,
                        run.repository.source_path,
                    )
                except WorkspaceError as exc:
                    err = AgentError(
                        code="BASELINE_WORKSPACE_TAMPERED",
                        message=f"Failed to verify baseline workspace snapshot before execution: {exc}",
                    )
                    self._log(
                        run_id=run_id,
                        agent_name="IntegrityVerifier",
                        stage=WorkflowState.RUNNING_BASELINE_VERIFICATION,
                        status=AgentExecutionStatus.FAILED,
                        output_summary=err.message,
                        error=err,
                    )
                    return self._fail_run(
                        run_id, WorkflowState.RUNNING_BASELINE_VERIFICATION, err
                    )

                if not hmac.compare_digest(actual_base_snap, baseline_snapshot_sha):
                    err = AgentError(
                        code="BASELINE_WORKSPACE_TAMPERED",
                        message=(
                            "Baseline workspace source snapshot changed before baseline test execution; "
                            "refusing to execute verification against tampered baseline."
                        ),
                        details={
                            "expected_baseline_snapshot_sha256": baseline_snapshot_sha,
                            "actual_baseline_snapshot_sha256": actual_base_snap,
                        },
                    )
                    self._log(
                        run_id=run_id,
                        agent_name="IntegrityVerifier",
                        stage=WorkflowState.RUNNING_BASELINE_VERIFICATION,
                        status=AgentExecutionStatus.FAILED,
                        output_summary=err.message,
                        error=err,
                    )
                    return self._fail_run(
                        run_id, WorkflowState.RUNNING_BASELINE_VERIFICATION, err
                    )

                base_ver_started = utc_now()
                base_ver_t0 = time.perf_counter()
                self._log(
                    run_id=run_id,
                    agent_name="VerificationAdapter",
                    stage=WorkflowState.RUNNING_BASELINE_VERIFICATION,
                    status=AgentExecutionStatus.SUCCEEDED,
                    input_summary=(
                        f"Executing {len(selected_ids)} approved scenario(s) on baseline commit "
                        f"{resolved_base_commit[:12]} (trusted={is_trusted_workspace})."
                    ),
                    output_summary="Invoked M04 VerificationAdapter on baseline workspace.",
                )
                try:
                    snap_base_res, _base_agg, base_integrity_err = (
                        execute_snapshot_verification(
                            self.verification_adapter,
                            self.config,
                            snapshot_role="baseline",
                            workspace_path=baseline_workspace_path,
                            source_path=run.repository.source_path,
                            selected_test_ids=selected_ids,
                            approved_specs=specs,
                            test_purposes=test_purposes,
                            criterion_ids_by_test=criterion_ids_by_test,
                            is_trusted_workspace=is_trusted_workspace,
                            source_snapshot_sha256=baseline_snapshot_sha,
                            baseline_ref=baseline_ref_label,
                            baseline_commit=resolved_base_commit,
                        )
                    )
                    assert isinstance(snap_base_res, BaselineVerificationResult)
                    baseline_result = snap_base_res
                except Exception as exc:  # noqa: BLE001
                    base_ver_dur_ms = int((time.perf_counter() - base_ver_t0) * 1000)
                    base_err = AgentError(
                        code="M04_BASELINE_UNEXPECTED_ERROR",
                        message=f"Baseline verification raised an unexpected exception: {exc}",
                    )
                    baseline_result = BaselineVerificationResult(
                        execution_status="error",
                        verification_outcome=VerificationOutcome.EXECUTION_ERROR,
                        baseline_ref=baseline_ref_label,
                        baseline_commit=resolved_base_commit,
                        source_snapshot_sha256=baseline_snapshot_sha,
                        evidence_limitations=[base_err.message],
                        error=base_err,
                    )
                    base_integrity_err = None
                    self._log(
                        run_id=run_id,
                        agent_name="VerificationAdapter",
                        stage=WorkflowState.RUNNING_BASELINE_VERIFICATION,
                        status=AgentExecutionStatus.FAILED,
                        duration_ms=base_ver_dur_ms,
                        output_summary=base_err.message,
                        error=base_err,
                    )
                else:
                    base_ver_dur_ms = int((time.perf_counter() - base_ver_t0) * 1000)

                base_exec_status = (
                    AgentExecutionStatus.TIMEOUT
                    if baseline_result.verification_outcome == VerificationOutcome.TIMEOUT
                    else (
                        AgentExecutionStatus.SUCCEEDED
                        if baseline_result.execution_status == "completed"
                        else AgentExecutionStatus.FAILED
                    )
                )
                base_summary = (
                    f"Baseline verification outcome={baseline_result.verification_outcome.value}, "
                    f"exit_code={baseline_result.exit_code}, "
                    f"total={baseline_result.total_tests}, passed={baseline_result.passed}, "
                    f"failed={baseline_result.failed}."
                )
                base_rec = AgentExecutionRecord(
                    agent_name="VerificationAdapter",
                    stage=WorkflowState.RUNNING_BASELINE_VERIFICATION.value,
                    status=base_exec_status,
                    started_at=base_ver_started,
                    completed_at=utc_now(),
                    duration_ms=base_ver_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=base_summary,
                    error=baseline_result.error,
                    assumptions=list(baseline_result.evidence_limitations),
                )
                self._log(
                    run_id=run_id,
                    agent_name="VerificationAdapter",
                    stage=WorkflowState.RUNNING_BASELINE_VERIFICATION,
                    status=base_exec_status,
                    duration_ms=base_ver_dur_ms,
                    input_summary=f"baseline scenarios={selected_ids}",
                    output_summary=base_summary,
                    error=baseline_result.error,
                )

                if base_integrity_err is not None:
                    return self._fail_run(
                        run_id,
                        WorkflowState.RUNNING_BASELINE_VERIFICATION,
                        base_integrity_err,
                        updater=lambda current: current.model_copy(
                            update={
                                "baseline_verification": baseline_result,
                                "agent_records": [*current.agent_records, base_rec],
                            }
                        ),
                        reason=base_integrity_err.message,
                    )

                run = self.transition_state(
                    run_id,
                    WorkflowState.RUNNING_BASELINE_VERIFICATION,
                    WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                    updater=lambda current: current.model_copy(
                        update={
                            "baseline_verification": baseline_result,
                            "agent_records": [*current.agent_records, base_rec],
                        }
                    ),
                    reason="Baseline verification completed; executing identical approved tests against modified workspace.",
                )
            else:
                # Baseline unavailable: gracefully preserve useful modified verification results!
                prep_fail_rec = AgentExecutionRecord(
                    agent_name="WorkspaceManager",
                    stage=WorkflowState.PREPARING_BASELINE.value,
                    status=AgentExecutionStatus.FAILED,
                    started_at=base_prep_started,
                    completed_at=utc_now(),
                    duration_ms=base_prep_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=(
                        baseline_result.error.message
                        if baseline_result and baseline_result.error
                        else "Baseline workspace unavailable."
                    ),
                    error=baseline_result.error if baseline_result else None,
                )
                run = self.transition_state(
                    run_id,
                    WorkflowState.PREPARING_BASELINE,
                    WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                    updater=lambda current: current.model_copy(
                        update={
                            "baseline_verification": baseline_result,
                            "agent_records": [*current.agent_records, prep_fail_rec],
                        }
                    ),
                    reason=(
                        "Baseline workspace unavailable; proceeding to modified workspace verification "
                        "(comparison will be marked incomplete)."
                    ),
                )

            # ---- Stage 4C: RUNNING_MODIFIED_VERIFICATION ----
            # Re-verify modified workspace snapshot right before running tests
            try:
                self._verify_workspace_snapshot(run)
            except WorkflowIntegrityError as exc:
                err = AgentError(code=exc.code, message=exc.message, details=exc.details)
                self._log(
                    run_id=run_id,
                    agent_name="IntegrityVerifier",
                    stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                    status=AgentExecutionStatus.FAILED,
                    output_summary=exc.message,
                    error=err,
                )
                return self._fail_run(
                    run_id, WorkflowState.RUNNING_MODIFIED_VERIFICATION, err
                )

            m04_started = utc_now()
            m04_t0 = time.perf_counter()
            self._log(
                run_id=run_id,
                agent_name="VerificationAdapter",
                stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                status=AgentExecutionStatus.SUCCEEDED,
                input_summary=(
                    f"Executing {len(selected_ids)} approved scenario(s) on modified workspace "
                    f"(timeout={self.config.verification_timeout_seconds}s, "
                    f"trusted={is_trusted_workspace})."
                ),
                output_summary="Invoked M04 VerificationAdapter on modified workspace.",
            )

            try:
                snap_mod_res, ver_result, mod_integrity_err = (
                    execute_snapshot_verification(
                        self.verification_adapter,
                        self.config,
                        snapshot_role="modified",
                        workspace_path=run.workspace_path,
                        source_path=run.repository.source_path,
                        selected_test_ids=selected_ids,
                        approved_specs=specs,
                        test_purposes=test_purposes,
                        criterion_ids_by_test=criterion_ids_by_test,
                        is_trusted_workspace=is_trusted_workspace,
                        source_snapshot_sha256=run.source_snapshot_sha256,
                        git_diff_sha256=run.git_diff_sha256,
                        baseline_ref=baseline_ref_label,
                        baseline_commit=run.repository.resolved_base_commit,
                    )
                )
                assert isinstance(snap_mod_res, ModifiedVerificationResult)
                modified_result = snap_mod_res
            except Exception as exc:  # noqa: BLE001
                m04_dur_ms = int((time.perf_counter() - m04_t0) * 1000)
                err = AgentError(
                    code="M04_UNEXPECTED_ERROR",
                    message=f"VerificationAdapter raised an unexpected exception: {exc}",
                )
                self._log(
                    run_id=run_id,
                    agent_name="VerificationAdapter",
                    stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                    status=AgentExecutionStatus.FAILED,
                    duration_ms=m04_dur_ms,
                    output_summary="M04 modified verification failed with unexpected exception.",
                    error=err,
                )
                return self._fail_run(
                    run_id, WorkflowState.RUNNING_MODIFIED_VERIFICATION, err
                )

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
                f"Modified verification outcome={ver_result.verification_outcome.value}, "
                f"passed_flag={ver_result.verification_passed}, "
                f"exit_code={ver_result.exit_code}, "
                f"total={ver_result.total_tests}, passed={ver_result.passed}, failed={ver_result.failed}."
            )
            m04_rec = AgentExecutionRecord(
                agent_name="VerificationAdapter",
                stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION.value,
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
                stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                status=m04_exec_status,
                duration_ms=m04_dur_ms,
                input_summary=f"modified scenarios={selected_ids}",
                output_summary=m04_summary,
                error=ver_result.error,
            )

            # Hard integrity failure on exported test files stops the workflow immediately.
            if mod_integrity_err is not None:
                return self._fail_run(
                    run_id,
                    WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                    mod_integrity_err,
                    updater=lambda current: current.model_copy(
                        update={
                            "modified_verification": modified_result,
                            "verification": ver_result,
                            "agent_records": [*current.agent_records, m04_rec],
                        }
                    ),
                    reason=mod_integrity_err.message,
                )

            run = self.transition_state(
                run_id,
                WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                WorkflowState.COMPARING_VERIFICATION_RESULTS,
                updater=lambda current: current.model_copy(
                    update={
                        "modified_verification": modified_result,
                        "verification": ver_result,
                        "agent_records": [*current.agent_records, m04_rec],
                    }
                ),
                reason="Modified verification completed; comparing baseline and modified verification evidence.",
            )

            # ---- Stage 4D: COMPARING_VERIFICATION_RESULTS ----
            cmp_started = utc_now()
            cmp_t0 = time.perf_counter()
            try:
                comparison = BaselineComparisonEngine.compare(
                    baseline=baseline_result,
                    modified=modified_result,
                    approved_test_hashes=run.test_review.approved_test_hashes,
                    baseline_ref=baseline_ref_label,
                    baseline_commit=run.repository.resolved_base_commit,
                    baseline_source_snapshot_sha256=baseline_snapshot_sha,
                    modified_source_snapshot_sha256=run.source_snapshot_sha256,
                )
            except Exception as exc:  # noqa: BLE001
                cmp_dur_ms = int((time.perf_counter() - cmp_t0) * 1000)
                err = AgentError(
                    code="COMPARISON_ENGINE_ERROR",
                    message=f"Baseline comparison engine failed unexpectedly: {exc}",
                )
                self._log(
                    run_id=run_id,
                    agent_name="BaselineComparisonEngine",
                    stage=WorkflowState.COMPARING_VERIFICATION_RESULTS,
                    status=AgentExecutionStatus.FAILED,
                    duration_ms=cmp_dur_ms,
                    output_summary=err.message,
                    error=err,
                )
                return self._fail_run(
                    run_id, WorkflowState.COMPARING_VERIFICATION_RESULTS, err
                )

            cmp_dur_ms = int((time.perf_counter() - cmp_t0) * 1000)
            ver_result = ver_result.model_copy(
                update={"baseline_comparison": comparison}
            )
            cmp_summary = (
                f"Comparison status={comparison.status.value}: "
                f"preserved={comparison.summary.preserved_behavior_count}, "
                f"newly_introduced_failures={comparison.summary.newly_introduced_failure_count}, "
                f"newly_satisfied_acceptance={comparison.summary.newly_satisfied_acceptance_count}, "
                f"fixed_existing={comparison.summary.fixed_existing_failure_count}, "
                f"persistent_failures={comparison.summary.persistent_failure_count}, "
                f"uncomparable_or_unavailable={len(comparison.summary.unavailable_or_incomparable)}."
            )
            cmp_rec = AgentExecutionRecord(
                agent_name="BaselineComparisonEngine",
                stage=WorkflowState.COMPARING_VERIFICATION_RESULTS.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=cmp_started,
                completed_at=utc_now(),
                duration_ms=cmp_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=cmp_summary,
                assumptions=list(comparison.evidence_limitations),
            )
            self._log(
                run_id=run_id,
                agent_name="BaselineComparisonEngine",
                stage=WorkflowState.COMPARING_VERIFICATION_RESULTS,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=cmp_dur_ms,
                input_summary=(
                    f"baseline={baseline_result.verification_outcome.value if baseline_result else 'none'}, "
                    f"modified={modified_result.verification_outcome.value}"
                ),
                output_summary=cmp_summary,
            )

            # Transition COMPARING_VERIFICATION_RESULTS -> COMPILING_REPORT (verify_existing_change)
            # OR COMPARING_VERIFICATION_RESULTS -> EVALUATING_REPAIR_ELIGIBILITY (generate_and_verify_change)
            if run.workflow_mode != WorkflowMode.GENERATE_AND_VERIFY_CHANGE:
                run = self.transition_state(
                    run_id,
                    WorkflowState.COMPARING_VERIFICATION_RESULTS,
                    WorkflowState.COMPILING_REPORT,
                    updater=lambda current: current.model_copy(
                        update={
                            "verification": ver_result,
                            "baseline_comparison": comparison,
                            "agent_records": [*current.agent_records, cmp_rec],
                        }
                    ),
                    reason=(
                        f"Baseline-versus-modified comparison finished (status={comparison.status.value}); "
                        "compiling auditable change package report."
                    ),
                )
            else:
                initial_policy = run.repair_policy or self.config.build_repair_policy()
                initial_durations: dict[str, int] = {
                    "baseline_verification_ms": (
                        base_ver_dur_ms if baseline_ready else 0
                    ),
                    "modified_verification_ms": m04_dur_ms,
                    "comparison_ms": cmp_dur_ms,
                }
                for rec in run.agent_records:
                    if rec.stage == WorkflowState.GENERATING_PATCH.value:
                        initial_durations["patch_generation_ms"] = rec.duration_ms
                    elif rec.stage == WorkflowState.VALIDATING_PATCH.value:
                        initial_durations["patch_validation_ms"] = rec.duration_ms
                    elif rec.stage == WorkflowState.APPLYING_PATCH.value:
                        initial_durations["patch_application_ms"] = rec.duration_ms
                    elif rec.stage == WorkflowState.ANALYZING_POST_PATCH_IMPACT.value:
                        initial_durations["impact_analysis_ms"] = rec.duration_ms

                init_failures = list(modified_result.failures)
                if modified_result.existing_regression_suite is not None:
                    init_failures.extend(modified_result.existing_regression_suite.failures)
                init_errors: list[AgentError] = []
                if modified_result.error is not None:
                    init_errors.append(modified_result.error)
                if baseline_result is not None and baseline_result.error is not None:
                    init_errors.append(baseline_result.error)
                init_passed_clean = bool(
                    modified_result.verification_passed
                    and comparison.status == VerificationComparisonStatus.COMPLETED
                    and not comparison.summary.has_confirmed_regression
                    and (
                        modified_result.existing_regression_suite is None
                        or modified_result.existing_regression_suite.verification_outcome
                        == VerificationOutcome.PASSED
                    )
                )
                init_sig = compute_failure_signature(
                    modified=modified_result,
                    comparison=comparison,
                )
                init_proposal = (
                    run.patch_generation.proposal
                    if run.patch_generation is not None
                    else None
                )
                init_attempt = PatchAttemptRecord(
                    attempt_number=1,
                    attempt_type="initial",
                    repair_attempt_index=0,
                    original_requirement=run.requirements,
                    acceptance_criterion_ids=(
                        [ac.criterion_id for ac in run.requirement_analysis.acceptance_criteria]
                        if run.requirement_analysis is not None
                        else []
                    ),
                    previous_proposal_id=None,
                    previous_patch_sha256=None,
                    base_snapshot_sha256=run.original_source_snapshot_sha256,
                    resulting_source_snapshot_sha256=run.source_snapshot_sha256,
                    resulting_git_diff_sha256=run.git_diff_sha256,
                    patch_generation=run.patch_generation,
                    patch_proposal_id=(
                        init_proposal.proposal_id if init_proposal is not None else None
                    ),
                    patch_sha256=run.patch_proposal_sha256,
                    patch_validation=run.patch_validation,
                    patch_application=run.patch_application,
                    approved_test_ids=list(selected_ids),
                    approved_test_hashes=dict(run.test_review.approved_test_hashes),
                    baseline_reused=False,
                    baseline_verification=baseline_result,
                    modified_verification=modified_result,
                    failure_diagnostics=init_failures,
                    failure_signature=init_sig,
                    baseline_comparison=comparison,
                    repair_eligibility=None,
                    provider_invoked=True,
                    consumed_repair_budget=False,
                    is_deterministic_fixture=(
                        run.patch_generation.is_deterministic_fixture
                        if run.patch_generation is not None
                        else False
                    ),
                    is_mock_output=(
                        run.patch_generation.is_mock_output
                        if run.patch_generation is not None
                        else False
                    ),
                    model_label=(
                        run.patch_generation.model_metadata.model_label
                        if run.patch_generation is not None
                        else None
                    ),
                    durations_ms=initial_durations,
                    started_at=run.created_at,
                    completed_at=utc_now(),
                    outcome="passed" if init_passed_clean else "failed_verification",
                    errors=init_errors,
                )
                init_history = RepairAttemptHistory(
                    policy=initial_policy,
                    total_proposals_attempted=1,
                    repair_attempts_consumed=run.repair_attempts_consumed,
                    current_attempt_number=1,
                    remaining_repair_budget=max(
                        0,
                        initial_policy.effective_max_repair_attempts
                        - run.repair_attempts_consumed,
                    ),
                    attempts=[init_attempt],
                    seen_patch_hashes=(
                        [run.patch_proposal_sha256] if run.patch_proposal_sha256 else []
                    ),
                    seen_failure_signatures=[init_sig] if init_sig else [],
                    final_candidate_attempt_number=1,
                    final_candidate_proposal_id=(
                        init_proposal.proposal_id if init_proposal is not None else None
                    ),
                    final_candidate_patch_sha256=run.patch_proposal_sha256,
                )
                run = self.transition_state(
                    run_id,
                    WorkflowState.COMPARING_VERIFICATION_RESULTS,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    updater=lambda current: current.model_copy(
                        update={
                            "verification": ver_result,
                            "baseline_comparison": comparison,
                            "repair_policy": initial_policy,
                            "repair_history": init_history,
                            "agent_records": [*current.agent_records, cmp_rec],
                        }
                    ),
                    reason=(
                        f"Initial verification comparison finished (status={comparison.status.value}); "
                        "evaluating bounded automated patch repair eligibility."
                    ),
                )

                run, ver_result = self._run_bounded_repair_loop(
                    run=run,
                    specs=specs,
                    selected_ids=selected_ids,
                    scenarios_by_id=scenarios_by_id,
                    test_purposes=test_purposes,
                    criterion_ids_by_test=criterion_ids_by_test,
                    is_trusted_workspace=is_trusted_workspace,
                    baseline_ref_label=baseline_ref_label,
                    baseline_result=baseline_result,
                    baseline_snapshot_sha=baseline_snapshot_sha,
                    modified_result=modified_result,
                    comparison=comparison,
                    ver_result=ver_result,
                )
                if run.state != WorkflowState.COMPILING_REPORT:
                    return run
        finally:
            if self._cleanup_baseline_workspace and baseline_workspace_path:
                try:
                    self.workspace_manager.cleanup_workspace(baseline_workspace_path)
                except WorkspaceError:
                    pass

        # ---- Stage 5: Compile Auditable Change Package ----
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
            input_summary=(
                f"verification_outcome={ver_result.verification_outcome.value}, "
                f"comparison_status={ver_result.baseline_comparison.status.value}"
            ),
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

    # ------------------------------------------------------------------
    # Phase 8 (P0-8): Bounded Automated Patch Repair Orchestration
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_approved_tests_in_workspace(
        workspace_path: str | Path,
        specs: Mapping[str, ApprovedTestSpec],
        selected_ids: list[str],
    ) -> tuple[bool, dict[str, str], AgentError | None]:
        """Verify every approved test file in ``workspace_path`` matches its Gate 1 SHA-256."""
        ws_root = Path(workspace_path).resolve()
        verified: dict[str, str] = {}
        for tid in selected_ids:
            spec = specs.get(tid)
            if spec is None or not spec.approved:
                return (
                    False,
                    verified,
                    AgentError(
                        code="UNAPPROVED_TEST",
                        message=f"Scenario '{tid}' is not registered as approved.",
                    ),
                )
            candidate = ws_root / spec.relative_path
            if candidate.is_symlink() or not candidate.is_file():
                return (
                    False,
                    verified,
                    AgentError(
                        code="TEST_FILE_MISSING",
                        message=f"Approved test file missing or unsafe inside workspace: '{spec.relative_path}'",
                        details={"test_id": tid},
                    ),
                )
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(ws_root):
                return (
                    False,
                    verified,
                    AgentError(
                        code="UNAUTHORIZED_TEST_PATH",
                        message=f"Approved test path escapes workspace: '{spec.relative_path}'",
                        details={"test_id": tid},
                    ),
                )
            actual_sha = hashlib.sha256(resolved.read_bytes()).hexdigest()
            if not hmac.compare_digest(actual_sha, spec.expected_sha256):
                return (
                    False,
                    verified,
                    AgentError(
                        code="TEST_CONTENT_MODIFIED",
                        message=(
                            f"Approved test '{tid}' ({spec.relative_path}) no longer matches "
                            "its Gate 1 approved SHA-256 digest."
                        ),
                        details={
                            "test_id": tid,
                            "expected_sha256": spec.expected_sha256,
                            "actual_sha256": actual_sha,
                        },
                    ),
                )
            verified[tid] = actual_sha
        return True, verified, None

    def _is_baseline_reusable(
        self,
        *,
        policy: RepairPolicy,
        baseline_result: BaselineVerificationResult | None,
        baseline_snapshot_sha: str | None,
        original_snapshot_sha: str | None,
        resolved_base_commit: str | None,
        expected_approved_hashes: Mapping[str, str],
        repair_workspace_path: str | Path,
        source_path: str,
    ) -> bool:
        """Return True iff baseline snapshot, approved test hashes, and regression suite hashes match."""
        if not policy.reuse_validated_baseline:
            return False
        if baseline_result is None or baseline_result.execution_status != "completed":
            return False
        if not resolved_base_commit or baseline_result.baseline_commit != resolved_base_commit:
            return False
        if (
            not baseline_snapshot_sha
            or not original_snapshot_sha
            or not baseline_result.source_snapshot_sha256
        ):
            return False
        if not (
            hmac.compare_digest(baseline_result.source_snapshot_sha256, baseline_snapshot_sha)
            and hmac.compare_digest(baseline_snapshot_sha, original_snapshot_sha)
        ):
            return False
        if dict(baseline_result.approved_test_hashes) != dict(expected_approved_hashes):
            return False

        # Verify existing repository regression test hashes in repair workspace match baseline
        discovered_now = discover_existing_regression_tests(
            repair_workspace_path,
            source_path=source_path,
            timeout_seconds=self.config.git_timeout_seconds,
        )
        ws_root = Path(repair_workspace_path).resolve()
        current_reg_hashes = {
            rel: hashlib.sha256((ws_root / rel).read_bytes()).hexdigest()
            for rel in discovered_now
            if (ws_root / rel).is_file()
        }
        base_reg_hashes = (
            dict(baseline_result.existing_regression_suite.test_file_hashes)
            if baseline_result.existing_regression_suite is not None
            else {}
        )
        return current_reg_hashes == base_reg_hashes

    @staticmethod
    def _termination_reason_from_eligibility(
        category: RepairEligibilityCategory,
        *,
        repair_attempts_consumed: int,
    ) -> RepairTerminationReason:
        if category == RepairEligibilityCategory.NOT_NEEDED_VERIFICATION_PASSED:
            return (
                RepairTerminationReason.REPAIR_SUCCEEDED
                if repair_attempts_consumed > 0
                else RepairTerminationReason.NOT_NEEDED
            )
        if category == RepairEligibilityCategory.REPAIR_DISABLED:
            return RepairTerminationReason.REPAIR_DISABLED
        if category == RepairEligibilityCategory.WORKFLOW_MODE_UNSUPPORTED:
            return RepairTerminationReason.WORKFLOW_MODE_UNSUPPORTED
        if category == RepairEligibilityCategory.BUDGET_EXHAUSTED:
            return RepairTerminationReason.BUDGET_EXHAUSTED
        if category == RepairEligibilityCategory.WORKSPACE_OR_TEST_INTEGRITY_VIOLATION:
            return RepairTerminationReason.WORKSPACE_INTEGRITY_ERROR
        if category == RepairEligibilityCategory.PROVIDER_AUTH_OR_RATE_LIMIT:
            return RepairTerminationReason.PROVIDER_ERROR
        return RepairTerminationReason.INELIGIBLE_FAILURE

    def _run_bounded_repair_loop(
        self,
        *,
        run: WorkflowRunRecord,
        specs: Mapping[str, ApprovedTestSpec],
        selected_ids: list[str],
        scenarios_by_id: Mapping[str, Any],
        test_purposes: Mapping[str, Any],
        criterion_ids_by_test: Mapping[str, list[str]],
        is_trusted_workspace: bool,
        baseline_ref_label: str,
        baseline_result: BaselineVerificationResult | None,
        baseline_snapshot_sha: str | None,
        modified_result: ModifiedVerificationResult,
        comparison: BaselineComparisonEvidence,
        ver_result: VerificationResult,
    ) -> tuple[WorkflowRunRecord, VerificationResult]:
        """Execute bounded automated patch repair loop (max 2 repair attempts, 3 total proposals).

        Starts in ``WorkflowState.EVALUATING_REPAIR_ELIGIBILITY`` and finishes in
        ``WorkflowState.COMPILING_REPORT`` (or ``WorkflowState.FAILED`` on hard
        integrity violations). Never auto-approves, commits, merges, pushes, or
        mutates the original repository.
        """
        run_id = run.run_id
        assert run.repair_history is not None
        assert run.test_review is not None
        history = run.repair_history
        policy = history.policy
        initial_cb = run.codebase_analysis
        approved_test_codes = {
            sid: scenarios_by_id[sid].test_code
            for sid in selected_ids
            if sid in scenarios_by_id
        }

        known_repo_modules: set[str] = set()
        if run.pre_change_context is not None:
            for rel_p in run.pre_change_context.indexed_file_paths:
                known_repo_modules.add(PurePath(rel_p).stem)
                for part in PurePath(rel_p).parts[:-1]:
                    known_repo_modules.add(part)

        protected_test_paths: set[str] = {
            spec.relative_path for spec in specs.values()
        }
        if baseline_result is not None and baseline_result.existing_regression_suite is not None:
            protected_test_paths.update(
                baseline_result.existing_regression_suite.discovered_test_paths
            )
        if modified_result.existing_regression_suite is not None:
            protected_test_paths.update(
                modified_result.existing_regression_suite.discovered_test_paths
            )
        if run.workspace_path and Path(run.workspace_path).is_dir():
            protected_test_paths.update(
                discover_existing_regression_tests(
                    run.workspace_path,
                    source_path=run.repository.source_path,
                    timeout_seconds=self.config.git_timeout_seconds,
                )
            )

        valid_cids = (
            {ac.criterion_id for ac in run.requirement_analysis.acceptance_criteria}
            if run.requirement_analysis is not None
            else set()
        )
        raw_allowed_paths = (
            list(run.patch_generation_request.allowed_modification_paths)
            if run.patch_generation_request is not None
            and run.patch_generation_request.allowed_modification_paths
            else (
                [run.repository.source_path]
                if run.repository.source_path not in {"", "."}
                else (
                    list(run.pre_change_context.indexed_file_paths)
                    if run.pre_change_context is not None
                    else []
                )
            )
        )
        allowed_mod_paths = [
            p
            for p in raw_allowed_paths
            if p not in protected_test_paths
            and ".changeguard_generated_tests" not in PurePath(p).parts
            and "tests" not in PurePath(p).parts
        ]

        last_ver_result = ver_result
        last_modified = modified_result
        last_comparison = comparison
        active_baseline_result = baseline_result
        active_baseline_snapshot_sha = baseline_snapshot_sha
        last_provider_error: AgentError | None = None

        def _finalize_to_report(
            current_run: WorkflowRunRecord,
            from_state: WorkflowState,
            hist: RepairAttemptHistory,
            reason_enum: RepairTerminationReason,
            summary_text: str,
            eligibility_obj: RepairEligibilityResult | None,
        ) -> tuple[WorkflowRunRecord, VerificationResult]:
            final_hist = hist.model_copy(
                update={
                    "last_eligibility": eligibility_obj or hist.last_eligibility,
                    "termination_reason": reason_enum,
                    "termination_summary": redact_secrets(summary_text),
                }
            )
            updated_run = self.transition_state(
                run_id,
                from_state,
                WorkflowState.COMPILING_REPORT,
                updater=lambda cur: cur.model_copy(
                    update={
                        "repair_attempts_consumed": final_hist.repair_attempts_consumed,
                        "repair_eligibility": final_hist.last_eligibility,
                        "repair_termination_reason": reason_enum,
                        "repair_history": final_hist,
                    }
                ),
                reason=f"Bounded repair ended ({reason_enum.value}): {summary_text}",
            )
            return updated_run, last_ver_result

        # Hard structural upper bound: at most effective_max_repair_attempts + 1 evaluations
        for _iteration in range(policy.effective_max_repair_attempts + 1):
            # 1. Verify active workspace & approved test integrity before eligibility evaluation
            try:
                self._verify_workspace_snapshot(run)
            except WorkflowIntegrityError as exc:
                err = AgentError(code=exc.code, message=exc.message, details=exc.details)
                self._log(
                    run_id=run_id,
                    agent_name="IntegrityVerifier",
                    stage=WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    status=AgentExecutionStatus.FAILED,
                    output_summary=exc.message,
                    error=err,
                )
                return (
                    self._fail_run(
                        run_id, WorkflowState.EVALUATING_REPAIR_ELIGIBILITY, err
                    ),
                    last_ver_result,
                )

            assert run.workspace_path is not None
            tests_ok, _verified_hashes, test_integrity_err = (
                self._verify_approved_tests_in_workspace(
                    run.workspace_path, specs, selected_ids
                )
            )
            if not tests_ok and test_integrity_err is not None:
                self._log(
                    run_id=run_id,
                    agent_name="IntegrityVerifier",
                    stage=WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    status=AgentExecutionStatus.FAILED,
                    output_summary=test_integrity_err.message,
                    error=test_integrity_err,
                )
                return (
                    self._fail_run(
                        run_id,
                        WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                        test_integrity_err,
                    ),
                    last_ver_result,
                )

            # 2. Evaluate conservative deterministic repair eligibility
            elig_started = utc_now()
            elig_t0 = time.perf_counter()
            eligibility = RepairEligibilityEvaluator.evaluate(
                workflow_mode=run.workflow_mode,
                policy=policy,
                patch_application=run.patch_application,
                requirement_analysis=run.requirement_analysis,
                baseline=active_baseline_result,
                modified=last_modified,
                comparison=last_comparison,
                current_attempt_number=history.current_attempt_number,
                repair_attempts_consumed=history.repair_attempts_consumed,
                workspace_integrity_ok=True,
                approved_tests_valid_and_unchanged=tests_ok,
                provider_error=last_provider_error,
                known_repo_modules=known_repo_modules,
            )
            elig_dur_ms = int((time.perf_counter() - elig_t0) * 1000)

            # Attach eligibility decision onto the most recent verified attempt without mutating prior ones
            updated_attempts = list(history.attempts)
            if updated_attempts and updated_attempts[-1].repair_eligibility is None:
                updated_attempts[-1] = updated_attempts[-1].model_copy(
                    update={"repair_eligibility": eligibility}
                )
            history = history.model_copy(
                update={
                    "attempts": updated_attempts,
                    "last_eligibility": eligibility,
                }
            )

            elig_rec = AgentExecutionRecord(
                agent_name="RepairEligibilityEvaluator",
                stage=WorkflowState.EVALUATING_REPAIR_ELIGIBILITY.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=elig_started,
                completed_at=utc_now(),
                duration_ms=elig_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    f"eligible={eligibility.eligible}, category={eligibility.category.value}, "
                    f"remaining_budget={eligibility.remaining_repair_budget}: "
                    f"{eligibility.decision_reason}"
                ),
            )
            self._log(
                run_id=run_id,
                agent_name="RepairEligibilityEvaluator",
                stage=WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=elig_dur_ms,
                input_summary=(
                    f"attempt={history.current_attempt_number}, "
                    f"repair_consumed={history.repair_attempts_consumed}/{policy.effective_max_repair_attempts}"
                ),
                output_summary=elig_rec.output_summary,
            )
            run = self.store.update_run_atomic(
                run_id,
                expected_state=WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                updater=lambda cur: cur.model_copy(
                    update={
                        "repair_eligibility": eligibility,
                        "repair_history": history,
                        "agent_records": [*cur.agent_records, elig_rec],
                    }
                ),
            )

            # 3. Stop if not eligible
            if not eligibility.eligible or eligibility.next_attempt_number is None:
                term_reason = self._termination_reason_from_eligibility(
                    eligibility.category,
                    repair_attempts_consumed=history.repair_attempts_consumed,
                )
                return _finalize_to_report(
                    run,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    history,
                    term_reason,
                    eligibility.decision_reason,
                    eligibility,
                )

            next_attempt = eligibility.next_attempt_number
            repair_idx = next_attempt - 1
            if (
                history.repair_attempts_consumed >= policy.effective_max_repair_attempts
                or next_attempt > policy.max_total_proposals
            ):
                return _finalize_to_report(
                    run,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    history,
                    RepairTerminationReason.BUDGET_EXHAUSTED,
                    "Configured bounded repair budget exhausted.",
                    eligibility,
                )

            # 4. Prepare clean isolated repair workspace from the immutable original commit
            repair_attempt_started = utc_now()
            repair_durations: dict[str, int] = {}
            resolved_base_commit = run.repository.resolved_base_commit
            if (
                not resolved_base_commit
                or not run.original_source_snapshot_sha256
                or not run.original_git_diff_sha256
            ):
                err = AgentError(
                    code="ORIGINAL_SNAPSHOT_UNAVAILABLE",
                    message=(
                        "Immutable original snapshot metadata is unavailable; cannot safely "
                        "restore a clean workspace for bounded patch repair."
                    ),
                )
                return _finalize_to_report(
                    run,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    history,
                    RepairTerminationReason.WORKSPACE_INTEGRITY_ERROR,
                    err.message,
                    eligibility,
                )

            repair_ws_ctx = None
            repair_ws_path: str | None = None
            try:
                repair_ws_ctx = self.workspace_manager.prepare_clean_repair_workspace(
                    run_id,
                    run.repository.repository_path,
                    resolved_base_commit,
                    next_attempt,
                    source_path=run.repository.source_path,
                    base_ref=run.repository.base_ref,
                )
                repair_ws_path = repair_ws_ctx.workspace_repo_path
                clean_snap = compute_source_snapshot_sha256(
                    repair_ws_path, repair_ws_ctx.source_path
                )
                clean_diff = compute_git_diff_sha256(
                    repair_ws_path,
                    repair_ws_ctx.effective_base_ref,
                    repair_ws_ctx.source_path,
                    timeout_seconds=self.config.git_timeout_seconds,
                )
                if not (
                    hmac.compare_digest(clean_snap, run.original_source_snapshot_sha256)
                    and hmac.compare_digest(clean_diff, run.original_git_diff_sha256)
                ):
                    err = AgentError(
                        code="ORIGINAL_SNAPSHOT_MISMATCH",
                        message=(
                            "Restored clean repair workspace snapshot does not match the "
                            "approved immutable original snapshot."
                        ),
                        details={
                            "expected_snapshot_sha256": run.original_source_snapshot_sha256,
                            "actual_snapshot_sha256": clean_snap,
                        },
                    )
                    self.workspace_manager.cleanup_workspace(repair_ws_path)
                    return (
                        self._fail_run(
                            run_id, WorkflowState.EVALUATING_REPAIR_ELIGIBILITY, err
                        ),
                        last_ver_result,
                    )

                target_modules: list[str] = []
                if run.requirement_analysis is not None:
                    for ac in run.requirement_analysis.acceptance_criteria:
                        for mod in ac.target_modules:
                            if mod not in target_modules:
                                target_modules.append(mod)
                pre_ctx_clean, clean_relevant_files = (
                    self.codebase_adapter.collect_pre_change_context(
                        repair_ws_path,
                        source_path=repair_ws_ctx.source_path,
                        base_ref=repair_ws_ctx.effective_base_ref,
                        target_modules=target_modules,
                    )
                )
                if pre_ctx_clean.status != "completed" or not clean_relevant_files:
                    err = pre_ctx_clean.error or AgentError(
                        code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                        message="Failed to collect clean original source files for repair request.",
                    )
                    self.workspace_manager.cleanup_workspace(repair_ws_path)
                    return _finalize_to_report(
                        run,
                        WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                        history,
                        RepairTerminationReason.WORKSPACE_INTEGRITY_ERROR,
                        err.message,
                        eligibility,
                    )
            except Exception as exc:  # noqa: BLE001
                if repair_ws_path:
                    try:
                        self.workspace_manager.cleanup_workspace(repair_ws_path)
                    except WorkspaceError:
                        pass
                err = AgentError(
                    code="REPAIR_WORKSPACE_PREPARATION_FAILED",
                    message=f"Failed to restore clean repair workspace: {exc}",
                )
                return (
                    self._fail_run(
                        run_id, WorkflowState.EVALUATING_REPAIR_ELIGIBILITY, err
                    ),
                    last_ver_result,
                )

            # 5. Build typed, sanitized A4 repair request targeting the immutable original snapshot
            prev_proposal = (
                run.patch_generation.proposal
                if run.patch_generation is not None
                else None
            )
            prev_patch_diff = (
                redact_secrets(prev_proposal.unified_diff)
                if prev_proposal is not None
                else ""
            )
            prev_patch_sha = (
                prev_proposal.patch_sha256
                if prev_proposal is not None
                else run.patch_proposal_sha256
            )
            prev_proposal_id = (
                prev_proposal.proposal_id if prev_proposal is not None else None
            )
            sanitized_diags, comparison_payload, prev_ver_payload = (
                build_repair_request_evidence(
                    comparison=last_comparison,
                    modified=last_modified,
                    eligibility=eligibility,
                )
            )
            repair_request = PatchGenerationRequest(
                run_id=run_id,
                original_requirement=run.requirements,
                acceptance_criteria=(
                    list(run.requirement_analysis.acceptance_criteria)
                    if run.requirement_analysis is not None
                    else []
                ),
                business_constraints=(
                    list(run.requirement_analysis.business_constraints)
                    if run.requirement_analysis is not None
                    else []
                ),
                unresolved_ambiguities=(
                    [
                        amb
                        for amb in run.requirement_analysis.ambiguities
                        if not amb.resolved
                    ]
                    if run.requirement_analysis is not None
                    else []
                ),
                repository_snapshot_ref=resolved_base_commit,
                base_ref=run.repository.base_ref,
                resolved_base_commit=resolved_base_commit,
                source_snapshot_sha256=run.original_source_snapshot_sha256,
                source_path=run.repository.source_path,
                codebase_context=pre_ctx_clean,
                allowed_modification_paths=allowed_mod_paths,
                relevant_source_files=clean_relevant_files,
                relevant_source_file_paths=sorted(clean_relevant_files.keys()),
                attempt_number=next_attempt,
                attempt_type="repair",
                repair_attempt_number=repair_idx,
                execution_mode=run.execution_mode.value,
                previous_proposal_id=prev_proposal_id,
                previous_proposed_patch=prev_patch_diff,
                previous_patch_sha256=prev_patch_sha,
                prior_patch_hashes=list(history.seen_patch_hashes),
                failure_diagnostics=sanitized_diags,
                baseline_comparison_evidence=comparison_payload,
                failed_criterion_ids=list(eligibility.failed_criterion_ids),
                previous_verification_evidence=prev_ver_payload,
            )

            # 6. Atomically consume repair budget and persist BEFORE invoking A4 provider!
            new_consumed = history.repair_attempts_consumed + 1
            new_total_proposals = history.total_proposals_attempted + 1
            history = history.model_copy(
                update={
                    "total_proposals_attempted": new_total_proposals,
                    "repair_attempts_consumed": new_consumed,
                    "current_attempt_number": next_attempt,
                    "remaining_repair_budget": max(
                        0, policy.effective_max_repair_attempts - new_consumed
                    ),
                }
            )
            run = self.transition_state(
                run_id,
                WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                WorkflowState.REPAIRING_PATCH,
                updater=lambda cur: cur.model_copy(
                    update={
                        "patch_generation_request": repair_request,
                        "patch_attempt_number": next_attempt,
                        "repair_attempts_consumed": new_consumed,
                        "repair_history": history,
                    }
                ),
                reason=(
                    f"Starting bounded repair attempt #{repair_idx} "
                    f"(proposal {next_attempt}/{policy.max_total_proposals}); "
                    f"budget consumed={new_consumed}/{policy.effective_max_repair_attempts}."
                ),
            )

            if self._before_repair_provider_hook is not None:
                self._before_repair_provider_hook(run, next_attempt)

            # 7. Invoke A4 PatchGenerationAdapter
            a4_started = utc_now()
            a4_t0 = time.perf_counter()
            self._log(
                run_id=run_id,
                agent_name="PatchGenerationAdapter",
                stage=WorkflowState.REPAIRING_PATCH,
                status=AgentExecutionStatus.SUCCEEDED,
                input_summary=(
                    f"repair_attempt={repair_idx}, attempt={next_attempt}, "
                    f"mode={run.execution_mode.value}, "
                    f"failed_criteria={eligibility.failed_criterion_ids}, "
                    f"prev_sha={prev_patch_sha[:12] if prev_patch_sha else 'none'}"
                ),
                output_summary="Invoked A4 PatchGenerationAdapter.generate for bounded repair.",
            )
            try:
                patch_result = self.patch_adapter.generate(repair_request)
            except Exception as exc:  # noqa: BLE001
                gen_dur_ms = int((time.perf_counter() - a4_t0) * 1000)
                repair_durations["patch_generation_ms"] = gen_dur_ms
                err = AgentError(
                    code="A4_UNEXPECTED_ERROR",
                    message=f"PatchGenerationAdapter raised an unexpected exception during repair: {exc}",
                )
                patch_result = PatchGenerationResult(
                    status="error",
                    attempt_number=next_attempt,
                    proposal=None,
                    model_metadata=PatchModelMetadata(
                        provider=None,
                        model_label="provider_error",
                        generation_mode=run.execution_mode.value,
                        is_deterministic_fixture=False,
                        is_mock_output=False,
                    ),
                    is_deterministic_fixture=False,
                    is_mock_output=False,
                    error=err,
                )
            else:
                gen_dur_ms = int((time.perf_counter() - a4_t0) * 1000)
                repair_durations["patch_generation_ms"] = gen_dur_ms

            def _make_failed_attempt_record(
                *,
                outcome: Any,
                gen_res: PatchGenerationResult | None = None,
                val_res: Any = None,
                app_res: Any = None,
                errors: list[AgentError] | None = None,
                notes: list[str] | None = None,
            ) -> PatchAttemptRecord:
                prop = gen_res.proposal if gen_res is not None else None
                return PatchAttemptRecord(
                    attempt_number=next_attempt,
                    attempt_type="repair",
                    repair_attempt_index=repair_idx,
                    original_requirement=run.requirements,
                    acceptance_criterion_ids=sorted(valid_cids),
                    previous_proposal_id=prev_proposal_id,
                    previous_patch_sha256=prev_patch_sha,
                    base_snapshot_sha256=run.original_source_snapshot_sha256,
                    patch_generation=gen_res,
                    patch_proposal_id=prop.proposal_id if prop is not None else None,
                    patch_sha256=prop.patch_sha256 if prop is not None else None,
                    patch_validation=val_res,
                    patch_application=app_res,
                    approved_test_ids=list(selected_ids),
                    approved_test_hashes=dict(run.test_review.approved_test_hashes),
                    provider_invoked=True,
                    consumed_repair_budget=True,
                    is_deterministic_fixture=(
                        gen_res.is_deterministic_fixture if gen_res is not None else False
                    ),
                    is_mock_output=(
                        gen_res.is_mock_output if gen_res is not None else False
                    ),
                    model_label=(
                        gen_res.model_metadata.model_label if gen_res is not None else None
                    ),
                    durations_ms=dict(repair_durations),
                    started_at=repair_attempt_started,
                    completed_at=utc_now(),
                    outcome=outcome,
                    errors=list(errors or []),
                    notes=list(notes or []),
                )

            # Handle non-proposed A4 result (unavailable / auth / rate-limit / invalid response)
            if patch_result.status != "proposed" or patch_result.proposal is None:
                err = patch_result.error or AgentError(
                    code="PATCH_REPAIR_GENERATION_FAILED",
                    message=(
                        f"A4 repair attempt #{repair_idx} did not return a valid patch proposal "
                        f"(status='{patch_result.status}')."
                    ),
                )
                last_provider_error = err
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                a4_rec = AgentExecutionRecord(
                    agent_name="PatchGenerationAdapter",
                    stage=WorkflowState.REPAIRING_PATCH.value,
                    status=AgentExecutionStatus.FAILED,
                    started_at=a4_started,
                    completed_at=utc_now(),
                    duration_ms=gen_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=err.message,
                    error=err,
                )
                self._log(
                    run_id=run_id,
                    agent_name="PatchGenerationAdapter",
                    stage=WorkflowState.REPAIRING_PATCH,
                    status=AgentExecutionStatus.FAILED,
                    duration_ms=gen_dur_ms,
                    output_summary=err.message,
                    error=err,
                )
                is_unavailable = (
                    patch_result.status == "unavailable"
                    or err.code == "PATCH_GENERATOR_UNAVAILABLE"
                )
                attempt_rec = _make_failed_attempt_record(
                    outcome=(
                        "generation_unavailable" if is_unavailable else "generation_failed"
                    ),
                    gen_res=patch_result,
                    errors=[err],
                    notes=[
                        "Repair provider invocation consumed repair budget despite returning no usable patch."
                    ],
                )
                history = history.model_copy(
                    update={"attempts": [*history.attempts, attempt_rec]}
                )
                run = self.store.update_run_atomic(
                    run_id,
                    expected_state=WorkflowState.REPAIRING_PATCH,
                    updater=lambda cur: cur.model_copy(
                        update={
                            "repair_history": history,
                            "agent_records": [*cur.agent_records, a4_rec],
                        }
                    ),
                )
                if is_unavailable:
                    return _finalize_to_report(
                        run,
                        WorkflowState.REPAIRING_PATCH,
                        history,
                        RepairTerminationReason.PROVIDER_UNAVAILABLE,
                        err.message,
                        eligibility,
                    )
                if (
                    is_provider_auth_or_rate_limit_error(err)
                    or err.code in {"LIVE_MODE_MOCK_REJECTED", "UNRESOLVED_REQUIREMENT_AMBIGUITY"}
                ):
                    return _finalize_to_report(
                        run,
                        WorkflowState.REPAIRING_PATCH,
                        history,
                        RepairTerminationReason.PROVIDER_ERROR,
                        err.message,
                        eligibility,
                    )
                if new_consumed >= policy.effective_max_repair_attempts:
                    return _finalize_to_report(
                        run,
                        WorkflowState.REPAIRING_PATCH,
                        history,
                        RepairTerminationReason.BUDGET_EXHAUSTED,
                        f"Repair budget exhausted after invalid repair response: {err.message}",
                        eligibility,
                    )
                last_provider_error = None
                run = self.transition_state(
                    run_id,
                    WorkflowState.REPAIRING_PATCH,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    reason=(
                        f"Repair attempt #{repair_idx} failed generation ({err.code}) and "
                        "consumed repair budget; re-evaluating remaining budget."
                    ),
                )
                continue

            proposal = patch_result.proposal
            a4_summary = (
                f"Generated replacement patch '{proposal.proposal_id}' "
                f"(sha256={proposal.patch_sha256[:12]}, repair_attempt={repair_idx}, "
                f"attempt={next_attempt}, model={patch_result.model_metadata.model_label})."
            )
            a4_rec = AgentExecutionRecord(
                agent_name="PatchGenerationAdapter",
                stage=WorkflowState.REPAIRING_PATCH.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=a4_started,
                completed_at=utc_now(),
                duration_ms=gen_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=a4_summary,
                assumptions=list(proposal.assumptions),
            )
            self._log(
                run_id=run_id,
                agent_name="PatchGenerationAdapter",
                stage=WorkflowState.REPAIRING_PATCH,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=gen_dur_ms,
                input_summary=f"repair_attempt={repair_idx}, attempt={next_attempt}",
                output_summary=a4_summary,
            )

            # 8. Duplicate patch detection BEFORE validation/application
            if (
                policy.stop_on_duplicate_patch
                and proposal.patch_sha256 in history.seen_patch_hashes
            ):
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                dup_err = AgentError(
                    code="DUPLICATE_PATCH_PROPOSAL",
                    message=(
                        f"Repair attempt #{repair_idx} proposed an identical patch SHA-256 "
                        f"({proposal.patch_sha256[:12]}) already attempted earlier; stopping "
                        "bounded repair to prevent redundant retries."
                    ),
                    details={"duplicate_patch_sha256": proposal.patch_sha256},
                )
                dup_attempt = _make_failed_attempt_record(
                    outcome="duplicate_patch_rejected",
                    gen_res=patch_result,
                    errors=[dup_err],
                    notes=["Duplicate patch SHA-256 detected; skipped application and verification."],
                )
                history = history.model_copy(
                    update={"attempts": [*history.attempts, dup_attempt]}
                )
                run = self.store.update_run_atomic(
                    run_id,
                    expected_state=WorkflowState.REPAIRING_PATCH,
                    updater=lambda cur: cur.model_copy(
                        update={
                            "repair_history": history,
                            "agent_records": [*cur.agent_records, a4_rec],
                        }
                    ),
                )
                return _finalize_to_report(
                    run,
                    WorkflowState.REPAIRING_PATCH,
                    history,
                    RepairTerminationReason.DUPLICATE_PATCH_DETECTED,
                    dup_err.message,
                    eligibility,
                )

            history = history.model_copy(
                update={
                    "seen_patch_hashes": [*history.seen_patch_hashes, proposal.patch_sha256]
                }
            )
            run = self.transition_state(
                run_id,
                WorkflowState.REPAIRING_PATCH,
                WorkflowState.VALIDATING_PATCH,
                updater=lambda cur: cur.model_copy(
                    update={
                        "repair_history": history,
                        "agent_records": [*cur.agent_records, a4_rec],
                    }
                ),
                reason=(
                    f"Distinct replacement patch '{proposal.proposal_id}' received; "
                    "validating against immutable original snapshot."
                ),
            )

            # 9. Validate replacement patch against clean original snapshot & forbid test modifications
            val_started = utc_now()
            val_t0 = time.perf_counter()
            validation_result = self.patch_validator.validate(
                workspace_repo_path=repair_ws_path,
                proposal=proposal,
                source_path=repair_ws_ctx.source_path,
                base_ref=repair_ws_ctx.effective_base_ref,
                allowed_modification_paths=allowed_mod_paths,
                expected_source_snapshot_sha256=run.original_source_snapshot_sha256,
                expected_git_diff_sha256=run.original_git_diff_sha256,
                valid_criterion_ids=valid_cids if valid_cids else None,
                forbidden_paths=protected_test_paths,
                forbid_test_modifications=True,
            )
            val_dur_ms = int((time.perf_counter() - val_t0) * 1000)
            repair_durations["patch_validation_ms"] = val_dur_ms

            if not validation_result.valid:
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                primary_err = (
                    validation_result.violations[0]
                    if validation_result.violations
                    else AgentError(
                        code="PATCH_VALIDATION_FAILED",
                        message="Replacement patch failed safety or applicability validation.",
                    )
                )
                is_test_mod = any(
                    v.code == "PATCH_TEST_MODIFICATION_FORBIDDEN"
                    for v in validation_result.violations
                )
                val_rec = AgentExecutionRecord(
                    agent_name="PatchValidator",
                    stage=WorkflowState.VALIDATING_PATCH.value,
                    status=AgentExecutionStatus.FAILED,
                    started_at=val_started,
                    completed_at=utc_now(),
                    duration_ms=val_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=primary_err.message,
                    error=primary_err,
                )
                self._log(
                    run_id=run_id,
                    agent_name="PatchValidator",
                    stage=WorkflowState.VALIDATING_PATCH,
                    status=AgentExecutionStatus.FAILED,
                    duration_ms=val_dur_ms,
                    input_summary=f"repair_attempt={repair_idx}, sha={proposal.patch_sha256[:12]}",
                    output_summary=primary_err.message,
                    error=primary_err,
                )
                val_attempt = _make_failed_attempt_record(
                    outcome=(
                        "test_modification_rejected"
                        if is_test_mod
                        else "validation_failed"
                    ),
                    gen_res=patch_result,
                    val_res=validation_result,
                    errors=list(validation_result.violations),
                    notes=[
                        "Invalid replacement patch was rejected before application and consumed repair budget."
                    ],
                )
                history = history.model_copy(
                    update={"attempts": [*history.attempts, val_attempt]}
                )
                run = self.store.update_run_atomic(
                    run_id,
                    expected_state=WorkflowState.VALIDATING_PATCH,
                    updater=lambda cur: cur.model_copy(
                        update={
                            "repair_history": history,
                            "agent_records": [*cur.agent_records, val_rec],
                        }
                    ),
                )
                if is_test_mod:
                    return _finalize_to_report(
                        run,
                        WorkflowState.VALIDATING_PATCH,
                        history,
                        RepairTerminationReason.TEST_MODIFICATION_REJECTED,
                        primary_err.message,
                        eligibility,
                    )
                if new_consumed >= policy.effective_max_repair_attempts:
                    return _finalize_to_report(
                        run,
                        WorkflowState.VALIDATING_PATCH,
                        history,
                        RepairTerminationReason.BUDGET_EXHAUSTED,
                        f"Repair budget exhausted after invalid replacement patch: {primary_err.message}",
                        eligibility,
                    )
                run = self.transition_state(
                    run_id,
                    WorkflowState.VALIDATING_PATCH,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    reason=(
                        f"Replacement patch #{repair_idx} failed validation ({primary_err.code}) "
                        "and consumed repair budget; re-evaluating remaining budget."
                    ),
                )
                continue

            val_rec = AgentExecutionRecord(
                agent_name="PatchValidator",
                stage=WorkflowState.VALIDATING_PATCH.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=val_started,
                completed_at=utc_now(),
                duration_ms=val_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    f"Repair patch #{repair_idx} passed validation "
                    f"({len(validation_result.checks_passed)} checks, files={validation_result.checked_files})."
                ),
            )
            self._log(
                run_id=run_id,
                agent_name="PatchValidator",
                stage=WorkflowState.VALIDATING_PATCH,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=val_dur_ms,
                input_summary=f"repair_attempt={repair_idx}, sha={proposal.patch_sha256[:12]}",
                output_summary=val_rec.output_summary,
            )
            run = self.transition_state(
                run_id,
                WorkflowState.VALIDATING_PATCH,
                WorkflowState.APPLYING_PATCH,
                updater=lambda cur: cur.model_copy(
                    update={"agent_records": [*cur.agent_records, val_rec]}
                ),
                reason=f"Replacement patch #{repair_idx} passed validation; applying inside clean isolated workspace.",
            )

            # 10. Apply validated replacement patch inside the clean repair workspace
            app_started = utc_now()
            app_t0 = time.perf_counter()
            app_result = self.patch_applicator.apply(
                workspace_repo_path=repair_ws_path,
                proposal=proposal,
                validation_result=validation_result,
                source_path=repair_ws_ctx.source_path,
                base_ref=repair_ws_ctx.effective_base_ref,
                expected_source_snapshot_sha256=run.original_source_snapshot_sha256,
                expected_git_diff_sha256=run.original_git_diff_sha256,
            )
            app_dur_ms = int((time.perf_counter() - app_t0) * 1000)
            repair_durations["patch_application_ms"] = app_dur_ms

            if (
                not app_result.applied
                or app_result.resulting_source_snapshot_sha256 is None
                or app_result.resulting_git_diff_sha256 is None
            ):
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                err = app_result.error or AgentError(
                    code="PATCH_APPLICATION_FAILED",
                    message=f"Replacement patch #{repair_idx} failed to apply cleanly.",
                )
                app_rec = AgentExecutionRecord(
                    agent_name="PatchApplicator",
                    stage=WorkflowState.APPLYING_PATCH.value,
                    status=AgentExecutionStatus.FAILED,
                    started_at=app_started,
                    completed_at=utc_now(),
                    duration_ms=app_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=err.message,
                    error=err,
                )
                app_attempt = _make_failed_attempt_record(
                    outcome="application_failed",
                    gen_res=patch_result,
                    val_res=validation_result,
                    app_res=app_result,
                    errors=[err],
                )
                history = history.model_copy(
                    update={"attempts": [*history.attempts, app_attempt]}
                )
                run = self.store.update_run_atomic(
                    run_id,
                    expected_state=WorkflowState.APPLYING_PATCH,
                    updater=lambda cur: cur.model_copy(
                        update={
                            "repair_history": history,
                            "agent_records": [*cur.agent_records, app_rec],
                        }
                    ),
                )
                if new_consumed >= policy.effective_max_repair_attempts:
                    return _finalize_to_report(
                        run,
                        WorkflowState.APPLYING_PATCH,
                        history,
                        RepairTerminationReason.BUDGET_EXHAUSTED,
                        f"Repair budget exhausted after patch application failure: {err.message}",
                        eligibility,
                    )
                run = self.transition_state(
                    run_id,
                    WorkflowState.APPLYING_PATCH,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    reason=(
                        f"Replacement patch #{repair_idx} failed application ({err.code}); "
                        "re-evaluating remaining budget."
                    ),
                )
                continue

            app_rec = AgentExecutionRecord(
                agent_name="PatchApplicator",
                stage=WorkflowState.APPLYING_PATCH.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=app_started,
                completed_at=utc_now(),
                duration_ms=app_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    f"Applied replacement patch #{repair_idx} inside clean repair workspace "
                    f"(files={app_result.applied_files}, "
                    f"new_snap={app_result.resulting_source_snapshot_sha256[:12]})."
                ),
                assumptions=[app_result.isolation_note],
            )
            self._log(
                run_id=run_id,
                agent_name="PatchApplicator",
                stage=WorkflowState.APPLYING_PATCH,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=app_dur_ms,
                input_summary=f"repair_attempt={repair_idx}, sha={proposal.patch_sha256[:12]}",
                output_summary=app_rec.output_summary,
            )
            run = self.transition_state(
                run_id,
                WorkflowState.APPLYING_PATCH,
                WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                updater=lambda cur: cur.model_copy(
                    update={"agent_records": [*cur.agent_records, app_rec]}
                ),
                reason=f"Replacement patch #{repair_idx} applied; recomputing M01 post-patch impact and scope checks.",
            )

            # 11. Post-patch M01 impact analysis & approved-test assumption preservation check
            m01_started = utc_now()
            m01_t0 = time.perf_counter()
            new_cb = self.codebase_adapter.analyze(
                repair_ws_path,
                source_path=repair_ws_ctx.source_path,
                base_ref=repair_ws_ctx.effective_base_ref,
            )
            m01_dur_ms = int((time.perf_counter() - m01_t0) * 1000)
            repair_durations["impact_analysis_ms"] = m01_dur_ms

            if new_cb.status != "completed" or not new_cb.has_relevant_changes:
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                err = new_cb.error or AgentError(
                    code="POST_PATCH_IMPACT_FAILED",
                    message=f"Post-patch M01 impact analysis failed on repair attempt #{repair_idx}.",
                )
                imp_rec = AgentExecutionRecord(
                    agent_name="CodebaseAdapter",
                    stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
                    status=AgentExecutionStatus.FAILED,
                    started_at=m01_started,
                    completed_at=utc_now(),
                    duration_ms=m01_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=err.message,
                    error=err,
                )
                imp_attempt = _make_failed_attempt_record(
                    outcome="impact_analysis_failed",
                    gen_res=patch_result,
                    val_res=validation_result,
                    app_res=app_result,
                    errors=[err],
                )
                history = history.model_copy(
                    update={"attempts": [*history.attempts, imp_attempt]}
                )
                run = self.store.update_run_atomic(
                    run_id,
                    expected_state=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    updater=lambda cur: cur.model_copy(
                        update={
                            "repair_history": history,
                            "agent_records": [*cur.agent_records, imp_rec],
                        }
                    ),
                )
                if new_consumed >= policy.effective_max_repair_attempts:
                    return _finalize_to_report(
                        run,
                        WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                        history,
                        RepairTerminationReason.BUDGET_EXHAUSTED,
                        err.message,
                        eligibility,
                    )
                run = self.transition_state(
                    run_id,
                    WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    reason="Post-patch impact analysis failed on repair attempt; re-evaluating budget.",
                )
                continue

            assumptions_ok, assumption_reasons = check_approved_test_assumptions_preserved(
                workspace_repo_path=repair_ws_path,
                source_path=repair_ws_ctx.source_path,
                initial_codebase_analysis=initial_cb,
                new_codebase_analysis=new_cb,
                approved_test_codes=approved_test_codes,
            )
            if not assumptions_ok:
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                scope_err = AgentError(
                    code="TEST_ASSUMPTIONS_INVALIDATED",
                    message=(
                        "Replacement patch changed intended scope or invalidated approved test "
                        "assumptions; stopping automatic repair for fresh human test review: "
                        + "; ".join(assumption_reasons)
                    ),
                    details={"reasons": assumption_reasons},
                )
                scope_rec = AgentExecutionRecord(
                    agent_name="CodebaseAdapter",
                    stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
                    status=AgentExecutionStatus.FAILED,
                    started_at=m01_started,
                    completed_at=utc_now(),
                    duration_ms=m01_dur_ms,
                    execution_mode=run.execution_mode.value,
                    output_summary=scope_err.message,
                    error=scope_err,
                )
                scope_attempt = _make_failed_attempt_record(
                    outcome="scope_invalidated",
                    gen_res=patch_result,
                    val_res=validation_result,
                    app_res=app_result,
                    errors=[scope_err],
                    notes=assumption_reasons,
                )
                history = history.model_copy(
                    update={"attempts": [*history.attempts, scope_attempt]}
                )
                run = self.store.update_run_atomic(
                    run_id,
                    expected_state=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    updater=lambda cur: cur.model_copy(
                        update={
                            "repair_history": history,
                            "agent_records": [*cur.agent_records, scope_rec],
                        }
                    ),
                )
                return _finalize_to_report(
                    run,
                    WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    history,
                    RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED,
                    scope_err.message,
                    eligibility,
                )

            m01_rec = AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=m01_started,
                completed_at=utc_now(),
                duration_ms=m01_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    f"Repair #{repair_idx} post-patch M01 detected "
                    f"{len(new_cb.changed_files_repo_relative)} changed file(s) and "
                    f"{len(new_cb.impacted_modules_repo_relative)} impacted module(s); "
                    "approved test scope & assumptions preserved."
                ),
            )
            self._log(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=m01_dur_ms,
                output_summary=m01_rec.output_summary,
            )

            # 12. Mirror exact Gate-1-approved tests into clean repair workspace (SHA-256 verified)
            try:
                mirrored_repair_hashes = mirror_approved_tests_to_workspace(
                    run.workspace_path,
                    repair_ws_path,
                    specs,
                    selected_ids,
                )
            except BaselineTestMirrorError as exc:
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                err = AgentError(code=exc.code, message=exc.message, details=exc.details)
                return (
                    self._fail_run(
                        run_id, WorkflowState.ANALYZING_POST_PATCH_IMPACT, err
                    ),
                    last_ver_result,
                )

            # 13. Validate whether prior baseline evidence can be safely reused, else rerun baseline
            baseline_reused = self._is_baseline_reusable(
                policy=policy,
                baseline_result=active_baseline_result,
                baseline_snapshot_sha=active_baseline_snapshot_sha,
                original_snapshot_sha=run.original_source_snapshot_sha256,
                resolved_base_commit=resolved_base_commit,
                expected_approved_hashes=mirrored_repair_hashes,
                repair_workspace_path=repair_ws_path,
                source_path=repair_ws_ctx.source_path,
            )

            if baseline_reused:
                self._log(
                    run_id=run_id,
                    agent_name="BaselineComparisonEngine",
                    stage=WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    status=AgentExecutionStatus.SUCCEEDED,
                    output_summary=(
                        "Validated baseline reuse: baseline commit, original source snapshot SHA-256, "
                        "approved test SHA-256 hashes, and existing regression suite hashes are identical."
                    ),
                )
                run = self.transition_state(
                    run_id,
                    WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                    updater=lambda cur: cur.model_copy(
                        update={"agent_records": [*cur.agent_records, m01_rec]}
                    ),
                    reason=(
                        f"Validated baseline reused for repair attempt #{repair_idx}; "
                        "running unchanged approved tests against repaired workspace."
                    ),
                )
            else:
                run = self.transition_state(
                    run_id,
                    WorkflowState.ANALYZING_POST_PATCH_IMPACT,
                    WorkflowState.PREPARING_BASELINE,
                    updater=lambda cur: cur.model_copy(
                        update={"agent_records": [*cur.agent_records, m01_rec]}
                    ),
                    reason=(
                        f"Baseline evidence not reusable for repair attempt #{repair_idx}; "
                        "re-executing baseline verification under identical approved conditions."
                    ),
                )
                rerun_base_path: str | None = None
                try:
                    rerun_base_ctx = self.workspace_manager.prepare_baseline_workspace(
                        f"{run_id}_r{next_attempt}",
                        run.repository.repository_path,
                        resolved_base_commit,
                        source_path=run.repository.source_path,
                        base_ref=run.repository.base_ref,
                    )
                    rerun_base_path = rerun_base_ctx.workspace_repo_path
                    active_baseline_snapshot_sha = compute_source_snapshot_sha256(
                        rerun_base_path, rerun_base_ctx.source_path
                    )
                    mirror_approved_tests_to_baseline(
                        repair_ws_path,
                        rerun_base_path,
                        specs,
                        selected_ids,
                    )
                    run = self.transition_state(
                        run_id,
                        WorkflowState.PREPARING_BASELINE,
                        WorkflowState.RUNNING_BASELINE_VERIFICATION,
                        reason=f"Re-running baseline verification for repair attempt #{repair_idx}.",
                    )
                    b_t0 = time.perf_counter()
                    rerun_base_res, _rb_agg, rb_int_err = execute_snapshot_verification(
                        self.verification_adapter,
                        self.config,
                        snapshot_role="baseline",
                        workspace_path=rerun_base_path,
                        source_path=run.repository.source_path,
                        selected_test_ids=selected_ids,
                        approved_specs=specs,
                        test_purposes=test_purposes,
                        criterion_ids_by_test=criterion_ids_by_test,
                        is_trusted_workspace=is_trusted_workspace,
                        source_snapshot_sha256=active_baseline_snapshot_sha,
                        baseline_ref=baseline_ref_label,
                        baseline_commit=resolved_base_commit,
                    )
                    repair_durations["baseline_verification_ms"] = int(
                        (time.perf_counter() - b_t0) * 1000
                    )
                    assert isinstance(rerun_base_res, BaselineVerificationResult)
                    active_baseline_result = rerun_base_res
                    if rb_int_err is not None:
                        self.workspace_manager.cleanup_workspace(repair_ws_path)
                        return (
                            self._fail_run(
                                run_id,
                                WorkflowState.RUNNING_BASELINE_VERIFICATION,
                                rb_int_err,
                            ),
                            last_ver_result,
                        )
                    run = self.transition_state(
                        run_id,
                        WorkflowState.RUNNING_BASELINE_VERIFICATION,
                        WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                        updater=lambda cur: cur.model_copy(
                            update={"baseline_verification": active_baseline_result}
                        ),
                        reason="Baseline re-verification completed; executing modified verification on repaired workspace.",
                    )
                finally:
                    if self._cleanup_baseline_workspace and rerun_base_path:
                        try:
                            self.workspace_manager.cleanup_workspace(rerun_base_path)
                        except WorkspaceError:
                            pass

            # 14. Execute unchanged approved verification suite on repaired workspace
            mod_started = utc_now()
            mod_t0 = time.perf_counter()
            new_mod_res, new_ver_res, new_mod_int_err = execute_snapshot_verification(
                self.verification_adapter,
                self.config,
                snapshot_role="modified",
                workspace_path=repair_ws_path,
                source_path=repair_ws_ctx.source_path,
                selected_test_ids=selected_ids,
                approved_specs=specs,
                test_purposes=test_purposes,
                criterion_ids_by_test=criterion_ids_by_test,
                is_trusted_workspace=is_trusted_workspace,
                source_snapshot_sha256=app_result.resulting_source_snapshot_sha256,
                git_diff_sha256=app_result.resulting_git_diff_sha256,
                baseline_ref=baseline_ref_label,
                baseline_commit=resolved_base_commit,
            )
            mod_dur_ms = int((time.perf_counter() - mod_t0) * 1000)
            repair_durations["modified_verification_ms"] = mod_dur_ms
            assert isinstance(new_mod_res, ModifiedVerificationResult)

            if new_mod_int_err is not None:
                self.workspace_manager.cleanup_workspace(repair_ws_path)
                return (
                    self._fail_run(
                        run_id,
                        WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                        new_mod_int_err,
                    ),
                    last_ver_result,
                )

            mod_exec_status = (
                AgentExecutionStatus.TIMEOUT
                if new_ver_res.verification_outcome == VerificationOutcome.TIMEOUT
                else (
                    AgentExecutionStatus.SUCCEEDED
                    if new_ver_res.execution_status == "completed"
                    else AgentExecutionStatus.FAILED
                )
            )
            mod_rec = AgentExecutionRecord(
                agent_name="VerificationAdapter",
                stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION.value,
                status=mod_exec_status,
                started_at=mod_started,
                completed_at=utc_now(),
                duration_ms=mod_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    f"Repair #{repair_idx} modified verification outcome="
                    f"{new_ver_res.verification_outcome.value}, passed_flag={new_ver_res.verification_passed}, "
                    f"total={new_ver_res.total_tests}, passed={new_ver_res.passed}, failed={new_ver_res.failed}."
                ),
                error=new_ver_res.error,
            )
            self._log(
                run_id=run_id,
                agent_name="VerificationAdapter",
                stage=WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                status=mod_exec_status,
                duration_ms=mod_dur_ms,
                input_summary=f"repair_attempt={repair_idx}, scenarios={selected_ids}",
                output_summary=mod_rec.output_summary,
                error=new_ver_res.error,
            )

            run = self.transition_state(
                run_id,
                WorkflowState.RUNNING_MODIFIED_VERIFICATION,
                WorkflowState.COMPARING_VERIFICATION_RESULTS,
                updater=lambda cur: cur.model_copy(
                    update={"agent_records": [*cur.agent_records, mod_rec]}
                ),
                reason=f"Repair #{repair_idx} modified verification completed; comparing against baseline.",
            )

            # 15. Compare baseline vs repaired workspace evidence
            cmp_started = utc_now()
            cmp_t0 = time.perf_counter()
            new_comparison = BaselineComparisonEngine.compare(
                baseline=active_baseline_result,
                modified=new_mod_res,
                approved_test_hashes=run.test_review.approved_test_hashes,
                baseline_ref=baseline_ref_label,
                baseline_commit=resolved_base_commit,
                baseline_source_snapshot_sha256=active_baseline_snapshot_sha,
                modified_source_snapshot_sha256=app_result.resulting_source_snapshot_sha256,
            )
            cmp_dur_ms = int((time.perf_counter() - cmp_t0) * 1000)
            repair_durations["comparison_ms"] = cmp_dur_ms
            new_ver_res = new_ver_res.model_copy(
                update={"baseline_comparison": new_comparison}
            )

            cmp_rec = AgentExecutionRecord(
                agent_name="BaselineComparisonEngine",
                stage=WorkflowState.COMPARING_VERIFICATION_RESULTS.value,
                status=AgentExecutionStatus.SUCCEEDED,
                started_at=cmp_started,
                completed_at=utc_now(),
                duration_ms=cmp_dur_ms,
                execution_mode=run.execution_mode.value,
                output_summary=(
                    f"Repair #{repair_idx} comparison status={new_comparison.status.value}: "
                    f"preserved={new_comparison.summary.preserved_behavior_count}, "
                    f"newly_introduced_failures={new_comparison.summary.newly_introduced_failure_count}, "
                    f"newly_satisfied_acceptance={new_comparison.summary.newly_satisfied_acceptance_count}."
                ),
            )
            self._log(
                run_id=run_id,
                agent_name="BaselineComparisonEngine",
                stage=WorkflowState.COMPARING_VERIFICATION_RESULTS,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=cmp_dur_ms,
                output_summary=cmp_rec.output_summary,
            )

            # Build complete attempt record preserving all evidence for this repair attempt
            rep_failures = list(new_mod_res.failures)
            if new_mod_res.existing_regression_suite is not None:
                rep_failures.extend(new_mod_res.existing_regression_suite.failures)
            rep_errors: list[AgentError] = []
            if new_mod_res.error is not None:
                rep_errors.append(new_mod_res.error)
            rep_passed_clean = bool(
                new_mod_res.verification_passed
                and new_comparison.status == VerificationComparisonStatus.COMPLETED
                and not new_comparison.summary.has_confirmed_regression
                and (
                    new_mod_res.existing_regression_suite is None
                    or new_mod_res.existing_regression_suite.verification_outcome
                    == VerificationOutcome.PASSED
                )
            )
            new_failure_sig = compute_failure_signature(
                modified=new_mod_res,
                comparison=new_comparison,
            )
            no_progress_detected = bool(
                not rep_passed_clean
                and policy.stop_on_no_progress
                and new_failure_sig is not None
                and new_failure_sig in history.seen_failure_signatures
            )

            rep_attempt = PatchAttemptRecord(
                attempt_number=next_attempt,
                attempt_type="repair",
                repair_attempt_index=repair_idx,
                original_requirement=run.requirements,
                acceptance_criterion_ids=sorted(valid_cids),
                previous_proposal_id=prev_proposal_id,
                previous_patch_sha256=prev_patch_sha,
                base_snapshot_sha256=run.original_source_snapshot_sha256,
                resulting_source_snapshot_sha256=app_result.resulting_source_snapshot_sha256,
                resulting_git_diff_sha256=app_result.resulting_git_diff_sha256,
                patch_generation=patch_result,
                patch_proposal_id=proposal.proposal_id,
                patch_sha256=proposal.patch_sha256,
                patch_validation=validation_result,
                patch_application=app_result,
                approved_test_ids=list(selected_ids),
                approved_test_hashes=dict(mirrored_repair_hashes),
                baseline_reused=baseline_reused,
                baseline_verification=active_baseline_result,
                modified_verification=new_mod_res,
                failure_diagnostics=rep_failures,
                failure_signature=new_failure_sig,
                baseline_comparison=new_comparison,
                repair_eligibility=None,
                provider_invoked=True,
                consumed_repair_budget=True,
                is_deterministic_fixture=patch_result.is_deterministic_fixture,
                is_mock_output=patch_result.is_mock_output,
                model_label=patch_result.model_metadata.model_label,
                durations_ms=repair_durations,
                started_at=repair_attempt_started,
                completed_at=utc_now(),
                outcome="passed" if rep_passed_clean else "failed_verification",
                errors=rep_errors,
                notes=(
                    ["Unchanged failure signature detected; no measurable progress over prior attempt."]
                    if no_progress_detected
                    else []
                ),
            )
            new_seen_sigs = list(history.seen_failure_signatures)
            if new_failure_sig and new_failure_sig not in new_seen_sigs:
                new_seen_sigs.append(new_failure_sig)

            history = history.model_copy(
                update={
                    "attempts": [*history.attempts, rep_attempt],
                    "seen_failure_signatures": new_seen_sigs,
                    "final_candidate_attempt_number": next_attempt,
                    "final_candidate_proposal_id": proposal.proposal_id,
                    "final_candidate_patch_sha256": proposal.patch_sha256,
                }
            )

            # Promote clean repaired workspace as active workspace & clean up superseded copy
            previous_ws_path = run.workspace_path
            run = self.transition_state(
                run_id,
                WorkflowState.COMPARING_VERIFICATION_RESULTS,
                WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                updater=lambda cur: cur.model_copy(
                    update={
                        "workspace_id": repair_ws_ctx.workspace_id,
                        "workspace_path": repair_ws_path,
                        "source_snapshot_sha256": app_result.resulting_source_snapshot_sha256,
                        "git_diff_sha256": app_result.resulting_git_diff_sha256,
                        "patch_generation": patch_result,
                        "patch_proposal_sha256": proposal.patch_sha256,
                        "patch_attempt_number": next_attempt,
                        "patch_validation": validation_result,
                        "patch_application": app_result,
                        "codebase_analysis": new_cb,
                        "modified_verification": new_mod_res,
                        "baseline_comparison": new_comparison,
                        "verification": new_ver_res,
                        "repair_history": history,
                        "agent_records": [*cur.agent_records, cmp_rec],
                    }
                ),
                reason=(
                    f"Repair attempt #{repair_idx} verified (outcome={new_ver_res.verification_outcome.value}); "
                    "evaluating repair termination and eligibility."
                ),
            )
            if previous_ws_path and previous_ws_path != repair_ws_path:
                try:
                    self.workspace_manager.cleanup_workspace(previous_ws_path)
                except WorkspaceError:
                    pass

            last_ver_result = new_ver_res
            last_modified = new_mod_res
            last_comparison = new_comparison
            last_provider_error = None

            if no_progress_detected:
                no_prog_elig = RepairEligibilityResult(
                    eligible=False,
                    category=RepairEligibilityCategory.UNATTRIBUTABLE_FAILURE,
                    decision_reason=(
                        f"Repair attempt #{repair_idx} produced an identical failure signature "
                        f"({new_failure_sig[:12]}) to an earlier attempt with zero measurable progress; "
                        "stopping automated repair for human review."
                    ),
                    blocking_reasons=[
                        "Unchanged failure signature across distinct repair attempts (no meaningful progress)."
                    ],
                    failure_signature=new_failure_sig,
                    next_attempt_number=None,
                    remaining_repair_budget=history.remaining_repair_budget,
                )
                upd_attempts = list(history.attempts)
                upd_attempts[-1] = upd_attempts[-1].model_copy(
                    update={"repair_eligibility": no_prog_elig}
                )
                history = history.model_copy(
                    update={"attempts": upd_attempts, "last_eligibility": no_prog_elig}
                )
                return _finalize_to_report(
                    run,
                    WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
                    history,
                    RepairTerminationReason.NO_PROGRESS_DETECTED,
                    no_prog_elig.decision_reason,
                    no_prog_elig,
                )

        # Fallback safety net (structurally unreachable unless loop guard hit)
        return _finalize_to_report(
            run,
            WorkflowState.EVALUATING_REPAIR_ELIGIBILITY,
            history,
            RepairTerminationReason.BUDGET_EXHAUSTED,
            "Bounded repair loop reached its configured maximum iteration bound.",
            history.last_eligibility,
        )

    def _build_criterion_traceability(
        self,
        run: WorkflowRunRecord,
        verification: VerificationResult,
        *,
        changed_files: list[str],
        impacted_modules: list[str],
        proposed_ids: list[str],
        approved_ids: list[str],
        executed_ids: list[str],
        unapproved_ids: list[str],
    ) -> tuple[list[RequirementTraceability], dict[str, Any], list[str]]:
        """Build truthful per-criterion traceability without fabricating scenario mappings."""
        outcome = verification.verification_outcome
        req_analysis = run.requirement_analysis
        gen = run.test_generation

        if req_analysis is None or not req_analysis.acceptance_criteria:
            unverified_reqs: list[str] = [
                "Full end-to-end fulfillment of supplied natural-language requirements is not automatically proven because structured acceptance criteria decomposition and baseline regression comparison are unavailable.",
            ]
            if unapproved_ids:
                unverified_reqs.append(
                    f"Proposed scenario(s) not selected during human test review were not executed: {', '.join(unapproved_ids)}."
                )
            if not verification.verification_passed:
                unverified_reqs.append(
                    f"Approved scenario(s) did not achieve passing verification (outcome='{outcome.value}')."
                )
            legacy_trace = {
                "requirement_traceability_status": "incomplete",
                "structured_acceptance_criteria_status": "unavailable",
                "criterion_mapping_status": "unavailable",
                "requirement_verification_proven": False,
                "complete_requirement_assurance": False,
                "baseline_regression_comparison_status": verification.baseline_comparison.status.value,
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
            return [], legacy_trace, unverified_reqs

        # Build explicit scenario mapping strictly from scenario.criterion_ids (never from test names)
        scenarios_by_criterion: dict[str, list[str]] = {}
        if gen is not None:
            for sc in gen.scenarios:
                for cid in sc.criterion_ids:
                    bucket = scenarios_by_criterion.setdefault(cid, [])
                    if sc.scenario_id not in bucket:
                        bucket.append(sc.scenario_id)

        approved_set = set(approved_ids)
        executed_set = set(executed_ids)
        exported_files = gen.exported_files if gen is not None else {}

        # Index unresolved ambiguities (both blocking and non-blocking) per criterion
        unresolved_by_criterion: dict[str, list[str]] = {}
        blocking_by_criterion: dict[str, list[str]] = {}
        ambiguity_by_id = {amb.ambiguity_id: amb for amb in req_analysis.ambiguities}

        for ac in req_analysis.acceptance_criteria:
            for amb_id in ac.ambiguity_ids:
                amb = ambiguity_by_id.get(amb_id)
                if amb is not None and not amb.resolved:
                    unresolved_by_criterion.setdefault(ac.criterion_id, [])
                    if amb_id not in unresolved_by_criterion[ac.criterion_id]:
                        unresolved_by_criterion[ac.criterion_id].append(amb_id)
                    if amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING:
                        blocking_by_criterion.setdefault(ac.criterion_id, [])
                        if amb_id not in blocking_by_criterion[ac.criterion_id]:
                            blocking_by_criterion[ac.criterion_id].append(amb_id)

        for amb in req_analysis.ambiguities:
            if amb.resolved:
                continue
            for cid in amb.related_criterion_ids:
                unresolved_by_criterion.setdefault(cid, [])
                if amb.ambiguity_id not in unresolved_by_criterion[cid]:
                    unresolved_by_criterion[cid].append(amb.ambiguity_id)
                if amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING:
                    blocking_by_criterion.setdefault(cid, [])
                    if amb.ambiguity_id not in blocking_by_criterion[cid]:
                        blocking_by_criterion[cid].append(amb.ambiguity_id)

        # Build file failure lookup & per-scenario baseline comparison lookup
        failure_test_names = [f.test_name for f in verification.failures]
        has_unattributable_failure = any(
            name in {"pytest_execution", "pytest_runner", "pytest_collection", "unknown_test"}
            for name in failure_test_names
        )
        comparison = verification.baseline_comparison
        comparisons_by_scenario = {
            tc.scenario_id: tc
            for tc in comparison.test_comparisons
            if tc.scenario_id is not None
        }

        entries: list[RequirementTraceability] = []
        verified_ids: list[str] = []
        failed_ids: list[str] = []
        unverified_ids: list[str] = []
        needs_clarification_ids: list[str] = []
        mandatory_ids: list[str] = []
        incomplete_mandatory_ids: list[str] = []
        explicit_mapping_count = 0

        for ac in req_analysis.acceptance_criteria:
            if ac.mandatory:
                mandatory_ids.append(ac.criterion_id)

            gen_scn_ids = list(scenarios_by_criterion.get(ac.criterion_id, []))
            app_scn_ids = [sid for sid in gen_scn_ids if sid in approved_set]
            exe_scn_ids = [sid for sid in app_scn_ids if sid in executed_set]
            mapping_status = "explicit" if gen_scn_ids else "unavailable"
            if mapping_status == "explicit":
                explicit_mapping_count += 1

            unresolved_ambs = list(unresolved_by_criterion.get(ac.criterion_id, []))
            blocking_ambs = list(blocking_by_criterion.get(ac.criterion_id, []))

            affected_files = list(changed_files)
            file_map_status = "repository_scope_only" if affected_files else "unavailable"

            matched_comparisons = [
                comparisons_by_scenario[sid]
                for sid in exe_scn_ids
                if sid in comparisons_by_scenario
            ]
            has_regressed_scenario = any(
                tc.classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE
                for tc in matched_comparisons
            )
            has_failed_scenario_ev = any(
                tc.modified_outcome == IndividualTestOutcome.FAILED
                for tc in matched_comparisons
            )

            if blocking_ambs or unresolved_ambs:
                v_status = TraceabilityEvidenceStatus.NEEDS_CLARIFICATION
                if blocking_ambs:
                    ev_summary = (
                        f"Blocked by mandatory unresolved ambiguity ({', '.join(blocking_ambs)}); "
                        "cannot be treated as a verified requirement."
                    )
                else:
                    ev_summary = (
                        f"Linked unresolved ambiguity ({', '.join(unresolved_ambs)}) requires "
                        "human clarification before marking criterion verified."
                    )
                note = "Unresolved ambiguity prevents verified status regardless of test pass/fail."
            elif mapping_status == "unavailable":
                v_status = TraceabilityEvidenceStatus.UNVERIFIED
                ev_summary = (
                    "No explicit criterion-to-scenario mapping was supplied by M03; "
                    "passing tests are not inferred to verify this criterion."
                )
                note = (
                    "Requires M03 contract extension emitting structured criterion_ids per scenario."
                )
            elif not exe_scn_ids:
                v_status = TraceabilityEvidenceStatus.UNVERIFIED
                ev_summary = (
                    f"Mapped scenario(s) ({', '.join(gen_scn_ids)}) were not approved and executed."
                )
                note = "Approve and execute mapped scenarios before treating criterion as verified."
            elif has_regressed_scenario:
                v_status = TraceabilityEvidenceStatus.FAILED
                regressed_ids = [
                    tc.scenario_id
                    for tc in matched_comparisons
                    if tc.classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE
                    and tc.scenario_id
                ]
                ev_summary = (
                    f"Confirmed newly introduced regression on mapped scenario(s) ({', '.join(regressed_ids)}): "
                    "passed on baseline and failed on modified snapshot."
                )
                note = "Newly introduced regression blocks ordinary approval."
            elif verification.verification_passed and outcome == VerificationOutcome.PASSED:
                v_status = TraceabilityEvidenceStatus.VERIFIED
                cls_summary = (
                    ", ".join(
                        f"{tc.scenario_id}:{tc.classification.value}"
                        for tc in matched_comparisons
                        if tc.scenario_id
                    )
                    if matched_comparisons
                    else "modified_only"
                )
                ev_summary = (
                    f"Explicitly mapped scenario(s) ({', '.join(exe_scn_ids)}) approved and "
                    f"executed with passing modified pytest outcome (passed={verification.passed}, failed=0; "
                    f"baseline comparison: {cls_summary})."
                )
                note = (
                    f"Baseline-versus-modified comparison status is '{comparison.status.value}'."
                )
            elif outcome == VerificationOutcome.FAILED_TESTS:
                matched_failure = has_failed_scenario_ev
                if not matched_failure:
                    for sid in exe_scn_ids:
                        exp_path = exported_files.get(sid)
                        if exp_path and any(
                            exp_path in tname or Path(exp_path).name in tname
                            for tname in failure_test_names
                        ):
                            matched_failure = True
                            break
                if matched_failure or has_unattributable_failure or not failure_test_names:
                    v_status = TraceabilityEvidenceStatus.FAILED
                    ev_summary = (
                        f"Mapped scenario(s) ({', '.join(exe_scn_ids)}) failed during M04 "
                        f"verification (outcome='{outcome.value}')."
                    )
                    note = "Inspect M04 failure diagnostics and baseline comparison evidence."
                else:
                    v_status = TraceabilityEvidenceStatus.UNVERIFIED
                    ev_summary = (
                        f"Overall verification failed on another scenario; criterion remains unverified "
                        "while the modified verification suite contains failing tests."
                    )
                    note = "Cannot mark verified while verification run exited with failures."
            else:
                v_status = TraceabilityEvidenceStatus.UNVERIFIED
                ev_summary = (
                    f"Verification did not complete cleanly (outcome='{outcome.value}') for "
                    f"mapped scenario(s) ({', '.join(exe_scn_ids)})."
                )
                note = "Criterion remains unverified until clean passing execution completes."

            entry = RequirementTraceability(
                criterion_id=ac.criterion_id,
                requirement_reference=ac.requirement_reference,
                description=ac.description,
                origin=ac.origin,
                mandatory=ac.mandatory,
                affected_source_files=affected_files,
                file_mapping_status=file_map_status,
                generated_scenario_ids=gen_scn_ids,
                approved_test_ids=app_scn_ids,
                executed_test_ids=exe_scn_ids,
                mapping_status=mapping_status,
                execution_evidence_summary=ev_summary,
                verification_status=v_status,
                unresolved_ambiguity_ids=unresolved_ambs,
                assumption_ids=list(ac.assumption_ids),
                notes=note,
            )
            entries.append(entry)

            if v_status == TraceabilityEvidenceStatus.VERIFIED:
                verified_ids.append(ac.criterion_id)
            elif v_status == TraceabilityEvidenceStatus.FAILED:
                failed_ids.append(ac.criterion_id)
            elif v_status == TraceabilityEvidenceStatus.NEEDS_CLARIFICATION:
                needs_clarification_ids.append(ac.criterion_id)
            else:
                unverified_ids.append(ac.criterion_id)

            if ac.mandatory and v_status != TraceabilityEvidenceStatus.VERIFIED:
                incomplete_mandatory_ids.append(ac.criterion_id)

        if explicit_mapping_count == 0:
            criterion_mapping_status = "unavailable"
        elif explicit_mapping_count == len(entries):
            criterion_mapping_status = "explicit"
        else:
            criterion_mapping_status = "partial"

        mandatory_coverage_complete = (
            bool(mandatory_ids)
            and not incomplete_mandatory_ids
            and not req_analysis.has_mandatory_unresolved_ambiguities
        )
        req_traceability_status = (
            "complete"
            if mandatory_coverage_complete and criterion_mapping_status == "explicit"
            else "incomplete"
        )
        patch_gen = run.patch_generation
        any_fixture_attempt = bool(
            run.repair_history is not None
            and any(
                att.is_deterministic_fixture or att.is_mock_output
                for att in run.repair_history.attempts
            )
        )
        req_verification_proven = bool(
            mandatory_coverage_complete
            and verification.verification_passed
            and comparison.status == VerificationComparisonStatus.COMPLETED
            and comparison.summary.regression_safety_established
            and not comparison.summary.has_confirmed_regression
            and not req_analysis.is_deterministic_fixture
            and (gen is None or not gen.is_mock_output)
            and (
                patch_gen is None
                or (
                    not patch_gen.is_deterministic_fixture
                    and not patch_gen.is_mock_output
                )
            )
            and not any_fixture_attempt
            and run.repair_termination_reason
            not in {
                RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED,
                RepairTerminationReason.TEST_MODIFICATION_REJECTED,
            }
        )

        unverified_reqs = []
        if req_analysis.has_mandatory_unresolved_ambiguities:
            blocking_list = [
                f"{amb.ambiguity_id}: {amb.description} ({amb.clarification_question})"
                for amb in req_analysis.ambiguities
                if (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
                and not amb.resolved
            ]
            unverified_reqs.append(
                "Mandatory unresolved requirement ambiguity blocks verification: "
                + "; ".join(blocking_list)
            )
        if criterion_mapping_status == "unavailable":
            unverified_reqs.append(
                "Structured criterion-to-test mapping is unavailable from M03; passing tests alone do not prove individual acceptance criteria are verified."
            )
        if incomplete_mandatory_ids:
            unverified_reqs.append(
                f"Mandatory acceptance criteria without verified evidence: {', '.join(incomplete_mandatory_ids)}."
            )
        if unapproved_ids:
            unverified_reqs.append(
                f"Proposed scenario(s) not selected during human test review were not executed: {', '.join(unapproved_ids)}."
            )
        if not verification.verification_passed:
            unverified_reqs.append(
                f"Approved scenario(s) did not achieve passing verification (outcome='{outcome.value}')."
            )
        if comparison.summary.has_confirmed_regression:
            unverified_reqs.append(
                "Confirmed newly introduced regression(s) detected against baseline: "
                + ", ".join(comparison.summary.newly_introduced_failures)
                + "."
            )
        elif comparison.status != VerificationComparisonStatus.COMPLETED:
            unverified_reqs.append(
                f"Baseline-versus-modified regression comparison status is '{comparison.status.value}'; "
                "complete regression safety against baseline is not established."
            )

        traceability_dict = {
            "workflow_mode": run.workflow_mode.value,
            "patch_proposal_sha256": run.patch_proposal_sha256,
            "patch_is_deterministic_fixture": (
                run.patch_generation.is_deterministic_fixture
                if run.patch_generation is not None
                else None
            ),
            "patch_applied": (
                run.patch_application.applied
                if run.patch_application is not None
                else None
            ),
            "repair_enabled": (
                run.repair_policy.enabled if run.repair_policy is not None else None
            ),
            "max_repair_attempts": (
                run.repair_policy.max_repair_attempts
                if run.repair_policy is not None
                else None
            ),
            "repair_attempts_consumed": run.repair_attempts_consumed,
            "remaining_repair_budget": (
                run.repair_history.remaining_repair_budget
                if run.repair_history is not None
                else (
                    max(
                        0,
                        run.repair_policy.effective_max_repair_attempts
                        - run.repair_attempts_consumed,
                    )
                    if run.repair_policy is not None
                    else None
                )
            ),
            "repair_termination_reason": (
                run.repair_termination_reason.value
                if run.repair_termination_reason is not None
                else None
            ),
            "requirement_traceability_status": req_traceability_status,
            "structured_acceptance_criteria_status": "available",
            "criterion_mapping_status": criterion_mapping_status,
            "requirement_analysis_status": req_analysis.status,
            "requirement_analysis_mode": req_analysis.analysis_mode,
            "requirement_analysis_is_deterministic_fixture": req_analysis.is_deterministic_fixture,
            "has_mandatory_unresolved_ambiguities": req_analysis.has_mandatory_unresolved_ambiguities,
            "mandatory_criteria_coverage_complete": mandatory_coverage_complete,
            "requirement_verification_proven": req_verification_proven,
            "complete_requirement_assurance": req_verification_proven,
            "baseline_regression_comparison_status": comparison.status.value,
            "regression_safety_established": comparison.summary.regression_safety_established,
            "has_confirmed_regression": comparison.summary.has_confirmed_regression,
            "newly_introduced_failure_count": comparison.summary.newly_introduced_failure_count,
            "total_criteria_count": len(entries),
            "mandatory_criteria_count": len(mandatory_ids),
            "verified_criterion_ids": verified_ids,
            "failed_criterion_ids": failed_ids,
            "unverified_criterion_ids": unverified_ids,
            "needs_clarification_criterion_ids": needs_clarification_ids,
            "incomplete_mandatory_criterion_ids": incomplete_mandatory_ids,
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
        return entries, traceability_dict, unverified_reqs

    def _compile_change_package(
        self,
        run: WorkflowRunRecord,
        verification: VerificationResult,
        logs: list[AgentLogEntry],
    ) -> ChangePackageReport:
        """Aggregate verified run evidence into an auditable ChangePackageReport."""
        req_analysis = run.requirement_analysis
        cb = run.codebase_analysis
        gen = run.test_generation
        review = run.test_review

        changed_files = cb.changed_files_repo_relative if cb else []
        impacted_modules = cb.impacted_modules_repo_relative if cb else []
        proposed_ids = [sc.scenario_id for sc in gen.scenarios] if gen else []
        approved_ids = list(review.selected_test_ids) if review and review.approved else []
        executed_ids = list(verification.executed_test_ids)
        unapproved_ids = [sc_id for sc_id in proposed_ids if sc_id not in set(approved_ids)]

        criterion_traceability, traceability, unverified_requirements = (
            self._build_criterion_traceability(
                run,
                verification,
                changed_files=changed_files,
                impacted_modules=impacted_modules,
                proposed_ids=proposed_ids,
                approved_ids=approved_ids,
                executed_ids=executed_ids,
                unapproved_ids=unapproved_ids,
            )
        )

        has_blocking_ambiguity = bool(
            req_analysis is not None and req_analysis.has_mandatory_unresolved_ambiguities
        )
        comparison = verification.baseline_comparison
        has_confirmed_regression = comparison.summary.blocks_ordinary_approval
        complete_assurance = bool(traceability.get("requirement_verification_proven", False))
        outcome = verification.verification_outcome

        if run.repair_termination_reason in {
            RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED,
            RepairTerminationReason.TEST_MODIFICATION_REJECTED,
        }:
            recommendation = "needs_review"
            recommendation_label = "additional_review_required"
            recommendation_summary = (
                f"Bounded automated patch repair stopped ({run.repair_termination_reason.value}): "
                f"{run.repair_history.termination_summary if run.repair_history else 'Fresh human review required.'} "
                "Ordinary approval is blocked; perform fresh human test and patch review."
            )
        elif has_confirmed_regression:
            recommendation = "reject"
            recommendation_label = "verification_failed"
            repair_clause = (
                f" Bounded repair ended with '{run.repair_termination_reason.value}' after "
                f"{run.repair_attempts_consumed} repair attempt(s)."
                if run.repair_termination_reason is not None
                else ""
            )
            recommendation_summary = (
                "Confirmed newly introduced regression(s) detected against baseline "
                f"({', '.join(comparison.summary.newly_introduced_failures)}).{repair_clause} "
                "Ordinary approval is blocked; inspect baseline-vs-modified comparison, attempt history, and failure diagnostics."
            )
        elif verification.verification_passed and outcome == VerificationOutcome.PASSED:
            recommendation = "needs_review"
            if has_blocking_ambiguity:
                recommendation_label = "additional_review_required"
                recommendation_summary = (
                    "Approved generated tests passed against the modified working copy, "
                    "but mandatory unresolved requirement ambiguities remain open. "
                    "Ordinary approval is blocked until ambiguities are clarified."
                )
            else:
                recommendation_label = "ready_for_human_review"
                mock_clause = (
                    f" Tests were produced by offline mock/stub model '{gen.model_label}' "
                    "(is_mock_output=True), not a live LLM."
                    if gen is not None and gen.is_mock_output
                    else ""
                )
                repair_clause = (
                    f" Bounded automated patch repair succeeded on attempt #{run.patch_attempt_number} "
                    f"({run.repair_attempts_consumed} repair attempt(s) consumed) against the unchanged "
                    "Gate-1-approved test suite; human final review remains mandatory."
                    if run.repair_termination_reason
                    == RepairTerminationReason.REPAIR_SUCCEEDED
                    else ""
                )
                comparison_clause = (
                    f" Baseline-vs-modified comparison completed with 0 confirmed regressions "
                    f"({comparison.summary.preserved_behavior_count} preserved, "
                    f"{comparison.summary.newly_satisfied_acceptance_count} newly satisfied acceptance)."
                    if comparison.status == VerificationComparisonStatus.COMPLETED
                    else (
                        f" Baseline-vs-modified comparison status is '{comparison.status.value}'; "
                        "complete regression safety is not established."
                    )
                )
                assurance_clause = (
                    " Ordinary human approval records review of this evidence package but does not "
                    "constitute complete requirement assurance."
                    if not complete_assurance
                    else ""
                )
                recommendation_summary = (
                    "All approved generated tests passed against the modified working copy. "
                    "Ready for human review; passing generated tests alone is not proof of "
                    f"full requirement verification, regression safety, or production readiness."
                    f"{repair_clause}{comparison_clause}{assurance_clause}{mock_clause}"
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
            "Git workspace isolation copies files into run-specific directories and is not an operating-system security sandbox.",
            "Reviewer identity labels are unauthenticated local MVP inputs and do not constitute cryptographic or SSO identity proof.",
        ]
        if comparison.status == VerificationComparisonStatus.COMPLETED:
            limitations.append(
                "Baseline-versus-modified comparison reflects only the executed approved scenarios "
                "and any discovered pre-existing regression tests; M04 report_parser omits individual "
                "passed pytest node IDs."
            )
        else:
            limitations.append(
                f"Baseline-versus-modified regression comparison is '{comparison.status.value}'; "
                "passing generated tests on the modified working copy alone does not guarantee complete regression safety."
            )
        for comp_lim in comparison.evidence_limitations:
            if comp_lim not in limitations:
                limitations.append(comp_lim)
        if run.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE:
            limitations.append(
                "Pre-change M01 codebase context was limited to static AST indexing; full reverse-dependency impact analysis ran post-patch."
            )
            limitations.append(
                "Independent adversarial review (A6) and code coverage measurement are not executed in this M02 MVP."
            )
            if run.repair_history is not None:
                if run.repair_history.termination_reason == RepairTerminationReason.PROVIDER_UNAVAILABLE:
                    limitations.append(
                        "Production A4 repair provider was unavailable during bounded repair; no automatic fallback to mock output occurred."
                    )
                elif any(
                    att.is_deterministic_fixture or att.is_mock_output
                    for att in run.repair_history.attempts
                ):
                    limitations.append(
                        "Patch generation and/or bounded repair attempts used an offline deterministic fixture "
                        "(is_deterministic_fixture=True), NOT actual AI self-repair results."
                    )
            if run.patch_generation is not None and (
                run.patch_generation.is_deterministic_fixture
                or run.patch_generation.is_mock_output
            ):
                limitations.append(
                    f"Applied patch was produced by an offline deterministic fixture "
                    f"('{run.patch_generation.model_metadata.model_label}', "
                    "is_deterministic_fixture=True), NOT actual AI-generated patch evidence."
                )
        else:
            limitations.append(
                "Automated patch generation (A4) was not executed in verify_existing_change mode; independent adversarial review (A6) and code coverage measurement are not executed in this M02 MVP."
            )
        if req_analysis is None:
            limitations.append(
                "Structured requirement decomposition (A1) was not executed for this run record."
            )
        else:
            if req_analysis.is_deterministic_fixture:
                limitations.append(
                    f"Structured requirement analysis (A1) was produced by an offline deterministic fixture ('{req_analysis.model_label}', is_deterministic_fixture=True), not a live LLM."
                )
            if traceability.get("criterion_mapping_status") == "unavailable":
                limitations.append(
                    "M03 RegressionTestGenerationAgent does not yet return structured criterion_ids per scenario; criterion-to-test mappings are unavailable and not inferred from test names."
                )
            if has_blocking_ambiguity:
                limitations.append(
                    "Mandatory unresolved requirement ambiguities remain open; affected acceptance criteria are marked needs_clarification."
                )
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
            workflow_mode=run.workflow_mode.value,
            execution_mode=run.execution_mode.value,
            model_label=gen.model_label if gen else None,
            is_mock_output=gen.is_mock_output if gen else None,
            patch_proposal_sha256=run.patch_proposal_sha256,
            patch_is_deterministic_fixture=(
                run.patch_generation.is_deterministic_fixture
                if run.patch_generation is not None
                else None
            ),
            patch_applied=(
                run.patch_application.applied
                if run.patch_application is not None
                else None
            ),
            repair_enabled=(
                run.repair_policy.enabled if run.repair_policy is not None else None
            ),
            max_repair_attempts=(
                run.repair_policy.max_repair_attempts
                if run.repair_policy is not None
                else None
            ),
            repair_attempts_consumed=run.repair_attempts_consumed,
            current_attempt_number=(
                run.repair_history.current_attempt_number
                if run.repair_history is not None
                and run.repair_history.current_attempt_number > 0
                else run.patch_attempt_number
            ),
            remaining_repair_budget=(
                run.repair_history.remaining_repair_budget
                if run.repair_history is not None
                else (
                    max(
                        0,
                        run.repair_policy.effective_max_repair_attempts
                        - run.repair_attempts_consumed,
                    )
                    if run.repair_policy is not None
                    else None
                )
            ),
            repair_eligibility=run.repair_eligibility,
            repair_termination_reason=run.repair_termination_reason,
            repair_history=run.repair_history,
            final_candidate_proposal_id=(
                run.repair_history.final_candidate_proposal_id
                if run.repair_history is not None
                else (
                    run.patch_generation.proposal.proposal_id
                    if run.patch_generation is not None
                    and run.patch_generation.proposal is not None
                    else None
                )
            ),
            final_candidate_patch_sha256=(
                run.repair_history.final_candidate_patch_sha256
                if run.repair_history is not None
                else run.patch_proposal_sha256
            ),
            final_verification_outcome=verification.verification_outcome,
            original_source_snapshot_sha256=run.original_source_snapshot_sha256,
            baseline_source_snapshot_sha256=run.baseline_source_snapshot_sha256,
            resulting_source_snapshot_sha256=run.source_snapshot_sha256,
            changed_files=changed_files,
            impacted_modules=impacted_modules,
            approved_scenario_ids=approved_ids,
            executed_scenario_ids=executed_ids,
            requirement_analysis=req_analysis,
            codebase_analysis=cb,
            test_generation=gen,
            verification=verification,
            baseline_verification=run.baseline_verification,
            modified_verification=run.modified_verification,
            baseline_comparison=comparison,
            risk_summary=verification.risk_summary if verification else RiskSummary(),
            complete_requirement_assurance=complete_assurance,
            recommendation=recommendation,
            recommendation_label=recommendation_label,
            recommendation_summary=recommendation_summary,
            unverified_requirements=unverified_requirements,
            criterion_traceability=criterion_traceability,
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
            active_comparison = (
                run.baseline_comparison
                or (run.verification.baseline_comparison if run.verification is not None else None)
            )
            if run.verification is None or not run.verification.verification_passed:
                outcome_val = (
                    run.verification.verification_outcome.value
                    if run.verification is not None
                    else "missing"
                )
                details: dict[str, Any] = {"verification_outcome": outcome_val}
                reg_clause = ""
                if (
                    active_comparison is not None
                    and active_comparison.summary.has_confirmed_regression
                ):
                    details["has_confirmed_regression"] = True
                    details["newly_introduced_failures"] = list(
                        active_comparison.summary.newly_introduced_failures
                    )
                    reg_clause = (
                        " Confirmed newly introduced regression(s): "
                        + ", ".join(active_comparison.summary.newly_introduced_failures)
                        + "."
                    )
                raise ReviewConflictError(
                    "Cannot approve a change package when mandatory verification did not pass "
                    f"(verification_outcome='{outcome_val}').{reg_clause} "
                    "Submit 'needs_review' or 'reject' instead.",
                    code="VERIFICATION_APPROVAL_BLOCKED",
                    details=details,
                    run_id=run_id,
                )
            if (
                active_comparison is not None
                and active_comparison.summary.blocks_ordinary_approval
            ):
                raise ReviewConflictError(
                    "Cannot approve a change package when baseline-versus-modified comparison "
                    "confirmed newly introduced mandatory regression(s) "
                    f"({', '.join(active_comparison.summary.newly_introduced_failures)}). "
                    "Submit 'needs_review' or 'reject' instead.",
                    code="REGRESSION_APPROVAL_BLOCKED",
                    details={
                        "newly_introduced_failures": list(
                            active_comparison.summary.newly_introduced_failures
                        ),
                        "comparison_status": active_comparison.status.value,
                    },
                    run_id=run_id,
                )
            if run.repair_termination_reason in {
                RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED,
                RepairTerminationReason.TEST_MODIFICATION_REJECTED,
            }:
                raise ReviewConflictError(
                    "Cannot approve a change package after bounded repair terminated due to "
                    f"'{run.repair_termination_reason.value}'. Submit 'needs_review' or 'reject' "
                    "and perform fresh test review instead.",
                    code="REPAIR_REVIEW_REQUIRED",
                    details={
                        "repair_termination_reason": run.repair_termination_reason.value
                    },
                    run_id=run_id,
                )
            if (
                run.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE
                and (run.patch_application is None or not run.patch_application.applied)
            ):
                raise ReviewConflictError(
                    "Cannot approve a generate_and_verify_change run without confirmed "
                    "isolated-workspace patch application evidence.",
                    code="PATCH_APPLICATION_REQUIRED",
                    run_id=run_id,
                )
            if (
                run.requirement_analysis is not None
                and run.requirement_analysis.has_mandatory_unresolved_ambiguities
            ):
                blocking_ids = [
                    amb.ambiguity_id
                    for amb in run.requirement_analysis.ambiguities
                    if (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
                    and not amb.resolved
                ]
                raise ReviewConflictError(
                    "Cannot approve a change package while mandatory unresolved requirement "
                    f"ambiguities remain open ({', '.join(blocking_ids)}). "
                    "Submit 'needs_review' or 'reject' instead.",
                    code="UNRESOLVED_REQUIREMENT_AMBIGUITY",
                    details={"unresolved_ambiguity_ids": blocking_ids},
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
