from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Literal

import numpy as np
from scipy import ndimage
from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union


Point = tuple[float, float]
GridPoint = tuple[int, int]


@dataclass(frozen=True)
class BoundaryAnchor:
    point: Point
    kind: Literal["endpoint", "junction"]


@dataclass(frozen=True)
class SharedBoundaryChain:
    """One canonical colour interface, reusable in either direction."""

    identity: str
    pair: tuple[int, int]
    left_colour: int
    right_colour: int
    points: tuple[Point, ...]
    source_points: tuple[Point, ...]
    anchors: tuple[BoundaryAnchor, ...]
    protected_source_edges: tuple[int, ...]
    closed: bool


@dataclass(frozen=True)
class SharedBoundaryTrack:
    chains: tuple[SharedBoundaryChain, ...]

    @property
    def hard_anchor_points(self) -> set[Point]:
        return {anchor.point for chain in self.chains for anchor in chain.anchors}


@dataclass(frozen=True)
class BoundaryChainUse:
    """An oriented reference; geometry remains owned by SharedBoundaryChain."""

    identity: str
    forward: bool


@dataclass(frozen=True)
class ColourBoundaryRing:
    colour: int
    uses: tuple[BoundaryChainUse, ...]


def assemble_colour_rings(
        track: SharedBoundaryTrack, included: list[int],
        ) -> dict[int, tuple[ColourBoundaryRing, ...]]:
    """Traverse canonical shared chains into deterministic closed colour rings.

    Each chain is fitted once. A colour on its left consumes the canonical fit;
    a colour on its right consumes the exact reverse. At junctions the smallest
    positive turn follows the planar face with that colour on the left.
    """
    by_identity = {chain.identity: chain for chain in track.chains}
    result: dict[int, tuple[ColourBoundaryRing, ...]] = {}

    def endpoints(use: BoundaryChainUse) -> tuple[Point, Point]:
        points = by_identity[use.identity].points
        return (points[0], points[-1]) if use.forward else (points[-1], points[0])

    def endpoint_direction(use: BoundaryChainUse, incoming: bool) -> Point:
        points = by_identity[use.identity].points
        oriented = points if use.forward else tuple(reversed(points))
        a, b = (oriented[-2], oriented[-1]) if incoming else (oriented[0], oriented[1])
        return b[0] - a[0], b[1] - a[1]

    for colour in included:
        uses: list[BoundaryChainUse] = []
        closed_rings: list[ColourBoundaryRing] = []
        for chain in track.chains:
            if chain.closed and colour in chain.pair:
                forward = chain.left_colour == colour
                closed_rings.append(ColourBoundaryRing(
                    colour, (BoundaryChainUse(chain.identity, forward),)))
            elif chain.left_colour == colour:
                uses.append(BoundaryChainUse(chain.identity, True))
            elif chain.right_colour == colour:
                uses.append(BoundaryChainUse(chain.identity, False))
        outgoing: dict[Point, list[BoundaryChainUse]] = {}
        for use in uses:
            start, _ = endpoints(use)
            outgoing.setdefault(start, []).append(use)
        unused = {(use.identity, use.forward): use for use in uses}
        rings = list(closed_rings)
        while unused:
            first = min(unused.values(), key=lambda use: (endpoints(use)[0], use.identity))
            ring_uses = [first]
            del unused[(first.identity, first.forward)]
            _, cursor = endpoints(first)
            start, _ = endpoints(first)
            while cursor != start:
                candidates = [use for use in outgoing.get(cursor, ())
                              if (use.identity, use.forward) in unused]
                if not candidates:
                    raise RuntimeError(
                        f"open boundary for colour {colour} at {cursor}; "
                        "shared-chain topology is not a closed planar face")
                incoming = endpoint_direction(ring_uses[-1], True)

                def turn_key(use: BoundaryChainUse) -> tuple[float, str]:
                    outgoing_direction = endpoint_direction(use, False)
                    cross = incoming[0] * outgoing_direction[1] - incoming[1] * outgoing_direction[0]
                    dot = incoming[0] * outgoing_direction[0] + incoming[1] * outgoing_direction[1]
                    turn = math.atan2(cross, dot) % (2.0 * math.pi)
                    return turn, use.identity

                following = min(candidates, key=turn_key)
                ring_uses.append(following)
                del unused[(following.identity, following.forward)]
                _, cursor = endpoints(following)
            rings.append(ColourBoundaryRing(colour, tuple(ring_uses)))
        rings.sort(key=lambda ring: tuple((use.identity, use.forward) for use in ring.uses))
        result[colour] = tuple(rings)
    return result


