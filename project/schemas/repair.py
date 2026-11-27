"""Validated Bounded Automated Patch Repair schemas for ChangeGuard AI (M02 / P0-8).

Design invariants:
- Distinguishes initial patch generation (``attempt_number=1``, ``attempt_type="initial"``)
  from bounded automated repair attempts (``attempt_number in {2, 3}``, ``attempt_type="repair"``).
- Enforces hard bounds: at most 2 automated repair attempts and at most 3 total patch
  proposals per workflow run.
- Disallows automatic repair for ``verify_existing_change`` workflows
  (``allow_for_verify_existing_change: Literal[False] = False``).
- Preserves complete chronological attempt history without overwriting earlier failed
  attempts or fabricating missing measurements.
- Tracks persisted repair budget consumption (including provider invocations that
  returned invalid or unavailable proposals) so retries can never become unbounded
  or silently reset across restarts.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import STABLE_ID_PATTERN, AgentError, utc_now
from .patch import PatchApplicationResult, PatchGenerationResult, PatchValidationResult
from .verification import (
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    FailureDiagnostic,
    ModifiedVerificationResult,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"

PatchAttemptType = Literal["initial", "repair"]
PatchAttemptOutcome = Literal[
    "passed",
    "failed_verification",
    "generation_failed",
    "generation_unavailable",
    "validation_failed",
    "application_failed",
    "impact_analysis_failed",
    "duplicate_patch_rejected",
    "test_modification_rejected",
    "scope_invalidated",
    "integrity_error",
    "interrupted",
]


class RepairTerminationReason(str, Enum):
    """Explicit, auditable reason why bounded automated repair did not run or stopped."""

    NOT_NEEDED = "not_needed"
    REPAIR_DISABLED = "repair_disabled"
    WORKFLOW_MODE_UNSUPPORTED = "workflow_mode_unsupported"
    REPAIR_SUCCEEDED = "repair_succeeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INELIGIBLE_FAILURE = "ineligible_failure"
    DUPLICATE_PATCH_DETECTED = "duplicate_patch_detected"
    NO_PROGRESS_DETECTED = "no_progress_detected"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_ERROR = "provider_error"
    PATCH_VALIDATION_FAILED = "patch_validation_failed"
    PATCH_APPLICATION_FAILED = "patch_application_failed"
    TEST_MODIFICATION_REJECTED = "test_modification_rejected"
    TEST_ASSUMPTIONS_INVALIDATED = "test_assumptions_invalidated"
    WORKSPACE_INTEGRITY_ERROR = "workspace_integrity_error"
    PROCESS_INTERRUPTED = "process_interrupted"


class RepairEligibilityCategory(str, Enum):
    """Deterministic classification produced by the conservative repair eligibility evaluator."""

    ELIGIBLE_APPLICATION_REGRESSION = "eligible_application_regression"
    ELIGIBLE_ACCEPTANCE_FAILURE = "eligible_acceptance_failure"
    NOT_NEEDED_VERIFICATION_PASSED = "not_needed_verification_passed"
    REPAIR_DISABLED = "repair_disabled"
    WORKFLOW_MODE_UNSUPPORTED = "workflow_mode_unsupported"
    PATCH_NOT_APPLIED = "patch_not_applied"
    UNRESOLVED_MANDATORY_AMBIGUITY = "unresolved_mandatory_ambiguity"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TEST_DEFECT_OR_SYNTAX_ERROR = "test_defect_or_syntax_error"
    COLLECTION_OR_IMPORT_ERROR = "collection_or_import_error"
    MISSING_DEPENDENCY = "missing_dependency"
    PROVIDER_AUTH_OR_RATE_LIMIT = "provider_auth_or_rate_limit"
    VERIFICATION_INFRASTRUCTURE_ERROR = "verification_infrastructure_error"
    UNAVAILABLE_OR_INCOMPARABLE_EVIDENCE = "unavailable_or_incomparable_evidence"
    WORKSPACE_OR_TEST_INTEGRITY_VIOLATION = "workspace_or_test_integrity_violation"
    UNATTRIBUTABLE_FAILURE = "unattributable_failure"
    PERSISTENT_PREEXISTING_FAILURE = "persistent_preexisting_failure"


class RepairPolicy(BaseModel):
    """Explicitly configurable bounded automated patch repair policy."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_repair_attempts: int = Field(default=2, ge=0, le=2)
    max_total_proposals: int = Field(default=3, ge=1, le=3)
    allow_for_verify_existing_change: Literal[False] = False
    patch_strategy: Literal["immutable_original_snapshot"] = (
        "immutable_original_snapshot"
    )
    stop_on_duplicate_patch: bool = True
    stop_on_no_progress: bool = True
    require_unchanged_approved_tests: bool = True
    reuse_validated_baseline: bool = True

    @model_validator(mode="before")
    @classmethod
    def _default_total_proposals(cls, data: Any) -> Any:
        if isinstance(data, dict) and "max_total_proposals" not in data:
            enabled = data.get("enabled", True)
            raw_max = data.get("max_repair_attempts", 2)
            if isinstance(enabled, bool) and isinstance(raw_max, int) and not isinstance(raw_max, bool):
                effective = raw_max if enabled else 0
                if 0 <= effective <= 2:
                    data = dict(data)
                    data["max_total_proposals"] = 1 + effective
        return data

    @property
    def effective_max_repair_attempts(self) -> int:
        """Return effective automated repair budget (0 when automatic repair is disabled)."""
        return self.max_repair_attempts if self.enabled else 0

    @model_validator(mode="after")
    def _enforce_policy_bounds(self) -> RepairPolicy:
        expected_total = 1 + self.effective_max_repair_attempts
        if self.max_total_proposals != expected_total:
            raise ValueError(
                f"max_total_proposals ({self.max_total_proposals}) must equal "
                f"1 + effective_max_repair_attempts ({expected_total})"
            )
        return self


