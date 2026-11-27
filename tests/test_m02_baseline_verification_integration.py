"""Comprehensive offline test suite & E2E demonstration for P0-7 Baseline Verification Integration.

Covers all 22 required P0-7 verification areas, Phase 1 P0-6 rollback regression
fixes, and the Phase 9 controlled premium-customer discount offline end-to-end
demonstration (Baseline vs Implementation A [Correct] vs Implementation B [Regressive]):

1.  Baseline workspace preparation from the exact resolved Git base commit
    (both ``working_tree`` and ``committed_range`` modes, secret exclusion, cleanup).
2.  Existing-change workflow compatibility (``verify_existing_change``).
3.  Generated-change workflow compatibility (``generate_and_verify_change``).
4.  Identical approved test SHA-256 hashes across Gate 1, baseline, and modified snapshots.
5.  Pass-to-pass comparison (``preserved_behavior``).
6.  Pass-to-fail comparison (``newly_introduced_failure``).
7.  Fail-to-pass acceptance criteria (``newly_satisfied_acceptance_criterion`` vs
    ``fail_to_pass_unclassified`` when ``test_purpose`` is ``unknown``).
8.  Fail-to-pass existing regression tests (``fixed_existing_failure``) & separate
    existing repository regression test suite discovery/execution.
9.  Fail-to-fail comparison (``persistent_failure``).
10. Collection errors (never classified as application regression; ``not_comparable``).
11. Missing test identities (``unavailable`` -> ``comparison_status=incomplete``).
12. Aggregate-only M04 results (never infers passing tests from pass counts).
13. Baseline timeout and unavailable baseline workspace (preserves modified results,
    marks comparison ``incomplete``).
14. Modified workspace tampering before execution.
15. Baseline workspace tampering before execution.
16. Strictly no test execution before Gate 1 approval (nor on Gate 1 rejection).
17. Approval blocking for confirmed regressions (both generated and existing repo suites).
18. Incomplete requirement coverage handling & distinction between ordinary approval
    and complete requirement assurance.
19. Persistence, pre-P0-7 backward compatibility, and safe restart recovery across all
    new baseline verification stages without replaying execution.
20. Original repository integrity and temporary baseline workspace cleanup.
21. API compatibility, zero host path exposure, and OpenAPI parity.
22. Secret redaction across baseline/modified errors, API responses, logs, and SQLite.
23. Phase 1 P0-6 rollback defect regression tests (Git index restoration + empty dir cleanup).
24. Phase 9 Offline E2E Demonstration: Premium-customer discount fixture comparing
    Baseline vs Implementation A (Correct) and Baseline vs Implementation B (Regressive).
"""

from __future__ import annotations

import hashlib
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
    BaselineComparisonEngine,
    DeterministicPremiumDiscountPatchFixture,
    OrchestratorConfig,
    OrchestratorService,
    PatchApplicator,
    PatchGenerationAdapter,
    PatchValidator,
    ReviewConflictError,
    RunStore,
    WorkflowIntegrityError,
    WorkspaceManager,
    build_test_evidence_from_aggregate_result,
    compute_git_diff_sha256,
    compute_source_snapshot_sha256,
)
from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
)
from project.schemas import (
    ApprovedTestSpec,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    CreateRunRequest,
    ExecutionMode,
    FailureDiagnostic,
    HumanDecision,
    HumanDecisionRequest,
    IndividualTestOutcome,
    ModifiedVerificationResult,
    PatchFileChange,
    PatchProposal,
    RegressionComparisonSummary,
    RepositoryInfo,
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
    "    # Incorrect: applies 10% discount to EVERY customer instead of premium only\n"
    "    return total * 0.90\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for P0-7 baseline verification tests")
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
    mode: str = "correct",
    committed_change: bool = False,
    existing_test_code: str | None = None,
) -> tuple[str, str]:
    """Create a controlled mini_pos Git fixture returning ``(baseline_commit, head_commit)``."""
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
    _git(repo, "config", "user.name", "ChangeGuard P0-7")
    _git(repo, "config", "user.email", "p07@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline mini_pos without premium discount")
    baseline_commit = _git(repo, "rev-parse", "HEAD")

    if mode == "correct":
        (app_dir / "billing.py").write_text(CORRECT_BILLING, encoding="utf-8")
    elif mode == "regressive":
        (app_dir / "billing.py").write_text(REGRESSIVE_BILLING, encoding="utf-8")
    elif mode != "clean":
        raise ValueError(f"Unsupported fixture mode: {mode}")

    if committed_change and mode != "clean":
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", f"apply {mode} billing implementation")

    head_commit = _git(repo, "rev-parse", "HEAD")
    return baseline_commit, head_commit


def _make_config(
    tmp_path: Path,
    *,
    repos_root: Path,
    configured: dict[str, Path],
    db_name: str = "p07.sqlite3",
    timeout: int = 30,
    trusted: bool = True,
) -> OrchestratorConfig:
    return OrchestratorConfig(
        database_path=tmp_path / "state" / db_name,
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories=configured,
        trusted_execution_roots=[repos_root] if trusted else [],
        verification_timeout_seconds=timeout,
        environment="test",
    )


def _common_premium_discount_test_suite(
    *,
    include_criterion_ids: bool = True,
    include_explicit_purpose: bool = False,
    marker_path: Path | None = None,
):
    """Common deterministic test suite for the Phase 9 premium-customer discount demo.

    Contains:
    - SCN-001 (AC-001, acceptance): Premium customer receives 10% discount
      (expected FAIL on baseline, PASS on Impl A & Impl B).
    - SCN-002 (AC-002, preservation): Regular customer checkout remains unchanged
      (expected PASS on baseline, PASS on Impl A, FAIL [regression!] on Impl B).
    - SCN-003 (AC-003, preservation): Dependent refund, dashboard, and sales report
      modules preserve regular-customer totals
      (expected PASS on baseline, PASS on Impl A, FAIL [regression!] on Impl B).
    - SCN-004 (AC-001, acceptance): Dependent refund and sales report reflect 10%
      discount for premium customers
      (expected FAIL on baseline, PASS on Impl A & Impl B).
    """
    header = (
        "import pathlib, sys\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
    )
    if marker_path is not None:
        header = "import os\n" + header
    marker_stmt = (
        "    pathlib.Path(os.environ['CHANGEGUARD_TEST_MARKER']).write_text('executed', encoding='utf-8')\n"
        if marker_path is not None
        else ""
    )

    def _stub(_prompt: str) -> str:
        s1: dict[str, Any] = {
            "title": "Premium customer checkout receives 10% discount (Acceptance)",
            "rationale": "Deterministic offline acceptance test verifying AC-001.",
            "assumptions": "Deterministic offline demonstration fixture; premium pays 90%.",
            "test_code": (
                header
                + "from billing import apply_discount, checkout\n\n"
                + "def test_premium_customer_discount_acceptance():\n"
                + marker_stmt
                + "    assert apply_discount(200.0, 'premium') == pytest.approx(180.0)\n"
                + "    assert checkout(50.0, 4, 'premium') == pytest.approx(180.0)\n"
            ),
        }
        s2: dict[str, Any] = {
            "title": "Regular customer checkout remains undiscounted (Preservation)",
            "rationale": "Deterministic offline preservation test verifying AC-002.",
            "assumptions": "Deterministic offline demonstration fixture; regular pays 100%.",
            "test_code": (
                header
                + "from billing import apply_discount, checkout\n\n"
                + "def test_regular_customer_preservation():\n"
                + marker_stmt
                + "    assert apply_discount(200.0, 'regular') == pytest.approx(200.0)\n"
                + "    assert checkout(50.0, 4, 'regular') == pytest.approx(200.0)\n"
            ),
        }
        s3: dict[str, Any] = {
            "title": "Dependent refund, dashboard, and report modules preserve regular customers (Preservation)",
            "rationale": "Deterministic offline preservation test verifying AC-003.",
            "assumptions": "Deterministic offline demonstration fixture for regular downstream flows.",
            "test_code": (
                header
                + "from dashboard import display_refund\n"
                + "from reports import generate_sales_report\n\n"
                + "def test_downstream_regular_customer_preservation():\n"
                + "    assert display_refund(100.0, 'regular') == {'refund_amount': pytest.approx(100.0)}\n"
                + "    assert generate_sales_report(100.0, 2, 'regular') == {'total_sales': pytest.approx(200.0)}\n"
            ),
        }
        s4: dict[str, Any] = {
            "title": "Dependent refund and sales report reflect 10% premium discount (Acceptance)",
            "rationale": "Deterministic offline acceptance test verifying premium discount propagates downstream.",
            "assumptions": "Deterministic offline demonstration fixture for premium downstream flows.",
            "test_code": (
                header
                + "from dashboard import display_refund\n"
                + "from reports import generate_sales_report\n\n"
                + "def test_downstream_premium_discount_acceptance():\n"
                + "    assert display_refund(100.0, 'premium') == {'refund_amount': pytest.approx(90.0)}\n"
                + "    assert generate_sales_report(100.0, 2, 'premium') == {'total_sales': pytest.approx(180.0)}\n"
            ),
        }
        if include_criterion_ids:
            s1["criterion_ids"] = ["AC-001"]
            s2["criterion_ids"] = ["AC-002"]
            s3["criterion_ids"] = ["AC-003"]
            s4["criterion_ids"] = ["AC-001"]
        if include_explicit_purpose:
            s1["test_purpose"] = TestPurpose.NEW_REQUIREMENT_ACCEPTANCE.value
            s2["test_purpose"] = TestPurpose.EXISTING_BEHAVIOR_PRESERVATION.value
            s3["test_purpose"] = TestPurpose.EXISTING_BEHAVIOR_PRESERVATION.value
            s4["test_purpose"] = TestPurpose.NEW_REQUIREMENT_ACCEPTANCE.value
        return json.dumps({"scenarios": [s1, s2, s3, s4]})

    return _stub


