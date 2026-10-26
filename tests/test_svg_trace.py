import json
import math
import re
import tempfile
import unittest
from dataclasses import replace
from unittest import mock
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.contracts import CleanupSettings, SegmentationBundle, TraceSettings
from vinylgen.boundary_graph import (
    shared_boundary_polygons,
)
from vinylgen.cutter_cleanup import cleanup_labels
from vinylgen.curve_fit import ring_path
from vinylgen.svg_writer import SVG
from vinylgen.preview import PREVIEW_MAX_SIDE, preview_plan
from vinylgen import tracing
from vinylgen.tracing import partition_watertight_fault, trace_subject


class PartitionWatertightGateTests(unittest.TestCase):
    def test_floating_point_noise_on_a_large_canvas_is_tolerated(self):
        # The phoenix-4x-regen failure: a geometrically perfect partition on an
        # 89-megapixel canvas whose overlap area is pure GEOS rounding noise.
        # A fixed absolute epsilon false-failed it; a canvas-scaled tolerance
        # must not.
        canvas_area = float(7920 * 11264)
        topology = {"partition_gap_area": 0.0,
                    "overlap_area": 1.3709068298339844e-06,
                    "polygonized_faces": 5168, "boundary_chains": 13668}
        self.assertIsNone(partition_watertight_fault(topology, canvas_area))

    def test_a_real_uncovered_gap_still_fails(self):
        topology = {"partition_gap_area": 5.0, "overlap_area": 0.0,
                    "polygonized_faces": 12, "boundary_chains": 20}
        message = partition_watertight_fault(topology, 400.0)
        self.assertIsNotNone(message)
        self.assertIn("no colour layer", message)
        self.assertIn("px²", message)
        self.assertIn("1.250%", message)

    def test_a_real_overlap_still_fails(self):
        topology = {"partition_gap_area": 0.0, "overlap_area": 2.0,
                    "polygonized_faces": 9, "boundary_chains": 15}
        message = partition_watertight_fault(topology, 400.0)
        self.assertIsNotNone(message)
        self.assertIn("more than one colour layer", message)

    def test_message_is_concise_and_omits_the_per_chain_inventory(self):
        # The raw topology carries a per-chain record for every boundary chain;
        # embedding it is what produced the unreadable frontend error. The gate
        # message must name the scalar fault only.
        topology = {"partition_gap_area": 3.0, "overlap_area": 0.0,
                    "polygonized_faces": 9, "boundary_chains": 15,
                    "chains": [{"id": i, "pair": (0, 1)} for i in range(13668)]}
        message = partition_watertight_fault(topology, 400.0)
        self.assertNotIn("chains", message)
        self.assertNotIn("'pair'", message)
        self.assertLess(len(message), 400)


