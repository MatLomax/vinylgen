"""The whole icon corpus (``vinylgen.fidelity.icons.icon_cases``), opt-in: every
case builds (import, truth, minimum feature) and derives its labels-tier bounds,
and ``icon_cases`` is deterministic. The default run checks a slice
(``tests/test_fidelity_icons.py``)."""

from __future__ import annotations

import json
import unittest

from vinylgen.fidelity import bounds, icons
from vinylgen.fidelity.truth import scene_truth


class IconCorpusTest(unittest.TestCase):
    def test_every_case_builds_truth_and_labels_bounds(self):
        cases = icons.icon_cases()
        self.assertEqual(len(cases), len(icons.glyph_files()))
        self.assertEqual(len({case.case_id for case in cases}), len(cases))
        for case in cases:
            with self.subTest(case=case.case_id):
                self.assertGreater(case.min_feature_px, 0.0)
                truth = scene_truth(case.scene)
                self.assertGreater(len(truth.corners), 0)
                self.assertIsNotNone(bounds.case_bounds(case, "labels", truth))
        icons._cached_cases.cache_clear()
        again = icons.icon_cases()
        self.assertEqual([json.dumps([c.case_id, c.min_feature_px, c.scene.to_json()])
                          for c in cases],
                         [json.dumps([c.case_id, c.min_feature_px, c.scene.to_json()])
                          for c in again])


if __name__ == "__main__":
    unittest.main()
