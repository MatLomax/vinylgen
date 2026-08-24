import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.cli_palette import input_files
from vinylgen.colour import reduced_histogram
from vinylgen.palette import infer_palette, merge_clusters
from vinylgen.contracts import PaletteSettings
from vinylgen.contracts import SegmentationBundle
from vinylgen.segmentation import (
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
            report = infer_palette(source, output, settings or PaletteSettings(initial_colours=10))
            self.assertEqual((output / "fixture" / "source.png").read_bytes(),
                             source.read_bytes())
            with Image.open(output / "fixture" / "report.png") as report_image:
                self.assertEqual(report_image.width, pixels.shape[1])
                self.assertGreater(report_image.height, pixels.shape[0])
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
        self.assertEqual(first["palette"], second["palette"])
        self.assertTrue(any(row["reason"] == "fixed colour count" for row in first["merges"]))

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
