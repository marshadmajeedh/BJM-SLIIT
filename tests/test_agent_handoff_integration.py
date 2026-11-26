import json
import shutil
import subprocess
from pathlib import Path

import pytest

from project.agents.mujahidh_codebase.codebase_agent import run_codebase_agent
from project.agents.munshif_testing.agent import generate_tests
from project.agents.munshif_testing.export import export_scenarios
from project.nafris_verification.generated_test_runner import run_reviewed_tests


def test_real_git_change_to_reviewed_test_evidence(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for the codebase agent")

    def git_run(*args):
        subprocess.run(
            [git, "-C", str(tmp_path), *args], check=True, capture_output=True
        )

    git_run("init")
    git_run("config", "user.name", "Test User")
    git_run("config", "user.email", "test@example.invalid")
    source = tmp_path / "billing.py"
    source.write_text("def checkout(amount):\n    return amount\n", encoding="utf-8")
    git_run("add", "billing.py")
    git_run("commit", "-m", "baseline")
    source.write_text(
        "def checkout(amount):\n    return amount * 0.9\n", encoding="utf-8"
    )

    impact = run_codebase_agent(str(tmp_path), ".", "HEAD")
    assert impact["reports"][0]["changed_functions"] == ["checkout"]

    def model(prompt):
        assert "checkout" in prompt and "0.9" in prompt
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "discount",
                        "rationale": "changed checkout",
                        "assumptions": "10% discount",
                        "test_code": "from billing import checkout\ndef test_checkout():\n    assert checkout(1000) == 900\n",
                    }
                ]
            }
        )

    proposals = generate_tests(tmp_path, impact, model, "10% discount")
    files = export_scenarios(proposals, tmp_path / "reviewed_tests")
    # The test author has reviewed this deterministic fixture before execution.
    result = run_reviewed_tests(
        tmp_path, [Path(p).relative_to(tmp_path) for p in files]
    )
    assert result["status"] == "completed"
    assert (result["passed"], result["failed"], result["exit_code"]) == (1, 0, 0)