def _canonical_chain(points: list[GridPoint]) -> tuple[GridPoint, ...]:
    """Canonicalize traversal so identity is independent of direction/start."""
    if points[0] != points[-1]:
        forward = tuple(points)
        reverse = tuple(reversed(points))
        return min(forward, reverse)
    ring = points[:-1]
    candidates: list[tuple[GridPoint, ...]] = []
    for direction in (ring, list(reversed(ring))):
        minimum = min(direction)
        for index, point in enumerate(direction):
            if point == minimum:
                rotated = tuple(direction[index:] + direction[:index])
                candidates.append((*rotated, rotated[0]))
    return min(candidates)


def _canonical_samples(grid: list[GridPoint], fitted: list[Point]
                       ) -> tuple[list[GridPoint], list[Point]]:
    canonical = _canonical_chain(grid)
    if grid[0] != grid[-1]:
        return (grid, fitted) if tuple(grid) == canonical else (list(reversed(grid)), list(reversed(fitted)))
    ring_grid, ring_fitted = grid[:-1], fitted[:-1]
    for reverse in (False, True):
        candidate_grid = ring_grid if not reverse else list(reversed(ring_grid))
        candidate_fitted = ring_fitted if not reverse else list(reversed(ring_fitted))
        for index in range(len(candidate_grid)):
            rotated = candidate_grid[index:] + candidate_grid[:index]
            if tuple([*rotated, rotated[0]]) == canonical:
                fitted_rotated = candidate_fitted[index:] + candidate_fitted[:index]
                return [*rotated, rotated[0]], [*fitted_rotated, fitted_rotated[0]]
    raise RuntimeError("failed to canonicalize a closed boundary chain")


def component_sizes(mask: np.ndarray) -> list[int]:
    labels, count = ndimage.label(mask, structure=ndimage.generate_binary_structure(2, 1))
    sizes = np.bincount(labels.ravel())[1:]
    return sorted((int(v) for v in sizes), reverse=True)


