import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.contracts import SegmentationBundle, TraceSettings
from vinylgen.svg_writer import INKSCAPE, SVG
from vinylgen.trace_window import (
    FITTERS_PER_WINDOW,
    PREVIEW_MARGIN_PX,
    PreviewCancelled,
    PreviewSegmentation,
    Window,
    clamp_window,
    margin_window,
    preview_trace_settings,
    preview_variant,
    render_window_svg,
    window_boundaries,
)
from vinylgen.tracing import trace_subject

SCRATCH = Path(__file__).resolve().parents[1] / ".tmp"


def nested_subject(root: Path, height: int = 32, width: int = 40,
                   origin: tuple[int, int] = (0, 0)) -> Path:
    """A background, an orange block and a green square inside the block."""
    subject = root / "nested"
    subject.mkdir()
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[:] = (15, 35, 60)
    top, left = origin
    pixels[top + 8:top + 24, left + 10:left + 30] = (225, 95, 45)
    pixels[top + 13:top + 19, left + 16:left + 24] = (60, 180, 90)
    pixels[top + 3:top + 6, left + 33:left + 37] = (225, 95, 45)
    Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
    Image.fromarray(pixels, "RGB").save(subject / "source.png")
    palette = {"dimensions": [width, height], "palette": [
        {"index": 0, "hex": "#0F233C", "rgb": [15, 35, 60], "share_percent": 55},
        {"index": 1, "hex": "#E15F2D", "rgb": [225, 95, 45], "share_percent": 35},
        {"index": 2, "hex": "#3CB45A", "rgb": [60, 180, 90], "share_percent": 10}]}
    (subject / "palette.json").write_text(json.dumps(palette))
    centres = np.array([[15, 35, 60], [225, 95, 45], [60, 180, 90]])
    labels = np.argmin(
        np.sum((pixels.astype(np.int32)[:, :, None, :] - centres[None, None]) ** 2, axis=3),
        axis=2).astype(np.int16)
    # Unequal costs either side of each interface move it off the pixel grid, so the
    # fit sees sub-pixel evidence exactly as it does on real segmentations.
    rng = np.random.default_rng(7)
    costs = rng.uniform(20.0, 60.0, size=(*labels.shape, 3)).astype(np.float32)
    np.put_along_axis(costs, labels[..., None], 0.0, axis=2)
    SegmentationBundle(
        labels=labels,
        protected_features=np.zeros(labels.shape, dtype=bool),
        explicit_protected_features=np.zeros(labels.shape, dtype=bool),
        palette_rgb=centres.astype(np.uint8),
        palette_costs=costs,
    ).save(subject / "segmentation.npz")
    return subject


def layer_paths(svg: ET.Element) -> dict[str, list[str]]:
    return {layer.attrib[f"{{{INKSCAPE}}}label"]: [path.attrib["d"] for path in layer.iter(f"{{{SVG}}}path")]
            for layer in svg.iter(f"{{{SVG}}}g") if f"{{{INKSCAPE}}}label" in layer.attrib}


class WindowGeometryTests(unittest.TestCase):
    def test_clamp_keeps_the_part_on_the_image(self):
        self.assertEqual(clamp_window(-10, 5, 30, 400, 100, 80), Window(0, 5, 20, 75))
        self.assertEqual(clamp_window(90, 70, 50, 50, 100, 80), Window(90, 70, 10, 10))
        self.assertEqual(clamp_window(10, 10, 20, 20, 100, 80), Window(10, 10, 20, 20))

    def test_clamp_rejects_a_window_off_the_image(self):
        for request in [(100, 0, 10, 10), (-20, 0, 20, 10), (0, 80, 5, 5)]:
            with self.assertRaises(ValueError):
                clamp_window(*request, 100, 80)

    def test_margin_grows_every_side_and_stops_at_the_image_edge(self):
        self.assertEqual(margin_window(Window(200, 300, 50, 60), 1000, 1000),
                         Window(200 - PREVIEW_MARGIN_PX, 300 - PREVIEW_MARGIN_PX,
                                50 + 2 * PREVIEW_MARGIN_PX, 60 + 2 * PREVIEW_MARGIN_PX))
        self.assertEqual(margin_window(Window(10, 950, 50, 50), 1000, 1000, 64),
                         Window(0, 886, 124, 114))

    def test_settings_normalize_like_a_trace_run(self):
        settings = preview_trace_settings({"straight_line_max_deviation": 0.7})
        self.assertEqual(settings.max_deviation, 2.0)
        self.assertEqual(settings.corner_straightness, 0.7)
        self.assertEqual(preview_variant(settings), "edge")
        self.assertEqual(preview_variant(preview_trace_settings({"overlap": 0.5})), "overlap")
        self.assertEqual(preview_variant(preview_trace_settings({"solid_underlayers": True})),
                         "overlap")


