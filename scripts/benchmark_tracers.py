#!/usr/bin/env python3
"""Benchmark Vinylgen's tracer against Potrace and VTracer on the synthetic corpus.

Every (tier, case) of the regression corpus (``regression.all_runs()``) is traced
by our trace stage exactly as the fidelity sweep traces it, and the label map our
trace stage received (the label render in the labels tier, our palette stage's
labels in the rgb tier) is handed to Potrace and VTracer, so all three trace the
same colours. All three are judged with the regression bounds
(``bounds.evaluate``) and summarised with the sweep's statistics
(``sweep.group_stats``) per tool, tier, family and canvas size.

- Potrace 1.16 traces stacked scans, as Inkscape's multi-colour trace does: the
  colours ordered by area, largest at the bottom, each traced as its mask united
  with the masks of every colour above it (``-t 0``: no speckle removal;
  otherwise its defaults, output quantised to 1/1000 px).
- VTracer 0.6.4 traces the flattened RGB in colour mode with its default stacked
  hierarchy (spline mode, ``--filter_speckle 1`` which keeps every patch,
  ``--gradient_step 1`` and ``--color_precision 8`` which keep the exact palette).

Neither writes a partition, so each tool's shapes are stacked (Potrace's scans in
order, VTracer's paths in document order) and each point belongs to the topmost
shape covering it; a point no shape covers belongs to no colour (it counts as
missing area). The nodes judged are the tool's own
nodes on its colours' visible boundaries, typed by their handles (a cusp where
the incoming and outgoing tangents differ by more than ``CUSP_TURN_DEG``,
otherwise a smooth node when the two handles are equally long and a symmetric
one when not), plus every point where the stacking clips one shape against
another, which is a cusp.

Neither tool knows the canvas frame, so a shape along it may be rounded a
fraction of a pixel off it, exposing a hairline of the colour beneath along the
canvas edge; that hairline is a colour boundary far from every true one. The
summary counts the Hausdorff outliers whose worst point lies at the frame.

Results go to ``.tmp/benchmark/<timestamp>/``: ``rows.jsonl`` and ``summary.md``.
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
    rings are the fill's visible area and whose nodes are the shapes' own nodes on
    that area's boundary plus the clip points."""
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
    for fill in visible_by_fill:
        area = unary_union(visible_by_fill[fill])
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


def run_potrace(labels: np.ndarray, fills: list[str], work: Path) -> list[tuple[str, list]]:
    """Stacked scans, as Inkscape's multi-colour Potrace trace stacks them: the
    colours ordered by area, largest at the bottom, and each traced as the union
    of its mask and the masks of every colour above it, so each scan lies under
    the ones above and no gap opens between neighbours."""
    work.mkdir(parents=True, exist_ok=True)
    counts = np.bincount(labels.ravel(), minlength=len(fills))
    order = sorted(np.flatnonzero(counts), key=lambda i: (-counts[i], i))
    rank = np.full(len(fills), -1)
    rank[order] = np.arange(len(order))
    ranks = rank[labels]
    shapes = []
    for position, index in enumerate(order):
        pbm, svg = work / f"c{index}.pbm", work / f"c{index}.svg"
        _pbm(ranks >= position, pbm)
        subprocess.run([POTRACE, "-b", "svg", "-t", "0", "-u", "1000", "-o", str(svg), str(pbm)],
                       check=True, capture_output=True)
        subpaths = [sub for _, subs in svg_paths(svg) for sub in subs]
        if subpaths:
            shapes.append((fills[index], subpaths))
    return shapes


def run_vtracer(labels: np.ndarray, fills: list[str], rgb: np.ndarray,
                work: Path) -> list[tuple[str, list]]:
    """The flattened RGB traced in colour mode; each path's fill is mapped to the
    nearest palette colour. Paths stack in document order."""
    from PIL import Image
    work.mkdir(parents=True, exist_ok=True)
    png, svg = work / "flattened.png", work / "vtracer.svg"
    Image.fromarray(rgb[labels], "RGB").save(png)
    subprocess.run([VTRACER, "--input", str(png), "--output", str(svg), "--colormode", "color",
                    "--hierarchical", "stacked", "--mode", "spline", "--filter_speckle", "1",
                    "--color_precision", "8", "--gradient_step", "1", "--path_precision", "4"],
                   check=True, capture_output=True)
    palette = np.asarray([[int(fill[i:i + 2], 16) for i in (1, 3, 5)] for fill in fills])
    shapes = []
    for fill, subpaths in svg_paths(svg):
        colour = np.asarray([int(fill[i:i + 2], 16) for i in (1, 3, 5)])
        nearest = int(np.abs(palette - colour).sum(axis=1).argmin())
        shapes.append((fills[nearest], subpaths))
    return shapes


