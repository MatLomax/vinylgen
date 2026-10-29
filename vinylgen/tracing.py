#!/usr/bin/env python3
"""Trace flattened palette rasters into layered, Inkscape-compatible SVGs."""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
from numba import njit, prange
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely.ops import unary_union

from vinylgen.colour import colour_names
from vinylgen.contracts import CleanupSettings, SegmentationBundle, TraceSettings
from vinylgen.cutter_cleanup import apply_cleanup, resolve_physical_size
from vinylgen.curve_fit import SharedChainFitter, geometry_components, region_components
from vinylgen.palette import Progress, _noop
from vinylgen.parallel_guard import KERNEL_LOCK as _KERNEL_LOCK
from vinylgen.preview import preview_plan
from vinylgen.svg_writer import add_face_layer, face_areas, face_elements, svg_document, write_svg
from vinylgen.validation import (
    semantic_warning_mask,
    validate_semantic_preservation,
    validate_svg,
)

# Two exact partition polygons neighbour each other when they share a boundary of
# positive length. A corner-only touch is a zero-length point intersection and is
# not adjacency under the authoritative four-connectivity model.
_UNDERLAYER_ADJACENCY_EPS = 1e-9

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


def _partition_polygons(geometry) -> list:
    if geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        return []
    return [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)


# The partition gap and overlap areas are GEOS unary-union area arithmetic summed
# over thousands of faces, so they carry floating-point rounding that grows with
# the canvas size and face count. A genuine partition defect — a region left
# uncovered by every layer, or claimed by two — is at least a whole pixel of
# area, orders of magnitude above that noise. The watertightness gate therefore
# tolerates noise scaled to the canvas area rather than a fixed absolute epsilon,
# which a large source trips even when every face tiles the plane perfectly.
_PARTITION_RELATIVE_TOLERANCE = 1e-9
_PARTITION_ABSOLUTE_TOLERANCE = 1e-6


def partition_watertight_fault(topology: dict, canvas_area: float) -> str | None:
    """Describe why the shared-boundary partition is not watertight, or ``None``.

    ``None`` means the uncovered (gap) and doubly-covered (overlap) areas are
    both within floating-point noise for a canvas of this size. Otherwise the
    returned message names each failing quantity in absolute px² and as a
    fraction of the canvas, without embedding the per-chain inventory that makes
    the raw topology dict unreadable when it surfaces to a user.
    """
    tolerance = max(_PARTITION_ABSOLUTE_TOLERANCE,
                    canvas_area * _PARTITION_RELATIVE_TOLERANCE)
    gap = float(topology["partition_gap_area"])
    overlap = float(topology["overlap_area"])
    faults: list[str] = []
    if gap > tolerance:
        faults.append(
            f"{gap:.4g} px² of the traced region is covered by no colour layer "
            f"({gap / canvas_area:.3%} of the canvas)")
    if overlap > tolerance:
        faults.append(
            f"{overlap:.4g} px² is claimed by more than one colour layer "
            f"({overlap / canvas_area:.3%} of the canvas)")
    if not faults:
        return None
    return ("shared-boundary partition is not watertight: " + "; and ".join(faults)
            + f". This exceeds the {tolerance:.4g} px² rounding tolerance for this "
            f"{int(round(canvas_area))} px² canvas of "
            f"{int(topology['polygonized_faces'])} faces.")



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


@njit(parallel=True, cache=True)
def _label_counts(labels, size):
    """Pixels of each label ``0..size-1`` in a 2-D label array (other values are not
    counted), tallied in parallel row bands."""
    height, width = labels.shape
    bands = 64
    partial = np.zeros((bands, size + 1), np.int64)
    step = (height + bands - 1) // bands
    for band in prange(bands):
        for y in range(band * step, min(height, (band + 1) * step)):
            for x in range(width):
                value = labels[y, x]
                partial[band, value if 0 <= value < size else size] += 1
    return partial.sum(axis=0)[:size]


def infer_order(labels: np.ndarray, included: list[int], palette: list[dict]) -> tuple[list[int], list[str]]:
    # Large, canvas-touching colours make the most plausible lower layers. The evidence
    # per colour is its pixel count and its count along the four edges (the first and
    # last row and column, so a corner pixel counts on both of its edges), tallied for
    # every colour at once rather than with one full-image mask per colour.
    h, w = labels.shape
    size = max(len(palette), max(included, default=-1) + 1)
    with _KERNEL_LOCK:
        areas = _label_counts(labels, size)
        edges = (_label_counts(labels[:1], size) + _label_counts(labels[-1:], size)
                 + _label_counts(labels[:, :1], size) + _label_counts(labels[:, -1:], size))
    rows = []
    for index in included:
        rows.append((-int(edges[index]), -int(areas[index]), str(palette[index]["hex"]), index))
    ordered = sorted(rows)
    order = [row[-1] for row in ordered]
    warnings = []
    if any(ordered[i][:2] == ordered[i + 1][:2] for i in range(len(ordered) - 1)):
        warnings.append(
            "Layer-order evidence tied adjacent colours; hexadecimal order broke the tie.")
    return order, warnings


