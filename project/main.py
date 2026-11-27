"""FastAPI application entrypoint for ChangeGuard AI (Module 02 Backend).

Startup command (loopback-only local development):
    python -m uvicorn project.main:app --host 127.0.0.1 --port 8000

Security & hygiene invariants:
- No external LLM SDK or live model client is imported or initialized at import time.
- CORS is restricted to configured local origins without wildcard credentials.
- Unhandled exceptions return safe structured JSON without stack traces or secrets.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from project.api.executor import BoundedRunExecutor
from project.api.routes import API_VERSION, router
from project.api.schemas import ErrorResponse
from project.marsadh_orchestrator.adapters import (
    CodebaseAdapter,
    PatchGenerationAdapter,
    TestingAdapter,
    VerificationAdapter,
)
from project.marsadh_orchestrator.adapters.patch_adapter import InjectablePatchProvider
from project.marsadh_orchestrator.config import OrchestratorConfig
from project.marsadh_orchestrator.patch_engine import PatchApplicator, PatchValidator
from project.marsadh_orchestrator.requirement_analyst import (
    DEFAULT_OFFLINE_REQUIREMENT_LABEL,
    RequirementAnalystAgent,
)
from project.marsadh_orchestrator.orchestrator import (
    InfrastructureUnavailableError,
    InvalidStateTransitionError,
    OrchestratorError,
    OrchestratorService,
    RepositoryAccessDeniedError,
    RunNotFoundError,
    ValidationInputError,
)
from project.marsadh_orchestrator.store import (
    RunStore,
    redact_nested_secrets,
    redact_secrets,
)
from project.marsadh_orchestrator.workspace import WorkspaceManager

logger = logging.getLogger("changeguard.api")


def _error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    details: dict | None = None,
    run_id: str | None = None,
) -> JSONResponse:
    sanitized_details = redact_nested_secrets(details or {})
    payload = ErrorResponse(
        code=code,
        message=redact_secrets(message),
        details=sanitized_details if isinstance(sanitized_details, dict) else {},
        run_id=run_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(mode="json"),
    )


def create_app(
    config: OrchestratorConfig | None = None,
    *,
    orchestrator: OrchestratorService | None = None,
    requirement_analyst: RequirementAnalystAgent | None = None,
    patch_adapter: PatchGenerationAdapter | None = None,
    patch_production_provider: InjectablePatchProvider | None = None,
    patch_offline_fixture_provider: InjectablePatchProvider | None = None,
    patch_validator: PatchValidator | None = None,
    patch_applicator: PatchApplicator | None = None,
    requirement_offline_callable: Callable[[str], str] | None = None,
    requirement_offline_label: str = DEFAULT_OFFLINE_REQUIREMENT_LABEL,
    requirement_live_callable: Callable[[str], str] | None = None,
    requirement_live_label: str | None = None,
    offline_model_callable: Callable[[str], str] | None = None,
    offline_model_label: str = "deterministic_offline_stub",
    live_model_callable: Callable[[str], str] | None = None,
    live_model_label: str | None = None,
    use_default_offline_stub: bool = True,
) -> FastAPI:
    """Create and configure the ChangeGuard AI FastAPI application."""
    resolved_config = config or OrchestratorConfig.from_env()
    if orchestrator is None:
        store = RunStore(resolved_config.database_path)
        workspace_manager = WorkspaceManager(resolved_config)
        codebase_adapter = CodebaseAdapter(resolved_config)
        testing_adapter = TestingAdapter(resolved_config)
        verification_adapter = VerificationAdapter(resolved_config)
        resolved_patch_adapter = patch_adapter or PatchGenerationAdapter(
            config=resolved_config,
            production_provider=patch_production_provider,
            offline_fixture_provider=patch_offline_fixture_provider,
        )
        resolved_orchestrator = OrchestratorService(
            config=resolved_config,
            store=store,
            workspace_manager=workspace_manager,
            codebase_adapter=codebase_adapter,
            testing_adapter=testing_adapter,
            verification_adapter=verification_adapter,
            requirement_analyst=requirement_analyst,
            patch_adapter=resolved_patch_adapter,
            patch_validator=patch_validator,
            patch_applicator=patch_applicator,
            requirement_offline_callable=requirement_offline_callable,
            requirement_offline_label=requirement_offline_label,
            requirement_live_callable=requirement_live_callable,
            requirement_live_label=requirement_live_label,
            offline_model_callable=offline_model_callable,
            offline_model_label=offline_model_label,
            live_model_callable=live_model_callable,
            live_model_label=live_model_label,
            use_default_offline_stub=use_default_offline_stub,
        )
    else:
        resolved_orchestrator = orchestrator

    executor = BoundedRunExecutor(max_workers=resolved_config.max_concurrent_runs)

    @asynccontextmanager
    async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.orchestrator.store.initialize()
        recovered = application.state.orchestrator.recover_interrupted_runs()
        if recovered:
            logger.warning(
                "Recovered %d interrupted workflow run(s) on startup.",
                len(recovered),
            )
        try:
            yield
        finally:
            application.state.executor.shutdown(wait=True)

    application = FastAPI(
        title="ChangeGuard AI API",
        description="Multi-agent software change assurance framework",
        version=API_VERSION,
        lifespan=_lifespan,
    )

    application.state.config = resolved_config
    application.state.orchestrator = resolved_orchestrator
    application.state.executor = executor

    # Restrict CORS to configured local frontend origins; never allow wildcard with credentials.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(resolved_config.cors_allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    # Register safe, structured exception handlers
    @application.exception_handler(RunNotFoundError)
    async def _handle_not_found(_request: Request, exc: RunNotFoundError) -> JSONResponse:
        return _error_response(
            status.HTTP_404_NOT_FOUND,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            run_id=exc.run_id,
        )

    @application.exception_handler(RepositoryAccessDeniedError)
    async def _handle_forbidden(
        _request: Request, exc: RepositoryAccessDeniedError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_403_FORBIDDEN,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            run_id=exc.run_id,
        )

    @application.exception_handler(InvalidStateTransitionError)
    async def _handle_conflict(
        _request: Request, exc: InvalidStateTransitionError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_409_CONFLICT,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            run_id=exc.run_id,
        )

    @application.exception_handler(ValidationInputError)
    async def _handle_validation_input(
        _request: Request, exc: ValidationInputError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            run_id=exc.run_id,
        )

    @application.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        safe_errors = [
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "msg": redact_secrets(str(err.get("msg", "Invalid input"))),
                "type": str(err.get("type", "validation_error")),
            }
            for err in exc.errors()
        ]
        return _error_response(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="VALIDATION_ERROR",
            message="Request validation failed.",
            details={"errors": safe_errors},
        )

    @application.exception_handler(InfrastructureUnavailableError)
    async def _handle_unavailable(
        _request: Request, exc: InfrastructureUnavailableError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            run_id=exc.run_id,
        )

    @application.exception_handler(OrchestratorError)
    async def _handle_orchestrator_error(
        _request: Request, exc: OrchestratorError
    ) -> JSONResponse:
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            code=exc.code,
            message=exc.message,
            details=exc.details,
            run_id=exc.run_id,
        )

    @application.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled server error: %s", type(exc).__name__)
        return _error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected internal server error occurred.",
        )

    application.include_router(router)
    return application


app = create_app()
