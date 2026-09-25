from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from vinylgen.contracts import CleanupSettings, PaletteSettings, TraceSettings, upgrade_trace_record
from vinylgen.cutter_cleanup import CLEANUP_ALGORITHM_VERSION
from vinylgen.trace_lengths import is_pixel_record, length_reference, pixel_record_in_units
from vinylgen.tracing import ensure_palette_names, resolve_colour
from vinylgen.validation import validate_svg

from .cleanup import completion_message
from .store import CLEANED_IMAGE_KINDS, Store
from .touch_ups import (CLEANED_FLATTENED_FILE, CLEANED_LABELS_FILE, TOUCH_UPS_FILE,
                        TOUCH_UPS_KIND, pending_touch_ups)


Progress = Callable[[float, str], None]
Cancelled = Callable[[], bool]

# The repository root, used as the working directory when spawning the compute worker
# so ``python -m vinylgen.web.pipeline_worker`` resolves regardless of the caller's cwd.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _terminate(process: subprocess.Popen) -> None:
    """Stop a worker's whole process group, escalating to SIGKILL if it lingers."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_worker(spec: dict, *, on_progress: Progress, cancelled: Cancelled) -> None:
    """Run one compute step in a killable subprocess, relaying its progress.

    Cancellation is checked on every poll rather than only when the worker emits a
    line, so a job wedged inside a long numpy call is killed within one poll interval
    instead of waiting for the next progress checkpoint.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "vinylgen.web.pipeline_worker"],
        cwd=REPO_ROOT, text=True, bufsize=1,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    events: queue.Queue[tuple[str, str | None]] = queue.Queue()

    def reader(stream, tag: str) -> None:
        for line in stream:
            events.put((tag, line))
        events.put((tag, None))

    threads = [threading.Thread(target=reader, args=(process.stdout, "out"), daemon=True),
               threading.Thread(target=reader, args=(process.stderr, "err"), daemon=True)]
    for thread in threads:
        thread.start()
    error: str | None = None
    stderr_tail: list[str] = []
    closed = 0
    try:
        process.stdin.write(json.dumps(spec))
        process.stdin.close()
        while closed < 2 or process.poll() is None:
            if cancelled():
                _terminate(process)
                raise asyncio.CancelledError
            try:
                tag, line = events.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                closed += 1
                continue
            if tag == "err":
                stderr_tail.append(line)
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "progress":
                on_progress(float(event["value"]), str(event["message"]))
            elif event.get("type") == "error":
                error = event.get("message") or "pipeline worker failed"
        process.wait()
        if cancelled():
            _terminate(process)
            raise asyncio.CancelledError
        if error is not None:
            raise RuntimeError(error)
        if process.returncode:
            raise RuntimeError("".join(stderr_tail).strip()
                               or f"pipeline worker exited with status {process.returncode}")
    finally:
        if process.poll() is None:
            _terminate(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except Exception:  # pragma: no cover - defensive cleanup
                pass

PALETTE_FILES = {
    "source": "source.png", "flattened": "flattened.png",
    "segmentation": "segmentation.npz", "palette": "palette.json", "report": "report.png",
    # Written only when the run explicitly protected a pixel (pipeline_worker).
    "protected-areas-overlay": "protected-areas-overlay.png",
}
# The flatten outputs a trace reads: every palette file but the optional overlay.
TRACE_INPUT_KINDS = set(PALETTE_FILES) - {"protected-areas-overlay"}
CLEANUP_FILES = {
    "flattened": "flattened.png", "segmentation": "segmentation.npz",
    "palette": "palette.json", "report": "report.png", "cleanup-audit": "cleanup.json",
    # Written only when the run changed a pixel (web.cleanup.write_changes_overlay).
    "cleanup-changes-overlay": "cleanup-changes-overlay.png",
    # Written only when the run replayed a touch-up (web.touch_ups).
    TOUCH_UPS_KIND: TOUCH_UPS_FILE,
    # Written only when replayed touch-ups changed the raster: the cleanup's own result
    # before them (store.CLEANED_IMAGE_KINDS).
    "cleaned-flattened": CLEANED_FLATTENED_FILE, "cleaned-labels": CLEANED_LABELS_FILE,
}
TRACE_FILES = {
    "palette": "palette.json",
    "traced-overlap": "traced-overlap.svg", "traced-edge": "traced-edge.svg",
    "traced-preview": "traced.png", "trace-audit": "trace.json",
    "trace-fallbacks": "trace-fallbacks.png",
    "trace-semantic-warnings": "trace-semantic-warnings.png", "trace-inspection": "trace.html",
    "trace-fallbacks-overlay": "trace-fallbacks-overlay.png",
    "trace-missed-details-overlay": "trace-missed-details-overlay.png",
}
MEDIA_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
               ".npz": "application/octet-stream", ".html": "text/html", ".zip": "application/zip"}


