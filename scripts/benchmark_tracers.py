#!/usr/bin/env python3
"""Benchmark Vinylgen's tracer against Potrace and VTracer on the synthetic corpus.

Every (tier, case) of the corpus (``--corpus synthetic``, the default: the
regression corpus, ``regression.all_runs()``; ``--corpus icons``: both tiers of
``vinylgen.fidelity.icons.icon_cases()``) is traced by our trace stage exactly as
the fidelity sweep traces it, and the label map our trace stage received (the
label render in the labels tier, our palette stage's labels in the rgb tier) is
handed to Potrace and VTracer, so all three trace the same colours. All three are
judged with the regression bounds (``bounds.evaluate``) and summarised with the
sweep's statistics (``sweep.group_stats``) per tool, tier, family and canvas size.

A third tier, ``rgb-flat``, gives every tracer the same input: our trace stage
traces the rgb tier's palette-stage labels with hard-edged costs
(``harness.trace_labels``), so our sub-pixel edge information no longer counts.
It is judged by the rgb tier's bounds with the rgb run's palette matching, and
its Potrace and VTracer rows are their rgb rows, whose input it shares.

- Potrace 1.16 traces stacked scans, as Inkscape's multi-colour trace does: the
  colours ordered by area, largest at the bottom, each traced as its mask united
  with the masks of every colour above it (``-t 0``: no speckle removal; corner
  threshold ``--potrace-alphamax`` and curve-optimisation tolerance
  ``--potrace-opttolerance``, 0 meaning ``-n``, default Potrace's 1 and 0.2;
  output quantised to 1/1000 px).
- VTracer 0.6.4 traces the flattened RGB in colour mode with its default stacked
  hierarchy (spline mode, ``--filter_speckle 1`` which keeps every patch,
  ``--gradient_step 1`` and ``--color_precision 8`` which keep the exact palette;
  ``--vtracer-corner-threshold``, ``--vtracer-splice-threshold`` and
  ``--vtracer-segment-length``, default VTracer's 60, 45 and 4).

Neither writes a partition, so each tool's shapes are stacked (Potrace's scans in
order, VTracer's paths in document order) and each point belongs to the topmost
shape covering it; a point no shape covers belongs to no colour (it counts as
missing area), and the partition is clipped to the canvas, where a viewer shows
it. The nodes judged are the tool's own
nodes on its colours' visible boundaries, typed by their handles (a cusp where
the incoming and outgoing tangents differ by more than ``CUSP_TURN_DEG``,
otherwise a smooth node when the two handles are equally long and a symmetric
one when not), plus every point where the stacking clips one shape against
another, which is a cusp.

Beyond the sweep's statistics, each trace is scored identically for every tool on:

- required-vertex recall per kind (genuine corners, edge points where a boundary
  meets the frame, junctions) and genuine-corner F1 (genuine-corner recall with
  the pooled cusp precision): shared boundaries anchor junctions and edge points
  by construction, so pooled recall rewards that architecture;
- curve quality off the vertex discs and zones (``metrics.curve_quality``, one
  function for the traced and truth rings): extra inflections per 100 px of truth
  boundary and the tangent-direction error at each traced sample (p95 and max per
  case, median over cases);
- node placement off the zones (``metrics.node_placement``): the share of the
  scene's authored nodes (outline segment endpoints on the drawn boundary, off
  the canvas corners) with a traced node within the boundary budget D, and the
  share of traced nodes with no authored node within D.

Neither tool knows the canvas frame, so a shape along it may be rounded a
fraction of a pixel inside it, exposing a hairline of the colour beneath along the
canvas edge; that hairline is a colour boundary far from every true one. The
summary counts the Hausdorff outliers whose worst point lies at the frame.

``--grid`` traces the labels tier only: ours once per case, and each tool at
every point of its tuning grid (``POTRACE_GRID``, ``VTRACER_GRID``), and names
each tool's best setting: the highest genuine-corner F1 among its settings whose
median local Hausdorff is within 10% of its default setting's.

Results go to ``.tmp/benchmark/<timestamp>/``: ``rows.jsonl``, ``meta.json`` and
``summary.md``, or with ``--grid`` ``.tmp/benchmark/<timestamp>-grid/`` with
``grid.md`` and ``grid.jsonl`` (one summary per grid point) instead of the summary.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOOLS = ("vinylgen", "potrace", "vtracer")
POTRACE = "/usr/bin/potrace"
VTRACER = str(Path.home() / ".local" / "bin" / "vtracer")
CUSP_TURN_DEG = 1.0
# Two handles are equally long (a smooth rather than a symmetric node) within this
# relative difference.
BALANCED = 0.01
# A node lies on a visible boundary within this distance (px).
ON_BOUNDARY = 1e-6
_DECIMALS = 6
SVG_NS = "{http://www.w3.org/2000/svg}"


# --- SVG path data ---------------------------------------------------------------------

_TOKEN = re.compile(r"[MmLlHhVvCcSsQqTtZzAa]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_d(d: str) -> list[tuple[tuple[float, float], list[tuple], bool]]:
    """Subpaths ``(start, segments, closed)`` of SVG path data, absolute, with
    segments ``("L", end)`` and ``("C", c1, c2, end)`` (quadratics raised to
    cubics). Arcs are not supported (neither tool writes them)."""
    tokens = _TOKEN.findall(d)
    subpaths, segments, start, current = [], None, (0.0, 0.0), (0.0, 0.0)
    last_control = None
    i, command = 0, None

    def number():
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    def close_current(closed):
        if segments is not None:
            subpaths.append((start, segments, closed))

    while i < len(tokens):
        if tokens[i].isalpha():
            command = tokens[i]
            i += 1
            if command in "Zz":
                close_current(True)
                segments, current, last_control = None, start, None
                continue
        elif command is None:
            raise ValueError("path data starts with a number")
        relative = command.islower()
        base = current if relative else (0.0, 0.0)
        kind = command.upper()
        if kind == "M":
            close_current(False)
            start = current = (base[0] + number(), base[1] + number())
            segments, last_control = [], None
            command = "l" if relative else "L"
            continue
        if segments is None:
            segments = []
            start = current
        if kind == "L":
            end = (base[0] + number(), base[1] + number())
            segments.append(("L", end))
            last_control = None
        elif kind == "H":
            value = number()
            end = (current[0] + value if relative else value, current[1])
            segments.append(("L", end))
            last_control = None
        elif kind == "V":
            value = number()
            end = (current[0], current[1] + value if relative else value)
            segments.append(("L", end))
            last_control = None
        elif kind == "C":
            c1 = (base[0] + number(), base[1] + number())
            c2 = (base[0] + number(), base[1] + number())
            end = (base[0] + number(), base[1] + number())
            segments.append(("C", c1, c2, end))
            last_control = c2
        elif kind == "S":
            c1 = (2 * current[0] - last_control[0], 2 * current[1] - last_control[1]) \
                if last_control else current
            c2 = (base[0] + number(), base[1] + number())
            end = (base[0] + number(), base[1] + number())
            segments.append(("C", c1, c2, end))
            last_control = c2
        elif kind == "Q":
            q = (base[0] + number(), base[1] + number())
            end = (base[0] + number(), base[1] + number())
            c1 = (current[0] + 2 / 3 * (q[0] - current[0]), current[1] + 2 / 3 * (q[1] - current[1]))
            c2 = (end[0] + 2 / 3 * (q[0] - end[0]), end[1] + 2 / 3 * (q[1] - end[1]))
            segments.append(("C", c1, c2, end))
            last_control = None
        else:
            raise ValueError(f"unsupported path command {command!r}")
        current = end
    close_current(False)
    return subpaths


def _transform_matrix(text: str | None) -> np.ndarray:
    matrix = np.eye(3)
    if not text:
        return matrix
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text):
        values = [float(v) for v in re.split(r"[\s,]+", args.strip()) if v]
        if name == "translate":
            step = np.array([[1, 0, values[0]], [0, 1, values[1] if len(values) > 1 else 0],
                             [0, 0, 1]])
        elif name == "scale":
            sy = values[1] if len(values) > 1 else values[0]
            step = np.diag([values[0], sy, 1.0])
        elif name == "matrix":
            a, b, c, d, e, f = values
            step = np.array([[a, c, e], [b, d, f], [0, 0, 1]])
        else:
            raise ValueError(f"unsupported transform {name!r}")
        matrix = matrix @ step
    return matrix


def _apply(matrix: np.ndarray, subpaths):
    def point(p):
        x, y, _ = matrix @ (p[0], p[1], 1.0)
        return (float(x), float(y))
    return [(point(start), [(segment[0], *map(point, segment[1:])) for segment in segments], closed)
            for start, segments, closed in subpaths]


def svg_paths(svg_path: Path) -> list[tuple[str, list]]:
    """Every filled ``<path>`` of an SVG in document order: ``(fill, subpaths)`` in
    user coordinates, ancestor and own transforms applied."""
    root = ET.parse(svg_path).getroot()
    found = []

    def walk(element, matrix, fill):
        matrix = matrix @ _transform_matrix(element.attrib.get("transform"))
        fill = element.attrib.get("fill", fill)
        if element.tag == f"{SVG_NS}path":
            found.append((str(fill).upper(), _apply(matrix, parse_d(element.attrib["d"]))))
        for child in element:
            walk(child, matrix, fill)

    walk(root, np.eye(3), "#000000")
    return found


# --- node types ------------------------------------------------------------------------

def _length(v) -> float:
    return math.hypot(v[0], v[1])


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _tangents(p0, segment):
    """The outgoing and incoming tangent (handle) vectors of a segment from ``p0``."""
    if segment[0] == "L":
        chord = _sub(segment[1], p0)
        return chord, chord
    _, c1, c2, p3 = segment
    out = next((v for v in (_sub(c1, p0), _sub(c2, p0), _sub(p3, p0)) if _length(v) > 1e-9),
               (0.0, 0.0))
    inc = next((v for v in (_sub(p3, c2), _sub(p3, c1), _sub(p3, p0)) if _length(v) > 1e-9),
               (0.0, 0.0))
    return out, inc


def _turn(incoming, outgoing) -> float:
    cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
    dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
    return math.degrees(math.atan2(abs(cross), dot))


def typed_nodes(start, segments, closed: bool) -> list[tuple[float, float, str]]:
    """The subpath's nodes with Inkscape type letters (``c`` cusp, ``s``
    symmetric, ``z`` smooth) read from the handles. Zero-length segments are
    dropped; an open subpath's two ends are cusps."""
    kept, current = [], start
    for segment in segments:
        points = [current, *segment[1:]]
        if max(_length(_sub(p, current)) for p in points) > 1e-9:
            kept.append(segment)
            current = segment[-1]
    if closed and kept and _length(_sub(kept[-1][-1], start)) > 1e-9:
        kept.append(("L", start))
    if not kept:
        return []
    starts = [start] + [segment[-1] for segment in kept[:-1]]
    tangents = [_tangents(p0, segment) for p0, segment in zip(starts, kept)]
    nodes = []
    count = len(kept)
    for k in range(count if closed else count + 1):
        position = starts[k] if k < count else kept[-1][-1]
        if not closed and (k == 0 or k == count):
            nodes.append((position[0], position[1], "c"))
            continue
        incoming = tangents[k - 1][1]
        outgoing = tangents[k % count][0]
        if _turn(incoming, outgoing) > CUSP_TURN_DEG:
            letter = "c"
        else:
            a, b = _length(incoming), _length(outgoing)
            letter = "z" if abs(a - b) <= BALANCED * max(a, b) else "s"
        nodes.append((position[0], position[1], letter))
    return nodes


