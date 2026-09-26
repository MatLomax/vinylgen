"""Fidelity bounds of a traced synthetic case: the one yardstick.

The synthetic regression tests (``tests/test_synthetic_fidelity.py``), the fidelity
sweep and the tuner (``sweep.py``, ``tune.py``) all judge a trace with this module:
``case_bounds`` derives everything that depends only on the case, its truth, the
default trace settings and (in the rgb tier) the palette stage's folds (``D``,
the true vertices with their roles and radii, the narrow zones, the area bounds,
the ideal node count); ``evaluate`` measures one trace against them; ``check``
turns a measured result into a pass or fail per assertion, and ``measurements``
and ``identities`` into the scalars and the items an expected failure records.

Lengths are pixels of the traced raster. ``M`` is the maximum deviation and ``A``
the corner sharpening allowance of the DEFAULT trace settings, resolved exactly as
the trace stage resolves them (``resolve_trace_lengths(TraceSettings(),
length_reference(width, height))``: 1 per mille of the longer side, floored at
1 px). The bounds never read the settings a trace ran with, so a tuner cannot
loosen its own yardstick.

What is derived and what is not
-------------------------------
Derived from the trace stage's own guarantees and the raster's geometry: the
label-map tier's quantisation ``Q``; ``D`` and ``e`` in the direction from the
truth or evidence to the trace; the vertex radii ``R(c)``; which corners are
decidable; the sharpenable-round radius and its reach ``rho``; the local
resolution and topology floors; the area tube ``E(c)``; the ideal node count and
the node limit built on it. Not derived:

- the rgb tier's ``Q``: a measured baseline (below), reused from the label-map
  tier because the palette stage's anti-alias resolution follows no geometric
  rule;
- ``D`` and ``e`` in the direction from the trace to the truth or evidence: the
  fitter keeps each sample within ``M`` of the curve at the sample's own
  parameter and bounds nothing between samples, so a curve may bow further there.
  The trace is held to the same ``D`` and ``e`` both ways by choice; the measured
  margin is in ``docs/validation-and-testing.md``;
- the node limit's one node per run part cut by a zone: the boundary there
  passes into geometry the ideal segmentation does not describe, so the fitter
  may end a piece at the cut; an allowance, not a derivation;
- the thin-feature pin ``THIN_RECALL``: the fitter's ``M`` allows a feature no
  wider than ``M`` to move wholly off its pixels, so holding a thin evidence
  region to its pixel centres is a requirement, set at the trace stage's own
  post-fit threshold for a protected component;
- which colours the palette stage folds and into what, and which survive under an
  unmatched colour (``observed_palette``): read from the run, a fold only among
  the colours the usage cutoff may fold, and the target checked against the
  replay's where the colour is at least ``D`` wide (``case_truth``);
- the topology bands: a trace within ``D`` of the truth sees each structure as
  the truth does from some band width between ``D`` and ``3 D``; the widths are
  sampled every ``D / 4``, not searched continuously;
- ``harness.MATCH_DELTA_E``, the palette matching threshold: chosen.

Quantities
----------
- ``Q`` (quantisation), label-map tier: the trace stage samples a colour boundary
  at the pixel-edge vertices of the label map (a hard-edged render puts every
  sub-pixel crossing on the pixel edge). A label-map pixel takes the colour
  covering most of it, which along a straight boundary is the colour at its
  centre, so a pixel edge between two colours crosses the true boundary between
  the two pixel centres: its midpoint lies within ``0.5 |cos t|`` of the boundary
  and its end vertices within ``0.5 (|cos t| + |sin t|) <= sqrt(2) / 2``
  (``t`` the boundary's angle to the edge normal). ``Q = sqrt(2) / 2``.
- ``Q``, full-pipeline tier: labels come from the palette stage's anti-alias
  resolution and samples move by a Lab-cost crossing estimate, so no quantisation
  bound is derivable. The rgb tier uses the label-map tier's ``sqrt(2) / 2`` as a
  measured baseline; its margin over the corpus, and the population it was
  measured on, are recorded in ``docs/validation-and-testing.md``.
- ``D = M + Q + DISTANCE_ERROR_BOUND``: the fitter keeps every sample within ``M``
  of the written curve (``curve_fit._curve_tolerance`` and
  ``_straight_deviation`` are both capped by it), the samples lie within ``Q`` of
  the truth and the metric (with the truth's own flattening) adds at most
  ``metrics.DISTANCE_ERROR_BOUND``. That bounds every truth point's distance to
  the trace; a traced point between two samples is held to ``D`` too, by choice
  (``What is derived``).
- ``e = max(M, A) + 1/2 + s + DISTANCE_ERROR_BOUND``, the evidence tolerance: the
  fitter keeps every sample within ``M`` of the curve and a sharpened corner
  within ``A`` of its sample; the samples are the pixel-edge vertices of the
  label map the trace received moved by the sub-pixel crossing offset ``s`` (0
  for hard labels; ``0.45 sqrt 2`` in the rgb tier, the crossing clamped to
  0.05-0.95 of the edge); and every point of a pixel-edge chain lies within 1/2
  of a vertex. Each shared boundary is fitted from the samples of that boundary
  alone, so each colour's evidence boundary lies within ``e`` of that colour's
  traced boundary. The other way round (a traced point between samples) is held
  to ``e`` by choice, as for ``D``.
- ``R(c)``, the radius of a true vertex ``c`` (corner, junction or edge point):
  the traced node where the traced branches meet lies within ``D`` of every true
  branch leaving ``c`` (each traced branch within ``D`` of its true branch), and
  an edge node lies on the exact frame. ``R(c)`` is the farthest point from ``c``
  of the part, containing ``c``, of the intersection of the ``D``-neighbourhoods
  of the true branches (each walked from ``c`` to the next true vertex), cut with
  the frame for an edge point (``vertex_radius``). It is computed exactly on the
  truth polylines, so it follows the geometry in every regime:

  * two straight branches with tip sector ``a`` (the smaller of the two sectors,
    ``a = 180 - turn`` for a corner): ``D / sin(a / 2)``, so a nearly straight
    corner has ``R`` about ``D`` and only a sharp tip grows;
  * an edge point whose branch meets the frame at ``phi``: ``D / sin(phi)``;
  * tangent branches (sector 0) with curvatures ``k1``, ``k2``: the branches
    separate as ``|k1 - k2| x^2 / 2``, so the bands stop overlapping near
    ``x = sqrt(4 D / |k1 - k2|)``;
  * any branch ends at the next true vertex, which caps a spike's radius by its
    side length.
- Decidable corners: a side's direction at a corner is read from the chord
  between boundary samples at the apex and at arc length ``l`` along the side.
  The true side's point there lies within ``e(l)`` of the apex tangent line and
  ``c(l)`` from the apex, so the true chord leaves the tangent by at most
  ``asin(e(l) / c(l))``; both samples lie within ``Q`` of the truth, which turns
  the chord by at most ``asin(2 Q / c(l))`` more. ``asin`` is superadditive on
  [0, 1], so the chord gives the tangent to ``asin((2 Q + e(l)) / c(l))``. The
  certified turn is ``turn_deg`` less both sides' errors, maximised over ``l`` up
  to the neighbouring true vertices along the boundary (``decidable_turn``; for
  straight sides ``turn - 2 asin(2 Q / l)``; both sides are read at the same
  ``l``). A side that curls away within a few pixels has a chord far shorter than
  its arc, so it certifies little. A ``corner`` whose certified turn reaches the
  threshold is *required*. Any other
  corner turning by at least ``threshold - delta``, ``delta = 2 atan(2 Q / s)``
  with ``s`` the distance to the nearest other true vertex, is *optional* (a cusp
  is allowed, not demanded). Every junction and edge point is required: the trace
  always anchors a cusp there.
- Sharpenable rounds: the trace sharpens a round whose sharp point (where its
  two sides meet) lies within ``A`` of its drawn edge (GLOSSARY "corner
  sharpening allowance"), a 90 degree round of radius up to
  ``SHARPENING_RADIUS_FACTOR * A`` (the sharp point lies ``r (sqrt 2 - 1)`` from
  the arc), and its corner detector skips that much arc beside a candidate (the
  tip skip), so a longer round vetoes its own corner. Seen through ``Q``: each
  side is read from samples within ``Q`` of the true side, so it may lie ``Q``
  inside it, which moves the two sides' meeting point up to ``Q sqrt 2`` towards
  the arc, and the drawn edge at the arc may lie ``Q`` outside it. So the trace
  may sharpen a round of radius up to
  ``round_radius = SHARPENING_RADIUS_FACTOR * (A + (1 + sqrt 2) Q)`` (2.71
  sharpening radii at ``A = 1`` px, 2.11 at the 1536 px canvas's ``A``), with its
  cusp within ``Q sqrt 2`` of the true sharp point, which lies within the round's
  radius of every point of its arc: all of the round lies within
  ``rho = round_radius + Q sqrt 2`` of the cusp. A cusp is therefore allowed where
  a stretch of true boundary turns by at least the threshold as a round of radius
  ``b`` (its length over its turn in radians; 0 at a single true vertex) of at
  most ``round_radius``, all within ``b + Q sqrt 2`` of it (``round_turn_near``).
  A cusp on a larger round (such as a circle of larger radius, whose every
  stretch is a round of that radius), or farther from a round or vertex than
  that, is a kink.

The truth a trace is judged against
-----------------------------------
In the labels tier it is the scene's truth. In the rgb tier the palette stage
replaces whole colours by design, so the truth is the scene with those colours
recoloured (``case_truth``): the colours ``scene.params["expect"]["merge"]`` names
merge (each pair into its lower index), and each colour the usage cutoff folds
takes the colour it folded into. Every boundary, vertex and ideal node that
survives the merge or fold is then well defined and asserted in full; a boundary
between a folded colour and the colour replacing it is gone. The usage cutoff
may fold a colour when ``palette_floors``' replay on the true areas folds it
(colours in increasing area order, each below ``min_colour_share`` of the canvas
and not preserved folds into the nearest remaining colour in Lab, stopping once
``final_colours or min_output_colours`` colours remain). Whether it does, and
into what, depends on the colour the palette stage inferred for it (a thin
feature's inferred colour is a blend with its neighbours, so both its share and
its nearest surviving colour differ from the scene colour's), so it is read from
the run (``observed_palette``): a colour some inferred colour matches survived;
otherwise it folded into the scene colour most of its pixel centres carry in the
evidence. A colour that may fold but covers no pixel centre stays a zone. Any
scene colour whose pixel centres mostly carry an inferred colour that matches no
scene colour survived under that colour (a thin feature's anti-alias blend): the
fill *stands in* for it in the geometric assertions, so its geometry is still
judged, while ``topology`` reports the layer as unmatched and ``evidence`` and
``palette`` report the colour as missing from the palette.

Local floors
------------
A feature narrower than ``RESOLUTION_FLOOR_PX`` (2 px) has in places a single
pixel centre across it, so one pixel row carries both of its boundaries and they
cannot each be placed within ``Q``; a band narrower than ``TOPOLOGY_FLOOR_PX``
(``sqrt 2``: a band of width ``w`` at angle ``t`` has a four-connected chain of
pixel centres only when ``w >= |cos t| + |sin t|``) may legitimately break into
corner-touching pieces. The floors are applied locally: a colour's narrow parts
at floor ``f`` are its geometry minus its morphological opening at half-width
``f / 2`` (``narrow_parts``), less the pieces a vertex disc already covers (the
opening also trims every convex corner, within that corner's ``R``, or within
``D`` of a shallow vertex, whose wide angle leaves a sliver a fraction of a pixel
deep). The
*resolution zone* is the resolution-floor narrow parts buffered by ``D`` (their
traced boundaries lie within ``D`` of them); the *topology zone* likewise at the
topology floor. Outside the zones every truth assertion holds in full; the
evidence distance holds inside them too. An assertion with nothing left outside
them (``available``) is reported as having nothing to hold, never as passed.

Full-pipeline floor (rgb tier): the palette stage removes by design a colour
region smaller than ``PaletteSettings().min_region_pixels`` (small-region
cleanup), unless it keeps it as a coherent thin feature, and fills it with a
touching colour of its choosing. Such a region (and a colour that may fold but
has no observable fold), buffered by ``D``, is the *palette zone*: the region's
own neighbourhood, nothing more. A zoned colour is left out of the per-colour
area and topology comparisons.

Assertions (``ASSERTIONS``)
---------------------------
- ``hausdorff``: every boundary sample (traced and truth, off the frame) outside
  the vertex discs and the zones is within ``D`` of the other side's samples;
  and everywhere, each traced layer's boundary is within ``e`` of the boundary
  of its label in the evidence, both ways (``evidence_distance``). The second
  part holds a feature below the resolution floor near its place, where the first
  has nothing; ``evidence``'s thin-region pin holds it on its pixels.
- ``corners``: every required vertex outside the zones has a cusp node within
  ``R(c)``. A required vertex inside a zone is optional.
- ``kinks``: every cusp node lies within ``R(c)`` of a required or optional
  vertex, inside a zone, or on a sharpenable round (a stretch of the truth
  turning by at least the threshold as a round of radius ``b`` at most
  ``round_radius``, all within ``b + Q sqrt 2`` of it).
- ``nodes``: boundary nodes outside the zones ``<=`` the node limit
  (``zone_node_limit``), built on the ideal node count of the boundary outside
  the zones (``zone_ideal_nodes``: the ideal segmentation's vertex nodes whose
  traced node can lie outside them, within ``R(c)`` of an anchored vertex or
  ``D`` of any other, and the ideal segmentation's interior nodes that can fall
  on the parts of its runs outside them). The ideal segmentation (one cubic per
  arc of at most 90 degrees, within ``2.7e-4 r`` of the arc; one line per line,
  which a cubic also draws exactly; one cubic per scene cubic) keeps every
  sample within ``Q + 2.7e-4 r`` of the curve, below the fitter's curve
  tolerance (at least 1 px, and ``Q + 2.7e-4 r < 1`` for every radius below
  1000 px). The fitter takes the fewest pieces over its candidate nodes
  (``docs/svg-tracing-spec.md``, "Selection"), which include every axis extreme,
  and seams a closed ring with no anchor at an axis extreme unless that costs a
  piece. Between consecutive vertices and axis extremes, an arc turns by at most
  90 degrees and a cubic is a piece of one cubic, so the ideal pieces split at
  every axis extreme are a route the fitter's search can take. The limit is the
  vertex nodes, plus on each part of an ideal run outside the zones the larger
  of its ideal interior nodes and its axis extremes, plus one node per part end
  the zone cuts (an allowance: see ``What is derived``). A traced count above it
  is a route that is not minimal over the fitter's own candidates.
- ``topology``: for every colour of the truth or the trace, the polygon and hole
  counts of the traced and the true geometry, each less the vertex discs, the
  topology zone and the palette zone, the structures that decide themselves by
  location, and no traced layer is unmatched. The discs are removed because a
  tip narrower than a pixel may leave a pixel touching the shape only corner to
  corner and two regions meeting at a point may join through one pixel. A
  boundary within ``D`` of the truth that meets the edge of a removed disc or
  zone may cut off a sliver or break a strip there, so a band round the removed
  region is filled with the colour being counted, for the truth and the trace
  alike, and only the pieces and holes reaching beyond it count. The trace is
  counted with a band ``2 D`` wide; seen from a band edge ``2 D`` out, a trace
  within ``D`` of the truth shows each structure as the truth does from an edge
  between ``D`` and ``3 D`` out, so the truth is counted at every width from
  ``D`` to ``3 D`` in steps of ``D / 4`` (``TOPOLOGY_BAND_MULTIPLES``). A count
  is asserted, to lie within its range over the widths, only where the truth's
  count at the traced band is the range's minimum, so deleting any one structure
  it counts fails it; a count whose range reaches below it (structures near the
  band edge that come and go with the width) cannot decide a single deletion and
  is not asserted. Each colour and each of its two counts is judged on its own.
  Located structures: a true piece or hole none of whose rings comes within
  ``3 D`` of the removed region (*isolated*) is counted the same at every width,
  and must match exactly one traced structure of its kind, each region within the
  topology tolerance (``max(D, e)``) of the other; a traced piece or hole whose
  rings clear the removed region by ``3 D`` plus that tolerance (so its true
  counterpart would be isolated) must match an isolated true structure.
  ``topology_mutations`` checks, per case, that the truth itself passes and that
  every single deletion the assertion claims (an isolated structure, or a
  structure an asserted count counts) fails it.
- ``area``: per colour ``c``, the symmetric difference of the traced and true
  geometry is at most the area of ``E(c)``: the ``D``-tube round ``c``'s true
  boundary, the discs of the vertices on it and the palette zone near it (with the
  boundary within ``D`` of the truth off the discs, every point traced in the wrong
  colour lies there; a feature narrower than ``2 D`` that vanishes lies in its own
  tube). A colour is asserted only where ``area(E(c))`` is below its true area,
  so a trace that drops it fails; the other colours are held by ``evidence``
  (``area_coverage`` checks that every colour is held by one of the two). The
  total misassigned area is also bounded by the area of the union of the ``E(c)``.
- ``evidence``: what the trace stage received must survive. The evidence is the
  label map it traced (the hard-edged render in the labels tier; the palette
  stage's ``segmentation.npz`` labels in the rgb tier). Every four-connected
  region of every evidence colour has traced area of that colour within ``e`` of
  it, and per colour the symmetric difference of the traced layer and the
  evidence pixels is at most the area of the ``e``-tube round the evidence
  boundary. In the rgb tier every scene colour the area assertion does not hold
  must be an inferred colour, unless the palette stage removes it by design (a
  folded or zoned colour, or one whose every region is below the small-region
  cleanup size) or it dominates no pixel of the render (it is absent from the
  hard-edged label render), so the palette stage never received it as a colour.
  A folded colour at least ``D`` wide must fold into the colour the replay folds
  it into (``case_truth``). Every *thin* evidence region (four-connected, most
  of its pixels in no 2 x 2 block of its own pixels, so narrower than the
  resolution floor along most of it), unless all those thin pixels lie in the
  vertex discs, must have at least ``THIN_RECALL`` of the thin pixels' centres
  covered by its traced layer (``thin_regions``): ``e``
  exceeds a one-pixel feature's width, so this is what holds such a feature on
  its pixels rather than a full width off them.
- ``palette`` (palette-stage cases): ``scene.params["expect"]`` (see
  ``palette_outcome``).

Expected failures record ``measurements``, the scalars each assertion compares
with its bound (``MEASUREMENTS``), and ``identities``, the items it finds wrong
(``IDENTITIES``: kink points, missed vertices, node positions, missing regions,
wrong per-colour counts and so on). ``entry_ratchet`` compares both with the
record: the scalars within ``RATCHET_TOLERANCE``, the identities as a set, item
for item within ``IDENTITY_TOLERANCE``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import shapely
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box
from shapely.ops import unary_union

from vinylgen.contracts import TraceSettings
from vinylgen.fidelity.families import SHARPENING_RADIUS_FACTOR, case_palette_settings
from vinylgen.fidelity.metrics import (DISTANCE_ERROR_BOUND, SAMPLE_SPACING,
                                       _boundary_samples, _geometry_rings, _polygons,
                                       measure, parse_traced_svg, path_geometry)
from vinylgen.trace_lengths import length_reference, resolve_trace_lengths

TIERS = ("labels", "rgb")
ASSERTIONS = ("hausdorff", "corners", "kinks", "nodes", "topology", "area", "evidence",
              "palette")
Q_PX = math.sqrt(2.0) / 2.0
# The rgb tier's sub-pixel crossing offset moves a sample by at most 0.45 px along
# each axis (``boundary_graph.shared_boundary_polygons`` clamps the crossing to
# 0.05-0.95 of the edge); hard labels put every crossing on the edge midpoint.
EVIDENCE_OFFSET_PX = {"labels": 0.0, "rgb": 0.45 * math.sqrt(2.0)}
RESOLUTION_FLOOR_PX = 2.0
# The least fraction of a thin evidence region's pixel centres its traced layer
# must cover: chosen, the trace stage's own post-fit acceptance threshold for a
# protected component (``validation.validate_postfit_protected_features``,
# ``minimum_component_pixel_recall``).
THIN_RECALL = 0.6
TOPOLOGY_FLOOR_PX = math.sqrt(2.0)
# Topology bands, in multiples of D beyond the removed discs and zones: the truth
# is counted with each, the trace with the middle one (see ``topology``).
TOPOLOGY_BAND_MULTIPLES = tuple(1.0 + 0.25 * step for step in range(9))
TOPOLOGY_BAND_TRACED = 2.0
MIN_PIECE_AREA = 1e-6
# A narrow piece a vertex disc covers to within this area is that vertex's.
_COVERED_AREA = 1e-2
# The opening is compared with a margin, so the chord error of the flattened
# truth and of the buffer arcs never reads as a narrow sliver along a curve.
_OPENING_MARGIN = 1e-2
_FLOOR_EPS = 1e-3
_BUFFER_SEGMENTS = 16
# Buffer arcs are inscribed polygons; buffering by this multiple circumscribes them.
_CIRCUMSCRIBE = 1.0 / math.cos(math.pi / (4 * _BUFFER_SEGMENTS))
_VERTEX_MATCH_PX = 0.01
# Two regions within the topology tolerance of each other may differ by this
# much area outside each other's buffer (floating-point slack of the buffers).
_SAME_REGION_AREA = 1e-4
_CHORD_GROWTH = 1.25
_FRAME_EPS = 1e-6
_NODE_DECIMALS = 6


# --- lengths --------------------------------------------------------------------------------


def resolved_lengths(width: int, height: int) -> TraceSettings:
    """The default trace lengths in px on this canvas, as the trace stage resolves them."""
    return resolve_trace_lengths(TraceSettings(), length_reference(width, height))


def boundary_bound(max_deviation: float) -> float:
    """``D``: the boundary distance bound off the vertex discs and zones."""
    return max_deviation + Q_PX + DISTANCE_ERROR_BOUND


def _buffer(geometry, distance: float):
    """``geometry`` grown by at least ``distance`` everywhere (circumscribed arcs)."""
    return geometry.buffer(distance * _CIRCUMSCRIBE, quad_segs=_BUFFER_SEGMENTS)


# --- truth rings and vertices -----------------------------------------------------------------


def truth_rings(truth) -> list[np.ndarray]:
    return [ring for geometry in truth.colour_geometry.values()
            for ring in _geometry_rings(geometry)]


def _on_frame(points: np.ndarray, width: float, height: float) -> np.ndarray:
    x, y = points[..., 0], points[..., 1]
    return ((np.abs(x) <= _FRAME_EPS) | (np.abs(x - width) <= _FRAME_EPS)
            | (np.abs(y) <= _FRAME_EPS) | (np.abs(y - height) <= _FRAME_EPS))


def _frame_segment(a, b, width: float, height: float) -> bool:
    """Whether the segment ``a``-``b`` runs along one side of the canvas frame."""
    return ((abs(a[0]) <= _FRAME_EPS and abs(b[0]) <= _FRAME_EPS)
            or (abs(a[0] - width) <= _FRAME_EPS and abs(b[0] - width) <= _FRAME_EPS)
            or (abs(a[1]) <= _FRAME_EPS and abs(b[1]) <= _FRAME_EPS)
            or (abs(a[1] - height) <= _FRAME_EPS and abs(b[1] - height) <= _FRAME_EPS))


def off_frame_lines(rings: list[np.ndarray], width: float, height: float):
    """The rings as a MultiLineString without their stretches along the frame."""
    lines = []
    for ring in rings:
        frame = np.asarray([_frame_segment(ring[i], ring[i + 1], width, height)
                            for i in range(len(ring) - 1)], dtype=bool)
        start = None
        for index, on in enumerate(frame):
            if not on and start is None:
                start = index
            if on and start is not None:
                lines.append(ring[start:index + 1])
                start = None
        if start is not None:
            lines.append(ring[start:])
    lines = [line for line in lines if len(line) >= 2]
    return MultiLineString([LineString(line) for line in lines]) if lines else MultiLineString()


def _rings_through(rings: list[np.ndarray], point) -> list[tuple[np.ndarray, int]]:
    """Every closed ring (without its repeated end) passing through ``point``, with
    the index of ``point`` in it: its vertex within ``_VERTEX_MATCH_PX``, or, when
    the ring passes that close between two vertices, ``point`` inserted there."""
    found = []
    target = np.asarray(point, dtype=np.float64)
    for ring in rings:
        closed = ring[:-1]
        if len(closed) < 2:
            continue
        distance = np.hypot(closed[:, 0] - target[0], closed[:, 1] - target[1])
        index = int(distance.argmin())
        if distance[index] <= _VERTEX_MATCH_PX:
            found.append((closed, index))
            continue
        starts, ends = closed, np.roll(closed, -1, axis=0)
        span = ends - starts
        length2 = np.einsum("ij,ij->i", span, span)
        t = np.clip(np.einsum("ij,ij->i", target - starts, span)
                    / np.where(length2 > 0, length2, 1.0), 0.0, 1.0)
        nearest = starts + span * t[:, None]
        gap = np.hypot(*(nearest - target).T)
        segment = int(gap.argmin())
        if gap[segment] <= _VERTEX_MATCH_PX:
            inserted = np.insert(closed, segment + 1, target, axis=0)
            found.append((inserted, segment + 1))
    return found


def _side(ring: np.ndarray, apex: int, direction: int, vertices: np.ndarray) -> np.ndarray:
    """The ring from ``apex`` walking ``direction`` up to (and including) the next
    true vertex; on a ring with no other vertex, up to half way round (where the
    apex's two sides meet)."""
    count = len(ring)
    order = (apex + direction * np.arange(count + 1)) % count
    side = ring[order]
    if len(vertices):
        near = cKDTree(vertices).query(side[1:])[0] <= _VERTEX_MATCH_PX
        if near.any():
            return side[:int(np.argmax(near)) + 2]
    arc = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(side, axis=0).T))))
    half = int(np.searchsorted(arc, arc[-1] / 2.0))
    return side[:max(2, half + 1)]


