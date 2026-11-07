from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

from vinylgen.contracts import CleanupSettings, TraceSettings, upgrade_trace_record
from vinylgen.tracing import normalize_hex, resolve_subjects, t3_setting, trace_subject

# What ``--half-arc``, ``--half-ellipse`` and ``--half-cubic`` parse ``off`` to: the kind is
# never drawn (``None`` in ``TraceSettings``). A flag left out parses to ``None`` and leaves the
# config's or the default value.
OFF = "off"


def half_width(text: str) -> float:
    """A ``--half-<kind>`` value: a half-width in pixels, > 0."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"a half-width must be > 0, got {text}")
    return value


def half_width_or_off(text: str) -> float | str:
    """A ``--half-<kind>`` value for an optional kind: a half-width > 0, or ``off``."""
    return OFF if text == OFF else half_width(text)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="t3 traces within each curve kind's tube half-width (--half-line, --half-arc, "
               "--half-ellipse, --half-cubic; 1.75 px each by default); A1 = 22.5, A2 = 45 and "
               "L = 4 are fixed. A --config may set the half-widths (half_line, half_arc, "
               "half_ellipse, half_cubic; null is off), the soft node limits (soft_path_nodes, "
               "soft_layer_nodes, soft_document_nodes), which only warn, and the cleanup fields; "
               "an older config's retired trace fields are dropped, and one without the "
               "half-widths traces at the defaults.")
    p.add_argument("inputs", nargs="+", type=Path, help="subject output directory, or outputs directory")
    p.add_argument("--config", type=Path)
    p.add_argument("--width-mm", type=float)
    p.add_argument("--height-mm", type=float)
    p.add_argument("--remove-colour")
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
    half_help = ("the furthest, in px, a drawn {} may lie from its edge. Higher lets this kind "
                 "cover longer stretches, looser; lower holds it tighter to the pixels.")
    p.add_argument("--half-line", type=half_width, metavar="PX",
                   help=half_help.format("line") + " Below about 0.55 px (sampled) long shallow "
                        "straight edges break into several lines. The line is never off.")
    for kind, name in (("arc", "circular arc"), ("ellipse", "elliptical arc"),
                       ("cubic", "cubic curve")):
        p.add_argument(f"--half-{kind}", type=half_width_or_off, metavar="PX|off",
                       help=half_help.format(name) + " off: this kind is never used.")
    return p


def upgrade_legacy_config(values: dict) -> dict:
    """A config (e.g. an old trace.json settings block) read without the retired trace
    fields (``contracts.RETIRED_TRACE_FIELDS``) and without the pixel-era
    ``curve_optimization`` flag, which steered the old fitter only."""
    values = upgrade_trace_record(values)
    values.pop("curve_optimization", None)
    return values


_TRACE_FIELDS = {f.name for f in fields(TraceSettings)}
_CLEANUP_FIELDS = {f.name for f in fields(CleanupSettings)}


def config_values(config: dict) -> dict:
    """Flatten a config file into one block of trace and cleanup fields: a whole
    trace.json's ``settings`` and ``cleanup_settings`` merged, or a block carrying a
    ``cleanup_settings`` object, with the retired fields dropped."""
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
            values[key] = None if key.startswith("half_") and value == OFF else value
    settings = TraceSettings(**{k: v for k, v in values.items() if k in _TRACE_FIELDS})
    t3_setting(settings)  # a table t3 refuses (from a config) raises ValueError here
    cleanup = CleanupSettings(**{k: v for k, v in values.items() if k in _CLEANUP_FIELDS})
    if cleanup.width_mm is not None and cleanup.width_mm <= 0 or cleanup.height_mm is not None and cleanup.height_mm <= 0:
        raise ValueError("physical dimensions must be positive")
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
        print(json.dumps({r["subject"]: {"valid": r["validation"]["edge"]["passed"],
                                         "warnings": len(r["warnings"])} for r in reports}, indent=2))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
