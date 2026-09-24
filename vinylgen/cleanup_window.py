"""Clean one window of a raster for the live cleanup preview.

The preview runs the real cutter cleanup (``cleanup_labels``) on a crop: the
visible window plus a hidden margin of the raster a Cleanup run starts from, under
the whole raster's physical size, with the removed colour left out and explicit
protection honoured exactly as ``apply_cleanup`` honours them. Whether a region is
undersized depends on its WHOLE four-connected area and width, which a crop would
misjudge for every region it cuts, so those are measured once on the whole raster
(``CleanupSource.regions``) and the crop reads its regions' decisions from them.

What still differs from a full run, by construction:

- Remove: a group of touching undersized regions is recoloured to its most similar
  touching neighbour. A group that reaches the crop edge is split there and sees
  only its neighbours inside the crop, so its colour can differ; the margin keeps
  that outside the visible window unless the group spans it.
- Widen grows regions one after another on the crop: the colour a region grows
  into, its thin ridges and the residual checks on the neighbour it thins are
  judged inside the crop, in the crop's order, and a widening decided differently
  near the crop edge changes what later ones see. The 256 px margin keeps most of
  that out of view: in testing on five corpus subjects (random 600 x 600 windows)
  widen matched a full run in 33 of 40 windows and remove-then-widen in 28 of 40,
  against 10 and 9 of 40 with a 64 px margin. It is still a crop, so a match is
  not guaranteed. The widen half of ``remove_then_widen`` judges regions by their
  part inside the crop, since the removal changed them.
- The automatic-protection audit numbers a Cleanup run records are not computed,
  and nothing is saved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from vinylgen.contracts import CleanupSettings
from vinylgen.cutter_cleanup import (
    RegionMeasurements,
    cleanup_labels,
    measure_colour_regions,
    pixel_scale,
    remove_then_widen_stages,
    resolve_physical_size,
)
from vinylgen.trace_window import Window, margin_window

# Hidden context cleaned around the visible window, in raster px: a removed group
# or a widening that reaches the crop edge is judged on the crop alone, and this
# keeps most of that outside the part the user sees. Widening needs far more
# context than the trace preview's boundaries (a region's widening colour and the
# order of widenings depend on neighbours well beyond the window).
CLEANUP_PREVIEW_MARGIN_PX = 256
# The largest visible window (raster px) a cleanup preview is drawn for.
CLEANUP_PREVIEW_MAX_AREA_PX = 600 * 600

Checkpoint = Callable[[], None]


@dataclass
class CleanupSource:
    """The uncleaned raster a Cleanup run starts from, loaded once per revision,
    with its whole-raster region measurements built on first use.

    The measurements are built one colour at a time and each finished colour is
    kept, so a preview cancelled part-way leaves the next one less to do. Within a
    colour, a cancellation is honoured after its regions are labelled, before the
    distance transform; the transform (one exact feature-transform call) cannot be
    split without changing the distances, so it always runs to its end.
    """

    labels: np.ndarray
    explicit: np.ndarray
    palette_rgb: np.ndarray
    _ids: np.ndarray | None = field(default=None, repr=False)
    _stats: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = field(default_factory=list, repr=False)
    _regions: RegionMeasurements | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path, checkpoint: Checkpoint | None = None) -> "CleanupSource":
        """Load the labels, explicit protection and palette of a segmentation; the
        cost field is never read."""
        with np.load(path, allow_pickle=False) as data:
            version = int(data["contract_version"])
            if version != 3:
                raise ValueError(f"unsupported segmentation contract version {version}")
            labels = data["labels"].astype(np.int16, copy=False)
            if checkpoint is not None:
                checkpoint()
            explicit = data["explicit_protected_features"].astype(bool, copy=False)
            palette_rgb = data["palette_rgb"].astype(np.uint8, copy=False)
        if labels.ndim != 2 or explicit.shape != labels.shape:
            raise ValueError("segmentation labels and explicit-protection dimensions differ")
        if labels.size and (labels.min() < 0 or labels.max() >= len(palette_rgb)):
            raise ValueError("segmentation contains a label outside its palette")
        return cls(labels=labels, explicit=explicit, palette_rgb=palette_rgb)

    @property
    def width(self) -> int:
        return int(self.labels.shape[1])

    @property
    def height(self) -> int:
        return int(self.labels.shape[0])

    def regions(self, checkpoint: Checkpoint | None = None) -> RegionMeasurements:
        """The whole-raster measurements of every colour's regions. ``checkpoint``
        runs before each colour and may raise to stop; finished colours are kept."""
        if self._regions is not None:
            return self._regions
        if self._ids is None:
            self._ids = np.zeros(self.labels.shape, dtype=np.int32)
        for colour in range(len(self._stats), len(self.palette_rgb)):
            if checkpoint is not None:
                checkpoint()
            bounds, components, count, area, width, held = measure_colour_regions(
                self.labels, colour, self.explicit, checkpoint)
            offset = sum(len(stats[0]) for stats in self._stats)
            on = components > 0
            # Renumbered in place, so no second per-pixel copy is made.
            components += offset - 1
            np.copyto(self._ids[bounds], components, where=on)
            del components, on
            self._stats.append((area, width, held))
        self._regions = RegionMeasurements(
            ids=self._ids,
            area=np.concatenate([stats[0] for stats in self._stats]),
            width=np.concatenate([stats[1] for stats in self._stats]),
            explicit=np.concatenate([stats[2] for stats in self._stats]))
        self._stats = []
        return self._regions

    @property
    def measured(self) -> bool:
        return self._regions is not None


@dataclass(frozen=True)
class CleanedWindow:
    """The cleanup of one window: the cleaned labels of ``window`` and the net
    change from the uncleaned raster, split by the stage that made it. A pixel
    that ends as it started is in neither mask."""

    window: Window
    crop: Window
    labels: np.ndarray
    removed: np.ndarray
    widened: np.ndarray


def clean_window(source: CleanupSource, window: Window, palette: list[dict],
                 settings: CleanupSettings, omitted: int | None,
                 checkpoint: Checkpoint | None = None) -> CleanedWindow:
    """Clean ``window`` (plus the hidden margin) as a Cleanup run cleans the raster.

    ``omitted`` is the removed colour's palette index, excluded from cleanup as
    ``apply_cleanup`` excludes it. A pixel whose final label differs from its
    uncleaned one is ``widened`` when the widening set that label, else ``removed``;
    a pixel removed and then widened back to its own colour is neither.
    """
    check = checkpoint or (lambda: None)
    crop = margin_window(window, source.width, source.height, CLEANUP_PREVIEW_MARGIN_PX)
    rows = slice(crop.y, crop.bottom)
    columns = slice(crop.x, crop.right)
    original = np.ascontiguousarray(source.labels[rows, columns])
    inner = (slice(window.y - crop.y, window.bottom - crop.y),
             slice(window.x - crop.x, window.right - crop.x))
    unchanged = np.zeros((window.height, window.width), dtype=bool)
    if not settings.cleanup:
        return CleanedWindow(window, crop, original[inner].copy(), unchanged, unchanged.copy())
    regions = source.regions(checkpoint).crop(rows, columns)
    check()
    resolved = resolve_physical_size(settings, source.width, source.height)
    scale = pixel_scale(resolved, source.width)
    included = [index for index in range(len(palette)) if index != omitted]
    explicit = np.ascontiguousarray(source.explicit[rows, columns])
    if resolved.cleanup_action == "remove_then_widen":
        remove_settings, widen_settings = remove_then_widen_stages(resolved)
        removed, _ = cleanup_labels(original, included, palette, remove_settings, scale,
                                    explicit=explicit, regions=regions,
                                    checkpoint=check)
        check()
        # The removal changed the raster, so the widening measures the crop itself.
        cleaned, _ = cleanup_labels(removed, included, palette, widen_settings, scale,
                                    explicit=explicit, checkpoint=check)
    elif resolved.cleanup_action == "remove":
        removed, _ = cleanup_labels(original, included, palette, resolved, scale,
                                    explicit=explicit, regions=regions,
                                    checkpoint=check)
        cleaned = removed
    else:
        removed = original
        cleaned, _ = cleanup_labels(original, included, palette, resolved, scale,
                                    explicit=explicit, regions=regions,
                                    checkpoint=check)
    changed = (cleaned != original)[inner]
    widened = changed & (cleaned != removed)[inner]
    return CleanedWindow(window, crop, cleaned[inner].copy(), changed & ~widened, widened)