# --- stacking into a partition ---------------------------------------------------------

def stacked_trace(shapes: list[tuple[str, list]], width: float, height: float) -> dict:
    """The visible partition of ``shapes`` (``(fill, subpaths)``, bottom to top) in
    ``metrics.parse_traced_svg``'s layout: one layer per fill with one path whose
    rings are the fill's visible area on the canvas (a viewer shows nothing beyond
    it) and whose nodes are the shapes' own nodes on that area's boundary plus the
    clip points."""
    import shapely
    from shapely.ops import unary_union

    from vinylgen.fidelity.metrics import flatten_subpath, path_geometry

    geometries, raw_vertices, raw_nodes = [], set(), []
    for fill, subpaths in shapes:
        rings, closed_flags, nodes = [], [], []
        for start, segments, closed in subpaths:
            if not segments:
                continue
            ring = flatten_subpath(start, segments, True)
            rings.append(ring)
            closed_flags.append(True)
            raw_vertices.update((round(x, _DECIMALS), round(y, _DECIMALS)) for x, y in ring)
            nodes.extend(typed_nodes(start, segments, True))
        geometry = path_geometry({"rings": rings, "closed": closed_flags})
        geometries.append(geometry)
        raw_nodes.append(nodes)
    covered = None
    visible_by_fill: dict[str, list] = {}
    nodes_by_fill: dict[str, list] = {}
    for (fill, _), geometry, nodes in reversed(list(zip(shapes, geometries, raw_nodes))):
        visible = geometry if covered is None else geometry.difference(covered)
        covered = geometry if covered is None else covered.union(geometry)
        visible_by_fill.setdefault(fill, []).append(visible)
        nodes_by_fill.setdefault(fill, []).extend(nodes)
    layers, clip_points = [], set()
    canvas = shapely.box(0.0, 0.0, float(width), float(height))
    for fill in visible_by_fill:
        area = unary_union(visible_by_fill[fill]).intersection(canvas)
        polygons = [area] if area.geom_type == "Polygon" else [
            g for g in getattr(area, "geoms", []) if g.geom_type == "Polygon"]
        rings = []
        for polygon in polygons:
            if polygon.is_empty:
                continue
            rings.append(np.asarray(polygon.exterior.coords, dtype=np.float64))
            rings.extend(np.asarray(hole.coords, dtype=np.float64) for hole in polygon.interiors)
        nodes = []
        if rings:
            boundary = area.boundary
            candidates = nodes_by_fill[fill]
            if candidates:
                points = shapely.points(np.asarray([(x, y) for x, y, _ in candidates]))
                near = shapely.distance(points, boundary) <= ON_BOUNDARY
                nodes += [node for node, keep in zip(candidates, near) if keep]
            own = {(round(x, _DECIMALS), round(y, _DECIMALS)) for x, y, _ in nodes}
            for ring in rings:
                for x, y in ring[:-1]:
                    key = (round(x, _DECIMALS), round(y, _DECIMALS))
                    if key not in raw_vertices and key not in own:
                        own.add(key)
                        clip_points.add(key)
                        nodes.append((float(x), float(y), "c"))
        layers.append({"fill": fill, "label": fill,
                       "paths": [{"id": fill, "rings": rings, "closed": [True] * len(rings),
                                  "nodes": nodes}]})
    return {"width": float(width), "height": float(height), "layers": layers,
            "clip_points": clip_points}


