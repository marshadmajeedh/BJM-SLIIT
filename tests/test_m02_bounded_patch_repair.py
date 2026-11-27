"""Comprehensive offline test suite & E2E demonstrations for P0-8 Bounded Automated Patch Repair.

Covers all 24 required P0-8 verification areas and Phase 11 Offline End-to-End
Demonstrations A, B, and C using real local ``pytest`` execution against isolated
``mini_pos`` Git fixtures (zero external LLM calls):

1.  Repair disabled (policy & per-run override).
2.  Successful initial patch requiring no repair.
3.  Demonstration A: Initial regressive patch followed by successful bounded repair.
4.  Demonstration B: Two failed distinct repair attempts followed by budget exhaustion.
5.  Invalid repair proposal consuming the configured repair budget without reset.
6.  Production repair provider unavailable during repair.
7.  Duplicate patch SHA-256 detection stopping redundant retries.
8.  Unchanged failure signature (no-progress) termination.
9.  Generated-test defects (syntax, fixture, test NameError) not triggering application repair.
10. Demonstration C: Generated-test import/collection errors not triggering application repair.
11. Incomplete baseline evidence / aggregate pass count < 100% preventing unjustified repair.
12. Mandatory unresolved business ambiguity blocking repair.
13. Approved test SHA-256 hash preservation across all attempts & baseline reuse validation.
14. Attempted modification of approved generated tests or existing regression suite rejected.
15. Original repository byte-for-byte & Git integrity across repair runs.
16. Safe clean workspace restoration from immutable original snapshot & scope invalidation check.
17. Prior attempt evidence preservation (append-only attempt history).
18. Repair state persistence & pre-P0-8 legacy record backward compatibility.
19. Restart recovery without automatic replay & persisted budget preservation.
20. Final human approval enforcement (never auto-approved; blocked on unresolved failures).
21. API compatibility, zero host path exposure, and committed OpenAPI parity.
22. Secret redaction across repair history, SQLite storage, logs, and API DTOs.
23. Live mode rejecting deterministic fixture fallback during repair.
24. Existing ``verify_existing_change`` workflow compatibility (never triggers automatic repair).
"""

from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from project.main import create_app
from project.marsadh_orchestrator import (
    DeterministicPersistentRegressionPatchFixture,
    DeterministicPremiumDiscountPatchFixture,
    DeterministicRegressiveThenFixedPatchFixture,
    OrchestratorConfig,
    OrchestratorService,
    PatchGenerationAdapter,
    RepairEligibilityEvaluator,
    RunStore,
    WorkspaceManager,
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
    ExecutionMode,
    FailureDiagnostic,
    HumanDecision,
    HumanDecisionRequest,
    IndividualTestOutcome,
    ModifiedVerificationResult,
    PatchApplicationResult,
    PatchAttemptRecord,
    PatchFileChange,
    PatchGenerationRequest,
    PatchGenerationResult,
    PatchModelMetadata,
    PatchProposal,
    RegressionComparisonSummary,
    RepairAttemptHistory,
    RepairEligibilityCategory,
    RepairEligibilityResult,
    RepairPolicy,
    RepairTerminationReason,
    RepositoryInfo,
    RequirementAmbiguity,
    RequirementAnalysisResult,
    AmbiguitySeverity,
    TestComparisonClassification,
    TestComparisonResult,
    TestExecutionEvidence,
    TestPurpose,
    TestReviewRequest,
    TestSuiteKind,
    VerificationComparisonStatus,
    VerificationOutcome,
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
        pytest.skip("Git is required for P0-8 bounded repair tests")
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
    clean: bool = True,
    regressive_working_tree: bool = False,
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
    _git(repo, "config", "user.name", "ChangeGuard P0-8")
    _git(repo, "config", "user.email", "p08@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline clean mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    if not clean and regressive_working_tree:
        (app_dir / "billing.py").write_text(REGRESSIVE_BILLING, encoding="utf-8")
    return base_commit


def _make_config(
    tmp_path: Path,
    *,
    repos_root: Path,
    configured: dict[str, Path],
    db_name: str = "p08.sqlite3",
    auto_repair_enabled: bool = True,
    max_repair_attempts: int = 2,
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
        environment="test",
    )


def _behavioral_mini_pos_m03_stub(_prompt: str) -> str:
    """Deterministic offline M03 test suite (explicitly labeled as offline fixture)."""
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
                    "title": "Premium customer checkout receives 10% discount (Acceptance)",
                    "rationale": "Offline fixture verifying AC-001.",
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
                    "title": "Regular customer checkout remains unchanged (Preservation)",
                    "rationale": "Offline fixture verifying AC-002.",
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
                    "title": "Dependent refund, dashboard, and report modules preserve regular customers",
                    "rationale": "Offline fixture verifying AC-003.",
                    "assumptions": "Offline deterministic behavioral test fixture.",
                    "criterion_ids": ["AC-003"],
                    "test_purpose": TestPurpose.EXISTING_BEHAVIOR_PRESERVATION.value,
                    "test_code": (
                        header
                        + "from dashboard import display_refund\n"
                        + "from reports import generate_sales_report\n\n"
                        + "def test_downstream_regular_preserved():\n"
                        + "    assert display_refund(100.0, 'regular') == {'refund_amount': pytest.approx(100.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'regular') == {'total_sales': pytest.approx(200.0)}\n"
                    ),
                },
                {
                    "title": "Dependent refund and sales report reflect 10% premium discount",
                    "rationale": "Offline fixture verifying AC-001 downstream.",
                    "assumptions": "Offline deterministic behavioral test fixture.",
                    "criterion_ids": ["AC-001"],
                    "test_purpose": TestPurpose.NEW_REQUIREMENT_ACCEPTANCE.value,
                    "test_code": (
                        header
                        + "from dashboard import display_refund\n"
                        + "from reports import generate_sales_report\n\n"
                        + "def test_downstream_premium_discount():\n"
                        + "    assert display_refund(100.0, 'premium') == {'refund_amount': pytest.approx(90.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'premium') == {'total_sales': pytest.approx(180.0)}\n"
                    ),
                },
            ]
        }
    )


