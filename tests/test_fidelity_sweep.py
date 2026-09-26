import contextlib
import copy
import io
import json
import math
import tempfile
import unittest
from dataclasses import asdict, fields
from pathlib import Path
from unittest import mock

from vinylgen import contracts
from vinylgen.contracts import TraceSettings
from vinylgen.fidelity import sweep, tune
from vinylgen.fidelity.cases import Case
from vinylgen.fidelity.scene import Region, Scene, circle, rectangle

ROOT = Path(__file__).resolve().parents[1]
PALETTE = [{"name": "Ground", "rgb": [240, 235, 220]}, {"name": "Ink", "rgb": [30, 40, 90]}]


def summary(**overrides) -> dict:
    values = {"hausdorff_px": 1.0, "mean_px": 0.3, "p95_px": 0.6, "hausdorff_per_mille": 1.0,
              "misassigned_px": 30.0, "misassigned_fraction": 0.01, "boundary_length_px": 100.0,
              "area_offset_px": 0.3, "corners_true": 4, "corners_found": 4, "corner_recall": 1.0,
              "required_corners": 4, "required_found": 4, "kinks": 0, "nodes": 8,
              "ideal_nodes": 4, "node_ratio": 2.0, "colours": 2, "topology_mismatches": 0,
              "unmatched_layers": 0, "structural_failures": 0, "semantic_passed": True,
              "dense_fallbacks": 0, "warnings": 0}
    values.update(overrides)
    if "area_offset_px" not in overrides and values["boundary_length_px"]:
        values["area_offset_px"] = values["misassigned_px"] / values["boundary_length_px"]
    return values


def row(case_id="c", family="disc", tags=("disc",), size=500, ok=True, sub_resolution=False,
        tier="labels", **overrides) -> dict:
    result = {"case_id": case_id, "tier": tier, "family": family, "tags": list(tags),
              "size": size, "ok": ok, "error": None if ok else "RuntimeError: boom",
              "sub_resolution": sub_resolution, "seconds": 1.0, "trace_seconds": 0.5}
    if ok:
        result["summary"] = summary(**overrides)
    return result


def hand_built_cases(size=96) -> list[Case]:
    square = Scene(size, size, PALETTE, 0,
                   [Region(1, rectangle(0.25 * size + 0.3, 0.25 * size + 0.3,
                                        0.75 * size - 0.4, 0.7 * size))], family="square")
    disc = Scene(size, size, PALETTE, 0, [Region(1, circle((size / 2 + 0.2, size / 2 - 0.3),
                                                          0.3 * size))], family="disc")
    return [Case(f"square-{size}", square, 0.45 * size, ("square", "scaled")),
            Case(f"disc-{size}", disc, 0.6 * size, ("disc", "scaled"))]


class AggregationTests(unittest.TestCase):
    def test_group_stats(self):
        rows = [row("a", hausdorff_px=1.0, mean_px=0.2, misassigned_fraction=0.01, node_ratio=2.0,
                    corners_true=4, corners_found=3, required_corners=4, required_found=3, kinks=1),
                row("b", hausdorff_px=3.0, mean_px=0.4, misassigned_fraction=0.03, node_ratio=8.0,
                    corners_true=2, corners_found=2, required_corners=0, required_found=0, kinks=2,
                    topology_mismatches=1),
                row("c", hausdorff_px=2.0, mean_px=0.6, misassigned_fraction=0.02, node_ratio=0.5,
                    corners_true=0, corners_found=0, required_corners=0, required_found=0),
                row("d", ok=False)]
        stats = sweep.group_stats(rows)
        self.assertEqual((stats["cases"], stats["failed"]), (4, 1))
        self.assertAlmostEqual(stats["hausdorff_px"]["median"], 2.0)
        self.assertAlmostEqual(stats["hausdorff_px"]["max"], 3.0)
        self.assertAlmostEqual(stats["hausdorff_px"]["p95"], 2.9)  # linear: 2 + 0.9 * (3 - 2)
        self.assertAlmostEqual(stats["mean_boundary_px"], 0.4)
        self.assertAlmostEqual(stats["misassigned_fraction"], 0.02)
        self.assertAlmostEqual(stats["corner_recall"], 5 / 6)  # pooled, not averaged
        self.assertAlmostEqual(stats["required_corner_recall"], 0.75)
        self.assertEqual(stats["kinks"]["total"], 3)
        self.assertAlmostEqual(stats["node_ratio"]["geometric_mean"], 2.0)  # (2 * 8 * 0.5) ** (1/3)
        self.assertEqual(stats["topology"], {"cases_mismatched": 1, "colours_mismatched": 1})

    def test_empty_group_statistics_are_none(self):
        stats = sweep.group_stats([row("x", ok=False)])
        self.assertIsNone(stats["hausdorff_px"]["median"])
        self.assertIsNone(stats["corner_recall"])
        self.assertIsNone(stats["node_ratio"]["geometric_mean"])

    def test_aggregate_groups_by_tier_family_tag_and_size(self):
        rows = [row("a", family="disc", tags=("disc", "scaled"), size=64),
                row("b", family="star", tags=("star", "scaled"), size=512),
                row("c", family="star", tags=("star",), size=64, tier="rgb")]
        summary_ = sweep.aggregate(rows)
        self.assertEqual(set(summary_), {"labels", "rgb"})
        labels = summary_["labels"]
        self.assertEqual(labels["all"]["cases"], 2)
        self.assertEqual(set(labels["family"]), {"disc", "star"})
        self.assertEqual(labels["tag"]["scaled"]["cases"], 2)
        self.assertEqual(list(labels["size"]), ["64", "512"])  # numeric order
        self.assertEqual(summary_["rgb"]["all"]["cases"], 1)


