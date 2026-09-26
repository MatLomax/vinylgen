"""Only detector corners and junction anchors are cusps; every other node is symmetric.

Turns are measured on the fitted primitives: the direction a piece arrives at a
node (its end handle, or a line's chord) against the direction the next piece
leaves it. A symmetric node turns by 0 degrees. The corner detector itself is
tested here too: tight curves get no corners, polygons keep theirs, and a short
edge between two corners keeps both.
"""

import math
import unittest

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import (
    ChainFit, HardAnchor, SharedChainFitter, _chord_monotone_nb, _constrained_cubic_nb,
    _geometric_corners, _tightened)
from vinylgen.trace_lengths import resolve_trace_lengths

SMOOTH_DEG = 1e-6


def _unit(vector):
    length = math.hypot(*vector)
    return None if length == 0 else (vector[0] / length, vector[1] / length)


def _arriving(start, primitive):
    end = primitive[-1]
    candidates = (start,) if primitive[0] == "L" else (primitive[2], primitive[1], start)
    for other in candidates:
        direction = _unit((end[0] - other[0], end[1] - other[1]))
        if direction is not None:
            return direction
    return None


def _leaving(node, primitive):
    for other in primitive[1:]:
        direction = _unit((other[0] - node[0], other[1] - node[1]))
        if direction is not None:
            return direction
    return None


def join_turns(fit: ChainFit, closed: bool) -> list[tuple[tuple, float, str]]:
    """``(node, turn in degrees, kinds)`` for every join of ``fit``; ``kinds`` is
    the two pieces' types, e.g. ``"LC"``. A closed fit includes its seam."""
    starts = [fit.start, *(primitive[-1] for primitive in fit.primitives[:-1])]
    pairs = [(index, index + 1) for index in range(len(fit.primitives) - 1)]
    if closed and len(fit.primitives) > 1:
        pairs.append((len(fit.primitives) - 1, 0))
    result = []
    for left, right in pairs:
        node = fit.primitives[left][-1]
        arriving = _arriving(starts[left], fit.primitives[left])
        leaving = _leaving(node, fit.primitives[right])
        dot = arriving[0] * leaving[0] + arriving[1] * leaving[1]
        cross = arriving[0] * leaving[1] - arriving[1] * leaving[0]
        result.append((node, math.degrees(math.atan2(abs(cross), dot)),
                       fit.primitives[left][0] + fit.primitives[right][0]))
    return result


def max_sample_distance(fit: ChainFit, samples) -> float:
    """Largest distance from any source sample to the densely evaluated fit.

    Each piece is evaluated at 4000 parameters, so the discretisation adds far less
    than the tolerances asserted against it.
    """
    t = np.linspace(0.0, 1.0, 4000)[:, None]
    dense = [np.asarray([fit.start], dtype=float)]
    start = np.asarray(fit.start, dtype=float)
    for primitive in fit.primitives:
        end = np.asarray(primitive[-1], dtype=float)
        if primitive[0] == "L":
            dense.append(start + t * (end - start))
        else:
            c1, c2 = np.asarray(primitive[1], dtype=float), np.asarray(primitive[2], dtype=float)
            dense.append((1 - t) ** 3 * start + 3 * (1 - t) ** 2 * t * c1
                         + 3 * (1 - t) * t ** 2 * c2 + t ** 3 * end)
        start = end
    curve = np.concatenate(dense)
    points = np.asarray(samples, dtype=float)
    distances = np.full(len(points), np.inf)
    for chunk in range(0, len(curve), 4096):
        part = curve[chunk:chunk + 4096]
        distances = np.minimum(distances, np.sqrt(
            ((points[:, None, :] - part[None, :, :]) ** 2).sum(axis=2)).min(axis=1))
    return float(distances.max())


def closed_ring(points):
    points = [tuple(map(float, point)) for point in points]
    return [*points, points[0]]


def ellipse(rx, ry, count, centre=(50.0, 50.0), phase=0.3):
    angles = phase + np.linspace(0.0, 2.0 * math.pi, count, endpoint=False)
    return closed_ring(zip(centre[0] + rx * np.cos(angles), centre[1] + ry * np.sin(angles)))


