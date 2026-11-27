import json


def analyze_coverage(coverage_path):

    if not coverage_path.exists():
        return {"status": "unavailable"}

    with open(coverage_path, encoding="utf-8") as file:
        report = json.load(file)

    totals = report["totals"]

    return {
        "status": "success",
        "coverage_percentage": round(
            totals["percent_covered"], 2
        ),
        "total_statements": totals["num_statements"],
        "covered_statements": totals["covered_lines"],
        "missing_statements": totals["missing_lines"]
    }