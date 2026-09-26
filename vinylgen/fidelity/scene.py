"""Vector scenes for the synthetic fidelity corpus.

A scene is a canvas, a palette and colour regions stacked bottom to top. Each
region is a closed outline (plus optional holes) made of segments: straight
lines, circular arcs, elliptical arcs and cubic Béziers. Coordinates are pixels
of the canvas, x right and y down; an angle ``a`` names the direction
``(cos a, sin a)`` in those coordinates, so a positive arc sweep runs clockwise
on screen (SVG's positive-angle direction).

Geometry is defined so that every derived quantity is exact or carries a stated
bound:

- ``Segment.sample_params(tolerance)`` returns parameters whose polyline lies
  within ``tolerance`` of the true segment (a proven chord-error bound for each
  segment type, see the classes);
- ``segment_intersections`` and ``segment_locate`` refine every point with
  Newton iteration on the exact parametrisation, so transversal intersections
  are exact to floating-point precision.

A scene is scaled to another canvas with ``Scene.scaled``; the scaled canvas
must have integer dimensions, so families define their canvas so that the
intended sizes are integral multiples.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from shapely.geometry import LinearRing

Point = tuple[float, float]

# Chord error of the flattened outlines the rasteriser and the truth geometry use.
FLATTEN_TOLERANCE = 0.001
# Two outline points closer than this are the same point: a segment's end must be
# the next segment's start to within it.
CONTINUITY_TOLERANCE = 1e-6
# A join whose tangents turn by less than this many degrees is smooth.
SMOOTH_TURN_DEG = 1e-6
# Chord error of the polylines that seed the Newton refinements.
_SEED_TOLERANCE = 0.05
_QUARTER = math.pi / 2.0
_TAU = 2.0 * math.pi


def _point(value) -> Point:
    x, y = value
    return (float(x), float(y))


def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _norm(v) -> float:
    return math.hypot(float(v[0]), float(v[1]))


def turn_degrees(incoming, outgoing) -> float:
    """The turn from direction ``incoming`` to ``outgoing`` in degrees
    (0 straight on, 180 doubling back)."""
    cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
    dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
    return math.degrees(math.atan2(abs(cross), dot))


class Segment:
    """Shared behaviour of the segment types.

    A segment is parametrised on ``t`` in [0, 1]. ``point``, ``d1`` and ``d2``
    evaluate the position and its first and second derivatives for a scalar or
    array ``t`` (returning shape ``t.shape + (2,)``). Arcs use ``start`` for their
    start angle, so the endpoints are read with ``segment_start``/``segment_end``.
    """

    kind = ""

    def start_tangent(self) -> np.ndarray:
        return self.d1(0.0)

    def end_tangent(self) -> np.ndarray:
        return self.d1(1.0)

    def carrier(self):
        """A hashable identity of the curve the segment lies on, for conics;
        None for lines and cubics."""
        return None


@dataclass(frozen=True)
class Line(Segment):
    """The straight segment from ``p0`` to ``p1``. Its polyline is exact."""

    p0: Point
    p1: Point
    kind = "line"

    def __post_init__(self):
        object.__setattr__(self, "p0", _point(self.p0))
        object.__setattr__(self, "p1", _point(self.p1))
        if math.dist(self.p0, self.p1) <= CONTINUITY_TOLERANCE:
            raise ValueError("a line segment must have non-zero length")

    def point(self, t):
        t = np.asarray(t, dtype=np.float64)[..., None]
        p0 = np.asarray(self.p0)
        return p0 + t * (np.asarray(self.p1) - p0)

    def d1(self, t):
        t = np.asarray(t, dtype=np.float64)
        return np.broadcast_to(np.subtract(self.p1, self.p0), t.shape + (2,)).copy()

    def d2(self, t):
        t = np.asarray(t, dtype=np.float64)
        return np.zeros(t.shape + (2,))

    def sample_params(self, tolerance: float) -> np.ndarray:
        return np.array([0.0, 1.0])

    def scaled(self, factor: float) -> "Line":
        return Line(_scale(self.p0, factor), _scale(self.p1, factor))

    def to_json(self) -> dict:
        return {"type": "line", "p0": list(self.p0), "p1": list(self.p1)}


@dataclass(frozen=True)
class Arc(Segment):
    """A circular arc: ``centre + radius * (cos a, sin a)`` for ``a`` running from
    ``start`` through ``start + sweep`` radians (sweep signed, ``|sweep| <= 2 pi``).

    Flattening with angular step ``h`` leaves a chord error of at most the sagitta
    ``radius * (1 - cos(h / 2))``; ``sample_params`` picks ``h`` so that this is at
    most the tolerance.
    """

    centre: Point
    radius: float
    start: float
    sweep: float
    kind = "arc"

    def __post_init__(self):
        object.__setattr__(self, "centre", _point(self.centre))
        object.__setattr__(self, "radius", float(self.radius))
        object.__setattr__(self, "start", float(self.start))
        object.__setattr__(self, "sweep", float(self.sweep))
        _check_conic(self.radius, self.radius, self.sweep)

    def _angle(self, t):
        return self.start + np.asarray(t, dtype=np.float64) * self.sweep

    def point(self, t):
        a = self._angle(t)
        return np.stack([self.centre[0] + self.radius * np.cos(a),
                         self.centre[1] + self.radius * np.sin(a)], axis=-1)

    def d1(self, t):
        a = self._angle(t)
        k = self.radius * self.sweep
        return np.stack([-k * np.sin(a), k * np.cos(a)], axis=-1)

    def d2(self, t):
        a = self._angle(t)
        k = -self.radius * self.sweep * self.sweep
        return np.stack([k * np.cos(a), k * np.sin(a)], axis=-1)

    def sample_params(self, tolerance: float) -> np.ndarray:
        return _conic_params(self.radius, self.sweep, tolerance)

    def carrier(self):
        return ("circle", self.centre[0], self.centre[1], self.radius)

    def scaled(self, factor: float) -> "Arc":
        return Arc(_scale(self.centre, factor), self.radius * factor, self.start, self.sweep)

    def to_json(self) -> dict:
        return {"type": "arc", "centre": list(self.centre), "radius": self.radius,
                "start": self.start, "sweep": self.sweep}


@dataclass(frozen=True)
class EllipticalArc(Segment):
    """An elliptical arc: ``centre + R(rotation) (rx cos a, ry sin a)`` for the
    parameter angle ``a`` running from ``start`` through ``start + sweep``.

    The ellipse is the image of the unit circle under a linear map of norm
    ``max(rx, ry)``, which maps each chord of the circle onto the corresponding
    chord of the ellipse, so an angular step ``h`` leaves a chord error of at
    most ``max(rx, ry) * (1 - cos(h / 2))``.
    """

    centre: Point
    rx: float
    ry: float
    rotation: float
    start: float
    sweep: float
    kind = "elliptical_arc"

    def __post_init__(self):
        object.__setattr__(self, "centre", _point(self.centre))
        for name in ("rx", "ry", "rotation", "start", "sweep"):
            object.__setattr__(self, name, float(getattr(self, name)))
        _check_conic(self.rx, self.ry, self.sweep)

    def _angle(self, t):
        return self.start + np.asarray(t, dtype=np.float64) * self.sweep

    def _map(self, u, v):
        """Rotate axis-frame offsets ``(u, v)`` by ``rotation``."""
        c, s = math.cos(self.rotation), math.sin(self.rotation)
        return np.stack([c * u - s * v, s * u + c * v], axis=-1)

    def point(self, t):
        a = self._angle(t)
        return np.asarray(self.centre) + self._map(self.rx * np.cos(a), self.ry * np.sin(a))

    def d1(self, t):
        a = self._angle(t)
        return self.sweep * self._map(-self.rx * np.sin(a), self.ry * np.cos(a))

    def d2(self, t):
        a = self._angle(t)
        return -self.sweep * self.sweep * self._map(self.rx * np.cos(a), self.ry * np.sin(a))

    def sample_params(self, tolerance: float) -> np.ndarray:
        return _conic_params(max(self.rx, self.ry), self.sweep, tolerance)

    def carrier(self):
        rx, ry, rotation = self.rx, self.ry, self.rotation
        if abs(rx - ry) <= 1e-12 * max(rx, ry):
            return ("circle", self.centre[0], self.centre[1], rx)
        if rx < ry:
            rx, ry, rotation = ry, rx, rotation + _QUARTER
        return ("ellipse", self.centre[0], self.centre[1], rx, ry, rotation % math.pi)

    def scaled(self, factor: float) -> "EllipticalArc":
        return EllipticalArc(_scale(self.centre, factor), self.rx * factor, self.ry * factor,
                             self.rotation, self.start, self.sweep)

    def to_json(self) -> dict:
        return {"type": "elliptical_arc", "centre": list(self.centre), "rx": self.rx,
                "ry": self.ry, "rotation": self.rotation, "start": self.start,
                "sweep": self.sweep}


@dataclass(frozen=True)
class Cubic(Segment):
    """A cubic Bézier from ``p0`` to ``p1`` with control points ``c1`` and ``c2``.

    Over a parameter interval of length ``h`` the curve deviates from its chord by
    at most ``(1/8) h^2 max|B''|`` (linear-interpolation error), and
    ``max|B''| = 6 max(|p0 - 2c1 + c2|, |c1 - 2c2 + p1|)``; ``sample_params`` uses
    uniform steps with ``0.75 h^2 M <= tolerance``.

    The curve must have a non-zero derivative everywhere (no control point
    coinciding with its endpoint, no interior cusp), so every point has a tangent.
    """

    p0: Point
    c1: Point
    c2: Point
    p1: Point
    kind = "cubic"

    def __post_init__(self):
        for name in ("p0", "c1", "c2", "p1"):
            object.__setattr__(self, name, _point(getattr(self, name)))
        if (math.dist(self.p0, self.c1) <= CONTINUITY_TOLERANCE
                or math.dist(self.c2, self.p1) <= CONTINUITY_TOLERANCE):
            raise ValueError("a cubic's control points must differ from its endpoints")
        coefficients = self._derivative_coefficients()
        scale = max(_norm(c) for c in coefficients) or 1.0
        roots = []
        for axis in (0, 1):
            a, b, c = (float(v[axis]) for v in coefficients)
            if abs(a) > 1e-15 * scale or abs(b) > 1e-15 * scale:
                roots.extend(r.real for r in np.roots([a, b, c])
                             if abs(r.imag) < 1e-9 and 0.0 < r.real < 1.0)
        for root in roots:
            if _norm(self.d1(root)) <= 1e-9 * scale:
                raise ValueError("a cubic must not have a cusp (zero derivative)")

    def _derivative_coefficients(self):
        p0, c1, c2, p1 = (np.asarray(v) for v in (self.p0, self.c1, self.c2, self.p1))
        a0, a1, a2 = 3 * (c1 - p0), 3 * (c2 - c1), 3 * (p1 - c2)
        # d1(t) = a0 (1-t)^2 + 2 a1 (1-t) t + a2 t^2, as a t^2 + b t + c
        return (a0 - 2 * a1 + a2, 2 * (a1 - a0), a0)

    def point(self, t):
        t = np.asarray(t, dtype=np.float64)[..., None]
        u = 1.0 - t
        return (u * u * u * np.asarray(self.p0) + 3 * u * u * t * np.asarray(self.c1)
                + 3 * u * t * t * np.asarray(self.c2) + t * t * t * np.asarray(self.p1))

    def d1(self, t):
        t = np.asarray(t, dtype=np.float64)[..., None]
        u = 1.0 - t
        p0, c1, c2, p1 = (np.asarray(v) for v in (self.p0, self.c1, self.c2, self.p1))
        return 3 * (u * u * (c1 - p0) + 2 * u * t * (c2 - c1) + t * t * (p1 - c2))

    def d2(self, t):
        t = np.asarray(t, dtype=np.float64)[..., None]
        p0, c1, c2, p1 = (np.asarray(v) for v in (self.p0, self.c1, self.c2, self.p1))
        return 6 * ((1.0 - t) * (c2 - 2 * c1 + p0) + t * (p1 - 2 * c2 + c1))

    def sample_params(self, tolerance: float) -> np.ndarray:
        p0, c1, c2, p1 = (np.asarray(v) for v in (self.p0, self.c1, self.c2, self.p1))
        bound = max(_norm(p0 - 2 * c1 + c2), _norm(c1 - 2 * c2 + p1))
        count = max(1, math.ceil(math.sqrt(0.75 * bound / tolerance)))
        return np.linspace(0.0, 1.0, count + 1)

    def scaled(self, factor: float) -> "Cubic":
        return Cubic(*(_scale(p, factor) for p in (self.p0, self.c1, self.c2, self.p1)))

    def to_json(self) -> dict:
        return {"type": "cubic", "p0": list(self.p0), "c1": list(self.c1),
                "c2": list(self.c2), "p1": list(self.p1)}


def _scale(p: Point, factor: float) -> Point:
    return (p[0] * factor, p[1] * factor)


def _check_conic(rx: float, ry: float, sweep: float) -> None:
    if not (rx > 0.0 and ry > 0.0):
        raise ValueError("an arc needs positive radii")
    if not (0.0 < abs(sweep) <= _TAU + 1e-12):
        raise ValueError("an arc's sweep must be non-zero and at most a full turn")


def _conic_params(radius: float, sweep: float, tolerance: float) -> np.ndarray:
    ratio = min(tolerance / radius, 2.0)
    step = 2.0 * math.acos(1.0 - ratio)
    count = max(math.ceil(abs(sweep) / step), math.ceil(abs(sweep) / _QUARTER - 1e-12), 1)
    return np.linspace(0.0, 1.0, count + 1)


def segment_start(segment: Segment) -> Point:
    x, y = segment.point(0.0)
    return (float(x), float(y))


def segment_end(segment: Segment) -> Point:
    x, y = segment.point(1.0)
    return (float(x), float(y))


_SEGMENT_TYPES = {"line": Line, "arc": Arc, "elliptical_arc": EllipticalArc, "cubic": Cubic}


def segment_from_json(data: dict) -> Segment:
    kind = data["type"]
    fields = {key: (tuple(value) if isinstance(value, list) else value)
              for key, value in data.items() if key != "type"}
    if kind not in _SEGMENT_TYPES:
        raise ValueError(f"unknown segment type {kind!r}")
    return _SEGMENT_TYPES[kind](**fields)


@lru_cache(maxsize=65536)
def segment_polyline(segment: Segment, tolerance: float) -> tuple[np.ndarray, np.ndarray]:
    """Parameters and points of the segment's polyline at ``tolerance`` chord error."""
    params = segment.sample_params(tolerance)
    return params, segment.point(params)


