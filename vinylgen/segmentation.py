from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from numba import njit, prange
from scipy import ndimage

from vinylgen.parallel_guard import KERNEL_LOCK as _KERNEL_LOCK
from vinylgen.ridge import two_sided_dark_ridge_response


def detect_intentional_strokes(
    source_lab: np.ndarray,
    labels: np.ndarray,
    costs: np.ndarray,
    centres: np.ndarray,
    minimum_contrast: float = 12.0,
    minimum_length: int = 8,
    locked_features: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, dict[str, object]]:
    """Detect coherent dark ridges before any destructive region cleanup.

    A stroke must be darker than samples on *both* sides at one of several
    widths.  Testing eight normals and three scales handles curved, diagonal,
    attached and varying-width marks; connected ridge support rejects isolated
    high-contrast quantisation specks.  The returned mask marks the actual
    palette-labelled stroke band, not merely a centreline.
    """
    lightness = source_lab[..., 0].astype(np.float32)
    height, width = labels.shape
    # The strongest two-sided dark-ridge contrast per pixel over eight normals
    # and radii 1/2/3, with the winning radius; a fused parallel kernel.
    response, best_radius = two_sided_dark_ridge_response(lightness)

    # A real dark stroke should also be assigned confidently enough that it is
    # not just a halfway blend between two palette colours. The gap between the
    # two smallest per-palette costs is the parallel kernel's exact output for
    # ``np.partition(costs, 1, axis=2)[..., 1] - [..., 0]`` (see its docstring).
    with _KERNEL_LOCK:
        margin = _two_smallest_margin(np.ascontiguousarray(costs))
    raw_seeds = (response >= minimum_contrast) & (margin >= 2.0)
    seeds = ndimage.binary_closing(raw_seeds, structure=np.ones((3, 3)), iterations=1)
    components, count = ndimage.label(seeds, structure=np.ones((3, 3)))
    objects = ndimage.find_objects(components)
    normalized = labels.copy()
    locked = (np.zeros_like(seeds) if locked_features is None
              else np.asarray(locked_features, dtype=bool))
    if locked.shape != labels.shape:
        raise ValueError("locked-feature dimensions differ from segmentation labels")
    protected = locked.copy()
    retained = 0
    retained_by_colour: dict[int, int] = {}
    # The cheapest per-pixel assignment cost is loop-invariant; costs is only read.
    best_cost = costs.min(axis=2)
    for component in range(1, count + 1):
        item = objects[component - 1]
        if item is None:
            continue
        local_component = components[item] == component
        span = max(item[0].stop - item[0].start, item[1].stop - item[1].start)
        population = int(local_component.sum())
        if span < minimum_length or population < minimum_length:
            continue
        component_labels = np.unique(labels[item][local_component])
        local_response = response[item]
        core_threshold = float(np.percentile(local_response[local_component], 75))
        core = local_component & (local_response >= core_threshold)
        component_costs = costs[item][core][:, component_labels].mean(axis=0)
        target = int(min(
            range(len(component_labels)),
            key=lambda index: (float(component_costs[index]),
                               float(centres[component_labels[index], 0]),
                               int(component_labels[index])),
        ))
        target = int(component_labels[target])
        target_cost = costs[..., target]
        # The band can only reach two pixels beyond the component box: the widest
        # dilation is binary_dilation(..., iterations=radius - 1) with radius 3,
        # i.e. two iterations of the cross structure. A two-pixel pad clamped to
        # the image therefore contains every pixel the band can touch, so working
        # in this window and writing through the views is identical to full-image
        # construction; edges lost to clamping are outside the image regardless.
        window = (
            slice(max(0, item[0].start - 2), min(height, item[0].stop + 2)),
            slice(max(0, item[1].start - 2), min(width, item[1].stop + 2)),
        )
        global_component = components[window] == component
        band = global_component.copy()
        for radius in (2, 3):
            radius_centres = global_component & (best_radius[window] == radius)
            if radius_centres.any():
                band |= ndimage.binary_dilation(radius_centres, iterations=radius - 1)
        # Include the AA/body pixels that plausibly belong to the consolidated
        # line colour, without painting across unrelated neighbouring regions.
        band &= ((target_cost[window] <= best_cost[window] + minimum_contrast)
                 & (response[window] >= minimum_contrast * 0.25)) | global_component
        band &= ~locked[window]
        normalized[window][band] = target
        protected[window] |= band
        retained += 1
        retained_by_colour[target] = retained_by_colour.get(target, 0) + 1
    return normalized, protected, {
        "candidate_pixels": int(raw_seeds.sum()),
        "protected_pixels": int(protected.sum()),
        "protected_components": retained,
        "protected_components_by_colour": retained_by_colour,
        "explicitly_locked_pixels": int(locked.sum()),
        "minimum_two_sided_contrast": minimum_contrast,
        "minimum_continuity_length": minimum_length,
    }


