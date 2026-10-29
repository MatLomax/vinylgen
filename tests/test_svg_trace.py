import json
import math
import re
import tempfile
import unittest
from dataclasses import replace
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
            self.assertFalse((subject / "traced-overlap.svg").exists())
            self.assertTrue(first["validation"]["edge"]["passed"])
            self.assertTrue(second["validation"]["edge"]["passed"])
            self.assertEqual(first["determinism"]["map_sha256"], second["determinism"]["map_sha256"])
            root = ET.parse(subject / "traced-edge.svg").getroot()
            self.assertEqual(len(root.findall(f"{{{SVG}}}g")), 2)
            for layer in root.findall(f"{{{SVG}}}g"):
                self.assertEqual(layer.attrib["stroke"], "none")
            orange = next(layer for layer in first["geometry"]["edge"] if layer["hex"] == "#E15F2D")
            self.assertEqual(orange["paths"], 2)
            self.assertEqual(orange["source_colour_components"], 2)
            self.assertGreaterEqual(sum(layer["holes"] for layer in first["geometry"]["edge"]), 1)
            audit = json.loads((subject / "trace.json").read_text())
            expected = preview_plan(subject / "traced-edge.svg")
            self.assertEqual(audit["preview"], {"file": "traced.png", **{
                key: value for key, value in expected.items() if key != "arguments"}})
            with Image.open(subject / "traced.png") as preview:
                self.assertEqual(preview.size, (expected["width"], expected["height"]))
            self.assertLessEqual(max(preview.size), PREVIEW_MAX_SIDE)

    def test_trace_reports_monotonic_progress(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            seen: list[tuple[float, str]] = []
            trace_subject(subject, TraceSettings(length_unit="px"), {},
                          progress=lambda fraction, message: seen.append((fraction, message)))
            self.assertTrue(seen, "expected trace to report progress")
            fractions = [fraction for fraction, _ in seen]
            # The bar only ever advances: the engine's chain ticks, the map and SVG
            # writes, the checks, the render and the write each report a higher fraction.
            self.assertEqual(fractions, sorted(fractions))
            self.assertLessEqual(fractions[0], 0.1)
            self.assertEqual(fractions[-1], 1.0)
            self.assertTrue(all(message for _, message in seen))
            self.assertTrue(any(message.startswith("Tracing: chains") for _, message in seen))

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

    def fixture(self, root: Path, *, details: bool = True, name: str = "fixture") -> Path:
        # With details: a dark source line the flattened raster lost (a semantic miss)
        # and a one-pixel explicitly protected spike that a coarse fit tightens.
        subject = root / name
        subject.mkdir()
        pixels = np.zeros((self.HEIGHT, self.WIDTH, 3), dtype=np.uint8)
        pixels[:] = (15, 35, 60)
        pixels[5:27, 8:32] = (225, 95, 45)
        pixels[11:20, 15:24] = (15, 35, 60)
        pixels[2:5, 35:38] = (225, 95, 45)
        if details:
            pixels[27:31, 20:21] = (225, 95, 45)
        Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
        source = pixels.copy()
        if details:
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
        if details:
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

    def test_overlays_are_absent_without_warnings_and_removed_when_a_rerun_clears_them(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            # Files an earlier fitter run left behind; this trace writes no fallbacks.
            for stale in ("trace-fallbacks.png", "trace-fallbacks-overlay.png"):
                Image.new("RGBA", (self.WIDTH, self.HEIGHT)).save(subject / stale)
            trace_subject(subject, TraceSettings(length_unit="px"), {})
            self.assertTrue((subject / "trace-missed-details-overlay.png").exists())
            self.assertFalse((subject / "trace-fallbacks-overlay.png").exists())
            # The same subject re-cleaned without the lost line and the spike: nothing is missed.
            clean = self.fixture(Path(name), details=False, name="clean")
            for input_name in ("flattened.png", "source.png", "segmentation.npz"):
                (subject / input_name).write_bytes((clean / input_name).read_bytes())
            report = trace_subject(subject, TraceSettings(length_unit="px"), {})
            self.assertEqual(report["warnings"], [])
            for name in ("trace-fallbacks.png", "trace-fallbacks-overlay.png",
                         "trace-semantic-warnings.png", "trace-missed-details-overlay.png"):
                self.assertFalse((subject / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
