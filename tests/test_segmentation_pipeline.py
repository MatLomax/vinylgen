import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import ndimage

from vinylgen.colour import palette_costs, srgb_to_lab
from vinylgen.contracts import SegmentationBundle
from vinylgen.ridge import two_sided_dark_ridge_response
from vinylgen.segmentation import (
    detect_intentional_strokes,
    remove_small_regions,
    resolve_ambiguous_boundaries,
)


def _reference_detect_intentional_strokes(
    source_lab, labels, costs, centres,
    minimum_contrast=12.0, minimum_length=8, locked_features=None,
):
    """Full-image reference for detect_intentional_strokes.

    Shares the production ridge-response stage, then constructs every component
    band against the whole image rather than a local window, so it isolates and
    fixes the expected normalized/protected/audit results the windowed
    production loop must reproduce bit-for-bit.
    """
    lightness = source_lab[..., 0].astype(np.float32)
    response, best_radius = two_sided_dark_ridge_response(lightness)
    ordered = np.partition(costs, 1, axis=2)
    margin = ordered[..., 1] - ordered[..., 0]
    raw_seeds = (response >= minimum_contrast) & (margin >= 2.0)
    seeds = ndimage.binary_closing(raw_seeds, structure=np.ones((3, 3)), iterations=1)
    components, count = ndimage.label(seeds, structure=np.ones((3, 3)))
    objects = ndimage.find_objects(components)
    normalized = labels.copy()
    locked = (np.zeros_like(seeds) if locked_features is None
              else np.asarray(locked_features, dtype=bool))
    if locked.shape != labels.shape:
        raise ValueError("locked-feature dimensions differ from segmentation labels")
    protected = locked.copy()
    retained = 0
    retained_by_colour: dict[int, int] = {}
    for component in range(1, count + 1):
        item = objects[component - 1]
        if item is None:
            continue
        local_component = components[item] == component
        span = max(item[0].stop - item[0].start, item[1].stop - item[1].start)
        population = int(local_component.sum())
        if span < minimum_length or population < minimum_length:
            continue
        component_labels = np.unique(labels[item][local_component])
        local_response = response[item]
        core_threshold = float(np.percentile(local_response[local_component], 75))
        core = local_component & (local_response >= core_threshold)
        component_costs = costs[item][core][:, component_labels].mean(axis=0)
        target = int(min(
            range(len(component_labels)),
            key=lambda index: (float(component_costs[index]),
                               float(centres[component_labels[index], 0]),
                               int(component_labels[index])),
        ))
        target = int(component_labels[target])
        target_cost = costs[..., target]
        best_cost = costs.min(axis=2)
        global_component = np.zeros_like(seeds)
        global_component[item] = local_component
        band = global_component.copy()
        for radius in (2, 3):
            radius_centres = global_component & (best_radius == radius)
            if radius_centres.any():
                band |= ndimage.binary_dilation(radius_centres, iterations=radius - 1)
        band &= ((target_cost <= best_cost + minimum_contrast)
                 & (response >= minimum_contrast * 0.25)) | global_component
        band &= ~locked
        normalized[band] = target
        protected |= band
        retained += 1
        retained_by_colour[target] = retained_by_colour.get(target, 0) + 1
    return normalized, protected, {
        "candidate_pixels": int(raw_seeds.sum()),
        "protected_pixels": int(protected.sum()),
        "protected_components": retained,
        "protected_components_by_colour": retained_by_colour,
        "explicitly_locked_pixels": int(locked.sum()),
        "minimum_two_sided_contrast": minimum_contrast,
        "minimum_continuity_length": minimum_length,
    }


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

    def test_windowed_band_matches_full_image_reference(self):
        # A synthetic scene that drives every path the windowed loop touches:
        #  - a full-height stroke at column 1 whose bounding box reaches the top,
        #    bottom and left edges, so all three low/high window-clamp bounds
        #    fire (a missing max(0, ...)/min(dim, ...) would pick the wrong rows);
        #  - a stroke one column in from the right edge to fire the last clamp;
        #  - stroke widths 1, 3 and 5 producing best_radius 1, 2 and 3, so both
        #    dilation branches run;
        #  - two width-5 strokes three pixels apart whose padded windows overlap
        #    and whose radius-3 dilated bands meet, so band writes land in a
        #    region an earlier component already wrote;
        #  - a mixed-colour stroke with a locked minority-colour row, so
        #    ~locked has an observable effect on the normalized labels.
        height, width = 60, 96
        rgb = np.full((height, width, 3), 225, dtype=np.uint8)
        rgb[0:60, 1] = 20                       # full-height width-1 (left/top/bottom clamps)
        rgb[6:41, 94] = 20                       # width-1 near the right edge (right clamp)
        rgb[8:52, 20:23] = 20                    # width-3 (best_radius 2)
        rgb[8:52:2, 20:23] = 50                  # ...mixed with a second dark colour
        rgb[6:54, 40:45] = 20                    # width-5, best_radius 3
        rgb[6:54, 48:53] = 20                    # width-5 three pixels away; windows overlap

        centres_rgb = np.array([[225, 225, 225], [20, 20, 20], [50, 50, 50]], dtype=np.uint8)
        centres = srgb_to_lab(centres_rgb)
        lab = srgb_to_lab(rgb.reshape(-1, 3)).reshape(height, width, 3)
        costs = palette_costs(lab.reshape(-1, 3), centres).reshape(height, width, 3)
        labels = costs.argmin(axis=2).astype(np.int32)

        locked = np.zeros((height, width), dtype=bool)
        locked[10, 20:23] = True  # a minority-colour row inside the mixed stroke

        normalized, protected, audit = detect_intentional_strokes(
            lab, labels, costs, centres, minimum_contrast=10, minimum_length=8,
            locked_features=locked,
        )
        ref_normalized, ref_protected, ref_audit = _reference_detect_intentional_strokes(
            lab, labels, costs, centres, minimum_contrast=10, minimum_length=8,
            locked_features=locked,
        )
        # Without the lock the mixed stroke consolidates the locked row to its
        # target colour; the lock must hold it, proving ~locked is exercised.
        unlocked, _, _ = detect_intentional_strokes(
            lab, labels, costs, centres, minimum_contrast=10, minimum_length=8,
        )

        self.assertGreaterEqual(audit["protected_components"], 3)
        self.assertFalse(np.array_equal(normalized[locked], unlocked[locked]))
        self.assertTrue(np.array_equal(normalized, ref_normalized))
        self.assertTrue(np.array_equal(protected, ref_protected))
        self.assertEqual(audit, ref_audit)

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
