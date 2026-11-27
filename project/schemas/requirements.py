"""Structured Requirement Analysis and Traceability schemas for ChangeGuard AI (A1 / M02).

Design invariants:
- Explicit requirements are strictly distinguished from inferred assumptions and
  unresolved ambiguities via ``origin`` (``explicit`` vs ``inferred``).
- Duplicate IDs (criteria, constraints, assumptions, ambiguities), empty criteria
  lists on non-error results, and dangling cross-references are rejected explicitly.
- Mandatory unresolved ambiguities can never be silently marked ``completed`` or
  ``verified``.
- Requirement-to-test traceability can never mark a criterion ``verified`` without
  explicit scenario mapping, human approval, execution evidence, and zero
  unresolved ambiguities.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import STABLE_ID_PATTERN, AgentError


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


def _validate_unique_text_list(values: list[str], *, field_name: str) -> list[str]:
    seen: set[str] = set()
    cleaned_list: list[str] = []
    for raw in values:
        item = raw.strip()
        if not item:
            raise ValueError(f"{field_name} must not contain blank entries")
        if item in seen:
            raise ValueError(f"Duplicate entry '{item}' in {field_name}")
        seen.add(item)
        cleaned_list.append(item)
    return cleaned_list


class CriterionOrigin(str, Enum):
    """Distinguishes directly stated requirements from model-inferred items."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class CriterionPriority(str, Enum):
    """Requirement priority level."""

    MUST = "must"
    SHOULD = "should"
    COULD = "could"


class VerificationStrategy(str, Enum):
    """Expected verification method for an acceptance criterion."""

    UNIT_TEST = "unit_test"
    INTEGRATION_TEST = "integration_test"
    REGRESSION_TEST = "regression_test"
    MANUAL_REVIEW = "manual_review"


class AmbiguitySeverity(str, Enum):
    """Severity indicating whether an ambiguity blocks verification sign-off."""

    BLOCKING = "blocking"
    NON_BLOCKING = "non_blocking"


class TraceabilityEvidenceStatus(str, Enum):
    """Truthful verification status for an individual acceptance criterion."""

    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    NEEDS_CLARIFICATION = "needs_clarification"


class BusinessConstraint(BaseModel):
    """Structured business rule or existing-behavior preservation constraint."""

    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    description: str = Field(..., min_length=1, max_length=2000)
    category: Literal[
        "functional_rule",
        "behavior_preservation",
        "boundary",
        "non_functional",
        "compliance",
        "other",
    ] = "functional_rule"
    origin: CriterionOrigin = CriterionOrigin.EXPLICIT
    preserves_existing_behavior: bool = False
    source_reference: str = Field(..., min_length=1, max_length=500)
    related_criterion_ids: list[str] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="description")

    @field_validator("source_reference")
    @classmethod
    def _validate_source_reference(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="source_reference")

    @field_validator("related_criterion_ids")
    @classmethod
    def _validate_related_criteria(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="related_criterion_ids")


class RequirementAssumption(BaseModel):
    """Inferred assumption explicitly separated from stated requirements."""

    model_config = ConfigDict(extra="forbid")

    assumption_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    description: str = Field(..., min_length=1, max_length=2000)
    rationale: str = Field(..., min_length=1, max_length=2000)
    requires_human_confirmation: bool = True
    related_criterion_ids: list[str] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="description")

    @field_validator("rationale")
    @classmethod
    def _validate_rationale(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="rationale")

    @field_validator("related_criterion_ids")
    @classmethod
    def _validate_related_criteria(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="related_criterion_ids")


