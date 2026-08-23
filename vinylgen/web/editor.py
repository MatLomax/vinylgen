from __future__ import annotations

import io
import json
from collections import deque
from copy import deepcopy
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from vinylgen.contracts import SegmentationBundle

from .models import LabelEditOperation
from .store import Store


def _inputs(store: Store, revision_id: str) -> tuple[SegmentationBundle, dict[str, Any]]:
    artifacts = store.effective_artifacts(revision_id)
    missing = {"segmentation", "palette"} - artifacts.keys()
    if missing:
        raise ValueError(f"revision is missing editor artifacts: {', '.join(sorted(missing))}")
    bundle = SegmentationBundle.load(store.artifact_path(artifacts["segmentation"]["id"]))
    bundle.validate()
    report = json.loads(store.artifact_path(artifacts["palette"]["id"]).read_text())
    return bundle, report


def editor_state(store: Store, revision_id: str) -> dict[str, Any]:
    bundle, report = _inputs(store, revision_id)
    return {"width": bundle.labels.shape[1], "height": bundle.labels.shape[0],
            "palette": report["palette"],
            "protected_pixels": int(bundle.explicit_protected_features.sum())}


def _check_label(value: int | None, count: int, field: str = "label") -> int:
    if value is None or value >= count:
        raise ValueError(f"{field} must identify an existing palette colour")
    return value


def _check_point(point: tuple[int, int], labels: np.ndarray) -> tuple[int, int]:
    x, y = point
    if not (0 <= x < labels.shape[1] and 0 <= y < labels.shape[0]):
        raise ValueError(f"point ({x}, {y}) is outside the image")
    return x, y


def _connected(labels: np.ndarray, x: int, y: int) -> np.ndarray:
    wanted = int(labels[y, x])
    selected = np.zeros(labels.shape, dtype=bool)
    selected[y, x] = True
    queue = deque([(x, y)])
    while queue:
        cx, cy = queue.popleft()
        for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
            if (0 <= nx < labels.shape[1] and 0 <= ny < labels.shape[0]
                    and not selected[ny, nx] and labels[ny, nx] == wanted):
                selected[ny, nx] = True
                queue.append((nx, ny))
    return selected


def _shape_mask(labels: np.ndarray, operation: LabelEditOperation) -> np.ndarray:
    if not operation.points:
        raise ValueError(f"{operation.tool} requires at least one point")
    points = [_check_point(point, labels) for point in operation.points]
    mask_image = Image.new("1", (labels.shape[1], labels.shape[0]))
    draw = ImageDraw.Draw(mask_image)
    if operation.tool == "lasso":
        if len(points) < 3:
            raise ValueError("lasso requires at least three points")
        draw.polygon(points, fill=1)
    else:
        radius = 0 if operation.tool == "pencil" else operation.radius
        width = radius * 2 + 1
        if len(points) == 1:
            x, y = points[0]
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=1)
        else:
            draw.line(points, fill=1, width=width, joint="curve")
            for x, y in (points[0], points[-1]):
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=1)
    return np.asarray(mask_image, dtype=bool)


def _refresh_report(report: dict[str, Any], bundle: SegmentationBundle) -> None:
    total = max(1, bundle.labels.size)
    report["inferred_colour_count"] = len(bundle.palette_rgb)
    for index, row in enumerate(report["palette"]):
        rgb = [int(value) for value in bundle.palette_rgb[index]]
        row.update(index=index, rgb=rgb, hex="#" + "".join(f"{value:02X}" for value in rgb),
                   share_percent=round(float(np.count_nonzero(bundle.labels == index)) * 100 / total, 4))
    report["editor"] = {"authoritative": "segmentation.labels", "connectivity": 4}


def apply_edits(store: Store, project_id: str, revision_id: str,
                operations: list[LabelEditOperation], message: str | None) -> str:
    parent = store.get_revision(revision_id)
    if parent["project_id"] != project_id:
        raise ValueError("revision belongs to another project")
    bundle, report = _inputs(store, revision_id)
    report = deepcopy(report)
    palette = report["palette"]

    for operation in operations:
        count = len(bundle.palette_rgb)
        if operation.tool in {"pencil", "brush", "lasso"}:
            label = _check_label(operation.label, count)
            bundle.labels[_shape_mask(bundle.labels, operation)] = label
        elif operation.tool in {"flood_fill", "component"}:
            label = _check_label(operation.label, count)
            if len(operation.points) != 1:
                raise ValueError(f"{operation.tool} requires exactly one seed point")
            x, y = _check_point(operation.points[0], bundle.labels)
            bundle.labels[_connected(bundle.labels, x, y)] = label
        elif operation.tool == "protect":
            if len(operation.points) == 1:
                x, y = _check_point(operation.points[0], bundle.labels)
                mask = _connected(bundle.labels, x, y)
            else:
                mask = _shape_mask(bundle.labels, operation.model_copy(update={"tool": "lasso"}))
            bundle.explicit_protected_features[mask] = True
            bundle.protected_features[mask] = True
        elif operation.tool == "rename_colour":
            label = _check_label(operation.label, count)
            if not operation.name:
                raise ValueError("rename_colour requires a name")
            palette[label]["name"] = operation.name
        elif operation.tool == "adjust_colour":
            label = _check_label(operation.label, count)
            if operation.rgb is None or any(not 0 <= value <= 255 for value in operation.rgb):
                raise ValueError("adjust_colour requires RGB values from 0 to 255")
            bundle.palette_rgb[label] = operation.rgb
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
        else:  # pragma: no cover - Pydantic constrains the value
            raise ValueError(f"unsupported editor tool: {operation.tool}")

    bundle.validate()
    _refresh_report(report, bundle)
    revision = store.create_revision(
        project_id, parent_revision_id=revision_id, kind="label-edit",
        message=message or f"Applied {len(operations)} flattened-label edit(s)",
        settings={"pipeline_stage": "label-edit",
                  "operations": [operation.model_dump(mode="json") for operation in operations],
                  "stale_concerns": ["trace", "validation", "export"]})

    segmentation = io.BytesIO()
    np.savez_compressed(segmentation, labels=bundle.labels.astype(np.int16),
                        protected_features=bundle.protected_features.astype(bool),
                        explicit_protected_features=bundle.explicit_protected_features.astype(bool),
                        palette_rgb=bundle.palette_rgb.astype(np.uint8),
                        palette_costs=bundle.palette_costs.astype(np.float16),
                        contract_version=np.array(3, dtype=np.int16))
    flattened = io.BytesIO()
    Image.fromarray(bundle.palette_rgb[bundle.labels].astype(np.uint8), "RGB").save(flattened, "PNG")
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
