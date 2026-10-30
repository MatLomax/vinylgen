#!/usr/bin/env python3
"""Trace flattened palette rasters into layered, Inkscape-compatible SVGs."""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import base64
from dataclasses import asdict
from pathlib import Path
from typing import Callable

import numba
import numpy as np
import scipy
from PIL import Image
from scipy import ndimage

from vinylgen.colour import colour_names
from vinylgen.contracts import CleanupSettings, SegmentationBundle, TraceSettings
from vinylgen.cutter_cleanup import apply_cleanup, resolve_physical_size
from vinylgen.palette import Progress, _noop
from vinylgen.preview import preview_plan
from vinylgen.svg_writer import add_face_layer, face_areas, face_elements, svg_document, write_svg
from vinylgen.validation import (
    semantic_warning_mask,
    validate_semantic_preservation,
    validate_svg,
)

# The semantic audit checks source thin features at least this long: three
# times the maximum deviation (a feature shorter than that may legitimately be
# smoothed away by a fit within it), and never under the 6 px the thin-feature
# detector needs to tell a line from a speck. It is 6 px at the pixel-era 2 px
# deviation and grows with the image, like the deviation.
FEATURE_SPAN_DEVIATIONS = 3.0
MINIMUM_FEATURE_SPAN_PX = 6.0


def feature_span(maximum_deviation: float) -> float:
    """The shortest source feature (px) the semantic audit checks."""
    return max(MINIMUM_FEATURE_SPAN_PX, FEATURE_SPAN_DEVIATIONS * float(maximum_deviation))


def resolve_subjects(inputs: list[Path]) -> list[Path]:
    result: list[Path] = []
    for item in inputs:
        if item.is_dir() and (item / "flattened.png").exists():
            result.append(item)
        elif item.is_dir():
            result.extend(sorted(p.parent for p in item.glob("*/flattened.png")))
        else:
            raise ValueError(f"not a traceable output directory: {item}")
    return list(dict.fromkeys(result))


def normalize_hex(value: str) -> str:
    value = value.strip().upper()
    if not value.startswith("#"):
        value = "#" + value
    if not re.fullmatch(r"#[0-9A-F]{6}", value):
        raise ValueError(f"invalid colour: {value}")
    return value


def ensure_palette_names(report: dict, overrides: dict[str, str]) -> bool:
    palette = report["palette"]
    generated = colour_names(np.asarray([row["rgb"] for row in palette], dtype=np.uint8))
    changed = False
    for row, generated_name in zip(palette, generated):
        chosen = overrides.get(str(row["hex"]).upper(), row.get("name", generated_name))
        if row.get("name") != chosen:
            row["name"] = chosen
            changed = True
    names = [str(row["name"]).casefold() for row in palette]
    if len(names) != len(set(names)):
        raise ValueError("palette names must be unique")
    return changed


def resolve_colour(value: str | None, palette: list[dict]) -> int | None:
    if value is None:
        return None
    folded = value.strip().casefold()
    matches = [i for i, row in enumerate(palette)
               if folded in {str(row["name"]).casefold(), str(row["hex"]).casefold(),
                             str(row["hex"])[1:].casefold()}]
    if len(matches) != 1:
        raise ValueError(f"colour {value!r} does not uniquely match the palette")
    return matches[0]


def label_raster(image: np.ndarray, palette: list[dict]) -> np.ndarray:
    rgb = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
    packed_palette = (rgb[:, 0].astype(np.uint32) << 16) | (rgb[:, 1].astype(np.uint32) << 8) | rgb[:, 2]
    packed = (image[:, :, 0].astype(np.uint32) << 16) | (image[:, :, 1].astype(np.uint32) << 8) | image[:, :, 2]
    lookup = {int(value): index for index, value in enumerate(packed_palette)}
    unknown = set(np.unique(packed).tolist()) - set(lookup)
    if unknown:
        raise ValueError(f"flattened.png contains {len(unknown)} colour(s) absent from palette.json")
    result = np.empty(packed.shape, dtype=np.int16)
    for value, index in lookup.items():
        result[packed == value] = index
    return result


