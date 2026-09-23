from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

from vinylgen.contracts import CleanupSettings, TraceSettings
from vinylgen.tracing import normalize_hex, resolve_subjects, trace_subject


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", type=Path, help="subject output directory, or outputs directory")
    p.add_argument("--config", type=Path)
    p.add_argument("--width-mm", type=float)
    p.add_argument("--height-mm", type=float)
    p.add_argument("--overlap", type=float,
                   help="lower-layer overlap in mm with a physical size, else px")
    p.add_argument("--solid-underlayers", action="store_true", default=None,
                   help="make each lower layer a solid stacked shape under everything above it, so no cut edges are shared")
    p.add_argument("--remove-colour")
    p.add_argument("--layer-order", help="comma-separated bottom-to-top colour names or hex values")
    p.add_argument("--corner-threshold", type=float,
                   help="minimum corner turn in degrees before a bend becomes a hard corner (default 60)")
    p.add_argument("--curve-fit-tolerance", type=float,
                   help="Bezier fit tolerance in px of the traced image (default 1.5)")
    p.add_argument("--straight-line-max-deviation", type=float,
                   help="px a run may stray from a line and still become one straight segment "
                        "(default 1.6). 0 turns this shortcut off, but pieces bowing less than "
                        "--curve-fit-tolerance can still become lines; lower that to keep them curved. "
                        "Without --corner-straightness, corner detection follows this value too, so 0 "
                        "also disables most corners")
    p.add_argument("--minimum-straight-length", type=float,
                   help="shortest run in px eligible to become straight (default 4)")
    p.add_argument("--max-deviation", type=float,
                   help="px of the traced image (default 2). Caps the curve-fit tolerance (fitting uses the "
                        "smaller of the two, so only a lower value changes curves), sets the post-fit audit "
                        "threshold, and limits how far a corner may move. It does not limit straight lines")
    p.add_argument("--corner-straightness", type=float,
                   help="px the runs either side of a corner may stray from a line and still confirm "
                        "a hard corner. Defaults to --straight-line-max-deviation, so set it (e.g. 1.6) "
                        "when that is 0 to keep corners")
    p.add_argument("--corner-analysis-span", type=float,
                   help="distance in px used to distinguish raster steps from intentional corners")
    p.add_argument("--cleanup", action="store_true", default=None)
    p.add_argument("--cleanup-action", choices=("remove", "widen", "remove_then_widen"))
    p.add_argument("--min-detail-width", type=float,
                   help="details narrower than this, in mm with a physical size, else px, are removed "
                        "(remove) or widened (widen, remove_then_widen)")
    p.add_argument("--min-region-area", type=float,
                   help="remove regions smaller than this, in mm^2 with a physical size, else px^2")
    p.add_argument("--remove-below-width", type=float,
                   help="with remove_then_widen, remove features below this width (mm with a physical size, "
                        "else px) before widening")
    p.add_argument("--colour-name", action="append", default=[], metavar="HEX=NAME")
    return p


def upgrade_legacy_config(values: dict) -> dict:
    """Accept a config (e.g. an old trace.json settings block) carrying the
    ``curve_optimization`` flag. ``false`` keeps the recorded corners by pinning
    ``corner_straightness`` to the recorded straight-line deviation, then sets the
    deviation to 0; the flag is dropped in every case."""
    values = dict(values)
    if "curve_optimization" not in values:
        return values
    if values.pop("curve_optimization") is False:
        if values.get("corner_straightness") is None:
            values["corner_straightness"] = values.get(
                "straight_line_max_deviation", TraceSettings.straight_line_max_deviation)
        values["straight_line_max_deviation"] = 0.0
    return values


_TRACE_FIELDS = {f.name for f in fields(TraceSettings)}
_CLEANUP_FIELDS = {f.name for f in fields(CleanupSettings)}


def config_values(config: dict) -> dict:
    """Flatten a config file into one block of trace and cleanup fields."""
    if isinstance(config.get("settings"), dict):
        config = {**config["settings"], "cleanup_settings": config.get("cleanup_settings")}
    values = dict(config)
    cleanup = values.pop("cleanup_settings", None)
    if cleanup is not None:
        values = {**cleanup, **values}
    return upgrade_legacy_config(values)


def settings_from_args(args: argparse.Namespace
                       ) -> tuple[TraceSettings, CleanupSettings, dict[str, str]]:
    """The trace settings, the cleanup settings (size, removed colour and cutter
    cleanup, applied inline before tracing) and the colour-name overrides.

    A config file is one flat block that may mix both groups' fields (as an older
    trace.json ``settings`` block does), a block carrying a ``cleanup_settings``
    object, or a whole trace.json, whose ``settings`` and ``cleanup_settings``
    are merged so a traced subject's configuration round-trips. Flags override it."""
    values = {}
    if args.config:
        values = config_values(json.loads(args.config.read_text()))
        unknown = set(values) - _TRACE_FIELDS - _CLEANUP_FIELDS
        if unknown:
            raise ValueError(f"unknown config setting(s): {', '.join(sorted(unknown))}")
    mapping = {name: getattr(args, name, None) for name in _TRACE_FIELDS | _CLEANUP_FIELDS}
    for key, value in mapping.items():
        if value is not None:
            values[key] = value
    if args.layer_order is not None:
        values["layer_order"] = [x.strip() for x in args.layer_order.split(",") if x.strip()]
    settings = TraceSettings(**{k: v for k, v in values.items() if k in _TRACE_FIELDS})
    cleanup = CleanupSettings(**{k: v for k, v in values.items() if k in _CLEANUP_FIELDS})
    if cleanup.width_mm is not None and cleanup.width_mm <= 0 or cleanup.height_mm is not None and cleanup.height_mm <= 0:
        raise ValueError("physical dimensions must be positive")
    if settings.overlap < 0:
        raise ValueError("overlap must be non-negative")
    if cleanup.remove_below_width < 0:
        raise ValueError("remove-below width must be non-negative")
    overrides = {}
    for item in args.colour_name:
        if "=" not in item:
            raise ValueError("--colour-name must be HEX=NAME")
        key, name = item.split("=", 1)
        overrides[normalize_hex(key)] = name.strip().title()
    return settings, cleanup, overrides


def main() -> None:
    args = parser().parse_args()
    try:
        settings, cleanup, overrides = settings_from_args(args)
        reports = [trace_subject(path, TraceSettings(**asdict(settings)), overrides,
                                 cleanup=CleanupSettings(**asdict(cleanup)), clean_inline=True)
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
