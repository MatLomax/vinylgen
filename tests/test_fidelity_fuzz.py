"""The seeded shape fuzzer of the synthetic fidelity corpus.

Covers determinism (in process and across processes), kind forcing, the committed
pinned cases regenerating byte for byte, validity of every accepted outline
(shapely simplicity and validity over 300 seeds), the truth partition and
colour-share floor of accepted scenes, the rejection and fallback paths, the
palette constraints, and the minimum-feature estimate on shapes of known width and its
invariance to where rings start and which way they run.
"""

import hashlib
import json
import math
import os
import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import shapely
from shapely.geometry import LinearRing, Polygon

from vinylgen.contracts import PaletteSettings
from vinylgen.fidelity import fuzz
from vinylgen.fidelity.fuzz import (FALLBACK_PALETTE, KINDS, MIN_COLOUR_SHARE, case_id,
                                    case_json, estimate_min_feature_px, fuzz_case,
                                    fuzz_scene, palette_violations, pinned_cases)
from vinylgen.fidelity.scene import (FLATTEN_TOLERANCE, Region, Scene, circle,
                                     polygon, rectangle)
from vinylgen.fidelity.truth import scene_truth

ROOT = Path(__file__).resolve().parents[1]
SEEDS = ROOT / "tests" / "synthetic" / "fuzz-seeds.json"
PINNED = ROOT / "tests" / "synthetic" / "fuzz"
SIZES = (256, 512, 1024)
PALETTE = [{"name": "paper", "rgb": [236, 232, 220]}, {"name": "ink", "rgb": [30, 40, 90]}]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DeterminismTest(unittest.TestCase):
    def test_same_seed_gives_identical_json(self):
        for seed in (3, 4, 9):
            with self.subTest(seed=seed):
                self.assertEqual(case_json(fuzz_case(seed, 256)), case_json(fuzz_case(seed, 256)))

    def test_json_is_identical_in_a_fresh_process(self):
        seeds = list(range(8))
        code = ("import hashlib, json, sys\n"
                "from vinylgen.fidelity.fuzz import case_json, fuzz_case\n"
                f"print(json.dumps([hashlib.sha256(case_json(fuzz_case(s, 256)).encode()).hexdigest()"
                f" for s in {seeds!r}]))\n")
        env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONHASHSEED="12345")
        result = subprocess.run([sys.executable, "-W", "ignore", "-c", code], cwd=ROOT, env=env,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout.strip().splitlines()[-1]),
                         [digest(case_json(fuzz_case(seed, 256))) for seed in seeds])

    def test_seed_selects_every_kind(self):
        kinds = {fuzz_case(seed, 256).scene.params["kind"] for seed in range(12)}
        self.assertEqual(kinds, set(KINDS))

    def test_forcing_a_kind(self):
        for seed in (0, 5):
            natural = fuzz_case(seed, 256)
            drawn = natural.scene.params["kind"]
            self.assertEqual(case_json(fuzz_case(seed, 256, drawn)), case_json(natural))
            for kind in KINDS:
                with self.subTest(seed=seed, kind=kind):
                    case = fuzz_case(seed, 256, kind)
                    self.assertEqual(case.scene.params["kind"], kind)
                    self.assertEqual(case.tags, ("fuzz", kind))
                    self.assertEqual(case.case_id, case_id(seed, 256, kind))
                    self.assertEqual(case.case_id, f"fuzz-{kind}-s{seed}-256")
                    if kind == "layered":
                        self.assertIn("shapes", case.scene.params)
                    else:
                        self.assertEqual(case.scene.params["shape"]["shape"], kind)
                        self.assertEqual(len(case.scene.regions), 1)

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            fuzz_case(1, 256, "spiral")
        with self.assertRaises(ValueError):
            fuzz_case(-1, 256)


