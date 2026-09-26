#!/usr/bin/env python3
"""Record the measurements and identities of the synthetic fidelity expected-failure table.

Traces every (tier, case) of the regression corpus (``--only REGEX`` narrows the
selection to matching ``tier/case_id`` keys) exactly as
``tests/test_synthetic_fidelity.py`` does, and for every assertion that fails:

- with a table entry, writes its current measurements and identities into the
  entry. A worse record (a measurement larger than the recorded one beyond
  ``bounds.RATCHET_TOLERANCE``, or an identity item not recorded) is a
  regression and is refused unless ``--allow-worse`` is given. An entry whose
  record lacks a measurement or identity kind the assertion now has is recorded
  afresh, the kinds it shares with the new record still compared;
- without one, it is unrecorded; ``--task KEY:ASSERTION=SLUG`` (repeatable) adds
  the entry under that worklog task and records it.

An entry whose assertion now passes is removed (the tests would report it as an
unexpected success). An orphan entry, one naming a (tier, case) the suite does
not trace, an assertion that case does not make or one with nothing to hold, is
refused unless ``--remove-orphans`` is given, which removes it.

``--retask KEY:ASSERTION=SLUG`` (repeatable) moves existing entries to another
worklog task without tracing, measurements and identities unchanged, for when a
cause is re-attributed.

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


def parse_tasks(values: list[str]) -> dict[tuple[str, str], str]:
    tasks = {}
    for value in values:
        match = re.fullmatch(r"(.+):([a-z]+)=([a-z0-9-]+)", value)
        if not match:
            raise SystemExit(f"--task {value!r}: expected TIER/CASE:ASSERTION=SLUG")
        tasks[(match.group(1), match.group(2))] = match.group(3)
    return tasks


def _fresh(name: str, entry: dict) -> bool:
    """Whether ``entry``'s record lacks a kind the assertion now measures."""
    return (set(entry.get("measured", {})) != set(bounds.MEASUREMENTS[name])
            or set(entry.get("identities", {})) != set(bounds.IDENTITIES[name]))


def reconcile(table: dict, results: dict, traced: dict, tasks: dict[tuple[str, str], str],
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
                    orphans.append(f"{key} {name}: entry {entry['task']} for an assertion with "
                                   f"nothing to hold ({verdict['skip']})")
                    if remove_orphans:
                        del entries[name]
                continue
            if verdict["passed"]:
                if entry is not None:
                    changes.append(f"{key} {name}: now passes; entry {entry['task']} removed")
                    del entries[name]
                continue
            measured, found = verdict["measured"], verdict["identities"]
            if entry is None:
                slug = tasks.get((key, name))
                if slug is None:
                    unrecorded.append(f"{key} {name}: fails and has no entry: "
                                      f"{verdict['detail'][:300]}")
                    continue
                entries[name] = {"task": slug, "measured": measured, "identities": found}
                changes.append(f"{key} {name}: added under {slug}: {measured}")
                continue
            if _fresh(name, entry):
                shared = {k: v for k, v in entry.get("measured", {}).items() if k in measured}
                worse, _ = bounds.ratchet(shared, {k: measured[k] for k in shared})
                if worse and not allow_worse:
                    refused.append(f"{key} {name} ({entry['task']}): worse than recorded: "
                                   + "; ".join(worse))
                    continue
                entry["measured"], entry["identities"] = measured, found
                changes.append(f"{key} {name}: recorded afresh {measured}"
                               + (f" ({'; '.join(worse)})" if worse else ""))
                continue
            worse, better = bounds.entry_ratchet(entry, measured, found)
            if worse and not allow_worse:
                refused.append(f"{key} {name} ({entry['task']}): worse than recorded: "
                               + "; ".join(worse))
                continue
            if worse or better:
                changes.append(f"{key} {name}: " + "; ".join(worse + better))
                entry["measured"], entry["identities"] = measured, found
    for key in [key for key, entries in table.items() if not entries]:
        del table[key]
    blocked = bool(refused or errors or unrecorded or (orphans and not remove_orphans))
    return {"changes": changes, "orphans": orphans, "unrecorded": unrecorded,
            "refused": refused, "errors": errors, "blocked": blocked}


def retask(moves: dict, dry_run: bool) -> int:
    """Move each named entry to its new task; nothing is written if any is missing."""
    table = regression.load_expected_failures()
    missing = []
    for (key, name), slug in moves.items():
        entry = table.get(key, {}).get(name)
        if entry is None:
            missing.append(f"{key}:{name}")
        elif isinstance(entry, str):
            table[key][name] = slug
        else:
            entry["task"] = slug
        if entry is not None:
            print("RETASK", f"{key}:{name}", "->", slug)
    if missing:
        print("nothing written: no entry for", ", ".join(missing))
        return 1
    if not dry_run:
        regression.write_expected_failures(table)
        print(f"wrote {regression.EXPECTED_FAILURES_PATH.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="regular expression over tier/case_id keys")
    parser.add_argument("--allow-worse", action="store_true",
                        help="record measurements and identities worse than the recorded ones")
    parser.add_argument("--task", action="append", default=[],
                        help="TIER/CASE:ASSERTION=SLUG: add an entry for a new failure")
    parser.add_argument("--retask", action="append", default=[],
                        help="TIER/CASE:ASSERTION=SLUG: move an existing entry to another task "
                             "without tracing")
    parser.add_argument("--remove-orphans", action="store_true",
                        help="remove entries for a (tier, case) or assertion the suite does "
                             "not trace or assert")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if args.retask:
        return retask(parse_tasks(args.retask), args.dry_run)
    tasks = parse_tasks(args.task)

    # An entry may name only its task (a slug string, or {"task": slug}) until
    # it is first recorded.
    table = {key: {name: ({"task": entry} if isinstance(entry, str) else dict(entry))
                   for name, entry in entries.items()}
             for key, entries in regression.load_expected_failures().items()}
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
