from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import math
from typing import Callable, Literal

import numpy as np
from numba import njit
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
    """One canonical colour interface, reusable in either direction.

    ``anchors`` are its ends: two for an open chain, one for a closed chain
    that leaves a vertex of pair degree other than two and returns to it, and
    none for a ring of degree-two vertices.
    """

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


def _assemble_rings(by_identity: dict[str, SharedBoundaryChain],
                    uses: list[BoundaryChainUse],
                    closed_rings: list[tuple[BoundaryChainUse, ...]],
                    ) -> list[tuple[BoundaryChainUse, ...]]:
    """Traverse oriented open chain uses into closed rings by smallest positive turn.

    Every use keeps the enclosed region on its left, so at each junction the smallest
    positive turn follows that same planar face. Closed chains are self-contained rings.
    """

    def endpoints(use: BoundaryChainUse) -> tuple[Point, Point]:
        points = by_identity[use.identity].points
        return (points[0], points[-1]) if use.forward else (points[-1], points[0])

    def endpoint_direction(use: BoundaryChainUse, incoming: bool) -> Point:
        points = by_identity[use.identity].points
        oriented = points if use.forward else tuple(reversed(points))
        a, b = (oriented[-2], oriented[-1]) if incoming else (oriented[0], oriented[1])
        return b[0] - a[0], b[1] - a[1]

    outgoing: dict[Point, list[BoundaryChainUse]] = {}
    for use in uses:
        start, _ = endpoints(use)
        outgoing.setdefault(start, []).append(use)
    unused = {(use.identity, use.forward): use for use in uses}
    rings: list[tuple[BoundaryChainUse, ...]] = list(closed_rings)
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
                    f"open boundary ring at {cursor}; "
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
        rings.append(tuple(ring_uses))
    return rings


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
    for colour in included:
        uses: list[BoundaryChainUse] = []
        closed_rings: list[tuple[BoundaryChainUse, ...]] = []
        for chain in track.chains:
            if chain.closed and colour in chain.pair:
                forward = chain.left_colour == colour
                closed_rings.append((BoundaryChainUse(chain.identity, forward),))
            elif chain.left_colour == colour:
                uses.append(BoundaryChainUse(chain.identity, True))
            elif chain.right_colour == colour:
                uses.append(BoundaryChainUse(chain.identity, False))
        rings = [ColourBoundaryRing(colour, ring_uses)
                 for ring_uses in _assemble_rings(by_identity, uses, closed_rings)]
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


@njit(cache=True)
def _walk_edge_chains(ia, ib, deg, indptr, csr_dst, csr_eidx):
    """Deterministic edge-chain traversal over an integer-encoded pair graph.

    A chain is a maximal run of edges whose interior vertices all have pair
    degree two: it runs from one vertex whose degree is not two to the next (or
    back to the same one), or it is a ring of degree-two vertices only. So an
    interface is never split at a degree-two vertex, and the chains are the same
    whichever edge seeds them.

    Edges are provided in ascending ``(ia, ib)`` order so the smallest unused
    edge is the earliest still-unused index.  Each chain seeds at that edge.
    When an end of the seed has degree other than two, the walk starts there
    (the lower end when both do).  When both ends have degree two, the seed is
    inside a chain: the walk first traces back from its lower end, away from the
    seed, to the chain's end and then starts there along the edge it traced
    back on; if the trace returns to the seed instead, the chain is a ring and
    starts at the seed's lower end.  From its start the walk goes to the
    smallest available neighbour until it returns to the start or reaches a
    vertex whose pair degree is not two.  Nodes are compact ids whose order
    matches the encoded grid-point order, so every minimum reproduces the
    tuple-order minimum of the reference traversal.
    """
    m = ia.shape[0]
    out_nodes = np.empty(2 * m + 8, dtype=np.int64)
    chain_starts = np.empty(m + 8, dtype=np.int64)
    unused = np.ones(m, dtype=np.bool_)
    n_out = 0
    n_chains = 0
    ptr = 0
    while ptr < m:
        if not unused[ptr]:
            ptr += 1
            continue
        seed_a = ia[ptr]
        seed_b = ib[ptr]
        first_edge = -1
        first_node = -1
        if deg[seed_a] != 2:
            start = seed_a
        elif deg[seed_b] != 2:
            start = seed_b
        else:
            start = seed_a
            # Trace back from seed_a, away from the seed edge, to the chain's end.
            # Every edge of this chain is still unused: a walk that had used one
            # would have continued through its degree-two vertices to the whole chain.
            cursor = seed_a
            arrived_by = ptr
            while True:
                next_edge = -1
                next_node = -1
                for j in range(indptr[cursor], indptr[cursor + 1]):
                    if csr_eidx[j] != arrived_by:
                        next_edge = csr_eidx[j]
                        next_node = csr_dst[j]
                if next_edge == ptr:
                    break  # back at the seed: a ring of degree-two vertices
                if deg[next_node] != 2:
                    start = next_node
                    first_edge = next_edge
                    first_node = cursor
                    break
                cursor = next_node
                arrived_by = next_edge
        chain_starts[n_chains] = n_out
        n_chains += 1
        out_nodes[n_out] = start
        n_out += 1
        current = start
        if first_edge != -1:
            # Leave the chain's end along the traced edge, not its smallest edge,
            # which may belong to another chain meeting there.
            unused[first_edge] = False
            out_nodes[n_out] = first_node
            n_out += 1
            current = first_node
        while True:
            best = -1
            best_edge = -1
            for j in range(indptr[current], indptr[current + 1]):
                edge = csr_eidx[j]
                if unused[edge]:
                    neighbour = csr_dst[j]
                    if best_edge == -1 or neighbour < best:
                        best = neighbour
                        best_edge = edge
            if best_edge == -1:
                break
            unused[best_edge] = False
            out_nodes[n_out] = best
            n_out += 1
            current = best
            if current == start or deg[current] != 2:
                break
    return out_nodes[:n_out], chain_starts[:n_chains]


