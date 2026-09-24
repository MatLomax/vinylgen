import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy import ndimage

from vinylgen.cleanup_window import CleanupSource
from vinylgen.contracts import CleanupSettings, SegmentationBundle
from vinylgen.cutter_cleanup import apply_cleanup, cleanup_labels

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_PATH = Path(__file__).resolve().parent / "oracles" / "cutter_cleanup_reference.py"
_OVERLAP_ROOT = _REPO_ROOT / ".tmp" / "trace-baseline" / "before" / "overlap"
_SOURCEUP = _REPO_ROOT / "outputs" / "sourceup"

_CORPUS_SETTINGS = dict(width_mm=150.0, cleanup=True,
                        cleanup_action="remove_then_widen", min_detail_width=0.6,
                        min_region_area=1.5, remove_below_width=0.3)


def _without_protection_counts(summary: dict) -> dict:
    """The summary minus the explicit-protection count, which the frozen reference
    (written before explicit protection) does not report; it must be zero here."""
    assert summary["protected_kept_regions"] == 0
    trimmed = {key: value for key, value in summary.items() if key != "protected_kept_regions"}
    if "stages" in trimmed:
        trimmed["stages"] = {name: _without_protection_counts(stage)
                             for name, stage in trimmed["stages"].items()}
    return trimmed


def _require_slow_tests() -> None:
    """Real-image oracle checks take minutes; they run only when asked for."""
    if os.environ.get("VINYLGEN_SLOW_TESTS") != "1":
        raise unittest.SkipTest("set VINYLGEN_SLOW_TESTS=1 to run real-image oracle checks")


