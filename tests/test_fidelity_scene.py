"""The synthetic corpus's vector scene model, coverage rasteriser and truth extraction.

Covers lossless JSON, the SVG export, outline validation, the proven flattening
bounds, the rasterised areas against analytic areas, byte-identical renders across
runs and processes, and the truth's corners, junctions, edge points, node counts
and colour partition on scenes whose answers are known in closed form.
"""

import hashlib
import json
import math
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, box

from vinylgen.fidelity.raster import coverage, render_labels, render_rgb
from vinylgen.fidelity.scene import (FLATTEN_TOLERANCE, Arc, Cubic, EllipticalArc, Line,
                                     Outline, Region, Scene, circle, ellipse, polygon,
                                     rectangle, segment_intersections, wedge)
from vinylgen.fidelity.truth import scene_truth

ROOT = Path(__file__).resolve().parents[1]

PALETTE = [{"name": "paper", "rgb": [236, 232, 220]}, {"name": "red", "rgb": [196, 40, 44]},
           {"name": "blue", "rgb": [30, 60, 150]}, {"name": "green", "rgb": [40, 150, 70]}]


def disc_scene(size=100, centre=(50.3, 49.6), radius=30.7):
    return Scene(size, size, PALETTE, 0, [Region(1, circle(centre, radius))])


def mixed_scene():
    blob = Outline((Cubic((20, 70), (30, 40), (60, 40), (70, 70)),
                    Arc((45, 70), 25, 0.0, math.pi)))
    return Scene(120, 90, PALETTE, 0, [
        Region(1, rectangle(10.5, 10.25, 80.75, 60.5), (circle((40, 35), 9.5),)),
        Region(2, ellipse((80, 45), 30, 12, 0.35)),
        Region(3, blob),
        Region(1, wedge((100, 70), 18, -2.0, 2.4)),
    ], family="mixed", params={"seed": 7, "note": "test"})


def sample_colours(truth, point, radius=0.05, count=720, width=None, height=None):
    """Colours met walking a small circle around ``point`` (None off the canvas)."""
    colours = []
    for k in range(count):
        angle = 2 * math.pi * (k + 0.5) / count
        q = Point(point[0] + radius * math.cos(angle), point[1] + radius * math.sin(angle))
        if not (0 <= q.x <= truth.width and 0 <= q.y <= truth.height):
            colours.append(None)
            continue
        colours.append(next(c for c, g in truth.colour_geometry.items() if g.covers(q)))
    return colours


