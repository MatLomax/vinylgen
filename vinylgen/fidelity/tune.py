"""Tuning loop: search the trace lengths and corner threshold against synthetic truth.

The tuner PROPOSES; it never changes the shipped defaults. It iterates on its own
record of the settings (``asdict`` of a fresh ``TraceSettings``, or of the base
settings the caller gives), builds a new ``TraceSettings`` for every trace from
that record, and ends with a proposed best set and a report. Defaults change only
through a later reviewed commit.

Objective
---------
Every traced case gets an error score ``E`` and a node excess; an evaluation
(one settings point over a fixed case subset) averages them.

Per case, with ``r = 0.5 px + (longer side) / 1000`` (the error budget the
regression bounds use: half a pixel of quantisation plus the default maximum
deviation of 1 per mille; fixed per case, never taken from the settings under
test, so the tuner cannot widen its own yardstick)::

    h = min(10, Hausdorff / r)            worst boundary distance, in budgets
    m = min(10, mean distance / r)        typical boundary distance, in budgets
    a = min(10, (misassigned area / truth boundary length) / r)
                                           mean displacement implied by wrong area,
                                           which also catches wrong-colour fills
    c = missed required corners / max(1, required corners)
    k = min(10, log2(1 + kinks / max(1, ideal nodes)))
    t = mismatched colours / colours      topology: polygon or hole count differs
    s = 1 if the edge or overlap SVG failed the trace stage's structural validation

    E = 1 h + 2 m + 1 a + 2 c + 1 k + 4 t + 4 s

Weights: ``m`` runs at roughly a third of ``h`` on a sound trace, so doubling it
lets the whole boundary count as much as its worst point. ``a`` counts once: it
largely restates ``m`` except where colour assignment is wrong. A missed corner
on a four-corner shape (``c = 0.25``) costs 0.5, the size of the geometric error
a rounded corner leaves at the default budget. Kinks are log-compressed rather
than capped linearly: a staircase trace has tens of kinks per ideal node, and a
linear cap would make every such trace score the same, leaving the search no
slope to follow; one kink on a four-node disc costs 0.32, a kink at every
staircase step of a small disc (about 40 per ideal node) about 5.4. A topology
error or an invalid SVG is structural, so either costs 4 per case (2 for one
wrong colour of two), more than any single distance term at its budget. Distance
terms are capped at 10 budgets so one runaway case cannot swamp the mean. A case
below the resolution floor (``sweep.FEATURE_FLOOR_PX``) is scored on ``a``,
``t`` and ``s`` only. A failed trace scores ``FAILURE_ERROR``, every term at its
maximum.

Node economy: ``N`` is the geometric mean over cases of ``max(1, boundary nodes
/ ideal nodes)`` (cases with no ideal nodes are left out). The ideal count is
already the fewest nodes that represent the truth exactly, so fewer earns no
credit: the error terms judge what the missing nodes cost. The scalar objective
at node weight ``w`` is ``J = E + w * log2(N)``: doubling the node excess costs
``w`` error units. Every evaluation keeps its ``(E, N)`` pair, and the Pareto
front over them is reported, so the trade-off can be re-read at any weight
without re-tracing.

Search
------
A deterministic compass (pattern) search from the base settings on an integer
lattice: a length is ``start * 2 ** (i / 8)`` and the corner threshold
``start + 2.5 * i`` degrees for integer ``i``. Steps start at a factor of 2 for a
length (8 units) and 10 degrees for the threshold (4 units), and halve to one
unit (a factor of about 1.09; 2.5 degrees). Each sweep tries every parameter up then down and moves on the
first improvement of ``J``; a sweep with no improvement halves every step. Moves
are clipped to each parameter's range. Why this rather than CMA-ES: evaluations
are expensive (each is a batch of real traces), so the budget is tens to a few
hundred points, below what CMA-ES needs to adapt a covariance in 9 dimensions
(its population alone is about 10); compass search makes progress from the
first evaluation, starts from the shipped defaults so the proposal is a small
readable diff, needs no randomness, and respects the box bounds by clipping. The
first step is a factor of 2 because sub-pixel lengths on small images move the
trace only in whole-pixel jumps, so a finer first step mostly lands on the same
plateau.

Evaluations are memoised by the full effective settings they trace with, and
appended to ``evaluations.jsonl`` as they finish, each with the run's
fingerprint (case ids, tiers, cleanup and the trace path's source hash).
``--resume`` replays the same deterministic search against that file, so every
evaluated point is served from disk and the search continues exactly where it
stopped; a file from a different fingerprint is refused.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from vinylgen.contracts import (
    CURVE_FEATURE_SCALE_RANGE,
    LENGTH_UNIT_PER_MILLE,
    TRACE_LENGTH_FIELDS,
    CleanupSettings,
    TraceSettings,
)
from vinylgen.fidelity import sweep

ROOT = sweep.ROOT
SEEDS_FILE = ROOT / "tests" / "synthetic" / "fuzz-seeds.json"
IMAGES_DIR = ROOT / "images"

DISTANCE_CAP = 10.0
KINK_CAP = 10.0
WEIGHTS = {"hausdorff": 1.0, "mean": 2.0, "area": 1.0, "corners": 2.0, "kinks": 1.0,
           "topology": 4.0, "structural": 4.0}
FAILURE_ERROR = (DISTANCE_CAP * (WEIGHTS["hausdorff"] + WEIGHTS["mean"] + WEIGHTS["area"])
                 + KINK_CAP * WEIGHTS["kinks"]
                 + WEIGHTS["corners"] + WEIGHTS["topology"] + WEIGHTS["structural"])
DEFAULT_NODE_WEIGHT = 1.0


# --- objective --------------------------------------------------------------------------

def error_reference_px(size: int) -> float:
    """The per-case error budget ``r`` in px (module docstring)."""
    return 0.5 + float(size) / 1000.0


def error_terms(row: dict) -> dict:
    """The objective's terms for one result row (module docstring)."""
    if not row["ok"]:
        return {"failed": True}
    s = row["summary"]
    ref = error_reference_px(row["size"])

    def distance(value):
        return DISTANCE_CAP if value is None else min(DISTANCE_CAP, float(value) / ref)

    if s["boundary_length_px"] > 0:
        area = distance(s["area_offset_px"])
    else:
        area = DISTANCE_CAP if s["misassigned_px"] > 0.5 else 0.0
    terms = {"area": area,
             "topology": s["topology_mismatches"] / s["colours"] if s["colours"] else 0.0,
             "structural": 1.0 if s.get("structural_failures") else 0.0}
    if not row.get("sub_resolution"):
        terms.update(
            hausdorff=distance(s["hausdorff_px"]), mean=distance(s["mean_px"]),
            corners=(s["required_corners"] - s["required_found"]) / max(1, s["required_corners"]),
            kinks=min(KINK_CAP, math.log2(1.0 + s["kinks"] / max(1, s["ideal_nodes"]))))
    return terms


