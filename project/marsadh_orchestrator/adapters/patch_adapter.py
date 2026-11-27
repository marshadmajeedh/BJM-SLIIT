"""Injectable A4 Automated Patch Generation Adapter for ChangeGuard AI (M02).

Design invariants:
- No production LLM patch generation call is implemented here; this module
  provides the clean M02 dependency-injection boundary so the future A4 owner
  can connect their implementation without redesigning the orchestrator.
- Missing production A4 implementation produces ``PATCH_GENERATOR_UNAVAILABLE``
  and never silently falls back to offline fixture generation.
- ``live_llm`` mode NEVER invokes an offline fixture provider.
- Invalid A4 responses, tampered patch hashes, mismatched attempt numbers, and
  unknown acceptance criterion references are strictly rejected.
- Original repository files are never modified by this adapter (A4 receives
  serialized request context only and returns a unified diff proposal).
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.store import redact_secrets
from project.schemas.agents import AgentError
from project.schemas.patch import (
    PatchFileChange,
    PatchGenerationRequest,
    PatchGenerationResult,
    PatchModelMetadata,
    PatchProposal,
    compute_patch_sha256,
)
from project.schemas.requirements import AmbiguitySeverity

DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL = "deterministic_offline_patch_fixture"


@runtime_checkable
class PatchGeneratorProvider(Protocol):
    """Protocol implemented by future teammate-owned A4 patch generation agents."""

    def generate_patch(
        self,
        request: PatchGenerationRequest,
    ) -> PatchGenerationResult | dict[str, Any]:
        """Generate a validated patch proposal or structured error for ``request``."""
        ...


PatchProviderCallable = Callable[
    [PatchGenerationRequest], PatchGenerationResult | dict[str, Any]
]
InjectablePatchProvider = PatchGeneratorProvider | PatchProviderCallable


def _invoke_provider(
    provider: InjectablePatchProvider,
    request: PatchGenerationRequest,
) -> Any:
    if isinstance(provider, PatchGeneratorProvider):
        return provider.generate_patch(request)
    return provider(request)


class DeterministicPremiumDiscountPatchFixture:
    """Deterministic offline test fixture for the mini_pos premium discount demo.

    Intended SOLELY for automated testing and offline integration demonstrations.
    Explicitly labels all output with ``is_deterministic_fixture=True`` and
    ``is_mock_output=True`` and must NEVER be presented as actual AI-generated
    patch evidence or enabled as a live-mode fallback.
    """

    def __init__(
        self,
        *,
        model_label: str = DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL,
        discount_multiplier: float = 0.90,
    ) -> None:
        self.model_label = model_label
        self.discount_multiplier = discount_multiplier

    def generate_patch(
        self,
        request: PatchGenerationRequest,
    ) -> PatchGenerationResult:
        """Build a deterministic unified Git diff adding the 10% premium discount."""
        metadata = PatchModelMetadata(
            provider="offline_fixture",
            model_name="deterministic_premium_discount_fixture",
            model_label=self.model_label,
            generation_mode="offline_test",
            is_deterministic_fixture=True,
            is_mock_output=True,
        )

        # Locate billing.py inside the request's relevant source files
        target_path: str | None = None
        original_content: str | None = None
        for rel_path, content in sorted(request.relevant_source_files.items()):
            norm = rel_path.replace("\\", "/")
            if norm == "billing.py" or norm.endswith("/billing.py"):
                target_path = norm
                original_content = content
                break

        if target_path is None or original_content is None:
            return PatchGenerationResult(
                status="error",
                attempt_number=request.attempt_number,
                model_metadata=metadata,
                is_deterministic_fixture=True,
                is_mock_output=True,
                error=AgentError(
                    code="FIXTURE_TARGET_NOT_APPLICABLE",
                    message=(
                        "Deterministic premium-discount fixture requires a 'billing.py' "
                        "file in relevant_source_files."
                    ),
                ),
            )

        # Preserve the exact line endings of the target workspace file (LF or CRLF)
        eol = "\r\n" if "\r\n" in original_content else "\n"

        # Transform baseline apply_discount or checkout pattern deterministically
        target_block_apply = (
            f"def apply_discount(total, customer_type):{eol}"
            f"    return total{eol}"
        )
        replacement_block_apply = (
            f"def apply_discount(total, customer_type):{eol}"
            f"    if customer_type == 'premium':{eol}"
            f"        return total * {self.discount_multiplier:.2f}{eol}"
            f"    return total{eol}"
        )

        target_block_checkout = (
            f"def checkout(amount, customer_type='regular'):{eol}"
            f"    return amount{eol}"
        )
        replacement_block_checkout = (
            f"def checkout(amount, customer_type='regular'):{eol}"
            f"    if customer_type == 'premium':{eol}"
            f"        return amount * {self.discount_multiplier:.2f}{eol}"
            f"    return amount{eol}"
        )

        if target_block_apply in original_content:
            updated_content = original_content.replace(
                target_block_apply, replacement_block_apply, 1
            )
            summary = (
                "Add 10% premium customer discount in billing.apply_discount "
                "while preserving regular customer totals."
            )
        elif target_block_checkout in original_content:
            updated_content = original_content.replace(
                target_block_checkout, replacement_block_checkout, 1
            )
            summary = (
                "Add 10% premium customer discount in billing.checkout "
                "while preserving regular customer totals."
            )
        else:
            return PatchGenerationResult(
                status="error",
                attempt_number=request.attempt_number,
                model_metadata=metadata,
                is_deterministic_fixture=True,
                is_mock_output=True,
                error=AgentError(
                    code="FIXTURE_TARGET_NOT_APPLICABLE",
                    message=(
                        f"Deterministic fixture could not locate baseline discount function "
                        f"in '{target_path}'."
                    ),
                ),
            )

        orig_lines = original_content.splitlines(keepends=True)
        new_lines = updated_content.splitlines(keepends=True)
        diff_body = "".join(
            difflib.unified_diff(
                orig_lines,
                new_lines,
                fromfile=f"a/{target_path}",
                tofile=f"b/{target_path}",
                lineterm=eol,
                n=3,
            )
        )
        unified_diff = f"diff --git a/{target_path} b/{target_path}{eol}{diff_body}"
        if not unified_diff.endswith("\n"):
            unified_diff += eol

        added = sum(
            1
            for line in diff_body.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in diff_body.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )

        # Reference only actual acceptance criterion IDs from A1
        available_cids = [ac.criterion_id for ac in request.acceptance_criteria]
        related_cids = [
            ac.criterion_id
            for ac in request.acceptance_criteria
            if ac.mandatory or "billing" in ac.target_modules
        ]
        if not related_cids and available_cids:
            related_cids = [available_cids[0]]

        proposal = PatchProposal(
            proposal_id=f"PATCH-{request.attempt_number:03d}",
            unified_diff=unified_diff,
            patch_sha256=compute_patch_sha256(unified_diff),
            affected_files=[
                PatchFileChange(
                    path=target_path,
                    change_type="modify",
                    added_lines=added,
                    removed_lines=removed,
                    summary=summary,
                    related_criterion_ids=related_cids,
                )
            ],
            change_rationale=(
                "Offline deterministic fixture patch implementing a 10% discount "
                "exclusively for customer_type == 'premium' while keeping regular "
                "customer pricing unchanged."
            ),
            related_criterion_ids=related_cids,
            assumptions=[
                "Generated by an offline deterministic test fixture (not an AI model).",
                "Assumes customer_type uses exact lowercase string 'premium'.",
            ],
            risk_indicators=[
                "Downstream modules importing billing (e.g., refunds, dashboard, reports) "
                "must be verified after patch application.",
            ],
            notes=[
                "OFFLINE FIXTURE DEMONSTRATION ONLY: Not actual AI-generated patch evidence.",
            ],
        )

        return PatchGenerationResult(
            status="proposed",
            attempt_number=request.attempt_number,
            proposal=proposal,
            model_metadata=metadata,
            is_deterministic_fixture=True,
            is_mock_output=True,
            notes=[
                "Generated by deterministic offline patch fixture (is_deterministic_fixture=True, is_mock_output=True).",
            ],
        )


class PatchGenerationAdapter:
    """Modular, dependency-injectable M02 adapter for A4 Patch Generation.

    Default behavior with no injected provider returns ``status='unavailable'``
    with ``PATCH_GENERATOR_UNAVAILABLE`` so missing A4 intelligence fails
    explicitly rather than fabricating patches or silently falling back.
    """

    def __init__(
        self,
        config: OrchestratorConfig | None = None,
        *,
        production_provider: InjectablePatchProvider | None = None,
        offline_fixture_provider: InjectablePatchProvider | None = None,
    ) -> None:
        self.config = config
        self._production_provider = production_provider
        self._offline_fixture_provider = offline_fixture_provider

    @property
    def is_production_configured(self) -> bool:
        """Return True iff a production A4 provider has been explicitly injected."""
        return self._production_provider is not None

    @property
    def is_offline_fixture_configured(self) -> bool:
        """Return True iff an offline deterministic fixture provider has been injected."""
        return self._offline_fixture_provider is not None

    def _error_result(
        self,
        *,
        status: str,
        attempt_number: int,
        execution_mode: str,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        is_fixture: bool = False,
        model_label: str = "unconfigured",
    ) -> PatchGenerationResult:
        mode_literal = "live_llm" if execution_mode == "live_llm" else "offline_test"
        fixture_flag = False if mode_literal == "live_llm" else is_fixture
        return PatchGenerationResult(
            status=status,  # type: ignore[arg-type]
            attempt_number=attempt_number,
            proposal=None,
            model_metadata=PatchModelMetadata(
                provider="offline_fixture" if fixture_flag else None,
                model_label=model_label,
                generation_mode=mode_literal,
                is_deterministic_fixture=fixture_flag,
                is_mock_output=fixture_flag,
            ),
            is_deterministic_fixture=fixture_flag,
            is_mock_output=fixture_flag,
            error=AgentError(
                code=code,
                message=redact_secrets(message),
                details=details or {},
                retryable=retryable,
            ),
        )

    def generate(self, request: PatchGenerationRequest) -> PatchGenerationResult:
        """Invoke the configured A4 provider and strictly validate its response."""
        # 1. Defense-in-depth: refuse generation when blocking ambiguities remain unresolved
        blocking_ids = [
            amb.ambiguity_id
            for amb in request.unresolved_ambiguities
            if (amb.mandatory_to_resolve or amb.severity == AmbiguitySeverity.BLOCKING)
            and not amb.resolved
        ]
        if blocking_ids:
            return self._error_result(
                status="blocked",
                attempt_number=request.attempt_number,
                execution_mode=request.execution_mode,
                code="UNRESOLVED_REQUIREMENT_AMBIGUITY",
                message=(
                    "Patch generation blocked: mandatory requirement ambiguities remain "
                    f"unresolved ({', '.join(blocking_ids)})."
                ),
                details={"unresolved_ambiguity_ids": blocking_ids},
                model_label="blocked_ambiguity",
            )

        # 2. Strict provider selection — live_llm NEVER falls back to offline fixture
        used_offline_fixture = False
        if request.execution_mode == "live_llm":
            if self._production_provider is None:
                return self._error_result(
                    status="unavailable",
                    attempt_number=request.attempt_number,
                    execution_mode="live_llm",
                    code="PATCH_GENERATOR_UNAVAILABLE",
                    message=(
                        "Production A4 patch generation agent is not implemented or configured "
                        "for live_llm mode. Refusing to fall back to deterministic fixture output."
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
                    status="unavailable",
                    attempt_number=request.attempt_number,
                    execution_mode="offline_test",
                    code="PATCH_GENERATOR_UNAVAILABLE",
                    message=(
                        "No A4 patch generation provider or deterministic offline fixture "
                        "is configured. Inject a PatchGeneratorProvider implementation."
                    ),
                    details={"execution_mode": "offline_test"},
                    model_label="unconfigured",
                )

        # 3. Invoke provider without leaking secrets
        try:
            raw_output = _invoke_provider(active_provider, request)
        except Exception as exc:  # noqa: BLE001
            return self._error_result(
                status="error",
                attempt_number=request.attempt_number,
                execution_mode=request.execution_mode,
                code="A4_PROVIDER_EXCEPTION",
                message=f"A4 patch generator raised an exception: {exc}",
                details={"exception_type": type(exc).__name__},
                is_fixture=used_offline_fixture,
                model_label=DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL
                if used_offline_fixture
                else "provider_error",
            )

        # 4. Strictly validate response schema
        if raw_output is None or not isinstance(
            raw_output, (PatchGenerationResult, dict)
        ):
            return self._error_result(
                status="error",
                attempt_number=request.attempt_number,
                execution_mode=request.execution_mode,
                code="INVALID_PATCH_RESPONSE",
                message=(
                    "A4 patch generator returned an invalid response type; expected "
                    "PatchGenerationResult or conforming dictionary."
                ),
                is_fixture=used_offline_fixture,
                model_label=DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL
                if used_offline_fixture
                else "invalid_response",
            )

        try:
            result = (
                raw_output
                if isinstance(raw_output, PatchGenerationResult)
                else PatchGenerationResult.model_validate(raw_output)
            )
        except ValidationError as exc:
            first_msg = exc.errors()[0].get("msg", str(exc)) if exc.errors() else str(exc)
            return self._error_result(
                status="error",
                attempt_number=request.attempt_number,
                execution_mode=request.execution_mode,
                code="INVALID_PATCH_RESPONSE",
                message=f"A4 response failed PatchGenerationResult schema validation: {first_msg}",
                details={"validation_error_count": len(exc.errors())},
                is_fixture=used_offline_fixture,
                model_label=DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL
                if used_offline_fixture
                else "invalid_response",
            )

        # 5. Enforce execution-mode & fixture-labeling honesty
        if request.execution_mode == "live_llm":
            if (
                result.is_deterministic_fixture
                or result.is_mock_output
                or result.model_metadata.is_deterministic_fixture
                or result.model_metadata.is_mock_output
                or result.model_metadata.generation_mode != "live_llm"
            ):
                return self._error_result(
                    status="error",
                    attempt_number=request.attempt_number,
                    execution_mode="live_llm",
                    code="LIVE_MODE_MOCK_REJECTED",
                    message=(
                        "A4 provider returned fixture/mock or non-live metadata while "
                        "execution_mode='live_llm' was requested."
                    ),
                    model_label=result.model_metadata.model_label,
                )
        elif used_offline_fixture and (
            not result.is_deterministic_fixture or not result.is_mock_output
        ):
            return self._error_result(
                status="error",
                attempt_number=request.attempt_number,
                execution_mode="offline_test",
                code="INVALID_FIXTURE_LABELING",
                message=(
                    "Offline fixture patch provider must explicitly set "
                    "is_deterministic_fixture=True and is_mock_output=True."
                ),
                is_fixture=True,
                model_label=DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL,
            )

        # 6. Validate attempt number matches request
        if result.attempt_number != request.attempt_number:
            return self._error_result(
                status="error",
                attempt_number=request.attempt_number,
                execution_mode=request.execution_mode,
                code="INVALID_PATCH_ATTEMPT_NUMBER",
                message=(
                    f"A4 returned attempt_number={result.attempt_number}, "
                    f"expected {request.attempt_number}."
                ),
                is_fixture=used_offline_fixture,
                model_label=result.model_metadata.model_label,
            )

        # 7. Validate acceptance criterion references when a proposal is returned
        if result.status == "proposed" and result.proposal is not None:
            valid_cids = {ac.criterion_id for ac in request.acceptance_criteria}
            unknown_proposal = [
                cid
                for cid in result.proposal.related_criterion_ids
                if cid not in valid_cids
            ]
            unknown_file_cids: list[str] = []
            for fc in result.proposal.affected_files:
                for cid in fc.related_criterion_ids:
                    if cid not in valid_cids and cid not in unknown_file_cids:
                        unknown_file_cids.append(cid)
            all_unknown = sorted(set(unknown_proposal + unknown_file_cids))
            if all_unknown:
                return self._error_result(
                    status="error",
                    attempt_number=request.attempt_number,
                    execution_mode=request.execution_mode,
                    code="INVALID_CRITERION_REFERENCE",
                    message=(
                        "Patch proposal references unknown acceptance criterion ID(s) "
                        f"not present in A1 analysis: {all_unknown}"
                    ),
                    details={"unknown_criterion_ids": all_unknown},
                    is_fixture=used_offline_fixture,
                    model_label=result.model_metadata.model_label,
                )

        return result
