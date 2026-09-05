from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
from scipy import ndimage

from vinylgen.colour import srgb_to_lab
from vinylgen.contracts import TraceSettings


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
            for component in range(1, count + 1):
                bounds = objects[component - 1]
                local = components[bounds] == component
                pixels = int(local.sum())
                distance = ndimage.distance_transform_edt(np.pad(local, 1))[1:-1, 1:-1]
                feature_width = 2.0 * float(distance.max())
                too_small = ((area_px > 0 and pixels < area_px) or
                             (width_px > 0 and feature_width < width_px))
                if too_small:
                    component_number += 1
                    small_mask[bounds][local] = True
                    small_component_ids[bounds][local] = component_number
        groups, group_count = ndimage.label(small_mask, structure=structure)
        for group in range(1, group_count + 1):
            group_mask = groups == group
            original_components = np.unique(small_component_ids[group_mask])
            original_components = original_components[original_components > 0]
            border = ndimage.binary_dilation(group_mask, structure=structure) & ~group_mask
            neighbours = [int(value) for value in np.unique(snapshot[border & ~small_mask])
                          if int(value) in included]
            if not neighbours:
                summary["unchanged_regions"] += len(original_components)
                summary["warnings"].append(
                    f"undersized component group {group}: no stable touching included colour")
                continue
            source_values, counts = np.unique(snapshot[group_mask], return_counts=True)
            source_lab = np.average(labs[source_values], axis=0, weights=counts)
            target = min(neighbours,
                         key=lambda value: (float(np.linalg.norm(labs[value] - source_lab)), value))
            result[group_mask] = target
            summary["removed_regions"] += len(original_components)
            summary["removed_pixels"] += int(group_mask.sum())
        return result, summary
    for source in included:
        components, count = ndimage.label(snapshot == source, structure=structure)
        objects = ndimage.find_objects(components)
        for component in range(1, count + 1):
            bounds = objects[component - 1]
            local = components[bounds] == component
            pixels = int(local.sum())
            distance = ndimage.distance_transform_edt(np.pad(local, 1))[1:-1, 1:-1]
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
            consumed = np.zeros(result.shape, dtype=bool)
            consumed[expanded] = targets
            neighbour_labels, _ = ndimage.label(result == target, structure=structure)
            neighbour_objects = ndimage.find_objects(neighbour_labels)
            refused = False
            for neighbour_id in np.unique(neighbour_labels[consumed]):
                if neighbour_id == 0:
                    continue
                neighbour_bounds = neighbour_objects[neighbour_id - 1]
                component_local = neighbour_labels[neighbour_bounds] == neighbour_id
                residual = component_local & ~consumed[neighbour_bounds]
                residual_pixels = int(residual.sum())
                residual_distance = ndimage.distance_transform_edt(
                    np.pad(residual, 1))[1:-1, 1:-1]
                residual_width = 2.0 * float(residual_distance.max())
                if ((width_px > 0 and residual_width < width_px) or
                        (area_px > 0 and residual_pixels < area_px)):
                    refused = True
                    break
            if refused:
                summary["unchanged_regions"] += 1
                summary["warnings"].append(f"{palette[source]['name']} component {component}: widening constrained by neighbour threshold")
            else:
                result_view[targets] = source
                summary["widened_regions"] += 1
                summary["widened_pixels"] += int(targets.sum())
    return result, summary

