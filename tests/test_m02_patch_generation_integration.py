"""Comprehensive offline test suite for P0-6 Patch Generation Integration Infrastructure.

Covers all 20 required verification areas plus the full offline end-to-end
deterministic premium-customer discount demonstration on a clean mini_pos
repository (explicitly labeled as an offline fixture demonstration, NOT actual
AI-generated patch evidence):

1.  Existing verification workflow compatibility (verify_existing_change + COMPLETED_NO_CHANGES).
2.  Clean-repository generation workflow (generate_and_verify_change).
3.  Missing A4 implementation (PATCH_GENERATOR_UNAVAILABLE).
4.  Injected deterministic patch generation.
5.  Invalid A4 responses (schema, hash, attempt mismatch, provider exception).
6.  Invalid acceptance criterion references.
7.  Valid patch application & post-patch snapshot/diff evidence.
8.  Invalid patch syntax, binary patches, unsafe file modes, header smuggling.
9.  Path traversal, absolute paths, outside scope, and protected files/dirs.
10. Symlink escape prevention.
11. Patch size limits and changed-file limits.
12. Patch hash tampering and workspace tampering before validation/application.
13. Failed patch application without partial verification (rollback verified).
14. Mandatory unresolved ambiguity blocking before A4 invocation.
15. Post-patch M01 impact analysis on generated patch.
16. Gate 1 and Gate 2 enforcement (no auto commit/merge/push/deploy).
17. Original repository integrity.
18. Persistence, pre-P0-6 backward compatibility, safe restart recovery, and zero path/secret leakage.
19. Explicit fixture labeling (and rejection of mislabeled fixture output).
20. Live mode without silent fallback to offline fixture.
"""

from __future__ import annotations

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
    DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL,
    DeterministicPremiumDiscountPatchFixture,
    OrchestratorConfig,
    OrchestratorService,
    PatchApplicator,
    PatchGenerationAdapter,
    PatchValidator,
    RunStore,
    WorkspaceManager,
)
from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
)
from project.marsadh_orchestrator.orchestrator import (
    compute_git_diff_sha256,
    compute_source_snapshot_sha256,
)
from project.schemas import (
    AcceptanceCriterion,
    CreateRunRequest,
    ExecutionMode,
    HumanDecision,
    HumanDecisionRequest,
    PatchFileChange,
    PatchGenerationRequest,
    PatchGenerationResult,
    PatchModelMetadata,
    PatchProposal,
    RepositoryInfo,
    TestReviewRequest,
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

MODIFIED_BILLING = (
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


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for P0-6 integration tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _create_mini_pos_repo(repo: Path, *, clean: bool = True) -> str:
    app_dir = repo / "mini_pos"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "billing.py").write_text(BASELINE_BILLING, encoding="utf-8")
    for fname in ("refunds.py", "customer_service.py", "dashboard.py", "reports.py"):
        shutil.copyfile(MINI_POS_DIR / fname, app_dir / fname)

    _git(repo, "init")
    _git(repo, "config", "user.name", "ChangeGuard P0-6")
    _git(repo, "config", "user.email", "p06@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline clean mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    if not clean:
        (app_dir / "billing.py").write_text(MODIFIED_BILLING, encoding="utf-8")
    return base_commit


def _make_config(
    tmp_path: Path,
    *,
    repos_root: Path,
    configured: dict[str, Path],
    db_name: str = "p06.sqlite3",
    max_patch_size_bytes: int = 100_000,
    max_patch_changed_files: int = 10,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        database_path=tmp_path / "state" / db_name,
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories=configured,
        trusted_execution_roots=[repos_root],
        verification_timeout_seconds=30,
        max_patch_size_bytes=max_patch_size_bytes,
        max_patch_changed_files=max_patch_changed_files,
        environment="test",
    )


def _behavioral_m03_stub(prompt: str) -> str:
    """Deterministic offline M03 behavioral test stub for mini_pos verification."""
    assert "mini_pos/billing.py" in prompt
    assert "apply_discount" in prompt
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
                    "rationale": "Verify AC-001 on post-patch billing.apply_discount.",
                    "assumptions": "Offline deterministic behavioral test stub.",
                    "criterion_ids": ["AC-001"],
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_premium_discount():\n"
                        + "    assert apply_discount(200.0, 'premium') == pytest.approx(180.0)\n"
                        + "    assert checkout(50.0, 4, 'premium') == pytest.approx(180.0)\n"
                    ),
                },
                {
                    "title": "Regular customer checkout remains unchanged",
                    "rationale": "Verify AC-002 regular customer behavior preservation.",
                    "assumptions": "Offline deterministic behavioral test stub.",
                    "criterion_ids": ["AC-002"],
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_regular_unchanged():\n"
                        + "    assert apply_discount(200.0, 'regular') == pytest.approx(200.0)\n"
                        + "    assert checkout(50.0, 4, 'regular') == pytest.approx(200.0)\n"
                    ),
                },
                {
                    "title": "Dependent refund, dashboard, and sales report modules preserved",
                    "rationale": "Verify AC-003 across M01-detected impacted modules.",
                    "assumptions": "Offline deterministic behavioral test stub.",
                    "criterion_ids": ["AC-003"],
                    "test_code": (
                        header
                        + "from dashboard import display_refund\n"
                        + "from reports import generate_sales_report\n\n"
                        + "def test_downstream_modules():\n"
                        + "    assert display_refund(100.0, 'premium') == {'refund_amount': pytest.approx(90.0)}\n"
                        + "    assert display_refund(100.0, 'regular') == {'refund_amount': pytest.approx(100.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'premium') == {'total_sales': pytest.approx(180.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'regular') == {'total_sales': pytest.approx(200.0)}\n"
                    ),
                },
            ]
        }
    )


