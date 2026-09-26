"""Seeded shape fuzzer for the synthetic fidelity corpus.

``fuzz_case(seed, size)`` draws one random scene and returns it as a ``Case``.
The same ``(seed, size, kind)`` always gives the same scene JSON: every random
value comes from ``numpy.random.default_rng(seed)`` read only through
``Generator.random()`` (the PCG64 double stream, stable across platforms and
numpy versions; no global RNG state, no ``hash()``), and every coordinate is
snapped to a dyadic grid of ``GRID`` px, so sums of grid values are exact and
last-bit differences in ``sin``/``cos`` between C libraries cannot reach the
JSON (a value would have to land within one ulp of a half-grid point, about a
2^-37 chance per coordinate). Acceptance decisions and the minimum-feature
estimate run through floating-point geometry (the scene model, truth extraction,
GEOS); a different platform or library version could in principle flip a
borderline one, which the byte comparison against the committed pinned cases
(``tests/synthetic/fuzz/``) catches.

A scene at another ``size`` is drawn from the same stream with every length
proportional to ``size``, so it is the same scene scaled, unless a size-dependent
check (``MIN_EDGE_PX``, a pixel gap) rejects a draw at one size and not another.

Kinds
-----
The first draw of the stream selects the kind uniformly from ``KINDS``;
``kind=`` overrides it after that draw, so a forced kind equal to the drawn one
gives the identical scene.

- ``polygon``: one polygon of 3-20 vertices on a two-colour canvas. Vertices are
  star-shaped around a centre (radius 20-42% of the canvas, vertex radii 35-100%
  of it), with 1-3 deliberate spikes of 10-45 degree tip angle on half the seeds
  (each spike's neighbours are placed so the tip's interior angle is exact
  before any warp), and a radius-proportional twist of up to 2.5 rad on 35% of
  seeds (non-star, spiral-like outlines that may self-intersect). 15% of seeds
  push the shape across the canvas edge.
- ``blob``: one smooth closed loop of 4-16 cubic Béziers through anchors on a
  Fourier radius function (harmonics 2-5, amplitude up to 0.35 of the radius),
  converted with Catmull-Rom tangents (tension 0.6-1.2) so every join is exactly
  C1 (the two handles of an anchor are the same snapped vector, so the join
  turn is exactly 0), optionally twisted like the polygons.
- ``cornered-blob``: the same loops with 1 to (anchors - 1) labelled corner
  anchors whose two handles are rotated apart by a turn of 15-165 degrees;
  the stretch between two consecutive corners is a straight line on half the
  occasions. ``params["corners"]`` lists each labelled anchor and its turn.
- ``layered``: 2-6 palette colours (the background included) and 3-15 shapes
  stacked bottom to top, each a polygon (30%), blob (25%), cornered blob (25%),
  circle (10%) or ellipse (10%), 15% of them with a hole. Each non-background
  colour has one carrier shape with a log-uniform radius of 12-30% of the canvas,
  centred in its middle 70%, so the colour is likely to stay visible; the other
  shapes take random colours and a log-uniform radius of 4-30%, and are placed
  45% anywhere (slightly beyond the canvas allowed), 35% centred on a point of an
  earlier shape's outline (overlaps, junctions, necks), 20% slid against an
  earlier shape to leave a gap of 0.3-5 px (narrow gaps and slivers). The colour
  and shape counts and the palette are drawn once per seed; a rejection redraws
  only the geometry, so rejections do not bias the counts.

Palettes
--------
Colours are drawn so that every pair is at least ``MIN_PALETTE_DELTA_E`` (45)
CIE76 apart, well clear of the palette stage's merge thresholds
(``PaletteSettings``: similar 28, small-and-similar 36, fragmented 24), and so
that no colour lies within ``MIN_BLEND_CLEARANCE`` (20) of the linear-light
anti-alias blend between any other two (the render blends in linear light), so
an edge blend cannot be mistaken for a real colour. Every palette colour must
cover at least ``MIN_COLOUR_SHARE`` (2%) of the canvas after occlusion, twice
the palette stage's whole-colour usage cutoff (1%), so the stage does not fold a
colour away for being rare.

Rejection
---------
A drawn outline is rejected and redrawn from the same stream when the scene
model refuses it (``Outline``/``Line``/``Cubic`` validation: self-contact or
crossing, doubling back, cubic cusp, zero-length or sub-``MIN_EDGE_PX`` edges) or,
for a labelled corner, when its turn leaves 10-170 degrees. A drawn scene is
rejected and redrawn when ``scene_truth`` raises one of the geometric
degeneracies it documents (partial curve overlap, an outline through a point
twice, an undecidable side, a boundary piece off every vertex), a colour falls
under the share floor, or a visible piece of a colour is smaller than
``MIN_COMPONENT_AREA_PX`` (1 px^2: a crumb no raster of the scene can show,
typically where a shape's tip pokes a hair's breadth past another outline or
the canvas edge). Only the errors matching ``_OUTLINE_REJECTIONS`` or
``_TRUTH_REJECTIONS`` are caught; anything else propagates.

Retries are bounded: ``OUTLINE_ATTEMPTS`` per outline and ``SCENE_ATTEMPTS`` per
scene. Exhaustion takes a deterministic fallback: an outline in a layered scene
becomes a circle of the drawn centre and radius; a whole scene becomes
``_fallback_scene(kind, size)`` (fixed shapes and the fixed palette
``FALLBACK_PALETTE``), with ``params["fallback"]`` set. ``params`` also records
``attempts`` and the count of each rejection reason.

Minimum feature width
---------------------
``estimate_min_feature_px`` is documented at the function.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import Polygon

from vinylgen.colour import srgb_to_lab
from vinylgen.fidelity.cases import Case
from vinylgen.fidelity.raster import linear_to_srgb, srgb_to_linear
from vinylgen.fidelity.scene import Arc, Cubic, EllipticalArc, Line, Outline, Region, Scene
from vinylgen.fidelity.truth import SceneTruth, scene_truth

KINDS = ("polygon", "blob", "cornered-blob", "layered")
GENERATOR_VERSION = 1
GRID = 2.0 ** -16
MIN_PALETTE_DELTA_E = 45.0
MIN_BLEND_CLEARANCE = 20.0
MIN_COLOUR_SHARE = 0.02
MIN_EDGE_PX = 1.0
# A visible colour piece smaller than one pixel cannot appear in any raster of the
# scene; a scene with one is rejected.
MIN_COMPONENT_AREA_PX = 1.0
# Rings of the truth geometry below this area are overlay noise (zero-area holes).
_NOISE_AREA_PX = 1e-6
OUTLINE_ATTEMPTS = 40
SCENE_ATTEMPTS = 25
PALETTE_ATTEMPTS = 20
COLOUR_DRAWS = 300
# A fixed palette meeting both palette constraints (checked by the tests).
FALLBACK_PALETTE = ((238, 234, 222), (28, 36, 84), (214, 48, 40), (40, 150, 70),
                    (240, 190, 30), (150, 70, 170))
FUZZ_ROOT = Path(__file__).resolve().parents[2] / "tests" / "synthetic"
SEEDS_PATH = FUZZ_ROOT / "fuzz-seeds.json"
PINNED_DIR = FUZZ_ROOT / "fuzz"

# Scene-model and truth errors that mean "this random geometry is degenerate",
# matched on their messages; the reason key is what ``params`` counts.
_OUTLINE_REJECTIONS = (
    ("must not touch or cross itself", "self-intersection"),
    ("doubles back", "doubles-back"),
    ("must not have a cusp", "cubic-cusp"),
    ("control points must differ", "cubic-degenerate"),
    ("must have non-zero length", "zero-length"),
)
_TRUTH_REJECTIONS = (
    ("overlap along a stretch", "truth-partial-overlap"),
    ("passes through", "truth-multiple-pass"),
    ("cannot decide which side", "truth-undecidable-side"),
    ("doubles back on itself", "truth-doubles-back"),
    ("visible boundary ends inside", "truth-open-boundary"),
    ("lost its own outline", "truth-lost-outline"),
    ("off every vertex", "truth-piece-off-vertex"),
)


class _Rejected(Exception):
    """A drawn outline or scene failed validation; ``reason`` is its key."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _classify(error: ValueError, table) -> str | None:
    message = str(error)
    for fragment, reason in table:
        if fragment in message:
            return reason
    return None