@dataclass(frozen=True)
class Outline:
    """A closed sequence of segments, each starting where the previous one ends.

    The outline must be a simple closed curve (it may not touch or cross itself),
    checked on its flattened polyline, and no join may double back.
    """

    segments: tuple[Segment, ...]

    def __post_init__(self):
        segments = tuple(self.segments)
        object.__setattr__(self, "segments", segments)
        if not segments:
            raise ValueError("an outline needs at least one segment")
        for index, segment in enumerate(segments):
            following = segments[(index + 1) % len(segments)]
            gap = math.dist(segment_end(segment), segment_start(following))
            if gap > CONTINUITY_TOLERANCE:
                raise ValueError(f"outline segment {index} ends {gap:.6g} px from where "
                                 f"segment {(index + 1) % len(segments)} starts")
        for point, turn in self.join_turns():
            if turn >= 180.0 - 1e-9:
                raise ValueError(f"outline doubles back at {point}")
        ring = LinearRing(self.flatten(FLATTEN_TOLERANCE))
        if not ring.is_simple:
            raise ValueError("an outline must not touch or cross itself")

    def flatten(self, tolerance: float = FLATTEN_TOLERANCE) -> np.ndarray:
        """The outline as an open ring of points (the closing point is not
        repeated) lying within ``tolerance`` of the true outline."""
        parts = [segment_polyline(segment, tolerance)[1][:-1] for segment in self.segments]
        return np.concatenate(parts, axis=0)

    def join_turns(self) -> list[tuple[Point, float]]:
        """Every join (the start of each segment) with its turn in degrees."""
        joins = []
        for index, segment in enumerate(self.segments):
            previous = self.segments[index - 1]
            turn = turn_degrees(previous.end_tangent(), segment.start_tangent())
            joins.append((segment_start(segment), turn))
        return joins

    def corners(self) -> list[tuple[Point, float]]:
        """The joins where the tangent is discontinuous, with their turn in degrees."""
        return [(point, turn) for point, turn in self.join_turns() if turn >= SMOOTH_TURN_DEG]

    def signed_area(self) -> float:
        """Shoelace area of the flattened outline: positive when the interior lies
        to the left of the direction of travel (counter-clockwise in the
        mathematical sense, clockwise on a y-down screen)."""
        points = self.flatten(FLATTEN_TOLERANCE)
        x, y = points[:, 0], points[:, 1]
        return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    def scaled(self, factor: float) -> "Outline":
        return Outline(tuple(segment.scaled(factor) for segment in self.segments))

    def to_json(self) -> list[dict]:
        return [segment.to_json() for segment in self.segments]

    @classmethod
    def from_json(cls, data: list[dict]) -> "Outline":
        return cls(tuple(segment_from_json(item) for item in data))

    def svg_path(self) -> str:
        """SVG path data; arcs are written as arc commands of at most 90 degrees."""
        x, y = segment_start(self.segments[0])
        commands = [f"M {_num(x)} {_num(y)}"]
        for segment in self.segments:
            commands.extend(_svg_commands(segment))
        commands.append("Z")
        return " ".join(commands)


