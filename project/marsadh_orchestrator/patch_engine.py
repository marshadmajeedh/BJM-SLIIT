"""Controlled patch validation and isolated-workspace application for ChangeGuard AI (M02).

Security & safety guarantees:
- Operates EXCLUSIVELY inside an isolated workspace under ``config.workspace_root``;
  refuses to touch any path outside ``workspace_root`` (original repositories
  can never be modified).
- Validates unified Git diff syntax, binary restrictions, symlink/submodule
  modes, path traversal, absolute paths, symlink escape, protected paths
  (credentials, Git internals, orchestrator/CI infrastructure), allowed source
  scope, size and changed-file limits, declared ``affected_files`` parity,
  acceptance criterion references, workspace snapshot integrity, and patch
  applicability via ``git apply --check``.
- Uses safe subprocess argument lists (never ``shell=True``), explicit working
  directories, and bounded timeouts. Never executes commands embedded in
  generated output.
- Revalidates workspace snapshots, symlink containment, and patch SHA-256 right
  before ``git apply``.
- Rolls back workspace modifications if patch application or post-apply
  verification fails so partially applied changes never reach verification.
- Explicitly notes that temporary Git working-copy isolation is NOT an
  operating-system security sandbox.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import redact_secrets
from project.marsadh_orchestrator.workspace import EXCLUDED_DIR_NAMES, WorkspaceError
from project.schemas.agents import AgentError
from project.schemas.patch import (
    PatchApplicationResult,
    PatchProposal,
    PatchValidationResult,
    compute_patch_sha256,
)

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_DIFF_GIT_RE = re.compile(r"^diff --git\s+(\S+)\s+(\S+)\s*$")

_PROTECTED_DIR_SEGMENTS: frozenset[str] = frozenset(
    {
        ".git",
        ".github",
        ".changeguard",
        ".changeguard_generated_tests",
        ".ssh",
        ".aws",
        ".venv",
        "venv",
        "node_modules",
    }
)

_PROTECTED_REPO_PREFIXES: tuple[str, ...] = (
    "project/marsadh_orchestrator",
    "project/api",
    "project/schemas",
    "project/agents",
    "project/nafris_verification",
    "frontend",
)

_PROTECTED_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "project/main.py",
        ".gitignore",
        ".gitmodules",
        ".gitattributes",
        ".pre-commit-config.yaml",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "makefile",
        "dockerfile",
    }
)

_PROTECTED_FILENAMES: frozenset[str] = frozenset(
    {
        ".env",
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "id_ed25519.pub",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "secrets.py",
    }
)

_PROTECTED_EXTENSIONS: tuple[str, ...] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".kdbx",
)


@dataclass(frozen=True)
class ParsedPatchFile:
    """Normalized file entry parsed from a unified Git diff."""

    old_path: str | None
    new_path: str | None
    change_type: str
    hunk_count: int


def _compute_snapshot_sha256(workspace_repo_path: Path, source_path: str) -> str:
    """Compute deterministic SHA-256 fingerprint of source files under source_path."""
    repo_root = workspace_repo_path.resolve(strict=True)
    target_dir = (
        repo_root
        if source_path == "."
        else (repo_root / source_path).resolve(strict=True)
    )
    if not target_dir.is_relative_to(repo_root) or not target_dir.is_dir():
        raise WorkspaceError(
            f"Source snapshot target escapes or is missing inside workspace: '{source_path}'"
        )

    excluded = set(EXCLUDED_DIR_NAMES) | {".changeguard_generated_tests"}
    entries: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(target_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in excluded)
        base = Path(dirpath)
        for fname in sorted(filenames):
            fpath = base / fname
            if fpath.is_symlink():
                continue
            rel = fpath.relative_to(repo_root).as_posix()
            entries.append((rel, fpath))

    digest = hashlib.sha256()
    for rel, fpath in sorted(entries, key=lambda item: item[0]):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(fpath.read_bytes())
        digest.update(b"\x00")
    return digest.hexdigest()


def _compute_git_diff_sha256(
    workspace_repo_path: Path,
    base_ref: str,
    source_path: str,
    *,
    timeout_seconds: int,
) -> str:
    """Compute deterministic SHA-256 digest of workspace Git diff under source_path."""
    repo_root = workspace_repo_path.resolve(strict=True)
    pathspec = "." if source_path == "." else source_path
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "diff",
                "--no-ext-diff",
                base_ref,
                "--",
                pathspec,
            ],
            cwd=str(repo_root),
            capture_output=True,
            timeout=timeout_seconds,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceError(
            f"Git diff snapshot timed out after {timeout_seconds}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (
            exc.stderr.decode("utf-8", errors="replace")
            if isinstance(exc.stderr, bytes)
            else str(exc.stderr or exc)
        ).strip()
        raise WorkspaceError(f"Failed to hash workspace Git diff: {stderr}") from exc
    except OSError as exc:
        raise WorkspaceError(f"Failed to execute git for diff snapshot: {exc}") from exc

    return hashlib.sha256(proc.stdout).hexdigest()


def _strip_diff_prefix(raw_token: str, expected_prefix: str) -> str:
    """Strip standard Git 'a/' or 'b/' header prefix without stripping absolute paths."""
    cleaned = raw_token.strip()
    if "\t" in cleaned:
        cleaned = cleaned.split("\t", 1)[0].strip()
    if cleaned.startswith('"') and cleaned.endswith('"') and len(cleaned) >= 2:
        cleaned = cleaned[1:-1]
    if cleaned == "/dev/null":
        return "/dev/null"
    if cleaned.startswith(expected_prefix):
        return cleaned[len(expected_prefix) :]
    return cleaned


def _validate_patch_path(raw_path: str) -> tuple[str | None, AgentError | None]:
    """Validate a single patch path against null bytes, absolute paths, and traversal."""
    if not raw_path or not raw_path.strip():
        return None, AgentError(
            code="PATCH_INVALID_SYNTAX",
            message="Patch header contains an empty file path.",
        )
    if "\x00" in raw_path:
        return None, AgentError(
            code="PATCH_PATH_TRAVERSAL",
            message="Patch path contains null byte.",
        )
    if raw_path.startswith(("/", "\\")) or _WINDOWS_DRIVE_RE.match(raw_path):
        return None, AgentError(
            code="PATCH_ABSOLUTE_PATH",
            message=f"Patch targets an absolute path, which is forbidden: '{raw_path}'",
            details={"path": raw_path},
        )
    normalized = raw_path.replace("\\", "/")
    parts = PurePath(normalized).parts
    if not parts or normalized in {".", "./"} or ".." in parts:
        return None, AgentError(
            code="PATCH_PATH_TRAVERSAL",
            message=f"Patch targets a path traversal or invalid relative path: '{raw_path}'",
            details={"path": raw_path},
        )
    return PurePath(normalized).as_posix(), None


def _is_protected_path(rel_posix: str) -> str | None:
    """Return a protection reason if ``rel_posix`` targets a protected file/dir."""
    lowered = rel_posix.lower()
    parts = [part.lower() for part in PurePath(rel_posix).parts]
    if any(part in _PROTECTED_DIR_SEGMENTS for part in parts):
        return f"targets protected directory ({rel_posix})"
    if lowered in _PROTECTED_EXACT_PATHS:
        return f"targets protected infrastructure file ({rel_posix})"
    for prefix in _PROTECTED_REPO_PREFIXES:
        if lowered == prefix or lowered.startswith(f"{prefix}/"):
            return f"targets protected orchestrator or teammate scope ({prefix})"
    fname = parts[-1] if parts else ""
    if (
        fname in _PROTECTED_FILENAMES
        or fname.startswith(".env")
        or fname.endswith(_PROTECTED_EXTENSIONS)
        or fname.startswith("docker-compose")
        or "secret" in fname
        or "credential" in fname
    ):
        return f"targets protected credential or sensitive file ({rel_posix})"
    return None


def _is_within_allowed_paths(
    rel_posix: str,
    source_path: str,
    allowed_modification_paths: list[str] | None,
) -> bool:
    """Verify ``rel_posix`` sits inside ``source_path`` and ``allowed_modification_paths``."""
    norm_source = source_path.strip().replace("\\", "/").rstrip("/")
    if norm_source not in {"", "."}:
        if rel_posix != norm_source and not rel_posix.startswith(f"{norm_source}/"):
            return False

    if allowed_modification_paths:
        matched = False
        for raw_allowed in allowed_modification_paths:
            allowed = raw_allowed.strip().replace("\\", "/").rstrip("/")
            if allowed in {"", "."}:
                matched = True
                break
            if rel_posix == allowed or rel_posix.startswith(f"{allowed}/"):
                matched = True
                break
        if not matched:
            return False
    return True


def _check_path_symlink_safety(
    workspace_repo: Path,
    source_root: Path,
    rel_posix: str,
) -> AgentError | None:
    """Reject any symlink component along or at ``rel_posix`` inside the workspace."""
    current = workspace_repo
    for part in PurePath(rel_posix).parts:
        current = current / part
        if current.is_symlink():
            return AgentError(
                code="PATCH_SYMLINK_ESCAPE",
                message=(
                    f"Patch targets or traverses a symbolic link inside workspace: '{rel_posix}'"
                ),
                details={"path": rel_posix},
            )
    resolved = (workspace_repo / rel_posix).resolve()
    if not resolved.is_relative_to(workspace_repo) or not resolved.is_relative_to(source_root):
        return AgentError(
            code="PATCH_SYMLINK_ESCAPE",
            message=f"Resolved patch target escapes permitted workspace source root: '{rel_posix}'",
            details={"path": rel_posix},
        )
    return None


def parse_and_check_unified_diff(
    unified_diff: str,
) -> tuple[list[ParsedPatchFile], list[AgentError]]:
    """Parse a unified Git diff strictly, rejecting binary, symlink-mode, and malformed patches."""
    violations: list[AgentError] = []
    if not unified_diff or not unified_diff.strip():
        violations.append(
            AgentError(
                code="PATCH_INVALID_SYNTAX",
                message="Unified diff is empty.",
            )
        )
        return [], violations

    if "\x00" in unified_diff:
        violations.append(
            AgentError(
                code="PATCH_BINARY_REJECTED",
                message="Patch contains NUL bytes (binary content is forbidden).",
            )
        )
        return [], violations

    lines = unified_diff.splitlines()
    parsed_files: list[ParsedPatchFile] = []

    current_old: str | None = None
    current_new: str | None = None
    current_rename_from: str | None = None
    current_rename_to: str | None = None
    current_hunks = 0
    in_hunk = False
    remaining_old_lines = 0
    remaining_new_lines = 0
    seen_any_header = False

    def _finalize_current() -> None:
        nonlocal current_old, current_new, current_rename_from, current_rename_to
        nonlocal current_hunks, in_hunk
        if current_old is None and current_new is None:
            return
        old_p = current_rename_from or (
            None if current_old == "/dev/null" else current_old
        )
        new_p = current_rename_to or (
            None if current_new == "/dev/null" else current_new
        )
        if old_p is None and new_p is not None:
            ctype = "add"
        elif new_p is None and old_p is not None:
            ctype = "delete"
        elif old_p is not None and new_p is not None and old_p != new_p:
            ctype = "rename"
        else:
            ctype = "modify"
        if current_hunks == 0 and ctype != "rename":
            violations.append(
                AgentError(
                    code="PATCH_INVALID_SYNTAX",
                    message=(
                        f"Patch file section for '{new_p or old_p}' contains no valid diff hunks."
                    ),
                )
            )
        parsed_files.append(
            ParsedPatchFile(
                old_path=old_p,
                new_path=new_p,
                change_type=ctype,
                hunk_count=current_hunks,
            )
        )
        current_old = None
        current_new = None
        current_rename_from = None
        current_rename_to = None
        current_hunks = 0
        in_hunk = False

    idx = 0
    while idx < len(lines):
        line = lines[idx]

        # Binary patch markers anywhere outside hunk body
        if not in_hunk and (
            line == "GIT binary patch"
            or line.startswith("Binary files ")
            and line.endswith(" differ")
        ):
            violations.append(
                AgentError(
                    code="PATCH_BINARY_REJECTED",
                    message="Binary patches are forbidden.",
                )
            )
            return parsed_files, violations

        # Forbidden special file modes (symlinks 120000, gitlinks/submodules 160000)
        if not in_hunk and any(
            line.startswith(prefix)
            for prefix in ("new file mode ", "old mode ", "new mode ", "deleted file mode ")
        ):
            mode_val = line.rsplit(" ", 1)[-1].strip()
            if mode_val.startswith("120") or mode_val.startswith("160"):
                violations.append(
                    AgentError(
                        code="PATCH_UNSAFE_FILE_MODE",
                        message=(
                            f"Patch declares forbidden special Git file mode '{mode_val}' "
                            "(symlinks and submodules are prohibited)."
                        ),
                    )
                )
                return parsed_files, violations

        if line.startswith("diff --git "):
            if in_hunk and (remaining_old_lines > 0 or remaining_new_lines > 0):
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message="Truncated hunk encountered before next 'diff --git' header.",
                    )
                )
            _finalize_current()
            seen_any_header = True
            match = _DIFF_GIT_RE.match(line)
            if not match:
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message=f"Malformed 'diff --git' header: '{line[:120]}'",
                    )
                )
                return parsed_files, violations
            current_old = _strip_diff_prefix(match.group(1), "a/")
            current_new = _strip_diff_prefix(match.group(2), "b/")
            idx += 1
            continue

        if not in_hunk and line.startswith("rename from "):
            current_rename_from = line[len("rename from ") :].strip()
            idx += 1
            continue
        if not in_hunk and line.startswith("rename to "):
            current_rename_to = line[len("rename to ") :].strip()
            idx += 1
            continue
        if not in_hunk and line.startswith("copy from "):
            current_rename_from = line[len("copy from ") :].strip()
            idx += 1
            continue
        if not in_hunk and line.startswith("copy to "):
            current_rename_to = line[len("copy to ") :].strip()
            idx += 1
            continue

        if not in_hunk and line.startswith("--- "):
            # Support unified diffs without a leading 'diff --git' line as well
            if current_old is not None and current_hunks > 0:
                _finalize_current()
            seen_any_header = True
            header_old = _strip_diff_prefix(line[4:], "a/")
            idx += 1
            if idx >= len(lines) or not lines[idx].startswith("+++ "):
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message="Expected '+++ ' header immediately after '--- ' header.",
                    )
                )
                return parsed_files, violations
            header_new = _strip_diff_prefix(lines[idx][4:], "b/")
            # Prevent diff --git vs ---/+++ header path smuggling
            if (
                current_old is not None
                and header_old != "/dev/null"
                and header_old != current_old
            ):
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message=(
                            f"Mismatched diff headers: 'diff --git' old path '{current_old}' "
                            f"conflicts with '---' path '{header_old}'."
                        ),
                    )
                )
                return parsed_files, violations
            if (
                current_new is not None
                and header_new != "/dev/null"
                and header_new != current_new
            ):
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message=(
                            f"Mismatched diff headers: 'diff --git' new path '{current_new}' "
                            f"conflicts with '+++' path '{header_new}'."
                        ),
                    )
                )
                return parsed_files, violations
            current_old = header_old
            current_new = header_new
            idx += 1
            continue

        if line.startswith("@@ "):
            if current_old is None and current_new is None:
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message="Hunk header encountered before any file header.",
                    )
                )
                return parsed_files, violations
            hunk_match = _HUNK_HEADER_RE.match(line)
            if not hunk_match:
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message=f"Malformed unified diff hunk header: '{line[:120]}'",
                    )
                )
                return parsed_files, violations
            remaining_old_lines = (
                int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            )
            remaining_new_lines = (
                int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            )
            current_hunks += 1
            in_hunk = remaining_old_lines > 0 or remaining_new_lines > 0
            idx += 1
            continue

        if in_hunk:
            if line.startswith("\\ No newline at end of file"):
                idx += 1
                continue
            if line.startswith(" "):
                remaining_old_lines -= 1
                remaining_new_lines -= 1
            elif line.startswith("-"):
                remaining_old_lines -= 1
            elif line.startswith("+"):
                remaining_new_lines -= 1
            else:
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message=f"Unexpected line inside unified diff hunk: '{line[:80]}'",
                    )
                )
                return parsed_files, violations
            if remaining_old_lines < 0 or remaining_new_lines < 0:
                violations.append(
                    AgentError(
                        code="PATCH_INVALID_SYNTAX",
                        message="Unified diff hunk line counts do not match hunk header.",
                    )
                )
                return parsed_files, violations
            if remaining_old_lines == 0 and remaining_new_lines == 0:
                in_hunk = False
            idx += 1
            continue

        # Allowed metadata header lines outside hunks
        if (
            line.startswith("index ")
            or line.startswith("new file mode ")
            or line.startswith("deleted file mode ")
            or line.startswith("old mode ")
            or line.startswith("new mode ")
            or line.startswith("similarity index ")
            or line.startswith("dissimilarity index ")
            or line == ""
        ):
            idx += 1
            continue

        violations.append(
            AgentError(
                code="PATCH_INVALID_SYNTAX",
                message=f"Unrecognized line in unified diff: '{line[:100]}'",
            )
        )
        return parsed_files, violations

    if in_hunk and (remaining_old_lines > 0 or remaining_new_lines > 0):
        violations.append(
            AgentError(
                code="PATCH_INVALID_SYNTAX",
                message="Unified diff ended mid-hunk (truncated patch).",
            )
        )

    _finalize_current()

    if not seen_any_header or not parsed_files:
        violations.append(
            AgentError(
                code="PATCH_INVALID_SYNTAX",
                message="Unified diff does not contain any valid file change sections.",
            )
        )

    return parsed_files, violations


class PatchValidator:
    """Validates proposed unified Git diffs strictly against isolated workspaces."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def validate(
        self,
        *,
        workspace_repo_path: str | Path,
        proposal: PatchProposal,
        source_path: str = ".",
        base_ref: str = "HEAD",
        allowed_modification_paths: list[str] | None = None,
        expected_source_snapshot_sha256: str | None = None,
        expected_git_diff_sha256: str | None = None,
        valid_criterion_ids: set[str] | None = None,
        max_patch_size_bytes: int | None = None,
        max_patch_changed_files: int | None = None,
    ) -> PatchValidationResult:
        """Run all pre-application safety, scope, integrity, and applicability checks."""
        checks_passed: list[str] = []
        violations: list[AgentError] = []
        checked_files: list[str] = []

        patch_bytes = proposal.unified_diff.encode("utf-8")
        patch_size_bytes = len(patch_bytes)
        actual_patch_sha = compute_patch_sha256(proposal.unified_diff)

        # 1. Patch SHA-256 integrity
        if not hmac.compare_digest(actual_patch_sha, proposal.patch_sha256):
            violations.append(
                AgentError(
                    code="PATCH_HASH_MISMATCH",
                    message="Proposed patch SHA-256 does not match unified_diff contents.",
                    details={
                        "expected_sha256": proposal.patch_sha256,
                        "actual_sha256": actual_patch_sha,
                    },
                )
            )
        else:
            checks_passed.append("patch_hash_verified")

        # 2. Patch size limit
        size_limit = (
            max_patch_size_bytes
            if max_patch_size_bytes is not None
            else self.config.max_patch_size_bytes
        )
        if patch_size_bytes > size_limit:
            violations.append(
                AgentError(
                    code="PATCH_SIZE_LIMIT_EXCEEDED",
                    message=(
                        f"Patch size ({patch_size_bytes} bytes) exceeds configured limit "
                        f"({size_limit} bytes)."
                    ),
                    details={"patch_size_bytes": patch_size_bytes, "limit_bytes": size_limit},
                )
            )
        else:
            checks_passed.append("patch_size_limit")

        # 3. Acceptance criterion references
        if valid_criterion_ids is not None:
            unknown_cids: set[str] = set()
            for cid in proposal.related_criterion_ids:
                if cid not in valid_criterion_ids:
                    unknown_cids.add(cid)
            for fc in proposal.affected_files:
                for cid in fc.related_criterion_ids:
                    if cid not in valid_criterion_ids:
                        unknown_cids.add(cid)
            if unknown_cids:
                violations.append(
                    AgentError(
                        code="INVALID_CRITERION_REFERENCE",
                        message=(
                            "Patch proposal references unknown acceptance criterion ID(s): "
                            f"{sorted(unknown_cids)}"
                        ),
                        details={"unknown_criterion_ids": sorted(unknown_cids)},
                    )
                )
            else:
                checks_passed.append("criterion_references")

        # 4. Workspace containment (MUST be strictly inside config.workspace_root!)
        ws_root = self.config.workspace_root.resolve()
        ws_repo_raw = Path(workspace_repo_path)
        if not ws_repo_raw.exists() or not ws_repo_raw.is_dir():
            violations.append(
                AgentError(
                    code="WORKSPACE_MISSING",
                    message="Isolated workspace directory does not exist.",
                )
            )
            return PatchValidationResult(
                valid=False,
                status="failed",
                patch_sha256=actual_patch_sha,
                expected_snapshot_sha256=expected_source_snapshot_sha256,
                checked_files=checked_files,
                patch_size_bytes=patch_size_bytes,
                changed_file_count=0,
                git_apply_check_passed=False,
                checks_passed=checks_passed,
                violations=violations,
            )

        ws_repo = ws_repo_raw.resolve(strict=True)
        if not ws_repo.is_relative_to(ws_root) or ws_repo == ws_root:
            violations.append(
                AgentError(
                    code="WORKSPACE_OUTSIDE_ROOT",
                    message=(
                        "Refusing to validate or apply patch outside configured workspace_root."
                    ),
                )
            )
            return PatchValidationResult(
                valid=False,
                status="failed",
                patch_sha256=actual_patch_sha,
                expected_snapshot_sha256=expected_source_snapshot_sha256,
                checked_files=checked_files,
                patch_size_bytes=patch_size_bytes,
                changed_file_count=0,
                git_apply_check_passed=False,
                checks_passed=checks_passed,
                violations=violations,
            )
        checks_passed.append("workspace_containment")

        source_root = (
            ws_repo if source_path == "." else (ws_repo / source_path).resolve()
        )
        if not source_root.is_relative_to(ws_repo) or not source_root.is_dir():
            violations.append(
                AgentError(
                    code="WORKSPACE_SOURCE_MISSING",
                    message=f"Source directory '{source_path}' is missing inside workspace.",
                )
            )
            return PatchValidationResult(
                valid=False,
                status="failed",
                patch_sha256=actual_patch_sha,
                expected_snapshot_sha256=expected_source_snapshot_sha256,
                checked_files=checked_files,
                patch_size_bytes=patch_size_bytes,
                changed_file_count=0,
                git_apply_check_passed=False,
                checks_passed=checks_passed,
                violations=violations,
            )

        # 5. Workspace snapshot & diff integrity against expected pre-patch snapshot
        try:
            current_snapshot = _compute_snapshot_sha256(ws_repo, source_path)
            current_diff = _compute_git_diff_sha256(
                ws_repo,
                base_ref,
                source_path,
                timeout_seconds=self.config.git_timeout_seconds,
            )
        except WorkspaceError as exc:
            violations.append(
                AgentError(
                    code="WORKSPACE_INTEGRITY_ERROR",
                    message=f"Failed to verify workspace snapshot: {exc}",
                )
            )
        else:
            if expected_source_snapshot_sha256 and not hmac.compare_digest(
                current_snapshot, expected_source_snapshot_sha256
            ):
                violations.append(
                    AgentError(
                        code="WORKSPACE_SNAPSHOT_MISMATCH",
                        message=(
                            "Workspace source snapshot changed before patch validation; "
                            "refusing to validate patch against tampered workspace."
                        ),
                        details={
                            "expected_snapshot_sha256": expected_source_snapshot_sha256,
                            "actual_snapshot_sha256": current_snapshot,
                        },
                    )
                )
            elif expected_git_diff_sha256 and not hmac.compare_digest(
                current_diff, expected_git_diff_sha256
            ):
                violations.append(
                    AgentError(
                        code="WORKSPACE_DIFF_MISMATCH",
                        message=(
                            "Workspace Git diff changed before patch validation; "
                            "refusing to validate patch against tampered workspace."
                        ),
                    )
                )
            else:
                checks_passed.append("workspace_integrity")

        # 6. Parse unified diff syntax, binary restriction, special file modes
        parsed_files, parse_violations = parse_and_check_unified_diff(
            proposal.unified_diff
        )
        violations.extend(parse_violations)
        if not parse_violations:
            checks_passed.append("patch_syntax_and_binary_check")

        # 7. Changed-file limit
        file_limit = (
            max_patch_changed_files
            if max_patch_changed_files is not None
            else self.config.max_patch_changed_files
        )
        if len(parsed_files) > file_limit:
            violations.append(
                AgentError(
                    code="PATCH_FILE_LIMIT_EXCEEDED",
                    message=(
                        f"Patch touches {len(parsed_files)} file(s), exceeding configured "
                        f"limit ({file_limit})."
                    ),
                    details={"changed_file_count": len(parsed_files), "limit": file_limit},
                )
            )
        elif parsed_files:
            checks_passed.append("changed_file_limit")

        # 8. Path traversal, absolute paths, protected files, allowed paths, symlinks
        diff_target_paths: set[str] = set()
        path_check_failed = False
        for pf in parsed_files:
            raw_candidates = [p for p in (pf.old_path, pf.new_path) if p is not None]
            for raw_p in raw_candidates:
                norm_p, path_err = _validate_patch_path(raw_p)
                if path_err is not None or norm_p is None:
                    if path_err is not None:
                        violations.append(path_err)
                    path_check_failed = True
                    continue

                protected_reason = _is_protected_path(norm_p)
                if protected_reason is not None:
                    violations.append(
                        AgentError(
                            code="PATCH_PROTECTED_PATH",
                            message=f"Patch rejected: {protected_reason}.",
                            details={"path": norm_p},
                        )
                    )
                    path_check_failed = True
                    continue

                if not _is_within_allowed_paths(
                    norm_p, source_path, allowed_modification_paths
                ):
                    violations.append(
                        AgentError(
                            code="PATCH_PATH_OUTSIDE_SCOPE",
                            message=(
                                f"Patch modifies path '{norm_p}' outside permitted source "
                                f"scope ('{source_path}')."
                            ),
                            details={
                                "path": norm_p,
                                "source_path": source_path,
                                "allowed_modification_paths": allowed_modification_paths or [],
                            },
                        )
                    )
                    path_check_failed = True
                    continue

                sym_err = _check_path_symlink_safety(ws_repo, source_root, norm_p)
                if sym_err is not None:
                    violations.append(sym_err)
                    path_check_failed = True
                    continue

                if norm_p not in checked_files:
                    checked_files.append(norm_p)

            primary_target = pf.new_path or pf.old_path
            if primary_target is not None:
                norm_t, _ = _validate_patch_path(primary_target)
                if norm_t is not None:
                    diff_target_paths.add(norm_t)

        if not path_check_failed and parsed_files:
            checks_passed.extend(
                ["allowed_paths", "protected_paths", "symlink_containment"]
            )

        # 9. Cross-check declared affected_files against parsed diff targets
        declared_paths = {fc.path for fc in proposal.affected_files}
        if parsed_files and not path_check_failed and declared_paths != diff_target_paths:
            violations.append(
                AgentError(
                    code="PATCH_AFFECTED_FILES_MISMATCH",
                    message=(
                        "PatchProposal.affected_files does not match the files modified in "
                        "unified_diff."
                    ),
                    details={
                        "declared_files": sorted(declared_paths),
                        "diff_files": sorted(diff_target_paths),
                    },
                )
            )
        elif parsed_files and not path_check_failed:
            checks_passed.append("affected_files_parity")

        # 10. Patch applicability check via `git apply --check -` (only if prior safety checks passed)
        git_apply_check_passed = False
        if not violations:
            try:
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(ws_repo),
                        "apply",
                        "--check",
                        "-",
                    ],
                    input=patch_bytes,
                    cwd=str(ws_repo),
                    capture_output=True,
                    timeout=self.config.git_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                violations.append(
                    AgentError(
                        code="PATCH_APPLY_CHECK_TIMEOUT",
                        message=(
                            f"git apply --check timed out after "
                            f"{self.config.git_timeout_seconds}s."
                        ),
                    )
                )
            except OSError as exc:
                violations.append(
                    AgentError(
                        code="PATCH_APPLY_CHECK_ERROR",
                        message=f"Failed to execute git apply --check: {exc}",
                    )
                )
            else:
                if proc.returncode != 0:
                    stderr = redact_secrets(
                        proc.stderr.decode("utf-8", errors="replace").strip()
                    )
                    violations.append(
                        AgentError(
                            code="PATCH_NOT_APPLICABLE",
                            message=(
                                "Patch failed git apply --check against the expected "
                                f"workspace snapshot: {stderr or 'patch does not apply cleanly'}"
                            ),
                        )
                    )
                else:
                    git_apply_check_passed = True
                    checks_passed.append("git_apply_check")

        is_valid = len(violations) == 0 and git_apply_check_passed
        return PatchValidationResult(
            valid=is_valid,
            status="passed" if is_valid else "failed",
            patch_sha256=actual_patch_sha,
            expected_snapshot_sha256=expected_source_snapshot_sha256,
            checked_files=sorted(checked_files),
            patch_size_bytes=patch_size_bytes,
            changed_file_count=len(parsed_files),
            git_apply_check_passed=git_apply_check_passed,
            checks_passed=checks_passed,
            violations=violations,
        )


