"""Hand-authored icon glyphs as synthetic fidelity cases.

``icon_cases()`` imports the pinned icon subset committed under ``tests/icons/``
(fetched by ``scripts/fetch_icons.py``; each set's ``SOURCE.json`` names its
upstream repository, commit, licence and glyphs) and returns one ``Case`` per
glyph and style, usable in both tiers exactly like the synthetic families.

Importer
--------
``parse_svg`` reads an icon file: every filled ``<path>``, ``<circle>``,
``<ellipse>``, ``<rect>`` (square corners) and ``<polygon>`` element, with the
``transform`` attributes of the element and its ancestors and the root
``viewBox`` (mapped onto ``width`` x ``height`` when both are given, uniformly
scaled and centred as SVG's default ``xMidYMid meet`` does) composed into one
affine map. An element whose fill is ``none`` is skipped; a glyph with no filled
element is refused (stroke-only icons are not used).

Path data supports every command (``M L H V C S Q T A Z``, absolute and
relative, with implicit repeats). A quadratic is raised to the cubic with
control points ``p0 + 2/3 (q - p0)`` and ``p1 + 2/3 (q - p1)``, which is the same
curve exactly. An elliptical arc is converted to its centre parametrisation
(SVG 1.1 appendix F.6.5, out-of-range radii scaled up) and carried through the
affine map exactly as an ``EllipticalArc``: the map sends the ellipse's matrix
``R(phi) diag(rx, ry)`` to another 2x2 matrix whose singular value
decomposition gives the image ellipse's radii, rotation and parameter shift
(``_map_arc``).

Degenerate authored pieces are resolved at the final pixel scale:

- a zero-length line, arc or cubic is dropped, and so is a subpath enclosing no
  area (``M x y Z``, or one running out and back along itself, both Material
  Symbols idioms);
- an arc with a zero radius is the straight line SVG says it is;
- a cubic whose control points lie on its chord, between its endpoints, is that
  chord (the same point set), so it becomes a ``Line``;
- a cubic whose control point coincides with its endpoint (``Cubic`` requires a
  tangent at every point) has that control point moved ``min(1e-3 px, 1% of the
  distance to the next distinct control point)`` towards it. The moved curve
  leaves the authored one by at most ``4/9`` of that move (the Bernstein weight
  ``3 t (1 - t)^2`` peaks at 4/9), well under the truth's 0.001 px flattening,
  and its end tangent is the authored curve's geometric tangent.

Fill rule
---------
Each element's subpaths become ``Outline``s (which must be simple). Their
boundaries must not touch or cross one another (a glyph that does is refused),
so they nest into a tree; each ring's parent is the smallest ring containing it
(shapely). The winding number of the face just inside a ring is the sum of the
signs of the ring and its ancestors (``Outline.signed_area``), and the face is
filled when that is non-zero (``nonzero``, the default) or when the ring's depth
is odd (``evenodd``). A filled face under an unfilled one starts a ``Region``;
descending through filled faces (redundant same-direction rings under
``nonzero`` are no boundary), the first unfilled faces are its holes; filled
faces below those are islands and start further regions. The regions of all
elements are stacked in document order in one glyph colour.

Rendering
---------
Each committed glyph file is one case. Its seed is the first 8 bytes of the SHA-256
of ``"<seed>:<set>:<style>:<name>"`` fed to ``numpy.random.default_rng``, which
draws, in order: the square canvas size from ``SIZES`` (128, 256, 512), the
rotation uniform over [0, 360) degrees, a sub-pixel offset uniform over
[-0.5, 0.5) px on each axis and the glyph colour from ``families.SEQUENCE``
(on the ``paper`` background). The glyph is rotated about the origin, scaled so
the longer side of its rotated bounding box (from the geometry, not the
viewBox) is ``FILL`` (80%) of the canvas, and centred on the canvas centre plus
the offset.

Case facts
----------
``case_id`` is ``icon-<set>-<style>-<name>-<size>`` (names lowercased, ``_`` to
``-``); ``scene.family`` is ``icon-<set>``; ``tags`` are ``("icon", set,
style, "icon-<set>-<style>")`` plus ``identical-<other style>`` for each style
whose upstream file is byte-identical to this one (such a file is committed and
cased once, under the first style in the set's order). ``min_feature_px`` is
``fuzz.estimate_min_feature_px`` of the scene's truth: a morphological opening
of each colour's visible geometry (the glyph and the background, so a narrow gap
counts as much as a narrow stroke) that reports the narrowest neck, strip or arm
and ignores true corners' tips (its documentation states the method and its
error, about 0.06 px plus 0.2%).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from shapely.geometry import LinearRing, Point as ShapelyPoint, Polygon

from vinylgen.fidelity import families
from vinylgen.fidelity.cases import Case
from vinylgen.fidelity.scene import (CONTINUITY_TOLERANCE, FLATTEN_TOLERANCE, Cubic,
                                     EllipticalArc, Line, Outline, Region, Scene)

ROOT = Path(__file__).resolve().parents[2]
ICONS_DIR = ROOT / "tests" / "icons"
SIZES = (128, 256, 512)
FILL = 0.8
BACKGROUND = "paper"
DEFAULT_SEED = 0
# A control point this close to its endpoint (px) is the endpoint.
_DEGENERATE_PX = 1e-7
_NUDGE_PX = 1e-3
# A subpath enclosing no more than this (px^2) is empty: ``M x y Z`` or a path
# that runs out and back along itself (a Material Symbols idiom).
_EMPTY_AREA_PX = 1e-6
_NUDGE_FRACTION = 0.01
# Samples per curve for the rotated bounding box (its error is far below a pixel).
_BOX_SAMPLES = 64
_TAU = 2.0 * math.pi

Affine = np.ndarray  # 3x3 homogeneous matrix


# --- raw geometry (user units) --------------------------------------------------------


@dataclass(frozen=True)
class RawLine:
    p0: tuple[float, float]
    p1: tuple[float, float]


@dataclass(frozen=True)
class RawCubic:
    p0: tuple[float, float]
    c1: tuple[float, float]
    c2: tuple[float, float]
    p1: tuple[float, float]


@dataclass(frozen=True)
class RawArc:
    """``centre + K (cos a, sin a)`` for ``a`` from ``start`` through ``start +
    sweep``; ``matrix`` is ``K`` as ``((k00, k01), (k10, k11))``."""

    centre: tuple[float, float]
    matrix: tuple[tuple[float, float], tuple[float, float]]
    start: float
    sweep: float

    def point(self, t):
        a = self.start + np.asarray(t, dtype=np.float64) * self.sweep
        k = np.asarray(self.matrix)
        return np.asarray(self.centre) + np.stack([np.cos(a), np.sin(a)], axis=-1) @ k.T


@dataclass(frozen=True)
class RawElement:
    """One filled element: its subpaths (lists of raw segments) and fill rule."""

    subpaths: tuple[tuple, ...]
    fill_rule: str


def _pt(value) -> tuple[float, float]:
    return (float(value[0]), float(value[1]))


def raw_start(segment):
    if isinstance(segment, RawArc):
        return _pt(segment.point(0.0))
    return segment.p0


def raw_end(segment):
    if isinstance(segment, RawArc):
        return _pt(segment.point(1.0))
    return segment.p1


def raw_points(segment, samples: int = _BOX_SAMPLES) -> np.ndarray:
    """Points along a raw segment (endpoints included)."""
    if isinstance(segment, RawLine):
        return np.asarray([segment.p0, segment.p1])
    t = np.linspace(0.0, 1.0, samples + 1)
    if isinstance(segment, RawArc):
        return segment.point(t)
    t = t[:, None]
    u = 1.0 - t
    p0, c1, c2, p1 = (np.asarray(v) for v in (segment.p0, segment.c1, segment.c2, segment.p1))
    return u ** 3 * p0 + 3 * u * u * t * c1 + 3 * u * t * t * c2 + t ** 3 * p1


# --- affine maps ------------------------------------------------------------------------


def _apply(m: Affine, p) -> tuple[float, float]:
    x, y = p
    return (float(m[0, 0] * x + m[0, 1] * y + m[0, 2]), float(m[1, 0] * x + m[1, 1] * y + m[1, 2]))


def _map_arc(m: Affine, arc: RawArc) -> RawArc:
    k = m[:2, :2] @ np.asarray(arc.matrix)
    return RawArc(_apply(m, arc.centre), ((float(k[0, 0]), float(k[0, 1])),
                                          (float(k[1, 0]), float(k[1, 1]))),
                  arc.start, arc.sweep)


def map_segment(m: Affine, segment):
    """``segment`` under the affine map ``m`` (exact for every type)."""
    if isinstance(segment, RawLine):
        return RawLine(_apply(m, segment.p0), _apply(m, segment.p1))
    if isinstance(segment, RawCubic):
        return RawCubic(*(_apply(m, p) for p in (segment.p0, segment.c1, segment.c2,
                                                 segment.p1)))
    return _map_arc(m, segment)


def _matrix(a, b, c, d, e, f) -> Affine:
    return np.array([[a, c, e], [b, d, f], [0.0, 0.0, 1.0]], dtype=np.float64)


_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_TRANSFORM = re.compile(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)")


def parse_transform(text: str | None) -> Affine:
    """The affine map of an SVG ``transform`` attribute (identity when absent)."""
    m = np.eye(3)
    if not text:
        return m
    for name, args in _TRANSFORM.findall(text):
        v = [float(x) for x in re.findall(_NUMBER, args)]
        if name == "matrix":
            t = _matrix(*v)
        elif name == "translate":
            t = _matrix(1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0.0)
        elif name == "scale":
            t = _matrix(v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            a = math.radians(v[0])
            r = _matrix(math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
            if len(v) == 3:
                t = _matrix(1, 0, 0, 1, v[1], v[2]) @ r @ _matrix(1, 0, 0, 1, -v[1], -v[2])
            else:
                t = r
        elif name == "skewX":
            t = _matrix(1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        else:
            t = _matrix(1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        m = m @ t
    return m


# --- path data --------------------------------------------------------------------------


def _tokens(data: str):
    """Commands and numbers of path data; arc flags may be written without
    separators (``a1 1 0 01.5.5``), so flags are read by the parser."""
    pos, n = 0, len(data)
    number = re.compile(_NUMBER)
    out = []
    while pos < n:
        ch = data[pos]
        if ch.isspace() or ch == ",":
            pos += 1
        elif ch.isalpha() and ch not in "eE":
            out.append(ch)
            pos += 1
        else:
            match = number.match(data, pos)
            if not match:
                raise ValueError(f"bad path data at {pos}: {data[pos:pos + 20]!r}")
            out.append(match.group(0))
            pos = match.end()
    return out


def _arc_segment(p0, rx, ry, phi_deg, large, sweep_flag, p1):
    """The SVG arc from ``p0`` to ``p1`` (endpoint parametrisation) as a
    ``RawArc`` or a ``RawLine`` (zero radius), or None (coincident endpoints)."""
    if math.dist(p0, p1) == 0.0:
        return None
    rx, ry = abs(rx), abs(ry)
    if rx == 0.0 or ry == 0.0:
        return RawLine(p0, p1)
    phi = math.radians(phi_deg % 360.0)
    c, s = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2.0, (p0[1] - p1[1]) / 2.0
    x1, y1 = c * dx + s * dy, -s * dx + c * dy
    lam = (x1 * x1) / (rx * rx) + (y1 * y1) / (ry * ry)
    if lam > 1.0:
        root = math.sqrt(lam)
        rx, ry = rx * root, ry * root
    num = rx * rx * ry * ry - rx * rx * y1 * y1 - ry * ry * x1 * x1
    den = rx * rx * y1 * y1 + ry * ry * x1 * x1
    coef = math.sqrt(max(0.0, num / den))
    if large == sweep_flag:
        coef = -coef
    cx1, cy1 = coef * rx * y1 / ry, -coef * ry * x1 / rx
    cx = c * cx1 - s * cy1 + (p0[0] + p1[0]) / 2.0
    cy = s * cx1 + c * cy1 + (p0[1] + p1[1]) / 2.0

    def angle(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    ux, uy = (x1 - cx1) / rx, (y1 - cy1) / ry
    vx, vy = (-x1 - cx1) / rx, (-y1 - cy1) / ry
    start = angle(1.0, 0.0, ux, uy)
    delta = angle(ux, uy, vx, vy)
    if not sweep_flag and delta > 0:
        delta -= _TAU
    elif sweep_flag and delta < 0:
        delta += _TAU
    matrix = ((c * rx, -s * ry), (s * rx, c * ry))
    return RawArc((cx, cy), matrix, start, delta)


def parse_path(data: str) -> list[list]:
    """The subpaths of SVG path data as lists of raw segments in user units.
    Every subpath is closed (a closing line is added where the data leaves it
    open, as a fill does); coincident closing points are snapped exactly."""
    tokens = _tokens(data)
    subpaths: list[list] = []
    current: list = []
    pos = 0
    cmd = None
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    last_cubic_ctrl = None
    last_quad_ctrl = None

    def num():
        nonlocal pos
        value = float(tokens[pos])
        pos += 1
        return value

    def flag():
        nonlocal pos
        token = tokens[pos]
        if token[0] not in "01":
            raise ValueError(f"bad arc flag {token!r}")
        if len(token) > 1:
            tokens[pos] = token[1:]
        else:
            pos += 1
        return token[0] == "1"

    def close():
        nonlocal current, cur
        if current:
            end = raw_end(current[-1])
            scale = max(1.0, abs(start[0]), abs(start[1]))
            if math.dist(end, start) > 1e-9 * scale:
                current.append(RawLine(end, start))
            else:
                current[-1] = _with_end(current[-1], start)
            subpaths.append(current)
        current = []
        cur = start

    while pos < len(tokens):
        token = tokens[pos]
        if token[0].isalpha():
            cmd = token
            pos += 1
            if cmd in "Zz":
                close()
                last_cubic_ctrl = last_quad_ctrl = None
                continue
        elif cmd is None:
            raise ValueError("path data must start with a command")
        rel = cmd.islower()
        c = cmd.upper()
        ox, oy = cur if rel else (0.0, 0.0)
        cubic_ctrl = quad_ctrl = None
        if c == "M":
            if current:
                close()
            cur = start = (ox + num(), oy + num())
            cmd = "l" if rel else "L"
        elif c in "LHV":
            if c == "L":
                p = (ox + num(), oy + num())
            elif c == "H":
                p = ((cur[0] if rel else 0.0) + num(), cur[1])
            else:
                p = (cur[0], (cur[1] if rel else 0.0) + num())
            current.append(RawLine(cur, p))
            cur = p
        elif c in "CS":
            if c == "C":
                c1 = (ox + num(), oy + num())
            else:
                c1 = ((2 * cur[0] - last_cubic_ctrl[0], 2 * cur[1] - last_cubic_ctrl[1])
                      if last_cubic_ctrl else cur)
            c2 = (ox + num(), oy + num())
            p = (ox + num(), oy + num())
            current.append(RawCubic(cur, c1, c2, p))
            cubic_ctrl = c2
            cur = p
        elif c in "QT":
            if c == "Q":
                q = (ox + num(), oy + num())
            else:
                q = ((2 * cur[0] - last_quad_ctrl[0], 2 * cur[1] - last_quad_ctrl[1])
                     if last_quad_ctrl else cur)
            p = (ox + num(), oy + num())
            current.append(quadratic_to_cubic(cur, q, p))
            quad_ctrl = q
            cur = p
        elif c == "A":
            rx, ry, phi = num(), num(), num()
            large, sweep = flag(), flag()
            p = (ox + num(), oy + num())
            arc = _arc_segment(cur, rx, ry, phi, large, sweep, p)
            if arc is not None:
                current.append(arc)
            cur = p
        else:
            raise ValueError(f"unsupported path command {cmd!r}")
        last_cubic_ctrl, last_quad_ctrl = cubic_ctrl, quad_ctrl
    if current:
        close()
    return subpaths


def quadratic_to_cubic(p0, q, p1) -> RawCubic:
    """The quadratic Bézier ``p0, q, p1`` as the identical cubic."""
    c1 = (p0[0] + 2.0 / 3.0 * (q[0] - p0[0]), p0[1] + 2.0 / 3.0 * (q[1] - p0[1]))
    c2 = (p1[0] + 2.0 / 3.0 * (q[0] - p1[0]), p1[1] + 2.0 / 3.0 * (q[1] - p1[1]))
    return RawCubic(_pt(p0), c1, c2, _pt(p1))


def _with_end(segment, end):
    if isinstance(segment, RawLine):
        return RawLine(segment.p0, end)
    if isinstance(segment, RawCubic):
        return RawCubic(segment.p0, segment.c1, segment.c2, end)
    return segment  # an arc's end is within 1e-9 of the start; the outline accepts it


# --- SVG documents ----------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _style(element) -> dict:
    out = {}
    for item in (element.get("style") or "").split(";"):
        if ":" in item:
            key, value = item.split(":", 1)
            out[key.strip()] = value.strip()
    return out


def _attr(element, name, inherited):
    style = _style(element)
    if name in style:
        return style[name]
    if element.get(name) is not None:
        return element.get(name)
    return inherited.get(name)


def _shape_subpaths(element, tag):
    g = lambda name: float(element.get(name, 0) or 0)  # noqa: E731
    if tag == "path":
        return parse_path(element.get("d", ""))
    if tag in ("circle", "ellipse"):
        cx, cy = g("cx"), g("cy")
        rx = g("r") if tag == "circle" else g("rx")
        ry = g("r") if tag == "circle" else g("ry")
        if rx <= 0 or ry <= 0:
            return []
        arc = RawArc((cx, cy), ((rx, 0.0), (0.0, ry)), 0.0, _TAU)
        return [[arc]]
    if tag == "rect":
        x, y, w, h = g("x"), g("y"), g("width"), g("height")
        if element.get("rx") or element.get("ry"):
            raise ValueError("rounded <rect> elements are not supported")
        if w <= 0 or h <= 0:
            return []
        return parse_path(f"M{x} {y}H{x + w}V{y + h}H{x}Z")
    if tag == "polygon":
        values = re.findall(_NUMBER, element.get("points", ""))
        pairs = [f"{values[i]} {values[i + 1]}" for i in range(0, len(values) - 1, 2)]
        return parse_path("M" + " L".join(pairs) + "Z") if pairs else []
    return None


def _viewbox_matrix(root) -> Affine:
    box = root.get("viewBox")
    if not box:
        return np.eye(3)
    vx, vy, vw, vh = (float(v) for v in re.findall(_NUMBER, box))
    width, height = root.get("width"), root.get("height")
    if not width or not height:
        return _matrix(1, 0, 0, 1, -vx, -vy)
    width, height = float(re.match(_NUMBER, width).group(0)), float(
        re.match(_NUMBER, height).group(0))
    scale = min(width / vw, height / vh)
    tx = (width - vw * scale) / 2.0 - vx * scale
    ty = (height - vh * scale) / 2.0 - vy * scale
    return _matrix(scale, 0, 0, scale, tx, ty)


def _shown(name: str, value: str) -> bool:
    """Whether a presentation value leaves an element fully drawn: any display but
    ``none``, a visible or inherited visibility, and an opacity of exactly 1."""
    value = value.replace("!important", "").strip()
    if value == "inherit":
        return True
    if name == "display":
        return value != "none"
    if name == "visibility":
        return value == "visible"
    try:
        number = float(value[:-1]) / 100.0 if value.endswith("%") else float(value)
    except ValueError:
        return False
    return number == 1.0


# Graphics elements the importer does not read: a glyph using one is refused
# rather than imported with a piece missing.
_UNSUPPORTED_GRAPHICS = ("use", "polyline", "line", "text", "image", "symbol", "switch",
                         "foreignObject")


def parse_svg(text: str) -> list[RawElement]:
    """The filled elements of an SVG document with their geometry mapped into
    the document's viewport (``transform`` and ``viewBox`` applied). A glyph with
    an element or presentation attribute the importer cannot honour (a ``<use>``,
    a hidden or translucent element) raises ``ValueError``."""
    root = ET.fromstring(text)
    elements: list[RawElement] = []
    stroked = False

    def walk(node, matrix, inherited):
        nonlocal stroked
        tag = _local(node.tag)
        if tag in ("defs", "clipPath", "mask", "title", "desc", "metadata", "style", "pattern",
                   "marker", "linearGradient", "radialGradient", "filter"):
            return
        if tag == "svg" and node is not root:
            raise ValueError("unsupported nested <svg> element")
        matrix = matrix @ parse_transform(node.get("transform"))
        state = dict(inherited)
        for name in ("fill", "fill-rule", "stroke"):
            value = _attr(node, name, {})
            if value is not None:
                state[name] = value
        if tag in _UNSUPPORTED_GRAPHICS:
            raise ValueError(f"unsupported <{tag}> element")
        for name in ("clip-path", "mask", "filter"):
            value = _attr(node, name, {})
            if value is not None and value.strip() != "none":
                raise ValueError(f"unsupported {name}={value!r}")
        for name in ("display", "visibility", "opacity", "fill-opacity"):
            value = _attr(node, name, {})
            if value is not None and not _shown(name, value):
                raise ValueError(f"unsupported {name}={value!r}")
        subpaths = _shape_subpaths(node, tag)
        if subpaths is not None:
            if state.get("fill", "black") == "none":
                stroked = stroked or state.get("stroke", "none") != "none"
                return
            mapped = tuple(tuple(map_segment(matrix, s) for s in sub) for sub in subpaths)
            elements.append(RawElement(mapped, state.get("fill-rule", "nonzero")))
            return
        for child in node:
            walk(child, matrix, state)

    walk(root, _viewbox_matrix(root), {})
    if not elements:
        raise ValueError("stroke-only glyph" if stroked else "the glyph has no filled element")
    return elements


# --- scene segments ---------------------------------------------------------------------


def _collinear_between(p0, c1, c2, p1) -> bool:
    d = np.subtract(p1, p0)
    length2 = float(np.dot(d, d))
    if length2 <= 0.0:
        return False
    length = math.sqrt(length2)
    for c in (c1, c2):
        v = np.subtract(c, p0)
        if abs(float(d[0] * v[1] - d[1] * v[0])) / length > 1e-9 * max(1.0, length):
            return False
        t = float(np.dot(v, d)) / length2
        if not -1e-12 <= t <= 1.0 + 1e-12:
            return False
    return True


def _nudged(end, control, others):
    """``control`` moved off ``end`` towards the first of ``others`` distinct from
    ``end``."""
    if math.dist(end, control) > _DEGENERATE_PX:
        return control
    for target in others:
        distance = math.dist(end, target)
        if distance > _DEGENERATE_PX:
            step = min(_NUDGE_PX, _NUDGE_FRACTION * distance)
            return (end[0] + (target[0] - end[0]) * step / distance,
                    end[1] + (target[1] - end[1]) * step / distance)
    return control


def scene_segment(raw):
    """The scene segment of a raw segment in pixels, or None when it has no
    length (module docstring, degenerate pieces)."""
    if isinstance(raw, RawLine):
        return Line(raw.p0, raw.p1) if math.dist(raw.p0, raw.p1) > CONTINUITY_TOLERANCE else None
    if isinstance(raw, RawCubic):
        p0, c1, c2, p1 = raw.p0, raw.c1, raw.c2, raw.p1
        if math.dist(p0, p1) <= CONTINUITY_TOLERANCE and max(
                math.dist(p0, c1), math.dist(p0, c2)) <= CONTINUITY_TOLERANCE:
            return None
        if _collinear_between(p0, c1, c2, p1) or (
                math.dist(p0, c1) <= _DEGENERATE_PX and math.dist(c2, p1) <= _DEGENERATE_PX):
            return Line(p0, p1) if math.dist(p0, p1) > CONTINUITY_TOLERANCE else None
        c1 = _nudged(p0, c1, (c2, p1))
        c2 = _nudged(p1, c2, (c1, p0))
        return Cubic(p0, c1, c2, p1)
    k = np.asarray(raw.matrix)
    if math.dist(raw_start(raw), raw_end(raw)) <= CONTINUITY_TOLERANCE and abs(raw.sweep) < 1e-9:
        return None
    u, s, vt = np.linalg.svd(k)
    d = np.diag([1.0, float(np.sign(np.linalg.det(u)) or 1.0)])
    rotation_matrix = u @ d
    w = d @ vt
    psi = math.atan2(w[0, 1], w[0, 0])
    if np.linalg.det(w) > 0:
        start, sweep = raw.start - psi, raw.sweep
    else:
        start, sweep = psi - raw.start, -raw.sweep
    rotation = math.atan2(rotation_matrix[1, 0], rotation_matrix[0, 0])
    if s[1] <= CONTINUITY_TOLERANCE:
        return Line(raw_start(raw), raw_end(raw))
    return EllipticalArc(raw.centre, float(s[0]), float(s[1]), rotation, start, sweep)


def _ring_polygon(outline: Outline) -> Polygon:
    return Polygon(outline.flatten(FLATTEN_TOLERANCE))


def element_regions(outlines: list[Outline], fill_rule: str, colour: int) -> list[Region]:
    """The regions one element's outlines fill under ``fill_rule`` (module
    docstring, fill rule)."""
    if fill_rule not in ("nonzero", "evenodd"):
        raise ValueError(f"unknown fill rule {fill_rule!r}")
    polygons = [_ring_polygon(o) for o in outlines]
    rings = [LinearRing(p.exterior.coords) for p in polygons]
    count = len(outlines)
    for i in range(count):
        for j in range(i + 1, count):
            if rings[i].intersects(rings[j]):
                raise ValueError("subpaths touch or cross one another")
    parent = [-1] * count
    for i in range(count):
        probe = ShapelyPoint(rings[i].coords[0])
        best = None
        for j in range(count):
            if j != i and polygons[j].area > polygons[i].area and polygons[j].contains(probe):
                if best is None or polygons[j].area < polygons[best].area:
                    best = j
        parent[i] = -1 if best is None else best
    children: dict[int, list[int]] = {k: [] for k in range(-1, count)}
    for i, p in enumerate(parent):
        children[p].append(i)
    sign = [1 if o.signed_area() > 0 else -1 for o in outlines]
    winding = [0] * count
    depth = [0] * count

    def fill_state(i):
        if i < 0:
            return False
        return winding[i] != 0 if fill_rule == "nonzero" else depth[i] % 2 == 1

    order = list(children[-1])
    while order:
        i = order.pop(0)
        winding[i] = sign[i] + (winding[parent[i]] if parent[i] >= 0 else 0)
        depth[i] = 1 + (depth[parent[i]] if parent[i] >= 0 else 0)
        order.extend(children[i])

    regions: list[Region] = []

    def holes_of(i) -> list[int]:
        found = []
        for child in children[i]:
            if fill_state(child):
                found.extend(holes_of(child))
            else:
                found.append(child)
        return found

    def visit(i):
        # ``i`` is a face (or the root) whose fill state differs from its parent.
        if i >= 0 and fill_state(i):
            holes = holes_of(i)
            regions.append(Region(colour, outlines[i], tuple(outlines[h] for h in holes)))
            for h in holes:
                visit(h)
            return
        for child in children[i]:
            if fill_state(child):
                visit(child)
            else:
                visit_unfilled(child)

    def visit_unfilled(i):
        for child in children[i]:
            if fill_state(child):
                visit(child)
            else:
                visit_unfilled(child)

    visit(-1)
    return regions


def glyph_regions(elements: list[RawElement], placement: Affine, colour: int) -> list[Region]:
    """Every region of a parsed glyph under ``placement`` (user units to px)."""
    regions: list[Region] = []
    for element in elements:
        outlines = []
        for sub in element.subpaths:
            mapped = [map_segment(placement, raw) for raw in sub]
            if abs(_raw_area(mapped)) <= _EMPTY_AREA_PX:
                continue
            segments = [s for s in (scene_segment(raw) for raw in mapped) if s is not None]
            _close_gaps(segments)
            outlines.append(Outline(tuple(segments)))
        regions.extend(element_regions(outlines, element.fill_rule, colour))
    return regions


def _raw_area(segments: list) -> float:
    """Shoelace area of a raw subpath's sampled outline (px^2)."""
    points = np.concatenate([raw_points(s)[:-1] for s in segments])
    x, y = points[:, 0], points[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _close_gaps(segments: list) -> None:
    """Replace a line whose end was separated from the next start only by a
    dropped zero-length segment, so the outline stays continuous."""
    from vinylgen.fidelity.scene import segment_end, segment_start

    for index, segment in enumerate(segments):
        following = segments[(index + 1) % len(segments)]
        end, start = segment_end(segment), segment_start(following)
        if 0.0 < math.dist(end, start) <= 10 * CONTINUITY_TOLERANCE and isinstance(segment, Line):
            segments[index] = Line(segment.p0, start)


# --- cases ------------------------------------------------------------------------------


def slug(name: str) -> str:
    return name.lower().replace("_", "-")


def glyph_seed(seed: int, set_name: str, style: str, name: str) -> int:
    digest = hashlib.sha256(f"{seed}:{set_name}:{style}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def placement_matrix(elements: list[RawElement], size: int, rotation_deg: float,
                     offset) -> tuple[Affine, float]:
    """The map from user units to canvas px (rotate, fit ``FILL`` of the canvas,
    centre plus ``offset``) and its scale."""
    a = math.radians(rotation_deg)
    rot = _matrix(math.cos(a), math.sin(a), -math.sin(a), math.cos(a), 0, 0)
    points = np.concatenate([raw_points(map_segment(rot, s)) for e in elements
                             for sub in e.subpaths for s in sub])
    lo, hi = points.min(axis=0), points.max(axis=0)
    scale = FILL * size / float(max(hi - lo))
    mid = (lo + hi) / 2.0
    centre = (size / 2.0 + float(offset[0]), size / 2.0 + float(offset[1]))
    move = _matrix(scale, 0, 0, scale, centre[0] - scale * mid[0], centre[1] - scale * mid[1])
    return move @ rot, scale


def glyph_scene(svg_text: str, set_name: str, style: str, name: str, seed: int = DEFAULT_SEED,
                source: str = "") -> Scene:
    """The scene of one glyph (module docstring, rendering)."""
    rng = np.random.default_rng(glyph_seed(seed, set_name, style, name))
    size = SIZES[int(rng.integers(len(SIZES)))]
    rotation = float(rng.random()) * 360.0
    offset = (float(rng.random()) - 0.5, float(rng.random()) - 0.5)
    colour_name = families.SEQUENCE[int(rng.integers(len(families.SEQUENCE)))]
    elements = parse_svg(svg_text)
    placement, scale = placement_matrix(elements, size, rotation, offset)
    palette = [{"name": BACKGROUND, "rgb": list(families.COLOURS[BACKGROUND])},
               {"name": colour_name, "rgb": list(families.COLOURS[colour_name])}]
    regions = glyph_regions(elements, placement, 1)
    params = {"set": set_name, "style": style, "name": name, "source": source, "seed": seed,
              "size": size, "rotation_deg": rotation, "offset_px": list(offset),
              "scale_px_per_unit": scale,
              "fill_rules": sorted({e.fill_rule for e in elements})}
    return Scene(size, size, palette, 0, regions, f"icon-{set_name}", params)


def case_id(set_name: str, style: str, name: str, size: int) -> str:
    return f"icon-{set_name}-{style}-{slug(name)}-{size}"


def glyph_case(svg_text: str, set_name: str, style: str, name: str, seed: int = DEFAULT_SEED,
               source: str = "", identical: tuple[str, ...] = ()) -> Case:
    """One glyph's case, with its truth-derived minimum feature width. ``identical``
    names other styles whose upstream file is this one byte for byte; each adds an
    ``identical-<style>`` tag and is listed in ``params["identical_styles"]``."""
    from vinylgen.fidelity.fuzz import estimate_min_feature_px
    from vinylgen.fidelity.truth import scene_truth

    scene = glyph_scene(svg_text, set_name, style, name, seed, source)
    width = estimate_min_feature_px(scene_truth(scene))
    scene.params["min_feature_px"] = width
    scene.params["identical_styles"] = list(identical)
    return Case(case_id(set_name, style, name, scene.width), scene, width,
                case_tags(set_name, style, identical))


def case_tags(set_name: str, style: str, identical: tuple[str, ...] = ()) -> tuple[str, ...]:
    return ("icon", set_name, style, f"icon-{set_name}-{style}",
            *(f"identical-{other}" for other in identical))


def load_sources(root: Path = ICONS_DIR) -> list[dict]:
    """Every set's ``SOURCE.json``, in set-name order."""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(Path(root).glob("*/SOURCE.json"))]