class SceneModelTests(unittest.TestCase):
    def test_json_round_trip_is_identical(self):
        scene = mixed_scene()
        data = scene.to_json()
        text = json.dumps(data)
        restored = Scene.from_json(json.loads(text))
        self.assertEqual(restored, scene)
        self.assertEqual(restored.to_json(), data)
        self.assertEqual(json.dumps(restored.to_json()), text)

    def test_json_preserves_floats_exactly(self):
        value = 0.1 + 0.2
        scene = Scene(10, 10, PALETTE, 0, [Region(1, circle((value, 5.0), math.pi))])
        restored = Scene.from_json(json.loads(json.dumps(scene.to_json())))
        arc = restored.regions[0].outline.segments[0]
        self.assertEqual(arc.centre[0], value)
        self.assertEqual(arc.radius, math.pi)

    def test_svg_export_parses_as_xml(self):
        scene = mixed_scene()
        root = ET.fromstring(scene.to_svg())
        namespace = "{http://www.w3.org/2000/svg}"
        self.assertEqual(root.tag, f"{namespace}svg")
        self.assertEqual(root.get("viewBox"), "0 0 120 90")
        paths = root.findall(f"{namespace}path")
        self.assertEqual(len(paths), len(scene.regions))
        self.assertTrue(all(path.get("fill-rule") == "evenodd" for path in paths))
        # A full circle is written as four quarter arcs.
        self.assertEqual(paths[0].get("d").count(" A "), 4)

    def test_outline_rejects_a_gap(self):
        with self.assertRaisesRegex(ValueError, "ends"):
            Outline((Line((0, 0), (10, 0)), Line((10, 0), (10, 10)), Line((10, 10.1), (0, 0))))

    def test_outline_rejects_self_intersection_and_doubling_back(self):
        with self.assertRaises(ValueError):
            polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
        with self.assertRaises(ValueError):
            Outline((Line((0, 0), (10, 0)), Line((10, 0), (0, 0))))

    def test_cubic_with_a_cusp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cusp"):
            Cubic((0, 0), (10, 10), (0, 10), (10, 0))

    def test_corner_turn_angles(self):
        self.assertEqual([turn for _, turn in rectangle(0, 0, 4, 3).corners()], [90.0] * 4)
        self.assertEqual(circle((0, 0), 5).corners(), [])
        self.assertEqual(ellipse((0, 0), 5, 2, 0.4).corners(), [])
        turns = [round(turn, 9) for _, turn in wedge((0, 0), 10, 0.0, math.pi / 3).corners()]
        # The radii meet at 60 degrees (turn 120); each meets the arc at 90.
        self.assertEqual(turns, [120.0, 90.0, 90.0])

    def test_scaled_scene_requires_an_integer_canvas(self):
        scene = disc_scene(size=64)
        scaled = scene.scaled(1.5)
        self.assertEqual((scaled.width, scaled.height), (96, 96))
        self.assertAlmostEqual(scaled.regions[0].outline.segments[0].radius, 30.7 * 1.5)
        with self.assertRaises(ValueError):
            scene.scaled(1.01)

    def test_flattening_honours_the_chord_error_bound(self):
        tolerance = 0.01
        segments = [Arc((3, 4), 250, 0.3, 4.0), EllipticalArc((0, 0), 300, 40, 0.7, 0.2, 6.0),
                    Cubic((0, 0), (200, 400), (300, -300), (500, 100))]
        for segment in segments:
            params = segment.sample_params(tolerance)
            polyline = LineString(segment.point(params))
            fine = segment.point(np.linspace(0, 1, 200001))
            deviation = shapely.distance(shapely.points(fine), polyline).max()
            self.assertLessEqual(deviation, tolerance, type(segment).__name__)

    def test_intersections_are_exact(self):
        arc = Arc((0, 0), 10, 0, 2 * math.pi)
        found = segment_intersections(arc, Line((-20, 6), (20, 6)))
        self.assertEqual(len(found), 2)
        for _, _, (x, y) in found:
            self.assertAlmostEqual(abs(x), 8.0, places=12)
            self.assertAlmostEqual(y, 6.0, places=12)
        first = EllipticalArc((0, 0), 30, 10, 0.2, 0, 2 * math.pi)
        second = Cubic((-40, -20), (-10, 40), (10, -40), (40, 20))
        found = segment_intersections(first, second)
        self.assertGreaterEqual(len(found), 2)
        for s, t, _ in found:
            self.assertLess(np.linalg.norm(first.point(s) - second.point(t)), 1e-9)