def _sample_patch_request(
    *,
    execution_mode: str = "offline_test",
    relevant_files: dict[str, str] | None = None,
) -> PatchGenerationRequest:
    return PatchGenerationRequest(
        run_id="run_test_p06_001",
        original_requirement=PREMIUM_DISCOUNT_REQUIREMENT,
        acceptance_criteria=[
            AcceptanceCriterion(
                criterion_id="AC-001",
                requirement_reference="Apply 10% discount to premium customers",
                description="Premium customers receive a 10% discount.",
                expected_behavior="apply_discount(100, 'premium') == 90.0",
                target_modules=["billing"],
            ),
            AcceptanceCriterion(
                criterion_id="AC-002",
                requirement_reference="Regular customers unchanged",
                description="Regular customers receive 0% discount.",
                expected_behavior="apply_discount(100, 'regular') == 100.0",
                target_modules=["billing"],
                preserves_existing_behavior=True,
            ),
        ],
        repository_snapshot_ref="deadbeef" * 5,
        base_ref="HEAD",
        source_path="mini_pos",
        allowed_modification_paths=["mini_pos"],
        relevant_source_files=relevant_files or {"mini_pos/billing.py": BASELINE_BILLING},
        attempt_number=1,
        execution_mode=execution_mode,  # type: ignore[arg-type]
    )


def _make_proposal(
    unified_diff: str,
    *,
    affected_files: list[PatchFileChange],
    related_criterion_ids: list[str] | None = None,
) -> PatchProposal:
    return PatchProposal(
        proposal_id="PATCH-001",
        unified_diff=unified_diff,
        patch_sha256=compute_patch_sha256(unified_diff),
        affected_files=affected_files,
        change_rationale="Test proposal",
        related_criterion_ids=related_criterion_ids or ["AC-001"],
    )


