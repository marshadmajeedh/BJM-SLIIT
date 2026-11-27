"""FastAPI REST API layer for ChangeGuard AI (Module 02)."""

from .executor import BoundedRunExecutor
from .routes import router
from .schemas import (
    ChangePackageResponse,
    CodebaseAnalysisResponse,
    CreateRunResponse,
    ErrorResponse,
    HealthResponse,
    RepositorySummaryResponse,
    RunDetailResponse,
    RunListResponse,
    RunLogsResponse,
    RunSummaryResponse,
    TestGenerationResponse,
    VerificationResponse,
    to_run_detail,
    to_run_summary,
)

__all__ = [
    "BoundedRunExecutor",
    "ChangePackageResponse",
    "CodebaseAnalysisResponse",
    "CreateRunResponse",
    "ErrorResponse",
    "HealthResponse",
    "RepositorySummaryResponse",
    "RunDetailResponse",
    "RunListResponse",
    "RunLogsResponse",
    "RunSummaryResponse",
    "TestGenerationResponse",
    "VerificationResponse",
    "router",
    "to_run_detail",
    "to_run_summary",
]
