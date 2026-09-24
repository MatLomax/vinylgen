"""Subprocess entrypoint that runs one cancellable pipeline compute step.

The parent copies inputs into a scratch directory, launches this worker in its own
process group, and relays the progress lines it prints on stdout. Because the heavy
numpy work runs here rather than on a parent thread, cancelling the job kills this
process group and stops the work immediately; the parent commits results only after a
clean exit, so a kill can never leave a half-written revision.

Protocol: the parent writes one JSON spec object to stdin. The worker prints newline
delimited JSON events to stdout — ``{"type": "progress", "value": f, "message": s}``
during the run, then ``{"type": "done"}`` on success or ``{"type": "error",
"message": s}`` on failure — and exits non-zero when it fails.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from vinylgen.contracts import CleanupSettings, PaletteSettings, TraceSettings

# The Flatten canvas's "Protected areas" overlay: a semi-transparent green over each
# pixel a protected_component_points pick protected, clear elsewhere. Green stays
# distinct from the red missed-details and cyan tightened-boundary trace overlays.
PROTECTED_AREAS_RGBA = (40, 220, 120, 150)
PROTECTED_AREAS_OVERLAY = "protected-areas-overlay.png"


def _emit(value: float, message: str) -> None:
    sys.stdout.write(json.dumps({"type": "progress", "value": value, "message": message}) + "\n")
    sys.stdout.flush()


def write_protected_areas_overlay(subject_dir: Path) -> bool:
    """Write ``protected-areas-overlay.png`` for the subject's segmentation, on its grid.

    The overlay marks every ``explicit_protected_features`` pixel. It is written only
    when that mask has a true pixel; otherwise any earlier file is removed, so a run
    that protected nothing records no overlay. Returns whether it was written.
    """
    from PIL import Image

    target = subject_dir / PROTECTED_AREAS_OVERLAY
    # Only the boolean mask member is decompressed, never the palette-cost volume.
    with np.load(subject_dir / "segmentation.npz", allow_pickle=False) as data:
        mask = data["explicit_protected_features"].astype(bool)
    if not mask.any():
        target.unlink(missing_ok=True)
        return False
    # A 1-bit paletted PNG (index 0 clear, index 1 the overlay colour at its alpha) keeps
    # a gigapixel mask to one byte per pixel in memory and far less on disk than RGBA.
    overlay = Image.fromarray(np.ascontiguousarray(mask).view(np.uint8))
    overlay.putpalette([0, 0, 0, *PROTECTED_AREAS_RGBA[:3]])
    overlay.save(target, transparency=bytes([0, PROTECTED_AREAS_RGBA[3]]), bits=1,
                 compress_level=1)
    return True


def _run(spec: dict) -> None:
    op = spec["op"]
    if op == "palette":
        from vinylgen.palette import infer_palette

        source = Path(spec["source"])
        infer_palette(source, Path(spec["outputs"]),
                      PaletteSettings(**spec["settings"]), progress=_emit)
        write_protected_areas_overlay(Path(spec["outputs"]) / source.stem)
    elif op == "cleanup":
        from vinylgen.web.cleanup import clean_subject

        clean_subject(Path(spec["subject"]), CleanupSettings(**spec["settings"]), progress=_emit)
    elif op == "trace":
        from vinylgen.tracing import trace_subject

        cleanup = spec.get("cleanup")
        trace_subject(Path(spec["subject"]), TraceSettings(**spec["settings"]),
                      spec["colour_names"], progress=_emit,
                      cleanup=CleanupSettings(**cleanup) if cleanup is not None else None)
    else:
        raise ValueError(f"unknown pipeline worker op: {op}")


def main() -> int:
    spec = json.loads(sys.stdin.read())
    try:
        _run(spec)
    except Exception as exc:  # relayed to the parent, which fails the job
        sys.stdout.write(json.dumps({"type": "error", "message": str(exc)}) + "\n")
        sys.stdout.flush()
        return 1
    sys.stdout.write(json.dumps({"type": "done"}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
