"""Conservative Bounded Patch Repair Eligibility, Progress & Scope Engine (M02 / P0-8).

Design & safety invariants:
- Automated patch repair is ONLY permitted for ``generate_and_verify_change`` runs
  when automatic repair is enabled, an initial patch was applied, mandatory
  ambiguities are resolved, workspace & approved-test integrity hold, reliable
  comparable baseline-vs-modified verification evidence attributes a failure to
  the proposed application change, and repair budget remains.
- Never triggers automated application repair for:
  - generated-test syntax or fixture defects,
  - pytest collection or import errors,
  - missing dependencies,
  - provider authentication or rate-limit errors,
  - verification infrastructure errors (timeouts, runner errors, zero collected tests),
  - unavailable or incomparable baseline evidence (or aggregate pass count < 100% alone),
  - workspace or approved-test integrity violations,
  - unresolved mandatory business ambiguities,
  - pre-existing repository failures unrelated to the proposed change.
- Detects duplicate patch hashes and unchanged failure signatures to stop
  no-progress repair loops deterministically.
- Detects scope drift or invalidated approved-test assumptions via static AST checks.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Any

from project.marsadh_orchestrator.store import redact_secrets
from project.schemas.agents import (
    AgentError,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    CodebaseAnalysisResult,
    FailureDiagnostic,
    IndividualTestOutcome,
    ModifiedVerificationResult,
    TestComparisonClassification,
    TestPurpose,
    TestSuiteKind,
    VerificationComparisonStatus,
    VerificationOutcome,
)
from project.schemas.patch import PatchApplicationResult
from project.schemas.repair import (
    RepairEligibilityCategory,
    RepairEligibilityResult,
    RepairPolicy,
)
from project.schemas.requirements import AmbiguitySeverity, RequirementAnalysisResult
from project.schemas.workflow import WorkflowMode

PROVIDER_AUTH_OR_RATE_LIMIT_CODES: frozenset[str] = frozenset(
    {
        "PROVIDER_AUTH_ERROR",
        "AUTHENTICATION_ERROR",
        "AUTH_ERROR",
        "UNAUTHORIZED",
        "PROVIDER_RATE_LIMIT",
        "RATE_LIMIT",
        "RATE_LIMITED",
        "QUOTA_EXCEEDED",
        "LIVE_LLM_AUTH_ERROR",
        "LIVE_LLM_RATE_LIMIT",
    }
)

_MISSING_DEP_RE = re.compile(
    r"(?:ModuleNotFoundError:\s*No module named\s+['\"]([^'\"]+)['\"]|"
    r"ImportError:\s*cannot import name\s+['\"]([^'\"]+)['\"])",
    re.IGNORECASE,
)
_SYNTAX_ERR_RE = re.compile(
    r"\b(?:SyntaxError|IndentationError|TabError)\b"
)
_FIXTURE_ERR_RE = re.compile(r"fixture\s+['\"][^'\"]+['\"]\s+not found", re.IGNORECASE)
_COLLECTION_ERR_RE = re.compile(
    r"(?:ERROR collecting\b|error(?:s)? during collection\b|ImportError\b|ModuleNotFoundError\b)",
    re.IGNORECASE,
)
_TEST_FILE_TRACE_RE = re.compile(r"\.changeguard_generated_tests[/\\][^\s:]+")
_HEX_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_DURATION_RE = re.compile(r"\b\d+\.\d+s\b")
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]+")
_UNIX_TMP_PATH_RE = re.compile(r"/(?:tmp|var|private|home|Users)/[^\s'\"]+")


def is_provider_auth_or_rate_limit_error(error: AgentError | None) -> bool:
    """Return True if ``error`` indicates provider authentication or rate-limit failure."""
    if error is None:
        return False
    code_upper = error.code.upper()
    if code_upper in PROVIDER_AUTH_OR_RATE_LIMIT_CODES:
        return True
    if any(
        token in code_upper
        for token in ("AUTH", "RATE_LIMIT", "RATELIMIT", "QUOTA", "429", "401")
    ):
        return True
    msg_lower = error.message.lower()
    return any(
        phrase in msg_lower
        for phrase in (
            "rate limit",
            "rate_limit",
            "too many requests",
            "http 429",
            "status 429",
            "authentication failed",
            "invalid api key",
            "unauthorized",
            "http 401",
            "status 401",
        )
    )


def _normalize_failure_text(text: str) -> str:
    """Normalize failure text by stripping transient paths, timings, and object addresses."""
    cleaned = redact_secrets(text or "")
    cleaned = _WIN_PATH_RE.sub("<PATH>", cleaned)
    cleaned = _UNIX_TMP_PATH_RE.sub("<PATH>", cleaned)
    cleaned = _HEX_ADDR_RE.sub("0xADDR", cleaned)
    cleaned = _DURATION_RE.sub("<DUR>", cleaned)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return "\n".join(lines[:20])


def compute_failure_signature(
    *,
    modified: ModifiedVerificationResult | None,
    comparison: BaselineComparisonEvidence | None,
) -> str | None:
    """Compute a deterministic SHA-256 signature of observed verification failures.

    Returns ``None`` when there are no recorded failures (e.g., passing run).
    """
    parts: list[str] = []
    if comparison is not None:
        for tc in sorted(comparison.test_comparisons, key=lambda item: item.test_identity):
            if tc.classification in {
                TestComparisonClassification.NEWLY_INTRODUCED_FAILURE,
                TestComparisonClassification.PERSISTENT_FAILURE,
                TestComparisonClassification.NOT_COMPARABLE,
                TestComparisonClassification.UNAVAILABLE,
                TestComparisonClassification.UNKNOWN,
            } or tc.modified_outcome in {
                IndividualTestOutcome.FAILED,
                IndividualTestOutcome.COLLECTION_ERROR,
                IndividualTestOutcome.TIMEOUT,
                IndividualTestOutcome.EXECUTION_ERROR,
            }:
                parts.append(
                    f"cmp|{tc.test_identity}|{tc.suite_kind.value}|"
                    f"{tc.baseline_outcome.value}->{tc.modified_outcome.value}|"
                    f"{tc.classification.value}"
                )

    if modified is not None:
        for diag in sorted(
            modified.failures,
            key=lambda d: (d.test_name, d.error_type, d.status),
        ):
            parts.append(
                f"diag|{diag.test_name}|{diag.status}|{diag.error_type}|"
                f"{_normalize_failure_text(diag.evidence)}"
            )
        for col_err in sorted(modified.collection_errors):
            parts.append(f"col|{_normalize_failure_text(col_err)}")
        if modified.existing_regression_suite is not None:
            for diag in sorted(
                modified.existing_regression_suite.failures,
                key=lambda d: (d.test_name, d.error_type, d.status),
            ):
                parts.append(
                    f"reg_diag|{diag.test_name}|{diag.status}|{diag.error_type}|"
                    f"{_normalize_failure_text(diag.evidence)}"
                )

    if not parts:
        return None
    canonical = "\n".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _detect_non_application_failure(
    *,
    modified: ModifiedVerificationResult | None,
    baseline: BaselineVerificationResult | None,
    comparison: BaselineComparisonEvidence | None,
    provider_error: AgentError | None = None,
    known_repo_modules: set[str] | None = None,
) -> tuple[RepairEligibilityCategory, str] | None:
    """Detect non-application failures that must NEVER trigger application patch repair."""
    if is_provider_auth_or_rate_limit_error(provider_error):
        return (
            RepairEligibilityCategory.PROVIDER_AUTH_OR_RATE_LIMIT,
            f"Provider authentication or rate-limit error ({provider_error.code if provider_error else 'unknown'}) "
            "must not trigger automated application repair.",
        )

    if modified is None:
        return (
            RepairEligibilityCategory.UNAVAILABLE_OR_INCOMPARABLE_EVIDENCE,
            "Modified verification evidence is unavailable; cannot attribute failure to proposed patch.",
        )

    # 1. Collection / import / syntax errors during pytest collection
    has_collection_error = (
        modified.verification_outcome == VerificationOutcome.COLLECTION_ERROR
        or bool(modified.collection_errors)
        or (
            modified.existing_regression_suite is not None
            and (
                modified.existing_regression_suite.verification_outcome
                == VerificationOutcome.COLLECTION_ERROR
                or bool(modified.existing_regression_suite.collection_errors)
            )
        )
        or (
            baseline is not None
            and (
                baseline.verification_outcome == VerificationOutcome.COLLECTION_ERROR
                or bool(baseline.collection_errors)
            )
        )
        or (
            comparison is not None
            and any(
                tc.baseline_outcome == IndividualTestOutcome.COLLECTION_ERROR
                or tc.modified_outcome == IndividualTestOutcome.COLLECTION_ERROR
                for tc in comparison.test_comparisons
            )
        )
    )
    if has_collection_error:
        combined_col = "\n".join(
            [
                *modified.collection_errors,
                *(baseline.collection_errors if baseline else []),
                *(d.evidence for d in modified.failures),
            ]
        )
        if _SYNTAX_ERR_RE.search(combined_col):
            return (
                RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR,
                "Syntax or indentation error prevented test collection; refusing automated application patch repair.",
            )
        dep_match = _MISSING_DEP_RE.search(combined_col)
        if dep_match:
            missing_mod = (dep_match.group(1) or dep_match.group(2) or "").split(".")[0]
            if known_repo_modules and missing_mod and missing_mod not in known_repo_modules:
                return (
                    RepairEligibilityCategory.MISSING_DEPENDENCY,
                    f"Missing external dependency '{missing_mod}' prevented test collection; "
                    "refusing automated application patch repair.",
                )
        return (
            RepairEligibilityCategory.COLLECTION_OR_IMPORT_ERROR,
            "Pytest collection or import error prevented valid test execution; "
            "refusing automated application patch repair.",
        )

    # 2. Verification infrastructure errors (timeout, runner error, zero tests, not executed)
    if (
        modified.execution_status != "completed"
        or modified.verification_outcome
        in {
            VerificationOutcome.TIMEOUT,
            VerificationOutcome.EXECUTION_ERROR,
            VerificationOutcome.NO_TESTS_COLLECTED,
            VerificationOutcome.UNAVAILABLE,
        }
        or modified.error is not None
    ):
        return (
            RepairEligibilityCategory.VERIFICATION_INFRASTRUCTURE_ERROR,
            f"Verification infrastructure did not complete cleanly "
            f"(execution_status='{modified.execution_status}', "
            f"outcome='{modified.verification_outcome.value}'); "
            "refusing automated application patch repair.",
        )

    if baseline is None or baseline.execution_status != "completed":
        return (
            RepairEligibilityCategory.UNAVAILABLE_OR_INCOMPARABLE_EVIDENCE,
            "Baseline verification did not complete cleanly; incomplete baseline evidence "
            "prevents reliable failure attribution for automated patch repair.",
        )

    # 3. Inspect generated-test failure diagnostics for test-authoring defects (syntax, fixture, missing dep, test NameError)
    for diag in modified.failures:
        evidence = diag.evidence or ""
        if _FIXTURE_ERR_RE.search(evidence):
            return (
                RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR,
                f"Generated test '{diag.test_name}' failed due to a missing pytest fixture; "
                "refusing automated application patch repair.",
            )
        if _SYNTAX_ERR_RE.search(evidence) or diag.error_type in {
            "SyntaxError",
            "IndentationError",
            "TabError",
        }:
            return (
                RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR,
                f"Test failure '{diag.test_name}' contains a syntax error; "
                "refusing automated application patch repair.",
            )
        if _COLLECTION_ERR_RE.search(evidence) or diag.status == "collection_error":
            dep_match = _MISSING_DEP_RE.search(evidence)
            if dep_match:
                missing_mod = (dep_match.group(1) or dep_match.group(2) or "").split(".")[0]
                if known_repo_modules and missing_mod and missing_mod not in known_repo_modules:
                    return (
                        RepairEligibilityCategory.MISSING_DEPENDENCY,
                        f"Missing external dependency '{missing_mod}' encountered in '{diag.test_name}'; "
                        "refusing automated application patch repair.",
                    )
            return (
                RepairEligibilityCategory.COLLECTION_OR_IMPORT_ERROR,
                f"Import or collection error encountered in '{diag.test_name}'; "
                "refusing automated application patch repair.",
            )
        # If a generated test itself fails with NameError (e.g. undefined variable in test body)
        # on BOTH baseline and modified, it is a generated-test defect rather than a patch bug.
        if (
            "NameError:" in evidence
            and _TEST_FILE_TRACE_RE.search(evidence)
            and baseline is not None
            and any(
                b_diag.test_name == diag.test_name and "NameError:" in (b_diag.evidence or "")
                for b_diag in baseline.failures
            )
        ):
            return (
                RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR,
                f"Generated test '{diag.test_name}' raised an identical NameError on both baseline "
                "and modified snapshots (generated-test defect); refusing automated application patch repair.",
            )

    # 4. Baseline-vs-modified comparison completeness & comparability
    if (
        comparison is None
        or comparison.status != VerificationComparisonStatus.COMPLETED
        or not comparison.approved_test_hashes_verified
        or comparison.summary.not_comparable_count > 0
        or comparison.summary.unknown_count > 0
        or comparison.summary.unavailable_count > 0
    ):
        status_label = comparison.status.value if comparison is not None else "missing"
        return (
            RepairEligibilityCategory.UNAVAILABLE_OR_INCOMPARABLE_EVIDENCE,
            f"Baseline-versus-modified comparison evidence is incomplete or incomparable "
            f"(status='{status_label}'); refusing unjustified automated repair.",
        )

    return None


_TEST_ASSUMPTION_ERROR_RE = re.compile(
    r"\b(?:TypeError|AttributeError|NameError|UnboundLocalError):[^\n]*",
)


def _matching_failure_evidence(
    test_identity: str,
    relative_path: str | None,
    snapshot: BaselineVerificationResult | ModifiedVerificationResult | None,
) -> list[str]:
    """Collect matching failure diagnostic text for a single test identity."""
    if snapshot is None:
        return []
    matches: list[str] = []
    for ev in snapshot.test_evidence:
        if ev.test_identity == test_identity and ev.failure_message:
            matches.append(ev.failure_message)
    file_name = PurePath(relative_path).name if relative_path else None
    for diag in snapshot.failures:
        if (
            diag.test_name == test_identity
            or (relative_path and relative_path in diag.test_name)
            or (file_name and file_name in diag.test_name)
        ):
            if diag.evidence:
                matches.append(diag.evidence)
    return matches


def _is_persistent_test_assumption_defect(
    *,
    test_identity: str,
    relative_path: str | None,
    baseline: BaselineVerificationResult | None,
    modified: ModifiedVerificationResult | None,
) -> tuple[bool, bool, str | None]:
    """Check whether a persistent acceptance failure has sufficient attribution evidence.

    Returns ``(has_diagnostic_evidence, is_unreliable_test_assumption, detail)``:
    - If neither ``modified.failures`` nor ``modified.test_evidence`` contains
      diagnostic evidence for the failing test, ``has_diagnostic_evidence=False``
      (preventing unjustified retries when failure attribution is incomplete).
    - If both baseline and modified fail with an identical non-assertion runtime
      assumption error (``TypeError``, ``AttributeError``, ``NameError``,
      ``UnboundLocalError``) rather than an ``AssertionError``, marks it as an
      unreliable generated-test assumption rather than an unmet requirement.
    """
    mod_texts = _matching_failure_evidence(test_identity, relative_path, modified)
    if not mod_texts:
        return False, False, (
            f"Persistent acceptance failure '{test_identity}' lacks structured failure "
            "diagnostic evidence; refusing unjustified application repair."
        )

    combined_mod = "\n".join(mod_texts)
    if "AssertionError" in combined_mod:
        return True, False, None

    base_texts = _matching_failure_evidence(test_identity, relative_path, baseline)
    combined_base = "\n".join(base_texts)
    mod_match = _TEST_ASSUMPTION_ERROR_RE.search(combined_mod)
    base_match = _TEST_ASSUMPTION_ERROR_RE.search(combined_base)
    if mod_match and base_match:
        mod_err_type = mod_match.group(0).split(":", 1)[0]
        base_err_type = base_match.group(0).split(":", 1)[0]
        if mod_err_type == base_err_type:
            return True, True, (
                f"Persistent acceptance failure '{test_identity}' raised an identical "
                f"{mod_err_type} on both baseline and modified snapshots without an "
                "AssertionError, indicating an unreliable generated-test assumption "
                "(e.g., wrong function signature or symbol name) rather than an unmet requirement."
            )
    return True, False, None


class RepairEligibilityEvaluator:
    """Conservative, deterministic evaluator for bounded automated patch repair."""

    @staticmethod
    def evaluate(
        *,
        workflow_mode: WorkflowMode,
        policy: RepairPolicy,
        patch_application: PatchApplicationResult | None,
        requirement_analysis: RequirementAnalysisResult | None,
        baseline: BaselineVerificationResult | None,
        modified: ModifiedVerificationResult | None,
        comparison: BaselineComparisonEvidence | None,
        current_attempt_number: int,
        repair_attempts_consumed: int,
        workspace_integrity_ok: bool = True,
        approved_tests_valid_and_unchanged: bool = True,
        provider_error: AgentError | None = None,
        known_repo_modules: set[str] | None = None,
    ) -> RepairEligibilityResult:
        """Evaluate all 8 conservative conditions for automated patch repair."""
        effective_budget = policy.effective_max_repair_attempts
        remaining_budget = max(0, effective_budget - repair_attempts_consumed)
        failure_sig = compute_failure_signature(modified=modified, comparison=comparison)

        def _ineligible(
            category: RepairEligibilityCategory,
            reason: str,
            *,
            blocking: list[str] | None = None,
            attributable: list[str] | None = None,
            failed_cids: list[str] | None = None,
            regressions: list[str] | None = None,
        ) -> RepairEligibilityResult:
            return RepairEligibilityResult(
                eligible=False,
                category=category,
                decision_reason=reason,
                blocking_reasons=blocking or [reason],
                attributable_test_identities=attributable or [],
                failed_criterion_ids=failed_cids or [],
                confirmed_regression_identities=regressions or [],
                failure_signature=failure_sig,
                next_attempt_number=None,
                remaining_repair_budget=remaining_budget,
            )

        # Condition 1: Workflow mode must be generate_and_verify_change
        if workflow_mode != WorkflowMode.GENERATE_AND_VERIFY_CHANGE:
            return _ineligible(
                RepairEligibilityCategory.WORKFLOW_MODE_UNSUPPORTED,
                f"Automated patch repair is not enabled for workflow_mode='{workflow_mode.value}'.",
            )

        # Condition 2: Automatic repair must be enabled
        if not policy.enabled or effective_budget <= 0:
            return _ineligible(
                RepairEligibilityCategory.REPAIR_DISABLED,
                "Automated patch repair is disabled by repair policy.",
            )

        # Condition 3: A patch must have been successfully applied
        if patch_application is None or not patch_application.applied:
            return _ineligible(
                RepairEligibilityCategory.PATCH_NOT_APPLIED,
                "No applied patch is present; automated repair requires an applied candidate patch.",
            )

        # Condition 4: Mandatory requirement ambiguities must be resolved
        blocking_ambiguity_ids: list[str] = []
        if requirement_analysis is not None:
            blocking_ambiguity_ids = [
                amb.ambiguity_id
                for amb in requirement_analysis.ambiguities
                if (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
                and not amb.resolved
            ]
        if (
            requirement_analysis is not None
            and requirement_analysis.has_mandatory_unresolved_ambiguities
        ) or blocking_ambiguity_ids:
            return _ineligible(
                RepairEligibilityCategory.UNRESOLVED_MANDATORY_AMBIGUITY,
                "Mandatory requirement ambiguities remain unresolved "
                f"({', '.join(blocking_ambiguity_ids) or 'unspecified'}); automated repair is blocked.",
            )

        # Condition 6 & 7: Approved test suite validity & workspace integrity
        if not workspace_integrity_ok or not approved_tests_valid_and_unchanged:
            return _ineligible(
                RepairEligibilityCategory.WORKSPACE_OR_TEST_INTEGRITY_VIOLATION,
                "Workspace integrity or approved test hash validation failed; automated repair is blocked.",
            )

        # Short-circuit if verification passed cleanly with zero regressions
        if (
            modified is not None
            and modified.verification_passed
            and comparison is not None
            and comparison.status == VerificationComparisonStatus.COMPLETED
            and not comparison.summary.has_confirmed_regression
            and (
                modified.existing_regression_suite is None
                or modified.existing_regression_suite.verification_outcome
                == VerificationOutcome.PASSED
            )
        ):
            return RepairEligibilityResult(
                eligible=False,
                category=RepairEligibilityCategory.NOT_NEEDED_VERIFICATION_PASSED,
                decision_reason=(
                    "Verification passed with zero confirmed regressions; no automated repair needed."
                ),
                blocking_reasons=[],
                failure_signature=None,
                next_attempt_number=None,
                remaining_repair_budget=remaining_budget,
            )

        # Condition 5a: Exclude non-application failures (collection, syntax, test defect, infra, incomplete baseline)
        non_app = _detect_non_application_failure(
            modified=modified,
            baseline=baseline,
            comparison=comparison,
            provider_error=provider_error,
            known_repo_modules=known_repo_modules,
        )
        if non_app is not None:
            cat, reason = non_app
            return _ineligible(cat, reason)

        # Condition 5b: Reliable attribution to the proposed application change
        assert comparison is not None
        confirmed_regressions: list[str] = []
        acceptance_failures: list[str] = []
        unreliable_test_assumptions: list[str] = []
        unattributed_acceptance_failures: list[str] = []
        preexisting_failures: list[str] = []
        failed_cids: list[str] = []

        for tc in comparison.test_comparisons:
            if not tc.comparable:
                continue
            if tc.classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE:
                confirmed_regressions.append(tc.test_identity)
                for cid in tc.criterion_ids:
                    if cid not in failed_cids:
                        failed_cids.append(cid)
            elif tc.classification == TestComparisonClassification.PERSISTENT_FAILURE:
                if (
                    tc.suite_kind == TestSuiteKind.GENERATED_ACCEPTANCE
                    and tc.test_purpose == TestPurpose.NEW_REQUIREMENT_ACCEPTANCE
                ):
                    has_diag, is_test_assumption, detail = (
                        _is_persistent_test_assumption_defect(
                            test_identity=tc.test_identity,
                            relative_path=tc.relative_path,
                            baseline=baseline,
                            modified=modified,
                        )
                    )
                    if is_test_assumption:
                        unreliable_test_assumptions.append(
                            detail or tc.test_identity
                        )
                        continue
                    if not has_diag or not tc.criterion_ids:
                        unattributed_acceptance_failures.append(
                            detail
                            or (
                                f"Persistent acceptance failure '{tc.test_identity}' lacks "
                                "explicit criterion mapping or failure diagnostics."
                            )
                        )
                        continue
                    acceptance_failures.append(tc.test_identity)
                    for cid in tc.criterion_ids:
                        if cid not in failed_cids:
                            failed_cids.append(cid)
                else:
                    preexisting_failures.append(tc.test_identity)

        if unreliable_test_assumptions and not confirmed_regressions:
            return _ineligible(
                RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR,
                " ".join(unreliable_test_assumptions),
                blocking=unreliable_test_assumptions,
            )

        attributable = [*confirmed_regressions, *acceptance_failures]
        if not attributable:
            if unattributed_acceptance_failures:
                return _ineligible(
                    RepairEligibilityCategory.UNATTRIBUTABLE_FAILURE,
                    " ".join(unattributed_acceptance_failures),
                    blocking=unattributed_acceptance_failures,
                )
            if preexisting_failures:
                return _ineligible(
                    RepairEligibilityCategory.PERSISTENT_PREEXISTING_FAILURE,
                    "Only pre-existing or unclassified persistent failure(s) were observed "
                    f"({', '.join(preexisting_failures)}); failure cannot be reliably attributed "
                    "to the proposed patch.",
                )
            return _ineligible(
                RepairEligibilityCategory.UNATTRIBUTABLE_FAILURE,
                "Verification failure could not be reliably attributed to the proposed application patch; "
                "stopping automated repair for human review.",
            )

        # Condition 8: Configured repair budget must not be exhausted
        next_attempt = current_attempt_number + 1
        if remaining_budget <= 0 or next_attempt > policy.max_total_proposals:
            return _ineligible(
                RepairEligibilityCategory.BUDGET_EXHAUSTED,
                f"Configured repair budget exhausted ({repair_attempts_consumed}/{effective_budget} "
                f"repair attempts consumed; max total proposals={policy.max_total_proposals}).",
                attributable=attributable,
                failed_cids=failed_cids,
                regressions=confirmed_regressions,
            )

        if confirmed_regressions:
            category = RepairEligibilityCategory.ELIGIBLE_APPLICATION_REGRESSION
            reason = (
                f"Eligible for bounded repair attempt #{repair_attempts_consumed + 1} "
                f"(proposal {next_attempt}/{policy.max_total_proposals}): confirmed newly "
                f"introduced regression(s) attributed to applied patch ({', '.join(confirmed_regressions)})."
            )
        else:
            category = RepairEligibilityCategory.ELIGIBLE_ACCEPTANCE_FAILURE
            reason = (
                f"Eligible for bounded repair attempt #{repair_attempts_consumed + 1} "
                f"(proposal {next_attempt}/{policy.max_total_proposals}): new-requirement "
                f"acceptance test(s) still failing after patch ({', '.join(acceptance_failures)})."
            )

        return RepairEligibilityResult(
            eligible=True,
            category=category,
            decision_reason=reason,
            blocking_reasons=[],
            attributable_test_identities=attributable,
            failed_criterion_ids=failed_cids,
            confirmed_regression_identities=confirmed_regressions,
            failure_signature=failure_sig,
            next_attempt_number=next_attempt,
            remaining_repair_budget=remaining_budget,
        )


def check_approved_test_assumptions_preserved(
    *,
    workspace_repo_path: str | Path,
    source_path: str,
    initial_codebase_analysis: CodebaseAnalysisResult | None,
    new_codebase_analysis: CodebaseAnalysisResult,
    approved_test_codes: Mapping[str, str],
) -> tuple[bool, list[str]]:
    """Deterministically check whether a replacement patch preserves scope & approved test assumptions.

    Stops automatic repair and requires fresh human test review when:
    1. The replacement patch modifies source files outside the initial Gate-1-analyzed
       scope (``initial_changed_files | initial_impacted_modules``).
    2. An approved generated test explicitly imports a top-level function or class
       from a modified module that no longer exists after the replacement patch.
    """
    reasons: list[str] = []
    if initial_codebase_analysis is not None:
        initial_scope = set(initial_codebase_analysis.changed_files_repo_relative) | set(
            initial_codebase_analysis.impacted_modules_repo_relative
        )
        new_changed = set(new_codebase_analysis.changed_files_repo_relative)
        out_of_scope = sorted(new_changed - initial_scope)
        if out_of_scope:
            reasons.append(
                "Replacement patch expanded modification scope beyond Gate-1-reviewed files/impacted modules: "
                + ", ".join(out_of_scope)
            )

    repo_root = Path(workspace_repo_path).resolve()
    source_root = repo_root if source_path in {"", "."} else (repo_root / source_path).resolve()

    # Map modified module names to their defined top-level symbols via AST
    module_symbols: dict[str, set[str]] = {}
    for rel_src in new_codebase_analysis.changed_files_source_relative:
        mod_name = PurePath(rel_src).with_suffix("").as_posix().replace("/", ".")
        mod_file = source_root / rel_src
        if not mod_file.is_file():
            continue
        try:
            tree = ast.parse(mod_file.read_text(encoding="utf-8"), filename=rel_src)
        except (SyntaxError, OSError, UnicodeDecodeError):
            reasons.append(
                f"Modified module '{rel_src}' failed Python AST parsing after replacement patch."
            )
            continue
        defined: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                defined.add(node.target.id)
        module_symbols[mod_name] = defined

    for scenario_id, test_code in sorted(approved_test_codes.items()):
        try:
            test_tree = ast.parse(test_code, filename=f"{scenario_id}.py")
        except SyntaxError:
            continue
        for node in ast.walk(test_tree):
            if isinstance(node, ast.ImportFrom) and node.module in module_symbols:
                defined = module_symbols[node.module]
                for alias in node.names:
                    if alias.name != "*" and alias.name not in defined:
                        reasons.append(
                            f"Approved test '{scenario_id}' imports '{alias.name}' from "
                            f"'{node.module}', which is missing after the replacement patch."
                        )

    return len(reasons) == 0, reasons


def build_repair_request_evidence(
    *,
    comparison: BaselineComparisonEvidence | None,
    modified: ModifiedVerificationResult | None,
    eligibility: RepairEligibilityResult,
) -> tuple[list[FailureDiagnostic], dict[str, Any], dict[str, Any]]:
    """Build sanitized failure diagnostics & comparison evidence for A4 repair requests.

    Never exposes host filesystem paths, credentials, or unrelated source blobs.
    """
    sanitized_diagnostics: list[FailureDiagnostic] = []
    if modified is not None:
        for diag in modified.failures:
            sanitized_diagnostics.append(
                diag.model_copy(
                    update={
                        "evidence": redact_secrets(diag.evidence),
                        "reason": redact_secrets(diag.reason),
                        "recommendation": redact_secrets(diag.recommendation),
                    }
                )
            )
        if modified.existing_regression_suite is not None:
            for diag in modified.existing_regression_suite.failures:
                sanitized_diagnostics.append(
                    diag.model_copy(
                        update={
                            "evidence": redact_secrets(diag.evidence),
                            "reason": redact_secrets(diag.reason),
                            "recommendation": redact_secrets(diag.recommendation),
                        }
                    )
                )

    comparison_payload: dict[str, Any] = {}
    if comparison is not None:
        failing_items = [
            {
                "test_identity": tc.test_identity,
                "scenario_id": tc.scenario_id,
                "suite_kind": tc.suite_kind.value,
                "test_purpose": tc.test_purpose.value,
                "criterion_ids": list(tc.criterion_ids),
                "baseline_outcome": tc.baseline_outcome.value,
                "modified_outcome": tc.modified_outcome.value,
                "classification": tc.classification.value,
                "is_mandatory_regression": tc.is_mandatory_regression,
                "rationale": redact_secrets(tc.rationale),
            }
            for tc in comparison.test_comparisons
            if tc.classification
            in {
                TestComparisonClassification.NEWLY_INTRODUCED_FAILURE,
                TestComparisonClassification.PERSISTENT_FAILURE,
            }
        ]
        comparison_payload = {
            "status": comparison.status.value,
            "baseline_ref": comparison.baseline_ref,
            "baseline_commit": comparison.baseline_commit,
            "approved_test_hashes_verified": comparison.approved_test_hashes_verified,
            "has_confirmed_regression": comparison.summary.has_confirmed_regression,
            "newly_introduced_failures": list(comparison.summary.newly_introduced_failures),
            "preserved_behaviors": list(comparison.summary.preserved_behaviors),
            "newly_satisfied_acceptance_criteria": list(
                comparison.summary.newly_satisfied_acceptance_criteria
            ),
            "persistent_failures": list(comparison.summary.persistent_failures),
            "failing_comparisons": failing_items,
        }

    previous_verification_evidence: dict[str, Any] = {
        "eligibility_category": eligibility.category.value,
        "attributable_test_identities": list(eligibility.attributable_test_identities),
        "confirmed_regression_identities": list(eligibility.confirmed_regression_identities),
        "failed_criterion_ids": list(eligibility.failed_criterion_ids),
        "failure_signature": eligibility.failure_signature,
        "modified_verification_outcome": (
            modified.verification_outcome.value if modified is not None else "unavailable"
        ),
        "modified_passed": modified.passed if modified is not None else None,
        "modified_failed": modified.failed if modified is not None else None,
        "baseline_comparison": comparison_payload,
    }
    return sanitized_diagnostics, comparison_payload, previous_verification_evidence
