import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.colour import nearest_palette_labels, palette_costs, srgb_to_lab


def _reference_srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Verbatim pre-port numpy sRGB->Lab formulation.

    Fixes the exact Lab array the fused Numba kernel must reproduce bit-for-bit.
    """
    x = np.asarray(rgb, dtype=np.float64) / 255.0
    x = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    xyz = x @ np.array([
        [0.4124564, 0.2126729, 0.0193339],
        [0.3575761, 0.7151522, 0.1191920],
        [0.1804375, 0.0721750, 0.9503041],
    ])
    xyz /= np.array([0.95047, 1.0, 1.08883])
    epsilon = 216 / 24389
    kappa = 24389 / 27
    f = np.where(xyz > epsilon, np.cbrt(xyz), (kappa * xyz + 16) / 116)
    return np.column_stack((
        116 * f[:, 1] - 16,
        500 * (f[:, 0] - f[:, 1]),
        200 * (f[:, 1] - f[:, 2]),
    ))


def _reference_palette_costs(
    pixels: np.ndarray, centres: np.ndarray, chunk: int = 200_000
) -> np.ndarray:
    """Verbatim pre-port numpy palette-cost formulation.

    Fixes the exact float32 cost array the fused Numba kernel must reproduce
    bit-for-bit, chunked exactly as the original was.
    """
    costs = np.empty((len(pixels), len(centres)), dtype=np.float32)
    for start in range(0, len(pixels), chunk):
        part = pixels[start:start + chunk]
        costs[start:start + chunk] = np.linalg.norm(
            part[:, None, :] - centres[None, :, :], axis=2
        )
    return costs


class SrgbToLabKernelBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, rgb: np.ndarray) -> None:
        expected = _reference_srgb_to_lab(rgb)
        result = srgb_to_lab(rgb)
        self.assertEqual(result.dtype, np.float64)
        self.assertEqual(result.shape, (len(rgb), 3))
        self.assertTrue(np.array_equal(result, expected))

    def test_full_uint8_domain_sweep(self) -> None:
        # A dense stride across the entire (256**3) uint8 cube exercises both the
        # linear and gamma companding branches and the cube-root/linear fork.
        grid = np.array(np.meshgrid(range(256), range(256), range(256)))
        cube = grid.reshape(3, -1).T.astype(np.uint8)[::29]
        self._assert_identical(cube)

    def test_channel_ramps_and_edges(self) -> None:
        ramp = np.stack([np.arange(256)] * 3, axis=1).astype(np.uint8)
        edges = np.array([[0, 0, 0], [255, 255, 255], [10, 128, 250],
                          [11, 11, 11], [1, 2, 3]], dtype=np.uint8)
        self._assert_identical(ramp)
        self._assert_identical(edges)

    def test_random_uint8_inputs(self) -> None:
        rng = np.random.default_rng(20240607)
        for count in (1, 2, 23, 1000, 90_017):
            with self.subTest(count=count):
                rgb = rng.integers(0, 256, size=(count, 3)).astype(np.uint8)
                self._assert_identical(rgb)

    def test_non_integer_float64_inputs(self) -> None:
        # reduced_histogram feeds non-integer float64 RGB into the transform.
        rng = np.random.default_rng(7)
        for count in (1, 500, 40_000):
            with self.subTest(count=count):
                rgb = rng.uniform(0.0, 255.0, size=(count, 3))
                self._assert_identical(rgb)

    def test_real_desert_rocket_image(self) -> None:
        path = Path("images/desert-rocket.png")
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        self._assert_identical(rgb.reshape(-1, 3))

    def test_concurrent_thread_pool_callers_match_reference(self) -> None:
        rng = np.random.default_rng(101)
        images = [rng.integers(0, 256, size=(5000 + 500 * i, 3)).astype(np.uint8)
                  for i in range(6)]
        expected = [_reference_srgb_to_lab(image) for image in images]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(srgb_to_lab, images))
        for result, want in zip(results, expected):
            self.assertTrue(np.array_equal(result, want))


class PaletteCostsKernelBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, pixels: np.ndarray, centres: np.ndarray) -> None:
        expected = _reference_palette_costs(pixels, centres)
        result = palette_costs(pixels, centres)
        self.assertEqual(result.dtype, np.float32)
        self.assertEqual(result.shape, (len(pixels), len(centres)))
        self.assertTrue(np.array_equal(result, expected))
        # The fused-argmin call sites depend on argmin agreeing bit-for-bit.
        self.assertTrue(np.array_equal(result.argmin(axis=1), expected.argmin(axis=1)))

    def test_random_lab_inputs(self) -> None:
        rng = np.random.default_rng(20240608)
        for count in (1, 2, 1000, 60_000):
            for k in (1, 2, 6, 8):
                with self.subTest(count=count, k=k):
                    pixels = rng.uniform(-20.0, 120.0, size=(count, 3))
                    centres = rng.uniform(-20.0, 120.0, size=(k, 3))
                    self._assert_identical(pixels, centres)

    def test_crosses_reference_chunk_boundary(self) -> None:
        # More than the reference's 200_000-row chunk, so the numpy path chunks
        # while the kernel materialises directly; the arrays must still match.
        rng = np.random.default_rng(3)
        pixels = rng.uniform(0.0, 100.0, size=(450_000, 3))
        centres = rng.uniform(0.0, 100.0, size=(6, 3))
        self._assert_identical(pixels, centres)

    def test_tie_and_zero_distance_cases(self) -> None:
        centres = np.array([[10.0, 20.0, 30.0], [10.0, 20.0, 30.0],
                            [50.0, 5.0, -5.0]])
        pixels = np.array([[10.0, 20.0, 30.0], [30.0, 12.5, 12.5],
                          [0.0, 0.0, 0.0]])
        self._assert_identical(pixels, centres)

    def test_concurrent_thread_pool_callers_match_reference(self) -> None:
        rng = np.random.default_rng(202)
        cases = [(rng.uniform(-10.0, 110.0, size=(4000 + 300 * i, 3)),
                  rng.uniform(-10.0, 110.0, size=(5 + i, 3))) for i in range(6)]
        expected = [_reference_palette_costs(p, c) for p, c in cases]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda case: palette_costs(*case), cases))
        for result, want in zip(results, expected):
            self.assertTrue(np.array_equal(result, want))


def _reference_nearest_palette_labels(image: np.ndarray, palette_rgb: np.ndarray) -> np.ndarray:
    """Verbatim pre-port banded numpy nearest-palette argmin.

    Fixes the exact int16 label field the Numba kernel must reproduce
    bit-for-bit, banded and int32/int64-typed exactly as the trace stage's
    first-pass label seed was.
    """
    palette = np.asarray(palette_rgb, dtype=np.int32)
    height, width = image.shape[:2]
    labels = np.empty((height, width), dtype=np.int16)
    band = max(1, (1 << 24) // max(1, width * len(palette) * 3))
    for start in range(0, height, band):
        rows = image[start:start + band].astype(np.int32)
        distances = np.sum((rows[:, :, None, :] - palette[None, None, :, :]) ** 2,
                           axis=3, dtype=np.int64)
        labels[start:start + band] = distances.argmin(axis=2).astype(np.int16)
    return labels


class NearestPaletteLabelsKernelBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, image: np.ndarray, palette: np.ndarray) -> None:
        expected = _reference_nearest_palette_labels(image, palette)
        result = nearest_palette_labels(image, palette)
        self.assertEqual(result.dtype, np.int16)
        self.assertEqual(result.shape, image.shape[:2])
        self.assertTrue(np.array_equal(result, expected))

    def test_random_images_and_palette_sizes(self) -> None:
        rng = np.random.default_rng(20240609)
        for shape in ((1, 1), (3, 7), (64, 40), (200, 137)):
            for k in (1, 2, 4, 7, 15):
                with self.subTest(shape=shape, k=k):
                    image = rng.integers(0, 256, size=(*shape, 3)).astype(np.uint8)
                    palette = rng.integers(0, 256, size=(k, 3)).astype(np.int32)
                    self._assert_identical(image, palette)

    def test_first_minimum_tie_break_matches_argmin(self) -> None:
        # Duplicate and equidistant palette entries must resolve to the lowest
        # index, exactly as numpy argmin does; a strict `<` in the kernel keeps it.
        palette = np.array([[10, 20, 30], [10, 20, 30], [40, 40, 40],
                            [0, 0, 0], [40, 40, 40]], dtype=np.int32)
        image = np.array([[[10, 20, 30], [40, 40, 40]],
                          [[20, 30, 40], [0, 0, 0]]], dtype=np.uint8)
        self._assert_identical(image, palette)

    def test_crosses_reference_band_boundary(self) -> None:
        # A tall image forces the numpy reference to span several bands while the
        # kernel materialises the whole label field; both must agree.
        rng = np.random.default_rng(11)
        image = rng.integers(0, 256, size=(4096, 2048, 3)).astype(np.uint8)
        palette = rng.integers(0, 256, size=(7, 3)).astype(np.int32)
        self._assert_identical(image, palette)

    def test_real_flattened_image(self) -> None:
        image = np.asarray(Image.open("outputs/desert-rocket/flattened.png").convert("RGB"))
        report = json.loads(Path("outputs/desert-rocket/palette.json").read_text())
        palette = np.asarray([row["rgb"] for row in report["palette"]], dtype=np.int32)
        self._assert_identical(image, palette)

    def test_concurrent_thread_pool_callers_match_reference(self) -> None:
        rng = np.random.default_rng(404)
        cases = [(rng.integers(0, 256, size=(300 + 40 * i, 220 + i, 3)).astype(np.uint8),
                  rng.integers(0, 256, size=(4 + i, 3)).astype(np.int32)) for i in range(6)]
        expected = [_reference_nearest_palette_labels(image, palette)
                    for image, palette in cases]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda case: nearest_palette_labels(*case), cases))
        for result, want in zip(results, expected):
            self.assertTrue(np.array_equal(result, want))


if __name__ == "__main__":
    unittest.main()
