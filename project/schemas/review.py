"""Validated A6 Independent Reviewer and Evidence Reconciliation schemas (M02 / P0-9).

Design invariants:
- No production A6 LLM intelligence is implemented here; these schemas define the
  strict integration contract for the future teammate-owned A6 agent and
  controlled offline test fixtures.
- Never fabricates arbitrary confidence percentages, numerical severity scores,
  model provenance, or live AI evidence.
- Live-mode review results can never be labeled as deterministic fixtures or mock
  output, nor report ``availability='completed_offline_fixture'``.
- Every ``ReviewEvidenceReference`` can be deterministically verified against
  actual known run artifacts (criteria, constraints, assumptions, ambiguities,
  files, patch proposals/hashes, snapshot hashes, scenarios, tests, repair attempts).
- ``EvidenceReconciliationResult`` enforces truthfulness invariants at schema level:
  failed verification, confirmed regressions, blocking ambiguities, or candidate
  integrity failures can NEVER be overridden by reviewer support, and
  ``complete_requirement_assurance`` can NEVER be ``True`` when mandatory live
  evidence is missing or offline fixtures were used.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import STABLE_ID_PATTERN, AgentError, utc_now
from .requirements import (
    AcceptanceCriterion,
    BusinessConstraint,
    RequirementAmbiguity,
    RequirementAssumption,
    RequirementTraceability,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _strip_non_empty(value: str, *, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} must not be blank")
    return cleaned


def _validate_unique_id_list(values: list[str], *, field_name: str) -> list[str]:
    seen: set[str] = set()
    cleaned_list: list[str] = []
    for raw in values:
        item = raw.strip()
        if not STABLE_ID_PATTERN.match(item):
            raise ValueError(
                f"Invalid identifier '{raw}' in {field_name}: expected stable ID "
                "matching ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
            )
        if item in seen:
            raise ValueError(f"Duplicate identifier '{item}' in {field_name}")
        seen.add(item)
        cleaned_list.append(item)
    return cleaned_list


def _validate_clean_text_list(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in values:
        item = raw.strip()
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


class ReviewRecommendation(str, Enum):
    """Supported qualitative recommendations emitted by an A6 Independent Reviewer."""

    SUPPORT = "support"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class ReviewAvailabilityStatus(str, Enum):
    """Explicit availability and provenance status of independent review evidence."""

    COMPLETED = "completed"
    COMPLETED_OFFLINE_FIXTURE = "completed_offline_fixture"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    SKIPPED = "skipped"


class ReviewFindingSeverity(str, Enum):
    """Qualitative finding severity without fabricated numerical severity scores."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReviewFindingCategory(str, Enum):
    """Structured review finding classification."""

    REQUIREMENT_COMPLIANCE = "requirement_compliance"
    BEHAVIORAL_REGRESSION = "behavioral_regression"
    SECURITY = "security"
    MAINTAINABILITY = "maintainability"
    MISSING_VERIFICATION_EVIDENCE = "missing_verification_evidence"
    UNSUPPORTED_ASSUMPTION = "unsupported_assumption"
    EVIDENCE_CONFLICT = "evidence_conflict"
    OTHER = "other"


ReviewEvidenceReferenceType = Literal[
    "criterion",
    "constraint",
    "assumption",
    "ambiguity",
    "file",
    "patch_proposal",
    "patch_sha256",
    "snapshot_sha256",
    "scenario",
    "test_identity",
    "repair_attempt",
    "baseline_comparison",
    "verification_outcome",
]


class ReviewEvidenceReference(BaseModel):
    """Typed citation linking a review finding or conclusion to a known run artifact."""

    model_config = ConfigDict(extra="forbid")

    reference_type: ReviewEvidenceReferenceType
    reference_id: str = Field(..., min_length=1, max_length=300)
    description: str = Field(default="", max_length=1000)
    verified: bool | None = None

    @field_validator("reference_id")
    @classmethod
    def _validate_reference_id(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="reference_id").replace("\\", "/")

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str) -> str:
        return value.strip()


