"""Corpus straight-run bound check, opt-in via ``scripts/run_tests.py --full-image``.

No emitted piece on the corpus has a sample beyond its bound, at the default
thousandths and under stage-3 recovery (tightened lengths plus a fit constraint).
The fits are the ones the product writes: ``shared_boundary_components``, crossing
repair included. Reuses ``run_violations`` from ``test_straight_runs`` (kept there because the
synthetic tests still use it) and ``CORPUS`` from ``test_smooth_joins_corpus``
rather than duplicating them.
"""

import unittest
from pathlib import Path

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import _recovery_settings, shared_boundary_components
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
            failures = []
            for recovered in (False, True):
                # Stage 3 recovery on every seventh chain: tightened lengths plus a
                # fit constraint, as protected-feature recovery fits.
                recovery = ({chain.identity: {"stage": 3,
                                              "constraint": chain.points[len(chain.points) // 2]}
                             for chain in track.chains[::7]} if recovered else {})
                _, fits, _ = shared_boundary_components(track, included, settings,
                                                        recovery=recovery)
                for chain in track.chains:
                    if recovered and chain.identity not in recovery:
                        continue
                    points = tuple(tuple(map(float, point)) for point in chain.points)
                    forward = points <= tuple(reversed(points))
                    canonical = points if forward else tuple(reversed(points))
                    # Run diagnostics index the canonical samples.
                    fit = fits[chain.identity] if forward else fits[chain.identity].reversed()
                    chain_settings = _recovery_settings(settings, 3) if recovered else settings
                    violations = run_violations(fit, canonical, chain_settings)
                    checked += 1
                    if violations:
                        failures.append((chain.identity, recovered,
                                         max(violations, key=lambda row: row[1] - row[2])))
            self.assertEqual(failures, [], f"{subject}: {len(failures)} chains beyond their bound")
        if not checked:
            self.skipTest("no corpus segmentation found")


if __name__ == "__main__":
    unittest.main()
