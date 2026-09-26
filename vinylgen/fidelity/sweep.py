"""Offline fidelity sweep: trace synthetic cases through the real stages and measure them.

``run_cases`` traces each case through one or both harness tiers (``labels``: the
hard-edged label map straight into the trace stage; ``rgb``: the anti-aliased
render through the palette stage first) with the given ``TraceSettings``, measures
the edge variant against the scene truth (``metrics.measure``) and returns one
result row per case and tier. Cases run in a process pool; every case traces into
its own directory under the caller's work directory, and the trace stage's own
scratch render is uuid-named, so concurrent cases never share a file.

``aggregate`` summarises rows per tier and per family, tag and size, and
``write_sweep`` writes ``cases.jsonl``, ``summary.json`` and ``summary.md``.

Measurement policy: every trace is judged with ``vinylgen/fidelity/bounds.py``,
the bounds the synthetic regression tests assert (fixed per case from the default
trace settings, never derived from the settings under test, so a tuner cannot
loosen its own yardstick):

- a true vertex is found when a cusp lies within its radius ``R(c)``; required
  vertices are the junctions, edge points and corners whose turn the raster can
  decide, outside the narrow and palette zones (``bounds.corner_roles``);
- a kink is a cusp away from every required or optional vertex's ``R(c)``,
  outside the zones and not on a sharpenable round (``bounds.evaluate``), so
  cusp precision counts exactly what the ``kinks`` assertion rejects;
- the local Hausdorff and mean boundary distance to the truth leave out the
  vertex discs and the zones, as the ``hausdorff`` assertion does, and
  ``bound_px`` (``D``) is the per-case error budget the tuner normalises by; the
  boundary distance to the raster evidence the trace received holds everywhere,
  with its own budget ``evidence_tolerance_px`` (``e``);
- nodes are counted outside the zones against the ideal count of the boundary
  outside them (``nodes_outside_zones``, ``ideal_nodes_outside_zones``), and
  area, evidence regions and surviving colours as the ``area`` and ``evidence``
  assertions count them (``bounds.summary_numbers``);
- a case whose whole truth boundary lies in the discs and zones (features below
  the resolution floor, or removed by the palette stage by design) is flagged
  ``sub_resolution``; the tuning objective scores its truth-distance, corner,
  kink and node terms as zero (``tune`` docstring).

In the rgb tier the truth is the scene after the palette stage's merges and folds
(``bounds.case_truth``), so the bounds are derived per run.

The rgb tier infers each case's palette with its ``families.case_palette_settings``,
as the regression tests do.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict
from multiprocessing import get_context
from pathlib import Path

import numpy as np

from vinylgen.contracts import (
    DEFAULT_MAX_DEVIATION_PER_MILLE,
    FOLLOWING_LENGTHS,
    LENGTH_UNIT_PER_MILLE,
    RENAMED_TRACE_FIELDS,
    CleanupSettings,
    TraceSettings,
)

ROOT = Path(__file__).resolve().parents[2]
FIDELITY_TMP = ROOT / ".tmp" / "fidelity"
TIERS = ("labels", "rgb")
WORKER_DIED = "worker process died"


def case_family(case) -> str:
    """The case's family: its scene's ``family`` (a fuzzer scene's split by its
    generator kind, ``fuzz-polygon``), else its first tag."""
    if case.scene.family == "fuzz" and case.scene.params.get("kind"):
        return f"fuzz-{case.scene.params['kind']}"
    if case.scene.family:
        return str(case.scene.family)
    return str(case.tags[0]) if case.tags else "untagged"


def case_size(case) -> int:
    """The longer side of the case's canvas in px."""
    return int(max(case.scene.width, case.scene.height))


# --- worker ----------------------------------------------------------------------------

# Per-process caches: a worker that measures the same case again (every tuning
# evaluation traces the same subset) renders and derives its truth once. Each
# holds at most ``CACHE_ENTRIES`` entries (a case's truth, or one tier's render
# of a case), least recently used out first, so a long sweep over thousands of
# seeds stays bounded.
CACHE_ENTRIES = 96
_TRUTH_CACHE: OrderedDict = OrderedDict()
_BOUNDS_CACHE: OrderedDict = OrderedDict()
_RENDER_CACHE: OrderedDict = OrderedDict()