def _provenance(stage: str, palette: dict | None, trace: dict | None,
                cleanup: dict | None = None, removed_colour: dict | None = None) -> dict:
    stale_after = {
        "palette": ["cleanup", "trace", "validation", "export"],
        "cleanup": ["trace", "validation", "export"],
        # A new trace run needs its own validation: an older run's never carries over.
        "trace": ["validation", "export"],
        "validate": ["export"],
        "export": [],
    }
    provenance = {
        "pipeline_stage": stage,
        "palette": palette,
        "trace": trace,
        "stale_concerns": stale_after[stage],
    }
    # Only a cleanup revision records cleanup settings; a trace reads them from its
    # nearest active cleanup ancestor (``active_cleanup_settings``).
    if cleanup is not None:
        provenance["cleanup"] = cleanup
        provenance["cleanup_algorithm_version"] = CLEANUP_ALGORITHM_VERSION
        # The colour ``cleanup["remove_colour"]`` named when the run resolved it, as its
        # palette index (``cleanup_removed_index``) and hex (``cleanup_removed_hex``);
        # both ``None`` when none was removed.
        provenance["removed_colour_index"] = (removed_colour or {}).get("index")
        provenance["removed_colour_hex"] = (removed_colour or {}).get("hex")
    return provenance


STALE_CLEANUP_MESSAGE = "the artwork changed after Cleanup: re-run Cleanup before tracing"


def nearest_cleanup(store: Store, revision_id: str) -> tuple[dict | None, bool]:
    """The nearest ``cleanup`` ancestor of ``revision_id`` (itself included) and
    whether it is stale: a revision in between (a flatten, a merge or another label
    edit made without a current cleanup, a source upscale) marked cleanup stale.
    ``(None, False)`` when there is no cleanup."""
    stale = False
    current: str | None = revision_id
    seen: set[str] = set()
    while current:
        if current in seen:
            raise ValueError("revision ancestry contains a cycle")
        seen.add(current)
        revision = store.get_revision(current)
        settings = revision["settings"]
        if revision["kind"] == "cleanup" and settings.get("cleanup") is not None:
            return revision, stale
        if "cleanup" in settings.get("stale_concerns", ()):
            stale = True
        current = revision["parent_revision_id"]
    return None, False


def cleanup_removed_index(store: Store, cleanup_revision: dict) -> int | None:
    """The palette index of the colour ``cleanup_revision`` removed; ``None`` when it
    removed none.

    A colour keeps its palette index from the flatten on, but not its name: a trace's
    colour names rename colours in the palette.json that trace writes, so resolving
    ``remove_colour`` (a name or hex) against a later palette could pick a different
    colour than the cleanup removed. A cleanup records the index it resolved
    (``removed_colour_index``). One recorded without it has ``remove_colour`` resolved
    against the palette it ran with: its own palette.json (the palette it cleaned,
    names included), else, with no such artifact, the palette it started from. Raises
    ``ValueError`` when the record does not identify one palette colour."""
    settings = cleanup_revision["settings"]
    if "removed_colour_index" in settings:
        index = settings["removed_colour_index"]
        if index is not None and (isinstance(index, bool) or not isinstance(index, int)
                                  or index < 0):
            raise ValueError(f"recorded removed colour index {index!r} is not a palette index")
        return index
    remove_colour = (settings.get("cleanup") or {}).get("remove_colour")
    if remove_colour is None:
        return None
    return resolve_colour(remove_colour, _cleanup_palette(store, cleanup_revision))


