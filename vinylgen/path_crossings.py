"""Crossing detection for fitted boundary pieces.

Every traced path is assembled from fitted pieces (the shared chains, or a
ring fitted on its own), so a path crosses itself exactly where two of its
pieces cross or one piece crosses itself. Pieces may meet only at a vertex
both of them carry exactly: consecutive primitives of one piece at their
shared node, pieces ending together at a junction or endpoint anchor, and a
touch, where pieces meet at a shared vertex without crossing there (a region
meeting itself at a corner contact). Every other contact is a defect: a
crossing, a touch anywhere else, or pieces running along one another.

The test is exact down to ``RESOLUTION``, with no sampling. Each pair of
primitives whose control boxes overlap is subdivided while the boxes of its
parts still overlap; a curve lies inside its control hull, so parts whose
boxes are apart cannot meet. Near a vertex both primitives carry, the parts
there are cleared once the cones their control points span from the vertex
are disjoint, since each part lies inside its cone. Parts still in contact at
``RESOLUTION`` are a contact. A cubic's own loop is found in closed form.
Whether pieces visiting one vertex cross there is decided from their tangents
at it.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Hashable, Mapping

import numpy as np
import shapely
from numba import njit
from shapely.strtree import STRtree

Point = tuple[float, float]

# Size, in pixels, below which two parts still in contact are a contact.
RESOLUTION = 1e-7
# Subdivision steps spent on one pair of primitives before it counts as a
# contact: only curves running along one another within about RESOLUTION for
# a long stretch get this far.
_STEP_BUDGET = 200_000


@dataclass(frozen=True)
class Crossing:
    """Two pieces (the same piece twice for a self-crossing) in illegal contact at ``point``.

    ``first_primitives`` and ``second_primitives`` index the primitives of each
    piece that take part: the two in contact, or at a vertex the pieces cross
    through, those meeting at it.
    """
    first: Hashable
    second: Hashable
    point: Point
    first_primitives: tuple[int, ...] = ()
    second_primitives: tuple[int, ...] = ()


def cubic_loop(p0: Point, c1: Point, c2: Point, p3: Point) -> Point | None:
    """Where the cubic crosses or runs back along itself, or ``None``.

    With ``B(t) = a t^3 + b t^2 + c t + P0``, ``B(s) = B(t)`` for ``s != t`` reduces
    to ``a (sigma^2 - pi) + b sigma + c = 0`` in ``sigma = s + t`` and
    ``pi = s t``: two linear equations giving ``sigma`` and then ``pi``, so ``s``
    and ``t`` are the roots of ``u^2 - sigma u + pi``. The cubic loops when both
    lie inside ``(0, 1)``; ``s = 0`` with ``t = 1`` is a closed cubic's own ends.
    When ``a`` and ``b`` are parallel the cubic cannot loop transversally, but
    with all four controls on one line it runs back along itself wherever its
    derivative along that line changes sign.
    """
    ax = -p0[0] + 3.0 * c1[0] - 3.0 * c2[0] + p3[0]
    ay = -p0[1] + 3.0 * c1[1] - 3.0 * c2[1] + p3[1]
    bx = 3.0 * p0[0] - 6.0 * c1[0] + 3.0 * c2[0]
    by = 3.0 * p0[1] - 6.0 * c1[1] + 3.0 * c2[1]
    cx = 3.0 * (c1[0] - p0[0])
    cy = 3.0 * (c1[1] - p0[1])
    denominator = ay * bx - ax * by
    if denominator == 0.0 or abs(denominator) <= 1e-12 * (math.hypot(ax, ay) * math.hypot(bx, by)):
        return _collinear_reversal(p0, c1, c2, p3)
    sigma = (ax * cy - ay * cx) / denominator
    if abs(ax) >= abs(ay):
        pi = sigma * sigma + (bx * sigma + cx) / ax
    else:
        pi = sigma * sigma + (by * sigma + cy) / ay
    discriminant = sigma * sigma - 4.0 * pi
    if discriminant <= 0.0:
        return None
    root = math.sqrt(discriminant)
    s = (sigma - root) / 2.0
    t = (sigma + root) / 2.0
    if s <= 1e-9 or t >= 1.0 - 1e-9 or t - s <= 1e-9:
        return None
    return (((ax * s + bx) * s + cx) * s + p0[0], ((ay * s + by) * s + cy) * s + p0[1])


def _collinear_reversal(p0: Point, c1: Point, c2: Point, p3: Point) -> Point | None:
    """Where a cubic whose controls lie on one line turns back along it, or ``None``."""
    controls = np.asarray((p0, c1, c2, p3), dtype=np.float64)
    offsets = controls - controls[0]
    extent = np.hypot(offsets[:, 0], offsets[:, 1])
    if extent.max() == 0.0:
        return None
    direction = offsets[int(np.argmax(extent))] / extent.max()
    across = offsets[:, 0] * direction[1] - offsets[:, 1] * direction[0]
    if np.abs(across).max() > 1e-12 * extent.max():
        return None
    along = offsets @ direction
    # The derivative along the line is the quadratic Bernstein polynomial of
    # the control-leg projections; where it changes sign inside (0, 1) the
    # cubic turns back.
    d0, d1, d2 = along[1] - along[0], along[2] - along[1], along[3] - along[2]
    slope = lambda u: (1 - u) ** 2 * d0 + 2 * (1 - u) * u * d1 + u * u * d2
    for root in np.roots([d0 - 2.0 * d1 + d2, 2.0 * (d1 - d0), d0]):
        if abs(root.imag) > 1e-12 or not 1e-9 < root.real < 1.0 - 1e-9:
            continue
        t = float(root.real)
        if slope(t - 1e-6) * slope(t + 1e-6) < 0.0:
            u = 1.0 - t
            point = (u ** 3 * controls[0] + 3 * u * u * t * controls[1]
                     + 3 * u * t * t * controls[2] + t ** 3 * controls[3])
            return (float(point[0]), float(point[1]))
    return None


@njit(cache=True, nogil=True)
def _blossom_nb(c: np.ndarray, u: float, v: float, w: float, axis: int) -> float:
    a0 = c[0, axis] + (c[1, axis] - c[0, axis]) * u
    a1 = c[1, axis] + (c[2, axis] - c[1, axis]) * u
    a2 = c[2, axis] + (c[3, axis] - c[2, axis]) * u
    b0 = a0 + (a1 - a0) * v
    b1 = a1 + (a2 - a1) * v
    return b0 + (b1 - b0) * w


@njit(cache=True, nogil=True)
def _part_nb(c: np.ndarray, t0: float, t1: float, out: np.ndarray) -> None:
    """Controls of the cubic ``c`` restricted to ``[t0, t1]``, its own ends exactly."""
    for axis in range(2):
        if t0 == 0.0:
            out[0, axis] = c[0, axis]
        else:
            out[0, axis] = _blossom_nb(c, t0, t0, t0, axis)
        out[1, axis] = _blossom_nb(c, t0, t0, t1, axis)
        out[2, axis] = _blossom_nb(c, t0, t1, t1, axis)
        if t1 == 1.0:
            out[3, axis] = c[3, axis]
        else:
            out[3, axis] = _blossom_nb(c, t1, t1, t1, axis)


@njit(cache=True, nogil=True)
def _cone_nb(part: np.ndarray, at_start: bool, vx: float, vy: float):
    """The cone of directions the part spans from its end ``(vx, vy)``.

    Returns ``(kind, start, width)``: kind 0 for a part that is only the
    vertex, 1 for the cone of angles ``[start, start + width]`` (width below
    pi), 2 for a cone too wide to certify anything.
    """
    reference = 0.0
    low = 0.0
    high = 0.0
    found = False
    for k in range(1, 4):
        index = k if at_start else 3 - k
        dx = part[index, 0] - vx
        dy = part[index, 1] - vy
        if dx == 0.0 and dy == 0.0:
            continue
        angle = math.atan2(dy, dx)
        if not found:
            reference = angle
            found = True
            continue
        relative = angle - reference
        if relative > math.pi:
            relative -= 2.0 * math.pi
        elif relative <= -math.pi:
            relative += 2.0 * math.pi
        if relative < low:
            low = relative
        if relative > high:
            high = relative
    if not found:
        return 0, 0.0, 0.0
    if high - low >= math.pi:
        return 2, 0.0, 0.0
    return 1, reference + low, high - low


@njit(cache=True, nogil=True)
def _cones_apart_nb(first_start: float, first_width: float,
                    second_start: float, second_width: float) -> bool:
    tau = 2.0 * math.pi
    ahead = (second_start - first_start) % tau
    behind = (first_start - second_start) % tau
    return ahead > first_width and behind > second_width


@njit(cache=True, nogil=True)
def _certify_nb(controls: np.ndarray, left: np.ndarray, right: np.ndarray,
                resolution: float, budget: int, contact: np.ndarray,
                points: np.ndarray) -> None:
    """For each primitive pair, whether they meet anywhere but a shared end.

    Sets ``contact[k]`` and ``points[k]`` (where) for pair ``(left[k], right[k])``.
    The subdivision is depth-first and stops at the first contact.
    """
    stack = np.empty((256, 4), dtype=np.float64)
    part_a = np.empty((4, 2), dtype=np.float64)
    part_b = np.empty((4, 2), dtype=np.float64)
    shared = np.empty((4, 2), dtype=np.float64)
    for k in range(left.size):
        a = controls[left[k]]
        b = controls[right[k]]
        count = 0
        for ea in range(2):
            for eb in range(2):
                if a[3 * ea, 0] == b[3 * eb, 0] and a[3 * ea, 1] == b[3 * eb, 1]:
                    shared[count, 0] = float(ea)
                    shared[count, 1] = float(eb)
                    count += 1
        top = 1
        stack[0, 0] = 0.0
        stack[0, 1] = 1.0
        stack[0, 2] = 0.0
        stack[0, 3] = 1.0
        steps = 0
        while top > 0:
            top -= 1
            a0 = stack[top, 0]
            a1 = stack[top, 1]
            b0 = stack[top, 2]
            b1 = stack[top, 3]
            _part_nb(a, a0, a1, part_a)
            _part_nb(b, b0, b1, part_b)
            steps += 1
            ax0 = min(part_a[0, 0], part_a[1, 0], part_a[2, 0], part_a[3, 0])
            ax1 = max(part_a[0, 0], part_a[1, 0], part_a[2, 0], part_a[3, 0])
            ay0 = min(part_a[0, 1], part_a[1, 1], part_a[2, 1], part_a[3, 1])
            ay1 = max(part_a[0, 1], part_a[1, 1], part_a[2, 1], part_a[3, 1])
            bx0 = min(part_b[0, 0], part_b[1, 0], part_b[2, 0], part_b[3, 0])
            bx1 = max(part_b[0, 0], part_b[1, 0], part_b[2, 0], part_b[3, 0])
            by0 = min(part_b[0, 1], part_b[1, 1], part_b[2, 1], part_b[3, 1])
            by1 = max(part_b[0, 1], part_b[1, 1], part_b[2, 1], part_b[3, 1])
            if ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0:
                continue
            size_a = math.hypot(ax1 - ax0, ay1 - ay0)
            size_b = math.hypot(bx1 - bx0, by1 - by0)
            vertex = -1
            for s in range(count):
                ea = shared[s, 0]
                eb = shared[s, 1]
                if (ea == a0 or ea == a1) and (eb == b0 or eb == b1):
                    vertex = s
            small = (size_a < resolution and size_b < resolution) or steps > budget
            if vertex >= 0:
                start_a = shared[vertex, 0] == a0
                start_b = shared[vertex, 1] == b0
                vx = part_a[0, 0] if start_a else part_a[3, 0]
                vy = part_a[0, 1] if start_a else part_a[3, 1]
                kind_a, cone_a, width_a = _cone_nb(part_a, start_a, vx, vy)
                kind_b, cone_b, width_b = _cone_nb(part_b, start_b, vx, vy)
                if kind_a == 0 or kind_b == 0:
                    continue
                if kind_a == 1 and kind_b == 1 and _cones_apart_nb(cone_a, width_a,
                                                                   cone_b, width_b):
                    continue
                if small:
                    contact[k] = True
                    points[k, 0] = vx
                    points[k, 1] = vy
                    break
            elif small or top + 2 > stack.shape[0]:
                contact[k] = True
                points[k, 0] = 0.5 * (part_a[0, 0] + part_a[3, 0])
                points[k, 1] = 0.5 * (part_a[0, 1] + part_a[3, 1])
                break
            if top + 2 > stack.shape[0]:
                contact[k] = True
                points[k, 0] = 0.5 * (part_a[0, 0] + part_a[3, 0])
                points[k, 1] = 0.5 * (part_a[0, 1] + part_a[3, 1])
                break
            if size_a >= size_b:
                middle = 0.5 * (a0 + a1)
                stack[top, 0] = a0
                stack[top, 1] = middle
                stack[top, 2] = b0
                stack[top, 3] = b1
                stack[top + 1, 0] = middle
                stack[top + 1, 1] = a1
                stack[top + 1, 2] = b0
                stack[top + 1, 3] = b1
            else:
                middle = 0.5 * (b0 + b1)
                stack[top, 0] = a0
                stack[top, 1] = a1
                stack[top, 2] = b0
                stack[top, 3] = middle
                stack[top + 1, 0] = a0
                stack[top + 1, 1] = a1
                stack[top + 1, 2] = middle
                stack[top + 1, 3] = b1
            top += 2


def _controls(start: Point, primitives) -> tuple[np.ndarray, np.ndarray]:
    """Each primitive of non-zero extent as four cubic controls (a line exactly),
    with its index among ``primitives``."""
    rows = []
    indices = []
    cursor = start
    for index, primitive in enumerate(primitives):
        end = primitive[-1]
        if primitive[0] == "L":
            row = (cursor,
                   (cursor[0] + (end[0] - cursor[0]) / 3.0, cursor[1] + (end[1] - cursor[1]) / 3.0),
                   (cursor[0] + 2.0 * (end[0] - cursor[0]) / 3.0,
                    cursor[1] + 2.0 * (end[1] - cursor[1]) / 3.0),
                   end)
        else:
            row = (cursor, primitive[1], primitive[2], end)
        if any(point != cursor for point in row):
            rows.append(row)
            indices.append(index)
        cursor = end
    return (np.asarray(rows, dtype=np.float64).reshape(-1, 4, 2),
            np.asarray(indices, dtype=np.int64))


def _tangent(row: np.ndarray, at_start: bool) -> float:
    """The direction, as an angle, in which the primitive leaves its end."""
    origin = row[0] if at_start else row[3]
    for index in ((1, 2, 3) if at_start else (2, 1, 0)):
        dx, dy = row[index] - origin
        if dx != 0.0 or dy != 0.0:
            return math.atan2(dy, dx) % math.tau
    return 0.0


def _visits(rows: np.ndarray, point: tuple[float, float]) -> list[tuple[tuple, tuple]]:
    """Each visit of the piece ``rows`` to ``point``: its directions and rows.

    A visit through the point leaves it twice (back along the arriving
    primitive and on along the next); an end leaves it once, and a closed
    piece's two ends at its seam make one visit through. Each visit is
    ``(directions, rows at the point)``.
    """
    count = len(rows)
    closed = count > 0 and bool(np.all(rows[0, 0] == rows[-1, 3]))
    visits = []
    for index in range(count + 1):
        node = rows[index, 0] if index < count else rows[-1, 3]
        if node[0] != point[0] or node[1] != point[1]:
            continue
        if 0 < index < count:
            visits.append(((_tangent(rows[index - 1], False), _tangent(rows[index], True)),
                           (index - 1, index)))
        elif closed and index == 0:
            visits.append(((_tangent(rows[0], True), _tangent(rows[-1], False)),
                           (0, count - 1)))
        elif not closed:
            visits.append(((_tangent(rows[0], True),), (0,)) if index == 0
                          else ((_tangent(rows[-1], False),), (count - 1,)))
    return visits


def _crossing_visits(visits: list[tuple[float, ...]]) -> list[tuple[int, int]]:
    """The pairs of visits to one vertex that cross there or leave it the same way.

    Two visits leave the same way when they share a direction (a visit through
    the vertex that doubles back pairs with itself). Two visits through the
    vertex cross when exactly one of the second's directions lies strictly
    inside the turn from the first's one direction anticlockwise to its other.
    """
    pairs = set()
    for first, visit in enumerate(visits):
        if len(visit) == 2 and visit[0] == visit[1]:
            pairs.add((first, first))
        for second in range(first + 1, len(visits)):
            if set(visit) & set(visits[second]):
                pairs.add((first, second))
    for first, visit in enumerate(visits):
        if len(visit) != 2:
            continue
        low, high = visit
        span = (high - low) % math.tau
        for second in range(first + 1, len(visits)):
            other = visits[second]
            if len(other) != 2:
                continue
            inside = [0.0 < (angle - low) % math.tau < span for angle in other]
            if inside[0] != inside[1]:
                pairs.add((first, second))
    return sorted(pairs)


def crossings(pieces: Mapping[Hashable, tuple[Point, tuple]], *, only: set | None = None,
              cache: dict | None = None) -> list[Crossing]:
    """Every illegal contact among ``pieces``, each ``key -> (start, primitives)``.

    With ``only``, just the contacts involving at least one of those keys. A
    caller checking the same pieces repeatedly may pass one ``cache`` dict to
    every call: a piece whose start and primitives tuple (the same object) are
    unchanged reuses its controls and cubic loops.

    Sorted by the keys' string forms and then the point, so the result does not
    depend on the mapping's order.
    """
    found: list[Crossing] = []
    keys = list(pieces)
    rows_of: list[np.ndarray] = []
    indices_of: list[np.ndarray] = []
    owners: list[np.ndarray] = []
    for number, key in enumerate(keys):
        start, primitives = pieces[key]
        entry = cache.get(key) if cache is not None else None
        if entry is None or entry[1] is not primitives or entry[0] != start:
            rows, indices = _controls(start, primitives)
            loops = [(int(index), loop) for row, index in zip(rows, indices)
                     if (loop := cubic_loop(*map(tuple, row))) is not None]
            entry = (start, primitives, rows, indices, loops)
            if cache is not None:
                cache[key] = entry
        _, _, rows, indices, loops = entry
        if only is None or key in only:
            found.extend(Crossing(key, key, loop, (index,), (index,)) for index, loop in loops)
        rows_of.append(rows)
        indices_of.append(indices)
        owners.append(np.full(len(rows), number, dtype=np.int64))
    ordered = lambda items: sorted(set(items), key=lambda item: (
        str(item.first), str(item.second), item.point))
    if not any(len(rows) for rows in rows_of):
        return ordered(found)
    controls = np.ascontiguousarray(np.concatenate(rows_of))
    owner = np.concatenate(owners)
    primitive = np.concatenate(indices_of)
    low = controls.min(axis=1)
    high = controls.max(axis=1)
    boxes = shapely.box(low[:, 0], low[:, 1], high[:, 0], high[:, 1])
    tree = STRtree(boxes)
    if only is None:
        left, right = tree.query(boxes, predicate="intersects")
    else:
        chosen = np.flatnonzero(np.isin(
            owner, [number for number, key in enumerate(keys) if key in only]))
        query, right = tree.query(boxes[chosen], predicate="intersects")
        left = chosen[query]
        left, right = np.minimum(left, right), np.maximum(left, right)
        if left.size:
            pairs = np.unique(np.stack((left, right), axis=1), axis=0)
            left, right = pairs[:, 0], pairs[:, 1]
    pick = left < right
    left = np.ascontiguousarray(left[pick])
    right = np.ascontiguousarray(right[pick])
    contact = np.zeros(left.size, dtype=np.bool_)
    points = np.zeros((left.size, 2), dtype=np.float64)
    _certify_nb(controls, left, right, RESOLUTION, _STEP_BUDGET, contact, points)
    for a, b, first, second, point in zip(
            owner[left[contact]].tolist(), owner[right[contact]].tolist(),
            primitive[left[contact]].tolist(), primitive[right[contact]].tolist(),
            points[contact].tolist()):
        found.append(Crossing(keys[a], keys[b], (point[0], point[1]), (first,), (second,)))

    # Crossing through a vertex that two pieces, or one piece twice, visit.
    last = np.r_[np.diff(owner) != 0, True]
    nodes = np.concatenate((controls[:, 0], controls[last, 3]))
    node_owner = np.concatenate((owner, owner[last]))
    order = np.lexsort((node_owner, nodes[:, 1], nodes[:, 0]))
    nodes, node_owner = nodes[order], node_owner[order]
    candidates: dict[tuple[float, float], set[int]] = {}
    for index in np.flatnonzero(np.all(nodes[1:] == nodes[:-1], axis=1)).tolist():
        point = (float(nodes[index, 0]), float(nodes[index, 1]))
        candidates.setdefault(point, set()).update(
            (int(node_owner[index]), int(node_owner[index + 1])))
    for point, members in candidates.items():
        if only is not None and not any(keys[number] in only for number in members):
            continue
        visits = [(number, visit) for number in sorted(members)
                  for visit in _visits(rows_of[number], point)]
        if len(visits) < 2:
            continue
        for first, second in _crossing_visits([directions for _, (directions, _) in visits]):
            (a, (_, rows_a)), (b, (_, rows_b)) = visits[first], visits[second]
            found.append(Crossing(keys[a], keys[b], point,
                                  tuple(sorted(int(indices_of[a][row]) for row in rows_a)),
                                  tuple(sorted(int(indices_of[b][row]) for row in rows_b))))
    return ordered(found)