def _cached(cache: OrderedDict, key, make):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    value = make()
    cache[key] = value
    while len(cache) > CACHE_ENTRIES:
        cache.popitem(last=False)
    return value


def _truth(case):
    from vinylgen.fidelity.truth import scene_truth
    return _cached(_TRUTH_CACHE, case.case_id, lambda: scene_truth(case.scene))


def _bounds(case, tier: str, run):
    """The regression bounds of ``case`` in ``tier`` for ``run``. The rgb tier's
    depend on the palette stage's folds in the run, so only the labels tier's are
    cached."""
    from vinylgen.fidelity.bounds import case_bounds
    if tier != "labels":
        return case_bounds(case, tier, _truth(case), run=run)
    return _cached(_BOUNDS_CACHE, (case.case_id, tier),
                   lambda: case_bounds(case, tier, _truth(case)))


def _render(case, tier: str, tiers=()):
    """``case`` rendered for ``tier``. When the sweep also traces the case in the
    other tier (named in ``tiers``) and that render is not cached yet, one coverage
    pass renders both and caches both."""
    from vinylgen.fidelity.raster import render_labels, render_labels_and_rgb, render_rgb
    key = (case.case_id, tier)
    if key in _RENDER_CACHE:
        _RENDER_CACHE.move_to_end(key)
        return _RENDER_CACHE[key]
    other = "rgb" if tier == "labels" else "labels"
    rendered = {}
    if other in tiers and (case.case_id, other) not in _RENDER_CACHE:
        rendered["labels"], rendered["rgb"] = render_labels_and_rgb(case.scene)
    elif tier == "labels":
        rendered["labels"] = render_labels(case.scene)
    else:
        rendered["rgb"] = render_rgb(case.scene)
    if "rgb" in rendered:
        # The RGB tier traces what the palette stage would receive: the render with
        # the case's seeded noise, exactly as the regression tests trace it.
        from vinylgen.fidelity.families import with_case_noise
        rendered["rgb"] = with_case_noise(case, rendered["rgb"])
    dtypes = {"labels": np.int16, "rgb": np.uint8}
    # The requested render goes in last, so it is the most recently used.
    for name in sorted(rendered, key=lambda name: name == tier):
        _RENDER_CACHE[(case.case_id, name)] = np.asarray(rendered[name], dtype=dtypes[name])
    while len(_RENDER_CACHE) > CACHE_ENTRIES:
        _RENDER_CACHE.popitem(last=False)
    return _RENDER_CACHE[key]


_DENSE_FALLBACK = re.compile(r"(\d+) shared boundaries required a reported dense fallback")


def audit_summary(audit: dict) -> dict:
    """What the trace stage itself reported: structural validation of the two SVG
    variants, the semantic feature audit, and dense-fallback boundaries."""
    fallbacks = 0
    for warning in audit.get("warnings", []):
        match = _DENSE_FALLBACK.search(str(warning))
        if match:
            fallbacks += int(match.group(1))
    validation = audit.get("validation", {})
    return {"structural_failures": sum(1 for variant in ("edge", "overlap")
                                       if not validation.get(variant, {}).get("passed", False)),
            "semantic_passed": bool(validation.get("semantic", {}).get("passed", False)),
            "dense_fallbacks": fallbacks, "warnings": len(audit.get("warnings", []))}


