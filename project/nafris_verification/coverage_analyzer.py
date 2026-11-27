
import json
import math
from pathlib import Path


def analyze_coverage(coverage_path):
    coverage_path = Path(coverage_path)

    if not coverage_path.is_file():
        return {
            "status": "unavailable",
            "reason": "Coverage report not found"
        }

    try:
        with coverage_path.open(encoding="utf-8") as file:
            report = json.load(file)

        totals = report["totals"]

        raw_percentage = totals["percent_covered"]
        if type(raw_percentage) not in (int, float):
            raise ValueError("Invalid coverage percentage")
        percentage = float(raw_percentage)
        total = totals["num_statements"]
        covered = totals["covered_lines"]
        missing = totals["missing_lines"]

        if (
            not math.isfinite(percentage)
            or not 0 <= percentage <= 100
            or any(type(n) is not int for n in
                   (total, covered, missing))
            or total <= 0
            or covered < 0
            or missing < 0
            or covered + missing != total
        ):
            raise ValueError("Invalid coverage totals")

        return {
            "status": "success",
            "coverage_percentage": round(percentage, 2),
            "total_statements": total,
            "covered_statements": covered,
            "missing_statements": missing
        }

    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return {
            "status": "unavailable",
            "reason": "Invalid coverage report"
        }
