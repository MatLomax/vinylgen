"""Bit-identity tests for the accelerated trace-stage validation paths.

Every assertion compares the live :mod:`vinylgen.validation` against a frozen
reference copy loaded from ``tests/oracles/validation_reference.py``. The
reference is the oracle; the fast paths must reproduce its returned values
exactly (arrays bit-for-bit, dicts structurally, floats to the bit including
NaN, infinity and the sign of zero).
"""

from __future__ import annotations

import importlib.util
import math
import os
import struct
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen import validation as fast


_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_PATH = Path(__file__).resolve().parent / "oracles" / "validation_reference.py"
_SOURCEUP_PATH = _REPO_ROOT / "outputs" / "sourceup" / "source.png"
_CORPUS_ROOT = _REPO_ROOT / ".tmp" / "stroke-baseline" / "after"
_CORPUS_SUBJECTS = (
    "cosmic-jellyfish", "crashed-ring", "crashed-spacecraft", "desert-mech",
    "desert-rocket", "floating-castle", "griffin-castle", "kraken-lighthouse",
    "sky-whale-city",
)


def _load_reference():
    spec = importlib.util.spec_from_file_location(
        "validation_reference", _REFERENCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reference = _load_reference()


def _skip_slow() -> bool:
    """Real-image oracle checks take minutes; they run only when asked for."""
    return os.environ.get("VINYLGEN_SLOW_TESTS") != "1"


def _corpus_source(subject: str) -> np.ndarray:
    path = _CORPUS_ROOT / subject / "source.png"
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def _require_corpus() -> None:
    if _skip_slow():
        raise unittest.SkipTest("set VINYLGEN_SLOW_TESTS=1 to run real-image oracle checks")
    if not _CORPUS_ROOT.is_dir():
        raise unittest.SkipTest(f"corpus directory missing: {_CORPUS_ROOT}")


def _float_bits(value: float) -> bytes:
    return struct.pack("<d", float(value))


def _assert_deep_bit_equal(test: unittest.TestCase, expected, actual,
                           path: str = "") -> None:
    """Assert two JSON-like structures are equal to the bit for every float."""
    if isinstance(expected, dict):
        test.assertIsInstance(actual, dict, path)
        test.assertEqual(set(expected), set(actual), f"keys differ at {path}")
        for key in expected:
            _assert_deep_bit_equal(test, expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, (list, tuple)):
        test.assertEqual(type(expected), type(actual), path)
        test.assertEqual(len(expected), len(actual), f"length differs at {path}")
        for index, (want, got) in enumerate(zip(expected, actual)):
            _assert_deep_bit_equal(test, want, got, f"{path}[{index}]")
    elif isinstance(expected, bool):
        test.assertIs(actual is True, expected is True, path)
        test.assertEqual(expected, actual, path)
    elif isinstance(expected, float):
        test.assertIsInstance(actual, float, path)
        test.assertEqual(_float_bits(expected), _float_bits(actual),
                         f"float bits differ at {path}: {expected!r} vs {actual!r}")
    else:
        test.assertEqual(expected, actual, path)


class LuminanceBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, rgb: np.ndarray) -> None:
        expected = reference._luminance(rgb)
        actual = fast._luminance(rgb)
        self.assertEqual(expected.dtype, actual.dtype)
        self.assertEqual(expected.shape, actual.shape)
        self.assertTrue(np.array_equal(expected, actual))

    def test_all_grey_values(self) -> None:
        greys = np.arange(256, dtype=np.uint8)
        image = np.stack([greys, greys, greys], axis=-1).reshape(16, 16, 3)
        self._assert_identical(image)

    def test_random_uint8_various_shapes(self) -> None:
        rng = np.random.default_rng(20240502)
        for shape in [(1, 1, 3), (2, 9, 3), (17, 3, 3), (33, 64, 3),
                      (1, 257, 3), (257, 1, 3), (100, 100, 3)]:
            with self.subTest(shape=shape):
                self._assert_identical(rng.integers(0, 256, size=shape, dtype=np.uint8))

    def test_non_uint8_input_uses_original_expression(self) -> None:
        rng = np.random.default_rng(11)
        self._assert_identical(rng.uniform(0, 255, size=(20, 24, 3)).astype(np.float64))
        self._assert_identical(rng.uniform(0, 255, size=(8, 8, 3)).astype(np.float32))

    def test_corpus_sources(self) -> None:
        _require_corpus()
        for subject in _CORPUS_SUBJECTS:
            with self.subTest(subject=subject):
                self._assert_identical(_corpus_source(subject))

    @unittest.skipIf(_skip_slow(), "set VINYLGEN_SLOW_TESTS=1 to run real-image oracle checks")
    def test_sourceup(self) -> None:
        if not _SOURCEUP_PATH.exists():
            self.skipTest(f"sourceup source missing: {_SOURCEUP_PATH}")
        rgb = np.asarray(Image.open(_SOURCEUP_PATH).convert("RGB"), dtype=np.uint8)
        self._assert_identical(rgb)


class RidgeStrengthBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, luminance: np.ndarray, maximum_half_width: int) -> None:
        expected = reference._two_sided_dark_ridge_strength(luminance, maximum_half_width)
        actual = fast._two_sided_dark_ridge_strength(luminance, maximum_half_width)
        self.assertEqual(expected.dtype, actual.dtype)
        self.assertEqual(expected.dtype, np.float32)
        self.assertEqual(expected.shape, actual.shape)
        self.assertTrue(np.array_equal(expected, actual))

    def test_random_float32_images(self) -> None:
        rng = np.random.default_rng(20240503)
        shapes = [(1, 1), (1, 7), (7, 1), (1, 200), (200, 1), (3, 4), (5, 7),
                  (13, 9), (64, 97), (200, 300), (2, 2), (128, 128)]
        for shape in shapes:
            for maximum_half_width in (1, 2, 3, 4, 5):
                with self.subTest(shape=shape, maximum_half_width=maximum_half_width):
                    image = rng.uniform(0.0, 1.0, size=shape).astype(np.float32)
                    self._assert_identical(image, maximum_half_width)

    def test_images_with_exact_zero_luminance(self) -> None:
        # Black regions produce exact +0.0 luminance; the fmax/min tie handling
        # must keep the result bit-identical (never a stray -0.0).
        rng = np.random.default_rng(7)
        image = rng.uniform(0.0, 1.0, size=(40, 50)).astype(np.float32)
        image[image < 0.4] = np.float32(0.0)
        for maximum_half_width in (1, 3, 4):
            with self.subTest(maximum_half_width=maximum_half_width):
                self._assert_identical(image, maximum_half_width)

    def test_corpus_luminances(self) -> None:
        _require_corpus()
        for subject in _CORPUS_SUBJECTS:
            with self.subTest(subject=subject):
                luminance = reference._luminance(_corpus_source(subject))
                self._assert_identical(luminance, 4)

    @unittest.skipIf(_skip_slow(), "set VINYLGEN_SLOW_TESTS=1 to run real-image oracle checks")
    def test_sourceup_luminance(self) -> None:
        if not _SOURCEUP_PATH.exists():
            self.skipTest(f"sourceup source missing: {_SOURCEUP_PATH}")
        rgb = np.asarray(Image.open(_SOURCEUP_PATH).convert("RGB"), dtype=np.uint8)
        self._assert_identical(reference._luminance(rgb), 4)

    def test_concurrent_thread_pool_callers_match_reference(self) -> None:
        """The trace CLI validates independent subjects on a thread pool, so the
        kernel is entered from several Python threads at once."""
        rng = np.random.default_rng(99)
        images = [rng.uniform(0.0, 1.0, size=(90 + i, 70 + i)).astype(np.float32)
                  for i in range(6)]
        expected = [reference._two_sided_dark_ridge_strength(image, 4) for image in images]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda image: fast._two_sided_dark_ridge_strength(image, 4), images))
        for got, want in zip(results, expected):
            self.assertTrue(np.array_equal(got, want))