# --- worker ----------------------------------------------------------------------------

def _row(case, tier: str, tool: str) -> dict:
    from vinylgen.fidelity.sweep import case_family, case_size
    return {"case_id": case.case_id, "tier": tier, "tool": tool, "family": case_family(case),
            "tags": list(case.tags), "size": case_size(case), "ok": False, "error": None,
            "sub_resolution": None}


def _judge(case, tier, run, case_bounds, traced) -> dict:
    from vinylgen.fidelity.bounds import colour_map, evaluate
    from vinylgen.fidelity.metrics import measure
    from vinylgen.fidelity.sweep import summarise_measure
    result = measure(run.edge_svg, case_bounds.truth, case.scene.palette, corner_radius=-1.0,
                     smooth_min_distance=-1.0, colour_map=colour_map(case_bounds, run),
                     traced=traced)
    evaluation = evaluate(case, tier, run, case_bounds, measured=result, traced=traced)
    worst = evaluation["local_hausdorff"]["worst_point"]
    width, height = result["canvas"]
    frame = (None if worst is None else
             min(worst[0], worst[1], width - worst[0], height - worst[1]))
    clips = traced.get("clip_points", set()) if traced else set()
    clip_cusps = [cusp for cusp in evaluation["cusps"]
                  if (round(cusp["point"][0], _DECIMALS), round(cusp["point"][1], _DECIMALS)) in clips]
    return {**summarise_measure(result, evaluation), "worst_point": worst,
            "worst_frame_px": frame, "clip_cusps": len(clip_cusps),
            "clip_kinks": sum(1 for cusp in clip_cusps if cusp["kink"])}


def run_one(case, tier: str, work_dir: str) -> list[dict]:
    """Our trace of one (tier, case) and the two tools' traces of the same labels,
    each judged against the case's bounds."""
    from vinylgen.contracts import SegmentationBundle, TraceSettings
    from vinylgen.fidelity.families import case_palette_settings
    from vinylgen.fidelity.harness import trace_labels, trace_rgb
    from vinylgen.fidelity.sweep import _bounds, _render, audit_summary
    case_dir = Path(work_dir) / tier / case.case_id
    rows = []
    ours = _row(case, tier, "vinylgen")
    rows.append(ours)
    try:
        palette = case.scene.palette
        if tier == "labels":
            run = trace_labels((_render(case, tier), palette), case_dir, TraceSettings(),
                               subject=case.case_id)
        else:
            run = trace_rgb((_render(case, tier), palette), case_dir, TraceSettings(),
                            palette_options=case_palette_settings(case), subject=case.case_id)
        case_bounds = _bounds(case, tier, run)
        ours.update(ok=True, sub_resolution=not case_bounds.geometric,
                    trace_seconds=run.trace_seconds,
                    summary={**_judge(case, tier, run, case_bounds, None),
                             **audit_summary(run.audit)})
    except Exception as error:
        ours.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
        shutil.rmtree(case_dir, ignore_errors=True)
        return rows + [{**_row(case, tier, tool), "error": "our trace failed"}
                       for tool in TOOLS[1:]]
    bundle = SegmentationBundle.load(run.subject_dir / "segmentation.npz", with_costs=False)
    labels = np.asarray(bundle.labels)
    fills = [str(row["hex"]).upper() for row in run.palette]
    rgb = np.asarray([row["rgb"] for row in run.palette], dtype=np.uint8)
    height, width = labels.shape
    for tool in TOOLS[1:]:
        row = _row(case, tier, tool)
        rows.append(row)
        try:
            started = time.perf_counter()
            work = case_dir / tool
            shapes = (run_potrace(labels, fills, work) if tool == "potrace"
                      else run_vtracer(labels, fills, rgb, work))
            seconds = time.perf_counter() - started
            traced = stacked_trace(shapes, width, height)
            row.update(ok=True, sub_resolution=not case_bounds.geometric, trace_seconds=seconds,
                       summary=_judge(case, tier, run, case_bounds, traced))
        except Exception as error:
            row.update(error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc())
    shutil.rmtree(case_dir, ignore_errors=True)
    return rows


