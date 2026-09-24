"""Node placement: curve nodes at chosen candidate points, and straight-line snapping.

Candidates are axis extremes, inflections and points of tightest bend of the
samples smoothed at the curve feature scale; a shortest path over the pieces that
fit gives the fewest nodes (docs/svg-tracing-spec.md "Node placement").
"""

import itertools
import json
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
from pydantic import ValidationError

from vinylgen import curve_fit as cf
from vinylgen.cli_trace import parser, settings_from_args
from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import SharedChainFitter
from vinylgen.trace_window import WindowBoundaries
from vinylgen.web.models import TraceJobSettings

from test_smooth_joins import (SMOOTH_DEG, closed_ring, ellipse, join_turns,
                               max_sample_distance, rasterised_disc)

SCRATCH = Path(__file__).resolve().parents[1] / ".tmp"


def section_records(points, settings: TraceSettings = TraceSettings(length_unit="px")) -> np.ndarray:
    """The records ``_fit_section_nb`` emits for an open run with free ends."""
    array = np.ascontiguousarray(np.asarray(points, dtype=np.float64))
    last = len(array) - 1
    return cf._fit_section_records(array, 0, last, 0, last, settings)


def unit_candidates(points, settings: TraceSettings = TraceSettings(length_unit="px")):
    """``(index, tangent, kind)`` of the candidates of a whole open run."""
    array = np.ascontiguousarray(np.asarray(points, dtype=np.float64))
    m = len(array)
    theta = np.empty(m)
    index = np.empty(4 * m + 8, dtype=np.int64)
    tx, ty = np.empty(4 * m + 8), np.empty(4 * m + 8)
    kind = np.empty(4 * m + 8, dtype=np.int64)
    count = cf._unit_candidates_nb(array, 0, m - 1, 0, m - 1, settings.curve_feature_scale,
                                   settings.nodes_at_bends, cf._curve_tolerance(settings),
                                   theta, index, tx, ty, kind)
    return sorted((int(index[k]), (float(tx[k]), float(ty[k])), int(kind[k]))
                  for k in range(count))


def place(points, tolerance: float, mode: int = 0, streak: int = 0):
    """Records of ``_place_nodes_nb`` on a whole open run with free ends."""
    array = np.ascontiguousarray(np.asarray(points, dtype=np.float64))
    m = len(array)
    records = np.empty((m, cf._RECORD_COLUMNS))
    small, large = cf._join_scales(TraceSettings(length_unit="px"))
    count = cf._place_nodes_nb(
        array, 0, m - 1, 0, m - 1, False, 0.0, 0.0, False, 0.0, 0.0, tolerance,
        cf._pad_cap(TraceSettings(length_unit="px")), small, large,
        mode, True, 4.0, streak, cf._PRUNE_FACTOR, cf._NEWTON_WINDOW, records, 0,
        np.empty((m + 1, 2), dtype=np.int64), np.empty((m + 1, 6)), np.empty(m),
        np.empty((m, 2)))
    return records[:count]


def nodes_of(fit) -> list:
    return [primitive[-1] for primitive in fit.primitives]


def assert_symmetric_except_corners(test: unittest.TestCase, fit, closed: bool) -> None:
    corners = set(fit.corners)
    for node, turn, kinds in join_turns(fit, closed):
        if node not in corners:
            test.assertLess(turn, SMOOTH_DEG, f"cusp of {turn:.4f} deg at {node} ({kinds})")


def hook(peak=0.12, width=4.0, turn_from=10.0):
    """An open curve whose tangent turns from ``turn_from`` degrees, sharply in the
    middle (curvature peak ``peak`` 1/px at sample 100) and gently elsewhere, so it
    has no axis extreme and no inflection."""
    points = [(0.0, 0.0)]
    angle = math.radians(turn_from)
    x = y = 0.0
    for k in range(200):
        s = k * 0.5
        angle += (0.004 + peak * math.exp(-((s - 50.0) / width) ** 2)) * 0.5
        x += 0.5 * math.cos(angle)
        y += 0.5 * math.sin(angle)
        points.append((x, y))
    return points


