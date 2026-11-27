
"""Automated tests for dynamic code coverage integration."""

import hashlib
import json

import pytest

from project.marsadh_orchestrator.adapters import VerificationAdapter
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.schemas import ApprovedTestSpec, VerificationOutcome
from project.nafris_verification.coverage_analyzer import analyze_coverage


@pytest.mark.parametrize(
    "percentage", [True, "75", None, float("nan"), float("inf"), -1, 101]
)
def test_malformed_report_cannot_become_measured_coverage(tmp_path, percentage):
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"totals": {
            "percent_covered": percentage,
            "num_statements": 4,
            "covered_lines": 3,
            "missing_lines": 1,
        }}),
        encoding="utf-8",
    )
    assert analyze_coverage(report)["status"] == "unavailable"


@pytest.fixture
def verification_setup(tmp_path):
    workspace = tmp_path / "trusted_workspace"
    workspace.mkdir()

    test_file = workspace / "test_sample.py"
    test_file.write_text(
        "def test_sample():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    config = OrchestratorConfig(
        database_path=tmp_path / "coverage_test.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[tmp_path],
        trusted_execution_roots=[tmp_path],
        verification_timeout_seconds=30,
    )

    registry = {
        "SCN-001": ApprovedTestSpec(
            scenario_id="SCN-001",
            relative_path="test_sample.py",
            expected_sha256=hashlib.sha256(
                test_file.read_bytes()
            ).hexdigest(),
            approved=True,
        )
    }

    return VerificationAdapter(config), workspace, registry


@pytest.mark.parametrize(
    "percentage, expected_status, expected_percentage",
    [
        (80.0, "measured", 80.0),
        (0.0, "measured", 0.0),
        (100.0, "measured", 100.0),
        (-10.0, "unavailable", None),
        (120.0, "unavailable", None),
        (None, "unavailable", None),
        ("invalid", "unavailable", None),
        (True, "unavailable", None),
        (float("nan"), "unavailable", None),
    ],
)
def test_coverage_percentage_validation(
    verification_setup,
    monkeypatch,
    percentage,
    expected_status,
    expected_percentage,
):
    verifier, workspace, registry = verification_setup

    monkeypatch.setattr(
        "project.marsadh_orchestrator.adapters."
        "verification_adapter.run_reviewed_tests",
        lambda *_a, **_kw: {
            "status": "completed",
            "exit_code": 0,
            "total_tests": 1,
            "passed": 1,
            "failed": 0,
            "duration": 0.01,
            "failures": [],
            "stdout": "",
            "stderr": "",
            "coverage": {
                "status": "success",
                "coverage_percentage": percentage,
            },
        },
    )

    result = verifier.verify(
        workspace,
        ["SCN-001"],
        registry,
        is_trusted_workspace=True,
    )

    assert result.verification_passed is True
    assert result.coverage_status == expected_status
    assert result.coverage_percent == expected_percentage


@pytest.mark.parametrize(
    "coverage_data",
    [
        None,
        {},
        {"status": "unavailable"},
        {"status": "success"},
    ],
)
def test_missing_coverage(
    verification_setup,
    monkeypatch,
    coverage_data,
):
    verifier, workspace, registry = verification_setup

    raw_result = {
        "status": "completed",
        "exit_code": 0,
        "total_tests": 1,
        "passed": 1,
        "failed": 0,
        "duration": 0.01,
        "failures": [],
        "stdout": "",
        "stderr": "",
    }

    if coverage_data is not None:
        raw_result["coverage"] = coverage_data

    monkeypatch.setattr(
        "project.marsadh_orchestrator.adapters."
        "verification_adapter.run_reviewed_tests",
        lambda *_a, **_kw: raw_result,
    )

    result = verifier.verify(
        workspace,
        ["SCN-001"],
        registry,
        is_trusted_workspace=True,
    )

    assert result.verification_passed is True
    assert result.coverage_status == "unavailable"
    assert result.coverage_percent is None


@pytest.mark.parametrize(
    "exit_code, status, expected_outcome",
    [
        (1, "completed", VerificationOutcome.FAILED_TESTS),
        (2, "completed", VerificationOutcome.COLLECTION_ERROR),
        (3, "completed", VerificationOutcome.EXECUTION_ERROR),
        (5, "completed", VerificationOutcome.NO_TESTS_COLLECTED),
        (None, "timeout", VerificationOutcome.TIMEOUT),
    ],
)
def test_coverage_with_execution_outcomes(
    verification_setup,
    monkeypatch,
    exit_code,
    status,
    expected_outcome,
):
    verifier, workspace, registry = verification_setup

    monkeypatch.setattr(
        "project.marsadh_orchestrator.adapters."
        "verification_adapter.run_reviewed_tests",
        lambda *_a, **_kw: {
            "status": status,
            "exit_code": exit_code,
            "total_tests": 1 if exit_code == 1 else 0,
            "passed": 0,
            "failed": 1 if exit_code == 1 else 0,
            "duration": 0.01,
            "failures": [],
            "stdout": "Test execution evidence",
            "stderr": "",
            "coverage": {
                "status": "success",
                "coverage_percentage": 80.0,
            },
        },
    )

    result = verifier.verify(
        workspace,
        ["SCN-001"],
        registry,
        is_trusted_workspace=True,
    )

    assert result.verification_passed is False
    assert result.verification_outcome == expected_outcome

    if exit_code == 1:
        assert result.coverage_status == "measured"
        assert result.coverage_percent == 80.0
    else:
        assert result.coverage_status == "unavailable"
        assert result.coverage_percent is None


@pytest.mark.parametrize("expected_value", [900, 1000])
def test_real_coverage_execution(
    verification_setup,
    expected_value,
):
    verifier, workspace, registry = verification_setup

    source_file = workspace / "billing.py"
    source_file.write_text(
        "def calculate_payment(amount):\n"
        "    return amount * 0.9\n"
        "\n"
        "def unused_function():\n"
        "    return 0\n",
        encoding="utf-8",
    )

    test_file = workspace / "test_sample.py"
    test_file.write_text(
        "from billing import calculate_payment\n"
        "\n"
        "def test_payment():\n"
        f"    assert calculate_payment(1000) == {expected_value}\n",
        encoding="utf-8",
    )

    registry["SCN-001"] = ApprovedTestSpec(
        scenario_id="SCN-001",
        relative_path="test_sample.py",
        expected_sha256=hashlib.sha256(
            test_file.read_bytes()
        ).hexdigest(),
        approved=True,
    )

    result = verifier.verify(
        workspace,
        ["SCN-001"],
        registry,
        is_trusted_workspace=True,
    )

    assert result.execution_status == "completed"
    assert result.coverage_status == "measured"
    assert result.coverage_percent == 75.0

    if expected_value == 900:
        assert result.verification_passed is True
        assert result.passed == 1
    else:
        assert result.verification_passed is False
        assert result.failed == 1
