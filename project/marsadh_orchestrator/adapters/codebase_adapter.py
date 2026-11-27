"""M01 Codebase Intelligence Adapter wrapping Mujahidh's static analysis pipeline.

Reuses:
- ``project.agents.mujahidh_codebase.codebase_agent.run_codebase_agent``
- ``project.agents.mujahidh_codebase.analyzer.analyze_repository``

Does not modify teammate source code. Normalizes repository-relative and
source-relative paths separately, handles empty diffs and invalid Git refs
explicitly, and preserves raw M01 payloads verbatim.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePath
from typing import Any

from project.agents.mujahidh_codebase.analyzer import analyze_repository
from project.agents.mujahidh_codebase.codebase_agent import run_codebase_agent
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.schemas.agents import (
    AgentError,
    ChangedFileImpact,
    CodebaseAnalysisResult,
    ImpactedFile,
)

_SAFE_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/@^~{}][A-Za-z0-9._/@^~{}-]{0,199}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


def _normalize_source_path(source_path: str) -> str:
    cleaned = source_path.strip()
    if not cleaned:
        raise ValueError("source_path must not be blank")
    if "\x00" in cleaned:
        raise ValueError("source_path must not contain null bytes")
    if cleaned.startswith(("/", "\\")) or _WINDOWS_DRIVE_PATTERN.match(cleaned):
        raise ValueError(f"source_path must be relative to repository root: '{source_path}'")
    normalized = cleaned.replace("\\", "/")
    if ".." in PurePath(normalized).parts:
        raise ValueError(f"source_path must not contain '..': '{source_path}'")
    if normalized in {".", "./"}:
        return "."
    return PurePath(normalized).as_posix()


def _validate_base_ref(base_ref: str) -> str:
    cleaned = base_ref.strip()
    if not cleaned or cleaned.startswith("-"):
        raise ValueError(f"Invalid Git base reference: '{base_ref}'")
    if not _SAFE_GIT_REF_PATTERN.match(cleaned) or ".." in cleaned:
        raise ValueError(f"Unsupported Git base reference syntax: '{base_ref}'")
    return cleaned


class CodebaseAdapter:
    """Adapter around M01 CodebaseIntelligenceAgent producing normalized schemas."""

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config

    def _error_result(
        self,
        *,
        repository_path: str,
        source_path: str,
        base_ref: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        resolved_base_commit: str | None = None,
        raw_report: dict[str, Any] | None = None,
    ) -> CodebaseAnalysisResult:
        return CodebaseAnalysisResult(
            status="error",
            repository_path=repository_path,
            source_path=source_path,
            base_ref=base_ref,
            resolved_base_commit=resolved_base_commit,
            has_relevant_changes=False,
            raw_report=raw_report,
            error=AgentError(code=code, message=message, details=details or {}),
        )

    def _is_path_permitted(self, repo_resolved: Path) -> bool:
        if self.config is None:
            return True
        if self.config.is_repository_allowed(repo_resolved):
            return True
        ws_root = self.config.workspace_root.resolve()
        if repo_resolved.is_relative_to(ws_root):
            return True
        return not self.config.allowed_repository_roots

    def analyze(
        self,
        repository_path: str | Path,
        source_path: str = ".",
        base_ref: str = "HEAD",
    ) -> CodebaseAnalysisResult:
        """Analyze tracked Python changes and reverse-dependency impact via M01."""
        repo_str = str(repository_path)
        git_timeout = self.config.git_timeout_seconds if self.config else 30

        try:
            norm_source = _normalize_source_path(source_path)
        except ValueError as exc:
            return self._error_result(
                repository_path=repo_str,
                source_path=source_path or ".",
                base_ref=base_ref or "HEAD",
                code="INVALID_SOURCE_PATH",
                message=str(exc),
            )

        try:
            clean_base_ref = _validate_base_ref(base_ref)
        except ValueError as exc:
            return self._error_result(
                repository_path=repo_str,
                source_path=norm_source,
                base_ref=base_ref or "HEAD",
                code="INVALID_GIT_REF",
                message=str(exc),
            )

        raw_repo = Path(repository_path)
        if not raw_repo.exists() or not raw_repo.is_dir():
            return self._error_result(
                repository_path=repo_str,
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="REPOSITORY_NOT_FOUND",
                message=f"Repository directory does not exist: '{repository_path}'",
            )

        repo_resolved = raw_repo.resolve(strict=True)
        if not self._is_path_permitted(repo_resolved):
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="REPOSITORY_NOT_ALLOWED",
                message=f"Repository '{repo_resolved}' is outside permitted roots",
            )

        source_candidate = (
            repo_resolved if norm_source == "." else (repo_resolved / norm_source)
        )
        if source_candidate.is_symlink() and not source_candidate.resolve().is_relative_to(
            repo_resolved
        ):
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="SYMLINK_ESCAPE",
                message=f"source_path symlink escapes repository root: '{norm_source}'",
            )
        if not source_candidate.exists() or not source_candidate.is_dir():
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="SOURCE_PATH_NOT_FOUND",
                message=f"Source directory '{norm_source}' not found in '{repo_resolved}'",
            )
        source_resolved = source_candidate.resolve(strict=True)
        if not source_resolved.is_relative_to(repo_resolved):
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="SOURCE_PATH_ESCAPE",
                message=f"Source directory '{norm_source}' escapes repository root",
            )

        # Verify Git repository and resolve base_ref with bounded subprocess timeout
        try:
            ref_proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_resolved),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{clean_base_ref}^{{commit}}",
                ],
                capture_output=True,
                text=True,
                timeout=git_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="GIT_TIMEOUT",
                message=f"Git ref validation timed out after {git_timeout}s",
            )
        except OSError as exc:
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="GIT_EXECUTION_ERROR",
                message=f"Failed to execute git: {exc}",
            )

        if ref_proc.returncode != 0:
            stderr = (ref_proc.stderr or "").strip()
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                code="INVALID_GIT_REF",
                message=f"Invalid Git repository or base_ref '{clean_base_ref}': {stderr}",
            )
        resolved_base_commit = ref_proc.stdout.strip()

        # Invoke existing M01 functions without modifying teammate source
        try:
            raw_repo_analysis = analyze_repository(str(source_resolved))
            raw_report = run_codebase_agent(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
            )
        except subprocess.CalledProcessError as exc:
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                resolved_base_commit=resolved_base_commit,
                code="M01_GIT_ERROR",
                message=f"M01 git diff failed: {(exc.stderr or str(exc)).strip()}",
            )
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                resolved_base_commit=resolved_base_commit,
                code="M01_EXECUTION_ERROR",
                message=f"M01 codebase analysis failed: {exc}",
            )

        # Validate required result fields from M01
        if not isinstance(raw_report, dict):
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                resolved_base_commit=resolved_base_commit,
                code="INVALID_M01_PAYLOAD",
                message="M01 returned a non-dict impact report",
            )

        required_keys = {"agent", "status", "repository_summary", "reports"}
        missing_keys = sorted(required_keys - set(raw_report.keys()))
        if missing_keys or not isinstance(raw_report.get("reports"), list):
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                resolved_base_commit=resolved_base_commit,
                code="INVALID_M01_PAYLOAD",
                message=f"M01 payload missing required fields: {missing_keys}",
                raw_report=raw_report,
            )

        total_indexed = raw_report.get("repository_summary", {}).get("total_files")
        if not isinstance(total_indexed, int):
            total_indexed = (
                raw_repo_analysis.get("total_files")
                if isinstance(raw_repo_analysis, dict)
                else None
            )

        reports = raw_report["reports"]
        if not reports:
            return CodebaseAnalysisResult(
                status="no_changes",
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                resolved_base_commit=resolved_base_commit,
                total_indexed_files=total_indexed,
                has_relevant_changes=False,
                raw_report=raw_report,
                raw_repository_analysis=raw_repo_analysis
                if isinstance(raw_repo_analysis, dict)
                else None,
                assumptions=[
                    "M01 detected no tracked Python changes under source_path relative to base_ref.",
                    "Downstream M03 test generation must be skipped and this state must not be reported as passing verification.",
                ],
            )

        changed_repo_set: set[str] = set()
        changed_source_set: set[str] = set()
        impacted_repo_set: set[str] = set()
        impacted_source_set: set[str] = set()
        file_impacts: list[ChangedFileImpact] = []

        try:
            for entry in reports:
                if not isinstance(entry, dict) or "changed_file" not in entry:
                    raise ValueError("M01 report entry missing 'changed_file'")
                changed_repo_raw = str(entry["changed_file"]).replace("\\", "/")
                changed_abs = (repo_resolved / changed_repo_raw).resolve()
                if not changed_abs.is_relative_to(repo_resolved) or not changed_abs.is_relative_to(
                    source_resolved
                ):
                    raise ValueError(
                        f"M01 changed_file escapes repository or source_path: '{changed_repo_raw}'"
                    )
                changed_repo_rel = changed_abs.relative_to(repo_resolved).as_posix()
                changed_src_rel = changed_abs.relative_to(source_resolved).as_posix()
                changed_repo_set.add(changed_repo_rel)
                changed_source_set.add(changed_src_rel)

                impact_data = entry.get("impact_analysis") or {}
                if not isinstance(impact_data, dict):
                    raise TypeError("M01 report entry has invalid 'impact_analysis'")
                raw_impacted = impact_data.get("potentially_impacted_files", [])
                if not isinstance(raw_impacted, list):
                    raise TypeError("M01 'potentially_impacted_files' must be a list")

                direct_items: list[ImpactedFile] = []
                indirect_items: list[ImpactedFile] = []
                for item in raw_impacted:
                    src_rel_raw = str(item["file"]).replace("\\", "/")
                    impacted_abs = (source_resolved / src_rel_raw).resolve()
                    if not impacted_abs.is_relative_to(source_resolved):
                        raise ValueError(
                            f"M01 impacted file escapes source_path: '{src_rel_raw}'"
                        )
                    imp_src_rel = impacted_abs.relative_to(source_resolved).as_posix()
                    imp_repo_rel = impacted_abs.relative_to(repo_resolved).as_posix()
                    impacted_source_set.add(imp_src_rel)
                    impacted_repo_set.add(imp_repo_rel)

                    level = "direct" if item.get("impact_level") == "direct" else "indirect"
                    normalized_item = ImpactedFile(
                        source_relative_path=imp_src_rel,
                        repo_relative_path=imp_repo_rel,
                        impact_level=level,
                        dependency_depth=int(item.get("dependency_depth", 1)),
                        via_file=str(item.get("via_file", changed_src_rel)).replace("\\", "/"),
                        import_line=item.get("import_line"),
                        reason=str(item.get("reason", "")),
                    )
                    if level == "direct":
                        direct_items.append(normalized_item)
                    else:
                        indirect_items.append(normalized_item)

                file_impacts.append(
                    ChangedFileImpact(
                        repo_relative_path=changed_repo_rel,
                        source_relative_path=changed_src_rel,
                        changed_functions=list(entry.get("changed_functions", [])),
                        changed_lines=[int(ln) for ln in entry.get("changed_lines", [])],
                        direct_impacted_files=direct_items,
                        indirect_impacted_files=indirect_items,
                        total_impacted=len(direct_items) + len(indirect_items),
                        direct_count=len(direct_items),
                        indirect_count=len(indirect_items),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                repository_path=str(repo_resolved),
                source_path=norm_source,
                base_ref=clean_base_ref,
                resolved_base_commit=resolved_base_commit,
                code="INVALID_M01_PAYLOAD",
                message=f"Failed to normalize M01 report payload: {exc}",
                raw_report=raw_report,
            )

        return CodebaseAnalysisResult(
            status="completed",
            repository_path=str(repo_resolved),
            source_path=norm_source,
            base_ref=clean_base_ref,
            resolved_base_commit=resolved_base_commit,
            total_indexed_files=total_indexed,
            has_relevant_changes=True,
            changed_files_repo_relative=sorted(changed_repo_set),
            changed_files_source_relative=sorted(changed_source_set),
            impacted_modules_repo_relative=sorted(impacted_repo_set),
            impacted_modules_source_relative=sorted(impacted_source_set),
            file_impacts=file_impacts,
            raw_report=raw_report,
            raw_repository_analysis=raw_repo_analysis
            if isinstance(raw_repo_analysis, dict)
            else None,
            assumptions=[
                "M01 static reverse-dependency analysis resolves absolute Python imports only; dynamic or relative imports may not appear.",
            ],
        )
