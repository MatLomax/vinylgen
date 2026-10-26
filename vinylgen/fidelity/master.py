"""The master case: one composed synthetic scene carrying every feature the tracer
must handle, for a fast development loop.

Most features sit along one long outline, the main navy region, traced
clockwise with the interior on the right. Each feature is separated from the
next by a clean straight run, so a miss is the feature's own fault. The
exceptions are the notch, the short edge and the slot, which test crowding on
purpose.

Features that cannot live on one outline are separate regions:
- a hole with a tiny off-pixel disc in it;
- an aspect-8 ellipse;
- a thin ring, and a thin channel of one colour between two others;
- concentric three-colour bands;
- wedges cut across the edge where three colours meet, and a bar crossing it
  square to make T-junctions;
- two squares touching at a single pixel corner;
- tiny polygons beside a tiny disc;
- a thin diagonal bar;
- near-axis bars at 1 and 3 degrees;
- pixel-aligned and half-pixel-offset squares;
- shapes cut by the image edge;
- a thin dark outline, and two near-identical shades side by side, for the
  palette stage;
- curved outlines crossing or touching: two overlapping discs of different
  colours and of one colour, a disc over an ellipse, a disc over a cubic-edged
  blob, two cubic-edged blobs crossing, and two pairs of circles touching at a
  single point, externally and internally.

The geometry is authored at ``MASTER_SIZE`` and scaled for the other render
sizes (``MASTER_SIZES``). Trace lengths are thousandths of the image, so each
size exercises a different resolution. ``FEATURES`` records, per feature, its
name and position at ``MASTER_SIZE``, for annotation and per-feature reporting.
"""

from __future__ import annotations

import math

from vinylgen.fidelity.cases import Case
from vinylgen.fidelity.scene import (Arc, Cubic, Line, Outline, Region, Scene, circle, ellipse,
                                     polygon, rectangle)

MASTER_SIZE = 1024
MASTER_SIZES = (1024, 256)

PALETTE = [
    {"name": "paper", "rgb": [238, 232, 218]},
    {"name": "navy", "rgb": [32, 44, 98]},
    {"name": "gold", "rgb": [240, 190, 40]},
    {"name": "red", "rgb": [200, 40, 44]},
    {"name": "sky", "rgb": [96, 170, 226]},
    {"name": "black", "rgb": [22, 22, 26]},
    {"name": "green", "rgb": [34, 139, 76]},
    {"name": "green-near", "rgb": [40, 146, 82]},
]
PAPER, NAVY, GOLD, RED, SKY, BLACK, GREEN, GREEN_NEAR = range(len(PALETTE))

_DEG = math.pi / 180.0
_START = (230.0, 170.0)


