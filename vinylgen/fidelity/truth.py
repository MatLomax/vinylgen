"""The measurable truth of a vector scene: the visible geometry of every palette
colour, the true corners and junctions, and the ideal node count.

``scene_truth`` derives all three from the scene's exact segments.

Visible boundary and its vertices
---------------------------------
The candidate vertices are every outline join (every segment start, including
the canvas corners) and every point where two segments of different rings meet
(``scene.segment_intersections``), kept when they lie on the canvas. Each
candidate ``p`` is classified exactly from the local configuration of the curves
through it: every ring passing within ``CONTINUITY_TOLERANCE`` of ``p``
contributes two branches (its outgoing and reversed incoming tangent), the canvas
border contributes its own, and the branches are ordered counter-clockwise by
tangent angle and, for equal angles, by signed curvature (so tangent curves are
ordered by which bends away first; equal angle and curvature means the curves
coincide, as on a shared edge). The sectors between consecutive branch directions
each have one colour: a ring's interior spans the sectors from its outgoing to
its incoming branch on its interior side, rings not through ``p`` contribute
their constant inside/outside state, the topmost region containing a sector gives
its colour, and sectors outside the canvas have none. A branch separating two
different colours is a visible boundary branch.

- ``edge``: ``p`` is on the canvas border and has at least one visible boundary
  branch.
- ``junction``: ``p`` is inside the canvas and has three or more visible
  boundary branches: three or more colours meet, or two colours meet corner to
  corner.
- ``corner``: exactly two visible boundary branches turning by at least
  ``SMOOTH_TURN_DEG``.

``turn_deg`` is, for every kind, the largest turn any colour's outline makes at
the point: the maximum over the colour sectors around ``p`` (sectors between
visible boundary branches or the image edge) of ``|180 - sector angle|``, angles
measured between tangent directions. For a corner that is the turn of the
boundary itself; a sector between tangent curves has angle 0 (turn 180).

Ideal node count
----------------
The visible boundary network is the set of stretches of region outlines that
separate two different colours, excluding the image border itself. Each segment
is split at every candidate lying on it; each resulting piece is visible when the
colours on its two sides (read at its midpoint) differ, and a stretch shared by
coincident segments is counted once. Pieces meeting at a vertex that is not a
true corner, junction or edge point, with exactly two piece ends, merge when they
continue one curve (the same segment, collinear lines, or arcs of one circle or
ellipse). ``ideal_nodes`` is the number of remaining vertices plus, for every
merged run of arcs with total sweep ``S``, ``ceil(S / 90deg) - 1`` interior nodes
(``ceil(S / 90deg)`` for a closed run with no vertex: a full circle is 4). Cubics
and lines add no interior nodes. A square is 4, a disc 4, a disc partly covered
by a square counts its visible arc pieces plus the square's vertices.

Bounds
------
Vertices are exact to floating-point precision (Newton-refined), except tangent
contacts, located to within ``sqrt(1e-8 * radius of curvature)`` px along the
common tangent. Points closer than ``CONTINUITY_TOLERANCE`` (1e-6 px) are one
point. ``colour_geometry`` uses outlines flattened to ``FLATTEN_TOLERANCE``
(0.001 px) chord error; the flattened polylines are inscribed in convex curves,
so a curved colour boundary sits on average about two thirds of that inside.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import LinearRing, MultiPolygon, Polygon, box

from vinylgen.fidelity.scene import (_SEED_TOLERANCE, CONTINUITY_TOLERANCE, FLATTEN_TOLERANCE,
                                     SMOOTH_TURN_DEG, Line, Outline, rectangle,
                                     segment_end, segment_intersections, segment_locate,
                                     segment_polyline, segment_start)

Point = tuple[float, float]


@dataclass(frozen=True)
class TrueCorner:
    """A point where a traced boundary must have a cusp node.

    ``corner``: a tangent discontinuity of a visible outline, with its turn angle.
    ``junction``: a point where three or more colours meet (or two colours meet
    corner to corner, so three or more boundary branches leave it).
    ``edge``: a point where a colour boundary meets the image edge.

    For junction and edge points ``turn_deg`` is the sharpest turn of any colour's
    outline at the point (see the module docstring).
    """

    point: Point
    turn_deg: float
    kind: Literal["corner", "junction", "edge"]


@dataclass
class SceneTruth:
    """``colour_geometry`` maps each palette index to its visible area after
    occlusion (a shapely Polygon or MultiPolygon in pixel coordinates, y down),
    with curves flattened to at most 0.01 px chord error; together they
    partition the canvas. ``corners`` lists every visible corner, junction and
    edge point. ``ideal_nodes`` is the node count of the visible boundaries in
    their own segment representation, with arcs split into pieces of at most
    90 degrees (a full circle is 4 nodes, a square 4)."""

    width: int
    height: int
    colour_geometry: dict[int, object]
    corners: list[TrueCorner] = field(default_factory=list)
    ideal_nodes: int = 0


# --- truth extraction ------------------------------------------------------------------

# The canvas border is ring 0 of every ``_Truth``; its region index is -1.
_CANVAS = -1
_CANVAS_RING = 0
_ANGLE_TIE = 1e-9
_TANGENT_SIN = 1e-3
_TANGENT_RADIUS = 1e-3


@dataclass
class _Ring:
    region: int
    outline: Outline
    sign: float
    bbox: tuple[float, float, float, float]
    segment_boxes: np.ndarray


@dataclass
class _Local:
    """The curves through one point: branch identities per ray, sector colours
    (sector ``k`` lies counter-clockwise of ray ``k``) and sector angles."""

    rays: list[list[tuple[int, int, int]]]
    colours: list[int | None]
    angles: list[float]
    hits: list[tuple[int, int, float]]


class _Truth:
    def __init__(self, scene):
        self.scene = scene
        self.rings: list[_Ring] = []
        margin = _SEED_TOLERANCE + 1e-5
        canvas = rectangle(0.0, 0.0, float(scene.width), float(scene.height))
        for region_index, outline in [(_CANVAS, canvas)] + [
                (index, ring) for index, region in enumerate(scene.regions)
                for ring in region.rings]:
            boxes = []
            for segment in outline.segments:
                points = segment_polyline(segment, _SEED_TOLERANCE)[1]
                boxes.append([*(points.min(0) - margin), *(points.max(0) + margin)])
            boxes = np.asarray(boxes)
            bbox = (boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max())
            self.rings.append(_Ring(region_index, outline, math.copysign(1.0, outline.signed_area()),
                                    bbox, boxes))
        self.region_rings: dict[int, list[int]] = {}
        for ring_id, ring in enumerate(self.rings):
            self.region_rings.setdefault(ring.region, []).append(ring_id)
        self._contains_cache: dict = {}

    # -- point containment for rings not passing through the point --

    def _ring_contains(self, ring_id: int, p: Point) -> bool:
        ring = self.rings[ring_id]
        x0, y0, x1, y1 = ring.bbox
        if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
            return False
        tolerance = FLATTEN_TOLERANCE
        point = shapely.Point(p)
        while True:
            key = (ring_id, tolerance)
            if key not in self._contains_cache:
                coords = ring.outline.flatten(tolerance)
                polygon = Polygon(coords)
                shapely.prepare(polygon)
                self._contains_cache[key] = (polygon, LinearRing(coords))
            polygon, boundary = self._contains_cache[key]
            distance = boundary.distance(point)
            # The true curve lies within ``tolerance`` of the polyline, so a point
            # farther than twice that is on the same side of both.
            if distance > 2.0 * tolerance:
                return bool(polygon.contains(point))
            tolerance = distance / 4.0
            if tolerance < 1e-10:
                raise ValueError(f"cannot decide which side of an outline {p} lies on")

    # -- local configuration --

    def local(self, p: Point) -> _Local:
        branches: list[tuple[float, float, tuple[int, int, int]]] = []
        through: dict[int, tuple[tuple[int, int, int], tuple[int, int, int]]] = {}
        hits: list[tuple[int, int, float]] = []
        x, y = p
        for ring_id, ring in enumerate(self.rings):
            bx0, by0, bx1, by1 = ring.bbox
            if not (bx0 <= x <= bx1 and by0 <= y <= by1):
                continue
            boxes = ring.segment_boxes
            near = np.flatnonzero((boxes[:, 0] <= x) & (x <= boxes[:, 2])
                                  & (boxes[:, 1] <= y) & (y <= boxes[:, 3]))
            segments = ring.outline.segments
            found = []
            for index in near:
                segment = segments[index]
                if math.dist(segment_start(segment), p) <= CONTINUITY_TOLERANCE:
                    previous_index = (index - 1) % len(segments)
                    found.append(((segment, 0.0, (ring_id, int(index), 1)),
                                  (segments[previous_index], 1.0,
                                   (ring_id, int(previous_index), -1))))
                    hits.append((ring_id, int(index), 0.0))
                    hits.append((ring_id, int(previous_index), 1.0))
                    continue
                for t in segment_locate(segment, p, CONTINUITY_TOLERANCE):
                    # A point at the segment's end is the join with the next
                    # segment, found from that segment's start.
                    located = tuple(segment.point(t))
                    if min(math.dist(segment_start(segment), located),
                           math.dist(segment_end(segment), located)) <= CONTINUITY_TOLERANCE:
                        continue
                    found.append(((segment, t, (ring_id, int(index), 1)),
                                  (segment, t, (ring_id, int(index), -1))))
                    hits.append((ring_id, int(index), t))
            # The joins at a point may be reached from both neighbouring segments'
            # boxes; keep one entry per distinct join or crossing.
            unique = {}
            for out_branch, in_branch in found:
                unique[(out_branch[2], in_branch[2])] = (out_branch, in_branch)
            if len(unique) > 1:
                raise ValueError(f"an outline passes through {p} more than once")
            for out_branch, in_branch in unique.values():
                ids = []
                for segment, t, identity in (out_branch, in_branch):
                    direction = segment.d1(t) * identity[2]
                    second = segment.d2(t)
                    speed = math.hypot(direction[0], direction[1])
                    kappa = float(direction[0] * second[1] - direction[1] * second[0]) / speed ** 3
                    theta = math.atan2(direction[1], direction[0]) % (2 * math.pi)
                    branches.append((theta, kappa, identity))
                    ids.append(identity)
                through[ring_id] = (ids[0], ids[1])
        rays, angles = _order_rays(branches)
        count = len(rays)
        ray_of = {identity: k for k, ray in enumerate(rays) for identity in ray}

        def ring_inside(ring_id: int) -> np.ndarray:
            if ring_id in through:
                out_ray, in_ray = (ray_of[identity] for identity in through[ring_id])
                if out_ray == in_ray:
                    raise ValueError(f"an outline doubles back on itself at {p}")
                first, stop = (out_ray, in_ray) if self.rings[ring_id].sign > 0 else (in_ray, out_ray)
                inside = np.zeros(count, dtype=bool)
                k = first
                while k != stop:
                    inside[k] = True
                    k = (k + 1) % count
                return inside
            return np.full(max(count, 1), self._ring_contains(ring_id, p))

        size = max(count, 1)
        on_canvas = ring_inside(_CANVAS_RING) if _CANVAS_RING in through else np.full(
            size, 0.0 < x < self.scene.width and 0.0 < y < self.scene.height)
        colours: list[int | None] = [None] * size
        settled = ~on_canvas
        for k in range(size):
            if on_canvas[k]:
                colours[k] = self.scene.background
        for region_index in range(len(self.scene.regions) - 1, -1, -1):
            if settled.all():
                break
            inside = np.zeros(size, dtype=bool)
            for ring_id in self.region_rings[region_index]:
                inside ^= ring_inside(ring_id)
            newly = inside & ~settled
            for k in np.flatnonzero(newly):
                colours[k] = self.scene.regions[region_index].colour
            settled |= inside
        return _Local(rays, colours, angles, hits)


def _order_rays(branches):
    """Branch identities grouped into rays in counter-clockwise order, and the
    angle of each sector (sector ``k`` runs from ray ``k`` to ray ``k + 1``)."""
    if not branches:
        return [], [2 * math.pi]
    thetas = np.asarray([b[0] for b in branches])
    order = np.argsort(thetas, kind="stable")
    sorted_theta = thetas[order]
    gaps = np.diff(np.concatenate([sorted_theta, [sorted_theta[0] + 2 * math.pi]]))
    cut = int(np.argmax(gaps))
    rotated = list(order[cut + 1:]) + list(order[:cut + 1])
    unwrapped = []
    base = thetas[rotated[0]]
    for index in rotated:
        unwrapped.append((thetas[index] - base) % (2 * math.pi))
    groups: list[list[int]] = []
    group_angles: list[float] = []
    for position, index in enumerate(rotated):
        angle = unwrapped[position]
        if groups and angle - group_angles[-1] <= _ANGLE_TIE:
            groups[-1].append(index)
        else:
            groups.append([index])
            group_angles.append(angle)
    rays: list[list[tuple[int, int, int]]] = []
    ray_angles: list[float] = []
    for group, angle in zip(groups, group_angles):
        members = sorted(group, key=lambda i: branches[i][1])
        current: list[int] = []
        for index in members:
            kappa = branches[index][1]
            if current and abs(kappa - branches[current[-1]][1]) > 1e-7 * max(1.0, abs(kappa)):
                rays.append([branches[i][2] for i in current])
                ray_angles.append(angle)
                current = []
            current.append(index)
        rays.append([branches[i][2] for i in current])
        ray_angles.append(angle)
    sectors = []
    for k in range(len(rays)):
        following = ray_angles[k + 1] if k + 1 < len(rays) else ray_angles[0] + 2 * math.pi
        sectors.append(following - ray_angles[k])
    return rays, sectors


def _classify(local: _Local):
    """``(kind, turn_deg)`` for a candidate vertex, or None when no visible
    boundary passes through it or it is a smooth point of one."""
    count = len(local.rays)
    if count == 0:
        return None
    colours = local.colours
    boundary = [k for k in range(count) if colours[k - 1] != colours[k]]
    visible = [k for k in boundary if colours[k - 1] is not None and colours[k] is not None]
    if not visible:
        return None
    turn = 0.0
    for position, k in enumerate(boundary):
        following = boundary[(position + 1) % len(boundary)]
        colour = colours[k]
        if colour is None:
            continue
        angle, j = 0.0, k
        while True:
            angle += local.angles[j]
            j = (j + 1) % count
            if j == following:
                break
        turn = max(turn, abs(180.0 - math.degrees(angle)))
    if any(colour is None for colour in colours):
        return "edge", turn
    if len(visible) >= 3:
        return "junction", turn
    if len(visible) == 2:
        return ("corner", turn) if turn >= SMOOTH_TURN_DEG else None
    raise ValueError("a visible boundary ends inside the canvas")


def _dedupe(points: list[Point], tangent: list[bool]) -> list[Point]:
    """One point per cluster: points within ``CONTINUITY_TOLERANCE`` are one
    point, and a tangent contact absorbs points within 1e-3 px (its position
    along the common tangent is only known to about that). Transversal points
    are kept first, so a tangent contact at a join keeps the exact join."""
    cell = _TANGENT_RADIUS
    buckets: dict[tuple[int, int], list[Point]] = {}
    kept: list[Point] = []
    order = sorted(range(len(points)), key=lambda i: (tangent[i], points[i]))
    for index in order:
        point = points[index]
        radius = _TANGENT_RADIUS if tangent[index] else CONTINUITY_TOLERANCE
        cx, cy = math.floor(point[0] / cell), math.floor(point[1] / cell)
        if any(math.dist(point, other) <= radius
               for dx in (-1, 0, 1) for dy in (-1, 0, 1)
               for other in buckets.get((cx + dx, cy + dy), ())):
            continue
        buckets.setdefault((cx, cy), []).append(point)
        kept.append(point)
    return kept


def _candidates(truth: _Truth) -> list[Point]:
    scene = truth.scene
    points: list[Point] = []
    tangent: list[bool] = []
    for ring in truth.rings:
        for segment in ring.outline.segments:
            points.append(segment_start(segment))
            tangent.append(False)
    rings = truth.rings
    for a in range(len(rings)):
        for b in range(a + 1, len(rings)):
            boxes_a, boxes_b = rings[a].segment_boxes, rings[b].segment_boxes
            overlap = ((boxes_a[:, None, 0] <= boxes_b[None, :, 2])
                       & (boxes_b[None, :, 0] <= boxes_a[:, None, 2])
                       & (boxes_a[:, None, 1] <= boxes_b[None, :, 3])
                       & (boxes_b[None, :, 1] <= boxes_a[:, None, 3]))
            for i, j in zip(*np.nonzero(overlap)):
                sa = rings[a].outline.segments[i]
                sb = rings[b].outline.segments[j]
                for s, t, point in segment_intersections(sa, sb):
                    ta, tb = sa.d1(s), sb.d1(t)
                    sine = abs(float(ta[0] * tb[1] - ta[1] * tb[0])) / (
                        math.hypot(*ta) * math.hypot(*tb))
                    points.append(point)
                    tangent.append(sine < _TANGENT_SIN)
    margin = CONTINUITY_TOLERANCE
    inside = [(_snap_to_border(p, scene.width, scene.height, margin), t)
              for p, t in zip(points, tangent)
              if -margin <= p[0] <= scene.width + margin and -margin <= p[1] <= scene.height + margin]
    return _dedupe([p for p, _ in inside], [t for _, t in inside])


def _snap_to_border(p: Point, width: int, height: int, margin: float) -> Point:
    """``p`` with a coordinate within ``margin`` of the canvas border set onto it."""
    x, y = p
    for edge in (0.0, float(width)):
        if abs(x - edge) <= margin:
            x = edge
    for edge in (0.0, float(height)):
        if abs(y - edge) <= margin:
            y = edge
    return (x, y)


def _colour_geometry(scene) -> dict[int, object]:
    """Each palette colour's visible area, from one noded arrangement of every
    flattened outline and the canvas frame.

    The arrangement's faces partition the canvas and neighbouring faces share
    identical edges, so the faces of one colour dissolve exactly. (Chained
    region overlays do not: where two outlines coincide, the pieces of one colour
    can meet along edges that differ by rounding, and their union keeps a
    zero-width slit or spike that is a boundary of the colour with the colour on
    both sides.) A face takes the colour of the topmost region containing an
    interior point of it, by even-odd over the region's rings as the rasteriser
    fills them, or the background. A face below ``_SLIVER_AREA`` (a rounding
    sliver where outlines coincide) takes the colour of the neighbour it shares
    the longest edge with.
    """
    canvas = box(0.0, 0.0, float(scene.width), float(scene.height))
    lines = [canvas.exterior]
    region_rings = []
    for region in scene.regions:
        polygons = []
        for ring in region.rings:
            points = ring.flatten(FLATTEN_TOLERANCE)
            lines.append(LinearRing(points))
            polygons.append(Polygon(points))
        region_rings.append(polygons)
    noded = shapely.get_parts(shapely.union_all(lines))
    faces = np.asarray(shapely.get_parts(shapely.polygonize(noded)))
    inside = shapely.point_on_surface(faces)
    on_canvas = shapely.contains(canvas, inside)
    faces, inside = faces[on_canvas], inside[on_canvas]
    colours = np.full(len(faces), scene.background, dtype=np.int64)
    for region, polygons in zip(scene.regions, region_rings):
        parity = np.zeros(len(faces), dtype=bool)
        for polygon in polygons:
            parity ^= shapely.contains(polygon, inside)
        colours[parity] = region.colour
    areas = shapely.area(faces)
    whole = np.flatnonzero(areas >= _SLIVER_AREA)
    slivers = np.flatnonzero(areas < _SLIVER_AREA)
    if len(slivers) and len(whole):
        tree = shapely.STRtree(faces[whole])
        for index in slivers:
            neighbours = whole[tree.query(faces[index], predicate="intersects")]
            if len(neighbours):
                shared = shapely.length(shapely.intersection(
                    shapely.boundary(faces[neighbours]), faces[index].boundary))
                colours[index] = colours[neighbours[int(np.argmax(shared))]]
    geometry = {}
    for colour in sorted(set(colours.tolist())):
        merged = _without_slivers(_polygonal(shapely.union_all(faces[colours == colour])))
        if not merged.is_empty and merged.area > 0.0:
            geometry[colour] = merged
    return geometry


# Where flattened outlines coincide, rounding leaves slivers (faces, holes and
# parts of about 1e-13 px²); no scene feature is this small.
_SLIVER_AREA = 1e-6


def _without_slivers(geometry):
    """``geometry`` without polygon parts or holes smaller than ``_SLIVER_AREA``."""
    parts = []
    for polygon in getattr(geometry, "geoms", [geometry]):
        if polygon.area < _SLIVER_AREA:
            continue
        holes = [ring for ring in polygon.interiors if Polygon(ring).area >= _SLIVER_AREA]
        parts.append(Polygon(polygon.exterior, holes))
    if not parts:
        return Polygon()
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _polygonal(geometry):
    if isinstance(geometry, (Polygon, MultiPolygon)):
        return geometry
    parts = []
    pending = [geometry]
    while pending:
        item = pending.pop()
        if isinstance(item, Polygon):
            if not item.is_empty:
                parts.append(item)
        elif hasattr(item, "geoms"):
            pending.extend(shapely.get_parts(item))
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _conic_sweep(segment, t0: float, t1: float) -> float:
    return abs(segment.sweep) * (t1 - t0)


def _ideal_nodes(truth: _Truth, vertices: list[Point], locals_: list[_Local],
                 vertex_kinds: dict[int, str]) -> int:
    hits: dict[tuple[int, int], set[float]] = {}
    for local in locals_:
        for ring_id, index, t in local.hits:
            hits.setdefault((ring_id, index), set()).add(t)
    tree = cKDTree(np.asarray(vertices)) if vertices else None
    pieces = []  # (ring_id, index, t0, t1, vertex_a, vertex_b)
    for ring_id, ring in enumerate(truth.rings):
        if ring.region == _CANVAS:
            continue
        for index, segment in enumerate(ring.outline.segments):
            params = sorted({0.0, 1.0} | hits.get((ring_id, index), set()))
            for t0, t1 in zip(params[:-1], params[1:]):
                if t1 - t0 <= 1e-12:
                    continue
                middle = 0.5 * (t0 + t1)
                ends = segment.point(np.asarray([t0, middle, t1]))
                if max(np.linalg.norm(ends[1] - ends[0]), np.linalg.norm(ends[2] - ends[1])) \
                        <= 4 * CONTINUITY_TOLERANCE:
                    continue
                local = truth.local((float(ends[1][0]), float(ends[1][1])))
                identity = (ring_id, index, 1)
                ray = next((k for k, members in enumerate(local.rays) if identity in members), None)
                if ray is None:
                    raise ValueError("a piece's midpoint lost its own outline")
                left, right = local.colours[ray], local.colours[ray - 1]
                if left is None or right is None or left == right:
                    continue
                owners = [(r, i) for r, i, _ in local.rays[ray] if truth.rings[r].region != _CANVAS]
                if min(owners) != (ring_id, index):
                    continue
                vertex = []
                for end in (ends[0], ends[2]):
                    distance, nearest = tree.query(end)
                    if distance > 1e-5:
                        raise ValueError(f"a boundary piece ends at {tuple(end)}, off every vertex")
                    vertex.append(int(nearest))
                pieces.append((ring_id, index, t0, t1, vertex[0], vertex[1]))

    def carrier(piece):
        segment = truth.rings[piece[0]].outline.segments[piece[1]]
        if isinstance(segment, Line):
            return ("line",)
        key = segment.carrier()
        return key if key is not None else ("segment", piece[0], piece[1])

    ends_at: dict[int, list[int]] = {}
    for number, piece in enumerate(pieces):
        ends_at.setdefault(piece[4], []).append(number)
        ends_at.setdefault(piece[5], []).append(number)
    parent = list(range(len(pieces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    merged_vertices = set()
    for vertex, incident in ends_at.items():
        if len(incident) != 2 or vertex in vertex_kinds:
            continue
        first, second = (pieces[i] for i in incident)
        same_segment = first[:2] == second[:2]
        key_a, key_b = carrier(first), carrier(second)
        if same_segment or (key_a == key_b and key_a[0] == "line") or \
                (key_a[0] in ("circle", "ellipse") and _same_key(key_a, key_b)):
            merged_vertices.add(vertex)
            parent[find(incident[0])] = find(incident[1])
    runs: dict[int, dict] = {}
    for number, piece in enumerate(pieces):
        run = runs.setdefault(find(number), {"sweep": 0.0, "conic": False, "open": False})
        segment = truth.rings[piece[0]].outline.segments[piece[1]]
        if segment.carrier() is not None:
            run["conic"] = True
            run["sweep"] += _conic_sweep(segment, piece[2], piece[3])
        if piece[4] not in merged_vertices or piece[5] not in merged_vertices:
            run["open"] = True
    nodes = sum(1 for vertex in ends_at if vertex not in merged_vertices)
    for run in runs.values():
        if run["conic"]:
            quarters = max(1, math.ceil(run["sweep"] / (math.pi / 2) - 1e-9))
            nodes += quarters - 1 if run["open"] else quarters
        elif not run["open"]:
            nodes += 1
    return nodes


def _same_key(a, b) -> bool:
    if a[0] != b[0]:
        return False
    va, vb = np.asarray(a[1:]), np.asarray(b[1:])
    scale = max(1.0, float(np.max(np.abs(va))))
    if a[0] == "ellipse":
        angle = abs(va[-1] - vb[-1]) % math.pi
        return min(angle, math.pi - angle) <= 1e-12 and bool(
            np.all(np.abs(va[:-1] - vb[:-1]) <= 1e-12 * scale))
    return bool(np.all(np.abs(va - vb) <= 1e-12 * scale))


def scene_truth(scene) -> SceneTruth:
    """The visible colour geometry, true corners and ideal node count of a scene
    (see the module docstring for the exact definitions and bounds)."""
    truth = _Truth(scene)
    vertices = _candidates(truth)
    locals_ = [truth.local(point) for point in vertices]
    corners: list[TrueCorner] = []
    kinds: dict[int, str] = {}
    for index, (point, local) in enumerate(zip(vertices, locals_)):
        classified = _classify(local)
        if classified is None:
            continue
        kind, turn = classified
        kinds[index] = kind
        corners.append(TrueCorner(point, turn, kind))
    corners.sort(key=lambda corner: (round(corner.point[1], 6), round(corner.point[0], 6)))
    return SceneTruth(width=scene.width, height=scene.height,
                      colour_geometry=_colour_geometry(scene), corners=corners,
                      ideal_nodes=_ideal_nodes(truth, vertices, locals_, kinds))
