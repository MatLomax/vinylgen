"""Behavioural tests for width-based thin-feature removal.

``remove_thin_features`` removes whole 4-connected components that vanish under an
erosion of radius ``minimum_width // 2`` — long-but-thin features such as
anti-aliased seams — then folds the removed pixels into a touching palette colour,
leaving every surviving region pixel-for-pixel intact. These tests pin the core
divergence from area-based cleanup, that compact bodies are untouched (even at a
width larger than the region), audit honesty, the width no-op, protection, palette
closure, and determinism.
"""

from __future__ import annotations

import unittest

import numpy as np

from vinylgen.segmentation import remove_small_regions, remove_thin_features


# Distinct palette centres: A (seam), B (blob), C (background). Chosen so a removed
# seam pixel's only non-A 4-neighbour is C, keeping the fold target well-defined.
_CENTRES = np.array(
    [
        [50.0, 60.0, 70.0],   # 0 = A (seam colour)
        [20.0, -40.0, 10.0],  # 1 = B (blob colour)
        [90.0, 5.0, -30.0],   # 2 = C (background colour)
    ]
)
_A, _B, _C = 0, 1, 2


def _divergence_scene() -> np.ndarray:
    """A 420x420 background of C with a 400x4 thin seam of A and a 40x40 blob of B.

    Seam and blob have equal area (1600 px) and sit apart, so no area threshold can
    remove one without the other while width-based component-vanish removal can.
    """
    scene = np.full((420, 420), _C, dtype=np.int32)
    scene[10:410, 20:24] = _A       # vertical seam, 400 tall x 4 wide = 1600 px
    scene[200:240, 300:340] = _B    # compact blob, 40 x 40 = 1600 px
    return scene


class ThinFeatureDivergence(unittest.TestCase):
    def test_width_removal_separates_seam_from_equal_area_blob(self):
        scene = _divergence_scene()
        self.assertEqual(int((scene == _A).sum()), 1600)
        self.assertEqual(int((scene == _B).sum()), 1600)

        # minimum_width=6 -> r=3: the 4-wide seam erodes to nothing (its whole
        # component vanishes and is removed), while the 40-wide blob keeps its core
        # and is left pixel-for-pixel untouched.
        result, audit = remove_thin_features(scene, _CENTRES, minimum_width=6)

        self.assertEqual(int((result == _A).sum()), 0, "seam must be fully removed")
        self.assertEqual(
            int((result == _B).sum()), 1600, "compact blob must be untouched"
        )
        self.assertEqual(audit["radius"], 3)
        # Exactly the seam component was removed: no corners of the blob are shaved.
        self.assertEqual(audit["removed_pixels"], 1600)
        self.assertEqual(audit["removed_regions"], 1)
        self.assertGreater(audit["passes"], 0)
        # Audit honesty: removed_pixels equals the pixels that actually changed.
        self.assertEqual(
            sum(row["pixels"] for row in audit["replacements"]), 1600
        )
        # Palette closure holds even on this real scene.
        self.assertTrue(
            set(np.unique(result).tolist()).issubset({_A, _B, _C})
        )

    def test_compact_body_and_background_untouched_at_oversized_width(self):
        """A width far larger than a compact region must not shave or grow it.

        A morphological opening would erode the blob's convex corners into the
        background and, symmetrically, fold background corners into the blob; the
        component-vanish rule leaves both byte-identical because neither the blob
        nor the background vanishes under the erosion.
        """
        scene = np.full((420, 420), _C, dtype=np.int32)
        scene[200:240, 300:340] = _B  # 40x40 blob, no thin feature anywhere

        # r=12 (width 24) is well over half the 40 px blob width, the exact regime
        # that corrupted the opening approach.
        result, audit = remove_thin_features(scene, _CENTRES, minimum_width=24)

        self.assertTrue(
            np.array_equal(result, scene), "no region vanishes, so nothing changes"
        )
        self.assertEqual(audit["removed_pixels"], 0)
        self.assertEqual(audit["removed_regions"], 0)
        self.assertEqual(audit["replacements"], [])

    def test_area_cleanup_cannot_produce_the_same_divergence(self):
        """An equal-area threshold either keeps BOTH regions or folds BOTH.

        This is exactly the outcome width-based removal avoids: no ``minimum_pixels``
        value removes the seam while sparing the blob, because they are the same area.
        """
        scene = _divergence_scene()

        # Threshold just below the shared area: both regions are stable, so neither
        # folds.
        keep_both, _ = remove_small_regions(scene, _CENTRES, minimum_pixels=1600)
        self.assertEqual(int((keep_both == _A).sum()), 1600)
        self.assertEqual(int((keep_both == _B).sum()), 1600)

        # Threshold just above the shared area: both regions fold away together.
        fold_both, _ = remove_small_regions(scene, _CENTRES, minimum_pixels=1601)
        self.assertEqual(int((fold_both == _A).sum()), 0)
        self.assertEqual(int((fold_both == _B).sum()), 0)


