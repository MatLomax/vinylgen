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

import numpy as np
from PIL import Image, ImageDraw
import shapely
from scipy import ndimage
from shapely.geometry import box

from vinylgen.colour import colour_names
from vinylgen.contracts import SegmentationBundle, TraceSettings
from vinylgen.cutter_cleanup import cleanup_labels
from vinylgen.curve_fit import geometry_components, shared_boundary_components
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


def infer_order(labels: np.ndarray, included: list[int], palette: list[dict]) -> tuple[list[int], list[str]]:
    # Large, canvas-touching colours make the most plausible lower layers.
    h, w = labels.shape
    rows = []
    for index in included:
        mask = labels == index
        border = int(mask[0].sum() + mask[-1].sum() + mask[:, 0].sum() + mask[:, -1].sum())
        rows.append((-border, -int(mask.sum()), str(palette[index]["hex"]), index))
    ordered = sorted(rows)
    order = [row[-1] for row in ordered]
    warnings = []
    if any(ordered[i][:2] == ordered[i + 1][:2] for i in range(len(ordered) - 1)):
        warnings.append(
            "Layer-order evidence tied adjacent colours; hexadecimal order broke the tie.")
    return order, warnings


def apply_order_override(inferred: list[int], requested: list[str] | None,
                         palette: list[dict]) -> tuple[list[int], list[int]]:
    if not requested:
        return inferred, []
    specified = [resolve_colour(value, palette) for value in requested]
    if len(specified) != len(set(specified)):
        raise ValueError("layer order contains a duplicate colour")
    missing = [index for index in inferred if index not in specified]
    return [*specified, *missing], specified


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



