import json
import math
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from vinylgen.boundary_graph import shared_boundary_polygons
from vinylgen.contracts import SegmentationBundle, TraceSettings
from vinylgen.trace_window import (PreviewSegmentation, Window, preview_trace_settings,
                                   render_window_svg, window_boundaries)
from vinylgen.tracing import trace_subject

from vinylgen import node_types
from vinylgen.node_types import (ANGLE_TOLERANCE_DEG, LENGTH_TOLERANCE, classify_join,
                                 cusp_anchor_keys, junction_keys, parse_path, path_nodetypes,
                                 point_key,
                                 subpath_nodetypes)
from vinylgen.svg_writer import SODIPODI, SVG, add_layer, svg_document


def rotated(length: float, degrees: float, origin=(0.0, 0.0)):
    angle = math.radians(degrees)
    return (origin[0] + length * math.cos(angle), origin[1] + length * math.sin(angle))


class ClassifyJoinTests(unittest.TestCase):
    def test_line_line_is_always_cusp_even_when_collinear(self):
        self.assertEqual(classify_join((0, 0), ("L", (1, 0)), ("L", (2, 0))), "c")

    def test_line_then_collinear_curve_is_symmetric(self):
        # The curve's start handle continues the line's direction.
        self.assertEqual(
            classify_join((0, 0), ("L", (1, 0)), ("C", (2, 0), (3, 1), (3, 2))), "s")

    def test_curve_then_collinear_line_is_symmetric(self):
        self.assertEqual(
            classify_join((-3, 2), ("C", (-3, 1), (-2, 0), (-1, 0)), ("L", (4, 0))), "s")

    def test_line_then_non_collinear_curve_is_cusp(self):
        self.assertEqual(
            classify_join((0, 0), ("L", (1, 0)), ("C", (2, 1), (3, 1), (3, 2))), "c")

    def test_line_and_collinear_curve_is_never_smooth_even_with_equal_lengths(self):
        # A line has no handle, so its node cannot be balanced.
        self.assertEqual(
            classify_join((0, 0), ("L", (1, 0)), ("C", (1, 0), (1, 0), (2, 0))), "s")

    def test_curves_with_collinear_unequal_handles_are_symmetric(self):
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-1, 0), (0, 0)),
                          ("C", (3, 0), (4, 1), (4, 2))), "s")

    def test_curves_with_collinear_equal_handles_are_smooth(self):
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-2, 0), (0, 0)),
                          ("C", (2, 0), (4, 1), (4, 2))), "z")

    def test_curves_with_a_corner_are_cusp(self):
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-2, 0), (0, 0)),
                          ("C", (2, 1), (4, 1), (4, 2))), "c")

    def test_curves_doubling_back_are_cusp(self):
        self.assertEqual(
            classify_join((-4, 0), ("C", (-3, 0), (-1, 0), (0, 0)),
                          ("C", (-1, 0.001), (-3, 1), (-4, 1))), "c")

    def test_junction_anchor_is_always_cusp(self):
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-2, 0), (0, 0)),
                          ("C", (2, 0), (4, 1), (4, 2)), junction=True), "c")

    def test_angle_tolerance_edges(self):
        inside = ANGLE_TOLERANCE_DEG * 0.98
        outside = ANGLE_TOLERANCE_DEG * 1.02
        previous = ("C", (-4, 2), (-2, 0), (0, 0))
        for turn, expected in ((inside, "z"), (-inside, "z"), (outside, "c"), (-outside, "c")):
            handle = rotated(2.0, turn)
            with self.subTest(turn=turn):
                self.assertEqual(
                    classify_join((-4, 4), previous, ("C", handle, (4, 1), (4, 2))), expected)

    def test_length_tolerance_edges(self):
        previous = ("C", (-4, 2), (-2, 0), (0, 0))  # arriving handle length 2
        for leaving, expected in ((2 * (1 + LENGTH_TOLERANCE * 0.98), "z"),
                                  (2 * (1 - LENGTH_TOLERANCE * 0.98), "z"),
                                  (2 / (1 - LENGTH_TOLERANCE * 1.02), "s"),
                                  (2 * (1 - LENGTH_TOLERANCE * 1.02), "s")):
            with self.subTest(leaving=leaving):
                self.assertEqual(
                    classify_join((-4, 4), previous, ("C", (leaving, 0), (4, 1), (4, 2))),
                    expected)

    def test_zero_length_handle_falls_back_to_the_next_control_point(self):
        # The leaving handle sits on the node, so its direction is toward c2 (on-axis):
        # collinear, but a zero handle cannot balance a 2-long one.
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-2, 0), (0, 0)),
                          ("C", (0, 0), (3, 0), (4, 2))), "s")
        # Off-axis c2 makes the fallback direction a corner.
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-2, 0), (0, 0)),
                          ("C", (0, 0), (3, 3), (4, 2))), "c")

    def test_fully_degenerate_handles_fall_back_to_the_end_point(self):
        # Both handles of the leaving cubic sit on the node: its direction is its end.
        self.assertEqual(
            classify_join((-4, 2), ("C", (-4, 0), (-2, 0), (0, 0)),
                          ("C", (0, 0), (0, 0), (5, 0))), "s")

    def test_both_zero_handles_collinear_are_symmetric_not_smooth(self):
        self.assertEqual(
            classify_join((-4, 0), ("C", (-4, 0), (0, 0), (0, 0)),
                          ("C", (0, 0), (5, 0), (5, 0))), "s")


