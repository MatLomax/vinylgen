"""Run the unittest suite across worker processes.

Tests are dispatched as independent units: a whole class when it has class or
module fixtures, otherwise each test on its own. Units are scheduled
longest-first using the durations recorded by the previous run, so the slowest
tests start immediately instead of extending the tail. Workers are persistent
spawned processes, so each pays the numpy/numba import cost once. A unit whose
worker dies is retried, then run alone and reported as an error if it dies again.

The default run discovers ``tests/`` only, so it never touches ``tests/fullimage/``.
That directory is the opt-in full-image bucket: real-corpus and sourceup checks
of the palette, cleanup and validation stages that need the full image corpus or
a `.tmp` baseline, selected only with ``--full-image`` (or by module name).

Usage: run_tests.py [-j N] [-v] [--durations N] [--full-image] [test names ...]
"""

from __future__ import annotations

import argparse
import io
import json
import multiprocessing
import os
import sys
import time
import traceback
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
FULLIMAGE = TESTS / "fullimage"
DURATIONS_FILE = ROOT / ".tmp" / "test-durations.json"


class _Result(unittest.TestResult):
    """Collects picklable outcomes and per-test wall time."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = True
        self.records: list[dict] = []
        self._started: dict[str, float] = {}

    def startTest(self, test):
        self._started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test, outcome: str, detail: str = "") -> None:
        started = self._started.pop(test.id(), None)
        elapsed = time.perf_counter() - started if started is not None else 0.0
        self.records.append({"id": test.id(), "outcome": outcome, "detail": detail,
                             "seconds": elapsed})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "FAIL", self.failures[-1][1])

    def addError(self, test, err):
        super().addError(test, err)
        # Class/module fixture errors arrive as a _ErrorHolder, not a TestCase.
        self._record(test, "ERROR", self.errors[-1][1])

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "UNEXPECTED SUCCESS")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            # A failed subtest's ``err`` is its ``sys.exc_info()``, so it names the exception.
            exc_type = err[0]
            assert exc_type is not None
            kind = "FAIL" if issubclass(exc_type, test.failureException) else "ERROR"
            detail = (self.failures if kind == "FAIL" else self.errors)[-1][1]
            self.records.append({"id": subtest.id(), "outcome": kind, "detail": detail,
                                 "seconds": 0.0})


def _worker_init() -> None:
    os.chdir(ROOT)
    # FULLIMAGE is always added (not only for --full-image runs) so a worker can
    # resolve a full-image module name whichever bucket dispatched the unit.
    for path in (str(TESTS), str(ROOT), str(FULLIMAGE)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _run_suite(suite: unittest.TestSuite, unit_id: str) -> dict:
    started = time.perf_counter()
    result = _Result()
    try:
        suite.run(result)
    except KeyboardInterrupt:
        raise
    except BaseException:  # noqa: BLE001 - SystemExit from a fixture is a unit error
        result.records.append({"id": unit_id, "outcome": "ERROR",
                               "detail": traceback.format_exc(), "seconds": 0.0})
    return {"records": result.records, "ran": result.testsRun,
            "seconds": time.perf_counter() - started}


def _run_unit(names: list[str]) -> dict:
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    except Exception:  # noqa: BLE001 - reported as a unit error, never swallowed
        return {"records": [{"id": names[0], "outcome": "ERROR",
                             "detail": traceback.format_exc(), "seconds": 0.0}],
                "ran": 0, "seconds": 0.0}
    return _run_suite(suite, names[0])


def _flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _units(suite: unittest.TestSuite) -> tuple[list[list[str]], list[unittest.TestCase]]:
    """Split into worker units (lists of test ids) and tests run in this process.

    Tests unittest synthesises itself (a module that failed to import, a
    module-level SkipTest) have ids a worker cannot load back, so they run here.
    """
    by_class: dict[type, list[str]] = {}
    units: list[list[str]] = []
    local: list[unittest.TestCase] = []
    for test in _flatten(suite):
        if type(test).__module__.startswith("unittest"):
            local.append(test)
            continue
        by_class.setdefault(type(test), []).append(test.id())
    for cls, ids in by_class.items():
        module = sys.modules.get(cls.__module__)
        has_fixtures = (
            cls.setUpClass.__func__ is not unittest.TestCase.setUpClass.__func__
            or cls.tearDownClass.__func__ is not unittest.TestCase.tearDownClass.__func__
            or hasattr(module, "setUpModule") or hasattr(module, "tearDownModule")
        )
        if has_fixtures:
            units.append(ids)
        else:
            units.extend([test_id] for test_id in ids)
    return units, local


def _execute(units: list[list[str]], jobs: int, report) -> list[list[str]]:
    """Run units on a worker pool; return the units lost to a dead worker.

    A worker that dies (os._exit, segfault, OOM kill) breaks the whole pool, so
    every unit still pending comes back for the caller to retry.
    """
    lost: list[list[str]] = []
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(jobs, mp_context=context, initializer=_worker_init) as pool:
        futures = {pool.submit(_run_unit, unit): unit for unit in units}
        for future in as_completed(futures):
            try:
                report(future.result())
            except BrokenProcessPool:
                lost.append(futures[future])
    return lost


WARM_SCRIPT = ROOT / "scripts" / "warm_t3_cache.py"


def _warm_t3_cache() -> None:
    """Warm the t3 engine's kernel cache before any test runs, once per cache.

    Every web trace job is a fresh ``pipeline_worker`` process, and on a cold cache its
    first ``t3api.trace`` compiles the engine's kernels (tens of seconds, longer than a web
    test's job budget). ``scripts/warm_t3_cache.py`` (the deploy's warm) pays that once, here,
    and records it in ``<t3api.cache_dir()>/warm.json``; a cache holding that record is warm
    and is left alone. Its line is printed, so the cost is measured in the run's log."""
    import subprocess

    import t3api

    record = Path(t3api.cache_dir()) / "warm.json"
    if record.exists():
        return
    completed = subprocess.run([sys.executable, str(WARM_SCRIPT)], cwd=ROOT, text=True,
                               capture_output=True, timeout=600)
    print((completed.stdout + completed.stderr).strip(), flush=True)