# --- the two tools ---------------------------------------------------------------------

def _pbm(mask: np.ndarray, path: Path) -> None:
    height, width = mask.shape
    packed = np.packbits(mask.astype(np.uint8), axis=1)
    path.write_bytes(f"P4\n{width} {height}\n".encode() + packed.tobytes())


# Each tool's own defaults (Potrace 1.16, VTracer 0.6.4), which the settings
# options start from.
POTRACE_DEFAULTS = {"alphamax": 1.0, "opttolerance": 0.2}
VTRACER_DEFAULTS = {"corner_threshold": 60.0, "splice_threshold": 45.0, "segment_length": 4.0}
# VTracer 0.6.4 rejects a segment length outside this range.
VTRACER_SEGMENT_RANGE = (3.5, 10.0)


def run_potrace(labels: np.ndarray, fills: list[str], work: Path, *,
                alphamax: float = POTRACE_DEFAULTS["alphamax"],
                opttolerance: float = POTRACE_DEFAULTS["opttolerance"]) -> list[tuple[str, list]]:
    """Stacked scans, as Inkscape's multi-colour Potrace trace stacks them: the
    colours ordered by area, largest at the bottom, and each traced as the union
    of its mask and the masks of every colour above it, so each scan lies under
    the ones above and no gap opens between neighbours. ``alphamax`` is Potrace's
    corner threshold (``-a``); ``opttolerance`` its curve-optimisation tolerance
    (``-O``), 0 turning curve optimisation off (``-n``)."""
    work.mkdir(parents=True, exist_ok=True)
    counts = np.bincount(labels.ravel(), minlength=len(fills))
    order = sorted(np.flatnonzero(counts), key=lambda i: (-counts[i], i))
    rank = np.full(len(fills), -1)
    rank[order] = np.arange(len(order))
    ranks = rank[labels]
    options = ["-a", f"{alphamax:g}"] + (["-n"] if opttolerance == 0
                                         else ["-O", f"{opttolerance:g}"])
    shapes = []
    for position, index in enumerate(order):
        pbm, svg = work / f"c{index}.pbm", work / f"c{index}.svg"
        _pbm(ranks >= position, pbm)
        subprocess.run([POTRACE, "-b", "svg", "-t", "0", "-u", "1000", *options, "-o", str(svg),
                        str(pbm)], check=True, capture_output=True)
        subpaths = [sub for _, subs in svg_paths(svg) for sub in subs]
        if subpaths:
            shapes.append((fills[index], subpaths))
    return shapes


