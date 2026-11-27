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

from project.schemas.agents import (
    AgentError,
    BaselineComparisonEvidence,
    BaselineVerificationResult,
    ModifiedVerificationResult,
    SuiteExecutionSummary,
    TestExecutionEvidence,
)
from project.schemas.patch import PatchApplicationResult, PatchGenerationResult, PatchValidationResult
from project.schemas.repair import (
    PatchAttemptRecord,
    RepairAttemptHistory,
    RepairEligibilityResult,
)
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


def _sanitize_test_evidence_list(
    items: list[TestExecutionEvidence],
) -> list[TestExecutionEvidence]:
    return [
        ev.model_copy(
            update={
                "collection_errors": [redact_secrets(c) for c in ev.collection_errors],
                "failure_message": (
                    redact_secrets(ev.failure_message)
                    if ev.failure_message is not None
                    else None
                ),
                "evidence_limitations": [
                    redact_secrets(lim) for lim in ev.evidence_limitations
                ],
            }
        )
        for ev in items
    ]


def _sanitize_suite_summary(
    suite: SuiteExecutionSummary | None,
) -> SuiteExecutionSummary | None:
    if suite is None:
        return None
    return suite.model_copy(
        update={
            "collection_errors": [redact_secrets(c) for c in suite.collection_errors],
            "failures": [
                fail.model_copy(update={"evidence": redact_secrets(fail.evidence)})
                for fail in suite.failures
            ],
            "test_evidence": _sanitize_test_evidence_list(suite.test_evidence),
            "evidence_limitations": [
                redact_secrets(lim) for lim in suite.evidence_limitations
            ],
            "error": _sanitize_error(suite.error),
        }
    )


def _sanitize_baseline_verification(
    res: BaselineVerificationResult | None,
) -> BaselineVerificationResult | None:
    if res is None:
        return None
    return res.model_copy(
        update={
            "collection_errors": [redact_secrets(c) for c in res.collection_errors],
            "failures": [
                fail.model_copy(update={"evidence": redact_secrets(fail.evidence)})
                for fail in res.failures
            ],
            "test_evidence": _sanitize_test_evidence_list(res.test_evidence),
            "existing_regression_suite": _sanitize_suite_summary(
                res.existing_regression_suite
            ),
            "evidence_limitations": [
                redact_secrets(lim) for lim in res.evidence_limitations
            ],
            "error": _sanitize_error(res.error),
        }
    )


def _sanitize_modified_verification(
    res: ModifiedVerificationResult | None,
) -> ModifiedVerificationResult | None:
    if res is None:
        return None
    return res.model_copy(
        update={
            "collection_errors": [redact_secrets(c) for c in res.collection_errors],
            "failures": [
                fail.model_copy(update={"evidence": redact_secrets(fail.evidence)})
                for fail in res.failures
            ],
            "test_evidence": _sanitize_test_evidence_list(res.test_evidence),
            "existing_regression_suite": _sanitize_suite_summary(
                res.existing_regression_suite
            ),
            "evidence_limitations": [
                redact_secrets(lim) for lim in res.evidence_limitations
            ],
            "error": _sanitize_error(res.error),
        }
    )


def _sanitize_baseline_comparison(
    comp: BaselineComparisonEvidence | None,
) -> BaselineComparisonEvidence | None:
    if comp is None:
        return None
    return comp.model_copy(
        update={
            "baseline_result": _sanitize_baseline_verification(comp.baseline_result),
            "modified_result": _sanitize_modified_verification(comp.modified_result),
            "test_comparisons": [
                tc.model_copy(
                    update={
                        "rationale": redact_secrets(tc.rationale),
                        "limitations": [redact_secrets(lim) for lim in tc.limitations],
                    }
                )
                for tc in comp.test_comparisons
            ],
            "summary": comp.summary.model_copy(
                update={
                    "limitations": [
                        redact_secrets(lim) for lim in comp.summary.limitations
                    ]
                }
            ),
            "evidence_limitations": [
                redact_secrets(lim) for lim in comp.evidence_limitations
            ],
            "notes": redact_secrets(comp.notes),
        }
    )