class _Turtle:
    """Builds an outline from straight runs, corners, tangent arcs and tangent
    cubics. Headings are degrees in image coordinates (y down): 0 is east, 90
    south, and a positive turn is clockwise on screen."""

    def __init__(self, start, heading: float):
        self.start = (float(start[0]), float(start[1]))
        self.point = self.start
        self.heading = float(heading)
        self.segments: list = []
        self.features: list[tuple[str, tuple[float, float]]] = []

    def _unit(self, heading: float) -> tuple[float, float]:
        return (math.cos(heading * _DEG), math.sin(heading * _DEG))

    def mark(self, name: str, point=None) -> None:
        self.features.append((name, point or self.point))

    def line(self, length: float) -> None:
        ux, uy = self._unit(self.heading)
        end = (self.point[0] + length * ux, self.point[1] + length * uy)
        self.segments.append(Line(self.point, end))
        self.point = end

    def corner(self, turn: float, name: str | None = None) -> None:
        if name:
            self.mark(name)
        self.heading += turn

    def arc(self, radius: float, sweep: float, name: str | None = None) -> None:
        """A tangent arc turning by ``sweep`` degrees (positive: clockwise)."""
        h = self.heading * _DEG
        side = 1.0 if sweep > 0 else -1.0
        centre = (self.point[0] - side * radius * math.sin(h),
                  self.point[1] + side * radius * math.cos(h))
        start = h - side * math.pi / 2.0
        end_angle = start + sweep * _DEG
        if name:
            middle = start + sweep * _DEG / 2.0
            self.mark(name, (centre[0] + radius * math.cos(middle),
                             centre[1] + radius * math.sin(middle)))
        self.segments.append(Arc(centre, radius, start, sweep * _DEG))
        self.point = (centre[0] + radius * math.cos(end_angle),
                      centre[1] + radius * math.sin(end_angle))
        self.heading += sweep

    def cubic(self, forward: float, side: float, turn: float, first: float, last: float,
              name: str | None = None) -> None:
        """A tangent cubic ending ``forward`` along the heading and ``side`` to its
        right, turned by ``turn`` degrees, with handles ``first`` and ``last`` long.
        Unequal handles give a curvature that varies along the curve."""
        ux, uy = self._unit(self.heading)
        rx, ry = -uy, ux
        end = (self.point[0] + forward * ux + side * rx, self.point[1] + forward * uy + side * ry)
        vx, vy = self._unit(self.heading + turn)
        c1 = (self.point[0] + first * ux, self.point[1] + first * uy)
        c2 = (end[0] - last * vx, end[1] - last * vy)
        segment = Cubic(self.point, c1, c2, end)
        if name:
            self.mark(name, tuple(float(v) for v in segment.point(0.5)))
        self.segments.append(segment)
        self.point = end
        self.heading += turn

    def close(self, name: str) -> Outline:
        self.mark(name, self.start)
        if math.dist(self.point, self.start) > 1e-6:
            self.segments.append(Line(self.point, self.start))
        else:
            last = self.segments[-1]
            self.segments[-1] = Line(last.p0, self.start)
        return Outline(tuple(self.segments))


def _build(bottom_run: float, left_run: float) -> _Turtle:
    t = _Turtle(_START, -7.0)
    # Top edge, heading roughly east.
    t.line(90)                                   # straight run at an odd angle (-7 degrees)
    t.corner(20, "20° corner (straight sides)")
    t.line(70)
    t.corner(-35, "35° corner")
    t.line(70)
    t.corner(55, "55° corner")
    t.line(60)
    t.corner(-33, "33° corner onto the axis")
    t.line(60)                                   # axis-aligned straight run
    t.arc(40, 40, "wave (inflections, no corners)")
    t.arc(40, -80)
    t.arc(40, 40)
    t.line(70)
    t.corner(5, "5° bend: below noise, stays smooth")
    t.line(70)
    t.corner(-15, "15° corner")
    t.line(70)
    t.corner(10, "10° bend")
    t.line(60)
    t.arc(1.5, 90, "tight convex 90° round: sharpens")
    # Right edge, heading south.
    t.line(60)
    t.corner(-75, "spike base (75°)")
    t.line(50)
    t.corner(150, "150° spike tip")
    t.line(50)
    t.corner(-75)
    t.line(60)
    t.corner(-84, "needle spike: 168° tip")
    t.line(56)
    t.corner(168)
    t.line(56)
    t.corner(-84)
    t.line(60)
    t.corner(-90, "thin tendril, 2 px wide")
    t.line(64)
    t.corner(90)
    t.line(2)
    t.corner(90)
    t.line(64)
    t.corner(-90)
    t.line(60)
    t.arc(80, 45, "straight into arc: tangent, no corner")
    t.arc(80, -45)
    t.line(60)
    t.corner(40, "line meets arc at a 40° corner")
    t.arc(60, -40)
    t.line(60)
    t.cubic(150, 120, 90, 130, 25, "varying-curvature bend (cubic)")
    # Bottom edge, heading west.
    t.line(60)
    t.arc(30, 90, "big convex 90° round: stays round")
    t.line(50)
    t.arc(1.5, -90, "tight concave 90° round: sharpens")
    t.line(60)
    t.arc(48, -60, "curve-to-curve tip (120°)")
    t.corner(120)
    t.arc(48, -60)
    t.line(60)
    t.corner(90, "90° corner")
    t.line(50)
    t.arc(30, -90, "big concave 90° round: stays round")
    t.line(60)
    t.corner(90, "notch with a short 12 px edge")
    t.line(24)
    t.corner(-90)
    t.line(12)
    t.corner(-90)
    t.line(24)
    t.corner(90)
    t.line(60)
    t.corner(90, "narrow slot, 4 px wide")
    t.line(80)
    t.corner(-90)
    t.line(4)
    t.corner(-90)
    t.line(80)
    t.corner(90)
    t.line(bottom_run)
    t.corner(45, "45° corner onto a 45° run")
    t.line(80)                                   # straight run at 45 degrees
    t.corner(45)
    # Left edge, heading north.
    t.line(60)
    t.arc(90, 30, "shallow 30° round, too big: stays round")
    t.line(60)
    t.arc(4, 30, "shallow 30° round, tight: sharpens")
    t.line(60)
    t.arc(300, -15, "gentle large-radius curve (r 300)")
    t.line(50)
    turn = (270.0 - t.heading + 180.0) % 360.0 - 180.0
    t.corner(turn, f"{abs(turn):.0f}° corner")
    t.line(left_run)
    return t