@njit(cache=True, fastmath=False, inline="always")
def _boundary_best_colour(result, costs, y, x, height, width, palette_size, smooth):
    """Lowest-energy palette index for one uncertain pixel.

    The energy is the pixel's assignment cost plus a smoothness penalty
    ``smooth * (4 - same)`` where ``same`` counts, among the four edge
    neighbours that exist, how many currently carry the candidate colour; the
    penalty term is formed in float32 exactly as the numpy formulation. Ties
    keep the first (lowest) index, matching ``argmin``.
    """
    neighbour_up = result[y - 1, x] if y > 0 else -1
    neighbour_down = result[y + 1, x] if y < height - 1 else -1
    neighbour_left = result[y, x - 1] if x > 0 else -1
    neighbour_right = result[y, x + 1] if x < width - 1 else -1
    best_colour = 0
    same0 = ((1 if neighbour_up == 0 else 0) + (1 if neighbour_down == 0 else 0)
             + (1 if neighbour_left == 0 else 0) + (1 if neighbour_right == 0 else 0))
    best_energy = costs[y, x, 0] + smooth * np.float32(4.0 - same0)
    for colour in range(1, palette_size):
        same = ((1 if neighbour_up == colour else 0)
                + (1 if neighbour_down == colour else 0)
                + (1 if neighbour_left == colour else 0)
                + (1 if neighbour_right == colour else 0))
        energy = costs[y, x, colour] + smooth * np.float32(4.0 - same)
        if energy < best_energy:
            best_energy = energy
            best_colour = colour
    return best_colour


@njit(parallel=True, cache=True, fastmath=False)
def _two_smallest_margin(costs):
    """Per-pixel gap between the two smallest per-palette costs.

    Selecting the smallest and second-smallest cost by comparison picks the exact
    two float32 values ``np.partition(costs, 1, axis=2)`` places at indices 0 and
    1, and the difference is formed in the same float32 dtype, so the result is
    byte-identical to ``ordered[..., 1] - ordered[..., 0]``. Each pixel is
    independent, so the row-parallel evaluation carries no cross-thread reduction
    and is deterministic across thread counts.
    """
    height, width, palette_size = costs.shape
    margin = np.empty((height, width), dtype=costs.dtype)
    for y in prange(height):
        for x in range(width):
            first = costs[y, x, 0]
            second = costs[y, x, 1]
            if second < first:
                first, second = second, first
            for k in range(2, palette_size):
                value = costs[y, x, k]
                if value < first:
                    second = first
                    first = value
                elif value < second:
                    second = value
            margin[y, x] = second - first
    return margin