def _sanitize_patch_generation(
    pg: PatchGenerationResult | None,
) -> PatchGenerationResult | None:
    if pg is None:
        return None
    sanitized_proposal = None
    if pg.proposal is not None:
        sanitized_proposal = pg.proposal.model_copy(
            update={
                "change_rationale": redact_secrets(pg.proposal.change_rationale),
                "assumptions": [redact_secrets(a) for a in pg.proposal.assumptions],
                "risk_indicators": [
                    redact_secrets(r) for r in pg.proposal.risk_indicators
                ],
                "notes": [redact_secrets(n) for n in pg.proposal.notes],
            }
        )
    return pg.model_copy(
        update={
            "proposal": sanitized_proposal,
            "error": _sanitize_error(pg.error),
            "notes": [redact_secrets(note) for note in pg.notes],
        }
    )


def _sanitize_patch_validation(
    pv: PatchValidationResult | None,
) -> PatchValidationResult | None:
    if pv is None:
        return None
    return pv.model_copy(
        update={
            "violations": [
                sanitized
                for v in pv.violations
                if (sanitized := _sanitize_error(v)) is not None
            ],
        }
    )


def _sanitize_patch_application(
    pa: PatchApplicationResult | None,
) -> PatchApplicationResult | None:
    if pa is None:
        return None
    return pa.model_copy(update={"error": _sanitize_error(pa.error)})


def _sanitize_repair_eligibility(
    elig: RepairEligibilityResult | None,
) -> RepairEligibilityResult | None:
    if elig is None:
        return None
    return elig.model_copy(
        update={
            "decision_reason": redact_secrets(elig.decision_reason),
            "blocking_reasons": [redact_secrets(r) for r in elig.blocking_reasons],
        }
    )


def _sanitize_patch_attempt(att: PatchAttemptRecord) -> PatchAttemptRecord:
    return att.model_copy(
        update={
            "original_requirement": redact_secrets(att.original_requirement),
            "patch_generation": _sanitize_patch_generation(att.patch_generation),
            "patch_validation": _sanitize_patch_validation(att.patch_validation),
            "patch_application": _sanitize_patch_application(att.patch_application),
            "baseline_verification": _sanitize_baseline_verification(
                att.baseline_verification
            ),
            "modified_verification": _sanitize_modified_verification(
                att.modified_verification
            ),
            "failure_diagnostics": [
                diag.model_copy(
                    update={
                        "evidence": redact_secrets(diag.evidence),
                        "reason": redact_secrets(diag.reason),
                        "recommendation": redact_secrets(diag.recommendation),
                    }
                )
                for diag in att.failure_diagnostics
            ],
            "baseline_comparison": _sanitize_baseline_comparison(
                att.baseline_comparison
            ),
            "repair_eligibility": _sanitize_repair_eligibility(att.repair_eligibility),
            "errors": [
                sanitized
                for err in att.errors
                if (sanitized := _sanitize_error(err)) is not None
            ],
            "notes": [redact_secrets(note) for note in att.notes],
        }
    )


