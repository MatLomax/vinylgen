"""Fidelity bounds of a traced synthetic case: the one yardstick.

The synthetic regression tests (``tests/test_synthetic_fidelity.py``), the fidelity
sweep and the tuner (``sweep.py``, ``tune.py``) all judge a trace with this module:
``case_bounds`` derives everything that depends only on the case, its truth, the
default trace settings and (in the rgb tier) the palette stage's folds (``D``,
the true vertices with their roles and radii, the narrow zones, the area bounds,
the ideal node count); ``evaluate`` measures one trace against them; ``check``
turns a measured result into a pass or fail per assertion, and ``measurements``
into the scalars an expected failure records.

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
decidable; the sharpenable-round radius of each turn; the local resolution and
topology floors; the area tube ``E(c)``; the ideal node count; the node limit's
explicit route and the sample distance it leaves room for. Not derived:

- the rgb tier's ``Q``: a measured baseline (below), reused from the label-map
  tier because the palette stage's anti-alias resolution follows no geometric
  rule;
- ``D`` and ``e`` in the direction from the trace to the truth or evidence: the
  fitter keeps each sample within ``M`` of the curve at the sample's own
  parameter and bounds nothing between samples, so a curve may bow further there.
  The trace is held to the same ``D`` and ``e`` both ways by choice; the measured
  margin is in ``docs/validation-and-testing.md``;
- the node limit is a geometric requirement, not a claim about the fitter's
  search: a route of cubics through the true boundary's axis extremes that
  keeps every boundary sample within the fitter's curve tolerance exists with
  that many nodes (derived, below), and a trace may use no more. That the
  fitter finds such a route is not derived: its nodes sit on samples rather than
  on the truth, it measures a sample's distance at the sample's parameter (at
  least the geometric distance), and its candidates are the extremes, the
  inflections and the bends of the smoothed samples, not the route's splits;
- the node limit's one node per run part cut by a zone: the boundary there
  passes into geometry the ideal segmentation does not describe, so the fitter
  may end a piece at the cut; an allowance, not a derivation;
- the tip skip in the sharpenable radius: ``curve_fit._TIP_SKIP_ALLOWANCES``,
  the trace stage's own setting, read as it is; likewise the corner detector's
  noise ratio ``curve_fit._CORNER_NOISE_RATIO`` in the corner roles;
- the thin-feature pin ``THIN_RECALL`` and its one-piece companion: the
  fitter's ``M`` allows a feature no wider than ``M`` to move wholly off its
  pixels or break, so holding a thin evidence region to its pixel centres, in
  one traced piece, is a requirement, set at the trace stage's own post-fit
  thresholds for a protected component (60% recall, ``viable_pieces == 1``);
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
- Decidable corners: the corner detector confirms a corner when its turn
  reaches ``K (u_a + u_b)``, ``K`` its noise ratio
  (``curve_fit._CORNER_NOISE_RATIO``) and ``u = atan2(e, L)`` each side's
  direction noise: ``L`` the chord of the side's grown window, ``e`` the larger
  of the side's fit error and the quantisation. The truth reads the same noise
  from its own geometry: ``e = Q`` and ``L`` the chord the detector's window can
  have along that side, from the tip skip (nearer on a short side, keeping the
  minimum straight length) to the longest window's end (``curve_fit._side_cap``)
  or the neighbouring true vertex, whichever is nearer (``side_chord``,
  ``side_reaches``).
  A side read from samples within ``Q`` of the truth has its direction off by up
  to its ``u``, so the turn the detector measures lies within ``u_a + u_b`` of the
  true turn. A ``corner`` whose true turn reaches ``(K + 1) (u_a + u_b)`` is
  *required* (the measured turn reaches the detector's bound however the noise
  falls); any other corner turning by at least ``(K - 1) (u_a + u_b)`` is
  *optional* (a cusp is allowed, not demanded); the rest are *shallow*. Every
  junction and edge point is required: the trace always anchors a cusp there.
- Sharpenable rounds: a round turning by ``T`` (below 180 degrees) with radius
  ``r`` has its sharp point (where its two sides meet) ``r (sec(T/2) - 1)`` from
  its arc and its tangent points ``r tan(T/2)`` from the sharp point, so its
  tangent points lie within the tip skip ``S`` (2.41 ``A``,
  ``curve_fit._TIP_SKIP_ALLOWANCES``) exactly when its sharp point lies within
  ``S tan(T/4)`` of its arc. The trace sharpens it only where the cusp lies
  within ``A`` of its sample (the move check) and, less the sides' error ``e``
  (at least ``Q``), within ``S tan(T/4)`` of it (the sides reach the corner;
  GLOSSARY "corner sharpening allowance"). Seen through ``Q``: each side is read
  from samples within ``Q`` of the true side, so it may lie ``Q`` inside it,
  which moves the sides' meeting point up to ``Q sec(T/2)`` towards the arc, and
  the drawn edge at the arc may lie ``Q`` outside it; with ``e = Q`` the trace
  may sharpen a round of radius up to ``sharpenable_radius(T) = (min(A, S tan(T/4)
  + Q) + (1 + sec(T/2)) Q) / (sec(T/2) - 1)``. At ``A = 1`` px the allowance
  governs at every turn (``S tan(T/4) + Q`` exceeds it): 16.3 px at 60 degrees,
  6.54 at 90, 3.12 at 120, 1.55 at 150. At ``A = 4`` px the tip skip governs
  below 90 degrees: 96.9 px at 30 degrees (154 by the allowance alone), 31.1 at
  60 (35.7), and 13.8 at 90.
  The least turn of a sharpenable round is ``(K - 1)`` times two sides' noise
  over the longest windows (``minimum_round_turn``), the optional corners' bound.
  Which round that is: the corner
  detector reads the sides up to its reach (``curve_fit._corner_reach`` of the
  default settings: the tip skip plus the longest window) from a candidate, so the turn
  it sees is the *full turn*, the truth's net turn over the stretch extended by
  that reach on each side; a stretch of a longer round is judged by the full
  turn, so a circle or a large round is never sharpenable through a piece of it.
  A round turns one way and holds no true vertex (a corner, junction or edge
  point is judged by its own disc ``R(c)``). The cusp lies within ``A`` of its
  sample, a sample within ``Q`` of the round's middle, which lies
  ``r (sec(T/2) - 1)`` from the sharp point: so a cusp is allowed where a stretch
  of true boundary turning by at least the least round turn, with its radius
  ``b`` (its length over its turn in radians) at most ``sharpenable_radius`` of
  its full turn, has its sharp point within ``A + Q + b (sec(T/2) - 1)`` of the
  cusp (``round_turn_near``). A cusp on a larger round, or away from a
  sharpenable one's sharp point, is a kink.

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
  vertex, inside a zone, or on a sharpenable round (within ``A + Q`` of a stretch
  of the truth turning by at least the least round turn as a round of radius
  ``b`` at most ``sharpenable_radius`` of its turn).
- ``nodes``: boundary nodes outside the zones ``<=`` the node limit
  (``zone_node_limit``), the node count of an explicit route through the
  boundary outside the zones. Its vertex nodes (``_vertex_nodes``): every node
  the trace stage must anchor in the label map it received, outside the zones
  (``evidence_anchors``: a pixel corner where three or more colours meet or two
  meet corner to corner, and a frame point between two colours; the trace makes
  every such chain end a cusp, and a many-colour junction splits into several of
  them over neighbouring pixel corners), and every vertex of the ideal
  segmentation whose traced node can lie outside the zones (within ``R(c)`` of an
  anchored vertex or ``D`` of any other) that is a corner, or a junction or edge
  point no anchor within that radius stands for; a corner-to-corner contact of
  two colours with no anchor counts two (the raster bridges or parts the two
  regions, and the outline turns at two pixel corners either way). Then on each
  part of an ideal run outside them, its axis extremes,
  the splits the route needs between consecutive nodes (part ends and extremes),
  and one node per part end a zone cuts. Between two route nodes the route draws
  the truth with one explicit cubic (``span_cubic_error``: ends on the truth, the
  truth's end tangents, handle lengths fitted) or, where one cubic does not stay
  within the span's budget of the truth, with the fewest pieces the greedy split
  gives (``span_pieces``). The budget (``span_budget``) is the fitter's curve
  tolerance (``curve_fit._curve_tolerance``: ``curve_fit_tolerance`` capped by
  ``max_deviation``) less the farthest any boundary sample of the span lies from
  the truth (at least ``Q``; ``sample_quantisation`` measures it on the
  hard-edged label render's pixel-edge vertices, which exceed ``Q`` where the
  truth curves tightly), so every sample lies within the tolerance of the route's
  curve. The count is derived; it is a geometric requirement on the trace, not a
  claim that the fitter finds that route (``What is derived``). The ideal
  segmentation's own pieces (one cubic per arc of at most 90 degrees, split at
  its own nodes) are not the route: the fitter's candidates include every axis
  extreme and none of the ideal nodes, and a piece of an arc between extremes
  can sweep far more than 90 degrees of an ellipse's parameter (155 degrees on
  ``ellipse-a8-rot17-1536``, whose one-cubic error there is 7 px, so each such
  span takes two pieces and the limit is 6, not 4).
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
  ``D`` to ``3 D`` in steps of ``D / 4`` (``TOPOLOGY_BAND_MULTIPLES``), and every
  count of every colour is asserted to lie within its range over the widths: an
  added structure beyond the range's maximum, or deletions below its minimum,
  fail. One deletion is decided where the truth's count at the traced band is
  the range's minimum, one addition where it is the maximum; elsewhere a single
  change near the band edge is indistinguishable from the band's own effect, and
  only a change that leaves the range is decided. Located structures: a true
  piece or hole none of whose rings comes within ``3 D`` of the removed region
  (*isolated*) is counted the same at every width, and must match exactly one
  traced structure of its kind, each region within the topology tolerance
  (``max(D, e)``) of the other; a traced piece or hole whose rings clear the
  removed region by ``3 D`` plus that tolerance (so its true counterpart would be
  isolated) must match an isolated true structure, so an isolated addition fails
  whatever the counts. ``topology_mutations`` checks, per case, that the truth
  itself passes and that every change the truth decides fails: each isolated
  structure deleted, each counted structure deleted where its count decides one
  deletion, an isolated addition, an addition near the removed region where its
  count decides one, and otherwise the smallest multiple deletion or addition
  that leaves the range; each single change a count cannot decide is reported,
  with why, never passed.
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
  its pixels rather than a full width off them. Such a region must also be
  covered by exactly one connected piece of its traced layer within ``e`` of its
  pixels that covers a viable share of its thin pixels' centres (at least
  ``max(2, 5%)``; ``_covering_pieces``, the trace stage's ``viable_pieces == 1``
  for a protected component), so a thin feature cut by a gap fails however
  little area the gap takes.
- ``palette`` (palette-stage cases): ``scene.params["expect"]`` (see
  ``palette_outcome``).

Expected failures record ``measurements``, the scalars each assertion compares
with its bound (``MEASUREMENTS``); ``worse_than_recorded`` says which have got
worse than the record by more than ``WORSE_MARGIN``.
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
from vinylgen import curve_fit
from vinylgen.curve_fit import _corner_reach, _curve_tolerance, _side_cap, _tip_skip
from vinylgen.fidelity.families import case_palette_settings
from vinylgen.fidelity.metrics import (DISTANCE_ERROR_BOUND, SAMPLE_SPACING,
                                       _boundary_samples, _geometry_rings, _polygons,
                                       measure, parse_traced_svg, path_geometry)
from vinylgen.fidelity.raster import render_labels
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
_FRAME_EPS = 1e-6
_NODE_DECIMALS = 6


# --- lengths --------------------------------------------------------------------------------


def corner_windows(lengths: TraceSettings) -> tuple[float, float, float]:
    """The corner detector's side windows in px for resolved ``lengths``: the
    longest window (``curve_fit._side_cap``), the tip skip and the minimum
    straight length (``curve_fit._geometric_corners``)."""
    minimum = max(1e-4, float(lengths.minimum_straight_length))
    return float(_side_cap(lengths)), float(_tip_skip(lengths)), minimum


def side_chord(distance: float, lengths: TraceSettings) -> float:
    """The chord of the detector's grown side window along a straight side whose
    neighbouring true vertex is ``distance`` px from the corner: from the tip
    skip (nearer when the side is short, keeping the minimum straight length) to
    the longest window's end or that vertex, whichever is nearer."""
    window, skip, minimum = corner_windows(lengths)
    return min(window, max(distance - skip, min(distance, minimum)))


