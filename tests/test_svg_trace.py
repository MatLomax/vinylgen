import json
import math
import re
import tempfile
import unittest
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
        settings = TraceSettings()
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
        settings = TraceSettings(corner_threshold=math.degrees(1.0), curve_fit_tolerance=0.2,
                                 corner_analysis_span=3.0)
        square = ring_path(
            [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)], settings)
        shallow = ring_path(
            [(0, 0), (5, 0), (10, 1), (10, 10), (0, 10), (0, 0)], settings)
        self.assertEqual(square.metrics.cubics, 0)
        self.assertNotIn("C", square.path)
        self.assertGreater(shallow.metrics.cubics, 0)
        self.assertIn("C", shallow.path)

    def test_corner_analysis_looks_past_single_pixel_stair_step(self):
        settings = TraceSettings(corner_threshold=math.degrees(1.0), curve_fit_tolerance=0.2,
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
            for explicit in (None, .5):
                pixels, pixel_fits, pixel_paths = self._trace_capturing_fit_settings(
                    subject, TraceSettings(max_deviation=explicit))
                physical, physical_fits, physical_paths = self._trace_capturing_fit_settings(
                    subject, TraceSettings(max_deviation=explicit),
                    cleanup=CleanupSettings(width_mm=400))
                expected = 2.0 if explicit is None else explicit
                self.assertEqual(physical["mode"], "physical")
                for report, fits in ((pixels, pixel_fits), (physical, physical_fits)):
                    self.assertEqual(report["settings"]["max_deviation"], expected)
                    self.assertEqual(
                        report["validation"]["semantic"]["settings"]["maximum_deviation_px"], expected)
                    self.assertTrue(fits)
                    self.assertTrue(all(fit.max_deviation == expected for fit in fits))
                # A physical size changes cut dimensions, never the fitted geometry.
                self.assertEqual(pixel_paths, physical_paths)

    def test_layers_components_holes_and_determinism(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            subject = self.fixture(Path(name))
            first = trace_subject(subject, TraceSettings(), {})
            svg_before = (subject / "traced-edge.svg").read_bytes()
            second = trace_subject(subject, TraceSettings(), {})
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
            report = trace_subject(subject, TraceSettings(solid_underlayers=True), {})
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
            report = trace_subject(subject, TraceSettings(solid_underlayers=True), {})
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
            trace_subject(subject, TraceSettings(solid_underlayers=True), {})
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
            trace_subject(subject, TraceSettings(), {},
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
            report = trace_subject(subject, TraceSettings(), {},
                                   cleanup=CleanupSettings(remove_colour="#E15F2D"))
            self.assertEqual(report["colours"]["omitted"]["hex"], "#E15F2D")
            self.assertEqual(report["validation"]["edge"]["layer_count"], 1)


if __name__ == "__main__":
    unittest.main()
