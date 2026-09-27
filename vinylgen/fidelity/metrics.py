"""Fidelity metrics of a traced SVG against the vector truth of a synthetic scene.

``measure`` reads the edge variant (``traced-edge.svg``): its layers are
``<g inkscape:groupmode="layer" fill="#RRGGBB">`` groups of paths whose ``d`` holds
absolute ``M``/``L``/``C``/``Z`` data (``vinylgen.node_types.parse_path``) and whose
``sodipodi:nodetypes`` names each node's type in Inkscape's letters: ``c`` a cusp
node, ``s`` a Vinylgen symmetric node, ``z`` a Vinylgen smooth node.

Coordinates are pixels of the traced raster (the SVG ``viewBox``), y down, as the
truth's are.

Error bounds, relative to the analytic scene (the truth polygons are its curves
flattened to ``scene.FLATTEN_TOLERANCE``, 0.001 px chord error):

- Traced cubics are flattened to at most ``CHORD_TOLERANCE`` (0.01 px) chord
  error.
- Boundaries on both sides are sampled every ``SAMPLE_SPACING`` (0.04 px) along
  their flattened polylines and distances are taken sample to nearest sample
  (a KD-tree). A sample's distance to the other side's samples exceeds its
  distance to the other side's polyline by at most half the spacing (0.02 px),
  and the supremum over a polyline exceeds the maximum over its samples by at
  most the same, so every boundary distance, and the Hausdorff distance, mean
  and p95 built from them, is within ``DISTANCE_ERROR_BOUND`` (0.031 px: half the
  spacing, the traced flattening and the truth's own flattening) of the exact
  distance to the analytic scene.
- Areas: flattening moves a traced boundary by at most 0.01 px and a true one by
  at most 0.001 px, so a colour's traced area is within 0.01 px times its traced
  boundary length, and its true area within 0.001 px times its true boundary
  length, of the exact areas; IoU and symmetric difference inherit that.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from shapely.ops import unary_union

from vinylgen.fidelity.scene import FLATTEN_TOLERANCE
from vinylgen.fidelity.truth import SceneTruth
from vinylgen.node_types import parse_path
from vinylgen.svg_writer import INKSCAPE, SODIPODI, SVG

CHORD_TOLERANCE = 0.01
SAMPLE_SPACING = 0.04
DISTANCE_ERROR_BOUND = SAMPLE_SPACING / 2 + CHORD_TOLERANCE + FLATTEN_TOLERANCE
# Two written node positions are one node when they agree to this many decimals
# (the tracer writes ten significant digits, and a shared boundary is written
# from one fit in both of its colour layers).
_NODE_DECIMALS = 6
# A boundary sample within this distance of the canvas frame lies on it.
_FRAME_EPS = 1e-6
_NODE_TYPES = {"c": "cusp", "s": "symmetric", "z": "smooth"}


def _cubic_steps(p0, p1, p2, p3, tolerance: float) -> int:
    """Uniform parameter steps that keep a cubic's chords within ``tolerance``.

    A chord over a parameter step ``1/n`` deviates from the curve by at most
    ``max|B''| / (8 n^2)``, and ``|B''| <= 6 max(|p0 - 2p1 + p2|, |p1 - 2p2 + p3|)``.
    """
    second = max(math.hypot(p0[0] - 2 * p1[0] + p2[0], p0[1] - 2 * p1[1] + p2[1]),
                 math.hypot(p1[0] - 2 * p2[0] + p3[0], p1[1] - 2 * p2[1] + p3[1]))
    return max(1, math.ceil(math.sqrt(6.0 * second / (8.0 * tolerance))))


def flatten_subpath(start, segments, closed: bool,
                    tolerance: float = CHORD_TOLERANCE) -> np.ndarray:
    """The subpath as a polyline (``(n, 2)``), cubics within ``tolerance``; a closed
    subpath ends on its start point."""
    points = [start]
    current = start
    for segment in segments:
        if segment[0] == "L":
            points.append(segment[1])
        else:
            _, c1, c2, end = segment
            steps = _cubic_steps(current, c1, c2, end, tolerance)
            t = np.arange(1, steps + 1, dtype=np.float64) / steps
            u = 1.0 - t
            coefficients = np.stack((u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3), axis=1)
            controls = np.asarray((current, c1, c2, end), dtype=np.float64)
            curve = coefficients @ controls
            curve[-1] = end
            points.extend(map(tuple, curve))
        current = segment[-1]
    if closed and points[-1] != start:
        points.append(start)
    return np.asarray(points, dtype=np.float64)


def _subpath_nodes(start, segments, closed: bool, types: str) -> list[tuple[float, float, str]]:
    """The nodes of one subpath with their type letters, from its slice of
    ``sodipodi:nodetypes`` (``vinylgen.node_types.subpath_nodetypes`` layout)."""
    segments = list(segments)
    if closed and segments and segments[-1][-1] != start:
        segments.append(("L", start))
    if not segments:
        return []
    ends = [segment[-1] for segment in segments]
    positions = [start, *ends[:-1]] if closed else [start, *ends]
    return [(float(x), float(y), letter) for (x, y), letter in zip(positions, types)]


def _subpath_type_length(segments, start, closed: bool) -> int:
    if not segments:
        return 0
    return len(segments) + 1 + (1 if closed and segments[-1][-1] != start else 0)


def parse_traced_svg(svg_path: Path) -> dict:
    """Layers, paths, flattened rings and typed nodes of a traced SVG.

    Returns ``{"width", "height", "layers": [{"fill", "label", "paths": [{"id",
    "rings": [polyline arrays], "closed": [bool], "nodes": [(x, y, letter)]}]}]}``.
    """
    root = ET.parse(svg_path).getroot()
    x0, y0, width, height = (float(value) for value in root.attrib["viewBox"].split())
    if x0 != 0 or y0 != 0:
        raise ValueError("traced SVG viewBox must start at the origin")
    layers = []
    for group in root.iter(f"{{{SVG}}}g"):
        if group.attrib.get(f"{{{INKSCAPE}}}groupmode") != "layer":
            continue
        fill = str(group.attrib["fill"]).upper()
        paths = []
        for element in group.iter(f"{{{SVG}}}path"):
            subpaths = parse_path(element.attrib["d"])
            letters = element.attrib.get(f"{{{SODIPODI}}}nodetypes", "")
            expected = sum(_subpath_type_length(segments, start, closed)
                           for start, segments, closed in subpaths)
            if len(letters) != expected:
                raise ValueError(f"path {element.attrib.get('id')!r} has {len(letters)} node "
                                 f"types for {expected} node slots")
            rings, closed_flags, nodes, offset = [], [], [], 0
            for start, segments, closed in subpaths:
                length = _subpath_type_length(segments, start, closed)
                nodes.extend(_subpath_nodes(start, segments, closed, letters[offset:offset + length]))
                offset += length
                if segments:
                    rings.append(flatten_subpath(start, segments, closed))
                    closed_flags.append(closed)
            paths.append({"id": element.attrib.get("id"), "rings": rings,
                          "closed": closed_flags, "nodes": nodes})
        layers.append({"fill": fill, "label": group.attrib.get(f"{{{INKSCAPE}}}label"),
                       "paths": paths})
    return {"width": width, "height": height, "layers": layers}


def path_geometry(path: dict):
    """The filled area of one parsed path under the even-odd rule."""
    shapes = []
    for ring, closed in zip(path["rings"], path["closed"]):
        if closed and len(ring) >= 4:
            polygon = Polygon(ring)
            shapes.append(polygon if polygon.is_valid else shapely.make_valid(polygon))
    if not shapes:
        return Polygon()
    result = shapes[0]
    for shape in shapes[1:]:
        result = result.symmetric_difference(shape)
    return result


def _polygons(geometry) -> list[Polygon]:
    """The non-empty polygons of a geometry, collections flattened."""
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, Polygon):
        return [geometry]
    return [polygon for part in getattr(geometry, "geoms", ()) for polygon in _polygons(part)]


def _geometry_rings(geometry) -> list[np.ndarray]:
    rings = []
    for polygon in _polygons(geometry):
        rings.append(np.asarray(polygon.exterior.coords, dtype=np.float64))
        rings.extend(np.asarray(interior.coords, dtype=np.float64) for interior in polygon.interiors)
    return rings


def sample_polyline(points: np.ndarray, spacing: float = SAMPLE_SPACING) -> np.ndarray:
    """Points along a polyline no more than ``spacing`` apart, both ends included."""
    if len(points) < 2:
        return points.reshape(-1, 2)
    starts, ends = points[:-1], points[1:]
    lengths = np.hypot(*(ends - starts).T)
    counts = np.maximum(1, np.ceil(lengths / spacing).astype(np.int64))
    segment = np.repeat(np.arange(len(starts)), counts)
    first = np.cumsum(counts) - counts
    t = (np.arange(int(counts.sum())) - np.repeat(first, counts)) / np.repeat(counts, counts)
    samples = starts[segment] + (ends - starts)[segment] * t[:, None]
    return np.vstack((samples, points[-1:]))


def _off_frame(samples: np.ndarray, width: float, height: float) -> np.ndarray:
    """The samples not on the canvas frame: the frame is no colour boundary."""
    if not len(samples):
        return samples.reshape(-1, 2)
    x, y = samples[:, 0], samples[:, 1]
    on_frame = ((np.abs(x) <= _FRAME_EPS) | (np.abs(x - width) <= _FRAME_EPS)
                | (np.abs(y) <= _FRAME_EPS) | (np.abs(y - height) <= _FRAME_EPS))
    return samples[~on_frame]


def _boundary_samples(rings: list[np.ndarray], width: float, height: float) -> np.ndarray:
    if not rings:
        return np.empty((0, 2))
    return _off_frame(np.vstack([sample_polyline(ring) for ring in rings]), width, height)


def _distance_summary(traced: np.ndarray, truth: np.ndarray, longer: float) -> dict:
    """Symmetric boundary distances between two sample sets, in px and per mille."""
    if not len(traced) or not len(truth):
        return {"traced_samples": int(len(traced)), "truth_samples": int(len(truth)),
                "hausdorff_px": None, "mean_px": None, "p95_px": None,
                "traced_to_truth_max_px": None, "truth_to_traced_max_px": None,
                "hausdorff_per_mille": None, "mean_per_mille": None, "p95_per_mille": None}
    forward, _ = cKDTree(truth).query(traced)
    backward, _ = cKDTree(traced).query(truth)
    pooled = np.concatenate((forward, backward))
    hausdorff = float(max(forward.max(), backward.max()))
    mean = float(pooled.mean())
    p95 = float(np.percentile(pooled, 95))
    scale = 1000.0 / longer
    return {"traced_samples": int(len(traced)), "truth_samples": int(len(truth)),
            "hausdorff_px": hausdorff, "mean_px": mean, "p95_px": p95,
            "traced_to_truth_max_px": float(forward.max()),
            "truth_to_traced_max_px": float(backward.max()),
            "hausdorff_per_mille": hausdorff * scale, "mean_per_mille": mean * scale,
            "p95_per_mille": p95 * scale}


def _topology(geometry) -> dict:
    polygons = _polygons(geometry)
    return {"polygons": len(polygons), "holes": sum(len(polygon.interiors) for polygon in polygons)}


def measure(svg_path: Path, truth: SceneTruth, palette: list[dict], *,
            corner_radius: float, smooth_min_distance: float | None = None,
            colour_map: dict[str, int] | None = None, traced: dict | None = None) -> dict:
    """Measure a traced edge-variant SVG against a scene's truth.

    ``palette`` is the scene palette (rows with ``rgb``); each layer is assigned to
    the palette index whose colour equals its fill. ``colour_map`` (fill hex to
    palette index) overrides that, for a trace whose colours were inferred
    (``harness.TraceRun.colour_map``); a layer it does not map is reported under
    ``unmatched_layers`` and its whole area counts as misassigned.

    Boundaries are the colour-geometry boundaries off the canvas frame, on both
    sides. Nodes are unique written positions across all layers (a shared boundary
    is written in both of its colours); the four canvas corners are frame nodes,
    not boundary nodes, and are left out of every node, corner and kink count.
    ``nodes.boundary`` is the count compared with ``truth.ideal_nodes``.

    A true corner (any kind) is found when a cusp node lies within
    ``corner_radius``. A kink is a cusp node farther than ``smooth_min_distance``
    (default ``corner_radius``) from every true corner.

    ``traced`` is the SVG already parsed (``parse_traced_svg``'s layout), which
    replaces reading ``svg_path``: another tracer's output converted to layers,
    rings and typed nodes (``scripts/benchmark_tracers.py``).
    """
    if traced is None:
        traced = parse_traced_svg(Path(svg_path))
    width, height = traced["width"], traced["height"]
    if (width, height) != (float(truth.width), float(truth.height)):
        raise ValueError(f"traced canvas {width:g}x{height:g} differs from the truth's "
                         f"{truth.width}x{truth.height}")
    longer = float(max(width, height))
    if colour_map is None:
        colour_map = {"#" + "".join(f"{int(v):02X}" for v in row["rgb"]): index
                      for index, row in enumerate(palette)}
    colour_map = {str(key).upper(): int(value) for key, value in colour_map.items()}
    kink_distance = corner_radius if smooth_min_distance is None else smooth_min_distance

    traced_geometry: dict[int, list] = {}
    traced_rings: dict[int, list[np.ndarray]] = {}
    unmatched_layers = []
    all_nodes: dict[tuple[float, float], set[str]] = {}
    per_path_nodes = 0
    path_count = 0
    for layer in traced["layers"]:
        index = colour_map.get(layer["fill"])
        geometry = unary_union([path_geometry(path) for path in layer["paths"]])
        for path in layer["paths"]:
            path_count += 1
            per_path_nodes += len(path["nodes"])
            for x, y, letter in path["nodes"]:
                key = (round(x, _NODE_DECIMALS), round(y, _NODE_DECIMALS))
                all_nodes.setdefault(key, set()).add(letter)
        if index is None:
            unmatched_layers.append({"fill": layer["fill"], "label": layer["label"],
                                     "area_px": float(geometry.area)})
            continue
        traced_geometry.setdefault(index, []).append(geometry)
        traced_rings.setdefault(index, []).extend(
            ring for path in layer["paths"] for ring in path["rings"])
    traced_by_colour = {index: unary_union(parts) for index, parts in traced_geometry.items()}

    # Boundary distances.
    colours = sorted(set(truth.colour_geometry) | set(traced_by_colour))
    traced_samples = {index: _boundary_samples(traced_rings.get(index, []), width, height)
                      for index in colours}
    truth_samples = {index: _boundary_samples(_geometry_rings(truth.colour_geometry.get(index)),
                                              width, height)
                     for index in colours}
    empty = np.empty((0, 2))
    boundary = _distance_summary(
        np.vstack([traced_samples[i] for i in colours] or [empty]),
        np.vstack([truth_samples[i] for i in colours] or [empty]), longer)
    boundary["per_colour"] = {str(index): _distance_summary(
        traced_samples[index], truth_samples[index], longer) for index in colours}
    boundary["sample_spacing_px"] = SAMPLE_SPACING
    boundary["error_bound_px"] = DISTANCE_ERROR_BOUND

    # Areas and topology.
    per_colour_area = {}
    misassigned = sum(row["area_px"] for row in unmatched_layers)
    topology = {}
    for index in colours:
        mine = traced_by_colour.get(index, Polygon())
        true = truth.colour_geometry.get(index, Polygon())
        union = mine.union(true).area
        intersection = mine.intersection(true).area
        extra = mine.difference(true).area
        missing = true.difference(mine).area
        misassigned += extra
        per_colour_area[str(index)] = {
            "traced_area_px": float(mine.area), "truth_area_px": float(true.area),
            "symmetric_difference_px": float(extra + missing),
            "iou": float(intersection / union) if union > 0 else None}
        topology[str(index)] = {"traced": _topology(mine), "truth": _topology(true)}
    area = {"per_colour": per_colour_area, "misassigned_px": float(misassigned),
            "misassigned_fraction": float(misassigned / (width * height)),
            "unmatched_layers": unmatched_layers}

    # Nodes.
    canvas_corners = {(0.0, 0.0), (round(width, _NODE_DECIMALS), 0.0),
                      (round(width, _NODE_DECIMALS), round(height, _NODE_DECIMALS)),
                      (0.0, round(height, _NODE_DECIMALS))}
    boundary_nodes = {key: letters for key, letters in all_nodes.items()
                      if key not in canvas_corners}
    type_counts = {name: 0 for name in _NODE_TYPES.values()}
    conflicts = 0
    cusp_points = []
    for key, letters in boundary_nodes.items():
        if len(letters) > 1:
            conflicts += 1
        # A node written as a cusp in any layer is a cusp where it is drawn.
        letter = "c" if "c" in letters else sorted(letters)[0]
        name = _NODE_TYPES.get(letter, letter)
        type_counts[name] = type_counts.get(name, 0) + 1
        if letter == "c":
            cusp_points.append(key)
    nodes = {"paths": path_count, "per_path_total": per_path_nodes,
             "unique": len(all_nodes),
             "canvas_corners": len(all_nodes) - len(boundary_nodes),
             "boundary": len(boundary_nodes), "ideal": int(truth.ideal_nodes),
             "ratio_to_ideal": (len(boundary_nodes) / truth.ideal_nodes
                                if truth.ideal_nodes else None),
             "types": type_counts, "type_conflicts": conflicts}

    # Corners and kinks.
    cusps = np.asarray(cusp_points, dtype=np.float64).reshape(-1, 2)
    cusp_tree = cKDTree(cusps) if len(cusps) else None
    by_kind: dict[str, list[int]] = {}
    misses = []
    for corner in truth.corners:
        nearest = (float(cusp_tree.query(corner.point)[0]) if cusp_tree is not None
                   else math.inf)
        hit = nearest <= corner_radius
        found = by_kind.setdefault(corner.kind, [0, 0])
        found[0] += int(hit)
        found[1] += 1
        if not hit:
            misses.append({"point": [float(corner.point[0]), float(corner.point[1])],
                           "kind": corner.kind, "turn_deg": float(corner.turn_deg),
                           "nearest_cusp_px": None if math.isinf(nearest) else nearest})
    total_hits = sum(row[0] for row in by_kind.values())
    corners = {"radius_px": float(corner_radius), "true": len(truth.corners),
               "found": total_hits,
               "recall": total_hits / len(truth.corners) if truth.corners else None,
               "per_kind": {kind: {"true": row[1], "found": row[0], "recall": row[0] / row[1]}
                            for kind, row in sorted(by_kind.items())},
               "misses": misses}
    corner_points = np.asarray([corner.point for corner in truth.corners],
                               dtype=np.float64).reshape(-1, 2)
    kinks_list = []
    if len(cusps):
        if len(corner_points):
            distances, _ = cKDTree(corner_points).query(cusps)
        else:
            distances = np.full(len(cusps), math.inf)
        for point, distance in zip(cusps, distances):
            if distance > kink_distance:
                kinks_list.append({"point": [float(point[0]), float(point[1])],
                                   "nearest_true_corner_px": (None if math.isinf(distance)
                                                              else float(distance))})
    kinks = {"distance_px": float(kink_distance), "count": len(kinks_list), "kinks": kinks_list,
             "cusp_precision": ((len(cusps) - len(kinks_list)) / len(cusps)
                                if len(cusps) else None)}
    return {"canvas": [width, height], "boundary": boundary, "area": area,
            "corners": corners, "kinks": kinks, "nodes": nodes, "topology": topology}
