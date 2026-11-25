import subprocess
import sys
import json
from pathlib import Path

repo_path = (
    Path(__file__).resolve().parents[2]
    / "sample_projects/refund_demo"
)

report_path = repo_path / "report.json"
coverage_path = repo_path / "coverage.json"

report_path.unlink(missing_ok=True)
coverage_path.unlink(missing_ok=True)

result = subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "--json-report",
        "--json-report-file=report.json",
        "--cov=billing",
        "--cov=refunds",
        "--cov-report=json:coverage.json"
    ],
    cwd=repo_path,
    capture_output=True,
    text=True,
    timeout=60
)

if report_path.exists():
    with open(report_path, encoding="utf-8") as file:
        report = json.load(file)

    print(json.dumps(report["summary"], indent=4))
    print("Exit code:", result.returncode)

else:
    print(result.stdout)
    print(result.stderr)
    sys.exit(2)

if coverage_path.exists():
    with open(coverage_path, encoding="utf-8") as file:
        coverage = json.load(file)

    print(
        "Code Coverage:",
        coverage["totals"]["percent_covered"],
        "%"
    )