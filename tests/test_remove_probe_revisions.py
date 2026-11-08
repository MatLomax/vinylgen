"""``scripts/remove_probe_revisions.py`` removes exactly the named probe revisions, after a backup, and
refuses (removing nothing) a revision with a child left behind, one a job references, one created
outside the probe windows, and one with another message."""
from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from vinylgen.web.config import WebConfig
from vinylgen.web.store import Store

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("remove_probe_revisions",
                                               ROOT / "scripts" / "remove_probe_revisions.py")
remover = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(remover)

WIDE = ["--window", "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"]


class RemoveProbeRevisionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.store = Store(WebConfig(self.data))
        self.project = self.store.create_project("Probe target")["id"]
        self.base = self.revision(None, "Deterministic trace pipeline")
        self.a = self.revision(self.base, "edited in nib")
        self.b = self.revision(self.a, "edited in nib")
        self.kept = self.revision(self.base, "edited in nib")

    def revision(self, parent: str | None, message: str) -> str:
        rid = self.store.create_revision(self.project, parent_revision_id=parent, kind="trace",
                                         message=message, settings={})["id"]
        self.store.add_artifact(rid, kind="traced-map", filename="map.json",
                                media_type="application/json", source=io.BytesIO(rid.encode()))
        return rid

    def run_main(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = remover.main(["--data-dir", str(self.data), *args])
        return code, out.getvalue()

    def ids(self) -> set[str]:
        return {r["id"] for r in self.store.list_revisions(self.project)}

    def folder(self, rid: str) -> Path:
        return self.store.artifact_path(self.store.list_artifacts(rid)[0]["id"]).parent

    def test_the_named_revisions_go_with_their_folders_and_nothing_else(self):
        folders = {rid: self.folder(rid) for rid in (self.base, self.a, self.b, self.kept)}
        backup = Path(self.temp.name) / "backup"
        code, out = self.run_main(*WIDE, "--backup", str(backup), self.a, self.b)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ids(), {self.base, self.kept})
        self.assertFalse(folders[self.a].exists() or folders[self.b].exists())
        self.assertTrue(folders[self.base].is_dir() and folders[self.kept].is_dir())
        self.assertTrue((backup / "vinylgen.sqlite3").is_file())
        for rid in (self.a, self.b):
            copied = backup / folders[rid].relative_to(self.data) / "map.json"
            self.assertEqual(copied.read_bytes(), rid.encode())
        self.assertIn("foreign_key_check 0 rows, integrity ok", out)

    def assert_refused(self, code: int, out: str, why: str) -> None:
        self.assertEqual(code, 1, out)
        self.assertIn(why, out)
        self.assertIn("refused: nothing removed", out)
        self.assertEqual(self.ids(), {self.base, self.a, self.b, self.kept})

    def test_a_child_left_behind_is_refused(self):
        self.assert_refused(*self.run_main(*WIDE, self.a), "child revisions not removed")

    def test_a_revision_a_job_references_is_refused(self):
        self.store.create_job(kind="trace", project_id=self.project, revision_id=self.kept)
        self.assert_refused(*self.run_main(*WIDE, self.kept), "referenced by 1 row(s) of jobs.revision_id")

    def test_a_revision_outside_the_windows_or_with_another_message_is_refused(self):
        code, out = self.run_main("--window", "2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00",
                                  self.kept)
        self.assert_refused(code, out, "in no probe window")
        self.assert_refused(*self.run_main(*WIDE, self.base), "message 'Deterministic trace pipeline'")

    def test_ids_recorded_in_a_file_by_the_probes_are_removed(self):
        recorded = Path(self.temp.name) / "probe-revisions.txt"
        recorded.write_text(f"{self.a}\n{self.b}\n")
        code, out = self.run_main(*WIDE, "--backup", str(Path(self.temp.name) / "b"), "--ids-file", str(recorded))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ids(), {self.base, self.kept})

    def test_a_dry_run_removes_nothing(self):
        code, out = self.run_main(*WIDE, "--dry-run", self.a, self.b)
        self.assertEqual(code, 0, out)
        self.assertIn("dry run: would remove 2 revisions", out)
        self.assertEqual(self.ids(), {self.base, self.a, self.b, self.kept})


if __name__ == "__main__":
    unittest.main()