class ReviewFinding(BaseModel):
    """Single structured concern or observation raised by an A6 Independent Reviewer."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    category: ReviewFindingCategory
    severity: ReviewFindingSeverity
    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field(..., min_length=1, max_length=2000)
    is_material: bool = False
    related_criterion_ids: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    evidence_references: list[ReviewEvidenceReference] = Field(default_factory=list)
    substantiated: bool | None = None
    reconciliation_note: str = Field(default="", max_length=1000)

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="title")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="description")

    @field_validator("related_criterion_ids")
    @classmethod
    def _validate_criterion_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="related_criterion_ids")

    @field_validator("affected_files")
    @classmethod
    def _validate_affected_files(cls, value: list[str]) -> list[str]:
        normalized = [p.strip().replace("\\", "/") for p in value if p and p.strip()]
        seen: set[str] = set()
        result: list[str] = []
        for item in normalized:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    @model_validator(mode="after")
    def _enforce_material_severity_consistency(self) -> ReviewFinding:
        if self.severity in {ReviewFindingSeverity.CRITICAL, ReviewFindingSeverity.HIGH}:
            self.is_material = True
        return self


class ReviewModelMetadata(BaseModel):
    """Non-fabricated provider and model provenance for an independent review call."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=160)
    model_label: str = Field(default="unconfigured", min_length=1, max_length=160)
    execution_mode: Literal["offline_test", "live_llm"] = "offline_test"
    is_deterministic_fixture: bool = False
    is_mock_output: bool = False
    token_usage: dict[str, int] | None = None

    @field_validator("model_label")
    @classmethod
    def _validate_model_label(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="model_label")

    @model_validator(mode="after")
    def _validate_live_mode_honesty(self) -> ReviewModelMetadata:
        if self.execution_mode == "live_llm" and (
            self.is_deterministic_fixture or self.is_mock_output
        ):
            raise ValueError(
                "Live LLM independent review metadata cannot declare "
                "is_deterministic_fixture=True or is_mock_output=True"
            )
        return self