def side_noise_deg(chord: float, quantisation: float = Q_PX) -> float:
    """A side's direction noise (degrees) read over ``chord``: ``atan2(Q, L)``."""
    return math.degrees(math.atan2(quantisation, chord)) if chord > 0 else 90.0


def minimum_round_turn(lengths: TraceSettings) -> float:
    """The least turn (degrees) of a round a cusp may sharpen: the detector's
    measured turn may exceed the true one by the two sides' noise, so it reaches
    the noise ratio times that noise from ``(ratio - 1)`` times it; the least
    noise is over two of the longest windows."""
    window, _, _ = corner_windows(lengths)
    return (curve_fit._CORNER_NOISE_RATIO - 1.0) * 2.0 * side_noise_deg(window)


def resolved_lengths(width: int, height: int) -> TraceSettings:
    """The default trace lengths in px on this canvas, as the trace stage resolves them."""
    return resolve_trace_lengths(TraceSettings(), length_reference(width, height))


def route_tolerance(lengths: TraceSettings) -> float:
    """The fitter's curve tolerance, which the node limit's explicit route meets:
    ``curve_fit._curve_tolerance`` (``curve_fit_tolerance`` capped by
    ``max_deviation``)."""
    return _curve_tolerance(lengths)


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


def _other_vertices(vertices: np.ndarray, point) -> np.ndarray:
    if not len(vertices):
        return vertices
    return vertices[np.hypot(vertices[:, 0] - point[0], vertices[:, 1] - point[1])
                    > _VERTEX_MATCH_PX]


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


def corner_roles(truth, rings: list[np.ndarray], lengths: TraceSettings) -> list[dict]:
    """Every true vertex with its role (module docstring, "Decidable corners"):
    ``required`` (a junction or edge point, or a corner turning by at least the
    noise ratio plus one times its sides' direction noise), ``optional`` (any
    other corner turning by at least the noise ratio less one times it) or
    ``shallow``. ``lengths`` are the resolved default lengths the detector's
    side windows follow."""
    points = np.asarray([corner.point for corner in truth.corners],
                        dtype=np.float64).reshape(-1, 2)
    if len(points) > 1:
        distances, _ = cKDTree(points).query(points, k=2)
        nearest = distances[:, 1]
    else:
        nearest = np.full(len(points), math.inf)
    ratio = float(curve_fit._CORNER_NOISE_RATIO)
    rows = []
    for corner, spacing in zip(truth.corners, nearest):
        noise = None
        if corner.kind != "corner":
            role = "required"
        else:
            reaches = side_reaches(rings, corner.point, points)
            if reaches is None:
                reaches = (spacing, spacing)
            noise = sum(side_noise_deg(side_chord(reach, lengths)) for reach in reaches)
            if corner.turn_deg >= (ratio + 1.0) * noise:
                role = "required"
            elif corner.turn_deg >= (ratio - 1.0) * noise:
                role = "optional"
            else:
                role = "shallow"
        rows.append({"point": [float(corner.point[0]), float(corner.point[1])],
                     "kind": corner.kind, "turn_deg": float(corner.turn_deg),
                     "noise_deg": noise, "role": role, "narrow": False})
    return rows