def case_error(row: dict) -> float:
    terms = error_terms(row)
    if terms.get("failed"):
        return FAILURE_ERROR
    return float(sum(WEIGHTS[name] * value for name, value in terms.items()))


def case_log_node_excess(row: dict) -> float | None:
    """ln(max(1, boundary nodes / ideal nodes)), or None for a failed trace or a
    truth with no ideal nodes."""
    if not row["ok"]:
        return None
    s = row["summary"]
    if not s["ideal_nodes"]:
        return None
    return math.log(max(1.0, s["nodes"] / s["ideal_nodes"]))


def score_rows(rows: list[dict]) -> dict:
    """An evaluation's error ``E`` (mean case error) and node excess ``N``
    (geometric mean of ``max(1, boundary / ideal nodes)``)."""
    if not rows:
        raise ValueError("an evaluation needs at least one traced case")
    errors = [case_error(row) for row in rows]
    logs = [value for value in (case_log_node_excess(row) for row in rows) if value is not None]
    return {"error": float(sum(errors) / len(errors)),
            "nodes": float(math.exp(sum(logs) / len(logs))) if logs else 1.0,
            "failures": sum(1 for row in rows if not row["ok"])}


def objective(error: float, nodes: float, node_weight: float) -> float:
    """``J = E + w * log2(N)``."""
    return float(error + node_weight * math.log2(max(nodes, 1e-12)))


def pareto_front(points: list[tuple[float, float]]) -> list[int]:
    """Indices of the points no other point dominates (both coordinates
    minimised; dominating means no worse in both and better in one), ordered by
    the first coordinate. Exact duplicates are all kept."""
    front = []
    for i, (e_i, n_i) in enumerate(points):
        dominated = any(e_j <= e_i and n_j <= n_i and (e_j < e_i or n_j < n_i)
                        for j, (e_j, n_j) in enumerate(points) if j != i)
        if not dominated:
            front.append(i)
    return sorted(front, key=lambda i: (points[i][0], points[i][1], i))


# --- parameters -------------------------------------------------------------------------

@dataclass(frozen=True)
class Parameter:
    """A searched setting. ``unit`` is one lattice step: a log2 factor for a
    length, degrees for the corner threshold. ``step`` is the first step in
    lattice units."""

    name: str
    low: float
    high: float
    log: bool
    unit: float
    step: int


_LENGTH_RANGES = {
    "curve_fit_tolerance": (0.05, 10.0), "straight_line_max_deviation": (0.05, 10.0),
    "minimum_straight_length": (0.1, 40.0), "max_deviation": (0.05, 10.0),
    "side_smoothness": (0.05, 10.0), "corner_sharpening_allowance": (0.05, 10.0),
    "corner_analysis_span": (0.25, 40.0), "curve_feature_scale": CURVE_FEATURE_SCALE_RANGE}
