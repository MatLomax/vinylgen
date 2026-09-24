"""Every emitted piece keeps its samples within its bound, measured on the geometry written.

A straight run is emitted as a segment between its end nodes (after any
straight-line snap and corner move), so its acceptance is measured on that
segment, not on a best-fit line through the samples: every sample it replaces
lies within ``min(straight_line_max_deviation, max_deviation)`` of it. A curve
run's samples lie within the curve-fit tolerance of its pieces, except that a
line inside it that snapped may use the straight-line bound. A confirmed
corner's own sample lies within the corner sharpening allowance of its node;
that allowance may exceed ``max_deviation``, and applies to that one sample only.
"""

import math
import os
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from vinylgen import curve_fit as cf
from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import ChainFit, HardAnchor, SharedChainFitter, _tightened
from vinylgen.trace_lengths import resolve_trace_lengths

from test_smooth_joins import CORPUS, closed_ring, rounded_rectangle

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Float slack on distances that are compared with the bound they were accepted at.
EPS = 1e-7
# Cubic pieces are measured to a 256-segment polyline; its chordal error on a
# corpus piece is thousandths of a pixel.
CUBIC_SEGMENTS = 256


def straight_bound(settings: TraceSettings) -> float:
    deviation = float(settings.straight_line_max_deviation)
    if settings.max_deviation is not None:
        deviation = min(deviation, float(settings.max_deviation))
    return deviation


def _polyline(start, primitive) -> np.ndarray:
    start = np.asarray(start, dtype=float)
    end = np.asarray(primitive[-1], dtype=float)
    if primitive[0] == "L":
        return np.stack((start, end))
    c1, c2 = np.asarray(primitive[1], dtype=float), np.asarray(primitive[2], dtype=float)
    t = np.linspace(0.0, 1.0, CUBIC_SEGMENTS + 1)[:, None]
    return ((1 - t) ** 3 * start + 3 * (1 - t) ** 2 * t * c1
            + 3 * (1 - t) * t ** 2 * c2 + t ** 3 * end)


def distances_to_pieces(samples, pieces) -> np.ndarray:
    """Each sample's distance to the nearest of ``pieces`` (``(start, primitive)``)."""
    points = np.asarray(samples, dtype=float).reshape(-1, 2)
    best = np.full(len(points), np.inf)
    for start, primitive in pieces:
        line = _polyline(start, primitive)
        a, b = line[:-1], line[1:]
        ab = b - a
        length2 = (ab * ab).sum(axis=1)
        rel = points[:, None, :] - a[None, :, :]
        safe = np.where(length2 > 0.0, length2, 1.0)
        t = np.clip((rel * ab[None]).sum(axis=2) / safe[None], 0.0, 1.0)
        t = np.where(length2[None] > 0.0, t, 0.0)
        nearest = a[None] + t[..., None] * ab[None]
        best = np.minimum(best, np.sqrt(((points[:, None, :] - nearest) ** 2).sum(axis=2)).min(axis=1))
    return best


def run_violations(fit: ChainFit, canonical, settings: TraceSettings) -> list[tuple]:
    """Every breach of the per-piece bounds in a forward (canonical) chain fit.

    Returns ``(kind, distance, bound, sample index)`` rows: ``straight`` for a
    straight run's sample beyond ``straight_bound``, ``curve`` for a curve
    run's sample beyond the curve tolerance (or the straight bound when snaps
    are on, since a snapped line inside a curve run may use it), ``corner``
    for a corner node further than the allowance from its sample, and
    ``max_deviation`` for any non-corner sample beyond ``max_deviation``.
    """
    canonical = [tuple(map(float, point)) for point in canonical]
    closed = len(canonical) >= 4 and canonical[0] == canonical[-1]
    count = len(canonical) - 1 if closed else len(canonical)
    straight = straight_bound(settings)
    tolerance = cf._curve_tolerance(settings)
    curve = max(tolerance, straight) if settings.straight_line_snap != "off" else tolerance
    allowance = cf._sharpening_allowance(settings)
    ceiling = math.inf if settings.max_deviation is None else float(settings.max_deviation)
    corners = {row.source_start: row.max_error for row in fit.diagnostics
               if row.classification == "corner"}
    out = [("corner", error, allowance, index) for index, error in corners.items()
           if error > allowance + EPS]
    cursor = fit.start
    pieces = []
    for primitive in fit.primitives:
        pieces.append((cursor, primitive))
        cursor = primitive[-1]
    position = 0
    for row in fit.diagnostics:
        if row.classification == "corner":
            continue
        own = pieces[position:position + row.primitives]
        position += row.primitives
        start, end = row.source_start, row.source_end
        if closed and end <= start:
            indices = [*range(start, count), *range(0, end + 1)]
        else:
            indices = list(range(start, end + 1))
        indices = [index for index in indices if index not in corners]
        if not indices:
            continue
        distances = distances_to_pieces([canonical[index] for index in indices], own)
        bound = straight if row.classification == "straight" else curve
        for index, distance in zip(indices, distances):
            if distance > bound + EPS:
                out.append((row.classification, float(distance), bound, index))
            if distance > ceiling + EPS:
                out.append(("max_deviation", float(distance), ceiling, index))
    assert position == len(pieces), "diagnostics do not cover every piece"
    return out