# ---------------------------------------------------------------------------
# 1 & 20: Baseline Workspace Preparation (working_tree & committed_range) & Integrity
# ---------------------------------------------------------------------------


def test_baseline_workspace_preparation_working_tree_and_committed_range(
    tmp_path: Path,
) -> None:
    """Req 1 & 20: Baseline workspace reflects exact resolved base commit, not modified working tree."""
    repos_root = tmp_path / "repos"
    repo_wt = repos_root / "wt_repo"
    repo_cr = repos_root / "cr_repo"

    base_wt, head_wt = _create_mini_pos_repo(repo_wt, mode="correct", committed_change=False)
    base_cr, head_cr = _create_mini_pos_repo(repo_cr, mode="correct", committed_change=True)
    assert base_wt == head_wt
    assert base_cr != head_cr

    # Place an untracked secret .env outside source_path to confirm it is never transferred
    (repo_wt / ".env").write_text("SECRET_KEY=gsk_ShouldNeverBeCopied1234567890\n", encoding="utf-8")

    orig_wt_tree = _snapshot_tree_bytes(repo_wt)
    orig_cr_tree = _snapshot_tree_bytes(repo_cr)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"wt": repo_wt, "cr": repo_cr},
    )
    wm = WorkspaceManager(cfg)

    # Case A: working_tree change relative to HEAD
    mod_wt = wm.prepare_workspace("run_wt_001", repo_wt, source_path="mini_pos", base_ref="HEAD")
    base_ws_wt = wm.prepare_baseline_workspace(
        "run_wt_001",
        repo_wt,
        mod_wt.resolved_base_commit,
        source_path="mini_pos",
        base_ref="HEAD",
    )
    assert base_ws_wt.workspace_role == "baseline"
    assert base_ws_wt.workspace_repo_path != mod_wt.workspace_repo_path
    assert base_ws_wt.resolved_base_commit == base_wt
    assert base_ws_wt.is_trusted_for_execution is True
    assert (Path(base_ws_wt.workspace_repo_path) / "mini_pos" / "billing.py").read_text(
        encoding="utf-8"
    ) == BASELINE_BILLING
    assert (Path(mod_wt.workspace_repo_path) / "mini_pos" / "billing.py").read_text(
        encoding="utf-8"
    ) == CORRECT_BILLING
    assert not (Path(base_ws_wt.workspace_repo_path) / ".env").exists()

    # Case B: committed_range change relative to HEAD~1
    mod_cr = wm.prepare_workspace("run_cr_001", repo_cr, source_path="mini_pos", base_ref="HEAD~1")
    assert mod_cr.change_mode == "committed_range"
    assert mod_cr.resolved_base_commit == base_cr
    base_ws_cr = wm.prepare_baseline_workspace(
        "run_cr_001",
        repo_cr,
        mod_cr.resolved_base_commit,
        source_path="mini_pos",
        base_ref="HEAD~1",
    )
    assert (Path(base_ws_cr.workspace_repo_path) / "mini_pos" / "billing.py").read_text(
        encoding="utf-8"
    ) == BASELINE_BILLING
    assert (Path(mod_cr.workspace_repo_path) / "mini_pos" / "billing.py").read_text(
        encoding="utf-8"
    ) == CORRECT_BILLING

    # Safe cleanup removes baseline workspaces cleanly while leaving original repos untouched
    assert wm.cleanup_workspace(base_ws_wt.workspace_repo_path) is True
    assert wm.cleanup_workspace(base_ws_cr.workspace_repo_path) is True
    assert not Path(base_ws_wt.workspace_repo_path).exists()
    assert not Path(base_ws_cr.workspace_repo_path).exists()
    assert _snapshot_tree_bytes(repo_wt) == orig_wt_tree
    assert _snapshot_tree_bytes(repo_cr) == orig_cr_tree


# ---------------------------------------------------------------------------
# 5, 6, 7, 8, 9, 10, 11, 12: Deterministic Comparison Engine & Schema Invariants
# ---------------------------------------------------------------------------