class IndependentReviewRequest(BaseModel):
    """Typed A6 input contract containing bounded, frozen candidate evidence only.

    A6 never receives host filesystem paths, credentials, or mutable workspace
    handles and cannot modify source files, tests, or workflow state.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=120)
    workflow_mode: Literal["verify_existing_change", "generate_and_verify_change"] = (
        "verify_existing_change"
    )
    execution_mode: Literal["offline_test", "live_llm"] = "offline_test"
    original_requirement: str = Field(..., min_length=1, max_length=10000)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    business_constraints: list[BusinessConstraint] = Field(default_factory=list)
    unresolved_assumptions: list[RequirementAssumption] = Field(default_factory=list)
    unresolved_ambiguities: list[RequirementAmbiguity] = Field(default_factory=list)
    candidate_proposal_id: str | None = Field(
        default=None, pattern=STABLE_ID_PATTERN.pattern
    )
    candidate_patch_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    candidate_attempt_number: int | None = Field(default=None, ge=1, le=3)
    original_source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    baseline_source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    modified_source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    modified_git_diff_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    candidate_unified_diff: str | None = Field(default=None, max_length=200_000)
    relevant_source_excerpts: dict[str, str] = Field(default_factory=dict)
    relevant_source_file_paths: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    impacted_modules: list[str] = Field(default_factory=list)
    impact_summary: list[dict[str, Any]] = Field(default_factory=list)
    approved_test_ids: list[str] = Field(default_factory=list)
    approved_test_hashes: dict[str, str] = Field(default_factory=dict)
    verification_summary: dict[str, Any] = Field(default_factory=dict)
    baseline_comparison_summary: dict[str, Any] = Field(default_factory=dict)
    requirement_traceability: list[RequirementTraceability] = Field(
        default_factory=list
    )
    repair_attempt_summary: list[dict[str, Any]] = Field(default_factory=list)
    repair_termination_reason: str | None = None
    existing_evidence_limitations: list[str] = Field(default_factory=list)
    known_artifact_ids: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("original_requirement")
    @classmethod
    def _validate_original_requirement(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="original_requirement")

    @model_validator(mode="after")
    def _sync_relevant_paths(self) -> IndependentReviewRequest:
        if self.relevant_source_excerpts and not self.relevant_source_file_paths:
            self.relevant_source_file_paths = sorted(
                k.replace("\\", "/") for k in self.relevant_source_excerpts
            )
        return self


class IndependentReviewResult(BaseModel):
    """Normalized, validated output returned by ``IndependentReviewAdapter`` (A6)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "unavailable", "invalid", "error"]
    availability: ReviewAvailabilityStatus
    recommendation: ReviewRecommendation | None = None
    reviewed_run_id: str = Field(..., min_length=1, max_length=120)
    reviewed_candidate_proposal_id: str | None = Field(
        default=None, pattern=STABLE_ID_PATTERN.pattern
    )
    reviewed_candidate_patch_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    reviewed_baseline_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    reviewed_modified_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    summary: str = Field(default="", max_length=4000)
    findings: list[ReviewFinding] = Field(default_factory=list)
    requirement_compliance_concerns: list[str] = Field(default_factory=list)
    potential_behavioral_regressions: list[str] = Field(default_factory=list)
    security_and_maintainability_concerns: list[str] = Field(default_factory=list)
    missing_verification_evidence: list[str] = Field(default_factory=list)
    unsupported_assumptions: list[str] = Field(default_factory=list)
    evidence_references: list[ReviewEvidenceReference] = Field(default_factory=list)
    model_metadata: ReviewModelMetadata = Field(default_factory=ReviewModelMetadata)
    is_deterministic_fixture: bool = False
    is_mock_output: bool = False
    reviewed_at: datetime = Field(default_factory=utc_now)
    error: AgentError | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator(
        "requirement_compliance_concerns",
        "potential_behavioral_regressions",
        "security_and_maintainability_concerns",
        "missing_verification_evidence",
        "unsupported_assumptions",
        "notes",
    )
    @classmethod
    def _clean_text_lists(cls, value: list[str]) -> list[str]:
        return _validate_clean_text_list(value)

    @model_validator(mode="after")
    def _enforce_review_result_invariants(self) -> IndependentReviewResult:
        seen_finding_ids: set[str] = set()
        for finding in self.findings:
            if finding.finding_id in seen_finding_ids:
                raise ValueError(
                    f"Duplicate finding_id in IndependentReviewResult: '{finding.finding_id}'"
                )
            seen_finding_ids.add(finding.finding_id)

        if self.is_deterministic_fixture != self.model_metadata.is_deterministic_fixture:
            raise ValueError(
                "is_deterministic_fixture must match model_metadata.is_deterministic_fixture"
            )
        if self.is_mock_output != self.model_metadata.is_mock_output:
            raise ValueError(
                "is_mock_output must match model_metadata.is_mock_output"
            )
        if self.model_metadata.execution_mode == "live_llm" and (
            self.is_deterministic_fixture
            or self.is_mock_output
            or self.availability == ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE
        ):
            raise ValueError(
                "Live LLM independent review cannot be labeled as a deterministic fixture or mock output"
            )

        if self.status == "completed":
            if self.recommendation is None:
                raise ValueError(
                    "IndependentReviewResult with status='completed' requires a recommendation"
                )
            if self.error is not None:
                raise ValueError(
                    "IndependentReviewResult with status='completed' must not carry an error"
                )
            expected_avail = (
                ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE
                if (self.is_deterministic_fixture or self.is_mock_output)
                else ReviewAvailabilityStatus.COMPLETED
            )
            if self.availability != expected_avail:
                raise ValueError(
                    f"IndependentReviewResult with status='completed' and "
                    f"is_deterministic_fixture={self.is_deterministic_fixture} must set "
                    f"availability='{expected_avail.value}'"
                )
        else:
            if self.recommendation is not None:
                raise ValueError(
                    f"IndependentReviewResult with status='{self.status}' must set recommendation=None"
                )
            if self.error is None:
                raise ValueError(
                    f"IndependentReviewResult with status='{self.status}' requires a structured error"
                )
            if self.status == "unavailable" and self.availability != ReviewAvailabilityStatus.UNAVAILABLE:
                raise ValueError(
                    "IndependentReviewResult with status='unavailable' requires availability='unavailable'"
                )
            if (
                self.status in {"invalid", "error"}
                and self.availability != ReviewAvailabilityStatus.FAILED
            ):
                raise ValueError(
                    f"IndependentReviewResult with status='{self.status}' requires availability='failed'"
                )
        return self