def glyph_files(root: Path = ICONS_DIR) -> list[tuple[str, str, str, Path, tuple[str, ...]]]:
    """``(set, style, name, path, identical)`` of every committed glyph file, in a
    stable order. A style whose upstream file is byte-identical to an earlier
    style's (``SOURCE.json`` ``identical_styles``) has no file and no case of its
    own; ``identical`` names those styles on the case of the file they equal."""
    rows = []
    for source in load_sources(root):
        identical = source.get("identical_styles", {})
        for style in source["styles"]:
            for name in source["names"]:
                if style in identical.get(name, {}):
                    continue
                same = tuple(other for other, target in identical.get(name, {}).items()
                             if target == style)
                path = Path(root) / source["set"] / style / f"{name}.svg"
                rows.append((source["set"], style, name, path, same))
    return rows


@lru_cache(maxsize=4)
def _cached_cases(root: str, seed: int) -> tuple[Case, ...]:
    cases = []
    for set_name, style, name, path, identical in glyph_files(Path(root)):
        cases.append(glyph_case(path.read_text(encoding="utf-8"), set_name, style, name, seed,
                                str(path.relative_to(root)), identical))
    return tuple(cases)


def icon_cases(seed: int = DEFAULT_SEED, root: Path = ICONS_DIR) -> list[Case]:
    """Every committed glyph and style as a ``Case`` (module docstring). The same
    ``seed`` always gives the same cases; results are cached per process and
    returned as deep copies, so callers may mutate them."""
    return copy.deepcopy(list(_cached_cases(str(Path(root)), int(seed))))