def _num(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _svg_commands(segment: Segment) -> list[str]:
    if isinstance(segment, Line):
        return [f"L {_num(segment.p1[0])} {_num(segment.p1[1])}"]
    if isinstance(segment, Cubic):
        return ["C " + " ".join(f"{_num(p[0])} {_num(p[1])}"
                                for p in (segment.c1, segment.c2, segment.p1))]
    if isinstance(segment, Arc):
        rx = ry = segment.radius
        rotation = 0.0
    else:
        rx, ry, rotation = segment.rx, segment.ry, math.degrees(segment.rotation)
    pieces = max(1, math.ceil(abs(segment.sweep) / _QUARTER - 1e-12))
    flag = 1 if segment.sweep > 0 else 0
    commands = []
    for piece in range(1, pieces + 1):
        x, y = segment.point(piece / pieces)
        commands.append(f"A {_num(rx)} {_num(ry)} {_num(rotation)} 0 {flag} "
                        f"{_num(float(x))} {_num(float(y))}")
    return commands


def polygon(vertices) -> Outline:
    """A closed polygon through ``vertices`` (the last joins back to the first)."""
    points = [_point(v) for v in vertices]
    return Outline(tuple(Line(points[i], points[(i + 1) % len(points)])
                         for i in range(len(points))))


def rectangle(x0: float, y0: float, x1: float, y1: float) -> Outline:
    return polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def circle(centre, radius: float, start: float = 0.0) -> Outline:
    """A full circle as one arc starting (and ending) at angle ``start``."""
    return Outline((Arc(centre, radius, start, _TAU),))


def ellipse(centre, rx: float, ry: float, rotation: float = 0.0, start: float = 0.0) -> Outline:
    """A full ellipse as one elliptical arc, exact (no Bézier approximation)."""
    return Outline((EllipticalArc(centre, rx, ry, rotation, start, _TAU),))


def wedge(centre, radius: float, start: float, sweep: float) -> Outline:
    """A pie wedge: the radius at ``start``, the arc through ``sweep`` and the
    radius back to the centre."""
    centre = _point(centre)
    arc = Arc(centre, radius, start, sweep)
    return Outline((Line(centre, segment_start(arc)), arc, Line(segment_end(arc), centre)))


@dataclass(frozen=True)
class Region:
    """One palette colour filling an outline minus its holes (even-odd: every
    ring boundary toggles inside and outside)."""

    colour: int
    outline: Outline
    holes: tuple[Outline, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "colour", int(self.colour))
        object.__setattr__(self, "holes", tuple(self.holes))

    @property
    def rings(self) -> tuple[Outline, ...]:
        return (self.outline, *self.holes)

    def scaled(self, factor: float) -> "Region":
        return Region(self.colour, self.outline.scaled(factor),
                      tuple(hole.scaled(factor) for hole in self.holes))

    def to_json(self) -> dict:
        return {"colour": self.colour, "outline": self.outline.to_json(),
                "holes": [hole.to_json() for hole in self.holes]}

    @classmethod
    def from_json(cls, data: dict) -> "Region":
        return cls(int(data["colour"]), Outline.from_json(data["outline"]),
                   tuple(Outline.from_json(hole) for hole in data.get("holes", [])))


@dataclass
class Scene:
    """A canvas of ``width`` x ``height`` pixels filled with the ``background``
    palette colour, with ``regions`` painted bottom to top. ``palette`` rows are
    ``{"name": str, "rgb": [r, g, b]}``. ``family`` and ``params`` record how the
    scene was generated."""

    width: int
    height: int
    palette: list[dict]
    background: int
    regions: list[Region]
    family: str = ""
    params: dict = field(default_factory=dict)

    def __post_init__(self):
        self.width = int(self.width)
        self.height = int(self.height)
        self.background = int(self.background)
        self.regions = list(self.regions)
        self.palette = [{"name": str(row["name"]), "rgb": [int(v) for v in row["rgb"]]}
                        for row in self.palette]
        if self.width <= 0 or self.height <= 0:
            raise ValueError("a scene needs a positive canvas")
        for row in self.palette:
            if len(row["rgb"]) != 3 or not all(0 <= v <= 255 for v in row["rgb"]):
                raise ValueError(f"palette colour {row['name']!r} is not an 8-bit RGB triple")
        colours = [self.background] + [region.colour for region in self.regions]
        if any(not 0 <= colour < len(self.palette) for colour in colours):
            raise ValueError("a region or the background names a colour outside the palette")

    def scaled(self, factor: float) -> "Scene":
        """The scene with geometry and canvas multiplied by ``factor``. The scaled
        canvas must have integer dimensions."""
        factor = float(factor)
        dims = []
        for size in (self.width, self.height):
            scaled = size * factor
            if abs(scaled - round(scaled)) > 1e-9 * max(1.0, scaled) or round(scaled) <= 0:
                raise ValueError(f"scaling {size} px by {factor} does not give a whole "
                                 "number of pixels")
            dims.append(int(round(scaled)))
        return Scene(dims[0], dims[1], copy.deepcopy(self.palette), self.background,
                     [region.scaled(factor) for region in self.regions], self.family,
                     copy.deepcopy(self.params))

    def to_json(self) -> dict:
        return {"width": self.width, "height": self.height,
                "palette": copy.deepcopy(self.palette), "background": self.background,
                "regions": [region.to_json() for region in self.regions],
                "family": self.family, "params": copy.deepcopy(self.params)}

    @classmethod
    def from_json(cls, data: dict) -> "Scene":
        return cls(width=data["width"], height=data["height"], palette=data["palette"],
                   background=data["background"],
                   regions=[Region.from_json(item) for item in data["regions"]],
                   family=data.get("family", ""), params=copy.deepcopy(data.get("params", {})))

    def to_svg(self) -> str:
        """A plain SVG of the scene for visual inspection."""
        def fill(colour: int) -> str:
            r, g, b = self.palette[colour]["rgb"]
            return f"#{r:02x}{g:02x}{b:02x}"

        lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
                 f'height="{self.height}" viewBox="0 0 {self.width} {self.height}">',
                 f'  <rect x="0" y="0" width="{self.width}" height="{self.height}" '
                 f'fill="{fill(self.background)}"/>']
        for region in self.regions:
            data = " ".join(ring.svg_path() for ring in region.rings)
            lines.append(f'  <path fill="{fill(region.colour)}" fill-rule="evenodd" '
                         f'd="{data}"/>')
        lines.append("</svg>")
        return "\n".join(lines) + "\n"


