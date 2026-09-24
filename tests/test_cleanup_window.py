from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image
from scipy import ndimage

from vinylgen import cleanup_window, cutter_cleanup
from vinylgen.cleanup_window import (CLEANUP_PREVIEW_MARGIN_PX, CleanupSource, clean_window,
                                     will_change)
from vinylgen.contracts import CleanupSettings
from vinylgen.cutter_cleanup import (apply_cleanup, cleanup_labels, measure_colour_regions,
                                     remove_then_widen_stages)
from vinylgen.web.cleanup import write_changes_overlay
from vinylgen.trace_window import PREVIEW_MARGIN_PX, PreviewCancelled, Window

PALETTE = [
    {"index": 0, "name": "Night", "hex": "#0F233C", "rgb": [15, 35, 60]},
    {"index": 1, "name": "Ember", "hex": "#E15F2D", "rgb": [225, 95, 45]},
    {"index": 2, "name": "Bone", "hex": "#F0E1C8", "rgb": [240, 225, 200]},
    {"index": 3, "name": "Moss", "hex": "#3C6E3C", "rgb": [60, 110, 60]},
]
HEIGHT, WIDTH = 800, 1100
# The visible window; its crop (window plus the 256 px margin) is x 0..636, y 0..516.
WINDOW = Window(200, 160, 180, 100)
CROP = Window(0, 0, 636, 516)


