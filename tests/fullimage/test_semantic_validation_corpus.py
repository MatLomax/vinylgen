"""Real-image semantic-validation checks, opt-in via ``scripts/run_tests.py --full-image``."""

import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.validation import source_thin_feature_mask, validate_feature_roi

_REPO_ROOT = Path(__file__).resolve().parents[2]


class GriffinHeadOutlineCorpusTests(unittest.TestCase):
    def test_griffin_head_outline_is_source_feature_evidence(self):
        source_path = _REPO_ROOT / "images" / "griffin-castle.png"
        if not source_path.exists():
            self.skipTest("Griffin corpus source is unavailable")
        source = np.asarray(Image.open(source_path).convert("RGB"))
        features = source_thin_feature_mask(source)
        height, width = features.shape
        # Stable normalized box around the dark crown/forehead outline.  This
        # asserts source evidence, not a generated output or giant golden file.
        roi = features[
            round(height * 0.065):round(height * 0.145),
            round(width * 0.405):round(width * 0.495),
        ]

        self.assertGreaterEqual(int(roi.sum()), 45)

    def test_griffin_head_roi_accepts_preserved_outline_and_rejects_omission(self):
        source_path = _REPO_ROOT / "images" / "griffin-castle.png"
        if not source_path.exists():
            self.skipTest("Griffin corpus source is unavailable")
        source = np.asarray(Image.open(source_path).convert("RGB"))
        bounds = (0.405, 0.065, 0.495, 0.145)
        omitted = source.copy()
        height, width = source.shape[:2]
        x0, x1 = round(bounds[0] * width), round(bounds[2] * width)
        y0, y1 = round(bounds[1] * height), round(bounds[3] * height)
        omitted[y0:y1, x0:x1] = source[y0, x0]

        preserved_audit = validate_feature_roi(source, source, bounds)
        omitted_audit = validate_feature_roi(source, omitted, bounds)

        self.assertTrue(preserved_audit["passed"])
        self.assertFalse(omitted_audit["passed"])


if __name__ == "__main__":
    unittest.main()