def _reading_at(side: np.ndarray, length: float) -> tuple[float, float]:
    """For the point at arc length ``length`` along ``side`` (from the apex, its
    first point): its chord length from the apex, and the largest distance of the
    side up to it from the side's tangent line at the apex."""
    steps = np.hypot(*np.diff(side, axis=0).T)
    arc = np.concatenate(([0.0], np.cumsum(steps)))
    first = int(np.argmax(steps > 0))
    tangent = (side[first + 1] - side[0]) / steps[first]
    reach = min(int(np.searchsorted(arc, length, side="left")), len(arc) - 1)
    span = arc[reach] - arc[reach - 1] if reach > 0 else 0.0
    t = (length - arc[reach - 1]) / span if span > 0 else 1.0
    point = side[reach - 1] + (side[reach] - side[reach - 1]) * min(1.0, max(0.0, t))
    offsets = np.vstack((side[:reach], point[None, :])) - side[0]
    distance = np.abs(offsets[:, 0] * tangent[1] - offsets[:, 1] * tangent[0])
    return float(np.hypot(*offsets[-1])), float(distance.max())


def _other_vertices(vertices: np.ndarray, point) -> np.ndarray:
    if not len(vertices):
        return vertices
    return vertices[np.hypot(vertices[:, 0] - point[0], vertices[:, 1] - point[1])
                    > _VERTEX_MATCH_PX]