# --- exact point location and intersection -------------------------------------------


def _polyline(segment: Segment) -> tuple[np.ndarray, np.ndarray]:
    return segment_polyline(segment, _SEED_TOLERANCE)


def _nearest_on_polyline(points: np.ndarray, params: np.ndarray, targets: np.ndarray):
    """For each target, its distance to the polyline and the interpolated
    parameter of the nearest polyline point."""
    a = points[:-1]
    d = points[1:] - points[:-1]
    length2 = np.maximum(np.einsum("ij,ij->i", d, d), 1e-300)
    rel = targets[:, None, :] - a[None, :, :]
    u = np.clip(np.einsum("tij,ij->ti", rel, d) / length2, 0.0, 1.0)
    nearest = a[None] + u[..., None] * d[None]
    dist = np.hypot(*(targets[:, None, :] - nearest).transpose(2, 0, 1))
    best = np.argmin(dist, axis=1)
    rows = np.arange(len(targets))
    param = params[best] + u[rows, best] * (params[best + 1] - params[best])
    return dist, dist[rows, best], param


def segment_locate(segment: Segment, p: Point, tolerance: float = CONTINUITY_TOLERANCE
                   ) -> list[float]:
    """Parameters in [0, 1] at which ``segment`` passes within ``tolerance`` of
    ``p``, refined to the closest point by Newton iteration."""
    target = np.asarray(p, dtype=np.float64)
    if isinstance(segment, Line):
        p0 = np.asarray(segment.p0)
        d = np.asarray(segment.p1) - p0
        t = float(np.dot(target - p0, d) / np.dot(d, d))
        t = min(1.0, max(0.0, t))
        return [t] if _norm(segment.point(t) - target) <= tolerance else []
    if isinstance(segment, Arc):
        v = target - np.asarray(segment.centre)
        if abs(_norm(v) - segment.radius) > tolerance:
            return []
        angle = math.atan2(v[1], v[0])
        offset = (angle - segment.start) * (1.0 if segment.sweep > 0 else -1.0)
        offset %= _TAU
        found = []
        for delta in (offset, offset - _TAU):
            t = min(1.0, max(0.0, delta / abs(segment.sweep)))
            if _norm(segment.point(t) - target) <= tolerance and \
                    all(abs(t - other) > 1e-12 for other in found):
                found.append(t)
        return found
    params, points = _polyline(segment)
    dist_all, _, _ = _nearest_on_polyline(points, params, target[None])
    near = np.flatnonzero(dist_all[0] <= _SEED_TOLERANCE + tolerance)
    found: list[float] = []
    for index in near:
        a, b = points[index], points[index + 1]
        d = b - a
        u = float(np.clip(np.dot(target - a, d) / max(np.dot(d, d), 1e-300), 0.0, 1.0))
        t = params[index] + u * (params[index + 1] - params[index])
        for _ in range(40):
            offset = segment.point(t) - target
            first = segment.d1(t)
            slope = float(np.dot(first, first) + np.dot(offset, segment.d2(t)))
            if slope <= 0.0:
                break
            step = float(np.dot(offset, first)) / slope
            t = min(1.0, max(0.0, t - step))
            if abs(step) < 1e-15:
                break
        if _norm(segment.point(t) - target) <= tolerance and \
                all(abs(t - other) > 1e-9 for other in found):
            found.append(t)
    return sorted(found)


