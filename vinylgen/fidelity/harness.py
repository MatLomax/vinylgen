"""Run synthetic scenes through the real palette and trace stages.

Two tiers:

- ``trace_labels`` (label-map tier) writes a subject directory exactly as the
  palette stage would, from a known label map, and traces it with the real
  ``vinylgen.tracing.trace_subject``: shared boundaries, curve fitting, the
  recovery passes, the Inkscape validation render and SVG writing all run.
- ``trace_rgb`` (full-pipeline tier) writes the anti-aliased RGB render as an
  input image, runs the real palette stage (``vinylgen.palette.infer_palette``,
  the per-image entry of the palette CLI) in process, then traces the result.
  Inferred colours are matched back to the scene palette by nearest Lab colour.

Everything is written under the caller's ``work_dir`` (a ``.tmp/`` path), apart
from the trace stage's own transient validation render, which it keeps in the
repository's ``.tmp/`` and deletes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.colour import hex_colour, palette_costs, srgb_to_lab
from vinylgen.contracts import CleanupSettings, PaletteSettings, SegmentationBundle, TraceSettings
from vinylgen.palette import infer_palette
from vinylgen.tracing import trace_subject

# An inferred palette colour matches a scene colour when its nearest scene colour
# lies within this CIE76 distance. Palette inference averages the pixels of a
# colour family, so a matched colour drifts by a few units from the scene colour;
# an anti-alias blend that survived as its own colour lies roughly halfway
# between two scene colours, far beyond this.
MATCH_DELTA_E = 12.0


@dataclass
class TraceRun:
    """One traced synthetic subject.

    ``palette`` is the palette the trace ran with (``palette.json`` rows).
    ``colour_map`` maps each traced layer's fill hex to the scene palette index it
    stands for; ``measure`` reads it. In the label-map tier it is the identity. In
    the full-pipeline tier it holds only the inferred colours within
    ``MATCH_DELTA_E`` of a scene colour; ``palette_matches`` records every inferred
    colour's nearest scene colour and distance, and ``unmatched`` the inferred
    colours matching none.
    """

    subject_dir: Path
    edge_svg: Path
    overlap_svg: Path
    audit: dict
    seconds: float
    palette: list[dict]
    colour_map: dict[str, int]
    palette_seconds: float = 0.0
    trace_seconds: float = 0.0
    palette_matches: list[dict] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)


def _scene_labels(source) -> tuple[np.ndarray, list[dict]]:
    """The label map and palette of a scene, or of a ``(labels, palette)`` pair."""
    if isinstance(source, tuple):
        labels, palette = source
        return np.asarray(labels, dtype=np.int16), list(palette)
    from vinylgen.fidelity.raster import render_labels
    return np.asarray(render_labels(source), dtype=np.int16), list(source.palette)


def _palette_rows(palette: list[dict], labels: np.ndarray) -> list[dict]:
    """``palette.json`` rows (index, name, hex, rgb, share) for a scene palette."""
    counts = np.bincount(labels.ravel(), minlength=len(palette))
    rows = []
    for index, colour in enumerate(palette):
        rgb = [int(value) for value in colour["rgb"]]
        row = {"index": index, "hex": hex_colour(np.asarray(rgb)), "rgb": rgb,
               "share_percent": round(100.0 * int(counts[index]) / labels.size, 4)}
        if colour.get("name"):
            row["name"] = str(colour["name"])
        rows.append(row)
    return rows


def _trace(subject_dir: Path, settings: TraceSettings | None,
           cleanup: CleanupSettings | None) -> tuple[dict, float]:
    started = time.perf_counter()
    # Like the trace CLI, cutter cleanup in ``cleanup`` is applied to the labels first.
    audit = trace_subject(subject_dir, settings or TraceSettings(), {}, cleanup=cleanup,
                          clean_inline=True)
    return audit, time.perf_counter() - started


def trace_labels(source, work_dir: Path, settings: TraceSettings | None = None,
                 cleanup: CleanupSettings | None = None, *, subject: str = "synthetic",
                 source_rgb: np.ndarray | None = None) -> TraceRun:
    """Trace a known label map through the real trace stage.

    ``source`` is a ``Scene`` (rendered with ``raster.render_labels``) or a
    ``(labels, palette)`` pair, the palette as ``{"name", "rgb"}`` rows. The
    subject directory ``work_dir/subject`` receives what the palette stage
    writes: ``flattened.png`` (the palette RGB of the labels), ``source.png``,
    ``palette.json`` and a contract-v3 ``segmentation.npz`` with empty automatic
    and explicit protection.

    ``source.png`` and the stored Lab costs come from ``source_rgb`` when given,
    otherwise from the hard-edged render (the flattened RGB). The palette stage
    stores ``palette_costs(srgb_to_lab(source pixels), palette Lab)``; the same
    call is made here with the palette's Lab taken from its exact RGB. With the
    hard-edged render every boundary pixel costs 0 to its own colour and the
    same distance to its neighbour, so every sub-pixel crossing sits on the
    pixel-edge midpoint.
    """
    labels, palette = _scene_labels(source)
    if labels.ndim != 2:
        raise ValueError("labels must be a 2-D array")
    if labels.size and (labels.min() < 0 or labels.max() >= len(palette)):
        raise ValueError("labels reference a colour outside the palette")
    palette_rgb = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
    flattened = palette_rgb[labels]
    original = flattened if source_rgb is None else np.asarray(source_rgb, dtype=np.uint8)
    if original.shape != flattened.shape:
        raise ValueError("source_rgb must match the label map's dimensions")
    subject_dir = Path(work_dir) / subject
    subject_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(original, "RGB").save(subject_dir / "source.png")
    Image.fromarray(flattened, "RGB").save(subject_dir / "flattened.png")
    rows = _palette_rows(palette, labels)
    height, width = labels.shape
    report = {"source": str(subject_dir / "source.png"),
              "segmentation": str(subject_dir / "segmentation.npz"),
              "dimensions": [width, height], "inferred_colour_count": len(rows),
              "palette": rows, "synthetic": True}
    (subject_dir / "palette.json").write_text(json.dumps(report, indent=2) + "\n")
    costs = palette_costs(srgb_to_lab(original.reshape(-1, 3)), srgb_to_lab(palette_rgb))
    bundle = SegmentationBundle(
        labels=labels,
        protected_features=np.zeros(labels.shape, dtype=bool),
        explicit_protected_features=np.zeros(labels.shape, dtype=bool),
        palette_rgb=palette_rgb,
        palette_costs=costs.reshape((height, width, len(palette))))
    bundle.validate()
    bundle.save(subject_dir / "segmentation.npz")
    audit, seconds = _trace(subject_dir, settings, cleanup)
    traced_palette = json.loads((subject_dir / "palette.json").read_text())["palette"]
    return TraceRun(subject_dir=subject_dir, edge_svg=subject_dir / "traced-edge.svg",
                    overlap_svg=subject_dir / "traced-overlap.svg", audit=audit,
                    seconds=seconds, trace_seconds=seconds, palette=traced_palette,
                    colour_map={row["hex"]: index for index, row in enumerate(rows)})


def match_palette(inferred: list[dict], truth: list[dict]) -> list[dict]:
    """Every inferred colour's nearest truth colour by CIE76 Lab distance."""
    inferred_lab = srgb_to_lab(np.asarray([row["rgb"] for row in inferred], dtype=np.uint8))
    truth_lab = srgb_to_lab(np.asarray([row["rgb"] for row in truth], dtype=np.uint8))
    distances = np.linalg.norm(inferred_lab[:, None, :] - truth_lab[None, :, :], axis=2)
    matches = []
    for index, row in enumerate(inferred):
        nearest = int(distances[index].argmin())
        delta = float(distances[index, nearest])
        matches.append({"inferred_index": index, "hex": str(row["hex"]).upper(),
                        "rgb": list(row["rgb"]), "truth_index": nearest,
                        "delta_e_76": round(delta, 3), "matched": delta <= MATCH_DELTA_E})
    return matches


