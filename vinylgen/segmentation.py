from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    # not just a halfway blend between two palette colours.
    ordered = np.partition(costs, 1, axis=2)
    margin = ordered[..., 1] - ordered[..., 0]
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
def _resolve_boundary_pass(result, costs, ambiguous, smoothness):
    """One red/black ICM sweep, mutating ``result`` in place; returns changes.

    The two parities run in sequence so the black sweep observes the red sweep's
    writes, exactly as the numpy in-place update did. Within a parity every
    active pixel is on the checkerboard, so its four edge neighbours are the
    opposite parity and are never written during the sweep; the row-parallel
    evaluation and in-place write therefore reproduce the whole-array snapshot
    semantics byte-for-byte.
    """
    height, width = result.shape
    palette_size = costs.shape[2]
    smooth = np.float32(smoothness)
    changed = 0
    for y in prange(height):
        for x in range(width):
            if not ambiguous[y, x]:
                continue
            if ((y + x) & 1) != 0:
                continue
            best_colour = _boundary_best_colour(
                result, costs, y, x, height, width, palette_size, smooth
            )
            if best_colour != result[y, x]:
                result[y, x] = best_colour
                changed += 1
    for y in prange(height):
        for x in range(width):
            if not ambiguous[y, x]:
                continue
            if ((y + x) & 1) != 1:
                continue
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
    ordered = np.partition(costs, 1, axis=2)
    margin = ordered[..., 1] - ordered[..., 0]
    ambiguous = np.ascontiguousarray((margin < ambiguity_margin) & ~protected)
    costs_c = np.ascontiguousarray(costs)
    changed_total = 0
    used_passes = 0
    # Red/black ICM avoids the checkerboard oscillation caused by updating every
    # uncertain pixel from the same stale neighbourhood; the fused parallel
    # kernel keeps that ordering while scaling the per-pass work across cores.
    with _KERNEL_LOCK:
        for pass_index in range(passes):
            count = int(_resolve_boundary_pass(result, costs_c, ambiguous, smoothness))
            if not count:
                break
            changed_total += count
            used_passes = pass_index + 1
    return result, {
        "ambiguous_pixels": int(ambiguous.sum()),
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


def spatial_stats(label_map: np.ndarray, count: int) -> list[dict[str, float | int]]:
    stats: list[dict[str, float | int]] = []
    structure = np.ones((3, 3), dtype=np.uint8)
    for index in range(count):
        mask = label_map == index
        occupied = int(mask.sum())
        if not occupied:
            stats.append({"regions": 0, "largest_region_share": 0.0})
            continue
        labelled, regions = ndimage.label(mask, structure=structure)
        sizes = np.bincount(labelled.ravel())[1:]
        largest = int(sizes.max()) if len(sizes) else 0
        stats.append({"regions": int(regions), "largest_region_share": largest / occupied})
    return stats


# Bounded thread pool over the independent per-colour ``ndimage.label`` scans;
# each scan releases the GIL, so a small pool overlaps them without touching any
# shared writable state (results are consumed serially in the caller).
_SMALL_REGION_MAX_WORKERS = 4


def _scan_colour_small_regions(
    snapshot: np.ndarray,
    colour: int,
    minimum_pixels: int,
    component_structure: np.ndarray,
) -> tuple[list, np.ndarray | None]:
    """Label one palette colour over the full frame.

    Returns the colour's sub-threshold component descriptors in ascending
    component-label order — which is first-pixel raster order, exactly the order
    ``np.flatnonzero`` yields — plus the boolean field of pixels that belong to a
    stable (>= ``minimum_pixels``) component of this colour. Every value is a copy
    or a self-contained array, so the returned data survives after the source
    labelling is freed and carries no view into shared state. Independent per
    colour and GIL-releasing in its heavy calls, so it parallelises cleanly.
    """
    mask_c = snapshot == colour
    components, count = ndimage.label(mask_c, structure=component_structure)
    if not count:
        return [], None
    sizes = np.bincount(components.ravel())
    stable_full = mask_c & (sizes[components] >= minimum_pixels)
    objects = ndimage.find_objects(components)
    height, width = snapshot.shape
    descriptors: list = []
    for component in np.flatnonzero((sizes < minimum_pixels) & (np.arange(len(sizes)) > 0)):
        component_slice = objects[component - 1]
        if component_slice is None:
            continue
        y_slice, x_slice = component_slice
        bounds = (
            slice(max(0, y_slice.start - 1), min(height, y_slice.stop + 1)),
            slice(max(0, x_slice.start - 1), min(width, x_slice.stop + 1)),
        )
        mask = components[bounds] == component
        descriptors.append((y_slice, x_slice, bounds, mask, int(sizes[component])))
    return descriptors, stable_full


def remove_small_regions(
    label_map: np.ndarray,
    centres: np.ndarray,
    minimum_pixels: int,
    maximum_passes: int = 8,
    protected_features: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Fold specks, never pixels protected by source-aware segmentation.

    Work is localized to the colours whose masks actually changed. A colour's
    component structure and its contribution to the global ``stable_pixels`` field
    depend only on that colour's own indicator mask, so a colour untouched by the
    previous pass keeps a byte-identical labelling: its cached sub-threshold
    descriptors and its stable pixels stay exact. The first pass necessarily scans
    every colour; each later pass rescans only the sources and targets of the
    previous pass's folds and refreshes ``stable_pixels`` in place over just those
    colours' pixels. Because a colour changes its mask only by gaining pixels (a
    fold target) or losing them (a fold source), that set captures every colour
    whose labelling or stability could differ, so the field this maintains equals a
    full per-pass recompute pixel for pixel. The fold-candidate scan still runs for
    every colour each pass against the current ``stable_pixels``, so a speck of an
    untouched colour still folds the moment a neighbour becomes stable.
    """
    result = label_map.copy()
    # Edge-sharing connectivity matches cutter geometry: two regions touching
    # only at a single corner are not physically joined pieces of vinyl.
    component_structure = ndimage.generate_binary_structure(2, 1)
    neighbour_structure = np.ones((3, 3), dtype=np.uint8)
    replacements: dict[tuple[int, int], dict[str, int]] = {}
    protected_lines: set[tuple[int, int, int, int, int]] = set()
    passes = 0
    if minimum_pixels <= 1:
        return result, {
            "minimum_pixels": minimum_pixels,
            "passes": 0,
            "removed_regions": 0,
            "removed_pixels": 0,
            "protected_intentional_lines": 0,
            "replacements": [],
        }

    colour_count = len(centres)
    # Per-colour sub-threshold descriptors, held in ascending component-label order;
    # reused untouched for colours that did not change in the previous pass.
    descriptor_cache: list[list] = [[] for _ in range(colour_count)]
    stable_pixels = np.zeros(result.shape, dtype=bool)
    # Pass 1 must build the full global structure once; every colour is "affected".
    affected: set[int] = set(range(colour_count))
    max_workers = max(1, min(_SMALL_REGION_MAX_WORKERS, os.cpu_count() or 1, colour_count))

    for pass_index in range(maximum_passes):
        snapshot = result.copy()
        # Refresh labelling and the stable field for exactly the changed colours.
        # Stable pixels are written per colour over that colour's current mask;
        # colours partition the frame, so these writes never overlap and pixels
        # that changed hands are rewritten by their new owner (also affected).
        scan_targets = sorted(affected)

        def apply_scan(colour: int, descriptors: list, stable_full: np.ndarray | None) -> None:
            descriptor_cache[colour] = descriptors
            if stable_full is not None:
                mask_c = snapshot == colour
                stable_pixels[mask_c] = stable_full[mask_c]

        if max_workers > 1 and len(scan_targets) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(
                        _scan_colour_small_regions,
                        snapshot,
                        colour,
                        minimum_pixels,
                        component_structure,
                    ): colour
                    for colour in scan_targets
                }
                for future in as_completed(futures):
                    colour = futures[future]
                    descriptors, stable_full = future.result()
                    apply_scan(colour, descriptors, stable_full)
        else:
            for colour in scan_targets:
                descriptors, stable_full = _scan_colour_small_regions(
                    snapshot, colour, minimum_pixels, component_structure
                )
                apply_scan(colour, descriptors, stable_full)

        changes: list[tuple[tuple[slice, slice], np.ndarray, int, int, int]] = []
        for source in range(colour_count):
            for y_slice, x_slice, bounds, mask, size in descriptor_cache[source]:
                if protected_features is not None and np.any(
                    protected_features[bounds] & mask
                ):
                    protected_lines.add((source, y_slice.start, y_slice.stop,
                                         x_slice.start, x_slice.stop))
                    continue
                boundary = (
                    ndimage.binary_dilation(mask, structure=neighbour_structure) & ~mask
                )
                eligible_boundary = boundary & stable_pixels[bounds]
                neighbours = np.unique(snapshot[bounds][eligible_boundary])
                neighbours = neighbours[neighbours != source]
                if not len(neighbours):
                    continue
                distances = np.linalg.norm(centres[neighbours] - centres[source], axis=1)
                nearest = int(np.argmin(distances))
                target = int(neighbours[nearest])
                changes.append((bounds, mask, source, target, size))
        if not changes:
            break
        affected = set()
        for bounds, mask, source, target, pixels in changes:
            # Do not overwrite pixels already changed by an overlapping decision.
            target_view = result[bounds]
            applicable = mask & (target_view == source)
            actual = int(applicable.sum())
            # Both endpoints' masks may shift this pass, so both must be rescanned
            # next pass; recording them from every change (even a zero-pixel one)
            # keeps the affected set a safe superset of the changed colours.
            affected.add(source)
            affected.add(target)
            if not actual:
                continue
            target_view[applicable] = target
            item = replacements.setdefault((source, target), {"regions": 0, "pixels": 0})
            item["regions"] += 1
            item["pixels"] += actual
        passes = pass_index + 1

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
