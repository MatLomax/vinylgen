"""Touch-ups: area and pencil edits made on the cleaned raster, replayed by the next
Cleanup run.

While a revision's cleanup is current, the Edit stage's area recolours
(``reassign_region``) and pencil strokes (``paint_pixels``) change the cleaned raster
(``editor.apply_edits``). Each is recorded as a touch-up: an entry in the revision's
``touch_ups`` settings list, whose pixel data lives in the revision's ``touch-ups``
artifact (``touch-ups.npz``), member names prefixed by the entry's list index:

- a pencil stroke: ``<i>_pixels``, the painted ``(x, y)`` pixels (int32, N x 2);
- an area recolour: the exact four-connected region it recoloured, as its bounding
  box ``<i>_origin`` (x, y) and ``<i>_size`` (width, height), both int64, and
  ``<i>_mask``, the box's pixels row-major as ``numpy.packbits`` bits.

An entry states the tool, the painted palette index (``target_label``), and for an
area recolour the click point and the region's colour before it (``source_label``);
a label edit's entry also names its operation's index in the batch (``operation``).
An area or pencil edit made while the cleanup is stale applies to the raster from
before cleanup and is not replayed, but is recorded the same way with ``replays``
false: its pixels are its footprint, which an older touch-up never paints over.
A touch-up painted in the colour the current cleanup removes is an erase
(``erase``): a run paints it in the colour that run removes, whatever it is, and
skips it when the run removes none. Any other touch-up paints its own colour.

A Cleanup run cleans the raster from before any cleanup, then replays the touch-ups
made since the previous cleanup (``pending_touch_ups``), oldest first
(``replay_touch_ups``): a pencil stroke always, an area recolour only while the
region under its click point is exactly the recorded one, in the same colour; and
neither over the footprint of a later edit (newer edits win). Colours are followed
through later merges by identity, not by palette index, and across a re-flatten by
exact hex; a colour merged away or not in the new palette leaves a touch-up nothing
to paint, and a re-flatten at a different size drops every touch-up. The run
records the touch-ups it replayed in the same form, so the run after it replays
them again, and keeps those that do not apply now but may again (an erase while the
run removes no colour, an area recolour whose area has changed: ``KEPT_REASONS``),
marked ``waiting``; the rest it skips are dropped.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from .store import Store

TOUCH_UPS_KIND = "touch-ups"
TOUCH_UPS_FILE = "touch-ups.npz"

# Why a touch-up was not replayed (``replay_touch_ups``), checked in this order: a
# re-flatten changed the raster's size; a later edit covers it (all of a pencil
# stroke, any of an area); it erases and the run removes no colour; the colour it
# paints is not in the palette; the colour of the area it recoloured is not; the
# area has changed since it was edited.
RESIZED = "resized"
SUPERSEDED = "superseded"
ERASE_UNREMOVED = "erase-unremoved"
COLOUR_GONE = "colour-gone"
AREA_COLOUR_GONE = "area-colour-gone"
AREA_CHANGED = "area-changed"
# The reasons a run keeps a touch-up for later runs, which replay it once it applies
# again; a touch-up skipped for any other reason is dropped for good (a later edit
# won, or its colour or the raster it was made on is gone).
KEPT_REASONS = {ERASE_UNREMOVED, AREA_CHANGED}

# Four-way connectivity: the authoritative rule for what one physical vinyl region is.
_CROSS = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


def region_arrays(region: np.ndarray) -> dict[str, np.ndarray]:
    """The arrays that record the region ``region`` (a full-raster mask) exactly."""
    rows = np.flatnonzero(region.any(axis=1))
    columns = np.flatnonzero(region.any(axis=0))
    top, bottom = int(rows[0]), int(rows[-1]) + 1
    left, right = int(columns[0]), int(columns[-1]) + 1
    box = region[top:bottom, left:right]
    return {"origin": np.array([left, top], dtype=np.int64),
            "size": np.array([right - left, bottom - top], dtype=np.int64),
            "mask": np.packbits(box, axis=None)}


def pixel_arrays(pixels: Any) -> dict[str, np.ndarray]:
    """The arrays that record a pencil stroke's ``(x, y)`` pixels."""
    return {"pixels": np.asarray(pixels, dtype=np.int32).reshape(-1, 2)}


