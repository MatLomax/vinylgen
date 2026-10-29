from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

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
    result_revision_id TEXT REFERENCES revisions(id), owner_token TEXT
);
CREATE INDEX IF NOT EXISTS revisions_project ON revisions(project_id, created_at);
CREATE INDEX IF NOT EXISTS artifacts_revision ON artifacts(revision_id, created_at);
"""

# The artifact kinds each stale concern hides. The trace concern covers the trace
# run's own outputs and an Inkscape edit of that run's SVG: both describe geometry
# a retrace replaces.
STALE_ARTIFACT_KINDS = {
    "palette": {"flattened", "segmentation", "palette", "report", "protected-areas-overlay",
                "before-cleanup-flattened", "before-cleanup-segmentation",
                "before-cleanup-palette", "before-cleanup-report"},
    "cleanup": {"cleanup-audit", "cleanup-changes-overlay", "cleaned-flattened", "cleaned-labels"},
    "trace": {
        "traced-map", "traced-overlap", "traced-edge", "traced-preview", "trace-audit",
        "trace-fallbacks", "trace-semantic-warnings", "trace-inspection",
        "trace-fallbacks-overlay", "trace-missed-details-overlay",
        "inkscape-svg", "inkscape-preview", "inkscape-audit",
    },
    "validation": {"validation"},
    "export": {"export"},
}

# Everything a ``trace`` revision supersedes from older revisions, whatever its
# recorded ``stale_concerns``: every trace-derived artifact of an older run, and
# the validation and export made of that run.
TRACE_RUN_SUPERSEDES = (
    STALE_ARTIFACT_KINDS["trace"] | STALE_ARTIFACT_KINDS["validation"]
    | STALE_ARTIFACT_KINDS["export"])

# Everything a ``palette`` (flatten) revision supersedes from older revisions: every
# flatten output of an older run, including the protected-areas overlay a run that
# protected nothing does not write.
PALETTE_RUN_SUPERSEDES = STALE_ARTIFACT_KINDS["palette"]


# The raster a cleanup revision rewrites, and the overlay of what it changed; a newer
# cleanup replaces them, even one that changed nothing and so wrote neither. A label edit
# on the cleaned raster (``describes_cleaned_raster``) writes the same kinds.
CLEANUP_RASTER_KINDS = {"flattened", "segmentation", "palette", "report",
                        "cleanup-changes-overlay"}

# A rename or recolour on the cleaned raster also rewrites the raster a Cleanup run
# starts from, stored under these kinds (and filenames, so the two rasters of one
# revision never share a file); they resolve as the kind and filename they name only
# where that revision's cleaned raster is skipped (``Store.effective_artifacts``).
BEFORE_CLEANUP_KINDS = {"before-cleanup-flattened": ("flattened", "flattened.png"),
                        "before-cleanup-segmentation": ("segmentation", "segmentation.npz"),
                        "before-cleanup-palette": ("palette", "palette.json"),
                        "before-cleanup-report": ("report", "report.png")}

# The Cleaned image while touch-ups on it are shown: the current cleanup's own result
# before any touch-up, in the current colours. A Cleanup run whose replayed touch-ups
# changed its raster writes both (its labels, and the image); a recolour of the cleaned
# raster while touch-ups are shown writes the image again in the new colours (a rename
# changes no colour, so it writes nothing).
# Like a cleaned raster they are skipped once a newer cleanup has been passed and in a
# before-cleanup lookup, and an export never packages them.
CLEANED_IMAGE_KINDS = {"cleaned-flattened", "cleaned-labels"}

# Kinds that belong to their own revision only and never resolve through ancestry: the
# touch-ups a revision records for a later Cleanup run to replay (``web.touch_ups``).
REVISION_ONLY_KINDS = {"touch-ups"}


def describes_cleaned_raster(revision: dict[str, Any]) -> bool:
    """Whether ``revision``'s raster artifacts are a cleaned raster: a cleanup's, or a
    label edit's made on the cleaned raster of a current cleanup."""
    return revision["kind"] == "cleanup" or bool(revision["settings"].get("cleaned_raster"))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


