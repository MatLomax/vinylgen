#!/usr/bin/env python3
"""Record the measurements of the synthetic fidelity expected-failure table.

Traces every (tier, case) of the regression corpus (``--only REGEX`` narrows the
selection to matching ``tier/case_id`` keys) exactly as
``tests/test_synthetic_fidelity.py`` does, and for every assertion that fails:

- with a table entry, writes its current measurements into the entry. A
  measurement worse than the recorded one (beyond ``bounds.RATCHET_TOLERANCE``)
  is a regression and is refused, and nothing is written, unless
  ``--allow-worse`` is given; an entry without ``measured`` is recorded fresh;
- without one, reports it; ``--task KEY:ASSERTION=SLUG`` (repeatable) adds the
  entry under that worklog task and records it.

An entry whose assertion now passes is reported and removed (the tests would
report it as an unexpected success). ``--dry-run`` reports without writing.
The exit status is non-zero when anything was refused or left unrecorded.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="regular expression over tier/case_id keys")
    parser.add_argument("--allow-worse", action="store_true",
                        help="record measurements worse than the recorded ones")
    parser.add_argument("--task", action="append", default=[],
                        help="TIER/CASE:ASSERTION=SLUG: add an entry for a new failure")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    tasks = parse_tasks(args.task)

    # An entry may name only its task (a slug string, or {"task": slug}) until
    # its measurements are first recorded.
    table = {key: {name: ({"task": entry} if isinstance(entry, str) else dict(entry))
                   for name, entry in entries.items()}
             for key, entries in regression.load_expected_failures().items()}
    runs = regression.all_runs()
    selected = [index for index, (tier, case) in enumerate(runs)
                if args.only is None or re.search(args.only, f"{tier}/{case.case_id}")]
    with ProcessPoolExecutor(max(1, args.jobs)) as pool:
        results = dict(pool.map(_job, selected))

    refused, unrecorded, errors, changes = [], [], [], []
    for key, row in sorted(results.items()):
        if "error" in row:
            errors.append(f"{key}: {row['error']}")
            continue
        entries = table.get(key, {})
        for name, verdict in row["verdicts"].items():
            entry = entries.get(name)
            if "skip" in verdict:
                if entry is not None:
                    errors.append(f"{key} {name}: entry for an assertion with nothing to "
                                  f"hold ({verdict['skip']})")
                continue
            if verdict["passed"]:
                if entry is not None:
                    changes.append(f"{key} {name}: now passes; entry {entry['task']} removed")
                    del entries[name]
                continue
            measured = verdict["measured"]
            if entry is None:
                slug = tasks.get((key, name))
                if slug is None:
                    unrecorded.append(f"{key} {name}: fails and has no entry: "
                                      f"{verdict['detail'][:300]}")
                    continue
                entries[name] = {"task": slug, "measured": measured}
                table[key] = entries
                changes.append(f"{key} {name}: added under {slug}: {measured}")
                continue
            if "measured" not in entry:
                entry["measured"] = measured
                changes.append(f"{key} {name}: recorded {measured}")
                continue
            worse, better = bounds.ratchet(entry["measured"], measured)
            if worse and not args.allow_worse:
                refused.append(f"{key} {name} ({entry['task']}): worse than recorded: "
                               + "; ".join(worse))
                continue
            if worse or better:
                changes.append(f"{key} {name}: " + "; ".join(worse + better))
                entry["measured"] = measured
    for key in [key for key, entries in table.items() if not entries]:
        del table[key]

    for line in changes:
        print("CHANGE", line)
    for line in unrecorded:
        print("UNRECORDED", line)
    for line in refused:
        print("REFUSED", line)
    for line in errors:
        print("ERROR", line)
    if refused or errors:
        print("nothing written: a measurement is worse than recorded or a case failed")
        return 1
    if not args.dry_run:
        regression.write_expected_failures(table)
        print(f"wrote {regression.EXPECTED_FAILURES_PATH.relative_to(ROOT)}")
    return 1 if unrecorded else 0


if __name__ == "__main__":
    raise SystemExit(main())
