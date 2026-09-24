from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable

import numpy as np
from numba import njit
from scipy import ndimage

from vinylgen.colour import srgb_to_lab
from vinylgen.contracts import CleanupSettings


def _padded_edt(mask: np.ndarray) -> np.ndarray:
    """Exact Euclidean distance from each True pixel to the nearest False pixel.

    The mask is padded with a one-pixel False border before the transform and
    stripped back afterwards, so the image edge counts as background exactly as
    an explicit surrounding zero would.
    """
    return ndimage.distance_transform_edt(np.pad(mask, 1))[1:-1, 1:-1]


@njit(cache=True)
def _widen_refusal(result, target, seed_idx, targets, exp_r0, exp_c0,
                   dfield, dist_window, win_r0, win_c0,
                   visited, gen, stack, width_px, area_px):
    """Whether consuming ``seed_idx`` (colour ``target``) thins any touched
    component below the width or area threshold.

    Each seed lies in a four-connected component of ``result == target``. For a
    residual pixel p (in the component, not consumed) the exact distance to the
    nearest non-residual pixel is ``min(dfield[p], dist(p, consumed))`` because
    the nearest non-residual pixel is either a non-``target`` pixel (distance
    ``dfield[p]`` = the component's padded EDT, per the per-colour field
    invariant) or a consumed pixel. ``dist_window`` supplies ``dist(p,
    consumed)`` wherever it can fall below ``dfield``; elsewhere ``dfield``
    already holds the minimum. The residual width is twice the maximum of that
    field over the unconsumed pixels (0.0 for an empty residual), matching the
    padded-EDT maximum the reference computes.

    ``visited`` is stamped with the strictly increasing ``gen`` so no per-call
    clearing is needed. Refusal has no side effect, so the first refusing
    component determines the result regardless of traversal order.
    """
    H, W = result.shape
    exp_h, exp_w = targets.shape
    win_h, win_w = dist_window.shape
    n = seed_idx.shape[0]
    for s in range(n):
        start = seed_idx[s]
        if visited[start] == gen:
            continue
        sp = 0
        stack[sp] = start
        sp += 1
        visited[start] = gen
        count = 0
        c_count = 0
        maxfield = 0.0
        while sp > 0:
            sp -= 1
            idx = stack[sp]
            r = idx // W
            c = idx - r * W
            count += 1
            is_c = False
            er = r - exp_r0
            ec = c - exp_c0
            if 0 <= er < exp_h and 0 <= ec < exp_w:
                if targets[er, ec]:
                    is_c = True
            if is_c:
                c_count += 1
            else:
                fv = dfield[r, c]
                wr = r - win_r0
                wc = c - win_c0
                if 0 <= wr < win_h and 0 <= wc < win_w:
                    dv = dist_window[wr, wc]
                    if dv < fv:
                        fv = dv
                if fv > maxfield:
                    maxfield = fv
            if r > 0:
                nidx = idx - W
                if result[r - 1, c] == target and visited[nidx] != gen:
                    visited[nidx] = gen
                    stack[sp] = nidx
                    sp += 1
            if r < H - 1:
                nidx = idx + W
                if result[r + 1, c] == target and visited[nidx] != gen:
                    visited[nidx] = gen
                    stack[sp] = nidx
                    sp += 1
            if c > 0:
                nidx = idx - 1
                if result[r, c - 1] == target and visited[nidx] != gen:
                    visited[nidx] = gen
                    stack[sp] = nidx
                    sp += 1
            if c < W - 1:
                nidx = idx + 1
                if result[r, c + 1] == target and visited[nidx] != gen:
                    visited[nidx] = gen
                    stack[sp] = nidx
                    sp += 1
        residual_pixels = count - c_count
        residual_width = 2.0 * maxfield
        if ((width_px > 0.0 and residual_width < width_px) or
                (area_px > 0.0 and residual_pixels < area_px)):
            return True
    return False