def run_vtracer(labels: np.ndarray, fills: list[str], rgb: np.ndarray, work: Path, *,
                corner_threshold: float = VTRACER_DEFAULTS["corner_threshold"],
                splice_threshold: float = VTRACER_DEFAULTS["splice_threshold"],
                segment_length: float = VTRACER_DEFAULTS["segment_length"]
                ) -> list[tuple[str, list]]:
    """The flattened RGB traced in colour mode; each path's fill is mapped to the
    nearest palette colour. Paths stack in document order. ``corner_threshold``,
    ``splice_threshold`` (degrees) and ``segment_length`` (px) are VTracer's
    spline-fitting options of those names."""
    from PIL import Image
    work.mkdir(parents=True, exist_ok=True)
    png, svg = work / "flattened.png", work / "vtracer.svg"
    svg.unlink(missing_ok=True)
    Image.fromarray(rgb[labels], "RGB").save(png)
    subprocess.run([VTRACER, "--input", str(png), "--output", str(svg), "--colormode", "color",
                    "--hierarchical", "stacked", "--mode", "spline", "--filter_speckle", "1",
                    "--color_precision", "8", "--gradient_step", "1", "--path_precision", "4",
                    "--corner_threshold", f"{corner_threshold:g}",
                    "--splice_threshold", f"{splice_threshold:g}",
                    "--segment_length", f"{segment_length:g}"],
                   check=True, capture_output=True)
    palette = np.asarray([[int(fill[i:i + 2], 16) for i in (1, 3, 5)] for fill in fills])
    shapes = []
    for fill, subpaths in svg_paths(svg):
        colour = np.asarray([int(fill[i:i + 2], 16) for i in (1, 3, 5)])
        nearest = int(np.abs(palette - colour).sum(axis=1).argmin())
        shapes.append((fills[nearest], subpaths))
    return shapes


def run_tool(tool: str, labels: np.ndarray, fills: list[str], rgb: np.ndarray, work: Path,
             setting: dict) -> list[tuple[str, list]]:
    """``run_potrace`` or ``run_vtracer`` with ``setting`` as its options."""
    if tool == "potrace":
        return run_potrace(labels, fills, work, **setting)
    return run_vtracer(labels, fills, rgb, work, **setting)


# --- worker ----------------------------------------------------------------------------

def _row(case, tier: str, tool: str, setting: dict | None = None) -> dict:
    from vinylgen.fidelity.sweep import case_family, case_size
    return {"case_id": case.case_id, "tier": tier, "tool": tool, "family": case_family(case),
            "tags": list(case.tags), "size": case_size(case), "ok": False, "error": None,
            "sub_resolution": None, "setting": dict(setting or {})}


def _context(case, case_bounds) -> dict:
    """What every trace of one (case, tier) is judged against beyond the bounds:
    the kept region (off the vertex discs and zones), the truth side of the curve
    quality, and the authored nodes on the truth's boundary off the zones."""
    from vinylgen.fidelity.bounds import _inside, _outside_discs
    from vinylgen.fidelity.metrics import authored_nodes, curve_reference
    excluded = case_bounds.excluded_zone

    def keep(points):
        return (_outside_discs(points, case_bounds.centres, case_bounds.radii)
                & ~_inside(excluded, points))

    authored = authored_nodes(case.scene, case_bounds.rings)
    return {"excluded": excluded, "keep": keep,
            "reference": curve_reference(case_bounds.rings, case_bounds.width,
                                         case_bounds.height, keep),
            "authored": authored[~_inside(excluded, authored)]}


def _judge(case, tier, run, case_bounds, traced, context=None) -> dict:
    """The per-case numbers of one trace: ``sweep.summarise_measure``'s, the
    frame and clip-point counts, required vertices found per kind, curve quality
    and node placement. ``traced`` is ``None`` for our own trace (``run.edge_svg``)."""
    from vinylgen.fidelity.bounds import (_boundary_nodes, _inside, _traced_layers, colour_map,
                                          evaluate)
    from vinylgen.fidelity.metrics import curve_quality, measure, node_placement, parse_traced_svg
    from vinylgen.fidelity.sweep import summarise_measure
    context = context or _context(case, case_bounds)
    if traced is None:
        traced = parse_traced_svg(run.edge_svg)
    mapping = colour_map(case_bounds, run)
    result = measure(run.edge_svg, case_bounds.truth, case.scene.palette, corner_radius=-1.0,
                     smooth_min_distance=-1.0, colour_map=mapping, traced=traced)
    evaluation = evaluate(case, tier, run, case_bounds, measured=result, traced=traced)
    worst = evaluation["local_hausdorff"]["worst_point"]
    width, height = result["canvas"]
    frame = (None if worst is None else
             min(worst[0], worst[1], width - worst[0], height - worst[1]))
    clips = traced.get("clip_points", set())
    clip_cusps = [cusp for cusp in evaluation["cusps"]
                  if (round(cusp["point"][0], _DECIMALS), round(cusp["point"][1], _DECIMALS)) in clips]
    by_kind: dict[str, list[int]] = {}
    for corner in evaluation["corners"]:
        if corner["role"] == "required":
            counts = by_kind.setdefault(corner["kind"], [0, 0])
            counts[0] += int(corner["found"])
            counts[1] += 1
    rings = _traced_layers(traced, mapping)[2]
    nodes = np.asarray([(x, y) for x, y, _ in _boundary_nodes(traced)],
                       dtype=np.float64).reshape(-1, 2)
    nodes = nodes[~_inside(context["excluded"], nodes)]
    return {**summarise_measure(result, evaluation), "worst_point": worst,
            "worst_frame_px": frame, "clip_cusps": len(clip_cusps),
            "clip_kinks": sum(1 for cusp in clip_cusps if cusp["kink"]),
            "required_by_kind": by_kind,
            "curve": curve_quality(rings, context["reference"]),
            "node_placement": node_placement(context["authored"], nodes, case_bounds.bound)}


def _our_trace(case, tier: str, case_dir: Path):
    """Our trace of one (tier, case) as the fidelity sweep traces it, and its bounds."""
    from vinylgen.contracts import TraceSettings
    from vinylgen.fidelity.families import case_palette_settings
    from vinylgen.fidelity.harness import trace_labels, trace_rgb
    from vinylgen.fidelity.sweep import _bounds, _render
    palette = case.scene.palette
    if tier == "labels":
        run = trace_labels((_render(case, tier), palette), case_dir, TraceSettings(),
                           subject=case.case_id)
    else:
        run = trace_rgb((_render(case, tier), palette), case_dir, TraceSettings(),
                        palette_options=case_palette_settings(case), subject=case.case_id)
    return run, _bounds(case, tier, run)


