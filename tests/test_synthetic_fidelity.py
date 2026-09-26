"""Synthetic ground-truth fidelity regression tests of the default trace.

Every committed synthetic case (``tests/synthetic/fixed``, ``tests/synthetic/
palette-stage`` and the pinned fuzz scenes listed in ``tests/synthetic/
fuzz-seeds.json``) is traced with the product defaults (``TraceSettings()``)
through the real stages and its edge variant judged against the case's truth and
the raster evidence the trace received. Two tiers:

- ``labels``: the hard-edged label map (``raster.render_labels``) straight into the
  trace stage (``harness.trace_labels``);
- ``rgb``: the anti-aliased render through the palette stage first
  (``harness.trace_rgb``, with the case's ``families.case_palette_settings``).
  Palette-stage cases run in this tier only.

The runs, the tracing and the expected-failure table's format are in
``vinylgen/fidelity/regression.py``; the bounds, the assertions and why each holds
are in ``vinylgen/fidelity/bounds.py``, the one yardstick the fidelity sweep and
tuner use too.

Each (case, tier) is one generated ``TestCase`` class whose ``setUpClass`` traces
once; each assertion is its own test method, so ``scripts/run_tests.py`` schedules
a (case, tier) as one unit and reports every assertion separately. An assertion
with nothing to hold for a case (``bounds.available``: every boundary lies in a
narrow or palette zone) is reported as a skip naming the reason.
``test_every_colour_is_held_by_an_area_assertion`` checks that the area and
evidence assertions together can fail for every scene colour
(``bounds.area_coverage``), and
``test_the_topology_assertion_fails_every_deletion_it_claims`` that the truth
itself passes the topology assertion while every single deletion it claims to
catch (an isolated structure, or one an asserted count counts) fails it
(``bounds.topology_mutations``), so no asserted count's range admits one.

Expected failures
-----------------
An assertion listed in ``tests/synthetic/expected-failures.json`` fails today for
the defect tracked by the entry's worklog task, and the entry records the
assertion's measurements (``bounds.measurements``) and the identities of what it
found wrong (``bounds.identities``). Only the assertion's own ``AssertionError``
counts as the expected failure, and only while every measurement equals its
recorded value within ``bounds.RATCHET_TOLERANCE`` and the identities are the
recorded set (``bounds.entry_ratchet``): a worse record (a larger measurement or
an unrecorded item) is a regression the entry would otherwise hide, and a better
one leaves the record too loose, so either is reported as a plain failure
(re-record with ``scripts/record_expected_failures.py``, which refuses a worse
record without ``--allow-worse``). Fixing one recorded defect while adding another
of the same size changes the identities, so it fails too. Any other exception while checking it is an
error. The method's failure message and docstring name the slug; an unexpected
pass is reported as an ``UNEXPECTED SUCCESS``, which fails the run, so a fixed
defect forces its entry out of the table. An entry naming an unknown (tier, case),
an assertion the case does not make, a placeholder slug or measurements other
than the assertion's fails ``ExpectedFailureTableTests``; an entry for an
assertion that has nothing to hold is an error when it runs. The table checks a
slug's form only: worklog is not available to the tests, so that a slug names a
real task is checked by hand.
"""

from __future__ import annotations

import json
import shutil
import unittest

from vinylgen.fidelity import bounds, regression
from vinylgen.fidelity.regression import (EXPECTED_FAILURES_PATH, PLACEHOLDER_SLUG_PREFIX,
                                          ROOT, all_runs, fuzz_cases, load_expected_failures,
                                          run_case)

WORK = ROOT / ".tmp" / "test-synthetic-fidelity"
TIERS = bounds.TIERS
ASSERTIONS = bounds.ASSERTIONS

__all__ = ["EXPECTED_FAILURES_PATH", "PLACEHOLDER_SLUG_PREFIX", "all_runs", "fuzz_cases",
           "load_expected_failures", "run_case"]


def _class_name(tier: str, case_id: str) -> str:
    import re
    words = re.split(r"[^0-9a-zA-Z]+", f"{tier}-{case_id}")
    return "Fidelity_" + "_".join(word for word in words if word)


