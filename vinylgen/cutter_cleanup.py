from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
from numba import njit
from scipy import ndimage

from vinylgen.colour import srgb_to_lab
from vinylgen.contracts import TraceSettings


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


def cleanup_labels(labels: np.ndarray, included: list[int], palette: list[dict],
                   settings: TraceSettings, units_per_pixel: float) -> tuple[np.ndarray, dict]:
    result = labels.copy()
    summary = {"enabled": settings.cleanup, "action": settings.cleanup_action,
               "removed_regions": 0, "removed_pixels": 0, "widened_regions": 0,
               "widened_pixels": 0, "unchanged_regions": 0, "warnings": []}
    if not settings.cleanup:
        return result, summary
    width = settings.min_detail_width
    area = settings.min_region_area
    if settings.cleanup_action == "remove_then_widen":
        remove_settings = replace(settings, cleanup_action="remove",
                                  min_detail_width=settings.remove_below_width,
                                  min_region_area=area)
        removed, remove_summary = cleanup_labels(
            result, included, palette, remove_settings, units_per_pixel)
        widen_settings = replace(settings, cleanup_action="widen",
                                 min_detail_width=width, min_region_area=0.0)
        widened, widen_summary = cleanup_labels(
            removed, included, palette, widen_settings, units_per_pixel)
        combined = {"enabled": True, "action": "remove_then_widen",
                    "removed_regions": remove_summary["removed_regions"],
                    "removed_pixels": remove_summary["removed_pixels"],
                    "widened_regions": widen_summary["widened_regions"],
                    "widened_pixels": widen_summary["widened_pixels"],
                    "unchanged_regions": (remove_summary["unchanged_regions"] +
                                          widen_summary["unchanged_regions"]),
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
        for source in included:
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
            pixels = int(local.sum())
            # The component's padded-box EDT: the per-colour field on its pixels,
            # zero on the rest of the box (background there in the box transform).
            distance = np.where(local, source_field[bounds], 0.0)
            feature_width = 2.0 * float(distance.max())
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
