"""Card 7 of vinylgen-on-t3, the nib embed: a save from nib's editor makes a new trace revision.

The route ``POST /api/revisions/{revision_id}/map`` takes the planar-map file nib wrote (the body,
``application/json``, the exact text) for a shown trace revision that has a ``map.json``. Revisions
are immutable, so the server makes a child revision of kind ``trace`` (parent: the shown trace
revision; message "edited in nib") whose ``map.json`` is the posted document byte for byte, and
rebuilds ``traced-edge.svg``, the checks, the audit, ``traced.png`` and ``trace.json`` from it as the
re-export path does, with the revision's own settings. It refuses with HTTP 422 a document that
fails ``t3api.read_planar_map`` or ``planar_map_check``, one whose ``W``, ``H`` or palette differ
from the parent's, and one byte-equal to the parent's map (nothing edited). The contract is the
tracer card ``cards/vinylgen-on-t3/card.md`` section 5 ("The traced-node viewer is nib's editor")
and section 8 ("Later: the nib embed"), as decided in ``build-7a-brief.md``.

The 422 body is JSON: ``detail`` (a sentence the client shows), ``reason`` (``"invalid"`` for a
document the reader or the check refuses, ``"mismatch"`` for another size or palette,
``"unchanged"`` for the parent's own bytes), ``problems`` (a list of strings) and, for a document
``planar_map_check`` refuses, ``violations`` (its count). A revision with no ``map.json`` answers
404.

Every route test runs through the live app (the frozen ``test_t3_trace`` helpers' in-process ASGI
client, the identity header required as on the deployed service) on the test array traced by the
web pipeline's trace job.

The embed mounts ``@nib/core`` 0.4.0 and its chrome. ``VendoredNibCoreTest`` pins the package: the
tarball packed from nib at tag ``core-v0.4.0`` (commit ``f9e355e5ee302d24d3d4d14c0373d2d3df4e7d93``)
as ``web/vendor/nib-core-0.4.0.tgz`` with its sha256, no other tarball vendored, ``web/vendor/README.md``
naming the tag, the commit and the hash, ``web/package.json`` and its lockfile depending on it by
``file:`` path, and the package exporting ``mountChrome`` and ``./chrome.css``. The editor version a
save records is the package's, ``0.4.0``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image

import t3api

from test_t3_trace import FIXTURE, SCRATCH, _Api, build_subject, expected_layers, svg_layers
from vinylgen.svg_writer import add_face_layer, svg_document, write_svg

IDENTITY = {"X-Gandalf-User": "nib-embed@example.test"}

# The vendored @nib/core: packed by `pnpm pack` in nib's packages/core at tag core-v0.4.0; the pack
# is reproducible.
ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "web" / "vendor"
NIB_CORE_VERSION = "0.4.0"
NIB_CORE_TARBALL = f"nib-core-{NIB_CORE_VERSION}.tgz"
NIB_CORE_SHA256 = "511a16be3a1b8e9a3ce56ab2e791c1439880131190911531818d53c6f0cf79c7"
NIB_CORE_TAG = "core-v0.4.0"
NIB_CORE_COMMIT = "f9e355e5ee302d24d3d4d14c0373d2d3df4e7d93"

# The edited document: one node moved by (0.25, 0.25) with nib's node tool. Produced by nib's
# core at tag core-v0.1.0 (21e34c1), run under Node from packages/core/dist:
# LabelMapSession.fromText(the fixture's map.json), the view node of face 15 at edge 10's piece
# boundary j = 1 (the point (164.5, 181.0) between two L pieces), moveNodes([that ref], 0.25,
# 0.25), then session.text(). checkLabelMap reported no violation. The vendored tarball's
# dist/label-map-session.js writes the same bytes from the same steps. Its sha256:
NIB_MOVE_SHA256 = "b1b1af569263cdbf91ab83821bfaf26eac66e479b26ae28284b6c1a8dc749a36"
MOVED_EDGE, MOVED_J, MOVE = 10, 1, (0.25, 0.25)

# The artefacts a trace revision made from a map holds, by kind and file name.
REBUILT = {"traced-map": "map.json", "traced-edge": "traced-edge.svg",
           "traced-preview": "traced.png", "trace-audit": "trace.json"}


def nib_text(pm: dict) -> str:
    """A planar-map document as nib's writer serialises it: Python's
    ``json.dumps(separators=(",", ":"))`` with ``repr`` floats, keys in the file's order."""
    return json.dumps(pm, separators=(",", ":"))