class _Draw:
    """Every random value, read from ``Generator.random()`` only."""

    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)

    def random(self) -> float:
        return float(self.rng.random())

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self.random()

    def log_uniform(self, low: float, high: float) -> float:
        return math.exp(self.uniform(math.log(low), math.log(high)))

    def integer(self, low: int, high: int) -> int:
        """Uniform over ``low..high`` inclusive."""
        return low + min(high - low, int(self.random() * (high - low + 1)))

    def chance(self, probability: float) -> bool:
        return self.random() < probability

    def pick(self, weights: tuple[float, ...]) -> int:
        target = self.random() * sum(weights)
        for index, weight in enumerate(weights):
            target -= weight
            if target < 0.0:
                return index
        return len(weights) - 1


def _q(value: float) -> float:
    """``value`` snapped to the dyadic ``GRID``."""
    return math.floor(value / GRID + 0.5) * GRID


def _qp(point) -> tuple[float, float]:
    return (_q(point[0]), _q(point[1]))


def _outline(build) -> Outline:
    """``build()`` with scene-model validation errors turned into rejections."""
    try:
        return build()
    except ValueError as error:
        reason = _classify(error, _OUTLINE_REJECTIONS)
        if reason is None:
            raise
        raise _Rejected(reason) from error


def _check_edges(points) -> None:
    for index in range(len(points)):
        if math.dist(points[index], points[(index + 1) % len(points)]) < MIN_EDGE_PX:
            raise _Rejected("short-edge")


# --- shape generators -------------------------------------------------------------------


def _twist(draw: _Draw, points, centre, radius: float, probability: float):
    """Rotate each point about ``centre`` by an angle proportional to its distance."""
    if not draw.chance(probability):
        return points, 0.0
    strength = draw.uniform(-2.5, 2.5)
    twisted = []
    for x, y in points:
        dx, dy = x - centre[0], y - centre[1]
        angle = strength * math.hypot(dx, dy) / radius
        c, s = math.cos(angle), math.sin(angle)
        twisted.append((centre[0] + c * dx - s * dy, centre[1] + s * dx + c * dy))
    return twisted, strength


def _interior_angles(points) -> list[float]:
    """Interior angle at each vertex of a simple polygon, in degrees."""
    area = 0.5 * sum(points[i - 1][0] * points[i][1] - points[i][0] * points[i - 1][1]
                     for i in range(len(points)))
    # Interior on the left of travel (positive area) turns left at convex vertices.
    orientation = 1.0 if area > 0 else -1.0
    angles = []
    for i in range(len(points)):
        a, b, c = points[i - 1], points[i], points[(i + 1) % len(points)]
        u = (b[0] - a[0], b[1] - a[1])
        v = (c[0] - b[0], c[1] - b[1])
        turn = math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1])
        angles.append(180.0 - math.degrees(turn) * orientation)
    return angles


