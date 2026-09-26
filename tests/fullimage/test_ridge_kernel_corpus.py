"""Real-image ridge-kernel check, opt-in via ``scripts/run_tests.py --full-image``.

Reuses the bit-exactness helper from ``test_ridge_kernel`` (kept there because the
fast synthetic tests still use it) rather than duplicating it.
"""

import unittest
from pathlib import Path

import numpy as np
from PIL import Image

# Imported as a module, not by class name: a `from ... import SomeTestCase`
# here would make unittest discovery collect that TestCase a second time under
# this module too, re-running its fast synthetic tests in the full-image bucket.
import test_ridge_kernel
from vinylgen.colour import srgb_to_lab

_REPO_ROOT = Path(__file__).resolve().parents[2]


class RidgeKernelCorpusTest(unittest.TestCase):
    def test_real_desert_rocket_l_channel(self) -> None:
        path = _REPO_ROOT / "images" / "desert-rocket.png"
        if not path.exists():
            self.skipTest(f"corpus image absent at {path}")
        rgb = np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)
        height, width, _ = rgb.shape
        lab = srgb_to_lab(rgb.reshape(-1, 3))
        lightness = lab[:, 0].reshape(height, width).astype(np.float32)
        test_ridge_kernel.RidgeKernelBitExactnessTest._assert_identical(self, lightness)


if __name__ == "__main__":
    unittest.main()