def summarise_measure(result: dict, evaluation: dict) -> dict:
    """The flat per-case numbers the summaries and the tuning objective read:
    ``metrics.measure``'s raw distances, areas and nodes, and the regression
    bounds' verdicts (``bounds.summary_numbers``)."""
    from vinylgen.fidelity.bounds import summary_numbers
    boundary, area, nodes = result["boundary"], result["area"], result["nodes"]
    # Colour boundaries are sampled on both sides, so the truth's boundary length
    # is half its sample count times the spacing.
    length = boundary["truth_samples"] * boundary["sample_spacing_px"] / 2.0
    judged = summary_numbers(evaluation)
    return {
        "hausdorff_px": boundary["hausdorff_px"], "mean_px": boundary["mean_px"],
        "p95_px": boundary["p95_px"], "hausdorff_per_mille": boundary["hausdorff_per_mille"],
        "misassigned_px": area["misassigned_px"],
        "misassigned_fraction": area["misassigned_fraction"],
        "boundary_length_px": length,
        "area_offset_px": area["misassigned_px"] / length if length > 0 else None,
        **judged,
        "corner_recall": (judged["corners_found"] / judged["corners_true"]
                          if judged["corners_true"] else None),
        "nodes": nodes["boundary"], "ideal_nodes": nodes["ideal"],
        "node_ratio": nodes["ratio_to_ideal"],
        "unmatched_layers": len(area["unmatched_layers"]),
    }


def judged_measure(result: dict, evaluation: dict) -> dict:
    """``metrics.measure``'s result with its corner and kink sections replaced by
    the regression bounds' (each vertex with its role, radius and nearest cusp;
    each cusp with whether it is a kink)."""
    corners = evaluation["corners"]
    cusps = evaluation["cusps"]
    kinks = [cusp for cusp in cusps if cusp["kink"]]
    return {**result,
            "corners": {"true": len(corners), "found": sum(1 for c in corners if c["found"]),
                        "vertices": corners},
            "kinks": {"count": len(kinks), "kinks": kinks, "cusps": len(cusps),
                      "cusp_precision": ((len(cusps) - len(kinks)) / len(cusps)
                                         if cusps else None)},
            "local_boundary": evaluation["local_hausdorff"]}


def run_case(case, tier: str, settings: dict, cleanup: dict | None, work_dir: str,
             keep: bool = True, tiers=()) -> dict:
    """Trace and measure one case in one tier. ``settings`` and ``cleanup`` are
    ``asdict`` records; a fresh ``TraceSettings`` is built from them here, so no
    caller-held settings object is ever handed to the trace stage. ``tiers`` names
    every tier the sweep traces the case in, so a render can serve them all."""
    from vinylgen.fidelity.bounds import colour_map, evaluate
    from vinylgen.fidelity.families import case_palette_settings
    from vinylgen.fidelity.harness import trace_labels, trace_rgb
    from vinylgen.fidelity.metrics import measure
    width, height = case.scene.width, case.scene.height
    row = {"case_id": case.case_id, "tier": tier, "family": case_family(case),
           "tags": list(case.tags), "size": case_size(case), "width": width, "height": height,
           "min_feature_px": float(case.min_feature_px), "sub_resolution": None,
           "ok": False, "error": None}
    started = time.perf_counter()
    case_dir = Path(work_dir) / tier / case.case_id
    try:
        trace_settings = TraceSettings(**settings)
        cleanup_settings = CleanupSettings(**cleanup) if cleanup else None
        palette = case.scene.palette
        if tier == "labels":
            run = trace_labels((_render(case, tier, tiers), palette), case_dir, trace_settings,
                               cleanup_settings, subject=case.case_id)
        elif tier == "rgb":
            run = trace_rgb((_render(case, tier, tiers), palette), case_dir, trace_settings,
                            palette_options=case_palette_settings(case),
                            cleanup=cleanup_settings, subject=case.case_id)
        else:
            raise ValueError(f"unknown tier {tier!r}")
        measured = time.perf_counter()
        bounds = _bounds(case, tier, run)
        row["sub_resolution"] = not bounds.geometric
        row["bound_px"] = bounds.bound
        # A negative radius and distance list every vertex's and cusp's nearest
        # distance; the bounds classify them.
        result = measure(run.edge_svg, bounds.truth, palette, corner_radius=-1.0,
                         smooth_min_distance=-1.0, colour_map=colour_map(bounds, run))
        evaluation = evaluate(case, tier, run, bounds, measured=result)
        row.update(ok=True, palette_seconds=run.palette_seconds, trace_seconds=run.trace_seconds,
                   measure_seconds=time.perf_counter() - measured,
                   palette_unmatched=len(run.unmatched),
                   summary={**summarise_measure(result, evaluation), **audit_summary(run.audit)},
                   measure=judged_measure(result, evaluation))
        if keep:
            row["subject_dir"] = str(run.subject_dir)
    except Exception as error:  # a failed trace is a result, never a crash of the sweep
        row["error"] = f"{type(error).__name__}: {error}"
        row["traceback"] = traceback.format_exc()
    row["seconds"] = time.perf_counter() - started
    if not keep:
        shutil.rmtree(case_dir, ignore_errors=True)
    return row


