import subprocess
import sys
import json
from pathlib import Path

repo_path = (
    Path(__file__).resolve().parents[2]
    / "sample_projects"
    / "refund_demo"
)

report_path = repo_path / "report.json"

report_path.unlink(missing_ok=True)

result = subprocess.run(
    [
        sys.executable, "-m", "pytest", "tests/",
        "--json-report",
        "--json-report-file=report.json"
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
    print("Report generation failed")