def decidable_turn(rings: list[np.ndarray], point, turn_deg: float,
                   vertices: np.ndarray) -> float | None:
    """The smallest turn the raster certifies at a true corner of turn ``turn_deg``
    (module docstring); None when no truth ring passes through the corner."""
    through = _rings_through(rings, point)
    if not through:
        return None
    ring, apex = through[0]
    others = _other_vertices(vertices, point)
    sides = [_side(ring, apex, direction, others) for direction in (-1, 1)]
    arcs = [np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(side, axis=0).T))))
            for side in sides]
    if any(arc[-1] <= 0 for arc in arcs):
        return None
    best = -math.inf
    length = 2.0 * Q_PX
    while all(length <= arc[-1] for arc in arcs):
        error = 0.0
        for side in sides:
            chord, departure = _reading_at(side, length)
            ratio = (2.0 * Q_PX + departure) / chord if chord > 0 else 1.0
            error += math.degrees(math.asin(min(1.0, ratio)))
        best = max(best, turn_deg - error)
        length *= _CHORD_GROWTH
    return best


def vertex_branches(rings: list[np.ndarray], point, vertices: np.ndarray, width: float,
                    height: float) -> list[np.ndarray]:
    """The true boundary branches leaving ``point``: both sides of every truth ring
    through it, each walked to the next true vertex, without the stretches that run
    along the canvas frame (the frame is no colour boundary)."""
    others = _other_vertices(vertices, point)
    branches = []
    for ring, apex in _rings_through(rings, point):
        for direction in (-1, 1):
            side = _side(ring, apex, direction, others)
            if len(side) < 2 or _frame_segment(side[0], side[1], width, height):
                continue
            branches.append(side)
    return branches


def vertex_radius(kind: str, point, branches: list[np.ndarray], bound: float, width: float,
                  height: float) -> float:
    """``R(c)`` (module docstring): the farthest point from ``point`` of the part,
    containing ``point``, of the intersection of the branches' ``bound``
    neighbourhoods, cut with the canvas frame for an edge point."""
    if not branches:
        return bound
    region = None
    for branch in branches:
        near = _buffer(LineString(branch), bound)
        region = near if region is None else region.intersection(near)
    if kind == "edge":
        region = region.intersection(box(0.0, 0.0, width, height).exterior)
    here = Point(point)
    parts = [part for part in getattr(region, "geoms", [region]) if not part.is_empty]
    if not parts:
        return bound
    part = min(parts, key=lambda part: part.distance(here))
    coordinates = [np.asarray(part.exterior.coords) if isinstance(part, Polygon)
                   else np.asarray(part.coords)]
    coords = np.vstack(coordinates)
    return float(max(bound, np.hypot(coords[:, 0] - point[0], coords[:, 1] - point[1]).max()))


def corner_roles(truth, rings: list[np.ndarray], threshold: float) -> list[dict]:
    """Every true vertex with its role: ``required`` (a junction or edge point, or a
    corner whose ``decidable_turn`` reaches the threshold), ``optional`` (any other
    corner turning by at least the threshold less ``delta = 2 atan(2 Q / s)``, ``s``
    the distance to the nearest other true vertex) or ``shallow``."""
    points = np.asarray([corner.point for corner in truth.corners],
                        dtype=np.float64).reshape(-1, 2)
    if len(points) > 1:
        distances, _ = cKDTree(points).query(points, k=2)
        nearest = distances[:, 1]
    else:
        nearest = np.full(len(points), math.inf)
    rows = []
    for corner, spacing in zip(truth.corners, nearest):
        delta = math.degrees(2.0 * math.atan(2.0 * Q_PX / spacing)) if spacing > 0 else 180.0
        decidable = None
        if corner.kind != "corner":
            role = "required"
        else:
            decidable = decidable_turn(rings, corner.point, corner.turn_deg, points)
            if decidable is None:
                decidable = corner.turn_deg - delta
            if decidable >= threshold:
                role = "required"
            elif corner.turn_deg >= threshold - delta:
                role = "optional"
            else:
                role = "shallow"
        rows.append({"point": [float(corner.point[0]), float(corner.point[1])],
                     "kind": corner.kind, "turn_deg": float(corner.turn_deg),
                     "delta_deg": float(delta), "decidable_turn_deg": decidable,
                     "role": role, "narrow": False})
    return rows


def round_turn_near(rings: list[np.ndarray], point, round_radius: float,
                    reach: float) -> float:
    """The largest net turn (degrees) of a *round* near ``point``: a stretch of
    true boundary (vertices ``i..j`` of a ring, turning at each) whose radius
    ``b`` (its length between its first and last vertex over its net turn in
    radians; 0 for a single vertex, a tangent discontinuity) is at most
    ``round_radius``, and every vertex of which lies within ``b + reach`` of
    ``point``. A round of radius ``b`` lies within ``b`` of its sharp point, and a
    cusp sharpened from it within ``reach`` of that point (module docstring,
    "Sharpenable rounds")."""
    best = 0.0
    px, py = point
    window = round_radius + reach
    for ring in rings:
        closed = ring[:-1] if np.allclose(ring[0], ring[-1]) else ring
        count = len(closed)
        if count < 3:
            continue
        distance = np.hypot(closed[:, 0] - px, closed[:, 1] - py)
        near = distance <= window
        if not near.any():
            continue
        before = closed - np.roll(closed, 1, axis=0)
        after = np.roll(closed, -1, axis=0) - closed
        turns = np.arctan2(before[:, 0] * after[:, 1] - before[:, 1] * after[:, 0],
                           np.einsum("ij,ij->i", before, after))
        steps = np.hypot(*after.T)
        # Walk each run of vertices within the window, starting outside it.
        runs = []
        if near.all():
            runs.append(np.arange(count))
        else:
            start = int(np.flatnonzero(~near)[0])
            order = np.roll(np.arange(count), -start)
            flags = near[order]
            index = 0
            while index < count:
                if not flags[index]:
                    index += 1
                    continue
                end = index
                while end + 1 < count and flags[end + 1]:
                    end += 1
                runs.append(order[index:end + 1])
                index = end + 1
        for run in runs:
            run_turns = turns[run]
            run_steps = steps[run]
            run_distance = distance[run]
            for i in range(len(run)):
                total, length, farthest = 0.0, 0.0, 0.0
                for j in range(i, len(run)):
                    if j > i:
                        length += run_steps[j - 1]
                    total += run_turns[j]
                    farthest = max(farthest, run_distance[j])
                    turn = abs(total)
                    if turn <= 1e-12:
                        continue
                    bend = length / turn
                    if bend > round_radius:
                        continue
                    if farthest <= bend + reach:
                        best = max(best, math.degrees(min(turn, 2.0 * math.pi)))
    return best


# --- narrow parts, palette floors ---------------------------------------------------------------


def narrow_parts(geometry, floor: float, discs) -> list:
    """The parts of ``geometry`` narrower than ``floor``: the geometry less its
    morphological opening at half-width ``floor / 2`` (a hair under, so a feature
    exactly at the floor is not narrow), without the pieces the vertex ``discs``
    cover (the opening trims every convex corner)."""
    radius = floor / 2.0 - _FLOOR_EPS
    opened = geometry.buffer(-radius, quad_segs=_BUFFER_SEGMENTS).buffer(
        radius, quad_segs=_BUFFER_SEGMENTS)
    remainder = geometry.difference(opened.buffer(_OPENING_MARGIN, quad_segs=_BUFFER_SEGMENTS))
    pieces = []
    for piece in _polygons(remainder):
        if piece.area <= MIN_PIECE_AREA:
            continue
        if discs is not None and piece.difference(discs).area <= _COVERED_AREA:
            continue
        pieces.append(piece)
    return pieces


def preserved_truth_colours(case, colour_geometry: dict) -> set[int]:
    """The scene colours whole-colour preservation exempts from the usage cutoff:
    the colour at each ``preserved_colour_points`` source pixel's centre."""
    colours = set()
    for x, y in case_palette_settings(case).preserved_colour_points:
        centre = Point(x + 0.5, y + 0.5)
        for index, geometry in colour_geometry.items():
            if geometry.contains(centre):
                colours.add(index)
    return colours


def merge_aliases(case) -> dict[int, int]:
    """The colours ``scene.params["expect"]["merge"]`` names as merging, each mapped
    to the lower index of its pair (the colour the post-merge truth keeps)."""
    expect = case.scene.params.get("expect") or {}
    aliases: dict[int, int] = {}
    for first, second in expect.get("merge", []):
        low, high = sorted((int(first), int(second)))
        aliases[high] = aliases.get(low, low)
    return aliases


def palette_floors(case, colour_geometry: dict) -> dict:
    """What the palette stage removes from ``case`` by design: ``regions`` (true
    colour regions below the small-region cleanup size, as ``(colour, polygon)``),
    ``folded`` (each colour the whole-colour usage cutoff folds, mapped to the
    colour it finally folds into) and a reason each.

    The fold follows ``segmentation.fold_underrepresented_colours`` on the true
    colour areas: colours in increasing area order, each below
    ``min_colour_share`` of the canvas and not preserved folds into the nearest
    remaining colour in Lab (whose area then grows), and folding stops once
    ``final_colours or min_output_colours`` colours remain (so a thin feature on a
    two-colour canvas is never folded). Small-region cleanup runs before the fold,
    so the regions of a colour that later folds are listed too."""
    from vinylgen.colour import srgb_to_lab
    palette = case_palette_settings(case)
    canvas = float(case.scene.width * case.scene.height)
    preserved = preserved_truth_colours(case, colour_geometry)
    lab = srgb_to_lab(np.asarray([row["rgb"] for row in case.scene.palette], dtype=np.uint8))
    areas = {index: float(g.area) for index, g in colour_geometry.items() if g.area > 0}
    active = set(areas)
    minimum = palette.final_colours or palette.min_output_colours
    into: dict[int, int] = {}
    reasons = []
    for source in sorted(active, key=lambda index: (areas[index], index)):
        if len(active) <= minimum:
            break
        if source not in active or source in preserved:
            continue
        share = areas[source] / canvas
        if share >= palette.min_colour_share:
            continue
        targets = sorted(active - {source})
        target = min(targets, key=lambda index: float(np.linalg.norm(lab[index] - lab[source])))
        areas[target] += areas[source]
        active.remove(source)
        into[source] = target
        reasons.append(f"colour {source} covers {100 * share:.2f}% "
                       f"< {100 * palette.min_colour_share:g}% and folds into {target}")
    folded = {}
    for source in into:
        target = into[source]
        while target in into:
            target = into[target]
        folded[source] = target
    regions = []
    for index, geometry in sorted(colour_geometry.items()):
        for polygon in _polygons(geometry):
            if MIN_PIECE_AREA < polygon.area < palette.min_region_pixels:
                regions.append((index, polygon))
                reasons.append(f"colour {index} has a region of {polygon.area:.1f} px^2 "
                               f"< {palette.min_region_pixels} px")
    return {"regions": regions, "folded": folded, "reasons": reasons}


def recoloured_scene(scene, mapping: dict[int, int]):
    """``scene`` with every region (and the background) of a colour in ``mapping``
    drawn in the colour it maps to; the geometry is unchanged."""
    from dataclasses import replace
    return replace(scene, background=mapping.get(scene.background, scene.background),
                   regions=[replace(region, colour=mapping.get(region.colour, region.colour))
                            for region in scene.regions])