def test_comparison_engine_all_deterministic_classifications_and_schema_truthfulness() -> None:
    """Reqs 5-12: Exact deterministic classification matrix, collection errors, missing IDs, aggregate-only M04."""
    sha_a = "a" * 64
    sha_b = "b" * 64
    sha_c = "c" * 64
    sha_d = "d" * 64
    sha_e = "e" * 64
    sha_f = "f" * 64
    sha_col = "1" * 64
    sha_hash_base = "2" * 64
    sha_hash_mod = "3" * 64

    baseline = BaselineVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        baseline_ref="HEAD",
        baseline_commit="deadbeef" * 5,
        exit_code=1,
        total_tests=7,
        passed=3,
        failed=4,
        test_evidence=[
            # 5. Pass -> Pass (preserved behavior)
            TestExecutionEvidence(
                test_identity="SCN-001",
                scenario_id="SCN-001",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_a,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            # 6. Pass -> Fail (newly introduced failure)
            TestExecutionEvidence(
                test_identity="SCN-002",
                scenario_id="SCN-002",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_b,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            # 7a. Fail -> Pass with NEW_REQUIREMENT_ACCEPTANCE
            TestExecutionEvidence(
                test_identity="SCN-003",
                scenario_id="SCN-003",
                test_purpose=TestPurpose.NEW_REQUIREMENT_ACCEPTANCE,
                test_source_sha256=sha_c,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
            # 7b. Fail -> Pass with UNKNOWN purpose (must NOT be described as repaired regression!)
            TestExecutionEvidence(
                test_identity="SCN-004",
                scenario_id="SCN-004",
                test_purpose=TestPurpose.UNKNOWN,
                test_source_sha256=sha_d,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
            # 8. Fail -> Pass with EXISTING_REPOSITORY_REGRESSION -> fixed_existing_failure
            TestExecutionEvidence(
                test_identity="SCN-005",
                scenario_id="SCN-005",
                test_purpose=TestPurpose.EXISTING_REPOSITORY_REGRESSION,
                test_source_sha256=sha_e,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
            # 9. Fail -> Fail -> persistent_failure
            TestExecutionEvidence(
                test_identity="SCN-006",
                scenario_id="SCN-006",
                test_purpose=TestPurpose.NEW_REQUIREMENT_ACCEPTANCE,
                test_source_sha256=sha_f,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
            # 10. Collection error on modified (must NOT become regression!)
            TestExecutionEvidence(
                test_identity="SCN-007",
                scenario_id="SCN-007",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_col,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            # Hash mismatch across snapshots -> NOT_COMPARABLE
            TestExecutionEvidence(
                test_identity="SCN-008",
                scenario_id="SCN-008",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_hash_base,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            # 11. Missing on modified -> UNAVAILABLE
            TestExecutionEvidence(
                test_identity="SCN-009",
                scenario_id="SCN-009",
                test_purpose=TestPurpose.UNKNOWN,
                test_source_sha256=sha_a,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
        ],
    )

    modified = ModifiedVerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        exit_code=1,
        total_tests=7,
        passed=4,
        failed=2,
        test_evidence=[
            TestExecutionEvidence(
                test_identity="SCN-001",
                scenario_id="SCN-001",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_a,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-002",
                scenario_id="SCN-002",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_b,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-003",
                scenario_id="SCN-003",
                test_purpose=TestPurpose.NEW_REQUIREMENT_ACCEPTANCE,
                test_source_sha256=sha_c,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-004",
                scenario_id="SCN-004",
                test_purpose=TestPurpose.UNKNOWN,
                test_source_sha256=sha_d,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-005",
                scenario_id="SCN-005",
                test_purpose=TestPurpose.EXISTING_REPOSITORY_REGRESSION,
                test_source_sha256=sha_e,
                outcome=IndividualTestOutcome.PASSED,
                exit_code=0,
                total_tests=1,
                passed=1,
                failed=0,
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-006",
                scenario_id="SCN-006",
                test_purpose=TestPurpose.NEW_REQUIREMENT_ACCEPTANCE,
                test_source_sha256=sha_f,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-007",
                scenario_id="SCN-007",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_col,
                outcome=IndividualTestOutcome.COLLECTION_ERROR,
                exit_code=2,
                collection_errors=["ERROR collecting test_generated_007.py"],
                evidence_granularity="single_scenario_file",
            ),
            TestExecutionEvidence(
                test_identity="SCN-008",
                scenario_id="SCN-008",
                test_purpose=TestPurpose.EXISTING_BEHAVIOR_PRESERVATION,
                test_source_sha256=sha_hash_mod,
                outcome=IndividualTestOutcome.FAILED,
                exit_code=1,
                total_tests=1,
                passed=0,
                failed=1,
                evidence_granularity="single_scenario_file",
            ),
        ],
    )

    approved_hashes = {
        "SCN-001": sha_a,
        "SCN-002": sha_b,
        "SCN-003": sha_c,
        "SCN-004": sha_d,
        "SCN-005": sha_e,
        "SCN-006": sha_f,
        "SCN-007": sha_col,
        "SCN-008": sha_hash_base,
        "SCN-009": sha_a,
    }

    evidence = BaselineComparisonEngine.compare(
        baseline=baseline,
        modified=modified,
        approved_test_hashes=approved_hashes,
    )
    by_id = {tc.test_identity: tc for tc in evidence.test_comparisons}

    assert by_id["SCN-001"].classification == TestComparisonClassification.PRESERVED_BEHAVIOR
    assert by_id["SCN-002"].classification == TestComparisonClassification.NEWLY_INTRODUCED_FAILURE
    assert by_id["SCN-002"].is_mandatory_regression is True
    assert by_id["SCN-003"].classification == TestComparisonClassification.NEWLY_SATISFIED_ACCEPTANCE_CRITERION
    assert by_id["SCN-004"].classification == TestComparisonClassification.FAIL_TO_PASS_UNCLASSIFIED
    assert "not automatically classified" in by_id["SCN-004"].rationale.lower()
    assert by_id["SCN-005"].classification == TestComparisonClassification.FIXED_EXISTING_FAILURE
    assert by_id["SCN-006"].classification == TestComparisonClassification.PERSISTENT_FAILURE
    assert by_id["SCN-007"].classification == TestComparisonClassification.NOT_COMPARABLE
    assert by_id["SCN-007"].is_mandatory_regression is False
    assert by_id["SCN-008"].classification == TestComparisonClassification.NOT_COMPARABLE
    assert by_id["SCN-008"].test_hashes_match is False
    assert by_id["SCN-009"].classification == TestComparisonClassification.UNAVAILABLE

    assert evidence.status == VerificationComparisonStatus.INCOMPLETE
    assert evidence.approved_test_hashes_verified is False
    assert evidence.summary.has_confirmed_regression is True
    assert evidence.summary.blocks_ordinary_approval is True
    assert evidence.summary.regression_safety_established is False
    assert evidence.summary.newly_introduced_failures == ["SCN-002"]
    assert evidence.newly_failing_tests == ["SCN-002"]
    assert evidence.summary.preserved_behaviors == ["SCN-001"]
    assert evidence.summary.newly_satisfied_acceptance_criteria == ["SCN-003"]
    assert evidence.summary.fail_to_pass_unclassified == ["SCN-004"]
    assert evidence.summary.fixed_existing_failures == ["SCN-005"]
    assert evidence.summary.persistent_failures == ["SCN-006"]
    assert set(evidence.summary.unavailable_or_incomparable) == {
        "SCN-007",
        "SCN-008",
        "SCN-009",
    }

    # Req 12: Aggregate-only M04 results NEVER infer passing tests from aggregate pass counts
    specs = {
        "SCN-001": ApprovedTestSpec(
            scenario_id="SCN-001",
            relative_path=".changeguard_generated_tests/test_generated_001.py",
            expected_sha256=sha_a,
        ),
        "SCN-002": ApprovedTestSpec(
            scenario_id="SCN-002",
            relative_path=".changeguard_generated_tests/test_generated_002.py",
            expected_sha256=sha_b,
        ),
    }
    agg_m04_result = VerificationResult(
        execution_status="completed",
        verification_outcome=VerificationOutcome.FAILED_TESTS,
        verification_passed=False,
        executed_test_ids=["SCN-001", "SCN-002"],
        exit_code=1,
        total_tests=2,
        passed=1,
        failed=1,
        failures=[
            FailureDiagnostic(
                test_name=".changeguard_generated_tests/test_generated_001.py::test_x",
                status="failed",
                evidence="AssertionError",
            )
        ],
    )
    agg_ev = build_test_evidence_from_aggregate_result(
        agg_m04_result,
        expected_test_ids=["SCN-001", "SCN-002"],
        approved_specs=specs,
    )
    agg_by_id = {ev.test_identity: ev for ev in agg_ev}
    assert agg_by_id["SCN-001"].outcome == IndividualTestOutcome.FAILED
    # SCN-002 has NO passed nodeid in M04 output -> MUST be UNKNOWN, NOT PASSED!
    assert agg_by_id["SCN-002"].outcome == IndividualTestOutcome.UNKNOWN

    agg_cmp = BaselineComparisonEngine.compare(
        baseline=BaselineVerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.FAILED_TESTS,
            exit_code=1,
            total_tests=2,
            passed=1,
            failed=1,
            test_evidence=agg_ev,
        ),
        modified=ModifiedVerificationResult(
            execution_status="completed",
            verification_outcome=VerificationOutcome.FAILED_TESTS,
            verification_passed=False,
            exit_code=1,
            total_tests=2,
            passed=1,
            failed=1,
            test_evidence=agg_ev,
        ),
        approved_test_hashes={"SCN-001": sha_a, "SCN-002": sha_b},
    )
    assert agg_cmp.status == VerificationComparisonStatus.INCOMPLETE
    assert agg_cmp.summary.unknown_count == 1
    assert agg_cmp.summary.regression_safety_established is False

    # Schema truthfulness checks reject invalid/fabricated claims directly
    with pytest.raises(ValidationError, match="collection error"):
        TestComparisonResult(
            test_identity="SCN-ERR",
            baseline_outcome=IndividualTestOutcome.PASSED,
            modified_outcome=IndividualTestOutcome.COLLECTION_ERROR,
            test_hashes_match=True,
            classification=TestComparisonClassification.NEWLY_INTRODUCED_FAILURE,
        )

    with pytest.raises(ValidationError, match="regression_safety_established"):
        RegressionComparisonSummary(
            comparison_status=VerificationComparisonStatus.INCOMPLETE,
            comparable_count=1,
            unknown_count=1,
            regression_safety_established=True,
        )


# ---------------------------------------------------------------------------
# 2, 4, 16, 17, 18, 20, 21, 24: Phase 9 Offline End-to-End Demonstration
# ---------------------------------------------------------------------------


def test_phase9_offline_e2e_premium_discount_demo_correct_vs_regressive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 9 E2E Demo + Reqs 2, 4, 16, 17, 18, 20, 21: Real pytest on baseline vs Impl A & Impl B."""
    repos_root = tmp_path / "repos"
    repo_a = repos_root / "pos_impl_a_correct"
    repo_b = repos_root / "pos_impl_b_regressive"

    base_a, _ = _create_mini_pos_repo(repo_a, mode="correct")
    base_b, _ = _create_mini_pos_repo(repo_b, mode="regressive")
    orig_a_tree = _snapshot_tree_bytes(repo_a)
    orig_b_tree = _snapshot_tree_bytes(repo_b)

    pre_gate1_marker = tmp_path / "no_exec_before_gate1.txt"
    monkeypatch.setenv("CHANGEGUARD_TEST_MARKER", str(pre_gate1_marker))
    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-impl-a": repo_a, "pos-impl-b": repo_b},
    )
    app = create_app(
        cfg,
        offline_model_callable=_common_premium_discount_test_suite(
            include_criterion_ids=True,
            marker_path=pre_gate1_marker,
        ),
        offline_model_label="deterministic_offline_demo_suite",
    )

    with TestClient(app) as client:
        # ==== IMPLEMENTATION A: Correct premium-only 10% discount ====
        run_a_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-impl-a",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.VERIFY_EXISTING_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_a_id, timeout=30.0)

        # Req 16: Strictly NO test execution before Gate 1 approval
        assert not pre_gate1_marker.exists()
        gate1_a = client.get(f"/api/runs/{run_a_id}").json()
        assert gate1_a["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert gate1_a["baseline_verification"] is None
        assert gate1_a["modified_verification"] is None
        assert gate1_a["baseline_comparison"] is None

        # Approve all 4 scenarios in the common deterministic test suite
        rev_a = client.post(
            f"/api/runs/{run_a_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003", "SCN-004"],
                "reviewer": "marsadh",
                "notes": "Approve common deterministic baseline-vs-modified suite for Impl A.",
            },
        )
        assert rev_a.status_code == 202
        assert rev_a.json()["state"] == WorkflowState.PREPARING_BASELINE.value
        app.state.executor.wait_for_run(run_a_id, timeout=60.0)
        assert pre_gate1_marker.exists()

        gate2_a_res = client.get(f"/api/runs/{run_a_id}")
        assert gate2_a_res.status_code == 200
        # Req 21: Zero host filesystem path leakage in API response
        assert str(tmp_path) not in gate2_a_res.text
        assert "baseline_workspace_path" not in gate2_a_res.json()
        assert "workspace_path" not in gate2_a_res.json()

        gate2_a = gate2_a_res.json()
        assert gate2_a["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value

        # Verify actual observed baseline vs modified results on Implementation A
        base_ver_a = gate2_a["baseline_verification"]
        mod_ver_a = gate2_a["modified_verification"]
        cmp_a = gate2_a["baseline_comparison"]
        assert base_ver_a["execution_status"] == "completed"
        assert base_ver_a["baseline_commit"] == base_a
        assert (base_ver_a["total_tests"], base_ver_a["passed"], base_ver_a["failed"]) == (4, 2, 2)
        assert mod_ver_a["execution_status"] == "completed"
        assert mod_ver_a["verification_passed"] is True
        assert (mod_ver_a["total_tests"], mod_ver_a["passed"], mod_ver_a["failed"]) == (4, 4, 0)

        # Req 4: Identical approved test SHA-256 hashes verified across Gate 1, baseline, and modified
        approved_hashes_a = gate2_a["test_review"]["approved_test_hashes"]
        assert base_ver_a["approved_test_hashes"] == approved_hashes_a
        assert mod_ver_a["approved_test_hashes"] == approved_hashes_a
        assert cmp_a["approved_test_hashes_verified"] is True
        assert cmp_a["status"] == VerificationComparisonStatus.COMPLETED.value

        by_id_a = {tc["test_identity"]: tc for tc in cmp_a["test_comparisons"]}
        assert by_id_a["SCN-001"]["classification"] == "newly_satisfied_acceptance_criterion"
        assert by_id_a["SCN-002"]["classification"] == "preserved_behavior"
        assert by_id_a["SCN-003"]["classification"] == "preserved_behavior"
        assert by_id_a["SCN-004"]["classification"] == "newly_satisfied_acceptance_criterion"
        assert cmp_a["summary"]["has_confirmed_regression"] is False
        assert cmp_a["summary"]["newly_introduced_failure_count"] == 0
        assert cmp_a["summary"]["preserved_behavior_count"] == 2
        assert cmp_a["summary"]["newly_satisfied_acceptance_count"] == 2

        # Req 18: Ordinary approval is allowed for Impl A, while complete_requirement_assurance stays False (offline fixture!)
        rep_a = gate2_a["report"]
        assert rep_a["recommendation_label"] == "ready_for_human_review"
        assert rep_a["complete_requirement_assurance"] is False
        assert rep_a["traceability"]["requirement_verification_proven"] is False
        assert rep_a["traceability"]["baseline_regression_comparison_status"] == "completed"
        assert "submit_final_decision:approve" in gate2_a["allowed_actions"]

        dec_a = client.post(
            f"/api/runs/{run_a_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Impl A verified: 2 acceptance criteria newly satisfied, 2 preservation tests preserved, 0 regressions.",
            },
        )
        assert dec_a.status_code == 200
        assert dec_a.json()["state"] == WorkflowState.APPROVED.value

        # Verify state progression in logs
        logs_a = client.get(f"/api/runs/{run_a_id}/logs").json()["logs"]
        stages_a = [entry["stage"] for entry in logs_a]
        for expected_stage in (
            WorkflowState.PREPARING_BASELINE.value,
            WorkflowState.RUNNING_BASELINE_VERIFICATION.value,
            WorkflowState.RUNNING_MODIFIED_VERIFICATION.value,
            WorkflowState.COMPARING_VERIFICATION_RESULTS.value,
            WorkflowState.COMPILING_REPORT.value,
        ):
            assert expected_stage in stages_a

        # ==== IMPLEMENTATION B: Incorrect (applies 10% discount to EVERY customer) ====
        run_b_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-impl-b",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.VERIFY_EXISTING_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_b_id, timeout=30.0)

        rev_b = client.post(
            f"/api/runs/{run_b_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003", "SCN-004"],
                "reviewer": "marsadh",
                "notes": "Approve identical suite against Impl B (regressive).",
            },
        )
        assert rev_b.status_code == 202
        app.state.executor.wait_for_run(run_b_id, timeout=60.0)

        gate2_b = client.get(f"/api/runs/{run_b_id}").json()
        assert gate2_b["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value

        base_ver_b = gate2_b["baseline_verification"]
        mod_ver_b = gate2_b["modified_verification"]
        cmp_b = gate2_b["baseline_comparison"]
        assert base_ver_b["baseline_commit"] == base_b
        assert (base_ver_b["total_tests"], base_ver_b["passed"], base_ver_b["failed"]) == (4, 2, 2)
        assert (mod_ver_b["total_tests"], mod_ver_b["passed"], mod_ver_b["failed"]) == (4, 2, 2)
        assert cmp_b["status"] == VerificationComparisonStatus.COMPLETED.value

        by_id_b = {tc["test_identity"]: tc for tc in cmp_b["test_comparisons"]}
        assert by_id_b["SCN-001"]["classification"] == "newly_satisfied_acceptance_criterion"
        assert by_id_b["SCN-002"]["classification"] == "newly_introduced_failure"
        assert by_id_b["SCN-002"]["is_mandatory_regression"] is True
        assert by_id_b["SCN-003"]["classification"] == "newly_introduced_failure"
        assert by_id_b["SCN-003"]["is_mandatory_regression"] is True
        assert by_id_b["SCN-004"]["classification"] == "newly_satisfied_acceptance_criterion"

        assert cmp_b["summary"]["has_confirmed_regression"] is True
        assert cmp_b["summary"]["blocks_ordinary_approval"] is True
        assert cmp_b["summary"]["newly_introduced_failures"] == ["SCN-002", "SCN-003"]
        assert gate2_b["report"]["recommendation"] == "reject"
        assert gate2_b["report"]["recommendation_label"] == "verification_failed"
        assert gate2_b["allowed_actions"] == [
            "submit_final_decision:reject",
            "submit_final_decision:needs_review",
        ]

        # Req 17: Ordinary approval MUST be blocked (409 Conflict)
        blocked_b = client.post(
            f"/api/runs/{run_b_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "marsadh",
                "comments": "Attempting override on confirmed regression",
            },
        )
        assert blocked_b.status_code == 409
        assert blocked_b.json()["details"]["has_confirmed_regression"] is True

        # Reject succeeds cleanly
        rej_b = client.post(
            f"/api/runs/{run_b_id}/decision",
            json={
                "decision": "reject",
                "reviewer": "marsadh",
                "comments": "Rejected due to confirmed pass->fail regressions on regular customer checkout and downstream modules.",
            },
        )
        assert rej_b.status_code == 200
        assert rej_b.json()["state"] == WorkflowState.REJECTED.value

    # Req 20: Original repositories untouched & temporary baseline workspace folders cleaned up
    assert _snapshot_tree_bytes(repo_a) == orig_a_tree
    assert _snapshot_tree_bytes(repo_b) == orig_b_tree
    assert not (repo_a / ".changeguard_generated_tests").exists()
    assert not (repo_b / ".changeguard_generated_tests").exists()
    remaining_baseline_dirs = [
        p for p in (tmp_path / "workspaces").iterdir() if "_baseline_" in p.name
    ]
    assert remaining_baseline_dirs == []


# ---------------------------------------------------------------------------
# 3: Generated-Change Workflow Compatibility (generate_and_verify_change)
# ---------------------------------------------------------------------------


def test_generate_and_verify_change_baseline_comparison_end_to_end(
    tmp_path: Path,
) -> None:
    """Req 3: Clean repo + applied offline patch fixture runs baseline (pre-patch) vs modified (post-patch)."""
    repos_root = tmp_path / "repos"
    clean_repo = repos_root / "clean_mini_pos"
    base_commit, _ = _create_mini_pos_repo(clean_repo, mode="clean")
    orig_tree = _snapshot_tree_bytes(clean_repo)

    cfg = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"clean-mini-pos": clean_repo},
    )
    app = create_app(
        cfg,
        patch_offline_fixture_provider=DeterministicPremiumDiscountPatchFixture(),
        offline_model_callable=_common_premium_discount_test_suite(include_criterion_ids=True),
    )

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "clean-mini-pos",
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "workflow_mode": WorkflowMode.GENERATE_AND_VERIFY_CHANGE.value,
                "execution_mode": ExecutionMode.OFFLINE_TEST.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        gate1 = client.get(f"/api/runs/{run_id}").json()
        assert gate1["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert gate1["patch_application"]["applied"] is True

        client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003", "SCN-004"],
                "reviewer": "marsadh",
            },
        )
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        gate2 = client.get(f"/api/runs/{run_id}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        # Baseline source snapshot MUST match original pre-patch snapshot!
        assert gate2["baseline_source_snapshot_sha256"] == gate2["original_source_snapshot_sha256"]
        assert gate2["baseline_source_snapshot_sha256"] != gate2["source_snapshot_sha256"]
        assert gate2["baseline_verification"]["baseline_commit"] == base_commit
        assert gate2["baseline_comparison"]["status"] == "completed"
        assert gate2["baseline_comparison"]["summary"]["newly_satisfied_acceptance_count"] == 2
        assert gate2["baseline_comparison"]["summary"]["preserved_behavior_count"] == 2
        assert gate2["baseline_comparison"]["summary"]["has_confirmed_regression"] is False

    assert _snapshot_tree_bytes(clean_repo) == orig_tree


# ---------------------------------------------------------------------------
# 8 & 17: Existing Repository Regression Suite Separation & Regression Approval Block
# ---------------------------------------------------------------------------


def test_existing_repo_regression_suite_blocks_approval_even_when_generated_tests_pass(
    tmp_path: Path,
) -> None:
    """Req 8 & 17: Pre-existing repo regression test suite runs separately and blocks approval on pass->fail."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_with_existing_tests"
    existing_test = (
        "import sys\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
        "from billing import apply_discount\n\n"
        "def test_existing_regular_no_discount():\n"
        "    assert apply_discount(100.0, 'regular') == pytest.approx(100.0)\n"
    )
    _create_mini_pos_repo(repo, mode="regressive", existing_test_code=existing_test)

    # Generated test ONLY checks premium (passes on regressive implementation!)
    def _premium_only_stub(_prompt: str) -> str:
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Premium checkout applies 10% discount",
                        "rationale": "Checks premium only.",
                        "assumptions": "Offline stub.",
                        "criterion_ids": ["AC-001"],
                        "test_code": (
                            "import sys\n"
                            "from pathlib import Path\n"
                            "import pytest\n"
                            "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
                            "from billing import apply_discount\n\n"
                            "def test_premium_only():\n"
                            "    assert apply_discount(100.0, 'premium') == pytest.approx(90.0)\n"
                        ),
                    }
                ]
            }
        )

    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos-reg-suite": repo})
    app = create_app(cfg, offline_model_callable=_premium_only_stub)

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-reg-suite",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        client.post(
            f"/api/runs/{run_id}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001"], "reviewer": "marsadh"},
        )
        app.state.executor.wait_for_run(run_id, timeout=60.0)

        gate2 = client.get(f"/api/runs/{run_id}").json()
        # Generated scenario passed on modified, BUT existing repo regression test went PASS -> FAIL!
        assert gate2["verification"]["verification_passed"] is True
        assert gate2["baseline_verification"]["existing_regression_suite"]["passed"] == 1
        assert gate2["modified_verification"]["existing_regression_suite"]["failed"] == 1

        cmp_data = gate2["baseline_comparison"]
        assert cmp_data["status"] == "completed"
        assert cmp_data["summary"]["has_confirmed_regression"] is True
        assert cmp_data["summary"]["blocks_ordinary_approval"] is True
        assert cmp_data["summary"]["newly_introduced_failures"] == [
            "existing::tests/test_existing_regression.py"
        ]
        # Clearly separated suites
        suite_kinds = {tc["suite_kind"] for tc in cmp_data["test_comparisons"]}
        assert suite_kinds == {
            TestSuiteKind.GENERATED_ACCEPTANCE.value,
            TestSuiteKind.EXISTING_REGRESSION.value,
        }

        # Gate 2 ordinary approval MUST be blocked with REGRESSION_APPROVAL_BLOCKED (409)!
        assert "submit_final_decision:approve" not in gate2["allowed_actions"]
        blocked = client.post(
            f"/api/runs/{run_id}/decision",
            json={"decision": "approve", "reviewer": "marsadh", "comments": "try approve"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "REGRESSION_APPROVAL_BLOCKED"


# ---------------------------------------------------------------------------
# 10 & 13: Real Collection Error, Baseline Timeout & Unavailable Baseline Preservation
# ---------------------------------------------------------------------------


def test_real_collection_error_and_baseline_timeout_and_unavailable_baseline_preserve_modified(
    tmp_path: Path,
) -> None:
    """Reqs 10 & 13: Collection error is NOT a regression; timeout/unavailable baseline still preserves modified results."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_edge"
    _create_mini_pos_repo(repo, mode="correct")

    # 10. Real collection error on baseline/modified -> NOT_COMPARABLE (never regression)
    cfg_col = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="col.sqlite3")
    col_stub = lambda _p: json.dumps(  # noqa: E731
        {
            "scenarios": [
                {
                    "title": "Import error scenario",
                    "rationale": "Triggers real pytest collection error.",
                    "assumptions": "Offline stub.",
                    "test_code": "import nonexistent_p07_module_xyz\ndef test_never():\n    assert True\n",
                }
            ]
        }
    )
    svc_col = OrchestratorService(
        config=cfg_col,
        store=RunStore(cfg_col.database_path),
        workspace_manager=WorkspaceManager(cfg_col),
        codebase_adapter=CodebaseAdapter(cfg_col),
        testing_adapter=TestingAdapter(cfg_col),
        verification_adapter=VerificationAdapter(cfg_col),
        offline_model_callable=col_stub,
    )
    r_col = svc_col.start_run(
        CreateRunRequest(repository_id="pos", source_path="mini_pos", requirements=PREMIUM_DISCOUNT_REQUIREMENT)
    )
    done_col = svc_col.submit_test_review(
        r_col.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
    )
    assert done_col.baseline_comparison is not None
    assert done_col.baseline_comparison.status == VerificationComparisonStatus.INCOMPLETE
    assert done_col.baseline_comparison.summary.has_confirmed_regression is False
    assert (
        done_col.baseline_comparison.test_comparisons[0].classification
        == TestComparisonClassification.NOT_COMPARABLE
    )

    # 13a. Baseline workspace unavailable -> modified verification STILL executes & comparison marked INCOMPLETE!
    cfg_unavail = _make_config(
        tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="unavail.sqlite3"
    )

    class FailingBaselineWorkspaceManager(WorkspaceManager):
        def prepare_baseline_workspace(self, *args, **kwargs):
            raise RuntimeError("Simulated unavailable baseline Git object")

    svc_unavail = OrchestratorService(
        config=cfg_unavail,
        store=RunStore(cfg_unavail.database_path),
        workspace_manager=FailingBaselineWorkspaceManager(cfg_unavail),
        codebase_adapter=CodebaseAdapter(cfg_unavail),
        testing_adapter=TestingAdapter(cfg_unavail),
        verification_adapter=VerificationAdapter(cfg_unavail),
        offline_model_callable=_common_premium_discount_test_suite(include_criterion_ids=True),
    )
    r_unavail = svc_unavail.start_run(
        CreateRunRequest(repository_id="pos", source_path="mini_pos", requirements=PREMIUM_DISCOUNT_REQUIREMENT)
    )
    done_unavail = svc_unavail.submit_test_review(
        r_unavail.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001", "SCN-002"], reviewer="qa"),
    )
    assert done_unavail.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert done_unavail.verification is not None
    assert done_unavail.verification.verification_passed is True
    assert done_unavail.modified_verification is not None
    assert done_unavail.modified_verification.passed == 2
    assert done_unavail.baseline_verification is not None
    assert done_unavail.baseline_verification.execution_status == "unavailable"
    assert done_unavail.baseline_comparison is not None
    assert done_unavail.baseline_comparison.status == VerificationComparisonStatus.INCOMPLETE
    assert done_unavail.baseline_comparison.summary.regression_safety_established is False
    assert done_unavail.report is not None
    assert done_unavail.report.complete_requirement_assurance is False

    # 13b. Baseline timeout while modified passes -> preserves modified results & marks INCOMPLETE
    cfg_to = _make_config(
        tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="to.sqlite3", timeout=1
    )

    class BaselineTimeoutOnlyAdapter(VerificationAdapter):
        def verify(self, workspace_path, *args, **kwargs):
            if "_baseline_" in str(workspace_path):
                return VerificationResult(
                    execution_status="timeout",
                    verification_outcome=VerificationOutcome.TIMEOUT,
                    verification_passed=False,
                    executed_test_ids=list(args[0]),
                    error=None,
                )
            return super().verify(workspace_path, *args, **kwargs)

    svc_to = OrchestratorService(
        config=cfg_to,
        store=RunStore(cfg_to.database_path),
        workspace_manager=WorkspaceManager(cfg_to),
        codebase_adapter=CodebaseAdapter(cfg_to),
        testing_adapter=TestingAdapter(cfg_to),
        verification_adapter=BaselineTimeoutOnlyAdapter(cfg_to),
        offline_model_callable=_common_premium_discount_test_suite(include_criterion_ids=True),
    )
    r_to = svc_to.start_run(
        CreateRunRequest(repository_id="pos", source_path="mini_pos", requirements=PREMIUM_DISCOUNT_REQUIREMENT)
    )
    done_to = svc_to.submit_test_review(
        r_to.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
    )
    assert done_to.state == WorkflowState.AWAITING_FINAL_APPROVAL
    assert done_to.baseline_verification is not None
    assert done_to.baseline_verification.verification_outcome == VerificationOutcome.TIMEOUT
    assert done_to.modified_verification is not None
    assert done_to.modified_verification.verification_passed is True
    assert done_to.baseline_comparison is not None
    assert done_to.baseline_comparison.status == VerificationComparisonStatus.INCOMPLETE
    assert done_to.baseline_comparison.summary.regression_safety_established is False


# ---------------------------------------------------------------------------
# 14 & 15: Modified Workspace Tampering & Baseline Workspace Tampering
# ---------------------------------------------------------------------------


def test_modified_and_baseline_workspace_tampering_caught_before_execution(
    tmp_path: Path,
) -> None:
    """Reqs 14 & 15: Tampering with either modified or baseline workspace fails closed."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_tamper"
    _create_mini_pos_repo(repo, mode="correct")

    # 14. Modified workspace tampering between accept_test_review and verification
    cfg1 = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="t1.sqlite3")
    svc1 = OrchestratorService(
        config=cfg1,
        store=RunStore(cfg1.database_path),
        workspace_manager=WorkspaceManager(cfg1),
        codebase_adapter=CodebaseAdapter(cfg1),
        testing_adapter=TestingAdapter(cfg1),
        verification_adapter=VerificationAdapter(cfg1),
        offline_model_callable=_common_premium_discount_test_suite(),
    )
    run1 = svc1.start_run(
        CreateRunRequest(repository_id="pos", source_path="mini_pos", requirements=PREMIUM_DISCOUNT_REQUIREMENT)
    )
    claimed1, specs1 = svc1.accept_test_review(
        run1.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
    )
    assert claimed1.workspace_path is not None and specs1 is not None
    (Path(claimed1.workspace_path) / "mini_pos" / "billing.py").write_text(
        "# TAMPERED MODIFIED WORKSPACE\n", encoding="utf-8"
    )
    failed1 = svc1.execute_verification_and_report(run1.run_id, specs1)
    assert failed1.state == WorkflowState.FAILED
    assert failed1.error is not None
    assert failed1.error.code == "SOURCE_SNAPSHOT_MODIFIED"

    # 15. Baseline workspace tampering right before baseline test execution
    cfg2 = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo}, db_name="t2.sqlite3")

    def _tamper_baseline(_run: WorkflowRunRecord, baseline_repo: Path) -> None:
        (baseline_repo / "mini_pos" / "billing.py").write_text(
            "# TAMPERED BASELINE WORKSPACE\n", encoding="utf-8"
        )

    svc2 = OrchestratorService(
        config=cfg2,
        store=RunStore(cfg2.database_path),
        workspace_manager=WorkspaceManager(cfg2),
        codebase_adapter=CodebaseAdapter(cfg2),
        testing_adapter=TestingAdapter(cfg2),
        verification_adapter=VerificationAdapter(cfg2),
        offline_model_callable=_common_premium_discount_test_suite(),
        before_baseline_execution_hook=_tamper_baseline,
    )
    run2 = svc2.start_run(
        CreateRunRequest(repository_id="pos", source_path="mini_pos", requirements=PREMIUM_DISCOUNT_REQUIREMENT)
    )
    failed2 = svc2.submit_test_review(
        run2.run_id,
        TestReviewRequest(approved=True, selected_test_ids=["SCN-001"], reviewer="qa"),
    )
    assert failed2.state == WorkflowState.FAILED
    assert failed2.error is not None
    assert failed2.error.code == "BASELINE_WORKSPACE_TAMPERED"
    assert failed2.baseline_verification is None
    assert failed2.modified_verification is None


