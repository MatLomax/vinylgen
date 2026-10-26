"""Trace lengths in thousandths of the traced image's longer side.

Covers the resolver (including the live preview resolving on the whole image, not
its window), pixel-era records (trace.json, CLI configs, web revision settings)
converting so that a re-trace reproduces the recorded geometry, and the scale
invariance units exist for.
"""

import contextlib
import io
import json
import math
import random
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from vinylgen.boundary_graph import shared_boundary_polygons
from vinylgen.cli_trace import main, parser, settings_from_args
from vinylgen.contracts import (
    PER_MILLE_DEFAULTS,
    PX_DEFAULTS,
    TRACE_LENGTH_FIELDS,
    PaletteSettings,
    TraceSettings,
)
from vinylgen.curve_fit import shared_boundary_components
from vinylgen.node_types import path_nodetypes
from vinylgen.palette import infer_palette
from vinylgen.trace_lengths import (
    FLOORED_LENGTHS,
    MIN_ACCURACY_PX,
    MIN_STRAIGHT_RUN_PX,
    PREVIEW_MARGIN_MIN_PX,
    is_pixel_record,
    length_reference,
    pixel_record_in_units,
    preview_margin,
    resolve_trace_lengths,
    trace_lengths_in_units,
)
from vinylgen.trace_window import preview_trace_settings
from vinylgen.tracing import trace_subject

SCRATCH = Path(__file__).resolve().parents[1] / ".tmp"
PX_ERA = {**PX_DEFAULTS, "max_deviation": 2.0, "side_smoothness": 1.6,
          "corner_sharpening_allowance": 2.0}


def lengths(settings: TraceSettings) -> dict:
    return {name: getattr(settings, name) for name in TRACE_LENGTH_FIELDS}


