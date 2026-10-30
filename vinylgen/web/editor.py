from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from vinylgen.contracts import SegmentationBundle
from vinylgen.reporting import create_report

from .models import LabelEditOperation
from .npz_edit import reuse_member_supported, savez_bytes, write_npz_reusing_member
from .pipeline import cleanup_removed_index, inherited_remove_colour, nearest_cleanup
from .store import Store
from .touch_ups import (CLEANED_FLATTENED_FILE, CLEANED_FLATTENED_KIND, CLEANED_LABELS_KIND,
                        TOUCH_UPS_FILE, TOUCH_UPS_KIND, load_cleaned_labels, pixel_arrays,
                        region_arrays, touch_ups_bytes)

# Four-way connectivity: the authoritative rule for what one physical vinyl region is.
_CROSS = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


# While a revision's cleanup is current, edits apply to its cleaned raster: an area
# recolour or pencil stroke there is a touch-up (``web.touch_ups``) that the next
# Cleanup run replays, and it leaves the cleanup current. A rename or recolour applies
# to the cleaned raster and to the raster from before cleanup alike. A merge applies to
# the raster from before cleanup and marks the cleanup stale, as does an area or pencil
# edit made without a current cleanup, which applies to that raster: the next cleanup
# starts from it, so an earlier cleanup is never baked into it.


def edits_cleaned_raster(store: Store, revision_id: str) -> bool:
    """Whether edits of ``revision_id`` apply to its cleaned raster: its nearest
    cleanup ancestor is current (not stale)."""
    cleanup_revision, stale = nearest_cleanup(store, revision_id)
    return cleanup_revision is not None and not stale


def cleaned_image_labels(store: Store, revision_id: str) -> np.ndarray | None:
    """The labels of the Cleaned image while touch-ups on it are shown at ``revision_id``:
    the current cleanup's own result before any touch-up. That is the ``cleaned-labels``
    a Cleanup run wrote when its replayed touch-ups changed its raster, else the labels
    of the revision before the oldest label edit since that cleanup that recorded
    touch-ups (renames and recolours never change labels). ``None`` without a current
    cleanup or without touch-ups since it."""
    cleanup_revision, stale = nearest_cleanup(store, revision_id)
    if cleanup_revision is None or stale:
        return None
    own = store.effective_artifacts(revision_id).get(CLEANED_LABELS_KIND)
    if own is not None:
        return load_cleaned_labels(store.artifact_path(own["id"]))
    base: str | None = None
    current: str | None = revision_id
    while current and current != cleanup_revision["id"]:
        revision = store.get_revision(current)
        if revision["kind"] == "label-edit" and revision["settings"].get("touch_ups"):
            base = revision["parent_revision_id"]
        current = revision["parent_revision_id"]
    if base is None:
        return None
    segmentation = store.effective_artifacts(base)["segmentation"]
    return SegmentationBundle.load(store.artifact_path(segmentation["id"]), with_costs=False).labels


@dataclass
class _Raster:
    """One raster of a revision that an edit batch reads and changes: its cleaned
    raster, or the raster from before cleanup. Loaded on first use."""

    store: Store
    revision_id: str
    before_cleanup: bool
    with_costs: bool = False
    _bundle: SegmentationBundle | None = None
    _report: dict[str, Any] | None = None
    names_changed: bool = False
    rgb_changed: bool = False
    labels_changed: bool = False

    def _artifact_path(self, kind: str) -> Path:
        artifacts = self.store.effective_artifacts(self.revision_id,
                                                   before_cleanup=self.before_cleanup)
        missing = {"segmentation", "palette"} - artifacts.keys()
        if missing:
            raise ValueError(f"revision is missing editor artifacts: {', '.join(sorted(missing))}")
        return self.store.artifact_path(artifacts[kind]["id"])

    @property
    def segmentation_path(self) -> Path:
        return self._artifact_path("segmentation")

    @property
    def bundle(self) -> SegmentationBundle:
        if self._bundle is None:
            self._bundle = SegmentationBundle.load(self.segmentation_path,
                                                   with_costs=self.with_costs)
            self._bundle.validate(check_costs=self.with_costs)
        return self._bundle

    @property
    def report(self) -> dict[str, Any]:
        if self._report is None:
            self._report = json.loads(self._artifact_path("palette").read_text())
        return self._report

    @property
    def changed(self) -> bool:
        return self.names_changed or self.rgb_changed or self.labels_changed