@njit(parallel=True, cache=True, fastmath=False)
def _resolve_boundary_pass_compact(
    result, costs, red_y, red_x, black_y, black_x, smoothness
):
    """One red/black ICM sweep over only the pre-compacted active pixels.

    ``red_*``/``black_*`` list the ambiguous pixels of parity 0 then parity 1.
    The two parities run in sequence so the black sweep observes the red sweep's
    writes, exactly as the whole-frame version did. Within a parity every active
    pixel is on the checkerboard, so its four edge neighbours are the opposite
    parity and are never written during that sub-sweep; visiting only the active
    pixels in parallel therefore reproduces the whole-array snapshot semantics
    byte-for-byte, and the per-pixel energy and tie-break are unchanged.
    """
    height, width = result.shape
    palette_size = costs.shape[2]
    smooth = np.float32(smoothness)
    changed = 0
    for i in prange(red_y.shape[0]):
        y = red_y[i]
        x = red_x[i]
        best_colour = _boundary_best_colour(
            result, costs, y, x, height, width, palette_size, smooth
        )
        if best_colour != result[y, x]:
            result[y, x] = best_colour
            changed += 1
    for i in prange(black_y.shape[0]):
        y = black_y[i]
        x = black_x[i]
        best_colour = _boundary_best_colour(
            result, costs, y, x, height, width, palette_size, smooth
        )
        if best_colour != result[y, x]:
            result[y, x] = best_colour
            changed += 1
    return changed


def resolve_ambiguous_boundaries(
    labels: np.ndarray,
    costs: np.ndarray,
    protected: np.ndarray,
    ambiguity_margin: float = 7.0,
    smoothness: float = 2.5,
    passes: int = 5,
) -> tuple[np.ndarray, dict[str, int | float]]:
    """Resolve AA bands by spatially regularising only uncertain pixels."""
    result = np.ascontiguousarray(labels.copy())
    costs_c = np.ascontiguousarray(costs)
    with _KERNEL_LOCK:
        margin = _two_smallest_margin(costs_c)
    ambiguous = (margin < ambiguity_margin) & ~protected
    ambiguous_count = int(ambiguous.sum())
    # Compact the uncertain pixels once into per-parity coordinate lists so each
    # sweep visits only active pixels instead of scanning the whole frame. Within
    # a parity update order is immaterial (see the kernel docstring), so the flat
    # row-major listing np.nonzero yields is a valid ordering.
    ys, xs = np.nonzero(ambiguous)
    ys = ys.astype(np.int64)
    xs = xs.astype(np.int64)
    red = ((ys + xs) & 1) == 0
    red_y = np.ascontiguousarray(ys[red])
    red_x = np.ascontiguousarray(xs[red])
    black_y = np.ascontiguousarray(ys[~red])
    black_x = np.ascontiguousarray(xs[~red])
    changed_total = 0
    used_passes = 0
    # Red/black ICM avoids the checkerboard oscillation caused by updating every
    # uncertain pixel from the same stale neighbourhood; the fused parallel
    # kernel keeps that ordering while scaling the per-pass work across cores.
    # Sweeping stops as soon as a sweep flips zero pixels, so the result is the
    # exact fixed point (up to the pass cap) rather than an early approximation.
    with _KERNEL_LOCK:
        for pass_index in range(passes):
            count = int(_resolve_boundary_pass_compact(
                result, costs_c, red_y, red_x, black_y, black_x, smoothness
            ))
            if not count:
                break
            changed_total += count
            used_passes = pass_index + 1
    return result, {
        "ambiguous_pixels": ambiguous_count,
        "changed_assignments": changed_total,
        "passes": used_passes,
        "ambiguity_margin": ambiguity_margin,
        "smoothness": smoothness,
    }