def _stop_expecting_failure(test: unittest.TestCase) -> None:
    """Report the exception now propagating from ``test`` as an error or failure,
    not as the expected failure its method is marked with: ``unittest`` counts any
    exception in an ``expectedFailure`` method as expected, and reads the
    outcome's flag only when the exception arrives."""
    outcome = getattr(test, "_outcome", None)
    if outcome is not None:
        outcome.expecting_failure = False


class _SyntheticCase:
    """Mixed into each generated (case, tier) ``TestCase``: ``setUpClass`` traces the
    case once; each test method is one assertion."""

    case = None
    tier = ""

    @classmethod
    def setUpClass(cls):
        cls.work_dir = WORK / cls.tier / cls.case.case_id
        shutil.rmtree(cls.work_dir, ignore_errors=True)
        cls.result = run_case(cls.case, cls.tier, cls.work_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work_dir, ignore_errors=True)

    def test_resolved_max_deviation_matches_the_trace(self):
        self.assertAlmostEqual(self.result["max_deviation_px"],
                               self.result["audit_max_deviation_px"], places=9)

    def test_every_colour_is_held_by_an_area_assertion(self):
        """Every scene colour is held by the truth area bound (its limit below its
        true area) or by the evidence assertion, so a trace that drops it fails."""
        self.assertEqual(self.result["area_coverage"], [])

    def test_the_topology_assertion_fails_every_deletion_it_claims(self):
        """The truth as its own trace passes the topology assertion, and every
        single deletion it claims to catch fails it."""
        rows = self.result["topology_mutations"]
        self.assertEqual([row for row in rows if row["mutation"] == "truth" and row["error"]],
                         [])
        self.assertEqual([row for row in rows if row["mutation"] != "truth" and not row["error"]],
                         [])

    def _assert(self, name: str, entry: dict | None):
        reason = bounds.available(name, self.result)
        if reason is not None:
            if entry is not None:
                _stop_expecting_failure(self)
                raise RuntimeError(f"expected-failure entry {entry['task']} for {name}, which "
                                   f"has nothing to hold here: {reason}")
            self.skipTest(reason)
        try:
            passed, detail = bounds.check(name, self.result, self.case)
            measured = bounds.measurements(name, self.result, self.case)
            found = bounds.identities(name, self.result, self.case)
        except Exception:
            _stop_expecting_failure(self)
            raise
        prefix = f"[{self.tier}/{self.case.case_id} {name}]"
        if entry is None:
            self.assertTrue(passed, f"{prefix} {detail}")
            return
        if not passed:
            worse, better = bounds.entry_ratchet(entry, measured, found)
            if worse or better:
                _stop_expecting_failure(self)
                change = "worse than recorded" if worse else "better than recorded"
                self.fail(f"{prefix} expected failure {entry['task']} is {change} "
                          f"({'; '.join(worse + better)}); a worse value is a regression, "
                          "a better one is re-recorded with "
                          f"scripts/record_expected_failures.py: {detail}")
        self.assertTrue(passed, f"{prefix} expected failure, tracked by {entry['task']}: "
                                f"{detail}")


def _assertion_method(name: str, entry: dict | None):
    def method(self):
        self._assert(name, entry)
    method.__name__ = f"test_{name}"
    if entry is not None:
        method.__doc__ = f"{name}: expected failure, tracked by worklog task {entry['task']}"
        return unittest.expectedFailure(method)
    method.__doc__ = f"{name}: within its bound"
    return method


def _generate() -> dict[str, type]:
    expected = load_expected_failures()
    classes = {}
    for tier, case in all_runs():
        entries = expected.get(f"{tier}/{case.case_id}", {})
        body = {"case": case, "tier": tier, "__module__": __name__,
                "__doc__": f"{case.case_id} traced in the {tier} tier."}
        for name in bounds.applicable(case, tier):
            entry = entries.get(name)
            body[f"test_{name}"] = _assertion_method(
                name, entry if isinstance(entry, dict) else None)
        name = _class_name(tier, case.case_id)
        if name in classes:
            raise ValueError(f"duplicate synthetic test class {name}")
        classes[name] = type(name, (_SyntheticCase, unittest.TestCase), body)
    return classes


globals().update(_generate())