# ---------------------------------------------------------------------------
# 19, 21, 22: Persistence, Backward Compatibility, Restart Recovery, OpenAPI, Secrets
# ---------------------------------------------------------------------------


def test_persistence_restart_recovery_backward_compat_openapi_and_secret_redaction(
    tmp_path: Path,
) -> None:
    """Reqs 19, 21, 22: Persistence, restart recovery across all new states, OpenAPI parity, secret redaction."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_persist"
    _create_mini_pos_repo(repo, mode="correct")
    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos": repo})
    store = RunStore(cfg.database_path)

    # 19a. Restart recovery marks interrupted runs in EVERY new P0-7 active state as FAILED without replaying
    interrupted_p07_states = [
        WorkflowState.PREPARING_BASELINE,
        WorkflowState.RUNNING_BASELINE_VERIFICATION,
        WorkflowState.RUNNING_MODIFIED_VERIFICATION,
        WorkflowState.COMPARING_VERIFICATION_RESULTS,
    ]
    for idx, st in enumerate(interrupted_p07_states, start=1):
        store.save_run(
            WorkflowRunRecord(
                run_id=f"run_p07_interrupted_{idx}",
                state=st,
                requirements="Interrupted mid-baseline stage",
                repository=RepositoryInfo(
                    repository_id="pos",
                    repository_path=str(repo),
                    source_path="mini_pos",
                    base_ref="HEAD",
                ),
            )
        )

    verify_replayed = 0

    class NeverReplayVerificationAdapter(VerificationAdapter):
        def verify(self, *args, **kwargs):
            nonlocal verify_replayed
            verify_replayed += 1
            raise AssertionError("Restart recovery must never replay verification")

    svc = OrchestratorService(
        config=cfg,
        store=store,
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=NeverReplayVerificationAdapter(cfg),
    )
    recovered = svc.recover_interrupted_runs()
    assert verify_replayed == 0
    assert len(recovered) == len(interrupted_p07_states)
    assert all(r.state == WorkflowState.FAILED for r in recovered)
    assert all(r.error is not None and r.error.code == "PROCESS_INTERRUPTED" for r in recovered)

    # 19b. Pre-P0-7 legacy persisted JSON (without baseline_* fields) loads cleanly
    legacy_p06_json = json.dumps(
        {
            "run_id": "run_legacy_p06_001",
            "state": "APPROVED",
            "requirements": "Legacy P0-6 run.",
            "workflow_mode": "verify_existing_change",
            "execution_mode": "offline_test",
            "repository": {
                "repository_id": "pos",
                "repository_path": str(repo),
                "source_path": "mini_pos",
                "base_ref": "HEAD",
                "change_mode": "working_tree",
            },
            "verification": {
                "execution_status": "completed",
                "verification_outcome": "passed",
                "verification_passed": True,
                "exit_code": 0,
                "total_tests": 1,
                "passed": 1,
                "failed": 0,
                "baseline_comparison": {
                    "status": "unavailable",
                    "baseline_ref": "HEAD",
                    "modified_passed": 1,
                    "modified_failed": 0,
                },
            },
        }
    )
    legacy = WorkflowRunRecord.model_validate_json(legacy_p06_json)
    assert legacy.baseline_verification is None
    assert legacy.modified_verification is None
    assert legacy.baseline_comparison is None
    assert legacy.verification is not None
    assert legacy.verification.baseline_comparison.status == VerificationComparisonStatus.UNAVAILABLE

    # 22. Secret redaction across baseline_verification, modified_verification, baseline_comparison, API & SQLite
    synthetic_secret = "gsk_" + "BaselineSecretLeakCheck1234567890"
    secret_run = WorkflowRunRecord(
        run_id="run_p07_secret_redact",
        state=WorkflowState.AWAITING_FINAL_APPROVAL,
        requirements="Verify secret redaction in baseline comparison.",
        repository=RepositoryInfo(
            repository_id="pos",
            repository_path=str(repo),
            source_path="mini_pos",
            base_ref="HEAD",
        ),
        baseline_verification=BaselineVerificationResult(
            execution_status="error",
            verification_outcome=VerificationOutcome.EXECUTION_ERROR,
            collection_errors=[f"Auth failed with key {synthetic_secret}"],
            evidence_limitations=[f"Token leaked: Bearer eyJhbGciOiJIUzI1NiJ9.{synthetic_secret}"],
            error=None,
        ),
        baseline_comparison=BaselineComparisonEvidence(
            status=VerificationComparisonStatus.INCOMPLETE,
            notes=f"Baseline error contained {synthetic_secret}",
            evidence_limitations=[f"api_key={synthetic_secret}"],
        ),
    )
    store.save_run(secret_run)

    # Confirm SQLite file contains zero unredacted secret bytes
    assert synthetic_secret.encode("utf-8") not in cfg.database_path.read_bytes()

    # Confirm API response contains zero unredacted secrets & matches committed OpenAPI spec
    app = create_app(cfg)
    live_openapi = app.openapi()
    committed_openapi = json.loads(
        (REPO_ROOT / "docs" / "m02" / "openapi.json").read_text(encoding="utf-8")
    )
    assert live_openapi == committed_openapi

    with TestClient(app) as client:
        res = client.get("/api/runs/run_p07_secret_redact")
        assert res.status_code == 200
        assert synthetic_secret not in res.text
        assert "[REDACTED_GROQ_KEY]" in res.text


# ---------------------------------------------------------------------------
# 23: Phase 1 P0-6 Rollback Defect Regression Test (Git Index Restoration)
# ---------------------------------------------------------------------------


def test_p06_patch_rollback_restores_git_index_and_prunes_empty_dirs(
    tmp_path: Path,
) -> None:
    """Phase 1 defect fix: Rollback removes intent-to-add index entries & verifies both snapshot and diff."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_rb"
    _create_mini_pos_repo(repo, mode="clean")
    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos-rb": repo})

    wm = WorkspaceManager(cfg)
    ws_ctx = wm.prepare_workspace("run_rb_001", repo, source_path="mini_pos", base_ref="HEAD")
    ws_repo = Path(ws_ctx.workspace_repo_path)
    orig_snap = compute_source_snapshot_sha256(ws_repo, "mini_pos")
    orig_diff = compute_git_diff_sha256(ws_repo, ws_ctx.effective_base_ref, "mini_pos")

    # Patch adding a brand-new file inside a new subdirectory
    add_diff = (
        "diff --git a/mini_pos/new_pkg/helper.py b/mini_pos/new_pkg/helper.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/mini_pos/new_pkg/helper.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def added_helper():\n"
        "+    return 42\n"
    )
    proposal = PatchProposal(
        proposal_id="PATCH-ADD-001",
        unified_diff=add_diff,
        patch_sha256=compute_patch_sha256(add_diff),
        affected_files=[
            PatchFileChange(path="mini_pos/new_pkg/helper.py", change_type="add")
        ],
        change_rationale="Test rollback of added file + intent-to-add index entry.",
    )
    val_res = PatchValidator(cfg).validate(
        workspace_repo_path=ws_repo,
        proposal=proposal,
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        allowed_modification_paths=["mini_pos"],
        expected_source_snapshot_sha256=orig_snap,
        expected_git_diff_sha256=orig_diff,
    )
    assert val_res.valid is True

    # Simulate partial write + intent-to-add entry in index before failure
    added_file = ws_repo / "mini_pos" / "new_pkg" / "helper.py"
    added_file.parent.mkdir(parents=True, exist_ok=True)
    added_file.write_text("def added_helper():\n    return 42\n", encoding="utf-8")
    _git(ws_repo, "add", "-N", "--", "mini_pos/new_pkg/helper.py")
    assert _git(ws_repo, "ls-files", "--", "mini_pos/new_pkg/helper.py") == "mini_pos/new_pkg/helper.py"

    applicator = PatchApplicator(cfg)
    clean_ok = applicator._rollback_and_verify_clean(
        ws_repo,
        {"mini_pos/new_pkg/helper.py": None},
        ["mini_pos/new_pkg/helper.py"],
        source_path="mini_pos",
        base_ref=ws_ctx.effective_base_ref,
        expected_source_snapshot_sha256=orig_snap,
        expected_git_diff_sha256=orig_diff,
    )
    assert clean_ok is True
    assert not added_file.exists()
    assert not (ws_repo / "mini_pos" / "new_pkg").exists()
    assert _git(ws_repo, "ls-files", "--", "mini_pos/new_pkg/helper.py") == ""
    assert _git(ws_repo, "status", "--porcelain") == ""
    assert compute_source_snapshot_sha256(ws_repo, "mini_pos") == orig_snap
    assert compute_git_diff_sha256(ws_repo, ws_ctx.effective_base_ref, "mini_pos") == orig_diff