def _polygon_shape(draw: _Draw, centre, radius: float, twist: float = 0.35):
    count = draw.integer(3, 20)
    gaps = [0.35 + draw.random() for _ in range(count)]
    total = sum(gaps)
    start = draw.uniform(0.0, 2.0 * math.pi)
    angles, running = [], start
    for gap in gaps:
        angles.append(running)
        running += 2.0 * math.pi * gap / total
    radii = [radius * draw.uniform(0.35, 1.0) for _ in range(count)]
    spikes = []
    if count >= 4 and draw.chance(0.5):
        wanted = draw.integer(1, max(1, min(3, count // 3)))
        for _ in range(wanted):
            index = draw.integer(0, count - 1)
            tip = draw.uniform(10.0, 45.0)
            if any(min((index - other) % count, (other - index) % count) <= 2
                   for other, _ in spikes):
                continue
            half = math.radians(tip) / 2.0
            reach = radius * draw.uniform(1.0, 1.15)
            ok = True
            offsets = []
            for side in (-1, 1):
                neighbour = (index + side) % count
                sine = math.sin(half) * reach / radii[neighbour]
                if sine >= 1.0:
                    ok = False
                    break
                offsets.append(math.asin(sine) - half)
            previous_gap = (angles[index] - angles[index - 1]) % (2.0 * math.pi)
            next_gap = (angles[(index + 1) % count] - angles[index]) % (2.0 * math.pi)
            if not ok or offsets[0] >= 0.9 * previous_gap or offsets[1] >= 0.9 * next_gap:
                continue
            radii[index] = reach
            angles[index - 1] = angles[index] - offsets[0]
            angles[(index + 1) % count] = angles[index] + offsets[1]
            spikes.append((index, tip))
    points = [(centre[0] + r * math.cos(a), centre[1] + r * math.sin(a))
              for r, a in zip(radii, angles)]
    points, strength = _twist(draw, points, centre, radius * 1.15, twist)
    points = [_qp(p) for p in points]
    _check_edges(points)
    outline = _outline(lambda: Outline(tuple(Line(points[i], points[(i + 1) % count])
                                             for i in range(count))))
    return outline, {"shape": "polygon", "vertices": count, "spikes": len(spikes),
                     "twist": round(strength, 6),
                     "min_interior_angle_deg": round(min(_interior_angles(points)), 4)}


def _anchors(draw: _Draw, centre, radius: float, twist: float):
    count = draw.integer(4, 16)
    harmonics = []
    for k in range(2, 6):
        amplitude = draw.uniform(-0.35, 0.35) / (k - 1)
        harmonics.append((k, amplitude, draw.uniform(0.0, 2.0 * math.pi)))
    start = draw.uniform(0.0, 2.0 * math.pi)
    step = 2.0 * math.pi / count
    points = []
    for index in range(count):
        angle = start + step * (index + draw.uniform(-0.3, 0.3))
        scale = 1.0 + sum(a * math.cos(k * angle + phase) for k, a, phase in harmonics)
        scale = max(scale, 0.25)
        points.append((centre[0] + radius * scale * math.cos(angle),
                       centre[1] + radius * scale * math.sin(angle)))
    points, strength = _twist(draw, points, centre, radius * 1.6, twist)
    return points, strength


def _handles(draw: _Draw, points):
    """Catmull-Rom handle offsets (a third of the tangent), snapped."""
    tension = draw.uniform(0.6, 1.2)
    count = len(points)
    handles = []
    for index in range(count):
        before, after = points[index - 1], points[(index + 1) % count]
        handles.append(_qp(((after[0] - before[0]) * tension / 6.0,
                            (after[1] - before[1]) * tension / 6.0)))
    return handles, tension


def _rotate(vector, angle: float):
    c, s = math.cos(angle), math.sin(angle)
    return (c * vector[0] - s * vector[1], s * vector[0] + c * vector[1])


def _blob_shape(draw: _Draw, centre, radius: float, corners: bool, twist: float = 0.3):
    raw, strength = _anchors(draw, centre, radius, twist)
    points = [_qp(p) for p in raw]
    count = len(points)
    _check_edges(points)
    handles, tension = _handles(draw, points)
    # Each anchor's incoming handle offset (anchor minus the previous segment's
    # second control point) and outgoing offset (first control point minus anchor).
    incoming = list(handles)
    outgoing = list(handles)
    corner_turns: dict[int, float] = {}
    straight: set[int] = set()
    if corners:
        wanted = draw.integer(1, max(1, count - 1))
        for index in sorted({draw.integer(0, count - 1) for _ in range(wanted)}):
            turn = draw.uniform(15.0, 165.0) * (1.0 if draw.chance(0.5) else -1.0)
            half = math.radians(turn) / 2.0
            incoming[index] = _qp(_rotate(handles[index], -half))
            outgoing[index] = _qp(_rotate(handles[index], half))
            incoming[index] = _qp(tuple(v * draw.uniform(0.5, 1.2) for v in incoming[index]))
            outgoing[index] = _qp(tuple(v * draw.uniform(0.5, 1.2) for v in outgoing[index]))
            corner_turns[index] = turn
        for index in corner_turns:
            if (index + 1) % count in corner_turns and draw.chance(0.5):
                straight.add(index)

    def build():
        segments = []
        for index in range(count):
            following = (index + 1) % count
            p0, p1 = points[index], points[following]
            if index in straight:
                segments.append(Line(p0, p1))
                continue
            h0, h1 = outgoing[index], incoming[following]
            segments.append(Cubic(p0, (p0[0] + h0[0], p0[1] + h0[1]),
                                  (p1[0] - h1[0], p1[1] - h1[1]), p1))
        return Outline(tuple(segments))

    outline = _outline(build)
    params = {"shape": "cornered-blob" if corners else "blob", "anchors": count,
              "tension": round(tension, 6), "twist": round(strength, 6)}
    if corners:
        turns = dict(enumerate(turn for _, turn in outline.join_turns()))
        labelled = []
        for index in sorted(corner_turns):
            turn = turns[index]
            if not 10.0 <= turn <= 170.0:
                raise _Rejected("corner-turn-range")
            labelled.append({"anchor": index, "turn_deg": round(turn, 4),
                             "straight_after": index in straight})
        for index, turn in turns.items():
            if index not in corner_turns and turn != 0.0:
                raise RuntimeError("a smooth anchor must join with zero turn")
        params["corners"] = labelled
    return outline, params


def _circle_shape(draw: _Draw, centre, radius: float):
    r = _q(radius * draw.uniform(0.5, 1.0))
    start = _q(draw.uniform(0.0, 2.0 * math.pi))
    return (_outline(lambda: Outline((Arc(_qp(centre), r, start, 2.0 * math.pi),))),
            {"shape": "circle"})


def _ellipse_shape(draw: _Draw, centre, radius: float):
    rx = _q(radius * draw.uniform(0.5, 1.0))
    ry = _q(rx * draw.uniform(0.2, 0.9))
    rotation = _q(draw.uniform(0.0, math.pi))
    start = _q(draw.uniform(0.0, 2.0 * math.pi))
    return (_outline(lambda: Outline((EllipticalArc(_qp(centre), rx, ry, rotation, start,
                                                     2.0 * math.pi),))),
            {"shape": "ellipse"})


def _translated(outline: Outline, dx: float, dy: float) -> Outline:
    """``outline`` moved by a grid vector (exact: every coordinate stays on the grid)."""
    def move(p):
        return (p[0] + dx, p[1] + dy)

    segments = []
    for segment in outline.segments:
        if isinstance(segment, Line):
            segments.append(Line(move(segment.p0), move(segment.p1)))
        elif isinstance(segment, Cubic):
            segments.append(Cubic(*(move(p) for p in (segment.p0, segment.c1, segment.c2,
                                                      segment.p1))))
        elif isinstance(segment, Arc):
            segments.append(Arc(move(segment.centre), segment.radius, segment.start,
                                segment.sweep))
        else:
            segments.append(EllipticalArc(move(segment.centre), segment.rx, segment.ry,
                                          segment.rotation, segment.start, segment.sweep))
    return _outline(lambda: Outline(tuple(segments)))


def _shape_polygon(outline: Outline) -> Polygon:
    return Polygon(outline.flatten(0.05))


# --- palettes ---------------------------------------------------------------------------


def _blend_lab(first, second) -> np.ndarray:
    """Lab of the linear-light blends of two sRGB colours at 10%..90%."""
    weights = np.linspace(0.1, 0.9, 9)[:, None]
    linear = (1.0 - weights) * srgb_to_linear(first) + weights * srgb_to_linear(second)
    return srgb_to_lab(np.rint(linear_to_srgb(linear) * 255.0))


def palette_violations(colours) -> list[str]:
    """Why a palette breaks the fuzzer's colour constraints (empty when it meets them)."""
    rgb = np.asarray(colours, dtype=np.float64)
    lab = srgb_to_lab(rgb)
    problems = []
    for a in range(len(rgb)):
        for b in range(a + 1, len(rgb)):
            distance = float(np.linalg.norm(lab[a] - lab[b]))
            if distance < MIN_PALETTE_DELTA_E:
                problems.append(f"colours {a} and {b} are {distance:.1f} apart")
            blends = _blend_lab(rgb[a], rgb[b])
            for c in range(len(rgb)):
                if c in (a, b):
                    continue
                clearance = float(np.min(np.linalg.norm(blends - lab[c], axis=1)))
                if clearance < MIN_BLEND_CLEARANCE:
                    problems.append(f"colour {c} is {clearance:.1f} from the {a}/{b} blend")
    return problems


def _palette(draw: _Draw, count: int, stats: dict) -> list[tuple[int, int, int]]:
    for _ in range(PALETTE_ATTEMPTS):
        chosen: list[tuple[int, int, int]] = []
        while len(chosen) < count:
            for _ in range(COLOUR_DRAWS):
                candidate = tuple(draw.integer(0, 255) for _ in range(3))
                if not palette_violations(chosen + [candidate]):
                    chosen.append(candidate)
                    break
            else:
                break
        if len(chosen) == count:
            return chosen
        stats["palette-restart"] = stats.get("palette-restart", 0) + 1
    stats["palette-fallback"] = stats.get("palette-fallback", 0) + 1
    return list(FALLBACK_PALETTE[:count])


def _palette_rows(colours) -> list[dict]:
    return [{"name": f"colour-{index}", "rgb": list(rgb)} for index, rgb in enumerate(colours)]


# --- scenes -----------------------------------------------------------------------------


def _count(stats: dict, reason: str) -> None:
    stats[reason] = stats.get(reason, 0) + 1


def _retry_outline(stats: dict, make):
    """``make()`` redrawn until it passes, at most ``OUTLINE_ATTEMPTS`` times; None
    when every attempt was rejected."""
    for _ in range(OUTLINE_ATTEMPTS):
        try:
            return make()
        except _Rejected as rejection:
            _count(stats, rejection.reason)
    return None


def _single_scene(draw: _Draw, kind: str, size: int, colours: list, stats: dict) -> Scene:
    centre = (size * draw.uniform(0.42, 0.58), size * draw.uniform(0.42, 0.58))
    if draw.chance(0.15):
        side = draw.integer(0, 3)
        shift = size * draw.uniform(0.25, 0.4)
        centre = (centre[0] + shift * (1, -1, 0, 0)[side], centre[1] + shift * (0, 0, 1, -1)[side])
    if kind == "polygon":
        radius = size * draw.uniform(0.2, 0.42)
        made = _retry_outline(stats, lambda: _polygon_shape(draw, centre, radius))
    else:
        radius = size * draw.uniform(0.18, 0.3)
        made = _retry_outline(stats, lambda: _blob_shape(draw, centre, radius,
                                                         kind == "cornered-blob"))
    if made is None:
        raise _Rejected("outline-exhausted")
    outline, shape = made
    return Scene(size, size, _palette_rows(colours), 0, [Region(1, outline)], "fuzz",
                 {"shape": shape})


def _layered_shape(draw: _Draw, centre, radius: float):
    choice = draw.pick((0.30, 0.25, 0.25, 0.10, 0.10))
    if choice == 0:
        return _polygon_shape(draw, centre, radius, twist=0.25)
    if choice in (1, 2):
        return _blob_shape(draw, centre, radius, choice == 2, twist=0.2)
    if choice == 3:
        return _circle_shape(draw, centre, radius)
    return _ellipse_shape(draw, centre, radius)


def _slide_to_gap(draw: _Draw, outline: Outline, target: Polygon, stats: dict):
    """``outline`` moved along the line through its centroid and ``target``'s so
    that the two lie a drawn 0.3-5 px apart (to the 0.05 px flattening), or None
    when that line offers no such position."""
    gap = draw.uniform(0.3, 5.0)
    shape = _shape_polygon(outline)
    here, there = shape.centroid, target.centroid
    direction = np.asarray([here.x - there.x, here.y - there.y])
    length = float(np.hypot(*direction))
    if length < 1e-6:
        direction, length = np.asarray([1.0, 0.0]), 1.0
    direction /= length

    def distance(offset: float) -> float:
        moved = shapely.affinity.translate(shape, *(direction * offset))
        return float(moved.distance(target))

    # ``near`` is closer than the gap, ``far`` at least the gap away: moving the
    # centroids together gives the first, moving apart by both diameters the second.
    far = float(sum(np.hypot(g.bounds[2] - g.bounds[0], g.bounds[3] - g.bounds[1])
                    for g in (shape, target)))
    near = 0.0 if distance(0.0) < gap else -length
    if distance(near) >= gap or distance(far) < gap:
        _count(stats, "gap-unplaced")
        return None
    for _ in range(50):
        middle = 0.5 * (near + far)
        if distance(middle) < gap:
            near = middle
        else:
            far = middle
    dx, dy = _qp(direction * far)
    return _translated(outline, dx, dy), round(gap, 6)


def _layered_scene(draw: _Draw, size: int, colours: list, shape_count: int,
                   stats: dict) -> Scene:
    colour_count = len(colours)
    regions: list[Region] = []
    polygons: list[Polygon] = []
    shapes = []
    # Every colour except the background has one carrier shape, large (radius at
    # least 12% of the canvas) and freely placed inside the canvas so the colour
    # is likely to stay visible; the other shapes take random colours.
    assignment = ([(colour, True) for colour in range(1, colour_count)]
                  + [(draw.integer(0, colour_count - 1), False)
                     for _ in range(shape_count - colour_count + 1)])
    for index in range(shape_count - 1, 0, -1):
        other = draw.integer(0, index)
        assignment[index], assignment[other] = assignment[other], assignment[index]
    for colour, carrier in assignment:
        radius = size * draw.log_uniform(0.12 if carrier else 0.04, 0.3)
        mode = 0 if carrier or not polygons else draw.pick((0.45, 0.35, 0.20))
        if mode == 1:
            ring = regions[draw.integer(0, len(regions) - 1)].outline.flatten(0.05)
            x, y = ring[draw.integer(0, len(ring) - 1)]
            centre = (float(x) + radius * draw.uniform(-0.3, 0.3),
                      float(y) + radius * draw.uniform(-0.3, 0.3))
        else:
            low, high = (0.15, 0.85) if carrier else (-0.05, 1.05)
            centre = (size * draw.uniform(low, high), size * draw.uniform(low, high))
        made = _retry_outline(stats, lambda: _layered_shape(draw, centre, radius))
        if made is None:
            _count(stats, "outline-fallback-circle")
            made = (Outline((Arc(_qp(centre), _q(radius), 0.0, 2.0 * math.pi),)),
                    {"shape": "circle", "fallback": True})
        outline, shape = made
        shape = dict(shape, placement=("free", "on-outline", "gap")[mode], carrier=carrier)
        if mode == 2:
            target = polygons[draw.integer(0, len(polygons) - 1)]
            try:
                slid = _slide_to_gap(draw, outline, target, stats)
            except _Rejected as rejection:
                _count(stats, rejection.reason)
                slid = None
            if slid is not None:
                outline, shape["gap_px"] = slid
            else:
                shape["placement"] = "free"
        holes = ()
        if draw.chance(0.15):
            hole = _retry_outline(stats, lambda: _blob_shape(
                draw, _hole_centre(outline), radius * draw.uniform(0.15, 0.35), False, twist=0.0))
            if hole is not None:
                inner = _shape_polygon(hole[0])
                if _shape_polygon(outline).buffer(-1.0).contains(inner):
                    holes = (hole[0],)
                    shape["hole"] = True
                else:
                    _count(stats, "hole-outside")
        regions.append(Region(colour, outline, holes))
        polygons.append(_shape_polygon(outline))
        shapes.append(shape)
    return Scene(size, size, _palette_rows(colours), 0, regions, "fuzz",
                 {"colours": colour_count, "shapes": shapes})


def _hole_centre(outline: Outline):
    point = _shape_polygon(outline).representative_point()
    return (point.x, point.y)


def _validate(scene: Scene) -> SceneTruth:
    """The scene's truth, or a rejection when truth extraction meets a degeneracy,
    a colour covers less than ``MIN_COLOUR_SHARE`` of the canvas, or a visible
    piece of a colour is smaller than ``MIN_COMPONENT_AREA_PX``."""
    try:
        truth = scene_truth(scene)
    except ValueError as error:
        reason = _classify(error, _TRUTH_REJECTIONS)
        if reason is None:
            raise
        raise _Rejected(reason) from error
    canvas = float(scene.width * scene.height)
    for colour in range(len(scene.palette)):
        geometry = truth.colour_geometry.get(colour)
        if geometry is None or geometry.area < MIN_COLOUR_SHARE * canvas:
            raise _Rejected("colour-share")
        if any(part.area < MIN_COMPONENT_AREA_PX for part in shapely.get_parts(geometry)):
            raise _Rejected("crumb")
    return truth


def _fallback_scene(kind: str, size: int) -> Scene:
    """The fixed scene used when every drawn scene was rejected."""
    s = float(size)
    centre = (_q(0.5 * s), _q(0.5 * s))
    palette = _palette_rows(FALLBACK_PALETTE[:4 if kind == "layered" else 2])
    if kind == "polygon":
        outline = Outline(tuple(Line(*(_qp((centre[0] + 0.3 * s * math.cos(a),
                                            centre[1] + 0.3 * s * math.sin(a)))
                                       for a in (2 * math.pi * i / 5, 2 * math.pi * (i + 1) / 5)))
                                for i in range(5)))
        return Scene(size, size, palette, 0, [Region(1, outline)], "fuzz", {})
    if kind in ("blob", "cornered-blob"):
        outline = Outline((EllipticalArc(centre, _q(0.3 * s), _q(0.2 * s), 0.0, 0.0,
                                         2.0 * math.pi),))
        return Scene(size, size, palette, 0, [Region(1, outline)], "fuzz", {})
    regions = [Region(1, Outline((Arc((_q(0.35 * s), _q(0.4 * s)), _q(0.25 * s), 0.0,
                                      2.0 * math.pi),))),
               Region(2, Outline(tuple(Line(a, b) for a, b in zip(
                   [(_q(0.4 * s), _q(0.3 * s)), (_q(0.9 * s), _q(0.35 * s)),
                    (_q(0.6 * s), _q(0.9 * s))],
                   [(_q(0.9 * s), _q(0.35 * s)), (_q(0.6 * s), _q(0.9 * s)),
                    (_q(0.4 * s), _q(0.3 * s))])))),
               Region(3, Outline((EllipticalArc((_q(0.3 * s), _q(0.75 * s)), _q(0.2 * s),
                                                _q(0.1 * s), 0.0, 0.0, 2.0 * math.pi),)))]
    return Scene(size, size, palette, 0, regions, "fuzz", {})


def fuzz_scene(seed: int, size: int = 512, kind: str | None = None) -> tuple[Scene, SceneTruth]:
    """The fuzzed scene of ``seed`` at a ``size`` x ``size`` canvas, with its truth."""
    if not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError("a fuzz seed is a non-negative integer")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown fuzz kind {kind!r}; expected one of {KINDS}")
    size = int(size)
    draw = _Draw(int(seed))
    drawn = KINDS[draw.integer(0, len(KINDS) - 1)]
    kind = kind or drawn
    stats: dict[str, int] = {}
    # The scene's structure (colour and shape counts, palette) is drawn once;
    # only its geometry is redrawn on rejection, so rejections do not bias the
    # structure towards scenes that pass more easily.
    if kind == "layered":
        colour_count = draw.integer(2, 6)
        shape_count = draw.integer(max(3, colour_count - 1), 15)
    else:
        colour_count, shape_count = 2, 1
    colours = _palette(draw, colour_count, stats)
    scene = truth = None
    attempts = 0
    for attempts in range(1, SCENE_ATTEMPTS + 1):
        try:
            if kind == "layered":
                candidate = _layered_scene(draw, size, colours, shape_count, stats)
            else:
                candidate = _single_scene(draw, kind, size, colours, stats)
            truth = _validate(candidate)
            scene = candidate
            break
        except _Rejected as rejection:
            _count(stats, rejection.reason)
    fallback = scene is None
    if fallback:
        scene = _fallback_scene(kind, size)
        truth = scene_truth(scene)
    scene.params = {"generator_version": GENERATOR_VERSION, "seed": int(seed), "size": size,
                    "kind": kind, "attempts": attempts, "fallback": fallback,
                    "rejections": dict(sorted(stats.items())), **scene.params}
    return scene, truth


def case_id(seed: int, size: int, kind: str) -> str:
    return f"fuzz-{kind}-s{int(seed)}-{int(size)}"


def fuzz_case(seed: int, size: int = 512, kind: str | None = None) -> Case:
    """One fuzzed case: the scene, its estimated minimum feature width and the
    tags ``("fuzz", kind)``."""
    scene, truth = fuzz_scene(seed, size, kind)
    kind = scene.params["kind"]
    width = estimate_min_feature_px(truth)
    scene.params["min_feature_px"] = width
    return Case(case_id(seed, size, kind), scene, width, ("fuzz", kind))


# --- minimum feature width --------------------------------------------------------------

_FEATURE_START = 0.05
_FEATURE_RATIO = 1.25
_FEATURE_BISECTIONS = 8
_SMOOTH_TIP_DEG = 30.0
_PROTRUSION_SLACK = 0.5
_SIMPLIFY = 0.01
_QUAD_SEGS = 16
_SAMPLE_PX = 0.5
_REMOVED_PX = 0.05
_ERODED_NOISE_PX = 0.02


def _is_noise(polygon: Polygon) -> bool:
    """Empty after a further ``_ERODED_NOISE_PX`` erosion: too thin to be told
    apart from a buffer artefact."""
    return polygon.buffer(-_ERODED_NOISE_PX, quad_segs=2).is_empty


def estimate_min_feature_px(truth: SceneTruth) -> float:
    """Approximate narrowest width of any colour's visible geometry, in pixels.

    Method (morphological opening): each colour's visible geometry
    (``truth.colour_geometry``, simplified by 0.01 px) is split into connected
    components and eroded by a radius ``r``. A feature of width ``2r`` is lost at
    ``r`` when, for some component,

    - the erosion no longer has exactly one part with as many holes: a neck or
      a narrow strip splits it, a narrow stretch between a hole and the outside
      (or another hole) opens, or the whole component (a small blob, a narrow gap
      or sliver) vanishes; or
    - opening (erosion, then dilation back by ``r``) removes part of the
      component's boundary by more than a tip could lose: an arm that erodes
      away without splitting anything. The boundary is sampled every 0.5 px; a
      sample's loss ``p`` is its distance from the opened shape (0 within 0.05 px
      of it), and the feature is lost when some sample has
      ``p > allowance + 0.5 px``.

    Opening by ``r`` removes only tips from a shape with no feature narrower than
    ``2r``: a true corner's eroded tip, or a smooth tip, taken to be at least 30
    degrees (``S = r (1 / sin 15deg - 1)``, about ``2.86 r``). A true corner
    (corner, junction or edge point on the component's boundary) with tip angle
    ``a = 180 - turn_deg`` (at least 0.5 degree) has its tip replaced by the
    radius-``r`` arc tangent to both sides ``t = r cot(a / 2)`` from the apex, so
    the apex loses ``A = r (1 / sin(a / 2) - 1)`` and the loss falls
    monotonically along each side (``sqrt(r^2 + (t - s)^2) - r`` at ``s`` from
    the apex) to 0 at ``t``. The allowance at a sample ``x`` is
    ``max(R(x), T(x))``:

    - ``R(x) = min(S, min_c (A_c + |x - c|))``: no sample loses more than a
      smooth tip, and since ``p`` is 1-Lipschitz, nothing within ``|x - c|`` of
      a corner whose own tip loses at most ``A_c`` loses more than
      ``A_c + |x - c|``. This finds an arm's square end (its end corners lose
      the arm's length) and bounds loss beside an obtuse corner without
      attributing loss far from the corner to it.
    - ``T(x) = max_c min(A_c, the least loss on the boundary from c's apex to x,
      either way round)``: a corner's eroded tip never loses more farther from
      its apex, so loss reached from the apex without first falling is that
      tip's, whatever the curvature of its sides; loss behind a dip (an arm
      beside the corner) is not. This keeps a corner sharper than 30 degrees,
      and a compound tip (a blunt corner on a sharper corner's side), from
      counting as narrow.

    Both terms are continuous in the geometry and in ``r`` (minima and maxima of
    distances and losses), so no removal is attributed to a corner merely because
    it reaches the corner, and a small change of the geometry moves the estimate
    by a correspondingly small amount.

    The radius is scanned from 0.05 px in steps of 25% until the first loss, then
    bisected eight times; the result is twice that radius.

    The result depends only on the geometry, never on where its rings start or
    which way they run: each colour's geometry is normalised (``shapely.normalize``:
    every ring starts at its least vertex and runs a fixed way) before it is
    simplified, since the simplification always keeps a ring's first vertex.

    Pieces and holes smaller than 1e-6 px^2 are overlay noise of the truth
    geometry and are ignored. So are eroded parts and eroded holes thinner than
    0.02 px (empty after a further 0.02 px erosion), and the opening dilates the
    one remaining part: a negative buffer of a finely flattened acute corner can
    leave a detached speck (thousandths of a pixel across, growing with ``r``, so
    no true erosion) near the eroded tip, which is not a feature. A true hole of
    the erosion is never that thin: erosion grows holes by ``r``.

    Error and limits: the numerical error is about 0.06 px plus 0.2% (the 0.01 px
    simplification, a part lost up to 0.02 px of radius early as noise, 16-segment
    quarter circles in the buffers, bisection). By construction acute corners do
    not count as narrow (their width is zero at the apex); corner fidelity is
    measured by the corner metrics instead. A round-ended arm is detected only
    once it protrudes more than ``S`` (about 3r) beyond the opened shape, so the
    estimate can exceed the width of a short stub, and an arm whose loss falls
    continuously from a corner's apex is judged as that corner's tip; a
    square-ended arm is found through its own end corners (``A`` is about 0.41r
    at 90 degrees). ``A`` is the loss of the sharpest sector at the corner, so for
    a colour whose sector there is blunter or concave the bound is looser than it
    could be. A tip whose sides curve inward, or a compound tip, loses more at
    its apex than ``A`` and registers once the excess exceeds 0.5 px, which
    underestimates; so does a smooth end sharper than 30 degrees. The scan assumes
    the first loss is the narrowest one; a feature lost only between two scan
    radii is found at the next.
    """
    corner_xy = np.asarray([corner.point for corner in truth.corners], dtype=float).reshape(-1, 2)
    corner_tip = np.radians([max(0.5, 180.0 - corner.turn_deg) for corner in truth.corners])
    corner_points = shapely.points(corner_xy)
    components = []
    for geometry in truth.colour_geometry.values():
        simplified = shapely.normalize(geometry).simplify(_SIMPLIFY)
        for part in shapely.get_parts(shapely.make_valid(simplified)):
            if isinstance(part, Polygon) and part.area > _NOISE_AREA_PX:
                part = Polygon(part.exterior, [ring for ring in part.interiors
                                               if Polygon(ring).area > _NOISE_AREA_PX])
                # The true corners on the component's boundary: exact on the
                # truth outline, which the simplification moves by at most 0.01 px.
                own = shapely.distance(part.boundary, corner_points) <= _REMOVED_PX
                rings = []
                for ring in (part.exterior, *part.interiors):
                    coords = shapely.get_coordinates(shapely.segmentize(ring, _SAMPLE_PX))[:-1]
                    on_ring = own & (shapely.distance(ring, corner_points) <= _REMOVED_PX)
                    apexes = np.asarray([int(np.argmin(np.hypot(*(coords - point).T)))
                                         for point in corner_xy[on_ring]], dtype=int)
                    rings.append((coords, shapely.points(coords), apexes, corner_tip[on_ring]))
                components.append((part, rings, corner_xy[own], corner_tip[own]))
    if not components:
        return 0.0
    smooth_half = math.radians(_SMOOTH_TIP_DEG) / 2.0

    def lost(radius: float) -> bool:
        smooth_loss = radius * (1.0 / math.sin(smooth_half) - 1.0)
        for component, rings, corners, tips in components:
            eroded = component.buffer(-radius, quad_segs=_QUAD_SEGS)
            parts = [part for part in shapely.get_parts(eroded)
                     if not part.is_empty and not _is_noise(part)]
            if len(parts) != 1:
                return True
            holes = sum(1 for ring in parts[0].interiors if not _is_noise(Polygon(ring)))
            if holes != len(component.interiors):
                return True
            opened = parts[0].buffer(radius, quad_segs=_QUAD_SEGS)
            kept = opened.buffer(_REMOVED_PX, quad_segs=2)
            shapely.prepare(kept)
            apex_loss = radius * (1.0 / np.sin(tips / 2.0) - 1.0)
            for coords, points, apexes, apex_tips in rings:
                removed = ~shapely.contains_xy(kept, coords[:, 0], coords[:, 1])
                if not removed.any():
                    continue
                loss = np.zeros(len(coords))
                loss[removed] = shapely.distance(points[removed], opened)
                allowance = np.full(int(removed.sum()), smooth_loss)
                if len(corners):
                    apart = np.hypot(coords[removed, 0, None] - corners[None, :, 0],
                                     coords[removed, 1, None] - corners[None, :, 1])
                    allowance = np.minimum(allowance, (apex_loss + apart).min(axis=1))
                    tip = np.zeros(len(coords))
                    steps = np.arange(len(coords))
                    for apex, angle in zip(apexes, apex_tips):
                        cap = radius * (1.0 / math.sin(angle / 2.0) - 1.0)
                        for order in ((apex + steps) % len(coords), (apex - steps) % len(coords)):
                            falling = np.empty(len(coords))
                            falling[order] = np.minimum.accumulate(loss[order])
                            tip = np.maximum(tip, np.minimum(falling, cap))
                    allowance = np.maximum(allowance, tip[removed])
                if (loss[removed] > allowance + _PROTRUSION_SLACK).any():
                    return True
        return False

    low, high = 0.0, _FEATURE_START
    while not lost(high):
        if high > truth.width + truth.height:
            raise RuntimeError("no radius erodes the scene away")
        low, high = high, high * _FEATURE_RATIO
    for _ in range(_FEATURE_BISECTIONS):
        middle = 0.5 * (low + high)
        if lost(middle):
            high = middle
        else:
            low = middle
    return round(2.0 * high, 3)


# --- pinned cases -----------------------------------------------------------------------


def case_json(case: Case) -> str:
    """The committed JSON text of a case: its id, feature width, tags and scene, in
    the fixed-set file format (one-space indent, trailing newline)."""
    return json.dumps({"case_id": case.case_id, "min_feature_px": case.min_feature_px,
                       "tags": list(case.tags), "scene": case.scene.to_json()},
                      indent=1) + "\n"


def case_from_json(text: str) -> Case:
    data = json.loads(text)
    return Case(data["case_id"], Scene.from_json(data["scene"]), float(data["min_feature_px"]),
                tuple(data["tags"]))


def load_seeds(path: Path = SEEDS_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def pinned_cases(path: Path = SEEDS_PATH) -> list[Case]:
    """The pinned cases listed in ``path`` (``tests/synthetic/fuzz-seeds.json``),
    read from their committed scene files in the ``fuzz/`` directory beside it
    (the committed files are the ground truth; the tests check that the generator
    still reproduces them byte for byte)."""
    path = Path(path)
    directory = path.parent / "fuzz"
    cases = []
    for row in load_seeds(path)["pinned"]:
        name = case_id(row["seed"], row["size"], row["kind"])
        cases.append(case_from_json((directory / f"{name}.json").read_text(encoding="utf-8")))
    return cases


def pinned_rows(path: Path = SEEDS_PATH) -> list[dict]:
    return list(load_seeds(path)["pinned"])