def _proc_starttime(pid: int) -> str:
    """The process's start time in clock ticks, field 22 of ``/proc/<pid>/stat``.

    Pairing it with the PID yields an identity that survives PID reuse: a recycled
    PID belonging to a different process reports a different start time.
    """
    with open(f"/proc/{pid}/stat", "r") as handle:
        data = handle.read()
    # The comm field (2) is parenthesised and may contain spaces, so parse after
    # its closing paren; starttime is field 22, i.e. index 19 of the remainder.
    return data[data.rfind(")") + 2:].split()[19]


# Distinguishes this process from any other when procfs is unavailable and a start
# time cannot be read; regenerated per process, so a reused PID never collides.
_INSTANCE_NONCE = uuid.uuid4().hex


def _process_token(pid: int | None = None) -> str:
    """Identity of the owning process as ``<pid>:<starttime>``.

    On Linux the start time (from procfs) makes the token survive PID reuse and lets
    the reaper prove liveness. On a platform without procfs the start time is
    unavailable, so the token falls back to ``<pid>:<per-process nonce>`` — still
    unique per process (a reused PID cannot forge a live owner's token), though
    liveness then cannot be proven and the reaper degrades to reaping on any second
    construction, as it did before ownership scoping.
    """
    pid = os.getpid() if pid is None else pid
    try:
        return f"{pid}:{_proc_starttime(pid)}"
    except (OSError, IndexError):
        return f"{pid}:{_INSTANCE_NONCE}"


