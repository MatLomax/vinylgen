from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Literal

import numpy as np
from numba import njit
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from vinylgen.contracts import TraceSettings
from vinylgen.palette import Progress, _noop

Point = tuple[float, float]
Primitive = tuple
AnchorKind = Literal["junction", "endpoint"]


@dataclass(frozen=True)
class HardAnchor:
    point: Point
    kind: AnchorKind

    def __post_init__(self) -> None:
        if self.kind not in ("junction", "endpoint"):
            raise ValueError(f"unsupported hard-anchor kind {self.kind!r}")


@dataclass(frozen=True)
class FitMetrics:
    lines: int
    cubics: int
    hard_anchors: int
    source_samples: int
    final_nodes: int

    def as_dict(self) -> dict[str, int | float]:
        reduction = self.source_samples - self.final_nodes
        return {"lines": self.lines, "cubics": self.cubics,
                "hard_anchors": self.hard_anchors,
                "source_samples": self.source_samples,
                "final_nodes": self.final_nodes,
                "node_reduction": reduction,
                "node_reduction_ratio": (reduction / self.source_samples
                                         if self.source_samples else 0.0)}


@dataclass(frozen=True)
class RunDiagnostic:
    classification: Literal["straight", "curve", "corner"]
    source_start: int
    source_end: int
    max_error: float
    primitives: int
    recovery_attempts: int = 0
    dense_fallback: bool = False
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "max_error": self.max_error,
            "primitives": self.primitives,
            "recovery_attempts": self.recovery_attempts,
            "dense_fallback": self.dense_fallback,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class ChainFit:
    start: Point
    primitives: tuple[Primitive, ...]
    metrics: FitMetrics
    diagnostics: tuple[RunDiagnostic, ...] = ()

    def reversed(self) -> "ChainFit":
        cursor = self.start
        sections: list[tuple[Point, Primitive]] = []
        for primitive in self.primitives:
            sections.append((cursor, primitive))
            cursor = primitive[-1]
        reversed_primitives: list[Primitive] = []
        for section_start, primitive in reversed(sections):
            if primitive[0] == "L":
                reversed_primitives.append(("L", section_start))
            else:
                reversed_primitives.append(("C", primitive[2], primitive[1], section_start))
        return ChainFit(cursor, tuple(reversed_primitives), self.metrics,
                        tuple(reversed(self.diagnostics)))


@dataclass(frozen=True)
class RingFit:
    path: str
    metrics: FitMetrics


@njit(cache=True, nogil=True)
def _distance_to_line_nb(points: np.ndarray, start: int, end: int):
    """Max perpendicular distance of samples ``start..end`` from their chord.

    Returns ``(max_distance, local_index)`` where ``local_index`` is the offset
    from ``start`` of the farthest sample. Ties keep the first sample, matching
    ``np.argmax``. Callable from the compiled fitter and from Python.
    """
    n = end - start + 1
    vx = points[end, 0] - points[start, 0]
    vy = points[end, 1] - points[start, 1]
    length = math.sqrt(vx * vx + vy * vy)
    best = -1.0
    best_index = 0
    if length < 1e-12:
        for i in range(n):
            dx = points[start + i, 0] - points[start, 0]
            dy = points[start + i, 1] - points[start, 1]
            distance = math.sqrt(dx * dx + dy * dy)
            if distance > best:
                best = distance
                best_index = i
    else:
        for i in range(n):
            dx = points[start + i, 0] - points[start, 0]
            dy = points[start + i, 1] - points[start, 1]
            distance = abs(vx * dy - vy * dx) / length
            if distance > best:
                best = distance
                best_index = i
    return best, best_index


def _distance_to_line(points: np.ndarray) -> tuple[float, int]:
    array = np.ascontiguousarray(points, dtype=np.float64)
    distance, index = _distance_to_line_nb(array, 0, len(array) - 1)
    return float(distance), int(index)


@dataclass(frozen=True)
class _LineEvidence:
    origin: np.ndarray
    direction: np.ndarray
    maximum_error: float
    length: float


def _robust_line(points: np.ndarray) -> _LineEvidence:
    """Total-least-squares line evidence, insensitive to axis stair steps.

    The principal direction is the eigenvector of the larger eigenvalue of the
    centred 2x2 covariance ``[[a, b], [b, c]]``, obtained in closed form:
    ``lambda1 = (a + c) / 2 + sqrt(((a - c) / 2)^2 + b^2)``. This equals the
    direction ``svd`` returns up to sign and to ~1e-12, and unlike ``svd`` it is
    reproducible in the Numba corner kernel below.
    """
    origin = points.mean(axis=0)
    centred = points - origin
    if len(points) < 2 or not np.any(centred):
        return _LineEvidence(origin, np.array((1.0, 0.0)), 0.0, 0.0)
    a = float(centred[:, 0] @ centred[:, 0])
    b = float(centred[:, 0] @ centred[:, 1])
    c = float(centred[:, 1] @ centred[:, 1])
    half_diff = (a - c) * 0.5
    root = math.sqrt(half_diff * half_diff + b * b)
    if b == 0.0:
        # Axis-aligned covariance: the principal axis is x when a >= c else y.
        direction = np.array((1.0, 0.0)) if a >= c else np.array((0.0, 1.0))
    else:
        # lambda1 - c = half_diff + root, strictly positive when b != 0, so this
        # eigenvector is always well-defined here.
        eigen_x = half_diff + root
        direction = np.array((eigen_x, b)) / math.hypot(eigen_x, b)
    if np.dot(direction, points[-1] - points[0]) < 0:
        direction = -direction
    projections = centred @ direction
    errors = np.abs(direction[0] * centred[:, 1] - direction[1] * centred[:, 0])
    return _LineEvidence(origin, direction, float(errors.max()),
                         float(projections.max() - projections.min()))


