from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .config import WebConfig


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    parent_revision_id TEXT REFERENCES revisions(id), kind TEXT NOT NULL,
    message TEXT, settings_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES revisions(id),
    kind TEXT NOT NULL, filename TEXT NOT NULL, media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL, UNIQUE(revision_id, kind, filename)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id),
    revision_id TEXT REFERENCES revisions(id), kind TEXT NOT NULL,
    status TEXT NOT NULL, progress REAL NOT NULL, message TEXT, error TEXT,
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    result_revision_id TEXT REFERENCES revisions(id)
);
CREATE INDEX IF NOT EXISTS revisions_project ON revisions(project_id, created_at);
CREATE INDEX IF NOT EXISTS artifacts_revision ON artifacts(revision_id, created_at);
"""

STALE_ARTIFACT_KINDS = {
    "palette": {"flattened", "segmentation", "palette", "report"},
    "trace": {
        "traced-overlap", "traced-edge", "traced-preview", "trace-audit",
        "trace-fallbacks", "trace-semantic-warnings", "trace-inspection",
    },
    "validation": {"validation"},
    "export": {"export"},
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or "project"


def _timestamp_slug(created_at: str) -> str:
    """Filesystem-safe, chronologically sortable form of an ISO 8601 timestamp.

    Microsecond precision is kept so two revisions of one stage cannot share a folder.
    """
    return datetime.fromisoformat(created_at).strftime("%Y%m%d-%H%M%S-%f")


def blob_relpath(slug: str, stage: str, created_at: str, filename: str) -> Path:
    """Canonical on-disk location of an artifact blob, relative to the data directory.

    ``files/<project-slug>/<revision-timestamp>-<stage>/<filename>`` — one folder per
    revision, timestamp first so a project's stages interleave in chronological order when
    listed. Shared by the save path and the reconcile step so both agree where a blob lives.
    """
    return Path("files") / slug / f"{_timestamp_slug(created_at)}-{stage}" / Path(filename).name


class Store:
    def __init__(self, config: WebConfig):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.files_dir.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "result_revision_id" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN result_revision_id TEXT REFERENCES revisions(id)")
            now = _now()
            db.execute(
                "UPDATE jobs SET status='failed', message=?, error=?, finished_at=? "
                "WHERE status IN ('queued', 'running')",
                ("Interrupted by process restart", "Worker process stopped before completion", now),
            )
            if config.retention_days > 0:
                cutoff = (datetime.now(UTC) - timedelta(days=config.retention_days)).isoformat()
                db.execute(
                    "DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled') "
                    "AND finished_at IS NOT NULL AND finished_at < ?", (cutoff,))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.config.database_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _one(self, query: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(query, args).fetchone()
        return dict(row) if row else None

    def _stage_incoming(self, artifact_id: str, source: BinaryIO) -> tuple[str, int, Path]:
        """Stream an upload into a temp file at the files-dir root, hashing as it lands."""
        self.config.files_dir.mkdir(parents=True, exist_ok=True)
        incoming = self.config.files_dir / f".{artifact_id}.incoming"
        digest = hashlib.sha256()
        size = 0
        try:
            with incoming.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
                    target.write(chunk)
        except BaseException:
            incoming.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), size, incoming

    def _finalize(self, incoming: Path, storage_path: str) -> None:
        """Move a staged blob to its canonical slug path once its row is written."""
        destination = self.config.data_dir / storage_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(incoming, destination)

    def create_project(self, name: str) -> dict[str, Any]:
        project_id, now, base = _id(), _now(), _slug(name)
        with self.connection() as db:
            slug, suffix = base, 2
            while db.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                slug, suffix = f"{base}-{suffix}", suffix + 1
            db.execute(
                "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                (project_id, slug, name.strip(), now, now),
            )
        return self.get_project(project_id)

    def import_project(self, name: str, *, filename: str, media_type: str,
                       source: BinaryIO) -> tuple[dict[str, Any], dict[str, Any]]:
        """Create an imported project and its source revision as one DB transaction."""
        project_id, revision_id, artifact_id = _id(), _id(), _id()
        now, base = _now(), _slug(name)
        sha256, size, incoming = self._stage_incoming(artifact_id, source)
        try:
            with self.connection() as db:
                slug, suffix = base, 2
                while db.execute("SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                    slug, suffix = f"{base}-{suffix}", suffix + 1
                storage_path = str(blob_relpath(slug, "source", now, filename))
                db.execute(
                    "INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                    (project_id, slug, name.strip(), now, now),
                )
                db.execute(
                    "INSERT INTO revisions VALUES (?, ?, NULL, 'source', ?, '{}', ?)",
                    (revision_id, project_id, "Imported source", now),
                )
                db.execute(
                    "INSERT INTO artifacts VALUES (?, ?, 'source', ?, ?, ?, ?, ?, ?)",
                    (artifact_id, revision_id, Path(filename).name, media_type, size, sha256,
                     storage_path, now),
                )
                self._finalize(incoming, storage_path)
        finally:
            incoming.unlink(missing_ok=True)

        return self.get_project(project_id), self.get_revision(revision_id)

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM projects WHERE id=?", (project_id,))
        if row is None:
            raise KeyError(project_id)
        return row

    def delete_project(self, project_id: str) -> None:
        """Remove a project and everything under it: revisions, artifacts, jobs, and blobs.

        Refuses while the project has a queued or running job, whose worker still writes
        revisions against it; the caller cancels that job first. Rows go in foreign-key
        order (children before the project) so the immediate FK checks hold; the blob tree
        is dropped only after the rows commit.
        """
        project = self.get_project(project_id)
        with self.connection() as db:
            active = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE project_id=? AND status IN ('queued', 'running')",
                (project_id,),
            ).fetchone()[0]
            if active:
                raise ValueError("Cancel the project's active job before deleting it")
            db.execute(
                "DELETE FROM artifacts WHERE revision_id IN "
                "(SELECT id FROM revisions WHERE project_id=?)", (project_id,))
            db.execute("DELETE FROM jobs WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM revisions WHERE project_id=?", (project_id,))
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        shutil.rmtree(self.config.files_dir / project["slug"], ignore_errors=True)

    def create_revision(self, project_id: str, *, parent_revision_id: str | None,
                        kind: str, message: str | None, settings: dict[str, Any]) -> dict[str, Any]:
        self.get_project(project_id)
        if parent_revision_id:
            parent = self.get_revision(parent_revision_id)
            if parent["project_id"] != project_id:
                raise ValueError("parent revision belongs to another project")
        revision_id, now = _id(), _now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (revision_id, project_id, parent_revision_id, kind, message,
                 json.dumps(settings, sort_keys=True, separators=(",", ":")), now),
            )
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
        return self.get_revision(revision_id)

    def create_generated_source(self, project_id: str, *, message: str,
                                settings: dict[str, Any], source: BinaryIO) -> str:
        """Persist a generated revision and its sole source artifact atomically."""
        project = self.get_project(project_id)
        revision_id, artifact_id, now = _id(), _id(), _now()
        sha256, size, incoming = self._stage_incoming(artifact_id, source)
        storage_path = str(blob_relpath(project["slug"], "generated-source", now, "source.png"))
        try:
            with self.connection() as db:
                db.execute(
                    "INSERT INTO revisions VALUES (?, ?, NULL, 'generated-source', ?, ?, ?)",
                    (revision_id, project_id, message,
                     json.dumps(settings, sort_keys=True, separators=(",", ":")), now),
                )
                db.execute(
                    "INSERT INTO artifacts VALUES (?, ?, 'source', 'source.png', "
                    "'image/png', ?, ?, ?, ?)",
                    (artifact_id, revision_id, size, sha256, storage_path, now),
                )
                db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
                self._finalize(incoming, storage_path)
        finally:
            incoming.unlink(missing_ok=True)
        return revision_id

    def get_revision(self, revision_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM revisions WHERE id=?", (revision_id,))
        if row is None:
            raise KeyError(revision_id)
        row["settings"] = json.loads(row.pop("settings_json"))
        return row

    def list_revisions(self, project_id: str) -> list[dict[str, Any]]:
        self.get_project(project_id)
        with self.connection() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM revisions WHERE project_id=? ORDER BY created_at DESC", (project_id,)
            )]
        for row in rows:
            row["settings"] = json.loads(row.pop("settings_json"))
        return rows

    def add_artifact(self, revision_id: str, *, kind: str, filename: str,
                     media_type: str, source: BinaryIO) -> dict[str, Any]:
        revision = self.get_revision(revision_id)
        project = self.get_project(revision["project_id"])
        artifact_id, now = _id(), _now()
        sha256, size, incoming = self._stage_incoming(artifact_id, source)
        storage_path = str(blob_relpath(
            project["slug"], revision["kind"], revision["created_at"], filename))
        try:
            with self.connection() as db:
                db.execute(
                    "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (artifact_id, revision_id, kind, Path(filename).name, media_type,
                     size, sha256, storage_path, now),
                )
                self._finalize(incoming, storage_path)
        finally:
            # Only the private staging file is safe to remove when a write or DB op fails;
            # the committed blob lives under the revision's slug folder.
            incoming.unlink(missing_ok=True)
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
        if row is None:
            raise KeyError(artifact_id)
        return row

    def artifact_path(self, artifact_id: str) -> Path:
        row = self.get_artifact(artifact_id)
        return self.config.data_dir / row["storage_path"]

    def list_artifacts(self, revision_id: str) -> list[dict[str, Any]]:
        self.get_revision(revision_id)
        with self.connection() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM artifacts WHERE revision_id=? ORDER BY created_at", (revision_id,)
            )]

    def effective_artifacts(self, revision_id: str) -> dict[str, dict[str, Any]]:
        """Resolve current artifacts through ancestry without exposing stale ancestors."""
        result: dict[str, dict[str, Any]] = {}
        current: str | None = revision_id
        seen: set[str] = set()
        root = self.get_revision(revision_id)
        stale_kinds = {
            kind
            for concern in root["settings"].get("stale_concerns", ())
            for kind in STALE_ARTIFACT_KINDS.get(concern, ())
        }
        while current:
            if current in seen:
                raise ValueError("revision ancestry contains a cycle")
            seen.add(current)
            for row in self.list_artifacts(current):
                if current != revision_id and row["kind"] in stale_kinds:
                    continue
                result.setdefault(row["kind"], row)
            current = self.get_revision(current)["parent_revision_id"]
        return result

    def create_job(self, *, kind: str, project_id: str | None,
                   revision_id: str | None) -> dict[str, Any]:
        if project_id:
            self.get_project(project_id)
        if revision_id:
            revision = self.get_revision(revision_id)
            if project_id and revision["project_id"] != project_id:
                raise ValueError("revision belongs to another project")
        job_id, now = _id(), _now()
        with self.connection() as db:
            db.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, 'queued', 0, ?, NULL, ?, NULL, NULL, NULL)",
                (job_id, project_id, revision_id, kind, "Queued", now),
            )
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        return row

    def active_job_count(self) -> int:
        with self.connection() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
        return int(row[0])

    def update_job(self, job_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {"status", "progress", "message", "error", "started_at", "finished_at",
                   "result_revision_id"}
        fields = {key: value for key, value in changes.items() if key in allowed}
        if not fields:
            return self.get_job(job_id)
        assignments = ", ".join(f"{key}=?" for key in fields)
        with self.connection() as db:
            db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))
        return self.get_job(job_id)