def touch_ups_bytes(arrays: list[dict[str, np.ndarray]]) -> bytes:
    """``touch-ups.npz`` for the touch-ups whose arrays are ``arrays``, in entry order."""
    stream = io.BytesIO()
    np.savez_compressed(stream, **{f"{index}_{name}": value
                                   for index, members in enumerate(arrays)
                                   for name, value in members.items()})
    return stream.getvalue()


def load_arrays(path: Path, member: int) -> dict[str, np.ndarray]:
    """The arrays of entry ``member`` in the ``touch-ups.npz`` at ``path``."""
    prefix = f"{member}_"
    with np.load(path, allow_pickle=False) as data:
        return {name[len(prefix):]: data[name] for name in data.files if name.startswith(prefix)}


def _region_box(arrays: dict[str, np.ndarray]) -> tuple[int, int, np.ndarray]:
    left, top = (int(value) for value in arrays["origin"])
    width, height = (int(value) for value in arrays["size"])
    box = np.unpackbits(arrays["mask"], count=width * height).reshape(height, width).astype(bool)
    return left, top, box


def _remap(label: int | None, source: int) -> int | None:
    """Palette index ``label`` after palette colour ``source`` is merged into another:
    ``None`` for the colour merged away, the indices above it shifted down one."""
    if label is None or label == source:
        return None
    return label - 1 if label > source else label


@dataclass
class PendingTouchUp:
    """An edit made since a cleanup, as the next Cleanup run sees it: a touch-up it
    replays (``replays``), or the pixel footprint of an edit made while that cleanup
    was stale, which an older touch-up never paints over (newer edits win). Colours
    are palette indices of the raster that run cleans (``None``: that colour is not in
    it any more); ``resized``: a re-flatten since changed the raster's size."""

    tool: str
    target_label: int | None
    source_label: int | None
    point: tuple[int, int] | None
    path: Path
    member: int
    replays: bool = True
    resized: bool = False
    erase: bool = False

    def spec(self) -> dict:
        """The edit as the cleanup worker receives it."""
        return {"tool": self.tool, "target_label": self.target_label,
                "source_label": self.source_label,
                "point": list(self.point) if self.point is not None else None,
                "path": str(self.path), "member": self.member,
                "replays": self.replays, "resized": self.resized, "erase": self.erase}


def _touch_ups_path(store: Store, revision: dict) -> Path:
    row = next((row for row in store.list_artifacts(revision["id"])
                if row["kind"] == TOUCH_UPS_KIND), None)
    if row is None:
        raise ValueError(f"revision {revision['id']} records touch-ups without their pixels")
    return store.artifact_path(row["id"])


def _pending(entry: dict, path: Path, member: int) -> PendingTouchUp:
    point = entry.get("point")
    return PendingTouchUp(tool=entry["tool"], target_label=entry["target_label"],
                          source_label=entry.get("source_label"),
                          point=(int(point[0]), int(point[1])) if point is not None else None,
                          path=path, member=member, replays=entry.get("replays", True),
                          erase=entry.get("erase", False))


def _labels_shape(path: Path) -> tuple[int, ...]:
    """The shape of a segmentation's labels, read from the member's header only."""
    with zipfile.ZipFile(path) as archive, archive.open("labels.npy") as member:
        version = np.lib.format.read_magic(member)
        read_header = (np.lib.format.read_array_header_1_0 if version == (1, 0)
                       else np.lib.format.read_array_header_2_0)
        shape, _, _ = read_header(member)
    return tuple(shape)


def _raster(store: Store, revision_id: str, *, before_cleanup: bool
            ) -> tuple[list[str], tuple[int, ...]] | None:
    """The palette hexes and raster shape a revision resolves, or ``None`` when it
    resolves no flattened raster."""
    artifacts = store.effective_artifacts(revision_id, before_cleanup=before_cleanup)
    if "palette" not in artifacts or "segmentation" not in artifacts:
        return None
    palette = json.loads(store.artifact_path(artifacts["palette"]["id"]).read_text())["palette"]
    return ([str(row["hex"]).casefold() for row in palette],
            _labels_shape(store.artifact_path(artifacts["segmentation"]["id"])))


