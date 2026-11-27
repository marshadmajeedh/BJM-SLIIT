"""Dashboard-ready, safe FastAPI response schemas for ChangeGuard AI (M02).

Security invariants:
- Never expose internal host filesystem absolute paths (such as ``workspace_path``
  or absolute ``repository_path``).
- Never expose raw unredacted internal agent payloads (``raw_report``,
  ``raw_proposals``, ``raw_result``) or secret credentials.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from project.marsadh_orchestrator.store import redact_nested_secrets, redact_secrets
from project.schemas import (
    AgentError,
    AgentExecutionRecord,
    AgentLogEntry,
    BaselineComparisonEvidence,
    ChangedFileImpact,
    ChangePackageReport,
    CodebaseAnalysisResult,
    ExecutionMode,
    FailureDiagnostic,
    GeneratedTestScenario,
    HumanDecisionRecord,
    RiskSummary,
    TestGenerationResult,
    TestReviewRecord,
    VerificationOutcome,
    VerificationResult,
    WorkflowRunRecord,
    WorkflowState,
)


class ErrorResponse(BaseModel):
    """Safe, structured API error envelope without internal stack traces."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = None


class HealthResponse(BaseModel):
    """Service health and capability overview (never exposes secrets)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"]
    service: str = "ChangeGuard AI API"
    version: str
    environment: str
    database_available: bool
    live_llm_configured: bool
    groq_model: str | None = None
    verification_dependencies_available: bool
    git_available: bool
    configured_repository_ids: list[str] = Field(default_factory=list)
    execution_model: str = "in_process_bounded_threadpool"
    limitations: list[str] = Field(default_factory=list)


class CreateRunResponse(BaseModel):
    """HTTP 202 response returned when a workflow run is created and queued."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: WorkflowState
    execution_mode: ExecutionMode
    created_at: datetime
    execution_model: str = "in_process_background_task"
    message: str


class RepositorySummaryResponse(BaseModel):
    """Safe repository metadata without internal absolute filesystem paths."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str | None = None
    repository_name: str
    source_path: str
    base_ref: str
    resolved_base_commit: str | None = None
    change_mode: Literal["working_tree", "committed_range", "unknown"] = "unknown"


class CodebaseAnalysisResponse(BaseModel):
    """Normalized M01 codebase analysis view excluding raw payloads and host paths."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "no_changes", "error"]
    source_path: str
    base_ref: str
    resolved_base_commit: str | None = None
    total_indexed_files: int | None = None
    has_relevant_changes: bool = False
    changed_files_repo_relative: list[str] = Field(default_factory=list)
    changed_files_source_relative: list[str] = Field(default_factory=list)
    impacted_modules_repo_relative: list[str] = Field(default_factory=list)
    impacted_modules_source_relative: list[str] = Field(default_factory=list)
    file_impacts: list[ChangedFileImpact] = Field(default_factory=list)
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)