class WindowTraceTests(unittest.TestCase):
    def trace_and_preview(self, settings: dict, window: Window | None = None):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            subject = nested_subject(Path(name))
            trace_subject(subject, TraceSettings(**settings), {})
            variant = preview_variant(preview_trace_settings(settings))
            traced = ET.parse(subject / f"traced-{variant}.svg").getroot()
            palette = json.loads((subject / "palette.json").read_text())["palette"]
            segmentation = PreviewSegmentation.load(subject / "segmentation.npz")
        included = [0, 1, 2]
        window = window or Window(0, 0, segmentation.width, segmentation.height)
        crop = margin_window(window, segmentation.width, segmentation.height)
        boundaries = window_boundaries(segmentation, included, crop)
        order = segmentation.layer_order(included, palette)
        preview = ET.fromstring(render_window_svg(
            boundaries, window, order, palette, preview_trace_settings(settings),
            float(settings.get("overlap", 0.0))))
        return traced, preview, crop

    def test_a_whole_image_window_matches_the_full_trace_exactly(self):
        # With nothing split at a crop edge the preview is the trace itself: same
        # boundaries, same fitter, same layer construction and order.
        for settings in ({}, {"solid_underlayers": True}, {"overlap": 1.5},
                         {"curve_fit_tolerance": 0.4, "straight_line_max_deviation": 0.3}):
            with self.subTest(settings=settings):
                traced, preview, _ = self.trace_and_preview(settings)
                self.assertEqual(layer_paths(preview), layer_paths(traced))
                self.assertEqual(
                    [layer.attrib["fill"] for layer in preview.iter(f"{{{SVG}}}g")
                     if "fill" in layer.attrib],
                    [layer.attrib["fill"] for layer in traced.iter(f"{{{SVG}}}g")
                     if "fill" in layer.attrib])

    def test_a_window_is_drawn_at_its_image_position_with_the_margin_hidden(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            subject = nested_subject(Path(name), height=200, width=220, origin=(85, 95))
            segmentation = PreviewSegmentation.load(subject / "segmentation.npz")
        palette = [{"index": index, "name": f"C{index}", "hex": hex_value}
                   for index, hex_value in enumerate(["#0F233C", "#E15F2D", "#3CB45A"])]
        window = Window(100, 90, 40, 30)
        crop = margin_window(window, segmentation.width, segmentation.height)
        self.assertEqual(crop, Window(36, 26, 168, 158))
        boundaries = window_boundaries(segmentation, [0, 1, 2], crop)
        # The boundary graph is crop-local: the crop's right edge is x = crop.width.
        xs = [point[0] for chain in boundaries.track.chains for point in chain.points]
        self.assertEqual((min(xs), max(xs)), (0.0, float(crop.width)))
        svg = ET.fromstring(render_window_svg(
            boundaries, window, [0, 1, 2], palette, preview_trace_settings({}), 0.0))
        self.assertEqual(svg.attrib["viewBox"], "100 90 40 30")
        group = next(svg.iter(f"{{{SVG}}}g"))
        self.assertEqual(group.attrib["transform"], "translate(36 26)")
        # Every colour inside the window is drawn, each shape a separately fitted path.
        paths = layer_paths(svg)
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(paths.values()))

    def test_layer_order_is_the_whole_image_order_and_cached(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            subject = nested_subject(Path(name))
            segmentation = PreviewSegmentation.load(subject / "segmentation.npz")
        palette = [{"index": index, "name": f"C{index}", "hex": hex_value}
                   for index, hex_value in enumerate(["#0F233C", "#E15F2D", "#3CB45A"])]
        first = segmentation.layer_order([0, 1, 2], palette)
        self.assertEqual(first, [0, 1, 2])
        segmentation.labels[:] = 2  # a cached order is not recomputed from the labels
        self.assertEqual(segmentation.layer_order([0, 1, 2], palette), first)

    def test_float16_costs_crop_to_the_same_values_as_a_full_load(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            subject = nested_subject(Path(name))
            segmentation = PreviewSegmentation.load(subject / "segmentation.npz")
            bundle = SegmentationBundle.load(subject / "segmentation.npz")
        self.assertEqual(segmentation.palette_costs.dtype, np.float16)
        np.testing.assert_array_equal(
            segmentation.palette_costs[3:20, 5:30].astype(np.float32),
            bundle.palette_costs[3:20, 5:30])


def speckled_segmentation(size: int = 160) -> PreviewSegmentation:
    """Many small regions of three colours: hundreds of boundary chains."""
    rng = np.random.default_rng(3)
    coarse = rng.integers(0, 3, size=(size // 8, size // 8)).astype(np.int16)
    labels = np.kron(coarse, np.ones((8, 8), dtype=np.int16))
    costs = rng.uniform(20.0, 60.0, size=(*labels.shape, 3)).astype(np.float16)
    np.put_along_axis(costs, labels[..., None].astype(np.int64), 0.0, axis=2)
    return PreviewSegmentation(labels=labels, protected_features=np.zeros(labels.shape, bool),
                               palette_rgb=np.array([[0, 0, 0], [200, 30, 30], [30, 30, 200]],
                                                    dtype=np.uint8),
                               palette_costs=costs)


PALETTE = [{"index": index, "name": f"C{index}", "hex": hex_value}
           for index, hex_value in enumerate(["#000000", "#C81E1E", "#1E1EC8"])]


class CancellationTests(unittest.TestCase):
    @staticmethod
    def cancel_after(calls: int):
        seen = []

        def checkpoint() -> None:
            seen.append(1)
            if len(seen) > calls:
                raise PreviewCancelled

        return checkpoint, seen

    def test_the_boundary_build_stops_at_a_checkpoint_inside_it(self):
        segmentation = speckled_segmentation()
        crop = Window(0, 0, segmentation.width, segmentation.height)
        _, seen = self.cancel_after(10**6)
        window_boundaries(segmentation, [0, 1, 2], crop, lambda: seen.append(1))
        self.assertGreater(len(seen), 3)  # checked repeatedly, not only at the ends
        checkpoint, stopped = self.cancel_after(2)
        with self.assertRaises(PreviewCancelled):
            window_boundaries(segmentation, [0, 1, 2], crop, checkpoint)
        self.assertEqual(len(stopped), 3)

    def test_the_fit_stops_between_chain_fits_and_keeps_what_it_fitted(self):
        segmentation = speckled_segmentation()
        window = Window(0, 0, segmentation.width, segmentation.height)
        boundaries = window_boundaries(segmentation, [0, 1, 2], window)
        self.assertGreater(len(boundaries.track.chains), 50)
        settings = preview_trace_settings({})
        checkpoint, _ = self.cancel_after(20)
        with self.assertRaises(PreviewCancelled):
            render_window_svg(boundaries, window, [0, 1, 2], PALETTE, settings, 0.0, checkpoint)
        self.assertEqual(boundaries.fitter_for(settings, 0.0).cache_entries, 20)
        # The next request re-uses those fits and completes.
        full = render_window_svg(boundaries, window, [0, 1, 2], PALETTE, settings, 0.0)
        self.assertEqual(full, render_window_svg(
            window_boundaries(segmentation, [0, 1, 2], window), window, [0, 1, 2], PALETTE,
            settings, 0.0))

    def test_each_window_keeps_only_its_recent_fit_caches(self):
        segmentation = speckled_segmentation(48)
        boundaries = window_boundaries(segmentation, [0, 1, 2], Window(0, 0, 48, 48))
        first = boundaries.fitter_for(preview_trace_settings({"curve_fit_tolerance": 1.0}), 0.0)
        self.assertIs(boundaries.fitter_for(
            preview_trace_settings({"curve_fit_tolerance": 1.0}), 0.0), first)
        for tolerance in (1.1, 1.2, 1.3, 1.4):
            boundaries.fitter_for(preview_trace_settings({"curve_fit_tolerance": tolerance}), 0.0)
        self.assertEqual(len(boundaries.fitters), FITTERS_PER_WINDOW)
        self.assertIsNot(boundaries.fitter_for(
            preview_trace_settings({"curve_fit_tolerance": 1.0}), 0.0), first)


if __name__ == "__main__":
    unittest.main()