def _load_durations() -> dict[str, float]:
    try:
        return json.loads(DURATIONS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--durations", type=int, default=0, metavar="N",
                        help="show the N slowest tests")
    parser.add_argument("--full-image", action="store_true",
                        help="run only the opt-in full-image bucket (tests/fullimage/)"
                             " instead of the default tests/ discovery")
    parser.add_argument("names", nargs="*", help="test names; default discovers tests/")
    args = parser.parse_args(argv)

    _worker_init()
    _warm_t3_cache()
    loader = unittest.defaultTestLoader
    if args.names:
        suite = loader.loadTestsFromNames(args.names)
    elif args.full_image:
        suite = loader.discover(str(FULLIMAGE), top_level_dir=str(FULLIMAGE))
    else:
        suite = loader.discover(str(TESTS), top_level_dir=str(TESTS))
    units, local = _units(suite)
    previous = _load_durations()
    units.sort(key=lambda unit: sum(previous.get(name, 1.0) for name in unit), reverse=True)

    jobs = max(1, min(args.jobs, len(units) or 1))
    started = time.perf_counter()
    records: list[dict] = []
    ran = 0
    marks = {"ok": ".", "FAIL": "F", "ERROR": "E", "skip": "s",
             "expected failure": "x", "UNEXPECTED SUCCESS": "u"}

    def report(unit: dict) -> None:
        nonlocal ran
        records.extend(unit["records"])
        ran += unit["ran"]
        if args.verbose:
            for record in unit["records"]:
                print(f"{record['id']} ... {record['outcome']} ({record['seconds']:.2f}s)",
                      flush=True)
        else:
            print("".join(marks[r["outcome"]] for r in unit["records"]), end="", flush=True)

    for test in local:
        report(_run_suite(unittest.TestSuite([test]), test.id()))
    lost = _execute(units, jobs, report) if units else []
    if lost:
        # Retry once on a fresh pool: most lost units were only queued behind
        # the one that killed its worker.
        lost = _execute(lost, jobs, report)
    for unit in lost:
        # Still lost: run alone, so a death is pinned on this unit.
        if _execute([unit], 1, report):
            report({"records": [{"id": unit[0], "outcome": "ERROR", "seconds": 0.0,
                                 "detail": "worker process died while running "
                                           + ", ".join(unit)}], "ran": len(unit)})
    elapsed = time.perf_counter() - started
    if not args.verbose:
        print()

    bad = [r for r in records if r["outcome"] in {"FAIL", "ERROR", "UNEXPECTED SUCCESS"}]
    for record in bad:
        print("=" * 70)
        print(f"{record['outcome']}: {record['id']}")
        print("-" * 70)
        print(record["detail"])

    if args.durations:
        print(f"\nSlowest {args.durations} tests:")
        for record in sorted(records, key=lambda r: r["seconds"], reverse=True)[:args.durations]:
            print(f"  {record['seconds']:7.3f}s  {record['id']}")

    counts = {label: sum(r["outcome"] == outcome for r in records) for label, outcome in (
        ("failures", "FAIL"), ("errors", "ERROR"), ("skipped", "skip"),
        ("expected failures", "expected failure"),
        ("unexpected successes", "UNEXPECTED SUCCESS"))}
    print("-" * 70)
    print(f"Ran {ran} tests in {elapsed:.3f}s with {jobs} workers")
    details = ", ".join(f"{label}={count}" for label, count in counts.items() if count)
    print(f"\n{'FAILED' if bad else 'OK'}{f' ({details})' if details else ''}")

    if not args.names and not args.full_image:
        durations = {r["id"]: r["seconds"] for r in records if r["outcome"] != "skip"}
        DURATIONS_FILE.parent.mkdir(exist_ok=True)
        DURATIONS_FILE.write_text(json.dumps(durations, indent=0, sort_keys=True))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