def _flat_trace(case, run, case_dir: Path):
    """Our trace of the rgb tier's palette-stage labels with hard-edged costs
    (``harness.trace_labels``): the input Potrace and VTracer receive. It carries
    the rgb run's palette and palette matching, so the rgb tier's bounds judge it
    exactly as they judge the rgb trace."""
    from vinylgen.contracts import SegmentationBundle, TraceSettings
    from vinylgen.fidelity.harness import trace_labels
    bundle = SegmentationBundle.load(run.subject_dir / "segmentation.npz", with_costs=False)
    palette = [{"name": row.get("name", row["hex"]), "rgb": row["rgb"]} for row in run.palette]
    flat = trace_labels((np.asarray(bundle.labels), palette), case_dir / "flat", TraceSettings(),
                        subject=case.case_id)
    return replace(flat, palette=run.palette, colour_map=dict(run.colour_map),
                   palette_matches=run.palette_matches, unmatched=run.unmatched)


def _tool_input(run):
    """The label map our trace received, its fills and palette RGB."""
    from vinylgen.contracts import SegmentationBundle
    bundle = SegmentationBundle.load(run.subject_dir / "segmentation.npz", with_costs=False)
    labels = np.asarray(bundle.labels)
    fills = [str(row["hex"]).upper() for row in run.palette]
    rgb = np.asarray([row["rgb"] for row in run.palette], dtype=np.uint8)
    return labels, fills, rgb


def _tool_row(case, tier, tool, setting, run, case_bounds, context, labels, fills, rgb,
              work: Path) -> dict:
    row = _row(case, tier, tool, setting)
    try:
        started = time.perf_counter()
        shapes = run_tool(tool, labels, fills, rgb, work, setting)
        seconds = time.perf_counter() - started
        height, width = labels.shape
        traced = stacked_trace(shapes, width, height)
        row.update(ok=True, sub_resolution=not case_bounds.geometric, trace_seconds=seconds,
                   summary=_judge(case, tier, run, case_bounds, traced, context))
    except Exception as error:
        row.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
    return row


def run_one(case, tier: str, work_dir: str, settings: dict | None = None) -> list[dict]:
    """Our trace of one (tier, case) and the two tools' traces of the same labels
    (``settings`` maps a tool to its options, each tool's defaults otherwise),
    each judged against the case's bounds. An rgb case also yields the
    ``rgb-flat`` tier: our trace of the palette stage's labels with hard-edged
    costs, and the tools' rgb rows, whose input is the same."""
    from vinylgen.fidelity.sweep import audit_summary
    settings = settings or {}
    case_dir = Path(work_dir) / tier / case.case_id
    rows = []
    ours = _row(case, tier, "vinylgen")
    rows.append(ours)
    tiers = [tier] + (["rgb-flat"] if tier == "rgb" else [])
    try:
        run, case_bounds = _our_trace(case, tier, case_dir)
        context = _context(case, case_bounds)
        ours.update(ok=True, sub_resolution=not case_bounds.geometric,
                    trace_seconds=run.trace_seconds,
                    summary={**_judge(case, tier, run, case_bounds, None, context),
                             **audit_summary(run.audit)})
    except Exception as error:
        ours.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        shutil.rmtree(case_dir, ignore_errors=True)
        return rows + [{**_row(case, name, "vinylgen"), "error": "our trace failed"}
                       for name in tiers[1:]] + [
            {**_row(case, name, tool, settings.get(tool)), "error": "our trace failed"}
            for name in tiers for tool in TOOLS[1:]]
    if tier == "rgb":
        flat_row = _row(case, "rgb-flat", "vinylgen")
        rows.append(flat_row)
        try:
            flat = _flat_trace(case, run, case_dir)
            flat_row.update(ok=True, sub_resolution=not case_bounds.geometric,
                            trace_seconds=flat.trace_seconds,
                            summary={**_judge(case, tier, flat, case_bounds, None, context),
                                     **audit_summary(flat.audit)})
        except Exception as error:
            flat_row.update(error=f"{type(error).__name__}: {error}",
                            traceback=traceback.format_exc())
    labels, fills, rgb = _tool_input(run)
    for tool in TOOLS[1:]:
        row = _tool_row(case, tier, tool, settings.get(tool, {}), run, case_bounds, context,
                        labels, fills, rgb, case_dir / tool)
        rows.append(row)
        if tier == "rgb":
            rows.append({**row, "tier": "rgb-flat"})
    shutil.rmtree(case_dir, ignore_errors=True)
    return rows


def run_grid_one(case, work_dir: str, grid: dict[str, list[dict]]) -> list[dict]:
    """The labels tier of one case: our trace once, and each tool at every
    setting of ``grid`` (tool to settings), all judged against the same bounds."""
    from vinylgen.fidelity.sweep import audit_summary
    tier = "labels"
    case_dir = Path(work_dir) / tier / case.case_id
    ours = _row(case, tier, "vinylgen")
    try:
        run, case_bounds = _our_trace(case, tier, case_dir)
        context = _context(case, case_bounds)
        ours.update(ok=True, sub_resolution=not case_bounds.geometric,
                    trace_seconds=run.trace_seconds,
                    summary={**_judge(case, tier, run, case_bounds, None, context),
                             **audit_summary(run.audit)})
    except Exception as error:
        ours.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        shutil.rmtree(case_dir, ignore_errors=True)
        return [ours] + [{**_row(case, tier, tool, setting), "error": "our trace failed"}
                         for tool, settings in grid.items() for setting in settings]
    rows = [ours]
    labels, fills, rgb = _tool_input(run)
    for tool, settings in grid.items():
        for setting in settings:
            rows.append(_tool_row(case, tier, tool, setting, run, case_bounds, context,
                                  labels, fills, rgb, case_dir / tool))
    shutil.rmtree(case_dir, ignore_errors=True)
    return rows


# --- summary ---------------------------------------------------------------------------

