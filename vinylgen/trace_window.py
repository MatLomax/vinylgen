"""Trace one window of a segmentation for the live trace preview.

The preview reuses the real vector stage on a crop: the shared-boundary graph of
the window plus a hidden margin, the canonical chain fitter with the requested
trace settings, the same overlap/solid-underlayer construction and the same SVG
layer writer. Only three things differ from a full trace, all by construction:

- Boundaries crossing the crop edge are split there, so their fit can differ near
  the edge. The margin keeps most of that outside the visible window.
- Layer order is inferred from the whole image (it is a whole-image decision), but
  underlayer adjacency and overlap buffers only see the crop.
- Validation and protected-feature recovery are skipped, and nothing is saved.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import numpy as np
from shapely.geometry import box

from vinylgen.boundary_graph import SharedBoundaryTrack, shared_boundary_polygons
from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import SharedChainFitter, shared_boundary_components
from vinylgen.svg_writer import SVG, add_layer
from vinylgen.tracing import (
    DEFAULT_MAX_DEVIATION_PX,
    infer_order,
    overlap_layer_components,
    partition_watertight_fault,
    underlayer_geometry,
)

# Hidden context traced around the visible window, in traced-image px. Boundaries
# are split at the crop edge; this keeps that split, and the fit differences it
# causes, outside the part the user sees.
PREVIEW_MARGIN_PX = 64
# The largest visible window (traced-image px) a preview is drawn for. A warm dense
# window of this size, margin included, traces in well under a second.
PREVIEW_MAX_AREA_PX = 600 * 600
# Fit caches kept per cached window: one per recent combination of fitting settings,
# so stepping back to a recent setting is instant without the cache growing with
# every value a field passes through.
FITTERS_PER_WINDOW = 4

Checkpoint = Callable[[], None]


class PreviewCancelled(Exception):
    """A newer request superseded this preview before it finished."""


@dataclass(frozen=True)
class Window:
    """An axis-aligned pixel rectangle of the traced image."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


def clamp_window(x: int, y: int, width: int, height: int,
                 image_width: int, image_height: int) -> Window:
    """The part of a requested window that lies on the image.

    Raises ``ValueError`` when the window and the image do not overlap.
    """
    left = min(max(int(x), 0), image_width)
    top = min(max(int(y), 0), image_height)
    right = min(max(int(x) + int(width), 0), image_width)
    bottom = min(max(int(y) + int(height), 0), image_height)
    if right <= left or bottom <= top:
        raise ValueError("the preview window lies outside the image")
    return Window(left, top, right - left, bottom - top)


def margin_window(window: Window, image_width: int, image_height: int,
                  margin: int = PREVIEW_MARGIN_PX) -> Window:
    """``window`` grown by ``margin`` px on every side, clipped to the image."""
    left = max(0, window.x - margin)
    top = max(0, window.y - margin)
    right = min(image_width, window.right + margin)
    bottom = min(image_height, window.bottom + margin)
    return Window(left, top, right - left, bottom - top)


def preview_trace_settings(values: dict) -> TraceSettings:
    """The trace settings a preview fits with, normalized exactly as a trace run does."""
    settings = TraceSettings(**values)
    if settings.max_deviation is None:
        settings = replace(settings, max_deviation=DEFAULT_MAX_DEVIATION_PX)
    return settings


def preview_variant(settings: TraceSettings) -> str:
    """The SVG variant a trace with ``settings`` would show: the overlap file only
    differs from the edge-to-edge one with solid underlayers or a positive overlap."""
    return "overlap" if settings.solid_underlayers or settings.overlap > 0 else "edge"


@dataclass
class PreviewSegmentation:
    """The whole-image inputs of a preview, loaded once and cropped per window.

    The palette costs stay in their stored float16 form; a window's crop is widened
    to float32, which yields exactly the values a full load would, at half the
    resident memory.
    """

    labels: np.ndarray
    protected_features: np.ndarray
    palette_rgb: np.ndarray
    palette_costs: np.ndarray
    _orders: dict[tuple[int, ...], tuple[list[int], list[str]]] = field(
        default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path, checkpoint: Checkpoint | None = None) -> "PreviewSegmentation":
        """Load a segmentation; ``checkpoint`` runs before the (large) cost field is
        decompressed and may raise to abandon the load."""
        with np.load(path, allow_pickle=False) as data:
            version = int(data["contract_version"])
            if version != 3:
                raise ValueError(f"unsupported segmentation contract version {version}")
            labels = data["labels"].astype(np.int16, copy=False)
            protected = data["protected_features"].astype(bool, copy=False)
            palette_rgb = data["palette_rgb"].astype(np.uint8, copy=False)
            if checkpoint is not None:
                checkpoint()
            result = cls(labels=labels, protected_features=protected,
                         palette_rgb=palette_rgb, palette_costs=data["palette_costs"])
        if result.labels.ndim != 2 or result.protected_features.shape != result.labels.shape:
            raise ValueError("segmentation labels and protected-feature dimensions differ")
        if result.palette_costs.shape != (*result.labels.shape, len(result.palette_rgb)):
            raise ValueError("segmentation palette-cost dimensions differ")
        return result

    @property
    def width(self) -> int:
        return int(self.labels.shape[1])

    @property
    def height(self) -> int:
        return int(self.labels.shape[0])

    def layer_order(self, included: list[int], palette: list[dict]) -> list[int]:
        """The inferred bottom-to-top order of the whole image, computed once per colour set."""
        key = tuple(included)
        if key not in self._orders:
            self._orders[key] = infer_order(self.labels, included, palette)
        return list(self._orders[key][0])


