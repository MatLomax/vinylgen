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
class CleanupSettings:
    """The cleanup stage's settings: the physical output size, the colour left out
    of the cut, and the cutter cleanup applied to the labels.

    One physical size governs both the cleanup thresholds and the traced SVG, so the
    trace reads ``width_mm``, ``height_mm`` and ``remove_colour`` from here. Without
    a size, every length is in pixels of the flattened raster.
    """

    width_mm: float | None = None
    height_mm: float | None = None
    remove_colour: str | None = None
    cleanup: bool = False
    cleanup_action: str = "remove"
    min_detail_width: float = 0.0
    min_region_area: float = 0.0
    remove_below_width: float = 0.0


# Trace lengths are in one of two units, named by ``TraceSettings.length_unit``:
# ``per_mille`` (thousandths of the traced image's longer side, what settings are
# stored and edited in) or ``px`` (pixels of the traced image, what the fitter
# works in and what every setting recorded before units existed means).
# ``vinylgen.trace_lengths`` converts between them (docs/svg-tracing-spec.md
# "Trace length units").
LENGTH_UNIT_PER_MILLE = "per_mille"
LENGTH_UNIT_PX = "px"
LENGTH_UNITS = (LENGTH_UNIT_PER_MILLE, LENGTH_UNIT_PX)
# Every trace length. Each has exactly one default rule, from one of the three
# tables below; a new length is added here and to one of them.
TRACE_LENGTH_FIELDS = (
    "curve_fit_tolerance", "straight_line_max_deviation", "minimum_straight_length",
    "max_deviation", "side_smoothness", "corner_sharpening_allowance", "corner_analysis_span",
    "curve_feature_scale")
# Lengths with a fixed default, filled at construction, in thousandths (they
# reproduce the pixel defaults on a 2000 px image) and in pixels (the defaults
# every setting recorded before units existed was traced with).
PER_MILLE_DEFAULTS = {
    "curve_fit_tolerance": 0.75, "straight_line_max_deviation": 0.8,
    "minimum_straight_length": 2.0, "corner_analysis_span": 4.0,
    "curve_feature_scale": 2.0}
PX_DEFAULTS = {
    "curve_fit_tolerance": 1.5, "straight_line_max_deviation": 1.6,
    "minimum_straight_length": 4.0, "corner_analysis_span": 8.0,
    "curve_feature_scale": 4.0}
# Lengths that default to another length's value, filled at construction after the
# fixed defaults and recorded as that value. One that follows an automatic length
# (still None at construction) is filled when the lengths are resolved, after it.
FOLLOWING_LENGTHS = {"side_smoothness": "straight_line_max_deviation",
                     "corner_sharpening_allowance": "max_deviation"}
# Trace settings renamed since they were first recorded: stored records (web drafts
# and revisions, CLI configs, trace.json) are read under the new name.
RENAMED_TRACE_FIELDS = {"corner_straightness": "side_smoothness"}
# Lengths whose None means "automatic": kept None in the settings and filled with
# this default (thousandths, pixels) when the lengths are resolved.
AUTOMATIC_LENGTHS = {"max_deviation": (1.0, 2.0)}
DEFAULT_MAX_DEVIATION_PER_MILLE = AUTOMATIC_LENGTHS["max_deviation"][0]
# The range a user may set the curve feature scale to, in thousandths of the
# longer side (an eighth to eight times the default). It bounds input (the web
# form and API, the CLI flag), not records: a pixel-era record converted on a very
# large or small image may fall outside it and still traces as recorded.
CURVE_FEATURE_SCALE_RANGE = (0.25, 16.0)


def upgrade_trace_record(values: dict) -> dict:
    """A stored trace settings record with renamed fields under their current
    names (``RENAMED_TRACE_FIELDS``). A record carrying both keeps the current one."""
    upgraded = dict(values)
    for old, new in RENAMED_TRACE_FIELDS.items():
        if old in upgraded:
            value = upgraded.pop(old)
            upgraded.setdefault(new, value)
    return upgraded


@dataclass
class TraceSettings:
    overlap: float = 0.0
    solid_underlayers: bool = False
    layer_order: list[str] | None = None
    corner_threshold: float = 60.0
    # The unit of the trace lengths below (``TRACE_LENGTH_FIELDS``, ``LENGTH_UNITS``).
    # A length left None takes that unit's default at construction, or the length
    # it follows (``FOLLOWING_LENGTHS``); an automatic length (``AUTOMATIC_LENGTHS``,
    # ``max_deviation``) stays None until the lengths are resolved. So
    # ``TraceSettings(length_unit="px")`` is the pixel-era default record.
    length_unit: str = LENGTH_UNIT_PER_MILLE
    curve_fit_tolerance: float | None = None
    # Geometry classification is deliberately independent from curve fitting.
    # A raster staircase may be this far from its best-fit line and still be
    # represented by one exact SVG line.
    straight_line_max_deviation: float | None = None
    minimum_straight_length: float | None = None
    max_deviation: float | None = None
    corner_analysis_span: float | None = None
    # Side smoothness: how far the runs either side of a candidate corner may
    # stray from their best-fit circular arcs (straight lines included) and still
    # confirm a hard corner. None takes the straight-line deviation, resolved once,
    # at construction (``FOLLOWING_LENGTHS``).
    side_smoothness: float | None = None
    # How far a sharpened corner (the side arcs' intersection) may lie from its
    # sample on the drawn edge. None follows ``max_deviation``.
    corner_sharpening_allowance: float | None = None
    # Node placement (docs/svg-tracing-spec.md "Node placement"). Straight runs
    # snap to exact lines: "off", "axis" (horizontal and vertical) or
    # "axis_diagonal" (also 45 degrees), when within ``snap_angle`` degrees.
    straight_line_snap: str = "axis"
    snap_angle: float = 2.0
    # "prefer_extremes" (fewest nodes, extremes preferred in ties),
    # "at_extremes" (every axis extreme is a node) or "fewest" (no preference).
    node_placement: str = "prefer_extremes"
    nodes_at_bends: bool = True
    # Arc length the curve is smoothed over before candidate nodes are found.
    curve_feature_scale: float | None = None
    soft_path_nodes: int = 5_000
    soft_layer_nodes: int = 50_000
    soft_document_nodes: int = 200_000

    def __post_init__(self) -> None:
        if self.length_unit not in LENGTH_UNITS:
            raise ValueError(f"length_unit must be per_mille or px, not {self.length_unit!r}")
        defaults = PER_MILLE_DEFAULTS if self.length_unit == LENGTH_UNIT_PER_MILLE else PX_DEFAULTS
        for name, value in defaults.items():
            if getattr(self, name) is None:
                setattr(self, name, value)
        for name, source in FOLLOWING_LENGTHS.items():
            if getattr(self, name) is None:
                setattr(self, name, getattr(self, source))
        if self.straight_line_snap not in ("off", "axis", "axis_diagonal"):
            raise ValueError(f"straight_line_snap must be off, axis or axis_diagonal, "
                             f"not {self.straight_line_snap!r}")
        if self.node_placement not in ("prefer_extremes", "at_extremes", "fewest"):
            raise ValueError(f"node_placement must be prefer_extremes, at_extremes or fewest, "
                             f"not {self.node_placement!r}")
        if not 0.5 <= float(self.snap_angle) <= 10.0:
            raise ValueError("snap_angle must be between 0.5 and 10 degrees")
        if not float(self.curve_feature_scale) > 0.0:
            raise ValueError("curve_feature_scale must be positive")