def rasterised_disc(radius, centre=(40.0, 40.0)):
    """The pixel-edge boundary of a rasterised disc: a staircase ring."""
    size = int(centre[0] * 2)
    yy, xx = np.mgrid[0:size, 0:size]
    mask = (xx + 0.5 - centre[0]) ** 2 + (yy + 0.5 - centre[1]) ** 2 <= radius * radius
    edges = {}
    for y, x in zip(*np.nonzero(mask)):
        for a, b, inside in (((x, y), (x + 1, y), (y > 0 and mask[y - 1, x])),
                             ((x + 1, y), (x + 1, y + 1), (x + 1 < size and mask[y, x + 1])),
                             ((x + 1, y + 1), (x, y + 1), (y + 1 < size and mask[y + 1, x])),
                             ((x, y + 1), (x, y), (x > 0 and mask[y, x - 1]))):
            if not inside:
                edges[a] = b
    start = min(edges)
    ring, cursor = [start], edges[start]
    while cursor != start:
        ring.append(cursor)
        cursor = edges[cursor]
    return closed_ring(ring)


class SyntheticChainTests(unittest.TestCase):
    def assert_all_smooth(self, fit, closed):
        turns = join_turns(fit, closed)
        self.assertTrue(turns)
        for node, turn, kinds in turns:
            self.assertLess(turn, SMOOTH_DEG, f"kink of {turn:.3f} deg at {node} ({kinds})")

    def test_circle_and_ellipse_joins_are_all_symmetric(self):
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=0.05)
        for ring in (ellipse(30, 30, 190), ellipse(40, 20, 190), ellipse(8, 8, 50)):
            fit = SharedChainFitter().fit(ring, settings)
            self.assertGreater(fit.metrics.cubics, 1)
            self.assertEqual(fit.metrics.lines, 0)
            self.assert_all_smooth(fit, closed=True)
            self.assertLessEqual(max_sample_distance(fit, ring), 0.05 + 1e-9)

    def test_rasterised_disc_staircase_is_smooth_and_within_tolerance(self):
        settings = TraceSettings(length_unit="px", max_deviation=2.0)
        for radius in (3.0, 5.0, 12.0, 30.0):
            ring = rasterised_disc(radius)
            fit = SharedChainFitter().fit(ring, settings)
            self.assertLessEqual(max_sample_distance(fit, ring), 1.5 + 1e-9)
            self.assert_all_smooth(fit, closed=True)

    def test_square_has_four_cusp_lines(self):
        side = [(float(x), 0.0) for x in range(20)] + [(20.0, float(y)) for y in range(20)] \
            + [(float(x), 20.0) for x in range(20, 0, -1)] + [(0.0, float(y)) for y in range(20, 0, -1)]
        # Start mid-side so the seam is not a corner.
        ring = closed_ring(side[7:] + side[:7])
        fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
        self.assertEqual(fit.metrics.cubics, 0)
        self.assertEqual(fit.metrics.lines, 4)
        turns = [turn for _, turn, _ in join_turns(fit, closed=True)]
        self.assertEqual(len(turns), 4)
        for turn in turns:
            self.assertAlmostEqual(turn, 90.0, places=6)
        # The closed chain is fitted from a corner, so the seam is no extra node.
        self.assertIn(fit.start, {(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)})

    def test_ring_seam_is_a_join_like_any_other(self):
        # The seam of a smooth ring (refitted from a natural node) is symmetric.
        ring = ellipse(25, 15, 140, phase=0.0)
        fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px", curve_fit_tolerance=0.1))
        seam = join_turns(fit, closed=True)[-1]
        self.assertEqual(seam[0], fit.start)
        self.assertIn(fit.start, ring)
        self.assertLess(seam[1], SMOOTH_DEG)
        # A seam on a confirmed corner is that corner: its sample moves to the
        # corner point and the fit starts there.
        corner_first = [(0.0, 0.0), *[(float(x), 0.0) for x in range(1, 16)],
                        *[(15.0, float(y)) for y in range(1, 16)],
                        *[(15.0 - 15.0 * math.sin(a), 15.0 * math.cos(a))
                          for a in np.linspace(0.05, math.pi / 2 - 0.05, 20)]]
        ring = closed_ring(corner_first)
        corners = _geometric_corners(np.asarray(ring), TraceSettings(length_unit="px"), closed=True)
        self.assertIn(0, corners)
        fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
        for node in (fit.start, fit.primitives[-1][-1]):
            self.assertAlmostEqual(math.dist(node, corners[0]), 0.0, places=9)

    def test_shallow_bend_of_two_straight_runs_is_one_smooth_curve(self):
        # Two 20 px straight runs meeting at a 20 degree turn: the old fitter split
        # at the apex into two lines with a 20 degree kink.
        heading = math.radians(20.0)
        first = [(float(x), 0.0) for x in range(21)]
        second = [(20.0 + s * math.cos(heading), s * math.sin(heading)) for s in range(1, 21)]
        chain = first + second
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=0.5, straight_line_max_deviation=0.5)
        fit = SharedChainFitter().fit(chain, settings)
        self.assertEqual(fit.metrics.lines, 0)
        self.assert_all_smooth(fit, closed=False)
        self.assertLessEqual(max_sample_distance(fit, chain), 0.5 + 1e-9)
        # A 70 degree bend between the same straight runs is a detector corner:
        # its one cusp is the corner, and the runs stay lines.
        sharp = replace_turn(first, 70.0)
        fit = SharedChainFitter().fit(sharp, settings)
        self.assertEqual(fit.metrics.lines, 2)
        self.assertAlmostEqual(join_turns(fit, closed=False)[0][1], 70.0, delta=1.0)

    def test_curve_meeting_a_line_aligns_its_handle_with_the_line(self):
        line = [(float(x), 0.0) for x in range(31)]
        arc = [(30.0 + 15.0 * math.sin(a), 15.0 - 15.0 * math.cos(a))
               for a in np.linspace(0.0, math.pi / 2, 25)[1:]]
        chain = line + arc
        fit = SharedChainFitter().fit(chain, TraceSettings(length_unit="px", curve_fit_tolerance=0.2))
        self.assertEqual(fit.primitives[0][0], "L")
        joins = join_turns(fit, closed=False)
        self.assertEqual(joins[0][2], "LC")
        self.assertLess(joins[0][1], SMOOTH_DEG)
        # The line stays straight and the curve's first handle continues it.
        start, end = np.asarray(fit.start), np.asarray(fit.primitives[0][1])
        handle = np.asarray(fit.primitives[1][1]) - end
        direction = (end - start) / np.linalg.norm(end - start)
        self.assertAlmostEqual(float(direction[0] * handle[1] - direction[1] * handle[0]), 0.0, places=9)
        self.assertGreater(float(direction @ handle), 0.0)
        self.assert_all_smooth(fit, closed=False)
        self.assertLessEqual(max_sample_distance(fit, chain), 0.2 + 1e-9)

    def test_tolerance_and_max_deviation_hold_on_noisy_blobs(self):
        rng = np.random.default_rng(7)
        for trial in range(5):
            angles = np.linspace(0.0, 2 * math.pi, 220, endpoint=False)
            radius = 30 + 6 * np.sin(3 * angles + trial) + 3 * np.cos(5 * angles)
            radius = radius + rng.normal(0.0, 0.3, len(angles))
            ring = closed_ring(zip(60 + radius * np.cos(angles), 60 + radius * np.sin(angles)))
            # A sharpened corner lies up to the corner sharpening allowance off the
            # samples by design; the blob's tight waists (a bend of radius 7-9 px
            # turning 60-80 degrees) are sharpened within the default 2 px. The
            # allowance is held to the tolerance here, so the bound is the fit's.
            for settings, bound in ((TraceSettings(length_unit="px", corner_sharpening_allowance=1.5), 1.5),
                                    (TraceSettings(length_unit="px", max_deviation=0.4), 0.4),
                                    (TraceSettings(length_unit="px", curve_fit_tolerance=_tightened(1.5, 4.0),
                                                   corner_sharpening_allowance=0.375), 0.375)):
                fit = SharedChainFitter().fit(ring, settings)
                self.assertLessEqual(max_sample_distance(fit, ring), bound + 1e-9)
                corners = set(_geometric_corners(np.asarray(ring), settings, closed=True).values())
                for node, turn, kinds in join_turns(fit, closed=True):
                    if node not in corners:
                        self.assertLess(turn, SMOOTH_DEG, f"kink of {turn:.3f} deg at {node} ({kinds})")

    def test_junctions_are_cusps_and_other_forced_nodes_are_symmetric(self):
        chain = [(float(x), 6.0 * math.sin(x / 12.0)) for x in range(60)]
        self.assertEqual(_geometric_corners(np.asarray(chain), TraceSettings(length_unit="px")), {})
        junction, endpoint, constraint = chain[13], chain[30], chain[45]
        fit = SharedChainFitter().fit(
            chain, TraceSettings(length_unit="px"),
            [HardAnchor(junction, "junction"), HardAnchor(endpoint, "endpoint")], [constraint])
        turns = {node: turn for node, turn, _ in join_turns(fit, closed=False)}
        for node in (junction, endpoint, constraint):
            self.assertIn(node, turns)
        # The junction splits the chain into independently fitted runs; the
        # endpoint anchor and the recovery constraint are forced symmetric nodes.
        self.assertGreater(turns[junction], SMOOTH_DEG)
        for node, turn in turns.items():
            if node != junction:
                self.assertLess(turn, SMOOTH_DEG, node)
        # Straight runs meeting at a forced node are refitted as one smooth curve.
        bend = [(float(x), 0.0) for x in range(21)] + [
            (20.0 + s * math.cos(0.3), s * math.sin(0.3)) for s in range(1, 21)]
        fit = SharedChainFitter().fit(bend, TraceSettings(length_unit="px", curve_fit_tolerance=0.5), (), [bend[20]])
        self.assertEqual(fit.metrics.lines, 0)
        self.assert_all_smooth(fit, closed=False)