# A case's Hausdorff distance is an outlier beyond this many px; an outlier whose
# worst point lies within FRAME_PX of the canvas frame is a frame sliver (a
# frame-unaware tracer pulling a shape's edge off the canvas edge).
OUTLIER_PX = 5.0
FRAME_PX = 1.0
KINDS = ("corner", "edge", "junction")
COLUMNS = ("tool", "cases", "failed", "Hausdorff median px", "Hausdorff p95 px",
           "outliers > 5 px", "of them frame slivers", "mean dist px",
           "misassigned %", "required recall", "genuine-corner recall", "edge-point recall",
           "junction recall", "cusp precision", "corner F1", "genuine-corner F1", "kinks",
           "kinks/case", "of kinks at clip points", "extra inflections/100 px",
           "tangent p95 deg (median case)", "tangent max deg (median case)",
           "authored-node recall", "stray nodes", "node ratio (geo)", "node ratio (median)",
           "topology mismatches", "trace s (median)")
# A tool's best grid setting keeps its median local Hausdorff within this factor
# of its default setting's.
GRID_HAUSDORFF_SLACK = 1.10
POTRACE_GRID = [{"alphamax": alphamax, "opttolerance": opttolerance}
                for alphamax in (0.2, 0.4, 0.6, 0.8, 1.0, 1.2) for opttolerance in (0.0, 0.2, 0.5)]
# The segment lengths start at VTracer's minimum (``VTRACER_SEGMENT_RANGE``).
VTRACER_GRID = [{"corner_threshold": corner, "splice_threshold": splice, "segment_length": length}
                for corner in (30.0, 45.0, 60.0, 90.0) for splice in (30.0, 45.0, 60.0)
                for length in (3.5, 4.0, 6.0)]


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, str)):
        return str(value)
    return f"{value:.{digits}g}"


def _outliers(rows: list[dict]) -> tuple[int, int]:
    outliers = [row["summary"] for row in rows if row["ok"] and not row.get("sub_resolution")
                and (row["summary"].get("local_hausdorff_px") or 0) > OUTLIER_PX]
    return len(outliers), sum(1 for s in outliers if (s.get("worst_frame_px") or 0) <= FRAME_PX)


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def benchmark_stats(rows: list[dict]) -> dict:
    """``sweep.group_stats`` of a group of rows plus the benchmark's own figures:
    required-vertex recall per kind (pooled found over true), genuine-corner F1
    (the harmonic mean of the corner kind's recall and the pooled cusp
    precision), extra inflections per 100 px (pooled over the geometric cases:
    traced minus truth inflections over the kept truth length), the median over
    geometric cases of each case's p95 and maximum tangent-direction error, and
    pooled authored-node recall and stray-node share. Rows from before these
    figures existed contribute nothing to them."""
    from vinylgen.fidelity.sweep import _f1, _stat, group_stats
    stats = group_stats(rows)
    ok = [row for row in rows if row["ok"]]
    geometric = [row["summary"] for row in ok if not row.get("sub_resolution")]
    kinds = {}
    for kind in KINDS:
        found = sum(row["summary"].get("required_by_kind", {}).get(kind, [0, 0])[0] for row in ok)
        true = sum(row["summary"].get("required_by_kind", {}).get(kind, [0, 0])[1] for row in ok)
        kinds[kind] = {"found": found, "true": true, "recall": _ratio(found, true)}
    curves = [summary["curve"] for summary in geometric if "curve" in summary]
    extra = sum(c["inflections_traced"] - c["inflections_truth"] for c in curves)
    length = sum(c["truth_length_px"] for c in curves)
    placement = [row["summary"]["node_placement"] for row in ok
                 if "node_placement" in row["summary"]]
    stats.update(
        required_by_kind=kinds,
        genuine_corner_recall=kinds["corner"]["recall"],
        genuine_corner_f1=_f1(kinds["corner"]["recall"], stats["cusp_precision"]),
        extra_inflections_per_100px=(100.0 * extra / length) if length > 0 else None,
        tangent_p95_deg=_stat([c["tangent_p95_deg"] for c in curves
                               if c["tangent_p95_deg"] is not None], "median"),
        tangent_max_deg=_stat([c["tangent_max_deg"] for c in curves
                               if c["tangent_max_deg"] is not None], "median"),
        authored_node_recall=_ratio(sum(p["recalled"] for p in placement),
                                    sum(p["authored"] for p in placement)),
        stray_nodes=_ratio(sum(p["stray"] for p in placement),
                           sum(p["traced"] for p in placement)))
    return stats


def _line(tool: str, rows: list[dict]) -> str:
    stats = benchmark_stats(rows)
    misassigned = stats["misassigned_fraction"]
    kinds = stats["required_by_kind"]
    cells = [tool, stats["cases"], stats["failed"], stats["hausdorff_px"]["median"],
             stats["hausdorff_px"]["p95"], *_outliers(rows), stats["mean_boundary_px"],
             None if misassigned is None else 100 * misassigned, stats["required_corner_recall"],
             *(kinds[kind]["recall"] for kind in KINDS),
             stats["cusp_precision"], stats["corner_f1"], stats["genuine_corner_f1"],
             stats["kinks"]["total"], stats["kinks"]["per_case"],
             sum(row["summary"].get("clip_kinks", 0) for row in rows if row["ok"]),
             stats["extra_inflections_per_100px"], stats["tangent_p95_deg"],
             stats["tangent_max_deg"], stats["authored_node_recall"], stats["stray_nodes"],
             stats["node_ratio"]["geometric_mean"],
             stats["node_ratio"]["median"], stats["topology"]["cases_mismatched"],
             stats["seconds"]["trace_median"]]
    return "| " + " | ".join(_fmt(cell) for cell in cells) + " |"


def setting_label(tool: str, setting: dict) -> str:
    """A tool setting in its command-line letters (``-n`` for Potrace without curve
    optimisation)."""
    if tool == "potrace":
        opt = setting.get("opttolerance", POTRACE_DEFAULTS["opttolerance"])
        return (f"-a {setting.get('alphamax', POTRACE_DEFAULTS['alphamax']):g} "
                + ("-n" if opt == 0 else f"-O {opt:g}"))
    if tool == "vtracer":
        merged = {**VTRACER_DEFAULTS, **setting}
        return (f"corner {merged['corner_threshold']:g}, splice {merged['splice_threshold']:g}, "
                f"segment {merged['segment_length']:g}")
    return "defaults"