def _make_diff_for_file(rel_path: str, old_text: str, new_text: str) -> str:
    eol = "\r\n" if "\r\n" in old_text else "\n"
    diff_body = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
            lineterm=eol,
            n=3,
        )
    )
    diff = f"diff --git a/{rel_path} b/{rel_path}{eol}{diff_body}"
    return diff if diff.endswith("\n") else diff + eol


def _make_fixture_result(
    request: PatchGenerationRequest,
    *,
    rel_path: str,
    unified_diff: str,
    rationale: str,
    model_label: str = "deterministic_offline_custom_fixture",
) -> PatchGenerationResult:
    proposal = PatchProposal(
        proposal_id=f"PATCH-{request.attempt_number:03d}",
        unified_diff=unified_diff,
        patch_sha256=compute_patch_sha256(unified_diff),
        affected_files=[
            PatchFileChange(
                path=rel_path,
                change_type="modify",
                related_criterion_ids=[request.acceptance_criteria[0].criterion_id],
            )
        ],
        change_rationale=rationale,
        related_criterion_ids=[request.acceptance_criteria[0].criterion_id],
        notes=["OFFLINE FIXTURE ONLY"],
    )
    return PatchGenerationResult(
        status="proposed",
        attempt_number=request.attempt_number,
        proposal=proposal,
        model_metadata=PatchModelMetadata(
            provider="offline_fixture",
            model_name="custom_fixture",
            model_label=model_label,
            generation_mode="offline_test",
            is_deterministic_fixture=True,
            is_mock_output=True,
        ),
        is_deterministic_fixture=True,
        is_mock_output=True,
    )


# ---------------------------------------------------------------------------
# Schema & Policy Invariant Tests (Phase 2 & 3)
# ---------------------------------------------------------------------------


def test_repair_policy_and_schema_bounds() -> None:
    """Phase 2 & 3: RepairPolicy and RepairAttemptHistory enforce hard bounds and truthful invariants."""
    default_pol = RepairPolicy()
    assert default_pol.enabled is True
    assert default_pol.max_repair_attempts == 2
    assert default_pol.max_total_proposals == 3
    assert default_pol.effective_max_repair_attempts == 2
    assert default_pol.allow_for_verify_existing_change is False

    disabled_pol = RepairPolicy(enabled=False)
    assert disabled_pol.effective_max_repair_attempts == 0
    assert disabled_pol.max_total_proposals == 1

    one_repair = RepairPolicy(enabled=True, max_repair_attempts=1)
    assert one_repair.max_total_proposals == 2

    with pytest.raises(ValidationError):
        RepairPolicy(max_repair_attempts=3)
    with pytest.raises(ValidationError):
        RepairPolicy(max_total_proposals=4)
    with pytest.raises(ValidationError):
        RepairPolicy(allow_for_verify_existing_change=True)  # type: ignore[arg-type]

    # Env config parsing for CHANGEGUARD_AUTO_REPAIR_ENABLED
    cfg_off = OrchestratorConfig.from_env(
        {
            "CHANGEGUARD_AUTO_REPAIR_ENABLED": "false",
            "CHANGEGUARD_MAX_REPAIR_ATTEMPTS": "2",
        }
    )
    assert cfg_off.auto_repair_enabled is False
    assert cfg_off.build_repair_policy().effective_max_repair_attempts == 0

    # PatchAttemptRecord rejects initial attempt claiming repair budget consumption
    with pytest.raises(ValidationError):
        PatchAttemptRecord(
            attempt_number=1,
            attempt_type="initial",
            repair_attempt_index=0,
            original_requirement="req",
            consumed_repair_budget=True,
            outcome="passed",
        )


# ---------------------------------------------------------------------------
# Req 3, 13, 15, 16, 17, 20: Demonstration A — Regressive Initial -> Successful Repair
# ---------------------------------------------------------------------------


