"""Offline unit and integration tests for M01, M03, and M04 adapters."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
    build_approved_test_specs,
)
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import RunStore
from project.marsadh_orchestrator.workspace import WorkspaceManager
from project.schemas import (
    AgentExecutionStatus,
    AgentLogEntry,
    ApprovedTestSpec,
    ExecutionMode,
    RepositoryInfo,
    TestReviewRequest,
    VerificationOutcome,
    WorkflowRunRecord,
    WorkflowState,
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for adapter tests")
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
        "def checkout(amount):\n    return amount\n", encoding="utf-8"
    )
    (pkg / "refunds.py").write_text(
        "from billing import checkout\n\ndef refund(amount):\n    return checkout(amount)\n",
        encoding="utf-8",
    )
    (pkg / "dashboard.py").write_text(
        "from refunds import refund\n\ndef summary(amount):\n    return refund(amount)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    if modify:
        (pkg / "billing.py").write_text(
            "def checkout(amount):\n    return amount * 0.9\n", encoding="utf-8"
        )


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted_config(tmp_path: Path, timeout: int = 30) -> OrchestratorConfig:
    return OrchestratorConfig(
        database_path=tmp_path / "m02.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[tmp_path],
        trusted_execution_roots=[tmp_path],
        verification_timeout_seconds=timeout,
    )


# ---------------------------------------------------------------------------
# CodebaseAdapter (M01) tests
# ---------------------------------------------------------------------------


def test_codebase_adapter_valid_change_and_path_normalization(tmp_path) -> None:
    repo = tmp_path / "repo"
    _create_git_fixture(repo, modify=True)

    adapter = CodebaseAdapter(_trusted_config(tmp_path))
    result = adapter.analyze(repo, source_path="app", base_ref="HEAD")

    assert result.status == "completed"
    assert result.has_relevant_changes is True
    assert result.total_indexed_files == 3
    assert result.changed_files_repo_relative == ["app/billing.py"]
    assert result.changed_files_source_relative == ["billing.py"]
    assert result.impacted_modules_repo_relative == ["app/dashboard.py", "app/refunds.py"]
    assert result.impacted_modules_source_relative == ["dashboard.py", "refunds.py"]
    assert result.raw_report is not None
    assert result.raw_report["agent"] == "CodebaseIntelligenceAgent"
    assert len(result.file_impacts) == 1
    assert result.file_impacts[0].changed_functions == ["checkout"]
    assert result.file_impacts[0].direct_count == 1
    assert result.file_impacts[0].indirect_count == 1


def test_codebase_adapter_empty_diff_and_invalid_base_ref(tmp_path) -> None:
    repo = tmp_path / "clean_repo"
    _create_git_fixture(repo, modify=False)

    adapter = CodebaseAdapter(_trusted_config(tmp_path))
    empty_res = adapter.analyze(repo, source_path="app", base_ref="HEAD")
    assert empty_res.status == "no_changes"
    assert empty_res.has_relevant_changes is False
    assert empty_res.changed_files_repo_relative == []
    assert empty_res.impacted_modules_repo_relative == []

    bad_ref = adapter.analyze(repo, source_path="app", base_ref="nonexistent_ref_xyz")
    assert bad_ref.status == "error"
    assert bad_ref.error is not None
    assert bad_ref.error.code == "INVALID_GIT_REF"

    flag_ref = adapter.analyze(repo, source_path="app", base_ref="--all")
    assert flag_ref.status == "error"
    assert flag_ref.error is not None
    assert flag_ref.error.code == "INVALID_GIT_REF"


# ---------------------------------------------------------------------------
# TestingAdapter (M03) tests
# ---------------------------------------------------------------------------


def test_testing_adapter_stub_generation_export_and_error_cases(tmp_path) -> None:
    repo = tmp_path / "repo"
    _create_git_fixture(repo, modify=True)
    cfg = _trusted_config(tmp_path)

    codebase_res = CodebaseAdapter(cfg).analyze(repo, source_path="app", base_ref="HEAD")
    testing = TestingAdapter(cfg)

    side_effect_marker = tmp_path / "must_not_run.txt"

    def valid_stub(_prompt: str) -> str:
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Premium discount checkout",
                        "rationale": "Verify 10% discount on billing.checkout",
                        "assumptions": "10% discount applies to amount",
                        "test_code": (
                            "import pathlib\n"
                            "from app.billing import checkout\n"
                            "def test_discount():\n"
                            f"    pathlib.Path({str(side_effect_marker)!r}).write_text('ran')\n"
                            "    assert checkout(1000) == 900\n"
                        ),
                    }
                ]
            }
        )

    gen = testing.generate_proposals(
        repo,
        codebase_res,
        "Apply 10% discount",
        execution_mode=ExecutionMode.OFFLINE_TEST,
        model_callable=valid_stub,
    )
    assert gen.status == "proposed"
    assert gen.executed is False
    assert gen.is_mock_output is True
    assert len(gen.scenarios) == 1
    assert gen.scenarios[0].scenario_id == "SCN-001"
    assert not side_effect_marker.exists(), "Generation must never execute test code"

    exported = testing.export_selected_scenarios(
        repo,
        gen,
        selected_scenario_ids=["SCN-001"],
        output_subdir=".changeguard_generated_tests/run_01",
    )
    assert exported.status == "proposed"
    assert "SCN-001" in exported.exported_files
    assert "SCN-001" in exported.exported_hashes
    assert not side_effect_marker.exists(), "Export must never execute test code"

    # Re-exporting into same non-empty directory must fail without overwriting
    overwrite_attempt = testing.export_selected_scenarios(
        repo,
        gen,
        selected_scenario_ids=["SCN-001"],
        output_subdir=".changeguard_generated_tests/run_01",
    )
    assert overwrite_attempt.status == "error"
    assert overwrite_attempt.error is not None
    assert overwrite_attempt.error.code == "EXPORT_DIR_NOT_EMPTY"

    # Invalid model JSON
    bad_json = testing.generate_proposals(
        repo,
        codebase_res,
        "Apply 10% discount",
        model_callable=lambda _p: "{not valid json",
    )
    assert bad_json.status == "error"
    assert bad_json.error is not None
    assert bad_json.error.code == "INVALID_MODEL_JSON"

    # Missing scenarios
    empty_scenarios = testing.generate_proposals(
        repo,
        codebase_res,
        "Apply 10% discount",
        model_callable=lambda _p: json.dumps({"scenarios": []}),
    )
    assert empty_scenarios.status == "error"
    assert empty_scenarios.error is not None
    assert empty_scenarios.error.code == "INVALID_MODEL_OUTPUT"

    # Empty impact report must skip M03 without invoking model
    clean_repo = tmp_path / "clean_repo"
    _create_git_fixture(clean_repo, modify=False)
    empty_cb = CodebaseAdapter(cfg).analyze(clean_repo, source_path="app", base_ref="HEAD")
    called = False

    def should_not_be_called(_p: str) -> str:
        nonlocal called
        called = True
        return "{}"

    skipped = testing.generate_proposals(
        clean_repo,
        empty_cb,
        "Check clean repo",
        model_callable=should_not_be_called,
    )
    assert skipped.status == "skipped_no_changes"
    assert called is False

    # Live LLM requested without credentials must fail explicitly (no silent mock fallback)
    live_fail = testing.generate_proposals(
        repo,
        codebase_res,
        "Live call",
        execution_mode=ExecutionMode.LIVE_LLM,
    )
    assert live_fail.status == "error"
    assert live_fail.is_mock_output is False
    assert live_fail.error is not None
    assert live_fail.error.code == "LIVE_LLM_UNCONFIGURED"


# ---------------------------------------------------------------------------
# VerificationAdapter (M04) tests
# ---------------------------------------------------------------------------


def test_verification_adapter_does_not_import_forbidden_m04_modules() -> None:
    adapter_file = (
        Path(__file__).resolve().parent.parent
        / "project"
        / "marsadh_orchestrator"
        / "adapters"
        / "verification_adapter.py"
    )
    tree = ast.parse(adapter_file.read_text(encoding="utf-8"))
    forbidden = {
        "verification_agent",
        "benchmark_engine",
        "test_runner",
        "ai_analyzer",
        "regression_detector",
    }
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module)
    for mod in imported_names:
        segments = set(mod.split("."))
        overlap = segments & forbidden
        assert not overlap, f"Forbidden M04 module imported: {mod}"


def test_verification_adapter_pass_fail_collection_zero_timeout_and_guards(
    tmp_path, monkeypatch
) -> None:
    ws = tmp_path / "trusted_ws"
    ws.mkdir()
    cfg = _trusted_config(tmp_path, timeout=30)
    verifier = VerificationAdapter(cfg)

    def _register(rel_name: str, content: str, *, approved: bool = True) -> tuple[Path, dict[str, ApprovedTestSpec]]:
        p = ws / rel_name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        spec = ApprovedTestSpec(
            scenario_id="SCN-001",
            relative_path=rel_name,
            expected_sha256=_sha256_file(p),
            approved=approved,
        )
        return p, {"SCN-001": spec}

    # 1. Passing test
    _, reg_pass = _register("test_pass.py", "def test_ok():\n    assert 2 + 2 == 4\n")
    res_pass = verifier.verify(ws, ["SCN-001"], reg_pass, is_trusted_workspace=True)
    assert res_pass.execution_status == "completed"
    assert res_pass.verification_outcome == VerificationOutcome.PASSED
    assert res_pass.verification_passed is True
    assert (res_pass.exit_code, res_pass.total_tests, res_pass.passed, res_pass.failed) == (0, 1, 1, 0)
    assert res_pass.coverage_status == "unavailable"
    assert res_pass.coverage_percent is None
    assert res_pass.baseline_comparison.status == "unavailable"

    # 2. Failing test
    _, reg_fail = _register("test_fail.py", "def test_bad():\n    assert 1 == 2\n")
    res_fail = verifier.verify(ws, ["SCN-001"], reg_fail, is_trusted_workspace=True)
    assert res_fail.execution_status == "completed"
    assert res_fail.verification_outcome == VerificationOutcome.FAILED_TESTS
    assert res_fail.verification_passed is False
    assert (res_fail.exit_code, res_fail.passed, res_fail.failed) == (1, 0, 1)
    assert len(res_fail.failures) == 1
    assert res_fail.failures[0].error_type == "AssertionError"

    # 3. Collection error (syntax / import error — report_parser omits these from failures!)
    _, reg_col = _register(
        "test_collection_err.py",
        "import nonexistent_pkg_xyz_12345\ndef test_never_runs():\n    assert True\n",
    )
    res_col = verifier.verify(ws, ["SCN-001"], reg_col, is_trusted_workspace=True)
    assert res_col.verification_passed is False
    assert res_col.verification_outcome == VerificationOutcome.COLLECTION_ERROR
    assert res_col.exit_code == 2
    assert len(res_col.failures) >= 1

    # 4. Zero tests collected (exit_code == 5)
    _, reg_zero = _register("test_empty.py", "def helper_not_a_test():\n    return 1\n")
    res_zero = verifier.verify(ws, ["SCN-001"], reg_zero, is_trusted_workspace=True)
    assert res_zero.verification_passed is False
    assert res_zero.verification_outcome == VerificationOutcome.NO_TESTS_COLLECTED
    assert res_zero.exit_code == 5

    # 5. Timeout result
    _, reg_timeout = _register(
        "test_slow.py",
        "import time\ndef test_sleep():\n    time.sleep(5)\n",
    )
    res_timeout = verifier.verify(
        ws,
        ["SCN-001"],
        reg_timeout,
        timeout_seconds=1,
        is_trusted_workspace=True,
    )
    assert res_timeout.execution_status == "timeout"
    assert res_timeout.verification_outcome == VerificationOutcome.TIMEOUT
    assert res_timeout.verification_passed is False
    assert res_timeout.error is not None
    assert res_timeout.error.code == "VERIFICATION_TIMEOUT"

    # 6. Unauthorized test path (traversal)
    bad_path_reg = {
        "SCN-001": ApprovedTestSpec(
            scenario_id="SCN-001",
            relative_path="../test_outside.py",
            expected_sha256="a" * 64,
            approved=True,
        )
    }
    res_path = verifier.verify(ws, ["SCN-001"], bad_path_reg, is_trusted_workspace=True)
    assert res_path.execution_status == "not_executed"
    assert res_path.verification_passed is False
    assert res_path.error is not None
    assert res_path.error.code == "UNAUTHORIZED_TEST_PATH"

    # 7. Unapproved test
    _, reg_unapproved = _register(
        "test_unapproved.py", "def test_x():\n    assert True\n", approved=False
    )
    res_unapp = verifier.verify(ws, ["SCN-001"], reg_unapproved, is_trusted_workspace=True)
    assert res_unapp.execution_status == "not_executed"
    assert res_unapp.error is not None
    assert res_unapp.error.code == "UNAPPROVED_TEST"

    # 8. Modified test content after approval
    tamper_path, reg_tamper = _register(
        "test_tampered.py", "def test_original():\n    assert True\n"
    )
    tamper_path.write_text("def test_original():\n    assert False\n", encoding="utf-8")
    res_tamper = verifier.verify(ws, ["SCN-001"], reg_tamper, is_trusted_workspace=True)
    assert res_tamper.execution_status == "not_executed"
    assert res_tamper.error is not None
    assert res_tamper.error.code == "TEST_CONTENT_MODIFIED"

    # 9. Untrusted workspace rejected
    res_untrusted = verifier.verify(ws, ["SCN-001"], reg_pass, is_trusted_workspace=False)
    assert res_untrusted.execution_status == "not_executed"
    assert res_untrusted.error is not None
    assert res_untrusted.error.code == "UNTRUSTED_WORKSPACE"

    # 10. Nonzero exit code with failed=0 and failures=[] must NEVER pass
    monkeypatch.setattr(
        "project.marsadh_orchestrator.adapters.verification_adapter.run_reviewed_tests",
        lambda *_a, **_kw: {
            "status": "completed",
            "exit_code": 3,
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "duration": 0.01,
            "failures": [],
            "stdout": "INTERNALERROR",
            "stderr": "",
        },
    )
    res_nz = verifier.verify(ws, ["SCN-001"], reg_pass, is_trusted_workspace=True)
    assert res_nz.verification_passed is False
    assert res_nz.verification_outcome == VerificationOutcome.EXECUTION_ERROR
    assert res_nz.exit_code == 3


# ---------------------------------------------------------------------------
# End-to-end offline M02 foundation integration test
# ---------------------------------------------------------------------------


def test_end_to_end_m02_foundation_handoff_with_workspace_and_store(tmp_path) -> None:
    repo = tmp_path / "repos" / "mini_shop"
    _create_git_fixture(repo, modify=True)

    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / "runs.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[tmp_path / "repos"],
        trusted_execution_roots=[tmp_path / "repos"],
        verification_timeout_seconds=30,
    )
    store = RunStore(cfg.database_path)
    ws_manager = WorkspaceManager(cfg)
    codebase_adapter = CodebaseAdapter(cfg)
    testing_adapter = TestingAdapter(cfg)
    verification_adapter = VerificationAdapter(cfg)

    run_id = "run_e2e_001"
    ctx = ws_manager.prepare_workspace(run_id, repo, source_path="app", base_ref="HEAD")
    assert ctx.is_trusted_for_execution is True

    run = WorkflowRunRecord(
        run_id=run_id,
        state=WorkflowState.CREATED,
        requirements="Apply 10% discount to checkout",
        repository=RepositoryInfo(
            repository_path=ctx.original_repository_path,
            source_path=ctx.source_path,
            base_ref=ctx.base_ref,
            resolved_base_commit=ctx.resolved_base_commit,
            change_mode=ctx.change_mode,
        ),
        workspace_id=ctx.workspace_id,
        workspace_path=ctx.workspace_repo_path,
    )
    store.save_run(run)

    # Stage 1: M01 Codebase Analysis inside isolated workspace
    store.update_state(run_id, WorkflowState.ANALYZING_CODEBASE)
    cb_result = codebase_adapter.analyze(
        ctx.workspace_repo_path,
        source_path=ctx.source_path,
        base_ref=ctx.effective_base_ref,
    )
    assert cb_result.status == "completed"
    assert cb_result.changed_files_repo_relative == ["app/billing.py"]
    store.append_log(
        AgentLogEntry(
            run_id=run_id,
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_CODEBASE,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=10,
            input_summary="app @ HEAD",
            output_summary="1 changed file, 2 impacted modules",
        )
    )

    # Stage 2: M03 Test Generation + Export (offline deterministic stub)
    store.update_state(run_id, WorkflowState.GENERATING_TESTS)

    def stub_model(prompt: str) -> str:
        assert "checkout" in prompt
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Checkout applies 10% discount",
                        "rationale": "billing.checkout changed",
                        "assumptions": "10% discount on numeric amount",
                        "test_code": (
                            "import sys\n"
                            "from pathlib import Path\n"
                            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))\n"
                            "from billing import checkout\n"
                            "def test_checkout_discount():\n"
                            "    assert checkout(1000) == 900\n"
                        ),
                    }
                ]
            }
        )

    gen_result = testing_adapter.generate_proposals(
        ctx.workspace_repo_path,
        cb_result,
        run.requirements,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        model_callable=stub_model,
    )
    assert gen_result.status == "proposed"
    exported = testing_adapter.export_selected_scenarios(
        ctx.workspace_repo_path,
        gen_result,
        output_subdir=".changeguard_generated_tests",
    )
    assert exported.status == "proposed"
    assert "SCN-001" in exported.exported_files

    # Stage 3: Human test review & verification
    review = TestReviewRequest(
        approved=True,
        selected_test_ids=["SCN-001"],
        reviewer="qa-reviewer",
        notes="Verified discount expectation",
    )
    specs = build_approved_test_specs(exported, review)
    ver_result = verification_adapter.verify(
        ctx.workspace_repo_path,
        review.selected_test_ids,
        specs,
        is_trusted_workspace=ctx.is_trusted_for_execution,
        baseline_ref=ctx.resolved_base_commit,
    )
    assert ver_result.verification_passed is True
    assert ver_result.verification_outcome == VerificationOutcome.PASSED
    assert (ver_result.passed, ver_result.failed, ver_result.exit_code) == (1, 0, 0)

    # Persist final evidence and verify reload
    final_run = store.get_run(run_id)
    assert final_run is not None
    store.save_run(
        final_run.model_copy(
            update={
                "state": WorkflowState.AWAITING_FINAL_APPROVAL,
                "codebase_analysis": cb_result,
                "test_generation": exported,
                "verification": ver_result,
            }
        )
    )
    reloaded = RunStore(cfg.database_path).get_run(run_id)
    assert reloaded is not None
    assert reloaded.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert reloaded.verification is not None
    assert reloaded.verification.verification_passed is True
    assert len(RunStore(cfg.database_path).get_logs(run_id)) == 1

    # Original repository has no exported tests and remains untouched
    assert not (repo / ".changeguard_generated_tests").exists()
    assert ws_manager.cleanup_workspace(ctx.workspace_repo_path) is True
