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

from project.marsadh_orchestrator.store import (
    _sanitize_baseline_comparison,
    _sanitize_baseline_verification,
    _sanitize_modified_verification,
    redact_nested_secrets,
    redact_secrets,
)
from project.schemas import (
    AcceptanceCriterion,
    AgentError,
    AgentExecutionRecord,
    AgentLogEntry,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    BusinessConstraint,
    ChangedFileImpact,
    ChangePackageReport,
    CodebaseAnalysisResult,
    ExecutionMode,
    FailureDiagnostic,
    GeneratedTestScenario,
    HumanDecisionRecord,
    ModifiedVerificationResult,
    PatchApplicationResult,
    PatchFileChange,
    PatchGenerationResult,
    PatchModelMetadata,
    PatchValidationResult,
    PreChangeCodebaseContext,
    PreChangeFileSummary,
    RequirementAmbiguity,
    RequirementAnalysisResult,
    RequirementAssumption,
    RequirementTraceability,
    RiskSummary,
    TestGenerationResult,
    TestReviewRecord,
    VerificationComparisonStatus,
    VerificationOutcome,
    VerificationResult,
    WorkflowMode,
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
    patch_generator_configured: bool = False
    supported_workflow_modes: list[str] = Field(
        default_factory=lambda: [
            WorkflowMode.VERIFY_EXISTING_CHANGE.value,
            WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
        ]
    )
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
    workflow_mode: WorkflowMode = WorkflowMode.VERIFY_EXISTING_CHANGE
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


class RequirementAnalysisResponse(BaseModel):
    """Normalized A1 structured requirement analysis view for dashboard consumption."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "needs_clarification", "error"]
    analysis_mode: Literal["offline_test", "live_llm"]
    is_deterministic_fixture: bool
    is_mock_output: bool
    model_label: str
    requirement_summary: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    business_constraints: list[BusinessConstraint] = Field(default_factory=list)
    assumptions: list[RequirementAssumption] = Field(default_factory=list)
    ambiguities: list[RequirementAmbiguity] = Field(default_factory=list)
    preserved_behaviors: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    has_mandatory_unresolved_ambiguities: bool = False
    error: AgentError | None = None
    notes: list[str] = Field(default_factory=list)


class PreChangeContextResponse(BaseModel):
    """Safe pre-change repository context summary excluding host paths and raw source code."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "unavailable", "error"]
    scope: Literal["static_repository_index_only", "unavailable"]
    pre_change_impact_prediction_available: Literal[False] = False
    source_path: str
    base_ref: str
    resolved_base_commit: str | None = None
    total_indexed_files: int = 0
    indexed_file_paths: list[str] = Field(default_factory=list)
    indexed_files: list[PreChangeFileSummary] = Field(default_factory=list)
    relevant_file_paths: list[str] = Field(default_factory=list)
    limitation_notes: list[str] = Field(default_factory=list)
    error: AgentError | None = None


class PatchProposalResponse(BaseModel):
    """Safe reviewable patch proposal view for dashboard inspection."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    patch_sha256: str
    unified_diff: str
    affected_files: list[PatchFileChange] = Field(default_factory=list)
    change_rationale: str
    related_criterion_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risk_indicators: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PatchGenerationResponse(BaseModel):
    """Sanitized A4 patch generation view with explicit fixture/mock indicators."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["proposed", "unavailable", "blocked", "error"]
    attempt_number: int = 1
    proposal: PatchProposalResponse | None = None
    model_metadata: PatchModelMetadata
    is_deterministic_fixture: bool = False
    is_mock_output: bool = False
    error: AgentError | None = None
    notes: list[str] = Field(default_factory=list)


class PatchValidationResponse(BaseModel):
    """Safe patch validation evidence view."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    status: Literal["passed", "failed"]
    patch_sha256: str
    expected_snapshot_sha256: str | None = None
    checked_files: list[str] = Field(default_factory=list)
    patch_size_bytes: int = 0
    changed_file_count: int = 0
    git_apply_check_passed: bool = False
    checks_passed: list[str] = Field(default_factory=list)
    violations: list[AgentError] = Field(default_factory=list)
    validated_at: datetime


class PatchApplicationResponse(BaseModel):
    """Safe patch application evidence view preserving original and resulting snapshots."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    status: Literal["applied", "failed", "reverted"]
    patch_sha256: str
    original_source_snapshot_sha256: str
    original_git_diff_sha256: str
    resulting_source_snapshot_sha256: str | None = None
    resulting_git_diff_sha256: str | None = None
    applied_files: list[str] = Field(default_factory=list)
    rollback_performed: bool = False
    workspace_clean_after_rollback: bool | None = None
    error: AgentError | None = None
    applied_at: datetime
    isolation_note: str


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
    workflow_mode: str = "verify_existing_change"
    execution_mode: str = "offline_test"
    model_label: str | None = None
    is_mock_output: bool | None = None
    patch_proposal_sha256: str | None = None
    patch_is_deterministic_fixture: bool | None = None
    patch_applied: bool | None = None
    original_source_snapshot_sha256: str | None = None
    baseline_source_snapshot_sha256: str | None = None
    resulting_source_snapshot_sha256: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    impacted_modules: list[str] = Field(default_factory=list)
    approved_scenario_ids: list[str] = Field(default_factory=list)
    executed_scenario_ids: list[str] = Field(default_factory=list)
    requirement_analysis: RequirementAnalysisResponse | None = None
    baseline_verification: BaselineVerificationResult | None = None
    modified_verification: ModifiedVerificationResult | None = None
    baseline_comparison: BaselineComparisonEvidence | None = None
    risk_summary: RiskSummary = Field(default_factory=RiskSummary)
    complete_requirement_assurance: bool = False
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
    criterion_traceability: list[RequirementTraceability] = Field(default_factory=list)
    traceability: dict[str, Any] = Field(default_factory=dict)
    agent_communication_summary: list[dict[str, Any]] = Field(default_factory=list)
    evidence_limitations: list[str] = Field(default_factory=list)
    human_approval_required: bool = True


class RunSummaryResponse(BaseModel):
    """Compact workflow run summary returned in paginated run listings."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: WorkflowState
    workflow_mode: WorkflowMode = WorkflowMode.VERIFY_EXISTING_CHANGE
    execution_mode: ExecutionMode
    repository: RepositorySummaryResponse
    requirements_preview: str
    created_at: datetime
    updated_at: datetime
    acceptance_criteria_count: int = 0
    has_unresolved_ambiguities: bool = False
    patch_applied: bool | None = None
    patch_attempt_number: int | None = None
    changed_file_count: int = 0
    proposed_scenario_count: int = 0
    verification_outcome: VerificationOutcome | None = None
    verification_passed: bool | None = None
    baseline_comparison_status: VerificationComparisonStatus | None = None
    has_confirmed_regression: bool = False
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
    workflow_mode: WorkflowMode = WorkflowMode.VERIFY_EXISTING_CHANGE
    execution_mode: ExecutionMode
    requirements: str
    repository: RepositorySummaryResponse
    original_source_snapshot_sha256: str | None = None
    original_git_diff_sha256: str | None = None
    baseline_source_snapshot_sha256: str | None = None
    source_snapshot_sha256: str | None = None
    git_diff_sha256: str | None = None
    patch_proposal_sha256: str | None = None
    patch_attempt_number: int | None = None
    created_at: datetime
    updated_at: datetime
    allowed_actions: list[str] = Field(default_factory=list)
    agent_records: list[AgentExecutionRecord] = Field(default_factory=list)
    requirement_analysis: RequirementAnalysisResponse | None = None
    pre_change_context: PreChangeContextResponse | None = None
    patch_generation: PatchGenerationResponse | None = None
    patch_validation: PatchValidationResponse | None = None
    patch_application: PatchApplicationResponse | None = None
    codebase_analysis: CodebaseAnalysisResponse | None = None
    test_generation: TestGenerationResponse | None = None
    test_review: TestReviewRecord | None = None
    baseline_verification: BaselineVerificationResult | None = None
    modified_verification: ModifiedVerificationResult | None = None
    baseline_comparison: BaselineComparisonEvidence | None = None
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
        has_blocking_ambiguity = bool(
            run.requirement_analysis is not None
            and run.requirement_analysis.has_mandatory_unresolved_ambiguities
        )
        patch_ready = (
            run.workflow_mode != WorkflowMode.GENERATE_AND_VERIFY_CHANGE
            or (run.patch_application is not None and run.patch_application.applied)
        )
        active_comparison = (
            run.baseline_comparison
            or (run.verification.baseline_comparison if run.verification is not None else None)
        )
        has_confirmed_regression = bool(
            active_comparison is not None
            and active_comparison.summary.blocks_ordinary_approval
        )
        if (
            run.verification is not None
            and run.verification.verification_passed
            and not has_blocking_ambiguity
            and not has_confirmed_regression
            and patch_ready
        ):
            return [
                "submit_final_decision:approve",
                "submit_final_decision:reject",
                "submit_final_decision:needs_review",
            ]
        return ["submit_final_decision:reject", "submit_final_decision:needs_review"]
    return []


def _to_pre_change_context_response(
    ctx: PreChangeCodebaseContext | None,
) -> PreChangeContextResponse | None:
    if ctx is None:
        return None
    return PreChangeContextResponse(
        status=ctx.status,
        scope=ctx.scope,
        pre_change_impact_prediction_available=ctx.pre_change_impact_prediction_available,
        source_path=ctx.source_path,
        base_ref=ctx.base_ref,
        resolved_base_commit=ctx.resolved_base_commit,
        total_indexed_files=ctx.total_indexed_files,
        indexed_file_paths=list(ctx.indexed_file_paths),
        indexed_files=list(ctx.indexed_files),
        relevant_file_paths=list(ctx.relevant_file_paths),
        limitation_notes=[redact_secrets(note) for note in ctx.limitation_notes],
        error=_sanitize_error(ctx.error),
    )


def _to_patch_generation_response(
    pg: PatchGenerationResult | None,
) -> PatchGenerationResponse | None:
    if pg is None:
        return None
    proposal_resp = None
    if pg.proposal is not None:
        proposal_resp = PatchProposalResponse(
            proposal_id=pg.proposal.proposal_id,
            patch_sha256=pg.proposal.patch_sha256,
            unified_diff=redact_secrets(pg.proposal.unified_diff),
            affected_files=list(pg.proposal.affected_files),
            change_rationale=redact_secrets(pg.proposal.change_rationale),
            related_criterion_ids=list(pg.proposal.related_criterion_ids),
            assumptions=[redact_secrets(a) for a in pg.proposal.assumptions],
            risk_indicators=[redact_secrets(r) for r in pg.proposal.risk_indicators],
            notes=[redact_secrets(n) for n in pg.proposal.notes],
        )
    return PatchGenerationResponse(
        status=pg.status,
        attempt_number=pg.attempt_number,
        proposal=proposal_resp,
        model_metadata=pg.model_metadata,
        is_deterministic_fixture=pg.is_deterministic_fixture,
        is_mock_output=pg.is_mock_output,
        error=_sanitize_error(pg.error),
        notes=[redact_secrets(note) for note in pg.notes],
    )


def _to_patch_validation_response(
    pv: PatchValidationResult | None,
) -> PatchValidationResponse | None:
    if pv is None:
        return None
    return PatchValidationResponse(
        valid=pv.valid,
        status=pv.status,
        patch_sha256=pv.patch_sha256,
        expected_snapshot_sha256=pv.expected_snapshot_sha256,
        checked_files=list(pv.checked_files),
        patch_size_bytes=pv.patch_size_bytes,
        changed_file_count=pv.changed_file_count,
        git_apply_check_passed=pv.git_apply_check_passed,
        checks_passed=list(pv.checks_passed),
        violations=[
            sanitized
            for v in pv.violations
            if (sanitized := _sanitize_error(v)) is not None
        ],
        validated_at=pv.validated_at,
    )


def _to_patch_application_response(
    pa: PatchApplicationResult | None,
) -> PatchApplicationResponse | None:
    if pa is None:
        return None
    return PatchApplicationResponse(
        applied=pa.applied,
        status=pa.status,
        patch_sha256=pa.patch_sha256,
        original_source_snapshot_sha256=pa.original_source_snapshot_sha256,
        original_git_diff_sha256=pa.original_git_diff_sha256,
        resulting_source_snapshot_sha256=pa.resulting_source_snapshot_sha256,
        resulting_git_diff_sha256=pa.resulting_git_diff_sha256,
        applied_files=list(pa.applied_files),
        rollback_performed=pa.rollback_performed,
        workspace_clean_after_rollback=pa.workspace_clean_after_rollback,
        error=_sanitize_error(pa.error),
        applied_at=pa.applied_at,
        isolation_note=pa.isolation_note,
    )


def _to_requirement_analysis_response(
    req: RequirementAnalysisResult | None,
) -> RequirementAnalysisResponse | None:
    if req is None:
        return None
    return RequirementAnalysisResponse(
        status=req.status,
        analysis_mode=req.analysis_mode,
        is_deterministic_fixture=req.is_deterministic_fixture,
        is_mock_output=req.is_mock_output,
        model_label=req.model_label,
        requirement_summary=redact_secrets(req.requirement_summary),
        acceptance_criteria=list(req.acceptance_criteria),
        business_constraints=list(req.business_constraints),
        assumptions=list(req.assumptions),
        ambiguities=list(req.ambiguities),
        preserved_behaviors=list(req.preserved_behaviors),
        edge_cases=list(req.edge_cases),
        has_mandatory_unresolved_ambiguities=req.has_mandatory_unresolved_ambiguities,
        error=_sanitize_error(req.error),
        notes=[redact_secrets(note) for note in req.notes],
    )


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
        baseline_comparison=(
            _sanitize_baseline_comparison(ver.baseline_comparison)
            or ver.baseline_comparison
        ),
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
        workflow_mode=report.workflow_mode,
        execution_mode=report.execution_mode,
        model_label=report.model_label,
        is_mock_output=report.is_mock_output,
        patch_proposal_sha256=report.patch_proposal_sha256,
        patch_is_deterministic_fixture=report.patch_is_deterministic_fixture,
        patch_applied=report.patch_applied,
        original_source_snapshot_sha256=report.original_source_snapshot_sha256,
        baseline_source_snapshot_sha256=report.baseline_source_snapshot_sha256,
        resulting_source_snapshot_sha256=report.resulting_source_snapshot_sha256,
        changed_files=list(report.changed_files),
        impacted_modules=list(report.impacted_modules),
        approved_scenario_ids=list(report.approved_scenario_ids),
        executed_scenario_ids=list(report.executed_scenario_ids),
        requirement_analysis=_to_requirement_analysis_response(
            report.requirement_analysis
        ),
        baseline_verification=_sanitize_baseline_verification(
            report.baseline_verification
        ),
        modified_verification=_sanitize_modified_verification(
            report.modified_verification
        ),
        baseline_comparison=_sanitize_baseline_comparison(report.baseline_comparison),
        risk_summary=report.risk_summary,
        complete_requirement_assurance=report.complete_requirement_assurance,
        recommendation=report.recommendation,
        recommendation_label=report.recommendation_label,
        recommendation_summary=report.recommendation_summary,
        unverified_requirements=list(report.unverified_requirements),
        criterion_traceability=list(report.criterion_traceability),
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
        workflow_mode=run.workflow_mode,
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
        acceptance_criteria_count=(
            len(run.requirement_analysis.acceptance_criteria)
            if run.requirement_analysis
            else 0
        ),
        has_unresolved_ambiguities=(
            run.requirement_analysis.has_mandatory_unresolved_ambiguities
            if run.requirement_analysis
            else False
        ),
        patch_applied=(
            run.patch_application.applied if run.patch_application is not None else None
        ),
        patch_attempt_number=run.patch_attempt_number,
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
        baseline_comparison_status=(
            run.baseline_comparison.status
            if run.baseline_comparison is not None
            else (
                run.verification.baseline_comparison.status
                if run.verification is not None
                else None
            )
        ),
        has_confirmed_regression=bool(
            (
                run.baseline_comparison is not None
                and run.baseline_comparison.summary.has_confirmed_regression
            )
            or (
                run.verification is not None
                and run.verification.baseline_comparison.summary.has_confirmed_regression
            )
        ),
        has_error=run.error is not None,
    )


def to_run_detail(run: WorkflowRunRecord) -> RunDetailResponse:
    """Convert internal WorkflowRunRecord to a safe dashboard-ready detail payload."""
    return RunDetailResponse(
        run_id=run.run_id,
        state=run.state,
        workflow_mode=run.workflow_mode,
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
        original_source_snapshot_sha256=run.original_source_snapshot_sha256,
        original_git_diff_sha256=run.original_git_diff_sha256,
        baseline_source_snapshot_sha256=run.baseline_source_snapshot_sha256,
        source_snapshot_sha256=run.source_snapshot_sha256,
        git_diff_sha256=run.git_diff_sha256,
        patch_proposal_sha256=run.patch_proposal_sha256,
        patch_attempt_number=run.patch_attempt_number,
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
        requirement_analysis=_to_requirement_analysis_response(
            run.requirement_analysis
        ),
        pre_change_context=_to_pre_change_context_response(run.pre_change_context),
        patch_generation=_to_patch_generation_response(run.patch_generation),
        patch_validation=_to_patch_validation_response(run.patch_validation),
        patch_application=_to_patch_application_response(run.patch_application),
        codebase_analysis=_to_codebase_response(run.codebase_analysis),
        test_generation=_to_test_generation_response(run.test_generation),
        test_review=run.test_review,
        baseline_verification=_sanitize_baseline_verification(run.baseline_verification),
        modified_verification=_sanitize_modified_verification(run.modified_verification),
        baseline_comparison=_sanitize_baseline_comparison(run.baseline_comparison),
        verification=_to_verification_response(run.verification),
        report=_to_report_response(run.report),
        final_decision=run.final_decision,
        error=_sanitize_error(run.error),
    )
