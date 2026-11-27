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
from project.marsadh_orchestrator.store import redact_secrets
from project.schemas.agents import (
    AgentError,
    ChangedFileImpact,
    CodebaseAnalysisResult,
    ImpactedFile,
)
from project.schemas.patch import (
    PreChangeClassMetadata,
    PreChangeCodebaseContext,
    PreChangeFileSummary,
    PreChangeFunctionMetadata,
    PreChangeImportMetadata,
)

_SAFE_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9._/@^~{}][A-Za-z0-9._/@^~{}-]{0,199}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_PRE_CHANGE_MAX_FILES = 20
_PRE_CHANGE_MAX_FILE_BYTES = 32_768
_PRE_CHANGE_MAX_TOTAL_BYTES = 131_072
_PRE_CHANGE_MAX_SYMBOL_NAME_LEN = 300
_PRE_CHANGE_MAX_WARNINGS_PER_FILE = 25
_PRE_CHANGE_MAX_IMPORT_LEVEL = 64
_VALID_FUNCTION_KINDS = frozenset({"function", "method", "async_function"})
_VALID_IMPORT_KINDS = frozenset({"absolute", "relative"})
_PRE_CHANGE_SENSITIVE_NAMES = frozenset(
    {".env", "id_rsa", "id_ed25519", "secrets.py", "credentials.py"}
)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sanitize_symbol_name(
    raw_name: str,
    *,
    section: str,
    index: int,
    warnings: list[str],
) -> str:
    cleaned = redact_secrets(raw_name.strip())
    if len(cleaned) > _PRE_CHANGE_MAX_SYMBOL_NAME_LEN:
        warnings.append(
            f"Truncated oversized '{section}' symbol name at index {index} "
            f"to {_PRE_CHANGE_MAX_SYMBOL_NAME_LEN} characters."
        )
        cleaned = cleaned[:_PRE_CHANGE_MAX_SYMBOL_NAME_LEN]
    return cleaned


def _parse_optional_line_bounds(
    entry: dict[str, Any],
    *,
    section: str,
    index: int,
    warnings: list[str],
) -> tuple[int | None, int | None]:
    line: int | None = None
    end_line: int | None = None

    if "line" in entry and entry["line"] is not None:
        raw_line = entry["line"]
        if _is_positive_int(raw_line):
            line = raw_line
        else:
            warnings.append(
                f"Ignored invalid 'line' in '{section}' entry at index {index}: "
                f"expected positive int, got {type(raw_line).__name__}."
            )

    if "end_line" in entry and entry["end_line"] is not None:
        raw_end = entry["end_line"]
        if _is_positive_int(raw_end):
            end_line = raw_end
        else:
            warnings.append(
                f"Ignored invalid 'end_line' in '{section}' entry at index {index}: "
                f"expected positive int, got {type(raw_end).__name__}."
            )

    if line is not None and end_line is not None and end_line < line:
        warnings.append(
            f"Ignored inconsistent 'end_line' ({end_line} < line {line}) "
            f"in '{section}' entry at index {index}."
        )
        end_line = None

    return line, end_line


