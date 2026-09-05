import importlib.util
import os
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely

from vinylgen.boundary_graph import shared_boundary_polygons
from vinylgen.contracts import SegmentationBundle

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_PATH = Path(__file__).resolve().parent / "oracles" / "boundary_graph_reference.py"
_CORPUS_ROOT = _REPO_ROOT / ".tmp" / "stroke-baseline" / "after"
_SOURCEUP_NPZ = _REPO_ROOT / "outputs" / "sourceup" / "segmentation.npz"

_CORPUS_SUBJECTS = (
    "cosmic-jellyfish", "crashed-ring", "crashed-spacecraft", "desert-mech",
    "desert-rocket", "floating-castle", "griffin-castle", "kraken-lighthouse",
    "sky-whale-city",
)


def _require_slow_tests() -> None:
    """Real-image oracle checks take minutes; they run only when asked for."""
    if os.environ.get("VINYLGEN_SLOW_TESTS") != "1":
        raise unittest.SkipTest("set VINYLGEN_SLOW_TESTS=1 to run real-image oracle checks")


def _load_reference():
    """Import the frozen oracle module without polluting the package namespace."""
    spec = importlib.util.spec_from_file_location(
        "boundary_graph_reference", _REFERENCE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _chain_fields(chain):
    """Order-stable tuple of every field, anchors decomposed to plain values."""
    return (
        chain.identity, chain.pair, chain.left_colour, chain.right_colour,
        chain.points, chain.source_points,
        tuple((anchor.point, anchor.kind) for anchor in chain.anchors),
        chain.protected_source_edges, chain.closed,
    )


def _assert_equal(test, produced, expected):
    """Bit-identical equality of the whole result triple across two modules.

    Dataclasses from two independently imported modules never compare equal by
    ``==`` (dataclass equality demands identical classes), so every field is
    compared by value; geometry is checked both by exact equality and raw WKB.
    """
    geometry, track, diagnostics = produced
    ref_geometry, ref_track, ref_diagnostics = expected

    test.assertEqual(diagnostics, ref_diagnostics)

    test.assertEqual(len(track.chains), len(ref_track.chains))
    for chain, ref_chain in zip(track.chains, ref_track.chains):
        test.assertEqual(_chain_fields(chain), _chain_fields(ref_chain))

    test.assertEqual(set(geometry), set(ref_geometry))
    for colour, shape in geometry.items():
        ref_shape = ref_geometry[colour]
        test.assertTrue(shapely.equals_exact(shape, ref_shape, 0),
                        f"colour {colour} geometry differs")
        test.assertEqual(shape.wkb, ref_shape.wkb, f"colour {colour} WKB differs")


def _random_labels(rng, kind, height, width, colours):
    if kind == "uniform":
        return rng.integers(0, colours, size=(height, width)).astype(np.int16)
    if kind == "checkerboard":
        grid = (np.indices((height, width)).sum(0) % colours)
        return grid.astype(np.int16)
    if kind == "blobs":
        coarse_h = max(1, -(-height // 4))
        coarse_w = max(1, -(-width // 4))
        coarse = rng.integers(0, colours, size=(coarse_h, coarse_w))
        grown = np.kron(coarse, np.ones((4, 4), dtype=coarse.dtype))
        return grown[:height, :width].astype(np.int16)
    if kind == "isolated":
        labels = np.zeros((height, width), dtype=np.int16)
        specks = rng.integers(0, height * width + 1)
        for _ in range(int(specks)):
            y = int(rng.integers(0, height))
            x = int(rng.integers(0, width))
            labels[y, x] = int(rng.integers(0, colours))
        return labels
    raise ValueError(kind)


class BoundaryGraphFastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reference = _load_reference()

    def _compare(self, labels, included, palette_costs, protected_features):
        expected = self.reference.shared_boundary_polygons(
            labels.copy(), included, palette_costs,
            protected_features=protected_features)
        produced = shared_boundary_polygons(
            labels.copy(), included, palette_costs,
            protected_features=protected_features)
        _assert_equal(self, produced, expected)

    def test_fuzz_matches_reference(self):
        rng = np.random.default_rng(20240905)
        kinds = ("uniform", "blobs", "checkerboard", "isolated")
        cases = 0
        for index in range(320):
            kind = kinds[index % len(kinds)]
            if kind == "checkerboard":
                height = int(rng.integers(1, 41))
                width = int(rng.integers(1, 61))
            else:
                height = int(rng.integers(1, 41))
                width = int(rng.integers(1, 61))
            colours = int(rng.integers(2, 7))
            labels = _random_labels(rng, kind, height, width, colours)
            used_max = int(labels.max())
            palette = used_max + 1 + int(rng.integers(0, 2))
            dtype = np.float32 if index % 2 else np.float64
            palette_costs = rng.random((height, width, palette)).astype(dtype)
            if index % 5 == 0:
                # Force denominator ties so the crossing clamps to the midpoint.
                palette_costs[:] = np.float32(1.0) if dtype is np.float32 else 1.0
            elif index % 7 == 0:
                # Non-finite costs: NaN crossings clamp to the lower bound and
                # infinite ones to whichever bound the sign selects.
                specials = rng.random(palette_costs.shape) < 0.05
                palette_costs[specials] = np.nan
                palette_costs[rng.random(palette_costs.shape) < 0.02] = np.inf
            protected_features = None
            if index % 3:
                protected_features = rng.random((height, width)) < 0.3
            included = list(range(palette))
            with self.subTest(case=index, kind=kind, shape=(height, width)):
                self._compare(labels, included, palette_costs, protected_features)
            cases += 1
        self.assertGreaterEqual(cases, 300)

    def test_corpus_subjects_match_reference(self):
        _require_slow_tests()
        if not _CORPUS_ROOT.is_dir():
            raise unittest.SkipTest(f"corpus baseline absent at {_CORPUS_ROOT}")
        checked = 0
        for subject in _CORPUS_SUBJECTS:
            npz = _CORPUS_ROOT / subject / "segmentation.npz"
            if not npz.exists():
                continue
            bundle = SegmentationBundle.load(npz)
            included = list(range(bundle.palette_rgb.shape[0]))
            with self.subTest(subject=subject):
                self._compare(bundle.labels, included, bundle.palette_costs,
                              bundle.protected_features)
            checked += 1
        if not checked:
            raise unittest.SkipTest("no corpus segmentation bundles present")

    def test_sourceup_matches_reference(self):
        _require_slow_tests()
        if not _SOURCEUP_NPZ.exists():
            raise unittest.SkipTest(f"sourceup bundle absent at {_SOURCEUP_NPZ}")
        bundle = SegmentationBundle.load(_SOURCEUP_NPZ)
        included = list(range(bundle.palette_rgb.shape[0]))
        self._compare(bundle.labels, included, bundle.palette_costs,
                      bundle.protected_features)


if __name__ == "__main__":
    unittest.main()