def trace_subject(subject_dir: Path, settings: TraceSettings, name_overrides: dict[str, str]) -> dict:
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
    omitted = resolve_colour(settings.remove_colour, palette)
    included = [i for i in range(len(palette)) if i != omitted]
    if not included:
        raise ValueError("cannot omit the only palette colour")
    inferred, order_warnings = infer_order(labels, included, palette)
    order, override = apply_order_override(inferred, settings.layer_order, palette)
    if any(i not in included for i in order):
        raise ValueError("omitted colour cannot occur in layer order")
    if settings.width_mm is not None and settings.height_mm is None:
        settings.height_mm = settings.width_mm * height / width
    elif settings.height_mm is not None and settings.width_mm is None:
        settings.width_mm = settings.height_mm * width / height
    units_per_pixel = settings.width_mm / width if settings.width_mm is not None else 1.0
    if settings.max_deviation is None:
        settings.max_deviation = 2.0 * units_per_pixel
    working, cleanup = cleanup_labels(labels, included, palette, settings, units_per_pixel)
    overlap_px = settings.overlap / units_per_pixel
    tracing_settings = TraceSettings(**asdict(settings))
    if tracing_settings.width_mm is not None:
        if tracing_settings.max_deviation is not None:
            tracing_settings.max_deviation /= units_per_pixel
    warnings = list(order_warnings)
    scratch = Path(__file__).resolve().parents[1] / ".tmp"
    scratch.mkdir(exist_ok=True)
    tolerance = float(tracing_settings.max_deviation or 0.0)
    partition, boundary_track, topology = shared_boundary_polygons(
        working, included, segmentation.palette_costs,
        protected_features=segmentation.protected_features)
    if topology["partition_gap_area"] > 1e-7 or topology["overlap_area"] > 1e-7:
        raise RuntimeError(f"shared-boundary partition failed: {topology}")
    fallback_image = subject_dir / "trace-fallbacks.png"
    semantic_render = scratch / f"{subject_dir.name}-semantic-validation.png"
    renderer = Path(__file__).resolve().parents[1] / "scripts" / "inkscape-headless"
    palette_rgb = segmentation.palette_rgb.astype(np.int32)
    canvas = box(0, 0, width, height)

    def build_and_validate(recovery: dict[str, dict[str, object]]):
        components_by_colour, fit_audit = shared_boundary_components(
            boundary_track, included, tracing_settings, recovery)
        variant_stats: dict[str, list[dict]] = {"overlap": [], "edge": []}
        roots = {name: svg_document(width, height, settings) for name in variant_stats}
        for position, index in enumerate(order):
            edge_geometry = partition[index]
            edge_components = components_by_colour[index]
            edge_stats = add_layer(roots["edge"], palette[index], edge_components, "",
                                   subject_dir.name, "edge", 0.0)
            edge_stats["construction"] = "canonical shared-chain fit"
            edge_stats["source_colour_components"] = len(component_sizes(working == index))
            variant_stats["edge"].append(edge_stats)
            overlap_geometry = edge_geometry
            if overlap_px > 0 and position < len(order) - 1:
                overlap_geometry = edge_geometry.buffer(overlap_px, join_style=1).intersection(canvas)
            overlap_components = (edge_components if overlap_px == 0 else
                                  geometry_components(overlap_geometry, tracing_settings))
            overlap_stats = add_layer(roots["overlap"], palette[index], overlap_components, "",
                                      subject_dir.name, "overlap", 0.0)
            overlap_stats["construction"] = (
                "canonical shared-chain fit" if overlap_px == 0
                else "shared partition plus lower-layer overlap buffer")
            overlap_stats["raster_component_pixels_descending"] = component_sizes(working == index)
            variant_stats["overlap"].append(overlap_stats)
        for variant, root in roots.items():
            write_svg(subject_dir / f"traced-{variant}.svg", root)
        write_trace_html(subject_dir, width, height)
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
        vector_distances = np.sum(
            (vector_rgb.astype(np.int32)[:, :, None, :] - palette_rgb[None, None, :, :]) ** 2,
            axis=3, dtype=np.int64)
        vector_labels = vector_distances.argmin(axis=2).astype(np.int16)
        validations["semantic"] = validate_semantic_preservation(
            original, source, vector_rgb=vector_rgb,
            protected_features=segmentation.protected_features,
            segmentation_labels=segmentation.labels, vector_labels=vector_labels,
            maximum_deviation=tolerance)
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
    for fit_attempt in range(5):
        variant_stats, validations, vector_rgb, shared_fit_audit = build_and_validate(
            recovery)
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
            recovery)
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
        warnings.append(
            f"{len(dense_fallbacks)} shared boundaries required a reported dense fallback; "
            f"see {fallback_image.name}")
    else:
        fallback_image.unlink(missing_ok=True)
    semantic_inspection = subject_dir / "trace-semantic-warnings.png"
    if not validations["semantic"]["passed"]:
        missing = semantic_warning_mask(
            original, vector_rgb, maximum_deviation=tolerance)
        faded = Image.blend(Image.fromarray(original),
                            Image.new("RGB", (width, height), "white"), 0.82)
        overlay = np.asarray(faded).copy()
        overlay[missing] = (220, 35, 35)
        Image.fromarray(overlay).save(semantic_inspection)
        warnings.append(
            f"semantic feature audit found {int(missing.sum())} source pixels beyond "
            f"the {tolerance:g}px deviation limit; see {semantic_inspection.name}")
    else:
        semantic_inspection.unlink(missing_ok=True)
    for variant, layers in variant_stats.items():
        if sum(layer["nodes"] for layer in layers) > settings.soft_document_nodes:
            warnings.append(f"traced-{variant}.svg exceeds the soft document-node limit")
    audit = {"subject": subject_dir.name,
             "sources": {"original": str(original_path), "flattened": str(flattened_path),
                         "palette": str(palette_path),
                         "segmentation": str(segmentation_path)},
             "raster_dimensions": [width, height],
             "mode": "physical" if settings.width_mm is not None else "pixels",
             "physical_dimensions_mm": [settings.width_mm, settings.height_mm] if settings.width_mm is not None else None,
             "settings": asdict(settings),
             "backend": {"tracer": "shared raster boundary graph",
                         "planar_geometry": f"Shapely {shapely.__version__}"},
             "colours": {"included": [palette[i] for i in order], "omitted": palette[omitted] if omitted is not None else None},
             "layer_order": {"inferred_bottom_to_top": [palette[i]["name"] for i in inferred],
                             "effective_bottom_to_top": [palette[i]["name"] for i in order],
                             "override": [palette[i]["name"] for i in override], "warnings": order_warnings},
             "cleanup": cleanup, "geometry": variant_stats, "topology": topology,
             "validation": validations, "warnings": warnings,
             "raster_partition_validation": {"edge_has_gaps_or_overlaps": False,
                                             "method": "global polygonization of once-simplified shared boundaries"}}
    audit["determinism"] = {"input_sha256": hashlib.sha256(
        flattened_path.read_bytes() + palette_path.read_bytes()
        + segmentation_path.read_bytes()).hexdigest()}
    (subject_dir / "trace.json").write_text(json.dumps(audit, indent=2) + "\n")
    subprocess.run([
        str(renderer), str(subject_dir / "traced-overlap.svg"),
        "--export-type=png", "--export-dpi=768",
        f"--export-filename={subject_dir / 'traced.png'}",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return audit
