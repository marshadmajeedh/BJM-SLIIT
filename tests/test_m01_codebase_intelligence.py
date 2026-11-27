import json
import subprocess
from pathlib import Path

import pytest

from project.agents.mujahidh_codebase.analyzer import analyze_repository
from project.agents.mujahidh_codebase.codebase_agent import run_codebase_agent
from project.agents.mujahidh_codebase.impact_agent import analyze_impact
from project.agents.mujahidh_codebase.risk_reasoning_agent import analyze_regression_risk, validate_report


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def init_repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    return tmp_path


def test_ast_and_package_analysis(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "class Service:\n    def run(self):\n        return 1\n\ndef helper():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "bad.py").write_text("def broken(:\n", encoding="utf-8")

    report = analyze_repository(str(tmp_path))

    assert report["total_files"] == 3
    assert any(error["path"] == "pkg/bad.py" for error in report["errors"])
    core = next(item for item in report["files"] if item["path"] == "pkg/core.py")
    assert "Service" in [item["name"] for item in core["classes"]]
    assert "Service.run" in [item["name"] for item in core["functions"]]


def test_absolute_relative_and_indirect_dependencies(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (pkg / "middle.py").write_text("from .base import VALUE\n", encoding="utf-8")
    (pkg / "top.py").write_text("from pkg.middle import VALUE\n", encoding="utf-8")

    impact = analyze_impact(str(tmp_path), "pkg/base.py")

    assert impact["direct_count"] == 1
    assert impact["potentially_impacted_files"][0]["file"] == "pkg/middle.py"
    indirect = analyze_impact(str(tmp_path), "pkg/base.py")
    assert indirect["indirect_count"] == 1
    assert any(item["file"] == "pkg/top.py" for item in indirect["potentially_impacted_files"])


def test_dependency_cycles_are_safe(tmp_path):
    (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import a\n", encoding="utf-8")

    impact = analyze_impact(str(tmp_path), "a.py")
    assert impact["total_impacted"] == 1


def test_added_deleted_and_modified_git_changes(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "billing.py").write_text("def checkout():\n    return 10\n", encoding="utf-8")
    git(tmp_path, "add", "billing.py")
    git(tmp_path, "commit", "-m", "baseline")

    (tmp_path / "billing.py").write_text("def checkout():\n    return 15\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("def added():\n    return 1\n", encoding="utf-8")
    git(tmp_path, "add", "new.py")

    report = run_codebase_agent(str(tmp_path), ".", "HEAD")
    statuses = {item["change_status"]: item for item in report["reports"]}
    assert statuses["modified"]["changed_functions"] == ["checkout"]
    assert statuses["added"]["changed_functions"] == ["added"]

    (tmp_path / "billing.py").unlink()
    git(tmp_path, "add", "-u", "billing.py")
    deleted = run_codebase_agent(str(tmp_path), ".", "HEAD")
    assert any(item["change_status"] == "deleted" for item in deleted["reports"])


def test_no_changes_and_invalid_source_path(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "one.py").write_text("x = 1\n", encoding="utf-8")
    git(tmp_path, "add", "one.py")
    git(tmp_path, "commit", "-m", "baseline")

    report = run_codebase_agent(str(tmp_path), ".", "HEAD")
    assert report["reports"] == []
    with pytest.raises(ValueError, match="Source directory not found"):
        run_codebase_agent(str(tmp_path), "missing", "HEAD")


def test_risk_json_validation_and_mock_llm(monkeypatch):
    valid = {
        "summary": "Premium discount changed",
        "affected_files": ["billing.py"],
        "potential_risks": [{
            "file": "billing.py",
            "issue": "Premium checkout discount changed",
            "evidence": "Diff changes 0.90 to 0.85",
            "severity": "medium",
        }],
        "recommended_tests": ["Verify premium checkout totals"],
        "requires_verification": True,
    }
    assert validate_report(valid) == valid
    with pytest.raises(ValueError, match="Missing summary"):
        validate_report({"potential_risks": [], "recommended_tests": []})

    class Choices:
        def __init__(self):
            self.choices = [type("Choice", (), {"message": type("Message", (), {"content": json.dumps(valid)})()})()]

    class MockGroq:
        def __init__(self, **kwargs):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            assert kwargs["response_format"] == {"type": "json_object"}
            return Choices()

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr("project.agents.mujahidh_codebase.risk_reasoning_agent.Groq", MockGroq)

    report = analyze_regression_risk({}, "Premium customers receive a 15% discount", "-0.90\n+0.85")
    assert report["agent"] == "RiskReasoningAgent"
    assert report["requires_verification"] is True


def test_mini_pos_handoff_and_discount_direction(tmp_path):
    repository = init_repo(tmp_path)
    source = repository / "mini_pos"
    source.mkdir()
    billing = source / "billing.py"
    billing.write_text(
        "def apply_discount(total, customer_type):\n"
        "    if customer_type == 'premium':\n"
        "        return total * 0.90\n"
        "    return total\n",
        encoding="utf-8",
    )
    git(repository, "add", "mini_pos/billing.py")
    git(repository, "commit", "-m", "10 percent")
    billing.write_text(billing.read_text(encoding="utf-8").replace("0.90", "0.85"), encoding="utf-8")

    report = run_codebase_agent(str(repository), "mini_pos", "HEAD")
    assert report["reports"][0]["changed_functions"] == ["apply_discount"]
    diff = git(repository, "diff", "HEAD", "--", "mini_pos/billing.py")
    assert "-        return total * 0.90" in diff
    assert "+        return total * 0.85" in diff
