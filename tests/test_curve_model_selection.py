import math
import unittest

import numpy as np

from vinylgen.contracts import TraceSettings
from dataclasses import asdict, replace

from vinylgen.curve_fit import HardAnchor, SharedChainFitter, _geometric_corners, _tightened, ring_path
from vinylgen.web.models import TraceJobSettings


class CurveModelSelectionTests(unittest.TestCase):
    def test_sustained_staircase_is_collapsed_to_line_model(self):
        chain = [(0, 0), (1, .1), (2, .2), (3, .3), (4, .4)]
        fitted = SharedChainFitter().fit(
            chain, TraceSettings(curve_fit_tolerance=.2))
        self.assertEqual(fitted.metrics.lines, 1)
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.source_samples, 5)
        self.assertEqual(fitted.metrics.final_nodes, 1)

    def test_smooth_arc_is_one_cubic_not_pixel_segments(self):
        chain = [(float(x), float(np.sqrt(100-x*x))) for x in range(-8, 9)]
        fitted = SharedChainFitter().fit(
            chain, TraceSettings(curve_fit_tolerance=.25))
        self.assertGreater(fitted.metrics.cubics, 0)
        self.assertLess(fitted.metrics.final_nodes, fitted.metrics.source_samples)

    def test_three_sample_shallow_turn_is_a_stable_cubic(self):
        fitted = SharedChainFitter().fit(
            [(0, 0), (4, 0), (4, 1)],
            TraceSettings(curve_fit_tolerance=.2,
                          straight_line_max_deviation=.1),
        )
        self.assertEqual(fitted.metrics.cubics, 1)
        self.assertEqual(fitted.metrics.lines, 0)

    def test_reverse_shared_chain_reuses_reversible_fit(self):
        chain = [(0, 0), (1, .5), (2, 1.6), (3, 3)]
        fitter = SharedChainFitter()
        forward = fitter.fit(chain, TraceSettings(curve_fit_tolerance=.2))
        reverse = fitter.fit(reversed(chain), TraceSettings(curve_fit_tolerance=.2))
        self.assertEqual(forward.start, reverse.primitives[-1][-1])
        self.assertEqual(reverse.start, forward.primitives[-1][-1])
        self.assertEqual(forward.metrics, reverse.metrics)
        expected = tuple(
            (("L", section_start) if primitive[0] == "L" else
             ("C", primitive[2], primitive[1], section_start))
            for section_start, primitive in reversed(list(zip(
                [forward.start, *[p[-1] for p in forward.primitives[:-1]]],
                forward.primitives)))
        )
        self.assertEqual(reverse.primitives, expected)
        self.assertEqual(fitter.cache_entries, 1)

    def test_typed_junction_is_explicit_but_semantic_set_is_not_an_api(self):
        points = [(0, 0), (2, .2), (4, .8), (6, 1.8),
                  (6, 8), (0, 8), (0, 0)]
        fitted = ring_path(points, TraceSettings(
            corner_threshold=math.degrees(1.2), curve_fit_tolerance=1.0),
            [HardAnchor((4., .8), "junction")])
        self.assertIn("4 0.8", fitted.path)
        self.assertEqual(fitted.metrics.hard_anchors, 1)
        with self.assertRaises((AttributeError, TypeError)):
            ring_path(points, TraceSettings(), {(4., .8)})

    def test_shared_chain_splits_at_typed_hard_anchor(self):
        chain = [(0, 0), (1, .1), (2, .2), (3, .3), (4, .4)]
        fitted = SharedChainFitter().fit(
            chain, TraceSettings(curve_fit_tolerance=1),
            [HardAnchor((2., .2), "junction")])
        self.assertEqual(fitted.metrics.final_nodes, 2)
        self.assertEqual(fitted.primitives[0][-1], (2., .2))

    def test_metrics_exactly_describe_emitted_primitives(self):
        fitted = ring_path([(0, 0), (3, 0), (3, 2), (0, 2), (0, 0)],
                           TraceSettings())
        commands = fitted.path.count("L") + fitted.path.count("C")
        self.assertEqual(fitted.metrics.final_nodes, commands)
        self.assertEqual(fitted.metrics.final_nodes,
                         fitted.metrics.lines + fitted.metrics.cubics)
        metrics = fitted.metrics.as_dict()
        self.assertEqual(metrics["node_reduction"],
                         metrics["source_samples"] - metrics["final_nodes"])
        self.assertNotIn("straightened_cubics", metrics)

    def test_pixel_staircase_uses_robust_sustained_line(self):
        chain = [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2),
                 (3, 2), (3, 3), (4, 3)]
        fitted = SharedChainFitter().fit(chain, TraceSettings(
            curve_fit_tolerance=.15, straight_line_max_deviation=.8,
            minimum_straight_length=3))
        self.assertEqual(fitted.primitives, (("L", (4.0, 3.0)),))
        self.assertEqual(fitted.diagnostics[0].classification, "straight")

    def test_coherent_architectural_corner_becomes_two_lines(self):
        chain = [(0, .1), (2, -.1), (4, .1), (6, 0), (8, .1),
                 (8.1, 2), (7.9, 4), (8.1, 6), (8, 8)]
        fitted = SharedChainFitter().fit(chain, TraceSettings(
            curve_fit_tolerance=.8, straight_line_max_deviation=.25,
            minimum_straight_length=3, corner_analysis_span=5,
            corner_threshold=math.degrees(.7)))
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.lines, 2)
        self.assertTrue(any(item.classification == "corner"
                            for item in fitted.diagnostics))

    def test_diagnostics_report_models_without_dense_fallback(self):
        fitted = SharedChainFitter().fit(
            [(0, 0), (2, .2), (4, 1), (6, 2.5), (8, 5)],
            TraceSettings(curve_fit_tolerance=.3,
                          straight_line_max_deviation=.1))
        rows = [item.as_dict() for item in fitted.diagnostics]
        self.assertTrue(rows)
        self.assertTrue(all(not row["dense_fallback"] for row in rows))
        self.assertTrue(all(row["fallback_reason"] is None for row in rows))


