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
from typing import Callable

import numpy as np
from PIL import Image
from vinylgen.contracts import PaletteSettings, SegmentationBundle
from vinylgen.colour import (
    analysis_image,
    assign,
    despeckle,
    palette_costs,
    colour_names,
    hex_colour,
    lab_to_srgb,
    reduced_histogram,
    srgb_to_lab,
    weighted_kmeans,
)
from vinylgen.segmentation import (
    collapse_dither_speckle,
    fold_underrepresented_colours,
    detect_intentional_strokes,
    protect_components_at_points,
    remove_small_regions,
    resolve_ambiguous_boundaries,
    spatial_stats,
)
from vinylgen.reporting import create_report


# Reports fractional stage completion (0..1) and a human-readable activity label.
Progress = Callable[[float, str], None]


def _noop(fraction: float, message: str) -> None:
    pass


def merge_clusters(
    centres: np.ndarray,
    counts: np.ndarray,
    label_map: np.ndarray,
    settings: PaletteSettings,
    distinct_cluster_pairs: set[tuple[int, int]] | None = None,
    progress: Progress | None = None,
) -> tuple[np.ndarray, list[dict[str, object]]]:
    merges: list[dict[str, object]] = []
    total = float(counts.sum())
    groups = [{index} for index in range(len(centres))]
    distinct = distinct_cluster_pairs or set()
    target_count = settings.final_colours or settings.min_output_colours
    tick = progress or _noop
    start_count = len(centres)
    span = max(1, start_count - target_count)
    while len(centres) > target_count:
        tick((start_count - len(centres)) / span, f"Merging similar colours ({len(centres)} left)")
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
                elif (
                    settings.fold_small_colours
                    and share <= settings.small_share
                    and distance <= settings.small_delta_e
                ):
                    reason = "small and similar"
                elif (
                    settings.merge_fragmented_colours
                    and share <= settings.fragmented_share
                    and stats[smaller]["largest_region_share"]
                    <= settings.fragmented_largest_region
                    and distance <= settings.fragmented_delta_e
                ):
                    reason = "small, fragmented, and similar"
                if reason:
                    candidates.append((distance, a, b, reason))
        if not candidates and settings.final_colours is not None:
            for a in range(len(centres)):
                for b in range(a + 1, len(centres)):
                    if any(
                        (left in groups[a] and right in groups[b])
                        or (right in groups[a] and left in groups[b])
                        for left, right in distinct
                    ):
                        continue
                    candidates.append((float(distances[a, b]), a, b, "fixed colour count"))
        if not candidates:
            if settings.final_colours is not None:
                raise ValueError(
                    f"cannot reach fixed colour count {settings.final_colours}; explicit distinct-colour constraints prevent further merges"
                )
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


def _scale2x_labels(labels: np.ndarray, guard: np.ndarray) -> np.ndarray:
    """Double a palette-index grid with Scale2x/EPX staircase smoothing.

    Each pixel becomes a 2x2 block whose outer corners are filled with an
    adjacent label only where a diagonal step exists, so the result never leaves
    the source palette. Pixels flagged in ``guard`` keep a solid block, leaving
    protected features and intentional linework un-eroded.
    """
    padded = np.pad(labels, 1, mode="edge")
    centre = labels
    up = padded[:-2, 1:-1]
    down = padded[2:, 1:-1]
    left = padded[1:-1, :-2]
    right = padded[1:-1, 2:]
    top_left = np.where((left == up) & (up != right) & (left != down), left, centre)
    top_right = np.where((up == right) & (up != left) & (right != down), right, centre)
    bottom_left = np.where((down == left) & (down != right) & (left != up), left, centre)
    bottom_right = np.where((right == down) & (right != left) & (down != up), right, centre)
    quadrants = (top_left, top_right, bottom_left, bottom_right)
    top_left, top_right, bottom_left, bottom_right = (
        np.where(guard, centre, quadrant) for quadrant in quadrants
    )
    out = np.empty((labels.shape[0] * 2, labels.shape[1] * 2), dtype=labels.dtype)
    out[0::2, 0::2] = top_left
    out[0::2, 1::2] = top_right
    out[1::2, 0::2] = bottom_left
    out[1::2, 1::2] = bottom_right
    return out