def write_trace_html(subject_dir: Path, width: int, height: int) -> None:
    """Write a self-contained overlay viewer with inline PNG and SVG data."""
    svg = (subject_dir / "traced-edge.svg").read_text()
    svg = re.sub(r"<\?xml[^>]*>\s*", "", svg, count=1)
    source_data = base64.b64encode((subject_dir / "source.png").read_bytes()).decode("ascii")
    title = html.escape(subject_dir.name.replace("-", " ").title())
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} trace overlay</title>
<style>
html {{ color-scheme: dark; font: 14px system-ui, sans-serif; background: #181818; }}
body {{ margin: 0; }}
header {{ box-sizing: border-box; height: 48px; display: flex; gap: 12px;
  align-items: center; padding: 8px 14px; }}
input {{ width: min(360px, 45vw); }}
.stage {{ position: relative; width: min(calc(100vw - 24px), {width}px);
  aspect-ratio: {width} / {height}; margin: 0 auto 12px; background: white; overflow: hidden; }}
.stage > img, .stage > svg {{ position: absolute; inset: 0; width: 100%; height: 100%; }}
.stage > img {{ object-fit: fill; }}
.stage > svg {{ opacity: .5; }}
</style>
</head>
<body>
<header><strong>{title}</strong><label for="opacity">Trace opacity</label>
<input id="opacity" type="range" min="0" max="1" value="0.5" step="0.01"></header>
<main class="stage"><img src="data:image/png;base64,{source_data}" alt="Original source">{svg}</main>
<script>
const trace = document.querySelector('.stage > svg');
document.querySelector('#opacity').addEventListener('input', event => {{
  trace.style.opacity = event.target.value;
}});
</script>
</body>
</html>
"""
    (subject_dir / "trace.html").write_text(document)


# The missed-detail overlay shares the traced pixel grid and carries only its
# marks, so a viewer can stack them over the SVG without hiding it.
MISSED_DETAIL_RGBA = (220, 35, 35, 255)


def missed_detail_overlay(missing: np.ndarray) -> Image.Image:
    """An RGBA image the size of ``missing``: opaque red on each missed source pixel, clear elsewhere."""
    overlay = np.zeros((*missing.shape, 4), dtype=np.uint8)
    overlay[missing] = MISSED_DETAIL_RGBA
    return Image.fromarray(overlay, "RGBA")


def source_component_count(mask: np.ndarray) -> int:
    """The 4-connected raster components of one colour's mask (edge-sharing connectivity, the
    cutter's authoritative region model)."""
    return int(ndimage.label(mask, structure=ndimage.generate_binary_structure(2, 1))[1])


class TraceTicks:
    """``trace_subject``'s progress band from 0.03 to 0.80, filled by t3's own counts (card
    section 1): f = 0.03 + 0.77 (j + c) / (J + N), j junction edges and c chains drawn out of
    J and N. J is the ``edges`` event's ``junction_edges``; N is a ``chain`` event's
    ``total``, taken as the ``edges`` event's ``count`` (an upper bound) until the first chain
    arrives. j and c only grow, and N only falls to its true value before c counts, so the bar
    never moves backwards. Called as t3api's ``on_event``."""

    BAND_START, BAND_END = 0.03, 0.80

    def __init__(self, tick: Progress) -> None:
        self.tick = tick
        self.started = False
        self.junction_edges = 0
        self.upper = 0
        self.j = 0
        self.c = 0

    def __call__(self, kind: str, payload: dict) -> None:
        if kind in ("start", "paths") and not self.started:
            self.started = True
            self.tick(0.02, "Tracing: planar map")
        elif kind == "edges":
            self.junction_edges = int(payload["junction_edges"])
            self.upper = int(payload["count"])
        elif kind == "junction_edge":
            self.j += 1
            self.tick(self.band(self.j, self.junction_edges + self.upper),
                      f"Tracing: junction edges {self.j} of {self.junction_edges}")
        elif kind == "chain":
            total = int(payload["total"])
            self.c = max(self.c, int(payload["drawn"]))
            self.tick(self.band(self.junction_edges + self.c, self.junction_edges + total),
                      f"Tracing: chains {self.c} of {total}")

    def band(self, done: int, total: int) -> float:
        """0.03 + 0.77 done / total, never past the band's end: at done = total the float sum
        can round above 0.80, the next tick's fraction."""
        fraction = self.BAND_START + (self.BAND_END - self.BAND_START) * done / max(1, total)
        return min(self.BAND_END, fraction)


def tracer_info() -> dict:
    """The tracer a trace run uses, as the web client's read-only tracer line names it:
    ``t3api``, its build and its one fixed setting (w, A1, A2, L)."""
    import t3api

    return {"name": "t3api", "build": t3api.BUILD_ID,
            "setting": {key: float(value) for key, value in t3api.SETTING.items()}}


def face_event(payload: dict) -> dict:
    """t3's ``face`` event as the live trace view receives it: ``{"type": "face", "face",
    "label", "rings", "nodes"}``, with the face's rings joined into one path-data string
    (the ``d`` of its path in the edge SVG) and its ``nodes`` the path's nodetypes."""
    return {"type": "face", "face": int(payload["face"]), "label": int(payload["label"]),
            "rings": "".join(payload["rings"]), "nodes": str(payload["nodes"])}


class TraceEvents:
    """t3api's ``on_event`` for a trace run: progress through ``TraceTicks``, and each ``face``
    event handed to ``on_face`` (``face_event``) the moment t3 emits it, in t3's order."""

    def __init__(self, tick: Progress, on_face: Callable[[dict], None] | None = None) -> None:
        self.ticks = TraceTicks(tick)
        self.on_face = on_face

    def __call__(self, kind: str, payload: dict) -> None:
        self.ticks(kind, payload)
        if kind == "face" and self.on_face is not None:
            self.on_face(face_event(payload))


def map_with_palette(pm: dict, palette: list[dict]) -> dict:
    """``pm`` with its header's ``palette`` and ``names`` set to ``palette``'s colours and
    names, as ``t3api.planar_map`` writes them; the faces, edges and nodes are untouched, so
    the map paints the same labels. ``pm`` itself when they already agree."""
    rgb = [list(row["rgb"]) for row in palette]
    names = [row["name"] for row in palette]
    if pm["palette"] == rgb and pm["names"] == names:
        return pm
    return {**pm, "palette": rgb, "names": names}


# The paper behind the cut vinyl: the colour the semantic audit sees at a centre no drawn
# face holds (t3's painted label -1).
UNPAINTED_RGB = (255, 255, 255)


def planar_map_gate(pm: dict, painted: np.ndarray) -> tuple[dict, dict]:
    """The planar map's gate, run on the read-back map before anything is derived from it:
    ``t3api.planar_map_check`` with no violation, then the map's paint
    (``t3api.paint_planar_map``) equal to t3's painted labels at every centre. Returns the
    check and ``{"differ": 0}``; either failure raises a ``RuntimeError`` naming its count."""
    import t3api

    check = t3api.planar_map_check(pm)
    if check["violations"]:
        raise RuntimeError(f"map.json fails planar_map_check with {check['violations']} "
                           f"violation(s): {check['problems']}")
    differ = int(np.count_nonzero(t3api.paint_planar_map(pm) != painted))
    if differ:
        raise RuntimeError(f"map.json read back paints {differ} centre(s) other than t3's "
                           "painted labels")
    return check, {"differ": differ}


def changed_explicit_components(explicit: np.ndarray, labels: np.ndarray,
                                painted: np.ndarray) -> list[dict]:
    """Each explicit protected component the drawing changes: a 4-connected one-colour region
    of the explicit mask (vinylgen's explicit component protection) holding at least one
    centre whose painted label differs from the traced label. Each row holds the component's
    ``label``, ``bounds`` (left, top, right, bottom; right and bottom exclusive), ``size``,
    ``count`` of changed centres and the ``changed`` mask, in label then raster order."""
    explicit = np.asarray(explicit, dtype=bool)
    rows: list[dict] = []
    if not explicit.any():
        return rows
    differs = np.asarray(painted) != labels
    four = ndimage.generate_binary_structure(2, 1)
    for label in np.unique(labels[explicit]).tolist():
        regions, _ = ndimage.label(explicit & (labels == label), structure=four)
        for number, window in enumerate(ndimage.find_objects(regions), start=1):
            if window is None:
                continue
            mask = regions[window] == number
            changed = mask & differs[window]
            count = int(changed.sum())
            if not count:
                continue
            full = np.zeros(labels.shape, dtype=bool)
            full[window] = changed
            rows.append({"label": int(label),
                         "bounds": (window[1].start, window[0].start, window[1].stop,
                                    window[0].stop),
                         "size": int(mask.sum()), "count": count, "changed": full})
    return rows


def node_limit_warnings(edge_stats: list[dict], settings: TraceSettings) -> list[str]:
    """The soft node limits' warnings: one per path over ``soft_path_nodes`` naming its id,
    one per layer over ``soft_layer_nodes`` naming it, and one for a document over
    ``soft_document_nodes``. Nodes are Inkscape's count, as ``geometry.edge`` records them."""
    warnings = []
    for layer in edge_stats:
        for row in layer["components"]:
            if row["nodes"] > settings.soft_path_nodes:
                warnings.append(f"path {row['id']} has {row['nodes']} nodes, over the soft "
                                f"path-node limit of {settings.soft_path_nodes}")
    for layer in edge_stats:
        if layer["nodes"] > settings.soft_layer_nodes:
            warnings.append(f"layer {layer['name']} has {layer['nodes']} nodes, over the soft "
                            f"layer-node limit of {settings.soft_layer_nodes}")
    total = sum(layer["nodes"] for layer in edge_stats)
    if total > settings.soft_document_nodes:
        warnings.append(f"traced-edge.svg has {total} nodes, over the soft document-node "
                        f"limit of {settings.soft_document_nodes}")
    return warnings


def trace_subject(subject_dir: Path, settings: TraceSettings, name_overrides: dict[str, str],
                  progress: Progress | None = None, *, cleanup: CleanupSettings | None = None,
                  clean_inline: bool = False, removed_index: int | None = None,
                  on_face: Callable[[dict], None] | None = None,
                  reuse: dict | None = None) -> dict:
    """Trace a subject's segmentation with t3 into ``map.json`` and the edge SVG.

    One ``t3api.trace`` call on the cleaned labels (``int16`` palette indices, every colour,
    the omitted one too) at t3's one setting. Its planar map is written as ``map.json``
    (``t3api.planar_map_json``) and read back (``t3api.read_planar_map``) for every later
    step: the planar map's gate (``planar_map_check`` with no violation, and the read-back
    paint equal to t3's painted labels at every centre, else a ``RuntimeError``), the edge
    SVG ``traced-edge.svg`` (one layer per included colour, ``svg_writer.add_face_layer``),
    and its ``traced.png`` preview. The semantic audit reads t3's painted labels at t3's
    tube half-width.

    ``cleanup`` supplies the physical size and the removed colour; without it the trace is in
    pixels and cuts every colour. ``removed_index``, when given, is the palette index of the
    removed colour as the caller already resolved it: the web pipeline resolves the
    cleanup's ``remove_colour`` against the palette that cleanup ran with, because
    ``name_overrides`` may since have renamed its colours. Without it
    ``cleanup.remove_colour`` is resolved against this trace's (renamed) palette. The
    segmentation is traced as it is stored, which in the web pipeline is the cleanup
    stage's cleaned labels. With ``clean_inline`` (the trace CLI) the cutter cleanup in
    ``cleanup`` is first applied to the labels in memory, through the same function the
    cleanup stage uses. ``settings`` (the soft node limits) is recorded and warns; t3 has
    one setting, so nothing in it steers the trace.

    ``on_face`` receives each of t3's ``face`` events (``face_event``) as t3 emits it, for the
    live trace view.

    ``reuse`` re-exports without a trace: the web pipeline found that an earlier trace
    revision traced these same labels with this same t3 build, and copied its ``map.json``
    into ``subject_dir``. ``reuse`` names that revision (``revision``) and carries its audit's
    ``t3`` block (``t3``). The map is used byte for byte (its header's palette and names are
    rewritten only where this trace's palette.json differs, which leaves every face as it
    is), checked with ``planar_map_check``, and everything after the trace (the edge SVG,
    the checks, the audit and the preview) is rebuilt from it with these settings. No
    ``t3api.trace`` call is made.
    """
    import t3api

    tick = progress or _noop
    cleanup = cleanup or CleanupSettings()
    tick(0.01, "Reading segmentation")
    flattened_path = subject_dir / "flattened.png"
    palette_path = subject_dir / "palette.json"
    report = json.loads(palette_path.read_text())
    if ensure_palette_names(report, name_overrides):
        palette_path.write_text(json.dumps(report, indent=2) + "\n")
    palette = report["palette"]
    source = np.asarray(Image.open(flattened_path).convert("RGB"))
    original_path = subject_dir / "source.png"
    original = np.asarray(Image.open(original_path).convert("RGB"))
    height, width = source.shape[:2]
    # The flatten stage may emit the segmentation at an integer nearest-neighbour
    # upscale of the source, so bring the pristine source onto that working grid
    # before it is compared against, and blended with, the flattened raster.
    if original.shape[:2] != (height, width):
        scale_y, scale_x = height // original.shape[0], width // original.shape[1]
        if original.shape[0] * scale_y != height or original.shape[1] * scale_x != width:
            raise ValueError("source dimensions are not an integer factor of the flattened output")
        original = np.repeat(np.repeat(original, scale_y, axis=0), scale_x, axis=1)
    segmentation_path = subject_dir / "segmentation.npz"
    if not segmentation_path.exists():
        raise FileNotFoundError(
            f"missing segmentation contract: {segmentation_path}; rerun palette inference")
    segmentation = SegmentationBundle.load(segmentation_path)
    segmentation.validate()
    expected_palette = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
    if not np.array_equal(segmentation.palette_rgb, expected_palette):
        raise ValueError("segmentation palette does not match palette.json")
    if segmentation.labels.shape != (height, width):
        raise ValueError("segmentation dimensions do not match flattened.png")
    labels = segmentation.labels
    if removed_index is None:
        omitted = resolve_colour(cleanup.remove_colour, palette)
    elif isinstance(removed_index, bool) or not 0 <= removed_index < len(palette):
        raise ValueError(f"removed colour index {removed_index!r} is not a palette colour")
    else:
        omitted = removed_index
    included = [i for i in range(len(palette)) if i != omitted]
    if not included:
        raise ValueError("cannot omit the only palette colour")
    if clean_inline:
        # From here on the trace sees exactly what the cleanup stage would have
        # stored: the trace, the audit and the warning images all use the cleaned
        # labels and the raster derived from them, never the uncleaned one.
        labels, protected, cleanup_summary, _ = apply_cleanup(
            labels, palette, cleanup, omitted,
            protected_features=segmentation.protected_features,
            explicit_protected_features=segmentation.explicit_protected_features)
        segmentation.labels = labels
        segmentation.protected_features = protected
        source = segmentation.palette_rgb[labels].astype(np.uint8)
    else:
        cleanup_summary = None
    if labels.dtype != np.int16:
        raise ValueError(f"the labels t3 traces are int16 palette indices, not {labels.dtype}")
    physical = resolve_physical_size(cleanup, width, height)
    physical_mm = ((physical.width_mm, physical.height_mm)
                   if physical.width_mm is not None else None)
    renderer = Path(__file__).resolve().parents[1] / "scripts" / "inkscape-headless"
    warnings: list[str] = []

    # A trace.json left by an earlier trace of this folder describes another map.json: it
    # goes before the map is written or checked, so a failed gate leaves no record vouching
    # for a map other than the one on disk.
    audit_path = subject_dir / "trace.json"
    audit_path.unlink(missing_ok=True)
    map_path = subject_dir / "map.json"
    if reuse is None:
        # The trace: one call, at t3's one setting, on every CPU the process may use.
        result = t3api.trace(labels, on_event=TraceEvents(tick, on_face))
        tick(0.8, "Writing the planar map and the edge SVG")
        map_path.write_text(t3api.planar_map_json(t3api.planar_map(result, palette)))
        pm = t3api.read_planar_map(map_path.read_text())
        painted = np.asarray(result.painted)
        check, read_back = planar_map_gate(pm, painted)
        t3_record = {"summary": result.summary, "seconds": result.seconds,
                     "warm_s": result.warm_s, "workers": result.workers}
    else:
        tick(0.8, "Reusing the planar map of the same labels and t3 build")
        pm = t3api.read_planar_map(map_path.read_text())
        if pm["build"] != t3api.BUILD_ID:
            raise RuntimeError(f"the reused map.json was written by t3api {pm['build']}, "
                               f"not {t3api.BUILD_ID}")
        if (pm["W"], pm["H"]) != (width, height):
            raise RuntimeError("the reused map.json is not the size of the traced labels")
        named = map_with_palette(pm, palette)
        if named is not pm:
            map_path.write_text(t3api.planar_map_json(named))
            pm = t3api.read_planar_map(map_path.read_text())
        check = t3api.planar_map_check(pm)
        if check["violations"]:
            raise RuntimeError(f"map.json fails planar_map_check with {check['violations']} "
                               f"violation(s): {check['problems']}")
        # The map's paint is the one its own trace's gate found equal to t3's painted labels:
        # the faces are the same bytes, and the header holds no geometry.
        painted = np.asarray(t3api.paint_planar_map(pm))
        read_back = {"differ": 0, "reused_from": reuse["revision"]}
        t3_record = {**{key: reuse["t3"][key] for key in ("summary", "seconds", "warm_s",
                                                           "workers")},
                     "reused_from": reuse["revision"]}

    paths = t3api.planar_map_paths(pm)
    areas = face_areas(pm)
    elements = face_elements(pm)
    root = svg_document(pm["W"], pm["H"], physical_mm)
    edge_stats = []
    for index in included:
        stats = add_face_layer(root, palette[index], pm, paths=paths, areas=areas,
                               elements=elements)
        stats["source_colour_components"] = source_component_count(labels == index)
        edge_stats.append(stats)
    edge_path = subject_dir / "traced-edge.svg"
    write_svg(edge_path, root)
    # The overlap variant and the recovery fallbacks are not written by this tracer; a file
    # left by an earlier trace of this folder would describe another drawing.
    for stale in ("traced-overlap.svg", "trace-fallbacks.png", "trace-fallbacks-overlay.png"):
        (subject_dir / stale).unlink(missing_ok=True)
    tick(0.85, "Checking the planar map")
    gap_stats: dict = {}
    t3api.faces_svg_from_planar_map(
        pm, [dict(row, index=i) for i, row in enumerate(palette)], gap_stats)
    validations = {"edge": {**validate_svg(edge_path, palette, included),
                            "planar_map_check": check, "read_back": read_back}}

    tick(0.88, "Semantic audit")
    # The audit reads the exact raster of the drawing, t3's painted labels, at t3's tube
    # half-width: every drawn piece lies within it of its edge's points.
    tolerance = float(t3api.SETTING["w"]) / 2.0
    minimum_feature_span = feature_span(tolerance)
    vector_labels = painted.astype(np.int16)
    palette_rgb = segmentation.palette_rgb.astype(np.uint8)
    vector_rgb = np.where((painted >= 0)[..., None], palette_rgb[np.maximum(painted, 0)],
                          np.asarray(UNPAINTED_RGB, dtype=np.uint8)).astype(np.uint8)
    semantic_cache: dict = {}
    validations["semantic"] = validate_semantic_preservation(
        original, source, vector_rgb=vector_rgb,
        protected_features=segmentation.protected_features,
        segmentation_labels=segmentation.labels, vector_labels=vector_labels,
        maximum_deviation=tolerance, minimum_feature_span=minimum_feature_span,
        invariant_cache=semantic_cache)
    write_trace_html(subject_dir, width, height)
    semantic_inspection = subject_dir / "trace-semantic-warnings.png"
    semantic_overlay = subject_dir / "trace-missed-details-overlay.png"
    # The missed detail: the source pixels the audit finds beyond its limit, and every changed
    # centre of each explicit protected component the drawing changes. The overlay marks it
    # all; the opaque inspection image, written when the audit fails, marks the same pixels.
    missed = np.zeros(labels.shape, dtype=bool)
    if not validations["semantic"]["passed"]:
        missing = semantic_warning_mask(
            original, vector_rgb, maximum_deviation=tolerance,
            source_features=semantic_cache.get("source_features"))
        missed |= missing
        warnings.append(
            f"semantic feature audit found {int(missing.sum())} source pixels beyond "
            f"the {tolerance:g}px deviation limit; see {semantic_inspection.name} and "
            f"{semantic_overlay.name}")
    for component in changed_explicit_components(
            segmentation.explicit_protected_features, labels, painted):
        missed |= component["changed"]
        x0, y0, x1, y1 = component["bounds"]
        warnings.append(
            f"explicit protected component of {palette[component['label']]['name']} at pixel "
            f"bounds [{x0}, {y0}, {x1}, {y1}] (left, top, right, bottom) is changed by the "
            f"trace: {component['count']} of {component['size']} centres changed; see "
            f"{semantic_overlay.name}")
    if not validations["semantic"]["passed"]:
        faded = Image.blend(Image.fromarray(original),
                            Image.new("RGB", (width, height), "white"), 0.82)
        overlay = np.asarray(faded).copy()
        overlay[missed] = MISSED_DETAIL_RGBA[:3]
        Image.fromarray(overlay).save(semantic_inspection)
    else:
        semantic_inspection.unlink(missing_ok=True)
    if missed.any():
        missed_detail_overlay(missed).save(semantic_overlay)
    else:
        semantic_overlay.unlink(missing_ok=True)
    warnings.extend(node_limit_warnings(edge_stats, settings))
    if gap_stats["gaps"]:
        warnings.append(
            f"the planar map holds {gap_stats['gaps']} gap connector(s), the longest "
            f"{gap_stats['gap_max']:g}px: a drawn edge does not reach its neighbour there")
    audit = {"subject": subject_dir.name,
             "sources": {"original": str(original_path), "flattened": str(flattened_path),
                         "palette": str(palette_path),
                         "segmentation": str(segmentation_path), "map": str(map_path)},
             "raster_dimensions": [width, height],
             "mode": "physical" if physical_mm is not None else "pixels",
             "physical_dimensions_mm": list(physical_mm) if physical_mm is not None else None,
             "settings": asdict(settings),
             "cleanup_settings": asdict(physical),
             "backend": {"tracer": "t3api", "build": t3api.BUILD_ID, "version": t3api.VERSION,
                         "setting": t3api.SETTING, "numpy": np.__version__,
                         "scipy": scipy.__version__, "numba": numba.__version__},
             "t3": {**t3_record, "gaps": gap_stats["gaps"], "gap_max": gap_stats["gap_max"]},
             "colours": {"included": [palette[i] for i in included],
                         "omitted": palette[omitted] if omitted is not None else None},
             "cleanup": cleanup_summary, "geometry": {"edge": edge_stats},
             "validation": validations, "warnings": warnings}
    audit["determinism"] = {
        "input_sha256": hashlib.sha256(
            flattened_path.read_bytes() + palette_path.read_bytes()
            + segmentation_path.read_bytes()).hexdigest(),
        "map_sha256": hashlib.sha256(map_path.read_bytes()).hexdigest()}
    plan = preview_plan(edge_path)
    audit["preview"] = {"file": "traced.png",
                        **{key: value for key, value in plan.items() if key != "arguments"}}
    tick(0.9, "Writing artifacts")
    audit_path.write_text(json.dumps(audit, indent=2) + "\n")
    tick(0.95, "Rendering preview")
    subprocess.run([
        str(renderer), str(edge_path),
        "--export-type=png", *plan["arguments"],
        f"--export-filename={subject_dir / 'traced.png'}",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tick(1.0, "Complete")
    return audit
