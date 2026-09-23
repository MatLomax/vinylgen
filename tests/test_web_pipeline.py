from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from vinylgen.contracts import CleanupSettings, PaletteSettings, SegmentationBundle, TraceSettings
from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.web.pipeline import active_cleanup_settings, run_worker


class PipelineApiHarness(unittest.IsolatedAsyncioTestCase):
    """A live app over a scratch store, with helpers that run jobs through the API."""

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


class WebPipelineTest(PipelineApiHarness):
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
                         ["cleanup", "trace", "validation", "export"])
        self.assertEqual(settings_by_kind["trace"]["stale_concerns"], ["export"])
        self.assertEqual(settings_by_kind["export"]["stale_concerns"], [])
        self.assertIn("initial_colours", settings_by_kind["trace"]["palette"])
        self.assertIn("curve_fit_tolerance", settings_by_kind["trace"]["trace"])

    def test_transparent_audit_overlays_are_trace_outputs(self) -> None:
        # A trace commits every TRACE_FILES entry it wrote; the trace concern's stale
        # kinds hide them once an upstream change outdates the run.
        from vinylgen.web.pipeline import MEDIA_TYPES, TRACE_FILES
        from vinylgen.web.store import STALE_ARTIFACT_KINDS
        overlays = {"trace-fallbacks-overlay": "trace-fallbacks-overlay.png",
                    "trace-missed-details-overlay": "trace-missed-details-overlay.png"}
        for kind, filename in overlays.items():
            self.assertEqual(TRACE_FILES[kind], filename)
            self.assertIn(kind, STALE_ARTIFACT_KINDS["trace"])
            self.assertEqual(MEDIA_TYPES[Path(filename).suffix], "image/png")

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


