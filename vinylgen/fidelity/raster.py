"""Deterministic supersampled coverage rasteriser for vector scenes.

Every pixel is sampled on an ``s`` x ``s`` grid of subsample points at
``((i + 0.5) / s, (j + 0.5) / s)`` inside the pixel. A subsample takes the colour
of the topmost region containing it (the background where none does); region
membership is even-odd over the region's rings flattened to
``FLATTEN_TOLERANCE`` chord error.

Membership is computed by scanline parity rather than per-point tests: each
flattened edge is intersected with the subsample rows it spans (half-open in y,
so a vertex on a row is counted once), and each crossing flips the parity of
every subsample to its right. Rows are processed in bands to bound memory. All
arithmetic is plain float64/integer numpy with a fixed order, so identical
scenes give byte-identical arrays in any process.
"""

from __future__ import annotations

import numpy as np

from vinylgen.fidelity.scene import FLATTEN_TOLERANCE, Scene

# Subsamples held per band (sets the band height).
_BAND_SUBSAMPLES = 1 << 22


def _region_crossings(region, supersample: int, width: int, height: int):
    """Subsample-row indices and parity-flip columns of a region's edges.

    A crossing at ``x`` flips subsample columns whose centre lies strictly right
    of ``x``: the first is ``floor(x * s - 0.5) + 1``, clamped to
    ``[0, width * s]`` (column ``width * s`` lies beyond the canvas).
    """
    s = supersample
    rows, cols = [], []
    for ring in region.rings:
        points = ring.flatten(FLATTEN_TOLERANCE)
        x0, y0 = points[:, 0], points[:, 1]
        x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
        keep = y0 != y1
        x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
        low, high = np.minimum(y0, y1), np.maximum(y0, y1)
        first = np.ceil(low * s - 0.5).astype(np.int64)
        stop = np.ceil(high * s - 0.5).astype(np.int64)
        first = np.clip(first, 0, height * s)
        stop = np.clip(stop, 0, height * s)
        counts = np.maximum(stop - first, 0)
        total = int(counts.sum())
        if total == 0:
            continue
        edge = np.repeat(np.arange(len(counts)), counts)
        offsets = np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)
        row = first[edge] + offsets
        y = (row + 0.5) / s
        slope = (x1[edge] - x0[edge]) / (y1[edge] - y0[edge])
        x = x0[edge] + (y - y0[edge]) * slope
        col = np.floor(x * s - 0.5).astype(np.int64) + 1
        rows.append(row)
        cols.append(np.clip(col, 0, width * s))
    if not rows:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    row = np.concatenate(rows)
    col = np.concatenate(cols)
    order = np.argsort(row, kind="stable")
    return row[order], col[order]


def _coverage_counts(scene: Scene, supersample: int) -> np.ndarray:
    """Subsample counts per palette colour and pixel, int32 (n, H, W)."""
    s = int(supersample)
    if s < 1:
        raise ValueError("supersample must be at least 1")
    width, height = scene.width, scene.height
    n = len(scene.palette)
    counts = np.zeros((n, height, width), dtype=np.int32)
    crossings = [_region_crossings(region, s, width, height) for region in scene.regions]
    colours_present = sorted({scene.background, *(r.colour for r in scene.regions)})
    band = max(1, _BAND_SUBSAMPLES // (s * s * width))
    sub_width = width * s
    for top in range(0, height, band):
        rows = min(band, height - top)
        row_lo, row_hi = top * s, (top + rows) * s
        labels = np.full((rows * s, sub_width), scene.background, dtype=np.int16)
        for region, (row, col) in zip(scene.regions, crossings):
            lo, hi = np.searchsorted(row, [row_lo, row_hi])
            if lo == hi:
                continue
            band_row, band_col = row[lo:hi] - row_lo, col[lo:hi]
            c0 = int(band_col.min())
            c1 = int(band_col.max())
            if c1 <= c0:
                continue
            r0 = int(band_row.min())
            r1 = int(band_row.max()) + 1
            # One spare column receives the flips at ``c1``, which end the region.
            flips = np.zeros((r1 - r0, c1 - c0 + 1), dtype=np.uint8)
            np.bitwise_xor.at(flips, (band_row - r0, band_col - c0), 1)
            inside = np.bitwise_xor.accumulate(flips, axis=1)[:, :-1].view(bool)
            window = labels[r0:r1, c0:c1]
            window[inside] = region.colour
        blocks = labels.reshape(rows, s, width, s)
        for colour in colours_present:
            counts[colour, top:top + rows] = (blocks == colour).sum(axis=(1, 3), dtype=np.int32)
    return counts


def coverage(scene: Scene, supersample: int = 16) -> np.ndarray:
    """Fraction of each pixel covered by each palette colour's visible area,
    float64 (n_colours, H, W); the fractions of a pixel sum to exactly 1."""
    return _coverage_counts(scene, supersample) / float(supersample * supersample)


def stacking_rank(scene: Scene) -> list[int]:
    """Each palette colour's height in the stack: the index of its topmost
    region, -1 for the background alone, -2 for an unused colour."""
    rank = [-2] * len(scene.palette)
    rank[scene.background] = -1
    for index, region in enumerate(scene.regions):
        rank[region.colour] = max(rank[region.colour], index)
    return rank


def render_labels(scene: Scene, supersample: int = 16) -> np.ndarray:
    """int16 (H, W) label map: each pixel takes the colour with the largest
    coverage; equal coverage goes to the colour higher in the stack."""
    counts = _coverage_counts(scene, supersample)
    rank = stacking_rank(scene)
    order = sorted(range(len(rank)), key=lambda colour: (-rank[colour], colour))
    best = np.argmax(counts[order], axis=0)
    return np.asarray(order, dtype=np.int16)[best]


def srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(linear, dtype=np.float64), 0.0, 1.0)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1.0 / 2.4) - 0.055)


def render_rgb(scene: Scene, supersample: int = 16) -> np.ndarray:
    """uint8 (H, W, 3) anti-aliased render: palette colours blended by coverage
    in linear light, encoded back to sRGB and rounded half to even
    (``numpy.rint``). A fully covered pixel reproduces its palette RGB exactly."""
    fractions = coverage(scene, supersample)
    linear = srgb_to_linear(np.asarray([row["rgb"] for row in scene.palette]))
    blended = np.tensordot(fractions, linear, axes=([0], [0]))
    return np.rint(linear_to_srgb(blended) * 255.0).astype(np.uint8)
