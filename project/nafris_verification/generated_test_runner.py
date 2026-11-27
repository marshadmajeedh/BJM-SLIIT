
"""Run reviewed tests and collect verification evidence.

This module must be used inside an isolated environment.
It does not provide a security sandbox.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .report_parser import parse_report
from .coverage_analyzer import analyze_coverage


def run_reviewed_tests(repository_path, test_files, timeout=60):
    root = Path(repository_path).resolve()

    if not root.is_dir() or not test_files:
        raise ValueError(
            "A repository and at least one reviewed test are required"
        )

    if timeout <= 0:
        raise ValueError("Timeout must be positive")

    tests = []

    for name in test_files:
        path = (root / name).resolve()

        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or not path.name.startswith("test_")
            or path.suffix != ".py"
        ):
            raise ValueError(
                "Test files must be test_*.py inside the repository"
            )

        tests.append(str(path))

    with tempfile.TemporaryDirectory() as directory:
        temp_dir = Path(directory)

        report = temp_dir / "report.json"
        coverage_report = temp_dir / "coverage.json"
        coverage_config = temp_dir / ".coveragerc"

        coverage_config.write_text(
            "[run]\n"
            "branch = False\n"
            "omit =\n"
            f"    {root.as_posix()}/tests/*\n"
            "    */test_*.py\n"
            "    */*_test.py\n"
            "    */conftest.py\n"
            "    */.venv/*\n"
            "    */venv/*\n"
            "    */site-packages/*\n",
            encoding="utf-8"
        )

        env = os.environ.copy()
        env["COVERAGE_FILE"] = str(temp_dir / ".coverage")

        command = [
            sys.executable,
            "-m",
            "pytest",
            *tests,
            "--json-report",
            f"--json-report-file={report}",
            f"--cov={root}",
            f"--cov-config={coverage_config}",
            f"--cov-report=json:{coverage_report}",
        ]

        try:
            process = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )

        except subprocess.TimeoutExpired:
            return {
                "status": "timeout",
                "exit_code": None,
                "failures": [],
                "timeout_seconds": timeout,
                "coverage": {
                    "status": "unavailable",
                    "reason": "Test execution timed out"
                },
            }

        if not report.is_file():
            return {
                "status": "error",
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "failures": [],
                "coverage": {
                    "status": "unavailable",
                    "reason": "Test report not generated"
                },
            }

        result = parse_report(report)

        if process.returncode in (0, 1):
            coverage = analyze_coverage(coverage_report)
        else:
            coverage = {
                "status": "unavailable",
                "reason": "Test execution incomplete"
            }

        result.update(
            {
                "status": "completed",
                "exit_code": process.returncode,
                "stdout": process.stdout,
                "stderr": process.stderr,
                "coverage": coverage,
            }
        )

        return result