PARAMETERS = tuple(
    [Parameter(name, *_LENGTH_RANGES[name], log=True, unit=0.125, step=8)
     for name in TRACE_LENGTH_FIELDS]
    + [Parameter("corner_threshold", 10.0, 170.0, log=False, unit=2.5, step=4)])
PARAMETER_NAMES = tuple(parameter.name for parameter in PARAMETERS)


def base_record(settings: TraceSettings | None = None) -> dict:
    """The tuner's own settings record: the effective ``asdict`` of a fresh copy
    (automatic and following lengths filled in), lengths in thousandths."""
    settings = TraceSettings() if settings is None else TraceSettings(**asdict(settings))
    if settings.length_unit != LENGTH_UNIT_PER_MILLE:
        raise ValueError("the tuner's base settings must be in per_mille (thousandths); "
                         "convert a pixel-era config first")
    return sweep.effective_record(settings)


def start_values(record: dict) -> dict:
    """The searched parameters' values in a (base) record."""
    return {name: float(record[name]) for name in PARAMETER_NAMES}


def lattice_value(parameter: Parameter, start: float, index: int) -> float:
    if parameter.log:
        return float(start * 2.0 ** (index * parameter.unit))
    return float(start + index * parameter.unit)


def lattice_bounds(parameter: Parameter, start: float) -> tuple[int, int]:
    """The lattice indices whose values lie within the parameter's range (the
    start itself is always allowed, even when a base config set it outside)."""
    if parameter.log:
        low = math.ceil(math.log2(parameter.low / start) / parameter.unit - 1e-9)
        high = math.floor(math.log2(parameter.high / start) / parameter.unit + 1e-9)
    else:
        low = math.ceil((parameter.low - start) / parameter.unit - 1e-9)
        high = math.floor((parameter.high - start) / parameter.unit + 1e-9)
    return min(low, 0), max(high, 0)


def lattice_values(lattice: dict, starts: dict, parameters=PARAMETERS) -> dict:
    return {p.name: lattice_value(p, starts[p.name], lattice[p.name]) for p in parameters}


def settings_for(record: dict, values: dict) -> TraceSettings:
    """A new ``TraceSettings`` for searched ``values``; ``record`` is copied, never
    changed."""
    merged = json.loads(json.dumps(record))
    merged.update(values)
    return TraceSettings(**merged)


def settings_key(settings: TraceSettings) -> str:
    """The memo key of a traced settings set: its full effective record."""
    return json.dumps(sweep.effective_record(settings), sort_keys=True)


def settings_diff(settings: TraceSettings, reference: TraceSettings) -> dict:
    """Effective fields that differ: ``{name: [reference value, value]}``."""
    new, old = sweep.effective_record(settings), sweep.effective_record(reference)
    return {name: [old[name], new[name]] for name in new if old.get(name) != new[name]}


def proposed_config(settings: TraceSettings, cleanup: CleanupSettings | None) -> dict:
    """The proposed set as a trace ``--config`` block: ``length_unit``, every
    searched value (explicit, so no length silently follows another it was not
    evaluated with), every other field that differs from the shipped defaults,
    and the cleanup settings the run traced with when they are not the defaults."""
    record = sweep.effective_record(settings)
    config = {"length_unit": LENGTH_UNIT_PER_MILLE,
              **{name: record[name] for name in PARAMETER_NAMES},
              **{name: new for name, (_, new) in settings_diff(settings, TraceSettings()).items()}}
    changed = sweep.cleanup_changed(cleanup)
    if changed:
        config["cleanup_settings"] = asdict(cleanup)
    return config


# --- evaluation -------------------------------------------------------------------------

def fingerprint(cases, tiers, cleanup: CleanupSettings | None, code: str) -> str:
    """What an evaluation's numbers depend on besides its settings."""
    payload = {"cases": [case.case_id for case in cases], "tiers": list(tiers),
               "cleanup": asdict(cleanup) if cleanup is not None else None, "code": code}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _stored_row(row: dict) -> dict:
    """The part of a result row the objective reads, as stored per evaluation."""
    return {key: row.get(key) for key in ("case_id", "tier", "family", "size", "ok", "error",
                                          "sub_resolution", "summary")}