class ThinFeatureContract(unittest.TestCase):
    def test_width_zero_and_one_are_byte_identical_noops(self):
        rng = np.random.default_rng(11)
        label_map = rng.integers(0, 3, size=(40, 40)).astype(np.int32)
        for minimum_width in (0, 1):
            result, audit = remove_thin_features(label_map, _CENTRES, minimum_width)
            self.assertTrue(np.array_equal(result, label_map))
            self.assertEqual(result.dtype, label_map.dtype)
            self.assertEqual(
                audit,
                {
                    "minimum_width": minimum_width,
                    "radius": 0,
                    "passes": 0,
                    "removed_regions": 0,
                    "removed_pixels": 0,
                    "replacements": [],
                },
            )

    def test_protected_pixels_keep_their_colour(self):
        scene = _divergence_scene()
        protected = np.zeros(scene.shape, dtype=bool)
        protected[10:410, 20:24] = True  # protect the whole seam
        result, audit = remove_thin_features(
            scene, _CENTRES, minimum_width=6, protected_features=protected
        )
        # Every protected seam pixel keeps colour A.
        self.assertTrue(np.all(result[protected] == _A))
        self.assertEqual(int((result == _A).sum()), 1600)

    def test_palette_closure(self):
        rng = np.random.default_rng(303)
        for _ in range(30):
            size = int(rng.integers(20, 60))
            colours = int(rng.integers(2, 6))
            label_map = rng.integers(0, colours, size=(size, size)).astype(np.int32)
            centres = rng.normal(size=(colours, 3)) * 30.0
            minimum_width = int(rng.integers(2, 7))
            result, _ = remove_thin_features(label_map, centres, minimum_width)
            self.assertTrue(
                set(np.unique(result).tolist()).issubset(
                    set(np.unique(label_map).tolist())
                )
            )

    def test_all_thin_field_reverts_with_honest_zero_audit(self):
        # A whole single-colour field narrower than the width is one vanishing
        # component with no surviving neighbour, so every pixel reverts to its own
        # colour. The audit must report the real (zero) change, not the geometric
        # flag count.
        field = np.zeros((400, 4), dtype=np.int32)
        result, audit = remove_thin_features(field, _CENTRES, minimum_width=6)
        self.assertTrue(np.array_equal(result, field))
        self.assertEqual(audit["removed_pixels"], 0)
        self.assertEqual(audit["removed_regions"], 0)
        self.assertEqual(audit["replacements"], [])

    def test_enclosed_same_colour_pocket_is_not_counted_as_removed(self):
        # A surviving A body holds a lone A pixel walled off by a 1-px B ring. The
        # ring vanishes and folds to A; the enclosed A pixel also vanishes but
        # refills to A (distance 0). Only the ring actually changed, so the audit
        # must count 8 px / 1 region, and removed_pixels must equal the replacement
        # total (the honesty invariant).
        scene = np.full((9, 9), _A, dtype=np.int32)
        scene[3:6, 3:6] = _B
        scene[4, 4] = _A  # lone A pixel enclosed by the 8-px B ring
        result, audit = remove_thin_features(scene, _CENTRES, minimum_width=2)
        self.assertTrue(np.all(result == _A))
        self.assertEqual(
            audit["removed_pixels"], sum(row["pixels"] for row in audit["replacements"])
        )
        self.assertEqual(audit["removed_pixels"], 8)
        self.assertEqual(audit["removed_regions"], 1)

    def test_deterministic_across_runs(self):
        rng = np.random.default_rng(2718)
        for _ in range(20):
            size = int(rng.integers(20, 50))
            colours = int(rng.integers(2, 6))
            label_map = rng.integers(0, colours, size=(size, size)).astype(np.int32)
            centres = rng.normal(size=(colours, 3)) * 30.0
            minimum_width = int(rng.integers(2, 8))
            first, first_audit = remove_thin_features(label_map, centres, minimum_width)
            second, second_audit = remove_thin_features(label_map, centres, minimum_width)
            self.assertTrue(np.array_equal(first, second))
            self.assertEqual(first_audit, second_audit)


if __name__ == "__main__":
    unittest.main()
