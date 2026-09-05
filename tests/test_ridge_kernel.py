import math
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from vinylgen.colour import srgb_to_lab
from vinylgen.ridge import two_sided_dark_ridge_response


def _reference_response(lightness: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Verbatim scipy order-1 map_coordinates ridge-response formulation.

    Fixes the exact response and best-radius arrays the fused Numba kernel must
    reproduce bit-for-bit.
    """
    lightness = lightness.astype(np.float32)
    yy, xx = np.indices(lightness.shape, dtype=np.float32)
    response = np.zeros(lightness.shape, dtype=np.float32)
    best_radius = np.ones(lightness.shape, dtype=np.uint8)
    for radius in (1, 2, 3):
        for angle in np.linspace(0.0, np.pi, 8, endpoint=False):
            dx = radius * math.cos(float(angle))
            dy = radius * math.sin(float(angle))
            a = ndimage.map_coordinates(lightness, (yy + dy, xx + dx), order=1,
                                        mode="nearest")
            b = ndimage.map_coordinates(lightness, (yy - dy, xx - dx), order=1,
                                        mode="nearest")
            two_sided = np.minimum(a - lightness, b - lightness)
            improve = two_sided > response
            response[improve] = two_sided[improve]
            best_radius[improve] = radius
    return response, best_radius


def _structured_lines(height: int, width: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((height, width), 80.0, dtype=np.float32)
    yy, xx = np.indices((height, width))
    for _ in range(6):
        angle = rng.uniform(0, np.pi)
        thickness = int(rng.integers(1, 4))
        projection = xx * math.cos(angle) + yy * math.sin(angle)
        offset = rng.uniform(-min(height, width), min(height, width))
        image[np.abs(projection - (projection.mean() + offset)) <= thickness] = \
            rng.uniform(0, 30)
    return image.astype(np.float32)


class RidgeKernelBitExactnessTest(unittest.TestCase):
    def _assert_identical(self, lightness: np.ndarray) -> None:
        expected_response, expected_radius = _reference_response(lightness)
        response, best_radius = two_sided_dark_ridge_response(lightness)
        self.assertEqual(response.dtype, np.float32)
        self.assertEqual(best_radius.dtype, np.uint8)
        self.assertEqual(response.shape, lightness.shape)
        self.assertTrue(np.array_equal(response, expected_response))
        self.assertTrue(np.array_equal(best_radius, expected_radius))

    def test_random_float32_images(self) -> None:
        rng = np.random.default_rng(20240501)
        for height, width in [(3, 4), (5, 7), (1, 1), (64, 97), (201, 33),
                              (128, 128), (100, 1), (1, 100), (50, 50)]:
            with self.subTest(size=(height, width)):
                image = rng.uniform(0.0, 100.0, size=(height, width)).astype(np.float32)
                self._assert_identical(image)

    def test_structured_line_image(self) -> None:
        self._assert_identical(_structured_lines(160, 120, 7))

    def test_real_desert_rocket_l_channel(self) -> None:
        path = Path("images/desert-rocket.png")
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        height, width, _ = rgb.shape
        lab = srgb_to_lab(rgb.reshape(-1, 3))
        lightness = lab[:, 0].reshape(height, width).astype(np.float32)
        self._assert_identical(lightness)

    def test_concurrent_thread_pool_callers_match_reference(self) -> None:
        """The palette CLI flattens independent images on a thread pool, so the
        kernel is entered from several Python threads at once."""
        rng = np.random.default_rng(99)
        images = [rng.uniform(0.0, 100.0, size=(90 + i, 70 + i)).astype(np.float32)
                  for i in range(6)]
        expected = [_reference_response(image) for image in images]
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(two_sided_dark_ridge_response, images))
        for (response, radius), (want_response, want_radius) in zip(results, expected):
            self.assertTrue(np.array_equal(response, want_response))
            self.assertTrue(np.array_equal(radius, want_radius))


if __name__ == "__main__":
    unittest.main()