class SourceThinFeatureMaskTest(unittest.TestCase):
    def test_random_uint8_images(self) -> None:
        rng = np.random.default_rng(20240504)
        for shape in [(60, 80, 3), (48, 48, 3), (37, 91, 3)]:
            with self.subTest(shape=shape):
                image = rng.integers(0, 256, size=shape, dtype=np.uint8)
                expected = reference.source_thin_feature_mask(image)
                actual = fast.source_thin_feature_mask(image)
                self.assertEqual(expected.dtype, actual.dtype)
                self.assertTrue(np.array_equal(expected, actual))

    def test_corpus_sources(self) -> None:
        _require_corpus()
        for subject in _CORPUS_SUBJECTS:
            with self.subTest(subject=subject):
                image = _corpus_source(subject)
                self.assertTrue(np.array_equal(
                    reference.source_thin_feature_mask(image),
                    fast.source_thin_feature_mask(image)))


def _protected_fixture():
    """A small labelled scene with two protected components and a candidate."""
    height, width = 48, 60
    protected = np.zeros((height, width), dtype=bool)
    protected[10:20, 8:34] = True
    protected[30:40, 6:14] = True
    candidate = np.zeros((height, width), dtype=bool)
    candidate[11:19, 9:33] = True
    candidate[31:39, 6:13] = True
    reference_labels = np.zeros((height, width), dtype=np.int32)
    reference_labels[protected] = 3
    candidate_labels = np.zeros((height, width), dtype=np.int32)
    candidate_labels[candidate] = 3
    return protected, candidate, reference_labels, candidate_labels


class PostfitProtectedFeaturesTest(unittest.TestCase):
    def test_label_aware_matches_reference_with_and_without_cache(self) -> None:
        protected, candidate, ref_labels, cand_labels = _protected_fixture()
        expected = reference.validate_postfit_protected_features(
            protected, candidate, reference_labels=ref_labels,
            candidate_labels=cand_labels)
        standalone = fast.validate_postfit_protected_features(
            protected, candidate, reference_labels=ref_labels,
            candidate_labels=cand_labels)
        _assert_deep_bit_equal(self, expected, standalone, "standalone")
        cache: dict = {}
        first = fast.validate_postfit_protected_features(
            protected, candidate, reference_labels=ref_labels,
            candidate_labels=cand_labels, invariant_cache=cache)
        second = fast.validate_postfit_protected_features(
            protected, candidate, reference_labels=ref_labels,
            candidate_labels=cand_labels, invariant_cache=cache)
        _assert_deep_bit_equal(self, expected, first, "cache-first")
        _assert_deep_bit_equal(self, expected, second, "cache-second")
        self.assertTrue(any(isinstance(key, tuple) and key[0] == "postfit_invariants"
                            for key in cache))

    def test_cache_reused_across_different_candidates(self) -> None:
        # The invariants depend only on the protected mask and its labels, so a
        # second call with a different candidate but the same reference reuses
        # the cached invariants and still matches the reference exactly.
        protected, candidate, ref_labels, cand_labels = _protected_fixture()
        other = np.zeros_like(candidate)
        other[12:18, 10:30] = True
        other_labels = np.zeros_like(cand_labels)
        other_labels[other] = 3
        cache: dict = {}
        fast.validate_postfit_protected_features(
            protected, candidate, reference_labels=ref_labels,
            candidate_labels=cand_labels, invariant_cache=cache)
        keys_after_first = {k for k in cache}
        expected = reference.validate_postfit_protected_features(
            protected, other, reference_labels=ref_labels,
            candidate_labels=other_labels)
        actual = fast.validate_postfit_protected_features(
            protected, other, reference_labels=ref_labels,
            candidate_labels=other_labels, invariant_cache=cache)
        _assert_deep_bit_equal(self, expected, actual, "reused")
        self.assertEqual(keys_after_first, {k for k in cache})

    def test_label_free_path_matches_reference(self) -> None:
        protected, candidate, _, _ = _protected_fixture()
        expected = reference.validate_postfit_protected_features(protected, candidate)
        actual = fast.validate_postfit_protected_features(protected, candidate)
        _assert_deep_bit_equal(self, expected, actual, "label-free")

    def test_empty_candidate_label_free(self) -> None:
        protected, _, _, _ = _protected_fixture()
        empty = np.zeros_like(protected)
        expected = reference.validate_postfit_protected_features(protected, empty)
        actual = fast.validate_postfit_protected_features(protected, empty)
        _assert_deep_bit_equal(self, expected, actual, "empty-label-free")