def _method_lines(meta: dict) -> list[str]:
    settings = meta.get("settings", {})
    return ["- Judged with the synthetic regression bounds, statistics as in "
            "`sweep.group_stats` (Hausdorff off the vertex discs and zones; recall of "
            "required vertices, pooled and per kind: genuine corners, edge points, "
            "junctions; cusp precision = cusps that are no kink; F1 = harmonic mean of a "
            "recall and cusp precision; node ratio = boundary nodes over ideal).",
            "- Curve quality off the vertex discs and zones: extra inflections (traced minus "
            "truth, runs of same-sign turns reaching 2 deg) per 100 px of truth boundary; "
            "tangent-direction error at each traced sample against the nearest truth sample "
            "(mod 180), each case's p95 and max, median over cases.",
            "- Node placement off the zones: authored-node recall = outline segment endpoints "
            "with a traced node within D; stray nodes = traced nodes with no authored node "
            "within D.",
            f"- Corpus: {meta.get('corpus', 'synthetic')}. Potrace: "
            f"{setting_label('potrace', settings.get('potrace', {}))}; VTracer: "
            f"{setting_label('vtracer', settings.get('vtracer', {}))}.", ""]


def summary_markdown(rows: list[dict], meta: dict) -> str:
    header = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines = ["# Tracer benchmark: Vinylgen, Potrace, VTracer", "",
             f"- Runs: {meta['runs']} (tier, case) pairs; wall time {meta['wall_seconds']:.0f} s.",
             *_method_lines(meta)]
    for tier in sorted({row["tier"] for row in rows}):
        tier_rows = [row for row in rows if row["tier"] == tier]
        lines += [f"## Tier `{tier}`", "", "### All cases", ""] + header
        for tool in TOOLS:
            lines.append(_line(tool, [r for r in tier_rows if r["tool"] == tool]))
        for kind in ("size", "family"):
            names = sorted({row[kind] for row in tier_rows},
                           key=lambda name: (name if kind == "size" else 0, str(name)))
            lines += ["", f"### By {kind}", ""]
            lines += ["| " + kind + " | " + " | ".join(COLUMNS) + " |",
                      "|" + "---|" * (len(COLUMNS) + 1)]
            for name in names:
                for tool in TOOLS:
                    members = [r for r in tier_rows if r["tool"] == tool and r[kind] == name]
                    lines.append(f"| {name} " + _line(tool, members))
        lines.append("")
    failures = [row for row in rows if not row["ok"]]
    if failures:
        lines += ["## Failed runs", ""]
        lines += [f"- `{r['tool']}` `{r['tier']}/{r['case_id']}`: {r['error']}" for r in failures]
    return "\n".join(lines) + "\n"


# --- grid ------------------------------------------------------------------------------

GRID_FIELDS = (("genuine-corner recall", "genuine_corner_recall"),
               ("cusp precision", "cusp_precision"),
               ("genuine-corner F1", "genuine_corner_f1"),
               ("Hausdorff median px", "hausdorff_median_px"),
               ("node ratio (geo)", "node_ratio"),
               ("extra inflections/100 px", "extra_inflections_per_100px"),
               ("tangent p95 deg", "tangent_p95_deg"), ("tangent max deg", "tangent_max_deg"),
               ("topology mismatches", "topology_mismatches"), ("failed", "failed"))


def grid_points(rows: list[dict]) -> list[dict]:
    """One summary per (tool, setting) of a grid run, in first-seen order."""
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = (row["tool"], json.dumps(row.get("setting", {}), sort_keys=True))
        groups.setdefault(key, []).append(row)
    points = []
    for (tool, setting), members in groups.items():
        stats = benchmark_stats(members)
        points.append({
            "tool": tool, "setting": json.loads(setting),
            "label": setting_label(tool, json.loads(setting)), "cases": stats["cases"],
            "failed": stats["failed"], "genuine_corner_recall": stats["genuine_corner_recall"],
            "cusp_precision": stats["cusp_precision"],
            "genuine_corner_f1": stats["genuine_corner_f1"],
            "hausdorff_median_px": stats["hausdorff_px"]["median"],
            "node_ratio": stats["node_ratio"]["geometric_mean"],
            "extra_inflections_per_100px": stats["extra_inflections_per_100px"],
            "tangent_p95_deg": stats["tangent_p95_deg"],
            "tangent_max_deg": stats["tangent_max_deg"],
            "topology_mismatches": stats["topology"]["cases_mismatched"]})
    return points


def best_settings(points: list[dict]) -> dict[str, dict]:
    """Each tool's best grid point: the highest genuine-corner F1 among the points
    whose median local Hausdorff is within ``GRID_HAUSDORFF_SLACK`` of the tool's
    default setting's, with that default point."""
    defaults = {"potrace": POTRACE_DEFAULTS, "vtracer": VTRACER_DEFAULTS}
    chosen = {}
    for tool, default_setting in defaults.items():
        mine = [point for point in points if point["tool"] == tool]
        default = next((point for point in mine
                        if {**default_setting, **point["setting"]} == default_setting), None)
        if default is None or default["hausdorff_median_px"] is None:
            continue
        limit = GRID_HAUSDORFF_SLACK * default["hausdorff_median_px"]
        eligible = [point for point in mine if point["genuine_corner_f1"] is not None
                    and point["hausdorff_median_px"] is not None
                    and point["hausdorff_median_px"] <= limit]
        if not eligible:
            continue
        best = max(eligible, key=lambda point: point["genuine_corner_f1"])
        chosen[tool] = {"best": best, "default": default, "hausdorff_limit_px": limit}
    return chosen