class WebCleanupStageTest(PipelineApiHarness):
    """The cleanup stage between Edit and Trace, driven through the HTTP API."""

    def speck_source_png(self) -> bytes:
        # A 40 x 32 image: navy background, an orange block, and a two-pixel orange
        # speck the cleanup's minimum area removes.
        pixels = np.full((32, 40, 3), (15, 35, 60), dtype=np.uint8)
        pixels[5:27, 8:24] = (225, 95, 45)
        pixels[3, 34:36] = (225, 95, 45)
        buffer = io.BytesIO()
        Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
        return buffer.getvalue()

    async def flattened(self) -> tuple[dict, str]:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Cleanup Fixture"},
            files={"source": ("fixture.png", self.speck_source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        palette = await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 2, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0})
        return project, palette["result_revision_id"]

    async def artifacts(self, revision_id: str, *, effective: bool = False) -> dict[str, dict]:
        path = "effective-artifacts" if effective else "artifacts"
        rows = (await self.client.get(f"/api/revisions/{revision_id}/{path}")).json()
        return {row["kind"]: row for row in rows}

    async def download(self, row: dict) -> bytes:
        return (await self.client.get(row["download_url"])).content

    async def bundle(self, row: dict) -> SegmentationBundle:
        return SegmentationBundle.load(io.BytesIO(await self.download(row)))

    def revision(self, revision_id: str) -> dict:
        return self.app.state.store.get_revision(revision_id)

    async def test_cleanup_writes_a_contract_valid_cleaned_revision(self) -> None:
        project, palette_revision = await self.flattened()
        requested = {"width_mm": 40, "remove_colour": None, "cleanup": True,
                     "cleanup_action": "remove", "min_region_area": 4}
        job = await self.run_job("cleanup", project["id"], palette_revision, cleanup=requested)
        cleaned_revision = job["result_revision_id"]
        revision = self.revision(cleaned_revision)
        self.assertEqual(revision["kind"], "cleanup")
        self.assertEqual(revision["parent_revision_id"], palette_revision)
        self.assertEqual(revision["settings"]["cleanup"],
                         {**asdict(CleanupSettings()), **requested})
        self.assertEqual(revision["settings"]["stale_concerns"], ["trace", "validation", "export"])
        self.assertEqual(revision["settings"]["palette"]["initial_colours"], 2)

        own = await self.artifacts(cleaned_revision)
        self.assertEqual(set(own), {"flattened", "segmentation", "palette", "report", "cleanup-audit"})
        before = await self.bundle((await self.artifacts(palette_revision))["segmentation"])
        after = await self.bundle(own["segmentation"])
        after.validate()
        # 40 mm over 40 px is 1 mm per pixel, so the 4 mm^2 minimum removes the
        # two-pixel speck and nothing else.
        changed = after.labels != before.labels
        self.assertEqual(int(changed.sum()), 2)
        self.assertTrue(changed[3, 34:36].all())
        self.assertEqual(int(after.labels[3, 34]), int(before.labels[0, 0]))
        np.testing.assert_array_equal(after.palette_costs, before.palette_costs)
        # Automatic protection lapses where the cleanup changed labels; explicit
        # protection is carried unchanged.
        np.testing.assert_array_equal(
            after.protected_features,
            before.protected_features & ~(changed & ~before.explicit_protected_features))
        np.testing.assert_array_equal(after.explicit_protected_features,
                                      before.explicit_protected_features)
        np.testing.assert_array_equal(after.palette_rgb, before.palette_rgb)
        flattened = np.asarray(Image.open(io.BytesIO(await self.download(own["flattened"]))))
        np.testing.assert_array_equal(flattened, after.palette_rgb[after.labels])
        # The palette report is re-rendered over the cleaned raster, with the cleaned shares.
        report = np.asarray(Image.open(io.BytesIO(await self.download(own["report"]))).convert("RGB"))
        self.assertGreater(report.shape[0], flattened.shape[0])
        np.testing.assert_array_equal(report[:flattened.shape[0]], flattened)
        shares = {row["hex"]: row["share_percent"]
                  for row in json.loads(await self.download(own["palette"]))["palette"]}
        counts = np.bincount(after.labels.ravel(), minlength=len(after.palette_rgb))
        for index, rgb in enumerate(after.palette_rgb):
            self.assertEqual(shares["#" + "".join(f"{v:02X}" for v in rgb)],
                             round(float(counts[index]) * 100 / after.labels.size, 4))
        audit = json.loads(await self.download(own["cleanup-audit"]))
        self.assertEqual(audit["physical_dimensions_mm"], [40.0, 32.0])
        self.assertEqual(audit["changed_pixels"], 2)
        self.assertEqual(audit["cleanup"]["removed_regions"], 1)

        # The trace consumes the cleaned raster and the cleanup's size, and never
        # cleans again itself.
        traced = await self.run_job("trace", project["id"], cleaned_revision)
        trace_revision = self.revision(traced["result_revision_id"])
        self.assertNotIn("cleanup", trace_revision["settings"])
        for moved in ("width_mm", "height_mm", "remove_colour", "cleanup", "min_region_area"):
            self.assertNotIn(moved, trace_revision["settings"]["trace"])
        trace_artifacts = await self.artifacts(traced["result_revision_id"], effective=True)
        trace_audit = json.loads(await self.download(trace_artifacts["trace-audit"]))
        self.assertEqual(trace_audit["mode"], "physical")
        self.assertEqual(trace_audit["physical_dimensions_mm"], [40.0, 32.0])
        self.assertIsNone(trace_audit["cleanup"])
        self.assertEqual(trace_audit["cleanup_settings"]["min_region_area"], 4)
        svg = (await self.download(trace_artifacts["traced-edge"])).decode()
        self.assertIn('width="40mm"', svg)
        self.assertIn('height="32mm"', svg)
        self.assertEqual(trace_artifacts["segmentation"]["revision_id"], cleaned_revision)

    async def test_explicitly_protected_detail_survives_cleanup_and_is_reported(self) -> None:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Protected Fixture"},
            files={"source": ("fixture.png", self.speck_source_png(), "image/png")})
        project = imported.json()
        # The user protects the speck when flattening.
        palette = await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 2, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0, "protected_component_points": [[34, 3]]})
        palette_revision = palette["result_revision_id"]
        before = await self.bundle((await self.artifacts(palette_revision))["segmentation"])
        self.assertTrue(before.explicit_protected_features[3, 34:36].all())
        job = await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4})
        cleaned = job["result_revision_id"]
        # Nothing was changed, so the revision keeps no raster of its own (and offers
        # no Cleaned view), and the protected speck is still there.
        self.assertEqual(set(await self.artifacts(cleaned)), {"palette", "cleanup-audit"})
        np.testing.assert_array_equal(await self.labels(cleaned), before.labels)
        audit = await self.cleanup_audit(cleaned)
        self.assertEqual(audit["changed_pixels"], 0)
        self.assertEqual(audit["cleanup"]["protected_kept_regions"], 1)
        self.assertEqual(audit["cleanup"]["removed_regions"], 0)
        self.assertIn("kept 1 explicitly protected regions under the threshold", job["message"])

    async def test_the_cleanup_result_message_counts_cleared_automatic_protection(self) -> None:
        project, palette_revision = await self.flattened()
        job = await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4})
        audit = await self.cleanup_audit(job["result_revision_id"])
        summary = audit["cleanup"]
        self.assertEqual(job["message"], (
            f"Cleanup changed 2 px; removed {summary['auto_protected_components_cleared']} "
            f"automatically protected details ({summary['auto_protected_pixels_cleared']} px); "
            "kept 0 explicitly protected regions under the threshold"))
        off = await self.run_job("cleanup", project["id"], palette_revision, cleanup={"width_mm": 40})
        self.assertEqual(off["message"], "Cleanup off: recorded the size and removed colour")

    async def test_cleanup_off_records_only_the_size_and_removed_colour(self) -> None:
        project, palette_revision = await self.flattened()
        palette = json.loads(await self.download(
            (await self.artifacts(palette_revision))["palette"]))["palette"]
        background = max(palette, key=lambda row: row["share_percent"])
        job = await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "height_mm": 64, "remove_colour": background["hex"]})
        cleaned_revision = job["result_revision_id"]
        self.assertEqual(set(await self.artifacts(cleaned_revision)), {"palette", "cleanup-audit"})
        effective = await self.artifacts(cleaned_revision, effective=True)
        self.assertEqual(effective["segmentation"]["revision_id"], palette_revision)
        self.assertEqual(effective["flattened"]["revision_id"], palette_revision)

        traced = await self.run_job("trace", project["id"], cleaned_revision)
        trace_artifacts = await self.artifacts(traced["result_revision_id"])
        trace_audit = json.loads(await self.download(trace_artifacts["trace-audit"]))
        self.assertEqual(trace_audit["physical_dimensions_mm"], [80.0, 64.0])
        self.assertEqual(trace_audit["colours"]["omitted"]["hex"], background["hex"])
        self.assertEqual(len(trace_audit["colours"]["included"]), 1)

    async def test_trace_without_a_cleanup_is_in_pixels_cutting_every_colour(self) -> None:
        project, palette_revision = await self.flattened()
        traced = await self.run_job("trace", project["id"], palette_revision)
        trace_audit = json.loads(await self.download(
            (await self.artifacts(traced["result_revision_id"]))["trace-audit"]))
        self.assertEqual(trace_audit["mode"], "pixels")
        self.assertIsNone(trace_audit["physical_dimensions_mm"])
        self.assertIsNone(trace_audit["colours"]["omitted"])
        self.assertEqual(len(trace_audit["colours"]["included"]), 2)

    async def test_a_flatten_or_edit_after_cleanup_marks_cleanup_stale(self) -> None:
        project, palette_revision = await self.flattened()
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4}))["result_revision_id"]
        store = self.app.state.store
        self.assertEqual(active_cleanup_settings(store, cleaned)["width_mm"], 40)
        traced = (await self.run_job("trace", project["id"], cleaned))["result_revision_id"]
        # A trace, and its validation, keep reading the cleanup behind them.
        self.assertEqual(active_cleanup_settings(store, traced)["width_mm"], 40)

        edit = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{cleaned}/edits",
            json={"operations": [{"tool": "rename_colour", "label": 0, "name": "Midnight"}]})
        self.assertEqual(edit.status_code, 201, edit.text)
        edited = edit.json()["id"]
        self.assertIn("cleanup", self.revision(edited)["settings"]["stale_concerns"])
        self.assertIsNone(active_cleanup_settings(store, edited))
        self.assertNotIn("cleanup-audit", await self.artifacts(edited, effective=True))
        # A trace is blocked while the cleanup behind it is stale ...
        blocked = await self.run_failed_job("trace", project["id"], edited)
        self.assertIn("the artwork changed after Cleanup: re-run Cleanup", blocked["error"])
        # ... until Cleanup is run again on the changed artwork.
        recleaned = (await self.run_job("cleanup", project["id"], edited, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4}))["result_revision_id"]
        retraced = await self.run_job("trace", project["id"], recleaned)
        audit = json.loads(await self.download(
            (await self.artifacts(retraced["result_revision_id"]))["trace-audit"]))
        self.assertEqual(audit["physical_dimensions_mm"], [40.0, 32.0])

        reflattened = (await self.run_job("palette", project["id"], cleaned, palette={
            "initial_colours": 2, "min_colour_share": 0}))["result_revision_id"]
        self.assertEqual(self.revision(reflattened)["settings"]["stale_concerns"],
                         ["cleanup", "trace", "validation", "export"])
        self.assertIsNone(active_cleanup_settings(store, reflattened))
        self.assertNotIn("cleanup-audit", await self.artifacts(reflattened, effective=True))
        blocked = await self.run_failed_job("trace", project["id"], reflattened)
        self.assertIn("re-run Cleanup", blocked["error"])

    async def labels(self, revision_id: str, *, before_cleanup: bool = False) -> np.ndarray:
        query = "?before_cleanup=true" if before_cleanup else ""
        rows = (await self.client.get(
            f"/api/revisions/{revision_id}/effective-artifacts{query}")).json()
        row = next(row for row in rows if row["kind"] == "segmentation")
        return (await self.bundle(row)).labels

    async def cleanup_audit(self, revision_id: str) -> dict:
        return json.loads(await self.download((await self.artifacts(revision_id))["cleanup-audit"]))

    async def test_rerunning_cleanup_replaces_an_earlier_cleanup_instead_of_stacking(self) -> None:
        project, palette_revision = await self.flattened()
        uncleaned = await self.labels(palette_revision)
        settings = {"width_mm": 40, "cleanup": True, "min_region_area": 4}
        first = (await self.run_job("cleanup", project["id"], palette_revision,
                                    cleanup=settings))["result_revision_id"]
        cleaned = await self.labels(first)
        self.assertEqual(int((cleaned != uncleaned).sum()), 2)
        traced = (await self.run_job("trace", project["id"], first))["result_revision_id"]
        # The raster a cleanup starts from is the uncleaned one, whichever revision
        # the cleanup is run on.
        np.testing.assert_array_equal(await self.labels(traced, before_cleanup=True), uncleaned)

        # Cleanup off after cleanup on restores the uncleaned labels exactly, and its
        # audit is truthful about it.
        off = (await self.run_job("cleanup", project["id"], traced, cleanup={
            "width_mm": 40}))["result_revision_id"]
        np.testing.assert_array_equal(await self.labels(off), uncleaned)
        self.assertEqual((await self.artifacts(off, effective=True))["flattened"]["revision_id"],
                         palette_revision)
        audit = await self.cleanup_audit(off)
        self.assertFalse(audit["cleanup"]["enabled"])
        self.assertEqual(audit["changed_pixels"], 0)
        off_trace = await self.run_job("trace", project["id"], off)
        off_audit = json.loads(await self.download(
            (await self.artifacts(off_trace["result_revision_id"]))["trace-audit"]))
        self.assertEqual(off_audit["physical_dimensions_mm"], [40.0, 32.0])

        # A larger size re-cleans from the uncleaned raster: at 2 mm per pixel the
        # 4 mm^2 minimum is one pixel, so the two-pixel speck survives.
        larger = (await self.run_job("cleanup", project["id"], traced, cleanup={
            **settings, "width_mm": 80}))["result_revision_id"]
        np.testing.assert_array_equal(await self.labels(larger), uncleaned)
        self.assertEqual((await self.cleanup_audit(larger))["changed_pixels"], 0)

        # Stepping back to the first cleanup's settings and re-running reproduces it
        # exactly rather than cleaning its cleaned raster again.
        again = (await self.run_job("cleanup", project["id"], larger,
                                    cleanup=settings))["result_revision_id"]
        np.testing.assert_array_equal(await self.labels(again), cleaned)
        self.assertEqual((await self.cleanup_audit(again))["changed_pixels"], 2)

    async def edit(self, project: dict, revision_id: str, operations: list[dict]) -> str:
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{revision_id}/edits",
            json={"operations": operations})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    async def test_an_edit_after_a_cleanup_applies_to_the_uncleaned_raster(self) -> None:
        project, palette_revision = await self.flattened()
        uncleaned = await self.labels(palette_revision)
        settings = {"width_mm": 40, "cleanup": True, "min_region_area": 4}
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision,
                                      cleanup=settings))["result_revision_id"]
        # The editor shows, and edits, the raster from before the cleanup.
        state = (await self.client.get(f"/api/revisions/{cleaned}/editor")).json()
        counts = np.bincount(uncleaned.ravel(), minlength=len(state["palette"]))
        self.assertEqual([row["share_percent"] for row in state["palette"]],
                         [round(float(c) * 100 / uncleaned.size, 4) for c in counts])
        # Repaint one background pixel; the edit carries the speck the cleanup removed.
        background = int(uncleaned[0, 0])
        speck = int(uncleaned[3, 34])
        edited = await self.edit(project, cleaned, [
            {"tool": "paint_pixels", "target_label": speck, "pixels": [[1, 1]]}])
        expected = uncleaned.copy()
        expected[1, 1] = speck
        np.testing.assert_array_equal(await self.labels(edited), expected)
        self.assertNotEqual(speck, background)

        # Cleanup off after it gives exactly the uncleaned-plus-edited labels, truthfully.
        off = (await self.run_job("cleanup", project["id"], edited, cleanup={
            "width_mm": 40}))["result_revision_id"]
        np.testing.assert_array_equal(await self.labels(off), expected)
        audit = await self.cleanup_audit(off)
        self.assertEqual((audit["cleanup"]["enabled"], audit["changed_pixels"]), (False, 0))
        # At 80 mm the 4 mm^2 minimum is one pixel, so the speck is kept.
        larger = (await self.run_job("cleanup", project["id"], edited, cleanup={
            **settings, "width_mm": 80}))["result_revision_id"]
        np.testing.assert_array_equal(await self.labels(larger), expected)
        # At 40 mm the speck is cleaned from the edited raster once, not twice.
        again = (await self.run_job("cleanup", project["id"], edited,
                                    cleanup=settings))["result_revision_id"]
        self.assertEqual((await self.cleanup_audit(again))["changed_pixels"], 3)

    async def test_a_palette_recolour_after_a_cleanup_applies_to_the_uncleaned_raster(self) -> None:
        project, palette_revision = await self.flattened()
        uncleaned = await self.labels(palette_revision)
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4}))["result_revision_id"]
        recoloured = await self.edit(project, cleaned, [
            {"tool": "recolour_colour", "label": 0, "rgb": [12, 200, 90]}])
        np.testing.assert_array_equal(await self.labels(recoloured), uncleaned)
        effective = await self.artifacts(recoloured, effective=True)
        bundle = await self.bundle(effective["segmentation"])
        self.assertEqual(bundle.palette_rgb[0].tolist(), [12, 200, 90])
        flattened = np.asarray(Image.open(io.BytesIO(await self.download(effective["flattened"]))))
        np.testing.assert_array_equal(flattened, bundle.palette_rgb[uncleaned])
        off = (await self.run_job("cleanup", project["id"], recoloured, cleanup={
            "width_mm": 40}))["result_revision_id"]
        np.testing.assert_array_equal(await self.labels(off), uncleaned)

    async def test_every_cleanup_states_the_shares_of_its_resulting_raster(self) -> None:
        project, palette_revision = await self.flattened()
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4}))["result_revision_id"]
        # The trace records the cleaned raster's palette.json ...
        traced = (await self.run_job("trace", project["id"], cleaned))["result_revision_id"]
        # ... so an audit-only cleanup after it must restate the uncleaned shares.
        off = (await self.run_job("cleanup", project["id"], traced, cleanup={
            "width_mm": 40}))["result_revision_id"]
        effective = await self.artifacts(off, effective=True)
        self.assertEqual(effective["palette"]["revision_id"], off)
        labels = await self.labels(off)
        palette = json.loads(await self.download(effective["palette"]))["palette"]
        counts = np.bincount(labels.ravel(), minlength=len(palette))
        self.assertEqual([row["share_percent"] for row in palette],
                         [round(float(c) * 100 / labels.size, 4) for c in counts])
        self.assertEqual(effective["report"]["revision_id"], palette_revision)

    async def test_stale_concerns_accumulate_along_the_ancestry(self) -> None:
        # A validation-like record under an edit made after a cleanup: the edit marks
        # cleanup stale, so the older cleanup audit must not surface below it either.
        project, palette_revision = await self.flattened()
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4}))["result_revision_id"]
        edit = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{cleaned}/edits",
            json={"operations": [{"tool": "rename_colour", "label": 0, "name": "Midnight"}]})
        below = self.app.state.store.create_revision(
            project["id"], parent_revision_id=edit.json()["id"], kind="validate",
            message="Record below the edit", settings={"stale_concerns": ["export"]})
        effective = await self.artifacts(below["id"], effective=True)
        self.assertNotIn("cleanup-audit", effective)
        self.assertEqual(effective["segmentation"]["revision_id"], edit.json()["id"])

    async def test_a_cleanup_marks_the_trace_behind_it_stale(self) -> None:
        project, palette_revision = await self.flattened()
        traced = (await self.run_job("trace", project["id"], palette_revision))["result_revision_id"]
        cleaned = (await self.run_job("cleanup", project["id"], traced, cleanup={
            "width_mm": 40}))["result_revision_id"]
        effective = await self.artifacts(cleaned, effective=True)
        self.assertFalse({"traced-edge", "traced-overlap", "trace-audit"} & set(effective))
        self.assertIn("cleanup-audit", effective)
        validation = await self.run_failed_job("validate", project["id"], cleaned)
        self.assertIn("stale prerequisite concerns: trace", validation["error"])

    async def test_legacy_trace_settings_stay_as_recorded_and_are_not_applied(self) -> None:
        # A trace revision recorded before the cleanup stage embedded the size, the
        # removed colour and the cleanup in its trace settings. It is left as stored;
        # a new trace from it has no cleanup ancestor, so it traces in pixels.
        project, palette_revision = await self.flattened()
        legacy_settings = {
            "pipeline_stage": "trace", "palette": None, "stale_concerns": ["export"],
            "trace": {**asdict(TraceSettings()), **asdict(CleanupSettings(
                width_mm=400, cleanup=True, min_region_area=2))}}
        legacy = self.app.state.store.create_revision(
            project["id"], parent_revision_id=palette_revision, kind="trace",
            message="Recorded before the cleanup stage", settings=legacy_settings)
        self.assertIsNone(active_cleanup_settings(self.app.state.store, legacy["id"]))
        retraced = await self.run_job("trace", project["id"], legacy["id"])
        audit = json.loads(await self.download(
            (await self.artifacts(retraced["result_revision_id"]))["trace-audit"]))
        self.assertEqual(audit["mode"], "pixels")
        self.assertEqual(self.revision(legacy["id"])["settings"], legacy_settings)

    async def test_the_trace_request_no_longer_accepts_cleanup_fields(self) -> None:
        for field, value in (("width_mm", 100), ("remove_colour", "#000000"), ("cleanup", True)):
            response = await self.client.post("/api/jobs", json={
                "kind": "trace", "project_id": "x", "revision_id": "y", "trace": {field: value}})
            self.assertEqual(response.status_code, 422, field)
        for cleanup in ({"width_mm": 0}, {"min_detail_width": -1}, {"cleanup_action": "shrink"},
                        {"overlap": 1}):
            response = await self.client.post("/api/jobs", json={
                "kind": "cleanup", "project_id": "x", "revision_id": "y", "cleanup": cleanup})
            self.assertEqual(response.status_code, 422, cleanup)

    async def test_an_unknown_removed_colour_fails_the_cleanup_run(self) -> None:
        project, palette_revision = await self.flattened()
        response = await self.client.post("/api/jobs", json={
            "kind": "cleanup", "project_id": project["id"], "revision_id": palette_revision,
            "cleanup": {"remove_colour": "#123456"}})
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("does not uniquely match the palette", job["error"])


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

    def test_cleanup_worker_relays_progress_and_cleans_in_place(self) -> None:
        run_worker(self.spec, on_progress=lambda value, message: None, cancelled=lambda: False)
        subject = self.work / "outputs" / "subject"
        seen: list[tuple[float, str]] = []
        run_worker({"op": "cleanup", "subject": str(subject),
                    "settings": asdict(CleanupSettings(width_mm=120, cleanup=True,
                                                       min_region_area=1))},
                   on_progress=lambda value, message: seen.append((value, message)),
                   cancelled=lambda: False)
        audit = json.loads((subject / "cleanup.json").read_text())
        self.assertEqual(audit["physical_dimensions_mm"], [120.0, 96.0])
        SegmentationBundle.load(subject / "segmentation.npz").validate()
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