class ResolverTests(unittest.TestCase):
    def test_the_defaults_are_the_pixel_era_defaults_on_a_2000_px_image(self):
        resolved = resolve_trace_lengths(TraceSettings(), 2000)
        self.assertEqual(resolved.length_unit, "px")
        self.assertEqual(lengths(resolved), PX_ERA)

    def test_lengths_scale_with_the_longer_side(self):
        self.assertEqual(length_reference(1200, 3000), 3000)
        self.assertEqual(length_reference(3000, 1200), 3000)
        small = lengths(resolve_trace_lengths(TraceSettings(), 1500))
        large = lengths(resolve_trace_lengths(TraceSettings(), 15000))
        for name in TRACE_LENGTH_FIELDS:
            self.assertAlmostEqual(large[name], 10 * small[name], places=9, msg=name)
        self.assertAlmostEqual(small["curve_fit_tolerance"],
                               PER_MILLE_DEFAULTS["curve_fit_tolerance"] * 1.5)

    def test_accuracy_lengths_never_resolve_below_one_pixel(self):
        # At 600 px the defaults would give a 0.45 px tolerance and 0.6 px deviation
        # and allowance, below the boundary's own sub-pixel noise.
        small = lengths(resolve_trace_lengths(TraceSettings(), 600))
        for name in FLOORED_LENGTHS:
            self.assertEqual(small[name], MIN_ACCURACY_PX, name)
        self.assertAlmostEqual(small["straight_line_max_deviation"], 0.48)
        self.assertAlmostEqual(small["corner_analysis_span"], 2.4)
        explicit = TraceSettings(curve_fit_tolerance=0.1, max_deviation=0.2,
                                 corner_sharpening_allowance=0.3)
        for name in FLOORED_LENGTHS:
            self.assertEqual(getattr(resolve_trace_lengths(explicit, 2000), name),
                             MIN_ACCURACY_PX, name)
        # Pixel settings are held too, so the trace and the live preview agree.
        pixels = TraceSettings(length_unit="px", curve_fit_tolerance=0.4, max_deviation=0.5)
        self.assertEqual(resolve_trace_lengths(pixels, 600).curve_fit_tolerance, MIN_ACCURACY_PX)
        self.assertEqual(resolve_trace_lengths(pixels, 600).straight_line_max_deviation, 1.6)

    def test_minimum_straight_length_always_resolves_above_one_pixel_step(self):
        # At 2 per mille a 256 px image would give 0.512 px and a 500 px image exactly
        # 1 px; either would accept every unit step of a pixel staircase as a straight
        # run. A 600 px image gives 1.2 px, which stands.
        self.assertGreater(MIN_STRAIGHT_RUN_PX, 1.0)
        for reference in (64, 256, 500):
            self.assertEqual(resolve_trace_lengths(TraceSettings(), reference)
                             .minimum_straight_length, MIN_STRAIGHT_RUN_PX, reference)
        self.assertAlmostEqual(resolve_trace_lengths(TraceSettings(), 600)
                               .minimum_straight_length, 1.2)
        pixels = TraceSettings(length_unit="px", minimum_straight_length=1.0)
        self.assertEqual(resolve_trace_lengths(pixels, 600).minimum_straight_length,
                         MIN_STRAIGHT_RUN_PX)

    def test_automatic_and_following_lengths(self):
        settings = TraceSettings(straight_line_max_deviation=1.2)
        self.assertEqual(settings.side_smoothness, 1.2)
        self.assertIsNone(settings.max_deviation)
        resolved = resolve_trace_lengths(settings, 3000)
        self.assertAlmostEqual(resolved.max_deviation, 3.0)
        self.assertAlmostEqual(resolved.side_smoothness, 3.6)
        # An explicit maximum deviation is a length like the others.
        self.assertAlmostEqual(resolve_trace_lengths(TraceSettings(max_deviation=0.5),
                                                     3000).max_deviation, 1.5)

    def test_pixel_settings_resolve_to_themselves(self):
        settings = TraceSettings(length_unit="px", curve_fit_tolerance=1.1, corner_analysis_span=7)
        resolved = resolve_trace_lengths(settings, 5000)
        self.assertEqual(lengths(resolved), {**lengths(settings), "max_deviation": 2.0,
                                             "corner_sharpening_allowance": 2.0})

    def test_pixel_values_convert_and_resolve_back_exactly(self):
        rng = random.Random(7)
        for reference in (7, 600, 1495, 1536, 2990, 4344, 11264):
            for _ in range(50):
                values = {name: round(rng.uniform(0.0, 40.0), rng.randint(0, 4))
                          for name in TRACE_LENGTH_FIELDS}
                values["curve_feature_scale"] = max(values["curve_feature_scale"], 0.1)
                settings = TraceSettings(length_unit="px", **values)
                units = trace_lengths_in_units(settings, reference)
                self.assertEqual(units.length_unit, "per_mille")
                # A pixel record reproduces its lengths, with the accuracy lengths held
                # at the 1 px minimum like any resolved length.
                expected = {name: max(MIN_ACCURACY_PX, value) if name in FLOORED_LENGTHS else value
                            for name, value in values.items()}
                expected["minimum_straight_length"] = max(MIN_STRAIGHT_RUN_PX,
                                                          values["minimum_straight_length"])
                self.assertEqual(lengths(resolve_trace_lengths(units, reference)), expected,
                                 reference)

    def test_an_automatic_pixel_deviation_converts_to_the_pixel_era_value(self):
        units = trace_lengths_in_units(TraceSettings(length_unit="px"), 1495)
        self.assertAlmostEqual(units.max_deviation, 2000 / 1495)
        self.assertEqual(lengths(resolve_trace_lengths(units, 1495)), PX_ERA)

    def test_the_preview_resolves_on_the_whole_image_not_its_window(self):
        # The live preview traces a crop, but its lengths are thousandths of the
        # traced image's longer side, so they match what the full trace run uses.
        values = {"curve_fit_tolerance": 1.0, "length_unit": "per_mille"}
        whole = preview_trace_settings(values, 3000, 2000)
        self.assertEqual(whole, resolve_trace_lengths(TraceSettings(**values), 3000))
        self.assertEqual(whole.curve_fit_tolerance, 3.0)

    def test_the_preview_margin_grows_with_the_resolved_windows(self):
        # The longest reach is the analysis span plus the corner tip skip (2.41
        # sharpening allowances): 8 + 4.83 px at 2000 px, 45.056 + 27.19 px at
        # 11264 px. A 600 px image stays at the floor.
        skip = 1 + math.sqrt(2)
        self.assertEqual(preview_margin(resolve_trace_lengths(TraceSettings(), 2000)),
                         math.ceil(8 * (8 + 2 * skip)))
        self.assertEqual(preview_margin(resolve_trace_lengths(TraceSettings(), 600)),
                         PREVIEW_MARGIN_MIN_PX)
        self.assertEqual(preview_margin(resolve_trace_lengths(TraceSettings(), 11264)),
                         math.ceil(8 * (45.056 + 11.264 * skip)))


