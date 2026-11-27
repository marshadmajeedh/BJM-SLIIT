"""SQLite persistence layer for ChangeGuard AI workflow runs and agent logs.

Guarantees:
- Parameterized SQL queries only (no string interpolation in SQL).
- Explicit ``BEGIN IMMEDIATE`` transactions with WAL mode and busy timeout so
  concurrent writers cannot silently overwrite unrelated run records.
- Secret pattern redaction before persisting communication logs.
- Connections are always closed cleanly after each operation.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from project.schemas.agents import AgentError
from project.schemas.workflow import AgentLogEntry, WorkflowRunRecord, WorkflowState

_SECRET_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GITHUB_PAT]"),
    (re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b"), "[REDACTED_GROQ_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED_GOOGLE_KEY]"),
    (
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            r".*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{10,}"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[^\s'\",;]{6,}['\"]?"
        ),
        r"\1=[REDACTED]",
    ),
)


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def generate_run_id() -> str:
    """Generate a collision-resistant run identifier."""
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    return f"run_{stamp}_{uuid.uuid4().hex[:12]}"


def generate_log_id() -> str:
    """Generate a collision-resistant structured log entry identifier."""
    return f"log_{uuid.uuid4().hex[:16]}"


def redact_secrets(text: str) -> str:
    """Mask common credential patterns before writing logs to storage."""
    if not text:
        return text
    sanitized = text
    for pattern, replacement in _SECRET_REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def redact_nested_secrets(value: Any) -> Any:
    """Recursively redact secrets across nested dicts, lists, tuples, and strings."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: redact_nested_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_nested_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_nested_secrets(item) for item in value]
    return value


def _sanitize_error(error: AgentError | None) -> AgentError | None:
    if error is None:
        return None
    sanitized_details = redact_nested_secrets(error.details)
    return error.model_copy(
        update={
            "message": redact_secrets(error.message),
            "details": sanitized_details if isinstance(sanitized_details, dict) else {},
        }
    )


def _sanitize_run_record(run: WorkflowRunRecord) -> WorkflowRunRecord:
    """Redact secret patterns across top-level and nested run errors/summaries."""
    updates: dict[str, object] = {"error": _sanitize_error(run.error)}

    if run.agent_records:
        updates["agent_records"] = [
            rec.model_copy(
                update={
                    "output_summary": redact_secrets(rec.output_summary),
                    "error": _sanitize_error(rec.error),
                }
            )
            for rec in run.agent_records
        ]

    if run.codebase_analysis is not None and run.codebase_analysis.error is not None:
        updates["codebase_analysis"] = run.codebase_analysis.model_copy(
            update={"error": _sanitize_error(run.codebase_analysis.error)}
        )

    if run.test_generation is not None and run.test_generation.error is not None:
        updates["test_generation"] = run.test_generation.model_copy(
            update={"error": _sanitize_error(run.test_generation.error)}
        )

    if run.verification is not None:
        sanitized_failures = [
            fail.model_copy(update={"evidence": redact_secrets(fail.evidence)})
            for fail in run.verification.failures
        ]
        updates["verification"] = run.verification.model_copy(
            update={
                "error": _sanitize_error(run.verification.error),
                "collection_errors": [
                    redact_secrets(err) for err in run.verification.collection_errors
                ],
                "failures": sanitized_failures,
                "stdout_excerpt": redact_secrets(run.verification.stdout_excerpt),
                "stderr_excerpt": redact_secrets(run.verification.stderr_excerpt),
            }
        )

    return run.model_copy(update=updates)