class CandidateTests(unittest.TestCase):
    def test_a_circle_has_four_nodes_at_its_axis_extremes(self):
        for radius, ring in ((30, ellipse(30, 30, 190, centre=(100.0, 100.0))),
                             (100, ellipse(100, 100, 628, centre=(150.0, 150.0))),
                             (30, rasterised_disc(30.0, centre=(80.0, 80.0)))):
            centre = np.mean(np.asarray(ring[:-1]), axis=0)
            fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
            self.assertEqual(len(fit.primitives), 4, radius)
            self.assertEqual(fit.metrics.lines, 0)
            for node in nodes_of(fit):
                offset = np.subtract(node, centre)
                # Each node is on an axis through the centre (within one sample).
                self.assertLess(min(abs(offset[0]), abs(offset[1])), 1.0, (radius, node))
            # Their tangents are exactly horizontal or vertical: both handles at a
            # node share its x or its y.
            for left, right in zip(fit.primitives, fit.primitives[1:] + fit.primitives[:1]):
                node = left[-1]
                self.assertTrue((left[2][0] == node[0] == right[1][0])
                                or (left[2][1] == node[1] == right[1][1]), node)
            self.assertLessEqual(max_sample_distance(fit, ring), 1.5 + 1e-9)

    def test_a_rotated_ellipse_offers_its_sharp_vertices_as_bends(self):
        a, b, rotation = 40.0, 10.0, math.radians(30)
        count = 300
        ring = []
        for k in range(count):
            angle = 2 * math.pi * k / count
            x, y = a * math.cos(angle), b * math.sin(angle)
            ring.append((100 + x * math.cos(rotation) - y * math.sin(rotation),
                         100 + x * math.sin(rotation) + y * math.cos(rotation)))
        # A whole lap from the minor vertex, so both sharp vertices are interior.
        run = ring[75:] + ring[:76]
        vertices = [(100 + a * math.cos(rotation), 100 + a * math.sin(rotation)),
                    (100 - a * math.cos(rotation), 100 - a * math.sin(rotation))]
        bends = [candidate for candidate in unit_candidates(run) if candidate[2] == cf._KIND_BEND]
        self.assertEqual(len(bends), 2)
        for vertex in vertices:
            self.assertTrue(any(math.dist(run[index], vertex) <= 1.0 for index, _, _ in bends))
        off = unit_candidates(run, TraceSettings(length_unit="px", nodes_at_bends=False))
        self.assertFalse([c for c in off if c[2] in (cf._KIND_BEND, cf._KIND_MINOR_BEND)])

    def test_the_tightest_bend_takes_the_node_a_turn_needs_when_bends_are_on(self):
        # A turn of about 70 degrees with no axis extreme or inflection, sharpest
        # at sample 100. At a tolerance that needs one interior node, bends on put
        # the bend there; bends off leave a worst-sample fallback split.
        curve = hook(peak=0.08, width=6.0)
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=0.5, straight_line_max_deviation=0.0)
        on = section_records(curve, settings)
        off = section_records(curve, replace(settings, nodes_at_bends=False))
        self.assertEqual([(int(row[7]), int(row[10])) for row in on[:-1]],
                         [(cf._KIND_BEND, 100)])
        self.assertEqual([int(row[7]) for row in off[:-1]], [cf._KIND_FALLBACK])

    def test_an_s_curve_has_its_node_at_the_inflection_or_none(self):
        curve = [(x, 12.0 * math.sin(x / 15.0)) for x in np.arange(-23.0, 23.5, 0.5)]
        middle = len(curve) // 2
        self.assertEqual(curve[middle], (0.0, 0.0))
        loose = section_records(curve, TraceSettings(length_unit="px", curve_fit_tolerance=1.5))
        self.assertEqual(len(loose), 1)
        tight = section_records(curve, TraceSettings(length_unit="px", curve_fit_tolerance=0.05))
        self.assertEqual([(int(row[7]), int(row[10])) for row in tight[:-1]],
                         [(cf._KIND_INFLECTION, middle)])

    def test_pixel_ripple_is_a_minor_candidate_not_a_significant_one(self):
        # A long gentle staircase: its ripple still marks where the boundary turns,
        # but no lobe bulges half a pixel, so nothing is significant.
        run = []
        for x in range(0, 160):
            run.append((float(x), math.floor(x / 16.0) + 0.3 * ((x % 16) / 16.0)))
        kinds = {kind for _, _, kind in unit_candidates(run)}
        self.assertNotIn(cf._KIND_INFLECTION, kinds)
        self.assertNotIn(cf._KIND_BEND, kinds)