def side_reaches(rings: list[np.ndarray], point, vertices: np.ndarray
                 ) -> tuple[float, float] | None:
    """How far each side of the true corner at ``point`` runs before the next true
    vertex: the chord from the corner to that vertex (or to half way round a ring
    with no other vertex). None when no truth ring passes through the corner."""
    through = _rings_through(rings, point)
    if not through:
        return None
    ring, apex = through[0]
    others = _other_vertices(vertices, point)
    sides = [_side(ring, apex, direction, others) for direction in (-1, 1)]
    return tuple(float(np.hypot(*(side[-1] - side[0]))) for side in sides)


def sharpenable_radius(turn, allowance: float, skip: float, quantisation: float = Q_PX):
    """The largest radius of a round turning by ``turn`` radians (``0 < turn <
    pi``; a number or an array) the trace may sharpen (module docstring,
    "Sharpenable rounds"), seen through ``Q``: the smaller of the radius whose
    sharp point, ``r (sec(turn / 2) - 1)`` from its arc, lies within the
    allowance, ``(A + (1 + sec(turn / 2)) Q) / (sec(turn / 2) - 1)``, and the
    radius whose tangent points, ``r tan(turn / 2)`` from its sharp point, lie
    within the tip skip, ``(S + Q) / tan(turn / 2)``."""
    turn = np.asarray(turn, dtype=np.float64)
    secant = 1.0 / np.cos(turn / 2.0)
    by_allowance = (allowance + (1.0 + secant) * quantisation) / (secant - 1.0)
    by_skip = (skip + quantisation) / np.tan(turn / 2.0)
    radius = np.minimum(by_allowance, by_skip)
    return float(radius) if radius.ndim == 0 else radius


def round_window(allowance: float, skip: float, minimum_turn_deg: float,
                 quantisation: float = Q_PX) -> float:
    """How far from a cusp a sharpenable round can lie: ``A + Q`` plus the longest
    sharpenable round turning by at least ``minimum_turn_deg`` (its length
    ``sharpenable_radius(T) T``; the tip-skip bound keeps it below ``2 (S + Q)``)."""
    turns = np.radians(np.linspace(max(minimum_turn_deg, 1e-3), 179.9, 400))
    return allowance + quantisation + float((sharpenable_radius(turns, allowance, skip,
                                                                  quantisation) * turns).max())


# A round's local radius (at each of its vertices, its share of the curve over
# the vertex's turn) stays within this factor of the round's radius.
_ROUND_RADIUS_FACTOR = 2.0
_ROUNDS_CACHE: dict = {}


def truth_rounds(ring: np.ndarray, vertices=None) -> list[tuple]:
    """The rounds of one truth ring (module docstring, "Sharpenable rounds"): each
    maximal run of consecutive ring vertices, none a true vertex of ``vertices``,
    turning the same way at roughly constant curvature. The local radius of a
    vertex is its share of the curve (half of each adjacent segment, a straight
    stretch counting none) over its turn; a run extends while
    that stays within ``_ROUND_RADIUS_FACTOR`` of the run's radius (its summed
    local lengths over its summed turn), so it ends at a straight segment, an
    inflection, a true vertex or a curvature change beyond that factor. A ring
    that is one run all the way round (a circle) has no round below 180 degrees.

    Returns ``(turn, radius, sharp_point)`` per round turning by less than 180
    degrees: its whole turn (radians), its radius, and where the segment into its
    first vertex and the segment out of its last one meet."""
    closed = ring[:-1] if np.allclose(ring[0], ring[-1]) else ring
    vertex_key = None if vertices is None else np.asarray(vertices, dtype=np.float64).tobytes()
    key = (closed.tobytes(), vertex_key)
    if key in _ROUNDS_CACHE:
        return _ROUNDS_CACHE[key]
    count = len(closed)
    rounds: list[tuple] = []
    if count >= 3:
        before = closed - np.roll(closed, 1, axis=0)
        after = np.roll(closed, -1, axis=0) - closed
        turns = np.arctan2(before[:, 0] * after[:, 1] - before[:, 1] * after[:, 0],
                           np.einsum("ij,ij->i", before, after))
        steps_in = np.hypot(*before.T)
        steps_out = np.hypot(*after.T)
        # A vertex's share of the curve: half of each adjacent segment, a
        # straight stretch (a segment more than the factor longer than the other)
        # counting none.
        local = 0.5 * (np.where(steps_in > _ROUND_RADIUS_FACTOR * steps_out, 0.0, steps_in)
                       + np.where(steps_out > _ROUND_RADIUS_FACTOR * steps_in, 0.0, steps_out))
        sign = np.sign(np.where(np.abs(turns) > 1e-9, turns, 0.0))
        vertex = np.zeros(count, dtype=bool)
        if vertices is not None and len(vertices):
            vertex = cKDTree(np.asarray(vertices, dtype=np.float64).reshape(-1, 2)).query(
                closed)[0] <= 1e-6
        # A segment much longer than its neighbours is a straight stretch: a round
        # ends before it.
        straight_before = steps_in > _ROUND_RADIUS_FACTOR * np.minimum(
            np.roll(steps_in, 1), steps_out)
        breaks = np.flatnonzero((sign == 0) | vertex | (sign != np.roll(sign, 1))
                                | straight_before)
        with np.errstate(divide="ignore", invalid="ignore"):
            own_radius = local / np.abs(turns)
        if len(breaks):
            start = int(breaks[0])
        else:
            # A ring turning one way all round starts at its sharpest curvature
            # change; if the runs still close up into one, it is a circle.
            change = np.abs(np.log(own_radius / np.roll(own_radius, 1)))
            start = int(np.argmax(np.where(np.isfinite(change), change, 0.0)))
        if True:
            order = (start + np.arange(count)) % count
            runs: list[list[int]] = []
            run: list[int] = []
            length = turn = 0.0
            for index in order:
                index = int(index)
                usable = sign[index] != 0 and not vertex[index]
                if run and usable and sign[index] == sign[run[0]] and not straight_before[index]:
                    radius = length / turn
                    own = own_radius[index]
                    if radius / _ROUND_RADIUS_FACTOR <= own <= radius * _ROUND_RADIUS_FACTOR:
                        run.append(index)
                        length += local[index]
                        turn += abs(turns[index])
                        continue
                if run:
                    runs.append(run)
                run = [index] if usable else []
                length = float(local[index]) if usable else 0.0
                turn = float(abs(turns[index])) if usable else 0.0
            if run:
                runs.append(run)
            if len(runs) == 1 and len(runs[0]) == count:
                runs = []
            elif (not len(breaks) and len(runs) > 1
                  and (runs[-1][-1] + 1) % count == runs[0][0]):
                # A ring turning one way all round: the run closing the ring joins
                # the first when their curvatures agree.
                joined = runs[-1] + runs[0]
                radius = float(local[joined].sum()) / float(np.abs(turns[joined]).sum())
                own = [float(local[run].sum()) / float(np.abs(turns[run]).sum())
                       for run in (runs[-1], runs[0])]
                if all(radius / _ROUND_RADIUS_FACTOR <= value <= radius * _ROUND_RADIUS_FACTOR
                       for value in own):
                    runs = [joined] + runs[1:-1]
            for run in runs:
                total = float(np.abs(turns[run]).sum())
                if total >= math.pi:
                    continue
                radius = float(local[run].sum()) / total
                first, last = run[0], run[-1]
                head = after[(first - 1) % count]
                tail = after[last]
                cross = head[0] * tail[1] - head[1] * tail[0]
                if abs(cross) <= 1e-12:
                    continue
                offset = closed[last] - closed[first]
                along = (offset[0] * tail[1] - offset[1] * tail[0]) / cross
                rounds.append((total, radius, closed[first] + along * head))
    if len(_ROUNDS_CACHE) > 4096:
        _ROUNDS_CACHE.clear()
    _ROUNDS_CACHE[key] = rounds
    return rounds