class RequirementAmbiguity(BaseModel):
    """Unresolved or resolved ambiguity identified during requirement analysis."""

    model_config = ConfigDict(extra="forbid")

    ambiguity_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    description: str = Field(..., min_length=1, max_length=2000)
    clarification_question: str = Field(..., min_length=1, max_length=1000)
    severity: AmbiguitySeverity = AmbiguitySeverity.NON_BLOCKING
    mandatory_to_resolve: bool = False
    resolved: bool = False
    resolution_notes: str | None = Field(default=None, max_length=2000)
    related_criterion_ids: list[str] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="description")

    @field_validator("clarification_question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="clarification_question")

    @field_validator("resolution_notes")
    @classmethod
    def _strip_resolution_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("related_criterion_ids")
    @classmethod
    def _validate_related_criteria(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="related_criterion_ids")

    @model_validator(mode="after")
    def _validate_ambiguity_consistency(self) -> RequirementAmbiguity:
        if self.severity == AmbiguitySeverity.BLOCKING and not self.mandatory_to_resolve:
            raise ValueError(
                f"Blocking ambiguity '{self.ambiguity_id}' must set mandatory_to_resolve=True"
            )
        if self.resolved and not self.resolution_notes:
            raise ValueError(
                f"Resolved ambiguity '{self.ambiguity_id}' requires non-empty resolution_notes"
            )
        return self


class AcceptanceCriterion(BaseModel):
    """Single verifiable, traceable acceptance criterion extracted by A1."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    requirement_reference: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=2000)
    priority: CriterionPriority = CriterionPriority.MUST
    mandatory: bool = True
    expected_behavior: str = Field(..., min_length=1, max_length=2000)
    verification_strategy: VerificationStrategy = VerificationStrategy.UNIT_TEST
    origin: CriterionOrigin = CriterionOrigin.EXPLICIT
    is_edge_case: bool = False
    preserves_existing_behavior: bool = False
    target_modules: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    ambiguity_ids: list[str] = Field(default_factory=list)

    @field_validator("requirement_reference")
    @classmethod
    def _validate_requirement_reference(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="requirement_reference")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="description")

    @field_validator("expected_behavior")
    @classmethod
    def _validate_expected_behavior(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="expected_behavior")

    @field_validator("target_modules")
    @classmethod
    def _validate_target_modules(cls, value: list[str]) -> list[str]:
        return _validate_unique_text_list(value, field_name="target_modules")

    @field_validator("assumption_ids")
    @classmethod
    def _validate_assumption_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="assumption_ids")

    @field_validator("ambiguity_ids")
    @classmethod
    def _validate_ambiguity_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="ambiguity_ids")

    @model_validator(mode="after")
    def _validate_priority_mandatory_consistency(self) -> AcceptanceCriterion:
        if self.priority == CriterionPriority.MUST and not self.mandatory:
            raise ValueError(
                f"Criterion '{self.criterion_id}' with priority='must' must have mandatory=True"
            )
        return self


class RequirementAnalysisResult(BaseModel):
    """Normalized, validated output returned by the A1 Requirement Analyst Agent."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "needs_clarification", "error"]
    analysis_mode: Literal["offline_test", "live_llm"] = "offline_test"
    is_deterministic_fixture: bool = True
    is_mock_output: bool = True
    model_label: str = Field(
        default="deterministic_offline_requirement_analyst",
        min_length=1,
        max_length=160,
    )
    original_requirement: str = Field(..., min_length=1, max_length=10000)
    requirement_summary: str = Field(default="", max_length=2000)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    business_constraints: list[BusinessConstraint] = Field(default_factory=list)
    assumptions: list[RequirementAssumption] = Field(default_factory=list)
    ambiguities: list[RequirementAmbiguity] = Field(default_factory=list)
    preserved_behaviors: list[str] = Field(default_factory=list)
    edge_cases: list[str] = Field(default_factory=list)
    has_mandatory_unresolved_ambiguities: bool = False
    error: AgentError | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator("original_requirement")
    @classmethod
    def _validate_original_requirement(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="original_requirement")

    @field_validator("requirement_summary")
    @classmethod
    def _strip_summary(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_integrity_and_references(self) -> RequirementAnalysisResult:
        # 1. Enforce unique IDs across every collection
        criterion_ids: set[str] = set()
        for criterion in self.acceptance_criteria:
            if criterion.criterion_id in criterion_ids:
                raise ValueError(
                    f"Duplicate acceptance criterion_id: '{criterion.criterion_id}'"
                )
            criterion_ids.add(criterion.criterion_id)

        constraint_ids: set[str] = set()
        for constraint in self.business_constraints:
            if constraint.constraint_id in constraint_ids:
                raise ValueError(
                    f"Duplicate business constraint_id: '{constraint.constraint_id}'"
                )
            constraint_ids.add(constraint.constraint_id)

        assumption_ids: set[str] = set()
        for assumption in self.assumptions:
            if assumption.assumption_id in assumption_ids:
                raise ValueError(
                    f"Duplicate requirement assumption_id: '{assumption.assumption_id}'"
                )
            assumption_ids.add(assumption.assumption_id)

        ambiguity_ids: set[str] = set()
        for ambiguity in self.ambiguities:
            if ambiguity.ambiguity_id in ambiguity_ids:
                raise ValueError(
                    f"Duplicate requirement ambiguity_id: '{ambiguity.ambiguity_id}'"
                )
            ambiguity_ids.add(ambiguity.ambiguity_id)

        # 2. Enforce non-empty criteria for successful/clarification analyses & error on error status
        if self.status in {"completed", "needs_clarification"} and not self.acceptance_criteria:
            raise ValueError(
                f"RequirementAnalysisResult with status='{self.status}' requires at least one acceptance criterion"
            )
        if self.status == "error" and self.error is None:
            raise ValueError(
                "RequirementAnalysisResult with status='error' must include a structured error"
            )

        # 3. Validate all cross-references strictly
        for criterion in self.acceptance_criteria:
            unknown_assumptions = [
                aid for aid in criterion.assumption_ids if aid not in assumption_ids
            ]
            if unknown_assumptions:
                raise ValueError(
                    f"Criterion '{criterion.criterion_id}' references unknown assumption_ids: "
                    f"{unknown_assumptions}"
                )
            unknown_ambiguities = [
                amb_id for amb_id in criterion.ambiguity_ids if amb_id not in ambiguity_ids
            ]
            if unknown_ambiguities:
                raise ValueError(
                    f"Criterion '{criterion.criterion_id}' references unknown ambiguity_ids: "
                    f"{unknown_ambiguities}"
                )

        for constraint in self.business_constraints:
            unknown_criteria = [
                cid for cid in constraint.related_criterion_ids if cid not in criterion_ids
            ]
            if unknown_criteria:
                raise ValueError(
                    f"BusinessConstraint '{constraint.constraint_id}' references unknown "
                    f"criterion_ids: {unknown_criteria}"
                )

        for assumption in self.assumptions:
            unknown_criteria = [
                cid for cid in assumption.related_criterion_ids if cid not in criterion_ids
            ]
            if unknown_criteria:
                raise ValueError(
                    f"RequirementAssumption '{assumption.assumption_id}' references unknown "
                    f"criterion_ids: {unknown_criteria}"
                )

        for ambiguity in self.ambiguities:
            unknown_criteria = [
                cid for cid in ambiguity.related_criterion_ids if cid not in criterion_ids
            ]
            if unknown_criteria:
                raise ValueError(
                    f"RequirementAmbiguity '{ambiguity.ambiguity_id}' references unknown "
                    f"criterion_ids: {unknown_criteria}"
                )

        # 4. Enforce truthfulness around mandatory unresolved ambiguities
        actual_mandatory_unresolved = any(
            (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
            and not amb.resolved
            for amb in self.ambiguities
        )
        if self.has_mandatory_unresolved_ambiguities != actual_mandatory_unresolved:
            raise ValueError(
                "has_mandatory_unresolved_ambiguities must accurately reflect whether "
                "unresolved mandatory/blocking ambiguities exist"
            )
        if actual_mandatory_unresolved and self.status == "completed":
            raise ValueError(
                "RequirementAnalysisResult cannot have status='completed' while "
                "mandatory unresolved ambiguities exist; use status='needs_clarification'"
            )

        # 5. Live LLM results must never be labeled as deterministic fixtures
        if self.analysis_mode == "live_llm" and self.is_deterministic_fixture:
            raise ValueError(
                "Live LLM requirement analysis cannot set is_deterministic_fixture=True"
            )

        return self


class RequirementTraceability(BaseModel):
    """Per-criterion traceability linking requirement -> files -> tests -> evidence."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    requirement_reference: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1, max_length=2000)
    origin: CriterionOrigin = CriterionOrigin.EXPLICIT
    mandatory: bool = True
    affected_source_files: list[str] = Field(default_factory=list)
    file_mapping_status: Literal["explicit", "repository_scope_only", "unavailable"] = (
        "unavailable"
    )
    generated_scenario_ids: list[str] = Field(default_factory=list)
    approved_test_ids: list[str] = Field(default_factory=list)
    executed_test_ids: list[str] = Field(default_factory=list)
    mapping_status: Literal["explicit", "unavailable"] = "unavailable"
    execution_evidence_summary: str = Field(
        default="No structured criterion-to-test execution evidence available.",
        max_length=2000,
    )
    verification_status: TraceabilityEvidenceStatus = TraceabilityEvidenceStatus.UNVERIFIED
    unresolved_ambiguity_ids: list[str] = Field(default_factory=list)
    assumption_ids: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2000)

    @field_validator("requirement_reference")
    @classmethod
    def _validate_requirement_reference(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="requirement_reference")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="description")

    @field_validator("affected_source_files")
    @classmethod
    def _validate_affected_files(cls, value: list[str]) -> list[str]:
        return _validate_unique_text_list(value, field_name="affected_source_files")

    @field_validator("generated_scenario_ids")
    @classmethod
    def _validate_generated_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="generated_scenario_ids")

    @field_validator("approved_test_ids")
    @classmethod
    def _validate_approved_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="approved_test_ids")

    @field_validator("executed_test_ids")
    @classmethod
    def _validate_executed_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="executed_test_ids")

    @field_validator("unresolved_ambiguity_ids")
    @classmethod
    def _validate_ambiguity_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="unresolved_ambiguity_ids")

    @field_validator("assumption_ids")
    @classmethod
    def _validate_assumption_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="assumption_ids")

    @model_validator(mode="after")
    def _enforce_traceability_truthfulness(self) -> RequirementTraceability:
        generated_set = set(self.generated_scenario_ids)
        approved_set = set(self.approved_test_ids)
        executed_set = set(self.executed_test_ids)

        if not approved_set.issubset(generated_set):
            invalid = sorted(approved_set - generated_set)
            raise ValueError(
                f"Criterion '{self.criterion_id}' approved_test_ids not present in "
                f"generated_scenario_ids: {invalid}"
            )
        if not executed_set.issubset(approved_set):
            invalid = sorted(executed_set - approved_set)
            raise ValueError(
                f"Criterion '{self.criterion_id}' executed_test_ids not present in "
                f"approved_test_ids: {invalid}"
            )

        if self.mapping_status == "unavailable" and self.generated_scenario_ids:
            raise ValueError(
                f"Criterion '{self.criterion_id}' cannot declare mapping_status='unavailable' "
                "while listing generated_scenario_ids"
            )
        if self.mapping_status == "explicit" and not self.generated_scenario_ids:
            raise ValueError(
                f"Criterion '{self.criterion_id}' with mapping_status='explicit' requires "
                "at least one generated_scenario_id"
            )

        if self.verification_status == TraceabilityEvidenceStatus.VERIFIED:
            if self.unresolved_ambiguity_ids:
                raise ValueError(
                    f"Criterion '{self.criterion_id}' cannot be marked 'verified' while "
                    f"unresolved ambiguities exist: {self.unresolved_ambiguity_ids}"
                )
            if self.mapping_status != "explicit":
                raise ValueError(
                    f"Criterion '{self.criterion_id}' cannot be marked 'verified' without "
                    "explicit criterion-to-test mapping"
                )
            if not self.executed_test_ids:
                raise ValueError(
                    f"Criterion '{self.criterion_id}' cannot be marked 'verified' without "
                    "approved and executed test evidence"
                )

        return self