def shared_boundary_polygons(labels: np.ndarray, included: list[int],
                             palette_costs: np.ndarray,
                             protected_features: np.ndarray | None = None,
                             ) -> tuple[dict[int, object], SharedBoundaryTrack, dict]:
    """Build the exact dense evidence for a shared-boundary planar partition.

    This stage deliberately performs no geometric simplification.  Its job is
    to preserve canonical, sub-pixel-adjusted samples, colour ownership,
    topological anchors, and protected-feature evidence.  Line/curve
    classification and fitting belong to the vector fitting stage.
    """
    height, width = labels.shape
    outside = -1
    segments: dict[tuple[int, int], set[tuple[tuple[int, int], tuple[int, int]]]] = {}
    vertex_adjustments: dict[tuple[int, int], dict[str, list[float]]] = {}
    protected_grid_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    if palette_costs.ndim != 3 or palette_costs.shape[:2] != labels.shape:
        raise ValueError("palette costs must have shape (height, width, colours)")
    if labels.size and palette_costs.shape[2] <= int(labels.max()):
        raise ValueError("palette costs do not cover every segmentation label")
    if protected_features is not None and protected_features.shape != labels.shape:
        raise ValueError("protected-feature evidence must match label dimensions")

    def add(pair: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> None:
        if a > b:
            a, b = b, a
        segments.setdefault(pair, set()).add((a, b))

    def adjust(points: tuple[tuple[int, int], tuple[int, int]], axis: str,
               first: tuple[int, int], second: tuple[int, int],
               pair: tuple[int, int]) -> None:
        a, b = pair
        first_difference = float(palette_costs[first][a] - palette_costs[first][b])
        second_difference = float(palette_costs[second][a] - palette_costs[second][b])
        denominator = first_difference - second_difference
        crossing = 0.5 if abs(denominator) < 1e-12 else first_difference / denominator
        crossing = min(0.95, max(0.05, crossing))
        offset = crossing - 0.5
        for point in points:
            vertex_adjustments.setdefault(point, {"x": [], "y": []})[axis].append(offset)

    ys, xs = np.nonzero(labels[:, :-1] != labels[:, 1:])
    for y, x in zip(ys.tolist(), xs.tolist()):
        pair = tuple(sorted((int(labels[y, x]), int(labels[y, x + 1]))))
        edge = ((x + 1, y), (x + 1, y + 1))
        add(pair, *edge)
        edge_is_protected = (protected_features is not None and
                             (protected_features[y, x] or protected_features[y, x + 1]))
        if edge_is_protected:
            protected_grid_edges.add(tuple(sorted(edge)))
        adjust(edge, "x", (y, x), (y, x + 1), pair)
    ys, xs = np.nonzero(labels[:-1, :] != labels[1:, :])
    for y, x in zip(ys.tolist(), xs.tolist()):
        pair = tuple(sorted((int(labels[y, x]), int(labels[y + 1, x]))))
        edge = ((x, y + 1), (x + 1, y + 1))
        add(pair, *edge)
        edge_is_protected = (protected_features is not None and
                             (protected_features[y, x] or protected_features[y + 1, x]))
        if edge_is_protected:
            protected_grid_edges.add(tuple(sorted(edge)))
        adjust(edge, "y", (y, x), (y + 1, x), pair)
    for x in range(width):
        add(tuple(sorted((outside, int(labels[0, x])))), (x, 0), (x + 1, 0))
        add(tuple(sorted((outside, int(labels[-1, x])))), (x, height), (x + 1, height))
    for y in range(height):
        add(tuple(sorted((outside, int(labels[y, 0])))), (0, y), (0, y + 1))
        add(tuple(sorted((outside, int(labels[y, -1])))), (width, y), (width, y + 1))

    lines: list[LineString] = []
    tracked_chains: list[SharedBoundaryChain] = []
    chain_records: list[dict] = []

    def edge_chains(edges: set[tuple[tuple[int, int], tuple[int, int]]]
                    ) -> list[list[tuple[int, int]]]:
        adjacency: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for a, b in edges:
            adjacency.setdefault(a, []).append(b)
            adjacency.setdefault(b, []).append(a)
        unused = set(edges)
        result: list[list[tuple[int, int]]] = []
        while unused:
            seed = min(unused)
            endpoints = [point for point in seed if len(adjacency[point]) != 2]
            start = min(endpoints) if endpoints else seed[0]
            chain = [start]
            current = start
            while True:
                candidates = [neighbour for neighbour in adjacency[current]
                              if tuple(sorted((current, neighbour))) in unused]
                if not candidates:
                    break
                neighbour = min(candidates)
                unused.remove(tuple(sorted((current, neighbour))))
                chain.append(neighbour)
                current = neighbour
                if current == start or len(adjacency[current]) != 2:
                    break
            result.append(chain)
        return result

    chain_inventory = [(pair, chain) for pair in sorted(segments)
                       for chain in edge_chains(segments[pair])]
    incident_colours: dict[GridPoint, set[int]] = {}
    for pair, chain in chain_inventory:
        for point in (chain[0], chain[-1]) if chain[0] != chain[-1] else ():
            incident_colours.setdefault(point, set()).update(pair)
    def fitted_point(point: tuple[int, int]) -> tuple[float, float]:
        values = vertex_adjustments.get(point, {"x": [], "y": []})
        dx = float(np.mean(values["x"])) if values["x"] else 0.0
        dy = float(np.mean(values["y"])) if values["y"] else 0.0
        return float(point[0]) + dx, float(point[1]) + dy

    for chain_count, (pair, chain) in enumerate(chain_inventory):
        fitted_chain = [fitted_point(point) for point in chain]
        chain, fitted_chain = _canonical_samples(chain, fitted_chain)
        line = LineString(fitted_chain)
        protected_edge_indices = tuple(
            index for index in range(len(chain) - 1)
            if tuple(sorted((chain[index], chain[index + 1]))) in protected_grid_edges)
        lines.append(line)
        identity_source = f"{pair[0]}:{pair[1]}|" + ";".join(
            f"{x},{y}" for x, y in chain)
        identity = hashlib.sha256(identity_source.encode("ascii")).hexdigest()[:20]
        anchors: list[BoundaryAnchor] = []
        if chain[0] != chain[-1]:
            for grid_point, fitted in ((chain[0], fitted_chain[0]),
                                       (chain[-1], fitted_chain[-1])):
                colours = incident_colours.get(grid_point, set()) - {outside}
                kind = "junction" if len(colours) >= 3 else "endpoint"
                anchors.append(BoundaryAnchor(fitted, kind))
        first, second = chain[0], chain[1]
        midpoint = ((first[0] + second[0]) / 2.0,
                    (first[1] + second[1]) / 2.0)
        normal = (-(second[1] - first[1]), second[0] - first[0])

        def colour_at(sample: Point) -> int:
            x, y = math.floor(sample[0]), math.floor(sample[1])
            return int(labels[y, x]) if 0 <= x < width and 0 <= y < height else outside

        left_colour = colour_at((midpoint[0] + normal[0] * 0.25,
                                 midpoint[1] + normal[1] * 0.25))
        right_colour = colour_at((midpoint[0] - normal[0] * 0.25,
                                  midpoint[1] - normal[1] * 0.25))
        tracked_chains.append(SharedBoundaryChain(
            identity=identity, pair=pair, left_colour=left_colour,
            right_colour=right_colour,
            points=tuple((float(x), float(y)) for x, y in line.coords),
            source_points=tuple(fitted_chain), anchors=tuple(anchors),
            protected_source_edges=protected_edge_indices,
            closed=chain[0] == chain[-1]))
        bounds = line.bounds
        chain_records.append({"id": chain_count, "pair": pair,
                              "bounds": tuple(map(float, bounds)),
                              "protected_evidence": bool(protected_edge_indices),
                              "source_samples": len(fitted_chain),
                              "protected_source_edges": len(protected_edge_indices)})

    faces = list(polygonize(unary_union(lines)))
    by_colour: dict[int, list[Polygon]] = {index: [] for index in included}
    for face in faces:
        point = face.representative_point()
        x = min(width - 1, max(0, int(math.floor(point.x))))
        y = min(height - 1, max(0, int(math.floor(point.y))))
        index = int(labels[y, x])
        if index in by_colour:
            by_colour[index].append(face)
    geometry = {index: unary_union(parts) if parts else Polygon()
                for index, parts in by_colour.items()}
    covered = unary_union(list(geometry.values()))
    canvas = box(0, 0, width, height)
    included_faces = unary_union([face for face in faces if int(labels[
        min(height - 1, max(0, int(math.floor(face.representative_point().y)))),
        min(width - 1, max(0, int(math.floor(face.representative_point().x))))]) in included])
    diagnostics = {"boundary_chains": len(chain_inventory), "polygonized_faces": len(faces),
                   "uncovered_area": float(canvas.difference(covered).area),
                   "partition_gap_area": float(included_faces.difference(covered).area),
                   "overlap_area": float(sum(g.area for g in geometry.values()) - covered.area),
                   "subpixel_adjusted_vertices": len(vertex_adjustments),
                   "dense_boundary_samples": sum(len(chain.source_points)
                                                   for chain in tracked_chains),
                   "protected_boundary_chains": sum(
                       bool(chain.protected_source_edges) for chain in tracked_chains),
                   "chains": chain_records}
    return geometry, SharedBoundaryTrack(tuple(tracked_chains)), diagnostics
