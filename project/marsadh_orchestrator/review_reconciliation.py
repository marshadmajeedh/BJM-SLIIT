"""Deterministic Evidence Reconciliation, Candidate Integrity & Approval Policy (M02 / P0-9).

Single source of truth shared by ``OrchestratorService`` (report compilation and
Gate 2 approval enforcement) and ``project/api/schemas.py`` (``allowed_actions``
projection) so approval rules can never drift across layers.

Enforced principles:
1. Reviewer support cannot override failed mandatory verification.
2. Reviewer support cannot override confirmed regressions.
3. Reviewer support cannot override unresolved mandatory ambiguities.
4. Reviewer support cannot override workspace or candidate evidence integrity failures.
5. A material reviewer concern produces an explicit disagreement or review-required outcome.
6. Unsupported reviewer claims are marked unsubstantiated.
7. Unavailable independent review is reported honestly.
8. Offline reviewer fixtures are never presented as independent live AI evidence.
9. ``complete_requirement_assurance`` remains False whenever mandatory live evidence is missing.
10. A6 can never automatically approve, merge, push, or deploy changes.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from project.marsadh_orchestrator.adapters.review_adapter import (
    build_known_artifact_registry,
    validate_review_evidence_references,
)
from project.schemas.agents import (
    BaselineComparisonEvidence,
    VerificationComparisonStatus,
    VerificationOutcome,
    VerificationResult,
)
from project.schemas.repair import RepairTerminationReason
from project.schemas.requirements import AmbiguitySeverity, RequirementTraceability
from project.schemas.review import (
    EvidenceReconciliationResult,
    IndependentReviewRequest,
    IndependentReviewResult,
    ReviewAvailabilityStatus,
    ReviewFindingSeverity,
    ReviewRecommendation,
)
from project.schemas.workflow import WorkflowMode, WorkflowRunRecord


@dataclass(frozen=True)
class CandidateIntegrityCheck:
    """Result of verifying that final verification evidence belongs to the current candidate."""

    valid: bool
    violations: tuple[str, ...]
    primary_code: str | None = None


@dataclass(frozen=True)
class ApprovalPolicyDecision:
    """Unified Gate 2 approval policy decision used across orchestrator and API DTOs."""

    ordinary_approval_allowed: bool
    blocking_code: str | None
    blocking_message: str | None
    blocking_details: dict[str, Any]
    restrictions: tuple[str, ...]


def verify_candidate_evidence_integrity(
    run: WorkflowRunRecord,
) -> CandidateIntegrityCheck:
    """Verify final verification evidence, snapshots, patch identity, and test hashes match.

    Prevents an earlier passing verification attempt from authorizing a later
    unverified or different candidate patch/snapshot, and ensures approved test
    hashes remained consistent across verification attempts.
    """
    violations: list[str] = []
    primary_code: str | None = None

    def _add(code: str, message: str) -> None:
        nonlocal primary_code
        if primary_code is None:
            primary_code = code
        violations.append(message)

    mod_ver = run.modified_verification
    comp = run.baseline_comparison or (
        run.verification.baseline_comparison if run.verification is not None else None
    )

    # 1. Modified verification snapshot must match current active candidate snapshot
    if (
        mod_ver is not None
        and mod_ver.source_snapshot_sha256
        and run.source_snapshot_sha256
        and not hmac.compare_digest(
            mod_ver.source_snapshot_sha256, run.source_snapshot_sha256
        )
    ):
        _add(
            "CANDIDATE_VERIFICATION_MISMATCH",
            "Final modified verification snapshot SHA-256 does not match the current candidate "
            "source snapshot; earlier verification evidence cannot authorize a different candidate.",
        )

    if (
        mod_ver is not None
        and mod_ver.git_diff_sha256
        and run.git_diff_sha256
        and not hmac.compare_digest(mod_ver.git_diff_sha256, run.git_diff_sha256)
    ):
        _add(
            "CANDIDATE_VERIFICATION_MISMATCH",
            "Final modified verification Git diff SHA-256 does not match the current candidate diff.",
        )

    # 2. Baseline comparison modified snapshot must match current candidate snapshot
    if (
        comp is not None
        and comp.modified_source_snapshot_sha256
        and run.source_snapshot_sha256
        and not hmac.compare_digest(
            comp.modified_source_snapshot_sha256, run.source_snapshot_sha256
        )
    ):
        _add(
            "CANDIDATE_VERIFICATION_MISMATCH",
            "Baseline-versus-modified comparison refers to a different modified source snapshot "
            "than the current candidate.",
        )

    # 3. Baseline snapshot identity consistency
    if (
        comp is not None
        and comp.baseline_source_snapshot_sha256
        and run.baseline_source_snapshot_sha256
        and not hmac.compare_digest(
            comp.baseline_source_snapshot_sha256, run.baseline_source_snapshot_sha256
        )
    ):
        _add(
            "BASELINE_SNAPSHOT_MISMATCH",
            "Baseline comparison snapshot SHA-256 does not match the run's recorded baseline snapshot.",
        )

    # 4. Patch generation candidate identity consistency (generate_and_verify_change)
    if run.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE:
        if run.patch_application is not None and run.patch_application.applied:
            if (
                run.patch_proposal_sha256
                and not hmac.compare_digest(
                    run.patch_application.patch_sha256, run.patch_proposal_sha256
                )
            ):
                _add(
                    "CANDIDATE_PATCH_MISMATCH",
                    "Applied patch SHA-256 does not match the active candidate patch proposal SHA-256.",
                )
            if (
                run.patch_application.resulting_source_snapshot_sha256
                and run.source_snapshot_sha256
                and not hmac.compare_digest(
                    run.patch_application.resulting_source_snapshot_sha256,
                    run.source_snapshot_sha256,
                )
            ):
                _add(
                    "CANDIDATE_PATCH_MISMATCH",
                    "Applied patch resulting snapshot SHA-256 does not match the active candidate snapshot.",
                )
            if (
                run.original_source_snapshot_sha256
                and not hmac.compare_digest(
                    run.patch_application.original_source_snapshot_sha256,
                    run.original_source_snapshot_sha256,
                )
            ):
                _add(
                    "ORIGINAL_SNAPSHOT_MISMATCH",
                    "Applied patch original snapshot SHA-256 does not match the preserved original snapshot.",
                )

        if run.repair_history is not None:
            hist = run.repair_history
            if (
                hist.final_candidate_patch_sha256
                and run.patch_proposal_sha256
                and not hmac.compare_digest(
                    hist.final_candidate_patch_sha256, run.patch_proposal_sha256
                )
            ):
                _add(
                    "CANDIDATE_PATCH_MISMATCH",
                    "Repair history final_candidate_patch_sha256 does not match the active run patch SHA-256.",
                )
            if hist.final_candidate_attempt_number is not None:
                matching = [
                    att
                    for att in hist.attempts
                    if att.attempt_number == hist.final_candidate_attempt_number
                ]
                if not matching:
                    _add(
                        "CANDIDATE_VERIFICATION_MISMATCH",
                        "Repair history final_candidate_attempt_number does not correspond to a recorded attempt.",
                    )
                else:
                    final_att = matching[-1]
                    if (
                        final_att.resulting_source_snapshot_sha256
                        and run.source_snapshot_sha256
                        and not hmac.compare_digest(
                            final_att.resulting_source_snapshot_sha256,
                            run.source_snapshot_sha256,
                        )
                    ):
                        _add(
                            "CANDIDATE_VERIFICATION_MISMATCH",
                            "Final candidate attempt snapshot SHA-256 does not match the active workspace snapshot.",
                        )
                    if (
                        final_att.modified_verification is None
                        and run.verification is not None
                        and run.verification.verification_passed
                    ):
                        _add(
                            "CANDIDATE_VERIFICATION_MISMATCH",
                            "Final candidate patch attempt has no modified verification evidence; "
                            "earlier passing verification cannot authorize an unverified candidate.",
                        )

    # 5. Approved test hashes parity across Gate 1 review, modified verification, and all verified attempts
    if run.test_review is not None and run.test_review.approved and run.test_review.approved_test_hashes:
        expected_hashes = dict(run.test_review.approved_test_hashes)
        if (
            mod_ver is not None
            and mod_ver.execution_status == "completed"
            and mod_ver.approved_test_hashes
            and dict(mod_ver.approved_test_hashes) != expected_hashes
        ):
            _add(
                "APPROVED_TEST_HASH_MISMATCH",
                "Approved test SHA-256 hashes recorded during modified verification do not match "
                "the Gate-1-approved test hashes.",
            )
        if run.repair_history is not None:
            for att in run.repair_history.attempts:
                if (
                    att.modified_verification is not None
                    and att.modified_verification.execution_status == "completed"
                    and att.modified_verification.approved_test_hashes
                    and dict(att.modified_verification.approved_test_hashes)
                    != expected_hashes
                ):
                    _add(
                        "APPROVED_TEST_HASH_MISMATCH",
                        f"Approved test SHA-256 hashes in repair attempt #{att.attempt_number} "
                        "do not match Gate-1-approved test hashes.",
                    )

    # 6. Consistency between aggregate run.verification and run.modified_verification
    if (
        run.verification is not None
        and mod_ver is not None
        and run.verification.verification_passed != mod_ver.verification_passed
    ):
        _add(
            "CANDIDATE_VERIFICATION_MISMATCH",
            "Aggregate verification_passed flag conflicts with modified_verification.verification_passed.",
        )

    return CandidateIntegrityCheck(
        valid=len(violations) == 0,
        violations=tuple(violations),
        primary_code=primary_code,
    )


def _check_review_candidate_identity(
    run: WorkflowRunRecord,
    review: IndependentReviewResult,
) -> tuple[bool, list[str]]:
    """Verify an ``IndependentReviewResult`` matches the current frozen candidate."""
    mismatches: list[str] = []
    if review.reviewed_run_id != run.run_id:
        mismatches.append(
            f"Review references run '{review.reviewed_run_id}', expected '{run.run_id}'."
        )

    expected_patch_sha = (
        run.repair_history.final_candidate_patch_sha256
        if run.repair_history is not None
        and run.repair_history.final_candidate_patch_sha256
        else run.patch_proposal_sha256
    )
    if expected_patch_sha is not None:
        if not review.reviewed_candidate_patch_sha256 or not hmac.compare_digest(
            review.reviewed_candidate_patch_sha256, expected_patch_sha
        ):
            mismatches.append(
                "Independent review references a stale or different candidate patch SHA-256 "
                f"({review.reviewed_candidate_patch_sha256 or 'missing'} != {expected_patch_sha})."
            )

    expected_prop_id = (
        run.repair_history.final_candidate_proposal_id
        if run.repair_history is not None
        and run.repair_history.final_candidate_proposal_id
        else (
            run.patch_generation.proposal.proposal_id
            if run.patch_generation is not None
            and run.patch_generation.proposal is not None
            else None
        )
    )
    if (
        expected_prop_id is not None
        and review.reviewed_candidate_proposal_id is not None
        and review.reviewed_candidate_proposal_id != expected_prop_id
    ):
        mismatches.append(
            f"Independent review references proposal '{review.reviewed_candidate_proposal_id}', "
            f"expected '{expected_prop_id}'."
        )

    if run.source_snapshot_sha256 is not None:
        if not review.reviewed_modified_snapshot_sha256 or not hmac.compare_digest(
            review.reviewed_modified_snapshot_sha256, run.source_snapshot_sha256
        ):
            mismatches.append(
                "Independent review references a stale or different modified source snapshot SHA-256."
            )

    if (
        run.baseline_source_snapshot_sha256 is not None
        and review.reviewed_baseline_snapshot_sha256 is not None
        and not hmac.compare_digest(
            review.reviewed_baseline_snapshot_sha256,
            run.baseline_source_snapshot_sha256,
        )
    ):
        mismatches.append(
            "Independent review references a different baseline source snapshot SHA-256."
        )

    return len(mismatches) == 0, mismatches


class EvidenceReconciliationEngine:
    """Deterministically reconciles verification, baseline, traceability, repair, and A6 review."""

    @classmethod
    def reconcile(
        cls,
        *,
        run: WorkflowRunRecord,
        verification: VerificationResult,
        comparison: BaselineComparisonEvidence,
        criterion_traceability: list[RequirementTraceability],
        traceability_meta: Mapping[str, Any],
        review_request: IndependentReviewRequest | None,
        review_result: IndependentReviewResult | None,
    ) -> tuple[EvidenceReconciliationResult, IndependentReviewResult | None]:
        """Reconcile all deterministic evidence with A6 review findings.

        Returns ``(reconciliation_result, updated_review_result)`` where finding
        ``substantiated`` flags and reconciliation notes have been populated.
        """
        integrity = verify_candidate_evidence_integrity(run)
        deterministic_passed = bool(
            verification.verification_passed
            and verification.verification_outcome == VerificationOutcome.PASSED
        )
        has_confirmed_regression = bool(
            comparison.summary.has_confirmed_regression
            or comparison.summary.blocks_ordinary_approval
        )
        has_blocking_ambiguity = bool(
            run.requirement_analysis is not None
            and (
                run.requirement_analysis.has_mandatory_unresolved_ambiguities
                or any(
                    (
                        amb.mandatory_to_resolve
                        or amb.severity == AmbiguitySeverity.BLOCKING
                    )
                    and not amb.resolved
                    for amb in run.requirement_analysis.ambiguities
                )
            )
        )
        repair_blocked = run.repair_termination_reason in {
            RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED,
            RepairTerminationReason.TEST_MODIFICATION_REJECTED,
        }
        patch_ready = (
            run.workflow_mode != WorkflowMode.GENERATE_AND_VERIFY_CHANGE
            or (run.patch_application is not None and run.patch_application.applied)
        )

        # Deterministic baseline assurance (before A6)
        det_requirement_proven = bool(
            traceability_meta.get("requirement_verification_proven", False)
        )

        material_disagreements: list[str] = []
        unsubstantiated_claims: list[str] = []
        invalid_refs: list[str] = []
        approval_restrictions: list[str] = []

        blocking_error_code: str | None = None
        blocks_ordinary_approval = False

        # --- Deterministic blocks ALWAYS take precedence (Principles 1–4) ---
        if not integrity.valid:
            blocks_ordinary_approval = True
            blocking_error_code = (
                integrity.primary_code or "CANDIDATE_INTEGRITY_FAILED"
            )
            approval_restrictions.extend(integrity.violations)

        if not deterministic_passed:
            blocks_ordinary_approval = True
            if blocking_error_code is None:
                blocking_error_code = "VERIFICATION_APPROVAL_BLOCKED"
            approval_restrictions.append(
                f"Mandatory verification did not pass (outcome='{verification.verification_outcome.value}'); "
                "ordinary approval is blocked regardless of reviewer opinion."
            )

        if has_confirmed_regression:
            blocks_ordinary_approval = True
            if blocking_error_code is None:
                blocking_error_code = "REGRESSION_APPROVAL_BLOCKED"
            approval_restrictions.append(
                "Baseline-versus-modified comparison confirmed newly introduced regression(s): "
                + ", ".join(comparison.summary.newly_introduced_failures)
                + "."
            )

        if repair_blocked and run.repair_termination_reason is not None:
            blocks_ordinary_approval = True
            if blocking_error_code is None:
                blocking_error_code = "REPAIR_REVIEW_REQUIRED"
            approval_restrictions.append(
                f"Bounded automated repair terminated due to '{run.repair_termination_reason.value}'; "
                "ordinary approval is blocked pending fresh human test and patch review."
            )

        if not patch_ready:
            blocks_ordinary_approval = True
            if blocking_error_code is None:
                blocking_error_code = "PATCH_APPLICATION_REQUIRED"
            approval_restrictions.append(
                "generate_and_verify_change run lacks confirmed isolated-workspace patch application evidence."
            )

        if has_blocking_ambiguity:
            blocks_ordinary_approval = True
            if blocking_error_code is None:
                blocking_error_code = "UNRESOLVED_REQUIREMENT_AMBIGUITY"
            approval_restrictions.append(
                "Mandatory unresolved requirement ambiguities remain open; ordinary approval is blocked."
            )

        has_deterministic_block = blocks_ordinary_approval

        # --- Process independent reviewer output ---
        updated_review = review_result
        review_identity_ok = False
        review_availability = (
            review_result.availability
            if review_result is not None
            else ReviewAvailabilityStatus.UNAVAILABLE
        )
        reviewer_rec = (
            review_result.recommendation if review_result is not None else None
        )
        reviewer_is_fixture = bool(
            review_result is not None
            and (
                review_result.is_deterministic_fixture
                or review_result.is_mock_output
                or review_result.availability
                == ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE
            )
        )

        # Case 1: Reviewer unavailable or skipped
        if (
            review_result is None
            or review_result.status == "unavailable"
            or review_availability == ReviewAvailabilityStatus.UNAVAILABLE
        ):
            approval_restrictions.append(
                "Independent review (A6) is unavailable; evidence package is incomplete "
                "(complete_requirement_assurance=False)."
            )
            if run.require_independent_review and not has_deterministic_block:
                blocks_ordinary_approval = True
                blocking_error_code = "INDEPENDENT_REVIEW_REQUIRED"
                approval_restrictions.append(
                    "Run policy requires completed independent review before ordinary approval."
                )

            recon_status = (
                "reconciled_deterministic_block"
                if has_deterministic_block
                else "reconciled_reviewer_unavailable"
            )
            assurance_cls = (
                "deterministic_verification_blocked"
                if has_deterministic_block
                else "reviewer_unavailable"
            )
            summary = (
                "Deterministic verification or integrity block prevents ordinary approval; "
                "independent reviewer (A6) was also unavailable."
                if has_deterministic_block
                else "Independent reviewer (A6) is unavailable; deterministic evidence is preserved "
                "as an incomplete evidence package without live independent review."
            )
            return (
                EvidenceReconciliationResult(
                    reconciliation_status=recon_status,
                    assurance_classification=assurance_cls,
                    deterministic_verification_passed=deterministic_passed,
                    has_confirmed_regression=has_confirmed_regression,
                    has_blocking_ambiguity=has_blocking_ambiguity,
                    candidate_integrity_verified=integrity.valid,
                    review_candidate_identity_verified=False,
                    review_availability=ReviewAvailabilityStatus.UNAVAILABLE,
                    reviewer_recommendation=None,
                    reviewer_is_deterministic_fixture=False,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=blocks_ordinary_approval,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=summary,
                ),
                updated_review,
            )

        # Case 2: Reviewer failed or returned invalid output
        if review_result.status in {"invalid", "error"} or review_availability == ReviewAvailabilityStatus.FAILED:
            err_code = review_result.error.code if review_result.error else "REVIEW_FAILED"
            err_msg = (
                review_result.error.message
                if review_result.error
                else "Independent review did not produce a valid result."
            )
            if review_result.error and isinstance(
                review_result.error.details.get("invalid_references"), list
            ):
                invalid_refs.extend(
                    str(item)
                    for item in review_result.error.details["invalid_references"]
                )
            approval_restrictions.append(
                f"Independent review (A6) failed or was rejected ({err_code}): {err_msg}"
            )
            if run.require_independent_review and not has_deterministic_block:
                blocks_ordinary_approval = True
                blocking_error_code = "INDEPENDENT_REVIEW_FAILED"

            recon_status = (
                "reconciled_deterministic_block"
                if has_deterministic_block
                else "reconciled_reviewer_invalid_or_stale"
            )
            assurance_cls = (
                "deterministic_verification_blocked"
                if has_deterministic_block
                else "incomplete_evidence_package"
            )
            summary = (
                f"Independent review result was rejected ({err_code}); "
                "rejected or stale review output is never accepted as valid assurance evidence."
            )
            return (
                EvidenceReconciliationResult(
                    reconciliation_status=recon_status,
                    assurance_classification=assurance_cls,
                    deterministic_verification_passed=deterministic_passed,
                    has_confirmed_regression=has_confirmed_regression,
                    has_blocking_ambiguity=has_blocking_ambiguity,
                    candidate_integrity_verified=integrity.valid,
                    review_candidate_identity_verified=False,
                    review_availability=ReviewAvailabilityStatus.FAILED,
                    reviewer_recommendation=None,
                    reviewer_is_deterministic_fixture=reviewer_is_fixture,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=blocks_ordinary_approval,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=summary,
                ),
                updated_review,
            )

        # Case 3: Completed review — verify candidate/snapshot identity & references defensively
        review_identity_ok, identity_mismatches = _check_review_candidate_identity(
            run, review_result
        )
        if not review_identity_ok:
            approval_restrictions.extend(identity_mismatches)
            approval_restrictions.append(
                "Stale independent review evidence was rejected because it does not match "
                "the current frozen candidate patch or snapshot."
            )
            if not has_deterministic_block:
                blocks_ordinary_approval = True
                blocking_error_code = "STALE_REVIEW_CANDIDATE"
            return (
                EvidenceReconciliationResult(
                    reconciliation_status=(
                        "reconciled_deterministic_block"
                        if has_deterministic_block
                        else "reconciled_reviewer_invalid_or_stale"
                    ),
                    assurance_classification=(
                        "deterministic_verification_blocked"
                        if has_deterministic_block
                        else "incomplete_evidence_package"
                    ),
                    deterministic_verification_passed=deterministic_passed,
                    has_confirmed_regression=has_confirmed_regression,
                    has_blocking_ambiguity=has_blocking_ambiguity,
                    candidate_integrity_verified=integrity.valid,
                    review_candidate_identity_verified=False,
                    review_availability=ReviewAvailabilityStatus.FAILED,
                    reviewer_recommendation=None,
                    reviewer_is_deterministic_fixture=reviewer_is_fixture,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=identity_mismatches,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=blocks_ordinary_approval,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=(
                        "Stale independent review evidence rejected: candidate patch or snapshot "
                        "identity mismatch."
                    ),
                ),
                updated_review,
            )

        # Defensive reference validation when review_request is available
        if review_request is not None:
            known_registry = build_known_artifact_registry(review_request)
            updated_review, def_invalid = validate_review_evidence_references(
                review_result, known_registry
            )
            invalid_refs.extend(def_invalid)
        else:
            updated_review = review_result

        # Inspect every finding for substantiation and materiality (Principles 5 & 6)
        material_findings = []
        for finding in updated_review.findings:
            is_material = finding.is_material or finding.severity in {
                ReviewFindingSeverity.CRITICAL,
                ReviewFindingSeverity.HIGH,
            }
            if finding.substantiated is False or not finding.evidence_references:
                unsubstantiated_claims.append(
                    f"Finding {finding.finding_id} ('{finding.title}') lacks verified artifact "
                    "evidence references and is marked unsubstantiated."
                )
            if is_material:
                material_findings.append(finding)
                material_disagreements.append(
                    f"[{finding.severity.value.upper()}] {finding.finding_id} "
                    f"({finding.category.value}): {finding.title} — {finding.description}"
                )

        # Principle 1–4: Detect reviewer support conflicting with deterministic block (Case C!)
        if has_deterministic_block and reviewer_rec == ReviewRecommendation.SUPPORT:
            conflict_msg = (
                "Independent reviewer recommended 'support', which conflicts with deterministic "
                "blocking evidence (failed verification, confirmed regression, blocking ambiguity, "
                "or candidate integrity failure). Deterministic evidence takes precedence; "
                "reviewer support is unsubstantiated and cannot authorize approval."
            )
            material_disagreements.append(conflict_msg)
            unsubstantiated_claims.append(conflict_msg)
            approval_restrictions.append(conflict_msg)
            return (
                EvidenceReconciliationResult(
                    reconciliation_status="reconciled_deterministic_block",
                    assurance_classification="deterministic_verification_blocked",
                    deterministic_verification_passed=deterministic_passed,
                    has_confirmed_regression=has_confirmed_regression,
                    has_blocking_ambiguity=has_blocking_ambiguity,
                    candidate_integrity_verified=integrity.valid,
                    review_candidate_identity_verified=True,
                    review_availability=review_availability,
                    reviewer_recommendation=reviewer_rec,
                    reviewer_is_deterministic_fixture=reviewer_is_fixture,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=True,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=conflict_msg,
                ),
                updated_review,
            )

        if has_deterministic_block:
            summary = (
                "Deterministic verification or integrity block prevents ordinary approval "
                f"(reviewer recommendation='{reviewer_rec.value if reviewer_rec else 'none'}')."
            )
            return (
                EvidenceReconciliationResult(
                    reconciliation_status="reconciled_deterministic_block",
                    assurance_classification="deterministic_verification_blocked",
                    deterministic_verification_passed=deterministic_passed,
                    has_confirmed_regression=has_confirmed_regression,
                    has_blocking_ambiguity=has_blocking_ambiguity,
                    candidate_integrity_verified=integrity.valid,
                    review_candidate_identity_verified=True,
                    review_availability=review_availability,
                    reviewer_recommendation=reviewer_rec,
                    reviewer_is_deterministic_fixture=reviewer_is_fixture,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=True,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=summary,
                ),
                updated_review,
            )

        # --- Deterministic checks passed; reconcile against A6 recommendation & findings ---
        has_reviewer_rejection = reviewer_rec == ReviewRecommendation.REJECT
        has_reviewer_needs_review = reviewer_rec == ReviewRecommendation.NEEDS_REVIEW
        has_material_concern = bool(material_findings) or has_reviewer_rejection

        if has_reviewer_rejection and not material_findings:
            material_disagreements.append(
                "Independent reviewer recommended 'reject' despite passing deterministic tests."
            )

        if has_material_concern:
            # Principle 5: Material concern produces explicit disagreement and blocks ordinary
            # approval in generation workflows / mandatory review policy (and on explicit rejection).
            mandatory_review_policy = (
                run.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE
                or run.require_independent_review
                or has_reviewer_rejection
                or bool(material_findings)
            )
            if mandatory_review_policy:
                blocks_ordinary_approval = True
                blocking_error_code = (
                    "INDEPENDENT_REVIEW_REJECTED"
                    if has_reviewer_rejection
                    else "INDEPENDENT_REVIEW_DISAGREEMENT"
                )
            approval_restrictions.append(
                "Unresolved material disagreement between passing tests and independent review "
                f"(recommendation='{reviewer_rec.value if reviewer_rec else 'unknown'}', "
                f"material_findings={len(material_findings)}). Ordinary approval is blocked; "
                "submit 'needs_review' or 'reject'."
            )
            if reviewer_is_fixture:
                approval_restrictions.append(
                    "Note: Independent review output came from an offline deterministic fixture "
                    "(is_deterministic_fixture=True), not live AI."
                )
            summary = (
                "Material disagreement detected: deterministic verification passed, but the "
                "independent reviewer raised material concern(s) or recommended rejection. "
                "Additional human review is required and ordinary approval is blocked."
            )
            return (
                EvidenceReconciliationResult(
                    reconciliation_status="reconciled_with_disagreement",
                    assurance_classification="reviewer_disagreement",
                    deterministic_verification_passed=True,
                    has_confirmed_regression=False,
                    has_blocking_ambiguity=False,
                    candidate_integrity_verified=True,
                    review_candidate_identity_verified=True,
                    review_availability=review_availability,
                    reviewer_recommendation=reviewer_rec,
                    reviewer_is_deterministic_fixture=reviewer_is_fixture,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=blocks_ordinary_approval,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=summary,
                ),
                updated_review,
            )

        if has_reviewer_needs_review:
            approval_restrictions.append(
                "Independent reviewer expressed uncertainty (recommendation='needs_review'); "
                "additional human review is advised and complete_requirement_assurance remains False."
            )
            if run.require_independent_review:
                blocks_ordinary_approval = True
                blocking_error_code = "INDEPENDENT_REVIEW_NEEDS_REVIEW"
                approval_restrictions.append(
                    "Run policy requires explicit reviewer support before ordinary approval."
                )
            if reviewer_is_fixture:
                approval_restrictions.append(
                    "Independent review used an offline deterministic fixture (not live independent AI)."
                )
            summary = (
                "Deterministic verification passed, but independent reviewer recommended "
                "'needs_review' due to remaining uncertainty or non-material concerns."
            )
            return (
                EvidenceReconciliationResult(
                    reconciliation_status="reconciled_with_disagreement",
                    assurance_classification="incomplete_evidence_package",
                    deterministic_verification_passed=True,
                    has_confirmed_regression=False,
                    has_blocking_ambiguity=False,
                    candidate_integrity_verified=True,
                    review_candidate_identity_verified=True,
                    review_availability=review_availability,
                    reviewer_recommendation=reviewer_rec,
                    reviewer_is_deterministic_fixture=reviewer_is_fixture,
                    material_disagreements=material_disagreements,
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=blocks_ordinary_approval,
                    blocking_error_code=blocking_error_code,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=summary,
                ),
                updated_review,
            )

        # --- Reviewer recommends SUPPORT with zero material findings & deterministic tests passed ---
        if reviewer_is_fixture:
            # Principle 8 & 9: Offline fixture support can NEVER claim live AI review or
            # complete requirement assurance!
            approval_restrictions.append(
                "Independent review support was produced by an offline deterministic fixture "
                "(is_deterministic_fixture=True), NOT live independent AI verification. "
                "Human approval records review of an incomplete/offline evidence package."
            )
            summary = (
                "Deterministic verification passed and offline deterministic reviewer fixture "
                "supported the final candidate. Ready for human review; does NOT represent "
                "live independent AI verification or complete requirement assurance."
            )
            return (
                EvidenceReconciliationResult(
                    reconciliation_status="reconciled_aligned",
                    assurance_classification="incomplete_evidence_package",
                    deterministic_verification_passed=True,
                    has_confirmed_regression=False,
                    has_blocking_ambiguity=False,
                    candidate_integrity_verified=True,
                    review_candidate_identity_verified=True,
                    review_availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
                    reviewer_recommendation=ReviewRecommendation.SUPPORT,
                    reviewer_is_deterministic_fixture=True,
                    material_disagreements=[],
                    unsubstantiated_claims=unsubstantiated_claims,
                    invalid_evidence_references=invalid_refs,
                    approval_restrictions=approval_restrictions,
                    blocks_ordinary_approval=False,
                    blocking_error_code=None,
                    complete_requirement_assurance=False,
                    evidence_package_complete=False,
                    reconciled_summary=summary,
                ),
                updated_review,
            )

        # Live production A6 reviewer supported the final candidate with zero material findings!
        has_valid_refs = any(r.verified is True for r in updated_review.evidence_references)
        if not has_valid_refs and not updated_review.evidence_references:
            unsubstantiated_claims.append(
                "Independent reviewer recommended 'support' without citing explicit artifact evidence references."
            )

        # Principle 9: Complete requirement assurance requires ALL mandatory live evidence
        # (A1 criteria verified, baseline comparison completed with regression safety, non-mock
        # stages, AND live A6 support with verified evidence references).
        complete_assurance = bool(
            det_requirement_proven
            and comparison.status == VerificationComparisonStatus.COMPLETED
            and comparison.summary.regression_safety_established
            and review_availability == ReviewAvailabilityStatus.COMPLETED
            and not reviewer_is_fixture
            and not invalid_refs
            and has_valid_refs
            and not unsubstantiated_claims
        )
        if complete_assurance:
            assurance_cls = "fully_supported_requirement_assurance"
            summary = (
                "Deterministic verification, completed baseline comparison, mandatory criterion "
                "traceability, and live independent review (A6) all align in support of the "
                "final candidate. Final human decision remains mandatory."
            )
        else:
            assurance_cls = "reviewer_supported_verification"
            approval_restrictions.append(
                "Live independent reviewer supported the verified candidate, but some upstream "
                "evidence remains incomplete or offline (complete_requirement_assurance=False)."
            )
            summary = (
                "Deterministic verification passed and live independent reviewer supported the "
                "final candidate, though complete requirement assurance is not yet established "
                "across all upstream evidence stages. Final human decision remains mandatory."
            )

        return (
            EvidenceReconciliationResult(
                reconciliation_status="reconciled_aligned",
                assurance_classification=assurance_cls,
                deterministic_verification_passed=True,
                has_confirmed_regression=False,
                has_blocking_ambiguity=False,
                candidate_integrity_verified=True,
                review_candidate_identity_verified=True,
                review_availability=ReviewAvailabilityStatus.COMPLETED,
                reviewer_recommendation=ReviewRecommendation.SUPPORT,
                reviewer_is_deterministic_fixture=False,
                material_disagreements=[],
                unsubstantiated_claims=unsubstantiated_claims,
                invalid_evidence_references=invalid_refs,
                approval_restrictions=approval_restrictions,
                blocks_ordinary_approval=False,
                blocking_error_code=None,
                complete_requirement_assurance=complete_assurance,
                evidence_package_complete=complete_assurance,
                reconciled_summary=summary,
            ),
            updated_review,
        )


def evaluate_approval_policy(run: WorkflowRunRecord) -> ApprovalPolicyDecision:
    """Single source of truth for Gate 2 ordinary-approval eligibility and restrictions.

    Used identically by ``OrchestratorService.submit_final_decision`` and
    ``project/api/schemas.py._allowed_actions`` so API affordances and backend
    enforcement never diverge, while remaining backward compatible with historical
    runs persisted before P0-9.
    """
    restrictions: list[str] = []
    if run.review_reconciliation is not None:
        restrictions.extend(run.review_reconciliation.approval_restrictions)

    # 1. Candidate evidence integrity check (guards against stale/mismatched verification)
    integrity = verify_candidate_evidence_integrity(run)
    if not integrity.valid:
        code = integrity.primary_code or "CANDIDATE_INTEGRITY_FAILED"
        msg = (
            "Cannot approve a change package when candidate verification integrity check fails: "
            + " ".join(integrity.violations)
            + " Submit 'needs_review' or 'reject' instead."
        )
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code=code,
            blocking_message=msg,
            blocking_details={"integrity_violations": list(integrity.violations)},
            restrictions=tuple(dict.fromkeys([*restrictions, *integrity.violations])),
        )

    active_comparison = (
        run.baseline_comparison
        or (run.verification.baseline_comparison if run.verification is not None else None)
    )

    # 2. Mandatory verification passed
    if run.verification is None or not run.verification.verification_passed:
        outcome_val = (
            run.verification.verification_outcome.value
            if run.verification is not None
            else "missing"
        )
        details: dict[str, Any] = {"verification_outcome": outcome_val}
        reg_clause = ""
        if (
            active_comparison is not None
            and active_comparison.summary.has_confirmed_regression
        ):
            details["has_confirmed_regression"] = True
            details["newly_introduced_failures"] = list(
                active_comparison.summary.newly_introduced_failures
            )
            reg_clause = (
                " Confirmed newly introduced regression(s): "
                + ", ".join(active_comparison.summary.newly_introduced_failures)
                + "."
            )
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code="VERIFICATION_APPROVAL_BLOCKED",
            blocking_message=(
                "Cannot approve a change package when mandatory verification did not pass "
                f"(verification_outcome='{outcome_val}').{reg_clause} "
                "Submit 'needs_review' or 'reject' instead."
            ),
            blocking_details=details,
            restrictions=tuple(dict.fromkeys(restrictions)),
        )

    # 3. Confirmed newly introduced regression
    if (
        active_comparison is not None
        and active_comparison.summary.blocks_ordinary_approval
    ):
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code="REGRESSION_APPROVAL_BLOCKED",
            blocking_message=(
                "Cannot approve a change package when baseline-versus-modified comparison "
                "confirmed newly introduced mandatory regression(s) "
                f"({', '.join(active_comparison.summary.newly_introduced_failures)}). "
                "Submit 'needs_review' or 'reject' instead."
            ),
            blocking_details={
                "newly_introduced_failures": list(
                    active_comparison.summary.newly_introduced_failures
                ),
                "comparison_status": active_comparison.status.value,
            },
            restrictions=tuple(dict.fromkeys(restrictions)),
        )

    # 4. Bounded repair integrity/scope stop
    if run.repair_termination_reason in {
        RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED,
        RepairTerminationReason.TEST_MODIFICATION_REJECTED,
    }:
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code="REPAIR_REVIEW_REQUIRED",
            blocking_message=(
                "Cannot approve a change package after bounded repair terminated due to "
                f"'{run.repair_termination_reason.value}'. Submit 'needs_review' or 'reject' "
                "and perform fresh test review instead."
            ),
            blocking_details={
                "repair_termination_reason": run.repair_termination_reason.value
            },
            restrictions=tuple(dict.fromkeys(restrictions)),
        )

    # 5. Patch application evidence required for generation mode
    if (
        run.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE
        and (run.patch_application is None or not run.patch_application.applied)
    ):
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code="PATCH_APPLICATION_REQUIRED",
            blocking_message=(
                "Cannot approve a generate_and_verify_change run without confirmed "
                "isolated-workspace patch application evidence."
            ),
            blocking_details={},
            restrictions=tuple(dict.fromkeys(restrictions)),
        )

    # 6. Unresolved mandatory business ambiguities
    if (
        run.requirement_analysis is not None
        and run.requirement_analysis.has_mandatory_unresolved_ambiguities
    ):
        blocking_ids = [
            amb.ambiguity_id
            for amb in run.requirement_analysis.ambiguities
            if (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
            and not amb.resolved
        ]
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code="UNRESOLVED_REQUIREMENT_AMBIGUITY",
            blocking_message=(
                "Cannot approve a change package while mandatory unresolved requirement "
                f"ambiguities remain open ({', '.join(blocking_ids)}). "
                "Submit 'needs_review' or 'reject' instead."
            ),
            blocking_details={"unresolved_ambiguity_ids": blocking_ids},
            restrictions=tuple(dict.fromkeys(restrictions)),
        )

    # 7. Independent review candidate identity staleness check (defense in depth)
    if (
        run.independent_review is not None
        and run.independent_review.status == "completed"
    ):
        id_ok, id_mismatches = _check_review_candidate_identity(
            run, run.independent_review
        )
        if not id_ok:
            return ApprovalPolicyDecision(
                ordinary_approval_allowed=False,
                blocking_code="STALE_REVIEW_CANDIDATE",
                blocking_message=(
                    "Cannot approve a change package with stale independent review evidence: "
                    + " ".join(id_mismatches)
                    + " Submit 'needs_review' or 'reject' instead."
                ),
                blocking_details={"identity_mismatches": id_mismatches},
                restrictions=tuple(dict.fromkeys([*restrictions, *id_mismatches])),
            )

    # 8. Evidence reconciliation blocking policy (material reviewer disagreement, rejection,
    #    or mandatory independent review policy when reviewer unavailable/failed/uncertain)
    if (
        run.review_reconciliation is not None
        and run.review_reconciliation.blocks_ordinary_approval
    ):
        code = (
            run.review_reconciliation.blocking_error_code
            or "INDEPENDENT_REVIEW_DISAGREEMENT"
        )
        return ApprovalPolicyDecision(
            ordinary_approval_allowed=False,
            blocking_code=code,
            blocking_message=(
                "Cannot approve a change package due to independent review reconciliation "
                f"policy ({code}): {run.review_reconciliation.reconciled_summary} "
                "Submit 'needs_review' or 'reject' instead."
            ),
            blocking_details={
                "reconciliation_status": run.review_reconciliation.reconciliation_status,
                "assurance_classification": run.review_reconciliation.assurance_classification,
                "material_disagreements": list(
                    run.review_reconciliation.material_disagreements
                ),
            },
            restrictions=tuple(dict.fromkeys(restrictions)),
        )

    return ApprovalPolicyDecision(
        ordinary_approval_allowed=True,
        blocking_code=None,
        blocking_message=None,
        blocking_details={},
        restrictions=tuple(dict.fromkeys(restrictions)),
    )
