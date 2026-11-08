"""Remove revisions a live probe made from the store, after backing them up and checking each is safe.

A live probe (a smoke test of the deployed service) saves revisions into the live store. This removes
exactly the revisions named, and nothing else, through the tables and paths ``Store.delete_project``
handles (``artifacts``, ``jobs``, ``revisions`` and the blob folders under ``files/<slug>/``), scoped to
those revision ids. Each named revision must:

- exist, and have been created inside one of the probe runs' windows (``--window``, from the cards'
  logs) with the message a probe's save gives it (``--message``);
- have no child revision that is not also named;
- not be referenced by any other row: every foreign key to ``revisions(id)`` in the schema is read
  (``jobs.revision_id`` and ``jobs.result_revision_id`` among them), and the ``projects`` rows and
  every other revision's ``settings_json`` are searched for its id. The schema keeps no current or
  pinned revision on a project; were a column added that names one, the foreign-key scan finds it;
- own its blob folder alone (no other revision's artifact is stored in it).

The project may have no queued or running job. Any failed check removes nothing.

Before removing, the database (through SQLite's backup API, so the copy is consistent under WAL) and
the named revisions' blob folders are copied into ``--backup``. ``--dry-run`` runs every check and
prints what would go, writing nothing. The ids are given as arguments or in files (``--ids-file``): every
live probe that saves appends the id of each revision it makes to such a file, so the run's last step
removes them.

Run: .venv/bin/python scripts/remove_probe_revisions.py --data-dir .vinylgen-data \\
       --window START END --backup .tmp/x/backup [--dry-run] [--ids-file FILE] [ID...]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def connect(database: Path) -> sqlite3.Connection:
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def resolve(db: sqlite3.Connection, ids: list[str]) -> list[dict]:
    """Each id (whole, or a prefix naming one revision) as its revision row."""
    rows = []
    for given in ids:
        found = db.execute("SELECT * FROM revisions WHERE id LIKE ?", (given + "%",)).fetchall()
        if len(found) != 1:
            raise SystemExit(f"{given}: {len(found)} revisions match, not one")
        rows.append(dict(found[0]))
    return rows


def revision_references(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every (table, column) whose foreign key points at revisions(id)."""
    out = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for fk in db.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if fk["table"] == "revisions" and fk["to"] in ("id", None):
                out.append((table, fk["from"]))
    return out


def folders_of(db: sqlite3.Connection, revision_id: str) -> set[str]:
    """The blob folders (relative to the data directory) the revision's artifacts are stored in."""
    return {str(Path(row["storage_path"]).parent)
            for row in db.execute("SELECT storage_path FROM artifacts WHERE revision_id=?", (revision_id,))}


def problems_of(db: sqlite3.Connection, rows: list[dict], windows: list[tuple[datetime, datetime]],
                message: str) -> dict[str, list[str]]:
    """Each named revision's failed checks (empty when it is safe to remove)."""
    named = {row["id"] for row in rows}
    references = revision_references(db)
    out: dict[str, list[str]] = {}
    for row in rows:
        rid, problems = row["id"], []
        created = datetime.fromisoformat(row["created_at"])
        if not any(start <= created <= end for start, end in windows):
            problems.append(f"created {row['created_at']}, in no probe window")
        if row["message"] != message:
            problems.append(f"message {row['message']!r}, not {message!r}")
        children = [r["id"] for r in db.execute("SELECT id FROM revisions WHERE parent_revision_id=?", (rid,))]
        stray = [c for c in children if c not in named]
        if stray:
            problems.append(f"child revisions not removed: {stray}")
        for table, column in references:
            if (table, column) in (("artifacts", "revision_id"), ("revisions", "parent_revision_id")):
                continue
            hits = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (rid,)).fetchone()[0]
            if hits:
                problems.append(f"referenced by {hits} row(s) of {table}.{column}")
        for project in db.execute("SELECT * FROM projects").fetchall():
            if rid in json.dumps(dict(project)):
                problems.append(f"named by project {project['slug']}")
        mentions = [r["id"] for r in db.execute(
            "SELECT id FROM revisions WHERE settings_json LIKE ? AND id<>?", (f"%{rid}%", rid))]
        if [m for m in mentions if m not in named]:
            problems.append(f"named in the settings of revisions {mentions}")
        for folder in folders_of(db, rid):
            others = db.execute(
                "SELECT COUNT(*) FROM artifacts WHERE storage_path LIKE ? AND revision_id<>?",
                (folder + "/%", rid)).fetchone()[0]
            if others:
                problems.append(f"folder {folder} holds {others} artifact(s) of another revision")
        out[rid] = problems
    return out