class RasterTests(unittest.TestCase):
    def area_bound(self, vertical_extent, perimeter, supersample=16):
        # Each subsample row misplaces each of its two crossings by at most one
        # subsample column (1/s px), and each row has weight 1/s px; the flattened
        # outline lies within FLATTEN_TOLERANCE of the true one.
        return 2 * vertical_extent / supersample + perimeter * FLATTEN_TOLERANCE

    def test_disc_area_matches_analytic_area(self):
        radius = 30.7
        area = coverage(disc_scene(radius=radius))[1].sum()
        self.assertLessEqual(abs(area - math.pi * radius ** 2),
                             self.area_bound(2 * radius, 2 * math.pi * radius))

    def test_square_area_matches_analytic_area(self):
        x0, y0, x1, y1 = 12.3, 17.85, 61.1, 70.45
        scene = Scene(80, 80, PALETTE, 0, [Region(2, rectangle(x0, y0, x1, y1))])
        area = coverage(scene)[2].sum()
        self.assertLessEqual(abs(area - (x1 - x0) * (y1 - y0)),
                             self.area_bound(y1 - y0, 0.0))
        aligned = Scene(80, 80, PALETTE, 0, [Region(2, rectangle(10, 20, 50, 45))])
        self.assertEqual(coverage(aligned)[2].sum(), 40 * 25)

    def test_coverage_sums_to_one_and_matches_truth_areas(self):
        scene = mixed_scene()
        fractions = coverage(scene)
        self.assertEqual(fractions.shape, (4, 90, 120))
        np.testing.assert_array_equal(fractions.sum(axis=0), 1.0)
        truth = scene_truth(scene)
        for colour, geometry in truth.colour_geometry.items():
            perimeter = geometry.length
            self.assertLessEqual(abs(fractions[colour].sum() - geometry.area),
                                 self.area_bound(0, 0) + 2 * perimeter / 16, colour)

    def test_labels_take_the_largest_coverage_and_ties_go_up_the_stack(self):
        scene = Scene(10, 4, PALETTE, 0, [Region(1, rectangle(0, 0, 5.5, 4))])
        labels = render_labels(scene)
        self.assertEqual(labels.dtype, np.int16)
        self.assertTrue(np.all(labels[:, :5] == 1))
        self.assertTrue(np.all(labels[:, 5] == 1))  # exactly half covered
        self.assertTrue(np.all(labels[:, 6:] == 0))
        # Occlusion: the topmost region wins.
        stacked = Scene(10, 10, PALETTE, 0, [Region(1, rectangle(0, 0, 10, 10)),
                                             Region(2, rectangle(2, 2, 8, 8))])
        self.assertTrue(np.all(render_labels(stacked)[2:8, 2:8] == 2))

    def test_rgb_blends_in_linear_light(self):
        scene = Scene(10, 4, PALETTE, 0, [Region(2, rectangle(0, 0, 5.5, 4))])
        rgb = render_rgb(scene)
        self.assertEqual(rgb.dtype, np.uint8)
        np.testing.assert_array_equal(rgb[:, 0], np.tile(PALETTE[2]["rgb"], (4, 1)))
        np.testing.assert_array_equal(rgb[:, 9], np.tile(PALETTE[0]["rgb"], (4, 1)))

        def linear(value):
            c = value / 255.0
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        def encode(value):
            c = 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
            return round(c * 255.0)

        expected = [encode(0.5 * linear(a) + 0.5 * linear(b))
                    for a, b in zip(PALETTE[2]["rgb"], PALETTE[0]["rgb"])]
        np.testing.assert_array_equal(rgb[0, 5], expected)

    def test_renders_are_byte_identical_across_runs_and_processes(self):
        scene = mixed_scene()
        first = (render_labels(scene).tobytes(), render_rgb(scene).tobytes())
        second = (render_labels(scene).tobytes(), render_rgb(scene).tobytes())
        self.assertEqual(first, second)
        digest = hashlib.sha256(first[0] + first[1]).hexdigest()
        script = (
            "import hashlib, json, sys\n"
            "from vinylgen.fidelity.raster import render_labels, render_rgb\n"
            "from vinylgen.fidelity.scene import Scene\n"
            "scene = Scene.from_json(json.loads(sys.stdin.read()))\n"
            "print(hashlib.sha256(render_labels(scene).tobytes()"
            " + render_rgb(scene).tobytes()).hexdigest())\n")
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True,
                                input=json.dumps(scene.to_json()), capture_output=True,
                                text=True)
        self.assertEqual(result.stdout.strip(), digest)