def mask_ring(mask):
    """The pixel-edge boundary ring of a mask's single component."""
    height, width = mask.shape
    edges = {}
    for y, x in zip(*np.nonzero(mask)):
        for a, b, inside in (((x, y), (x + 1, y), y > 0 and mask[y - 1, x]),
                             ((x + 1, y), (x + 1, y + 1), x + 1 < width and mask[y, x + 1]),
                             ((x + 1, y + 1), (x, y + 1), y + 1 < height and mask[y + 1, x]),
                             ((x, y + 1), (x, y), x > 0 and mask[y, x - 1])):
            if not inside:
                edges[a] = b
    start = min(edges)
    ring, cursor = [start], edges[start]
    while cursor != start:
        ring.append(cursor)
        cursor = edges[cursor]
    return closed_ring(ring)


def sampled_circle(radius, snapped):
    count = max(16, int(2 * math.pi * radius))
    points = []
    for index in range(count):
        angle = 2 * math.pi * index / count
        x, y = 100 + radius * math.cos(angle), 100 + radius * math.sin(angle)
        if snapped:
            x, y = round(x * 2) / 2, round(y * 2) / 2
        points.append((x, y))
    return closed_ring(points)


def sampled_polygon(sides, radius=30.0):
    vertices = [(100 + radius * math.cos(2 * math.pi * k / sides),
                 100 + radius * math.sin(2 * math.pi * k / sides)) for k in range(sides)]
    points = []
    for a, b in zip(vertices, vertices[1:] + vertices[:1]):
        steps = int(math.dist(a, b))
        points += [(a[0] + (b[0] - a[0]) * j / steps, a[1] + (b[1] - a[1]) * j / steps)
                   for j in range(steps)]
    return closed_ring(points)


