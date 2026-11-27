"""Canonical FastAPI REST routes for ChangeGuard AI (Module 02).

Canonical endpoints (matching docs/m02/M02_FOUNDATION.md handoff):
- GET  /health
- POST /api/runs
- GET  /api/runs
- GET  /api/runs/{run_id}
- GET  /api/runs/{run_id}/logs
- POST /api/runs/{run_id}/test-review
- POST /api/runs/{run_id}/decision
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil

from fastapi import APIRouter, Depends, Query, Request, Response, status

from project.api.executor import BoundedRunExecutor
from project.api.schemas import (
    CreateRunResponse,
    ErrorResponse,
    HealthResponse,
    RunDetailResponse,
    RunListResponse,
    RunLogsResponse,
    to_run_detail,
    to_run_summary,
)
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.orchestrator import (
    OrchestratorService,
    ReviewConflictError,
)
from project.schemas import (
    CreateRunRequest,
    HumanDecisionRequest,
    TestReviewRequest,
    WorkflowState,
)

API_VERSION = "0.2.0-mvp"

router = APIRouter()


def get_config(request: Request) -> OrchestratorConfig:
    return request.app.state.config


def get_orchestrator(request: Request) -> OrchestratorService:
    return request.app.state.orchestrator


def get_executor(request: Request) -> BoundedRunExecutor:
    return request.app.state.executor


def _verification_deps_available() -> bool:
    return (
        importlib.util.find_spec("pytest") is not None
        and importlib.util.find_spec("pytest_jsonreport") is not None
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Service health and capability status",
)
async def health_check(
    config: OrchestratorConfig = Depends(get_config),
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> HealthResponse:
    """Return safe service health and runtime capability flags without exposing secrets."""
    db_ok = await asyncio.to_thread(orchestrator.store.check_health)
    git_ok = shutil.which("git") is not None
    ver_deps_ok = _verification_deps_available()
    live_configured = bool(config.get_groq_api_key() and config.groq_model)
    patch_gen_configured = bool(orchestrator.patch_adapter.is_production_configured)
    reviewer_configured = bool(orchestrator.review_adapter.is_production_configured)

    overall = "ok" if (db_ok and git_ok) else "degraded"
    limitations = [
        "Background workflow stages execute in a single-process bounded thread pool (not a durable distributed queue).",
        "Untrusted repository execution is disabled; pytest runs only inside server-trusted roots.",
        "Git workspace isolation is not an operating-system security sandbox.",
        "Local API endpoints are unauthenticated in this MVP; reviewer labels are local metadata and do not represent authenticated identities.",
    ]
    if not live_configured:
        limitations.append(
            "Live LLM mode is unconfigured (GROQ_API_KEY and GROQ_MODEL required); offline_test mode remains available."
        )
    if not patch_gen_configured:
        limitations.append(
            "Production A4 patch generation agent is not configured; generate_and_verify_change requires an injected PatchGeneratorProvider."
        )
    if not reviewer_configured:
        limitations.append(
            "Production A6 independent reviewer agent is not configured; independent review reports 'unavailable' unless an IndependentReviewerProvider is injected."
        )

    return HealthResponse(
        status=overall,
        version=API_VERSION,
        environment=config.environment,
        database_available=db_ok,
        live_llm_configured=live_configured,
        groq_model=config.groq_model if live_configured else None,
        patch_generator_configured=patch_gen_configured,
        independent_reviewer_configured=reviewer_configured,
        require_independent_review=config.require_independent_review,
        auto_repair_enabled=config.auto_repair_enabled,
        max_repair_attempts=config.max_repair_attempts,
        verification_dependencies_available=ver_deps_ok,
        git_available=git_ok,
        configured_repository_ids=sorted(config.configured_repositories.keys()),
        limitations=limitations,
    )


@router.post(
    "/api/runs",
    response_model=CreateRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Workflow Runs"],
    summary="Create and schedule a new change assurance workflow run",
    responses={
        403: {"model": ErrorResponse, "description": "Repository path or ID is not permitted"},
        422: {"model": ErrorResponse, "description": "Invalid request payload"},
        503: {"model": ErrorResponse, "description": "Required infrastructure unavailable"},
    },
)
async def create_workflow_run(
    payload: CreateRunRequest,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
    executor: BoundedRunExecutor = Depends(get_executor),
) -> CreateRunResponse:
    """Persist a new workflow run and schedule M01 + M03 analysis in-process."""
    run = await asyncio.to_thread(orchestrator.create_run, payload)
    executor.submit(
        run.run_id,
        orchestrator.execute_analysis_and_generation,
        run.run_id,
    )
    return CreateRunResponse(
        run_id=run.run_id,
        state=run.state,
        workflow_mode=run.workflow_mode,
        execution_mode=run.execution_mode,
        created_at=run.created_at,
        message=(
            "Workflow run created and scheduled on the in-process background executor. "
            "Poll GET /api/runs/{run_id} for progress until AWAITING_TEST_REVIEW "
            "(or COMPLETED_NO_CHANGES / FAILED)."
        ),
    )


@router.get(
    "/api/runs",
    response_model=RunListResponse,
    tags=["Workflow Runs"],
    summary="List persisted workflow runs with bounded pagination",
)
async def list_workflow_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    state: WorkflowState | None = Query(default=None),
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> RunListResponse:
    """Return paginated workflow run summaries ordered newest-first."""
    runs = await asyncio.to_thread(
        orchestrator.list_runs, limit=limit, offset=offset, state=state
    )
    total = await asyncio.to_thread(orchestrator.count_runs, state=state)
    return RunListResponse(
        runs=[to_run_summary(r) for r in runs],
        total=total,
        limit=limit,
        offset=offset,
        state_filter=state,
    )


@router.get(
    "/api/runs/{run_id}",
    response_model=RunDetailResponse,
    tags=["Workflow Runs"],
    summary="Retrieve full details for a single workflow run",
    responses={
        404: {"model": ErrorResponse, "description": "Workflow run not found"},
    },
)
async def get_workflow_run(
    run_id: str,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> RunDetailResponse:
    """Return dashboard-ready workflow details, evidence, and allowed actions."""
    run = await asyncio.to_thread(orchestrator.get_run, run_id)
    return to_run_detail(run)


@router.get(
    "/api/runs/{run_id}/logs",
    response_model=RunLogsResponse,
    tags=["Workflow Runs"],
    summary="Retrieve structured agent communication logs for a run",
    responses={
        404: {"model": ErrorResponse, "description": "Workflow run not found"},
    },
)
async def get_workflow_run_logs(
    run_id: str,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> RunLogsResponse:
    """Return secret-redacted communication logs in deterministic insertion order."""
    logs = await asyncio.to_thread(orchestrator.get_run_logs, run_id)
    return RunLogsResponse(
        run_id=run_id,
        count=len(logs),
        logs=logs,
    )


@router.post(
    "/api/runs/{run_id}/test-review",
    response_model=RunDetailResponse,
    tags=["Human Review Gates"],
    summary="Submit Human Review Gate 1 decision for proposed test scenarios",
    responses={
        200: {"description": "Generated test proposals rejected (terminal REJECTED state)"},
        202: {"description": "Selected test scenarios approved; verification scheduled"},
        404: {"model": ErrorResponse, "description": "Workflow run not found"},
        409: {"model": ErrorResponse, "description": "Invalid state, duplicate review, or integrity conflict"},
        422: {"model": ErrorResponse, "description": "Invalid scenario selection or request payload"},
    },
)
async def submit_test_review(
    run_id: str,
    payload: TestReviewRequest,
    response: Response,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
    executor: BoundedRunExecutor = Depends(get_executor),
) -> RunDetailResponse:
    """Approve selected generated tests (scheduling M04 verification) or reject them."""
    updated, approved_specs = await asyncio.to_thread(
        orchestrator.accept_test_review,
        run_id,
        payload,
    )
    if (
        payload.approved
        and approved_specs is not None
        and updated.state
        in {WorkflowState.PREPARING_BASELINE, WorkflowState.RUNNING_VERIFICATION}
    ):
        try:
            executor.submit(
                run_id,
                orchestrator.execute_verification_and_report,
                run_id,
                approved_specs,
            )
        except RuntimeError as exc:
            raise ReviewConflictError(
                str(exc),
                code="CONCURRENT_EXECUTION_CONFLICT",
                run_id=run_id,
            ) from exc
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        response.status_code = status.HTTP_200_OK
    return to_run_detail(updated)


@router.post(
    "/api/runs/{run_id}/decision",
    response_model=RunDetailResponse,
    status_code=status.HTTP_200_OK,
    tags=["Human Review Gates"],
    summary="Submit Human Review Gate 2 final decision on compiled change package",
    responses={
        404: {"model": ErrorResponse, "description": "Workflow run not found"},
        409: {"model": ErrorResponse, "description": "Invalid state, repeated decision, or blocked approval"},
        422: {"model": ErrorResponse, "description": "Invalid decision payload"},
    },
)
async def submit_final_decision(
    run_id: str,
    payload: HumanDecisionRequest,
    orchestrator: OrchestratorService = Depends(get_orchestrator),
) -> RunDetailResponse:
    """Persist final human decision (approve / reject / needs_review) without mutating Git."""
    updated = await asyncio.to_thread(
        orchestrator.submit_final_decision,
        run_id,
        payload,
    )
    return to_run_detail(updated)