class EvidenceReconciliationResult(BaseModel):
    """Deterministic reconciliation between verification, traceability, repair, and A6 review."""

    model_config = ConfigDict(extra="forbid")

    reconciliation_status: Literal[
        "reconciled_aligned",
        "reconciled_with_disagreement",
        "reconciled_reviewer_unavailable",
        "reconciled_reviewer_invalid_or_stale",
        "reconciled_deterministic_block",
    ]
    assurance_classification: Literal[
        "fully_supported_requirement_assurance",
        "reviewer_supported_verification",
        "incomplete_evidence_package",
        "reviewer_disagreement",
        "reviewer_unavailable",
        "deterministic_verification_blocked",
    ]
    deterministic_verification_passed: bool
    has_confirmed_regression: bool = False
    has_blocking_ambiguity: bool = False
    candidate_integrity_verified: bool = True
    review_candidate_identity_verified: bool = False
    review_availability: ReviewAvailabilityStatus = ReviewAvailabilityStatus.UNAVAILABLE
    reviewer_recommendation: ReviewRecommendation | None = None
    reviewer_is_deterministic_fixture: bool = False
    material_disagreements: list[str] = Field(default_factory=list)
    unsubstantiated_claims: list[str] = Field(default_factory=list)
    invalid_evidence_references: list[str] = Field(default_factory=list)
    approval_restrictions: list[str] = Field(default_factory=list)
    blocks_ordinary_approval: bool = False
    blocking_error_code: str | None = Field(default=None, max_length=100)
    complete_requirement_assurance: bool = False
    evidence_package_complete: bool = False
    reconciled_summary: str = Field(default="", max_length=4000)
    reconciled_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _enforce_reconciliation_truthfulness(self) -> EvidenceReconciliationResult:
        deterministic_block = (
            not self.deterministic_verification_passed
            or self.has_confirmed_regression
            or self.has_blocking_ambiguity
            or not self.candidate_integrity_verified
        )
        if deterministic_block:
            if not self.blocks_ordinary_approval:
                raise ValueError(
                    "Deterministic verification failure, confirmed regression, blocking ambiguity, "
                    "or candidate integrity failure must set blocks_ordinary_approval=True"
                )
            if self.complete_requirement_assurance:
                raise ValueError(
                    "complete_requirement_assurance cannot be True under a deterministic block"
                )

        if self.complete_requirement_assurance:
            if self.blocks_ordinary_approval:
                raise ValueError(
                    "complete_requirement_assurance cannot be True when ordinary approval is blocked"
                )
            if self.review_availability != ReviewAvailabilityStatus.COMPLETED:
                raise ValueError(
                    "complete_requirement_assurance=True requires live completed independent review "
                    "(offline fixtures or unavailable review cannot establish complete assurance)"
                )
            if self.reviewer_is_deterministic_fixture:
                raise ValueError(
                    "complete_requirement_assurance=True is forbidden when reviewer is an offline fixture"
                )
            if self.reviewer_recommendation != ReviewRecommendation.SUPPORT:
                raise ValueError(
                    "complete_requirement_assurance=True requires reviewer recommendation='support'"
                )
            if not self.review_candidate_identity_verified:
                raise ValueError(
                    "complete_requirement_assurance=True requires verified candidate review identity"
                )
            if self.material_disagreements:
                raise ValueError(
                    "complete_requirement_assurance=True is forbidden while material disagreements exist"
                )

        return self
