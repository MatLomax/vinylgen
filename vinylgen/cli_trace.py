from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

from vinylgen.contracts import CleanupSettings, TraceSettings, upgrade_trace_record
from vinylgen.tracing import normalize_hex, resolve_subjects, trace_subject


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="t3 traces at its one fixed setting (w = 3.5, A1 = 22.5, A2 = 45, L = 4), so no "
               "option changes the fit. A --config may set the soft node limits "
               "(soft_path_nodes, soft_layer_nodes, soft_document_nodes), which only warn, and "
               "the cleanup fields; an older config's retired trace fields are dropped.")
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
            values[key] = value
    settings = TraceSettings(**{k: v for k, v in values.items() if k in _TRACE_FIELDS})
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