class SubpathTests(unittest.TestCase):
    def test_closed_ring_repeats_start_type_classified_from_the_closing_join(self):
        # A circle-like ring of four smooth cubics: every node is smooth, the start
        # node's type is written first and repeated last.
        k = 0.5523
        segments = [("C", (1, k), (k, 1), (0, 1)), ("C", (-k, 1), (-1, k), (-1, 0)),
                    ("C", (-1, -k), (-k, -1), (0, -1)), ("C", (k, -1), (1, -k), (1, 0))]
        self.assertEqual(subpath_nodetypes((1, 0), segments, True), "zzzzz")

    def test_closed_ring_start_node_is_a_cusp_when_the_closing_join_is(self):
        segments = [("L", (4, 0)), ("C", (5, 0), (6, 1), (6, 2)), ("L", (0, 0))]
        # start: L then L -> c; (4,0): L then collinear C -> s; (6,2): C then non-collinear L -> c
        self.assertEqual(subpath_nodetypes((0, 0), segments, True), "cscc")

    def test_closing_line_is_an_extra_segment_when_the_path_does_not_return(self):
        segments = [("L", (4, 0)), ("L", (4, 4))]
        self.assertEqual(subpath_nodetypes((0, 0), segments, True), "cccc")

    def test_open_subpath_ends_are_cusps(self):
        self.assertEqual(subpath_nodetypes((0, 0), [("L", (1, 0)), ("C", (2, 0), (3, 0), (4, 0))],
                                           False), "csc")

    def test_junction_start_node_is_cusp(self):
        k = 0.5523
        segments = [("C", (1, k), (k, 1), (0, 1)), ("C", (-k, 1), (-1, k), (-1, 0)),
                    ("C", (-1, -k), (-k, -1), (0, -1)), ("C", (k, -1), (1, -k), (1, 0))]
        self.assertEqual(subpath_nodetypes((1, 0), segments, True, {point_key((1, 0))}), "czzzc")
        self.assertEqual(subpath_nodetypes((1, 0), segments, True, {point_key((0, 1))}), "zczzz")


class PathDataTests(unittest.TestCase):
    def test_parses_traced_path_data_and_concatenates_subpaths(self):
        d = "M0 0 L4 0 L4 4 L0 0 Z M1 1 C2 1 3 2 3 3 L1 1 Z"
        self.assertEqual([(start, len(segments), closed) for start, segments, closed in parse_path(d)],
                         [((0.0, 0.0), 3, True), ((1.0, 1.0), 2, True)])
        self.assertEqual(path_nodetypes(d), "cccc" + "ccc")

    def test_junctions_match_the_written_point_text(self):
        d = "M0 0 C0 1 1 2 2 2 C3 2 4 1 4 0 L0 0 Z"
        self.assertEqual(path_nodetypes(d), "czcc")
        self.assertEqual(path_nodetypes(d, {point_key((2.0, 2.0))}), "cccc")

    def test_rejects_commands_traced_output_never_writes(self):
        for d in ("M0 0 Q1 1 2 2 Z", "m0 0 l1 1 z", "M0 0 A1 1 0 0 1 2 2 Z"):
            with self.subTest(d=d), self.assertRaises(ValueError):
                path_nodetypes(d)


