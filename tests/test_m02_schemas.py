"""Offline unit tests for M02 shared workflow schemas, agent schemas, and config."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import SecretStr, ValidationError

from project.marsadh_orchestrator.config import OrchestratorConfig
from project.schemas import (
    AgentError,
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentLogEntry,
    ChangePackageReport,
    CodebaseAnalysisResult,
    CreateRunRequest,
    ExecutionMode,
    GeneratedTestScenario,
    HumanDecision,
    HumanDecisionRequest,
    RepositoryInfo,
    RiskSummary,
    TestGenerationResult,
    TestReviewRequest,
    VerificationOutcome,
    VerificationResult,
    WorkflowRunRecord,
    WorkflowState,
)


def test_workflow_states_match_mvp_contract_without_patch_repair() -> None:
    expected = {
        "CREATED",
        "ANALYZING_CODEBASE",
        "GENERATING_TESTS",
        "AWAITING_TEST_REVIEW",
        "RUNNING_VERIFICATION",
        "COMPILING_REPORT",
        "AWAITING_FINAL_APPROVAL",
        "COMPLETED_NO_CHANGES",
        "APPROVED",
        "REJECTED",
        "NEEDS_REVIEW",
        "FAILED",
    }
    actual = {state.value for state in WorkflowState}
    assert actual == expected
    assert "REPAIRING_PATCH" not in actual


def test_create_run_request_defaults_and_validation() -> None:
    req = CreateRunRequest(
        repository_path="sample_projects/mini_pos",
        source_path="sample_projects/mini_pos",
        base_ref="HEAD",
        requirements="Apply 10% discount for premium customers only.",
    )
    assert req.execution_mode == ExecutionMode.OFFLINE_TEST
    assert req.source_path == "sample_projects/mini_pos"

    req_by_id = CreateRunRequest(
        repository_id="mini-pos-demo",
        requirements="Check refund calculation.",
    )
    assert req_by_id.repository_id == "mini-pos-demo"
    assert req_by_id.execution_mode == ExecutionMode.OFFLINE_TEST

    with pytest.raises(ValidationError):
        CreateRunRequest(requirements="Missing repository info")

    with pytest.raises(ValidationError):
        CreateRunRequest(repository_id="demo", requirements="   ")

    with pytest.raises(ValidationError):
        CreateRunRequest(
            repository_id="demo",
            source_path="../escape",
            requirements="Traversal should fail",
        )

    with pytest.raises(ValidationError):
        CreateRunRequest(
            repository_id="demo",
            source_path="/etc/passwd",
            requirements="Absolute source path should fail",
        )

    with pytest.raises(ValidationError):
        CreateRunRequest(
            repository_id="demo",
            base_ref="--exec=evil",
            requirements="Flag injection in base_ref should fail",
        )


def test_test_review_and_human_decision_validation() -> None:
    review = TestReviewRequest(
        approved=True,
        selected_test_ids=["SCN-001", "SCN-002", "SCN-001"],
        reviewer="reviewer-1",
        notes="Checked assertions.",
    )
    assert review.selected_test_ids == ["SCN-001", "SCN-002"]

    with pytest.raises(ValidationError):
        TestReviewRequest(approved=True, selected_test_ids=[])

    with pytest.raises(ValidationError):
        TestReviewRequest(approved=True, selected_test_ids=["../test_escape.py"])

    with pytest.raises(ValidationError):
        TestReviewRequest(approved=True, selected_test_ids=["dir/test_generated_001.py"])

    rejected_review = TestReviewRequest(approved=False, notes="Assumptions need update")
    assert rejected_review.approved is False

    approve_decision = HumanDecisionRequest(
        decision=HumanDecision.APPROVE,
        reviewer="lead-reviewer",
        comments="Looks good.",
    )
    assert approve_decision.decision == HumanDecision.APPROVE

    with pytest.raises(ValidationError):
        HumanDecisionRequest(
            decision=HumanDecision.REJECT,
            reviewer="lead-reviewer",
            comments="",
        )

    with pytest.raises(ValidationError):
        HumanDecisionRequest(
            decision=HumanDecision.NEEDS_REVIEW,
            reviewer="lead-reviewer",
            comments="   ",
        )


def test_workflow_and_agent_result_json_roundtrip() -> None:
    code = "def test_ok():\n    assert True\n"
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()

    codebase = CodebaseAnalysisResult(
        status="completed",
        repository_path="/repo",
        source_path="src",
        base_ref="HEAD",
        resolved_base_commit="a" * 40,
        total_indexed_files=2,
        has_relevant_changes=True,
        changed_files_repo_relative=["src/billing.py"],
        changed_files_source_relative=["billing.py"],
        raw_report={"agent": "CodebaseIntelligenceAgent", "reports": []},
    )
    generation = TestGenerationResult(
        status="proposed",
        generation_mode="offline_test",
        is_mock_output=True,
        model_label="deterministic_stub",
        scenarios=[
            GeneratedTestScenario(
                scenario_id="SCN-001",
                title="Discount test",
                rationale="Check changed checkout",
                assumptions="10% discount",
                test_code=code,
                content_sha256=code_hash,
            )
        ],
    )
    verification = VerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.PASSED,
        verification_passed=True,
        executed_test_ids=["SCN-001"],
        executed_test_paths=[".changeguard_generated_tests/test_generated_001.py"],
        exit_code=0,
        total_tests=1,
        passed=1,
        failed=0,
        duration_seconds=0.12,
        risk_summary=RiskSummary(),
    )
    report = ChangePackageReport(
        run_id="run_test_001",
        requirements="Apply 10% discount.",
        repository_path="/repo",
        source_path="src",
        base_ref="HEAD",
        codebase_analysis=codebase,
        test_generation=generation,
        verification=verification,
    )
    run = WorkflowRunRecord(
        run_id="run_test_001",
        state=WorkflowState.AWAITING_FINAL_APPROVAL,
        requirements="Apply 10% discount.",
        repository=RepositoryInfo(
            repository_path="/repo",
            source_path="src",
            base_ref="HEAD",
            change_mode="working_tree",
        ),
        agent_records=[
            AgentExecutionRecord(
                agent_name="CodebaseAdapter",
                stage="ANALYZING_CODEBASE",
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=25,
                output_summary="1 changed file detected",
            )
        ],
        codebase_analysis=codebase,
        test_generation=generation,
        verification=verification,
        report=report,
    )

    serialized = run.model_dump_json()
    restored = WorkflowRunRecord.model_validate_json(serialized)
    assert restored.run_id == "run_test_001"
    assert restored.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert restored.verification is not None
    assert restored.verification.verification_passed is True
    assert restored.verification.coverage_status == "unavailable"
    assert restored.verification.coverage_percent is None

    log_entry = AgentLogEntry(
        run_id="run_test_001",
        agent_name="TestingAdapter",
        stage=WorkflowState.GENERATING_TESTS,
        status=AgentExecutionStatus.SUCCEEDED,
        duration_ms=10,
        input_summary="1 changed file",
        output_summary="1 proposed scenario",
        error=AgentError(code="NONE", message="none"),
    )
    restored_log = AgentLogEntry.model_validate_json(log_entry.model_dump_json())
    assert restored_log.stage == WorkflowState.GENERATING_TESTS


def test_verification_result_forbids_false_positive_pass() -> None:
    with pytest.raises(ValidationError):
        VerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.PASSED,
            verification_passed=True,
            exit_code=2,
            total_tests=1,
            passed=1,
            failed=0,
        )

    with pytest.raises(ValidationError):
        VerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.PASSED,
            verification_passed=True,
            exit_code=0,
            total_tests=0,
            passed=0,
            failed=0,
        )

    with pytest.raises(ValidationError):
        VerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.COLLECTION_ERROR,
            verification_passed=True,
            exit_code=0,
            total_tests=1,
            passed=1,
            failed=0,
        )


def test_config_validation_and_secret_protection(tmp_path) -> None:
    secret_marker = "test_secret_" + "9876543210"
    cfg = OrchestratorConfig(
        database_path=tmp_path / "runs.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[tmp_path],
        trusted_execution_roots=[tmp_path],
        verification_timeout_seconds=30,
        max_repair_attempts=2,
        groq_model="llama-3.3-70b-versatile",
        groq_api_key=SecretStr(secret_marker),
    )

    assert cfg.execution_mode == ExecutionMode.OFFLINE_TEST
    assert cfg.allow_untrusted_execution is False
    assert secret_marker not in repr(cfg)
    assert secret_marker not in cfg.model_dump_json()
    assert "groq_api_key" not in cfg.model_dump()
    summary = cfg.safe_summary()
    assert summary["groq_api_key_configured"] is True
    assert secret_marker not in str(summary)
    assert cfg.get_groq_api_key() == secret_marker

    with pytest.raises(ValidationError):
        OrchestratorConfig(verification_timeout_seconds=0)

    with pytest.raises(ValidationError):
        OrchestratorConfig(verification_timeout_seconds=601)

    with pytest.raises(ValidationError):
        OrchestratorConfig(max_repair_attempts=-1)

    with pytest.raises(ValidationError):
        OrchestratorConfig(max_repair_attempts=3)

    with pytest.raises(ValidationError):
        OrchestratorConfig(app_port=0)

    env_cfg = OrchestratorConfig.from_env(
        {
            "CHANGEGUARD_DB_PATH": str(tmp_path / "env.sqlite3"),
            "CHANGEGUARD_WORKSPACE_ROOT": str(tmp_path / "env_ws"),
            "CHANGEGUARD_VERIFICATION_TIMEOUT": "45",
            "CHANGEGUARD_MAX_REPAIR_ATTEMPTS": "1",
            "GROQ_API_KEY": secret_marker,
        }
    )
    assert env_cfg.verification_timeout_seconds == 45
    assert env_cfg.max_repair_attempts == 1
    assert secret_marker not in env_cfg.model_dump_json()
