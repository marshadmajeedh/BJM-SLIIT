"""Validated Baseline-vs-Modified Verification Comparison schemas for ChangeGuard AI (M02).

Design invariants:
- Reuses and extends ``BaselineComparisonEvidence`` with backward-compatible
  defaults so pre-P0-7 persisted runs deserialize cleanly.
- Distinguishes generated acceptance tests from existing repository regression
  tests via ``TestSuiteKind`` and explicit ``TestPurpose`` (never inventing
  classifications when test intent is unavailable).
- Enforces truthfulness across every comparison model:
  - A collection error can NEVER be classified as a newly introduced regression.
  - A fail-to-pass outcome is NEVER automatically described as a repaired
    regression when ``test_purpose`` is ``unknown``.
  - ``regression_safety_established`` can NEVER be ``True`` unless comparison
    completed with comparable per-test evidence and zero confirmed regressions.
  - Aggregate suite outcomes are preserved separately from per-test outcomes so
    individual passing tests are never inferred solely from pass counts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import STABLE_ID_PATTERN, AgentError

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


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


class TestPurpose(str, Enum):
    """Explicit test intent; defaults to unknown rather than inventing intent."""

    __test__ = False

    NEW_REQUIREMENT_ACCEPTANCE = "new_requirement_acceptance"
    EXISTING_BEHAVIOR_PRESERVATION = "existing_behavior_preservation"
    EXISTING_REPOSITORY_REGRESSION = "existing_repository_regression"
    UNKNOWN = "unknown"


class TestSuiteKind(str, Enum):
    """Distinguishes approved generated tests from existing repository test suites."""

    __test__ = False

    GENERATED_ACCEPTANCE = "generated_acceptance"
    EXISTING_REGRESSION = "existing_regression"


class IndividualTestOutcome(str, Enum):
    """Observed execution outcome for an individual test or scenario unit."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    COLLECTION_ERROR = "collection_error"
    TIMEOUT = "timeout"
    EXECUTION_ERROR = "execution_error"
    NOT_EXECUTED = "not_executed"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class TestComparisonClassification(str, Enum):
    """Deterministic classification of baseline-vs-modified execution comparison."""

    __test__ = False

    PRESERVED_BEHAVIOR = "preserved_behavior"
    NEWLY_INTRODUCED_FAILURE = "newly_introduced_failure"
    NEWLY_SATISFIED_ACCEPTANCE_CRITERION = "newly_satisfied_acceptance_criterion"
    FIXED_EXISTING_FAILURE = "fixed_existing_failure"
    FAIL_TO_PASS_UNCLASSIFIED = "fail_to_pass_unclassified"
    PERSISTENT_FAILURE = "persistent_failure"
    NOT_COMPARABLE = "not_comparable"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class VerificationComparisonStatus(str, Enum):
    """Overall completeness status of a baseline-vs-modified verification comparison."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class TestExecutionEvidence(BaseModel):
    """Structured execution evidence for a single stable test or scenario identity."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    test_identity: str = Field(..., min_length=1, max_length=300)
    scenario_id: str | None = Field(default=None, pattern=STABLE_ID_PATTERN.pattern)
    node_id: str | None = Field(default=None, max_length=500)
    relative_path: str | None = Field(default=None, max_length=500)
    suite_kind: TestSuiteKind = TestSuiteKind.GENERATED_ACCEPTANCE
    test_purpose: TestPurpose = TestPurpose.UNKNOWN
    criterion_ids: list[str] = Field(default_factory=list)
    test_source_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    outcome: IndividualTestOutcome = IndividualTestOutcome.UNAVAILABLE
    exit_code: int | None = None
    total_tests: int | None = Field(default=None, ge=0)
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    collection_errors: list[str] = Field(default_factory=list)
    failure_message: str | None = Field(default=None, max_length=4000)
    evidence_granularity: Literal[
        "per_test_node",
        "single_scenario_file",
        "aggregate_only",
        "unavailable",
    ] = "unavailable"
    evidence_limitations: list[str] = Field(default_factory=list)

    @field_validator("criterion_ids")
    @classmethod
    def _validate_criterion_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw_id in value:
            cid = raw_id.strip()
            if not STABLE_ID_PATTERN.match(cid):
                raise ValueError(f"Invalid criterion_id '{raw_id}' in TestExecutionEvidence")
            if cid not in seen:
                seen.add(cid)
                cleaned.append(cid)
        return cleaned

    @model_validator(mode="after")
    def _validate_outcome_truthfulness(self) -> TestExecutionEvidence:
        if self.outcome == IndividualTestOutcome.PASSED:
            if self.collection_errors:
                raise ValueError("TestExecutionEvidence cannot be PASSED with collection_errors")
            if self.failed is not None and self.failed > 0:
                raise ValueError("TestExecutionEvidence cannot be PASSED with failed > 0")
            if self.exit_code is not None and self.exit_code != 0:
                raise ValueError("TestExecutionEvidence cannot be PASSED with nonzero exit_code")
        return self