def check_scenes(test: unittest.TestCase, seeds: range, size: int = 256) -> dict:
    """Assert every accepted scene of ``seeds`` is valid; return the summed
    rejection counts."""
    totals: dict[str, int] = {}
    for seed in seeds:
        scene, truth = fuzz_scene(seed, size)
        params = scene.params
        for reason, count in params["rejections"].items():
            totals[reason] = totals.get(reason, 0) + count
        with test.subTest(seed=seed, kind=params["kind"]):
            test.assertFalse(params["fallback"])
            for region in scene.regions:
                outer = Polygon(region.outline.flatten(FLATTEN_TOLERANCE))
                for ring in region.rings:
                    points = ring.flatten(FLATTEN_TOLERANCE)
                    test.assertTrue(LinearRing(points).is_simple)
                    test.assertTrue(Polygon(points).is_valid)
                for hole in region.holes:
                    test.assertTrue(outer.contains(Polygon(hole.flatten(FLATTEN_TOLERANCE))))
            if params["kind"] in ("blob", "cornered-blob"):
                outline = scene.regions[0].outline
                labelled = {corner["anchor"] for corner in params["shape"].get("corners", [])}
                corners = {index for index, (_, turn) in enumerate(outline.join_turns())
                           if turn > 0.0}
                test.assertEqual(corners, labelled)
            canvas = float(size * size)
            areas = {colour: geometry.area for colour, geometry in truth.colour_geometry.items()}
            test.assertEqual(sorted(areas), list(range(len(scene.palette))))
            test.assertAlmostEqual(sum(areas.values()) / canvas, 1.0, delta=1e-7)
            union = shapely.union_all(list(truth.colour_geometry.values()))
            test.assertAlmostEqual(union.area / canvas, 1.0, delta=1e-7)
            for colour, area in areas.items():
                test.assertGreaterEqual(area / canvas, MIN_COLOUR_SHARE)
            # No overlay sliver survives as a hole or part to inflate topology counts.
            for geometry in truth.colour_geometry.values():
                for polygon in getattr(geometry, "geoms", [geometry]):
                    test.assertGreater(polygon.area, 1e-6)
                    for ring in polygon.interiors:
                        test.assertGreater(Polygon(ring).area, 1e-6)
            test.assertEqual(palette_violations([row["rgb"] for row in scene.palette]), [])
    return totals


class AcceptedScenesTest(unittest.TestCase):
    """300 seeds in six parallelisable chunks."""

    def test_seeds_0_to_49(self):
        check_scenes(self, range(0, 50))

    def test_seeds_50_to_99(self):
        check_scenes(self, range(50, 100))

    def test_seeds_100_to_149(self):
        check_scenes(self, range(100, 150))

    def test_seeds_150_to_199(self):
        check_scenes(self, range(150, 200))

    def test_seeds_200_to_249(self):
        check_scenes(self, range(200, 250))

    def test_seeds_250_to_299(self):
        check_scenes(self, range(250, 300))

    def test_rejection_paths_are_exercised(self):
        totals = check_scenes(self, range(300, 340))
        self.assertGreater(totals.get("self-intersection", 0), 0)
        self.assertGreater(totals.get("colour-share", 0), 0)


class RejectionTest(unittest.TestCase):
    def test_self_intersecting_outline_is_a_rejection(self):
        bowtie = [(0, 0), (10, 10), (10, 0), (0, 10)]
        with self.assertRaises(fuzz._Rejected) as caught:
            fuzz._outline(lambda: polygon(bowtie))
        self.assertEqual(caught.exception.reason, "self-intersection")

    def test_unrelated_errors_propagate(self):
        def broken():
            raise ValueError("an unrelated failure")

        with self.assertRaises(ValueError):
            fuzz._outline(broken)
        with mock.patch.object(fuzz, "scene_truth", side_effect=ValueError("unrelated")):
            with self.assertRaisesRegex(ValueError, "unrelated"):
                fuzz_scene(0, 256)

    def test_truth_degeneracy_is_rejected_then_falls_back(self):
        real = scene_truth

        def degenerate(scene):
            if scene.params:  # a drawn scene, not the fallback
                raise ValueError("two different curves overlap along a stretch; shared "
                                 "boundaries must use identical segments")
            return real(scene)

        with mock.patch.object(fuzz, "scene_truth", side_effect=degenerate):
            scene, truth = fuzz_scene(2, 256, "blob")
        self.assertTrue(scene.params["fallback"])
        self.assertEqual(scene.params["attempts"], fuzz.SCENE_ATTEMPTS)
        self.assertEqual(scene.params["rejections"]["truth-partial-overlap"], fuzz.SCENE_ATTEMPTS)
        self.assertEqual(sorted(truth.colour_geometry), [0, 1])

    def test_fallback_scenes_are_valid(self):
        for kind in KINDS:
            with self.subTest(kind=kind), mock.patch.object(fuzz, "SCENE_ATTEMPTS", 0):
                case = fuzz_case(7, 256, kind)
                self.assertTrue(case.scene.params["fallback"])
                self.assertEqual(case.tags, ("fuzz", kind))
                self.assertEqual(case_json(case), case_json(case_from_fresh(kind)))
                truth = scene_truth(case.scene)
                total = sum(g.area for g in truth.colour_geometry.values())
                self.assertAlmostEqual(total, 256 * 256, delta=1e-3)


