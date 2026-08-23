from __future__ import annotations

import asyncio
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, AsyncIterator

from .store import Store
from .pipeline import PipelineRunner
from .generation import CodexImageProvider, GenerationRunner, ImageProvider


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GenerationCancellation:
    """Serialize cancellation against the generated-source commit boundary."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._committed = False

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> bool:
        with self._lock:
            if self._committed:
                return False
            self._event.set()
            return True

    def commit(self, operation: Any) -> Any:
        with self._lock:
            if self._event.is_set():
                raise asyncio.CancelledError
            result = operation()
            self._committed = True
            return result


class JobManager:
    def __init__(self, store: Store, generation_providers: dict[str, ImageProvider] | None = None):
        self.store = store
        self.pipeline = PipelineRunner(store)
        self.generation = GenerationRunner(store, generation_providers or {"codex": CodexImageProvider()})
        self._worker_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vinylgen-worker")
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._pipeline_cancellations: dict[str, threading.Event | GenerationCancellation] = {}
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def start_generation(self, job_id: str, *, project_id: str, prompt: str,
                         settings: dict[str, Any]) -> None:
        self._pipeline_cancellations[job_id] = GenerationCancellation()
        self._tasks[job_id] = asyncio.create_task(
            self._run_generation(job_id, project_id, prompt, settings),
            name=f"vinylgen-generation-{job_id}")

    async def _run_generation(self, job_id: str, project_id: str, prompt: str,
                              settings: dict[str, Any]) -> None:
        loop = asyncio.get_running_loop()
        cancellation = self._pipeline_cancellations[job_id]
        def progress(value: float, message: str) -> None:
            if cancellation.is_set():
                raise asyncio.CancelledError
            asyncio.run_coroutine_threadsafe(
                self._update(job_id, progress=value, message=message), loop).result()
        try:
            await self._update(job_id, status="running", started_at=_now(), message="Expanding prompt")
            result_id = await loop.run_in_executor(
                self._worker_executor,
                lambda: self.generation.run(
                    project_id, prompt, settings, progress, cancellation.is_set,
                    cancellation.commit))
            if cancellation.is_set():
                raise asyncio.CancelledError
            await self._update(job_id, status="succeeded", progress=1, message="Complete",
                               result_revision_id=result_id, finished_at=_now())
        except asyncio.CancelledError:
            await self._update(job_id, status="cancelled", message="Cancelled", finished_at=_now())
        except Exception as exc:
            await self._update(job_id, status="failed", error=str(exc), message="Failed", finished_at=_now())
        finally:
            self._tasks.pop(job_id, None)
            self._pipeline_cancellations.pop(job_id, None)

    def start_demo(self, job_id: str, *, steps: int, step_delay_ms: int) -> None:
        self._tasks[job_id] = asyncio.create_task(
            self._run_demo(job_id, steps, step_delay_ms), name=f"vinylgen-job-{job_id}"
        )

    def start_pipeline(self, job_id: str, *, kind: str, project_id: str, revision_id: str,
                       palette: dict | None, trace: dict | None,
                       colour_names: dict[str, str]) -> None:
        self._pipeline_cancellations[job_id] = threading.Event()
        self._tasks[job_id] = asyncio.create_task(
            self._run_pipeline(job_id, kind, project_id, revision_id, palette, trace, colour_names),
            name=f"vinylgen-job-{job_id}")

    async def _run_pipeline(self, job_id: str, kind: str, project_id: str, revision_id: str,
                            palette: dict | None, trace: dict | None,
                            colour_names: dict[str, str]) -> None:
        loop = asyncio.get_running_loop()
        cancellation = self._pipeline_cancellations[job_id]

        def progress(value: float, message: str) -> None:
            if cancellation.is_set():
                raise asyncio.CancelledError
            asyncio.run_coroutine_threadsafe(
                self._update(job_id, progress=value, message=message), loop).result()

        try:
            await self._update(job_id, status="running", started_at=_now(), message="Starting")
            result_revision_id = await loop.run_in_executor(
                self._worker_executor,
                lambda: self.pipeline.run(
                    job_id, kind, project_id, revision_id, palette=palette,
                    trace=trace, colour_names=colour_names, progress=progress))
            if cancellation.is_set():
                raise asyncio.CancelledError
            await self._update(job_id, status="succeeded", progress=1, message="Complete",
                               result_revision_id=result_revision_id, finished_at=_now())
        except asyncio.CancelledError:
            await self._update(job_id, status="cancelled", message="Cancelled", finished_at=_now())
        except Exception as exc:
            await self._update(job_id, status="failed", error=str(exc), message="Failed",
                               finished_at=_now())
        finally:
            self._tasks.pop(job_id, None)
            self._pipeline_cancellations.pop(job_id, None)

    async def _run_demo(self, job_id: str, steps: int, delay_ms: int) -> None:
        try:
            await self._update(job_id, status="running", started_at=_now(), message="Starting")
            for step in range(1, steps + 1):
                await asyncio.sleep(delay_ms / 1000)
                await self._update(job_id, progress=step / steps, message=f"Step {step} of {steps}")
            await self._update(job_id, status="succeeded", progress=1, message="Complete", finished_at=_now())
        except asyncio.CancelledError:
            await self._update(job_id, status="cancelled", message="Cancelled", finished_at=_now())
        except Exception as exc:  # pragma: no cover - defensive boundary
            await self._update(job_id, status="failed", error=str(exc), finished_at=_now())
        finally:
            self._tasks.pop(job_id, None)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        task = self._tasks.get(job_id)
        if task:
            cancellation = self._pipeline_cancellations.get(job_id)
            if cancellation:
                requested = cancellation.set()
                if requested is False:
                    await self._update(
                        job_id,
                        message="Commit already started; cancellation is no longer possible",
                    )
                    return self.store.get_job(job_id)
                await self._update(
                    job_id,
                    message="Cancellation requested; waiting for current operation",
                )
                return self.store.get_job(job_id)
            else:
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            current = self.store.get_job(job_id)
            if current["status"] in {"queued", "running"}:
                await self._update(job_id, status="cancelled", message="Cancelled", finished_at=_now())
        else:
            await self._update(job_id, status="cancelled", message="Cancelled", finished_at=_now())
        return self.store.get_job(job_id)

    def start_inline(self, job_id: str, message: str, function: Any) -> asyncio.Task[Any]:
        """Own request-originated blocking work until its DB job is terminal.

        The returned task should be awaited through ``asyncio.shield`` by the
        request handler.  Losing the HTTP request must not abandon either the
        worker future or the capacity-accounting job row.
        """
        self._pipeline_cancellations[job_id] = threading.Event()
        task = asyncio.create_task(
            self._run_inline(job_id, message, function),
            name=f"vinylgen-inline-{job_id}",
        )
        self._tasks[job_id] = task
        return task

    async def _run_inline(self, job_id: str, message: str, function: Any) -> Any:
        cancellation = self._pipeline_cancellations[job_id]
        try:
            await self._update(job_id, status="running", started_at=_now(), message=message)
            result = await self.run_blocking(
                lambda: function(cancellation.is_set)
            )
            if cancellation.is_set():
                await self._update(job_id, status="cancelled", message="Cancelled",
                                   finished_at=_now())
            else:
                await self._update(job_id, status="succeeded", progress=1, message="Complete",
                                   finished_at=_now())
            return result
        except asyncio.CancelledError:
            await self._update(job_id, status="cancelled", message="Cancelled",
                               finished_at=_now())
            raise
        except Exception as exc:
            await self._update(job_id, status="failed", error=str(exc), message="Failed",
                               finished_at=_now())
            raise
        finally:
            self._tasks.pop(job_id, None)
            self._pipeline_cancellations.pop(job_id, None)

    async def run_blocking(self, function: Any) -> Any:
        """Run provider/image work off-loop in Vinylgen's owned executor."""
        future = self._worker_executor.submit(function)
        # Polling also keeps this robust in embedded ASGI/test loops whose
        # cross-thread wakeup can be suppressed during request handling.
        while not future.done():
            await asyncio.sleep(.01)
        return future.result()

    async def _update(self, job_id: str, **changes: Any) -> None:
        job = self.store.update_job(job_id, **changes)
        for queue in tuple(self._subscribers[job_id]):
            queue.put_nowait(job)

    async def subscribe(self, job_id: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers[job_id].add(queue)
        try:
            initial = self.store.get_job(job_id)
            yield initial
            if initial["status"] in {"succeeded", "failed", "cancelled"}:
                return
            while True:
                try:
                    job = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield {"keepalive": True}
                    continue
                yield job
                if job["status"] in {"succeeded", "failed", "cancelled"}:
                    return
        finally:
            self._subscribers[job_id].discard(queue)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for cancellation in tuple(self._pipeline_cancellations.values()):
            cancellation.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_executor.shutdown(wait=True, cancel_futures=True)
