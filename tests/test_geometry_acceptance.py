import math
import unittest

import numpy as np
from scipy import ndimage

from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import RunDiagnostic, SharedChainFitter, ring_path
from vinylgen.validation import (
    source_thin_feature_mask,
    validate_postfit_protected_features,
)


class GeometryAcceptanceTests(unittest.TestCase):
    """Behavioural gates for the raster-evidence/vector-model boundary."""

    def test_diagonal_pixel_staircase_becomes_one_line(self):
        # This is the exact kind of alternating pixel run visible on the left
        # edge of the Griffin's wing.  Its individual steps are evidence for a
        # diagonal, not corners that belong in the SVG.
        staircase = []
        for x in range(25):
            staircase.extend(((float(x), float(x // 2)),
                              (float(x + 1), float(x // 2))))
        fitted = SharedChainFitter().fit(
            staircase,
            TraceSettings(length_unit="px", curve_fit_tolerance=0.3),
        )

        self.assertEqual(fitted.metrics.lines, 1)
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.final_nodes, 1)
        self.assertGreater(fitted.metrics.as_dict()["node_reduction_ratio"], 0.95)
        self.assertEqual([row.classification for row in fitted.diagnostics],
                         ["straight"])
        self.assertFalse(any(row.dense_fallback for row in fitted.diagnostics))

    def test_building_walls_are_lines_and_real_corners_are_hard(self):
        # Repeated samples and a little subpixel wobble emulate distant raster
        # architecture.  The outline is still a rectangle with four sustained
        # walls, not a rounded four-cubic blob.
        points = [
            (0, 0), (3, .1), (6, -.1), (9, .1), (12, 0),
            (12.1, 3), (11.9, 6), (12.1, 9), (12, 12),
            (9, 11.9), (6, 12.1), (3, 11.9), (0, 12),
            (-.1, 9), (.1, 6), (-.1, 3), (0, 0),
        ]
        fitted = ring_path(points, TraceSettings(
            length_unit="px", curve_fit_tolerance=0.3,
            corner_analysis_span=4,
            corner_threshold=math.degrees(0.7),
        ))

        self.assertEqual(fitted.metrics.lines, 4)
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertEqual(fitted.metrics.final_nodes, 4)

    def test_smooth_feather_edge_remains_cubic(self):
        x = np.linspace(0, 24, 49)
        feather = [(float(value), float(5 * np.sin(value * np.pi / 24)))
                   for value in x]
        fitted = SharedChainFitter().fit(
            feather,
            TraceSettings(length_unit="px", curve_fit_tolerance=0.25),
        )

        self.assertGreater(fitted.metrics.cubics, 0)
        self.assertLessEqual(fitted.metrics.final_nodes, 3)
        self.assertIn("curve", {row.classification for row in fitted.diagnostics})
        self.assertFalse(any(row.dense_fallback for row in fitted.diagnostics))

    def test_intentional_thin_diagonal_survives_colour_width_and_topology(self):
        source = np.zeros((64, 64), dtype=bool)
        for position in range(10, 54):
            source[position, position] = True
            source[position, position + 1] = True
        source = ndimage.binary_dilation(source, iterations=1)
        candidate = source.copy()
        labels = np.zeros(source.shape, dtype=np.int16)
        labels[source] = 2

        audit = validate_postfit_protected_features(
            source,
            candidate,
            reference_labels=labels,
            candidate_labels=labels,
            maximum_deviation=1,
        )

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["source_components"], 1)
        self.assertEqual(audit["components"][0]["source_label"], 2)
        self.assertTrue(audit["components"][0]["topology_preserved"])
        self.assertGreaterEqual(audit["components"][0]["width_ratio"], 0.9)

    def test_one_sided_antialias_fringe_is_not_protected(self):
        image = np.empty((48, 80, 3), dtype=np.uint8)
        image[:, :38] = (20, 25, 35)
        image[:, 38] = (65, 70, 78)
        image[:, 39] = (145, 150, 155)
        image[:, 40:] = (225, 225, 220)

        protected = source_thin_feature_mask(image)

        self.assertFalse(protected.any())

    def test_local_recovery_and_dense_fallback_have_explicit_audit_contract(self):
        recovered = RunDiagnostic(
            "curve", 12, 30, 0.42, 2,
            recovery_attempts=2,
        ).as_dict()
        fallback = RunDiagnostic(
            "curve", 31, 46, 0.9, 15,
            recovery_attempts=3,
            dense_fallback=True,
            fallback_reason="maximum deviation still exceeded after local recovery",
        ).as_dict()

        self.assertEqual(recovered["recovery_attempts"], 2)
        self.assertFalse(recovered["dense_fallback"])
        self.assertIsNone(recovered["fallback_reason"])
        self.assertEqual(fallback["recovery_attempts"], 3)
        self.assertTrue(fallback["dense_fallback"])
        self.assertIn("maximum deviation", fallback["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
