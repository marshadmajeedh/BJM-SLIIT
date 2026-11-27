"""Bounded in-process background task executor for ChangeGuard AI MVP.

Design & limitations:
- Uses a bounded ``ThreadPoolExecutor`` so blocking Git, LLM, and pytest
  operations never block the ASGI event loop.
- Tracks active futures per ``run_id`` to prevent overlapping background
  executions for the same workflow run.
- Because tasks execute in-process rather than in an external durable queue,
  a process restart interrupts active stages; ``OrchestratorService.recover_interrupted_runs``
  marks those runs as FAILED on restart without replaying external calls.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger("changeguard.executor")


class BoundedRunExecutor:
    """Thread-safe bounded in-process executor keyed by workflow run_id."""

    def __init__(self, max_workers: int = 4) -> None:
        self._max_workers = max(1, int(max_workers))
        self._lock = threading.Lock()
        self._active_futures: dict[str, Future[Any]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="changeguard-worker",
        )
        self._closed = False

    def submit(
        self,
        run_id: str,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        """Schedule a background workflow stage for ``run_id``, preventing overlap."""
        with self._lock:
            if self._closed:
                raise RuntimeError("Background executor is shut down")
            existing = self._active_futures.get(run_id)
            if existing is not None and not existing.done():
                raise RuntimeError(
                    f"Background execution is already in progress for run '{run_id}'"
                )

            def _wrapped() -> Any:
                try:
                    return fn(*args, **kwargs)
                except Exception:  # noqa: BLE001
                    logger.exception("Unhandled error in background stage for run %s", run_id)
                    raise
                finally:
                    with self._lock:
                        if self._active_futures.get(run_id) is future:
                            self._active_futures.pop(run_id, None)

            future: Future[Any] = self._executor.submit(_wrapped)
            self._active_futures[run_id] = future
            return future

    def get_future(self, run_id: str) -> Future[Any] | None:
        """Return the active Future for ``run_id`` if currently scheduled."""
        with self._lock:
            return self._active_futures.get(run_id)

    def wait_for_run(self, run_id: str, timeout: float = 30.0) -> None:
        """Deterministically wait for an in-flight background task for ``run_id``."""
        future = self.get_future(run_id)
        if future is not None:
            try:
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001
                # Orchestrator persists structured failures onto the run record;
                # callers inspect run state via API/store.
                pass

    def wait_all(self, timeout: float = 30.0) -> None:
        """Wait for all currently active background tasks to complete."""
        with self._lock:
            futures = list(self._active_futures.values())
        for future in futures:
            try:
                future.result(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass

    def active_run_ids(self) -> list[str]:
        """Return sorted run IDs with active in-flight tasks."""
        with self._lock:
            return sorted(
                rid for rid, fut in self._active_futures.items() if not fut.done()
            )

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the underlying thread pool cleanly."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)
