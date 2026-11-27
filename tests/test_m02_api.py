"""Offline FastAPI integration tests for ChangeGuard AI (Module 02 REST API)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from project.main import create_app
from project.marsadh_orchestrator import OrchestratorConfig
from project.schemas import WorkflowState


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for API tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _create_git_fixture(repo: Path, *, modify: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    pkg = repo / "app"
    pkg.mkdir()
    (pkg / "billing.py").write_text(
        "def checkout(amount, customer_type='regular'):\n"
        "    return amount\n",
        encoding="utf-8",
    )
    (pkg / "refunds.py").write_text(
        "from billing import checkout\n\n"
        "def refund(amount, customer_type='regular'):\n"
        "    return checkout(amount, customer_type)\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    if modify:
        (pkg / "billing.py").write_text(
            "def checkout(amount, customer_type='regular'):\n"
            "    if customer_type == 'premium':\n"
            "        return amount * 0.9\n"
            "    return amount\n",
            encoding="utf-8",
        )


def _passing_stub(_prompt: str) -> str:
    return json.dumps(
        {
            "scenarios": [
                {
                    "title": "Premium checkout applies 10% discount",
                    "rationale": "Verify modified billing.checkout discount logic",
                    "assumptions": "Offline deterministic stub assumption",
                    "test_code": (
                        "import sys\n"
                        "from pathlib import Path\n"
                        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))\n"
                        "from billing import checkout\n"
                        "def test_premium_discount():\n"
                        "    assert checkout(1000, 'premium') == 900\n"
                    ),
                },
                {
                    "title": "Regular checkout applies no discount",
                    "rationale": "Verify regular customer behavior unchanged",
                    "assumptions": "Offline deterministic stub assumption",
                    "test_code": (
                        "import sys\n"
                        "from pathlib import Path\n"
                        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))\n"
                        "from billing import checkout\n"
                        "def test_regular_checkout():\n"
                        "    assert checkout(1000, 'regular') == 1000\n"
                    ),
                },
            ]
        }
    )


def _failing_stub(_prompt: str) -> str:
    return json.dumps(
        {
            "scenarios": [
                {
                    "title": "Failing discount expectation",
                    "rationale": "Demonstrates real pytest assertion failure",
                    "assumptions": "Offline deterministic stub",
                    "test_code": (
                        "import sys\n"
                        "from pathlib import Path\n"
                        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))\n"
                        "from billing import checkout\n"
                        "def test_failing_discount():\n"
                        "    assert checkout(1000, 'premium') == 800\n"
                    ),
                }
            ]
        }
    )


def _build_test_app(
    tmp_path: Path,
    *,
    repo: Path,
    clean_repo: Path | None = None,
    stub=None,
    secret_key: str | None = None,
):
    configured = {"demo-shop": repo}
    if clean_repo is not None:
        configured["clean-shop"] = clean_repo
    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / "api_test.sqlite3",
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[tmp_path / "repos"],
        configured_repositories=configured,
        trusted_execution_roots=[tmp_path / "repos"],
        verification_timeout_seconds=30,
        groq_model="llama-3.3-70b-versatile" if secret_key else None,
        groq_api_key=SecretStr(secret_key) if secret_key else None,
        cors_allowed_origins=["http://localhost:5173"],
        environment="test",
    )
    return create_app(cfg, offline_model_callable=stub or _passing_stub)


def test_health_openapi_cors_and_no_secret_leakage(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    synthetic_secret = "gsk_" + "TestSecretValue1234567890ABCD"
    app = _build_test_app(tmp_path, repo=repo, secret_key=synthetic_secret)

    with TestClient(app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["database_available"] is True
        assert body["live_llm_configured"] is True
        assert body["verification_dependencies_available"] is True
        assert body["git_available"] is True
        assert body["configured_repository_ids"] == ["demo-shop"]
        assert synthetic_secret not in res.text

        # OpenAPI & Swagger docs available
        openapi_res = client.get("/openapi.json")
        assert openapi_res.status_code == 200
        assert "/api/runs" in openapi_res.json()["paths"]
        assert client.get("/docs").status_code == 200

        # Configured CORS allows http://localhost:5173 and rejects unconfigured origins
        allowed_preflight = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed_preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert allowed_preflight.headers.get("access-control-allow-credentials") != "true"

        denied_preflight = client.options(
            "/health",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert denied_preflight.headers.get("access-control-allow-origin") is None


def test_full_api_lifecycle_approval_pagination_and_no_host_path_exposure(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    clean_repo = tmp_path / "repos" / "clean"
    _create_git_fixture(repo, modify=True)
    _create_git_fixture(clean_repo, modify=False)
    app = _build_test_app(tmp_path, repo=repo, clean_repo=clean_repo)

    with TestClient(app) as client:
        # 1. Create run (HTTP 202 Accepted)
        create_res = client.post(
            "/api/runs",
            json={
                "repository_id": "demo-shop",
                "source_path": "app",
                "base_ref": "HEAD",
                "requirements": "Apply 10% discount for premium customers.",
                "execution_mode": "offline_test",
            },
        )
        assert create_res.status_code == 202
        created = create_res.json()
        run_id = created["run_id"]
        assert created["state"] == WorkflowState.CREATED.value

        # Deterministically wait for in-process background stage (no sleep polling)
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        # 2. Inspect run detail at Gate 1
        detail_res = client.get(f"/api/runs/{run_id}")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert detail["allowed_actions"] == ["submit_test_review"]
        assert detail["codebase_analysis"]["changed_files_repo_relative"] == ["app/billing.py"]
        assert len(detail["test_generation"]["scenarios"]) == 2
        assert detail["test_generation"]["executed"] is False
        assert detail["test_generation"]["is_mock_output"] is True
        # Ensure host temp paths & raw payloads are NOT leaked in API responses
        assert str(tmp_path) not in detail_res.text
        assert "workspace_path" not in detail
        assert "raw_report" not in detail_res.text
        assert "raw_proposals" not in detail_res.text

        # Cannot submit final decision while in AWAITING_TEST_REVIEW (409)
        premature = client.post(
            f"/api/runs/{run_id}/decision",
            json={"decision": "approve", "reviewer": "nafris", "comments": "too early"},
        )
        assert premature.status_code == 409

        # 3. Approve selected tests (HTTP 202 Accepted -> schedules verification)
        review_res = client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001"],
                "reviewer": "nafris",
                "notes": "Verified premium discount scenario.",
            },
        )
        assert review_res.status_code == 202
        assert review_res.json()["state"] == WorkflowState.PREPARING_BASELINE.value

        # Duplicate approval immediately rejected with 409
        dup_res = client.post(
            f"/api/runs/{run_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001"],
                "reviewer": "nafris",
            },
        )
        assert dup_res.status_code == 409

        # Wait deterministically for verification & report compilation
        app.state.executor.wait_for_run(run_id, timeout=30.0)

        gate2_res = client.get(f"/api/runs/{run_id}")
        assert gate2_res.status_code == 200
        gate2 = gate2_res.json()
        assert gate2["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2["verification"]["verification_passed"] is True
        assert gate2["verification"]["executed_test_ids"] == ["SCN-001"]
        assert gate2["report"]["recommendation_label"] == "ready_for_human_review"
        assert "raw_result" not in gate2_res.text

        # 4. Retrieve structured logs
        logs_res = client.get(f"/api/runs/{run_id}/logs")
        assert logs_res.status_code == 200
        logs_payload = logs_res.json()
        assert logs_payload["run_id"] == run_id
        assert logs_payload["count"] == len(logs_payload["logs"])
        assert logs_payload["count"] >= 8

        # 5. Final human approval (HTTP 200 OK)
        dec_res = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "approve",
                "reviewer": "nafris",
                "comments": "Verified evidence package.",
            },
        )
        assert dec_res.status_code == 200
        assert dec_res.json()["state"] == WorkflowState.APPROVED.value

        # Repeated final decision -> 409 Conflict
        repeat_dec = client.post(
            f"/api/runs/{run_id}/decision",
            json={
                "decision": "reject",
                "reviewer": "nafris",
                "comments": "Attempting conflicting decision.",
            },
        )
        assert repeat_dec.status_code == 409

        # 6. Create a clean-repo run to verify COMPLETED_NO_CHANGES & GET /api/runs filtering
        clean_res = client.post(
            "/api/runs",
            json={
                "repository_id": "clean-shop",
                "source_path": "app",
                "base_ref": "HEAD",
                "requirements": "Check clean repo",
            },
        )
        assert clean_res.status_code == 202
        clean_id = clean_res.json()["run_id"]
        app.state.executor.wait_for_run(clean_id, timeout=30.0)
        assert client.get(f"/api/runs/{clean_id}").json()["state"] == WorkflowState.COMPLETED_NO_CHANGES.value

        list_all = client.get("/api/runs?limit=10&offset=0").json()
        assert list_all["total"] == 2
        assert len(list_all["runs"]) == 2

        list_approved = client.get("/api/runs?state=APPROVED").json()
        assert list_approved["total"] == 1
        assert list_approved["runs"][0]["run_id"] == run_id


def test_api_test_rejection_and_failing_verification_flow(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    _create_git_fixture(repo, modify=True)
    app = _build_test_app(tmp_path, repo=repo, stub=_failing_stub)

    with TestClient(app) as client:
        # Run 1: Reject generated tests at Gate 1 (200 OK -> REJECTED)
        r1 = client.post(
            "/api/runs",
            json={"repository_id": "demo-shop", "source_path": "app", "requirements": "Reject test"},
        )
        run1_id = r1.json()["run_id"]
        app.state.executor.wait_for_run(run1_id, timeout=30.0)

        rej = client.post(
            f"/api/runs/{run1_id}/test-review",
            json={"approved": False, "reviewer": "nafris", "notes": "Tests have wrong assertion."},
        )
        assert rej.status_code == 200
        assert rej.json()["state"] == WorkflowState.REJECTED.value
        assert rej.json()["verification"] is None

        # Run 2: Approve failing test -> verification fails -> APPROVE blocked (409), NEEDS_REVIEW works (200)
        r2 = client.post(
            "/api/runs",
            json={"repository_id": "demo-shop", "source_path": "app", "requirements": "Fail test"},
        )
        run2_id = r2.json()["run_id"]
        app.state.executor.wait_for_run(run2_id, timeout=30.0)

        app_res = client.post(
            f"/api/runs/{run2_id}/test-review",
            json={"approved": True, "selected_test_ids": ["SCN-001"], "reviewer": "nafris"},
        )
        assert app_res.status_code == 202
        app.state.executor.wait_for_run(run2_id, timeout=30.0)

        detail = client.get(f"/api/runs/{run2_id}").json()
        assert detail["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert detail["verification"]["verification_passed"] is False
        assert detail["verification"]["verification_outcome"] == "failed_tests"
        assert detail["report"]["recommendation_label"] == "verification_failed"
        assert "submit_final_decision:approve" not in detail["allowed_actions"]

        blocked = client.post(
            f"/api/runs/{run2_id}/decision",
            json={"decision": "approve", "reviewer": "nafris", "comments": "try override"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "VERIFICATION_APPROVAL_BLOCKED"

        escalated = client.post(
            f"/api/runs/{run2_id}/decision",
            json={"decision": "needs_review", "reviewer": "nafris", "comments": "Assertion failed"},
        )
        assert escalated.status_code == 200
        assert escalated.json()["state"] == WorkflowState.NEEDS_REVIEW.value


def test_api_error_status_codes_and_safe_500_handler(tmp_path) -> None:
    repo = tmp_path / "repos" / "shop"
    outside = tmp_path / "outside_repo"
    _create_git_fixture(repo, modify=True)
    _create_git_fixture(outside, modify=True)
    app = _build_test_app(tmp_path, repo=repo)

    with TestClient(app, raise_server_exceptions=False) as client:
        # 404: Unknown run
        nf = client.get("/api/runs/run_missing_xyz")
        assert nf.status_code == 404
        assert nf.json()["code"] == "RUN_NOT_FOUND"
        assert client.get("/api/runs/run_missing_xyz/logs").status_code == 404

        # 422: Invalid DTO (blank requirements, path traversal, arbitrary test path)
        bad_dto = client.post(
            "/api/runs",
            json={"repository_id": "demo-shop", "source_path": "../escape", "requirements": "x"},
        )
        assert bad_dto.status_code == 422
        assert bad_dto.json()["code"] == "VALIDATION_ERROR"

        # 403: Disallowed repository path
        forb = client.post(
            "/api/runs",
            json={
                "repository_path": str(outside),
                "source_path": "app",
                "requirements": "Unauthorized path",
            },
        )
        assert forb.status_code == 403
        assert forb.json()["code"] == "REPOSITORY_ACCESS_DENIED"

        # 500: Safe error response without traceback or secrets
        @app.get("/_test_boom")
        async def _boom():
            raise RuntimeError("secret=gsk_ShouldNeverAppearIn500Response12345678")

        boom_res = client.get("/_test_boom")
        assert boom_res.status_code == 500
        boom_json = boom_res.json()
        assert boom_json["code"] == "INTERNAL_SERVER_ERROR"
        assert "Traceback" not in boom_res.text
        assert "gsk_ShouldNeverAppearIn500Response" not in boom_res.text