def _editor_raster(store: Store, revision_id: str) -> _Raster:
    """The raster the editor shows and edits for ``revision_id``: the cleaned raster of
    a current cleanup, else the raster from before cleanup."""
    return _Raster(store, revision_id,
                   before_cleanup=not edits_cleaned_raster(store, revision_id))


def editor_state(store: Store, revision_id: str) -> dict[str, Any]:
    raster = _editor_raster(store, revision_id)
    bundle, report = raster.bundle, raster.report
    # A trace records the palette.json it traced, and a cleanup the shares of its own
    # raster; state the shares of the raster the editor actually edits.
    _refresh_report(report, bundle)
    return {"width": bundle.labels.shape[1], "height": bundle.labels.shape[0],
            "palette": report["palette"],
            "requested_colour_count": report.get("fixed_colour_count", {}).get("requested"),
            "protected_pixels": int(bundle.explicit_protected_features.sum()),
            "removed_colour_index": removed_colour_index(store, revision_id,
                                                         len(report["palette"])),
            "inherited_remove_colour": _inherited_remove_colour(store, revision_id)}


def _inherited_remove_colour(store: Store, revision_id: str) -> dict[str, Any] | None:
    """The removed colour the Cleanup form inherits for ``revision_id``
    (``pipeline.inherited_remove_colour``, against the palette a Cleanup run of it
    starts from): ``{"value", "unresolved", "hex", "problem"}``, ``value`` ``None``
    with ``unresolved`` when the last cleanup's colour is not exactly one colour of it
    (``problem``: ``"missing"`` or ``"ambiguous"``), ``hex`` the cleanup's own. ``None``
    when no cleanup ancestor removed a colour."""
    try:
        inherited = inherited_remove_colour(store, revision_id)
    except (KeyError, ValueError):
        return None
    if inherited is None:
        return None
    return {"value": inherited["value"], "unresolved": inherited["value"] is None,
            "hex": inherited["hex"], "problem": inherited["problem"]}


def removed_colour_index(store: Store, revision_id: str, palette_size: int) -> int | None:
    """The palette index of the colour a trace of ``revision_id`` leaves out: the one
    its active (not stale) cleanup removed, by index rather than by name, since a
    trace's colour names may have renamed the palette this revision shows. ``None``
    when nothing is removed or the record identifies no colour of the palette."""
    cleanup_revision, stale = nearest_cleanup(store, revision_id)
    if cleanup_revision is None or stale:
        return None
    try:
        index = cleanup_removed_index(store, cleanup_revision)
    except ValueError:
        return None
    return index if index is not None and index < palette_size else None


def _check_label(value: int | None, count: int, field: str = "label") -> int:
    if value is None or value >= count:
        raise ValueError(f"{field} must identify an existing palette colour")
    return value


def _refresh_report(report: dict[str, Any], bundle: SegmentationBundle) -> None:
    total = max(1, bundle.labels.size)
    count = len(bundle.palette_rgb)
    report["inferred_colour_count"] = count
    # One pass over the labels tallies every colour's pixel count, versus a full scan
    # per colour — the difference is O(pixels) instead of O(pixels·palette).
    shares = np.bincount(bundle.labels.ravel(), minlength=count)
    for index, row in enumerate(report["palette"]):
        rgb = [int(value) for value in bundle.palette_rgb[index]]
        row.update(index=index, rgb=rgb, hex="#" + "".join(f"{value:02X}" for value in rgb),
                   share_percent=round(float(shares[index]) * 100 / total, 4))
    report["editor"] = {"authoritative": "segmentation.labels", "connectivity": 4}