class SelectionTests(unittest.TestCase):
    def brute_force(self, points, tolerance: float, mandatory_extremes: bool):
        """Fewest pieces over every ordered subset of the run's candidates."""
        array = np.ascontiguousarray(np.asarray(points, dtype=np.float64))
        m = len(array)
        arc, table = cf._range_tables_nb(array, 0, m - 1)
        t, fitted = np.empty(m), np.empty((m, 2))
        candidates = [(index, tangent, kind) for index, tangent, kind in unit_candidates(
            points, TraceSettings(length_unit="px", curve_fit_tolerance=tolerance)) if 0 < index < m - 1]
        nodes = [(0, None, 0), *candidates, (m - 1, None, 0)]

        def valid(a, b):
            (i, ti, _), (j, tj, _) = a, b
            if i >= j:
                return False
            return bool(cf._pair_fit_nb(
                array, i, j, ti is not None, *(ti or (0.0, 0.0)), tj is not None,
                *(tj or (0.0, 0.0)), tolerance, 2.0, cf._NEWTON_WINDOW, True, t, fitted,
                arc, table, 0)[0])

        best = None
        inner = nodes[1:-1]
        for size in range(len(inner) + 1):
            for chosen in itertools.combinations(inner, size):
                if mandatory_extremes and any(node[2] == cf._KIND_EXTREME and node not in chosen
                                              for node in inner):
                    continue
                route = [nodes[0], *chosen, nodes[-1]]
                if all(valid(a, b) for a, b in zip(route, route[1:])):
                    best = size + 1
                    break
            if best is not None:
                break
        return best, len(candidates)

    def test_fewest_pieces_match_brute_force_on_small_runs(self):
        rng = np.random.default_rng(11)
        compared = 0
        for trial in range(40):
            x = np.linspace(0.0, 30.0, 61)
            phases = rng.uniform(0, 2 * math.pi, 3)
            y = sum(rng.uniform(1.0, 4.0) * np.sin(x / rng.uniform(2.5, 6.0) + phase)
                    for phase in phases)
            points = list(zip(x, y))
            tolerance = float(rng.uniform(0.1, 0.4))
            for mode, mandatory in ((0, False), (1, True)):
                best, count = self.brute_force(points, tolerance, mandatory)
                if best is None or count > 13:
                    continue
                records = place(points, tolerance, mode)
                # A route through candidates exists, so no fallback node is added.
                self.assertNotIn(cf._KIND_FALLBACK, records[:, 7].astype(int))
                self.assertEqual(len(records), best, (trial, mode))
                compared += 1
        self.assertGreater(compared, 20)

    def test_always_at_extremes_keeps_every_extreme(self):
        # A wavy run whose small bumps need no node at a loose tolerance.
        points = [(x, 2.0 * math.sin(x / 5.0) + 0.9 * math.sin(x / 1.7))
                  for x in np.arange(0.0, 60.0, 0.5)]
        tolerance = 1.0
        extremes = {index for index, _, kind in unit_candidates(
            points, TraceSettings(length_unit="px", curve_fit_tolerance=tolerance)) if kind == cf._KIND_EXTREME}
        self.assertTrue(extremes)
        prefer = place(points, tolerance, 0, cf._PRUNE_STREAK)
        always = place(points, tolerance, cf._PLACEMENT_AT_EXTREMES, cf._PRUNE_STREAK)
        fewest = place(points, tolerance, cf._PLACEMENT_FEWEST, cf._PRUNE_STREAK)
        self.assertTrue(extremes <= {int(row[10]) for row in always})
        self.assertFalse(extremes <= {int(row[10]) for row in prefer})
        self.assertLess(len(prefer), len(always))
        self.assertLessEqual(len(fewest), len(prefer))
        for records in (prefer, always, fewest):
            fit = cf.ChainFit(points[0], tuple(cf._fit_records(records)),
                              cf.FitMetrics(0, 0, 0, 0, 0))
            self.assertLessEqual(max_sample_distance(fit, points), tolerance + 1e-9)

    def test_fewest_nodes_only_prefers_no_kind(self):
        for kind in (cf._KIND_EXTREME, cf._KIND_INFLECTION, cf._KIND_BEND,
                     cf._KIND_MINOR_BEND, cf._KIND_SPLIT, cf._KIND_FALLBACK):
            self.assertEqual(cf._kind_penalty_nb(kind, cf._PLACEMENT_FEWEST), 0.0)
        penalties = [cf._kind_penalty_nb(kind, 0) for kind in (
            cf._KIND_EXTREME, cf._KIND_INFLECTION, cf._KIND_BEND, cf._KIND_FALLBACK)]
        self.assertEqual(penalties, sorted(penalties))
        self.assertEqual(penalties[0], 0.0)

    def test_fallback_splits_where_no_candidate_route_exists(self):
        # A zigzag whose teeth are smoothed away at a 16 px feature scale: only
        # fallback splits can meet the tolerance.
        zigzag = [(float(x), 1.2 if (x // 2) % 2 else -1.2) for x in range(60)]
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=0.5, curve_feature_scale=16,
                                 straight_line_max_deviation=0.1)
        records = section_records(zigzag, settings)
        self.assertIn(cf._KIND_FALLBACK, records[:, 7].astype(int))
        fit = cf.ChainFit(zigzag[0], tuple(cf._fit_records(records)), cf.FitMetrics(0, 0, 0, 0, 0))
        self.assertLessEqual(max_sample_distance(fit, zigzag), 0.5 + 1e-9)

    def test_tolerance_holds_and_only_corners_are_cusps_in_every_mode(self):
        rng = np.random.default_rng(5)
        for trial in range(4):
            angles = np.linspace(0.0, 2 * math.pi, 240, endpoint=False)
            radius = 30 + 6 * np.sin(3 * angles + trial) + 3 * np.cos(5 * angles)
            radius = radius + rng.normal(0.0, 0.3, len(angles))
            ring = closed_ring(zip(60 + radius * np.cos(angles), 60 + radius * np.sin(angles)))
            for placement in cf.PLACEMENT_MODES:
                for bends in (True, False):
                    for tolerance in (1.5, 0.4):
                        # A sharpened corner may lie the corner sharpening
                        # allowance off its samples; it is held to the tolerance.
                        settings = TraceSettings(length_unit="px", node_placement=placement, nodes_at_bends=bends,
                                                 curve_fit_tolerance=tolerance,
                                                 corner_sharpening_allowance=tolerance,
                                                 straight_line_snap="off")
                        fit = SharedChainFitter().fit(ring, settings)
                        self.assertLessEqual(max_sample_distance(fit, ring), tolerance + 1e-9)
                        assert_symmetric_except_corners(self, fit, closed=True)

    def test_a_cyclic_ring_is_refitted_from_a_natural_node(self):
        ring = ellipse(25, 15, 140, phase=0.0)
        fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px", curve_fit_tolerance=0.1))
        start = np.subtract(fit.start, (50.0, 50.0))
        # The seam moved from the canonical first sample to an axis extreme.
        self.assertLess(min(abs(start[0]), abs(start[1])), 1.0)
        self.assertEqual(fit.primitives[-1][-1], fit.start)
        assert_symmetric_except_corners(self, fit, closed=True)


def rotated_rectangle(width, height, degrees, centre=(60.0, 60.0)):
    angle = math.radians(degrees)
    corners = [(-width / 2, -height / 2), (width / 2, -height / 2),
               (width / 2, height / 2), (-width / 2, height / 2)]
    points = []
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        steps = int(math.hypot(x1 - x0, y1 - y0))
        for k in range(steps):
            x, y = x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps
            points.append((centre[0] + x * math.cos(angle) - y * math.sin(angle),
                           centre[1] + x * math.sin(angle) + y * math.cos(angle)))
    return closed_ring(points[7:] + points[:7])


def line_directions(fit) -> list[float]:
    """Direction in degrees (mod 180) of every line of ``fit``."""
    directions = []
    start = fit.start
    for primitive in fit.primitives:
        if primitive[0] == "L":
            directions.append(math.degrees(math.atan2(primitive[1][1] - start[1],
                                                      primitive[1][0] - start[0])) % 180.0)
        start = primitive[-1]
    return directions


class SnapTests(unittest.TestCase):
    def test_off_keeps_the_measured_angle(self):
        fit = SharedChainFitter().fit(rotated_rectangle(40, 20, 1.0),
                                      TraceSettings(length_unit="px", straight_line_snap="off"))
        self.assertEqual(len(line_directions(fit)), 4)
        for direction in line_directions(fit):
            self.assertGreater(min(direction % 90.0, 90.0 - direction % 90.0), 0.5)

    def test_horizontal_and_vertical_lines_snap_exactly_within_the_angle(self):
        for degrees in (0.4, 1.0, 1.9):
            ring = rotated_rectangle(40, 20, degrees)
            fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
            nodes = [fit.start, *nodes_of(fit)]
            self.assertEqual(fit.metrics.lines, 4)
            for a, b in zip(nodes, nodes[1:]):
                self.assertTrue(a[0] == b[0] or a[1] == b[1], (degrees, a, b))
            # Corners moved onto the snapped lines stay cusps; nothing else is.
            self.assertEqual(len(fit.corners), 4)
            self.assertEqual(set(fit.corners), set(nodes))
            for corner in fit.corners:
                self.assertLessEqual(min(math.dist(corner, sample) for sample in ring), 2.0)
            self.assertLessEqual(max_sample_distance(fit, ring),
                                 TraceSettings(length_unit="px").straight_line_max_deviation + 1e-9)
        # Beyond the snap angle nothing snaps.
        fit = SharedChainFitter().fit(rotated_rectangle(40, 20, 3.0), TraceSettings(length_unit="px"))
        nodes = [fit.start, *nodes_of(fit)]
        self.assertFalse(any(a[0] == b[0] or a[1] == b[1] for a, b in zip(nodes, nodes[1:])))
        # A wider snap angle takes it.
        fit = SharedChainFitter().fit(rotated_rectangle(40, 20, 3.0), TraceSettings(length_unit="px", snap_angle=4.0))
        nodes = [fit.start, *nodes_of(fit)]
        self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(nodes, nodes[1:])))

    def test_a_snap_never_moves_a_line_beyond_the_straight_line_deviation(self):
        # At 1.5 degrees a 40 px side's samples lie up to about 0.5 px from its
        # snapped line: allowed at 1.6 px, refused at 0.2 px.
        ring = rotated_rectangle(40, 20, 1.5)
        loose = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
        self.assertTrue(all(direction % 90.0 == 0.0 for direction in line_directions(loose)))
        strict = SharedChainFitter().fit(ring, TraceSettings(length_unit="px", straight_line_max_deviation=0.2,
                                                             side_smoothness=1.6))
        self.assertFalse(any(direction % 90.0 == 0.0 for direction in line_directions(strict)))

    def test_also_45_degrees_snaps_diagonals(self):
        ring = rotated_rectangle(40, 20, 44.0)
        axis = SharedChainFitter().fit(ring, TraceSettings(length_unit="px"))
        diagonal = SharedChainFitter().fit(ring, TraceSettings(length_unit="px", straight_line_snap="axis_diagonal"))
        self.assertFalse(any(abs(direction - 45.0) < 1e-6 for direction in line_directions(axis)))
        directions = line_directions(diagonal)
        self.assertEqual(len(directions), 4)
        for direction in directions:
            self.assertAlmostEqual(min(abs(direction - 45.0), abs(direction - 135.0)), 0.0, places=6)

    def test_lines_between_curves_snap_and_their_joins_stay_symmetric(self):
        # A rounded rectangle turned by one degree: no corners, so its straight
        # sides are lines inside one curve run, joined smoothly to the rounds.
        width, height, radius = 90.0, 60.0, 15.0
        centres = ((width - radius, radius), (width - radius, height - radius),
                   (radius, height - radius), (radius, radius))
        points = []
        for quarter, (cx, cy) in enumerate(centres):
            start = -90.0 + 90.0 * quarter
            points += [(cx + radius * math.cos(math.radians(start + 3.0 * k)),
                        cy + radius * math.sin(math.radians(start + 3.0 * k))) for k in range(30)]
            ex, ey = (cx + radius * math.cos(math.radians(start + 90.0)),
                      cy + radius * math.sin(math.radians(start + 90.0)))
            nx, ny = centres[(quarter + 1) % 4]
            nx += radius * math.cos(math.radians(start + 90.0))
            ny += radius * math.sin(math.radians(start + 90.0))
            steps = int(math.hypot(nx - ex, ny - ey))
            points += [(ex + (nx - ex) * k / steps, ey + (ny - ey) * k / steps) for k in range(steps)]
        turn = math.radians(1.0)
        points = [(20 + x * math.cos(turn) - y * math.sin(turn),
                   20 + x * math.sin(turn) + y * math.cos(turn)) for x, y in points]
        ring = closed_ring(points)
        fit = SharedChainFitter().fit(ring, TraceSettings(length_unit="px", curve_fit_tolerance=0.5))
        self.assertEqual(fit.corners, ())
        starts = [fit.start, *nodes_of(fit)[:-1]]
        lines = [(start, primitive[1]) for start, primitive in zip(starts, fit.primitives)
                 if primitive[0] == "L"]
        # The long sides are found as straight runs; each snaps exactly.
        self.assertGreaterEqual(len(lines), 2)
        for a, b in lines:
            self.assertTrue(a[0] == b[0] or a[1] == b[1], (a, b))
        assert_symmetric_except_corners(self, fit, closed=True)

    def test_junctions_never_move(self):
        # A straight chain between two junctions 1 degree off horizontal: its ends
        # are fixed, so it cannot become exactly horizontal and stays as sampled.
        end = (40.0 * math.cos(math.radians(1.0)), 40.0 * math.sin(math.radians(1.0)))
        chain = [(end[0] * k / 40, end[1] * k / 40) for k in range(41)]
        anchors = [cf.HardAnchor(chain[0], "junction"), cf.HardAnchor(chain[-1], "junction")]
        fit = SharedChainFitter().fit(chain, TraceSettings(length_unit="px"), anchors)
        self.assertEqual(fit.start, chain[0])
        self.assertEqual(fit.primitives[-1][-1], chain[-1])