def test_gate1_rejection_never_runs_tests_and_incomplete_coverage_distinguishes_assurance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reqs 16 & 18: Gate 1 rejection runs zero tests; partial coverage distinguishes ordinary approval from complete assurance."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_cov"
    _create_mini_pos_repo(repo, mode="correct")
    marker = tmp_path / "gate1_reject_marker.txt"
    monkeypatch.setenv("CHANGEGUARD_TEST_MARKER", str(marker))
    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos-cov": repo})
    app = create_app(
        cfg,
        offline_model_callable=_common_premium_discount_test_suite(
            include_criterion_ids=True,
            marker_path=marker,
        ),
    )

    with TestClient(app) as client:
        # 16. Reject at Gate 1 -> zero baseline preparation, zero test execution
        r1 = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-cov",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(r1, timeout=30.0)
        rej = client.post(
            f"/api/runs/{r1}/test-review",
            json={"approved": False, "reviewer": "qa", "notes": "Reject proposals"},
        )
        assert rej.status_code == 200
        assert rej.json()["state"] == WorkflowState.REJECTED.value
        assert rej.json()["baseline_verification"] is None
        assert rej.json()["modified_verification"] is None
        assert not marker.exists()

        # 18. Approve ONLY SCN-001 (AC-001) leaving mandatory AC-002 & AC-003 uncovered
        r2 = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-cov",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(r2, timeout=30.0)
        client.post(
            f"/api/runs/{r2}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001"], "reviewer": "qa"},
        )
        app.state.executor.wait_for_run(r2, timeout=60.0)

        gate2 = client.get(f"/api/runs/{r2}").json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2["verification"]["verification_passed"] is True
        assert gate2["baseline_comparison"]["status"] == "completed"
        rep = gate2["report"]
        assert rep["complete_requirement_assurance"] is False
        assert rep["traceability"]["requirement_verification_proven"] is False
        assert rep["traceability"]["mandatory_criteria_coverage_complete"] is False
        assert rep["traceability"]["incomplete_mandatory_criterion_ids"] == ["AC-002", "AC-003"]
        assert "does not constitute complete requirement assurance" in rep["recommendation_summary"].lower()
        # Ordinary human approval remains permitted with disclosed limitations (0 regressions)
        assert "submit_final_decision:approve" in gate2["allowed_actions"]