def square_ring(side):
    points = ([(float(x), 0.0) for x in range(side)] + [(float(side), float(y)) for y in range(side)]
              + [(float(side - x), float(side)) for x in range(side)]
              + [(0.0, float(side - y)) for y in range(side)])
    return closed_ring(points)


def resampled(outline, snapped=False):
    """A closed outline (vertices, not repeated) sampled about every pixel, its
    vertices kept; ``snapped`` rounds the samples to half pixels."""
    points = []
    for a, b in zip(outline, outline[1:] + outline[:1]):
        steps = max(1, round(math.dist(a, b)))
        points += [(a[0] + (b[0] - a[0]) * j / steps, a[1] + (b[1] - a[1]) * j / steps)
                   for j in range(steps)]
    if snapped:
        rounded = []
        for x, y in points:
            point = (round(x * 2) / 2, round(y * 2) / 2)
            if not rounded or point != rounded[-1]:
                rounded.append(point)
        points = rounded[:-1] if rounded[0] == rounded[-1] else rounded
    return closed_ring(points)


def arc(cx, cy, radius, start, end, step=0.2):
    """Points on a circle from angle ``start`` to ``end`` (either direction)."""
    count = max(2, int(abs(end - start) * radius / step) + 1)
    return [(cx + radius * math.cos(start + (end - start) * t / (count - 1)),
             cy + radius * math.sin(start + (end - start) * t / (count - 1)))
            for t in range(count)]


