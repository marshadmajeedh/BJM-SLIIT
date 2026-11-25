from pathlib import Path

import pytest

from project.agents.munshif_testing.export import export_scenarios
from project.nafris_verification.generated_test_runner import run_reviewed_tests


def test_export_and_verify_reports_actual_failure(tmp_path):
    proposals = {"status": "proposed", "executed": False, "scenarios": [
        {"test_code": "def test_pass():\n    assert 1 == 1\n"},
        {"test_code": "def test_fail():\n    assert 1 == 2\n"},
    ]}
    paths = export_scenarios(proposals, tmp_path / "review")
    result = run_reviewed_tests(tmp_path, [Path(p).relative_to(tmp_path) for p in paths])
    assert result["status"] == "completed"
    assert (result["passed"], result["failed"], result["exit_code"]) == (1, 1, 1)
    assert "assert 1 == 2" in result["failures"][0]["evidence"]


def test_handoff_rejects_outside_files_and_overwrite(tmp_path):
    proposal = {"status": "proposed", "executed": False, "scenarios": [{"test_code": "def test_ok(): pass"}]}
    export_scenarios(proposal, tmp_path / "review")
    with pytest.raises(ValueError):
        export_scenarios(proposal, tmp_path / "review")
    with pytest.raises(ValueError):
        run_reviewed_tests(tmp_path, ["../test_outside.py"])
