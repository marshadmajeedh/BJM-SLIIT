import time
import json
import statistics

from verification_agent import verify
from pathlib import Path


def run_benchmark():

    start_time = time.perf_counter()

    result = verify()

    execution_time = time.perf_counter() - start_time

    return {
        "pipeline": "Verification Agent",
        "execution_time_seconds": round(execution_time, 4),
        "test_duration_seconds": result["duration"],
        "total_tests": result["total_tests"],
        "passed_tests": result["passed"],
        "failed_tests": result["failed"],
        "coverage": result["coverage"],
        "regression": result["regression"],
        "ai_success": all(
            failure.get("ai_status") == "success"
            for failure in result["failures"]
        )
    }


def run_multiple_benchmarks(runs=5):

    results = []

    for i in range(runs):

        print(f"Running benchmark {i + 1}/{runs}")

        results.append(run_benchmark())

    times = [
        result["execution_time_seconds"]
        for result in results
    ]

    return {
        "total_runs": runs,
        "average_time": round(statistics.mean(times), 4),
        "median_time": round(statistics.median(times), 4),
        "minimum_time": min(times),
        "maximum_time": max(times),
        "standard_deviation": round(
            statistics.pstdev(times), 4
        ),
        "successful_ai_runs": sum(
            result["ai_success"] for result in results
        ),
        "individual_results": results
    }
    
    


def save_benchmark_report(result):
    report_path = (
        Path(__file__).resolve().parent
        / "benchmark_report.json"
    )

    with open(report_path, "w", encoding="utf-8") as file:
        json.dump(result, file, indent=4)

    print(f"Benchmark report saved: {report_path}")


if __name__ == "__main__":

    result = run_multiple_benchmarks(5)

    save_benchmark_report(result)

    print(json.dumps(result, indent=4))