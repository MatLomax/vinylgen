"""Trace synthetic fidelity cases through the real stages and summarise their accuracy.

Writes .tmp/fidelity/sweep-<timestamp>/ (or --out): cases.jsonl (one row per case
and tier: timing, the flat summary numbers and the full measure result),
summary.json and summary.md (per tier, per family, tag and size: Hausdorff
median/p95/max, mean boundary distance, misassigned area, corner recall, kinks,
node ratio to ideal, topology mismatches) and, unless --discard-traces, each
case's traced subject directory under work/.

Examples:
  .venv/bin/python scripts/fidelity_sweep.py --fixed
  .venv/bin/python scripts/fidelity_sweep.py --grid --fuzz 0:500 --tier both
  .venv/bin/python scripts/fidelity_sweep.py --fixed --sizes 64 256 --set corner_threshold=45
  .venv/bin/python scripts/fidelity_sweep.py --fixed --config trace.json --tag star
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vinylgen.fidelity import sweep  # noqa: E402


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    cases = p.add_argument_group("case selection (union of the sets chosen, then filtered)")
    cases.add_argument("--fixed", action="store_true", help="the fixed regression set")
    cases.add_argument("--grid", action="store_true", help="the full family grid")
    cases.add_argument("--palette-stage", action="store_true", help="the palette-stage RGB set")
    cases.add_argument("--fuzz", metavar="START:STOP", help="fuzzer seeds START..STOP-1")
    cases.add_argument("--fuzz-size", type=int, default=512,
                       help="longer side of fuzzer cases in px (default 512)")
    cases.add_argument("--sizes", type=int, nargs="+", metavar="PX",
                       help="keep only cases whose longer side is one of these")
    cases.add_argument("--tag", action="append", default=[],
                       help="keep only cases carrying this tag (repeatable: any of them)")
    p.add_argument("--tier", choices=("labels", "rgb", "both"), default="labels",
                   help="labels: label map straight into the trace stage; rgb: anti-aliased "
                        "render through the palette stage first; both (default labels)")
    p.add_argument("--config", type=Path,
                   help="trace settings file, read exactly as svg_trace.py --config reads it")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="a trace setting, as the svg_trace.py flag of that name (repeatable; "
                        "e.g. corner_threshold=45, nodes_at_bends=false)")
    p.add_argument("--jobs", type=int, default=sweep.default_jobs(),
                   help="worker processes (default: CPU count; 1 runs in process)")
    p.add_argument("--out", type=Path, help="output directory (default .tmp/fidelity/sweep-<timestamp>)")
    p.add_argument("--discard-traces", action="store_true",
                   help="delete each case's traced subject directory after measuring it")
    return p


def main() -> None:
    args = parser().parse_args()
    if args.out is not None and args.out.exists() and any(args.out.iterdir()):
        print(f"error: {args.out} is not empty; choose a new --out", file=sys.stderr)
        raise SystemExit(2)
    selection = {"fixed": args.fixed, "grid": args.grid, "palette_stage": args.palette_stage,
                 "fuzz": args.fuzz, "fuzz_size": args.fuzz_size, "sizes": args.sizes,
                 "tags": args.tag, "tier": args.tier}
    pool = sweep.make_pool(args.jobs)
    try:
        try:
            settings, cleanup = sweep.settings_from_options(args.config, args.set)
            cases = sweep.select_cases(
                fixed=args.fixed, grid=args.grid, palette_stage=args.palette_stage,
                fuzz=sweep.parse_range(args.fuzz) if args.fuzz else None,
                fuzz_size=args.fuzz_size, sizes=args.sizes, tags=args.tag, pool=pool)
        except (ValueError, ImportError) as error:
            print(f"error: {error}", file=sys.stderr)
            raise SystemExit(2)
        if not cases:
            print("error: no cases selected (choose --fixed, --grid, --palette-stage or --fuzz)",
                  file=sys.stderr)
            raise SystemExit(2)
        tiers = list(sweep.TIERS) if args.tier == "both" else [args.tier]
        out = args.out or sweep.new_run_dir("sweep")
        out.mkdir(parents=True, exist_ok=True)
        total = len(cases) * len(tiers)
        done = [0]
        # Rows stream to cases.jsonl as they finish, so an interrupted sweep keeps
        # everything measured so far; write_sweep rewrites it in case order.
        partial = (out / "cases.jsonl").open("w")

        def progress(row):
            done[0] += 1
            partial.write(json.dumps(row) + "\n")
            partial.flush()
            status = "ok" if row["ok"] else f"FAILED {row['error']}"
            print(f"[{done[0]}/{total}] {row['case_id']} {row['tier']} {row['seconds']:.2f}s "
                  f"{status}", flush=True)

        started = time.perf_counter()
        try:
            rows = sweep.run_cases(cases, tiers, settings, out / "work", cleanup=cleanup,
                                   jobs=args.jobs, pool=pool, keep=not args.discard_traces,
                                   on_row=progress)
        finally:
            partial.close()
    finally:
        if pool is not None:
            pool.shutdown()
    wall = time.perf_counter() - started
    if args.discard_traces:
        shutil.rmtree(out / "work", ignore_errors=True)
    sweep.write_sweep(out, rows, {"cases": len(cases), "tiers": tiers, "jobs": args.jobs,
                                  "wall_seconds": wall, "selection": selection,
                                  "code_hash": sweep.code_hash(),
                                  "settings_changed": sweep.settings_changed(settings),
                                  "settings": sweep.asdict(settings),
                                  "cleanup": sweep.asdict(cleanup)})
    failed = sum(1 for row in rows if not row["ok"])
    print(f"{total} traces in {wall:.1f}s, {failed} failed; summary: {out / 'summary.md'}")


if __name__ == "__main__":
    main()
