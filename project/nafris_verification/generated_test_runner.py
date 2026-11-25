"""Run explicitly reviewed tests and return pytest evidence.

Run this inside an appropriately isolated environment. This module does not
provide a security sandbox and executes Python from the specified repository.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from .report_parser import parse_report


def run_reviewed_tests(repository_path, test_files, timeout=60):
    root = Path(repository_path).resolve()
    if not root.is_dir() or not test_files:
        raise ValueError("A repository and at least one reviewed test are required")
    if timeout <= 0:
        raise ValueError("Timeout must be positive")
    tests = []
    for name in test_files:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or not path.name.startswith("test_") or path.suffix != ".py":
            raise ValueError("Test files must be test_*.py inside the repository")
        tests.append(str(path))
    with tempfile.TemporaryDirectory() as directory:
        report = Path(directory) / "report.json"
        command = [sys.executable, "-m", "pytest", *tests, "--json-report", f"--json-report-file={report}"]
        try:
            process = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "exit_code": None, "failures": [], "timeout_seconds": timeout}
        if not report.is_file():
            return {"status": "error", "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr, "failures": []}
        result = parse_report(report)
        result.update({"status": "completed", "exit_code": process.returncode, "stdout": process.stdout, "stderr": process.stderr})
        return result