def cleanup_removed_hex(store: Store, cleanup_revision: dict) -> str | None:
    """The hex of the colour ``cleanup_revision`` removed, in the palette it ran with;
    ``None`` when it removed none. It holds whether or not that cleanup is stale now,
    and, unlike the recorded ``remove_colour`` name, a later rename never changes the
    colour it identifies. A cleanup records it (``removed_colour_hex``); one recorded
    without it takes the colour at ``cleanup_removed_index`` in its own palette.json.
    Raises ``ValueError`` when the record does not identify one palette colour."""
    settings = cleanup_revision["settings"]
    if "removed_colour_hex" in settings:
        return settings["removed_colour_hex"]
    removed = cleanup_removed_index(store, cleanup_revision)
    if removed is None:
        return None
    ran_with = _cleanup_palette(store, cleanup_revision)
    if removed >= len(ran_with):
        raise ValueError("the Cleanup's removed colour is not in the palette it ran with")
    return str(ran_with[removed]["hex"])


def with_removed_colour_hex(store: Store, revisions: list[dict]) -> list[dict]:
    """``revisions`` as the API reports them: each cleanup revision's settings carry
    ``removed_colour_hex`` (``cleanup_removed_hex``), which the Cleanup form inherits in
    place of the recorded name. A cleanup recorded before the hex was has it resolved
    here; one whose record identifies no colour is left without it."""
    for revision in revisions:
        settings = revision["settings"]
        if (revision["kind"] != "cleanup" or settings.get("cleanup") is None
                or "removed_colour_hex" in settings):
            continue
        try:
            settings["removed_colour_hex"] = cleanup_removed_hex(store, revision)
        except (KeyError, ValueError):
            continue
    return revisions


_VIEW_BOX = re.compile(rb'viewBox="0 0 (\d+) (\d+)"')


def traced_length_reference(store: Store, revision: dict) -> int | None:
    """The longer side (px) of the raster the trace run behind ``revision`` traced:
    its own trace revision, else the nearest trace ancestor (a validation or export
    copies that run's settings). Read from the ``viewBox`` its traced SVG opens with,
    which is that raster's pixel grid. ``None`` when no traced SVG says."""
    seen: set[str] = set()
    current: dict | None = revision
    while current is not None and current["id"] not in seen:
        seen.add(current["id"])
        if current["kind"] == "trace":
            for row in store.list_artifacts(current["id"]):
                if row["kind"] in ("traced-overlap", "traced-edge"):
                    with store.artifact_path(row["id"]).open("rb") as handle:
                        found = _VIEW_BOX.search(handle.read(4096))
                    if found:
                        return length_reference(int(found[1]), int(found[2]))
            return None
        parent = current["parent_revision_id"]
        current = store.get_revision(parent) if parent else None
    return None


def with_trace_length_units(store: Store, revisions: list[dict]) -> list[dict]:
    """``revisions`` as the API reports them: trace settings recorded in pixels (every
    run before trace lengths were thousandths of the image's longer side, which
    carries no ``length_unit``) are converted to thousandths of the raster that run
    traced, so the form resolves them back to the pixels it traced with. A record
    whose raster size cannot be read is left in pixels, marked ``length_unit: px``.
    Renamed fields (``corner_straightness``) are served under their current names."""
    for revision in revisions:
        settings = revision["settings"]
        trace = settings.get("trace")
        if not isinstance(trace, dict):
            continue
        trace = settings["trace"] = upgrade_trace_record(trace)
        if not is_pixel_record(trace):
            continue
        reference = traced_length_reference(store, revision)
        settings["trace"] = (pixel_record_in_units(trace, reference) if reference is not None
                             else {**trace, "length_unit": "px"})
    return revisions


def served_revisions(store: Store, revisions: list[dict]) -> list[dict]:
    """``revisions`` with every legacy record brought up to date for the client."""
    return with_trace_length_units(store, with_removed_colour_hex(store, revisions))


def _cleanup_palette(store: Store, cleanup_revision: dict) -> list[dict]:
    """The palette ``cleanup_revision`` ran with, names included: its own palette.json
    (the palette it cleaned), else, with no such artifact, the palette it started from.
    Raises ``ValueError`` when there is neither."""
    ran_with = next((row for row in store.list_artifacts(cleanup_revision["id"])
                     if row["kind"] == "palette"), None)
    if ran_with is None and cleanup_revision["parent_revision_id"] is not None:
        ran_with = store.effective_artifacts(cleanup_revision["parent_revision_id"],
                                             before_cleanup=True).get("palette")
    if ran_with is None:
        raise ValueError("the cleanup's removed colour has no palette to resolve against")
    report = json.loads(store.artifact_path(ran_with["id"]).read_text())
    ensure_palette_names(report, {})
    return report["palette"]