# --- running ----------------------------------------------------------------------------

def default_jobs() -> int:
    return os.cpu_count() or 1


def make_pool(jobs: int) -> ProcessPoolExecutor | None:
    """A spawned process pool (numba's threads make fork unsafe), or None for
    ``jobs <= 1``, which runs cases in process."""
    if jobs <= 1:
        return None
    return ProcessPoolExecutor(max_workers=jobs, mp_context=get_context("spawn"))


def run_cases(cases, tiers, settings: TraceSettings, work_dir: Path, *,
              cleanup: CleanupSettings | None = None, jobs: int | None = None,
              pool: ProcessPoolExecutor | None = None, keep: bool = True,
              on_row=None) -> list[dict]:
    """Every case in every tier, rows in case-then-tier order. ``pool`` reuses a
    caller's pool; otherwise one of ``jobs`` workers is made for this call."""
    tiers = list(tiers)
    for tier in tiers:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; expected one of {', '.join(TIERS)}")
    ids = [case.case_id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("case ids must be unique")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    record = asdict(settings)
    cleanup_record = asdict(cleanup) if cleanup is not None else None
    jobs_list = [(case, tier) for case in cases for tier in tiers]
    own_pool = None
    if pool is None:
        own_pool = pool = make_pool(default_jobs() if jobs is None else jobs)
    try:
        if pool is None:
            rows = []
            for case, tier in jobs_list:
                row = run_case(case, tier, record, cleanup_record, str(work_dir), keep,
                               tuple(tiers))
                rows.append(row)
                if on_row:
                    on_row(row)
            return rows
        futures = [pool.submit(run_case, case, tier, record, cleanup_record, str(work_dir), keep,
                               tuple(tiers))
                   for case, tier in jobs_list]
        rows = []
        for (case, tier), future in zip(jobs_list, futures):
            try:
                row = future.result()
            except BrokenProcessPool as error:
                # A worker died (killed, out of memory): its case and every case
                # still queued are recorded as failed rows (``WORKER_DIED``), never
                # lost. The pool is unusable afterwards.
                row = {"case_id": case.case_id, "tier": tier, "family": case_family(case),
                       "tags": list(case.tags), "size": case_size(case),
                       "width": case.scene.width, "height": case.scene.height,
                       "min_feature_px": float(case.min_feature_px), "sub_resolution": None,
                       "ok": False, "error": f"{WORKER_DIED}: {error}", "seconds": 0.0}
            rows.append(row)
            if on_row:
                on_row(row)
        return rows
    finally:
        if own_pool is not None:
            own_pool.shutdown()


# --- aggregation -----------------------------------------------------------------------

def _values(rows, key) -> list[float]:
    return [float(row["summary"][key]) for row in rows
            if row["ok"] and row["summary"].get(key) is not None
            and math.isfinite(float(row["summary"][key]))]


def _stat(values: list[float], kind: str):
    if not values:
        return None
    if kind == "median":
        return float(np.median(values))
    if kind == "p95":
        return float(np.percentile(values, 95))
    if kind == "max":
        return float(max(values))
    if kind == "mean":
        return float(np.mean(values))
    raise ValueError(kind)


def geometric_mean(values: list[float]) -> float | None:
    positive = [value for value in values if value > 0]
    if not positive:
        return None
    return float(math.exp(sum(math.log(value) for value in positive) / len(positive)))


def _f1(recall: float | None, precision: float | None) -> float | None:
    if recall is None or precision is None:
        return None
    return 2.0 * recall * precision / (recall + precision) if recall + precision else 0.0


def group_stats(rows: list[dict]) -> dict:
    """Aggregate numbers for one group of rows.

    Hausdorff: median, p95 and max over cases (px) of the boundary distance off
    the vertex discs and zones (the regression ``hausdorff`` quantity; cases whose
    whole boundary lies in them are left out). Boundary distance: the mean of the
    per-case mean distances over the same samples (px). Misassigned area: the mean
    per-case fraction of the canvas. Corner recall: pooled (all found over all
    true), for all true vertices and for the required ones, each found within its
    ``R(c)``. Cusp precision: pooled cusp nodes that are no kink (``bounds``) over
    all cusp nodes. Corner F1: the
    harmonic mean of corner recall and cusp precision; recall alone is satisfied
    for free by a staircase trace whose every node is a cusp, so it is only read
    together with precision. Kinks: total and per case. Node ratio: the
    geometric mean of boundary nodes over ideal nodes (ratios compound, so the
    geometric mean weighs a halving and a doubling equally), plus the median.
    Topology: cases with any colour whose traced polygon or hole count differs
    from the truth's off the vertex discs and zones, and the mismatched colours in
    total.
    """
    ok = [row for row in rows if row["ok"]]
    geometric = [row for row in ok if not row.get("sub_resolution")]
    hausdorff = _values(geometric, "local_hausdorff_px")
    corners_true = sum(row["summary"]["corners_true"] for row in ok)
    corners_found = sum(row["summary"]["corners_found"] for row in ok)
    required = sum(row["summary"]["required_corners"] for row in ok)
    required_found = sum(row["summary"]["required_found"] for row in ok)
    kinks = [row["summary"]["kinks"] for row in ok]
    cusps = sum(row["summary"]["cusps"] for row in ok)
    precision = (cusps - sum(kinks)) / cusps if cusps else None
    recall = corners_found / corners_true if corners_true else None
    ratios = _values(ok, "node_ratio")
    mismatches = [row["summary"]["topology_mismatches"] for row in ok]
    return {
        "cases": len(rows), "failed": len(rows) - len(ok),
        "sub_resolution": sum(1 for row in rows if row.get("sub_resolution")),
        "hausdorff_px": {"median": _stat(hausdorff, "median"), "p95": _stat(hausdorff, "p95"),
                         "max": _stat(hausdorff, "max")},
        "mean_boundary_px": _stat(_values(geometric, "local_mean_px"), "mean"),
        "misassigned_fraction": _stat(_values(ok, "misassigned_fraction"), "mean"),
        "corner_recall": recall,
        "required_corner_recall": required_found / required if required else None,
        "cusp_precision": precision,
        "corner_f1": _f1(recall, precision),
        "corners": {"true": corners_true, "found": corners_found,
                    "required": required, "required_found": required_found},
        "kinks": {"total": int(sum(kinks)), "per_case": _stat(kinks, "mean")},
        "node_ratio": {"geometric_mean": geometric_mean(ratios), "median": _stat(ratios, "median")},
        "topology": {"cases_mismatched": sum(1 for count in mismatches if count),
                     "colours_mismatched": int(sum(mismatches))},
        "trace_audit": {
            "structural_failures": sum(1 for row in ok if row["summary"].get("structural_failures")),
            "semantic_failures": sum(1 for row in ok
                                     if not row["summary"].get("semantic_passed", True)),
            "dense_fallbacks": int(sum(row["summary"].get("dense_fallbacks", 0) for row in ok))},
        "seconds": {"total": float(sum(row.get("seconds", 0.0) for row in rows)),
                    "trace_median": _stat([row["trace_seconds"] for row in ok], "median")},
    }


def aggregate(rows: list[dict]) -> dict:
    """``{tier: {"all": stats, "family": {name: stats}, "tag": {...}, "size": {...}}}``."""
    summary = {}
    for tier in sorted({row["tier"] for row in rows}):
        tier_rows = [row for row in rows if row["tier"] == tier]
        groups = {"family": {}, "tag": {}, "size": {}}
        for row in tier_rows:
            groups["family"].setdefault(row["family"], []).append(row)
            for tag in row["tags"]:
                groups["tag"].setdefault(tag, []).append(row)
            groups["size"].setdefault(str(row["size"]), []).append(row)
        summary[tier] = {"all": group_stats(tier_rows),
                         **{kind: {name: group_stats(members)
                                   for name, members in sorted(
                                       found.items(),
                                       key=lambda item: (int(item[0]) if kind == "size"
                                                         else 0, item[0]))}
                            for kind, found in groups.items()}}
    return summary


def _fmt(value, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}g}"


