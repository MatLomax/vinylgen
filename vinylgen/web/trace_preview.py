"""Live trace and cleanup previews of the zoomed-in canvas area.

The Trace canvas asks for the vector shapes of the window the user is looking at,
traced with the current (unsaved) form settings. Requests run one at a time on a
dedicated background thread, never on the event loop, and a request whose client
has gone away (a newer view or setting aborted it) is dropped at the next phase
boundary or, inside the boundary build and the fit, at its next chain. The thread
holds at most one loaded segmentation (its cost field can be gigabytes), evicting
it before loading another and releasing it after ``SEGMENTATION_IDLE_SECONDS``
without a preview, and keeps a small LRU cache of window boundary graphs with
their fit caches, so a pan re-traces only the new window and a fitting-only change
re-fits cached boundaries.

What is traced is what a "Trace and validate" run from the same revision would
trace: its effective (cleaned) segmentation, the removed colour and physical size
of its active cleanup, and the layer order that run would use. See
``vinylgen.trace_window`` for the documented differences from a full run.

The Cleanup canvas likewise asks for the cleaned pixels of its window under the
current (unsaved) Cleanup form, on the same thread and under the same rules. It
cleans the raster a Cleanup run starts from, with the whole raster's region
measurements computed once per revision and held with it; see
``vinylgen.cleanup_window`` for the documented differences from a full run. The
uncleaned raster and the traced segmentation are never resident together. A
cheap state read, answered off the preview thread, tells the canvas whether those
measurements are held for a revision and whether they are being built now, so it
can say why a first preview is slow.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import Callable, Hashable

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from vinylgen.cleanup_window import (
    CLEANUP_PREVIEW_MARGIN_PX,
    CLEANUP_PREVIEW_MAX_AREA_PX,
    CleanupSource,
    clean_window,
    will_change,
)
from vinylgen.contracts import CleanupSettings
from vinylgen.cutter_cleanup import CLEANUP_ALGORITHM_VERSION, pixel_scale, resolve_physical_size
from vinylgen.trace_window import (
    PREVIEW_MARGIN_PX,
    PREVIEW_MAX_AREA_PX,
    PreviewCancelled,
    PreviewSegmentation,
    Window,
    WindowBoundaries,
    clamp_window,
    margin_window,
    preview_trace_settings,
    preview_variant,
    render_window_svg,
    window_boundaries,
)
from vinylgen.tracing import apply_order_override, ensure_palette_names, resolve_colour

from .models import CleanupJobSettings, TraceJobSettings
from .pipeline import (STALE_CLEANUP_MESSAGE, _require_fresh, cleanup_removed_index,
                       nearest_cleanup)
from .store import Store

BOUNDARY_CACHE_SIZE = 16
# A loaded segmentation (and every cache derived from it) is released after this
# long without a preview, so the web process gives the memory back.
SEGMENTATION_IDLE_SECONDS = 600.0


class PreviewNotFound(Exception):
    """The revision does not exist (HTTP 404)."""


class PreviewShutDown(Exception):
    """The service is shutting down: it stopped this preview or takes no more (HTTP 503)."""

SHUTTING_DOWN_MESSAGE = "server shutting down"


class PreviewDataFault(Exception):
    """The revision's stored data cannot be traced (HTTP 500)."""


class PreviewUnavailable(Exception):
    """The revision cannot be previewed as it stands (HTTP 409)."""


class PreviewRejected(Exception):
    """The request itself is unusable: window or settings (HTTP 422)."""


class PreviewWindow(BaseModel):
    """A window of the traced image in its px; it is clamped to the image."""

    model_config = ConfigDict(extra="forbid")

    x: int
    y: int
    width: int = Field(gt=0, le=1_000_000)
    height: int = Field(gt=0, le=1_000_000)


class TracePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: PreviewWindow
    trace: TraceJobSettings = Field(default_factory=TraceJobSettings)
    colour_names: dict[str, str] = Field(default_factory=dict)


class PreviewBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class TracePreview(BaseModel):
    window: PreviewBox
    crop: PreviewBox
    image: dict[str, int]
    variant: str
    svg: str
    elapsed_ms: float


class CleanupPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window: PreviewWindow
    cleanup: CleanupJobSettings = Field(default_factory=CleanupJobSettings)


class CleanupPreview(BaseModel):
    window: PreviewBox
    crop: PreviewBox
    image: dict[str, int]
    # PNG data URLs of the window at raster px: the cleaned pixels, and what a Cleanup
    # run with these settings would change on the raster shown now (``will_change``):
    # removed pixels red, widened green, returned to their uncleaned colour blue,
    # transparent elsewhere.
    pixels: str
    changes: str
    removed_pixels: int
    widened_pixels: int
    returned_pixels: int
    elapsed_ms: float


class CleanupRegionsState(BaseModel):
    """Whether the preview thread holds, or is building now, the whole-raster region
    measurements a cleanup preview of the revision cleans with."""

    measured: bool
    measuring: bool


REMOVED_RGBA = (230, 40, 40, 255)
WIDENED_RGBA = (40, 200, 70, 255)
RETURNED_RGBA = (50, 110, 240, 255)


def _png_url(array: np.ndarray, mode: str) -> str:
    buffer = io.BytesIO()
    Image.fromarray(array, mode).save(buffer, format="PNG", compress_level=1)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class _Lru:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: OrderedDict[Hashable, object] = OrderedDict()

    def get(self, key: Hashable, build: Callable[[], object]) -> tuple[object, bool]:
        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key], True
        # Evict before building, so the cache never holds capacity + 1 entries.
        while len(self._items) >= self.capacity:
            self._items.popitem(last=False)
        value = build()
        self._items[key] = value
        return value, False

    def keys(self) -> list[Hashable]:
        return list(self._items)

    def clear(self) -> None:
        self._items.clear()


def _warm_segmentation() -> PreviewSegmentation:
    """A small three-colour segmentation whose trace compiles every Numba kernel
    of the boundary walk, corner analysis and curve fitter."""
    size = 48
    yy, xx = np.mgrid[0:size, 0:size]
    labels = np.zeros((size, size), dtype=np.int16)
    labels[(xx - 20) ** 2 + (yy - 22) ** 2 < 150] = 1
    labels[8:20, 28:42] = 2
    costs = np.ones((size, size, 3), dtype=np.float16)
    for index in range(3):
        costs[..., index][labels == index] = 0
    return PreviewSegmentation(labels=labels,
                               protected_features=np.zeros_like(labels, dtype=bool),
                               palette_rgb=np.array([[0, 0, 0], [200, 30, 30], [30, 30, 200]],
                                                    dtype=np.uint8),
                               palette_costs=costs)