def _max_sample_distance(fitted, samples) -> float:
    """Largest distance from any source sample to the fitted primitive sequence."""
    t = np.linspace(0.0, 1.0, 400)[:, None]
    dense = [np.asarray([fitted.start], dtype=float)]
    start = np.asarray(fitted.start, dtype=float)
    for primitive in fitted.primitives:
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
    return float(np.sqrt(((points[:, None, :] - curve[None, :, :]) ** 2).sum(axis=2)).min(axis=1).max())


class StraightLineDeviationTests(unittest.TestCase):
    # A shallow arc (radius 100, 40 px wide, sagitta about 2 px) that the default
    # straight-line deviation of 1.6 px collapses to one chord.
    ARC = [(float(x), float(math.sqrt(100 * 100 - x * x) - 100)) for x in range(-20, 21)]

    def test_default_deviation_straightens_the_shallow_arc(self):
        fitted = SharedChainFitter().fit(self.ARC, TraceSettings())
        self.assertEqual(fitted.metrics.lines, 1)
        self.assertEqual(fitted.metrics.cubics, 0)

    def test_zero_deviation_fits_cubics_within_tolerance_instead_of_a_chord(self):
        tolerance = .25
        settings = TraceSettings(straight_line_max_deviation=0, curve_fit_tolerance=tolerance)
        fitted = SharedChainFitter().fit(self.ARC, settings)
        self.assertGreater(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.lines, 0)
        self.assertTrue(all(row.classification == "curve" for row in fitted.diagnostics))
        self.assertLessEqual(_max_sample_distance(fitted, self.ARC), tolerance + 1e-6)
        # The chord the removed on/off shortcut emitted misses the arc by its sagitta.
        chord = SharedChainFitter().fit(self.ARC, TraceSettings(
            straight_line_max_deviation=10, curve_fit_tolerance=tolerance))
        self.assertEqual(chord.primitives, (("L", self.ARC[-1]),))
        self.assertGreater(_max_sample_distance(chord, self.ARC), 1.0)

    def test_zero_deviation_still_allows_a_line_within_the_curve_fit_tolerance(self):
        # Deviation 0 turns off the straight-line shortcut only. A piece bowing
        # less than the curve-fit tolerance is still a valid line fit, so at the
        # 1.5 px default a 1 px bow becomes a line; a 0.25 px tolerance keeps it curved.
        bow = [(float(x), float(math.sqrt(200 * 200 - x * x) - 200)) for x in range(-20, 21)]
        self.assertAlmostEqual(-min(y for _, y in bow), 1.0, places=2)
        default = SharedChainFitter().fit(bow, TraceSettings(straight_line_max_deviation=0))
        self.assertEqual((default.metrics.lines, default.metrics.cubics), (1, 0))
        self.assertEqual([row.classification for row in default.diagnostics], ["curve"])
        tight = SharedChainFitter().fit(bow, TraceSettings(straight_line_max_deviation=0,
                                                           curve_fit_tolerance=.25))
        self.assertEqual((tight.metrics.lines, tight.metrics.cubics), (0, 1))

    def test_max_deviation_only_tightens_curves_and_never_limits_lines(self):
        fit = lambda **kw: SharedChainFitter().fit(self.ARC, TraceSettings(**kw)).primitives
        curves = dict(straight_line_max_deviation=0, corner_straightness=1.6)
        # Fitting uses min(curve_fit_tolerance, max_deviation).
        self.assertEqual(fit(max_deviation=5, **curves), fit(**curves))
        self.assertEqual(fit(max_deviation=.25, **curves), fit(curve_fit_tolerance=.25, **curves))
        # A run passing the straight-line test is a line whatever max_deviation says.
        self.assertEqual(fit(max_deviation=.01), (("L", self.ARC[-1]),))

    def test_zero_deviation_still_straightens_exactly_straight_runs(self):
        for chain in ([(float(x), 3.0) for x in range(12)],
                      [(float(x), float(x)) for x in range(12)]):
            fitted = SharedChainFitter().fit(chain, TraceSettings(straight_line_max_deviation=0))
            self.assertEqual(fitted.primitives, (("L", chain[-1]),))

    def test_recovery_tightening_never_loosens_a_configured_value(self):
        self.assertEqual(_tightened(0.0, 2.0), 0.0)
        self.assertEqual(_tightened(0.03, 4.0), 0.03)
        self.assertEqual(_tightened(0.08, 4.0), 0.05)
        self.assertEqual(_tightened(1.6, 2.0), 0.8)