def fit_forward(points, settings, anchors=(), constraints=()) -> tuple[ChainFit, tuple]:
    points = tuple(tuple(map(float, point)) for point in points)
    canonical = points if points <= tuple(reversed(points)) else tuple(reversed(points))
    return SharedChainFitter().fit(canonical, settings, anchors, constraints), canonical


# cosmic-jellyfish at default thousandths (2992 px): an open 66-sample run from an
# image-edge endpoint to a junction, bowed along its length. Its total-least-squares
# line is within the 2.39 px straight deviation, but its chord, the segment that
# was written for it, missed a sample by 4.52 px.
JELLYFISH_RUN = [
    (2656.2214133673906, 1495.0), (2657.0, 1495.0), (2657.0, 1494.0),
    (2657.0, 1493.0), (2657.8426877470356, 1493.0), (2658.0646264615293, 1492.0),
    (2658.286565176023, 1491.0), (2659.0, 1491.0), (2659.0, 1490.0),
    (2659.0, 1489.0), (2659.8346051125523, 1489.0), (2659.990575958103, 1488.0),
    (2660.146546803653, 1487.0), (2660.20251084712, 1486.0), (2660.258474890587, 1485.0),
    (2661.0, 1485.0), (2661.0, 1484.0), (2661.0, 1483.0),
    (2661.55, 1483.0), (2661.55, 1482.0), (2661.55, 1481.0),
    (2661.5503164556962, 1480.0), (2661.5506329113923, 1479.0), (2661.566437951023, 1478.0),
    (2661.5822429906543, 1477.0), (2661.6099068675517, 1476.0), (2661.6375707444495, 1475.0),
    (2661.663889965712, 1474.0), (2661.6902091869742, 1473.0), (2661.762129628044, 1472.0),
    (2661.834050069114, 1471.0), (2662.046164784332, 1470.0), (2662.25827949955, 1469.0),
    (2663.0, 1469.0), (2663.0, 1468.2691707012339), (2664.0, 1467.909585350617),
    (2665.0, 1467.55), (2665.0, 1467.0), (2665.55, 1467.0),
    (2665.601573653472, 1466.0), (2665.6531473069435, 1465.0), (2665.627325533171, 1464.0),
    (2665.6015037593984, 1463.0), (2665.7307729656245, 1462.0), (2665.86004217185, 1461.0),
    (2665.914293898133, 1460.0), (2665.968545624416, 1459.0), (2665.9674175552536, 1458.0),
    (2665.9662894860917, 1457.0), (2665.968959790068, 1456.0), (2665.971630094044, 1455.0),
    (2665.9714543305304, 1454.0), (2665.9712785670167, 1453.0), (2665.961905106293, 1452.0),
    (2665.9525316455697, 1451.0), (2665.940748023432, 1450.0), (2665.9289644012947, 1449.0),
    (2665.9092685254336, 1448.0), (2665.8895726495725, 1447.0), (2665.8578844443173, 1446.0),
    (2665.8261962390616, 1445.0), (2665.7942618276174, 1444.0), (2665.762327416174, 1443.0),
    (2665.701669262383, 1442.0), (2665.641011108592, 1441.0), (2665.6132728498937, 1439.55),
]


def jellyfish_settings() -> TraceSettings:
    return resolve_trace_lengths(TraceSettings(), 2992)