def _pixel_centres_inside(geometry, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """The (row, column) indices of the pixels whose centre lies in ``geometry``."""
    if geometry.is_empty:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    x0, y0, x1, y1 = geometry.bounds
    xs = np.arange(max(0, math.floor(x0)), min(width, math.ceil(x1)))
    ys = np.arange(max(0, math.floor(y0)), min(height, math.ceil(y1)))
    if not len(xs) or not len(ys):
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    grid_x, grid_y = np.meshgrid(xs, ys)
    shapely.prepare(geometry)
    inside = shapely.contains_xy(geometry, grid_x + 0.5, grid_y + 0.5)
    return grid_y[inside], grid_x[inside]


def observed_palette(case, colour_geometry: dict, may_fold: set[int], aliases: dict[int, int],
                     run) -> dict:
    """How the palette stage treated each scene colour no inferred colour matches,
    read from the evidence (the label map the trace received) at the pixels whose
    centre the colour covers:

    - ``folded``: a colour the usage cutoff may fold (``may_fold``,
      ``palette_floors``' replay on the true areas) whose pixels mostly carry
      another scene colour folded into it;
    - ``stand_ins``: a colour whose pixels mostly carry an inferred colour that
      matches no scene colour survived under that colour (a thin feature's
      anti-alias blend, ``harness.MATCH_DELTA_E`` away from its own colour);
      that fill stands in for it in the geometric assertions, while the palette,
      topology and evidence assertions still report the unmatched colour;
    - ``zoned``: a colour that may fold but covers no pixel centre, whose outcome
      cannot be observed.

    A colour neither may fold nor stands in anywhere is left as it is, so the
    assertions that hold it fail when it is lost. The fold's decision (the
    colour's share of the canvas as the palette stage counts it, anti-aliased
    pixels included) and its target (the nearest surviving colour to the colour
    the palette stage inferred for it, which for a thin feature is a blend of the
    feature and its neighbours) depend on the inferred colours, so neither is
    derivable from the truth; the whole-colour replacement itself is."""
    matched = {aliases.get(row["truth_index"], row["truth_index"])
               for row in run.palette_matches if row["matched"]}
    with np.load(run.subject_dir / "segmentation.npz") as bundle:
        labels = np.asarray(bundle["labels"])
    mapping = {str(fill).upper(): aliases.get(index, index)
               for fill, index in run.colour_map.items()}
    fills = [str(row["hex"]).upper() for row in run.palette]
    height, width = labels.shape
    folded, stand_ins, zoned = {}, {}, set()
    claims: dict[str, tuple[int, int]] = {}
    for colour in sorted(colour_geometry):
        if colour in matched:
            continue
        rows, columns = _pixel_centres_inside(colour_geometry[colour], width, height)
        if not len(rows):
            if colour in may_fold:
                zoned.add(colour)
            continue
        counts = np.bincount(labels[rows, columns], minlength=len(fills))
        fill = fills[int(counts.argmax())]
        if fill in mapping:
            if colour in may_fold:
                folded[colour] = mapping[fill]
            continue
        # An unmatched fill stands in for the colour whose pixels it holds most of.
        count = int(counts.max())
        if fill not in claims or claims[fill][1] < count:
            claims[fill] = (colour, count)
    stand_ins = {fill: colour for fill, (colour, _) in claims.items()}
    return {"folded": folded, "stand_ins": stand_ins, "zoned": zoned}


def case_truth(case, tier: str, truth=None, run=None) -> tuple[object, dict]:
    """The truth a trace of ``case`` in ``tier`` is judged against, and the palette
    outcome behind it: ``aliases`` (merged colours), ``folded`` (each folded colour
    and the colour it folded into), ``stand_ins`` (unmatched inferred colours and
    the scene colour each stands in for), ``zoned`` (colours whose fold cannot be
    observed), ``regions`` (below the small-region cleanup size) and ``reasons``.

    In the labels tier that is the scene's truth. In the rgb tier the palette stage
    merges the colours ``merge_aliases`` names and folds colours below the usage
    cutoff, each a whole-colour replacement, so the truth is the scene with those
    colours recoloured (``recoloured_scene``): every boundary, vertex and ideal
    node that survives the merge or fold is well defined, and one that separated
    a folded or merged colour from the colour replacing it is gone. Which colours
    fold, and into what, and which survive under an unmatched colour, is read from
    ``run`` (``observed_palette``); without ``run`` every colour the usage cutoff
    may fold is ``zoned``. The regions below the small-region cleanup size are not
    recoloured: which touching colour replaces one, or whether a coherent thin
    feature keeps it, is the palette stage's call, so they stay a zone."""
    from vinylgen.fidelity.truth import scene_truth
    scene = case.scene
    truth = truth if truth is not None else scene_truth(scene)
    if tier != "rgb":
        return truth, {"aliases": {}, "folded": {}, "stand_ins": {}, "zoned": set(),
                       "regions": [], "reasons": [], "fold_mismatches": []}
    aliases = merge_aliases(case)
    merged = scene_truth(recoloured_scene(scene, aliases)) if aliases else truth
    floors = palette_floors(case, merged.colour_geometry)
    may_fold = set(floors["folded"])
    if run is None:
        folded, stand_ins, zoned = {}, {}, may_fold
    else:
        observed = observed_palette(case, merged.colour_geometry, may_fold, aliases, run)
        folded, stand_ins, zoned = observed["folded"], observed["stand_ins"], observed["zoned"]
    reasons = [f"colour {high} merges into {low}" for high, low in sorted(aliases.items())]
    reasons += floors["reasons"]
    reasons += [f"colour {colour} folded into {target}" for colour, target in sorted(folded.items())]
    reasons += [f"colour {colour} may fold and its outcome is not observable"
                for colour in sorted(zoned)]
    reasons += [f"colour {colour} survives as the unmatched colour {fill}"
                for fill, colour in sorted(stand_ins.items())]
    # The replay's fold target is the truth's call; the observed one moves the
    # boundary by up to the folded colour's width where they differ, which
    # matters (and must agree) only where the colour is at least D wide.
    bound = boundary_bound(float(resolved_lengths(scene.width, scene.height).max_deviation))
    fold_mismatches = []
    for colour, target in sorted(folded.items()):
        replay = floors["folded"].get(colour)
        if replay is None or replay == target:
            continue
        wide = not merged.colour_geometry[colour].buffer(
            -bound / 2.0, quad_segs=_BUFFER_SEGMENTS).is_empty
        if wide:
            fold_mismatches.append([colour, target, replay])
        else:
            reasons.append(f"colour {colour} folded into {target}, not the replay's {replay}: "
                           f"immaterial, as the colour is narrower than D ({bound:.3f} px) "
                           f"everywhere, so either target's boundary lies within D of the "
                           f"other's")
    mapping = {index: folded.get(target, target) for index, target in aliases.items()}
    mapping.update(folded)
    effective = scene_truth(recoloured_scene(scene, mapping)) if folded else merged
    regions = list(floors["regions"])
    regions += [(colour, polygon) for colour in sorted(zoned)
                for polygon in _polygons(merged.colour_geometry[colour])]
    return effective, {"aliases": aliases, "folded": folded, "stand_ins": dict(stand_ins),
                       "zoned": set(zoned), "regions": regions, "reasons": reasons,
                       "fold_mismatches": fold_mismatches}


@dataclass
class CaseBounds:
    """Everything the bounds of one (case, tier) derive from the case alone.
    ``truth`` is the truth the trace is judged against (``case_truth``)."""

    tier: str
    width: int
    height: int
    truth: object
    max_deviation: float
    allowance: float
    bound: float
    rho: float
    round_radius: float
    threshold: float
    evidence_tolerance: float
    corners: list[dict]
    centres: np.ndarray
    radii: np.ndarray
    discs: object
    resolution_zone: object
    topology_zone: object
    palette_zone: object
    aliases: dict[int, int]
    folded: dict[int, int]
    stand_ins: dict[str, int]
    zoned: set[int]
    removed_by_design: set[int]
    topology_removed: object
    topology_far: object
    topology_isolation: object
    topology_tolerance: float
    palette_reasons: list[str]
    fold_mismatches: list
    rings: list[np.ndarray]
    truth_samples: np.ndarray
    truth_kept: np.ndarray
    colour_limits: dict[int, dict]
    area_region_limit: float
    ideal_nodes: int
    node_limit: int
    topology_truth: dict[str, dict] = field(default_factory=dict)

    @property
    def excluded_zone(self):
        """The resolution zone and the palette zone together."""
        return unary_union([self.resolution_zone, self.palette_zone])

    @property
    def geometric(self) -> bool:
        """Whether any truth boundary lies outside the discs and zones, so the
        truth distance, corner, kink and node assertions have something to hold."""
        return bool(self.truth_kept.any())

    @property
    def topological(self) -> bool:
        """Whether the topology assertion holds anything: some colour has an
        isolated structure, or an asserted count whose range reaches above 0."""
        return any(row["isolated"]
                   or any(row["asserted"][name] and row[f"{name}_range"][1]
                          for name in ("polygons", "holes"))
                   for row in self.topology_truth.values())


def _inside(geometry, points: np.ndarray) -> np.ndarray:
    if geometry is None or geometry.is_empty or not len(points):
        return np.zeros(len(points), dtype=bool)
    shapely.prepare(geometry)
    return shapely.contains_xy(geometry, points[:, 0], points[:, 1])


def _outside_discs(points: np.ndarray, centres: np.ndarray, radii: np.ndarray) -> np.ndarray:
    keep = np.ones(len(points), dtype=bool)
    for centre, radius in zip(centres, radii):
        keep &= np.hypot(*(points - centre).T) > radius
    return keep


def _local_topology(geometry, removed, far) -> dict:
    """Polygon and hole counts of ``geometry`` less ``removed``, with the band
    ``far`` less ``removed`` filled with the colour being counted and only the
    pieces and holes reaching beyond ``far`` counted."""
    if removed is None or removed.is_empty:
        polygons = [polygon for polygon in _polygons(geometry) if polygon.area > MIN_PIECE_AREA]
        return {"polygons": len(polygons),
                "holes": sum(1 for polygon in polygons for ring in polygon.interiors
                             if Polygon(ring).area > MIN_PIECE_AREA)}

    def beyond(shape) -> bool:
        return shape.difference(far).area > MIN_PIECE_AREA

    remaining = geometry.union(far).difference(removed)
    polygons = [polygon for polygon in _polygons(remaining) if beyond(polygon)]
    return {"polygons": len(polygons),
            "holes": sum(1 for polygon in polygons for ring in polygon.interiors
                         if beyond(Polygon(ring)))}


def _quarters(sweep: float) -> int:
    return max(1, math.ceil(sweep / (math.pi / 2) - 1e-9))


def _axis_extremes(points: np.ndarray, closed: bool) -> np.ndarray:
    """Which points of the polyline ``points`` (a closed one ending on its start)
    are axis extremes: where the sign of the x or y step changes, zero steps
    carrying the sign before them. An open polyline's two ends are never
    extremes; a closed one's repeated end is not flagged twice."""
    steps = np.diff(points, axis=0)
    count = len(points)
    flags = np.zeros(count, dtype=bool)
    if not len(steps):
        return flags
    for axis in (0, 1):
        signs = np.sign(np.where(np.abs(steps[:, axis]) > 1e-12, steps[:, axis], 0.0))
        nonzero = np.flatnonzero(signs)
        if not len(nonzero):
            continue
        if closed:
            # Carry the last non-zero sign round the seam.
            filled = signs.copy()
            last = signs[nonzero[-1]]
            for index in range(len(filled)):
                if filled[index] == 0:
                    filled[index] = last
                last = filled[index]
            # Point i (1..n-1) joins step i-1 and step i; point 0 joins the last
            # step and step 0.
            before = np.roll(filled, 1)
            flags[:-1] |= before != filled
        else:
            filled = signs.copy()
            last = 0.0
            for index in range(len(filled)):
                if filled[index] == 0:
                    filled[index] = last
                last = filled[index]
            change = (filled[:-1] != 0) & (filled[1:] != 0) & (filled[:-1] != filled[1:])
            flags[1:-1] |= change
    return flags


def _run_nodes(run, outside: np.ndarray) -> tuple[int, int]:
    """For one ideal run and its points ``outside`` the zone: the ideal interior
    nodes that can lie on its parts outside the zone (``zone_ideal_nodes``), and
    the interior nodes the fitter's fewest-piece route may need there
    (``zone_node_limit``): per part, the larger of its ideal nodes and its axis
    extremes, plus one per part end cut by the zone."""
    whole = _quarters(run.sweep[-1]) if run.conic else 1
    interior = whole if run.closed else whole - 1
    if not run.conic and not run.closed:
        interior = 0
    if not outside.any():
        return 0, 0
    extremes = _axis_extremes(run.points, run.closed)
    if outside.all():
        count = int(extremes[:-1].sum()) if run.closed else int(extremes.sum())
        return interior, max(interior, count)
    spacing = run.sweep[-1] / whole if run.conic else math.inf
    flags, sweep, marks = outside, run.sweep, extremes
    if run.closed:
        # Drop the repeated end and start the walk inside the zone, so no part
        # outside it wraps round the start.
        flags, sweep, marks = outside[:-1], run.sweep[:-1], extremes[:-1]
        total = run.sweep[-1]
        first = int(np.flatnonzero(~flags)[0])
        order = np.roll(np.arange(len(flags)), -first)
        flags, marks = flags[order], marks[order]
        sweep = np.where(order >= first, sweep[order] - sweep[first],
                         sweep[order] + total - sweep[first])
    count, limit = 0, 0
    index, last = 0, len(flags) - 1
    while index <= last:
        if not flags[index]:
            index += 1
            continue
        end = index
        while end + 1 <= last and flags[end + 1]:
            end += 1
        part = sweep[end] - sweep[index]
        steps = math.floor(part / spacing + 1e-9) if math.isfinite(spacing) else 0
        # Nodes spaced ``spacing`` apart: from a vertex end at most ``steps`` fall
        # on the part; with both ends cut, at any phase, at most ``steps + 1``.
        starts_run = not run.closed and index == 0
        ends_run = not run.closed and end == last
        ideal = (steps if starts_run or ends_run else steps + 1) if (run.conic or run.closed) \
            else 0
        count += ideal
        cuts = (0 if starts_run else 1) + (0 if ends_run else 1)
        limit += max(ideal, int(marks[index:end + 1].sum())) + cuts
        index = end + 1
    return min(count, interior), limit


def _vertices_outside(truth, zone, radii: dict | None, default_radius: float) -> int:
    vertices = np.asarray(truth.ideal_vertices, dtype=np.float64).reshape(-1, 2)
    if zone is None or zone.is_empty:
        return len(vertices)
    shapely.prepare(zone)
    count = 0
    for x, y in vertices:
        radius = (radii or {}).get((round(float(x), 6), round(float(y), 6)), default_radius)
        if not zone.contains(Point(x, y).buffer(max(radius, 1e-9), quad_segs=4)):
            count += 1
    return count


def zone_ideal_nodes(truth, zone, radii: dict | None = None, default_radius: float = 0.0) -> int:
    """The ideal node count (``truth.ideal_nodes``, the truth module docstring) of
    the boundary outside ``zone``: every vertex node of the ideal segmentation
    whose traced node can lie outside the zone (its disc of radius
    ``radii[(x, y)]``, or ``default_radius`` for a vertex with none, is not inside
    it), and for each part of an ideal run outside the zone, the interior nodes of
    the ideal segmentation (each run split into ``ceil(S / 90deg)`` equal pieces)
    that can fall on it: from a vertex end ``floor(S_i / s)``, with both ends in
    the zone ``floor(S_i / s) + 1`` at any phase (``s`` the piece sweep, ``S_i``
    the part's). Lines and cubics have no interior node. With an empty zone this
    is ``truth.ideal_nodes``."""
    count = _vertices_outside(truth, zone, radii, default_radius)
    for run in truth.ideal_runs:
        count += _run_nodes(run, ~_inside(zone, run.points))[0]
    return count


def zone_node_limit(truth, zone, radii: dict | None = None, default_radius: float = 0.0) -> int:
    """The ``nodes`` assertion's limit (module docstring): the vertex nodes
    ``zone_ideal_nodes`` counts, and on each part of an ideal run outside the
    zone the larger of its ideal interior nodes and its axis extremes, plus one
    node per part end the zone cuts."""
    count = _vertices_outside(truth, zone, radii, default_radius)
    for run in truth.ideal_runs:
        count += _run_nodes(run, ~_inside(zone, run.points))[1]
    return count


def _structures(geometry) -> list[dict]:
    """Every piece and hole of ``geometry``: its kind, its region (a piece's shell
    filled, a hole's ring filled), its rings, and a representative point."""
    rows = []
    for polygon in _polygons(geometry):
        if polygon.area <= MIN_PIECE_AREA:
            continue
        rows.append({"kind": "piece", "region": Polygon(polygon.exterior),
                     "rings": MultiLineString([LineString(polygon.exterior.coords)]
                                              + [LineString(ring.coords)
                                                 for ring in polygon.interiors]),
                     "point": polygon.representative_point()})
        for ring in polygon.interiors:
            hole = Polygon(ring)
            if hole.area > MIN_PIECE_AREA:
                rows.append({"kind": "hole", "region": hole,
                             "rings": MultiLineString([LineString(ring.coords)]),
                             "point": hole.representative_point()})
    return rows


def _isolated(structure: dict, near) -> bool:
    """Whether no ring of ``structure`` meets ``near``."""
    return near is None or near.is_empty or not structure["rings"].intersects(near)


def _topology_row(geometry, removed, bands) -> dict:
    """The truth's counts with the traced band, the range of each count over every
    band width, which counts are asserted, and the isolated structures
    (``topology``)."""
    rows = [_local_topology(geometry, removed, band) for band in bands]
    traced = rows[TOPOLOGY_BAND_MULTIPLES.index(TOPOLOGY_BAND_TRACED)]
    result = dict(traced)
    result["asserted"] = {}
    for name in ("polygons", "holes"):
        values = [row[name] for row in rows]
        result[f"{name}_range"] = [min(values), max(values)]
        # Asserted only where deleting one structure counted at the traced band
        # takes the count below the range.
        result["asserted"][name] = min(values) == traced[name]
    result["isolated"] = [row for row in _structures(geometry) if _isolated(row, bands[-1])]
    return result


def case_bounds(case, tier: str, truth=None, run=None) -> CaseBounds:
    """The bounds of ``case`` traced in ``tier`` (module docstring). ``truth`` is
    the scene's truth (``truth.scene_truth(case.scene)``), computed when omitted;
    the bounds judge against ``case_truth``'s truth derived from it, with the
    palette stage's folds read from ``run`` (the trace being judged) in the rgb
    tier."""
    scene = case.scene
    truth, design = case_truth(case, tier, truth, run)
    width, height = scene.width, scene.height
    lengths = resolved_lengths(width, height)
    max_deviation = float(lengths.max_deviation)
    allowance = float(lengths.corner_sharpening_allowance)
    bound = boundary_bound(max_deviation)
    threshold = float(TraceSettings().corner_threshold)
    round_radius = SHARPENING_RADIUS_FACTOR * (allowance + (1.0 + math.sqrt(2.0)) * Q_PX)
    rho = round_radius + math.sqrt(2.0) * Q_PX
    rings = truth_rings(truth)
    vertices = np.asarray([corner.point for corner in truth.corners],
                          dtype=np.float64).reshape(-1, 2)

    corners = corner_roles(truth, rings, threshold)
    for corner in corners:
        branches = vertex_branches(rings, corner["point"], vertices, width, height)
        corner["branches"] = len(branches)
        corner["radius_px"] = vertex_radius(corner["kind"], corner["point"], branches, bound,
                                            width, height)
    anchored = [corner for corner in corners if corner["role"] != "shallow"]
    discs = unary_union([Point(c["point"]).buffer(c["radius_px"] * _CIRCUMSCRIBE,
                                                  quad_segs=_BUFFER_SEGMENTS)
                         for c in anchored]) if anchored else Polygon()

    geometry = truth.colour_geometry
    # The opening trims every convex corner; at a shallow vertex (no disc) the
    # trimmed sliver lies well within D of it, and is no narrow feature.
    shallow = [Point(c["point"]).buffer(bound * _CIRCUMSCRIBE, quad_segs=_BUFFER_SEGMENTS)
               for c in corners if c["role"] == "shallow"]
    trims = unary_union([discs, *shallow]) if shallow else discs
    resolution = [piece for g in geometry.values()
                  for piece in narrow_parts(g, RESOLUTION_FLOOR_PX, trims)]
    topology = [piece for g in geometry.values()
                for piece in narrow_parts(g, TOPOLOGY_FLOOR_PX, trims)]
    resolution_zone = _buffer(unary_union(resolution), bound) if resolution else Polygon()
    topology_zone = _buffer(unary_union(topology), bound) if topology else Polygon()

    palette_pieces = [polygon for _, polygon in design["regions"]]
    palette_zone = (_buffer(unary_union(palette_pieces), bound) if palette_pieces
                    else Polygon())

    excluded = unary_union([resolution_zone, palette_zone])
    centres = np.asarray([c["point"] for c in anchored], dtype=np.float64).reshape(-1, 2)
    radii = np.asarray([c["radius_px"] for c in anchored], dtype=np.float64)
    in_zone = _inside(excluded, centres) if len(centres) else np.zeros(0, dtype=bool)
    for corner, zoned in zip(anchored, in_zone):
        if zoned:
            corner["narrow"] = True
            if corner["role"] == "required":
                corner["role"] = "optional"

    # A traced vertex node lies within R(c) of an anchored vertex, and within D of
    # any other point of the truth.
    vertex_radii = {(round(c["point"][0], 6), round(c["point"][1], 6)): c["radius_px"]
                    for c in anchored}
    samples = _boundary_samples(rings, width, height)
    kept = _outside_discs(samples, centres, radii) & ~_inside(excluded, samples)

    # Area: E(c) per colour.
    colour_limits = {}
    regions = []
    removed_topology = unary_union([discs, topology_zone, palette_zone])
    bands = ([_buffer(removed_topology, k * bound) for k in TOPOLOGY_BAND_MULTIPLES]
             if not removed_topology.is_empty else [None] * len(TOPOLOGY_BAND_MULTIPLES))
    far_topology = bands[TOPOLOGY_BAND_MULTIPLES.index(TOPOLOGY_BAND_TRACED)]
    evidence_tolerance = (max(max_deviation, allowance) + 0.5 + EVIDENCE_OFFSET_PX[tier]
                          + DISTANCE_ERROR_BOUND)
    # A trace lies within D of the truth, and within e of the evidence where the
    # truth distance does not hold it (a feature below the resolution floor).
    topology_tolerance = max(bound, evidence_tolerance)
    isolation = (_buffer(removed_topology,
                         TOPOLOGY_BAND_MULTIPLES[-1] * bound + topology_tolerance)
                 if not removed_topology.is_empty else None)
    topology_truth = {}
    region_floored = {index for index, g in geometry.items()
                      if _polygons(g) and all(any(polygon.equals(floored)
                                                  for _, floored in design["regions"])
                                              for polygon in _polygons(g)
                                              if polygon.area > MIN_PIECE_AREA)}
    for index, g in sorted(geometry.items()):
        lines = off_frame_lines(_geometry_rings(g), width, height)
        tube = _buffer(lines, bound) if not lines.is_empty else Polygon()
        on_boundary = [c for c in anchored
                       if not lines.is_empty
                       and lines.distance(Point(c["point"])) <= _VERTEX_MATCH_PX]
        parts = [tube] + [Point(c["point"]).buffer(c["radius_px"] * _CIRCUMSCRIBE,
                                                  quad_segs=_BUFFER_SEGMENTS)
                          for c in on_boundary]
        if not palette_zone.is_empty:
            parts.append(palette_zone.intersection(_buffer(g, bound)))
        region = unary_union(parts)
        regions.append(region)
        limit = float(region.area)
        truth_area = float(g.area)
        colour_limits[index] = {"truth_area_px": truth_area, "limit_px": limit,
                                "asserted": index not in design["zoned"] and limit < truth_area}
        if index not in design["zoned"]:
            topology_truth[str(index)] = _topology_row(g, removed_topology, bands)
    # GEOS raises floating-point divide or invalid flags inside some unions of
    # these tubes (numpy reports them as warnings); the union's area is checked
    # to be a finite value no larger than the sum of its parts.
    with np.errstate(divide="ignore", invalid="ignore"):
        area_region_limit = float(unary_union(regions).area) if regions else 0.0
    if not (math.isfinite(area_region_limit)
            and area_region_limit <= sum(region.area for region in regions) + 1e-6):
        raise ArithmeticError(f"area bound union of {case.case_id} is {area_region_limit}")

    return CaseBounds(
        tier=tier, width=width, height=height, truth=truth, max_deviation=max_deviation,
        allowance=allowance, bound=bound, rho=rho, round_radius=round_radius,
        threshold=threshold,
        evidence_tolerance=evidence_tolerance,
        corners=corners, centres=centres, radii=radii, discs=discs,
        resolution_zone=resolution_zone, topology_zone=topology_zone,
        palette_zone=palette_zone, aliases=dict(design["aliases"]),
        folded=dict(design["folded"]), stand_ins=dict(design["stand_ins"]),
        zoned=set(design["zoned"]),
        removed_by_design=set(design["folded"]) | set(design["zoned"]) | region_floored,
        topology_removed=removed_topology, topology_far=far_topology,
        topology_isolation=isolation, topology_tolerance=topology_tolerance,
        palette_reasons=list(design["reasons"]), fold_mismatches=list(design["fold_mismatches"]),
        rings=rings, truth_samples=samples,
        truth_kept=kept, colour_limits=colour_limits, area_region_limit=area_region_limit,
        ideal_nodes=zone_ideal_nodes(truth, excluded, vertex_radii, bound),
        node_limit=zone_node_limit(truth, excluded, vertex_radii, bound),
        topology_truth=topology_truth)


# --- raster evidence ------------------------------------------------------------------------------


def label_geometry(labels: np.ndarray) -> dict[int, object]:
    """Each label's pixels as exact pixel-edge polygons (y down, one unit per
    pixel): the union of its row runs."""
    labels = np.asarray(labels)
    height, width = labels.shape
    result = {}
    for value in np.unique(labels):
        mask = labels == value
        padded = np.zeros((height, width + 2), dtype=np.int8)
        padded[:, 1:-1] = mask
        change = np.diff(padded, axis=1)
        rows, starts = np.nonzero(change == 1)
        _, ends = np.nonzero(change == -1)
        boxes = shapely.box(starts, rows, ends, rows + 1)
        result[int(value)] = shapely.union_all(boxes)
    return result


def evidence_regions(labels: np.ndarray, geometry: dict[int, object]) -> list[dict]:
    """Every four-connected region of every label: its label, pixel count and the
    polygon of its label's geometry that holds it."""
    regions = []
    for value, shape in sorted(geometry.items()):
        mask = labels == value
        component, count = ndimage.label(mask)  # four-connected by default
        if not count:
            continue
        flat = component.ravel()
        sizes = np.bincount(flat)[1:]
        # The first pixel of each region, in raster order.
        order = np.flatnonzero(flat)
        firsts = np.full(count, -1, dtype=np.int64)
        unique, first_index = np.unique(flat[order] - 1, return_index=True)
        firsts[unique] = order[first_index]
        polygons = _polygons(shape)
        tree = shapely.STRtree(polygons)
        width = labels.shape[1]
        for region, (first, size) in enumerate(zip(firsts, sizes)):
            y, x = divmod(int(first), width)
            centre = Point(x + 0.5, y + 0.5)
            hits = [polygons[i] for i in tree.query(centre, predicate="within")]
            regions.append({"label": int(value), "pixels": int(size), "pixel": [x, y],
                            "polygon": hits[0] if hits else None})
    return regions


# --- one trace ------------------------------------------------------------------------------------


def _traced_layers(traced: dict, colour_map: dict[str, int]):
    """The traced layers as geometry per scene colour and per fill, the rings of the
    layers ``colour_map`` maps, and the rings per fill."""
    by_colour: dict[int, list] = {}
    by_fill: dict[str, list] = {}
    rings = []
    rings_by_fill: dict[str, list] = {}
    for layer in traced["layers"]:
        geometry = [path_geometry(path) for path in layer["paths"]]
        by_fill.setdefault(layer["fill"], []).extend(geometry)
        layer_rings = [ring for path in layer["paths"] for ring in path["rings"]]
        rings_by_fill.setdefault(layer["fill"], []).extend(layer_rings)
        index = colour_map.get(layer["fill"])
        if index is None:
            continue
        by_colour.setdefault(index, []).extend(geometry)
        rings.extend(layer_rings)
    return ({index: unary_union(parts) for index, parts in by_colour.items()},
            {fill: unary_union(parts) for fill, parts in by_fill.items()}, rings, rings_by_fill)


def _boundary_nodes(traced: dict) -> list[tuple[float, float, bool]]:
    """Unique written node positions off the four canvas corners, with whether
    any layer writes the node as a cusp (``metrics.measure``'s node rule)."""
    width, height = traced["width"], traced["height"]
    nodes: dict[tuple[float, float], bool] = {}
    for layer in traced["layers"]:
        for path in layer["paths"]:
            for x, y, letter in path["nodes"]:
                key = (round(x, _NODE_DECIMALS), round(y, _NODE_DECIMALS))
                nodes[key] = nodes.get(key, False) or letter == "c"
    corners = {(0.0, 0.0), (round(width, _NODE_DECIMALS), 0.0),
               (round(width, _NODE_DECIMALS), round(height, _NODE_DECIMALS)),
               (0.0, round(height, _NODE_DECIMALS))}
    return [(x, y, cusp) for (x, y), cusp in nodes.items() if (x, y) not in corners]


def _directed(mine: np.ndarray, other: np.ndarray) -> np.ndarray:
    """Each of ``mine``'s samples' distance to the nearest of ``other``'s."""
    if not len(mine):
        return np.empty(0)
    if not len(other):
        return np.full(len(mine), math.inf)
    return cKDTree(other).query(mine)[0]


def evidence_distance(geometry: dict[int, object], palette_hex: list[str],
                      traced_rings_by_fill: dict[str, list], tolerance: float, width: float,
                      height: float) -> dict:
    """Per evidence label, the boundary distance (off the frame) between the traced
    layer of the label's fill and the label's pixels, both ways: the worst, where,
    and the length of boundary farther than ``tolerance``."""
    worst, point, fill = 0.0, None, None
    beyond = 0
    directed = {"trace_to_evidence_px": 0.0, "evidence_to_trace_px": 0.0}
    for index, hex_code in enumerate(palette_hex):
        hex_code = hex_code.upper()
        shape = geometry.get(index)
        evidence = (_boundary_samples(_geometry_rings(shape), width, height)
                    if shape is not None else np.empty((0, 2)))
        traced = _boundary_samples(traced_rings_by_fill.get(hex_code, []), width, height)
        for name, mine, other in (("trace_to_evidence_px", traced, evidence),
                                  ("evidence_to_trace_px", evidence, traced)):
            distances = _directed(mine, other)
            if not len(distances):
                continue
            beyond += int((distances > tolerance).sum())
            directed[name] = max(directed[name], float(distances.max()))
            if distances.max() > worst:
                worst = float(distances.max())
                point = [float(value) for value in mine[int(distances.argmax())]]
                fill = hex_code
    return {"distance_px": worst, "worst_point": point, "fill": fill,
            "length_beyond_px": beyond * SAMPLE_SPACING, **directed}


def thin_regions(labels: np.ndarray, palette_hex: list[str], traced_by_fill: dict,
                 exempt=None) -> list[dict]:
    """Every *thin* four-connected evidence region: one most of whose pixels lie in
    no 2 x 2 block of its own pixels (narrower than the resolution floor along
    most of it), with the fraction of those thin pixels' centres its label's
    traced layer covers (``recall``); a region whose thin pixels all lie in
    ``exempt`` (the vertex discs, where a tip or a corner contact leaves a pixel
    no trace is held to) is left out."""
    labels = np.asarray(labels)
    rows = []
    for value in np.unique(labels):
        mask = labels == value
        block = mask[:-1, :-1] & mask[1:, :-1] & mask[:-1, 1:] & mask[1:, 1:]
        covered = np.zeros_like(mask)
        covered[:-1, :-1] |= block
        covered[1:, :-1] |= block
        covered[:-1, 1:] |= block
        covered[1:, 1:] |= block
        component, count = ndimage.label(mask)
        if not count:
            continue
        sizes = np.bincount(component.ravel(), minlength=count + 1)
        thin_sizes = np.bincount(component[mask & ~covered], minlength=count + 1)
        slices = ndimage.find_objects(component)
        traced = traced_by_fill.get(palette_hex[int(value)].upper(), Polygon())
        if not traced.is_empty:
            shapely.prepare(traced)
        for region in np.flatnonzero(2 * thin_sizes[1:] > sizes[1:]) + 1:
            rows_slice, columns_slice = slices[region - 1]
            local = ((component[rows_slice, columns_slice] == region)
                     & ~covered[rows_slice, columns_slice])
            ys, xs = np.nonzero(local)
            ys, xs = ys + rows_slice.start, xs + columns_slice.start
            if exempt is not None and not exempt.is_empty \
                    and shapely.contains_xy(exempt, xs + 0.5, ys + 0.5).all():
                continue
            order = np.lexsort((xs, ys))
            inside = (shapely.contains_xy(traced, xs + 0.5, ys + 0.5) if not traced.is_empty
                      else np.zeros(len(xs), dtype=bool))
            rows.append({"label": int(value), "pixels": int(sizes[region]),
                         "pixel": [int(xs[order[0]]), int(ys[order[0]])],
                         "recall": float(inside.mean())})
    return rows


def evidence_check(labels: np.ndarray, palette_hex: list[str], traced_by_fill: dict,
                   traced_rings_by_fill: dict, tolerance: float, width: float,
                   height: float, exempt=None) -> dict:
    """Per evidence label: the four-connected regions with no traced area of their
    colour within ``tolerance``, the symmetric difference of the traced layer and
    the label's pixels against the area of the ``tolerance``-tube round the label's
    boundary (off the frame), the boundary distance (``evidence_distance``), and
    the thin regions (``thin_regions``, outside ``exempt``) whose traced layer
    covers less than ``THIN_RECALL`` of their pixel centres."""
    geometry = label_geometry(labels)
    regions = evidence_regions(labels, geometry)
    colours = {}
    for index, hex_code in enumerate(palette_hex):
        shape = geometry.get(index, Polygon())
        traced = traced_by_fill.get(hex_code.upper(), Polygon())
        if shape.is_empty and traced.is_empty:
            continue
        lines = off_frame_lines(_geometry_rings(shape), width, height)
        limit = float(_buffer(lines, tolerance).area) if not lines.is_empty else 0.0
        difference = float(traced.symmetric_difference(shape).area)
        colours[str(index)] = {"hex": hex_code.upper(), "evidence_area_px": float(shape.area),
                               "traced_area_px": float(traced.area),
                               "symmetric_difference_px": difference, "limit_px": limit}
    missing = []
    for region in regions:
        traced = traced_by_fill.get(palette_hex[region["label"]].upper(), Polygon())
        polygon = region["polygon"]
        near = (polygon.buffer(tolerance * _CIRCUMSCRIBE, quad_segs=_BUFFER_SEGMENTS)
                if polygon is not None else None)
        overlap = traced.intersection(near).area if near is not None and not traced.is_empty \
            else 0.0
        if overlap <= MIN_PIECE_AREA:
            missing.append({"label": region["label"], "pixels": region["pixels"],
                            "pixel": region["pixel"]})
    lost = {(row["label"], tuple(row["pixel"])) for row in missing}
    thin = thin_regions(labels, palette_hex, traced_by_fill, exempt)
    misplaced = [row for row in thin
                 if row["recall"] < THIN_RECALL and (row["label"], tuple(row["pixel"])) not in lost]
    return {"tolerance_px": tolerance, "regions": len(regions), "missing_regions": missing,
            "thin_regions": len(thin), "misplaced_thin_regions": misplaced,
            "colours": colours, "present_labels": sorted(int(k) for k in geometry),
            "distance": evidence_distance(geometry, palette_hex, traced_rings_by_fill,
                                          tolerance, width, height)}


def matched_colour_map(bounds: CaseBounds, run) -> dict[str, int]:
    """``run.colour_map`` with each merged colour read as the colour the truth keeps
    for its pair (``merge_aliases``): the inferred colours that match a scene
    colour."""
    return {str(fill).upper(): bounds.aliases.get(index, index)
            for fill, index in run.colour_map.items()}


def colour_map(bounds: CaseBounds, run) -> dict[str, int]:
    """The scene colour each traced fill is judged as geometrically:
    ``matched_colour_map`` and every stand-in (``observed_palette``)."""
    return {**matched_colour_map(bounds, run), **bounds.stand_ins}


def _same_region(first, second, tolerance: float) -> bool:
    """Whether each region lies within ``tolerance`` of the other."""
    return (first.difference(_buffer(second, tolerance)).area <= _SAME_REGION_AREA
            and second.difference(_buffer(first, tolerance)).area <= _SAME_REGION_AREA)


def topology_colour_verdict(traced_geometry, truth_row: dict, bounds: "CaseBounds") -> dict:
    """One colour's ``topology`` verdict for ``traced_geometry`` against the
    truth's row (``_topology_row``): each asserted count within its range over the
    band widths; each isolated true structure matching exactly one traced structure
    of its kind within the topology tolerance; and no traced structure whose rings
    clear the removed region by ``3 D`` plus that tolerance (so its true
    counterpart would be isolated) left unmatched. ``error`` sums how far each
    asserted count lies outside its range, the unmatched isolated structures and
    the unexpected traced ones."""
    traced = _local_topology(traced_geometry, bounds.topology_removed, bounds.topology_far)
    error = 0
    for name in ("polygons", "holes"):
        if not truth_row["asserted"][name]:
            continue
        low, high = truth_row[f"{name}_range"]
        error += max(0, low - traced[name], traced[name] - high)
    mine = _structures(traced_geometry)
    tolerance = bounds.topology_tolerance
    matched = set()
    missing = []
    for structure in truth_row["isolated"]:
        near = _buffer(structure["region"], tolerance)
        hits = [k for k, other in enumerate(mine)
                if other["kind"] == structure["kind"] and other["region"].intersects(near)
                and _same_region(structure["region"], other["region"], tolerance)]
        if len(hits) != 1:
            missing.append(structure)
        matched.update(hits)
    unexpected = [other for k, other in enumerate(mine)
                  if k not in matched and _isolated(other, bounds.topology_isolation)]
    error += len(missing) + len(unexpected)

    def located(rows):
        return [[row["kind"], float(row["point"].x), float(row["point"].y)] for row in rows]

    return {"traced": traced, "truth": {name: truth_row[name] for name in ("polygons", "holes")},
            "ranges": {name: truth_row[f"{name}_range"] for name in ("polygons", "holes")},
            "asserted": dict(truth_row["asserted"]), "isolated": len(truth_row["isolated"]),
            "missing": located(missing), "unexpected": located(unexpected), "error": error}


def topology_mutations(bounds: "CaseBounds") -> list[dict]:
    """The topology assertion judged, per colour, on the truth as its own trace
    (``"truth"``, which must pass) and on every single deletion it claims to
    catch (each must fail): every isolated structure, and every structure whose
    deletion changes an asserted count at the traced band (a hole filled with the
    colour, a piece dropped)."""
    rows = []
    for colour, truth_row in sorted(bounds.topology_truth.items()):
        geometry = bounds.truth.colour_geometry[int(colour)]
        verdict = topology_colour_verdict(geometry, truth_row, bounds)
        rows.append({"colour": colour, "mutation": "truth", "error": verdict["error"]})
        isolated = {(row["kind"], round(row["point"].x, 6), round(row["point"].y, 6))
                    for row in truth_row["isolated"]}
        pieces = [p for p in _polygons(geometry) if p.area > MIN_PIECE_AREA]
        for structure in _structures(geometry):
            if structure["kind"] == "hole":
                mutated = geometry.union(structure["region"])
            else:
                piece = next(p for p in pieces
                             if p.representative_point().equals(structure["point"]))
                mutated = geometry.difference(piece)
            key = (structure["kind"], round(structure["point"].x, 6),
                   round(structure["point"].y, 6))
            counts = _local_topology(mutated, bounds.topology_removed, bounds.topology_far)
            changes = any(truth_row["asserted"][name] and counts[name] != truth_row[name]
                          for name in ("polygons", "holes"))
            if key not in isolated and not changes:
                continue
            verdict = topology_colour_verdict(mutated, truth_row, bounds)
            rows.append({"colour": colour,
                         "mutation": f"delete the {structure['kind']} at "
                                     f"({structure['point'].x:.2f}, {structure['point'].y:.2f})",
                         "error": verdict["error"]})
    return rows


def evaluate(case, tier: str, run, bounds: CaseBounds, *, measured: dict | None = None,
             raster_colours: set[int] | None = None) -> dict:
    """Measure ``run`` (a ``harness.TraceRun`` of ``case`` in ``tier``) against
    ``bounds``: every number ``check`` and the sweep summaries read.
    ``raster_colours`` (the scene colours of the hard-edged label render, which
    are the colours that dominate some pixel) limits the rgb tier's demand that
    a scene colour survive the palette stage to colours the raster carries.
    ``measured`` is ``metrics.measure`` of the run against ``bounds.truth`` with
    ``colour_map(bounds, run)``, when the caller already has it."""
    scene = case.scene
    truth = bounds.truth
    mapping = colour_map(bounds, run)
    if measured is None:
        # A negative radius and distance make every true vertex a miss and every
        # cusp a kink, so the result lists each one's nearest distance.
        measured = measure(run.edge_svg, truth, scene.palette, corner_radius=-1.0,
                           smooth_min_distance=-1.0, colour_map=mapping)
    traced = parse_traced_svg(run.edge_svg)
    width, height = traced["width"], traced["height"]
    traced_by_colour, traced_by_fill, traced_rings, rings_by_fill = _traced_layers(traced, mapping)
    excluded = bounds.excluded_zone

    # Vertices: the nearest cusp to each.
    nodes = _boundary_nodes(traced)
    cusp_points = np.asarray([(x, y) for x, y, cusp in nodes if cusp],
                             dtype=np.float64).reshape(-1, 2)
    cusp_tree = cKDTree(cusp_points) if len(cusp_points) else None
    corners = []
    for corner in bounds.corners:
        row = dict(corner)
        row["nearest_cusp_px"] = (float(cusp_tree.query(corner["point"])[0])
                                  if cusp_tree is not None else math.inf)
        row["found"] = row["nearest_cusp_px"] <= row["radius_px"]
        corners.append(row)

    # Boundary distance to the truth outside the discs and zones.
    traced_samples = _boundary_samples(traced_rings, width, height)
    truth_samples = bounds.truth_samples
    worst, point = 0.0, None
    traced_kept = (_outside_discs(traced_samples, bounds.centres, bounds.radii)
                   & ~_inside(excluded, traced_samples))
    pooled = []
    directed = {"trace_to_truth_px": 0.0, "truth_to_trace_px": 0.0}
    for name, mine, other in (("trace_to_truth_px", traced_samples[traced_kept], truth_samples),
                              ("truth_to_trace_px", truth_samples[bounds.truth_kept],
                               traced_samples)):
        distances = _directed(mine, other)
        if not len(distances):
            continue
        pooled.append(distances)
        directed[name] = float(distances.max())
        if distances.max() > worst:
            worst = float(distances.max())
            point = [float(value) for value in mine[int(distances.argmax())]]
    everything = np.concatenate(pooled) if pooled else np.empty(0)
    mean = float(everything.mean()) if len(everything) else 0.0
    local = {"hausdorff_px": worst, "mean_px": mean, "worst_point": point,
             "truth_samples": int(bounds.truth_kept.sum()),
             "traced_samples": int(traced_kept.sum()),
             "length_beyond_px": int((everything > bounds.bound).sum()) * SAMPLE_SPACING,
             **directed}

    # Cusps: kinks unless near an anchored vertex, in a zone or on a sharpenable round.
    in_zone = _inside(excluded, cusp_points)
    cusps = []
    anchored = [c for c in corners if c["role"] != "shallow"]
    for (x, y), zoned in zip(cusp_points, in_zone):
        near_corner = any(math.hypot(x - c["point"][0], y - c["point"][1]) <= c["radius_px"]
                          for c in anchored)
        turn = round_turn_near(bounds.rings, (x, y), bounds.round_radius,
                               math.sqrt(2.0) * Q_PX)
        cusps.append({"point": [float(x), float(y)], "near_corner": near_corner,
                      "in_zone": bool(zoned), "truth_turn_deg": turn,
                      "kink": not near_corner and not zoned and turn < bounds.threshold})

    node_points = np.asarray([(x, y) for x, y, _ in nodes], dtype=np.float64).reshape(-1, 2)
    outside_flags = ~_inside(excluded, node_points)
    nodes_outside = int(outside_flags.sum())

    # Topology outside the discs and zones, every colour of the truth or the trace.
    absent = {"polygons": 0, "holes": 0, "polygons_range": [0, 0], "holes_range": [0, 0],
              "asserted": {"polygons": True, "holes": True}, "isolated": []}
    topology = {}
    for index in sorted(set(truth.colour_geometry) | set(traced_by_colour)):
        if index in bounds.zoned:
            continue
        topology[str(index)] = topology_colour_verdict(
            traced_by_colour.get(index, Polygon()),
            bounds.topology_truth.get(str(index), absent), bounds)

    # Area per colour.
    colour_area = {}
    for index, limits in bounds.colour_limits.items():
        mine = traced_by_colour.get(index, Polygon())
        true = truth.colour_geometry[index]
        colour_area[str(index)] = {**limits,
                                   "symmetric_difference_px":
                                       float(mine.symmetric_difference(true).area)}

    result = {
        "case_id": case.case_id, "tier": tier, "min_feature_px": float(case.min_feature_px),
        "width": scene.width, "height": scene.height,
        "max_deviation_px": bounds.max_deviation,
        "audit_max_deviation_px": float(run.audit["lengths"]["px"]["max_deviation"]),
        "allowance_px": bounds.allowance, "rho_px": bounds.rho,
        "round_radius_px": bounds.round_radius,
        "bound_px": bounds.bound, "threshold_deg": bounds.threshold,
        "geometric": bounds.geometric, "topological": bounds.topological,
        "palette_reasons": list(bounds.palette_reasons), "folded": sorted(bounds.folded),
        "merged": sorted(bounds.aliases),
        "local_hausdorff": local, "hausdorff_px": measured["boundary"]["hausdorff_px"],
        "mean_px": measured["boundary"]["mean_px"],
        "corners": corners, "cusps": cusps,
        "nodes": measured["nodes"]["boundary"], "nodes_outside_zones": nodes_outside,
        "nodes_outside_points": [[float(x), float(y)] for x, y in node_points[outside_flags]],
        "ideal_nodes": int(bounds.ideal_nodes), "node_limit": int(bounds.node_limit),
        "scene_ideal_nodes": int(truth.ideal_nodes),
        "topology": topology,
        # A stand-in is judged as its colour geometrically but is still no match.
        "unmatched_layers": measured["area"]["unmatched_layers"]
        + [{"fill": fill, "stands_in_for": colour} for fill, colour in bounds.stand_ins.items()
           if fill in traced_by_fill],
        "misassigned_px": float(measured["area"]["misassigned_px"]),
        "area_limit_px": bounds.area_region_limit, "colour_area": colour_area,
        "truth_length_px": float(measured["boundary"]["truth_samples"]
                                 * measured["boundary"]["sample_spacing_px"] / 2.0),
        "palette_matches": run.palette_matches, "inferred_colours": len(run.palette),
    }
    with np.load(run.subject_dir / "segmentation.npz") as bundle:
        labels = np.asarray(bundle["labels"])
    palette_hex = [str(row["hex"]).upper() for row in run.palette]
    result["evidence"] = evidence_check(labels, palette_hex, traced_by_fill, rings_by_fill,
                                        bounds.evidence_tolerance, width, height,
                                        exempt=bounds.discs)
    matched = matched_colour_map(bounds, run)
    present = {matched[palette_hex[label]]
               for label in result["evidence"]["present_labels"]
               if palette_hex[label] in matched}
    raster = (None if raster_colours is None
              else {bounds.aliases.get(index, index) for index in raster_colours})
    required = [index for index, limits in bounds.colour_limits.items()
                if not limits["asserted"] and index not in bounds.removed_by_design
                and (raster is None or index in raster)]
    result["evidence"]["required_scene_colours"] = required
    result["evidence"]["absent_scene_colours"] = (
        [index for index in required if index not in present] if tier == "rgb" else [])
    result["evidence"]["fold_target_mismatches"] = [list(row) for row in bounds.fold_mismatches]
    return result


# --- verdicts -------------------------------------------------------------------------------------


def applicable(case, tier: str) -> tuple[str, ...]:
    """The assertions a (case, tier) makes (module docstring). ``available`` says
    whether one has anything to hold once the case's bounds are known."""
    names = ["hausdorff", "corners", "kinks", "nodes", "topology", "area", "evidence"]
    if case.scene.params.get("expect") is not None:
        names.append("palette")
    return tuple(names)


def available(name: str, result: dict) -> str | None:
    """None when assertion ``name`` holds something for ``result``; otherwise why not."""
    if name in ("corners", "kinks", "nodes") and not result["geometric"]:
        return ("every truth boundary lies in a vertex disc or a narrow or palette zone "
                "(features below the resolution floor or removed by the palette stage)")
    if name == "topology" and not result["topological"]:
        return "every colour lies in a vertex disc or a narrow or palette zone"
    return None


def _points(points, limit: int = 5) -> str:
    text = ", ".join(f"({x:.2f}, {y:.2f})" for x, y in points[:limit])
    return text + (f" (+{len(points) - limit} more)" if len(points) > limit else "")


def _area_problems(result: dict) -> tuple[list[str], float]:
    problems, excess = [], 0.0
    for colour, row in result["colour_area"].items():
        if row["asserted"] and row["symmetric_difference_px"] > row["limit_px"]:
            problems.append(f"colour {colour}: symmetric difference "
                            f"{row['symmetric_difference_px']:.1f} px^2 > {row['limit_px']:.1f}")
            excess += row["symmetric_difference_px"] - row["limit_px"]
    if result["misassigned_px"] > result["area_limit_px"]:
        problems.append(f"misassigned {result['misassigned_px']:.1f} px^2 > "
                        f"{result['area_limit_px']:.1f}")
        excess += result["misassigned_px"] - result["area_limit_px"]
    return problems, excess


def _evidence_problems(result: dict) -> tuple[list[str], float]:
    evidence = result["evidence"]
    problems = [f"label {row['label']} region of {row['pixels']} px at {row['pixel']} "
                f"has no traced area within {evidence['tolerance_px']:.2f} px"
                for row in evidence["missing_regions"][:6]]
    if len(evidence["missing_regions"]) > 6:
        problems.append(f"(+{len(evidence['missing_regions']) - 6} more regions)")
    excess = 0.0
    for label, row in evidence["colours"].items():
        if row["symmetric_difference_px"] > row["limit_px"]:
            problems.append(f"label {label} ({row['hex']}): symmetric difference "
                            f"{row['symmetric_difference_px']:.1f} px^2 > {row['limit_px']:.1f}")
            excess += row["symmetric_difference_px"] - row["limit_px"]
    problems += [f"scene colour {index} is no inferred colour"
                 for index in evidence["absent_scene_colours"]]
    problems += [f"thin label {row['label']} region of {row['pixels']} px at {row['pixel']}: "
                 f"traced layer covers {100 * row['recall']:.0f}% of its pixel centres "
                 f"(< {100 * THIN_RECALL:.0f}%)"
                 for row in evidence["misplaced_thin_regions"][:6]]
    problems += [f"colour {colour} folded into {target}, not {replay} as the usage cutoff "
                 f"replay folds it, and is at least D wide"
                 for colour, target, replay in evidence["fold_target_mismatches"]]
    return problems, excess


def check(name: str, result: dict, case) -> tuple[bool, str]:
    """Whether ``result`` passes assertion ``name``, and what it measured."""
    bound = result["bound_px"]
    if name == "hausdorff":
        local = result["local_hausdorff"]
        distance = result["evidence"]["distance"]
        tolerance = result["evidence"]["tolerance_px"]
        passed = local["hausdorff_px"] <= bound and distance["distance_px"] <= tolerance
        return passed, (f"truth distance off the vertex discs and zones "
                        f"{local['hausdorff_px']:.3f} px (limit D {bound:.3f} px) at "
                        f"{local['worst_point']}, {local['length_beyond_px']:.2f} px beyond; "
                        f"evidence distance {distance['distance_px']:.3f} px (limit e "
                        f"{tolerance:.3f} px) at {distance['worst_point']} in "
                        f"{distance['fill']}, {distance['length_beyond_px']:.2f} px beyond")
    if name == "corners":
        misses = [c for c in result["corners"] if c["role"] == "required" and not c["found"]]
        return not misses, (f"{len(misses)} required vertices without a cusp within R: " + "; ".join(
            f"{c['kind']} turn {c['turn_deg']:.1f} at ({c['point'][0]:.2f}, {c['point'][1]:.2f}) "
            f"nearest cusp {c['nearest_cusp_px']:.2f} px > R {c['radius_px']:.2f} px"
            for c in misses[:6]))
    if name == "kinks":
        kinks = [cusp["point"] for cusp in result["cusps"] if cusp["kink"]]
        return not kinks, f"{len(kinks)} cusp nodes on smooth truth: {_points(kinks)}"
    if name == "nodes":
        limit = result["node_limit"]
        count = result["nodes_outside_zones"]
        return count <= limit, (f"{count} boundary nodes outside the zones (limit {limit}: "
                                f"{result['ideal_nodes']} ideal, with the axis extremes and "
                                f"zone cuts the fitter's route may add)")
    if name == "topology":
        wrong = {colour: row for colour, row in result["topology"].items() if row["error"]}
        unmatched = [row["fill"] for row in result["unmatched_layers"]]
        def located(rows):
            return ", ".join(f"{kind} at ({x:.1f}, {y:.1f})" for kind, x, y in rows[:4])

        return not wrong and not unmatched, "; ".join(
            [f"colour {colour}: traced {row['traced']}, truth ranges {row['ranges']} "
             f"(asserted {[n for n, a in row['asserted'].items() if a]})"
             + (f", isolated true {located(row['missing'])} unmatched" if row["missing"]
                else "")
             + (f", traced {located(row['unexpected'])} matching no isolated truth"
                if row["unexpected"] else "")
             for colour, row in wrong.items()]
            + ([f"unmatched layers {unmatched}"] if unmatched else []))
    if name == "area":
        problems, _ = _area_problems(result)
        asserted = [colour for colour, row in result["colour_area"].items() if row["asserted"]]
        return not problems, "; ".join(problems) or f"colours {asserted} within their bound"
    if name == "evidence":
        problems, _ = _evidence_problems(result)
        return not problems, "; ".join(problems)
    if name == "palette":
        return palette_outcome(case, result)
    raise KeyError(name)


def _palette_findings(case, result: dict) -> list[tuple[str, list]]:
    """Each ``palette`` problem as its text and its identity."""
    from vinylgen.fidelity.harness import MATCH_DELTA_E
    expect = case.scene.params["expect"]
    matched: dict[int, int] = {}
    for row in result["palette_matches"]:
        if row["matched"]:
            matched[row["truth_index"]] = matched.get(row["truth_index"], 0) + 1
    problems = []
    for first, second in expect.get("merge", []):
        count = matched.get(first, 0) + matched.get(second, 0)
        if count != 1:
            problems.append((f"merge {first}+{second}: {count} inferred colours",
                             ["merge", first, second, count]))
    for index in expect.get("survive", []):
        if matched.get(index, 0) != 1:
            problems.append((f"survive {index}: {matched.get(index, 0)} inferred colours",
                             ["survive", index, matched.get(index, 0)]))
    for index in expect.get("fold", []):
        if matched.get(index, 0):
            problems.append((f"fold {index}: {matched[index]} inferred colours",
                             ["fold", index, matched[index]]))
    unmatched = [row["hex"] for row in result["palette_matches"] if not row["matched"]]
    if unmatched:
        problems.append((f"unmatched inferred colours {unmatched} (beyond {MATCH_DELTA_E} dE)",
                         ["unmatched", *sorted(unmatched)]))
    return problems


def _palette_problems(case, result: dict) -> list[str]:
    return [text for text, _ in _palette_findings(case, result)]


def palette_outcome(case, result: dict) -> tuple[bool, str]:
    """``scene.params["expect"]``: every ``merge`` pair became exactly one inferred
    colour, every ``survive`` colour is exactly one inferred colour, every ``fold``
    colour (below the whole-colour usage cutoff, folded into a surviving colour) is
    no inferred colour, and no inferred colour is unmatched
    (``harness.MATCH_DELTA_E`` nearest-colour matching); with every inferred colour
    matching a surviving scene colour, a folded colour's pixels are necessarily
    surviving colours."""
    problems = _palette_problems(case, result)
    return not problems, "; ".join(problems)


# The scalars each assertion compares with its bound, all larger-is-worse: what an
# expected failure records and must not exceed (``measurements``). A name ending
# ``_px`` is a length in px, ``_px2`` an area in px^2; any other is a count.
MEASUREMENTS = {
    "hausdorff": ("truth_distance_px", "truth_length_beyond_px", "evidence_distance_px",
                  "evidence_length_beyond_px"),
    "corners": ("missed_vertices",),
    "kinks": ("kinks",),
    "nodes": ("nodes_outside_zones",),
    "topology": ("wrong_colours", "count_error", "unmatched_layers"),
    "area": ("colours_over", "excess_px2"),
    "evidence": ("missing_regions", "missing_pixels", "colours_over", "excess_px2",
                 "absent_scene_colours", "misplaced_thin_regions", "fold_target_mismatches"),
    "palette": ("problems",),
}
# The identities of what each assertion finds wrong (``identities``): what an
# expected failure records besides its measurements, so fixing one defect while
# introducing another of the same size is no longer the recorded failure. Each
# is a list of items, each a list of numbers and strings.
IDENTITIES = {
    "hausdorff": ("truth_worst_point", "evidence_worst_point"),
    "corners": ("missed_vertices",),
    "kinks": ("kinks",),
    "nodes": ("nodes_outside_zones",),
    "topology": ("wrong_counts", "missing_structures", "unexpected_structures",
                 "unmatched_layers"),
    "area": ("colours_over",),
    "evidence": ("missing_regions", "colours_over", "absent_scene_colours",
                 "misplaced_thin_regions", "fold_target_mismatches"),
    "palette": ("problems",),
}
# Numbers in an identity item match within this: its positions are node and
# vertex coordinates, which a deterministic trace reproduces to floating-point
# drift (``RATCHET_TOLERANCE``'s px margin); its counts and indices are whole.
IDENTITY_TOLERANCE = 1e-3
# Tolerances of the expected-failure ratchet. The pipeline is deterministic (the
# same code and inputs give the same SVG, and the measures are exact geometry on
# it), so a count may not grow at all; lengths and areas get a margin far below
# any change a trace makes (a node moves on a grid of whole and sub-pixel sample
# positions, so the smallest geometric change a trace makes is of order 0.01 px)
# and far above floating-point drift of the geometry on these canvases (coordinates
# up to 1536 px, relative error ~1e-15, so ~1e-12 px).
RATCHET_TOLERANCE = {"px": 1e-3, "px2": 1e-2, "count": 0.0}


def measurement_unit(key: str) -> str:
    if key.endswith("_px2"):
        return "px2"
    if key.endswith("_px"):
        return "px"
    return "count"


def measurements(name: str, result: dict, case) -> dict[str, float]:
    """The scalars assertion ``name`` compares with its bound (``MEASUREMENTS``)."""
    if name == "hausdorff":
        local = result["local_hausdorff"]
        distance = result["evidence"]["distance"]
        values = {"truth_distance_px": local["hausdorff_px"],
                  "truth_length_beyond_px": local["length_beyond_px"],
                  "evidence_distance_px": distance["distance_px"],
                  "evidence_length_beyond_px": distance["length_beyond_px"]}
    elif name == "corners":
        values = {"missed_vertices": sum(1 for c in result["corners"]
                                         if c["role"] == "required" and not c["found"])}
    elif name == "kinks":
        values = {"kinks": sum(1 for cusp in result["cusps"] if cusp["kink"])}
    elif name == "nodes":
        values = {"nodes_outside_zones": result["nodes_outside_zones"]}
    elif name == "topology":
        rows = result["topology"].values()
        values = {"wrong_colours": sum(1 for row in rows if row["error"]),
                  "count_error": sum(row["error"] for row in rows),
                  "unmatched_layers": len(result["unmatched_layers"])}
    elif name == "area":
        problems, excess = _area_problems(result)
        values = {"colours_over": len(problems), "excess_px2": excess}
    elif name == "evidence":
        evidence = result["evidence"]
        over = [row for row in evidence["colours"].values()
                if row["symmetric_difference_px"] > row["limit_px"]]
        values = {"missing_regions": len(evidence["missing_regions"]),
                  "missing_pixels": sum(row["pixels"] for row in evidence["missing_regions"]),
                  "colours_over": len(over),
                  "excess_px2": sum(row["symmetric_difference_px"] - row["limit_px"]
                                    for row in over),
                  "absent_scene_colours": len(evidence["absent_scene_colours"]),
                  "misplaced_thin_regions": len(evidence["misplaced_thin_regions"]),
                  "fold_target_mismatches": len(evidence["fold_target_mismatches"])}
    elif name == "palette":
        values = {"problems": len(_palette_problems(case, result))}
    else:
        raise KeyError(name)
    return {key: float(value) for key, value in values.items()}


def identities(name: str, result: dict, case) -> dict[str, list[list]]:
    """What assertion ``name`` finds wrong, item by item (``IDENTITIES``)."""
    def point(value):
        return [] if value is None else [[float(value[0]), float(value[1])]]

    if name == "hausdorff":
        values = {"truth_worst_point": point(result["local_hausdorff"]["worst_point"]),
                  "evidence_worst_point": point(result["evidence"]["distance"]["worst_point"])}
    elif name == "corners":
        values = {"missed_vertices": [list(map(float, c["point"])) for c in result["corners"]
                                      if c["role"] == "required" and not c["found"]]}
    elif name == "kinks":
        values = {"kinks": [list(map(float, cusp["point"])) for cusp in result["cusps"]
                            if cusp["kink"]]}
    elif name == "nodes":
        values = {"nodes_outside_zones": [list(p) for p in result["nodes_outside_points"]]}
    elif name == "topology":
        rows = result["topology"]
        values = {"wrong_counts": [[colour, row["traced"]["polygons"], row["traced"]["holes"]]
                                   for colour, row in rows.items() if row["error"]],
                  "missing_structures": [[colour, *item] for colour, row in rows.items()
                                         for item in row["missing"]],
                  "unexpected_structures": [[colour, *item] for colour, row in rows.items()
                                            for item in row["unexpected"]],
                  "unmatched_layers": [[row["fill"]] for row in result["unmatched_layers"]]}
    elif name == "area":
        over = [[colour] for colour, row in result["colour_area"].items()
                if row["asserted"] and row["symmetric_difference_px"] > row["limit_px"]]
        if result["misassigned_px"] > result["area_limit_px"]:
            over.append(["misassigned"])
        values = {"colours_over": over}
    elif name == "evidence":
        evidence = result["evidence"]
        values = {"missing_regions": [[row["label"], *row["pixel"], row["pixels"]]
                                      for row in evidence["missing_regions"]],
                  "colours_over": [[label] for label, row in evidence["colours"].items()
                                   if row["symmetric_difference_px"] > row["limit_px"]],
                  "absent_scene_colours": [[index]
                                           for index in evidence["absent_scene_colours"]],
                  "misplaced_thin_regions": [[row["label"], *row["pixel"], row["pixels"]]
                                             for row in evidence["misplaced_thin_regions"]],
                  "fold_target_mismatches": [list(row)
                                             for row in evidence["fold_target_mismatches"]]}
    elif name == "palette":
        values = {"problems": [item for _, item in _palette_findings(case, result)]}
    else:
        raise KeyError(name)
    return values


def _same_item(first: list, second: list) -> bool:
    if len(first) != len(second):
        return False
    for a, b in zip(first, second):
        if isinstance(a, str) or isinstance(b, str):
            if a != b:
                return False
        elif abs(float(a) - float(b)) > IDENTITY_TOLERANCE:
            return False
    return True


def identity_ratchet(recorded: dict[str, list], found: dict[str, list]) -> tuple[list[str],
                                                                                 list[str]]:
    """The items ``found`` has and ``recorded`` lacks (new defects: worse), and
    the ones ``recorded`` has and ``found`` lacks (fixed: better), each kind
    matched item for item within ``IDENTITY_TOLERANCE``."""
    worse, better = [], []
    for key in sorted(set(found) | set(recorded)):
        unmatched = list(recorded.get(key, []))
        new = []
        for item in found.get(key, []):
            hit = next((k for k, other in enumerate(unmatched) if _same_item(item, other)), None)
            if hit is None:
                new.append(item)
            else:
                unmatched.pop(hit)
        if new:
            worse.append(f"{key}: {len(new)} not recorded, e.g. {new[:3]}")
        if unmatched:
            better.append(f"{key}: {len(unmatched)} recorded no longer found, "
                          f"e.g. {unmatched[:3]}")
    return worse, better


def entry_ratchet(entry: dict, measured: dict[str, float],
                  found: dict[str, list]) -> tuple[list[str], list[str]]:
    """An expected failure's ratchet: its measurements (``ratchet``) and its
    identities (``identity_ratchet``), worse and better lines together."""
    worse, better = ratchet(entry["measured"], measured)
    more, fewer = identity_ratchet(entry.get("identities", {}), found)
    return worse + more, better + fewer


def ratchet(recorded: dict[str, float], measured: dict[str, float]) -> tuple[list[str], list[str]]:
    """The measurements worse and better than ``recorded`` beyond
    ``RATCHET_TOLERANCE``, each as a readable line."""
    worse, better = [], []
    for key in sorted(measured):
        value, then = float(measured[key]), float(recorded[key])
        tolerance = RATCHET_TOLERANCE[measurement_unit(key)]
        if value > then + tolerance:
            worse.append(f"{key} {value:.6g} > recorded {then:.6g}")
        elif value < then - tolerance:
            better.append(f"{key} {value:.6g} < recorded {then:.6g}")
    return worse, better


def area_coverage(bounds: CaseBounds, evidence_labels: set[int] | None) -> list[str]:
    """The colours no area-type assertion can fail on: neither held by the truth
    area bound (``limit < truth area``) nor by the evidence (``evidence_labels``,
    the scene colours present in the label map the trace received; None in the rgb
    tier, where the evidence assertion itself demands every such colour survive
    the palette stage)."""
    problems = []
    for index, limits in sorted(bounds.colour_limits.items()):
        if limits["asserted"]:
            continue
        if evidence_labels is None or index in evidence_labels:
            continue
        problems.append(f"colour {index}: area limit {limits['limit_px']:.1f} px^2 >= truth "
                        f"area {limits['truth_area_px']:.1f} and no pixel of it in the evidence")
    return problems


# --- sweep summaries ------------------------------------------------------------------------------


def boundary_coverage(bounds: CaseBounds) -> dict:
    """How much of the truth boundary the truth distance holds (outside the vertex
    discs and zones), as a fraction of all of it; the evidence distance holds
    everywhere."""
    total = len(bounds.truth_samples)
    return {"truth_samples": total, "kept": int(bounds.truth_kept.sum()),
            "fraction": float(bounds.truth_kept.sum() / total) if total else 1.0}


def summary_numbers(result: dict) -> dict:
    """The flat per-case numbers the sweep summaries and the tuning objective read,
    all classified by the regression bounds."""
    corners = result["corners"]
    required = [c for c in corners if c["role"] == "required"]
    kinks = sum(1 for cusp in result["cusps"] if cusp["kink"])
    area_ratios = [row["symmetric_difference_px"] / row["limit_px"]
                   for row in result["colour_area"].values()
                   if row["asserted"] and row["limit_px"] > 0]
    if result["area_limit_px"] > 0:
        area_ratios.append(result["misassigned_px"] / result["area_limit_px"])
    evidence = result["evidence"]
    evidence_ratios = [row["symmetric_difference_px"] / row["limit_px"]
                       for row in evidence["colours"].values() if row["limit_px"] > 0]
    return {
        "bound_px": result["bound_px"],
        "evidence_tolerance_px": evidence["tolerance_px"],
        "local_hausdorff_px": result["local_hausdorff"]["hausdorff_px"],
        "local_mean_px": result["local_hausdorff"]["mean_px"],
        "evidence_distance_px": evidence["distance"]["distance_px"],
        "geometric": result["geometric"],
        "corners_true": len(corners),
        "corners_found": sum(1 for c in corners if c["found"]),
        "required_corners": len(required),
        "required_found": sum(1 for c in required if c["found"]),
        "cusps": len(result["cusps"]), "kinks": kinks,
        "nodes_outside_zones": result["nodes_outside_zones"],
        "ideal_nodes_outside_zones": result["ideal_nodes"],
        "colours": len(result["topology"]),
        "node_limit": result["node_limit"],
        "topology_mismatches": sum(1 for row in result["topology"].values() if row["error"]),
        "unmatched_layers": len(result["unmatched_layers"]),
        "area_ratio": max(area_ratios) if area_ratios else 0.0,
        "evidence_area_ratio": max(evidence_ratios) if evidence_ratios else 0.0,
        "evidence_colours_over": sum(1 for ratio in evidence_ratios if ratio > 1.0),
        "evidence_regions": evidence["regions"],
        "evidence_missing_regions": len(evidence["missing_regions"]),
        "thin_regions": evidence["thin_regions"],
        "misplaced_thin_regions": len(evidence["misplaced_thin_regions"]),
        "required_scene_colours": len(evidence["required_scene_colours"]),
        "absent_scene_colours": len(evidence["absent_scene_colours"]),
        "fold_target_mismatches": len(evidence["fold_target_mismatches"]),
    }
