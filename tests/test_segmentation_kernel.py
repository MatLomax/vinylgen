import unittest
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from scipy import ndimage

# Import ONLY the segmentation module (which pulls vinylgen.ridge). This test
# never touches vinylgen.palette or vinylgen.colour, and never runs the pipeline.
from vinylgen.segmentation import (
    _resolve_boundary_pass,
    collapse_dither_speckle,
    resolve_ambiguous_boundaries,
)


# --------------------------------------------------------------------------- #
# Verbatim pre-port references. These freeze the exact numpy/scipy formulations
# the fused Numba kernels must reproduce bit-for-bit.
# --------------------------------------------------------------------------- #
def _reference_resolve_pass(result, costs, ambiguous, smoothness):
    """One red/black ICM sweep, verbatim from the pre-port numpy formulation."""
    result = result.copy()
    palette_size = costs.shape[2]
    count = 0
    yy, xx = np.indices(result.shape)
    for parity in (0, 1):
        active = ambiguous & (((yy + xx) & 1) == parity)
        energies = costs.copy()
        for colour in range(palette_size):
            same = np.zeros(result.shape, dtype=np.float32)
            same[1:] += result[:-1] == colour
            same[:-1] += result[1:] == colour
            same[:, 1:] += result[:, :-1] == colour
            same[:, :-1] += result[:, 1:] == colour
            energies[..., colour] += smoothness * (4.0 - same)
        proposed = energies.argmin(axis=2).astype(result.dtype)
        change = active & (proposed != result)
        count += int(change.sum())
        result[change] = proposed[change]
    return result, count


def _reference_resolve_full(labels, costs, protected, ambiguity_margin=7.0,
                            smoothness=2.5, passes=5):
    """Verbatim pre-port resolve_ambiguous_boundaries (multi-pass)."""
    result = labels.copy()
    ordered = np.partition(costs, 1, axis=2)
    margin = ordered[..., 1] - ordered[..., 0]
    ambiguous = (margin < ambiguity_margin) & ~protected
    changed_total = 0
    used_passes = 0
    for pass_index in range(passes):
        result, count = _reference_resolve_pass(result, costs, ambiguous, smoothness)
        if not count:
            break
        changed_total += count
        used_passes = pass_index + 1
    return result, {
        "ambiguous_pixels": int(ambiguous.sum()),
        "changed_assignments": changed_total,
        "passes": used_passes,
        "ambiguity_margin": ambiguity_margin,
        "smoothness": smoothness,
    }


def _reference_collapse(labels, despeckled_labels, count, radius, protected):
    """Verbatim pre-port collapse_dither_speckle."""
    if radius <= 0:
        return labels, {"radius": 0, "changed_assignments": 0}
    size = 2 * radius + 1
    shares = np.stack([
        ndimage.uniform_filter((labels == index).astype(np.float64),
                               size=size, mode="nearest")
        for index in range(count)
    ])
    voted = shares.argmax(axis=0).astype(labels.dtype)
    agreed = (voted == despeckled_labels) & (voted != labels) & ~protected
    result = np.where(agreed, voted, labels)
    return result, {
        "radius": radius,
        "changed_assignments": int(np.count_nonzero(agreed)),
    }


# --------------------------------------------------------------------------- #
# Input builders — structured (tie-prone dither/AA bands) and random.
# --------------------------------------------------------------------------- #
def _random_costs(height, width, palette, seed):
    rng = np.random.default_rng(seed)
    return rng.uniform(0.0, 40.0, size=(height, width, palette)).astype(np.float32)


def _random_labels(height, width, palette, seed):
    rng = np.random.default_rng(seed)
    return rng.integers(0, palette, size=(height, width)).astype(np.int32)


