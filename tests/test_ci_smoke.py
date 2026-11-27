"""Deterministic CI smoke tests for ChangeGuard AI repository integrity.

These tests verify foundational repository invariants, Python syntax validity,
CI workflow security/configuration guarantees, and secret hygiene without
modifying application business logic or requiring external LLM API credentials.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
}

BINARY_EXTENSIONS = {
    ".docx",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".zip",
    ".gz",
    ".whl",
    ".pyc",
    ".sqlite",
    ".db",
}

ALLOWED_ENV_NAMES = {
    ".env.example",
    ".env.sample",
    ".env.template",
}

SECRET_PATTERNS = [
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub Personal Access Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")),
    ("GitHub Fine-Grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("OpenAI API Key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b")),
    ("Anthropic API Key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "Private Key Block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    ),
]


def _iter_repo_python_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    invalid_fixtures = REPO_ROOT / "tests" / "fixtures" / "invalid"
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.is_relative_to(invalid_fixtures):
            continue
        files.append(path)
    return files


def test_expected_repository_directories_exist() -> None:
    expected_dirs = [
        REPO_ROOT / "project",
        REPO_ROOT / "project" / "agents",
        REPO_ROOT / "project" / "marsadh_orchestrator",
        REPO_ROOT / "project" / "schemas",
        REPO_ROOT / "tests",
        REPO_ROOT / "docs",
    ]
    missing = [str(d.relative_to(REPO_ROOT)) for d in expected_dirs if not d.is_dir()]
    assert not missing, f"Missing required repository directories: {missing}"


def test_all_repository_python_files_have_valid_syntax() -> None:
    python_files = _iter_repo_python_files()
    assert python_files, "Expected at least one Python file in the repository."

    errors: list[str] = []
    for path in python_files:
        try:
            source = path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{path.relative_to(REPO_ROOT)}: {exc}")

    assert not errors, "Python syntax errors detected:\n" + "\n".join(errors)


def test_project_entrypoint_is_present_and_parseable() -> None:
    entrypoint = REPO_ROOT / "project" / "main.py"
    assert entrypoint.is_file(), "project/main.py must exist."
    ast.parse(entrypoint.read_text(encoding="utf-8"), filename=str(entrypoint))


def test_ci_workflow_security_and_quality_gate_contract() -> None:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    assert workflow_path.is_file(), ".github/workflows/ci.yml must exist."

    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    assert workflow.get("name") == "ChangeGuard CI"

    # PyYAML parses unquoted YAML 1.1 `on:` key as boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "Workflow triggers ('on:') must be a mapping."
    assert "pull_request" in triggers
    assert "push" in triggers
    assert "workflow_dispatch" in triggers
    assert "pull_request_target" not in triggers, "pull_request_target must not be used."

    for event_name in ("pull_request", "push"):
        branches = triggers[event_name].get("branches", [])
        assert "develop" in branches and "main" in branches
        assert "paths" not in triggers[event_name], (
            f"Workflow-level path filters must not be set on {event_name}."
        )

    assert workflow.get("permissions") == {"contents": "read"}

    jobs = workflow.get("jobs", {})
    assert "ci-quality-gate" in jobs
    gate = jobs["ci-quality-gate"]
    assert gate.get("name") == "CI Quality Gate"
    assert gate.get("if") == "always()"

    sha_pattern = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
    for job_id, job in jobs.items():
        assert not job.get("continue-on-error", False), (
            f"Job '{job_id}' must not set continue-on-error."
        )
        assert "timeout-minutes" in job, f"Job '{job_id}' must define timeout-minutes."
        for step in job.get("steps", []):
            assert not step.get("continue-on-error", False), (
                f"Step in '{job_id}' must not set continue-on-error."
            )
            uses = step.get("uses")
            if uses:
                assert sha_pattern.match(uses), (
                    f"Action '{uses}' in job '{job_id}' must be pinned to a full 40-char commit SHA."
                )
                if uses.startswith("actions/checkout@"):
                    step_with = step.get("with", {})
                    assert step_with.get("persist-credentials") is False, (
                        f"Checkout in '{job_id}' must set persist-credentials: false."
                    )


def test_pr_template_and_ci_documentation_exist() -> None:
    pr_template = REPO_ROOT / ".github" / "pull_request_template.md"
    ci_docs = REPO_ROOT / "docs" / "CI_CD.md"

    assert pr_template.is_file() and pr_template.stat().st_size > 0
    assert ci_docs.is_file() and ci_docs.stat().st_size > 0


def test_no_committed_secrets_or_env_files() -> None:
    findings: list[str] = []
    if (REPO_ROOT / ".git").exists():
        # Local ignored files (especially .env) are not committed content.
        result = subprocess.run(
            [
                "git", "-c", f"safe.directory={REPO_ROOT.as_posix()}",
                "ls-files", "-z",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
        paths = [
            REPO_ROOT / name.decode("utf-8")
            for name in result.stdout.split(b"\0") if name
        ]
    else:
        # Source archives contain the exported repository files without .git.
        paths = list(REPO_ROOT.rglob("*"))

    for path in sorted(paths):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name not in ALLOWED_ENV_NAMES
        ):
            findings.append(f"{path.relative_to(REPO_ROOT)} [Committed .env file]")
            continue
        if path.suffix.lower() in BINARY_EXTENSIONS:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for rule_name, regex in SECRET_PATTERNS:
                if regex.search(line):
                    findings.append(
                        f"{path.relative_to(REPO_ROOT)}:{lineno} [{rule_name}]"
                    )

    assert not findings, "Potential secret or .env file detected:\n" + "\n".join(findings)
