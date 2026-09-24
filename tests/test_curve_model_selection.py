import math
import unittest

import numpy as np

from vinylgen.contracts import TraceSettings, upgrade_trace_record
from dataclasses import asdict, replace

from vinylgen.curve_fit import HardAnchor, SharedChainFitter, _geometric_corners, _tightened, ring_path
from vinylgen.web.models import TraceJobSettings
from vinylgen.trace_lengths import resolve_trace_lengths


class CurveModelSelectionTests(unittest.TestCase):
    def test_sustained_staircase_is_collapsed_to_line_model(self):
        chain = [(0, 0), (1, .1), (2, .2), (3, .3), (4, .4)]
        fitted = SharedChainFitter().fit(
            chain, TraceSettings(length_unit="px", curve_fit_tolerance=.2))
        self.assertEqual(fitted.metrics.lines, 1)
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.source_samples, 5)
        self.assertEqual(fitted.metrics.final_nodes, 1)

    def test_smooth_arc_is_one_cubic_not_pixel_segments(self):
        chain = [(float(x), float(np.sqrt(100-x*x))) for x in range(-8, 9)]
        fitted = SharedChainFitter().fit(
            chain, TraceSettings(length_unit="px", curve_fit_tolerance=.25))
        self.assertGreater(fitted.metrics.cubics, 0)
        self.assertLess(fitted.metrics.final_nodes, fitted.metrics.source_samples)

    def test_three_sample_shallow_turn_is_a_stable_cubic(self):
        fitted = SharedChainFitter().fit(
            [(0, 0), (4, 0), (4, 1)],
            TraceSettings(length_unit="px", curve_fit_tolerance=.2,
                          straight_line_max_deviation=.1),
        )
        self.assertEqual(fitted.metrics.cubics, 1)
        self.assertEqual(fitted.metrics.lines, 0)

    def test_reverse_shared_chain_reuses_reversible_fit(self):
        chain = [(0, 0), (1, .5), (2, 1.6), (3, 3)]
        fitter = SharedChainFitter()
        forward = fitter.fit(chain, TraceSettings(length_unit="px", curve_fit_tolerance=.2))
        reverse = fitter.fit(reversed(chain), TraceSettings(length_unit="px", curve_fit_tolerance=.2))
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
            length_unit="px", corner_threshold=math.degrees(1.2), curve_fit_tolerance=1.0),
            [HardAnchor((4., .8), "junction")])
        self.assertIn("4 0.8", fitted.path)
        self.assertEqual(fitted.metrics.hard_anchors, 1)
        with self.assertRaises((AttributeError, TypeError)):
            ring_path(points, TraceSettings(length_unit="px"), {(4., .8)})

    def test_shared_chain_splits_at_typed_hard_anchor(self):
        chain = [(0, 0), (1, .1), (2, .2), (3, .3), (4, .4)]
        fitted = SharedChainFitter().fit(
            chain, TraceSettings(length_unit="px", curve_fit_tolerance=1),
            [HardAnchor((2., .2), "junction")])
        self.assertEqual(fitted.metrics.final_nodes, 2)
        self.assertEqual(fitted.primitives[0][-1], (2., .2))

    def test_metrics_exactly_describe_emitted_primitives(self):
        fitted = ring_path([(0, 0), (3, 0), (3, 2), (0, 2), (0, 0)],
                           TraceSettings(length_unit="px"))
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
            length_unit="px", curve_fit_tolerance=.15, straight_line_max_deviation=.8,
            minimum_straight_length=3))
        self.assertEqual(fitted.primitives, (("L", (4.0, 3.0)),))
        self.assertEqual(fitted.diagnostics[0].classification, "straight")

    def test_coherent_architectural_corner_becomes_two_lines(self):
        chain = [(0, .1), (2, -.1), (4, .1), (6, 0), (8, .1),
                 (8.1, 2), (7.9, 4), (8.1, 6), (8, 8)]
        fitted = SharedChainFitter().fit(chain, TraceSettings(
            length_unit="px", curve_fit_tolerance=.8, straight_line_max_deviation=.25,
            minimum_straight_length=3, corner_analysis_span=5,
            corner_threshold=math.degrees(.7)))
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.lines, 2)
        self.assertTrue(any(item.classification == "corner"
                            for item in fitted.diagnostics))

    def test_diagnostics_report_models_without_dense_fallback(self):
        fitted = SharedChainFitter().fit(
            [(0, 0), (2, .2), (4, 1), (6, 2.5), (8, 5)],
            TraceSettings(length_unit="px", curve_fit_tolerance=.3,
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
    # A shallow arc (radius 100, 40 px wide): its chord misses its middle by its
    # sagitta, about 2 px, while its best-fit line is within about 1.3 px.
    ARC = [(float(x), float(math.sqrt(100 * 100 - x * x) - 100)) for x in range(-20, 21)]
    # The same width at radius 150: sagitta about 1.34 px.
    FLAT_ARC = [(float(x), float(math.sqrt(150 * 150 - x * x) - 150)) for x in range(-20, 21)]

    def test_default_deviation_straightens_an_arc_whose_chord_is_within_it(self):
        fitted = SharedChainFitter().fit(self.FLAT_ARC, TraceSettings(length_unit="px"))
        self.assertEqual(fitted.primitives, (("L", self.FLAT_ARC[-1]),))
        self.assertEqual([row.classification for row in fitted.diagnostics], ["straight"])
        # The chord is what is written, so the deeper arc, whose chord misses it
        # by more than the 1.6 px deviation, is not one line.
        fitted = SharedChainFitter().fit(self.ARC, TraceSettings(length_unit="px"))
        self.assertNotEqual(fitted.primitives, (("L", self.ARC[-1]),))
        self.assertEqual([row.classification for row in fitted.diagnostics], ["curve"])
        self.assertLessEqual(_max_sample_distance(fitted, self.ARC), 1.5 + 1e-6)

    def test_zero_deviation_fits_cubics_within_tolerance_instead_of_a_chord(self):
        tolerance = .25
        settings = TraceSettings(length_unit="px", straight_line_max_deviation=0, curve_fit_tolerance=tolerance)
        fitted = SharedChainFitter().fit(self.ARC, settings)
        self.assertGreater(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.lines, 0)
        self.assertTrue(all(row.classification == "curve" for row in fitted.diagnostics))
        self.assertLessEqual(_max_sample_distance(fitted, self.ARC), tolerance + 1e-6)
        # The chord the removed on/off shortcut emitted misses the arc by its sagitta.
        chord = SharedChainFitter().fit(self.ARC, TraceSettings(
            length_unit="px", straight_line_max_deviation=10, curve_fit_tolerance=tolerance))
        self.assertEqual(chord.primitives, (("L", self.ARC[-1]),))
        self.assertGreater(_max_sample_distance(chord, self.ARC), 1.0)

    def test_zero_deviation_still_allows_a_line_within_the_curve_fit_tolerance(self):
        # Deviation 0 turns off the straight-line shortcut only. A piece bowing
        # less than the curve-fit tolerance is still a valid line fit, so at the
        # 1.5 px default a 1 px bow becomes a line; a 0.25 px tolerance keeps it curved.
        bow = [(float(x), float(math.sqrt(200 * 200 - x * x) - 200)) for x in range(-20, 21)]
        self.assertAlmostEqual(-min(y for _, y in bow), 1.0, places=2)
        default = SharedChainFitter().fit(bow, TraceSettings(length_unit="px", straight_line_max_deviation=0))
        self.assertEqual((default.metrics.lines, default.metrics.cubics), (1, 0))
        self.assertEqual([row.classification for row in default.diagnostics], ["curve"])
        tight = SharedChainFitter().fit(bow, TraceSettings(length_unit="px", straight_line_max_deviation=0,
                                                           curve_fit_tolerance=.25))
        self.assertEqual((tight.metrics.lines, tight.metrics.cubics), (0, 1))

    def test_max_deviation_caps_the_curve_tolerance_and_the_straight_deviation(self):
        fit = lambda chain, **kw: SharedChainFitter().fit(
            chain, TraceSettings(length_unit="px", **kw)).primitives
        curves = dict(straight_line_max_deviation=0, side_smoothness=1.6)
        # Fitting uses min(curve_fit_tolerance, max_deviation).
        self.assertEqual(fit(self.ARC, max_deviation=5, **curves), fit(self.ARC, **curves))
        self.assertEqual(fit(self.ARC, max_deviation=.25, **curves),
                         fit(self.ARC, curve_fit_tolerance=.25, **curves))
        # Straightening uses min(straight_line_max_deviation, max_deviation).
        self.assertEqual(fit(self.FLAT_ARC, max_deviation=5), (("L", self.FLAT_ARC[-1]),))
        capped = fit(self.FLAT_ARC, max_deviation=1.0, side_smoothness=1.6)
        self.assertNotEqual(capped, (("L", self.FLAT_ARC[-1]),))
        self.assertEqual(capped, fit(self.FLAT_ARC, straight_line_max_deviation=1.0,
                                     curve_fit_tolerance=1.0, side_smoothness=1.6,
                                     corner_sharpening_allowance=1.0))

    def test_zero_deviation_still_straightens_exactly_straight_runs(self):
        for chain in ([(float(x), 3.0) for x in range(12)],
                      [(float(x), float(x)) for x in range(12)]):
            fitted = SharedChainFitter().fit(chain, TraceSettings(length_unit="px", straight_line_max_deviation=0))
            self.assertEqual(fitted.primitives, (("L", chain[-1]),))

    def test_recovery_tightening_never_loosens_a_configured_value(self):
        self.assertEqual(_tightened(0.0, 2.0), 0.0)
        self.assertEqual(_tightened(0.03, 4.0), 0.03)
        self.assertEqual(_tightened(0.08, 4.0), 0.05)
        self.assertEqual(_tightened(1.6, 2.0), 0.8)


class SideSmoothnessTests(unittest.TestCase):
    # Two coherent, slightly noisy walls meeting at a hard corner.
    CHAIN = [(0, .1), (2, -.1), (4, .1), (6, 0), (8, .1),
             (8.1, 2), (7.9, 4), (8.1, 6), (8, 8)]
    CORNER = dict(curve_fit_tolerance=.8, minimum_straight_length=3,
                  corner_analysis_span=5, corner_threshold=math.degrees(.7))

    def test_none_resolves_to_the_straight_line_deviation(self):
        self.assertEqual(TraceSettings(length_unit="px").side_smoothness, 1.6)
        self.assertEqual(TraceSettings(length_unit="px", straight_line_max_deviation=.7).side_smoothness, .7)
        self.assertEqual(TraceSettings(length_unit="px", straight_line_max_deviation=0,
                                       side_smoothness=.4).side_smoothness, .4)
        # A payload without the key resolves the same way through the web model.
        job = TraceJobSettings(straight_line_max_deviation=.9, length_unit="per_mille")
        self.assertIsNone(job.side_smoothness)
        self.assertEqual(TraceSettings(**job.model_dump()).side_smoothness, .9)
        # A request or record naming it by its former name, corner_straightness.
        self.assertEqual(TraceJobSettings(corner_straightness=.4, length_unit="per_mille").side_smoothness, .4)
        self.assertEqual(upgrade_trace_record({"corner_straightness": .4}), {"side_smoothness": .4})
        self.assertEqual(upgrade_trace_record({"corner_straightness": .4, "side_smoothness": .5}),
                         {"side_smoothness": .5})
        # The resolved value survives copies, so later deviation changes leave it alone.
        settings = TraceSettings(length_unit="px", straight_line_max_deviation=.7)
        self.assertEqual(TraceSettings(**asdict(settings)).side_smoothness, .7)
        self.assertEqual(replace(settings, straight_line_max_deviation=0).side_smoothness, .7)

    def test_the_sharpening_allowance_follows_the_maximum_deviation(self):
        self.assertIsNone(TraceSettings().corner_sharpening_allowance)
        self.assertEqual(TraceSettings(max_deviation=.5).corner_sharpening_allowance, .5)
        self.assertEqual(TraceSettings(max_deviation=.5, corner_sharpening_allowance=2).corner_sharpening_allowance, 2)
        self.assertEqual(resolve_trace_lengths(TraceSettings(), 2000).corner_sharpening_allowance, 2.0)
        self.assertEqual(resolve_trace_lengths(TraceSettings(length_unit="px"), 2000)
                         .corner_sharpening_allowance, 2.0)

    def test_the_sharpening_allowance_is_part_of_the_fit_cache_key(self):
        fitter = SharedChainFitter()
        fitter.fit(self.CHAIN, TraceSettings(length_unit="px", corner_sharpening_allowance=.5, **self.CORNER))
        fitter.fit(self.CHAIN, TraceSettings(length_unit="px", corner_sharpening_allowance=3, **self.CORNER))
        self.assertEqual(fitter.cache_entries, 2)

    def test_corners_are_unchanged_when_deviation_drops_to_zero_with_explicit_straightness(self):
        points = np.asarray(self.CHAIN, dtype=float)
        before = TraceSettings(length_unit="px", straight_line_max_deviation=.25, **self.CORNER)
        after = TraceSettings(length_unit="px", straight_line_max_deviation=0, side_smoothness=.25, **self.CORNER)
        corners = _geometric_corners(points, before)
        self.assertTrue(corners)
        self.assertEqual(_geometric_corners(points, after), corners)
        fitted = SharedChainFitter().fit(self.CHAIN, after)
        self.assertEqual([row.source_start for row in fitted.diagnostics if row.classification == "corner"],
                         list(corners))
        # Without its own straightness the corner test would inherit the 0 deviation
        # and no noisy wall would confirm the corner.
        self.assertEqual(_geometric_corners(points, TraceSettings(
            length_unit="px", straight_line_max_deviation=0, **self.CORNER)), {})

    def test_side_smoothness_is_part_of_the_fit_cache_key(self):
        fitter = SharedChainFitter()
        fitter.fit(self.CHAIN, TraceSettings(length_unit="px", side_smoothness=.25, **self.CORNER))
        fitter.fit(self.CHAIN, TraceSettings(length_unit="px", side_smoothness=.01, **self.CORNER))
        self.assertEqual(fitter.cache_entries, 2)


if __name__ == "__main__":
    unittest.main()