def _same_curve(a: Segment, b: Segment) -> bool:
    """Whether two segments lie on one curve, so that any shared stretch ends at
    segment endpoints (which are outline joins)."""
    if isinstance(a, Line) and isinstance(b, Line):
        da = np.subtract(a.p1, a.p0)
        scale = _norm(da) * max(_norm(np.subtract(b.p1, b.p0)), _norm(np.subtract(b.p0, a.p0)))
        return (abs(float(_cross(da, np.subtract(b.p1, b.p0)))) <= 1e-12 * max(scale, 1e-300)
                and abs(float(_cross(da, np.subtract(b.p0, a.p0)))) <= 1e-12 * max(scale, 1e-300))
    key_a, key_b = a.carrier(), b.carrier()
    if key_a is not None and key_b is not None and key_a[0] == key_b[0]:
        values = np.asarray(key_a[1:]), np.asarray(key_b[1:])
        scale = max(1.0, float(np.max(np.abs(values[0]))))
        if key_a[0] == "ellipse":
            angle = abs(values[0][-1] - values[1][-1]) % math.pi
            if min(angle, math.pi - angle) > 1e-12:
                return False
            return bool(np.all(np.abs(values[0][:-1] - values[1][:-1]) <= 1e-12 * scale))
        return bool(np.all(np.abs(values[0] - values[1]) <= 1e-12 * scale))
    if isinstance(a, Cubic) and isinstance(b, Cubic):
        pa = np.asarray([a.p0, a.c1, a.c2, a.p1])
        pb = np.asarray([b.p0, b.c1, b.c2, b.p1])
        return bool(np.max(np.abs(pa - pb)) <= CONTINUITY_TOLERANCE
                    or np.max(np.abs(pa - pb[::-1])) <= CONTINUITY_TOLERANCE)
    return False


