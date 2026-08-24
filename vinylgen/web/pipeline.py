from __future__ import annotations

import json
import mimetypes
import shutil
import subprocess
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from vinylgen.contracts import PaletteSettings, TraceSettings
from vinylgen.palette import infer_palette
from vinylgen.tracing import trace_subject
from vinylgen.validation import validate_svg

from .store import Store


Progress = Callable[[float, str], None]

PALETTE_FILES = {
    "source": "source.png", "flattened": "flattened.png",
    "segmentation": "segmentation.npz", "palette": "palette.json", "report": "report.png",
}
TRACE_FILES = {
    "palette": "palette.json",
    "traced-overlap": "traced-overlap.svg", "traced-edge": "traced-edge.svg",
    "traced-preview": "traced.png", "trace-audit": "trace.json",
    "trace-fallbacks": "trace-fallbacks.png",
    "trace-semantic-warnings": "trace-semantic-warnings.png", "trace-inspection": "trace.html",
}
MEDIA_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json",
               ".npz": "application/octet-stream", ".html": "text/html", ".zip": "application/zip"}


def _provenance(stage: str, palette: dict | None, trace: dict | None) -> dict:
    stale_after = {
        "palette": ["trace", "validation", "export"],
        "trace": ["export"],
        "validate": ["export"],
        "export": [],
    }
    return {
        "pipeline_stage": stage,
        "palette": palette,
        "trace": trace,
        "stale_concerns": stale_after[stage],
    }


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


def _require_fresh(store: Store, revision_id: str, concerns: set[str]) -> None:
    stale = set(store.get_revision(revision_id)["settings"].get("stale_concerns", ()))
    blocked = sorted(stale & concerns)
    if blocked:
        raise ValueError(
            "revision has stale prerequisite concerns: " + ", ".join(blocked)
        )


class PipelineRunner:
    def __init__(self, store: Store, scratch_root: Path | None = None):
        self.store = store
        self.scratch_root = scratch_root or Path(__file__).resolve().parents[2] / ".tmp" / "web-jobs"

    def run(self, job_id: str, kind: str, project_id: str, revision_id: str,
            *, palette: dict | None, trace: dict | None, colour_names: dict[str, str],
            progress: Progress) -> str:
        work = self.scratch_root / job_id
        work.mkdir(parents=True, exist_ok=False)
        try:
            if kind == "palette":
                return self._palette(project_id, revision_id, work, palette or {}, progress)
            if kind == "trace":
                return self._trace(project_id, revision_id, work, trace or {}, colour_names, progress)
            if kind == "validate":
                return self._validate(project_id, revision_id, work, progress)
            if kind == "export":
                return self._export(project_id, revision_id, work, progress)
            raise ValueError(f"unsupported pipeline job kind: {kind}")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _copy_inputs(self, revision_id: str, target: Path, required: set[str]) -> dict[str, dict]:
        artifacts = self.store.effective_artifacts(revision_id)
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
                 progress: Progress) -> str:
        project = self.store.get_project(project_id)
        artifacts = self._copy_inputs(parent_id, work, {"source"})
        source_row = artifacts["source"]
        source = work / source_row["filename"]
        staged_source = work / f"{project['slug']}{source.suffix.lower()}"
        shutil.copyfile(source, staged_source)
        effective = asdict(PaletteSettings(**values))
        infer_palette(staged_source, work / "outputs", PaletteSettings(**effective),
                      progress=lambda fraction, message: progress(0.15 + fraction * 0.70, message))
        subject = work / "outputs" / project["slug"]
        progress(0.85, "Storing palette artifacts")
        files = {kind: subject / filename for kind, filename in PALETTE_FILES.items()}
        return self._commit(project_id, parent_id, "palette", files,
                            _provenance("palette", effective, None))

    def _trace(self, project_id: str, parent_id: str, work: Path, values: dict,
               colour_names: dict[str, str], progress: Progress) -> str:
        _require_fresh(self.store, parent_id, {"palette"})
        subject = work / self.store.get_project(project_id)["slug"]
        required = set(PALETTE_FILES)
        self._copy_inputs(parent_id, subject, required)
        trace_settings = TraceSettings(**values)
        progress(0.1, "Tracing shared boundaries")
        trace_subject(subject, trace_settings, colour_names)
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
        project = self.store.get_project(project_id)
        archive = work / f"{project['slug']}-vinylgen.zip"
        progress(0.3, "Packaging immutable artifacts")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for row in sorted(artifacts.values(), key=lambda item: (item["kind"], item["filename"])):
                output.write(self.store.artifact_path(row["id"]), arcname=row["filename"])
        palette_settings, trace_settings = _effective_settings(self.store, parent_id)
        return self._commit(project_id, parent_id, "export", {"export": archive},
                            _provenance("export", palette_settings, trace_settings))