def inherited_remove_colour(store: Store, revision_id: str,
                            palette: list[dict] | None = None) -> dict | None:
    """The removed colour a Cleanup run of ``revision_id`` inherits from the nearest
    ``cleanup`` ancestor (stale or not), as a value the run resolves (``resolve_colour``,
    by name or hex) to that cleanup's colour in ``palette``: the palette the run starts
    from (``revision_id``'s from before cleanup, read when not given).

    ``None`` when there is no cleanup ancestor or it removed no colour. Otherwise
    ``{"value", "problem", "hex"}``, ``hex`` being the cleanup's (``None`` when its
    record identifies none): ``value`` is the cleanup's hex (``cleanup_removed_hex``)
    when that resolves to exactly one colour. Failing that, when the cleanup is not
    stale its palette index still addresses the same colour, so ``value`` is that
    colour's current name when that resolves to exactly it (a recolour can give two
    colours one hex, and a trace's colour names can move a name). Otherwise ``value``
    is ``None`` and ``problem`` says why: ``"missing"`` (no colour has the hex: it was
    re-flattened, recoloured or merged) or ``"ambiguous"`` (the hex matches more than one colour
    by hex or name)."""
    cleanup_revision, stale = nearest_cleanup(store, revision_id)
    if cleanup_revision is None or (cleanup_revision["settings"]["cleanup"] or {}).get(
            "remove_colour") is None:
        return None
    if palette is None:
        start = store.effective_artifacts(revision_id, before_cleanup=True).get("palette")
        if start is None:
            raise ValueError("revision is missing required artifacts: palette")
        palette = json.loads(store.artifact_path(start["id"]).read_text())["palette"]
    report = {"palette": [dict(row) for row in palette]}
    ensure_palette_names(report, {})
    palette = report["palette"]
    try:
        removed_hex = cleanup_removed_hex(store, cleanup_revision)
    except ValueError:
        removed_hex = None
    if removed_hex is not None:
        # The one colour it resolves to must be that hex's, not one named like it.
        try:
            resolved = resolve_colour(removed_hex, palette)
            if str(palette[resolved]["hex"]).casefold() == removed_hex.casefold():
                return {"value": removed_hex, "problem": None, "hex": removed_hex}
        except ValueError:
            pass
    if not stale:
        try:
            index = cleanup_removed_index(store, cleanup_revision)
        except ValueError:
            index = None
        if index is not None and index < len(palette):
            name = str(palette[index]["name"])
            try:
                if resolve_colour(name, palette) == index:
                    return {"value": name, "problem": None, "hex": removed_hex}
            except ValueError:
                pass
    missing = removed_hex is None or not any(
        str(row["hex"]).casefold() == removed_hex.casefold() for row in palette)
    return {"value": None, "problem": "missing" if missing else "ambiguous",
            "hex": removed_hex}


def rerun_cleanup_settings(store: Store, revision_id: str) -> dict:
    """The settings that re-run the last cleanup (the nearest ``cleanup`` ancestor of
    ``revision_id``, usually a stale one) on ``revision_id``: that cleanup's recorded
    settings, with its removed colour as the Cleanup form inherits it
    (``inherited_remove_colour``): its hex, or while it is not stale the current name
    of the colour at its index, whichever resolves to exactly that colour.

    The re-run starts from ``revision_id``'s palette from before cleanup, whose colour
    names a trace's colour names may have changed, and whose indices a merge may have
    shifted, so the recorded name does not identify the colour there. Raises
    ``ValueError`` when there is no cleanup to re-run, or when neither identifies
    exactly one colour of the palette the re-run starts from, checked as the cleanup
    job resolves it, rather than removing a different colour or failing the job."""
    cleanup_revision, _ = nearest_cleanup(store, revision_id)
    if cleanup_revision is None:
        raise ValueError("there is no earlier Cleanup run to re-run")
    settings = dict(cleanup_revision["settings"]["cleanup"])
    inherited = inherited_remove_colour(store, revision_id)
    if inherited is None:
        settings["remove_colour"] = None
        return settings
    if inherited["value"] is None:
        described = inherited["hex"] or "its colour"
        if inherited["problem"] == "missing":
            # A re-flatten, a recolour or a merge: its hex is gone, and a name is never
            # carried across a palette change.
            raise ValueError(
                f"the colour the last Cleanup removed ({described}) is not in this artwork's "
                "palette any more (it was re-flattened, recoloured or merged). Choose the "
                "colour to remove in the Cleanup form and run Cleanup")
        raise ValueError(
            f"the colour the last Cleanup removed ({described}) now matches more than "
            "one colour of this artwork's palette, by hex or by name. Choose the colour "
            "to remove in the Cleanup form and run Cleanup")
    settings["remove_colour"] = inherited["value"]
    return settings