@njit(cache=True)
def _side_line(points: np.ndarray, index: int, step: int, span: float):
    """Closed-form line evidence for one arc-length side window of a corner.

    Walks from ``index`` in the ``step`` direction accumulating physical arc
    length up to ``span``, then computes the same origin, principal direction,
    maximum perpendicular error, and projection length as ``_robust_line``.
    Returns ``(dir_x, dir_y, maximum_error, length, origin_x, origin_y)``.
    """
    n = points.shape[0]
    walk = np.empty(n, dtype=np.int64)
    walk[0] = index
    count = 1
    travelled = 0.0
    cursor = index
    while 0 <= cursor + step < n and travelled < span:
        following = cursor + step
        dx = points[following, 0] - points[cursor, 0]
        dy = points[following, 1] - points[cursor, 1]
        travelled += math.sqrt(dx * dx + dy * dy)
        walk[count] = following
        count += 1
        cursor = following
    origin_x = points[index, 0]
    origin_y = points[index, 1]
    if count < 2:
        return 1.0, 0.0, 0.0, 0.0, origin_x, origin_y
    sum_x = 0.0
    sum_y = 0.0
    for i in range(count):
        p = walk[i]
        sum_x += points[p, 0]
        sum_y += points[p, 1]
    origin_x = sum_x / count
    origin_y = sum_y / count
    a = 0.0
    b = 0.0
    c = 0.0
    any_nonzero = False
    for i in range(count):
        p = walk[i]
        cx = points[p, 0] - origin_x
        cy = points[p, 1] - origin_y
        if cx != 0.0 or cy != 0.0:
            any_nonzero = True
        a += cx * cx
        b += cx * cy
        c += cy * cy
    if not any_nonzero:
        return 1.0, 0.0, 0.0, 0.0, origin_x, origin_y
    half_diff = (a - c) * 0.5
    root = math.sqrt(half_diff * half_diff + b * b)
    if b == 0.0:
        if a >= c:
            dir_x = 1.0
            dir_y = 0.0
        else:
            dir_x = 0.0
            dir_y = 1.0
    else:
        eigen_x = half_diff + root
        norm = math.sqrt(eigen_x * eigen_x + b * b)
        dir_x = eigen_x / norm
        dir_y = b / norm
    # Sign convention on the (possibly reversed) window: a backward walk yields a
    # window whose first sample is the far point and last sample is ``index``.
    if step < 0:
        sign_x = points[walk[0], 0] - points[walk[count - 1], 0]
        sign_y = points[walk[0], 1] - points[walk[count - 1], 1]
    else:
        sign_x = points[walk[count - 1], 0] - points[walk[0], 0]
        sign_y = points[walk[count - 1], 1] - points[walk[0], 1]
    if dir_x * sign_x + dir_y * sign_y < 0.0:
        dir_x = -dir_x
        dir_y = -dir_y
    proj_min = math.inf
    proj_max = -math.inf
    error_max = 0.0
    for i in range(count):
        p = walk[i]
        cx = points[p, 0] - origin_x
        cy = points[p, 1] - origin_y
        proj = cx * dir_x + cy * dir_y
        if proj < proj_min:
            proj_min = proj
        if proj > proj_max:
            proj_max = proj
        error = abs(dir_x * cy - dir_y * cx)
        if error > error_max:
            error_max = error
    return dir_x, dir_y, error_max, proj_max - proj_min, origin_x, origin_y


@njit(cache=True)
def _corner_candidates(points: np.ndarray, deviation: float, minimum: float,
                       span: float, threshold: float, maximum_move: float):
    """Per-index corner evidence for the two-scale sustained-run test.

    For every interior sample both sides must show an independent sustained
    straight run at ``span`` and again at ``span * 1.75``, the turn must reach
    ``threshold`` at each scale, the two span-scale lines must intersect, and
    that intersection must lie within ``maximum_move`` of the sample. Emits
    ``(valid, turn, ix, iy)`` per index; the span-scale turn and intersection
    feed the Python non-max suppression.
    """
    n = points.shape[0]
    valid = np.zeros(n, dtype=np.bool_)
    turns = np.zeros(n, dtype=np.float64)
    ix = np.zeros(n, dtype=np.float64)
    iy = np.zeros(n, dtype=np.float64)
    for index in range(1, n - 1):
        ok = True
        span_turn = 0.0
        b_dx = b_dy = b_ox = b_oy = 0.0
        a_dx = a_dy = a_ox = a_oy = 0.0
        for scale_step in range(2):
            scale = span if scale_step == 0 else span * 1.75
            bdx, bdy, berr, blen, box, boy = _side_line(points, index, -1, scale)
            if blen < minimum or berr > deviation:
                ok = False
                break
            adx, ady, aerr, alen, aox, aoy = _side_line(points, index, 1, scale)
            if alen < minimum or aerr > deviation:
                ok = False
                break
            dot = bdx * adx + bdy * ady
            if dot > 1.0:
                dot = 1.0
            elif dot < -1.0:
                dot = -1.0
            turn = math.acos(dot)
            if turn < threshold:
                ok = False
                break
            if scale_step == 0:
                span_turn = turn
                b_dx, b_dy, b_ox, b_oy = bdx, bdy, box, boy
                a_dx, a_dy, a_ox, a_oy = adx, ady, aox, aoy
        if not ok:
            continue
        cross = b_dx * a_dy - b_dy * a_dx
        if abs(cross) < 1e-6:
            continue
        delta_x = a_ox - b_ox
        delta_y = a_oy - b_oy
        distance = (delta_x * a_dy - delta_y * a_dx) / cross
        cx = b_ox + distance * b_dx
        cy = b_oy + distance * b_dy
        move_x = cx - points[index, 0]
        move_y = cy - points[index, 1]
        if math.sqrt(move_x * move_x + move_y * move_y) > maximum_move:
            continue
        valid[index] = True
        turns[index] = span_turn
        ix[index] = cx
        iy[index] = cy
    return valid, turns, ix, iy


