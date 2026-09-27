"""The icon corpus of the synthetic fidelity suite (``vinylgen.fidelity.icons``).

Covers the SVG importer (absolute and relative commands and their implicit
repeats, smooth-curve reflection, exact quadratic-to-cubic raising, elliptical
arcs through the endpoint-to-centre conversion and through affine maps
including reflections, ``transform`` and ``viewBox``, the nonzero and evenodd
fill rules with holes and an island inside a hole, refusal of touching
subpaths and of stroke-only glyphs), and the committed subset: provenance and
byte-identical-style records, counts, unique ids, deterministic scenes,
placement within the canvas, and a slice of full cases building truth, minimum
feature and labels-tier bounds (every case: ``tests/fullimage/
test_fidelity_icons_corpus.py``).
"""

import json
import math
import unittest
from pathlib import Path

import numpy as np

from vinylgen.fidelity import bounds, icons
from vinylgen.fidelity.icons import (RawArc, RawCubic, RawLine, element_regions,
                                     glyph_case, glyph_regions, map_segment, parse_path,
                                     parse_svg, parse_transform, quadratic_to_cubic,
                                     scene_segment)
from vinylgen.fidelity.scene import Cubic, EllipticalArc, Line, polygon
from vinylgen.fidelity.sweep import case_family
from vinylgen.fidelity.truth import scene_truth

ROOT = Path(__file__).resolve().parents[1]
# Material Symbols files byte-identical to an earlier style's are cased once
# (SOURCE.json identical_styles): 20 Outlined and 4 Rounded files.
EXPECTED_COUNTS = {("material", "sharp"): 60, ("material", "rounded"): 56,
                   ("material", "outlined"): 40, ("bootstrap", "regular"): 20,
                   ("remix", "fill"): 20, ("heroicons", "solid"): 20}


def _svg(body: str, extra: str = 'viewBox="0 0 100 100"') -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {extra}>{body}</svg>'


def _square(x0, y0, x1, y1, clockwise=True):
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return polygon(points if clockwise else points[::-1])


class PathDataTest(unittest.TestCase):
    def test_relative_and_absolute_commands_agree(self):
        absolute = parse_path("M10 10 L30 10 H40 V30 C40 40 30 45 20 40 Q10 35 10 10 Z")
        relative = parse_path("m10 10 l20 0 h10 v20 c0 10-10 15-20 10 q-10-5-10-30 z")
        self.assertEqual(len(absolute), 1)
        self.assertEqual(len(absolute[0]), len(relative[0]))
        for a, b in zip(absolute[0], relative[0]):
            self.assertEqual(type(a), type(b))
            np.testing.assert_allclose(icons.raw_points(a, 8), icons.raw_points(b, 8),
                                       atol=1e-12)

    def test_implicit_repeats_and_second_subpath(self):
        subpaths = parse_path("M0 0 10 0 10 10z m20 0 l5 0 0 5")
        self.assertEqual(len(subpaths), 2)
        self.assertEqual([type(s) for s in subpaths[0]], [RawLine] * 3)
        # The relative moveto starts from the closed subpath's start point, and an
        # open subpath is closed as a fill closes it.
        self.assertEqual(subpaths[1][0].p0, (20.0, 0.0))
        self.assertEqual(subpaths[1][-1].p1, (20.0, 0.0))

    def test_zero_length_subpath_is_dropped(self):
        self.assertEqual(parse_path("M0 0 L10 0 L10 10 Z M5 5 Z"), [parse_path(
            "M0 0 L10 0 L10 10 Z")[0]])

    def test_smooth_reflections(self):
        cubic = parse_path("M0 0 C0 10 10 10 10 0 S20 -10 20 0 Z")[0]
        self.assertEqual(cubic[1].c1, (10.0, -10.0))
        quad = parse_path("M0 0 Q10 10 20 0 T40 0 Z")[0]
        expected = quadratic_to_cubic((20.0, 0.0), (30.0, -10.0), (40.0, 0.0))
        self.assertEqual(quad[1], expected)

    def test_quadratic_raised_to_cubic_exactly(self):
        p0, q, p1 = (1.0, 2.0), (7.5, -3.25), (11.0, 9.0)
        cubic = quadratic_to_cubic(p0, q, p1)
        t = np.linspace(0.0, 1.0, 101)[:, None]
        quadratic = ((1 - t) ** 2 * np.asarray(p0) + 2 * (1 - t) * t * np.asarray(q)
                     + t * t * np.asarray(p1))
        np.testing.assert_allclose(icons.raw_points(cubic, 100), quadratic, atol=1e-12)

    def test_arc_flags_without_separators(self):
        packed = parse_path("M0 0a10 10 0 0110 10z")[0]
        spaced = parse_path("M0 0 a 10 10 0 0 1 10 10 z")[0]
        self.assertEqual(packed[0], spaced[0])

    def test_semicircle_arc(self):
        arc = parse_path("M0 0 A10 10 0 0 1 20 0 Z")[0][0]
        self.assertIsInstance(arc, RawArc)
        points = icons.raw_points(arc, 32)
        np.testing.assert_allclose(np.hypot(points[:, 0] - 10.0, points[:, 1]), 10.0,
                                   atol=1e-12)
        np.testing.assert_allclose(points[0], (0.0, 0.0), atol=1e-12)
        np.testing.assert_allclose(points[-1], (20.0, 0.0), atol=1e-12)
        # Sweep flag 1 runs in the positive-angle direction: clockwise on a y-down
        # screen, so from the left end over the top (negative y).
        self.assertAlmostEqual(float(points[16][1]), -10.0, places=9)

    def test_rotated_elliptical_arc_endpoints_and_carrier(self):
        for large in (0, 1):
            for sweep in (0, 1):
                arc = parse_path(f"M3 4 A12 5 30 {large} {sweep} 15 9 Z")[0][0]
                points = icons.raw_points(arc, 64)
                np.testing.assert_allclose(points[0], (3.0, 4.0), atol=1e-9)
                np.testing.assert_allclose(points[-1], (15.0, 9.0), atol=1e-9)
                c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
                d = points - np.asarray(arc.centre)
                u, v = c * d[:, 0] + s * d[:, 1], -s * d[:, 0] + c * d[:, 1]
                np.testing.assert_allclose((u / 12) ** 2 + (v / 5) ** 2, 1.0, atol=1e-9)
                self.assertEqual(abs(arc.sweep) > math.pi, bool(large))
                self.assertEqual(arc.sweep > 0, bool(sweep))

    def test_out_of_range_radii_scale_up(self):
        arc = parse_path("M0 0 A1 1 0 0 1 10 0 Z")[0][0]
        points = icons.raw_points(arc, 16)
        np.testing.assert_allclose(np.hypot(points[:, 0] - 5.0, points[:, 1]), 5.0, atol=1e-9)


