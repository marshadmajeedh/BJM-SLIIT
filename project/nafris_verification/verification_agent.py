import subprocess
import sys
import json
from pathlib import Path
from report_parser import parse_report
from failure_analyzer import analyze_failure

folder = Path(__file__).resolve().parent


def verify():
    subprocess.run(
        [sys.executable, str(folder / "test_runner.py")],
        check=True
    )

    report_path = (
        folder.parents[1]
        / "sample_projects/refund_demo/report.json"
    )

    result = parse_report(report_path)

    for failure in result["failures"]:
        failure["analysis"] = analyze_failure(
            failure["evidence"]
        )

    return result


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=4))