def _geometric_corners(points: np.ndarray, settings: TraceSettings) -> dict[int, Point]:
    """Find corners supported by coherent straight runs at two scales.

    Individual horizontal/vertical turns in a diagonal pixel staircase fail
    the sustained-run tests; a building corner has stable line evidence on
    both sides and survives the larger-scale confirmation. The per-index
    evidence runs in a Numba kernel; the non-max suppression stays here.
    """
    if len(points) < 5:
        return {}
    deviation = max(1e-4, float(settings.corner_straightness))
    minimum = max(1e-4, float(settings.minimum_straight_length))
    span = max(minimum, float(settings.corner_analysis_span))
    threshold = math.radians(float(settings.corner_threshold))
    maximum_move = (float(settings.max_deviation)
                    if settings.max_deviation is not None else deviation * 2.0)
    array = np.ascontiguousarray(points, dtype=np.float64)
    valid, turns, ix, iy = _corner_candidates(
        array, deviation, minimum, span, threshold, maximum_move)
    candidates: list[tuple[float, int, Point]] = [
        (float(turns[index]), index, (float(ix[index]), float(iy[index])))
        for index in range(1, len(points) - 1) if valid[index]]

    # One physical corner creates several neighbouring candidate samples. Keep
    # the strongest and suppress candidates within half the analysis span.
    chosen: dict[int, Point] = {}
    for _, index, intersection in sorted(candidates, reverse=True):
        if all(abs(index - other) >= max(2, int(span / 2)) for other in chosen):
            chosen[index] = intersection
    return chosen


@njit(cache=True, nogil=True)
def _parameters_nb(points: np.ndarray, start: int, end: int, out: np.ndarray) -> None:
    """Fill ``out[0:n]`` with the cumulative-chord parameterisation of ``start..end``.

    Matches ``np.cumsum`` of the per-segment Euclidean lengths normalised by the
    total; an all-coincident run stays at zero, mirroring the numpy version's
    ``if cumulative[-1]`` guard.
    """
    n = end - start + 1
    out[0] = 0.0
    total = 0.0
    for i in range(1, n):
        dx = points[start + i, 0] - points[start + i - 1, 0]
        dy = points[start + i, 1] - points[start + i - 1, 1]
        total += math.sqrt(dx * dx + dy * dy)
        out[i] = total
    if total != 0.0:
        for i in range(n):
            out[i] /= total


@njit(cache=True, nogil=True)
def _cubic_solve_nb(points: np.ndarray, start: int, end: int, tolerance: float,
                    t: np.ndarray, fitted: np.ndarray):
    """Closed-form cubic Bezier controls for samples ``start..end``.

    The two interior controls solve the least-squares fit of the fixed-endpoint
    Bezier basis. For three samples the underdetermined system is replaced by the
    exact quadratic-through-the-sample degree-elevated to a cubic. For four or
    more samples the ``A^T A x = A^T rhs`` 2x2 symmetric normal equations are
    solved per coordinate via the determinant, the closed form of the numpy
    ``lstsq``. Controls are clipped to the padded sample box and ``fitted[0:n]``
    is filled with the curve at the chord parameters. Returns
    ``(c1x, c1y, c2x, c2y)``.
    """
    n = end - start + 1
    _parameters_nb(points, start, end, t)
    p0x = points[start, 0]
    p0y = points[start, 1]
    p3x = points[end, 0]
    p3y = points[end, 1]
    if n == 3:
        middle_t = t[1]
        omt = 1.0 - middle_t
        denominator = 2.0 * omt * middle_t
        if denominator < 1e-12:
            denominator = 1e-12
        qx = (points[start + 1, 0] - omt * omt * p0x - middle_t * middle_t * p3x) / denominator
        qy = (points[start + 1, 1] - omt * omt * p0y - middle_t * middle_t * p3y) / denominator
        c1x = p0x + (2.0 / 3.0) * (qx - p0x)
        c1y = p0y + (2.0 / 3.0) * (qy - p0y)
        c2x = p3x + (2.0 / 3.0) * (qx - p3x)
        c2y = p3y + (2.0 / 3.0) * (qy - p3y)
    else:
        m00 = 0.0
        m01 = 0.0
        m11 = 0.0
        r0x = 0.0
        r0y = 0.0
        r1x = 0.0
        r1y = 0.0
        for i in range(n):
            ti = t[i]
            omt = 1.0 - ti
            b0 = omt * omt * omt
            b1 = 3.0 * omt * omt * ti
            b2 = 3.0 * omt * ti * ti
            b3 = ti * ti * ti
            rhsx = points[start + i, 0] - b0 * p0x - b3 * p3x
            rhsy = points[start + i, 1] - b0 * p0y - b3 * p3y
            m00 += b1 * b1
            m01 += b1 * b2
            m11 += b2 * b2
            r0x += b1 * rhsx
            r0y += b1 * rhsy
            r1x += b2 * rhsx
            r1y += b2 * rhsy
        det = m00 * m11 - m01 * m01
        if abs(det) < 1e-30:
            # Rank-deficient basis (coincident interior parameters): the numpy
            # lstsq minimum-norm answer is unreachable in closed form, so fall
            # back to the endpoints. The fit then fails tolerance and splits.
            c1x = p0x
            c1y = p0y
            c2x = p3x
            c2y = p3y
        else:
            c1x = (r0x * m11 - r1x * m01) / det
            c2x = (m00 * r1x - m01 * r0x) / det
            c1y = (r0y * m11 - r1y * m01) / det
            c2y = (m00 * r1y - m01 * r0y) / det
    minx = p0x
    maxx = p0x
    miny = p0y
    maxy = p0y
    for i in range(n):
        x = points[start + i, 0]
        y = points[start + i, 1]
        if x < minx:
            minx = x
        if x > maxx:
            maxx = x
        if y < miny:
            miny = y
        if y > maxy:
            maxy = y
    ptp_x = maxx - minx
    ptp_y = maxy - miny
    span = ptp_x if ptp_x > ptp_y else ptp_y
    inner = span * 0.25
    if inner > 2.0:
        inner = 2.0
    padding = tolerance if tolerance > inner else inner
    lowx = minx - padding
    upx = maxx + padding
    lowy = miny - padding
    upy = maxy + padding
    if c1x < lowx:
        c1x = lowx
    elif c1x > upx:
        c1x = upx
    if c2x < lowx:
        c2x = lowx
    elif c2x > upx:
        c2x = upx
    if c1y < lowy:
        c1y = lowy
    elif c1y > upy:
        c1y = upy
    if c2y < lowy:
        c2y = lowy
    elif c2y > upy:
        c2y = upy
    for i in range(n):
        ti = t[i]
        omt = 1.0 - ti
        b0 = omt * omt * omt
        b1 = 3.0 * omt * omt * ti
        b2 = 3.0 * omt * ti * ti
        b3 = ti * ti * ti
        fitted[i, 0] = b0 * p0x + b1 * c1x + b2 * c2x + b3 * p3x
        fitted[i, 1] = b0 * p0y + b1 * c1y + b2 * c2y + b3 * p3y
    return c1x, c1y, c2x, c2y