class RepairEligibilityResult(BaseModel):
    """Conservative, deterministic decision on whether automated patch repair is permitted."""

    model_config = ConfigDict(extra="forbid")

    eligible: bool
    category: RepairEligibilityCategory
    decision_reason: str = Field(..., min_length=1, max_length=2000)
    blocking_reasons: list[str] = Field(default_factory=list)
    attributable_test_identities: list[str] = Field(default_factory=list)
    failed_criterion_ids: list[str] = Field(default_factory=list)
    confirmed_regression_identities: list[str] = Field(default_factory=list)
    failure_signature: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    next_attempt_number: int | None = Field(default=None, ge=2, le=3)
    remaining_repair_budget: int = Field(default=0, ge=0, le=2)
    evaluated_at: datetime = Field(default_factory=utc_now)

    @field_validator("decision_reason")
    @classmethod
    def _strip_decision_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("decision_reason must not be blank")
        return cleaned

    @field_validator("failed_criterion_ids")
    @classmethod
    def _validate_criterion_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in value:
            cid = raw.strip()
            if not STABLE_ID_PATTERN.match(cid):
                raise ValueError(
                    f"Invalid criterion_id '{raw}' in RepairEligibilityResult"
                )
            if cid not in seen:
                seen.add(cid)
                cleaned.append(cid)
        return cleaned

    @model_validator(mode="after")
    def _enforce_eligibility_truthfulness(self) -> RepairEligibilityResult:
        eligible_categories = {
            RepairEligibilityCategory.ELIGIBLE_APPLICATION_REGRESSION,
            RepairEligibilityCategory.ELIGIBLE_ACCEPTANCE_FAILURE,
        }
        if self.eligible:
            if self.category not in eligible_categories:
                raise ValueError(
                    f"eligible=True is only valid for application failure categories, got '{self.category.value}'"
                )
            if self.blocking_reasons:
                raise ValueError("eligible=True cannot carry blocking_reasons")
            if self.next_attempt_number is None:
                raise ValueError("eligible=True requires next_attempt_number (2 or 3)")
            if self.remaining_repair_budget <= 0:
                raise ValueError("eligible=True requires remaining_repair_budget >= 1")
            if not self.attributable_test_identities:
                raise ValueError(
                    "eligible=True requires at least one attributable failing test identity"
                )
        else:
            if self.category in eligible_categories:
                raise ValueError(
                    f"eligible=False cannot use eligible category '{self.category.value}'"
                )
            if self.next_attempt_number is not None:
                raise ValueError("eligible=False must set next_attempt_number=None")
        return self


class PatchAttemptRecord(BaseModel):
    """Complete immutable audit record for one initial or repair patch attempt."""

    model_config = ConfigDict(extra="forbid")

    attempt_number: int = Field(..., ge=1, le=3)
    attempt_type: PatchAttemptType
    repair_attempt_index: int = Field(default=0, ge=0, le=2)
    original_requirement: str = Field(..., min_length=1, max_length=10000)
    acceptance_criterion_ids: list[str] = Field(default_factory=list)
    previous_proposal_id: str | None = Field(
        default=None, pattern=STABLE_ID_PATTERN.pattern
    )
    previous_patch_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    base_snapshot_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    resulting_source_snapshot_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    resulting_git_diff_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )
    patch_generation: PatchGenerationResult | None = None
    patch_proposal_id: str | None = Field(
        default=None, pattern=STABLE_ID_PATTERN.pattern
    )
    patch_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    patch_validation: PatchValidationResult | None = None
    patch_application: PatchApplicationResult | None = None
    approved_test_ids: list[str] = Field(default_factory=list)
    approved_test_hashes: dict[str, str] = Field(default_factory=dict)
    baseline_reused: bool = False
    baseline_verification: BaselineVerificationResult | None = None
    modified_verification: ModifiedVerificationResult | None = None
    failure_diagnostics: list[FailureDiagnostic] = Field(default_factory=list)
    failure_signature: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    baseline_comparison: BaselineComparisonEvidence | None = None
    repair_eligibility: RepairEligibilityResult | None = None
    provider_invoked: bool = False
    consumed_repair_budget: bool = False
    is_deterministic_fixture: bool = False
    is_mock_output: bool = False
    model_label: str | None = Field(default=None, max_length=160)
    durations_ms: dict[str, int] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    outcome: PatchAttemptOutcome
    errors: list[AgentError] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("durations_ms")
    @classmethod
    def _validate_durations(cls, value: dict[str, int]) -> dict[str, int]:
        cleaned: dict[str, int] = {}
        for k, v in value.items():
            if v < 0:
                raise ValueError(f"Duration '{k}' must be non-negative")
            cleaned[k] = int(v)
        return cleaned

    @model_validator(mode="after")
    def _enforce_attempt_identity_invariants(self) -> PatchAttemptRecord:
        if self.attempt_number == 1:
            if self.attempt_type != "initial":
                raise ValueError("attempt_number=1 must have attempt_type='initial'")
            if self.repair_attempt_index != 0:
                raise ValueError("attempt_number=1 must have repair_attempt_index=0")
            if self.consumed_repair_budget:
                raise ValueError(
                    "Initial patch attempt (attempt_number=1) must not consume repair budget"
                )
        else:
            if self.attempt_type != "repair":
                raise ValueError("attempt_number >= 2 must have attempt_type='repair'")
            expected_idx = self.attempt_number - 1
            if self.repair_attempt_index != expected_idx:
                raise ValueError(
                    f"attempt_number={self.attempt_number} must have "
                    f"repair_attempt_index={expected_idx}"
                )
        return self