def case_from_fresh(kind: str):
    with mock.patch.object(fuzz, "SCENE_ATTEMPTS", 0):
        return fuzz_case(7, 256, kind)


class PaletteTest(unittest.TestCase):
    def test_fallback_palette_meets_the_constraints(self):
        self.assertEqual(palette_violations(FALLBACK_PALETTE), [])

    def test_constraints_clear_the_palette_stage_thresholds(self):
        settings = PaletteSettings()
        merge = max(settings.similar_delta_e, settings.small_delta_e, settings.fragmented_delta_e)
        self.assertGreaterEqual(fuzz.MIN_PALETTE_DELTA_E, merge + 5.0)
        self.assertGreaterEqual(MIN_COLOUR_SHARE, 2.0 * settings.min_colour_share)

    def test_violations_are_reported(self):
        self.assertTrue(palette_violations([(10, 10, 10), (14, 12, 10)]))
        # Mid-grey sits on the black/white blend.
        self.assertTrue(palette_violations([(0, 0, 0), (255, 255, 255), (119, 119, 119)]))


def feature(regions, size: int = 256) -> float:
    scene = Scene(size, size, PALETTE, 0, regions)
    return estimate_min_feature_px(scene_truth(scene))


class MinFeatureTest(unittest.TestCase):
    def test_strip_width(self):
        self.assertAlmostEqual(feature([Region(1, rectangle(20, 100, 220, 106))]), 6.0, delta=0.05)

    def test_disc_diameter(self):
        self.assertAlmostEqual(feature([Region(1, circle((128, 128), 15))]), 30.0, delta=0.1)

    def test_neck_between_two_discs(self):
        regions = [Region(1, circle((70, 128), 40)), Region(1, circle((186, 128), 40)),
                   Region(1, rectangle(70, 124, 186, 132))]
        self.assertAlmostEqual(feature(regions), 8.0, delta=0.05)

    def test_gap_between_two_blocks(self):
        regions = [Region(1, rectangle(40, 40, 126, 216)), Region(1, rectangle(129, 40, 216, 216))]
        self.assertAlmostEqual(feature(regions), 3.0, delta=0.05)

    def test_arm_on_a_body(self):
        # A 4 px wide, 60 px long arm on a large square: no split, lost by protrusion.
        regions = [Region(1, rectangle(40, 40, 140, 140)), Region(1, rectangle(139, 90, 200, 94))]
        self.assertAlmostEqual(feature(regions), 4.0, delta=0.1)

    def test_acute_corner_is_not_a_narrow_feature(self):
        tip = math.radians(10.0)
        apex = (30.0, 128.0)
        length = 200.0
        points = [apex, (apex[0] + length * math.cos(tip / 2), apex[1] - length * math.sin(tip / 2)),
                  (apex[0] + length * math.cos(tip / 2), apex[1] + length * math.sin(tip / 2))]
        # The triangle is at most 2 * 200 * sin(5 deg) = 34.9 px wide at its base.
        self.assertGreater(feature([Region(1, polygon(points))]), 15.0)


def _restarted(ring, shift: int, reverse: bool) -> list:
    coords = list(ring.coords)[:-1]
    if reverse:
        coords.reverse()
    shift %= len(coords)
    return coords[shift:] + coords[:shift]


def _restarted_geometry(geometry, shift: int, reverse: bool):
    polygons = [Polygon(_restarted(part.exterior, shift, reverse),
                        [_restarted(ring, shift, reverse) for ring in part.interiors])
                for part in shapely.get_parts(geometry)]
    return polygons[0] if len(polygons) == 1 else shapely.MultiPolygon(polygons)


