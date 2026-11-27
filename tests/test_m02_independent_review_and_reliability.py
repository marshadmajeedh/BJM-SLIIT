"""Comprehensive deterministic offline test suite for P0-9 Independent Review & Backend Reliability.

Covers all 20 required P0-9 test categories and Phase 9 Offline Demonstrations (Cases A, B, C)
using real isolated Git repositories and real local ``pytest`` execution (zero external LLM calls):

1.  Valid independent reviewer response (live-shaped & deterministic offline fixture).
2.  Missing production reviewer (honest unavailable status & optional mandatory policy).
3.  Invalid reviewer response (malformed types, invalid schema, bad fixture labeling).
4.  Invalid evidence references (fabricated artifacts rejected; unreferenced claims unsubstantiated).
5.  Reviewer support with passing verification (Demo Case A + live-shaped complete assurance).
6.  Reviewer support with failed verification (Demo Case C: deterministic block overrides AI opinion).
7.  Reviewer rejection despite passing tests (ordinary approval blocked; reject/needs_review allowed).
8.  Material reviewer disagreement (Demo Case B: material finding blocks ordinary approval).
9.  Reviewer uncertainty (needs_review without material finding).
10. Stale candidate review evidence (superseded patch/snapshot rejected).
11. Live mode rejecting offline fallback and mock-labeled outputs.
12. Reviewer failure and secret + host-path redaction across storage, logs, and API.
13. Review persistence and pre-P0-9 legacy record backward compatibility.
14. Restart recovery from RUNNING_INDEPENDENT_REVIEW / RECONCILING_REVIEW_EVIDENCE without replay
    + preservation of completed repair termination reason + terminal non-resumption.
15. No automatic approval by reviewer & reviewer disagreement never triggers extra patch repair.
16. Existing verify_existing_change and generate_and_verify_change workflow compatibility.
17. Candidate verification evidence integrity (stale/unverified candidate & test hash mismatch).
18. Confirmed reliability audit defects:
    - Initial & replacement patch test/pytest-config/import-hook manipulation protection.
    - Persistent acceptance failure attribution vs unreliable generated-test assumptions.
    - Workspace cleanup containment & symlink protection.
    - Secret redaction in report.verification and stripping raw source excerpts from SQLite.
19. Concurrent Gate 1 and Gate 2 review submission protection.
20. OpenAPI contract parity & /health capability flags.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from project.main import create_app
from project.marsadh_orchestrator import (
    DeterministicIndependentReviewerFixture,
    DeterministicPremiumDiscountPatchFixture,
    DeterministicRegressiveThenFixedPatchFixture,
    EvidenceReconciliationEngine,
    IndependentReviewAdapter,
    InvalidStateTransitionError,
    OrchestratorConfig,
    OrchestratorService,
    PatchGenerationAdapter,
    PatchValidator,
    RepairEligibilityEvaluator,
    ReviewConflictError,
    RunStore,
    WorkspaceManager,
    WorkspaceSecurityError,
    evaluate_approval_policy,
    verify_candidate_evidence_integrity,
)
from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
)
from project.schemas import (
    AcceptanceCriterion,
    AgentError,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    CreateRunRequest,
    EvidenceReconciliationResult,
    ExecutionMode,
    FailureDiagnostic,
    HumanDecision,
    HumanDecisionRequest,
    IndependentReviewRequest,
    IndependentReviewResult,
    IndividualTestOutcome,
    ModifiedVerificationResult,
    PatchApplicationResult,
    PatchFileChange,
    PatchProposal,
    RegressionComparisonSummary,
    RepairEligibilityCategory,
    RepairPolicy,
    RepairTerminationReason,
    RepositoryInfo,
    RequirementAnalysisResult,
    ReviewAvailabilityStatus,
    ReviewEvidenceReference,
    ReviewFinding,
    ReviewFindingCategory,
    ReviewFindingSeverity,
    ReviewModelMetadata,
    ReviewRecommendation,
    TestComparisonClassification,
    TestComparisonResult,
    TestExecutionEvidence,
    TestPurpose,
    TestReviewRequest,
    TestSuiteKind,
    VerificationComparisonStatus,
    VerificationOutcome,
    VerificationResult,
    WorkflowMode,
    WorkflowRunRecord,
    WorkflowState,
    compute_patch_sha256,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MINI_POS_DIR = REPO_ROOT / "sample_projects" / "mini_pos"

PREMIUM_DISCOUNT_REQUIREMENT = (
    "Apply a 10% discount to premium customers only, without changing regular "
    "customer checkout or breaking dependent refund, dashboard, and report modules."
)

BASELINE_BILLING = (
    "def calculate_total(price, quantity):\n"
    "    return price * quantity\n\n\n"
    "def apply_discount(total, customer_type):\n"
    "    return total\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)

CORRECT_BILLING = (
    "def calculate_total(price, quantity):\n"
    "    return price * quantity\n\n\n"
    "def apply_discount(total, customer_type):\n"
    "    if customer_type == 'premium':\n"
    "        return total * 0.90\n"
    "    return total\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)

REGRESSIVE_BILLING = (
    "def calculate_total(price, quantity):\n"
    "    return price * quantity\n\n\n"
    "def apply_discount(total, customer_type):\n"
    "    return total * 0.90\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for P0-9 tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _snapshot_tree_bytes(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _create_mini_pos_repo(
    repo: Path,
    *,
    working_billing: str | None = None,
    existing_test_code: str | None = None,
) -> str:
    app_dir = repo / "mini_pos"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "billing.py").write_text(BASELINE_BILLING, encoding="utf-8")
    for fname in ("refunds.py", "customer_service.py", "dashboard.py", "reports.py"):
        shutil.copyfile(MINI_POS_DIR / fname, app_dir / fname)

    if existing_test_code is not None:
        tests_dir = repo / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_existing_regression.py").write_text(
            existing_test_code, encoding="utf-8"
        )

    _git(repo, "init")
    _git(repo, "config", "user.name", "ChangeGuard P0-9")
    _git(repo, "config", "user.email", "p09@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline clean mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    if working_billing is not None:
        (app_dir / "billing.py").write_text(working_billing, encoding="utf-8")
    return base_commit


def _make_config(
    tmp_path: Path,
    *,
    repos_root: Path,
    configured: dict[str, Path],
    db_name: str = "p09.sqlite3",
    auto_repair_enabled: bool = True,
    max_repair_attempts: int = 2,
    require_independent_review: bool = False,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        database_path=tmp_path / "state" / db_name,
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories=configured,
        trusted_execution_roots=[repos_root],
        verification_timeout_seconds=30,
        auto_repair_enabled=auto_repair_enabled,
        max_repair_attempts=max_repair_attempts,
        require_independent_review=require_independent_review,
        environment="test",
    )


def _behavioral_mini_pos_m03_stub(_prompt: str) -> str:
    header = (
        "import sys\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
    )
    return json.dumps(
        {
            "scenarios": [
                {
                    "title": "Premium checkout applies 10% discount",
                    "rationale": "Verify AC-001 premium discount.",
                    "assumptions": "Offline deterministic behavioral test fixture.",
                    "criterion_ids": ["AC-001"],
                    "test_purpose": TestPurpose.NEW_REQUIREMENT_ACCEPTANCE.value,
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_premium_discount():\n"
                        + "    assert apply_discount(200.0, 'premium') == pytest.approx(180.0)\n"
                        + "    assert checkout(50.0, 4, 'premium') == pytest.approx(180.0)\n"
                    ),
                },
                {
                    "title": "Regular checkout remains unchanged",
                    "rationale": "Verify AC-002 regular checkout preservation.",
                    "assumptions": "Offline deterministic behavioral test fixture.",
                    "criterion_ids": ["AC-002"],
                    "test_purpose": TestPurpose.EXISTING_BEHAVIOR_PRESERVATION.value,
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_regular_unchanged():\n"
                        + "    assert apply_discount(200.0, 'regular') == pytest.approx(200.0)\n"
                        + "    assert checkout(50.0, 4, 'regular') == pytest.approx(200.0)\n"
                    ),
                },
                {
                    "title": "Dependent refunds and reports continue functioning",
                    "rationale": "Verify AC-003 dependent module preservation.",
                    "assumptions": "Offline deterministic behavioral test fixture.",
                    "criterion_ids": ["AC-003"],
                    "test_purpose": TestPurpose.EXISTING_BEHAVIOR_PRESERVATION.value,
                    "test_code": (
                        header
                        + "from refunds import calculate_refund\n"
                        + "from reports import generate_sales_report\n\n"
                        + "def test_dependents():\n"
                        + "    assert calculate_refund(200.0, 'regular') == pytest.approx(200.0)\n"
                        + "    assert generate_sales_report(100.0, 2, 'regular')['total_sales'] == pytest.approx(200.0)\n"
                    ),
                },
            ]
        }
    )


def _sample_review_request(**overrides: Any) -> IndependentReviewRequest:
    patch_sha = "a" * 64
    base_snap = "b" * 64
    mod_snap = "c" * 64
    defaults: dict[str, Any] = {
        "run_id": "run_test_001",
        "workflow_mode": "generate_and_verify_change",
        "execution_mode": "offline_test",
        "original_requirement": PREMIUM_DISCOUNT_REQUIREMENT,
        "acceptance_criteria": [
            AcceptanceCriterion(
                criterion_id="AC-001",
                requirement_reference="10% discount for premium",
                description="Apply 10% discount for premium customers.",
                expected_behavior="Premium customer gets 0.90x total.",
            ),
            AcceptanceCriterion(
                criterion_id="AC-002",
                requirement_reference="regular unchanged",
                description="Regular customer total unchanged.",
                expected_behavior="Regular customer gets 1.0x total.",
                preserves_existing_behavior=True,
            ),
        ],
        "candidate_proposal_id": "PATCH-001",
        "candidate_patch_sha256": patch_sha,
        "candidate_attempt_number": 1,
        "baseline_source_snapshot_sha256": base_snap,
        "modified_source_snapshot_sha256": mod_snap,
        "changed_files": ["mini_pos/billing.py"],
        "impacted_modules": ["mini_pos/refunds.py"],
        "approved_test_ids": ["SCN-001", "SCN-002"],
        "approved_test_hashes": {"SCN-001": "1" * 64, "SCN-002": "2" * 64},
        "verification_summary": {
            "execution_status": "completed",
            "verification_outcome": "passed",
            "verification_passed": True,
            "passed": 2,
            "failed": 0,
        },
        "baseline_comparison_summary": {
            "status": "completed",
            "has_confirmed_regression": False,
            "regression_safety_established": True,
            "preserved_behaviors": ["SCN-002"],
            "newly_satisfied_acceptance_criteria": ["SCN-001"],
        },
    }
    defaults.update(overrides)
    return IndependentReviewRequest(**defaults)


# ---------------------------------------------------------------------------
# Tests 1, 2, 3, 4, 10, 11, 12: Schemas & Injectable A6 Adapter Unit Tests
# ---------------------------------------------------------------------------


def test_review_schemas_and_adapter_valid_missing_invalid_refs_live_and_secrets() -> None:
    """Reqs 1, 2, 3, 4, 10, 11, 12: Adapter validation, reference checks, live guard, secret redaction."""
    req = _sample_review_request()

    # 1. Valid deterministic offline fixture response
    fixture = DeterministicIndependentReviewerFixture(mode="support")
    adapter_ok = IndependentReviewAdapter(offline_fixture_provider=fixture)
    res_ok = adapter_ok.review(req)
    assert res_ok.status == "completed"
    assert res_ok.availability == ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE
    assert res_ok.recommendation == ReviewRecommendation.SUPPORT
    assert res_ok.is_deterministic_fixture is True
    assert res_ok.is_mock_output is True
    assert all(r.verified is True for r in res_ok.evidence_references)

    # 2. Missing production reviewer returns explicit UNAVAILABLE (never fabricates)
    adapter_empty = IndependentReviewAdapter()
    res_unavail = adapter_empty.review(req)
    assert res_unavail.status == "unavailable"
    assert res_unavail.availability == ReviewAvailabilityStatus.UNAVAILABLE
    assert res_unavail.recommendation is None
    assert res_unavail.error is not None
    assert res_unavail.error.code == "INDEPENDENT_REVIEWER_UNAVAILABLE"

    # 3. Invalid reviewer outputs rejected (non-dict, invalid schema, mislabeled fixture)
    res_bad_type = IndependentReviewAdapter(
        offline_fixture_provider=lambda _r: "not-a-dict"  # type: ignore[arg-type,return-value]
    ).review(req)
    assert res_bad_type.status == "invalid"
    assert res_bad_type.availability == ReviewAvailabilityStatus.FAILED
    assert res_bad_type.error is not None
    assert res_bad_type.error.code == "INVALID_REVIEW_RESPONSE"

    res_bad_schema = IndependentReviewAdapter(
        offline_fixture_provider=lambda _r: {"status": "completed", "unexpected": 123}
    ).review(req)
    assert res_bad_schema.status == "invalid"
    assert res_bad_schema.error is not None
    assert res_bad_schema.error.code == "INVALID_REVIEW_RESPONSE"

    # Mislabeled offline fixture claiming is_deterministic_fixture=False is rejected
    def _mislabeled_fixture(r: IndependentReviewRequest) -> dict[str, Any]:
        return {
            "status": "completed",
            "availability": ReviewAvailabilityStatus.COMPLETED.value,
            "recommendation": ReviewRecommendation.SUPPORT.value,
            "reviewed_run_id": r.run_id,
            "reviewed_candidate_proposal_id": r.candidate_proposal_id,
            "reviewed_candidate_patch_sha256": r.candidate_patch_sha256,
            "reviewed_baseline_snapshot_sha256": r.baseline_source_snapshot_sha256,
            "reviewed_modified_snapshot_sha256": r.modified_source_snapshot_sha256,
            "summary": "Pretending to be live AI.",
            "model_metadata": {
                "model_label": "pretend-live",
                "execution_mode": "offline_test",
                "is_deterministic_fixture": False,
                "is_mock_output": False,
            },
            "is_deterministic_fixture": False,
            "is_mock_output": False,
        }

    res_mislabel = IndependentReviewAdapter(
        offline_fixture_provider=_mislabeled_fixture
    ).review(req)
    assert res_mislabel.status == "invalid"
    assert res_mislabel.error is not None
    assert res_mislabel.error.code == "INVALID_FIXTURE_LABELING"

    # 4. Fabricated / invalid evidence references are rejected
    def _bad_refs_provider(r: IndependentReviewRequest) -> IndependentReviewResult:
        return IndependentReviewResult(
            status="completed",
            availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
            recommendation=ReviewRecommendation.NEEDS_REVIEW,
            reviewed_run_id=r.run_id,
            reviewed_candidate_proposal_id=r.candidate_proposal_id,
            reviewed_candidate_patch_sha256=r.candidate_patch_sha256,
            reviewed_baseline_snapshot_sha256=r.baseline_source_snapshot_sha256,
            reviewed_modified_snapshot_sha256=r.modified_source_snapshot_sha256,
            summary="Cites nonexistent artifacts.",
            findings=[
                ReviewFinding(
                    finding_id="REV-001",
                    category=ReviewFindingCategory.REQUIREMENT_COMPLIANCE,
                    severity=ReviewFindingSeverity.HIGH,
                    title="Fabricated reference",
                    description="Points to AC-999 and ghost.py.",
                    related_criterion_ids=["AC-999"],
                    affected_files=["mini_pos/ghost.py"],
                    evidence_references=[
                        ReviewEvidenceReference(
                            reference_type="criterion",
                            reference_id="AC-999",
                        )
                    ],
                )
            ],
            model_metadata=ReviewModelMetadata(
                provider="offline_fixture",
                model_label="bad_refs_fixture",
                execution_mode="offline_test",
                is_deterministic_fixture=True,
                is_mock_output=True,
            ),
            is_deterministic_fixture=True,
            is_mock_output=True,
        )

    res_bad_refs = IndependentReviewAdapter(
        offline_fixture_provider=_bad_refs_provider
    ).review(req)
    assert res_bad_refs.status == "invalid"
    assert res_bad_refs.error is not None
    assert res_bad_refs.error.code == "INVALID_REVIEW_EVIDENCE_REFERENCE"
    assert "AC-999" in res_bad_refs.error.message

    # 10. Stale candidate patch / snapshot identity rejected
    def _stale_patch_provider(r: IndependentReviewRequest) -> IndependentReviewResult:
        return IndependentReviewResult(
            status="completed",
            availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
            recommendation=ReviewRecommendation.SUPPORT,
            reviewed_run_id=r.run_id,
            reviewed_candidate_proposal_id="PATCH-001",
            reviewed_candidate_patch_sha256="f" * 64,  # stale superseded patch hash!
            reviewed_baseline_snapshot_sha256=r.baseline_source_snapshot_sha256,
            reviewed_modified_snapshot_sha256=r.modified_source_snapshot_sha256,
            summary="Reviewed old patch.",
            model_metadata=ReviewModelMetadata(
                provider="offline_fixture",
                model_label="stale_fixture",
                execution_mode="offline_test",
                is_deterministic_fixture=True,
                is_mock_output=True,
            ),
            is_deterministic_fixture=True,
            is_mock_output=True,
        )

    res_stale = IndependentReviewAdapter(
        offline_fixture_provider=_stale_patch_provider
    ).review(req)
    assert res_stale.status == "invalid"
    assert res_stale.error is not None
    assert res_stale.error.code == "STALE_REVIEW_CANDIDATE"

    # 11. Live mode NEVER falls back to offline fixture
    live_req = _sample_review_request(execution_mode="live_llm")
    fixture_not_called = DeterministicIndependentReviewerFixture(mode="support")
    adapter_live_only_fixture = IndependentReviewAdapter(
        production_provider=None,
        offline_fixture_provider=fixture_not_called,
    )
    res_live_unavail = adapter_live_only_fixture.review(live_req)
    assert res_live_unavail.status == "unavailable"
    assert res_live_unavail.error is not None
    assert res_live_unavail.error.code == "INDEPENDENT_REVIEWER_UNAVAILABLE"
    assert len(fixture_not_called.received_requests) == 0

    # Live mode rejects a production provider returning fixture/mock metadata
    res_live_mock = IndependentReviewAdapter(
        production_provider=DeterministicIndependentReviewerFixture(mode="support"),
    ).review(live_req)
    assert res_live_mock.status == "invalid"
    assert res_live_mock.error is not None
    assert res_live_mock.error.code == "LIVE_MODE_MOCK_REJECTED"

    # 12. Provider exception & secret redaction
    secret_key = "gsk_" + "SyntheticSecretReviewKey9876543210"
    bearer = "Bearer super_secret_token_abcdef123456"

    def _crashing_provider(_r: IndependentReviewRequest) -> IndependentReviewResult:
        raise RuntimeError(f"Upstream failure auth={bearer} key={secret_key}")

    res_err = IndependentReviewAdapter(
        offline_fixture_provider=_crashing_provider
    ).review(req)
    assert res_err.status == "error"
    assert res_err.availability == ReviewAvailabilityStatus.FAILED
    assert res_err.error is not None
    assert res_err.error.code == "A6_PROVIDER_EXCEPTION"
    assert secret_key not in res_err.error.message
    assert "super_secret_token_abcdef123456" not in res_err.error.message
    assert "[REDACTED_GROQ_KEY]" in res_err.error.message

    # Schema-level truthfulness guard on EvidenceReconciliationResult
    with pytest.raises(ValidationError):
        EvidenceReconciliationResult(
            reconciliation_status="reconciled_aligned",
            assurance_classification="fully_supported_requirement_assurance",
            deterministic_verification_passed=False,  # Cannot pass approval when verification failed!
            blocks_ordinary_approval=False,
            complete_requirement_assurance=False,
        )
    with pytest.raises(ValidationError):
        EvidenceReconciliationResult(
            reconciliation_status="reconciled_aligned",
            assurance_classification="fully_supported_requirement_assurance",
            deterministic_verification_passed=True,
            review_candidate_identity_verified=True,
            review_availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
            reviewer_recommendation=ReviewRecommendation.SUPPORT,
            reviewer_is_deterministic_fixture=True,
            blocks_ordinary_approval=False,
            complete_requirement_assurance=True,  # Forbidden for offline fixture!
        )


# ---------------------------------------------------------------------------
# Phase 9 Offline Demonstrations A, B, C & Tests 5, 6, 7, 8, 9, 15, 16
# ---------------------------------------------------------------------------


def test_offline_demonstration_case_a_pass_and_fixture_support(tmp_path: Path) -> None:
    """Demo Case A (Reqs 5, 15, 16): Verification passes + deterministic reviewer supports final candidate.

    Expected:
    - Bounded repair fixes attempt 1 regression -> attempt 2 passes.
    - Independent reviewer evaluates the FINAL repaired candidate (attempt 2), NOT attempt 1.
    - Proceeds to human review (Gate 2) without claiming live independent AI verification
      or complete_requirement_assurance.
    - Reviewer never auto-approves; original repo stays untouched.
    """
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "demo_a_pos"
    orig_head = _create_mini_pos_repo(clean_repo)
    orig_tree = _snapshot_tree_bytes(clean_repo)

    patch_fixture = DeterministicRegressiveThenFixedPatchFixture()
    review_fixture = DeterministicIndependentReviewerFixture(mode="support")

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"demo-a-pos": clean_repo},
        db_name="demo_a.sqlite3",
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=patch_fixture,
        review_offline_fixture_provider=review_fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "demo-a-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        gate1 = client.get(f"/api/runs/{run_id}").json()
        assert gate1["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        attempt1_sha = gate1["patch_proposal_sha256"]

        rev = client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
                "notes": "Approve behavioral scenarios for Demo Case A.",
            },
        )
        assert rev.status_code == 202
        app.state.executor.wait_for_run(run_id, timeout=90.0)

        gate2 = client.get(f"/api/runs/{run_id}").json()
        # Never auto-approved by A6!
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2["repair_termination_reason"] == RepairTerminationReason.REPAIR_SUCCEEDED.value
        assert gate2["final_candidate_patch_sha256"] != attempt1_sha

        # Reviewer evaluated the FINAL repaired candidate (attempt 2), not superseded attempt 1!
        assert len(review_fixture.received_requests) == 1
        assert (
            review_fixture.received_requests[0].candidate_patch_sha256
            == gate2["final_candidate_patch_sha256"]
        )
        assert (
            gate2["reviewed_candidate_patch_sha256"]
            == gate2["final_candidate_patch_sha256"]
        )
        assert gate2["review_availability"] == ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE.value
        assert gate2["reviewer_recommendation"] == ReviewRecommendation.SUPPORT.value
        assert gate2["independent_review"]["is_deterministic_fixture"] is True
        assert gate2["independent_review"]["is_mock_output"] is True
        assert gate2["complete_requirement_assurance"] is False
        assert gate2["review_reconciliation"]["reconciliation_status"] == "reconciled_aligned"
        assert gate2["review_reconciliation"]["assurance_classification"] == "incomplete_evidence_package"
        assert gate2["review_reconciliation"]["blocks_ordinary_approval"] is False
        assert gate2["report"]["recommendation_label"] == "ready_for_human_review"
        assert "submit_final_decision:approve" in gate2["allowed_actions"]

        # Verify workflow stage sequence includes independent review & reconciliation
        logs = client.get(f"/api/runs/{run_id}/logs").json()["logs"]
        stages = [entry["stage"] for entry in logs]
        assert WorkflowState.RUNNING_INDEPENDENT_REVIEW.value in stages
        assert WorkflowState.RECONCILING_REVIEW_EVIDENCE.value in stages
        assert stages.index(WorkflowState.RUNNING_INDEPENDENT_REVIEW.value) < stages.index(
            WorkflowState.RECONCILING_REVIEW_EVIDENCE.value
        ) < stages.index(WorkflowState.COMPILING_REPORT.value)

        # Human explicitly approves
        dec = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Demo Case A human review recorded (offline fixture evidence).",
            },
        )
        assert dec.status_code == 200
        assert dec.json()["state"] == WorkflowState.APPROVED.value

    assert _git(clean_repo, "rev-parse", "HEAD") == orig_head
    assert _snapshot_tree_bytes(clean_repo) == orig_tree


def test_offline_demonstration_case_b_pass_but_material_reviewer_disagreement(
    tmp_path: Path,
) -> None:
    """Demo Case B (Reqs 8, 15): Verification passes, but reviewer identifies material concern.

    Expected:
    - Does NOT trigger additional patch repair solely because reviewer disagreed.
    - Produces explicit reviewer_disagreement and recommendation_label='additional_review_required'.
    - Blocks ordinary approval (HTTP 409) while allowing 'needs_review' and 'reject'.
    """
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "demo_b_pos"
    _create_mini_pos_repo(clean_repo)

    patch_fixture = DeterministicPremiumDiscountPatchFixture()
    review_fixture = DeterministicIndependentReviewerFixture(
        mode="material_disagreement"
    )

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"demo-b-pos": clean_repo},
        db_name="demo_b.sqlite3",
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=patch_fixture,
        review_offline_fixture_provider=review_fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "demo-b-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
                "notes": "Approve scenarios for Demo Case B.",
            },
        )
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        gate2 = client.get(f"/api/runs/{run_id}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2["verification"]["verification_passed"] is True
        # No extra repair triggered by reviewer disagreement!
        assert gate2["repair_attempts_consumed"] == 0
        assert gate2["review_availability"] == ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE.value
        assert gate2["reviewer_recommendation"] == ReviewRecommendation.NEEDS_REVIEW.value
        assert len(gate2["material_disagreements"]) >= 1
        assert gate2["review_reconciliation"]["assurance_classification"] == "reviewer_disagreement"
        assert gate2["review_reconciliation"]["blocks_ordinary_approval"] is True
        assert gate2["complete_requirement_assurance"] is False
        assert gate2["report"]["recommendation_label"] == "additional_review_required"
        assert gate2["allowed_actions"] == [
            "submit_final_decision:reject",
            "submit_final_decision:needs_review",
        ]

        # Ordinary approval is blocked with HTTP 409
        blocked = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Attempting approval despite material disagreement.",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "INDEPENDENT_REVIEW_DISAGREEMENT"

        # Escalation to needs_review succeeds
        escalated = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "needs_review",
                "reviewer": "marsadh",
                "comments": "Escalated due to material unverified requirement concern raised by A6.",
            },
        )
        assert escalated.status_code == 200
        assert escalated.json()["state"] == WorkflowState.NEEDS_REVIEW.value


def test_offline_demonstration_case_c_verification_fails_but_reviewer_supports(
    tmp_path: Path,
) -> None:
    """Demo Case C (Req 6): Verification fails, but reviewer incorrectly recommends support.

    Expected:
    - Deterministic failed verification / confirmed regression ALWAYS takes precedence.
    - Reviewer's support is recorded as an unsubstantiated conflicting claim.
    - Ordinary approval remains blocked (HTTP 409).
    """
    repos_root = tmp_path / "repos"
    regressive_repo = repos_root / "demo_c_pos"
    _create_mini_pos_repo(regressive_repo, working_billing=REGRESSIVE_BILLING)

    review_fixture = DeterministicIndependentReviewerFixture(mode="false_support")
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"demo-c-pos": regressive_repo},
        db_name="demo_c.sqlite3",
    )
    app = create_app(
        cfg,
        review_offline_fixture_provider=review_fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "demo-c-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.VERIFY_EXISTING_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
                "notes": "Approve scenarios for Demo Case C.",
            },
        )
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        gate2 = client.get(f"/api/runs/{run_id}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2["verification"]["verification_passed"] is False
        assert gate2["baseline_comparison"]["summary"]["has_confirmed_regression"] is True
        assert gate2["reviewer_recommendation"] == ReviewRecommendation.SUPPORT.value
        assert gate2["review_reconciliation"]["reconciliation_status"] == "reconciled_deterministic_block"
        assert (
            gate2["review_reconciliation"]["assurance_classification"]
            == "deterministic_verification_blocked"
        )
        assert gate2["review_reconciliation"]["blocks_ordinary_approval"] is True
        assert gate2["complete_requirement_assurance"] is False
        assert len(gate2["review_reconciliation"]["unsubstantiated_claims"]) >= 1
        assert len(gate2["material_disagreements"]) >= 1
        assert gate2["report"]["recommendation_label"] == "verification_failed"
        assert "submit_final_decision:approve" not in gate2["allowed_actions"]

        blocked = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Attempting approval based on false AI support.",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "VERIFICATION_APPROVAL_BLOCKED"


def test_reviewer_rejection_uncertainty_and_live_complete_assurance(
    tmp_path: Path,
) -> None:
    """Reqs 5, 7, 9: Reviewer rejection despite passing tests, uncertainty, and live complete assurance."""
    repos_root = tmp_path / "repos"
    good_repo = repos_root / "good_pos"
    _create_mini_pos_repo(good_repo, working_billing=CORRECT_BILLING)

    # 7. Reviewer rejects despite passing tests
    cfg_rej = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"good-pos": good_repo},
        db_name="rej.sqlite3",
    )
    app_rej = create_app(
        cfg_rej,
        review_offline_fixture_provider=DeterministicIndependentReviewerFixture(mode="reject"),
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app_rej) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "good-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app_rej.state.executor.wait_for_run(rid, timeout=60.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "qa",
            },
        )
        app_rej.state.executor.wait_for_run(rid, timeout=60.0)
        g2 = client.get(f"/api/runs/{rid}").json()
        assert g2["verification"]["verification_passed"] is True
        assert g2["reviewer_recommendation"] == ReviewRecommendation.REJECT.value
        assert g2["review_reconciliation"]["blocks_ordinary_approval"] is True
        assert g2["allowed_actions"] == [
            "submit_final_decision:reject",
            "submit_final_decision:needs_review",
        ]
        res_409 = client.post(
            f"/api/runs/{rid}/decision",
            json={"decision": "approve", "reviewer": "qa", "comments": "try"},
        )
        assert res_409.status_code == 409
        assert res_409.json()["code"] == "INDEPENDENT_REVIEW_REJECTED"

    # 9. Reviewer uncertainty (needs_review, non-material) with require_independent_review=True
    cfg_unc = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"good-pos": good_repo},
        db_name="unc.sqlite3",
        require_independent_review=True,
    )
    app_unc = create_app(
        cfg_unc,
        review_offline_fixture_provider=DeterministicIndependentReviewerFixture(mode="uncertain"),
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app_unc) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "good-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app_unc.state.executor.wait_for_run(rid, timeout=60.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "qa",
            },
        )
        app_unc.state.executor.wait_for_run(rid, timeout=60.0)
        g2 = client.get(f"/api/runs/{rid}").json()
        assert g2["reviewer_recommendation"] == ReviewRecommendation.NEEDS_REVIEW.value
        assert g2["report"]["recommendation_label"] == "additional_review_required"
        assert g2["complete_requirement_assurance"] is False
        assert g2["review_reconciliation"]["blocks_ordinary_approval"] is True
        assert g2["review_reconciliation"]["blocking_error_code"] == "INDEPENDENT_REVIEW_NEEDS_REVIEW"

    # 5 (live-shaped): Complete requirement assurance becomes True ONLY when ALL stages are
    # live non-mock + baseline regression safety established + live A6 supports with verified refs.
    def _live_review_provider(r: IndependentReviewRequest) -> IndependentReviewResult:
        return IndependentReviewResult(
            status="completed",
            availability=ReviewAvailabilityStatus.COMPLETED,
            recommendation=ReviewRecommendation.SUPPORT,
            reviewed_run_id=r.run_id,
            reviewed_candidate_proposal_id=r.candidate_proposal_id,
            reviewed_candidate_patch_sha256=r.candidate_patch_sha256,
            reviewed_baseline_snapshot_sha256=r.baseline_source_snapshot_sha256,
            reviewed_modified_snapshot_sha256=r.modified_source_snapshot_sha256,
            summary="Live A6 supports verified candidate with full artifact traceability.",
            findings=[],
            evidence_references=[
                ReviewEvidenceReference(
                    reference_type="criterion",
                    reference_id="AC-001",
                    description="Verified premium discount.",
                ),
                ReviewEvidenceReference(
                    reference_type="file",
                    reference_id=r.changed_files[0],
                    description="Verified diff.",
                ),
            ],
            model_metadata=ReviewModelMetadata(
                provider="injected_live_test",
                model_name="simulated-live-reviewer",
                model_label="injected:simulated-live-reviewer",
                execution_mode="live_llm",
                is_deterministic_fixture=False,
                is_mock_output=False,
            ),
            is_deterministic_fixture=False,
            is_mock_output=False,
        )

    from project.marsadh_orchestrator import deterministic_offline_requirement_analyzer

    cfg_live = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"good-pos": good_repo},
        db_name="live_shaped.sqlite3",
    )
    app_live = create_app(
        cfg_live,
        requirement_live_callable=deterministic_offline_requirement_analyzer,
        requirement_live_label="injected:live-a1",
        live_model_callable=_behavioral_mini_pos_m03_stub,
        live_model_label="injected:live-m03",
        review_production_provider=_live_review_provider,
    )
    with TestClient(app_live) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "good-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "execution_mode": ExecutionMode.LIVE_LLM.value,
            },
        ).json()["run_id"]
        app_live.state.executor.wait_for_run(rid, timeout=60.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "qa",
            },
        )
        app_live.state.executor.wait_for_run(rid, timeout=60.0)
        g2 = client.get(f"/api/runs/{rid}").json()
        assert g2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert g2["review_availability"] == ReviewAvailabilityStatus.COMPLETED.value
        assert (
            g2["review_reconciliation"]["assurance_classification"]
            == "fully_supported_requirement_assurance"
        )
        assert g2["complete_requirement_assurance"] is True
        assert g2["report"]["complete_requirement_assurance"] is True


# ---------------------------------------------------------------------------
# Tests 12, 13, 14, 19, 20: Persistence, Legacy Compat, Restart Recovery, Concurrency, OpenAPI
# ---------------------------------------------------------------------------


def test_persistence_legacy_compat_restart_recovery_secret_redaction_and_openapi(
    tmp_path: Path,
) -> None:
    """Reqs 12, 13, 14, 20: Persistence, legacy deserialization, restart recovery, redaction, OpenAPI."""
    repos_root = tmp_path / "repos"
    good_repo = repos_root / "good_pos"
    _create_mini_pos_repo(good_repo, working_billing=CORRECT_BILLING)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"good-pos": good_repo},
        db_name="persist_p09.sqlite3",
    )
    store = RunStore(cfg.database_path)

    # 13. Pre-P0-9 historical persisted record without review fields deserializes cleanly
    legacy_payload = {
        "run_id": "run_legacy_pre_p09",
        "state": WorkflowState.AWAITING_FINAL_APPROVAL.value,
        "requirements": "Legacy change request",
        "workflow_mode": WorkflowMode.VERIFY_EXISTING_CHANGE.value,
        "execution_mode": ExecutionMode.OFFLINE_TEST.value,
        "repository": {
            "repository_id": "good-pos",
            "repository_path": str(good_repo),
            "source_path": "mini_pos",
            "base_ref": "HEAD",
        },
        "verification": {
            "execution_status": "completed",
            "verification_outcome": "passed",
            "verification_passed": True,
            "exit_code": 0,
            "total_tests": 1,
            "passed": 1,
            "failed": 0,
        },
    }
    legacy_rec = WorkflowRunRecord.model_validate(legacy_payload)
    store.save_run(legacy_rec)
    reloaded_legacy = store.get_run("run_legacy_pre_p09")
    assert reloaded_legacy is not None
    assert reloaded_legacy.independent_review is None
    assert reloaded_legacy.review_reconciliation is None
    assert evaluate_approval_policy(reloaded_legacy).ordinary_approval_allowed is True

    # 14. Restart recovery on RUNNING_INDEPENDENT_REVIEW & RECONCILING_REVIEW_EVIDENCE
    # without replaying A6 and WITHOUT overwriting already-completed repair_termination_reason!
    call_counter = {"a6": 0}

    def _counting_reviewer(r: IndependentReviewRequest) -> IndependentReviewResult:
        call_counter["a6"] += 1
        return DeterministicIndependentReviewerFixture(mode="support").review_change(r)

    interrupted_rev = WorkflowRunRecord(
        run_id="run_interrupted_in_review",
        state=WorkflowState.RUNNING_INDEPENDENT_REVIEW,
        requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        repository=RepositoryInfo(
            repository_id="good-pos",
            repository_path=str(good_repo),
            source_path="mini_pos",
            base_ref="HEAD",
        ),
        repair_attempts_consumed=1,
        repair_termination_reason=RepairTerminationReason.REPAIR_SUCCEEDED,
    )
    interrupted_rec = WorkflowRunRecord(
        run_id="run_interrupted_in_reconcile",
        state=WorkflowState.RECONCILING_REVIEW_EVIDENCE,
        requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        repository=RepositoryInfo(
            repository_id="good-pos",
            repository_path=str(good_repo),
            source_path="mini_pos",
            base_ref="HEAD",
        ),
    )
    terminal_approved = WorkflowRunRecord(
        run_id="run_terminal_approved",
        state=WorkflowState.APPROVED,
        requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        repository=RepositoryInfo(
            repository_id="good-pos",
            repository_path=str(good_repo),
            source_path="mini_pos",
            base_ref="HEAD",
        ),
    )
    store.save_run(interrupted_rev)
    store.save_run(interrupted_rec)
    store.save_run(terminal_approved)

    svc = OrchestratorService(
        config=cfg,
        store=store,
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        review_adapter=IndependentReviewAdapter(
            config=cfg, offline_fixture_provider=_counting_reviewer
        ),
    )
    recovered = svc.recover_interrupted_runs()
    recovered_ids = {r.run_id for r in recovered}
    assert "run_interrupted_in_review" in recovered_ids
    assert "run_interrupted_in_reconcile" in recovered_ids
    assert "run_terminal_approved" not in recovered_ids
    assert call_counter["a6"] == 0  # Never replayed!

    after_rev = store.get_run("run_interrupted_in_review")
    assert after_rev is not None
    assert after_rev.state == WorkflowState.FAILED
    assert after_rev.error is not None
    assert after_rev.error.code == "PROCESS_INTERRUPTED"
    # Confirmed Defect #9 regression check: completed repair reason preserved!
    assert after_rev.repair_termination_reason == RepairTerminationReason.REPAIR_SUCCEEDED
    assert after_rev.repair_attempts_consumed == 1

    # Terminal run cannot resume
    with pytest.raises(InvalidStateTransitionError):
        svc.transition_state(
            "run_terminal_approved",
            WorkflowState.APPROVED,
            WorkflowState.RUNNING_INDEPENDENT_REVIEW,
        )

    # 12 + Defect #10: Secret redaction in report.verification, independent_review, and stripping raw excerpts
    secret_val = "gsk_" + "LeakyReportSecret1234567890ABCD"
    noisy_review_fixture = DeterministicIndependentReviewerFixture(
        mode="material_disagreement",
        custom_concern=f"Exposed token {secret_val} found near {tmp_path}",
    )
    app = create_app(
        cfg,
        review_offline_fixture_provider=noisy_review_fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app) as client:
        # 20. OpenAPI parity & /health capability flags
        health = client.get("/health").json()
        assert health["independent_reviewer_configured"] is False
        assert health["require_independent_review"] is False
        assert any("unauthenticated" in lim.lower() for lim in health["limitations"])

        live_openapi = app.openapi()
        committed_openapi = json.loads(
            (REPO_ROOT / "docs" / "m02" / "openapi.json").read_text(encoding="utf-8")
        )
        assert live_openapi == committed_openapi

        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "good-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(rid, timeout=60.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "qa",
            },
        )
        app.state.executor.wait_for_run(rid, timeout=60.0)

        detail_res = client.get(f"/api/runs/{rid}")
        assert detail_res.status_code == 200
        assert secret_val not in detail_res.text
        assert str(tmp_path) not in detail_res.text

        # Confirm SQLite persisted record stripped raw relevant_source_excerpts and redacted secret
        raw_reloaded = RunStore(cfg.database_path).get_run(rid)
        assert raw_reloaded is not None
        assert raw_reloaded.independent_review_request is not None
        assert raw_reloaded.independent_review_request.relevant_source_excerpts == {}
        assert secret_val not in raw_reloaded.model_dump_json()

        # Confirmed Defect #7 regression check: secrets inside report.verification are redacted at save_run
        assert raw_reloaded.report is not None and raw_reloaded.report.verification is not None
        raw_reloaded.report.verification.stdout_excerpt = f"leaked key {secret_val}"
        RunStore(cfg.database_path).save_run(raw_reloaded)
        reloaded_after_report_secret = RunStore(cfg.database_path).get_run(rid)
        assert reloaded_after_report_secret is not None
        assert secret_val not in reloaded_after_report_secret.model_dump_json()
        assert (
            "[REDACTED_GROQ_KEY]"
            in reloaded_after_report_secret.report.verification.stdout_excerpt
        )


# ---------------------------------------------------------------------------
# Tests 17, 18, 19: Candidate Integrity, Audit Reliability Fixes & Concurrency
# ---------------------------------------------------------------------------


def test_candidate_verification_evidence_integrity_and_audit_fixes(
    tmp_path: Path,
) -> None:
    """Reqs 17 & 18: Candidate integrity, test-manipulation protection, repair attribution, workspace safety."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "audit_pos"
    _create_mini_pos_repo(
        repo,
        existing_test_code="def test_existing_ok():\n    assert True\n",
    )
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"audit-pos": repo},
        db_name="audit.sqlite3",
    )

    # 17a. Earlier passing verification snapshot cannot authorize a different candidate snapshot
    snap_a = "1" * 64
    snap_b = "2" * 64
    mismatched_run = WorkflowRunRecord(
        run_id="run_stale_ver",
        state=WorkflowState.AWAITING_FINAL_APPROVAL,
        requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        repository=RepositoryInfo(
            repository_id="audit-pos",
            repository_path=str(repo),
            source_path="mini_pos",
            base_ref="HEAD",
        ),
        source_snapshot_sha256=snap_b,  # Current unverified candidate snapshot!
        patch_proposal_sha256="a" * 64,
        patch_application=PatchApplicationResult(
            applied=True,
            status="applied",
            patch_sha256="a" * 64,
            original_source_snapshot_sha256="0" * 64,
            original_git_diff_sha256="0" * 64,
            resulting_source_snapshot_sha256=snap_b,
            resulting_git_diff_sha256="d" * 64,
            applied_files=["mini_pos/billing.py"],
        ),
        modified_verification=ModifiedVerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.PASSED,
            verification_passed=True,
            source_snapshot_sha256=snap_a,  # Belongs to earlier attempt!
            exit_code=0,
            total_tests=1,
            passed=1,
            failed=0,
        ),
        verification=VerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.PASSED,
            verification_passed=True,
            exit_code=0,
            total_tests=1,
            passed=1,
            failed=0,
        ),
    )
    integrity = verify_candidate_evidence_integrity(mismatched_run)
    assert integrity.valid is False
    assert integrity.primary_code == "CANDIDATE_VERIFICATION_MISMATCH"
    decision = evaluate_approval_policy(mismatched_run)
    assert decision.ordinary_approval_allowed is False
    assert decision.blocking_code == "CANDIDATE_VERIFICATION_MISMATCH"

    # 18a. PatchValidator blocks pytest.ini, conftest.py, sitecustomize.py, .pth import hooks,
    # and test files even on INITIAL patches!
    wm = WorkspaceManager(cfg)
    ws_ctx = wm.prepare_workspace("run_val_01", repo, source_path=".", base_ref="HEAD")
    validator = PatchValidator(cfg)
    for forbidden_file in (
        "pytest.ini",
        "mini_pos/conftest.py",
        "mini_pos/sitecustomize.py",
        "mini_pos/inject.pth",
        "tests/test_existing_regression.py",
    ):
        diff_text = (
            f"diff --git a/{forbidden_file} b/{forbidden_file}\n"
            f"new file mode 100644\n"
            f"--- /dev/null\n"
            f"+++ b/{forbidden_file}\n"
            f"@@ -0,0 +1,1 @@\n"
            f"+# injected\n"
        )
        prop = PatchProposal(
            proposal_id="PATCH-001",
            unified_diff=diff_text,
            patch_sha256=compute_patch_sha256(diff_text),
            affected_files=[
                PatchFileChange(path=forbidden_file, change_type="add", added_lines=1)
            ],
            change_rationale="Test manipulation attempt.",
        )
        res = validator.validate(
            workspace_repo_path=ws_ctx.workspace_repo_path,
            proposal=prop,
            source_path=".",
            base_ref=ws_ctx.effective_base_ref,
            forbid_test_modifications=True,
        )
        assert res.valid is False, f"Expected '{forbidden_file}' patch to be rejected"
        assert any(
            v.code in {"PATCH_TEST_MODIFICATION_FORBIDDEN", "PATCH_PROTECTED_PATH"}
            for v in res.violations
        )

    # 18b. WorkspaceManager.cleanup_workspace rejects outside paths
    outside_dir = tmp_path / "outside_protected"
    outside_dir.mkdir()
    with pytest.raises(WorkspaceSecurityError):
        wm.cleanup_workspace(outside_dir)
    assert outside_dir.exists()
    assert wm.cleanup_workspace(ws_ctx.workspace_repo_path) is True

    # 18c. RepairEligibilityEvaluator rejects persistent acceptance failures caused by
    # unreliable generated-test assumptions (identical TypeError on baseline & modified)
    # and rejects persistent acceptance failures lacking diagnostic attribution.
    applied_ok = PatchApplicationResult(
        applied=True,
        status="applied",
        patch_sha256="a" * 64,
        original_source_snapshot_sha256="0" * 64,
        original_git_diff_sha256="0" * 64,
        resulting_source_snapshot_sha256="1" * 64,
        resulting_git_diff_sha256="2" * 64,
        applied_files=["mini_pos/billing.py"],
    )
    policy = RepairPolicy(enabled=True, max_repair_attempts=2)

    base_type_err = BaselineVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        exit_code=1,
        total_tests=1,
        passed=0,
        failed=1,
        failures=[
            FailureDiagnostic(
                test_name="test_generated_001.py::test_bad_arity",
                status="failed",
                evidence="TypeError: checkout() missing 1 required positional argument: 'customer_type'",
                error_type="TypeError",
            )
        ],
    )
    mod_type_err = ModifiedVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        exit_code=1,
        total_tests=1,
        passed=0,
        failed=1,
        failures=[
            FailureDiagnostic(
                test_name="test_generated_001.py::test_bad_arity",
                status="failed",
                evidence="TypeError: checkout() missing 1 required positional argument: 'customer_type'",
                error_type="TypeError",
            )
        ],
    )
    comp_persistent = BaselineComparisonEvidence(
        status=VerificationComparisonStatus.COMPLETED,
        approved_test_hashes_verified=True,
        test_comparisons=[
            TestComparisonResult(
                test_identity="SCN-001",
                scenario_id="SCN-001",
                relative_path=".changeguard_generated_tests/test_generated_001.py",
                suite_kind=TestSuiteKind.GENERATED_ACCEPTANCE,
                test_purpose=TestPurpose.NEW_REQUIREMENT_ACCEPTANCE,
                criterion_ids=["AC-001"],
                test_hashes_match=True,
                baseline_outcome=IndividualTestOutcome.FAILED,
                modified_outcome=IndividualTestOutcome.FAILED,
                classification=TestComparisonClassification.PERSISTENT_FAILURE,
                comparable=True,
            )
        ],
        summary=RegressionComparisonSummary(
            comparison_status=VerificationComparisonStatus.COMPLETED,
            total_compared=1,
            comparable_count=1,
            persistent_failure_count=1,
            persistent_failures=["SCN-001"],
        ),
    )
    elig_type_err = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied_ok,
        requirement_analysis=None,
        baseline=base_type_err,
        modified=mod_type_err,
        comparison=comp_persistent,
        current_attempt_number=1,
        repair_attempts_consumed=0,
    )
    assert elig_type_err.eligible is False
    assert elig_type_err.category == RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR

    # Missing failure diagnostics -> UNATTRIBUTABLE_FAILURE (no unjustified retry)
    mod_no_diag = ModifiedVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        exit_code=1,
        total_tests=1,
        passed=0,
        failed=1,
        failures=[],
    )
    elig_no_diag = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied_ok,
        requirement_analysis=None,
        baseline=base_type_err,
        modified=mod_no_diag,
        comparison=comp_persistent,
        current_attempt_number=1,
        repair_attempts_consumed=0,
    )
    assert elig_no_diag.eligible is False
    assert elig_no_diag.category == RepairEligibilityCategory.UNATTRIBUTABLE_FAILURE

    # Genuine AssertionError with explicit criterion_ids -> ELIGIBLE_ACCEPTANCE_FAILURE
    mod_assert_err = ModifiedVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        exit_code=1,
        total_tests=1,
        passed=0,
        failed=1,
        failures=[
            FailureDiagnostic(
                test_name="test_generated_001.py::test_premium_discount",
                status="failed",
                evidence="AssertionError: assert 190.0 == 180.0",
                error_type="AssertionError",
            )
        ],
    )
    elig_real = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied_ok,
        requirement_analysis=None,
        baseline=base_type_err,
        modified=mod_assert_err,
        comparison=comp_persistent,
        current_attempt_number=1,
        repair_attempts_consumed=0,
    )
    assert elig_real.eligible is True
    assert elig_real.category == RepairEligibilityCategory.ELIGIBLE_ACCEPTANCE_FAILURE

    # 17b. Stale independent review identity on a persisted run is blocked by reconciliation & policy
    stale_review_run = WorkflowRunRecord(
        run_id="run_stale_a6_policy",
        state=WorkflowState.AWAITING_FINAL_APPROVAL,
        requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        repository=RepositoryInfo(
            repository_id="audit-pos",
            repository_path=str(repo),
            source_path="mini_pos",
            base_ref="HEAD",
        ),
        source_snapshot_sha256=snap_b,
        patch_proposal_sha256="a" * 64,
        patch_application=PatchApplicationResult(
            applied=True,
            status="applied",
            patch_sha256="a" * 64,
            original_source_snapshot_sha256="0" * 64,
            original_git_diff_sha256="0" * 64,
            resulting_source_snapshot_sha256=snap_b,
            resulting_git_diff_sha256="d" * 64,
            applied_files=["mini_pos/billing.py"],
        ),
        modified_verification=ModifiedVerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.PASSED,
            verification_passed=True,
            source_snapshot_sha256=snap_b,
            git_diff_sha256="d" * 64,
            exit_code=0,
            total_tests=1,
            passed=1,
            failed=0,
        ),
        verification=VerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.PASSED,
            verification_passed=True,
            exit_code=0,
            total_tests=1,
            passed=1,
            failed=0,
        ),
        independent_review=IndependentReviewResult(
            status="completed",
            availability=ReviewAvailabilityStatus.COMPLETED_OFFLINE_FIXTURE,
            recommendation=ReviewRecommendation.SUPPORT,
            reviewed_run_id="run_stale_a6_policy",
            reviewed_candidate_patch_sha256="f" * 64,  # Stale superseded patch hash!
            reviewed_modified_snapshot_sha256=snap_a,  # Stale snapshot!
            summary="Stale support for old attempt.",
            model_metadata=ReviewModelMetadata(
                provider="offline_fixture",
                model_label="stale_fixture",
                execution_mode="offline_test",
                is_deterministic_fixture=True,
                is_mock_output=True,
            ),
            is_deterministic_fixture=True,
            is_mock_output=True,
        ),
    )
    stale_policy = evaluate_approval_policy(stale_review_run)
    assert stale_policy.ordinary_approval_allowed is False
    assert stale_policy.blocking_code == "STALE_REVIEW_CANDIDATE"
    stale_comp = BaselineComparisonEvidence(
        status=VerificationComparisonStatus.COMPLETED,
        modified_source_snapshot_sha256=snap_b,
        approved_test_hashes_verified=True,
    )
    stale_rec, _ = EvidenceReconciliationEngine.reconcile(
        run=stale_review_run,
        verification=stale_review_run.verification,
        comparison=stale_comp,
        criterion_traceability=[],
        traceability_meta={},
        review_request=None,
        review_result=stale_review_run.independent_review,
    )
    assert stale_rec.review_candidate_identity_verified is False
    assert stale_rec.blocks_ordinary_approval is True
    assert stale_rec.blocking_error_code == "STALE_REVIEW_CANDIDATE"

    # 18d. Post-execution test file tampering detection inside execute_snapshot_verification
    import hashlib
    from project.marsadh_orchestrator.baseline_verification import (
        execute_snapshot_verification,
    )
    from project.schemas.agents import ApprovedTestSpec

    tamper_ws = wm.prepare_workspace("run_tamper_01", repo, source_path=".", base_ref="HEAD")
    test_dir = Path(tamper_ws.workspace_repo_path) / ".changeguard_generated_tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    self_tampering_test = test_dir / "test_self_modify.py"
    self_tampering_test.write_text(
        "from pathlib import Path\n"
        "def test_rewrites_itself():\n"
        "    Path(__file__).write_text('def test_rewrites_itself(): assert True\\n', encoding='utf-8')\n"
        "    assert True\n",
        encoding="utf-8",
    )
    initial_hash = hashlib.sha256(self_tampering_test.read_bytes()).hexdigest()
    spec = ApprovedTestSpec(
        scenario_id="SCN-TAMPER",
        relative_path=".changeguard_generated_tests/test_self_modify.py",
        expected_sha256=initial_hash,
    )
    _snap_res, _agg_res, hard_err = execute_snapshot_verification(
        VerificationAdapter(cfg),
        cfg,
        snapshot_role="modified",
        workspace_path=tamper_ws.workspace_repo_path,
        source_path=".",
        selected_test_ids=["SCN-TAMPER"],
        approved_specs={"SCN-TAMPER": spec},
        test_purposes={"SCN-TAMPER": TestPurpose.NEW_REQUIREMENT_ACCEPTANCE},
        criterion_ids_by_test={"SCN-TAMPER": []},
        is_trusted_workspace=True,
        include_existing_regression_suite=False,
    )
    assert hard_err is not None
    assert hard_err.code == "TEST_CONTENT_MODIFIED"
    wm.cleanup_workspace(tamper_ws.workspace_repo_path)