def test_demo_a_regressive_initial_patch_repaired_with_preserved_history_and_gate2(
    tmp_path: Path,
) -> None:
    """Demo A & Reqs 3, 13, 15, 16, 17, 20: Initial regressive patch repaired on attempt 2."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_demo_a"
    base_commit = _create_mini_pos_repo(repo, clean=True)
    before_bytes = _snapshot_tree_bytes(repo)

    fixture = DeterministicRegressiveThenFixedPatchFixture()
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"mini-pos-a": repo},
        auto_repair_enabled=True,
        max_repair_attempts=2,
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )

    with TestClient(app) as client:
        # 1. Start clean-repo generate_and_verify_change run
        create_res = client.post(
            "/api/runs",
            json={
                "repository_id": "mini-pos-a",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        )
        assert create_res.status_code == 202
        run_id = create_res.json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        gate1 = client.get(f"/api/runs/{run_id}").json()
        assert gate1["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert gate1["patch_attempt_number"] == 1
        assert len(fixture.received_requests) == 1
        assert fixture.received_requests[0].attempt_type == "initial"
        orig_snap_sha = gate1["original_source_snapshot_sha256"]
        attempt1_patch_sha = gate1["patch_proposal_sha256"]

        # 2. Human approves behavioral test suite at Gate 1
        review_res = client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003", "SCN-004"],
                "reviewer": "marsadh",
                "notes": "Approve premium & regular behavioral tests for Demo A.",
            },
        )
        assert review_res.status_code == 202
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        gate2 = client.get(f"/api/runs/{run_id}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value

        # Verify bounded repair succeeded on attempt 2 (1 repair consumed, 1 remaining)
        assert gate2["repair_enabled"] is True
        assert gate2["max_repair_attempts"] == 2
        assert gate2["repair_attempts_consumed"] == 1
        assert gate2["remaining_repair_budget"] == 1
        assert gate2["patch_attempt_number"] == 2
        assert gate2["repair_termination_reason"] == RepairTerminationReason.REPAIR_SUCCEEDED.value
        assert gate2["final_verification_outcome"] == VerificationOutcome.PASSED.value
        assert gate2["verification"]["verification_passed"] is True
        assert gate2["baseline_comparison"]["status"] == "completed"
        assert gate2["baseline_comparison"]["summary"]["has_confirmed_regression"] is False
        assert gate2["baseline_comparison"]["summary"]["preserved_behavior_count"] == 2
        assert gate2["baseline_comparison"]["summary"]["newly_satisfied_acceptance_count"] == 2

        # Verify A4 repair request received actual failure evidence & immutable original snapshot
        assert len(fixture.received_requests) == 2
        repair_req = fixture.received_requests[1]
        assert repair_req.attempt_number == 2
        assert repair_req.attempt_type == "repair"
        assert repair_req.repair_attempt_number == 1
        assert repair_req.source_snapshot_sha256 == orig_snap_sha
        assert repair_req.previous_patch_sha256 == attempt1_patch_sha
        assert repair_req.previous_proposed_patch is not None
        assert "return total * 0.90" in repair_req.previous_proposed_patch
        assert set(repair_req.failed_criterion_ids) == {"AC-002", "AC-003"}
        assert repair_req.baseline_comparison_evidence is not None
        assert set(repair_req.baseline_comparison_evidence["newly_introduced_failures"]) == {
            "SCN-002",
            "SCN-003",
        }
        # Clean original billing.py (not broken attempt 1) was sent to A4!
        assert "if customer_type" not in repair_req.relevant_source_files["mini_pos/billing.py"]
        assert "return total * 0.90" not in repair_req.relevant_source_files["mini_pos/billing.py"]

        # Verify complete attempt history preserved Attempt 1 AND Attempt 2
        hist = gate2["repair_history"]
        assert hist["total_proposals_attempted"] == 2
        assert hist["repair_attempts_consumed"] == 1
        assert hist["remaining_repair_budget"] == 1
        assert len(hist["attempts"]) == 2

        att1, att2 = hist["attempts"]
        assert att1["attempt_number"] == 1
        assert att1["attempt_type"] == "initial"
        assert att1["outcome"] == "failed_verification"
        assert att1["patch_sha256"] == attempt1_patch_sha
        assert att1["baseline_comparison"]["summary"]["has_confirmed_regression"] is True
        assert set(att1["baseline_comparison"]["summary"]["newly_introduced_failures"]) == {
            "SCN-002",
            "SCN-003",
        }
        assert att1["repair_eligibility"]["eligible"] is True
        assert (
            att1["repair_eligibility"]["category"]
            == RepairEligibilityCategory.ELIGIBLE_APPLICATION_REGRESSION.value
        )
        assert att1["is_deterministic_fixture"] is True

        assert att2["attempt_number"] == 2
        assert att2["attempt_type"] == "repair"
        assert att2["repair_attempt_index"] == 1
        assert att2["previous_patch_sha256"] == attempt1_patch_sha
        assert att2["base_snapshot_sha256"] == orig_snap_sha
        assert att2["patch_sha256"] != attempt1_patch_sha
        assert att2["outcome"] == "passed"
        assert att2["baseline_reused"] is True
        assert att2["consumed_repair_budget"] is True
        assert att2["approved_test_hashes"] == att1["approved_test_hashes"]
        assert att2["approved_test_hashes"] == gate2["test_review"]["approved_test_hashes"]
        assert att2["is_deterministic_fixture"] is True
        assert att2["baseline_comparison"]["summary"]["has_confirmed_regression"] is False

        # Report honesty: offline fixture cannot claim complete_requirement_assurance
        rep = gate2["report"]
        assert rep["complete_requirement_assurance"] is False
        assert rep["human_approval_required"] is True
        assert rep["repair_termination_reason"] == "repair_succeeded"
        assert rep["final_candidate_patch_sha256"] == att2["patch_sha256"]
        assert any("offline deterministic fixture" in lim.lower() for lim in rep["evidence_limitations"])

        # Never auto-approved: still paused at AWAITING_FINAL_APPROVAL until human decides
        assert "submit_final_decision:approve" in gate2["allowed_actions"]
        dec_res = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Verified repaired premium-only patch and preserved attempt history.",
            },
        )
        assert dec_res.status_code == 200
        assert dec_res.json()["state"] == WorkflowState.APPROVED.value

    # Original repository completely untouched
    assert _snapshot_tree_bytes(repo) == before_bytes
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "rev-parse", "HEAD") == base_commit


# ---------------------------------------------------------------------------
# Req 4 & 20: Demonstration B — Two Failed Repair Attempts -> Budget Exhaustion
# ---------------------------------------------------------------------------


def test_demo_b_two_failed_repairs_exhaust_budget_and_block_approval(
    tmp_path: Path,
) -> None:
    """Demo B & Reqs 4, 20: 1 initial + 2 distinct failing repairs -> budget exhausted, approve blocked."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_demo_b"
    _create_mini_pos_repo(repo, clean=True)

    fixture = DeterministicPersistentRegressionPatchFixture()
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"mini-pos-b": repo},
        auto_repair_enabled=True,
        max_repair_attempts=2,
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )

    with TestClient(app) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "mini-pos-b",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(rid, timeout=30.0)

        client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003", "SCN-004"],
                "reviewer": "marsadh",
            },
        )
        app.state.executor.wait_for_run(rid, timeout=60.0)

        gate2 = client.get(f"/api/runs/{rid}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert len(fixture.received_requests) == 3  # 1 initial + 2 repairs, NO 4th call!
        assert gate2["repair_attempts_consumed"] == 2
        assert gate2["remaining_repair_budget"] == 0
        assert gate2["repair_termination_reason"] == RepairTerminationReason.BUDGET_EXHAUSTED.value
        assert gate2["repair_history"]["total_proposals_attempted"] == 3
        assert len(gate2["repair_history"]["attempts"]) == 3
        assert [a["attempt_number"] for a in gate2["repair_history"]["attempts"]] == [1, 2, 3]
        assert len(set(gate2["repair_history"]["seen_patch_hashes"])) == 3
        assert gate2["verification"]["verification_passed"] is False
        assert "submit_final_decision:approve" not in gate2["allowed_actions"]

        blocked = client.post(
            f"/api/runs/{rid}/decision",
            json={"decision": "approve", "reviewer": "marsadh", "comments": "attempt override"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "VERIFICATION_APPROVAL_BLOCKED"

        escalate = client.post(
            f"/api/runs/{rid}/decision",
            json={
                "decision": "needs_review",
                "reviewer": "marsadh",
                "comments": "Budget exhausted after 2 repair attempts; escalating for manual fix.",
            },
        )
        assert escalate.status_code == 200
        assert escalate.json()["state"] == WorkflowState.NEEDS_REVIEW.value


# ---------------------------------------------------------------------------
# Req 10: Demonstration C — Collection/Import Error Does NOT Trigger Repair
# ---------------------------------------------------------------------------


def test_demo_c_collection_or_import_error_does_not_trigger_repair(
    tmp_path: Path,
) -> None:
    """Demo C & Req 10: Generated-test import/collection error reports failure category & runs 0 repairs."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_demo_c"
    _create_mini_pos_repo(repo, clean=True)

    fixture = DeterministicRegressiveThenFixedPatchFixture()

    def _broken_import_m03_stub(_prompt: str) -> str:
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Broken import generated test",
                        "rationale": "Simulates generated test with nonexistent module import.",
                        "assumptions": "Offline deterministic Demo C fixture.",
                        "criterion_ids": ["AC-001"],
                        "test_code": (
                            "import nonexistent_demo_c_package_xyz\n\n"
                            "def test_never_collected():\n"
                            "    assert True\n"
                        ),
                    }
                ]
            }
        )

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"mini-pos-c": repo},
        auto_repair_enabled=True,
        max_repair_attempts=2,
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=fixture,
        offline_model_callable=_broken_import_m03_stub,
    )

    with TestClient(app) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "mini-pos-c",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(rid, timeout=30.0)

        client.post(
            f"/api/runs/{rid}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001"], "reviewer": "marsadh"},
        )
        app.state.executor.wait_for_run(rid, timeout=60.0)

        gate2 = client.get(f"/api/runs/{rid}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        # ZERO repair invocations occurred!
        assert len(fixture.received_requests) == 1
        assert gate2["repair_attempts_consumed"] == 0
        assert gate2["repair_termination_reason"] == RepairTerminationReason.INELIGIBLE_FAILURE.value
        assert gate2["repair_eligibility"]["eligible"] is False
        assert gate2["repair_eligibility"]["category"] in {
            RepairEligibilityCategory.MISSING_DEPENDENCY.value,
            RepairEligibilityCategory.COLLECTION_OR_IMPORT_ERROR.value,
        }
        assert gate2["verification"]["verification_outcome"] == VerificationOutcome.COLLECTION_ERROR.value
        assert gate2["report"]["recommendation_label"] == "verification_incomplete"
        assert "submit_final_decision:approve" not in gate2["allowed_actions"]


# ---------------------------------------------------------------------------
# Req 1 & 2: Repair Disabled & Successful Initial Patch Requiring No Repair
# ---------------------------------------------------------------------------


def test_repair_disabled_and_initial_success_no_repair(tmp_path: Path) -> None:
    """Reqs 1 & 2: Disabled repair performs 0 retries; passing initial patch performs 0 retries."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_r12"
    _create_mini_pos_repo(repo, clean=True)

    # Case 1: Repair disabled on regressive initial patch
    reg_fixture = DeterministicRegressiveThenFixedPatchFixture()
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos": repo},
        db_name="r1.sqlite3",
        auto_repair_enabled=False,
        max_repair_attempts=2,
    )
    app1 = create_app(
        cfg,
        patch_offline_fixture_provider=reg_fixture,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app1) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app1.state.executor.wait_for_run(rid, timeout=30.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002"],
                "reviewer": "qa",
            },
        )
        app1.state.executor.wait_for_run(rid, timeout=60.0)
        d1 = client.get(f"/api/runs/{rid}").json()
        assert len(reg_fixture.received_requests) == 1
        assert d1["repair_enabled"] is False
        assert d1["repair_attempts_consumed"] == 0
        assert d1["repair_termination_reason"] == RepairTerminationReason.REPAIR_DISABLED.value
        assert d1["verification"]["verification_passed"] is False

    # Case 2: Successful initial patch requires zero repair attempts
    good_calls = 0
    good_fixture = DeterministicPremiumDiscountPatchFixture()

    def _counting_good(req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal good_calls
        good_calls += 1
        return good_fixture.generate_patch(req)

    cfg2 = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos": repo},
        db_name="r2.sqlite3",
        auto_repair_enabled=True,
        max_repair_attempts=2,
    )
    app2 = create_app(
        cfg2,
        patch_offline_fixture_provider=_counting_good,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app2) as client:
        rid2 = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app2.state.executor.wait_for_run(rid2, timeout=30.0)
        client.post(
            f"/api/runs/{rid2}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002"],
                "reviewer": "qa",
            },
        )
        app2.state.executor.wait_for_run(rid2, timeout=60.0)
        d2 = client.get(f"/api/runs/{rid2}").json()
        assert good_calls == 1
        assert d2["repair_attempts_consumed"] == 0
        assert d2["remaining_repair_budget"] == 2
        assert d2["repair_termination_reason"] == RepairTerminationReason.NOT_NEEDED.value
        assert d2["verification"]["verification_passed"] is True
        assert "submit_final_decision:approve" in d2["allowed_actions"]


