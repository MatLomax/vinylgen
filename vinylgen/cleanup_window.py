"""Clean one window of a raster for the live cleanup preview.

The preview runs the real cutter cleanup (``cleanup_labels``) on a crop: the
visible window plus a hidden margin of the raster a Cleanup run starts from, under
the whole raster's physical size, with the removed colour left out and explicit
protection honoured exactly as ``apply_cleanup`` honours them. Whether a region is
undersized depends on its WHOLE four-connected area and width, which a crop would
misjudge for every region it cuts, so those are measured once on the whole raster
(``CleanupSource.regions``) and the crop reads its regions' decisions from them.

What still differs from a full run, by construction:

- Remove: a group of touching undersized regions is recoloured to the touching
  neighbour it shares the most edge with. A group that reaches the crop edge is split
  there and counts only the edge it shares inside the crop, so its colour can differ;
  the margin keeps that outside the visible window unless the group spans it.
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
    change_classes,
    cleanup_labels,
    measure_colour_regions,
    pixel_scale,
    remove_then_widen_stages,
    resolve_physical_size,
)

# Hidden context cleaned around the visible window, in raster px: a removed group
# or a widening that reaches the crop edge is judged on the crop alone, and this
# keeps most of that outside the part the user sees. Widening needs wide context:
# a region's widening colour and the order of widenings depend on neighbours well
# beyond the window.
CLEANUP_PREVIEW_MARGIN_PX = 256
# The largest visible window (raster px) a cleanup preview is drawn for.
CLEANUP_PREVIEW_MAX_AREA_PX = 600 * 600

Checkpoint = Callable[[], None]


class PreviewCancelled(Exception):
    """A newer request superseded this preview before it finished."""


@dataclass(frozen=True)
class Window:
    """An axis-aligned pixel rectangle of the raster."""

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
                  margin: int) -> Window:
    """``window`` grown by ``margin`` px on every side, clipped to the image."""
    left = max(0, window.x - margin)
    top = max(0, window.y - margin)
    right = min(image_width, window.right + margin)
    bottom = min(image_height, window.bottom + margin)
    return Window(left, top, right - left, bottom - top)


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
    _current: tuple[str, np.ndarray] | None = field(default=None, repr=False)

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

    def release_current(self) -> None:
        """Drop the shown raster's labels (the revision shows this raster itself)."""
        self._current = None

    def current_labels(self, artifact_id: str, path: Path) -> np.ndarray:
        """The labels of the raster shown for a revision (segmentation ``artifact_id``),
        held with this raster so a preview compares against them without reloading.
        Only the labels member is read."""
        if self._current is None or self._current[0] != artifact_id:
            self._current = None
            with np.load(path, allow_pickle=False) as data:
                labels = data["labels"].astype(np.int16, copy=False)
            if labels.shape != self.labels.shape:
                raise ValueError("the shown raster and the uncleaned raster differ in size")
            self._current = (artifact_id, labels)
        return self._current[1]


@dataclass(frozen=True)
class CleanedWindow:
    """The cleanup of one window: the uncleaned labels of ``window``, its labels
    after the removal stage and its final cleaned labels, the net change from the
    uncleaned raster split by the stage that made it (``change_classes``), and the
    final cleaned labels of the whole ``crop``."""

    window: Window
    crop: Window
    original: np.ndarray
    after_removal: np.ndarray
    labels: np.ndarray
    removed: np.ndarray
    widened: np.ndarray
    cleaned_crop: np.ndarray


def will_change(original: np.ndarray, current: np.ndarray, after_removal: np.ndarray,
                final: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """What a Cleanup run would change on the raster shown now, per pixel.

    ``original`` is the uncleaned label, ``current`` the one shown (the active
    cleanup's result, or ``original`` without one), ``after_removal`` and ``final``
    the labels the run would reach after its removal stage and at its end. A pixel
    changes when ``final`` differs from ``current``; it then *returns* when
    ``final`` is its uncleaned label (the active cleanup changed it and the run would
    not), else it is *widened* when the widening set ``final`` (it differs from
    ``after_removal``), else *removed*. Returns ``(removed, widened, returned)``.
    """
    changes = final != current
    returned = changes & (final == original)
    widened = changes & ~returned & (final != after_removal)
    return changes & ~returned & ~widened, widened, returned


def clean_window(source: CleanupSource, window: Window, palette: list[dict],
                 settings: CleanupSettings, omitted: int | None,
                 checkpoint: Checkpoint | None = None) -> CleanedWindow:
    """Clean ``window`` (plus the hidden margin) as a Cleanup run cleans the raster.

    ``omitted`` is the removed colour's palette index, excluded from cleanup as
    ``apply_cleanup`` excludes it.
    """
    check = checkpoint or (lambda: None)
    crop = margin_window(window, source.width, source.height, CLEANUP_PREVIEW_MARGIN_PX)
    rows = slice(crop.y, crop.bottom)
    columns = slice(crop.x, crop.right)
    original = np.ascontiguousarray(source.labels[rows, columns])
    inner = (slice(window.y - crop.y, window.bottom - crop.y),
             slice(window.x - crop.x, window.right - crop.x))
    if not settings.cleanup:
        same = original[inner].copy()
        unchanged = np.zeros(same.shape, dtype=bool)
        return CleanedWindow(window, crop, same, same, same, unchanged, unchanged.copy(),
                             original)
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
    removed_window, widened_window = change_classes(original[inner], removed[inner], cleaned[inner])
    return CleanedWindow(window, crop, original[inner].copy(), removed[inner].copy(),
                         cleaned[inner].copy(), removed_window, widened_window, cleaned)