class Evaluator:
    """Evaluates settings on a fixed case subset, memoised on disk.

    ``evaluations.jsonl`` gets one line per new evaluation as it finishes; a
    settings set already in it is never traced again. Scores are recomputed from
    the stored per-case numbers on load, so they always follow the objective in
    this module."""

    def __init__(self, cases, tiers, out_dir: Path, *, cleanup: CleanupSettings | None = None,
                 pool=None, jobs: int = 1, node_weight: float = DEFAULT_NODE_WEIGHT,
                 max_evals: int | None = None, code: str | None = None, resume: bool = False,
                 log=print):
        self.cases, self.tiers = list(cases), list(tiers)
        if not self.cases:
            raise ValueError("the tuner needs at least one training case")
        self.cleanup = cleanup
        self.out_dir = Path(out_dir)
        self.pool, self.jobs, self.node_weight = pool, jobs, node_weight
        self.max_evals = max_evals
        self.log = log
        self.fingerprint = fingerprint(self.cases, self.tiers, cleanup,
                                       sweep.code_hash() if code is None else code)
        self.path = self.out_dir / "evaluations.jsonl"
        self.evaluations: list[dict] = []
        self.memo: dict[str, dict] = {}
        self.requested: list[str] = []
        if self.path.exists():
            if not resume:
                raise ValueError(f"{self.path} exists: resume that run or choose a new directory")
            for line in self.path.read_text().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("fingerprint") != self.fingerprint:
                    raise ValueError(
                        f"{self.path} was evaluated with other cases, tiers, cleanup or trace "
                        f"code (fingerprint {entry.get('fingerprint')}, now {self.fingerprint}); "
                        "start a new run")
                entry.update(score_rows(entry["cases"]))
                entry["objective"] = objective(entry["error"], entry["nodes"], node_weight)
                self.evaluations.append(entry)
                self.memo[entry["key"]] = entry

    @property
    def budget_left(self) -> bool:
        return self.max_evals is None or len(self.evaluations) < self.max_evals

    def __call__(self, settings: TraceSettings, note: dict | None = None,
                 lattice: dict | None = None) -> dict | None:
        key = settings_key(settings)
        self.requested.append(key)
        if key in self.memo:
            return self.memo[key]
        if not self.budget_left:
            return None
        index = len(self.evaluations)
        started = time.perf_counter()
        work = self.out_dir / "work" / f"eval-{index:04d}"
        rows = sweep.run_cases(self.cases, self.tiers, TraceSettings(**asdict(settings)), work,
                               cleanup=self.cleanup, pool=self.pool, jobs=self.jobs, keep=False)
        shutil.rmtree(work, ignore_errors=True)
        died = [row for row in rows if str(row.get("error") or "").startswith(sweep.WORKER_DIED)]
        if died:
            raise RuntimeError(f"a worker process died during evaluation {index} "
                               f"({died[0]['case_id']}); resume the run to continue")
        score = score_rows(rows)
        entry = {"index": index, "key": key, "fingerprint": self.fingerprint,
                 "values": start_values(sweep.effective_record(settings)),
                 "settings": sweep.effective_record(settings), "lattice": lattice, **score,
                 "objective": objective(score["error"], score["nodes"], self.node_weight),
                 "seconds": time.perf_counter() - started, "search": note or {},
                 "cases": [_stored_row(row) for row in rows]}
        self.evaluations.append(entry)
        self.memo[key] = entry
        with self.path.open("a") as handle:
            handle.write(json.dumps(entry) + "\n")
        self.log(f"eval {index}: E={entry['error']:.4f} N={entry['nodes']:.3f} "
                 f"J={entry['objective']:.4f} failures={entry['failures']} "
                 f"({entry['seconds']:.1f}s) {json.dumps(note or {})}")
        return entry


