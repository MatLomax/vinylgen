"""The synthetic corpus's shape families and its committed fixed set.

Covers byte-identical regeneration of the committed cases, their validity (every
scene loads and validates, and its truth partitions the canvas), case identity and
feature widths, and closed-form truth spot checks on representative families.
"""

import math
import re
import unittest
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import box

from vinylgen.fidelity import families
from vinylgen.fidelity.raster import render_rgb
from vinylgen.fidelity.truth import scene_truth

ROOT = Path(__file__).resolve().parents[1]
FIXED = ROOT / families.FIXED_DIR
PALETTE_STAGE = ROOT / families.PALETTE_STAGE_DIR
CASE_ID = re.compile(r"^[a-z0-9.]+(-[a-z0-9.]+)*$")


def committed_files(directory: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8")
            for path in sorted(directory.iterdir()) if path.suffix in (".json", ".svg")}


class CommittedSetTests(unittest.TestCase):
    def test_committed_sets_regenerate_byte_identically(self):
        for directory, cases in ((FIXED, families.fixed_set()),
                                 (PALETTE_STAGE, families.palette_stage_set())):
            with self.subTest(directory=directory.name):
                expected = families.case_files(cases)
                found = committed_files(directory)
                self.assertEqual(sorted(found), sorted(expected),
                                 "run scripts/generate_synthetic_fixed.py")
                for name, text in expected.items():
                    self.assertEqual(found[name], text, f"{name} differs from the generator")

    def test_loaded_cases_equal_the_generated_cases(self):
        for directory, cases in ((FIXED, families.fixed_set()),
                                 (PALETTE_STAGE, families.palette_stage_set())):
            loaded = families.load_fixed_set(directory)
            generated = sorted(cases, key=lambda case: case.case_id)
            self.assertEqual([case.case_id for case in loaded],
                             [case.case_id for case in generated])
            for mine, theirs in zip(loaded, generated):
                self.assertEqual(families.case_json_text(mine), families.case_json_text(theirs))
                self.assertEqual(mine.scene.to_svg(), theirs.scene.to_svg())


class CaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixed = families.load_fixed_set(FIXED)
        cls.palette_stage = families.load_fixed_set(PALETTE_STAGE)
        cls.cases = cls.fixed + cls.palette_stage
        cls.truths = {case.case_id: scene_truth(case.scene) for case in cls.cases}

    def test_every_truth_partitions_the_canvas(self):
        for case in self.cases:
            with self.subTest(case=case.case_id):
                truth = self.truths[case.case_id]
                canvas = box(0, 0, truth.width, truth.height).area
                geometries = list(truth.colour_geometry.values())
                self.assertAlmostEqual(sum(g.area for g in geometries) / canvas, 1.0, delta=1e-9)
                self.assertAlmostEqual(shapely.union_all(geometries).area / canvas, 1.0,
                                       delta=1e-9)
                for i in range(len(geometries)):
                    for j in range(i + 1, len(geometries)):
                        self.assertLess(geometries[i].intersection(geometries[j]).area, 1e-6)
                # Every palette colour a scene declares is visible.
                self.assertEqual(sorted(truth.colour_geometry), list(range(len(case.scene.palette))))

    def test_case_ids_tags_and_feature_widths(self):
        ids = [case.case_id for case in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for case in self.cases:
            with self.subTest(case=case.case_id):
                self.assertRegex(case.case_id, CASE_ID)
                self.assertGreater(case.min_feature_px, 0.0)
                self.assertTrue(math.isfinite(case.min_feature_px))
                self.assertEqual(len({"scaled", "fixed-px"} & set(case.tags)), 1)
                self.assertEqual(case.tags[0], case.scene.family)

    def test_fixed_set_spans_small_medium_and_large_canvases_for_every_family(self):
        by_family: dict[str, set[int]] = {}
        for case in self.fixed:
            if "scaled" in case.tags:
                by_family.setdefault(case.tags[0], set()).add(case.scene.width)
        self.assertEqual(len(by_family), 16)
        for family, sizes in by_family.items():
            self.assertEqual(sizes, set(families.FIXED_SET_SIZES), family)
        self.assertGreaterEqual(len(self.fixed), 60)
        self.assertLessEqual(len(self.fixed), 120)

    def test_full_grid_is_unique_and_contains_the_fixed_set(self):
        grid = families.full_grid()
        ids = [case.case_id for case in grid]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual({case.case_id for case in self.fixed}, set(ids))
        self.assertTrue(all(case.min_feature_px > 0 for case in grid))

    def truth(self, case_id):
        return self.truths[case_id]

    def test_five_point_star_has_ten_corners(self):
        corners = self.truth("star-5p-r0.4-128").corners
        self.assertEqual(len(corners), 10)
        self.assertEqual({corner.kind for corner in corners}, {"corner"})

    def test_pie_of_six_has_a_centre_junction(self):
        truth = self.truth("pie-6-ph0-512")
        centre = [c for c in truth.corners if math.dist(c.point, (256.0, 256.0)) < 1e-9]
        self.assertEqual([c.kind for c in centre], ["junction"])
        self.assertEqual(sorted(truth.colour_geometry), list(range(7)))

    def test_stadium_has_no_corners(self):
        for size in families.FIXED_SET_SIZES:
            rotation = 20 if size == 512 else 0
            self.assertEqual(self.truth(f"stadium-rot{rotation}-{size}").corners, [])

    def test_d_shape_has_two_right_angle_corners(self):
        for case_id in ("d-rot0-128", "d-rot20-512"):
            corners = self.truth(case_id).corners
            self.assertEqual([c.kind for c in corners], ["corner", "corner"], case_id)
            for corner in corners:
                self.assertAlmostEqual(corner.turn_deg, 90.0, places=9)

    def test_lens_and_crescent_have_two_cusps(self):
        for case_id in ("lens-rot0-128", "crescent-rot0-128"):
            corners = self.truth(case_id).corners
            self.assertEqual([c.kind for c in corners], ["corner", "corner"], case_id)
            self.assertTrue(all(0.0 < c.turn_deg < 180.0 for c in corners), case_id)

    def test_corner_only_contact_is_a_junction(self):
        truth = self.truth("corner-contact-rot30-ph0.5-512")
        centre = [c for c in truth.corners if math.dist(c.point, (256.5, 256.5)) < 1e-9]
        self.assertEqual([c.kind for c in centre], ["junction"])

    def test_rounded_rectangles_straddle_the_sharpening_radius(self):
        for size in families.FIXED_SET_SIZES:
            cases = [c for c in self.fixed if c.scene.family == "rounded-rectangle"
                     and c.scene.width == size and "scaled" in c.tags]
            radius = families.sharpening_radius_px(size)
            self.assertAlmostEqual(radius, max(1.0, size / 1000) / (math.sqrt(2) - 1), places=9)
            below = [c for c in cases if c.scene.params["radius_px"] < radius]
            above = [c for c in cases if c.scene.params["radius_px"] > radius]
            self.assertTrue(below and above, size)
            for case in cases:
                self.assertEqual(self.truth(case.case_id).corners, [])

    def test_every_preservation_point_is_a_pixel_wholly_inside_its_colour(self):
        preserving = [c for c in self.palette_stage
                      if (c.scene.params.get("palette_options") or {}).get(
                          "preserved_colour_points")]
        self.assertTrue(preserving)
        for case in preserving:
            rgb = families.render_case_rgb(case)
            for x, y in case.scene.params["palette_options"]["preserved_colour_points"]:
                with self.subTest(case=case.case_id, point=(x, y)):
                    pixel = box(x, y, x + 1, y + 1)
                    owners = [index for index, geometry
                              in self.truth(case.case_id).colour_geometry.items()
                              if geometry.buffer(1e-9).contains(pixel)]
                    self.assertEqual(len(owners), 1, "the pixel straddles a colour boundary")
                    # Full coverage: the anti-aliased render is the colour itself.
                    self.assertEqual(rgb[y, x].tolist(),
                                     list(case.scene.palette[owners[0]]["rgb"]))

    def test_the_ideal_segmentation_geometry_counts_the_ideal_nodes(self):
        from shapely.geometry import Polygon
        from vinylgen.fidelity.bounds import zone_ideal_nodes
        for case in self.cases:
            with self.subTest(case=case.case_id):
                truth = self.truth(case.case_id)
                self.assertEqual(zone_ideal_nodes(truth, Polygon()), truth.ideal_nodes)
        # A disc (4 ideal nodes, 90 degrees apart at any phase) with a zone over
        # its right-hand quarter of diameter: the part outside sweeps 240 degrees,
        # which holds at most floor(240 / 90) + 1 = 3 of them.
        disc = next(c for c in self.fixed if c.case_id == "disc-off0-128")
        truth = self.truth(disc.case_id)
        x0, y0, x1, y1 = truth.colour_geometry[1].bounds
        zone = box(x1 - (x1 - x0) / 4.0, 0, truth.width, truth.height)
        self.assertEqual(zone_ideal_nodes(truth, zone), 3)
        # A square's corner counts while its radius reaches outside the zone.
        square = next(c for c in self.fixed if c.case_id == "square-rot0-int-128")
        truth = self.truth(square.case_id)
        corner = truth.corners[0].point
        small = box(corner[0] - 1, corner[1] - 1, corner[0] + 1, corner[1] + 1)
        self.assertEqual(zone_ideal_nodes(truth, small, {}, 2.0), 4)
        self.assertEqual(zone_ideal_nodes(truth, small, {}, 0.5), 3)
        self.assertEqual(zone_ideal_nodes(truth, box(0, 0, truth.width, truth.height)), 0)

    def test_noise_is_seeded_and_bounded(self):
        noisy = [c for c in self.palette_stage if "noise" in c.scene.params]
        self.assertTrue(noisy)
        case = min(noisy, key=lambda c: c.scene.width)
        first = families.render_case_rgb(case)
        np.testing.assert_array_equal(first, families.render_case_rgb(case))
        clean = render_rgb(case.scene).astype(int)
        amplitude = case.scene.params["noise"]["amplitude"]
        self.assertLessEqual(int(np.abs(first.astype(int) - clean).max()), amplitude)
        self.assertGreater(int(np.abs(first.astype(int) - clean).max()), 0)
        quiet = next(c for c in self.palette_stage if "noise" not in c.scene.params)
        np.testing.assert_array_equal(families.render_case_rgb(quiet), render_rgb(quiet.scene))


if __name__ == "__main__":
    unittest.main()