class AffineTest(unittest.TestCase):
    def test_transform_composition(self):
        m = parse_transform("translate(5,6) rotate(90) scale(2)")
        np.testing.assert_allclose(m @ [1.0, 0.0, 1.0], [5.0, 8.0, 1.0], atol=1e-12)
        pivot = parse_transform("rotate(180 10 10)")
        np.testing.assert_allclose(pivot @ [0.0, 0.0, 1.0], [20.0, 20.0, 1.0], atol=1e-12)
        matrix = parse_transform("matrix(1 2 3 4 5 6)")
        np.testing.assert_allclose(matrix @ [1.0, 1.0, 1.0], [9.0, 12.0, 1.0])

    def test_arc_through_affine_maps_exactly(self):
        arc = parse_path("M3 4 A12 5 30 1 0 15 9 Z")[0][0]
        for text in ("scale(2 0.5)", "skewX(25) rotate(40)", "scale(-1 1)",
                     "matrix(0.3 -1.2 0.8 0.4 7 -3)", "scale(1 -1) rotate(10)"):
            m = parse_transform(text)
            mapped = map_segment(m, arc)
            segment = scene_segment(mapped)
            self.assertIsInstance(segment, EllipticalArc)
            t = np.linspace(0.0, 1.0, 41)
            np.testing.assert_allclose(segment.point(t), mapped.point(t), atol=1e-9,
                                       err_msg=text)
            original = icons.raw_points(arc, 40)
            by_map = (m[:2, :2] @ original.T).T + m[:2, 2]
            np.testing.assert_allclose(segment.point(t), by_map, atol=1e-9, err_msg=text)

    def test_group_transform_and_viewbox(self):
        text = _svg('<g transform="translate(10 0)"><path transform="scale(2)" '
                    'd="M0 0H5V5H0Z"/></g>',
                    'viewBox="0 -50 50 50" width="100" height="100"')
        (element,) = parse_svg(text)
        corners = [segment.p0 for segment in element.subpaths[0]]
        # viewBox maps (0, -50)-(50, 0) onto 100 x 100: scale 2, y shifted by 100.
        self.assertEqual(corners, [(20.0, 100.0), (40.0, 100.0), (40.0, 120.0), (20.0, 120.0)])

    def test_degenerate_cubics(self):
        straight = scene_segment(RawCubic((0.0, 0.0), (2.0, 0.0), (7.0, 0.0), (10.0, 0.0)))
        self.assertIsInstance(straight, Line)
        nudged = scene_segment(RawCubic((0.0, 0.0), (0.0, 0.0), (10.0, 10.0), (20.0, 0.0)))
        self.assertIsInstance(nudged, Cubic)
        self.assertLessEqual(math.dist(nudged.c1, (0.0, 0.0)), 1e-3 + 1e-12)
        t = np.linspace(0.0, 1.0, 201)[:, None]
        authored = 3 * (1 - t) * t * t * np.asarray([10.0, 10.0]) + t ** 3 * np.asarray(
            [20.0, 0.0])
        self.assertLess(float(np.max(np.hypot(*(nudged.point(t[:, 0]) - authored).T))), 5e-4)
        self.assertIsNone(scene_segment(RawLine((1.0, 1.0), (1.0, 1.0))))