def removal_order(rows: list[dict]) -> list[str]:
    """The named revisions, every child before its parent, so the immediate foreign-key checks hold."""
    parent = {row["id"]: row["parent_revision_id"] for row in rows}

    def depth(rid: str) -> int:
        d = 0
        while parent.get(rid) in parent:
            rid, d = parent[rid], d + 1
        return d

    return sorted(parent, key=depth, reverse=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="the probe revisions' ids (whole, or a unique prefix)")
    ap.add_argument("--ids-file", type=Path, action="append", default=[],
                    help="a file of probe revision ids, one per line, as the live probes record them")
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--window", nargs=2, action="append", required=True, metavar=("START", "END"),
                    help="a probe run's window, ISO 8601 with offset (repeatable)")
    ap.add_argument("--message", default="edited in nib")
    ap.add_argument("--backup", type=Path, help="where the database and the folders are copied first")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    windows = [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in args.window]
    data = args.data_dir.resolve()
    db = connect(data / "vinylgen.sqlite3")
    ids = list(args.ids)
    for path in args.ids_file:
        ids += [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if not ids:
        raise SystemExit("no revision named")
    rows = resolve(db, list(dict.fromkeys(ids)))
    projects = {row["project_id"] for row in rows}
    failed = False
    for project_id in projects:
        active = db.execute("SELECT COUNT(*) FROM jobs WHERE project_id=? AND status IN ('queued', 'running')",
                            (project_id,)).fetchone()[0]
        if active:
            print(f"FAIL project {project_id} has {active} active job(s)")
            failed = True
    problems = problems_of(db, rows, windows, args.message)
    folders: dict[str, set[str]] = {}
    for row in rows:
        rid = row["id"]
        folders[rid] = folders_of(db, rid)
        artifacts = db.execute("SELECT COUNT(*) FROM artifacts WHERE revision_id=?", (rid,)).fetchone()[0]
        state = "ok  " if not problems[rid] else "FAIL"
        print(f"{state} {rid} parent {row['parent_revision_id']} created {row['created_at']} "
              f"{row['message']!r}: {artifacts} artifacts in {sorted(folders[rid])}"
              + ("" if not problems[rid] else f"; {problems[rid]}"))
        failed = failed or bool(problems[rid])
    before = {p: db.execute("SELECT COUNT(*) FROM revisions WHERE project_id=?", (p,)).fetchone()[0]
              for p in projects}
    if failed:
        print("refused: nothing removed")
        return 1
    if args.dry_run:
        print(f"dry run: would remove {len(rows)} revisions ({before} -> "
              f"{ {p: n - sum(r['project_id'] == p for r in rows) for p, n in before.items()} })")
        return 0
    if args.backup is None:
        raise SystemExit("--backup is required to remove")
    args.backup.mkdir(parents=True, exist_ok=True)
    copy = sqlite3.connect(args.backup / "vinylgen.sqlite3")
    db.backup(copy)
    copy.close()
    for rid in removal_order(rows):
        for folder in folders[rid]:
            shutil.copytree(data / folder, args.backup / folder)
    print(f"backed up the database and {sum(map(len, folders.values()))} folders to {args.backup}")
    ids = removal_order(rows)
    with db:
        marks = ",".join("?" * len(ids))
        db.execute(f"DELETE FROM artifacts WHERE revision_id IN ({marks})", ids)
        db.execute(f"DELETE FROM jobs WHERE revision_id IN ({marks}) OR result_revision_id IN ({marks})",
                   ids + ids)
        for rid in ids:
            db.execute("DELETE FROM revisions WHERE id=?", (rid,))
    files = (data / "files").resolve()
    for rid in ids:
        for folder in folders[rid]:
            target = (data / folder).resolve()
            if files not in target.parents:
                raise SystemExit(f"{target} is not under {files}")
            shutil.rmtree(target)
    left = db.execute(f"SELECT COUNT(*) FROM revisions WHERE id IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
    fk = db.execute("PRAGMA foreign_key_check").fetchall()
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    gone = all(not (data / f).exists() for fs in folders.values() for f in fs)
    after = {p: db.execute("SELECT COUNT(*) FROM revisions WHERE project_id=?", (p,)).fetchone()[0]
             for p in projects}
    print(f"removed {len(ids)} revisions: left {left}, folders gone {gone}, foreign_key_check {len(fk)} rows, "
          f"integrity {integrity}, revisions {before} -> {after}")
    return 0 if left == 0 and gone and not fk and integrity == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
