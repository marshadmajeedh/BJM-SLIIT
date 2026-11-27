"""Validated A4 Patch Generation Integration schemas for ChangeGuard AI (M02).

Design invariants:
- No production LLM patch generation is implemented here; these schemas define
  the strict integration contract for the future teammate-owned A4 agent and
  controlled offline test fixtures.
- Model metadata, fixture/mock indicators, source mappings, and verification
  evidence are never fabricated.
- Live-mode results can never be labeled as deterministic fixtures or mock
  output.
- Every ``PatchProposal`` enforces SHA-256 integrity matching ``unified_diff``.
- Every ``PatchValidationResult`` and ``PatchApplicationResult`` enforces
  truthful status/error/flag consistency.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import STABLE_ID_PATTERN, AgentError, utc_now
from .requirements import (
    AcceptanceCriterion,
    BusinessConstraint,
    RequirementAmbiguity,
)


def compute_patch_sha256(unified_diff: str) -> str:
    """Return the deterministic SHA-256 hex digest of a UTF-8 unified diff."""
    return hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()


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


class PreChangeFileSummary(BaseModel):
    """Static AST summary of a single indexed Python file before patch generation."""

    model_config = ConfigDict(extra="forbid")

    repo_relative_path: str = Field(..., min_length=1, max_length=500)
    source_relative_path: str = Field(..., min_length=1, max_length=500)
    functions: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    parse_error: str | None = Field(default=None, max_length=1000)


class PreChangeCodebaseContext(BaseModel):
    """Explicitly scoped pre-change repository context available before A4 runs.

    M01's current implementation is diff-driven for reverse-dependency impact
    analysis; therefore pre-change context provides static AST indexing and
    bounded source file discovery only, and explicitly declares
    ``pre_change_impact_prediction_available=False``.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "unavailable", "error"]
    scope: Literal["static_repository_index_only", "unavailable"] = (
        "static_repository_index_only"
    )
    pre_change_impact_prediction_available: Literal[False] = False
    source_path: str = Field(..., min_length=1, max_length=500)
    base_ref: str = Field(..., min_length=1, max_length=200)
    resolved_base_commit: str | None = None
    total_indexed_files: int = Field(default=0, ge=0)
    indexed_file_paths: list[str] = Field(default_factory=list)
    indexed_files: list[PreChangeFileSummary] = Field(default_factory=list)
    relevant_file_paths: list[str] = Field(default_factory=list)
    limitation_notes: list[str] = Field(default_factory=list)
    error: AgentError | None = None

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> PreChangeCodebaseContext:
        if self.status in {"unavailable", "error"} and self.error is None:
            raise ValueError(
                f"PreChangeCodebaseContext with status='{self.status}' requires a structured error"
            )
        if self.status == "completed" and self.total_indexed_files <= 0:
            raise ValueError(
                "PreChangeCodebaseContext with status='completed' requires at least one indexed file"
            )
        return self


class PatchModelMetadata(BaseModel):
    """Non-fabricated provider and model provenance for a patch generation call."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=160)
    model_label: str = Field(default="unconfigured", min_length=1, max_length=160)
    generation_mode: Literal["offline_test", "live_llm"] = "offline_test"
    is_deterministic_fixture: bool = False
    is_mock_output: bool = False
    token_usage: dict[str, int] | None = None

    @field_validator("model_label")
    @classmethod
    def _validate_model_label(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="model_label")

    @model_validator(mode="after")
    def _validate_live_mode_honesty(self) -> PatchModelMetadata:
        if self.generation_mode == "live_llm" and (
            self.is_deterministic_fixture or self.is_mock_output
        ):
            raise ValueError(
                "Live LLM patch generation metadata cannot declare "
                "is_deterministic_fixture=True or is_mock_output=True"
            )
        return self


class PatchFileChange(BaseModel):
    """Metadata describing a single file modified by a proposed patch."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(..., min_length=1, max_length=500)
    change_type: Literal["modify", "add", "delete", "rename"] = "modify"
    old_path: str | None = Field(default=None, max_length=500)
    added_lines: int = Field(default=0, ge=0)
    removed_lines: int = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=1000)
    related_criterion_ids: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        cleaned = _strip_non_empty(value, field_name="path")
        return cleaned.replace("\\", "/")

    @field_validator("old_path")
    @classmethod
    def _validate_old_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned.replace("\\", "/") if cleaned else None

    @field_validator("related_criterion_ids")
    @classmethod
    def _validate_criterion_ids(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="related_criterion_ids")

    @model_validator(mode="after")
    def _validate_rename_old_path(self) -> PatchFileChange:
        if self.change_type == "rename" and not self.old_path:
            raise ValueError(
                f"PatchFileChange for '{self.path}' with change_type='rename' requires old_path"
            )
        return self