def _reflattened(store: Store, chain: list[dict], at: int, pending: list[PendingTouchUp]) -> None:
    """Follow ``pending`` across the re-flatten ``chain[at]``: a colour survives as the
    new palette's one colour with its exact hex, and a change of size drops every
    touch-up (``resized``)."""
    before = None
    for revision in reversed(chain[:at]):
        before = _raster(store, revision["id"], before_cleanup=True)
        if before is not None:
            break
    after = _raster(store, chain[at]["id"], before_cleanup=False)
    if before is None or after is None or before[1] != after[1]:
        for touch_up in pending:
            touch_up.resized = True
        return
    old, new = before[0], after[0]

    def carried(label: int | None) -> int | None:
        if label is None or label >= len(old) or new.count(old[label]) != 1:
            return None
        return new.index(old[label])

    for touch_up in pending:
        touch_up.target_label = carried(touch_up.target_label)
        touch_up.source_label = carried(touch_up.source_label)


def pending_touch_ups(store: Store, revision_id: str) -> list[PendingTouchUp]:
    """The edits a Cleanup run on ``revision_id`` replays or keeps, oldest first: the
    area and pencil edits made since its nearest cleanup ancestor (a label edit's,
    and the touch-ups that cleanup itself replayed), with their colours followed
    through every later merge, and across a re-flatten by exact hex. None without a
    cleanup ancestor."""
    chain: list[dict] = []
    current: str | None = revision_id
    seen: set[str] = set()
    while current:
        if current in seen:
            raise ValueError("revision ancestry contains a cycle")
        seen.add(current)
        revision = store.get_revision(current)
        chain.append(revision)
        if revision["kind"] == "cleanup" and revision["settings"].get("cleanup") is not None:
            break
        current = revision["parent_revision_id"]
    else:
        return []
    chain.reverse()
    pending: list[PendingTouchUp] = []
    for at, revision in enumerate(chain):
        settings = revision["settings"]
        entries = settings.get("touch_ups") or []
        path = _touch_ups_path(store, revision) if entries else None
        if revision["kind"] == "palette":
            if pending:
                _reflattened(store, chain, at, pending)
            continue
        if revision["kind"] == "cleanup":
            pending.extend(_pending(entry, path, member) for member, entry in enumerate(entries))
            continue
        if revision["kind"] != "label-edit":
            continue
        by_operation: dict[int, list[tuple[int, dict]]] = {}
        for member, entry in enumerate(entries):
            by_operation.setdefault(int(entry["operation"]), []).append((member, entry))
        for index, operation in enumerate(settings.get("operations", ())):
            pending.extend(_pending(entry, path, member)
                           for member, entry in by_operation.get(index, ()))
            if operation.get("tool") == "merge_colour":
                source = int(operation["source_label"])
                for touch_up in pending:
                    touch_up.target_label = _remap(touch_up.target_label, source)
                    touch_up.source_label = _remap(touch_up.source_label, source)
    return pending


def _region_unchanged(labels: np.ndarray, left: int, top: int, box: np.ndarray,
                      source: int) -> bool:
    """Whether the four-connected region of colour ``source`` covering the recorded
    region (``box`` at ``left``, ``top`` on ``labels``' grid) is exactly it: every
    recorded pixel is ``source`` and no pixel sharing an edge with it is. The recorded
    region is one four-connected region, so this is equality with the region under
    its click point, judged within its box plus one pixel. The part of it outside
    ``labels`` (a preview window of the raster) is not judged."""
    height, width = box.shape
    rows, columns = labels.shape
    y0, y1 = max(0, top - 1), min(rows, top + height + 1)
    x0, x1 = max(0, left - 1), min(columns, left + width + 1)
    if y0 >= y1 or x0 >= x1:
        return True
    same = labels[y0:y1, x0:x1] == source
    region = np.zeros_like(same)
    ry0, rx0 = max(0, top - y0), max(0, left - x0)
    by0, bx0 = max(0, y0 - top), max(0, x0 - left)
    by1, bx1 = min(height, y1 - top), min(width, x1 - left)
    if by0 < by1 and bx0 < bx1:
        region[ry0:ry0 + by1 - by0, rx0:rx0 + bx1 - bx0] = box[by0:by1, bx0:bx1]
    if not same[region].all():
        return False
    border = ndimage.binary_dilation(region, structure=_CROSS) & ~region
    return not (same & border).any()