class SuiteExecutionSummary(BaseModel):
    """Execution summary for a separate existing repository regression test suite."""

    model_config = ConfigDict(extra="forbid")

    suite_kind: TestSuiteKind = TestSuiteKind.EXISTING_REGRESSION
    execution_status: Literal[
        "completed", "timeout", "error", "not_executed", "unavailable"
    ] = "unavailable"
    verification_outcome: VerificationOutcome = VerificationOutcome.UNAVAILABLE
    discovered_test_paths: list[str] = Field(default_factory=list)
    test_file_hashes: dict[str, str] = Field(default_factory=dict)
    exit_code: int | None = None
    total_tests: int | None = Field(default=None, ge=0)
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    collection_errors: list[str] = Field(default_factory=list)
    failures: list[FailureDiagnostic] = Field(default_factory=list)
    test_evidence: list[TestExecutionEvidence] = Field(default_factory=list)
    evidence_limitations: list[str] = Field(default_factory=list)
    error: AgentError | None = None


class BaselineVerificationResult(BaseModel):
    """Verification execution evidence captured against the resolved baseline Git commit."""

    model_config = ConfigDict(extra="forbid")

    snapshot_role: Literal["baseline"] = "baseline"
    execution_status: Literal[
        "completed", "timeout", "error", "not_executed", "unavailable"
    ] = "unavailable"
    verification_outcome: VerificationOutcome = VerificationOutcome.UNAVAILABLE
    baseline_ref: str | None = None
    baseline_commit: str | None = None
    source_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    approved_test_hashes: dict[str, str] = Field(default_factory=dict)
    executed_test_ids: list[str] = Field(default_factory=list)
    executed_test_paths: list[str] = Field(default_factory=list)
    suite_kind: TestSuiteKind = TestSuiteKind.GENERATED_ACCEPTANCE
    exit_code: int | None = None
    total_tests: int | None = Field(default=None, ge=0)
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    collection_errors: list[str] = Field(default_factory=list)
    failures: list[FailureDiagnostic] = Field(default_factory=list)
    test_evidence: list[TestExecutionEvidence] = Field(default_factory=list)
    existing_regression_suite: SuiteExecutionSummary | None = None
    evidence_limitations: list[str] = Field(default_factory=list)
    error: AgentError | None = None


class ModifiedVerificationResult(BaseModel):
    """Verification execution evidence captured against the modified workspace snapshot."""

    model_config = ConfigDict(extra="forbid")

    snapshot_role: Literal["modified"] = "modified"
    execution_status: Literal[
        "completed", "timeout", "error", "not_executed", "unavailable"
    ] = "unavailable"
    verification_outcome: VerificationOutcome = VerificationOutcome.UNAVAILABLE
    verification_passed: bool = False
    source_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    git_diff_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    approved_test_hashes: dict[str, str] = Field(default_factory=dict)
    executed_test_ids: list[str] = Field(default_factory=list)
    executed_test_paths: list[str] = Field(default_factory=list)
    suite_kind: TestSuiteKind = TestSuiteKind.GENERATED_ACCEPTANCE
    exit_code: int | None = None
    total_tests: int | None = Field(default=None, ge=0)
    passed: int | None = Field(default=None, ge=0)
    failed: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0.0)
    collection_errors: list[str] = Field(default_factory=list)
    failures: list[FailureDiagnostic] = Field(default_factory=list)
    test_evidence: list[TestExecutionEvidence] = Field(default_factory=list)
    existing_regression_suite: SuiteExecutionSummary | None = None
    evidence_limitations: list[str] = Field(default_factory=list)
    error: AgentError | None = None

    @model_validator(mode="after")
    def _validate_modified_pass_truthfulness(self) -> ModifiedVerificationResult:
        if self.verification_passed:
            if self.verification_outcome != VerificationOutcome.PASSED:
                raise ValueError(
                    "ModifiedVerificationResult verification_passed=True requires verification_outcome=PASSED"
                )
            if self.execution_status != "completed":
                raise ValueError(
                    "ModifiedVerificationResult verification_passed=True requires execution_status='completed'"
                )
            if self.exit_code != 0:
                raise ValueError(
                    "ModifiedVerificationResult verification_passed=True requires exit_code == 0"
                )
            if self.collection_errors:
                raise ValueError(
                    "ModifiedVerificationResult verification_passed=True requires no collection_errors"
                )
        return self


