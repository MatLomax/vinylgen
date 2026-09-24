"""The cleanup stage: cutter cleanup of a subject's labels under its physical size.

It runs between Edit and Trace. It records the physical output size and the removed
colour that the trace then uses, and, with cleanup enabled, rewrites the labels with
``cutter_cleanup.apply_cleanup`` (the implementation the trace CLI also runs inline).
Palette costs, explicit protection and the palette itself are carried unchanged;
automatic protection is cleared where the cleanup changed labels, and explicitly
protected detail is never removed.
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from vinylgen.contracts import CleanupSettings, SegmentationBundle
from vinylgen.cutter_cleanup import apply_cleanup, change_classes, resolve_physical_size
from vinylgen.palette import Progress, _noop
from vinylgen.reporting import create_report
from vinylgen.tracing import ensure_palette_names, resolve_colour

from .npz_edit import reuse_member_supported, savez_bytes, write_npz_reusing_member

CLEANUP_AUDIT = "cleanup.json"
CLEANUP_CHANGES_OVERLAY = "cleanup-changes-overlay.png"
# The overlay's palette: index 0 clear, 1 removed, 2 widened (opaque).
CHANGES_OVERLAY_PALETTE = [0, 0, 0, 230, 40, 40, 40, 200, 70]


def write_changes_overlay(path: Path, original: np.ndarray, after_removal: np.ndarray,
                          cleaned: np.ndarray) -> bool:
    """Write the run's net changes as a paletted PNG on the raster's grid.

    Pixels the removal changed are index 1, pixels the widening changed index 2
    (``change_classes``), everything else clear. One byte per pixel in memory. It is
    written only when a pixel changed; otherwise any earlier file is removed.
    Returns whether it was written.
    """
    removed, widened = change_classes(original, after_removal, cleaned)
    if not (removed.any() or widened.any()):
        path.unlink(missing_ok=True)
        return False
    indices = removed.view(np.uint8) + 2 * widened.view(np.uint8)
    overlay = Image.fromarray(indices, "P")
    overlay.putpalette(CHANGES_OVERLAY_PALETTE)
    overlay.save(path, transparency=bytes([0, 255, 255]), compress_level=1)
    return True


def _write_segmentation(path: Path, bundle: SegmentationBundle) -> None:
    """Rewrite ``path`` with the bundle's cleaned labels, copying its unchanged cost
    member verbatim rather than decompressing and recompressing it."""
    fresh = {
        "labels": bundle.labels.astype(np.int16),
        "protected_features": bundle.protected_features.astype(bool),
        "explicit_protected_features": bundle.explicit_protected_features.astype(bool),
        "palette_rgb": bundle.palette_rgb.astype(np.uint8),
        "contract_version": np.array(3, dtype=np.int16),
    }
    source = path.read_bytes()
    payload: bytes | None = None
    if reuse_member_supported():
        try:
            stream = io.BytesIO()
            write_npz_reusing_member(stream, source=source, reuse="palette_costs", fresh=fresh)
            payload = stream.getvalue()
        except Exception:  # noqa: BLE001 - any container issue falls back to the safe path
            payload = None
    if payload is None:
        costs = SegmentationBundle.load(path, with_costs=True).palette_costs
        payload = savez_bytes(palette_costs=costs.astype(np.float16), **fresh)
    path.write_bytes(payload)


def completion_message(audit: dict) -> str:
    """The job result line for a cleanup run, stating what it did to protected detail."""
    summary = audit["cleanup"]
    if not summary["enabled"]:
        return "Cleanup off: recorded the size and removed colour"
    return (f"Cleanup changed {audit['changed_pixels']} px; removed "
            f"{summary['auto_protected_components_cleared']} automatically protected details "
            f"({summary['auto_protected_pixels_cleared']} px); kept "
            f"{summary['protected_kept_regions']} explicitly protected regions under the threshold")


def clean_subject(subject_dir: Path, settings: CleanupSettings,
                  progress: Progress | None = None) -> dict:
    """Clean ``subject_dir``'s segmentation in place and write ``cleanup.json``.

    ``subject_dir`` holds the raster from before any earlier cleanup, so a run always
    cleans the uncleaned labels and never stacks on another cleanup. When the run
    changes pixels, ``segmentation.npz`` (labels and automatic protection),
    ``flattened.png`` and ``report.png`` are rewritten and ``cleanup-changes-overlay.png``
    records the changed pixels (``write_changes_overlay``); otherwise the raster is left
    untouched and the run records the size and removed colour. ``palette.json`` is
    always rewritten with the resulting raster's shares. The removed colour is
    validated against the palette here, so a bad value fails the cleanup run rather
    than the trace that follows it.
    """
    tick = progress or _noop
    tick(0.02, "Reading segmentation")
    palette_path = subject_dir / "palette.json"
    segmentation_path = subject_dir / "segmentation.npz"
    report = json.loads(palette_path.read_text())
    ensure_palette_names(report, {})
    palette = report["palette"]
    bundle = SegmentationBundle.load(segmentation_path, with_costs=False)
    bundle.validate(check_costs=False)
    expected_palette = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
    if not np.array_equal(bundle.palette_rgb, expected_palette):
        raise ValueError("segmentation palette does not match palette.json")
    omitted = resolve_colour(settings.remove_colour, palette)
    if omitted is not None and len(palette) == 1:
        raise ValueError("cannot omit the only palette colour")
    height, width = bundle.labels.shape
    if settings.cleanup:
        tick(0.1, "Cleaning up cutter geometry")
        stages: dict = {}
        cleaned, protected, summary, resolved = apply_cleanup(
            bundle.labels, palette, settings, omitted,
            protected_features=bundle.protected_features,
            explicit_protected_features=bundle.explicit_protected_features, stages=stages)
        changed = int(np.count_nonzero(cleaned != bundle.labels))
    else:
        resolved = resolve_physical_size(settings, width, height)
        summary = {"enabled": False, "action": settings.cleanup_action,
                   "protected_kept_regions": 0, "auto_protected_components_cleared": 0,
                   "auto_protected_pixels_cleared": 0}
        changed = 0
    # palette.json always states the shares of the raster this run results in, so a
    # later lookup never reads shares recorded for a different (cleaned) raster.
    shares = np.bincount((cleaned if changed else bundle.labels).ravel(), minlength=len(palette))
    total = max(1, bundle.labels.size)
    for index, row in enumerate(palette):
        row["share_percent"] = round(float(shares[index]) * 100 / total, 4)
    palette_path.write_text(json.dumps(report, indent=2) + "\n")
    overlay_path = subject_dir / CLEANUP_CHANGES_OVERLAY
    if not changed:
        overlay_path.unlink(missing_ok=True)
    if changed:
        write_changes_overlay(overlay_path, bundle.labels, stages["after_removal"], cleaned)
        bundle.labels = cleaned
        bundle.protected_features = protected
        tick(0.8, "Writing cleaned segmentation")
        _write_segmentation(segmentation_path, bundle)
        flattened = Image.fromarray(bundle.palette_rgb[cleaned].astype(np.uint8), "RGB")
        flattened.save(subject_dir / "flattened.png", compress_level=1)
        tick(0.9, "Rendering palette report")
        legend = sorted(palette, key=lambda row: -float(row["share_percent"]))
        create_report(flattened, legend).save(subject_dir / "report.png", compress_level=1)
    audit = {
        "raster_dimensions": [width, height],
        "mode": "physical" if resolved.width_mm is not None else "pixels",
        "physical_dimensions_mm": ([resolved.width_mm, resolved.height_mm]
                                   if resolved.width_mm is not None else None),
        "settings": asdict(settings),
        "resolved_settings": asdict(resolved),
        "omitted_colour": palette[omitted] if omitted is not None else None,
        "changed_pixels": changed,
        "cleanup": summary,
    }
    (subject_dir / CLEANUP_AUDIT).write_text(json.dumps(audit, indent=2) + "\n")
    tick(1.0, "Complete")
    return audit
