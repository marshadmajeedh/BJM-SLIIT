"""Configuration management for ChangeGuard AI Module 02 Orchestrator.

Security & hygiene invariants:
- No external AI client or paid service is imported or initialized at import time.
- API keys are never hardcoded and are stored as ``SecretStr`` with serialization
  excluded so they cannot leak into JSON dumps or logs.
- Untrusted repository execution remains disabled (``allow_untrusted_execution=False``);
  test execution requires explicit ``trusted_execution_roots`` for local fixtures.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from project.schemas.workflow import ExecutionMode


class OrchestratorConfig(BaseModel):
    """Validated runtime configuration for M02 foundation components."""

    model_config = ConfigDict(extra="forbid")

    database_path: Path = Field(
        default=Path(".changeguard") / "changeguard.sqlite3"
    )
    workspace_root: Path = Field(default=Path(".changeguard") / "workspaces")
    allowed_repository_roots: list[Path] = Field(default_factory=list)
    configured_repositories: dict[str, Path] = Field(default_factory=dict)
    trusted_execution_roots: list[Path] = Field(default_factory=list)
    verification_timeout_seconds: int = Field(default=60, ge=1, le=600)
    git_timeout_seconds: int = Field(default=30, ge=1, le=120)
    execution_mode: ExecutionMode = ExecutionMode.OFFLINE_TEST
    groq_model: str | None = Field(default=None, max_length=160)
    groq_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    max_repair_attempts: int = Field(default=2, ge=0, le=2)
    max_patch_size_bytes: int = Field(default=100_000, ge=1024, le=1_000_000)
    max_patch_changed_files: int = Field(default=10, ge=1, le=50)
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    app_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    app_port: int = Field(default=8000, ge=1, le=65535)
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    environment: Literal["development", "test", "production"] = "development"
    allow_untrusted_execution: Literal[False] = False

    @field_validator("cors_allowed_origins")
    @classmethod
    def _validate_cors_origins(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for origin in value:
            item = origin.strip().rstrip("/")
            if not item:
                continue
            if item == "*":
                raise ValueError(
                    "Wildcard '*' is not allowed in cors_allowed_origins; "
                    "specify explicit local origins"
                )
            if item not in cleaned:
                cleaned.append(item)
        return cleaned

    @field_validator("groq_model", "app_host")
    @classmethod
    def _strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("app_host")
    @classmethod
    def _require_app_host(cls, value: str | None) -> str:
        if not value:
            raise ValueError("app_host must not be blank")
        return value

    @model_validator(mode="after")
    def _validate_repository_registry(self) -> OrchestratorConfig:
        resolved_allowed = [root.resolve() for root in self.allowed_repository_roots]
        if resolved_allowed:
            for repo_id, repo_path in self.configured_repositories.items():
                resolved_repo = repo_path.resolve()
                if not any(
                    resolved_repo.is_relative_to(allowed)
                    for allowed in resolved_allowed
                ):
                    raise ValueError(
                        f"Configured repository '{repo_id}' ({repo_path}) is outside "
                        "allowed_repository_roots"
                    )
        return self

    def is_repository_allowed(self, repository_path: str | Path) -> bool:
        """Return True iff the resolved repository path sits inside an allowed root."""
        if not self.allowed_repository_roots:
            return False
        resolved_repo = Path(repository_path).resolve()
        return any(
            resolved_repo.is_relative_to(root.resolve())
            for root in self.allowed_repository_roots
        )

    def is_trusted_for_execution(self, path: str | Path) -> bool:
        """Return True iff the resolved path sits inside an explicitly trusted root."""
        if not self.trusted_execution_roots:
            return False
        resolved_target = Path(path).resolve()
        return any(
            resolved_target.is_relative_to(root.resolve())
            for root in self.trusted_execution_roots
        )

    def resolve_repository(
        self,
        repository_path: str | None = None,
        repository_id: str | None = None,
    ) -> Path:
        """Resolve a repository path or configured identifier and enforce allowlist."""
        if repository_id:
            if repository_id not in self.configured_repositories:
                raise ValueError(
                    f"Unknown configured repository_id: '{repository_id}'"
                )
            candidate = self.configured_repositories[repository_id]
        elif repository_path:
            candidate = Path(repository_path)
        else:
            raise ValueError("Either repository_path or repository_id is required")

        resolved = candidate.resolve()
        if not self.is_repository_allowed(resolved):
            raise ValueError(
                f"Repository path '{resolved}' is outside allowed_repository_roots"
            )
        return resolved

    def get_groq_api_key(self) -> str | None:
        """Return the raw Groq API key for explicit live-mode adapter use only."""
        if self.groq_api_key is None:
            return None
        value = self.groq_api_key.get_secret_value().strip()
        return value or None

    def safe_summary(self) -> dict[str, Any]:
        """Return a JSON-safe configuration summary that never exposes secrets."""
        data = self.model_dump(mode="json")
        data["groq_api_key_configured"] = bool(self.get_groq_api_key())
        return data

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> OrchestratorConfig:
        """Build configuration explicitly from environment variables without import side effects."""
        source = os.environ if env is None else env

        def _parse_paths(raw: str | None) -> list[Path]:
            if not raw:
                return []
            return [Path(part.strip()) for part in raw.split(os.pathsep) if part.strip()]

        kwargs: dict[str, Any] = {}
        if source.get("CHANGEGUARD_DB_PATH"):
            kwargs["database_path"] = Path(source["CHANGEGUARD_DB_PATH"])
        if source.get("CHANGEGUARD_WORKSPACE_ROOT"):
            kwargs["workspace_root"] = Path(source["CHANGEGUARD_WORKSPACE_ROOT"])
        if source.get("CHANGEGUARD_ALLOWED_REPO_ROOTS"):
            kwargs["allowed_repository_roots"] = _parse_paths(
                source["CHANGEGUARD_ALLOWED_REPO_ROOTS"]
            )
        if source.get("CHANGEGUARD_TRUSTED_EXECUTION_ROOTS"):
            kwargs["trusted_execution_roots"] = _parse_paths(
                source["CHANGEGUARD_TRUSTED_EXECUTION_ROOTS"]
            )
        if source.get("CHANGEGUARD_VERIFICATION_TIMEOUT"):
            kwargs["verification_timeout_seconds"] = int(
                source["CHANGEGUARD_VERIFICATION_TIMEOUT"]
            )
        if source.get("CHANGEGUARD_GIT_TIMEOUT"):
            kwargs["git_timeout_seconds"] = int(source["CHANGEGUARD_GIT_TIMEOUT"])
        if source.get("CHANGEGUARD_EXECUTION_MODE"):
            kwargs["execution_mode"] = ExecutionMode(
                source["CHANGEGUARD_EXECUTION_MODE"].strip()
            )
        if source.get("GROQ_MODEL"):
            kwargs["groq_model"] = source["GROQ_MODEL"].strip()
        if source.get("GROQ_API_KEY"):
            kwargs["groq_api_key"] = SecretStr(source["GROQ_API_KEY"].strip())
        if source.get("CHANGEGUARD_MAX_REPAIR_ATTEMPTS"):
            kwargs["max_repair_attempts"] = int(
                source["CHANGEGUARD_MAX_REPAIR_ATTEMPTS"]
            )
        if source.get("CHANGEGUARD_MAX_PATCH_SIZE_BYTES"):
            kwargs["max_patch_size_bytes"] = int(
                source["CHANGEGUARD_MAX_PATCH_SIZE_BYTES"]
            )
        if source.get("CHANGEGUARD_MAX_PATCH_CHANGED_FILES"):
            kwargs["max_patch_changed_files"] = int(
                source["CHANGEGUARD_MAX_PATCH_CHANGED_FILES"]
            )
        if source.get("CHANGEGUARD_MAX_CONCURRENT_RUNS"):
            kwargs["max_concurrent_runs"] = int(
                source["CHANGEGUARD_MAX_CONCURRENT_RUNS"]
            )
        if source.get("CHANGEGUARD_APP_HOST"):
            kwargs["app_host"] = source["CHANGEGUARD_APP_HOST"].strip()
        if source.get("CHANGEGUARD_APP_PORT"):
            kwargs["app_port"] = int(source["CHANGEGUARD_APP_PORT"])
        if source.get("CHANGEGUARD_CORS_ORIGINS"):
            kwargs["cors_allowed_origins"] = [
                part.strip()
                for part in source["CHANGEGUARD_CORS_ORIGINS"].split(",")
                if part.strip()
            ]
        if source.get("CHANGEGUARD_CONFIGURED_REPOSITORIES"):
            configured: dict[str, Path] = {}
            for entry in source["CHANGEGUARD_CONFIGURED_REPOSITORIES"].split(","):
                item = entry.strip()
                if not item or "=" not in item:
                    continue
                repo_id, repo_raw = item.split("=", 1)
                repo_id = repo_id.strip()
                repo_raw = repo_raw.strip()
                if repo_id and repo_raw:
                    configured[repo_id] = Path(repo_raw)
            kwargs["configured_repositories"] = configured
        if source.get("CHANGEGUARD_ENV"):
            kwargs["environment"] = source["CHANGEGUARD_ENV"].strip()

        return cls(**kwargs)
