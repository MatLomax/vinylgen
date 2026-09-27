from __future__ import annotations

import bisect
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from typing import Callable, Iterable, Literal

import numpy as np
from numba import njit
import shapely
from shapely.geometry import Polygon
from shapely.strtree import STRtree

from vinylgen.contracts import AUTOMATIC_LENGTHS, LENGTH_UNIT_PX, TraceSettings
from vinylgen.palette import Progress, _noop
from vinylgen.path_crossings import crossings

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
    # Where the confirmed corners' cusp nodes ended up (after any straight-line
    # snap moved them), in canonical order.
    corners: tuple[Point, ...] = ()

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
                        tuple(reversed(self.diagnostics)), self.corners)


@dataclass(frozen=True)
class RingFit:
    path: str
    metrics: FitMetrics


def _require_pixels(settings: TraceSettings) -> None:
    """The fitter works in pixels: settings in thousandths of the image must be
    resolved first (``vinylgen.trace_lengths.resolve_trace_lengths``)."""
    if settings.length_unit != LENGTH_UNIT_PX:
        raise ValueError("trace lengths must be resolved to pixels before fitting "
                         f"(length_unit is {settings.length_unit!r})")


@njit(cache=True, nogil=True)
def _segment_distance_nb(points: np.ndarray, start: int, end: int,
                         ax: float, ay: float, bx: float, by: float):
    """Max distance of samples ``start..end`` from the segment ``a``-``b``.

    A sample beyond either end of the segment is measured to that end, so a
    line through the segment never counts as covering it. Returns
    ``(max_distance, local_index)`` where ``local_index`` is the offset from
    ``start`` of the farthest sample. Ties keep the first sample, matching
    ``np.argmax``.
    """
    n = end - start + 1
    vx = bx - ax
    vy = by - ay
    length = math.sqrt(vx * vx + vy * vy)
    best = -1.0
    best_index = 0
    if length < 1e-12:
        for i in range(n):
            dx = points[start + i, 0] - ax
            dy = points[start + i, 1] - ay
            distance = math.sqrt(dx * dx + dy * dy)
            if distance > best:
                best = distance
                best_index = i
    else:
        for i in range(n):
            dx = points[start + i, 0] - ax
            dy = points[start + i, 1] - ay
            along = (vx * dx + vy * dy) / length
            if along < 0.0:
                distance = math.sqrt(dx * dx + dy * dy)
            elif along > length:
                ex = points[start + i, 0] - bx
                ey = points[start + i, 1] - by
                distance = math.sqrt(ex * ex + ey * ey)
            else:
                distance = abs(vx * dy - vy * dx) / length
            if distance > best:
                best = distance
                best_index = i
    return best, best_index


@njit(cache=True, nogil=True)
def _distance_to_line_nb(points: np.ndarray, start: int, end: int):
    """Max distance of samples ``start..end`` from their chord (the segment).

    ``_segment_distance_nb`` from the first sample to the last: a run folding
    back on itself is measured to the chord's ends. Callable from the compiled
    fitter and from Python.
    """
    return _segment_distance_nb(points, start, end, points[start, 0], points[start, 1],
                                points[end, 0], points[end, 1])


def _distance_to_line(points: np.ndarray) -> tuple[float, int]:
    array = np.ascontiguousarray(points, dtype=np.float64)
    distance, index = _distance_to_line_nb(array, 0, len(array) - 1)
    return float(distance), int(index)


def _segment_distance(samples: np.ndarray, a, b) -> float:
    """Max distance of ``samples`` from the segment ``a``-``b`` (0 for no samples)."""
    if len(samples) == 0:
        return 0.0
    array = np.ascontiguousarray(samples, dtype=np.float64)
    distance, _ = _segment_distance_nb(array, 0, len(array) - 1, float(a[0]), float(a[1]),
                                       float(b[0]), float(b[1]))
    return float(distance)


@njit(cache=True, nogil=True)
def _side_line(points: np.ndarray, index: int, step: int, span: float, stops: np.ndarray):
    """Closed-form line evidence for one arc-length side window of a corner.

    Walks from ``index`` in the ``step`` direction accumulating physical arc
    length up to ``span``, stopping early at the chain end or at the first
    sample marked in ``stops`` (a neighbouring corner; that sample is the
    window's last), then computes its total-least-squares line in closed form
    (the principal eigenvector of the centred 2x2 covariance): origin,
    principal direction, maximum perpendicular error, and projection length.
    Returns ``(dir_x, dir_y, maximum_error, length, origin_x, origin_y,
    travelled, stopped)``: ``travelled`` is the window's arc length and
    ``stopped`` the index of the sample that cut it short (-1 when none did).
    ``_provisional_corners`` uses it.
    """
    n = points.shape[0]
    # The window is the contiguous run of samples from ``near`` to ``cursor``.
    near = index
    travelled = 0.0
    stopped = -1
    cursor = index
    while 0 <= cursor + step < n and travelled < span:
        following = cursor + step
        dx = points[following, 0] - points[cursor, 0]
        dy = points[following, 1] - points[cursor, 1]
        travelled += math.sqrt(dx * dx + dy * dy)
        cursor = following
        if stops[following] != 0:
            stopped = following
            break
    count = abs(cursor - near) + 1
    origin_x = points[index, 0]
    origin_y = points[index, 1]
    if count < 2:
        return 1.0, 0.0, 0.0, 0.0, origin_x, origin_y, travelled, stopped
    sum_x = 0.0
    sum_y = 0.0
    for i in range(count):
        p = near + i * step
        sum_x += points[p, 0]
        sum_y += points[p, 1]
    origin_x = sum_x / count
    origin_y = sum_y / count
    a = 0.0
    b = 0.0
    c = 0.0
    any_nonzero = False
    for i in range(count):
        p = near + i * step
        cx = points[p, 0] - origin_x
        cy = points[p, 1] - origin_y
        if cx != 0.0 or cy != 0.0:
            any_nonzero = True
        a += cx * cx
        b += cx * cy
        c += cy * cy
    if not any_nonzero:
        return 1.0, 0.0, 0.0, 0.0, origin_x, origin_y, travelled, stopped
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
    # Orient forward along the chain: a backward window points from its far
    # sample towards ``index``, a forward one away from it.
    if step < 0:
        sign_x = points[near, 0] - points[cursor, 0]
        sign_y = points[near, 1] - points[cursor, 1]
    else:
        sign_x = points[cursor, 0] - points[near, 0]
        sign_y = points[cursor, 1] - points[near, 1]
    if dir_x * sign_x + dir_y * sign_y < 0.0:
        dir_x = -dir_x
        dir_y = -dir_y
    proj_min = math.inf
    proj_max = -math.inf
    error_max = 0.0
    for i in range(count):
        p = near + i * step
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
    return (dir_x, dir_y, error_max, proj_max - proj_min, origin_x, origin_y,
            travelled, stopped)


# The tip skip, in corner sharpening allowances of arc on each side of a
# candidate corner. A 90-degree round of radius r lies r(sqrt 2 - 1) from its
# sharp point, so the allowance sharpens rounds up to (1 + sqrt 2), about 2.41,
# allowances in radius; such a round reaches r * pi / 4 (1.9 allowances) of arc
# from its midpoint, so windows that start this far out see its straight sides.
_TIP_SKIP_ALLOWANCES = 1.0 + math.sqrt(2.0)
# A side window's arc is treated as a straight line when its fitted radius
# exceeds this many window lengths (the circle is then numerically a line).
_LINE_RADIUS_WINDOWS = 1.0e4
# A side is an arc only when the arc fits at least this much closer than the
# side's straight line (max distance). A pixel staircase's zigzag is about as far
# from any arc as from its line, so a curvature fitted to it is noise, which the
# arc would then extrapolate across the tip.
_ARC_IMPROVEMENT = 0.8
# An arc has three parameters: through three or four samples it follows their
# noise, so a shorter window is fitted with its line.
_ARC_MIN_SAMPLES = 5
# One arc through both sides and the tip must miss by more than this many times
# the worse side's own error plus this many px (raster noise) for a corner.
_ONE_ARC_RATIO = 1.5
_ONE_ARC_FLOOR = 0.3
# A side's arc turns by at most this much (radians) over its window: one that
# turns further runs round another feature (the floor of a narrow slot), so the
# side is judged on its nearest minimum length instead (``_side_nb``).
_SIDE_SWEEP = math.pi / 2


@njit(cache=True, nogil=True)
def _side_window_nb(points: np.ndarray, index: int, step: int, skip: float, length: float,
                    minimum: float, stops: np.ndarray):
    """One side window of a candidate corner, walked by arc length.

    Walks from ``index`` in the ``step`` direction up to ``skip + length`` of arc,
    stopping early at the chain end or at the first sample marked in ``stops`` (a
    neighbouring corner; that sample is the window's last). The window is the
    samples from ``skip`` of arc (the tip skip) to where the walk ended. A walk
    cut short keeps at least ``minimum`` of arc when it has that much: the
    window then starts ``minimum`` before its end, nearer than ``skip``, so a
    short edge between two corners is judged whole.

    Returns ``(near, far, window_length, travelled, stopped)``: the window's
    sample indices nearest and furthest from ``index``, its arc length, the arc
    walked, and the sample that cut the walk short (-1 when none did).
    """
    n = points.shape[0]
    target = skip + length
    travelled = 0.0
    stopped = -1
    cursor = index
    while 0 <= cursor + step < n and travelled < target:
        following = cursor + step
        dx = points[following, 0] - points[cursor, 0]
        dy = points[following, 1] - points[cursor, 1]
        travelled += math.sqrt(dx * dx + dy * dy)
        cursor = following
        # A neighbouring corner closer than ``minimum`` is the same corner (an
        # edge that short between two corners rounds off), not a stop.
        if stops[following] != 0 and travelled >= minimum:
            stopped = following
            break
    far = cursor
    start = skip
    if travelled < skip + minimum:
        start = travelled - minimum
        if start < 0.0:
            start = 0.0
    # The window starts at the last sample no further than ``start``, so a cut
    # window keeps its ``minimum``.
    walked = 0.0
    near = index
    while near != far:
        following = near + step
        dx = points[following, 0] - points[near, 0]
        dy = points[following, 1] - points[near, 1]
        advance = math.sqrt(dx * dx + dy * dy)
        if walked + advance > start:
            break
        walked += advance
        near = following
    return near, far, travelled - walked, travelled, stopped


@njit(cache=True, nogil=True)
def _fit_side_nb(points: np.ndarray, near: int, far: int, step: int, window: float,
                 improvement: float = _ARC_IMPROVEMENT):
    """A circular arc (a straight line included) fitted to one side window.

    The circle is Taubin's algebraic fit (closed form, a Newton solve of its
    characteristic cubic); it becomes the total-least-squares line when the
    samples are collinear or its radius exceeds ``_LINE_RADIUS_WINDOWS`` window
    lengths. Samples run from ``near`` to ``far`` in ``step`` steps; the arc is
    oriented forward along the chain (from the chain-earlier sample onwards).

    Returns ``(is_line, ox, oy, dx, dy, cx, cy, radius, sign, error)``: a line's
    point and forward unit direction, a circle's centre, radius and turning
    sign (+1 anticlockwise), and the samples' largest distance from the fit.
    """
    count = abs(far - near) + 1
    mx = 0.0
    my = 0.0
    for i in range(count):
        p = near + i * step
        mx += points[p, 0]
        my += points[p, 1]
    mx /= count
    my /= count
    mxx = 0.0
    myy = 0.0
    mxy = 0.0
    mxz = 0.0
    myz = 0.0
    mzz = 0.0
    for i in range(count):
        p = near + i * step
        xi = points[p, 0] - mx
        yi = points[p, 1] - my
        zi = xi * xi + yi * yi
        mxx += xi * xi
        myy += yi * yi
        mxy += xi * yi
        mxz += xi * zi
        myz += yi * zi
        mzz += zi * zi
    mxx /= count
    myy /= count
    mxy /= count
    mxz /= count
    myz /= count
    mzz /= count
    # The chain-earlier and chain-later ends orient the fit.
    if step < 0:
        first = far
        last = near
    else:
        first = near
        last = far
    half_diff = (mxx - myy) * 0.5
    root = math.sqrt(half_diff * half_diff + mxy * mxy)
    if mxy == 0.0:
        if mxx >= myy:
            dir_x = 1.0
            dir_y = 0.0
        else:
            dir_x = 0.0
            dir_y = 1.0
    else:
        eigen_x = half_diff + root
        norm = math.sqrt(eigen_x * eigen_x + mxy * mxy)
        dir_x = eigen_x / norm
        dir_y = mxy / norm
    if (dir_x * (points[last, 0] - points[first, 0])
            + dir_y * (points[last, 1] - points[first, 1])) < 0.0:
        dir_x = -dir_x
        dir_y = -dir_y
    is_line = True
    cx = 0.0
    cy = 0.0
    radius = 0.0
    sign = 1.0
    if count >= _ARC_MIN_SAMPLES:
        mz = mxx + myy
        cov_xy = mxx * myy - mxy * mxy
        var_z = mzz - mz * mz
        a3 = 4.0 * mz
        a2 = -3.0 * mz * mz - mzz
        a1 = var_z * mz + 4.0 * cov_xy * mz - mxz * mxz - myz * myz
        a0 = (mxz * (mxz * myy - myz * mxy) + myz * (myz * mxx - mxz * mxy)
              - var_z * cov_xy)
        x = 0.0
        y = a0
        for _ in range(32):
            slope = a1 + x * (2.0 * a2 + 3.0 * a3 * x)
            if slope == 0.0:
                break
            x_new = x - y / slope
            if x_new == x or not math.isfinite(x_new):
                break
            y_new = a0 + x_new * (a1 + x_new * (a2 + x_new * a3))
            if abs(y_new) >= abs(y):
                break
            x = x_new
            y = y_new
        det = x * x - x * mz + cov_xy
        if det != 0.0 and math.isfinite(det):
            centre_x = (mxz * (myy - x) - myz * mxy) / det * 0.5
            centre_y = (myz * (mxx - x) - mxz * mxy) / det * 0.5
            radius = math.sqrt(centre_x * centre_x + centre_y * centre_y + mz)
            if (math.isfinite(radius) and radius > 0.0
                    and radius < _LINE_RADIUS_WINDOWS * max(window, 1e-9)):
                is_line = False
                cx = centre_x + mx
                cy = centre_y + my
    line_error = 0.0
    for i in range(count):
        p = near + i * step
        e = abs(dir_x * (points[p, 1] - my) - dir_y * (points[p, 0] - mx))
        if e > line_error:
            line_error = e
    if is_line:
        return True, mx, my, dir_x, dir_y, cx, cy, radius, sign, line_error
    error = 0.0
    swept = 0.0
    for i in range(count):
        p = first + i
        ux = points[p, 0] - cx
        uy = points[p, 1] - cy
        e = abs(math.sqrt(ux * ux + uy * uy) - radius)
        if e > error:
            error = e
        if i + 1 < count:
            swept += ux * (points[p + 1, 1] - cy) - uy * (points[p + 1, 0] - cx)
    # The line is kept unless the arc fits closer (a degenerate solve of nearly
    # collinear samples can return a small, poorly fitting circle).
    if error >= improvement * line_error:
        return True, mx, my, dir_x, dir_y, cx, cy, radius, sign, line_error
    sign = 1.0 if swept >= 0.0 else -1.0
    return False, mx, my, dir_x, dir_y, cx, cy, radius, sign, error


@njit(cache=True, nogil=True)
def _arc_tangent_nb(is_line: bool, dx: float, dy: float, cx: float, cy: float,
                    sign: float, qx: float, qy: float):
    """The forward unit tangent of a side fit at the point ``(qx, qy)`` on it."""
    if is_line:
        return dx, dy
    ux = qx - cx
    uy = qy - cy
    norm = math.sqrt(ux * ux + uy * uy)
    if norm == 0.0:
        return dx, dy
    return -sign * uy / norm, sign * ux / norm


@njit(cache=True, nogil=True)
def _line_circle_nb(ox: float, oy: float, dx: float, dy: float, cx: float, cy: float,
                    radius: float, sx: float, sy: float):
    """The intersection of a line and a circle nearest ``(sx, sy)``."""
    fx = ox - cx
    fy = oy - cy
    b = fx * dx + fy * dy
    c = fx * fx + fy * fy - radius * radius
    disc = b * b - c
    if disc < 0.0:
        return False, 0.0, 0.0
    root = math.sqrt(disc)
    best = math.inf
    px = 0.0
    py = 0.0
    for t in (-b - root, -b + root):
        qx = ox + t * dx
        qy = oy + t * dy
        d = (qx - sx) * (qx - sx) + (qy - sy) * (qy - sy)
        if d < best:
            best = d
            px = qx
            py = qy
    return True, px, py


@njit(cache=True, nogil=True)
def _meeting_nb(b_line: bool, box: float, boy: float, bdx: float, bdy: float, bcx: float,
                bcy: float, br: float, bsign: float,
                a_line: bool, aox: float, aoy: float, adx: float, ady: float, acx: float,
                acy: float, ar: float, asign: float, sx: float, sy: float):
    """Where two side fits meet: their intersection nearest the sample ``(sx, sy)``
    and the turn (radians) between their forward tangents there.

    Returns ``(met, px, py, turn)``; ``met`` is False when the fits do not cross
    (parallel lines, disjoint or nested circles).
    """
    met = False
    px = 0.0
    py = 0.0
    if b_line and a_line:
        cross = bdx * ady - bdy * adx
        if abs(cross) >= 1e-9:
            distance = ((aox - box) * ady - (aoy - boy) * adx) / cross
            px = box + distance * bdx
            py = boy + distance * bdy
            met = True
    elif b_line:
        met, px, py = _line_circle_nb(box, boy, bdx, bdy, acx, acy, ar, sx, sy)
    elif a_line:
        met, px, py = _line_circle_nb(aox, aoy, adx, ady, bcx, bcy, br, sx, sy)
    else:
        ex = acx - bcx
        ey = acy - bcy
        d = math.sqrt(ex * ex + ey * ey)
        if d > 0.0 and d <= br + ar and d >= abs(br - ar):
            along = (br * br - ar * ar + d * d) / (2.0 * d)
            h2 = br * br - along * along
            h = math.sqrt(h2) if h2 > 0.0 else 0.0
            mx_ = bcx + along * ex / d
            my_ = bcy + along * ey / d
            best = math.inf
            for side in (-1.0, 1.0):
                qx = mx_ - side * h * ey / d
                qy = my_ + side * h * ex / d
                dist = (qx - sx) * (qx - sx) + (qy - sy) * (qy - sy)
                if dist < best:
                    best = dist
                    px = qx
                    py = qy
            met = True
    if not met:
        return False, 0.0, 0.0, 0.0
    tbx, tby = _arc_tangent_nb(b_line, bdx, bdy, bcx, bcy, bsign, px, py)
    tax, tay = _arc_tangent_nb(a_line, adx, ady, acx, acy, asign, px, py)
    return True, px, py, _angle_between_nb(tbx, tby, tax, tay)


@njit(cache=True, nogil=True)
def _side_nb(points: np.ndarray, index: int, step: int, skip: float, length: float,
             minimum: float, smoothness: float, stops: np.ndarray, shrink: bool):
    """One side of a candidate corner: its window (``_side_window_nb``) and arc
    (``_fit_side_nb``).

    The side must be at least ``minimum`` long, within ``smoothness`` of its arc,
    and its arc may turn by at most ``_SIDE_SWEEP`` over the window. With
    ``shrink``, a full window that fails (it runs into another feature, such as
    round the floor of a narrow slot) is judged on its nearest ``minimum`` past
    the tip instead, like an edge cut short by a neighbouring corner.

    Returns ``(ok, near, far, cut, stopped, is_line, ox, oy, dx, dy, cx, cy,
    radius, sign, error)``; ``cut`` is True when the window ended before
    ``skip + length`` (a stop, the chain end or a shrink), so a longer window
    would be the same.
    """
    near, far, window, travelled, stopped = _side_window_nb(points, index, step, skip, length,
                                                            minimum, stops)
    cut = travelled < skip + length
    ok = window >= minimum
    fit = _fit_side_nb(points, near, far, step, window)
    if ok and (fit[9] > smoothness or (not fit[0] and window > _SIDE_SWEEP * fit[7])):
        ok = False
        if shrink and not cut and length > minimum:
            near, far, window, travelled, stopped = _side_window_nb(
                points, index, step, skip, minimum, minimum, stops)
            cut = True
            ok = window >= minimum
            fit = _fit_side_nb(points, near, far, step, window)
            ok = ok and fit[9] <= smoothness and (fit[0] or window <= _SIDE_SWEEP * fit[7])
    return (ok, near, far, cut, stopped, fit[0], fit[1], fit[2], fit[3], fit[4], fit[5],
            fit[6], fit[7], fit[8], fit[9])