class FillRuleTest(unittest.TestCase):
    def test_same_direction_nested_rings(self):
        rings = [_square(0, 0, 10, 10), _square(3, 3, 7, 7)]
        nonzero = element_regions(rings, "nonzero", 1)
        self.assertEqual([(len(r.holes)) for r in nonzero], [0])
        evenodd = element_regions(rings, "evenodd", 1)
        self.assertEqual([len(r.holes) for r in evenodd], [1])

    def test_opposite_direction_ring_is_a_hole_in_both_rules(self):
        rings = [_square(0, 0, 10, 10), _square(3, 3, 7, 7, clockwise=False)]
        for rule in ("nonzero", "evenodd"):
            regions = element_regions(rings, rule, 1)
            self.assertEqual([len(r.holes) for r in regions], [1], rule)

    def test_island_inside_a_hole(self):
        outer, hole = _square(0, 0, 20, 20), _square(4, 4, 16, 16, clockwise=False)
        island = _square(8, 8, 12, 12)
        for rule in ("nonzero", "evenodd"):
            regions = element_regions([island, outer, hole], rule, 1)
            self.assertEqual(len(regions), 2, rule)
            self.assertIs(regions[0].outline, outer)
            self.assertEqual(regions[0].holes, (hole,))
            self.assertIs(regions[1].outline, island)
            self.assertEqual(regions[1].holes, ())
        # Under nonzero an island running the same way as the hole winds to -1 and
        # is still filled; an inner ring running the same way as the outline winds
        # to 2, so it is no hole and everything inside it is filled.
        same_as_hole = _square(8, 8, 12, 12, clockwise=False)
        regions = element_regions([outer, hole, same_as_hole], "nonzero", 1)
        self.assertEqual([len(r.holes) for r in regions], [1, 0])
        same_as_outer = _square(4, 4, 16, 16)
        regions = element_regions([outer, same_as_outer, island], "nonzero", 1)
        self.assertEqual([(r.outline, r.holes) for r in regions], [(outer, ())])
        regions = element_regions([outer, same_as_outer, island], "evenodd", 1)
        self.assertEqual([len(r.holes) for r in regions], [1, 0])

    def test_island_scene_renders_with_its_hole(self):
        text = _svg('<path fill-rule="evenodd" d="M0 0H100V100H0Z M20 20H80V80H20Z '
                    'M40 40H60V60H40Z"/>')
        elements = parse_svg(text)
        regions = glyph_regions(elements, np.eye(3), 1)
        self.assertEqual([len(r.holes) for r in regions], [1, 0])

    def test_separate_islands_and_touching_subpaths(self):
        separate = [_square(0, 0, 4, 4), _square(6, 0, 10, 4)]
        self.assertEqual(len(element_regions(separate, "nonzero", 1)), 2)
        touching = [_square(0, 0, 4, 4), _square(4, 0, 8, 4)]
        with self.assertRaisesRegex(ValueError, "touch or cross"):
            element_regions(touching, "nonzero", 1)

    def test_stroke_only_glyph_is_refused(self):
        with self.assertRaisesRegex(ValueError, "stroke-only"):
            parse_svg(_svg('<path fill="none" stroke="black" d="M0 0L10 10"/>'))

    def test_elements_it_cannot_honour_refuse_the_glyph(self):
        square = '<path d="M0 0H10V10H0Z"/>'
        for extra in ('<use href="#a"/>', '<polyline points="0,0 5,5 0,5"/>',
                      '<path style="display:none" d="M2 2H4V4H2Z"/>',
                      '<path opacity="0.5" d="M2 2H4V4H2Z"/>',
                      '<path clip-path="url(#c)" d="M2 2H4V4H2Z"/>',
                      '<svg x="5"><path d="M2 2H4V4H2Z"/></svg>'):
            with self.assertRaises(ValueError, msg=extra):
                parse_svg(_svg(square + extra))

    def test_fully_drawn_spellings_are_accepted(self):
        for attributes in ('opacity="1.00"', 'display="block"', 'visibility="inherit"',
                           'style="opacity: 1 !important"', 'fill-opacity="100%"'):
            (element,) = parse_svg(_svg(f'<path {attributes} d="M0 0H10V10H0Z"/>'))
            self.assertTrue(element.subpaths, attributes)

    def test_pattern_and_marker_children_are_not_glyph_geometry(self):
        text = _svg('<pattern id="p"><path d="M0 0H1V1H0Z"/></pattern><path d="M0 0H10V10H0Z"/>')
        self.assertEqual(len(parse_svg(text)), 1)


