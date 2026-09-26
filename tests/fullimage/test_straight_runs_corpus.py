"""Corpus straight-run bound check, opt-in via ``scripts/run_tests.py --full-image``.

No emitted piece on the corpus has a sample beyond its bound, at the default
thousandths and under stage-3 recovery (tightened lengths plus a fit constraint).
Reuses ``run_violations`` from ``test_straight_runs`` (kept there because the
synthetic tests still use it) and ``CORPUS`` from ``test_smooth_joins_corpus``
rather than duplicating them.
"""

import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import HardAnchor, SharedChainFitter, _tightened
from vinylgen.trace_lengths import resolve_trace_lengths

from test_smooth_joins_corpus import CORPUS
from test_straight_runs import run_violations

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _corpus_segmentation(subject: str) -> Path | None:
    return next((candidate for candidate in (
        _REPO_ROOT / "outputs" / subject / "segmentation.npz",
        _REPO_ROOT / ".tmp" / "stroke-baseline" / "after" / subject / "segmentation.npz")
        if candidate.exists()), None)


class CorpusBoundTests(unittest.TestCase):
    """No emitted piece on the corpus has a sample beyond its bound, at the default
    thousandths and under stage-3 recovery (tightened lengths plus a fit constraint)."""

    def test_no_sample_is_beyond_its_piece_bound_on_the_corpus(self):
        from vinylgen.boundary_graph import shared_boundary_polygons

        checked = 0
        for subject in CORPUS:
            path = _corpus_segmentation(subject)
            if path is None:
                continue
            with np.load(path, allow_pickle=False) as data:
                labels = data["labels"].astype(np.int16)
                costs = data["palette_costs"].astype(np.float32)
                protected = data["protected_features"].astype(bool)
            included = sorted(int(value) for value in np.unique(labels))
            _, track, _ = shared_boundary_polygons(labels, included, costs,
                                                   protected_features=protected)
            settings = resolve_trace_lengths(TraceSettings(), max(labels.shape))
            tightened = replace(
                settings, curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 4.0),
                straight_line_max_deviation=_tightened(settings.straight_line_max_deviation, 2.0),
                side_smoothness=_tightened(settings.side_smoothness, 2.0))
            fitter = SharedChainFitter()
            failures = []
            for number, chain in enumerate(track.chains):
                anchors = [HardAnchor(anchor.point, anchor.kind) for anchor in chain.anchors]
                points = tuple(tuple(map(float, point)) for point in chain.points)
                canonical = points if points <= tuple(reversed(points)) else tuple(reversed(points))
                cases = [(settings, ())]
                if number % 7 == 0:
                    cases.append((tightened, (canonical[len(canonical) // 2],)))
                for chain_settings, constraints in cases:
                    fit = fitter.fit(canonical, chain_settings, anchors, constraints)
                    violations = run_violations(fit, canonical, chain_settings)
                    checked += 1
                    if violations:
                        failures.append((chain.identity, max(violations, key=lambda row: row[1] - row[2])))
            self.assertEqual(failures, [], f"{subject}: {len(failures)} chains beyond their bound")
        if not checked:
            self.skipTest("no corpus segmentation found")


if __name__ == "__main__":
    unittest.main()
