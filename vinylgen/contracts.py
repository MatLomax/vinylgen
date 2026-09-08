from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass(frozen=True)
class PaletteSettings:
    initial_colours: int = 16
    final_colours: int | None = None
    histogram_bits: int = 5
    analysis_max_dimension: int = 640
    dither_radius: int = 1
    similar_delta_e: float = 28.0
    small_share: float = 0.004
    small_delta_e: float = 36.0
    fold_small_colours: bool = True
    fragmented_share: float = 0.035
    fragmented_largest_region: float = 0.30
    fragmented_delta_e: float = 24.0
    merge_fragmented_colours: bool = True
    min_output_colours: int = 2
    min_region_pixels: int = 24
    min_feature_width: int = 0
    min_colour_share: float = 0.01
    intentional_line_min_contrast: float = 12.0
    intentional_line_min_length: int = 8
    protect_intentional_lines: bool = True
    aa_ambiguity_margin: float = 7.0
    aa_smoothness: float = 2.5
    aa_resolution_passes: int = 5
    resolve_antialiasing: bool = True
    flatten_upscale: int = 1
    flatten_smooth_edges: bool = True
    protected_component_points: tuple[tuple[int, int], ...] = ()
    preserved_colour_points: tuple[tuple[int, int], ...] = ()
    distinct_colour_point_pairs: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = ()


@dataclass
class SegmentationBundle:
    """Explicit handoff from raster segmentation to vector construction."""

    labels: np.ndarray
    protected_features: np.ndarray
    explicit_protected_features: np.ndarray
    palette_rgb: np.ndarray
    palette_costs: np.ndarray

    def save(self, path: Path) -> None:
        np.savez_compressed(
            path,
            labels=self.labels.astype(np.int16),
            protected_features=self.protected_features.astype(bool),
            explicit_protected_features=self.explicit_protected_features.astype(bool),
            palette_rgb=self.palette_rgb.astype(np.uint8),
            palette_costs=self.palette_costs.astype(np.float16),
            contract_version=np.array(3, dtype=np.int16),
        )

    @classmethod
    def load(cls, path: Path, *, with_costs: bool = True) -> "SegmentationBundle":
        # ``with_costs=False`` skips the per-pixel/per-palette cost volume, which is by
        # far the largest member. ``np.load`` is lazy per array, so never touching
        # ``palette_costs`` means it is never decompressed — the editor uses this for
        # label edits that leave the cost field unchanged. ``palette_costs`` is then an
        # empty placeholder and cost-shape validation must be skipped.
        with np.load(path, allow_pickle=False) as data:
            version = int(data["contract_version"])
            if version != 3:
                raise ValueError(f"unsupported segmentation contract version {version}")
            return cls(
                labels=data["labels"].astype(np.int16),
                protected_features=data["protected_features"].astype(bool),
                explicit_protected_features=data["explicit_protected_features"].astype(bool),
                palette_rgb=data["palette_rgb"].astype(np.uint8),
                palette_costs=(data["palette_costs"].astype(np.float32) if with_costs
                               else np.empty((0,), dtype=np.float32)),
            )

    def validate(self, *, check_costs: bool = True) -> None:
        if self.labels.shape != self.protected_features.shape:
            raise ValueError("segmentation labels and protected-feature dimensions differ")
        if self.labels.shape != self.explicit_protected_features.shape:
            raise ValueError("segmentation labels and explicit-protection dimensions differ")
        if self.labels.ndim != 2 or self.palette_rgb.ndim != 2 or self.palette_rgb.shape[1] != 3:
            raise ValueError("invalid segmentation bundle dimensions")
        if self.labels.size and (self.labels.min() < 0 or self.labels.max() >= len(self.palette_rgb)):
            raise ValueError("segmentation contains a label outside its palette")
        if check_costs and self.palette_costs.shape != (
            *self.labels.shape, len(self.palette_rgb)
        ):
            raise ValueError("segmentation palette-cost dimensions differ")


@dataclass
class TraceSettings:
    width_mm: float | None = None
    height_mm: float | None = None
    overlap: float = 0.0
    solid_underlayers: bool = False
    remove_colour: str | None = None
    layer_order: list[str] | None = None
    corner_threshold: float = 60.0
    curve_optimization: bool = True
    curve_fit_tolerance: float = 1.5
    # Geometry classification is deliberately independent from curve fitting.
    # A raster staircase may be this far from its best-fit line and still be
    # represented by one exact SVG line.
    straight_line_max_deviation: float = 1.6
    minimum_straight_length: float = 4.0
    max_deviation: float | None = None
    corner_analysis_span: float = 8.0
    cleanup: bool = False
    cleanup_action: str = "remove"
    min_detail_width: float = 0.0
    min_region_area: float = 0.0
    remove_below_width: float = 0.0
    soft_path_nodes: int = 5_000
    soft_layer_nodes: int = 50_000
    soft_document_nodes: int = 200_000