class TruthTests(unittest.TestCase):
    def assert_partition(self, truth):
        canvas = box(0, 0, truth.width, truth.height)
        geometries = list(truth.colour_geometry.values())
        total = sum(geometry.area for geometry in geometries)
        self.assertAlmostEqual(total / canvas.area, 1.0, delta=1e-6)
        self.assertAlmostEqual(shapely.union_all(geometries).area / canvas.area, 1.0, delta=1e-6)
        for i in range(len(geometries)):
            for j in range(i + 1, len(geometries)):
                self.assertLess(geometries[i].intersection(geometries[j]).area, 1e-6)

    def test_disc_has_four_ideal_nodes_and_no_corners(self):
        truth = scene_truth(disc_scene())
        self.assertEqual(truth.corners, [])
        self.assertEqual(truth.ideal_nodes, 4)
        self.assertEqual(sorted(truth.colour_geometry), [0, 1])
        # Flattened within FLATTEN_TOLERANCE, so the area is off by at most the
        # perimeter times that.
        self.assertAlmostEqual(truth.colour_geometry[1].area, math.pi * 30.7 ** 2,
                               delta=2 * math.pi * 30.7 * FLATTEN_TOLERANCE)
        self.assert_partition(truth)

    def test_square_has_four_right_angle_corners(self):
        scene = Scene(100, 100, PALETTE, 0, [Region(1, rectangle(20, 20, 70, 70))])
        truth = scene_truth(scene)
        self.assertEqual([(c.point, c.turn_deg, c.kind) for c in truth.corners],
                         [((20.0, 20.0), 90.0, "corner"), ((70.0, 20.0), 90.0, "corner"),
                          ((20.0, 70.0), 90.0, "corner"), ((70.0, 70.0), 90.0, "corner")])
        self.assertEqual(truth.ideal_nodes, 4)
        self.assert_partition(truth)

    def test_square_over_disc(self):
        # The square's top-left corner sits on the disc's centre, so it hides one
        # quadrant: its edges cross the circle at (80, 50) and (50, 80).
        scene = Scene(100, 100, PALETTE, 0, [Region(1, circle((50, 50), 30)),
                                             Region(2, rectangle(50, 50, 90, 90))])
        truth = scene_truth(scene)
        found = {(round(c.point[0], 9), round(c.point[1], 9)): (c.kind, round(c.turn_deg, 9))
                 for c in truth.corners}
        self.assertEqual(found, {
            (50.0, 50.0): ("corner", 90.0), (90.0, 50.0): ("corner", 90.0),
            (90.0, 90.0): ("corner", 90.0), (50.0, 90.0): ("corner", 90.0),
            (80.0, 50.0): ("junction", 90.0), (50.0, 80.0): ("junction", 90.0)})
        for corner in truth.corners:
            colours = set(sample_colours(truth, corner.point))
            self.assertEqual(len(colours), 3 if corner.kind == "junction" else 2, corner)
        # Six vertices, and the visible 270-degree arc splits into three pieces.
        self.assertEqual(truth.ideal_nodes, 8)
        self.assertAlmostEqual(truth.colour_geometry[1].area, 0.75 * math.pi * 900,
                               delta=1.5 * math.pi * 30 * FLATTEN_TOLERANCE)
        self.assert_partition(truth)

    def test_square_crossing_a_same_coloured_disc_leaves_corners(self):
        scene = Scene(100, 100, PALETTE, 0, [Region(1, circle((50, 50), 30)),
                                             Region(1, rectangle(50, 50, 90, 90))])
        truth = scene_truth(scene)
        crossings = [c for c in truth.corners if c.point in ((80.0, 50.0), (50.0, 80.0))]
        self.assertEqual([c.kind for c in crossings], ["corner", "corner"])
        # The circle leaves (80, 50) along the vertical tangent; the square edge
        # continues horizontally: a 90-degree turn.
        self.assertTrue(all(abs(c.turn_deg - 90.0) < 1e-9 for c in crossings))
        self.assertNotIn((50.0, 50.0), [c.point for c in truth.corners])
        self.assert_partition(truth)

    def test_pie_of_three_wedges_meets_at_a_centre_junction(self):
        third = 2 * math.pi / 3
        scene = Scene(100, 100, PALETTE, 0,
                      [Region(1 + k, wedge((50, 50), 30, k * third, third)) for k in range(3)])
        truth = scene_truth(scene)
        centre = [c for c in truth.corners if math.dist(c.point, (50, 50)) < 1e-9]
        self.assertEqual(len(centre), 1)
        self.assertEqual(centre[0].kind, "junction")
        self.assertAlmostEqual(centre[0].turn_deg, 60.0, places=9)
        self.assertEqual(set(sample_colours(truth, (50, 50))), {1, 2, 3})
        # The three rim points are junctions of two wedges and the background.
        rim = [c for c in truth.corners if c is not centre[0]]
        self.assertEqual([c.kind for c in rim], ["junction"] * 3)
        for corner in rim:
            self.assertAlmostEqual(math.dist(corner.point, (50, 50)), 30.0, places=9)
        # Four vertices; each 120-degree arc needs one interior node.
        self.assertEqual(truth.ideal_nodes, 7)
        self.assert_partition(truth)

    def test_boundaries_meeting_the_image_edge_are_edge_points(self):
        scene = Scene(100, 100, PALETTE, 0, [Region(1, rectangle(0, 0, 100, 40)),
                                             Region(2, circle((50, 40), 20))])
        truth = scene_truth(scene)
        edges = [c.point for c in truth.corners if c.kind == "edge"]
        self.assertEqual(edges, [(0.0, 40.0), (100.0, 40.0)])
        junctions = sorted(c.point for c in truth.corners if c.kind == "junction")
        self.assertEqual(len(junctions), 2)
        for found, expected in zip(junctions, [(30.0, 40.0), (70.0, 40.0)]):
            self.assertLess(math.dist(found, expected), 1e-12)
        # The image border itself carries no nodes: the two edge points, the two
        # junctions and one interior node on each 180-degree half circle.
        self.assertEqual(truth.ideal_nodes, 6)
        self.assert_partition(truth)

    def test_colours_meeting_corner_to_corner_form_a_junction(self):
        scene = Scene(100, 100, PALETTE, 0, [Region(1, rectangle(0, 0, 50, 50)),
                                             Region(1, rectangle(50, 50, 100, 100))])
        truth = scene_truth(scene)
        centre = [c for c in truth.corners if c.point == (50.0, 50.0)]
        self.assertEqual([c.kind for c in centre], ["junction"])

    def test_holes_and_shared_cubic_edges(self):
        shared = Cubic((0, 50), (30, 20), (70, 80), (100, 50))
        top = Outline((Line((0, 0), (100, 0)), Line((100, 0), (100, 50)),
                       Cubic(shared.p1, shared.c2, shared.c1, shared.p0), Line((0, 50), (0, 0))))
        bottom = Outline((shared, Line((100, 50), (100, 100)), Line((100, 100), (0, 100)),
                          Line((0, 100), (0, 50))))
        scene = Scene(100, 100, PALETTE, 0,
                      [Region(1, top, (circle((50, 20), 8),)), Region(2, bottom)])
        truth = scene_truth(scene)
        self.assertEqual([(c.point, c.kind) for c in truth.corners],
                         [((0.0, 50.0), "edge"), ((100.0, 50.0), "edge")])
        # The shared cubic counts once (two edge points), the hole is a circle.
        self.assertEqual(truth.ideal_nodes, 6)
        self.assertAlmostEqual(truth.colour_geometry[0].area, math.pi * 64,
                               delta=2 * math.pi * 8 * FLATTEN_TOLERANCE)
        self.assert_partition(truth)

    def test_truth_scales_with_the_scene(self):
        scene = mixed_scene()
        truth = scene_truth(scene)
        scaled = scene_truth(scene.scaled(3))
        self.assertEqual(scaled.ideal_nodes, truth.ideal_nodes)
        self.assertEqual(len(scaled.corners), len(truth.corners))
        for small, large in zip(truth.corners, scaled.corners):
            self.assertEqual(small.kind, large.kind)
            self.assertLess(math.dist((3 * small.point[0], 3 * small.point[1]), large.point), 1e-6)
            self.assertAlmostEqual(small.turn_deg, large.turn_deg, places=6)
        self.assert_partition(truth)
        self.assert_partition(scaled)

    def test_every_true_corner_is_a_real_vertex_of_the_partition(self):
        truth = scene_truth(mixed_scene())
        self.assertTrue(truth.corners)
        for corner in truth.corners:
            colours = sample_colours(truth, corner.point, radius=0.1)
            changes = sum(1 for k in range(len(colours))
                          if colours[k] != colours[k - 1]
                          and None not in (colours[k], colours[k - 1]))
            minimum = {"corner": 2, "junction": 3, "edge": 1}[corner.kind]
            self.assertGreaterEqual(changes, minimum, corner)


if __name__ == "__main__":
    unittest.main()
