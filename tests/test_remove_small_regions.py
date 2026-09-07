"""Byte-identity proof for the localized ``remove_small_regions``.

The optimization must be a pure performance change: for every input the new
implementation must return an array equal to the original's and an audit dict
equal in every field. ``_reference_remove_small_regions`` below is a VERBATIM
copy of the pre-optimization function body (from ``git show HEAD`` at the time of
the change, when only this function was being modified). Each fuzz family is
built to hit the four documented byte-identity landmines:

1. Small-component iteration / apply order (dense multi-colour speck grids force
   many overlapping folds in a single pass).
2. Global ``stable_pixels`` maintained across passes (cascades where a speck only
   folds after a neighbour folds and becomes stable, including bodies that span
   several fold windows).
3. Neighbour selection and nearest-centre tie-breaks (random centres, colours
   sharing boundaries).
4. Audit dict identity (``passes``, ``removed_regions``, ``removed_pixels``,
   ``protected_intentional_lines``, ``replacements`` rows, ``minimum_pixels``).
"""

from __future__ import annotations

import unittest

import numpy as np
from scipy import ndimage

from vinylgen.segmentation import remove_small_regions


def _reference_remove_small_regions(
    label_map: np.ndarray,
    centres: np.ndarray,
    minimum_pixels: int,
    maximum_passes: int = 8,
    protected_features: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Verbatim pre-optimization implementation, frozen as the oracle."""
    result = label_map.copy()
    component_structure = ndimage.generate_binary_structure(2, 1)
    neighbour_structure = np.ones((3, 3), dtype=np.uint8)
    replacements: dict[tuple[int, int], dict[str, int]] = {}
    protected_lines: set[tuple[int, int, int, int, int]] = set()
    passes = 0
    if minimum_pixels <= 1:
        return result, {
            "minimum_pixels": minimum_pixels,
            "passes": 0,
            "removed_regions": 0,
            "removed_pixels": 0,
            "protected_intentional_lines": 0,
            "replacements": [],
        }

    for pass_index in range(maximum_passes):
        snapshot = result.copy()
        changes: list[tuple[tuple[slice, slice], np.ndarray, int, int, int]] = []
        component_data = []
        stable_pixels = np.zeros(snapshot.shape, dtype=bool)
        for source in range(len(centres)):
            components, count = ndimage.label(
                snapshot == source, structure=component_structure
            )
            sizes = np.bincount(components.ravel())
            objects = ndimage.find_objects(components)
            component_data.append((components, sizes, objects, count))
            if count:
                stable_pixels |= (snapshot == source) & (sizes[components] >= minimum_pixels)
        for source, (components, sizes, objects, count) in enumerate(component_data):
            if not count:
                continue
            for component in np.flatnonzero((sizes < minimum_pixels) & (np.arange(len(sizes)) > 0)):
                component_slice = objects[component - 1]
                if component_slice is None:
                    continue
                y_slice, x_slice = component_slice
                bounds = (
                    slice(max(0, y_slice.start - 1), min(snapshot.shape[0], y_slice.stop + 1)),
                    slice(max(0, x_slice.start - 1), min(snapshot.shape[1], x_slice.stop + 1)),
                )
                local_components = components[bounds]
                mask = local_components == component
                if protected_features is not None and np.any(
                    protected_features[bounds] & mask
                ):
                    protected_lines.add((source, y_slice.start, y_slice.stop,
                                         x_slice.start, x_slice.stop))
                    continue
                boundary = (
                    ndimage.binary_dilation(mask, structure=neighbour_structure) & ~mask
                )
                eligible_boundary = boundary & stable_pixels[bounds]
                neighbours = np.unique(snapshot[bounds][eligible_boundary])
                neighbours = neighbours[neighbours != source]
                if not len(neighbours):
                    continue
                distances = np.linalg.norm(centres[neighbours] - centres[source], axis=1)
                nearest = int(np.argmin(distances))
                target = int(neighbours[nearest])
                changes.append((bounds, mask, source, target, int(sizes[component])))
        if not changes:
            break
        for bounds, mask, source, target, pixels in changes:
            target_view = result[bounds]
            applicable = mask & (target_view == source)
            actual = int(applicable.sum())
            if not actual:
                continue
            target_view[applicable] = target
            item = replacements.setdefault((source, target), {"regions": 0, "pixels": 0})
            item["regions"] += 1
            item["pixels"] += actual
        passes = pass_index + 1

    replacement_rows = [
        {
            "from_index": source,
            "to_index": target,
            "regions": values["regions"],
            "pixels": values["pixels"],
        }
        for (source, target), values in sorted(replacements.items())
    ]
    return result, {
        "minimum_pixels": minimum_pixels,
        "passes": passes,
        "removed_regions": sum(row["regions"] for row in replacement_rows),
        "removed_pixels": sum(row["pixels"] for row in replacement_rows),
        "protected_intentional_lines": len(protected_lines),
        "replacements": replacement_rows,
    }


def _assert_identical(testcase, label_map, centres, minimum_pixels,
                      maximum_passes=8, protected_features=None):
    ref_result, ref_audit = _reference_remove_small_regions(
        label_map, centres, minimum_pixels, maximum_passes, protected_features
    )
    new_result, new_audit = remove_small_regions(
        label_map, centres, minimum_pixels, maximum_passes, protected_features
    )
    testcase.assertTrue(
        np.array_equal(new_result, ref_result),
        msg=f"result array diverged (min={minimum_pixels})",
    )
    testcase.assertEqual(new_result.dtype, ref_result.dtype)
    testcase.assertEqual(new_audit, ref_audit)
    return ref_audit


class RemoveSmallRegionsByteIdentity(unittest.TestCase):
    def test_fuzz_matches_reference(self):
        """Broad randomized sweep over sizes, colours, thresholds, protection."""
        rng = np.random.default_rng(20260907)
        total_passes = 0
        multi_pass_cases = 0
        change_cases = 0
        protected_cases = 0
        overlap_apply_cases = 0
        cases = 0
        for _ in range(400):
            height = int(rng.integers(3, 40))
            width = int(rng.integers(3, 40))
            colours = int(rng.integers(1, 9))
            # Small minimum_pixels + few colours on small grids yields dense
            # speck fields: many folds per pass and multi-pass cascades.
            minimum_pixels = int(rng.integers(2, 12))
            label_map = rng.integers(0, colours, size=(height, width)).astype(np.int32)
            centres = rng.normal(size=(colours, 3)).astype(np.float64) * 40.0
            protected_features = None
            if rng.random() < 0.4:
                protected_features = rng.random((height, width)) < rng.uniform(0.02, 0.2)
                protected_cases += 1
            audit = _assert_identical(
                self, label_map, centres, minimum_pixels,
                protected_features=protected_features,
            )
            cases += 1
            total_passes += int(audit["passes"])
            if audit["passes"] > 1:
                multi_pass_cases += 1
            if audit["removed_regions"] > 0:
                change_cases += 1
            # Any pass that folds more regions than net colours changed proves
            # multiple folds coexisted in a pass (overlap-apply territory).
            if audit["removed_regions"] > len(audit["replacements"]):
                overlap_apply_cases += 1

        # Guard the guard: the sweep must actually exercise folding, cascades,
        # protection and dense multi-fold passes, else identity is vacuous.
        self.assertEqual(cases, 400)
        self.assertGreater(change_cases, 200)
        self.assertGreater(multi_pass_cases, 20)
        self.assertGreater(protected_cases, 0)
        self.assertGreater(overlap_apply_cases, 20)
        self.assertGreater(total_passes, 400)

    def test_noop_when_minimum_pixels_not_positive(self):
        rng = np.random.default_rng(1)
        label_map = rng.integers(0, 5, size=(20, 20)).astype(np.int32)
        centres = rng.normal(size=(5, 3))
        for minimum_pixels in (-3, 0, 1):
            ref_result, ref_audit = _reference_remove_small_regions(
                label_map, centres, minimum_pixels
            )
            new_result, new_audit = remove_small_regions(label_map, centres, minimum_pixels)
            self.assertTrue(np.array_equal(new_result, label_map))
            self.assertTrue(np.array_equal(new_result, ref_result))
            self.assertEqual(new_audit, ref_audit)
            self.assertEqual(new_audit["passes"], 0)

    def test_single_colour_never_folds(self):
        label_map = np.zeros((30, 30), dtype=np.int32)
        centres = np.array([[10.0, 20.0, 30.0]])
        audit = _assert_identical(self, label_map, centres, minimum_pixels=50)
        self.assertEqual(audit["removed_regions"], 0)
        self.assertEqual(audit["passes"], 0)

    def test_minimum_pixels_two(self):
        rng = np.random.default_rng(7)
        for _ in range(40):
            label_map = rng.integers(0, 4, size=(24, 24)).astype(np.int32)
            centres = rng.normal(size=(4, 3)) * 30.0
            _assert_identical(self, label_map, centres, minimum_pixels=2)

    def test_large_minimum_pixels_stays_identical(self):
        """A threshold larger than any random component means most colours have no
        stable body, so folds are sparse or absent. Identity must still hold on
        this near-no-op path (specks with no stable neighbour, empty stable
        field), which the dense-cascade fuzz does not otherwise cover."""
        rng = np.random.default_rng(99)
        for _ in range(30):
            label_map = rng.integers(0, 6, size=(22, 22)).astype(np.int32)
            centres = rng.normal(size=(6, 3)) * 25.0
            _assert_identical(self, label_map, centres, minimum_pixels=200)

    def test_engineered_cascade_chain(self):
        """A speck can only fold after its neighbour folds and becomes stable.

        A large stable body of colour 0 fills a 6x6 frame. A single colour-1 pixel
        sits in the corner, and all three of its (8-connected) neighbours are a
        three-pixel colour-2 speck (B) that otherwise touches the stable body. B is
        not stable, so in pass 1 only B can fold (into the body); the corner
        colour-1 pixel has no stable neighbour yet. In pass 2, B's pixels are part
        of the stable body, so the corner pixel finally folds. This is landmine 2
        in miniature: a second-pass fold that depends on the first pass's
        stable-field update. Note the fold boundary uses 8-connectivity, so all
        three surrounding cells must be non-stable to hold the corner back."""
        label_map = np.zeros((6, 6), dtype=np.int32)  # stable body of colour 0
        label_map[5, 5] = 1                           # lone colour-1 corner speck
        label_map[4, 5] = 2                           # colour-2 speck wrapping it
        label_map[5, 4] = 2                           # colour-2 speck wrapping it
        label_map[4, 4] = 2                           # diagonal wrap (8-connected)
        centres = np.array([
            [0.0, 0.0, 0.0],
            [90.0, 0.0, 0.0],
            [30.0, 0.0, 0.0],
        ])
        audit = _assert_identical(self, label_map, centres, minimum_pixels=4)
        self.assertEqual(audit["passes"], 2)
        self.assertEqual(audit["removed_regions"], 2)

    def test_overlapping_specks_share_boundary(self):
        """Dense adjacent specks of several colours around a stable core, so a
        single pass records many folds with overlapping windows and the apply
        step's ordering / non-overwrite path is exercised heavily."""
        rng = np.random.default_rng(2024)
        for _ in range(60):
            grid = np.zeros((14, 14), dtype=np.int32)
            grid[4:10, 4:10] = 0                      # stable core
            speck = rng.integers(1, 6, size=(14, 14))
            edge = np.ones((14, 14), dtype=bool)
            edge[4:10, 4:10] = False
            grid[edge] = speck[edge].astype(np.int32)  # ring of mixed specks
            centres = rng.normal(size=(6, 3)) * 20.0
            _assert_identical(self, grid, centres, minimum_pixels=5)

    def test_protected_features_intersecting_specks(self):
        """Protected pixels inside specks must be recorded identically and never
        folded, across many overlaps with random protection masks."""
        rng = np.random.default_rng(555)
        saw_protected = 0
        for _ in range(60):
            label_map = rng.integers(0, 5, size=(20, 20)).astype(np.int32)
            centres = rng.normal(size=(5, 3)) * 30.0
            protected_features = rng.random((20, 20)) < 0.15
            audit = _assert_identical(
                self, label_map, centres, minimum_pixels=6,
                protected_features=protected_features,
            )
            if audit["protected_intentional_lines"] > 0:
                saw_protected += 1
        self.assertGreater(saw_protected, 0)

    def test_maximum_passes_variants(self):
        rng = np.random.default_rng(31337)
        for maximum_passes in (0, 1, 2, 3):
            for _ in range(20):
                label_map = rng.integers(0, 5, size=(18, 18)).astype(np.int32)
                centres = rng.normal(size=(5, 3)) * 30.0
                _assert_identical(
                    self, label_map, centres, minimum_pixels=5,
                    maximum_passes=maximum_passes,
                )

    def test_non_contiguous_label_dtype(self):
        """A non-C-contiguous, differently-typed label map must behave the same."""
        rng = np.random.default_rng(8)
        base = rng.integers(0, 5, size=(24, 48)).astype(np.int16)
        label_map = base[:, ::2]  # a non-contiguous view
        self.assertFalse(label_map.flags["C_CONTIGUOUS"])
        centres = rng.normal(size=(5, 3)) * 30.0
        _assert_identical(self, label_map, centres, minimum_pixels=5)


if __name__ == "__main__":
    unittest.main()