class SettingsTests(unittest.TestCase):
    FIELDS = {"straight_line_snap": "axis_diagonal", "snap_angle": 3.5,
              "node_placement": "at_extremes", "nodes_at_bends": False,
              "curve_feature_scale": 8.0}

    def test_defaults(self):
        settings = TraceSettings(length_unit="px")
        self.assertEqual((settings.straight_line_snap, settings.snap_angle, settings.node_placement,
                          settings.nodes_at_bends, settings.curve_feature_scale),
                         ("axis", 2.0, "prefer_extremes", True, 4.0))
        job = TraceJobSettings()
        self.assertEqual(TraceSettings(**job.model_dump()), TraceSettings())

    def test_invalid_values_are_rejected(self):
        for field, value in (("straight_line_snap", "diagonal"), ("node_placement", "extremes"),
                             ("snap_angle", 0.4), ("snap_angle", 10.5),
                             ("curve_feature_scale", 0.0)):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    TraceSettings(**{field: value})
                with self.assertRaises(ValidationError):
                    TraceJobSettings(**{field: value, "length_unit": "per_mille"})
        # The feature scale's range (thousandths of the longer side) bounds input only.
        for value in (0.2, 16.5):
            with self.subTest(field="curve_feature_scale", value=value):
                with self.assertRaises(ValidationError):
                    TraceJobSettings(curve_feature_scale=value, length_unit="per_mille")
                self.assertEqual(TraceSettings(curve_feature_scale=value).curve_feature_scale, value)

    def test_web_settings_round_trip(self):
        job = TraceJobSettings(**self.FIELDS, length_unit="per_mille")
        settings = TraceSettings(**job.model_dump())
        for field, value in self.FIELDS.items():
            self.assertEqual(getattr(settings, field), value)
        # Settings stored before these fields existed load with the defaults.
        stored = {key: value for key, value in TraceJobSettings().model_dump().items()
                  if key not in self.FIELDS}
        self.assertEqual(TraceSettings(**TraceJobSettings(**stored).model_dump()), TraceSettings())

    def test_cli_flags_and_config_round_trip(self):
        args = parser().parse_args([
            "outputs", "--straight-line-snap", "axis_diagonal", "--snap-angle", "3.5",
            "--node-placement", "at_extremes", "--no-nodes-at-bends",
            "--curve-feature-scale", "8"])
        settings, _, _ = settings_from_args(args)
        for field, value in self.FIELDS.items():
            self.assertEqual(getattr(settings, field), value)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            path = Path(name) / "trace.json"
            path.write_text(json.dumps({"settings": asdict(settings), "cleanup_settings": None}))
            again, _, _ = settings_from_args(parser().parse_args(["outputs", "--config", str(path)]))
            self.assertEqual(again, settings)
            # An older trace.json without the fields loads with the defaults.
            block = {key: value for key, value in asdict(TraceSettings(length_unit="px")).items()
                     if key not in self.FIELDS}
            path.write_text(json.dumps({"settings": block}))
            old, _, _ = settings_from_args(parser().parse_args(["outputs", "--config", str(path)]))
            self.assertEqual(old, TraceSettings(length_unit="px"))

    def test_every_field_is_in_the_fit_caches_keys(self):
        chain = [(float(x), 4.0 * math.sin(x / 6.0)) for x in range(40)]
        fitter = SharedChainFitter()
        keys = {fitter.prepare(chain, TraceSettings(length_unit="px", **{field: value}))[0]
                for field, value in self.FIELDS.items()}
        self.assertEqual(len(keys), len(self.FIELDS))
        self.assertNotIn(fitter.prepare(chain, TraceSettings(length_unit="px"))[0], keys)
        for field, value in self.FIELDS.items():
            window = WindowBoundaries(None, {}, None)
            default = window.fitter_for(TraceSettings(length_unit="px"), 0.0)
            self.assertIsNot(window.fitter_for(TraceSettings(length_unit="px", **{field: value}),
                                               0.0), default, field)


if __name__ == "__main__":
    unittest.main()
