"""The pieces of a save from nib's editor that the frozen ``test_nib_embed`` does not reach:
the vendored package's pins, ``nib.map_refusal`` on its own, and a later trace from an edited
revision tracing afresh instead of re-exporting the edit as t3's map."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import tarfile
import tempfile
import unittest
from pathlib import Path

import t3api

from test_nib_embed import _IdentityApi, move_one_node
from test_t3_trace import FIXTURE, SCRATCH, build_subject
from vinylgen.web.nib import NIB_EDITOR, map_refusal

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "web" / "vendor"


class VendoredPackageTest(unittest.TestCase):
    """``web/package.json`` installs the tarball ``web/vendor/README.md`` pins, and the version
    the server records as the editor's is that tarball's."""

    def test_the_dependency_the_readme_and_the_recorded_editor_name_one_tarball(self):
        dependency = json.loads((ROOT / "web" / "package.json").read_text())["dependencies"]["@nib/core"]
        match = re.fullmatch(r"file:vendor/(nib-core-(\d+\.\d+\.\d+)\.tgz)", dependency)
        self.assertIsNotNone(match, dependency)
        tarball = VENDOR / match.group(1)
        readme = (VENDOR / "README.md").read_text()
        self.assertIn(f"## {tarball.name}", readme)
        self.assertIn(f"`{hashlib.sha256(tarball.read_bytes()).hexdigest()}`", readme)
        with tarfile.open(tarball) as archive:
            manifest = json.load(archive.extractfile("package/package.json"))
        self.assertEqual((manifest["name"], manifest["version"]), ("@nib/core", match.group(2)))
        self.assertEqual(NIB_EDITOR, {"name": "nib", "version": manifest["version"]})


class MapRefusalTest(unittest.TestCase):
    PARENT = (FIXTURE / "map.json").read_bytes()

    def test_an_edit_is_accepted(self):
        self.assertIsNone(map_refusal(move_one_node(self.PARENT.decode()).encode(), self.PARENT))

    def test_text_that_is_not_utf8_is_invalid(self):
        self.assertEqual(map_refusal(b"\xff\xfe", self.PARENT)["reason"], "invalid")

    def test_a_document_missing_a_key_is_invalid(self):
        pm = json.loads(self.PARENT)
        del pm["faces"]
        refusal = map_refusal(json.dumps(pm).encode(), self.PARENT)
        self.assertEqual(refusal["reason"], "invalid", refusal)
        self.assertTrue(refusal["problems"], refusal)

    def test_another_height_palette_or_names_is_a_mismatch(self):
        for key, change in (("H", lambda pm: pm["H"] + 1),
                            ("palette", lambda pm: [[0, 0, 0]] + pm["palette"][1:]),
                            ("names", lambda pm: ["Renamed"] + pm["names"][1:])):
            pm = json.loads(self.PARENT)
            pm[key] = change(pm)
            refusal = map_refusal(json.dumps(pm, separators=(",", ":")).encode(), self.PARENT)
            self.assertEqual(refusal["reason"], "mismatch", (key, refusal))
            self.assertTrue(any(problem.startswith(f"{key} is") for problem in refusal["problems"]),
                            refusal)

    def test_the_parents_own_bytes_are_unchanged(self):
        self.assertEqual(map_refusal(self.PARENT, self.PARENT)["reason"], "unchanged")


class TraceAfterAnEditTest(unittest.IsolatedAsyncioTestCase):
    """A trace from an edited revision does not reuse the edited map: it is not t3's trace of
    the labels. The map of a revision t3 traced is still reused."""

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_the_edited_map_is_never_reused(self):
        async with _IdentityApi(self, self.root / "data") as api:
            project_id, palette_id = await api.palette_revision(build_subject(self.root / "subject"))
            trace_id = await api.trace(project_id, palette_id)
            parent_map = (await api.artifact(trace_id, "traced-map")).decode()
            saved = await api.post_map(trace_id, move_one_node(parent_map))
            self.assertEqual(saved.status_code, 201, saved.text)
            runner = api.app.state.jobs.pipeline
            setting = t3api.normal_setting(None)
            self.assertIsNotNone(await asyncio.to_thread(runner.reusable_map, trace_id, setting))
            self.assertIsNone(await asyncio.to_thread(runner.reusable_map, saved.json()["id"],
                                                      setting))