def compass_search(evaluate: Evaluator, record: dict, parameters=PARAMETERS) -> dict:
    """The deterministic compass search from ``record`` (module docstring);
    returns the best evaluation. Stops when a new point is needed and the budget
    is spent, or every step is below one lattice unit. Points already evaluated
    cost nothing, so a resumed search replays to where it stopped."""
    starts = start_values(record)
    bounds = {p.name: lattice_bounds(p, starts[p.name]) for p in parameters}
    lattice = {p.name: 0 for p in parameters}

    def evaluate_at(point, note):
        values = lattice_values(point, starts, parameters)
        return evaluate(settings_for(record, values), note, lattice=dict(point))

    def score(entry):
        return objective(entry["error"], entry["nodes"], evaluate.node_weight)

    current = evaluate_at(lattice, {"phase": "start"})
    if current is None:
        raise ValueError("the evaluation budget allows no evaluation")
    steps = {p.name: p.step for p in parameters}
    sweep_index = 0
    while any(steps[p.name] >= 1 for p in parameters):
        improved = False
        for parameter in parameters:
            step = steps[parameter.name]
            if step < 1:
                continue
            low, high = bounds[parameter.name]
            for direction in (1, -1):
                target = min(high, max(low, lattice[parameter.name] + direction * step))
                if target == lattice[parameter.name]:
                    continue
                candidate = {**lattice, parameter.name: target}
                entry = evaluate_at(candidate, {
                    "phase": "search", "sweep": sweep_index, "parameter": parameter.name,
                    "step": direction * step, "from": current["index"]})
                if entry is None:
                    return current
                if score(entry) < score(current) - 1e-12:
                    lattice, current = candidate, entry
                    improved = True
                    break
        if not improved:
            steps = {name: step // 2 for name, step in steps.items()}
        sweep_index += 1
    return current


def no_effect_parameters(evaluations: list[dict]) -> list[str]:
    """Searched parameters every one of whose moves scored exactly as the point
    it moved from: they had no effect on the training cases (typically lengths
    that resolve below a floor on small images)."""
    by_index = {entry["index"]: entry for entry in evaluations}
    moved: dict[str, bool] = {}
    for entry in evaluations:
        note = entry.get("search") or {}
        if note.get("phase") != "search" or note.get("from") not in by_index:
            continue
        origin = by_index[note["from"]]
        same = (entry["error"] == origin["error"] and entry["nodes"] == origin["nodes"])
        moved[note["parameter"]] = moved.get(note["parameter"], False) or not same
    return sorted(name for name, changed in moved.items() if not changed)


# --- case sets --------------------------------------------------------------------------

def seed_ranges(path: Path = SEEDS_FILE) -> dict[str, range]:
    data = json.loads(Path(path).read_text())
    return {name: range(*data["tuning"][name]) for name in ("train", "held_out")}


def seed_size(seed: int, sizes: list[int]) -> int:
    """A fuzzer seed's canvas size: one of ``sizes``, fixed by the seed (a
    multiplicative hash, independent of the fuzzer's own draws)."""
    return int(sizes[((int(seed) * 2654435761) % 2 ** 32) % len(sizes)])


def training_pool(fixed_cases, train_seeds, sizes: list[int]) -> list[list]:
    """``["fixed", case_id]`` and ``["fuzz", seed, size]`` entries, in a stable order."""
    return ([["fixed", case.case_id] for case in fixed_cases]
            + [["fuzz", int(seed), seed_size(seed, sizes)] for seed in train_seeds])


def sample_pool(pool: list, count: int | None, seed: int) -> list:
    """A fixed random subset (the same for every evaluation); the whole pool when
    ``count`` is None or not smaller."""
    if count is None or count >= len(pool):
        return list(pool)
    chosen = random.Random(seed).sample(range(len(pool)), count)
    return [pool[index] for index in sorted(chosen)]


def build_cases(entries, fixed_by_id: dict, pool=None) -> list:
    fuzz = [(entry[1], entry[2]) for entry in entries if entry[0] == "fuzz"]
    built = iter(sweep.fuzz_cases([seed for seed, _ in fuzz], [size for _, size in fuzz],
                                  pool=pool))
    return [fixed_by_id[entry[1]] if entry[0] == "fixed" else next(built) for entry in entries]


# --- held-out before/after ------------------------------------------------------------------

def family_scores(rows: list[dict]) -> dict:
    """Per family (and ``all``): cases, E, N and the headline sweep numbers."""
    groups: dict[str, list[dict]] = {"all": list(rows)}
    for row in rows:
        groups.setdefault(row["family"], []).append(row)
    result = {}
    for name, members in groups.items():
        stats = sweep.group_stats(members)
        result[name] = {**score_rows(members), "cases": len(members),
                        "hausdorff_median_px": stats["hausdorff_px"]["median"],
                        "hausdorff_max_px": stats["hausdorff_px"]["max"],
                        "mean_boundary_px": stats["mean_boundary_px"],
                        "required_corner_recall": stats["required_corner_recall"],
                        "kinks": stats["kinks"]["total"],
                        "topology_mismatches": stats["topology"]["cases_mismatched"],
                        "structural_failures": stats["trace_audit"]["structural_failures"]}
    return result


# --- real images ------------------------------------------------------------------------

def _real_palette(image: str, subject_dir: str) -> str:
    """The real palette stage for one image into ``subject_dir/palette``."""
    from vinylgen.contracts import PaletteSettings
    from vinylgen.palette import infer_palette
    image, subject_dir = Path(image), Path(subject_dir)
    subject_dir.mkdir(parents=True, exist_ok=True)
    infer_palette(image, subject_dir, PaletteSettings())
    target = subject_dir / "palette"
    shutil.rmtree(target, ignore_errors=True)
    (subject_dir / image.stem).rename(target)
    return str(target)


_PALETTE_FILES = ("source.png", "flattened.png", "segmentation.npz", "palette.json")


def _real_trace(palette_dir: str, target: str, settings: dict, cleanup: dict | None) -> dict:
    """The real trace stage (which renders ``traced.png``) into ``target``, with
    the cleanup applied inline as ``svg_trace.py`` applies it."""
    from vinylgen.tracing import trace_subject
    palette_dir, target = Path(palette_dir), Path(target)
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    for name in _PALETTE_FILES:
        shutil.copy2(palette_dir / name, target / name)
    started = time.perf_counter()
    audit = trace_subject(target, TraceSettings(**settings), {},
                          cleanup=CleanupSettings(**cleanup) if cleanup else None,
                          clean_inline=bool(cleanup))
    return {"dir": str(target), "seconds": time.perf_counter() - started,
            "edge_nodes": int(sum(layer["nodes"] for layer in audit["geometry"]["edge"])),
            "valid": all(audit["validation"][variant]["passed"] for variant in ("edge", "overlap")),
            "warnings": len(audit["warnings"])}


def trace_real_images(images: list[Path], variants: dict[str, TraceSettings], out_dir: Path,
                      pool=None, cleanup: CleanupSettings | None = None) -> dict:
    """Every image through the palette stage once, then the trace stage once per
    variant, into ``out_dir/<subject>/<variant>/``; returns per-subject results."""
    out_dir = Path(out_dir)

    def run(function, *args):
        if pool is not None:
            return pool.submit(function, *args)
        try:
            return _Done(function(*args))
        except Exception as error:
            return _Done(error=error)

    palettes = {image.stem: run(_real_palette, str(image), str(out_dir / image.stem))
                for image in images}
    results: dict[str, dict] = {}
    traces = {}
    for subject, future in palettes.items():
        try:
            palette_dir = future.result()
        except Exception as error:  # reported per subject, never aborts the report
            results[subject] = {label: {"error": f"palette stage: {type(error).__name__}: {error}"}
                                for label in variants}
            continue
        for label, settings in variants.items():
            traces[(subject, label)] = run(_real_trace, palette_dir,
                                           str(out_dir / subject / label), asdict(settings),
                                           asdict(cleanup) if cleanup is not None else None)
    for (subject, label), future in traces.items():
        try:
            results.setdefault(subject, {})[label] = future.result()
        except Exception as error:  # reported per subject, never aborts the report
            results.setdefault(subject, {})[label] = {"error": f"{type(error).__name__}: {error}"}
    return results


class _Done:
    """A finished in-process call with the ``result()`` of a future."""

    def __init__(self, value=None, error: Exception | None = None):
        self.value, self.error = value, error

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


def contact_sheet(out_dir: Path, results: dict, labels: tuple[str, ...], path: Path,
                  cell_width: int = 560) -> Path:
    """One row per subject: the flattened image, then each variant's ``traced.png``."""
    from PIL import Image, ImageDraw
    columns = ("flattened", *labels)
    rows = []
    for subject in sorted(results):
        cells = []
        for column in columns:
            source = (out_dir / subject / "palette" / "flattened.png" if column == "flattened"
                      else out_dir / subject / column / "traced.png")
            if source.exists():
                image = Image.open(source).convert("RGB")
                image = image.resize((cell_width, round(image.height * cell_width / image.width)),
                                     Image.LANCZOS)
            else:
                image = Image.new("RGB", (cell_width, cell_width * 2 // 3), (60, 60, 60))
            cells.append(image)
        rows.append((subject, cells))
    header, caption, gap = 28, 24, 8
    height = header + sum(caption + max(cell.height for cell in cells) + gap for _, cells in rows)
    width = len(columns) * (cell_width + gap) + gap
    sheet = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    for index, column in enumerate(columns):
        draw.text((gap + index * (cell_width + gap), 8), column, fill=(20, 20, 20))
    y = header
    for subject, cells in rows:
        notes = [subject]
        for label in labels:
            entry = results[subject].get(label, {})
            notes.append(f"{label}: {entry.get('edge_nodes', 'failed')} nodes")
        draw.text((gap, y + 6), "   ".join(notes), fill=(20, 20, 20))
        y += caption
        for index, cell in enumerate(cells):
            sheet.paste(cell, (gap + index * (cell_width + gap), y))
        y += max(cell.height for cell in cells) + gap
    sheet.save(path)
    return path


# --- plot -------------------------------------------------------------------------------

def pareto_plot(evaluations: list[dict], front: list[int], current: int,
                proposed: int, path: Path, size=(900, 600)) -> Path:
    """Error against node ratio for every evaluation, the front joined in order,
    the current defaults and the proposal labelled. Drawn with PIL (matplotlib
    is not a project dependency)."""
    from PIL import Image, ImageDraw
    width, height = size
    left, right, top, bottom = 80, 30, 40, 60
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    xs = [entry["nodes"] for entry in evaluations]
    ys = [entry["error"] for entry in evaluations]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad_x, pad_y = (x1 - x0) * 0.08 or 0.1, (y1 - y0) * 0.08 or 0.1
    x0, x1, y0, y1 = x0 - pad_x, x1 + pad_x, y0 - pad_y, y1 + pad_y

    def at(x, y):
        return (left + (x - x0) / (x1 - x0) * (width - left - right),
                top + (y1 - y) / (y1 - y0) * (height - top - bottom))

    grid, ink, muted = (232, 232, 232), (30, 30, 30), (110, 110, 110)

    def tick(value, span):
        # Enough decimals that neighbouring ticks (a fifth of the span apart) differ.
        decimals = max(0, min(6, 1 - math.floor(math.log10(span / 5))))
        return f"{value:.{decimals}f}"

    for i in range(6):
        gx = x0 + (x1 - x0) * i / 5
        gy = y0 + (y1 - y0) * i / 5
        px, _ = at(gx, y0)
        _, py = at(x0, gy)
        draw.line([(px, top), (px, height - bottom)], fill=grid)
        draw.line([(left, py), (width - right, py)], fill=grid)
        draw.text((px - 14, height - bottom + 8), tick(gx, x1 - x0), fill=muted)
        draw.text((left - 60, py - 6), tick(gy, y1 - y0), fill=muted)
    draw.text((width // 2 - 150, height - 24), "node excess N over ideal (lower is better)",
              fill=ink)
    draw.text((10, 10), "error E (lower is better); grey: evaluated, blue: Pareto front", fill=ink)
    for entry in evaluations:
        px, py = at(entry["nodes"], entry["error"])
        draw.ellipse([px - 4, py - 4, px + 4, py + 4], fill=(185, 185, 185))
    front_points = sorted((evaluations[i]["nodes"], evaluations[i]["error"]) for i in front)
    if len(front_points) > 1:
        draw.line([at(*point) for point in front_points], fill=(37, 99, 235), width=2)
    for x, y in front_points:
        px, py = at(x, y)
        draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill=(37, 99, 235),
                     outline=(255, 255, 255), width=2)
    for index, label in ((current, "current defaults"), (proposed, "proposed")):
        if index is None:
            continue
        px, py = at(evaluations[index]["nodes"], evaluations[index]["error"])
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], outline=ink, width=2)
        draw.text((px + 10, py - (18 if label == "proposed" else -4)), label, fill=ink)
    image.save(path)
    return path


# --- report -----------------------------------------------------------------------------

def _f(value, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}g}"