def test_real_existing_regression_suite_fail_to_pass_is_fixed_existing_failure(
    tmp_path: Path,
) -> None:
    """Req 8: Real existing repository regression test failing on baseline and passing on modified -> fixed_existing_failure."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_fix_existing"
    existing_test = (
        "import sys\n"
        "from pathlib import Path\n"
        "import pytest\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
        "from billing import apply_discount\n\n"
        "def test_existing_premium_bug_regression():\n"
        "    assert apply_discount(100.0, 'premium') == pytest.approx(90.0)\n"
    )
    _create_mini_pos_repo(repo, mode="correct", existing_test_code=existing_test)
    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos-fix": repo})
    app = create_app(
        cfg,
        offline_model_callable=_common_premium_discount_test_suite(include_criterion_ids=True),
    )

    with TestClient(app) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-fix",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(rid, timeout=30.0)
        client.post(
            f"/api/runs/{rid}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-002"], "reviewer": "qa"},
        )
        app.state.executor.wait_for_run(rid, timeout=60.0)

        gate2 = client.get(f"/api/runs/{rid}").json()
        cmp_data = gate2["baseline_comparison"]
        by_id = {tc["test_identity"]: tc for tc in cmp_data["test_comparisons"]}
        assert by_id["SCN-002"]["classification"] == "preserved_behavior"
        assert (
            by_id["existing::tests/test_existing_regression.py"]["classification"]
            == "fixed_existing_failure"
        )
        assert cmp_data["summary"]["fixed_existing_failure_count"] == 1
        assert cmp_data["summary"]["has_confirmed_regression"] is False