def _process_alive(token: str | None) -> bool:
    """True only when the token's originating process is still running.

    Distinguishes a job orphaned by a genuine restart (its process is gone) from a
    job that belongs to another live process reading the same database.
    """
    if not token:
        return False
    pid_text, _, starttime = token.partition(":")
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    try:
        return _proc_starttime(pid) == starttime
    except (OSError, IndexError):
        return False


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
    def __init__(self, config: WebConfig, *, owner_token: str | None = None,
                 owner_alive: Callable[[str | None], bool] | None = None):
        self.config = config
        # Identity stamped onto every job this process creates, and the predicate the
        # reaper uses to tell a genuinely orphaned job from one another live process is
        # still working. Both are injectable so tests can simulate a dead prior owner.
        self.owner_token = owner_token or _process_token()
        self._owner_alive = owner_alive or _process_alive
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.files_dir.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            job_columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            if "result_revision_id" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN result_revision_id TEXT REFERENCES revisions(id)")
            if "owner_token" not in job_columns:
                db.execute("ALTER TABLE jobs ADD COLUMN owner_token TEXT")
            now = _now()
            # Fail only jobs whose owning process has died. A restart leaves this
            # process's token unmatched by any live process, so its orphans are reaped;
            # a second Store opened against the same database by a dev server, migration
            # script, or REPL leaves the live server's in-flight jobs untouched.
            orphaned = [
                row["id"]
                for row in db.execute(
                    "SELECT id, owner_token FROM jobs WHERE status IN ('queued', 'running')")
                if row["owner_token"] != self.owner_token and not self._owner_alive(row["owner_token"])
            ]
            if orphaned:
                db.executemany(
                    "UPDATE jobs SET status='failed', message=?, error=?, finished_at=? WHERE id=?",
                    [("Interrupted by process restart",
                      "Worker process stopped before completion", now, job_id)
                     for job_id in orphaned],
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

    def create_upscaled_source(self, parent_revision_id: str, *, message: str,
                               settings: dict[str, Any], checkpoint: str, seed: int,
                               source: BinaryIO) -> str:
        """Persist a tiled-regen upscale as a new, parent-linked immutable revision.

        Mirrors ``create_generated_source``'s atomic blob/revision write, but links to
        ``parent_revision_id`` instead of starting a new root and marks every downstream
        stage stale, since the tiled regen redraws the source pixels. The checkpoint and
        seed are folded into the persisted settings alongside the rest of the effective
        upscale configuration, so the exact ComfyUI run is reconstructable from the
        revision alone.
        """
        parent = self.get_revision(parent_revision_id)
        project = self.get_project(parent["project_id"])
        revision_id, artifact_id, now = _id(), _id(), _now()
        effective_settings = {
            **settings, "checkpoint": checkpoint, "seed": seed,
            "stale_concerns": ["palette", "cleanup", "trace", "validation", "export"],
        }
        sha256, size, incoming = self._stage_incoming(artifact_id, source)
        storage_path = str(blob_relpath(project["slug"], "upscaled-source", now, "source.png"))
        try:
            with self.connection() as db:
                db.execute(
                    "INSERT INTO revisions VALUES (?, ?, ?, 'upscaled-source', ?, ?, ?)",
                    (revision_id, project["id"], parent_revision_id, message,
                     json.dumps(effective_settings, sort_keys=True, separators=(",", ":")), now),
                )
                db.execute(
                    "INSERT INTO artifacts VALUES (?, ?, 'source', 'source.png', "
                    "'image/png', ?, ?, ?, ?)",
                    (artifact_id, revision_id, size, sha256, storage_path, now),
                )
                db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project["id"]))
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

    def effective_artifacts(self, revision_id: str, *, before_cleanup: bool = False
                            ) -> dict[str, dict[str, Any]]:
        """Resolve current artifacts through ancestry without exposing stale ancestors.

        The newest artifact of each kind wins. Stale concerns accumulate while walking:
        once a revision marks a concern stale, the artifacts of that concern's kinds
        from every older revision are excluded. A cleanup replaces any earlier cleanup,
        so the raster artifacts and changes overlay of a cleaned raster (a ``cleanup``
        revision, or a label edit on its cleaned raster: ``describes_cleaned_raster``,
        ``CLEANUP_RASTER_KINDS``, and the ``CLEANED_IMAGE_KINDS``) are skipped once a
        newer cleanup has been passed;
        with ``before_cleanup`` they are skipped from the start, which resolves the
        raster that cleanup starts from. Where a cleaned raster is skipped, its
        revision's ``BEFORE_CLEANUP_KINDS`` (a rename or recolour of the raster before
        cleanup) resolve as the kinds they name; elsewhere they are never returned, nor
        are ``REVISION_ONLY_KINDS``. A ``trace`` run
        writes its complete output set, so it supersedes every trace artifact of an
        older run, including optional audits that run did not repeat, and an older
        run's Inkscape edit, validation and export (``TRACE_RUN_SUPERSEDES``); this
        holds for trace revisions recorded before a trace marked validation stale.
        Likewise a ``palette`` run supersedes every flatten output of an older run,
        including its optional protected-areas overlay (``PALETTE_RUN_SUPERSEDES``).
        """
        result: dict[str, dict[str, Any]] = {}
        current: str | None = revision_id
        seen: set[str] = set()
        stale_kinds: set[str] = set()
        cleanup_superseded = before_cleanup
        while current:
            if current in seen:
                raise ValueError("revision ancestry contains a cycle")
            seen.add(current)
            revision = self.get_revision(current)
            skip_raster = describes_cleaned_raster(revision) and cleanup_superseded
            for row in self.list_artifacts(current):
                kind = row["kind"]
                if kind in stale_kinds or kind in REVISION_ONLY_KINDS:
                    continue
                if skip_raster and (kind in CLEANUP_RASTER_KINDS or kind in CLEANED_IMAGE_KINDS):
                    continue
                if kind in BEFORE_CLEANUP_KINDS:
                    if not skip_raster:
                        continue
                    kind, filename = BEFORE_CLEANUP_KINDS[kind]
                    row = {**row, "kind": kind, "filename": filename}
                result.setdefault(kind, row)
            stale_kinds.update(
                kind
                for concern in revision["settings"].get("stale_concerns", ())
                for kind in STALE_ARTIFACT_KINDS.get(concern, ()))
            if revision["kind"] == "cleanup":
                cleanup_superseded = True
            if revision["kind"] == "trace":
                stale_kinds.update(TRACE_RUN_SUPERSEDES)
            if revision["kind"] == "palette":
                stale_kinds.update(PALETTE_RUN_SUPERSEDES)
            current = revision["parent_revision_id"]
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
                "INSERT INTO jobs VALUES (?, ?, ?, ?, 'queued', 0, ?, NULL, ?, NULL, NULL, NULL, ?)",
                (job_id, project_id, revision_id, kind, "Queued", now, self.owner_token),
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