class PixelRecordTests(unittest.TestCase):
    def test_a_record_without_a_unit_is_pixels(self):
        self.assertTrue(is_pixel_record({"curve_fit_tolerance": 1.5}))
        self.assertTrue(is_pixel_record({"length_unit": "px"}))
        self.assertFalse(is_pixel_record({"length_unit": "per_mille"}))

    def test_a_stored_pixel_record_converts_on_its_image(self):
        # A pixel-era web revision: some lengths absent (the pixel-era defaults), no
        # side smoothness (the recorded deviation), an automatic maximum
        # deviation (2 px), legacy keys, which are kept for the form to read, and
        # the retired corner threshold, which is dropped.
        stored = {"curve_fit_tolerance": 1.6, "straight_line_max_deviation": 1.2,
                  "max_deviation": None, "curve_optimization": False, "width_mm": 400.0,
                  "corner_threshold": 80.0}
        converted = pixel_record_in_units(stored, 4344)
        self.assertEqual(converted["length_unit"], "per_mille")
        self.assertEqual((converted["curve_optimization"], converted["width_mm"]),
                         (False, 400.0))
        self.assertNotIn("corner_threshold", converted)
        settings = TraceSettings(**{key: value for key, value in converted.items()
                                    if key in TraceSettings.__dataclass_fields__})
        self.assertEqual(lengths(resolve_trace_lengths(settings, 4344)), {
            **PX_ERA, "curve_fit_tolerance": 1.6, "straight_line_max_deviation": 1.2,
            "side_smoothness": 1.2})
        self.assertEqual(pixel_record_in_units(converted, 4344), converted)


class CliUnitTests(unittest.TestCase):
    def settings(self, config: dict | None, *flags: str) -> TraceSettings:
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            arguments = ["outputs", *flags]
            if config is not None:
                path = Path(name) / "config.json"
                path.write_text(json.dumps(config))
                arguments += ["--config", str(path)]
            settings, _, _ = settings_from_args(parser().parse_args(arguments))
        return settings

    def test_length_flags_are_thousandths(self):
        settings = self.settings(None, "--curve-fit-tolerance", "1", "--corner-analysis-span", "5")
        self.assertEqual((settings.length_unit, settings.curve_fit_tolerance,
                          settings.corner_analysis_span), ("per_mille", 1.0, 5.0))
        self.assertIn("thousandths of the traced image's longer side",
                      " ".join(parser().format_help().split()))

    def test_a_config_setting_lengths_must_name_their_unit(self):
        with self.assertRaisesRegex(ValueError, "without length_unit"):
            self.settings({"curve_fit_tolerance": 1.5})
        # Settings without any length need no unit; a retired setting is dropped.
        self.assertEqual(self.settings({"snap_angle": 3}).length_unit, "per_mille")
        self.assertEqual(self.settings({"corner_threshold": 45}), TraceSettings())
        pixels = self.settings({"curve_fit_tolerance": 1.5, "length_unit": "px"})
        self.assertEqual(lengths(pixels), {**PX_ERA, "max_deviation": None, "corner_sharpening_allowance": None})
        units = self.settings({"curve_fit_tolerance": 1.5, "length_unit": "per_mille"})
        self.assertEqual(units.curve_fit_tolerance, 1.5)

    def test_a_whole_trace_json_recorded_before_units_is_pixels(self):
        block = {key: value for key, value in asdict(TraceSettings(length_unit="px")).items()
                 if key != "length_unit"}
        settings = self.settings({"settings": block, "cleanup_settings": None})
        self.assertEqual(settings.length_unit, "px")
        self.assertEqual(lengths(settings), {**PX_ERA, "max_deviation": None, "corner_sharpening_allowance": None})

    def test_a_curve_optimization_block_is_pixels(self):
        settings = self.settings({"curve_optimization": False, "straight_line_max_deviation": 1.2})
        self.assertEqual((settings.length_unit, settings.straight_line_max_deviation,
                          settings.side_smoothness), ("px", 0.0, 1.2))

    def test_the_curve_feature_scale_flag_is_range_checked(self):
        with self.assertRaisesRegex(ValueError, "curve-feature-scale"):
            self.settings(None, "--curve-feature-scale", "20")