class ObjectiveTests(unittest.TestCase):
    def test_error_increases_with_every_error_measure(self):
        base = tune.case_error(row(kinks=0, topology_mismatches=0))
        worse = {"hausdorff_px": 2.0, "mean_px": 0.8, "misassigned_px": 90.0,
                 "required_found": 2, "kinks": 3, "topology_mismatches": 1}
        for key, value in worse.items():
            with self.subTest(key=key):
                self.assertGreater(tune.case_error(row(**{key: value})), base)

    def test_error_is_monotone_along_each_distance(self):
        for key in ("hausdorff_px", "mean_px", "area_offset_px"):
            errors = [tune.case_error(row(**{key: value})) for value in (0.0, 0.2, 0.5, 1.0, 3.0)]
            self.assertEqual(errors, sorted(errors), key)
            self.assertEqual(len(set(errors)), len(errors), key)

    def test_distance_is_normalised_by_the_case_budget(self):
        small = tune.error_terms(row(size=100, hausdorff_px=0.6))["hausdorff"]
        large = tune.error_terms(row(size=2000, hausdorff_px=0.6))["hausdorff"]
        self.assertAlmostEqual(small, 1.0)  # budget 0.5 + 0.1 px
        self.assertAlmostEqual(large, 0.24)  # budget 0.5 + 2 px

    def test_sub_resolution_cases_score_area_topology_and_structure_only(self):
        terms = tune.error_terms(row(sub_resolution=True, hausdorff_px=50.0, kinks=9))
        self.assertEqual(set(terms), {"area", "topology", "structural"})

    def test_kink_term_keeps_its_slope_on_staircase_traces(self):
        # A staircase trace has tens of kinks per ideal node; fewer kinks must
        # still score better there, not saturate at a cap.
        errors = [tune.case_error(row(kinks=kinks, ideal_nodes=4)) for kinks in (300, 150, 60, 4, 0)]
        self.assertEqual(errors, sorted(errors, reverse=True))
        self.assertEqual(len(set(errors)), len(errors))

    def test_structural_failure_is_penalised(self):
        self.assertAlmostEqual(tune.case_error(row(structural_failures=1))
                               - tune.case_error(row()), tune.WEIGHTS["structural"])

    def test_failure_scores_worst(self):
        worst = row(hausdorff_px=1e6, mean_px=1e6, misassigned_px=1e9, required_found=0,
                    kinks=10 ** 7, topology_mismatches=2, structural_failures=2)
        self.assertAlmostEqual(tune.case_error(worst), tune.FAILURE_ERROR)
        self.assertEqual(tune.case_error(row(ok=False)), tune.FAILURE_ERROR)

    def test_scores_and_scalar_objective(self):
        rows = [row("a", nodes=8, ideal_nodes=4), row("b", nodes=4, ideal_nodes=4),
                row("c", nodes=16, ideal_nodes=4)]
        score = tune.score_rows(rows)
        self.assertAlmostEqual(score["nodes"], 2.0)  # (2 * 1 * 4) ** (1/3)
        self.assertAlmostEqual(tune.objective(1.0, 4.0, 0.5), 2.0)
        self.assertLess(tune.objective(1.0, 2.0, 1.0), tune.objective(1.0, 3.0, 1.0))

    def test_fewer_nodes_than_ideal_earn_no_credit(self):
        at_ideal = tune.score_rows([row("a", nodes=4, ideal_nodes=4),
                                    row("b", nodes=16, ideal_nodes=4)])
        below = tune.score_rows([row("a", nodes=1, ideal_nodes=4),
                                 row("b", nodes=16, ideal_nodes=4)])
        self.assertEqual(at_ideal["nodes"], below["nodes"])

    def test_empty_evaluation_is_rejected(self):
        with self.assertRaises(ValueError):
            tune.score_rows([])