def _location(tool: str, arrays: dict[str, np.ndarray], point: list | None) -> list[int]:
    if tool == "reassign_region" and point is not None:
        return [int(point[0]), int(point[1])]
    pixels = arrays.get("pixels")
    if pixels is not None and len(pixels):
        return [int(pixels[0][0]), int(pixels[0][1])]
    return [0, 0]


def _footprint(arrays: dict[str, np.ndarray], origin: tuple[int, int],
               shape: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """The ``(ys, xs)`` pixels an edit recorded, on a window of the raster whose
    top-left pixel is ``origin`` and whose shape is ``shape``, clipped to it."""
    rows, columns = shape
    if "pixels" in arrays:
        xs = arrays["pixels"][:, 0].astype(np.int64) - origin[0]
        ys = arrays["pixels"][:, 1].astype(np.int64) - origin[1]
    else:
        left, top, box = _region_box(arrays)
        ys, xs = np.nonzero(box)
        ys, xs = ys + top - origin[1], xs + left - origin[0]
    inside = (xs >= 0) & (ys >= 0) & (xs < columns) & (ys < rows)
    return ys[inside], xs[inside]


def _plan(touch_ups: list[dict], arrays: list[dict[str, np.ndarray]],
          shape: tuple[int, int], origin: tuple[int, int]) -> list[np.ndarray | bool | None]:
    """For each touch-up, what the edits made after it that are not replayed (their
    footprints) leave it: a pencil stroke, the mask of its pixels still its own; an
    area recolour, whether its region is untouched by them. ``None`` for a footprint.
    Newer edits win, so an older touch-up never paints over them."""
    later: np.ndarray | None = None
    plan: list[np.ndarray | bool | None] = [None] * len(touch_ups)
    for index in range(len(touch_ups) - 1, -1, -1):
        touch_up, members = touch_ups[index], arrays[index]
        if touch_up.get("resized"):
            continue
        if not touch_up.get("replays", True):
            ys, xs = _footprint(members, origin, shape)
            if len(ys):
                if later is None:
                    later = np.zeros(shape, dtype=bool)
                later[ys, xs] = True
        elif touch_up["tool"] == "paint_pixels":
            pixels = members["pixels"]
            own = np.ones(len(pixels), dtype=bool)
            if later is not None:
                xs = pixels[:, 0].astype(np.int64) - origin[0]
                ys = pixels[:, 1].astype(np.int64) - origin[1]
                inside = (xs >= 0) & (ys >= 0) & (xs < shape[1]) & (ys < shape[0])
                own[inside] = ~later[ys[inside], xs[inside]]
            plan[index] = own
        else:
            ys, xs = _footprint(members, origin, shape)
            plan[index] = later is None or not later[ys, xs].any()
    return plan


def _painting(touch_up: dict, removed: int | None) -> dict:
    """``touch_up`` as a run that removes palette index ``removed`` paints it: an erase
    paints that colour (``None``: nothing to erase with)."""
    return {**touch_up, "target_label": removed} if touch_up.get("erase") else touch_up


def _outcome(touch_up: dict, plan: np.ndarray | bool | None, palette_size: int) -> str | None:
    """Why ``touch_up`` (as ``_painting`` gives it) does not replay before its region
    is judged, or ``None``."""
    target, source = touch_up["target_label"], touch_up.get("source_label")
    if touch_up.get("resized"):
        return RESIZED
    if plan is False or (isinstance(plan, np.ndarray) and len(plan) and not plan.any()):
        return SUPERSEDED
    if touch_up.get("erase") and target is None:
        return ERASE_UNREMOVED
    if target is None or not 0 <= target < palette_size:
        return COLOUR_GONE
    if touch_up["tool"] == "reassign_region" and (source is None or not 0 <= source < palette_size):
        return AREA_COLOUR_GONE
    return None


def _replay_one(labels: np.ndarray, touch_up: dict, arrays: dict[str, np.ndarray],
                own: np.ndarray | None, origin: tuple[int, int], whole: bool) -> bool:
    """Replay one touch-up on ``labels``, a window of the raster whose top-left pixel
    is ``origin`` (x, y) (the whole raster when ``whole``); a pencil stroke only on its
    ``own`` pixels. Whether it applied."""
    target = int(touch_up["target_label"])
    rows, columns = labels.shape
    if touch_up["tool"] == "paint_pixels":
        pixels = arrays["pixels"]
        xs, ys = pixels[:, 0] - origin[0], pixels[:, 1] - origin[1]
        inside = (xs >= 0) & (ys >= 0) & (xs < columns) & (ys < rows)
        if whole and not inside.all():
            raise ValueError("a recorded pencil stroke lies outside the raster it touches up")
        keep = inside & own if own is not None else inside
        labels[ys[keep], xs[keep]] = target
        return True
    left, top, box = _region_box(arrays)
    left, top = left - origin[0], top - origin[1]
    height, width = box.shape
    if whole and (left < 0 or top < 0 or top + height > rows or left + width > columns):
        return False
    if not _region_unchanged(labels, left, top, box, int(touch_up["source_label"])):
        return False
    y0, x0 = max(0, top), max(0, left)
    y1, x1 = min(rows, top + height), min(columns, left + width)
    if y0 < y1 and x0 < x1:
        labels[y0:y1, x0:x1][box[y0 - top:y1 - top, x0 - left:x1 - left]] = target
    return True


def replay_touch_ups(labels: np.ndarray, touch_ups: list[dict], palette_size: int,
                     removed: int | None = None
                     ) -> tuple[list[dict], list[dict[str, np.ndarray]], list[dict], list[dict]]:
    """Replay ``touch_ups`` (``PendingTouchUp.spec`` form) on ``labels`` in place, in
    order, never over a footprint recorded after one (``_plan``), an erase in the
    colour the run removes (palette index ``removed``). Returns the entries and arrays
    to record for the next run, in the original order: the replayed touch-ups, and
    those kept though not applied (``KEPT_REASONS``, marked ``waiting``), a pencil
    stroke's with only the pixels no later edit painted; the skipped ones,
    ``{"tool", "point", "reason", "kept"}``, ``point`` the click point or a stroke's
    first pixel, ``reason`` ``RESIZED``, ``SUPERSEDED``, ``ERASE_UNREMOVED``,
    ``COLOUR_GONE``, ``AREA_COLOUR_GONE`` or ``AREA_CHANGED``; and the replayed pencil
    strokes a later edit painted over in part, in the same form."""
    arrays = [load_arrays(Path(touch_up["path"]), int(touch_up["member"]))
              for touch_up in touch_ups]
    plan = _plan(touch_ups, arrays, labels.shape, (0, 0))
    entries: list[dict] = []
    arrays_out: list[dict[str, np.ndarray]] = []
    skipped: list[dict] = []
    partly: list[dict] = []
    for recorded, members, own in zip(touch_ups, arrays, plan):
        if not recorded.get("replays", True):
            continue
        touch_up = _painting(recorded, removed)
        tool = touch_up["tool"]
        location = _location(tool, members, touch_up.get("point"))
        reason = _outcome(touch_up, own, palette_size)
        if reason is None and not _replay_one(
                labels, touch_up, members, own if isinstance(own, np.ndarray) else None,
                (0, 0), whole=True):
            reason = AREA_CHANGED
        if reason is not None:
            kept = reason in KEPT_REASONS
            skipped.append({"tool": tool, "point": location, "reason": reason, "kept": kept})
            if not kept:
                continue
        # An erase records the colour it paints as it was made; each run resolves it anew.
        target = recorded["target_label"] if reason is not None else touch_up["target_label"]
        entry = {"tool": tool, "target_label": None if target is None else int(target),
                 "erase": bool(touch_up.get("erase"))}
        if reason is not None:
            entry["waiting"] = True
        if tool == "reassign_region":
            entry.update(source_label=int(touch_up["source_label"]), point=location)
        elif not own.all():
            members = {"pixels": members["pixels"][own]}
            if reason is None:
                partly.append({"tool": tool, "point": location})
        entries.append(entry)
        arrays_out.append(members)
    return entries, arrays_out, skipped, partly


def replay_in_window(labels: np.ndarray, origin: tuple[int, int], touch_ups: list[dict],
                     palette_size: int, removed: int | None = None) -> None:
    """Replay ``touch_ups`` on ``labels`` in place, a window of the raster whose
    top-left pixel is ``origin`` (x, y), as a Cleanup run replays them on the whole
    raster (``replay_touch_ups``), for a preview of that run. An area recolour, and
    whether a later edit covers it, are judged on its part inside the window, so one
    changed only outside it replays here, where the run would skip it."""
    arrays = [load_arrays(Path(touch_up["path"]), int(touch_up["member"]))
              for touch_up in touch_ups]
    plan = _plan(touch_ups, arrays, labels.shape, origin)
    for touch_up, members, own in zip(touch_ups, arrays, plan):
        touch_up = _painting(touch_up, removed)
        if touch_up.get("replays", True) and _outcome(touch_up, own, palette_size) is None:
            _replay_one(labels, touch_up, members,
                        own if isinstance(own, np.ndarray) else None, origin, whole=False)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def _places(rows: list[dict]) -> str:
    places = [f"{'an area' if row['tool'] == 'reassign_region' else 'a pencil stroke'} "
              f"at ({row['point'][0]}, {row['point'][1]})" for row in rows]
    return places[0] if len(places) == 1 else ", ".join(places[:-1]) + " and " + places[-1]


def replay_message(total: int, replayed: int, skipped: list[dict], partly: list[dict]) -> str:
    """What a Cleanup run did with the touch-ups it replayed, naming where each one
    it kept waiting, dropped, or replayed only in part, is."""
    parts = [f"Replayed {replayed} of {total} {_plural(total, 'touch-up', 'touch-ups')}"]

    def rows(reason: str) -> list[dict]:
        return [row for row in skipped if row["reason"] == reason]

    def was(count: int) -> str:
        return _plural(count, "was", "were")

    if unremoved := rows(ERASE_UNREMOVED):
        parts.append(f"{len(unremoved)} {_plural(len(unremoved), 'erase is', 'erases are')} "
                     f"still waiting because Cleanup removes no colour: {_places(unremoved)}")
    if changed := rows(AREA_CHANGED):
        count = len(changed)
        parts.append(f"{count} area {_plural(count, 'edit is', 'edits are')} still waiting "
                     f"because {_plural(count, 'that area has', 'those areas have')} changed "
                     f"since {_plural(count, 'it was', 'they were')} edited: {_places(changed)}")
    if resized := rows(RESIZED):
        parts.append(f"{len(resized)} {was(len(resized))} dropped because the image was "
                     f"re-flattened at a different size: {_places(resized)}")
    if superseded := rows(SUPERSEDED):
        count = len(superseded)
        parts.append(f"{count} {_plural(count, 'touch-up', 'touch-ups')} {was(count)} dropped "
                     f"because a later edit replaced {_plural(count, 'it', 'them')}: "
                     f"{_places(superseded)}")
    if gone := rows(COLOUR_GONE):
        count = len(gone)
        parts.append(f"{count} {_plural(count, 'touch-up', 'touch-ups')} {was(count)} dropped "
                     f"because {_plural(count, 'it paints', 'they paint')} a colour no longer in "
                     f"the palette (merged away or re-flattened): {_places(gone)}")
    if area_gone := rows(AREA_COLOUR_GONE):
        count = len(area_gone)
        parts.append(f"{count} area {_plural(count, 'edit', 'edits')} {was(count)} dropped because "
                     f"the colour of the area {_plural(count, 'it', 'they')} recoloured is no longer "
                     f"in the palette (merged away or re-flattened): {_places(area_gone)}")
    if partly:
        parts.append(f"{len(partly)} {_plural(len(partly), 'touch-up', 'touch-ups')} "
                     f"{was(len(partly))} partly superseded by a later edit: {_places(partly)}")
    return "; ".join(parts)