@njit(parallel=True, cache=True, fastmath=False)
def _dither_majority_vote(labels, count, radius):
    """Per-pixel majority palette index over a ``(2r+1)`` box neighbourhood.

    Reproduces ``ndimage.uniform_filter(..., mode="nearest")`` bit-for-bit: for
    each colour a separable box mean is formed with scipy's exact algorithm — a
    running sum swept first down each column (axis 0) then across each row
    (axis 1), each output divided by the filter length, with nearest-edge
    extension. Colours are folded in ascending order and a share replaces the
    incumbent only when strictly greater, so ties fall to the lowest index like
    ``argmax(axis=0)``.
    """
    height, width = labels.shape
    length = 2 * radius + 1
    size1 = length // 2
    size2 = length - size1 - 1
    best_share = np.full((height, width), -1.0)
    voted = np.zeros((height, width), dtype=labels.dtype)
    column_mean = np.empty((height, width))
    for colour in range(count):
        # Axis-0 running sum down each column of the (labels == colour) indicator.
        for x in prange(width):
            running = 0.0
            for offset in range(length):
                index = offset - size1
                if index < 0:
                    index = 0
                elif index > height - 1:
                    index = height - 1
                running += 1.0 if labels[index, x] == colour else 0.0
            column_mean[0, x] = running / length
            for y in range(1, height):
                add_index = y + size2
                if add_index > height - 1:
                    add_index = height - 1
                sub_index = y - 1 - size1
                if sub_index < 0:
                    sub_index = 0
                elif sub_index > height - 1:
                    sub_index = height - 1
                running += ((1.0 if labels[add_index, x] == colour else 0.0)
                            - (1.0 if labels[sub_index, x] == colour else 0.0))
                column_mean[y, x] = running / length
        # Axis-1 running sum across each row, folding the share into the vote.
        for y in prange(height):
            running = 0.0
            for offset in range(length):
                index = offset - size1
                if index < 0:
                    index = 0
                elif index > width - 1:
                    index = width - 1
                running += column_mean[y, index]
            share = running / length
            if share > best_share[y, 0]:
                best_share[y, 0] = share
                voted[y, 0] = colour
            for x in range(1, width):
                add_index = x + size2
                if add_index > width - 1:
                    add_index = width - 1
                sub_index = x - 1 - size1
                if sub_index < 0:
                    sub_index = 0
                elif sub_index > width - 1:
                    sub_index = width - 1
                running += column_mean[y, add_index] - column_mean[y, sub_index]
                share = running / length
                if share > best_share[y, x]:
                    best_share[y, x] = share
                    voted[y, x] = colour
    return voted


def collapse_dither_speckle(
    labels: np.ndarray,
    despeckled_labels: np.ndarray,
    count: int,
    radius: int,
    protected: np.ndarray,
) -> tuple[np.ndarray, dict[str, int]]:
    """Remove dither speckle by consensus: reassign an unprotected pixel only
    where the neighbourhood majority label and the independent despeckled-source
    assignment agree on the same new colour. Requiring both to concur collapses
    the salt-and-pepper that dithered regions produce once every pixel is snapped
    to its nearest palette colour, while a lone genuine detail — which only one
    of the two ever proposes changing — is left intact. Protected details keep
    their own label, and a radius of zero returns the labels unchanged. Ties in
    the majority fall to the lowest label index, keeping the pass deterministic."""
    if radius <= 0:
        return labels, {"radius": 0, "changed_assignments": 0}
    labels_c = np.ascontiguousarray(labels)
    with _KERNEL_LOCK:
        voted = _dither_majority_vote(labels_c, count, radius)
    agreed = (voted == despeckled_labels) & (voted != labels) & ~protected
    result = np.where(agreed, voted, labels)
    return result, {
        "radius": radius,
        "changed_assignments": int(np.count_nonzero(agreed)),
    }


# Bounded thread pool over the independent per-colour 8-connected ``ndimage.label``
# scans of one label map. Each scan reads the shared map and allocates its own
# mask, and the heavy ``ndimage.label`` releases the GIL, so a small pool overlaps
# them; results are assembled serially in ascending colour order, so the output
# list is byte-identical to the serial computation. Small maps (the analysis-image
# calls inside ``merge_clusters``) skip the pool to avoid its overhead.
_SPATIAL_STATS_LABEL_WORKERS = 4
_SPATIAL_STATS_PARALLEL_MIN_PIXELS = 1_000_000