def leaf(length, bulge, angle=0.3):
    """Two circular arcs through two tips ``length`` apart, each bulging ``bulge``:
    each tip turns by 180 - 4 atan(bulge / (length / 2)) degrees."""
    half = length / 2
    radius = (half * half + bulge * bulge) / (2 * bulge)
    offset = radius - bulge
    theta = math.asin(half / radius)
    points = (arc(0, -offset, radius, math.pi / 2 + theta, math.pi / 2 - theta)
              + arc(0, offset, radius, -math.pi / 2 + theta, -math.pi / 2 - theta))
    c, s = math.cos(angle), math.sin(angle)
    return [(100 + x * c - y * s, 100 + x * s + y * c) for x, y in points]


def crescent(outer, inner, shift):
    """A disc of radius ``outer`` less a disc of radius ``inner`` shifted right."""
    along = (outer ** 2 - inner ** 2 + shift ** 2) / (2 * shift)
    height = math.sqrt(outer ** 2 - along ** 2)
    outer_tip = math.atan2(height, along)
    inner_tip = math.atan2(height, along - shift)
    points = (arc(0, 0, outer, outer_tip, 2 * math.pi - outer_tip)
              + arc(shift, 0, inner, 2 * math.pi - inner_tip, inner_tip)[1:-1])
    return [(150 + x, 150 + y) for x, y in points]


def gothic_arch(width, wall):
    """Two arcs of radius ``width`` meeting at an apex (a 60-degree turn) on
    vertical walls (joined smoothly) and a base (two 90-degree corners)."""
    x0, y0 = 60.0, 160.0
    spring = y0 - wall
    return ([(x0, y0), (x0 + width, y0)] + [(x0 + width, y0 - t) for t in (wall / 2,)]
            + arc(x0, spring, width, 0, -math.pi / 3)
            + arc(x0 + width, spring, width, -2 * math.pi / 3, -math.pi)[1:]
            + [(x0, y0 - wall / 2)])


def heart(side):
    """A square turned 45 degrees with half-discs on its two upper sides: a
    90-degree bottom point and a 90-degree notch where the two lobes meet."""
    from shapely.geometry import Point as ShapelyPoint, Polygon as ShapelyPolygon
    from shapely.ops import unary_union
    half = side / math.sqrt(2)
    top, right, bottom, left = (150, 150 - half), (150 + half, 150), (150, 150 + half), (150 - half, 150)
    discs = [ShapelyPoint((top[0] + p[0]) / 2, (top[1] + p[1]) / 2).buffer(side / 2, quad_segs=256)
             for p in (left, right)]
    return list(unary_union([ShapelyPolygon([top, right, bottom, left]), *discs]).exterior.coords)[:-1]


def rounded_rectangle(width, height, radius):
    points = []
    for cx, cy, start in ((50 + width - radius, 50 + radius, -math.pi / 2),
                          (50 + width - radius, 50 + height - radius, 0.0),
                          (50 + radius, 50 + height - radius, math.pi / 2),
                          (50 + radius, 50 + radius, math.pi)):
        points += arc(cx, cy, radius, start, start + math.pi / 2) if radius else [(cx, cy)]
    return points


