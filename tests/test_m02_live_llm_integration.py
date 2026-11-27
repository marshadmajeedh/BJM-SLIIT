"""Offline verification tests for ChangeGuard AI M02 Live LLM Integration (P0-4).

All tests in this module run 100% offline using controlled mocks and NEVER
require real Groq credentials or make external network calls.

Covers:
- Phase 2: Actual Groq SDK invocation wiring through Munshif's unmodified
  ``project.agents.munshif_testing.groq_model.generate`` (client timeout=60,
  max_retries=2, model, json_object response format, temperature=0.1),
  thread-safe env isolation via ``_GROQ_ENV_LOCK``, and zero mock fallback.
- Phase 7: All controlled-mock live-mode failure scenarios:
  1. Missing API key.
  2. Missing model configuration.
  3. Invalid model response (None, non-dict JSON, missing fields, no test_*, syntax error).
  4. Provider authentication failure (groq.AuthenticationError / HTTP 401).
  5. Rate limit (groq.RateLimitError / HTTP 429).
  6. Provider timeout (groq.APITimeoutError and TimeoutError).
  7. Invalid JSON.
  8. Empty test scenarios.
  9. Unsupported / decommissioned model (groq.NotFoundError / groq.BadRequestError).
  10. Secret redaction in deeply nested errors (dicts/lists, Bearer tokens, gsk_* keys).
- Phase 8: Live-mode evidence integrity (generation_mode="live_llm",
  is_mock_output=False, model_label="groq:<model>", Gate 1 mandatory,
  Gate 2 HTTP 409 block on failed verification, original repo unchanged).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
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
    AgentError,
    AgentExecutionStatus,
    AgentLogEntry,
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
    "    return total * 0.90\n\n\n"
    "def checkout(price, quantity, customer_type):\n"
    "    total = calculate_total(price, quantity)\n"
    "    return apply_discount(total, customer_type)\n"
)


def _git(repo: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        pytest.skip("Git is required for M02 live integration tests")
    proc = subprocess.run(
        [git, "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _create_mini_pos_fixture(repo: Path, *, mode: str = "correct") -> str:
    app_dir = repo / "mini_pos"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "billing.py").write_text(BASELINE_BILLING, encoding="utf-8")
    for fname in ("refunds.py", "customer_service.py", "dashboard.py", "reports.py"):
        shutil.copyfile(MINI_POS_DIR / fname, app_dir / fname)

    _git(repo, "init")
    _git(repo, "config", "user.name", "ChangeGuard P0-4")
    _git(repo, "config", "user.email", "p04@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    if mode == "correct":
        (app_dir / "billing.py").write_text(CORRECT_BILLING, encoding="utf-8")
    elif mode == "regressive":
        (app_dir / "billing.py").write_text(REGRESSIVE_BILLING, encoding="utf-8")
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    return base_commit


def _valid_behavioral_json() -> str:
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
                    "rationale": "Verify premium discount on billing.checkout.",
                    "assumptions": "Premium customers receive 10% off; human review required.",
                    "test_code": (
                        header
                        + "from billing import checkout\n\n"
                        + "def test_premium_discount():\n"
                        + "    assert checkout(100.0, 2, 'premium') == pytest.approx(180.0)\n"
                    ),
                },
                {
                    "title": "Regular customer checkout remains unchanged",
                    "rationale": "Verify regular customers are not discounted.",
                    "assumptions": "Regular customers pay full price; human review required.",
                    "test_code": (
                        header
                        + "from billing import checkout\n\n"
                        + "def test_regular_unchanged():\n"
                        + "    assert checkout(100.0, 2, 'regular') == pytest.approx(200.0)\n"
                    ),
                },
            ]
        }
    )


def _mock_httpx_response(status_code: int) -> httpx.Response:
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return httpx.Response(status_code=status_code, request=req)


def _build_live_app(
    tmp_path: Path,
    *,
    repos_root: Path,
    configured_repos: dict[str, Path],
    api_key: str | None = None,
    model: str | None = None,
    db_name: str = "live_test.sqlite3",
    requirement_live_callable=None,
    requirement_live_label: str | None = None,
):
    cfg = OrchestratorConfig(
        database_path=tmp_path / "state" / db_name,
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories=configured_repos,
        trusted_execution_roots=[repos_root],
        groq_api_key=SecretStr(api_key) if api_key else None,
        groq_model=model,
        verification_timeout_seconds=30,
        environment="test",
    )
    return cfg, create_app(
        cfg,
        requirement_live_callable=requirement_live_callable,
        requirement_live_label=requirement_live_label,
        offline_model_callable=default_offline_model_stub,
    )


def _run_live_and_wait(
    client: TestClient,
    app,
    *,
    repo_id: str = "pos-ok",
    requirements: str = REQUIREMENT_TEXT,
) -> dict[str, Any]:
    create_res = client.post(
        "/api/runs",
        json={
            "repository_id": repo_id,
            "source_path": "mini_pos",
            "base_ref": "HEAD",
            "requirements": requirements,
            "execution_mode": ExecutionMode.LIVE_LLM.value,
        },
    )
    assert create_res.status_code == 202
    run_id = create_res.json()["run_id"]
    app.state.executor.wait_for_run(run_id, timeout=30.0)
    detail_res = client.get(f"/api/runs/{run_id}")
    assert detail_res.status_code == 200
    return detail_res.json()


# ---------------------------------------------------------------------------
# Phase 2 & 8: Real groq_model.generate SDK Wiring & Evidence Integrity
# ---------------------------------------------------------------------------


def test_real_groq_adapter_sdk_wiring_and_live_evidence_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Munshif's unmodified groq_model.generate receives exact SDK parameters."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    repos_root = tmp_path / "repos"
    repo_ok = repos_root / "pos_correct"
    repo_bad = repos_root / "pos_regressive"
    base_ok = _create_mini_pos_fixture(repo_ok, mode="correct")
    base_bad = _create_mini_pos_fixture(repo_bad, mode="regressive")
    orig_ok_status = _git(repo_ok, "status", "--porcelain")
    orig_bad_status = _git(repo_bad, "status", "--porcelain")

    synthetic_key = "gsk_" + "ControlledSdkWiringTestKey1234567890"
    configured_model = "openai/gpt-oss-120b"
    captured_init: list[dict[str, Any]] = []
    captured_create: list[dict[str, Any]] = []

    class _FakeGroqClient:
        def __init__(self, *, api_key: str, timeout: int, max_retries: int) -> None:
            captured_init.append(
                {"api_key": api_key, "timeout": timeout, "max_retries": max_retries}
            )
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs: Any) -> Any:
            captured_create.append(kwargs)
            messages = kwargs.get("messages", [])
            system_msg = messages[0]["content"] if messages else ""
            user_msg = messages[1]["content"] if len(messages) > 1 else ""
            if "Requirement Analyst" in system_msg:
                content = deterministic_offline_requirement_analyzer(user_msg)
            else:
                content = _valid_behavioral_json()
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content)
                    )
                ]
            )

    monkeypatch.setattr(groq, "Groq", _FakeGroqClient)

    cfg, app = _build_live_app(
        tmp_path,
        repos_root=repos_root,
        configured_repos={"pos-ok": repo_ok, "pos-bad": repo_bad},
        api_key=synthetic_key,
        model=configured_model,
    )

    with TestClient(app) as client:
        health = client.get("/health").json()
        assert health["live_llm_configured"] is True
        assert health["groq_model"] == configured_model
        assert synthetic_key not in json.dumps(health)

        # Scenario A (Correct)
        gate1_ok = _run_live_and_wait(client, app, repo_id="pos-ok")
        assert gate1_ok["state"] == WorkflowState.AWAITING_TEST_REVIEW.value
        assert gate1_ok["requirement_analysis"]["analysis_mode"] == "live_llm"
        assert gate1_ok["requirement_analysis"]["is_deterministic_fixture"] is False
        assert gate1_ok["requirement_analysis"]["model_label"] == f"groq:{configured_model}"
        assert gate1_ok["test_generation"]["generation_mode"] == "live_llm"
        assert gate1_ok["test_generation"]["is_mock_output"] is False
        assert gate1_ok["test_generation"]["model_label"] == f"groq:{configured_model}"
        assert gate1_ok["test_generation"]["executed"] is False
        assert gate1_ok["verification"] is None

        # Verify exact SDK initialization & chat completion contract for BOTH A1 and M03
        assert len(captured_init) == 2
        for init_call in captured_init:
            assert init_call == {
                "api_key": synthetic_key,
                "timeout": 60,
                "max_retries": 2,
            }
        assert len(captured_create) == 2
        for create_call in captured_create:
            assert create_call["model"] == configured_model
            assert create_call["response_format"] == {"type": "json_object"}
            assert create_call["temperature"] == 0.1
        # Process env must be restored cleanly after invocation
        assert os.environ.get("GROQ_API_KEY") is None
        assert os.environ.get("GROQ_MODEL") is None

        # Approve & verify Scenario A
        run_ok_id = gate1_ok["run_id"]
        rev_ok = client.post(
            f"/api/runs/{run_ok_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002"],
                "reviewer": "marsadh",
                "notes": "Reviewed controlled-mock live scenarios.",
            },
        )
        assert rev_ok.status_code == 202
        app.state.executor.wait_for_run(run_ok_id, timeout=30.0)
        gate2_ok = client.get(f"/api/runs/{run_ok_id}").json()
        assert gate2_ok["state"] == WorkflowState.AWAITING_FINAL_APPROVAL.value
        assert gate2_ok["verification"]["verification_passed"] is True
        assert gate2_ok["report"]["is_mock_output"] is False
        assert gate2_ok["report"]["model_label"] == f"groq:{configured_model}"
        assert gate2_ok["report"]["traceability"]["requirement_verification_proven"] is False
        assert gate2_ok["verification"]["baseline_comparison"]["status"] == "completed"
        assert gate2_ok["verification"]["baseline_comparison"]["summary"]["has_confirmed_regression"] is False

        # Scenario B (Regressive) -> ordinary final approval blocked with HTTP 409
        gate1_bad = _run_live_and_wait(client, app, repo_id="pos-bad")
        run_bad_id = gate1_bad["run_id"]
        rev_bad = client.post(
            f"/api/runs/{run_bad_id}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002"],
                "reviewer": "marsadh",
            },
        )
        assert rev_bad.status_code == 202
        app.state.executor.wait_for_run(run_bad_id, timeout=30.0)
        gate2_bad = client.get(f"/api/runs/{run_bad_id}").json()
        assert gate2_bad["verification"]["verification_passed"] is False
        assert gate2_bad["verification"]["verification_outcome"] == "failed_tests"

        blocked = client.post(
            f"/api/runs/{run_bad_id}/decision",
            json={"decision": "approve", "reviewer": "marsadh", "comments": "try approve"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "VERIFICATION_APPROVAL_BLOCKED"

    # Confirm zero credential leaks in SQLite and zero mutations on target repositories
    assert synthetic_key.encode("utf-8") not in cfg.database_path.read_bytes()
    assert _git(repo_ok, "rev-parse", "HEAD") == base_ok
    assert _git(repo_ok, "status", "--porcelain") == orig_ok_status
    assert _git(repo_bad, "rev-parse", "HEAD") == base_bad
    assert _git(repo_bad, "status", "--porcelain") == orig_bad_status


# ---------------------------------------------------------------------------
# Phase 7: Missing Configuration (Missing API Key / Missing Model)
# ---------------------------------------------------------------------------


def test_missing_api_key_and_missing_model_fail_closed_without_mock_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_correct"
    _create_mini_pos_fixture(repo, mode="correct")

    # 1. Missing GROQ_API_KEY (GROQ_MODEL present)
    _, app_no_key = _build_live_app(
        tmp_path,
        repos_root=repos_root,
        configured_repos={"pos-ok": repo},
        api_key=None,
        model="openai/gpt-oss-120b",
        db_name="no_key.sqlite3",
    )
    with TestClient(app_no_key) as client:
        health = client.get("/health").json()
        assert health["live_llm_configured"] is False
        assert health["groq_model"] is None

        failed = _run_live_and_wait(client, app_no_key)
        assert failed["state"] == WorkflowState.FAILED.value
        assert failed["error"]["code"] == "LIVE_LLM_UNCONFIGURED"
        assert "GROQ_API_KEY" in failed["error"]["message"]
        assert failed["requirement_analysis"]["analysis_mode"] == "live_llm"
        assert failed["requirement_analysis"]["is_deterministic_fixture"] is False
        assert failed["requirement_analysis"]["is_mock_output"] is False
        assert failed["test_generation"] is None

        logs = client.get(f"/api/runs/{failed['run_id']}/logs").json()
        assert any(
            entry["error"] and entry["error"]["code"] == "LIVE_LLM_UNCONFIGURED"
            for entry in logs["logs"]
        )

    # 2. Missing GROQ_MODEL (GROQ_API_KEY present)
    synthetic_key = "gsk_" + "MissingModelTestKeySecret1234567890"
    cfg_no_model, app_no_model = _build_live_app(
        tmp_path,
        repos_root=repos_root,
        configured_repos={"pos-ok": repo},
        api_key=synthetic_key,
        model=None,
        db_name="no_model.sqlite3",
    )
    with TestClient(app_no_model) as client:
        health_res = client.get("/health")
        assert health_res.status_code == 200
        assert synthetic_key not in health_res.text
        health = health_res.json()
        assert health["live_llm_configured"] is False
        assert health["groq_model"] is None

        failed = _run_live_and_wait(client, app_no_model)
        assert failed["state"] == WorkflowState.FAILED.value
        assert failed["error"]["code"] == "LIVE_LLM_UNCONFIGURED"
        assert "GROQ_MODEL" in failed["error"]["message"]
        assert failed["requirement_analysis"]["is_deterministic_fixture"] is False
        assert failed["requirement_analysis"]["is_mock_output"] is False
        assert failed["test_generation"] is None
        assert synthetic_key not in json.dumps(failed)
    assert synthetic_key.encode("utf-8") not in cfg_no_model.database_path.read_bytes()


# ---------------------------------------------------------------------------
# Phase 7: Invalid Model Responses, Invalid JSON, Empty Scenarios
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_output", "expected_code", "expected_retryable"),
    [
        # Invalid JSON
        ("{not valid json", "INVALID_MODEL_JSON", True),
        # Empty test scenarios
        (json.dumps({"scenarios": []}), "INVALID_MODEL_OUTPUT", False),
        # Non-dict JSON response
        (json.dumps([1, 2, 3]), "INVALID_MODEL_OUTPUT", False),
        # Missing required scenario fields
        (
            json.dumps({"scenarios": [{"title": "Only title"}]}),
            "INVALID_MODEL_OUTPUT",
            False,
        ),
        # Missing pytest def test_* function
        (
            json.dumps(
                {
                    "scenarios": [
                        {
                            "title": "No pytest function",
                            "rationale": "Helper only",
                            "assumptions": "None",
                            "test_code": "def helper():\n    return 42\n",
                        }
                    ]
                }
            ),
            "INVALID_MODEL_OUTPUT",
            False,
        ),
        # Invalid Python syntax in test_code
        (
            json.dumps(
                {
                    "scenarios": [
                        {
                            "title": "Broken syntax",
                            "rationale": "Syntax error",
                            "assumptions": "None",
                            "test_code": "def test_broken(:\n    assert True\n",
                        }
                    ]
                }
            ),
            "M03_GENERATION_ERROR",
            False,
        ),
        # Provider returned None content
        (None, "M03_GENERATION_ERROR", False),
    ],
)
def test_invalid_model_responses_fail_without_mock_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_output: str | None,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_correct"
    _create_mini_pos_fixture(repo, mode="correct")

    synthetic_key = "gsk_" + "InvalidResponseTestKey1234567890ABCD"
    call_count = 0

    def _fake_generate(_prompt: str):
        nonlocal call_count
        call_count += 1
        return raw_output

    monkeypatch.setattr(
        "project.agents.munshif_testing.groq_model.generate",
        _fake_generate,
    )

    _, app = _build_live_app(
        tmp_path,
        repos_root=repos_root,
        configured_repos={"pos-ok": repo},
        api_key=synthetic_key,
        model="openai/gpt-oss-120b",
        requirement_live_callable=deterministic_offline_requirement_analyzer,
        requirement_live_label="groq:openai/gpt-oss-120b",
    )
    with TestClient(app) as client:
        failed = _run_live_and_wait(client, app)
        assert call_count == 1
        assert failed["state"] == WorkflowState.FAILED.value
        assert failed["error"]["code"] == expected_code
        assert failed["error"]["retryable"] is expected_retryable
        assert failed["test_generation"]["generation_mode"] == "live_llm"
        assert failed["test_generation"]["is_mock_output"] is False
        assert failed["test_generation"]["scenarios"] == []

        logs = client.get(f"/api/runs/{failed['run_id']}/logs").json()
        assert logs["count"] >= 4
        assert any(
            entry["error"] and entry["error"]["code"] == expected_code
            for entry in logs["logs"]
        )


# ---------------------------------------------------------------------------
# Phase 7: Controlled Provider Exceptions (Auth, Rate Limit, Timeout, Unsupported Model)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc_factory", "expected_error_type", "expected_retryable"),
    [
        # 4. Provider authentication failure (HTTP 401 with embedded secrets)
        (
            lambda key: groq.AuthenticationError(
                f"Invalid API Key {key} (Authorization: Bearer {key})",
                response=_mock_httpx_response(401),
                body={"error": {"message": f"Invalid key {key}"}},
            ),
            "AuthenticationError",
            False,
        ),
        # 5. Rate limit (HTTP 429)
        (
            lambda _key: groq.RateLimitError(
                "Rate limit reached for requests per minute",
                response=_mock_httpx_response(429),
                body={"error": {"type": "rate_limit_exceeded"}},
            ),
            "RateLimitError",
            True,
        ),
        # 6a. Provider timeout (groq.APITimeoutError)
        (
            lambda _key: groq.APITimeoutError(
                request=httpx.Request(
                    "POST", "https://api.groq.com/openai/v1/chat/completions"
                )
            ),
            "APITimeoutError",
            True,
        ),
        # 6b. Generic Python TimeoutError
        (
            lambda _key: TimeoutError("Groq request timed out after 60s"),
            "TimeoutError",
            True,
        ),
        # 9a. Unsupported / unknown model (HTTP 404 NotFoundError)
        (
            lambda _key: groq.NotFoundError(
                "The model `unsupported-model-xyz` does not exist or you do not have access to it.",
                response=_mock_httpx_response(404),
                body={"error": {"code": "model_not_found"}},
            ),
            "NotFoundError",
            False,
        ),
        # 9b. Decommissioned model (HTTP 400 BadRequestError)
        (
            lambda _key: groq.BadRequestError(
                "The model `legacy-model` has been decommissioned and is no longer supported.",
                response=_mock_httpx_response(400),
                body={"error": {"code": "model_decommissioned"}},
            ),
            "BadRequestError",
            False,
        ),
    ],
)
def test_provider_failures_persist_structured_errors_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc_factory,
    expected_error_type: str,
    expected_retryable: bool,
) -> None:
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_correct"
    _create_mini_pos_fixture(repo, mode="correct")

    synthetic_key = "gsk_" + "ProviderFailureSecretKey1234567890AB"
    call_count = 0

    def _failing_generate(_prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        raise exc_factory(synthetic_key)

    monkeypatch.setattr(
        "project.agents.munshif_testing.groq_model.generate",
        _failing_generate,
    )

    cfg, app = _build_live_app(
        tmp_path,
        repos_root=repos_root,
        configured_repos={"pos-ok": repo},
        api_key=synthetic_key,
        model="unsupported-or-test-model",
        requirement_live_callable=deterministic_offline_requirement_analyzer,
        requirement_live_label="groq:unsupported-or-test-model",
    )
    with TestClient(app) as client:
        failed = _run_live_and_wait(client, app)
        assert call_count == 1, "Orchestrator must not silently retry provider failures"
        assert failed["state"] == WorkflowState.FAILED.value
        assert failed["test_generation"]["generation_mode"] == "live_llm"
        assert failed["test_generation"]["is_mock_output"] is False
        assert failed["test_generation"]["scenarios"] == []
        assert failed["error"]["code"] == "M03_GENERATION_ERROR"
        assert failed["error"]["retryable"] is expected_retryable
        assert failed["error"]["details"]["provider_error_type"] == expected_error_type
        assert synthetic_key not in json.dumps(failed)

        logs_res = client.get(f"/api/runs/{failed['run_id']}/logs")
        assert logs_res.status_code == 200
        assert synthetic_key not in logs_res.text
        assert any(
            entry["agent_name"] == "TestingAdapter"
            and entry["status"] == "failed"
            and entry["error"]["code"] == "M03_GENERATION_ERROR"
            for entry in logs_res.json()["logs"]
        )

    assert synthetic_key.encode("utf-8") not in cfg.database_path.read_bytes()


# ---------------------------------------------------------------------------
# Phase 7: Deeply Nested Secret Redaction Across Store, API, and SQLite
# ---------------------------------------------------------------------------


def test_secret_redaction_in_deeply_nested_errors_and_logs(tmp_path: Path) -> None:
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_correct"
    _create_mini_pos_fixture(repo, mode="correct")

    fake_gsk = "gsk_" + "DeepNestedGroqSecretKey1234567890XYZ"
    fake_bearer = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.NestedBearerSecret"
    fake_kv = "SuperSecretValue987654"

    cfg, app = _build_live_app(
        tmp_path,
        repos_root=repos_root,
        configured_repos={"pos-ok": repo},
        api_key=fake_gsk,
        model="openai/gpt-oss-120b",
        db_name="nested_redact.sqlite3",
    )

    nested_error = AgentError(
        code="M03_GENERATION_ERROR",
        message=f"Auth failed with Bearer {fake_bearer} and key {fake_gsk}",
        details={
            "provider": "groq",
            "nested_response": {
                "headers": {
                    "Authorization": f"Bearer {fake_bearer}",
                    "X-Key": f"api_key={fake_kv}",
                },
                "causes": [
                    f"rejected token {fake_gsk}",
                    {"inner_secret": f"secret={fake_kv}"},
                ],
            },
        },
    )

    store = RunStore(cfg.database_path)
    run_id = "run_nested_redaction_p04"
    store.save_run(
        WorkflowRunRecord(
            run_id=run_id,
            state=WorkflowState.FAILED,
            execution_mode=ExecutionMode.LIVE_LLM,
            requirements="Verify nested secret redaction",
            repository=RepositoryInfo(
                repository_id="pos-ok",
                repository_path=str(repo),
                source_path="mini_pos",
                base_ref="HEAD",
            ),
            error=nested_error,
        )
    )
    store.append_log(
        AgentLogEntry(
            run_id=run_id,
            agent_name="TestingAdapter",
            stage=WorkflowState.GENERATING_TESTS,
            status=AgentExecutionStatus.FAILED,
            duration_ms=42,
            input_summary=f"Authorization: Bearer {fake_bearer}",
            output_summary=f"Failed with {fake_gsk}",
            error=nested_error,
        )
    )

    with TestClient(app) as client:
        run_res = client.get(f"/api/runs/{run_id}")
        assert run_res.status_code == 200
        logs_res = client.get(f"/api/runs/{run_id}/logs")
        assert logs_res.status_code == 200

        for text in (run_res.text, logs_res.text):
            assert fake_gsk not in text
            assert fake_bearer not in text
            assert fake_kv not in text
            assert "[REDACTED_GROQ_KEY]" in text
            assert "Bearer [REDACTED]" in text

        nested_details = run_res.json()["error"]["details"]["nested_response"]
        assert nested_details["headers"]["Authorization"] == "Bearer [REDACTED]"
        assert nested_details["headers"]["X-Key"] == "api_key=[REDACTED]"
        assert "[REDACTED_GROQ_KEY]" in nested_details["causes"][0]
        assert nested_details["causes"][1]["inner_secret"] == "secret=[REDACTED]"

    raw_db = cfg.database_path.read_bytes()
    assert fake_gsk.encode("utf-8") not in raw_db
    assert fake_bearer.encode("utf-8") not in raw_db
    assert fake_kv.encode("utf-8") not in raw_db


# ---------------------------------------------------------------------------
# Phase 2: Concurrent Live Execution Env Lock Isolation
# ---------------------------------------------------------------------------


def test_concurrent_live_invocations_serialize_env_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm _GROQ_ENV_LOCK prevents cross-thread GROQ_API_KEY / GROQ_MODEL races."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_OriginalProcessKeyBeforeTest12345")
    monkeypatch.setenv("GROQ_MODEL", "original-model")

    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_correct"
    _create_mini_pos_fixture(repo, mode="correct")

    active = 0
    max_overlap = 0
    observed_pairs: list[tuple[str | None, str | None]] = []
    guard = threading.Lock()

    def _slow_fake_generate(_prompt: str) -> str:
        nonlocal active, max_overlap
        with guard:
            active += 1
            max_overlap = max(max_overlap, active)
        try:
            k1 = os.environ.get("GROQ_API_KEY")
            m1 = os.environ.get("GROQ_MODEL")
            time.sleep(0.05)
            assert os.environ.get("GROQ_API_KEY") == k1
            assert os.environ.get("GROQ_MODEL") == m1
            with guard:
                observed_pairs.append((k1, m1))
            return _valid_behavioral_json()
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(
        "project.agents.munshif_testing.groq_model.generate",
        _slow_fake_generate,
    )

    from project.marsadh_orchestrator.adapters import CodebaseAdapter, TestingAdapter

    cfg_a = OrchestratorConfig(
        allowed_repository_roots=[repos_root],
        groq_api_key=SecretStr("gsk_" + "TenantAKeyForConcurrencyCheck123456"),
        groq_model="model-a",
    )
    cfg_b = OrchestratorConfig(
        allowed_repository_roots=[repos_root],
        groq_api_key=SecretStr("gsk_" + "TenantBKeyForConcurrencyCheck123456"),
        groq_model="model-b",
    )
    cb_result = CodebaseAdapter(cfg_a).analyze(repo, source_path="mini_pos", base_ref="HEAD")
    assert cb_result.status == "completed"

    barrier = threading.Barrier(2)

    def _run_adapter(cfg: OrchestratorConfig):
        barrier.wait()
        return TestingAdapter(cfg).generate_proposals(
            repo,
            cb_result,
            REQUIREMENT_TEXT,
            execution_mode=ExecutionMode.LIVE_LLM,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_adapter, cfg_a)
        f2 = pool.submit(_run_adapter, cfg_b)
        r1, r2 = f1.result(), f2.result()

    assert r1.status == "proposed" and r2.status == "proposed"
    assert max_overlap == 1, "Concurrent groq_model calls must be serialized by _GROQ_ENV_LOCK"
    assert set(observed_pairs) == {
        ("gsk_" + "TenantAKeyForConcurrencyCheck123456", "model-a"),
        ("gsk_" + "TenantBKeyForConcurrencyCheck123456", "model-b"),
    }
    assert os.environ.get("GROQ_API_KEY") == "gsk_OriginalProcessKeyBeforeTest12345"
    assert os.environ.get("GROQ_MODEL") == "original-model"
