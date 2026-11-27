"""Shared low-level schema primitives for ChangeGuard AI (M02).

Kept dependency-free so ``requirements.py``, ``agents.py``, and ``workflow.py``
can share ``AgentError``, ``utc_now``, and identifier validation without circular
imports.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class AgentError(BaseModel):
    """Structured, non-secret error payload recorded across M02 components."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=100)
    message: str = Field(..., min_length=1, max_length=2000)
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