def _normalize_function_entries(
    item: dict[str, Any],
) -> tuple[list[str], list[PreChangeFunctionMetadata], list[str]]:
    if "functions" not in item:
        return [], [], []
    raw_entries = item["functions"]
    if not isinstance(raw_entries, list):
        return (
            [],
            [],
            [
                f"Malformed 'functions' metadata: expected list, got {type(raw_entries).__name__}."
            ],
        )

    names: list[str] = []
    metadata: list[PreChangeFunctionMetadata] = []
    warnings: list[str] = []

    for idx, entry in enumerate(raw_entries):
        if isinstance(entry, str):
            cleaned = _sanitize_symbol_name(
                entry, section="functions", index=idx, warnings=warnings
            )
            if not cleaned:
                warnings.append(f"Skipped blank string in 'functions' at index {idx}.")
                continue
            names.append(cleaned)
            metadata.append(PreChangeFunctionMetadata(name=cleaned))
        elif isinstance(entry, dict):
            raw_name = entry.get("name")
            if not isinstance(raw_name, str):
                warnings.append(
                    f"Skipped malformed 'functions' entry at index {idx}: "
                    "missing or non-string 'name'."
                )
                continue
            cleaned = _sanitize_symbol_name(
                raw_name, section="functions", index=idx, warnings=warnings
            )
            if not cleaned:
                warnings.append(
                    f"Skipped malformed 'functions' entry at index {idx}: blank 'name'."
                )
                continue
            line, end_line = _parse_optional_line_bounds(
                entry, section="functions", index=idx, warnings=warnings
            )
            kind: str | None = None
            if "kind" in entry and entry["kind"] is not None:
                raw_kind = entry["kind"]
                if isinstance(raw_kind, str) and raw_kind.strip() in _VALID_FUNCTION_KINDS:
                    kind = raw_kind.strip()
                else:
                    warnings.append(
                        f"Ignored invalid 'kind' in 'functions' entry at index {idx}."
                    )
            names.append(cleaned)
            metadata.append(
                PreChangeFunctionMetadata(
                    name=cleaned,
                    line=line,
                    end_line=end_line,
                    kind=kind,  # type: ignore[arg-type]
                )
            )
        else:
            warnings.append(
                f"Skipped malformed 'functions' entry at index {idx}: "
                f"expected dict or str, got {type(entry).__name__}."
            )

    return names, metadata, warnings


def _normalize_class_entries(
    item: dict[str, Any],
) -> tuple[list[str], list[PreChangeClassMetadata], list[str]]:
    if "classes" not in item:
        return [], [], []
    raw_entries = item["classes"]
    if not isinstance(raw_entries, list):
        return (
            [],
            [],
            [
                f"Malformed 'classes' metadata: expected list, got {type(raw_entries).__name__}."
            ],
        )

    names: list[str] = []
    metadata: list[PreChangeClassMetadata] = []
    warnings: list[str] = []

    for idx, entry in enumerate(raw_entries):
        if isinstance(entry, str):
            cleaned = _sanitize_symbol_name(
                entry, section="classes", index=idx, warnings=warnings
            )
            if not cleaned:
                warnings.append(f"Skipped blank string in 'classes' at index {idx}.")
                continue
            names.append(cleaned)
            metadata.append(PreChangeClassMetadata(name=cleaned))
        elif isinstance(entry, dict):
            raw_name = entry.get("name")
            if not isinstance(raw_name, str):
                warnings.append(
                    f"Skipped malformed 'classes' entry at index {idx}: "
                    "missing or non-string 'name'."
                )
                continue
            cleaned = _sanitize_symbol_name(
                raw_name, section="classes", index=idx, warnings=warnings
            )
            if not cleaned:
                warnings.append(
                    f"Skipped malformed 'classes' entry at index {idx}: blank 'name'."
                )
                continue
            line, end_line = _parse_optional_line_bounds(
                entry, section="classes", index=idx, warnings=warnings
            )
            names.append(cleaned)
            metadata.append(
                PreChangeClassMetadata(
                    name=cleaned,
                    line=line,
                    end_line=end_line,
                )
            )
        else:
            warnings.append(
                f"Skipped malformed 'classes' entry at index {idx}: "
                f"expected dict or str, got {type(entry).__name__}."
            )

    return names, metadata, warnings


