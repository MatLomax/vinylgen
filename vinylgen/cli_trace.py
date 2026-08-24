from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

from vinylgen.contracts import TraceSettings
from vinylgen.tracing import normalize_hex, resolve_subjects, trace_subject


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", type=Path, help="subject output directory, or outputs directory")
    p.add_argument("--config", type=Path)
    p.add_argument("--width-mm", type=float)
    p.add_argument("--height-mm", type=float)
    p.add_argument("--overlap", type=float)
    p.add_argument("--solid-underlayers", action="store_true", default=None,
                   help="make each lower layer a solid stacked shape under everything above it, so no cut edges are shared")
    p.add_argument("--remove-colour")
    p.add_argument("--layer-order", help="comma-separated bottom-to-top colour names or hex values")
    p.add_argument("--corner-threshold", type=float,
                   help="minimum corner turn in degrees before a bend becomes a hard corner (default 57.3)")
    p.add_argument("--curve-fit-tolerance", type=float)
    p.add_argument("--straight-line-max-deviation", type=float)
    p.add_argument("--minimum-straight-length", type=float)
    p.add_argument("--no-curve-optimization", action="store_true", default=None)
    p.add_argument("--max-deviation", type=float)
    p.add_argument("--corner-analysis-span", type=float,
                   help="distance used to distinguish raster steps from intentional corners")
    p.add_argument("--cleanup", action="store_true", default=None)
    p.add_argument("--cleanup-action", choices=("remove", "widen", "remove_then_widen"))
    p.add_argument("--min-detail-width", type=float)
    p.add_argument("--min-region-area", type=float)
    p.add_argument("--remove-below-width", type=float,
                   help="with remove_then_widen, remove features below this width before widening")
    p.add_argument("--colour-name", action="append", default=[], metavar="HEX=NAME")
    return p


def settings_from_args(args: argparse.Namespace) -> tuple[TraceSettings, dict[str, str]]:
    values = {}
    if args.config:
        values = json.loads(args.config.read_text())
        unknown = set(values) - {f.name for f in fields(TraceSettings)}
        if unknown:
            raise ValueError(f"unknown config setting(s): {', '.join(sorted(unknown))}")
    mapping = {f.name: getattr(args, f.name, None) for f in fields(TraceSettings)}
    for key, value in mapping.items():
        if value is not None:
            values[key] = value
    if args.no_curve_optimization is not None:
        values["curve_optimization"] = not args.no_curve_optimization
    if args.layer_order is not None:
        values["layer_order"] = [x.strip() for x in args.layer_order.split(",") if x.strip()]
    settings = TraceSettings(**values)
    if settings.width_mm is not None and settings.width_mm <= 0 or settings.height_mm is not None and settings.height_mm <= 0:
        raise ValueError("physical dimensions must be positive")
    if settings.overlap < 0:
        raise ValueError("overlap must be non-negative")
    if settings.remove_below_width < 0:
        raise ValueError("remove-below width must be non-negative")
    overrides = {}
    for item in args.colour_name:
        if "=" not in item:
            raise ValueError("--colour-name must be HEX=NAME")
        key, name = item.split("=", 1)
        overrides[normalize_hex(key)] = name.strip().title()
    return settings, overrides


def main() -> None:
    args = parser().parse_args()
    try:
        settings, overrides = settings_from_args(args)
        reports = [trace_subject(path, TraceSettings(**asdict(settings)), overrides)
                   for path in resolve_subjects(args.inputs)]
        print(json.dumps({r["subject"]: {"valid": all(
                                                    r["validation"][variant]["passed"]
                                                    for variant in ("edge", "overlap")),
                                                "warnings": len(r["warnings"])} for r in reports}, indent=2))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