def _snapshot_tree_bytes(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.parts:
            continue
        if path.is_file():
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


# ---------------------------------------------------------------------------
# 1 & 2 & 4 & 7 & 15 & 16 & 17 & 19: Full Offline End-to-End Demonstration + Compatibility
# ---------------------------------------------------------------------------


def test_verify_existing_change_backward_compatibility_clean_and_modified(
    tmp_path: Path,
) -> None:
    """Requirement 1: Existing verify_existing_change behavior preserved on clean & modified repos."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_pos"
    mod_repo = repos_root / "mod_pos"
    _create_mini_pos_repo(clean_repo, clean=True)
    _create_mini_pos_repo(mod_repo, clean=False)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-pos": clean_repo, "mod-pos": mod_repo},
    )
    app = create_app(cfg, offline_model_callable=_behavioral_m03_stub)

    with TestClient(app) as client:
        # Default workflow_mode omitted -> verify_existing_change -> clean repo ends in COMPLETED_NO_CHANGES
        r_clean = client.post(
            "/api/runs",
            json={
                "repository_id": "clean-pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        )
        assert r_clean.status_code == 202
        assert r_clean.json()["workflow_mode"] == WorkflowMode.VERIFY_EXISTING_CHANGE.value
        clean_id = r_clean.json()["run_id"]
        app.state.executor.wait_for_run(clean_id, timeout=30.0)

        clean_detail = client.get(f"/api/runs/{clean_id}").json()
        assert clean_detail["state"] == WorkflowState.COMPLETED_NO_CHANGES.value
        assert clean_detail["workflow_mode"] == WorkflowMode.VERIFY_EXISTING_CHANGE.value
        assert clean_detail["patch_generation"] is None
        assert clean_detail["pre_change_context"] is None

        # Existing modified repo -> pauses at Gate 1 as before
        r_mod = client.post(
            "/api/runs",
            json={
                "repository_id": "mod-pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        )
        assert r_mod.status_code == 202
        mod_id = r_mod.json()["run_id"]
        app.state.executor.wait_for_run(mod_id, timeout=30.0)

        mod_detail = client.get(f"/api/runs/{mod_id}").json()
        assert mod_detail["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert mod_detail["codebase_analysis"]["changed_files_repo_relative"] == [
            "mini_pos/billing.py"
        ]


def test_clean_repo_generate_and_verify_end_to_end_offline_fixture_demo(
    tmp_path: Path,
) -> None:
    """Requirements 2, 4, 7, 15, 16, 17, 19: Full clean-repo offline fixture demonstration."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_mini_pos"
    orig_head = _create_mini_pos_repo(clean_repo, clean=True)
    orig_status = _git(clean_repo, "status", "--porcelain")
    orig_tree = _snapshot_tree_bytes(clean_repo)
    assert orig_status == ""

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-mini-pos": clean_repo},
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=DeterministicPremiumDiscountPatchFixture(),
        offline_model_callable=_behavioral_m03_stub,
    )

    with TestClient(app) as client:
        # Health endpoint accurately reports production A4 not configured
        health = client.get("/health").json()
        assert health["patch_generator_configured"] is False
        assert set(health["supported_workflow_modes"]) == {
            "verify_existing_change",
            "generate_and_verify_change",
        }

        # 1. Start generate_and_verify_change run against a CLEAN repository
        create_res = client.post(
            "/api/runs",
            json={
                "repository_id": "clean-mini-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        )
        assert create_res.status_code == 202
        created = create_res.json()
        assert created["workflow_mode"] == WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value
        run_id = created["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        # 2. Verify Gate 1 pause (no tests executed yet, patch validated & applied, post-patch M01 ran)
        gate1_res = client.get(f"/api/runs/{run_id}")
        assert gate1_res.status_code == 200
        # Ensure no host paths or raw source blobs leaked
        assert str(tmp_path) not in gate1_res.text
        gate1 = gate1_res.json()

        assert gate1["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert gate1["workflow_mode"] == WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value
        assert gate1["allowed_actions"] == ["submit_test_review"]

        # Pre-change context explicitly documents M01 static-index scope
        pre_ctx = gate1["pre_change_context"]
        assert pre_ctx["status"] == "completed"
        assert pre_ctx["scope"] == "static_repository_index_only"
        assert pre_ctx["pre_change_impact_prediction_available"] is False
        assert pre_ctx["total_indexed_files"] == 5

        # Explicit offline fixture labeling
        pg = gate1["patch_generation"]
        assert pg["status"] == "proposed"
        assert pg["attempt_number"] == 1
        assert pg["is_deterministic_fixture"] is True
        assert pg["is_mock_output"] is True
        assert pg["model_metadata"]["model_label"] == DEFAULT_OFFLINE_PATCH_FIXTURE_LABEL
        assert pg["model_metadata"]["provider"] == "offline_fixture"
        assert pg["proposal"]["patch_sha256"] == gate1["patch_proposal_sha256"]
        assert pg["proposal"]["affected_files"][0]["path"] == "mini_pos/billing.py"

        # Patch validation passed all checks including git apply --check
        pv = gate1["patch_validation"]
        assert pv["valid"] is True
        assert pv["status"] == "passed"
        assert pv["git_apply_check_passed"] is True
        assert pv["checked_files"] == ["mini_pos/billing.py"]
        assert pv["violations"] == []

        # Patch application succeeded and preserved original vs resulting snapshots
        pa = gate1["patch_application"]
        assert pa["applied"] is True
        assert pa["status"] == "applied"
        assert pa["original_source_snapshot_sha256"] == gate1["original_source_snapshot_sha256"]
        assert pa["resulting_source_snapshot_sha256"] == gate1["source_snapshot_sha256"]
        assert pa["original_source_snapshot_sha256"] != pa["resulting_source_snapshot_sha256"]
        assert pa["original_git_diff_sha256"] != pa["resulting_git_diff_sha256"]
        assert "not an operating-system security sandbox" in pa["isolation_note"].lower()

        # Post-patch M01 impact analysis detected billing.py + 4 downstream modules
        cb = gate1["codebase_analysis"]
        assert cb["status"] == "completed"
        assert cb["changed_files_repo_relative"] == ["mini_pos/billing.py"]
        assert set(cb["impacted_modules_repo_relative"]) == {
            "mini_pos/customer_service.py",
            "mini_pos/dashboard.py",
            "mini_pos/refunds.py",
            "mini_pos/reports.py",
        }

        # Gate 1: tests proposed but NOT executed yet
        assert gate1["test_generation"]["executed"] is False
        assert gate1["verification"] is None

        # Cannot jump straight to Gate 2 decision
        premature = client.post(
            f"/api/runs/{run_id}/decision",
            json={"decision": "approve", "reviewer": "marsadh", "comments": "too early"},
        )
        assert premature.status_code == 409

        # 3. Approve all 3 behavioral scenarios at Gate 1 -> runs M04 pytest
        rev_res = client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
                "notes": "Approve behavioral verification for offline fixture patch demo.",
            },
        )
        assert rev_res.status_code == 202
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        # 4. Inspect Gate 2 compiled ChangePackageReport
        gate2 = client.get(f"/api/runs/{run_id}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2["verification"]["verification_passed"] is True
        assert gate2["verification"]["passed"] == 3
        assert gate2["verification"]["failed"] == 0
        assert gate2["report"]["workflow_mode"] == WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value
        assert gate2["report"]["patch_applied"] is True
        assert gate2["report"]["patch_is_deterministic_fixture"] is True
        assert gate2["report"]["patch_proposal_sha256"] == gate1["patch_proposal_sha256"]
        assert gate2["report"]["traceability"]["requirement_verification_proven"] is False
        assert any(
            "offline deterministic fixture" in lim.lower()
            for lim in gate2["report"]["evidence_limitations"]
        )
        assert any(
            "not an operating-system security sandbox" in lim.lower()
            for lim in gate2["report"]["evidence_limitations"]
        )
        assert "submit_final_decision:approve" in gate2["allowed_actions"]

        # 5. Record final human approval at Gate 2
        dec_res = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Verified offline fixture demo end-to-end.",
            },
        )
        assert dec_res.status_code == 200
        assert dec_res.json()["state"] == WorkflowState.APPROVED.value

        # 6. Verify structured logs captured every stage in order
        logs = client.get(f"/api/runs/{run_id}/logs").json()["logs"]
        stages = [entry["stage"] for entry in logs]
        expected_order = [
            WorkflowState.CREATED.value,
            WorkflowState.ANALYZING_REQUIREMENTS.value,
            WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT.value,
            WorkflowState.GENERATING_PATCH.value,
            WorkflowState.VALIDATING_PATCH.value,
            WorkflowState.APPLYING_PATCH.value,
            WorkflowState.ANALYZING_POST_PATCH_IMPACT.value,
            WorkflowState.GENERATING_TESTS.value,
            WorkflowState.AWAITING_TEST_REVIEW.value,
            WorkflowState.RUNNING_VERIFICATION.value,
            WorkflowState.COMPILING_REPORT.value,
            WorkflowState.AWAITING_FINAL_APPROVAL.value,
            WorkflowState.APPROVED.value,
        ]
        last_idx = -1
        for st in expected_order:
            pos = stages.index(st)
            assert pos > last_idx, f"Expected stage {st} after index {last_idx}, got {pos}"
            last_idx = pos

    # 7. Requirement 17: Original repository is 100% untouched (no edits, commits, merges, pushes)
    assert _git(clean_repo, "rev-parse", "HEAD") == orig_head
    assert _git(clean_repo, "status", "--porcelain") == ""
    assert _snapshot_tree_bytes(clean_repo) == orig_tree


# ---------------------------------------------------------------------------
# 3 & 20: Missing A4 Implementation & Live Mode Without Silent Fallback
# ---------------------------------------------------------------------------


def test_missing_a4_implementation_fails_with_patch_generator_unavailable(
    tmp_path: Path,
) -> None:
    """Requirement 3: Missing A4 implementation produces PATCH_GENERATOR_UNAVAILABLE."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_pos"
    _create_mini_pos_repo(clean_repo, clean=True)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-pos": clean_repo},
    )
    # No patch provider injected
    app = create_app(cfg, offline_model_callable=_behavioral_m03_stub)

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "clean-pos",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["state"] == WorkflowState.FAILED.value
        assert detail["error"]["code"] == "PATCH_GENERATOR_UNAVAILABLE"
        assert detail["patch_generation"]["status"] == "unavailable"
        assert detail["patch_validation"] is None
        assert detail["patch_application"] is None
        assert detail["codebase_analysis"] is None
        assert detail["test_generation"] is None


def test_live_mode_never_falls_back_to_offline_fixture_and_rejects_mock_live_output() -> None:
    """Requirement 20: Live mode never silently falls back to fixture generation."""
    fixture_called = False

    def _tracking_fixture(req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal fixture_called
        fixture_called = True
        return DeterministicPremiumDiscountPatchFixture().generate_patch(req)

    # Even when offline fixture IS injected, live_llm MUST NOT invoke it
    adapter = PatchGenerationAdapter(offline_fixture_provider=_tracking_fixture)
    live_req = _sample_patch_request(execution_mode="live_llm")
    res = adapter.generate(live_req)

    assert fixture_called is False
    assert res.status == "unavailable"
    assert res.error is not None
    assert res.error.code == "PATCH_GENERATOR_UNAVAILABLE"
    assert res.is_deterministic_fixture is False
    assert res.is_mock_output is False

    # If a misbehaving production provider returns mock/fixture output in live_llm mode, reject it
    def _sneaky_mock_provider(req: PatchGenerationRequest) -> PatchGenerationResult:
        return DeterministicPremiumDiscountPatchFixture().generate_patch(req)

    adapter_sneaky = PatchGenerationAdapter(production_provider=_sneaky_mock_provider)
    sneaky_res = adapter_sneaky.generate(live_req)
    assert sneaky_res.status == "error"
    assert sneaky_res.error is not None
    assert sneaky_res.error.code == "LIVE_MODE_MOCK_REJECTED"


# ---------------------------------------------------------------------------
# 5, 6, 19: Invalid A4 Responses, Invalid Criterion References, Mislabeled Fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_return", "expected_code"),
    [
        (None, "INVALID_PATCH_RESPONSE"),
        ("not-a-dict", "INVALID_PATCH_RESPONSE"),
        ({"status": "proposed"}, "INVALID_PATCH_RESPONSE"),
        (
            {
                "status": "proposed",
                "attempt_number": 1,
                "proposal": {
                    "proposal_id": "PATCH-001",
                    "unified_diff": "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n",
                    "patch_sha256": "0" * 64,  # Wrong SHA-256
                    "affected_files": [{"path": "mini_pos/billing.py"}],
                    "change_rationale": "Bad hash",
                },
                "model_metadata": {
                    "model_label": "offline_fixture",
                    "generation_mode": "offline_test",
                    "is_deterministic_fixture": True,
                    "is_mock_output": True,
                },
                "is_deterministic_fixture": True,
                "is_mock_output": True,
            },
            "INVALID_PATCH_RESPONSE",
        ),
    ],
)
def test_adapter_rejects_invalid_a4_responses(
    bad_return: Any,
    expected_code: str,
) -> None:
    """Requirement 5: Invalid A4 responses are rejected with structured errors."""
    adapter = PatchGenerationAdapter(offline_fixture_provider=lambda _r: bad_return)
    res = adapter.generate(_sample_patch_request())
    assert res.status == "error"
    assert res.error is not None
    assert res.error.code == expected_code


def test_adapter_rejects_mismatched_attempt_unknown_criteria_mislabeled_fixture_and_redacts_secrets() -> None:
    """Requirements 5, 6, 19: Attempt mismatch, unknown criteria, mislabeled fixture, secret redaction."""
    req = _sample_patch_request()
    valid_res = DeterministicPremiumDiscountPatchFixture().generate_patch(req)
    assert valid_res.proposal is not None

    # 1. Mismatched attempt number
    wrong_attempt = valid_res.model_copy(update={"attempt_number": 2})
    res_attempt = PatchGenerationAdapter(
        offline_fixture_provider=lambda _r: wrong_attempt
    ).generate(req)
    assert res_attempt.status == "error"
    assert res_attempt.error is not None
    assert res_attempt.error.code == "INVALID_PATCH_ATTEMPT_NUMBER"

    # 2. Unknown acceptance criterion reference
    bad_crit_proposal = valid_res.proposal.model_copy(
        update={"related_criterion_ids": ["AC-999"]}
    )
    bad_crit_res = valid_res.model_copy(update={"proposal": bad_crit_proposal})
    res_crit = PatchGenerationAdapter(
        offline_fixture_provider=lambda _r: bad_crit_res
    ).generate(req)
    assert res_crit.status == "error"
    assert res_crit.error is not None
    assert res_crit.error.code == "INVALID_CRITERION_REFERENCE"

    # 3. Offline fixture falsely claiming is_deterministic_fixture=False
    unlabeled_meta = PatchModelMetadata(
        provider="fake_ai",
        model_label="fake_ai_model",
        generation_mode="offline_test",
        is_deterministic_fixture=False,
        is_mock_output=False,
    )
    unlabeled_res = valid_res.model_copy(
        update={
            "model_metadata": unlabeled_meta,
            "is_deterministic_fixture": False,
            "is_mock_output": False,
        }
    )
    res_label = PatchGenerationAdapter(
        offline_fixture_provider=lambda _r: unlabeled_res
    ).generate(req)
    assert res_label.status == "error"
    assert res_label.error is not None
    assert res_label.error.code == "INVALID_FIXTURE_LABELING"

    # 4. Provider exception redacts secrets
    fake_secret = "gsk_" + "PatchProviderSecretLeak1234567890"

    def _exploding_provider(_r: PatchGenerationRequest) -> Any:
        raise RuntimeError(f"Failed calling upstream with key {fake_secret}")

    res_exc = PatchGenerationAdapter(
        offline_fixture_provider=_exploding_provider
    ).generate(req)
    assert res_exc.status == "error"
    assert res_exc.error is not None
    assert res_exc.error.code == "A4_PROVIDER_EXCEPTION"
    assert fake_secret not in res_exc.error.message
    assert "[REDACTED_GROQ_KEY]" in res_exc.error.message

    # 5. Schema rejects live_llm with is_deterministic_fixture=True directly
    with pytest.raises(ValidationError):
        PatchModelMetadata(
            model_label="fake",
            generation_mode="live_llm",
            is_deterministic_fixture=True,
            is_mock_output=False,
        )


# ---------------------------------------------------------------------------
# 8, 9, 10, 11, 12: Patch Validator & Applicator Security & Integrity Checks
# ---------------------------------------------------------------------------


def _prepare_isolated_workspace(
    tmp_path: Path,
    *,
    max_patch_size_bytes: int = 100_000,
    max_patch_changed_files: int = 10,
):
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_sec"
    _create_mini_pos_repo(repo, clean=True)
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-sec": repo},
        max_patch_size_bytes=max_patch_size_bytes,
        max_patch_changed_files=max_patch_changed_files,
    )
    wm = WorkspaceManager(cfg)
    ws_ctx = wm.prepare_workspace("run_sec_001", repo, source_path="mini_pos", base_ref="HEAD")
    snap = compute_source_snapshot_sha256(ws_ctx.workspace_repo_path, "mini_pos")
    diff = compute_git_diff_sha256(ws_ctx.workspace_repo_path, ws_ctx.effective_base_ref, "mini_pos")
    return cfg, repo, ws_ctx, snap, diff


def test_validator_rejects_invalid_syntax_binary_unsafe_mode_and_header_smuggling(
    tmp_path: Path,
) -> None:
    """Requirement 8: Invalid patch syntax, binary patches, symlink modes, header mismatch."""
    cfg, _repo, ws_ctx, snap, diff = _prepare_isolated_workspace(tmp_path)
    validator = PatchValidator(cfg)

    cases = [
        # Malformed text (not a diff)
        ("not a valid unified diff at all\n", "PATCH_INVALID_SYNTAX"),
        # Truncated hunk
        (
            "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n"
            "--- a/mini_pos/billing.py\n"
            "+++ b/mini_pos/billing.py\n"
            "@@ -1,3 +1,4 @@\n"
            " def calculate_total(price, quantity):\n",
            "PATCH_INVALID_SYNTAX",
        ),
        # Binary patch
        (
            "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n"
            "GIT binary patch\n"
            "literal 4\n"
            "LcmZQz0000100000\n",
            "PATCH_BINARY_REJECTED",
        ),
        # Symlink file mode 120000
        (
            "diff --git a/mini_pos/link.py b/mini_pos/link.py\n"
            "new file mode 120000\n"
            "--- /dev/null\n"
            "+++ b/mini_pos/link.py\n"
            "@@ -0,0 +1 @@\n"
            "+../../etc/passwd\n",
            "PATCH_UNSAFE_FILE_MODE",
        ),
        # Header smuggling (diff --git != +++ target)
        (
            "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n"
            "--- a/mini_pos/billing.py\n"
            "+++ b/.env\n"
            "@@ -1,1 +1,1 @@\n"
            "-x\n"
            "+y\n",
            "PATCH_INVALID_SYNTAX",
        ),
    ]

    for bad_diff, expected_code in cases:
        proposal = _make_proposal(
            bad_diff,
            affected_files=[PatchFileChange(path="mini_pos/billing.py")],
        )
        res = validator.validate(
            workspace_repo_path=ws_ctx.workspace_repo_path,
            proposal=proposal,
            source_path="mini_pos",
            base_ref=ws_ctx.effective_base_ref,
            allowed_modification_paths=["mini_pos"],
            expected_source_snapshot_sha256=snap,
            expected_git_diff_sha256=diff,
            valid_criterion_ids={"AC-001"},
        )
        assert res.valid is False
        assert any(v.code == expected_code for v in res.violations), (
            f"Expected {expected_code}, got {[v.code for v in res.violations]}"
        )


@pytest.mark.parametrize(
    ("target_path", "expected_code"),
    [
        ("../outside.py", "PATCH_PATH_TRAVERSAL"),
        ("mini_pos/../../escape.py", "PATCH_PATH_TRAVERSAL"),
        ("/etc/passwd", "PATCH_ABSOLUTE_PATH"),
        ("C:/Windows/win.ini", "PATCH_ABSOLUTE_PATH"),
        (".git/config", "PATCH_PROTECTED_PATH"),
        ("mini_pos/.env", "PATCH_PROTECTED_PATH"),
        ("mini_pos/secrets.py", "PATCH_PROTECTED_PATH"),
        ("project/marsadh_orchestrator/orchestrator.py", "PATCH_PROTECTED_PATH"),
        ("other_dir/file.py", "PATCH_PATH_OUTSIDE_SCOPE"),
    ],
)
def test_validator_rejects_path_traversal_absolute_protected_and_out_of_scope_paths(
    tmp_path: Path,
    target_path: str,
    expected_code: str,
) -> None:
    """Requirement 9: Path traversal, absolute paths, protected files, out-of-scope paths."""
    cfg, _repo, ws_ctx, snap, diff = _prepare_isolated_workspace(tmp_path)
    validator = PatchValidator(cfg)

    raw_diff = (
        f"diff --git a/{target_path} b/{target_path}\n"
        f"--- a/{target_path}\n"
        f"+++ b/{target_path}\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    # For absolute paths, craft raw --- / +++ without 'a/' prefix so validator catches absolute path
    if target_path.startswith("/") or target_path.startswith("C:"):
        raw_diff = (
            f"--- {target_path}\n"
            f"+++ {target_path}\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
        )

    proposal = _make_proposal(
        raw_diff,
        affected_files=[PatchFileChange(path=target_path)],
    )
    res = validator.validate(
        workspace_repo_path=ws_ctx.workspace_repo_path,
        proposal=proposal,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
        valid_criterion_ids={"AC-001"},
    )
    assert res.valid is False
    assert any(v.code == expected_code for v in res.violations), (
        f"For '{target_path}' expected {expected_code}, got {[v.code for v in res.violations]}"
    )


def test_validator_prevents_symlink_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 10: Symlink escape prevention (real symlink if OS permits, plus deterministic check)."""
    cfg, _repo, ws_ctx, snap, diff = _prepare_isolated_workspace(tmp_path)
    ws_repo = Path(ws_ctx.workspace_repo_path)
    outside_file = tmp_path / "outside_secret.py"
    outside_file.write_text("SECRET = 'do_not_touch'\n", encoding="utf-8")

    symlink_target = ws_repo / "mini_pos" / "linked_billing.py"
    created_real_symlink = False
    try:
        symlink_target.symlink_to(outside_file)
        created_real_symlink = True
    except OSError:
        # Windows without Developer Mode / SeCreateSymbolicLinkPrivilege: simulate symlink deterministically
        orig_is_symlink = Path.is_symlink

        def _fake_is_symlink(self: Path) -> bool:
            if self == symlink_target:
                return True
            return orig_is_symlink(self)

        monkeypatch.setattr(Path, "is_symlink", _fake_is_symlink)

    symlink_diff = (
        "diff --git a/mini_pos/linked_billing.py b/mini_pos/linked_billing.py\n"
        "--- a/mini_pos/linked_billing.py\n"
        "+++ b/mini_pos/linked_billing.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-SECRET = 'do_not_touch'\n"
        "+SECRET = 'pwned'\n"
    )
    proposal = _make_proposal(
        symlink_diff,
        affected_files=[PatchFileChange(path="mini_pos/linked_billing.py")],
    )
    validator = PatchValidator(cfg)
    res = validator.validate(
        workspace_repo_path=ws_repo,
        proposal=proposal,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
        valid_criterion_ids={"AC-001"},
    )
    assert res.valid is False
    assert any(v.code == "PATCH_SYMLINK_ESCAPE" for v in res.violations)
    assert outside_file.read_text(encoding="utf-8") == "SECRET = 'do_not_touch'\n"
    if created_real_symlink and symlink_target.is_symlink():
        symlink_target.unlink()


def test_validator_enforces_patch_size_and_changed_file_limits(tmp_path: Path) -> None:
    """Requirement 11: Patch size limits and changed-file count limits."""
    cfg, _repo, ws_ctx, snap, diff = _prepare_isolated_workspace(
        tmp_path,
        max_patch_size_bytes=1024,
        max_patch_changed_files=1,
    )
    validator = PatchValidator(cfg)

    # 1. Oversized patch (> 1024 bytes)
    huge_added = "".join(f"+# filler line {i:03d} " + ("x" * 40) + "\n" for i in range(30))
    oversized_diff = (
        "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n"
        "--- a/mini_pos/billing.py\n"
        "+++ b/mini_pos/billing.py\n"
        "@@ -1,1 +1,31 @@\n"
        " def calculate_total(price, quantity):\n"
        + huge_added
    )
    oversized_prop = _make_proposal(
        oversized_diff,
        affected_files=[PatchFileChange(path="mini_pos/billing.py")],
    )
    res_size = validator.validate(
        workspace_repo_path=ws_ctx.workspace_repo_path,
        proposal=oversized_prop,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
    )
    assert res_size.valid is False
    assert any(v.code == "PATCH_SIZE_LIMIT_EXCEEDED" for v in res_size.violations)

    # 2. Too many changed files (2 > limit 1)
    two_file_diff = (
        "diff --git a/mini_pos/billing.py b/mini_pos/billing.py\n"
        "--- a/mini_pos/billing.py\n"
        "+++ b/mini_pos/billing.py\n"
        "@@ -1,1 +1,2 @@\n"
        " def calculate_total(price, quantity):\n"
        "+    # c1\n"
        "diff --git a/mini_pos/refunds.py b/mini_pos/refunds.py\n"
        "--- a/mini_pos/refunds.py\n"
        "+++ b/mini_pos/refunds.py\n"
        "@@ -1,1 +1,2 @@\n"
        " \n"
        "+# c2\n"
    )
    two_file_prop = _make_proposal(
        two_file_diff,
        affected_files=[
            PatchFileChange(path="mini_pos/billing.py"),
            PatchFileChange(path="mini_pos/refunds.py"),
        ],
    )
    res_files = validator.validate(
        workspace_repo_path=ws_ctx.workspace_repo_path,
        proposal=two_file_prop,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
    )
    assert res_files.valid is False
    assert any(v.code == "PATCH_FILE_LIMIT_EXCEEDED" for v in res_files.violations)


def test_patch_hash_and_workspace_tampering_rejected_before_application(
    tmp_path: Path,
) -> None:
    """Requirement 12: Patch hash mismatch & workspace tampering between validation and apply."""
    cfg, _repo, ws_ctx, snap, diff = _prepare_isolated_workspace(tmp_path)
    req = _sample_patch_request()
    gen_res = DeterministicPremiumDiscountPatchFixture().generate_patch(req)
    assert gen_res.proposal is not None
    proposal = gen_res.proposal

    validator = PatchValidator(cfg)
    val_res = validator.validate(
        workspace_repo_path=ws_ctx.workspace_repo_path,
        proposal=proposal,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
        valid_criterion_ids={"AC-001", "AC-002"},
    )
    assert val_res.valid is True

    applicator = PatchApplicator(cfg)

    # 1. Tamper with proposal content after validation (different hash than validation_result.patch_sha256)
    tampered_diff = proposal.unified_diff.replace("0.90", "0.50")
    tampered_proposal = proposal.model_copy(
        update={
            "unified_diff": tampered_diff,
            "patch_sha256": compute_patch_sha256(tampered_diff),
        }
    )
    res_hash = applicator.apply(
        workspace_repo_path=ws_ctx.workspace_repo_path,
        proposal=tampered_proposal,
        validation_result=val_res,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
    )
    assert res_hash.applied is False
    assert res_hash.error is not None
    assert res_hash.error.code == "PATCH_HASH_MISMATCH"

    # 2. Tamper with workspace file between validation and application
    ws_billing = Path(ws_ctx.workspace_repo_path) / "mini_pos" / "billing.py"
    ws_billing.write_text(BASELINE_BILLING + "\n# tampered\n", encoding="utf-8")

    res_ws = applicator.apply(
        workspace_repo_path=ws_ctx.workspace_repo_path,
        proposal=proposal,
        validation_result=val_res,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
    )
    assert res_ws.applied is False
    assert res_ws.error is not None
    assert res_ws.error.code == "WORKSPACE_TAMPERED"

    # 3. Refuse to validate or apply against the original repository (outside workspace_root)
    res_orig = validator.validate(
        workspace_repo_path=_repo,
        proposal=proposal,
        source_path="mini_pos",
        base_ref="HEAD",
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=snap,
        expected_git_diff_sha256=diff,
    )
    assert res_orig.valid is False
    assert any(v.code == "WORKSPACE_OUTSIDE_ROOT" for v in res_orig.violations)


# ---------------------------------------------------------------------------
# 13: Failed Patch Application Without Partial Verification
# ---------------------------------------------------------------------------


def test_failed_patch_application_rolls_back_and_blocks_verification(
    tmp_path: Path,
) -> None:
    """Requirement 13: Mid-apply failure rolls back cleanly and prevents partial changes reaching M01/M03/M04."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_pos"
    _create_mini_pos_repo(clean_repo, clean=True)
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-pos": clean_repo},
    )

    def _partially_mutating_failing_git_apply(
        _cmd: list[str],
        *,
        cwd: str,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        # Simulate a partial write occurring before git apply fails with exit code 1
        target = Path(cwd) / "mini_pos" / "billing.py"
        target.write_text("# PARTIAL CORRUPTED WRITE\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=_cmd,
            returncode=1,
            stdout=b"",
            stderr=b"error: simulated partial patch application failure",
        )

    store = RunStore(cfg.database_path)
    svc = OrchestratorService(
        config=cfg,
        store=store,
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        patch_adapter=PatchGenerationAdapter(
            cfg,
            offline_fixture_provider=DeterministicPremiumDiscountPatchFixture(),
        ),
        patch_applicator=PatchApplicator(
            cfg,
            git_apply_runner=_partially_mutating_failing_git_apply,
        ),
        offline_model_callable=_behavioral_m03_stub,
    )

    run = svc.start_run(
        CreateRunRequest(
            repository_id="clean-pos",
            source_path="mini_pos",
            requirements=PREMIUM_DISCOUNT_REQUIREMENT,
            workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
            execution_mode=ExecutionMode.OFFLINE_TEST,
        )
    )

    assert run.state == WorkflowState.FAILED
    assert run.error is not None
    assert run.error.code == "PATCH_APPLICATION_FAILED"
    assert run.patch_application is not None
    assert run.patch_application.applied is False
    assert run.patch_application.status == "reverted"
    assert run.patch_application.rollback_performed is True
    assert run.patch_application.workspace_clean_after_rollback is True

    # Workspace file must be rolled back to exact baseline bytes
    assert run.workspace_path is not None
    ws_billing = Path(run.workspace_path) / "mini_pos" / "billing.py"
    assert ws_billing.read_text(encoding="utf-8") == BASELINE_BILLING

    # Post-patch M01, M03, and M04 MUST NEVER have executed
    assert run.codebase_analysis is None
    assert run.test_generation is None
    assert run.verification is None


# ---------------------------------------------------------------------------
# 14: Mandatory Unresolved Ambiguity Blocking
# ---------------------------------------------------------------------------


def test_mandatory_unresolved_ambiguity_blocks_patch_generation_before_a4(
    tmp_path: Path,
) -> None:
    """Requirement 14: Blocking requirement ambiguities halt before A4 is invoked."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_pos"
    _create_mini_pos_repo(clean_repo, clean=True)
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-pos": clean_repo},
    )

    a4_calls = 0

    def _counting_fixture(req: PatchGenerationRequest) -> PatchGenerationResult:
        nonlocal a4_calls
        a4_calls += 1
        return DeterministicPremiumDiscountPatchFixture().generate_patch(req)

    app = create_app(
        cfg,
        patch_offline_fixture_provider=_counting_fixture,
        offline_model_callable=_behavioral_m03_stub,
    )

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "clean-pos",
                "source_path": "mini_pos",
                "requirements": (
                    "Apply an appropriate discount to eligible customers somehow?"
                ),
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        detail = client.get(f"/api/runs/{run_id}").json()
        assert a4_calls == 0, "A4 adapter must never be called when blocking ambiguities exist"
        assert detail["state"] == WorkflowState.FAILED.value
        assert detail["error"]["code"] == "UNRESOLVED_REQUIREMENT_AMBIGUITY"
        assert detail["requirement_analysis"]["has_mandatory_unresolved_ambiguities"] is True
        assert detail["patch_generation"]["status"] == "blocked"
        assert detail["patch_validation"] is None
        assert detail["patch_application"] is None
        assert detail["codebase_analysis"] is None
        assert detail["test_generation"] is None