def segment_intersections(a: Segment, b: Segment) -> list[tuple[float, float, Point]]:
    """Every point where segments ``a`` and ``b`` meet, as ``(s, t, point)`` with
    ``a(s) == b(t)``.

    Segments on the same curve (collinear lines, arcs of one circle or ellipse,
    identical cubics) return nothing: their shared stretch ends at segment
    endpoints, which callers take from the outline joins. Other pairs are seeded
    from 0.05 px polylines (crossings and closest approaches within 0.2 px) and
    refined by damped Newton iteration on ``a(s) - b(t)`` to a residual below
    1e-8 px. Transversal intersections are exact to floating-point precision; a
    tangent contact is located to within ``sqrt(1e-8 * radius of curvature)``
    along the common tangent.

    Raises ValueError when two different curves run together along a stretch
    (a partial overlap the endpoint rule would not capture).
    """
    if _same_curve(a, b):
        return []
    if isinstance(a, Line) and isinstance(b, Line):
        p, r = np.asarray(a.p0), np.subtract(a.p1, a.p0)
        q, u = np.asarray(b.p0), np.subtract(b.p1, b.p0)
        denominator = float(_cross(r, u))
        if denominator == 0.0:
            return []
        s = float(_cross(q - p, u)) / denominator
        t = float(_cross(q - p, r)) / denominator
        eps = 1e-12
        if -eps <= s <= 1 + eps and -eps <= t <= 1 + eps:
            s, t = min(1.0, max(0.0, s)), min(1.0, max(0.0, t))
            x, y = a.point(s)
            return [(s, t, (float(x), float(y)))]
        return []
    params_a, points_a = _polyline(a)
    params_b, points_b = _polyline(b)
    lo = np.maximum(points_a.min(0), points_b.min(0)) - 4 * _SEED_TOLERANCE
    hi = np.minimum(points_a.max(0), points_b.max(0)) + 4 * _SEED_TOLERANCE
    if np.any(lo > hi):
        return []
    seeds = _crossing_seeds(points_a, params_a, points_b, params_b)
    for first, first_params, second, second_params, flip in (
            (points_a, params_a, points_b, params_b, False),
            (points_b, params_b, points_a, params_a, True)):
        _, dist, nearest = _nearest_on_polyline(second, second_params, first)
        close = dist <= 4 * _SEED_TOLERANCE
        run = 0
        for index in range(len(first)):
            run = run + 1 if dist[index] <= 1e-7 else 0
            if run >= 3:
                raise ValueError("two different curves overlap along a stretch; shared "
                                 "boundaries must use identical segments")
            if not close[index]:
                continue
            before = dist[index - 1] if index > 0 else math.inf
            after = dist[index + 1] if index + 1 < len(first) else math.inf
            if dist[index] <= before and dist[index] <= after:
                pair = (first_params[index], nearest[index])
                seeds.append(pair[::-1] if flip else pair)
    found: list[tuple[float, float, Point]] = []
    for s, t in seeds:
        solved = _newton_pair(a, b, s, t)
        if solved is None:
            continue
        s, t = solved
        x, y = 0.5 * (a.point(s) + b.point(t))
        point = (float(x), float(y))
        if all(math.dist(point, other[2]) > 1e-6 for other in found):
            found.append((s, t, point))
    return found


