"""Synthetic ground-truth fidelity regression tests of the default trace.

Every committed synthetic case (``tests/synthetic/fixed``, ``tests/synthetic/
palette-stage`` and the pinned fuzz scenes listed in ``tests/synthetic/
fuzz-seeds.json``) is traced with the product defaults (``TraceSettings()``)
through the real stages and its edge variant judged against the scene's vector
truth and the raster evidence the trace received. Two tiers:

- ``labels``: the hard-edged label map (``raster.render_labels``) straight into the
  trace stage (``harness.trace_labels``);
- ``rgb``: the anti-aliased render through the palette stage first
  (``harness.trace_rgb``, with the case's ``families.case_palette_settings``).
  Palette-stage cases run in this tier only.

The bounds, the assertions and why each holds are in ``vinylgen/fidelity/
bounds.py``, the one yardstick the fidelity sweep and tuner use too.

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
``tests/synthetic/expected-failures.json`` maps ``"<tier>/<case_id>"`` to
``{assertion: slug}``: that assertion fails today for the defect tracked by
worklog task ``slug``. Only the assertion's own ``AssertionError`` counts as the
expected failure; any other exception while checking it is reported as an
error. The method's failure message and docstring name the slug; an unexpected
pass is reported as an ``UNEXPECTED SUCCESS``, which fails the run, so a fixed
defect forces its entry out of the table. An entry naming an unknown (tier,
case), an assertion the case does not make or a placeholder slug fails
``ExpectedFailureTableTests``; an entry for an assertion that has nothing to hold
is an error when it runs. The table checks a slug's form only: worklog is not
available to the tests, so that a slug names a real task is checked by hand.
"""

from __future__ import annotations

import json
import re
import shutil
import unittest
from pathlib import Path

import numpy as np

from vinylgen.contracts import TraceSettings
from vinylgen.fidelity import bounds, families
from vinylgen.fidelity.harness import trace_labels, trace_rgb
from vinylgen.fidelity.raster import render_labels
from vinylgen.fidelity.truth import scene_truth

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests" / "synthetic"
EXPECTED_FAILURES_PATH = SYNTHETIC / "expected-failures.json"
FUZZ_SEEDS_PATH = SYNTHETIC / "fuzz-seeds.json"
WORK = ROOT / ".tmp" / "test-synthetic-fidelity"

# Expected failures name real worklog tasks; a slug with this prefix is a
# placeholder for a finding no task tracks yet.
PLACEHOLDER_SLUG_PREFIX = "synthetic-finding-"
TIERS = bounds.TIERS
ASSERTIONS = bounds.ASSERTIONS


# --- running one (case, tier) ---------------------------------------------------------------


def run_case(case, tier: str, work_dir: Path, settings: TraceSettings | None = None) -> dict:
    """Trace ``case`` in ``tier`` (with the default settings unless ``settings`` is
    given) and evaluate it against its bounds, which always come from the default
    settings. The result also carries the scene colours of the hard-edged label
    render (``raster_colours``) and ``bounds.area_coverage``'s findings."""
    scene = case.scene
    truth = scene_truth(scene)
    case_bounds = bounds.case_bounds(case, tier, truth)
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
    result = bounds.evaluate(case, tier, run, truth, case_bounds, raster_colours=raster_colours)
    result["raster_colours"] = sorted(raster_colours)
    # In the labels tier the evidence is the label render itself; in the rgb tier
    # the evidence assertion demands every unasserted colour it carries survive.
    result["area_coverage"] = bounds.area_coverage(
        case_bounds, raster_colours if tier == "labels" else None)
    return result


# --- the cases -------------------------------------------------------------------------------


def fuzz_cases() -> list:
    from vinylgen.fidelity.fuzz import pinned_cases
    return pinned_cases(FUZZ_SEEDS_PATH)


def all_runs() -> list[tuple[str, object]]:
    """Every (tier, case) the suite traces, in a stable order."""
    runs = []
    for case in families.load_fixed_set(ROOT / families.FIXED_DIR):
        runs += [(tier, case) for tier in TIERS]
    for case in families.load_fixed_set(ROOT / families.PALETTE_STAGE_DIR):
        runs.append(("rgb", case))
    for case in fuzz_cases():
        runs += [(tier, case) for tier in TIERS]
    return runs


def load_expected_failures() -> dict[str, dict[str, str]]:
    return json.loads(EXPECTED_FAILURES_PATH.read_text(encoding="utf-8"))


