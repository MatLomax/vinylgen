"""The synthetic fidelity regression corpus: every (tier, case) the regression tests
trace, how one is traced and judged, and the expected-failure table.

``tests/test_synthetic_fidelity.py`` asserts on these runs and
``scripts/record_expected_failures.py`` records the table's measurements from them.

Expected-failure table
----------------------
``tests/synthetic/expected-failures.json`` maps ``"<tier>/<case_id>"`` to
``{assertion: {"tasks": [slug, ...], "counterfactual": name | null, "measured":
{name: value}, "identities": {kind: [item, ...]}}}``: that assertion fails today,
with the measurements ``bounds.measurements`` gave it when it was recorded
(``bounds.MEASUREMENTS`` names them) and the identities of what it found wrong
(``bounds.identities``, ``bounds.IDENTITIES``: the missed vertices, kink points,
node positions, missing regions, wrong per-colour counts and so on).

Attribution: ``tasks`` are the worklog tasks whose fixes the failure needs, all
of them, and ``counterfactual`` names the proxies (``COUNTERFACTUALS``, joined by
``+``) that, applied together to the same trace, make the assertion pass: each
proxy stands in for one task's fix, the proxies' tasks are exactly ``tasks``,
and leaving out any one of them no longer clears it. An entry no combination of
the proxies clears names a single placeholder task (``PLACEHOLDER_SLUG_PREFIX``)
for its pinned cause and no counterfactual. A proxy may stand for a placeholder
task too (a cause pinned in the code that no worklog task tracks yet). The table
test rejects every placeholder until a worklog task tracks its cause.

An entry is a ratchet (``bounds.entry_ratchet``): the assertion must still fail
its bound, every measurement must equal its recorded value within
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

# The counterfactual proxies an attribution names: each re-traces the case's own
# input with one task's mechanism removed, by the change described, applied at
# run time (no product file changes). Lengths are pixels of the traced image,
# applied as thousandths of its longer side and never below the default. A proxy
# may stand for a placeholder task: a cause pinned in the code that no worklog
# task tracks yet.
_FLOORS = "stop-default-per-mille-trace-lengths-collapsing-below-a-pixe"
_STAIRCASE = "keep-the-curve-fit-tolerance-clear-of-staircase-noise-so-smo"
_ROUTE = "make-the-curve-section-route-minimal-and-symmetric-about-a-c"
_JUNCTION = "stop-anti-alias-resolution-giving-a-many-colour-junction-s-c"
_THIN_COLOUR = "stop-palette-inference-replacing-a-thin-feature-s-colour-wit"
COUNTERFACTUALS = {
    "pixel-floors": {
        "task": _FLOORS,
        "change": "corner_analysis_span, side_smoothness, straight_line_max_deviation, "
                  "minimum_straight_length and curve_feature_scale each at least its "
                  "pixel-era value (8, 1.6, 1.6, 4 and 4 px)"},
    "span-12px": {
        "task": _FLOORS,
        "change": "corner_analysis_span at least 12 px, where the one-arc test rejects a "
                  "real corner at 8 px"},
    "tolerance-1.5px": {
        "task": _STAIRCASE,
        "change": "curve_fit_tolerance and max_deviation at least 1.5 px, the corner "
                  "sharpening allowance held at its default"},
    "straight-8px": {
        "task": _STAIRCASE,
        "change": "minimum_straight_length at least 8 px"},
    "no-unit-lines": {
        "task": _STAIRCASE,
        "change": "curve_fit._fit_section_records fits a curve section with "
                  "minimum_straight_length infinite, so step 1's line pieces never become "
                  "straight units inside it; whole straight sections, corners and snapping "
                  "keep the default"},
    "no-prune": {
        "task": _ROUTE,
        "change": "curve_fit._PRUNE_STREAK 0: the route search tries every start "
                  "candidate back to the turn prune"},
    "no-window": {
        "task": _ROUTE,
        "change": "curve_fit._NEWTON_WINDOW unbounded: every piece missing the tolerance is "
                  "refitted at Newton-improved parameters before it is judged"},
    "minimax-piece": {
        "task": _ROUTE,
        "change": "a piece the least-squares fit and its Newton refits reject, whose start "
                  "would give fewer segments, is searched for its minimax cubic (handle "
                  "lengths, and a free end's direction) and accepted when every sample lies "
                  "within the tolerance and it is chord-monotone"},
    "minimax-free": {
        "task": _ROUTE,
        "change": "minimax-piece, searching a piece with two free (cusp) ends too"},
    "nearline": {
        "task": _ROUTE,
        "change": "a fixed-tangent piece whose samples lie within the tolerance of its chord "
                  "is also tried as a near-line cubic with handles short enough to keep "
                  "every sample within the tolerance; a piece with one free end also with "
                  "that end's tangent from its first samples"},
    "handle-scan": {
        "task": _ROUTE,
        "change": "a fixed-tangent piece the least-squares handles miss is also tried with "
                  "every handle-length pair on a grid down to 1% of the chord, measured at "
                  "Newton-projected parameters"},
    "snap-off": {
        "task": "stop-writing-the-flat-bottom-of-a-gentle-arc-as-an-axis-snap",
        "change": "straight_line_snap off"},
    "no-move-check": {
        "task": "let-a-quantised-corner-apex-sharpen-beyond-the-corner-sharpe",
        "change": "the corner candidates' move check (the sides' meeting point within the "
                  "corner sharpening allowance of its sample) disabled, the tip skip and "
                  "the suppression distances unchanged"},
    "curl-oracle": {
        "task": "detect-corners-whose-sides-curl-at-the-apex-which-the-raster",
        "change": "every required true corner the raster certifies only beyond the corner "
                  "detector's reach (its decidable turn, with chords no longer than the "
                  "reach at an 8 px span, below the threshold) is confirmed at the chain "
                  "sample nearest it"},
    "junction-labels": {
        "task": _JUNCTION,
        "change": "rgb tier: every pixel centre within 2.5 px of a true junction takes the "
                  "inferred colour of its hard-edged render colour, with hard label costs"},
    "junction-labels-6px": {
        "task": _JUNCTION,
        "change": "junction-labels within 6 px of a true junction"},
    "unmix-third": {
        "task": _JUNCTION,
        "change": "rgb tier: an unprotected pixel with two or more colours in its 3 x 3 "
                  "neighbourhood that fits a linear-light mix of two of them better than its "
                  "own label, which is neither, takes the mix's dominant colour, with hard "
                  "label costs"},
    "true-colours": {
        "task": _THIN_COLOUR,
        "change": "rgb tier: every inferred colour standing in for a scene colour (a thin "
                  "feature's blend) takes that scene colour, its Lab costs recomputed from "
                  "the source"},
    "thin-recolour": {
        "task": _THIN_COLOUR,
        "change": "rgb tier: the inferred colour holding most of a scene colour no inferred "
                  "colour matches (and no matched colour's pixels) takes that scene colour, "
                  "its Lab costs recomputed from the source"},
    "no-despeckle": {
        "task": _THIN_COLOUR,
        "change": "rgb tier: the palette stage re-run with dither_radius 0, so its "
                  "candidates come from the source rather than its 3 x 3 median"},
    "thin-tight": {
        "task": "stop-the-trace-dropping-one-pixel-wide-label-regions",
        "change": "a chain bounding a thin pixel (in no 2 x 2 block of its own colour) is "
                  "fitted with curve_fit_tolerance and straight_line_max_deviation at most "
                  "0.2 px"},
    "thin-dense": {
        "task": "stop-the-trace-dropping-one-pixel-wide-label-regions",
        "change": "a chain bounding a thin pixel is written as its samples (the trace "
                  "stage's recovery stage 4)"},
    "fold-hard-costs": {
        "task": "keep-a-folded-colour-s-pixels-from-pulling-sub-pixel-boundar",
        "change": "rgb tier: pixels whose stored Lab cost to every palette colour exceeds "
                  "40 (a folded colour's) get hard label costs"},
    "hard-costs": {
        "task": "stop-the-rgb-tier-s-sub-pixel-boundary-crossings-putting-cus",
        "change": "rgb tier: every pixel's stored Lab costs replaced by hard label costs, so "
                  "every sub-pixel crossing sits on its pixel-edge midpoint"},
    "stroke-no-bridge": {
        "task": "synthetic-finding-stroke-detection-paints-across-gaps",
        "change": "rgb tier: the palette stage re-run with "
                  "segmentation.detect_intentional_strokes' seeds not closed (no 3 x 3 "
                  "binary closing), so ridges 1-2 px apart stay separate strokes"},
    "stroke-cost-test": {
        "task": "synthetic-finding-stroke-detection-paints-across-gaps",
        "change": "rgb tier: the palette stage re-run with every pixel "
                  "detect_intentional_strokes relabels keeping its label, unprotected, when "
                  "the stroke colour costs more than its cheapest colour plus "
                  "minimum_contrast"},
    "fold-into-touching": {
        "task": "synthetic-finding-usage-fold-plants-third-colour-strips",
        "change": "rgb tier: the palette stage re-run with "
                  "segmentation.fold_underrepresented_colours folding each pixel of a "
                  "folded colour into the surviving colour touching its region whose "
                  "centre is nearest the pixel's source Lab"},
    "truth-joins": {
        "task": "synthetic-finding-no-node-candidate-at-a-line-arc-join",
        "change": "every tangent-continuous join of two truth runs (a line meeting an arc) "
                  "within 1 px of a sample becomes a bend candidate with the truth's "
                  "tangent"},
    "no-one-arc": {
        "task": "synthetic-finding-one-arc-test-rejects-a-threshold-turn-tip",
        "change": "the corner candidates' one-arc test disabled (_ONE_ARC_RATIO 0, "
                  "_ONE_ARC_FLOOR -1)"},
    "merge-tip-corners": {
        "task": "synthetic-finding-two-corners-either-side-of-a-tip-spike",
        "change": "two confirmed corners whose cusps and samples lie within the corner "
                  "analysis span of each other are one corner, the one nearer its sample "
                  "kept"},
}


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
        return {"tasks": list(entry["tasks"]), "counterfactual": entry["counterfactual"],
                "measured": {k: entry["measured"][k] for k in bounds.MEASUREMENTS[name]},
                "identities": {k: [_rounded(item) for item in entry["identities"][k]]
                               for k in bounds.IDENTITIES[name]}}

    ordered = {key: {name: written(name, entry) for name, entry in sorted(entries.items())}
               for key, entries in sorted(table.items()) if entries}
    Path(path).write_text(_format(ordered) + "\n", encoding="utf-8")


ENTRY_KEYS = {"tasks", "counterfactual", "measured", "identities"}


def attribution_problems(tasks, counterfactual) -> list[str]:
    """What is wrong with an attribution: ``tasks`` a non-empty list of distinct
    slugs; ``counterfactual`` proxies of ``COUNTERFACTUALS`` joined by ``+`` whose
    tasks are exactly ``tasks``, or None for a failure no proxy combination clears,
    which names a single placeholder task. Every placeholder slug is reported, so
    the table test fails until a worklog task tracks its cause."""
    if not isinstance(tasks, list) or not tasks:
        return ["tasks must be a non-empty list of slugs"]
    problems = [f"task {slug!r} is not a slug" for slug in tasks
                if not isinstance(slug, str) or not SLUG_PATTERN.match(slug)]
    if problems:
        return problems
    if len(set(tasks)) != len(tasks):
        problems.append(f"tasks {tasks} repeat a slug")
    placeholders = [slug for slug in tasks if slug.startswith(PLACEHOLDER_SLUG_PREFIX)]
    problems += [f"placeholder slug {slug}; name the worklog task that tracks the defect"
                 for slug in placeholders]
    if counterfactual is None:
        if len(tasks) != 1 or not placeholders:
            problems.append(f"tasks {tasks} need the counterfactual that clears the failure; "
                            "one no proxy clears names a single placeholder")
        return problems
    if not isinstance(counterfactual, str) or not counterfactual:
        return problems + [f"counterfactual {counterfactual!r} is no proxy name"]
    names = counterfactual.split("+")
    unknown = [name for name in names if name not in COUNTERFACTUALS]
    if unknown:
        return problems + [f"unknown counterfactual proxies {unknown}"]
    if len(set(names)) != len(names):
        problems.append(f"counterfactual {counterfactual} repeats a proxy")
    covered = {COUNTERFACTUALS[name]["task"] for name in names}
    if covered != set(tasks):
        problems.append(f"counterfactual {counterfactual} stands for tasks {sorted(covered)}, "
                        f"not {sorted(tasks)}")
    return problems


def entry_problems(name: str, entry) -> list[str]:
    """What is wrong with one table entry's form (empty when it is well formed)."""
    if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
        return [f"{name}: an entry is {{\"tasks\": [slug, ...], \"counterfactual\": name, "
                f"\"measured\": {{...}}, \"identities\": {{...}}}}"]
    problems = [f"{name}: {problem}"
                for problem in attribution_problems(entry["tasks"], entry["counterfactual"])]
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