def _parameters(points: np.ndarray) -> np.ndarray:
    array = np.ascontiguousarray(points, dtype=np.float64)
    out = np.empty(len(array), dtype=np.float64)
    _parameters_nb(array, 0, len(array) - 1, out)
    return out


def _cubic(points: np.ndarray, tolerance: float):
    array = np.ascontiguousarray(points, dtype=np.float64)
    n = len(array)
    t = np.empty(n, dtype=np.float64)
    fitted = np.empty((n, 2), dtype=np.float64)
    c1x, c1y, c2x, c2y = _cubic_solve_nb(array, 0, n - 1, tolerance, t, fitted)
    return np.array((c1x, c1y)), np.array((c2x, c2y)), fitted


@njit(cache=True, nogil=True)
def _fit_nb(points: np.ndarray, tolerance: float):
    """Recursive line/cubic fit of a chain, returned as a primitive record array.

    Emits an ``(m, 7)`` array: column 0 is the kind flag (0 = line, 1 = cubic);
    columns 1-4 are the two cubic control points; columns 5-6 the segment
    endpoint (a line uses only the endpoint). An explicit stack replaces the
    numpy recursion; pushing the right sub-range before the left makes the left
    resolve first, so records land in the exact left-to-right order the recursive
    ``_fit(points[:split+1]) + _fit(points[split:])`` produced. The split rule is
    unchanged: a line when the chord error is within tolerance, else a cubic when
    its max error is within tolerance, else split at the argmax cubic error with
    the chord-split then midpoint fallbacks.
    """
    n = points.shape[0]
    records = np.empty((n, 7), dtype=np.float64)
    count = 0
    # A split turns one range into two, so the live-range stack never exceeds n.
    stack = np.empty((n + 1, 2), dtype=np.int64)
    top = 0
    stack[0, 0] = 0
    stack[0, 1] = n - 1
    top = 1
    t = np.empty(n, dtype=np.float64)
    fitted = np.empty((n, 2), dtype=np.float64)
    while top > 0:
        top -= 1
        start = stack[top, 0]
        end = stack[top, 1]
        m = end - start + 1
        if m <= 2:
            records[count, 0] = 0.0
            records[count, 5] = points[end, 0]
            records[count, 6] = points[end, 1]
            count += 1
            continue
        line_error, line_split = _distance_to_line_nb(points, start, end)
        if line_error <= tolerance:
            records[count, 0] = 0.0
            records[count, 5] = points[end, 0]
            records[count, 6] = points[end, 1]
            count += 1
            continue
        c1x, c1y, c2x, c2y = _cubic_solve_nb(
            points, start, end, tolerance, t[:m], fitted[:m])
        max_error = -1.0
        split = 0
        for i in range(m):
            dx = fitted[i, 0] - points[start + i, 0]
            dy = fitted[i, 1] - points[start + i, 1]
            error = math.sqrt(dx * dx + dy * dy)
            if error > max_error:
                max_error = error
                split = i
        if max_error <= tolerance:
            records[count, 0] = 1.0
            records[count, 1] = c1x
            records[count, 2] = c1y
            records[count, 3] = c2x
            records[count, 4] = c2y
            records[count, 5] = points[end, 0]
            records[count, 6] = points[end, 1]
            count += 1
            continue
        if not (0 < split < m - 1):
            split = line_split
        if not (0 < split < m - 1):
            split = m // 2
        # Push right then left so the left range resolves first: left-to-right.
        stack[top, 0] = start + split
        stack[top, 1] = end
        top += 1
        stack[top, 0] = start
        stack[top, 1] = start + split
        top += 1
    return records[:count]


def _fit(points: np.ndarray, tolerance: float) -> list[Primitive]:
    array = np.ascontiguousarray(points, dtype=np.float64)
    if len(array) <= 2:
        return [("L", (float(array[-1, 0]), float(array[-1, 1])))]
    records = _fit_nb(array, tolerance)
    primitives: list[Primitive] = []
    for row in records:
        if row[0] == 0.0:
            primitives.append(("L", (float(row[5]), float(row[6]))))
        else:
            primitives.append(("C", (float(row[1]), float(row[2])),
                               (float(row[3]), float(row[4])),
                               (float(row[5]), float(row[6]))))
    return primitives


