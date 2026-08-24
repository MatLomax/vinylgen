from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig


class WebFoundationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.app = create_app(WebConfig(Path(self.temp.name)))

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()

    async def test_import_creates_project_revision_and_immutable_artifact(self) -> None:
        response = await self.client.post(
            "/api/projects/import",
            data={"name": "Desert Mech"},
            files={"source": ("source.png", b"not-really-a-png", "image/png")},
        )
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertEqual(project["slug"], "desert-mech")
        revision = project["revisions"][0]

        artifacts = (await self.client.get(f"/api/revisions/{revision['id']}/artifacts")).json()
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["kind"], "source")
        content = await self.client.get(artifacts[0]["download_url"])
        self.assertEqual(content.content, b"not-really-a-png")

        stored = list((Path(self.temp.name) / "artifacts").glob("*/*"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].name, artifacts[0]["sha256"])

    async def test_import_rolls_back_rows_and_cleans_staging_file_when_blob_move_fails(self) -> None:
        store = self.app.state.store
        with mock.patch("vinylgen.web.store.shutil.move", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                store.import_project(
                    "Broken import", filename="source.png", media_type="image/png",
                    source=io.BytesIO(b"source"),
                )

        self.assertEqual(store.list_projects(), [])
        with store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM revisions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM artifacts").fetchone()[0], 0)
        self.assertEqual(list((Path(self.temp.name) / "artifacts").glob(".*.incoming")), [])

    async def test_failed_artifact_read_cleans_staging_file(self) -> None:
        class FailingSource:
            def read(self, _: int) -> bytes:
                raise OSError("upload interrupted")

        store = self.app.state.store
        project = store.create_project("Interrupted")
        revision = store.create_revision(
            project["id"], parent_revision_id=None, kind="source", message=None, settings={},
        )
        with self.assertRaisesRegex(OSError, "upload interrupted"):
            store.add_artifact(
                revision["id"], kind="source", filename="source.png",
                media_type="image/png", source=FailingSource(),
            )

        self.assertEqual(list((Path(self.temp.name) / "artifacts").glob(".*.incoming")), [])

    async def test_failed_artifact_insert_keeps_shared_blob_and_cleans_staging_file(self) -> None:
        store = self.app.state.store
        project = store.create_project("Shared")
        revision = store.create_revision(
            project["id"], parent_revision_id=None, kind="source", message=None, settings={},
        )
        first = store.add_artifact(
            revision["id"], kind="source", filename="same.png",
            media_type="image/png", source=io.BytesIO(b"shared"),
        )
        shared_path = store.artifact_path(first["id"])

        with self.assertRaises(Exception):
            store.add_artifact(
                revision["id"], kind="source", filename="same.png",
                media_type="image/png", source=io.BytesIO(b"shared"),
            )

        self.assertTrue(shared_path.is_file())
        self.assertEqual(shared_path.read_bytes(), b"shared")
        self.assertEqual(list((Path(self.temp.name) / "artifacts").glob(".*.incoming")), [])

    async def test_duplicate_artifact_upload_returns_conflict_not_server_error(self) -> None:
        project = (await self.client.post("/api/projects", json={"name": "Dupe"})).json()
        revision = (await self.client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"kind": "source", "settings": {}},
        )).json()

        first = await self.client.post(
            f"/api/revisions/{revision['id']}/artifacts",
            data={"kind": "source"},
            files={"file": ("same.png", b"one", "image/png")},
        )
        self.assertEqual(first.status_code, 201, first.text)

        second = await self.client.post(
            f"/api/revisions/{revision['id']}/artifacts",
            data={"kind": "source"},
            files={"file": ("same.png", b"two", "image/png")},
        )
        self.assertEqual(second.status_code, 409, second.text)

    async def test_revision_branch_and_artifact_queries(self) -> None:
        project = (await self.client.post("/api/projects", json={"name": "Branches"})).json()
        first = (await self.client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"kind": "source", "settings": {"colours": 6}},
        )).json()
        second = (await self.client.post(
            f"/api/projects/{project['id']}/revisions",
            json={"parent_revision_id": first["id"], "kind": "settings", "settings": {"colours": 7}},
        )).json()
        revisions = (await self.client.get(f"/api/projects/{project['id']}/revisions")).json()
        self.assertEqual({row["id"] for row in revisions}, {first["id"], second["id"]})
        self.assertEqual(second["parent_revision_id"], first["id"])

    async def test_effective_artifacts_hide_stale_ancestor_outputs_but_keep_local_replacements(self) -> None:
        store = self.app.state.store
        project = store.create_project("Freshness")
        parent = store.create_revision(
            project["id"], parent_revision_id=None, kind="trace", message=None, settings={},
        )
        for kind in ("source", "flattened", "traced-overlap", "validation", "export"):
            store.add_artifact(
                parent["id"], kind=kind, filename=f"{kind}.bin",
                media_type="application/octet-stream", source=io.BytesIO(kind.encode()),
            )
        edited = store.create_revision(
            project["id"], parent_revision_id=parent["id"], kind="masked-edit", message=None,
            settings={"stale_concerns": ["palette", "trace", "validation", "export"]},
        )
        store.add_artifact(
            edited["id"], kind="source", filename="source.png", media_type="image/png",
            source=io.BytesIO(b"new source"),
        )

        effective = store.effective_artifacts(edited["id"])
        self.assertEqual(set(effective), {"source"})
        self.assertEqual(effective["source"]["revision_id"], edited["id"])

    async def test_built_frontend_is_served(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Vinylgen", response.text)

    async def test_job_reports_progress_over_sse(self) -> None:
        response = await self.client.post("/api/jobs", json={"steps": 2, "step_delay_ms": 10})
        self.assertEqual(response.status_code, 202, response.text)
        job = response.json()
        async with self.client.stream("GET", f"/api/jobs/{job['id']}/events") as stream:
            body = "".join([chunk async for chunk in stream.aiter_text()])
        self.assertIn('"status": "succeeded"', body)
        self.assertIn('"progress": 1.0', body)

    async def test_completed_subscription_leaves_no_residual_subscriber_entry(self) -> None:
        response = await self.client.post("/api/jobs", json={"steps": 2, "step_delay_ms": 10})
        job = response.json()
        async with self.client.stream("GET", f"/api/jobs/{job['id']}/events") as stream:
            "".join([chunk async for chunk in stream.aiter_text()])
        self.assertNotIn(job["id"], self.app.state.jobs._subscribers)

    async def test_job_can_be_cancelled(self) -> None:
        job = (await self.client.post("/api/jobs", json={"steps": 100, "step_delay_ms": 20})).json()
        cancelled = await self.client.post(f"/api/jobs/{job['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")

    async def test_store_startup_reconciles_jobs_interrupted_by_restart(self) -> None:
        store = self.app.state.store
        queued = store.create_job(kind="demo", project_id=None, revision_id=None)
        running = store.create_job(kind="demo", project_id=None, revision_id=None)
        store.update_job(running["id"], status="running", started_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(store.active_job_count(), 2)

        restarted = create_app(WebConfig(Path(self.temp.name)))
        try:
            self.assertEqual(restarted.state.store.active_job_count(), 0)
            for job_id in (queued["id"], running["id"]):
                job = restarted.state.store.get_job(job_id)
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["message"], "Interrupted by process restart")
                self.assertEqual(job["error"], "Worker process stopped before completion")
                self.assertIsNotNone(job["finished_at"])
        finally:
            await restarted.state.jobs.shutdown()


if __name__ == "__main__":
    unittest.main()
