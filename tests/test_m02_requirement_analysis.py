"""Comprehensive offline test suite for P0-5 A1 Structured Requirement Analyst Agent.

Covers all 15 required P0-5 verification areas plus a deterministic offline
demonstration using the premium-customer discount requirement on mini_pos:
1.  Valid structured requirement analysis.
2.  Multiple acceptance criteria.
3.  Explicit requirements versus inferred assumptions.
4.  Ambiguous requirements.
5.  Missing or invalid model output.
6.  Duplicate criterion IDs (and constraint/assumption/ambiguity IDs).
7.  Invalid criterion references (within A1 and from M03 scenarios).
8.  Live provider failure without mock fallback (controlled mocks; zero real network calls).
9.  Requirement analysis persistence & reload across RunStore instances.
10. Workflow state transitions & restart recovery from ANALYZING_REQUIREMENTS.
11. Backward compatibility of existing pre-P0-5 persisted runs.
12. Requirement-to-test traceability (unavailable mapping, explicit pass, explicit fail, no fabrication).
13. Incomplete mandatory requirement coverage.
14. Secret redaction across A1 errors, API responses, logs, and SQLite bytes.
15. Existing human approval and verification gates (plus blocking ambiguity gate).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import groq
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from project.main import create_app
from project.marsadh_orchestrator import (
    DEFAULT_OFFLINE_REQUIREMENT_LABEL,
    InvalidStateTransitionError,
    OrchestratorConfig,
    OrchestratorService,
    RequirementAnalystAgent,
    ReviewConflictError,
    RunStore,
    WorkspaceManager,
    deterministic_offline_requirement_analyzer,
    format_requirements_for_downstream,
)
from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    TestingAdapter,
    VerificationAdapter,
)
from project.schemas import (
    AcceptanceCriterion,
    AmbiguitySeverity,
    BusinessConstraint,
    CreateRunRequest,
    CriterionOrigin,
    CriterionPriority,
    ExecutionMode,
    HumanDecision,
    HumanDecisionRequest,
    RepositoryInfo,
    RequirementAmbiguity,
    RequirementAnalysisResult,
    RequirementAssumption,
    RequirementTraceability,
    TestReviewRequest,
    TraceabilityEvidenceStatus,
    VerificationOutcome,
    VerificationStrategy,
    WorkflowRunRecord,
    WorkflowState,
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
        pytest.skip("Git is required for P0-5 integration tests")
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
    _git(repo, "config", "user.name", "ChangeGuard P0-5")
    _git(repo, "config", "user.email", "p05@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline mini_pos")
    base_commit = _git(repo, "rev-parse", "HEAD")

    if mode == "correct":
        (app_dir / "billing.py").write_text(CORRECT_BILLING, encoding="utf-8")
    elif mode == "regressive":
        (app_dir / "billing.py").write_text(REGRESSIVE_BILLING, encoding="utf-8")
    elif mode != "clean":
        raise ValueError(f"Unsupported mode: {mode}")
    return base_commit


def _make_config(
    tmp_path: Path,
    *,
    repos_root: Path,
    configured: dict[str, Path],
    api_key: str | None = None,
    model: str | None = None,
    db_name: str = "p05.sqlite3",
) -> OrchestratorConfig:
    return OrchestratorConfig(
        database_path=tmp_path / "state" / db_name,
        workspace_root=tmp_path / "workspaces",
        allowed_repository_roots=[repos_root],
        configured_repositories=configured,
        trusted_execution_roots=[repos_root],
        groq_api_key=SecretStr(api_key) if api_key else None,
        groq_model=model,
        verification_timeout_seconds=30,
        environment="test",
    )


def _behavioral_stub(*, include_criterion_ids: bool = False, partial_mapping: bool = False):
    """Deterministic behavioral M03 stub for mini_pos with optional explicit criterion_ids."""
    header = (
        "import sys\n"
        "from pathlib import Path\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mini_pos'))\n"
    )

    def _stub(prompt: str) -> str:
        # Confirm A1 structured criteria were forwarded into M03 context
        assert "Structured Requirement Analysis (A1)" in prompt
        assert "AC-001" in prompt

        scn1: dict[str, Any] = {
            "title": "Premium checkout applies 10% discount",
            "rationale": "Verify AC-001 premium customer discount in billing.apply_discount.",
            "assumptions": "Deterministic offline behavioral fixture; premium gets 10% off.",
            "test_code": (
                header
                + "from billing import apply_discount, checkout\n\n"
                + "def test_ac001_premium_customer_discount():\n"
                + "    assert apply_discount(200.0, 'premium') == pytest.approx(180.0)\n"
                + "    assert checkout(50.0, 4, 'premium') == pytest.approx(180.0)\n"
            ),
        }
        scn2: dict[str, Any] = {
            "title": "Regular checkout remains unchanged",
            "rationale": "Verify AC-002 regular customers receive no discount.",
            "assumptions": "Deterministic offline behavioral fixture; regular pays full price.",
            "test_code": (
                header
                + "from billing import apply_discount, checkout\n\n"
                + "def test_ac002_regular_customer_unchanged():\n"
                + "    assert apply_discount(200.0, 'regular') == pytest.approx(200.0)\n"
                + "    assert checkout(50.0, 4, 'regular') == pytest.approx(200.0)\n"
            ),
        }
        scn3: dict[str, Any] = {
            "title": "Dependent refund, dashboard, and sales report modules preserved",
            "rationale": "Verify AC-003 dependent modules remain functional.",
            "assumptions": "Deterministic offline behavioral fixture across dependent modules.",
            "test_code": (
                header
                + "from dashboard import display_refund\n"
                + "from reports import generate_sales_report\n\n"
                + "def test_ac003_dependent_modules_preserved():\n"
                + "    assert display_refund(100.0, 'premium') == {'refund_amount': pytest.approx(90.0)}\n"
                + "    assert display_refund(100.0, 'regular') == {'refund_amount': pytest.approx(100.0)}\n"
                + "    assert generate_sales_report(100.0, 2, 'premium') == {'total_sales': pytest.approx(180.0)}\n"
                + "    assert generate_sales_report(100.0, 2, 'regular') == {'total_sales': pytest.approx(200.0)}\n"
            ),
        }
        if include_criterion_ids:
            scn1["criterion_ids"] = ["AC-001"]
            if not partial_mapping:
                scn2["criterion_ids"] = ["AC-002"]
                scn3["criterion_ids"] = ["AC-003"]
        return json.dumps({"scenarios": [scn1, scn2, scn3]})

    return _stub


def _mock_httpx_response(status_code: int) -> httpx.Response:
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return httpx.Response(status_code=status_code, request=req)


# ---------------------------------------------------------------------------
# 1, 2, 3: Valid Structured Requirement Analysis, Multiple Criteria, Explicit vs Inferred
# ---------------------------------------------------------------------------


def test_valid_premium_discount_decomposition_multiple_criteria_and_explicit_vs_inferred() -> None:
    agent = RequirementAnalystAgent()
    result = agent.analyze(
        PREMIUM_DISCOUNT_REQUIREMENT,
        execution_mode=ExecutionMode.OFFLINE_TEST,
    )

    assert result.status == "completed"
    assert result.analysis_mode == "offline_test"
    assert result.is_deterministic_fixture is True
    assert result.is_mock_output is True
    assert result.model_label == DEFAULT_OFFLINE_REQUIREMENT_LABEL
    assert result.has_mandatory_unresolved_ambiguities is False
    assert result.error is None

    # Multiple acceptance criteria extracted
    assert len(result.acceptance_criteria) == 4
    ids = [ac.criterion_id for ac in result.acceptance_criteria]
    assert ids == ["AC-001", "AC-002", "AC-003", "AC-004"]

    # Explicit vs inferred origin strictly separated
    explicit_criteria = [ac for ac in result.acceptance_criteria if ac.origin == CriterionOrigin.EXPLICIT]
    inferred_criteria = [ac for ac in result.acceptance_criteria if ac.origin == CriterionOrigin.INFERRED]
    assert [ac.criterion_id for ac in explicit_criteria] == ["AC-001", "AC-002", "AC-003"]
    assert all(ac.mandatory is True for ac in explicit_criteria)
    assert [ac.criterion_id for ac in inferred_criteria] == ["AC-004"]
    assert inferred_criteria[0].mandatory is False
    assert inferred_criteria[0].is_edge_case is True

    # Preserved existing behaviors & business constraints extracted
    assert len(result.business_constraints) >= 3
    assert any(bc.preserves_existing_behavior for bc in result.business_constraints)
    assert len(result.preserved_behaviors) >= 2
    assert len(result.edge_cases) >= 2

    # Assumptions clearly separated and marked for human confirmation
    assert len(result.assumptions) >= 2
    assert all(asm.requires_human_confirmation is True for asm in result.assumptions)

    # Non-blocking ambiguity surfaced for unspecified rounding/case sensitivity
    assert len(result.ambiguities) == 1
    assert result.ambiguities[0].severity == AmbiguitySeverity.NON_BLOCKING
    assert result.ambiguities[0].mandatory_to_resolve is False

    # Downstream formatting exposes structured criteria cleanly
    formatted = format_requirements_for_downstream(PREMIUM_DISCOUNT_REQUIREMENT, result)
    assert PREMIUM_DISCOUNT_REQUIREMENT in formatted
    assert "AC-001" in formatted and "AC-002" in formatted and "AC-003" in formatted
    assert "BC-001" in formatted and "ASM-001" in formatted

    # Lossless JSON roundtrip
    restored = RequirementAnalysisResult.model_validate_json(result.model_dump_json())
    assert restored == result


# ---------------------------------------------------------------------------
# 4: Ambiguous Requirements (Do Not Fabricate Missing Business Rules)
# ---------------------------------------------------------------------------


def test_ambiguous_requirement_flags_blocking_ambiguities_without_fabricating_rules() -> None:
    agent = RequirementAnalystAgent()
    ambiguous_req = (
        "Apply an appropriate discount to eligible customers and improve checkout somehow?"
    )
    result = agent.analyze(ambiguous_req, execution_mode=ExecutionMode.OFFLINE_TEST)

    assert result.status == "needs_clarification"
    assert result.has_mandatory_unresolved_ambiguities is True
    assert result.is_deterministic_fixture is True
    assert len(result.acceptance_criteria) >= 1

    blocking = [
        amb
        for amb in result.ambiguities
        if amb.severity == AmbiguitySeverity.BLOCKING and amb.mandatory_to_resolve
    ]
    assert len(blocking) >= 3
    assert all(not amb.resolved for amb in blocking)
    assert all(amb.clarification_question for amb in blocking)
    # Every explicit criterion is linked to the blocking ambiguities
    for ac in result.acceptance_criteria:
        if ac.origin == CriterionOrigin.EXPLICIT:
            assert set(ac.ambiguity_ids) == {amb.ambiguity_id for amb in blocking}


# ---------------------------------------------------------------------------
# 5, 6, 7: Schema Validation — Missing/Invalid Output, Duplicate IDs, Invalid References
# ---------------------------------------------------------------------------


def test_schema_rejects_duplicate_ids_empty_criteria_invalid_references_and_inconsistent_status() -> None:
    valid_ac = AcceptanceCriterion(
        criterion_id="AC-001",
        requirement_reference="Apply 10% discount",
        description="Premium gets 10% off",
        priority=CriterionPriority.MUST,
        mandatory=True,
        expected_behavior="Total reduced by 10%",
        verification_strategy=VerificationStrategy.UNIT_TEST,
        origin=CriterionOrigin.EXPLICIT,
    )

    # Empty criteria rejected for non-error status
    with pytest.raises(ValidationError, match="at least one acceptance criterion"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[],
        )

    # Duplicate criterion IDs rejected
    with pytest.raises(ValidationError, match="Duplicate acceptance criterion_id"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[valid_ac, valid_ac.model_copy()],
        )

    # Duplicate constraint IDs rejected
    bc = BusinessConstraint(
        constraint_id="BC-001",
        description="Only premium",
        source_reference="premium only",
        related_criterion_ids=["AC-001"],
    )
    with pytest.raises(ValidationError, match="Duplicate business constraint_id"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[valid_ac],
            business_constraints=[bc, bc.model_copy()],
        )

    # Invalid assumption reference from criterion rejected
    with pytest.raises(ValidationError, match="unknown assumption_ids"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[
                valid_ac.model_copy(update={"assumption_ids": ["ASM-999"]})
            ],
        )

    # Invalid ambiguity reference from criterion rejected
    with pytest.raises(ValidationError, match="unknown ambiguity_ids"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[
                valid_ac.model_copy(update={"ambiguity_ids": ["AMB-999"]})
            ],
        )

    # Invalid criterion reference from BusinessConstraint / Assumption / Ambiguity rejected
    with pytest.raises(ValidationError, match="unknown criterion_ids"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[valid_ac],
            business_constraints=[
                bc.model_copy(update={"related_criterion_ids": ["AC-999"]})
            ],
        )

    with pytest.raises(ValidationError, match="unknown criterion_ids"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[valid_ac],
            assumptions=[
                RequirementAssumption(
                    assumption_id="ASM-001",
                    description="Default regular",
                    rationale="Not stated",
                    related_criterion_ids=["AC-404"],
                )
            ],
        )

    # Mandatory unresolved ambiguity cannot have status='completed'
    blocking_amb = RequirementAmbiguity(
        ambiguity_id="AMB-001",
        description="Rate missing",
        clarification_question="What rate?",
        severity=AmbiguitySeverity.BLOCKING,
        mandatory_to_resolve=True,
        resolved=False,
        related_criterion_ids=["AC-001"],
    )
    with pytest.raises(ValidationError, match="status='completed'"):
        RequirementAnalysisResult(
            status="completed",
            original_requirement="Apply discount",
            acceptance_criteria=[valid_ac],
            ambiguities=[blocking_amb],
            has_mandatory_unresolved_ambiguities=True,
        )

    # RequirementTraceability refuses verified status without explicit mapping or with ambiguity
    with pytest.raises(ValidationError, match="explicit criterion-to-test mapping"):
        RequirementTraceability(
            criterion_id="AC-001",
            requirement_reference="Apply 10% discount",
            description="Premium discount",
            mapping_status="unavailable",
            verification_status=TraceabilityEvidenceStatus.VERIFIED,
        )

    with pytest.raises(ValidationError, match="unresolved ambiguities exist"):
        RequirementTraceability(
            criterion_id="AC-001",
            requirement_reference="Apply 10% discount",
            description="Premium discount",
            mapping_status="explicit",
            generated_scenario_ids=["SCN-001"],
            approved_test_ids=["SCN-001"],
            executed_test_ids=["SCN-001"],
            unresolved_ambiguity_ids=["AMB-001"],
            verification_status=TraceabilityEvidenceStatus.VERIFIED,
        )


@pytest.mark.parametrize(
    ("raw_output", "expected_code"),
    [
        (None, "INVALID_REQUIREMENT_MODEL_OUTPUT"),
        ("   ", "INVALID_REQUIREMENT_MODEL_OUTPUT"),
        ("{broken json", "INVALID_REQUIREMENT_MODEL_JSON"),
        (json.dumps(["not", "a", "dict"]), "INVALID_REQUIREMENT_MODEL_OUTPUT"),
        (
            json.dumps({"status": "completed", "acceptance_criteria": []}),
            "INVALID_REQUIREMENT_ANALYSIS_SCHEMA",
        ),
        (
            json.dumps(
                {
                    "status": "completed",
                    "acceptance_criteria": [
                        {
                            "criterion_id": "AC-001",
                            "requirement_reference": "ref",
                            "description": "first",
                            "expected_behavior": "beh 1",
                        },
                        {
                            "criterion_id": "AC-001",
                            "requirement_reference": "ref",
                            "description": "duplicate",
                            "expected_behavior": "beh 2",
                        },
                    ],
                }
            ),
            "INVALID_REQUIREMENT_ANALYSIS_SCHEMA",
        ),
        (
            json.dumps(
                {
                    "status": "completed",
                    "acceptance_criteria": [
                        {
                            "criterion_id": "AC-001",
                            "requirement_reference": "ref",
                            "description": "dangling assumption ref",
                            "expected_behavior": "beh",
                            "assumption_ids": ["ASM-MISSING"],
                        }
                    ],
                }
            ),
            "INVALID_REQUIREMENT_ANALYSIS_SCHEMA",
        ),
    ],
)
def test_agent_handles_missing_invalid_duplicate_and_dangling_model_outputs(
    raw_output: str | None,
    expected_code: str,
) -> None:
    agent = RequirementAnalystAgent(offline_analyzer_callable=lambda _p: raw_output)
    result = agent.analyze("Apply 10% discount.", execution_mode=ExecutionMode.OFFLINE_TEST)
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code == expected_code


def test_testing_adapter_rejects_invalid_criterion_id_reference_from_m03(tmp_path: Path) -> None:
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_ok"
    _create_mini_pos_fixture(repo, mode="correct")
    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos-ok": repo})

    cb = CodebaseAdapter(cfg).analyze(repo, source_path="mini_pos", base_ref="HEAD")
    req_analysis = RequirementAnalystAgent(cfg).analyze(PREMIUM_DISCOUNT_REQUIREMENT)

    def _bad_criterion_ref_stub(_prompt: str) -> str:
        return json.dumps(
            {
                "scenarios": [
                    {
                        "title": "Bad ref test",
                        "rationale": "References nonexistent AC-999",
                        "assumptions": "Test stub",
                        "criterion_ids": ["AC-999"],
                        "test_code": "def test_bad_ref():\n    assert True\n",
                    }
                ]
            }
        )

    res = TestingAdapter(cfg).generate_proposals(
        repo,
        cb,
        PREMIUM_DISCOUNT_REQUIREMENT,
        requirement_analysis=req_analysis,
        execution_mode=ExecutionMode.OFFLINE_TEST,
        model_callable=_bad_criterion_ref_stub,
    )
    assert res.status == "error"
    assert res.error is not None
    assert res.error.code == "INVALID_CRITERION_REFERENCE"


# ---------------------------------------------------------------------------
# 8 & 14: Live Provider Failure Without Mock Fallback & Secret Redaction
# ---------------------------------------------------------------------------


def test_live_provider_missing_credentials_and_errors_never_fall_back_and_redact_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)

    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_ok"
    _create_mini_pos_fixture(repo, mode="correct")

    # 8a. Missing credentials -> LIVE_LLM_UNCONFIGURED, zero fallback to deterministic fixture
    cfg_missing = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-ok": repo},
        api_key=None,
        model=None,
        db_name="live_missing.sqlite3",
    )
    app_missing = create_app(cfg_missing, offline_model_callable=_behavioral_stub())
    with TestClient(app_missing) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "execution_mode": ExecutionMode.LIVE_LLM.value,
            },
        ).json()["run_id"]
        app_missing.state.executor.wait_for_run(run_id, timeout=30.0)

        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["state"] == WorkflowState.FAILED.value
        assert detail["error"]["code"] == "LIVE_LLM_UNCONFIGURED"
        assert detail["requirement_analysis"]["status"] == "error"
        assert detail["requirement_analysis"]["analysis_mode"] == "live_llm"
        assert detail["requirement_analysis"]["is_deterministic_fixture"] is False
        assert detail["requirement_analysis"]["is_mock_output"] is False
        assert detail["requirement_analysis"]["acceptance_criteria"] == []
        # Stopped cleanly before M01 / M03
        assert detail["codebase_analysis"] is None
        assert detail["test_generation"] is None

    # 8b & 14. Controlled live Groq SDK exception with embedded secret -> no fallback, no retry, secret redacted
    synthetic_key = "gsk_" + "A1LiveFailureSecretKey1234567890ABCD"
    synthetic_bearer = "eyJhbGciOiJIUzI1NiJ9.A1SecretBearerToken999"
    call_count = 0

    class _FailingGroqClient:
        def __init__(self, *, api_key: str, timeout: int, max_retries: int) -> None:
            self.api_key = api_key
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **_kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            raise groq.AuthenticationError(
                f"Unauthorized key {synthetic_key} (Bearer {synthetic_bearer})",
                response=_mock_httpx_response(401),
                body={"error": {"message": f"api_key={synthetic_key}"}},
            )

    monkeypatch.setattr(groq, "Groq", _FailingGroqClient)

    cfg_auth_fail = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-ok": repo},
        api_key=synthetic_key,
        model="openai/gpt-oss-120b",
        db_name="live_auth_fail.sqlite3",
    )
    app_auth_fail = create_app(cfg_auth_fail, offline_model_callable=_behavioral_stub())
    with TestClient(app_auth_fail) as client:
        run_id = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
                "execution_mode": ExecutionMode.LIVE_LLM.value,
            },
        ).json()["run_id"]
        app_auth_fail.state.executor.wait_for_run(run_id, timeout=30.0)

        assert call_count == 1, "A1 must never silently retry live provider failures"
        res = client.get(f"/api/runs/{run_id}")
        logs_res = client.get(f"/api/runs/{run_id}/logs")

        assert synthetic_key not in res.text
        assert synthetic_bearer not in res.text
        assert synthetic_key not in logs_res.text
        assert synthetic_bearer not in logs_res.text

        body = res.json()
        assert body["state"] == WorkflowState.FAILED.value
        assert body["error"]["code"] == "A1_LIVE_LLM_ERROR"
        assert body["error"]["retryable"] is False
        assert body["requirement_analysis"]["is_deterministic_fixture"] is False
        assert "[REDACTED_GROQ_KEY]" in body["requirement_analysis"]["error"]["message"]
        assert "Bearer [REDACTED]" in body["requirement_analysis"]["error"]["message"]

    raw_db = cfg_auth_fail.database_path.read_bytes()
    assert synthetic_key.encode("utf-8") not in raw_db
    assert synthetic_bearer.encode("utf-8") not in raw_db


# ---------------------------------------------------------------------------
# 9, 10, 11: Persistence, Workflow Transitions, Restart Recovery & Backward Compatibility
# ---------------------------------------------------------------------------


def test_persistence_state_transitions_restart_recovery_and_backward_compatibility(
    tmp_path: Path,
) -> None:
    repos_root = tmp_path / "repos"
    repo = repos_root / "pos_ok"
    _create_mini_pos_fixture(repo, mode="correct")
    cfg = _make_config(tmp_path, repos_root=repos_root, configured={"pos-ok": repo})

    store = RunStore(cfg.database_path)
    svc = OrchestratorService(
        config=cfg,
        store=store,
        workspace_manager=WorkspaceManager(cfg),
        codebase_adapter=CodebaseAdapter(cfg),
        testing_adapter=TestingAdapter(cfg),
        verification_adapter=VerificationAdapter(cfg),
        offline_model_callable=_behavioral_stub(),
    )

    # 10a. Invalid skip CREATED -> ANALYZING_CODEBASE directly is forbidden
    created = svc.create_run(
        CreateRunRequest(
            repository_id="pos-ok",
            source_path="mini_pos",
            requirements=PREMIUM_DISCOUNT_REQUIREMENT,
        )
    )
    with pytest.raises(InvalidStateTransitionError):
        svc.transition_state(
            created.run_id,
            WorkflowState.CREATED,
            WorkflowState.ANALYZING_CODEBASE,
        )

    # 9 & 10b. Run full stage 0..3 -> transitions through ANALYZING_REQUIREMENTS & persists
    paused = svc.execute_analysis_and_generation(created.run_id)
    assert paused.state == WorkflowState.AWAITING_TEST_REVIEW
    assert paused.requirement_analysis is not None
    assert len(paused.requirement_analysis.acceptance_criteria) == 4

    # Reload via fresh RunStore instance to prove SQLite persistence
    reloaded = RunStore(cfg.database_path).get_run(created.run_id)
    assert reloaded is not None
    assert reloaded.requirement_analysis is not None
    assert [c.criterion_id for c in reloaded.requirement_analysis.acceptance_criteria] == [
        "AC-001",
        "AC-002",
        "AC-003",
        "AC-004",
    ]

    logs = svc.get_run_logs(created.run_id)
    stages = [entry.stage for entry in logs]
    assert WorkflowState.ANALYZING_REQUIREMENTS in stages
    assert stages.index(WorkflowState.ANALYZING_REQUIREMENTS) < stages.index(
        WorkflowState.ANALYZING_CODEBASE
    )
    assert any(entry.agent_name == "RequirementAnalystAgent" for entry in logs)

    # 10c. Restart recovery marks interrupted ANALYZING_REQUIREMENTS run as FAILED
    store.save_run(
        WorkflowRunRecord(
            run_id="run_interrupted_a1",
            state=WorkflowState.ANALYZING_REQUIREMENTS,
            requirements="Interrupted during A1",
            repository=RepositoryInfo(
                repository_id="pos-ok",
                repository_path=str(repo),
                source_path="mini_pos",
                base_ref="HEAD",
            ),
        )
    )
    recovered = svc.recover_interrupted_runs()
    assert [r.run_id for r in recovered] == ["run_interrupted_a1"]
    assert recovered[0].state == WorkflowState.FAILED
    assert recovered[0].error is not None
    assert recovered[0].error.code == "PROCESS_INTERRUPTED"
    assert svc.get_run(created.run_id).state == WorkflowState.AWAITING_TEST_REVIEW

    # 11. Backward compatibility: legacy pre-P0-5 persisted run JSON (without requirement_analysis)
    legacy_payload = {
        "run_id": "run_legacy_p04_001",
        "state": "APPROVED",
        "requirements": "Legacy run before A1 existed.",
        "execution_mode": "offline_test",
        "repository": {
            "repository_id": "pos-ok",
            "repository_path": str(repo),
            "source_path": "mini_pos",
            "base_ref": "HEAD",
            "change_mode": "working_tree",
        },
        "report": {
            "package_id": "pkg_legacy_001",
            "run_id": "run_legacy_p04_001",
            "requirements": "Legacy run before A1 existed.",
            "repository_id": "pos-ok",
            "repository_path": str(repo),
            "source_path": "mini_pos",
            "base_ref": "HEAD",
            "recommendation": "needs_review",
            "recommendation_label": "ready_for_human_review",
            "traceability": {
                "requirement_traceability_status": "incomplete",
                "structured_acceptance_criteria_status": "unavailable",
                "requirement_verification_proven": False,
            },
        },
    }
    legacy_record = WorkflowRunRecord.model_validate_json(json.dumps(legacy_payload))
    assert legacy_record.requirement_analysis is None
    assert legacy_record.report is not None
    assert legacy_record.report.criterion_traceability == []
    store.save_run(legacy_record)

    app = create_app(cfg, offline_model_callable=_behavioral_stub())
    with TestClient(app) as client:
        legacy_res = client.get("/api/runs/run_legacy_p04_001")
        assert legacy_res.status_code == 200
        legacy_body = legacy_res.json()
        assert legacy_body["requirement_analysis"] is None
        assert legacy_body["report"]["criterion_traceability"] == []
        list_res = client.get("/api/runs")
        assert list_res.status_code == 200
        assert any(r["run_id"] == "run_legacy_p04_001" for r in list_res.json()["runs"])


# ---------------------------------------------------------------------------
# 12, 13, 15 & Offline Premium-Customer Discount Demonstration
# ---------------------------------------------------------------------------


def test_offline_premium_discount_demo_unmapped_explicit_partial_regressive_and_ambiguous_gates(
    tmp_path: Path,
) -> None:
    """Deterministic offline demonstration covering traceability & all gates on mini_pos."""
    repos_root = tmp_path / "repos"
    repo_ok = repos_root / "pos_correct"
    repo_bad = repos_root / "pos_regressive"
    base_ok = _create_mini_pos_fixture(repo_ok, mode="correct")
    base_bad = _create_mini_pos_fixture(repo_bad, mode="regressive")
    orig_ok_status = _git(repo_ok, "status", "--porcelain")
    orig_bad_status = _git(repo_bad, "status", "--porcelain")

    # ---- Case A: Unmodified M03 (no criterion_ids) -> NEVER fabricates mappings from test names ----
    cfg_unmapped = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-ok": repo_ok},
        db_name="case_unmapped.sqlite3",
    )
    app_unmapped = create_app(
        cfg_unmapped,
        offline_model_callable=_behavioral_stub(include_criterion_ids=False),
    )
    with TestClient(app_unmapped) as client:
        rid = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app_unmapped.state.executor.wait_for_run(rid, timeout=30.0)

        gate1 = client.get(f"/api/runs/{rid}").json()
        assert gate1["state"] == "AWAITING_TEST_REVIEW"
        assert gate1["requirement_analysis"]["status"] == "completed"
        assert gate1["requirement_analysis"]["is_deterministic_fixture"] is True
        assert gate1["verification"] is None

        rev = client.post(
            f"/api/runs/{rid}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
            },
        )
        assert rev.status_code == 202
        app_unmapped.state.executor.wait_for_run(rid, timeout=30.0)

        gate2 = client.get(f"/api/runs/{rid}").json()
        assert gate2["verification"]["verification_passed"] is True
        rep = gate2["report"]
        # Passing tests alone MUST NOT infer criteria are verified!
        assert rep["traceability"]["structured_acceptance_criteria_status"] == "available"
        assert rep["traceability"]["criterion_mapping_status"] == "unavailable"
        assert rep["traceability"]["verified_criterion_ids"] == []
        assert rep["traceability"]["incomplete_mandatory_criterion_ids"] == [
            "AC-001",
            "AC-002",
            "AC-003",
        ]
        assert rep["traceability"]["requirement_verification_proven"] is False
        by_cid = {item["criterion_id"]: item for item in rep["criterion_traceability"]}
        assert by_cid["AC-001"]["mapping_status"] == "unavailable"
        assert by_cid["AC-001"]["verification_status"] == "unverified"
        assert by_cid["AC-004"]["verification_status"] == "needs_clarification"

    # ---- Case B: Explicit criterion_ids (full coverage vs partial mandatory coverage) ----
    cfg_mapped = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-ok": repo_ok, "pos-bad": repo_bad},
        db_name="case_mapped.sqlite3",
    )
    app_mapped = create_app(
        cfg_mapped,
        offline_model_callable=_behavioral_stub(include_criterion_ids=True, partial_mapping=False),
    )
    with TestClient(app_mapped) as client:
        # B1: All 3 mandatory criteria mapped, approved, executed, passing -> AC-001..AC-003 verified
        rid_full = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app_mapped.state.executor.wait_for_run(rid_full, timeout=30.0)
        client.post(
            f"/api/runs/{rid_full}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
            },
        )
        app_mapped.state.executor.wait_for_run(rid_full, timeout=30.0)
        rep_full = client.get(f"/api/runs/{rid_full}").json()["report"]
        assert rep_full["traceability"]["verified_criterion_ids"] == [
            "AC-001",
            "AC-002",
            "AC-003",
        ]
        assert rep_full["traceability"]["incomplete_mandatory_criterion_ids"] == []
        assert rep_full["traceability"]["mandatory_criteria_coverage_complete"] is True
        assert rep_full["traceability"]["needs_clarification_criterion_ids"] == ["AC-004"]
        # Still False because offline fixture & baseline regression unavailable!
        assert rep_full["traceability"]["requirement_verification_proven"] is False

        # B2 (13): Incomplete mandatory coverage — approve ONLY SCN-001 (AC-002, AC-003 uncovered!)
        rid_partial = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app_mapped.state.executor.wait_for_run(rid_partial, timeout=30.0)
        client.post(
            f"/api/runs/{rid_partial}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001"],
                "reviewer": "marsadh",
            },
        )
        app_mapped.state.executor.wait_for_run(rid_partial, timeout=30.0)
        rep_partial = client.get(f"/api/runs/{rid_partial}").json()["report"]
        assert rep_partial["traceability"]["verified_criterion_ids"] == ["AC-001"]
        assert rep_partial["traceability"]["unverified_criterion_ids"] == [
            "AC-002",
            "AC-003",
        ]
        assert rep_partial["traceability"]["incomplete_mandatory_criterion_ids"] == [
            "AC-002",
            "AC-003",
        ]
        assert rep_partial["traceability"]["mandatory_criteria_coverage_complete"] is False
        assert rep_partial["traceability"]["requirement_traceability_status"] == "incomplete"

        # B3: Regressive fixture -> AC-002 & AC-003 marked FAILED, Gate 2 approve blocked (409)
        rid_bad = client.post(
            "/api/runs",
            json={
                "repository_id": "pos-bad",
                "source_path": "mini_pos",
                "requirements": PREMIUM_DISCOUNT_REQUIREMENT,
            },
        ).json()["run_id"]
        app_mapped.state.executor.wait_for_run(rid_bad, timeout=30.0)
        client.post(
            f"/api/runs/{rid_bad}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001", "SCN-002", "SCN-003"],
                "reviewer": "marsadh",
            },
        )
        app_mapped.state.executor.wait_for_run(rid_bad, timeout=30.0)
        gate2_bad = client.get(f"/api/runs/{rid_bad}").json()
        assert gate2_bad["verification"]["verification_outcome"] == VerificationOutcome.FAILED_TESTS.value
        assert "AC-002" in gate2_bad["report"]["traceability"]["failed_criterion_ids"]
        assert "AC-003" in gate2_bad["report"]["traceability"]["failed_criterion_ids"]
        assert (
            client.post(
                f"/api/runs/{rid_bad}/decision",
                json={"decision": "approve", "reviewer": "marsadh", "comments": "try"},
            ).status_code
            == 409
        )

        # ---- Case C (4 & 15): Ambiguous requirement -> passing tests STILL block Gate 2 approve! ----
    cfg_amb = _make_config(
        tmp_path,
        repos_root=repos_root,
        configured={"pos-ok": repo_ok},
        db_name="case_ambiguous.sqlite3",
    )
    app_amb = create_app(
        cfg_amb,
        offline_model_callable=_behavioral_stub(include_criterion_ids=True, partial_mapping=True),
    )
    with TestClient(app_amb) as client_amb:
        rid_amb = client_amb.post(
            "/api/runs",
            json={
                "repository_id": "pos-ok",
                "source_path": "mini_pos",
                "requirements": "Apply an appropriate discount to eligible customers somehow.",
            },
        ).json()["run_id"]
        app_amb.state.executor.wait_for_run(rid_amb, timeout=30.0)
        gate1_amb = client_amb.get(f"/api/runs/{rid_amb}").json()
        assert gate1_amb["state"] == "AWAITING_TEST_REVIEW"
        assert gate1_amb["requirement_analysis"]["status"] == "needs_clarification"
        assert gate1_amb["requirement_analysis"]["has_mandatory_unresolved_ambiguities"] is True

        rev_amb = client_amb.post(
            f"/api/runs/{rid_amb}/test-review",
            json={
                "approved": True,
                "selected_test_ids": ["SCN-001"],
                "reviewer": "marsadh",
            },
        )
        assert rev_amb.status_code == 202
        app_amb.state.executor.wait_for_run(rid_amb, timeout=30.0)
        gate2_amb = client_amb.get(f"/api/runs/{rid_amb}").json()
        assert gate2_amb["state"] == "AWAITING_FINAL_APPROVAL"
        assert gate2_amb["verification"]["verification_passed"] is True
        # Even though tests passed, blocking ambiguity prevents verified status and blocks APPROVE!
        assert gate2_amb["report"]["recommendation_label"] == "additional_review_required"
        assert gate2_amb["report"]["traceability"]["verified_criterion_ids"] == []
        assert "AC-001" in gate2_amb["report"]["traceability"]["needs_clarification_criterion_ids"]
        assert "submit_final_decision:approve" not in gate2_amb["allowed_actions"]

        blocked_amb = client_amb.post(
            f"/api/runs/{rid_amb}/decision",
            json={"decision": "approve", "reviewer": "marsadh", "comments": "override"},
        )
        assert blocked_amb.status_code == 409
        assert blocked_amb.json()["code"] == "UNRESOLVED_REQUIREMENT_AMBIGUITY"

        escalated_amb = client_amb.post(
            f"/api/runs/{rid_amb}/decision",
            json={
                "decision": "needs_review",
                "reviewer": "marsadh",
                "comments": "Needs clarification on discount percentage and customer eligibility.",
            },
        )
        assert escalated_amb.status_code == 200
        assert escalated_amb.json()["state"] == "NEEDS_REVIEW"

    # Original repositories untouched
    assert _git(repo_ok, "rev-parse", "HEAD") == base_ok
    assert _git(repo_ok, "status", "--porcelain") == orig_ok_status
    assert _git(repo_bad, "rev-parse", "HEAD") == base_bad
    assert _git(repo_bad, "status", "--porcelain") == orig_bad_status