SUMMARY_COLUMNS = ("cases", "failed", "Hausdorff median px", "Hausdorff p95 px",
                   "Hausdorff max px", "mean dist px", "misassigned %", "corner recall",
                   "required recall", "cusp precision", "corner F1", "kinks", "node ratio (geo)", "topology mismatches",
                   "structural failures", "semantic failures", "dense fallbacks")


def summary_row(name: str, stats: dict) -> str:
    misassigned = stats["misassigned_fraction"]
    cells = [name, _fmt(stats["cases"]), _fmt(stats["failed"]),
             _fmt(stats["hausdorff_px"]["median"]), _fmt(stats["hausdorff_px"]["p95"]),
             _fmt(stats["hausdorff_px"]["max"]), _fmt(stats["mean_boundary_px"]),
             _fmt(None if misassigned is None else 100.0 * misassigned),
             _fmt(stats["corner_recall"]), _fmt(stats["required_corner_recall"]),
             _fmt(stats["cusp_precision"]), _fmt(stats["corner_f1"]),
             _fmt(stats["kinks"]["total"]), _fmt(stats["node_ratio"]["geometric_mean"]),
             _fmt(stats["topology"]["cases_mismatched"]),
             _fmt(stats["trace_audit"]["structural_failures"]),
             _fmt(stats["trace_audit"]["semantic_failures"]),
             _fmt(stats["trace_audit"]["dense_fallbacks"])]
    return "| " + " | ".join(cells) + " |"


