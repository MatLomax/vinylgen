"""Real-image cutter-cleanup checks, opt-in via ``scripts/run_tests.py --full-image``.

Reuses ``_load_reference`` and ``_without_protection_counts`` from
``test_cutter_cleanup_fast`` (kept there because the fast fuzz test still uses
them) rather than duplicating them.
"""

import json
import unittest
from pathlib import Path

import numpy as np

from vinylgen.contracts import CleanupSettings, SegmentationBundle
from vinylgen.cutter_cleanup import cleanup_labels

from test_cutter_cleanup_fast import _load_reference, _without_protection_counts

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OVERLAP_ROOT = _REPO_ROOT / ".tmp" / "trace-baseline" / "before" / "overlap"
_SOURCEUP = _REPO_ROOT / "outputs" / "sourceup"

_CORPUS_SETTINGS = dict(width_mm=150.0, cleanup=True,
                        cleanup_action="remove_then_widen", min_detail_width=0.6,
                        min_region_area=1.5, remove_below_width=0.3)


def _compare(test, reference, labels, included, palette, settings, upp):
    want_labels, want_summary = reference.cleanup_labels(
        labels.copy(), included, palette, settings, upp)
    got_labels, got_summary = cleanup_labels(
        labels.copy(), included, palette, settings, upp)
    test.assertEqual(got_labels.dtype, want_labels.dtype)
    test.assertTrue(np.array_equal(got_labels, want_labels), "labels differ")
    test.assertEqual(_without_protection_counts(got_summary), want_summary)


class CutterCleanupCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = _load_reference()

    def test_corpus_subjects_match_reference(self):
        subjects = ("desert-rocket", "kraken-lighthouse", "cosmic-jellyfish")
        checked = 0
        for subject in subjects:
            directory = _OVERLAP_ROOT / subject
            npz = directory / "segmentation.npz"
            palette_path = directory / "palette.json"
            if not npz.exists() or not palette_path.exists():
                continue
            bundle = SegmentationBundle.load(npz)
            palette = json.loads(palette_path.read_text())["palette"]
            included = list(range(len(palette)))
            height, width = bundle.labels.shape
            settings = CleanupSettings(**_CORPUS_SETTINGS)
            upp = settings.width_mm / width
            with self.subTest(subject=subject):
                _compare(self, self.reference, bundle.labels, included, palette,
                         settings, upp)
            checked += 1
        if not checked:
            raise unittest.SkipTest(f"overlap baseline absent at {_OVERLAP_ROOT}")

    def test_sourceup_matches_reference(self):
        npz = _SOURCEUP / "segmentation.npz"
        palette_path = _SOURCEUP / "palette.json"
        if not npz.exists() or not palette_path.exists():
            raise unittest.SkipTest(f"sourceup bundle absent at {_SOURCEUP}")
        bundle = SegmentationBundle.load(npz)
        palette = json.loads(palette_path.read_text())["palette"]
        included = list(range(len(palette)))
        height, width = bundle.labels.shape
        settings = CleanupSettings(**_CORPUS_SETTINGS)
        upp = settings.width_mm / width
        _compare(self, self.reference, bundle.labels, included, palette, settings, upp)


if __name__ == "__main__":
    unittest.main()
