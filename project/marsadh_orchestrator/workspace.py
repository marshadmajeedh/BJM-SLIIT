"""Safe working-copy management for ChangeGuard AI (M02).

Supported change modes:
1. ``working_tree``: Tracked working-tree modifications (staged or unstaged)
   relative to ``base_ref`` (typically ``HEAD``).
2. ``committed_range``: Committed changes relative to a specified ``base_ref``
   (e.g., ``HEAD~1``, a commit SHA, or a branch/tag reference).

Untracked files under ``source_path`` are explicitly detected and rejected as
unsupported (``UnsupportedWorkspaceStateError``) rather than silently ignored,
because M01's ``git diff`` analyzer only inspects tracked Python files.

Security note:
A temporary Git working copy isolates files from the original repository; it is
NOT an operating-system security sandbox. Execution trust is tracked explicitly
via ``is_trusted_for_execution`` against ``config.trusted_execution_roots``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path, PurePath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import OrchestratorConfig

EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        ".changeguard",
    }
)
ALLOWED_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
SAFE_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/@^~{}][A-Za-z0-9._/@^~{}-]{0,199}$")
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class WorkspaceError(Exception):
    """Base error raised during workspace validation, preparation, or cleanup."""


class WorkspaceSecurityError(WorkspaceError):
    """Raised when path traversal, root escape, or unsafe symlink escape is detected."""


class UnsupportedWorkspaceStateError(WorkspaceError):
    """Raised when repository state is unsupported (e.g. untracked files under source_path)."""


class WorkspaceContext(BaseModel):
    """Immutable metadata describing a prepared run-specific working copy."""

    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    run_id: str
    workspace_role: Literal["modified", "baseline"] = "modified"
    original_repository_path: str
    workspace_root: str
    workspace_repo_path: str
    source_path: str
    base_ref: str
    effective_base_ref: str
    resolved_base_commit: str
    head_commit: str
    change_mode: Literal["working_tree", "committed_range"]
    tracked_modified_files: list[str] = Field(default_factory=list)
    is_trusted_for_execution: bool = False
    warnings: list[str] = Field(default_factory=list)


def _is_excluded_relative_path(rel_posix: str) -> bool:
    parts = PurePath(rel_posix).parts
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    name = parts[-1] if parts else ""
    return bool(
        name == ".env"
        or (name.startswith(".env.") and name not in ALLOWED_ENV_TEMPLATES)
    )


def _validate_relative_subpath(raw_path: str, *, field_name: str = "source_path") -> str:
    cleaned = raw_path.strip()
    if not cleaned:
        raise WorkspaceError(f"{field_name} must not be blank")
    if "\x00" in cleaned:
        raise WorkspaceSecurityError(f"{field_name} contains null byte")
    if cleaned.startswith(("/", "\\")) or WINDOWS_DRIVE_PATTERN.match(cleaned):
        raise WorkspaceSecurityError(
            f"{field_name} must be relative to the repository root: '{raw_path}'"
        )
    normalized = cleaned.replace("\\", "/")
    parts = PurePath(normalized).parts
    if ".." in parts:
        raise WorkspaceSecurityError(
            f"{field_name} must not contain parent traversal ('..'): '{raw_path}'"
        )
    if normalized in {".", "./"}:
        return "."
    return PurePath(normalized).as_posix()


def _validate_git_ref(base_ref: str) -> str:
    cleaned = base_ref.strip()
    if not cleaned or cleaned.startswith("-"):
        raise WorkspaceError(f"Invalid Git base reference: '{base_ref}'")
    if not SAFE_GIT_REF_PATTERN.match(cleaned) or ".." in cleaned:
        raise WorkspaceError(f"Unsupported Git base reference syntax: '{base_ref}'")
    return cleaned


def _handle_remove_readonly(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _safe_rmtree(path: Path) -> None:
    shutil.rmtree(path, onerror=_handle_remove_readonly)


def _parse_nul_name_status(raw_output: bytes) -> list[tuple[str, str, str | None]]:
    """Parse ``git diff --name-status -z`` output into (status, path1, path2)."""
    if not raw_output:
        return []
    tokens = [tok.decode("utf-8", errors="surrogateescape") for tok in raw_output.split(b"\x00")]
    if tokens and tokens[-1] == "":
        tokens.pop()
    entries: list[tuple[str, str, str | None]] = []
    idx = 0
    while idx < len(tokens):
        status = tokens[idx]
        idx += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"}:
            if idx + 1 >= len(tokens):
                break
            old_path = tokens[idx]
            new_path = tokens[idx + 1]
            idx += 2
            entries.append((status, old_path, new_path))
        else:
            if idx >= len(tokens):
                break
            path1 = tokens[idx]
            idx += 1
            entries.append((status, path1, None))
    return entries


class WorkspaceManager:
    """Prepares and cleans up isolated working copies without altering source repos."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def _run_git(
        self,
        repo: Path,
        *args: str,
        check: bool = True,
        binary: bool = False,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=not binary,
                timeout=self.config.git_timeout_seconds,
                check=check,
            )
        except subprocess.TimeoutExpired as exc:
            raise WorkspaceError(
                f"Git command timed out after {self.config.git_timeout_seconds}s: "
                f"git {' '.join(args[:3])}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            ).strip()
            raise WorkspaceError(
                f"Git command failed ({' '.join(args[:3])}): {stderr or exc}"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(f"Failed to execute git: {exc}") from exc

    def _check_symlink_containment(self, root: Path, target_dir: Path) -> None:
        """Reject symlinks along or inside target_dir that resolve outside root."""
        current = root
        rel_parts = (
            ()
            if target_dir == root
            else target_dir.relative_to(root).parts
        )
        for part in rel_parts:
            current = current / part
            if current.is_symlink():
                resolved = current.resolve()
                if not resolved.is_relative_to(root):
                    raise WorkspaceSecurityError(
                        f"Symlink escape detected in source_path: '{current}'"
                    )

        for dirpath, dirnames, filenames in os.walk(target_dir, followlinks=False):
            dirpath_obj = Path(dirpath)
            # Prune excluded directories in-place before descending
            kept_dirs: list[str] = []
            for dname in dirnames:
                if dname in EXCLUDED_DIR_NAMES:
                    continue
                dpath = dirpath_obj / dname
                if dpath.is_symlink():
                    resolved_dir = dpath.resolve()
                    if not resolved_dir.is_relative_to(root):
                        raise WorkspaceSecurityError(
                            f"Symlink directory escapes repository root: '{dpath}'"
                        )
                    continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in filenames:
                fpath = dirpath_obj / fname
                if fpath.is_symlink():
                    resolved_file = fpath.resolve()
                    if not resolved_file.is_relative_to(root):
                        raise WorkspaceSecurityError(
                            f"Symlink file escapes repository root: '{fpath}'"
                        )

    def _validate_source_repository(
        self,
        repository_path: str | Path,
        source_path: str,
        base_ref: str,
    ) -> tuple[Path, str, str, str, str]:
        raw_repo = Path(repository_path)
        if not raw_repo.exists() or not raw_repo.is_dir():
            raise WorkspaceError(f"Repository directory does not exist: '{repository_path}'")

        repo_resolved = raw_repo.resolve(strict=True)
        if not self.config.is_repository_allowed(repo_resolved):
            raise WorkspaceSecurityError(
                f"Repository '{repo_resolved}' is outside allowed_repository_roots"
            )

        # Verify Git repository root matches repo_resolved
        toplevel_proc = self._run_git(
            repo_resolved, "rev-parse", "--show-toplevel", check=False
        )
        if toplevel_proc.returncode != 0:
            raise WorkspaceError(
                f"Path is not a valid Git repository: '{repo_resolved}'"
            )
        git_toplevel = Path(toplevel_proc.stdout.strip()).resolve()
        if git_toplevel != repo_resolved:
            raise WorkspaceError(
                f"Repository path '{repo_resolved}' must be the Git repository root "
                f"(detected root: '{git_toplevel}')"
            )

        norm_source = _validate_relative_subpath(source_path, field_name="source_path")
        unnormalized_source = repo_resolved if norm_source == "." else (repo_resolved / norm_source)
        if (
            unnormalized_source.is_symlink()
            and not unnormalized_source.resolve().is_relative_to(repo_resolved)
        ):
            raise WorkspaceSecurityError(
                f"source_path symlink escapes repository root: '{source_path}'"
            )
        if not unnormalized_source.exists() or not unnormalized_source.is_dir():
            raise WorkspaceError(
                f"Source directory '{norm_source}' not found inside repository '{repo_resolved}'"
            )
        source_resolved = unnormalized_source.resolve(strict=True)
        if not source_resolved.is_relative_to(repo_resolved):
            raise WorkspaceSecurityError(
                f"Source directory '{source_path}' escapes repository root"
            )

        self._check_symlink_containment(repo_resolved, unnormalized_source)

        clean_base_ref = _validate_git_ref(base_ref)
        head_commit = self._run_git(
            repo_resolved, "rev-parse", "--verify", "HEAD^{commit}"
        ).stdout.strip()
        resolved_base_commit = self._run_git(
            repo_resolved,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{clean_base_ref}^{{commit}}",
        ).stdout.strip()

        return repo_resolved, norm_source, clean_base_ref, resolved_base_commit, head_commit

    def _detect_untracked_files(self, repo_resolved: Path, norm_source: str) -> list[str]:
        pathspec = "." if norm_source == "." else norm_source
        proc = self._run_git(
            repo_resolved,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            pathspec,
            binary=True,
        )
        if not proc.stdout:
            return []
        untracked = [
            item.decode("utf-8", errors="surrogateescape")
            for item in proc.stdout.split(b"\x00")
            if item
        ]
        return [p for p in untracked if not _is_excluded_relative_path(p)]

    def prepare_workspace(
        self,
        run_id: str,
        repository_path: str | Path,
        source_path: str = ".",
        base_ref: str = "HEAD",
    ) -> WorkspaceContext:
        """Create an isolated run-specific working copy preserving the intended Git diff."""
        if not SAFE_ID_PATTERN.match(run_id):
            raise WorkspaceSecurityError(f"Invalid run_id for workspace naming: '{run_id}'")

        (
            repo_resolved,
            norm_source,
            clean_base_ref,
            resolved_base_commit,
            head_commit,
        ) = self._validate_source_repository(repository_path, source_path, base_ref)

        untracked_files = self._detect_untracked_files(repo_resolved, norm_source)
        if untracked_files:
            raise UnsupportedWorkspaceStateError(
                "Untracked files under source_path are not supported because M01 "
                "analyzes tracked Git diffs only: "
                + ", ".join(sorted(untracked_files))
            )

        # Inspect tracked working-tree modifications relative to HEAD (read-only)
        diff_entries = _parse_nul_name_status(
            self._run_git(
                repo_resolved, "diff", "--name-status", "-z", "HEAD", binary=True
            ).stdout
        )
        staged_added_entries = _parse_nul_name_status(
            self._run_git(
                repo_resolved,
                "diff",
                "--cached",
                "--name-status",
                "--diff-filter=A",
                "-z",
                "HEAD",
                binary=True,
            ).stdout
        )
        staged_added_paths = {
            path1 for _status, path1, _path2 in staged_added_entries
        }

        tracked_modified_files: list[str] = []
        for status, path1, path2 in diff_entries:
            active_path = path2 if path2 is not None else path1
            if _is_excluded_relative_path(active_path):
                continue
            tracked_modified_files.append(active_path)
            candidate_src = repo_resolved / active_path
            if (
                candidate_src.is_symlink()
                and not candidate_src.resolve().is_relative_to(repo_resolved)
            ):
                raise WorkspaceSecurityError(
                    f"Modified file symlink escapes repository root: '{active_path}'"
                )

        change_mode: Literal["working_tree", "committed_range"] = (
            "working_tree"
            if tracked_modified_files or resolved_base_commit == head_commit
            else "committed_range"
        )

        ws_root = self.config.workspace_root.resolve()
        ws_root.mkdir(parents=True, exist_ok=True)
        workspace_id = f"{run_id}_{uuid.uuid4().hex[:8]}"
        workspace_dir = (ws_root / workspace_id).resolve()
        if not workspace_dir.is_relative_to(ws_root) or workspace_dir == ws_root:
            raise WorkspaceSecurityError("Computed workspace directory escapes workspace_root")

        workspace_dir.mkdir(parents=True, exist_ok=False)
        dest_repo = workspace_dir / "repo"
        no_hooks_dir = workspace_dir / "_empty_hooks"
        no_hooks_dir.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        try:
            # Clone without hardlinks and without running hooks; original repo is untouched.
            subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={no_hooks_dir}",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--quiet",
                    str(repo_resolved),
                    str(dest_repo),
                ],
                capture_output=True,
                text=True,
                timeout=self.config.git_timeout_seconds,
                check=True,
            )

            # Ensure head_commit and resolved_base_commit exist in the clone
            for commit_sha in {head_commit, resolved_base_commit}:
                verify = self._run_git(
                    dest_repo,
                    "cat-file",
                    "-e",
                    f"{commit_sha}^{{commit}}",
                    check=False,
                )
                if verify.returncode != 0:
                    self._run_git(
                        dest_repo,
                        "fetch",
                        "--no-tags",
                        "--quiet",
                        str(repo_resolved),
                        commit_sha,
                    )

            # Checkout exact HEAD commit (detached) into the isolated copy
            self._run_git(
                dest_repo, "checkout", "--quiet", "--detach", head_commit
            )

            # Ensure base_ref resolves identically inside the isolated workspace
            effective_base_ref = clean_base_ref
            check_ref = self._run_git(
                dest_repo,
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{clean_base_ref}^{{commit}}",
                check=False,
            )
            if (
                check_ref.returncode != 0
                or check_ref.stdout.strip() != resolved_base_commit
            ):
                if clean_base_ref != "HEAD":
                    branch_ok = self._run_git(
                        dest_repo,
                        "check-ref-format",
                        "--branch",
                        clean_base_ref,
                        check=False,
                    )
                    if branch_ok.returncode == 0:
                        self._run_git(
                            dest_repo,
                            "branch",
                            "-f",
                            clean_base_ref,
                            resolved_base_commit,
                        )
                    else:
                        effective_base_ref = resolved_base_commit
                else:
                    effective_base_ref = resolved_base_commit

            # Apply tracked working-tree modifications into isolated workspace only
            for status, path1, path2 in diff_entries:
                code = status[0]
                if code == "D":
                    if _is_excluded_relative_path(path1):
                        continue
                    dest_target = (dest_repo / path1).resolve()
                    if not dest_target.is_relative_to(dest_repo.resolve()):
                        raise WorkspaceSecurityError(f"Path escapes workspace: '{path1}'")
                    if dest_target.is_file() or dest_target.is_symlink():
                        dest_target.unlink()
                    continue

                if code == "R" and path2 is not None:
                    if not _is_excluded_relative_path(path1):
                        old_target = (dest_repo / path1).resolve()
                        if old_target.is_relative_to(dest_repo.resolve()) and old_target.is_file():
                            old_target.unlink()
                    rel_active = path2
                else:
                    rel_active = path2 if path2 is not None else path1

                if _is_excluded_relative_path(rel_active):
                    warnings.append(
                        f"Excluded sensitive or cache path from workspace overlay: {rel_active}"
                    )
                    continue

                src_file = repo_resolved / rel_active
                dest_file = (dest_repo / rel_active).resolve()
                if not dest_file.is_relative_to(dest_repo.resolve()):
                    raise WorkspaceSecurityError(
                        f"Modified path escapes workspace: '{rel_active}'"
                    )
                if src_file.is_file():
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src_file, dest_file)
                    if rel_active in staged_added_paths:
                        self._run_git(dest_repo, "add", "--", rel_active)

            # Remove any secret .env files from workspace working tree if tracked
            for env_candidate in dest_repo.rglob(".env*"):
                if ".git" in env_candidate.parts:
                    continue
                if (
                    env_candidate.is_file()
                    and env_candidate.name not in ALLOWED_ENV_TEMPLATES
                ):
                    env_candidate.unlink()
                    warnings.append(
                        f"Removed secret environment file from isolated workspace: "
                        f"{env_candidate.relative_to(dest_repo).as_posix()}"
                    )

        except Exception:
            if workspace_dir.exists():
                _safe_rmtree(workspace_dir)
            raise

        is_trusted = self.config.is_trusted_for_execution(repo_resolved)
        return WorkspaceContext(
            workspace_id=workspace_id,
            run_id=run_id,
            workspace_role="modified",
            original_repository_path=str(repo_resolved),
            workspace_root=str(ws_root),
            workspace_repo_path=str(dest_repo.resolve()),
            source_path=norm_source,
            base_ref=clean_base_ref,
            effective_base_ref=effective_base_ref,
            resolved_base_commit=resolved_base_commit,
            head_commit=head_commit,
            change_mode=change_mode,
            tracked_modified_files=sorted(set(tracked_modified_files)),
            is_trusted_for_execution=is_trusted,
            warnings=warnings,
        )

    def prepare_baseline_workspace(
        self,
        run_id: str,
        repository_path: str | Path,
        resolved_base_commit: str,
        *,
        source_path: str = ".",
        base_ref: str = "HEAD",
    ) -> WorkspaceContext:
        """Create a separate isolated working copy checked out to the exact resolved base commit.

        Guarantees:
        - Reads strictly from the original Git repository without mutating it.
        - Never touches or copies from the modified workspace.
        - Never transfers untracked files, working-tree modifications, or secret ``.env*`` files.
        - Note: Git working-copy isolation is NOT an operating-system security sandbox.
        """
        if not SAFE_ID_PATTERN.match(run_id):
            raise WorkspaceSecurityError(f"Invalid run_id for workspace naming: '{run_id}'")

        raw_repo = Path(repository_path)
        if not raw_repo.exists() or not raw_repo.is_dir():
            raise WorkspaceError(f"Repository directory does not exist: '{repository_path}'")

        repo_resolved = raw_repo.resolve(strict=True)
        if not self.config.is_repository_allowed(repo_resolved):
            raise WorkspaceSecurityError(
                f"Repository '{repo_resolved}' is outside allowed_repository_roots"
            )

        toplevel_proc = self._run_git(
            repo_resolved, "rev-parse", "--show-toplevel", check=False
        )
        if toplevel_proc.returncode != 0:
            raise WorkspaceError(f"Path is not a valid Git repository: '{repo_resolved}'")
        git_toplevel = Path(toplevel_proc.stdout.strip()).resolve()
        if git_toplevel != repo_resolved:
            raise WorkspaceError(
                f"Repository path '{repo_resolved}' must be the Git repository root "
                f"(detected root: '{git_toplevel}')"
            )

        norm_source = _validate_relative_subpath(source_path, field_name="source_path")
        clean_base_ref = _validate_git_ref(base_ref)
        clean_commit = _validate_git_ref(resolved_base_commit)

        # Confirm the exact requested baseline commit exists in the original repository
        verified_commit = self._run_git(
            repo_resolved,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{clean_commit}^{{commit}}",
        ).stdout.strip()

        ws_root = self.config.workspace_root.resolve()
        ws_root.mkdir(parents=True, exist_ok=True)
        workspace_id = f"{run_id}_baseline_{uuid.uuid4().hex[:8]}"
        workspace_dir = (ws_root / workspace_id).resolve()
        if not workspace_dir.is_relative_to(ws_root) or workspace_dir == ws_root:
            raise WorkspaceSecurityError(
                "Computed baseline workspace directory escapes workspace_root"
            )

        workspace_dir.mkdir(parents=True, exist_ok=False)
        dest_repo = workspace_dir / "repo"
        no_hooks_dir = workspace_dir / "_empty_hooks"
        no_hooks_dir.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        try:
            subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={no_hooks_dir}",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--quiet",
                    str(repo_resolved),
                    str(dest_repo),
                ],
                capture_output=True,
                text=True,
                timeout=self.config.git_timeout_seconds,
                check=True,
            )

            verify_obj = self._run_git(
                dest_repo,
                "cat-file",
                "-e",
                f"{verified_commit}^{{commit}}",
                check=False,
            )
            if verify_obj.returncode != 0:
                self._run_git(
                    dest_repo,
                    "fetch",
                    "--no-tags",
                    "--quiet",
                    str(repo_resolved),
                    verified_commit,
                )

            # Checkout the exact resolved baseline commit (detached) with zero working-tree overlays
            self._run_git(
                dest_repo, "checkout", "--quiet", "--detach", verified_commit
            )

            # Verify source_path exists in the baseline commit and passes symlink containment
            baseline_source = (
                dest_repo if norm_source == "." else (dest_repo / norm_source)
            )
            if (
                baseline_source.is_symlink()
                and not baseline_source.resolve().is_relative_to(dest_repo.resolve())
            ):
                raise WorkspaceSecurityError(
                    f"Baseline source_path symlink escapes workspace root: '{norm_source}'"
                )
            if not baseline_source.exists() or not baseline_source.is_dir():
                raise WorkspaceError(
                    f"Source directory '{norm_source}' does not exist at baseline commit "
                    f"'{verified_commit[:12]}'"
                )
            self._check_symlink_containment(dest_repo.resolve(), baseline_source)

            # Remove any tracked secret .env files from baseline workspace working tree
            for env_candidate in dest_repo.rglob(".env*"):
                if ".git" in env_candidate.parts:
                    continue
                if (
                    env_candidate.is_file()
                    and env_candidate.name not in ALLOWED_ENV_TEMPLATES
                ):
                    env_candidate.unlink()
                    warnings.append(
                        f"Removed secret environment file from baseline workspace: "
                        f"{env_candidate.relative_to(dest_repo).as_posix()}"
                    )
        except Exception:
            if workspace_dir.exists():
                _safe_rmtree(workspace_dir)
            raise

        is_trusted = self.config.is_trusted_for_execution(repo_resolved)
        return WorkspaceContext(
            workspace_id=workspace_id,
            run_id=run_id,
            workspace_role="baseline",
            original_repository_path=str(repo_resolved),
            workspace_root=str(ws_root),
            workspace_repo_path=str(dest_repo.resolve()),
            source_path=norm_source,
            base_ref=clean_base_ref,
            effective_base_ref=verified_commit,
            resolved_base_commit=verified_commit,
            head_commit=verified_commit,
            change_mode="committed_range",
            tracked_modified_files=[],
            is_trusted_for_execution=is_trusted,
            warnings=warnings,
        )

    def prepare_clean_repair_workspace(
        self,
        run_id: str,
        repository_path: str | Path,
        resolved_base_commit: str,
        attempt_number: int,
        *,
        source_path: str = ".",
        base_ref: str = "HEAD",
    ) -> WorkspaceContext:
        """Create a clean isolated working copy from the immutable original commit for repair.

        Implements the immutable-original-snapshot repair strategy:
        - Reads strictly from the original Git repository at ``resolved_base_commit``
          without mutating the original repository or reusing a dirty prior attempt workspace.
        - Disables Git hooks, removes tracked secret ``.env*`` files, and enforces symlink
          containment.
        - Note: Git working-copy isolation is NOT an operating-system security sandbox.
        """
        if attempt_number < 2 or attempt_number > 3:
            raise WorkspaceError(
                f"Repair attempt number must be 2 or 3; got {attempt_number}"
            )
        base_ctx = self.prepare_baseline_workspace(
            f"{run_id}_repair{attempt_number}",
            repository_path,
            resolved_base_commit,
            source_path=source_path,
            base_ref=base_ref,
        )
        return base_ctx.model_copy(
            update={
                "run_id": run_id,
                "workspace_role": "modified",
                "change_mode": "working_tree",
            }
        )

    def cleanup_workspace(self, workspace_path: str | Path) -> bool:
        """Safely delete a run-specific workspace strictly inside workspace_root."""
        ws_root = self.config.workspace_root.resolve()
        target = Path(workspace_path).resolve()

        if not target.is_relative_to(ws_root) or target == ws_root:
            raise WorkspaceSecurityError(
                f"Refusing to delete path outside workspace_root: '{workspace_path}'"
            )

        # If caller passed <workspace_root>/<workspace_id>/repo, remove the parent run folder
        rel_parts = target.relative_to(ws_root).parts
        delete_root = ws_root / rel_parts[0]
        if not delete_root.exists():
            return False

        _safe_rmtree(delete_root)
        return True