def _dither_labels(height, width, palette, seed):
    """A dithered field: checkerboard of colours with sparse specks, the case
    that produces exact-tie majority shares stressing the argmax tie-break and
    scipy's separable running-sum rounding."""
    rng = np.random.default_rng(seed)
    yy, xx = np.indices((height, width))
    labels = ((yy + xx) % palette).astype(np.int32)
    speck = rng.random((height, width)) < 0.15
    labels[speck] = rng.integers(0, palette, size=(height, width))[speck]
    labels[: height // 2, : width // 2] = 0
    return labels


class ResolveBoundaryKernelBitExactnessTest(unittest.TestCase):
    def _assert_pass_identical(self, labels, costs, ambiguous, smoothness):
        expected_result, expected_count = _reference_resolve_pass(
            labels, costs, ambiguous, smoothness
        )
        result = np.ascontiguousarray(labels.copy())
        count = int(_resolve_boundary_pass(
            result, np.ascontiguousarray(costs),
            np.ascontiguousarray(ambiguous), smoothness,
        ))
        self.assertEqual(result.dtype, labels.dtype)
        self.assertEqual(result.shape, labels.shape)
        self.assertEqual(count, expected_count)
        self.assertTrue(np.array_equal(result, expected_result))

    def test_single_pass_random_fields(self):
        for i, (height, width) in enumerate(
            [(3, 4), (5, 7), (1, 1), (1, 40), (40, 1), (33, 61), (64, 64)]
        ):
            for palette in (2, 3, 5):
                with self.subTest(size=(height, width), palette=palette):
                    labels = _random_labels(height, width, palette, 10 + i)
                    costs = _random_costs(height, width, palette, 100 + i)
                    rng = np.random.default_rng(200 + i)
                    ambiguous = rng.random((height, width)) < 0.6
                    for smoothness in (2.5, 3.0, 1.0):
                        self._assert_pass_identical(labels, costs, ambiguous, smoothness)

    def test_single_pass_structured_band(self):
        height, width, palette = 24, 40, 3
        labels = _dither_labels(height, width, palette, 7)
        costs = _random_costs(height, width, palette, 8)
        ordered = np.partition(costs, 1, axis=2)
        margin = ordered[..., 1] - ordered[..., 0]
        ambiguous = margin < 7.0
        self._assert_pass_identical(labels, costs, ambiguous, 2.5)

    def test_full_function_matches_reference(self):
        for i, (height, width, palette) in enumerate(
            [(9, 15, 2), (30, 45, 4), (17, 17, 5), (1, 50, 3)]
        ):
            with self.subTest(size=(height, width), palette=palette):
                labels = _dither_labels(height, width, palette, 300 + i)
                costs = _random_costs(height, width, palette, 400 + i)
                rng = np.random.default_rng(500 + i)
                protected = rng.random((height, width)) < 0.1
                expected, expected_audit = _reference_resolve_full(
                    labels, costs, protected, smoothness=3.0
                )
                result, audit = resolve_ambiguous_boundaries(
                    labels, costs, protected, smoothness=3.0
                )
                self.assertTrue(np.array_equal(result, expected))
                self.assertEqual(audit, expected_audit)

    def test_concurrent_thread_pool_callers_match_reference(self):
        cases = []
        for i in range(6):
            labels = _dither_labels(28 + i, 33 + i, 4, 600 + i)
            costs = _random_costs(28 + i, 33 + i, 4, 700 + i)
            rng = np.random.default_rng(800 + i)
            protected = rng.random((28 + i, 33 + i)) < 0.1
            cases.append((labels, costs, protected))
        expected = [_reference_resolve_full(lbl, cst, prt, smoothness=2.5)
                    for lbl, cst, prt in cases]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda case: resolve_ambiguous_boundaries(*case, smoothness=2.5),
                cases,
            ))
        for (result, audit), (want, want_audit) in zip(results, expected):
            self.assertTrue(np.array_equal(result, want))
            self.assertEqual(audit, want_audit)


class DitherCollapseKernelBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, labels, despeckled, count, radius, protected):
        expected, expected_audit = _reference_collapse(
            labels, despeckled, count, radius, protected
        )
        result, audit = collapse_dither_speckle(
            labels, despeckled, count, radius, protected
        )
        self.assertEqual(result.dtype, labels.dtype)
        self.assertEqual(result.shape, labels.shape)
        self.assertTrue(np.array_equal(result, expected))
        self.assertEqual(audit, expected_audit)

    def test_dither_fields_various_radii(self):
        for i, (height, width) in enumerate(
            [(7, 7), (1, 1), (1, 40), (40, 1), (25, 31), (48, 48)]
        ):
            for palette in (2, 3, 4):
                labels = _dither_labels(height, width, palette, 20 + i)
                count = int(labels.max()) + 2  # exercise an unused trailing colour
                rng = np.random.default_rng(900 + i)
                despeckled = rng.integers(0, palette, size=(height, width)).astype(np.int32)
                protected = rng.random((height, width)) < 0.1
                for radius in (1, 2, 3):
                    with self.subTest(size=(height, width), palette=palette, radius=radius):
                        self._assert_identical(labels, despeckled, count, radius, protected)

    def test_random_fields(self):
        for i in range(6):
            height, width, palette = 20 + i, 27 + i, 5
            labels = _random_labels(height, width, palette, 1000 + i)
            despeckled = _random_labels(height, width, palette, 1100 + i)
            rng = np.random.default_rng(1200 + i)
            protected = rng.random((height, width)) < 0.1
            with self.subTest(case=i):
                self._assert_identical(labels, despeckled, palette, 2, protected)

    def test_radius_zero_noop(self):
        labels = _dither_labels(9, 9, 3, 5)
        result, audit = collapse_dither_speckle(
            labels, np.zeros_like(labels), 3, 0, np.zeros_like(labels, dtype=bool)
        )
        self.assertTrue(result is labels)
        self.assertEqual(audit, {"radius": 0, "changed_assignments": 0})

    def test_concurrent_thread_pool_callers_match_reference(self):
        cases = []
        for i in range(6):
            labels = _dither_labels(30 + i, 34 + i, 4, 1300 + i)
            despeckled = _random_labels(30 + i, 34 + i, 4, 1400 + i)
            rng = np.random.default_rng(1500 + i)
            protected = rng.random((30 + i, 34 + i)) < 0.1
            cases.append((labels, despeckled, 4, 2, protected))
        expected = [_reference_collapse(*case) for case in cases]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda case: collapse_dither_speckle(*case), cases
            ))
        for (result, audit), (want, want_audit) in zip(results, expected):
            self.assertTrue(np.array_equal(result, want))
            self.assertEqual(audit, want_audit)


if __name__ == "__main__":
    unittest.main()
