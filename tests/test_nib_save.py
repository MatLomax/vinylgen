"""The pieces of a save from nib's editor that the frozen ``test_nib_embed`` does not reach:
the vendored package's pins, ``nib.map_refusal`` on its own (a palette grown by appended colours,
and the rule of the map), a later trace from an edited revision tracing afresh instead of
re-exporting the edit as t3's map, and a map whose palette grew flowing through every stage after
Nodes."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import t3api
from PIL import Image

from test_nib_embed import _IdentityApi, move_one_node, nib_text
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
            # A palette or names entry changed is named by its index.
            prefix = f"{key} is" if key == "H" else f"{key} entry 0 is"
            self.assertTrue(any(problem.startswith(prefix) for problem in refusal["problems"]),
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


# ----------------------------------------------------------------------------- a grown palette

# The colour nib's boundary tool appends for a "new colour" pick, and the face it relabels: face
# 15 (label 1, Golden Yellow, no border).
NEW_RGB = [12, 200, 90]
NEW_HEX = "#0CC85A"
NEW_FACE = 15
# A second new colour near Cream, whose generated name is Cream's: the trace names it "Cream 2".
CREAM_LIKE = [240, 230, 170]


def grown(text: str, rgb: list, face: int = NEW_FACE, name: str | None = None) -> dict:
    """``text``'s map with ``rgb`` appended to its palette (and ``name`` to its names) as nib's
    ``commitFace`` writes a new colour, and face ``face`` relabelled with the new label."""
    pm = json.loads(text)
    label = len(pm["palette"])
    pm["palette"].append(rgb)
    pm["names"].append(name)
    next(row for row in pm["faces"] if row["face"] == face)["label"] = label
    return pm


def nib_bytes(pm: dict) -> bytes:
    return json.dumps(pm, separators=(",", ":")).encode()


class GrownPaletteRefusalTest(unittest.TestCase):
    """The save route accepts a posted palette exactly when the parent's is a prefix of it, every
    appended entry is a colour in the palette's format and the label of a face of the posted map;
    any other palette change is refused naming the first differing entry."""

    PARENT = (FIXTURE / "map.json").read_bytes()

    def refusal(self, pm: dict) -> dict | None:
        return map_refusal(nib_bytes(pm), self.PARENT)

    def test_one_appended_colour_labelling_a_face_is_accepted(self):
        self.assertIsNone(self.refusal(grown(self.PARENT.decode(), NEW_RGB)))

    def test_two_appended_colours_each_labelling_a_face_are_accepted(self):
        pm = grown(nib_text(grown(self.PARENT.decode(), NEW_RGB)), CREAM_LIKE, face=13)
        self.assertIsNone(self.refusal(pm))

    def test_an_appended_named_colour_is_accepted(self):
        self.assertIsNone(self.refusal(grown(self.PARENT.decode(), NEW_RGB, name="Leaf")))

    def assert_names_entry(self, pm: dict, entry: int) -> None:
        refusal = self.refusal(pm)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["reason"], "mismatch", refusal)
        self.assertIn(f"palette entry {entry}", refusal["detail"], refusal)
        self.assertTrue(any(f"palette entry {entry}" in problem for problem in refusal["problems"]),
                        refusal)

    def test_a_changed_entry_is_refused_naming_it(self):
        pm = grown(self.PARENT.decode(), NEW_RGB)
        pm["palette"][2] = [244, 230, 174]
        self.assert_names_entry(pm, 2)

    def test_reordered_entries_are_refused_naming_the_first(self):
        pm = grown(self.PARENT.decode(), NEW_RGB)
        pm["palette"][1], pm["palette"][2] = pm["palette"][2], pm["palette"][1]
        pm["names"][1], pm["names"][2] = pm["names"][2], pm["names"][1]
        self.assert_names_entry(pm, 1)

    def test_a_removed_entry_is_refused_naming_it(self):
        pm = json.loads(self.PARENT)
        del pm["palette"][3]
        del pm["names"][3]
        for face in pm["faces"]:
            if face["label"] == 3:
                face["label"] = 2
        self.assert_names_entry(pm, 3)

    def test_an_appended_entry_that_is_no_colour_is_refused_naming_it(self):
        for bad in ([300, 0, 0], [-1, 0, 0], [1, 2], [1, 2, 3, 4], None, [1.5, 2, 3], [True, 0, 0],
                    "#0CC85A"):
            with self.subTest(bad=bad):
                self.assert_names_entry(grown(self.PARENT.decode(), bad), 4)

    def test_an_appended_colour_no_face_uses_is_refused_naming_it(self):
        pm = json.loads(self.PARENT)
        pm["palette"].append(NEW_RGB)
        pm["names"].append(None)
        self.assert_names_entry(pm, 4)

    def test_an_appended_colour_equal_to_an_earlier_entry_is_refused_naming_both(self):
        # Equal to the parent's Cream (entry 2), and an appended colour appended again.
        pm = grown(self.PARENT.decode(), [244, 230, 173])
        self.assert_names_entry(pm, 4)
        refusal = self.refusal(pm)
        self.assertIn("palette entry 4 [244, 230, 173] equals palette entry 2", refusal["detail"], refusal)
        twice = grown(nib_text(grown(self.PARENT.decode(), NEW_RGB)), NEW_RGB, face=13)
        refusal = self.refusal(twice)
        self.assertEqual(refusal["reason"], "mismatch", refusal)
        self.assertIn("palette entry 5 [12, 200, 90] equals palette entry 4", refusal["detail"], refusal)

    def test_a_changed_name_is_refused_naming_its_entry(self):
        pm = grown(self.PARENT.decode(), NEW_RGB)
        pm["names"][0] = "Ink"
        refusal = self.refusal(pm)
        self.assertEqual(refusal["reason"], "mismatch", refusal)
        self.assertIn("names entry 0", refusal["detail"], refusal)

    def test_an_appended_name_another_entry_holds_is_refused_naming_it(self):
        refusal = self.refusal(grown(self.PARENT.decode(), NEW_RGB, name="cream"))
        self.assertEqual(refusal["reason"], "mismatch", refusal)
        self.assertIn("names entry 4", refusal["detail"], refusal)

    def test_a_face_label_that_is_no_palette_entry_is_invalid(self):
        pm = json.loads(self.PARENT)
        next(row for row in pm["faces"] if row["face"] == NEW_FACE)["label"] = 4
        refusal = self.refusal(pm)
        self.assertEqual(refusal["reason"], "invalid", refusal)
        self.assertIn(f"face {NEW_FACE}", " ".join(refusal["problems"]), refusal)


class RuleOfTheMapTest(unittest.TestCase):
    """The save route refuses a map with an edge whose two sides carry one label, naming the edge;
    an edge with a null side is exempt (nib's ``checkLabelMap``)."""

    PARENT = (FIXTURE / "map.json").read_bytes()

    def test_an_edge_with_one_label_on_both_sides_is_refused_naming_it(self):
        pm = json.loads(self.PARENT)
        labels = {row["face"]: row["label"] for row in pm["faces"]}
        k, edge = next((k, edge) for k, edge in enumerate(pm["edges"])
                       if edge["left"] is not None and edge["right"] is not None
                       and labels[edge["left"]] != labels[edge["right"]])
        next(row for row in pm["faces"] if row["face"] == edge["right"])["label"] = labels[edge["left"]]
        refusal = map_refusal(nib_bytes(pm), self.PARENT)
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal["reason"], "invalid", refusal)
        self.assertIn("rule of the map", refusal["detail"], refusal)
        self.assertIn(f"edge {k} has label {labels[edge['left']]} on both sides", refusal["problems"][0],
                      refusal)

    def test_a_border_edge_is_exempt(self):
        # The fixture's border edges each have one null side, and label 0's faces lie along the
        # border: a rule reading a null side as any label would refuse it.
        from vinylgen.web.nib import rule_problems

        pm = t3api.read_planar_map(self.PARENT.decode())
        labels = {row["face"]: row["label"] for row in pm["faces"]}
        border = [edge for edge in pm["edges"] if edge["left"] is None or edge["right"] is None]
        self.assertTrue(any(labels.get(edge["left"], labels.get(edge["right"])) == 0
                            for edge in border))
        self.assertEqual(rule_problems(pm), [])
        self.assertIsNone(map_refusal(move_one_node(self.PARENT.decode()).encode(), self.PARENT))