def layer_order_matters(settings: TraceSettings) -> bool:
    """Whether bottom-to-top layer order can change any traced geometry.

    Only solid underlayers or a positive overlap extend a lower layer beneath the
    layers above it. Without either, every colour meets its neighbours edge to edge
    and each layer is its own partition region, whatever the stacking.
    """
    return bool(settings.solid_underlayers or settings.overlap > 0)


# Why the automatic order is not inferred when stacking cannot change geometry.
ORDER_NOT_INFERRED = ("no solid underlayers and no overlap: layers meet edge to edge, "
                      "so stacking order changes no geometry; palette order is used")


def automatic_order(labels: np.ndarray, included: list[int], palette: list[dict],
                    settings: TraceSettings) -> tuple[list[int], list[str], bool]:
    """The bottom-to-top order colours an override leaves unnamed follow, its warnings,
    and whether it was inferred. ``infer_order`` runs only when order can matter;
    otherwise the included colours stack in palette order."""
    if layer_order_matters(settings):
        order, warnings = infer_order(labels, included, palette)
        return order, warnings, True
    return list(included), [], False


def apply_order_override(inferred: list[int], requested: list[str] | None,
                         palette: list[dict]) -> tuple[list[int], list[int]]:
    if not requested:
        return inferred, []
    specified = [resolve_colour(value, palette) for value in requested]
    if len(specified) != len(set(specified)):
        raise ValueError("layer order contains a duplicate colour")
    missing = [index for index in inferred if index not in specified]
    return [*specified, *missing], specified


def underlayer_geometry(partition: dict[int, object], order: list[int], *,
                        solid_underlayers: bool, overlap_px: float,
                        canvas) -> dict[int, tuple[object, bool]]:
    """The overlap-variant geometry of each layer, derived from the fixed partition.

    A solid underlayer backs only the higher shapes this colour actually touches:
    extending it beneath non-adjacent colours would stack identical shapes with no
    physical purpose, so each higher partition contributes only the components
    sharing a boundary of positive length with this colour's region. Without solid
    underlayers, every layer but the top one grows by the overlap buffer, clipped to
    ``canvas``. Each value is ``(geometry, stacked)``; the mapping is empty when the
    overlap variant equals the edge variant. These are independent GEOS operations
    that release the GIL, so they run on threads.
    """
    def _solid_geometry(position: int) -> tuple[object, bool]:
        base = partition[order[position]]
        stacked = [base]
        for higher in range(position + 1, len(order)):
            for shape in _partition_polygons(partition[order[higher]]):
                if base.boundary.intersection(shape.boundary).length > _UNDERLAYER_ADJACENCY_EPS:
                    stacked.append(shape)
        solid = len(stacked) > 1
        return (unary_union(stacked) if solid else base), solid

    def _buffered_geometry(position: int) -> tuple[object, bool]:
        return partition[order[position]].buffer(overlap_px, join_style=1).intersection(canvas), True

    if not (solid_underlayers or overlap_px > 0):
        return {}
    builder = _solid_geometry if solid_underlayers else _buffered_geometry
    positions = [position for position in range(len(order))
                 if solid_underlayers or position < len(order) - 1]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {order[position]: executor.submit(builder, position)
                   for position in positions}
        return {index: future.result() for index, future in futures.items()}


def overlap_layer_components(position: int, order: list[int], edge_components: list[dict],
                             layer_geometry: dict[int, tuple[object, bool]], boundary_track,
                             chain_fits: dict, settings: TraceSettings, overlap_px: float,
                             fitter: SharedChainFitter) -> tuple[list[dict], str]:
    """The overlap-variant components of the layer at ``position`` and how they were built."""
    index = order[position]
    if settings.solid_underlayers:
        overlap_geometry, solid = layer_geometry[index]
        # Reassemble the solid outline from the same once-fitted chains, dissolving
        # only the interfaces it swallows. Re-fitting the union instead (as an
        # independent curve pass) is what opened gaps between adjacent underlayers.
        if solid:
            return (region_components(boundary_track, chain_fits, overlap_geometry),
                    "solid stacked underlayer")
        return edge_components, "canonical shared-chain fit"
    if overlap_px > 0 and position < len(order) - 1:
        overlap_geometry, _ = layer_geometry[index]
        return (geometry_components(overlap_geometry, settings, fitter=fitter),
                "shared partition plus lower-layer overlap buffer")
    return edge_components, "canonical shared-chain fit"


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