class TracePreviewService:
    """Runs trace previews on one warm background thread with LRU caches."""

    def __init__(self, store: Store, *, clock: Callable[[], float] = time.monotonic,
                 idle_seconds: float = SEGMENTATION_IDLE_SECONDS) -> None:
        self.store = store
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._executor: ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()
        # (segmentation artifact id, loaded segmentation), or None. Only the preview
        # thread reads or writes the caches below.
        self._segmentation: tuple[str, PreviewSegmentation] | None = None
        # (segmentation artifact id, uncleaned raster with its region measurements),
        # or None; never resident together with ``_segmentation``.
        self._cleanup_source: tuple[str, CleanupSource] | None = None
        # The segmentation artifact id whose regions the preview thread is measuring
        # now, or None. Written only by the preview thread; read by ``regions_state``
        # on the event loop.
        self._measuring: str | None = None
        self._boundaries = _Lru(BOUNDARY_CACHE_SIZE)
        self._palettes = _Lru(8)
        self._last_used = clock()
        self._release_timer: threading.Timer | None = None
        self.warm_future: Future | None = None
        # Set once by shutdown: every checkpoint of a running preview or warm-up then
        # raises, and no new work is accepted.
        self._closed = threading.Event()

    def _pool(self) -> ThreadPoolExecutor:
        with self._executor_lock:
            if self._closed.is_set():
                raise PreviewShutDown(SHUTTING_DOWN_MESSAGE)
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="trace-preview")
            return self._executor

    def warm_up(self) -> Future:
        """Compile the tracer's Numba kernels on the preview thread, off the event loop."""
        self.warm_future = self._pool().submit(self._warm)
        return self.warm_future

    def _closing(self) -> None:
        if self._closed.is_set():
            raise PreviewShutDown(SHUTTING_DOWN_MESSAGE)

    def _warm(self) -> None:
        segmentation = _warm_segmentation()
        palette = [{"index": index, "name": f"Warm {index}", "hex": "#000000"}
                   for index in range(3)]
        crop = Window(0, 0, segmentation.width, segmentation.height)
        boundaries = window_boundaries(segmentation, [0, 1, 2], crop, self._closing)
        render_window_svg(boundaries, Window(8, 8, 32, 32), [0, 1, 2], palette,
                          preview_trace_settings({}), 0.0, self._closing)
        # The cleanup preview's widening kernel.
        source = CleanupSource(labels=segmentation.labels,
                               explicit=np.zeros_like(segmentation.labels, dtype=bool),
                               palette_rgb=segmentation.palette_rgb)
        clean_window(source, Window(8, 8, 32, 32),
                     [{**row, "rgb": rgb.tolist()} for row, rgb in zip(palette, source.palette_rgb)],
                     CleanupSettings(cleanup=True, cleanup_action="widen", min_detail_width=6.0),
                     None, self._closing)

    def shutdown(self) -> None:
        """Stop taking previews and stop the running one (or the warm-up) at its next
        checkpoint, so shutdown never waits out a trace."""
        self._closed.set()
        with self._executor_lock:
            executor, self._executor = self._executor, None
            timer, self._release_timer = self._release_timer, None
        if timer is not None:
            timer.cancel()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def _segmentation_for(self, artifact_id: str,
                          load: Callable[[], PreviewSegmentation]) -> PreviewSegmentation:
        """The loaded segmentation of ``artifact_id``. Another one is released before
        loading, so at most one cost field is ever resident."""
        if self._segmentation is not None and self._segmentation[0] == artifact_id:
            return self._segmentation[1]
        self._segmentation = None
        self._cleanup_source = None
        self._boundaries.clear()
        segmentation = load()
        self._segmentation = (artifact_id, segmentation)
        return segmentation

    def _cleanup_source_for(self, artifact_id: str,
                            load: Callable[[], CleanupSource]) -> CleanupSource:
        """The loaded uncleaned raster of ``artifact_id``; anything else resident (the
        traced segmentation included) is released before loading."""
        if self._cleanup_source is not None and self._cleanup_source[0] == artifact_id:
            return self._cleanup_source[1]
        self._cleanup_source = None
        self._segmentation = None
        self._boundaries.clear()
        source = load()
        self._cleanup_source = (artifact_id, source)
        return source

    def _measure(self, artifact_id: str, source: CleanupSource,
                 checkpoint: Callable[[], None]) -> None:
        """Build ``source``'s whole-raster region measurements, if not already held,
        reporting the measuring through ``regions_state`` while it runs."""
        if source.measured:
            return
        self._measuring = artifact_id
        try:
            source.regions(checkpoint)
        finally:
            self._measuring = None

    def regions_state(self, revision_id: str) -> dict:
        """Whether the preview thread holds the region measurements a cleanup preview
        of the revision cleans with, and whether it is measuring them now. It reads
        the thread's state without queueing behind the preview it reports on."""
        try:
            self.store.get_revision(revision_id)
        except KeyError as exc:
            raise PreviewNotFound(revision_id) from exc
        segmentation = self.store.effective_artifacts(
            revision_id, before_cleanup=True).get("segmentation")
        if segmentation is None:
            return {"measured": False, "measuring": False}
        resident = self._cleanup_source
        return {"measured": (resident is not None and resident[0] == segmentation["id"]
                             and resident[1].measured),
                "measuring": self._measuring == segmentation["id"]}

    def release_if_idle(self) -> bool:
        """Drop the loaded segmentation and derived caches when no preview has run
        for the idle period; returns whether anything was released."""
        if self._clock() - self._last_used < self._idle_seconds:
            return False
        released = (self._segmentation is not None or self._cleanup_source is not None
                    or bool(self._boundaries.keys()))
        self._segmentation = None
        self._cleanup_source = None
        self._boundaries.clear()
        self._palettes.clear()
        return released

    def _schedule_release(self) -> None:
        # The check runs on the preview thread itself, so it never races a preview.
        def submit() -> None:
            with self._executor_lock:
                executor = self._executor
            if executor is not None:
                try:
                    executor.submit(self.release_if_idle)
                except RuntimeError:  # shut down meanwhile
                    pass

        timer = threading.Timer(self._idle_seconds, submit)
        timer.daemon = True
        with self._executor_lock:
            previous, self._release_timer = self._release_timer, timer
            if self._executor is None:
                return
        if previous is not None:
            previous.cancel()
        timer.start()

    async def preview(self, revision_id: str, request: TracePreviewRequest,
                      cancelled: threading.Event) -> dict:
        return await self._on_thread(self.run, revision_id, request, cancelled)

    async def cleanup_preview(self, revision_id: str, request: CleanupPreviewRequest,
                              cancelled: threading.Event) -> dict:
        return await self._on_thread(self.run_cleanup, revision_id, request, cancelled)

    async def _on_thread(self, work: Callable, revision_id: str, request: BaseModel,
                         cancelled: threading.Event) -> dict:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._pool(), work, revision_id, request, cancelled.is_set)
        except RuntimeError as exc:
            # Shutdown raced this request: the pool closed between lookup and submit.
            if "shutdown" in str(exc) or self._closed.is_set():
                raise PreviewShutDown(SHUTTING_DOWN_MESSAGE) from exc
            raise
        except asyncio.CancelledError:
            # Shutdown dropped this request while it waited in the queue.
            if self._closed.is_set():
                raise PreviewShutDown(SHUTTING_DOWN_MESSAGE) from None
            raise

    def _stored_palette(self, artifact: dict) -> dict:
        """The revision's palette.json, checked and with every colour named as a trace
        run names it; a fault here is the server's data."""
        def load() -> dict:
            try:
                report = json.loads(self.store.artifact_path(artifact["id"]).read_text())
                rows = report["palette"]
                if not isinstance(rows, list) or not rows:
                    raise ValueError("palette has no colours")
                for row in rows:
                    rgb = row["rgb"]
                    if (not isinstance(rgb, list) or len(rgb) != 3
                            or any(isinstance(value, bool) or not isinstance(value, int)
                                   or not 0 <= value <= 255 for value in rgb)):
                        raise ValueError(f"colour {rgb!r} is not three integers in 0..255")
                ensure_palette_names(report, {})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise PreviewDataFault(f"the revision's palette.json is unreadable: {exc}") from exc
            return report

        report, _ = self._palettes.get(artifact["id"], load)
        return report

    def _palette(self, artifact: dict, colour_names: dict[str, str]) -> list[dict]:
        named = copy.deepcopy(self._stored_palette(artifact))
        try:
            ensure_palette_names(named, colour_names)
        except ValueError as exc:  # the request's renames collide
            raise PreviewRejected(str(exc)) from exc
        return named["palette"]

    def run(self, revision_id: str, request: TracePreviewRequest,
            cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Trace ``request.window`` of the revision a trace run would start from."""
        return self._guarded(self._run, revision_id, request, cancelled)

    def run_cleanup(self, revision_id: str, request: CleanupPreviewRequest,
                    cancelled: Callable[[], bool] = lambda: False) -> dict:
        """Clean ``request.window`` of the raster a Cleanup run of the revision cleans."""
        return self._guarded(self._run_cleanup, revision_id, request, cancelled)

    def _guarded(self, work: Callable, revision_id: str, request: BaseModel,
                 cancelled: Callable[[], bool]) -> dict:
        def checkpoint() -> None:
            if self._closed.is_set():
                raise PreviewShutDown(SHUTTING_DOWN_MESSAGE)
            if cancelled():
                raise PreviewCancelled

        # A request superseded while it waited in the queue exits before any work.
        checkpoint()
        try:
            return work(revision_id, request, checkpoint)
        finally:
            self._last_used = self._clock()
            self._schedule_release()

    def _run_cleanup(self, revision_id: str, request: CleanupPreviewRequest,
                     checkpoint: Callable[[], None]) -> dict:
        started = time.perf_counter()
        try:
            self.store.get_revision(revision_id)
        except KeyError as exc:
            raise PreviewNotFound(revision_id) from exc
        try:
            _require_fresh(self.store, revision_id, {"palette"})
        except ValueError as exc:
            raise PreviewUnavailable(str(exc)) from exc
        # A Cleanup run cleans the raster from before any earlier cleanup; the changes
        # are judged against the raster shown now, the active cleanup's if any.
        artifacts = self.store.effective_artifacts(revision_id, before_cleanup=True)
        if "segmentation" not in artifacts or "palette" not in artifacts:
            raise PreviewUnavailable("the revision has no flattened artwork to clean up yet")
        shown_id = self.store.effective_artifacts(revision_id)["segmentation"]["id"]
        palette = self._stored_palette(artifacts["palette"])["palette"]
        settings = CleanupSettings(**request.cleanup.model_dump())
        segmentation_id = artifacts["segmentation"]["id"]
        source = self._cleanup_source_for(
            segmentation_id,
            lambda: CleanupSource.load(self.store.artifact_path(segmentation_id), checkpoint))
        checkpoint()
        expected_rgb = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
        if not np.array_equal(source.palette_rgb, expected_rgb):
            raise PreviewUnavailable("segmentation palette does not match palette.json")
        width, height = source.width, source.height
        window_request = request.window
        try:
            window = clamp_window(window_request.x, window_request.y, window_request.width,
                                  window_request.height, width, height)
        except ValueError as exc:
            raise PreviewRejected(str(exc)) from exc
        if window.width * window.height > CLEANUP_PREVIEW_MAX_AREA_PX:
            raise PreviewRejected(
                f"zoom in to preview: the window covers {window.width} x {window.height} px, "
                f"more than the {CLEANUP_PREVIEW_MAX_AREA_PX} px preview limit")
        try:
            omitted = resolve_colour(settings.remove_colour, palette)
        except ValueError as exc:
            raise PreviewRejected(str(exc)) from exc
        if omitted is not None and len(palette) == 1:
            raise PreviewRejected("cannot omit the only palette colour")
        # The shown raster is the uncleaned one unless a cleanup changed pixels; only
        # then is a second label raster (2 bytes per pixel) held beside it. Its load
        # cannot be split, so it runs before the windowed clean rather than after.
        if shown_id == segmentation_id:
            source.release_current()
            shown = source.labels
        else:
            shown = source.current_labels(shown_id, self.store.artifact_path(shown_id))
        checkpoint()
        current = shown[window.y:window.bottom, window.x:window.right]
        if self._produced_by(revision_id, settings, omitted, width, height):
            # The shown raster is these settings' full-run result, so it is the answer
            # and nothing would change; the windowed clean would only approximate it.
            nothing = np.zeros((window.height, window.width), dtype=bool)
            removed = widened = returned = nothing
            final = current
            crop = margin_window(window, width, height, CLEANUP_PREVIEW_MARGIN_PX)
        else:
            if settings.cleanup:
                self._measure(segmentation_id, source, checkpoint)
                checkpoint()
            cleaned = clean_window(source, window, palette, settings, omitted, checkpoint)
            checkpoint()
            removed, widened, returned = will_change(cleaned.original, current,
                                                     cleaned.after_removal, cleaned.labels)
            final = cleaned.labels
            crop = cleaned.crop
        changes = np.zeros((window.height, window.width, 4), dtype=np.uint8)
        changes[removed] = REMOVED_RGBA
        changes[widened] = WIDENED_RGBA
        changes[returned] = RETURNED_RGBA
        return {"window": window.as_dict(), "crop": crop.as_dict(),
                "image": {"width": width, "height": height},
                "pixels": _png_url(source.palette_rgb[final], "RGB"),
                "changes": _png_url(changes, "RGBA"),
                "removed_pixels": int(removed.sum()),
                "widened_pixels": int(widened.sum()),
                "returned_pixels": int(returned.sum()),
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)}

    def _produced_by(self, revision_id: str, settings: CleanupSettings, omitted: int | None,
                     width: int, height: int) -> bool:
        """Whether the raster shown for the revision is the result of a Cleanup run
        with these settings under the current cleanup rules: its active cleanup (not
        stale) recorded ``CLEANUP_ALGORITHM_VERSION`` (a run recorded without a
        version ran under version 1), the same action and thresholds, the same
        physical size once resolved (to a relative 1e-9, so a size given by the other
        dimension still matches) and removed the same palette colour. The recorded
        colour is the palette index that run removed (``cleanup_removed_index``, never
        its name read against a later palette), ``omitted`` resolved against the
        palette this preview cleans with, so a rename in between (a trace's colour
        names) is never mistaken for a match. Any doubt answers no, and the preview
        runs the clean."""
        cleanup_revision, stale = nearest_cleanup(self.store, revision_id)
        if cleanup_revision is None or stale or cleanup_revision["parent_revision_id"] is None:
            return False
        if (cleanup_revision["settings"].get("cleanup_algorithm_version", 1)
                != CLEANUP_ALGORITHM_VERSION):
            return False
        try:
            recorded = CleanupSettings(**cleanup_revision["settings"]["cleanup"])
            recorded_omitted = cleanup_removed_index(self.store, cleanup_revision)
        except (KeyError, TypeError, ValueError, PreviewDataFault):
            return False
        if recorded_omitted != omitted:
            return False
        ours = resolve_physical_size(settings, width, height)
        theirs = resolve_physical_size(recorded, width, height)
        for mine, other in ((ours.width_mm, theirs.width_mm), (ours.height_mm, theirs.height_mm)):
            if (mine is None) != (other is None) or (
                    mine is not None and not math.isclose(mine, other, rel_tol=1e-9)):
                return False

        def thresholds(values: CleanupSettings) -> CleanupSettings:
            return replace(values, width_mm=None, height_mm=None, remove_colour=None)

        return thresholds(ours) == thresholds(theirs)

    def _run(self, revision_id: str, request: TracePreviewRequest,
             checkpoint: Callable[[], None]) -> dict:
        started = time.perf_counter()
        try:
            self.store.get_revision(revision_id)
        except KeyError as exc:
            raise PreviewNotFound(revision_id) from exc
        try:
            _require_fresh(self.store, revision_id, {"palette"})
        except ValueError as exc:
            raise PreviewUnavailable(str(exc)) from exc
        cleanup_revision, cleanup_stale = nearest_cleanup(self.store, revision_id)
        if cleanup_stale:
            raise PreviewUnavailable(STALE_CLEANUP_MESSAGE)
        artifacts = self.store.effective_artifacts(revision_id)
        if "segmentation" not in artifacts or "palette" not in artifacts:
            raise PreviewUnavailable("the revision has no flattened artwork to trace yet")
        try:
            settings = preview_trace_settings(request.trace.model_dump())
        except ValueError as exc:
            raise PreviewRejected(str(exc)) from exc
        palette = self._palette(artifacts["palette"], request.colour_names)
        segmentation_id = artifacts["segmentation"]["id"]
        segmentation = self._segmentation_for(
            segmentation_id,
            lambda: PreviewSegmentation.load(self.store.artifact_path(segmentation_id),
                                             checkpoint))
        checkpoint()
        expected_rgb = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
        if not np.array_equal(segmentation.palette_rgb, expected_rgb):
            raise PreviewUnavailable("segmentation palette does not match palette.json")
        width, height = segmentation.width, segmentation.height
        window_request = request.window
        try:
            window = clamp_window(window_request.x, window_request.y, window_request.width,
                                  window_request.height, width, height)
        except ValueError as exc:
            raise PreviewRejected(str(exc)) from exc
        if window.width * window.height > PREVIEW_MAX_AREA_PX:
            raise PreviewRejected(
                f"zoom in to preview: the window covers {window.width} x {window.height} px, "
                f"more than the {PREVIEW_MAX_AREA_PX} px preview limit")
        cleanup = CleanupSettings(**(cleanup_revision["settings"]["cleanup"]
                                     if cleanup_revision is not None else {}))
        # The colour the cleanup removed, by palette index: the request's renames (and
        # a trace run's, in the stored palette) may since have given its name to
        # another colour. A record that identifies no colour is the server's data.
        try:
            omitted = (cleanup_removed_index(self.store, cleanup_revision)
                       if cleanup_revision is not None else None)
            if omitted is not None and omitted >= len(palette):
                raise ValueError(f"index {omitted} is not a palette colour")
        except (KeyError, ValueError) as exc:
            raise PreviewDataFault(f"the active cleanup's removed colour: {exc}") from exc
        included = [index for index in range(len(palette)) if index != omitted]
        try:
            order, _ = apply_order_override(
                segmentation.automatic_order(included, palette, settings), settings.layer_order,
                palette)
        except ValueError as exc:
            raise PreviewRejected(str(exc)) from exc
        if any(index not in included for index in order):
            raise PreviewRejected("omitted colour cannot occur in layer order")
        overlap_px = settings.overlap / pixel_scale(
            resolve_physical_size(cleanup, width, height), width)
        crop = margin_window(window, width, height, PREVIEW_MARGIN_PX)
        boundaries, _ = self._boundaries.get(
            (segmentation_id, tuple(included), crop),
            lambda: window_boundaries(segmentation, included, crop, checkpoint))
        assert isinstance(boundaries, WindowBoundaries)
        checkpoint()
        svg = render_window_svg(boundaries, window, order, palette, settings, overlap_px,
                                checkpoint)
        return {"window": window.as_dict(), "crop": crop.as_dict(),
                "image": {"width": width, "height": height},
                "variant": f"traced-{preview_variant(settings)}", "svg": svg,
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1)}


def trace_preview_router(service: TracePreviewService) -> APIRouter:
    router = APIRouter()

    async def _serve(request: Request, preview: Callable, revision_id: str,
                     payload: BaseModel) -> dict:
        cancelled = threading.Event()

        async def watch_disconnect() -> None:
            # A client that aborted (a newer view or setting superseded it) stops
            # the work at its next phase boundary instead of working for nobody.
            while not cancelled.is_set():
                if await request.is_disconnected():
                    cancelled.set()
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            return await preview(revision_id, payload, cancelled)
        except PreviewNotFound as exc:
            raise HTTPException(404, f"Resource not found: {exc.args[0]}") from exc
        except PreviewShutDown as exc:
            raise HTTPException(503, SHUTTING_DOWN_MESSAGE) from exc
        except PreviewCancelled as exc:
            raise HTTPException(409, "preview superseded by a newer request") from exc
        except PreviewUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc
        except PreviewRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # a tracing or cleanup fault, reported rather than hidden
            raise HTTPException(500, f"preview failed: {exc}") from exc
        finally:
            cancelled.set()
            watcher.cancel()

    @router.post("/api/revisions/{revision_id}/trace-preview", response_model=TracePreview)
    async def trace_preview(revision_id: str, payload: TracePreviewRequest,
                            request: Request) -> dict:
        return await _serve(request, service.preview, revision_id, payload)

    @router.post("/api/revisions/{revision_id}/cleanup-preview", response_model=CleanupPreview)
    async def cleanup_preview(revision_id: str, payload: CleanupPreviewRequest,
                              request: Request) -> dict:
        return await _serve(request, service.cleanup_preview, revision_id, payload)

    @router.get("/api/revisions/{revision_id}/cleanup-preview/regions",
                response_model=CleanupRegionsState)
    async def cleanup_preview_regions(revision_id: str) -> dict:
        try:
            return service.regions_state(revision_id)
        except PreviewNotFound as exc:
            raise HTTPException(404, f"Resource not found: {exc.args[0]}") from exc

    return router