def _component_at(labels: np.ndarray, x: int, y: int) -> np.ndarray:
    if x < 0 or y < 0 or y >= labels.shape[0] or x >= labels.shape[1]:
        raise ValueError("point must lie inside the flattened image")
    # The four-connected region of the point's colour, found with a compiled labeller
    # rather than an interpreted flood fill so it stays fast on gigapixel rasters.
    source = int(labels[y, x])
    components, _ = ndimage.label(labels == source, structure=_CROSS)
    return components == components[y, x]


def _serialize_segmentation(bundle: SegmentationBundle, source: Path, *,
                            costs_changed: bool) -> io.BytesIO:
    """Write the edited segmentation, copying the cost member of ``source`` (the
    segmentation it was edited from) when it is unchanged."""
    fresh = {
        "labels": bundle.labels.astype(np.int16),
        "protected_features": bundle.protected_features.astype(bool),
        "explicit_protected_features": bundle.explicit_protected_features.astype(bool),
        "palette_rgb": bundle.palette_rgb.astype(np.uint8),
        "contract_version": np.array(3, dtype=np.int16),
    }
    if costs_changed:
        stream = io.BytesIO()
        np.savez_compressed(stream, palette_costs=bundle.palette_costs.astype(np.float16), **fresh)
        return stream

    if reuse_member_supported():
        try:
            stream = io.BytesIO()
            write_npz_reusing_member(
                stream, source=source.read_bytes(), reuse="palette_costs", fresh=fresh)
            return stream
        except Exception:  # noqa: BLE001 - any container issue falls back to the safe path
            pass
    # Fallback: re-encode the unchanged cost volume loaded from the source verbatim.
    costs = SegmentationBundle.load(source, with_costs=True).palette_costs
    return io.BytesIO(savez_bytes(palette_costs=costs.astype(np.float16), **fresh))


def _png(image: Image.Image) -> io.BytesIO:
    stream = io.BytesIO()
    # The images are derived output for a transient edit revision; a fast deflate level
    # keeps a gigapixel re-encode cheap without changing the exact on-palette pixels.
    image.save(stream, "PNG", compress_level=1)
    return stream


def _json_bytes(report: dict[str, Any]) -> io.BytesIO:
    return io.BytesIO((json.dumps(report, indent=2) + "\n").encode())


def _raster_streams(raster: _Raster, *, costs_changed: bool
                    ) -> list[tuple[str, str, str, io.BytesIO]]:
    """The segmentation, flattened raster and palette.json of an edited raster, as
    ``(kind, filename, media type, stream)``, and its ``report.png`` when its pixels
    or colours changed (the report shows each colour's hex and share)."""
    _refresh_report(raster.report, raster.bundle)
    flattened = Image.fromarray(raster.bundle.palette_rgb[raster.bundle.labels].astype(np.uint8), "RGB")
    streams = [
        ("segmentation", "segmentation.npz", "application/octet-stream",
         _serialize_segmentation(raster.bundle, raster.segmentation_path,
                                 costs_changed=costs_changed)),
        ("flattened", "flattened.png", "image/png", _png(flattened)),
        ("palette", "palette.json", "application/json", _json_bytes(raster.report)),
    ]
    if raster.labels_changed or raster.rgb_changed:
        legend = sorted(raster.report["palette"], key=lambda row: -float(row["share_percent"]))
        streams.append(("report", "report.png", "image/png", _png(create_report(flattened, legend))))
    return streams


def _before_cleanup_streams(raster: _Raster) -> list[tuple[str, str, str, io.BytesIO]]:
    """The raster from before cleanup as a rename or recolour on the cleaned raster
    left it, under the ``before-cleanup-*`` kinds (``store.BEFORE_CLEANUP_KINDS``): its
    palette.json, and, when colours changed, its segmentation, flattened raster and
    report."""
    if not raster.rgb_changed:
        # A rename changes names only, so the raster is as it was; its shares are
        # restated from it, since the palette.json it resolves may be a trace's, which
        # states the cleaned raster's.
        _refresh_report(raster.report, raster.bundle)
        return [("before-cleanup-palette", "before-cleanup-palette.json", "application/json",
                 _json_bytes(raster.report))]
    return [(f"before-cleanup-{kind}", f"before-cleanup-{filename}", media_type, stream)
            for kind, filename, media_type, stream in _raster_streams(raster, costs_changed=False)]


