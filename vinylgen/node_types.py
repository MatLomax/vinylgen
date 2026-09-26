"""Path node types of traced SVG paths, written as ``sodipodi:nodetypes``.

Terms follow GLOSSARY.md "Path nodes". Each node gets one character:

- ``c``, a **cusp node**: the node's two tangent directions are not at 180 degrees
  within ``ANGLE_TOLERANCE_DEG``, or both sides are straight lines, or the node is a
  junction anchor (three or more colours meet there) or an endpoint anchor (a
  chain end where fewer than three colours meet: the image edge, a corner contact
  between two regions of one colour, or a live-preview crop edge).
- ``s``, a **symmetric node** (Inkscape's *smooth*): the two tangent directions are at
  180 degrees within ``ANGLE_TOLERANCE_DEG`` and the handle lengths differ, or one side
  is a straight line.
- ``z``, a **smooth node** (Inkscape's *symmetric*): as ``s``, and both sides are cubics
  whose handle lengths are equal within ``LENGTH_TOLERANCE`` of the longer one.

A line's direction is its tangent. A zero-length cubic handle falls back to the
direction toward that segment's next control point, then its far end point.

The classification reads the path data exactly as written to the SVG, so the types
describe the file Inkscape (and the web canvas's Nodes overlay) sees. The string
follows Inkscape's layout: one character per node of each subpath in order; a closed
subpath repeats its start node's type at its end, and its start node is classified
from the closing join.
"""

from __future__ import annotations

import math
import re
from typing import Iterable

Point = tuple[float, float]
Segment = tuple  # ("L", end) or ("C", c1, c2, end)

ANGLE_TOLERANCE_DEG = 0.5
LENGTH_TOLERANCE = 0.01