class RingStartInvarianceTest(unittest.TestCase):
    """The estimate is a function of the geometry alone: restarting every ring at
    another vertex or reversing it never changes it."""

    def assert_invariant(self, truth, shifts=(1, 7, 40)):
        expected = estimate_min_feature_px(truth)
        for reverse in (False, True):
            for shift in (0, *shifts):
                if shift == 0 and not reverse:
                    continue
                with self.subTest(shift=shift, reverse=reverse):
                    geometry = {colour: _restarted_geometry(part, shift, reverse)
                                for colour, part in truth.colour_geometry.items()}
                    for colour, part in geometry.items():
                        self.assertTrue(part.equals(truth.colour_geometry[colour]))
                    restarted = replace(truth, colour_geometry=geometry)
                    self.assertEqual(estimate_min_feature_px(restarted), expected)
        return expected

    def test_cornered_blob_whose_estimate_followed_the_start_vertex(self):
        # fuzz_case(1029, 1024): restarting its rings once moved the estimate
        # between 72.575 and 96.162 px (a sub-0.01 px erosion sliver was counted
        # as a split, and whether it appeared followed the simplification's
        # start-anchored vertex choice).
        _, truth = fuzz.fuzz_scene(1029, 1024, "cornered-blob")
        self.assertAlmostEqual(self.assert_invariant(truth), 96.162, delta=0.001)

    def test_neck_between_two_discs(self):
        regions = [Region(1, circle((70, 128), 40)), Region(1, circle((186, 128), 40)),
                   Region(1, rectangle(70, 124, 186, 132))]
        truth = scene_truth(Scene(256, 256, PALETTE, 0, regions))
        self.assertAlmostEqual(self.assert_invariant(truth), 8.0, delta=0.05)

    def test_ring_with_a_hole(self):
        regions = [Region(1, circle((128, 128), 90)), Region(0, circle((150, 120), 40))]
        truth = scene_truth(Scene(256, 256, PALETTE, 0, regions))
        self.assert_invariant(truth)

    def test_layered_scene(self):
        _, truth = fuzz.fuzz_scene(1009, 256, "layered")
        self.assert_invariant(truth)


class PinnedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads(SEEDS.read_text(encoding="utf-8"))
        cls.rows = cls.data["pinned"]

    def test_seed_file_covers_kinds_and_sizes(self):
        self.assertGreaterEqual(len(self.rows), 36)
        keys = [(row["seed"], row["size"], row["kind"]) for row in self.rows]
        self.assertEqual(len(keys), len(set(keys)))
        for kind in KINDS:
            self.assertEqual({row["size"] for row in self.rows if row["kind"] == kind}, set(SIZES))
        for row in self.rows:
            self.assertTrue(row["note"])
        train, held_out = (tuple(self.data["tuning"][name]) for name in ("train", "held_out"))
        for start, stop in (train, held_out):
            self.assertLess(start, stop)
        self.assertTrue(train[1] <= held_out[0] or held_out[1] <= train[0])
        for row in self.rows:
            for start, stop in (train, held_out):
                self.assertFalse(start <= row["seed"] < stop)

    def test_committed_files_are_exactly_the_pinned_cases(self):
        expected = {f"{case_id(row['seed'], row['size'], row['kind'])}.{ext}"
                    for row in self.rows for ext in ("json", "svg")}
        self.assertEqual({path.name for path in PINNED.iterdir()}, expected)

    def test_pinned_cases_load(self):
        cases = pinned_cases(SEEDS)
        self.assertEqual([case.case_id for case in cases],
                         [case_id(row["seed"], row["size"], row["kind"]) for row in self.rows])
        for case, row in zip(cases, self.rows):
            self.assertEqual(case.tags, ("fuzz", row["kind"]))
            self.assertEqual((case.scene.width, case.scene.height), (row["size"], row["size"]))
            self.assertGreater(case.min_feature_px, 0.0)

    def regenerate(self, kind: str):
        for row in self.rows:
            if row["kind"] != kind:
                continue
            with self.subTest(seed=row["seed"], size=row["size"]):
                case = fuzz_case(row["seed"], row["size"], kind)
                self.assertFalse(case.scene.params["fallback"])
                self.assertEqual(case_json(case),
                                 (PINNED / f"{case.case_id}.json").read_text(encoding="utf-8"))
                self.assertEqual(case.scene.to_svg(),
                                 (PINNED / f"{case.case_id}.svg").read_text(encoding="utf-8"))

    def test_pinned_polygons_regenerate(self):
        self.regenerate("polygon")

    def test_pinned_blobs_regenerate(self):
        self.regenerate("blob")

    def test_pinned_cornered_blobs_regenerate(self):
        self.regenerate("cornered-blob")

    def test_pinned_layered_regenerate(self):
        self.regenerate("layered")


if __name__ == "__main__":
    unittest.main()
