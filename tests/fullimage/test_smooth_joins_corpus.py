"""Corpus join-shape check, opt-in via ``scripts/run_tests.py --full-image``.

Every node inside a fitted chain is symmetric unless it is a corner, a junction
or a closed chain's anchor. Reuses ``SMOOTH_DEG`` and ``join_turns`` from
``test_smooth_joins`` (kept there because the synthetic tests still use them)
rather than duplicating them.
"""

import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import (
    ChainFit, _chord_monotone_nb, _fmt, _geometric_corners, _tightened,
    shared_boundary_components)
from vinylgen.node_types import classify_join, parse_path

from test_smooth_joins import SMOOTH_DEG, join_turns

_REPO_ROOT = Path(__file__).resolve().parents[2]

CORPUS = ("cosmic-jellyfish", "crashed-ring", "crashed-spacecraft", "desert-mech",
          "desert-rocket", "floating-castle", "griffin-castle", "kraken-lighthouse",
          "sky-whale-city")


def written_types(fit: ChainFit, closed: bool) -> list[str]:
    """The ``sodipodi:nodetypes`` letter of each join of ``fit`` as written, in
    ``join_turns`` order (no junction information: a chain's joins are inside it)."""
    d = "M" + _fmt(fit.start) + " " + " ".join(
        ("L" + _fmt(p[1])) if p[0] == "L" else
        ("C" + _fmt(p[1]) + " " + _fmt(p[2]) + " " + _fmt(p[3])) for p in fit.primitives)
    (start, segments, _), = parse_path(d)
    starts = [start, *(segment[-1] for segment in segments[:-1])]
    types = [classify_join(starts[i], segments[i], segments[i + 1])
             for i in range(len(segments) - 1)]
    if closed and len(segments) > 1:
        types.append(classify_join(starts[-1], segments[-1], segments[0]))
    return types


def looping_cubics(fit: ChainFit) -> list:
    """Cubics of ``fit`` that turn back along their chord (loops or reversals)."""
    found = []
    start = fit.start
    for primitive in fit.primitives:
        if primitive[0] == "C" and not _chord_monotone_nb(*start, *primitive[1], *primitive[2],
                                                          *primitive[3]):
            found.append((start, primitive))
        start = primitive[-1]
    return found


def backtrack(start, primitive) -> float:
    """How far (px) a cubic travels backwards along its own chord."""
    controls = np.array([start, primitive[1], primitive[2], primitive[3]], dtype=float)
    chord = controls[3] - controls[0]
    length = float(np.hypot(*chord))
    t = np.linspace(0.0, 1.0, 801)[:, None]
    curve = ((1 - t) ** 3 * controls[0] + 3 * (1 - t) ** 2 * t * controls[1]
             + 3 * (1 - t) * t ** 2 * controls[2] + t ** 3 * controls[3])
    along = (curve - controls[0]) @ (chord / length)
    return float(np.max(np.maximum.accumulate(along) - along))


def _cusp_nodes(chain, fit: ChainFit) -> set:
    """The nodes allowed to be cusps: the fit's corner nodes (the detector's
    corners, where a straight-line snap may have moved them), junction anchors, and
    a closed chain's anchor (where the loop leaves a vertex and returns: its ends)."""
    return set(fit.corners) | {anchor.point for anchor in chain.anchors
                               if anchor.kind == "junction" or chain.closed}


class CorpusJoinTests(unittest.TestCase):
    """Every node inside a chain fit is symmetric unless it is a corner, a junction
    or a closed chain's anchor."""

    def test_only_corners_and_junctions_are_cusps_on_the_corpus(self):
        from vinylgen.boundary_graph import shared_boundary_polygons

        checked = 0
        for subject in CORPUS:
            path = next((candidate for candidate in (
                _REPO_ROOT / "outputs" / subject / "segmentation.npz",
                _REPO_ROOT / ".tmp" / "stroke-baseline" / "after" / subject / "segmentation.npz")
                if candidate.exists()), None)
            if path is None:
                continue
            with np.load(path, allow_pickle=False) as data:
                labels = data["labels"].astype(np.int16)
                costs = data["palette_costs"].astype(np.float32)
                protected = data["protected_features"].astype(bool)
            included = sorted(int(value) for value in np.unique(labels))
            _, track, _ = shared_boundary_polygons(labels, included, costs,
                                                   protected_features=protected)
            settings = TraceSettings(length_unit="px", max_deviation=2.0)
            # Stage 3 recovery on every seventh chain: tightened tolerances and
            # straightness plus a fit constraint, as protected-feature recovery fits.
            tightened = replace(
                settings, curve_fit_tolerance=_tightened(settings.curve_fit_tolerance, 4.0),
                straight_line_max_deviation=_tightened(settings.straight_line_max_deviation, 2.0),
                side_smoothness=_tightened(settings.side_smoothness, 2.0))
            for recovered in (False, True):
                recovery = ({chain.identity: {"stage": 3,
                                              "constraint": chain.points[len(chain.points) // 2]}
                             for chain in track.chains[::7]} if recovered else {})
                _, fits, _ = shared_boundary_components(track, included, settings, recovery=recovery)
                for chain in track.chains:
                    fit = fits[chain.identity]
                    cusps = _cusp_nodes(chain, fit)
                    # The fit's corner nodes are the detector's corners.
                    state = recovery.get(chain.identity)
                    points = chain.points
                    canonical = points if points <= tuple(reversed(points)) else tuple(reversed(points))
                    detected = _geometric_corners(np.asarray(canonical, dtype=float),
                                                  tightened if state else settings, chain.closed)
                    anchored = {anchor.point for anchor in chain.anchors}
                    self.assertEqual(len(fit.corners),
                                     sum(canonical[index] not in anchored for index in detected))
                    types = written_types(fit, chain.closed)
                    for (node, turn, kinds), written in zip(join_turns(fit, chain.closed), types):
                        if node in cusps:
                            continue
                        checked += 1
                        self.assertLess(
                            turn, SMOOTH_DEG,
                            f"{subject} recovered={recovered}: cusp of {turn:.4f} deg "
                            f"at {node} ({kinds})")
                        # The written coordinates keep it symmetric for Inkscape.
                        self.assertIn(written, "sz", f"{subject}: written {written!r} at {node}")
                    # A cubic of three or more samples never turns back (the fitter
                    # splits it); a two-sample piece next to a node whose tangent
                    # opposes its chord cannot split, and doubles back by a
                    # fraction of a pixel.
                    for start, primitive in looping_cubics(fit):
                        self.assertLess(backtrack(start, primitive), 0.25,
                                        f"{subject}: reversal in {chain.identity}")
        if not checked:
            self.skipTest("no corpus segmentation found")


if __name__ == "__main__":
    unittest.main()