@njit(cache=True, nogil=True)
def _corner_candidates(points: np.ndarray, smoothness: float, minimum: float,
                       span: float, skip: float, threshold: float, allowance: float,
                       stops: np.ndarray, active: np.ndarray):
    """Per-index corner evidence: a direction jump between two smooth sides.

    For every active interior sample each side is fitted with a circular arc
    (``_fit_side_nb``; a straight line is the arc of infinite radius) on a window
    that starts ``skip`` of arc from the sample (the tip skip, so a round the
    allowance would sharpen is left out) and runs ``span`` further, and again
    ``span * 1.75`` further (``_side_nb``; a span-scale side that is not smooth is
    judged on its nearest ``minimum`` instead). At both scales each side must be
    at least ``minimum`` long and within ``smoothness`` of its arc, and the two
    arcs' forward tangents where they meet (their intersection nearest the sample)
    must turn by at least ``threshold``. One arc fitted through both long sides
    and the tip must fit clearly worse than the two sides do
    (``_ONE_ARC_RATIO``, ``_ONE_ARC_FLOOR``): a smooth curve, a circle however
    tight or a pixel staircase, is one arc, a corner is two. The long-scale
    meeting point is the cusp, and it must lie within ``allowance`` of the
    sample. A side window ends at a neighbouring corner marked in ``stops`` (2
    confirmed, 1 provisional), so a short edge between two corners is judged on
    its own; it is then the same window at both scales. ``cuts`` records, per
    side, the provisional corner that ended its long window (-1 for none), for
    the mutual support test in ``_geometric_corners``.

    Emits ``(valid, turn, ix, iy, error, move, cuts)`` per index: the span-scale
    turn, the cusp, the two long sides' summed error and the cusp's distance
    from the sample feed the Python non-max suppression.
    """
    n = points.shape[0]
    valid = np.zeros(n, dtype=np.bool_)
    turns = np.zeros(n, dtype=np.float64)
    ix = np.zeros(n, dtype=np.float64)
    iy = np.zeros(n, dtype=np.float64)
    errors = np.zeros(n, dtype=np.float64)
    moves = np.zeros(n, dtype=np.float64)
    cuts = np.full((n, 2), -1, dtype=np.int64)
    for index in range(1, n - 1):
        if not active[index]:
            continue
        sx = points[index, 0]
        sy = points[index, 1]
        b = _side_nb(points, index, -1, skip, span, minimum, smoothness, stops, True)
        if not b[0]:
            continue
        a = _side_nb(points, index, 1, skip, span, minimum, smoothness, stops, True)
        if not a[0]:
            continue
        met, _, _, turn = _meeting_nb(b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12],
                                      b[13], a[5], a[6], a[7], a[8], a[9], a[10], a[11],
                                      a[12], a[13], sx, sy)
        if not met or turn < threshold:
            continue
        # The long scale: a side cut short is the same window at both scales.
        lb = b if b[3] else _side_nb(points, index, -1, skip, span * 1.75, minimum,
                                     smoothness, stops, False)
        if not lb[0]:
            continue
        la = a if a[3] else _side_nb(points, index, 1, skip, span * 1.75, minimum,
                                     smoothness, stops, False)
        if not la[0]:
            continue
        met, px, py, long_turn = _meeting_nb(lb[5], lb[6], lb[7], lb[8], lb[9], lb[10],
                                             lb[11], lb[12], lb[13], la[5], la[6], la[7],
                                             la[8], la[9], la[10], la[11], la[12], la[13],
                                             sx, sy)
        if not met or long_turn < threshold:
            continue
        move = math.sqrt((px - sx) * (px - sx) + (py - sy) * (py - sy))
        if move > allowance:
            continue
        # One arc through both long sides and the tip.
        first = lb[2]
        last = la[2]
        length = 0.0
        for q in range(first, last):
            dx = points[q + 1, 0] - points[q, 0]
            dy = points[q + 1, 1] - points[q, 1]
            length += math.sqrt(dx * dx + dy * dy)
        one_arc = _fit_side_nb(points, first, last, 1, length, 1.0)[9]
        if one_arc <= _ONE_ARC_RATIO * max(lb[14], la[14]) + _ONE_ARC_FLOOR:
            continue
        valid[index] = True
        turns[index] = turn
        ix[index] = px
        iy[index] = py
        errors[index] = lb[14] + la[14]
        moves[index] = move
        if lb[4] >= 0 and stops[lb[4]] == 1:
            cuts[index, 0] = lb[4]
        if la[4] >= 0 and stops[la[4]] == 1:
            cuts[index, 1] = la[4]
    return valid, turns, ix, iy, errors, moves, cuts


@njit(cache=True, nogil=True)
def _provisional_corners(points: np.ndarray, deviation: float, minimum: float,
                         threshold: float, stops: np.ndarray, active: np.ndarray):
    """Samples that turn by ``threshold`` between two ``minimum``-long straight runs.

    The shortest-edge form of the corner test, used only to cut the side windows
    of other candidates: two corners joined by a short edge each need the other
    as a stop before either can pass, so both are proposed here first. Emits
    ``(valid, turn, error)`` per index; ``error`` is the two sides' summed
    straightness error, which ranks a sample at the apex (two straight sides)
    above one beside it whose window wraps round the apex.
    """
    n = points.shape[0]
    valid = np.zeros(n, dtype=np.bool_)
    turns = np.zeros(n, dtype=np.float64)
    errors = np.zeros(n, dtype=np.float64)
    for index in range(1, n - 1):
        if not active[index]:
            continue
        bdx, bdy, berr, _, _, _, btrav, _ = _side_line(points, index, -1, minimum, stops)
        if btrav < minimum or berr > deviation:
            continue
        adx, ady, aerr, _, _, _, atrav, _ = _side_line(points, index, 1, minimum, stops)
        if atrav < minimum or aerr > deviation:
            continue
        turn = _angle_between_nb(bdx, bdy, adx, ady)
        if turn < threshold:
            continue
        valid[index] = True
        turns[index] = turn
        errors[index] = berr + aerr
    return valid, turns, errors


