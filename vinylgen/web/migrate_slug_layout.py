"""One-time migration from content-addressed blob storage to slug folders.

Old layout stored each artifact once at ``artifacts/<sha[:2]>/<sha>`` and let several
revisions share a blob. The new layout gives every artifact its own file at
``files/<project-slug>/<stage>-<revision-timestamp>/<filename>`` (see
``store.blob_relpath``), so the tree is human-navigable at the cost of that dedup.

The migration copies each blob to its new home and rewrites ``storage_path``; it never
moves or deletes, so it is safe to run while the service is live and safe to re-run. The
old ``artifacts/`` tree is left in place for inspection — remove it only after
``verify_rehomed`` reports every old blob accounted for.
"""

from __future__ import annotations

import argparse
import hashlib
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


def migrate(config: WebConfig) -> dict[str, object]:
    """Copy every artifact blob into its slug folder and rewrite its storage_path."""
    moved, skipped, errors = 0, 0, []
    db = sqlite3.connect(config.database_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        for row in _artifact_rows(db):
            target_rel = blob_relpath(row["slug"], row["stage"], row["rev_created_at"], row["filename"])
            target_rel_str = str(target_rel)
            if row["storage_path"] == target_rel_str:
                skipped += 1
                continue
            source = config.data_dir / row["storage_path"]
            destination = config.data_dir / target_rel
            if not source.exists():
                # Already relocated by an earlier run: accept the new home if it is present.
                if destination.exists():
                    db.execute("UPDATE artifacts SET storage_path=? WHERE id=?", (target_rel_str, row["id"]))
                    moved += 1
                else:
                    errors.append(f"{row['id']}: source blob missing at {row['storage_path']}")
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and _sha256(destination) != row["sha256"]:
                errors.append(f"{row['id']}: {target_rel_str} already holds different content")
                continue
            # Copy (never move): several rows may share one old blob and each needs its own file.
            shutil.copyfile(source, destination)
            db.execute("UPDATE artifacts SET storage_path=? WHERE id=?", (target_rel_str, row["id"]))
            moved += 1
        db.commit()
    finally:
        db.close()
    return {"moved": moved, "skipped": skipped, "errors": errors}


def verify_rehomed(config: WebConfig) -> dict[str, object]:
    """Confirm every artifact resolves to a present, content-correct file, and that every
    blob in the old ``artifacts/`` tree is accounted for by a rehomed artifact."""
    db = sqlite3.connect(config.database_path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute("SELECT id, sha256, storage_path FROM artifacts").fetchall()
    finally:
        db.close()

    verified, bad = 0, []
    rehomed_shas: set[str] = set()
    for row in rows:
        path = config.data_dir / row["storage_path"]
        if not path.exists():
            bad.append(f"{row['id']}: missing file at {row['storage_path']}")
            continue
        if _sha256(path) != row["sha256"]:
            bad.append(f"{row['id']}: content at {row['storage_path']} does not match recorded sha256")
            continue
        verified += 1
        rehomed_shas.add(row["sha256"])

    old_blobs, orphans = 0, []
    artifact_dir = config.artifact_dir
    if artifact_dir.exists():
        for blob in artifact_dir.rglob("*"):
            if not blob.is_file() or blob.name.startswith("."):
                continue
            old_blobs += 1
            if blob.name not in rehomed_shas:
                orphans.append(str(blob.relative_to(config.data_dir)))

    return {
        "artifacts": len(rows),
        "verified": verified,
        "bad": bad,
        "old_blobs": old_blobs,
        "orphans": orphans,
        "all_rehomed": not bad and not orphans,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate artifact storage to slug folders.")
    parser.add_argument("data_dir", nargs="?", help="Data directory (defaults to $VINYLGEN_DATA_DIR).")
    args = parser.parse_args()
    config = WebConfig(Path(args.data_dir).expanduser().resolve()) if args.data_dir else WebConfig.from_env()

    result = migrate(config)
    print(f"Migrated {result['moved']} artifact(s); {result['skipped']} already in place.")
    for error in result["errors"]:
        print(f"  ERROR: {error}")

    check = verify_rehomed(config)
    print(f"Verified {check['verified']}/{check['artifacts']} artifacts against their sha256.")
    print(f"Old blob files: {check['old_blobs']}; orphaned (no rehomed artifact references them): "
          f"{len(check['orphans'])}.")
    for orphan in check["orphans"]:
        print(f"  ORPHAN: {orphan}")
    for bad in check["bad"]:
        print(f"  BAD: {bad}")
    if check["all_rehomed"] and not result["errors"]:
        print("All files from the old sha tree have been rehomed. The old artifacts/ tree "
              "is retained; remove it once you have confirmed the result.")
        return 0
    print("Migration incomplete — the old artifacts/ tree was left untouched.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
