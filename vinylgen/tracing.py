#!/usr/bin/env python3
"""Trace flattened palette rasters into layered, Inkscape-compatible SVGs."""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import base64
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import numpy as np
from numba import njit, prange
from PIL import Image, ImageDraw
import shapely
from scipy import ndimage
from shapely.geometry import box
from shapely.ops import unary_union

from vinylgen.colour import colour_names, nearest_palette_labels
from vinylgen.contracts import CleanupSettings, SegmentationBundle, TraceSettings
from vinylgen.cutter_cleanup import apply_cleanup, pixel_scale, resolve_physical_size
from vinylgen.curve_fit import (
    SharedChainFitter, geometry_components, region_components, shared_boundary_components)
from vinylgen.palette import Progress, _noop
from vinylgen.parallel_guard import KERNEL_LOCK as _KERNEL_LOCK
from vinylgen.preview import preview_plan
from vinylgen.boundary_graph import (
    shared_boundary_polygons,
    component_sizes,
)
from vinylgen.svg_writer import add_layer, svg_document, write_svg
from vinylgen.validation import (
    semantic_warning_mask,
    validate_semantic_preservation,
    validate_svg,
)

# Two exact partition polygons neighbour each other when they share a boundary of
# positive length. A corner-only touch is a zero-length point intersection and is
# not adjacency under the authoritative four-connectivity model.
_UNDERLAYER_ADJACENCY_EPS = 1e-9

# The maximum contour deviation (px of the traced image) when none is configured.
DEFAULT_MAX_DEVIATION_PX = 2.0


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
    svg = (subject_dir / "traced-overlap.svg").read_text()
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



