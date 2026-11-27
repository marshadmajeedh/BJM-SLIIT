"""Injectable A6 Independent Reviewer Adapter for ChangeGuard AI (M02 / P0-9).

Design invariants:
- No production A6 LLM call is implemented here; this module provides the
  clean dependency-injection boundary (``IndependentReviewerProvider``) so the
  future A6 owner can integrate their production implementation without
  redesigning the orchestrator.
- Missing production A6 implementation produces ``status='unavailable'`` with
  ``availability='unavailable'`` and ``INDEPENDENT_REVIEWER_UNAVAILABLE`` — never
  silently falling back to offline fixture output.
- ``live_llm`` mode NEVER invokes an offline deterministic fixture provider.
- Invalid reviewer responses, mismatched candidate/snapshot identities,
  fabricated evidence references, and secret leakage are strictly rejected or
  sanitized.
- The independent reviewer receives bounded, frozen evidence only and NEVER
  modifies source files, tests, or workflow decisions directly.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import ValidationError

from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import redact_nested_secrets, redact_secrets
from project.schemas.agents import AgentError
from project.schemas.review import (
    IndependentReviewRequest,
    IndependentReviewResult,
    ReviewAvailabilityStatus,
    ReviewEvidenceReference,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewModelMetadata,
    ReviewRecommendation,
)

DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL = "deterministic_offline_independent_reviewer_fixture"


@runtime_checkable
class IndependentReviewerProvider(Protocol):
    """Protocol implemented by future teammate-owned A6 Independent Reviewer agents."""

    def review_change(
        self,
        request: IndependentReviewRequest,
    ) -> IndependentReviewResult | dict[str, Any]:
        """Independently review frozen candidate evidence and return a validated result."""
        ...


ReviewProviderCallable = Callable[
    [IndependentReviewRequest], IndependentReviewResult | dict[str, Any]
]
InjectableReviewerProvider = IndependentReviewerProvider | ReviewProviderCallable


def _invoke_provider(
    provider: InjectableReviewerProvider,
    request: IndependentReviewRequest,
) -> Any:
    if isinstance(provider, IndependentReviewerProvider):
        return provider.review_change(request)
    return provider(request)


def build_known_artifact_registry(
    request: IndependentReviewRequest,
) -> dict[str, set[str]]:
    """Build deterministic lookup of actual known artifacts from ``IndependentReviewRequest``."""
    registry: dict[str, set[str]] = {
        "criterion": {ac.criterion_id for ac in request.acceptance_criteria},
        "constraint": {bc.constraint_id for bc in request.business_constraints},
        "assumption": {asm.assumption_id for asm in request.unresolved_assumptions},
        "ambiguity": {amb.ambiguity_id for amb in request.unresolved_ambiguities},
        "file": set(request.changed_files)
        | set(request.impacted_modules)
        | set(request.relevant_source_file_paths)
        | {p.replace("\\", "/") for p in request.relevant_source_excerpts},
        "patch_proposal": (
            {request.candidate_proposal_id} if request.candidate_proposal_id else set()
        ),
        "patch_sha256": (
            {request.candidate_patch_sha256} if request.candidate_patch_sha256 else set()
        ),
        "snapshot_sha256": {
            sha
            for sha in (
                request.original_source_snapshot_sha256,
                request.baseline_source_snapshot_sha256,
                request.modified_source_snapshot_sha256,
                request.modified_git_diff_sha256,
            )
            if sha
        },
        "scenario": set(request.approved_test_ids),
        "test_identity": set(request.approved_test_ids),
        "repair_attempt": set(),
        "baseline_comparison": set(),
        "verification_outcome": set(),
    }

    for tr in request.requirement_traceability:
        registry["criterion"].add(tr.criterion_id)
        registry["scenario"].update(tr.generated_scenario_ids)
        registry["file"].update(tr.affected_source_files)

    for att in request.repair_attempt_summary:
        if isinstance(att, dict):
            num = att.get("attempt_number")
            if num is not None:
                registry["repair_attempt"].add(str(num))
                registry["repair_attempt"].add(f"attempt-{num}")
            prop_id = att.get("patch_proposal_id")
            if isinstance(prop_id, str) and prop_id:
                registry["patch_proposal"].add(prop_id)
            p_sha = att.get("patch_sha256")
            if isinstance(p_sha, str) and p_sha:
                registry["patch_sha256"].add(p_sha)

    ver_outcome = request.verification_summary.get("verification_outcome")
    if isinstance(ver_outcome, str) and ver_outcome:
        registry["verification_outcome"].add(ver_outcome)
    if request.verification_summary:
        registry["verification_outcome"].add("modified_verification")

    bc_status = request.baseline_comparison_summary.get("status")
    if isinstance(bc_status, str) and bc_status:
        registry["baseline_comparison"].add(bc_status)
    if request.baseline_comparison_summary:
        registry["baseline_comparison"].add("baseline_comparison")
        for key in (
            "newly_introduced_failures",
            "preserved_behaviors",
            "newly_satisfied_acceptance_criteria",
            "fixed_existing_failures",
            "persistent_failures",
            "unavailable_or_incomparable",
        ):
            vals = request.baseline_comparison_summary.get(key)
            if isinstance(vals, list):
                for item in vals:
                    if isinstance(item, str) and item:
                        registry["test_identity"].add(item)

    # Merge any explicit known_artifact_ids attached to the request
    for ref_type, ids in request.known_artifact_ids.items():
        bucket = registry.setdefault(ref_type, set())
        for item in ids:
            if isinstance(item, str) and item.strip():
                bucket.add(item.strip().replace("\\", "/"))

    return registry


def _check_reference(
    ref: ReviewEvidenceReference,
    known_registry: Mapping[str, set[str]],
) -> bool:
    known = known_registry.get(ref.reference_type, set())
    if not known:
        return False
    norm_id = ref.reference_id.strip().replace("\\", "/")
    if norm_id in known:
        return True
    # Allow matching file basenames only if unambiguous? No — require exact repo-relative
    # or source-relative match to prevent loose/fabricated file citations.
    return False


def validate_review_evidence_references(
    result: IndependentReviewResult,
    known_registry: Mapping[str, set[str]],
) -> tuple[IndependentReviewResult, list[str]]:
    """Validate all evidence references and criterion/file links against known run artifacts.

    Returns ``(annotated_result, invalid_references)`` where each
    ``ReviewEvidenceReference.verified`` is populated deterministically.
    """
    invalid_refs: list[str] = []

    validated_top_refs: list[ReviewEvidenceReference] = []
    for ref in result.evidence_references:
        ok = _check_reference(ref, known_registry)
        validated_top_refs.append(ref.model_copy(update={"verified": ok}))
        if not ok:
            invalid_refs.append(
                f"{ref.reference_type}:{ref.reference_id} (top-level reference not found in run artifacts)"
            )

    known_criteria = known_registry.get("criterion", set())
    known_files = known_registry.get("file", set())
    validated_findings: list[ReviewFinding] = []
    for finding in result.findings:
        finding_refs: list[ReviewEvidenceReference] = []
        finding_invalid = False
        for ref in finding.evidence_references:
            ok = _check_reference(ref, known_registry)
            finding_refs.append(ref.model_copy(update={"verified": ok}))
            if not ok:
                finding_invalid = True
                invalid_refs.append(
                    f"{finding.finding_id}:{ref.reference_type}:{ref.reference_id} "
                    "(not found in run artifacts)"
                )
        for cid in finding.related_criterion_ids:
            if cid not in known_criteria:
                finding_invalid = True
                invalid_refs.append(
                    f"{finding.finding_id}:criterion:{cid} (unknown acceptance criterion ID)"
                )
        for fpath in finding.affected_files:
            norm_f = fpath.replace("\\", "/")
            if norm_f not in known_files:
                finding_invalid = True
                invalid_refs.append(
                    f"{finding.finding_id}:file:{norm_f} (not in reviewed run file scope)"
                )

        has_verified_ref = any(r.verified is True for r in finding_refs)
        substantiated = (not finding_invalid) and has_verified_ref
        note = finding.reconciliation_note
        if finding_invalid:
            note = "Contains invalid or unverified artifact reference(s); marked unsubstantiated."
        elif not finding_refs:
            note = "No supporting artifact evidence references provided; marked unsubstantiated."

        validated_findings.append(
            finding.model_copy(
                update={
                    "evidence_references": finding_refs,
                    "substantiated": substantiated,
                    "reconciliation_note": note,
                }
            )
        )

    annotated = result.model_copy(
        update={
            "evidence_references": validated_top_refs,
            "findings": validated_findings,
        }
    )
    return annotated, invalid_refs


def sanitize_review_result(result: IndependentReviewResult) -> IndependentReviewResult:
    """Redact secret patterns across all text fields of an ``IndependentReviewResult``."""
    sanitized_error = None
    if result.error is not None:
        raw_details = redact_nested_secrets(result.error.details)
        sanitized_error = result.error.model_copy(
            update={
                "message": redact_secrets(result.error.message),
                "details": raw_details if isinstance(raw_details, dict) else {},
            }
        )

    sanitized_findings = [
        f.model_copy(
            update={
                "title": redact_secrets(f.title),
                "description": redact_secrets(f.description),
                "reconciliation_note": redact_secrets(f.reconciliation_note),
                "evidence_references": [
                    ref.model_copy(update={"description": redact_secrets(ref.description)})
                    for ref in f.evidence_references
                ],
            }
        )
        for f in result.findings
    ]
    sanitized_refs = [
        ref.model_copy(update={"description": redact_secrets(ref.description)})
        for ref in result.evidence_references
    ]

    return result.model_copy(
        update={
            "summary": redact_secrets(result.summary),
            "findings": sanitized_findings,
            "requirement_compliance_concerns": [
                redact_secrets(item) for item in result.requirement_compliance_concerns
            ],
            "potential_behavioral_regressions": [
                redact_secrets(item) for item in result.potential_behavioral_regressions
            ],
            "security_and_maintainability_concerns": [
                redact_secrets(item)
                for item in result.security_and_maintainability_concerns
            ],
            "missing_verification_evidence": [
                redact_secrets(item) for item in result.missing_verification_evidence
            ],
            "unsupported_assumptions": [
                redact_secrets(item) for item in result.unsupported_assumptions
            ],
            "evidence_references": sanitized_refs,
            "error": sanitized_error,
            "notes": [redact_secrets(n) for n in result.notes],
        }
    )


class DeterministicIndependentReviewerFixture:
    """Deterministic offline A6 reviewer fixture solely for automated tests & offline demos.

    Explicitly labels every output with ``is_deterministic_fixture=True``,
    ``is_mock_output=True``, and ``availability='completed_offline_fixture'``.
    Must NEVER be presented as live independent AI review evidence or enabled
    as a silent fallback in ``live_llm`` mode.
    """

    def __init__(
        self,
        *,
        mode: Literal[
            "auto",
            "support",
            "material_disagreement",
            "reject",
            "uncertain",
            "false_support",
        ] = "auto",
        model_label: str = DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL,
        custom_concern: str | None = None,
    ) -> None:
        self.mode = mode
        self.model_label = model_label
        self.custom_concern = custom_concern
        self.received_requests: list[IndependentReviewRequest] = []

    def review_change(
        self,
        request: IndependentReviewRequest,
    ) -> IndependentReviewResult:
        """Return a deterministic, artifact-grounded offline review result."""
        self.received_requests.append(request)

        metadata = ReviewModelMetadata(
            provider="offline_fixture",
            model_name=f"deterministic_reviewer_fixture_{self.mode}",
            model_label=self.model_label,
            execution_mode="offline_test",
            is_deterministic_fixture=True,
            is_mock_output=True,
        )

        # Build valid references grounded in actual request artifacts
        base_refs: list[ReviewEvidenceReference] = []
        if request.candidate_proposal_id:
            base_refs.append(
                ReviewEvidenceReference(
                    reference_type="patch_proposal",
                    reference_id=request.candidate_proposal_id,
                    description="Reviewed final candidate patch proposal.",
                )
            )
        if request.candidate_patch_sha256:
            base_refs.append(
                ReviewEvidenceReference(
                    reference_type="patch_sha256",
                    reference_id=request.candidate_patch_sha256,
                    description="Reviewed final candidate patch SHA-256 digest.",
                )
            )
        if request.modified_source_snapshot_sha256:
            base_refs.append(
                ReviewEvidenceReference(
                    reference_type="snapshot_sha256",
                    reference_id=request.modified_source_snapshot_sha256,
                    description="Reviewed frozen modified source snapshot SHA-256.",
                )
            )
        if request.changed_files:
            base_refs.append(
                ReviewEvidenceReference(
                    reference_type="file",
                    reference_id=request.changed_files[0],
                    description="Primary modified application file.",
                )
            )
        if request.approved_test_ids:
            base_refs.append(
                ReviewEvidenceReference(
                    reference_type="scenario",
                    reference_id=request.approved_test_ids[0],
                    description="Approved verification scenario executed on candidate.",
                )
            )
        if request.acceptance_criteria:
            base_refs.append(
                ReviewEvidenceReference(
                    reference_type="criterion",
                    reference_id=request.acceptance_criteria[0].criterion_id,
                    description="Primary acceptance criterion reviewed.",
                )
            )

        ver_passed = bool(request.verification_summary.get("verification_passed", False))
        has_regression = bool(
            request.baseline_comparison_summary.get("has_confirmed_regression", False)
        )
        first_cid = (
            request.acceptance_criteria[0].criterion_id
            if request.acceptance_criteria
            else None
        )
        first_file = request.changed_files[0] if request.changed_files else None

        # Choose effective mode
        effective_mode = self.mode
        if effective_mode == "auto":
            if not ver_passed or has_regression:
                effective_mode = "reject"
            else:
                effective_mode = "support"

        fixture_disclosure = (
            "OFFLINE DETERMINISTIC FIXTURE OUTPUT ONLY "
            "(is_deterministic_fixture=True, is_mock_output=True): "
            "Not live independent AI review evidence."
        )

        if effective_mode in {"support", "false_support"}:
            notes = [fixture_disclosure]
            if effective_mode == "false_support":
                notes.append(
                    "Controlled Demo Case C fixture: deliberately recommends 'support' "
                    "regardless of verification failure to verify deterministic override protection."
                )
            return IndependentReviewResult(
                status="completed",
                availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
                recommendation=ReviewRecommendation.SUPPORT,
                reviewed_run_id=request.run_id,
                reviewed_candidate_proposal_id=request.candidate_proposal_id,
                reviewed_candidate_patch_sha256=request.candidate_patch_sha256,
                reviewed_baseline_snapshot_sha256=request.baseline_source_snapshot_sha256,
                reviewed_modified_snapshot_sha256=request.modified_source_snapshot_sha256,
                summary=(
                    "Offline deterministic reviewer fixture supports the final candidate "
                    "for human review based on inspected diff and verification artifacts "
                    "(not live independent AI verification)."
                ),
                findings=[],
                evidence_references=base_refs,
                model_metadata=metadata,
                is_deterministic_fixture=True,
                is_mock_output=True,
                notes=notes,
            )

        if effective_mode == "material_disagreement":
            # Pick an unverified criterion if one exists, otherwise first acceptance criterion
            target_cid = first_cid
            for tr in request.requirement_traceability:
                if tr.verification_status.value != "verified":
                    target_cid = tr.criterion_id
                    break
            finding_refs: list[ReviewEvidenceReference] = []
            if target_cid:
                finding_refs.append(
                    ReviewEvidenceReference(
                        reference_type="criterion",
                        reference_id=target_cid,
                        description="Acceptance criterion with unverified edge-case or requirement gap.",
                    )
                )
            if first_file:
                finding_refs.append(
                    ReviewEvidenceReference(
                        reference_type="file",
                        reference_id=first_file,
                        description="Modified file inspected for edge-case/requirement compliance.",
                    )
                )
            if not finding_refs:
                finding_refs = list(base_refs[:1])

            concern_text = self.custom_concern or (
                "Material requirement verification gap: boundary handling (e.g., case-insensitive "
                "customer_type, negative amounts, or rounding precision) and downstream module "
                "behavior lack explicit verification coverage despite passing approved tests."
            )
            finding = ReviewFinding(
                finding_id="REV-001",
                category=ReviewFindingCategory.MISSING_VERIFICATION_EVIDENCE,
                severity=ReviewFindingSeverity.HIGH,
                title="Material unverified requirement or conflicting evidence detected",
                description=concern_text,
                is_material=True,
                related_criterion_ids=[target_cid] if target_cid else [],
                affected_files=[first_file] if first_file else [],
                evidence_references=finding_refs,
            )
            return IndependentReviewResult(
                status="completed",
                availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
                recommendation=ReviewRecommendation.NEEDS_REVIEW,
                reviewed_run_id=request.run_id,
                reviewed_candidate_proposal_id=request.candidate_proposal_id,
                reviewed_candidate_patch_sha256=request.candidate_patch_sha256,
                reviewed_baseline_snapshot_sha256=request.baseline_source_snapshot_sha256,
                reviewed_modified_snapshot_sha256=request.modified_source_snapshot_sha256,
                summary=(
                    "Offline deterministic reviewer fixture identified a material unverified "
                    "requirement concern despite passing tests and recommends additional review."
                ),
                findings=[finding],
                requirement_compliance_concerns=[concern_text],
                missing_verification_evidence=[concern_text],
                evidence_references=base_refs,
                model_metadata=metadata,
                is_deterministic_fixture=True,
                is_mock_output=True,
                notes=[
                    fixture_disclosure,
                    "Controlled Demo Case B fixture: raises material disagreement despite passing tests.",
                ],
            )

        if effective_mode == "reject":
            finding_refs = list(base_refs[:2])
            concern_text = self.custom_concern or (
                "Independent reviewer fixture rejects candidate due to material compliance "
                "or behavioral regression risk."
            )
            finding = ReviewFinding(
                finding_id="REV-001",
                category=(
                    ReviewFindingCategory.BEHAVIORAL_REGRESSION
                    if has_regression
                    else ReviewFindingCategory.REQUIREMENT_COMPLIANCE
                ),
                severity=ReviewFindingSeverity.CRITICAL,
                title="Candidate rejected by independent review fixture",
                description=concern_text,
                is_material=True,
                related_criterion_ids=[first_cid] if first_cid else [],
                affected_files=[first_file] if first_file else [],
                evidence_references=finding_refs,
            )
            return IndependentReviewResult(
                status="completed",
                availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
                recommendation=ReviewRecommendation.REJECT,
                reviewed_run_id=request.run_id,
                reviewed_candidate_proposal_id=request.candidate_proposal_id,
                reviewed_candidate_patch_sha256=request.candidate_patch_sha256,
                reviewed_baseline_snapshot_sha256=request.baseline_source_snapshot_sha256,
                reviewed_modified_snapshot_sha256=request.modified_source_snapshot_sha256,
                summary=(
                    "Offline deterministic reviewer fixture recommends rejecting the candidate "
                    "due to material concerns."
                ),
                findings=[finding],
                requirement_compliance_concerns=[concern_text],
                potential_behavioral_regressions=[concern_text] if has_regression else [],
                evidence_references=base_refs,
                model_metadata=metadata,
                is_deterministic_fixture=True,
                is_mock_output=True,
                notes=[fixture_disclosure],
            )

        # mode == "uncertain"
        uncertainty_text = self.custom_concern or (
            "Reviewer fixture cannot establish full compliance from the provided bounded "
            "evidence alone; human inspection of inferred assumptions is advised."
        )
        finding = ReviewFinding(
            finding_id="REV-001",
            category=ReviewFindingCategory.UNSUPPORTED_ASSUMPTION,
            severity=ReviewFindingSeverity.MEDIUM,
            title="Reviewer uncertainty on non-explicit assumptions",
            description=uncertainty_text,
            is_material=False,
            related_criterion_ids=[first_cid] if first_cid else [],
            affected_files=[first_file] if first_file else [],
            evidence_references=list(base_refs[:1]),
        )
        return IndependentReviewResult(
            status="completed",
            availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
            recommendation=ReviewRecommendation.NEEDS_REVIEW,
            reviewed_run_id=request.run_id,
            reviewed_candidate_proposal_id=request.candidate_proposal_id,
            reviewed_candidate_patch_sha256=request.candidate_patch_sha256,
            reviewed_baseline_snapshot_sha256=request.baseline_source_snapshot_sha256,
            reviewed_modified_snapshot_sha256=request.modified_source_snapshot_sha256,
            summary=(
                "Offline deterministic reviewer fixture expresses uncertainty and recommends "
                "manual review of non-explicit assumptions."
            ),
            findings=[finding],
            unsupported_assumptions=[uncertainty_text],
            evidence_references=base_refs,
            model_metadata=metadata,
            is_deterministic_fixture=True,
            is_mock_output=True,
            notes=[fixture_disclosure],
        )


class IndependentReviewAdapter:
    """Modular, dependency-injectable M02 adapter for A6 Independent Review.

    Default behavior with no injected provider returns ``status='unavailable'``
    with ``availability='unavailable'`` and ``INDEPENDENT_REVIEWER_UNAVAILABLE``
    so missing A6 intelligence is reported honestly without fabricating review
    evidence or silently falling back to offline fixtures.
    """

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        *,
        production_provider: InjectableReviewerProvider | None = None,
        offline_fixture_provider: InjectableReviewerProvider | None = None,
    ) -> None:
        self.config = config
        self._production_provider = production_provider
        self._offline_fixture_provider = offline_fixture_provider

    @property
    def is_production_configured(self) -> bool:
        """Return True iff a production A6 provider has been explicitly injected."""
        return self._production_provider is not None

    @property
    def is_offline_fixture_configured(self) -> bool:
        """Return True iff an offline deterministic review fixture has been injected."""
        return self._offline_fixture_provider is not None

    def _error_result(
        self,
        *,
        request: IndependentReviewRequest,
        status: Literal["unavailable", "invalid", "error"],
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        is_fixture: bool = False,
        model_label: str = "unconfigured",
    ) -> IndependentReviewResult:
        mode_literal: Literal["offline_test", "live_llm"] = (
            "live_llm" if request.execution_mode == "live_llm" else "offline_test"
        )
        fixture_flag = False if mode_literal == "live_llm" else is_fixture
        availability = (
            ReviewAvailabilityStatus.UNAVAILABLE
            if status == "unavailable"
            else ReviewAvailabilityStatus.FAILED
        )
        raw_details = redact_nested_secrets(details or {})
        return IndependentReviewResult(
            status=status,
            availability=availability,
            recommendation=None,
            reviewed_run_id=request.run_id,
            reviewed_candidate_proposal_id=request.candidate_proposal_id,
            reviewed_candidate_patch_sha256=request.candidate_patch_sha256,
            reviewed_baseline_snapshot_sha256=request.baseline_source_snapshot_sha256,
            reviewed_modified_snapshot_sha256=request.modified_source_snapshot_sha256,
            summary=redact_secrets(message),
            model_metadata=ReviewModelMetadata(
                provider="offline_fixture" if fixture_flag else None,
                model_label=model_label,
                execution_mode=mode_literal,
                is_deterministic_fixture=fixture_flag,
                is_mock_output=fixture_flag,
            ),
            is_deterministic_fixture=fixture_flag,
            is_mock_output=fixture_flag,
            error=AgentError(
                code=code,
                message=redact_secrets(message),
                details=raw_details if isinstance(raw_details, dict) else {},
                retryable=retryable,
            ),
        )

    def review(self, request: IndependentReviewRequest) -> IndependentReviewResult:
        """Invoke the configured A6 provider, validate identity & references, and sanitize."""
        used_offline_fixture = False
        if request.execution_mode == "live_llm":
            if self._production_provider is None:
                return self._error_result(
                    request=request,
                    status="unavailable",
                    code="INDEPENDENT_REVIEWER_UNAVAILABLE",
                    message=(
                        "Production A6 independent reviewer is not implemented or configured "
                        "for live_llm mode. Refusing to fall back to deterministic offline review."
                    ),
                    details={"execution_mode": "live_llm"},
                    model_label="unconfigured",
                )
            active_provider = self._production_provider
        else:
            if self._offline_fixture_provider is not None:
                active_provider = self._offline_fixture_provider
                used_offline_fixture = True
            elif self._production_provider is not None:
                active_provider = self._production_provider
            else:
                return self._error_result(
                    request=request,
                    status="unavailable",
                    code="INDEPENDENT_REVIEWER_UNAVAILABLE",
                    message=(
                        "No A6 independent reviewer provider or deterministic offline fixture "
                        "is configured. Inject an IndependentReviewerProvider implementation."
                    ),
                    details={"execution_mode": "offline_test"},
                    model_label="unconfigured",
                )

        # Invoke provider safely with error sanitization
        try:
            raw_output = _invoke_provider(active_provider, request)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                request=request,
                status="error",
                code="A6_PROVIDER_EXCEPTION",
                message=f"A6 independent reviewer raised an exception: {exc}",
                details={"exception_type": type(exc).__name__},
                is_fixture=used_offline_fixture,
                model_label=(
                    DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL
                    if used_offline_fixture
                    else "provider_error"
                ),
            )

        if raw_output is None or not isinstance(
            raw_output, (IndependentReviewResult, dict)
        ):
            return self._error_result(
                request=request,
                status="invalid",
                code="INVALID_REVIEW_RESPONSE",
                message=(
                    "A6 independent reviewer returned an invalid response type; expected "
                    "IndependentReviewResult or conforming dictionary."
                ),
                is_fixture=used_offline_fixture,
                model_label=(
                    DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL
                    if used_offline_fixture
                    else "invalid_response"
                ),
            )

        try:
            result = (
                raw_output
                if isinstance(raw_output, IndependentReviewResult)
                else IndependentReviewResult.model_validate(raw_output)
            )
        except ValidationError as exc:
            first_msg = exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc)
            return self._error_result(
                request=request,
                status="invalid",
                code="INVALID_REVIEW_RESPONSE",
                message=(
                    f"A6 response failed IndependentReviewResult schema validation: {first_msg}"
                ),
                details={"validation_error_count": len(exc.errors())},
                is_fixture=used_offline_fixture,
                model_label=(
                    DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL
                    if used_offline_fixture
                    else "invalid_response"
                ),
            )

        result = sanitize_review_result(result)

        # Enforce live-mode / offline-fixture honesty
        if request.execution_mode == "live_llm":
            if (
                result.is_deterministic_fixture
                or result.is_mock_output
                or result.model_metadata.is_deterministic_fixture
                or result.model_metadata.is_mock_output
                or result.model_metadata.execution_mode != "live_llm"
                or result.availability
                == ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE
            ):
                return self._error_result(
                    request=request,
                    status="invalid",
                    code="LIVE_MODE_MOCK_REJECTED",
                    message=(
                        "A6 provider returned offline fixture/mock or non-live metadata while "
                        "execution_mode='live_llm' was requested."
                    ),
                    model_label=result.model_metadata.model_label,
                )
        elif used_offline_fixture and (
            not result.is_deterministic_fixture
            or not result.is_mock_output
            or (
                result.status == "completed"
                and result.availability
                != ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE
            )
        ):
            return self._error_result(
                request=request,
                status="invalid",
                code="INVALID_FIXTURE_LABELING",
                message=(
                    "Offline fixture reviewer must explicitly set is_deterministic_fixture=True, "
                    "is_mock_output=True, and availability='completed_offline_fixture'."
                ),
                is_fixture=True,
                model_label=DEFAULT_OFFLINE_REVIEW_FIXTURE_LABEL,
            )

        # If provider itself returned a valid non-completed status (unavailable/error/invalid),
        # return it directly (already sanitized).
        if result.status != "completed":
            return result

        # Verify reviewed run, candidate patch, and snapshot identities to reject stale reviews!
        if result.reviewed_run_id != request.run_id:
            return self._error_result(
                request=request,
                status="invalid",
                code="STALE_REVIEW_CANDIDATE",
                message=(
                    f"Independent review targeted run '{result.reviewed_run_id}', "
                    f"expected '{request.run_id}'."
                ),
                is_fixture=used_offline_fixture,
                model_label=result.model_metadata.model_label,
            )

        if request.candidate_patch_sha256 is not None:
            if not result.reviewed_candidate_patch_sha256 or not hmac.compare_digest(
                result.reviewed_candidate_patch_sha256,
                request.candidate_patch_sha256,
            ):
                return self._error_result(
                    request=request,
                    status="invalid",
                    code="STALE_REVIEW_CANDIDATE",
                    message=(
                        "Independent review patch SHA-256 does not match the current final "
                        "candidate patch; rejecting stale review evidence."
                    ),
                    details={
                        "expected_patch_sha256": request.candidate_patch_sha256,
                        "reviewed_patch_sha256": result.reviewed_candidate_patch_sha256,
                    },
                    is_fixture=used_offline_fixture,
                    model_label=result.model_metadata.model_label,
                )

        if (
            request.candidate_proposal_id is not None
            and result.reviewed_candidate_proposal_id is not None
            and result.reviewed_candidate_proposal_id != request.candidate_proposal_id
        ):
            return self._error_result(
                request=request,
                status="invalid",
                code="STALE_REVIEW_CANDIDATE",
                message=(
                    f"Independent review proposal_id '{result.reviewed_candidate_proposal_id}' "
                    f"does not match current final candidate '{request.candidate_proposal_id}'."
                ),
                is_fixture=used_offline_fixture,
                model_label=result.model_metadata.model_label,
            )

        if request.modified_source_snapshot_sha256 is not None:
            if (
                not result.reviewed_modified_snapshot_sha256
                or not hmac.compare_digest(
                    result.reviewed_modified_snapshot_sha256,
                    request.modified_source_snapshot_sha256,
                )
            ):
                return self._error_result(
                    request=request,
                    status="invalid",
                    code="STALE_REVIEW_SNAPSHOT",
                    message=(
                        "Independent review modified snapshot SHA-256 does not match the "
                        "frozen final candidate snapshot; rejecting stale review evidence."
                    ),
                    details={
                        "expected_modified_snapshot_sha256": request.modified_source_snapshot_sha256,
                        "reviewed_modified_snapshot_sha256": result.reviewed_modified_snapshot_sha256,
                    },
                    is_fixture=used_offline_fixture,
                    model_label=result.model_metadata.model_label,
                )

        if (
            request.baseline_source_snapshot_sha256 is not None
            and result.reviewed_baseline_snapshot_sha256 is not None
            and not hmac.compare_digest(
                result.reviewed_baseline_snapshot_sha256,
                request.baseline_source_snapshot_sha256,
            )
        ):
            return self._error_result(
                request=request,
                status="invalid",
                code="STALE_REVIEW_SNAPSHOT",
                message=(
                    "Independent review baseline snapshot SHA-256 does not match the "
                    "verified baseline snapshot; rejecting stale review evidence."
                ),
                is_fixture=used_offline_fixture,
                model_label=result.model_metadata.model_label,
            )

        # Validate all evidence references against actual known run artifacts
        known_registry = build_known_artifact_registry(request)
        annotated, invalid_refs = validate_review_evidence_references(
            result, known_registry
        )
        if invalid_refs:
            return self._error_result(
                request=request,
                status="invalid",
                code="INVALID_REVIEW_EVIDENCE_REFERENCE",
                message=(
                    "Independent reviewer cited fabricated or unknown artifact reference(s): "
                    + "; ".join(invalid_refs[:5])
                ),
                details={"invalid_references": invalid_refs},
                is_fixture=used_offline_fixture,
                model_label=result.model_metadata.model_label,
            )

        return annotated