class RepairAttemptHistory(BaseModel):
    """Persisted, append-only bounded patch repair history and budget ledger."""

    model_config = ConfigDict(extra="forbid")

    policy: RepairPolicy = Field(default_factory=RepairPolicy)
    total_proposals_attempted: int = Field(default=0, ge=0, le=3)
    repair_attempts_consumed: int = Field(default=0, ge=0, le=2)
    current_attempt_number: int = Field(default=0, ge=0, le=3)
    remaining_repair_budget: int = Field(default=2, ge=0, le=2)
    attempts: list[PatchAttemptRecord] = Field(default_factory=list)
    seen_patch_hashes: list[str] = Field(default_factory=list)
    seen_failure_signatures: list[str] = Field(default_factory=list)
    last_eligibility: RepairEligibilityResult | None = None
    termination_reason: RepairTerminationReason | None = None
    termination_summary: str = Field(default="", max_length=2000)
    final_candidate_attempt_number: int | None = Field(default=None, ge=1, le=3)
    final_candidate_proposal_id: str | None = Field(
        default=None, pattern=STABLE_ID_PATTERN.pattern
    )
    final_candidate_patch_sha256: str | None = Field(
        default=None, pattern=_SHA256_PATTERN
    )

    @model_validator(mode="before")
    @classmethod
    def _default_remaining_budget(cls, data: Any) -> Any:
        if isinstance(data, dict) and "remaining_repair_budget" not in data:
            raw_policy = data.get("policy")
            if isinstance(raw_policy, RepairPolicy):
                eff = raw_policy.effective_max_repair_attempts
            elif isinstance(raw_policy, dict):
                enabled = raw_policy.get("enabled", True)
                max_rep = raw_policy.get("max_repair_attempts", 2)
                eff = max_rep if enabled else 0
            else:
                eff = 2
            consumed = data.get("repair_attempts_consumed", 0)
            if isinstance(eff, int) and isinstance(consumed, int):
                data = dict(data)
                data["remaining_repair_budget"] = max(0, eff - consumed)
        return data

    @model_validator(mode="after")
    def _enforce_budget_and_history_invariants(self) -> RepairAttemptHistory:
        eff_max = self.policy.effective_max_repair_attempts
        if self.repair_attempts_consumed > eff_max:
            raise ValueError(
                f"repair_attempts_consumed ({self.repair_attempts_consumed}) exceeds "
                f"effective_max_repair_attempts ({eff_max})"
            )
        expected_remaining = eff_max - self.repair_attempts_consumed
        if self.remaining_repair_budget != expected_remaining:
            raise ValueError(
                f"remaining_repair_budget ({self.remaining_repair_budget}) must equal "
                f"effective_max_repair_attempts - repair_attempts_consumed ({expected_remaining})"
            )
        if self.total_proposals_attempted > self.policy.max_total_proposals:
            raise ValueError(
                f"total_proposals_attempted ({self.total_proposals_attempted}) exceeds "
                f"policy.max_total_proposals ({self.policy.max_total_proposals})"
            )
        if len(self.attempts) > self.policy.max_total_proposals:
            raise ValueError(
                f"attempts count ({len(self.attempts)}) exceeds "
                f"policy.max_total_proposals ({self.policy.max_total_proposals})"
            )

        seen_nums: list[int] = []
        for rec in self.attempts:
            if seen_nums and rec.attempt_number <= seen_nums[-1]:
                raise ValueError(
                    "RepairAttemptHistory.attempts must have strictly increasing unique attempt_numbers"
                )
            seen_nums.append(rec.attempt_number)
        return self