@dataclass
class WindowBoundaries:
    """The shared-boundary graph of one crop, in crop-local px, and its fit caches."""

    crop: Window
    partition: dict[int, object]
    track: SharedBoundaryTrack
    fitters: OrderedDict = field(default_factory=OrderedDict)

    def fitter_for(self, settings: TraceSettings, overlap_px: float) -> SharedChainFitter:
        """The fit cache for these fitting settings (and overlap, whose buffered rings
        it also caches), keeping only the ``FITTERS_PER_WINDOW`` most recent."""
        key = (settings.curve_fit_tolerance, settings.straight_line_max_deviation,
               settings.minimum_straight_length, settings.corner_analysis_span,
               settings.corner_threshold, settings.corner_straightness,
               settings.max_deviation, float(overlap_px))
        if key in self.fitters:
            self.fitters.move_to_end(key)
        else:
            self.fitters[key] = SharedChainFitter()
            while len(self.fitters) > FITTERS_PER_WINDOW:
                self.fitters.popitem(last=False)
        return self.fitters[key]


def window_boundaries(segmentation: PreviewSegmentation, included: list[int],
                      crop: Window, checkpoint: Checkpoint | None = None) -> WindowBoundaries:
    """Build the exact shared-boundary partition of ``crop`` (no simplification)."""
    rows = slice(crop.y, crop.bottom)
    columns = slice(crop.x, crop.right)
    labels = np.ascontiguousarray(segmentation.labels[rows, columns])
    costs = segmentation.palette_costs[rows, columns].astype(np.float32)
    protected = np.ascontiguousarray(segmentation.protected_features[rows, columns])
    partition, track, topology = shared_boundary_polygons(
        labels, included, costs, protected_features=protected, checkpoint=checkpoint)
    fault = partition_watertight_fault(topology, float(crop.width * crop.height))
    if fault is not None:
        raise RuntimeError(fault)
    return WindowBoundaries(crop, partition, track)


def render_window_svg(boundaries: WindowBoundaries, window: Window, order: list[int],
                      palette: list[dict], settings: TraceSettings, overlap_px: float,
                      checkpoint: Checkpoint | None = None) -> str:
    """Fit the crop's chains and write the variant ``settings`` selects as SVG.

    The document's ``viewBox`` is ``window`` in traced-image px, so drawn at the
    window's size and position over the image it registers exactly; the hidden
    margin around it falls outside the view box. ``checkpoint`` runs before each
    chain fit and each layer and may raise to abandon the render.
    """
    check = checkpoint or (lambda: None)
    crop = boundaries.crop
    included = sorted(order)
    fitter = boundaries.fitter_for(settings, overlap_px)
    components, chain_fits, _ = shared_boundary_components(
        boundaries.track, included, settings, fitter=fitter, checkpoint=checkpoint)
    check()
    variant = preview_variant(settings)
    layer_geometry = (underlayer_geometry(
        boundaries.partition, order, solid_underlayers=settings.solid_underlayers,
        overlap_px=overlap_px, canvas=box(0, 0, crop.width, crop.height))
        if variant == "overlap" else {})
    root = ET.Element(f"{{{SVG}}}svg", {
        "version": "1.1",
        "viewBox": f"{window.x} {window.y} {window.width} {window.height}",
        "preserveAspectRatio": "none"})
    group = ET.SubElement(root, f"{{{SVG}}}g", {"transform": f"translate({crop.x} {crop.y})"})
    for position, index in enumerate(order):
        check()
        edge_components = components.get(index, [])
        if variant == "overlap":
            layer_components, _ = overlap_layer_components(
                position, order, edge_components, layer_geometry, boundaries.track,
                chain_fits, settings, overlap_px, fitter)
        else:
            layer_components = edge_components
        add_layer(group, palette[index], layer_components, "", "preview", variant, 0.0)
    return ET.tostring(root, encoding="unicode")
