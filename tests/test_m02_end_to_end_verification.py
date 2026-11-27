"""P0-3 End-to-End Integration Verification, Evidence Integrity, and Gate Security Tests.

Verifies the real ChangeGuard AI backend coordinating M01 (CodebaseAdapter),
M03 (TestingAdapter), and M04 (VerificationAdapter) over isolated trusted Git
fixtures and real FastAPI HTTP routes without modifying teammate business logic.

Covers:
- Phase 1: OpenAPI parity against docs/m02/openapi.json and CORS enforcement.
- Phase 2 & 3: Real multi-agent pipeline on Scenario A (correct change) and
  Scenario B (regressive change), including a live loopback HTTP server run.
- Phase 4: Distinction between default offline plumbing smoke tests and
  meaningful behavioral verification tests that fail against regressions.
- Phase 5: Evidence integrity, requirement traceability status, non-fabricated
  baseline regression status, and offline mock labeling.
- Phase 6: Human review & approval gates including concurrent HTTP approvals,
  selective scenario execution, tamper detection, and non-mutating Git approval.
- Phase 7: Failure handling (clean repo, invalid repo ID, invalid Git ref,
  disallowed path, invalid model JSON, missing Groq credentials, timeout,
  collection error, zero tests, interrupted active run, restart during paused review).
- Phase 8: Live Groq readiness wiring (mocked locally; zero external network calls)
  and nested secret redaction across persistence and API responses.
- Phase 9 & 10: Dashboard contract completeness and untrusted workspace rejection.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from pydantic import SecretStr

from project.main import create_app
from project.marsadh_orchestrator import (
    OrchestratorConfig,
    RunStore,
    deterministic_offline_requirement_analyzer,
)
from project.marsadh_orchestrator.orchestrator import default_offline_model_stub
from project.schemas import (
    ExecutionMode,
    RepositoryInfo,
    WorkflowRunRecord,
    WorkflowState,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MINI_POS_DIR = REPO_ROOT / "sample_projects" / "mini_pos"

REQUIREMENT_TEXT = (
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
    "    # Regression: erroneously discounts regular customers as well as premium\n"
    "    return total * 0.90\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for P0-3 integration tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _create_mini_pos_fixture(repo: Path, *, mode: str = "correct") -> str:
    """Create an isolated Git repository cloning the mini_pos module structure."""
    app_dir = repo / "mini_pos"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "billing.py").write_text(BASELINE_BILLING, encoding="utf-8")
    for fname in ("refunds.py", "customer_service.py", "dashboard.py", "reports.py"):
        shutil.copyfile(MINI_POS_DIR / fname, app_dir / fname)

    _git(repo, "init")
    _git(repo, "config", "user.name", "ChangeGuard P0-3")
    _git(repo, "config", "user.email", "p03@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline mini_pos without premium discount")
    baseline_commit = _git(repo, "rev-parse", "HEAD")

    if mode == "correct":
        (app_dir / "billing.py").write_text(CORRECT_BILLING, encoding="utf-8")
    elif mode == "regressive":
        (app_dir / "billing.py").write_text(REGRESSIVE_BILLING, encoding="utf-8")
    elif mode == "clean":
        pass
    else:
        raise ValueError(f"Unsupported fixture mode: {mode}")

    return baseline_commit


def _behavioral_pos_stub(prompt: str) -> str:
    """Deterministic fixture-specific behavioral test generator for mini_pos.

    Generates genuine behavioral assertions grounded in actual billing, refunds,
    customer_service, dashboard, and reports APIs so regressions fail under pytest.
    """
    assert "apply_discount" in prompt
    assert "mini_pos/billing.py" in prompt
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
                    "title": "Premium checkout applies exactly 10% discount",
                    "rationale": "billing.apply_discount changed to discount premium customers.",
                    "assumptions": "Deterministic offline behavioral fixture test; premium receives 10% off.",
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_premium_customer_gets_10_percent_discount():\n"
                        + "    assert apply_discount(200.0, 'premium') == pytest.approx(180.0)\n"
                        + "    assert checkout(50.0, 4, 'premium') == pytest.approx(180.0)\n"
                    ),
                },
                {
                    "title": "Regular customer checkout remains undiscounted",
                    "rationale": "Regular customers must not be affected by the premium discount change.",
                    "assumptions": "Deterministic offline behavioral fixture test; regular pays full price.",
                    "test_code": (
                        header
                        + "from billing import apply_discount, checkout\n\n"
                        + "def test_regular_customer_checkout_unchanged():\n"
                        + "    assert apply_discount(200.0, 'regular') == pytest.approx(200.0)\n"
                        + "    assert checkout(50.0, 4, 'regular') == pytest.approx(200.0)\n"
                    ),
                },
                {
                    "title": "Dependent refunds, dashboard, and sales reports preserve regular vs premium behavior",
                    "rationale": "M01 detected dependent modules refunds.py, customer_service.py, dashboard.py, and reports.py.",
                    "assumptions": "Deterministic offline behavioral fixture test covering downstream impact chain.",
                    "test_code": (
                        header
                        + "from dashboard import display_refund\n"
                        + "from reports import generate_sales_report\n\n"
                        + "def test_downstream_refund_dashboard_and_sales_report():\n"
                        + "    assert display_refund(100.0, 'premium') == {'refund_amount': pytest.approx(90.0)}\n"
                        + "    assert display_refund(100.0, 'regular') == {'refund_amount': pytest.approx(100.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'premium') == {'total_sales': pytest.approx(180.0)}\n"
                        + "    assert generate_sales_report(100.0, 2, 'regular') == {'total_sales': pytest.approx(200.0)}\n"
                    ),
                },
            ]
        }
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _poll_run_until(
    client: httpx.Client,
    base_url: str,
    run_id: str,
    *,
    stop_states: set[str],
    timeout_s: float = 30.0,
) -> dict:
    deadline = time.monotonic() + timeout_s
    last_payload: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"{base_url}/api/runs/{run_id}")
        assert resp.status_code == 200
        last_payload = resp.json()
        if last_payload["state"] in stop_states:
            return last_payload
        time.sleep(0.1)
    raise AssertionError(
        f"Timed out waiting for run {run_id} to reach {stop_states}; last state={last_payload.get('state')}"
    )


# ---------------------------------------------------------------------------
# Phase 1 & 9: OpenAPI Contract Parity & Dashboard Field Completeness
# ---------------------------------------------------------------------------


def test_live_openapi_matches_committed_contract_without_drift(tmp_path) -> None:
    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / "openapi.sqlite3",
        workspace_root=tmp_path / "workspaces",
        environment="test",
    )
    app = create_app(cfg)
    live_spec = app.openapi()
    committed_spec = json.loads(
        (REPO_ROOT / "docs" / "m02" / "openapi.json").read_text(encoding="utf-8")
    )

    assert live_spec == committed_spec
    assert set(live_spec["paths"].keys()) == {
        "/health",
        "/api/runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/logs",
        "/api/runs/{run_id}/test-review",
        "/api/runs/{run_id}/decision",
    }


# ---------------------------------------------------------------------------
# Phase 2, 3, 5, 9: Real Live HTTP Server End-to-End (Scenario A & Scenario B)
# ---------------------------------------------------------------------------


def test_real_http_end_to_end_scenario_a_correct_and_scenario_b_regressive(tmp_path) -> None:
    """Exercise all 7 canonical endpoints against a live loopback uvicorn HTTP server."""
    repos_root = tmp_path / "repos"
    repo_correct = repos_root / "pos_correct"
    repo_regressive = repos_root / "pos_regressive"

    base_a = _create_mini_pos_fixture(repo_correct, mode="correct")
    base_b = _create_mini_pos_fixture(repo_regressive, mode="regressive")

    orig_a_status = _git(repo_correct, "status", "--porcelain")
    orig_b_status = _git(repo_regressive, "status", "--porcelain")

    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / "live_http.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories={
            "pos-correct": repo_correct,
            "pos-regressive": repo_regressive,
        },
        trusted_execution_roots=[repos_root],
        verification_timeout_seconds=30,
        environment="test",
    )
    app = create_app(
        cfg,
        offline_model_callable=_behavioral_pos_stub,
        offline_model_label="deterministic_behavioral_fixture_stub",
    )

    port = _find_free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(timeout=20.0) as client:
            for _ in range(50):
                try:
                    health_res = client.get(f"{base_url}/health")
                    if health_res.status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                pytest.fail("Live uvicorn server failed to start")

            health = health_res.json()
            assert health["status"] == "ok"
            assert health["database_available"] is True
            assert health["git_available"] is True
            assert health["verification_dependencies_available"] is True
            assert health["configured_repository_ids"] == ["pos-correct", "pos-regressive"]

            # ---- Scenario A: Correct Software Change ----
            create_a = client.post(
                f"{base_url}/api/runs",
                json={
                    "repository_id": "pos-correct",
                    "source_path": "mini_pos",
                    "base_ref": "HEAD",
                    "requirements": REQUIREMENT_TEXT,
                    "execution_mode": "offline_test",
                },
            )
            assert create_a.status_code == 202
            run_a_id = create_a.json()["run_id"]

            gate1_a = _poll_run_until(
                client,
                base_url,
                run_a_id,
                stop_states={"AWAITING_TEST_REVIEW", "FAILED"},
            )
            assert gate1_a["state"] == "AWAITING_TEST_REVIEW"
            assert gate1_a["repository"]["resolved_base_commit"] == base_a
            assert gate1_a["codebase_analysis"]["changed_files_repo_relative"] == [
                "mini_pos/billing.py"
            ]
            assert gate1_a["codebase_analysis"]["impacted_modules_repo_relative"] == [
                "mini_pos/customer_service.py",
                "mini_pos/dashboard.py",
                "mini_pos/refunds.py",
                "mini_pos/reports.py",
            ]
            assert gate1_a["test_generation"]["executed"] is False
            assert gate1_a["test_generation"]["is_mock_output"] is True
            assert gate1_a["test_generation"]["model_label"] == "deterministic_behavioral_fixture_stub"
            assert [s["scenario_id"] for s in gate1_a["test_generation"]["scenarios"]] == [
                "SCN-001",
                "SCN-002",
                "SCN-003",
            ]
            assert gate1_a["verification"] is None
            assert gate1_a["allowed_actions"] == ["submit_test_review"]

            # Approve all 3 behavioral scenarios at Gate 1
            rev_a = client.post(
                f"{base_url}/api/runs/{run_a_id}/test-review",
                json={
                    "approved": True,
                    "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                    "reviewer": "marsadh",
                    "notes": "Verified behavioral assertions for premium, regular, and downstream modules.",
                },
            )
            assert rev_a.status_code == 202

            gate2_a = _poll_run_until(
                client,
                base_url,
                run_a_id,
                stop_states={"AWAITING_FINAL_APPROVAL", "FAILED"},
            )
            assert gate2_a["state"] == "AWAITING_FINAL_APPROVAL"
            ver_a = gate2_a["verification"]
            assert ver_a["execution_status"] == "completed"
            assert ver_a["verification_outcome"] == "passed"
            assert ver_a["verification_passed"] is True
            assert (ver_a["exit_code"], ver_a["total_tests"], ver_a["passed"], ver_a["failed"]) == (
                0,
                3,
                3,
                0,
            )
            assert ver_a["baseline_comparison"]["status"] == "completed"
            assert ver_a["baseline_comparison"]["newly_failing_tests"] == []
            assert ver_a["coverage_status"] == "unavailable"

            # Phase 5: Verify evidence integrity fields on Scenario A
            rep_a = gate2_a["report"]
            assert rep_a["is_mock_output"] is True
            assert rep_a["model_label"] == "deterministic_behavioral_fixture_stub"
            assert rep_a["recommendation"] == "needs_review"
            assert rep_a["recommendation_label"] == "ready_for_human_review"
            assert "is_mock_output=True" in rep_a["recommendation_summary"]
            assert rep_a["traceability"]["requirement_traceability_status"] == "incomplete"
            assert rep_a["traceability"]["structured_acceptance_criteria_status"] == "available"
            assert rep_a["traceability"]["criterion_mapping_status"] == "unavailable"
            assert rep_a["traceability"]["requirement_verification_proven"] is False
            assert rep_a["traceability"]["baseline_regression_comparison_status"] == "completed"
            assert rep_a["traceability"]["user_supplied_requirements"] == REQUIREMENT_TEXT
            assert len(rep_a["unverified_requirements"]) >= 1
            assert len(rep_a["evidence_limitations"]) >= 5
            assert gate2_a["allowed_actions"] == [
                "submit_final_decision:approve",
                "submit_final_decision:reject",
                "submit_final_decision:needs_review",
            ]

            # Gate 2: Human approval of limited local evidence package
            dec_a = client.post(
                f"{base_url}/api/runs/{run_a_id}/decision",
                json={
                    "decision": "approve",
                    "reviewer": "marsadh",
                    "comments": "All 3 behavioral scenarios passed; limitations acknowledged.",
                },
            )
            assert dec_a.status_code == 200
            assert dec_a.json()["state"] == "APPROVED"

            logs_a = client.get(f"{base_url}/api/runs/{run_a_id}/logs").json()
            assert logs_a["count"] >= 10
            assert {
                "OrchestratorService",
                "WorkspaceManager",
                "CodebaseAdapter",
                "TestingAdapter",
                "HumanTestReviewGate",
                "VerificationAdapter",
                "ChangePackageCompiler",
                "HumanFinalDecisionGate",
            }.issubset({entry["agent_name"] for entry in logs_a["logs"]})

            # ---- Scenario B: Regressive Software Change ----
            create_b = client.post(
                f"{base_url}/api/runs",
                json={
                    "repository_id": "pos-regressive",
                    "source_path": "mini_pos",
                    "base_ref": "HEAD",
                    "requirements": REQUIREMENT_TEXT,
                    "execution_mode": "offline_test",
                },
            )
            assert create_b.status_code == 202
            run_b_id = create_b.json()["run_id"]

            gate1_b = _poll_run_until(
                client,
                base_url,
                run_b_id,
                stop_states={"AWAITING_TEST_REVIEW", "FAILED"},
            )
            assert gate1_b["state"] == "AWAITING_TEST_REVIEW"
            assert gate1_b["repository"]["resolved_base_commit"] == base_b

            rev_b = client.post(
                f"{base_url}/api/runs/{run_b_id}/test-review",
                json={
                    "approved": True,
                    "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                    "reviewer": "marsadh",
                    "notes": "Execute behavioral suite against regressive candidate.",
                },
            )
            assert rev_b.status_code == 202

            gate2_b = _poll_run_until(
                client,
                base_url,
                run_b_id,
                stop_states={"AWAITING_FINAL_APPROVAL", "FAILED"},
            )
            assert gate2_b["state"] == "AWAITING_FINAL_APPROVAL"
            ver_b = gate2_b["verification"]
            assert ver_b["execution_status"] == "completed"
            assert ver_b["verification_outcome"] == "failed_tests"
            assert ver_b["verification_passed"] is False
            assert ver_b["exit_code"] == 1
            assert ver_b["total_tests"] == 3
            assert ver_b["passed"] == 1  # Premium discount still passes
            assert ver_b["failed"] == 2  # Regular checkout & downstream refund/report FAIL!
            assert len(ver_b["failures"]) == 2
            assert all(f["error_type"] == "AssertionError" for f in ver_b["failures"])

            rep_b = gate2_b["report"]
            assert rep_b["recommendation"] == "reject"
            assert rep_b["recommendation_label"] == "verification_failed"
            assert gate2_b["allowed_actions"] == [
                "submit_final_decision:reject",
                "submit_final_decision:needs_review",
            ]

            # Ordinary approval MUST be blocked with HTTP 409
            blocked_b = client.post(
                f"{base_url}/api/runs/{run_b_id}/decision",
                json={
                    "decision": "approve",
                    "reviewer": "marsadh",
                    "comments": "Should be rejected by backend gate",
                },
            )
            assert blocked_b.status_code == 409
            assert blocked_b.json()["code"] == "VERIFICATION_APPROVAL_BLOCKED"

            # Reject regressive change cleanly
            rej_b = client.post(
                f"{base_url}/api/runs/{run_b_id}/decision",
                json={
                    "decision": "reject",
                    "reviewer": "marsadh",
                    "comments": "Regression detected in regular checkout and downstream refund/report calculations.",
                },
            )
            assert rej_b.status_code == 200
            assert rej_b.json()["state"] == "REJECTED"

            # Verify GET /api/runs lists both runs cleanly
            listed = client.get(f"{base_url}/api/runs?limit=10&offset=0").json()
            assert listed["total"] == 2
            assert {r["run_id"] for r in listed["runs"]} == {run_a_id, run_b_id}
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)

    # Confirm neither fixture repository was mutated, committed, or merged
    assert _git(repo_correct, "rev-parse", "HEAD") == base_a
    assert _git(repo_correct, "status", "--porcelain") == orig_a_status
    assert not (repo_correct / ".changeguard_generated_tests").exists()
    assert _git(repo_regressive, "rev-parse", "HEAD") == base_b
    assert _git(repo_regressive, "status", "--porcelain") == orig_b_status
    assert not (repo_regressive / ".changeguard_generated_tests").exists()


# ---------------------------------------------------------------------------
# Phase 4: Default Offline Stub Plumbing Smoke Test vs Behavioral Verification
# ---------------------------------------------------------------------------


def test_default_offline_stub_is_plumbing_smoke_test_and_misses_regression(tmp_path) -> None:
    """Prove empirically that default_offline_model_stub checks symbol callability only."""
    repos_root = tmp_path / "repos"
    repo_regressive = repos_root / "pos_regressive"
    _create_mini_pos_fixture(repo_regressive, mode="regressive")

    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / "stub_check.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories={"pos-regressive": repo_regressive},
        trusted_execution_roots=[repos_root],
        environment="test",
    )
    app = create_app(cfg, offline_model_callable=default_offline_model_stub)

    with TestClient(app) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-regressive",
                "source_path": "mini_pos",
                "requirements": REQUIREMENT_TEXT,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        gate1 = client.get(f"/api/runs/{run_id}").json()
        assert gate1["state"] == "AWAITING_TEST_REVIEW"
        scn = gate1["test_generation"]["scenarios"][0]
        # Default offline stub does NOT emit bare `assert True` when changed functions exist;
        # instead it emits an import/callable plumbing smoke test.
        assert "callable(getattr(mod, 'apply_discount'))" in scn["test_code"]
        assert "assert True" not in scn["test_code"]
        assert "does not prove full requirement compliance" in scn["assumptions"]

        # Because it only checks callability, it passes even on the regressive fixture!
        client.post(
            f"/api/runs/{run_id}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001"], "reviewer": "auditor"},
        )
        app.state.executor.wait_for_run(run_id, timeout=30.0)
        gate2 = client.get(f"/api/runs/{run_id}").json()
        assert gate2["verification"]["verification_passed"] is True
        # Evidence report MUST still mark requirement traceability incomplete & mock output!
        assert gate2["report"]["is_mock_output"] is True
        assert gate2["report"]["traceability"]["requirement_verification_proven"] is False
        assert gate2["report"]["unverified_requirements"]


# ---------------------------------------------------------------------------
# Phase 6 & 7: Concurrent HTTP Approvals, Invalid Ref, Restart & Untrusted Repo
# ---------------------------------------------------------------------------


def test_http_concurrency_invalid_ref_untrusted_repo_and_restart_recovery(tmp_path) -> None:
    repos_root = tmp_path / "repos"
    trusted_repo = repos_root / "trusted" / "pos_ok"
    untrusted_repo = repos_root / "untrusted" / "pos_untrusted"
    _create_mini_pos_fixture(trusted_repo, mode="correct")
    _create_mini_pos_fixture(untrusted_repo, mode="correct")

    db_path = tmp_path / "state" / "gates.sqlite3"
    ws_root = tmp_path / "workspaces"

    def _make_app():
        cfg = OrchestratorConfig(
            database_path=db_path,
            workspace_root=ws_root,
            allowed_repository_roots=[repos_root],
            configured_repositories={
                "trusted-pos": trusted_repo,
                "untrusted-pos": untrusted_repo,
            },
            trusted_execution_roots=[repos_root / "trusted"],
            environment="test",
        )
        return create_app(cfg, offline_model_callable=_behavioral_pos_stub)

    app1 = _make_app()
    with TestClient(app1) as client1:
        # 1. Invalid Git ref -> persisted FAILED with WORKSPACE_PREPARATION_FAILED
        bad_ref_res = client1.post(
            "/api/runs",
            json={
                "repository_id": "trusted-pos",
                "source_path": "mini_pos",
                "base_ref": "nonexistent_ref_p03",
                "requirements": "Test invalid ref handling",
            },
        )
        assert bad_ref_res.status_code == 202
        bad_ref_id = bad_ref_res.json()["run_id"]
        app1.state.executor.wait_for_run(bad_ref_id, timeout=30.0)
        bad_ref_detail = client1.get(f"/api/runs/{bad_ref_id}").json()
        assert bad_ref_detail["state"] == "FAILED"
        assert bad_ref_detail["error"]["code"] == "WORKSPACE_PREPARATION_FAILED"
        assert client1.get(f"/api/runs/{bad_ref_id}/logs").json()["count"] >= 3

        # 2. Allowlisted but UNTRUSTED repository -> pytest execution blocked, approval blocked
        untrusted_id = client1.post(
            "/api/runs",
            json={
                "repository_id": "untrusted-pos",
                "source_path": "mini_pos",
                "requirements": "Untrusted execution guard check",
            },
        ).json()["run_id"]
        app1.state.executor.wait_for_run(untrusted_id, timeout=30.0)
        assert client1.get(f"/api/runs/{untrusted_id}").json()["state"] == "AWAITING_TEST_REVIEW"

        # Client attempting to inject is_trusted_workspace is rejected with 422 (extra="forbid")
        inject = client1.post(
            f"/api/runs/{untrusted_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001"],
                "reviewer": "attacker",
                "is_trusted_workspace": True,
            },
        )
        assert inject.status_code == 422

        tr_untrusted = client1.post(
            f"/api/runs/{untrusted_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001"],
                "reviewer": "qa",
            },
        )
        assert tr_untrusted.status_code == 202
        app1.state.executor.wait_for_run(untrusted_id, timeout=30.0)
        u_detail = client1.get(f"/api/runs/{untrusted_id}").json()
        assert u_detail["state"] == "AWAITING_FINAL_APPROVAL"
        assert u_detail["verification"]["execution_status"] == "not_executed"
        assert u_detail["verification"]["verification_outcome"] == "unavailable"
        assert u_detail["verification"]["verification_passed"] is False
        assert u_detail["verification"]["error"]["code"] == "UNTRUSTED_WORKSPACE"
        assert u_detail["report"]["recommendation_label"] == "verification_incomplete"
        assert "submit_final_decision:approve" not in u_detail["allowed_actions"]
        assert (
            client1.post(
                f"/api/runs/{untrusted_id}/decision",
                json={"decision": "approve", "reviewer": "qa", "comments": "try"},
            ).status_code
            == 409
        )

        # 3. Concurrent HTTP POST /test-review approvals: exactly one 202, rest 409
        conc_id = client1.post(
            "/api/runs",
            json={
                "repository_id": "trusted-pos",
                "source_path": "mini_pos",
                "requirements": "Concurrent HTTP approval check",
            },
        ).json()["run_id"]
        app1.state.executor.wait_for_run(conc_id, timeout=30.0)

        barrier = threading.Barrier(4)
        codes: list[int] = []
        codes_lock = threading.Lock()

        def _hit_gate1(idx: int) -> None:
            barrier.wait()
            resp = client1.post(
                f"/api/runs/{conc_id}/test-review",
                json={
                    "approved": True,
                    "selected_test_ids": ["SCN-001"],
                    "reviewer": f"reviewer-{idx}",
                },
            )
            with codes_lock:
                codes.append(resp.status_code)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for fut in [pool.submit(_hit_gate1, i) for i in range(4)]:
                fut.result()

        assert codes.count(202) == 1
        assert codes.count(409) == 3
        app1.state.executor.wait_for_run(conc_id, timeout=30.0)
        assert client1.get(f"/api/runs/{conc_id}").json()["state"] == "AWAITING_FINAL_APPROVAL"

        # 4. Prepare a paused run at Gate 1 AND an interrupted active run, then restart app
        paused_id = client1.post(
            "/api/runs",
            json={
                "repository_id": "trusted-pos",
                "source_path": "mini_pos",
                "requirements": "Resume after backend restart",
            },
        ).json()["run_id"]
        app1.state.executor.wait_for_run(paused_id, timeout=30.0)
        assert client1.get(f"/api/runs/{paused_id}").json()["state"] == "AWAITING_TEST_REVIEW"

        store = RunStore(db_path)
        store.save_run(
            WorkflowRunRecord(
                run_id="run_interrupted_active_p03",
                state=WorkflowState.GENERATING_TESTS,
                requirements="Interrupted mid-generation",
                repository=RepositoryInfo(
                    repository_id="trusted-pos",
                    repository_path=str(trusted_repo),
                    source_path="mini_pos",
                    base_ref="HEAD",
                ),
            )
        )

    # Restart backend (brand-new FastAPI app pointing at same SQLite DB)
    app2 = _make_app()
    with TestClient(app2) as client2:
        # Interrupted active run recovered to FAILED without replay
        interrupted_detail = client2.get("/api/runs/run_interrupted_active_p03").json()
        assert interrupted_detail["state"] == "FAILED"
        assert interrupted_detail["error"]["code"] == "PROCESS_INTERRUPTED"

        # Paused Gate 1 run remains intact and completes cleanly after restart
        assert client2.get(f"/api/runs/{paused_id}").json()["state"] == "AWAITING_TEST_REVIEW"
        resume_res = client2.post(
            f"/api/runs/{paused_id}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001"], "reviewer": "post-restart"},
        )
        assert resume_res.status_code == 202
        app2.state.executor.wait_for_run(paused_id, timeout=30.0)
        assert client2.get(f"/api/runs/{paused_id}").json()["state"] == "AWAITING_FINAL_APPROVAL"


# ---------------------------------------------------------------------------
# Phase 8 & 10: Live Groq Wiring Readiness & Nested Secret Redaction
# ---------------------------------------------------------------------------


def test_live_groq_wiring_no_silent_fallback_and_nested_secret_redaction(
    tmp_path, monkeypatch
) -> None:
    """Verify Groq adapter wiring without external network calls and verify secret redaction."""
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_ok"
    _create_mini_pos_fixture(repo, mode="correct")

    synthetic_key = "gsk_" + "FakeLiveKeyForRedactionTest1234567890"
    model_name = "llama-3.3-70b-versatile"

    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / "groq_ready.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories={"pos-ok": repo},
        trusted_execution_roots=[repos_root],
        groq_api_key=SecretStr(synthetic_key),
        groq_model=model_name,
        environment="test",
    )

    # 1. Mock groq_model.generate to verify lazy wiring, model identity, and zero network calls
    observed_env: dict[str, str | None] = {}

    def _fake_groq_generate(prompt: str) -> str:
        import os

        observed_env["key"] = os.environ.get("GROQ_API_KEY")
        observed_env["model"] = os.environ.get("GROQ_MODEL")
        return _behavioral_pos_stub(prompt)

    monkeypatch.setattr(
        "project.agents.munshif_testing.groq_model.generate",
        _fake_groq_generate,
    )

    app = create_app(
        cfg,
        requirement_live_callable=deterministic_offline_requirement_analyzer,
        requirement_live_label=f"groq:{model_name}",
        offline_model_callable=default_offline_model_stub,
    )
    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["live_llm_configured"] is True
        assert health["groq_model"] == model_name
        assert synthetic_key not in json.dumps(health)

        live_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": REQUIREMENT_TEXT,
                "execution_mode": ExecutionMode.LIVE_LLM.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(live_id, timeout=30.0)

        live_detail = client.get(f"/api/runs/{live_id}").json()
        assert live_detail["state"] == "AWAITING_TEST_REVIEW"
        assert observed_env == {"key": synthetic_key, "model": model_name}
        assert live_detail["test_generation"]["generation_mode"] == "live_llm"
        assert live_detail["test_generation"]["is_mock_output"] is False
        assert live_detail["test_generation"]["model_label"] == f"groq:{model_name}"

        # 2. Provider exception containing a secret key must NOT retry, must NOT fall back to mock,
        # and MUST redact the secret from top-level error, nested test_generation.error,
        # agent_records, logs, and persisted SQLite JSON!
        call_count = 0

        def _failing_groq_generate(_prompt: str) -> str:
            nonlocal call_count
            call_count += 1
            raise RuntimeError(f"Groq HTTP 401 unauthorized for token {synthetic_key}")

        monkeypatch.setattr(
            "project.agents.munshif_testing.groq_model.generate",
            _failing_groq_generate,
        )

        fail_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": "Verify Groq error redaction and no silent fallback",
                "execution_mode": ExecutionMode.LIVE_LLM.value,
            },
        ).json()["run_id"]
        app.state.executor.wait_for_run(fail_id, timeout=30.0)

        assert call_count == 1, "Orchestrator/TestingAdapter must not silently retry paid LLM calls"

        fail_res = client.get(f"/api/runs/{fail_id}")
        assert fail_res.status_code == 200
        assert synthetic_key not in fail_res.text
        fail_body = fail_res.json()
        assert fail_body["state"] == "FAILED"
        assert fail_body["test_generation"]["is_mock_output"] is False
        assert fail_body["test_generation"]["error"]["code"] == "M03_GENERATION_ERROR"
        assert "[REDACTED_GROQ_KEY]" in fail_body["test_generation"]["error"]["message"]

        logs_res = client.get(f"/api/runs/{fail_id}/logs")
        assert synthetic_key not in logs_res.text

        # Confirm SQLite database file itself contains zero unredacted secret occurrences
        raw_db_bytes = cfg.database_path.read_bytes()
        assert synthetic_key.encode("utf-8") not in raw_db_bytes