class ExpectedFailureTableTests(unittest.TestCase):
    def test_every_entry_names_a_traced_case_an_assertion_it_makes_and_its_measurements(self):
        runs = {f"{tier}/{case.case_id}": case for tier, case in all_runs()}
        for key, entries in load_expected_failures().items():
            with self.subTest(key=key):
                self.assertIn(key, runs, "expected failure for a (tier, case) the suite "
                                         "does not trace")
                self.assertTrue(entries, f"{key} lists no assertion")
                tier = key.split("/", 1)[0]
                made = bounds.applicable(runs[key], tier)
                for name, entry in entries.items():
                    self.assertIn(name, ASSERTIONS)
                    self.assertIn(name, made, f"{key} makes no {name} assertion")
                    self.assertEqual(regression.entry_problems(name, entry), [], key)

    def test_the_entry_check_rejects_malformed_entries(self):
        points = {"kinks": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]}
        good = {"task": "some-task", "measured": {"kinks": 3.0}, "identities": points}
        self.assertEqual(regression.entry_problems("kinks", good), [])
        for bad in ("some-task", {"task": "some-task"},
                    {"task": "some-task", "measured": {"kinks": 3.0}},
                    {"task": "Some Task", "measured": {"kinks": 3.0}, "identities": points},
                    {"task": PLACEHOLDER_SLUG_PREFIX + "x", "measured": {"kinks": 3.0},
                     "identities": points},
                    {"task": "some-task", "measured": {"nodes_outside_zones": 3.0},
                     "identities": points},
                    {"task": "some-task", "measured": {"kinks": -1.0}, "identities": points},
                    {"task": "some-task", "measured": {"kinks": "3"}, "identities": points},
                    {"task": "some-task", "measured": {"kinks": 3.0},
                     "identities": {"cusps": [[1.0, 2.0]]}},
                    {"task": "some-task", "measured": {"kinks": 3.0},
                     "identities": {"kinks": [[]]}},
                    {"task": "some-task", "measured": {"kinks": 3.0},
                     "identities": {"kinks": [[1.0, None]]}}):
            with self.subTest(bad=bad):
                self.assertTrue(regression.entry_problems("kinks", bad))

    def test_the_identity_ratchet_catches_a_swapped_defect(self):
        recorded = {"kinks": [[10.0, 20.0], [30.0, 40.0]]}
        self.assertEqual(bounds.identity_ratchet(recorded, {"kinks": [[30.0, 40.0005],
                                                                      [10.0, 20.0]]}), ([], []))
        # One recorded kink fixed and a new one elsewhere: the count is unchanged.
        worse, better = bounds.identity_ratchet(recorded, {"kinks": [[10.0, 20.0],
                                                                     [50.0, 60.0]]})
        self.assertTrue(worse and better)
        worse, better = bounds.identity_ratchet(recorded, {"kinks": [[10.0, 20.0]]})
        self.assertTrue(better and not worse)
        entry = {"task": "t", "measured": {"kinks": 2.0}, "identities": recorded}
        worse, _ = bounds.entry_ratchet(entry, {"kinks": 2.0},
                                        {"kinks": [[10.0, 20.0], [50.0, 60.0]]})
        self.assertTrue(worse)
        # Strings match exactly, numbers within the tolerance.
        self.assertTrue(bounds.identity_ratchet({"layers": [["#AABBCC"]]},
                                                {"layers": [["#AABBCD"]]})[0])

    def test_the_table_is_written_one_identity_item_per_line(self):
        import json
        import tempfile
        from pathlib import Path
        table = {"labels/x": {"kinks": {"task": "t", "measured": {"kinks": 2.0},
                                        "identities": {"kinks": [[1.123456789, 2.0],
                                                                 [3.0, 4.0]]}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.json"
            regression.write_expected_failures(table, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("[1.1235, 2.0]", text)
            written = json.loads(text)
        self.assertEqual(written["labels/x"]["kinks"]["identities"]["kinks"],
                         [[1.1235, 2.0], [3.0, 4.0]])

    def test_the_ratchet_tolerates_only_float_drift(self):
        self.assertEqual(bounds.ratchet({"kinks": 3.0}, {"kinks": 3.0}), ([], []))
        worse, better = bounds.ratchet({"kinks": 3.0}, {"kinks": 4.0})
        self.assertTrue(worse and not better)
        worse, better = bounds.ratchet({"kinks": 3.0}, {"kinks": 2.0})
        self.assertTrue(better and not worse)
        self.assertEqual(bounds.ratchet({"truth_distance_px": 2.0},
                                        {"truth_distance_px": 2.0005}), ([], []))
        self.assertTrue(bounds.ratchet({"truth_distance_px": 2.0},
                                       {"truth_distance_px": 2.01})[0])
        self.assertTrue(bounds.ratchet({"excess_px2": 5.0}, {"excess_px2": 5.1})[0])


class RecordScriptTests(unittest.TestCase):
    """``scripts/record_expected_failures.py`` writes all or nothing and refuses
    orphan entries unless told to remove them."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = ROOT / "scripts" / "record_expected_failures.py"
        spec = importlib.util.spec_from_file_location("record_expected_failures", path)
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    KINKS = {"kinks": [[1.0, 2.0]]}

    def traced(self):
        from types import SimpleNamespace
        case = SimpleNamespace(scene=SimpleNamespace(params={}))
        return {"labels/a": ("labels", case), "labels/b": ("labels", case)}

    def failing(self, kinks=None):
        found = {"kinks": kinks if kinks is not None else self.KINKS["kinks"]}
        return {"verdicts": {"kinks": {"passed": False, "detail": "kink",
                                       "measured": {"kinks": float(len(found["kinks"]))},
                                       "identities": found}}}

    def entry(self):
        return {"task": "some-task", "measured": {"kinks": 1.0}, "identities": dict(self.KINKS)}

    def test_an_unrecorded_failure_blocks_the_write(self):
        table = {"labels/a": {"kinks": self.entry()}}
        report = self.script.reconcile(table, {"labels/a": self.failing(),
                                               "labels/b": self.failing()}, self.traced(), {})
        self.assertTrue(report["blocked"])
        self.assertEqual(len(report["unrecorded"]), 1)
        report = self.script.reconcile(table, {"labels/b": self.failing()}, self.traced(),
                                       {("labels/b", "kinks"): "other-task"})
        self.assertFalse(report["blocked"])
        self.assertEqual(table["labels/b"]["kinks"]["task"], "other-task")

    def test_retask_moves_entries_and_writes_nothing_if_one_is_missing(self):
        from unittest import mock
        table = {"labels/a": {"kinks": self.entry()}}
        with mock.patch.object(self.script.regression, "load_expected_failures",
                               side_effect=lambda: json.loads(json.dumps(table))), \
                mock.patch.object(self.script.regression, "write_expected_failures") as write:
            self.assertEqual(self.script.retask({("labels/a", "kinks"): "other-task",
                                                 ("labels/b", "kinks"): "other-task"}, False), 1)
            write.assert_not_called()
            self.assertEqual(self.script.retask({("labels/a", "kinks"): "other-task"}, False), 0)
            written = write.call_args.args[0]
        self.assertEqual(written["labels/a"]["kinks"],
                         {**self.entry(), "task": "other-task"})

    def test_an_orphan_entry_is_refused_unless_removed(self):
        table = {"labels/gone": {"kinks": self.entry()}, "labels/a": {"kinks": self.entry()}}
        report = self.script.reconcile(table, {"labels/a": self.failing()}, self.traced(), {})
        self.assertTrue(report["blocked"])
        self.assertIn("labels/gone", table)
        report = self.script.reconcile(table, {"labels/a": self.failing()}, self.traced(), {},
                                       remove_orphans=True)
        self.assertFalse(report["blocked"])
        self.assertNotIn("labels/gone", table)

    def test_a_swapped_defect_is_refused_unless_worse_is_allowed(self):
        table = {"labels/a": {"kinks": self.entry()}}
        moved = self.failing([[50.0, 60.0]])
        report = self.script.reconcile(table, {"labels/a": moved}, self.traced(), {})
        self.assertTrue(report["blocked"] and report["refused"])
        self.assertEqual(table["labels/a"]["kinks"]["identities"], self.KINKS)
        report = self.script.reconcile(table, {"labels/a": moved}, self.traced(), {},
                                       allow_worse=True)
        self.assertFalse(report["blocked"])
        self.assertEqual(table["labels/a"]["kinks"]["identities"], {"kinks": [[50.0, 60.0]]})

    def test_a_passing_entry_is_removed(self):
        table = {"labels/a": {"kinks": self.entry()}}
        passing = {"verdicts": {"kinks": {"passed": True, "detail": ""}}}
        report = self.script.reconcile(table, {"labels/a": passing}, self.traced(), {})
        self.assertFalse(report["blocked"])
        self.assertEqual(table, {})


class ExpectedFailureWrappingTests(unittest.TestCase):
    """Only an assertion's ``AssertionError`` is its expected failure, and only while
    its measurements match the recorded ones."""

    RECORDED = {"truth_distance_px": 3.0, "truth_length_beyond_px": 10.0,
                "evidence_distance_px": 1.0, "evidence_length_beyond_px": 0.0}
    IDENTITIES = {"truth_worst_point": [[5.0, 6.0]], "evidence_worst_point": [[7.0, 8.0]]}

    def outcome(self, check, entry="default", available=None, measured=None,
                found=None) -> unittest.TestResult:
        from types import SimpleNamespace
        from unittest import mock
        if entry == "default":
            entry = {"task": "some-task", "measured": dict(self.RECORDED),
                     "identities": dict(self.IDENTITIES)}
        body = {"case": SimpleNamespace(case_id="probe"), "tier": "labels", "result": {},
                "__module__": __name__,
                "test_hausdorff": _assertion_method("hausdorff", entry)}
        cls = type("Probe", (_SyntheticCase, unittest.TestCase), body)
        result = unittest.TestResult()
        values = dict(self.RECORDED if measured is None else measured)
        items = dict(self.IDENTITIES if found is None else found)
        with mock.patch.object(bounds, "check", check), \
                mock.patch.object(bounds, "measurements", lambda name, result, case: values), \
                mock.patch.object(bounds, "identities", lambda name, result, case: items), \
                mock.patch.object(bounds, "available", lambda name, result: available):
            cls("test_hausdorff").run(result)
        return result

    def test_an_assertion_failure_as_recorded_is_the_expected_failure(self):
        result = self.outcome(lambda name, result, case: (False, "outside"))
        self.assertEqual((len(result.expectedFailures), len(result.errors),
                          len(result.failures), len(result.unexpectedSuccesses)), (1, 0, 0, 0))

    def test_a_failure_worse_than_recorded_is_a_failure(self):
        worse = dict(self.RECORDED, truth_length_beyond_px=40.0)
        result = self.outcome(lambda name, result, case: (False, "outside"), measured=worse)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (0, 1))
        self.assertIn("worse than recorded", result.failures[0][1])

    def test_a_failure_better_than_recorded_is_a_failure(self):
        better = dict(self.RECORDED, truth_distance_px=2.5)
        result = self.outcome(lambda name, result, case: (False, "outside"), measured=better)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (0, 1))
        self.assertIn("better than recorded", result.failures[0][1])

    def test_a_failure_elsewhere_with_the_same_measurements_is_a_failure(self):
        moved = dict(self.IDENTITIES, truth_worst_point=[[50.0, 60.0]])
        result = self.outcome(lambda name, result, case: (False, "outside"), found=moved)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (0, 1))
        self.assertIn("worse than recorded", result.failures[0][1])

    def test_a_pass_is_an_unexpected_success(self):
        result = self.outcome(lambda name, result, case: (True, ""))
        self.assertEqual((len(result.expectedFailures), len(result.unexpectedSuccesses)), (0, 1))

    def test_any_other_exception_is_an_error(self):
        def broken(name, result, case):
            raise KeyError("missing measurement")
        result = self.outcome(broken)
        self.assertEqual((len(result.expectedFailures), len(result.errors)), (0, 1))
        self.assertIn("KeyError", result.errors[0][1])

    def test_an_entry_for_an_assertion_with_nothing_to_hold_is_an_error(self):
        result = self.outcome(lambda name, result, case: (False, ""), available="all narrow")
        self.assertEqual((len(result.expectedFailures), len(result.errors),
                          len(result.skipped)), (0, 1, 0))

    def test_an_unexpected_exception_without_an_entry_is_an_error(self):
        def broken(name, result, case):
            raise ValueError("bad")
        result = self.outcome(broken, entry=None)
        self.assertEqual((len(result.errors), len(result.failures)), (1, 0))


if __name__ == "__main__":
    unittest.main()