def move_one_node(text: str) -> str:
    """The edit nib's ``moveNodes`` makes at edge 10's boundary 1: the end of piece 0 and the start
    of piece 1 (both ``L``) move by ``MOVE``, in ``seg`` and in ``p1``/``p0``; nothing else changes."""
    pm = json.loads(text)
    pieces = pm["edges"][MOVED_EDGE]["pieces"]
    x, y = pieces[MOVED_J - 1]["p1"]
    moved = [x + MOVE[0], y + MOVE[1]]
    assert pieces[MOVED_J - 1]["kind"] == "L" and pieces[MOVED_J]["kind"] == "L"
    pieces[MOVED_J - 1]["seg"][3:5] = moved
    pieces[MOVED_J - 1]["p1"] = list(moved)
    pieces[MOVED_J]["seg"][1:3] = moved
    pieces[MOVED_J]["p0"] = list(moved)
    return nib_text(pm)


def reversed_reference(text: str) -> str:
    """Face 0's first ring reference reversed: the ring no longer meets end to start and the edge's
    sides no longer match its references (``planar_map_check``: 2 violations)."""
    pm = json.loads(text)
    ring = pm["faces"][0]["rings"][0]
    ring[0] = ~ring[0]
    return nib_text(pm)


def other_width(text: str) -> str:
    pm = json.loads(text)
    pm["W"] = pm["W"] + 1
    return nib_text(pm)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _IdentityApi(_Api):
    """``_Api`` on an app that requires the proxy identity on every mutation, as the deployed
    service does, with a client that sends it."""

    def __init__(self, test: unittest.TestCase, data_dir: Path):
        from vinylgen.web.app import create_app
        from vinylgen.web.config import WebConfig

        self.test = test
        self.app = create_app(WebConfig(data_dir, require_proxy_identity=True))

    async def __aenter__(self):
        import httpx

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test", headers=IDENTITY)
        return self

    async def post_map(self, revision_id: str, text: str, headers: dict | None = None):
        return await self.client.post(
            f"/api/revisions/{revision_id}/map", content=text.encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})})

    async def own(self, revision_id: str) -> dict[str, dict]:
        """The revision's own artefacts by kind, each with its content's sha256."""
        rows = (await self.client.get(f"/api/revisions/{revision_id}/artifacts")).json()
        out = {}
        for row in rows:
            content = (await self.client.get(row["download_url"])).content
            out[row["kind"]] = {**row, "sha256": sha256(content), "content": content}
        return out

    async def revision_ids(self, project_id: str) -> list[str]:
        response = await self.client.get(f"/api/projects/{project_id}/revisions")
        return sorted(row["id"] for row in response.json())


