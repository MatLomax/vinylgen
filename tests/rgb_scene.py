"""Anti-aliased RGB renders of small vector scenes, the palette-stage tests' synthetic inputs.

A scene is a canvas, a palette and colour regions stacked bottom to top. Each region is a closed
outline (plus optional holes) of straight lines, circular arcs and elliptical arcs, in pixels of the
canvas, x right and y down; an angle ``a`` names the direction ``(cos a, sin a)``, so a positive arc
sweep runs clockwise on screen.

``render_rgb`` samples every pixel on an ``s`` x ``s`` grid of subsample points at
``((i + 0.5) / s, (j + 0.5) / s)`` inside it. A subsample takes the colour of the topmost region
containing it (the background where none does); membership is even-odd over the region's rings
flattened to ``FLATTEN_TOLERANCE`` chord error, computed by scanline parity: each flattened edge is
intersected with the subsample rows it spans (half-open in y, so a vertex on a row is counted once),
and each crossing flips the parity of every subsample to its right. The colours are blended by
coverage in linear light and encoded back to sRGB. All arithmetic is plain float64/integer numpy in a
fixed order, so a scene renders to byte-identical arrays in any process.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

Point = tuple[float, float]

# Chord error of the flattened outlines the rasteriser reads.
FLATTEN_TOLERANCE = 0.001
_QUARTER = math.pi / 2.0
_TAU = 2.0 * math.pi
# Subsamples held per band (sets the band height).
_BAND_SUBSAMPLES = 1 << 22


def _point(value) -> Point:
    x, y = value
    return (float(x), float(y))


def _conic_params(radius: float, sweep: float, tolerance: float) -> np.ndarray:
    """Parameters of an arc's polyline: an angular step ``h`` leaves a chord error of at most the
    sagitta ``radius * (1 - cos(h / 2))``, at most ``tolerance``; no step exceeds a quarter turn."""
    ratio = min(tolerance / radius, 2.0)
    step = 2.0 * math.acos(1.0 - ratio)
    count = max(math.ceil(abs(sweep) / step), math.ceil(abs(sweep) / _QUARTER - 1e-12), 1)
    return np.linspace(0.0, 1.0, count + 1)


def _check_conic(rx: float, ry: float, sweep: float) -> None:
    if not (rx > 0.0 and ry > 0.0):
        raise ValueError("an arc needs positive radii")
    if not (0.0 < abs(sweep) <= _TAU + 1e-12):
        raise ValueError("an arc's sweep must be non-zero and at most a full turn")


@dataclass(frozen=True)
class Line:
    """The straight segment from ``p0`` to ``p1``, parametrised on ``t`` in [0, 1]."""

    p0: Point
    p1: Point

    def __post_init__(self):
        object.__setattr__(self, "p0", _point(self.p0))
        object.__setattr__(self, "p1", _point(self.p1))

    def point(self, t):
        t = np.asarray(t, dtype=np.float64)[..., None]
        p0 = np.asarray(self.p0)
        return p0 + t * (np.asarray(self.p1) - p0)

    def sample_params(self, tolerance: float) -> np.ndarray:
        return np.array([0.0, 1.0])


@dataclass(frozen=True)
class Arc:
    """A circular arc: ``centre + radius * (cos a, sin a)`` for ``a`` running from ``start`` through
    ``start + sweep`` radians (sweep signed, ``|sweep| <= 2 pi``)."""

    centre: Point
    radius: float
    start: float
    sweep: float

    def __post_init__(self):
        object.__setattr__(self, "centre", _point(self.centre))
        for name in ("radius", "start", "sweep"):
            object.__setattr__(self, name, float(getattr(self, name)))
        _check_conic(self.radius, self.radius, self.sweep)

    def point(self, t):
        a = self.start + np.asarray(t, dtype=np.float64) * self.sweep
        return np.stack([self.centre[0] + self.radius * np.cos(a),
                         self.centre[1] + self.radius * np.sin(a)], axis=-1)

    def sample_params(self, tolerance: float) -> np.ndarray:
        return _conic_params(self.radius, self.sweep, tolerance)


@dataclass(frozen=True)
class EllipticalArc:
    """An elliptical arc: ``centre + R(rotation) (rx cos a, ry sin a)`` for the parameter angle ``a``
    running from ``start`` through ``start + sweep``. The ellipse is the image of the unit circle under
    a linear map of norm ``max(rx, ry)``, so an angular step ``h`` leaves a chord error of at most
    ``max(rx, ry) * (1 - cos(h / 2))``."""

    centre: Point
    rx: float
    ry: float
    rotation: float
    start: float
    sweep: float

    def __post_init__(self):
        object.__setattr__(self, "centre", _point(self.centre))
        for name in ("rx", "ry", "rotation", "start", "sweep"):
            object.__setattr__(self, name, float(getattr(self, name)))
        _check_conic(self.rx, self.ry, self.sweep)

    def point(self, t):
        a = self.start + np.asarray(t, dtype=np.float64) * self.sweep
        u, v = self.rx * np.cos(a), self.ry * np.sin(a)
        c, s = math.cos(self.rotation), math.sin(self.rotation)
        return np.asarray(self.centre) + np.stack([c * u - s * v, s * u + c * v], axis=-1)

    def sample_params(self, tolerance: float) -> np.ndarray:
        return _conic_params(max(self.rx, self.ry), self.sweep, tolerance)


def _start(segment) -> Point:
    x, y = segment.point(0.0)
    return (float(x), float(y))


def _end(segment) -> Point:
    x, y = segment.point(1.0)
    return (float(x), float(y))


@dataclass(frozen=True)
class Outline:
    """A closed sequence of segments, each starting where the previous one ends."""

    segments: tuple

    def flatten(self, tolerance: float = FLATTEN_TOLERANCE) -> np.ndarray:
        """The outline as an open ring of points (the closing point is not repeated) lying within
        ``tolerance`` of the true outline."""
        parts = [segment.point(segment.sample_params(tolerance))[:-1] for segment in self.segments]
        return np.concatenate(parts, axis=0)


def polygon(vertices) -> Outline:
    """A closed polygon through ``vertices`` (the last joins back to the first)."""
    points = [_point(v) for v in vertices]
    return Outline(tuple(Line(points[i], points[(i + 1) % len(points)]) for i in range(len(points))))


def circle(centre, radius: float, start: float = 0.0) -> Outline:
    """A full circle as one arc starting (and ending) at angle ``start``."""
    return Outline((Arc(centre, radius, start, _TAU),))


def ellipse(centre, rx: float, ry: float, rotation: float = 0.0, start: float = 0.0) -> Outline:
    """A full ellipse as one elliptical arc."""
    return Outline((EllipticalArc(centre, rx, ry, rotation, start, _TAU),))


def wedge(centre, radius: float, start: float, sweep: float) -> Outline:
    """A pie wedge: the radius at ``start``, the arc through ``sweep`` and the radius back to the
    centre."""
    centre = _point(centre)
    arc = Arc(centre, radius, start, sweep)
    return Outline((Line(centre, _start(arc)), arc, Line(_end(arc), centre)))


@dataclass(frozen=True)
class Region:
    """One palette colour filling an outline minus its holes (even-odd: every ring boundary toggles
    inside and outside)."""

    colour: int
    outline: Outline
    holes: tuple = ()

    @property
    def rings(self) -> tuple:
        return (self.outline, *self.holes)


@dataclass
class Scene:
    """A canvas of ``width`` x ``height`` pixels filled with the ``background`` palette colour, with
    ``regions`` painted bottom to top. ``palette`` rows are ``{"name": str, "rgb": [r, g, b]}``."""

    width: int
    height: int
    palette: list
    background: int
    regions: list
    family: str = ""
    params: dict = field(default_factory=dict)


def _region_crossings(region: Region, supersample: int, width: int, height: int):
    """Subsample-row indices and parity-flip columns of a region's edges. A crossing at ``x`` flips
    the subsample columns whose centre lies strictly right of ``x``: the first is
    ``floor(x * s - 0.5) + 1``, clamped to ``[0, width * s]``."""
    s = supersample
    rows, cols = [], []
    for ring in region.rings:
        points = ring.flatten(FLATTEN_TOLERANCE)
        x0, y0 = points[:, 0], points[:, 1]
        x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
        keep = y0 != y1
        x0, y0, x1, y1 = x0[keep], y0[keep], x1[keep], y1[keep]
        low, high = np.minimum(y0, y1), np.maximum(y0, y1)
        first = np.clip(np.ceil(low * s - 0.5).astype(np.int64), 0, height * s)
        stop = np.clip(np.ceil(high * s - 0.5).astype(np.int64), 0, height * s)
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
    band = max(1, _BAND_SUBSAMPLES // (s * s * width))
    sub_width = width * s
    # ``bins`` holds each subsample's band-pixel index times ``n``, so ``bins + labels`` is its
    # (pixel, colour) bin and one bincount counts every colour in every pixel of the band.
    sub_rows = np.arange(min(band, height) * s) // s
    bins = ((sub_rows[:, None] * width + np.arange(sub_width) // s) * n).astype(np.intp)
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
        keys = bins[:rows * s] + labels
        tally = np.bincount(keys.ravel(), minlength=rows * width * n)
        counts[:, top:top + rows] = tally.reshape(rows, width, n).transpose(2, 0, 1)
    return counts


def _srgb_to_linear(rgb: np.ndarray) -> np.ndarray:
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(linear, dtype=np.float64), 0.0, 1.0)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1.0 / 2.4) - 0.055)


def render_rgb(scene: Scene, supersample: int = 16) -> np.ndarray:
    """uint8 (H, W, 3) anti-aliased render: palette colours blended by coverage in linear light,
    encoded back to sRGB and rounded half to even (``numpy.rint``). A fully covered pixel reproduces
    its palette RGB exactly."""
    fractions = _coverage_counts(scene, supersample) / float(supersample * supersample)
    linear = _srgb_to_linear(np.asarray([row["rgb"] for row in scene.palette]))
    blended = np.tensordot(fractions, linear, axes=([0], [0]))
    return np.rint(_linear_to_srgb(blended) * 255.0).astype(np.uint8)
