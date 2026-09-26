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
(``bounds.area_coverage``).

Expected failures
-----------------
An assertion listed in ``tests/synthetic/expected-failures.json`` fails today for
the defect tracked by the entry's worklog task, and the entry records the
assertion's measurements (``bounds.measurements``). Only the assertion's own
``AssertionError`` counts as the expected failure, and only while every
measurement equals its recorded value within ``bounds.RATCHET_TOLERANCE``: a worse
measurement is a regression the entry would otherwise hide, and a better one
leaves the recorded value too loose, so either is reported as a plain failure
(re-record with ``scripts/record_expected_failures.py``, which refuses a worse
value without ``--allow-worse``). Any other exception while checking it is an
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
        except Exception:
            _stop_expecting_failure(self)
            raise
        prefix = f"[{self.tier}/{self.case.case_id} {name}]"
        if entry is None:
            self.assertTrue(passed, f"{prefix} {detail}")
            return
        if not passed:
            worse, better = bounds.ratchet(entry["measured"], measured)
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
        good = {"task": "some-task", "measured": {"kinks": 3.0}}
        self.assertEqual(regression.entry_problems("kinks", good), [])
        for bad in ("some-task", {"task": "some-task"},
                    {"task": "Some Task", "measured": {"kinks": 3.0}},
                    {"task": PLACEHOLDER_SLUG_PREFIX + "x", "measured": {"kinks": 3.0}},
                    {"task": "some-task", "measured": {"nodes_outside_zones": 3.0}},
                    {"task": "some-task", "measured": {"kinks": -1.0}},
                    {"task": "some-task", "measured": {"kinks": "3"}}):
            with self.subTest(bad=bad):
                self.assertTrue(regression.entry_problems("kinks", bad))

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


class ExpectedFailureWrappingTests(unittest.TestCase):
    """Only an assertion's ``AssertionError`` is its expected failure, and only while
    its measurements match the recorded ones."""

    RECORDED = {"truth_distance_px": 3.0, "truth_length_beyond_px": 10.0,
                "evidence_distance_px": 1.0, "evidence_length_beyond_px": 0.0}

    def outcome(self, check, entry="default", available=None,
                measured=None) -> unittest.TestResult:
        from types import SimpleNamespace
        from unittest import mock
        if entry == "default":
            entry = {"task": "some-task", "measured": dict(self.RECORDED)}
        body = {"case": SimpleNamespace(case_id="probe"), "tier": "labels", "result": {},
                "__module__": __name__,
                "test_hausdorff": _assertion_method("hausdorff", entry)}
        cls = type("Probe", (_SyntheticCase, unittest.TestCase), body)
        result = unittest.TestResult()
        values = dict(self.RECORDED if measured is None else measured)
        with mock.patch.object(bounds, "check", check), \
                mock.patch.object(bounds, "measurements", lambda name, result, case: values), \
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