class NibSaveRouteTest(unittest.IsolatedAsyncioTestCase):
    """One web trace of the test array, and one save of nib's one-node move from it, shared by the
    class's tests."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        root = Path(cls._temp.name)
        cls.data = root / "data"
        cls.error = None
        try:
            asyncio.run(cls._prepare(build_subject(root / "subject")))
        except Exception as error:  # noqa: BLE001 - re-raised by each test
            cls.error = error

    @classmethod
    async def _prepare(cls, subject: Path) -> None:
        checker = unittest.TestCase()
        async with _IdentityApi(checker, cls.data) as api:
            cls.project_id, cls.palette_id = await api.palette_revision(subject)
            cls.trace_id = await api.trace(cls.project_id, cls.palette_id)
            cls.parent = (await api.client.get(f"/api/projects/{cls.project_id}")).json()
            cls.parent_revision = next(row for row in cls.parent["revisions"]
                                       if row["id"] == cls.trace_id)
            cls.parent_own = await api.own(cls.trace_id)
            cls.parent_map = cls.parent_own["traced-map"]["content"].decode("utf-8")
            cls.parent_audit = json.loads(cls.parent_own["trace-audit"]["content"])
            # A trace revision from before t3: an edge SVG and no map.json.
            store = api.app.state.store
            old = store.create_revision(
                cls.project_id, parent_revision_id=cls.palette_id, kind="trace",
                message="Deterministic trace pipeline",
                settings={"pipeline_stage": "trace", "palette": {}, "trace": {},
                          "stale_concerns": ["validation", "export"]})
            store.add_artifact(old["id"], kind="traced-edge", filename="traced-edge.svg",
                               media_type="image/svg+xml",
                               source=io.BytesIO(cls.parent_own["traced-edge"]["content"]))
            cls.old_trace_id = old["id"]
            # The save of nib's one-node move.
            cls.moved = move_one_node(cls.parent_map)
            cls.saved = await api.post_map(cls.trace_id, cls.moved)
            cls.child = cls.saved.json() if cls.saved.status_code == 201 else None
            cls.child_own = await api.own(cls.child["id"]) if cls.child else None

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def prepared(self) -> None:
        if self.error is not None:
            raise self.error

    def edited(self) -> tuple[dict, dict]:
        """The child revision the save made and its own artefacts."""
        self.prepared()
        self.assertEqual(self.saved.status_code, 201, self.saved.text)
        return self.child, self.child_own

    def child_audit(self) -> dict:
        _, own = self.edited()
        return json.loads(own["trace-audit"]["content"])

    async def refused(self, text: str) -> dict:
        """Post ``text`` to the parent; it must answer 422 and make no revision."""
        self.prepared()
        async with _IdentityApi(self, self.data) as api:
            before = await api.revision_ids(self.project_id)
            response = await api.post_map(self.trace_id, text)
            after = await api.revision_ids(self.project_id)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(after, before, "a refused document makes no revision")
        body = response.json()
        self.assertIsInstance(body.get("detail"), str, body)
        self.assertTrue(body["detail"].strip(), body)
        self.assertIsInstance(body.get("problems"), list, body)
        self.assertTrue(all(isinstance(problem, str) for problem in body["problems"]), body)
        return body

    # ------------------------------------------------------------------------- the new revision

    def test_a_save_makes_a_child_trace_revision_edited_in_nib_with_the_rebuilt_artefacts(self):
        child, own = self.edited()
        self.assertEqual(child["kind"], "trace")
        self.assertEqual(child["parent_revision_id"], self.trace_id)
        self.assertEqual(child["project_id"], self.project_id)
        self.assertEqual(child["message"], "edited in nib")
        self.assertNotEqual(child["id"], self.trace_id)
        # The revision's own settings: the shown trace revision's, a trace's stale concerns.
        settings = child["settings"]
        self.assertEqual(settings["pipeline_stage"], "trace")
        self.assertEqual(settings["trace"], self.parent_revision["settings"]["trace"])
        self.assertEqual(settings["palette"], self.parent_revision["settings"]["palette"])
        self.assertEqual(settings["stale_concerns"], ["validation", "export"])
        for kind, filename in REBUILT.items():
            self.assertIn(kind, own, f"the child holds its own {kind}")
            self.assertEqual(own[kind]["filename"], filename)
        png = Image.open(io.BytesIO(own["traced-preview"]["content"]))
        self.assertEqual(png.format, "PNG")
        self.assertGreater(png.size[0] * png.size[1], 0)

    def test_the_stored_map_json_is_the_posted_document_byte_for_byte(self):
        _, own = self.edited()
        self.assertEqual(own["traced-map"]["content"], self.moved.encode("utf-8"))
        self.assertEqual(own["traced-map"]["media_type"], "application/json")

    def test_trace_json_records_the_editor_the_map_and_the_parents_t3_summary(self):
        audit = self.child_audit()
        self.assertTrue(str(audit["sources"]["map"]).endswith("map.json"), audit["sources"])
        self.assertEqual(audit["backend"]["editor"], {"name": "nib", "version": NIB_CORE_VERSION})
        self.assertEqual(audit["backend"]["tracer"], "t3api")
        self.assertEqual(audit["backend"]["build"], t3api.BUILD_ID)
        self.assertEqual(audit["determinism"]["map_sha256"], sha256(self.moved.encode("utf-8")))
        self.assertEqual(audit["t3"]["summary"], self.parent_audit["t3"]["summary"])
        self.assertIs(audit["t3"]["edited"], True)
        self.assertEqual(audit["settings"], self.parent_audit["settings"])
        edge = audit["validation"]["edge"]
        # The posted map's paint against the stored map's paint: the same bytes.
        self.assertEqual(edge["read_back"]["differ"], 0)
        self.assertEqual(edge["planar_map_check"]["violations"], 0)
        # The semantic audit runs against the cleaned labels as for a trace.
        self.assertIn("semantic", audit["validation"])
        self.assertEqual(set(audit["validation"]["semantic"]),
                         set(self.parent_audit["validation"]["semantic"]))

    def test_the_rebuilt_edge_svg_is_t3s_faces_of_the_posted_map_for_the_included_colours(self):
        _, own = self.edited()
        pm = t3api.read_planar_map(self.moved)
        self.assertIn("palette", self.parent_own, "the parent holds palette.json")
        full = json.loads(self.parent_own["palette"]["content"])["palette"]
        included = [full.index(row) for row in self.parent_audit["colours"]["included"]]
        # t3's own writer of the posted map: each face's d and nodetypes.
        faces_svg = ET.fromstring(t3api.faces_svg_from_planar_map(
            pm, [dict(row, index=i) for i, row in enumerate(full)]))
        paths = [{"face": int(node.attrib["data-face"]), "label": int(node.attrib["data-label"]),
                  "d": node.attrib["d"],
                  "nodetypes": node.attrib["{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}"
                                           "nodetypes"]}
                 for node in faces_svg.iter("{http://www.w3.org/2000/svg}path")]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            edge = Path(name) / "traced-edge.svg"
            edge.write_bytes(own["traced-edge"]["content"])
            self.assertEqual(svg_layers(edge), expected_layers(pm, paths, full, included))
            # And the re-export path's writer, from the posted map alone, byte for byte.
            root = svg_document(pm["W"], pm["H"], None)
            for index in included:
                add_face_layer(root, full[index], pm)
            exported = Path(name) / "exported.svg"
            write_svg(exported, root)
            self.assertEqual(own["traced-edge"]["content"], exported.read_bytes())
        self.assertNotEqual(own["traced-edge"]["content"], self.parent_own["traced-edge"]["content"],
                            "the moved node is drawn")

    def test_a_one_node_move_written_by_nib_is_accepted_with_the_check_green(self):
        self.prepared()
        self.assertEqual(self.parent_map.encode("utf-8"), (FIXTURE / "map.json").read_bytes())
        self.assertEqual(sha256(self.moved.encode("utf-8")), NIB_MOVE_SHA256,
                         "the posted document is the one nib's core wrote")
        _, own = self.edited()
        audit = json.loads(own["trace-audit"]["content"])
        self.assertEqual(audit["validation"]["edge"]["planar_map_check"]["violations"], 0)
        self.assertTrue(audit["validation"]["edge"]["passed"], audit["validation"]["edge"])
        stored = t3api.read_planar_map(own["traced-map"]["content"].decode("utf-8"))
        self.assertEqual(t3api.planar_map_check(stored)["violations"], 0)
        # The edit is real: the stored map paints other labels than the parent's somewhere.
        parent = t3api.read_planar_map(self.parent_map)
        self.assertGreater(int(np.count_nonzero(
            np.asarray(t3api.paint_planar_map(stored)) != np.asarray(t3api.paint_planar_map(parent)))), 0)

    # ------------------------------------------------------------------------------- refusals

    async def test_an_unchanged_document_is_refused_422_unchanged(self):
        self.prepared()
        body = await self.refused(self.parent_map)
        self.assertEqual(body["reason"], "unchanged", body)

    async def test_an_invalid_document_is_refused_422_with_its_violation_count(self):
        self.prepared()
        bad = reversed_reference(self.parent_map)
        check = t3api.planar_map_check(t3api.read_planar_map(bad))
        self.assertEqual(check["violations"], 2, check)
        body = await self.refused(bad)
        self.assertEqual(body["reason"], "invalid", body)
        self.assertEqual(body["violations"], 2, body)
        self.assertEqual(body["problems"], check["problems"], body)
        # A document t3api.read_planar_map refuses.
        unreadable = await self.refused('{"format":"t3-planar-map","version":99}')
        self.assertEqual(unreadable["reason"], "invalid", unreadable)
        self.assertTrue(unreadable["problems"], unreadable)

    async def test_a_document_of_another_width_is_refused_422_mismatch(self):
        self.prepared()
        wide = other_width(self.parent_map)
        self.assertEqual(t3api.planar_map_check(t3api.read_planar_map(wide))["violations"], 0,
                         "the check alone passes it: only the comparison with the parent refuses")
        body = await self.refused(wide)
        self.assertEqual(body["reason"], "mismatch", body)
        self.assertTrue(any("W" in problem for problem in body["problems"]), body)

    async def test_the_parents_artefacts_and_record_are_untouched(self):
        self.edited()
        for text in (self.parent_map, reversed_reference(self.parent_map),
                     other_width(self.parent_map)):
            await self.refused(text)
        async with _IdentityApi(self, self.data) as api:
            now = await api.own(self.trace_id)
            project = (await api.client.get(f"/api/projects/{self.project_id}")).json()
        self.assertEqual({kind: (row["id"], row["filename"], row["sha256"]) for kind, row in now.items()},
                         {kind: (row["id"], row["filename"], row["sha256"])
                          for kind, row in self.parent_own.items()})
        record = next(row for row in project["revisions"] if row["id"] == self.trace_id)
        self.assertEqual(record, self.parent_revision)

    # --------------------------------------------------------------------- no map, no identity

    async def test_a_revision_with_no_map_json_answers_404(self):
        self.prepared()
        async with _IdentityApi(self, self.data) as api:
            before = await api.revision_ids(self.project_id)
            for revision_id in (self.palette_id, self.old_trace_id):
                response = await api.post_map(revision_id, self.moved)
                self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(await api.revision_ids(self.project_id), before)

    async def test_the_route_needs_the_identity_header(self):
        # Holds on b5758a3 for every mutation (the middleware); pinned so the route keeps it.
        self.prepared()
        async with _IdentityApi(self, self.data) as api:
            import httpx

            before = await api.revision_ids(self.project_id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),
                                         base_url="http://test") as anonymous:
                response = await anonymous.post(
                    f"/api/revisions/{self.trace_id}/map", content=self.moved.encode("utf-8"),
                    headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(await api.revision_ids(self.project_id), before)


class VendoredNibCoreTest(unittest.TestCase):
    """``@nib/core`` 0.4.0 is the vendored tarball the web client installs, and it carries the
    chrome vinylgen mounts."""

    def tarball(self) -> bytes:
        path = VENDOR / NIB_CORE_TARBALL
        self.assertTrue(path.is_file(), f"{path} is vendored")
        return path.read_bytes()

    def member(self, name: str) -> bytes:
        with tarfile.open(VENDOR / NIB_CORE_TARBALL) as archive:
            names = archive.getnames()
            self.assertIn(name, names)
            return archive.extractfile(name).read()

    def test_the_tarball_is_the_pack_of_core_v0_4_0_and_no_other_tarball_is_vendored(self):
        self.assertEqual(sha256(self.tarball()), NIB_CORE_SHA256)
        self.assertEqual(sorted(path.name for path in VENDOR.glob("*.tgz")), [NIB_CORE_TARBALL])
        self.assertFalse((VENDOR / "nib-core-0.1.0.tgz").exists())

    def test_the_readme_names_the_tarball_its_tag_commit_and_hash(self):
        readme = (VENDOR / "README.md").read_text()
        for text in (f"## {NIB_CORE_TARBALL}", f"`{NIB_CORE_SHA256}`", NIB_CORE_TAG, NIB_CORE_COMMIT):
            self.assertTrue(text in readme, f"README.md names {text}")
        for text in ("nib-core-0.1.0.tgz", "core-v0.1.0"):
            self.assertFalse(text in readme, f"README.md no longer names {text}")

    def test_the_web_client_depends_on_it_by_file_path_and_the_lockfile_holds_its_integrity(self):
        package = json.loads((ROOT / "web" / "package.json").read_text())
        self.assertEqual(package["dependencies"]["@nib/core"], f"file:vendor/{NIB_CORE_TARBALL}")
        lock = json.loads((ROOT / "web" / "package-lock.json").read_text())
        self.assertEqual(lock["packages"][""]["dependencies"]["@nib/core"], f"file:vendor/{NIB_CORE_TARBALL}")
        entry = lock["packages"]["node_modules/@nib/core"]
        self.assertEqual(entry["version"], NIB_CORE_VERSION)
        self.assertEqual(entry["resolved"], f"file:vendor/{NIB_CORE_TARBALL}")
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(self.tarball()).digest()).decode()
        self.assertEqual(entry["integrity"], integrity)

    def test_the_package_exports_mount_chrome_and_its_stylesheet(self):
        manifest = json.loads(self.member("package/package.json"))
        self.assertEqual((manifest["name"], manifest["version"]), ("@nib/core", NIB_CORE_VERSION))
        exports = manifest["exports"]
        # The stylesheet subpath, and the file it names in the tarball.
        self.assertIn("./chrome.css", exports)
        stylesheet = exports["./chrome.css"]
        self.assertIsInstance(stylesheet, str)
        self.assertIn(".nib-chrome", self.member("package/" + stylesheet.removeprefix("./")).decode("utf-8"))
        # mountChrome beside mountEditor, from the entry "." names, code and declarations.
        entry = exports["."]
        for key in ("default", "types"):
            text = self.member("package/" + entry[key].removeprefix("./")).decode("utf-8")
            exported = set()
            for names in re.findall(r"^export\s*\{([^}]*)\}", text, re.M):
                exported.update(name.strip().split(" as ")[-1] for name in names.split(","))
            self.assertTrue({"mountChrome", "mountEditor"} <= exported, (key, sorted(exported)))


if __name__ == "__main__":
    unittest.main()