# ---------------------------------------------------------------------------
# Req 5, 6, 7, 8: Invalid Proposal Budget, Provider Unavailable, Duplicate, No Progress
# ---------------------------------------------------------------------------


def test_invalid_repair_proposal_consumes_budget_and_never_resets(tmp_path: Path) -> None:
    """Req 5: Invalid repair proposal consumes budget without resetting; budget=2 still terminates at 3 calls."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_invalid_rep"
    _create_mini_pos_repo(repo, clean=True)

    calls: list[int] = []
    base_fixture = DeterministicRegressiveThenFixedPatchFixture()

    def _provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        calls.append(req.attempt_number)
        if req.attempt_number == 1:
            return base_fixture.generate_patch(req)
        # Attempts 2 & 3 return malformed/unapplicable diffs targeting non-matching context
        bad_diff = (
            "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n"
            "--- a/mini_pos/billing.py\n"
            "+++ b/mini_pos/billing.py\n"
            "@@ -99,1 +99,1 @@\n"
            "-nonexistent_line\n"
            f"+replacement_attempt_{req.attempt_number}\n"
        )
        return _make_fixture_result(
            req,
            rel_path="mini_pos/billing.py",
            unified_diff=bad_diff,
            rationale="Unapplicable repair patch",
        )

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos": repo},
        max_repair_attempts=2,
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=_provider,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(rid, timeout=30.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app.state.executor.wait_for_run(rid, timeout=60.0)

        detail = client.get(f"/api/runs/{rid}").json()
        assert calls == [1, 2, 3]
        assert detail["repair_attempts_consumed"] == 2
        assert detail["remaining_repair_budget"] == 0
        assert detail["repair_termination_reason"] == RepairTerminationReason.BUDGET_EXHAUSTED.value
        assert detail["repair_history"]["attempts"][1]["outcome"] == "validation_failed"
        assert detail["repair_history"]["attempts"][1]["consumed_repair_budget"] is True
        assert detail["repair_history"]["attempts"][2]["outcome"] == "validation_failed"
        assert detail["repair_history"]["attempts"][2]["consumed_repair_budget"] is True
        assert "submit_final_decision:approve" not in detail["allowed_actions"]


def test_repair_provider_unavailable_stops_cleanly_and_consumes_budget(tmp_path: Path) -> None:
    """Req 6: Production repair provider returning unavailable stops cleanly without fallback."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_unavail"
    _create_mini_pos_repo(repo, clean=True)

    calls = 0
    base_fixture = DeterministicRegressiveThenFixedPatchFixture()

    def _provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal calls
        calls += 1
        if req.attempt_number == 1:
            return base_fixture.generate_patch(req)
        return PatchGenerationResult(
            status="unavailable",
            attempt_number=req.attempt_number,
            proposal=None,
            model_metadata=PatchModelMetadata(
                provider="offline_fixture",
                model_label="unavailable_repair_provider",
                generation_mode="offline_test",
                is_deterministic_fixture=True,
                is_mock_output=True,
            ),
            is_deterministic_fixture=True,
            is_mock_output=True,
            error=AgentError(
                code="PATCH_GENERATOR_UNAVAILABLE",
                message="Production A4 repair model is unavailable.",
            ),
        )

    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo})
    app = create_app(
        cfg,
        patch_offline_fixture_provider=_provider,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(rid, timeout=30.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app.state.executor.wait_for_run(rid, timeout=60.0)

        detail = client.get(f"/api/runs/{rid}").json()
        assert calls == 2
        assert detail["repair_attempts_consumed"] == 1
        assert detail["repair_termination_reason"] == RepairTerminationReason.PROVIDER_UNAVAILABLE.value
        assert detail["repair_history"]["attempts"][1]["outcome"] == "generation_unavailable"


def test_duplicate_patch_and_no_progress_termination(tmp_path: Path) -> None:
    """Reqs 7 & 8: Duplicate patch hash stops immediately; distinct patch with unchanged failure signature stops."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "mini_pos_dup"
    _create_mini_pos_repo(repo, clean=True)

    # 7. Duplicate patch hash detection
    dup_calls = 0
    base_fixture = DeterministicRegressiveThenFixedPatchFixture()

    def _dup_provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal dup_calls
        dup_calls += 1
        # Always generate the exact same regressive diff (attempt 1 behavior)
        first_req = req.model_copy(
            update={
                "attempt_number": 1,
                "attempt_type": "initial",
                "repair_attempt_number": 0,
            }
        )
        res = base_fixture.generate_patch(first_req)
        assert res.proposal is not None
        return res.model_copy(
            update={
                "attempt_number": req.attempt_number,
                "proposal": res.proposal.model_copy(
                    update={"proposal_id": f"PATCH-{req.attempt_number:03d}"}
                ),
            }
        )

    cfg1 = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="dup.sqlite3")
    app1 = create_app(
        cfg1,
        patch_offline_fixture_provider=_dup_provider,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app1) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app1.state.executor.wait_for_run(rid, timeout=30.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app1.state.executor.wait_for_run(rid, timeout=60.0)
        d_dup = client.get(f"/api/runs/{rid}").json()
        assert dup_calls == 2  # Stopped on repair #1 without wasting repair #2
        assert d_dup["repair_termination_reason"] == RepairTerminationReason.DUPLICATE_PATCH_DETECTED.value
        assert d_dup["repair_history"]["attempts"][1]["outcome"] == "duplicate_patch_rejected"

    # 8. Distinct patch (different comment, same logic -> identical failure signature) -> NO_PROGRESS_DETECTED
    noprog_calls = 0

    def _noprog_provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal noprog_calls
        noprog_calls += 1
        orig = req.relevant_source_files["mini_pos/billing.py"]
        eol = "\r\n" if "\r\n" in orig else "\n"
        updated = orig.replace(
            f"def apply_discount(total, customer_type):{eol}    return total{eol}",
            f"def apply_discount(total, customer_type):{eol}"
            f"    # distinct comment attempt {req.attempt_number}{eol}"
            f"    return total * 0.90{eol}",
        )
        return _make_fixture_result(
            req,
            rel_path="mini_pos/billing.py",
            unified_diff=_make_diff_for_file("mini_pos/billing.py", orig, updated),
            rationale=f"Distinct comment attempt {req.attempt_number}",
        )

    cfg2 = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="noprog.sqlite3")
    app2 = create_app(
        cfg2,
        patch_offline_fixture_provider=_noprog_provider,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app2) as client:
        rid2 = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app2.state.executor.wait_for_run(rid2, timeout=30.0)
        client.post(
            f"/api/runs/{rid2}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app2.state.executor.wait_for_run(rid2, timeout=60.0)
        d_np = client.get(f"/api/runs/{rid2}").json()
        assert noprog_calls == 2  # Stopped after repair #1 showed unchanged failure signature!
        assert d_np["repair_termination_reason"] == RepairTerminationReason.NO_PROGRESS_DETECTED.value
        assert d_np["repair_history"]["attempts"][0]["failure_signature"] == d_np["repair_history"]["attempts"][1]["failure_signature"]


# ---------------------------------------------------------------------------
# Req 9, 11, 12: Eligibility Guards (Test Defects, Incomplete Baseline, Ambiguity)
# ---------------------------------------------------------------------------


def test_eligibility_blocks_test_defects_incomplete_baseline_and_ambiguity() -> None:
    """Reqs 9, 11, 12: Conservative evaluator blocks test defects, incomplete baseline, and unresolved ambiguity."""
    sha = "a" * 64
    applied = PatchApplicationResult(
        applied=True,
        status="applied",
        patch_sha256=sha,
        original_source_snapshot_sha256=sha,
        original_git_diff_sha256=sha,
        resulting_source_snapshot_sha256="b" * 64,
        resulting_git_diff_sha256="c" * 64,
        applied_files=["mini_pos/billing.py"],
    )
    policy = RepairPolicy(enabled=True, max_repair_attempts=2)

    # 9a. Generated-test defect (missing fixture) -> TEST_DEFECT_OR_SYNTAX_ERROR
    base_ok = BaselineVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.PASSED,
        exit_code=0,
        total_tests=1,
        passed=1,
        failed=0,
    )
    mod_fixture_err = ModifiedVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        exit_code=1,
        total_tests=1,
        passed=0,
        failed=1,
        failures=[
            FailureDiagnostic(
                test_name=".changeguard_generated_tests/test_generated_001.py::test_bad",
                status="failed",
                evidence="E       fixture 'nonexistent_fixture' not found",
            )
        ],
    )
    res_fix = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied,
        requirement_analysis=None,
        baseline=base_ok,
        modified=mod_fixture_err,
        comparison=None,
        current_attempt_number=1,
        repair_attempts_consumed=0,
    )
    assert res_fix.eligible is False
    assert res_fix.category == RepairEligibilityCategory.TEST_DEFECT_OR_SYNTAX_ERROR

    # 11. Incomplete baseline evidence / aggregate pass count < 100% alone -> UNAVAILABLE_OR_INCOMPARABLE_EVIDENCE
    mod_fail = ModifiedVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        exit_code=1,
        total_tests=2,
        passed=1,
        failed=1,
        failures=[
            FailureDiagnostic(
                test_name="test_regular",
                status="failed",
                evidence="AssertionError: assert 180 == 200",
            )
        ],
    )
    incomplete_cmp = BaselineComparisonEvidence(
        status=VerificationComparisonStatus.INCOMPLETE,
        approved_test_hashes_verified=True,
        summary=RegressionComparisonSummary(
            comparison_status=VerificationComparisonStatus.INCOMPLETE,
            total_compared=2,
            comparable_count=1,
            unknown_count=1,
        ),
    )
    res_inc = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied,
        requirement_analysis=None,
        baseline=base_ok,
        modified=mod_fail,
        comparison=incomplete_cmp,
        current_attempt_number=1,
        repair_attempts_consumed=0,
    )
    assert res_inc.eligible is False
    assert res_inc.category == RepairEligibilityCategory.UNAVAILABLE_OR_INCOMPARABLE_EVIDENCE

    # 12. Mandatory unresolved ambiguity -> UNRESOLVED_MANDATORY_AMBIGUITY
    amb_req = RequirementAnalysisResult(
        status="needs_clarification",
        original_requirement="Give discount to VIPs.",
        acceptance_criteria=[
            AcceptanceCriterion(
                criterion_id="AC-001",
                requirement_reference="VIP discount",
                description="Discount for VIPs",
                expected_behavior="Discount applied",
                ambiguity_ids=["AMB-001"],
            )
        ],
        ambiguities=[
            RequirementAmbiguity(
                ambiguity_id="AMB-001",
                description="Discount rate unspecified",
                clarification_question="What percentage discount applies?",
                severity=AmbiguitySeverity.BLOCKING,
                mandatory_to_resolve=True,
                resolved=False,
                related_criterion_ids=["AC-001"],
            )
        ],
        has_mandatory_unresolved_ambiguities=True,
    )
    res_amb = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied,
        requirement_analysis=amb_req,
        baseline=base_ok,
        modified=mod_fail,
        comparison=incomplete_cmp,
        current_attempt_number=1,
        repair_attempts_consumed=0,
    )
    assert res_amb.eligible is False
    assert res_amb.category == RepairEligibilityCategory.UNRESOLVED_MANDATORY_AMBIGUITY

    # Provider auth/rate-limit error -> PROVIDER_AUTH_OR_RATE_LIMIT
    res_rl = RepairEligibilityEvaluator.evaluate(
        workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
        policy=policy,
        patch_application=applied,
        requirement_analysis=None,
        baseline=base_ok,
        modified=mod_fail,
        comparison=incomplete_cmp,
        current_attempt_number=1,
        repair_attempts_consumed=0,
        provider_error=AgentError(code="PROVIDER_RATE_LIMIT", message="HTTP 429 too many requests"),
    )
    assert res_rl.eligible is False
    assert res_rl.category == RepairEligibilityCategory.PROVIDER_AUTH_OR_RATE_LIMIT


# ---------------------------------------------------------------------------
# Req 14 & 16: Test Modification Rejection & Scope / Assumption Invalidation
# ---------------------------------------------------------------------------


def test_repair_rejects_test_modification_and_scope_invalidation(tmp_path: Path) -> None:
    """Reqs 14 & 16: Rejects repair modifying approved/existing tests and stops if patch breaks test assumptions."""
    repos_root = tmp_path / "repos"
    existing_test = (
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
        "from billing import apply_discount\n\n"
        "def test_existing_regular():\n"
        "    assert apply_discount(100.0, 'regular') == 100.0\n"
    )
    repo = repos_root / "mini_pos_guard"
    _create_mini_pos_repo(repo, clean=True, existing_test_code=existing_test)

    # 14. Attempted modification of existing regression test suite rejected immediately
    base_fixture = DeterministicRegressiveThenFixedPatchFixture()

    def _test_tampering_provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        if req.attempt_number == 1:
            return base_fixture.generate_patch(req)
        tamper_diff = _make_diff_for_file(
            "tests/test_existing_regression.py",
            existing_test,
            existing_test.replace("== 100.0", "== 90.0"),
        )
        return _make_fixture_result(
            req,
            rel_path="tests/test_existing_regression.py",
            unified_diff=tamper_diff,
            rationale="Illegal attempt to weaken existing regression test assertion",
        )

    cfg1 = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="tmod.sqlite3")
    app1 = create_app(
        cfg1,
        patch_offline_fixture_provider=_test_tampering_provider,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app1) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app1.state.executor.wait_for_run(rid, timeout=30.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app1.state.executor.wait_for_run(rid, timeout=60.0)
        d1 = client.get(f"/api/runs/{rid}").json()
        assert d1["repair_termination_reason"] == RepairTerminationReason.TEST_MODIFICATION_REJECTED.value
        assert d1["repair_history"]["attempts"][1]["outcome"] == "test_modification_rejected"
        assert "submit_final_decision:approve" not in d1["allowed_actions"]

    # 16b. Replacement patch deleting a symbol imported by approved tests -> TEST_ASSUMPTIONS_INVALIDATED
    def _symbol_removing_provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        if req.attempt_number == 1:
            return base_fixture.generate_patch(req)
        orig = req.relevant_source_files["mini_pos/billing.py"]
        renamed = orig.replace("def apply_discount(", "def apply_discount_v2(").replace(
            "return apply_discount(", "return apply_discount_v2("
        )
        return _make_fixture_result(
            req,
            rel_path="mini_pos/billing.py",
            unified_diff=_make_diff_for_file("mini_pos/billing.py", orig, renamed),
            rationale="Renames apply_discount breaking approved test import assumption",
        )

    cfg2 = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="scope.sqlite3")
    app2 = create_app(
        cfg2,
        patch_offline_fixture_provider=_symbol_removing_provider,
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    with TestClient(app2) as client:
        rid2 = client.post(
            "/api/runs",
            json={
                "repository_id": "pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
            },
        ).json()["run_id"]
        app2.state.executor.wait_for_run(rid2, timeout=30.0)
        client.post(
            f"/api/runs/{rid2}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app2.state.executor.wait_for_run(rid2, timeout=60.0)
        d2 = client.get(f"/api/runs/{rid2}").json()
        assert d2["repair_termination_reason"] == RepairTerminationReason.TEST_ASSUMPTIONS_INVALIDATED.value
        assert d2["repair_history"]["attempts"][1]["outcome"] == "scope_invalidated"
        assert "submit_final_decision:approve" not in d2["allowed_actions"]


# ---------------------------------------------------------------------------
# Req 18, 19, 21, 22, 23, 24: Persistence, Restart, OpenAPI, Secrets, Live Mode, Verify-Existing
# ---------------------------------------------------------------------------


def test_persistence_restart_recovery_openapi_secrets_live_mode_and_verify_existing(
    tmp_path: Path,
) -> None:
    """Reqs 18, 19, 21, 22, 23, 24: Persistence, restart recovery, OpenAPI, redaction, live mode, compatibility."""
    repos_root = tmp_path / "repos"
    repo_clean = repos_root / "pos_clean"
    repo_reg = repos_root / "pos_reg"
    _create_mini_pos_repo(repo_clean, clean=True)
    _create_mini_pos_repo(repo_reg, clean=False, regressive_working_tree=True)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean": repo_clean, "reg": repo_reg},
    )
    store = RunStore(cfg.database_path)

    # 19. Restart recovery on new P0-8 active states preserves consumed budget & never replays A4
    replayed_calls = 0

    def _never_replay(_req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal replayed_calls
        replayed_calls += 1
        raise AssertionError("Restart recovery must never replay A4 provider calls")

    for idx, st in enumerate(
        [WorkflowState.EVALUATING_REPAIR_ELIGIBILITY, WorkflowState.REPAIRING_PATCH],
        start=1,
    ):
        hist = RepairAttemptHistory(
            policy=RepairPolicy(enabled=True, max_repair_attempts=2),
            total_proposals_attempted=2,
            repair_attempts_consumed=1,
            current_attempt_number=2,
            remaining_repair_budget=1,
        )
        store.save_run(
            WorkflowRunRecord(
                run_id=f"run_p08_interrupted_{idx}",
                state=st,
                workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
                requirements="Interrupted mid-repair",
                repair_policy=hist.policy,
                repair_attempts_consumed=1,
                repair_history=hist,
                repository=RepositoryInfo(
                    repository_id="clean",
                    repository_path=str(repo_clean),
                    source_path="mini_pos",
                    base_ref="HEAD",
                ),
            )
        )

    svc = OrchestratorService(
        config=cfg,
        store=store,
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        patch_adapter=PatchGenerationAdapter(
            config=cfg, offline_fixture_provider=_never_replay
        ),
    )
    recovered = svc.recover_interrupted_runs()
    assert replayed_calls == 0
    assert len(recovered) == 2
    for rec in recovered:
        assert rec.state == WorkflowState.FAILED
        assert rec.error is not None and rec.error.code == "PROCESS_INTERRUPTED"
        assert rec.repair_attempts_consumed == 1  # Persisted budget NOT reset!
        assert rec.repair_termination_reason == RepairTerminationReason.PROCESS_INTERRUPTED
        assert rec.repair_history is not None
        assert rec.repair_history.remaining_repair_budget == 1

    # 18. Pre-P0-8 legacy persisted JSON loads cleanly with backward-compatible defaults
    legacy_p07_json = json.dumps(
        {
            "run_id": "run_legacy_p07_001",
            "state": "APPROVED",
            "requirements": "Legacy P0-7 run",
            "workflow_mode": "generate_and_verify_change",
            "execution_mode": "offline_test",
            "repository": {
                "repository_id": "clean",
                "repository_path": str(repo_clean),
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "change_mode": "working_tree",
            },
        }
    )
    legacy = WorkflowRunRecord.model_validate_json(legacy_p07_json)
    assert legacy.repair_policy is None
    assert legacy.repair_attempts_consumed == 0
    assert legacy.repair_history is None
    assert legacy.repair_termination_reason is None

    # 22. Secret redaction across repair_history, SQLite storage & API DTOs
    synthetic_secret = "gsk_" + "RepairSecretRedactionCheck9876543210"
    secret_hist = RepairAttemptHistory(
        policy=RepairPolicy(enabled=True, max_repair_attempts=2),
        total_proposals_attempted=1,
        repair_attempts_consumed=0,
        current_attempt_number=1,
        remaining_repair_budget=2,
        termination_reason=RepairTerminationReason.PROVIDER_ERROR,
        termination_summary=f"Provider failed with token {synthetic_secret}",
        attempts=[
            PatchAttemptRecord(
                attempt_number=1,
                attempt_type="initial",
                repair_attempt_index=0,
                original_requirement="Check secret redaction",
                outcome="failed_verification",
                failure_diagnostics=[
                    FailureDiagnostic(
                        test_name="test_secret",
                        status="failed",
                        evidence=f"AssertionError: key={synthetic_secret}",
                    )
                ],
                errors=[
                    AgentError(
                        code="TEST_ERR",
                        message=f"Leaked Bearer eyJhbGciOiJIUzI1NiJ9.{synthetic_secret}",
                    )
                ],
                notes=[f"api_key={synthetic_secret}"],
            )
        ],
    )
    store.save_run(
        WorkflowRunRecord(
            run_id="run_p08_secret_check",
            state=WorkflowState.AWAITING_FINAL_APPROVAL,
            workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
            requirements="Verify repair secret redaction",
            repair_policy=secret_hist.policy,
            repair_termination_reason=RepairTerminationReason.PROVIDER_ERROR,
            repair_history=secret_hist,
            repository=RepositoryInfo(
                repository_id="clean",
                repository_path=str(repo_clean),
                source_path="mini_pos",
                base_ref="HEAD",
            ),
        )
    )
    assert synthetic_secret.encode("utf-8") not in cfg.database_path.read_bytes()

    # 21. OpenAPI parity, /health fields, zero host path exposure
    app = create_app(
        cfg,
        patch_offline_fixture_provider=DeterministicRegressiveThenFixedPatchFixture(),
        offline_model_callable=_behavioral_mini_pos_m03_stub,
    )
    committed_openapi = json.loads(
        (REPO_ROOT / "docs" / "m02" / "openapi.json").read_text(encoding="utf-8")
    )
    assert app.openapi() == committed_openapi

    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["auto_repair_enabled"] is True
        assert health["max_repair_attempts"] == 2

        sec_res = client.get("/api/runs/run_p08_secret_check")
        assert sec_res.status_code == 200
        assert synthetic_secret not in sec_res.text
        assert "[REDACTED_GROQ_KEY]" in sec_res.text
        assert str(tmp_path) not in sec_res.text

        # 24. Existing verify_existing_change workflow with regressive change NEVER triggers repair
        r_ver = client.post(
            "/api/runs",
            json={
                "repository_id": "reg",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.VERIFY_EXISTING_CHANGE.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(r_ver, timeout=30.0)
        client.post(
            f"/api/runs/{r_ver}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001", "SCN-002"], "reviewer": "qa"},
        )
        app.state.executor.wait_for_run(r_ver, timeout=60.0)
        ver_detail = client.get(f"/api/runs/{r_ver}").json()
        assert ver_detail["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert ver_detail["repair_enabled"] is False
        assert ver_detail["repair_attempts_consumed"] == 0
        assert ver_detail["repair_history"] is None
        assert ver_detail["baseline_comparison"]["summary"]["has_confirmed_regression"] is True
        assert "submit_final_decision:approve" not in ver_detail["allowed_actions"]

    # 23. Live mode rejects deterministic fixture fallback during repair
    live_adapter = PatchGenerationAdapter(
        config=cfg,
        production_provider=None,
        offline_fixture_provider=DeterministicRegressiveThenFixedPatchFixture(),
    )
    live_repair_req = PatchGenerationRequest(
        run_id="run_live_repair_check",
        original_requirement=PREMIUM_DISCOUNT_REQUIREMENT,
        acceptance_criteria=[
            AcceptanceCriterion(
                criterion_id="AC-001",
                requirement_reference="Premium discount",
                description="10% discount for premium",
                expected_behavior="apply_discount(100, 'premium') == 90",
            )
        ],
        repository_snapshot_ref="deadbeef" * 5,
        source_path="mini_pos",
        allowed_modification_paths=["mini_pos"],
        relevant_source_files={"mini_pos/billing.py": BASELINE_BILLING},
        attempt_number=2,
        attempt_type="repair",
        repair_attempt_number=1,
        execution_mode="live_llm",
        previous_proposed_patch="diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n",
        previous_patch_sha256="a" * 64,
    )
    live_res = live_adapter.generate(live_repair_req)
    assert live_res.status == "unavailable"
    assert live_res.error is not None
    assert live_res.error.code == "PATCH_GENERATOR_UNAVAILABLE"
    assert live_res.is_deterministic_fixture is False
    assert live_res.is_mock_output is False