def active_cleanup_settings(store: Store, revision_id: str) -> dict | None:
    """The cleanup settings a trace of ``revision_id`` uses: those of the nearest
    ``cleanup`` ancestor while it is not stale. ``None`` means trace in pixels,
    cutting every colour (or, with a stale cleanup, that tracing is blocked)."""
    revision, stale = nearest_cleanup(store, revision_id)
    return revision["settings"]["cleanup"] if revision is not None and not stale else None


def _effective_settings(store: Store, revision_id: str) -> tuple[dict | None, dict | None]:
    """Resolve configuration provenance independently of artifact inheritance."""
    palette = trace = None
    current: str | None = revision_id
    seen: set[str] = set()
    while current and (palette is None or trace is None):
        if current in seen:
            raise ValueError("revision ancestry contains a cycle")
        seen.add(current)
        revision = store.get_revision(current)
        settings = revision["settings"]
        if palette is None and settings.get("palette") is not None:
            palette = settings["palette"]
        if trace is None and settings.get("trace") is not None:
            trace = settings["trace"]
        current = revision["parent_revision_id"]
    return palette, trace


def _stale_prerequisites(concerns: list[str]) -> ValueError:
    return ValueError("revision has stale prerequisite concerns: " + ", ".join(concerns))


def _require_fresh(store: Store, revision_id: str, concerns: set[str]) -> None:
    stale = set(store.get_revision(revision_id)["settings"].get("stale_concerns", ()))
    blocked = sorted(stale & concerns)
    if blocked:
        raise _stale_prerequisites(blocked)


