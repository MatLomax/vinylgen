"""Tiny degenerate rings: node placement terminates and the fit stays within tolerance.

Rings narrower than the tolerance (hairline slivers about 0.1 px wide, loops a
few pixels round whose samples double back, zero-area out-and-back rings,
duplicate consecutive samples) once made ``_place_nodes_nb`` loop forever: the
route search's turn prune skipped the very pieces the fallback split had just
accepted. Each case runs in a child process under a timeout, so a hang fails
the test instead of stalling the suite.
"""

import json
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
# A generous bound: the cases take a few seconds warm; a cold Numba cache
# compiles the fitter first. A hang never finishes.
TIMEOUT = 300

# Exact `_fit_section_records` calls that hung, from the fox-table project
# (4096 px, pixel-era lengths scaled by 2.048) and the stroke-baseline corpus
# (pixel era, max_deviation 2): (ring, context, settings overrides). The fitted
# points are `_cyclic_extension(ring, context)` over samples context..context+len(ring).
REPROS = {
    "fox-table-hairline": (
        [(573.55, 623.0), (574.0, 624.0), (574.45, 625.0), (574.45, 626.0), (574.45, 627.0),
         (574.45, 627.55), (574.55, 627.55), (574.55, 627.0), (574.55, 626.0),
         (574.55, 625.0), (574.55, 624.0), (574.55, 623.45)],
        12,
        dict(curve_fit_tolerance=3.072, straight_line_max_deviation=3.2768,
             minimum_straight_length=8.192, max_deviation=4.096, corner_analysis_span=16.384,
             side_smoothness=3.2768, corner_sharpening_allowance=4.096,
             curve_feature_scale=8.192)),
    "corpus-sliver": (
        [(494.0, 952.0), (494.45, 952.55), (495.0, 952.55), (495.55, 952.55),
         (495.55, 952.45), (495.0, 952.45)],
        8,
        dict(max_deviation=2.0, corner_sharpening_allowance=2.0)),
}


def _synthetic_rings():
    """``(name, samples)`` of small degenerate rings (open lists; the fit closes them)."""
    rings = []
    for length in (1, 3, 8):
        for width in (0.0, 0.1):
            for degrees in (0, 45, 133):
                angle = math.radians(degrees)
                dx, dy = math.cos(angle), math.sin(angle)
                out = [(100 + dx * s, 100 + dy * s) for s in range(length + 1)]
                back = [(x - dy * width, y + dx * width) for x, y in reversed(out)]
                # Zero width: the way back retraces the way out, so the far
                # end is a repeated sample and the start closes the ring.
                rings.append((f"fold-back sliver {length} px, {width} wide, {degrees} deg",
                              out + (back[:-1] if width == 0.0 else back)))
    rings.append(("figure-8 loop", [(564.55, 653.0), (564.55, 652.45), (564.0, 652.0),
                                    (564.45, 653.0), (564.45, 653.55), (564.55, 653.55)]))
    rng = np.random.default_rng(11)
    for count in range(3, 10):
        for trial in range(6):
            # On the boundary graph's 0.05 px grid, inside a 3 px box.
            samples = np.round(rng.uniform(0.0, 3.0, (count, 2)) / 0.05) * 0.05 + 200.0
            rings.append((f"{count}-sample loop #{trial}", [tuple(p) for p in samples]))
    for count in (2, 3, 4, 5):
        out = [(300.0 + i, 300.0 + 0.5 * i) for i in range(count)]
        rings.append((f"zero-area ring of {count}", out + out[-2:0:-1]))
    for trial in range(12):
        count = int(rng.integers(3, 9))
        angles = np.sort(rng.uniform(0.0, 2 * math.pi, count))
        radius = float(rng.uniform(0.5, 3.0))
        samples = []
        for angle in angles:
            point = (400 + radius * math.cos(angle), 400 + radius * math.sin(angle))
            samples += [point, point] if rng.random() < 0.5 else [point]
        rings.append((f"duplicate samples #{trial}", samples))
    rings.append(("one repeated point", [(5.0, 5.0)] * 5))
    return rings