class PatchProposal(BaseModel):
    """Structured unified Git diff proposal returned by an A4 implementation."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(..., pattern=STABLE_ID_PATTERN.pattern)
    unified_diff: str = Field(..., min_length=1)
    patch_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    affected_files: list[PatchFileChange] = Field(..., min_length=1)
    change_rationale: str = Field(..., min_length=1, max_length=4000)
    related_criterion_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risk_indicators: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("change_rationale")
    @classmethod
    def _validate_rationale(cls, value: str) -> str:
        return _strip_non_empty(value, field_name="change_rationale")

    @field_validator("related_criterion_ids")
    @classmethod
    def _validate_related_criteria(cls, value: list[str]) -> list[str]:
        return _validate_unique_id_list(value, field_name="related_criterion_ids")

    @field_validator("assumptions", "risk_indicators", "notes")
    @classmethod
    def _validate_text_lists(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def _validate_hash_and_unique_files(self) -> PatchProposal:
        if not self.unified_diff.strip():
            raise ValueError("unified_diff must not be blank")
        expected_sha = compute_patch_sha256(self.unified_diff)
        if not hmac.compare_digest(expected_sha, self.patch_sha256):
            raise ValueError(
                "patch_sha256 does not match the SHA-256 digest of unified_diff"
            )

        seen_paths: set[str] = set()
        for file_change in self.affected_files:
            if file_change.path in seen_paths:
                raise ValueError(
                    f"Duplicate affected file path in PatchProposal: '{file_change.path}'"
                )
            seen_paths.add(file_change.path)

        return self


class PatchGenerationRequest(BaseModel):
    """Typed A4 input contract passed by M02 into ``PatchGenerationAdapter``."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=120)
    original_requirement: str = Field(..., min_length=1, max_length=10000)
    acceptance_criteria: list[AcceptanceCriterion] = Field(..., min_length=1)
    business_constraints: list[BusinessConstraint] = Field(default_factory=list)
    unresolved_ambiguities: list[RequirementAmbiguity] = Field(default_factory=list)
    repository_snapshot_ref: str = Field(..., min_length=1, max_length=200)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)
    resolved_base_commit: str | None = None
    source_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_path: str = Field(default=".", min_length=1, max_length=500)
    codebase_context: PreChangeCodebaseContext | None = None
    allowed_modification_paths: list[str] = Field(default_factory=list)
    relevant_source_files: dict[str, str] = Field(default_factory=dict)
    relevant_source_file_paths: list[str] = Field(default_factory=list)
    attempt_number: int = Field(default=1, ge=1, le=10)
    execution_mode: Literal["offline_test", "live_llm"] = "offline_test"
    previous_verification_evidence: dict[str, Any] | None = None

    @field_validator("original_requirement", "repository_snapshot_ref")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be blank")
        return cleaned

    @field_validator("allowed_modification_paths", "relevant_source_file_paths")
    @classmethod
    def _validate_paths_list(cls, value: list[str]) -> list[str]:
        normalized = [p.strip().replace("\\", "/") for p in value if p and p.strip()]
        return _validate_unique_text_list(normalized, field_name="paths")

    @model_validator(mode="after")
    def _sync_relevant_source_file_paths(self) -> PatchGenerationRequest:
        if self.relevant_source_files and not self.relevant_source_file_paths:
            self.relevant_source_file_paths = sorted(
                k.replace("\\", "/") for k in self.relevant_source_files
            )
        return self