class CorpusTest(unittest.TestCase):
    """The committed subset, checked on scenes (cheap) and on a slice of full cases
    (truth, minimum feature, labels bounds). Every case's truth and bounds are
    checked in the full-image bucket (``tests/fullimage/test_fidelity_icons_corpus.py``)."""

    SLICE = 24

    @classmethod
    def setUpClass(cls):
        cls.rows = icons.glyph_files()
        cls.scenes = [icons.glyph_scene(path.read_text(encoding="utf-8"), set_name, style,
                                        name, source=str(path.relative_to(icons.ICONS_DIR)))
                      for set_name, style, name, path, _ in cls.rows]

    def test_sources_record_provenance(self):
        for source in icons.load_sources():
            directory = icons.ICONS_DIR / source["set"]
            self.assertRegex(source["commit"], r"^[0-9a-f]{40}$")
            self.assertTrue(source["repository"].startswith("https://github.com/"))
            self.assertIn(source["licence"], ("Apache-2.0", "MIT"))
            self.assertTrue((directory / "LICENSE").read_text(encoding="utf-8").strip())
            for name, styles in source.get("identical_styles", {}).items():
                for style, target in styles.items():
                    self.assertFalse((directory / style / f"{name}.svg").exists())
                    self.assertTrue((directory / target / f"{name}.svg").exists())

    def test_counts_ids_and_tags(self):
        counts, ids, material = {}, [], {}
        for (set_name, style, name, _, identical), scene in zip(self.rows, self.scenes):
            counts[(set_name, style)] = counts.get((set_name, style), 0) + 1
            ids.append(icons.case_id(set_name, style, name, scene.width))
            self.assertEqual(scene.family, f"icon-{set_name}")
            self.assertIn(scene.width, icons.SIZES)
            tags = icons.case_tags(set_name, style, identical)
            self.assertEqual(tags[:4], ("icon", set_name, style, f"icon-{set_name}-{style}"))
            if set_name == "material":
                # Every Material name is covered in all three styles, by its own
                # case or by the case of the byte-identical file.
                for other in (style, *identical):
                    material.setdefault(other, []).append(name)
        self.assertEqual(counts, EXPECTED_COUNTS)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual({style: len(names) for style, names in material.items()},
                         {"sharp": 60, "rounded": 60, "outlined": 60})
        self.assertEqual(set(material["sharp"]), set(material["outlined"]))
        self.assertEqual(set(material["sharp"]), set(material["rounded"]))

    def test_deterministic(self):
        for (set_name, style, name, path, _), scene in zip(self.rows, self.scenes):
            again = icons.glyph_scene(path.read_text(encoding="utf-8"), set_name, style, name,
                                      source=str(path.relative_to(icons.ICONS_DIR)))
            self.assertEqual(json.dumps(again.to_json()), json.dumps(scene.to_json()))
        set_name, style, name, path, _ = self.rows[0]
        other = icons.glyph_scene(path.read_text(encoding="utf-8"), set_name, style, name,
                                  seed=1)
        self.assertNotEqual(other.params["rotation_deg"], self.scenes[0].params["rotation_deg"])

    def test_glyph_fills_about_80_percent_of_the_canvas(self):
        for scene in self.scenes:
            points = np.concatenate([ring.flatten(0.01) for region in scene.regions
                                     for ring in region.rings])
            extent = float(np.max(points.max(axis=0) - points.min(axis=0)))
            self.assertAlmostEqual(extent / scene.width, icons.FILL, delta=0.002,
                                   msg=scene.params["source"])
            self.assertTrue(np.all(points > 0) and np.all(points < scene.width))

    def test_slice_builds_cases_truth_and_labels_bounds(self):
        for set_name, style, name, path, identical in self.rows[::self.SLICE]:
            with self.subTest(glyph=f"{set_name}/{style}/{name}"):
                case = glyph_case(path.read_text(encoding="utf-8"), set_name, style, name,
                                  identical=identical)
                self.assertEqual(case_family(case), f"icon-{set_name}")
                self.assertGreater(case.min_feature_px, 0.0)
                again = glyph_case(path.read_text(encoding="utf-8"), set_name, style, name,
                                   identical=identical)
                self.assertEqual(again.min_feature_px, case.min_feature_px)
                truth = scene_truth(case.scene)
                self.assertGreater(len(truth.corners), 0)
                self.assertIsNotNone(bounds.case_bounds(case, "labels", truth))


if __name__ == "__main__":
    unittest.main()
