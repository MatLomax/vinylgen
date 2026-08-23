import tempfile
import unittest
from pathlib import Path

import numpy as np

from vinylgen.colour import palette_costs, srgb_to_lab
from vinylgen.contracts import SegmentationBundle
from vinylgen.segmentation import (
    detect_intentional_strokes,
    remove_small_regions,
    resolve_ambiguous_boundaries,
)


class SourceAwareSegmentationTests(unittest.TestCase):
    def test_explicit_lock_has_priority_over_stroke_normalization(self):
        source = np.full((24, 40, 3), [80.0, 0.0, 0.0], dtype=np.float32)
        source[10:14, :] = [10.0, 0.0, 0.0]
        labels = np.zeros((24, 40), dtype=np.int32)
        labels[10:14, :] = 1
        labels[11:13, 18:22] = 2
        centres = np.array([[80.0, 0.0, 0.0], [10.0, 0.0, 0.0], [55.0, 10.0, 5.0]])
        costs = np.linalg.norm(source[:, :, None, :] - centres[None, None, :, :], axis=3)
        locked = np.zeros(labels.shape, dtype=bool)
        locked[11:13, 18:22] = True

        normalized, protected, audit = detect_intentional_strokes(
            source, labels, costs, centres, locked_features=locked
        )

        self.assertTrue(np.all(normalized[locked] == 2))
        self.assertTrue(np.all(protected[locked]))
        self.assertEqual(audit["explicitly_locked_pixels"], 8)

    def test_curved_attached_dark_ridge_is_protected_before_cleanup(self):
        height = width = 72
        yy, xx = np.indices((height, width))
        rgb = np.full((height, width, 3), 225, dtype=np.uint8)
        curve_y = 24 + ((xx - 36) ** 2 / 100).astype(int)
        ridge = np.abs(yy - curve_y) <= 1
        # Attach the curve to a large dark object; component-size logic cannot
        # distinguish the outline from that object.
        ridge |= (xx < 9) & (yy > 20)
        rgb[ridge] = 20
        centres_rgb = np.array([[225, 225, 225], [20, 20, 20]], dtype=np.uint8)
        centres = srgb_to_lab(centres_rgb)
        lab = srgb_to_lab(rgb.reshape(-1, 3)).reshape(height, width, 3)
        costs = palette_costs(lab.reshape(-1, 3), centres).reshape(height, width, 2)
        labels = costs.argmin(axis=2).astype(np.int32)

        labels, protected, audit = detect_intentional_strokes(
            lab, labels, costs, centres, minimum_contrast=10, minimum_length=8
        )

        self.assertGreater(audit["protected_components"], 0)
        self.assertGreater(np.count_nonzero(protected & ridge), 50)
        cleaned, _ = remove_small_regions(
            labels, centres, minimum_pixels=200, protected_features=protected
        )
        self.assertGreater(np.count_nonzero((cleaned == 1) & ridge), 50)

    def test_isolated_dark_speck_is_not_a_protected_stroke(self):
        rgb = np.full((40, 40, 3), 230, dtype=np.uint8)
        rgb[20, 20] = 10
        centres = srgb_to_lab(np.array([[230] * 3, [10] * 3], dtype=np.uint8))
        lab = srgb_to_lab(rgb.reshape(-1, 3)).reshape(40, 40, 3)
        costs = palette_costs(lab.reshape(-1, 3), centres).reshape(40, 40, 2)
        labels = costs.argmin(axis=2).astype(np.int32)

        _, protected, _ = detect_intentional_strokes(lab, labels, costs, centres)

        self.assertFalse(protected[20, 20])

    def test_mixed_colour_ridge_consolidates_to_darkest_plausible_colour(self):
        rgb = np.full((40, 80, 3), 230, dtype=np.uint8)
        rgb[19:21, 8:72] = 15
        lab = srgb_to_lab(rgb.reshape(-1, 3)).reshape(40, 80, 3)
        labels = np.zeros((40, 80), dtype=np.int32)
        for start in range(8, 72, 8):
            labels[19:21, start:start + 4] = 1
            labels[19:21, start + 4:start + 8] = 2
        costs = np.full((40, 80, 3), 30.0, dtype=np.float32)
        costs[..., 0] = 0
        costs[19:21, 8:72, :] = 20
        for colour in (1, 2):
            costs[..., colour][labels == colour] = 0

        centres = srgb_to_lab(np.array([[230] * 3, [10] * 3, [35] * 3], dtype=np.uint8))
        normalized, protected, _ = detect_intentional_strokes(
            lab, labels, costs, centres, minimum_contrast=10, minimum_length=8)

        self.assertTrue(protected[19:21, 8:72].any())
        self.assertTrue(np.all(normalized[19:21, 8:72] == 1))

    def test_uncertain_aa_band_resolves_coherently(self):
        labels = np.zeros((9, 15), dtype=np.int32)
        labels[:, 8:] = 1
        labels[::2, 7] = 1
        costs = np.full((9, 15, 2), 30.0, dtype=np.float32)
        costs[:, :7, 0], costs[:, :7, 1] = 0, 40
        costs[:, 8:, 0], costs[:, 8:, 1] = 40, 0
        costs[:, 7, :] = 10

        resolved, audit = resolve_ambiguous_boundaries(
            labels, costs, np.zeros_like(labels, dtype=bool), smoothness=3
        )

        self.assertGreater(audit["changed_assignments"], 0)
        self.assertEqual(len(np.unique(resolved[:, 7])), 1)

    def test_contract_persists_complete_palette_cost_field(self):
        bundle = SegmentationBundle(
            labels=np.array([[0, 1]], dtype=np.int16),
            protected_features=np.array([[False, True]]),
            explicit_protected_features=np.array([[False, False]]),
            palette_rgb=np.array([[0, 0, 0], [255, 255, 255]], dtype=np.uint8),
            palette_costs=np.array([[[1, 9], [8, 2]]], dtype=np.float32),
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as tmp:
            path = Path(tmp) / "segmentation.npz"
            bundle.save(path)
            loaded = SegmentationBundle.load(path)
        np.testing.assert_allclose(loaded.palette_costs, bundle.palette_costs)


if __name__ == "__main__":
    unittest.main()
