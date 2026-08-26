from __future__ import annotations

import math

import numpy as np
from scipy import ndimage


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
    yy, xx = np.indices(labels.shape, dtype=np.float32)
    response = np.zeros(labels.shape, dtype=np.float32)
    best_radius = np.ones(labels.shape, dtype=np.uint8)
    # Normals over half a circle; the opposite sample is generated explicitly.
    for radius in (1, 2, 3):
        for angle in np.linspace(0.0, np.pi, 8, endpoint=False):
            dx = radius * math.cos(float(angle))
            dy = radius * math.sin(float(angle))
            a = ndimage.map_coordinates(lightness, (yy + dy, xx + dx), order=1,
                                         mode="nearest")
            b = ndimage.map_coordinates(lightness, (yy - dy, xx - dx), order=1,
                                         mode="nearest")
            two_sided = np.minimum(a - lightness, b - lightness)
            improve = two_sided > response
            response[improve] = two_sided[improve]
            best_radius[improve] = radius

    # A real dark stroke should also be assigned confidently enough that it is
    # not just a halfway blend between two palette colours.
    ordered = np.partition(costs, 1, axis=2)
    margin = ordered[..., 1] - ordered[..., 0]
    raw_seeds = (response >= minimum_contrast) & (margin >= 2.0)
    seeds = ndimage.binary_closing(raw_seeds, structure=np.ones((3, 3)), iterations=1)
    components, count = ndimage.label(seeds, structure=np.ones((3, 3)))
    objects = ndimage.find_objects(components)
    coherent = np.zeros_like(seeds)
    normalized = labels.copy()
    locked = (np.zeros_like(seeds) if locked_features is None
              else np.asarray(locked_features, dtype=bool))
    if locked.shape != labels.shape:
        raise ValueError("locked-feature dimensions differ from segmentation labels")
    protected = locked.copy()
    retained = 0
    retained_by_colour: dict[int, int] = {}
    for component in range(1, count + 1):
        item = objects[component - 1]
        if item is None:
            continue
        local_component = components[item] == component
        span = max(item[0].stop - item[0].start, item[1].stop - item[1].start)
        population = int(local_component.sum())
        if span < minimum_length or population < minimum_length:
            continue
        coherent[item] |= local_component
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
        best_cost = costs.min(axis=2)
        global_component = np.zeros_like(seeds)
        global_component[item] = local_component
        band = global_component.copy()
        for radius in (2, 3):
            radius_centres = global_component & (best_radius == radius)
            if radius_centres.any():
                band |= ndimage.binary_dilation(radius_centres, iterations=radius - 1)
        # Include the AA/body pixels that plausibly belong to the consolidated
        # line colour, without painting across unrelated neighbouring regions.
        band &= ((target_cost <= best_cost + minimum_contrast)
                 & (response >= minimum_contrast * 0.25)) | global_component
        band &= ~locked
        normalized[band] = target
        protected |= band
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


def resolve_ambiguous_boundaries(
    labels: np.ndarray,
    costs: np.ndarray,
    protected: np.ndarray,
    ambiguity_margin: float = 7.0,
    smoothness: float = 2.5,
    passes: int = 5,
) -> tuple[np.ndarray, dict[str, int | float]]:
    """Resolve AA bands by spatially regularising only uncertain pixels."""
    result = labels.copy()
    ordered = np.partition(costs, 1, axis=2)
    margin = ordered[..., 1] - ordered[..., 0]
    ambiguous = (margin < ambiguity_margin) & ~protected
    changed_total = 0
    used_passes = 0
    palette_size = costs.shape[2]
    for pass_index in range(passes):
        count = 0
        # Red/black ICM avoids the checkerboard oscillation caused by updating
        # every uncertain pixel from the same stale neighbourhood.
        yy, xx = np.indices(labels.shape)
        for parity in (0, 1):
            active = ambiguous & (((yy + xx) & 1) == parity)
            energies = costs.copy()
            for colour in range(palette_size):
                same = np.zeros(labels.shape, dtype=np.float32)
                same[1:] += result[:-1] == colour
                same[:-1] += result[1:] == colour
                same[:, 1:] += result[:, :-1] == colour
                same[:, :-1] += result[:, 1:] == colour
                energies[..., colour] += smoothness * (4.0 - same)
            proposed = energies.argmin(axis=2).astype(result.dtype)
            change = active & (proposed != result)
            count += int(change.sum())
            result[change] = proposed[change]
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
    size = 2 * radius + 1
    shares = np.stack([
        ndimage.uniform_filter((labels == index).astype(np.float64), size=size, mode="nearest")
        for index in range(count)
    ])
    voted = shares.argmax(axis=0).astype(labels.dtype)
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


def remove_small_regions(
    label_map: np.ndarray,
    centres: np.ndarray,
    minimum_pixels: int,
    maximum_passes: int = 8,
    protected_features: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Fold specks, never pixels protected by source-aware segmentation."""
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

    for pass_index in range(maximum_passes):
        snapshot = result.copy()
        changes: list[tuple[tuple[slice, slice], np.ndarray, int, int, int]] = []
        component_data = []
        stable_pixels = np.zeros(snapshot.shape, dtype=bool)
        for source in range(len(centres)):
            components, count = ndimage.label(
                snapshot == source, structure=component_structure
            )
            sizes = np.bincount(components.ravel())
            objects = ndimage.find_objects(components)
            component_data.append((components, sizes, objects, count))
            if count:
                stable_pixels |= (snapshot == source) & (sizes[components] >= minimum_pixels)
        for source, (components, sizes, objects, count) in enumerate(component_data):
            if not count:
                continue
            for component in np.flatnonzero((sizes < minimum_pixels) & (np.arange(len(sizes)) > 0)):
                component_slice = objects[component - 1]
                if component_slice is None:
                    continue
                y_slice, x_slice = component_slice
                bounds = (
                    slice(max(0, y_slice.start - 1), min(snapshot.shape[0], y_slice.stop + 1)),
                    slice(max(0, x_slice.start - 1), min(snapshot.shape[1], x_slice.stop + 1)),
                )
                local_components = components[bounds]
                mask = local_components == component
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
                changes.append((bounds, mask, source, target, int(sizes[component])))
        if not changes:
            break
        for bounds, mask, source, target, pixels in changes:
            # Do not overwrite pixels already changed by an overlapping decision.
            target_view = result[bounds]
            applicable = mask & (target_view == source)
            actual = int(applicable.sum())
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