class WriterTests(unittest.TestCase):
    def test_every_path_carries_nodetypes_in_the_sodipodi_namespace(self):
        root = svg_document(10, 10, None)
        components = [{"d": "M0 0 L4 0 C5 0 6 1 6 2 L0 0 Z", "nodes": 3, "holes": 0,
                       "fit_metrics": {name: 0 for name in (
                           "lines", "cubics", "hard_anchors", "source_samples",
                           "final_nodes", "node_reduction")}}]
        add_layer(root, {"index": 0, "name": "Red", "hex": "#FF0000"}, components, "",
                  "subject", "edge", 0.0, {point_key((4.0, 0.0))})
        text = ET.tostring(root, encoding="unicode")
        self.assertIn('xmlns:sodipodi="http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"', text)
        path = ET.fromstring(text).find(f".//{{{SVG}}}path")
        self.assertEqual(path.get(f"{{{SODIPODI}}}nodetypes"), "cccc")


SCRATCH = Path(__file__).resolve().parents[1] / ".tmp"


def junction_subject(root: Path) -> Path:
    """An orange block with a green disc across its edge: curves, and junctions where
    the background, the block and the disc meet."""
    subject = root / "junctions"
    subject.mkdir()
    height, width = 60, 80
    yy, xx = np.mgrid[0:height, 0:width]
    labels = np.zeros((height, width), dtype=np.int16)
    labels[10:50, 12:46] = 1
    labels[(xx - 46) ** 2 + (yy - 30) ** 2 <= 14 ** 2] = 2
    centres = np.array([[15, 35, 60], [225, 95, 45], [60, 180, 90]], dtype=np.uint8)
    pixels = centres[labels]
    Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
    Image.fromarray(pixels, "RGB").save(subject / "source.png")
    shares = [float(np.mean(labels == index) * 100) for index in range(3)]
    palette = {"dimensions": [width, height], "palette": [
        {"index": index, "hex": "#%02X%02X%02X" % tuple(int(v) for v in centres[index]),
         "rgb": [int(v) for v in centres[index]], "share_percent": shares[index]}
        for index in range(3)]}
    (subject / "palette.json").write_text(json.dumps(palette))
    rng = np.random.default_rng(3)
    costs = rng.uniform(20.0, 60.0, size=(height, width, 3)).astype(np.float32)
    np.put_along_axis(costs, labels[..., None], 0.0, axis=2)
    SegmentationBundle(
        labels=labels,
        protected_features=np.zeros(labels.shape, dtype=bool),
        explicit_protected_features=np.zeros(labels.shape, dtype=bool),
        palette_rgb=centres, palette_costs=costs,
    ).save(subject / "segmentation.npz")
    return subject


def inkscape_type_count(d: str) -> int:
    """Characters Inkscape expects for traced path data, counted independently of
    ``node_types``: per subpath its segments plus the start node, plus the repeated
    start character when closed, less one when the last segment returns to the start."""
    total = 0
    for body in re.findall(r"M([^M]*)", d):
        closed = body.rstrip().endswith("Z")
        numbers = re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", body)
        segments = len(re.findall(r"[LC]", body))
        returns = numbers[:2] == numbers[-2:]
        nodes = segments + 1 - (1 if closed and returns else 0)
        # Not returning, the implicit closing line is one more segment, but it ends on
        # the start node, so the node count is unchanged.
        total += nodes + (1 if closed else 0)
    return total


