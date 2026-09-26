"""Real-image boundary-graph checks, opt-in via ``scripts/run_tests.py --full-image``.

Reuses the ``_compare`` helper from ``test_boundary_graph_fast`` (kept there
because the fast fuzz tests still use it) rather than duplicating it.
"""

import unittest
from pathlib import Path

from vinylgen.contracts import SegmentationBundle

from test_boundary_graph_fast import _compare

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORPUS_ROOT = _REPO_ROOT / ".tmp" / "stroke-baseline" / "after"
_SOURCEUP_NPZ = _REPO_ROOT / "outputs" / "sourceup" / "segmentation.npz"

_CORPUS_SUBJECTS = (
    "cosmic-jellyfish", "crashed-ring", "crashed-spacecraft", "desert-mech",
    "desert-rocket", "floating-castle", "griffin-castle", "kraken-lighthouse",
    "sky-whale-city",
)


class BoundaryGraphCorpusTests(unittest.TestCase):
    def test_corpus_subjects_match_reference(self):
        if not _CORPUS_ROOT.is_dir():
            raise unittest.SkipTest(f"corpus baseline absent at {_CORPUS_ROOT}")
        checked = 0
        for subject in _CORPUS_SUBJECTS:
            npz = _CORPUS_ROOT / subject / "segmentation.npz"
            if not npz.exists():
                continue
            bundle = SegmentationBundle.load(npz)
            included = list(range(bundle.palette_rgb.shape[0]))
            with self.subTest(subject=subject):
                _compare(self, bundle.labels, included, bundle.palette_costs,
                         bundle.protected_features)
            checked += 1
        if not checked:
            raise unittest.SkipTest("no corpus segmentation bundles present")

    def test_sourceup_matches_reference(self):
        if not _SOURCEUP_NPZ.exists():
            raise unittest.SkipTest(f"sourceup bundle absent at {_SOURCEUP_NPZ}")
        bundle = SegmentationBundle.load(_SOURCEUP_NPZ)
        included = list(range(bundle.palette_rgb.shape[0]))
        _compare(self, bundle.labels, included, bundle.palette_costs,
                 bundle.protected_features)


if __name__ == "__main__":
    unittest.main()