@dataclass(frozen=True)
class RegionMeasurements:
    """Every four-connected single-colour region of a label raster, measured whole.

    ``ids`` numbers each pixel's region (unique across colours) and indexes the
    per-region arrays: ``area`` is the region's pixel count, ``width`` twice the
    maximum of its padded EDT (the feature width cleanup compares against), and
    ``explicit`` whether it holds an explicitly protected pixel. None of these
    depend on cleanup settings, so a region's undersized decision is exact under any
    thresholds even when only a crop of the raster is cleaned: ``crop`` keeps the
    per-region arrays whole and crops only ``ids``.
    """

    ids: np.ndarray
    area: np.ndarray
    width: np.ndarray
    explicit: np.ndarray

    def crop(self, rows: slice, columns: slice) -> "RegionMeasurements":
        return replace(self, ids=self.ids[rows, columns])


# Rows of the distance pass computed at a time in ``_region_peak_distances``.
MEASURE_CHUNK_ROWS = 256


def _region_peak_distances(mask: np.ndarray, components: np.ndarray, count: int) -> np.ndarray:
    """Each component's maximum padded EDT (``_padded_edt(mask)``), bit for bit.

    Only the nearest-background feature transform is held for the whole mask; the
    distances are formed from it a band of rows at a time, with the same integer
    offsets, float64 squares, sum and square root the EDT itself computes, so the
    full float64 distance field is never resident.
    """
    padded = np.pad(mask, 1)
    features = ndimage.distance_transform_edt(padded, return_distances=False,
                                              return_indices=True)
    del padded
    height, width = components.shape
    columns = np.arange(1, width + 1)
    peak = np.zeros(count + 1)
    for start in range(0, height, MEASURE_CHUNK_ROWS):
        stop = min(height, start + MEASURE_CHUNK_ROWS)
        rows = np.arange(start + 1, stop + 1)[:, None]
        dy = (features[0, start + 1:stop + 1, 1:width + 1] - rows).astype(np.float64)
        dx = (features[1, start + 1:stop + 1, 1:width + 1] - columns).astype(np.float64)
        distance = np.sqrt(dy * dy + dx * dx)
        np.maximum.at(peak, components[start:stop].ravel(), distance.ravel())
    return peak[1:]


def measure_colour_regions(labels: np.ndarray, colour: int, explicit: np.ndarray,
                           checkpoint: Callable[[], None] | None = None,
                           ) -> tuple[tuple[slice, slice], np.ndarray, int, np.ndarray,
                                      np.ndarray, np.ndarray]:
    """Measure the four-connected regions of one colour of the whole raster.

    Returns the colour's bounding box, the component labels inside it (0 off the
    colour, 1..count on it), the count, and per component (index 0 is component 1)
    its pixel count, feature width and whether it holds an explicitly protected
    pixel, exactly as ``cleanup_labels`` measures a component. The distances are
    taken on the bounding box padded by one pixel: any background pixel outside the
    box is at least as far as the pad ring between, so they equal the whole-raster
    padded EDT. ``checkpoint`` runs before the distance transform and may raise.
    """
    mask = labels == colour
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        empty = np.zeros(0)
        return ((slice(0, 0), slice(0, 0)), np.zeros((0, 0), dtype=np.int32), 0,
                empty.astype(np.int64), empty, empty.astype(bool))
    columns = np.flatnonzero(mask.any(axis=0))
    bounds = (slice(int(rows[0]), int(rows[-1]) + 1), slice(int(columns[0]), int(columns[-1]) + 1))
    mask = np.ascontiguousarray(mask[bounds])
    structure = ndimage.generate_binary_structure(2, 1)
    components, count = ndimage.label(mask, structure=structure)
    area = np.bincount(components.ravel(), minlength=count + 1)[1:].astype(np.int64)
    held = np.bincount(components[explicit[bounds].astype(bool) & mask],
                       minlength=count + 1)[1:] > 0
    if checkpoint is not None:
        checkpoint()
    width = 2.0 * _region_peak_distances(mask, components, count)
    return bounds, components, count, area, width, held