class SvgTraceTests(unittest.TestCase):
    def test_shared_boundary_construction_preserves_dense_component_evidence(self):
        labels = np.zeros((80, 120), dtype=np.int16)
        for x in range(5, 115):
            y = 35 + int(8 * np.sin(x / 6))
            labels[y:y + 3, x] = 1

        geometry, track, diagnostics = shared_boundary_polygons(
            labels, [0, 1], np.zeros((*labels.shape, 2), dtype=np.float32))

        self.assertFalse(geometry[1].is_empty)
        self.assertGreater(diagnostics["dense_boundary_samples"], 100)
        self.assertTrue(all(chain.points == chain.source_points
                            for chain in track.chains))

    def test_cleanup_thresholds_default_to_zero(self):
        cleanup = CleanupSettings()
        self.assertFalse(cleanup.cleanup)
        self.assertEqual(cleanup.min_detail_width, 0.0)
        self.assertEqual(cleanup.min_region_area, 0.0)
        self.assertEqual(cleanup.remove_below_width, 0.0)
        settings = TraceSettings(length_unit="px")
        self.assertEqual(settings.curve_fit_tolerance, 1.5)
        self.assertEqual(settings.corner_analysis_span, 8.0)

    def test_adjacent_undersized_regions_merge_without_swapping(self):
        labels = np.zeros((5, 7), dtype=np.int16)
        labels[2, 2:4] = 1
        labels[2, 4:6] = 2
        palette = [
            {"rgb": [0, 0, 0], "name": "Black"},
            {"rgb": [230, 100, 20], "name": "Orange"},
            {"rgb": [170, 45, 25], "name": "Red"},
        ]
        cleaned, summary = cleanup_labels(
            labels, [0, 1, 2], palette,
            CleanupSettings(cleanup=True, cleanup_action="remove",
                          min_region_area=3, min_detail_width=0), 1.0)
        self.assertTrue(np.all(cleaned[2, 2:6] == 0))
        self.assertEqual(summary["removed_regions"], 2)
        self.assertEqual(summary["removed_pixels"], 4)

    def test_combined_cleanup_removes_then_widens_with_separate_thresholds(self):
        labels = np.zeros((9, 12), dtype=np.int16)
        labels[1, 1] = 1
        labels[3:8, 5] = 2
        palette = [
            {"rgb": [0, 0, 0], "name": "Black"},
            {"rgb": [230, 100, 20], "name": "Orange"},
            {"rgb": [240, 220, 160], "name": "Cream"},
        ]
        cleaned, summary = cleanup_labels(
            labels, [0, 1, 2], palette,
            CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                          min_region_area=2, min_detail_width=3), 1.0)
        self.assertEqual(cleaned[1, 1], 0)
        self.assertGreater(int((cleaned == 2).sum()), 5)
        self.assertEqual(summary["removed_regions"], 1)
        self.assertGreaterEqual(summary["widened_regions"], 1)

    def test_combined_cleanup_removes_thin_before_widening_survivors(self):
        labels = np.zeros((10, 14), dtype=np.int16)
        labels[1:7, 2] = 1
        labels[1:7, 7:10] = 2
        palette = [
            {"rgb": [0, 0, 0], "name": "Black"},
            {"rgb": [230, 100, 20], "name": "Orange"},
            {"rgb": [240, 220, 160], "name": "Cream"},
        ]
        cleaned, summary = cleanup_labels(
            labels, [0, 1, 2], palette,
            CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                          min_region_area=0, remove_below_width=3,
                          min_detail_width=6), 1.0)
        self.assertFalse(np.any(cleaned == 1))
        self.assertGreater(int((cleaned == 2).sum()), 18)
        self.assertEqual(summary["removed_regions"], 1)

    def test_widen_detects_thin_branch_attached_to_thick_component(self):
        labels = np.zeros((15, 20), dtype=np.int16)
        labels[4:11, 3:10] = 1
        labels[7, 10:17] = 1
        palette = [
            {"rgb": [240, 220, 160], "name": "Cream"},
            {"rgb": [0, 0, 0], "name": "Black"},
        ]

        cleaned, summary = cleanup_labels(
            labels, [0, 1], palette,
            CleanupSettings(cleanup=True, cleanup_action="widen",
                          min_detail_width=2), 1.0)

        self.assertGreater(int((cleaned == 1).sum()), int((labels == 1).sum()))
        self.assertGreater(summary["widened_regions"], 0)
        self.assertEqual(summary["widened_regions"], 1)

    def test_widen_refused_when_it_would_thin_a_two_pixel_neighbour_band(self):
        # A thin source sits directly below a 2px-wide neighbour band whose
        # colour also has a large disconnected blob, so a global colour total
        # stays well above threshold. Widening the source would consume the band
        # and drop that component below the configured minimum width, so the
        # neighbour must be protected by its own residual geometry.
        labels = np.zeros((16, 40), dtype=np.int16)
        labels[7:9, 0:20] = 2
        labels[9, 0:20] = 1
        labels[:, 34:40] = 2
        palette = [
            {"rgb": [0, 0, 0], "name": "Black"},
            {"rgb": [230, 100, 20], "name": "Orange"},
            {"rgb": [200, 90, 30], "name": "Red"},
        ]
        cleaned, summary = cleanup_labels(
            labels, [0, 1, 2], palette,
            CleanupSettings(cleanup=True, cleanup_action="widen",
                          min_detail_width=3), 1.0)
        self.assertTrue(np.all(cleaned[7:9, 0:20] == 2))
        self.assertEqual(int((cleaned == 2).sum()), int((labels == 2).sum()))
        self.assertEqual(summary["widened_regions"], 0)
        self.assertGreaterEqual(summary["unchanged_regions"], 1)
        self.assertTrue(any("constrained by neighbour threshold" in warning
                            for warning in summary["warnings"]))

    def test_shared_curve_fit_smooths_shallow_turns_but_keeps_square_corners(self):
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=0.2,
                                 corner_analysis_span=3.0)
        square = ring_path(
            [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], settings)
        shallow_ring = [(0, 0), (5, 0), (10, 1), (10, 10), (0, 10), (0, 0)]
        self.assertEqual(square.metrics.cubics, 0)
        self.assertNotIn("C", square.path)
        # The 11 degree bend at (5, 0) is no corner: it never becomes a node. With
        # the default straight-line deviation its 1 px bow straightens into one
        # line between the corners at (0, 0) and (10, 1); with a tighter one it is
        # a curve.
        shallow = ring_path(shallow_ring, settings)
        self.assertNotIn("5 0", shallow.path)
        self.assertIn("L10 1", shallow.path)
        curved = ring_path(shallow_ring, replace(settings, straight_line_max_deviation=0.1))
        self.assertGreater(curved.metrics.cubics, 0)
        self.assertIn("C", curved.path)
        nodes = [tuple(map(float, command[1:].split()[-2:]))
                 for command in re.findall(r"[MLC][^MLCZ]*", curved.path)]
        self.assertNotIn((5.0, 0.0), nodes)

    def test_corner_analysis_looks_past_single_pixel_stair_step(self):
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=0.2,
                                 corner_analysis_span=3.0)
        fitted = ring_path(
            [(0, 0), (4, 0), (4, 1), (8, 1), (8, 8), (0, 8), (0, 0)],
            settings)
        self.assertEqual(fitted.metrics.cubics, 0)
        self.assertIn("L8 1", fitted.path)
        self.assertNotIn("L4 0 L4 1 L8 1", fitted.path)

    def fixture(self, root: Path) -> Path:
        subject = root / "fixture"
        subject.mkdir()
        pixels = np.zeros((32, 40, 3), dtype=np.uint8)
        pixels[:] = (15, 35, 60)
        pixels[5:27, 8:32] = (225, 95, 45)
        pixels[11:20, 15:24] = (15, 35, 60)
        pixels[2:5, 35:38] = (225, 95, 45)
        Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
        Image.fromarray(pixels, "RGB").save(subject / "source.png")
        palette = {"dimensions": [40, 32], "palette": [
            {"index": 0, "hex": "#0F233C", "rgb": [15, 35, 60], "share_percent": 60},
            {"index": 1, "hex": "#E15F2D", "rgb": [225, 95, 45], "share_percent": 40}]}
        (subject / "palette.json").write_text(json.dumps(palette))
        labels = np.where(np.all(pixels == (225, 95, 45), axis=2), 1, 0).astype(np.int16)
        costs = np.full((*labels.shape, 2), 50.0, dtype=np.float32)
        np.put_along_axis(costs, labels[..., None], 0.0, axis=2)
        SegmentationBundle(
            labels=labels,
            protected_features=np.zeros(labels.shape, dtype=bool),
            explicit_protected_features=np.zeros(labels.shape, dtype=bool),
            palette_rgb=np.array([[15, 35, 60], [225, 95, 45]], dtype=np.uint8),
            palette_costs=costs,
        ).save(subject / "segmentation.npz")
        return subject

    def _trace_capturing_fit_settings(self, subject: Path, settings: TraceSettings,
                                      cleanup: CleanupSettings | None = None):
        """Trace, returning the report, the settings the curve fitter received, and edge paths."""
        seen: list[TraceSettings] = []
        real = tracing.shared_boundary_components

        def spy(track, included, fit_settings, *args, **kwargs):
            seen.append(fit_settings)
            return real(track, included, fit_settings, *args, **kwargs)

        with mock.patch.object(tracing, "shared_boundary_components", spy):
            report = trace_subject(subject, settings, {}, cleanup=cleanup)
        root = ET.parse(subject / "traced-edge.svg").getroot()
        paths = [path.attrib["d"] for path in root.iter(f"{{{SVG}}}path")]
        return report, seen, paths

    def test_max_deviation_is_in_traced_pixels_whatever_the_physical_size(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            # 400 mm over the 40 px fixture is 10 mm per pixel, so any mm conversion
            # would move the fitting limit by a factor of ten.
            # 1.5 px, not below the 1 px floor every resolved deviation is held at.
            for explicit in (None, 1.5):
                pixels, pixel_fits, pixel_paths = self._trace_capturing_fit_settings(
                    subject, TraceSettings(length_unit="px", max_deviation=explicit))
                physical, physical_fits, physical_paths = self._trace_capturing_fit_settings(
                    subject, TraceSettings(length_unit="px", max_deviation=explicit),
                    cleanup=CleanupSettings(width_mm=400))
                expected = 2.0 if explicit is None else explicit
                self.assertEqual(physical["mode"], "physical")
                for report, fits in ((pixels, pixel_fits), (physical, physical_fits)):
                    self.assertEqual(report["lengths"]["px"]["max_deviation"], expected)
                    # The settings record thousandths of the 40 px fixture's longer side.
                    self.assertEqual(report["settings"]["length_unit"], "per_mille")
                    self.assertAlmostEqual(report["settings"]["max_deviation"], expected * 25.0)
                    self.assertEqual(
                        report["validation"]["semantic"]["settings"]["maximum_deviation_px"], expected)
                    self.assertTrue(fits)
                    self.assertTrue(all(fit.max_deviation == expected for fit in fits))
                # A physical size changes cut dimensions, never the fitted geometry.
                self.assertEqual(pixel_paths, physical_paths)

    def test_layers_components_holes_and_determinism(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            first = trace_subject(subject, TraceSettings(length_unit="px"), {})
            svg_before = (subject / "traced-edge.svg").read_bytes()
            second = trace_subject(subject, TraceSettings(length_unit="px"), {})
            self.assertEqual(svg_before, (subject / "traced-edge.svg").read_bytes())
            overlay = (subject / "trace.html").read_text()
            self.assertIn('<img src="data:image/png;base64,', overlay)
            self.assertIn("<svg", overlay)
            self.assertNotIn("traced-overlap.svg\"", overlay)
            self.assertTrue(first["validation"]["edge"]["passed"])
            self.assertTrue(second["validation"]["overlap"]["passed"])
            self.assertEqual(first["topology"]["partition_gap_area"], 0.0)
            self.assertEqual(first["topology"]["overlap_area"], 0.0)
            root = ET.parse(subject / "traced-edge.svg").getroot()
            self.assertEqual(len(root.findall(f"{{{SVG}}}g")), 2)
            for layer in root.findall(f"{{{SVG}}}g"):
                self.assertEqual(layer.attrib["stroke"], "none")
            orange = next(layer for layer in first["geometry"]["edge"] if layer["hex"] == "#E15F2D")
            self.assertEqual(orange["paths"], 2)
            self.assertEqual(orange["source_colour_components"], 2)
            self.assertGreaterEqual(sum(layer["holes"] for layer in first["geometry"]["edge"]), 1)
            audit = json.loads((subject / "trace.json").read_text())
            expected = preview_plan(subject / "traced-overlap.svg")
            self.assertEqual(audit["preview"], {"file": "traced.png", **{
                key: value for key, value in expected.items() if key != "arguments"}})
            with Image.open(subject / "traced.png") as preview:
                self.assertEqual(preview.size, (expected["width"], expected["height"]))
            self.assertLessEqual(max(preview.size), PREVIEW_MAX_SIDE)

    def test_solid_underlayers_make_lower_layers_solid_without_shared_edges(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            report = trace_subject(subject, TraceSettings(length_unit="px", solid_underlayers=True), {})
            # The edge variant remains the gap-free registration partition, so the
            # bottom layer still carries the shared-edge cutout under the top layer.
            self.assertGreaterEqual(
                sum(layer["holes"] for layer in report["geometry"]["edge"]), 1)
            # In the overlap variant the bottom layer absorbs everything above it and
            # becomes a hole-free solid, so no edge is shared with the top layer.
            background = next(layer for layer in report["geometry"]["overlap"]
                              if layer["hex"] == "#0F233C")
            self.assertEqual(background["holes"], 0)
            self.assertEqual(background["construction"], "solid stacked underlayer")
            self.assertTrue(report["validation"]["overlap"]["passed"])

    def nested_fixture(self, root: Path) -> Path:
        # A blue background, an orange block inside it, and a green square fully
        # enclosed by the orange block. The green square borders only orange, never
        # the background.
        subject = root / "nested"
        subject.mkdir()
        pixels = np.zeros((32, 40, 3), dtype=np.uint8)
        pixels[:] = (15, 35, 60)
        pixels[8:24, 10:30] = (225, 95, 45)
        pixels[13:19, 16:24] = (60, 180, 90)
        Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
        Image.fromarray(pixels, "RGB").save(subject / "source.png")
        palette = {"dimensions": [40, 32], "palette": [
            {"index": 0, "hex": "#0F233C", "rgb": [15, 35, 60], "share_percent": 55},
            {"index": 1, "hex": "#E15F2D", "rgb": [225, 95, 45], "share_percent": 35},
            {"index": 2, "hex": "#3CB45A", "rgb": [60, 180, 90], "share_percent": 10}]}
        (subject / "palette.json").write_text(json.dumps(palette))
        centres = np.array([[15, 35, 60], [225, 95, 45], [60, 180, 90]])
        labels = np.argmin(
            np.sum((pixels.astype(np.int32)[:, :, None, :] - centres[None, None]) ** 2, axis=3),
            axis=2).astype(np.int16)
        costs = np.full((*labels.shape, 3), 50.0, dtype=np.float32)
        np.put_along_axis(costs, labels[..., None], 0.0, axis=2)
        SegmentationBundle(
            labels=labels,
            protected_features=np.zeros(labels.shape, dtype=bool),
            explicit_protected_features=np.zeros(labels.shape, dtype=bool),
            palette_rgb=centres.astype(np.uint8),
            palette_costs=costs,
        ).save(subject / "segmentation.npz")
        return subject

    def test_solid_underlayers_skip_non_adjacent_upper_colours(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.nested_fixture(Path(name))
            report = trace_subject(subject, TraceSettings(length_unit="px", solid_underlayers=True), {})
            overlap = {layer["hex"]: layer for layer in report["geometry"]["overlap"]}
            # The orange middle layer borders the green square, so it backs it into a
            # hole-free solid.
            self.assertEqual(overlap["#E15F2D"]["holes"], 0)
            self.assertEqual(overlap["#E15F2D"]["construction"], "solid stacked underlayer")
            # The background never touches the green square (orange encloses it), so it
            # is not extended beneath it: the square stays a hole instead of stacking an
            # identical fourth-of-the-same-shape layer.
            self.assertGreaterEqual(overlap["#0F233C"]["holes"], 1)
            self.assertTrue(report["validation"]["overlap"]["passed"])

    def palette_first_fixture(self, root: Path) -> Path:
        """The nested fixture with its palette listed orange, green, navy: palette order
        then differs from the inferred order, which puts the canvas-touching navy lowest."""
        root.mkdir(parents=True, exist_ok=True)
        subject = self.nested_fixture(root)
        permutation = [1, 2, 0]
        report = json.loads((subject / "palette.json").read_text())
        report["palette"] = [dict(report["palette"][old], index=new)
                             for new, old in enumerate(permutation)]
        (subject / "palette.json").write_text(json.dumps(report))
        bundle = SegmentationBundle.load(subject / "segmentation.npz")
        remap = np.argsort(permutation).astype(np.int16)
        SegmentationBundle(
            labels=remap[bundle.labels],
            protected_features=bundle.protected_features,
            explicit_protected_features=bundle.explicit_protected_features,
            palette_rgb=bundle.palette_rgb[permutation],
            palette_costs=bundle.palette_costs[..., permutation],
        ).save(subject / "segmentation.npz")
        return subject

    def layer_shapes(self, svg_path: Path) -> tuple[list[str], dict[str, list[str]]]:
        """The SVG's layer fills bottom to top, and each fill's sorted path data."""
        layers = ET.parse(svg_path).getroot().findall(f"{{{SVG}}}g")
        return ([layer.attrib["fill"] for layer in layers],
                {layer.attrib["fill"]: sorted(path.attrib["d"]
                                              for path in layer.iter(f"{{{SVG}}}path"))
                 for layer in layers})

    def test_edge_to_edge_trace_skips_order_inference_with_identical_geometry(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            root = Path(name)
            skipped = self.palette_first_fixture(root / "skipped")
            with mock.patch.object(tracing, "infer_order",
                                   side_effect=AssertionError("infer_order ran")):
                report = trace_subject(skipped, TraceSettings(length_unit="px"), {})
            order = report["layer_order"]
            self.assertIsNone(order["inferred_bottom_to_top"])
            self.assertEqual(order["not_inferred"], tracing.ORDER_NOT_INFERRED)
            self.assertEqual(order["warnings"], [])
            names = {row["hex"]: row["name"] for row in report["colours"]["included"]}
            self.assertEqual(order["effective_bottom_to_top"],
                             [names[value] for value in ("#E15F2D", "#3CB45A", "#0F233C")])

            # The same subject traced in the order inference would have chosen.
            inferred_subject = self.palette_first_fixture(root / "inferred")
            palette = json.loads((inferred_subject / "palette.json").read_text())["palette"]
            labels = SegmentationBundle.load(inferred_subject / "segmentation.npz").labels
            inferred, _ = tracing.infer_order(labels, [0, 1, 2], palette)
            inferred_hex = [palette[index]["hex"] for index in inferred]
            self.assertEqual(inferred_hex, ["#0F233C", "#E15F2D", "#3CB45A"])
            trace_subject(inferred_subject, TraceSettings(length_unit="px", layer_order=inferred_hex), {})
            for variant in ("edge", "overlap"):
                skipped_fills, skipped_shapes = self.layer_shapes(skipped / f"traced-{variant}.svg")
                inferred_fills, inferred_shapes = self.layer_shapes(
                    inferred_subject / f"traced-{variant}.svg")
                self.assertEqual(skipped_fills, ["#E15F2D", "#3CB45A", "#0F233C"])
                self.assertEqual(inferred_fills, inferred_hex)
                # Only the stacking differs: every colour's shapes are the same.
                self.assertEqual(skipped_shapes, inferred_shapes, variant)

    def test_edge_to_edge_trace_honours_an_explicit_layer_order(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.palette_first_fixture(Path(name))
            with mock.patch.object(tracing, "infer_order",
                                   side_effect=AssertionError("infer_order ran")):
                report = trace_subject(subject, TraceSettings(length_unit="px", layer_order=["#0F233C"]), {})
            names = {row["hex"]: row["name"] for row in report["colours"]["included"]}
            # The named colour first, then the rest in palette order.
            self.assertEqual(report["layer_order"]["effective_bottom_to_top"],
                             [names[value] for value in ("#0F233C", "#E15F2D", "#3CB45A")])
            self.assertEqual(report["layer_order"]["override"], [names["#0F233C"]])
            self.assertEqual(self.layer_shapes(subject / "traced-edge.svg")[0],
                             ["#0F233C", "#E15F2D", "#3CB45A"])

    def test_order_is_inferred_when_underlayers_or_overlap_make_it_matter(self):
        for settings in (TraceSettings(length_unit="px", solid_underlayers=True), TraceSettings(length_unit="px", overlap=1.0)):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory(
                    dir=Path(__file__).parents[1] / ".tmp") as name:
                subject = self.palette_first_fixture(Path(name))
                with mock.patch.object(tracing, "infer_order",
                                       wraps=tracing.infer_order) as spy:
                    report = trace_subject(subject, settings, {})
                spy.assert_called_once()
                names = {row["hex"]: row["name"] for row in report["colours"]["included"]}
                inferred = [names[value] for value in ("#0F233C", "#E15F2D", "#3CB45A")]
                self.assertEqual(report["layer_order"]["inferred_bottom_to_top"], inferred)
                self.assertIsNone(report["layer_order"]["not_inferred"])
                self.assertEqual(report["layer_order"]["effective_bottom_to_top"], inferred)
                self.assertEqual(self.layer_shapes(subject / "traced-overlap.svg")[0],
                                 ["#0F233C", "#E15F2D", "#3CB45A"])

    def drawing_commands(self, svg_path: Path) -> list[str]:
        # Every line/curve command in the file; the M start is excluded because a ring's
        # start vertex depends on assembly order, not on the fitted geometry.
        tree = ET.parse(svg_path)
        commands: list[str] = []
        for path in tree.iter(f"{{{SVG}}}path"):
            commands.extend(token.strip()
                            for token in re.findall(r"[LC][^MLCZ]*", path.attrib["d"]))
        return commands

    def test_solid_underlayers_reuse_shared_fits_without_reoptimising(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.nested_fixture(Path(name))
            trace_subject(subject, TraceSettings(length_unit="px", solid_underlayers=True), {})
            edge = set(self.drawing_commands(subject / "traced-edge.svg"))
            overlap = self.drawing_commands(subject / "traced-overlap.svg")
            self.assertTrue(overlap, "expected the solid underlayers to emit fitted geometry")
            # Each solid-underlayer edge is a canonical shared-chain fit reused verbatim, so
            # it registers against its neighbours. A re-optimised union would introduce
            # curves absent from the gap-free edge variant and reopen the gaps.
            introduced = sorted(command for command in overlap if command not in edge)
            self.assertEqual(introduced, [],
                             f"solid underlayers re-optimised {len(introduced)} segment(s)")

    def test_trace_reports_monotonic_progress(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            seen: list[tuple[float, str]] = []
            trace_subject(subject, TraceSettings(length_unit="px"), {},
                          progress=lambda fraction, message: seen.append((fraction, message)))
            self.assertTrue(seen, "expected trace to report progress")
            fractions = [fraction for fraction, _ in seen]
            # The bar only ever advances: fitting, per-layer assembly, render, and
            # write each report a higher fraction than the stage before it.
            self.assertEqual(fractions, sorted(fractions))
            self.assertLessEqual(fractions[0], 0.1)
            self.assertEqual(fractions[-1], 1.0)
            self.assertTrue(all(message for _, message in seen))
            self.assertTrue(any("layer 1 of" in message.lower() for _, message in seen))

    def test_optional_colour_removal(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            report = trace_subject(subject, TraceSettings(length_unit="px"), {},
                                   cleanup=CleanupSettings(remove_colour="#E15F2D"))
            self.assertEqual(report["colours"]["omitted"]["hex"], "#E15F2D")
            self.assertEqual(report["validation"]["edge"]["layer_count"], 1)


class InspectionOverlayTests(unittest.TestCase):
    """The transparent audit overlays: only their marks are visible, on the traced grid."""

    WIDTH, HEIGHT = 40, 32

    def fixture(self, root: Path) -> Path:
        # A dark source line the flattened raster lost (a semantic miss) and a
        # one-pixel explicitly protected spike that a coarse fit tightens.
        subject = root / "fixture"
        subject.mkdir()
        pixels = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
        pixels[:] = (15, 35, 60)
        pixels[5:27, 8:32] = (225, 95, 45)
        pixels[11:20, 15:24] = (15, 35, 60)
        pixels[2:5, 35:38] = (225, 95, 45)
        pixels[27:31, 20:21] = (225, 95, 45)
        Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
        source = pixels.copy()
        source[22:24, 9:31] = (5, 5, 5)
        Image.fromarray(source, "RGB").save(subject / "source.png")
        palette = {"dimensions": [self.WIDTH, self.HEIGHT], "palette": [
            {"index": 0, "hex": "#0F233C", "rgb": [15, 35, 60], "share_percent": 60},
            {"index": 1, "hex": "#E15F2D", "rgb": [225, 95, 45], "share_percent": 40}]}
        (subject / "palette.json").write_text(json.dumps(palette))
        labels = np.where(np.all(pixels == (225, 95, 45), axis=2), 1, 0).astype(np.int16)
        costs = np.full((*labels.shape, 2), 50.0, dtype=np.float32)
        np.put_along_axis(costs, labels[..., None], 0.0, axis=2)
        protected = np.zeros(labels.shape, dtype=bool)
        protected[27:31, 20:21] = True
        SegmentationBundle(
            labels=labels, protected_features=protected, explicit_protected_features=protected,
            palette_rgb=np.array([[15, 35, 60], [225, 95, 45]], dtype=np.uint8),
            palette_costs=costs,
        ).save(subject / "segmentation.npz")
        return subject

    def rgba(self, path: Path) -> np.ndarray:
        with Image.open(path) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.size, (self.WIDTH, self.HEIGHT))
            return np.asarray(image)

    def test_missed_details_overlay_marks_only_the_missed_pixels(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            report = trace_subject(subject, TraceSettings(length_unit="px", max_deviation=2.0), {})
            self.assertFalse(report["validation"]["semantic"]["passed"])
            overlay = self.rgba(subject / "trace-missed-details-overlay.png")
            with Image.open(subject / "trace-semantic-warnings.png") as opaque:
                marked = np.all(np.asarray(opaque.convert("RGB")) == (220, 35, 35), axis=2)
            visible = overlay[..., 3] > 0
            self.assertTrue(visible.any())
            np.testing.assert_array_equal(visible, marked)
            self.assertTrue(np.all(overlay[visible] == tracing.MISSED_DETAIL_RGBA))
            self.assertTrue(np.all(overlay[~visible] == 0))

    def test_tightened_boundaries_overlay_marks_only_the_fallback_chains(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            report = trace_subject(subject, TraceSettings(length_unit="px", max_deviation=50.0), {})
            self.assertTrue(any("dense fallback" in warning for warning in report["warnings"]))
            self.assertTrue((subject / "trace-fallbacks.png").exists())
            overlay = self.rgba(subject / "trace-fallbacks-overlay.png")
            visible = overlay[..., 3] > 0
            self.assertTrue(visible.any())
            self.assertFalse(visible.all())
            self.assertTrue(np.all(overlay[~visible] == 0))
            colours = {tuple(int(v) for v in pixel) for pixel in overlay[visible]}
            self.assertIn(tracing.TIGHTENED_BOUNDARY_RGBA, colours)
            self.assertLessEqual(colours, {tracing.TIGHTENED_BOUNDARY_RGBA,
                                           tracing.TIGHTENED_BOUNDARY_HALO_RGBA})

    def test_overlays_are_absent_without_warnings_and_removed_when_a_rerun_clears_them(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            trace_subject(subject, TraceSettings(length_unit="px", max_deviation=50.0), {})
            self.assertTrue((subject / "trace-fallbacks-overlay.png").exists())
            self.assertTrue((subject / "trace-missed-details-overlay.png").exists())
            report = trace_subject(subject, TraceSettings(length_unit="px", max_deviation=6.0), {})
            self.assertEqual(report["warnings"], [])
            for name in ("trace-fallbacks.png", "trace-fallbacks-overlay.png",
                         "trace-semantic-warnings.png", "trace-missed-details-overlay.png"):
                self.assertFalse((subject / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
