#!/usr/bin/env python3
"""Record the measurements of the synthetic fidelity expected-failure table.

Traces every (tier, case) of the regression corpus (``--only REGEX`` narrows the
selection to matching ``tier/case_id`` keys) exactly as the synthetic fidelity
tests do, and for every assertion that fails:

- with a table entry, writes its current measurements into the entry. A worse
  record (``bounds.worse_than_recorded``) is a regression and is refused unless
  ``--allow-worse`` is given; a better one tightens the record. An entry whose
  record lacks a measurement the assertion now has is recorded afresh;
- without one, it is unrecorded; ``--task KEY:ASSERTION=SLUG[,SLUG...]``
  (repeatable) adds the entry under those worklog tasks and records it.

An entry whose assertion now passes is removed (the tests would report it as an
unexpected success). An orphan entry, one naming a (tier, case) the suite does
not trace, an assertion that case does not make or one with nothing to hold, is
refused unless ``--remove-orphans`` is given, which removes it. An entry's tasks
are edited in the JSON by hand.

All or nothing: the table is written only when nothing was refused, no traced
case raised, and every failing assertion is recorded; otherwise every finding is
reported, nothing is written and the exit status is 1. ``--dry-run`` reports
without writing.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vinylgen.fidelity import bounds, regression  # noqa: E402

WORK = ROOT / ".tmp" / "record-expected-failures"


def _job(index: int) -> tuple[str, dict]:
    tier, case = regression.all_runs()[index]
    key = f"{tier}/{case.case_id}"
    try:
        judged = regression.run_and_judge(tier, case, WORK / tier / case.case_id)
        return key, {"verdicts": judged["verdicts"]}
    except Exception:
        return key, {"error": traceback.format_exc()}


def parse_tasks(values: list[str]) -> dict[tuple[str, str], list[str]]:
    """``KEY:ASSERTION=SLUG[,SLUG...]`` values as ``{(key, assertion): [slug, ...]}``."""
    tasks = {}
    for value in values:
        match = re.fullmatch(r"(.+):([a-z]+)=([a-z0-9,-]+)", value)
        slugs = match.group(3).split(",") if match else []
        if not match or not all(regression.SLUG_PATTERN.match(slug) for slug in slugs):
            raise SystemExit(f"--task {value!r}: expected TIER/CASE:ASSERTION=SLUG[,SLUG...]")
        tasks[(match.group(1), match.group(2))] = slugs
    return tasks


def reconcile(table: dict, results: dict, traced: dict, tasks: dict[tuple[str, str], list],
              allow_worse: bool = False, remove_orphans: bool = False) -> dict:
    """Bring ``table`` (changed in place) up to date with ``results`` (``_job``'s
    rows by ``tier/case_id`` key) for the traced runs ``traced`` (key to ``(tier,
    case)``), as the module docstring says. Returns the report: ``changes``,
    ``orphans``, ``unrecorded``, ``refused``, ``errors`` (lists of lines) and
    ``blocked`` (whether nothing may be written)."""
    refused, unrecorded, errors, changes, orphans = [], [], [], [], []
    for key in sorted(table):
        if key not in traced:
            orphans.append(f"{key}: no such traced (tier, case) "
                           f"({', '.join(sorted(table[key]))})")
            if remove_orphans:
                del table[key]
            continue
        tier, case = traced[key]
        made = bounds.applicable(case, tier)
        for name in sorted(table[key]):
            if name not in made:
                orphans.append(f"{key} {name}: the case makes no {name} assertion")
                if remove_orphans:
                    del table[key][name]

    for key, row in sorted(results.items()):
        if "error" in row:
            errors.append(f"{key}: {row['error']}")
            continue
        entries = table.setdefault(key, {})
        for name, verdict in row["verdicts"].items():
            entry = entries.get(name)
            if "skip" in verdict:
                if entry is not None:
                    orphans.append(f"{key} {name}: entry for an assertion with nothing to "
                                   f"hold ({verdict['skip']})")
                    if remove_orphans:
                        del entries[name]
                continue
            if verdict["passed"]:
                if entry is not None:
                    changes.append(f"{key} {name}: now passes; entry removed")
                    del entries[name]
                continue
            measured = verdict["measured"]
            if entry is None:
                slugs = tasks.get((key, name))
                if slugs is None:
                    unrecorded.append(f"{key} {name}: fails and has no entry: "
                                      f"{verdict['detail'][:300]}")
                    continue
                entries[name] = {"tasks": slugs, "measured": measured}
                changes.append(f"{key} {name}: added under {','.join(slugs)}: {measured}")
                continue
            recorded = entry.get("measured", {})
            shared = {k: v for k, v in recorded.items() if k in measured}
            worse = bounds.worse_than_recorded(shared, {k: measured[k] for k in shared})
            if worse and not allow_worse:
                refused.append(f"{key} {name}: worse than recorded: " + "; ".join(worse))
                continue
            # A record only tightens: a measurement worse than recorded, even within
            # the margin, keeps its recorded value unless worse is allowed.
            kept = {k: v if allow_worse or k not in recorded else min(float(recorded[k]), v)
                    for k, v in measured.items()}
            if kept != recorded:
                changes.append(f"{key} {name}: {recorded} -> {kept}")
                entry["measured"] = kept
    for key in [key for key, entries in table.items() if not entries]:
        del table[key]
    blocked = bool(refused or errors or unrecorded or (orphans and not remove_orphans))
    return {"changes": changes, "orphans": orphans, "unrecorded": unrecorded,
            "refused": refused, "errors": errors, "blocked": blocked}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="regular expression over tier/case_id keys")
    parser.add_argument("--allow-worse", action="store_true",
                        help="record measurements worse than the recorded ones")
    parser.add_argument("--task", action="append", default=[],
                        help="TIER/CASE:ASSERTION=SLUG[,SLUG...]: add an entry for a new "
                             "failure")
    parser.add_argument("--remove-orphans", action="store_true",
                        help="remove entries for a (tier, case) or assertion the suite does "
                             "not trace or assert")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    tasks = parse_tasks(args.task)

    table = regression.load_expected_failures()
    runs = regression.all_runs()
    traced = {f"{tier}/{case.case_id}": (tier, case) for tier, case in runs}
    selected = [index for index, (tier, case) in enumerate(runs)
                if args.only is None or re.search(args.only, f"{tier}/{case.case_id}")]
    with ProcessPoolExecutor(max(1, args.jobs)) as pool:
        results = dict(pool.map(_job, selected))
    report = reconcile(table, results, traced, tasks, args.allow_worse, args.remove_orphans)

    for line in report["changes"]:
        print("CHANGE", line)
    for line in report["orphans"]:
        print("ORPHAN", line, "(removed)" if args.remove_orphans else "")
    for line in report["unrecorded"]:
        print("UNRECORDED", line)
    for line in report["refused"]:
        print("REFUSED", line)
    for line in report["errors"]:
        print("ERROR", line)
    if report["blocked"]:
        print("nothing written: a record is worse than the table's, a case failed, a failure "
              "has no entry (--task), or an orphan entry is kept (--remove-orphans)")
        return 1
    if not args.dry_run:
        regression.write_expected_failures(table)
        print(f"wrote {regression.EXPECTED_FAILURES_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