def test_concurrent_gate1_and_gate2_review_submission_protection(
    tmp_path: Path,
) -> None:
    """Req 19: Concurrent Gate 1 & Gate 2 review submissions allow exactly one winner."""
    repos_root = tmp_path / "repos"
    good_repo = repos_root / "conc_pos"
    _create_mini_pos_repo(good_repo, working_billing=CORRECT_BILLING)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"conc-pos": good_repo},
        db_name="concurrency.sqlite3",
    )
    svc = OrchestratorService(
        config=cfg,
        store=RunStore(cfg.database_path),
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        review_adapter=IndependentReviewAdapter(
            config=cfg,
            offline_fixture_provider=DeterministicIndependentReviewerFixture(mode="support"),
        ),
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )

    run = svc.start_run(
        CreateRunRequest(
            repository_id="conc-pos",
            source_path="mini_pos",
            base_ref="HEAD",
            requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        )
    )
    assert run.state == WorkflowState.AWAITING_TEST_REVIEW

    # Race 4 concurrent Gate 1 test-review approvals
    def _gate1_worker(idx: int) -> tuple[str, Any]:
        try:
            updated, specs = svc.accept_test_review(
                run.run_id,
                TestReviewRequest(
                    approved=True,
                    selected_test_ids=["SCN-001", "SCN-002", "SCN-003"],
                    reviewer=f"reviewer_{idx}",
                ),
            )
            return ("ok", (updated, specs))
        except ReviewConflictError as exc:
            return ("conflict", exc.code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        g1_results = list(pool.map(_gate1_worker, range(4)))

    winners_g1 = [r for r in g1_results if r[0] == "ok"]
    conflicts_g1 = [r for r in g1_results if r[0] == "conflict"]
    assert len(winners_g1) == 1
    assert len(conflicts_g1) == 3

    # Execute verification + independent review + reconciliation to Gate 2
    _updated_g1, approved_specs = winners_g1[0][1]
    at_gate2 = svc.execute_verification_and_report(run.run_id, approved_specs)
    assert at_gate2.state == WorkflowState.AWAITING_FINAL_APPROVAL

    # Race 4 concurrent Gate 2 final decisions
    def _gate2_worker(idx: int) -> tuple[str, Any]:
        try:
            res = svc.submit_final_decision(
                run.run_id,
                HumanDecisionRequest(
                    decision=HumanDecision.APPROVE,
                    reviewer=f"approver_{idx}",
                    comments="Concurrent approval attempt.",
                ),
            )
            return ("ok", res.state)
        except ReviewConflictError as exc:
            return ("conflict", exc.code)

    with ThreadPoolExecutor(max_workers=4) as pool:
        g2_results = list(pool.map(_gate2_worker, range(4)))

    winners_g2 = [r for r in g2_results if r[0] == "ok"]
    conflicts_g2 = [r for r in g2_results if r[0] == "conflict"]
    assert len(winners_g2) == 1
    assert len(conflicts_g2) == 3
    assert svc.get_run(run.run_id).state == WorkflowState.APPROVED