def round_turn_near(rings: list[np.ndarray], point, allowance: float, skip: float,
                    minimum_turn_deg: float, vertices=None,
                    quantisation: float = Q_PX) -> float:
    """The largest turn (degrees) of a *sharpenable round* near ``point`` (0 when
    there is none): a round of the truth (``truth_rounds``) turning by ``T``, at
    least ``minimum_turn_deg`` and below 180 degrees, whose radius ``b`` is at most
    ``sharpenable_radius(T)`` and whose sharp point lies within ``A + Q + b
    (sec(T/2) - 1)`` of ``point`` (within ``A`` of a sample within ``Q`` of the
    round's middle, which lies ``b (sec(T/2) - 1)`` from the sharp point). A round
    is judged by its whole turn, so a piece of a large round is never
    sharpenable."""
    best = 0.0
    low = math.radians(minimum_turn_deg)
    px, py = point
    for ring in rings:
        for total, radius, sharp in truth_rounds(ring, vertices):
            if total < low or radius > sharpenable_radius(total, allowance, skip, quantisation):
                continue
            reach = allowance + quantisation + radius * (1.0 / math.cos(total / 2.0) - 1.0)
            if math.hypot(sharp[0] - px, sharp[1] - py) <= reach:
                best = max(best, math.degrees(total))
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
    corner_reach: float
    tip_skip: float
    minimum_round_turn: float
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
    topology_beyond_px: float = 0.0

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
        """Whether the topology assertion holds anything: some of the canvas lies
        beyond the traced band round the removed region, where a count or an
        isolated structure can change."""
        return bool(self.topology_truth) and self.topology_beyond_px > MIN_PIECE_AREA


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


# The explicit route's cubic: Newton reparameterisations per fit, least-squares
# handle refits, the refinement of the handle lengths tried when the fit misses by
# at most ``_ROUTE_REFINE`` budgets, and the most equal pieces a span is split
# into before its count is taken as it stands.
_ROUTE_NEWTON = 6
_ROUTE_REFITS = 3
_ROUTE_REFINE = math.inf
_ROUTE_MAX_PIECES = 64
# A span whose samples lie as far from the truth as the tolerance still gets a
# route, of cubics this close to the truth.
_ROUTE_MIN_BUDGET = 0.05


def _polyline_tangent(points: np.ndarray, index: int, direction: int) -> np.ndarray:
    """The unit tangent, along the direction of travel, of the polyline ``points``
    at ``index``, read from the points after it (``direction`` 1) or before it
    (-1), to second order."""
    count = len(points)
    if direction > 0:
        if index + 2 < count:
            vector = -3.0 * points[index] + 4.0 * points[index + 1] - points[index + 2]
        else:
            vector = points[min(index + 1, count - 1)] - points[index]
    else:
        if index - 2 >= 0:
            vector = 3.0 * points[index] - 4.0 * points[index - 1] + points[index - 2]
        else:
            vector = points[index] - points[max(index - 1, 0)]
    norm = float(np.hypot(*vector))
    return vector / norm if norm > 0 else np.zeros(2)


def _bezier(control: np.ndarray, u: np.ndarray) -> np.ndarray:
    v = 1.0 - u
    return (np.outer(v ** 3, control[0]) + np.outer(3 * v * v * u, control[1])
            + np.outer(3 * v * u * u, control[2]) + np.outer(u ** 3, control[3]))


def _reproject(control: np.ndarray, points: np.ndarray, u: np.ndarray) -> np.ndarray:
    """One Newton step moving each ``u`` towards its point's nearest curve parameter."""
    v = 1.0 - u
    first = (np.outer(3 * v * v, control[1] - control[0])
             + np.outer(6 * v * u, control[2] - control[1])
             + np.outer(3 * u * u, control[3] - control[2]))
    second = (np.outer(6 * v, control[2] - 2 * control[1] + control[0])
              + np.outer(6 * u, control[3] - 2 * control[2] + control[1]))
    offset = _bezier(control, u) - points
    numerator = np.einsum("ij,ij->i", offset, first)
    denominator = np.einsum("ij,ij->i", first, first) + np.einsum("ij,ij->i", offset, second)
    step = np.divide(numerator, denominator, out=np.zeros_like(u),
                     where=np.abs(denominator) > 1e-12)
    return np.clip(u - step, 0.0, 1.0)


def _span_error(points: np.ndarray, start: np.ndarray, end: np.ndarray,
                handles, u: np.ndarray) -> tuple[float, np.ndarray]:
    """The largest distance from ``points`` to the cubic from the first point to
    the last with handles ``handles`` long along ``start`` and ``end``, each
    point measured to the curve point at its Newton-projected parameter (so never
    less than its geometric distance to the curve), and the parameters."""
    control = np.asarray([points[0], points[0] + handles[0] * start,
                          points[-1] - handles[1] * end, points[-1]])
    for _ in range(_ROUTE_NEWTON):
        u = _reproject(control, points, u)
    return float(np.hypot(*(_bezier(control, u) - points).T).max()), u


