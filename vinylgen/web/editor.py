from __future__ import annotations

import io
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from vinylgen.contracts import SegmentationBundle

from .models import LabelEditOperation
from .npz_edit import reuse_member_supported, savez_bytes, write_npz_reusing_member
from .store import Store

# Four-way connectivity: the authoritative rule for what one physical vinyl region is.
_CROSS = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=bool)


# Palette and label edits apply to the raster from before any cleanup: an edit made
# after a cleanup marks that cleanup stale, and the next cleanup starts from this
# uncleaned-plus-edited raster, so an earlier cleanup is never baked into it.


def _segmentation_path(store: Store, revision_id: str) -> Path:
    artifacts = store.effective_artifacts(revision_id, before_cleanup=True)
    missing = {"segmentation", "palette"} - artifacts.keys()
    if missing:
        raise ValueError(f"revision is missing editor artifacts: {', '.join(sorted(missing))}")
    return store.artifact_path(artifacts["segmentation"]["id"])


def _inputs(store: Store, revision_id: str, *, with_costs: bool = False,
            ) -> tuple[SegmentationBundle, dict[str, Any]]:
    artifacts = store.effective_artifacts(revision_id, before_cleanup=True)
    missing = {"segmentation", "palette"} - artifacts.keys()
    if missing:
        raise ValueError(f"revision is missing editor artifacts: {', '.join(sorted(missing))}")
    bundle = SegmentationBundle.load(
        store.artifact_path(artifacts["segmentation"]["id"]), with_costs=with_costs)
    bundle.validate(check_costs=with_costs)
    report = json.loads(store.artifact_path(artifacts["palette"]["id"]).read_text())
    return bundle, report


def editor_state(store: Store, revision_id: str) -> dict[str, Any]:
    bundle, report = _inputs(store, revision_id)
    # A trace records the palette.json it traced, whose shares may describe a cleaned
    # raster; state the shares of the raster the editor actually edits.
    _refresh_report(report, bundle)
    return {"width": bundle.labels.shape[1], "height": bundle.labels.shape[0],
            "palette": report["palette"],
            "requested_colour_count": report.get("fixed_colour_count", {}).get("requested"),
            "protected_pixels": int(bundle.explicit_protected_features.sum())}


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


def _serialize_segmentation(store: Store, parent_revision_id: str,
                            bundle: SegmentationBundle, *, costs_changed: bool) -> io.BytesIO:
    """Write the edited segmentation, reusing the parent's cost member when it is unchanged."""
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

    parent_path = _segmentation_path(store, parent_revision_id)
    if reuse_member_supported():
        try:
            stream = io.BytesIO()
            write_npz_reusing_member(
                stream, source=parent_path.read_bytes(), reuse="palette_costs", fresh=fresh)
            return stream
        except Exception:  # noqa: BLE001 - any container issue falls back to the safe path
            pass
    # Fallback: re-encode the unchanged cost volume loaded from the parent verbatim.
    costs = SegmentationBundle.load(parent_path, with_costs=True).palette_costs
    return io.BytesIO(savez_bytes(palette_costs=costs.astype(np.float16), **fresh))


def apply_edits(store: Store, project_id: str, revision_id: str,
                operations: list[LabelEditOperation], message: str | None) -> str:
    parent = store.get_revision(revision_id)
    if parent["project_id"] != project_id:
        raise ValueError("revision belongs to another project")
    # Only a merge rewrites the cost volume (it drops a column); every other edit leaves
    # it byte-identical. Load the huge cost field only when it will actually change.
    costs_changed = any(operation.tool == "merge_colour" for operation in operations)
    bundle, report = _inputs(store, revision_id, with_costs=costs_changed)
    report = deepcopy(report)
    palette = report["palette"]

    for operation in operations:
        count = len(bundle.palette_rgb)
        if operation.tool == "rename_colour":
            label = _check_label(operation.label, count)
            if not operation.name:
                raise ValueError("rename_colour requires a name")
            palette[label]["name"] = operation.name
        elif operation.tool == "recolour_colour":
            label = _check_label(operation.label, count)
            if operation.rgb is None:
                raise ValueError("recolour_colour requires an rgb value")
            # Repaint a whole palette colour: the raster is palette_rgb[labels], so
            # swapping the entry recolours every pixel of that colour at once. Labels
            # are untouched, so regions, costs, and connectivity carry over unchanged.
            bundle.palette_rgb[label] = np.array(operation.rgb, dtype=np.uint8)
        elif operation.tool == "merge_colour":
            source = _check_label(operation.source_label, count, "source_label")
            target = _check_label(operation.target_label, count, "target_label")
            if source == target:
                raise ValueError("merge source and target must differ")
            bundle.labels[bundle.labels == source] = target
            bundle.labels[bundle.labels > source] -= 1
            bundle.palette_rgb = np.delete(bundle.palette_rgb, source, axis=0)
            bundle.palette_costs = np.delete(bundle.palette_costs, source, axis=2)
            palette.pop(source)
        elif operation.tool == "reassign_region":
            target = _check_label(operation.target_label, count, "target_label")
            if operation.point is None:
                raise ValueError("reassign_region requires a point")
            x, y = operation.point
            component = _component_at(bundle.labels, x, y)
            if int(bundle.labels[y, x]) == target:
                raise ValueError("selected region already uses the target colour")
            bundle.labels[component] = target
        elif operation.tool == "paint_pixels":
            target = _check_label(operation.target_label, count, "target_label")
            if not operation.pixels:
                raise ValueError("paint_pixels requires at least one pixel")
            height, width = bundle.labels.shape
            # Repaint individual pixels. Regions are derived from the label grid by
            # four-way connectivity, so a stroke that severs a region splits it into
            # separate components the next time anything recomputes connectivity.
            for x, y in operation.pixels:
                if x < 0 or y < 0 or y >= height or x >= width:
                    raise ValueError("paint pixel lies outside the flattened image")
                bundle.labels[y, x] = target
        else:  # pragma: no cover - Pydantic constrains the value
            raise ValueError(f"unsupported editor tool: {operation.tool}")

    bundle.validate(check_costs=costs_changed)
    _refresh_report(report, bundle)
    revision = store.create_revision(
        project_id, parent_revision_id=revision_id, kind="label-edit",
        message=message or f"Applied {len(operations)} flattened-label edit(s)",
        settings={"pipeline_stage": "label-edit",
                  "operations": [operation.model_dump(mode="json") for operation in operations],
                  "stale_concerns": ["cleanup", "trace", "validation", "export"]})

    segmentation = _serialize_segmentation(
        store, revision_id, bundle, costs_changed=costs_changed)
    flattened = io.BytesIO()
    # The raster is derived output for a transient edit revision; a fast deflate level
    # keeps a gigapixel re-encode cheap without changing the exact on-palette pixels.
    Image.fromarray(bundle.palette_rgb[bundle.labels].astype(np.uint8), "RGB").save(
        flattened, "PNG", compress_level=1)
    palette_json = io.BytesIO((json.dumps(report, indent=2) + "\n").encode())
    for kind, filename, media_type, stream in (
        ("segmentation", "segmentation.npz", "application/octet-stream", segmentation),
        ("flattened", "flattened.png", "image/png", flattened),
        ("palette", "palette.json", "application/json", palette_json),
    ):
        stream.seek(0)
        store.add_artifact(revision["id"], kind=kind, filename=filename,
                           media_type=media_type, source=stream)
    return revision["id"]
