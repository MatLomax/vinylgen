"""Card 13 of vinylgen-on-t3: a save made by nib's own code on a 0.27.0 map keeps the map's setting.

The tracer card brief ``cards/vinylgen-on-t3/build-13-brief.md``, step 3. Card 12a's two nib saves
(``test_kind_tolerance_settings.KindTraceChainTest``) post a document built by the Python
``move_one_node``, which keeps the header and never reaches nib's reader, so they pinned the
server's side while the real save path failed: nib core 0.4.0 refused to open every map t3api
0.27.0 writes (its ``setting`` holds the per-kind ``half`` table).

Here the edit is nib's. Each map is traced by the live app's trace job on the test array, one with
the arc off and one with the cubic at 2.5, and handed to the installed ``@nib/core``
(``web/node_modules/@nib/core``, the package the web client bundles) under Node:
``LabelMapSession.fromText`` (the editor's ``open``, through ``readLabelMap``), ``moveNodes`` on the
first view node between two line pieces (the node tool's move, which converts no piece), and
``session.text()`` (the text the editor's ``saveDocument`` hands the host, through
``writeLabelMap``). What nib wrote is posted to the save route, and the new trace revision's
``map.json`` header carries the input's setting: the parsed ``setting`` equal, and the header
(everything before ``"nodes":``) byte for byte.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import t3api

from test_nib_embed import ROOT, _IdentityApi
from test_t3_settings import effective, run_job
from test_t3_trace import SCRATCH, build_subject

NIB_CORE = ROOT / "web" / "node_modules" / "@nib" / "core"
NODE_BOUND_S = 60

# The installed nib core's session: open, move one L-L node by (dx, dy), write. Args: the
# package's dist directory, the input map's path, the output path, dx, dy. Prints the moved
# point and nib's own check of the written document as JSON.
NIB_MOVE = r"""
import { readFileSync, writeFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
const [dist, input, output, dx, dy] = process.argv.slice(1)
const { LabelMapSession } = await import(pathToFileURL(`${dist}/label-map-session.js`).href)
const session = LabelMapSession.fromText(readFileSync(input, 'utf8'))
let ref = null, at = null
for (let k = 0; k < session.map.edges.length && !ref; k++) {
  const edge = session.map.edges[k]
  if (edge.left === null) continue
  for (let j = 1; j < edge.pieces.length && !ref; j++) {
    if (edge.pieces[j].kind === 'L' && edge.pieces[j - 1].kind === 'L') {
      ref = session.nodeRefOf(session.faceIndexOf(edge.left), { edge: k, j })
      at = { edge: k, j }
    }
  }
}
if (!ref) throw new Error('no view node between two line pieces')
session.moveNodes([ref], Number(dx), Number(dy))
const text = session.text()
writeFileSync(output, text)
console.log(JSON.stringify({ at, violations: session.check().violations }))
"""


def header(text: str) -> str:
    """A planar-map file's header: everything before ``"nodes":``."""
    return text[:text.index('"nodes":')]


def nib_move(map_text: str, work: Path) -> tuple[str, dict]:
    """The document the installed nib core writes after one L-L node moves by (0.25, 0.25)."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is on PATH: the save is made by nib's own code")
    source, written = work / "input.json", work / "nib-written.json"
    source.write_text(map_text, encoding="utf-8")
    run = subprocess.run(
        [node, "--input-type=module", "-e", NIB_MOVE, str(NIB_CORE / "dist"), str(source),
         str(written), "0.25", "0.25"],
        capture_output=True, text=True, timeout=NODE_BOUND_S, check=False)
    if run.returncode != 0:
        raise AssertionError(f"nib core failed on the map: {run.stderr.strip()}")
    return written.read_text(encoding="utf-8"), json.loads(run.stdout)


class NibSaveThroughNibTest(unittest.TestCase):
    """Two 0.27.0 maps traced by the live app, each moved and written by nib's own code and saved
    through the route."""

    TABLES = {"arc_off": {"half_arc": None}, "cubic_2_5": {"half_cubic": 2.5}}

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.out: dict = {}
        cls.error: BaseException | None = None
        try:
            asyncio.run(cls._saves(Path(cls._temp.name)))
        except BaseException as error:  # noqa: BLE001 - re-raised by each test
            cls.error = error

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    @classmethod
    async def _saves(cls, root: Path) -> None:
        checks = unittest.TestCase()
        subject = build_subject(root / "subject")
        async with _IdentityApi(checks, root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            for name, trace in cls.TABLES.items():
                revision = await run_job(checks, api, "trace", project_id, palette_revision,
                                         trace=trace)
                parent = (await effective(api, revision, "traced-map")).decode("utf-8")
                work = root / name
                work.mkdir()
                written, report = nib_move(parent, work)
                saved = await api.post_map(revision, written)
                child = saved.json()["id"] if saved.status_code == 201 else None
                own = await api.own(child) if child else {}
                cls.out[name] = {"parent": parent, "written": written, "report": report,
                                 "status": saved.status_code, "body": saved.text,
                                 "stored": own["traced-map"]["content"].decode("utf-8") if own else None,
                                 "audit": json.loads(own["trace-audit"]["content"]) if own else None}

    def save(self, name: str) -> dict:
        if self.error is not None:
            raise self.error
        out = self.out[name]
        self.assertEqual(out["status"], 201, out["body"])
        return out

    def assert_setting_kept(self, name: str, expected: dict) -> None:
        out = self.save(name)
        parent = json.loads(out["parent"])
        self.assertEqual(t3api.normal_setting(parent["setting"]), expected,
                         "the input is a 0.27.0 map at its trace's table")
        self.assertIn("half", parent["setting"])
        self.assertEqual(out["report"]["violations"], 0, "nib's own check passes what nib wrote")
        self.assertNotEqual(out["written"], out["parent"], "nib moved a node")
        self.assertEqual(out["stored"], out["written"], "the new revision holds what nib wrote")
        self.assertEqual(json.loads(out["stored"])["setting"], parent["setting"],
                         "the new revision's header setting is the input's")
        self.assertEqual(header(out["stored"]), header(out["parent"]),
                         "the header, byte for byte")
        self.assertEqual(out["audit"]["backend"]["editor"]["name"], "nib")
        self.assertEqual(out["audit"]["backend"]["setting"], expected)

    def test_nib_saves_a_map_traced_with_the_arc_off_with_its_setting(self):
        half = {"line": 1.75, "arc": None, "ellipse": 1.75, "cubic": 1.75}
        self.assert_setting_kept("arc_off", {"half": half, "A1": 22.5, "A2": 45.0, "L": 4.0})

    def test_nib_saves_a_map_traced_with_the_cubic_at_2_5_with_its_setting(self):
        half = {"line": 1.75, "arc": 1.75, "ellipse": 1.75, "cubic": 2.5}
        self.assert_setting_kept("cubic_2_5", {"half": half, "A1": 22.5, "A2": 45.0, "L": 4.0})


if __name__ == "__main__":
    unittest.main()
