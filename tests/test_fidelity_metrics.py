import json
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import Point, box

from vinylgen.contracts import TraceSettings
from vinylgen.fidelity.harness import trace_labels, trace_rgb
from vinylgen.fidelity.metrics import (DISTANCE_ERROR_BOUND, authored_nodes, boundary_pieces,
                                       count_inflections, curve_quality, curve_reference,
                                       measure, node_placement)
from vinylgen.fidelity.scene import FLATTEN_TOLERANCE
from vinylgen.fidelity.truth import SceneTruth, TrueCorner

ROOT = Path(__file__).resolve().parents[1]
PALETTE = [{"name": "Ground", "rgb": [240, 235, 220]}, {"name": "Ink", "rgb": [30, 40, 90]}]
GROUND, INK = "#F0EBDC", "#1E285A"
# The standard four-cubic circle's handle length as a fraction of the radius.
KAPPA = 4.0 * (math.sqrt(2.0) - 1.0) / 3.0


def svg(width: int, height: int, layers: list[tuple[str, list[tuple[str, str]]]]) -> str:
    """A traced-edge-style SVG: ``layers`` are ``(fill, [(d, nodetypes), ...])``."""
    groups = []
    for number, (fill, paths) in enumerate(layers):
        elements = "".join(
            f'<path id="p{number}-{index}" d="{d}" sodipodi:nodetypes="{types}" />'
            for index, (d, types) in enumerate(paths))
        groups.append(f'<g inkscape:groupmode="layer" inkscape:label="L{number}" '
                      f'fill="{fill}" fill-rule="evenodd">{elements}</g>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            'xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd" '
            f'viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
            + "".join(groups) + "</svg>")


def frame(width: int, height: int) -> str:
    return f"M0 0 L{width} 0 L{width} {height} L0 {height} L0 0 Z"


def rectangle(x0, y0, x1, y1, reverse=False) -> str:
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    if reverse:
        corners = [corners[0], *reversed(corners[1:])]
    return ("M" + " L".join(f"{x:g} {y:g}" for x, y in [*corners, corners[0]]) + " Z")


def square_svg(x0, y0, x1, y1, size=20) -> str:
    return svg(size, size, [
        (GROUND, [(frame(size, size) + " " + rectangle(x0, y0, x1, y1, reverse=True),
                   "ccccc" + "ccccc")]),
        (INK, [(rectangle(x0, y0, x1, y1), "ccccc")])])


def circle_d(cx, cy, r, reverse=False) -> str:
    """A circle of four cubics starting at its rightmost point."""
    k = KAPPA * r
    quarters = [((cx + r, cy), (cx + r, cy + k), (cx + k, cy + r), (cx, cy + r)),
                ((cx, cy + r), (cx - k, cy + r), (cx - r, cy + k), (cx - r, cy)),
                ((cx - r, cy), (cx - r, cy - k), (cx - k, cy - r), (cx, cy - r)),
                ((cx, cy - r), (cx + k, cy - r), (cx + r, cy - k), (cx + r, cy))]
    if reverse:
        quarters = [(q[3], q[2], q[1], q[0]) for q in reversed(quarters)]
    start = quarters[0][0]
    parts = [f"M{start[0]:.10g} {start[1]:.10g}"]
    for _, c1, c2, end in quarters:
        parts.append("C" + " ".join(f"{v:.10g}" for point in (c1, c2, end) for v in point))
    return " ".join(parts) + " Z"


def circle_polygon(cx, cy, r):
    """A circle flattened to at most ``FLATTEN_TOLERANCE`` chord error, as truths are."""
    vertices = math.ceil(math.pi / math.acos(1.0 - FLATTEN_TOLERANCE / r))
    return Point(cx, cy).buffer(r, quad_segs=math.ceil(vertices / 4))


def square_truth(x0=5, y0=5, x1=15, y1=15, size=20, corners=None) -> SceneTruth:
    ink = box(x0, y0, x1, y1)
    if corners is None:
        corners = [TrueCorner((x, y), 90.0, "corner") for x, y in ink.exterior.coords[:-1]]
    return SceneTruth(width=size, height=size,
                      colour_geometry={0: box(0, 0, size, size).difference(ink), 1: ink},
                      corners=corners, ideal_nodes=4)


class FidelityMetricTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self._dir = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text)
        return path

    def test_exact_square_has_zero_distance_and_full_overlap(self):
        path = self.write("exact.svg", square_svg(5, 5, 15, 15))
        result = measure(path, square_truth(), PALETTE, corner_radius=1.0)
        self.assertLessEqual(result["boundary"]["hausdorff_px"], DISTANCE_ERROR_BOUND)
        for colour in ("0", "1"):
            self.assertAlmostEqual(result["area"]["per_colour"][colour]["iou"], 1.0, places=9)
        self.assertAlmostEqual(result["area"]["misassigned_px"], 0.0, places=9)
        self.assertEqual(result["corners"]["recall"], 1.0)
        self.assertEqual(result["kinks"]["count"], 0)
        self.assertEqual(result["topology"]["0"]["traced"], {"polygons": 1, "holes": 1})
        self.assertEqual(result["topology"]["1"], {"traced": {"polygons": 1, "holes": 0},
                                                   "truth": {"polygons": 1, "holes": 0}})
        json.dumps(result)

    def test_offset_square_distance_and_iou_are_analytic(self):
        path = self.write("offset.svg", square_svg(5.5, 5, 15.5, 15))
        result = measure(path, square_truth(), PALETTE, corner_radius=1.0)
        boundary = result["boundary"]
        self.assertLessEqual(abs(boundary["hausdorff_px"] - 0.5), DISTANCE_ERROR_BOUND)
        self.assertAlmostEqual(boundary["hausdorff_per_mille"],
                               boundary["hausdorff_px"] * 1000 / 20)
        for colour in ("0", "1"):
            self.assertLessEqual(
                abs(boundary["per_colour"][colour]["hausdorff_px"] - 0.5), DISTANCE_ERROR_BOUND)
        ink = result["area"]["per_colour"]["1"]
        self.assertAlmostEqual(ink["iou"], 95.0 / 105.0, places=9)
        self.assertAlmostEqual(ink["symmetric_difference_px"], 10.0, places=9)
        self.assertAlmostEqual(result["area"]["misassigned_px"], 10.0, places=9)
        # Each traced corner is 0.5 px from its true corner.
        self.assertEqual(result["corners"]["found"], 4)
        self.assertEqual(measure(path, square_truth(), PALETTE, corner_radius=0.4)
                         ["corners"]["found"], 0)

    def test_four_cubic_circle_distance_matches_its_radial_error(self):
        size, r = 200, 80.0
        cx = cy = size / 2
        path = self.write("circle.svg", svg(size, size, [
            (GROUND, [(frame(size, size) + " " + circle_d(cx, cy, r, reverse=True),
                       "ccccc" + "zzzzz")]),
            (INK, [(circle_d(cx, cy, r), "zzzzz")])]))
        disc = circle_polygon(cx, cy, r)
        truth = SceneTruth(width=size, height=size,
                           colour_geometry={0: box(0, 0, size, size).difference(disc), 1: disc},
                           ideal_nodes=4)
        result = measure(path, truth, PALETTE, corner_radius=2.0)
        # The four-cubic circle's radial error, evaluated independently and densely.
        t = np.linspace(0.0, 1.0, 200_001)
        p0, p1, p2, p3 = np.array([[r, 0], [r, KAPPA * r], [KAPPA * r, r], [0, r]])
        curve = ((1 - t) ** 3)[:, None] * p0 + (3 * (1 - t) ** 2 * t)[:, None] * p1 \
            + (3 * (1 - t) * t * t)[:, None] * p2 + (t ** 3)[:, None] * p3
        radial = float(np.abs(np.hypot(*curve.T) - r).max())
        self.assertGreater(radial, 0.02)
        # The truth polygon itself sits up to FLATTEN_TOLERANCE inside the analytic
        # circle, which DISTANCE_ERROR_BOUND includes.
        self.assertLessEqual(abs(result["boundary"]["hausdorff_px"] - radial),
                             DISTANCE_ERROR_BOUND)
        self.assertGreater(result["area"]["per_colour"]["1"]["iou"], 0.999)
        nodes = result["nodes"]
        self.assertEqual(nodes["per_path_total"], 12)
        self.assertEqual(nodes["unique"], 8)
        self.assertEqual(nodes["canvas_corners"], 4)
        self.assertEqual(nodes["boundary"], 4)
        self.assertEqual(nodes["ratio_to_ideal"], 1.0)
        self.assertEqual(nodes["types"], {"cusp": 0, "symmetric": 0, "smooth": 4})
        self.assertEqual(nodes["type_conflicts"], 0)
        self.assertEqual(result["kinks"]["count"], 0)
        self.assertIsNone(result["corners"]["recall"])

    def test_spurious_cusp_on_a_smooth_circle_is_one_kink(self):
        size, r = 100, 30.0
        cx = cy = size / 2
        path = self.write("kink.svg", svg(size, size, [
            (GROUND, [(frame(size, size) + " " + circle_d(cx, cy, r, reverse=True),
                       "ccccc" + "czzzc")]),
            (INK, [(circle_d(cx, cy, r), "czzzc")])]))
        disc = circle_polygon(cx, cy, r)
        truth = SceneTruth(width=size, height=size,
                           colour_geometry={0: box(0, 0, size, size).difference(disc), 1: disc},
                           ideal_nodes=4)
        result = measure(path, truth, PALETTE, corner_radius=2.0)
        self.assertEqual(result["kinks"]["count"], 1)
        self.assertEqual(result["kinks"]["kinks"][0]["point"], [cx + r, cy])
        self.assertEqual(result["kinks"]["cusp_precision"], 0.0)
        self.assertEqual(result["nodes"]["types"], {"cusp": 1, "symmetric": 0, "smooth": 3})

    def test_cusp_three_pixels_from_a_true_corner(self):
        path = self.write("near.svg", square_svg(8, 5, 15, 15))
        truth = square_truth(corners=[TrueCorner((5.0, 5.0), 90.0, "corner")])
        missed = measure(path, truth, PALETTE, corner_radius=2.0)
        self.assertEqual(missed["corners"]["found"], 0)
        miss = missed["corners"]["misses"][0]
        self.assertEqual(miss["turn_deg"], 90.0)
        self.assertAlmostEqual(miss["nearest_cusp_px"], 3.0)
        found = measure(path, truth, PALETTE, corner_radius=4.0)
        self.assertEqual(found["corners"]["recall"], 1.0)
        self.assertEqual(found["corners"]["per_kind"]["corner"], {"true": 1, "found": 1, "recall": 1.0})
        # The other three traced cusps lie far from the only true corner.
        self.assertEqual(found["kinks"]["count"], 3)

    def test_mismatched_node_types_are_rejected(self):
        path = self.write("bad.svg", svg(20, 20, [(INK, [(rectangle(5, 5, 15, 15), "cccc")])]))
        with self.assertRaises(ValueError):
            measure(path, square_truth(), PALETTE, corner_radius=1.0)


