import subprocess
import sys
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]


def run_tests(project_name):
    repo_path = root / "sample_projects" / project_name

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            return "passed"

        if result.returncode == 1:
            return "failed"

        return "execution_error"

    except (subprocess.TimeoutExpired, OSError):
        return "execution_error"


def detect_regression(modified_status=None):
    baseline = run_tests("refund_baseline")

    modified = (
        modified_status
        if modified_status is not None
        else run_tests("refund_demo")
    )

    if "execution_error" in [baseline, modified]:
        status = "Verification incomplete"

    elif baseline == "passed" and modified == "failed":
        status = "Regression detected"

    elif baseline == "passed" and modified == "passed":
        status = "No regression observed"

    elif baseline == "failed" and modified == "passed":
        status = "Previously failing tests now pass"

    else:
        status = "Both versions contain failures"

    return {
        "baseline_status": baseline,
        "modified_status": modified,
        "regression_status": status
    }


if __name__ == "__main__":
    print(json.dumps(detect_regression(), indent=4))