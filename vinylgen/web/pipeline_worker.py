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

from vinylgen.contracts import PaletteSettings, TraceSettings


def _emit(value: float, message: str) -> None:
    sys.stdout.write(json.dumps({"type": "progress", "value": value, "message": message}) + "\n")
    sys.stdout.flush()


def _run(spec: dict) -> None:
    op = spec["op"]
    if op == "palette":
        from vinylgen.palette import infer_palette

        infer_palette(Path(spec["source"]), Path(spec["outputs"]),
                      PaletteSettings(**spec["settings"]), progress=_emit)
    elif op == "trace":
        from vinylgen.tracing import trace_subject

        trace_subject(Path(spec["subject"]), TraceSettings(**spec["settings"]),
                      spec["colour_names"], progress=_emit)
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