# ---------------------------------------------------------------------------
# 18: Persistence, Backward Compatibility, and Safe Restart Recovery
# ---------------------------------------------------------------------------


def test_persistence_backward_compatibility_and_safe_restart_recovery_for_patch_stages(
    tmp_path: Path,
) -> None:
    """Requirement 18: SQLite persistence, pre-P0-6 compatibility, safe interrupted patch recovery."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_pos"
    _create_mini_pos_repo(clean_repo, clean=True)
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-pos": clean_repo},
    )
    store = RunStore(cfg.database_path)

    # 1. Execute a generate_and_verify_change run to Gate 1, then reload via fresh RunStore
    svc = OrchestratorService(
        config=cfg,
        store=store,
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        patch_adapter=PatchGenerationAdapter(
            cfg,
            offline_fixture_provider=DeterministicPremiumDiscountPatchFixture(),
        ),
        offline_model_callable=_behavioral_m03_stub,
    )
    paused = svc.start_run(
        CreateRunRequest(
            repository_id="clean-pos",
            source_path="mini_pos",
            requirements=PREMIUM_DISCOUNT_REQUIREMENT,
            workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
            execution_mode=ExecutionMode.OFFLINE_TEST,
        )
    )
    assert paused.state == WorkflowState.AWAITING_TEST_REVIEW

    reloaded = RunStore(cfg.database_path).get_run(paused.run_id)
    assert reloaded is not None
    assert reloaded.workflow_mode == WorkflowMode.GENERATE_AND_VERIFY_CHANGE
    assert reloaded.patch_attempt_number == 1
    assert reloaded.patch_proposal_sha256 is not None
    assert reloaded.patch_generation_request is not None
    # Raw source file blobs stripped from persistence; file paths preserved
    assert reloaded.patch_generation_request.relevant_source_files == {}
    assert "mini_pos/billing.py" in reloaded.patch_generation_request.relevant_source_file_paths
    assert reloaded.patch_generation is not None
    assert reloaded.patch_validation is not None and reloaded.patch_validation.valid is True
    assert reloaded.patch_application is not None and reloaded.patch_application.applied is True
    assert reloaded.original_source_snapshot_sha256 != reloaded.source_snapshot_sha256

    # 2. Simulate interrupted runs in EVERY new active patch stage -> recover_interrupted_runs marks FAILED without replaying!
    interrupted_states = [
        WorkflowState.COLLECTING_PRE_CHANGE_CONTEXT,
        WorkflowState.GENERATING_PATCH,
        WorkflowState.VALIDATING_PATCH,
        WorkflowState.APPLYING_PATCH,
        WorkflowState.ANALYZING_POST_PATCH_IMPACT,
    ]
    for idx, st in enumerate(interrupted_states, start=1):
        store.save_run(
            WorkflowRunRecord(
                run_id=f"run_interrupted_patch_{idx}",
                state=st,
                requirements="Interrupted mid-patch stage",
                workflow_mode=WorkflowMode.GENERATE_AND_VERIFY_CHANGE,
                repository=RepositoryInfo(
                    repository_id="clean-pos",
                    repository_path=str(clean_repo),
                    source_path="mini_pos",
                    base_ref="HEAD",
                ),
            )
        )

    a4_replay_calls = 0

    def _never_replay(_req: PatchGenerationRequest) -> Any:
        nonlocal a4_replay_calls
        a4_replay_calls += 1
        raise AssertionError("Restart recovery must never replay A4 generation calls")

    recovered_svc = OrchestratorService(
        config=cfg,
        store=RunStore(cfg.database_path),
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        patch_adapter=PatchGenerationAdapter(cfg, offline_fixture_provider=_never_replay),
        offline_model_callable=_behavioral_m03_stub,
    )
    recovered = recovered_svc.recover_interrupted_runs()
    assert a4_replay_calls == 0
    assert len(recovered) == len(interrupted_states)
    assert all(r.state == WorkflowState.FAILED for r in recovered)
    assert all(r.error is not None and r.error.code == "PROCESS_INTERRUPTED" for r in recovered)
    # Paused Gate 1 run remains untouched & resumable after restart!
    assert recovered_svc.get_run(paused.run_id).state == WorkflowState.AWAITING_TEST_REVIEW

    # 3. Pre-P0-6 legacy persisted JSON (lacking workflow_mode and all patch_* fields) loads cleanly
    legacy_p05_json = json.dumps(
        {
            "run_id": "run_legacy_p05_001",
            "state": "APPROVED",
            "requirements": "Legacy P0-5 run without workflow_mode or patch fields.",
            "execution_mode": "offline_test",
            "repository": {
                "repository_id": "clean-pos",
                "repository_path": str(clean_repo),
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "change_mode": "working_tree",
            },
        }
    )
    legacy_record = WorkflowRunRecord.model_validate_json(legacy_p05_json)
    assert legacy_record.workflow_mode == WorkflowMode.VERIFY_EXISTING_CHANGE
    assert legacy_record.patch_generation is None
    assert legacy_record.patch_validation is None
    assert legacy_record.patch_application is None
    assert legacy_record.pre_change_context is None
