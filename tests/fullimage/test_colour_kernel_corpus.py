"""Real-image colour-kernel checks, opt-in via ``scripts/run_tests.py --full-image``.

Reuses the bit-exactness helpers from ``test_colour_kernel`` (kept there because
the fast synthetic tests still use them) rather than duplicating them.
"""

import json
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

# Imported as a module, not by class name: a `from ... import SomeTestCase`
# here would make unittest discovery collect that TestCase a second time under
# this module too, re-running its fast synthetic tests in the full-image bucket.
import test_colour_kernel

_REPO_ROOT = Path(__file__).resolve().parents[2]


class SrgbToLabKernelCorpusTest(unittest.TestCase):
    def test_real_desert_rocket_image(self) -> None:
        path = _REPO_ROOT / "images" / "desert-rocket.png"
        if not path.exists():
            self.skipTest(f"corpus image absent at {path}")
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        test_colour_kernel.SrgbToLabKernelBitExactnessTest._assert_identical(
            self, rgb.reshape(-1, 3))


class NearestPaletteLabelsKernelCorpusTest(unittest.TestCase):
    def test_real_flattened_image(self) -> None:
        flattened = _REPO_ROOT / "outputs" / "desert-rocket" / "flattened.png"
        palette_path = _REPO_ROOT / "outputs" / "desert-rocket" / "palette.json"
        if not flattened.exists() or not palette_path.exists():
            self.skipTest(f"desert-rocket outputs absent under {_REPO_ROOT / 'outputs'}")
        image = np.asarray(Image.open(flattened).convert("RGB"))
        report = json.loads(palette_path.read_text())
        palette = np.asarray([row["rgb"] for row in report["palette"]], dtype=np.int32)
        test_colour_kernel.NearestPaletteLabelsKernelBitExactnessTest._assert_identical(
            self, image, palette)


if __name__ == "__main__":
    unittest.main()