class ParetoTests(unittest.TestCase):
    def test_front(self):
        points = [(1.0, 5.0), (2.0, 3.0), (3.0, 3.0), (2.5, 1.0), (4.0, 0.5), (2.0, 3.0),
                  (5.0, 5.0), (1.0, 6.0)]
        # (3,3) is dominated by (2,3); (5,5) by many; (1,6) by (1,5). The two (2,3) are equal.
        self.assertEqual(tune.pareto_front(points), [0, 1, 5, 3, 4])

    def test_single_and_all_equal(self):
        self.assertEqual(tune.pareto_front([(1.0, 1.0)]), [0])
        self.assertEqual(tune.pareto_front([(1.0, 1.0)] * 3), [0, 1, 2])


class _Stub:
    """A case stand-in for evaluations whose traces are faked."""

    def __init__(self, case_id):
        self.case_id = case_id


class LatticeTests(unittest.TestCase):
    def test_lattice_returns_exactly_to_its_start(self):
        record = tune.base_record()
        starts = tune.start_values(record)
        for parameter in tune.PARAMETERS:
            start = starts[parameter.name]
            for index in (-8, -3, 0, 5, 8):
                value = tune.lattice_value(parameter, start, index)
                again = tune.lattice_value(parameter, start, index + 4 - 4)
                self.assertEqual(value, again)
            self.assertEqual(tune.lattice_value(parameter, start, 0), start)

    def test_lattice_bounds_keep_values_in_range(self):
        record = tune.base_record()
        for parameter in tune.PARAMETERS:
            start = record[parameter.name]
            low, high = tune.lattice_bounds(parameter, start)
            self.assertGreaterEqual(tune.lattice_value(parameter, start, low), parameter.low - 1e-9)
            self.assertLessEqual(tune.lattice_value(parameter, start, high), parameter.high + 1e-9)
            self.assertLess(tune.lattice_value(parameter, start, low - 1), parameter.low)


