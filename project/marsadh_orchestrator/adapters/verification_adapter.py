"""M04 Verification Adapter wrapping only integrable Nafris verification helpers.

Reuses ONLY:
- ``project.nafris_verification.generated_test_runner.run_reviewed_tests``
- ``project.nafris_verification.failure_analyzer.analyze_failure``
- ``project.nafris_verification.risk_scorer.calculate_risk``

Explicitly does NOT import ``verification_agent.py``, ``benchmark_engine.py``,
``test_runner.py``, ``ai_analyzer.py``, or ``regression_detector.py``.

Critical safeguards:
- Executes only on explicitly trusted, validated working copies.
- Accepts stable approved test identifiers mapped to controlled paths and
  verifies SHA-256 content integrity before execution.
- Inspects ``exit_code`` explicitly so collection errors, zero-test runs,
  timeouts, and runner errors can NEVER be misclassified as passing verification.
- Never fabricates coverage, baseline regression deltas, risk scores, or AI
  root-cause analysis.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

from project.marsadh_orchestrator.config import OrchestratorConfig
from project.nafris_verification.failure_analyzer import analyze_failure
from project.nafris_verification.generated_test_runner import run_reviewed_tests
from project.nafris_verification.risk_scorer import calculate_risk
from project.schemas.agents import (
    AgentError,
    ApprovedTestSpec,
    BaselineComparisonEvidence,
    FailureDiagnostic,
    RiskSummary,
    TestGenerationResult,
    VerificationOutcome,
    VerificationResult,
)
from project.schemas.workflow import TestReviewRecord, TestReviewRequest

_SCENARIO_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_MAX_EXCERPT_CHARS = 8000


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truncate(text: str | None, limit: int = _MAX_EXCERPT_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "\n...[truncated]"


def _extract_collection_errors(stdout: str, stderr: str) -> list[str]:
    combined = "\n".join(part for part in (stdout, stderr) if part)
    errors: list[str] = []
    for line in combined.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith(
                (
                    "ERROR collecting ",
                    "E   SyntaxError:",
                    "E   IndentationError:",
                    "E   ImportError:",
                    "E   ModuleNotFoundError:",
                )
            )
            or "error during collection" in stripped.lower()
            or "errors during collection" in stripped.lower()
        ):
            errors.append(stripped)
    return errors


def _validate_relative_test_path(raw_path: str) -> str:
    cleaned = raw_path.strip()
    if not cleaned or "\x00" in cleaned:
        raise ValueError("Test path must be a non-empty relative path without null bytes")
    if cleaned.startswith(("/", "\\")) or _WINDOWS_DRIVE_PATTERN.match(cleaned):
        raise ValueError(f"Test path must be relative to workspace: '{raw_path}'")
    normalized = cleaned.replace("\\", "/")
    parts = PurePath(normalized).parts
    if not parts or ".." in parts:
        raise ValueError(f"Test path must not contain '..': '{raw_path}'")
    return PurePath(normalized).as_posix()


def build_approved_test_specs(
    generation_result: TestGenerationResult,
    review: TestReviewRequest | TestReviewRecord,
) -> dict[str, ApprovedTestSpec]:
    """Build controlled ApprovedTestSpec registry from exported proposals and human review."""
    if not review.approved:
        return {}
    specs: dict[str, ApprovedTestSpec] = {}
    selected = set(review.selected_test_ids)
    for scenario in generation_result.scenarios:
        if scenario.scenario_id not in selected:
            continue
        if not scenario.exported_relative_path or not scenario.exported_sha256:
            raise ValueError(
                f"Selected scenario '{scenario.scenario_id}' has not been exported with a verified hash"
            )
        specs[scenario.scenario_id] = ApprovedTestSpec(
            scenario_id=scenario.scenario_id,
            relative_path=scenario.exported_relative_path,
            expected_sha256=scenario.exported_sha256,
            approved=True,
        )
    missing = selected - set(specs.keys())
    if missing:
        raise ValueError(
            f"Approved scenario identifiers not found in exported proposals: {sorted(missing)}"
        )
    return specs


def _compute_risk_summary(
    outcome: VerificationOutcome,
    diagnostics: list[FailureDiagnostic],
) -> RiskSummary:
    """Compute RiskSummary via M04 calculate_risk without fabricating missing scores."""
    if outcome == VerificationOutcome.PASSED:
        return RiskSummary(
            severity="Unknown",
            score=None,
            method="unavailable",
            rationale=(
                "Reviewed tests passed against the modified working copy, but "
                "baseline-versus-modified regression evidence and business-impact "
                "risk analysis are unavailable."
            ),
            evidence_available=False,
        )

    if not diagnostics:
        return RiskSummary(
            severity="Unknown",
            score=None,
            method="unavailable",
            rationale=(
                f"Verification ended with '{outcome.value}' without structured "
                "business-impact evidence for quantitative risk scoring."
            ),
            evidence_available=False,
        )

    combined_text = " ".join(
        f"{diag.error_type} {diag.reason} {diag.evidence}" for diag in diagnostics
    )
    risk_raw = calculate_risk({"business_impact": combined_text})
    severity_raw = str(risk_raw.get("severity", "Unknown"))
    if severity_raw not in {"Low", "Medium", "High", "Critical", "Unknown"}:
        severity_raw = "Unknown"
    score = risk_raw.get("score")
    if score is None or severity_raw == "Unknown":
        return RiskSummary(
            severity="Unknown",
            score=None,
            method="unavailable",
            rationale=(
                "M04 keyword-based risk_scorer did not find matching business-impact "
                "indicators; reporting Unknown instead of fabricating a risk score."
            ),
            evidence_available=False,
        )

    return RiskSummary(
        severity=severity_raw,
        score=int(score),
        method=str(risk_raw.get("method", "keyword_based")),
        rationale="Computed via M04 keyword-based risk_scorer from observed failure evidence.",
        evidence_available=True,
    )


class VerificationAdapter:
    """Adapter around M04 reviewed-test execution and failure analysis helpers."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config

    def _not_executed_error(
        self,
        *,
        code: str,
        message: str,
        baseline_ref: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> VerificationResult:
        return VerificationResult(
            execution_status="not_executed",
            verification_outcome=VerificationOutcome.UNAVAILABLE,
            verification_passed=False,
            baseline_comparison=BaselineComparisonEvidence(
                status="unavailable",
                baseline_ref=baseline_ref,
            ),
            error=AgentError(code=code, message=message, details=details or {}),
            assumptions=[
                "Tests were not executed because pre-execution safety or approval validation failed.",
            ],
        )

    def _is_workspace_trusted(
        self,
        workspace_resolved: Path,
        is_trusted_workspace: bool,
    ) -> bool:
        if not is_trusted_workspace:
            return False
        if not self.config.trusted_execution_roots:
            return False
        if self.config.is_trusted_for_execution(workspace_resolved):
            return True
        ws_root = self.config.workspace_root.resolve()
        return workspace_resolved.is_relative_to(ws_root)

    def verify(
        self,
        workspace_path: str | Path,
        approved_test_ids: list[str],
        test_registry: Mapping[str, ApprovedTestSpec],
        *,
        timeout_seconds: int | None = None,
        is_trusted_workspace: bool = False,
        baseline_ref: str | None = None,
    ) -> VerificationResult:
        """Execute explicitly approved, hash-verified tests inside a trusted working copy."""
        ws_raw = Path(workspace_path)
        if not ws_raw.exists() or not ws_raw.is_dir():
            return self._not_executed_error(
                code="WORKSPACE_NOT_FOUND",
                message=f"Workspace directory does not exist: '{workspace_path}'",
                baseline_ref=baseline_ref,
            )
        ws_resolved = ws_raw.resolve(strict=True)

        if not self._is_workspace_trusted(ws_resolved, is_trusted_workspace):
            return self._not_executed_error(
                code="UNTRUSTED_WORKSPACE",
                message=(
                    "Refusing to execute tests in an untrusted workspace. "
                    "Untrusted repository execution is disabled without an OS-level sandbox."
                ),
                baseline_ref=baseline_ref,
            )

        effective_timeout = (
            self.config.verification_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if not isinstance(effective_timeout, int) or effective_timeout < 1 or effective_timeout > 600:
            return self._not_executed_error(
                code="INVALID_TIMEOUT",
                message=f"Verification timeout must be an integer between 1 and 600 seconds; got {effective_timeout!r}",
                baseline_ref=baseline_ref,
            )

        if not approved_test_ids:
            return self._not_executed_error(
                code="NO_APPROVED_TESTS",
                message="At least one approved test identifier is required for verification",
                baseline_ref=baseline_ref,
            )

        validated_ids: list[str] = []
        validated_paths: list[str] = []
        seen_ids: set[str] = set()

        for raw_id in approved_test_ids:
            test_id = raw_id.strip()
            if not _SCENARIO_ID_PATTERN.match(test_id):
                return self._not_executed_error(
                    code="INVALID_TEST_ID",
                    message=(
                        f"Invalid test identifier '{raw_id}': only stable identifiers "
                        "are permitted, not arbitrary paths"
                    ),
                    baseline_ref=baseline_ref,
                )
            if test_id in seen_ids:
                continue
            seen_ids.add(test_id)

            spec = test_registry.get(test_id)
            if spec is None or not spec.approved:
                return self._not_executed_error(
                    code="UNAPPROVED_TEST",
                    message=f"Test identifier '{test_id}' is not registered as approved",
                    baseline_ref=baseline_ref,
                )

            try:
                rel_path = _validate_relative_test_path(spec.relative_path)
            except ValueError as exc:
                return self._not_executed_error(
                    code="UNAUTHORIZED_TEST_PATH",
                    message=str(exc),
                    baseline_ref=baseline_ref,
                )

            candidate = ws_resolved / rel_path
            if candidate.is_symlink() and not candidate.resolve().is_relative_to(ws_resolved):
                return self._not_executed_error(
                    code="UNAUTHORIZED_TEST_PATH",
                    message=f"Test file symlink escapes workspace root: '{rel_path}'",
                    baseline_ref=baseline_ref,
                )
            if not candidate.exists() or not candidate.is_file():
                return self._not_executed_error(
                    code="TEST_FILE_MISSING",
                    message=f"Approved test file does not exist inside workspace: '{rel_path}'",
                    baseline_ref=baseline_ref,
                )
            resolved_test = candidate.resolve(strict=True)
            if (
                not resolved_test.is_relative_to(ws_resolved)
                or not resolved_test.name.startswith("test_")
                or resolved_test.suffix != ".py"
            ):
                return self._not_executed_error(
                    code="UNAUTHORIZED_TEST_PATH",
                    message=f"Approved test path must be a test_*.py file inside workspace: '{rel_path}'",
                    baseline_ref=baseline_ref,
                )

            actual_sha = _sha256_file(resolved_test)
            if not hmac.compare_digest(actual_sha, spec.expected_sha256):
                return self._not_executed_error(
                    code="TEST_CONTENT_MODIFIED",
                    message=(
                        f"Approved test '{test_id}' ({rel_path}) has been modified "
                        "since review approval; refusing to execute"
                    ),
                    baseline_ref=baseline_ref,
                    details={
                        "test_id": test_id,
                        "expected_sha256": spec.expected_sha256,
                        "actual_sha256": actual_sha,
                    },
                )

            validated_ids.append(test_id)
            validated_paths.append(resolved_test.relative_to(ws_resolved).as_posix())

        try:
            raw_result = run_reviewed_tests(
                ws_resolved,
                validated_paths,
                timeout=effective_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return VerificationResult(
                execution_status="error",
                verification_outcome=VerificationOutcome.EXECUTION_ERROR,
                verification_passed=False,
                executed_test_ids=validated_ids,
                executed_test_paths=validated_paths,
                baseline_comparison=BaselineComparisonEvidence(
                    status="unavailable",
                    baseline_ref=baseline_ref,
                ),
                error=AgentError(
                    code="M04_RUNNER_EXCEPTION",
                    message=f"Verification runner raised an exception: {exc}",
                ),
            )

        raw_status = raw_result.get("status")
        if raw_status == "timeout":
            return VerificationResult(
                execution_status="timeout",
                verification_outcome=VerificationOutcome.TIMEOUT,
                verification_passed=False,
                executed_test_ids=validated_ids,
                executed_test_paths=validated_paths,
                exit_code=None,
                baseline_comparison=BaselineComparisonEvidence(
                    status="unavailable",
                    baseline_ref=baseline_ref,
                ),
                raw_result=raw_result,
                error=AgentError(
                    code="VERIFICATION_TIMEOUT",
                    message=f"Verification timed out after {effective_timeout}s",
                    retryable=True,
                ),
                assumptions=[
                    "Timeout is never treated as a passing verification result.",
                ],
            )

        stdout = str(raw_result.get("stdout", ""))
        stderr = str(raw_result.get("stderr", ""))
        exit_code = raw_result.get("exit_code")
        collection_errors = _extract_collection_errors(stdout, stderr)

        if raw_status == "error":
            outcome = (
                VerificationOutcome.COLLECTION_ERROR
                if exit_code == 2 or collection_errors
                else VerificationOutcome.EXECUTION_ERROR
            )
            diag_evidence = _truncate(stderr or stdout, 2000)
            diagnostics: list[FailureDiagnostic] = []
            if diag_evidence:
                analysis = analyze_failure(diag_evidence)
                diagnostics.append(
                    FailureDiagnostic(
                        test_name="pytest_runner",
                        status="error",
                        evidence=diag_evidence,
                        error_type=str(analysis.get("error_type", "Unknown")),
                        reason=str(analysis.get("reason", "Further analysis required")),
                        risk=str(analysis.get("risk", "unknown")),
                        recommendation=str(
                            analysis.get("recommendation", "Review failure evidence")
                        ),
                    )
                )
            return VerificationResult(
                execution_status="error",
                verification_outcome=outcome,
                verification_passed=False,
                executed_test_ids=validated_ids,
                executed_test_paths=validated_paths,
                exit_code=exit_code,
                collection_errors=collection_errors,
                failures=diagnostics,
                risk_summary=_compute_risk_summary(outcome, diagnostics),
                baseline_comparison=BaselineComparisonEvidence(
                    status="unavailable",
                    baseline_ref=baseline_ref,
                ),
                stdout_excerpt=_truncate(stdout),
                stderr_excerpt=_truncate(stderr),
                raw_result=raw_result,
                error=AgentError(
                    code="VERIFICATION_RUNNER_ERROR",
                    message=(
                        f"Verification runner failed without producing a JSON report "
                        f"(exit_code={exit_code})"
                    ),
                ),
            )

        total_tests = int(raw_result.get("total_tests", 0) or 0)
        passed = int(raw_result.get("passed", 0) or 0)
        failed = int(raw_result.get("failed", 0) or 0)
        duration = raw_result.get("duration")
        duration_seconds = float(duration) if isinstance(duration, (int, float)) else None

        diagnostics = []
        for raw_failure in raw_result.get("failures", []):
            evidence = str(raw_failure.get("evidence", ""))
            analysis = analyze_failure(evidence)
            diagnostics.append(
                FailureDiagnostic(
                    test_name=str(raw_failure.get("test_name", "unknown_test")),
                    status=str(raw_failure.get("status", "failed")),
                    evidence=_truncate(evidence, 4000),
                    error_type=str(analysis.get("error_type", "Unknown")),
                    reason=str(analysis.get("reason", "Further analysis required")),
                    risk=str(analysis.get("risk", "unknown")),
                    recommendation=str(
                        analysis.get("recommendation", "Review failure evidence")
                    ),
                )
            )

        # Explicitly inspect exit_code: report_parser may omit collection/setup errors.
        if exit_code == 2 or collection_errors:
            outcome = VerificationOutcome.COLLECTION_ERROR
            verification_passed = False
            if not diagnostics:
                col_evidence = "\n".join(collection_errors) or _truncate(
                    stdout or stderr, 2000
                )
                analysis = analyze_failure(col_evidence)
                diagnostics.append(
                    FailureDiagnostic(
                        test_name="pytest_collection",
                        status="collection_error",
                        evidence=col_evidence,
                        error_type=str(analysis.get("error_type", "Unknown")),
                        reason=str(analysis.get("reason", "Pytest collection failed")),
                        risk=str(analysis.get("risk", "unknown")),
                        recommendation=str(
                            analysis.get("recommendation", "Inspect test syntax and imports")
                        ),
                    )
                )
        elif exit_code == 5 or (exit_code == 0 and (total_tests <= 0 or passed <= 0)):
            outcome = VerificationOutcome.NO_TESTS_COLLECTED
            verification_passed = False
        elif exit_code == 1 or failed > 0:
            outcome = VerificationOutcome.FAILED_TESTS
            verification_passed = False
            if not diagnostics:
                fallback_evidence = _truncate(stdout or stderr, 2000)
                analysis = analyze_failure(fallback_evidence)
                diagnostics.append(
                    FailureDiagnostic(
                        test_name="pytest_execution",
                        status="failed",
                        evidence=fallback_evidence,
                        error_type=str(analysis.get("error_type", "Unknown")),
                        reason=str(analysis.get("reason", "Pytest exited with failure")),
                        risk=str(analysis.get("risk", "unknown")),
                        recommendation=str(
                            analysis.get("recommendation", "Review pytest output")
                        ),
                    )
                )
        elif exit_code != 0 or exit_code is None:
            outcome = VerificationOutcome.EXECUTION_ERROR
            verification_passed = False
        else:
            outcome = VerificationOutcome.PASSED
            verification_passed = True


        coverage_percent = None
        coverage_status = "unavailable"

        coverage_data = raw_result.get("coverage")

        if (
            outcome in (
                VerificationOutcome.PASSED,
                VerificationOutcome.FAILED_TESTS,
            )
            and exit_code in (0, 1)
            and isinstance(coverage_data, dict)
            and coverage_data.get("status") == "success"
        ):
            percentage = coverage_data.get("coverage_percentage")

            if (
                type(percentage) in (int, float)
                and 0 <= percentage <= 100
            ):
                coverage_percent = float(percentage)
                coverage_status = "measured"


        error_obj: AgentError | None = None
        if outcome == VerificationOutcome.COLLECTION_ERROR:
            error_obj = AgentError(
                code="PYTEST_COLLECTION_ERROR",
                message=f"Pytest failed during test collection (exit_code={exit_code})",
            )
        elif outcome == VerificationOutcome.NO_TESTS_COLLECTED:
            error_obj = AgentError(
                code="NO_TESTS_COLLECTED",
                message="Zero tests were collected or executed; cannot mark verification as passed",
            )
        elif outcome == VerificationOutcome.EXECUTION_ERROR:
            error_obj = AgentError(
                code="PYTEST_EXECUTION_ERROR",
                message=f"Pytest exited with unexpected error code {exit_code}",
            )

        return VerificationResult(
            execution_status="completed",
            verification_outcome=outcome,
            verification_passed=verification_passed,
            executed_test_ids=validated_ids,
            executed_test_paths=validated_paths,
            exit_code=exit_code,
            total_tests=total_tests,
            passed=passed,
            failed=failed,
            duration_seconds=duration_seconds,
            collection_errors=collection_errors,
            failures=diagnostics,
            risk_summary=_compute_risk_summary(outcome, diagnostics),
            coverage_percent=coverage_percent,
            coverage_status=coverage_status,
            ai_root_cause_status="unavailable",
            baseline_comparison=BaselineComparisonEvidence(
                status="unavailable",
                baseline_ref=baseline_ref,
                modified_passed=passed,
                modified_failed=failed,
            ),
            stdout_excerpt=_truncate(stdout),
            stderr_excerpt=_truncate(stderr),
            raw_result=raw_result,
            error=error_obj,
            assumptions=[
                "Running generated tests against the modified repository alone is not proof of regression safety.",
                "Code coverage measures executed statements, not software correctness.",
                "AI root-cause analysis is not executed by this adapter.",
                "Baseline comparison is handled separately by the orchestration workflow.",
            ],
        )