def _sanitize_repair_history(
    hist: RepairAttemptHistory | None,
) -> RepairAttemptHistory | None:
    if hist is None:
        return None
    return hist.model_copy(
        update={
            "attempts": [_sanitize_patch_attempt(att) for att in hist.attempts],
            "last_eligibility": _sanitize_repair_eligibility(hist.last_eligibility),
            "termination_summary": redact_secrets(hist.termination_summary),
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

    if run.requirement_analysis is not None:
        updates["requirement_analysis"] = run.requirement_analysis.model_copy(
            update={
                "requirement_summary": redact_secrets(
                    run.requirement_analysis.requirement_summary
                ),
                "error": _sanitize_error(run.requirement_analysis.error),
                "notes": [redact_secrets(note) for note in run.requirement_analysis.notes],
            }
        )

    if run.pre_change_context is not None:
        updates["pre_change_context"] = run.pre_change_context.model_copy(
            update={
                "limitation_notes": [
                    redact_secrets(note)
                    for note in run.pre_change_context.limitation_notes
                ],
                "error": _sanitize_error(run.pre_change_context.error),
            }
        )

    if run.patch_generation_request is not None:
        req_obj = run.patch_generation_request
        sanitized_prev = (
            redact_nested_secrets(req_obj.previous_verification_evidence)
            if req_obj.previous_verification_evidence is not None
            else None
        )
        sanitized_bc_ev = (
            redact_nested_secrets(req_obj.baseline_comparison_evidence)
            if req_obj.baseline_comparison_evidence is not None
            else None
        )
        updates["patch_generation_request"] = req_obj.model_copy(
            update={
                "original_requirement": redact_secrets(req_obj.original_requirement),
                # Never persist raw source file contents into SQLite; retain file path list only.
                "relevant_source_files": {},
                "previous_proposed_patch": (
                    redact_secrets(req_obj.previous_proposed_patch)
                    if req_obj.previous_proposed_patch is not None
                    else None
                ),
                "failure_diagnostics": [
                    diag.model_copy(
                        update={
                            "evidence": redact_secrets(diag.evidence),
                            "reason": redact_secrets(diag.reason),
                            "recommendation": redact_secrets(diag.recommendation),
                        }
                    )
                    for diag in req_obj.failure_diagnostics
                ],
                "baseline_comparison_evidence": (
                    sanitized_bc_ev if isinstance(sanitized_bc_ev, dict) else None
                ),
                "previous_verification_evidence": (
                    sanitized_prev if isinstance(sanitized_prev, dict) else None
                ),
            }
        )

    if run.patch_generation is not None:
        updates["patch_generation"] = _sanitize_patch_generation(run.patch_generation)

    if run.patch_validation is not None:
        updates["patch_validation"] = _sanitize_patch_validation(run.patch_validation)

    if run.patch_application is not None:
        updates["patch_application"] = _sanitize_patch_application(run.patch_application)

    if run.repair_eligibility is not None:
        updates["repair_eligibility"] = _sanitize_repair_eligibility(
            run.repair_eligibility
        )

    if run.repair_history is not None:
        updates["repair_history"] = _sanitize_repair_history(run.repair_history)

    if run.codebase_analysis is not None and run.codebase_analysis.error is not None:
        updates["codebase_analysis"] = run.codebase_analysis.model_copy(
            update={"error": _sanitize_error(run.codebase_analysis.error)}
        )

    if run.test_generation is not None and run.test_generation.error is not None:
        updates["test_generation"] = run.test_generation.model_copy(
            update={"error": _sanitize_error(run.test_generation.error)}
        )

    if run.report is not None:
        rep_updates: dict[str, object] = {}
        if run.report.requirement_analysis is not None:
            req_rep = run.report.requirement_analysis
            rep_updates["requirement_analysis"] = req_rep.model_copy(
                update={
                    "requirement_summary": redact_secrets(
                        req_rep.requirement_summary
                    ),
                    "error": _sanitize_error(req_rep.error),
                    "notes": [redact_secrets(note) for note in req_rep.notes],
                }
            )
        if run.report.baseline_verification is not None:
            rep_updates["baseline_verification"] = _sanitize_baseline_verification(
                run.report.baseline_verification
            )
        if run.report.modified_verification is not None:
            rep_updates["modified_verification"] = _sanitize_modified_verification(
                run.report.modified_verification
            )
        if run.report.baseline_comparison is not None:
            rep_updates["baseline_comparison"] = _sanitize_baseline_comparison(
                run.report.baseline_comparison
            )
        if run.report.repair_eligibility is not None:
            rep_updates["repair_eligibility"] = _sanitize_repair_eligibility(
                run.report.repair_eligibility
            )
        if run.report.repair_history is not None:
            rep_updates["repair_history"] = _sanitize_repair_history(
                run.report.repair_history
            )
        if run.report.evidence_limitations:
            rep_updates["evidence_limitations"] = [
                redact_secrets(lim) for lim in run.report.evidence_limitations
            ]
        if rep_updates:
            updates["report"] = run.report.model_copy(update=rep_updates)

    if run.baseline_verification is not None:
        updates["baseline_verification"] = _sanitize_baseline_verification(
            run.baseline_verification
        )
    if run.modified_verification is not None:
        updates["modified_verification"] = _sanitize_modified_verification(
            run.modified_verification
        )
    if run.baseline_comparison is not None:
        updates["baseline_comparison"] = _sanitize_baseline_comparison(
            run.baseline_comparison
        )

    if run.verification is not None:
        sanitized_failures = [
            fail.model_copy(update={"evidence": redact_secrets(fail.evidence)})
            for fail in run.verification.failures
        ]
        sanitized_bc = _sanitize_baseline_comparison(
            run.verification.baseline_comparison
        )
        updates["verification"] = run.verification.model_copy(
            update={
                "error": _sanitize_error(run.verification.error),
                "collection_errors": [
                    redact_secrets(err) for err in run.verification.collection_errors
                ],
                "failures": sanitized_failures,
                "baseline_comparison": sanitized_bc
                if sanitized_bc is not None
                else run.verification.baseline_comparison,
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