def tls_error(points) -> float:
    """Max distance of ``points`` from their total-least-squares line."""
    array = np.asarray(points, dtype=float)
    centred = array - array.mean(axis=0)
    direction = np.linalg.svd(centred)[2][0]
    return float(np.abs(centred[:, 0] * direction[1] - centred[:, 1] * direction[0]).max())


def pixel_path(function, count: int) -> list:
    """The 4-connected pixel staircase through ``round(function(t))``, t in [0, 1]."""
    out: list = []
    for k in range(count + 1):
        x, y = function(k / count)
        target = (float(round(x)), float(round(y)))
        if not out:
            out.append(target)
            continue
        px, py = out[-1]
        while (px, py) != target:
            if px != target[0]:
                px += math.copysign(1.0, target[0] - px)
            else:
                py += math.copysign(1.0, target[1] - py)
            out.append((px, py))
    return out


def bowed_staircase(sagitta: float, run=60.0, rise=20.0) -> list:
    """A pixel staircase along a diagonal bowed sideways by ``sagitta`` px."""
    nx, ny = -rise / math.hypot(run, rise), run / math.hypot(run, rise)
    return pixel_path(lambda t: (run * t + nx * sagitta * 4 * t * (1 - t),
                                 rise * t + ny * sagitta * 4 * t * (1 - t)), 400)


def bulged_run(height: float, length=200, width=30.0) -> list:
    """A long horizontal run on half-pixel samples with a Gaussian bump mid-way."""
    return [(float(x), round(height * math.exp(-((x - length / 2) / (width / 2)) ** 2) * 2) / 2)
            for x in range(length + 1)]


def rotated(points, degrees, centre=(100.0, 100.0)) -> list:
    angle = math.radians(degrees)
    cx, cy = centre
    return [(cx + (x - cx) * math.cos(angle) - (y - cy) * math.sin(angle),
             cy + (x - cx) * math.sin(angle) + (y - cy) * math.cos(angle)) for x, y in points]


def densified(ring) -> list:
    """A ring sampled at least every pixel along each edge."""
    out = []
    for a, b in zip(ring, ring[1:] + ring[:1]):
        steps = max(1, int(math.dist(a, b)))
        out += [(a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)
                for k in range(steps)]
    return out


def axis_lines(fit: ChainFit) -> int:
    starts = [fit.start, *(primitive[-1] for primitive in fit.primitives)]
    return sum(1 for start, primitive in zip(starts, fit.primitives)
               if primitive[0] == "L" and (start[0] == primitive[1][0] or start[1] == primitive[1][1]))