def remove_then_widen_stages(settings: CleanupSettings) -> tuple[CleanupSettings, CleanupSettings]:
    """The remove and widen settings a ``remove_then_widen`` cleanup runs in turn."""
    remove = replace(settings, cleanup_action="remove",
                     min_detail_width=settings.remove_below_width,
                     min_region_area=settings.min_region_area)
    widen = replace(settings, cleanup_action="widen",
                    min_detail_width=settings.min_detail_width, min_region_area=0.0)
    return remove, widen


def resolve_physical_size(settings: CleanupSettings, width: int, height: int) -> CleanupSettings:
    """The settings with a one-sided physical size completed from the raster's
    proportions. Without a size both dimensions stay ``None`` (pixel units)."""
    width_mm, height_mm = settings.width_mm, settings.height_mm
    if width_mm is not None and height_mm is None:
        height_mm = width_mm * height / width
    elif height_mm is not None and width_mm is None:
        width_mm = height_mm * width / height
    return replace(settings, width_mm=width_mm, height_mm=height_mm)


def pixel_scale(resolved: CleanupSettings, width: int) -> float:
    """Millimetres per raster pixel with a physical size, else 1 (pixel units)."""
    return resolved.width_mm / width if resolved.width_mm is not None else 1.0


def apply_cleanup(labels: np.ndarray, palette: list[dict], settings: CleanupSettings,
                  omitted: int | None, *, protected_features: np.ndarray,
                  explicit_protected_features: np.ndarray,
                  ) -> tuple[np.ndarray, np.ndarray, dict, CleanupSettings]:
    """Run cutter cleanup on a label raster under its physical size.

    ``omitted`` is the palette index of the removed colour: it gets no vinyl layer, so
    it is excluded from the cleanup regions and its pixels stay as they are.

    The user's explicit protections win: an explicitly protected pixel is never
    reassigned (see ``cleanup_labels``). Automatic protection is cleared wherever the
    cleanup changed a label, since that detail was removed on purpose; no explicitly
    protected pixel ever changes, so explicit protection stays as it was.

    Returns the cleaned labels, the updated automatic-plus-explicit protected mask,
    the cleanup summary (with the automatic protection it cleared) and the settings
    with the physical size resolved against the raster. This is the one cleanup
    implementation shared by the cleanup stage and the trace CLI.
    """
    height, width = labels.shape
    resolved = resolve_physical_size(settings, width, height)
    included = [index for index in range(len(palette)) if index != omitted]
    explicit = explicit_protected_features.astype(bool)
    cleaned, summary = cleanup_labels(labels, included, palette, resolved,
                                      pixel_scale(resolved, width), explicit=explicit)
    changed = cleaned != labels
    automatic = protected_features.astype(bool) & ~explicit
    cleared = automatic & changed
    # A protected detail is an eight-connected component, as tracing's protection
    # audit counts it (a diagonal stroke is one detail).
    components, _ = ndimage.label(automatic, structure=np.ones((3, 3), dtype=bool))
    touched = np.unique(components[cleared])
    summary["auto_protected_components_cleared"] = int(np.count_nonzero(touched))
    summary["auto_protected_pixels_cleared"] = int(cleared.sum())
    return cleaned, protected_features.astype(bool) & ~cleared, summary, resolved