def _normalize_import_entries(
    item: dict[str, Any],
) -> tuple[list[str], list[PreChangeImportMetadata], list[str]]:
    if "imports" not in item:
        return [], [], []
    raw_entries = item["imports"]
    if not isinstance(raw_entries, list):
        return (
            [],
            [],
            [
                f"Malformed 'imports' metadata: expected list, got {type(raw_entries).__name__}."
            ],
        )

    names: list[str] = []
    metadata: list[PreChangeImportMetadata] = []
    warnings: list[str] = []

    for idx, entry in enumerate(raw_entries):
        if isinstance(entry, str):
            cleaned = _sanitize_symbol_name(
                entry, section="imports", index=idx, warnings=warnings
            )
            if not cleaned:
                warnings.append(f"Skipped blank string in 'imports' at index {idx}.")
                continue
            names.append(cleaned)
            metadata.append(PreChangeImportMetadata(name=cleaned))
        elif isinstance(entry, dict):
            raw_name = entry.get("name")
            if raw_name is None:
                cleaned = ""
            elif isinstance(raw_name, str):
                cleaned = _sanitize_symbol_name(
                    raw_name, section="imports", index=idx, warnings=warnings
                )
            else:
                warnings.append(
                    f"Skipped malformed 'imports' entry at index {idx}: "
                    f"invalid 'name' type {type(raw_name).__name__}."
                )
                continue

            line: int | None = None
            if "line" in entry and entry["line"] is not None:
                raw_line = entry["line"]
                if _is_positive_int(raw_line):
                    line = raw_line
                else:
                    warnings.append(
                        f"Ignored invalid 'line' in 'imports' entry at index {idx}: "
                        f"expected positive int, got {type(raw_line).__name__}."
                    )

            kind: str | None = None
            if "kind" in entry and entry["kind"] is not None:
                raw_kind = entry["kind"]
                if isinstance(raw_kind, str) and raw_kind.strip() in _VALID_IMPORT_KINDS:
                    kind = raw_kind.strip()
                else:
                    warnings.append(
                        f"Ignored invalid 'kind' in 'imports' entry at index {idx}."
                    )

            level: int | None = None
            if "level" in entry and entry["level"] is not None:
                raw_level = entry["level"]
                if (
                    _is_non_negative_int(raw_level)
                    and raw_level <= _PRE_CHANGE_MAX_IMPORT_LEVEL
                ):
                    level = raw_level
                else:
                    warnings.append(
                        f"Ignored invalid 'level' in 'imports' entry at index {idx}."
                    )

            if kind == "absolute" and level is not None and level > 0:
                warnings.append(
                    f"Ignored contradictory import kind='absolute' with level={level} "
                    f"at index {idx}."
                )
                kind = None
                level = None
            elif kind == "relative" and level is not None and level == 0:
                warnings.append(
                    f"Ignored contradictory import kind='relative' with level=0 "
                    f"at index {idx}."
                )
                kind = None
                level = None

            if not cleaned and not (level is not None and level > 0):
                warnings.append(
                    f"Skipped malformed 'imports' entry at index {idx}: "
                    "empty import name without positive relative level."
                )
                continue

            if level is not None and level > 0:
                display_name = (
                    cleaned if cleaned.startswith(".") else ("." * level) + cleaned
                )
            else:
                display_name = cleaned

            names.append(display_name)
            metadata.append(
                PreChangeImportMetadata(
                    name=cleaned,
                    line=line,
                    kind=kind,  # type: ignore[arg-type]
                    level=level,
                )
            )
        else:
            warnings.append(
                f"Skipped malformed 'imports' entry at index {idx}: "
                f"expected dict or str, got {type(entry).__name__}."
            )

    return names, metadata, warnings