class StraightRunBoundTests(unittest.TestCase):
    def assert_within_bounds(self, fit, canonical, settings):
        self.assertEqual(run_violations(fit, canonical, settings), [])

    def test_jellyfish_run_whose_chord_misses_its_samples_is_not_one_line(self):
        settings = jellyfish_settings()
        bound = straight_bound(settings)
        self.assertAlmostEqual(bound, 2.3936)
        # The case: the best-fit line is within the deviation, the chord is not.
        self.assertLessEqual(tls_error(JELLYFISH_RUN), bound)
        self.assertGreater(cf._distance_to_line(np.asarray(JELLYFISH_RUN))[0], 4.5)
        anchors = [HardAnchor(JELLYFISH_RUN[0], "endpoint"),
                   HardAnchor(JELLYFISH_RUN[-1], "junction")]
        fit, canonical = fit_forward(JELLYFISH_RUN, settings, anchors)
        self.assertEqual(canonical, tuple(JELLYFISH_RUN))
        self.assertNotEqual(fit.primitives, (("L", JELLYFISH_RUN[-1]),))
        self.assert_within_bounds(fit, canonical, settings)

    def test_section_line_tests_the_chord_it_writes(self):
        settings = jellyfish_settings()
        straight, error = cf._section_line(np.asarray(JELLYFISH_RUN), settings)
        self.assertFalse(straight)
        self.assertAlmostEqual(error, cf._distance_to_line(np.asarray(JELLYFISH_RUN))[0])
        run = np.asarray(JELLYFISH_RUN[:20])
        straight, error = cf._section_line(run, settings)
        self.assertTrue(straight)
        self.assertLessEqual(error, straight_bound(settings))

    def test_bowed_staircase_is_fitted_within_the_straight_deviation(self):
        settings = TraceSettings(length_unit="px", straight_line_max_deviation=2.0, max_deviation=3.0)
        points = bowed_staircase(2.0)
        self.assertLessEqual(tls_error(points), 2.0)
        self.assertGreater(cf._distance_to_line(np.asarray(points))[0], 2.0)
        fit, canonical = fit_forward(points, settings)
        self.assertNotIn("straight", [row.classification for row in fit.diagnostics])
        self.assert_within_bounds(fit, canonical, settings)
        # A gentler bow whose chord is within the deviation stays one line.
        points = bowed_staircase(1.5)
        fit, canonical = fit_forward(points, settings)
        self.assertEqual([row.classification for row in fit.diagnostics], ["straight"])
        self.assertEqual(len(fit.primitives), 1)
        self.assert_within_bounds(fit, canonical, settings)

    def test_long_run_with_a_mid_bulge_keeps_the_bulge(self):
        settings = TraceSettings(length_unit="px", straight_line_max_deviation=1.8)
        points = bulged_run(2.2)
        self.assertLessEqual(tls_error(points), 1.8)
        self.assertGreater(cf._distance_to_line(np.asarray(points))[0], 1.8)
        fit, canonical = fit_forward(points, settings)
        self.assertNotEqual(fit.primitives, (("L", points[-1]),))
        self.assert_within_bounds(fit, canonical, settings)

    def test_a_section_snap_never_takes_samples_beyond_max_deviation(self):
        # An 80 x 50 rectangle turned 1.9 degrees: snapping its sides moves them up
        # to 1.33 px from their samples, inside a 3 px straight deviation but
        # beyond a 1 px maximum deviation. The 3 px corner allowance lets the
        # corners follow, so only the deviation bound can refuse the snap.
        ring = closed_ring(rotated(densified([(60, 75), (140, 75), (140, 125), (60, 125)]), 1.9))
        loose = TraceSettings(length_unit="px", straight_line_max_deviation=3.0, snap_angle=2.0,
                              corner_sharpening_allowance=3.0)
        fit, canonical = fit_forward(ring, loose)
        self.assertEqual(axis_lines(fit), 4)
        self.assertGreater(max(distances_to_pieces(canonical, _pieces(fit))), 1.0)
        self.assert_within_bounds(fit, canonical, loose)
        capped = replace(loose, max_deviation=1.0)
        fit, canonical = fit_forward(ring, capped)
        self.assertLess(axis_lines(fit), 4)
        self.assertLessEqual(max(distances_to_pieces(canonical, _pieces(fit))), 1.0 + EPS)
        self.assert_within_bounds(fit, canonical, capped)

    def test_a_snap_inside_a_curve_run_never_takes_samples_beyond_max_deviation(self):
        # A rounded rectangle turned one degree: its sides are straight units
        # inside one curve run, and snapping them leaves samples about 1 px off.
        ring = closed_ring(rotated(_rounded_sides(90.0, 60.0, 15.0), 1.0, centre=(0.0, 0.0)))
        loose = TraceSettings(length_unit="px", curve_fit_tolerance=0.5)
        fit, canonical = fit_forward(ring, loose)
        self.assertEqual(fit.corners, ())
        self.assertGreaterEqual(axis_lines(fit), 2)
        self.assertGreater(max(distances_to_pieces(canonical, _pieces(fit))), 0.9)
        self.assert_within_bounds(fit, canonical, loose)
        capped = replace(loose, max_deviation=0.4)
        fit, canonical = fit_forward(ring, capped)
        self.assertEqual(axis_lines(fit), 0)
        self.assertLessEqual(max(distances_to_pieces(canonical, _pieces(fit))), 0.4 + EPS)
        self.assert_within_bounds(fit, canonical, capped)

    def test_a_sharpened_corner_may_exceed_max_deviation_only_at_its_own_sample(self):
        # Rounds of radius 5 sharpen within a 3 px allowance, their cusps up to
        # 1.95 px from the sample, beyond the 1.5 px maximum deviation. Only the
        # corner's own sample may be that far: the sides next to it, snapped or
        # not, keep every other sample within their bounds.
        settings = TraceSettings(length_unit="px", max_deviation=1.5,
                                 corner_sharpening_allowance=3.0,
                                 straight_line_max_deviation=1.6)
        for degrees in (0.0, 0.5, 1.0):
            ring = closed_ring(rotated(densified(rounded_rectangle(60, 40, 5.0)), degrees))
            fit, canonical = fit_forward(ring, settings)
            corner_errors = [row.max_error for row in fit.diagnostics
                             if row.classification == "corner"]
            self.assertEqual(len(corner_errors), 4)
            self.assertGreater(max(corner_errors), 1.5)
            self.assertLessEqual(max(corner_errors), 3.0)
            self.assert_within_bounds(fit, canonical, settings)


