import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

import numpy as np

from vinylgen.contracts import SegmentationBundle, TraceSettings
from vinylgen.cutter_cleanup import cleanup_labels

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_PATH = Path(__file__).resolve().parent / "oracles" / "cutter_cleanup_reference.py"
_OVERLAP_ROOT = _REPO_ROOT / ".tmp" / "trace-baseline" / "before" / "overlap"
_SOURCEUP = _REPO_ROOT / "outputs" / "sourceup"

_CORPUS_SETTINGS = dict(overlap=0.4, width_mm=150.0, cleanup=True,
                        cleanup_action="remove_then_widen", min_detail_width=0.6,
                        min_region_area=1.5, remove_below_width=0.3)


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
        self.assertEqual(got_summary, want_summary)

    def test_fuzz_matches_reference(self):
        rng = np.random.default_rng(20260905)
        actions = ("remove", "widen", "remove_then_widen")
        refused_seen = 0
        cases = 0
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
            settings = TraceSettings(
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
                self.assertEqual(got_summary, want_summary)
            if any("constrained by neighbour threshold" in w
                   for w in want_summary["warnings"]):
                refused_seen += 1
            cases += 1
        self.assertGreaterEqual(cases, 300)
        self.assertGreater(refused_seen, 0, "no widening-refusal case was exercised")

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
            settings = TraceSettings(**_CORPUS_SETTINGS)
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
        settings = TraceSettings(**_CORPUS_SETTINGS)
        upp = settings.width_mm / width
        self._compare(bundle.labels, included, palette, settings, upp)


if __name__ == "__main__":
    unittest.main()
