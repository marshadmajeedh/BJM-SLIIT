"""Offline unit tests for SQLite RunStore persistence and structured logs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from project.marsadh_orchestrator.store import RunStore, generate_run_id
from project.schemas import (
    AgentError,
    AgentExecutionStatus,
    AgentLogEntry,
    RepositoryInfo,
    WorkflowRunRecord,
    WorkflowState,
)


def _sample_run(run_id: str | None = None, state: WorkflowState = WorkflowState.CREATED) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        run_id=run_id or generate_run_id(),
        state=state,
        requirements="Apply 10% discount to premium customers.",
        repository=RepositoryInfo(
            repository_path="/tmp/demo_repo",
            source_path="src",
            base_ref="HEAD",
            change_mode="working_tree",
        ),
    )


def test_save_retrieve_list_update_and_reload_across_instances(tmp_path) -> None:
    db_path = tmp_path / "state" / "changeguard.sqlite3"
    store = RunStore(db_path)

    run1 = _sample_run("run_001")
    run2 = _sample_run("run_002")
    store.save_run(run1)
    store.save_run(run2)

    loaded = store.get_run("run_001")
    assert loaded is not None
    assert loaded.run_id == "run_001"
    assert loaded.state == WorkflowState.CREATED

    updated = store.update_state(
        "run_001",
        WorkflowState.ANALYZING_CODEBASE,
        expected_state=WorkflowState.CREATED,
    )
    assert updated.state == WorkflowState.ANALYZING_CODEBASE

    with pytest.raises(ValueError):
        store.update_state(
            "run_001",
            WorkflowState.GENERATING_TESTS,
            expected_state=WorkflowState.CREATED,
        )

    with pytest.raises(KeyError):
        store.update_state("run_missing", WorkflowState.FAILED)

    # Append structured logs, including synthetic secret-like text that must be redacted
    fake_groq_token = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
    store.append_log(
        AgentLogEntry(
            run_id="run_001",
            agent_name="CodebaseAdapter",
            stage=WorkflowState.ANALYZING_CODEBASE,
            status=AgentExecutionStatus.SUCCEEDED,
            duration_ms=42,
            input_summary=f"Analyzing repo with token {fake_groq_token}",
            output_summary="1 changed file found",
        )
    )
    store.append_log(
        AgentLogEntry(
            run_id="run_001",
            agent_name="TestingAdapter",
            stage=WorkflowState.GENERATING_TESTS,
            status=AgentExecutionStatus.FAILED,
            duration_ms=15,
            input_summary="Generating scenarios",
            output_summary="Failed",
            error=AgentError(
                code="ERR",
                message=f"api_key={fake_groq_token}",
            ),
        )
    )

    with pytest.raises(KeyError):
        store.append_log(
            AgentLogEntry(
                run_id="run_unknown",
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE,
                status=AgentExecutionStatus.FAILED,
                duration_ms=1,
            )
        )

    # Re-open SQLite DB via a brand new RunStore instance to simulate restart
    reloaded_store = RunStore(db_path)
    reloaded_run = reloaded_store.get_run("run_001")
    assert reloaded_run is not None
    assert reloaded_run.state == WorkflowState.ANALYZING_CODEBASE

    all_runs = reloaded_store.list_runs()
    assert {r.run_id for r in all_runs} == {"run_001", "run_002"}

    filtered = reloaded_store.list_runs(state=WorkflowState.ANALYZING_CODEBASE)
    assert [r.run_id for r in filtered] == ["run_001"]

    logs = reloaded_store.get_logs("run_001")
    assert len(logs) == 2
    assert logs[0].agent_name == "CodebaseAdapter"
    assert fake_groq_token not in logs[0].input_summary
    assert "[REDACTED_GROQ_KEY]" in logs[0].input_summary
    assert logs[1].error is not None
    assert fake_groq_token not in logs[1].error.message


def test_concurrent_updates_do_not_overwrite_unrelated_runs(tmp_path) -> None:
    db_path = tmp_path / "concurrent.sqlite3"
    store = RunStore(db_path)

    run_ids = [f"run_concurrent_{idx:02d}" for idx in range(12)]
    for run_id in run_ids:
        store.save_run(_sample_run(run_id))

    def _worker(run_id: str) -> None:
        local_store = RunStore(db_path)
        local_store.update_state(
            run_id,
            WorkflowState.ANALYZING_CODEBASE,
            expected_state=WorkflowState.CREATED,
        )
        local_store.append_log(
            AgentLogEntry(
                run_id=run_id,
                agent_name="CodebaseAdapter",
                stage=WorkflowState.ANALYZING_CODEBASE,
                status=AgentExecutionStatus.SUCCEEDED,
                duration_ms=5,
                input_summary=f"worker {run_id}",
                output_summary="ok",
            )
        )
        local_store.update_state(
            run_id,
            WorkflowState.COMPLETED_NO_CHANGES,
            expected_state=WorkflowState.ANALYZING_CODEBASE,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_worker, rid) for rid in run_ids]
        for fut in futures:
            fut.result()

    for run_id in run_ids:
        rec = store.get_run(run_id)
        assert rec is not None
        assert rec.state == WorkflowState.COMPLETED_NO_CHANGES
        logs = store.get_logs(run_id)
        assert len(logs) == 1
        assert logs[0].run_id == run_id
