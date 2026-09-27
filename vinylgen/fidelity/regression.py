"""The synthetic fidelity regression corpus: every (tier, case) the regression tests
trace, how one is traced and judged, and the expected-failure table.

``tests/test_synthetic_fidelity.py`` asserts on these runs and
``scripts/record_expected_failures.py`` records the table's measurements from them.

Expected-failure table
----------------------
``tests/synthetic/expected-failures.json`` maps ``"<tier>/<case_id>"`` to
``{assertion: {"tasks": [slug, ...], "measured": {name: value}}}``: that assertion
fails today for the defects the worklog tasks track, with the measurements
``bounds.measurements`` gave it when it was recorded (``bounds.MEASUREMENTS``
names them, all larger-is-worse).

An entry must not get worse (``bounds.worse_than_recorded``): a count may not
grow and a length or area may not grow beyond ``bounds.WORSE_MARGIN``, so a
regression the entry would otherwise hide still fails the run. A better
measurement is not a failure; ``scripts/record_expected_failures.py`` tightens
the record when asked. An assertion that passes is an unexpected success, which
fails the run until its entry is removed.

Gate slice
----------
The default test run traces ``gate_runs()``, the slice of the corpus listed in
``tests/synthetic/gate-slice.json``: every case on a canvas of at most 256 px,
plus the cheapest case of each family and tier without one (the palette-stage
sets and the fixed-pixel sweep), about a third of the expected failures at a
tenth of the corpus's trace time. The full corpus, with the checks that each
case's bounds can fail, runs in the opt-in full-image bucket
(``tests/fullimage/test_synthetic_fidelity_corpus.py``).
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
GATE_SLICE_PATH = SYNTHETIC / "gate-slice.json"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def run_case(case, tier: str, work_dir: Path, settings: TraceSettings | None = None,
             bound_checks: bool = False) -> dict:
    """Trace ``case`` in ``tier`` (with the default settings unless ``settings`` is
    given) and evaluate it against its bounds, which always come from the default
    settings. The result also carries the scene colours of the hard-edged label
    render (``raster_colours``). With ``bound_checks`` it also carries the checks
    that the case's bounds can fail: ``bounds.area_coverage``'s findings and
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
    if not bound_checks:
        return result
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


def gate_slice(path: Path = GATE_SLICE_PATH) -> list[str]:
    """The ``tier/case_id`` keys the default test run traces."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def gate_runs() -> list[tuple[str, object]]:
    """The (tier, case) runs of ``gate_slice``, in ``all_runs`` order."""
    keys = set(gate_slice())
    return [(tier, case) for tier, case in all_runs() if f"{tier}/{case.case_id}" in keys]


def load_expected_failures(path: Path = EXPECTED_FAILURES_PATH) -> dict[str, dict[str, dict]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _format(value, depth: int = 0) -> str:
    """JSON with one level of indent per nesting, every list of plain values on
    one line."""
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


def write_expected_failures(table: dict, path: Path = EXPECTED_FAILURES_PATH) -> None:
    def written(name: str, entry: dict) -> dict:
        return {"tasks": list(entry["tasks"]),
                "measured": {k: entry["measured"][k] for k in bounds.MEASUREMENTS[name]}}

    ordered = {key: {name: written(name, entry) for name, entry in sorted(entries.items())}
               for key, entries in sorted(table.items()) if entries}
    Path(path).write_text(_format(ordered) + "\n", encoding="utf-8")


ENTRY_KEYS = {"tasks", "measured"}


def entry_problems(name: str, entry) -> list[str]:
    """What is wrong with one table entry's form (empty when it is well formed):
    ``tasks`` a non-empty list of distinct slugs, ``measured`` exactly the
    assertion's measurements, each a non-negative number."""
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
        return [f'{name}: an entry is {{"tasks": [slug, ...], "measured": {{...}}}}']
    problems = []
    tasks = entry["tasks"]
    if not isinstance(tasks, list) or not tasks:
        problems.append(f"{name}: tasks must be a non-empty list of slugs")
    else:
        problems += [f"{name}: task {slug!r} is not a slug" for slug in tasks
                     if not isinstance(slug, str) or not SLUG_PATTERN.match(slug)]
        if len(set(map(str, tasks))) != len(tasks):
            problems.append(f"{name}: tasks {tasks} repeat a slug")
    measured = entry["measured"]
    expected = set(bounds.MEASUREMENTS.get(name, ()))
    if not isinstance(measured, dict) or set(measured) != expected:
        problems.append(f"{name}: measured must name exactly {sorted(expected)}")
    else:
        for key, value in measured.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or math.isnan(value) or value < 0:
                problems.append(f"{name}: measured {key} {value!r} is no non-negative number")
    return problems


def run_and_judge(tier: str, case, work_dir: Path) -> dict:
    """``run_case`` plus every applicable assertion's verdict and measurements:
    ``{"result", "verdicts": {name: {"skip"} | {"passed", "detail", "measured"}}}``.
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
                          "measured": bounds.measurements(name, result, case)}
    return {"result": result, "verdicts": verdicts}