def _bound_metadata_warnings(warnings: list[str]) -> list[str]:
    sanitized = [redact_secrets(w)[:500] for w in warnings if w and w.strip()]
    if len(sanitized) <= _PRE_CHANGE_MAX_WARNINGS_PER_FILE:
        return sanitized
    omitted = len(sanitized) - _PRE_CHANGE_MAX_WARNINGS_PER_FILE
    return [
        *sanitized[:_PRE_CHANGE_MAX_WARNINGS_PER_FILE],
        f"Omitted {omitted} additional malformed AST metadata warning(s).",
    ]


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
            error=AgentError(code=code, message=redact_secrets(message), details=details or {}),
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
                "M01 static reverse-dependency analysis resolves static absolute and relative Python imports via AST; dynamic imports or runtime sys.path dependencies may not appear.",
            ],
        )

    def collect_pre_change_context(
        self,
        repository_path: str | Path,
        source_path: str = ".",
        base_ref: str = "HEAD",
        *,
        target_modules: list[str] | None = None,
        max_files: int = _PRE_CHANGE_MAX_FILES,
        max_file_bytes: int = _PRE_CHANGE_MAX_FILE_BYTES,
        max_total_bytes: int = _PRE_CHANGE_MAX_TOTAL_BYTES,
    ) -> tuple[PreChangeCodebaseContext, dict[str, str]]:
        """Collect available pre-change static repository context for A4 without requiring a Git diff.

        Uses M01's ``analyze_repository`` for static AST indexing of Python files
        under ``source_path`` and returns bounded, non-sensitive source file
        contents separately so raw file contents are not leaked into public API
        responses. Explicitly declares ``pre_change_impact_prediction_available=False``
        because M01's impact engine is diff-driven.
        """
        git_timeout = self.config.git_timeout_seconds if self.config else 30

        def _unavailable(
            *,
            norm_src: str,
            ref_str: str,
            code: str,
            message: str,
            resolved_commit: str | None = None,
        ) -> tuple[PreChangeCodebaseContext, dict[str, str]]:
            return (
                PreChangeCodebaseContext(
                    status="unavailable",
                    scope="unavailable",
                    pre_change_impact_prediction_available=False,
                    source_path=norm_src,
                    base_ref=ref_str,
                    resolved_base_commit=resolved_commit,
                    total_indexed_files=0,
                    limitation_notes=[
                        "Pre-change codebase context is unavailable; cannot proceed with patch generation.",
                    ],
                    error=AgentError(code=code, message=redact_secrets(message)),
                ),
                {},
            )

        try:
            norm_source = _normalize_source_path(source_path)
        except ValueError as exc:
            return _unavailable(
                norm_src=source_path or ".",
                ref_str=base_ref or "HEAD",
                code="INVALID_SOURCE_PATH",
                message=str(exc),
            )

        try:
            clean_base_ref = _validate_base_ref(base_ref)
        except ValueError as exc:
            return _unavailable(
                norm_src=norm_source,
                ref_str=base_ref or "HEAD",
                code="INVALID_GIT_REF",
                message=str(exc),
            )

        raw_repo = Path(repository_path)
        if not raw_repo.exists() or not raw_repo.is_dir():
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=f"Repository directory does not exist: '{repository_path}'",
            )
        repo_resolved = raw_repo.resolve(strict=True)
        if not self._is_path_permitted(repo_resolved):
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="REPOSITORY_NOT_ALLOWED",
                message=f"Repository '{repo_resolved}' is outside permitted roots",
            )

        source_candidate = (
            repo_resolved if norm_source == "." else (repo_resolved / norm_source)
        )
        if source_candidate.is_symlink() and not source_candidate.resolve().is_relative_to(
            repo_resolved
        ):
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="SYMLINK_ESCAPE",
                message=f"source_path symlink escapes repository root: '{norm_source}'",
            )
        if not source_candidate.exists() or not source_candidate.is_dir():
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=f"Source directory '{norm_source}' not found in repository",
            )
        source_resolved = source_candidate.resolve(strict=True)
        if not source_resolved.is_relative_to(repo_resolved):
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="SOURCE_PATH_ESCAPE",
                message=f"Source directory '{norm_source}' escapes repository root",
            )

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
        except (subprocess.TimeoutExpired, OSError) as exc:
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=f"Failed to resolve Git base ref '{clean_base_ref}': {exc}",
            )
        if ref_proc.returncode != 0:
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                code="INVALID_GIT_REF",
                message=(
                    f"Invalid Git repository or base_ref '{clean_base_ref}': "
                    f"{(ref_proc.stderr or '').strip()}"
                ),
            )
        resolved_base_commit = ref_proc.stdout.strip()

        try:
            raw_repo_analysis = analyze_repository(str(source_resolved))
        except Exception as exc:  # noqa: BLE001
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                resolved_commit=resolved_base_commit,
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=f"M01 static repository indexing failed: {exc}",
            )

        raw_files = (
            raw_repo_analysis.get("files", [])
            if isinstance(raw_repo_analysis, dict)
            else []
        )
        if not isinstance(raw_files, list) or not raw_files:
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                resolved_commit=resolved_base_commit,
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=(
                    f"No indexable Python source files found under source_path='{norm_source}'."
                ),
            )

        indexed_summaries: list[PreChangeFileSummary] = []
        indexed_repo_paths: list[str] = []
        candidate_files: list[tuple[int, str, Path]] = []
        hint_tokens = {tok.strip().lower() for tok in (target_modules or []) if tok.strip()}
        malformed_file_entries = 0
        total_metadata_warnings = 0
        files_with_metadata_warnings = 0

        for item in raw_files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip():
                malformed_file_entries += 1
                continue
            src_rel = item["path"].strip().replace("\\", "/")
            file_candidate = source_resolved / src_rel
            if file_candidate.is_symlink():
                continue
            try:
                file_abs = file_candidate.resolve(strict=True)
            except OSError:
                continue
            if not file_abs.is_relative_to(source_resolved) or not file_abs.is_file():
                continue

            fname_lower = file_abs.name.lower()
            if (
                fname_lower in _PRE_CHANGE_SENSITIVE_NAMES
                or fname_lower.startswith(".env")
                or fname_lower.endswith((".pem", ".key"))
            ):
                continue

            repo_rel = file_abs.relative_to(repo_resolved).as_posix()
            indexed_repo_paths.append(repo_rel)

            fn_names, fn_meta, fn_warnings = _normalize_function_entries(item)
            cls_names, cls_meta, cls_warnings = _normalize_class_entries(item)
            imp_names, imp_meta, imp_warnings = _normalize_import_entries(item)
            raw_warnings = [*fn_warnings, *cls_warnings, *imp_warnings]
            bounded_warnings = _bound_metadata_warnings(raw_warnings)
            if raw_warnings:
                total_metadata_warnings += len(raw_warnings)
                files_with_metadata_warnings += 1

            raw_parse_error = item.get("error")
            parse_error = (
                redact_secrets(str(raw_parse_error))[:1000]
                if raw_parse_error
                else None
            )

            indexed_summaries.append(
                PreChangeFileSummary(
                    repo_relative_path=repo_rel,
                    source_relative_path=src_rel,
                    functions=fn_names,
                    classes=cls_names,
                    imports=imp_names,
                    function_metadata=fn_meta,
                    class_metadata=cls_meta,
                    import_metadata=imp_meta,
                    metadata_warnings=bounded_warnings,
                    parse_error=parse_error,
                )
            )

            stem_lower = file_abs.stem.lower()
            priority = 0 if (hint_tokens and stem_lower in hint_tokens) else 1
            candidate_files.append((priority, repo_rel, file_abs))

        if not indexed_summaries:
            return _unavailable(
                norm_src=norm_source,
                ref_str=clean_base_ref,
                resolved_commit=resolved_base_commit,
                code="PRE_CHANGE_CONTEXT_UNAVAILABLE",
                message=(
                    f"No safe, non-sensitive Python source files could be indexed under '{norm_source}'."
                ),
            )

        relevant_sources: dict[str, str] = {}
        total_bytes = 0
        for _prio, repo_rel, file_abs in sorted(
            candidate_files, key=lambda entry: (entry[0], entry[1])
        ):
            if len(relevant_sources) >= max_files:
                break
            try:
                raw_bytes = file_abs.read_bytes()
            except OSError:
                continue
            if len(raw_bytes) > max_file_bytes or total_bytes + len(raw_bytes) > max_total_bytes:
                continue
            try:
                text = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                continue
            relevant_sources[repo_rel] = redact_secrets(text)
            total_bytes += len(raw_bytes)

        limitation_notes = [
            "Pre-change codebase context is limited to static AST file/symbol indexing from M01 analyze_repository.",
            "Current M01 impact analysis (run_codebase_agent) is diff-driven and does not provide pre-change impact prediction; full reverse-dependency impact analysis executes after patch application.",
            f"Provided {len(relevant_sources)} bounded non-sensitive source file(s) under source_path='{norm_source}' to A4.",
        ]
        if malformed_file_entries:
            limitation_notes.append(
                f"Skipped {malformed_file_entries} malformed M01 file metadata entry/entries missing a valid path."
            )
        if total_metadata_warnings:
            limitation_notes.append(
                f"Recorded {total_metadata_warnings} malformed AST metadata warning(s) across "
                f"{files_with_metadata_warnings} indexed file(s); valid symbols were preserved."
            )

        context = PreChangeCodebaseContext(
            status="completed",
            scope="static_repository_index_only",
            pre_change_impact_prediction_available=False,
            source_path=norm_source,
            base_ref=clean_base_ref,
            resolved_base_commit=resolved_base_commit,
            total_indexed_files=len(indexed_summaries),
            indexed_file_paths=sorted(indexed_repo_paths),
            indexed_files=indexed_summaries,
            relevant_file_paths=sorted(relevant_sources.keys()),
            limitation_notes=limitation_notes,
        )
        return context, relevant_sources
