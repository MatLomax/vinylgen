from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from vinylgen.web.config import WebConfig
from vinylgen.web.migrate_slug_layout import migrate, verify_rehomed
from vinylgen.web.store import Store


class SlugLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(self.temp.cleanup)
        self.config = WebConfig(Path(self.temp.name))
        self.store = Store(self.config)

    def test_imported_source_lands_in_a_slug_folder(self) -> None:
        content = b"source-image-bytes"
        project, revision = self.store.import_project(
            "Desert Mech", filename="art.png", media_type="image/png", source=BytesIO(content))
        [artifact] = self.store.list_artifacts(revision["id"])
        self.assertTrue(artifact["storage_path"].startswith("files/desert-mech/source-"))
        self.assertTrue(artifact["storage_path"].endswith("/art.png"))
        stored = self.config.data_dir / artifact["storage_path"]
        self.assertEqual(stored.read_bytes(), content)
        self.assertEqual(self.store.artifact_path(artifact["id"]), stored)

    def test_identical_names_get_distinct_slug_folders(self) -> None:
        first, _ = self.store.import_project(
            "Sky Whale", filename="a.png", media_type="image/png", source=BytesIO(b"a"))
        second, _ = self.store.import_project(
            "Sky Whale", filename="b.png", media_type="image/png", source=BytesIO(b"b"))
        self.assertEqual(first["slug"], "sky-whale")
        self.assertEqual(second["slug"], "sky-whale-2")

    def test_added_artifacts_share_their_revisions_folder(self) -> None:
        project = self.store.create_project("Griffin Castle")
        revision = self.store.create_revision(
            project["id"], parent_revision_id=None, kind="palette", message="x", settings={})
        flattened = self.store.add_artifact(
            revision["id"], kind="flattened", filename="flattened.png",
            media_type="image/png", source=BytesIO(b"flat"))
        report = self.store.add_artifact(
            revision["id"], kind="report", filename="report.png",
            media_type="image/png", source=BytesIO(b"rep"))
        folder = (self.config.data_dir / flattened["storage_path"]).parent
        self.assertEqual((self.config.data_dir / report["storage_path"]).parent, folder)
        self.assertEqual(folder.parent.name, "griffin-castle")
        self.assertTrue(folder.name.startswith("palette-"))


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(self.temp.cleanup)
        self.config = WebConfig(Path(self.temp.name))
        self.store = Store(self.config)

    def _write_old_blob(self, content: bytes) -> tuple[str, str]:
        sha = hashlib.sha256(content).hexdigest()
        blob = self.config.artifact_dir / sha[:2] / sha
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
        return sha, f"artifacts/{sha[:2]}/{sha}"

    def _insert_artifact(self, revision_id: str, kind: str, filename: str,
                         content: bytes, sha: str, storage_path: str) -> str:
        artifact_id = f"art-{kind}"
        db = sqlite3.connect(self.config.database_path)
        try:
            db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (artifact_id, revision_id, kind, filename, "image/png",
                        len(content), sha, storage_path, "2026-08-23T10:00:00+00:00"))
            db.commit()
        finally:
            db.close()
        return artifact_id

    def test_migration_rehomes_blobs_and_expands_dedup(self) -> None:
        project = self.store.create_project("Kraken Lighthouse")
        revision = self.store.create_revision(
            project["id"], parent_revision_id=None, kind="palette", message="x", settings={})
        # One old blob shared by two artifact rows — the CAS dedup case.
        shared = b"shared-blob-content"
        sha, old_path = self._write_old_blob(shared)
        self._insert_artifact(revision["id"], "flattened", "flattened.png", shared, sha, old_path)
        self._insert_artifact(revision["id"], "report", "report.png", shared, sha, old_path)

        result = migrate(self.config)
        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["errors"], [])

        artifacts = self.store.list_artifacts(revision["id"])
        paths = {row["filename"]: row["storage_path"] for row in artifacts}
        for filename in ("flattened.png", "report.png"):
            self.assertTrue(paths[filename].startswith("files/kraken-lighthouse/palette-"))
            self.assertEqual((self.config.data_dir / paths[filename]).read_bytes(), shared)
        # Dedup expands: the two rows now have their own files.
        self.assertNotEqual(paths["flattened.png"], paths["report.png"])
        # Copy, not move: the original blob is left in place for inspection.
        self.assertTrue((self.config.data_dir / old_path).exists())

        check = verify_rehomed(self.config)
        self.assertTrue(check["all_rehomed"], check)
        self.assertEqual(check["verified"], 2)
        self.assertEqual(check["old_blobs"], 1)
        self.assertEqual(check["orphans"], [])

    def test_migration_is_idempotent(self) -> None:
        project = self.store.create_project("Floating Castle")
        revision = self.store.create_revision(
            project["id"], parent_revision_id=None, kind="trace", message="x", settings={})
        content = b"trace-output"
        sha, old_path = self._write_old_blob(content)
        self._insert_artifact(revision["id"], "traced-edge", "traced-edge.svg", content, sha, old_path)

        first = migrate(self.config)
        self.assertEqual(first["moved"], 1)
        second = migrate(self.config)
        self.assertEqual(second["moved"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertTrue(verify_rehomed(self.config)["all_rehomed"])


if __name__ == "__main__":
    unittest.main()