def apply_edits(store: Store, project_id: str, revision_id: str,
                operations: list[LabelEditOperation], message: str | None) -> str:
    parent = store.get_revision(revision_id)
    if parent["project_id"] != project_id:
        raise ValueError("revision belongs to another project")
    # Only a merge rewrites the cost volume (it drops a column), and only of the raster
    # from before cleanup; every other edit leaves it byte-identical. Load the huge cost
    # field only when it will actually change.
    costs_changed = any(operation.tool == "merge_colour" for operation in operations)
    before = _Raster(store, revision_id, before_cleanup=True, with_costs=costs_changed)
    # The cleaned raster while the cleanup stays current; a merge makes it stale.
    cleaned = (_Raster(store, revision_id, before_cleanup=False)
               if edits_cleaned_raster(store, revision_id) else None)
    # Since a cleanup, every area or pencil edit is recorded (web.touch_ups): one on its
    # cleaned raster as a touch-up the next Cleanup run replays, one made while it is
    # stale as the footprint no older touch-up paints over.
    records_edits = nearest_cleanup(store, revision_id)[0] is not None
    # A touch-up painted in the colour the current cleanup removes is an erase: the next
    # Cleanup run paints it in whatever colour that run removes (web.touch_ups).
    removed = (removed_colour_index(store, revision_id, len(cleaned.report["palette"]))
               if cleaned is not None else None)
    stales_cleanup = False
    # The cleaned labels before this batch's first touch-up, kept when the batch also
    # recolours: with no touch-up before the batch, they are the Cleaned image.
    snapshots_cleaned = cleaned is not None and any(
        operation.tool == "recolour_colour" for operation in operations)
    before_touch_ups: np.ndarray | None = None
    touch_ups: list[dict[str, Any]] = []
    touch_up_arrays: list[dict[str, np.ndarray]] = []

    for index, operation in enumerate(operations):
        edited = cleaned if cleaned is not None else before
        count = len(edited.report["palette"])
        if operation.tool == "rename_colour":
            label = _check_label(operation.label, count)
            if not operation.name:
                raise ValueError("rename_colour requires a name")
            for raster in (before, cleaned):
                if raster is not None:
                    raster.report["palette"][label]["name"] = operation.name
                    raster.names_changed = True
        elif operation.tool == "recolour_colour":
            label = _check_label(operation.label, count)
            if operation.rgb is None:
                raise ValueError("recolour_colour requires an rgb value")
            # Repaint a whole palette colour: the raster is palette_rgb[labels], so
            # swapping the entry recolours every pixel of that colour at once. Labels
            # are untouched, so regions, costs, and connectivity carry over unchanged.
            for raster in (before, cleaned):
                if raster is not None:
                    raster.bundle.palette_rgb[label] = np.array(operation.rgb, dtype=np.uint8)
                    raster.rgb_changed = True
        elif operation.tool == "merge_colour":
            source = _check_label(operation.source_label, count, "source_label")
            target = _check_label(operation.target_label, count, "target_label")
            if source == target:
                raise ValueError("merge source and target must differ")
            bundle = before.bundle
            bundle.labels[bundle.labels == source] = target
            bundle.labels[bundle.labels > source] -= 1
            bundle.palette_rgb = np.delete(bundle.palette_rgb, source, axis=0)
            bundle.palette_costs = np.delete(bundle.palette_costs, source, axis=2)
            before.report["palette"].pop(source)
            before.labels_changed = before.rgb_changed = True
            # A merge changes the raster's structure, so the cleanup is stale from here
            # and its cleaned raster is left behind; the next Cleanup run replays the
            # touch-ups made on it.
            cleaned, stales_cleanup = None, True
        elif operation.tool == "reassign_region":
            target = _check_label(operation.target_label, count, "target_label")
            if operation.point is None:
                raise ValueError("reassign_region requires a point")
            x, y = operation.point
            labels = edited.bundle.labels
            if snapshots_cleaned and edited is cleaned and before_touch_ups is None:
                before_touch_ups = labels.copy()
            component = _component_at(labels, x, y)
            source = int(labels[y, x])
            if source == target:
                raise ValueError("selected region already uses the target colour")
            labels[component] = target
            edited.labels_changed = True
            stales_cleanup = stales_cleanup or cleaned is None
            if records_edits:
                touch_ups.append({"operation": index, "tool": operation.tool,
                                  "target_label": target, "source_label": source,
                                  "point": [x, y], "replays": cleaned is not None,
                                  "erase": cleaned is not None and target == removed})
                touch_up_arrays.append(region_arrays(component))
        elif operation.tool == "paint_pixels":
            target = _check_label(operation.target_label, count, "target_label")
            if not operation.pixels:
                raise ValueError("paint_pixels requires at least one pixel")
            labels = edited.bundle.labels
            if snapshots_cleaned and edited is cleaned and before_touch_ups is None:
                before_touch_ups = labels.copy()
            height, width = labels.shape
            # Repaint individual pixels. Regions are derived from the label grid by
            # four-way connectivity, so a stroke that severs a region splits it into
            # separate components the next time anything recomputes connectivity.
            for x, y in operation.pixels:
                if x < 0 or y < 0 or y >= height or x >= width:
                    raise ValueError("paint pixel lies outside the flattened image")
                labels[y, x] = target
            edited.labels_changed = True
            stales_cleanup = stales_cleanup or cleaned is None
            if records_edits:
                touch_ups.append({"operation": index, "tool": operation.tool,
                                  "target_label": target, "replays": cleaned is not None,
                                  "erase": cleaned is not None and target == removed})
                touch_up_arrays.append(pixel_arrays(operation.pixels))
        else:  # pragma: no cover - Pydantic constrains the value
            raise ValueError(f"unsupported editor tool: {operation.tool}")

    edited = cleaned if cleaned is not None else before
    edited.bundle.validate(check_costs=edited is before and costs_changed)
    streams = _raster_streams(edited, costs_changed=edited is before and costs_changed)
    if cleaned is not None and before.changed:
        streams.extend(_before_cleanup_streams(before))
    if cleaned is not None and cleaned.rgb_changed:
        # A recolour while touch-ups are shown (made before this batch, or by it) recolours
        # the Cleaned image behind them too.
        cleaned_labels = cleaned_image_labels(store, revision_id)
        if cleaned_labels is None:
            cleaned_labels = before_touch_ups
        if cleaned_labels is not None:
            streams.append((CLEANED_FLATTENED_KIND, CLEANED_FLATTENED_FILE, "image/png",
                            _png(Image.fromarray(cleaned.bundle.palette_rgb[cleaned_labels].astype(np.uint8), "RGB"))))
    if touch_ups:
        streams.append((TOUCH_UPS_KIND, TOUCH_UPS_FILE, "application/octet-stream",
                        io.BytesIO(touch_ups_bytes(touch_up_arrays))))
    settings: dict[str, Any] = {
        "pipeline_stage": "label-edit",
        "operations": [operation.model_dump(mode="json") for operation in operations],
        "stale_concerns": (["cleanup"] if stales_cleanup else []) + ["trace", "validation", "export"]}
    if cleaned is not None:
        # Its raster artifacts are the cleaned raster (store.describes_cleaned_raster).
        settings["cleaned_raster"] = True
    if touch_ups:
        settings["touch_ups"] = touch_ups
    revision = store.create_revision(
        project_id, parent_revision_id=revision_id, kind="label-edit",
        message=message or f"Applied {len(operations)} flattened-label edit(s)",
        settings=settings)
    for kind, filename, media_type, stream in streams:
        stream.seek(0)
        store.add_artifact(revision["id"], kind=kind, filename=filename,
                           media_type=media_type, source=stream)
    return revision["id"]