def grid_markdown(points: list[dict], meta: dict) -> str:
    header = ["| tool | setting | " + " | ".join(name for name, _ in GRID_FIELDS) + " |",
              "|" + "---|" * (len(GRID_FIELDS) + 2)]
    lines = ["# Tracer benchmark grid: Potrace and VTracer settings, labels tier", "",
             f"- Runs: {meta['runs']} cases; wall time {meta['wall_seconds']:.0f} s; "
             f"corpus {meta.get('corpus', 'synthetic')}.",
             "- A tool's best setting maximises genuine-corner F1 (genuine-corner recall with "
             "cusp precision) among its settings whose median local Hausdorff is within "
             f"{100 * (GRID_HAUSDORFF_SLACK - 1):.0f}% of its default setting's.",
             "- VTracer accepts segment lengths from "
             f"{VTRACER_SEGMENT_RANGE[0]:g} to {VTRACER_SEGMENT_RANGE[1]:g} px; the grid's "
             "shortest is that minimum.", "", "## Best settings", ""]
    for tool, choice in best_settings(points).items():
        best, default = choice["best"], choice["default"]
        lines.append(f"- **{tool}**: `{best['label']}`: genuine-corner F1 "
                     f"{_fmt(best['genuine_corner_f1'])} (default `{default['label']}`: "
                     f"{_fmt(default['genuine_corner_f1'])}), genuine-corner recall "
                     f"{_fmt(best['genuine_corner_recall'])} ({_fmt(default['genuine_corner_recall'])}), "
                     f"cusp precision {_fmt(best['cusp_precision'])} "
                     f"({_fmt(default['cusp_precision'])}), Hausdorff median "
                     f"{_fmt(best['hausdorff_median_px'])} px ({_fmt(default['hausdorff_median_px'])}; "
                     f"limit {_fmt(choice['hausdorff_limit_px'])}).")
    lines += ["", "## Every grid point", ""] + header
    for point in points:
        lines.append(f"| {point['tool']} | {point['label']} | "
                     + " | ".join(_fmt(point[key]) for _, key in GRID_FIELDS) + " |")
    return "\n".join(lines) + "\n"


def write_grid(out: Path, rows: list[dict], meta: dict) -> None:
    points = grid_points(rows)
    with (out / "grid.jsonl").open("w") as handle:
        for point in points:
            handle.write(json.dumps(point) + "\n")
    (out / "grid.md").write_text(grid_markdown(points, meta))


# --- command line ----------------------------------------------------------------------

def corpus_runs(corpus: str) -> list[tuple[str, object]]:
    """Every (tier, case) of a corpus: the synthetic regression corpus, or both
    tiers of the icon corpus."""
    if corpus == "icons":
        from vinylgen.fidelity.bounds import TIERS
        from vinylgen.fidelity.icons import icon_cases
        return [(tier, case) for case in icon_cases() for tier in TIERS]
    from vinylgen.fidelity.regression import all_runs
    return all_runs()


def _segment_length(text: str) -> float:
    value = float(text)
    low, high = VTRACER_SEGMENT_RANGE
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(f"VTracer accepts a segment length from {low:g} to {high:g}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="only the first N runs")
    parser.add_argument("--match", default=None, help="only case ids containing this text")
    parser.add_argument("--corpus", choices=("synthetic", "icons"), default="synthetic")
    parser.add_argument("--potrace-alphamax", type=float, default=POTRACE_DEFAULTS["alphamax"])
    parser.add_argument("--potrace-opttolerance", type=float,
                        default=POTRACE_DEFAULTS["opttolerance"],
                        help="0 turns curve optimisation off (-n)")
    parser.add_argument("--vtracer-corner-threshold", type=float,
                        default=VTRACER_DEFAULTS["corner_threshold"])
    parser.add_argument("--vtracer-splice-threshold", type=float,
                        default=VTRACER_DEFAULTS["splice_threshold"])
    parser.add_argument("--vtracer-segment-length", type=_segment_length,
                        default=VTRACER_DEFAULTS["segment_length"])
    parser.add_argument("--grid", action="store_true",
                        help="the labels tier only, each tool at every point of its tuning grid")
    parser.add_argument("--summarise", type=Path, default=None,
                        help="rewrite summary.md (or grid.md) from an earlier run's directory")
    args = parser.parse_args()
    if args.summarise:
        rows = [json.loads(line) for line in (args.summarise / "rows.jsonl").open()]
        meta = json.loads((args.summarise / "meta.json").read_text())
        if meta.get("grid"):
            write_grid(args.summarise, rows, meta)
            print(args.summarise / "grid.md")
        else:
            (args.summarise / "summary.md").write_text(summary_markdown(rows, meta))
            print(args.summarise / "summary.md")
        return 0
    from vinylgen.fidelity.sweep import default_jobs, timestamp
    runs = corpus_runs(args.corpus)
    if args.grid:
        runs = [(tier, case) for tier, case in runs if tier == "labels"]
    if args.match:
        runs = [(tier, case) for tier, case in runs if args.match in case.case_id]
    if args.limit:
        runs = runs[:args.limit]
    settings = {"potrace": {"alphamax": args.potrace_alphamax,
                            "opttolerance": args.potrace_opttolerance},
                "vtracer": {"corner_threshold": args.vtracer_corner_threshold,
                            "splice_threshold": args.vtracer_splice_threshold,
                            "segment_length": args.vtracer_segment_length}}
    grid = {"potrace": POTRACE_GRID, "vtracer": VTRACER_GRID}
    out = ROOT / ".tmp" / "benchmark" / (timestamp() + ("-grid" if args.grid else ""))
    work = out / "work"
    work.mkdir(parents=True)
    started = time.perf_counter()
    rows = []
    jobs = args.jobs or default_jobs()
    with ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(run_grid_one, case, str(work), grid) if args.grid
                   else pool.submit(run_one, case, tier, str(work), settings)
                   for tier, case in runs]
        with (out / "rows.jsonl").open("w") as handle:
            for done, future in enumerate(futures, 1):
                for row in future.result():
                    rows.append(row)
                    handle.write(json.dumps(row) + "\n")
                print(f"{done}/{len(futures)}", end="\r", flush=True)
    meta = {"runs": len(runs), "wall_seconds": time.perf_counter() - started,
            "corpus": args.corpus, "grid": args.grid,
            "settings": grid if args.grid else settings}
    (out / "meta.json").write_text(json.dumps(meta) + "\n")
    if args.grid:
        write_grid(out, rows, meta)
        report = out / "grid.md"
    else:
        (out / "summary.md").write_text(summary_markdown(rows, meta))
        report = out / "summary.md"
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