def _main_outline() -> tuple[Outline, list]:
    """The main outline. Two runs are solved so that the path closes cleanly: the
    bottom edge's last run (heading west) puts the end directly below the start,
    and the final run (heading north) reaches it, meeting the -7 degree first run
    at an 83 degree closing corner."""
    probe = _build(1.0, 1.0).point
    t = _build(1.0 + probe[0] - _START[0], 1.0 + probe[1] - _START[1])
    return t.close("83° closing corner"), t.features


def _rotated_rect(centre, width: float, height: float, degrees: float) -> Outline:
    c, s = math.cos(degrees * _DEG), math.sin(degrees * _DEG)
    corners = [(-width / 2, -height / 2), (width / 2, -height / 2),
               (width / 2, height / 2), (-width / 2, height / 2)]
    return polygon([(centre[0] + c * x - s * y, centre[1] + s * x + c * y) for x, y in corners])


def _arc_ring(centre, radius: float, thickness: float, start_deg: float, sweep_deg: float) -> Outline:
    """A thin circular arc feature: an annular sector of mean ``radius`` and radial
    ``thickness``, spanning ``sweep_deg`` from ``start_deg``, as two arcs joined by radial
    caps. A curved counterpart to ``_rotated_rect`` for the thin-feature threading tests."""
    r_in, r_out = radius - thickness / 2, radius + thickness / 2
    t0, sweep = start_deg * _DEG, sweep_deg * _DEG
    t1 = t0 + sweep

    def at(r, a):
        return (centre[0] + r * math.cos(a), centre[1] + r * math.sin(a))

    return Outline((Arc(centre, r_out, t0, sweep), Line(at(r_out, t1), at(r_in, t1)),
                    Arc(centre, r_in, t1, -sweep), Line(at(r_in, t0), at(r_out, t0))))


def _hexagon(centre, radius: float) -> Outline:
    return polygon([(centre[0] + radius * math.cos(k * math.pi / 3.0 + 0.2),
                     centre[1] + radius * math.sin(k * math.pi / 3.0 + 0.2)) for k in range(6)])


def _blob(centre, radii, rotation: float) -> Outline:
    """A smooth closed blob of four cubics through the points at ``radii`` (right,
    down, left, up before rotating by ``rotation`` degrees), each crossed square to
    its radius, with handles of 0.55 times the neighbouring radius."""
    c, s = math.cos(rotation * _DEG), math.sin(rotation * _DEG)

    def at(x, y):
        return (centre[0] + c * x - s * y, centre[1] + s * x + c * y)

    right, down, left, up = radii
    points = [(right, 0.0), (0.0, down), (-left, 0.0), (0.0, -up)]
    # handle directions: clockwise in y-down coordinates
    tangents = [(0.0, 1.0), (-1.0, 0.0), (0.0, -1.0), (1.0, 0.0)]
    segments = []
    for i in range(4):
        (x0, y0), (x1, y1) = points[i], points[(i + 1) % 4]
        (ux, uy), (vx, vy) = tangents[i], tangents[(i + 1) % 4]
        h0 = 0.55 * radii[(i + 1) % 4]
        h1 = 0.55 * radii[i]
        segments.append(Cubic(at(x0, y0), at(x0 + ux * h0, y0 + uy * h0),
                              at(x1 - vx * h1, y1 - vy * h1), at(x1, y1)))
    return Outline(tuple(segments))


