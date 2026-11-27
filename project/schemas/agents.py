"""Normalized agent execution and result schemas for ChangeGuard AI (M02).

Design assumptions:
- Teammate payloads (M01, M03, M04) are preserved verbatim under raw_* fields
  while normalized fields expose consistent contracts for the orchestrator and
  future API/UI layers.
- Missing metrics (such as coverage, baseline-vs-modified regression deltas,
  or AI root-cause analysis) are represented explicitly as unavailable / None
  and are never fabricated.
- Successful test execution is strictly distinct from successful verification:
  nonzero exit codes, collection errors, timeouts, and zero-test runs can never
  yield ``verification_passed=True``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class AgentExecutionStatus(str, Enum):
    """Normalized execution status for an individual agent invocation."""

    SUCCEEDED = "succeeded"
    COMPLETED_NO_CHANGES = "completed_no_changes"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class AgentError(BaseModel):
    """Structured, non-secret error payload recorded across M02 components."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class AgentExecutionRecord(BaseModel):
    """Audit record describing a single agent or adapter invocation."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(..., min_length=1, max_length=120)
    stage: str = Field(..., min_length=1, max_length=80)
    status: AgentExecutionStatus
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)
    duration_ms: int = Field(default=0, ge=0)
    execution_mode: str = Field(default="offline_test", min_length=1, max_length=60)
    output_summary: str = Field(default="", max_length=2000)
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)


class ImpactedFile(BaseModel):
    """Normalized downstream module impacted by a changed Python file."""

    model_config = ConfigDict(extra="forbid")

    source_relative_path: str = Field(..., min_length=1)
    repo_relative_path: str = Field(..., min_length=1)
    impact_level: Literal["direct", "indirect"]
    dependency_depth: int = Field(..., ge=1)
    via_file: str = Field(..., min_length=1)
    import_line: int | None = Field(default=None, ge=1)
    reason: str = ""


class ChangedFileImpact(BaseModel):
    """Normalized impact analysis for a single changed Python file."""

    model_config = ConfigDict(extra="forbid")

    repo_relative_path: str = Field(..., min_length=1)
    source_relative_path: str = Field(..., min_length=1)
    changed_functions: list[str] = Field(default_factory=list)
    changed_lines: list[int] = Field(default_factory=list)
    direct_impacted_files: list[ImpactedFile] = Field(default_factory=list)
    indirect_impacted_files: list[ImpactedFile] = Field(default_factory=list)
    total_impacted: int = Field(default=0, ge=0)
    direct_count: int = Field(default=0, ge=0)
    indirect_count: int = Field(default=0, ge=0)


class CodebaseAnalysisResult(BaseModel):
    """Normalized result returned by the M01 CodebaseAdapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "no_changes", "error"]
    repository_path: str = Field(..., min_length=1)
    source_path: str = Field(..., min_length=1)
    base_ref: str = Field(..., min_length=1)
    resolved_base_commit: str | None = None
    total_indexed_files: int | None = Field(default=None, ge=0)
    has_relevant_changes: bool = False
    changed_files_repo_relative: list[str] = Field(default_factory=list)
    changed_files_source_relative: list[str] = Field(default_factory=list)
    impacted_modules_repo_relative: list[str] = Field(default_factory=list)
    impacted_modules_source_relative: list[str] = Field(default_factory=list)
    file_impacts: list[ChangedFileImpact] = Field(default_factory=list)
    raw_report: dict[str, Any] | None = None
    raw_repository_analysis: dict[str, Any] | None = None
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_consistency(self) -> CodebaseAnalysisResult:
        if self.status == "no_changes" and self.has_relevant_changes:
            raise ValueError("no_changes status cannot report has_relevant_changes=True")
        if self.status == "completed" and not self.has_relevant_changes:
            raise ValueError("completed status requires at least one relevant changed file")
        return self


class GeneratedTestScenario(BaseModel):
    """A single reviewable test proposal produced by M03."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    )
    title: str = Field(..., min_length=1)
    rationale: str = Field(..., min_length=1)
    assumptions: str = Field(..., min_length=1)
    test_code: str = Field(..., min_length=1)
    content_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    exported_relative_path: str | None = None
    exported_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approved: bool = False


class ApprovedTestSpec(BaseModel):
    """Controlled mapping from a reviewed test identifier to its verified file hash."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(
        ...,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
    )
    relative_path: str = Field(..., min_length=1)
    expected_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    approved: bool = True