class RunStore:
    """Lightweight SQLite store for workflow run state and structured agent logs."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.initialize()

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.database_path),
            timeout=5.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.execute("COMMIT")
        except Exception:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create SQLite schema and indices if they do not already exist."""
        with self._connect(write=False) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
        with self._connect(write=True) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_logs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    log_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_agent_logs_run_seq
                ON agent_logs (run_id, seq)
                """
            )

    def save_run(self, run: WorkflowRunRecord) -> WorkflowRunRecord:
        """Persist a new or updated WorkflowRunRecord by its unique run_id."""
        now = utc_now()
        record = _sanitize_run_record(run).model_copy(update={"updated_at": now})
        payload = record.model_dump_json()
        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs (run_id, state, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = excluded.updated_at,
                    payload_json = excluded.payload_json
                """,
                (
                    record.run_id,
                    record.state.value,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    payload,
                ),
            )
        return record

    def get_run(self, run_id: str) -> WorkflowRunRecord | None:
        """Retrieve a single workflow run by run_id, or None if not found."""
        with self._connect(write=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return WorkflowRunRecord.model_validate_json(row["payload_json"])

    def list_runs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        state: WorkflowState | None = None,
    ) -> list[WorkflowRunRecord]:
        """List persisted runs ordered from newest to oldest."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        with self._connect(write=False) as conn:
            if state is not None:
                rows = conn.execute(
                    """
                    SELECT payload_json
                    FROM workflow_runs
                    WHERE state = ?
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (state.value, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT payload_json
                    FROM workflow_runs
                    ORDER BY created_at DESC, run_id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        return [
            WorkflowRunRecord.model_validate_json(row["payload_json"])
            for row in rows
        ]

    def count_runs(self, *, state: WorkflowState | None = None) -> int:
        """Return the total number of persisted runs, optionally filtered by state."""
        with self._connect(write=False) as conn:
            if state is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM workflow_runs WHERE state = ?",
                    (state.value,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM workflow_runs"
                ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def list_runs_by_states(
        self, states: Iterable[WorkflowState]
    ) -> list[WorkflowRunRecord]:
        """Return all persisted runs whose state belongs to the given set."""
        state_values = [state.value for state in states]
        if not state_values:
            return []
        placeholders = ",".join("?" for _ in state_values)
        query = (
            f"SELECT payload_json FROM workflow_runs "
            f"WHERE state IN ({placeholders}) "
            f"ORDER BY created_at ASC, run_id ASC"
        )
        with self._connect(write=False) as conn:
            rows = conn.execute(query, state_values).fetchall()
        return [
            WorkflowRunRecord.model_validate_json(row["payload_json"])
            for row in rows
        ]

    def check_health(self) -> bool:
        """Return True if the SQLite database responds to a lightweight read query."""
        try:
            with self._connect(write=False) as conn:
                row = conn.execute("SELECT 1 AS ok").fetchone()
            return bool(row is not None and row["ok"] == 1)
        except sqlite3.Error:
            return False

    def update_run_atomic(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState | set[WorkflowState] | frozenset[WorkflowState] | None = None,
        updater: Callable[[WorkflowRunRecord], WorkflowRunRecord],
    ) -> WorkflowRunRecord:
        """Atomically verify expected state, apply updater, and persist the run record."""
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Workflow run not found: '{run_id}'")

            current = WorkflowRunRecord.model_validate_json(row["payload_json"])
            if expected_state is not None:
                allowed = (
                    {expected_state}
                    if isinstance(expected_state, WorkflowState)
                    else set(expected_state)
                )
                if current.state not in allowed:
                    allowed_str = ", ".join(sorted(s.value for s in allowed))
                    raise ValueError(
                        f"State conflict for run '{run_id}': expected one of "
                        f"[{allowed_str}], found '{current.state.value}'"
                    )

            updated = updater(current)
            sanitized = _sanitize_run_record(updated).model_copy(
                update={"updated_at": utc_now()}
            )
            conn.execute(
                """
                UPDATE workflow_runs
                SET state = ?, updated_at = ?, payload_json = ?
                WHERE run_id = ?
                """,
                (
                    sanitized.state.value,
                    sanitized.updated_at.isoformat(),
                    sanitized.model_dump_json(),
                    run_id,
                ),
            )
        return sanitized

    def update_state(
        self,
        run_id: str,
        new_state: WorkflowState,
        *,
        error: AgentError | None = None,
        expected_state: WorkflowState | None = None,
    ) -> WorkflowRunRecord:
        """Atomically update the workflow state (and optional error) of a single run."""
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Workflow run not found: '{run_id}'")

            current = WorkflowRunRecord.model_validate_json(row["payload_json"])
            if expected_state is not None and current.state != expected_state:
                raise ValueError(
                    f"State conflict for run '{run_id}': expected "
                    f"'{expected_state.value}', found '{current.state.value}'"
                )

            updated = current.model_copy(
                update={
                    "state": new_state,
                    "updated_at": utc_now(),
                    "error": _sanitize_error(error)
                    if error is not None
                    else current.error,
                }
            )
            conn.execute(
                """
                UPDATE workflow_runs
                SET state = ?, updated_at = ?, payload_json = ?
                WHERE run_id = ?
                """,
                (
                    updated.state.value,
                    updated.updated_at.isoformat(),
                    updated.model_dump_json(),
                    run_id,
                ),
            )
        return updated

    def append_log(self, entry: AgentLogEntry) -> AgentLogEntry:
        """Append a sanitized structured communication log entry for an existing run."""
        sanitized = entry.model_copy(
            update={
                "log_id": entry.log_id or generate_log_id(),
                "input_summary": redact_secrets(entry.input_summary),
                "output_summary": redact_secrets(entry.output_summary),
                "error": _sanitize_error(entry.error),
            }
        )
        with self._connect(write=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM workflow_runs WHERE run_id = ?",
                (sanitized.run_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(
                    f"Cannot append log for unknown run_id: '{sanitized.run_id}'"
                )
            conn.execute(
                """
                INSERT INTO agent_logs (
                    log_id,
                    run_id,
                    agent_name,
                    stage,
                    timestamp,
                    status,
                    duration_ms,
                    payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sanitized.log_id,
                    sanitized.run_id,
                    sanitized.agent_name,
                    sanitized.stage.value,
                    sanitized.timestamp.isoformat(),
                    sanitized.status.value,
                    sanitized.duration_ms,
                    sanitized.model_dump_json(),
                ),
            )
        return sanitized

    def get_logs(self, run_id: str) -> list[AgentLogEntry]:
        """Return all structured communication log entries for a run in insertion order."""
        with self._connect(write=False) as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM agent_logs
                WHERE run_id = ?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
        return [AgentLogEntry.model_validate_json(row["payload_json"]) for row in rows]