def _spatial_stats_one(
    label_map: np.ndarray, index: int, structure: np.ndarray
) -> dict[str, float | int]:
    mask = label_map == index
    occupied = int(mask.sum())
    if not occupied:
        return {"regions": 0, "largest_region_share": 0.0}
    labelled, regions = ndimage.label(mask, structure=structure)
    sizes = np.bincount(labelled.ravel())[1:]
    largest = int(sizes.max()) if len(sizes) else 0
    return {"regions": int(regions), "largest_region_share": largest / occupied}


def spatial_stats(label_map: np.ndarray, count: int) -> list[dict[str, float | int]]:
    structure = np.ones((3, 3), dtype=np.uint8)
    max_workers = max(1, min(_SPATIAL_STATS_LABEL_WORKERS, os.cpu_count() or 1, count))
    if (max_workers > 1 and count > 1
            and label_map.size >= _SPATIAL_STATS_PARALLEL_MIN_PIXELS):
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(
                lambda index: _spatial_stats_one(label_map, index, structure),
                range(count),
            ))
    return [_spatial_stats_one(label_map, index, structure) for index in range(count)]


# Bounded thread pool over the independent per-colour ``ndimage.label`` scans of
# the single initial labelling; each scan releases the GIL, so a small pool
# overlaps them without touching any shared writable state (results are assembled
# serially, in colour order, by the caller).
_SMALL_REGION_LABEL_WORKERS = 4


def _label_one_colour(
    base: np.ndarray,
    colour: int,
    component_structure: np.ndarray,
) -> tuple[int, tuple[np.ndarray, int, np.ndarray, list] | None]:
    """Label the 4-connected components of one palette colour over the full frame.

    Returns the colour's local label field, component count, per-component pixel
    counts and bounding-box slices. The heavy ``ndimage`` calls release the GIL,
    so independent colours parallelise cleanly; the returned data is self-contained
    and is stitched into the global component space serially by the caller.
    """
    mask = base == colour
    labelled, count = ndimage.label(mask, structure=component_structure)
    if not count:
        return colour, None
    sizes = np.bincount(labelled.ravel())
    objects = ndimage.find_objects(labelled)
    return colour, (labelled, count, sizes, objects)


def _unique_edges(
    lo: np.ndarray, hi: np.ndarray, num_nodes: int
) -> tuple[np.ndarray, np.ndarray]:
    """Collapse directed boundary-pixel pairs into unique undirected component edges."""
    if lo.size == 0:
        empty = np.empty(0, dtype=np.int64)
        return empty, empty
    stride = np.int64(num_nodes + 1)
    key = np.unique(lo.astype(np.int64) * stride + hi.astype(np.int64))
    return key // stride, key % stride


