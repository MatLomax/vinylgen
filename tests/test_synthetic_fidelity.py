"""Synthetic ground-truth fidelity regression tests of the default trace.

Every committed synthetic case (``tests/synthetic/fixed``, ``tests/synthetic/
palette-stage`` and the pinned fuzz scenes listed in ``tests/synthetic/
fuzz-seeds.json``) is traced with the product defaults (``TraceSettings()``)
through the real stages and its edge variant measured against the scene's vector
truth (``metrics.measure``). Two tiers:

- ``labels``: the hard-edged label map (``raster.render_labels``) straight into the
  trace stage (``harness.trace_labels``);
- ``rgb``: the anti-aliased render through the palette stage first
  (``harness.trace_rgb``). Palette-stage cases run in this tier only.

Each (case, tier) is one generated ``TestCase`` class whose ``setUpClass`` traces
once; each assertion is its own test method, so ``scripts/run_tests.py`` schedules
a (case, tier) as one unit and reports every assertion separately.

Expected failures
-----------------
``tests/synthetic/expected-failures.json`` maps ``"<tier>/<case_id>"`` to
``{assertion: slug}``: that assertion fails today for the defect tracked by
worklog task ``slug``. The method is wrapped in ``unittest.expectedFailure`` and
its failure message and docstring name the slug; an unexpected pass is reported
as an ``UNEXPECTED SUCCESS``, which fails the run, so a fixed defect forces its
entry out of the table. An entry naming an unknown (tier, case) or an assertion
the case does not make fails ``ExpectedFailureTableTests``.

Bounds
------
Lengths are pixels of the traced raster. ``M`` is the maximum deviation the trace
used and ``A`` its corner sharpening allowance, both resolved exactly as the trace
stage resolves them (``resolve_trace_lengths(TraceSettings(),
length_reference(width, height))``: 1 per mille of the longer side, floored at
1 px); ``M`` is checked against the trace audit's ``lengths`` record.

- ``Q`` (quantisation), label-map tier: the trace stage samples a colour boundary
  at the pixel-edge vertices of the label map (a hard-edged render puts every
  sub-pixel crossing on the pixel edge). A label-map pixel takes the colour
  covering most of it, which along a straight boundary is the colour at its
  centre, so a pixel edge between two colours crosses the true boundary between
  the two pixel centres: its midpoint lies within ``0.5 |cos t|`` of the boundary
  and its end vertices within ``0.5 (|cos t| + |sin t|) <= sqrt(2) / 2``
  (``t`` the boundary's angle to the edge normal). ``Q = sqrt(2) / 2``.
- ``Q``, full-pipeline tier: labels come from the palette stage's anti-alias
  resolution, which follows no geometric rule, and samples move by a Lab-cost
  crossing estimate, so no quantisation bound is derivable. Measured: over the
  corpus's rgb cases with the ``hausdorff`` assertion, apart from those whose
  failure is tracked as a palette-stage or truth defect, the local boundary
  distance exceeds ``M + DISTANCE_ERROR_BOUND`` by at most 0.41 px
  (``ellipse-a8-rot45-128``, 2026-09-26); the bound uses the label-map tier's
  ``sqrt(2) / 2``, a 0.30 px margin over that.
- ``D = M + Q + DISTANCE_ERROR_BOUND``: the fitter keeps every sample within ``M``
  of the written curve (``curve_fit._curve_tolerance`` and
  ``_straight_deviation`` are both capped by it), the samples lie within ``Q`` of
  the truth and the metric adds at most ``DISTANCE_ERROR_BOUND``.
- ``R(c)``, corner radius: ``D / sin(phi / 2)`` for a corner or junction, ``D /
  sin(phi)`` for an edge point, ``phi = min(a, 180 - a)`` for the narrowest sector
  ``a = 180 - turn_deg``. With each traced branch within ``D`` of its true
  branch, the node where the branches meet lies in the intersection of their
  ``D``-bands, whose farthest point from the true vertex is ``D / sin(phi / 2)``
  (two bands crossing at ``phi``); an edge node lies on the exact frame, so one
  band crossing the frame at ``phi`` bounds it: ``D / sin(phi)``.
- Decidable corners: a side's direction at a corner is read from the chord
  between boundary samples at the apex and at arc length ``l`` along the side.
  Both lie within ``Q`` of the truth and the true side lies within ``e(l)`` of its
  apex tangent line up to ``l``, so the chord gives the tangent to
  ``atan((2 Q + e(l)) / l)``. The certified turn is ``turn_deg`` less both sides'
  errors, maximised over ``l`` up to the neighbouring true vertices along the
  boundary (``decidable_turn``; for straight sides ``turn - 2 atan(2 Q / l)``). A
  ``corner`` whose certified turn reaches the threshold is *required*. Any other
  corner turning by at least ``threshold - delta``, ``delta = 2 atan(2 Q / s)``
  with ``s`` the distance to the nearest other true vertex, is *optional* (a cusp
  is allowed, not demanded): a corner whose sides curl away within a few pixels
  is not distinguishable from a tight bend at this resolution. Every junction
  and edge point is required: the trace always anchors a cusp there.
- Sharpenable rounds: the trace sharpens a round whose sharp point lies within
  ``A`` of its drawn edge (GLOSSARY "corner sharpening allowance"), a 90 degree
  round of radius up to ``SHARPENING_RADIUS_FACTOR * A``; seen through ``Q`` that
  is a radius up to ``rho = SHARPENING_RADIUS_FACTOR * (A + Q)``, all of whose arc
  lies within ``rho`` of the sharp point. A cusp is therefore allowed where the
  true boundary turns by at least the threshold within ``rho`` of it.

Assertions (``ASSERTIONS``):

- ``hausdorff``: every boundary sample (traced and truth, off the frame) farther
  than ``R(c)`` from every required or optional corner ``c`` is within ``D`` of the
  other side's samples. Nearer a corner the ``corners`` assertion bounds the
  geometry: a tip narrower than a pixel is not in the raster, so the traced tip
  may stop up to ``R(c)`` short of it.
- ``corners``: every required corner has a cusp node within ``R(c)``.
- ``kinks``: every cusp node lies within ``R(c)`` of a required or optional corner,
  or where the truth turns by at least the threshold within ``rho`` of it.
- ``nodes``: boundary nodes ``<= NODE_FACTOR * ideal_nodes + NODE_CONSTANT``
  (``2 * ideal + 4``). The ideal segmentation (one cubic per arc of at most 90
  degrees, within ``3e-4 r`` of the arc, one line per line) keeps every sample
  within ``Q < 1 px <= M`` of the curve, so it meets the fitter's own tolerance
  and a node-minimal fit needs no more than ``ideal_nodes``. The fitter's search
  is not a global optimum and its default node placement prefers axis extremes:
  the factor 2 allows every ideal piece to be split once, the 4 the axis
  extremes of one closed smooth run that coincide with no ideal node.
- ``topology``: for every scene colour, the polygon and hole counts of the traced
  and the true geometry, each with the discs ``R(c)`` round the required and
  optional corners removed, are equal, and no traced layer is unmatched. The
  discs are removed because a tip narrower than a pixel may leave a pixel touching
  the shape only corner to corner (a separate four-connected region, which the
  trace must keep separate) and two regions meeting at a point may join through
  one pixel. Asserted when ``min_feature_px >= TOPOLOGY_FLOOR_PX`` (``sqrt 2``): a
  band of width ``w`` at angle ``t`` has a four-connected chain of pixel centres
  only when ``w >= |cos t| + |sin t|``, whose maximum is ``sqrt 2``; a narrower
  band may legitimately break into corner-touching pieces.
- ``area``: misassigned area ``<= 2 D L + pi sum R(c)^2`` over the required and
  optional corners (``L`` the truth boundary length): with the boundary within
  ``D`` of the truth off the corner discs, every misassigned point lies in the
  ``D``-tube round the truth boundary or in a corner disc. A feature narrower
  than ``2 D`` that vanishes entirely costs at most its own area, which the tube
  round its two boundaries covers. In the full-pipeline tier ``D`` carries the
  measured ``Q``.
- ``palette`` (palette-stage cases): ``scene.params["expect"]``: every ``merge``
  pair became exactly one inferred colour, every ``survive`` colour is exactly one
  inferred colour, every ``fold`` colour (a colour below the whole-colour usage
  cutoff, which folds it into a surviving colour) is no inferred colour, and no
  inferred colour is unmatched (``harness.MATCH_DELTA_E`` nearest-colour
  matching); with every inferred colour matching a surviving scene colour, a
  folded colour's pixels are necessarily surviving colours. A case expecting a
  merge or a fold asserts only this: its truth keeps the colours meant to merge
  or fold, and the geometry the palette stage then leaves is not the scene's.

Resolution floor ``RESOLUTION_FLOOR_PX`` (2 px): a feature narrower than 2 px has
in places a single pixel centre across it, so one pixel row carries both of its
boundaries and they cannot each be placed within ``Q``. A case whose
``min_feature_px`` is below it asserts ``topology`` (at or above the topology
floor) and ``area`` only.

Full-pipeline floor: the palette stage removes by design a colour region smaller
than ``PaletteSettings().min_region_pixels`` (small-region cleanup: "contrast
alone does not preserve a speck") and a whole colour covering less than
``PaletteSettings().min_colour_share`` of the canvas (whole-colour usage cleanup,
exempted only by explicit whole-colour preservation). An rgb case whose truth has
such a region or colour asserts ``area`` only (and ``palette`` for a palette-stage
case, whose ``expect`` states the intended outcome regardless).
"""

