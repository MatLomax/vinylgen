import math
import unittest

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import HardAnchor, SharedChainFitter, ring_path


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


if __name__ == "__main__":
    unittest.main()
