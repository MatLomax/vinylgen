"""Real-image validation checks, opt-in via ``scripts/run_tests.py --full-image``.

Reuses the live oracle ``reference`` module and the ``_assert_identical`` helpers
from ``test_validation_fast`` (kept there because the fast synthetic tests still
use them) rather than duplicating them.
"""

import unittest
from pathlib import Path

import numpy as np
from PIL import Image

# test_validation_fast itself is imported as a module, not by class name: a
# `from ... import SomeTestCase` here would make unittest discovery collect
# that TestCase a second time under this module too, re-running its fast
# synthetic tests in the full-image bucket. `reference` is a plain module
# object, so importing it by name is safe.
import test_validation_fast
from test_validation_fast import reference
from vinylgen import validation as fast

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SOURCEUP_PATH = _REPO_ROOT / "outputs" / "sourceup" / "source.png"
_CORPUS_ROOT = _REPO_ROOT / ".tmp" / "stroke-baseline" / "after"
_CORPUS_SUBJECTS = (
    "cosmic-jellyfish", "crashed-ring", "crashed-spacecraft", "desert-mech",
    "desert-rocket", "floating-castle", "griffin-castle", "kraken-lighthouse",
    "sky-whale-city",
)


def _corpus_source(subject: str) -> np.ndarray:
    path = _CORPUS_ROOT / subject / "source.png"
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.uint8)


def _require_corpus(test: unittest.TestCase) -> None:
    if not _CORPUS_ROOT.is_dir():
        test.skipTest(f"corpus directory missing: {_CORPUS_ROOT}")


class LuminanceCorpusTest(unittest.TestCase):
    def test_corpus_sources(self) -> None:
        _require_corpus(self)
        for subject in _CORPUS_SUBJECTS:
            with self.subTest(subject=subject):
                test_validation_fast.LuminanceBitExactnessTest._assert_identical(
                    self, _corpus_source(subject))

    def test_sourceup(self) -> None:
        if not _SOURCEUP_PATH.exists():
            self.skipTest(f"sourceup source missing: {_SOURCEUP_PATH}")
        rgb = np.asarray(Image.open(_SOURCEUP_PATH).convert("RGB"), dtype=np.uint8)
        test_validation_fast.LuminanceBitExactnessTest._assert_identical(self, rgb)


class RidgeStrengthCorpusTest(unittest.TestCase):
    def test_corpus_luminances(self) -> None:
        _require_corpus(self)
        for subject in _CORPUS_SUBJECTS:
            with self.subTest(subject=subject):
                luminance = reference._luminance(_corpus_source(subject))
                test_validation_fast.RidgeStrengthBitExactnessTest._assert_identical(
                    self, luminance, 4)

    def test_sourceup_luminance(self) -> None:
        if not _SOURCEUP_PATH.exists():
            self.skipTest(f"sourceup source missing: {_SOURCEUP_PATH}")
        rgb = np.asarray(Image.open(_SOURCEUP_PATH).convert("RGB"), dtype=np.uint8)
        test_validation_fast.RidgeStrengthBitExactnessTest._assert_identical(
            self, reference._luminance(rgb), 4)


class SourceThinFeatureMaskCorpusTest(unittest.TestCase):
    def test_corpus_sources(self) -> None:
        _require_corpus(self)
        for subject in _CORPUS_SUBJECTS:
            with self.subTest(subject=subject):
                image = _corpus_source(subject)
                self.assertTrue(np.array_equal(
                    reference.source_thin_feature_mask(image),
                    fast.source_thin_feature_mask(image)))


if __name__ == "__main__":
    unittest.main()