def infer_palette(path: Path, output_dir: Path, settings: PaletteSettings,
                  progress: Progress | None = None) -> dict[str, object]:
    tick = progress or _noop
    tick(0.0, "Analysing source colours")
    # Palette discovery runs on a median-despeckled copy so dither blends never
    # spawn their own palette entries, while the raw source drives the faithful
    # full-resolution baseline and thin-feature detection. The original file is
    # still copied verbatim to source.png below; nothing here overwrites it.
    original = Image.open(path).convert("RGB")
    source = despeckle(original, settings.dither_radius)
    analysed = analysis_image(source, settings.analysis_max_dimension)
    analysis_rgb = np.asarray(analysed)
    histogram_rgb, histogram_weights = reduced_histogram(analysis_rgb, settings.histogram_bits)
    if settings.final_colours is not None:
        if settings.final_colours > settings.initial_colours:
            raise ValueError("fixed final colour count cannot exceed initial colour candidates")
        if settings.final_colours > len(histogram_rgb):
            raise ValueError(
                f"fixed colour count {settings.final_colours} exceeds the source's {len(histogram_rgb)} analysed colours"
            )
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
    source_rgb = np.asarray(original)
    source_height, source_width = source_rgb.shape[:2]
    for first, second in settings.distinct_colour_point_pairs:
        for x, y in (first, second):
            if x < 0 or y < 0 or x >= source_width or y >= source_height:
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
        centres, counts, label_map, settings, distinct_cluster_pairs,
        progress=lambda local, message: tick(0.05 + local * 0.10, message),
    )

    tick(0.15, "Mapping palette to full resolution")
    full_rgb = source_rgb
    full_lab_flat = srgb_to_lab(full_rgb.reshape(-1, 3))
    full_lab = full_lab_flat.reshape((*full_rgb.shape[:2], 3))
    initial_costs = palette_costs(full_lab_flat, centres).reshape(
        (*full_rgb.shape[:2], len(centres))
    )
    full_labels = initial_costs.argmin(axis=2).astype(np.int32)
    # An independent assignment of the despeckled copy. The dither pass below
    # reassigns a pixel only where this proposal and the neighbourhood majority
    # agree, so speckle is removed by consensus while lone true detail survives.
    # When despeckling is off this equals the baseline, so the pass is a no-op.
    despeckled_labels = palette_costs(
        srgb_to_lab(np.asarray(source).reshape(-1, 3)), centres
    ).reshape((*full_rgb.shape[:2], len(centres))).argmin(axis=2).astype(np.int32)
    tick(0.35, "Protecting connected details")
    explicit_protection, protected_components = protect_components_at_points(
        full_labels, settings.protected_component_points
    )
    tick(0.40, "Detecting intentional linework")
    if settings.protect_intentional_lines:
        full_labels, protected_features, feature_detection = detect_intentional_strokes(
            full_lab, full_labels, initial_costs, centres,
            minimum_contrast=settings.intentional_line_min_contrast,
            minimum_length=settings.intentional_line_min_length,
            locked_features=explicit_protection,
        )
    else:
        # Automatic line detection off: labels pass through unconsolidated and only
        # the user's explicit point protections carry into later cleanup.
        protected_features = explicit_protection.copy()
        feature_detection = {"skipped": True,
                             "explicitly_locked_pixels": int(explicit_protection.sum())}
    tick(0.50, "Collapsing dither speckle")
    full_labels, dither_cleanup = collapse_dither_speckle(
        full_labels, despeckled_labels, len(centres), settings.dither_radius,
        protected_features | explicit_protection,
    )
    tick(0.52, "Resolving anti-aliased boundaries")
    if settings.resolve_antialiasing:
        full_labels, aa_resolution = resolve_ambiguous_boundaries(
            full_labels, initial_costs, protected_features,
            ambiguity_margin=settings.aa_ambiguity_margin,
            smoothness=settings.aa_smoothness,
            passes=settings.aa_resolution_passes,
        )
    else:
        aa_resolution = {"skipped": True}
    tick(0.72, "Removing small regions")
    full_labels, cleanup = remove_small_regions(
        full_labels, centres, settings.min_region_pixels,
        protected_features=protected_features,
    )
    preserved_colours: set[int] = set()
    preserved_colour_points: list[dict[str, int]] = []
    height, width = full_labels.shape
    for x, y in settings.preserved_colour_points:
        if x < 0 or y < 0 or x >= width or y >= height:
            raise ValueError(f"preserved colour point ({x}, {y}) is outside {width}x{height} source")
        colour = int(full_labels[y, x])
        preserved_colours.add(colour)
        preserved_colour_points.append({"x": x, "y": y, "colour_index_before_final_remap": colour})
    tick(0.82, "Folding underrepresented colours")
    full_labels, colour_cleanup = fold_underrepresented_colours(
        full_labels, centres, settings.min_colour_share,
        settings.final_colours or settings.min_output_colours,
        preserved_colours=preserved_colours,
    )
    used = np.unique(full_labels)
    tick(0.86, "Finalising palette")
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
    final_costs = palette_costs(full_lab_flat, centres).reshape(
        (*full_labels.shape, len(centres))
    )
    factor = settings.flatten_upscale
    if factor > 1:
        smoothed = full_labels
        remaining = factor
        if settings.flatten_smooth_edges:
            # Scale2x doubles the grid, so consume the power-of-two part of the
            # factor with smoothing passes and repeat any odd remainder plainly.
            guard = protected_features | explicit_protection
            while remaining % 2 == 0:
                smoothed = _scale2x_labels(smoothed, guard)
                guard = np.repeat(np.repeat(guard, 2, axis=0), 2, axis=1)
                remaining //= 2
        if remaining > 1:
            smoothed = np.repeat(np.repeat(smoothed, remaining, axis=0), remaining, axis=1)
        full_labels = smoothed
        protected_features = np.repeat(np.repeat(protected_features, factor, axis=0), factor, axis=1)
        explicit_protection = np.repeat(np.repeat(explicit_protection, factor, axis=0), factor, axis=1)
        final_costs = np.repeat(np.repeat(final_costs, factor, axis=0), factor, axis=1)
    flattened = palette_rgb[full_labels]

    tick(0.92, "Rendering flattened image")
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
    tick(0.96, "Building palette report")
    create_report(Image.fromarray(flattened, "RGB"), palette).save(subject_dir / "report.png")
    report: dict[str, object] = {
        "source": str(path),
        "segmentation": str(segmentation_path),
        "dimensions": [full_labels.shape[1], full_labels.shape[0]],
        "inferred_colour_count": len(centres),
        "fixed_colour_count": {
            "requested": settings.final_colours,
            "produced": len(centres),
            "met": settings.final_colours is None or len(centres) == settings.final_colours,
        },
        "palette": palette,
        "merges": merges,
        "dither_speckle_cleanup": dither_cleanup,
        "small_region_cleanup": cleanup,
        "intentional_feature_detection": feature_detection,
        "aa_boundary_resolution": aa_resolution,
        "explicitly_protected_components": protected_components,
        "explicitly_preserved_colours": preserved_colour_points,
        "underrepresented_colour_cleanup": colour_cleanup,
        "settings": asdict(settings),
    }
    (subject_dir / "palette.json").write_text(json.dumps(report, indent=2) + "\n")
    tick(1.0, "Palette ready")
    return report
