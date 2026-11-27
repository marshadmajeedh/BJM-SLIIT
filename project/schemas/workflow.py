"""Shared workflow, request, decision, and log schemas for ChangeGuard AI (M02).

Design assumptions:
- No active patch-repair workflow state is included until patch generation (M03/A4)
  is implemented.
- Test review references stable scenario identifiers rather than raw filesystem
  paths supplied by API clients.
- Execution defaults to safe offline mode (``offline_test``); live LLM requests
  must never silently fall back to mock output.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from pathlib import PurePath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .agents import (
    AgentError,
    AgentExecutionRecord,
    AgentExecutionStatus,
    ChangePackageReport,
    CodebaseAnalysisResult,
    TestGenerationResult,
    VerificationResult,
    utc_now,
)
from .requirements import RequirementAnalysisResult

SCENARIO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SAFE_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/@^~{}][A-Za-z0-9._/@^~{}-]{0,199}$")
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class WorkflowState(str, Enum):
    """Deterministic workflow lifecycle states for M02 orchestration."""

    CREATED = "CREATED"
    ANALYZING_REQUIREMENTS = "ANALYZING_REQUIREMENTS"
    ANALYZING_CODEBASE = "ANALYZING_CODEBASE"
    GENERATING_TESTS = "GENERATING_TESTS"
    AWAITING_TEST_REVIEW = "AWAITING_TEST_REVIEW"
    RUNNING_VERIFICATION = "RUNNING_VERIFICATION"
    COMPILING_REPORT = "COMPILING_REPORT"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    COMPLETED_NO_CHANGES = "COMPLETED_NO_CHANGES"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class ExecutionMode(str, Enum):
    """Explicit execution mode controlling offline stub vs live LLM invocation."""

    OFFLINE_TEST = "offline_test"
    LIVE_LLM = "live_llm"


class HumanDecision(str, Enum):
    """Supported final human review decisions."""

    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class CreateRunRequest(BaseModel):
    """Request payload to initiate a ChangeGuard workflow run."""

    model_config = ConfigDict(extra="forbid")

    repository_path: str | None = Field(default=None, max_length=1000)
    repository_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    source_path: str = Field(default=".", min_length=1, max_length=500)
    base_ref: str = Field(default="HEAD", min_length=1, max_length=200)
    requirements: str = Field(..., min_length=1, max_length=10000)
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST

    @field_validator("repository_path", "source_path", "base_ref", "requirements")
    @classmethod
    def _strip_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped

    @field_validator("requirements")
    @classmethod
    def _require_non_empty_requirements(cls, value: str) -> str:
        if not value:
            raise ValueError("requirements must not be blank")
        return value

    @field_validator("source_path")
    @classmethod
    def _validate_source_path(cls, value: str) -> str:
        if not value:
            raise ValueError("source_path must not be blank")
        if "\x00" in value:
            raise ValueError("source_path must not contain null bytes")
        if value.startswith(("/", "\\")) or WINDOWS_DRIVE_PATTERN.match(value):
            raise ValueError("source_path must be relative to the repository root")
        normalized = value.replace("\\", "/")
        parts = PurePath(normalized).parts
        if ".." in parts:
            raise ValueError("source_path must not contain parent directory traversal")
        return normalized

    @field_validator("base_ref")
    @classmethod
    def _validate_base_ref(cls, value: str) -> str:
        if not value:
            raise ValueError("base_ref must not be blank")
        if value.startswith("-"):
            raise ValueError("base_ref must not start with '-'")
        if not SAFE_GIT_REF_PATTERN.match(value) or ".." in value:
            raise ValueError("base_ref contains unsupported characters or range syntax")
        return value

    @model_validator(mode="after")
    def _validate_repository_reference(self) -> CreateRunRequest:
        has_path = bool(self.repository_path)
        has_id = bool(self.repository_id)
        if not has_path and not has_id:
            raise ValueError("Either repository_path or repository_id must be provided")
        return self


class AgentLogEntry(BaseModel):
    """Structured inter-agent communication and stage log entry."""

    model_config = ConfigDict(extra="forbid")

    log_id: str | None = Field(default=None, max_length=120)
    run_id: str = Field(..., min_length=1, max_length=120)
    agent_name: str = Field(..., min_length=1, max_length=120)
    stage: WorkflowState
    timestamp: datetime = Field(default_factory=utc_now)
    status: AgentExecutionStatus
    duration_ms: int = Field(default=0, ge=0)
    input_summary: str = Field(default="", max_length=2000)
    output_summary: str = Field(default="", max_length=2000)
    error: AgentError | None = None


class TestReviewRequest(BaseModel):
    """Human review decision for generated test proposals before verification."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    approved: bool
    selected_test_ids: list[str] = Field(default_factory=list)
    reviewer: str = Field(default="human_reviewer", min_length=1, max_length=200)
    notes: str = Field(default="", max_length=4000)

    @field_validator("reviewer", "notes")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("reviewer")
    @classmethod
    def _require_reviewer(cls, value: str) -> str:
        if not value:
            raise ValueError("reviewer must not be blank")
        return value

    @field_validator("selected_test_ids")
    @classmethod
    def _validate_test_ids(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        validated: list[str] = []
        for raw_id in value:
            test_id = raw_id.strip()
            if not SCENARIO_ID_PATTERN.match(test_id):
                raise ValueError(
                    f"Invalid test identifier '{raw_id}': expected stable identifier "
                    "matching ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ (no filesystem paths)"
                )
            if test_id not in seen:
                seen.add(test_id)
                validated.append(test_id)
        return validated

    @model_validator(mode="after")
    def _validate_approval_selection(self) -> TestReviewRequest:
        if self.approved and not self.selected_test_ids:
            raise ValueError(
                "At least one selected_test_id is required when approving tests"
            )
        return self


class TestReviewRecord(BaseModel):
    """Persisted test review decision including approved test file hashes."""

    __test__ = False
    model_config = ConfigDict(extra="forbid")

    approved: bool
    selected_test_ids: list[str] = Field(default_factory=list)
    approved_test_hashes: dict[str, str] = Field(default_factory=dict)
    reviewer: str = Field(..., min_length=1, max_length=200)
    notes: str = Field(default="", max_length=4000)
    decided_at: datetime = Field(default_factory=utc_now)


class HumanDecisionRequest(BaseModel):
    """Final human approval gate decision for a compiled change package."""

    model_config = ConfigDict(extra="forbid")

    decision: HumanDecision
    reviewer: str = Field(..., min_length=1, max_length=200)
    comments: str = Field(default="", max_length=4000)

    @field_validator("reviewer", "comments")
    @classmethod
    def _strip_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("reviewer")
    @classmethod
    def _require_reviewer(cls, value: str) -> str:
        if not value:
            raise ValueError("reviewer must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_comments_for_non_approval(self) -> HumanDecisionRequest:
        if (
            self.decision in {HumanDecision.REJECT, HumanDecision.NEEDS_REVIEW}
            and not self.comments
        ):
            raise ValueError(
                f"comments are required when decision is '{self.decision.value}'"
            )
        return self


class HumanDecisionRecord(BaseModel):
    """Persisted final human decision record."""

    model_config = ConfigDict(extra="forbid")

    decision: HumanDecision
    reviewer: str = Field(..., min_length=1, max_length=200)
    comments: str = Field(default="", max_length=4000)
    decided_at: datetime = Field(default_factory=utc_now)
    evidence_package_id: str | None = None
    evidence_package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RepositoryInfo(BaseModel):
    """Resolved repository metadata persisted on a workflow run."""

    model_config = ConfigDict(extra="forbid")

    repository_id: str | None = None
    repository_path: str = Field(..., min_length=1)
    source_path: str = Field(default=".", min_length=1)
    base_ref: str = Field(default="HEAD", min_length=1)
    effective_base_ref: str | None = None
    resolved_base_commit: str | None = None
    head_commit: str | None = None
    change_mode: Literal["working_tree", "committed_range", "unknown"] = "unknown"


class WorkflowRunRecord(BaseModel):
    """Complete persisted state for a ChangeGuard AI workflow run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., min_length=1, max_length=120)
    state: WorkflowState = WorkflowState.CREATED
    requirements: str = Field(..., min_length=1, max_length=10000)
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST
    repository: RepositoryInfo
    workspace_id: str | None = None
    workspace_path: str | None = None
    source_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    git_diff_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    agent_records: list[AgentExecutionRecord] = Field(default_factory=list)
    requirement_analysis: RequirementAnalysisResult | None = None
    codebase_analysis: CodebaseAnalysisResult | None = None
    test_generation: TestGenerationResult | None = None
    test_review: TestReviewRecord | None = None
    verification: VerificationResult | None = None
    report: ChangePackageReport | None = None
    final_decision: HumanDecisionRecord | None = None
    error: AgentError | None = None