def _tightened(value: float, divisor: float) -> float:
    """Divide a recovery tolerance, floored at 0.05 but never above ``value``."""
    return min(float(value), max(0.05, float(value) / divisor))


def _fit_classified(points: np.ndarray, settings: TraceSettings,
                    source_start: int, source_end: int,
                    ) -> tuple[list[Primitive], RunDiagnostic]:
    """Choose the simplest valid model for a classified geometric run."""
    if len(points) <= 2:
        primitives = [("L", tuple(points[-1]))]
        return primitives, RunDiagnostic("straight", source_start, source_end, 0.0, 1)
    line = _robust_line(points)
    if (line.length >= float(settings.minimum_straight_length) and
            line.maximum_error <= float(settings.straight_line_max_deviation)):
        primitives = [("L", tuple(points[-1]))]
        return primitives, RunDiagnostic("straight", source_start, source_end,
                                         line.maximum_error, 1)
    tolerance = max(1e-4, float(settings.curve_fit_tolerance))
    if settings.max_deviation is not None:
        tolerance = min(tolerance, max(1e-4, float(settings.max_deviation)))
    primitives = _fit(points, tolerance)
    # The recursive fitter guarantees this bound at source parameters; retain
    # the observed line residual as useful classification evidence too.
    return primitives, RunDiagnostic("curve", source_start, source_end,
                                     min(line.maximum_error, tolerance), len(primitives))


def _compute_chain_fit(canonical: tuple, anchors: tuple, constraint_key: tuple,
                       settings: TraceSettings) -> ChainFit:
    """Fit one canonical chain from its samples, anchors, and constraints."""
    anchor_points = {anchor.point for anchor in anchors}
    split_points = anchor_points | set(constraint_key)
    array = np.asarray(canonical, dtype=float)
    corners = _geometric_corners(array, settings)
    split_indices = [0, *[index for index, point in enumerate(canonical[1:-1], 1)
                          if point in split_points or index in corners], len(canonical) - 1]
    split_indices = sorted(set(split_indices))
    model_points = array.copy()
    for index, point in corners.items():
        if canonical[index] not in anchor_points:
            model_points[index] = point
    pieces: list[Primitive] = []
    diagnostics: list[RunDiagnostic] = []
    for index in sorted(corners):
        if canonical[index] in anchor_points:
            continue
        delta = model_points[index] - array[index]
        diagnostics.append(RunDiagnostic(
            "corner", index, index,
            math.sqrt(delta[0] * delta[0] + delta[1] * delta[1]), 0))
    for start, end in zip(split_indices, split_indices[1:]):
        section = model_points[start:end + 1]
        fitted_piece, diagnostic = _fit_classified(section, settings, start, end)
        pieces.extend(fitted_piece)
        diagnostics.append(diagnostic)
    primitives = tuple(pieces)
    lines = sum(p[0] == "L" for p in primitives)
    cubics = len(primitives) - lines
    used = len(anchor_points.intersection(canonical))
    return ChainFit(canonical[0], primitives, FitMetrics(
        lines, cubics, used, len(canonical), len(primitives)), tuple(diagnostics))


def _fit_cache_misses(fitter: "SharedChainFitter", plans: list[tuple],
                      tick: Progress = _noop) -> None:
    """Fit every plan whose key is absent from the fitter cache, serially.

    Each plan is ``(key, canonical, anchors, constraint_key, settings)`` as
    produced by ``SharedChainFitter.prepare``. Distinct keys are fitted once and
    land in the cache under their key. The per-chain fit is now compiled (corner
    analysis, the cubic solve, and the recursive fitter are all Numba kernels), so
    even the cold first pass over the whole image is a fraction of a second; the
    former process pool's fork/pickle/shutdown cost exceeded that, and threads
    thrash on the GIL the corner kernel still holds, so a serial loop is both the
    fastest measured option here and trivially deterministic.
    """
    unique_pending: dict[tuple, tuple] = {}
    for key, canonical, anchor_tuple, constraint_key, chain_settings in plans:
        if key not in fitter._cache:
            unique_pending.setdefault(
                key, (canonical, anchor_tuple, constraint_key, chain_settings))
    if not unique_pending:
        return
    total = len(unique_pending)
    for done, (key, argument) in enumerate(unique_pending.items(), 1):
        fitter._cache[key] = _compute_chain_fit(*argument)
        if done % 100 == 0 or done == total:
            tick(done / total, f"Fitting boundary curves ({done} of {total})")


class SharedChainFitter:
    """Canonical cache ensures both users of a shared chain get one fit."""

    def __init__(self) -> None:
        self._cache: dict[tuple, ChainFit] = {}

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def prepare(self, coords: Iterable[Point], settings: TraceSettings,
                hard_anchors: Iterable[HardAnchor] = (),
                fit_constraints: Iterable[Point] = ()) -> tuple:
        """Canonicalize a chain and build its cache key without fitting it.

        Returns ``(key, forward, canonical, anchors, constraint_key)``; the four
        latter values are exactly the inputs ``_compute_chain_fit`` needs.
        """
        points = tuple(tuple(map(float, p)) for p in coords)
        if len(points) < 2:
            raise ValueError("a shared chain needs at least two samples")
        anchors = tuple(hard_anchors)
        constraints = tuple(tuple(map(float, point)) for point in fit_constraints)
        anchor_key = tuple(sorted((a.point, a.kind) for a in anchors
                                  if a.point in points))
        forward = points <= tuple(reversed(points))
        canonical = points if forward else tuple(reversed(points))
        constraint_key = tuple(sorted(point for point in constraints if point in canonical))
        key = (canonical, anchor_key, constraint_key,
               float(settings.curve_fit_tolerance),
               float(settings.straight_line_max_deviation),
               float(settings.minimum_straight_length),
               float(settings.corner_analysis_span), float(settings.corner_threshold),
               float(settings.corner_straightness), settings.max_deviation)
        return key, forward, canonical, anchors, constraint_key

    def fit(self, coords: Iterable[Point], settings: TraceSettings,
            hard_anchors: Iterable[HardAnchor] = (),
            fit_constraints: Iterable[Point] = ()) -> ChainFit:
        key, forward, canonical, anchors, constraint_key = self.prepare(
            coords, settings, hard_anchors, fit_constraints)
        if key not in self._cache:
            self._cache[key] = _compute_chain_fit(
                canonical, anchors, constraint_key, settings)
        fitted = self._cache[key]
        return fitted if forward else fitted.reversed()