def span_cubic_error(points: np.ndarray, start: np.ndarray, end: np.ndarray,
                     budget: float = 0.0) -> float:
    """How closely one explicit cubic draws the polyline ``points`` (a stretch of
    true boundary): it ends on the stretch's ends with end tangents ``start`` and
    ``end`` (unit, along the direction of travel), and its handle lengths are the
    least-squares fit at Newton-improved parameters (Schneider), refined by a
    Nelder-Mead search when that fit misses ``budget`` by at most
    ``_ROUTE_REFINE`` times. Returns the largest distance of a point of the
    stretch to the cubic (0 for a straight stretch, which its chord draws)."""
    points = np.asarray(points, dtype=np.float64)
    if len(points) <= 2:
        return 0.0
    chord = points[-1] - points[0]
    length = float(np.hypot(*chord))
    arc = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(points, axis=0).T))))
    if arc[-1] <= 0:
        return 0.0
    if length > 0:
        normal = np.asarray([-chord[1], chord[0]]) / length
        if float(np.abs((points - points[0]) @ normal).max()) <= 1e-9:
            return 0.0
    chord_u = arc / arc[-1]
    candidates = [(arc[-1] / 3.0, arc[-1] / 3.0)]
    # The tangent lines' meeting point: a conic arc is the affine image of a
    # circular arc, whose cubic has handles (4/3) tan(turn/4) / tan(turn/2) of the
    # way to it.
    cross = start[0] * end[1] - start[1] * end[0]
    if abs(cross) > 1e-9:
        reach = ((chord[0] * end[1] - chord[1] * end[0]) / cross,
                 (start[0] * chord[1] - start[1] * chord[0]) / cross)
        turn = math.acos(max(-1.0, min(1.0, float(start @ end))))
        if reach[0] > 0 and reach[1] > 0 and 0 < turn < math.pi:
            share = 4.0 / 3.0 * math.tan(turn / 4.0) / math.tan(turn / 2.0)
            candidates.append((share * reach[0], share * reach[1]))
    u = chord_u.copy()
    ends = np.asarray([points[0], points[0], points[-1], points[-1]])
    for _ in range(_ROUTE_REFITS):
        v = 1.0 - u
        a1 = np.outer(3 * v * v * u, start)
        a2 = np.outer(-3 * v * u * u, end)
        rest = points - _bezier(ends, u)
        matrix = np.asarray([[np.sum(a1 * a1), np.sum(a1 * a2)],
                             [np.sum(a1 * a2), np.sum(a2 * a2)]])
        vector = np.asarray([np.sum(a1 * rest), np.sum(a2 * rest)])
        if abs(np.linalg.det(matrix)) <= 1e-12 * max(1.0, float(np.abs(matrix).max())) ** 2:
            break
        solved = np.linalg.solve(matrix, vector)
        if not (solved[0] > 1e-6 and solved[1] > 1e-6 and max(solved) < 4.0 * arc[-1]):
            break
        candidates.append((float(solved[0]), float(solved[1])))
        _, u = _span_error(points, start, end, candidates[-1], u)
    scored = [(_span_error(points, start, end, handles, chord_u.copy())[0], handles)
              for handles in candidates]
    error, handles = min(scored)
    if 0 < budget < error <= _ROUTE_REFINE * budget:
        from scipy.optimize import minimize
        # The search reads a subsample; the result is measured on every point.
        stride = max(1, len(points) // 400)
        sample = np.vstack([points[::stride], points[-1:]])
        sample_u = np.concatenate([chord_u[::stride], [1.0]])

        def objective(values):
            if values[0] <= 0 or values[1] <= 0:
                return math.inf
            return _span_error(sample, start, end, values, sample_u.copy())[0]

        found = minimize(objective, np.asarray(handles), method="Nelder-Mead",
                         options={"xatol": 1e-3, "fatol": 1e-4, "maxiter": 300})
        if found.x[0] > 0 and found.x[1] > 0:
            error = min(error, _span_error(points, start, end, found.x, chord_u.copy())[0])
    return error


def span_pieces(points: np.ndarray, start: np.ndarray, end: np.ndarray, budget: float) -> int:
    """The fewest pieces (greedy; at most ``_ROUTE_MAX_PIECES``) the
    stretch of true boundary ``points`` splits into so that each piece's explicit
    cubic (``span_cubic_error``; end tangents ``start`` and ``end`` at the
    stretch's ends, the stretch's own tangents at the splits) lies within
    ``budget`` of it."""
    points = np.asarray(points, dtype=np.float64)
    if len(points) <= 2:
        return 1
    last = len(points) - 1

    def fits(a: int, b: int) -> bool:
        return span_cubic_error(points[a:b + 1],
                                start if a == 0 else _polyline_tangent(points, a, 1),
                                end if b == last else _polyline_tangent(points, b, -1),
                                budget) <= budget

    if fits(0, last):
        return 1
    # Greedy: from each node the farthest point one fitting cubic reaches (found
    # by bisection), which gives the fewest pieces where every shorter stretch of
    # a fitting one fits too.
    pieces, at = 0, 0
    while at < last and pieces < _ROUTE_MAX_PIECES:
        low, high = at + 1, last
        while low < high:
            middle = (low + high + 1) // 2
            if fits(at, middle):
                low = middle
            else:
                high = middle - 1
        at = low
        pieces += 1
    return pieces if at >= last else _ROUTE_MAX_PIECES


def span_budget(tolerance: float, quantisation) -> float:
    """How far a route piece's cubic may lie from the truth so that every boundary
    sample of the span, within ``quantisation`` of the truth (at least ``Q``), lies
    within ``tolerance`` of it. Never below ``_ROUTE_MIN_BUDGET``."""
    return max(_ROUTE_MIN_BUDGET, tolerance - max(Q_PX, float(quantisation)))


def _route_extra(points: np.ndarray, nodes: list[int], tolerance: float,
                 quantisation: np.ndarray) -> int:
    """The nodes the explicit route adds between consecutive route nodes
    ``nodes`` (increasing indices into ``points``): each span's ``span_pieces``,
    at the span's ``span_budget`` (``quantisation`` per point), less one."""
    extra = 0
    for a, b in zip(nodes[:-1], nodes[1:]):
        if b - a < 2:
            continue
        budget = span_budget(tolerance, quantisation[a:b + 1].max())
        extra += span_pieces(points[a:b + 1], _polyline_tangent(points, a, 1),
                             _polyline_tangent(points, b, -1), budget) - 1
    return extra


def _run_nodes(run, outside: np.ndarray, tolerance: float = 0.0,
               quantisation: np.ndarray | None = None) -> tuple[int, int]:
    """For one ideal run and its points ``outside`` the zone: the ideal interior
    nodes that can lie on its parts outside the zone (``zone_ideal_nodes``), and
    the explicit route's interior nodes there (``zone_node_limit``): per part,
    its axis extremes, the nodes ``span_pieces`` adds between consecutive route
    nodes (the part's ends and extremes) so that one cubic per piece lies within
    ``span_budget`` of the truth, for the fitter's curve ``tolerance`` and the
    samples' distance to the truth at each run point (``quantisation``, ``Q``
    where None; no nodes are added when ``tolerance`` is 0), and one per part end
    the zone cuts. A closed run wholly outside is walked once round from an
    extreme."""
    if quantisation is None:
        quantisation = np.full(len(run.points), Q_PX)
    whole = _quarters(run.sweep[-1]) if run.conic else 1
    interior = whole if run.closed else whole - 1
    if not run.conic and not run.closed:
        interior = 0
    if not outside.any():
        return 0, 0
    extremes = _axis_extremes(run.points, run.closed)

    def extra(points, nodes, quantised):
        return _route_extra(points, nodes, tolerance, quantised) if tolerance > 0 else 0

    if outside.all():
        if run.closed:
            ring = run.points[:-1]
            marks = np.flatnonzero(extremes[:-1])
            seam = int(marks[0]) if len(marks) else 0
            order = np.roll(np.arange(len(ring)), -seam)
            points = np.vstack([ring[order], ring[order[:1]]])
            quantised = np.concatenate([quantisation[:-1][order], quantisation[order[:1]]])
            nodes = sorted(int((m - seam) % len(ring)) for m in marks) or [0]
            return interior, len(nodes) + extra(points, nodes + [len(ring)], quantised)
        marks = [int(m) for m in np.flatnonzero(extremes)]
        return interior, len(marks) + extra(run.points, [0] + marks + [len(run.points) - 1],
                                            quantisation)
    spacing = run.sweep[-1] / whole if run.conic else math.inf
    flags, sweep, marks, points = outside, run.sweep, extremes, run.points
    quantised = quantisation
    if run.closed:
        # Drop the repeated end and start the walk inside the zone, so no part
        # outside it wraps round the start.
        flags, sweep, marks = outside[:-1], run.sweep[:-1], extremes[:-1]
        total = run.sweep[-1]
        first = int(np.flatnonzero(~flags)[0])
        order = np.roll(np.arange(len(flags)), -first)
        flags, marks, points = flags[order], marks[order], run.points[:-1][order]
        quantised = quantisation[:-1][order]
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
        inner = [int(m) for m in np.flatnonzero(marks[index:end + 1]) + index
                 if index < m < end]
        limit += len(inner) + extra(points, [index] + inner + [end], quantised) + cuts
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


def evidence_anchors(labels: np.ndarray) -> np.ndarray:
    """The pixel corners of the label map ``labels`` where the trace stage always
    anchors a cusp node (``boundary_graph``: a chain of one colour pair ends where
    that pair's boundary does not simply pass through, and ``node_types.
    cusp_anchor_keys`` makes every chain end a cusp): an inner corner whose four
    pixels carry three or more colours, or two colours corner to corner; and a
    frame point between two different frame pixels. The canvas corners are left
    out, as ``_boundary_nodes`` leaves them out. ``(n, 2)`` as ``(x, y)``."""
    labels = np.asarray(labels)
    height, width = labels.shape
    points = []
    if height >= 2 and width >= 2:
        a, b = labels[:-1, :-1], labels[:-1, 1:]
        c, d = labels[1:, :-1], labels[1:, 1:]
        # The number of colours among each inner corner's four pixels.
        colours = 1 + (np.diff(np.sort(np.stack([a, b, c, d]), axis=0), axis=0) != 0).sum(axis=0)
        diagonal = (colours == 2) & (a == d) & (b == c) & (a != b)
        ys, xs = np.nonzero((colours >= 3) | diagonal)
        points.append(np.c_[xs + 1, ys + 1])
    for line, fixed, horizontal in ((labels[0, :], 0, True), (labels[-1, :], height, True),
                                    (labels[:, 0], 0, False), (labels[:, -1], width, False)):
        changes = np.flatnonzero(line[1:] != line[:-1]) + 1
        if horizontal:
            points.append(np.c_[changes, np.full(len(changes), fixed)])
        else:
            points.append(np.c_[np.full(len(changes), fixed), changes])
    found = np.vstack(points).astype(np.float64) if points else np.empty((0, 2))
    return np.unique(found, axis=0) if len(found) else found.reshape(0, 2)


def corner_contacts(truth, corners: list[dict]) -> list:
    """The junctions of ``corners`` where only two colours meet, corner to corner."""
    found = []
    for corner in corners:
        if corner["kind"] != "junction":
            continue
        point = Point(corner["point"])
        colours = [index for index, geometry in truth.colour_geometry.items()
                   if geometry.distance(point) <= 1e-3]
        if len(colours) == 2:
            found.append(corner["point"])
    return found


def _vertex_nodes(truth, zone, radii: dict | None, default_radius: float,
                  anchors: np.ndarray | None = None, anchored=None, contacts=None) -> int:
    """The vertex nodes of the node limit outside ``zone``: every forced anchor
    of the label map (``anchors``, ``evidence_anchors``) outside it, and every
    vertex of the ideal segmentation whose traced node can lie outside it (its disc
    of radius ``radii[(x, y)]``, or ``default_radius``, is not inside it) unless it
    is a junction or edge point (in ``anchored``, points) with an anchor within
    that radius, which stands for it. A corner is never an anchor. A point where
    two colours meet corner to corner (in ``contacts``) with no anchor counts two:
    the raster either bridges the two regions of one colour with a pixel or
    parts them, and either way the outline turns at two pixel corners there (only
    a checkerboard of the four pixels makes one anchor instead)."""
    vertices = np.asarray(truth.ideal_vertices, dtype=np.float64).reshape(-1, 2)
    anchors = np.empty((0, 2)) if anchors is None else np.asarray(anchors).reshape(-1, 2)
    if zone is not None and not zone.is_empty:
        shapely.prepare(zone)
    count = int((~_inside(zone, anchors)).sum()) if len(anchors) else 0
    tree = cKDTree(anchors) if len(anchors) else None
    anchored = {(round(float(x), 6), round(float(y), 6)) for x, y in (anchored or [])}
    contacts = {(round(float(x), 6), round(float(y), 6)) for x, y in (contacts or [])}
    for x, y in vertices:
        radius = (radii or {}).get((round(float(x), 6), round(float(y), 6)), default_radius)
        if zone is not None and not zone.is_empty \
                and zone.contains(Point(x, y).buffer(max(radius, 1e-9), quad_segs=4)):
            continue
        key = (round(float(x), 6), round(float(y), 6))
        if key in anchored and tree is not None \
                and tree.query_ball_point((x, y), max(radius, 1e-9)):
            continue
        count += 2 if key in contacts else 1
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


def zone_node_limit(truth, zone, radii: dict | None = None, default_radius: float = 0.0,
                    tolerance: float = 0.0, quantisation: list | None = None,
                    anchors: np.ndarray | None = None, anchored=None,
                    contacts=None) -> int:
    """The ``nodes`` assertion's limit (module docstring, ``nodes``): the vertex
    nodes (``_vertex_nodes``: the label map's forced ``anchors`` and the ideal
    vertices none of them stands for, ``anchored`` the junctions and edge points,
    ``contacts`` the corner-to-corner contacts),
    and on each part of an ideal run outside
    the zone the explicit route's interior nodes: its axis extremes, the nodes
    splitting each span between consecutive route nodes into pieces each drawn by
    one explicit cubic within the span's ``span_budget`` of the truth
    (``span_pieces``; ``quantisation``, per run, the samples' distance to the
    truth at each run point, ``sample_quantisation``), and one node per part end
    the zone cuts."""
    count = _vertex_nodes(truth, zone, radii, default_radius, anchors, anchored, contacts)
    for number, run in enumerate(truth.ideal_runs):
        count += _run_nodes(run, ~_inside(zone, run.points), tolerance,
                            None if quantisation is None else quantisation[number])[1]
    return count


# Boundary samples farther than this from every ideal run point are not
# attributed to a run: they belong to a boundary no run describes (the frame, or
# a boundary the palette stage folds away).
_SAMPLE_REACH = 2.0


def sample_quantisation(truth, labels: np.ndarray) -> list[np.ndarray]:
    """For each ideal run of ``truth``, per run point, the farthest distance to the
    truth of the boundary samples nearest that point: the pixel-edge vertices (at
    unit spacing, off the frame) of the label map ``labels``, each attributed to
    its nearest ideal run point within ``_SAMPLE_REACH``. 0 where none is."""
    runs = truth.ideal_runs
    if not runs:
        return []
    height, width = labels.shape
    vertices = []
    for geometry in label_geometry(labels).values():
        for polygon in _polygons(geometry):
            for ring in [polygon.exterior, *polygon.interiors]:
                coords = np.asarray(ring.coords)
                starts, ends = coords[:-1], coords[1:]
                steps = np.maximum(1, np.round(np.abs(ends - starts).max(axis=1)).astype(int))
                first = np.repeat(np.cumsum(steps) - steps, steps)
                fraction = (np.arange(int(steps.sum())) - first) / np.repeat(steps, steps)
                vertices.append(np.repeat(starts, steps, axis=0)
                                + np.repeat(ends - starts, steps, axis=0) * fraction[:, None])
    samples = np.unique(np.round(np.vstack(vertices).reshape(-1, 2), 6), axis=0) \
        if vertices else np.empty((0, 2))
    on_frame = ((samples[:, 0] <= 0) | (samples[:, 0] >= width)
                | (samples[:, 1] <= 0) | (samples[:, 1] >= height))
    samples = samples[~on_frame]
    points = np.vstack([run.points for run in runs])
    owner = np.concatenate([np.full(len(run.points), number)
                            for number, run in enumerate(runs)])
    offsets = np.concatenate([[0], np.cumsum([len(run.points) for run in runs])])
    result = [np.zeros(len(run.points)) for run in runs]
    if not len(samples):
        return result
    distance, nearest = cKDTree(points).query(samples, distance_upper_bound=_SAMPLE_REACH)
    for d, index in zip(distance, nearest):
        if not math.isfinite(d):
            continue
        number = int(owner[index])
        local = int(index - offsets[number])
        result[number][local] = max(result[number][local], float(d))
    return result


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
    band width (every count is asserted to lie in its range), which single
    changes each count decides (``decides``: a deletion where the traced count is
    the range's minimum, an addition where it is the maximum), and the isolated
    structures (``topology``)."""
    rows = [_local_topology(geometry, removed, band) for band in bands]
    traced = rows[TOPOLOGY_BAND_MULTIPLES.index(TOPOLOGY_BAND_TRACED)]
    result = dict(traced)
    result["decides"] = {}
    for name in ("polygons", "holes"):
        values = [row[name] for row in rows]
        result[f"{name}_range"] = [min(values), max(values)]
        result["decides"][name] = {"deletion": min(values) == traced[name],
                                   "addition": max(values) == traced[name]}
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
    skip = float(_tip_skip(lengths))
    # The least turn of a round a cusp may sharpen, the largest sharpenable radius
    # (a round turning by that much) and how far from a cusp ``round_turn_near``
    # looks for a round.
    round_turn = minimum_round_turn(lengths)
    round_radius = sharpenable_radius(math.radians(round_turn), allowance, skip)
    rho = round_window(allowance, skip, round_turn)
    rings = truth_rings(truth)
    vertices = np.asarray([corner.point for corner in truth.corners],
                          dtype=np.float64).reshape(-1, 2)

    corners = corner_roles(truth, rings, lengths)
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
    # The label maps: the hard-edged render, and the one the trace received (the
    # render in the labels tier, the palette stage's labels in the rgb tier).
    hard_labels = np.asarray(render_labels(scene))
    evidence_labels = hard_labels
    if tier == "rgb" and run is not None:
        with np.load(run.subject_dir / "segmentation.npz") as bundle:
            evidence_labels = np.asarray(bundle["labels"])
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
        corner_reach=float(_corner_reach(lengths)), tip_skip=skip,
        minimum_round_turn=round_turn,
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
        node_limit=zone_node_limit(truth, excluded, vertex_radii, bound,
                                   route_tolerance(lengths),
                                   sample_quantisation(truth, hard_labels),
                                   evidence_anchors(evidence_labels),
                                   [c["point"] for c in corners if c["kind"] != "corner"],
                                   corner_contacts(truth, corners)),
        topology_truth=topology_truth,
        topology_beyond_px=float(box(0, 0, width, height).area if far_topology is None
                                 else box(0, 0, width, height).difference(far_topology).area))


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
                 exempt=None, tolerance: float = 0.0) -> list[dict]:
    """Every *thin* four-connected evidence region: one most of whose pixels lie in
    no 2 x 2 block of its own pixels (narrower than the resolution floor along
    most of it), with the fraction of those thin pixels' centres its label's
    traced layer covers (``recall``) and how many connected pieces of that layer
    within ``tolerance`` of the region's pixels each cover a viable share of those
    centres (``pieces``, ``_covering_pieces``); a region whose thin pixels all lie
    in ``exempt`` (the vertex discs, where a tip or a corner contact leaves a pixel
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
            mask = component[rows_slice, columns_slice] == region
            rows.append({"label": int(value), "pixels": int(sizes[region]),
                         "pixel": [int(xs[order[0]]), int(ys[order[0]])],
                         "recall": float(inside.mean()),
                         "pieces": _covering_pieces(traced, mask, rows_slice.start,
                                                    columns_slice.start, xs, ys, tolerance)})
    return rows


def _covering_pieces(traced, region: np.ndarray, top: int, left: int, xs: np.ndarray,
                     ys: np.ndarray, tolerance: float) -> int:
    """How many connected pieces of ``traced`` within ``tolerance`` of the pixels
    of ``region`` (a mask whose first row and column are ``top`` and ``left``)
    each cover at least ``max(2, 5%)``, capped at all, of the pixel centres
    ``xs, ys``: the trace stage's own count of the pieces a protected component
    survives as (``validation``'s ``viable_pieces``), which must be one."""
    if traced.is_empty or not len(xs):
        return 0
    rows, columns = np.nonzero(region)
    pixels = shapely.union_all(shapely.box(columns + left, rows + top,
                                           columns + left + 1, rows + top + 1))
    corridor = pixels.buffer(max(tolerance, 0.0) * _CIRCUMSCRIBE, quad_segs=_BUFFER_SEGMENTS)
    floor = min(len(xs), max(2, round(0.05 * len(xs))))
    viable = 0
    for piece in _polygons(traced.intersection(corridor)):
        if piece.area > MIN_PIECE_AREA \
                and int(shapely.contains_xy(piece, xs + 0.5, ys + 0.5).sum()) >= floor:
            viable += 1
    return viable


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
    thin = thin_regions(labels, palette_hex, traced_by_fill, exempt, tolerance)
    misplaced = [row for row in thin
                 if row["recall"] < THIN_RECALL and (row["label"], tuple(row["pixel"])) not in lost]
    judged = lost | {(row["label"], tuple(row["pixel"])) for row in misplaced}
    severed = [row for row in thin
               if row["pieces"] != 1 and (row["label"], tuple(row["pixel"])) not in judged]
    return {"tolerance_px": tolerance, "regions": len(regions), "missing_regions": missing,
            "thin_regions": len(thin), "misplaced_thin_regions": misplaced,
            "severed_thin_regions": severed,
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
    truth's row (``_topology_row``): each count within its range over the band
    widths; each isolated true structure matching exactly one traced structure of
    its kind within the topology tolerance; and no traced structure whose rings
    clear the removed region by ``3 D`` plus that tolerance (so its true
    counterpart would be isolated) left unmatched. ``error`` sums how far each
    count lies outside its range, the unmatched isolated structures and the
    unexpected traced ones."""
    traced = _local_topology(traced_geometry, bounds.topology_removed, bounds.topology_far)
    error = 0
    for name in ("polygons", "holes"):
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
            "decides": {name: dict(value) for name, value in truth_row["decides"].items()},
            "isolated": len(truth_row["isolated"]),
            "missing": located(missing), "unexpected": located(unexpected), "error": error}


# The added structures of ``topology_mutations``: discs of this many D in radius,
# kept this many D (plus the opening margin) clear of every boundary and band they
# must not touch.
_ADDED_RADIUS = 0.3
_ADDED_MARGIN = 0.05
_KIND_COUNT = {"piece": "polygons", "hole": "holes"}


def _added_discs(room, count: int, radius: float, margin: float) -> list:
    """Up to ``count`` disjoint discs of ``radius`` centred in ``room`` (already
    shrunk so each disc keeps its margins), each ``margin`` clear of the others."""
    discs = []
    while len(discs) < count and room is not None and not room.is_empty \
            and room.area > MIN_PIECE_AREA:
        centre = room.representative_point()
        discs.append(centre.buffer(radius, quad_segs=_BUFFER_SEGMENTS))
        room = room.difference(centre.buffer(2 * radius + margin, quad_segs=_BUFFER_SEGMENTS))
    return discs


def topology_mutations(bounds: "CaseBounds") -> list[dict]:
    """The topology assertion judged per colour on the truth as its own trace and
    on the changes the truth decides. Each row is ``{"colour", "mutation",
    "expect", "error"}`` (``expect`` "pass" for the truth, "fail" for a change) or
    ``{"colour", "mutation", "skip"}``, a change the truth cannot decide, with why:

    - deletions: every isolated structure; every structure a count counts where
      that count decides one deletion (its traced count is its range's minimum);
      where it does not, that is reported and the smallest deletion leaving the
      range (``count - minimum + 1`` counted structures at once) is tried;
    - additions (discs ``_ADDED_RADIUS D`` in radius wholly beyond the traced
      band: holes in the colour, or pieces of it inside the other colours): one
      at an isolated place; near the removed region, one where the count decides
      one addition (its traced count is its range's maximum), otherwise that is
      reported and the smallest addition leaving the range (``maximum - count +
      1`` discs) is tried. A change with no room for it on the canvas is reported,
      never passed."""
    rows = []
    bound = bounds.bound
    radius = _ADDED_RADIUS * bound
    margin = _ADDED_MARGIN * bound + _OPENING_MARGIN
    canvas = box(0, 0, bounds.width, bounds.height)
    far, isolation = bounds.topology_far, bounds.topology_isolation
    removed = bounds.topology_removed

    def judged(colour, text, mutated, truth_row):
        verdict = topology_colour_verdict(mutated, truth_row, bounds)
        return {"colour": colour, "mutation": text, "expect": "fail", "error": verdict["error"]}

    for colour, truth_row in sorted(bounds.topology_truth.items()):
        geometry = bounds.truth.colour_geometry[int(colour)]
        verdict = topology_colour_verdict(geometry, truth_row, bounds)
        rows.append({"colour": colour, "mutation": "truth", "expect": "pass",
                     "error": verdict["error"]})
        isolated = {(row["kind"], round(row["point"].x, 6), round(row["point"].y, 6))
                    for row in truth_row["isolated"]}
        pieces = [p for p in _polygons(geometry) if p.area > MIN_PIECE_AREA]
        counted = {"polygons": [], "holes": []}
        for structure in _structures(geometry):
            if structure["kind"] == "hole":
                region = structure["region"]
                mutated = geometry.union(region)
            else:
                region = next(p for p in pieces
                              if p.representative_point().equals(structure["point"]))
                mutated = geometry.difference(region)
            name = _KIND_COUNT[structure["kind"]]
            fewer = _local_topology(mutated, removed, far)[name] < truth_row[name]
            if fewer:
                counted[name].append(region)
            key = (structure["kind"], round(structure["point"].x, 6),
                   round(structure["point"].y, 6))
            if key in isolated or (fewer and truth_row["decides"][name]["deletion"]):
                rows.append(judged(colour, f"delete the {structure['kind']} at "
                                           f"({structure['point'].x:.2f}, "
                                           f"{structure['point'].y:.2f})", mutated, truth_row))

        for name, kind in (("polygons", "piece"), ("holes", "hole")):
            low, high = truth_row[f"{name}_range"]
            count = truth_row[name]
            label = f"{kind}s"
            if count and not truth_row["decides"][name]["deletion"]:
                needed = count - low + 1
                rows.append({"colour": colour, "mutation": f"delete one counted {kind}",
                             "skip": f"{label} count {count}, range [{low}, {high}] over the "
                                     "band widths: one deletion near the band edge is "
                                     f"undecidable; {needed} at once are tried"})
                text = f"delete {needed} counted {label} at once"
                if len(counted[name]) >= needed:
                    chosen = unary_union(counted[name][:needed])
                    mutated = (geometry.union(chosen) if kind == "hole"
                               else geometry.difference(chosen))
                    rows.append(judged(colour, text, mutated, truth_row))
                else:
                    rows.append({"colour": colour, "mutation": text,
                                 "skip": f"only {len(counted[name])} {label} are counted"})
            if kind == "hole":
                host = geometry
            else:
                host = canvas.difference(_buffer(geometry, radius + margin))
            host = host.buffer(-(radius + margin), quad_segs=_BUFFER_SEGMENTS)
            if far is not None:
                host = host.difference(_buffer(far, radius + margin))
            places = [("isolated", host if isolation is None
                       else host.difference(_buffer(isolation, radius + margin)))]
            if isolation is not None:
                places.append(("near the removed region", host.intersection(isolation)))
            for place, room in places:
                decided = place == "isolated" or truth_row["decides"][name]["addition"]
                needed = 1 if decided else high - count + 1
                where = "at an isolated place" if place == "isolated" else place
                if not decided:
                    rows.append({"colour": colour, "mutation": f"add one {kind} {where}",
                                 "skip": f"{label} count {count}, range [{low}, {high}] over "
                                         "the band widths: one addition near the band edge "
                                         f"is undecidable; {needed} at once are tried"})
                text = f"add {needed} {label if needed > 1 else kind} {where}"
                discs = _added_discs(room, needed, radius, margin)
                if len(discs) < needed:
                    rows.append({"colour": colour, "mutation": text,
                                 "skip": f"no room for {needed} added {label} {where} beyond "
                                         "the traced band"})
                    continue
                added = unary_union(discs)
                mutated = geometry.difference(added) if kind == "hole" else geometry.union(added)
                changed = _local_topology(mutated, removed, far)[name]
                if changed != count + needed:
                    raise ArithmeticError(f"colour {colour}: adding {needed} {label} {where} "
                                          f"counts {changed}, not {count + needed}")
                rows.append(judged(colour, text, mutated, truth_row))
    return rows


def evaluate(case, tier: str, run, bounds: CaseBounds, *, measured: dict | None = None,
             raster_colours: set[int] | None = None, traced: dict | None = None) -> dict:
    """Measure ``run`` (a ``harness.TraceRun`` of ``case`` in ``tier``) against
    ``bounds``: every number ``check`` and the sweep summaries read.
    ``raster_colours`` (the scene colours of the hard-edged label render, which
    are the colours that dominate some pixel) limits the rgb tier's demand that
    a scene colour survive the palette stage to colours the raster carries.
    ``measured`` is ``metrics.measure`` of the run against ``bounds.truth`` with
    ``colour_map(bounds, run)``, when the caller already has it. ``traced`` is
    ``run.edge_svg`` already parsed (``metrics.parse_traced_svg``'s layout), or
    another tracer's output converted to it, which replaces reading the file."""
    scene = case.scene
    truth = bounds.truth
    mapping = colour_map(bounds, run)
    if measured is None:
        # A negative radius and distance make every true vertex a miss and every
        # cusp a kink, so the result lists each one's nearest distance.
        measured = measure(run.edge_svg, truth, scene.palette, corner_radius=-1.0,
                           smooth_min_distance=-1.0, colour_map=mapping, traced=traced)
    if traced is None:
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
        turn = round_turn_near(bounds.rings, (x, y), bounds.allowance, bounds.tip_skip,
                               bounds.minimum_round_turn,
                               [corner["point"] for corner in bounds.corners])
        cusps.append({"point": [float(x), float(y)], "near_corner": near_corner,
                      "in_zone": bool(zoned), "truth_turn_deg": turn,
                      "kink": not near_corner and not zoned
                              and turn < bounds.minimum_round_turn})

    node_points = np.asarray([(x, y) for x, y, _ in nodes], dtype=np.float64).reshape(-1, 2)
    outside_flags = ~_inside(excluded, node_points)
    nodes_outside = int(outside_flags.sum())

    # Topology outside the discs and zones, every colour of the truth or the trace.
    absent = {"polygons": 0, "holes": 0, "polygons_range": [0, 0], "holes_range": [0, 0],
              "decides": {name: {"deletion": True, "addition": True}
                          for name in ("polygons", "holes")}, "isolated": []}
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
        "bound_px": bounds.bound, "minimum_round_turn_deg": bounds.minimum_round_turn,
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
    problems += [f"thin label {row['label']} region of {row['pixels']} px at {row['pixel']}: "
                 f"{row['pieces']} traced pieces cover it, not one"
                 for row in evidence["severed_thin_regions"][:6]]
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
        return count <= limit, (f"{count} boundary nodes outside the zones (limit {limit}, "
                                f"the explicit route through the axis extremes; "
                                f"{result['ideal_nodes']} ideal)")
    if name == "topology":
        wrong = {colour: row for colour, row in result["topology"].items() if row["error"]}
        unmatched = [row["fill"] for row in result["unmatched_layers"]]
        def located(rows):
            return ", ".join(f"{kind} at ({x:.1f}, {y:.1f})" for kind, x, y in rows[:4])

        return not wrong and not unmatched, "; ".join(
            [f"colour {colour}: traced {row['traced']}, truth ranges {row['ranges']}"
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
                 "absent_scene_colours", "misplaced_thin_regions", "severed_thin_regions",
                 "fold_target_mismatches"),
    "palette": ("problems",),
}
# How far an expected failure's measurement may grow before it counts as worse
# (``worse_than_recorded``): a count may not grow at all (one more kink, missed
# vertex or lost region is a regression); a length may grow by half a pixel step
# or 10%, an area by 4 px^2 or 10%, whichever is larger, so a fix elsewhere that
# nudges the geometry by a sub-pixel amount does not trip every entry near it.
WORSE_MARGIN = {"px": (0.5, 0.1), "px2": (4.0, 0.1), "count": (0.0, 0.0)}


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
                  "severed_thin_regions": len(evidence["severed_thin_regions"]),
                  "fold_target_mismatches": len(evidence["fold_target_mismatches"])}
    elif name == "palette":
        values = {"problems": len(_palette_problems(case, result))}
    else:
        raise KeyError(name)
    return {key: float(value) for key, value in values.items()}


def worse_than_recorded(recorded: dict[str, float],
                        measured: dict[str, float]) -> list[str]:
    """The measurements worse than ``recorded`` beyond ``WORSE_MARGIN``, each as a
    readable line. A better measurement is never reported."""
    worse = []
    for key in sorted(measured):
        value, then = float(measured[key]), float(recorded[key])
        absolute, relative = WORSE_MARGIN[measurement_unit(key)]
        if value > then + max(absolute, relative * then):
            worse.append(f"{key} {value:.6g} > recorded {then:.6g}")
    return worse


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
        "severed_thin_regions": len(evidence["severed_thin_regions"]),
        "required_scene_colours": len(evidence["required_scene_colours"]),
        "absent_scene_colours": len(evidence["absent_scene_colours"]),
        "fold_target_mismatches": len(evidence["fold_target_mismatches"]),
    }