def subject_fixture(root: Path) -> Path:
    """A flattened subject with corners, curves and a thin bar (120 x 90 px)."""
    height, width = 90, 120
    yy, xx = np.mgrid[0:height, 0:width]
    pixels = np.full((height, width, 3), (240, 220, 160), dtype=np.uint8)
    pixels[(xx - 40) ** 2 + (yy - 45) ** 2 < 26 ** 2] = (225, 95, 45)
    pixels[15:70, 70:108] = (15, 35, 60)
    pixels[30:40, 80:100] = (240, 220, 160)
    pixels[75:80, 10:110] = (15, 35, 60)
    source = root / "subject.png"
    Image.fromarray(pixels, "RGB").save(source)
    infer_palette(source, root / "flat", PaletteSettings(
        initial_colours=3, min_colour_share=0, min_region_pixels=0, dither_radius=0))
    return root / "flat" / "subject"


class RetraceReproducesTests(unittest.TestCase):
    """Re-running a pixel-era trace with its recorded settings, converted to
    thousandths on its image, reproduces its geometry exactly."""

    RECORDED = {"curve_fit_tolerance": 1.2, "straight_line_max_deviation": 1.3,
                "minimum_straight_length": 3.5, "corner_analysis_span": 7.0,
                "curve_feature_scale": 3.0, "snap_angle": 3.0}

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(dir=SCRATCH))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.subject = subject_fixture(self.work)

    def trace(self, settings: TraceSettings) -> tuple[bytes, bytes, dict]:
        report = trace_subject(self.subject, settings, {})
        return ((self.subject / "traced-edge.svg").read_bytes(),
                (self.subject / "traced-overlap.svg").read_bytes(), report)

    def test_a_pixel_record_retraced_in_thousandths_is_identical(self):
        old_edge, old_overlap, old = self.trace(TraceSettings(length_unit="px", **self.RECORDED))
        # trace.json records the lengths in thousandths and the pixels they resolved to.
        self.assertEqual(old["settings"]["length_unit"], "per_mille")
        self.assertEqual(old["lengths"]["reference_px"], 120)
        self.assertEqual(old["lengths"]["px"], {
            **PX_ERA, **{key: value for key, value in self.RECORDED.items()
                         if key in TRACE_LENGTH_FIELDS},
            "side_smoothness": 1.3})
        for name, value in old["lengths"]["px"].items():
            self.assertAlmostEqual(old["lengths"]["per_mille"][name], value * 1000 / 120)
        # The web revision path: a stored pixel record converted on its image.
        stored = {key: value for key, value in asdict(
            TraceSettings(length_unit="px", **self.RECORDED)).items() if key != "length_unit"}
        converted = pixel_record_in_units(stored, 120)
        edge, overlap, again = self.trace(TraceSettings(**converted))
        self.assertEqual((edge, overlap), (old_edge, old_overlap))
        self.assertEqual(again["lengths"]["px"], old["lengths"]["px"])
        # The recorded trace.json settings (thousandths) re-run to the same geometry too.
        edge, overlap, _ = self.trace(TraceSettings(**old["settings"]))
        self.assertEqual((edge, overlap), (old_edge, old_overlap))

    def test_the_cli_retraces_a_pixel_era_trace_json_identically(self):
        old_edge, old_overlap, old = self.trace(TraceSettings(length_unit="px", **self.RECORDED))
        # A trace.json written before units: its settings block in pixels, no unit.
        legacy = {**old, "settings": {key: value for key, value in asdict(
            TraceSettings(length_unit="px", **self.RECORDED)).items() if key != "length_unit"}}
        config = self.work / "legacy-trace.json"
        config.write_text(json.dumps(legacy))
        with mock.patch("sys.argv", ["svg_trace.py", str(self.subject), "--config", str(config)]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        self.assertEqual((self.subject / "traced-edge.svg").read_bytes(), old_edge)
        self.assertEqual((self.subject / "traced-overlap.svg").read_bytes(), old_overlap)
        audit = json.loads((self.subject / "trace.json").read_text())
        self.assertEqual(audit["settings"]["length_unit"], "per_mille")
        self.assertEqual(audit["lengths"]["px"], old["lengths"]["px"])


def shape_distance(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Signed distance (positive inside, in image fractions) of a badge: a box with a
    round bulge on the right, a round bite on the left and a square notch below."""
    def box(x0, y0, x1, y1):
        qx = np.abs(x - (x0 + x1) / 2) - (x1 - x0) / 2
        qy = np.abs(y - (y0 + y1) / 2) - (y1 - y0) / 2
        return -(np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
                 + np.minimum(np.maximum(qx, qy), 0))

    def circle(cx, cy, radius):
        return radius - np.hypot(x - cx, y - cy)

    distance = np.maximum(box(.35, .38, .65, .62), circle(.65, .5, .08))
    distance = np.minimum(distance, -circle(.35, .5, .05))
    return np.minimum(distance, -box(.47, .58, .53, .63))


def traced_badge(size: int, settings: TraceSettings) -> tuple[str, list, list]:
    """The badge traced on a ``size`` px image: its path's node types (rotated to
    start at the smallest), and its corners and nodes in image fractions (sorted).

    Only the part of the image around the badge is rasterised; its costs are the
    signed distance, so the boundary is placed to sub-pixel accuracy at every size.
    """
    low, high = int(.25 * size), int(.75 * size)
    ys, xs = np.mgrid[low:high, low:high].astype(np.float64)
    distance = shape_distance((xs + .5) / size, (ys + .5) / size) * size
    labels = (distance > 0).astype(np.int16)
    costs = np.stack([distance / 2, -distance / 2], axis=-1).astype(np.float32)
    _, track, _ = shared_boundary_polygons(labels, [0, 1], costs,
                                           protected_features=np.zeros(labels.shape, bool))
    components, fits, _ = shared_boundary_components(
        track, [0, 1], resolve_trace_lengths(settings, size))
    badge = next(fit for fit in fits.values() if len(fit.corners) > 4)
    types = path_nodetypes(components[1][0]["d"])
    rotated = min(types[k:] + types[:k] for k in range(len(types)))
    scale = lambda point: (round((point[0] + low) / size, 3), round((point[1] + low) / size, 3))
    return (rotated, sorted(map(scale, badge.corners)),
            sorted(scale(primitive[-1]) for primitive in badge.primitives))


class ScaleInvarianceTests(unittest.TestCase):
    """The same artwork at 600 and 6000 px gets the same corners and node layout."""

    def assert_same_structure(self, first, second, tolerance):
        (types_a, corners_a, nodes_a), (types_b, corners_b, nodes_b) = first, second
        self.assertEqual(types_a, types_b)
        self.assertEqual(len(corners_a), len(corners_b))
        self.assertEqual(len(nodes_a), len(nodes_b))
        for a, b in zip([*corners_a, *nodes_a], [*corners_b, *nodes_b]):
            self.assertLessEqual(math.dist(a, b), tolerance, (a, b))

    def test_600_and_6000_px_trace_alike(self):
        # Two thousandths of tolerance and deviation are 1.2 px at 600 px, clear of
        # the raster's sampling noise; see ``test_default_lengths_trace_alike``.
        settings = TraceSettings(curve_fit_tolerance=2.0, max_deviation=2.0)
        small = traced_badge(600, settings)
        large = traced_badge(6000, settings)
        self.assertEqual(len(small[1]), 12)
        self.assertIn("c", small[0])  # corners stay cusps
        self.assertTrue({"s", "z"} & set(small[0]))  # curve joins stay symmetric or smooth
        # Within the resolved tolerance at 600 px, in image fractions.
        self.assertEqual((small[0], len(small[2])), (large[0], len(large[2])))
        self.assert_same_structure(small, large, 2.0 / 1000 + 1e-3)

    def test_default_lengths_trace_alike(self):
        # At the defaults the tolerance resolves to 1.5 px at 2000 px. (At 600 px it
        # would be 0.45 px, below the boundary samples' own sub-pixel noise, and the
        # fit follows that noise with extra nodes: small images need the defaults
        # calibrated, which is decided with the calibration sheets.)
        self.assert_same_structure(traced_badge(2000, TraceSettings()),
                                   traced_badge(6000, TraceSettings()), 0.75 / 1000 + 1e-3)


if __name__ == "__main__":
    unittest.main()
