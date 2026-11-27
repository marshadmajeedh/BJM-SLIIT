"""Offline unit tests for safe Git working-copy management (WorkspaceManager)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.workspace import (
    UnsupportedWorkspaceStateError,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceSecurityError,
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for workspace tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _init_sample_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(repo_dir, "init")
    _git(repo_dir, "config", "user.name", "Test User")
    _git(repo_dir, "config", "user.email", "test@example.invalid")
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "billing.py").write_text(
        "def checkout(amount):\n    return amount\n", encoding="utf-8"
    )
    (src_dir / "refunds.py").write_text(
        "from billing import checkout\n\ndef refund(amount):\n    return checkout(amount)\n",
        encoding="utf-8",
    )
    _git(repo_dir, "add", ".")
    _git(repo_dir, "commit", "-m", "initial commit")


def _make_manager(tmp_path: Path, *, trusted: bool = False) -> WorkspaceManager:
    repos_root = tmp_path / "repos"
    ws_root = tmp_path / "workspaces"
    config = OrchestratorConfig(
        database_path=tmp_path / "db.sqlite3",
        workspace_root=ws_root,
        allowed_repository_roots=[repos_root],
        trusted_execution_roots=[repos_root] if trusted else [],
    )
    return WorkspaceManager(config)


def test_tracked_working_tree_diff_preserved_and_original_unchanged(tmp_path) -> None:
    repo = tmp_path / "repos" / "demo"
    _init_sample_repo(repo)
    billing = repo / "src" / "billing.py"
    billing.write_text(
        "def checkout(amount):\n    return amount * 0.9\n", encoding="utf-8"
    )
    # Place local secret & cache files that must NOT be copied into workspace
    (repo / ".env").write_text("SECRET_DEMO=placeholder\n", encoding="utf-8")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "marker.txt").write_text("venv", encoding="utf-8")
    (repo / "src" / "__pycache__").mkdir()
    (repo / "src" / "__pycache__" / "billing.pyc").write_bytes(b"pyc")

    orig_head = _git(repo, "rev-parse", "HEAD")
    orig_diff = _git(repo, "diff", "--unified=0", "HEAD", "--", "src/billing.py")

    manager = _make_manager(tmp_path, trusted=True)
    ctx = manager.prepare_workspace("run_wt_01", repo, source_path="src", base_ref="HEAD")

    ws_repo = Path(ctx.workspace_repo_path)
    assert ws_repo.is_dir()
    assert ctx.change_mode == "working_tree"
    assert ctx.resolved_base_commit == orig_head
    assert ctx.is_trusted_for_execution is True
    assert "src/billing.py" in ctx.tracked_modified_files

    ws_diff = _git(ws_repo, "diff", "--unified=0", ctx.effective_base_ref, "--", "src/billing.py")
    assert "return amount * 0.9" in ws_diff
    assert not (ws_repo / ".env").exists()
    assert not (ws_repo / ".venv").exists()
    assert not (ws_repo / "src" / "__pycache__").exists()

    # Original repository must remain completely untouched
    assert _git(repo, "rev-parse", "HEAD") == orig_head
    assert _git(repo, "diff", "--unified=0", "HEAD", "--", "src/billing.py") == orig_diff
    assert (repo / ".env").exists()

    # Cleanup removes workspace safely
    assert manager.cleanup_workspace(ws_repo) is True
    assert not ws_repo.exists()
    assert manager.cleanup_workspace(ws_repo) is False


def test_committed_diff_relative_to_base_ref_preserved(tmp_path) -> None:
    repo = tmp_path / "repos" / "committed_demo"
    _init_sample_repo(repo)
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "billing.py").write_text(
        "def checkout(amount):\n    return amount * 0.85\n", encoding="utf-8"
    )
    _git(repo, "add", "src/billing.py")
    _git(repo, "commit", "-m", "apply 15% discount")

    manager = _make_manager(tmp_path, trusted=False)
    ctx = manager.prepare_workspace("run_cr_01", repo, source_path="src", base_ref="HEAD~1")

    ws_repo = Path(ctx.workspace_repo_path)
    assert ctx.change_mode == "committed_range"
    assert ctx.resolved_base_commit == base_sha
    assert ctx.is_trusted_for_execution is False

    ws_diff = _git(ws_repo, "diff", "--name-only", ctx.effective_base_ref, "--", "*.py")
    assert ws_diff == "src/billing.py"
    manager.cleanup_workspace(ws_repo)


def test_rejects_invalid_repo_root_traversal_and_untracked_files(tmp_path) -> None:
    manager = _make_manager(tmp_path)

    # Non-existent repo
    with pytest.raises(WorkspaceError):
        manager.prepare_workspace("run_bad_1", tmp_path / "repos" / "missing")

    # Non-git directory inside allowed root
    plain_dir = tmp_path / "repos" / "not_git"
    plain_dir.mkdir(parents=True)
    with pytest.raises(WorkspaceError):
        manager.prepare_workspace("run_bad_2", plain_dir)

    # Outside allowed_repository_roots
    outside_repo = tmp_path / "outside_repo"
    _init_sample_repo(outside_repo)
    with pytest.raises(WorkspaceSecurityError):
        manager.prepare_workspace("run_bad_3", outside_repo)

    # Traversal in source_path
    valid_repo = tmp_path / "repos" / "valid_repo"
    _init_sample_repo(valid_repo)
    with pytest.raises(WorkspaceSecurityError):
        manager.prepare_workspace("run_bad_4", valid_repo, source_path="../outside_repo")

    # Absolute source_path
    with pytest.raises(WorkspaceSecurityError):
        manager.prepare_workspace("run_bad_5", valid_repo, source_path=str(valid_repo / "src"))

    # Untracked Python file inside source_path must be explicitly reported, never ignored
    (valid_repo / "src" / "untracked_module.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(UnsupportedWorkspaceStateError, match="untracked_module.py"):
        manager.prepare_workspace("run_bad_6", valid_repo, source_path="src")

    # Cleanup refuses paths outside workspace_root
    with pytest.raises(WorkspaceSecurityError):
        manager.cleanup_workspace(valid_repo)


def test_symlink_escape_rejected(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repos" / "symlink_repo"
    _init_sample_repo(repo)
    outside_secret = tmp_path / "outside_target.py"
    outside_secret.write_text("SECRET = 1\n", encoding="utf-8")

    link_path = repo / "src" / "escaped_link.py"
    manager = _make_manager(tmp_path)

    created_real_symlink = False
    try:
        link_path.symlink_to(outside_secret)
        created_real_symlink = True
    except OSError:
        created_real_symlink = False

    if created_real_symlink:
        with pytest.raises(WorkspaceSecurityError, match="Symlink"):
            manager.prepare_workspace("run_sym_real", repo, source_path="src")
        link_path.unlink()

    # Deterministic check ensuring symlink escape guard always executes on all OSes
    target_file = (repo / "src" / "billing.py").resolve()
    orig_is_symlink = Path.is_symlink
    orig_resolve = Path.resolve

    def _fake_is_symlink(self: Path) -> bool:
        if self == target_file:
            return True
        return orig_is_symlink(self)

    def _fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == target_file:
            return outside_secret.resolve()
        return orig_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "is_symlink", _fake_is_symlink)
    monkeypatch.setattr(Path, "resolve", _fake_resolve)

    with pytest.raises(WorkspaceSecurityError, match="Symlink"):
        manager.prepare_workspace("run_sym_det", repo, source_path="src")