def summary_table(title: str, groups: dict) -> list[str]:
    lines = [f"| {title} | " + " | ".join(SUMMARY_COLUMNS) + " |",
             "|" + "---|" * (len(SUMMARY_COLUMNS) + 1)]
    lines += [summary_row(name, stats) for name, stats in groups.items()]
    return lines


def summary_markdown(summary: dict, meta: dict) -> str:
    lines = ["# Fidelity sweep", "",
             f"- Cases: {meta.get('cases')}; tiers: {', '.join(meta.get('tiers', []))}; "
             f"jobs: {meta.get('jobs')}; wall time: {_fmt(meta.get('wall_seconds'), 4)} s",
             f"- Settings: `{json.dumps(meta.get('settings_changed', {}))}` over the "
             "current defaults",
             "- Judged with the synthetic regression bounds (vinylgen/fidelity/bounds.py): a "
             "vertex is found by a cusp within its radius R(c); required vertices are the "
             "junctions, edge points and raster-decidable corners outside the narrow and "
             "palette zones; a kink is a cusp the kinks assertion rejects.",
             "- Hausdorff statistics are over cases, off the vertex discs and zones; mean dist "
             "is the mean of per-case mean boundary distances over the same samples; "
             "misassigned % is the mean per-case share of the canvas; recall is pooled over "
             "vertices; cusp precision is pooled over cusp nodes (those that are no kink); "
             "corner F1 combines the two, since a staircase whose every node is a cusp scores "
             "full recall; node ratio is the geometric mean of boundary nodes over ideal "
             "nodes; topology mismatches, structural failures (edge or overlap SVG failed the "
             "trace stage's validation) and semantic failures (its feature audit) count cases; "
             "dense fallbacks count boundaries.",
             f"- Selection: `{json.dumps(meta.get('selection', {}))}`",
             f"- Code: `{meta.get('code_hash', '-')}` (hash of the trace path's source)", ""]
    for tier, tier_summary in summary.items():
        lines += [f"## Tier `{tier}`", ""]
        lines += summary_table("group", {"all": tier_summary["all"]}) + [""]
        for kind in ("family", "tag", "size"):
            if tier_summary[kind]:
                lines += [f"### By {kind}", ""]
                lines += summary_table(kind, tier_summary[kind]) + [""]
    failures = meta.get("failures") or []
    if failures:
        lines += ["## Failed cases", ""]
        lines += [f"- `{row['case_id']}` ({row['tier']}): {row['error']}" for row in failures]
        lines.append("")
    return "\n".join(lines)