_TOKEN = re.compile(r"[MLCZmlcz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def point_key(point: Point) -> str:
    """The text a point is written as in traced path data (``curve_fit._fmt``)."""
    return f"{point[0]:.10g} {point[1]:.10g}"


def junction_keys(track) -> frozenset[str]:
    """The written positions of every junction anchor of a shared-boundary track."""
    return frozenset(point_key(anchor.point) for chain in track.chains
                     for anchor in chain.anchors if anchor.kind == "junction")


def cusp_anchor_keys(track) -> frozenset[str]:
    """The written positions of the anchors that are always cusp nodes: junctions
    (three or more colours meet) and endpoints, the other chain ends: where a
    boundary meets the image edge, which cuts the shape there, so the node is a
    corner of the cut outline even when the traced edge happens to meet the frame
    tangentially; where two regions of one colour touch only at a corner, which the
    outline turns to pass (a closed loop round one of them carries its one anchor
    there); and in the live preview a crop edge."""
    return frozenset(point_key(anchor.point) for chain in track.chains
                     for anchor in chain.anchors)


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 0 else None


def _incoming(start: Point, segment: Segment) -> tuple[tuple[float, float] | None, float]:
    """Direction of travel arriving at the segment's end, and its end handle's length."""
    end = segment[-1]
    if segment[0] == "L":
        return _unit(end[0] - start[0], end[1] - start[1]), 0.0
    c1, c2 = segment[1], segment[2]
    handle = math.hypot(end[0] - c2[0], end[1] - c2[1])
    for other in (c2, c1, start):
        direction = _unit(end[0] - other[0], end[1] - other[1])
        if direction is not None:
            return direction, handle
    return None, handle


def _outgoing(node: Point, segment: Segment) -> tuple[tuple[float, float] | None, float]:
    """Direction of travel leaving the node along the segment, and its start handle's length."""
    if segment[0] == "L":
        end = segment[1]
        return _unit(end[0] - node[0], end[1] - node[1]), 0.0
    c1 = segment[1]
    handle = math.hypot(c1[0] - node[0], c1[1] - node[1])
    for other in segment[1:]:
        direction = _unit(other[0] - node[0], other[1] - node[1])
        if direction is not None:
            return direction, handle
    return None, handle


def classify_join(previous_start: Point, previous: Segment, following: Segment,
                  junction: bool = False) -> str:
    """The type of the node where ``previous`` (starting at ``previous_start``) meets ``following``."""
    if junction or (previous[0] == "L" and following[0] == "L"):
        return "c"
    node = previous[-1]
    arriving, arriving_handle = _incoming(previous_start, previous)
    leaving, leaving_handle = _outgoing(node, following)
    if arriving is None or leaving is None:
        return "c"
    cross = arriving[0] * leaving[1] - arriving[1] * leaving[0]
    dot = arriving[0] * leaving[0] + arriving[1] * leaving[1]
    if math.degrees(math.atan2(abs(cross), dot)) > ANGLE_TOLERANCE_DEG:
        return "c"
    if previous[0] == "L" or following[0] == "L":
        return "s"
    longer = max(arriving_handle, leaving_handle)
    if longer > 0 and abs(arriving_handle - leaving_handle) <= LENGTH_TOLERANCE * longer:
        return "z"
    return "s"


def subpath_nodetypes(start: Point, segments: list[Segment], closed: bool,
                      junctions: Iterable[str] = frozenset()) -> str:
    """The node-type characters of one subpath, laid out as Inkscape reads them.

    A closed subpath whose last segment ends away from its start gets the implicit
    closing line as one more segment, exactly as a renderer draws it.
    """
    junctions = junctions if isinstance(junctions, (set, frozenset)) else frozenset(junctions)
    segments = list(segments)
    if closed and segments and segments[-1][-1] != start:
        segments.append(("L", start))
    if not segments:
        # Inkscape drops a subpath without segments before reading the types.
        return ""
    starts = [start, *(segment[-1] for segment in segments[:-1])]
    inner = [classify_join(starts[index], segments[index], segments[index + 1],
                           point_key(segments[index][-1]) in junctions)
             for index in range(len(segments) - 1)]
    if not closed:
        return "c" + "".join(inner) + "c"
    first = classify_join(starts[-1], segments[-1], segments[0], point_key(start) in junctions)
    return first + "".join(inner) + first


def parse_path(d: str) -> list[tuple[Point, list[Segment], bool]]:
    """Split traced path data into ``(start, segments, closed)`` subpaths.

    Traced paths are written with absolute ``M``, ``L``, ``C`` and ``Z`` only; any
    other command is an error rather than a silently wrong type string.
    """
    tokens = _TOKEN.findall(d)
    if "".join(tokens).replace(" ", "") != re.sub(r"[\s,]+", "", d):
        raise ValueError(f"unsupported path data: {d[:80]!r}")
    subpaths: list[tuple[Point, list[Segment], bool]] = []
    index, command = 0, None
    current: tuple[Point, list[Segment]] | None = None

    def number() -> float:
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    def point() -> Point:
        return (number(), number())

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            if token.islower():
                raise ValueError(f"relative path command {token!r} is not traced output")
            command = token
            index += 1
            if command == "Z":
                if current is None:
                    raise ValueError("Z without a subpath")
                subpaths.append((current[0], current[1], True))
                current = None
            continue
        if command == "M":
            if current is not None:
                subpaths.append((current[0], current[1], False))
            current = (point(), [])
            command = "L"
        elif command == "L" and current is not None:
            current[1].append(("L", point()))
        elif command == "C" and current is not None:
            current[1].append(("C", point(), point(), point()))
        else:
            raise ValueError(f"path data {d[:80]!r} has coordinates without a command")
    if current is not None:
        subpaths.append((current[0], current[1], False))
    return subpaths


def path_nodetypes(d: str, junctions: Iterable[str] = frozenset()) -> str:
    """The ``sodipodi:nodetypes`` value of traced path data ``d``."""
    junctions = frozenset(junctions)
    return "".join(subpath_nodetypes(start, segments, closed, junctions)
                   for start, segments, closed in parse_path(d))