def _fmt(point: Point) -> str:
    return f"{point[0]:g} {point[1]:g}"


def ring_path(coords: Iterable[Point], settings: TraceSettings,
              hard_anchors: Iterable[HardAnchor] = (),
              fitter: SharedChainFitter | None = None) -> RingFit:
    raw = [tuple(map(float, p)) for p in coords]
    points = raw[:-1] if raw and raw[0] == raw[-1] else raw
    anchors = tuple(hard_anchors)
    anchor_points = frozenset(a.point for a in anchors)
    fitter = fitter or SharedChainFitter()
    # Closed rings use the same modeller as shared open chains. The duplicated
    # seam is only a topological endpoint; it is not a protected or inferred
    # geometry anchor.
    fitted = fitter.fit([*points, points[0]], settings, anchors)
    commands = [f"M{_fmt(fitted.start)}"]
    primitives = list(fitted.primitives)
    for primitive in primitives:
        if primitive[0] == "L": commands.append(f"L{_fmt(primitive[1])}")
        else: commands.append(f"C{_fmt(primitive[1])} {_fmt(primitive[2])} {_fmt(primitive[3])}")
    commands.append("Z")
    lines = sum(p[0] == "L" for p in primitives)
    metrics = FitMetrics(lines, len(primitives)-lines,
                         len(anchor_points.intersection(points)), len(points), len(primitives))
    return RingFit(" ".join(commands), metrics)


def geometry_components(geometry, settings: TraceSettings,
                        hard_anchors: Iterable[HardAnchor] = (),
                        fitter: SharedChainFitter | None = None) -> list[dict]:
    polygons = [] if geometry.is_empty else ([geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms))
    polygons.sort(key=lambda polygon: (-polygon.area, polygon.bounds))
    fitter = fitter or SharedChainFitter()
    anchors = tuple(hard_anchors)
    plans = []
    for polygon in polygons:
        for ring in (polygon.exterior, *polygon.interiors):
            raw = [tuple(map(float, point)) for point in ring.coords]
            points = raw[:-1] if raw and raw[0] == raw[-1] else raw
            key, _, canonical, anchor_tuple, constraint_key = fitter.prepare(
                [*points, points[0]], settings, anchors)
            plans.append((key, canonical, anchor_tuple, constraint_key, settings))
    _fit_cache_misses(fitter, plans)
    result = []
    for polygon in polygons:
        fitted = [ring_path(r.coords, settings, hard_anchors, fitter)
                  for r in [polygon.exterior, *polygon.interiors]]
        aggregate = FitMetrics(
            sum(f.metrics.lines for f in fitted), sum(f.metrics.cubics for f in fitted),
            sum(f.metrics.hard_anchors for f in fitted), sum(f.metrics.source_samples for f in fitted),
            sum(f.metrics.final_nodes for f in fitted))
        result.append({"d": " ".join(f.path for f in fitted), "nodes": aggregate.final_nodes,
                       "holes": len(polygon.interiors), "fit_metrics": aggregate.as_dict(),
                       "area": float(polygon.area)})
    return result


def _ring_row(chains: dict, fits: dict[str, "ChainFit"], ring_uses) -> dict:
    """Assemble one closed ring of oriented chain uses into a path row.

    Each use contributes the canonical chain's once-fitted primitives, reversed when
    the use runs backwards, so a boundary shared by two rings is byte-identical in both.
    """
    commands: list[str] = []
    raw_points: list[Point] = []
    metrics: list[FitMetrics] = []
    for use in ring_uses:
        chain = chains[use.identity]
        fit = fits[use.identity] if use.forward else fits[use.identity].reversed()
        oriented = chain.points if use.forward else tuple(reversed(chain.points))
        if not commands:
            commands.append(f"M{_fmt(fit.start)}")
            raw_points.extend(oriented)
        else:
            if raw_points[-1] != oriented[0]:
                raise RuntimeError(
                    f"assembled chain {use.identity} does not join its predecessor")
            raw_points.extend(oriented[1:])
        for primitive in fit.primitives:
            if primitive[0] == "L":
                commands.append(f"L{_fmt(primitive[1])}")
            else:
                commands.append(
                    f"C{_fmt(primitive[1])} {_fmt(primitive[2])} {_fmt(primitive[3])}")
        metrics.append(fit.metrics)
    commands.append("Z")
    if raw_points[0] != raw_points[-1]:
        raw_points.append(raw_points[0])
    polygon = Polygon(np.asarray(raw_points, dtype=np.float64))
    if polygon.is_empty or polygon.area <= 0:
        raise RuntimeError("assembled boundary ring is empty")
    aggregate = FitMetrics(
        sum(item.lines for item in metrics), sum(item.cubics for item in metrics),
        sum(item.hard_anchors for item in metrics), sum(item.source_samples for item in metrics),
        sum(item.final_nodes for item in metrics))
    return {"d": " ".join(commands), "polygon": polygon, "fit_metrics": aggregate}


