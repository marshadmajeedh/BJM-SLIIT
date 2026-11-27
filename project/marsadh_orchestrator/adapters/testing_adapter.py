"""M03 Regression Test Generation Adapter wrapping Munshif's testing module.

Reuses:
- ``project.agents.munshif_testing.agent.generate_tests``
- ``project.agents.munshif_testing.export.export_scenarios``
- Optional live model (lazy-imported only when ``live_llm`` is explicitly requested):
  ``project.agents.munshif_testing.groq_model.generate``

Guarantees:
- Separate operations for proposal generation and scenario export.
- Never executes generated test code.
- Never calls M03 when M01 reports no relevant changed files.
- Never silently falls back to mock output when ``live_llm`` execution is requested.
- Explicitly labels offline/mock outputs and hashes exported files for downstream
  verification integrity checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path, PurePath

from project.agents.munshif_testing.agent import generate_tests
from project.agents.munshif_testing.export import export_scenarios
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import redact_secrets
from project.schemas.agents import (
    AgentError,
    CodebaseAnalysisResult,
    GeneratedTestScenario,
    TestGenerationResult,
)
from project.schemas.workflow import ExecutionMode

_GROQ_ENV_LOCK = threading.Lock()

_SCENARIO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_SENSITIVE_FILENAMES = frozenset({".env", "id_rsa", "id_ed25519", "secrets.py"})


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_relative_subdir(subdir: str) -> str:
    cleaned = subdir.strip()
    if not cleaned:
        raise ValueError("output_subdir must not be blank")
    if "\x00" in cleaned:
        raise ValueError("output_subdir must not contain null bytes")
    if cleaned.startswith(("/", "\\")) or _WINDOWS_DRIVE_PATTERN.match(cleaned):
        raise ValueError(f"output_subdir must be relative to repository root: '{subdir}'")
    normalized = cleaned.replace("\\", "/")
    parts = PurePath(normalized).parts
    if not parts or ".." in parts or normalized in {".", "./"}:
        raise ValueError(
            f"output_subdir must be a dedicated non-root subdirectory without '..': '{subdir}'"
        )
    return PurePath(normalized).as_posix()


def _contains_sensitive_paths(codebase_result: CodebaseAnalysisResult) -> list[str]:
    flagged: list[str] = []
    all_paths = (
        codebase_result.changed_files_repo_relative
        + codebase_result.impacted_modules_repo_relative
    )
    for rel_path in all_paths:
        name = PurePath(rel_path).name.lower()
        if (
            name in _SENSITIVE_FILENAMES
            or name.startswith(".env.")
            or name.endswith((".pem", ".key"))
        ):
            flagged.append(rel_path)
    return sorted(set(flagged))


class TestingAdapter:
    """Adapter around M03 test proposal generation and safe scenario export."""

    __test__ = False

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.config = config

    def _resolve_live_model(
        self,
        model_callable: Callable[[str], str] | None,
        model_label: str | None,
    ) -> tuple[Callable[[str], str], str]:
        if model_callable is not None:
            return model_callable, model_label or "custom_live_model"

        api_key = (
            self.config.get_groq_api_key()
            if self.config and self.config.get_groq_api_key()
            else os.environ.get("GROQ_API_KEY", "").strip()
        )
        groq_model = (
            self.config.groq_model
            if self.config and self.config.groq_model
            else os.environ.get("GROQ_MODEL", "").strip()
        )
        if not api_key or not groq_model:
            raise RuntimeError(
                "Live LLM execution requested, but GROQ_API_KEY and GROQ_MODEL are not "
                "configured. Refusing to fall back to mock output."
            )

        # Lazy import so importing TestingAdapter never initializes external AI SDKs.
        from project.agents.munshif_testing.groq_model import generate as groq_generate

        def _invoke_groq(prompt: str) -> str:
            with _GROQ_ENV_LOCK:
                prev_key = os.environ.get("GROQ_API_KEY")
                prev_model = os.environ.get("GROQ_MODEL")
                try:
                    os.environ["GROQ_API_KEY"] = api_key
                    os.environ["GROQ_MODEL"] = groq_model
                    return groq_generate(prompt)
                finally:
                    if prev_key is None:
                        os.environ.pop("GROQ_API_KEY", None)
                    else:
                        os.environ["GROQ_API_KEY"] = prev_key
                    if prev_model is None:
                        os.environ.pop("GROQ_MODEL", None)
                    else:
                        os.environ["GROQ_MODEL"] = prev_model

        return _invoke_groq, model_label or f"groq:{groq_model}"

    def generate_proposals(
        self,
        repository_path: str | Path,
        codebase_result: CodebaseAnalysisResult,
        requirements: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST,
        model_callable: Callable[[str], str] | None = None,
        model_label: str | None = None,
    ) -> TestGenerationResult:
        """Generate unexecuted pytest proposals from a validated M01 impact report."""
        is_offline = execution_mode == ExecutionMode.OFFLINE_TEST
        mode_value = execution_mode.value

        # Do not invoke M03 when M01 found no relevant changed files.
        raw_report = codebase_result.raw_report
        reports = raw_report.get("reports", []) if isinstance(raw_report, dict) else []
        if (
            codebase_result.status == "no_changes"
            or not codebase_result.has_relevant_changes
            or not reports
        ):
            return TestGenerationResult(
                status="skipped_no_changes",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=model_label or ("deterministic_offline_stub" if is_offline else "uninvoked"),
                assumptions=[
                    "Skipped M03 test generation because M01 reported no relevant changed files.",
                ],
            )

        if codebase_result.status != "completed":
            return TestGenerationResult(
                status="error",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=model_label or "uninvoked",
                error=AgentError(
                    code="INVALID_CODEBASE_RESULT",
                    message="Cannot generate tests from a failed codebase analysis result",
                ),
            )

        repo_path = Path(repository_path)
        if not repo_path.exists() or not repo_path.is_dir():
            return TestGenerationResult(
                status="error",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=model_label or "uninvoked",
                error=AgentError(
                    code="REPOSITORY_NOT_FOUND",
                    message=f"Repository directory does not exist: '{repository_path}'",
                ),
            )
        repo_resolved = repo_path.resolve(strict=True)

        if not is_offline:
            sensitive = _contains_sensitive_paths(codebase_result)
            if sensitive:
                return TestGenerationResult(
                    status="error",
                    executed=False,
                    generation_mode="live_llm",
                    is_mock_output=False,
                    model_label=model_label or "blocked_sensitive_context",
                    error=AgentError(
                        code="SENSITIVE_FILES_BLOCKED",
                        message=(
                            "Refusing to send potentially sensitive files to external LLM: "
                            + ", ".join(sensitive)
                        ),
                    ),
                )
            try:
                active_model, resolved_label = self._resolve_live_model(
                    model_callable, model_label
                )
            except Exception as exc:  # noqa: BLE001
                return TestGenerationResult(
                    status="error",
                    executed=False,
                    generation_mode="live_llm",
                    is_mock_output=False,
                    model_label=model_label or "groq:unconfigured",
                    error=AgentError(
                        code="LIVE_LLM_UNCONFIGURED",
                        message=str(exc),
                    ),
                )
        else:
            if model_callable is None:
                return TestGenerationResult(
                    status="error",
                    executed=False,
                    generation_mode="offline_test",
                    is_mock_output=True,
                    model_label=model_label or "missing_stub",
                    error=AgentError(
                        code="MODEL_CALLABLE_REQUIRED",
                        message="Offline test mode requires an explicit deterministic model_callable stub",
                    ),
                )
            active_model = model_callable
            resolved_label = model_label or "deterministic_offline_stub"

        try:
            raw_proposals = generate_tests(
                repo_resolved,
                raw_report,
                active_model,
                requirements,
            )
        except json.JSONDecodeError as exc:
            return TestGenerationResult(
                status="error",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=resolved_label,
                error=AgentError(
                    code="INVALID_MODEL_JSON",
                    message=f"Model returned invalid JSON: {exc}",
                    retryable=True,
                ),
            )
        except ValueError as exc:
            return TestGenerationResult(
                status="error",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=resolved_label,
                error=AgentError(
                    code="INVALID_MODEL_OUTPUT",
                    message=str(exc),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return TestGenerationResult(
                status="error",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=resolved_label,
                error=AgentError(
                    code="M03_GENERATION_ERROR",
                    message=redact_secrets(f"M03 test generation failed: {exc}"),
                ),
            )

        # Validate output status and assign stable scenario identifiers
        if (
            not isinstance(raw_proposals, dict)
            or raw_proposals.get("status") != "proposed"
            or raw_proposals.get("executed") is not False
            or not isinstance(raw_proposals.get("scenarios"), list)
            or not raw_proposals["scenarios"]
        ):
            return TestGenerationResult(
                status="error",
                executed=False,
                generation_mode=mode_value,
                is_mock_output=is_offline,
                model_label=resolved_label,
                raw_proposals=raw_proposals if isinstance(raw_proposals, dict) else None,
                error=AgentError(
                    code="INVALID_M03_PAYLOAD",
                    message="M03 did not return a valid unexecuted proposed scenarios payload",
                ),
            )

        scenarios: list[GeneratedTestScenario] = []
        for idx, item in enumerate(raw_proposals["scenarios"], start=1):
            test_code = str(item["test_code"])
            scenarios.append(
                GeneratedTestScenario(
                    scenario_id=f"SCN-{idx:03d}",
                    title=str(item["title"]).strip(),
                    rationale=str(item["rationale"]).strip(),
                    assumptions=str(item["assumptions"]).strip(),
                    test_code=test_code,
                    content_sha256=_sha256_bytes(test_code.encode("utf-8")),
                    approved=False,
                )
            )

        assumptions = [
            "Generated scenarios are unexecuted proposals and must be treated as untrusted code until reviewed and approved.",
        ]
        if is_offline:
            assumptions.append(
                "Generated using an offline deterministic stub (is_mock_output=True); do not present as live AI-generated evidence."
            )

        return TestGenerationResult(
            status="proposed",
            executed=False,
            generation_mode=mode_value,
            is_mock_output=is_offline,
            model_label=resolved_label,
            scenarios=scenarios,
            raw_proposals=raw_proposals,
            assumptions=assumptions,
        )

    def export_selected_scenarios(
        self,
        repository_path: str | Path,
        generation_result: TestGenerationResult,
        selected_scenario_ids: list[str] | None = None,
        *,
        output_subdir: str = ".changeguard_generated_tests",
    ) -> TestGenerationResult:
        """Export selected scenarios into an empty run-specific directory without executing them."""
        if generation_result.status != "proposed" or not generation_result.scenarios:
            return generation_result.model_copy(
                update={
                    "status": "error",
                    "error": AgentError(
                        code="NO_PROPOSALS_TO_EXPORT",
                        message="Cannot export scenarios from a non-proposed or empty generation result",
                    ),
                }
            )

        repo_path = Path(repository_path)
        if not repo_path.exists() or not repo_path.is_dir():
            return generation_result.model_copy(
                update={
                    "status": "error",
                    "error": AgentError(
                        code="REPOSITORY_NOT_FOUND",
                        message=f"Repository directory does not exist: '{repository_path}'",
                    ),
                }
            )
        repo_resolved = repo_path.resolve(strict=True)

        try:
            norm_subdir = _validate_relative_subdir(output_subdir)
        except ValueError as exc:
            return generation_result.model_copy(
                update={
                    "status": "error",
                    "error": AgentError(
                        code="INVALID_EXPORT_DIR",
                        message=str(exc),
                    ),
                }
            )

        export_dir_raw = repo_resolved / norm_subdir
        if export_dir_raw.is_symlink() and not export_dir_raw.resolve().is_relative_to(
            repo_resolved
        ):
            return generation_result.model_copy(
                update={
                    "status": "error",
                    "error": AgentError(
                        code="EXPORT_DIR_SYMLINK_ESCAPE",
                        message=f"Export directory symlink escapes repository root: '{norm_subdir}'",
                    ),
                }
            )
        export_dir = export_dir_raw.resolve()
        if not export_dir.is_relative_to(repo_resolved) or export_dir == repo_resolved:
            return generation_result.model_copy(
                update={
                    "status": "error",
                    "error": AgentError(
                        code="EXPORT_DIR_ESCAPE",
                        message=f"Export directory escapes repository root: '{norm_subdir}'",
                    ),
                }
            )

        if export_dir.exists():
            if not export_dir.is_dir():
                return generation_result.model_copy(
                    update={
                        "status": "error",
                        "error": AgentError(
                            code="EXPORT_DIR_NOT_DIRECTORY",
                            message=f"Export target already exists and is not a directory: '{norm_subdir}'",
                        ),
                    }
                )
            if any(export_dir.iterdir()):
                return generation_result.model_copy(
                    update={
                        "status": "error",
                        "error": AgentError(
                            code="EXPORT_DIR_NOT_EMPTY",
                            message=f"Refusing to overwrite non-empty export directory: '{norm_subdir}'",
                        ),
                    }
                )

        by_id = {sc.scenario_id: sc for sc in generation_result.scenarios}
        if selected_scenario_ids is None:
            chosen = list(generation_result.scenarios)
        else:
            if not selected_scenario_ids:
                return generation_result.model_copy(
                    update={
                        "status": "error",
                        "error": AgentError(
                            code="EMPTY_SCENARIO_SELECTION",
                            message="selected_scenario_ids must not be empty when explicitly supplied",
                        ),
                    }
                )
            chosen = []
            seen: set[str] = set()
            for raw_id in selected_scenario_ids:
                sc_id = raw_id.strip()
                if not _SCENARIO_ID_PATTERN.match(sc_id) or sc_id not in by_id:
                    return generation_result.model_copy(
                        update={
                            "status": "error",
                            "error": AgentError(
                                code="UNKNOWN_SCENARIO_ID",
                                message=f"Unknown or invalid scenario_id: '{raw_id}'",
                            ),
                        }
                    )
                if sc_id not in seen:
                    seen.add(sc_id)
                    chosen.append(by_id[sc_id])

        filtered_proposals = {
            "agent": "RegressionTestGenerationAgent",
            "status": "proposed",
            "executed": False,
            "scenarios": [
                {
                    "title": sc.title,
                    "rationale": sc.rationale,
                    "assumptions": sc.assumptions,
                    "test_code": sc.test_code,
                }
                for sc in chosen
            ],
        }

        try:
            exported_abs_paths = export_scenarios(filtered_proposals, export_dir)
        except Exception as exc:  # noqa: BLE001
            return generation_result.model_copy(
                update={
                    "status": "error",
                    "error": AgentError(
                        code="M03_EXPORT_ERROR",
                        message=f"Failed to export scenarios: {exc}",
                    ),
                }
            )

        exported_files: dict[str, str] = {}
        exported_hashes: dict[str, str] = {}
        updated_map: dict[str, GeneratedTestScenario] = {}

        for sc, abs_path_str in zip(chosen, exported_abs_paths, strict=True):
            abs_path = Path(abs_path_str).resolve(strict=True)
            if not abs_path.is_relative_to(repo_resolved):
                return generation_result.model_copy(
                    update={
                        "status": "error",
                        "error": AgentError(
                            code="EXPORT_PATH_ESCAPE",
                            message=f"Exported file path escaped repository root: '{abs_path}'",
                        ),
                    }
                )
            rel_posix = abs_path.relative_to(repo_resolved).as_posix()
            file_sha = _sha256_bytes(abs_path.read_bytes())
            exported_files[sc.scenario_id] = rel_posix
            exported_hashes[sc.scenario_id] = file_sha
            updated_map[sc.scenario_id] = sc.model_copy(
                update={
                    "exported_relative_path": rel_posix,
                    "exported_sha256": file_sha,
                }
            )

        updated_scenarios = [
            updated_map.get(sc.scenario_id, sc) for sc in generation_result.scenarios
        ]

        return generation_result.model_copy(
            update={
                "scenarios": updated_scenarios,
                "export_dir_relative": export_dir.relative_to(repo_resolved).as_posix(),
                "exported_files": exported_files,
                "exported_hashes": exported_hashes,
                "error": None,
            }
        )