def cleanup_labels(labels: np.ndarray, included: list[int], palette: list[dict],
                   settings: CleanupSettings, units_per_pixel: float,
                   *, explicit: np.ndarray | None = None,
                   regions: RegionMeasurements | None = None) -> tuple[np.ndarray, dict]:
    """Remove or widen undersized regions of ``included`` colours.

    ``explicit`` marks the user's explicitly protected pixels, which are never
    reassigned to another colour: an undersized region holding any of them is kept
    whole (counted in ``protected_kept_regions``) and widening never consumes one.
    Such a region may still be widened itself.

    ``regions``, when ``labels`` is a crop of a larger raster, holds that raster's
    whole-region measurements (``ids`` cropped like ``labels``). A region's area,
    width and explicit protection are then read from them rather than from its part
    inside the crop, so it is judged undersized exactly as a full run judges it.
    Only the first stage uses them: after it the raster differs from the measured one.
    """
    result = labels.copy()
    summary = {"enabled": settings.cleanup, "action": settings.cleanup_action,
               "removed_regions": 0, "removed_pixels": 0, "widened_regions": 0,
               "widened_pixels": 0, "unchanged_regions": 0, "protected_kept_regions": 0,
               "warnings": []}
    if not settings.cleanup:
        return result, summary
    width = settings.min_detail_width
    area = settings.min_region_area
    if settings.cleanup_action == "remove_then_widen":
        remove_settings, widen_settings = remove_then_widen_stages(settings)
        removed, remove_summary = cleanup_labels(
            result, included, palette, remove_settings, units_per_pixel, explicit=explicit,
            regions=regions)
        # The removal changed the raster, so the measurements no longer describe it.
        widened, widen_summary = cleanup_labels(
            removed, included, palette, widen_settings, units_per_pixel, explicit=explicit)
        combined = {"enabled": True, "action": "remove_then_widen",
                    "removed_regions": remove_summary["removed_regions"],
                    "removed_pixels": remove_summary["removed_pixels"],
                    "widened_regions": widen_summary["widened_regions"],
                    "widened_pixels": widen_summary["widened_pixels"],
                    "unchanged_regions": (remove_summary["unchanged_regions"] +
                                          widen_summary["unchanged_regions"]),
                    "protected_kept_regions": (remove_summary["protected_kept_regions"] +
                                               widen_summary["protected_kept_regions"]),
                    "warnings": [*remove_summary["warnings"], *widen_summary["warnings"]],
                    "stages": {"remove_below_area": remove_summary,
                               "widen_below_width": widen_summary}}
        return widened, combined
    width_px = (width / units_per_pixel) if width is not None else 0.0
    area_px = (area / (units_per_pixel * units_per_pixel)) if area is not None else 0.0
    labs = srgb_to_lab(np.asarray([row["rgb"] for row in palette], dtype=np.uint8))
    structure = ndimage.generate_binary_structure(2, 1)
    snapshot = result.copy()

    # One padded EDT of each colour's full snapshot mask. For a four-connected
    # component K of that colour, the padded EDT of K's cropped bounding box
    # equals this field at every pixel of K: the nearest non-K pixel is never a
    # same-colour pixel of another component (a four-path to it inside their
    # bounding rectangle would connect them, and some rectangle pixel is nearer),
    # and the box pad is matched by a real other-colour pixel or the image pad.
    # Each colour's field is only read while that colour's components are
    # being classified, so a single field is held at a time.
    def snapshot_field(colour: int) -> np.ndarray:
        return _padded_edt(snapshot == colour)

    if settings.cleanup_action == "remove":
        # Discover every undersized component before changing any labels. If
        # adjacent small components are handled independently, A can choose B
        # while B chooses A and the two colours merely swap. Grouping the full
        # small-component mask guarantees that its destination is a stable,
        # touching component outside the group.
        small_mask = np.zeros(result.shape, dtype=bool)
        small_component_ids = np.zeros(result.shape, dtype=np.int32)
        component_number = 0
        if regions is not None:
            undersized = np.zeros(regions.area.shape, dtype=bool)
            if area_px > 0:
                undersized |= regions.area < area_px
            if width_px > 0:
                undersized |= regions.width < width_px
            small_pixels = undersized[regions.ids] & np.isin(snapshot, included)
            kept = small_pixels & regions.explicit[regions.ids]
            summary["protected_kept_regions"] += int(np.unique(regions.ids[kept]).size)
            small_mask = small_pixels & ~kept
            small_component_ids = np.where(small_mask, regions.ids + 1, 0).astype(np.int32)
        for source in (included if regions is None else ()):
            components, count = ndimage.label(snapshot == source, structure=structure)
            objects = ndimage.find_objects(components)
            field = snapshot_field(source)
            for component in range(1, count + 1):
                bounds = objects[component - 1]
                local = components[bounds] == component
                pixels = int(local.sum())
                feature_width = 2.0 * float(field[bounds][local].max())
                too_small = ((area_px > 0 and pixels < area_px) or
                             (width_px > 0 and feature_width < width_px))
                if too_small and explicit is not None and explicit[bounds][local].any():
                    summary["protected_kept_regions"] += 1
                    continue
                if too_small:
                    component_number += 1
                    small_mask[bounds][local] = True
                    small_component_ids[bounds][local] = component_number
        groups, group_count = ndimage.label(small_mask, structure=structure)
        group_objects = ndimage.find_objects(groups)
        for group in range(1, group_count + 1):
            # A group's one-pixel four-connected border lies inside its bounding
            # box padded by one pixel, so every test runs on that window.
            window = tuple(slice(max(0, s.start - 1), min(result.shape[i], s.stop + 1))
                           for i, s in enumerate(group_objects[group - 1]))
            group_mask = groups[window] == group
            original_components = np.unique(small_component_ids[window][group_mask])
            original_components = original_components[original_components > 0]
            border = ndimage.binary_dilation(group_mask, structure=structure) & ~group_mask
            neighbours = [int(value)
                          for value in np.unique(snapshot[window][border & ~small_mask[window]])
                          if int(value) in included]
            if not neighbours:
                summary["unchanged_regions"] += len(original_components)
                summary["warnings"].append(
                    f"undersized component group {group}: no stable touching included colour")
                continue
            source_values, counts = np.unique(snapshot[window][group_mask], return_counts=True)
            source_lab = np.average(labs[source_values], axis=0, weights=counts)
            target = min(neighbours,
                         key=lambda value: (float(np.linalg.norm(labs[value] - source_lab)), value))
            result[window][group_mask] = target
            summary["removed_regions"] += len(original_components)
            summary["removed_pixels"] += int(group_mask.sum())
        return result, summary

    height, width_pixels = result.shape
    # Padded EDT of each colour on the CURRENT result, keyed by colour. A colour
    # that loses pixels (as a widening's target) is updated in its consumed-pixel
    # window; a colour that gains pixels (as a widening's source) is dropped and
    # rebuilt lazily on next use. ``max_field`` holds a non-increasing upper
    # bound on each field's maximum for sizing consumed-pixel distance windows.
    distance_fields: dict[int, np.ndarray] = {}
    max_field: dict[int, float] = {}

    def distance_field(colour: int) -> np.ndarray:
        field = distance_fields.get(colour)
        if field is None:
            field = _padded_edt(result == colour)
            distance_fields[colour] = field
            max_field[colour] = float(field.max()) if field.size else 0.0
        return field

    visited = np.zeros(height * width_pixels, dtype=np.int32)
    stack = np.empty(height * width_pixels, dtype=np.int32)
    generation = 0

    for source in included:
        components, count = ndimage.label(snapshot == source, structure=structure)
        objects = ndimage.find_objects(components)
        source_field = snapshot_field(source)
        for component in range(1, count + 1):
            bounds = objects[component - 1]
            local = components[bounds] == component
            # The component's padded-box EDT: the per-colour field on its pixels,
            # zero on the rest of the box (background there in the box transform).
            distance = np.where(local, source_field[bounds], 0.0)
            if regions is None:
                pixels = int(local.sum())
                feature_width = 2.0 * float(distance.max())
            else:
                region = int(regions.ids[bounds][local][0])
                pixels = int(regions.area[region])
                feature_width = float(regions.width[region])
            too_small = (area_px > 0 and pixels < area_px) or (width_px > 0 and feature_width < width_px)
            local_growth_seed = None
            if settings.cleanup_action == "widen" and width_px > 0:
                ridge = ((distance > 0) &
                         (distance >= ndimage.maximum_filter(distance, size=3)))
                # Raster centre distances overstate a one-pixel stroke as two
                # pixels wide. Subtract one pixel when selecting locally thin
                # medial ridges, including branches attached to thick bodies.
                local_seed = ridge & ((2.0 * distance - 1.0) < width_px)
                local_growth_seed = local_seed
                too_small = too_small or bool(local_seed.any())
            if not too_small:
                continue
            needed = (max(1, int(math.ceil(width_px / 2)))
                      if local_growth_seed is not None and local_growth_seed.any()
                      else max(1, int(math.ceil(width_px / 2 - feature_width / 2))))
            padding = needed + 1
            expanded = tuple(slice(max(0, s.start - padding),
                                   min(result.shape[i], s.stop + padding))
                             for i, s in enumerate(bounds))
            component_mask = components[expanded] == component
            neighbours = np.unique(snapshot[expanded][
                ndimage.binary_dilation(component_mask, structure=structure) & ~component_mask])
            neighbours = [int(n) for n in neighbours if int(n) in included and int(n) != source]
            if not neighbours:
                summary["unchanged_regions"] += 1
                summary["warnings"].append(f"{palette[source]['name']} component {component}: no touching included colour")
                continue
            target = min(neighbours, key=lambda n: (float(np.linalg.norm(labs[n] - labs[source])), n))
            if local_growth_seed is not None and local_growth_seed.any():
                seed = np.zeros_like(component_mask)
                local_slices = tuple(slice(bounds[i].start - expanded[i].start,
                                           bounds[i].stop - expanded[i].start)
                                     for i in range(2))
                seed[local_slices] = local_growth_seed
                grown = ndimage.binary_dilation(seed, iterations=needed) | component_mask
            else:
                grown = ndimage.binary_dilation(component_mask, iterations=needed)
            result_view = result[expanded]
            targets = grown & (result_view == target)
            if explicit is not None:
                guarded = targets & explicit[expanded]
                if guarded.any():
                    targets &= ~guarded
                    if not targets.any():
                        # Every pixel the widening needed is explicitly protected, so
                        # nothing can grow: this is a refusal, not a widening.
                        summary["unchanged_regions"] += 1
                        summary["warnings"].append(
                            f"{palette[source]['name']} component {component}: widening "
                            "blocked by explicitly protected pixels")
                        continue
            # Protect each neighbouring band by its own residual geometry rather
            # than a global colour total: consuming these pixels must not thin any
            # affected neighbour component below the feature-width threshold, nor
            # below the area threshold when one is configured.
            seed_rows, seed_cols = np.nonzero(targets)
            if seed_rows.size == 0:
                # No target pixels lie under the growth, so nothing is consumed;
                # the widening is recorded with zero pixels and no residual test.
                summary["widened_regions"] += 1
                summary["widened_pixels"] += 0
                continue
            field = distance_field(target)
            radius = int(math.ceil(max_field[target]))
            global_rows = seed_rows + expanded[0].start
            global_cols = seed_cols + expanded[1].start
            cr0 = int(global_rows.min())
            cr1 = int(global_rows.max()) + 1
            cc0 = int(global_cols.min())
            cc1 = int(global_cols.max()) + 1
            wr0 = max(0, cr0 - radius)
            wr1 = min(height, cr1 + radius)
            wc0 = max(0, cc0 - radius)
            wc1 = min(width_pixels, cc1 + radius)
            consumed_window = np.zeros((wr1 - wr0, wc1 - wc0), dtype=bool)
            consumed_window[global_rows - wr0, global_cols - wc0] = True
            # Distance to the nearest consumed pixel; only actual consumed pixels
            # are background, so the window border is not treated as consumed.
            dist_window = ndimage.distance_transform_edt(~consumed_window)
            seed_idx = global_rows.astype(np.int64) * width_pixels + global_cols
            generation += 1
            refused = _widen_refusal(
                result, target, seed_idx, targets,
                expanded[0].start, expanded[1].start,
                field, dist_window, wr0, wc0,
                visited, generation, stack, width_px, area_px)
            if refused:
                summary["unchanged_regions"] += 1
                summary["warnings"].append(f"{palette[source]['name']} component {component}: widening constrained by neighbour threshold")
            else:
                result_view[targets] = source
                # Removing the consumed pixels can only shorten distances to the
                # nearest non-target pixel, so the field update is a minimum in
                # the consumed-pixel window and unchanged elsewhere.
                field[wr0:wr1, wc0:wc1] = np.minimum(field[wr0:wr1, wc0:wc1], dist_window)
                distance_fields.pop(source, None)
                max_field.pop(source, None)
                summary["widened_regions"] += 1
                summary["widened_pixels"] += int(targets.sum())
    return result, summary
