import json

def parse_report(report_path):
    with open(report_path, encoding="utf-8") as file:
        report = json.load(file)

    summary = report.get("summary", {})
    failures = []

    for test in report.get("tests", []):
        if test["outcome"] == "failed":
            failures.append({
                "test_name": test["nodeid"],
                "status": "failed",
                "evidence": test.get("call", {}).get("longrepr", "")
            })

    return {
        "total_tests": summary.get("total", 0),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "exit_code": report.get("exitcode"),
        "duration": report.get("duration", 0),
        "failures": failures
    }