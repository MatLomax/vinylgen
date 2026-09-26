"""The synthetic fidelity regression corpus: every (tier, case) the regression tests
trace, how one is traced and judged, and the expected-failure table.

``tests/test_synthetic_fidelity.py`` asserts on these runs and
``scripts/record_expected_failures.py`` records the table's measurements from them.

Expected-failure table
----------------------
``tests/synthetic/expected-failures.json`` maps ``"<tier>/<case_id>"`` to
``{assertion: {"task": slug, "measured": {name: value}, "identities": {kind:
[item, ...]}}}``: that assertion fails today for the defect tracked by worklog
task ``slug``, with the measurements ``bounds.measurements`` gave it when it was
recorded (``bounds.MEASUREMENTS`` names them) and the identities of what it found
wrong (``bounds.identities``, ``bounds.IDENTITIES``: the missed vertices, kink
points, node positions, missing regions, wrong per-colour counts and so on). An
entry is a ratchet (``bounds.entry_ratchet``): the assertion must still fail its
bound, every measurement must equal its recorded value within
``bounds.RATCHET_TOLERANCE``, and the set of identities must be the recorded one,
item for item within ``bounds.IDENTITY_TOLERANCE``. Worse (a larger measurement,
an item not recorded) is a regression the entry would otherwise hide; better
(a smaller measurement, a recorded item no longer found) leaves the record too
loose to catch the next regression, so it is re-recorded. So fixing one recorded
defect while introducing another of the same size is a regression too.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.fidelity import bounds, families
from vinylgen.fidelity.harness import trace_labels, trace_rgb
from vinylgen.fidelity.raster import render_labels

ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC = ROOT / "tests" / "synthetic"
EXPECTED_FAILURES_PATH = SYNTHETIC / "expected-failures.json"
FUZZ_SEEDS_PATH = SYNTHETIC / "fuzz-seeds.json"
# Expected failures name real worklog tasks; a slug with this prefix is a
# placeholder for a finding no task tracks yet.
PLACEHOLDER_SLUG_PREFIX = "synthetic-finding-"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def run_case(case, tier: str, work_dir: Path, settings: TraceSettings | None = None) -> dict:
    """Trace ``case`` in ``tier`` (with the default settings unless ``settings`` is
    given) and evaluate it against its bounds, which always come from the default
    settings. The result also carries the scene colours of the hard-edged label
    render (``raster_colours``), ``bounds.area_coverage``'s findings and
    ``bounds.topology_mutations``' verdicts."""
    scene = case.scene
    labels = np.asarray(render_labels(scene), dtype=np.int16)
    raster_colours = {int(value) for value in np.unique(labels)}
    if tier == "labels":
        run = trace_labels((labels, scene.palette), work_dir, settings or TraceSettings(),
                           subject=case.case_id)
    else:
        run = trace_rgb((families.render_case_rgb(case), scene.palette), work_dir,
                        settings or TraceSettings(),
                        palette_options=families.case_palette_settings(case),
                        subject=case.case_id)
    case_bounds = bounds.case_bounds(case, tier, run=run)
    result = bounds.evaluate(case, tier, run, case_bounds, raster_colours=raster_colours)
    result["raster_colours"] = sorted(raster_colours)
    # In the labels tier the evidence is the label render itself; in the rgb tier
    # the evidence assertion demands every unasserted colour it carries survive.
    result["area_coverage"] = bounds.area_coverage(
        case_bounds, raster_colours if tier == "labels" else None)
    result["topology_mutations"] = bounds.topology_mutations(case_bounds)
    return result


def fuzz_cases() -> list:
    from vinylgen.fidelity.fuzz import pinned_cases
    return pinned_cases(FUZZ_SEEDS_PATH)


def all_runs() -> list[tuple[str, object]]:
    """Every (tier, case) the suite traces, in a stable order."""
    runs = []
    for case in families.load_fixed_set(ROOT / families.FIXED_DIR):
        runs += [(tier, case) for tier in bounds.TIERS]
    for case in families.load_fixed_set(ROOT / families.PALETTE_STAGE_DIR):
        runs.append(("rgb", case))
    for case in fuzz_cases():
        runs += [(tier, case) for tier in bounds.TIERS]
    return runs


