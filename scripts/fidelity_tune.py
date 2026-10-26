"""Search the trace lengths against synthetic truth and PROPOSE a set.

The tuner never edits TraceSettings or any source: it iterates on its own copy of
the settings and ends with a proposal. Defaults change only through a reviewed
commit. See vinylgen/fidelity/tune.py for the objective and the search.

Writes .tmp/fidelity/tune-<timestamp>/ (or --out, which must not hold a run):
  run.json            the run's configuration (read back by --resume)
  evaluations.jsonl   every evaluated settings set: values, E, N, J, per-case numbers
  pareto.json         the accuracy/node-count front, the proposal and the current defaults
  pareto.png          the trade-off plot
  proposed-trace.json the proposal as an svg_trace.py --config
  held-out/{current,proposed}/  sweeps of the held-out cases (cases.jsonl, summary.*):
                      fuzz seeds outside the training range, at every held-out size,
                      and analytic grid cases outside the fixed set
  held-out/families.json  held-out scores per family and per canvas size
  real-images/<subject>/{palette,current,proposed}/  the images/*.png subjects, palette
                      stage once, traced with each set (traced.png previews)
  real-images/contact.png   flattened | current | proposed, one row per subject
  report.md           the proposed diff, the front, held-out before/after, real-image table

"Current" is always the shipped TraceSettings() defaults; a --config/--set base only
moves where the search starts, and its changes appear in the proposed diff.

Examples:
  .venv/bin/python scripts/fidelity_tune.py
  .venv/bin/python scripts/fidelity_tune.py --max-evals 6 --cases-per-eval 8 --sizes 128 \\
      --fuzz-sizes 256 --held-out-count 24 --held-out-grid-count 24 --held-out-grid-sizes 64 128
  .venv/bin/python scripts/fidelity_tune.py --resume .tmp/fidelity/tune-20260926-120000 --max-evals 120
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vinylgen.contracts import CleanupSettings, TraceSettings, upgrade_trace_record  # noqa: E402
from vinylgen.fidelity import sweep, tune  # noqa: E402

DEFAULT_CASES_PER_EVAL = 48
DEFAULT_HELD_OUT = 200
DEFAULT_FUZZ_SIZES = (512, 1536)
# Held-out fuzz seeds also run on canvases the training fuzz never uses.
DEFAULT_HELD_OUT_FUZZ_SIZES = (256, 512, 1024, 1536)
DEFAULT_HELD_OUT_GRID = 150
# Every scaled grid size below 2048 px, and the fixed-px canvas: small canvases
# (64 and 256 px) the fixed training set never uses included.
DEFAULT_HELD_OUT_GRID_SIZES = (64, 128, 256, 512, 576, 1024, 1536)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-evals", type=int, default=60,
                   help="evaluation budget, counting evaluations resumed from disk (default 60)")
    p.add_argument("--cases-per-eval", type=int, default=DEFAULT_CASES_PER_EVAL,
                   help="size of the fixed random training subset every evaluation traces "
                        f"(default {DEFAULT_CASES_PER_EVAL}; 0 means the whole training pool)")
    p.add_argument("--subset-seed", type=int, default=0, help="seed of that subset (default 0)")
    p.add_argument("--node-weight", type=float, default=tune.DEFAULT_NODE_WEIGHT,
                   help="w in J = E + w log2 N: error units one doubling of the node excess "
                        f"costs (default {tune.DEFAULT_NODE_WEIGHT:g})")
    p.add_argument("--tier", choices=("labels", "rgb", "both"), default="labels",
                   help="harness tier the objective traces (default labels)")
    p.add_argument("--train", metavar="START:STOP",
                   help="training fuzz seeds (default: tuning.train in tests/synthetic/fuzz-seeds.json)")
    p.add_argument("--held-out", metavar="START:STOP",
                   help="held-out fuzz seeds (default: tuning.held_out in fuzz-seeds.json)")
    p.add_argument("--held-out-count", type=int, default=DEFAULT_HELD_OUT,
                   help=f"use the first N held-out fuzz seeds (default {DEFAULT_HELD_OUT}; with "
                        "--held-out-grid-count 0 as well, 0 skips the held-out table)")
    p.add_argument("--fuzz-sizes", type=int, nargs="+", default=list(DEFAULT_FUZZ_SIZES),
                   metavar="PX", help="canvas sizes of training fuzzer cases; each seed gets "
                                      "one, fixed by the seed (default 512 1536)")
    p.add_argument("--held-out-fuzz-sizes", type=int, nargs="+",
                   default=list(DEFAULT_HELD_OUT_FUZZ_SIZES), metavar="PX",
                   help="canvas sizes of held-out fuzzer cases, one per seed as for training "
                        "(default 256 512 1024 1536)")
    p.add_argument("--held-out-grid-count", type=int, default=DEFAULT_HELD_OUT_GRID,
                   help="analytic grid cases outside the fixed set in the held-out evaluation, "
                        "stratified over family and size (default "
                        f"{DEFAULT_HELD_OUT_GRID}; 0 skips them)")
    p.add_argument("--held-out-grid-sizes", type=int, nargs="+",
                   default=list(DEFAULT_HELD_OUT_GRID_SIZES), metavar="PX",
                   help="canvas sizes (longer side) of held-out grid cases (default 64 128 256 "
                        "512 576 1024 1536)")
    p.add_argument("--sizes", type=int, nargs="+", metavar="PX",
                   help="keep only fixed-set cases whose longer side is one of these")
    p.add_argument("--no-fixed", action="store_true", help="train on fuzz seeds only")
    p.add_argument("--no-real-images", action="store_true",
                   help="skip the real-corpus before/after traces")
    p.add_argument("--config", type=Path,
                   help="base trace settings (per_mille), read as svg_trace.py --config reads it; "
                        "the search starts there")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="a base trace setting, as the svg_trace.py flag of that name (repeatable)")
    p.add_argument("--jobs", type=int, default=sweep.default_jobs(),
                   help="worker processes (default: CPU count)")
    p.add_argument("--out", type=Path, help="report directory (default .tmp/fidelity/tune-<timestamp>)")
    p.add_argument("--resume", type=Path, metavar="DIR",
                   help="continue a run: its run.json fixes the cases and settings, its "
                        "evaluations are reused; --max-evals, --jobs and --no-real-images apply")
    return p


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=sweep.ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def configure(args) -> tuple[Path, dict]:
    """The run directory and its run.json configuration (new, or read back)."""
    if args.resume:
        out = args.resume
        config = json.loads((out / "run.json").read_text())
        config.update(max_evals=args.max_evals, jobs=args.jobs)
        (out / "run.json").write_text(json.dumps(config, indent=2) + "\n")
        return out, config
    if args.out is not None and (args.out / "run.json").exists():
        raise ValueError(f"{args.out} already holds a run: pass --resume {args.out} or a new --out")
    settings, cleanup = sweep.settings_from_options(args.config, args.set)
    ranges = {}
    if args.train is None or args.held_out is None:
        ranges = tune.seed_ranges()
    train = sweep.parse_range(args.train) if args.train else ranges["train"]
    held_out = sweep.parse_range(args.held_out) if args.held_out else ranges["held_out"]
    held_out = held_out[:max(0, args.held_out_count)]
    if set(train) & set(held_out):
        raise ValueError("training and held-out seed ranges overlap")
    fixed = []
    if not args.no_fixed:
        from vinylgen.fidelity import families
        fixed = families.fixed_set()
        if args.sizes:
            fixed = [case for case in fixed if sweep.case_size(case) in set(args.sizes)]
    pool = tune.training_pool(fixed, train, args.fuzz_sizes)
    subset = tune.sample_pool(pool, args.cases_per_eval or None, args.subset_seed)
    if not subset:
        raise ValueError("the training subset is empty")
    # Held-out grid cases exclude the whole fixed set (training draws from it),
    # whether or not this run trains on it.
    from vinylgen.fidelity import families
    fixed_ids = {case.case_id for case in families.fixed_set()}
    grid = tune.held_out_grid(fixed_ids, args.held_out_grid_sizes,
                              max(0, args.held_out_grid_count), args.subset_seed)
    trained = {entry[1] for entry in subset if entry[0] == "fixed"}
    if trained & {case.case_id for case in grid}:
        raise ValueError("training and held-out grid cases overlap")
    out = args.out or sweep.new_run_dir("tune")
    out.mkdir(parents=True, exist_ok=True)
    config = {"base_settings": tune.base_record(settings), "cleanup": asdict(cleanup),
              "tiers": list(sweep.TIERS) if args.tier == "both" else [args.tier],
              "tier": args.tier, "node_weight": args.node_weight, "max_evals": args.max_evals,
              "cases_per_eval": len(subset), "subset_seed": args.subset_seed,
              "subset": subset, "fixed_count": len(fixed), "fixed_sizes": args.sizes,
              "train": [train.start, train.stop], "held_out": [held_out.start, held_out.stop],
              "fuzz_sizes": list(args.fuzz_sizes),
              "held_out_fuzz_sizes": list(args.held_out_fuzz_sizes),
              "held_out_grid": [case.case_id for case in grid],
              "held_out_grid_sizes": list(args.held_out_grid_sizes), "jobs": args.jobs,
              "code_hash": sweep.code_hash(), "git_head": git_head(),
              "case_source": ("fuzz.fuzz_case()" if not fixed
                              else "families.fixed_set() + fuzz.fuzz_case()")}
    (out / "run.json").write_text(json.dumps(config, indent=2) + "\n")
    return out, config


def training_cases(config: dict, pool) -> list:
    fixed_by_id = {}
    if any(entry[0] == "fixed" for entry in config["subset"]):
        from vinylgen.fidelity import families
        fixed_by_id = {case.case_id: case for case in families.fixed_set()}
    return tune.build_cases(config["subset"], fixed_by_id, pool=pool)


def main() -> None:
    args = parser().parse_args()
    started = time.perf_counter()
    try:
        out, config = configure(args)
    except (ValueError, ImportError, FileNotFoundError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
    record = config["base_settings"]
    cleanup = CleanupSettings(**config["cleanup"])
    current_settings = TraceSettings()
    weight = config["node_weight"]
    pool = sweep.make_pool(config["jobs"])
    try:
        cases = training_cases(config, pool)
        log(f"{len(cases)} training cases x {len(config['tiers'])} tier(s); budget "
            f"{config['max_evals']} evaluations; report in {out}")
        try:
            evaluator = tune.Evaluator(cases, config["tiers"], out, cleanup=cleanup, pool=pool,
                                       jobs=config["jobs"], node_weight=weight,
                                       max_evals=config["max_evals"], resume=bool(args.resume),
                                       log=log)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            raise SystemExit(2)
        if evaluator.evaluations:
            log(f"resumed {len(evaluator.evaluations)} evaluations from disk")
        tune.compass_search(evaluator, record)
        # The shipped defaults are always on the plot and are the "before". The
        # search starts there unless a base config moved it; then they cost one
        # evaluation beyond the budget.
        known = tune.settings_key(current_settings) in evaluator.memo
        evaluator.max_evals = None
        current_entry = evaluator(current_settings, {"phase": "current defaults"})
        evaluations = evaluator.evaluations
        points = [(entry["error"], entry["nodes"]) for entry in evaluations]
        front = tune.pareto_front(points)
        proposed_index = min(range(len(evaluations)),
                             key=lambda i: (evaluations[i]["objective"], i))
        current_index = current_entry["index"]
        proposed_settings = TraceSettings(
            **upgrade_trace_record(evaluations[proposed_index]["settings"]))
        diff = tune.settings_diff(proposed_settings, current_settings)
        weights_table = []
        for w in sorted({0.0, 0.25, 0.5, 1.0, 2.0, 4.0, weight}):
            best = min(range(len(evaluations)),
                       key=lambda i: (tune.objective(evaluations[i]["error"],
                                                     evaluations[i]["nodes"], w), i))
            weights_table.append({"weight": w, "index": best, "error": evaluations[best]["error"],
                                  "nodes": evaluations[best]["nodes"]})
        proposed_cfg = tune.proposed_config(proposed_settings, cleanup)
        (out / "proposed-trace.json").write_text(json.dumps(proposed_cfg, indent=2) + "\n")
        no_effect = tune.no_effect_parameters(evaluations)
        (out / "pareto.json").write_text(json.dumps({
            "node_weight": weight, "current_index": current_index,
            "proposed_index": proposed_index, "diff": diff, "no_effect": no_effect,
            "front": [{"index": i, "error": evaluations[i]["error"],
                       "nodes": evaluations[i]["nodes"],
                       "objective": evaluations[i]["objective"],
                       "values": evaluations[i]["values"]} for i in front],
            "best_by_weight": weights_table}, indent=2) + "\n")
        tune.pareto_plot(evaluations, front, current_index, proposed_index, out / "pareto.png")
        log(f"proposed eval {proposed_index}: {json.dumps(diff)}")

        held_out = None
        seeds = range(*config["held_out"])
        fuzz_sizes = config.get("held_out_fuzz_sizes", config["fuzz_sizes"])
        grid_ids = config.get("held_out_grid", [])
        for label in ("current", "proposed"):
            shutil.rmtree(out / "held-out" / label, ignore_errors=True)
        if len(seeds) or grid_ids:
            log(f"held-out before/after on {len(seeds)} fuzz seeds and {len(grid_ids)} grid cases")
            held_cases = sweep.fuzz_cases(
                seeds, [tune.seed_size(seed, fuzz_sizes) for seed in seeds], pool=pool)
            if grid_ids:
                from vinylgen.fidelity import families
                by_id = {case.case_id: case for case in families.full_grid()}
                held_cases += [by_id[case_id] for case_id in grid_ids]
            held_out = {}
            for label, settings in (("current", current_settings), ("proposed", proposed_settings)):
                began = time.perf_counter()
                rows = sweep.run_cases(held_cases, config["tiers"], settings,
                                       out / "held-out" / label / "work", cleanup=cleanup,
                                       pool=pool, jobs=config["jobs"], keep=False)
                shutil.rmtree(out / "held-out" / label / "work", ignore_errors=True)
                sweep.write_sweep(out / "held-out" / label, rows, {
                    "cases": len(held_cases), "tiers": config["tiers"], "jobs": config["jobs"],
                    "wall_seconds": time.perf_counter() - began,
                    "selection": {"fuzz": f"{seeds.start}:{seeds.stop}",
                                  "fuzz_sizes": fuzz_sizes, "grid": len(grid_ids),
                                  "grid_sizes": config.get("held_out_grid_sizes")},
                    "code_hash": sweep.code_hash(),
                    "settings_changed": sweep.settings_changed(settings),
                    "settings": asdict(settings)})
                held_out[label] = tune.held_out_scores(rows)
            (out / "held-out" / "families.json").write_text(json.dumps(held_out, indent=2) + "\n")

        real = None
        if (out / "real-images").exists():
            # A resumed run's earlier traces show an older proposal; never leave them
            # beside a report they no longer match.
            shutil.rmtree(out / "real-images")
        if not args.no_real_images:
            images = sorted(tune.IMAGES_DIR.glob("*.png"))
            log(f"real corpus: {len(images)} images, current and proposed")
            real = tune.trace_real_images(images, {"current": current_settings,
                                                   "proposed": proposed_settings},
                                          out / "real-images", pool=pool, cleanup=cleanup)
            (out / "real-images" / "results.json").write_text(json.dumps(real, indent=2) + "\n")
            tune.contact_sheet(out / "real-images", real, ("current", "proposed"),
                               out / "real-images" / "contact.png")
    finally:
        if pool is not None:
            pool.shutdown()
        shutil.rmtree(out / "work", ignore_errors=True)
    meta = {**config, "wall_seconds": time.perf_counter() - started,
            "proposed_config": proposed_cfg, "held_out_count": len(seeds),
            "extra_current_eval": not known,
            "no_effect": no_effect,
            "train": f"{config['train'][0]}:{config['train'][1]}",
            "held_out": f"{config['held_out'][0]}:{config['held_out'][1]}"}
    report = tune.report_markdown(meta, evaluations, front, current_index, proposed_index, diff,
                                  weights_table, held_out, real)
    (out / "report.md").write_text(report + "\n")
    log(f"done in {meta['wall_seconds']:.1f}s: {out / 'report.md'}")


if __name__ == "__main__":
    main()
