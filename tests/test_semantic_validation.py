import unittest
from unittest.mock import patch

import numpy as np
from scipy import ndimage

from vinylgen.validation import (
    semantic_warning_mask,
    source_thin_feature_mask,
    validate_feature_roi,
    validate_postfit_protected_features,
    validate_semantic_preservation,
)

from rgb_scene import Region, Scene, ellipse, render_rgb


class SemanticValidationTests(unittest.TestCase):
    @staticmethod
    def curved_feature(*, shift: int = 0, thickness: int = 5,
                       omit_middle: bool = False) -> np.ndarray:
        mask = np.zeros((70, 100), dtype=bool)
        x = np.arange(12, 88)
        y = np.rint(34 + shift + 10 * np.sin((x - 12) * np.pi / 75)).astype(int)
        mask[y, x] = True
        dilation = max(0, (thickness - 1) // 2)
        if dilation:
            mask = ndimage.binary_dilation(mask, iterations=dilation)
        if omit_middle:
            mask[:, 45:56] = False
        return mask

    def test_smoothed_shifted_protected_curve_passes_postfit_validation(self):
        reference = self.curved_feature()
        # A one-pixel positional change represents an independently smoothed
        # vector render, not exact replication of source raster geometry.
        candidate = self.curved_feature(shift=1)

        audit = validate_postfit_protected_features(
            reference, candidate, maximum_deviation=2)

        self.assertTrue(audit["passed"])
        self.assertEqual(audit["topology_failures"], 0)
        self.assertGreaterEqual(audit["components"][0]["area_ratio"], 0.8)

    def test_omitted_and_collapsed_protected_curves_fail_postfit_validation(self):
        reference = self.curved_feature()
        omitted = self.curved_feature(omit_middle=True)
        collapsed = self.curved_feature(thickness=1)

        omitted_audit = validate_postfit_protected_features(
            reference, omitted, maximum_deviation=2)
        collapsed_audit = validate_postfit_protected_features(
            reference, collapsed, maximum_deviation=2)

        self.assertFalse(omitted_audit["passed"])
        self.assertGreaterEqual(omitted_audit["topology_failures"], 1)
        self.assertFalse(collapsed_audit["passed"])
        self.assertGreaterEqual(collapsed_audit["width_failures"], 1)

    def test_many_components_use_local_distance_transforms(self):
        reference = np.zeros((600, 800), dtype=bool)
        for y in range(20, 581, 40):
            for x in range(20, 781, 40):
                reference[y:y + 2, x:x + 12] = True
        candidate = reference.copy()
        original = ndimage.distance_transform_edt
        shapes = []

        def observed(values, *args, **kwargs):
            shapes.append(values.shape)
            return original(values, *args, **kwargs)

        with patch("vinylgen.validation.ndimage.distance_transform_edt",
                   side_effect=observed):
            audit = validate_postfit_protected_features(
                reference, candidate, maximum_deviation=2)

        self.assertTrue(audit["passed"])
        self.assertGreater(audit["source_components"], 200)
        self.assertEqual(shapes.count(reference.shape), 2)
        local_shapes = [shape for shape in shapes if shape != reference.shape]
        self.assertTrue(local_shapes)
        self.assertLess(max(height * width for height, width in local_shapes), 500)

    def test_nearby_wrong_colour_does_not_count_as_protected_survival(self):
        reference = np.zeros((40, 70), dtype=bool)
        reference[19:22, 15:50] = True
        candidate = np.zeros_like(reference)
        candidate[20:23, 15:50] = True
        reference_labels = np.zeros(reference.shape, dtype=np.int16)
        reference_labels[reference] = 1
        candidate_labels = np.zeros(reference.shape, dtype=np.int16)
        candidate_labels[candidate] = 2

        audit = validate_postfit_protected_features(
            reference, candidate,
            reference_labels=reference_labels,
            candidate_labels=candidate_labels,
            maximum_deviation=2)

        self.assertFalse(audit["passed"])
        self.assertEqual(audit["components"][0]["source_label"], 1)
        self.assertIn("insufficient feature recall",
                      audit["components"][0]["failure_reasons"])

    def test_label_aware_postfit_preserves_light_protected_island(self):
        source = np.full((45, 65, 3), (20, 25, 30), dtype=np.uint8)
        source[20:25, 29:36] = (245, 220, 120)
        protected = np.zeros(source.shape[:2], dtype=bool)
        protected[20:25, 29:36] = True
        labels = np.zeros(source.shape[:2], dtype=np.int16)
        labels[protected] = 1

        audit = validate_semantic_preservation(
            source, source, vector_rgb=source,
            protected_features=protected,
            segmentation_labels=labels, vector_labels=labels,
            maximum_deviation=1)

        self.assertTrue(audit["protected_features"]["postfit"]["passed"])
        self.assertTrue(audit["protected_features"]["vector_postfit"]["passed"])
        self.assertTrue(audit["passed"])

    def test_antialiased_step_edge_is_not_an_intentional_stroke(self):
        image = np.empty((40, 60, 3), dtype=np.uint8)
        image[:, :29] = 20
        image[:, 29] = 80
        image[:, 30] = 180
        image[:, 31:] = 235

        features = source_thin_feature_mask(image)

        self.assertFalse(features.any())

    def test_omitted_two_sided_dark_stroke_is_reported_without_protection_mask(self):
        source = np.full((50, 80, 3), (220, 190, 140), dtype=np.uint8)
        source[24:26, 12:68] = (12, 18, 22)
        omitted = np.full_like(source, (220, 190, 140))

        audit = validate_semantic_preservation(source, omitted, maximum_deviation=1.5)

        self.assertGreater(audit["source_feature_pixels"], 20)
        self.assertEqual(audit["segmentation"]["component_recall"], 0.0)
        self.assertEqual(audit["segmentation"]["pixel_recall"], 0.0)
        self.assertFalse(audit["passed"])

    def test_source_segmentation_and_vector_recall_are_separate(self):
        source = np.full((50, 80, 3), 220, dtype=np.uint8)
        source[24:26, 12:68] = 10
        segmentation = source.copy()
        vector = np.full_like(source, 220)
        vector[24:26, 18:62] = 10

        audit = validate_semantic_preservation(
            source, segmentation, vector_rgb=vector, maximum_deviation=1)

        self.assertEqual(audit["segmentation"]["component_recall"], 1.0)
        self.assertEqual(audit["segmentation"]["geometric_deviation_max_px"], 0.0)
        self.assertEqual(audit["vector"]["component_recall"], 1.0)
        self.assertLess(audit["vector"]["pixel_recall"], 1.0)
        self.assertGreater(audit["vector"]["geometric_deviation_max_px"], 1.0)

    def test_topology_reports_a_wholly_missing_feature_component(self):
        source = np.full((70, 90, 3), 230, dtype=np.uint8)
        source[15:17, 10:36] = 5
        source[50:52, 52:82] = 5
        target = source.copy()
        target[50:52, 52:82] = 230

        audit = validate_semantic_preservation(source, target, maximum_deviation=1)

        self.assertEqual(audit["segmentation"]["source_components"], 2)
        self.assertEqual(audit["segmentation"]["recalled_components"], 1)
        self.assertEqual(audit["segmentation"]["component_recall"], 0.5)
        self.assertFalse(audit["segmentation"]["topology_preserved"])

    def test_warning_mask_marks_only_unpreserved_source_feature(self):
        source = np.full((50, 80, 3), 220, dtype=np.uint8)
        source[12:14, 10:35] = 10
        source[35:37, 45:70] = 10
        target = source.copy()
        target[35:37, 45:70] = 220

        warning = semantic_warning_mask(source, target, maximum_deviation=1)

        self.assertFalse(warning[12:14, 10:35].any())
        self.assertTrue(warning[35:37, 45:70].any())


# Geometry of the synthetic replacement for the griffin-head outline corpus
# checks below: a filled ellipse ("head") on a lighter background, with a
# uniform dark outline built as a slightly larger ellipse underneath it (both
# concentric, so the visible ring is the outer ellipse's edge minus the width
# by which the inner, lighter fill ellipse is shrunk on each axis).
_OUTLINE_CENTRE = (100.0, 70.0)
_OUTLINE_OUTER_RX, _OUTLINE_OUTER_RY = 70.0, 48.0
_OUTLINE_RING_WIDTH = 3.0
_OUTLINE_ROI_BOUNDS = (0.20, 0.04, 0.80, 0.28)  # left, top, right, bottom


def _synthetic_outline_scene() -> Scene:
    """A filled ellipse with a uniform 3 px dark outline on a lighter
    background, at a canvas size comparable to the griffin-castle ROI
    (``images/griffin-castle.png`` normalised bounds ``(0.405, 0.065, 0.495,
    0.145)`` crop to roughly 134x85 px) the moved
    ``tests/fullimage/test_semantic_validation_corpus.py`` checks used."""
    inner_rx = _OUTLINE_OUTER_RX - _OUTLINE_RING_WIDTH
    inner_ry = _OUTLINE_OUTER_RY - _OUTLINE_RING_WIDTH
    palette = [
        {"name": "background", "rgb": [222, 205, 178]},
        {"name": "ring-dark", "rgb": [28, 20, 16]},
        {"name": "fill", "rgb": [150, 118, 80]},
    ]
    regions = [
        Region(1, ellipse(_OUTLINE_CENTRE, _OUTLINE_OUTER_RX, _OUTLINE_OUTER_RY)),
        Region(2, ellipse(_OUTLINE_CENTRE, inner_rx, inner_ry)),
    ]
    return Scene(200, 140, palette, 0, regions, family="griffin-outline-corpus")


def _outline_arc_length_in_roi(width: int, height: int) -> float:
    """The outer ellipse boundary's length inside the normalised ROI box.

    Sampled finely and summed as chords rather than looked up as a constant,
    so the expected-evidence threshold below is derived from the same ellipse
    geometry the scene is built from, not a number copied from the griffin
    corpus.
    """
    left, top, right, bottom = _OUTLINE_ROI_BOUNDS
    x0, x1 = left * width, right * width
    y0, y1 = top * height, bottom * height
    theta = np.linspace(0.0, 2.0 * np.pi, 200_000, endpoint=False)
    cx, cy = _OUTLINE_CENTRE
    x = cx + _OUTLINE_OUTER_RX * np.cos(theta)
    y = cy + _OUTLINE_OUTER_RY * np.sin(theta)
    inside = (x0 <= x) & (x < x1) & (y0 <= y) & (y < y1)
    segment_lengths = np.hypot(np.diff(x), np.diff(y))
    return float(segment_lengths[inside[:-1] & inside[1:]].sum())


class SyntheticOutlineCorpusTest(unittest.TestCase):
    """Synthetic replacement for the griffin-head outline checks moved to
    ``tests/fullimage/test_semantic_validation_corpus.py``: an AA-rendered
    filled shape with a dark outline stands in for the griffin crown/forehead
    outline, evidenced the same way (``source_thin_feature_mask``) and
    audited the same way (``validate_feature_roi``)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scene = _synthetic_outline_scene()
        cls.source = render_rgb(cls.scene, supersample=8)

    def _roi_pixel_bounds(self):
        height, width = self.source.shape[:2]
        left, top, right, bottom = _OUTLINE_ROI_BOUNDS
        return (round(left * width), round(top * height),
                round(right * width), round(bottom * height))

    def test_outline_is_source_feature_evidence(self) -> None:
        height, width = self.source.shape[:2]
        features = source_thin_feature_mask(self.source)
        x0, y0, x1, y1 = self._roi_pixel_bounds()
        roi = features[y0:y1, x0:x1]

        arc_length = _outline_arc_length_in_roi(width, height)
        # Half the analytic arc-length*ring-width area allows for the AA edge
        # softening and the ridge detector's own contrast margin, while still
        # requiring substantial (not incidental) evidence.
        expected_minimum = 0.5 * arc_length * _OUTLINE_RING_WIDTH
        self.assertGreater(expected_minimum, 0.0)
        self.assertGreaterEqual(int(roi.sum()), expected_minimum)

    def test_roi_accepts_preserved_outline_and_rejects_omission(self) -> None:
        x0, y0, x1, y1 = self._roi_pixel_bounds()
        omitted = self.source.copy()
        omitted[y0:y1, x0:x1] = self.source[y0, x0]

        preserved_audit = validate_feature_roi(self.source, self.source, _OUTLINE_ROI_BOUNDS)
        omitted_audit = validate_feature_roi(self.source, omitted, _OUTLINE_ROI_BOUNDS)

        self.assertTrue(preserved_audit["passed"])
        self.assertFalse(omitted_audit["passed"])


if __name__ == "__main__":
    unittest.main()