class TraceHarnessTests(unittest.TestCase):
    def test_disc_traced_through_the_real_trace_stage(self):
        size, radius = 96, 20.0
        centre = size / 2
        yy, xx = np.mgrid[0:size, 0:size] + 0.5
        labels = (np.hypot(xx - centre, yy - centre) <= radius).astype(np.int16)
        disc = circle_polygon(centre, centre, radius)
        truth = SceneTruth(width=size, height=size,
                           colour_geometry={0: box(0, 0, size, size).difference(disc), 1: disc},
                           ideal_nodes=4)
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            run = trace_labels((labels, PALETTE), Path(name), TraceSettings(length_unit="px"))
            self.assertTrue(run.edge_svg.exists())
            self.assertGreater(run.seconds, 0.0)
            self.assertEqual(run.colour_map, {GROUND: 0, INK: 1})
            result = measure(run.edge_svg, truth, PALETTE, corner_radius=2.0,
                             colour_map=run.colour_map)
        boundary = result["boundary"]
        for key in ("hausdorff_px", "mean_px", "p95_px"):
            self.assertTrue(math.isfinite(boundary[key]))
        self.assertLess(boundary["hausdorff_px"], 2.0)
        self.assertGreater(result["area"]["per_colour"]["1"]["iou"], 0.95)
        self.assertGreater(result["nodes"]["boundary"], 0)
        self.assertEqual(result["topology"]["1"]["traced"], {"polygons": 1, "holes": 0})
        self.assertEqual(result["topology"]["0"]["traced"], {"polygons": 1, "holes": 1})

    def test_square_traced_through_the_real_trace_stage_keeps_its_corners(self):
        size = 96
        labels = np.zeros((size, size), dtype=np.int16)
        labels[24:72, 24:72] = 1
        truth = square_truth(24, 24, 72, 72, size=size)
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            run = trace_labels((labels, PALETTE), Path(name), TraceSettings(length_unit="px"))
            result = measure(run.edge_svg, truth, PALETTE, corner_radius=2.0,
                             colour_map=run.colour_map)
        self.assertLess(result["boundary"]["hausdorff_px"], 2.0)
        self.assertEqual(result["corners"]["recall"], 1.0)
        self.assertEqual(result["kinks"]["count"], 0)

    def test_anti_aliased_disc_through_the_real_palette_and_trace_stages(self):
        size, radius, factor = 96, 20.0, 8
        centre = size / 2
        fine = (np.mgrid[0:size * factor, 0:size * factor] + 0.5) / factor
        inside = np.hypot(fine[1] - centre, fine[0] - centre) <= radius
        coverage = inside.reshape(size, factor, size, factor).mean(axis=(1, 3))[..., None]
        ground, ink = (np.asarray(row["rgb"], dtype=np.float64) for row in PALETTE)
        rgb = np.rint(ground * (1 - coverage) + ink * coverage).astype(np.uint8)
        disc = circle_polygon(centre, centre, radius)
        truth = SceneTruth(width=size, height=size,
                           colour_geometry={0: box(0, 0, size, size).difference(disc), 1: disc},
                           ideal_nodes=4)
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            run = trace_rgb((rgb, PALETTE), Path(name), TraceSettings(length_unit="px"))
            result = measure(run.edge_svg, truth, PALETTE, corner_radius=2.0,
                             colour_map=run.colour_map)
        self.assertEqual(run.unmatched, [])
        self.assertEqual(sorted(run.colour_map.values()), [0, 1])
        self.assertEqual(result["area"]["unmatched_layers"], [])
        self.assertLess(result["boundary"]["hausdorff_px"], 2.0)
        self.assertGreater(result["area"]["per_colour"]["1"]["iou"], 0.95)