def _record_changes(record: dict, reference: dict) -> dict:
    return {name: value for name, value in record.items() if reference.get(name) != value}


def report_markdown(meta: dict, evaluations: list[dict], front: list[int], current_index: int,
                    proposed_index: int, diff: dict, weights_table: list[dict],
                    held_out: dict | None, real: dict | None) -> str:
    proposed, current = evaluations[proposed_index], evaluations[current_index]
    defaults = sweep.effective_record(TraceSettings())
    headline = (f"Proposed set: training error E {_f(current['error'])} -> {_f(proposed['error'])}, "
                f"node excess N {_f(current['nodes'])} -> {_f(proposed['nodes'])}, objective J "
                f"{_f(current['objective'])} -> {_f(proposed['objective'])} at node weight "
                f"{meta['node_weight']:g}")
    if held_out:
        before, after = held_out["current"]["all"], held_out["proposed"]["all"]
        headline += (f"; held-out E {_f(before['error'])} -> {_f(after['error'])}, "
                     f"N {_f(before['nodes'])} -> {_f(after['nodes'])}")
    budget = f"budget {meta['max_evals']}"
    if meta.get("extra_current_eval"):
        budget += ", plus one for the current defaults, which the search did not start from"
    lines = ["# Trace tuning proposal", "", f"**{headline}.**", "",
             "This is a proposal. The shipped defaults are unchanged; they change only through a "
             "reviewed commit.", "",
             f"- Evaluations: {len(evaluations)} ({budget}); cases per evaluation: "
             f"{meta['cases_per_eval']} ({meta['tier']} tier); jobs: {meta['jobs']}; wall time: "
             f"{_f(meta.get('wall_seconds'), 5)} s",
             f"- Training pool: fixed set ({meta['fixed_count']} cases) + fuzz seeds "
             f"{meta['train']} at sizes {meta['fuzz_sizes']} px; subset seed {meta['subset_seed']}",
             f"- Held-out: fuzz seeds {meta['held_out']} ({meta['held_out_count']} cases), never "
             "trained on",
             f"- Case source: {meta['case_source']}; trace code hash `{meta['code_hash']}`",
             "- Objective (vinylgen/fidelity/tune.py): per case E = 1 h + 2 m + 1 a + 2 c + 1 k + "
             "4 t + 4 s (budget-normalised Hausdorff, mean distance and area displacement, missed "
             "required corners, log-compressed kinks per ideal node, topology mismatches, "
             "structural SVG failures); N = geometric mean of max(1, boundary / ideal nodes); "
             f"J = E + w log2 N with w = {meta['node_weight']:g}.", ""]
    if meta.get("no_effect"):
        lines += [f"- No effect on the training cases (every move scored exactly as its origin): "
                  f"{', '.join(meta['no_effect'])}. On small canvases these lengths resolve below "
                  "their pixel floor; their proposed values are untested at larger sizes.", ""]
    lines += ["## Proposed settings diff", "",
              "Trace lengths in thousandths of the longer side, corner threshold in degrees.", ""]
    if diff:
        lines += ["| setting | current | proposed |", "|---|---|---|"]
        lines += [f"| {name} | {_f(old)} | {_f(new)} |" for name, (old, new) in diff.items()]
        lines += ["", "The proposal is a set of explicit values. Shipping it by editing the default "
                  "tables instead (`PER_MILLE_DEFAULTS`, `AUTOMATIC_LENGTHS`) also moves the lengths "
                  "that follow them (`side_smoothness` follows `straight_line_max_deviation`, "
                  "`corner_sharpening_allowance` follows `max_deviation`) unless those are set "
                  "explicitly too."]
    else:
        lines.append("No change: no evaluated point beat the current defaults at this weight.")
    lines += ["", "As a trace config (`svg_trace.py --config`, also `proposed-trace.json`):", "",
              "```json", json.dumps(meta["proposed_config"], indent=2), "```", ""]
    lines += ["## Accuracy / node-count trade-off (Pareto front on the training subset)", "",
              "![Pareto front](pareto.png)", "",
              "| eval | E | N | J | failures | changed from current defaults |",
              "|---|---|---|---|---|---|"]
    for index in front:
        entry = evaluations[index]
        changes = _record_changes(entry["settings"], defaults)
        mark = " (proposed)" if index == proposed_index else ""
        mark += " (current)" if index == current_index else ""
        lines.append(f"| {entry['index']}{mark} | {_f(entry['error'])} | {_f(entry['nodes'])} | "
                     f"{_f(entry['objective'])} | {entry['failures']} | "
                     + (", ".join(f"{k} {_f(v)}" for k, v in changes.items()) or "-") + " |")
    lines += ["", "Best evaluated point at other node weights:", "",
              "| node weight | eval | E | N |", "|---|---|---|---|"]
    lines += [f"| {row['weight']:g} | {row['index']} | {_f(row['error'])} | {_f(row['nodes'])} |"
              for row in weights_table]
    lines.append("")
    if held_out:
        lines += ["## Held-out before/after (fuzz seeds not used in training)", "",
                  "Each cell is current -> proposed.", "",
                  "| family | cases | E | N | Hausdorff median px | required corner recall | "
                  "kinks | topology mismatches | structural failures |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for family in held_out["current"]:
            b, a = held_out["current"][family], held_out["proposed"].get(family, {})

            def pair(key):
                return f"{_f(b.get(key))} -> {_f(a.get(key))}"

            lines.append(f"| {family} | {b['cases']} | {pair('error')} | {pair('nodes')} | "
                         f"{pair('hausdorff_median_px')} | {pair('required_corner_recall')} | "
                         f"{pair('kinks')} | {pair('topology_mismatches')} | "
                         f"{pair('structural_failures')} |")
        lines += ["", "Full sweeps: `held-out/current/summary.md`, `held-out/proposed/summary.md`.", ""]
    else:
        lines += ["## Held-out before/after", "", "Skipped (no held-out cases).", ""]
    if real:
        lines += ["## Real corpus before/after (visual inspection)", "",
                  "Each image ran through the real palette stage once, then the trace stage with "
                  "the current defaults and with the proposed set (and the run's cleanup "
                  "settings). Previews: `real-images/<subject>/{current,proposed}/traced.png`; "
                  "side by side: `real-images/contact.png` (flattened | current | proposed).", "",
                  "| subject | edge nodes | structurally valid | trace s |", "|---|---|---|---|"]
        for subject in sorted(real):
            b, a = real[subject].get("current", {}), real[subject].get("proposed", {})

            def cell(entry, key, digits=4):
                if "error" in entry:
                    return f"failed ({entry['error']})"
                value = entry.get(key)
                return str(value) if isinstance(value, bool) else _f(value, digits)

            lines.append(f"| {subject} | {cell(b, 'edge_nodes')} -> {cell(a, 'edge_nodes')} | "
                         f"{cell(b, 'valid')} -> {cell(a, 'valid')} | "
                         f"{cell(b, 'seconds', 3)} -> {cell(a, 'seconds', 3)} |")
        lines.append("")
    else:
        lines += ["## Real corpus before/after", "", "Skipped (--no-real-images).", ""]
    return "\n".join(lines)
