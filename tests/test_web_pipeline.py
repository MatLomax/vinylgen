from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from vinylgen.contracts import PaletteSettings, TraceSettings
from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.web.pipeline import run_worker


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

    async def test_dither_radius_survives_the_api_into_the_flatten_revision(self) -> None:
        # A regression guard: the palette request model must carry dither_radius,
        # otherwise pydantic drops the client's value and the flatten silently
        # runs — and records — the default instead of what the user chose.
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Dither Fixture"},
            files={"source": ("fixture.png", self.source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        palette_job = await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 2, "min_colour_share": 0, "dither_radius": 2})
        revisions = (await self.client.get(
            f"/api/projects/{project['id']}/revisions")).json()
        recorded = next(row for row in revisions
                        if row["id"] == palette_job["result_revision_id"])
        self.assertEqual(recorded["settings"]["palette"]["dither_radius"], 2)

    async def test_flatten_when_source_filename_equals_slug(self) -> None:
        # A file whose stem slugifies to the project slug (the default when the
        # name is the filename) made the palette stage copy the source onto
        # itself and raise SameFileError; flattening must still succeed.
        imported = await self.client.post(
            "/api/projects/import", data={"name": "1000012355"},
            files={"source": ("1000012355.png", self.source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        self.assertEqual(project["slug"], "1000012355")
        palette_job = await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 2, "min_colour_share": 0})
        artifacts = (await self.client.get(
            f"/api/revisions/{palette_job['result_revision_id']}/artifacts")).json()
        self.assertIn("flattened", {row["kind"] for row in artifacts})

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
        response = await self.client.post("/api/jobs", json={
            "kind": "palette", "project_id": "x", "revision_id": "y",
            "palette": {"dither_radius": 6}})
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


class PipelineWorkerTest(unittest.TestCase):
    """The compute worker runs in a killable subprocess, so cancellation stops the work
    immediately rather than waiting for the next progress checkpoint."""

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(dir=".tmp"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.work, ignore_errors=True))
        pixels = np.full((48, 60, 3), (15, 35, 60), dtype=np.uint8)
        pixels[6:42, 10:50] = (225, 95, 45)
        self.source = self.work / "subject.png"
        Image.fromarray(pixels, "RGB").save(self.source)
        self.spec = {"op": "palette", "source": str(self.source),
                     "outputs": str(self.work / "outputs"),
                     "settings": asdict(PaletteSettings(initial_colours=4))}
        self.flattened = self.work / "outputs" / "subject" / "flattened.png"

    def test_worker_relays_progress_and_writes_output(self) -> None:
        seen: list[tuple[float, str]] = []
        run_worker(self.spec, on_progress=lambda value, message: seen.append((value, message)),
                   cancelled=lambda: False)
        self.assertTrue(self.flattened.exists())
        self.assertTrue(seen, "expected relayed progress events")
        self.assertEqual([value for value, _ in seen], sorted(value for value, _ in seen))
        self.assertEqual(seen[-1][0], 1.0)

    def test_trace_worker_relays_progress(self) -> None:
        # Flatten first so the subject carries the artifacts trace consumes, then
        # trace through the same killable subprocess and confirm it streams progress
        # rather than sitting silent until completion.
        run_worker(self.spec, on_progress=lambda value, message: None, cancelled=lambda: False)
        subject = self.work / "outputs" / "subject"
        seen: list[tuple[float, str]] = []
        trace_spec = {"op": "trace", "subject": str(subject),
                      "settings": asdict(TraceSettings()), "colour_names": {}}
        run_worker(trace_spec, on_progress=lambda value, message: seen.append((value, message)),
                   cancelled=lambda: False)
        self.assertTrue((subject / "traced-overlap.svg").exists())
        self.assertTrue(seen, "expected relayed trace progress events")
        self.assertEqual([value for value, _ in seen], sorted(value for value, _ in seen))
        self.assertEqual(seen[-1][0], 1.0)

    def test_cancellation_terminates_the_worker_before_it_finishes(self) -> None:
        # A cancel that is already set must raise and leave no committed-quality output,
        # proving the worker is killed rather than allowed to run to its next checkpoint.
        with self.assertRaises(asyncio.CancelledError):
            run_worker(self.spec, on_progress=lambda value, message: None,
                       cancelled=lambda: True)
        self.assertFalse(self.flattened.exists())


if __name__ == "__main__":
    unittest.main()