def _load_reference():
    """Import the frozen oracle module without polluting the package namespace."""
    spec = importlib.util.spec_from_file_location("cutter_cleanup_reference", _REFERENCE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _random_palette(rng, colours):
    names = ("Ink", "Bone", "Rust", "Moss", "Slate", "Amber", "Wine", "Fog")
    palette = []
    for index in range(colours):
        r, g, b = (int(v) for v in rng.integers(0, 256, size=3))
        palette.append({"name": names[index % len(names)] + str(index),
                        "hex": f"#{r:02x}{g:02x}{b:02x}", "rgb": [r, g, b]})
    return palette


def _random_labels(rng, height, width, colours):
    """A blob field overlaid with thin strokes, specks and diagonal-touching
    pairs of the same colour, sometimes running to the image border."""
    coarse_h = max(1, -(-height // 5))
    coarse_w = max(1, -(-width // 5))
    coarse = rng.integers(0, colours, size=(coarse_h, coarse_w))
    grown = np.kron(coarse, np.ones((5, 5), dtype=coarse.dtype))
    labels = grown[:height, :width].astype(np.int16)

    for _ in range(int(rng.integers(0, 5))):
        colour = int(rng.integers(0, colours))
        thickness = int(rng.integers(1, 3))
        if rng.random() < 0.5:
            row = int(rng.integers(0, height))
            labels[row:min(height, row + thickness), :] = colour
        else:
            col = int(rng.integers(0, width))
            labels[:, col:min(width, col + thickness)] = colour

    for _ in range(int(rng.integers(0, height * width // 4 + 1))):
        y = int(rng.integers(0, height))
        x = int(rng.integers(0, width))
        labels[y, x] = int(rng.integers(0, colours))

    # Deliberate diagonal touches of one colour separated by another colour.
    for _ in range(int(rng.integers(0, 3))):
        if height < 2 or width < 2:
            break
        y = int(rng.integers(0, height - 1))
        x = int(rng.integers(0, width - 1))
        colour = int(rng.integers(0, colours))
        other = int(rng.integers(0, colours))
        labels[y, x] = colour
        labels[y + 1, x + 1] = colour
        labels[y, x + 1] = other
        labels[y + 1, x] = other
    return labels


def _thin_band_case():
    """A thin source stroke bordered by an equally thin neighbour band, so the
    widen step must refuse rather than starve the neighbour."""
    labels = np.zeros((9, 12), dtype=np.int16)
    labels[3, :] = 1
    labels[4, :] = 2
    labels[5, :] = 1
    return labels


def _remove_choice_shapes(choices) -> dict:
    """Count the Remove choices the oracle made that exercise each close-call shape."""
    shapes = dict.fromkeys(("several candidates", "shorter edge within 90% won",
                            "closer colour outside 90% lost", "exact tie"), 0)
    for shared_edge, distances in choices:
        longest = max(shared_edge.values())
        close = [value for value in shared_edge if 10 * shared_edge[value] >= 9 * longest]
        target = min(close, key=lambda value: (distances[value], value))
        shapes["several candidates"] += len(shared_edge) > 1
        shapes["shorter edge within 90% won"] += shared_edge[target] < longest
        shapes["closer colour outside 90% lost"] += any(
            10 * shared_edge[value] < 9 * longest and distances[value] < distances[target]
            for value in shared_edge)
        shapes["exact tie"] += sum(distances[value] == distances[target] for value in close) > 1
    return shapes


class CutterCleanupFastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = _load_reference()

    def _compare(self, labels, included, palette, settings, upp):
        want_labels, want_summary = self.reference.cleanup_labels(
            labels.copy(), included, palette, settings, upp)
        got_labels, got_summary = cleanup_labels(
            labels.copy(), included, palette, settings, upp)
        self.assertEqual(got_labels.dtype, want_labels.dtype)
        self.assertTrue(np.array_equal(got_labels, want_labels), "labels differ")
        self.assertEqual(_without_protection_counts(got_summary), want_summary)

    def test_fuzz_matches_reference(self):
        rng = np.random.default_rng(20260905)
        actions = ("remove", "widen", "remove_then_widen")
        refused_seen = 0
        cases = 0
        self.reference.remove_choices.clear()
        for index in range(320):
            height = int(rng.integers(1, 49))
            width = int(rng.integers(1, 65))
            colours = int(rng.integers(2, 6))
            labels = _random_labels(rng, height, width, colours) if index % 17 else _thin_band_case()
            palette = _random_palette(rng, max(colours, int(labels.max()) + 1))
            drop = index % 11 == 0 and colours > 2
            included = [c for c in range(len(palette)) if not (drop and c == 0)]
            action = actions[index % len(actions)]

            upp = float(rng.uniform(0.3, 2.0))
            width_px_target = float(rng.uniform(0.5, 6.0))
            area_px_target = float(rng.uniform(0.0, 20.0))
            settings = CleanupSettings(
                cleanup=True, cleanup_action=action,
                min_detail_width=width_px_target * upp,
                min_region_area=area_px_target * upp * upp,
                remove_below_width=width_px_target * 0.5 * upp)

            with self.subTest(case=index, action=action, shape=(height, width)):
                want_labels, want_summary = self.reference.cleanup_labels(
                    labels.copy(), included, palette, settings, upp)
                got_labels, got_summary = cleanup_labels(
                    labels.copy(), included, palette, settings, upp)
                self.assertEqual(got_labels.dtype, want_labels.dtype)
                self.assertTrue(np.array_equal(got_labels, want_labels), "labels differ")
                self.assertEqual(_without_protection_counts(got_summary), want_summary)
            if any("constrained by neighbour threshold" in w
                   for w in want_summary["warnings"]):
                refused_seen += 1
            cases += 1
        self.assertGreaterEqual(cases, 300)
        self.assertGreater(refused_seen, 0, "no widening-refusal case was exercised")
        shapes = _remove_choice_shapes(self.reference.remove_choices)
        for shape in ("several candidates", "shorter edge within 90% won",
                      "closer colour outside 90% lost", "exact tie"):
            self.assertGreater(shapes[shape], 0, f"no Remove choice with {shape} was exercised")

    def test_remove_with_whole_raster_regions_matches_the_full_run_on_crops(self):
        """``regions`` measured on the whole raster, with explicit protection: on the
        whole-raster crop Remove equals the full run exactly, and on a random crop it
        equals it at every pixel outside the groups that reach the crop's edge."""
        rng = np.random.default_rng(20260924)
        structure = ndimage.generate_binary_structure(2, 1)
        protected_seen = partial_seen = 0
        for index in range(150):
            height = int(rng.integers(2, 41))
            width = int(rng.integers(2, 49))
            colours = int(rng.integers(2, 6))
            labels = _random_labels(rng, height, width, colours)
            palette = _random_palette(rng, colours)
            explicit = rng.random(labels.shape) < float(rng.uniform(0.0, 0.03))
            included = [c for c in range(colours) if not (index % 7 == 0 and c == 0)]
            # Every third case uses whole-pixel thresholds, so regions exactly at a
            # threshold occur.
            exact = index % 3 == 0
            upp = 1.0 if exact else float(rng.uniform(0.5, 2.0))
            settings = CleanupSettings(
                cleanup=True, cleanup_action="remove",
                min_detail_width=(float(rng.integers(0, 6)) if exact
                                  else float(rng.uniform(0.0, 5.0)) * upp),
                min_region_area=(float(rng.integers(0, 21)) if exact
                                 else float(rng.uniform(0.0, 20.0)) * upp * upp))
            source = CleanupSource(labels=labels, explicit=explicit,
                                   palette_rgb=np.asarray([row["rgb"] for row in palette],
                                                          dtype=np.uint8))
            regions = source.regions()
            full, full_summary = cleanup_labels(labels, included, palette, settings, upp,
                                                explicit=explicit)
            whole, whole_summary = cleanup_labels(labels, included, palette, settings, upp,
                                                  explicit=explicit, regions=regions)
            y0, x0 = int(rng.integers(0, height)), int(rng.integers(0, width))
            y1, x1 = int(rng.integers(y0 + 1, height + 1)), int(rng.integers(x0 + 1, width + 1))
            rows, columns = slice(y0, y1), slice(x0, x1)
            cropped, _ = cleanup_labels(np.ascontiguousarray(labels[rows, columns]), included,
                                        palette, settings, upp,
                                        explicit=np.ascontiguousarray(explicit[rows, columns]),
                                        regions=regions.crop(rows, columns))
            # The undersized, unprotected groups of the whole raster; a group with a
            # four-neighbour outside the crop is split by it and may choose otherwise.
            undersized = ((regions.area < settings.min_region_area / (upp * upp)) |
                          (regions.width < settings.min_detail_width / upp))
            small = (undersized & ~regions.explicit)[regions.ids] & np.isin(labels, included)
            groups, _ = ndimage.label(small, structure=structure)
            outside = np.ones(labels.shape, dtype=bool)
            outside[rows, columns] = False
            reaching = np.unique(groups[ndimage.binary_dilation(outside, structure=structure)
                                        & ~outside & (groups > 0)])
            compared = ~np.isin(groups, reaching[reaching > 0])[rows, columns]
            with self.subTest(case=index, shape=(height, width), crop=(y0, y1, x0, x1)):
                np.testing.assert_array_equal(whole, full)
                self.assertEqual(whole_summary, full_summary)
                np.testing.assert_array_equal(cropped[compared], full[rows, columns][compared])
            protected_seen += full_summary["protected_kept_regions"] > 0
            partial_seen += bool((groups[rows, columns] > 0)[compared].any() and
                                 (y1 - y0, x1 - x0) != (height, width))
        self.assertGreater(protected_seen, 0, "no explicitly protected region was kept")
        self.assertGreater(partial_seen, 0, "no crop held a group clear of its edge")

    def test_a_checkpoint_never_changes_the_result(self):
        rng = np.random.default_rng(20260924)
        actions = ("remove", "widen", "remove_then_widen")
        checked = 0
        for index in range(60):
            height = int(rng.integers(4, 49))
            width = int(rng.integers(4, 65))
            colours = int(rng.integers(2, 6))
            labels = _random_labels(rng, height, width, colours)
            palette = _random_palette(rng, max(colours, int(labels.max()) + 1))
            included = list(range(len(palette)))
            settings = CleanupSettings(
                cleanup=True, cleanup_action=actions[index % len(actions)],
                min_detail_width=float(rng.uniform(0.5, 6.0)),
                min_region_area=float(rng.uniform(0.0, 20.0)),
                remove_below_width=float(rng.uniform(0.0, 3.0)))
            calls = []
            with self.subTest(case=index, action=settings.cleanup_action):
                want_labels, want_summary = cleanup_labels(
                    labels.copy(), included, palette, settings, 1.0)
                got_labels, got_summary = cleanup_labels(
                    labels.copy(), included, palette, settings, 1.0,
                    checkpoint=lambda: calls.append(1))
                np.testing.assert_array_equal(got_labels, want_labels)
                self.assertEqual(got_summary, want_summary)
                self.assertGreater(len(calls), 0)
            checked += 1
        self.assertEqual(checked, 60)

    def test_corpus_subjects_match_reference(self):
        _require_slow_tests()
        subjects = ("desert-rocket", "kraken-lighthouse", "cosmic-jellyfish")
        checked = 0
        for subject in subjects:
            directory = _OVERLAP_ROOT / subject
            npz = directory / "segmentation.npz"
            palette_path = directory / "palette.json"
            if not npz.exists() or not palette_path.exists():
                continue
            bundle = SegmentationBundle.load(npz)
            palette = json.loads(palette_path.read_text())["palette"]
            included = list(range(len(palette)))
            height, width = bundle.labels.shape
            settings = CleanupSettings(**_CORPUS_SETTINGS)
            upp = settings.width_mm / width
            with self.subTest(subject=subject):
                self._compare(bundle.labels, included, palette, settings, upp)
            checked += 1
        if not checked:
            raise unittest.SkipTest(f"overlap baseline absent at {_OVERLAP_ROOT}")

    def test_sourceup_matches_reference(self):
        _require_slow_tests()
        npz = _SOURCEUP / "segmentation.npz"
        palette_path = _SOURCEUP / "palette.json"
        if not npz.exists() or not palette_path.exists():
            raise unittest.SkipTest(f"sourceup bundle absent at {_SOURCEUP}")
        bundle = SegmentationBundle.load(npz)
        palette = json.loads(palette_path.read_text())["palette"]
        included = list(range(len(palette)))
        height, width = bundle.labels.shape
        settings = CleanupSettings(**_CORPUS_SETTINGS)
        upp = settings.width_mm / width
        self._compare(bundle.labels, included, palette, settings, upp)



_PALETTE = [
    {"rgb": [0, 0, 0], "name": "Black"},
    {"rgb": [230, 100, 20], "name": "Orange"},
    {"rgb": [240, 220, 160], "name": "Cream"},
]


class CleanupProtectionTests(unittest.TestCase):
    """The user's explicit protection wins over cleanup; automatic protection lapses
    where the cleanup changed the raster."""

    def specks(self) -> np.ndarray:
        labels = np.zeros((12, 16), dtype=np.int16)
        labels[2, 2:4] = 1      # a two-pixel speck
        labels[8, 10] = 2       # a one-pixel speck
        return labels

    def test_remove_keeps_an_explicitly_protected_undersized_region_whole(self):
        labels = self.specks()
        settings = CleanupSettings(cleanup=True, cleanup_action="remove", min_region_area=4)
        unprotected, summary = cleanup_labels(labels, [0, 1, 2], _PALETTE, settings, 1.0)
        self.assertFalse(np.any(unprotected != 0))
        self.assertEqual(summary["protected_kept_regions"], 0)
        explicit = np.zeros(labels.shape, dtype=bool)
        explicit[2, 3] = True   # one protected pixel protects the whole region
        kept, summary = cleanup_labels(labels, [0, 1, 2], _PALETTE, settings, 1.0, explicit=explicit)
        self.assertTrue(np.all(kept[2, 2:4] == 1))
        self.assertEqual(kept[8, 10], 0)
        self.assertEqual(summary["protected_kept_regions"], 1)
        self.assertEqual(summary["removed_regions"], 1)

    def test_remove_then_widen_keeps_explicit_protection_through_its_remove_half(self):
        labels = self.specks()
        explicit = np.zeros(labels.shape, dtype=bool)
        explicit[8, 10] = True
        cleaned, summary = cleanup_labels(labels, [0, 1, 2], _PALETTE, CleanupSettings(
            cleanup=True, cleanup_action="remove_then_widen", min_region_area=4,
            min_detail_width=3), 1.0, explicit=explicit)
        self.assertEqual(cleaned[8, 10], 2)
        self.assertEqual(summary["protected_kept_regions"], 1)
        self.assertEqual(summary["stages"]["remove_below_area"]["protected_kept_regions"], 1)

    def test_widening_a_neighbour_never_consumes_an_explicitly_protected_pixel(self):
        labels = np.zeros((15, 20), dtype=np.int16)
        labels[7, 2:18] = 1     # a one-pixel line widened into the background
        settings = CleanupSettings(cleanup=True, cleanup_action="widen", min_detail_width=3)
        grown, _ = cleanup_labels(labels, [0, 1], _PALETTE[:2], settings, 1.0)
        self.assertTrue(np.any(grown[6, 2:18] == 1))
        explicit = np.zeros(labels.shape, dtype=bool)
        explicit[6, 2:18] = True
        guarded, summary = cleanup_labels(labels, [0, 1], _PALETTE[:2], settings, 1.0, explicit=explicit)
        self.assertTrue(np.all(guarded[6, 2:18] == 0))
        self.assertTrue(np.any(guarded[8, 2:18] == 1))
        self.assertGreater(summary["widened_pixels"], 0)

    def test_apply_cleanup_clears_automatic_protection_only_where_labels_changed(self):
        labels = self.specks()
        protected = np.zeros(labels.shape, dtype=bool)
        protected[2, 2:4] = True        # automatic, on the removed two-pixel speck
        protected[8, 10] = True         # explicit, on the one-pixel speck
        protected[5, 5:8] = True        # automatic, on untouched background
        explicit = np.zeros(labels.shape, dtype=bool)
        explicit[8, 10] = True
        cleaned, after, summary, _ = apply_cleanup(
            labels, _PALETTE, CleanupSettings(cleanup=True, min_region_area=4), None,
            protected_features=protected, explicit_protected_features=explicit)
        self.assertTrue(np.all(cleaned[2, 2:4] == 0))
        self.assertEqual(cleaned[8, 10], 2)
        expected = protected.copy()
        expected[2, 2:4] = False
        np.testing.assert_array_equal(after, expected)
        self.assertEqual(summary["auto_protected_components_cleared"], 1)
        self.assertEqual(summary["auto_protected_pixels_cleared"], 2)
        self.assertEqual(summary["protected_kept_regions"], 1)


class CleanupProtectionAuditTests(unittest.TestCase):
    def test_widening_fully_blocked_by_explicit_protection_is_a_warned_refusal(self):
        labels = np.ones((20, 20), dtype=np.int16)
        labels[2:18, 10] = 0
        explicit = labels == 1
        out, protected, summary, _ = apply_cleanup(
            labels, _PALETTE[:2], CleanupSettings(cleanup=True, cleanup_action="widen",
                                                  min_detail_width=3), None,
            protected_features=explicit.copy(), explicit_protected_features=explicit)
        np.testing.assert_array_equal(out, labels)
        self.assertEqual((summary["widened_regions"], summary["widened_pixels"]), (0, 0))
        self.assertEqual(summary["unchanged_regions"], 1)
        self.assertTrue(any("blocked by explicitly protected pixels" in warning
                            for warning in summary["warnings"]))

    def test_a_diagonal_automatic_detail_counts_as_one_component(self):
        labels = np.zeros((20, 20), dtype=np.int16)
        for step in range(5):
            labels[5 + step, 5 + step] = 1
        automatic = labels == 1
        _, protected, summary, _ = apply_cleanup(
            labels, _PALETTE, CleanupSettings(cleanup=True, min_region_area=4), None,
            protected_features=automatic, explicit_protected_features=np.zeros_like(automatic))
        self.assertFalse(protected.any())
        self.assertEqual(summary["auto_protected_components_cleared"], 1)
        self.assertEqual(summary["auto_protected_pixels_cleared"], 5)


# Remove's target fixtures: the removed speck is Red; Rust is the closest-looking
# candidate, Wine the next and Blue far off.
_TARGET_PALETTE = [
    {"rgb": [0, 0, 255], "name": "Blue"},
    {"rgb": [200, 0, 40], "name": "Wine"},
    {"rgb": [250, 10, 10], "name": "Rust"},
    {"rgb": [255, 0, 0], "name": "Red"},
]
_BLUE, _WINE, _RUST, _RED = range(4)


def _run_remove(labels: np.ndarray, min_region_area: float) -> tuple[np.ndarray, dict]:
    settings = CleanupSettings(cleanup=True, cleanup_action="remove",
                               min_region_area=min_region_area)
    return cleanup_labels(labels, list(range(len(_TARGET_PALETTE))), _TARGET_PALETTE,
                          settings, 1.0)


def _striped_speck(length: int, wine_edge: int) -> np.ndarray:
    """A one-pixel-high Red speck along row 5 from the left image edge: Blue above
    all of it, Wine below its first ``wine_edge`` pixels, Rust below the rest and at
    its right end."""
    labels = np.full((12, length + 5), _WINE, dtype=np.int16)
    labels[:5, :] = _BLUE
    labels[5:, wine_edge:] = _RUST
    labels[5, :length] = _RED
    return labels


class RemoveTargetTests(unittest.TestCase):
    """A removed group takes the colour it shares the most edge with; colours within
    90% of that edge are a close call settled by Lab distance, then palette index."""

    def test_a_speck_mostly_enclosed_by_one_colour_takes_it_over_a_closer_colour(self):
        labels = np.full((7, 7), _BLUE, dtype=np.int16)
        labels[:, 4:] = _RUST
        labels[3, 3] = _RED     # Blue on three sides, Rust on one
        cleaned, summary = _run_remove(labels, 4)
        self.assertEqual(int(cleaned[3, 3]), _BLUE)
        self.assertEqual(summary["removed_regions"], 1)

    def test_candidates_within_ten_percent_of_the_longest_edge_go_to_the_closer_colour(self):
        # Blue shares 10 pairs, Wine 9 (90%), Rust 2: Wine is closer than Blue.
        cleaned, _ = _run_remove(_striped_speck(10, 9), 20)
        self.assertTrue((cleaned[5, :10] == _WINE).all())
        # Exactly 90%: Blue 20, Wine 18.
        cleaned, _ = _run_remove(_striped_speck(20, 18), 25)
        self.assertTrue((cleaned[5, :20] == _WINE).all())

    def test_a_candidate_just_outside_ten_percent_loses_to_the_longest_edge(self):
        # Blue shares 20 pairs, Wine 17 (85%), Rust 4.
        cleaned, _ = _run_remove(_striped_speck(20, 17), 25)
        self.assertTrue((cleaned[5, :20] == _BLUE).all())

    def test_an_exact_close_call_goes_to_the_lower_palette_index(self):
        palette = [dict(_TARGET_PALETTE[_WINE], name="Wine2"), *_TARGET_PALETTE[1:]]
        labels = _striped_speck(10, 10)     # palette 0 and 1 both Wine, 10 pairs each
        labels[labels == _BLUE] = 0
        settings = CleanupSettings(cleanup=True, cleanup_action="remove", min_region_area=20)
        cleaned, _ = cleanup_labels(labels, [0, 1, 2, 3], palette, settings, 1.0)
        self.assertTrue((cleaned[5, :10] == 0).all())

    def test_a_multi_colour_group_counts_the_edge_of_the_whole_group(self):
        labels = np.full((12, 12), _WINE, dtype=np.int16)
        labels[:6, :] = _BLUE
        labels[5, 0:2] = _RED       # alone: 2 Blue pairs, 2 Wine
        labels[5, 2:6] = _RUST      # alone: 5 Blue pairs (4 above, 1 right), 4 Wine
        # As one group: Blue 7 pairs, Wine 6 (86%). Wine is closer to its average colour.
        cleaned, summary = _run_remove(labels, 5)
        self.assertTrue((cleaned[5, :6] == _BLUE).all())
        self.assertEqual(summary["removed_regions"], 2)
        self.assertEqual(summary["removed_pixels"], 6)

    def test_a_border_pixel_touching_the_group_on_two_sides_counts_twice(self):
        labels = np.full((12, 12), _WINE, dtype=np.int16)
        labels[6:, :5] = _RUST
        labels[6:, 6:] = _BLUE
        labels[5, 7:] = _BLUE
        labels[7:, 5] = _BLUE
        labels[5, 5:7] = _RED
        labels[6, 5] = _RED
        # The Red L's notch (6, 6) is Blue and touches it twice: Blue 4 pairs from 3
        # border pixels, Wine 3 from 3, Rust 1. Counting border pixels would make
        # Blue and Wine a close call that Wine wins.
        cleaned, _ = _run_remove(labels, 4)
        self.assertTrue((cleaned[[5, 5, 6], [5, 6, 5]] == _BLUE).all())


if __name__ == "__main__":
    unittest.main()