def _fit_repros() -> list[dict]:
    """Fit every repro section; runs in the child process."""
    from vinylgen import curve_fit as cf
    from vinylgen.contracts import TraceSettings
    from test_smooth_joins import max_sample_distance

    results = []
    for name, (ring, context, overrides) in REPROS.items():
        settings = TraceSettings(length_unit="px", **overrides)
        points = cf._cyclic_extension(np.asarray(ring, dtype=np.float64), context)
        first, last = context, context + len(ring)
        records = cf._fit_section_records(points, first, last, 0, len(points) - 1, settings, True)
        start = tuple(map(float, points[first]))
        fit = cf.ChainFit(start, tuple(cf._fit_records(records)), cf.FitMetrics(0, 0, 0, 0, 0))
        # Each piece against its own samples only: on a sliver the opposite
        # side of the ring would otherwise cover a sample the piece missed.
        error, piece_start, piece_point = 0.0, first, start
        for primitive, row in zip(fit.primitives, records):
            piece_end = int(row[10])
            piece = cf.ChainFit(piece_point, (primitive,), cf.FitMetrics(0, 0, 0, 0, 0))
            error = max(error, max_sample_distance(piece, points[piece_start:piece_end + 1]))
            piece_start, piece_point = piece_end, primitive[-1]
        results.append({"name": name, "error": error, "bound": cf._curve_tolerance(settings),
                        "closed": fit.primitives[-1][-1] == start,
                        "ends_at_last": int(records[-1, 10]) == last})
    return results


def _fit_synthetic() -> list[dict]:
    """Fit every synthetic ring in every placement mode; runs in the child process."""
    from vinylgen import curve_fit as cf
    from vinylgen.contracts import TraceSettings
    from vinylgen.curve_fit import SharedChainFitter
    from test_smooth_joins import closed_ring, max_sample_distance

    results = []
    for name, samples in _synthetic_rings():
        ring = closed_ring(samples)
        for placement in cf.PLACEMENT_MODES:
            for tolerance, deviation in ((1.5, 2.0), (0.4, None)):
                settings = TraceSettings(length_unit="px", node_placement=placement,
                                         curve_fit_tolerance=tolerance, max_deviation=deviation)
                fit = SharedChainFitter().fit(ring, settings)
                # A sharpened corner may lie the sharpening allowance off its samples.
                bound = max(cf._curve_tolerance(settings), cf._sharpening_allowance(settings))
                results.append({"name": f"{name}, {placement}, tolerance {tolerance}",
                                "error": max_sample_distance(fit, ring), "bound": bound,
                                "closed": bool(fit.primitives) and fit.primitives[-1][-1] == fit.start})
    return results


def _in_child(function: str) -> list[dict]:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(ROOT), str(TESTS))))
    completed = subprocess.run(
        [sys.executable, "-c",
         f"import json, test_degenerate_rings as t; print(json.dumps(t.{function}()))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=TIMEOUT, check=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


class DegenerateRingTests(unittest.TestCase):
    def assert_fits(self, results):
        self.assertTrue(results)
        for result in results:
            with self.subTest(result["name"]):
                self.assertLessEqual(result["error"], result["bound"] + 1e-9)
                self.assertTrue(result["closed"])
                if "ends_at_last" in result:
                    self.assertTrue(result["ends_at_last"])

    def test_the_rings_that_hung_are_fitted_within_tolerance(self):
        self.assert_fits(_in_child("_fit_repros"))

    def test_small_folded_zero_area_and_duplicate_sample_rings_fit_within_tolerance(self):
        self.assert_fits(_in_child("_fit_synthetic"))


class FoldedRunTests(unittest.TestCase):
    def test_a_line_never_covers_samples_beyond_its_chord(self):
        from vinylgen import curve_fit as cf

        # Out 4 px along x and back 3: every sample is on the chord's line.
        run = np.asarray([(0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (2.0, 0.0), (1.0, 0.0)])
        distance, index = cf._distance_to_line_nb(run, 0, len(run) - 1)
        self.assertAlmostEqual(distance, 3.0)
        self.assertEqual(index, 2)

    def test_a_ring_is_never_one_straight_run(self):
        from vinylgen import curve_fit as cf
        from vinylgen.contracts import TraceSettings

        settings = TraceSettings(length_unit="px")
        out = [(float(x), 0.0) for x in range(9)]
        straight, _ = cf._section_line(np.asarray(out + out[-2::-1]), settings)
        self.assertFalse(straight)
        self.assertTrue(cf._section_line(np.asarray(out), settings)[0])


if __name__ == "__main__":
    unittest.main()
