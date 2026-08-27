import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.cli_palette import input_files
from vinylgen.colour import reduced_histogram
from vinylgen.palette import infer_palette, merge_clusters, _scale2x_labels
from vinylgen.contracts import PaletteSettings
from vinylgen.contracts import SegmentationBundle
from vinylgen.segmentation import (
    collapse_dither_speckle,
    fold_underrepresented_colours,
    protect_components_at_points,
    remove_small_regions,
)


class PaletteInferenceTests(unittest.TestCase):
    def test_segmentation_contract_round_trip(self):
        bundle = SegmentationBundle(
            labels=np.array([[0, 1]], dtype=np.int16),
            protected_features=np.array([[False, True]]),
            explicit_protected_features=np.array([[False, True]]),
            palette_rgb=np.array([[0, 0, 0], [255, 255, 255]], dtype=np.uint8),
            palette_costs=np.array([[[0.0, 10.0], [10.0, 0.0]]], dtype=np.float32),
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            path = Path(temporary) / "segmentation.npz"
            bundle.save(path)
            loaded = SegmentationBundle.load(path)
            loaded.validate()
        np.testing.assert_array_equal(loaded.labels, bundle.labels)
        np.testing.assert_array_equal(loaded.protected_features, bundle.protected_features)
        np.testing.assert_array_equal(
            loaded.explicit_protected_features, bundle.explicit_protected_features)

    def test_default_minimum_colour_share_is_one_percent(self):
        self.assertEqual(PaletteSettings.min_colour_share, 0.01)

    def test_small_region_folding_is_enabled_by_default(self):
        self.assertEqual(PaletteSettings.min_region_pixels, 24)

    def test_high_contrast_speck_is_folded(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[3, 3] = 1
        centres = np.array([[90.0, 0.0, 0.0], [10.0, 0.0, 0.0]])

        cleaned, summary = remove_small_regions(
            labels, centres, minimum_pixels=2)

        self.assertFalse(np.any(cleaned == 1))
        self.assertEqual(summary["removed_regions"], 1)

    def test_explicitly_distinct_cluster_families_never_merge(self):
        centres = np.array([[50.0, 0.0, 0.0], [51.0, 0.0, 0.0], [80.0, 0.0, 0.0]])
        counts = np.array([50.0, 40.0, 10.0])
        labels = np.array([[0, 1, 2]], dtype=np.int32)
        settings = PaletteSettings(similar_delta_e=2.0, min_output_colours=2)

        merged, decisions = merge_clusters(
            centres, counts, labels, settings, distinct_cluster_pairs={(0, 1)}
        )

        self.assertEqual(len(merged), 3)
        self.assertEqual(decisions, [])

    def test_point_selects_only_its_four_connected_colour_component(self):
        labels = np.zeros((6, 7), dtype=np.int32)
        labels[1:3, 1:3] = 1
        labels[3, 3] = 1

        protected, resolved = protect_components_at_points(labels, ((1, 1),))

        self.assertTrue(np.all(protected[1:3, 1:3]))
        self.assertFalse(protected[3, 3])
        self.assertEqual(resolved[0]["component_pixels"], 4)

    def test_explicit_component_survives_small_region_cleanup(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[3:5, 3:5] = 1
        centres = np.array([[90.0, 0.0, 0.0], [10.0, 0.0, 0.0]])
        protected, _ = protect_components_at_points(labels, ((3, 3),))

        cleaned, _ = remove_small_regions(
            labels, centres, minimum_pixels=100, protected_features=protected
        )

        np.testing.assert_array_equal(cleaned, labels)

    def run_fixture(self, pixels: np.ndarray, settings: PaletteSettings | None = None):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            root = Path(temporary)
            source = root / "fixture.png"
            Image.fromarray(pixels.astype(np.uint8), "RGB").save(source)
            output = root / "output"
            effective = settings or PaletteSettings(initial_colours=10)
            report = infer_palette(source, output, effective)
            self.assertEqual((output / "fixture" / "source.png").read_bytes(),
                             source.read_bytes())
            with Image.open(output / "fixture" / "report.png") as report_image:
                self.assertEqual(report_image.width, pixels.shape[1] * effective.flatten_upscale)
                self.assertGreater(report_image.height, pixels.shape[0] * effective.flatten_upscale)
            return report

    def test_fixed_colour_count_is_exact_and_deterministic(self):
        pixels = np.empty((80, 80, 3), dtype=np.uint8)
        pixels[:40, :40] = (10, 20, 40)
        pixels[:40, 40:] = (220, 70, 45)
        pixels[40:, :40] = (235, 200, 90)
        pixels[40:, 40:] = (40, 140, 120)
        settings = PaletteSettings(initial_colours=4, final_colours=3,
                                   min_region_pixels=0, min_colour_share=0)
        first = self.run_fixture(pixels, settings)
        second = self.run_fixture(pixels, settings)
        self.assertEqual(first["inferred_colour_count"], 3)
        self.assertEqual(first["fixed_colour_count"],
                         {"requested": 3, "produced": 3, "met": True})
        self.assertEqual(first["palette"], second["palette"])
        self.assertTrue(any(row["reason"] == "fixed colour count" for row in first["merges"]))

    def test_fixed_colour_count_undershoot_reports_the_produced_count(self):
        # Four solid quadrants plus a sub-min_region_pixels speck of a fifth
        # colour: the speck survives histogram analysis and clustering, so five
        # colours are requested, but remove_small_regions folds the speck at full
        # resolution and four remain. The run succeeds and reports the shortfall
        # rather than raising.
        pixels = np.empty((80, 80, 3), dtype=np.uint8)
        pixels[:40, :40] = (10, 20, 40)
        pixels[:40, 40:] = (220, 70, 45)
        pixels[40:, :40] = (235, 200, 90)
        pixels[40:, 40:] = (40, 140, 120)
        pixels[10:14, 10:15] = (200, 30, 200)  # 20 px, below min_region_pixels
        settings = PaletteSettings(initial_colours=5, final_colours=5,
                                   min_region_pixels=24, dither_radius=0)
        report = self.run_fixture(pixels, settings)
        self.assertEqual(report["inferred_colour_count"], 4)
        self.assertEqual(report["fixed_colour_count"],
                         {"requested": 5, "produced": 4, "met": False})

    def test_flatten_upscale_is_exact_nearest_neighbour_of_native_output(self):
        pixels = np.empty((40, 60, 3), dtype=np.uint8)
        pixels[:, :30] = (12, 28, 48)
        pixels[:, 30:] = (238, 196, 95)
        pixels[10:20, 10:20] = (225, 40, 30)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            root = Path(temporary)
            source = root / "fixture.png"
            Image.fromarray(pixels, "RGB").save(source)
            infer_palette(source, root / "native",
                          PaletteSettings(initial_colours=6, flatten_upscale=1))
            infer_palette(source, root / "scaled",
                          PaletteSettings(initial_colours=6, flatten_upscale=2,
                                          flatten_smooth_edges=False))
            with Image.open(root / "native" / "fixture" / "flattened.png") as native_image:
                native = np.asarray(native_image.convert("RGB"))
            with Image.open(root / "scaled" / "fixture" / "flattened.png") as scaled_image:
                scaled = np.asarray(scaled_image.convert("RGB"))
            native_labels = SegmentationBundle.load(
                root / "native" / "fixture" / "segmentation.npz").labels
            scaled_bundle = SegmentationBundle.load(
                root / "scaled" / "fixture" / "segmentation.npz")
        self.assertEqual(native.shape, (40, 60, 3))
        self.assertEqual(scaled.shape, (80, 120, 3))
        # The upscale is a pure block expansion applied after inference, so doubling
        # the native raster reproduces the scaled one exactly.
        np.testing.assert_array_equal(scaled, np.repeat(np.repeat(native, 2, 0), 2, 1))
        scaled_bundle.validate()
        self.assertEqual(scaled_bundle.labels.shape, (80, 120))
        np.testing.assert_array_equal(
            scaled_bundle.labels, np.repeat(np.repeat(native_labels, 2, 0), 2, 1))

    def test_scale2x_smooths_diagonals_within_palette_and_guards_protection(self):
        labels = np.array([[0, 0, 1],
                           [0, 1, 1],
                           [1, 1, 1]], dtype=np.int32)
        plain = np.repeat(np.repeat(labels, 2, axis=0), 2, axis=1)
        unguarded = _scale2x_labels(labels, np.zeros_like(labels, dtype=bool))
        self.assertEqual(unguarded.shape, (6, 6))
        # Corner fills only ever reuse existing labels, never a new one.
        self.assertTrue(set(np.unique(unguarded)).issubset({0, 1}))
        # A diagonal step is actually smoothed away from the plain repeat.
        self.assertTrue(np.any(unguarded != plain))
        # Fully protecting every pixel leaves a solid block-for-block repeat.
        guarded = _scale2x_labels(labels, np.ones_like(labels, dtype=bool))
        np.testing.assert_array_equal(guarded, plain)

    def test_flatten_edge_smoothing_stays_within_palette_and_changes_output(self):
        pixels = np.empty((48, 48, 3), dtype=np.uint8)
        pixels[:] = (12, 28, 48)
        for row in range(48):
            pixels[row, :row] = (238, 196, 95)  # a diagonal boundary to smooth
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            root = Path(temporary)
            source = root / "fixture.png"
            Image.fromarray(pixels, "RGB").save(source)
            report = infer_palette(source, root / "smooth",
                                   PaletteSettings(initial_colours=4, flatten_smooth_edges=True))
            infer_palette(source, root / "plain",
                          PaletteSettings(initial_colours=4, flatten_smooth_edges=False))
            with Image.open(root / "smooth" / "fixture" / "flattened.png") as smoothed_image:
                smoothed = np.asarray(smoothed_image.convert("RGB"))
            with Image.open(root / "plain" / "fixture" / "flattened.png") as plain_image:
                plain = np.asarray(plain_image.convert("RGB"))
        self.assertEqual(smoothed.shape, plain.shape)
        # Smoothing reassigns staircase corners...
        self.assertTrue(np.any(smoothed != plain))
        # ...but introduces no colour outside the inferred palette.
        palette_colours = {tuple(row["rgb"]) for row in report["palette"]}
        smoothed_colours = {tuple(pixel) for pixel in smoothed.reshape(-1, 3)}
        self.assertTrue(smoothed_colours.issubset(palette_colours))

    def test_fixed_colour_count_above_source_colours_fails_before_output(self):
        pixels = np.zeros((20, 20, 3), dtype=np.uint8)
        pixels[:, 10:] = 255
        with self.assertRaisesRegex(ValueError, "exceeds the source"):
            self.run_fixture(pixels, PaletteSettings(initial_colours=4, final_colours=3))

    def test_small_similar_shade_merges_but_contrasting_accent_survives(self):
        pixels = np.empty((120, 120, 3), dtype=np.uint8)
        pixels[:60, :] = (12, 28, 48)       # navy
        pixels[60:, :60] = (242, 211, 140)  # cream
        pixels[60:, 60:] = (40, 126, 112)   # teal
        pixels[78:88, 78:88] = (48, 134, 120)  # small near-teal shade
        pixels[104:117, 104:117] = (225, 40, 30)  # small contrasting accent above cutoff

        report = self.run_fixture(pixels)

        self.assertEqual(report["inferred_colour_count"], 4)
        self.assertIsNone(report["fixed_colour_count"]["requested"])
        self.assertTrue(report["fixed_colour_count"]["met"])
        self.assertTrue(any(item["reason"] == "perceptually similar" for item in report["merges"]))

    def test_result_is_deterministic(self):
        pixels = np.empty((80, 100, 3), dtype=np.uint8)
        pixels[:, :50] = (10, 25, 45)
        pixels[:, 50:] = (238, 196, 95)
        pixels[20:30, 20:30] = (15, 32, 53)

        first = self.run_fixture(pixels)
        second = self.run_fixture(pixels)

        self.assertEqual(first["inferred_colour_count"], second["inferred_colour_count"])
        self.assertEqual(first["palette"], second["palette"])
        self.assertEqual(first["merges"], second["merges"])

    def test_small_region_uses_closest_touching_colour(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[:, 4:] = 1
        labels[3, 3] = 2
        centres = np.array(
            [
                [30.0, -35.0, 10.0],
                [85.0, 2.0, 75.0],
                [32.0, -32.0, 9.0],
            ]
        )

        cleaned, summary = remove_small_regions(labels, centres, minimum_pixels=2)

        self.assertEqual(cleaned[3, 3], 0)
        self.assertEqual(summary["removed_regions"], 1)
        self.assertEqual(summary["removed_pixels"], 1)

    def test_region_at_threshold_is_retained(self):
        labels = np.zeros((6, 6), dtype=np.int32)
        labels[2:4, 2:4] = 1
        centres = np.array([[20.0, 0.0, 0.0], [80.0, 0.0, 0.0]])

        cleaned, summary = remove_small_regions(labels, centres, minimum_pixels=4)

        np.testing.assert_array_equal(cleaned, labels)
        self.assertEqual(summary["removed_regions"], 0)

    def test_adjacent_tiny_regions_do_not_swap(self):
        labels = np.zeros((8, 8), dtype=np.int32)
        labels[3, 3] = 1
        labels[3, 4] = 2
        centres = np.array(
            [[25.0, 0.0, 0.0], [70.0, 35.0, 20.0], [72.0, 34.0, 20.0]]
        )

        cleaned, summary = remove_small_regions(labels, centres, minimum_pixels=3)

        self.assertEqual(cleaned[3, 3], 0)
        self.assertEqual(cleaned[3, 4], 0)
        self.assertEqual(summary["removed_regions"], 2)

    def test_corner_touch_does_not_preserve_isolated_pixel(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[0:3, 0:3] = 1
        labels[3, 3] = 1
        centres = np.array([[25.0, 0.0, 0.0], [75.0, 20.0, 10.0]])

        cleaned, summary = remove_small_regions(labels, centres, minimum_pixels=2)

        self.assertEqual(cleaned[3, 3], 0)
        self.assertGreaterEqual(summary["removed_regions"], 1)

    def test_underrepresented_colour_folds_into_nearest_colour(self):
        labels = np.zeros((100, 100), dtype=np.int32)
        labels[:, 60:] = 1
        labels[10:12, 10:12] = 2
        centres = np.array(
            [[30.0, -20.0, 5.0], [80.0, 10.0, 60.0], [32.0, -18.0, 4.0]]
        )

        cleaned, summary = fold_underrepresented_colours(
            labels, centres, minimum_share=0.001
        )

        self.assertFalse(np.any(cleaned == 2))
        self.assertTrue(np.all(cleaned[10:12, 10:12] == 0))
        self.assertEqual(summary["removed_colours"], 1)
        self.assertEqual(summary["removed_pixels"], 4)

    def test_colour_at_share_cutoff_is_retained(self):
        labels = np.zeros((100, 100), dtype=np.int32)
        labels[:, 60:] = 1
        labels[10:12, 10:15] = 2
        centres = np.array(
            [[30.0, 0.0, 0.0], [80.0, 0.0, 0.0], [50.0, 30.0, 0.0]]
        )

        cleaned, summary = fold_underrepresented_colours(
            labels, centres, minimum_share=0.001
        )

        np.testing.assert_array_equal(cleaned, labels)
        self.assertEqual(summary["removed_colours"], 0)

    def test_explicit_colour_bypasses_global_share_cutoff(self):
        labels = np.zeros((100, 100), dtype=np.int32)
        labels[:, 60:] = 1
        labels[10:12, 10:12] = 2
        centres = np.array(
            [[30.0, 0.0, 0.0], [80.0, 0.0, 0.0], [50.0, 30.0, 0.0]]
        )

        cleaned, summary = fold_underrepresented_colours(
            labels, centres, minimum_share=0.01, preserved_colours={2}
        )

        np.testing.assert_array_equal(cleaned, labels)
        self.assertEqual(summary["removed_colours"], 0)

    def test_dither_radius_defaults_to_one(self):
        self.assertEqual(PaletteSettings.dither_radius, 1)

    def test_dither_vote_radius_zero_is_a_noop(self):
        labels = np.array([[0, 1, 0], [1, 0, 1], [0, 1, 0]], dtype=np.int32)

        result, summary = collapse_dither_speckle(
            labels, np.zeros_like(labels), 2, 0, np.zeros_like(labels, dtype=bool)
        )

        np.testing.assert_array_equal(result, labels)
        self.assertEqual(summary["changed_assignments"], 0)

    def test_dither_consensus_folds_speckle_when_both_proposals_agree(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[1, 1] = 1
        labels[3, 4] = 1
        labels[5, 2] = 1
        # The despeckled-source assignment reads the whole field as background,
        # matching the neighbourhood majority, so every speck folds.
        despeckled = np.zeros_like(labels)

        result, summary = collapse_dither_speckle(
            labels, despeckled, 2, 1, np.zeros_like(labels, dtype=bool)
        )

        self.assertFalse(np.any(result == 1))
        self.assertEqual(summary["changed_assignments"], 3)

    def test_dither_consensus_keeps_a_speck_the_despeckled_source_still_asserts(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[3, 3] = 1
        # The despeckled source still assigns this pixel to label 1, so it
        # disagrees with the majority and the pixel is left untouched.
        despeckled = np.zeros_like(labels)
        despeckled[3, 3] = 1

        result, summary = collapse_dither_speckle(
            labels, despeckled, 2, 1, np.zeros_like(labels, dtype=bool)
        )

        self.assertEqual(result[3, 3], 1)
        self.assertEqual(summary["changed_assignments"], 0)

    def test_dither_vote_never_touches_protected_pixels(self):
        labels = np.zeros((7, 7), dtype=np.int32)
        labels[3, 3] = 1
        despeckled = np.zeros_like(labels)  # would otherwise fold the speck
        protected = np.zeros_like(labels, dtype=bool)
        protected[3, 3] = True

        result, summary = collapse_dither_speckle(labels, despeckled, 2, 1, protected)

        self.assertEqual(result[3, 3], 1)
        self.assertEqual(summary["changed_assignments"], 0)

    def test_dither_removal_erases_source_speckle_before_flattening(self):
        # A blue field peppered with sparse contrasting specks beside a solid
        # dark anchor. Cleanup is disabled so any difference comes only from the
        # dither pre-filter, not from small-region folding.
        pixels = np.empty((48, 48, 3), dtype=np.uint8)
        pixels[:, :24] = (60, 110, 170)
        pixels[:, 24:] = (30, 40, 60)
        pixels[3:24:3, 3:24:3] = (235, 210, 95)
        settings = lambda radius: PaletteSettings(
            initial_colours=6, min_region_pixels=0, min_colour_share=0,
            flatten_upscale=1, dither_radius=radius,
        )
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            root = Path(temporary)
            source = root / "fixture.png"
            Image.fromarray(pixels, "RGB").save(source)
            speckled = infer_palette(source, root / "speckled", settings(0))
            cleaned = infer_palette(source, root / "cleaned", settings(1))
            with Image.open(root / "speckled" / "fixture" / "flattened.png") as image:
                speckled_left = np.asarray(image.convert("RGB"))[:, :24]
            with Image.open(root / "cleaned" / "fixture" / "flattened.png") as image:
                cleaned_left = np.asarray(image.convert("RGB"))[:, :24]
        speckled_colours = {tuple(pixel) for pixel in speckled_left.reshape(-1, 3)}
        cleaned_colours = {tuple(pixel) for pixel in cleaned_left.reshape(-1, 3)}
        # Without removal the specks survive as a second colour in the field...
        self.assertGreater(len(speckled_colours), 1)
        # ...with removal the whole field flattens to one solid colour.
        self.assertEqual(len(cleaned_colours), 1)
        self.assertEqual(cleaned["dither_speckle_cleanup"]["radius"], 1)

    def test_reduced_histogram_round_trips_high_bit_depths(self):
        rgb = np.array([[[200, 100, 50]]], dtype=np.uint8)
        for bits in (6, 8):
            decoded, counts = reduced_histogram(rgb, bits)
            self.assertEqual(decoded.shape, (1, 3))
            np.testing.assert_allclose(
                decoded[0], [200, 100, 50], atol=255 / ((1 << bits) - 1)
            )
            self.assertEqual(int(counts[0]), 1)

    def test_reduced_histogram_bits_five_is_unchanged(self):
        rgb = np.array(
            [[[200, 100, 50], [10, 240, 130], [0, 0, 0], [255, 255, 255]]],
            dtype=np.uint8,
        )
        bits = 5
        shift = 8 - bits
        bins = (rgb.astype(np.uint16) >> shift).reshape(-1, 3)
        packed = (bins[:, 0] << (2 * bits)) | (bins[:, 1] << bits) | bins[:, 2]
        values, counts = np.unique(packed, return_counts=True)
        mask = (1 << bits) - 1
        expected = np.column_stack(
            ((values >> (2 * bits)) & mask, (values >> bits) & mask, values & mask)
        ).astype(np.float64) * (255 / mask)

        decoded, actual_counts = reduced_histogram(rgb, bits)

        np.testing.assert_array_equal(decoded, expected)
        np.testing.assert_array_equal(actual_counts, counts.astype(np.float64))

    def test_input_files_rejects_stem_collisions(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            root = Path(temporary)
            first = root / "foo.png"
            second = root / "foo.jpg"
            first.write_bytes(b"")
            second.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "foo"):
                input_files([first, second])

    def test_input_files_accepts_distinct_stems(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".tmp") as temporary:
            root = Path(temporary)
            first = root / "foo.png"
            second = root / "bar.jpg"
            first.write_bytes(b"")
            second.write_bytes(b"")
            self.assertEqual(input_files([first, second]), [first, second])


if __name__ == "__main__":
    unittest.main()
