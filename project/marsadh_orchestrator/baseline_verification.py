"""Baseline-versus-modified verification execution and deterministic comparison engine (M02).

Design & safety invariants:
- Reuses ``VerificationAdapter`` (which wraps only M04's ``run_reviewed_tests``,
  ``analyze_failure``, and ``calculate_risk``) without modifying teammate-owned
  implementations under ``project/nafris_verification/``.
- Executes the exact same approved generated test bytes (verified by SHA-256)
  against both the baseline workspace (checked out at the resolved Git base
  commit) and the modified workspace. Never regenerates tests per snapshot.
- Clearly separates approved generated acceptance tests from pre-existing
  repository regression tests (``TestSuiteKind.GENERATED_ACCEPTANCE`` vs
  ``TestSuiteKind.EXISTING_REGRESSION``).
- Handles M04's actual ``report_parser.py`` contract honestly: ``parse_report``
  returns aggregate counts and failing node IDs only, omitting passed/skipped
  test node IDs. Individual passing tests are NEVER inferred from aggregate
  pass counts in multi-test batches.
- Collection errors are never classified as application regressions, and
  fail-to-pass outcomes with unknown test purpose are never automatically
  described as repaired regressions.
"""

from __future__ import annotations

import hashlib
import hmac
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

from project.marsadh_orchestrator.adapters.verification_adapter import (
    VerificationAdapter,
    _compute_risk_summary,
    _truncate,
    _validate_relative_test_path,
)
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import redact_secrets
from project.marsadh_orchestrator.workspace import EXCLUDED_DIR_NAMES
from project.schemas.agents import (
    AgentError,
    ApprovedTestSpec,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    FailureDiagnostic,
    GeneratedTestScenario,
    IndividualTestOutcome,
    ModifiedVerificationResult,
    RegressionComparisonSummary,
    SuiteExecutionSummary,
    TestComparisonClassification,
    TestComparisonResult,
    TestExecutionEvidence,
    TestPurpose,
    TestSuiteKind,
    VerificationComparisonStatus,
    VerificationOutcome,
    VerificationResult,
)
from project.schemas.requirements import RequirementAnalysisResult

HARD_INTEGRITY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TEST_CONTENT_MODIFIED",
        "UNAUTHORIZED_TEST_PATH",
        "TEST_FILE_MISSING",
        "WORKSPACE_NOT_FOUND",
        "BASELINE_WORKSPACE_TAMPERED",
        "SOURCE_SNAPSHOT_MODIFIED",
        "GIT_DIFF_MODIFIED",
    }
)