def _pieces(fit: ChainFit) -> list:
    starts = [fit.start, *(primitive[-1] for primitive in fit.primitives)]
    return list(zip(starts, fit.primitives))


def _rounded_sides(width, height, radius) -> list:
    """A rounded rectangle whose rounds and sides are sampled about every pixel."""
    centres = ((width - radius, radius), (width - radius, height - radius),
               (radius, height - radius), (radius, radius))
    points = []
    for quarter, (cx, cy) in enumerate(centres):
        start = -90.0 + 90.0 * quarter
        points += [(cx + radius * math.cos(math.radians(start + 3.0 * k)),
                    cy + radius * math.sin(math.radians(start + 3.0 * k))) for k in range(30)]
        ex = cx + radius * math.cos(math.radians(start + 90.0))
        ey = cy + radius * math.sin(math.radians(start + 90.0))
        nx, ny = centres[(quarter + 1) % 4]
        nx += radius * math.cos(math.radians(start + 90.0))
        ny += radius * math.sin(math.radians(start + 90.0))
        steps = int(math.hypot(nx - ex, ny - ey))
        points += [(ex + (nx - ex) * k / steps, ey + (ny - ey) * k / steps) for k in range(steps)]
    return [(20 + x, 20 + y) for x, y in points]


def _corpus_segmentation(subject: str) -> Path | None:
    return next((candidate for candidate in (
        _REPO_ROOT / "outputs" / subject / "segmentation.npz",
        _REPO_ROOT / ".tmp" / "stroke-baseline" / "after" / subject / "segmentation.npz")
        if candidate.exists()), None)


@unittest.skipUnless(os.environ.get("VINYLGEN_SLOW_TESTS") == "1",
                     "set VINYLGEN_SLOW_TESTS=1 to run the corpus bound check")
class CorpusBoundTests(unittest.TestCase):
    """No emitted piece on the corpus has a sample beyond its bound, at the default
    thousandths and under stage-3 recovery (tightened lengths plus a fit constraint)."""

    def test_no_sample_is_beyond_its_piece_bound_on_the_corpus(self):
        from vinylgen.boundary_graph import shared_boundary_polygons

        checked = 0
        for subject in CORPUS:
            path = _corpus_segmentation(subject)
            if path is None:
                continue
            with np.load(path, allow_pickle=False) as data:
                labels = data["labels"].astype(np.int16)
                costs = data["palette_costs"].astype(np.float32)
                protected = data["protected_features"].astype(bool)
            included = sorted(int(value) for value in np.unique(labels))
            _, track, _ = shared_boundary_polygons(labels, included, costs,
                                                   protected_features=protected)
            settings = resolve_trace_lengths(TraceSettings(), max(labels.shape))
            tightened = replace(
                settings, curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 4.0),
                straight_line_max_deviation=_tightened(settings.straight_line_max_deviation, 2.0),
                side_smoothness=_tightened(settings.side_smoothness, 2.0))
            fitter = SharedChainFitter()
            failures = []
            for number, chain in enumerate(track.chains):
                anchors = [HardAnchor(anchor.point, anchor.kind) for anchor in chain.anchors]
                points = tuple(tuple(map(float, point)) for point in chain.points)
                canonical = points if points <= tuple(reversed(points)) else tuple(reversed(points))
                cases = [(settings, ())]
                if number % 7 == 0:
                    cases.append((tightened, (canonical[len(canonical) // 2],)))
                for chain_settings, constraints in cases:
                    fit = fitter.fit(canonical, chain_settings, anchors, constraints)
                    violations = run_violations(fit, canonical, chain_settings)
                    checked += 1
                    if violations:
                        failures.append((chain.identity, max(violations, key=lambda row: row[1] - row[2])))
            self.assertEqual(failures, [], f"{subject}: {len(failures)} chains beyond their bound")
        if not checked:
            self.skipTest("no corpus segmentation found")


if __name__ == "__main__":
    unittest.main()