def load_expected_failures(path: Path = EXPECTED_FAILURES_PATH) -> dict[str, dict[str, dict]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# Identity coordinates are written to this many decimals, well inside
# ``bounds.IDENTITY_TOLERANCE``.
IDENTITY_DECIMALS = 4


def _format(value, depth: int = 0) -> str:
    """JSON with one level of indent per nesting, every list of plain values on
    one line (so an identity item is one line)."""
    pad = " " * (depth + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = [f"{pad}{json.dumps(str(key))}: {_format(item, depth + 1)}"
                for key, item in value.items()]
        return "{\n" + ",\n".join(rows) + "\n" + " " * depth + "}"
    if isinstance(value, list):
        if all(not isinstance(item, (list, dict)) for item in value):
            return json.dumps(value)
        rows = [f"{pad}{_format(item, depth + 1)}" for item in value]
        return "[\n" + ",\n".join(rows) + "\n" + " " * depth + "]"
    return json.dumps(value)


def _rounded(item: list) -> list:
    return [round(value, IDENTITY_DECIMALS) if isinstance(value, float) else value
            for value in item]


def write_expected_failures(table: dict, path: Path = EXPECTED_FAILURES_PATH) -> None:
    def written(name: str, entry: dict) -> dict:
        return {"task": entry["task"],
                "measured": {k: entry["measured"][k] for k in bounds.MEASUREMENTS[name]},
                "identities": {k: [_rounded(item) for item in entry["identities"][k]]
                               for k in bounds.IDENTITIES[name]}}

    ordered = {key: {name: written(name, entry) for name, entry in sorted(entries.items())}
               for key, entries in sorted(table.items()) if entries}
    Path(path).write_text(_format(ordered) + "\n", encoding="utf-8")


def entry_problems(name: str, entry) -> list[str]:
    """What is wrong with one table entry's form (empty when it is well formed)."""
    if not isinstance(entry, dict) or set(entry) != {"task", "measured", "identities"}:
        return [f"{name}: an entry is {{\"task\": slug, \"measured\": {{...}}, "
                f"\"identities\": {{...}}}}"]
    problems = []
    slug = entry["task"]
    if not isinstance(slug, str) or not SLUG_PATTERN.match(slug):
        problems.append(f"{name}: task {slug!r} is not a slug")
    elif slug.startswith(PLACEHOLDER_SLUG_PREFIX):
        problems.append(f"{name}: placeholder slug {slug}; name the worklog task that tracks "
                        "the defect")
    measured = entry["measured"]
    expected = set(bounds.MEASUREMENTS.get(name, ()))
    if not isinstance(measured, dict) or set(measured) != expected:
        problems.append(f"{name}: measured must name exactly {sorted(expected)}")
    else:
        for key, value in measured.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or math.isnan(value) or value < 0:
                problems.append(f"{name}: measured {key} {value!r} is no non-negative number")
    found = entry["identities"]
    kinds = set(bounds.IDENTITIES.get(name, ()))
    if not isinstance(found, dict) or set(found) != kinds:
        problems.append(f"{name}: identities must name exactly {sorted(kinds)}")
    else:
        for key, items in found.items():
            if not isinstance(items, list) or not all(
                    isinstance(item, list) and item and all(
                        isinstance(value, str) or (isinstance(value, (int, float))
                                                   and not isinstance(value, bool)
                                                   and math.isfinite(value))
                        for value in item)
                    for item in items):
                problems.append(f"{name}: identities {key} must be a list of non-empty lists "
                                "of numbers and strings")
    return problems


def run_and_judge(tier: str, case, work_dir: Path) -> dict:
    """``run_case`` plus every applicable assertion's verdict, measurements and
    identities: ``{"result", "verdicts": {name: {"skip"} | {"passed", "detail",
    "measured", "identities"}}}``.
    The work directory is removed afterwards."""
    shutil.rmtree(work_dir, ignore_errors=True)
    try:
        result = run_case(case, tier, work_dir)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    verdicts = {}
    for name in bounds.applicable(case, tier):
        reason = bounds.available(name, result)
        if reason is not None:
            verdicts[name] = {"skip": reason}
            continue
        passed, detail = bounds.check(name, result, case)
        verdicts[name] = {"passed": passed, "detail": detail,
                          "measured": bounds.measurements(name, result, case),
                          "identities": bounds.identities(name, result, case)}
    return {"result": result, "verdicts": verdicts}