class PatchApplicator:
    """Applies a previously validated patch inside an isolated workspace with rollback safety."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        git_apply_runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    ) -> None:
        self.config = config
        self._git_apply_runner = git_apply_runner

    def _rollback_files(
        self,
        ws_repo: Path,
        backups: dict[str, bytes | None],
        added_paths: list[str],
    ) -> bool:
        """Restore every backed-up file and unstage/remove newly created files."""
        try:
            for rel_posix, original_bytes in backups.items():
                target = (ws_repo / rel_posix).resolve()
                if not target.is_relative_to(ws_repo):
                    continue
                if original_bytes is None:
                    if target.exists() or target.is_symlink():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original_bytes)
            if added_paths:
                subprocess.run(
                    ["git", "-C", str(ws_repo), "reset", "--quiet", "HEAD", "--", *added_paths],
                    cwd=str(ws_repo),
                    capture_output=True,
                    timeout=self.config.git_timeout_seconds,
                    check=False,
                )
            return True
        except Exception:  # noqa: BLE001
            return False

    def apply(
        self,
        *,
        workspace_repo_path: str | Path,
        proposal: PatchProposal,
        validation_result: PatchValidationResult,
        source_path: str = ".",
        base_ref: str = "HEAD",
        expected_source_snapshot_sha256: str,
        expected_git_diff_sha256: str,
    ) -> PatchApplicationResult:
        """Revalidate patch hash & workspace integrity, apply patch, and recompute snapshots."""
        actual_patch_sha = compute_patch_sha256(proposal.unified_diff)

        def _failed(
            code: str,
            message: str,
            *,
            details: dict | None = None,
            rollback_performed: bool = False,
            clean_after_rollback: bool | None = None,
        ) -> PatchApplicationResult:
            return PatchApplicationResult(
                applied=False,
                status="reverted" if rollback_performed else "failed",
                patch_sha256=actual_patch_sha,
                original_source_snapshot_sha256=expected_source_snapshot_sha256,
                original_git_diff_sha256=expected_git_diff_sha256,
                rollback_performed=rollback_performed,
                workspace_clean_after_rollback=clean_after_rollback,
                error=AgentError(
                    code=code,
                    message=redact_secrets(message),
                    details=details or {},
                ),
            )

        # 1. Require prior passing validation
        if not validation_result.valid:
            return _failed(
                "PATCH_NOT_VALIDATED",
                "Refusing to apply a patch that did not pass PatchValidator checks.",
            )

        # 2. Revalidate patch SHA-256 against BOTH proposal and validation_result
        if not (
            hmac.compare_digest(actual_patch_sha, proposal.patch_sha256)
            and hmac.compare_digest(actual_patch_sha, validation_result.patch_sha256)
        ):
            return _failed(
                "PATCH_HASH_MISMATCH",
                "Patch content changed after validation; refusing to apply tampered patch.",
                details={
                    "validated_sha256": validation_result.patch_sha256,
                    "actual_sha256": actual_patch_sha,
                },
            )

        # 3. Verify workspace resides strictly inside configured workspace_root
        ws_root = self.config.workspace_root.resolve()
        ws_repo_raw = Path(workspace_repo_path)
        if not ws_repo_raw.exists() or not ws_repo_raw.is_dir():
            return _failed("WORKSPACE_MISSING", "Isolated workspace directory no longer exists.")
        ws_repo = ws_repo_raw.resolve(strict=True)
        if not ws_repo.is_relative_to(ws_root) or ws_repo == ws_root:
            return _failed(
                "WORKSPACE_OUTSIDE_ROOT",
                "Refusing to apply patch outside configured workspace_root.",
            )
        source_root = (
            ws_repo if source_path == "." else (ws_repo / source_path).resolve()
        )

        # 4. Revalidate workspace snapshot & diff right before applying
        try:
            pre_snapshot = _compute_snapshot_sha256(ws_repo, source_path)
            pre_diff = _compute_git_diff_sha256(
                ws_repo,
                base_ref,
                source_path,
                timeout_seconds=self.config.git_timeout_seconds,
            )
        except WorkspaceError as exc:
            return _failed(
                "WORKSPACE_INTEGRITY_ERROR",
                f"Failed to revalidate workspace before patch application: {exc}",
            )

        if not hmac.compare_digest(pre_snapshot, expected_source_snapshot_sha256):
            return _failed(
                "WORKSPACE_TAMPERED",
                "Workspace source snapshot changed between validation and application; refusing to apply patch.",
                details={
                    "expected_snapshot_sha256": expected_source_snapshot_sha256,
                    "actual_snapshot_sha256": pre_snapshot,
                },
            )
        if not hmac.compare_digest(pre_diff, expected_git_diff_sha256):
            return _failed(
                "WORKSPACE_TAMPERED",
                "Workspace Git diff changed between validation and application; refusing to apply patch.",
                details={
                    "expected_diff_sha256": expected_git_diff_sha256,
                    "actual_diff_sha256": pre_diff,
                },
            )

        # 5. Re-check symlink containment right before application & capture exact backups
        backups: dict[str, bytes | None] = {}
        added_paths: list[str] = []
        applied_paths: list[str] = []
        for fc in proposal.affected_files:
            sym_err = _check_path_symlink_safety(ws_repo, source_root, fc.path)
            if sym_err is not None:
                return _failed(sym_err.code, sym_err.message, details=sym_err.details)
            target = ws_repo / fc.path
            backups[fc.path] = target.read_bytes() if target.is_file() else None
            applied_paths.append(fc.path)
            if fc.change_type == "add":
                added_paths.append(fc.path)
            if fc.old_path and fc.old_path not in backups:
                sym_old = _check_path_symlink_safety(ws_repo, source_root, fc.old_path)
                if sym_old is not None:
                    return _failed(sym_old.code, sym_old.message, details=sym_old.details)
                old_target = ws_repo / fc.old_path
                backups[fc.old_path] = (
                    old_target.read_bytes() if old_target.is_file() else None
                )

        # 6. Apply exact validated patch via safe subprocess (never --reject, never shell=True)
        patch_bytes = proposal.unified_diff.encode("utf-8")
        cmd = ["git", "-C", str(ws_repo), "apply", "-"]
        try:
            if self._git_apply_runner is not None:
                proc = self._git_apply_runner(
                    cmd,
                    input=patch_bytes,
                    cwd=str(ws_repo),
                    capture_output=True,
                    timeout=self.config.git_timeout_seconds,
                    check=False,
                )
            else:
                proc = subprocess.run(
                    cmd,
                    input=patch_bytes,
                    cwd=str(ws_repo),
                    capture_output=True,
                    timeout=self.config.git_timeout_seconds,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            self._rollback_files(ws_repo, backups, added_paths)
            clean_ok = _compute_snapshot_sha256(ws_repo, source_path) == expected_source_snapshot_sha256
            return _failed(
                "PATCH_APPLICATION_TIMEOUT",
                f"git apply timed out after {self.config.git_timeout_seconds}s.",
                rollback_performed=True,
                clean_after_rollback=clean_ok,
            )
        except Exception as exc:  # noqa: BLE001
            self._rollback_files(ws_repo, backups, added_paths)
            clean_ok = _compute_snapshot_sha256(ws_repo, source_path) == expected_source_snapshot_sha256
            return _failed(
                "PATCH_APPLICATION_FAILED",
                f"Unexpected error executing git apply: {exc}",
                rollback_performed=True,
                clean_after_rollback=clean_ok,
            )

        if proc.returncode != 0:
            stderr = redact_secrets(
                proc.stderr.decode("utf-8", errors="replace").strip()
                if isinstance(proc.stderr, bytes)
                else str(proc.stderr or "")
            )
            self._rollback_files(ws_repo, backups, added_paths)
            try:
                post_rb_snap = _compute_snapshot_sha256(ws_repo, source_path)
                clean_ok = hmac.compare_digest(
                    post_rb_snap, expected_source_snapshot_sha256
                )
            except WorkspaceError:
                clean_ok = False
            return _failed(
                "PATCH_APPLICATION_FAILED",
                f"git apply failed (exit_code={proc.returncode}): {stderr or 'patch could not be applied'}",
                details={"exit_code": proc.returncode},
                rollback_performed=True,
                clean_after_rollback=clean_ok,
            )

        # 7. Ensure newly added files are tracked via intent-to-add so M01 git diff sees them
        if added_paths:
            add_proc = subprocess.run(
                ["git", "-C", str(ws_repo), "add", "-N", "--", *added_paths],
                cwd=str(ws_repo),
                capture_output=True,
                timeout=self.config.git_timeout_seconds,
                check=False,
            )
            if add_proc.returncode != 0:
                self._rollback_files(ws_repo, backups, added_paths)
                clean_ok = (
                    _compute_snapshot_sha256(ws_repo, source_path)
                    == expected_source_snapshot_sha256
                )
                return _failed(
                    "PATCH_STAGE_ADDED_FAILED",
                    "Failed to register newly added patch file(s) in isolated workspace index.",
                    rollback_performed=True,
                    clean_after_rollback=clean_ok,
                )

        # 8. Recompute resulting snapshot and diff hashes
        try:
            resulting_snapshot = _compute_snapshot_sha256(ws_repo, source_path)
            resulting_diff = _compute_git_diff_sha256(
                ws_repo,
                base_ref,
                source_path,
                timeout_seconds=self.config.git_timeout_seconds,
            )
        except WorkspaceError as exc:
            self._rollback_files(ws_repo, backups, added_paths)
            return _failed(
                "POST_PATCH_SNAPSHOT_FAILED",
                f"Failed to compute post-patch workspace snapshot: {exc}",
                rollback_performed=True,
                clean_after_rollback=None,
            )

        if hmac.compare_digest(resulting_snapshot, expected_source_snapshot_sha256):
            self._rollback_files(ws_repo, backups, added_paths)
            return _failed(
                "PATCH_NO_EFFECTIVE_CHANGE",
                "Patch application completed without changing the workspace source snapshot.",
                rollback_performed=True,
                clean_after_rollback=True,
            )

        return PatchApplicationResult(
            applied=True,
            status="applied",
            patch_sha256=actual_patch_sha,
            original_source_snapshot_sha256=expected_source_snapshot_sha256,
            original_git_diff_sha256=expected_git_diff_sha256,
            resulting_source_snapshot_sha256=resulting_snapshot,
            resulting_git_diff_sha256=resulting_diff,
            applied_files=sorted(set(applied_paths)),
            rollback_performed=False,
            workspace_clean_after_rollback=None,
            error=None,
        )
