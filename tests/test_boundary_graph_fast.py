import functools
import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np
import shapely

from vinylgen.boundary_graph import shared_boundary_polygons

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REFERENCE_PATH = Path(__file__).resolve().parent / "oracles" / "boundary_graph_reference.py"


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


_FUZZ_CASES = 320
# The fuzz cases are split across shards so a parallel run spreads the slow
# reference comparisons over several workers. Every shard regenerates the same
# seeded sequence (cheap) and compares only its own indices, so together the
# shards check exactly the full case list.
_FUZZ_SHARDS = 8


@functools.cache
def _reference():
    return _load_reference()


def _fuzz_cases():
    """Yield every seeded fuzz case in a fixed order: (index, kind, inputs)."""
    rng = np.random.default_rng(20240905)
    kinds = ("uniform", "blobs", "checkerboard", "isolated")
    for index in range(_FUZZ_CASES):
        kind = kinds[index % len(kinds)]
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
        yield index, kind, (labels, included, palette_costs, protected_features)


def _compare(test, labels, included, palette_costs, protected_features):
    """Shared by the fuzz cases here and the real-image bucket's corpus checks."""
    expected = _reference().shared_boundary_polygons(
        labels.copy(), included, palette_costs,
        protected_features=protected_features)
    produced = shared_boundary_polygons(
        labels.copy(), included, palette_costs,
        protected_features=protected_features)
    _assert_equal(test, produced, expected)


class BoundaryGraphFastTests(unittest.TestCase):
    def _fuzz_shard(self, shard):
        checked = 0
        for index, kind, inputs in _fuzz_cases():
            # Deal consecutive blocks of four (one of each kind, both dtypes)
            # round-robin so every shard gets the same mix of case costs.
            if (index // 4) % _FUZZ_SHARDS != shard:
                continue
            with self.subTest(case=index, kind=kind, shape=inputs[0].shape):
                _compare(self, *inputs)
            checked += 1
        self.assertEqual(checked, _FUZZ_CASES // _FUZZ_SHARDS)


for _shard in range(_FUZZ_SHARDS):
    setattr(BoundaryGraphFastTests, f"test_fuzz_matches_reference_{_shard}",
            lambda self, shard=_shard: self._fuzz_shard(shard))


if __name__ == "__main__":
    unittest.main()