def _cyclic_context(ring: np.ndarray, span: float) -> int:
    """Samples of a closed ring needed on each side of it to walk ``span`` of arc.

    A window walked from any ring sample crosses the seam with less than ``span``
    of arc left, so the context is the larger of the step counts, backwards and
    forwards from the seam, whose arc length reaches ``span``, plus one. It may
    exceed the ring's own sample count when the ring is shorter than ``span``.
    """
    steps = np.hypot(*(np.roll(ring, -1, axis=0) - ring).T)
    perimeter = float(steps.sum())
    if len(ring) == 0 or perimeter <= 0.0:
        return 1
    laps = int(span // perimeter) + 2
    forward = np.cumsum(np.tile(steps, laps))
    backward = np.cumsum(np.tile(steps[::-1], laps))
    return int(max(np.searchsorted(forward, span), np.searchsorted(backward, span))) + 2


def _cyclic_extension(ring: np.ndarray, context: int) -> np.ndarray:
    """``ring`` with ``context`` wrapped samples before it and ``context + 1`` after.

    Index ``context`` is the seam and ``context + len(ring)`` its closing repeat, so
    a window walked from any ring sample sees the samples across the seam.
    """
    count = len(ring)
    order = (np.arange(-context, count + context + 1) % count)
    return np.ascontiguousarray(ring[order], dtype=np.float64)


def _sharpening_allowance(settings: TraceSettings) -> float:
    """The corner sharpening allowance (px). Unset, it follows ``max_deviation``,
    and an automatic ``max_deviation`` is its pixel default, as
    ``resolve_trace_lengths`` fills them."""
    for value in (settings.corner_sharpening_allowance, settings.max_deviation):
        if value is not None:
            return max(0.0, float(value))
    return float(AUTOMATIC_LENGTHS["max_deviation"][1])


def _tip_skip(settings: TraceSettings) -> float:
    """The arc (px) left out of each side window next to a candidate corner: the
    rounding a corner sharpening allowance sharpens (``_TIP_SKIP_ALLOWANCES``)."""
    return _TIP_SKIP_ALLOWANCES * _sharpening_allowance(settings)


def _corner_reach(settings: TraceSettings) -> float:
    """The furthest arc (px) a corner test reads from its sample: the tip skip plus
    the long side window."""
    minimum = max(1e-4, float(settings.minimum_straight_length))
    span = max(minimum, float(settings.corner_analysis_span))
    return _tip_skip(settings) + span * 1.75


def _geometric_corners(points: np.ndarray, settings: TraceSettings,
                       closed: bool = False) -> dict[int, Point]:
    """Find corners: a direction jump between two smooth sides.

    Each side of a candidate is fitted with a circular arc (a straight line
    included) past the tip skip, at two scales, and the arcs' tangents where
    they meet must turn by ``corner_threshold``; the cusp is their intersection,
    within ``corner_sharpening_allowance`` of the sample (``_corner_candidates``).
    A circle's two sides are one arc, so it has no corner however tight; a pixel
    staircase is a smooth side, not a series of turns; a crescent's tip is two
    arcs crossing. The per-index evidence runs in Numba kernels; the non-max
    suppression stays here.

    Corners are confirmed in rounds. Every round cuts side windows at the
    corners confirmed so far and at provisional corners
    (``_provisional_corners``: a turn between two minimum-length straight runs),
    so an edge of at least ``minimum_straight_length`` between two corners (an
    H, a notch, a bar end) keeps both corners, and a sample beside a corner,
    whose window would wrap round it, is cut short there. The first round tests
    every sample; later rounds re-test the samples whose windows reach a cut.
    Rounds repeat until no corner is added.

    A ``closed`` chain (its last sample repeats its first) is analysed as a
    ring: the side windows run across the seam, index 0 is a candidate like any
    other sample, and suppression measures distance around the ring.
    """
    if len(points) < 5:
        return {}
    smoothness = max(1e-4, float(settings.side_smoothness))
    minimum = max(1e-4, float(settings.minimum_straight_length))
    span = max(minimum, float(settings.corner_analysis_span))
    threshold = math.radians(float(settings.corner_threshold))
    allowance = _sharpening_allowance(settings)
    skip = _tip_skip(settings)
    reach = _corner_reach(settings)
    if closed:
        ring = np.asarray(points[:-1], dtype=np.float64)
        count = len(ring)
        context = _cyclic_context(ring, reach)
        array = _cyclic_extension(ring, context)
        offset = context
        copies = np.arange(len(array))
        ring_index = (copies - offset) % count
    else:
        array = np.ascontiguousarray(points, dtype=np.float64)
        count = len(array)
        offset = 0
        ring_index = np.arange(count)
    first_index = 0 if closed else 1
    last_index = count if closed else count - 1
    own = np.zeros(len(array), dtype=np.bool_)
    own[offset + first_index:offset + last_index] = True

    source = np.asarray(points, dtype=np.float64)
    arc = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(source, axis=0).T))))
    perimeter = float(arc[-1])
    ring_arc = arc[:count]
    # Two candidates are one corner when they lie within half the span of arc, or
    # when their cusps lie within half the span (or the allowance) of each other:
    # on sparse samples (a polygon's vertices) both samples beside one corner
    # propose it. Each cusp is within the allowance of its sample, so such samples
    # are within twice the allowance of arc of each other.
    same_reach = span / 2 + 2 * allowance
    same_apart = max(span / 2, allowance)

    def arc_gap(first: int, second: int) -> float:
        length = abs(float(ring_arc[first] - ring_arc[second]))
        return min(length, perimeter - length) if closed else length

    def nearby(taken_order: list, taken_arcs: list, index: int, distance: float) -> list:
        """Taken ring indices within ``distance`` of arc of ``index``."""
        position = float(ring_arc[index])
        spans = [(position - distance, position + distance)]
        if closed:
            spans += [(position - distance + perimeter, position + distance + perimeter),
                      (position - distance - perimeter, position + distance - perimeter)]
        found = []
        for low, high in spans:
            lo = bisect.bisect_left(taken_arcs, low)
            hi = bisect.bisect_right(taken_arcs, high)
            found.extend(taken_order[lo:hi])
        return found

    def suppress(candidates, chosen: dict[int, Point]) -> dict[int, Point]:
        # One physical corner creates several neighbouring candidate samples. Keep
        # the one nearest its cusp (the apex, or the middle of a round), then the
        # smoothest sides, then the strongest turn.
        added: dict[int, Point] = {}
        taken = dict(chosen)
        taken_pairs = sorted((float(ring_arc[index]), index) for index in taken)
        taken_arcs = [pair[0] for pair in taken_pairs]
        taken_order = [pair[1] for pair in taken_pairs]
        for _, index, intersection in sorted(candidates, reverse=True):
            clash = False
            for other in nearby(taken_order, taken_arcs, index, same_reach):
                gap = arc_gap(index, other)
                point = taken[other]
                if gap < span / 2 or math.hypot(intersection[0] - point[0],
                                                 intersection[1] - point[1]) < same_apart:
                    clash = True
                    break
            if not clash:
                added[index] = intersection
                taken[index] = intersection
                where = bisect.bisect_left(taken_arcs, float(ring_arc[index]))
                taken_arcs.insert(where, float(ring_arc[index]))
                taken_order.insert(where, index)
        return added

    def stop_mask(confirmed, provisional=()) -> np.ndarray:
        kinds = np.zeros(count, dtype=np.int8)
        kinds[list(provisional)] = 1
        kinds[list(confirmed)] = 2
        return kinds[ring_index]

    def evidence(stops: np.ndarray, active: np.ndarray):
        valid, turns, ix, iy, errors, moves, cuts = _corner_candidates(
            array, smoothness, minimum, span, skip, threshold, allowance, stops, active)
        valid &= own
        # A candidate whose two sides both end at provisional corners stands only
        # if both of those are candidates too (a small polygon's corners support
        # each other; a tight curve's noise-level passes do not). Removing one can orphan another, so repeat to a fixed point.
        supported = valid.copy()

        def backed(cut: int) -> bool:
            # A supported candidate within half the span of arc of the cut, or
            # its neighbouring sample (on sparse samples, a polygon's vertex).
            centre = int(ring_index[cut])
            gaps = np.abs(ring_arc[supported_ring] - ring_arc[centre])
            steps = np.abs(supported_ring - centre)
            if closed:
                gaps = np.minimum(gaps, perimeter - gaps)
                steps = np.minimum(steps, count - steps)
            return bool(((gaps < span / 2) | (steps <= 1)).any())

        changed = True
        while changed:
            changed = False
            supported_ring = np.unique(ring_index[supported])
            for position in np.flatnonzero(supported & (cuts[:, 0] >= 0) & (cuts[:, 1] >= 0)):
                if not (backed(cuts[position, 0]) and backed(cuts[position, 1])):
                    supported[position] = False
                    changed = True
        return [((-round(float(moves[position]), 9), -round(float(errors[position]), 9),
                  float(turns[position])),
                 int(ring_index[position]), (float(ix[position]), float(iy[position])))
                for position in np.flatnonzero(supported)]

    def within(indices, distance: float) -> np.ndarray:
        """Extended-array positions within ``distance`` of arc of any ring index."""
        marks = np.asarray(sorted(indices))
        cut_arc = arc[marks]
        position_arc = arc[ring_index]
        nearest = np.searchsorted(cut_arc, position_arc)
        gap = np.full(len(array), np.inf)
        for shift in (-1, 0):
            pick = nearest + shift
            ok = (pick >= 0) & (pick < len(cut_arc))
            gap[ok] = np.minimum(gap[ok], np.abs(position_arc[ok] - cut_arc[pick[ok]]))
        if closed:
            # Distances across the seam.
            gap = np.minimum(gap, position_arc + perimeter - cut_arc[-1])
            gap = np.minimum(gap, cut_arc[0] + perimeter - position_arc)
        return gap <= distance

    def propose(stops: np.ndarray, active: np.ndarray) -> dict[int, Point]:
        valid, turns, errors = _provisional_corners(array, smoothness, minimum, threshold,
                                                    stops, active)
        return suppress(
            [((0.0, -float(errors[position]), float(turns[position])),
              int(ring_index[position]),
              (float(array[position, 0]), float(array[position, 1])))
             for position in np.flatnonzero(valid & own)], chosen)

    chosen: dict[int, Point] = {}
    # Round one tests every sample; later rounds only the samples whose windows
    # can reach a corner added in the previous round (every other sample sees
    # the same stops as before, so its verdict cannot change).
    provisional = propose(np.zeros(len(array), dtype=np.int8), own)
    active = own
    while True:
        stops = stop_mask(chosen, provisional)
        added = suppress(evidence(stops, active & (stops != 2)), chosen)
        if not added:
            break
        chosen.update(added)
        near_added = within(added, reach + minimum)
        # Provisional corners near the new corners are proposed again with the
        # new corners as stops; the rest stand.
        confirmed_stops = stop_mask(chosen)
        keep = {index: point for index, point in provisional.items()
                if not near_added[offset + index] and index not in chosen}
        fresh = propose(confirmed_stops, own & near_added & (confirmed_stops == 0))
        provisional = {**keep, **{index: point for index, point in fresh.items()
                                  if index not in keep}}
        active = own & near_added
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
def _cubic_solve_nb(points: np.ndarray, start: int, end: int, tolerance: float, pad_cap: float,
                    t: np.ndarray, fitted: np.ndarray):
    """Closed-form cubic Bezier controls for samples ``start..end``.

    The two interior controls solve the least-squares fit of the fixed-endpoint
    Bezier basis. For three samples the underdetermined system is replaced by the
    exact quadratic-through-the-sample degree-elevated to a cubic. For four or
    more samples the ``A^T A x = A^T rhs`` 2x2 symmetric normal equations are
    solved per coordinate via the determinant, the closed form of the numpy
    ``lstsq``. Controls are clipped to the padded sample box (``_pad_cap``) and ``fitted[0:n]``
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
    if inner > pad_cap:
        inner = pad_cap
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


def _cubic(points: np.ndarray, tolerance: float, pad_cap: float):
    array = np.ascontiguousarray(points, dtype=np.float64)
    n = len(array)
    t = np.empty(n, dtype=np.float64)
    fitted = np.empty((n, 2), dtype=np.float64)
    c1x, c1y, c2x, c2y = _cubic_solve_nb(array, 0, n - 1, tolerance, pad_cap, t, fitted)
    return np.array((c1x, c1y)), np.array((c2x, c2y)), fitted


@njit(cache=True, nogil=True)
def _walk_nb(points: np.ndarray, index: int, step: int, span: float, lo: int, hi: int) -> int:
    """The sample reached walking from ``index`` by ``step`` until ``span`` of arc.

    The walk stops at the first sample whose accumulated arc length reaches
    ``span``, or at ``lo``/``hi``, whichever comes first.
    """
    travelled = 0.0
    cursor = index
    while lo <= cursor + step <= hi and travelled < span:
        following = cursor + step
        dx = points[following, 0] - points[cursor, 0]
        dy = points[following, 1] - points[cursor, 1]
        travelled += math.sqrt(dx * dx + dy * dy)
        cursor = following
    return cursor


@njit(cache=True, nogil=True)
def _tls_direction_nb(points: np.ndarray, first: int, last: int):
    """Unit principal direction of samples ``first..last``, oriented first to last.

    The closed-form total-least-squares direction (the principal eigenvector of
    the centred 2x2 covariance), so a pixel staircase yields its sampled
    diagonal rather than one of its steps. Returns
    ``(dx, dy, ok)``; ``ok`` is false when the window has fewer than two distinct
    samples.
    """
    count = last - first + 1
    if count < 2:
        return 1.0, 0.0, False
    sum_x = 0.0
    sum_y = 0.0
    for p in range(first, last + 1):
        sum_x += points[p, 0]
        sum_y += points[p, 1]
    origin_x = sum_x / count
    origin_y = sum_y / count
    a = 0.0
    b = 0.0
    c = 0.0
    for p in range(first, last + 1):
        cx = points[p, 0] - origin_x
        cy = points[p, 1] - origin_y
        a += cx * cx
        b += cx * cy
        c += cy * cy
    if a + c <= 0.0:
        return 1.0, 0.0, False
    if b == 0.0:
        if a >= c:
            dir_x = 1.0
            dir_y = 0.0
        else:
            dir_x = 0.0
            dir_y = 1.0
    else:
        half_diff = (a - c) * 0.5
        eigen_x = half_diff + math.sqrt(half_diff * half_diff + b * b)
        norm = math.sqrt(eigen_x * eigen_x + b * b)
        dir_x = eigen_x / norm
        dir_y = b / norm
    sign_x = points[last, 0] - points[first, 0]
    sign_y = points[last, 1] - points[first, 1]
    if dir_x * sign_x + dir_y * sign_y < 0.0:
        dir_x = -dir_x
        dir_y = -dir_y
    return dir_x, dir_y, True


@njit(cache=True, nogil=True)
def _angle_between_nb(ax: float, ay: float, bx: float, by: float) -> float:
    dot = ax * bx + ay * by
    if dot > 1.0:
        dot = 1.0
    elif dot < -1.0:
        dot = -1.0
    return math.acos(dot)


@njit(cache=True, nogil=True)
def _join_nb(points: np.ndarray, index: int, lo: int, hi: int,
             in_line: bool, in_x: float, in_y: float,
             out_line: bool, out_x: float, out_y: float, large: float):
    """The shared forward unit tangent of the symmetric node at ``index``.

    A straight-line side's direction (``in_x, in_y`` or ``out_x, out_y``, its
    chord) wins, so the line stays straight and the curve's handle continues it.
    Otherwise it is the total-least-squares direction of the samples within
    ``large`` of arc on both sides (windows limited to ``lo..hi``), oriented
    forward; a degenerate window falls back to the chord of the two neighbouring
    samples.
    """
    if in_line:
        return in_x, in_y
    if out_line:
        return out_x, out_y
    first = _walk_nb(points, index, -1, large, lo, hi)
    last = _walk_nb(points, index, 1, large, lo, hi)
    tx, ty, ok = _tls_direction_nb(points, first, last)
    if ok:
        return tx, ty
    first = max(lo, index - 1)
    last = min(hi, index + 1)
    tx, ty, _ = _chord_direction_nb(points, first, last)
    return tx, ty


@njit(cache=True, nogil=True)
def _ray_limit_nb(px: float, py: float, dx: float, dy: float,
                  lowx: float, upx: float, lowy: float, upy: float) -> float:
    """Largest ``s >= 0`` keeping ``(px, py) + s * (dx, dy)`` inside the box."""
    limit = math.inf
    if dx > 1e-12:
        limit = min(limit, (upx - px) / dx)
    elif dx < -1e-12:
        limit = min(limit, (lowx - px) / dx)
    if dy > 1e-12:
        limit = min(limit, (upy - py) / dy)
    elif dy < -1e-12:
        limit = min(limit, (lowy - py) / dy)
    return max(0.0, limit)


# The shortest fixed-tangent handle, as a fraction of the piece's chord.
_MIN_HANDLE = 0.1
# Added to a split half's score when it would turn back along its chord.
_REVERSAL_PENALTY = 1e6


@njit(cache=True, nogil=True)
def _chord_monotone_nb(p0x: float, p0y: float, c1x: float, c1y: float,
                       c2x: float, c2y: float, p3x: float, p3y: float) -> bool:
    """Whether the cubic advances along its chord without ever turning back.

    Its derivative projected on the chord is the quadratic Bernstein polynomial
    of the control-leg projections ``d0, d1, d2``; the cubic is chord-monotone
    when that quadratic stays non-negative on ``[0, 1]``. A cubic that is not can
    loop or double back between its samples even when every sample is within
    tolerance.
    """
    ux = p3x - p0x
    uy = p3y - p0y
    d0 = (c1x - p0x) * ux + (c1y - p0y) * uy
    d1 = (c2x - c1x) * ux + (c2y - c1y) * uy
    d2 = (p3x - c2x) * ux + (p3y - c2y) * uy
    scale = ux * ux + uy * uy
    slack = -1e-9 * scale
    if d0 < slack or d2 < slack:
        return False
    if d1 >= 0.0:
        return True
    denominator = d0 - 2.0 * d1 + d2
    if denominator <= 0.0:
        return True
    vertex = (d0 - d1) / denominator
    if vertex <= 0.0 or vertex >= 1.0:
        return True
    return (d0 * d2 - d1 * d1) / denominator >= slack


@njit(cache=True, nogil=True)
def _constrained_cubic_nb(points: np.ndarray, start: int, end: int, tolerance: float, pad_cap: float,
                          start_fixed: bool, stx: float, sty: float,
                          end_fixed: bool, etx: float, ety: float,
                          t: np.ndarray, fitted: np.ndarray, given_t: bool):
    """Cubic controls for samples ``start..end`` with optional fixed end tangents.

    A fixed start places the first control on the ray ``P0 + a * T0`` and a fixed
    end the second on ``P3 - b * T3`` (``T`` are forward unit tangents), so only
    the handle lengths ``a``, ``b`` are fitted: Schneider's least-squares handle
    lengths, generalised to one fixed and one free end by eliminating the free
    control in closed form. A free end is fitted as a 2-D control exactly as
    ``_cubic_solve_nb`` does, which serves the both-free case unchanged. A
    non-positive or rank-deficient handle length falls back to a third of the
    chord (Schneider's heuristic), and a fitted length is kept between
    ``_MIN_HANDLE`` of the chord and the chord: a near-zero handle would turn the
    curve abruptly beside the node (a kink in all but name) and one longer than
    the chord invites a loop; the error test then splits a piece that cannot fit
    within those bounds. Fixed handles are shortened, never rotated, to stay in
    the padded sample box that free controls are clipped to.

    Two samples (no interior evidence) get third-of-chord handles, shortened so
    the curve bows at most half the tolerance from its chord.

    The samples' curve parameters are their cumulative-chord parameters, or, with
    ``given_t``, the values already in ``t[0:n]`` (a Newton reparameterisation).
    ``fitted[0:n]`` is filled with the curve at those parameters. Returns
    ``(c1x, c1y, c2x, c2y)``.
    """
    n = end - start + 1
    if not start_fixed and not end_fixed and n >= 3 and not given_t:
        return _cubic_solve_nb(points, start, end, tolerance, pad_cap, t, fitted)
    if not given_t:
        _parameters_nb(points, start, end, t)
    p0x = points[start, 0]
    p0y = points[start, 1]
    p3x = points[end, 0]
    p3y = points[end, 1]
    chord_x = p3x - p0x
    chord_y = p3y - p0y
    chord = math.sqrt(chord_x * chord_x + chord_y * chord_y)
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
    if inner > pad_cap:
        inner = pad_cap
    padding = tolerance if tolerance > inner else inner
    lowx = minx - padding
    upx = maxx + padding
    lowy = miny - padding
    upy = maxy + padding

    heuristic = chord / 3.0
    a = heuristic
    b = heuristic
    c1x = p0x + chord_x / 3.0
    c1y = p0y + chord_y / 3.0
    c2x = p3x - chord_x / 3.0
    c2y = p3y - chord_y / 3.0
    if n >= 3:
        s11 = 0.0
        s12 = 0.0
        s22 = 0.0
        # Residual sums; each case subtracts the terms its fixed controls own.
        ra = 0.0
        rb = 0.0
        r1x = 0.0
        r1y = 0.0
        r2x = 0.0
        r2y = 0.0
        for i in range(n):
            ti = t[i]
            omt = 1.0 - ti
            b0 = omt * omt * omt
            b1 = 3.0 * omt * omt * ti
            b2 = 3.0 * omt * ti * ti
            b3 = ti * ti * ti
            s11 += b1 * b1
            s12 += b1 * b2
            s22 += b2 * b2
            qx = points[start + i, 0]
            qy = points[start + i, 1]
            if start_fixed and end_fixed:
                rx = qx - (b0 + b1) * p0x - (b2 + b3) * p3x
                ry = qy - (b0 + b1) * p0y - (b2 + b3) * p3y
                ra += b1 * (stx * rx + sty * ry)
                rb += b2 * (etx * rx + ety * ry)
            elif start_fixed:
                rx = qx - (b0 + b1) * p0x - b3 * p3x
                ry = qy - (b0 + b1) * p0y - b3 * p3y
                ra += b1 * (stx * rx + sty * ry)
                r2x += b2 * rx
                r2y += b2 * ry
            elif not end_fixed:
                rx = qx - b0 * p0x - b3 * p3x
                ry = qy - b0 * p0y - b3 * p3y
                r1x += b1 * rx
                r1y += b1 * ry
                r2x += b2 * rx
                r2y += b2 * ry
            else:
                rx = qx - b0 * p0x - (b2 + b3) * p3x
                ry = qy - b0 * p0y - (b2 + b3) * p3y
                rb += b2 * (etx * rx + ety * ry)
                r1x += b1 * rx
                r1y += b1 * ry
        epsilon = 1e-6 * chord
        if start_fixed and end_fixed:
            # [s11, -s12 d; -s12 d, s22] [a; b] = [ra; -rb], d = T0 . T3.
            off = -s12 * (stx * etx + sty * ety)
            det = s11 * s22 - off * off
            # Relative rank tests: a single interior sample leaves the system
            # rank-deficient up to rounding, and its solution is then arbitrary.
            if det > 1e-9 * s11 * s22:
                a_fit = (ra * s22 - off * -rb) / det
                b_fit = (s11 * -rb - off * ra) / det
                if a_fit > epsilon and b_fit > epsilon:
                    a = a_fit
                    b = b_fit
            a = min(max(a, _MIN_HANDLE * chord), chord)
            b = min(max(b, _MIN_HANDLE * chord), chord)
        elif start_fixed:
            if s22 > 1e-30:
                denominator = s11 - s12 * s12 / s22
                if denominator > 1e-9 * s11:
                    a_fit = (ra - s12 * (stx * r2x + sty * r2y) / s22) / denominator
                    if a_fit > epsilon:
                        a = a_fit
                a = min(max(a, _MIN_HANDLE * chord), chord)
                c2x = (r2x - s12 * a * stx) / s22
                c2y = (r2y - s12 * a * sty) / s22
        elif not end_fixed:
            # Both free: the per-coordinate 2x2 normal equations of _cubic_solve_nb.
            det = s11 * s22 - s12 * s12
            if abs(det) < 1e-30:
                c1x = p0x
                c1y = p0y
                c2x = p3x
                c2y = p3y
            else:
                c1x = (r1x * s22 - r2x * s12) / det
                c2x = (s11 * r2x - s12 * r1x) / det
                c1y = (r1y * s22 - r2y * s12) / det
                c2y = (s11 * r2y - s12 * r1y) / det
        else:
            if s11 > 1e-30:
                denominator = s22 - s12 * s12 / s11
                if denominator > 1e-9 * s22:
                    b_fit = (s12 * (etx * r1x + ety * r1y) / s11 - rb) / denominator
                    if b_fit > epsilon:
                        b = b_fit
                b = min(max(b, _MIN_HANDLE * chord), chord)
                c1x = (r1x + s12 * b * etx) / s11
                c1y = (r1y + s12 * b * ety) / s11
    if start_fixed:
        a = min(a, _ray_limit_nb(p0x, p0y, stx, sty, lowx, upx, lowy, upy))
        c1x = p0x + a * stx
        c1y = p0y + a * sty
    else:
        c1x = min(max(c1x, lowx), upx)
        c1y = min(max(c1y, lowy), upy)
    if end_fixed:
        b = min(b, _ray_limit_nb(p3x, p3y, -etx, -ety, lowx, upx, lowy, upy))
        c2x = p3x - b * etx
        c2y = p3y - b * ety
    else:
        c2x = min(max(c2x, lowx), upx)
        c2y = min(max(c2y, lowy), upy)
    if n == 2 and chord > 0.0:
        # No interior sample bounds the bow, so bound it directly: the offset
        # from the chord is linear in the two handle vectors.
        nx = -chord_y / chord
        ny = chord_x / chord
        h1 = (c1x - p0x) * nx + (c1y - p0y) * ny
        h2 = (c2x - p3x) * nx + (c2y - p3y) * ny
        bow = 0.0
        for k in range(1, 16):
            tk = k / 16.0
            omk = 1.0 - tk
            offset = abs(3.0 * omk * omk * tk * h1 + 3.0 * omk * tk * tk * h2)
            if offset > bow:
                bow = offset
        limit = 0.5 * tolerance
        if bow > limit:
            scale = limit / bow
            c1x = p0x + (c1x - p0x) * scale
            c1y = p0y + (c1y - p0y) * scale
            c2x = p3x + (c2x - p3x) * scale
            c2y = p3y + (c2y - p3y) * scale
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


@njit(cache=True, nogil=True)
def _reparameterize_nb(points: np.ndarray, start: int, end: int, c1x: float, c1y: float,
                       c2x: float, c2y: float, t: np.ndarray) -> None:
    """One Newton step moving each interior ``t[i]`` to its sample's nearest curve point.

    Schneider's reparameterisation: the root of ``(B(t) - Q) . B'(t)``. Values stay
    in ``[0, 1]``; the end parameters stay 0 and 1.
    """
    p0x = points[start, 0]
    p0y = points[start, 1]
    p3x = points[end, 0]
    p3y = points[end, 1]
    for i in range(1, end - start):
        ti = t[i]
        omt = 1.0 - ti
        bx = (omt * omt * omt * p0x + 3.0 * omt * omt * ti * c1x
              + 3.0 * omt * ti * ti * c2x + ti * ti * ti * p3x)
        by = (omt * omt * omt * p0y + 3.0 * omt * omt * ti * c1y
              + 3.0 * omt * ti * ti * c2y + ti * ti * ti * p3y)
        d1x = 3.0 * (omt * omt * (c1x - p0x) + 2.0 * omt * ti * (c2x - c1x) + ti * ti * (p3x - c2x))
        d1y = 3.0 * (omt * omt * (c1y - p0y) + 2.0 * omt * ti * (c2y - c1y) + ti * ti * (p3y - c2y))
        d2x = 6.0 * (omt * (c2x - 2.0 * c1x + p0x) + ti * (p3x - 2.0 * c2x + c1x))
        d2y = 6.0 * (omt * (c2y - 2.0 * c1y + p0y) + ti * (p3y - 2.0 * c2y + c1y))
        ex = bx - points[start + i, 0]
        ey = by - points[start + i, 1]
        numerator = ex * d1x + ey * d1y
        denominator = d1x * d1x + d1y * d1y + ex * d2x + ey * d2y
        if abs(denominator) > 1e-12:
            ti = ti - numerator / denominator
            if ti < 0.0:
                ti = 0.0
            elif ti > 1.0:
                ti = 1.0
            t[i] = ti


@njit(cache=True, nogil=True)
def _chord_direction_nb(points: np.ndarray, first: int, last: int):
    dx = points[last, 0] - points[first, 0]
    dy = points[last, 1] - points[first, 1]
    length = math.sqrt(dx * dx + dy * dy)
    if length > 0.0:
        return dx / length, dy / length, length
    return 1.0, 0.0, 0.0


@njit(cache=True, nogil=True)
def _split_score_nb(points: np.ndarray, start: int, index: int, end: int, tolerance: float, pad_cap: float,
                    s_fixed: bool, stx: float, sty: float, e_fixed: bool, etx: float, ety: float,
                    tx: float, ty: float, t: np.ndarray, fitted: np.ndarray) -> float:
    """The larger max sample error of the two constrained halves sharing tangent ``(tx, ty)``.

    A half that would turn back along its chord (``_chord_monotone_nb``) scores
    ``_REVERSAL_PENALTY`` plus its error, so any tangent that keeps both halves
    advancing is preferred; this matters most for a two-sample half, which
    cannot split again.
    """
    worst = 0.0
    for half in range(2):
        first = start if half == 0 else index
        last = index if half == 0 else end
        m = last - first + 1
        if half == 0:
            c1x, c1y, c2x, c2y = _constrained_cubic_nb(
                points, first, last, tolerance, pad_cap, s_fixed, stx, sty, True, tx, ty,
                t[:m], fitted[:m], False)
        else:
            c1x, c1y, c2x, c2y = _constrained_cubic_nb(
                points, first, last, tolerance, pad_cap, True, tx, ty, e_fixed, etx, ety,
                t[:m], fitted[:m], False)
        score = 0.0
        for i in range(m):
            dx = fitted[i, 0] - points[first + i, 0]
            dy = fitted[i, 1] - points[first + i, 1]
            error = math.sqrt(dx * dx + dy * dy)
            if error > score:
                score = error
        if not _chord_monotone_nb(points[first, 0], points[first, 1], c1x, c1y, c2x, c2y,
                                  points[last, 0], points[last, 1]):
            score += _REVERSAL_PENALTY
        if score > worst:
            worst = score
    return worst


@njit(cache=True, nogil=True)
def _best_split_tangent_nb(points: np.ndarray, start: int, index: int, end: int,
                           lo: int, hi: int, tolerance: float, pad_cap: float,
                           s_fixed: bool, stx: float, sty: float,
                           e_fixed: bool, etx: float, ety: float,
                           tx: float, ty: float, small: float, large: float,
                           t: np.ndarray, fitted: np.ndarray):
    """The shared tangent of a smooth split point that best fits both halves.

    Candidates are the total-least-squares directions of the samples centred on
    the split within ``large`` (the default, ``(tx, ty)``) and within ``small``,
    and the chord of the split's two neighbouring samples; the one whose worse
    half has the smaller max sample error wins (ties keep the earlier).
    """
    best_x = tx
    best_y = ty
    best = _split_score_nb(points, start, index, end, tolerance, pad_cap, s_fixed, stx, sty,
                           e_fixed, etx, ety, tx, ty, t, fitted)
    for candidate in range(2):
        if candidate == 0:
            first = _walk_nb(points, index, -1, small, lo, hi)
            last = _walk_nb(points, index, 1, small, lo, hi)
        else:
            first = max(lo, index - 1)
            last = min(hi, index + 1)
        cx, cy, ok = _tls_direction_nb(points, first, last)
        if not ok:
            continue
        if cx * tx + cy * ty < 0.0:
            cx = -cx
            cy = -cy
        score = _split_score_nb(points, start, index, end, tolerance, pad_cap, s_fixed, stx, sty,
                                e_fixed, etx, ety, cx, cy, t, fitted)
        if score < best:
            best = score
            best_x = cx
            best_y = cy
    # Golden-section refinement of the angle around the best candidate.
    base = math.atan2(best_y, best_x)
    lower = -0.35
    upper = 0.35
    ratio = 0.6180339887498949
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    f_left = _split_score_nb(points, start, index, end, tolerance, pad_cap, s_fixed, stx, sty, e_fixed,
                             etx, ety, math.cos(base + left), math.sin(base + left), t, fitted)
    f_right = _split_score_nb(points, start, index, end, tolerance, pad_cap, s_fixed, stx, sty, e_fixed,
                              etx, ety, math.cos(base + right), math.sin(base + right), t, fitted)
    for _ in range(10):
        if f_left <= f_right:
            upper = right
            right = left
            f_right = f_left
            left = upper - ratio * (upper - lower)
            f_left = _split_score_nb(points, start, index, end, tolerance, pad_cap, s_fixed, stx, sty,
                                     e_fixed, etx, ety, math.cos(base + left),
                                     math.sin(base + left), t, fitted)
        else:
            lower = left
            left = right
            f_left = f_right
            right = lower + ratio * (upper - lower)
            f_right = _split_score_nb(points, start, index, end, tolerance, pad_cap, s_fixed, stx, sty,
                                      e_fixed, etx, ety, math.cos(base + right),
                                      math.sin(base + right), t, fitted)
    if f_left < best and f_left <= f_right:
        best_x = math.cos(base + left)
        best_y = math.sin(base + left)
    elif f_right < best:
        best_x = math.cos(base + right)
        best_y = math.sin(base + right)
    return best_x, best_y


# Node placement. Every piece a curve unit is fitted with ends at chosen
# candidate points (see ``_place_nodes_nb``); records column 7 holds the kind of
# each piece's end node, 8-9 its forward unit tangent and 10 its sample index.
_KIND_END = 0
_KIND_EXTREME = 1
_KIND_INFLECTION = 2
_KIND_BEND = 3
_KIND_SPLIT = 4
_KIND_FALLBACK = 5
_KIND_LINE_JOIN = 6
_KIND_MINOR_INFLECTION = 7
_KIND_MINOR_BEND = 8
_RECORD_COLUMNS = 11

PLACEMENT_MODES = ("prefer_extremes", "at_extremes", "fewest")
SNAP_MODES = ("off", "axis", "axis_diagonal")
_PLACEMENT_AT_EXTREMES = 1
_PLACEMENT_FEWEST = 2

# Candidate detectors, on the samples smoothed at the curve feature scale. An
# axis extreme must rise ``max(_NOISE_FLOOR, _EXTREME_PROMINENCE * scale)`` px
# above the curve on both sides before a more extreme point: an eighth of the
# feature scale (0.5 px at the pixel-era 4 px scale), so a bump that is small
# against the scale is ignored at every image size, and never less than the
# half pixel of raster noise. An inflection is an extremum of the smoothed
# tangent angle whose lobes each turn by at least ``_LOBE_TURN``; a bend is a
# curvature peak rising ``1 / (_BEND_PROMINENCE_SCALES * scale)`` (1/px) above
# the gentlest curvature on either side. Both are *significant* when they also
# stand out in position by ``max(_NOISE_FLOOR, tolerance / 2)`` px: each lobe of
# an inflection bulges that far from its chord, and a bend's region (between
# the gentlest curvature on either side) bulges that far and turns by at least
# ``2 * _LOBE_TURN``. The rest are *minor*: low-amplitude ripple of a pixel
# boundary (a staircase leaves ripples a few tenths of a pixel high and 10-20 px
# long) that still marks where the boundary changes direction. Minor candidates
# are ranked below significant ones and fallback splits in ties, but they are
# real positions on the curve and often let a route span a long gentle edge in
# fewer pieces than worst-sample splits would.
_NOISE_FLOOR = 0.5
_EXTREME_PROMINENCE = 0.125
_LOBE_TURN = math.radians(10.0)
_BEND_PROMINENCE_SCALES = 4.0
# A chord-monotone cubic's tangents stay within 90 degrees of its chord, so no
# piece can span samples whose smoothed tangent turns by much more than 180
# degrees; pairs spanning more than this are not tried.
_TURN_PRUNE = 1.5 * math.pi
# The path search stops extending a piece backwards from a node after this many
# consecutive start candidates whose piece misses by more than
# ``_PRUNE_FACTOR`` times the tolerance (0 searches every start). A performance
# bound: a longer piece is then practically never within tolerance.
_PRUNE_STREAK = 6
_PRUNE_FACTOR = 3.0
# A piece missing the tolerance by less than this factor is refitted at
# Newton-improved sample parameters (Schneider) before it is judged.
_NEWTON_WINDOW = 4.0
_UNREACHED = 1 << 60


@njit(cache=True, nogil=True)
def _smooth_unit_nb(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                    scale: float, sx: np.ndarray, sy: np.ndarray,
                    tx: np.ndarray, ty: np.ndarray) -> None:
    """Gaussian-smoothed positions and tangents of samples ``first..last``.

    Weights are Gaussian in arc length with standard deviation half the curve
    feature ``scale``, truncated at ``scale`` on each side; the window may reach
    beyond the unit but never past ``lo..hi``. ``sx, sy`` are the weighted mean
    positions, ``tx, ty`` the weighted sum of the segment vectors (a smoothed,
    unnormalised forward tangent).
    """
    a = _walk_nb(points, first, -1, scale, lo, hi)
    b = _walk_nb(points, last, 1, scale, lo, hi)
    n = b - a + 1
    arc = np.empty(n, dtype=np.float64)
    arc[0] = 0.0
    for k in range(1, n):
        dx = points[a + k, 0] - points[a + k - 1, 0]
        dy = points[a + k, 1] - points[a + k - 1, 1]
        arc[k] = arc[k - 1] + math.sqrt(dx * dx + dy * dy)
    sigma = max(scale * 0.5, 1e-6)
    inverse = 0.5 / (sigma * sigma)
    left_point = 0
    left_segment = 0
    for i in range(last - first + 1):
        centre = arc[first - a + i]
        while arc[left_point] < centre - scale:
            left_point += 1
        weight_sum = 0.0
        px = 0.0
        py = 0.0
        p = left_point
        while p < n and arc[p] <= centre + scale:
            d = arc[p] - centre
            w = math.exp(-d * d * inverse)
            weight_sum += w
            px += w * points[a + p, 0]
            py += w * points[a + p, 1]
            p += 1
        sx[i] = px / weight_sum
        sy[i] = py / weight_sum
        while left_segment < n - 1 and (arc[left_segment] + arc[left_segment + 1]) * 0.5 < centre - scale:
            left_segment += 1
        vx = 0.0
        vy = 0.0
        q = left_segment
        while q < n - 1 and (arc[q] + arc[q + 1]) * 0.5 <= centre + scale:
            d = (arc[q] + arc[q + 1]) * 0.5 - centre
            w = math.exp(-d * d * inverse)
            vx += w * (points[a + q + 1, 0] - points[a + q, 0])
            vy += w * (points[a + q + 1, 1] - points[a + q, 1])
            q += 1
        if vx == 0.0 and vy == 0.0:
            index = first + i
            vx, vy, _ = _chord_direction_nb(points, max(a, index - 1), min(b, index + 1))
        tx[i] = vx
        ty[i] = vy


@njit(cache=True, nogil=True)
def _persistent_extrema_nb(values: np.ndarray, prominence: float,
                           out_index: np.ndarray, out_sign: np.ndarray) -> int:
    """Interior extrema of ``values`` that survive persistence simplification.

    Local maxima (+1) and minima (-1) are found with plateaus reduced to their
    middle sample. The two ends are fixed anchors. Repeatedly, the adjacent
    pair with the smallest value difference below ``prominence`` is removed:
    two interior extrema together (a noise wiggle), or one interior extremum
    beside an end. Survivors are written to ``out_index``/``out_sign`` in order;
    returns their count.
    """
    n = values.shape[0]
    if n < 3:
        return 0
    index = np.empty(n + 2, dtype=np.int64)
    value = np.empty(n + 2, dtype=np.float64)
    sign = np.zeros(n + 2, dtype=np.int64)
    count = 1
    index[0] = 0
    value[0] = values[0]
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[j + 1] == values[i]:
            j += 1
        if i > 0 and j < n - 1:
            before = values[i - 1]
            after = values[j + 1]
            v = values[i]
            if before < v and after < v:
                index[count] = (i + j) // 2
                value[count] = v
                sign[count] = 1
                count += 1
            elif before > v and after > v:
                index[count] = (i + j) // 2
                value[count] = v
                sign[count] = -1
                count += 1
        i = j + 1
    index[count] = n - 1
    value[count] = values[n - 1]
    count += 1
    alive = np.ones(count, dtype=np.bool_)
    while True:
        best = math.inf
        best_a = -1
        best_b = -1
        a = 0
        while True:
            b = a + 1
            while b < count and not alive[b]:
                b += 1
            if b >= count:
                break
            if sign[a] != 0 or sign[b] != 0:
                difference = abs(value[a] - value[b])
                if difference < best:
                    best = difference
                    best_a = a
                    best_b = b
            a = b
        if best_a < 0 or best >= prominence:
            break
        if sign[best_a] != 0:
            alive[best_a] = False
        if sign[best_b] != 0:
            alive[best_b] = False
    found = 0
    for k in range(1, count - 1):
        if alive[k]:
            out_index[found] = index[k]
            out_sign[found] = sign[k]
            found += 1
    return found


@njit(cache=True, nogil=True)
def _bulge_nb(sx: np.ndarray, sy: np.ndarray, a: int, b: int) -> float:
    """Largest distance of smoothed samples ``a..b`` from their chord."""
    vx = sx[b] - sx[a]
    vy = sy[b] - sy[a]
    length = math.sqrt(vx * vx + vy * vy)
    worst = 0.0
    for k in range(a, b + 1):
        dx = sx[k] - sx[a]
        dy = sy[k] - sy[a]
        distance = abs(vx * dy - vy * dx) / length if length > 1e-12 else math.sqrt(dx * dx + dy * dy)
        if distance > worst:
            worst = distance
    return worst


@njit(cache=True, nogil=True)
def _inflections_nb(theta: np.ndarray, sx: np.ndarray, sy: np.ndarray,
                    turn_floor: float, bulge_floor: float, out_index: np.ndarray) -> int:
    """Inflections: extrema of the smoothed tangent angle ``theta`` whose lobes matter.

    The lobes between consecutive angle extrema (and the unit ends) are merged,
    least significant first, while a lobe turns by less than ``turn_floor`` or
    bulges less than ``bulge_floor`` from its chord: two interior extrema
    bounding such a lobe are dropped together, and an extremum bounding one at
    a unit end is dropped alone. Survivors are written in order; returns their
    count.
    """
    n = theta.shape[0]
    if n < 3:
        return 0
    index = np.empty(n + 2, dtype=np.int64)
    interior = np.zeros(n + 2, dtype=np.bool_)
    count = 1
    index[0] = 0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and theta[j + 1] == theta[i]:
            j += 1
        if i > 0 and j < n - 1:
            before = theta[i - 1]
            after = theta[j + 1]
            v = theta[i]
            if (before < v and after < v) or (before > v and after > v):
                index[count] = (i + j) // 2
                interior[count] = True
                count += 1
        i = j + 1
    index[count] = n - 1
    count += 1
    following = np.empty(count, dtype=np.int64)
    preceding = np.empty(count, dtype=np.int64)
    cost = np.full(count, math.inf, dtype=np.float64)
    for k in range(count):
        following[k] = k + 1
        preceding[k] = k - 1

    def significance(a, b):
        turn = abs(theta[index[b]] - theta[index[a]]) / turn_floor
        bulge = _bulge_nb(sx, sy, index[a], index[b]) / bulge_floor
        return turn if turn < bulge else bulge

    for k in range(count - 1):
        if interior[k] or interior[k + 1]:
            cost[k] = significance(k, k + 1)
    while True:
        best = math.inf
        a = -1
        k = 0
        while k < count - 1:
            if cost[k] < best:
                best = cost[k]
                a = k
            k = following[k]
        if a < 0 or best >= 1.0:
            break
        b = following[a]
        if interior[a] and interior[b]:
            left = preceding[a]
            right = following[b]
        elif interior[b]:
            left = a
            right = following[b]
        else:
            left = preceding[a]
            right = b
        # Unlink everything strictly between ``left`` and ``right``.
        cursor = following[left]
        while cursor != right:
            cost[cursor] = math.inf
            cursor = following[cursor]
        following[left] = right
        preceding[right] = left
        cost[left] = significance(left, right) if (interior[left] or interior[right]) else math.inf
    found = 0
    k = following[0]
    while k < count - 1:
        out_index[found] = index[k]
        found += 1
        k = following[k]
    return found


@njit(cache=True, nogil=True)
def _unit_candidates_nb(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                        scale: float, bends: bool, tolerance: float, theta: np.ndarray,
                        c_index: np.ndarray, c_tx: np.ndarray, c_ty: np.ndarray,
                        c_kind: np.ndarray) -> int:
    """Candidate nodes strictly inside the curve unit ``first..last``.

    On the samples smoothed at ``scale`` (``_smooth_unit_nb``), with the floors
    above: axis extremes of the smoothed x and y, each with its tangent exactly
    vertical or horizontal; inflections, with the total-least-squares tangent of
    the raw samples within ``scale``, significant when ``_inflections_nb`` keeps
    them (``tolerance`` sets the bulge floor); and, with ``bends``, points of
    tightest bend (curvature peaks) with the smoothed tangent. ``theta`` receives the unwrapped smoothed tangent angle
    of every unit sample. Returns the number of candidates written (unsorted).
    """
    m = last - first + 1
    sx = np.empty(m, dtype=np.float64)
    sy = np.empty(m, dtype=np.float64)
    tx = np.empty(m, dtype=np.float64)
    ty = np.empty(m, dtype=np.float64)
    _smooth_unit_nb(points, first, last, lo, hi, scale, sx, sy, tx, ty)
    for i in range(m):
        angle = math.atan2(ty[i], tx[i])
        if i > 0:
            while angle - theta[i - 1] > math.pi:
                angle -= 2.0 * math.pi
            while angle - theta[i - 1] < -math.pi:
                angle += 2.0 * math.pi
        theta[i] = angle
    floor = max(_NOISE_FLOOR, 0.5 * tolerance)
    extreme_floor = max(_NOISE_FLOOR, scale * _EXTREME_PROMINENCE)
    found_index = np.empty(m + 2, dtype=np.int64)
    found_sign = np.empty(m + 2, dtype=np.int64)
    count = 0
    for axis in range(2):
        values = sx if axis == 0 else sy
        k = _persistent_extrema_nb(values, extreme_floor, found_index, found_sign)
        for e in range(k):
            local = found_index[e]
            c_index[count] = first + local
            if axis == 0:
                # An x extreme: the tangent is exactly vertical.
                c_tx[count] = 0.0
                c_ty[count] = 1.0 if ty[local] >= 0.0 else -1.0
            else:
                c_tx[count] = 1.0 if tx[local] >= 0.0 else -1.0
                c_ty[count] = 0.0
            c_kind[count] = _KIND_EXTREME
            count += 1
    significant = np.empty(m + 2, dtype=np.int64)
    strong = _inflections_nb(theta, sx, sy, _LOBE_TURN, floor, significant)
    k = _persistent_extrema_nb(theta, _LOBE_TURN, found_index, found_sign)
    # Every angle extremum turning by the lobe floor, marked significant when
    # the bulge test also keeps it; a significant one the plain angle test
    # merged away is added too.
    merged = np.empty(k + strong, dtype=np.int64)
    merged_kind = np.empty(k + strong, dtype=np.int64)
    total = 0
    a = 0
    b = 0
    while a < k or b < strong:
        if b >= strong or (a < k and found_index[a] < significant[b]):
            merged[total] = found_index[a]
            merged_kind[total] = _KIND_MINOR_INFLECTION
            a += 1
        elif a >= k or significant[b] < found_index[a]:
            merged[total] = significant[b]
            merged_kind[total] = _KIND_INFLECTION
            b += 1
        else:
            merged[total] = found_index[a]
            merged_kind[total] = _KIND_INFLECTION
            a += 1
            b += 1
        total += 1
    for e in range(total):
        local = merged[e]
        index = first + local
        near = _walk_nb(points, index, -1, scale, lo, hi)
        far = _walk_nb(points, index, 1, scale, lo, hi)
        dx, dy, ok = _tls_direction_nb(points, near, far)
        norm = math.sqrt(tx[local] * tx[local] + ty[local] * ty[local])
        if not ok:
            dx = tx[local] / norm
            dy = ty[local] / norm
        elif dx * tx[local] + dy * ty[local] < 0.0:
            dx = -dx
            dy = -dy
        c_index[count] = index
        c_tx[count] = dx
        c_ty[count] = dy
        c_kind[count] = merged_kind[e]
        count += 1
    if bends and m >= 3:
        curvature = np.empty(m, dtype=np.float64)
        arc = np.empty(m, dtype=np.float64)
        arc[0] = 0.0
        for i in range(1, m):
            dx = points[first + i, 0] - points[first + i - 1, 0]
            dy = points[first + i, 1] - points[first + i - 1, 1]
            arc[i] = arc[i - 1] + math.sqrt(dx * dx + dy * dy)
        half = scale * 0.5
        low = 0
        high = 0
        for i in range(m):
            while arc[i] - arc[low] > half:
                low += 1
            if high < i:
                high = i
            while high + 1 < m and arc[high + 1] - arc[i] <= half:
                high += 1
            span = arc[high] - arc[low]
            curvature[i] = (theta[high] - theta[low]) / span if span > 0.0 else 0.0
        k = _persistent_extrema_nb(curvature, 1.0 / (_BEND_PROMINENCE_SCALES * scale),
                                   found_index, found_sign)
        for e in range(k):
            local = found_index[e]
            sign = found_sign[e]
            if (sign > 0) != (curvature[local] > 0.0) or curvature[local] == 0.0:
                continue
            # The bend's region runs between the gentlest curvature on either side.
            left = found_index[e - 1] if e > 0 else 0
            right = found_index[e + 1] if e + 1 < k else m - 1
            turn = (theta[right] - theta[left]) * sign
            minor = turn < 2.0 * _LOBE_TURN or _bulge_nb(sx, sy, left, right) < floor
            norm = math.sqrt(tx[local] * tx[local] + ty[local] * ty[local])
            c_index[count] = first + local
            c_tx[count] = tx[local] / norm
            c_ty[count] = ty[local] / norm
            c_kind[count] = _KIND_MINOR_BEND if minor else _KIND_BEND
            count += 1
    return count


@njit(cache=True, nogil=True)
def _range_tables_nb(points: np.ndarray, first: int, last: int):
    """Cumulative arc length and range-extreme tables of samples ``first..last``.

    ``arc[k]`` is the arc length from ``first`` to ``first + k``. ``table[level, c,
    k]`` is the minimum x (c 0), maximum x (1), minimum y (2) or maximum y (3) of
    the ``2 ** level`` samples from ``first + k``, so ``_range_box_nb`` answers
    any range's bounding box in constant time.
    """
    m = last - first + 1
    arc = np.empty(m, dtype=np.float64)
    arc[0] = 0.0
    for k in range(1, m):
        dx = points[first + k, 0] - points[first + k - 1, 0]
        dy = points[first + k, 1] - points[first + k - 1, 1]
        arc[k] = arc[k - 1] + math.sqrt(dx * dx + dy * dy)
    levels = 1
    while (1 << levels) <= m:
        levels += 1
    table = np.empty((levels, 4, m), dtype=np.float64)
    for k in range(m):
        table[0, 0, k] = points[first + k, 0]
        table[0, 1, k] = points[first + k, 0]
        table[0, 2, k] = points[first + k, 1]
        table[0, 3, k] = points[first + k, 1]
    for level in range(1, levels):
        half = 1 << (level - 1)
        for k in range(m - (1 << level) + 1):
            table[level, 0, k] = min(table[level - 1, 0, k], table[level - 1, 0, k + half])
            table[level, 1, k] = max(table[level - 1, 1, k], table[level - 1, 1, k + half])
            table[level, 2, k] = min(table[level - 1, 2, k], table[level - 1, 2, k + half])
            table[level, 3, k] = max(table[level - 1, 3, k], table[level - 1, 3, k + half])
    return arc, table


@njit(cache=True, nogil=True)
def _range_box_nb(table: np.ndarray, low: int, high: int):
    """Bounding box ``(minx, maxx, miny, maxy)`` of table positions ``low..high``."""
    level = 0
    while (2 << level) <= high - low + 1:
        level += 1
    other = high - (1 << level) + 1
    return (min(table[level, 0, low], table[level, 0, other]),
            max(table[level, 1, low], table[level, 1, other]),
            min(table[level, 2, low], table[level, 2, other]),
            max(table[level, 3, low], table[level, 3, other]))


@njit(cache=True, nogil=True)
def _fixed_pair_nb(points: np.ndarray, start: int, end: int,
                   stx: float, sty: float, etx: float, ety: float,
                   tolerance: float, pad_cap: float, window: float, refine: bool, t: np.ndarray,
                   arc: np.ndarray, table: np.ndarray, base: int):
    """Cubic through samples ``start..end`` with both end tangents fixed, handles
    inside their tangent triangle, and its max sample error.

    Where the tangents turn the same way (a C-shaped piece) the two tangent
    rays meet at the triangle's apex, and each handle is at most as long as its
    ray to the apex; tangents that turn by 180 degrees or more (parallel rays,
    or an apex behind a node) cannot be one hand-drawn piece and return
    ``ok = False``. An S-shaped piece (tangents on opposite sides of the chord)
    has no triangle, and neither has a tangent along the chord (within 1e-12 of
    it). Handle lengths are Schneider's least-squares values, kept
    between ``_MIN_HANDLE`` of the chord and the chord, within the triangle and
    inside the padded sample box; a length clamped to a bound re-solves the
    other. ``clamps`` counts the handles held at a bound (or given the
    third-of-chord fallback), the handle-quality measure node selection uses.

    Samples start at their cumulative-chord parameters, read from ``arc`` (and
    the sample box from ``table``; both from ``_range_tables_nb`` of the samples
    from ``base``). With ``refine``, a fit
    missing ``tolerance`` by less than ``window`` times is refitted at up to four
    Newton-improved parameterisations (Schneider), each step computed in the
    same pass as the previous fit's error, keeping the best attempt. The error
    pass stops early once the error reaches ``window * tolerance`` (the first
    fit) or the best error so far (a refit), so a returned error at or above
    those is only a lower bound. Returns ``(ok, clamps, error, c1x, c1y, c2x,
    c2y, worst)``, ``worst`` being the offset of the worst sample seen.
    """
    m = end - start + 1
    p0x = points[start, 0]
    p0y = points[start, 1]
    p3x = points[end, 0]
    p3y = points[end, 1]
    ux = p3x - p0x
    uy = p3y - p0y
    chord = math.sqrt(ux * ux + uy * uy)
    if chord <= 0.0:
        return False, 0, math.inf, p0x, p0y, p3x, p3y, m // 2
    a_max = chord
    b_max = chord
    side_start = ux * sty - uy * stx
    side_end = ux * ety - uy * etx
    # A tangent along the chord is on neither side: rounding must not give it
    # one, or its triangle's apex falls on the far node.
    if abs(side_start) < 1e-12 * chord:
        side_start = 0.0
    if abs(side_end) < 1e-12 * chord:
        side_end = 0.0
    if side_start * side_end < 0.0:
        det = stx * ety - sty * etx
        if abs(det) < 1e-9:
            return False, 0, math.inf, p0x, p0y, p3x, p3y, m // 2
        apex_a = (ux * ety - uy * etx) / det
        apex_b = (stx * uy - sty * ux) / det
        if apex_a <= 0.0 or apex_b <= 0.0:
            return False, 0, math.inf, p0x, p0y, p3x, p3y, m // 2
        a_max = min(a_max, apex_a)
        b_max = min(b_max, apex_b)
    origin = arc[start - base]
    total = arc[end - base] - origin
    scale = 1.0 / total if total != 0.0 else 0.0
    for i in range(m):
        t[i] = (arc[start - base + i] - origin) * scale
    minx, maxx, miny, maxy = _range_box_nb(table, start - base, end - base)
    inner = min(max(maxx - minx, maxy - miny) * 0.25, pad_cap)
    padding = tolerance if tolerance > inner else inner
    a_max = min(a_max, _ray_limit_nb(p0x, p0y, stx, sty, minx - padding, maxx + padding,
                                     miny - padding, maxy + padding))
    b_max = min(b_max, _ray_limit_nb(p3x, p3y, -etx, -ety, minx - padding, maxx + padding,
                                     miny - padding, maxy + padding))
    a_min = min(_MIN_HANDLE * chord, a_max)
    b_min = min(_MIN_HANDLE * chord, b_max)
    turn_dot = stx * etx + sty * ety
    cap = window * tolerance
    best_error = math.inf
    best_worst = 0
    best_clamps = 0
    b1x = p0x
    b1y = p0y
    b2x = p3x
    b2y = p3y
    iteration = 0
    while True:
        s11 = 0.0
        s12 = 0.0
        s22 = 0.0
        ra = 0.0
        rb = 0.0
        for i in range(m):
            ti = t[i]
            omt = 1.0 - ti
            b0 = omt * omt * omt
            b1 = 3.0 * omt * omt * ti
            b2 = 3.0 * omt * ti * ti
            b3 = ti * ti * ti
            s11 += b1 * b1
            s12 += b1 * b2
            s22 += b2 * b2
            rx = points[start + i, 0] - (b0 + b1) * p0x - (b2 + b3) * p3x
            ry = points[start + i, 1] - (b0 + b1) * p0y - (b2 + b3) * p3y
            ra += b1 * (stx * rx + sty * ry)
            rb += b2 * (etx * rx + ety * ry)
        off = -s12 * turn_dot
        det = s11 * s22 - off * off
        a = chord / 3.0
        b = chord / 3.0
        clamps = 0
        solved = False
        if det > 1e-9 * s11 * s22:
            a_fit = (ra * s22 - off * -rb) / det
            b_fit = (s11 * -rb - off * ra) / det
            if a_fit > 1e-6 * chord and b_fit > 1e-6 * chord:
                a = a_fit
                b = b_fit
                solved = True
        if not solved:
            clamps += 1
        if a < a_min or a > a_max:
            a = min(max(a, a_min), a_max)
            clamps += 1
            if solved and s22 > 1e-30:
                b = (-rb - off * a) / s22
        if b < b_min or b > b_max:
            b = min(max(b, b_min), b_max)
            clamps += 1
            if solved and s11 > 1e-30:
                a = min(max((ra - off * b) / s11, a_min), a_max)
        c1x = p0x + a * stx
        c1y = p0y + a * sty
        c2x = p3x - b * etx
        c2y = p3y - b * ety
        # Error of this fit, and the Newton step of every interior parameter
        # towards its sample's nearest curve point for the next attempt.
        step = refine and m >= 4 and iteration < 4
        limit = cap if iteration == 0 else best_error
        error = -1.0
        worst = 0
        stopped = False
        for i in range(m):
            ti = t[i]
            omt = 1.0 - ti
            bx = (omt * omt * omt * p0x + 3.0 * omt * omt * ti * c1x
                  + 3.0 * omt * ti * ti * c2x + ti * ti * ti * p3x)
            by = (omt * omt * omt * p0y + 3.0 * omt * omt * ti * c1y
                  + 3.0 * omt * ti * ti * c2y + ti * ti * ti * p3y)
            ex = bx - points[start + i, 0]
            ey = by - points[start + i, 1]
            distance = math.sqrt(ex * ex + ey * ey)
            if distance > error:
                error = distance
                worst = i
                if error >= limit:
                    stopped = True
                    break
            if step and 0 < i < m - 1:
                d1x = 3.0 * (omt * omt * (c1x - p0x) + 2.0 * omt * ti * (c2x - c1x)
                             + ti * ti * (p3x - c2x))
                d1y = 3.0 * (omt * omt * (c1y - p0y) + 2.0 * omt * ti * (c2y - c1y)
                             + ti * ti * (p3y - c2y))
                d2x = 6.0 * (omt * (c2x - 2.0 * c1x + p0x) + ti * (p3x - 2.0 * c2x + c1x))
                d2y = 6.0 * (omt * (c2y - 2.0 * c1y + p0y) + ti * (p3y - 2.0 * c2y + c1y))
                numerator = ex * d1x + ey * d1y
                denominator = d1x * d1x + d1y * d1y + ex * d2x + ey * d2y
                if abs(denominator) > 1e-12:
                    ti = ti - numerator / denominator
                    if ti < 0.0:
                        ti = 0.0
                    elif ti > 1.0:
                        ti = 1.0
                    t[i] = ti
        if iteration > 0 and stopped:
            break
        best_error = error
        best_worst = worst
        best_clamps = clamps
        b1x = c1x
        b1y = c1y
        b2x = c2x
        b2y = c2y
        if stopped or not step or error <= tolerance:
            break
        iteration += 1
    return True, best_clamps, best_error, b1x, b1y, b2x, b2y, best_worst


@njit(cache=True, nogil=True)
def _max_error_nb(points: np.ndarray, start: int, n: int, fitted: np.ndarray):
    worst = -1.0
    at = 0
    for i in range(n):
        dx = fitted[i, 0] - points[start + i, 0]
        dy = fitted[i, 1] - points[start + i, 1]
        error = math.sqrt(dx * dx + dy * dy)
        if error > worst:
            worst = error
            at = i
    return worst, at


@njit(cache=True, nogil=True)
def _legs_cross_nb(p0x: float, p0y: float, c1x: float, c1y: float,
                   c2x: float, c2y: float, p3x: float, p3y: float) -> bool:
    """Whether the two handle legs ``P0-C1`` and ``C2-P3`` properly cross."""
    d1 = (c1x - p0x) * (c2y - p0y) - (c1y - p0y) * (c2x - p0x)
    d2 = (c1x - p0x) * (p3y - p0y) - (c1y - p0y) * (p3x - p0x)
    d3 = (p3x - c2x) * (p0y - c2y) - (p3y - c2y) * (p0x - c2x)
    d4 = (p3x - c2x) * (c1y - c2y) - (p3y - c2y) * (c1x - c2x)
    return d1 * d2 < 0.0 and d3 * d4 < 0.0


@njit(cache=True, nogil=True)
def _pair_fit_nb(points: np.ndarray, start: int, end: int,
                 s_fixed: bool, stx: float, sty: float,
                 e_fixed: bool, etx: float, ety: float,
                 tolerance: float, pad_cap: float, window: float, refine: bool,
                 t: np.ndarray, fitted: np.ndarray,
                 arc: np.ndarray, table: np.ndarray, base: int):
    """One piece from node ``start`` to node ``end`` and whether it is acceptable.

    Two free ends straight within ``tolerance`` (or adjacent) give a line.
    Otherwise a cubic: with both tangents fixed, ``_fixed_pair_nb`` (handles
    inside the tangent triangle); with a free end, ``_constrained_cubic_nb``.
    With ``refine``, a fit missing the tolerance by less than ``window`` times is
    refitted at up to four Newton-improved parameterisations. The piece is
    acceptable when every sample is within ``tolerance`` and it never turns
    back along its chord; a two-sample piece (no interior sample, so it cannot
    be split) is always acceptable. ``quality`` counts handle defects (handles
    held at a bound, crossing legs, a two-sample piece that turns back).
    Returns ``(valid, quality, error, is_line, c1x, c1y, c2x, c2y, worst)``,
    ``worst`` being the offset of the worst-fitting sample; an error of
    ``window * tolerance`` or more may be a lower bound.
    """
    m = end - start + 1
    p0x = points[start, 0]
    p0y = points[start, 1]
    p3x = points[end, 0]
    p3y = points[end, 1]
    if not s_fixed and not e_fixed:
        if m <= 2:
            return True, 0.0, 0.0, True, p0x, p0y, p3x, p3y, 0
        line_error, line_split = _distance_to_line_nb(points, start, end)
        if line_error <= tolerance:
            return True, 0.0, line_error, True, p0x, p0y, p3x, p3y, line_split
    if m <= 2:
        c1x, c1y, c2x, c2y = _constrained_cubic_nb(
            points, start, end, tolerance, pad_cap, s_fixed, stx, sty, e_fixed, etx, ety,
            t[:m], fitted[:m], False)
        quality = 0.0 if _chord_monotone_nb(p0x, p0y, c1x, c1y, c2x, c2y, p3x, p3y) else 4.0
        return True, quality, 0.0, False, c1x, c1y, c2x, c2y, 0
    clamps = 0
    if s_fixed and e_fixed:
        ok, clamps, error, c1x, c1y, c2x, c2y, worst = _fixed_pair_nb(
            points, start, end, stx, sty, etx, ety, tolerance, pad_cap, window, refine, t[:m],
            arc, table, base)
        if not ok:
            # No hand-drawn piece spans it; the unbounded fit still locates the
            # worst sample for a fallback split.
            _constrained_cubic_nb(points, start, end, tolerance, pad_cap, True, stx, sty, True,
                                  etx, ety, t[:m], fitted[:m], False)
            _, worst = _max_error_nb(points, start, m, fitted)
            return False, 0.0, math.inf, False, c1x, c1y, c2x, c2y, worst
    else:
        c1x, c1y, c2x, c2y = _constrained_cubic_nb(
            points, start, end, tolerance, pad_cap, s_fixed, stx, sty, e_fixed, etx, ety,
            t[:m], fitted[:m], False)
        error, worst = _max_error_nb(points, start, m, fitted)
        attempt = 0
        while (refine and m >= 4 and tolerance < error < window * tolerance
               and attempt < 4):
            attempt += 1
            _reparameterize_nb(points, start, end, c1x, c1y, c2x, c2y, t[:m])
            n1x, n1y, n2x, n2y = _constrained_cubic_nb(
                points, start, end, tolerance, pad_cap, s_fixed, stx, sty, e_fixed, etx, ety,
                t[:m], fitted[:m], True)
            new_error, new_worst = _max_error_nb(points, start, m, fitted)
            if new_error >= error:
                break
            c1x, c1y, c2x, c2y = n1x, n1y, n2x, n2y
            error = new_error
            worst = new_worst
    valid = error <= tolerance and _chord_monotone_nb(p0x, p0y, c1x, c1y, c2x, c2y, p3x, p3y)
    quality = float(clamps)
    if _legs_cross_nb(p0x, p0y, c1x, c1y, c2x, c2y, p3x, p3y):
        quality += 1.0
    return valid, quality, error, False, c1x, c1y, c2x, c2y, worst


@njit(cache=True, nogil=True)
def _kind_penalty_nb(kind: int, mode: int) -> float:
    """Tie-break cost of a node of ``kind``: extremes, then inflections, then bends."""
    if mode == _PLACEMENT_FEWEST or kind == _KIND_EXTREME or kind == _KIND_END:
        return 0.0
    if kind == _KIND_INFLECTION:
        return 1.0
    if kind == _KIND_BEND:
        return 2.0
    if kind == _KIND_SPLIT or kind == _KIND_MINOR_INFLECTION or kind == _KIND_MINOR_BEND:
        return 3.0
    return 4.0


@njit(cache=True, nogil=True)
def _better_nb(segments: int, quality: float, kind: float, error: float,
               best_segments: int, best_quality: float, best_kind: float,
               best_error: float) -> bool:
    """Lexicographic route order: segments, handle quality, node kinds, error."""
    if segments != best_segments:
        return segments < best_segments
    if abs(quality - best_quality) > 1e-9:
        return quality < best_quality
    if abs(kind - best_kind) > 1e-9:
        return kind < best_kind
    return error < best_error - 1e-12


@njit(cache=True, nogil=True)
def _greedy_split_nodes_nb(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                           first_fixed: bool, ftx: float, fty: float,
                           last_fixed: bool, ltx: float, lty: float,
                           tolerance: float, pad_cap: float, small: float, large: float, newton: float,
                           arc: np.ndarray, table: np.ndarray, base: int,
                           ranges: np.ndarray, tangents: np.ndarray,
                           t: np.ndarray, fitted: np.ndarray,
                           out_index: np.ndarray, out_tx: np.ndarray,
                           out_ty: np.ndarray) -> int:
    """The split points (sample index and shared tangent) of the worst-sample
    recursion over ``first..last``: a range whose piece ``_pair_fit_nb`` accepts
    is kept, else it splits at its worst-fitting sample with the tangent
    ``_best_split_tangent_nb`` picks. Its pieces always form an acceptable route
    (two adjacent samples always do). Returns the number of points written.
    """
    ranges[0, 0] = first
    ranges[0, 1] = last
    tangents[0, 0] = 1.0 if first_fixed else 0.0
    tangents[0, 1] = ftx
    tangents[0, 2] = fty
    tangents[0, 3] = 1.0 if last_fixed else 0.0
    tangents[0, 4] = ltx
    tangents[0, 5] = lty
    top = 1
    found = 0
    while top > 0:
        top -= 1
        start = ranges[top, 0]
        end = ranges[top, 1]
        s_fixed = tangents[top, 0] != 0.0
        stx = tangents[top, 1]
        sty = tangents[top, 2]
        e_fixed = tangents[top, 3] != 0.0
        etx = tangents[top, 4]
        ety = tangents[top, 5]
        m = end - start + 1
        if m <= 2:
            continue
        valid, _, _, _, _, _, _, _, split = _pair_fit_nb(
            points, start, end, s_fixed, stx, sty, e_fixed, etx, ety, tolerance, pad_cap, newton,
            True, t, fitted, arc, table, base)
        if valid:
            continue
        if not (0 < split < m - 1):
            _, split = _distance_to_line_nb(points, start, end)
        if not (0 < split < m - 1):
            split = m // 2
        index = start + split
        in_tx, in_ty = _join_nb(points, index, lo, hi, False, 0.0, 0.0, False, 0.0, 0.0, large)
        in_tx, in_ty = _best_split_tangent_nb(
            points, start, index, end, lo, hi, tolerance, pad_cap, s_fixed, stx, sty,
            e_fixed, etx, ety, in_tx, in_ty, small, large, t, fitted)
        out_index[found] = index
        out_tx[found] = in_tx
        out_ty[found] = in_ty
        found += 1
        ranges[top, 0] = index
        ranges[top, 1] = end
        tangents[top, 0] = 1.0
        tangents[top, 1] = in_tx
        tangents[top, 2] = in_ty
        tangents[top, 3] = 1.0 if e_fixed else 0.0
        tangents[top, 4] = etx
        tangents[top, 5] = ety
        top += 1
        ranges[top, 0] = start
        ranges[top, 1] = index
        tangents[top, 0] = 1.0 if s_fixed else 0.0
        tangents[top, 1] = stx
        tangents[top, 2] = sty
        tangents[top, 3] = 1.0
        tangents[top, 4] = in_tx
        tangents[top, 5] = in_ty
        top += 1
    return found


@njit(cache=True, nogil=True)
def _write_record_nb(records: np.ndarray, count: int, is_line: bool,
                     c1x: float, c1y: float, c2x: float, c2y: float,
                     ex: float, ey: float, kind: int, ntx: float, nty: float,
                     index: int) -> int:
    records[count, 0] = 0.0 if is_line else 1.0
    records[count, 1] = c1x
    records[count, 2] = c1y
    records[count, 3] = c2x
    records[count, 4] = c2y
    records[count, 5] = ex
    records[count, 6] = ey
    records[count, 7] = float(kind)
    records[count, 8] = ntx
    records[count, 9] = nty
    records[count, 10] = float(index)
    return count + 1


@njit(cache=True, nogil=True)
def _arrival_tangent_nb(sx: float, sy: float, is_line: bool, c2x: float, c2y: float,
                        ex: float, ey: float):
    """Forward unit tangent a piece arrives at its end with."""
    if not is_line:
        dx = ex - c2x
        dy = ey - c2y
        length = math.sqrt(dx * dx + dy * dy)
        if length > 0.0:
            return dx / length, dy / length
    dx = ex - sx
    dy = ey - sy
    length = math.sqrt(dx * dx + dy * dy)
    if length > 0.0:
        return dx / length, dy / length
    return 1.0, 0.0


@njit(cache=True, nogil=True)
def _place_nodes_nb(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                    first_fixed: bool, ftx: float, fty: float,
                    last_fixed: bool, ltx: float, lty: float,
                    tolerance: float, pad_cap: float, small: float, large: float,
                    mode: int, bends: bool, scale: float, streak: int,
                    prune_factor: float, newton: float, records: np.ndarray, count: int, ranges: np.ndarray,
                    tangents: np.ndarray, t: np.ndarray, fitted: np.ndarray) -> int:
    """Fit one curve unit through chosen candidate nodes, appending to ``records``.

    1. Candidates (``_unit_candidates_nb``): axis extremes, inflections and, with
       ``bends``, points of tightest bend, plus the unit's two ends with their
       given tangents (free at a cusp). "Fewest nodes only" (``mode`` 2) adds
       the split points of the worst-sample recursion.
    2. Every candidate has a fixed tangent, so the piece between two candidates
       is fitted independently (``_pair_fit_nb``). A shortest path over the
       acceptable pieces gives the fewest segments; ties go to fewer handle
       defects, then to preferred node kinds (extremes, inflections, bends; no
       preference in mode 2), then to the smaller summed error. With "Always at
       extremes" (``mode`` 1) no piece may skip an axis extreme.
    3. When no route reaches the unit's end, the gap after the furthest
       reachable candidate gets the split points of the worst-sample recursion
       across it (``_greedy_split_nodes_nb``), which always form an acceptable
       route that the recomputed path always may take, so each fallback makes a
       later sample reachable; every piece meets ``tolerance`` at its samples.

    The search extends pieces backwards from each candidate and stops after
    ``streak`` consecutive starts missing by more than ``prune_factor`` times the
    tolerance (0: no limit), or once the smoothed tangent turns by more than
    ``_TURN_PRUNE``. Pieces are first fitted at chord-length parameters;
    a near miss (within ``newton`` times the tolerance) is refitted with Newton
    steps only when its start could still tie or beat the best route.

    Returns the new record count.
    """
    m = last - first + 1
    if not first_fixed and not last_fixed:
        if m <= 2 or _distance_to_line_nb(points, first, last)[0] <= tolerance:
            ntx, nty = _arrival_tangent_nb(points[first, 0], points[first, 1], True, 0.0, 0.0,
                                           points[last, 0], points[last, 1])
            return _write_record_nb(records, count, True, 0.0, 0.0, 0.0, 0.0,
                                    points[last, 0], points[last, 1], _KIND_END,
                                    ntx, nty, last)
    if m <= 2:
        valid, quality, error, is_line, c1x, c1y, c2x, c2y, worst = _pair_fit_nb(
            points, first, last, first_fixed, ftx, fty, last_fixed, ltx, lty, tolerance, pad_cap, newton,
            True, t, fitted, np.zeros(1), np.zeros((1, 4, 1)), first)
        if last_fixed:
            ntx, nty = ltx, lty
        else:
            ntx, nty = _arrival_tangent_nb(points[first, 0], points[first, 1], is_line,
                                           c2x, c2y, points[last, 0], points[last, 1])
        return _write_record_nb(records, count, is_line, c1x, c1y, c2x, c2y,
                                points[last, 0], points[last, 1], _KIND_END, ntx, nty, last)

    arc, table = _range_tables_nb(points, first, last)
    theta = np.empty(m, dtype=np.float64)
    room = 4 * m + 8
    c_index = np.empty(room, dtype=np.int64)
    c_tx = np.empty(room, dtype=np.float64)
    c_ty = np.empty(room, dtype=np.float64)
    c_kind = np.empty(room, dtype=np.int64)
    found = _unit_candidates_nb(points, first, last, lo, hi, scale, bends, tolerance, theta,
                                c_index, c_tx, c_ty, c_kind)
    s_index = np.empty(m, dtype=np.int64)
    s_tx = np.empty(m, dtype=np.float64)
    s_ty = np.empty(m, dtype=np.float64)
    if mode == _PLACEMENT_FEWEST:
        splits = _greedy_split_nodes_nb(points, first, last, lo, hi, first_fixed, ftx, fty,
                                        last_fixed, ltx, lty, tolerance, pad_cap, small, large, newton,
                                        arc, table, first, ranges, tangents, t, fitted,
                                        s_index, s_tx, s_ty)
        for k in range(splits):
            c_index[found] = s_index[k]
            c_tx[found] = s_tx[k]
            c_ty[found] = s_ty[k]
            c_kind[found] = _KIND_SPLIT
            found += 1

    capacity = found + 18
    n_index = np.empty(capacity, dtype=np.int64)
    n_tx = np.empty(capacity, dtype=np.float64)
    n_ty = np.empty(capacity, dtype=np.float64)
    n_fixed = np.empty(capacity, dtype=np.bool_)
    n_kind = np.empty(capacity, dtype=np.int64)
    n_mandatory = np.empty(capacity, dtype=np.bool_)
    n_low = np.empty(capacity, dtype=np.int64)
    n_high = np.empty(capacity, dtype=np.int64)
    # A fallback chain's pieces: the node before each chain node (-1: none), and
    # that piece's handle quality and error.
    n_pred = np.full(capacity, -1, dtype=np.int64)
    n_pred_quality = np.zeros(capacity, dtype=np.float64)
    n_pred_error = np.zeros(capacity, dtype=np.float64)
    nodes = 0
    for k in range(-1, found + 1):
        if k == -1:
            index, dx, dy, fixed, kind = first, ftx, fty, first_fixed, _KIND_END
        elif k == found:
            index, dx, dy, fixed, kind = last, ltx, lty, last_fixed, _KIND_END
        else:
            index, dx, dy, fixed, kind = c_index[k], c_tx[k], c_ty[k], True, c_kind[k]
            if index <= first or index >= last:
                continue
            duplicate = False
            for other in range(1, nodes):
                if (n_index[other] == index and n_kind[other] == kind
                        and n_tx[other] == dx and n_ty[other] == dy):
                    duplicate = True
                    break
            if duplicate:
                continue
        n_index[nodes] = index
        n_tx[nodes] = dx
        n_ty[nodes] = dy
        n_fixed[nodes] = fixed
        n_kind[nodes] = kind
        n_mandatory[nodes] = mode == _PLACEMENT_AT_EXTREMES and kind == _KIND_EXTREME
        n_low[nodes] = first
        n_high[nodes] = last
        nodes += 1
    # Candidate order: by sample, a mandatory node before others at its sample.
    order = np.empty(capacity, dtype=np.int64)
    for k in range(nodes):
        position = k
        while position > 0:
            other = order[position - 1]
            later = (n_index[other] > n_index[k]) or (
                n_index[other] == n_index[k] and n_mandatory[k] and not n_mandatory[other])
            if not later:
                break
            order[position] = other
            position -= 1
        order[position] = k
    edge_state = np.zeros((capacity, capacity), dtype=np.int8)
    edge_quality = np.zeros((capacity, capacity), dtype=np.float64)
    edge_error = np.zeros((capacity, capacity), dtype=np.float64)
    tolerance_scale = 1.0 / tolerance if tolerance > 0.0 else 1.0

    # Each pass either reaches the unit's end or adds a fallback chain that makes
    # a node at a later sample reachable (see the fallback below), so there are
    # at most ``m - 1`` passes. A route to a node depends only on the nodes
    # before it in the order, so the routes to the nodes up to the fallback's
    # start ``r`` (``settled`` positions, which a fallback never reorders) are
    # kept from the previous pass rather than searched again.
    reach = np.full(nodes, _UNREACHED, dtype=np.int64)
    r_quality = np.zeros(nodes, dtype=np.float64)
    r_kind = np.zeros(nodes, dtype=np.float64)
    r_error = np.zeros(nodes, dtype=np.float64)
    back = np.full(nodes, -1, dtype=np.int64)
    reach[0] = 0
    settled = 1
    while True:
        kept_reach, kept_quality, kept_kind, kept_error, kept_back = (
            reach, r_quality, r_kind, r_error, back)
        reach = np.full(nodes, _UNREACHED, dtype=np.int64)
        r_quality = np.zeros(nodes, dtype=np.float64)
        r_kind = np.zeros(nodes, dtype=np.float64)
        r_error = np.zeros(nodes, dtype=np.float64)
        back = np.full(nodes, -1, dtype=np.int64)
        reach[:settled] = kept_reach[:settled]
        r_quality[:settled] = kept_quality[:settled]
        r_kind[:settled] = kept_kind[:settled]
        r_error[:settled] = kept_error[:settled]
        back[:settled] = kept_back[:settled]
        position_of = np.empty(nodes, dtype=np.int64)
        for jj in range(nodes):
            position_of[order[jj]] = jj
        for jj in range(settled, nodes):
            j = order[jj]
            j_index = n_index[j]
            theta_low = theta[j_index - first]
            theta_high = theta_low
            cursor = j_index
            penalty = 0.0 if jj == nodes - 1 else _kind_penalty_nb(n_kind[j], mode)
            misses = 0
            # Pass A, nearest start first: chord-parameter fits only. A near
            # miss (state 3) is refitted with Newton steps in pass B only if its
            # start could still tie or beat the best route found.
            stop = 0
            for ii in range(jj - 1, -1, -1):
                stop = ii
                i = order[ii]
                i_index = n_index[i]
                if i_index == j_index:
                    if n_mandatory[i]:
                        break
                    continue
                while cursor > i_index:
                    cursor -= 1
                    value = theta[cursor - first]
                    if value < theta_low:
                        theta_low = value
                    if value > theta_high:
                        theta_high = value
                if theta_high - theta_low > _TURN_PRUNE:
                    stop = ii + 1
                    break
                if reach[ii] < _UNREACHED and j_index <= n_high[i] and i_index >= n_low[j]:
                    state = edge_state[i, j]
                    if state == 0:
                        valid, quality, error, _, _, _, _, _, _ = _pair_fit_nb(
                            points, i_index, j_index, n_fixed[i], n_tx[i], n_ty[i],
                            n_fixed[j], n_tx[j], n_ty[j], tolerance, pad_cap, newton, False, t, fitted,
                            arc, table, first)
                        if valid:
                            state = 1
                        elif tolerance < error < newton * tolerance and j_index - i_index >= 3:
                            state = 3
                        else:
                            state = 2
                        edge_state[i, j] = state
                        edge_quality[i, j] = quality
                        edge_error[i, j] = error
                    if state != 1 and streak > 0 and edge_error[i, j] > prune_factor * tolerance:
                        misses += 1
                        if misses >= streak:
                            break
                    else:
                        misses = 0
                    if state == 1:
                        segments = reach[ii] + 1
                        quality = r_quality[ii] + edge_quality[i, j]
                        kind = r_kind[ii] + penalty
                        error = r_error[ii] + edge_error[i, j] * tolerance_scale
                        if back[jj] < 0 or _better_nb(segments, quality, kind, error, reach[jj],
                                                      r_quality[jj], r_kind[jj], r_error[jj]):
                            reach[jj] = segments
                            r_quality[jj] = quality
                            r_kind[jj] = kind
                            r_error[jj] = error
                            back[jj] = ii
                if n_mandatory[i]:
                    break
            # Pass B, furthest start first: Newton refits of the near misses
            # whose start can still tie or beat the best route.
            for ii in range(stop, jj):
                i = order[ii]
                if edge_state[i, j] != 3 or reach[ii] >= _UNREACHED:
                    continue
                # The refit's own quality and error are at least zero, so a start
                # whose route cannot beat the best even with a perfect piece is
                # skipped.
                if back[jj] >= 0 and not _better_nb(reach[ii] + 1, r_quality[ii],
                                                    r_kind[ii] + penalty, r_error[ii],
                                                    reach[jj], r_quality[jj], r_kind[jj],
                                                    r_error[jj]):
                    continue
                valid, quality, error, _, _, _, _, _, _ = _pair_fit_nb(
                    points, n_index[i], j_index, n_fixed[i], n_tx[i], n_ty[i],
                    n_fixed[j], n_tx[j], n_ty[j], tolerance, pad_cap, newton, True, t, fitted, arc, table, first)
                edge_state[i, j] = 1 if valid else 2
                edge_quality[i, j] = quality
                edge_error[i, j] = error
                if valid:
                    segments = reach[ii] + 1
                    quality = r_quality[ii] + quality
                    kind = r_kind[ii] + penalty
                    error = r_error[ii] + error * tolerance_scale
                    if back[jj] < 0 or _better_nb(segments, quality, kind, error, reach[jj],
                                                  r_quality[jj], r_kind[jj], r_error[jj]):
                        reach[jj] = segments
                        r_quality[jj] = quality
                        r_kind[jj] = kind
                        r_error[jj] = error
                        back[jj] = ii
            # Pass C: the fallback chain piece ending here, which the pruning
            # above may have skipped (see the fallback below).
            if n_pred[j] >= 0:
                ii = position_of[n_pred[j]]
                if reach[ii] < _UNREACHED:
                    segments = reach[ii] + 1
                    quality = r_quality[ii] + n_pred_quality[j]
                    kind = r_kind[ii] + penalty
                    error = r_error[ii] + n_pred_error[j] * tolerance_scale
                    if back[jj] < 0 or _better_nb(segments, quality, kind, error, reach[jj],
                                                  r_quality[jj], r_kind[jj], r_error[jj]):
                        reach[jj] = segments
                        r_quality[jj] = quality
                        r_kind[jj] = kind
                        r_error[jj] = error
                        back[jj] = ii
        if reach[nodes - 1] < _UNREACHED:
            break
        # Fallback: the gap after the furthest reachable candidate ``r`` up to
        # the next candidate sample ``b`` gets the split points of the
        # worst-sample recursion across it. Its pieces are each acceptable (the
        # recursion keeps a piece only when ``_pair_fit_nb`` accepts it, and a
        # two-sample piece always is), and they are recorded as the chain
        # ``r -> split -> ... -> b`` that pass C always relaxes, whatever the
        # search's pruning skips: its turn prune reads the smoothed tangent,
        # which on a ring narrower than the tolerance (a hairline sliver, a
        # loop a few pixels round) can turn by more than a full circle between
        # samples that one piece fits. The new nodes all lie after ``r`` in the
        # order and the routes up to ``r`` are kept (``settled``), so ``r``
        # stays reachable, ``b`` becomes reachable and the furthest reachable
        # sample strictly advances.
        rr = 0
        for jj in range(nodes):
            if reach[jj] < _UNREACHED:
                rr = jj
        r = order[rr]
        bb = rr + 1
        while n_index[order[bb]] == n_index[r]:
            bb += 1
        b = order[bb]
        settled = rr + 1
        start = n_index[r]
        end = n_index[b]
        added = _greedy_split_nodes_nb(points, start, end, lo, hi, n_fixed[r], n_tx[r], n_ty[r],
                                       n_fixed[b], n_tx[b], n_ty[b], tolerance, pad_cap, small, large,
                                       newton, arc, table, first, ranges, tangents, t, fitted,
                                       s_index, s_tx, s_ty)
        if nodes + added > capacity:
            grown = max(capacity * 2, nodes + added + 8)
            extra = grown - capacity
            n_index = np.concatenate((n_index, np.empty(extra, dtype=np.int64)))
            n_tx = np.concatenate((n_tx, np.empty(extra, dtype=np.float64)))
            n_ty = np.concatenate((n_ty, np.empty(extra, dtype=np.float64)))
            n_fixed = np.concatenate((n_fixed, np.empty(extra, dtype=np.bool_)))
            n_kind = np.concatenate((n_kind, np.empty(extra, dtype=np.int64)))
            n_mandatory = np.concatenate((n_mandatory, np.empty(extra, dtype=np.bool_)))
            n_low = np.concatenate((n_low, np.empty(extra, dtype=np.int64)))
            n_high = np.concatenate((n_high, np.empty(extra, dtype=np.int64)))
            n_pred = np.concatenate((n_pred, np.full(extra, -1, dtype=np.int64)))
            n_pred_quality = np.concatenate((n_pred_quality, np.zeros(extra, dtype=np.float64)))
            n_pred_error = np.concatenate((n_pred_error, np.zeros(extra, dtype=np.float64)))
            order = np.concatenate((order, np.empty(extra, dtype=np.int64)))
            state_grown = np.zeros((grown, grown), dtype=np.int8)
            quality_grown = np.zeros((grown, grown), dtype=np.float64)
            error_grown = np.zeros((grown, grown), dtype=np.float64)
            state_grown[:capacity, :capacity] = edge_state
            quality_grown[:capacity, :capacity] = edge_quality
            error_grown[:capacity, :capacity] = edge_error
            edge_state = state_grown
            edge_quality = quality_grown
            edge_error = error_grown
            capacity = grown
        for k in range(added):
            node = nodes
            n_index[node] = s_index[k]
            n_tx[node] = s_tx[k]
            n_ty[node] = s_ty[k]
            n_fixed[node] = True
            n_kind[node] = _KIND_FALLBACK
            n_mandatory[node] = False
            n_low[node] = first
            n_high[node] = last
            n_pred[node] = -1
            # Insert into the order after every node at or before its sample.
            position = nodes
            while position > 0 and n_index[order[position - 1]] > n_index[node]:
                order[position] = order[position - 1]
                position -= 1
            order[position] = node
            nodes += 1
        # The chain r -> splits (by sample) -> b; the splits sit in the order
        # after r and its same-sample siblings, and before b.
        previous = r
        for position in range(rr + 1, nodes):
            node = order[position]
            if node < nodes - added and node != b:
                continue
            _, quality, error, _, _, _, _, _, _ = _pair_fit_nb(
                points, n_index[previous], n_index[node], n_fixed[previous], n_tx[previous],
                n_ty[previous], n_fixed[node], n_tx[node], n_ty[node], tolerance, pad_cap,
                newton, True, t, fitted, arc, table, first)
            n_pred[node] = previous
            n_pred_quality[node] = quality
            n_pred_error[node] = error
            if node == b:
                break
            previous = node

    route = np.empty(nodes, dtype=np.int64)
    length = 0
    jj = nodes - 1
    while jj >= 0:
        route[length] = order[jj]
        length += 1
        jj = back[jj] if jj > 0 else -1
    for k in range(length - 1, 0, -1):
        i = route[k]
        j = route[k - 1]
        valid, quality, error, is_line, c1x, c1y, c2x, c2y, worst = _pair_fit_nb(
            points, n_index[i], n_index[j], n_fixed[i], n_tx[i], n_ty[i],
            n_fixed[j], n_tx[j], n_ty[j], tolerance, pad_cap, newton, True, t, fitted, arc, table, first)
        if n_fixed[j]:
            ntx, nty = n_tx[j], n_ty[j]
        else:
            ntx, nty = _arrival_tangent_nb(points[n_index[i], 0], points[n_index[i], 1],
                                           is_line, c2x, c2y,
                                           points[n_index[j], 0], points[n_index[j], 1])
        count = _write_record_nb(records, count, is_line, c1x, c1y, c2x, c2y,
                                 points[n_index[j], 0], points[n_index[j], 1],
                                 n_kind[j], ntx, nty, n_index[j])
    return count


@njit(cache=True, nogil=True)
def _snap_direction_nb(dx: float, dy: float, snap: int, max_angle: float):
    """The allowed direction nearest to ``(dx, dy)`` when within ``max_angle``.

    ``snap`` 1 allows horizontal and vertical, 2 also the two 45-degree
    diagonals. Returns ``(ok, vx, vy, nx, ny, difference)``: the direction
    oriented along ``(dx, dy)``, the line normal ``(nx, ny)`` in small
    integers (a line is ``nx * x + ny * y = c``) and the angle snapped away.
    """
    if snap == 0 or (dx == 0.0 and dy == 0.0):
        return False, 1.0, 0.0, 0.0, 1.0, math.inf
    folded = math.atan2(dy, dx) % math.pi
    choices = 2 if snap == 1 else 4
    best = math.inf
    pick = 0
    for k in range(choices):
        direction = k * (math.pi / choices)
        difference = abs(folded - direction)
        difference = min(difference, math.pi - difference)
        if difference < best:
            best = difference
            pick = k * (4 // choices)
    if best > max_angle:
        return False, 1.0, 0.0, 0.0, 1.0, best
    root = math.sqrt(0.5)
    if pick == 0:
        vx, vy, nx, ny = 1.0, 0.0, 0.0, 1.0
    elif pick == 1:
        vx, vy, nx, ny = root, root, 1.0, -1.0
    elif pick == 2:
        vx, vy, nx, ny = 0.0, 1.0, 1.0, 0.0
    else:
        vx, vy, nx, ny = -root, root, 1.0, 1.0
    if vx * dx + vy * dy < 0.0:
        vx = -vx
        vy = -vy
    return True, vx, vy, nx, ny, best


@njit(cache=True, nogil=True)
def _project_to_line_nb(px: float, py: float, nx: float, ny: float, c: float):
    """The point of the line ``nx * x + ny * y = c`` nearest ``(px, py)``; exact
    for horizontal and vertical lines."""
    if nx == 0.0:
        return px, c / ny
    if ny == 0.0:
        return c / nx, py
    shift = (nx * px + ny * py - c) / (nx * nx + ny * ny)
    return px - shift * nx, py - shift * ny


@njit(cache=True, nogil=True)
def _fit_section_nb(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                    cyclic: bool, first_fixed: bool, ftx: float, fty: float,
                    last_fixed: bool, ltx: float, lty: float,
                    tolerance: float, pad_cap: float, minimum: float, small: float, large: float,
                    mode: int, bends: bool, scale: float,
                    snap: int, snap_angle: float, straight_deviation: float,
                    streak: int, prune_factor: float, newton: float):
    """Fit samples ``first..last`` so that every node inside it is a symmetric node.

    ``first`` and ``last`` have free tangents (hard cusps) unless fixed by
    ``first_fixed``/``last_fixed`` to the forward unit tangents ``(ftx, fty)``/
    ``(ltx, lty)``. With ``cyclic`` they are the same seam sample of a closed
    ring, the seam is a symmetric node like any other, and a given
    ``first_fixed`` tangent is the seam's tangent where two curve runs meet
    there. Side windows never leave ``lo..hi``.

    1. The unconstrained recursive line/cubic split (line when the chord is
       within tolerance, else a cubic when within tolerance, else split at the
       worst sample) locates the straight pieces. A line at least ``minimum``
       long is a straight unit; everything else is curve evidence, and
       consecutive curve evidence forms one curve unit.
    2. Two straight units that meet (and a straight unit at a fixed end) become
       curve evidence too: a node between two lines cannot be symmetric, so the
       curve fit replaces the kink with one smooth run.
    3. With ``snap``, a straight unit within ``snap_angle`` (radians) of an
       allowed direction (``_snap_direction_nb``) becomes an exactly
       horizontal, vertical or 45-degree line: through its section end when it
       touches one (a cusp or forced node never moves), else through the mean
       of its samples. Its joins with neighbouring curve units move onto that
       line. The snap stands only when every sample of the unit lies within
       ``straight_deviation`` (``_straight_deviation``) of the segment written
       between those moved joins.
    4. Each join between a line and a curve takes the line's direction
       (``_join_nb``): the line stays straight and the curve's handle continues it.
       When the curve cannot leave or reach the line that way without turning
       back along its first or last piece (a sharp turn the corner detector
       did not confirm), that line becomes curve evidence as well and the
       section is refitted.
    5. Each curve unit is fitted with those end tangents through chosen
       candidate nodes (``_place_nodes_nb``). Only a unit with two free ends may
       become a line. Every curve-unit piece, and every straight unit that did
       not snap, is within ``tolerance`` of its samples; a snapped straight
       unit is within ``straight_deviation`` of them.

    Returns an ``(m, _RECORD_COLUMNS)`` record array: kind (0 line, 1 cubic),
    the two controls, the end point, the end node's kind (``_KIND_*``), its
    forward unit tangent and its sample index. ``_fit_records`` reads the first
    seven columns.
    """
    total = last - first + 1
    records = np.empty((total, _RECORD_COLUMNS), dtype=np.float64)
    count = 0
    t = np.empty(total, dtype=np.float64)
    fitted = np.empty((total, 2), dtype=np.float64)

    # 1. Unconstrained split: piece kinds and sample ranges.
    piece_kind = np.empty(total, dtype=np.int64)
    piece_start = np.empty(total, dtype=np.int64)
    piece_end = np.empty(total, dtype=np.int64)
    pieces = 0
    stack = np.empty((total + 1, 2), dtype=np.int64)
    stack[0, 0] = first
    stack[0, 1] = last
    top = 1
    while top > 0:
        top -= 1
        start = stack[top, 0]
        end = stack[top, 1]
        m = end - start + 1
        if m <= 2:
            piece_kind[pieces] = 0
            piece_start[pieces] = start
            piece_end[pieces] = end
            pieces += 1
            continue
        line_error, line_split = _distance_to_line_nb(points, start, end)
        if line_error <= tolerance:
            piece_kind[pieces] = 0
            piece_start[pieces] = start
            piece_end[pieces] = end
            pieces += 1
            continue
        _cubic_solve_nb(points, start, end, tolerance, pad_cap, t[:m], fitted[:m])
        max_error, split = _max_error_nb(points, start, m, fitted)
        if max_error <= tolerance:
            piece_kind[pieces] = 1
            piece_start[pieces] = start
            piece_end[pieces] = end
            pieces += 1
            continue
        if not (0 < split < m - 1):
            split = line_split
        if not (0 < split < m - 1):
            split = m // 2
        stack[top, 0] = start + split
        stack[top, 1] = end
        top += 1
        stack[top, 0] = start
        stack[top, 1] = start + split
        top += 1

    # Straight units (0) and curve evidence (1), one per unconstrained piece.
    base_kind = np.empty(pieces, dtype=np.int64)
    base_dir_x = np.zeros(pieces, dtype=np.float64)
    base_dir_y = np.zeros(pieces, dtype=np.float64)
    for i in range(pieces):
        base_kind[i] = 1
        if piece_kind[i] == 0:
            ux, uy, length = _chord_direction_nb(points, piece_start[i], piece_end[i])
            if length >= minimum:
                base_kind[i] = 0
                base_dir_x[i] = ux
                base_dir_y[i] = uy

    # 2. Straight units that meet each other or a fixed end become curve evidence.
    demote = np.zeros(pieces, dtype=np.bool_)
    for i in range(pieces - 1):
        if base_kind[i] == 0 and base_kind[i + 1] == 0:
            demote[i] = True
            demote[i + 1] = True
    if cyclic and base_kind[0] == 0 and base_kind[pieces - 1] == 0:
        demote[0] = True
        demote[pieces - 1] = True
    if first_fixed and not cyclic and base_kind[0] == 0:
        demote[0] = True
    if last_fixed and not cyclic and base_kind[pieces - 1] == 0:
        demote[pieces - 1] = True

    kind = np.empty(pieces, dtype=np.int64)
    unit_start = np.empty(pieces, dtype=np.int64)
    unit_end = np.empty(pieces, dtype=np.int64)
    unit_base = np.empty(pieces, dtype=np.int64)
    dir_x = np.zeros(pieces, dtype=np.float64)
    dir_y = np.zeros(pieces, dtype=np.float64)
    start_fixed = np.zeros(pieces, dtype=np.bool_)
    end_fixed = np.zeros(pieces, dtype=np.bool_)
    start_tx = np.zeros(pieces, dtype=np.float64)
    start_ty = np.zeros(pieces, dtype=np.float64)
    end_tx = np.zeros(pieces, dtype=np.float64)
    end_ty = np.zeros(pieces, dtype=np.float64)
    first_record = np.zeros(pieces, dtype=np.int64)
    last_record = np.zeros(pieces, dtype=np.int64)
    ranges = np.empty((total + 1, 2), dtype=np.int64)
    tangents = np.empty((total + 1, 6), dtype=np.float64)
    # A straight unit whose neighbouring curve cannot leave (or reach) it along
    # its direction without turning back becomes curve evidence too, and the
    # section is refitted; each pass demotes at least one line, so this ends.
    while True:
        work = points.copy()
        units = 0
        for i in range(pieces):
            is_curve = base_kind[i] == 1 or demote[i]
            if is_curve and units > 0 and kind[units - 1] == 1:
                unit_end[units - 1] = piece_end[i]
                continue
            kind[units] = 1 if is_curve else 0
            unit_start[units] = piece_start[i]
            unit_end[units] = piece_end[i]
            unit_base[units] = i
            dir_x[units] = base_dir_x[i]
            dir_y[units] = base_dir_y[i]
            units += 1

        # 3. Snap straight units; section ends (and the cyclic seam) stay put.
        for u in range(units):
            if kind[u] != 0 or snap == 0:
                continue
            us = unit_start[u]
            ue = unit_end[u]
            ok, vx, vy, nx, ny, _ = _snap_direction_nb(dir_x[u], dir_y[u], snap, snap_angle)
            if not ok:
                continue
            fixed_start = u == 0
            fixed_end = u == units - 1
            if fixed_start and fixed_end:
                offset = nx * points[us, 0] + ny * points[us, 1]
                if nx * points[ue, 0] + ny * points[ue, 1] != offset:
                    continue
            elif fixed_start:
                offset = nx * points[us, 0] + ny * points[us, 1]
            elif fixed_end:
                offset = nx * points[ue, 0] + ny * points[ue, 1]
            else:
                offset = 0.0
                for k in range(us, ue + 1):
                    offset += nx * points[k, 0] + ny * points[k, 1]
                offset /= ue - us + 1
            # The segment written: a fixed end stays, a free end moves onto the line.
            ax, ay = points[us, 0], points[us, 1]
            bx, by = points[ue, 0], points[ue, 1]
            if not fixed_start:
                ax, ay = _project_to_line_nb(ax, ay, nx, ny, offset)
            if not fixed_end:
                bx, by = _project_to_line_nb(bx, by, nx, ny, offset)
            worst, _ = _segment_distance_nb(points, us, ue, ax, ay, bx, by)
            if worst > straight_deviation:
                continue
            work[us, 0], work[us, 1] = ax, ay
            work[ue, 0], work[ue, 1] = bx, by
            dir_x[u] = vx
            dir_y[u] = vy

        # 4. End tangents of every curve unit (free at hard section ends).
        for u in range(units):
            start_fixed[u] = False
            end_fixed[u] = False
        if first_fixed and not cyclic:
            start_fixed[0] = True
            start_tx[0] = ftx
            start_ty[0] = fty
        if last_fixed and not cyclic:
            end_fixed[units - 1] = True
            end_tx[units - 1] = ltx
            end_ty[units - 1] = lty
        joins = units - 1 + (1 if cyclic else 0)
        for k in range(joins):
            left = k
            right = k + 1 if k + 1 < units else 0
            if kind[left] == 0 and kind[right] == 0:
                continue
            index = unit_end[left] if k + 1 < units else unit_start[0]
            if k + 1 >= units and first_fixed and kind[left] == 1 and kind[right] == 1:
                tx, ty = ftx, fty
            else:
                tx, ty = _join_nb(
                    work, index, lo, hi,
                    kind[left] == 0, dir_x[left], dir_y[left],
                    kind[right] == 0, dir_x[right], dir_y[right], large)
            if kind[left] == 1:
                end_fixed[left] = True
                end_tx[left] = tx
                end_ty[left] = ty
            if kind[right] == 1:
                start_fixed[right] = True
                start_tx[right] = tx
                start_ty[right] = ty

        # 5. Emit straight units and the placed nodes of each curve unit.
        count = 0
        for u in range(units):
            first_record[u] = count
            end_kind = _KIND_LINE_JOIN if u < units - 1 else _KIND_END
            if kind[u] == 0:
                count = _write_record_nb(records, count, True, 0.0, 0.0, 0.0, 0.0,
                                         work[unit_end[u], 0], work[unit_end[u], 1],
                                         end_kind, dir_x[u], dir_y[u], unit_end[u])
                last_record[u] = count - 1
                continue
            count = _place_nodes_nb(
                work, unit_start[u], unit_end[u], lo, hi,
                start_fixed[u], start_tx[u], start_ty[u],
                end_fixed[u], end_tx[u], end_ty[u],
                tolerance, pad_cap, small, large, mode, bends, scale, streak, prune_factor, newton,
                records, count, ranges, tangents, t, fitted)
            records[count - 1, 7] = float(end_kind)
            last_record[u] = count - 1

        changed = False
        for u in range(units):
            if kind[u] != 1:
                continue
            left = u - 1 if u > 0 else (units - 1 if cyclic else -1)
            right = u + 1 if u + 1 < units else (0 if cyclic else -1)
            if left >= 0 and left != u and kind[left] == 0:
                r = first_record[u]
                if records[r, 0] == 1.0 and not _chord_monotone_nb(
                        work[unit_start[u], 0], work[unit_start[u], 1],
                        records[r, 1], records[r, 2], records[r, 3], records[r, 4],
                        records[r, 5], records[r, 6]):
                    demote[unit_base[left]] = True
                    changed = True
            if right >= 0 and right != u and kind[right] == 0:
                r = last_record[u]
                sx = work[unit_start[u], 0]
                sy = work[unit_start[u], 1]
                if r > first_record[u]:
                    sx = records[r - 1, 5]
                    sy = records[r - 1, 6]
                if records[r, 0] == 1.0 and not _chord_monotone_nb(
                        sx, sy, records[r, 1], records[r, 2], records[r, 3], records[r, 4],
                        records[r, 5], records[r, 6]):
                    demote[unit_base[right]] = True
                    changed = True
        if not changed:
            break
    return records[:count]


def _fit_records(records: np.ndarray) -> list[Primitive]:
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


def _recovery_settings(settings: TraceSettings, stage: int) -> TraceSettings:
    """``settings`` tightened for local recovery ``stage``.

    Each stage only tightens: the 0.05 floor never raises a configured value
    that is already below it (a straight-line deviation of 0 stays 0). Stage 1
    halves the curve-fit tolerance; stage 2 quarters it and halves side
    smoothness together with the straight-line deviation, so fewer runs confirm
    a hard corner. Stage 3 adds fit constraints and stage 4 emits the dense
    samples, neither of which changes the settings.
    """
    if stage >= 2:
        return replace(
            settings,
            curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 4.0),
            straight_line_max_deviation=_tightened(settings.straight_line_max_deviation, 2.0),
            side_smoothness=_tightened(settings.side_smoothness, 2.0))
    if stage >= 1:
        return replace(settings, curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 2.0))
    return settings


def _curve_tolerance(settings: TraceSettings) -> float:
    tolerance = max(1e-4, float(settings.curve_fit_tolerance))
    if settings.max_deviation is not None:
        tolerance = min(tolerance, max(1e-4, float(settings.max_deviation)))
    return tolerance


def _join_scales(settings: TraceSettings) -> tuple[float, float]:
    """The two arc-length scales (px) a symmetric node's tangent is estimated at.

    Half the corner analysis span is the window on each side of a node for its
    starting tangent; a quarter span is a candidate tried at split points.
    """
    span = max(1e-4, float(settings.corner_analysis_span))
    return span * 0.25, span * 0.5


def _straight_deviation(settings: TraceSettings) -> float:
    """How far (px) a straight run's samples may lie from the segment written for it:
    ``straight_line_max_deviation``, capped at ``max_deviation`` when that is set."""
    deviation = max(0.0, float(settings.straight_line_max_deviation))
    if settings.max_deviation is not None:
        deviation = min(deviation, max(1e-4, float(settings.max_deviation)))
    return deviation


def _section_line(section: np.ndarray, settings: TraceSettings) -> tuple[bool, float]:
    """Whether a run is a sustained straight run, and the error of the line written.

    A straight run is written as its chord, the segment between its end nodes,
    so that segment is what is tested: it must be at least
    ``minimum_straight_length`` long and every sample must lie within
    ``_straight_deviation`` of it, a sample beyond either end being measured to
    that end. A best-fit line through the samples is not the line written; a
    bowed run can lie within the deviation of its best-fit line while its chord
    misses the middle by twice as much. A run that folds back on itself (a
    whole ring from its seam round to it again, a hairline sliver, an
    out-and-back run) is never one segment. Returns the chord's error either way.
    """
    if len(section) <= 2:
        return True, 0.0
    error, _ = _distance_to_line(section)
    vector = section[-1] - section[0]
    chord = math.hypot(float(vector[0]), float(vector[1]))
    if chord <= 0.0 or chord < float(settings.minimum_straight_length):
        return False, error
    return error <= _straight_deviation(settings), error


def _placement_arguments(settings: TraceSettings) -> tuple:
    """The node-placement and snapping arguments ``_fit_section_nb`` takes."""
    mode = PLACEMENT_MODES.index(settings.node_placement)
    snap = SNAP_MODES.index(settings.straight_line_snap)
    return (mode, bool(settings.nodes_at_bends), max(1e-3, float(settings.curve_feature_scale)),
            snap, math.radians(float(settings.snap_angle)),
            _straight_deviation(settings),
            _PRUNE_STREAK, _PRUNE_FACTOR, _NEWTON_WINDOW)


def _fit_section_records(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                         settings: TraceSettings, cyclic: bool = False,
                         first_tangent: tuple[float, float] | None = None,
                         last_tangent: tuple[float, float] | None = None) -> np.ndarray:
    _require_pixels(settings)
    small, large = _join_scales(settings)
    ftx, fty = first_tangent or (0.0, 0.0)
    ltx, lty = last_tangent or (0.0, 0.0)
    return _fit_section_nb(
        points, first, last, lo, hi, cyclic,
        first_tangent is not None, float(ftx), float(fty),
        last_tangent is not None, float(ltx), float(lty),
        _curve_tolerance(settings), _pad_cap(settings), float(settings.minimum_straight_length),
        small, large,
        *_placement_arguments(settings))


def _fit_curve_section(points: np.ndarray, first: int, last: int, lo: int, hi: int,
                       settings: TraceSettings, cyclic: bool = False,
                       first_tangent: tuple[float, float] | None = None,
                       last_tangent: tuple[float, float] | None = None) -> list[Primitive]:
    return _fit_records(_fit_section_records(points, first, last, lo, hi, settings, cyclic,
                                             first_tangent, last_tangent))


def _pad_cap(settings: TraceSettings) -> float:
    """The most (px) a free Bezier control may lie outside its samples' box.

    Controls are clipped to the sample box padded by the larger of the tolerance
    and a quarter of the box's extent, the latter capped here at a quarter of the
    corner analysis span (2 px at the pixel-era 8 px span), so the cap scales with
    the image like every other fit window.
    """
    return max(1e-4, float(settings.corner_analysis_span)) * 0.25


def _corner_move_limit(settings: TraceSettings) -> float:
    """How far a corner node may lie from its sample: the corner sharpening
    allowance (``_geometric_corners``, ``_snap_straight_sections``)."""
    return _sharpening_allowance(settings)


def _snap_straight_sections(points: np.ndarray, raw: np.ndarray, sections: list,
                            straight: list[bool], movable: set[int], key,
                            settings: TraceSettings) -> dict[int, float]:
    """Snap straight sections to exact horizontal, vertical or 45-degree lines.

    A straight section whose chord is within ``snap_angle`` of an allowed
    direction becomes an exact line in that direction. Junctions, chain ends and
    forced nodes never move, so a line touching one passes through it (two
    such ends must already lie on one exact line); otherwise it passes through
    the mean of the section's samples. Movable nodes are confirmed corners
    (``movable``, keyed by ``key(position)``): a corner between two snapped
    lines moves to their intersection, and one beside a single snapped line
    moves onto it (its nearest point). Distances are to the segment written
    between the section's end nodes where they end up. A snap stands only when
    every interior sample of the line stays within ``_straight_deviation`` of
    its segment, every moved corner stays within the corner sharpening
    allowance of its sample (``raw``), and every interior sample of an
    unsnapped straight neighbour of a moved corner stays within that deviation
    of the neighbour's moved chord. Otherwise the snap furthest from its
    direction is dropped and the rest re-solved.

    Moves nodes in ``points`` in place (both copies of a closed chain's seam)
    and returns ``{section: max sample distance}`` for the snapped sections.
    """
    snap = SNAP_MODES.index(settings.straight_line_snap)
    if snap == 0 or not any(straight):
        return {}
    max_angle = math.radians(float(settings.snap_angle))
    deviation = _straight_deviation(settings)
    limit = _corner_move_limit(settings)
    candidates: dict[int, tuple] = {}
    for index, (first, last) in enumerate(sections):
        if not straight[index]:
            continue
        ok, _, _, nx, ny, difference = _snap_direction_nb(
            float(points[last, 0] - points[first, 0]), float(points[last, 1] - points[first, 1]),
            snap, max_angle)
        if ok:
            candidates[index] = ((nx, ny), difference)
    if not candidates:
        return {}
    touching: dict[int, list[int]] = {}
    positions: dict[int, list[int]] = {}
    for index, (first, last) in enumerate(sections):
        for position in (first, last):
            touching.setdefault(key(position), []).append(index)
            if position not in positions.setdefault(key(position), []):
                positions[key(position)].append(position)
    original = points.copy()

    def chord_error(index: int, placed: dict[int, np.ndarray]) -> float:
        """Interior samples' distance to the section's segment with ``placed`` ends."""
        first, last = sections[index]
        start = placed.get(key(first), original[first])
        end = placed.get(key(last), original[last])
        return _segment_distance(original[first + 1:last], start, end)

    while candidates:
        lines: dict[int, tuple] = {}
        offending: set[int] = set()
        for index, ((nx, ny), _) in candidates.items():
            first, last = sections[index]
            fixed = [position for position in (first, last) if key(position) not in movable]
            if fixed:
                offset = nx * original[fixed[0], 0] + ny * original[fixed[0], 1]
                if len(fixed) == 2 and nx * original[fixed[1], 0] + ny * original[fixed[1], 1] != offset:
                    offending.add(index)
                    continue
            else:
                run = original[first:last + 1]
                offset = float(np.mean(run[:, 0] * nx + run[:, 1] * ny))
            lines[index] = ((nx, ny), offset)
        placed: dict[int, np.ndarray] = {}
        for node in sorted(movable):
            snapped = [index for index in touching.get(node, ()) if index in lines]
            if not snapped:
                continue
            here = original[positions[node][0]]
            if len(snapped) == 2 and lines[snapped[0]][0] != lines[snapped[1]][0]:
                (n1, c1), (n2, c2) = lines[snapped[0]], lines[snapped[1]]
                det = n1[0] * n2[1] - n1[1] * n2[0]
                point = ((c1 * n2[1] - c2 * n1[1]) / det, (n1[0] * c2 - n2[0] * c1) / det)
                if n1 == (0.0, 1.0) and n2 == (1.0, 0.0):
                    point = (c2, c1)
                elif n1 == (1.0, 0.0) and n2 == (0.0, 1.0):
                    point = (c1, c2)
            elif len(snapped) == 2:
                # Two parallel snapped lines cannot share a corner.
                offending.update(snapped)
                continue
            else:
                normal, offset = lines[snapped[0]]
                point = _project_to_line_nb(float(here[0]), float(here[1]), normal[0],
                                            normal[1], offset)
            point = np.asarray(point, dtype=np.float64)
            if math.dist(point, raw[positions[node][0]]) > limit:
                offending.update(snapped)
                continue
            placed[node] = point
        for index in lines:
            if chord_error(index, placed) > deviation:
                offending.add(index)
        for node in placed:
            for index in touching.get(node, ()):
                if straight[index] and index not in lines and chord_error(index, placed) > deviation:
                    offending.update(other for other in touching[node] if other in lines)
        if not offending:
            for node, point in placed.items():
                for position in positions[node]:
                    points[position] = point
            return {index: chord_error(index, placed) for index in lines}
        worst = max(offending, key=lambda index: (candidates[index][1], index))
        del candidates[worst]
    return {}


def _tight_step(start: Point, fitted: list[Primitive],
                first_tangent: tuple[float, float] | None,
                last_tangent: tuple[float, float] | None) -> Primitive:
    """The one cubic of a crossing-repair step between two consecutive samples
    made tight: its end tangents kept (a join's shared tangent where the node
    has one, else the fitted handle's direction) and both handles
    ``_MIN_HANDLE`` of the chord, the shortest the fitter allows."""
    end = fitted[-1][-1]
    chord = math.dist(start, end)
    if chord <= 0.0:
        return ("L", end)

    def unit(vector, fallback):
        length = math.hypot(*vector)
        return (vector[0] / length, vector[1] / length) if length > 1e-12 * chord else fallback

    along = ((end[0] - start[0]) / chord, (end[1] - start[1]) / chord)
    first, last = fitted[0], fitted[-1]
    out = first_tangent or unit((first[1][0] - start[0], first[1][1] - start[1])
                                if first[0] == "C" else along, along)
    back = last_tangent or unit((end[0] - last[2][0], end[1] - last[2][1])
                                if last[0] == "C" else along, along)
    handle = _MIN_HANDLE * chord
    return ("C", (start[0] + handle * out[0], start[1] + handle * out[1]),
            (end[0] - handle * back[0], end[1] - handle * back[1]), end)


def _compute_chain_fit(canonical: tuple, anchors: tuple, constraint_key: tuple,
                       settings: TraceSettings, step_key: tuple = ((), ())) -> ChainFit:
    """Fit one canonical chain from its samples, anchors, and constraints.

    Only confirmed corners (moved to where their two sides meet), junction
    anchors, where three or more colours meet, and a closed chain's anchor are
    cusp nodes: the chain is split there into runs fitted with free end
    tangents. A closed chain carries an anchor only where it leaves a vertex of
    pair degree other than two and returns to it, so that anchor is its two
    ends, as an open chain's ends are. Every
    other node is a symmetric node. A recovery fit constraint and any other
    anchor are forced nodes (the fit passes through them), symmetric like the
    rest: the runs on their two sides share one tangent there, a straight run's
    direction when one side is straight, and two straight runs meeting at one
    are both refitted as curves. ``step_key`` holds crossing repair's steps,
    pairs of consecutive constraint samples: a line step stays one line (the
    run beside it is the curve that takes its direction, and two line steps
    meeting stay lines, a cusp), and a tight step is its cubic with both handles
    ``_MIN_HANDLE`` of the chord (``_tight_step``). Straight runs may snap to exact lines
    (``_snap_straight_sections``), moving corners onto them. Inside a run
    ``_fit_section_nb`` keeps every node symmetric. An open chain's two ends
    are where the chain ends.

    A closed chain without an anchor has no ends: with a cusp it is fitted from its first cusp
    round to it again, so the fit starts there. With a forced node but no cusp
    it is fitted as a ring from its first forced node. With neither it is
    fitted as a ring from its canonical first sample and then refitted from
    the most natural node that fit placed (an axis extreme, else a line join,
    an inflection, a bend, then any node; the earliest such sample), with
    that node's tangent at the seam, and the refit is kept unless it has more
    pieces; the seam is a symmetric node like any other.
    """
    anchor_points = {anchor.point for anchor in anchors}
    junction_points = {anchor.point for anchor in anchors if anchor.kind == "junction"}
    forced_points = (anchor_points - junction_points) | set(constraint_key)
    line_set, tight_set = set(step_key[0]), set(step_key[1])
    array = np.asarray(canonical, dtype=float)
    closed = len(canonical) >= 4 and canonical[0] == canonical[-1]
    corners = _geometric_corners(array, settings, closed)
    # A corner stays on its sample at an anchor or a recovery fit constraint;
    # elsewhere it moves to where its two sides meet.
    pinned_points = anchor_points | set(constraint_key)
    model_points = array.copy()
    for index, point in corners.items():
        if canonical[index] not in pinned_points:
            model_points[index] = point
            if closed and index == 0:
                model_points[-1] = point
    movable_corners = {index for index in corners if canonical[index] not in pinned_points}

    count = len(canonical) - 1 if closed else len(canonical)
    interior = range(count) if closed else range(1, count - 1)
    hard = sorted(index for index in interior
                  if index in corners or canonical[index] in junction_points
                  or (closed and canonical[index] in anchor_points))
    hard_set = set(hard)
    forced = sorted(index for index in interior
                    if canonical[index] in forced_points and index not in hard_set)
    _, large = _join_scales(settings)
    reach = max(large, float(settings.curve_feature_scale))
    cyclic = closed and not hard
    tolerance = _curve_tolerance(settings)

    def layout(origin: int, seam_tangent: tuple[float, float] | None):
        if closed:
            ring = np.ascontiguousarray(np.roll(model_points[:-1], -origin, axis=0))
            raw_ring = np.ascontiguousarray(np.roll(array[:-1], -origin, axis=0))
            context = _cyclic_context(ring, reach) if cyclic else 0
            points = _cyclic_extension(ring, context) if cyclic else np.ascontiguousarray(
                np.vstack((ring, ring[:1])))
            raw = _cyclic_extension(raw_ring, context) if cyclic else np.ascontiguousarray(
                np.vstack((raw_ring, raw_ring[:1])))
            source = lambda local: (local + origin) % count
            hard_local = sorted({(index - origin) % count for index in hard} | {0, count}) \
                if hard else []
            forced_local = sorted((index - origin) % count for index in forced)
        else:
            context = 0
            points = np.ascontiguousarray(model_points)
            raw = array
            source = lambda local: local
            hard_local = sorted({0, count - 1, *hard})
            forced_local = forced
        # Section boundaries in ``points`` indices; a section's side windows stay
        # between the cusps that enclose it (the whole extended ring when cyclic).
        boundaries = sorted({*hard_local, *forced_local, *((0, count) if cyclic else ())})
        boundaries = [context + local for local in boundaries]
        sections = list(zip(boundaries, boundaries[1:]))
        hard_positions = [context + local for local in hard_local]

        def enclosing(first: int, last: int) -> tuple[int, int]:
            if cyclic:
                return 0, len(points) - 1
            lo = max(position for position in hard_positions if position <= first)
            hi = min(position for position in hard_positions if position >= last)
            return lo, hi

        evidence = [_section_line(points[first:last + 1], settings) for first, last in sections]
        straight = [flag for flag, _ in evidence]
        # Crossing-repair lines: a section between two consecutive samples the
        # repair drew as one line stays that line.
        steps = [tuple(sorted((canonical[source(first - context)],
                               canonical[source(last - context)])))
                 if last - first == 1 else None for first, last in sections]
        repair_line = [step is not None and step in line_set for step in steps]
        repair_tight = [step is not None and step in tight_set and step not in line_set
                        for step in steps]
        # Forced-node joins: (left section, right section, node position).
        joins = [(index, index + 1, sections[index][1]) for index in range(len(sections) - 1)
                 if sections[index][1] - context not in hard_local]
        if cyclic and len(sections) > 1:
            joins.append((len(sections) - 1, 0, sections[0][0]))
        # Two straight runs meeting at a forced node are refitted as curves, so
        # the node is symmetric; a repair line stays straight and the run beside
        # it is the curve that takes its direction. Two repair lines meeting
        # there stay lines.
        for left, right, _ in joins:
            if straight[left] and straight[right]:
                straight[left] = repair_line[left]
                straight[right] = repair_line[right]
        key = (lambda position: source(position - context))
        # A repair line runs between samples the fit must pass through: it is
        # never snapped.
        snapped = _snap_straight_sections(
            points, raw, sections,
            [flag and not repair for flag, repair in zip(straight, repair_line)],
            {index for index in movable_corners}, key, settings)
        first_tangent: list = [None] * len(sections)
        last_tangent: list = [None] * len(sections)
        for left, right, position in joins:
            lo, hi = enclosing(*sections[left]) if not cyclic else (0, len(points) - 1)
            in_line = straight[left]
            out_line = straight[right]
            in_dir = (_chord_direction_nb(points, *sections[left])[:2] if in_line else (0.0, 0.0))
            out_dir = (_chord_direction_nb(points, *sections[right])[:2] if out_line else (0.0, 0.0))
            lo = min(lo, enclosing(*sections[right])[0]) if not cyclic else lo
            hi = max(hi, enclosing(*sections[right])[1]) if not cyclic else hi
            tangent = _join_nb(points, position, lo, hi, in_line, in_dir[0], in_dir[1],
                               out_line, out_dir[0], out_dir[1], large)
            last_tangent[left] = tangent
            first_tangent[right] = tangent
        if seam_tangent is not None:
            first_tangent[0] = last_tangent[-1] = seam_tangent

        pieces: list[Primitive] = []
        section_diagnostics: list[RunDiagnostic] = []
        seam_records = None
        for index, (first, last) in enumerate(sections):
            start, end = source(first - context), source(last - context)
            residual = snapped.get(index, evidence[index][1])
            if straight[index]:
                pieces.append(("L", tuple(map(float, points[last]))))
                section_diagnostics.append(RunDiagnostic("straight", start, end, residual, 1))
                continue
            lo, hi = enclosing(first, last)
            records = _fit_section_records(points, first, last, lo, hi, settings,
                                           cyclic and len(sections) == 1,
                                           first_tangent[index], last_tangent[index])
            if cyclic and len(sections) == 1:
                seam_records = (records, context)
            fitted = _fit_records(records)
            if repair_tight[index]:
                fitted = [_tight_step(tuple(map(float, points[first])), fitted,
                                      first_tangent[index], last_tangent[index])]
            pieces.extend(fitted)
            section_diagnostics.append(RunDiagnostic("curve", start, end,
                                                     min(residual, tolerance), len(fitted)))
        corner_points = {source(position - context): points[position]
                         for position in boundaries if source(position - context) in corners}
        start_point = tuple(map(float, points[boundaries[0]]))
        return start_point, pieces, section_diagnostics, corner_points, seam_records

    if closed:
        origin = hard[0] if hard else (forced[0] if forced else 0)
    else:
        origin = 0
    start_point, pieces, section_diagnostics, corner_points, seam_records = layout(origin, None)
    if cyclic and not forced and seam_records is not None:
        records, context = seam_records
        # Most natural first: extreme, line join, inflection, bend, anything else.
        rank = {_KIND_EXTREME: 0, _KIND_LINE_JOIN: 1, _KIND_INFLECTION: 2, _KIND_BEND: 3}
        choices = [(rank.get(int(row[7]), 4), (int(row[10]) - context) % count, row)
                   for row in records[:-1]]
        if choices:
            _, seam, row = min(choices, key=lambda item: (item[0], item[1]))
            refit = layout(seam, (float(row[8]), float(row[9])))
            if len(refit[1]) <= len(pieces):
                start_point, pieces, section_diagnostics, corner_points, _ = refit

    diagnostics: list[RunDiagnostic] = []
    corner_nodes: list[Point] = []
    for index in sorted(corners):
        if canonical[index] in anchor_points:
            continue
        final = np.asarray(corner_points.get(index, model_points[index]), dtype=np.float64)
        corner_nodes.append((float(final[0]), float(final[1])))
        delta = final - array[index]
        diagnostics.append(RunDiagnostic(
            "corner", index, index,
            math.sqrt(delta[0] * delta[0] + delta[1] * delta[1]), 0))
    diagnostics.extend(section_diagnostics)
    primitives = tuple(pieces)
    line_count = sum(p[0] == "L" for p in primitives)
    used = len(anchor_points.intersection(canonical))
    return ChainFit(start_point, primitives, FitMetrics(
        line_count, len(primitives) - line_count, used, len(canonical), len(primitives)),
        tuple(diagnostics), tuple(corner_nodes))


# Distinct chains fitted concurrently once this many need fitting. The chain
# fit spends most of its time in node placement, a Numba kernel that releases the
# GIL, so threads overlap it; the Python between kernels still serialises, and
# beyond four threads contention for it made the corpus slower, not faster.
_THREADED_FIT_MINIMUM = 64
_FIT_THREADS = max(1, min(4, os.cpu_count() or 1))


def _fit_cache_misses(fitter: "SharedChainFitter", plans: list[tuple],
                      tick: Progress = _noop,
                      checkpoint: Callable[[], None] | None = None) -> None:
    """Fit every plan whose key is absent from the fitter cache.

    Each plan is ``(key, canonical, anchors, constraint_key, settings, step_key)`` as
    produced by ``SharedChainFitter.prepare``. Distinct keys are fitted once and
    land in the cache under their key. Each fit depends only on its own plan, so
    the result is the same in any order; from ``_THREADED_FIT_MINIMUM`` pending
    keys they are fitted on ``_FIT_THREADS`` threads, since node placement
    (``_fit_section_nb``) runs without the GIL. ``checkpoint``, when given, is
    called before each fit and may raise to abandon the loop; fits already made
    stay cached.
    """
    unique_pending: dict[tuple, tuple] = {}
    for key, canonical, anchor_tuple, constraint_key, chain_settings, step_key in plans:
        if key not in fitter._cache:
            unique_pending.setdefault(
                key, (canonical, anchor_tuple, constraint_key, chain_settings, step_key))
    if not unique_pending:
        return
    total = len(unique_pending)
    if total < _THREADED_FIT_MINIMUM or _FIT_THREADS == 1:
        for done, (key, argument) in enumerate(unique_pending.items(), 1):
            if checkpoint is not None:
                checkpoint()
            fitter._cache[key] = _compute_chain_fit(*argument)
            if done % 100 == 0 or done == total:
                tick(done / total, f"Fitting boundary curves ({done} of {total})")
        return

    def fit(item: tuple) -> None:
        key, argument = item
        if checkpoint is not None:
            checkpoint()
        fitter._cache[key] = _compute_chain_fit(*argument)

    with ThreadPoolExecutor(max_workers=_FIT_THREADS) as executor:
        futures = [executor.submit(fit, item) for item in unique_pending.items()]
        try:
            for done, future in enumerate(as_completed(futures), 1):
                future.result()
                if done % 100 == 0 or done == total:
                    tick(done / total, f"Fitting boundary curves ({done} of {total})")
        except BaseException:
            for future in futures:
                future.cancel()
            raise


class SharedChainFitter:
    """Canonical cache ensures both users of a shared chain get one fit."""

    def __init__(self) -> None:
        self._cache: dict[tuple, ChainFit] = {}

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def prepare(self, coords: Iterable[Point], settings: TraceSettings,
                hard_anchors: Iterable[HardAnchor] = (),
                fit_constraints: Iterable[Point] = (),
                fit_lines: Iterable[tuple[Point, Point]] = (),
                fit_tight: Iterable[tuple[Point, Point]] = ()) -> tuple:
        """Canonicalize a chain and build its cache key without fitting it.

        ``fit_lines`` and ``fit_tight`` are pairs of consecutive fit constraints
        (crossing repair): the fit draws the step between a line pair as one
        line, and between a tight pair as a curve with the shortest handles the
        fitter allows. Returns ``(key, forward, canonical, anchors,
        constraint_key, step_key)``; ``canonical``, ``anchors`` and
        ``constraint_key``, with the settings, then ``step_key``, are exactly the
        inputs ``_compute_chain_fit`` needs.
        """
        _require_pixels(settings)
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
        constrained = set(constraint_key)

        def step_pairs(pairs) -> tuple:
            ordered = {tuple(sorted((tuple(map(float, a)), tuple(map(float, b)))))
                       for a, b in pairs}
            return tuple(sorted(pair for pair in ordered
                                if pair[0] in constrained and pair[1] in constrained))

        step_key = (step_pairs(fit_lines), step_pairs(fit_tight))
        key = (canonical, anchor_key, constraint_key, step_key,
               float(settings.curve_fit_tolerance),
               float(settings.straight_line_max_deviation),
               float(settings.minimum_straight_length),
               float(settings.corner_analysis_span), float(settings.corner_threshold),
               float(settings.side_smoothness), settings.max_deviation,
               _sharpening_allowance(settings),
               settings.straight_line_snap, float(settings.snap_angle),
               settings.node_placement, bool(settings.nodes_at_bends),
               float(settings.curve_feature_scale))
        return key, forward, canonical, anchors, constraint_key, step_key

    def fit(self, coords: Iterable[Point], settings: TraceSettings,
            hard_anchors: Iterable[HardAnchor] = (),
            fit_constraints: Iterable[Point] = (),
            fit_lines: Iterable[tuple[Point, Point]] = (),
            fit_tight: Iterable[tuple[Point, Point]] = ()) -> ChainFit:
        key, forward, canonical, anchors, constraint_key, step_key = self.prepare(
            coords, settings, hard_anchors, fit_constraints, fit_lines, fit_tight)
        if key not in self._cache:
            self._cache[key] = _compute_chain_fit(
                canonical, anchors, constraint_key, settings, step_key)
        fitted = self._cache[key]
        return fitted if forward else fitted.reversed()


def _fmt(point: Point) -> str:
    """A path coordinate pair as written: 10 significant digits, enough that a
    symmetric node's collinear handles still read as collinear (within the 0.5
    degrees of ``node_types``) after rounding. ``node_types.point_key`` mirrors it."""
    return f"{point[0]:.10g} {point[1]:.10g}"


def _ring_fit_path(fitted: ChainFit, points: list[Point],
                   anchor_points: frozenset) -> RingFit:
    """The closed SVG subpath of one ring's fit, with its metrics."""
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


def ring_path(coords: Iterable[Point], settings: TraceSettings,
              hard_anchors: Iterable[HardAnchor] = (),
              fitter: SharedChainFitter | None = None) -> RingFit:
    raw = [tuple(map(float, p)) for p in coords]
    points = raw[:-1] if raw and raw[0] == raw[-1] else raw
    anchors = tuple(hard_anchors)
    fitter = fitter or SharedChainFitter()
    # Closed rings use the same modeller as shared open chains. The duplicated
    # seam is only a topological endpoint; it is not a protected or inferred
    # geometry anchor.
    fitted = fitter.fit([*points, points[0]], settings, anchors)
    return _ring_fit_path(fitted, points, frozenset(a.point for a in anchors))


def _noded_rings(polygons: list) -> dict[tuple[int, int], tuple[Point, ...]]:
    """Every ring of ``polygons`` as closed samples, keyed ``(polygon, ring)``.

    A valid polygon's rings may touch where a vertex of one lies inside a
    segment of another (or of itself); that vertex is inserted into the segment,
    so every contact between rings is at a vertex both carry.
    """
    rings: dict[tuple[int, int], list[Point]] = {}
    for number, polygon in enumerate(polygons):
        for position, ring in enumerate((polygon.exterior, *polygon.interiors)):
            raw = [tuple(map(float, point)) for point in ring.coords]
            rings[(number, position)] = raw if raw[0] == raw[-1] else [*raw, raw[0]]
    keys = list(rings)
    starts = np.asarray([point for key in keys for point in rings[key][:-1]], dtype=np.float64)
    ends = np.asarray([point for key in keys for point in rings[key][1:]], dtype=np.float64)
    owner = [(key, index) for key in keys for index in range(len(rings[key]) - 1)]
    if not len(starts):
        return {key: tuple(points) for key, points in rings.items()}
    segments = shapely.linestrings(np.stack((starts, ends), axis=1))
    vertices = shapely.points(starts)
    point_index, segment_index = STRtree(segments).query(vertices, predicate="intersects")
    inserted: dict[tuple, list[Point]] = {}
    for vertex, segment in zip(point_index.tolist(), segment_index.tolist()):
        point = tuple(starts[vertex].tolist())
        if point == tuple(starts[segment].tolist()) or point == tuple(ends[segment].tolist()):
            continue
        inserted.setdefault(owner[segment], []).append(point)
    for (key, index), points in sorted(inserted.items(), key=lambda item: (item[0][0], -item[0][1])):
        origin = rings[key][index]
        points = sorted(set(points), key=lambda point: (point[0] - origin[0]) ** 2
                        + (point[1] - origin[1]) ** 2)
        rings[key][index + 1:index + 1] = points
    return {key: tuple(points) for key, points in rings.items()}


def geometry_components(geometry, settings: TraceSettings,
                        hard_anchors: Iterable[HardAnchor] = (),
                        fitter: SharedChainFitter | None = None) -> list[dict]:
    """Fit every ring of ``geometry`` on its own and group the rings into components.

    The rings are repaired together (``_repair_crossings``), so no ring crosses
    itself or another ring of the geometry.
    """
    polygons = [] if geometry.is_empty else ([geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms))
    polygons.sort(key=lambda polygon: (-polygon.area, polygon.bounds))
    fitter = fitter or SharedChainFitter()
    anchors = tuple(hard_anchors)
    anchor_points = frozenset(anchor.point for anchor in anchors)
    rings = _noded_rings(polygons)
    repair: dict = {}
    fits: dict[tuple[int, int], ChainFit] = {}

    def refit(keys: Iterable) -> None:
        plans = []
        for key in keys:
            samples = rings[key]
            state = repair.get(key)
            pins = state["pins"] if state is not None else ()
            lines = state["lines"] if state is not None else ()
            tight = state["tight"] if state is not None else ()
            plan_key, forward, canonical, anchor_tuple, constraint_key, step_key = \
                fitter.prepare(samples, settings, anchors, pins, lines, tight)
            plans.append((key, forward, plan_key, canonical, anchor_tuple, constraint_key,
                          step_key))
        _fit_cache_misses(fitter, [(plan_key, canonical, anchor_tuple, constraint_key, settings,
                                    step_key)
                                   for _, _, plan_key, canonical, anchor_tuple, constraint_key,
                                   step_key in plans])
        for key, forward, plan_key, *_ in plans:
            fitted = fitter._cache[plan_key]
            fits[key] = fitted if forward else fitted.reversed()

    refit(rings)
    _repair_crossings(fits, rings, refit, repair, lambda key: False)
    result = []
    for number, polygon in enumerate(polygons):
        fitted = [_ring_fit_path(fits[(number, position)], list(rings[(number, position)][:-1]),
                                 anchor_points)
                  for position in range(1 + len(polygon.interiors))]
        aggregate = FitMetrics(
            sum(f.metrics.lines for f in fitted), sum(f.metrics.cubics for f in fitted),
            sum(f.metrics.hard_anchors for f in fitted), sum(f.metrics.source_samples for f in fitted),
            sum(f.metrics.final_nodes for f in fitted))
        states = [repair[(number, position)] for position in range(1 + len(polygon.interiors))
                  if (number, position) in repair]
        result.append({"d": " ".join(f.path for f in fitted), "nodes": aggregate.final_nodes,
                       "holes": len(polygon.interiors), "fit_metrics": aggregate.as_dict(),
                       "crossing_repair": {
                           "repaired_rings": len(states),
                           "dense_fallback_rings": sum(_repair_fell_back(state) for state in states)},
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


def _repair_dense(state: dict) -> bool:
    """Whether crossing repair has pinned every sample and drawn every step
    between consecutive samples as a line, making the piece its dense samples."""
    return bool(state.get("samples")) and len(state["pins"]) >= state["samples"] \
        and len(state.get("lines", ())) >= state.get("pairs", 0)


def _repair_fell_back(state: dict) -> bool:
    """Whether crossing repair reduced a piece with interior samples to its dense
    samples: a dense fallback. A two-sample piece pinned at both ends is only
    straightened to its one line."""
    return _repair_dense(state) and state["samples"] > 2


def _closed_samples(samples: tuple) -> tuple[bool, int]:
    closed = len(samples) >= 4 and samples[0] == samples[-1]
    return closed, len(samples) - 1 if closed else len(samples)


def _arrives_backward(fit: ChainFit, node: int, array: np.ndarray, low: int,
                      high: int) -> bool:
    """Whether the primitive of ``fit`` arriving at ``node`` (of a closed ring)
    has its middle nearer the samples from ``low`` to ``high`` (indices taken
    modulo the sample count) than any other sample: the fit steps back over
    them there."""
    primitive = fit.primitives[node - 1]
    start = fit.start if node == 1 else fit.primitives[node - 2][-1]
    if primitive[0] == "L":
        middle = ((start[0] + primitive[1][0]) / 2, (start[1] + primitive[1][1]) / 2)
    else:
        p0, p1, p2, p3 = start, primitive[1], primitive[2], primitive[3]
        middle = tuple(0.125 * p0[i] + 0.375 * p1[i] + 0.375 * p2[i] + 0.125 * p3[i]
                       for i in range(2))
    nearest = int(np.argmin(np.hypot(array[:, 0] - middle[0], array[:, 1] - middle[1])))
    count = len(array)
    return (nearest - low) % count <= high - low


def _node_samples(fit: ChainFit, samples: tuple) -> list[int]:
    """The sample index of each node of ``fit``, following the fit along the
    samples.

    ``fit`` runs in the samples' order, but may step back where it hooks (a
    moved corner, a turn back at a chain end), so the indices need not
    increase. A node that is a sample takes its index; a node between two such
    nodes (a moved corner, a snapped join) takes the index of its nearest sample
    near theirs, which may lie past them where it overshoots. On a closed chain each index is taken on the
    lap nearest the previous node's, so the indices run on past the seam and
    the last node's index is the first's plus the sample count; an open chain's
    indices stay within its samples.
    """
    closed, count = _closed_samples(samples)
    position: dict[Point, int] = {}
    for index, point in enumerate(samples[:count]):
        position.setdefault(point, index)
    array = np.asarray(samples[:count], dtype=np.float64)
    nodes = [fit.start, *(primitive[-1] for primitive in fit.primitives)]
    total = len(nodes)
    if closed:
        # Work from the first node that is a sample round to it again.
        ring = total - 1
        first = next((k for k in range(ring) if nodes[k] in position), None)
        if first is None:
            anchor = int(np.argmin(np.hypot(array[:, 0] - nodes[0][0],
                                            array[:, 1] - nodes[0][1])))
            first, fixed = 0, anchor
        else:
            fixed = position[nodes[first]]
        order = [(first + step) % ring for step in range(ring + 1)]
    else:
        order = list(range(total))
        fixed = position.get(nodes[0], 0)
    values: list[int | None] = [None] * len(order)
    values[0] = fixed
    previous = fixed
    for step in range(1, len(order)):
        k = order[step]
        index = fixed if closed and step == len(order) - 1 else position.get(nodes[k])
        if index is None:
            continue
        if closed:
            # Forward to the next lap unless the fit stepped back: the shorter
            # way round, with the middle of the primitive arriving at this node
            # on the samples it backs over.
            forward = previous + (index - previous) % count
            back = forward - count
            if step == len(order) - 1:
                index = fixed + count if forward == previous else forward
            elif previous - back < forward - previous and \
                    _arrives_backward(fit, k, array, back, previous):
                index = back
            else:
                index = forward
        values[step] = index
        previous = index
    if not closed and values[-1] is None:
        values[-1] = count - 1
    step = 1
    while step < len(values):
        if values[step] is not None:
            step += 1
            continue
        end = step
        while values[end] is None:
            end += 1
        low, high = sorted((values[step - 1], values[end]))
        # A node off the samples may overshoot its neighbours (a hook's tip):
        # search past them by their own gap, never round the rest of the ring.
        reach = max(high - low, 1)
        window = np.arange(low - reach, high + reach + 1)
        if not closed:
            window = window[(window >= 0) & (window < count)]
        ring = array[window % count]
        for j in range(step, end):
            node = nodes[order[j]]
            distance = np.hypot(ring[:, 0] - node[0], ring[:, 1] - node[1])
            # Where the window holds the nearest sample on more than one lap,
            # the lap nearest the previous node's.
            nearest = window[distance <= distance.min() + 1e-12]
            values[j] = int(nearest[int(np.argmin(np.abs(nearest - values[j - 1])))])
        step = end
    result = [0] * total
    if closed:
        # Back to the fit's own order: nodes the rotation reached before the
        # fit's first node come after it, one lap on.
        seam = order.index(0)
        for step, k in enumerate(order[:-1]):
            result[k] = values[step] + (count if step < seam else 0)
        result[-1] = result[0] + count
    else:
        result = [int(value) for value in values]
    return result


def _straighten(state: dict, fit: ChainFit, samples: tuple,
                primitives: tuple[int, ...], fresh: set) -> bool:
    """Tighten, then straighten, each contacting primitive of ``fit`` that runs
    between two consecutive pinned samples; returns whether the contact is
    handled so (any such primitive).

    Such a primitive cannot be pinned further. It is a curve whose end tangents
    are its symmetric nodes' shared tangents. Still in contact, it is first
    made tight: the same tangents with the shortest handles the fitter allows
    (``_MIN_HANDLE`` of the chord), hugging the chord. Still in contact then,
    the repair draws it as the dense boundary step itself, a line. A step next
    to one drawn as a line in the same pass (``fresh``) waits a pass: refitted
    beside that line it takes the line's direction, and may then need no line,
    so the node between them need not be a cusp.
    """
    pinned = set(state["pins"])
    closed, count = _closed_samples(samples)
    position: dict[Point, int] = {}
    for index, point in enumerate(samples[:count]):
        position.setdefault(point, index)
    nodes = [fit.start, *(primitive[-1] for primitive in fit.primitives)]
    tight = state.setdefault("tight", [])
    lines = state.setdefault("lines", [])
    handled = False
    for k in primitives:
        if k >= len(fit.primitives):
            continue
        a, b = nodes[k], nodes[k + 1]
        if a not in pinned or b not in pinned:
            continue
        i, j = position.get(a), position.get(b)
        if i is None or j is None:
            continue
        step = (j - i) % count if closed else abs(j - i)
        if step != 1 and not (closed and step == count - 1):
            continue
        pair = tuple(sorted((a, b)))
        if pair in lines:
            continue
        handled = True
        if pair not in tight:
            tight.append(pair)
        elif not any(a in other or b in other for other in fresh):
            lines.append(pair)
            fresh.add(pair)
    return handled


def _pin(state: dict, fit: ChainFit, samples: tuple, point: Point,
         primitives: tuple[int, ...]) -> bool:
    """Pin the samples of one contact; returns whether any is new.

    The contact's sample is the one nearest ``point`` among those the
    contacting ``primitives`` of ``fit`` span; round ``r`` of the piece's
    repair pins it and the ``2^(r-2)`` samples either side (none on round 1).
    """
    closed, count = _closed_samples(samples)
    indices = _node_samples(fit, samples)
    array = np.asarray(samples[:count], dtype=np.float64)
    # A primitive spans the samples between its ends' indices, whichever way
    # the fit runs there.
    span = np.concatenate([np.arange(min(indices[k], indices[k + 1]),
                                     max(indices[k], indices[k + 1]) + 1)
                           for k in primitives if k < len(indices) - 1]) \
        if primitives else np.arange(count)
    ring = span % count if closed else np.clip(span, 0, count - 1)
    centre = int(ring[int(np.argmin(np.hypot(array[ring, 0] - point[0],
                                             array[ring, 1] - point[1])))])
    reach = 0 if int(state["round"]) <= 1 else 2 ** (int(state["round"]) - 2)
    grew = False
    for offset in range(-reach, reach + 1):
        index = centre + offset
        if closed:
            index %= count
        elif not 0 <= index < count:
            continue
        if samples[index] not in state["pins"]:
            state["pins"].append(samples[index])
            grew = True
    return grew


def _repair_crossings(fits: dict, samples: dict, refit: Callable[[set], None],
                      repair: dict, dense: Callable[[object], bool]) -> int:
    """Repair fitted pieces locally until none is in an illegal contact.

    Every path is assembled from the pieces in ``fits``, so no path crosses
    itself once no piece crosses itself or another (``crossings``: a crossing,
    or a contact anywhere but a shared vertex where the pieces do not cross).
    Each piece in a contact is repaired once per pass and ``refit`` is called
    with them. A contacting primitive between two consecutive pinned samples,
    which pinning cannot change, is drawn as a line, the dense boundary step
    itself (``_straighten``); the fit keeps every other node symmetric, so the
    curves beside it take its direction. Every other contact pins the piece
    (fit constraints) to the sample nearest the contact among those the
    contacting primitives span, and on later rounds to 1, 2, 4, ... samples
    either side too (``_pin``); pinned samples are symmetric nodes like any
    other forced node. Each pass pins a new sample or draws a new line. The
    dense samples of every piece together form a planar partition and cannot
    cross, so the repair ends, at worst with a piece wholly pinned and drawn as
    lines: its dense samples. ``repair`` holds each piece's round, pins and
    lines; ``dense`` names pieces already dense for another reason. Returns the
    number of repair passes.
    """
    passes = 0
    flattened: dict = {}
    # A contact between two pieces untouched by the last pass would have been
    # found in it, so each later pass checks only the pieces it repaired.
    escalated: set | None = None
    while True:
        found = crossings({key: (fit.start, fit.primitives) for key, fit in fits.items()},
                          only=escalated, cache=flattened)
        if not found:
            return passes
        passes += 1
        contacts: dict = {}
        for item in found:
            contacts.setdefault(item.first, []).append((item.point, item.first_primitives))
            contacts.setdefault(item.second, []).append((item.point, item.second_primitives))
        escalated = set()
        for key in sorted(contacts, key=str):
            if dense(key):
                continue
            state = repair.setdefault(key, {"round": 0, "pins": [], "tight": [], "lines": [],
                                            "samples": 0, "pairs": 0})
            closed, count = _closed_samples(samples[key])
            state["samples"] = count
            state["pairs"] = count if closed else count - 1
            # A contact on a curve between consecutive pinned samples straightens
            # it; every other contact pins.
            steps = len(state["tight"]) + len(state["lines"])
            fresh: set = set()
            pinning = [(point, primitives) for point, primitives in contacts[key]
                       if not _straighten(state, fits[key], samples[key], primitives, fresh)]
            grew = len(state["tight"]) + len(state["lines"]) > steps
            # A round that finds every sample near its contacts pinned widens
            # until it pins a new one, or every sample is pinned; a contact
            # already on a repair line widens the same way (the other piece in
            # it is repaired too).
            if pinning or not grew:
                pinned = False
                while not pinned and len(state["pins"]) < count:
                    state["round"] = int(state["round"]) + 1
                    for point, primitives in pinning or contacts[key]:
                        pinned |= _pin(state, fits[key], samples[key], point, primitives)
                grew |= pinned
            if grew:
                escalated.add(key)
        # A contact neither of whose pieces can be repaired further is between
        # dense samples, which the planar partition rules out.
        stuck = [item for item in found
                 if item.first not in escalated and item.second not in escalated]
        if stuck:
            raise RuntimeError(
                "dense boundary samples cross at "
                + ", ".join(f"({item.point[0]:.6g}, {item.point[1]:.6g})" for item in stuck[:5]))
        refit(escalated)


def shared_boundary_components(track, included: list[int], settings: TraceSettings,
                               recovery: dict[str, dict[str, object]] | None = None,
                               progress: Progress | None = None,
                               fitter: "SharedChainFitter | None" = None,
                               checkpoint: Callable[[], None] | None = None,
                               ) -> tuple[dict[int, list[dict]], dict[str, "ChainFit"], dict[str, object]]:
    """Fit canonical interfaces once, then assemble exact reverse uses into paths.

    A caller may supply a persistent ``fitter`` so its cache survives across the
    recovery loop's repeated passes: a chain whose recovery state is unchanged
    keys identically and returns from cache instead of re-fitting. ``checkpoint`` is
    called before each chain fit and may raise to abandon the fit.
    """
    from vinylgen.boundary_graph import assemble_colour_rings

    fitter = fitter or SharedChainFitter()
    chains = {chain.identity: chain for chain in track.chains}
    fits: dict[str, ChainFit] = {}
    recovery = recovery or {}
    tick = progress or _noop
    total = len(track.chains)
    # Crossing repair per chain: its round and the samples it is pinned to.
    crossing: dict[str, dict[str, object]] = {}

    def is_dense(identity: str) -> bool:
        return int(recovery.get(identity, {}).get("stage", 0)) >= 4

    def fit_chains(identities: set[str] | None) -> None:
        # Phase 1: resolve each chain's recovery-adjusted settings and cache key.
        # This is cheap; the expensive curve fit is deferred to phase 2.
        plans: list[tuple] = []
        for index, chain in enumerate(track.chains):
            if identities is not None and chain.identity not in identities:
                continue
            if identities is None:
                tick(index / total if total else 1.0,
                     f"Fitting boundary curves ({index + 1} of {total})")
            anchors = [HardAnchor(anchor.point, anchor.kind) for anchor in chain.anchors]
            samples = chain.points
            state = recovery.get(chain.identity, {})
            stage = int(state.get("stage", 0))
            chain_settings = _recovery_settings(settings, stage)
            constraint = state.get("constraint")
            constraints = [tuple(constraint)] if constraint is not None and stage >= 3 else []
            constraints.extend(crossing.get(chain.identity, {}).get("pins", ()))
            lines = crossing.get(chain.identity, {}).get("lines", ())
            tight = crossing.get(chain.identity, {}).get("tight", ())
            if is_dense(chain.identity):
                reason = "maximum deviation still exceeded after ordered local recovery"
                primitives = tuple(("L", point) for point in samples[1:])
                fits[chain.identity] = ChainFit(
                    samples[0], primitives,
                    FitMetrics(len(primitives), 0, len(anchors), len(samples), len(primitives)),
                    (RunDiagnostic(
                        "curve", 0, len(samples) - 1, 0.0, len(primitives),
                        recovery_attempts=4, dense_fallback=True,
                        fallback_reason=reason),))
                continue
            key, forward, canonical, anchor_tuple, constraint_key, step_key = fitter.prepare(
                samples, chain_settings, anchors, constraints, lines, tight)
            plans.append((chain.identity, stage, forward, key,
                          canonical, anchor_tuple, constraint_key, chain_settings, step_key))

        # Phase 2: compute the cache misses. Distinct keys are fitted once by the
        # compiled per-chain fitter.
        _fit_cache_misses(
            fitter, [(key, canonical, anchor_tuple, constraint_key, chain_settings, step_key)
                     for _, _, _, key, canonical, anchor_tuple, constraint_key, chain_settings,
                     step_key in plans], tick if identities is None else _noop, checkpoint)

        # Phase 3: assemble oriented fits from the now-populated cache.
        for identity, stage, forward, key, *_ in plans:
            fitted = fitter._cache[key]
            if not forward:
                fitted = fitted.reversed()
            if stage:
                fitted = replace(fitted, diagnostics=tuple(
                    replace(item, recovery_attempts=stage) for item in fitted.diagnostics))
            fits[identity] = fitted

    # Phase 4: no path may cross itself (``_repair_crossings``). A chain already
    # dense from protected-feature recovery is not repaired further.
    fit_chains(None)
    crossing_passes = _repair_crossings(
        fits, {identity: chain.points for identity, chain in chains.items()},
        fit_chains, crossing,
        lambda identity: int(recovery.get(identity, {}).get("stage", 0)) >= 4)

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
                        is_dense(identity) or (identity in crossing
                                               and _repair_fell_back(crossing[identity]))
                        for identity in set(recovery) | set(crossing)),
                    "chain_recovery": {
                        identity: {"stage": int(state.get("stage", 0)),
                                   "constraint": state.get("constraint")}
                        for identity, state in sorted(recovery.items())},
                    "crossing_passes": crossing_passes,
                    "crossing_recovery": {
                        identity: {"round": int(state["round"]),
                                   "dense_fallback": _repair_fell_back(state),
                                   "pins": [list(point) for point in state["pins"]],
                                   "tight": [[list(a), list(b)] for a, b in state["tight"]],
                                   "lines": [[list(a), list(b)] for a, b in state["lines"]]}
                        for identity, state in sorted(crossing.items())},
                    "chain_diagnostics": {
                        identity: [item.as_dict() for item in fit.diagnostics]
                        for identity, fit in sorted(fits.items())
                    },
                    "fit_metrics": unique.as_dict()}