def _rows_to_components(rows: list[dict]) -> list[dict]:
    """Nest rings by even-odd containment into filled components with their holes.

    A ring's parent is the smallest strictly larger ring containing its
    representative point (lowest index among equal areas). Only rings whose
    bounding box covers the point can contain it, so an STRtree narrows the
    candidates before the exact containment test.
    """
    polygons = [row["polygon"] for row in rows]
    areas = [polygon.area for polygon in polygons]
    containing: list[list[int]] = [[] for _ in polygons]
    if polygons:
        # Querying the point tree with each ring prepares that ring once and
        # tests only the representative points inside its bounding box.
        points = [polygon.representative_point() for polygon in polygons]
        ring_indices, point_indices = STRtree(points).query(polygons, predicate="contains")
        for point_index, ring_index in zip(point_indices.tolist(), ring_indices.tolist()):
            containing[point_index].append(ring_index)
    parents: list[int | None] = []
    for index in range(len(polygons)):
        containers = [candidate for candidate in containing[index]
                      if candidate != index and areas[candidate] > areas[index]]
        parents.append(min(containers, key=lambda candidate: (areas[candidate], candidate))
                       if containers else None)

    def depth(index: int) -> int:
        value, seen = 0, set()
        while parents[index] is not None:
            if index in seen:
                raise RuntimeError("cyclic ring containment")
            seen.add(index)
            index = int(parents[index])
            value += 1
        return value

    components = []
    for index, row in enumerate(rows):
        if depth(index) % 2:
            continue
        holes = [candidate for candidate, parent in enumerate(parents)
                 if parent == index and depth(candidate) % 2]
        members = [row, *(rows[candidate] for candidate in holes)]
        aggregate = FitMetrics(
            sum(item["fit_metrics"].lines for item in members),
            sum(item["fit_metrics"].cubics for item in members),
            sum(item["fit_metrics"].hard_anchors for item in members),
            sum(item["fit_metrics"].source_samples for item in members),
            sum(item["fit_metrics"].final_nodes for item in members),
        )
        components.append({"d": " ".join(item["d"] for item in members),
                           "nodes": aggregate.final_nodes,
                           "holes": len(holes),
                           "fit_metrics": aggregate.as_dict(),
                           "area": float(row["polygon"].area)})
    components.sort(key=lambda item: -item["area"])
    return components


def _ring_is_ccw(points) -> bool:
    return sum((b[0] - a[0]) * (b[1] + a[1]) for a, b in zip(points, points[1:])) < 0


def _fit_vertices(fit: "ChainFit") -> list:
    return [fit.start, *(primitive[-1] for primitive in fit.primitives)]


def _region_ring_path(chains: dict, fits: dict[str, "ChainFit"],
                      segment_owner: dict, ring) -> dict:
    """Emit one union boundary ring, reusing each chain's once-fitted curve.

    The ring already comes from the geometry union, so its vertices are the canonical
    chain samples. Each maximal run of edges owned by one chain replays that chain's
    cached fit (reversed when the run runs backwards); the rare edge that the union
    renoded away from any chain falls back to its own straight segment, whose endpoints
    are shared by every neighbour, so registration holds either way.
    """
    coords = list(ring.coords)
    edges = list(zip(coords, coords[1:]))
    owners = [segment_owner.get(frozenset((a, b))) for a, b in edges]
    runs: list[list] = []
    for (a, b), owner in zip(edges, owners):
        if runs and runs[-1][0] == owner and owner is not None:
            runs[-1][1].append(b)
        else:
            runs.append([owner, [a, b]])
    # A chain straddling the ring's arbitrary start point splits across the seam; rejoin
    # it so each chain is one run and its run starts on a real junction.
    if len(runs) > 1 and runs[0][0] is not None and runs[0][0] == runs[-1][0]:
        tail = runs.pop()
        runs[0][1] = tail[1] + runs[0][1][1:]
    commands: list[str] = []
    lines = cubics = 0
    for owner, points in runs:
        chain = chains.get(owner) if owner is not None else None
        oriented_fit = None
        if chain is not None:
            if chain.closed:
                # A closed interface is a whole ring on its own; the union ring starts at an
                # arbitrary vertex, so orient by winding rather than a shared endpoint.
                forward = fits[owner]
                oriented_fit = forward if _ring_is_ccw(_fit_vertices(forward)) == _ring_is_ccw(points) \
                    else forward.reversed()
            elif points[0] == chain.points[0]:
                oriented_fit = fits[owner]
            elif points[0] == chain.points[-1]:
                oriented_fit = fits[owner].reversed()
        if oriented_fit is not None:
            if not commands:
                commands.append(f"M{_fmt(oriented_fit.start)}")
            for primitive in oriented_fit.primitives:
                if primitive[0] == "L":
                    commands.append(f"L{_fmt(primitive[1])}")
                    lines += 1
                else:
                    commands.append(
                        f"C{_fmt(primitive[1])} {_fmt(primitive[2])} {_fmt(primitive[3])}")
                    cubics += 1
        else:
            if not commands:
                commands.append(f"M{_fmt(points[0])}")
            for point in points[1:]:
                commands.append(f"L{_fmt(point)}")
                lines += 1
    commands.append("Z")
    return {"d": " ".join(commands), "lines": lines, "cubics": cubics}


def region_components(track, fits: dict[str, "ChainFit"], geometry) -> list[dict]:
    """Trace the outline of a planar region from the canonical chains it is built on.

    Reuses each interface's once-fitted curve (recovery included), dissolving only the
    interior interfaces the region swallows, so a solid underlayer registers exactly
    against every neighbour instead of being re-optimised into gaps. The region's own
    boundary rings drive the walk, so no separate topology traversal can fail to close.
    """
    chains = {chain.identity: chain for chain in track.chains}
    segment_owner: dict = {}
    for chain in track.chains:
        points = chain.points
        for index in range(len(points) - 1):
            segment_owner[frozenset((points[index], points[index + 1]))] = chain.identity
    polygons = [] if geometry.is_empty else (
        [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms))
    components = []
    for polygon in polygons:
        rings = [_region_ring_path(chains, fits, segment_owner, polygon.exterior),
                 *(_region_ring_path(chains, fits, segment_owner, hole)
                   for hole in polygon.interiors)]
        lines = sum(ring["lines"] for ring in rings)
        cubics = sum(ring["cubics"] for ring in rings)
        components.append({"d": " ".join(ring["d"] for ring in rings),
                           "nodes": lines + cubics,
                           "holes": len(polygon.interiors),
                           "fit_metrics": FitMetrics(lines, cubics, 0, 0, lines + cubics).as_dict(),
                           "area": float(polygon.area)})
    components.sort(key=lambda item: -item["area"])
    return components


