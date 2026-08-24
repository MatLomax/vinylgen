"""Reconcile artifact blobs on disk to the canonical slug layout.

Artifacts live at ``files/<project-slug>/<timestamp>-<stage>/<filename>`` (see
``store.blob_relpath``). Whenever that formula changes — the original move off
content-addressed ``artifacts/<sha>`` storage, or the later timestamp-first reordering —
this reconciles every artifact whose recorded ``storage_path`` no longer matches.

It relinks in place: each blob is hard-linked to its new path, the database is committed,
and only then is the old name removed. At every instant the path the database points at
exists, so it is safe to run against a live store; it copies only if a hard link is
impossible (a cross-filesystem move), never doubles the tree, and is safe to re-run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

from .config import WebConfig
from .store import blob_relpath


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_rows(db: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.execute(
        "SELECT a.id, a.filename, a.sha256, a.storage_path, "
        "       r.kind AS stage, r.created_at AS rev_created_at, p.slug AS slug "
        "FROM artifacts a "
        "JOIN revisions r ON r.id = a.revision_id "
        "JOIN projects p ON p.id = r.project_id"
    ).fetchall()


def restructure(config: WebConfig) -> dict[str, object]:
    """Relink every artifact whose storage_path diverges from its canonical location."""
    relinked: list[tuple[str, Path, str]] = []
    skipped, errors = 0, []
    db = sqlite3.connect(config.database_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        for row in _artifact_rows(db):
            target = blob_relpath(row["slug"], row["stage"], row["rev_created_at"], row["filename"])
            target_str = str(target)
            if row["storage_path"] == target_str:
                skipped += 1
                continue
            source = config.data_dir / row["storage_path"]
            destination = config.data_dir / target
            if destination.exists():
                if _sha256(destination) != row["sha256"]:
                    errors.append(f"{row['id']}: {target_str} already holds different content")
                    continue
            elif source.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(source, destination)  # instant, same inode, both paths valid
                except OSError:
                    shutil.copyfile(source, destination)  # fallback across filesystems
            else:
                errors.append(f"{row['id']}: source blob missing at {row['storage_path']}")
                continue
            relinked.append((row["id"], source, target_str))

        # Commit every storage_path first, so the DB never points at a name we then remove.
        for artifact_id, _, target_str in relinked:
            db.execute("UPDATE artifacts SET storage_path=? WHERE id=?", (target_str, artifact_id))
        db.commit()
    finally:
        db.close()

    old_dirs: set[Path] = set()
    for _, source, target_str in relinked:
        if source.exists() and source != config.data_dir / target_str:
            source.unlink()
        old_dirs.add(source.parent)
    for directory in old_dirs:
        try:
            directory.rmdir()  # only succeeds once the folder is empty
        except OSError:
            pass
    return {"relinked": len(relinked), "skipped": skipped, "errors": errors}


def verify(config: WebConfig) -> dict[str, object]:
    """Confirm every artifact resolves to a present file whose bytes match its sha256."""
    db = sqlite3.connect(config.database_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT id, sha256, storage_path FROM artifacts").fetchall()
    finally:
        db.close()

    verified, bad = 0, []
    for row in rows:
        path = config.data_dir / row["storage_path"]
        if not path.exists():
            bad.append(f"{row['id']}: missing file at {row['storage_path']}")
        elif _sha256(path) != row["sha256"]:
            bad.append(f"{row['id']}: content at {row['storage_path']} does not match sha256")
        else:
            verified += 1
    return {"artifacts": len(rows), "verified": verified, "bad": bad, "ok": not bad}


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile artifact storage to the slug layout.")
    parser.add_argument("data_dir", nargs="?", help="Data directory (defaults to $VINYLGEN_DATA_DIR).")
    args = parser.parse_args()
    config = WebConfig(Path(args.data_dir).expanduser().resolve()) if args.data_dir else WebConfig.from_env()

    result = restructure(config)
    print(f"Relinked {result['relinked']} artifact(s); {result['skipped']} already in place.")
    for error in result["errors"]:
        print(f"  ERROR: {error}")

    check = verify(config)
    print(f"Verified {check['verified']}/{check['artifacts']} artifacts against their sha256.")
    for bad in check["bad"]:
        print(f"  BAD: {bad}")
    if check["ok"] and not result["errors"]:
        print("Every artifact resolves to a present, content-correct file.")
        return 0
    print("Reconcile incomplete.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