class SearchTests(unittest.TestCase):
    def evaluator(self, out_dir, max_evals, resume=False, cases=("case",)):
        """A real Evaluator whose traces are replaced by rows scored from the settings."""
        def fake_run(cases, tiers, settings, work, **kwargs):
            # A bowl with its minimum at curve_fit_tolerance 1.5 and corner threshold 40.
            distance = (abs(math.log2(settings.curve_fit_tolerance / 1.5))
                        + abs(settings.corner_threshold - 40.0) / 40.0)
            return [row("a", hausdorff_px=0.1 + distance, nodes=4, ideal_nodes=4)]

        patcher = mock.patch.object(sweep, "run_cases", side_effect=fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tune.Evaluator([_Stub(name) for name in cases], ["labels"], out_dir,
                              max_evals=max_evals, code="test", resume=resume,
                              log=lambda message: None)

    def test_search_improves_and_resumes_on_the_same_path(self):
        parameters = [p for p in tune.PARAMETERS
                      if p.name in ("curve_fit_tolerance", "corner_threshold")]
        record = tune.base_record()
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            first = self.evaluator(Path(name), max_evals=5)
            best = tune.compass_search(first, record, parameters)
            self.assertEqual(len(first.evaluations), 5)
            self.assertLess(best["objective"], first.evaluations[0]["objective"])
            with self.assertRaises(ValueError):  # an existing run needs --resume
                self.evaluator(Path(name), max_evals=40)
            with self.assertRaises(ValueError):  # other cases: another fingerprint
                self.evaluator(Path(name), max_evals=40, resume=True, cases=("other",))
            resumed = self.evaluator(Path(name), max_evals=40, resume=True)
            self.assertEqual(len(resumed.evaluations), 5)
            final = tune.compass_search(resumed, record, parameters)
            # The replay asked for the first run's points in the same order.
            self.assertEqual(resumed.requested[:len(first.requested) - 1],
                             first.requested[:-1])
            lines = (Path(name) / "evaluations.jsonl").read_text().splitlines()
            keys = [json.loads(line)["key"] for line in lines]
            self.assertEqual(len(keys), len(set(keys)))  # no settings traced twice
            self.assertLessEqual(final["objective"], best["objective"])
            self.assertAlmostEqual(final["values"]["corner_threshold"], 40.0, delta=2.5)
            self.assertAlmostEqual(final["values"]["curve_fit_tolerance"], 1.5, delta=0.15)
            by_index = {entry["index"]: entry for entry in resumed.evaluations}
            self.assertEqual(tune.no_effect_parameters(list(by_index.values())), [])


class SettingsIsolationTests(unittest.TestCase):
    def test_tuner_evaluation_never_mutates_the_defaults(self):
        defaults = asdict(TraceSettings())
        field_defaults = {f.name: f.default for f in fields(TraceSettings)}
        tables = copy.deepcopy((contracts.PER_MILLE_DEFAULTS, contracts.PX_DEFAULTS,
                                contracts.FOLLOWING_LENGTHS, contracts.AUTOMATIC_LENGTHS))
        record = tune.base_record()
        record_copy = copy.deepcopy(record)
        values = {**tune.start_values(record), "curve_fit_tolerance": 3.0,
                  "max_deviation": 2.5, "corner_threshold": 35.0}
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            evaluator = tune.Evaluator(hand_built_cases()[:1], ["labels"], Path(name), jobs=1,
                                       log=lambda message: None)
            entry = evaluator(tune.settings_for(record, values))
        self.assertEqual(entry["failures"], 0)
        self.assertEqual(entry["settings"]["curve_fit_tolerance"], 3.0)
        self.assertEqual(record, record_copy)
        self.assertEqual(asdict(TraceSettings()), defaults)
        self.assertEqual({f.name: f.default for f in fields(TraceSettings)}, field_defaults)
        self.assertEqual((contracts.PER_MILLE_DEFAULTS, contracts.PX_DEFAULTS,
                          contracts.FOLLOWING_LENGTHS, contracts.AUTOMATIC_LENGTHS), tables)

    def test_base_record_makes_automatic_lengths_explicit_without_changing_the_trace(self):
        record = tune.base_record()
        self.assertEqual(record["max_deviation"], contracts.DEFAULT_MAX_DEVIATION_PER_MILLE)
        self.assertEqual(record["corner_sharpening_allowance"], record["max_deviation"])
        self.assertEqual(record["side_smoothness"], record["straight_line_max_deviation"])
        explicit = tune.settings_for(record, tune.start_values(record))
        self.assertEqual(tune.settings_key(explicit), tune.settings_key(TraceSettings()))
        self.assertEqual(tune.settings_diff(explicit, TraceSettings()), {})
        self.assertEqual(sweep.settings_changed(explicit), {})


class SettingsParsingTests(unittest.TestCase):
    def test_set_values_go_through_the_trace_cli(self):
        settings, cleanup = sweep.settings_from_options(
            None, ["corner_threshold=45", "nodes_at_bends=false", "curve-fit-tolerance=0.5",
                   "solid_underlayers=false", "corner_straightness=0.9"])
        self.assertEqual(settings.corner_threshold, 45.0)
        self.assertFalse(settings.nodes_at_bends)
        self.assertFalse(settings.solid_underlayers)
        self.assertEqual(settings.curve_fit_tolerance, 0.5)
        self.assertEqual(settings.side_smoothness, 0.9)
        with self.assertRaises(ValueError):
            sweep.settings_from_options(None, ["no_such_setting=1"])
        with self.assertRaises(ValueError):  # the trace CLI's own range check
            sweep.settings_from_options(None, ["curve_feature_scale=100"])
        with self.assertRaises(ValueError):  # the trace CLI's own type check
            sweep.settings_from_options(None, ["corner_threshold=steep"])

    def test_proposed_config_reproduces_the_proposed_settings(self):
        record = tune.base_record()
        proposed = tune.settings_for({**record, "nodes_at_bends": False},
                                     {**tune.start_values(record), "curve_fit_tolerance": 1.5,
                                      "max_deviation": 2.0, "corner_threshold": 45.0})
        config = tune.proposed_config(proposed, None)
        self.assertEqual(config["nodes_at_bends"], False)
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            path = Path(name) / "trace.json"
            path.write_text(json.dumps(config))
            parsed, _ = sweep.settings_from_options(path, [])
        self.assertEqual(sweep.effective_record(parsed), sweep.effective_record(proposed))


class EndToEndSweepTests(unittest.TestCase):
    def test_two_hand_built_cases_through_the_labels_tier(self):
        cases = hand_built_cases()
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            out = Path(name)
            rows = sweep.run_cases(cases, ["labels"], TraceSettings(), out / "work", jobs=1)
            sweep.write_sweep(out, rows, {"cases": 2, "tiers": ["labels"], "jobs": 1})
            self.assertTrue(all(row["ok"] for row in rows), [row["error"] for row in rows])
            self.assertEqual([row["case_id"] for row in rows], ["square-96", "disc-96"])
            lines = (out / "cases.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            summary = json.loads((out / "summary.json").read_text())["summary"]
            self.assertEqual(summary["labels"]["all"]["cases"], 2)
            self.assertEqual(set(summary["labels"]["family"]), {"square", "disc"})
            self.assertIn("## Tier `labels`", (out / "summary.md").read_text())
        for result in rows:
            s, measured = result["summary"], result["measure"]
            # The flat summary restates the measure result it was built from.
            self.assertEqual(s["kinks"], measured["kinks"]["count"])
            self.assertEqual(s["nodes"], measured["nodes"]["boundary"])
            self.assertEqual(s["hausdorff_px"], measured["boundary"]["hausdorff_px"])
            self.assertEqual(s["structural_failures"], 0)
            self.assertEqual(s["topology_mismatches"], 0)
        square = rows[0]["summary"]
        self.assertEqual(square["required_corners"], 4)
        self.assertEqual(square["required_found"], 4)
        # The disc's truth has four ideal nodes and no corners.
        self.assertEqual(rows[1]["summary"]["ideal_nodes"], 4)
        self.assertEqual(rows[1]["summary"]["required_corners"], 0)


class TuneCommandTests(unittest.TestCase):
    def run_main(self, *argv):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fidelity_tune",
                                                      ROOT / "scripts" / "fidelity_tune.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch("sys.argv", ["fidelity_tune.py", *argv]), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            module.main()

    def test_short_run_writes_the_report_refuses_reuse_and_resumes(self):
        (ROOT / ".tmp").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as name:
            out = Path(name) / "run"
            common = ["--no-fixed", "--train", "1000:1001", "--held-out", "5000:5001",
                      "--fuzz-sizes", "96", "--cases-per-eval", "0", "--jobs", "1",
                      "--no-real-images"]
            self.run_main(*common, "--max-evals", "2", "--out", str(out),
                          "--set", "nodes_at_bends=false")
            for artefact in ("run.json", "evaluations.jsonl", "pareto.json", "pareto.png",
                             "proposed-trace.json", "report.md", "held-out/families.json"):
                self.assertTrue((out / artefact).exists(), artefact)
            evaluations = (out / "evaluations.jsonl").read_text().splitlines()
            # Two within the budget plus the shipped defaults, which the moved base
            # did not start from.
            self.assertEqual(len(evaluations), 3)
            report = (out / "report.md").read_text()
            self.assertIn("plus one for the current defaults", report)
            self.assertIn("| nodes_at_bends | True | False |", report)
            with self.assertRaises(SystemExit):
                self.run_main(*common, "--max-evals", "2", "--out", str(out))
            self.run_main("--resume", str(out), "--max-evals", "4", "--jobs", "1",
                          "--no-real-images")
            self.assertEqual(len((out / "evaluations.jsonl").read_text().splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