def circle_ring(centre, radius, count=720):
    angles = np.linspace(0.0, 2.0 * math.pi, count, endpoint=False)
    ring = np.stack((centre[0] + radius * np.cos(angles), centre[1] + radius * np.sin(angles)), 1)
    return np.vstack((ring, ring[:1]))


class CurveQualityTests(unittest.TestCase):
    def test_tangent_error_ignores_how_finely_a_curve_was_flattened(self):
        # The traced side is flattened ten times coarser than the truth: chord
        # directions alone would differ by up to half a chord's turn (5 degrees at
        # radius 3); the vertex tangents agree to a fraction of a degree.
        for radius in (3.0, 10.0, 50.0):
            chords = [math.ceil(math.pi / math.acos(1.0 - tolerance / radius))
                      for tolerance in (0.001, 0.01)]
            reference = curve_reference([circle_ring((50, 50), radius, chords[0])], 100, 100)
            quality = curve_quality([circle_ring((50, 50), radius, chords[1])], reference)
            self.assertLess(quality["tangent_max_deg"], 0.5, radius)

    def test_tangent_error_holds_for_unequal_chords(self):
        # Chords alternating 1:3 in angle: a length-weighted direction leans towards
        # the longer chord by degrees; the three-point estimate stays exact.
        steps = np.tile([1.0, 3.0], 60)
        angles = np.concatenate(([0.0], np.cumsum(steps)))[:-1] * (2 * math.pi / steps.sum())
        ring = np.stack((50 + 10 * np.cos(angles), 50 + 10 * np.sin(angles)), 1)
        ring = np.vstack((ring, ring[:1]))
        reference = curve_reference([circle_ring((50, 50), 10.0, 3000)], 100, 100)
        self.assertLess(curve_quality([ring], reference)["tangent_max_deg"], 0.1)

    def test_circle_has_no_inflections(self):
        pieces = boundary_pieces([circle_ring((50, 50), 20)], 100, 100)
        self.assertEqual(len(pieces), 1)
        self.assertTrue(pieces[0][2])
        self.assertEqual(count_inflections(pieces[0][0], True), 0)

    def test_sine_wave_counts_each_sign_change(self):
        # Three full periods: six half-waves alternate their bending, five changes.
        x = np.linspace(0.0, 6.0 * math.pi, 2000)
        wave = np.stack((x * 5.0, 20.0 * np.sin(x)), axis=1)
        self.assertEqual(count_inflections(wave, False), 5)

    def test_closed_flower_wraps_its_sign_changes(self):
        angles = np.linspace(0.0, 2.0 * math.pi, 4000, endpoint=False)
        radius = 30.0 + 6.0 * np.cos(5.0 * angles)
        flower = np.stack((50 + radius * np.cos(angles), 50 + radius * np.sin(angles)), axis=1)
        # Five lobes, each concave between two convex stretches: ten inflections,
        # wherever the ring starts.
        self.assertEqual(count_inflections(flower, True), 10)
        self.assertEqual(count_inflections(np.roll(flower, 1234, axis=0), True), 10)

    def test_sub_threshold_jitter_has_no_inflections(self):
        # Headings alternate 0 and 0.5 degrees: every turn is +-0.5 degrees.
        headings = np.radians(np.where(np.arange(200) % 2, 0.5, 0.0))
        points = np.vstack(([0.0, 0.0], np.cumsum(np.stack((np.cos(headings),
                                                            np.sin(headings)), 1), axis=0)))
        self.assertEqual(count_inflections(points, False), 0)

    def test_cusp_turn_is_part_of_its_run(self):
        # A square's four 90-degree turns share one sign: no inflection.
        square = np.array([[10, 10], [40, 10], [40, 40], [10, 40]], dtype=float)
        self.assertEqual(count_inflections(square, True), 0)

    def test_excluded_region_splits_a_ring_into_pieces(self):
        ring = circle_ring((50, 50), 20)
        keep = lambda points: np.abs(points[:, 1] - 50) > 5  # noqa: E731
        pieces = boundary_pieces([ring], 100, 100, keep)
        self.assertEqual(len(pieces), 2)
        self.assertFalse(any(closed for _, _, closed in pieces))

    def test_frame_samples_are_not_boundary(self):
        square = np.array([[0, 0], [100, 0], [100, 50], [0, 50], [0, 0]], dtype=float)
        pieces = boundary_pieces([square], 100, 100)
        # Only the side along y = 50 lies off the frame; its ends touch the frame.
        self.assertEqual(len(pieces), 1)
        self.assertTrue(np.allclose(pieces[0][0][:, 1], 50.0))

    def test_identical_rings_score_zero(self):
        ring = circle_ring((50, 50), 20)
        result = curve_quality([ring], curve_reference([ring], 100, 100))
        self.assertEqual(result["extra_inflections_per_100px"], 0.0)
        self.assertAlmostEqual(result["tangent_p95_deg"], 0.0, places=6)
        self.assertAlmostEqual(result["tangent_max_deg"], 0.0, places=6)

    def test_rotated_segment_tangent_error_equals_the_rotation(self):
        truth = np.array([[20.0, 50.0], [80.0, 50.0]])
        angle = math.radians(3.0)
        centre = np.array([50.0, 50.0])
        rotation = np.array([[math.cos(angle), -math.sin(angle)],
                             [math.sin(angle), math.cos(angle)]])
        traced = (truth - centre) @ rotation.T + centre
        result = curve_quality([traced[::-1]], curve_reference([truth], 100, 100))
        self.assertAlmostEqual(result["tangent_p95_deg"], 3.0, places=6)
        self.assertAlmostEqual(result["tangent_max_deg"], 3.0, places=6)

    def test_extra_inflections_are_per_100px_of_truth(self):
        x = np.linspace(0.0, 2.0 * math.pi, 1000)
        wave = np.stack((10 + x * 10.0, 50 + 5.0 * np.sin(x)), axis=1)
        line = np.array([[10.0, 50.0], [10 + 20 * math.pi, 50.0]])
        result = curve_quality([wave], curve_reference([line], 100, 100))
        self.assertEqual(result["inflections_traced"], 1)
        self.assertEqual(result["inflections_truth"], 0)
        self.assertAlmostEqual(result["extra_inflections_per_100px"],
                               100.0 / (20 * math.pi), places=6)