class PipelineRunner:
    def __init__(self, store: Store, scratch_root: Path | None = None):
        self.store = store
        self.scratch_root = scratch_root or Path(__file__).resolve().parents[2] / ".tmp" / "web-jobs"

    def run(self, job_id: str, kind: str, project_id: str, revision_id: str,
            *, palette: dict | None, trace: dict | None, colour_names: dict[str, str],
            progress: Progress, cancelled: Cancelled = lambda: False,
            cleanup: dict | None = None) -> str:
        work = self.scratch_root / job_id
        work.mkdir(parents=True, exist_ok=False)
        try:
            if kind == "palette":
                return self._palette(project_id, revision_id, work, palette or {}, progress, cancelled)
            if kind == "cleanup":
                return self._cleanup(project_id, revision_id, work, cleanup or {},
                                     progress, cancelled)
            if kind == "trace":
                return self._trace(project_id, revision_id, work, trace or {}, colour_names,
                                   progress, cancelled)
            if kind == "validate":
                return self._validate(project_id, revision_id, work, progress)
            if kind == "export":
                return self._export(project_id, revision_id, work, progress)
            raise ValueError(f"unsupported pipeline job kind: {kind}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def completion_message(self, kind: str, revision_id: str) -> str:
        """The finished job's result line; a cleanup states what it did to protected detail."""
        if kind != "cleanup":
            return "Complete"
        audit = next(row for row in self.store.list_artifacts(revision_id)
                     if row["kind"] == "cleanup-audit")
        return completion_message(json.loads(self.store.artifact_path(audit["id"]).read_text()))

    def _copy_inputs(self, revision_id: str, target: Path, required: set[str],
                     *, before_cleanup: bool = False) -> dict[str, dict]:
        artifacts = self.store.effective_artifacts(revision_id, before_cleanup=before_cleanup)
        missing = sorted(required - artifacts.keys())
        if missing:
            raise ValueError(f"revision is missing required artifacts: {', '.join(missing)}")
        target.mkdir(parents=True, exist_ok=True)
        for kind in required:
            row = artifacts[kind]
            shutil.copyfile(self.store.artifact_path(row["id"]), target / row["filename"])
        return artifacts

    def _commit(self, project_id: str, parent_id: str, stage: str, files: dict[str, Path],
                settings: dict) -> str:
        revision = self.store.create_revision(
            project_id, parent_revision_id=parent_id, kind=stage,
            message=f"Deterministic {stage} pipeline", settings=settings)
        for kind, path in files.items():
            if not path.exists():
                continue
            with path.open("rb") as source:
                self.store.add_artifact(
                    revision["id"], kind=kind, filename=path.name,
                    media_type=MEDIA_TYPES.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0]
                                               or "application/octet-stream"), source=source)
        return revision["id"]

    def _palette(self, project_id: str, parent_id: str, work: Path, values: dict,
                 progress: Progress, cancelled: Cancelled) -> str:
        project = self.store.get_project(project_id)
        artifacts = self._copy_inputs(parent_id, work, {"source"})
        source_row = artifacts["source"]
        source = work / source_row["filename"]
        staged_source = work / f"{project['slug']}{source.suffix.lower()}"
        if not (staged_source.exists() and source.samefile(staged_source)):
            shutil.copyfile(source, staged_source)
        effective = asdict(PaletteSettings(**values))
        run_worker(
            {"op": "palette", "source": str(staged_source),
             "outputs": str(work / "outputs"), "settings": effective},
            on_progress=lambda fraction, message: progress(0.15 + fraction * 0.70, message),
            cancelled=cancelled)
        subject = work / "outputs" / project["slug"]
        progress(0.85, "Storing palette artifacts")
        files = {kind: subject / filename for kind, filename in PALETTE_FILES.items()}
        return self._commit(project_id, parent_id, "palette", files,
                            _provenance("palette", effective, None))

    def _cleanup(self, project_id: str, parent_id: str, work: Path, values: dict,
                 progress: Progress, cancelled: Cancelled) -> str:
        _require_fresh(self.store, parent_id, {"palette"})
        subject = work / self.store.get_project(project_id)["slug"]
        # Always clean the raster from before any earlier cleanup, so turning cleanup
        # off, or changing the size or thresholds, replaces that cleanup instead of
        # stacking on it; the touch-ups made on the earlier cleanup's raster are
        # replayed on the result.
        self._copy_inputs(parent_id, subject, {"segmentation", "palette"}, before_cleanup=True)
        touch_ups = [touch_up.spec() for touch_up in pending_touch_ups(self.store, parent_id)]
        cleanup_settings = CleanupSettings(**values)
        effective = asdict(cleanup_settings)
        progress(0.05, "Cleaning up cutter geometry")
        run_worker(
            {"op": "cleanup", "subject": str(subject), "settings": effective,
             "touch_ups": touch_ups},
            on_progress=lambda fraction, message: progress(0.05 + fraction * 0.85, message),
            cancelled=cancelled)
        progress(0.9, "Storing cleaned artifacts")
        # A run that changed no pixel (cleanup off, or nothing under the thresholds,
        # and no touch-up replayed) records only the audit and palette.json with its
        # raster's shares; its effective raster is the uncleaned one, because a cleanup
        # replaces the raster of any earlier cleanup (Store.effective_artifacts).
        audit = json.loads((subject / "cleanup.json").read_text())
        kinds = CLEANUP_FILES if audit["raster_changed"] else {
            "palette": "palette.json", "cleanup-audit": "cleanup.json",
            TOUCH_UPS_KIND: TOUCH_UPS_FILE}
        files = {kind: subject / filename for kind, filename in kinds.items()}
        palette_settings, _ = _effective_settings(self.store, parent_id)
        settings = _provenance("cleanup", palette_settings, None, effective,
                               {"index": audit["omitted_index"],
                                "hex": (audit["omitted_colour"] or {}).get("hex")})
        # How many pixels the cleanup itself changed (never a replayed touch-up), which
        # its changes overlay shows.
        settings["changed_pixels"] = audit["changed_pixels"]
        replay = audit.get("touch_ups")
        if replay is not None:
            # The touch-ups this run replayed, recorded as touch-ups of its own raster
            # for the next run to replay (web.touch_ups), and what it skipped.
            if replay["records"]:
                settings["touch_ups"] = replay["records"]
            settings["touch_up_replay"] = {key: replay[key] for key in
                                           ("total", "replayed", "skipped",
                                            "partly_superseded", "message")}
        return self._commit(project_id, parent_id, "cleanup", files, settings)

    def _trace(self, project_id: str, parent_id: str, work: Path, values: dict,
               colour_names: dict[str, str], progress: Progress, cancelled: Cancelled) -> str:
        _require_fresh(self.store, parent_id, {"palette"})
        cleanup_revision, cleanup_stale = nearest_cleanup(self.store, parent_id)
        if cleanup_stale:
            raise ValueError(STALE_CLEANUP_MESSAGE)
        subject = work / self.store.get_project(project_id)["slug"]
        self._copy_inputs(parent_id, subject, TRACE_INPUT_KINDS)
        trace_settings = TraceSettings(**values)
        cleanup_settings = (cleanup_revision["settings"]["cleanup"]
                            if cleanup_revision is not None else None)
        # The colour the cleanup removed, by index: ``colour_names`` rename the palette
        # this trace writes, so its names may no longer identify that colour.
        removed_index = (cleanup_removed_index(self.store, cleanup_revision)
                         if cleanup_revision is not None else None)
        progress(0.1, "Tracing shared boundaries")
        run_worker(
            {"op": "trace", "subject": str(subject),
             "settings": asdict(trace_settings), "colour_names": colour_names,
             "cleanup": cleanup_settings, "removed_index": removed_index},
            on_progress=lambda fraction, message: progress(0.1 + fraction * 0.8, message),
            cancelled=cancelled)
        effective = asdict(trace_settings)
        progress(0.9, "Storing traced and validated artifacts")
        files = {kind: subject / filename for kind, filename in TRACE_FILES.items()}
        palette_settings, _ = _effective_settings(self.store, parent_id)
        return self._commit(project_id, parent_id, "trace", files,
                            _provenance("trace", palette_settings, effective))

    def _validate(self, project_id: str, parent_id: str, work: Path, progress: Progress) -> str:
        _require_fresh(self.store, parent_id, {"trace"})
        required = {"palette", "trace-audit", "traced-edge", "traced-overlap"}
        artifacts = self._copy_inputs(parent_id, work, required)
        palette_report = json.loads((work / artifacts["palette"]["filename"]).read_text())
        trace_audit = json.loads((work / artifacts["trace-audit"]["filename"]).read_text())
        palette = palette_report["palette"]
        names = trace_audit["layer_order"]["effective_bottom_to_top"]
        by_name = {row["name"]: index for index, row in enumerate(palette)}
        order = [by_name[name] for name in names]
        progress(0.3, "Validating SVG structure")
        result = {
            variant: validate_svg(work / artifacts[f"traced-{variant}"]["filename"], palette, order)
            for variant in ("edge", "overlap")
        }
        validation_path = work / "validation.json"
        validation_path.write_text(json.dumps(result, indent=2) + "\n")
        palette_settings, trace_settings = _effective_settings(self.store, parent_id)
        return self._commit(project_id, parent_id, "validate", {"validation": validation_path},
                            _provenance("validate", palette_settings, trace_settings))

    def _export(self, project_id: str, parent_id: str, work: Path, progress: Progress) -> str:
        _require_fresh(self.store, parent_id, {"trace", "validation"})
        artifacts = self.store.effective_artifacts(parent_id)
        if not {"traced-edge", "traced-overlap", "traced-preview"}.issubset(artifacts):
            raise ValueError("revision has no complete traced output to export")
        # A trace run hides every older validation (Store.effective_artifacts), so an
        # effective validation is one of the current run. Its absence is stale
        # validation even when the parent's own concerns do not say so: a revision in
        # between recorded none, or the run predates trace marking validation stale.
        if "validation" not in artifacts:
            raise _stale_prerequisites(["validation"])
        project = self.store.get_project(project_id)
        archive = work / f"{project['slug']}-vinylgen.zip"
        progress(0.3, "Packaging immutable artifacts")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            # An earlier export zip is not part of the artwork, so it is never nested.
            # Nor is the Cleaned image behind the touch-ups, a view of the working state.
            packaged = [row for row in artifacts.values()
                        if row["kind"] != "export" and row["kind"] not in CLEANED_IMAGE_KINDS]
            for row in sorted(packaged, key=lambda item: (item["kind"], item["filename"])):
                output.write(self.store.artifact_path(row["id"]), arcname=row["filename"])
        palette_settings, trace_settings = _effective_settings(self.store, parent_id)
        return self._commit(project_id, parent_id, "export", {"export": archive},
                            _provenance("export", palette_settings, trace_settings))