from __future__ import annotations

import json
import math
import re
import shutil
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from vinylgen.contracts import PaletteSettings, TraceSettings
from vinylgen.fidelity import families
from vinylgen.fidelity.families import SHARPENING_RADIUS_FACTOR
from vinylgen.fidelity.harness import MATCH_DELTA_E, trace_labels, trace_rgb
from vinylgen.fidelity.metrics import (DISTANCE_ERROR_BOUND, _boundary_samples,
                                       _geometry_rings, _polygons, measure,
                                       parse_traced_svg, path_geometry)
from vinylgen.fidelity.raster import render_labels
from vinylgen.fidelity.truth import _colour_geometry, scene_truth
from vinylgen.trace_lengths import length_reference, resolve_trace_lengths

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests" / "synthetic"
EXPECTED_FAILURES_PATH = SYNTHETIC / "expected-failures.json"
FUZZ_SEEDS_PATH = SYNTHETIC / "fuzz-seeds.json"
WORK = ROOT / ".tmp" / "test-synthetic-fidelity"

# Expected failures name real worklog tasks; a slug with this prefix is a
# placeholder for a finding no task tracks yet.
PLACEHOLDER_SLUG_PREFIX = "synthetic-finding-"
TIERS = ("labels", "rgb")
ASSERTIONS = ("hausdorff", "corners", "kinks", "nodes", "topology", "area", "palette")
Q_PX = math.sqrt(2.0) / 2.0
RESOLUTION_FLOOR_PX = 2.0
TOPOLOGY_FLOOR_PX = math.sqrt(2.0)
NODE_FACTOR = 2
NODE_CONSTANT = 4
_MIN_ANGLE_DEG = 1e-3
_MIN_PIECE_AREA = 1e-6