# --- summary ---------------------------------------------------------------------------

# A case's Hausdorff distance is an outlier beyond this many px; an outlier whose
# worst point lies within FRAME_PX of the canvas frame is a frame sliver (a
# frame-unaware tracer pulling a shape's edge off the canvas edge).
OUTLIER_PX = 5.0
FRAME_PX = 1.0
COLUMNS = ("tool", "cases", "failed", "Hausdorff median px", "Hausdorff p95 px",
           "outliers > 5 px", "of them frame slivers", "mean dist px",
           "misassigned %", "required recall", "cusp precision", "corner F1", "kinks",
           "kinks/case", "of kinks at clip points", "node ratio (geo)", "node ratio (median)", "topology mismatches",
           "trace s (median)")


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


def _line(tool: str, rows: list[dict]) -> str:
    from vinylgen.fidelity.sweep import group_stats
    stats = group_stats(rows)
    misassigned = stats["misassigned_fraction"]
    cells = [tool, stats["cases"], stats["failed"], stats["hausdorff_px"]["median"],
             stats["hausdorff_px"]["p95"], *_outliers(rows), stats["mean_boundary_px"],
             None if misassigned is None else 100 * misassigned, stats["required_corner_recall"],
             stats["cusp_precision"], stats["corner_f1"], stats["kinks"]["total"],
             stats["kinks"]["per_case"],
             sum(row["summary"].get("clip_kinks", 0) for row in rows if row["ok"]),
             stats["node_ratio"]["geometric_mean"],
             stats["node_ratio"]["median"], stats["topology"]["cases_mismatched"],
             stats["seconds"]["trace_median"]]
    return "| " + " | ".join(_fmt(cell) for cell in cells) + " |"


def summary_markdown(rows: list[dict], meta: dict) -> str:
    header = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines = ["# Tracer benchmark: Vinylgen, Potrace, VTracer", "",
             f"- Runs: {meta['runs']} (tier, case) pairs; wall time {meta['wall_seconds']:.0f} s.",
             "- Judged with the synthetic regression bounds, statistics as in "
             "`sweep.group_stats` (Hausdorff off the vertex discs and zones; recall of "
             "required vertices; cusp precision = cusps that are no kink; node ratio = "
             "boundary nodes over ideal).", ""]
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--jobs", type=int, default=None)
    parser.add_argument("--limit", type=int, default=None, help="only the first N runs")
    parser.add_argument("--match", default=None, help="only case ids containing this text")
    parser.add_argument("--summarise", type=Path, default=None,
                        help="rewrite summary.md from an earlier run's directory")
    args = parser.parse_args()
    if args.summarise:
        rows = [json.loads(line) for line in (args.summarise / "rows.jsonl").open()]
        meta = json.loads((args.summarise / "meta.json").read_text())
        (args.summarise / "summary.md").write_text(summary_markdown(rows, meta))
        print(args.summarise / "summary.md")
        return 0
    from vinylgen.fidelity.regression import all_runs
    from vinylgen.fidelity.sweep import default_jobs, timestamp
    runs = all_runs()
    if args.match:
        runs = [(tier, case) for tier, case in runs if args.match in case.case_id]
    if args.limit:
        runs = runs[:args.limit]
    out = ROOT / ".tmp" / "benchmark" / timestamp()
    work = out / "work"
    work.mkdir(parents=True)
    started = time.perf_counter()
    rows = []
    jobs = args.jobs or default_jobs()
    with ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(run_one, case, tier, str(work)) for tier, case in runs]
        with (out / "rows.jsonl").open("w") as handle:
            for done, future in enumerate(futures, 1):
                for row in future.result():
                    rows.append(row)
                    handle.write(json.dumps(row) + "\n")
                print(f"{done}/{len(futures)}", end="\r", flush=True)
    meta = {"runs": len(runs), "wall_seconds": time.perf_counter() - started}
    (out / "meta.json").write_text(json.dumps(meta) + "\n")
    (out / "summary.md").write_text(summary_markdown(rows, meta))
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
