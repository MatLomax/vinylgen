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

from vinylgen.contracts import (
    TRACE_LENGTH_FIELDS, CleanupSettings, PaletteSettings, SegmentationBundle, TraceSettings)
from vinylgen.web.models import TraceJobSettings
from vinylgen.cutter_cleanup import CLEANUP_ALGORITHM_VERSION
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
        self.assertEqual(settings_by_kind["trace"]["stale_concerns"], ["validation", "export"])
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

    async def flattened_project(self, name: str) -> tuple[dict, str]:
        imported = await self.client.post(
            "/api/projects/import", data={"name": name},
            files={"source": ("fixture.png", self.source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        palette = await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 2, "min_colour_share": 0})
        return project, palette["result_revision_id"]

    async def test_a_pixel_era_trace_is_served_in_thousandths_and_retraces_identically(self) -> None:
        # A trace revision recorded before trace lengths were thousandths of the
        # image's longer side stores pixels and no length_unit. The API serves it
        # converted on the raster that run traced (40 px here), so a run posting the
        # served settings resolves back to the recorded pixels and the same geometry.
        project, palette_revision = await self.flattened_project("Pixel Era")
        recorded = {"curve_fit_tolerance": 1.2, "straight_line_max_deviation": 1.3,
                    "minimum_straight_length": 3.0, "corner_analysis_span": 6.0,
                    "curve_feature_scale": 0.6, "max_deviation": None, "corner_threshold": 50.0}
        units = {key: value * 1000 / 40 if key in TRACE_LENGTH_FIELDS and value is not None
                 else value for key, value in recorded.items()}
        units = {**units, "max_deviation": 2.0 * 1000 / 40, "length_unit": "per_mille"}
        first = (await self.run_job("trace", project["id"], palette_revision,
                                    trace=units))["result_revision_id"]
        first_artifacts = await self.effective_kinds(first)
        first_svg = (await self.client.get(first_artifacts["traced-edge"]["download_url"])).content
        audit = json.loads((await self.client.get(
            first_artifacts["trace-audit"]["download_url"])).content)
        self.assertEqual(audit["lengths"]["reference_px"], 40)
        self.assertEqual(audit["lengths"]["px"]["curve_fit_tolerance"], 1.2)
        # The pixel-era record of that same run, as a trace revision with its SVG.
        # It names side smoothness by its former name, corner_straightness, and has no
        # corner sharpening allowance (that followed the maximum deviation).
        legacy_trace = {("corner_straightness" if key == "side_smoothness" else key): value
                        for key, value in asdict(TraceSettings(length_unit="px", **recorded)).items()
                        if key not in ("length_unit", "corner_sharpening_allowance")}
        legacy = self.app.state.store.create_revision(
            project["id"], parent_revision_id=palette_revision, kind="trace",
            message="Recorded in pixels", settings={
                "pipeline_stage": "trace", "palette": None, "stale_concerns": ["export"],
                "trace": legacy_trace})
        uploaded = await self.client.post(
            f"/api/revisions/{legacy['id']}/artifacts", data={"kind": "traced-overlap"},
            files={"file": ("traced-overlap.svg", first_svg, "image/svg+xml")})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        served = next(row for row in (await self.client.get(
            f"/api/projects/{project['id']}/revisions")).json() if row["id"] == legacy["id"])
        trace = served["settings"]["trace"]
        self.assertEqual(trace["length_unit"], "per_mille")
        self.assertNotIn("corner_straightness", trace)
        followed = {"side_smoothness": "straight_line_max_deviation",
                    "corner_sharpening_allowance": "max_deviation"}
        for name in TRACE_LENGTH_FIELDS:
            self.assertAlmostEqual(trace[name], units[followed.get(name, name)], msg=name)
        # The stored record is untouched.
        self.assertEqual(self.app.state.store.get_revision(legacy["id"])["settings"]["trace"],
                         legacy_trace)
        again = (await self.run_job("trace", project["id"], palette_revision, trace={
            key: value for key, value in trace.items()
            if key in TraceJobSettings.model_fields}))["result_revision_id"]
        again_svg = (await self.client.get(
            (await self.effective_kinds(again))["traced-edge"]["download_url"])).content
        self.assertEqual(again_svg, first_svg)

    async def effective_kinds(self, revision_id: str) -> dict[str, dict]:
        rows = (await self.client.get(f"/api/revisions/{revision_id}/effective-artifacts")).json()
        return {row["kind"]: row for row in rows}

    async def test_a_new_trace_run_needs_its_own_validation_before_export(self) -> None:
        # The trace stepper lets a new run hang off an older run's validation.
        project, palette_revision = await self.flattened_project("Rerun Fixture")
        first = (await self.run_job("trace", project["id"], palette_revision))["result_revision_id"]
        first_validation = (await self.run_job(
            "validate", project["id"], first))["result_revision_id"]
        second = (await self.run_job(
            "trace", project["id"], first_validation))["result_revision_id"]

        self.assertNotIn("validation", await self.effective_kinds(second))
        refused = await self.run_failed_job("export", project["id"], second)
        self.assertIn("stale prerequisite concerns: validation", refused["error"])

        second_validation = (await self.run_job(
            "validate", project["id"], second))["result_revision_id"]
        validation_row = (await self.effective_kinds(second_validation))["validation"]
        self.assertEqual(validation_row["revision_id"], second_validation)
        exported = (await self.run_job(
            "export", project["id"], second_validation))["result_revision_id"]
        archive_row = (await self.effective_kinds(exported))["export"]
        archive_bytes = (await self.client.get(archive_row["download_url"])).content
        packaged = (await self.client.get(validation_row["download_url"])).content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertEqual(archive.read("validation.json"), packaged)

    async def test_export_refuses_a_run_without_validation_behind_a_neutral_revision(self) -> None:
        # A revision recording no stale concerns, and a trace recorded before a trace
        # marked validation stale, must not let an unvalidated run reach export.
        project, palette_revision = await self.flattened_project("Neutral Fixture")
        store = self.app.state.store
        first = (await self.run_job("trace", project["id"], palette_revision))["result_revision_id"]
        first_validation = (await self.run_job(
            "validate", project["id"], first))["result_revision_id"]
        second = (await self.run_job(
            "trace", project["id"], first_validation))["result_revision_id"]
        note = store.create_revision(project["id"], parent_revision_id=second, kind="note",
                                     message="No stale concerns", settings={})
        refused = await self.run_failed_job("export", project["id"], note["id"])
        self.assertIn("stale prerequisite concerns: validation", refused["error"])

        legacy = store.create_revision(
            project["id"], parent_revision_id=first_validation, kind="trace",
            message="Recorded when a trace only marked export stale",
            settings={"pipeline_stage": "trace", "stale_concerns": ["export"]})
        for row in store.list_artifacts(second):
            with store.artifact_path(row["id"]).open("rb") as source:
                store.add_artifact(legacy["id"], kind=row["kind"], filename=row["filename"],
                                   media_type=row["media_type"], source=source)
        self.assertNotIn("validation", await self.effective_kinds(legacy["id"]))
        refused = await self.run_failed_job("export", project["id"], legacy["id"])
        self.assertIn("stale prerequisite concerns: validation", refused["error"])

    async def test_a_new_trace_run_drops_an_inkscape_edit_of_the_older_run(self) -> None:
        project, palette_revision = await self.flattened_project("Inkscape Rerun Fixture")
        first = (await self.run_job("trace", project["id"], palette_revision))["result_revision_id"]
        svg = (await self.client.get(
            (await self.effective_kinds(first))["traced-edge"]["download_url"])).content
        edited = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{first}/inkscape-reimport",
            files={"svg": ("edited.svg", svg, "image/svg+xml")})
        self.assertEqual(edited.status_code, 201, edited.text)
        inkscape_kinds = {"inkscape-svg", "inkscape-preview", "inkscape-audit"}
        self.assertTrue(inkscape_kinds <= set(await self.effective_kinds(edited.json()["id"])))

        second = (await self.run_job(
            "trace", project["id"], edited.json()["id"]))["result_revision_id"]
        self.assertFalse(inkscape_kinds & set(await self.effective_kinds(second)))
        handoff = (await self.client.get(f"/api/revisions/{second}/inkscape-handoff")).json()
        self.assertTrue(all(row["revision_id"] == second for row in handoff["exports"]))
        validated = (await self.run_job("validate", project["id"], second))["result_revision_id"]
        exported = (await self.run_job("export", project["id"], validated))["result_revision_id"]
        archive_row = (await self.effective_kinds(exported))["export"]
        archive_bytes = (await self.client.get(archive_row["download_url"])).content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertNotIn("inkscape-edited.svg", archive.namelist())

    async def test_a_new_trace_run_drops_an_older_runs_export(self) -> None:
        project, palette_revision = await self.flattened_project("Export Rerun Fixture")
        first = (await self.run_job("trace", project["id"], palette_revision))["result_revision_id"]
        validated = (await self.run_job("validate", project["id"], first))["result_revision_id"]
        exported = (await self.run_job("export", project["id"], validated))["result_revision_id"]
        second = (await self.run_job("trace", project["id"], exported))["result_revision_id"]
        effective = await self.effective_kinds(second)
        self.assertFalse({"export", "validation"} & set(effective))

    async def test_exporting_an_export_never_nests_the_earlier_zip(self) -> None:
        project, palette_revision = await self.flattened_project("Export Nesting Fixture")
        traced = (await self.run_job("trace", project["id"], palette_revision))["result_revision_id"]
        validated = (await self.run_job("validate", project["id"], traced))["result_revision_id"]
        first = (await self.run_job("export", project["id"], validated))["result_revision_id"]
        second = (await self.run_job("export", project["id"], first))["result_revision_id"]
        archive_row = (await self.effective_kinds(second))["export"]
        self.assertEqual(archive_row["revision_id"], second)
        archive_bytes = (await self.client.get(archive_row["download_url"])).content
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            self.assertIn("traced-edge.svg", archive.namelist())
            self.assertFalse([name for name in archive.namelist() if name.endswith(".zip")])

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
        self.assertEqual(revision["settings"]["cleanup_algorithm_version"], CLEANUP_ALGORITHM_VERSION)
        self.assertEqual(revision["settings"]["palette"]["initial_colours"], 2)

        own = await self.artifacts(cleaned_revision)
        self.assertEqual(set(own), {"flattened", "segmentation", "palette", "report", "cleanup-audit",
                                    "cleanup-changes-overlay"})
        before = await self.bundle((await self.artifacts(palette_revision))["segmentation"])
        after = await self.bundle(own["segmentation"])
        after.validate()
        # 40 mm over 40 px is 1 mm per pixel, so the 4 mm^2 minimum removes the
        # two-pixel speck and nothing else.
        changed = after.labels != before.labels
        self.assertEqual(int(changed.sum()), 2)
        self.assertTrue(changed[3, 34:36].all())
        # The changes overlay marks exactly the removed pixels, index 1 (opaque red),
        # on a transparent background, one byte per pixel.
        overlay = Image.open(io.BytesIO(await self.download(own["cleanup-changes-overlay"])))
        self.assertEqual(overlay.mode, "P")
        self.assertEqual(overlay.size, (40, 32))
        np.testing.assert_array_equal(np.asarray(overlay), changed.astype(np.uint8))
        self.assertEqual(overlay.getpalette()[:9], [0, 0, 0, 230, 40, 40, 40, 200, 70])
        alpha = np.asarray(overlay.convert("RGBA"))[..., 3]
        np.testing.assert_array_equal(alpha, np.where(changed, 255, 0))
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
        self.assertEqual(audit["algorithm_version"], CLEANUP_ALGORITHM_VERSION)
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
        # A trace of the cleanup keeps its changes overlay.
        self.assertEqual(trace_artifacts["cleanup-changes-overlay"]["revision_id"], cleaned_revision)
        # The export packages it with the rest of the cleanup's artifacts.
        validated = await self.run_job("validate", project["id"], traced["result_revision_id"])
        exported = await self.run_job("export", project["id"], validated["result_revision_id"])
        archive_row = (await self.artifacts(exported["result_revision_id"]))["export"]
        with zipfile.ZipFile(io.BytesIO(await self.download(archive_row))) as archive:
            self.assertEqual(archive.read("cleanup-changes-overlay.png"),
                             await self.download(own["cleanup-changes-overlay"]))

    async def test_the_changes_overlay_is_replaced_by_a_newer_cleanup_and_hidden_by_an_edit(self) -> None:
        project, palette_revision = await self.flattened()
        removing = {"width_mm": 40, "cleanup": True, "min_region_area": 4}
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision,
                                      cleanup=removing))["result_revision_id"]
        self.assertIn("cleanup-changes-overlay", await self.artifacts(cleaned, effective=True))
        # A newer cleanup that changes nothing records no overlay and hides the older one.
        for settings in ({"width_mm": 40, "cleanup": False}, {"width_mm": 40, "cleanup": True}):
            with self.subTest(settings=settings):
                unchanged = (await self.run_job("cleanup", project["id"], cleaned,
                                                cleanup=settings))["result_revision_id"]
                self.assertNotIn("cleanup-changes-overlay", await self.artifacts(unchanged))
                self.assertNotIn("cleanup-changes-overlay",
                                 await self.artifacts(unchanged, effective=True))
        # A newer cleanup that changes pixels replaces it with its own.
        again = (await self.run_job("cleanup", project["id"], cleaned,
                                    cleanup=removing))["result_revision_id"]
        effective = await self.artifacts(again, effective=True)
        self.assertEqual(effective["cleanup-changes-overlay"]["revision_id"], again)
        # An edit after the cleanup makes it stale, and the overlay with it.
        edited = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{again}/edits",
            json={"operations": [{"tool": "paint_pixels", "target_label": 0, "pixels": [[1, 1]]}]})
        self.assertEqual(edited.status_code, 201, edited.text)
        self.assertNotIn("cleanup-changes-overlay",
                         await self.artifacts(edited.json()["id"], effective=True))
        # The raster a cleanup starts from never carries one.
        before = (await self.client.get(
            f"/api/revisions/{again}/effective-artifacts?before_cleanup=true")).json()
        self.assertNotIn("cleanup-changes-overlay", {row["kind"] for row in before})

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

    async def protected_flatten(self, **palette) -> tuple[dict, str]:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Protected Overlay Fixture"},
            files={"source": ("fixture.png", self.speck_source_png(), "image/png")})
        project = imported.json()
        job = await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 2, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0, **palette})
        return project, job["result_revision_id"]

    async def overlay_pixels(self, row: dict) -> np.ndarray:
        image = Image.open(io.BytesIO(await self.download(row)))
        # Stored compactly as a two-entry paletted PNG with a transparent index 0.
        self.assertEqual(image.mode, "P")
        self.assertEqual(len(image.getpalette()), 6)
        return np.asarray(image.convert("RGBA"))

    async def test_a_protecting_flatten_records_its_protected_areas_overlay(self) -> None:
        from vinylgen.web.pipeline_worker import PROTECTED_AREAS_RGBA
        for upscale in (1, 2):
            with self.subTest(flatten_upscale=upscale):
                _, revision = await self.protected_flatten(
                    protected_component_points=[[34, 3]], flatten_upscale=upscale)
                own = await self.artifacts(revision)
                self.assertEqual(own["protected-areas-overlay"]["filename"],
                                 "protected-areas-overlay.png")
                self.assertEqual(own["protected-areas-overlay"]["media_type"], "image/png")
                mask = (await self.bundle(own["segmentation"])).explicit_protected_features
                self.assertTrue(mask.any())
                self.assertEqual(mask.shape, (32 * upscale, 40 * upscale))
                overlay = await self.overlay_pixels(own["protected-areas-overlay"])
                # On the segmentation's (upscaled) grid, marking exactly the protected pixels.
                self.assertEqual(overlay.shape[:2], mask.shape)
                np.testing.assert_array_equal(overlay[..., 3] > 0, mask)
                self.assertTrue((overlay[mask] == PROTECTED_AREAS_RGBA).all())
                self.assertTrue((overlay[~mask] == 0).all())

    async def exported_names(self, project_id: str, revision_id: str) -> list[str]:
        traced = (await self.run_job("trace", project_id, revision_id))["result_revision_id"]
        validated = (await self.run_job("validate", project_id, traced))["result_revision_id"]
        exported = (await self.run_job("export", project_id, validated))["result_revision_id"]
        archive = await self.download((await self.artifacts(exported))["export"])
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            return zipped.namelist()

    async def test_the_export_packages_the_protected_areas_overlay_in_effect(self) -> None:
        project, protected = await self.protected_flatten(protected_component_points=[[34, 3]])
        self.assertIn("protected-areas-overlay.png",
                      await self.exported_names(project["id"], protected))
        project, plain = await self.protected_flatten()
        self.assertNotIn("protected-areas-overlay.png",
                         await self.exported_names(project["id"], plain))

    async def test_a_flatten_that_protects_nothing_records_no_overlay(self) -> None:
        _, revision = await self.protected_flatten()
        self.assertNotIn("protected-areas-overlay", await self.artifacts(revision))
        self.assertNotIn("protected-areas-overlay", await self.artifacts(revision, effective=True))
        # A trace still reads every flatten output it needs without the optional overlay.
        project = self.app.state.store.get_revision(revision)["project_id"]
        await self.run_job("trace", project, revision)

    async def test_a_new_flatten_never_inherits_an_older_runs_protected_areas(self) -> None:
        project, protected = await self.protected_flatten(protected_component_points=[[34, 3]])
        overlay = (await self.artifacts(protected))["protected-areas-overlay"]
        # Derived revisions keep the flatten's explicit protection, so they show its overlay.
        edit = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{protected}/edits",
            json={"operations": [{"tool": "rename_colour", "label": 0, "name": "Midnight"}]})
        self.assertEqual(edit.status_code, 201, edit.text)
        edited = edit.json()["id"]
        cleaned = (await self.run_job("cleanup", project["id"], edited, cleanup={
            "width_mm": 40, "cleanup": True, "min_region_area": 4}))["result_revision_id"]
        for derived in (edited, cleaned):
            effective = await self.artifacts(derived, effective=True)
            self.assertEqual(effective["protected-areas-overlay"]["id"], overlay["id"])
        traced = (await self.run_job("trace", project["id"], cleaned))["result_revision_id"]
        self.assertEqual((await self.artifacts(traced, effective=True))
                         ["protected-areas-overlay"]["id"], overlay["id"])
        # A re-flatten of `cleaned` that protects nothing drops the older run's overlay.
        reflattened = (await self.run_job("palette", project["id"], cleaned, palette={
            "initial_colours": 2, "min_colour_share": 0}))["result_revision_id"]
        self.assertNotIn("protected-areas-overlay",
                         await self.artifacts(reflattened, effective=True))
        # So does a source upscale, which marks the palette concern stale.
        store = self.app.state.store
        upscaled = store.create_upscaled_source(
            protected, message="Upscaled", settings={}, checkpoint="model", seed=1,
            source=io.BytesIO(self.speck_source_png()))
        self.assertNotIn("protected-areas-overlay", store.effective_artifacts(upscaled))

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

    async def swapped_names_trace(self) -> tuple[dict, dict, dict, str, str]:
        """A cleanup removing the background by name, then a trace whose colour names
        swap the two colours' names: (project, background, other, cleanup, trace)."""
        project, palette_revision = await self.flattened()
        palette = json.loads(await self.download(
            (await self.artifacts(palette_revision))["palette"]))["palette"]
        background = max(palette, key=lambda row: row["share_percent"])
        other = next(row for row in palette if row["hex"] != background["hex"])
        cleaned = (await self.run_job("cleanup", project["id"], palette_revision, cleanup={
            "width_mm": 40, "remove_colour": background["name"]}))["result_revision_id"]
        swapped = {background["hex"].upper(): other["name"], other["hex"].upper(): background["name"]}
        traced = (await self.run_job("trace", project["id"], cleaned,
                                     colour_names=swapped))["result_revision_id"]
        return project, background, other, cleaned, traced

    async def omitted_hex(self, trace_revision: str) -> str:
        audit = json.loads(await self.download(
            (await self.artifacts(trace_revision))["trace-audit"]))
        included = {row["hex"] for row in audit["colours"]["included"]}
        self.assertNotIn(audit["colours"]["omitted"]["hex"], included)
        return audit["colours"]["omitted"]["hex"]

    async def test_a_trace_renaming_colours_omits_the_colour_the_cleanup_removed(self) -> None:
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        cleaned_palette = json.loads(await self.download(
            (await self.artifacts(cleaned))["palette"]))["palette"]
        self.assertEqual(
            self.revision(cleaned)["settings"]["removed_colour_index"],
            [row["hex"] for row in cleaned_palette].index(background["hex"]))
        self.assertEqual(await self.omitted_hex(traced), background["hex"])
        # Resolving the recorded name against the trace's renamed palette would have
        # omitted the other colour: the removed one now carries the other's name.
        audit = json.loads(await self.download((await self.artifacts(traced))["trace-audit"]))
        self.assertEqual(audit["colours"]["omitted"]["name"], other["name"])
        # The trace's palette.json now gives the removed colour's name to the other
        # colour; a trace from it (no renames of its own) still omits the removed one,
        # and the editor state and automatic order name the same colour.
        renamed = json.loads(await self.download((await self.artifacts(traced))["palette"]))
        self.assertEqual(next(row["name"] for row in renamed["palette"]
                              if row["hex"] == other["hex"]), background["name"])
        retraced = (await self.run_job("trace", project["id"], traced))["result_revision_id"]
        self.assertEqual(await self.omitted_hex(retraced), background["hex"])
        state = (await self.client.get(f"/api/revisions/{traced}/editor")).json()
        self.assertEqual(state["palette"][state["removed_colour_index"]]["hex"], background["hex"])
        order = (await self.client.get(
            f"/api/revisions/{traced}/automatic-layer-order")).json()["automatic_layer_order"]
        self.assertEqual(order, [other["hex"]])

    async def test_a_cleanup_recorded_without_the_index_resolves_its_own_palette(self) -> None:
        # A cleanup recorded before the removed colour's index was: its name is resolved
        # against the palette.json that cleanup wrote, never a trace's renamed one.
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        store = self.app.state.store
        settings = self.revision(cleaned)["settings"]
        del settings["removed_colour_index"]
        with store.connection() as db:
            db.execute("UPDATE revisions SET settings_json=? WHERE id=?",
                       (json.dumps(settings, sort_keys=True, separators=(",", ":")), cleaned))
        self.assertNotIn("removed_colour_index", self.revision(cleaned)["settings"])
        retraced = (await self.run_job("trace", project["id"], traced))["result_revision_id"]
        self.assertEqual(await self.omitted_hex(retraced), background["hex"])
        state = (await self.client.get(f"/api/revisions/{traced}/editor")).json()
        self.assertEqual(state["palette"][state["removed_colour_index"]]["hex"], background["hex"])

    async def reported_removed_hex(self, project: dict, cleanup_revision: str) -> str | None:
        """The removed colour's hex the API reports on ``cleanup_revision``, from both the
        project and the revision list the app reads."""
        reported = []
        for path in (f"/api/projects/{project['id']}", f"/api/projects/{project['id']}/revisions"):
            body = (await self.client.get(path)).json()
            rows = body["revisions"] if isinstance(body, dict) else body
            reported.append(next(row for row in rows if row["id"] == cleanup_revision)
                            ["settings"]["removed_colour_hex"])
        self.assertEqual(reported[0], reported[1])
        return reported[0]

    async def test_a_cleanup_recorded_without_its_hex_reports_it_from_its_own_palette(self) -> None:
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        self.assertEqual(self.revision(cleaned)["settings"]["removed_colour_hex"], background["hex"])
        store = self.app.state.store
        settings = self.revision(cleaned)["settings"]
        del settings["removed_colour_hex"], settings["removed_colour_index"]
        with store.connection() as db:
            db.execute("UPDATE revisions SET settings_json=? WHERE id=?",
                       (json.dumps(settings, sort_keys=True, separators=(",", ":")), cleaned))
        # Resolved from the palette.json that cleanup wrote, not the trace's renamed one,
        # and reported only: the stored record is unchanged.
        self.assertEqual(await self.reported_removed_hex(project, cleaned), background["hex"])
        self.assertNotIn("removed_colour_hex", self.revision(cleaned)["settings"])

    async def test_rerunning_the_last_cleanup_refuses_a_hex_another_colour_is_named(self) -> None:
        # The other colour is renamed to the removed colour's hex, so the hex matches two
        # colours as the cleanup job resolves it (by name or hex): a 409, and no job.
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        state = (await self.client.get(f"/api/revisions/{traced}/editor")).json()
        label = [row["hex"] for row in state["palette"]].index(other["hex"])
        renamed = await self.edit(project, traced, [
            {"tool": "rename_colour", "label": label, "name": background["hex"]}])
        self.assertIn("cleanup", self.revision(renamed)["settings"]["stale_concerns"])
        with self.app.state.store.connection() as db:
            jobs_before = int(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        response = await self.rerun_last_cleanup(project, renamed)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         f"the colour the last Cleanup removed ({background['hex']}) now matches "
                         "more than one colour of this artwork's palette, by hex or by name. "
                         "Choose the colour to remove in the Cleanup form and run Cleanup")
        with self.app.state.store.connection() as db:
            self.assertEqual(int(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]), jobs_before)
        # The job itself refuses the same settings: the check is the job's own.
        response = await self.client.post("/api/jobs", json={
            "kind": "cleanup", "project_id": project["id"], "revision_id": renamed,
            "cleanup": {**self.revision(cleaned)["settings"]["cleanup"],
                        "remove_colour": background["hex"]}})
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("does not uniquely match", job["error"] or job.get("message", ""))

    async def test_a_hex_two_colours_share_is_inherited_as_the_removed_colour_s_name(self) -> None:
        # A recolour gives the two colours one RGB; a cleanup then removes the first by
        # its unique name, so its recorded hex matches both. The form inherits that
        # colour's current name instead, and its preview, a Run of it and a Re-run all
        # remove that colour.
        project, palette_revision = await self.flattened()
        state = (await self.client.get(f"/api/revisions/{palette_revision}/editor")).json()
        first, second = state["palette"]
        recoloured = await self.edit(project, palette_revision, [
            {"tool": "recolour_colour", "label": 0, "rgb": second["rgb"]}])
        state = (await self.client.get(f"/api/revisions/{recoloured}/editor")).json()
        self.assertEqual(state["palette"][0]["hex"], state["palette"][1]["hex"])
        name = state["palette"][0]["name"]
        settings = {"width_mm": 40, "remove_colour": name}
        cleaned = (await self.run_job("cleanup", project["id"], recoloured,
                                      cleanup=settings))["result_revision_id"]
        self.assertEqual(await self.reported_removed_hex(project, cleaned), second["hex"])
        state = (await self.client.get(f"/api/revisions/{cleaned}/editor")).json()
        self.assertEqual(state["inherited_remove_colour"],
                         {"value": name, "unresolved": False, "hex": second["hex"], "problem": None})
        form = {**settings, "remove_colour": state["inherited_remove_colour"]["value"]}
        preview = await self.client.post(f"/api/revisions/{cleaned}/cleanup-preview", json={
            "window": {"x": 0, "y": 0, "width": 40, "height": 32}, "cleanup": form})
        self.assertEqual(preview.status_code, 200, preview.text)
        # The shown raster is this cleanup's own result, so nothing would change.
        self.assertEqual((preview.json()["removed_pixels"], preview.json()["widened_pixels"],
                          preview.json()["returned_pixels"]), (0, 0, 0))
        run = (await self.run_job("cleanup", project["id"], cleaned, cleanup=form))["result_revision_id"]
        self.assertEqual((await self.cleanup_audit(run))["omitted_index"], 0)
        response = await self.rerun_last_cleanup(project, cleaned)
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "succeeded", job)
        self.assertEqual((await self.cleanup_audit(job["result_revision_id"]))["omitted_index"], 0)
        self.assertEqual(self.revision(job["result_revision_id"])["settings"]["cleanup"]["remove_colour"], name)

    async def test_the_inherited_removed_colour_after_a_rename_is_the_cleanup_s_hex(self) -> None:
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        state = (await self.client.get(f"/api/revisions/{traced}/editor")).json()
        self.assertEqual(state["inherited_remove_colour"],
                         {"value": background["hex"], "unresolved": False, "hex": background["hex"],
                          "problem": None})
        # No cleanup, or one that removed nothing: nothing is inherited.
        _, palette_revision = await self.flattened()
        self.assertIsNone((await self.client.get(
            f"/api/revisions/{palette_revision}/editor")).json()["inherited_remove_colour"])

    async def test_a_recoloured_removed_colour_after_a_stale_cleanup_is_unresolved(self) -> None:
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        state = (await self.client.get(f"/api/revisions/{traced}/editor")).json()
        label = [row["hex"] for row in state["palette"]].index(background["hex"])
        recoloured = await self.edit(project, traced, [
            {"tool": "recolour_colour", "label": label, "rgb": [12, 200, 90]}])
        state = (await self.client.get(f"/api/revisions/{recoloured}/editor")).json()
        # The hex the form keeps (blocking Cleanup until edited) and the re-run's 409.
        self.assertEqual(state["inherited_remove_colour"],
                         {"value": None, "unresolved": True, "hex": background["hex"],
                          "problem": "missing"})
        response = await self.rerun_last_cleanup(project, recoloured)
        self.assertEqual(response.status_code, 409, response.text)

    async def test_a_reflatten_leaves_the_removed_colour_unresolved_and_says_so(self) -> None:
        # A re-flatten after the cleanup makes a new palette; the cleanup's hex is not in
        # it, and no name is carried across the palette change.
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        reflattened = (await self.run_job(
            "palette", project["id"], traced,
            palette={"initial_colours": 2, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0, "histogram_bits": 3}))["result_revision_id"]
        state = (await self.client.get(f"/api/revisions/{reflattened}/editor")).json()
        self.assertNotIn(background["hex"], [row["hex"] for row in state["palette"]])
        self.assertEqual(state["inherited_remove_colour"],
                         {"value": None, "unresolved": True, "hex": background["hex"],
                          "problem": "missing"})
        response = await self.rerun_last_cleanup(project, reflattened)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("is not in this artwork's palette any more (it was re-flattened, "
                      "recoloured or merged)", response.json()["detail"])

    async def rerun_last_cleanup(self, project: dict, revision_id: str) -> httpx.Response:
        """Start the Trace panel's "Re-run Cleanup with its last settings" job."""
        return await self.client.post("/api/jobs", json={
            "kind": "cleanup", "project_id": project["id"], "revision_id": revision_id,
            "rerun_last_cleanup": True})

    async def test_rerunning_the_last_cleanup_removes_the_colour_it_removed_after_a_rename(self) -> None:
        # Cleanup removes the background by name, a trace swaps the two colours' names,
        # and a label edit on that trace marks the cleanup stale.
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        edited = await self.edit(project, traced, [
            {"tool": "paint_pixels", "target_label": 0, "pixels": [[1, 1]]}])
        self.assertIn("cleanup", self.revision(edited)["settings"]["stale_concerns"])
        # The palette the re-run starts from gives the removed colour's recorded name to
        # the other colour: re-running with the recorded name would remove the other.
        state = (await self.client.get(f"/api/revisions/{edited}/editor")).json()
        renamed = next(row for row in state["palette"] if row["name"] == background["name"])
        self.assertEqual(renamed["hex"], other["hex"])
        recorded = self.revision(cleaned)["settings"]["cleanup"]
        # The Cleanup form inherits the recorded settings with the removed colour named
        # by the hex the API reports for that cleanup, so running them as the form sends
        # them removes the same colour, not the one that took its name.
        form = {**recorded, "remove_colour": await self.reported_removed_hex(project, cleaned)}
        self.assertEqual(form["remove_colour"], background["hex"])
        by_form = (await self.run_job("cleanup", project["id"], edited,
                                      cleanup=form))["result_revision_id"]
        self.assertEqual((await self.cleanup_audit(by_form))["omitted_colour"]["rgb"],
                         background["rgb"])

        response = await self.rerun_last_cleanup(project, edited)
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "succeeded", job)
        rerun = job["result_revision_id"]
        audit = await self.cleanup_audit(rerun)
        self.assertEqual(audit["omitted_colour"]["rgb"], background["rgb"])
        # The re-run keeps the last cleanup's other settings and names the colour by hex.
        settings = self.revision(rerun)["settings"]
        self.assertEqual(settings["cleanup"], {**recorded, "remove_colour": background["hex"]})
        self.assertEqual(state["palette"][settings["removed_colour_index"]]["hex"], background["hex"])
        retraced = (await self.run_job("trace", project["id"], rerun))["result_revision_id"]
        self.assertEqual(await self.omitted_hex(retraced), background["hex"])

    async def test_rerunning_the_last_cleanup_fails_when_its_colour_was_recoloured(self) -> None:
        project, background, other, cleaned, traced = await self.swapped_names_trace()
        state = (await self.client.get(f"/api/revisions/{traced}/editor")).json()
        label = [row["hex"] for row in state["palette"]].index(background["hex"])
        recoloured = await self.edit(project, traced, [
            {"tool": "recolour_colour", "label": label, "rgb": [12, 200, 90]}])
        def job_count() -> int:
            with self.app.state.store.connection() as db:
                return int(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

        jobs_before = job_count()
        response = await self.rerun_last_cleanup(project, recoloured)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"],
                         f"the colour the last Cleanup removed ({background['hex']}) is not in "
                         "this artwork's palette any more (it was re-flattened, recoloured or "
                         "merged). Choose the colour to remove in the Cleanup form and run Cleanup")
        self.assertEqual(job_count(), jobs_before)

    async def test_rerunning_the_last_cleanup_follows_its_colour_across_a_merge(self) -> None:
        # Three colours; the cleanup removes the last. Merging the first colour away
        # shifts the removed colour's palette index down by one.
        pixels = np.full((32, 40, 3), (15, 35, 60), dtype=np.uint8)
        pixels[4:28, 4:18] = (225, 95, 45)
        pixels[4:28, 22:36] = (40, 200, 90)
        buffer = io.BytesIO()
        Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Merge Fixture"},
            files={"source": ("fixture.png", buffer.getvalue(), "image/png")})
        project = imported.json()
        flattened = (await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 3, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0}))["result_revision_id"]
        palette = json.loads(await self.download(
            (await self.artifacts(flattened))["palette"]))["palette"]
        self.assertEqual(len(palette), 3)
        removed = palette[2]
        cleaned = (await self.run_job("cleanup", project["id"], flattened, cleanup={
            "width_mm": 40, "remove_colour": removed["name"]}))["result_revision_id"]
        merged = await self.edit(project, cleaned, [
            {"tool": "merge_colour", "source_label": 0, "target_label": 1}])
        state = (await self.client.get(f"/api/revisions/{merged}/editor")).json()
        self.assertEqual([row["hex"] for row in state["palette"]].index(removed["hex"]), 1)
        response = await self.rerun_last_cleanup(project, merged)
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait_for_job(response.json()["id"])
        self.assertEqual(job["status"], "succeeded", job)
        audit = await self.cleanup_audit(job["result_revision_id"])
        self.assertEqual((audit["omitted_colour"]["rgb"], audit["omitted_index"]),
                         (removed["rgb"], 1))

    async def test_rerunning_the_last_cleanup_needs_a_cleanup_and_no_settings(self) -> None:
        project, palette_revision = await self.flattened()
        response = await self.rerun_last_cleanup(project, palette_revision)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "there is no earlier Cleanup run to re-run")
        for extra in ({"cleanup": {"width_mm": 40}}, {"kind": "trace"}):
            response = await self.client.post("/api/jobs", json={
                "kind": "cleanup", "project_id": project["id"], "revision_id": palette_revision,
                "rerun_last_cleanup": True, **extra})
            self.assertEqual(response.status_code, 422, response.text)

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
            "trace": {**{key: value for key, value in asdict(TraceSettings(length_unit="px")).items()
                         if key != "length_unit"}, **asdict(CleanupSettings(
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


class AutomaticLayerOrderTest(PipelineApiHarness):
    """The automatic layer order is the one a trace of the revision infers when layer
    order can matter; a trace where it cannot does not infer one."""

    def three_colour_png(self) -> bytes:
        # Navy fills the border; a large orange block sits inside, and a small cream block
        # touches the right edge. The palette lists colours by share (orange before cream),
        # but edge contact stacks cream below orange, so the orders differ.
        pixels = np.full((32, 40, 3), (15, 35, 60), dtype=np.uint8)
        pixels[4:28, 4:24] = (225, 95, 45)
        pixels[8:14, 34:40] = (240, 230, 200)
        buffer = io.BytesIO()
        Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
        return buffer.getvalue()

    async def trace_audit(self, project_id: str, revision_id: str, **trace) -> dict:
        traced = (await self.run_job("trace", project_id, revision_id,
                                     trace=trace))["result_revision_id"]
        rows = {row["kind"]: row for row in (await self.client.get(
            f"/api/revisions/{traced}/effective-artifacts")).json()}
        return json.loads((await self.client.get(rows["trace-audit"]["download_url"])).content)

    async def traced_inferred_hex(self, project_id: str, revision_id: str) -> list[str]:
        audit = await self.trace_audit(project_id, revision_id, solid_underlayers=True)
        by_name = {row["name"]: row["hex"] for row in audit["colours"]["included"]}
        return [by_name[name] for name in audit["layer_order"]["inferred_bottom_to_top"]]

    async def automatic(self, revision_id: str) -> list[str] | None:
        response = await self.client.get(f"/api/revisions/{revision_id}/automatic-layer-order")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["automatic_layer_order"]

    async def test_matches_the_tracer_with_and_without_a_removed_colour(self) -> None:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Order Fixture"},
            files={"source": ("fixture.png", self.three_colour_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        flattened = (await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 3, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0}))["result_revision_id"]
        state = (await self.client.get(f"/api/revisions/{flattened}/editor")).json()
        self.assertEqual(len(state["palette"]), 3)
        # The editor state no longer carries (or computes) the order.
        self.assertNotIn("automatic_layer_order", state)
        palette_order = [row["hex"] for row in state["palette"]]
        automatic = await self.automatic(flattened)
        # Not palette order: returning it instead would fail here.
        self.assertNotEqual(automatic, palette_order)
        self.assertEqual(automatic, await self.traced_inferred_hex(project["id"], flattened))

        # A cleanup removing a colour (named as the tracer resolves it): the order leaves it
        # out, and still differs from the remaining colours' palette order.
        navy = next(row for row in state["palette"] if row["rgb"][2] > row["rgb"][0])
        cleaned = (await self.run_job("cleanup", project["id"], flattened, cleanup={
            "width_mm": 40, "remove_colour": navy["hex"][1:].lower()}))["result_revision_id"]
        cleaned_state = (await self.client.get(f"/api/revisions/{cleaned}/editor")).json()
        cleaned_automatic = await self.automatic(cleaned)
        self.assertEqual(len(cleaned_automatic), 2)
        self.assertNotIn(navy["hex"], cleaned_automatic)
        self.assertNotEqual(cleaned_automatic,
                            [row["hex"] for row in cleaned_state["palette"] if row["hex"] != navy["hex"]])
        self.assertEqual(cleaned_automatic, await self.traced_inferred_hex(project["id"], cleaned))

    async def test_a_trace_where_order_cannot_matter_infers_none(self) -> None:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Order Fixture"},
            files={"source": ("fixture.png", self.three_colour_png(), "image/png")})
        project = imported.json()
        flattened = (await self.run_job(
            "palette", project["id"], project["revisions"][0]["id"],
            palette={"initial_colours": 3, "min_colour_share": 0, "min_region_pixels": 0,
                     "dither_radius": 0}))["result_revision_id"]
        palette = (await self.client.get(f"/api/revisions/{flattened}/editor")).json()["palette"]
        audit = await self.trace_audit(project["id"], flattened)
        order = audit["layer_order"]
        self.assertIsNone(order["inferred_bottom_to_top"])
        self.assertIsNotNone(order["not_inferred"])
        self.assertEqual(order["warnings"], [])
        self.assertEqual(order["effective_bottom_to_top"], [row["name"] for row in palette])


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
