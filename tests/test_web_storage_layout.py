from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from vinylgen.web.config import WebConfig
from vinylgen.web.migrate_slug_layout import restructure, verify
from vinylgen.web.store import Store, _timestamp_slug

TS = r"\d{8}-\d{6}-\d{6}"  # YYYYMMDD-HHMMSS-ffffff


class SlugLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(self.temp.cleanup)
        self.config = WebConfig(Path(self.temp.name))
        self.store = Store(self.config)

    def test_imported_source_lands_in_a_timestamp_first_slug_folder(self) -> None:
        content = b"source-image-bytes"
        project, revision = self.store.import_project(
            "Desert Mech", filename="art.png", media_type="image/png", source=BytesIO(content))
        [artifact] = self.store.list_artifacts(revision["id"])
        self.assertRegex(artifact["storage_path"], rf"^files/desert-mech/{TS}-source/art\.png$")
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

    def test_upscaled_source_is_a_parent_linked_stale_marking_revision(self) -> None:
        project = self.store.create_project("Desert Rocket")
        source = self.store.create_revision(
            project["id"], parent_revision_id=None, kind="generated-source",
            message="Generated", settings={})
        content = b"upscaled-source-bytes"

        revision_id = self.store.create_upscaled_source(
            source["id"], message="Tiled regen upscale",
            settings={"upscaler": "4xNomos8kSCHAT-S.pth", "upscale_factor": 2.0},
            checkpoint="blue_pencil-XL-v7.0.0.safetensors", seed=12345,
            source=BytesIO(content))

        revision = self.store.get_revision(revision_id)
        self.assertEqual(revision["parent_revision_id"], source["id"])
        self.assertEqual(revision["kind"], "upscaled-source")
        self.assertEqual(revision["settings"]["upscaler"], "4xNomos8kSCHAT-S.pth")
        self.assertEqual(revision["settings"]["upscale_factor"], 2.0)
        self.assertEqual(revision["settings"]["checkpoint"], "blue_pencil-XL-v7.0.0.safetensors")
        self.assertEqual(revision["settings"]["seed"], 12345)
        self.assertEqual(revision["settings"]["stale_concerns"],
                         ["palette", "trace", "validation", "export"])

        [artifact] = self.store.list_artifacts(revision_id)
        self.assertEqual(artifact["kind"], "source")
        self.assertEqual(artifact["filename"], "source.png")
        self.assertRegex(artifact["storage_path"],
                         rf"^files/desert-rocket/{TS}-upscaled-source/source\.png$")
        stored = self.config.data_dir / artifact["storage_path"]
        self.assertEqual(stored.read_bytes(), content)

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
        self.assertRegex(folder.name, rf"^{TS}-palette$")


class RestructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(self.temp.cleanup)
        self.config = WebConfig(Path(self.temp.name))
        self.store = Store(self.config)

    def _stage_first_artifact(self, revision: dict, kind: str, filename: str, content: bytes) -> str:
        """Write an artifact in the previous stage-first layout to be reconciled."""
        ts = _timestamp_slug(revision["created_at"])
        rel = f"files/{self.store.get_project(revision['project_id'])['slug']}/{revision['kind']}-{ts}/{filename}"
        path = self.config.data_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        artifact_id = f"art-{kind}"
        db = sqlite3.connect(self.config.database_path)
        try:
            db.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (artifact_id, revision["id"], kind, filename, "image/png",
                        len(content), hashlib.sha256(content).hexdigest(), rel, revision["created_at"]))
            db.commit()
        finally:
            db.close()
        return rel

    def test_restructure_renames_stage_first_folders_to_timestamp_first(self) -> None:
        project = self.store.create_project("Kraken Lighthouse")
        revision = self.store.create_revision(
            project["id"], parent_revision_id=None, kind="palette", message="x", settings={})
        old_rel = self._stage_first_artifact(revision, "flattened", "flattened.png", b"flat-bytes")
        old_folder = (self.config.data_dir / old_rel).parent

        result = restructure(self.config)
        self.assertEqual(result["relinked"], 1)
        self.assertEqual(result["errors"], [])

        [artifact] = self.store.list_artifacts(revision["id"])
        ts = _timestamp_slug(revision["created_at"])
        self.assertEqual(artifact["storage_path"],
                         f"files/kraken-lighthouse/{ts}-palette/flattened.png")
        new_path = self.config.data_dir / artifact["storage_path"]
        self.assertEqual(new_path.read_bytes(), b"flat-bytes")
        # The old stage-first folder is emptied and removed.
        self.assertFalse(old_folder.exists())
        self.assertTrue(verify(self.config)["ok"])

    def test_restructure_is_idempotent_and_skips_canonical_artifacts(self) -> None:
        project = self.store.create_project("Floating Castle")
        revision = self.store.create_revision(
            project["id"], parent_revision_id=None, kind="trace", message="x", settings={})
        self._stage_first_artifact(revision, "traced-edge", "traced-edge.svg", b"<svg/>")
        # An artifact saved by the current code is already canonical and must be left alone.
        self.store.add_artifact(revision["id"], kind="trace-audit", filename="trace.json",
                                media_type="application/json", source=BytesIO(b"{}"))

        first = restructure(self.config)
        self.assertEqual(first["relinked"], 1)
        self.assertEqual(first["skipped"], 1)
        second = restructure(self.config)
        self.assertEqual(second["relinked"], 0)
        self.assertEqual(second["skipped"], 2)
        self.assertTrue(verify(self.config)["ok"])


if __name__ == "__main__":
    unittest.main()