class CornerStraightnessTests(unittest.TestCase):
    # Two coherent, slightly noisy walls meeting at a hard corner.
    CHAIN = [(0, .1), (2, -.1), (4, .1), (6, 0), (8, .1),
             (8.1, 2), (7.9, 4), (8.1, 6), (8, 8)]
    CORNER = dict(curve_fit_tolerance=.8, minimum_straight_length=3,
                  corner_analysis_span=5, corner_threshold=math.degrees(.7))

    def test_none_resolves_to_the_straight_line_deviation(self):
        self.assertEqual(TraceSettings().corner_straightness, 1.6)
        self.assertEqual(TraceSettings(straight_line_max_deviation=.7).corner_straightness, .7)
        self.assertEqual(TraceSettings(straight_line_max_deviation=0,
                                       corner_straightness=.4).corner_straightness, .4)
        # A payload without the key resolves the same way through the web model.
        job = TraceJobSettings(straight_line_max_deviation=.9)
        self.assertIsNone(job.corner_straightness)
        self.assertEqual(TraceSettings(**job.model_dump()).corner_straightness, .9)
        # The resolved value survives copies, so later deviation changes leave it alone.
        settings = TraceSettings(straight_line_max_deviation=.7)
        self.assertEqual(TraceSettings(**asdict(settings)).corner_straightness, .7)
        self.assertEqual(replace(settings, straight_line_max_deviation=0).corner_straightness, .7)

    def test_corners_are_unchanged_when_deviation_drops_to_zero_with_explicit_straightness(self):
        points = np.asarray(self.CHAIN, dtype=float)
        before = TraceSettings(straight_line_max_deviation=.25, **self.CORNER)
        after = TraceSettings(straight_line_max_deviation=0, corner_straightness=.25, **self.CORNER)
        corners = _geometric_corners(points, before)
        self.assertTrue(corners)
        self.assertEqual(_geometric_corners(points, after), corners)
        fitted = SharedChainFitter().fit(self.CHAIN, after)
        self.assertEqual([row.source_start for row in fitted.diagnostics if row.classification == "corner"],
                         list(corners))
        # Without its own straightness the corner test would inherit the 0 deviation
        # and no noisy wall would confirm the corner.
        self.assertEqual(_geometric_corners(points, TraceSettings(
            straight_line_max_deviation=0, **self.CORNER)), {})

    def test_corner_straightness_is_part_of_the_fit_cache_key(self):
        fitter = SharedChainFitter()
        fitter.fit(self.CHAIN, TraceSettings(corner_straightness=.25, **self.CORNER))
        fitter.fit(self.CHAIN, TraceSettings(corner_straightness=.01, **self.CORNER))
        self.assertEqual(fitter.cache_entries, 2)


if __name__ == "__main__":
    unittest.main()