def _semantic_fixture():
    height, width = 70, 90
    source = np.full((height, width, 3), 235, dtype=np.uint8)
    source[34:36, 12:78] = 20
    source[20:50, 44:46] = 20
    segmentation = source.copy()
    vector = source.copy()
    protected = np.zeros((height, width), dtype=bool)
    protected[34:36, 12:78] = True
    protected[20:50, 44:46] = True
    segmentation_labels = np.zeros((height, width), dtype=np.int32)
    segmentation_labels[protected] = 4
    vector_labels = segmentation_labels.copy()
    return (source, segmentation, vector, protected,
            segmentation_labels, vector_labels)


class SemanticPreservationTest(unittest.TestCase):
    def test_label_aware_matches_reference(self) -> None:
        (source, segmentation, vector, protected,
         seg_labels, vec_labels) = _semantic_fixture()
        expected = reference.validate_semantic_preservation(
            source, segmentation, vector_rgb=vector, protected_features=protected,
            segmentation_labels=seg_labels, vector_labels=vec_labels)
        actual = fast.validate_semantic_preservation(
            source, segmentation, vector_rgb=vector, protected_features=protected,
            segmentation_labels=seg_labels, vector_labels=vec_labels)
        _assert_deep_bit_equal(self, expected, actual, "semantic")

    def test_repeated_passes_with_shared_cache_match_reference(self) -> None:
        (source, segmentation, vector, protected,
         seg_labels, vec_labels) = _semantic_fixture()
        expected = reference.validate_semantic_preservation(
            source, segmentation, vector_rgb=vector, protected_features=protected,
            segmentation_labels=seg_labels, vector_labels=vec_labels)
        cache: dict = {}
        for pass_index in range(3):
            actual = fast.validate_semantic_preservation(
                source, segmentation, vector_rgb=vector,
                protected_features=protected, segmentation_labels=seg_labels,
                vector_labels=vec_labels, invariant_cache=cache)
            _assert_deep_bit_equal(self, expected, actual, f"pass-{pass_index}")

    def test_no_protection_matches_reference(self) -> None:
        source, segmentation, vector, _, _, _ = _semantic_fixture()
        expected = reference.validate_semantic_preservation(
            source, segmentation, vector_rgb=vector)
        actual = fast.validate_semantic_preservation(
            source, segmentation, vector_rgb=vector)
        _assert_deep_bit_equal(self, expected, actual, "unprotected")


class SemanticWarningMaskTest(unittest.TestCase):
    def test_matches_reference_default(self) -> None:
        source, _, candidate, _, _, _ = _semantic_fixture()
        candidate = candidate.copy()
        candidate[34:36, 40:78] = 235  # erase part of the horizontal stroke
        expected = reference.semantic_warning_mask(source, candidate)
        actual = fast.semantic_warning_mask(source, candidate)
        self.assertEqual(expected.dtype, actual.dtype)
        self.assertTrue(np.array_equal(expected, actual))

    def test_supplied_source_features_match_recomputation(self) -> None:
        source, _, candidate, _, _, _ = _semantic_fixture()
        candidate = candidate.copy()
        candidate[34:36, 40:78] = 235
        expected = reference.semantic_warning_mask(source, candidate)
        precomputed = fast.source_thin_feature_mask(source)
        actual = fast.semantic_warning_mask(
            source, candidate, source_features=precomputed)
        self.assertTrue(np.array_equal(expected, actual))


if __name__ == "__main__":
    unittest.main()
