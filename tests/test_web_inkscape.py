from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.web.inkscape import validate_safe_svg


SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 20 20">
<rect width="20" height="20" fill="#224466"/><circle cx="10" cy="10" r="5" fill="#ee8844"/>
</svg>'''


class WebInkscapeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.shared = Path(self.temp.name) / "shared"
        self.app = create_app(WebConfig(Path(self.temp.name) / "data",
                                        inkscape_shared_dir=self.shared))

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()
        self.temp.cleanup()

    def traced_revision(self) -> tuple[dict, dict, dict]:
        store = self.app.state.store
        project = store.create_project("Inkscape Fixture")
        revision = store.create_revision(project["id"], parent_revision_id=None,
                                         kind="trace", message=None, settings={})
        artifact = store.add_artifact(revision["id"], kind="traced-overlap",
                                      filename="traced-overlap.svg", media_type="image/svg+xml",
                                      source=io.BytesIO(SVG))
        return project, revision, artifact

    async def test_download_shared_save_and_reimport_create_immutable_revision(self) -> None:
        project, original, svg = self.traced_revision()
        handoff = await self.client.get(f"/api/revisions/{original['id']}/inkscape-handoff")
        self.assertEqual(handoff.status_code, 200, handoff.text)
        self.assertIn("cannot launch desktop Inkscape", handoff.json()["guidance"])
        self.assertEqual(handoff.json()["exports"][0]["id"], svg["id"])

        saved = await self.client.post(
            f"/api/revisions/{original['id']}/inkscape-save", data={"artifact_id": svg["id"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((self.shared / saved.json()["filename"]).read_bytes(), SVG)

        edited = SVG.replace(b"#ee8844", b"#33cc77")
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{original['id']}/inkscape-reimport",
            files={"svg": ("edited.svg", edited, "image/svg+xml")})
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()
        self.assertEqual(revision["kind"], "inkscape-edit")
        self.assertEqual(revision["parent_revision_id"], original["id"])
        artifacts = (await self.client.get(
            f"/api/revisions/{revision['id']}/artifacts")).json()
        self.assertEqual({row["kind"] for row in artifacts},
                         {"inkscape-svg", "inkscape-preview", "inkscape-audit"})
        imported = next(row for row in artifacts if row["kind"] == "inkscape-svg")
        self.assertEqual(self.app.state.store.artifact_path(imported["id"]).read_bytes(), edited)
        audit_row = next(row for row in artifacts if row["kind"] == "inkscape-audit")
        audit = json.loads(self.app.state.store.artifact_path(audit_row["id"]).read_text())
        self.assertEqual((audit["preview_dpi"], audit["preview_width"], audit["preview_height"],
                          audit["preview_capped"]), (768.0, 160, 160, False))
        self.assertEqual(revision["settings"]["stale_concerns"],
                         ["trace", "validation", "export"])
        self.assertEqual(self.app.state.store.artifact_path(svg["id"]).read_bytes(), SVG)

        edited_handoff = await self.client.get(
            f"/api/revisions/{revision['id']}/inkscape-handoff")
        self.assertEqual(edited_handoff.status_code, 200, edited_handoff.text)
        self.assertEqual({row["kind"] for row in edited_handoff.json()["exports"]},
                         {"inkscape-svg", "inkscape-preview"})

    async def test_reimport_rejects_active_content(self) -> None:
        project, revision, _ = self.traced_revision()
        unsafe_documents = (
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://bad.test/x)</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://bad.test/x)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="url(https://bad.test/x)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>rect{fill:u\\72l(https://bad.test/x)}</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:u/**/rl(https://bad.test/x)"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/svg+xml,&lt;svg/&gt;"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><animate attributeName="x"/></svg>',
        )
        for unsafe in unsafe_documents:
            with self.subTest(svg=unsafe):
                response = await self.client.post(
                    f"/api/projects/{project['id']}/revisions/{revision['id']}/inkscape-reimport",
                    files={"svg": ("unsafe.svg", unsafe, "image/svg+xml")})
                self.assertEqual(response.status_code, 422, response.text)

    def test_svg_validation_rejects_processing_instructions_before_parsing(self) -> None:
        unsafe_documents = (
            b'<?xml-stylesheet href="https://bad.test/x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            b'<?XML-STYLEsheet   href="https://bad.test/x.css"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><?xml-stylesheet href="//bad.test/x"?></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><?target data?></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><? xml-stylesheet href="x"?></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><!-- <?xml-stylesheet href="x"?> --></svg>',
        )
        for unsafe in unsafe_documents:
            with self.subTest(svg=unsafe):
                with self.assertRaisesRegex(ValueError, "processing instructions"):
                    validate_safe_svg(unsafe)

    def test_svg_validation_allows_only_a_leading_xml_declaration(self) -> None:
        declared = (b'\xef\xbb\xbf<?xml version="1.0" encoding="UTF-8"?>\n'
                    b'<svg xmlns="http://www.w3.org/2000/svg"><rect fill="#123456"/></svg>')
        self.assertEqual(validate_safe_svg(declared), {"root": "svg", "nodes": 2})

        declaration_then_pi = declared.replace(
            b'\n<svg', b'\n<?xml-stylesheet href="https://bad.test/x.css"?>\n<svg')
        with self.assertRaisesRegex(ValueError, "processing instructions"):
            validate_safe_svg(declaration_then_pi)

    async def test_proxy_identity_required_for_hosted_mutations(self) -> None:
        temp = tempfile.TemporaryDirectory(dir=".tmp")
        app = create_app(WebConfig(Path(temp.name), require_proxy_identity=True))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            denied = await client.post("/api/projects", json={"name": "Denied"})
            allowed = await client.post("/api/projects", json={"name": "Allowed"},
                                        headers={"X-Gandalf-User": "user@example.test"})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(allowed.status_code, 201)
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