# --- bounds -------------------------------------------------------------------------------


def resolved_lengths(width: int, height: int) -> TraceSettings:
    """The default trace lengths in px on this canvas, as the trace stage resolves them."""
    return resolve_trace_lengths(TraceSettings(), length_reference(width, height))


def boundary_bound(max_deviation: float) -> float:
    """``D``: the boundary distance bound off the corner discs."""
    return max_deviation + Q_PX + DISTANCE_ERROR_BOUND


def corner_radius(kind: str, turn_deg: float, bound: float) -> float:
    """``R(c)``: how far from a true corner, junction or edge point its node may lie."""
    sector = 180.0 - float(turn_deg)
    phi = max(min(sector, 180.0 - sector), _MIN_ANGLE_DEG)
    half = phi if kind == "edge" else phi / 2.0
    return bound / math.sin(math.radians(half))


_VERTEX_MATCH_PX = 0.01
_CHORD_GROWTH = 1.25


def _ring_vertex(rings: list[np.ndarray], point) -> tuple[np.ndarray, int] | None:
    """The closed truth ring (without its repeated end) with a vertex within
    ``_VERTEX_MATCH_PX`` of ``point``, and that vertex's index."""
    for ring in rings:
        closed = ring[:-1]
        distance = np.hypot(closed[:, 0] - point[0], closed[:, 1] - point[1])
        index = int(distance.argmin())
        if distance[index] <= _VERTEX_MATCH_PX:
            return closed, index
    return None