def trace_subject(subject_dir: Path, settings: TraceSettings, name_overrides: dict[str, str],
                  progress: Progress | None = None, *, cleanup: CleanupSettings | None = None,
                  clean_inline: bool = False) -> dict:
    """Trace a subject's segmentation into layered SVGs.

    ``cleanup`` supplies the physical size and the removed colour; without it the
    trace is in pixels and cuts every colour. The segmentation is traced as it is
    stored, which in the web pipeline is the cleanup stage's cleaned labels. With
    ``clean_inline`` (the trace CLI) the cutter cleanup in ``cleanup`` is first
    applied to the labels in memory, through the same function the cleanup stage uses.
    """
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
    labels = segmentation.labels.copy()
    omitted = resolve_colour(cleanup.remove_colour, palette)
    included = [i for i in range(len(palette)) if i != omitted]
    if not included:
        raise ValueError("cannot omit the only palette colour")
    if clean_inline:
        # From here on the trace sees exactly what the cleanup stage would have
        # stored: layer order, validation, fallback and warning images all use the
        # cleaned labels and the raster derived from them, never the uncleaned one.
        labels, protected, cleanup_summary, _ = apply_cleanup(
            labels, palette, cleanup, omitted,
            protected_features=segmentation.protected_features,
            explicit_protected_features=segmentation.explicit_protected_features)
        segmentation.labels = labels
        segmentation.protected_features = protected
        source = segmentation.palette_rgb[labels].astype(np.uint8)
    else:
        cleanup_summary = None
    working = labels
    automatic, order_warnings, inferred = automatic_order(labels, included, palette, settings)
    order, override = apply_order_override(automatic, settings.layer_order, palette)
    if any(i not in included for i in order):
        raise ValueError("omitted colour cannot occur in layer order")
    physical = resolve_physical_size(cleanup, width, height)
    units_per_pixel = pixel_scale(physical, width)
    physical_mm = ((physical.width_mm, physical.height_mm)
                   if physical.width_mm is not None else None)
    # Curve-fitting settings, max_deviation included, are always in pixels of
    # the traced image; only overlap and the cutter-cleanup thresholds are
    # physical and converted through units_per_pixel.
    if settings.max_deviation is None:
        settings.max_deviation = DEFAULT_MAX_DEVIATION_PX
    overlap_px = settings.overlap / units_per_pixel
    tracing_settings = TraceSettings(**asdict(settings))
    warnings = list(order_warnings)
    scratch = Path(__file__).resolve().parents[1] / ".tmp"
    scratch.mkdir(exist_ok=True)
    tolerance = float(tracing_settings.max_deviation or 0.0)
    tick(0.05, "Building shared boundaries")
    partition, boundary_track, topology = shared_boundary_polygons(
        working, included, segmentation.palette_costs,
        protected_features=segmentation.protected_features)
    fault = partition_watertight_fault(topology, float(width * height))
    if fault is not None:
        raise RuntimeError(fault)
    fallback_image = subject_dir / "trace-fallbacks.png"
    fallback_overlay = subject_dir / "trace-fallbacks-overlay.png"
    # Unique per trace: concurrent traces of same-named subjects (parallel jobs,
    # parallel tests) must not overwrite or delete each other's render.
    semantic_render = scratch / f"{subject_dir.name}-{uuid.uuid4().hex}-semantic-validation.png"
    renderer = Path(__file__).resolve().parents[1] / "scripts" / "inkscape-headless"
    palette_rgb = segmentation.palette_rgb.astype(np.int32)
    canvas = box(0, 0, width, height)
    # Raster component sizes depend only on the cleaned labels, so they are
    # measured once per colour rather than on every recovery pass.
    raster_component_sizes = {index: component_sizes(working == index) for index in order}
    # The overlap-variant geometry of each layer derives from the fixed partition
    # alone, so it is built once here.
    layer_geometry = underlayer_geometry(
        partition, order, solid_underlayers=settings.solid_underlayers,
        overlap_px=overlap_px, canvas=canvas)

    # One fitter persists across every recovery pass. Its cache key encodes the
    # per-chain settings recovery varies, so an unchanged chain returns from cache
    # instead of re-running the curve fit on each of the up-to-five passes.
    shared_fitter = SharedChainFitter()
    # Source-side semantic-validation work is identical every pass (only the
    # rasterized vector changes), so it is computed once and reused.
    semantic_cache: dict = {}
    # Recovery re-fits only a handful of chains per pass, so most of the rendered
    # vector is byte-identical between passes. These hold the previous pass's
    # rendered RGB and its nearest-palette labels so a later pass can recompute
    # labels only where the RGB actually changed and reuse them everywhere else.
    prev_vector_rgb = None
    prev_vector_labels = None

    def build_and_validate(recovery: dict[str, dict[str, object]], report=_noop):
        nonlocal prev_vector_rgb, prev_vector_labels
        components_by_colour, chain_fits, fit_audit = shared_boundary_components(
            boundary_track, included, tracing_settings, recovery,
            progress=lambda fraction, message: report(fraction * 0.6, message),
            fitter=shared_fitter)
        variant_stats: dict[str, list[dict]] = {"overlap": [], "edge": []}
        roots = {name: svg_document(width, height, physical_mm) for name in variant_stats}
        for position, index in enumerate(order):
            report(0.6 + 0.2 * position / len(order),
                   f"Fitting layer {position + 1} of {len(order)}")
            edge_components = components_by_colour[index]
            edge_stats = add_layer(roots["edge"], palette[index], edge_components, "",
                                   subject_dir.name, "edge", 0.0)
            edge_stats["construction"] = "canonical shared-chain fit"
            edge_stats["source_colour_components"] = len(raster_component_sizes[index])
            variant_stats["edge"].append(edge_stats)
            overlap_components, construction = overlap_layer_components(
                position, order, edge_components, layer_geometry, boundary_track,
                chain_fits, tracing_settings, overlap_px, shared_fitter)
            overlap_stats = add_layer(roots["overlap"], palette[index], overlap_components, "",
                                      subject_dir.name, "overlap", 0.0)
            overlap_stats["construction"] = construction
            overlap_stats["raster_component_pixels_descending"] = raster_component_sizes[index]
            variant_stats["overlap"].append(overlap_stats)
        for variant, root in roots.items():
            write_svg(subject_dir / f"traced-{variant}.svg", root)
        report(0.85, "Rendering for validation")
        validations = {
            variant: validate_svg(subject_dir / f"traced-{variant}.svg", palette, order)
            for variant in variant_stats
        }
        subprocess.run([
            str(renderer), str(subject_dir / "traced-overlap.svg"),
            "--export-type=png", f"--export-width={width}",
            f"--export-filename={semantic_render}",
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            vector_rgb = np.asarray(Image.open(semantic_render).convert("RGB"))
        finally:
            semantic_render.unlink(missing_ok=True)
        if prev_vector_rgb is None:
            # First render: seed the labels with the per-pixel nearest-palette
            # argmin over the whole image. A guarded Numba kernel computes the
            # same first-minimum argmin of squared RGB distance bit-for-bit
            # without ever materialising the (rows, columns, colours, channels)
            # distance tensor.
            vector_labels = nearest_palette_labels(vector_rgb, palette_rgb)
        else:
            # Later passes: only the re-fitted region of the render differs, so
            # recompute labels solely where the RGB changed and reuse last pass's
            # labels elsewhere. This is bit-identical to the full recomputation:
            # unchanged pixels keep labels that equal the full argmin of the
            # identical RGB there, and changed pixels use the IDENTICAL distance
            # formula and dtypes (int32 diff, squared, summed as int64, argmin,
            # cast int16). argmin's first-minimum tie-break is the same for the
            # full 3-D reduction and the per-pixel 2-D reduction.
            changed = np.any(vector_rgb != prev_vector_rgb, axis=2)
            vector_labels = prev_vector_labels.copy()
            if changed.any():
                sub = vector_rgb[changed].astype(np.int32)
                sub_d = np.sum((sub[:, None, :] - palette_rgb[None, :, :]) ** 2,
                               axis=2, dtype=np.int64)
                vector_labels[changed] = sub_d.argmin(axis=1).astype(np.int16)
        prev_vector_rgb = vector_rgb
        prev_vector_labels = vector_labels
        # Only vector_postfit drives the re-fit decision below; the whole-image
        # dark-support metrics feed the final audit alone, so defer them here and
        # compute them once after the loop over the settled vector.
        validations["semantic"] = validate_semantic_preservation(
            original, source, vector_rgb=vector_rgb,
            protected_features=segmentation.protected_features,
            segmentation_labels=segmentation.labels, vector_labels=vector_labels,
            maximum_deviation=tolerance, invariant_cache=semantic_cache,
            defer_vector_dark_metrics=True)
        report(1.0, "Validated")
        return variant_stats, validations, vector_rgb, fit_audit

    chain_bounds = {}
    for chain in boundary_track.chains:
        xs = [point[0] for point in chain.source_points]
        ys = [point[1] for point in chain.source_points]
        chain_bounds[chain.identity] = (min(xs), min(ys), max(xs), max(ys))
    recovery: dict[str, dict[str, object]] = {}
    protected_components, _ = ndimage.label(
        segmentation.protected_features, structure=np.ones((3, 3), dtype=np.uint8))
    # Recovery is deliberately ordered and local. It first improves the model,
    # then introduces one non-corner fit constraint, and only finally emits the
    # dense evidence as an explicit, reported fallback.
    def _attempt_report(attempt: int):
        if attempt == 0:
            # The first pass is the common case and owns the whole 0.05..0.85 band.
            return lambda fraction, message: tick(0.05 + fraction * 0.80, message)
        # Protected-feature recovery re-runs are rare; report them in a compressed
        # tail so the bar keeps advancing without ever jumping backwards.
        return lambda fraction, message: tick(
            0.85 + min(1.0, fraction) * 0.03,
            f"Refining protected features (pass {attempt + 1})")

    for fit_attempt in range(5):
        variant_stats, validations, vector_rgb, shared_fit_audit = build_and_validate(
            recovery, _attempt_report(fit_attempt))
        failures = []
        for row in validations["semantic"]["protected_features"]["vector_postfit"]["components"]:
            if row["passed"]:
                continue
            if np.any(segmentation.explicit_protected_features
                      & (protected_components == int(row["component"]))):
                failures.append(row)
        if not failures:
            break
        changed = False
        advanced: set[str] = set()
        for failure in failures:
            x0, y0, x1, y1 = failure["source_bounds_px"]
            source_label = failure["source_label"]
            for chain in boundary_track.chains:
                if chain.identity in advanced:
                    continue
                if source_label not in chain.pair:
                    continue
                left, top, right, bottom = chain_bounds[chain.identity]
                if right < x0 - tolerance or left > x1 + tolerance or bottom < y0 - tolerance or top > y1 + tolerance:
                    continue
                state = recovery.setdefault(chain.identity, {"stage": 0})
                next_stage = min(4, int(state["stage"]) + 1)
                if int(state["stage"]) == next_stage:
                    continue
                state["stage"] = next_stage
                if next_stage >= 3:
                    centre = ((x0 + x1) / 2.0, (y0 + y1) / 2.0)
                    state["constraint"] = min(
                        chain.points,
                        key=lambda point: ((point[0] - centre[0]) ** 2
                                           + (point[1] - centre[1]) ** 2))
                advanced.add(chain.identity)
                changed = True
        if not changed:
            break
    else:
        # The final pass advanced recovery without a following build. Rebuild so
        # the emitted SVG and every audit artifact describe the same recovery
        # state that the reporting below counts.
        variant_stats, validations, vector_rgb, shared_fit_audit = build_and_validate(
            recovery, _attempt_report(4))
    # Every pass above deferred the whole-image dark-support metrics; compute
    # them once now over the settled vector. vector_postfit re-runs fully spliced
    # (the labels are unchanged since the last pass), so the reported audit is
    # byte-identical to computing these metrics on every pass.
    validations["semantic"] = validate_semantic_preservation(
        original, source, vector_rgb=vector_rgb,
        protected_features=segmentation.protected_features,
        segmentation_labels=segmentation.labels, vector_labels=prev_vector_labels,
        maximum_deviation=tolerance, invariant_cache=semantic_cache)
    write_trace_html(subject_dir, width, height)
    topology["shared_curve_fit"] = shared_fit_audit
    dense_fallbacks = [identity for identity, state in recovery.items()
                       if int(state["stage"]) >= 4]
    if recovery:
        warnings.append(
            f"{len(recovery)} shared curves used ordered local recovery after "
            "protected-feature validation")
    if dense_fallbacks:
        by_identity = {chain.identity: chain for chain in boundary_track.chains}
        faded = Image.blend(Image.fromarray(source),
                            Image.new("RGB", (width, height), "white"), 0.82)
        draw = ImageDraw.Draw(faded)
        for identity in dense_fallbacks:
            chain = by_identity[identity]
            colours = [index for index in chain.pair if index in included]
            colour = tuple(palette[colours[0]]["rgb"]) if colours else (0, 0, 0)
            draw.line(chain.points, fill=colour, width=2, joint="curve")
        faded.save(fallback_image)
        tightened_boundary_overlay(
            [by_identity[identity].points for identity in dense_fallbacks],
            width, height).save(fallback_overlay)
        warnings.append(
            f"{len(dense_fallbacks)} shared boundaries required a reported dense fallback; "
            f"see {fallback_image.name}")
    else:
        fallback_image.unlink(missing_ok=True)
        fallback_overlay.unlink(missing_ok=True)
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
    for variant, layers in variant_stats.items():
        if sum(layer["nodes"] for layer in layers) > settings.soft_document_nodes:
            warnings.append(f"traced-{variant}.svg exceeds the soft document-node limit")
    audit = {"subject": subject_dir.name,
             "sources": {"original": str(original_path), "flattened": str(flattened_path),
                         "palette": str(palette_path),
                         "segmentation": str(segmentation_path)},
             "raster_dimensions": [width, height],
             "mode": "physical" if physical_mm is not None else "pixels",
             "physical_dimensions_mm": list(physical_mm) if physical_mm is not None else None,
             "settings": asdict(settings),
             "cleanup_settings": asdict(physical),
             "backend": {"tracer": "shared raster boundary graph",
                         "planar_geometry": f"Shapely {shapely.__version__}"},
             "colours": {"included": [palette[i] for i in order], "omitted": palette[omitted] if omitted is not None else None},
             "layer_order": {"inferred_bottom_to_top": ([palette[i]["name"] for i in automatic]
                                                        if inferred else None),
                             "not_inferred": None if inferred else ORDER_NOT_INFERRED,
                             "effective_bottom_to_top": [palette[i]["name"] for i in order],
                             "override": [palette[i]["name"] for i in override], "warnings": order_warnings},
             "cleanup": cleanup_summary, "geometry": variant_stats, "topology": topology,
             "validation": validations, "warnings": warnings,
             "raster_partition_validation": {"edge_has_gaps_or_overlaps": False,
                                             "method": "global polygonization of once-simplified shared boundaries"}}
    audit["determinism"] = {"input_sha256": hashlib.sha256(
        flattened_path.read_bytes() + palette_path.read_bytes()
        + segmentation_path.read_bytes()).hexdigest()}
    audit["preview"] = {"file": "traced.png",
                        **{key: value for key, value in preview_plan(
                            subject_dir / "traced-overlap.svg").items() if key != "arguments"}}
    tick(0.9, "Writing artifacts")
    (subject_dir / "trace.json").write_text(json.dumps(audit, indent=2) + "\n")
    tick(0.95, "Rendering preview")
    plan = preview_plan(subject_dir / "traced-overlap.svg")
    subprocess.run([
        str(renderer), str(subject_dir / "traced-overlap.svg"),
        "--export-type=png", *plan["arguments"],
        f"--export-filename={subject_dir / 'traced.png'}",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tick(1.0, "Complete")
    return audit