def _class_name(tier: str, case_id: str) -> str:
    words = re.split(r"[^0-9a-zA-Z]+", f"{tier}-{case_id}")
    return "Fidelity_" + "_".join(word for word in words if word)


def _stop_expecting_failure(test: unittest.TestCase) -> None:
    """Report the exception now propagating from ``test`` as an error, not as the
    expected failure its method is marked with: ``unittest`` counts any exception
    in an ``expectedFailure`` method as expected, and reads the outcome's flag
    only when the exception arrives."""
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

    def _assert(self, name: str, slug: str | None):
        reason = bounds.available(name, self.result)
        if reason is not None:
            if slug is not None:
                _stop_expecting_failure(self)
                raise RuntimeError(f"expected-failure entry {slug} for {name}, which has "
                                   f"nothing to hold here: {reason}")
            self.skipTest(reason)
        try:
            passed, detail = bounds.check(name, self.result, self.case)
        except Exception:
            _stop_expecting_failure(self)
            raise
        prefix = f"[{self.tier}/{self.case.case_id} {name}]"
        if slug is not None:
            self.assertTrue(passed, f"{prefix} expected failure, tracked by {slug}: {detail}")
        else:
            self.assertTrue(passed, f"{prefix} {detail}")


def _assertion_method(name: str, slug: str | None):
    def method(self):
        self._assert(name, slug)
    method.__name__ = f"test_{name}"
    if slug is not None:
        method.__doc__ = f"{name}: expected failure, tracked by worklog task {slug}"
        return unittest.expectedFailure(method)
    method.__doc__ = f"{name}: within its bound"
    return method


def _generate() -> dict[str, type]:
    expected = load_expected_failures()
    classes = {}
    for tier, case in all_runs():
        slugs = expected.get(f"{tier}/{case.case_id}", {})
        body = {"case": case, "tier": tier, "__module__": __name__,
                "__doc__": f"{case.case_id} traced in the {tier} tier."}
        for name in bounds.applicable(case, tier):
            body[f"test_{name}"] = _assertion_method(name, slugs.get(name))
        name = _class_name(tier, case.case_id)
        if name in classes:
            raise ValueError(f"duplicate synthetic test class {name}")
        classes[name] = type(name, (_SyntheticCase, unittest.TestCase), body)
    return classes


globals().update(_generate())


class ExpectedFailureTableTests(unittest.TestCase):
    def test_every_entry_names_a_traced_case_and_an_assertion_it_makes(self):
        runs = {f"{tier}/{case.case_id}": case for tier, case in all_runs()}
        for key, entries in load_expected_failures().items():
            with self.subTest(key=key):
                self.assertIn(key, runs, "expected failure for a (tier, case) the suite "
                                         "does not trace")
                tier = key.split("/", 1)[0]
                made = bounds.applicable(runs[key], tier)
                for name, slug in entries.items():
                    self.assertIn(name, ASSERTIONS)
                    self.assertIn(name, made, f"{key} makes no {name} assertion")
                    self.assertRegex(slug, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                    self.assertFalse(slug.startswith(PLACEHOLDER_SLUG_PREFIX),
                                     f"{key} {name}: placeholder slug {slug}; name the "
                                     "worklog task that tracks the defect")


class ExpectedFailureWrappingTests(unittest.TestCase):
    """Only an assertion's ``AssertionError`` is its expected failure."""

    def outcome(self, check, slug="some-task", available=None) -> unittest.TestResult:
        from types import SimpleNamespace
        from unittest import mock
        body = {"case": SimpleNamespace(case_id="probe"), "tier": "labels", "result": {},
                "__module__": __name__,
                "test_hausdorff": _assertion_method("hausdorff", slug)}
        cls = type("Probe", (_SyntheticCase, unittest.TestCase), body)
        result = unittest.TestResult()
        with mock.patch.object(bounds, "check", check), \
                mock.patch.object(bounds, "available", lambda name, result: available):
            cls("test_hausdorff").run(result)
        return result

    def test_an_assertion_failure_is_the_expected_failure(self):
        result = self.outcome(lambda name, result, case: (False, "outside"))
        self.assertEqual((len(result.expectedFailures), len(result.errors),
                          len(result.unexpectedSuccesses)), (1, 0, 0))

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
        result = self.outcome(broken, slug=None)
        self.assertEqual((len(result.errors), len(result.failures)), (1, 0))


if __name__ == "__main__":
    unittest.main()
