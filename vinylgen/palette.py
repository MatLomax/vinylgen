#!/usr/bin/env python3
"""Infer and flatten a raster image to its visually meaningful palette.

The algorithm is deterministic. It over-clusters a quantized RGB histogram in
CIE Lab, then merges clusters using perceptual distance, occupied area, and
spatial coherence. It intentionally preserves small high-contrast accents.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image
from vinylgen.contracts import PaletteSettings, SegmentationBundle
from vinylgen.colour import (
    analysis_image,
    assign,
    palette_costs,
    colour_names,
    hex_colour,
    lab_to_srgb,
    reduced_histogram,
    srgb_to_lab,
    weighted_kmeans,
)
from vinylgen.segmentation import (
    fold_underrepresented_colours,
    detect_intentional_strokes,
    protect_components_at_points,
    remove_small_regions,
    resolve_ambiguous_boundaries,
    spatial_stats,
)
from vinylgen.reporting import create_report


def merge_clusters(
    centres: np.ndarray,
    counts: np.ndarray,
    label_map: np.ndarray,
    settings: PaletteSettings,
    distinct_cluster_pairs: set[tuple[int, int]] | None = None,
) -> tuple[np.ndarray, list[dict[str, object]]]:
    merges: list[dict[str, object]] = []
    total = float(counts.sum())
    groups = [{index} for index in range(len(centres))]
    distinct = distinct_cluster_pairs or set()
    while len(centres) > settings.min_output_colours:
        stats = spatial_stats(label_map, len(centres))
        distances = np.linalg.norm(centres[:, None, :] - centres[None, :, :], axis=2)
        np.fill_diagonal(distances, np.inf)
        candidates: list[tuple[float, int, int, str]] = []
        for a in range(len(centres)):
            for b in range(a + 1, len(centres)):
                if any(
                    (left in groups[a] and right in groups[b])
                    or (right in groups[a] and left in groups[b])
                    for left, right in distinct
                ):
                    continue
                distance = float(distances[a, b])
                smaller = a if counts[a] <= counts[b] else b
                share = float(counts[smaller] / total)
                reason = None
                if distance <= settings.similar_delta_e:
                    reason = "perceptually similar"
                elif share <= settings.small_share and distance <= settings.small_delta_e:
                    reason = "small and similar"
                elif (
                    share <= settings.fragmented_share
                    and stats[smaller]["largest_region_share"]
                    <= settings.fragmented_largest_region
                    and distance <= settings.fragmented_delta_e
                ):
                    reason = "small, fragmented, and similar"
                if reason:
                    candidates.append((distance, a, b, reason))
        if not candidates:
            break
        distance, a, b, reason = min(candidates)
        keep, remove = (a, b) if counts[a] >= counts[b] else (b, a)
        old_rgb = lab_to_srgb(np.vstack((centres[keep], centres[remove])))
        combined = counts[keep] + counts[remove]
        centres[keep] = (centres[keep] * counts[keep] + centres[remove] * counts[remove]) / combined
        counts[keep] = combined
        merges.append(
            {
                "from": hex_colour(old_rgb[1]),
                "into": hex_colour(old_rgb[0]),
                "delta_e_76": round(distance, 3),
                "reason": reason,
                "removed_share_percent": round(100 * float(counts[remove] / total), 4),
            }
        )
        label_map[label_map == remove] = keep
        label_map[label_map > remove] -= 1
        groups[keep] |= groups[remove]
        del groups[remove]
        centres = np.delete(centres, remove, axis=0)
        counts = np.delete(counts, remove)
    return centres, merges


def infer_palette(path: Path, output_dir: Path, settings: PaletteSettings) -> dict[str, object]:
    source = Image.open(path).convert("RGB")
    analysed = analysis_image(source, settings.analysis_max_dimension)
    analysis_rgb = np.asarray(analysed)
    histogram_rgb, histogram_weights = reduced_histogram(analysis_rgb, settings.histogram_bits)
    histogram_lab = srgb_to_lab(histogram_rgb)
    centres, histogram_labels = weighted_kmeans(
        histogram_lab, histogram_weights, settings.initial_colours
    )
    counts = np.bincount(
        histogram_labels,
        weights=histogram_weights,
        minlength=len(centres),
    ).astype(np.float64)
    analysis_lab = srgb_to_lab(analysis_rgb.reshape(-1, 3))
    label_map = assign(analysis_lab, centres).reshape(analysis_rgb.shape[:2])
    distinct_cluster_pairs: set[tuple[int, int]] = set()
    source_rgb = np.asarray(source)
    source_height, source_width = source_rgb.shape[:2]
    for first, second in settings.distinct_colour_point_pairs:
        for x, y in (first, second):
            if x >= source_width or y >= source_height:
                raise ValueError(
                    f"distinct-colour point ({x}, {y}) is outside {source_width}x{source_height} source"
                )
        samples = np.asarray([source_rgb[first[1], first[0]], source_rgb[second[1], second[0]]])
        sampled_clusters = assign(srgb_to_lab(samples), centres)
        if int(sampled_clusters[0]) == int(sampled_clusters[1]):
            raise ValueError(
                f"distinct-colour points {first} and {second} already belong to the same initial cluster"
            )
        distinct_cluster_pairs.add(tuple(sorted(map(int, sampled_clusters))))
    centres, merges = merge_clusters(
        centres, counts, label_map, settings, distinct_cluster_pairs
    )

    full_rgb = source_rgb
    full_lab_flat = srgb_to_lab(full_rgb.reshape(-1, 3))
    full_lab = full_lab_flat.reshape((*full_rgb.shape[:2], 3))
    initial_costs = palette_costs(full_lab_flat, centres).reshape(
        (*full_rgb.shape[:2], len(centres))
    )
    full_labels = initial_costs.argmin(axis=2).astype(np.int32)
    explicit_protection, protected_components = protect_components_at_points(
        full_labels, settings.protected_component_points
    )
    full_labels, protected_features, feature_detection = detect_intentional_strokes(
        full_lab, full_labels, initial_costs, centres,
        minimum_contrast=settings.intentional_line_min_contrast,
        minimum_length=settings.intentional_line_min_length,
        locked_features=explicit_protection,
    )
    full_labels, aa_resolution = resolve_ambiguous_boundaries(
        full_labels, initial_costs, protected_features,
        ambiguity_margin=settings.aa_ambiguity_margin,
        smoothness=settings.aa_smoothness,
        passes=settings.aa_resolution_passes,
    )
    full_labels, cleanup = remove_small_regions(
        full_labels, centres, settings.min_region_pixels,
        maximum_delta_e=settings.small_delta_e,
        protected_features=protected_features,
    )
    preserved_colours: set[int] = set()
    preserved_colour_points: list[dict[str, int]] = []
    height, width = full_labels.shape
    for x, y in settings.preserved_colour_points:
        if x >= width or y >= height:
            raise ValueError(f"preserved colour point ({x}, {y}) is outside {width}x{height} source")
        colour = int(full_labels[y, x])
        preserved_colours.add(colour)
        preserved_colour_points.append({"x": x, "y": y, "colour_index_before_final_remap": colour})
    full_labels, colour_cleanup = fold_underrepresented_colours(
        full_labels, centres, settings.min_colour_share, settings.min_output_colours,
        preserved_colours=preserved_colours,
    )
    used = np.unique(full_labels)
    centres = centres[used]
    used_remap = np.full(int(used.max()) + 1, -1, dtype=np.int32)
    used_remap[used] = np.arange(len(used))
    full_labels = used_remap[full_labels]
    full_counts = np.bincount(full_labels.ravel(), minlength=len(centres))
    order = np.argsort(-full_counts)
    centres = centres[order]
    full_counts = full_counts[order]
    palette_rgb = lab_to_srgb(centres)
    remap = np.empty(len(order), dtype=np.int32)
    remap[order] = np.arange(len(order))
    full_labels = remap[full_labels]
    flattened = palette_rgb[full_labels]
    final_costs = palette_costs(full_lab_flat, centres).reshape(
        (*full_labels.shape, len(centres))
    )

    stem = path.stem
    subject_dir = output_dir / stem
    subject_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, subject_dir / "source.png")
    Image.fromarray(flattened, "RGB").save(subject_dir / "flattened.png")
    segmentation_path = subject_dir / "segmentation.npz"
    segmentation = SegmentationBundle(
        labels=full_labels,
        protected_features=protected_features,
        explicit_protected_features=explicit_protection,
        palette_rgb=palette_rgb,
        palette_costs=final_costs,
    )
    segmentation.validate()
    segmentation.save(segmentation_path)
    final_map = full_labels
    final_stats = spatial_stats(final_map, len(centres))
    total = int(full_counts.sum())
    palette = []
    names = colour_names(palette_rgb)
    for index, (rgb, count) in enumerate(zip(palette_rgb, full_counts)):
        palette.append(
            {
                "index": index,
                "name": names[index],
                "hex": hex_colour(rgb),
                "rgb": [int(v) for v in rgb],
                "share_percent": round(100 * int(count) / total, 4),
                **final_stats[index],
            }
        )
    create_report(Image.fromarray(flattened, "RGB"), palette).save(subject_dir / "report.png")
    report: dict[str, object] = {
        "source": str(path),
        "segmentation": str(segmentation_path),
        "dimensions": list(source.size),
        "inferred_colour_count": len(centres),
        "palette": palette,
        "merges": merges,
        "small_region_cleanup": cleanup,
        "intentional_feature_detection": feature_detection,
        "aa_boundary_resolution": aa_resolution,
        "explicitly_protected_components": protected_components,
        "explicitly_preserved_colours": preserved_colour_points,
        "underrepresented_colour_cleanup": colour_cleanup,
        "settings": asdict(settings),
    }
    (subject_dir / "palette.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