async def run_job(api, kind: str, project_id: str, revision_id: str) -> dict:
    """Run pipeline job ``kind`` on ``revision_id`` to its end and return the job."""
    import time

    from test_t3_trace import TRACE_BOUND_S

    response = await api.client.post("/api/jobs", json={
        "kind": kind, "project_id": project_id, "revision_id": revision_id})
    api.test.assertEqual(response.status_code, 202, response.text)
    deadline = time.monotonic() + TRACE_BOUND_S
    while time.monotonic() < deadline:
        job = (await api.client.get(f"/api/jobs/{response.json()['id']}")).json()
        if job["status"] not in {"queued", "running"}:
            return job
        await asyncio.sleep(0.05)
    api.test.fail(f"the {kind} job did not finish")


class GrownPaletteDownstreamTest(unittest.IsolatedAsyncioTestCase):
    """A map whose palette grew in nib flows through every stage after Nodes as a traced one does:
    the save's re-export (edge SVG, audit, preview, inspection), validation, export, a second
    save, a later trace and the editor state. One web trace of the test array, one save of a new
    colour on face 15, and the stages after it, shared by the class's tests."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        root = Path(cls._temp.name)
        cls.error = None
        try:
            asyncio.run(cls._prepare(build_subject(root / "subject"), root / "data"))
        except Exception as error:  # noqa: BLE001 - re-raised by each test
            cls.error = error

    @classmethod
    async def _prepare(cls, subject: Path, data: Path) -> None:
        checker = unittest.TestCase()
        async with _IdentityApi(checker, data) as api:
            cls.project_id, palette_id = await api.palette_revision(subject)
            cls.trace_id = await api.trace(cls.project_id, palette_id)
            cls.parent_own = await api.own(cls.trace_id)
            parent_text = cls.parent_own["traced-map"]["content"].decode()
            cls.posted = nib_bytes(grown(parent_text, NEW_RGB))
            cls.saved = await api.post_map(cls.trace_id, cls.posted.decode())
            cls.child_id = cls.saved.json().get("id") if cls.saved.status_code == 201 else None
            if cls.child_id is None:
                return
            cls.child_own = await api.own(cls.child_id)
            cls.editor = await api.client.get(f"/api/revisions/{cls.child_id}/editor")
            cls.validate_job = await run_job(api, "validate", cls.project_id, cls.child_id)
            cls.validated_own = (await api.own(cls.validate_job["result_revision_id"])
                                 if cls.validate_job["status"] == "succeeded" else None)
            cls.export_job = (await run_job(api, "export", cls.project_id,
                                            cls.validate_job["result_revision_id"])
                              if cls.validated_own else None)
            cls.export_own = (await api.own(cls.export_job["result_revision_id"])
                              if cls.export_job and cls.export_job["status"] == "succeeded" else None)
            cls.second_posted = nib_bytes(grown(cls.posted.decode(), CREAM_LIKE, face=13))
            cls.second = await api.post_map(cls.child_id, cls.second_posted.decode())
            cls.second_own = (await api.own(cls.second.json()["id"])
                              if cls.second.status_code == 201 else None)
            cls.retrace_job = await run_job(api, "trace", cls.project_id, cls.child_id)
            cls.retrace_own = (await api.own(cls.retrace_job["result_revision_id"])
                               if cls.retrace_job["status"] == "succeeded" else None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def child(self) -> dict:
        if self.error is not None:
            raise self.error
        self.assertEqual(self.saved.status_code, 201, self.saved.text)
        return self.child_own

    def parent_palette(self) -> list[dict]:
        return json.loads(self.parent_own["palette"]["content"])["palette"]

    def test_the_save_stores_the_posted_map_with_the_parents_palette_plus_one(self):
        own = self.child()
        self.assertEqual(own["traced-map"]["content"], self.posted)
        pm = t3api.read_planar_map(own["traced-map"]["content"].decode())
        parent = t3api.read_planar_map(self.parent_own["traced-map"]["content"].decode())
        self.assertEqual(pm["palette"], parent["palette"] + [NEW_RGB])

    def test_the_edge_svg_holds_a_layer_of_the_new_colour_with_its_face(self):
        own = self.child()
        root = ET.fromstring(own["traced-edge"]["content"])
        layers = root.findall("{http://www.w3.org/2000/svg}g")
        parent_layers = ET.fromstring(self.parent_own["traced-edge"]["content"]).findall(
            "{http://www.w3.org/2000/svg}g")
        self.assertEqual(len(layers), len(parent_layers) + 1)
        self.assertEqual(layers[-1].attrib["fill"], NEW_HEX)
        paths = layers[-1].findall("{http://www.w3.org/2000/svg}path")
        self.assertEqual(len(paths), 1)
        ids = [layer.attrib["id"] for layer in layers]
        self.assertEqual(len(ids), len(set(ids)))

    def test_trace_json_records_the_new_colour_named_and_the_edge_validation_passes(self):
        audit = json.loads(self.child()["trace-audit"]["content"])
        row = audit["colours"]["included"][-1]
        self.assertEqual((row["index"], row["hex"], row["rgb"]), (4, NEW_HEX, NEW_RGB))
        names = [str(entry["name"]).casefold() for entry in audit["colours"]["included"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(audit["validation"]["edge"]["passed"], audit["validation"]["edge"])
        self.assertEqual(audit["validation"]["edge"]["read_back"]["differ"], 0)

    def test_the_preview_and_the_inspection_draw_the_new_colour(self):
        own = self.child()
        preview = np.asarray(Image.open(io.BytesIO(own["traced-preview"]["content"])).convert("RGB"))
        parent = np.asarray(Image.open(io.BytesIO(
            self.parent_own["traced-preview"]["content"])).convert("RGB"))
        new = np.all(preview == NEW_RGB, axis=-1)
        self.assertGreater(int(new.sum()), 0)
        self.assertEqual(int(np.all(parent == NEW_RGB, axis=-1).sum()), 0)
        self.assertIn(NEW_HEX, own["trace-inspection"]["content"].decode())

    def test_the_trace_keeps_the_raster_palette_json(self):
        own = self.child()
        self.assertEqual(json.loads(own["palette"]["content"])["palette"], self.parent_palette())
        self.assertEqual(self.editor.status_code, 200, self.editor.text)
        self.assertEqual(len(self.editor.json()["palette"]), len(self.parent_palette()))

    def test_validation_passes_with_the_new_layer(self):
        self.child()
        self.assertEqual(self.validate_job["status"], "succeeded", self.validate_job)
        validation = json.loads(self.validated_own["validation"]["content"])
        self.assertTrue(validation["edge"]["passed"], validation)
        self.assertEqual(validation["edge"]["layer_count"], len(self.parent_palette()) + 1)

    def test_the_export_packages_the_grown_map_and_the_new_layer(self):
        self.child()
        self.assertIsNotNone(self.export_job, self.validate_job)
        self.assertEqual(self.export_job["status"], "succeeded", self.export_job)
        with zipfile.ZipFile(io.BytesIO(self.export_own["export"]["content"])) as archive:
            self.assertEqual(archive.read("map.json"), self.posted)
            self.assertIn(f'fill="{NEW_HEX}"', archive.read("traced-edge.svg").decode())
            self.assertIn("traced.png", archive.namelist())

    def test_a_second_new_colour_saves_from_the_grown_revision_named_apart(self):
        self.child()
        self.assertEqual(self.second.status_code, 201, self.second.text)
        pm = t3api.read_planar_map(self.second_own["traced-map"]["content"].decode())
        self.assertEqual(pm["palette"][-2:], [NEW_RGB, CREAM_LIKE])
        audit = json.loads(self.second_own["trace-audit"]["content"])
        names = [row["name"] for row in audit["colours"]["included"]]
        self.assertEqual(names[-1], "Cream 2", names)
        self.assertTrue(audit["validation"]["edge"]["passed"], audit["validation"]["edge"])

    def test_a_later_trace_from_the_grown_revision_traces_the_labels_afresh(self):
        self.child()
        self.assertEqual(self.retrace_job["status"], "succeeded", self.retrace_job)
        self.assertEqual(self.retrace_own["traced-map"]["content"], (FIXTURE / "map.json").read_bytes())
