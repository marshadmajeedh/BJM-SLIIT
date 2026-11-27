"""Deterministic offline unit and integration tests for M02 OrchestratorService."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from project.marsadh_orchestrator import (
    InvalidStateTransitionError,
    OrchestratorConfig,
    OrchestratorService,
    RepositoryAccessDeniedError,
    ReviewConflictError,
    RunNotFoundError,
    RunStore,
    ValidationInputError,
    WorkflowIntegrityError,
    WorkspaceManager,
)
from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
)
from project.schemas import (
    CreateRunRequest,
    ExecutionMode,
    HumanDecision,
    HumanDecisionRequest,
    RepositoryInfo,
    TestReviewRequest,
    VerificationOutcome,
    WorkflowRunRecord,
    WorkflowState,
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for orchestrator tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _create_git_fixture(repo: Path, *, modify: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    pkg = repo / "app"
    pkg.mkdir()
    (pkg / "billing.py").write_text(
        "def checkout(amount, customer_type='regular'):\n"
        "    return amount\n",
        encoding="utf-8",
    )
    (pkg / "refunds.py").write_text(
        "from billing import checkout\n\n"
        "def refund(amount, customer_type='regular'):\n"
        "    return checkout(amount, customer_type)\n",
        encoding="utf-8",
    )
    (pkg / "dashboard.py").write_text(
        "from refunds import refund\n\n"
        "def summary(amount, customer_type='regular'):\n"
        "    return refund(amount, customer_type)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    if modify:
        (pkg / "billing.py").write_text(
            "def checkout(amount, customer_type='regular'):\n"
            "    if customer_type == 'premium':\n"
            "        return amount * 0.9\n"
            "    return amount\n",
            encoding="utf-8",
        )


def _make_config(
    tmp_path: Path,
    *,
    repo: Path | None = None,
    trusted: bool = True,
    timeout: int = 30,
) -> OrchestratorConfig:
    repos_root = tmp_path / "repos"
    configured = {"demo-repo": repo} if repo is not None else {}
    return OrchestratorConfig(
        database_path=tmp_path / "state" / "changeguard.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories=configured,
        trusted_execution_roots=[repos_root] if trusted else [],
        verification_timeout_seconds=timeout,
    )


def _make_service(
    config: OrchestratorConfig,
    *,
    offline_model_callable=None,
    use_default_offline_stub: bool = True,
    codebase_adapter: CodebaseAdapter | None = None,
    testing_adapter: TestingAdapter | None = None,
    verification_adapter: VerificationAdapter | None = None,
) -> OrchestratorService:
    store = RunStore(config.database_path)
    return OrchestratorService(
        config=config,
        store=store,
        workspace_manager=WorkspaceManager(config),
        codebase_adapter=codebase_adapter or CodebaseAdapter(config),
        testing_adapter=testing_adapter or TestingAdapter(config),
        verification_adapter=verification_adapter or VerificationAdapter(config),
        offline_model_callable=offline_model_callable,
        use_default_offline_stub=use_default_offline_stub,
    )


def _two_scenario_stub(
    marker_one: Path | None = None,
    marker_two: Path | None = None,
):
    m1_line = (
        f"    pathlib.Path({str(marker_one)!r}).write_text('ran_1', encoding='utf-8')\n"
        if marker_one is not None
        else ""
    )
    m2_line = (
        f"    pathlib.Path({str(marker_two)!r}).write_text('ran_2', encoding='utf-8')\n"
        if marker_two is not None
        else ""
    )

    def _stub(prompt: str) -> str:
        assert "checkout" in prompt
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Premium checkout receives 10% discount",
                        "rationale": "billing.checkout changed to apply 10% discount for premium",
                        "assumptions": "Deterministic offline stub assumption: premium=0.9x",
                        "test_code": (
                            "import pathlib, sys\n"
                            "from pathlib import Path\n"
                            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))\n"
                            "from billing import checkout\n"
                            "def test_premium_discount():\n"
                            f"{m1_line}"
                            "    assert checkout(1000, 'premium') == 900\n"
                        ),
                    },
                    {
                        "title": "Regular checkout remains unchanged",
                        "rationale": "Regular customer checkout must not receive a discount",
                        "assumptions": "Deterministic offline stub assumption: regular=1.0x",
                        "test_code": (
                            "import pathlib, sys\n"
                            "from pathlib import Path\n"
                            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))\n"
                            "from billing import checkout\n"
                            "def test_regular_checkout():\n"
                            f"{m2_line}"
                            "    assert checkout(1000, 'regular') == 1000\n"
                        ),
                    },
                ]
            }
        )

    return _stub


# ---------------------------------------------------------------------------
# 1, 7, 8, 12, 22, 24, 25: Full real M01 -> M03 -> Gate 1 -> M04 -> Gate 2
# ---------------------------------------------------------------------------


def test_full_workflow_through_both_gates_real_adapters_and_repo_unchanged(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    orig_head = _git(repo, "rev-parse", "HEAD")
    orig_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    orig_status = _git(repo, "status", "--porcelain")

    marker_1 = tmp_path / "marker_scn1.txt"
    marker_2 = tmp_path / "marker_scn2.txt"

    cfg = _make_config(tmp_path, repo=repo, trusted=True)
    svc = _make_service(cfg, offline_model_callable=_two_scenario_stub(marker_1, marker_2))

    # Start workflow -> must pause at AWAITING_TEST_REVIEW without executing tests
    run = svc.start_run(
        CreateRunRequest(
            repository_id="demo-repo",
            source_path="app",
            base_ref="HEAD",
            requirements="Apply 10% discount to premium customers only.",
            execution_mode=ExecutionMode.OFFLINE_TEST,
        )
    )
    assert run.state == WorkflowState.AWAITING_TEST_REVIEW
    assert run.codebase_analysis is not None
    assert run.codebase_analysis.status == "completed"
    assert run.codebase_analysis.changed_files_repo_relative == ["app/billing.py"]
    assert run.codebase_analysis.impacted_modules_repo_relative == [
        "app/dashboard.py",
        "app/refunds.py",
    ]
    assert run.test_generation is not None
    assert run.test_generation.executed is False
    assert run.test_generation.is_mock_output is True
    assert [s.scenario_id for s in run.test_generation.scenarios] == ["SCN-001", "SCN-002"]
    assert run.source_snapshot_sha256 is not None
    assert run.git_diff_sha256 is not None

    # Strictly no execution before human approval
    assert not marker_1.exists()
    assert not marker_2.exists()
    assert run.verification is None

    # Approve ONLY SCN-002; SCN-001 must NOT be exported or executed
    reviewed = svc.submit_test_review(
        run.run_id,
        TestReviewRequest(
            approved=True,
            selected_test_ids=["SCN-002"],
            reviewer="qa-lead",
            notes="Approve regular checkout scenario only.",
        ),
    )
    assert reviewed.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert not marker_1.exists(), "Unselected SCN-001 must not execute"
    assert marker_2.exists(), "Approved SCN-002 must execute via real pytest"

    assert reviewed.verification is not None
    assert reviewed.verification.verification_outcome == VerificationOutcome.PASSED
    assert reviewed.verification.verification_passed is True
    assert reviewed.verification.exit_code == 0
    assert reviewed.verification.total_tests == 1
    assert reviewed.verification.passed == 1
    assert reviewed.verification.failed == 0
    assert reviewed.verification.executed_test_ids == ["SCN-002"]
    assert reviewed.verification.coverage_status == "unavailable"
    assert reviewed.verification.baseline_comparison.status == "unavailable"

    assert reviewed.report is not None
    assert reviewed.report.package_id is not None
    assert reviewed.report.package_sha256 is not None
    assert reviewed.report.recommendation_label == "ready_for_human_review"
    assert reviewed.report.approved_scenario_ids == ["SCN-002"]
    assert reviewed.report.executed_scenario_ids == ["SCN-002"]
    assert "SCN-001" in reviewed.report.traceability["unverified_scenario_ids"]
    assert reviewed.report.evidence_limitations

    # Final human approval
    final = svc.submit_final_decision(
        run.run_id,
        HumanDecisionRequest(
            decision=HumanDecision.APPROVE,
            reviewer="release-reviewer",
            comments="Evidence verified for regular checkout scenario.",
        ),
    )
    assert final.state == WorkflowState.APPROVED
    assert final.final_decision is not None
    assert final.final_decision.decision == HumanDecision.APPROVE
    assert final.final_decision.evidence_package_id == reviewed.report.package_id
    assert final.final_decision.evidence_package_sha256 == reviewed.report.package_sha256

    # Persistent communication logs across all stages
    logs = svc.get_run_logs(run.run_id)
    assert len(logs) >= 10
    agents_seen = {entry.agent_name for entry in logs}
    assert {
        "OrchestratorService",
        "WorkspaceManager",
        "CodebaseAdapter",
        "TestingAdapter",
        "HumanTestReviewGate",
        "VerificationAdapter",
        "ChangePackageCompiler",
        "HumanFinalDecisionGate",
    }.issubset(agents_seen)
    assert all(entry.timestamp.tzinfo is not None for entry in logs)

    # Original repository untouched; no automatic Git commit, merge, or push
    assert _git(repo, "rev-parse", "HEAD") == orig_head
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == orig_branch
    assert _git(repo, "status", "--porcelain") == orig_status
    assert not (repo / ".changeguard_generated_tests").exists()


# ---------------------------------------------------------------------------
# 2: Empty Git diff -> COMPLETED_NO_CHANGES (M03 & M04 never called)
# ---------------------------------------------------------------------------


def test_empty_git_diff_stops_at_completed_no_changes(tmp_path) -> None:
    repo = tmp_path / "repos" / "clean"
    _create_git_fixture(repo, modify=False)
    cfg = _make_config(tmp_path, repo=repo)

    m03_called = False

    def fail_if_called(_prompt: str) -> str:
        nonlocal m03_called
        m03_called = True
        return "{}"

    svc = _make_service(cfg, offline_model_callable=fail_if_called)
    run = svc.start_run(
        CreateRunRequest(
            repository_id="demo-repo",
            source_path="app",
            base_ref="HEAD",
            requirements="Check clean repo.",
        )
    )
    assert run.state == WorkflowState.COMPLETED_NO_CHANGES
    assert m03_called is False
    assert run.test_generation is None
    assert run.verification is None
    assert run.codebase_analysis is not None
    assert run.codebase_analysis.status == "no_changes"
    assert run.codebase_analysis.has_relevant_changes is False


# ---------------------------------------------------------------------------
# 3, 4, 5: M01 failure, M03 failure, invalid generated scenarios, live_llm unconfigured
# ---------------------------------------------------------------------------


def test_m01_failure_transitions_to_failed(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo)

    class BrokenCodebaseAdapter(CodebaseAdapter):
        def analyze(self, *args, **kwargs):
            raise RuntimeError("Simulated M01 AST crash")

    svc = _make_service(cfg, codebase_adapter=BrokenCodebaseAdapter(cfg))
    run = svc.start_run(
        CreateRunRequest(
            repository_id="demo-repo",
            source_path="app",
            base_ref="HEAD",
            requirements="Test M01 failure handling.",
        )
    )
    assert run.state == WorkflowState.FAILED
    assert run.error is not None
    assert run.error.code == "M01_UNEXPECTED_ERROR"
    assert run.test_generation is None


def test_m03_failures_and_live_llm_never_falls_back_to_mock(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo)

    # 4a. Invalid JSON from model
    svc_bad_json = _make_service(cfg, offline_model_callable=lambda _p: "not-json{{{")
    r_json = svc_bad_json.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check bad JSON")
    )
    assert r_json.state == WorkflowState.FAILED
    assert r_json.error is not None
    assert r_json.error.code == "INVALID_MODEL_JSON"

    # 4b. Model exception
    def _boom(_p: str) -> str:
        raise RuntimeError("Provider connection dropped")

    svc_exc = _make_service(cfg, offline_model_callable=_boom)
    r_exc = svc_exc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check model error")
    )
    assert r_exc.state == WorkflowState.FAILED
    assert r_exc.error is not None
    assert r_exc.error.code == "M03_GENERATION_ERROR"

    # 5. Invalid scenario (no test_ function)
    svc_invalid_scn = _make_service(
        cfg,
        offline_model_callable=lambda _p: json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Bad scenario",
                        "rationale": "Missing pytest def",
                        "assumptions": "None",
                        "test_code": "def helper():\n    return 1\n",
                    }
                ]
            }
        ),
    )
    r_inv = svc_invalid_scn.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check invalid scenarios")
    )
    assert r_inv.state == WorkflowState.FAILED
    assert r_inv.error is not None
    assert r_inv.error.code == "INVALID_MODEL_OUTPUT"

    # 4c. Live LLM without credentials must fail explicitly at A1 (never silently fall back to offline stub)
    svc_live = _make_service(cfg, offline_model_callable=_two_scenario_stub())
    r_live = svc_live.start_run(
        CreateRunRequest(
            repository_id="demo-repo",
            source_path="app",
            requirements="Check live LLM fail-closed",
            execution_mode=ExecutionMode.LIVE_LLM,
        )
    )
    assert r_live.state == WorkflowState.FAILED
    assert r_live.error is not None
    assert r_live.error.code == "LIVE_LLM_UNCONFIGURED"
    assert r_live.requirement_analysis is not None
    assert r_live.requirement_analysis.is_deterministic_fixture is False
    assert r_live.requirement_analysis.is_mock_output is False
    assert r_live.test_generation is None


# ---------------------------------------------------------------------------
# 6, 9: Rejected proposals & invalid scenario identifier
# ---------------------------------------------------------------------------


def test_rejected_test_proposals_and_invalid_scenario_id(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo)
    marker = tmp_path / "must_not_run.txt"
    svc = _make_service(cfg, offline_model_callable=_two_scenario_stub(marker))

    run = svc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check rejection")
    )
    assert run.state == WorkflowState.AWAITING_TEST_REVIEW

    # Invalid scenario ID not belonging to this run raises ValidationInputError & keeps state intact
    with pytest.raises(ValidationInputError):
        svc.submit_test_review(
            run.run_id,
            TestReviewRequest(approved=True, selected_test_ids=["SCN-999"], reviewer="qa"),
        )
    assert svc.get_run(run.run_id).state == WorkflowState.AWAITING_TEST_REVIEW

    # Rejecting proposals transitions cleanly to REJECTED with zero export/execution
    rejected = svc.submit_test_review(
        run.run_id,
        TestReviewRequest(approved=False, reviewer="qa", notes="Wrong assumptions"),
    )
    assert rejected.state == WorkflowState.REJECTED
    assert rejected.verification is None
    assert not marker.exists()
    assert rejected.workspace_path is not None
    assert not (Path(rejected.workspace_path) / ".changeguard_generated_tests").exists()


# ---------------------------------------------------------------------------
# 10, 11: Tampered test content & modified source snapshot
# ---------------------------------------------------------------------------


def test_tampered_generated_test_content_caught_before_execution(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo)
    marker = tmp_path / "tamper_marker.txt"
    svc = _make_service(cfg, offline_model_callable=_two_scenario_stub(marker))

    run = svc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check tamper")
    )
    assert run.test_generation is not None

    # Tamper scenario test_code in persistence without updating content_sha256
    tampered_scenarios = [
        run.test_generation.scenarios[0].model_copy(
            update={"test_code": "def test_tampered():\n    assert True\n"}
        ),
        run.test_generation.scenarios[1],
    ]
    svc.store.save_run(
        run.model_copy(
            update={
                "test_generation": run.test_generation.model_copy(
                    update={"scenarios": tampered_scenarios}
                )
            }
        )
    )

    with pytest.raises(WorkflowIntegrityError) as exc_info:
        svc.submit_test_review(
            run.run_id,
            TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
        )
    assert exc_info.value.code == "TEST_CONTENT_MODIFIED"
    assert svc.get_run(run.run_id).state == WorkflowState.FAILED
    assert not marker.exists()


def test_modified_exported_file_and_modified_source_snapshot_caught(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo)
    svc = _make_service(cfg, offline_model_callable=_two_scenario_stub())

    # 11a. Modify source snapshot in workspace while paused at AWAITING_TEST_REVIEW
    run1 = svc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check snapshot")
    )
    assert run1.workspace_path is not None
    (Path(run1.workspace_path) / "app" / "billing.py").write_text(
        "def checkout(amount, customer_type='regular'):\n    return 0\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkflowIntegrityError) as exc_info:
        svc.submit_test_review(
            run1.run_id,
            TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
        )
    assert exc_info.value.code == "SOURCE_SNAPSHOT_MODIFIED"
    assert svc.get_run(run1.run_id).state == WorkflowState.FAILED

    # 10b. Modify exported test file on disk between accept_test_review and verification execution
    run2 = svc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Check disk tamper")
    )
    claimed, specs = svc.accept_test_review(
        run2.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
    )
    assert specs is not None and claimed.workspace_path is not None
    exported_file = Path(claimed.workspace_path) / specs["SCN-001"].relative_path
    exported_file.write_text("def test_evil():\n    assert True\n", encoding="utf-8")

    result = svc.execute_verification_and_report(run2.run_id, specs)
    assert result.state == WorkflowState.FAILED
    assert result.error is not None
    assert result.error.code == "TEST_CONTENT_MODIFIED"


# ---------------------------------------------------------------------------
# 13, 14, 15, 16, 20: Failing test, collection error, zero tests, timeout, approval blocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("test_code", "expected_outcome", "expected_rec_label", "timeout"),
    [
        (
            "def test_expected_fail():\n    assert 900 == 950\n",
            VerificationOutcome.FAILED_TESTS,
            "verification_failed",
            30,
        ),
        (
            "import nonexistent_pkg_for_collection_err_xyz\ndef test_never():\n    assert True\n",
            VerificationOutcome.COLLECTION_ERROR,
            "verification_incomplete",
            30,
        ),
        (
            "def test_only_helper():\n    pass\n".replace("test_only_helper", "helper_only"),
            VerificationOutcome.NO_TESTS_COLLECTED,
            "verification_incomplete",
            30,
        ),
        (
            "import time\ndef test_slow_timeout():\n    time.sleep(5)\n",
            VerificationOutcome.TIMEOUT,
            "verification_incomplete",
            1,
        ),
    ],
)
def test_non_passing_verification_outcomes_block_final_approval(
    tmp_path,
    test_code: str,
    expected_outcome: VerificationOutcome,
    expected_rec_label: str,
    timeout: int,
) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo, trusted=True, timeout=timeout)

    # Note: for NO_TESTS_COLLECTED we need M03 AST check to pass (needs def test_*),
    # so use a test class with __init__ (pytest collects 0 tests with warning, exit_code=5)
    if expected_outcome == VerificationOutcome.NO_TESTS_COLLECTED:
        effective_code = (
            "def test_placeholder_renamed():\n"
            "    assert True\n"
            "del test_placeholder_renamed\n"
        )
    else:
        effective_code = test_code

    stub = lambda _p: json.dumps(  # noqa: E731
        {
            "scenarios": [
                {
                    "title": f"Scenario for {expected_outcome.value}",
                    "rationale": "Verify truthful verification handling",
                    "assumptions": "Deterministic offline stub",
                    "test_code": effective_code,
                }
            ]
        }
    )

    svc = _make_service(cfg, offline_model_callable=stub)
    run = svc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Verify non-pass")
    )
    assert run.state == WorkflowState.AWAITING_TEST_REVIEW

    verified = svc.submit_test_review(
        run.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
    )
    assert verified.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert verified.verification is not None
    assert verified.verification.verification_passed is False
    assert verified.verification.verification_outcome == expected_outcome
    assert verified.report is not None
    assert verified.report.recommendation_label == expected_rec_label

    # Ordinary APPROVE must be blocked when verification did not pass
    with pytest.raises(ReviewConflictError) as exc_info:
        svc.submit_final_decision(
            run.run_id,
            HumanDecisionRequest(
                decision=HumanDecision.APPROVE,
                reviewer="lead",
                comments="Trying to override failed verification",
            ),
        )
    assert exc_info.value.code == "VERIFICATION_APPROVAL_BLOCKED"
    assert svc.get_run(run.run_id).state == WorkflowState.AWAITING_FINAL_APPROVAL

    # NEEDS_REVIEW is accepted
    escalated = svc.submit_final_decision(
        run.run_id,
        HumanDecisionRequest(
            decision=HumanDecision.NEEDS_REVIEW,
            reviewer="lead",
            comments=f"Escalating due to {expected_outcome.value}.",
        ),
    )
    assert escalated.state == WorkflowState.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 17, 18, 19, 21: State transitions, duplicate & concurrent approvals, repeated decisions
# ---------------------------------------------------------------------------


def test_invalid_transitions_duplicate_review_concurrent_approval_and_repeated_decision(
    tmp_path,
) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo, trusted=True)

    verify_call_count = 0
    count_lock = threading.Lock()

    class CountingVerificationAdapter(VerificationAdapter):
        def verify(self, *args, **kwargs):
            nonlocal verify_call_count
            with count_lock:
                verify_call_count += 1
            return super().verify(*args, **kwargs)

    svc = _make_service(
        cfg,
        offline_model_callable=_two_scenario_stub(),
        verification_adapter=CountingVerificationAdapter(cfg),
    )
    run = svc.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Concurrency check")
    )
    assert run.state == WorkflowState.AWAITING_TEST_REVIEW

    # 17. Invalid direct state transition (skipping verification & report)
    with pytest.raises(InvalidStateTransitionError):
        svc.transition_state(
            run.run_id,
            WorkflowState.AWAITING_TEST_REVIEW,
            WorkflowState.APPROVED,
        )

    # 19. Concurrent approval attempts: only ONE may win and trigger M04 once
    barrier = threading.Barrier(4)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def _attempt_approve(idx: int) -> None:
        barrier.wait()
        try:
            svc.submit_test_review(
                run.run_id,
                TestReviewRequest(
                    approved=True,
                    selected_test_ids=["SCN-001"],
                    reviewer=f"reviewer-{idx}",
                ),
            )
            with outcomes_lock:
                outcomes.append("won")
        except ReviewConflictError:
            with outcomes_lock:
                outcomes.append("conflict")

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_attempt_approve, i) for i in range(4)]
        for fut in futs:
            fut.result()

    assert outcomes.count("won") == 1
    assert outcomes.count("conflict") == 3
    assert verify_call_count == 1

    # 18. Duplicate review request after transition is rejected
    with pytest.raises(ReviewConflictError):
        svc.submit_test_review(
            run.run_id,
            TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="late"),
        )

    # 21. Final decision accepted once, repeated decision rejected
    done = svc.submit_final_decision(
        run.run_id,
        HumanDecisionRequest(
            decision=HumanDecision.APPROVE,
            reviewer="lead",
            comments="Approved after single execution.",
        ),
    )
    assert done.state == WorkflowState.APPROVED

    with pytest.raises(ReviewConflictError):
        svc.submit_final_decision(
            run.run_id,
            HumanDecisionRequest(
                decision=HumanDecision.REJECT,
                reviewer="lead-2",
                comments="Attempted conflicting decision.",
            ),
        )

    # Terminal state rejects any further transition
    with pytest.raises(InvalidStateTransitionError):
        svc.transition_state(run.run_id, WorkflowState.APPROVED, WorkflowState.FAILED)


# ---------------------------------------------------------------------------
# 23: Restart behavior (paused runs persist; interrupted active runs marked FAILED)
# ---------------------------------------------------------------------------


def test_restart_preserves_paused_runs_and_recovers_interrupted_active_runs(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    cfg = _make_config(tmp_path, repo=repo, trusted=True)

    svc1 = _make_service(cfg, offline_model_callable=_two_scenario_stub())
    paused_run = svc1.start_run(
        CreateRunRequest(repository_id="demo-repo", source_path="app", requirements="Restart test")
    )
    assert paused_run.state == WorkflowState.AWAITING_TEST_REVIEW

    # Simulate an interrupted active run left in ANALYZING_CODEBASE when process crashed
    interrupted = WorkflowRunRecord(
        run_id="run_interrupted_001",
        state=WorkflowState.ANALYZING_CODEBASE,
        requirements="Interrupted mid-stage",
        repository=RepositoryInfo(
            repository_id="demo-repo",
            repository_path=str(repo),
            source_path="app",
            base_ref="HEAD",
            change_mode="working_tree",
        ),
    )
    svc1.store.save_run(interrupted)

    # Simulate backend restart with brand new service & RunStore
    svc2 = _make_service(cfg, offline_model_callable=_two_scenario_stub())
    recovered = svc2.recover_interrupted_runs()
    assert [r.run_id for r in recovered] == ["run_interrupted_001"]
    assert recovered[0].state == WorkflowState.FAILED
    assert recovered[0].error is not None
    assert recovered[0].error.code == "PROCESS_INTERRUPTED"

    # Paused human-review run remains intact and completes cleanly after restart
    reloaded = svc2.get_run(paused_run.run_id)
    assert reloaded.state == WorkflowState.AWAITING_TEST_REVIEW
    after_review = svc2.submit_test_review(
        paused_run.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa-after-restart"),
    )
    assert after_review.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert after_review.verification is not None
    assert after_review.verification.verification_passed is True


# ---------------------------------------------------------------------------
# Security: Repository restriction & unknown run
# ---------------------------------------------------------------------------


def test_disallowed_repository_and_unknown_run_errors(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    outside = tmp_path / "outside"
    _create_git_fixture(outside, modify=True)

    cfg = _make_config(tmp_path, repo=repo, trusted=True)
    svc = _make_service(cfg)

    with pytest.raises(RepositoryAccessDeniedError):
        svc.create_run(
            CreateRunRequest(
                repository_path=str(outside),
                source_path="app",
                requirements="Unauthorized repo",
            )
        )

    with pytest.raises(RunNotFoundError):
        svc.get_run("run_does_not_exist")

    with pytest.raises(RunNotFoundError):
        svc.get_run_logs("run_does_not_exist")
