from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

from vinylgen.contracts import (
    CURVE_FEATURE_SCALE_RANGE,
    DEFAULT_MAX_DEVIATION_PER_MILLE,
    LENGTH_UNIT_PER_MILLE,
    LENGTH_UNIT_PX,
    PER_MILLE_DEFAULTS,
    PX_DEFAULTS,
    TRACE_LENGTH_FIELDS,
    CleanupSettings,
    TraceSettings,
    upgrade_trace_record,
)
from vinylgen.tracing import normalize_hex, resolve_subjects, trace_subject


# Every trace length flag is in thousandths of the traced image's longer side.
_UNITS = (" in thousandths of the traced image's longer side (so 1 is 2 px on a 2000 px "
          "image)")


def _default(name: str) -> str:
    return f"{PER_MILLE_DEFAULTS[name]:g}"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="Trace lengths (curve-fit tolerance, straight-line deviation, minimum straight "
               "length, maximum deviation, side smoothness, corner sharpening allowance, corner "
               "analysis span and curve feature scale) are in thousandths of each traced image's longer side. A --config "
               "must name the unit of any lengths it sets: \"length_unit\": \"per_mille\", or "
               "\"px\" for pixels (the unit of configs written before units), which are "
               "converted on each image. A whole trace.json recorded before units is pixels.")
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
                   help=f"Bezier fit tolerance{_UNITS} (default {_default('curve_fit_tolerance')})")
    p.add_argument("--straight-line-max-deviation", type=float,
                   help=f"how far{_UNITS} a run may stray from a line and still become one straight "
                        f"segment (default {_default('straight_line_max_deviation')}). 0 turns this "
                        "shortcut off, but pieces bowing less than --curve-fit-tolerance can still "
                        "become lines; lower that to keep them curved. Without --side-smoothness, "
                        "corner detection follows this value too, so 0 also disables most corners")
    p.add_argument("--minimum-straight-length", type=float,
                   help=f"shortest run{_UNITS} eligible to become straight "
                        f"(default {_default('minimum_straight_length')})")
    p.add_argument("--max-deviation", type=float,
                   help=f"maximum deviation{_UNITS} (default {DEFAULT_MAX_DEVIATION_PER_MILLE:g}). Caps the "
                        "curve-fit tolerance and the straight-line deviation (fitting uses the smaller, so "
                        "only a lower value changes the fit) and sets the post-fit audit threshold")
    p.add_argument("--side-smoothness", "--corner-straightness", dest="side_smoothness", type=float,
                   help=f"how far{_UNITS} the edge on either side of a corner may stray from a smooth "
                        "arc (a straight line or a circular curve) and still confirm a hard corner. "
                        "Defaults to --straight-line-max-deviation, so set it (e.g. "
                        f"{_default('straight_line_max_deviation')}) when that is 0 to keep corners. "
                        "--corner-straightness is its former name")
    p.add_argument("--corner-sharpening-allowance", type=float,
                   help=f"how far{_UNITS} a sharpened corner may lie from the drawn edge: a corner "
                        "becomes the point where its two sides meet only within this distance, so "
                        "roundings up to about 2.4 times it snap sharp and larger ones stay round. "
                        "Defaults to --max-deviation")
    p.add_argument("--corner-analysis-span", type=float,
                   help=f"analysis scale{_UNITS}: the length of edge checked on each side of a possible "
                        f"corner, which tells raster steps from intentional corners "
                        f"(default {_default('corner_analysis_span')})")
    p.add_argument("--straight-line-snap", choices=("off", "axis", "axis_diagonal"),
                   help="snap straight lines within --snap-angle of horizontal or vertical (axis, "
                        "the default), also of 45 degrees (axis_diagonal), or not at all (off); a "
                        "snapped line stays within --straight-line-max-deviation of its pixels")
    p.add_argument("--snap-angle", type=float,
                   help="degrees a straight line may be off an allowed direction and still snap "
                        "(0.5 to 10, default 2)")
    p.add_argument("--node-placement", choices=("prefer_extremes", "at_extremes", "fewest"),
                   help="where curve nodes go: the fewest nodes, preferring horizontal and vertical "
                        "extremes (prefer_extremes, the default); a node at every extreme "
                        "(at_extremes); or the fewest nodes with no preference (fewest)")
    p.add_argument("--nodes-at-bends", action=argparse.BooleanOptionalAction, default=None,
                   help="offer the tightest point of each bend as a node position (default on)")
    p.add_argument("--curve-feature-scale", type=float,
                   help=f"length of edge{_UNITS} smoothed over before looking for node positions, so "
                        "pixel steps and wiggles smaller than this are ignored "
                        f"({CURVE_FEATURE_SCALE_RANGE[0]:g} to {CURVE_FEATURE_SCALE_RANGE[1]:g}, "
                        f"default {_default('curve_feature_scale')})")
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
    ``curve_optimization`` flag, which only pixel-era settings have, so without a
    ``length_unit`` it is pixels. ``false`` keeps the recorded corners by pinning
    ``side_smoothness`` to the recorded straight-line deviation, then sets the
    deviation to 0; the flag is dropped in every case. Renamed fields
    (``corner_straightness``) are read under their current names."""
    values = upgrade_trace_record(values)
    if "curve_optimization" not in values:
        return values
    values.setdefault("length_unit", LENGTH_UNIT_PX)
    if values.pop("curve_optimization") is False:
        if values.get("side_smoothness") is None:
            values["side_smoothness"] = values.get(
                "straight_line_max_deviation", PX_DEFAULTS["straight_line_max_deviation"]
                if values["length_unit"] == LENGTH_UNIT_PX
                else PER_MILLE_DEFAULTS["straight_line_max_deviation"])
        values["straight_line_max_deviation"] = 0.0
    return values


_TRACE_FIELDS = {f.name for f in fields(TraceSettings)}
_CLEANUP_FIELDS = {f.name for f in fields(CleanupSettings)}


def config_values(config: dict) -> dict:
    """Flatten a config file into one block of trace and cleanup fields.

    The unit of its trace lengths is explicit: the block's ``length_unit``, else
    pixels for a whole trace.json (every one written before units recorded pixels)
    or a block with the pixel-era ``curve_optimization`` flag. Any other block that
    sets a length without naming its unit is rejected, never guessed."""
    whole_trace = isinstance(config.get("settings"), dict)
    if whole_trace:
        config = {**config["settings"], "cleanup_settings": config.get("cleanup_settings")}
        config.setdefault("length_unit", LENGTH_UNIT_PX)
    values = dict(config)
    cleanup = values.pop("cleanup_settings", None)
    if cleanup is not None:
        values = {**cleanup, **values}
    values = upgrade_legacy_config(values)
    lengths = sorted(name for name in TRACE_LENGTH_FIELDS if values.get(name) is not None)
    if lengths and "length_unit" not in values:
        raise ValueError(
            f"the config sets {', '.join(lengths)} without length_unit: add \"length_unit\": "
            "\"per_mille\" if they are thousandths of the image's longer side, or \"px\" if they "
            "are pixels (the unit of configs written before units), which are converted on each image")
    return values


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
    flagged = sorted(name for name in TRACE_LENGTH_FIELDS if mapping.get(name) is not None)
    if flagged and values.get("length_unit", LENGTH_UNIT_PER_MILLE) != LENGTH_UNIT_PER_MILLE:
        raise ValueError(
            f"--{flagged[0].replace('_', '-')} is in thousandths of the image's longer side, but the "
            "config's lengths are pixels; set the value in the config (in px) instead")
    if mapping.get("curve_feature_scale") is not None:
        low, high = CURVE_FEATURE_SCALE_RANGE
        if not low <= mapping["curve_feature_scale"] <= high:
            raise ValueError(f"--curve-feature-scale must be between {low:g} and {high:g}")
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
