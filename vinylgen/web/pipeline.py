from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from vinylgen.contracts import CleanupSettings, PaletteSettings, TraceSettings
from vinylgen.validation import validate_svg

from .cleanup import completion_message
from .store import Store


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
                cleanup: dict | None = None) -> dict:
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
    return provenance


STALE_CLEANUP_MESSAGE = "the artwork changed after Cleanup: re-run Cleanup before tracing"


def nearest_cleanup(store: Store, revision_id: str) -> tuple[dict | None, bool]:
    """The nearest ``cleanup`` ancestor of ``revision_id`` (itself included) and
    whether it is stale: a revision in between (a flatten, a label edit, a source
    upscale) marked cleanup stale. ``(None, False)`` when there is no cleanup."""
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
        # stacking on it.
        self._copy_inputs(parent_id, subject, {"segmentation", "palette"}, before_cleanup=True)
        cleanup_settings = CleanupSettings(**values)
        effective = asdict(cleanup_settings)
        progress(0.05, "Cleaning up cutter geometry")
        run_worker(
            {"op": "cleanup", "subject": str(subject), "settings": effective},
            on_progress=lambda fraction, message: progress(0.05 + fraction * 0.85, message),
            cancelled=cancelled)
        progress(0.9, "Storing cleaned artifacts")
        # A run that changed no pixel (cleanup off, or nothing under the thresholds)
        # records only the audit and palette.json with its raster's shares; its
        # effective raster is the uncleaned one, because a cleanup replaces the raster
        # of any earlier cleanup (Store.effective_artifacts).
        changed = json.loads((subject / "cleanup.json").read_text())["changed_pixels"]
        kinds = CLEANUP_FILES if changed else {
            "palette": "palette.json", "cleanup-audit": "cleanup.json"}
        files = {kind: subject / filename for kind, filename in kinds.items()}
        palette_settings, _ = _effective_settings(self.store, parent_id)
        return self._commit(project_id, parent_id, "cleanup", files,
                            _provenance("cleanup", palette_settings, None, effective))

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
        progress(0.1, "Tracing shared boundaries")
        run_worker(
            {"op": "trace", "subject": str(subject),
             "settings": asdict(trace_settings), "colour_names": colour_names,
             "cleanup": cleanup_settings},
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
            packaged = [row for row in artifacts.values() if row["kind"] != "export"]
            for row in sorted(packaged, key=lambda item: (item["kind"], item["filename"])):
                output.write(self.store.artifact_path(row["id"]), arcname=row["filename"])
        palette_settings, trace_settings = _effective_settings(self.store, parent_id)
        return self._commit(project_id, parent_id, "export", {"export": archive},
                            _provenance("export", palette_settings, trace_settings))
