"""Generated synthetic fidelity test classes, shared by the default gate slice
(``tests/test_synthetic_fidelity.py``) and the full corpus in the opt-in bucket
(``tests/fullimage/test_synthetic_fidelity_corpus.py``).

Each (case, tier) is one generated ``TestCase`` class whose ``setUpClass`` traces
once; each assertion is its own test method, so ``scripts/run_tests.py`` schedules
a (case, tier) as one unit and reports every assertion separately. An assertion
with nothing to hold for a case (``bounds.available``: every boundary lies in a
narrow or palette zone) is reported as a skip naming the reason.

An assertion listed in ``tests/synthetic/expected-failures.json`` is an expected
failure while it fails its bound no worse than recorded
(``bounds.worse_than_recorded``); worse is a plain failure, since the entry would
otherwise hide a regression. Any other exception while checking it is an error.
A pass is an ``UNEXPECTED SUCCESS``, which fails the run, so a fixed defect
forces its entry out of the table (``scripts/record_expected_failures.py``
removes it).

With ``bound_checks`` a class also checks that the case's bounds can fail:
``test_every_colour_is_held_by_an_area_assertion`` (``bounds.area_coverage``) and
``test_the_topology_assertion_fails_every_change_the_truth_decides``
(``bounds.topology_mutations``: the truth passes the topology assertion, every
change it decides fails it, and each single change a count cannot decide is a
skipped subtest naming why). These depend on the bounds code, not the trace, so
only the full corpus runs them.
"""

from __future__ import annotations

import re
import shutil
import unittest

from vinylgen.fidelity import bounds
from vinylgen.fidelity.regression import ROOT, load_expected_failures, run_case

WORK = ROOT / ".tmp" / "test-synthetic-fidelity"


def class_name(tier: str, case_id: str) -> str:
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


class SyntheticCase:
    """Mixed into each generated (case, tier) ``TestCase``: ``setUpClass`` traces the
    case once; each test method is one assertion."""

    case = None
    tier = ""
    bound_checks = False

    @classmethod
    def setUpClass(cls):
        cls.work_dir = WORK / cls.tier / cls.case.case_id
        shutil.rmtree(cls.work_dir, ignore_errors=True)
        cls.result = run_case(cls.case, cls.tier, cls.work_dir,
                              bound_checks=cls.bound_checks)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work_dir, ignore_errors=True)

    def test_resolved_max_deviation_matches_the_trace(self):
        self.assertAlmostEqual(self.result["max_deviation_px"],
                               self.result["audit_max_deviation_px"], places=9)

    def _assert(self, name: str, entry: dict | None):
        tasks = tasks_text(entry)
        reason = bounds.available(name, self.result)
        if reason is not None:
            if entry is not None:
                _stop_expecting_failure(self)
                raise RuntimeError(f"expected-failure entry {tasks} for {name}, which "
                                   f"has nothing to hold here: {reason}")
            self.skipTest(reason)
        try:
            passed, detail = bounds.check(name, self.result, self.case)
            measured = bounds.measurements(name, self.result, self.case)
            worse = (bounds.worse_than_recorded(entry["measured"], measured)
                     if entry is not None and not passed else [])
        except Exception:
            _stop_expecting_failure(self)
            raise
        prefix = f"[{self.tier}/{self.case.case_id} {name}]"
        if entry is None:
            self.assertTrue(passed, f"{prefix} {detail}")
            return
        if not passed:
            if worse:
                _stop_expecting_failure(self)
                self.fail(f"{prefix} expected failure {tasks} is worse than recorded "
                          f"({'; '.join(worse)}): {detail}")
        self.assertTrue(passed, f"{prefix} expected failure, tracked by {tasks}: {detail}")


def _test_every_colour_is_held_by_an_area_assertion(self):
    """Every scene colour is held by the truth area bound (its limit below its
    true area) or by the evidence assertion, so a trace that drops it fails."""
    self.assertEqual(self.result["area_coverage"], [])


def _test_the_topology_assertion_fails_every_change_the_truth_decides(self):
    """The truth as its own trace passes the topology assertion, every deletion
    and addition the truth decides fails it, and each change it cannot decide
    is a skipped subtest naming why."""
    rows = self.result["topology_mutations"]
    self.assertEqual([row for row in rows if row.get("expect") == "pass" and row["error"]], [])
    self.assertEqual([row for row in rows if row.get("expect") == "fail" and not row["error"]],
                     [])
    for row in rows:
        if "skip" in row:
            with self.subTest(colour=row["colour"], mutation=row["mutation"]):
                self.skipTest(row["skip"])


def tasks_text(entry: dict | None) -> str:
    """An entry's worklog tasks (a malformed entry as it stands: the table test
    reports it)."""
    if entry is None:
        return ""
    tasks = entry.get("tasks")
    return ", ".join(map(str, tasks)) if isinstance(tasks, list) else repr(entry)


def assertion_method(name: str, entry: dict | None):
    def method(self):
        self._assert(name, entry)
    method.__name__ = f"test_{name}"
    if entry is not None:
        method.__doc__ = f"{name}: expected failure, tracked by worklog tasks {tasks_text(entry)}"
        return unittest.expectedFailure(method)
    method.__doc__ = f"{name}: within its bound"
    return method


def generate(runs, module: str, bound_checks: bool = False) -> dict[str, type]:
    """One ``TestCase`` class per (tier, case) of ``runs``, for ``module``."""
    expected = load_expected_failures()
    classes = {}
    for tier, case in runs:
        entries = expected.get(f"{tier}/{case.case_id}", {})
        body = {"case": case, "tier": tier, "bound_checks": bound_checks,
                "__module__": module, "__doc__": f"{case.case_id} traced in the {tier} tier."}
        if bound_checks:
            body["test_every_colour_is_held_by_an_area_assertion"] = \
                _test_every_colour_is_held_by_an_area_assertion
            body["test_the_topology_assertion_fails_every_change_the_truth_decides"] = \
                _test_the_topology_assertion_fails_every_change_the_truth_decides
        for name in bounds.applicable(case, tier):
            entry = entries.get(name)
            body[f"test_{name}"] = assertion_method(
                name, entry if isinstance(entry, dict) else None)
        name = class_name(tier, case.case_id)
        if name in classes:
            raise ValueError(f"duplicate synthetic test class {name}")
        classes[name] = type(name, (SyntheticCase, unittest.TestCase), body)
    return classes