class RealTraceNodeTypeTests(unittest.TestCase):
    def test_every_traced_and_previewed_path_has_one_type_per_inkscape_node(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            subject = junction_subject(Path(name))
            segmentation = SegmentationBundle.load(subject / "segmentation.npz")
            _, track, _ = shared_boundary_polygons(
                segmentation.labels, [0, 1, 2], segmentation.palette_costs,
                protected_features=segmentation.protected_features)
            self.assertTrue(junction_keys(track), "the fixture must have three-colour junctions")
            # Junctions and image-edge endpoints are the anchors always written as cusps.
            junctions = cusp_anchor_keys(track)
            edge_ends = {point_key(anchor.point) for chain in track.chains
                         for anchor in chain.anchors if anchor.kind == "endpoint"}
            self.assertTrue(edge_ends, "the fixture must have boundaries meeting the image edge")
            self.assertEqual(junctions, junction_keys(track) | edge_ends)
            seen_types: set[str] = set()
            junction_nodes = 0
            for settings in ({}, {"overlap": 1.5}, {"solid_underlayers": True}):
                trace_subject(subject, TraceSettings(length_unit="px", **settings), {})
                for variant in ("edge", "overlap"):
                    root = ET.parse(subject / f"traced-{variant}.svg").getroot()
                    paths = list(root.iter(f"{{{SVG}}}path"))
                    self.assertTrue(paths)
                    for path in paths:
                        with self.subTest(settings=settings, variant=variant, path=path.get("id")):
                            types = path.get(f"{{{SODIPODI}}}nodetypes")
                            self.assertIsNotNone(types)
                            self.assertRegex(types, r"^[csz]+$")
                            self.assertEqual(len(types), inkscape_type_count(path.get("d")))
                            seen_types.update(types)
                            # The written types are the classification with the track's
                            # real junction anchors.
                            self.assertEqual(types, path_nodetypes(path.get("d"), junctions))
                            # And every node the file writes at a junction anchor is a cusp,
                            # read from the written string itself (a closed subpath's start
                            # takes its repeated last character, as Inkscape reads it).
                            cursor = 0
                            for start, segments, closed in parse_path(path.get("d")):
                                nodes = [start, *(segment[-1] for segment in segments)]
                                if closed and nodes[-1] == start:
                                    nodes.pop()
                                width = len(nodes) + (1 if closed else 0)
                                written = types[cursor:cursor + width]
                                cursor += width
                                for position, node in enumerate(nodes):
                                    if point_key(node) in junctions:
                                        junction_nodes += 1
                                        char = written[-1] if closed and position == 0 else written[position]
                                        self.assertEqual(char, "c")
            self.assertIn("c", seen_types)
            self.assertTrue(junction_nodes, "the traced paths must pass through junction anchors")

            # With the geometric tolerances opened wide, every join with a curve on
            # either side would be symmetric or smooth, so only the junction rule can
            # make those junction nodes cusps: an empty or wrong junction set reaches
            # the writer and this goes red, whatever the fitter does at junctions.
            with mock.patch.object(node_types, "ANGLE_TOLERANCE_DEG", 180.0), \
                    mock.patch.object(node_types, "LENGTH_TOLERANCE", float("inf")):
                trace_subject(subject, TraceSettings(length_unit="px"), {})
                exercised = 0
                for variant in ("edge", "overlap"):
                    root = ET.parse(subject / f"traced-{variant}.svg").getroot()
                    for path in root.iter(f"{{{SVG}}}path"):
                        written = path.get(f"{{{SODIPODI}}}nodetypes")
                        self.assertEqual(written, path_nodetypes(path.get("d"), junctions))
                        without = path_nodetypes(path.get("d"))
                        exercised += sum(a == "c" and b != "c" for a, b in zip(written, without))
                self.assertTrue(exercised, "no junction node relied on the junction rule")
                # The live preview of the whole image writes the same types as the trace,
                # its junction rule included.
                traced = ET.parse(subject / "traced-edge.svg").getroot()
                palette = json.loads((subject / "palette.json").read_text())["palette"]
                preview_segmentation = PreviewSegmentation.load(subject / "segmentation.npz")
                window = Window(0, 0, preview_segmentation.width, preview_segmentation.height)
                boundaries = window_boundaries(preview_segmentation, [0, 1, 2], window)
                settings = preview_trace_settings({"length_unit": "px"}, preview_segmentation.width,
                                                  preview_segmentation.height)
                order = preview_segmentation.automatic_order([0, 1, 2], palette, settings)
                preview = ET.fromstring(render_window_svg(boundaries, window, order, palette, settings, 0.0))
                key = lambda root: sorted((path.get("d"), path.get(f"{{{SODIPODI}}}nodetypes"))
                                          for path in root.iter(f"{{{SVG}}}path"))
                self.assertEqual(key(preview), key(traced))


if __name__ == "__main__":
    unittest.main()