def staircase(run, rise, steps, snapped):
    """An open pixel staircase ``run`` px across then ``rise`` up, repeated; or,
    ``snapped``, the diagonal it approximates sampled every pixel and rounded to
    half pixels."""
    points = [(0.0, 0.0)]
    for _ in range(steps):
        for _ in range(run):
            points.append((points[-1][0] + 1, points[-1][1]))
        for _ in range(rise):
            points.append((points[-1][0], points[-1][1] + 1))
    if snapped:
        end = points[-1]
        count = len(points)
        rounded = [(round(end[0] * t / (count - 1) * 2) / 2, round(end[1] * t / (count - 1) * 2) / 2)
                   for t in range(count)]
        points = [point for k, point in enumerate(rounded) if k == 0 or point != rounded[k - 1]]
    return points


class CornerDetectorTests(unittest.TestCase):
    def corners(self, ring, threshold=60.0):
        return _geometric_corners(np.asarray(ring, dtype=float),
                                  TraceSettings(length_unit="px", corner_threshold=threshold), closed=True)

    def detect(self, points, settings, closed=True):
        return _geometric_corners(np.asarray(points, dtype=float), settings, closed=closed)

    def resolved(self, reference, threshold, **lengths):
        return resolve_trace_lengths(TraceSettings(corner_threshold=threshold, **lengths),
                                     reference)

    def test_circles_and_gentle_curves_get_no_corners_at_any_threshold_or_size(self):
        # A circle's two sides are one arc, so it has no corner however tight. Its
        # samples are exact, half-pixel snapped, or (radius 6 px and more) the
        # pixel-edge staircase of a rasterised disc. The lengths are the default
        # thousandths resolved on images of 600, 2000 and 6000 px, so the same
        # circle is a large or a tiny feature. This replaces the scale-invariance
        # check: without the one-arc test, snapped circles got corners at all
        # three sizes.
        # Gentle curves, the same artwork on each image (sized with it): ellipses of
        # 50 x 30 and 40 x 30 px and a sine wave on a 2000 px image.
        def gentle(scale):
            t = np.linspace(0, 2 * math.pi, int(400 * scale), endpoint=False)
            return [resampled(list(zip(100 + rx * scale * np.cos(t), 100 + ry * scale * np.sin(t))),
                              snapped)
                    for rx, ry in ((50, 30), (40, 30)) for snapped in (False, True)]

        for reference in (600, 2000, 6000):
            scale = reference / 2000
            curves = gentle(scale)
            wave = [(float(x), 12.0 * scale * math.sin(x / (15.0 * scale)))
                    for x in np.arange(0.0, 200.0 * scale, 1.0)]
            for threshold in (30.0, 45.0, 60.0):
                settings = self.resolved(reference, threshold)
                for radius in (3, 4, 5, 6, 7, 8, 9, 10, 12, 14, 16, 20, 30, 50, 100):
                    for snapped in (False, True):
                        self.assertEqual(self.detect(sampled_circle(radius, snapped), settings), {},
                                         (reference, threshold, radius, snapped))
                    if radius >= 6:
                        yy, xx = np.mgrid[0:2 * radius + 8, 0:2 * radius + 8]
                        disc = (xx + 0.5 - radius - 4) ** 2 + (yy + 0.5 - radius - 4) ** 2 <= radius ** 2
                        self.assertEqual(self.detect(mask_ring(disc), settings), {},
                                         (reference, threshold, radius, "raster"))
                for ring in curves:
                    self.assertEqual(self.detect(ring, settings), {}, (reference, threshold))
                self.assertEqual(self.detect(wave, settings, closed=False), {},
                                 (reference, threshold))

    def test_pixel_staircases_are_one_smooth_side(self):
        settings = TraceSettings(length_unit="px")
        for run, rise in ((1, 1), (2, 1), (3, 1), (4, 1), (5, 1), (8, 1), (1, 2), (1, 3)):
            for snapped in (False, True):
                chain = staircase(run, rise, max(8, 60 // (run + rise)), snapped)
                self.assertEqual(self.detect(chain, settings, closed=False), {},
                                 (run, rise, snapped))

    def test_curve_to_curve_cusps_are_found(self):
        # Crescent tips, leaf points, a heart's notch and bottom point, and a
        # gothic arch's apex (a 60-degree turn, so only below a 60 threshold; its
        # base corners are two more): no straight side is needed.
        shapes = [(leaf(60, 8), 2), (leaf(40, 5), 2), (leaf(100, 12), 2), (leaf(30, 3), 2),
                  (crescent(40, 30, 20), 2), (crescent(30, 22, 14), 2), (crescent(60, 45, 30), 2),
                  (heart(30), 2), (heart(60), 2)]
        for threshold in (30.0, 45.0, 60.0):
            settings = TraceSettings(length_unit="px", corner_threshold=threshold)
            cases = shapes + ([(gothic_arch(40, 30), 3)] if threshold < 60 else [])
            for number, (outline, count) in enumerate(cases):
                for snapped in (False, True):
                    found = self.detect(resampled(outline, snapped), settings)
                    self.assertEqual(len(found), count, (threshold, number, snapped))
        # The cusp is where the two arcs cross: a leaf's tips.
        tips = [(100 + 30 * math.cos(0.3) * side, 100 + 30 * math.sin(0.3) * side)
                for side in (-1, 1)]
        found = self.detect(resampled(leaf(60, 8)), TraceSettings(length_unit="px"))
        self.assertEqual(len(found), 2)
        for cusp in found.values():
            self.assertLess(min(math.dist(cusp, tip) for tip in tips), 0.2)

    def test_rounded_corners_sharpen_within_the_allowance_and_stay_round_beyond_it(self):
        # A 90-degree round of radius r lies r (sqrt 2 - 1) from its sharp point, so
        # the allowance sharpens rounds up to 2.41 allowances; the tip skip keeps the
        # round out of the side windows, so nothing else vetoes them. Larger rounds
        # keep no corner at all.
        for allowance in (1.0, 2.0, 4.0):
            settings = TraceSettings(length_unit="px", corner_sharpening_allowance=allowance)
            for factor in (0.0, 0.5, 1.0, 1.5, 2.0, 2.2, 2.7, 3.0, 4.0, 6.0):
                radius = factor * allowance
                for snapped in (False, True):
                    found = self.detect(resampled(rounded_rectangle(80, 60, radius), snapped),
                                        settings)
                    self.assertEqual(len(found), 4 if factor <= 2.2 else 0,
                                     (allowance, factor, snapped))
                    if factor <= 2.2 and not snapped:
                        # Sharpened to the rectangle's corners.
                        for cusp in found.values():
                            self.assertLess(min(math.dist(cusp, corner) for corner in
                                                ((50, 50), (130, 50), (130, 110), (50, 110))),
                                            0.2, (allowance, factor))

    def test_squares_and_polygons_keep_every_corner(self):
        for threshold in (30.0, 45.0, 60.0):
            for side in (10, 20, 40):
                self.assertEqual(len(self.corners(square_ring(side), threshold)), 4,
                                 (threshold, side))
            for sides in (3, 4, 5):
                self.assertEqual(len(self.corners(sampled_polygon(sides), threshold)), sides,
                                 (threshold, sides))
        # A hexagon turns by exactly 60 degrees, so it needs a threshold below that.
        for threshold in (30.0, 45.0):
            self.assertEqual(len(self.corners(sampled_polygon(6), threshold)), 6)

    def test_an_edge_of_the_minimum_length_between_corners_keeps_both(self):
        def notch(width):
            mask = np.zeros((40, 50), bool)
            mask[5:35, 5:45] = True
            mask[5:15, 25 - width // 2:25 - width // 2 + width] = False
            return mask

        def letter_h(width):
            mask = np.zeros((50, 50), bool)
            mask[5:45, 5:45] = True
            mask[5:20, 25 - width // 2:25 - width // 2 + width] = False
            mask[30:45, 25 - width // 2:25 - width // 2 + width] = False
            return mask

        def bar(width):
            mask = np.zeros((30, 60), bool)
            mask[10:10 + width, 5:55] = True
            return mask

        # Edges of 4 px (the default minimum straight length) and more keep both
        # corners; shorter ones round off (a notch's floor merges into one
        # U-shaped curve, a thin bar's end is one curve).
        for width in (4, 5, 6, 8):
            self.assertEqual(len(self.corners(mask_ring(notch(width)))), 8, width)
            self.assertEqual(len(self.corners(mask_ring(letter_h(width)))), 12, width)
            self.assertEqual(len(self.corners(mask_ring(bar(width)))), 4, width)
        for width in (2, 3):
            self.assertEqual(len(self.corners(mask_ring(notch(width)))), 6, width)
            self.assertEqual(len(self.corners(mask_ring(letter_h(width)))), 8, width)
            self.assertEqual(len(self.corners(mask_ring(bar(width)))), 0, width)

    def test_a_notch_fits_with_square_corners_and_no_other_cusps(self):
        mask = np.zeros((40, 50), bool)
        mask[5:35, 5:45] = True
        mask[5:15, 23:29] = False
        ring = mask_ring(mask)
        fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
        self.assertEqual(fit.metrics.cubics, 0)
        self.assertEqual(fit.metrics.lines, 8)
        for _, turn, _ in join_turns(fit, closed=True):
            self.assertAlmostEqual(turn, 90.0, places=6)


class HandleTests(unittest.TestCase):
    def test_chord_monotone_rejects_loops_and_reversals(self):
        self.assertTrue(_chord_monotone_nb(0, 0, 1, 1, 2, 1, 3, 0))
        # Crossing handles: a loop.
        self.assertFalse(_chord_monotone_nb(0, 0, 4, 2, -1, 2, 3, 0))
        # The end handle lies beyond the end: the curve doubles back.
        self.assertFalse(_chord_monotone_nb(0, 0, 1, 0, 4, 0.2, 3, 0))
        # Perpendicular handles (a half circle) are the limit, still monotone.
        self.assertTrue(_chord_monotone_nb(0, 0, 0, 2, 3, 2, 3, 0))

    def test_fixed_handles_are_never_near_zero(self):
        rng = np.random.default_rng(3)
        for _ in range(40):
            count = int(rng.integers(3, 12))
            points = np.cumsum(rng.normal(0.0, 1.0, (count, 2)) + (1.0, 0.0), axis=0)
            angle_in, angle_out = rng.uniform(-1.0, 1.0, 2)
            tangent_in = np.array((math.cos(angle_in), math.sin(angle_in)))
            tangent_out = np.array((math.cos(angle_out), math.sin(angle_out)))
            t = np.empty(count)
            fitted = np.empty((count, 2))
            c1x, c1y, c2x, c2y = _constrained_cubic_nb(
                points, 0, count - 1, 1.5, 2.0, True, *tangent_in, True, *tangent_out, t, fitted,
                False)
            chord = float(np.hypot(*(points[-1] - points[0])))
            first = float(np.hypot(c1x - points[0, 0], c1y - points[0, 1]))
            last = float(np.hypot(points[-1, 0] - c2x, points[-1, 1] - c2y))
            for handle in (first, last):
                self.assertLessEqual(handle, chord + 1e-9)
            # Only the padded sample box may shorten a handle below a tenth of the chord.
            padding = max(1.5, min(0.25 * float(np.ptp(points, axis=0).max()), 2.0))
            box_low, box_high = points.min(axis=0) - padding, points.max(axis=0) + padding
            for handle, control in ((first, (c1x, c1y)), (last, (c2x, c2y))):
                on_box = np.isclose(control, box_low).any() or np.isclose(control, box_high).any()
                self.assertTrue(handle >= 0.1 * chord - 1e-9 or on_box, (handle, chord))


def replace_turn(first, degrees):
    heading = math.radians(degrees)
    end = first[-1]
    return first + [(end[0] + s * math.cos(heading), end[1] + s * math.sin(heading))
                    for s in range(1, 21)]


if __name__ == "__main__":
    unittest.main()
