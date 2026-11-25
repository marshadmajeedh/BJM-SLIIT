import subprocess
import sys
import json
from pathlib import Path
from report_parser import parse_report
from failure_analyzer import analyze_failure
from ai_analyzer import analyze_with_ai
from risk_scorer import calculate_risk
from coverage_analyzer import analyze_coverage
from regression_detector import detect_regression

folder = Path(__file__).resolve().parent


def verify():
    subprocess.run(
        [sys.executable, str(folder / "test_runner.py")],
        check=True
    )

    repo_path = (
        folder.parents[1]
        / "sample_projects/refund_demo"
    )

    report_path = repo_path / "report.json"
    coverage_path = repo_path / "coverage.json"

    result = parse_report(report_path)

    result["coverage"] = analyze_coverage(coverage_path)

    result["regression"] = detect_regression()

    for failure in result["failures"]:

        failure["analysis"] = analyze_failure(
            failure["evidence"]
        )

        try:
            failure["ai_analysis"] = analyze_with_ai(
                failure["evidence"]
            )
            failure["ai_status"] = "success"

        except Exception as error:
            failure["ai_analysis"] = None
            failure["ai_status"] = "failed"
            failure["ai_error"] = type(error).__name__

        failure["risk_assessment"] = calculate_risk(
            failure["ai_analysis"]
        )

    return result


if __name__ == "__main__":
    result = verify()
    print(json.dumps(result, indent=4))