class BaselineTestMirrorError(Exception):
    """Raised when mirroring approved tests to the baseline workspace fails integrity checks."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "TEST_CONTENT_MODIFIED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_scenario_test_purpose(
    scenario: GeneratedTestScenario,
    requirement_analysis: RequirementAnalysisResult | None,
) -> TestPurpose:
    """Resolve explicit test purpose for a generated scenario without inventing intent.

    Rules:
    1. If the scenario explicitly carries a non-unknown ``test_purpose``, honor it.
    2. Otherwise, if the scenario has explicit ``criterion_ids`` matching A1
       ``AcceptanceCriterion`` entries:
       - If ALL matched criteria have ``preserves_existing_behavior=True`` ->
         ``TestPurpose.EXISTING_BEHAVIOR_PRESERVATION``.
       - If ALL matched criteria have ``preserves_existing_behavior=False`` ->
         ``TestPurpose.NEW_REQUIREMENT_ACCEPTANCE``.
       - If criteria mix both or any ID is unmapped -> ``TestPurpose.UNKNOWN``.
    3. Otherwise -> ``TestPurpose.UNKNOWN`` (never guessed from test names or titles).
    """
    if scenario.test_purpose != TestPurpose.UNKNOWN:
        return scenario.test_purpose

    if not scenario.criterion_ids or requirement_analysis is None:
        return TestPurpose.UNKNOWN

    by_cid = {ac.criterion_id: ac for ac in requirement_analysis.acceptance_criteria}
    matched = [by_cid[cid] for cid in scenario.criterion_ids if cid in by_cid]
    if len(matched) != len(scenario.criterion_ids) or not matched:
        return TestPurpose.UNKNOWN

    flags = {ac.preserves_existing_behavior for ac in matched}
    if flags == {True}:
        return TestPurpose.EXISTING_BEHAVIOR_PRESERVATION
    if flags == {False}:
        return TestPurpose.NEW_REQUIREMENT_ACCEPTANCE
    return TestPurpose.UNKNOWN


def mirror_approved_tests_to_baseline(
    modified_workspace: str | Path,
    baseline_workspace: str | Path,
    approved_specs: Mapping[str, ApprovedTestSpec],
    selected_test_ids: list[str],
) -> dict[str, str]:
    """Copy exact Gate-1-approved test files into the baseline workspace and verify SHA-256 parity.

    Never regenerates tests; verifies identical SHA-256 digests across the approved
    specification, modified workspace file, and baseline workspace file.
    """
    mod_root = Path(modified_workspace).resolve(strict=True)
    base_root = Path(baseline_workspace).resolve(strict=True)
    if mod_root == base_root:
        raise BaselineTestMirrorError(
            "Baseline and modified workspaces must be distinct directories.",
            code="WORKSPACE_ISOLATION_VIOLATION",
        )

    verified_hashes: dict[str, str] = {}
    for test_id in selected_test_ids:
        spec = approved_specs.get(test_id)
        if spec is None or not spec.approved:
            raise BaselineTestMirrorError(
                f"Test identifier '{test_id}' is not registered as approved.",
                code="UNAPPROVED_TEST",
                details={"test_id": test_id},
            )
        try:
            rel_posix = _validate_relative_test_path(spec.relative_path)
        except ValueError as exc:
            raise BaselineTestMirrorError(
                str(exc),
                code="UNAUTHORIZED_TEST_PATH",
                details={"test_id": test_id},
            ) from exc

        mod_file = mod_root / rel_posix
        if mod_file.is_symlink() and not mod_file.resolve().is_relative_to(mod_root):
            raise BaselineTestMirrorError(
                f"Approved test file symlink escapes modified workspace: '{rel_posix}'",
                code="UNAUTHORIZED_TEST_PATH",
                details={"test_id": test_id},
            )
        if not mod_file.exists() or not mod_file.is_file():
            raise BaselineTestMirrorError(
                f"Approved test file missing in modified workspace: '{rel_posix}'",
                code="TEST_FILE_MISSING",
                details={"test_id": test_id},
            )
        mod_resolved = mod_file.resolve(strict=True)
        if not mod_resolved.is_relative_to(mod_root):
            raise BaselineTestMirrorError(
                f"Approved test path escapes modified workspace: '{rel_posix}'",
                code="UNAUTHORIZED_TEST_PATH",
                details={"test_id": test_id},
            )

        mod_bytes = mod_resolved.read_bytes()
        mod_sha = _sha256_bytes(mod_bytes)
        if not hmac.compare_digest(mod_sha, spec.expected_sha256):
            raise BaselineTestMirrorError(
                f"Approved test '{test_id}' ({rel_posix}) in modified workspace no longer matches "
                "its Gate 1 approved SHA-256 digest.",
                code="TEST_CONTENT_MODIFIED",
                details={
                    "test_id": test_id,
                    "expected_sha256": spec.expected_sha256,
                    "actual_sha256": mod_sha,
                },
            )

        # Verify no symlink component exists along the destination path in baseline workspace
        current = base_root
        for part in PurePath(rel_posix).parts:
            current = current / part
            if current.is_symlink():
                raise BaselineTestMirrorError(
                    f"Destination path in baseline workspace traverses a symlink: '{rel_posix}'",
                    code="UNAUTHORIZED_TEST_PATH",
                    details={"test_id": test_id},
                )

        base_file = base_root / rel_posix
        base_file.parent.mkdir(parents=True, exist_ok=True)
        base_parent_resolved = base_file.parent.resolve(strict=True)
        if not base_parent_resolved.is_relative_to(base_root):
            raise BaselineTestMirrorError(
                f"Baseline export directory escapes baseline workspace: '{rel_posix}'",
                code="UNAUTHORIZED_TEST_PATH",
                details={"test_id": test_id},
            )

        base_file.write_bytes(mod_bytes)
        base_resolved = base_file.resolve(strict=True)
        if not base_resolved.is_relative_to(base_root):
            raise BaselineTestMirrorError(
                f"Baseline test file escapes baseline workspace: '{rel_posix}'",
                code="UNAUTHORIZED_TEST_PATH",
                details={"test_id": test_id},
            )

        base_sha = _sha256_file(base_resolved)
        if not (
            hmac.compare_digest(base_sha, spec.expected_sha256)
            and hmac.compare_digest(base_sha, mod_sha)
        ):
            raise BaselineTestMirrorError(
                f"Baseline test file hash mismatch after mirroring '{test_id}'.",
                code="TEST_CONTENT_MODIFIED",
                details={
                    "test_id": test_id,
                    "expected_sha256": spec.expected_sha256,
                    "baseline_sha256": base_sha,
                    "modified_sha256": mod_sha,
                },
            )

        verified_hashes[test_id] = base_sha

    return verified_hashes


def mirror_approved_tests_to_workspace(
    source_workspace: str | Path,
    dest_workspace: str | Path,
    approved_specs: Mapping[str, ApprovedTestSpec],
    selected_test_ids: list[str],
) -> dict[str, str]:
    """Copy exact Gate-1-approved test files between isolated workspaces with SHA-256 parity."""
    return mirror_approved_tests_to_baseline(
        source_workspace,
        dest_workspace,
        approved_specs,
        selected_test_ids,
    )


def discover_existing_regression_tests(
    workspace_repo_path: str | Path,
    source_path: str = ".",
    *,
    timeout_seconds: int = 30,
) -> list[str]:
    """Discover tracked pre-existing ``test_*.py`` files inside a workspace repository.

    Strictly excludes ``.changeguard_generated_tests``, cache/venv directories,
    untracked files, and symlinks escaping the workspace root.
    """
    repo_root = Path(workspace_repo_path).resolve(strict=True)
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return []

    norm_source = source_path.strip().replace("\\", "/").rstrip("/")
    allowed_prefixes: list[str] = ["tests/"]
    if norm_source not in {"", "."}:
        allowed_prefixes.append(f"{norm_source}/tests/")

    excluded = set(EXCLUDED_DIR_NAMES) | {".changeguard_generated_tests"}
    discovered: list[str] = []
    for raw_item in proc.stdout.split(b"\x00"):
        if not raw_item:
            continue
        rel = raw_item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        parts = PurePath(rel).parts
        if any(part in excluded for part in parts):
            continue
        name = parts[-1] if parts else ""
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        if not any(rel.startswith(prefix) for prefix in allowed_prefixes):
            continue

        candidate = repo_root / rel
        if candidate.is_symlink():
            continue
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if not resolved.is_relative_to(repo_root):
            continue
        discovered.append(rel)

    return sorted(set(discovered))


def _outcome_from_single_file_verification(
    ver: VerificationResult,
) -> tuple[IndividualTestOutcome, str | None, list[str]]:
    """Map a single-file ``VerificationResult`` to an honest ``IndividualTestOutcome``.

    Returns ``(outcome, node_id, limitations)``.
    """
    limitations: list[str] = []
    node_id: str | None = None
    if ver.failures:
        first_name = ver.failures[0].test_name
        if first_name not in {
            "pytest_execution",
            "pytest_runner",
            "pytest_collection",
            "unknown_test",
        }:
            node_id = first_name

    if ver.execution_status == "not_executed":
        if ver.error is not None:
            limitations.append(redact_secrets(ver.error.message))
        return IndividualTestOutcome.NOT_EXECUTED, node_id, limitations
    if ver.verification_outcome == VerificationOutcome.TIMEOUT:
        limitations.append("Test execution timed out before completion.")
        return IndividualTestOutcome.TIMEOUT, node_id, limitations
    if ver.verification_outcome == VerificationOutcome.COLLECTION_ERROR:
        limitations.append(
            "Pytest collection error prevented valid test execution; not classified as an application regression."
        )
        return IndividualTestOutcome.COLLECTION_ERROR, node_id, limitations
    if ver.verification_outcome == VerificationOutcome.NO_TESTS_COLLECTED:
        limitations.append("Zero tests were collected from the test file.")
        return IndividualTestOutcome.UNAVAILABLE, node_id, limitations
    if ver.verification_outcome == VerificationOutcome.EXECUTION_ERROR:
        limitations.append("Pytest exited with an execution error.")
        return IndividualTestOutcome.EXECUTION_ERROR, node_id, limitations
    if ver.verification_outcome == VerificationOutcome.UNAVAILABLE:
        limitations.append("Verification evidence is unavailable.")
        return IndividualTestOutcome.UNAVAILABLE, node_id, limitations

    total = ver.total_tests or 0
    passed = ver.passed or 0
    failed = ver.failed or 0

    if ver.verification_outcome == VerificationOutcome.FAILED_TESTS or failed > 0:
        if passed > 0:
            limitations.append(
                f"M04 report_parser omits passing test node IDs in multi-test files "
                f"({passed} passed, {failed} failed); failing node ID(s) captured explicitly."
            )
        return IndividualTestOutcome.FAILED, node_id, limitations

    if ver.verification_passed and ver.exit_code == 0 and total > 0 and passed == total and failed == 0:
        limitations.append(
            "M04 report_parser does not emit individual passed pytest node IDs; "
            f"confirmed via isolated single-file execution ({passed}/{total} passed, exit_code=0)."
        )
        return IndividualTestOutcome.PASSED, node_id, limitations

    if total > 0 and passed == 0 and failed == 0 and ver.exit_code == 0:
        limitations.append("All collected tests were skipped or did not report pass/fail outcomes.")
        return IndividualTestOutcome.SKIPPED, node_id, limitations

    limitations.append(
        "M04 aggregate output did not provide sufficient evidence to determine per-test pass/fail outcome."
    )
    return IndividualTestOutcome.UNKNOWN, node_id, limitations


def build_test_evidence_from_aggregate_result(
    ver: VerificationResult,
    *,
    expected_test_ids: list[str],
    approved_specs: Mapping[str, ApprovedTestSpec],
    test_purposes: Mapping[str, TestPurpose] | None = None,
    criterion_ids_by_test: Mapping[str, list[str]] | None = None,
    suite_kind: TestSuiteKind = TestSuiteKind.GENERATED_ACCEPTANCE,
) -> list[TestExecutionEvidence]:
    """Conservatively derive per-test evidence from an aggregate multi-file M04 result.

    CRITICAL INVARIANT:
    When M04 executes multiple files in a single batch and returns aggregate counts
    plus failing node IDs only, this helper NEVER infers individual passing tests
    from aggregate pass counts when failures, skips, or non-zero exits occurred, or
    when per-test passing identities are unavailable.
    """
    purposes = test_purposes or {}
    crit_map = criterion_ids_by_test or {}
    evidence_list: list[TestExecutionEvidence] = []

    if ver.execution_status == "not_executed":
        for tid in expected_test_ids:
            spec = approved_specs.get(tid)
            evidence_list.append(
                TestExecutionEvidence(
                    test_identity=tid,
                    scenario_id=tid if suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE else None,
                    relative_path=spec.relative_path if spec else None,
                    suite_kind=suite_kind,
                    test_purpose=purposes.get(tid, TestPurpose.UNKNOWN),
                    criterion_ids=list(crit_map.get(tid, [])),
                    test_source_sha256=spec.expected_sha256 if spec else None,
                    outcome=IndividualTestOutcome.NOT_EXECUTED,
                    exit_code=ver.exit_code,
                    evidence_granularity="unavailable",
                    evidence_limitations=["Verification was not executed."],
                )
            )
        return evidence_list

    if ver.verification_outcome == VerificationOutcome.TIMEOUT:
        for tid in expected_test_ids:
            spec = approved_specs.get(tid)
            evidence_list.append(
                TestExecutionEvidence(
                    test_identity=tid,
                    scenario_id=tid if suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE else None,
                    relative_path=spec.relative_path if spec else None,
                    suite_kind=suite_kind,
                    test_purpose=purposes.get(tid, TestPurpose.UNKNOWN),
                    criterion_ids=list(crit_map.get(tid, [])),
                    test_source_sha256=spec.expected_sha256 if spec else None,
                    outcome=IndividualTestOutcome.TIMEOUT,
                    exit_code=None,
                    evidence_granularity="unavailable",
                    evidence_limitations=["Verification timed out before completing."],
                )
            )
        return evidence_list

    if ver.verification_outcome == VerificationOutcome.COLLECTION_ERROR:
        for tid in expected_test_ids:
            spec = approved_specs.get(tid)
            evidence_list.append(
                TestExecutionEvidence(
                    test_identity=tid,
                    scenario_id=tid if suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE else None,
                    relative_path=spec.relative_path if spec else None,
                    suite_kind=suite_kind,
                    test_purpose=purposes.get(tid, TestPurpose.UNKNOWN),
                    criterion_ids=list(crit_map.get(tid, [])),
                    test_source_sha256=spec.expected_sha256 if spec else None,
                    outcome=IndividualTestOutcome.COLLECTION_ERROR,
                    exit_code=ver.exit_code,
                    collection_errors=list(ver.collection_errors),
                    evidence_granularity="aggregate_only",
                    evidence_limitations=[
                        "Collection error prevented test execution in aggregate M04 run."
                    ],
                )
            )
        return evidence_list

    # Multi-file aggregate run: match explicit failing node IDs only; NEVER infer passes by subtraction!
    for tid in expected_test_ids:
        spec = approved_specs.get(tid)
        rel_path = spec.relative_path if spec else None
        matched_failure: FailureDiagnostic | None = None
        if rel_path:
            file_name = PurePath(rel_path).name
            for fail in ver.failures:
                if rel_path in fail.test_name or file_name in fail.test_name:
                    matched_failure = fail
                    break

        if matched_failure is not None:
            evidence_list.append(
                TestExecutionEvidence(
                    test_identity=tid,
                    scenario_id=tid if suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE else None,
                    node_id=matched_failure.test_name,
                    relative_path=rel_path,
                    suite_kind=suite_kind,
                    test_purpose=purposes.get(tid, TestPurpose.UNKNOWN),
                    criterion_ids=list(crit_map.get(tid, [])),
                    test_source_sha256=spec.expected_sha256 if spec else None,
                    outcome=IndividualTestOutcome.FAILED,
                    exit_code=ver.exit_code,
                    failure_message=_truncate(matched_failure.evidence, 2000),
                    evidence_granularity="per_test_node",
                    evidence_limitations=[
                        "Derived from explicit failing node ID in aggregate M04 report."
                    ],
                )
            )
        else:
            # Conservative rule: aggregate-only M04 reports omit passed test node IDs!
            # Even if exit_code == 0 across multiple files, without per-file/per-node evidence
            # we cannot confirm a specific file collected and ran >= 1 tests vs 0 tests.
            evidence_list.append(
                TestExecutionEvidence(
                    test_identity=tid,
                    scenario_id=tid if suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE else None,
                    relative_path=rel_path,
                    suite_kind=suite_kind,
                    test_purpose=purposes.get(tid, TestPurpose.UNKNOWN),
                    criterion_ids=list(crit_map.get(tid, [])),
                    test_source_sha256=spec.expected_sha256 if spec else None,
                    outcome=IndividualTestOutcome.UNKNOWN,
                    exit_code=ver.exit_code,
                    evidence_granularity="aggregate_only",
                    evidence_limitations=[
                        "Aggregate-only M04 report does not return individual passing test node IDs; "
                        "per-test passing outcome is not inferred from aggregate pass counts."
                    ],
                )
            )
    return evidence_list


def _aggregate_verification_results(
    per_scenario_results: list[tuple[str, VerificationResult]],
    *,
    selected_test_ids: list[str],
    approved_specs: Mapping[str, ApprovedTestSpec],
    baseline_ref: str | None = None,
) -> VerificationResult:
    """Combine per-scenario ``VerificationResult`` items into one truthful aggregate result."""
    if not per_scenario_results:
        return VerificationResult(
            execution_status="not_executed",
            verification_outcome=VerificationOutcome.UNAVAILABLE,
            verification_passed=False,
            baseline_comparison=BaselineComparisonEvidence(
                status=VerificationComparisonStatus.UNAVAILABLE,
                baseline_ref=baseline_ref,
            ),
            error=AgentError(
                code="NO_APPROVED_TESTS",
                message="No approved scenarios were executed.",
            ),
        )

    if len(per_scenario_results) == 1:
        return per_scenario_results[0][1]

    executed_ids: list[str] = []
    executed_paths: list[str] = []
    collection_errors: list[str] = []
    failures: list[FailureDiagnostic] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    assumptions: list[str] = []

    total_tests = 0
    total_passed = 0
    total_failed = 0
    total_duration = 0.0
    has_duration = False
    has_counts = False

    any_not_executed = False
    any_timeout = False
    any_exec_error = False
    any_collection_error = False
    any_failed_tests = False
    any_no_tests = False
    first_error: AgentError | None = None
    worst_exit_code: int | None = 0

    for sc_id, res in per_scenario_results:
        for tid in res.executed_test_ids:
            if tid not in executed_ids:
                executed_ids.append(tid)
        for tpath in res.executed_test_paths:
            if tpath not in executed_paths:
                executed_paths.append(tpath)
        collection_errors.extend(res.collection_errors)
        failures.extend(res.failures)
        if res.stdout_excerpt:
            stdout_parts.append(f"[{sc_id}]\n{res.stdout_excerpt}")
        if res.stderr_excerpt:
            stderr_parts.append(f"[{sc_id}]\n{res.stderr_excerpt}")
        for asm in res.assumptions:
            if asm not in assumptions:
                assumptions.append(asm)
        if res.error is not None and first_error is None:
            first_error = res.error

        if res.total_tests is not None:
            total_tests += res.total_tests
            has_counts = True
        if res.passed is not None:
            total_passed += res.passed
            has_counts = True
        if res.failed is not None:
            total_failed += res.failed
            has_counts = True
        if res.duration_seconds is not None:
            total_duration += res.duration_seconds
            has_duration = True

        if res.execution_status == "not_executed":
            any_not_executed = True
        if res.verification_outcome == VerificationOutcome.TIMEOUT:
            any_timeout = True
        elif res.verification_outcome == VerificationOutcome.COLLECTION_ERROR:
            any_collection_error = True
        elif res.verification_outcome == VerificationOutcome.EXECUTION_ERROR:
            any_exec_error = True
        elif res.verification_outcome == VerificationOutcome.FAILED_TESTS:
            any_failed_tests = True
        elif res.verification_outcome == VerificationOutcome.NO_TESTS_COLLECTED:
            any_no_tests = True

        if res.exit_code is None:
            worst_exit_code = None if (any_timeout or any_not_executed) else worst_exit_code
        elif worst_exit_code == 0 and res.exit_code != 0:
            worst_exit_code = res.exit_code

    if any_not_executed and not has_counts and not any_timeout and not any_exec_error:
        execution_status = "not_executed"
        outcome = VerificationOutcome.UNAVAILABLE
        verification_passed = False
        exit_code = None
    elif any_timeout:
        execution_status = "timeout"
        outcome = VerificationOutcome.TIMEOUT
        verification_passed = False
        exit_code = None
    elif any_collection_error:
        execution_status = "completed"
        outcome = VerificationOutcome.COLLECTION_ERROR
        verification_passed = False
        exit_code = 2 if worst_exit_code in {0, None} else worst_exit_code
    elif any_exec_error:
        execution_status = "error"
        outcome = VerificationOutcome.EXECUTION_ERROR
        verification_passed = False
        exit_code = worst_exit_code
    elif any_failed_tests or total_failed > 0:
        execution_status = "completed"
        outcome = VerificationOutcome.FAILED_TESTS
        verification_passed = False
        exit_code = 1 if worst_exit_code in {0, None} else worst_exit_code
    elif any_no_tests or total_tests <= 0:
        execution_status = "completed"
        outcome = VerificationOutcome.NO_TESTS_COLLECTED
        verification_passed = False
        exit_code = 5 if worst_exit_code in {0, None} else worst_exit_code
    else:
        execution_status = "completed"
        outcome = VerificationOutcome.PASSED
        verification_passed = True
        exit_code = 0

    return VerificationResult(
        execution_status=execution_status,
        verification_outcome=outcome,
        verification_passed=verification_passed,
        executed_test_ids=executed_ids or list(selected_test_ids),
        executed_test_paths=executed_paths
        or [approved_specs[s].relative_path for s in selected_test_ids if s in approved_specs],
        exit_code=exit_code,
        total_tests=total_tests if has_counts else None,
        passed=total_passed if has_counts else None,
        failed=total_failed if has_counts else None,
        duration_seconds=round(total_duration, 6) if has_duration else None,
        collection_errors=collection_errors,
        failures=failures,
        risk_summary=_compute_risk_summary(outcome, failures),
        coverage_percent=None,
        coverage_status="unavailable",
        ai_root_cause_status="unavailable",
        baseline_comparison=BaselineComparisonEvidence(
            status=VerificationComparisonStatus.UNAVAILABLE,
            baseline_ref=baseline_ref,
            modified_passed=total_passed if has_counts else None,
            modified_failed=total_failed if has_counts else None,
        ),
        stdout_excerpt=_truncate("\n".join(stdout_parts)),
        stderr_excerpt=_truncate("\n".join(stderr_parts)),
        raw_result={
            "per_scenario_raw": {
                sc_id: res.raw_result for sc_id, res in per_scenario_results
            }
        },
        error=first_error,
        assumptions=assumptions,
    )


def _execute_existing_regression_suite(
    verification_adapter: VerificationAdapter,
    *,
    workspace_path: str | Path,
    source_path: str,
    timeout_seconds: int,
    is_trusted_workspace: bool,
    git_timeout_seconds: int,
    baseline_ref: str | None = None,
) -> SuiteExecutionSummary | None:
    """Discover and run tracked pre-existing repository regression tests separately via M04."""
    discovered = discover_existing_regression_tests(
        workspace_path,
        source_path=source_path,
        timeout_seconds=git_timeout_seconds,
    )
    if not discovered:
        return None

    ws_root = Path(workspace_path).resolve(strict=True)
    file_hashes: dict[str, str] = {}
    specs: dict[str, ApprovedTestSpec] = {}
    id_to_rel: dict[str, str] = {}
    for idx, rel_path in enumerate(discovered, start=1):
        reg_id = f"REG-{idx:03d}"
        file_sha = _sha256_file(ws_root / rel_path)
        file_hashes[rel_path] = file_sha
        specs[reg_id] = ApprovedTestSpec(
            scenario_id=reg_id,
            relative_path=rel_path,
            expected_sha256=file_sha,
            approved=True,
        )
        id_to_rel[reg_id] = rel_path

    if not is_trusted_workspace:
        return SuiteExecutionSummary(
            suite_kind=TestSuiteKind.EXISTING_REGRESSION,
            execution_status="not_executed",
            verification_outcome=VerificationOutcome.UNAVAILABLE,
            discovered_test_paths=discovered,
            test_file_hashes=file_hashes,
            evidence_limitations=[
                "Existing repository regression tests were not executed because workspace is untrusted."
            ],
            error=AgentError(
                code="UNTRUSTED_WORKSPACE",
                message="Refusing to execute existing repository regression tests in an untrusted workspace.",
            ),
        )

    per_file_results: list[tuple[str, VerificationResult]] = []
    test_evidence: list[TestExecutionEvidence] = []
    suite_limitations: list[str] = []

    for reg_id, rel_path in id_to_rel.items():
        res = verification_adapter.verify(
            ws_root,
            [reg_id],
            specs,
            timeout_seconds=timeout_seconds,
            is_trusted_workspace=is_trusted_workspace,
            baseline_ref=baseline_ref,
        )
        per_file_results.append((reg_id, res))
        outcome, node_id, item_lims = _outcome_from_single_file_verification(res)
        test_evidence.append(
            TestExecutionEvidence(
                test_identity=f"existing::{rel_path}",
                scenario_id=None,
                node_id=node_id,
                relative_path=rel_path,
                suite_kind=TestSuiteKind.EXISTING_REGRESSION,
                test_purpose=TestPurpose.EXISTING_REPOSITORY_REGRESSION,
                criterion_ids=[],
                test_source_sha256=file_hashes[rel_path],
                outcome=outcome,
                exit_code=res.exit_code,
                total_tests=res.total_tests,
                passed=res.passed,
                failed=res.failed,
                duration_seconds=res.duration_seconds,
                collection_errors=list(res.collection_errors),
                failure_message=(
                    _truncate(res.failures[0].evidence, 2000) if res.failures else None
                ),
                evidence_granularity=(
                    "single_scenario_file"
                    if outcome
                    in {
                        IndividualTestOutcome.PASSED,
                        IndividualTestOutcome.FAILED,
                        IndividualTestOutcome.COLLECTION_ERROR,
                    }
                    else "unavailable"
                ),
                evidence_limitations=item_lims,
            )
        )

    agg = _aggregate_verification_results(
        per_file_results,
        selected_test_ids=list(id_to_rel.keys()),
        approved_specs=specs,
        baseline_ref=baseline_ref,
    )
    if any(
        ev.outcome == IndividualTestOutcome.FAILED and (ev.passed or 0) > 0
        for ev in test_evidence
    ):
        suite_limitations.append(
            "M04 report_parser omits individual passed node IDs inside existing regression test files with mixed pass/fail outcomes."
        )

    return SuiteExecutionSummary(
        suite_kind=TestSuiteKind.EXISTING_REGRESSION,
        execution_status=agg.execution_status,
        verification_outcome=agg.verification_outcome,
        discovered_test_paths=discovered,
        test_file_hashes=file_hashes,
        exit_code=agg.exit_code,
        total_tests=agg.total_tests,
        passed=agg.passed,
        failed=agg.failed,
        duration_seconds=agg.duration_seconds,
        collection_errors=list(agg.collection_errors),
        failures=list(agg.failures),
        test_evidence=test_evidence,
        evidence_limitations=suite_limitations,
        error=agg.error,
    )


def execute_snapshot_verification(
    verification_adapter: VerificationAdapter,
    config: OrchestratorConfig,
    *,
    snapshot_role: str,
    workspace_path: str | Path,
    source_path: str,
    selected_test_ids: list[str],
    approved_specs: Mapping[str, ApprovedTestSpec],
    test_purposes: Mapping[str, TestPurpose],
    criterion_ids_by_test: Mapping[str, list[str]],
    is_trusted_workspace: bool,
    source_snapshot_sha256: str | None = None,
    git_diff_sha256: str | None = None,
    baseline_ref: str | None = None,
    baseline_commit: str | None = None,
    include_existing_regression_suite: bool = True,
) -> tuple[
    BaselineVerificationResult | ModifiedVerificationResult,
    VerificationResult,
    AgentError | None,
]:
    """Execute identical approved scenarios via M04 ``VerificationAdapter`` on one snapshot.

    Executes each approved scenario file individually through ``VerificationAdapter.verify``
    so every scenario gets truthful per-scenario pass/fail/collection/timeout evidence
    without inferring passing tests from multi-file aggregate pass counts.

    Returns ``(snapshot_result, aggregate_verification_result, hard_integrity_error)``.
    """
    per_scenario_results: list[tuple[str, VerificationResult]] = []
    test_evidence: list[TestExecutionEvidence] = []
    verified_hashes: dict[str, str] = {}
    hard_integrity_error: AgentError | None = None
    snapshot_limitations: list[str] = [
        "M04 report_parser returns aggregate pass/fail counts and failing node IDs only; "
        "passed pytest node IDs are not emitted by M04 and are confirmed per isolated scenario file."
    ]

    ws_root = Path(workspace_path)
    for sc_id in selected_test_ids:
        spec = approved_specs.get(sc_id)
        single_res = verification_adapter.verify(
            workspace_path,
            [sc_id],
            approved_specs,
            timeout_seconds=config.verification_timeout_seconds,
            is_trusted_workspace=is_trusted_workspace,
            baseline_ref=baseline_ref,
        )
        per_scenario_results.append((sc_id, single_res))

        if (
            single_res.error is not None
            and single_res.error.code in HARD_INTEGRITY_ERROR_CODES
        ):
            hard_integrity_error = single_res.error
            # Record evidence item then stop executing further tests after integrity violation
            test_evidence.append(
                TestExecutionEvidence(
                    test_identity=sc_id,
                    scenario_id=sc_id,
                    relative_path=spec.relative_path if spec else None,
                    suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
                    test_purpose=test_purposes.get(sc_id, TestPurpose.UNKNOWN),
                    criterion_ids=list(criterion_ids_by_test.get(sc_id, [])),
                    test_source_sha256=None,
                    outcome=IndividualTestOutcome.NOT_EXECUTED,
                    exit_code=single_res.exit_code,
                    evidence_granularity="unavailable",
                    evidence_limitations=[redact_secrets(single_res.error.message)],
                )
            )
            break

        actual_file_sha: str | None = None
        if spec is not None and ws_root.is_dir():
            candidate = ws_root / spec.relative_path
            if candidate.is_file():
                actual_file_sha = _sha256_file(candidate)
                if not hmac.compare_digest(actual_file_sha, spec.expected_sha256):
                    hard_integrity_error = AgentError(
                        code="TEST_CONTENT_MODIFIED",
                        message=(
                            f"Approved test '{sc_id}' ({spec.relative_path}) changed during "
                            "verification execution; refusing to accept tampered results."
                        ),
                        details={
                            "test_id": sc_id,
                            "expected_sha256": spec.expected_sha256,
                            "actual_sha256": actual_file_sha,
                        },
                    )
                    test_evidence.append(
                        TestExecutionEvidence(
                            test_identity=sc_id,
                            scenario_id=sc_id,
                            relative_path=spec.relative_path,
                            suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
                            test_purpose=test_purposes.get(sc_id, TestPurpose.UNKNOWN),
                            criterion_ids=list(criterion_ids_by_test.get(sc_id, [])),
                            test_source_sha256=actual_file_sha,
                            outcome=IndividualTestOutcome.NOT_EXECUTED,
                            exit_code=single_res.exit_code,
                            evidence_granularity="unavailable",
                            evidence_limitations=[hard_integrity_error.message],
                        )
                    )
                    break
                verified_hashes[sc_id] = actual_file_sha

        outcome, node_id, item_lims = _outcome_from_single_file_verification(single_res)
        granularity = (
            "single_scenario_file"
            if outcome
            in {
                IndividualTestOutcome.PASSED,
                IndividualTestOutcome.FAILED,
                IndividualTestOutcome.COLLECTION_ERROR,
                IndividualTestOutcome.SKIPPED,
            }
            else "unavailable"
        )
        test_evidence.append(
            TestExecutionEvidence(
                test_identity=sc_id,
                scenario_id=sc_id,
                node_id=node_id,
                relative_path=spec.relative_path if spec else None,
                suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
                test_purpose=test_purposes.get(sc_id, TestPurpose.UNKNOWN),
                criterion_ids=list(criterion_ids_by_test.get(sc_id, [])),
                test_source_sha256=actual_file_sha,
                outcome=outcome,
                exit_code=single_res.exit_code,
                total_tests=single_res.total_tests,
                passed=single_res.passed,
                failed=single_res.failed,
                duration_seconds=single_res.duration_seconds,
                collection_errors=list(single_res.collection_errors),
                failure_message=(
                    _truncate(single_res.failures[0].evidence, 2000)
                    if single_res.failures
                    else None
                ),
                evidence_granularity=granularity,
                evidence_limitations=item_lims,
            )
        )

    agg_ver = _aggregate_verification_results(
        per_scenario_results,
        selected_test_ids=selected_test_ids,
        approved_specs=approved_specs,
        baseline_ref=baseline_ref,
    )

    existing_suite: SuiteExecutionSummary | None = None
    if include_existing_regression_suite and hard_integrity_error is None and ws_root.is_dir():
        existing_suite = _execute_existing_regression_suite(
            verification_adapter,
            workspace_path=ws_root,
            source_path=source_path,
            timeout_seconds=config.verification_timeout_seconds,
            is_trusted_workspace=is_trusted_workspace,
            git_timeout_seconds=config.git_timeout_seconds,
            baseline_ref=baseline_ref,
        )

    if snapshot_role == "baseline":
        snapshot_res: BaselineVerificationResult | ModifiedVerificationResult = (
            BaselineVerificationResult(
                execution_status=agg_ver.execution_status,
                verification_outcome=agg_ver.verification_outcome,
                baseline_ref=baseline_ref,
                baseline_commit=baseline_commit,
                source_snapshot_sha256=source_snapshot_sha256,
                approved_test_hashes=verified_hashes,
                executed_test_ids=list(agg_ver.executed_test_ids),
                executed_test_paths=list(agg_ver.executed_test_paths),
                suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
                exit_code=agg_ver.exit_code,
                total_tests=agg_ver.total_tests,
                passed=agg_ver.passed,
                failed=agg_ver.failed,
                duration_seconds=agg_ver.duration_seconds,
                collection_errors=list(agg_ver.collection_errors),
                failures=list(agg_ver.failures),
                test_evidence=test_evidence,
                existing_regression_suite=existing_suite,
                evidence_limitations=snapshot_limitations,
                error=agg_ver.error,
            )
        )
    else:
        snapshot_res = ModifiedVerificationResult(
            execution_status=agg_ver.execution_status,
            verification_outcome=agg_ver.verification_outcome,
            verification_passed=agg_ver.verification_passed,
            source_snapshot_sha256=source_snapshot_sha256,
            git_diff_sha256=git_diff_sha256,
            approved_test_hashes=verified_hashes,
            executed_test_ids=list(agg_ver.executed_test_ids),
            executed_test_paths=list(agg_ver.executed_test_paths),
            suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
            exit_code=agg_ver.exit_code,
            total_tests=agg_ver.total_tests,
            passed=agg_ver.passed,
            failed=agg_ver.failed,
            duration_seconds=agg_ver.duration_seconds,
            collection_errors=list(agg_ver.collection_errors),
            failures=list(agg_ver.failures),
            test_evidence=test_evidence,
            existing_regression_suite=existing_suite,
            evidence_limitations=snapshot_limitations,
            error=agg_ver.error,
        )

    return snapshot_res, agg_ver, hard_integrity_error


class BaselineComparisonEngine:
    """Deterministic baseline-versus-modified verification evidence comparator."""

    @staticmethod
    def _compare_single_item(
        *,
        test_identity: str,
        suite_kind: TestSuiteKind,
        b_ev: TestExecutionEvidence | None,
        m_ev: TestExecutionEvidence | None,
        approved_sha256: str | None,
        require_approved_hash: bool,
    ) -> TestComparisonResult:
        """Compare one stable test identity across baseline and modified evidence."""
        scenario_id = (
            (m_ev.scenario_id if m_ev and m_ev.scenario_id else None)
            or (b_ev.scenario_id if b_ev and b_ev.scenario_id else None)
        )
        node_id = (
            (m_ev.node_id if m_ev and m_ev.node_id else None)
            or (b_ev.node_id if b_ev and b_ev.node_id else None)
        )
        relative_path = (
            (m_ev.relative_path if m_ev and m_ev.relative_path else None)
            or (b_ev.relative_path if b_ev and b_ev.relative_path else None)
        )
        purpose = (
            m_ev.test_purpose
            if m_ev and m_ev.test_purpose != TestPurpose.UNKNOWN
            else (b_ev.test_purpose if b_ev else TestPurpose.UNKNOWN)
        )
        criterion_ids = list(
            m_ev.criterion_ids if m_ev and m_ev.criterion_ids else (b_ev.criterion_ids if b_ev else [])
        )

        b_outcome = b_ev.outcome if b_ev is not None else IndividualTestOutcome.UNAVAILABLE
        m_outcome = m_ev.outcome if m_ev is not None else IndividualTestOutcome.UNAVAILABLE
        b_sha = b_ev.test_source_sha256 if b_ev is not None else None
        m_sha = m_ev.test_source_sha256 if m_ev is not None else None

        limitations: list[str] = []
        if b_ev is not None:
            limitations.extend(b_ev.evidence_limitations)
        if m_ev is not None:
            for lim in m_ev.evidence_limitations:
                if lim not in limitations:
                    limitations.append(lim)

        # 1. Missing side or unavailable execution context
        if b_ev is None or m_ev is None:
            missing_side = "baseline" if b_ev is None else "modified"
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=False if (b_sha or m_sha) else None,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.UNAVAILABLE,
                is_mandatory_regression=False,
                comparable=False,
                rationale=(
                    f"Execution evidence for '{test_identity}' is missing on the "
                    f"{missing_side} snapshot; comparison is unavailable."
                ),
                limitations=[
                    *limitations,
                    f"Missing {missing_side} execution evidence for '{test_identity}'.",
                ],
            )

        # 2. Check SHA-256 test hash parity
        hashes_match: bool | None
        if b_sha is None or m_sha is None:
            hashes_match = False
        elif not hmac.compare_digest(b_sha, m_sha):
            hashes_match = False
        elif require_approved_hash and (
            approved_sha256 is None or not hmac.compare_digest(b_sha, approved_sha256)
        ):
            hashes_match = False
        else:
            hashes_match = True

        # 3. Collection error must NEVER be classified as an application regression
        if (
            b_outcome == IndividualTestOutcome.COLLECTION_ERROR
            or m_outcome == IndividualTestOutcome.COLLECTION_ERROR
            or b_ev.collection_errors
            or m_ev.collection_errors
        ):
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=hashes_match,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.NOT_COMPARABLE,
                is_mandatory_regression=False,
                comparable=False,
                rationale=(
                    f"Collection error observed for '{test_identity}' "
                    f"(baseline={b_outcome.value}, modified={m_outcome.value}); "
                    "collection errors are never classified as application regressions."
                ),
                limitations=[
                    *limitations,
                    "Collection error prevented comparable test execution.",
                ],
            )

        # 4. Unavailable / Timeout / Not Executed / Execution Error on either side
        unavailable_outcomes = {
            IndividualTestOutcome.UNAVAILABLE,
            IndividualTestOutcome.NOT_EXECUTED,
            IndividualTestOutcome.TIMEOUT,
            IndividualTestOutcome.EXECUTION_ERROR,
        }
        if b_outcome in unavailable_outcomes or m_outcome in unavailable_outcomes:
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=hashes_match,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.UNAVAILABLE,
                is_mandatory_regression=False,
                comparable=False,
                rationale=(
                    f"Execution was unavailable or interrupted for '{test_identity}' "
                    f"(baseline={b_outcome.value}, modified={m_outcome.value})."
                ),
                limitations=limitations,
            )

        # 5. Hash mismatch or missing test source hash -> NOT_COMPARABLE
        if hashes_match is not True:
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=False,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.NOT_COMPARABLE,
                is_mandatory_regression=False,
                comparable=False,
                rationale=(
                    f"Test source SHA-256 mismatch or missing hash for '{test_identity}'; "
                    "baseline and modified runs did not execute verified identical test code."
                ),
                limitations=[
                    *limitations,
                    "Test source SHA-256 did not match across approved/baseline/modified snapshots.",
                ],
            )

        # 6. Unknown outcome (e.g., aggregate-only M04 output lacking passed node IDs)
        if (
            b_outcome == IndividualTestOutcome.UNKNOWN
            or m_outcome == IndividualTestOutcome.UNKNOWN
        ):
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=True,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.UNKNOWN,
                is_mandatory_regression=False,
                comparable=False,
                rationale=(
                    f"Per-test outcome for '{test_identity}' is unknown "
                    f"(baseline={b_outcome.value}, modified={m_outcome.value}); "
                    "passing tests are never inferred from aggregate pass counts."
                ),
                limitations=limitations,
            )

        # 7. Skipped on either side -> NOT_COMPARABLE
        if (
            b_outcome == IndividualTestOutcome.SKIPPED
            or m_outcome == IndividualTestOutcome.SKIPPED
        ):
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=True,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.NOT_COMPARABLE,
                is_mandatory_regression=False,
                comparable=False,
                rationale=(
                    f"Test '{test_identity}' was skipped on at least one snapshot "
                    f"(baseline={b_outcome.value}, modified={m_outcome.value}); not comparable."
                ),
                limitations=[
                    *limitations,
                    "Skipped test cannot establish behavioral preservation or regression.",
                ],
            )

        # 8. Deterministic comparable outcomes: PASSED/FAILED x PASSED/FAILED with verified hashes
        if (
            b_outcome == IndividualTestOutcome.PASSED
            and m_outcome == IndividualTestOutcome.PASSED
        ):
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=True,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.PRESERVED_BEHAVIOR,
                is_mandatory_regression=False,
                comparable=True,
                rationale=(
                    f"Identical test '{test_identity}' passed on both baseline and modified snapshots."
                ),
                limitations=limitations,
            )

        if (
            b_outcome == IndividualTestOutcome.PASSED
            and m_outcome == IndividualTestOutcome.FAILED
        ):
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=True,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.NEWLY_INTRODUCED_FAILURE,
                is_mandatory_regression=True,
                comparable=True,
                rationale=(
                    f"Confirmed newly introduced regression: identical test '{test_identity}' "
                    "passed on baseline and failed on the modified snapshot."
                ),
                limitations=limitations,
            )

        if (
            b_outcome == IndividualTestOutcome.FAILED
            and m_outcome == IndividualTestOutcome.FAILED
        ):
            return TestComparisonResult(
                test_identity=test_identity,
                scenario_id=scenario_id,
                node_id=node_id,
                relative_path=relative_path,
                suite_kind=suite_kind,
                test_purpose=purpose,
                criterion_ids=criterion_ids,
                approved_test_sha256=approved_sha256,
                baseline_test_sha256=b_sha,
                modified_test_sha256=m_sha,
                test_hashes_match=True,
                baseline_outcome=b_outcome,
                modified_outcome=m_outcome,
                classification=TestComparisonClassification.PERSISTENT_FAILURE,
                is_mandatory_regression=False,
                comparable=True,
                rationale=(
                    f"Persistent failure: identical test '{test_identity}' failed on both "
                    "baseline and modified snapshots."
                ),
                limitations=limitations,
            )

        # b_outcome == FAILED and m_outcome == PASSED -> distinguish strictly by test_purpose!
        if purpose == TestPurpose.NEW_REQUIREMENT_ACCEPTANCE:
            classification = (
                TestComparisonClassification.NEWLY_SATISFIED_ACCEPTANCE_CRITERION
            )
            rationale = (
                f"Newly satisfied acceptance criterion: '{test_identity}' failed on baseline "
                "and passed on modified (test_purpose='new_requirement_acceptance')."
            )
        elif purpose in {
            TestPurpose.EXISTING_REPOSITORY_REGRESSION,
            TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
        }:
            classification = TestComparisonClassification.FIXED_EXISTING_FAILURE
            rationale = (
                f"Fixed existing failure: '{test_identity}' failed on baseline and passed on "
                f"modified (test_purpose='{purpose.value}')."
            )
        else:
            classification = TestComparisonClassification.FAIL_TO_PASS_UNCLASSIFIED
            rationale = (
                f"Fail-to-pass transition observed for '{test_identity}', but test_purpose is "
                "'unknown'; not automatically classified as a repaired regression or newly "
                "satisfied acceptance criterion."
            )
            limitations = [
                *limitations,
                "Test purpose is unknown; fail-to-pass transition was not automatically labeled as a repaired regression.",
            ]

        return TestComparisonResult(
            test_identity=test_identity,
            scenario_id=scenario_id,
            node_id=node_id,
            relative_path=relative_path,
            suite_kind=suite_kind,
            test_purpose=purpose,
            criterion_ids=criterion_ids,
            approved_test_sha256=approved_sha256,
            baseline_test_sha256=b_sha,
            modified_test_sha256=m_sha,
            test_hashes_match=True,
            baseline_outcome=b_outcome,
            modified_outcome=m_outcome,
            classification=classification,
            is_mandatory_regression=False,
            comparable=True,
            rationale=rationale,
            limitations=limitations,
        )

    @classmethod
    def compare(
        cls,
        *,
        baseline: BaselineVerificationResult | None,
        modified: ModifiedVerificationResult | None,
        approved_test_hashes: Mapping[str, str] | None = None,
        baseline_ref: str | None = None,
        baseline_commit: str | None = None,
        baseline_source_snapshot_sha256: str | None = None,
        modified_source_snapshot_sha256: str | None = None,
    ) -> BaselineComparisonEvidence:
        """Deterministically compare baseline and modified execution evidence."""
        approved_hashes = dict(approved_test_hashes or {})
        effective_ref = (
            baseline_ref
            or (baseline.baseline_ref if baseline else None)
            or baseline_commit
            or (baseline.baseline_commit if baseline else None)
        )
        effective_commit = baseline_commit or (baseline.baseline_commit if baseline else None)
        eff_base_snap = baseline_source_snapshot_sha256 or (
            baseline.source_snapshot_sha256 if baseline else None
        )
        eff_mod_snap = modified_source_snapshot_sha256 or (
            modified.source_snapshot_sha256 if modified else None
        )

        # If neither baseline nor modified executed anything, explicitly UNAVAILABLE
        if baseline is None and modified is None:
            return BaselineComparisonEvidence(
                status=VerificationComparisonStatus.UNAVAILABLE,
                baseline_ref=effective_ref,
                baseline_commit=effective_commit,
                baseline_source_snapshot_sha256=eff_base_snap,
                modified_source_snapshot_sha256=eff_mod_snap,
                approved_test_hashes_verified=False,
                summary=RegressionComparisonSummary(
                    comparison_status=VerificationComparisonStatus.UNAVAILABLE,
                    limitations=["Neither baseline nor modified verification results were provided."],
                ),
                evidence_limitations=[
                    "Baseline-versus-modified verification was not executed."
                ],
                notes=(
                    "Baseline-versus-modified comparison was not executed; running generated tests "
                    "against the modified repository alone is not proof of regression safety."
                ),
            )

        # Index generated test evidence by stable test_identity
        base_gen_by_id: dict[str, TestExecutionEvidence] = {
            ev.test_identity: ev for ev in (baseline.test_evidence if baseline else [])
        }
        mod_gen_by_id: dict[str, TestExecutionEvidence] = {
            ev.test_identity: ev for ev in (modified.test_evidence if modified else [])
        }

        ordered_gen_ids: list[str] = []
        for source_ids in (
            list(approved_hashes.keys()),
            list(base_gen_by_id.keys()),
            list(mod_gen_by_id.keys()),
        ):
            for tid in source_ids:
                if tid not in ordered_gen_ids:
                    ordered_gen_ids.append(tid)

        comparisons: list[TestComparisonResult] = []
        for tid in ordered_gen_ids:
            comparisons.append(
                cls._compare_single_item(
                    test_identity=tid,
                    suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
                    b_ev=base_gen_by_id.get(tid),
                    m_ev=mod_gen_by_id.get(tid),
                    approved_sha256=approved_hashes.get(tid),
                    require_approved_hash=True,
                )
            )

        # Compare separate existing repository regression suite (if discovered on either snapshot)
        base_reg_suite = baseline.existing_regression_suite if baseline else None
        mod_reg_suite = modified.existing_regression_suite if modified else None
        if base_reg_suite is not None or mod_reg_suite is not None:
            base_reg_by_id = {
                ev.test_identity: ev
                for ev in (base_reg_suite.test_evidence if base_reg_suite else [])
            }
            mod_reg_by_id = {
                ev.test_identity: ev
                for ev in (mod_reg_suite.test_evidence if mod_reg_suite else [])
            }
            ordered_reg_ids: list[str] = []
            for tid in (*base_reg_by_id.keys(), *mod_reg_by_id.keys()):
                if tid not in ordered_reg_ids:
                    ordered_reg_ids.append(tid)
            for tid in ordered_reg_ids:
                comparisons.append(
                    cls._compare_single_item(
                        test_identity=tid,
                        suite_kind=TestSuiteKind.EXISTING_REGRESSION,
                        b_ev=base_reg_by_id.get(tid),
                        m_ev=mod_reg_by_id.get(tid),
                        approved_sha256=None,
                        require_approved_hash=False,
                    )
                )

        # Bucket per-test outcomes strictly from deterministic classifications
        preserved: list[str] = []
        newly_introduced: list[str] = []
        newly_satisfied: list[str] = []
        fixed_existing: list[str] = []
        fail_to_pass_unclassified: list[str] = []
        persistent: list[str] = []
        not_comparable: list[str] = []
        unknown_items: list[str] = []
        unavailable_items: list[str] = []

        for item in comparisons:
            label = item.test_identity
            if item.classification == TestComparisonClassification.PRESERVED_BEHAVIOR:
                preserved.append(label)
            elif item.classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE:
                newly_introduced.append(label)
            elif (
                item.classification
                == TestComparisonClassification.NEWLY_SATISFIED_ACCEPTANCE_CRITERION
            ):
                newly_satisfied.append(label)
            elif item.classification == TestComparisonClassification.FIXED_EXISTING_FAILURE:
                fixed_existing.append(label)
            elif (
                item.classification
                == TestComparisonClassification.FAIL_TO_PASS_UNCLASSIFIED
            ):
                fail_to_pass_unclassified.append(label)
            elif item.classification == TestComparisonClassification.PERSISTENT_FAILURE:
                persistent.append(label)
            elif item.classification == TestComparisonClassification.NOT_COMPARABLE:
                not_comparable.append(label)
            elif item.classification == TestComparisonClassification.UNKNOWN:
                unknown_items.append(label)
            else:
                unavailable_items.append(label)

        comparable_count = sum(1 for item in comparisons if item.comparable)
        unavailable_or_incomparable = [
            *not_comparable,
            *unknown_items,
            *unavailable_items,
        ]

        # Verify whether every generated test had matching hashes across approved/baseline/modified
        gen_comparisons = [
            c for c in comparisons if c.suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE
        ]
        approved_hashes_verified = bool(gen_comparisons) and all(
            c.test_hashes_match is True for c in gen_comparisons
        )

        # Separate aggregate suite outcomes (never conflated with per-test comparisons)
        gen_aggregate: dict[str, Any] = {
            "baseline": (
                {
                    "execution_status": baseline.execution_status,
                    "verification_outcome": baseline.verification_outcome.value,
                    "exit_code": baseline.exit_code,
                    "total_tests": baseline.total_tests,
                    "passed": baseline.passed,
                    "failed": baseline.failed,
                    "duration_seconds": baseline.duration_seconds,
                    "collection_error_count": len(baseline.collection_errors),
                }
                if baseline is not None
                else {"execution_status": "unavailable"}
            ),
            "modified": (
                {
                    "execution_status": modified.execution_status,
                    "verification_outcome": modified.verification_outcome.value,
                    "verification_passed": modified.verification_passed,
                    "exit_code": modified.exit_code,
                    "total_tests": modified.total_tests,
                    "passed": modified.passed,
                    "failed": modified.failed,
                    "duration_seconds": modified.duration_seconds,
                    "collection_error_count": len(modified.collection_errors),
                }
                if modified is not None
                else {"execution_status": "unavailable"}
            ),
        }

        existing_reg_aggregate: dict[str, Any] = {}
        if base_reg_suite is not None or mod_reg_suite is not None:
            existing_reg_aggregate = {
                "baseline": (
                    {
                        "execution_status": base_reg_suite.execution_status,
                        "verification_outcome": base_reg_suite.verification_outcome.value,
                        "discovered_count": len(base_reg_suite.discovered_test_paths),
                        "exit_code": base_reg_suite.exit_code,
                        "total_tests": base_reg_suite.total_tests,
                        "passed": base_reg_suite.passed,
                        "failed": base_reg_suite.failed,
                        "duration_seconds": base_reg_suite.duration_seconds,
                    }
                    if base_reg_suite is not None
                    else {"execution_status": "unavailable"}
                ),
                "modified": (
                    {
                        "execution_status": mod_reg_suite.execution_status,
                        "verification_outcome": mod_reg_suite.verification_outcome.value,
                        "discovered_count": len(mod_reg_suite.discovered_test_paths),
                        "exit_code": mod_reg_suite.exit_code,
                        "total_tests": mod_reg_suite.total_tests,
                        "passed": mod_reg_suite.passed,
                        "failed": mod_reg_suite.failed,
                        "duration_seconds": mod_reg_suite.duration_seconds,
                    }
                    if mod_reg_suite is not None
                    else {"execution_status": "unavailable"}
                ),
            }

        # Determine overall comparison status strictly without overclaiming
        limitations: list[str] = []
        if baseline is None or baseline.execution_status in {"unavailable", "not_executed"}:
            limitations.append(
                "Baseline verification was unavailable or not executed; comparison is incomplete."
            )
            if baseline is not None and baseline.error is not None:
                limitations.append(
                    f"Baseline error ({baseline.error.code}): {redact_secrets(baseline.error.message)}"
                )
        elif baseline.execution_status in {"timeout", "error"}:
            limitations.append(
                f"Baseline verification ended with status '{baseline.execution_status}' "
                f"(outcome='{baseline.verification_outcome.value}'); comparison is incomplete."
            )

        if modified is None or modified.execution_status in {"unavailable", "not_executed"}:
            limitations.append(
                "Modified workspace verification was unavailable or not executed; comparison is incomplete."
            )
        elif modified.execution_status in {"timeout", "error"}:
            limitations.append(
                f"Modified verification ended with status '{modified.execution_status}' "
                f"(outcome='{modified.verification_outcome.value}'); comparison is incomplete."
            )

        if not approved_hashes_verified and gen_comparisons:
            limitations.append(
                "Identical approved test SHA-256 verification did not hold for all generated tests."
            )
        if not_comparable:
            limitations.append(
                f"{len(not_comparable)} test(s) were not comparable (collection error, hash mismatch, or skipped): "
                f"{', '.join(not_comparable)}."
            )
        if unknown_items:
            limitations.append(
                f"{len(unknown_items)} test(s) had unknown per-test outcome from aggregate-only M04 output: "
                f"{', '.join(unknown_items)}."
            )
        if unavailable_items:
            limitations.append(
                f"{len(unavailable_items)} test(s) lacked comparable baseline or modified execution evidence: "
                f"{', '.join(unavailable_items)}."
            )
        if fail_to_pass_unclassified:
            limitations.append(
                f"{len(fail_to_pass_unclassified)} fail-to-pass test(s) had unknown test_purpose and "
                f"were NOT automatically labeled as repaired regressions: {', '.join(fail_to_pass_unclassified)}."
            )
        if base_reg_suite is None and mod_reg_suite is None:
            limitations.append(
                "No pre-existing repository regression test suite (tests/test_*.py) was discovered; "
                "baseline comparison reflects approved generated scenarios only."
            )

        both_completed = (
            baseline is not None
            and modified is not None
            and baseline.execution_status == "completed"
            and modified.execution_status == "completed"
        )
        baseline_unavailable_only = (
            baseline is None
            or baseline.execution_status in {"unavailable", "not_executed"}
        ) and (
            modified is None
            or modified.execution_status in {"unavailable", "not_executed"}
        )

        if baseline_unavailable_only:
            comp_status = VerificationComparisonStatus.UNAVAILABLE
        elif (
            both_completed
            and comparisons
            and comparable_count == len(comparisons)
            and not unavailable_or_incomparable
            and approved_hashes_verified
            and not (baseline and baseline.collection_errors)
            and not (modified and modified.collection_errors)
        ):
            comp_status = VerificationComparisonStatus.COMPLETED
        else:
            comp_status = VerificationComparisonStatus.INCOMPLETE

        has_confirmed_regression = len(newly_introduced) > 0
        regression_safety_established = bool(
            comp_status == VerificationComparisonStatus.COMPLETED
            and not has_confirmed_regression
            and comparable_count > 0
            and not unavailable_or_incomparable
        )

        summary = RegressionComparisonSummary(
            comparison_status=comp_status,
            total_compared=len(comparisons),
            comparable_count=comparable_count,
            preserved_behavior_count=len(preserved),
            newly_introduced_failure_count=len(newly_introduced),
            newly_satisfied_acceptance_count=len(newly_satisfied),
            fixed_existing_failure_count=len(fixed_existing),
            fail_to_pass_unclassified_count=len(fail_to_pass_unclassified),
            persistent_failure_count=len(persistent),
            not_comparable_count=len(not_comparable),
            unknown_count=len(unknown_items),
            unavailable_count=len(unavailable_items),
            has_confirmed_regression=has_confirmed_regression,
            blocks_ordinary_approval=has_confirmed_regression,
            regression_safety_established=regression_safety_established,
            newly_introduced_failures=newly_introduced,
            preserved_behaviors=preserved,
            newly_satisfied_acceptance_criteria=newly_satisfied,
            fixed_existing_failures=fixed_existing,
            fail_to_pass_unclassified=fail_to_pass_unclassified,
            persistent_failures=persistent,
            unavailable_or_incomparable=unavailable_or_incomparable,
            generated_suite_aggregate=gen_aggregate,
            existing_regression_suite_aggregate=existing_reg_aggregate,
            limitations=limitations,
        )

        # Truthful backward-compatible newly_failing_tests:
        # - If any confirmed regression was detected -> report them explicitly.
        # - Else if comparison COMPLETED cleanly -> [] (confirmed zero newly failing tests among compared suite).
        # - Else (INCOMPLETE / UNAVAILABLE) -> None (never falsely report [] when comparison is incomplete).
        if newly_introduced:
            newly_failing_compat: list[str] | None = list(newly_introduced)
        elif comp_status == VerificationComparisonStatus.COMPLETED:
            newly_failing_compat = []
        else:
            newly_failing_compat = None

        if comp_status == VerificationComparisonStatus.COMPLETED:
            if has_confirmed_regression:
                notes = (
                    f"Baseline-versus-modified comparison completed across {len(comparisons)} "
                    f"identical test(s): confirmed {len(newly_introduced)} newly introduced "
                    f"regression(s) ({', '.join(newly_introduced)}). Ordinary approval is blocked."
                )
            else:
                notes = (
                    f"Baseline-versus-modified comparison completed across {len(comparisons)} "
                    f"identical test(s): {len(preserved)} preserved behavior(s), "
                    f"{len(newly_satisfied)} newly satisfied acceptance criterion(s), "
                    f"{len(fixed_existing)} fixed existing failure(s), "
                    f"{len(fail_to_pass_unclassified)} unclassified fail-to-pass transition(s), "
                    f"{len(persistent)} persistent failure(s), 0 newly introduced regressions."
                )
        elif comp_status == VerificationComparisonStatus.INCOMPLETE:
            notes = (
                "Baseline-versus-modified comparison is incomplete due to unavailable, timed-out, "
                "uncomparable, or aggregate-only execution evidence; modified results are preserved "
                "without claiming complete regression safety."
            )
        else:
            notes = (
                "Baseline-versus-modified comparison is unavailable; running tests against the "
                "modified repository alone is not proof of regression safety."
            )

        return BaselineComparisonEvidence(
            status=comp_status,
            baseline_ref=effective_ref,
            baseline_commit=effective_commit,
            baseline_source_snapshot_sha256=eff_base_snap,
            modified_source_snapshot_sha256=eff_mod_snap,
            approved_test_hashes_verified=approved_hashes_verified,
            baseline_passed=baseline.passed if baseline else None,
            baseline_failed=baseline.failed if baseline else None,
            modified_passed=modified.passed if modified else None,
            modified_failed=modified.failed if modified else None,
            newly_failing_tests=newly_failing_compat,
            baseline_result=baseline,
            modified_result=modified,
            test_comparisons=comparisons,
            summary=summary,
            evidence_limitations=limitations,
            notes=notes,
        )