class PatchGenerationResult(BaseModel):
    """Normalized, validated output returned by ``PatchGenerationAdapter``."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["proposed", "unavailable", "blocked", "error"]
    attempt_number: int = Field(default=1, ge=1, le=10)
    proposal: PatchProposal | None = None
    model_metadata: PatchModelMetadata = Field(default_factory=PatchModelMetadata)
    is_deterministic_fixture: bool = False
    is_mock_output: bool = False
    error: AgentError | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_result_invariants(self) -> PatchGenerationResult:
        if self.status == "proposed":
            if self.proposal is None:
                raise ValueError(
                    "PatchGenerationResult with status='proposed' requires a non-null proposal"
                )
            if self.error is not None:
                raise ValueError(
                    "PatchGenerationResult with status='proposed' must not carry an error"
                )
        else:
            if self.proposal is not None:
                raise ValueError(
                    f"PatchGenerationResult with status='{self.status}' must not carry a proposal"
                )
            if self.error is None:
                raise ValueError(
                    f"PatchGenerationResult with status='{self.status}' requires a structured error"
                )

        if self.is_deterministic_fixture != self.model_metadata.is_deterministic_fixture:
            raise ValueError(
                "is_deterministic_fixture must match model_metadata.is_deterministic_fixture"
            )
        if self.is_mock_output != self.model_metadata.is_mock_output:
            raise ValueError(
                "is_mock_output must match model_metadata.is_mock_output"
            )
        if self.model_metadata.generation_mode == "live_llm" and (
            self.is_deterministic_fixture or self.is_mock_output
        ):
            raise ValueError(
                "Live LLM patch generation results cannot be labeled as deterministic fixtures or mock output"
            )
        return self


class PatchValidationResult(BaseModel):
    """Structured outcome of M02 pre-application patch safety and applicability checks."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    status: Literal["passed", "failed"]
    patch_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    expected_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    checked_files: list[str] = Field(default_factory=list)
    patch_size_bytes: int = Field(default=0, ge=0)
    changed_file_count: int = Field(default=0, ge=0)
    git_apply_check_passed: bool = False
    checks_passed: list[str] = Field(default_factory=list)
    violations: list[AgentError] = Field(default_factory=list)
    validated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_truthfulness(self) -> PatchValidationResult:
        if self.valid:
            if self.status != "passed":
                raise ValueError("valid=True requires status='passed'")
            if not self.git_apply_check_passed:
                raise ValueError("valid=True requires git_apply_check_passed=True")
            if self.violations:
                raise ValueError("valid=True cannot contain validation violations")
        else:
            if self.status != "failed":
                raise ValueError("valid=False requires status='failed'")
            if not self.violations:
                raise ValueError("valid=False requires at least one structured violation")
        return self


class PatchApplicationResult(BaseModel):
    """Structured outcome of applying a validated patch inside the isolated workspace."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    status: Literal["applied", "failed", "reverted"]
    patch_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    original_source_snapshot_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    original_git_diff_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    resulting_source_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    resulting_git_diff_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    applied_files: list[str] = Field(default_factory=list)
    rollback_performed: bool = False
    workspace_clean_after_rollback: bool | None = None
    error: AgentError | None = None
    applied_at: datetime = Field(default_factory=utc_now)
    isolation_note: str = Field(
        default=(
            "Patch was applied inside an isolated temporary Git working copy; "
            "Git workspace isolation is not an operating-system security sandbox."
        ),
        max_length=1000,
    )

    @model_validator(mode="after")
    def _validate_application_truthfulness(self) -> PatchApplicationResult:
        if self.applied:
            if self.status != "applied":
                raise ValueError("applied=True requires status='applied'")
            if (
                self.resulting_source_snapshot_sha256 is None
                or self.resulting_git_diff_sha256 is None
            ):
                raise ValueError(
                    "applied=True requires resulting_source_snapshot_sha256 and resulting_git_diff_sha256"
                )
            if self.error is not None:
                raise ValueError("applied=True must not contain an error")
        else:
            if self.status not in {"failed", "reverted"}:
                raise ValueError("applied=False requires status='failed' or 'reverted'")
            if self.error is None:
                raise ValueError("applied=False requires a structured error")
        return self
