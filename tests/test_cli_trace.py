import contextlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from vinylgen.cli_trace import main, parser, settings_from_args
from vinylgen.contracts import CleanupSettings, PaletteSettings, SegmentationBundle, TraceSettings
from vinylgen.palette import infer_palette
from vinylgen import tracing
from vinylgen.cutter_cleanup import apply_cleanup
from vinylgen.tracing import trace_subject
from vinylgen.web.cleanup import clean_subject


class LegacyConfigTests(unittest.TestCase):
    """An older config loads with the retired trace fields dropped
    (``contracts.RETIRED_TRACE_FIELDS``) and the pixel-era ``curve_optimization`` flag
    dropped; the soft node limits it sets are kept."""

    def settings(self, config: dict, *flags: str) -> tuple[TraceSettings, CleanupSettings]:
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as name:
            path = Path(name) / "config.json"
            path.write_text(json.dumps(config))
            args = parser().parse_args(["outputs", "--config", str(path), *flags])
            settings, cleanup, _ = settings_from_args(args)
        return settings, cleanup

    def legacy_block(self, **changes) -> dict:
        # The settings block of an older trace.json: the old fitter's fields in pixels with
        # the curve_optimization flag, the cleanup fields, and set soft node limits.
        block = {"overlap": 0.0, "solid_underlayers": False, "layer_order": None,
                 "corner_threshold": 80.0, "curve_optimization": False,
                 "curve_fit_tolerance": 1.6, "straight_line_max_deviation": 1.2,
                 "minimum_straight_length": 4.0, "max_deviation": None,
                 "corner_analysis_span": 10.0, "corner_straightness": 1.2,
                 **asdict(CleanupSettings()),
                 "soft_path_nodes": 7, "soft_layer_nodes": 70, "soft_document_nodes": 700}
        block.update(changes)
        return block

    def test_retired_fields_and_the_flag_are_dropped_and_the_limits_kept(self):
        for flag in (True, False):
            settings, cleanup = self.settings(self.legacy_block(curve_optimization=flag))
            self.assertEqual(settings, TraceSettings(soft_path_nodes=7, soft_layer_nodes=70,
                                                     soft_document_nodes=700))
            self.assertEqual(cleanup, CleanupSettings())

    def test_a_per_mille_record_loads_as_the_limits(self):
        record = {"length_unit": "per_mille", "curve_fit_tolerance": .75, "side_smoothness": .8,
                  "straight_line_snap": "axis", "snap_angle": 2.0, "node_placement": "fewest",
                  "nodes_at_bends": True, "curve_feature_scale": 2.0,
                  "corner_sharpening_allowance": None}
        self.assertEqual(self.settings(record)[0], TraceSettings())

    def test_cli_flags_still_override_the_upgraded_config(self):
        _, cleanup = self.settings(self.legacy_block(), "--width-mm", "60", "--cleanup")
        self.assertEqual((cleanup.width_mm, cleanup.cleanup), (60, True))

    def test_the_retired_flags_are_gone(self):
        for flag in ("--overlap", "--layer-order", "--corner-threshold", "--snap-angle",
                     "--curve-fit-tolerance", "--corner-straightness"):
            with self.subTest(flag=flag), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parser().parse_args(["outputs", flag, "1"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["outputs", "--solid-underlayers"])

    def test_other_unknown_keys_are_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown config setting"):
            self.settings({"not_a_setting": 1})


class CliCleanupParityTests(unittest.TestCase):
    """The CLI keeps its cleanup, size and removed-colour flags and cleans inline with
    the cleanup stage's implementation, leaving the subject's files untouched."""

    FLAGS = ["--width-mm", "60", "--cleanup",
             "--cleanup-action", "remove_then_widen", "--min-detail-width", "6",
             "--min-region-area", "6", "--remove-below-width", "3"]

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(dir=Path(__file__).parents[1] / ".tmp"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        # 60 mm over 60 px: 1 mm per pixel.
        pixels = np.full((48, 60, 3), (240, 220, 160), dtype=np.uint8)
        pixels[6:42, 10:30] = (225, 95, 45)
        pixels[10:40, 40] = (15, 35, 60)          # an automatically protected thin line the cleanup removes
        pixels[10:40, 46:49] = (15, 35, 60)       # a three-pixel line the cleanup widens
        pixels[4, 50:52] = (225, 95, 45)          # a speck the cleanup removes
        source = self.work / "subject.png"
        Image.fromarray(pixels, "RGB").save(source)
        infer_palette(source, self.work / "flat", PaletteSettings(
            initial_colours=3, min_colour_share=0, min_region_pixels=0, dither_radius=0))
        self.flat = self.work / "flat" / "subject"

    def copy(self, name: str) -> Path:
        target = self.work / name / "subject"
        shutil.copytree(self.flat, target)
        return target

    def test_flags_split_into_trace_and_cleanup_settings(self):
        settings, cleanup, _ = settings_from_args(parser().parse_args(
            ["outputs", *self.FLAGS, "--remove-colour", "Navy"]))
        self.assertEqual(settings, TraceSettings())
        self.assertEqual(cleanup, CleanupSettings(
            width_mm=60, remove_colour="Navy", cleanup=True, cleanup_action="remove_then_widen",
            min_detail_width=6, min_region_area=6, remove_below_width=3))

    def test_cli_trace_matches_the_cleanup_stage_then_trace(self):
        cli = self.copy("cli")
        before = (cli / "segmentation.npz").read_bytes()
        masks = []
        real = tracing.validate_semantic_preservation

        def spy(original, source, **kwargs):
            masks.append(kwargs["protected_features"].copy())
            return real(original, source, **kwargs)

        with mock.patch.object(tracing, "validate_semantic_preservation", spy), \
                mock.patch("sys.argv", ["svg_trace.py", str(cli), *self.FLAGS]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        # The CLI cleans in memory: the subject's raster and segmentation stay as flattened.
        self.assertEqual((cli / "segmentation.npz").read_bytes(), before)
        cli_audit = json.loads((cli / "trace.json").read_text())
        self.assertTrue(cli_audit["cleanup"]["enabled"])
        self.assertGreater(cli_audit["cleanup"]["removed_pixels"], 0)
        self.assertGreater(cli_audit["cleanup"]["widened_pixels"], 0)
        self.assertGreater(cli_audit["cleanup"]["auto_protected_components_cleared"], 0)
        self.assertGreater(cli_audit["cleanup"]["auto_protected_pixels_cleared"], 0)

        staged = self.copy("staged")
        settings, cleanup, _ = settings_from_args(parser().parse_args(["outputs", *self.FLAGS]))
        audit = clean_subject(staged, cleanup)
        self.assertEqual(audit["cleanup"], cli_audit["cleanup"])
        staged_bundle = SegmentationBundle.load(staged / "segmentation.npz")
        staged_bundle.validate()
        # The CLI validates with the same protection the cleanup stage stores: automatic
        # protection cleared where labels changed.
        self.assertTrue(masks)
        for mask in masks:
            np.testing.assert_array_equal(mask, staged_bundle.protected_features)
        original = SegmentationBundle.load(cli / "segmentation.npz")
        self.assertLess(int(staged_bundle.protected_features.sum()),
                        int(original.protected_features.sum()))
        report = trace_subject(staged, settings, {}, cleanup=cleanup)
        self.assertIsNone(report["cleanup"])
        for written in ("map.json", "traced-edge.svg"):
            self.assertEqual((staged / written).read_bytes(), (cli / written).read_bytes(), written)
        # Both validate against the raster that was traced, the cleaned one, so the
        # whole audit agrees apart from input provenance and the inline summary.
        # The colour records match too, except the share each palette.json states:
        # the cleanup stage refreshes it, the CLI leaves the subject's file as flattened.
        staged_audit = json.loads((staged / "trace.json").read_text())
        for key in ("mode", "physical_dimensions_mm", "settings", "cleanup_settings",
                    "backend", "geometry", "validation", "warnings"):
            self.assertEqual(staged_audit[key], cli_audit[key], key)
        unshared = lambda rows: [{k: v for k, v in row.items() if k != "share_percent"} for row in rows]
        self.assertEqual(unshared(staged_audit["colours"]["included"]), unshared(cli_audit["colours"]["included"]))
        self.assertEqual(staged_audit["colours"]["omitted"], cli_audit["colours"]["omitted"])
        self.assertEqual(staged_audit["determinism"]["map_sha256"], cli_audit["determinism"]["map_sha256"])
        for image in ("trace-semantic-warnings.png", "trace-missed-details-overlay.png"):
            self.assertEqual((staged / image).exists(), (cli / image).exists(), image)
            if (cli / image).exists():
                self.assertEqual((staged / image).read_bytes(), (cli / image).read_bytes(), image)

    def test_the_cli_validates_against_the_cleaned_raster(self):
        # Validation must compare the vector with the raster that was traced; against
        # the uncleaned one, a speck the cleanup removed on purpose reads as lost detail.
        cli = self.copy("cli")
        stored = SegmentationBundle.load(cli / "segmentation.npz")
        palette = json.loads((cli / "palette.json").read_text())["palette"]
        _, cleanup, _ = settings_from_args(parser().parse_args(["outputs", *self.FLAGS]))
        cleaned, protected, _, _ = apply_cleanup(
            stored.labels, palette, cleanup, None, protected_features=stored.protected_features,
            explicit_protected_features=stored.explicit_protected_features)
        self.assertFalse(np.array_equal(cleaned, stored.labels))
        seen = []
        real = tracing.validate_semantic_preservation

        def spy(original, source, **kwargs):
            seen.append((source.copy(), kwargs["segmentation_labels"].copy()))
            return real(original, source, **kwargs)

        with mock.patch.object(tracing, "validate_semantic_preservation", spy), \
                mock.patch("sys.argv", ["svg_trace.py", str(cli), *self.FLAGS]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        self.assertTrue(seen)
        for source, labels in seen:
            np.testing.assert_array_equal(labels, cleaned)
            np.testing.assert_array_equal(source, stored.palette_rgb[cleaned])

    def test_a_whole_trace_json_round_trips_through_config(self):
        cli = self.copy("cli")
        with mock.patch("sys.argv", ["svg_trace.py", str(cli), *self.FLAGS]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        first = json.loads((cli / "trace.json").read_text())
        settings, cleanup, _ = settings_from_args(parser().parse_args(
            ["outputs", "--config", str(cli / "trace.json")]))
        self.assertEqual(asdict(settings), first["settings"])
        self.assertEqual(asdict(cleanup), first["cleanup_settings"])
        # A settings block carrying the cleanup object works the same way.
        block = self.work / "block.json"
        block.write_text(json.dumps({**first["settings"], "cleanup_settings": first["cleanup_settings"]}))
        _, from_block, _ = settings_from_args(parser().parse_args(["outputs", "--config", str(block)]))
        self.assertEqual(from_block, cleanup)


if __name__ == "__main__":
    unittest.main()
