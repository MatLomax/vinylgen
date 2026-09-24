from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from PIL import Image

from vinylgen.web.app import create_app
from vinylgen.web.comfy_client import ComfyUIError, ComfyUnavailable
from vinylgen.web.config import WebConfig
from vinylgen.web.generation import GeneratedImage
from vinylgen.web.models import UpscaleSettings


def _png(colour: tuple[int, int, int] = (30, 40, 50), size=(64, 48)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


# The installed models the default UpscaleSettings name, so up-front validation
# in the start-upscale route accepts the defaults without a live ComfyUI.
VALID_OPTIONS: dict[tuple[str, str], list[str]] = {
    ("UpscaleModelLoader", "model_name"): ["4xNomos8kSCHAT-S.pth", "4xUltraSharp.pth"],
    ("CheckpointLoaderSimple", "ckpt_name"): ["blue_pencil-XL-v7.0.0.safetensors"],
    ("ControlNetLoader", "control_net_name"): ["xinsir-controlnet-tile-sdxl-1.0.safetensors"],
}


class FakeUpscaleProvider:
    name = "tiled-regen"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.content = _png((200, 10, 10), size=(128, 96))

    def upscale(self, *, image, settings, cancelled=lambda: False, progress=None) -> GeneratedImage:
        self.calls.append({"image": image, "settings": settings})
        if progress:
            progress(.5, "fake tiled upscale")
        return GeneratedImage(self.content, "image/png", {
            **settings.model_dump(), "seed": settings.seed, "provider": self.name})


class BlockingUpscaleProvider(FakeUpscaleProvider):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self.entered = entered
        self.release = release

    def upscale(self, *, image, settings, cancelled=lambda: False, progress=None) -> GeneratedImage:
        self.entered.set()
        self.release.wait(5)
        return super().upscale(image=image, settings=settings, cancelled=cancelled,
                               progress=progress)


class UnreachableUpscaleProvider(FakeUpscaleProvider):
    """ComfyUI stopped answering after the job was accepted."""

    def upscale(self, *, image, settings, cancelled=lambda: False, progress=None) -> GeneratedImage:
        raise ComfyUnavailable("ComfyUI not running at http://127.0.0.1:8188")


class FakeComfyClient:
    """A canned ``ComfyClient`` for the options endpoint, injected by tests."""

    def __init__(self, options: dict[tuple[str, str], list[str]] | None = None,
                 unavailable: bool = False, failing: bool = False) -> None:
        self.options = options or {}
        self.unavailable = unavailable
        self.failing = failing
        self.closed = False

    def object_info_options(self, node_class: str, input_name: str) -> list[str]:
        if self.unavailable:
            raise ComfyUnavailable("ComfyUI not running at http://127.0.0.1:8188")
        if self.failing:
            raise ComfyUIError("ComfyUI /object_info/UpscaleModelLoader failed (500): boom")
        return self.options.get((node_class, input_name), [])

    def close(self) -> None:
        self.closed = True


class WebUpscaleRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp")
        self.provider = FakeUpscaleProvider()
        self.app = create_app(WebConfig(Path(self.tmp.name)),
                              upscale_provider=self.provider,
                              comfy_client_factory=lambda: FakeComfyClient(VALID_OPTIONS))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.store = self.app.state.store
        self.project = self.store.create_project("Upscale Subject")
        self.parent_id = self.store.create_generated_source(
            self.project["id"], message="Generated",
            settings={"width": 64, "height": 48},
            source=io.BytesIO(_png((10, 20, 30), size=(64, 48))))

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()
        self.tmp.cleanup()

    async def wait(self, client: httpx.AsyncClient, job_id: str) -> dict:
        for _ in range(1000):
            job = (await client.get(f"/api/jobs/{job_id}")).json()
            if job["status"] not in {"queued", "running"}:
                return job
            await asyncio.sleep(.01)
        self.fail("upscale did not finish")

    def _upscaled_revisions(self) -> list[dict]:
        return [r for r in self.store.list_revisions(self.project["id"])
                if r["kind"] == "upscaled-source"]

    async def test_upscale_route_commits_child_revision(self) -> None:
        response = await self.client.post(
            f"/api/projects/{self.project['id']}/revisions/{self.parent_id}/upscale",
            json={"settings": {"seed": 4242, "upscale_factor": 2.0}})
        self.assertEqual(response.status_code, 202, response.text)
        started = response.json()
        self.assertEqual(started["project"]["id"], self.project["id"])
        self.assertEqual(started["job"]["kind"], "upscale")
        self.assertEqual(started["job"]["revision_id"], self.parent_id)

        job = await self.wait(self.client, started["job"]["id"])
        self.assertEqual(job["status"], "succeeded", job)

        revision = self.store.get_revision(job["result_revision_id"])
        self.assertEqual(revision["kind"], "upscaled-source")
        self.assertEqual(revision["parent_revision_id"], self.parent_id)
        self.assertEqual(revision["settings"]["seed"], 4242)
        self.assertEqual(revision["settings"]["stale_concerns"],
                         ["palette", "cleanup", "trace", "validation", "export"])
        self.assertEqual(self.provider.calls[0]["settings"].seed, 4242)

    async def test_default_settings_are_applied_when_body_is_empty(self) -> None:
        response = await self.client.post(
            f"/api/projects/{self.project['id']}/revisions/{self.parent_id}/upscale",
            json={})
        self.assertEqual(response.status_code, 202, response.text)
        job = await self.wait(self.client, response.json()["job"]["id"])
        self.assertEqual(job["status"], "succeeded", job)
        settings = self.provider.calls[0]["settings"]
        self.assertEqual(settings.checkpoint, "blue_pencil-XL-v7.0.0.safetensors")
        self.assertEqual(settings.upscaler, "4xNomos8kSCHAT-S.pth")

    async def test_missing_revision_is_404(self) -> None:
        response = await self.client.post(
            f"/api/projects/{self.project['id']}/revisions/does-not-exist/upscale",
            json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._upscaled_revisions(), [])

    async def test_wrong_project_id_is_404(self) -> None:
        other = self.store.create_project("Other")
        response = await self.client.post(
            f"/api/projects/{other['id']}/revisions/{self.parent_id}/upscale",
            json={})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._upscaled_revisions(), [])

    async def test_unknown_upscaler_is_422(self) -> None:
        response = await self.client.post(
            f"/api/projects/{self.project['id']}/revisions/{self.parent_id}/upscale",
            json={"settings": {"upscaler": "not-installed.pth"}})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("not-installed.pth", response.text)
        self.assertEqual(self._upscaled_revisions(), [])

    async def test_unknown_checkpoint_is_422(self) -> None:
        response = await self.client.post(
            f"/api/projects/{self.project['id']}/revisions/{self.parent_id}/upscale",
            json={"settings": {"checkpoint": "made-up.safetensors"}})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("made-up.safetensors", response.text)
        self.assertEqual(self._upscaled_revisions(), [])

    async def test_missing_tile_controlnet_is_422(self) -> None:
        options = dict(VALID_OPTIONS)
        options[("ControlNetLoader", "control_net_name")] = ["some-other-controlnet.safetensors"]
        app = create_app(WebConfig(Path(self.tmp.name) / "nocn"),
                         upscale_provider=FakeUpscaleProvider(),
                         comfy_client_factory=lambda: FakeComfyClient(options))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            store = app.state.store
            project = store.create_project("NoCN")
            parent = store.create_generated_source(
                project["id"], message="Generated", settings={"width": 64, "height": 48},
                source=io.BytesIO(_png(size=(64, 48))))
            # ControlNet on (the default) needs the fixed tile ControlNet model.
            response = await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale", json={})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIn("ControlNet", response.text)
            self.assertEqual(
                [r for r in store.list_revisions(project["id"])
                 if r["kind"] == "upscaled-source"], [])
            # The same settings with ControlNet off pass validation.
            ok = await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale",
                json={"settings": {"controlnet": False}})
            self.assertEqual(ok.status_code, 202, ok.text)
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_start_upscale_reports_comfy_not_running(self) -> None:
        app = create_app(WebConfig(Path(self.tmp.name) / "down"),
                         upscale_provider=FakeUpscaleProvider(),
                         comfy_client_factory=lambda: FakeComfyClient(unavailable=True))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            store = app.state.store
            project = store.create_project("Down")
            parent = store.create_generated_source(
                project["id"], message="Generated", settings={"width": 64, "height": 48},
                source=io.BytesIO(_png(size=(64, 48))))
            response = await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale", json={})
            # A 4xx, so the public proxy passes the JSON detail through.
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json(), {"detail": "ComfyUI is not running."})
            self.assertEqual(
                [r for r in store.list_revisions(project["id"])
                 if r["kind"] == "upscaled-source"], [])
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_upscale_obeys_global_job_cap(self) -> None:
        capped = create_app(WebConfig(Path(self.tmp.name) / "capped", max_active_jobs=0),
                            upscale_provider=self.provider,
                            comfy_client_factory=lambda: FakeComfyClient(VALID_OPTIONS))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=capped),
                                   base_url="http://test")
        try:
            store = capped.state.store
            project = store.create_project("Capped")
            parent = store.create_generated_source(
                project["id"], message="Generated", settings={"width": 64, "height": 48},
                source=io.BytesIO(_png(size=(64, 48))))
            response = await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale", json={})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(
                [r for r in store.list_revisions(project["id"])
                 if r["kind"] == "upscaled-source"], [])
        finally:
            await client.aclose()
            await capped.state.jobs.shutdown()

    async def test_options_reports_models_defaults_and_cap(self) -> None:
        fake = FakeComfyClient({
            ("UpscaleModelLoader", "model_name"): ["4xNomos8kSCHAT-S.pth", "4xUltraSharp.pth"],
            ("CheckpointLoaderSimple", "ckpt_name"): ["blue_pencil-XL-v7.0.0.safetensors"],
            ("ControlNetLoader", "control_net_name"): ["xinsir-controlnet-tile-sdxl-1.0.safetensors"],
        })
        app = create_app(WebConfig(Path(self.tmp.name) / "options"),
                         comfy_client_factory=lambda: fake)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            response = await client.get("/api/upscale/options")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["upscalers"][:2], [
                {"value": "none", "label": "None"},
                {"value": "lanczos", "label": "Lanczos"}])
            self.assertEqual(body["upscalers"][2:], [
                {"value": "4xNomos8kSCHAT-S.pth", "label": "4xNomos8kSCHAT-S.pth"},
                {"value": "4xUltraSharp.pth", "label": "4xUltraSharp.pth"}])
            self.assertEqual(body["checkpoints"], [
                {"value": "blue_pencil-XL-v7.0.0.safetensors",
                 "label": "blue_pencil-XL-v7.0.0.safetensors"}])
            self.assertEqual(body["controlnets"], [
                {"value": "xinsir-controlnet-tile-sdxl-1.0.safetensors",
                 "label": "xinsir-controlnet-tile-sdxl-1.0.safetensors"}])
            self.assertEqual(body["defaults"], UpscaleSettings().model_dump())
            self.assertEqual(body["sample_size_max"], 1024)
            self.assertIs(body["available"], True)
            self.assertTrue(fake.closed)
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_options_reports_unavailable_when_comfy_unreachable(self) -> None:
        fake = FakeComfyClient(unavailable=True)
        app = create_app(WebConfig(Path(self.tmp.name) / "unavailable"),
                         comfy_client_factory=lambda: fake)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            response = await client.get("/api/upscale/options")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"available": False})
            self.assertTrue(fake.closed)
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_options_and_start_are_502_when_comfy_errors(self) -> None:
        fake = FakeComfyClient(failing=True)
        app = create_app(WebConfig(Path(self.tmp.name) / "erroring"),
                         upscale_provider=FakeUpscaleProvider(),
                         comfy_client_factory=lambda: fake)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            response = await client.get("/api/upscale/options")
            self.assertEqual(response.status_code, 502, response.text)
            self.assertIn("boom", response.json()["detail"])
            self.assertTrue(fake.closed)
            store = app.state.store
            project = store.create_project("Erroring")
            parent = store.create_generated_source(
                project["id"], message="Generated", settings={"width": 64, "height": 48},
                source=io.BytesIO(_png(size=(64, 48))))
            response = await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale", json={})
            self.assertEqual(response.status_code, 502, response.text)
            self.assertIn("boom", response.json()["detail"])
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_job_reports_comfy_not_running_when_it_goes_down_mid_run(self) -> None:
        app = create_app(WebConfig(Path(self.tmp.name) / "down-mid-run"),
                         upscale_provider=UnreachableUpscaleProvider(),
                         comfy_client_factory=lambda: FakeComfyClient(VALID_OPTIONS))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            store = app.state.store
            project = store.create_project("Down mid run")
            parent = store.create_generated_source(
                project["id"], message="Generated", settings={"width": 64, "height": 48},
                source=io.BytesIO(_png(size=(64, 48))))
            response = await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale", json={})
            self.assertEqual(response.status_code, 202, response.text)
            job = await self.wait(client, response.json()["job"]["id"])
            self.assertEqual(job["status"], "failed", job)
            self.assertEqual(job["error"], "ComfyUI is not running.")
            self.assertEqual(
                [r for r in store.list_revisions(project["id"])
                 if r["kind"] == "upscaled-source"], [])
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_cancellation_commits_no_revision(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        app = create_app(WebConfig(Path(self.tmp.name) / "cancel"),
                         upscale_provider=BlockingUpscaleProvider(entered, release),
                         comfy_client_factory=lambda: FakeComfyClient(VALID_OPTIONS))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            store = app.state.store
            project = store.create_project("Cancel")
            parent = store.create_generated_source(
                project["id"], message="Generated", settings={"width": 64, "height": 48},
                source=io.BytesIO(_png(size=(64, 48))))
            started = (await client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/upscale", json={})).json()
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            response = await asyncio.wait_for(
                client.post(f"/api/jobs/{started['job']['id']}/cancel"), timeout=.5)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "running")
            self.assertIn("requested", response.json()["message"].lower())
            release.set()
            job = await self.wait(client, started["job"]["id"])
            self.assertEqual(job["status"], "cancelled")
            self.assertEqual(
                [r for r in store.list_revisions(project["id"])
                 if r["kind"] == "upscaled-source"], [])
        finally:
            release.set()
            await client.aclose()
            await app.state.jobs.shutdown()


if __name__ == "__main__":
    unittest.main()