def remove_small_regions(
    label_map: np.ndarray,
    centres: np.ndarray,
    minimum_pixels: int,
    maximum_passes: int = 8,
    protected_features: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Fold specks, never pixels protected by source-aware segmentation.

    The frame is labelled into 4-connected per-colour components exactly once, and
    the spatial adjacency between those components is captured as two fixed graphs:
    edge-sharing (4-connected) adjacency, which matches cutter geometry because two
    regions touching only at a corner are not one physical piece of vinyl, and
    8-connected adjacency, which governs which stable colours a speck may fold into.
    Because folding only relabels pixels and never moves them, that adjacency is
    valid for the whole run; a union-find then resolves the fold cascade in rounds.

    Each round, a component (a union-find group) is stable iff its current pixel
    count is ``>= minimum_pixels``; every smaller group is a fold candidate. A
    candidate that touches ``protected_features`` is recorded and never folds.
    Otherwise it folds into the nearest-in-Lab palette colour among the distinct
    colours of the stable groups 8-adjacent to it (ties broken toward the lowest
    colour index, matching ``np.argmin`` over the ascending unique colours). All of
    a round's folds are decided against that round's snapshot, then applied; a
    folded group is merged (4-connected) into any same-colour neighbour, so it can
    both unlock further folds and, if it stays sub-threshold, fold again next round.
    ``passes`` reports the number of rounds in which at least one fold occurred.
    """
    base = label_map.copy()
    # Edge-sharing connectivity matches cutter geometry: two regions touching
    # only at a single corner are not physically joined pieces of vinyl.
    component_structure = ndimage.generate_binary_structure(2, 1)
    replacements: dict[tuple[int, int], dict[str, int]] = {}
    protected_lines: set[tuple[int, int, int, int, int]] = set()
    passes = 0
    if minimum_pixels <= 1:
        return base, {
            "minimum_pixels": minimum_pixels,
            "passes": 0,
            "removed_regions": 0,
            "removed_pixels": 0,
            "protected_intentional_lines": 0,
            "replacements": [],
        }

    colour_count = len(centres)

    # --- One labelling pass: stitch every colour's components into one id space. ---
    scans: dict[int, tuple[np.ndarray, int, np.ndarray, list] | None] = {}
    max_workers = max(1, min(_SMALL_REGION_LABEL_WORKERS, os.cpu_count() or 1, colour_count))
    if max_workers > 1 and colour_count > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for colour, data in pool.map(
                lambda c: _label_one_colour(base, c, component_structure),
                range(colour_count),
            ):
                scans[colour] = data
    else:
        for colour in range(colour_count):
            _, scans[colour] = _label_one_colour(base, colour, component_structure)

    comp = np.zeros(base.shape, dtype=np.int32)  # global 1-based component id per pixel
    node_colour = [-1]
    node_size = [0]
    by0 = [0]
    by1 = [0]
    bx0 = [0]
    bx1 = [0]
    offset = 0
    for colour in range(colour_count):
        data = scans.get(colour)
        if data is None:
            continue
        labelled, count, sizes, objects = data
        mask = base == colour
        comp[mask] = labelled[mask].astype(np.int32) + offset
        for local in range(1, count + 1):
            node_colour.append(colour)
            node_size.append(int(sizes[local]))
            y_slice, x_slice = objects[local - 1]
            by0.append(int(y_slice.start))
            by1.append(int(y_slice.stop))
            bx0.append(int(x_slice.start))
            bx1.append(int(x_slice.stop))
        offset += count
    num_nodes = offset

    if num_nodes == 0:
        return base, {
            "minimum_pixels": minimum_pixels,
            "passes": 0,
            "removed_regions": 0,
            "removed_pixels": 0,
            "protected_intentional_lines": 0,
            "replacements": [],
        }

    # --- Fixed spatial adjacency graphs over the single labelling. ---
    o_lo: list[np.ndarray] = []
    o_hi: list[np.ndarray] = []
    for a2d, b2d in ((comp[:, :-1], comp[:, 1:]), (comp[:-1, :], comp[1:, :])):
        a = a2d.reshape(-1)
        b = b2d.reshape(-1)
        diff = a != b
        aa = a[diff]
        bb = b[diff]
        o_lo.append(np.minimum(aa, bb))
        o_hi.append(np.maximum(aa, bb))
    d_lo: list[np.ndarray] = []
    d_hi: list[np.ndarray] = []
    for a2d, b2d in ((comp[:-1, :-1], comp[1:, 1:]), (comp[:-1, 1:], comp[1:, :-1])):
        a = a2d.reshape(-1)
        b = b2d.reshape(-1)
        diff = a != b
        aa = a[diff]
        bb = b[diff]
        d_lo.append(np.minimum(aa, bb))
        d_hi.append(np.maximum(aa, bb))
    u4, v4 = _unique_edges(np.concatenate(o_lo), np.concatenate(o_hi), num_nodes)
    u8, v8 = _unique_edges(
        np.concatenate(o_lo + d_lo), np.concatenate(o_hi + d_hi), num_nodes
    )

    # --- Union-find state, indexed by 1-based node id. ---
    n1 = num_nodes + 1
    parent = np.arange(n1, dtype=np.int64)
    size = np.asarray(node_size, dtype=np.int64)
    colour = np.asarray(node_colour, dtype=np.int64)
    bbox_y0 = np.asarray(by0, dtype=np.int64)
    bbox_y1 = np.asarray(by1, dtype=np.int64)
    bbox_x0 = np.asarray(bx0, dtype=np.int64)
    bbox_x1 = np.asarray(bx1, dtype=np.int64)
    has_prot = np.zeros(n1, dtype=bool)
    if protected_features is not None:
        protected_mask = np.asarray(protected_features, dtype=bool)
        has_prot[np.unique(comp[protected_mask])] = True

    def find(node: int) -> int:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(a: int, b: int) -> None:
        if a == b:
            return
        if size[a] < size[b]:
            a, b = b, a
        parent[b] = a
        size[a] += size[b]
        has_prot[a] |= has_prot[b]
        bbox_y0[a] = min(bbox_y0[a], bbox_y0[b])
        bbox_y1[a] = max(bbox_y1[a], bbox_y1[b])
        bbox_x0[a] = min(bbox_x0[a], bbox_x0[b])
        bbox_x1[a] = max(bbox_x1[a], bbox_x1[b])

    # Pairwise palette-centre distances; symmetric, so ``dist[s, c]`` equals the
    # per-neighbour Euclidean distance the scan would otherwise recompute.
    centre_dist = np.linalg.norm(
        centres[:, None, :] - centres[None, :, :], axis=2
    )
    any_protected = bool(has_prot.any())

    node_ids = np.arange(n1)
    for pass_index in range(maximum_passes):
        # Resolve every node to its current root by vectorised pointer jumping.
        roots = parent.copy()
        while True:
            nxt = parent[roots]
            if np.array_equal(nxt, roots):
                break
            roots = nxt
        is_root = roots == node_ids
        cand_roots = np.flatnonzero(is_root & (size < minimum_pixels) & (node_ids > 0))

        # A candidate that touches a protected feature is recorded, never folded.
        cand_of = np.zeros(n1, dtype=bool)
        cand_of[cand_roots] = True
        protected_cands = cand_of & has_prot if any_protected else None
        if any_protected:
            for r in np.flatnonzero(protected_cands):
                protected_lines.add(
                    (int(colour[r]), int(bbox_y0[r]), int(bbox_y1[r]),
                     int(bbox_x0[r]), int(bbox_x1[r]))
                )

        # Candidate -> colours of stable 8-adjacent groups (excluding its own colour).
        foldable = cand_of & ~has_prot
        ru = roots[u8]
        rv = roots[v8]
        cu = colour[u8]
        cv = colour[v8]
        stable = size >= minimum_pixels
        m1 = foldable[ru] & stable[rv] & (cv != cu)
        m2 = foldable[rv] & stable[ru] & (cu != cv)
        nbr_root = np.concatenate([ru[m1], rv[m2]])
        nbr_col = np.concatenate([cv[m1], cu[m2]])
        if nbr_root.size == 0:
            break

        # Nearest-colour fold per candidate, vectorised: among a candidate's stable
        # 8-neighbour colours pick the minimum centre distance, ties toward the
        # lowest colour index (matching ``argmin`` over the ascending unique set).
        src_of = colour[nbr_root]
        pair_dist = centre_dist[src_of, nbr_col]
        order = np.lexsort((nbr_col, pair_dist, nbr_root))
        sorted_root = nbr_root[order]
        first = np.ones(sorted_root.size, dtype=bool)
        first[1:] = sorted_root[1:] != sorted_root[:-1]
        winner = order[first]
        fold_roots = nbr_root[winner]
        fold_targets = nbr_col[winner]
        fold_sources = src_of[winner]

        # Relabel every member of every folded group in a single scatter.
        target_of_root = np.full(n1, -1, dtype=np.int64)
        target_of_root[fold_roots] = fold_targets
        node_target = target_of_root[roots]
        folded_nodes = node_target >= 0
        colour[folded_nodes] = node_target[folded_nodes]

        for root, target, source in zip(
            fold_roots.tolist(), fold_targets.tolist(), fold_sources.tolist()
        ):
            item = replacements.setdefault((source, target), {"regions": 0, "pixels": 0})
            item["regions"] += 1
            item["pixels"] += int(size[root])

        # Merge each folded region into any now-same-colour 4-adjacent neighbour so
        # the component structure (and thus stability) tracks a full re-labelling.
        same = np.flatnonzero(colour[u4] == colour[v4])
        for i in same.tolist():
            ra = find(int(u4[i]))
            rb = find(int(v4[i]))
            if ra != rb:
                union(ra, rb)
        passes = pass_index + 1

    result = colour[comp].astype(label_map.dtype)

    replacement_rows = [
        {
            "from_index": source,
            "to_index": target,
            "regions": values["regions"],
            "pixels": values["pixels"],
        }
        for (source, target), values in sorted(replacements.items())
    ]
    return result, {
        "minimum_pixels": minimum_pixels,
        "passes": passes,
        "removed_regions": sum(row["regions"] for row in replacement_rows),
        "removed_pixels": sum(row["pixels"] for row in replacement_rows),
        "protected_intentional_lines": len(protected_lines),
        "replacements": replacement_rows,
    }


def protect_components_at_points(
    label_map: np.ndarray,
    points: tuple[tuple[int, int], ...],
) -> tuple[np.ndarray, list[dict[str, int]]]:
    """Select exact four-connected colour components using source-pixel points."""
    protected = np.zeros(label_map.shape, dtype=bool)
    resolved: list[dict[str, int]] = []
    structure = ndimage.generate_binary_structure(2, 1)
    height, width = label_map.shape
    component_cache: dict[int, np.ndarray] = {}
    for x, y in points:
        if x >= width or y >= height:
            raise ValueError(
                f"protected component point ({x}, {y}) is outside {width}x{height} source"
            )
        colour = int(label_map[y, x])
        components = component_cache.get(colour)
        if components is None:
            components, _ = ndimage.label(label_map == colour, structure=structure)
            component_cache[colour] = components
        component = int(components[y, x])
        mask = components == component
        protected |= mask
        resolved.append({
            "x": x,
            "y": y,
            "colour_index_before_final_remap": colour,
            "component_pixels": int(mask.sum()),
        })
    return protected, resolved


def fold_underrepresented_colours(
    label_map: np.ndarray,
    centres: np.ndarray,
    minimum_share: float,
    minimum_colours: int = 2,
    preserved_colours: set[int] | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Fold globally under-represented colours into the nearest surviving colour."""
    result = label_map.copy()
    counts = np.bincount(result.ravel(), minlength=len(centres))
    total = int(counts.sum())
    active = set(np.flatnonzero(counts).tolist())
    preserved = preserved_colours or set()
    replacements: list[dict[str, object]] = []

    if minimum_share > 0:
        candidates = sorted(active, key=lambda index: (counts[index], index))
        for source in candidates:
            if len(active) <= minimum_colours:
                break
            if source not in active or source in preserved:
                continue
            share = float(counts[source] / total)
            if share >= minimum_share:
                continue
            targets = np.array(sorted(active - {source}), dtype=np.int32)
            distances = np.linalg.norm(centres[targets] - centres[source], axis=1)
            target = int(targets[int(np.argmin(distances))])
            pixels = int(counts[source])
            result[result == source] = target
            counts[target] += counts[source]
            counts[source] = 0
            active.remove(source)
            replacements.append(
                {
                    "from_index": source,
                    "to_index": target,
                    "pixels": pixels,
                    "share_percent": round(100 * share, 4),
                    "delta_e_76": round(float(distances.min()), 3),
                }
            )

    return result, {
        "minimum_share": minimum_share,
        "minimum_share_percent": 100 * minimum_share,
        "removed_colours": len(replacements),
        "removed_pixels": sum(int(row["pixels"]) for row in replacements),
        "replacements": replacements,
    }