def _regions() -> tuple[list[Region], list]:
    outline, features = _main_outline()
    hole = circle((360.0, 380.0), 48.0)
    regions = [
        Region(NAVY, outline, (hole,)),
        Region(GOLD, circle((360.5, 380.5), 3.0)),
        Region(SKY, ellipse((600.0, 330.0), 64.0, 8.0, 17.0 * _DEG)),
        Region(GOLD, circle((620.0, 520.0), 40.0), (circle((620.0, 520.0), 38.5),)),
        # Wedges across the top edge: three colours meet.
        Region(RED, polygon([(248.0, 125.0), (278.0, 125.0), (273.0, 215.0)])),
        Region(SKY, polygon([(278.0, 125.0), (308.0, 125.0), (273.0, 215.0)])),
        # A bar crossing the bottom edge square to it: two T-junctions.
        Region(RED, rectangle(180.0, 690.0, 204.0, 750.0)),
        # Diagonal single-pixel corner contact, outside the outline.
        Region(RED, rectangle(700.0, 900.0, 740.0, 940.0)),
        Region(SKY, rectangle(740.0, 940.0, 780.0, 980.0)),
        # Concentric bands of three colours.
        Region(RED, circle((380.0, 560.0), 44.0)),
        Region(GOLD, circle((380.0, 560.0), 30.0)),
        Region(SKY, circle((380.0, 560.0), 16.0)),
        # A thin gold channel between red and navy.
        Region(GOLD, rectangle(480.0, 420.0, 540.0, 480.0)),
        Region(RED, rectangle(481.5, 421.5, 538.5, 478.5)),
        # Tiny polygons beside a tiny disc.
        Region(GOLD, rectangle(236.0, 300.0, 244.0, 308.0)),
        Region(GOLD, _hexagon((270.0, 304.0), 4.5)),
        Region(GOLD, circle((300.0, 304.0), 4.0)),
        # A thin diagonal bar, 1 px wide at 30 degrees.
        Region(SKY, _rotated_rect((260.0, 460.0), 120.0, 1.0, 30.0)),
        # A thin 270-degree arc, 1 px wide: the curved counterpart of the diagonal bar.
        Region(SKY, _arc_ring((860.0, 300.0), 40.0, 1.0, 45.0, 270.0)),
        # Near-axis bars at 1 and 3 degrees.
        Region(SKY, _rotated_rect((560.0, 640.0), 200.0, 16.0, 1.0)),
        Region(SKY, _rotated_rect((560.0, 690.0), 200.0, 16.0, 3.0)),
        # Squares aligned to the pixel grid and offset by half a pixel.
        Region(GOLD, rectangle(760.0, 390.0, 800.0, 430.0)),
        Region(GOLD, rectangle(760.5, 450.5, 800.5, 490.5)),
        # Shapes cut by the image edge.
        Region(SKY, rectangle(0.0, 470.0, 40.0, 530.0)),
        Region(GREEN, circle((1024.0, 700.0), 50.0)),
        # A thin dark outline round a disc, and two near-identical shades.
        Region(BLACK, circle((140.0, 900.0), 42.0)),
        Region(SKY, circle((140.0, 900.0), 40.5)),
        Region(GREEN, rectangle(300.0, 880.0, 360.0, 940.0)),
        Region(GREEN_NEAR, rectangle(360.0, 880.0, 420.0, 940.0)),
        # Curved outlines crossing: two discs of different colours, then of one colour.
        Region(GOLD, circle((60.0, 60.0), 30.0)),
        Region(RED, circle((100.0, 64.0), 28.0)),
        Region(SKY, circle((58.0, 146.0), 28.0)),
        Region(SKY, circle((104.0, 140.0), 30.0)),
        # A disc over an ellipse.
        Region(SKY, ellipse((935.0, 60.0), 50.0, 16.0, 25.0 * _DEG)),
        Region(GOLD, circle((972.0, 80.0), 18.0)),
        # A disc over a cubic-edged blob, and two cubic-edged blobs crossing (on navy).
        Region(GOLD, _blob((752.0, 604.0), (34.0, 26.0, 38.0, 30.0), 10.0)),
        Region(RED, circle((792.0, 596.0), 22.0)),
        Region(SKY, _blob((742.0, 762.0), (30.0, 36.0, 32.0, 26.0), -15.0)),
        Region(GOLD, _blob((786.0, 752.0), (32.0, 28.0, 30.0, 34.0), 30.0)),
        # Circles touching at one point: externally, then internally (on navy).
        Region(GOLD, circle((160.0, 540.0), 25.0)),
        Region(RED, circle((210.0, 540.0), 25.0)),
        Region(SKY, circle((195.0, 620.0), 35.0)),
        Region(GOLD, circle((185.0, 620.0), 25.0)),
        # Two same-colour ellipses crossing: one merged blob, each a partial ellipse arc. Offset
        # along their shared major axis, so each shows a long visible arc and a modest overlap.
        Region(SKY, ellipse((140.0, 795.0), 44.0, 20.0, 20.0 * _DEG)),
        Region(SKY, ellipse((192.0, 814.0), 44.0, 20.0, 20.0 * _DEG)),
    ]
    features = features + [
        ("hole: large circle (r 48)", (360.0, 332.0)),
        ("tiny disc r 3, half-pixel offset", (360.5, 380.5)),
        ("aspect-8 ellipse (sharp smooth tips)", (600.0 + 64.0 * math.cos(17 * _DEG),
                                                  330.0 + 64.0 * math.sin(17 * _DEG))),
        ("thin ring, 1.5 px wide", (620.0, 480.0)),
        ("three colours meet (wedges across the edge)", (273.0, 164.0)),
        ("T-junctions: bar crossing the edge square", (180.0, 716.0)),
        ("diagonal single-pixel corner contact", (740.0, 940.0)),
        ("concentric bands of three colours", (380.0, 516.0)),
        ("thin 1.5 px channel between two colours", (480.0, 420.0)),
        ("tiny square, hexagon and disc (8-9 px)", (270.0, 294.0)),
        ("thin diagonal bar, 1 px at 30°", (260.0, 460.0)),
        ("thin 270° arc, 1 px wide", (860.0, 300.0)),
        ("near-axis bars at 1° and 3°", (460.0, 640.0)),
        ("pixel-aligned and half-pixel squares", (760.0, 440.0)),
        ("shapes cut by the image edge", (20.0, 470.0)),
        ("second edge cut: disc against the right border", (1000.0, 656.0)),
        ("thin dark outline round a disc", (140.0, 858.0)),
        ("near-identical shades side by side", (360.0, 880.0)),
        ("red disc over a gold disc: curves cross", (80.0, 60.0)),
        ("two sky discs merged: curves cross in one colour", (81.0, 143.0)),
        ("gold disc over a sky ellipse: circle crosses ellipse", (960.0, 70.0)),
        ("red disc over a cubic blob: circle crosses cubics", (775.0, 600.0)),
        ("two cubic blobs crossing", (764.0, 757.0)),
        ("two sky ellipses merged: ellipse arcs cross in one colour", (166.0, 805.0)),
        ("two discs touching at one point (outside)", (185.0, 540.0)),
        ("disc touching the inside of a disc", (160.0, 620.0)),
    ]
    return regions, features


def _master_scene(size: int = MASTER_SIZE) -> tuple[Scene, list]:
    regions, features = _regions()
    factor = size / MASTER_SIZE
    if size != MASTER_SIZE:
        regions = [region.scaled(factor) for region in regions]
    return Scene(size, size, PALETTE, PAPER, regions, "master", {"scale": factor}), features


FEATURES = _master_scene()[1]


def master_cases() -> list[Case]:
    """The master scene at every ``MASTER_SIZES`` size."""
    cases = []
    for size in MASTER_SIZES:
        scene, _ = _master_scene(size)
        # Narrowest feature: the 1 px diagonal bar, scaled.
        cases.append(Case(f"master-{size}", scene, 1.0 * size / MASTER_SIZE, ("master",)))
    return cases