def shared_boundary_components(track, included: list[int], settings: TraceSettings,
                               recovery: dict[str, dict[str, object]] | None = None,
                               progress: Progress | None = None,
                               fitter: "SharedChainFitter | None" = None,
                               ) -> tuple[dict[int, list[dict]], dict[str, "ChainFit"], dict[str, object]]:
    """Fit canonical interfaces once, then assemble exact reverse uses into paths.

    A caller may supply a persistent ``fitter`` so its cache survives across the
    recovery loop's repeated passes: a chain whose recovery state is unchanged
    keys identically and returns from cache instead of re-fitting.
    """
    from vinylgen.boundary_graph import assemble_colour_rings

    fitter = fitter or SharedChainFitter()
    chains = {chain.identity: chain for chain in track.chains}
    fits: dict[str, ChainFit] = {}
    recovery = recovery or {}
    tick = progress or _noop
    total = len(track.chains)
    # Phase 1: resolve each chain's recovery-adjusted settings and cache key.
    # This is cheap; the expensive curve fit is deferred to phase 2.
    plans: list[tuple] = []
    for index, chain in enumerate(track.chains):
        tick(index / total if total else 1.0,
             f"Fitting boundary curves ({index + 1} of {total})")
        anchors = [HardAnchor(anchor.point, anchor.kind) for anchor in chain.anchors]
        chain_settings = settings
        samples = chain.points
        state = recovery.get(chain.identity, {})
        stage = int(state.get("stage", 0))
        constraint = state.get("constraint")
        constraints = [tuple(constraint)] if constraint is not None and stage >= 3 else []
        # Each recovery stage only tightens: the 0.05 floor never raises a
        # configured value that is already below it (a straight-line deviation
        # of 0 stays 0). Stage 2 halves corner straightness together with the
        # straight-line deviation, so fewer runs confirm a hard corner.
        if stage >= 1:
            chain_settings = replace(
                chain_settings,
                curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 2.0))
        if stage >= 2:
            chain_settings = replace(
                chain_settings,
                curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 4.0),
                straight_line_max_deviation=_tightened(
                    settings.straight_line_max_deviation, 2.0),
                corner_straightness=_tightened(settings.corner_straightness, 2.0))
        if stage >= 4:
            primitives = tuple(("L", point) for point in samples[1:])
            fits[chain.identity] = ChainFit(
                samples[0], primitives,
                FitMetrics(len(primitives), 0, len(anchors), len(samples), len(primitives)),
                (RunDiagnostic(
                    "curve", 0, len(samples) - 1, 0.0, len(primitives),
                    recovery_attempts=4, dense_fallback=True,
                    fallback_reason=(
                        "maximum deviation still exceeded after ordered local recovery")),))
            continue
        key, forward, canonical, anchor_tuple, constraint_key = fitter.prepare(
            samples, chain_settings, anchors, constraints)
        plans.append((chain.identity, stage, forward, key,
                      canonical, anchor_tuple, constraint_key, chain_settings))

    # Phase 2: compute the cache misses. Distinct keys are fitted once by the
    # compiled per-chain fitter; the cold first pass over the whole image is
    # sub-second, so this is a serial loop.
    _fit_cache_misses(
        fitter, [(key, canonical, anchor_tuple, constraint_key, chain_settings)
                 for _, _, _, key, canonical, anchor_tuple, constraint_key, chain_settings
                 in plans], tick)

    # Phase 3: assemble oriented fits from the now-populated cache.
    for identity, stage, forward, key, *_ in plans:
        fitted = fitter._cache[key]
        if not forward:
            fitted = fitted.reversed()
        if stage:
            fitted = replace(fitted, diagnostics=tuple(
                replace(item, recovery_attempts=stage) for item in fitted.diagnostics))
        fits[identity] = fitted

    assembled = assemble_colour_rings(track, included)
    result: dict[int, list[dict]] = {}
    for colour, rings in assembled.items():
        result[colour] = _rows_to_components(
            [_ring_row(chains, fits, ring.uses) for ring in rings])

    unique = FitMetrics(
        sum(fit.metrics.lines for fit in fits.values()),
        sum(fit.metrics.cubics for fit in fits.values()),
        sum(fit.metrics.hard_anchors for fit in fits.values()),
        sum(fit.metrics.source_samples for fit in fits.values()),
        sum(fit.metrics.final_nodes for fit in fits.values()),
    )
    return result, fits, {"unique_chains": len(fits),
                    "recovery_stages": [
                        "tighten_curve", "reclassify_geometry",
                        "add_local_constraint", "dense_fallback"],
                    "recovered_chains": len(recovery),
                    "dense_fallback_chains": sum(
                        int(state.get("stage", 0)) >= 4 for state in recovery.values()),
                    "chain_recovery": {
                        identity: {"stage": int(state.get("stage", 0)),
                                   "constraint": state.get("constraint")}
                        for identity, state in sorted(recovery.items())},
                    "chain_diagnostics": {
                        identity: [item.as_dict() for item in fit.diagnostics]
                        for identity, fit in sorted(fits.items())
                    },
                    "fit_metrics": unique.as_dict()}