def trace_rgb(scene, work_dir: Path, settings: TraceSettings | None = None,
              palette_options: PaletteSettings | None = None,
              cleanup: CleanupSettings | None = None, *,
              subject: str = "synthetic") -> TraceRun:
    """Run a scene's anti-aliased render through the real palette and trace stages.

    ``scene`` is a ``Scene`` (rendered with ``raster.render_rgb``) or an
    ``(rgb, palette)`` pair. The render is written to ``work_dir/input/<subject>.png``
    and inferred into ``work_dir/<subject>/``.
    """
    if isinstance(scene, tuple):
        rgb, truth_palette = scene
        rgb = np.asarray(rgb, dtype=np.uint8)
    else:
        from vinylgen.fidelity.raster import render_rgb
        rgb, truth_palette = np.asarray(render_rgb(scene), dtype=np.uint8), scene.palette
    work_dir = Path(work_dir)
    input_dir = work_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    input_path = input_dir / f"{subject}.png"
    Image.fromarray(rgb, "RGB").save(input_path)
    started = time.perf_counter()
    report = infer_palette(input_path, work_dir, palette_options or PaletteSettings())
    palette_seconds = time.perf_counter() - started
    subject_dir = work_dir / subject
    audit, trace_seconds = _trace(subject_dir, settings, cleanup)
    inferred = json.loads((subject_dir / "palette.json").read_text())["palette"]
    matches = match_palette(inferred, list(truth_palette))
    return TraceRun(subject_dir=subject_dir, edge_svg=subject_dir / "traced-edge.svg",
                    overlap_svg=subject_dir / "traced-overlap.svg", audit=audit,
                    seconds=palette_seconds + trace_seconds, palette=inferred,
                    colour_map={row["hex"]: row["truth_index"] for row in matches if row["matched"]},
                    palette_seconds=palette_seconds, trace_seconds=trace_seconds,
                    palette_matches=matches,
                    unmatched=[row for row in matches if not row["matched"]])