class TestGenerationResponse(BaseModel):
    """Normalized M03 test generation view excluding raw model envelopes."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    status: Literal["proposed", "skipped_no_changes", "error"]
    executed: Literal[False] = False
    generation_mode: Literal["offline_test", "live_llm"]
    is_mock_output: bool
    model_label: str
    scenarios: list[GeneratedTestScenario] = Field(default_factory=list)
    export_dir_relative: str | None = None
    exported_files: dict[str, str] = Field(default_factory=dict)
    exported_hashes: dict[str, str] = Field(default_factory=dict)
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)


class VerificationResponse(BaseModel):
    """Normalized M04 verification evidence excluding raw runner internals."""

    model_config = ConfigDict(extra="forbid")

    execution_status: Literal["completed", "timeout", "error", "not_executed"]
    verification_outcome: VerificationOutcome
    verification_passed: bool = False
    executed_test_ids: list[str] = Field(default_factory=list)
    executed_test_paths: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    total_tests: int | None = None
    passed: int | None = None
    failed: int | None = None
    duration_seconds: float | None = None
    collection_errors: list[str] = Field(default_factory=list)
    failures: list[FailureDiagnostic] = Field(default_factory=list)
    risk_summary: RiskSummary = Field(default_factory=RiskSummary)
    coverage_percent: float | None = None
    coverage_status: Literal["unavailable", "measured"] = "unavailable"
    ai_root_cause_status: Literal["unavailable", "completed"] = "unavailable"
    baseline_comparison: BaselineComparisonEvidence = Field(
        default_factory=BaselineComparisonEvidence
    )
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)


class ChangePackageResponse(BaseModel):
    """Safe auditable change package summary for the dashboard final-decision view."""

    model_config = ConfigDict(extra="forbid")

    package_id: str | None = None
    package_sha256: str | None = None
    run_id: str
    generated_at: datetime
    requirements: str
    repository_id: str | None = None
    repository_name: str
    source_path: str
    base_ref: str
    resolved_base_commit: str | None = None
    change_mode: str = "unknown"
    execution_mode: str = "offline_test"
    model_label: str | None = None
    is_mock_output: bool | None = None
    changed_files: list[str] = Field(default_factory=list)
    impacted_modules: list[str] = Field(default_factory=list)
    approved_scenario_ids: list[str] = Field(default_factory=list)
    executed_scenario_ids: list[str] = Field(default_factory=list)
    risk_summary: RiskSummary = Field(default_factory=RiskSummary)
    recommendation: Literal["approve", "reject", "needs_review", "no_changes"]
    recommendation_label: Literal[
        "ready_for_human_review",
        "verification_failed",
        "verification_incomplete",
        "additional_review_required",
        "no_changes",
    ]
    recommendation_summary: str = ""
    unverified_requirements: list[str] = Field(default_factory=list)
    traceability: dict[str, Any] = Field(default_factory=dict)
    agent_communication_summary: list[dict[str, Any]] = Field(default_factory=list)
    evidence_limitations: list[str] = Field(default_factory=list)
    human_approval_required: bool = True


class RunSummaryResponse(BaseModel):
    """Compact workflow run summary returned in paginated run listings."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: WorkflowState
    execution_mode: ExecutionMode
    repository: RepositorySummaryResponse
    requirements_preview: str
    created_at: datetime
    updated_at: datetime
    changed_file_count: int = 0
    proposed_scenario_count: int = 0
    verification_outcome: VerificationOutcome | None = None
    verification_passed: bool | None = None
    has_error: bool = False


class RunListResponse(BaseModel):
    """Paginated list of persisted workflow runs."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunSummaryResponse]
    total: int
    limit: int
    offset: int
    state_filter: WorkflowState | None = None


class RunDetailResponse(BaseModel):
    """Full dashboard-ready workflow run details without host paths or raw payloads."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: WorkflowState
    execution_mode: ExecutionMode
    requirements: str
    repository: RepositorySummaryResponse
    source_snapshot_sha256: str | None = None
    git_diff_sha256: str | None = None
    created_at: datetime
    updated_at: datetime
    allowed_actions: list[str] = Field(default_factory=list)
    agent_records: list[AgentExecutionRecord] = Field(default_factory=list)
    codebase_analysis: CodebaseAnalysisResponse | None = None
    test_generation: TestGenerationResponse | None = None
    test_review: TestReviewRecord | None = None
    verification: VerificationResponse | None = None
    report: ChangePackageResponse | None = None
    final_decision: HumanDecisionRecord | None = None
    error: AgentError | None = None