def _pair_edge_chains(edge_lo: np.ndarray, edge_hi: np.ndarray
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Chains for one colour pair; inputs sorted ascending by ``(lo, hi)``.

    Returns the encoded grid-point keys visited in traversal order, the start
    offset of each chain within that flat sequence, and whether each chain
    starts at a vertex whose pair degree is not two (a chain end rather than a
    point on a ring of degree-two vertices).
    """
    nodes = np.unique(np.concatenate((edge_lo, edge_hi)))
    ia = np.searchsorted(nodes, edge_lo).astype(np.int64)
    ib = np.searchsorted(nodes, edge_hi).astype(np.int64)
    vertices = nodes.shape[0]
    deg = (np.bincount(ia, minlength=vertices)
           + np.bincount(ib, minlength=vertices)).astype(np.int64)
    edge_count = ia.shape[0]
    src = np.concatenate((ia, ib))
    dst = np.concatenate((ib, ia)).astype(np.int64)
    eidx = np.concatenate((np.arange(edge_count, dtype=np.int64),
                           np.arange(edge_count, dtype=np.int64)))
    order = np.argsort(src, kind="stable")
    src_sorted = src[order]
    counts = np.bincount(src_sorted, minlength=vertices)
    indptr = np.empty(vertices + 1, dtype=np.int64)
    indptr[0] = 0
    np.cumsum(counts, out=indptr[1:])
    csr_dst = dst[order]
    csr_eidx = eidx[order]
    out_nodes, chain_starts = _walk_edge_chains(
        ia, ib, deg, indptr, csr_dst, csr_eidx)
    return nodes[out_nodes], chain_starts, deg[out_nodes[chain_starts]] != 2


def shared_boundary_polygons(labels: np.ndarray, included: list[int],
                             palette_costs: np.ndarray,
                             protected_features: np.ndarray | None = None,
                             checkpoint: Callable[[], None] | None = None,
                             origin: tuple[int, int] = (0, 0),
                             ) -> tuple[dict[int, object], SharedBoundaryTrack, dict]:
    """Build the exact dense evidence for a shared-boundary planar partition.

    This stage deliberately performs no geometric simplification.  Its job is
    to preserve canonical, sub-pixel-adjusted samples, colour ownership,
    topological anchors, and protected-feature evidence.  Line/curve
    classification and fitting belong to the vector fitting stage.

    ``checkpoint``, when given, is called between phases and every few hundred
    chains; it may raise to abandon the build (the live preview cancels a
    superseded window this way). It never affects the result.

    ``origin`` is the image position ``(x, y)`` of ``labels[0, 0]`` when the
    arrays are a crop of a larger image. Samples, anchors, chain identities,
    faces and the canvas are then in that image's coordinates, and each sample
    is formed exactly as it would be from the whole image: a boundary the crop
    does not cut (none of its samples within 1 px of a crop edge that is not an
    image edge; a junction on that edge loses its outside colours and becomes an
    endpoint) gets the same chain, bit for bit, as in a whole-image build.
    """
    check = checkpoint or (lambda: None)
    height, width = labels.shape
    outside = -1
    height_stride = height + 2
    edge_multiplier = height_stride * (width + 2) + 1

    if palette_costs.ndim != 3 or palette_costs.shape[:2] != labels.shape:
        raise ValueError("palette costs must have shape (height, width, colours)")
    if labels.size and palette_costs.shape[2] <= int(labels.max()):
        raise ValueError("palette costs do not cover every segmentation label")
    if protected_features is not None and protected_features.shape != labels.shape:
        raise ValueError("protected-feature evidence must match label dimensions")

    def _eid(a: tuple[int, int], b: tuple[int, int]) -> int:
        key_a = a[0] * height_stride + a[1]
        key_b = b[0] * height_stride + b[1]
        if key_a > key_b:
            key_a, key_b = key_b, key_a
        return key_a * edge_multiplier + key_b

    def _offsets(first_difference: np.ndarray, second_difference: np.ndarray
                 ) -> np.ndarray:
        """Sub-pixel crossing offset per edge, matched to the scalar reference.

        The differences are formed in the palette-cost dtype and widened, the
        crossing is the intercept of the two colour-cost differences clamped to
        the interior of the edge, and the offset recentres it on the midpoint.
        """
        first_difference = first_difference.astype(np.float64)
        second_difference = second_difference.astype(np.float64)
        denominator = first_difference - second_difference
        near_zero = np.abs(denominator) < 1e-12
        safe = np.where(near_zero, 1.0, denominator)
        crossing = np.where(near_zero, 0.5, first_difference / safe)
        # A NaN crossing (non-finite costs) clamps to the lower bound, as the
        # scalar ``min(0.95, max(0.05, crossing))`` form does.
        crossing = np.where(np.isnan(crossing), 0.05,
                            np.minimum(0.95, np.maximum(0.05, crossing)))
        return crossing - 0.5

    key_lo: list[np.ndarray] = []
    key_hi: list[np.ndarray] = []
    pair_lo: list[np.ndarray] = []
    pair_hi: list[np.ndarray] = []
    x_offset_keys: list[np.ndarray] = []
    x_offset_values: list[np.ndarray] = []
    y_offset_keys: list[np.ndarray] = []
    y_offset_values: list[np.ndarray] = []
    protected_ids: set[int] = set()

    vertical_y, vertical_x = np.nonzero(labels[:, :-1] != labels[:, 1:])
    vertical_y = vertical_y.astype(np.int64)
    vertical_x = vertical_x.astype(np.int64)
    if vertical_y.size:
        left = labels[vertical_y, vertical_x].astype(np.int64)
        right = labels[vertical_y, vertical_x + 1].astype(np.int64)
        low = np.minimum(left, right)
        high = np.maximum(left, right)
        offset = _offsets(
            palette_costs[vertical_y, vertical_x, low]
            - palette_costs[vertical_y, vertical_x, high],
            palette_costs[vertical_y, vertical_x + 1, low]
            - palette_costs[vertical_y, vertical_x + 1, high])
        top_key = (vertical_x + 1) * height_stride + vertical_y
        bottom_key = (vertical_x + 1) * height_stride + (vertical_y + 1)
        key_lo.append(top_key)
        key_hi.append(bottom_key)
        pair_lo.append(low)
        pair_hi.append(high)
        x_offset_keys.append(np.concatenate((top_key, bottom_key)))
        x_offset_values.append(np.concatenate((offset, offset)))
        if protected_features is not None:
            mask = (protected_features[vertical_y, vertical_x]
                    | protected_features[vertical_y, vertical_x + 1])
            protected_ids.update(
                (top_key[mask] * edge_multiplier + bottom_key[mask]).tolist())

    horizontal_y, horizontal_x = np.nonzero(labels[:-1, :] != labels[1:, :])
    horizontal_y = horizontal_y.astype(np.int64)
    horizontal_x = horizontal_x.astype(np.int64)
    if horizontal_y.size:
        upper = labels[horizontal_y, horizontal_x].astype(np.int64)
        lower = labels[horizontal_y + 1, horizontal_x].astype(np.int64)
        low = np.minimum(upper, lower)
        high = np.maximum(upper, lower)
        offset = _offsets(
            palette_costs[horizontal_y, horizontal_x, low]
            - palette_costs[horizontal_y, horizontal_x, high],
            palette_costs[horizontal_y + 1, horizontal_x, low]
            - palette_costs[horizontal_y + 1, horizontal_x, high])
        left_key = horizontal_x * height_stride + (horizontal_y + 1)
        right_key = (horizontal_x + 1) * height_stride + (horizontal_y + 1)
        key_lo.append(left_key)
        key_hi.append(right_key)
        pair_lo.append(low)
        pair_hi.append(high)
        y_offset_keys.append(np.concatenate((left_key, right_key)))
        y_offset_values.append(np.concatenate((offset, offset)))
        if protected_features is not None:
            mask = (protected_features[horizontal_y, horizontal_x]
                    | protected_features[horizontal_y + 1, horizontal_x])
            protected_ids.update(
                (left_key[mask] * edge_multiplier + right_key[mask]).tolist())

    if height and width:
        columns = np.arange(width, dtype=np.int64)
        rows = np.arange(height, dtype=np.int64)
        border_specs = (
            (columns * height_stride, (columns + 1) * height_stride,
             labels[0, :].astype(np.int64)),
            (columns * height_stride + height, (columns + 1) * height_stride + height,
             labels[-1, :].astype(np.int64)),
            (rows, rows + 1, labels[:, 0].astype(np.int64)),
            (width * height_stride + rows, width * height_stride + rows + 1,
             labels[:, -1].astype(np.int64)),
        )
        for lo_key, hi_key, border_labels in border_specs:
            key_lo.append(lo_key)
            key_hi.append(hi_key)
            pair_lo.append(np.full(border_labels.shape[0], outside, dtype=np.int64))
            pair_hi.append(border_labels)

    def _accumulate(keys: list[np.ndarray], values: list[np.ndarray]
                    ) -> tuple[np.ndarray, np.ndarray]:
        if not keys:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
        flat_keys = np.concatenate(keys)
        flat_values = np.concatenate(values)
        unique_keys, inverse = np.unique(flat_keys, return_inverse=True)
        totals = np.zeros(unique_keys.shape[0], dtype=np.float64)
        np.add.at(totals, inverse, flat_values)
        counts = np.bincount(inverse, minlength=unique_keys.shape[0])
        return unique_keys, totals / counts

    check()
    x_keys, x_means = _accumulate(x_offset_keys, x_offset_values)
    y_keys, y_means = _accumulate(y_offset_keys, y_offset_values)
    subpixel_count = int(np.union1d(x_keys, y_keys).shape[0])

    if key_lo:
        edge_key_lo = np.concatenate(key_lo)
        edge_key_hi = np.concatenate(key_hi)
        edge_pair_lo = np.concatenate(pair_lo)
        edge_pair_hi = np.concatenate(pair_hi)
        endpoint_keys = np.unique(np.concatenate((edge_key_lo, edge_key_hi)))
    else:
        edge_key_lo = np.empty(0, dtype=np.int64)
        edge_key_hi = np.empty(0, dtype=np.int64)
        edge_pair_lo = np.empty(0, dtype=np.int64)
        edge_pair_hi = np.empty(0, dtype=np.int64)
        endpoint_keys = np.empty(0, dtype=np.int64)

    origin_x, origin_y = int(origin[0]), int(origin[1])
    # The integer grid position is moved to the image before it becomes a float,
    # so a sample is the whole image's ``float(x) + offset``, not a translated one.
    fitted_x = (endpoint_keys // height_stride + origin_x).astype(np.float64)
    fitted_y = (endpoint_keys % height_stride + origin_y).astype(np.float64)
    if x_keys.size:
        fitted_x[np.searchsorted(endpoint_keys, x_keys)] += x_means
    if y_keys.size:
        fitted_y[np.searchsorted(endpoint_keys, y_keys)] += y_means
    fitted_by_key = {int(key): (float(fx), float(fy)) for key, fx, fy
                     in zip(endpoint_keys.tolist(), fitted_x.tolist(),
                            fitted_y.tolist())}

    lines: list[LineString] = []
    tracked_chains: list[SharedBoundaryChain] = []
    chain_records: list[dict] = []

    inventory_pairs: list[tuple[int, int]] = []
    inventory_grid: list[list[GridPoint]] = []
    inventory_fitted: list[list[Point]] = []
    # A closed chain that leaves a vertex of pair degree other than two and returns
    # to it (a loop round one region touching another at a corner) has that vertex
    # as both of its ends, so it carries an anchor there like any open chain's end.
    inventory_node_loops: list[bool] = []
    if edge_key_lo.size:
        order = np.lexsort((edge_key_hi, edge_key_lo, edge_pair_hi, edge_pair_lo))
        sorted_pair_lo = edge_pair_lo[order]
        sorted_pair_hi = edge_pair_hi[order]
        sorted_key_lo = edge_key_lo[order]
        sorted_key_hi = edge_key_hi[order]
        boundary = np.ones(sorted_pair_lo.shape[0], dtype=bool)
        boundary[1:] = ((sorted_pair_lo[1:] != sorted_pair_lo[:-1])
                        | (sorted_pair_hi[1:] != sorted_pair_hi[:-1]))
        starts = np.nonzero(boundary)[0]
        ends = np.append(starts[1:], sorted_pair_lo.shape[0])
        for run_start, run_end in zip(starts.tolist(), ends.tolist()):
            check()
            pair = (int(sorted_pair_lo[run_start]), int(sorted_pair_hi[run_start]))
            keys, chain_starts, starts_at_end = _pair_edge_chains(
                sorted_key_lo[run_start:run_end], sorted_key_hi[run_start:run_end])
            key_list = keys.tolist()
            offsets = chain_starts.tolist()
            offsets.append(len(key_list))
            for index in range(len(offsets) - 1):
                segment = key_list[offsets[index]:offsets[index + 1]]
                inventory_pairs.append(pair)
                inventory_node_loops.append(
                    segment[0] == segment[-1] and bool(starts_at_end[index]))
                inventory_grid.append(
                    [(key // height_stride, key % height_stride) for key in segment])
                inventory_fitted.append([fitted_by_key[key] for key in segment])

    incident_colours: dict[GridPoint, set[int]] = {}
    for pair, chain, node_loop in zip(inventory_pairs, inventory_grid, inventory_node_loops):
        if chain[0] != chain[-1] or node_loop:
            for point in (chain[0], chain[-1]):
                incident_colours.setdefault(point, set()).update(pair)

    for chain_count, (pair, chain, fitted_chain, node_loop) in enumerate(
            zip(inventory_pairs, inventory_grid, inventory_fitted, inventory_node_loops)):
        if chain_count % 256 == 0:
            check()
        loop_node = chain[0] if node_loop else None
        loop_node_fitted = fitted_chain[0] if node_loop else None
        chain, fitted_chain = _canonical_samples(chain, fitted_chain)
        line = LineString(fitted_chain)
        protected_edge_indices = tuple(
            index for index in range(len(chain) - 1)
            if _eid(chain[index], chain[index + 1]) in protected_ids)
        lines.append(line)
        identity_source = f"{pair[0]}:{pair[1]}|" + ";".join(
            f"{x + origin_x},{y + origin_y}" for x, y in chain)
        identity = hashlib.sha256(identity_source.encode("ascii")).hexdigest()[:20]
        anchors: list[BoundaryAnchor] = []
        if chain[0] != chain[-1]:
            ends = ((chain[0], fitted_chain[0]), (chain[-1], fitted_chain[-1]))
        elif loop_node is not None:
            ends = ((loop_node, loop_node_fitted),)
        else:
            ends = ()
        for grid_point, fitted in ends:
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

    check()
    faces = list(polygonize(unary_union(lines)))
    check()
    by_colour: dict[int, list[Polygon]] = {index: [] for index in included}

    def face_colour(face) -> int:
        point = face.representative_point()
        x = min(width - 1, max(0, int(math.floor(point.x)) - origin_x))
        y = min(height - 1, max(0, int(math.floor(point.y)) - origin_y))
        return int(labels[y, x])

    face_colours = [face_colour(face) for face in faces]
    for face, index in zip(faces, face_colours):
        if index in by_colour:
            by_colour[index].append(face)
    included_face_list = [face for face, index in zip(faces, face_colours)
                          if index in included]
    # The per-colour unions and the whole-partition union are independent GEOS
    # work that releases the GIL, so they run on threads; the coverage union
    # follows once every colour geometry exists.
    with ThreadPoolExecutor(max_workers=4) as executor:
        included_future = executor.submit(unary_union, included_face_list)
        colour_futures = {index: executor.submit(unary_union, parts)
                          for index, parts in by_colour.items() if parts}
        geometry = {index: colour_futures[index].result() if index in colour_futures
                    else Polygon() for index in by_colour}
        covered = unary_union(list(geometry.values()))
        included_faces = included_future.result()
    canvas = box(origin_x, origin_y, origin_x + width, origin_y + height)
    diagnostics = {"boundary_chains": len(inventory_pairs), "polygonized_faces": len(faces),
                   "uncovered_area": float(canvas.difference(covered).area),
                   "partition_gap_area": float(included_faces.difference(covered).area),
                   "overlap_area": float(sum(g.area for g in geometry.values()) - covered.area),
                   "subpixel_adjusted_vertices": subpixel_count,
                   "dense_boundary_samples": sum(len(chain.source_points)
                                                   for chain in tracked_chains),
                   "protected_boundary_chains": sum(
                       bool(chain.protected_source_edges) for chain in tracked_chains),
                   "chains": chain_records}
    return geometry, SharedBoundaryTrack(tuple(tracked_chains)), diagnostics