# Transparent inspection overlays share the traced pixel grid and carry only their
# marks, so a viewer can stack them over the SVG without hiding it.
MISSED_DETAIL_RGBA = (220, 35, 35, 255)
TIGHTENED_BOUNDARY_RGBA = (0, 200, 255, 255)
TIGHTENED_BOUNDARY_HALO_RGBA = (12, 14, 13, 190)


def missed_detail_overlay(missing: np.ndarray) -> Image.Image:
    """An RGBA image the size of ``missing``: opaque red on each missed source pixel, clear elsewhere."""
    overlay = np.zeros((*missing.shape, 4), dtype=np.uint8)
    overlay[missing] = MISSED_DETAIL_RGBA
    return Image.fromarray(overlay, "RGBA")


def tightened_boundary_overlay(chains: list[list[tuple[float, float]]], width: int,
                               height: int) -> Image.Image:
    """An RGBA ``width`` x ``height`` image marking each tightened boundary chain.

    Every chain is a cyan stroke over a dark halo, so it reads against any vinyl
    colour beneath it; pixels off the marks stay fully transparent.
    """
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for points in chains:
        draw.line(points, fill=TIGHTENED_BOUNDARY_HALO_RGBA, width=4, joint="curve")
    for points in chains:
        draw.line(points, fill=TIGHTENED_BOUNDARY_RGBA, width=2, joint="curve")
    return overlay



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


# The paper behind the cut vinyl: the colour the semantic audit sees at a centre no drawn
# face holds (t3's painted label -1).
UNPAINTED_RGB = (255, 255, 255)


def trace_subject(subject_dir: Path, settings: TraceSettings, name_overrides: dict[str, str],
                  progress: Progress | None = None, *, cleanup: CleanupSettings | None = None,
                  clean_inline: bool = False, removed_index: int | None = None) -> dict:
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
    cleanup stage uses. ``settings`` is recorded; t3 has one setting, so none of its fit
    fields steers the trace.
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

    # The trace: one call, at t3's one setting, on every CPU the process may use.
    result = t3api.trace(labels, on_event=TraceTicks(tick))
    tick(0.8, "Writing the planar map and the edge SVG")
    map_path = subject_dir / "map.json"
    map_path.write_text(t3api.planar_map_json(t3api.planar_map(result, palette)))
    pm = t3api.read_planar_map(map_path.read_text())
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
    check = t3api.planar_map_check(pm)
    if check["violations"]:
        raise RuntimeError(f"map.json fails planar_map_check with {check['violations']} "
                           f"violation(s): {check['problems']}")
    painted = np.asarray(result.painted)
    read_back = t3api.paint_planar_map(pm)
    differ = int(np.count_nonzero(read_back != painted))
    if differ:
        raise RuntimeError(f"map.json read back paints {differ} centre(s) other than t3's "
                           "painted labels")
    validations = {"edge": validate_svg(edge_path, palette, included)}

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
    if not validations["semantic"]["passed"]:
        missing = semantic_warning_mask(
            original, vector_rgb, maximum_deviation=tolerance,
            source_features=semantic_cache.get("source_features"))
        faded = Image.blend(Image.fromarray(original),
                            Image.new("RGB", (width, height), "white"), 0.82)
        overlay = np.asarray(faded).copy()
        overlay[missing] = (220, 35, 35)
        Image.fromarray(overlay).save(semantic_inspection)
        missed_detail_overlay(missing).save(semantic_overlay)
        warnings.append(
            f"semantic feature audit found {int(missing.sum())} source pixels beyond "
            f"the {tolerance:g}px deviation limit; see {semantic_inspection.name}")
    else:
        semantic_inspection.unlink(missing_ok=True)
        semantic_overlay.unlink(missing_ok=True)
    if sum(layer["nodes"] for layer in edge_stats) > settings.soft_document_nodes:
        warnings.append("traced-edge.svg exceeds the soft document-node limit")
    audit = {"subject": subject_dir.name,
             "sources": {"original": str(original_path), "flattened": str(flattened_path),
                         "palette": str(palette_path),
                         "segmentation": str(segmentation_path)},
             "raster_dimensions": [width, height],
             "mode": "physical" if physical_mm is not None else "pixels",
             "physical_dimensions_mm": list(physical_mm) if physical_mm is not None else None,
             "settings": asdict(settings),
             "cleanup_settings": asdict(physical),
             "backend": {"tracer": "t3api", "build": t3api.BUILD_ID, "version": t3api.VERSION},
             "t3": {"summary": result.summary, "seconds": result.seconds,
                    "warm_s": result.warm_s, "workers": result.workers},
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
    (subject_dir / "trace.json").write_text(json.dumps(audit, indent=2) + "\n")
    tick(0.95, "Rendering preview")
    subprocess.run([
        str(renderer), str(edge_path),
        "--export-type=png", *plan["arguments"],
        f"--export-filename={subject_dir / 'traced.png'}",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tick(1.0, "Complete")
    return audit