class TestComparisonResult(BaseModel):
    """Deterministic baseline-vs-modified comparison outcome for a single test identity."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    test_identity: str = Field(..., min_length=1, max_length=300)
    scenario_id: str | None = Field(default=None, pattern=STABLE_ID_PATTERN.pattern)
    node_id: str | None = Field(default=None, max_length=500)
    relative_path: str | None = Field(default=None, max_length=500)
    suite_kind: TestSuiteKind = TestSuiteKind.GENERATED_ACCEPTANCE
    test_purpose: TestPurpose = TestPurpose.UNKNOWN
    criterion_ids: list[str] = Field(default_factory=list)
    approved_test_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    baseline_test_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    modified_test_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    test_hashes_match: bool | None = None
    baseline_outcome: IndividualTestOutcome = IndividualTestOutcome.UNAVAILABLE
    modified_outcome: IndividualTestOutcome = IndividualTestOutcome.UNAVAILABLE
    classification: TestComparisonClassification = (
        TestComparisonClassification.UNAVAILABLE
    )
    is_mandatory_regression: bool = False
    comparable: bool = False
    rationale: str = Field(default="", max_length=2000)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_classification_truthfulness(self) -> TestComparisonResult:
        if (
            self.baseline_outcome == IndividualTestOutcome.COLLECTION_ERROR
            or self.modified_outcome == IndividualTestOutcome.COLLECTION_ERROR
        ) and self.classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE:
            raise ValueError(
                "A collection error must never be classified as a newly introduced regression"
            )

        if self.classification == TestComparisonClassification.PRESERVED_BEHAVIOR:
            if (
                self.baseline_outcome != IndividualTestOutcome.PASSED
                or self.modified_outcome != IndividualTestOutcome.PASSED
                or self.test_hashes_match is not True
            ):
                raise ValueError(
                    "preserved_behavior requires baseline=PASSED, modified=PASSED, and matching test hashes"
                )
        elif self.classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE:
            if (
                self.baseline_outcome != IndividualTestOutcome.PASSED
                or self.modified_outcome != IndividualTestOutcome.FAILED
                or self.test_hashes_match is not True
            ):
                raise ValueError(
                    "newly_introduced_failure requires baseline=PASSED, modified=FAILED, and matching test hashes"
                )
        elif (
            self.classification
            == TestComparisonClassification.NEWLY_SATISFIED_ACCEPTANCE_CRITERION
        ):
            if (
                self.baseline_outcome != IndividualTestOutcome.FAILED
                or self.modified_outcome != IndividualTestOutcome.PASSED
                or self.test_hashes_match is not True
                or self.test_purpose != TestPurpose.NEW_REQUIREMENT_ACCEPTANCE
            ):
                raise ValueError(
                    "newly_satisfied_acceptance_criterion requires baseline=FAILED, modified=PASSED, "
                    "matching test hashes, and test_purpose='new_requirement_acceptance'"
                )
        elif self.classification == TestComparisonClassification.FIXED_EXISTING_FAILURE:
            if (
                self.baseline_outcome != IndividualTestOutcome.FAILED
                or self.modified_outcome != IndividualTestOutcome.PASSED
                or self.test_hashes_match is not True
                or self.test_purpose
                not in {
                    TestPurpose.EXISTING_REPOSITORY_REGRESSION,
                    TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                }
            ):
                raise ValueError(
                    "fixed_existing_failure requires baseline=FAILED, modified=PASSED, matching hashes, "
                    "and an existing regression or behavior preservation test purpose"
                )
        elif (
            self.classification
            == TestComparisonClassification.FAIL_TO_PASS_UNCLASSIFIED
        ):
            if (
                self.baseline_outcome != IndividualTestOutcome.FAILED
                or self.modified_outcome != IndividualTestOutcome.PASSED
                or self.test_hashes_match is not True
            ):
                raise ValueError(
                    "fail_to_pass_unclassified requires baseline=FAILED, modified=PASSED, and matching hashes"
                )
        elif self.classification == TestComparisonClassification.PERSISTENT_FAILURE:
            if (
                self.baseline_outcome != IndividualTestOutcome.FAILED
                or self.modified_outcome != IndividualTestOutcome.FAILED
                or self.test_hashes_match is not True
            ):
                raise ValueError(
                    "persistent_failure requires baseline=FAILED, modified=FAILED, and matching hashes"
                )

        if self.is_mandatory_regression and (
            self.classification != TestComparisonClassification.NEWLY_INTRODUCED_FAILURE
            or not self.comparable
        ):
            raise ValueError(
                "is_mandatory_regression=True is only valid for comparable newly_introduced_failure results"
            )
        return self


class RegressionComparisonSummary(BaseModel):
    """Aggregate summary of deterministic baseline-vs-modified comparison outcomes."""

    model_config = ConfigDict(extra="forbid")

    comparison_status: VerificationComparisonStatus = (
        VerificationComparisonStatus.UNAVAILABLE
    )
    total_compared: int = Field(default=0, ge=0)
    comparable_count: int = Field(default=0, ge=0)
    preserved_behavior_count: int = Field(default=0, ge=0)
    newly_introduced_failure_count: int = Field(default=0, ge=0)
    newly_satisfied_acceptance_count: int = Field(default=0, ge=0)
    fixed_existing_failure_count: int = Field(default=0, ge=0)
    fail_to_pass_unclassified_count: int = Field(default=0, ge=0)
    persistent_failure_count: int = Field(default=0, ge=0)
    not_comparable_count: int = Field(default=0, ge=0)
    unknown_count: int = Field(default=0, ge=0)
    unavailable_count: int = Field(default=0, ge=0)
    has_confirmed_regression: bool = False
    blocks_ordinary_approval: bool = False
    regression_safety_established: bool = False
    newly_introduced_failures: list[str] = Field(default_factory=list)
    preserved_behaviors: list[str] = Field(default_factory=list)
    newly_satisfied_acceptance_criteria: list[str] = Field(default_factory=list)
    fixed_existing_failures: list[str] = Field(default_factory=list)
    fail_to_pass_unclassified: list[str] = Field(default_factory=list)
    persistent_failures: list[str] = Field(default_factory=list)
    unavailable_or_incomparable: list[str] = Field(default_factory=list)
    generated_suite_aggregate: dict[str, Any] = Field(default_factory=dict)
    existing_regression_suite_aggregate: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_summary_truthfulness(self) -> RegressionComparisonSummary:
        if self.has_confirmed_regression != (self.newly_introduced_failure_count > 0):
            raise ValueError(
                "has_confirmed_regression must match newly_introduced_failure_count > 0"
            )
        if self.has_confirmed_regression and not self.blocks_ordinary_approval:
            raise ValueError(
                "Confirmed newly introduced regression must set blocks_ordinary_approval=True"
            )
        if self.regression_safety_established:
            if self.comparison_status != VerificationComparisonStatus.COMPLETED:
                raise ValueError(
                    "regression_safety_established=True requires comparison_status='completed'"
                )
            if self.has_confirmed_regression or self.newly_introduced_failure_count > 0:
                raise ValueError(
                    "regression_safety_established=True is forbidden when regressions exist"
                )
            if (
                self.comparable_count <= 0
                or self.not_comparable_count > 0
                or self.unknown_count > 0
                or self.unavailable_count > 0
            ):
                raise ValueError(
                    "regression_safety_established=True requires complete comparable execution evidence"
                )
        return self


class BaselineComparisonEvidence(BaseModel):
    """Baseline-versus-modified verification evidence and deterministic comparison.

    Preserves all pre-P0-7 fields (``status``, ``baseline_ref``,
    ``baseline_passed``, ``baseline_failed``, ``modified_passed``,
    ``modified_failed``, ``newly_failing_tests``, ``notes``) for backward
    compatibility with previously persisted records while adding itemized
    per-test comparison results, snapshot/test SHA-256 provenance, and
    explicit limitation disclosures.
    """

    model_config = ConfigDict(extra="forbid")

    status: VerificationComparisonStatus = VerificationComparisonStatus.UNAVAILABLE
    baseline_ref: str | None = None
    baseline_commit: str | None = None
    baseline_source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    modified_source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    approved_test_hashes_verified: bool = False
    baseline_passed: int | None = Field(default=None, ge=0)
    baseline_failed: int | None = Field(default=None, ge=0)
    modified_passed: int | None = Field(default=None, ge=0)
    modified_failed: int | None = Field(default=None, ge=0)
    newly_failing_tests: list[str] | None = None
    baseline_result: BaselineVerificationResult | None = None
    modified_result: ModifiedVerificationResult | None = None
    test_comparisons: list[TestComparisonResult] = Field(default_factory=list)
    summary: RegressionComparisonSummary = Field(
        default_factory=RegressionComparisonSummary
    )
    evidence_limitations: list[str] = Field(default_factory=list)
    notes: str = (
        "Baseline-versus-modified comparison was not executed; running generated tests "
        "against the modified repository alone is not proof of regression safety."
    )