def effective_record(settings: TraceSettings) -> dict:
    """``asdict(settings)`` with the automatic maximum deviation and the lengths
    that follow it filled in, as the trace stage fills them (per mille only)."""
    record = asdict(settings)
    if record["length_unit"] == LENGTH_UNIT_PER_MILLE:
        if record["max_deviation"] is None:
            record["max_deviation"] = DEFAULT_MAX_DEVIATION_PER_MILLE
        for name, source in FOLLOWING_LENGTHS.items():
            if record[name] is None:
                record[name] = record[source]
    return record


def settings_changed(settings: TraceSettings) -> dict:
    """The fields of ``settings`` whose effective value differs from
    ``TraceSettings()``'s."""
    current, default = effective_record(settings), effective_record(TraceSettings())
    return {name: value for name, value in current.items() if default.get(name) != value}


def cleanup_changed(cleanup: CleanupSettings | None) -> dict:
    if cleanup is None:
        return {}
    current, default = asdict(cleanup), asdict(CleanupSettings())
    return {name: value for name, value in current.items() if default.get(name) != value}


# The modules a traced and measured result depends on. Their source hash is
# recorded with every sweep and tuning evaluation, so results from different
# code are never mixed silently. The tuner's own module is left out: its
# objective is recomputed from stored per-case numbers.
_TRACE_PATH_GLOBS = ("vinylgen/*.py", "vinylgen/fidelity/harness.py",
                     "vinylgen/fidelity/metrics.py", "vinylgen/fidelity/bounds.py", "vinylgen/fidelity/truth.py",
                     "vinylgen/fidelity/raster.py", "vinylgen/fidelity/scene.py",
                     "vinylgen/fidelity/cases.py", "vinylgen/fidelity/families.py",
                     "vinylgen/fidelity/fuzz.py", "vinylgen/fidelity/sweep.py")