class RunLogsResponse(BaseModel):
    """Structured communication logs for a single workflow run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    count: int
    logs: list[AgentLogEntry]


# ---------------------------------------------------------------------------
# Projection Helpers (Strip Host Paths & Raw Internal Payloads)
# ---------------------------------------------------------------------------


def _repo_display_name(repository_path: str, repository_id: str | None) -> str:
    if repository_id:
        return repository_id
    name = Path(repository_path).name
    return name or "repository"


def _allowed_actions(run: WorkflowRunRecord) -> list[str]:
    if run.state == WorkflowState.AWAITING_TEST_REVIEW:
        return ["submit_test_review"]
    if run.state == WorkflowState.AWAITING_FINAL_APPROVAL:
        if run.verification is not None and run.verification.verification_passed:
            return ["submit_final_decision:approve", "submit_final_decision:reject", "submit_final_decision:needs_review"]
        return ["submit_final_decision:reject", "submit_final_decision:needs_review"]
    return []


def _sanitize_error(error: AgentError | None) -> AgentError | None:
    if error is None:
        return None
    sanitized_details = redact_nested_secrets(error.details)
    return error.model_copy(
        update={
            "message": redact_secrets(error.message),
            "details": sanitized_details if isinstance(sanitized_details, dict) else {},
        }
    )


def _to_codebase_response(
    cb: CodebaseAnalysisResult | None,
) -> CodebaseAnalysisResponse | None:
    if cb is None:
        return None
    return CodebaseAnalysisResponse(
        status=cb.status,
        source_path=cb.source_path,
        base_ref=cb.base_ref,
        resolved_base_commit=cb.resolved_base_commit,
        total_indexed_files=cb.total_indexed_files,
        has_relevant_changes=cb.has_relevant_changes,
        changed_files_repo_relative=list(cb.changed_files_repo_relative),
        changed_files_source_relative=list(cb.changed_files_source_relative),
        impacted_modules_repo_relative=list(cb.impacted_modules_repo_relative),
        impacted_modules_source_relative=list(cb.impacted_modules_source_relative),
        file_impacts=list(cb.file_impacts),
        error=_sanitize_error(cb.error),
        assumptions=list(cb.assumptions),
    )


def _to_test_generation_response(
    gen: TestGenerationResult | None,
) -> TestGenerationResponse | None:
    if gen is None:
        return None
    return TestGenerationResponse(
        status=gen.status,
        executed=gen.executed,
        generation_mode=gen.generation_mode,
        is_mock_output=gen.is_mock_output,
        model_label=gen.model_label,
        scenarios=list(gen.scenarios),
        export_dir_relative=gen.export_dir_relative,
        exported_files=dict(gen.exported_files),
        exported_hashes=dict(gen.exported_hashes),
        error=_sanitize_error(gen.error),
        assumptions=list(gen.assumptions),
    )


def _to_verification_response(
    ver: VerificationResult | None,
) -> VerificationResponse | None:
    if ver is None:
        return None
    return VerificationResponse(
        execution_status=ver.execution_status,
        verification_outcome=ver.verification_outcome,
        verification_passed=ver.verification_passed,
        executed_test_ids=list(ver.executed_test_ids),
        executed_test_paths=list(ver.executed_test_paths),
        exit_code=ver.exit_code,
        total_tests=ver.total_tests,
        passed=ver.passed,
        failed=ver.failed,
        duration_seconds=ver.duration_seconds,
        collection_errors=[redact_secrets(err) for err in ver.collection_errors],
        failures=[
            fail.model_copy(update={"evidence": redact_secrets(fail.evidence)})
            for fail in ver.failures
        ],
        risk_summary=ver.risk_summary,
        coverage_percent=ver.coverage_percent,
        coverage_status=ver.coverage_status,
        ai_root_cause_status=ver.ai_root_cause_status,
        baseline_comparison=ver.baseline_comparison,
        stdout_excerpt=redact_secrets(ver.stdout_excerpt),
        stderr_excerpt=redact_secrets(ver.stderr_excerpt),
        error=_sanitize_error(ver.error),
        assumptions=list(ver.assumptions),
    )


def _to_report_response(
    report: ChangePackageReport | None,
) -> ChangePackageResponse | None:
    if report is None:
        return None
    return ChangePackageResponse(
        package_id=report.package_id,
        package_sha256=report.package_sha256,
        run_id=report.run_id,
        generated_at=report.generated_at,
        requirements=report.requirements,
        repository_id=report.repository_id,
        repository_name=_repo_display_name(report.repository_path, report.repository_id),
        source_path=report.source_path,
        base_ref=report.base_ref,
        resolved_base_commit=report.resolved_base_commit,
        change_mode=report.change_mode,
        execution_mode=report.execution_mode,
        model_label=report.model_label,
        is_mock_output=report.is_mock_output,
        changed_files=list(report.changed_files),
        impacted_modules=list(report.impacted_modules),
        approved_scenario_ids=list(report.approved_scenario_ids),
        executed_scenario_ids=list(report.executed_scenario_ids),
        risk_summary=report.risk_summary,
        recommendation=report.recommendation,
        recommendation_label=report.recommendation_label,
        recommendation_summary=report.recommendation_summary,
        unverified_requirements=list(report.unverified_requirements),
        traceability=dict(report.traceability),
        agent_communication_summary=list(report.agent_communication_summary),
        evidence_limitations=list(report.evidence_limitations),
        human_approval_required=report.human_approval_required,
    )


def to_run_summary(run: WorkflowRunRecord) -> RunSummaryResponse:
    """Convert internal WorkflowRunRecord to a safe list summary."""
    preview = (
        run.requirements
        if len(run.requirements) <= 160
        else run.requirements[:157] + "..."
    )
    return RunSummaryResponse(
        run_id=run.run_id,
        state=run.state,
        execution_mode=run.execution_mode,
        repository=RepositorySummaryResponse(
            repository_id=run.repository.repository_id,
            repository_name=_repo_display_name(
                run.repository.repository_path, run.repository.repository_id
            ),
            source_path=run.repository.source_path,
            base_ref=run.repository.base_ref,
            resolved_base_commit=run.repository.resolved_base_commit,
            change_mode=run.repository.change_mode,
        ),
        requirements_preview=preview,
        created_at=run.created_at,
        updated_at=run.updated_at,
        changed_file_count=(
            len(run.codebase_analysis.changed_files_repo_relative)
            if run.codebase_analysis
            else 0
        ),
        proposed_scenario_count=(
            len(run.test_generation.scenarios) if run.test_generation else 0
        ),
        verification_outcome=(
            run.verification.verification_outcome if run.verification else None
        ),
        verification_passed=(
            run.verification.verification_passed if run.verification else None
        ),
        has_error=run.error is not None,
    )


def to_run_detail(run: WorkflowRunRecord) -> RunDetailResponse:
    """Convert internal WorkflowRunRecord to a safe dashboard-ready detail payload."""
    return RunDetailResponse(
        run_id=run.run_id,
        state=run.state,
        execution_mode=run.execution_mode,
        requirements=run.requirements,
        repository=RepositorySummaryResponse(
            repository_id=run.repository.repository_id,
            repository_name=_repo_display_name(
                run.repository.repository_path, run.repository.repository_id
            ),
            source_path=run.repository.source_path,
            base_ref=run.repository.base_ref,
            resolved_base_commit=run.repository.resolved_base_commit,
            change_mode=run.repository.change_mode,
        ),
        source_snapshot_sha256=run.source_snapshot_sha256,
        git_diff_sha256=run.git_diff_sha256,
        created_at=run.created_at,
        updated_at=run.updated_at,
        allowed_actions=_allowed_actions(run),
        agent_records=[
            rec.model_copy(
                update={
                    "output_summary": redact_secrets(rec.output_summary),
                    "error": _sanitize_error(rec.error),
                }
            )
            for rec in run.agent_records
        ],
        codebase_analysis=_to_codebase_response(run.codebase_analysis),
        test_generation=_to_test_generation_response(run.test_generation),
        test_review=run.test_review,
        verification=_to_verification_response(run.verification),
        report=_to_report_response(run.report),
        final_decision=run.final_decision,
        error=_sanitize_error(run.error),
    )