class NodePlacementTests(unittest.TestCase):
    def test_authored_nodes_are_visible_outline_joins_off_the_canvas_corners(self):
        from vinylgen.fidelity.scene import Region, Scene, rectangle
        from vinylgen.fidelity.truth import scene_truth
        from vinylgen.fidelity.bounds import truth_rings
        scene = Scene(100, 100, PALETTE + [{"name": "Red", "rgb": [200, 0, 0]}], 0,
                      [Region(1, rectangle(0, 0, 60, 60)), Region(2, rectangle(40, 40, 80, 80))])
        every = {tuple(p) for p in authored_nodes(scene).tolist()}
        self.assertNotIn((0.0, 0.0), every)
        self.assertIn((60.0, 60.0), every)
        visible = {tuple(p) for p in authored_nodes(scene, truth_rings(scene_truth(scene)))
                   .tolist()}
        # The first square's corner at (60, 60) lies under the second square.
        self.assertNotIn((60.0, 60.0), visible)
        self.assertIn((60.0, 0.0), visible)
        self.assertIn((80.0, 80.0), visible)

    def test_recall_and_strays(self):
        authored = np.array([[10.0, 10.0], [50.0, 50.0]])
        traced = np.array([[10.3, 10.0], [70.0, 70.0], [71.0, 70.0]])
        result = node_placement(authored, traced, 0.5)
        self.assertEqual((result["recalled"], result["stray"]), (1, 2))
        self.assertAlmostEqual(result["recall"], 0.5)
        self.assertAlmostEqual(result["stray_share"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