def code_hash() -> str:
    """SHA-256 (first 16 hex digits) of the trace path's source files."""
    digest = hashlib.sha256()
    paths = sorted({path for pattern in _TRACE_PATH_GLOBS for path in ROOT.glob(pattern)})
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def new_run_dir(prefix: str) -> Path:
    """A fresh ``.tmp/fidelity/<prefix>-<timestamp>[-n]`` directory."""
    FIDELITY_TMP.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}-{timestamp()}"
    for attempt in range(1, 1000):
        path = FIDELITY_TMP / (stem if attempt == 1 else f"{stem}-{attempt}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"no free run directory for {stem}")


def write_sweep(out_dir: Path, rows: list[dict], meta: dict) -> dict:
    """Write ``cases.jsonl``, ``summary.json`` and ``summary.md``; return the summary."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "cases.jsonl").open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    summary = aggregate(rows)
    meta = {**meta, "failures": [{"case_id": row["case_id"], "tier": row["tier"],
                                  "error": row["error"]} for row in rows if not row["ok"]]}
    (out_dir / "summary.json").write_text(json.dumps({"meta": meta, "summary": summary},
                                                     indent=2) + "\n")
    (out_dir / "summary.md").write_text(summary_markdown(summary, meta) + "\n")
    return summary


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# --- case selection ----------------------------------------------------------------------

def parse_range(text: str) -> range:
    """``START:STOP`` (stop exclusive) as a range."""
    try:
        start, stop = (int(part) for part in text.split(":"))
    except ValueError:
        raise ValueError(f"expected START:STOP, not {text!r}") from None
    if stop < start:
        raise ValueError(f"empty seed range {text!r}")
    return range(start, stop)


def _fuzz_case(seed: int, size: int):
    from vinylgen.fidelity.fuzz import fuzz_case
    return fuzz_case(int(seed), size=int(size))


def fuzz_cases(seeds, size=512, pool=None) -> list:
    """Fuzzer cases for ``seeds`` at ``size`` (an int, or one size per seed),
    generated in ``pool`` when given (generation takes about half a second a
    case)."""
    seeds = [int(seed) for seed in seeds]
    sizes = [int(v) for v in size] if isinstance(size, (list, tuple)) else [int(size)] * len(seeds)
    if pool is None:
        return [_fuzz_case(seed, sz) for seed, sz in zip(seeds, sizes)]
    return list(pool.map(_fuzz_case, seeds, sizes, chunksize=4))


def select_cases(*, fixed: bool = False, grid: bool = False, palette_stage: bool = False,
                 fuzz: range | None = None, fuzz_size: int = 512,
                 sizes: list[int] | None = None, tags: list[str] | None = None,
                 pool=None) -> list:
    """The union of the chosen case sets (first occurrence of an id wins), then
    kept when its longer side is in ``sizes`` and it carries any of ``tags``."""
    cases = []
    if fixed or grid or palette_stage:
        from vinylgen.fidelity import families
        if fixed:
            cases += families.fixed_set()
        if grid:
            cases += families.full_grid()
        if palette_stage:
            cases += families.palette_stage_set()
    if fuzz is not None:
        cases += fuzz_cases(fuzz, fuzz_size, pool=pool)
    seen, unique = set(), []
    for case in cases:
        if case.case_id not in seen:
            seen.add(case.case_id)
            unique.append(case)
    if sizes:
        unique = [case for case in unique if case_size(case) in set(sizes)]
    if tags:
        unique = [case for case in unique if set(tags) & set(case.tags)]
    return unique


# --- settings from the trace CLI's flags ----------------------------------------------------

def _trace_flag(parser, name: str, value: str) -> list[str]:
    """``--set name=value`` as the trace CLI's own flag(s)."""
    name = RENAMED_TRACE_FIELDS.get(name, name)
    for action in parser._actions:
        if action.dest != name or not action.option_strings:
            continue
        option = action.option_strings[0]
        if action.nargs == 0:  # store_true or BooleanOptionalAction
            truthy = value.strip().lower() in ("1", "true", "yes", "on")
            if not truthy and value.strip().lower() not in ("0", "false", "no", "off"):
                raise ValueError(f"--set {name}= expects true or false, not {value!r}")
            if truthy:
                return [option]
            negative = [flag for flag in action.option_strings if flag.startswith("--no-")]
            if negative:
                return [negative[0]]
            if isinstance(action, argparse._StoreTrueAction) and field_default(name) is False:
                return []  # false is the setting's default: the flag's absence
            raise ValueError(f"--set {name}=false: the trace CLI cannot turn this off")
        return [option, value]
    raise ValueError(f"unknown trace setting {name!r} (see svg_trace.py --help)")


def settings_from_options(config: Path | None, assignments: list[str]
                          ) -> tuple[TraceSettings, CleanupSettings]:
    """Trace and cleanup settings from a config file and ``name=value`` pairs,
    through the trace CLI's own parser and ``settings_from_args`` (units, ranges,
    config upgrades and flag precedence all as ``svg_trace.py`` applies them)."""
    from vinylgen.cli_trace import parser as trace_parser
    from vinylgen.cli_trace import settings_from_args
    parser = trace_parser()
    parser.exit_on_error = False
    argv = ["."]
    if config is not None:
        argv += ["--config", str(config)]
    for item in assignments:
        if "=" not in item:
            raise ValueError(f"--set expects name=value, not {item!r}")
        name, value = item.split("=", 1)
        argv += _trace_flag(parser, name.strip().replace("-", "_"), value)
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        raise ValueError(str(error)) from None
    settings, cleanup, _ = settings_from_args(args)
    return settings, cleanup


def field_default(name: str):
    """The default of a trace or cleanup settings field."""
    for record in (asdict(TraceSettings()), asdict(CleanupSettings())):
        if name in record:
            return record[name]
    raise KeyError(name)
