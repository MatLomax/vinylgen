from __future__ import annotations

import io
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from PIL import Image

from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.web.generation import GeneratedImage


def png(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


class FakeEditProvider:
    name = "codex"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def edit(self, **request) -> GeneratedImage:
        self.requests.append(request)
        image = Image.open(io.BytesIO(request["image"]))
        return GeneratedImage(png(Image.new("RGB", image.size, (240, 20, 20))), "image/png",
                              {"provider": "fake", "request_id": len(self.requests)})


class WebMaskedEditTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp")
        self.provider = FakeEditProvider()
        self.app = create_app(WebConfig(Path(self.temp.name)), {"codex": self.provider})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                       base_url="http://test")
        source = Image.new("RGB", (12, 10), (10, 30, 90))
        imported = (await self.client.post("/api/projects/import", data={"name": "Masked"},
            files={"source": ("source.png", png(source), "image/png")})).json()
        self.project_id = imported["id"]
        self.revision_id = imported["revisions"][0]["id"]

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()
        self.temp.cleanup()

    async def test_candidates_use_equal_crop_and_mask_and_hard_composition(self) -> None:
        mask = Image.new("L", (12, 10), 0)
        for y in range(3, 6):
            for x in range(4, 8):
                mask.putpixel((x, y), 255)
        response = await self.client.post(
            f"/api/projects/{self.project_id}/revisions/{self.revision_id}/masked-edits",
            data={"prompt": "a golden hatch", "count": "3", "padding": "2"},
            files={"mask": ("mask.png", png(mask), "image/png")})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(len(body["candidates"]), 3)
        revision = body["revision"]
        self.assertEqual(revision["settings"]["crop_size"], revision["settings"]["mask_size"])
        self.assertEqual(len(revision["settings"]["provider_metadata"]), 3)
        self.assertEqual(Image.open(io.BytesIO(self.provider.requests[0]["image"])).size,
                         Image.open(io.BytesIO(self.provider.requests[0]["mask"])).size)
        candidate_bytes = (await self.client.get(body["candidates"][0]["source"]["download_url"])).content
        candidate = Image.open(io.BytesIO(candidate_bytes)).convert("RGB")
        for y in range(10):
            for x in range(12):
                expected = (240, 20, 20) if mask.getpixel((x, y)) else (10, 30, 90)
                self.assertEqual(candidate.getpixel((x, y)), expected)

    async def test_apply_creates_source_branch_and_preserves_comparisons(self) -> None:
        mask = Image.new("L", (12, 10), 0)
        mask.putpixel((5, 5), 255)
        candidates = (await self.client.post(
            f"/api/projects/{self.project_id}/revisions/{self.revision_id}/masked-edits",
            data={"prompt": "remove antenna", "mode": "remove", "count": "1"},
            files={"mask": ("mask.png", png(mask), "image/png")})).json()
        response = await self.client.post(f"/api/projects/{self.project_id}/masked-edits/apply",
            json={"candidate_revision_id": candidates["revision"]["id"], "candidate_index": 1})
        self.assertEqual(response.status_code, 201, response.text)
        applied = response.json()
        self.assertEqual(applied["kind"], "masked-edit")
        self.assertEqual(applied["parent_revision_id"], self.revision_id)
        effective = (await self.client.get(f"/api/revisions/{applied['id']}/effective-artifacts")).json()
        self.assertIn("source", {row["kind"] for row in effective})
        self.assertIn("candidate_revision_id", applied["settings"])

    async def test_rejects_wrong_mask_dimensions(self) -> None:
        response = await self.client.post(
            f"/api/projects/{self.project_id}/revisions/{self.revision_id}/masked-edits",
            data={"prompt": "change"}, files={"mask": ("mask.png", png(Image.new("L", (2, 2), 255)), "image/png")})
        self.assertEqual(response.status_code, 409)
        self.assertIn("exactly match", response.text)

    async def test_masked_edit_obeys_global_job_cap(self) -> None:
        capped = create_app(WebConfig(Path(self.temp.name) / "capped", max_active_jobs=0),
                            {"codex": self.provider})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=capped),
                                   base_url="http://test")
        try:
            source = png(Image.new("RGB", (12, 10), (1, 2, 3)))
            imported = (await client.post("/api/projects/import", data={"name": "Capped"},
                files={"source": ("source.png", source, "image/png")})).json()
            response = await client.post(
                f"/api/projects/{imported['id']}/revisions/{imported['revisions'][0]['id']}/masked-edits",
                data={"prompt": "change"},
                files={"mask": ("mask.png", png(Image.new("L", (12, 10), 255)), "image/png")})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(len(capped.state.store.list_revisions(imported["id"])), 1)
        finally:
            await client.aclose()
            await capped.state.jobs.shutdown()

    async def test_cancelled_http_request_does_not_orphan_blocked_provider_job(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingEditProvider(FakeEditProvider):
            def edit(self, **request) -> GeneratedImage:
                entered.set()
                release.wait(5)
                return super().edit(**request)

        app = create_app(WebConfig(Path(self.temp.name) / "request-cancel", max_active_jobs=1),
                         {"codex": BlockingEditProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            source = png(Image.new("RGB", (12, 10), (1, 2, 3)))
            imported = (await client.post("/api/projects/import", data={"name": "Cancelled"},
                files={"source": ("source.png", source, "image/png")})).json()
            request = asyncio.create_task(client.post(
                f"/api/projects/{imported['id']}/revisions/{imported['revisions'][0]['id']}/masked-edits",
                data={"prompt": "slow edit"},
                files={"mask": ("mask.png", png(Image.new("L", (12, 10), 255)), "image/png")}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set(), "provider was not entered")

            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            self.assertEqual(app.state.store.active_job_count(), 1)
            with app.state.store.connection() as db:
                job_id = db.execute("SELECT id FROM jobs WHERE kind='masked-edit'").fetchone()[0]
            self.assertEqual(app.state.store.get_job(job_id)["status"], "running")

            release.set()
            for _ in range(100):
                job = app.state.store.get_job(job_id)
                if job["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(job["status"], "succeeded", job)
            self.assertIsNotNone(job["finished_at"])
            self.assertEqual(app.state.store.active_job_count(), 0)
        finally:
            release.set()
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_explicit_cancel_during_provider_does_not_commit_candidates(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingEditProvider(FakeEditProvider):
            def edit(self, **request) -> GeneratedImage:
                entered.set()
                release.wait(5)
                return super().edit(**request)

        app = create_app(WebConfig(Path(self.temp.name) / "explicit-cancel"),
                         {"codex": BlockingEditProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        request: asyncio.Task | None = None
        try:
            source = png(Image.new("RGB", (12, 10), (1, 2, 3)))
            imported = (await client.post("/api/projects/import", data={"name": "Cancelled"},
                files={"source": ("source.png", source, "image/png")})).json()
            request = asyncio.create_task(client.post(
                f"/api/projects/{imported['id']}/revisions/{imported['revisions'][0]['id']}/masked-edits",
                data={"prompt": "slow edit"},
                files={"mask": ("mask.png", png(Image.new("L", (12, 10), 255)), "image/png")}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set(), "provider was not entered")
            with app.state.store.connection() as db:
                job_id = db.execute("SELECT id FROM jobs WHERE kind='masked-edit'").fetchone()[0]

            cancelled = await asyncio.wait_for(
                client.post(f"/api/jobs/{job_id}/cancel"), timeout=.25)
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "running")
            release.set()
            await asyncio.gather(request, return_exceptions=True)

            job = app.state.store.get_job(job_id)
            self.assertEqual(job["status"], "cancelled", job)
            revisions = app.state.store.list_revisions(imported["id"])
            self.assertEqual([row["kind"] for row in revisions], ["source"])
        finally:
            release.set()
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_provider_failure_records_terminal_inline_job(self) -> None:
        class FailingEditProvider(FakeEditProvider):
            def edit(self, **request) -> GeneratedImage:
                raise RuntimeError("provider unavailable")

        app = create_app(WebConfig(Path(self.temp.name) / "provider-failure"),
                         {"codex": FailingEditProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            source = png(Image.new("RGB", (12, 10), (1, 2, 3)))
            imported = (await client.post("/api/projects/import", data={"name": "Failure"},
                files={"source": ("source.png", source, "image/png")})).json()
            with self.assertRaises(RuntimeError):
                await client.post(
                    f"/api/projects/{imported['id']}/revisions/{imported['revisions'][0]['id']}/masked-edits",
                    data={"prompt": "fail"},
                    files={"mask": ("mask.png", png(Image.new("L", (12, 10), 255)), "image/png")})
            with app.state.store.connection() as db:
                job_id = db.execute("SELECT id FROM jobs WHERE kind='masked-edit'").fetchone()[0]
            job = app.state.store.get_job(job_id)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"], "provider unavailable")
            self.assertIsNotNone(job["finished_at"])
            self.assertEqual(app.state.store.active_job_count(), 0)
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_shutdown_marks_blocked_inline_job_cancelled(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingEditProvider(FakeEditProvider):
            def edit(self, **request) -> GeneratedImage:
                entered.set()
                release.wait(5)
                return super().edit(**request)

        app = create_app(WebConfig(Path(self.temp.name) / "shutdown"),
                         {"codex": BlockingEditProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        request: asyncio.Task | None = None
        try:
            source = png(Image.new("RGB", (12, 10), (1, 2, 3)))
            imported = (await client.post("/api/projects/import", data={"name": "Shutdown"},
                files={"source": ("source.png", source, "image/png")})).json()
            request = asyncio.create_task(client.post(
                f"/api/projects/{imported['id']}/revisions/{imported['revisions'][0]['id']}/masked-edits",
                data={"prompt": "slow edit"},
                files={"mask": ("mask.png", png(Image.new("L", (12, 10), 255)), "image/png")}))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set(), "provider was not entered")
            with app.state.store.connection() as db:
                job_id = db.execute("SELECT id FROM jobs WHERE kind='masked-edit'").fetchone()[0]

            # shutdown honestly waits for the non-interruptible thread; release
            # it from outside the event loop so the test exercises that path.
            threading.Timer(.05, release.set).start()
            await app.state.jobs.shutdown()
            job = app.state.store.get_job(job_id)
            self.assertEqual(job["status"], "cancelled", job)
            self.assertIsNotNone(job["finished_at"])
            self.assertEqual(app.state.store.active_job_count(), 0)
        finally:
            release.set()
            if request is not None:
                await asyncio.gather(request, return_exceptions=True)
            await client.aclose()

    async def test_apply_records_all_source_downstream_staleness(self) -> None:
        mask = Image.new("L", (12, 10), 0)
        mask.putpixel((1, 1), 255)
        candidates = (await self.client.post(
            f"/api/projects/{self.project_id}/revisions/{self.revision_id}/masked-edits",
            data={"prompt": "change"}, files={"mask": ("mask.png", png(mask), "image/png")})).json()
        applied = (await self.client.post(f"/api/projects/{self.project_id}/masked-edits/apply",
            json={"candidate_revision_id": candidates["revision"]["id"], "candidate_index": 1})).json()
        self.assertEqual(applied["settings"]["stale_concerns"],
                         ["palette", "trace", "validation", "export"])


if __name__ == "__main__":
    unittest.main()
