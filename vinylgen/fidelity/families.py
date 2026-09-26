"""Analytic shape families and multi-colour partitions for the synthetic corpus.

Every generator builds one ``Case`` (``vinylgen.fidelity.cases``) from explicit
parameters. Two size sweeps:

- ``scaled``: the same normalised scene (geometry as fractions of the canvas
  side) on a square canvas whose side runs through ``SCALED_SIZES`` (64 to
  2048 px). A pixel-phase parameter (a centre offset in px) stays in pixels.
- ``fixed-px``: fixed pixel feature sizes (disc radius, ring or stroke width,
  corner radius) on the ``FIXED_PX_CANVAS`` square canvas.

Entry points:

- ``full_grid()``: every family x parameter x size combination (for the offline
  sweep and tuner; not committed);
- ``fixed_set()``: the committed regression subset, each family at three sizes
  (the scaled sweep at 128, 512 and 1536 px, so every family has a canvas below
  500 px, where the default trace lengths resolve below a pixel) with a
  representative parameter spread, plus fixed-px cases for the families whose
  parameter is a pixel size;
- ``palette_stage_set()``: RGB scenes for the palette-stage tier (near-identical
  shades, small high-contrast accents, dark thin outlines, seeded noise), rendered
  with ``render_case_rgb``.

``min_feature_px`` is the narrowest feature a case draws, computed analytically
per family (see each generator): a disc's diameter, an ellipse's minor axis, a
rectangle's shorter side, a polygon's minimum width, a star tip's width at its
base, a ring's or stroke's width and the gap between nested rings, a wedge's
chord at the rim, a target band's width. For shapes that taper to a point (star
tips, lens and crescent horns, pie wedges at the centre) the width at the base
of the taper is used, as for a star tip.

Label-geometry families use ``COLOURS``, whose members are pairwise at least 34
CIE76 units apart, above the palette stage's 28-unit similar-colour merge.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.fidelity.cases import Case
from vinylgen.fidelity.raster import render_rgb
from vinylgen.fidelity.scene import (Arc, EllipticalArc, Line, Outline, Region, Scene,
                                     circle, ellipse, polygon, rectangle, segment_end,
                                     segment_start)
from vinylgen.trace_lengths import resolve_trace_lengths

SCALED_SIZES = (64, 128, 256, 512, 1024, 1536, 2048)
FIXED_SET_SIZES = (128, 512, 1536)
FIXED_PX_CANVAS = 576
PALETTE_STAGE_CANVAS = 512

COLOURS = {
    "paper": (238, 232, 218),
    "navy": (32, 44, 98),
    "red": (200, 40, 44),
    "gold": (240, 190, 40),
    "green": (34, 139, 76),
    "sky": (96, 170, 226),
    "purple": (128, 64, 160),
    "orange": (236, 116, 36),
    "brown": (104, 64, 36),
    "black": (22, 22, 26),
}
# Wedge and band colours in an order whose neighbours (cyclically) are all at
# least 38 CIE76 units apart.
SEQUENCE = ("navy", "gold", "red", "sky", "purple", "green", "orange", "brown")
# 1 / (sqrt(2) - 1): a 90 degree round of radius r sharpens to a corner that lies
# r (sqrt(2) - 1) from the arc's midpoint, so it sharpens within the corner
# sharpening allowance A when r <= A / (sqrt(2) - 1), about 2.41 A (GLOSSARY
# "corner sharpening allowance").
SHARPENING_RADIUS_FACTOR = 1.0 / (math.sqrt(2.0) - 1.0)

_DEG = math.pi / 180.0
_TAU = 2.0 * math.pi


# --- helpers --------------------------------------------------------------------------


def _tok(value: float) -> str:
    """A number as an id token: shortest general form (``0.25``, ``2``, ``1.5``)."""
    text = f"{float(value):g}"
    return text.replace("-", "m")


def _palette(*names: str) -> list[dict]:
    return [{"name": name, "rgb": list(COLOURS[name])} for name in names]


def _map(point, centre, theta: float):
    c, s = math.cos(theta), math.sin(theta)
    x, y = point
    return (centre[0] + c * x - s * y, centre[1] + s * x + c * y)


def _place(outline: Outline, centre, theta_deg: float = 0.0) -> Outline:
    """``outline`` (built around the origin) rotated by ``theta_deg`` and moved to
    ``centre``; arcs keep their radii and turn their angles with the frame."""
    theta = theta_deg * _DEG
    segments = []
    for segment in outline.segments:
        if isinstance(segment, Line):
            segments.append(Line(_map(segment.p0, centre, theta), _map(segment.p1, centre, theta)))
        elif isinstance(segment, Arc):
            segments.append(Arc(_map(segment.centre, centre, theta), segment.radius,
                                segment.start + theta, segment.sweep))
        elif isinstance(segment, EllipticalArc):
            segments.append(EllipticalArc(_map(segment.centre, centre, theta), segment.rx,
                                          segment.ry, segment.rotation + theta, segment.start,
                                          segment.sweep))
        else:
            raise TypeError(f"cannot place a {segment.kind} segment")
    return Outline(tuple(segments))


def _scene(size: int, palette: list[dict], regions: list[Region], family: str,
           params: dict) -> Scene:
    return Scene(size, size, palette, 0, regions, family, params)


def _case(case_id: str, scene: Scene, min_feature: float, *tags: str) -> Case:
    return Case(case_id, scene, float(min_feature), tuple(tags))


def _sweep_id(size: int, sweep: str) -> str:
    return str(size) if sweep == "scaled" else f"px{size}"


def sharpening_allowance_px(size: int) -> float:
    """The default corner sharpening allowance in px on a canvas whose longer
    side is ``size``: 1 per mille of the longer side, floored at 1 px."""
    return float(resolve_trace_lengths(TraceSettings(), size).corner_sharpening_allowance)


def sharpening_radius_px(size: int) -> float:
    """The largest 90 degree round radius that sharpens to a corner by default."""
    return sharpening_allowance_px(size) * SHARPENING_RADIUS_FACTOR


def rounded_rectangle(hw: float, hh: float, r: float) -> Outline:
    """A rectangle of half sizes ``hw`` x ``hh`` round the origin with quarter-arc
    corners of radius ``r``, every join tangent."""
    q = math.pi / 2.0
    return Outline((
        Line((-hw + r, -hh), (hw - r, -hh)), Arc((hw - r, -hh + r), r, -q, q),
        Line((hw, -hh + r), (hw, hh - r)), Arc((hw - r, hh - r), r, 0.0, q),
        Line((hw - r, hh), (-hw + r, hh)), Arc((-hw + r, hh - r), r, q, q),
        Line((-hw, hh - r), (-hw, -hh + r)), Arc((-hw + r, -hh + r), r, math.pi, q)))


def _polygon_width(n: int, radius: float) -> float:
    """The minimum width of a regular n-gon of circumradius ``radius``."""
    if n % 2 == 0:
        return 2.0 * radius * math.cos(math.pi / n)
    return radius * (1.0 + math.cos(math.pi / n))


def _annular_sector(radius: float, width: float, start: float, sweep: float) -> Outline:
    """A circular band of ``width`` inside ``radius`` over ``sweep`` radians from
    ``start``, closed by radial ends, round the origin."""
    outer = Arc((0.0, 0.0), radius, start, sweep)
    inner = Arc((0.0, 0.0), radius - width, start + sweep, -sweep)
    return Outline((outer, Line(segment_end(outer), segment_start(inner)), inner,
                    Line(segment_end(inner), segment_start(outer))))


def _ring(centre, radius: float, width: float, colour: int) -> Region:
    return Region(colour, circle(centre, radius), (circle(centre, radius - width),))


# --- single-shape families ------------------------------------------------------------


def disc(size: int, sweep: str, offset: float, radius_px: float | None = None) -> Case:
    """A disc; scaled radius 0.3 of the side, fixed-px radius ``radius_px``; the
    centre is offset by ``offset`` px on both axes. Feature: the diameter."""
    radius = 0.3 * size if sweep == "scaled" else float(radius_px)
    centre = (size / 2 + offset, size / 2 + offset)
    params = {"sweep": sweep, "size": size, "offset_px": offset, "radius_px": radius}
    scene = _scene(size, _palette("paper", "navy"), [Region(1, circle(centre, radius))],
                   "disc", params)
    radius_tok = "" if sweep == "scaled" else f"-r{_tok(radius)}"
    return _case(f"disc{radius_tok}-off{_tok(offset)}-{_sweep_id(size, sweep)}", scene,
                 2 * radius, "disc", sweep, "shape")


def ellipse_case(size: int, aspect: float, rotation: float) -> Case:
    """An ellipse with semi-major axis 0.4 of the side and ``aspect`` major/minor,
    rotated ``rotation`` degrees. Feature: the minor axis."""
    rx = 0.4 * size
    ry = rx / aspect
    params = {"sweep": "scaled", "size": size, "aspect": aspect, "rotation_deg": rotation}
    scene = _scene(size, _palette("paper", "red"),
                   [Region(1, ellipse((size / 2, size / 2), rx, ry, rotation * _DEG))],
                   "ellipse", params)
    return _case(f"ellipse-a{_tok(aspect)}-rot{_tok(rotation)}-{size}", scene, 2 * ry,
                 "ellipse", "scaled", "shape")


def rectangle_case(size: int, kind: str, rotation: float, phase: str) -> Case:
    """A square (half sides 0.3 of the side) or a 2:1 rectangle (0.35 x 0.175),
    rotated ``rotation`` degrees about its centre. ``phase`` ``int``: whole-pixel
    half sides about the canvas centre, so unrotated edges lie on pixel edges;
    ``sub``: the centre moved by (0.3, 0.55) px. Feature: the shorter side."""
    if kind == "square":
        hw = hh = 0.3 * size
    else:
        hw, hh = 0.35 * size, 0.175 * size
    if phase == "int":
        hw, hh = float(round(hw)), float(round(hh))
        centre = (size / 2, size / 2)
    else:
        centre = (size / 2 + 0.3, size / 2 + 0.55)
    params = {"sweep": "scaled", "size": size, "kind": kind, "rotation_deg": rotation,
              "phase": phase, "half_width_px": hw, "half_height_px": hh}
    outline = _place(rectangle(-hw, -hh, hw, hh), centre, rotation)
    scene = _scene(size, _palette("paper", "green"), [Region(1, outline)], "rectangle", params)
    return _case(f"{kind}-rot{_tok(rotation)}-{phase}-{size}", scene, 2 * min(hw, hh),
                 "rectangle", "scaled", "shape")


def rounded_rectangle_case(size: int, sweep: str, rotation: float, multiple: float | None = None,
                           radius_px: float | None = None) -> Case:
    """A 0.6 x 0.4 rectangle with corner radius ``multiple`` times the default
    sharpening radius on this canvas (scaled) or ``radius_px`` (fixed-px), rotated
    ``rotation`` degrees. Feature: the shorter side."""
    threshold = sharpening_radius_px(size)
    radius = threshold * multiple if sweep == "scaled" else float(radius_px)
    hw, hh = 0.3 * size, 0.2 * size
    centre = (size / 2 + 0.25, size / 2 + 0.25)
    params = {"sweep": sweep, "size": size, "rotation_deg": rotation, "radius_px": radius,
              "sharpening_allowance_px": sharpening_allowance_px(size),
              "sharpening_radius_px": threshold}
    if multiple is not None:
        params["radius_multiple"] = multiple
    outline = _place(rounded_rectangle(hw, hh, radius), centre, rotation)
    scene = _scene(size, _palette("paper", "purple"), [Region(1, outline)],
                   "rounded-rectangle", params)
    radius_tok = f"x{_tok(multiple)}" if sweep == "scaled" else f"r{_tok(radius)}"
    return _case(f"rrect-{radius_tok}-rot{_tok(rotation)}-{_sweep_id(size, sweep)}", scene,
                 2 * hh, "rounded-rectangle", sweep, "shape")


def regular_polygon_case(size: int, sides: int) -> Case:
    """A regular polygon of circumradius 0.38 of the side, one vertex straight
    up. Feature: its minimum width."""
    radius = 0.38 * size
    centre = (size / 2, size / 2)
    vertices = [(radius * math.cos(-math.pi / 2 + _TAU * k / sides),
                 radius * math.sin(-math.pi / 2 + _TAU * k / sides)) for k in range(sides)]
    params = {"sweep": "scaled", "size": size, "sides": sides}
    scene = _scene(size, _palette("paper", "orange"), [Region(1, _place(polygon(vertices), centre))],
                   "regular-polygon", params)
    return _case(f"polygon-{sides}-{size}", scene, _polygon_width(sides, radius),
                 "regular-polygon", "scaled", "shape")


def star_case(size: int, points: int, ratio: float) -> Case:
    """A star of ``points`` tips, outer radius 0.4 of the side, inner radius
    ``ratio`` times that, one tip straight up. Feature: a tip's width at its
    base, the distance between its two inner vertices."""
    outer = 0.4 * size
    inner = ratio * outer
    vertices = []
    for k in range(2 * points):
        r = outer if k % 2 == 0 else inner
        a = -math.pi / 2 + math.pi * k / points
        vertices.append((r * math.cos(a), r * math.sin(a)))
    params = {"sweep": "scaled", "size": size, "points": points, "ratio": ratio}
    scene = _scene(size, _palette("paper", "navy"),
                   [Region(1, _place(polygon(vertices), (size / 2, size / 2)))], "star", params)
    return _case(f"star-{points}p-r{_tok(ratio)}-{size}", scene,
                 2 * inner * math.sin(math.pi / points), "star", "scaled", "shape")


def line_arc_case(size: int, shape: str, rotation: float) -> Case:
    """Where lines meet arcs, rotated ``rotation`` degrees:

    - ``d``: a semicircle closed by its chord, radius 0.35 of the side: two
      non-tangent 90 degree corners. Feature: its depth, the radius.
    - ``stadium``: two semicircles of radius 0.18 joined by tangent lines 0.4
      long: every join smooth, no corner. Feature: the diameter.
    - ``lens``: two arcs of radius 0.35 whose centres are 0.35 apart, meeting at
      two non-tangent cusps. Feature: the thickness, 2R - d.
    - ``crescent``: a disc of radius 0.4 minus a disc of 0.9 of its radius
      moved 0.3 of its radius aside: an outer and a concave inner arc meeting
      at two cusps. Feature: the thickness at its widest, R - (R2 - e).
    """
    if shape == "d":
        r = 0.35 * size
        x0 = -r / 2
        outline = Outline((Line((x0, r), (x0, -r)), Arc((x0, 0.0), r, -math.pi / 2, math.pi)))
        feature = r
    elif shape == "stadium":
        r, half = 0.18 * size, 0.2 * size
        outline = Outline((Line((-half, -r), (half, -r)), Arc((half, 0.0), r, -math.pi / 2, math.pi),
                           Line((half, r), (-half, r)), Arc((-half, 0.0), r, math.pi / 2, math.pi)))
        feature = 2 * r
    elif shape == "lens":
        radius = d = 0.35 * size
        alpha = math.acos(d / 2 / radius)
        outline = Outline((Arc((-d / 2, 0.0), radius, -alpha, 2 * alpha),
                           Arc((d / 2, 0.0), radius, math.pi - alpha, 2 * alpha)))
        feature = 2 * radius - d
    elif shape == "crescent":
        radius = 0.4 * size
        inner, e = 0.9 * radius, 0.3 * radius
        shift = 0.27 * radius
        x = (radius ** 2 - inner ** 2 + e ** 2) / (2 * e)
        y = math.sqrt(radius ** 2 - x ** 2)
        theta = math.atan2(y, x)
        phi = math.atan2(y, x - e)
        outline = Outline((Arc((shift, 0.0), radius, theta, _TAU - 2 * theta),
                           Arc((shift + e, 0.0), inner, -phi, 2 * phi - _TAU)))
        feature = radius - (inner - e)
    else:
        raise ValueError(f"unknown line-meets-arc shape {shape!r}")
    params = {"sweep": "scaled", "size": size, "shape": shape, "rotation_deg": rotation}
    scene = _scene(size, _palette("paper", "sky"),
                   [Region(1, _place(outline, (size / 2, size / 2), rotation))],
                   "line-meets-arc", params)
    return _case(f"{shape}-rot{_tok(rotation)}-{size}", scene, feature,
                 "line-meets-arc", "scaled", "shape")


def ring_case(size: int, sweep: str, nested: bool, width_px: float | None = None) -> Case:
    """A ring of outer radius 0.35 of the side; width 0.015 of the side (scaled)
    or ``width_px`` (fixed-px). ``nested``: three concentric rings of that width
    separated by gaps of the same width. Feature: the ring width (equal to the
    gap)."""
    width = 0.015 * size if sweep == "scaled" else float(width_px)
    radius = 0.35 * size
    centre = (size / 2 + 0.25, size / 2 + 0.25)
    count = 3 if nested else 1
    regions = [_ring(centre, radius - 2 * width * k, width, 1) for k in range(count)]
    params = {"sweep": sweep, "size": size, "nested": nested, "width_px": width,
              "gap_px": width if nested else None}
    scene = _scene(size, _palette("paper", "black"), regions, "ring", params)
    width_tok = "" if sweep == "scaled" else f"-w{_tok(width)}"
    kind = "nested" if nested else "single"
    return _case(f"ring-{kind}{width_tok}-{_sweep_id(size, sweep)}", scene, width,
                 "ring", sweep, "stroke")


# --- thin strokes ---------------------------------------------------------------------


def bar_case(size: int, sweep: str, angle: float, width_px: float | None = None) -> Case:
    """A straight bar 0.7 of the side long, 0.008 of the side wide (scaled) or
    ``width_px`` (fixed-px), at ``angle`` degrees, centred 0.25 px off the canvas
    centre. Feature: the width."""
    width = 0.008 * size if sweep == "scaled" else float(width_px)
    half = 0.35 * size
    centre = (size / 2 + 0.25, size / 2 + 0.25)
    outline = _place(rectangle(-half, -width / 2, half, width / 2), centre, angle)
    params = {"sweep": sweep, "size": size, "angle_deg": angle, "width_px": width}
    scene = _scene(size, _palette("paper", "black"), [Region(1, outline)], "thin-bar", params)
    width_tok = "" if sweep == "scaled" else f"-w{_tok(width)}"
    return _case(f"bar{width_tok}-ang{_tok(angle)}-{_sweep_id(size, sweep)}", scene, width,
                 "thin-bar", sweep, "stroke")


def thin_arc_case(size: int, sweep: str, width_px: float | None = None) -> Case:
    """A 180 degree circular band (an upper arch) of outer radius 0.35 of the
    side, 0.008 of the side wide (scaled) or ``width_px`` (fixed-px), with radial
    ends. Feature: the width."""
    width = 0.008 * size if sweep == "scaled" else float(width_px)
    outline = _place(_annular_sector(0.35 * size, width, 200 * _DEG, math.pi),
                     (size / 2 + 0.25, size / 2 + 0.25))
    params = {"sweep": sweep, "size": size, "width_px": width}
    scene = _scene(size, _palette("paper", "black"), [Region(1, outline)], "thin-arc", params)
    width_tok = "" if sweep == "scaled" else f"-w{_tok(width)}"
    return _case(f"arc{width_tok}-{_sweep_id(size, sweep)}", scene, width,
                 "thin-arc", sweep, "stroke")


def outline_case(size: int, sweep: str, kind: str, width_px: float | None = None) -> Case:
    """A gold fill (a disc of radius 0.3 or a square of half side 0.3 of the
    side) inside a dark outline 0.006 of the side (scaled) or ``width_px``
    (fixed-px) wide; the square's outline is a mitred frame. Feature: the
    outline width."""
    width = 0.006 * size if sweep == "scaled" else float(width_px)
    centre = (size / 2 + 0.25, size / 2 + 0.25)
    inner = 0.3 * size
    if kind == "disc":
        outer_outline, fill_outline = circle(centre, inner + width), circle(centre, inner)
    else:
        outer_outline = _place(rectangle(-inner - width, -inner - width, inner + width,
                                         inner + width), centre)
        fill_outline = _place(rectangle(-inner, -inner, inner, inner), centre)
    params = {"sweep": sweep, "size": size, "kind": kind, "width_px": width}
    scene = _scene(size, _palette("paper", "black", "gold"),
                   [Region(1, outer_outline), Region(2, fill_outline)], "outline", params)
    width_tok = "" if sweep == "scaled" else f"-w{_tok(width)}"
    return _case(f"outline-{kind}{width_tok}-{_sweep_id(size, sweep)}", scene, width,
                 "outline", sweep, "stroke")


# --- multi-colour partitions ----------------------------------------------------------


def _pie_regions(centre, radius: float, count: int, phase_deg: float) -> list[Region]:
    step = _TAU / count
    starts = [-math.pi / 2 + phase_deg * _DEG + step * k for k in range(count)]
    rim = [(centre[0] + radius * math.cos(a), centre[1] + radius * math.sin(a)) for a in starts]
    regions = []
    for k in range(count):
        arc = Arc(centre, radius, starts[k], step)
        regions.append(Region(k + 1, Outline((Line(centre, rim[k]), arc,
                                              Line(rim[(k + 1) % count], centre)))))
    return regions


def pie_case(size: int, wedges: int, phase: float) -> Case:
    """A disc of radius 0.4 of the side cut into ``wedges`` equal wedges of
    distinct colours, the first starting ``phase`` degrees past straight up. The
    centre is a junction of every wedge. Feature: a wedge's chord at the rim
    (its width at the base of its taper)."""
    radius = 0.4 * size
    params = {"sweep": "scaled", "size": size, "wedges": wedges, "phase_deg": phase}
    scene = _scene(size, _palette("paper", *SEQUENCE[:wedges]),
                   _pie_regions((size / 2, size / 2), radius, wedges, phase), "pie", params)
    return _case(f"pie-{wedges}-ph{_tok(phase)}-{size}", scene,
                 2 * radius * math.sin(math.pi / wedges), "pie", "scaled", "partition")


def t_junction_case(size: int, kind: str) -> Case:
    """T-junctions against a horizontal band (0.5 to 0.7 of the side, running off
    both canvas edges):

    - ``disc-over-band``: a disc of radius 0.15 centred on the band's top edge,
      on top: two arc-meets-line junctions. Feature: the band height (0.2).
    - ``bar-on-band``: a vertical bar 0.1 wide from 0.2 down into the band,
      beneath it: two right-angle junctions. Feature: the bar width.
    """
    band = Region(1, rectangle(-0.1 * size, 0.5 * size, 1.1 * size, 0.7 * size))
    if kind == "disc-over-band":
        regions = [band, Region(2, circle((0.5 * size, 0.5 * size), 0.15 * size))]
        feature = 0.2 * size
    elif kind == "bar-on-band":
        regions = [Region(2, rectangle(0.45 * size, 0.2 * size, 0.55 * size, 0.6 * size)), band]
        feature = 0.1 * size
    else:
        raise ValueError(f"unknown T-junction kind {kind!r}")
    params = {"sweep": "scaled", "size": size, "kind": kind}
    scene = _scene(size, _palette("paper", "navy", "gold"), regions, "t-junction", params)
    return _case(f"t-{kind}-{size}", scene, feature, "t-junction", "scaled", "partition")


def y_junction_case(size: int, rotation: float) -> Case:
    """Three colours meeting at 120 degrees at the canvas centre and running off
    every edge: two triangles whose far sides lie outside the canvas over the
    background. The first ray points straight up, turned by ``rotation`` degrees.
    Feature: half the side, the shortest distance from the junction to a canvas
    edge."""
    centre = (size / 2, size / 2)
    reach = 2.0 * size
    angles = [(-90 + rotation + 120 * k) * _DEG for k in range(3)]
    tips = [(centre[0] + reach * math.cos(a), centre[1] + reach * math.sin(a)) for a in angles]
    regions = [Region(1, polygon([centre, tips[0], tips[1]])),
               Region(2, polygon([centre, tips[1], tips[2]]))]
    params = {"sweep": "scaled", "size": size, "rotation_deg": rotation}
    scene = _scene(size, _palette("paper", "red", "sky"), regions, "y-junction", params)
    return _case(f"y-rot{_tok(rotation)}-{size}", scene, 0.5 * size, "y-junction", "scaled",
                 "partition")


def target_case(size: int, colours: int, offset: float) -> Case:
    """Concentric discs: ``colours`` colours counting the background, outer
    radius 0.42 of the side, equal band widths, centre moved ``offset`` px on
    both axes. Feature: the band width."""
    radius = 0.42 * size
    discs = colours - 1
    band = radius / discs
    centre = (size / 2 + offset, size / 2 + offset)
    regions = [Region(k + 1, circle(centre, radius - band * k)) for k in range(discs)]
    params = {"sweep": "scaled", "size": size, "colours": colours, "offset_px": offset}
    scene = _scene(size, _palette("paper", *SEQUENCE[:discs]), regions, "target", params)
    return _case(f"target-{colours}c-off{_tok(offset)}-{size}", scene, band, "target",
                 "scaled", "partition")


def corner_contact_case(size: int, rotation: float, phase: float) -> Case:
    """Two same-colour squares of side 0.25 touching only at one shared corner
    (the canvas centre moved ``phase`` px on both axes), turned ``rotation``
    degrees about it. Under four-way connectivity they are two regions; the
    shared corner is a junction. Feature: the square side."""
    side = 0.25 * size
    centre = (size / 2 + phase, size / 2 + phase)
    first = _place(polygon([(-side, -side), (0.0, -side), (0.0, 0.0), (-side, 0.0)]), centre,
                   rotation)
    second = _place(polygon([(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]), centre,
                    rotation)
    params = {"sweep": "scaled", "size": size, "rotation_deg": rotation, "phase_px": phase}
    scene = _scene(size, _palette("paper", "green"), [Region(1, first), Region(1, second)],
                   "corner-contact", params)
    return _case(f"corner-contact-rot{_tok(rotation)}-ph{_tok(phase)}-{size}", scene, side,
                 "corner-contact", "scaled", "partition")


# --- parameter spaces -----------------------------------------------------------------

DISC_OFFSETS = (0.0, 0.25, 0.5)
DISC_RADII_PX = (2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)
ELLIPSE_ASPECTS = (1.5, 3.0, 8.0)
ELLIPSE_ROTATIONS = (0.0, 17.0, 45.0)
RECTANGLE_KINDS = ("square", "rect")
RECTANGLE_ROTATIONS = (0.0, 3.0, 7.0, 15.0, 30.0, 45.0)
RECTANGLE_PHASES = ("int", "sub")
# Corner radius as a multiple of the default sharpening radius (straddling 1).
ROUNDED_MULTIPLES = (0.5, 0.8, 0.95, 1.05, 1.25, 2.0, 4.0)
ROUNDED_RADII_PX = (1.0, 1.5, 2.0, 2.25, 2.5, 2.75, 3.0, 4.0, 6.0, 10.0)
ROUNDED_ROTATIONS = (0.0, 20.0)
POLYGON_SIDES = (3, 5, 6, 8, 12)
STAR_POINTS = (3, 5, 6, 8, 12)
STAR_RATIOS = (0.2, 0.4, 0.6)
LINE_ARC_SHAPES = ("d", "stadium", "lens", "crescent")
LINE_ARC_ROTATIONS = (0.0, 20.0)
RING_WIDTHS_PX = (1.0, 2.0, 3.0, 5.0)
BAR_WIDTHS_PX = (0.5, 1.0, 1.5, 2.0, 3.0, 5.0)
BAR_ANGLES = (0.0, 10.0, 30.0, 45.0)
THIN_ARC_WIDTHS_PX = (0.5, 1.0, 2.0, 3.0)
OUTLINE_KINDS = ("disc", "square")
OUTLINE_WIDTHS_PX = (1.0, 2.0, 3.0)
PIE_WEDGES = (3, 4, 6, 8)
PIE_PHASES = (0.0, 7.0)
T_KINDS = ("disc-over-band", "bar-on-band")
Y_ROTATIONS = (0.0, 13.0)
TARGET_COLOURS = (3, 4, 5)
TARGET_OFFSETS = (0.0, 0.37)
CORNER_ROTATIONS = (0.0, 30.0)
CORNER_PHASES = (0.0, 0.5)


def full_grid() -> list[Case]:
    """Every family x parameter x size combination, both sweeps."""
    cases: list[Case] = []
    px = FIXED_PX_CANVAS
    for size in SCALED_SIZES:
        cases += [disc(size, "scaled", off) for off in DISC_OFFSETS]
        cases += [ellipse_case(size, a, r) for a in ELLIPSE_ASPECTS for r in ELLIPSE_ROTATIONS]
        cases += [rectangle_case(size, k, r, p) for k in RECTANGLE_KINDS
                  for r in RECTANGLE_ROTATIONS for p in RECTANGLE_PHASES]
        cases += [rounded_rectangle_case(size, "scaled", rot, multiple=m)
                  for m in ROUNDED_MULTIPLES for rot in ROUNDED_ROTATIONS]
        cases += [regular_polygon_case(size, n) for n in POLYGON_SIDES]
        cases += [star_case(size, n, r) for n in STAR_POINTS for r in STAR_RATIOS]
        cases += [line_arc_case(size, s, r) for s in LINE_ARC_SHAPES for r in LINE_ARC_ROTATIONS]
        cases += [ring_case(size, "scaled", nested) for nested in (False, True)]
        cases += [bar_case(size, "scaled", a) for a in BAR_ANGLES]
        cases.append(thin_arc_case(size, "scaled"))
        cases += [outline_case(size, "scaled", k) for k in OUTLINE_KINDS]
        cases += [pie_case(size, n, p) for n in PIE_WEDGES for p in PIE_PHASES]
        cases += [t_junction_case(size, k) for k in T_KINDS]
        cases += [y_junction_case(size, r) for r in Y_ROTATIONS]
        cases += [target_case(size, n, o) for n in TARGET_COLOURS for o in TARGET_OFFSETS]
        cases += [corner_contact_case(size, r, p) for r in CORNER_ROTATIONS for p in CORNER_PHASES]
    cases += [disc(px, "fixed-px", off, r) for r in DISC_RADII_PX for off in DISC_OFFSETS]
    cases += [rounded_rectangle_case(px, "fixed-px", rot, radius_px=r)
              for r in ROUNDED_RADII_PX for rot in ROUNDED_ROTATIONS]
    cases += [ring_case(px, "fixed-px", nested, w) for w in RING_WIDTHS_PX for nested in (False, True)]
    cases += [bar_case(px, "fixed-px", a, w) for w in BAR_WIDTHS_PX for a in BAR_ANGLES]
    cases += [thin_arc_case(px, "fixed-px", w) for w in THIN_ARC_WIDTHS_PX]
    cases += [outline_case(px, "fixed-px", k, w) for k in OUTLINE_KINDS for w in OUTLINE_WIDTHS_PX]
    return cases


def fixed_set() -> list[Case]:
    """The committed regression subset: each family on the scaled sweep at
    ``FIXED_SET_SIZES`` with its parameters spread across the three sizes, plus
    fixed-px cases for the pixel-size families (discs, rounded rectangles,
    rings, bars, thin arcs and outlines)."""
    small, medium, large = FIXED_SET_SIZES
    px = FIXED_PX_CANVAS
    return [
        disc(small, "scaled", 0.0), disc(medium, "scaled", 0.25), disc(large, "scaled", 0.5),
        disc(px, "fixed-px", 0.0, 2), disc(px, "fixed-px", 0.5, 8),
        disc(px, "fixed-px", 0.25, 64),
        ellipse_case(small, 1.5, 0.0), ellipse_case(small, 8.0, 45.0),
        ellipse_case(medium, 3.0, 17.0), ellipse_case(medium, 1.5, 45.0),
        ellipse_case(large, 8.0, 17.0), ellipse_case(large, 3.0, 0.0),
        rectangle_case(small, "square", 0.0, "int"), rectangle_case(small, "rect", 7.0, "sub"),
        rectangle_case(medium, "square", 3.0, "sub"), rectangle_case(medium, "rect", 30.0, "int"),
        rectangle_case(large, "square", 45.0, "int"), rectangle_case(large, "rect", 15.0, "sub"),
        rounded_rectangle_case(small, "scaled", 0.0, multiple=0.8),
        rounded_rectangle_case(small, "scaled", 20.0, multiple=1.25),
        rounded_rectangle_case(medium, "scaled", 20.0, multiple=0.8),
        rounded_rectangle_case(medium, "scaled", 0.0, multiple=1.25),
        rounded_rectangle_case(large, "scaled", 0.0, multiple=0.8),
        rounded_rectangle_case(large, "scaled", 20.0, multiple=1.25),
        rounded_rectangle_case(px, "fixed-px", 0.0, radius_px=2.0),
        rounded_rectangle_case(px, "fixed-px", 0.0, radius_px=3.0),
        regular_polygon_case(small, 3), regular_polygon_case(small, 8),
        regular_polygon_case(medium, 5), regular_polygon_case(medium, 12),
        regular_polygon_case(large, 6), regular_polygon_case(large, 3),
        star_case(small, 5, 0.4), star_case(small, 12, 0.2),
        star_case(medium, 3, 0.6), star_case(medium, 8, 0.4),
        star_case(large, 6, 0.2), star_case(large, 5, 0.6),
        *[line_arc_case(size, shape, 20.0 if size == medium else 0.0)
          for size in FIXED_SET_SIZES for shape in LINE_ARC_SHAPES],
        ring_case(small, "scaled", False), ring_case(medium, "scaled", True),
        ring_case(large, "scaled", False),
        ring_case(px, "fixed-px", False, 1.0), ring_case(px, "fixed-px", True, 2.0),
        ring_case(px, "fixed-px", False, 3.0), ring_case(px, "fixed-px", True, 5.0),
        bar_case(small, "scaled", 10.0), bar_case(medium, "scaled", 30.0),
        bar_case(large, "scaled", 45.0),
        bar_case(px, "fixed-px", 0.0, 0.5), bar_case(px, "fixed-px", 10.0, 1.0),
        bar_case(px, "fixed-px", 30.0, 2.0), bar_case(px, "fixed-px", 45.0, 5.0),
        thin_arc_case(small, "scaled"), thin_arc_case(medium, "scaled"),
        thin_arc_case(large, "scaled"),
        thin_arc_case(px, "fixed-px", 1.0), thin_arc_case(px, "fixed-px", 3.0),
        outline_case(small, "scaled", "disc"), outline_case(medium, "scaled", "square"),
        outline_case(large, "scaled", "disc"),
        outline_case(px, "fixed-px", "disc", 1.0), outline_case(px, "fixed-px", "square", 2.0),
        outline_case(px, "fixed-px", "disc", 3.0),
        pie_case(small, 3, 0.0), pie_case(medium, 6, 0.0), pie_case(medium, 4, 7.0),
        pie_case(large, 8, 7.0),
        t_junction_case(small, "disc-over-band"), t_junction_case(medium, "bar-on-band"),
        t_junction_case(large, "disc-over-band"), t_junction_case(large, "bar-on-band"),
        y_junction_case(small, 0.0), y_junction_case(medium, 13.0), y_junction_case(large, 0.0),
        target_case(small, 3, 0.0), target_case(medium, 4, 0.37), target_case(large, 5, 0.0),
        corner_contact_case(small, 0.0, 0.0), corner_contact_case(medium, 30.0, 0.5),
        corner_contact_case(large, 0.0, 0.5),
    ]


# --- palette-stage RGB variants -------------------------------------------------------

# Near-identical shades: CIE76 distance about 2 from their partner, far below the
# palette stage's 28-unit similar-colour merge.
NEAR_SHADES = {
    "navy-near": (35, 48, 102),
    "green-near": (37, 143, 80),
    "paper-near": (233, 227, 212),
}


def _rgb_palette(*names: str) -> list[dict]:
    rows = []
    for name in names:
        rgb = NEAR_SHADES.get(name) or COLOURS[name]
        rows.append({"name": name, "rgb": list(rgb)})
    return rows


def _palette_case(case_id: str, size: int, palette: list[dict], regions: list[Region],
                  min_feature: float, role: str, params: dict) -> Case:
    params = {"size": size, **params}
    scene = Scene(size, size, palette, 0, regions, "palette-stage", params)
    return _case(case_id, scene, min_feature, "palette-stage", role, "fixed-px")


def _star_outline(centre, points: int, outer: float, ratio: float) -> Outline:
    vertices = []
    for k in range(2 * points):
        r = outer if k % 2 == 0 else ratio * outer
        a = -math.pi / 2 + math.pi * k / points
        vertices.append((r * math.cos(a), r * math.sin(a)))
    return _place(polygon(vertices), centre)


def palette_stage_set() -> list[Case]:
    """RGB scenes for the palette-stage tier. ``scene.params["expect"]`` names the
    intended palette outcome: ``merge`` lists palette index pairs that should
    become one colour, ``survive`` palette indices that should stay their own
    colour. ``scene.params["noise"]`` (``seed``, ``amplitude``), when present,
    is applied by ``render_case_rgb``."""
    s = PALETTE_STAGE_CANVAS
    c = (s / 2 + 0.25, s / 2 + 0.25)
    cases = []

    radius = 0.4 * s
    cases.append(_palette_case(
        "palette-near-shades-pie-4-512", s, _rgb_palette("paper", "navy", "navy-near", "red", "gold"),
        _pie_regions(c, radius, 4, 0.0), 2 * radius * math.sin(math.pi / 4), "near-shades",
        {"expect": {"merge": [[1, 2]], "survive": [0, 3, 4]}}))
    cases.append(_palette_case(
        "palette-near-shades-discs-512", s, _rgb_palette("paper", "green", "green-near", "red"),
        [Region(1, circle((0.3 * s, 0.35 * s), 0.18 * s)),
         Region(2, circle((0.7 * s, 0.35 * s), 0.18 * s)),
         Region(3, circle((0.5 * s, 0.72 * s), 0.18 * s))], 0.36 * s, "near-shades",
        {"expect": {"merge": [[1, 2]], "survive": [0, 3]}}))
    cases.append(_palette_case(
        "palette-near-shades-background-512", s, _rgb_palette("paper", "paper-near", "navy"),
        [Region(1, _place(rectangle(-0.3 * s, -0.2 * s, 0.3 * s, 0.2 * s), c, 10.0)),
         Region(2, circle(c, 0.1 * s))], 0.2 * s, "near-shades",
        {"expect": {"merge": [[0, 1]], "survive": [2]}}))

    # Small high-contrast accents covering about 1.2 % and 1.5 % of the canvas,
    # just above the palette stage's 1 % whole-colour usage cutoff.
    accent = math.sqrt(0.012 * s * s / math.pi)
    cases.append(_palette_case(
        "palette-accent-red-on-star-512", s, _rgb_palette("paper", "navy", "red"),
        [Region(1, _star_outline((0.55 * s, 0.56 * s), 5, 0.4 * s, 0.45)),
         Region(2, circle((0.13 * s, 0.13 * s), accent))], 2 * accent, "accent",
        {"expect": {"survive": [0, 1, 2]}, "accent_share": 0.012}))
    accent = math.sqrt(0.015 * s * s / math.pi)
    cases.append(_palette_case(
        "palette-accent-gold-on-navy-512", s, _rgb_palette("navy", "sky", "gold"),
        [Region(1, _place(rectangle(-0.35 * s, -0.25 * s, 0.35 * s, 0.25 * s), c, 0.0)),
         Region(2, circle(c, accent))], 2 * accent, "accent",
        {"expect": {"survive": [0, 1, 2]}, "accent_share": 0.015}))

    # Dark thin outlines (the palette stage's thin-feature protection evidence).
    for kind, width in (("disc", 1.5), ("square", 1.0), ("disc", 2.5)):
        inner = 0.3 * s
        if kind == "disc":
            regions = [Region(1, circle(c, inner + width)), Region(2, circle(c, inner))]
        else:
            regions = [Region(1, _place(rectangle(-inner - width, -inner - width, inner + width,
                                                  inner + width), c)),
                       Region(2, _place(rectangle(-inner, -inner, inner, inner), c))]
        cases.append(_palette_case(
            f"palette-dark-outline-{kind}-w{_tok(width)}-512", s,
            _rgb_palette("paper", "black", "gold"), regions, width, "dark-outline",
            {"outline_width_px": width, "expect": {"survive": [0, 1, 2]}}))

    # Light seeded noise over clean scenes, at three canvas sizes.
    for size, amplitude, seed in ((128, 3, 11), (512, 4, 12), (1536, 6, 13)):
        centre = (size / 2 + 0.25, size / 2 + 0.25)
        outer, ratio = 0.4 * size, 0.4
        cases.append(_palette_case(
            f"palette-noise-a{amplitude}-star-5p-{size}", size,
            _rgb_palette("paper", "navy", "gold"),
            [Region(2, circle(centre, 0.45 * size)),
             Region(1, _star_outline(centre, 5, outer, ratio))],
            2 * ratio * outer * math.sin(math.pi / 5), "noise",
            {"noise": {"seed": seed, "amplitude": amplitude},
             "expect": {"survive": [0, 1, 2]}}))
    radius = 0.4 * s
    cases.append(_palette_case(
        "palette-noise-a5-pie-6-512", s, _rgb_palette("paper", *SEQUENCE[:6]),
        _pie_regions(c, radius, 6, 0.0), 2 * radius * math.sin(math.pi / 6), "noise",
        {"noise": {"seed": 14, "amplitude": 5}, "expect": {"survive": list(range(7))}}))
    return cases


def render_case_rgb(case: Case) -> np.ndarray:
    """The anti-aliased RGB render of ``case`` (``raster.render_rgb``) with its
    seeded noise, when ``scene.params["noise"]`` is ``{"seed", "amplitude"}``:
    independent integers uniform on [-amplitude, amplitude] per pixel channel from
    ``numpy.random.default_rng(seed)``, added and clipped to 0..255."""
    rgb = render_rgb(case.scene)
    noise = case.scene.params.get("noise")
    if not noise:
        return rgb
    amplitude = int(noise["amplitude"])
    rng = np.random.default_rng(int(noise["seed"]))
    offsets = rng.integers(-amplitude, amplitude + 1, size=rgb.shape, dtype=np.int16)
    return np.clip(rgb.astype(np.int16) + offsets, 0, 255).astype(np.uint8)


# --- committed files ------------------------------------------------------------------

FIXED_DIR = Path("tests/synthetic/fixed")
PALETTE_STAGE_DIR = Path("tests/synthetic/palette-stage")


def case_json_text(case: Case) -> str:
    """The committed JSON of a case: its id, feature width, tags and scene."""
    payload = {"case_id": case.case_id, "min_feature_px": case.min_feature_px,
               "tags": list(case.tags), "scene": case.scene.to_json()}
    return json.dumps(payload, indent=1) + "\n"


def case_files(cases: list[Case]) -> dict[str, str]:
    """File name to text for every committed file of ``cases``."""
    files: dict[str, str] = {}
    for case in cases:
        files[f"{case.case_id}.json"] = case_json_text(case)
        files[f"{case.case_id}.svg"] = case.scene.to_svg()
    return files


def write_cases(cases: list[Case], directory: Path) -> list[Path]:
    """Write ``cases`` to ``directory`` and delete any other case files there.
    Returns the paths written."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = case_files(cases)
    for stale in sorted(directory.glob("*.json")) + sorted(directory.glob("*.svg")):
        if stale.name not in files:
            stale.unlink()
    written = []
    for name, text in files.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def load_case(path: Path) -> Case:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Case(data["case_id"], Scene.from_json(data["scene"]), float(data["min_feature_px"]),
                tuple(data["tags"]))


def load_fixed_set(root: Path) -> list[Case]:
    """Every case committed under ``root`` (a directory of ``<case_id>.json``),
    in case-id order."""
    return [load_case(path) for path in sorted(Path(root).glob("*.json"))]