def _side(ring: np.ndarray, apex: int, direction: int, vertices: np.ndarray) -> np.ndarray:
    """The ring from ``apex`` walking ``direction`` up to (and including) the next
    true vertex, or once round the ring."""
    count = len(ring)
    order = (apex + direction * np.arange(count + 1)) % count
    side = ring[order]
    if len(vertices):
        near = cKDTree(vertices).query(side[1:])[0] <= _VERTEX_MATCH_PX
        if near.any():
            side = side[:int(np.argmax(near)) + 2]
    return side


def _side_departures(side: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Arc length along ``side`` (from the apex, its first point) and the largest
    distance of the side so far from its tangent line at the apex."""
    steps = np.hypot(*np.diff(side, axis=0).T)
    arc = np.concatenate(([0.0], np.cumsum(steps)))
    first = int(np.argmax(steps > 0))
    tangent = (side[first + 1] - side[0]) / steps[first]
    offset = side - side[0]
    distance = np.abs(offset[:, 0] * tangent[1] - offset[:, 1] * tangent[0])
    return arc, np.maximum.accumulate(distance)


def decidable_turn(rings: list[np.ndarray], point, turn_deg: float,
                   vertices: np.ndarray) -> float | None:
    """The smallest turn the raster certifies at a true corner of turn ``turn_deg``.

    A side's direction at the apex is read from the chord between boundary samples
    at the apex and at arc length ``l`` along the side: both samples lie within
    ``Q`` of the truth, and the true side lies within ``e(l)`` of its apex tangent
    line up to ``l``, so the chord's direction is the tangent's within
    ``atan((2 Q + e(l)) / l)``. The certified turn is the true turn less both
    sides' errors, maximised over ``l`` up to the neighbouring true vertices; for
    straight sides it is ``turn - 2 atan(2 Q / l)``. None when the corner lies on
    no truth ring vertex."""
    found = _ring_vertex(rings, point)
    if found is None:
        return None
    ring, apex = found
    others = vertices[np.hypot(vertices[:, 0] - point[0], vertices[:, 1] - point[1])
                      > _VERTEX_MATCH_PX] if len(vertices) else vertices
    sides = [_side_departures(_side(ring, apex, direction, others)) for direction in (-1, 1)]
    best = -math.inf
    length = 2.0 * Q_PX
    while all(length <= arc[-1] for arc, _ in sides):
        error = 0.0
        for arc, departure in sides:
            reach = int(np.searchsorted(arc, length, side="right"))
            error += math.degrees(math.atan((2.0 * Q_PX + departure[min(reach, len(arc) - 1)])
                                            / length))
        best = max(best, turn_deg - error)
        length *= _CHORD_GROWTH
    return best


def corner_roles(truth, rings: list[np.ndarray], threshold: float) -> list[dict]:
    """Every true vertex with its role: ``required`` (a junction or edge point, or a
    corner whose ``decidable_turn`` reaches the threshold), ``optional`` (any other
    corner turning by at least the threshold less ``delta = 2 atan(2 Q / s)``, ``s``
    the distance to the nearest other true vertex) or ``shallow``."""
    points = np.asarray([corner.point for corner in truth.corners],
                        dtype=np.float64).reshape(-1, 2)
    if len(points) > 1:
        distances, _ = cKDTree(points).query(points, k=2)
        nearest = distances[:, 1]
    else:
        nearest = np.full(len(points), math.inf)
    rows = []
    for corner, spacing in zip(truth.corners, nearest):
        delta = math.degrees(2.0 * math.atan(2.0 * Q_PX / spacing)) if spacing > 0 else 180.0
        decidable = None
        if corner.kind != "corner":
            role = "required"
        else:
            decidable = decidable_turn(rings, corner.point, corner.turn_deg, points)
            if decidable is None:
                decidable = corner.turn_deg - delta
            if decidable >= threshold:
                role = "required"
            elif corner.turn_deg >= threshold - delta:
                role = "optional"
            else:
                role = "shallow"
        rows.append({"point": [float(corner.point[0]), float(corner.point[1])],
                     "kind": corner.kind, "turn_deg": float(corner.turn_deg),
                     "delta_deg": float(delta), "decidable_turn_deg": decidable,
                     "role": role})
    return rows


def truth_turn_near(rings: list[np.ndarray], point, radius: float) -> float:
    """The largest net turn (degrees) of a stretch of true boundary lying within
    ``radius`` of ``point``; a whole ring inside counts as 360."""
    best = 0.0
    px, py = point
    for ring in rings:
        inside = np.hypot(ring[:, 0] - px, ring[:, 1] - py) <= radius
        if not inside.any():
            continue
        if inside.all():
            return 360.0
        # Rotate so the ring starts outside, then walk the runs of inside points.
        start = int(np.flatnonzero(~inside)[0])
        closed = ring[:-1] if np.allclose(ring[0], ring[-1]) else ring
        order = np.roll(np.arange(len(closed)), -start)
        flags = inside[:len(closed)][order]
        points = closed[order]
        index = 0
        while index < len(points):
            if not flags[index]:
                index += 1
                continue
            end = index
            while end + 1 < len(points) and flags[end + 1]:
                end += 1
            # The run's first and last segments include the crossing into and out
            # of the disc, so a run of one point still has two directions.
            before = points[index - 1]
            after = points[(end + 1) % len(points)]
            first = points[index] - before
            last = after - points[end]
            angle = math.degrees(abs(math.atan2(first[0] * last[1] - first[1] * last[0],
                                                first[0] * last[0] + first[1] * last[1])))
            best = max(best, angle)
            index = end + 1
    return best


def _outside(samples: np.ndarray, centres: np.ndarray, radii: np.ndarray) -> np.ndarray:
    if not len(samples) or not len(centres):
        return samples
    keep = np.ones(len(samples), dtype=bool)
    for centre, radius in zip(centres, radii):
        keep &= np.hypot(*(samples - centre).T) > radius
    return samples[keep]


def local_hausdorff(traced_rings: list[np.ndarray], truth_rings: list[np.ndarray],
                    width: float, height: float, centres: np.ndarray,
                    radii: np.ndarray) -> dict:
    """The larger directed boundary distance between traced and true boundaries
    over the samples outside every corner disc (``centres``, ``radii``).

    Samples as ``metrics.measure`` takes them (both sides of every colour boundary,
    off the frame, ``SAMPLE_SPACING`` apart); a sample is compared with every
    boundary sample of the other side, whichever colour, so a colour mismatch is
    left to the topology and area assertions."""
    traced = _boundary_samples(traced_rings, width, height)
    truth = _boundary_samples(truth_rings, width, height)
    if not len(traced) or not len(truth):
        return {"hausdorff_px": 0.0 if not len(traced) and not len(truth) else math.inf,
                "worst_point": None}
    worst, point = 0.0, None
    for mine, other in ((_outside(traced, centres, radii), truth),
                        (_outside(truth, centres, radii), traced)):
        if not len(mine):
            continue
        distances, _ = cKDTree(other).query(mine)
        if distances.max() > worst:
            worst = float(distances.max())
            point = [float(value) for value in mine[int(distances.argmax())]]
    return {"hausdorff_px": worst, "worst_point": point}


def _local_topology(geometry, discs) -> dict:
    remaining = geometry.difference(discs) if discs is not None else geometry
    polygons = [polygon for polygon in _polygons(remaining) if polygon.area > _MIN_PIECE_AREA]
    return {"polygons": len(polygons),
            "holes": sum(1 for polygon in polygons for ring in polygon.interiors
                         if Polygon(ring).area > _MIN_PIECE_AREA)}


# --- running one (case, tier) ---------------------------------------------------------------


def run_case(case, tier: str, work_dir: Path, settings: TraceSettings | None = None) -> dict:
    """Trace ``case`` in ``tier`` (with the default settings unless ``settings`` is
    given) and gather every number the assertions read. The bounds always come from
    the default settings' resolved lengths."""
    scene = case.scene
    if tier == "labels":
        run = trace_labels((np.asarray(render_labels(scene), dtype=np.int16), scene.palette),
                           work_dir, settings or TraceSettings(), subject=case.case_id)
    else:
        run = trace_rgb((families.render_case_rgb(case), scene.palette), work_dir,
                        settings or TraceSettings(), subject=case.case_id)
    truth = scene_truth(scene)
    # A negative radius and distance make every true corner a miss and every cusp a
    # kink, so the result lists each one's nearest distance.
    measured = measure(run.edge_svg, truth, scene.palette, corner_radius=-1.0,
                       smooth_min_distance=-1.0, colour_map=run.colour_map)
    lengths = resolved_lengths(scene.width, scene.height)
    max_deviation = float(lengths.max_deviation)
    allowance = float(lengths.corner_sharpening_allowance)
    bound = boundary_bound(max_deviation)
    threshold = float(TraceSettings().corner_threshold)

    truth_rings = [ring for geometry in truth.colour_geometry.values()
                   for ring in _geometry_rings(geometry)]
    corners = corner_roles(truth, truth_rings, threshold)
    nearest = {tuple(miss["point"]): miss["nearest_cusp_px"]
               for miss in measured["corners"]["misses"]}
    for corner in corners:
        corner["radius_px"] = corner_radius(corner["kind"], corner["turn_deg"], bound)
        distance = nearest.get(tuple(corner["point"]))
        corner["nearest_cusp_px"] = math.inf if distance is None else float(distance)
    anchored = [c for c in corners if c["role"] != "shallow"]
    centres = np.asarray([c["point"] for c in anchored], dtype=np.float64).reshape(-1, 2)
    radii = np.asarray([c["radius_px"] for c in anchored], dtype=np.float64)

    traced = parse_traced_svg(run.edge_svg)
    width, height = traced["width"], traced["height"]
    traced_by_colour: dict[int, list] = {}
    traced_rings = []
    for layer in traced["layers"]:
        index = run.colour_map.get(layer["fill"])
        if index is None:
            continue
        traced_by_colour.setdefault(index, []).extend(
            path_geometry(path) for path in layer["paths"])
        traced_rings.extend(ring for path in layer["paths"] for ring in path["rings"])
    hausdorff = local_hausdorff(traced_rings, truth_rings, width, height, centres, radii)

    rho = SHARPENING_RADIUS_FACTOR * (allowance + Q_PX)
    cusps = []
    for kink in measured["kinks"]["kinks"]:
        x, y = kink["point"]
        near_corner = any(math.hypot(x - c["point"][0], y - c["point"][1]) <= c["radius_px"]
                          for c in anchored)
        turn = truth_turn_near(truth_rings, (x, y), rho)
        cusps.append({"point": [x, y], "near_corner": near_corner, "truth_turn_deg": turn,
                      "kink": not near_corner and turn < threshold})

    discs = unary_union([Point(c["point"]).buffer(c["radius_px"]) for c in anchored]) \
        if anchored else None
    topology = {}
    for index in sorted(set(truth.colour_geometry) | set(traced_by_colour)):
        mine = unary_union(traced_by_colour.get(index, [])) if index in traced_by_colour \
            else Polygon()
        true = truth.colour_geometry.get(index, Polygon())
        topology[str(index)] = {"traced": _local_topology(mine, discs),
                                "truth": _local_topology(true, discs)}

    length = (measured["boundary"]["truth_samples"] * measured["boundary"]["sample_spacing_px"]
              / 2.0)
    return {
        "case_id": case.case_id, "tier": tier, "min_feature_px": float(case.min_feature_px),
        "width": scene.width, "height": scene.height,
        "max_deviation_px": max_deviation,
        "audit_max_deviation_px": float(run.audit["lengths"]["px"]["max_deviation"]),
        "allowance_px": allowance, "rho_px": rho,
        "bound_px": bound, "threshold_deg": threshold,
        "local_hausdorff": hausdorff, "hausdorff_px": measured["boundary"]["hausdorff_px"],
        "corners": corners, "cusps": cusps,
        "nodes": measured["nodes"]["boundary"], "ideal_nodes": int(truth.ideal_nodes),
        "topology": topology, "unmatched_layers": measured["area"]["unmatched_layers"],
        "misassigned_px": float(measured["area"]["misassigned_px"]),
        "truth_length_px": float(length),
        "palette_matches": run.palette_matches, "inferred_colours": len(run.palette),
    }


# --- assertions ----------------------------------------------------------------------------


def palette_floor_reasons(case) -> list[str]:
    """Why the palette stage may remove part of ``case`` by design: a true colour
    region below the small-region cleanup size, or a colour below the usage cutoff."""
    palette = PaletteSettings()
    canvas = float(case.scene.width * case.scene.height)
    reasons = []
    for index, geometry in sorted(_colour_geometry(case.scene).items()):
        share = geometry.area / canvas
        if 0.0 < share < palette.min_colour_share:
            reasons.append(f"colour {index} covers {100 * share:.2f}% "
                           f"< {100 * palette.min_colour_share:g}%")
        small = [polygon.area for polygon in _polygons(geometry)
                 if _MIN_PIECE_AREA < polygon.area < palette.min_region_pixels]
        if small:
            reasons.append(f"colour {index} has a region of {min(small):.1f} px^2 "
                           f"< {palette.min_region_pixels} px")
    return reasons


def applicable(case, tier: str) -> tuple[str, ...]:
    """The assertions a (case, tier) makes (see the module docstring)."""
    expect = case.scene.params.get("expect")
    if expect is not None and (expect.get("merge") or expect.get("fold")):
        return ("palette",)
    names: list[str] = []
    below_palette_floor = tier == "rgb" and bool(palette_floor_reasons(case))
    if case.min_feature_px >= RESOLUTION_FLOOR_PX and not below_palette_floor:
        names += ["hausdorff", "corners", "kinks", "nodes"]
    if case.min_feature_px >= TOPOLOGY_FLOOR_PX and not below_palette_floor:
        names.append("topology")
    names.append("area")
    if expect is not None:
        names.append("palette")
    return tuple(names)


def _points(points, limit: int = 5) -> str:
    text = ", ".join(f"({x:.2f}, {y:.2f})" for x, y in points[:limit])
    return text + (f" (+{len(points) - limit} more)" if len(points) > limit else "")


def check(name: str, result: dict, case) -> tuple[bool, str]:
    """Whether ``result`` passes assertion ``name``, and what it measured."""
    bound = result["bound_px"]
    if name == "hausdorff":
        value = result["local_hausdorff"]["hausdorff_px"]
        return value <= bound, (f"boundary distance off the corner discs {value:.3f} px "
                                f"(limit D {bound:.3f} px) at "
                                f"{result['local_hausdorff']['worst_point']}")
    if name == "corners":
        misses = [c for c in result["corners"]
                  if c["role"] == "required" and not c["nearest_cusp_px"] <= c["radius_px"]]
        return not misses, (f"{len(misses)} required corners without a cusp within R: " + "; ".join(
            f"{c['kind']} turn {c['turn_deg']:.1f} at ({c['point'][0]:.2f}, {c['point'][1]:.2f}) "
            f"nearest cusp {c['nearest_cusp_px']:.2f} px > R {c['radius_px']:.2f} px"
            for c in misses[:6]))
    if name == "kinks":
        kinks = [cusp["point"] for cusp in result["cusps"] if cusp["kink"]]
        return not kinks, f"{len(kinks)} cusp nodes on smooth truth: {_points(kinks)}"
    if name == "nodes":
        limit = NODE_FACTOR * result["ideal_nodes"] + NODE_CONSTANT
        return result["nodes"] <= limit, (f"{result['nodes']} boundary nodes (limit "
                                          f"{NODE_FACTOR} x {result['ideal_nodes']} ideal + "
                                          f"{NODE_CONSTANT} = {limit})")
    if name == "topology":
        wrong = {colour: row for colour, row in result["topology"].items()
                 if row["traced"] != row["truth"]}
        unmatched = [row["fill"] for row in result["unmatched_layers"]]
        return not wrong and not unmatched, "; ".join(
            [f"colour {colour}: traced {row['traced']} != truth {row['truth']}"
             for colour, row in wrong.items()]
            + ([f"unmatched layers {unmatched}"] if unmatched else []))
    if name == "area":
        limit = area_limit(result)
        value = result["misassigned_px"]
        return value <= limit, f"misassigned {value:.1f} px^2 (limit {limit:.1f} px^2)"
    if name == "palette":
        return palette_outcome(case, result)
    raise KeyError(name)


def area_limit(result: dict) -> float:
    discs = sum(math.pi * c["radius_px"] ** 2 for c in result["corners"]
                if c["role"] != "shallow")
    return 2.0 * result["bound_px"] * result["truth_length_px"] + discs


def palette_outcome(case, result: dict) -> tuple[bool, str]:
    expect = case.scene.params["expect"]
    matched: dict[int, int] = {}
    for row in result["palette_matches"]:
        if row["matched"]:
            matched[row["truth_index"]] = matched.get(row["truth_index"], 0) + 1
    problems = []
    for first, second in expect.get("merge", []):
        count = matched.get(first, 0) + matched.get(second, 0)
        if count != 1:
            problems.append(f"merge {first}+{second}: {count} inferred colours")
    for index in expect.get("survive", []):
        if matched.get(index, 0) != 1:
            problems.append(f"survive {index}: {matched.get(index, 0)} inferred colours")
    for index in expect.get("fold", []):
        if matched.get(index, 0):
            problems.append(f"fold {index}: {matched[index]} inferred colours")
    unmatched = [row["hex"] for row in result["palette_matches"] if not row["matched"]]
    if unmatched:
        problems.append(f"unmatched inferred colours {unmatched} (beyond {MATCH_DELTA_E} dE)")
    return not problems, "; ".join(problems)


# --- the cases -------------------------------------------------------------------------------


def fuzz_cases() -> list:
    from vinylgen.fidelity.fuzz import pinned_cases
    return pinned_cases(FUZZ_SEEDS_PATH)


def all_runs() -> list[tuple[str, object]]:
    """Every (tier, case) the suite traces, in a stable order."""
    runs = []
    for case in families.load_fixed_set(ROOT / families.FIXED_DIR):
        runs += [(tier, case) for tier in TIERS]
    for case in families.load_fixed_set(ROOT / families.PALETTE_STAGE_DIR):
        runs.append(("rgb", case))
    for case in fuzz_cases():
        runs += [(tier, case) for tier in TIERS]
    return runs


def load_expected_failures() -> dict[str, dict[str, str]]:
    return json.loads(EXPECTED_FAILURES_PATH.read_text(encoding="utf-8"))


def _class_name(tier: str, case_id: str) -> str:
    words = re.split(r"[^0-9a-zA-Z]+", f"{tier}-{case_id}")
    return "Fidelity_" + "_".join(word for word in words if word)


class _SyntheticCase:
    """Mixed into each generated (case, tier) ``TestCase``: ``setUpClass`` traces the
    case once; each test method is one assertion."""

    case = None
    tier = ""

    @classmethod
    def setUpClass(cls):
        cls.work_dir = WORK / cls.tier / cls.case.case_id
        shutil.rmtree(cls.work_dir, ignore_errors=True)
        cls.result = run_case(cls.case, cls.tier, cls.work_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work_dir, ignore_errors=True)

    def test_resolved_max_deviation_matches_the_trace(self):
        self.assertAlmostEqual(self.result["max_deviation_px"],
                               self.result["audit_max_deviation_px"], places=9)

    def _assert(self, name: str, slug: str | None):
        passed, detail = check(name, self.result, self.case)
        prefix = f"[{self.tier}/{self.case.case_id} {name}]"
        if slug is not None:
            self.assertTrue(passed, f"{prefix} expected failure, tracked by {slug}: {detail}")
        else:
            self.assertTrue(passed, f"{prefix} {detail}")


def _assertion_method(name: str, slug: str | None):
    def method(self):
        self._assert(name, slug)
    method.__name__ = f"test_{name}"
    if slug is not None:
        method.__doc__ = f"{name}: expected failure, tracked by worklog task {slug}"
        return unittest.expectedFailure(method)
    method.__doc__ = f"{name}: within its bound"
    return method


def _generate() -> dict[str, type]:
    expected = load_expected_failures()
    classes = {}
    for tier, case in all_runs():
        slugs = expected.get(f"{tier}/{case.case_id}", {})
        body = {"case": case, "tier": tier, "__module__": __name__,
                "__doc__": f"{case.case_id} traced in the {tier} tier."}
        for name in applicable(case, tier):
            body[f"test_{name}"] = _assertion_method(name, slugs.get(name))
        name = _class_name(tier, case.case_id)
        if name in classes:
            raise ValueError(f"duplicate synthetic test class {name}")
        classes[name] = type(name, (_SyntheticCase, unittest.TestCase), body)
    return classes


globals().update(_generate())


class ExpectedFailureTableTests(unittest.TestCase):
    def test_every_entry_names_a_traced_case_and_an_assertion_it_makes(self):
        runs = {f"{tier}/{case.case_id}": case for tier, case in all_runs()}
        for key, entries in load_expected_failures().items():
            with self.subTest(key=key):
                self.assertIn(key, runs, "expected failure for a (tier, case) the suite "
                                         "does not trace")
                tier = key.split("/", 1)[0]
                made = applicable(runs[key], tier)
                for name, slug in entries.items():
                    self.assertIn(name, ASSERTIONS)
                    self.assertIn(name, made, f"{key} makes no {name} assertion")
                    self.assertRegex(slug, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                    self.assertFalse(slug.startswith(PLACEHOLDER_SLUG_PREFIX),
                                     f"{key} {name}: placeholder slug {slug}; name the "
                                     "worklog task that tracks the defect")


if __name__ == "__main__":
    unittest.main()