def raster() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Labels, automatic and explicit protection exercising every remove decision."""
    labels = np.zeros((HEIGHT, WIDTH), dtype=np.int16)
    # A large Ember body with a Bone core, so undersized regions have two neighbours.
    labels[120:300, 150:420] = 1
    labels[180:240, 250:330] = 2
    # Undersized specks inside the window, one of them a two-colour touching group.
    labels[170:174, 210:214] = 2
    labels[200:203, 300:303] = 3
    labels[250:253, 220:224] = 3
    labels[250:253, 224:227] = 2
    # A Moss speck in the Ember body touching the Bone core on one side: Bone is the
    # closer colour, but the speck shares most of its edge with Ember.
    labels[200:203, 247:250] = 3
    # A thin Moss stripe crossing the window and running far outside the crop: its
    # part inside the crop is under the area threshold, the whole stripe is not.
    labels[215:218, 0:WIDTH] = 3
    # An undersized Bone region crossing the window edge whose only explicitly
    # protected pixel lies outside the crop: a full run keeps it whole.
    labels[190:193, 350:WIDTH - 20] = 2
    explicit = np.zeros_like(labels, dtype=bool)
    explicit[191, WIDTH - 22] = True
    # An explicitly protected speck inside the window.
    labels[230:233, 360:363] = 3
    explicit[231, 361] = True
    automatic = np.zeros_like(labels, dtype=bool)
    automatic[200:203, 300:303] = True
    return labels, automatic, explicit


def source_of(labels: np.ndarray, explicit: np.ndarray) -> CleanupSource:
    rgb = np.asarray([row["rgb"] for row in PALETTE], dtype=np.uint8)
    return CleanupSource(labels=labels, explicit=explicit, palette_rgb=rgb)


def window_of(array: np.ndarray, window: Window) -> np.ndarray:
    return array[window.y:window.bottom, window.x:window.right]


class CleanWindowTest(unittest.TestCase):
    def full_run(self, labels, automatic, explicit, settings, omitted=None) -> np.ndarray:
        cleaned, _, _, _ = apply_cleanup(labels, PALETTE, settings, omitted,
                                         protected_features=automatic,
                                         explicit_protected_features=explicit)
        return cleaned

    def test_remove_matches_a_full_run_including_regions_the_crop_cuts(self) -> None:
        labels, automatic, explicit = raster()
        source = source_of(labels, explicit)
        # Thresholds in mm under a physical size resolved from the whole raster (the
        # crop's proportions differ), 0.5 mm per px: 600 mm2 is 2400 px.
        settings = CleanupSettings(width_mm=WIDTH / 2, cleanup=True, cleanup_action="remove",
                                   min_region_area=600.0)
        full = self.full_run(labels, automatic, explicit, settings)
        preview = clean_window(source, WINDOW, PALETTE, settings, None)
        self.assertEqual(preview.crop, CROP)
        np.testing.assert_array_equal(preview.labels, window_of(full, WINDOW))
        np.testing.assert_array_equal(preview.removed, window_of(full != labels, WINDOW))
        self.assertFalse(preview.widened.any())
        self.assertTrue(preview.removed.any())
        # The stripe and the region protected outside the crop survive, as in the full run.
        self.assertTrue((window_of(full, WINDOW)[215 - WINDOW.y, :] == 3).all())
        self.assertEqual(int(full[191, 360]), 2)
        self.assertTrue((full[200:203, 247:250] == 1).all())
        # A naive crop misjudges both: this fixture tells the two apart.
        crop = cleanup_window.margin_window(WINDOW, WIDTH, HEIGHT, CLEANUP_PREVIEW_MARGIN_PX)
        rows, columns = slice(crop.y, crop.bottom), slice(crop.x, crop.right)
        naive = self.full_run(np.ascontiguousarray(labels[rows, columns]), automatic[rows, columns],
                              explicit[rows, columns], replace(settings, width_mm=None,
                                                              min_region_area=2400.0))
        inner = (slice(WINDOW.y - crop.y, WINDOW.bottom - crop.y),
                 slice(WINDOW.x - crop.x, WINDOW.right - crop.x))
        self.assertFalse(np.array_equal(naive[inner], window_of(full, WINDOW)))

    def test_removed_colour_and_width_threshold_match_a_full_run(self) -> None:
        labels, automatic, explicit = raster()
        source = source_of(labels, explicit)
        for settings, omitted in (
                (CleanupSettings(cleanup=True, cleanup_action="remove", min_detail_width=4.0), None),
                (CleanupSettings(cleanup=True, cleanup_action="remove", min_region_area=240.0,
                                 remove_colour="Moss"), 3),
                (CleanupSettings(cleanup=True, cleanup_action="remove", min_region_area=240.0,
                                 remove_colour="Night"), 0)):
            with self.subTest(settings=settings):
                full = self.full_run(labels, automatic, explicit, settings, omitted)
                preview = clean_window(source, WINDOW, PALETTE, settings, omitted)
                np.testing.assert_array_equal(preview.labels, window_of(full, WINDOW))

    def test_widen_matches_a_full_run_when_no_widening_reaches_the_crop_edge(self) -> None:
        labels = np.zeros((HEIGHT, WIDTH), dtype=np.int16)
        labels[150:280, 180:400] = 1
        # Thin strokes and a small speck wholly inside the window grow only into the
        # Ember body, which lies clear of the crop edge; the background that does
        # reach the edge is too wide to widen, so no widening involves the edge.
        labels[180:181, 220:300] = 2
        labels[200:240, 330:331] = 3
        labels[250:253, 240:243] = 2
        explicit = np.zeros_like(labels, dtype=bool)
        automatic = np.zeros_like(labels, dtype=bool)
        source = source_of(labels, explicit)
        for settings in (
                CleanupSettings(cleanup=True, cleanup_action="widen", min_detail_width=4.0,
                                min_region_area=20.0),
                CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                min_detail_width=4.0, remove_below_width=0.0,
                                min_region_area=12.0)):
            with self.subTest(action=settings.cleanup_action):
                full = self.full_run(labels, automatic, explicit, settings)
                preview = clean_window(source, WINDOW, PALETTE, settings, None)
                np.testing.assert_array_equal(preview.labels, window_of(full, WINDOW))
                changed = window_of(full != labels, WINDOW)
                np.testing.assert_array_equal(preview.removed | preview.widened, changed)
                self.assertTrue(preview.widened.any())
        # The remove stage's changes are red, the widening's green.
        remove_then_widen = CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                            min_detail_width=4.0, min_region_area=12.0)
        preview = clean_window(source, WINDOW, PALETTE, remove_then_widen, None)
        self.assertTrue(preview.removed[250 - WINDOW.y, 240 - WINDOW.x])
        self.assertTrue(preview.widened[181 - WINDOW.y, 250 - WINDOW.x])

    def test_the_hidden_margin_is_256_px_apart_from_the_trace_preview(self) -> None:
        self.assertEqual(CLEANUP_PREVIEW_MARGIN_PX, 256)
        self.assertEqual(PREVIEW_MARGIN_PX, 64)
        labels, _, explicit = raster()
        interior = Window(400, 300, 100, 100)
        preview = clean_window(source_of(labels, explicit), interior, PALETTE,
                               CleanupSettings(cleanup=True, min_region_area=10.0), None)
        self.assertEqual(preview.crop, Window(144, 44, 612, 612))

    def test_a_widening_that_diverged_with_a_64_px_margin_matches_with_256(self) -> None:
        labels = np.zeros((HEIGHT, WIDTH), dtype=np.int16)
        # A one-pixel Bone stroke crossing the window on the Night background, whose
        # far end touches an Ember block 150 px beyond the window: outside a 64 px
        # margin, inside a 256 px one. Ember is the stroke's nearest touching colour,
        # so a full run widens it into Ember only (nothing inside the window); a crop
        # that misses the block sees only Night and widens into Night there.
        labels[210, 250:531] = 2
        labels[180:240, 531:600] = 1
        explicit = np.zeros_like(labels, dtype=bool)
        automatic = np.zeros_like(labels, dtype=bool)
        source = source_of(labels, explicit)
        settings = CleanupSettings(cleanup=True, cleanup_action="widen", min_detail_width=4.0)
        full = self.full_run(labels, automatic, explicit, settings)
        preview = clean_window(source, WINDOW, PALETTE, settings, None)
        np.testing.assert_array_equal(preview.labels, window_of(full, WINDOW))
        with mock.patch.object(cleanup_window, "CLEANUP_PREVIEW_MARGIN_PX", 64):
            narrow = clean_window(source, WINDOW, PALETTE, settings, None)
        self.assertGreater(int((narrow.labels != window_of(full, WINDOW)).sum()), 100)

    def test_changes_are_the_net_change_from_the_uncleaned_raster(self) -> None:
        # The speck's top row is removed, then widened back, so it ends unchanged.
        labels = stroke_and_speck()
        explicit = np.zeros_like(labels, dtype=bool)
        automatic = np.zeros_like(labels, dtype=bool)
        settings = CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                   min_detail_width=4.0, min_region_area=12.0)
        full = self.full_run(labels, automatic, explicit, settings)
        self.assertEqual(int(full[202, 280]), 1)
        self.assertEqual(int(full[203, 280]), 0)
        preview = clean_window(source_of(labels, explicit), WINDOW, PALETTE, settings, None)
        np.testing.assert_array_equal(preview.labels, window_of(full, WINDOW))
        changed = window_of(full != labels, WINDOW)
        np.testing.assert_array_equal(preview.removed | preview.widened, changed)
        self.assertFalse((preview.removed & preview.widened).any())
        row, column = 202 - WINDOW.y, 280 - WINDOW.x
        self.assertFalse(preview.removed[row, column] or preview.widened[row, column])
        self.assertTrue(preview.removed[row + 1, column])
        self.assertTrue(preview.widened[201 - WINDOW.y, column])

    def test_a_window_whose_crop_is_the_whole_raster_matches_every_action_exactly(self) -> None:
        rng = np.random.default_rng(7)
        labels = rng.integers(0, 4, size=(60, 70)).astype(np.int16)
        labels = np.repeat(np.repeat(labels, 3, axis=0), 3, axis=1)[:150, :170]
        labels[40:45, 10:160] = 2
        explicit = np.zeros_like(labels, dtype=bool)
        explicit[::17, ::13] = True
        automatic = rng.random(labels.shape) < 0.05
        source = source_of(labels, explicit)
        window = Window(20, 20, 130, 110)
        for settings in (
                CleanupSettings(cleanup=True, cleanup_action="remove", min_region_area=30.0,
                                min_detail_width=3.0),
                CleanupSettings(cleanup=True, cleanup_action="widen", min_detail_width=5.0,
                                min_region_area=25.0),
                CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                min_detail_width=5.0, remove_below_width=2.0,
                                min_region_area=20.0, remove_colour="Bone")):
            with self.subTest(action=settings.cleanup_action):
                omitted = 2 if settings.remove_colour else None
                full = self.full_run(labels, automatic, explicit, settings, omitted)
                preview = clean_window(source, window, PALETTE, settings, omitted)
                self.assertEqual(preview.crop, Window(0, 0, 170, 150))
                np.testing.assert_array_equal(preview.labels, window_of(full, window))

    def test_cleanup_off_changes_nothing_and_measures_nothing(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        preview = clean_window(source, WINDOW, PALETTE, CleanupSettings(min_region_area=500.0), None)
        np.testing.assert_array_equal(preview.labels, window_of(labels, WINDOW))
        self.assertFalse(preview.removed.any() or preview.widened.any())
        self.assertFalse(source.measured)


class RegionMeasurementCacheTest(unittest.TestCase):
    def test_measurements_are_built_once_and_reused_for_any_settings(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        with mock.patch.object(cleanup_window, "measure_colour_regions",
                               wraps=measure_colour_regions) as measure:
            for area in (10.0, 240.0, 900.0):
                clean_window(source, WINDOW, PALETTE,
                             CleanupSettings(cleanup=True, min_region_area=area), None)
            clean_window(source, Window(0, 0, 100, 100), PALETTE,
                         CleanupSettings(cleanup=True, cleanup_action="widen",
                                         min_detail_width=3.0), None)
        self.assertEqual(measure.call_count, len(PALETTE))

    def test_a_cancelled_measurement_keeps_the_colours_it_finished(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        calls = []

        def checkpoint() -> None:
            # Each colour checks before it starts and again after labelling its
            # regions; the fourth check is inside the second colour.
            calls.append(1)
            if len(calls) == 4:
                raise PreviewCancelled

        settings = CleanupSettings(cleanup=True, min_region_area=240.0)
        with mock.patch.object(cleanup_window, "measure_colour_regions",
                               wraps=measure_colour_regions) as measure, \
                mock.patch.object(cutter_cleanup, "_region_peak_distances",
                                  wraps=cutter_cleanup._region_peak_distances) as distances:
            with self.assertRaises(PreviewCancelled):
                clean_window(source, WINDOW, PALETTE, settings, None, checkpoint)
            # Stopped before the second colour's distance transform.
            self.assertEqual(measure.call_count, 2)
            self.assertEqual(distances.call_count, 1)
            self.assertFalse(source.measured)
            clean_window(source, WINDOW, PALETTE, settings, None)
        self.assertEqual(measure.call_count, 2 + len(PALETTE) - 1)
        self.assertTrue(source.measured)

    def test_a_newer_request_stops_a_running_widening_between_components(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        source.regions()
        stopped_at = []

        def checkpoint() -> None:
            # Supersede the preview at the widening's first check between two
            # components: the check inside its component loop, after a component
            # has already been handled.
            frame = sys._getframe(1)
            if (frame.f_code.co_name == "cleanup_labels" and "source_field" in frame.f_locals
                    and frame.f_locals.get("component", 0) >= 2):
                stopped_at.append(frame.f_locals["component"])
                raise PreviewCancelled

        for action in ("widen", "remove_then_widen"):
            with self.subTest(action=action):
                stopped_at.clear()
                settings = CleanupSettings(cleanup=True, cleanup_action=action,
                                           min_detail_width=4.0, min_region_area=20.0)
                with self.assertRaises(PreviewCancelled):
                    clean_window(source, WINDOW, PALETTE, settings, None, checkpoint)
                self.assertEqual(stopped_at, [2])

    def test_the_checks_never_change_a_preview(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        for action in ("remove", "widen", "remove_then_widen"):
            with self.subTest(action=action):
                settings = CleanupSettings(cleanup=True, cleanup_action=action,
                                           min_detail_width=4.0, min_region_area=240.0,
                                           remove_below_width=2.0)
                calls = []
                plain = clean_window(source, WINDOW, PALETTE, settings, None)
                checked = clean_window(source, WINDOW, PALETTE, settings, None,
                                       lambda: calls.append(1))
                np.testing.assert_array_equal(checked.labels, plain.labels)
                np.testing.assert_array_equal(checked.removed, plain.removed)
                np.testing.assert_array_equal(checked.widened, plain.widened)
                self.assertGreater(len(calls), 1)

    def test_the_banded_distance_pass_equals_the_whole_field(self) -> None:
        labels, _, explicit = raster()
        with mock.patch.object(cutter_cleanup, "MEASURE_CHUNK_ROWS", 7):
            for colour in range(len(PALETTE)):
                bounds, components, count, area, width, _ = measure_colour_regions(
                    labels, colour, explicit)
                mask = labels == colour
                whole, _ = ndimage.label(mask, structure=ndimage.generate_binary_structure(2, 1))
                field = cutter_cleanup._padded_edt(mask)
                expected = 2.0 * np.asarray(ndimage.maximum(field, whole, np.arange(1, count + 1)))
                np.testing.assert_array_equal(width, expected)
                np.testing.assert_array_equal(area, np.bincount(whole.ravel())[1:])


def stroke_and_speck() -> np.ndarray:
    """A long one-pixel Ember stroke and a 2 x 2 Ember speck just below it: a
    remove-then-widen recolours the speck, then widening the stroke paints its top
    row back to Ember."""
    labels = np.zeros((HEIGHT, WIDTH), dtype=np.int16)
    labels[200, 220:340] = 1
    labels[202:204, 280:282] = 1
    return labels


class WillChangeTest(unittest.TestCase):
    def full_run(self, labels, explicit, settings) -> np.ndarray:
        cleaned, _, _, _ = apply_cleanup(labels, PALETTE, settings, None,
                                         protected_features=np.zeros_like(explicit),
                                         explicit_protected_features=explicit)
        return cleaned

    def test_each_pixel_is_classified_against_the_shown_raster(self) -> None:
        # Columns: unchanged; would be removed; would be widened; would return to its
        # uncleaned colour; the active cleanup's change kept; changed by the active
        # cleanup and would change again by removal.
        original = np.array([[0, 0, 0, 1, 1, 1]])
        current = np.array([[0, 0, 0, 2, 2, 2]])
        after_removal = np.array([[0, 3, 0, 1, 2, 3]])
        final = np.array([[0, 3, 4, 1, 2, 3]])
        removed, widened, returned = will_change(original, current, after_removal, final)
        self.assertEqual(removed.tolist(), [[False, True, False, False, False, True]])
        self.assertEqual(widened.tolist(), [[False, False, True, False, False, False]])
        self.assertEqual(returned.tolist(), [[False, False, False, True, False, False]])

    def test_nothing_will_change_after_a_run_with_the_same_settings(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        for settings in (CleanupSettings(cleanup=True, min_region_area=240.0),
                         CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                         min_detail_width=4.0, min_region_area=12.0)):
            with self.subTest(action=settings.cleanup_action):
                shown = self.full_run(labels, explicit, settings)
                preview = clean_window(source, WINDOW, PALETTE, settings, None)
                masks = will_change(preview.original, window_of(shown, WINDOW),
                                    preview.after_removal, preview.labels)
                self.assertTrue(window_of(shown != labels, WINDOW).any())
                self.assertFalse(any(mask.any() for mask in masks))

    def test_undoing_the_active_cleanup_returns_its_pixels(self) -> None:
        labels, _, explicit = raster()
        source = source_of(labels, explicit)
        shown = self.full_run(labels, explicit, CleanupSettings(cleanup=True, min_region_area=240.0))
        for settings in (CleanupSettings(cleanup=False),
                         CleanupSettings(cleanup=True, min_region_area=1.0)):
            with self.subTest(settings=settings):
                preview = clean_window(source, WINDOW, PALETTE, settings, None)
                removed, widened, returned = will_change(
                    preview.original, window_of(shown, WINDOW), preview.after_removal,
                    preview.labels)
                np.testing.assert_array_equal(returned, window_of(shown != labels, WINDOW))
                self.assertFalse(removed.any() or widened.any())


class ChangesOverlayTest(unittest.TestCase):
    def test_the_run_overlay_records_its_net_changes_by_stage(self) -> None:
        labels = stroke_and_speck()
        none = np.zeros_like(labels, dtype=bool)
        stages: dict = {}
        settings = CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                   min_detail_width=4.0, min_region_area=12.0)
        cleaned, _, _, _ = apply_cleanup(labels, PALETTE, settings, None, protected_features=none,
                                         explicit_protected_features=none, stages=stages)
        with tempfile.TemporaryDirectory(dir=".tmp") as name:
            path = Path(name) / "cleanup-changes-overlay.png"
            self.assertTrue(write_changes_overlay(path, labels, stages["after_removal"], cleaned))
            indices = np.asarray(Image.open(path))
            # Unchanged (removed, then widened back), removed, widened.
            self.assertEqual([int(indices[202, 280]), int(indices[203, 280]),
                              int(indices[201, 280])], [0, 1, 2])
            np.testing.assert_array_equal(indices > 0, cleaned != labels)
            # A run that changed nothing removes any earlier overlay.
            self.assertFalse(write_changes_overlay(path, labels, labels, labels))
            self.assertFalse(path.exists())

    def test_the_run_overlay_is_pixel_exact_over_the_whole_image(self) -> None:
        rng = np.random.default_rng(3)
        labels = np.repeat(np.repeat(rng.integers(0, 4, size=(40, 50)), 3, axis=0), 3, axis=1)
        labels = labels.astype(np.int16)
        labels[30:31, 5:140] = 2
        labels[60:100, 70:71] = 3
        none = np.zeros_like(labels, dtype=bool)
        included = list(range(len(PALETTE)))
        for settings in (CleanupSettings(cleanup=True, cleanup_action="widen", min_detail_width=5.0,
                                         min_region_area=20.0),
                         CleanupSettings(cleanup=True, cleanup_action="remove_then_widen",
                                         min_detail_width=5.0, remove_below_width=2.0,
                                         min_region_area=20.0)):
            with self.subTest(action=settings.cleanup_action):
                stages: dict = {}
                cleaned, _, _, _ = apply_cleanup(labels, PALETTE, settings, None, protected_features=none,
                                                 explicit_protected_features=none, stages=stages)
                # The stages run on their own, independently of ``stages``.
                if settings.cleanup_action == "widen":
                    after_removal = labels
                else:
                    remove, _ = remove_then_widen_stages(settings)
                    after_removal, _ = cleanup_labels(labels, included, PALETTE, remove, 1.0,
                                                      explicit=none)
                changed = cleaned != labels
                widened = changed & (cleaned != after_removal)
                expected = np.where(widened, 2, np.where(changed, 1, 0))
                self.assertTrue(widened.any())
                with tempfile.TemporaryDirectory(dir=".tmp") as name:
                    path = Path(name) / "cleanup-changes-overlay.png"
                    write_changes_overlay(path, labels, stages["after_removal"], cleaned)
                    np.testing.assert_array_equal(np.asarray(Image.open(path)), expected)

if __name__ == "__main__":
    unittest.main()