def _crossing_seeds(pa, sa, pb, sb) -> list[tuple[float, float]]:
    a0, da = pa[:-1], pa[1:] - pa[:-1]
    b0, db = pb[:-1], pb[1:] - pb[:-1]
    denominator = _cross(da[:, None, :], db[None, :, :])
    offset = b0[None, :, :] - a0[:, None, :]
    with np.errstate(divide="ignore", invalid="ignore"):
        u = _cross(offset, db[None, :, :]) / denominator
        v = _cross(offset, da[:, None, :]) / denominator
    hit = (denominator != 0) & (u >= 0) & (u <= 1) & (v >= 0) & (v <= 1)
    rows, cols = np.nonzero(hit)
    return [(float(sa[i] + u[i, j] * (sa[i + 1] - sa[i])),
             float(sb[j] + v[i, j] * (sb[j + 1] - sb[j]))) for i, j in zip(rows, cols)]


def _newton_pair(a: Segment, b: Segment, s: float, t: float):
    for _ in range(100):
        residual = a.point(s) - b.point(t)
        if _norm(residual) < 1e-12:
            break
        jacobian = np.column_stack([a.d1(s), -b.d1(t)])
        normal = jacobian.T @ jacobian
        damping = 1e-12 * float(np.trace(normal)) + 1e-300
        step = np.linalg.solve(normal + damping * np.eye(2), -(jacobian.T @ residual))
        s, t = s + float(step[0]), t + float(step[1])
        if not (-0.5 < s < 1.5 and -0.5 < t < 1.5):
            return None
        if abs(step[0]) < 1e-16 and abs(step[1]) < 1e-16:
            break
    if _norm(a.point(s) - b.point(t)) > 1e-8:
        return None
    eps = 1e-9
    if not (-eps <= s <= 1 + eps and -eps <= t <= 1 + eps):
        return None
    return min(1.0, max(0.0, s)), min(1.0, max(0.0, t))
