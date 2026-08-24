from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig


class WebPipelineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.app = create_app(WebConfig(Path(self.temp.name)))

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()
        self.temp.cleanup()

    def source_png(self) -> bytes:
        pixels = np.full((32, 40, 3), (15, 35, 60), dtype=np.uint8)
        pixels[5:27, 8:32] = (225, 95, 45)
        buffer = io.BytesIO()
        Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
        return buffer.getvalue()

    async def wait_for_job(self, job_id: str) -> dict:
        for _ in range(600):
            job = (await self.client.get(f"/api/jobs/{job_id}")).json()
            if job["status"] not in {"queued", "running"}:
                return job
            await asyncio.sleep(0.05)
        self.fail("pipeline job did not finish")

    async def run_job(self, kind: str, project_id: str, revision_id: str, **settings) -> dict:
        response = await self.client.post("/api/jobs", json={
            "kind": kind, "project_id": project_id, "revision_id": revision_id, **settings})
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "succeeded", job)
        self.assertIsNotNone(job["result_revision_id"])
        return job

    async def run_failed_job(self, kind: str, project_id: str, revision_id: str) -> dict:
        response = await self.client.post("/api/jobs", json={
            "kind": kind, "project_id": project_id, "revision_id": revision_id})
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "failed", job)
        return job

    async def test_import_palette_trace_validate_and_export(self) -> None:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Pipeline Fixture"},
            files={"source": ("fixture.png", self.source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        source_revision = project["revisions"][0]["id"]

        palette_job = await self.run_job(
            "palette", project["id"], source_revision,
            palette={"initial_colours": 2, "min_colour_share": 0})
        palette_revision = palette_job["result_revision_id"]
        palette_artifacts = (await self.client.get(
            f"/api/revisions/{palette_revision}/artifacts")).json()
        self.assertEqual(
            {row["kind"] for row in palette_artifacts},
            {"source", "flattened", "segmentation", "palette", "report"})

        trace_job = await self.run_job("trace", project["id"], palette_revision)
        trace_revision = trace_job["result_revision_id"]
        trace_artifacts = (await self.client.get(
            f"/api/revisions/{trace_revision}/artifacts")).json()
        trace_kinds = {row["kind"] for row in trace_artifacts}
        self.assertTrue({"traced-edge", "traced-overlap", "traced-preview", "trace-audit"}
                        .issubset(trace_kinds))
        preview = next(row for row in trace_artifacts if row["kind"] == "traced-preview")
        preview_content = await self.client.get(preview["download_url"])
        self.assertTrue(preview_content.content.startswith(b"\x89PNG"))

        validation_job = await self.run_job("validate", project["id"], trace_revision)
        export_job = await self.run_job(
            "export", project["id"], validation_job["result_revision_id"])
        export_artifacts = (await self.client.get(
            f"/api/revisions/{export_job['result_revision_id']}/artifacts")).json()
        archive_response = await self.client.get(export_artifacts[0]["download_url"])
        with zipfile.ZipFile(io.BytesIO(archive_response.content)) as archive:
            self.assertIn("traced-edge.svg", archive.namelist())
            self.assertIn("traced.png", archive.namelist())

        revisions = (await self.client.get(
            f"/api/projects/{project['id']}/revisions")).json()
        settings_by_kind = {row["kind"]: row["settings"] for row in revisions}
        self.assertEqual(settings_by_kind["palette"]["stale_concerns"],
                         ["trace", "validation", "export"])
        self.assertEqual(settings_by_kind["trace"]["stale_concerns"], ["export"])
        self.assertEqual(settings_by_kind["export"]["stale_concerns"], [])
        self.assertIn("initial_colours", settings_by_kind["trace"]["palette"])
        self.assertIn("curve_fit_tolerance", settings_by_kind["trace"]["trace"])

    async def test_pipeline_settings_are_typed_and_bounded(self) -> None:
        response = await self.client.post("/api/jobs", json={
            "kind": "palette", "project_id": "x", "revision_id": "y",
            "palette": {"min_region_pixels": -1}})
        self.assertEqual(response.status_code, 422)
        response = await self.client.post("/api/jobs", json={
            "kind": "palette", "project_id": "x", "revision_id": "y",
            "palette": {"final_colours": 1}})
        self.assertEqual(response.status_code, 422)
        response = await self.client.post("/api/jobs", json={
            "kind": "palette", "project_id": "x", "revision_id": "y",
            "palette": {"initial_colours": 4, "final_colours": 5}})
        self.assertEqual(response.status_code, 422)
        for field, value in (
            ("protected_component_points", [[-1, 0]]),
            ("preserved_colour_points", [[0, -1]]),
            ("distinct_colour_point_pairs", [[[-1, 0], [0, 0]]]),
        ):
            response = await self.client.post("/api/jobs", json={
                "kind": "palette", "project_id": "x", "revision_id": "y",
                "palette": {field: value}})
            self.assertEqual(response.status_code, 422, field)

    async def test_stale_inherited_trace_cannot_be_validated_or_exported(self) -> None:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Stale Fixture"},
            files={"source": ("fixture.png", self.source_png(), "image/png")})
        project = imported.json()
        source_revision = project["revisions"][0]["id"]
        palette = await self.run_job(
            "palette", project["id"], source_revision,
            palette={"initial_colours": 2, "min_colour_share": 0})
        trace = await self.run_job("trace", project["id"], palette["result_revision_id"])
        stale = self.app.state.store.create_revision(
            project["id"], parent_revision_id=trace["result_revision_id"], kind="label-edit",
            message="Adversarial stale branch",
            settings={"stale_concerns": ["trace", "validation", "export"]})

        validation = await self.run_failed_job("validate", project["id"], stale["id"])
        export = await self.run_failed_job("export", project["id"], stale["id"])
        self.assertIn("stale prerequisite concerns: trace", validation["error"])
        self.assertIn("stale prerequisite concerns: trace, validation", export["error"])

    async def test_trace_preserves_palette_settings_across_intermediate_revision(self) -> None:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Provenance Fixture"},
            files={"source": ("fixture.png", self.source_png(), "image/png")})
        project = imported.json()
        source_revision = project["revisions"][0]["id"]
        palette = await self.run_job(
            "palette", project["id"], source_revision,
            palette={"initial_colours": 2, "min_colour_share": 0})
        intermediate = self.app.state.store.create_revision(
            project["id"], parent_revision_id=palette["result_revision_id"], kind="label-edit",
            message="Intermediate deterministic edit",
            settings={"stale_concerns": ["trace", "validation", "export"]})
        traced = await self.run_job("trace", project["id"], intermediate["id"])
        settings = self.app.state.store.get_revision(traced["result_revision_id"])["settings"]
        self.assertEqual(settings["palette"]["initial_colours"], 2)
        self.assertEqual(settings["palette"]["min_colour_share"], 0)


if __name__ == "__main__":
    unittest.main()