class TestGenerationResult(BaseModel):
    """Normalized result returned by the M03 TestingAdapter."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    status: Literal["proposed", "skipped_no_changes", "error"]
    executed: Literal[False] = False
    generation_mode: Literal["offline_test", "live_llm"] = "offline_test"
    is_mock_output: bool = True
    model_label: str = Field(default="unconfigured", min_length=1, max_length=160)
    scenarios: list[GeneratedTestScenario] = Field(default_factory=list)
    export_dir_relative: str | None = None
    exported_files: dict[str, str] = Field(default_factory=dict)
    exported_hashes: dict[str, str] = Field(default_factory=dict)
    raw_proposals: dict[str, Any] | None = None
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)


class VerificationOutcome(str, Enum):
    """Semantic outcome distinguishing verification pass from failures/errors."""

    PASSED = "passed"
    FAILED_TESTS = "failed_tests"
    COLLECTION_ERROR = "collection_error"
    NO_TESTS_COLLECTED = "no_tests_collected"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    UNAVAILABLE = "unavailable"


class FailureDiagnostic(BaseModel):
    """Structured failure evidence enriched with M04 failure_analyzer output."""

    model_config = ConfigDict(extra="forbid")

    test_name: str = Field(..., min_length=1)
    status: str = Field(..., min_length=1)
    evidence: str = ""
    error_type: str = "Unknown"
    reason: str = "Further analysis required"
    risk: str = "unknown"
    recommendation: str = "Review failure evidence"


class RiskSummary(BaseModel):
    """Explicit risk summary that reports Unknown rather than fabricating scores."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["Low", "Medium", "High", "Critical", "Unknown"] = "Unknown"
    score: int | None = Field(default=None, ge=0, le=100)
    method: str = "unavailable"
    rationale: str = (
        "Risk could not be reliably calculated from available verification evidence."
    )
    evidence_available: bool = False


class BaselineComparisonEvidence(BaseModel):
    """Placeholder interface for future baseline-versus-modified regression runs."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["unavailable", "completed"] = "unavailable"
    baseline_ref: str | None = None
    baseline_passed: int | None = Field(default=None, ge=0)
    baseline_failed: int | None = Field(default=None, ge=0)
    modified_passed: int | None = Field(default=None, ge=0)
    modified_failed: int | None = Field(default=None, ge=0)
    newly_failing_tests: list[str] | None = None
    notes: str = (
        "Baseline-versus-modified comparison was not executed; running generated tests "
        "against the modified repository alone is not proof of regression safety."
    )


class VerificationResult(BaseModel):
    """Normalized result returned by the M04 VerificationAdapter."""

    model_config = ConfigDict(extra="forbid")

    execution_status: Literal["completed", "timeout", "error", "not_executed"]
    verification_outcome: VerificationOutcome
    verification_passed: bool = False
    executed_test_ids: list[str] = Field(default_factory=list)
    executed_test_paths: list[str] = Field(default_factory=list)
    exit_code: int | None = None
    total_tests: int | None = Field(default=None, ge=0)
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    collection_errors: list[str] = Field(default_factory=list)
    failures: list[FailureDiagnostic] = Field(default_factory=list)
    risk_summary: RiskSummary = Field(default_factory=RiskSummary)
    coverage_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    coverage_status: Literal["unavailable", "measured"] = "unavailable"
    ai_root_cause_status: Literal["unavailable", "completed"] = "unavailable"
    baseline_comparison: BaselineComparisonEvidence = Field(
        default_factory=BaselineComparisonEvidence
    )
    stdout_excerpt: str = Field(default="", max_length=8000)
    stderr_excerpt: str = Field(default="", max_length=8000)
    raw_result: dict[str, Any] | None = None
    error: AgentError | None = None
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_verification_truthfulness(self) -> VerificationResult:
        if self.verification_passed:
            if self.verification_outcome != VerificationOutcome.PASSED:
                raise ValueError(
                    "verification_passed=True requires verification_outcome=PASSED"
                )
            if self.execution_status != "completed":
                raise ValueError(
                    "verification_passed=True requires execution_status='completed'"
                )
            if self.exit_code != 0:
                raise ValueError(
                    "verification_passed=True requires exit_code == 0"
                )
            if not self.total_tests or self.total_tests <= 0:
                raise ValueError(
                    "verification_passed=True requires total_tests > 0"
                )
            if self.failed and self.failed > 0:
                raise ValueError(
                    "verification_passed=True requires failed == 0"
                )
            if self.collection_errors:
                raise ValueError(
                    "verification_passed=True requires no collection errors"
                )
        return self


class ChangePackageReport(BaseModel):
    """Compiled auditable change package presented for final human approval."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1)
    generated_at: datetime = Field(default_factory=utc_now)
    requirements: str = Field(..., min_length=1)
    repository_path: str = Field(..., min_length=1)
    source_path: str = Field(..., min_length=1)
    base_ref: str = Field(..., min_length=1)
    resolved_base_commit: str | None = None
    codebase_analysis: CodebaseAnalysisResult | None = None
    test_generation: TestGenerationResult | None = None
    verification: VerificationResult | None = None
    risk_summary: RiskSummary = Field(default_factory=RiskSummary)
    recommendation: Literal["approve", "reject", "needs_review", "no_changes"] = (
        "needs_review"
    )
    evidence_limitations: list[str] = Field(default_factory=list)
    human_approval_required: bool = True
