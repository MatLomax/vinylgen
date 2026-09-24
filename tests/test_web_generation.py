from __future__ import annotations

import io
import asyncio
import base64
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image

from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.web.generation import GeneratedImage, CodexImageProvider, expand_prompt
from vinylgen.web.jobs import GenerationCancellation


class FakeProvider:
    name = "codex"

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def generate(self, **request) -> GeneratedImage:
        self.requests.append(request)
        image = io.BytesIO()
        raster = Image.new("RGB", (48, 36))
        colours = [(12, 20, 28), (220, 52, 64), (238, 160, 48),
                   (240, 226, 178), (42, 138, 148), (72, 64, 132)]
        for index, colour in enumerate(colours):
            raster.paste(colour, (0, index * 6, 48, (index + 1) * 6))
        raster.save(image, "PNG")
        return GeneratedImage(image.getvalue(), "image/png", {
            "provider": "fake-codex", "model": "test-image", "request_id": "req-test"})


class CodexImageProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp")
        root = Path(self.temp.name)
        self.log = root / "calls.jsonl"
        image = io.BytesIO()
        Image.new("RGB", (7, 5), (90, 80, 70)).save(image, "PNG")
        encoded = base64.b64encode(image.getvalue()).decode()
        self.executable = root / "fake-codex"
        self.executable.write_text(f'''#!/usr/bin/env python3
import base64, json, os, pathlib, signal, subprocess, sys, time
if "--version" in sys.argv:
    print("codex-cli test-1")
    raise SystemExit(0)
if sys.argv[1:] == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
cwd = pathlib.Path.cwd()
with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
    log.write(json.dumps({{"cwd": str(cwd), "before": sorted(p.name for p in cwd.iterdir()), "argv": sys.argv[1:], "api_key": os.environ.get("OPENAI_API_KEY")}}) + "\\n")
prompt = sys.argv[-1]
print(json.dumps({{"type": "thread.started", "thread_id": "safe-thread-id"}}), flush=True)
print(json.dumps({{"type": "access_token_sk-proj-SECRET", "turn_id": "sk-proj-SECRET"}}), flush=True)
if "missing login" in prompt:
    print("Authentication required; run codex login", file=sys.stderr)
    raise SystemExit(1)
if "unavailable tool" in prompt:
    print("imagegen tool unavailable", file=sys.stderr)
    raise SystemExit(1)
if "sleep forever" in prompt:
    time.sleep(30)
elif "child forever" in prompt:
    child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
    pathlib.Path(os.environ["FAKE_CODEX_CHILD_PID"]).write_text(str(child.pid))
    raise SystemExit(0)
elif "malformed" in prompt:
    (cwd / "output.png").write_bytes(b"not an image")
elif "multiple" in prompt:
    (cwd / "one.png").write_bytes(base64.b64decode("{encoded}"))
    (cwd / "two.png").write_bytes(base64.b64decode("{encoded}"))
elif "no output" not in prompt:
    (cwd / "output.png").write_bytes(base64.b64decode("{encoded}"))
print(json.dumps({{"type": "turn.completed", "secret": "must-not-persist"}}), flush=True)
''')
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)
        self.environment = patch.dict(os.environ, {
            "FAKE_CODEX_LOG": str(self.log),
            "FAKE_CODEX_CHILD_PID": str(root / "child.pid"),
            "OPENAI_API_KEY": "must-not-reach-child"})
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temp.cleanup()

    def provider(self, timeout: float = 2) -> CodexImageProvider:
        return CodexImageProvider(str(self.executable), Path(self.temp.name) / "scratch", timeout)

    def test_success_uses_fresh_ephemeral_empty_jobs_and_sanitized_metadata(self) -> None:
        first = self.provider().generate(prompt="griffin", size="1024x1024", quality="high")
        second = self.provider().generate(prompt="whale", size="1536x1024", quality="low")
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]["cwd"], calls[1]["cwd"])
        self.assertEqual(calls[0]["before"], [])
        self.assertEqual(calls[1]["before"], [])
        self.assertIsNone(calls[0]["api_key"])
        self.assertIn("--ephemeral", calls[0]["argv"])
        self.assertIn("--json", calls[0]["argv"])
        self.assertFalse(Path(calls[0]["cwd"]).exists())
        self.assertEqual(Image.open(io.BytesIO(first.content)).size, (7, 5))
        self.assertEqual(second.metadata["provider"], "codex")
        self.assertEqual(second.metadata["codex_version"], "codex-cli test-1")
        self.assertEqual(second.metadata["dimensions"], [7, 5])
        self.assertNotIn("secret", json.dumps(second.metadata))
        summary = second.metadata["event_summary"]
        self.assertEqual(summary["counts"], {"thread.started": 1, "turn.completed": 1})
        self.assertEqual(summary["ignored_event_count"], 1)
        self.assertNotIn("SECRET", json.dumps(second.metadata))

    def test_missing_login_and_unavailable_tool_are_actionable(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "codex login"):
            self.provider().generate(prompt="missing login", size="1024x1024", quality="low")
        with self.assertRaisesRegex(RuntimeError, "capability is unavailable"):
            self.provider().generate(prompt="unavailable tool", size="1024x1024", quality="low")

    def test_missing_executable_is_actionable_and_leaves_no_job(self) -> None:
        provider = CodexImageProvider(
            str(Path(self.temp.name) / "does-not-exist"), Path(self.temp.name) / "scratch")
        with self.assertRaisesRegex(RuntimeError, "not installed"):
            provider.generate(prompt="griffin", size="1024x1024", quality="low")
        self.assertEqual(list((Path(self.temp.name) / "scratch").iterdir()), [])

    def test_missing_malformed_and_multiple_outputs_fail(self) -> None:
        for prompt, message in (("no output", "without an image"),
                                ("malformed", "not a decodable"),
                                ("multiple", "expected exactly one")):
            with self.subTest(prompt=prompt), self.assertRaisesRegex(RuntimeError, message):
                self.provider().generate(prompt=prompt, size="1024x1024", quality="low")

    def test_timeout_terminates_process_and_cleans_job(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.provider(2).generate(prompt="sleep forever", size="1024x1024", quality="low")
        call = json.loads(self.log.read_text().splitlines()[-1])
        self.assertFalse(Path(call["cwd"]).exists())

    def test_timeout_kills_descendant_after_leader_exits(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.provider(2).generate(prompt="child forever", size="1024x1024", quality="low")
        child_pid = int((Path(self.temp.name) / "child.pid").read_text())
        for _ in range(1000):
            if not Path(f"/proc/{child_pid}").exists():
                break
            time.sleep(.01)
        self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_cancellation_terminates_process(self) -> None:
        cancel = threading.Event()
        caught: list[BaseException] = []

        def run() -> None:
            try:
                self.provider().generate(prompt="sleep forever", size="1024x1024",
                                         quality="low", cancelled=cancel.is_set)
            except BaseException as exc:
                caught.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        for _ in range(1000):
            if self.log.exists() and self.log.stat().st_size:
                break
            time.sleep(.01)
        cancel.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(caught[0], asyncio.CancelledError)
        call = json.loads(self.log.read_text().splitlines()[-1])
        self.assertFalse(Path(call["cwd"]).exists())


class WebGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp")
        self.provider = FakeProvider()
        self.app = create_app(WebConfig(Path(self.tmp.name)), {"codex": self.provider})
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()
        self.tmp.cleanup()

    async def wait(self, job_id: str) -> dict:
        for _ in range(1000):
            job = (await self.client.get(f"/api/jobs/{job_id}")).json()
            if job["status"] not in {"queued", "running"}:
                return job
            await __import__("asyncio").sleep(.01)
        self.fail("generation did not finish")

    async def test_short_prompt_creates_persisted_immutable_source(self) -> None:
        response = await self.client.post("/api/generations", json={
            "prompt": "clockwork griffin", "project_name": "Griffin",
            "settings": {"width": 640, "height": 480, "desired_colours": 6,
                         "required_motifs": ["moon"],
                         "rejected_motifs": ["text"]}})
        self.assertEqual(response.status_code, 202, response.text)
        started = response.json()
        self.assertIn("one solitary, complete, full-frame", started["expanded_prompt"].lower())
        self.assertIn("maximum visual quality", started["expanded_prompt"].lower())
        self.assertIn("rich, intricate scene detail", started["expanded_prompt"].lower())
        self.assertIn("final delivery target after normalization: 4:3 (640 × 480)", started["expanded_prompt"].lower())
        self.assertIn("survive a centred crop", started["expanded_prompt"])
        self.assertIn("composed for the 4:3 final delivery target", started["expanded_prompt"])
        self.assertNotIn("return one independent, highly detailed, maximum-quality 4:3",
                         started["expanded_prompt"].lower())
        self.assertIn("exactly 6 colours total", started["expanded_prompt"].lower())
        self.assertIn("exactly 6 rolls of pre-coloured adhesive vinyl", started["expanded_prompt"])
        self.assertIn("crisp hard-edged shapes", started["expanded_prompt"])
        self.assertIn("Absolutely no gradients", started["expanded_prompt"])
        self.assertIn("dithering, stippling, halftones", started["expanded_prompt"])
        self.assertNotIn("later deterministic", started["expanded_prompt"].lower())
        self.assertNotIn("later stage", started["expanded_prompt"].lower())
        self.assertIn("Required subjects, objects, or motifs: moon.", started["expanded_prompt"])
        self.assertIn("Absolutely forbidden subjects or motifs: occult eyes", started["expanded_prompt"])
        self.assertIn("Chinese dragons", started["expanded_prompt"])
        self.assertIn("trains, text", started["expanded_prompt"])
        self.assertIn("background decoration, symbols, carvings, insignia", started["expanded_prompt"])
        self.assertNotIn("image 2", started["expanded_prompt"].lower())
        self.assertNotIn("four images", started["expanded_prompt"].lower())
        job = await self.wait(started["job"]["id"])
        self.assertEqual(job["status"], "succeeded", job)
        revision = (await self.client.get(
            f"/api/projects/{started['project']['id']}")).json()["revisions"][0]
        self.assertEqual(revision["kind"], "generated-source")
        self.assertIsNone(revision["parent_revision_id"])
        self.assertEqual(revision["settings"]["short_prompt"], "clockwork griffin")
        self.assertEqual(revision["settings"]["provider_quality"], "high")
        self.assertEqual(revision["settings"]["requested_source_colour_count"], 6)
        self.assertEqual(revision["settings"]["expanded_prompt"], started["expanded_prompt"])
        self.assertEqual(revision["settings"]["provider_metadata"]["request_id"], "req-test")
        self.assertEqual(revision["settings"]["reference_artifact_ids"], [])
        artifacts = (await self.client.get(
            f"/api/revisions/{revision['id']}/artifacts")).json()
        image = Image.open(io.BytesIO((await self.client.get(artifacts[0]["download_url"])).content))
        self.assertEqual(image.size, (640, 480))
        self.assertEqual(set(image.getdata()), {
            (12, 20, 28), (220, 52, 64), (238, 160, 48),
            (240, 226, 178), (42, 138, 148), (72, 64, 132),
        })
        self.assertEqual(self.provider.requests[0]["size"], "1536x1024")
        self.assertEqual(self.provider.requests[0]["quality"], "high")
        self.assertEqual(self.provider.requests[0]["prompt"], started["expanded_prompt"])
        self.assertNotIn("image", self.provider.requests[0])

    def test_selected_colour_count_rewrites_the_complete_style_contract(self) -> None:
        expanded = expand_prompt("sky whale", {
            "width": 1536, "height": 1024, "desired_colours": 9,
            "required_motifs": [], "rejected_motifs": [],
        })
        self.assertIn("STRICT 9-COLOUR PALETTE", expanded)
        self.assertIn("exactly 9 colours total", expanded)
        self.assertIn("exactly 9 rolls", expanded)
        self.assertIn("placement of the 9 flat colours", expanded)
        self.assertIn("made from exactly\n9 solid colours", expanded)
        self.assertNotIn("six-colour", expanded.lower())
        self.assertNotIn("six colours", expanded.lower())

    async def test_each_generation_is_an_unreferenced_root(self) -> None:
        first = await self.client.post("/api/generations", json={"prompt": "first world"})
        await self.wait(first.json()["job"]["id"])
        second = await self.client.post("/api/generations", json={"prompt": "second world"})
        job = await self.wait(second.json()["job"]["id"])
        revision = self.app.state.store.get_revision(job["result_revision_id"])
        self.assertIsNone(revision["parent_revision_id"])
        self.assertEqual(revision["settings"]["reference_artifact_ids"], [])
        self.assertEqual(len(self.provider.requests), 2)

    async def test_generation_settings_are_bounded(self) -> None:
        response = await self.client.post("/api/generations", json={
            "prompt": "tiny", "settings": {"width": 10, "height": 10000}})
        self.assertEqual(response.status_code, 422)

        response = await self.client.post("/api/generations", json={
            "prompt": "sky whale", "settings": {"desired_colours": 13}})
        self.assertEqual(response.status_code, 422)

        response = await self.client.post("/api/generations", json={
            "prompt": "sky whale", "settings": {"desired_colours": 1}})
        self.assertEqual(response.status_code, 422)

    async def test_generation_obeys_global_job_cap_without_orphan_project(self) -> None:
        capped = create_app(WebConfig(Path(self.tmp.name) / "capped", max_active_jobs=0),
                            {"codex": self.provider})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=capped),
                                   base_url="http://test")
        try:
            response = await client.post("/api/generations", json={"prompt": "blocked"})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(capped.state.store.list_projects(), [])
        finally:
            await client.aclose()
            await capped.state.jobs.shutdown()

    async def test_provider_cancellation_is_prompt_and_honest(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingProvider(FakeProvider):
            def generate(self, **request) -> GeneratedImage:
                entered.set()
                release.wait(5)
                return super().generate(**request)

        app = create_app(WebConfig(Path(self.tmp.name) / "cancel"),
                         {"codex": BlockingProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        try:
            started = (await client.post("/api/generations", json={"prompt": "slow"})).json()
            for _ in range(1000):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            response = await asyncio.wait_for(
                client.post(f"/api/jobs/{started['job']['id']}/cancel"), timeout=.25)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "running")
            self.assertIn("requested", response.json()["message"].lower())
            release.set()
            job = await self.wait_for_app(client, started["job"]["id"])
            self.assertEqual(job["status"], "cancelled")
            project = (await client.get(f"/api/projects/{started['project']['id']}")).json()
            self.assertEqual(project["revisions"], [])
        finally:
            release.set()
            await client.aclose()
            await app.state.jobs.shutdown()

    async def test_shutdown_signals_running_generation_provider(self) -> None:
        entered = threading.Event()

        class CancellationAwareProvider(FakeProvider):
            def generate(self, **request) -> GeneratedImage:
                entered.set()
                while not request["cancelled"]():
                    time.sleep(.01)
                raise asyncio.CancelledError

        app = create_app(WebConfig(Path(self.tmp.name) / "shutdown"),
                         {"codex": CancellationAwareProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        started = (await client.post("/api/generations", json={"prompt": "slow"})).json()
        for _ in range(1000):
            if entered.is_set():
                break
            await asyncio.sleep(.01)
        await asyncio.wait_for(app.state.jobs.shutdown(), timeout=.5)
        self.assertEqual(app.state.store.get_job(started["job"]["id"])["status"], "cancelled")
        await client.aclose()

    async def test_artifact_failure_rolls_back_generated_revision(self) -> None:
        app = create_app(WebConfig(Path(self.tmp.name) / "atomic"), {"codex": FakeProvider()})
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        try:
            with patch("vinylgen.web.store.shutil.move", side_effect=OSError("disk failed")):
                started = (await client.post(
                    "/api/generations", json={"prompt": "atomic"})).json()
                job = await self.wait_for_app(client, started["job"]["id"])
            self.assertEqual(job["status"], "failed")
            project = (await client.get(f"/api/projects/{started['project']['id']}")).json()
            self.assertEqual(project["revisions"], [])
        finally:
            await client.aclose()
            await app.state.jobs.shutdown()

    def test_commit_boundary_serializes_late_cancellation(self) -> None:
        cancellation = GenerationCancellation()
        entered = threading.Event()
        release = threading.Event()
        result: list[str] = []

        def operation() -> str:
            entered.set()
            release.wait(1)
            return "revision"

        worker = threading.Thread(target=lambda: result.append(cancellation.commit(operation)))
        worker.start()
        entered.wait(1)
        cancel_result: list[bool] = []
        canceller = threading.Thread(target=lambda: cancel_result.append(cancellation.set()))
        canceller.start()
        release.set()
        worker.join(1)
        canceller.join(1)
        self.assertEqual(result, ["revision"])
        self.assertEqual(cancel_result, [False])
        self.assertFalse(cancellation.is_set())

    async def wait_for_app(self, client: httpx.AsyncClient, job_id: str) -> dict:
        for _ in range(1000):
            job = (await client.get(f"/api/jobs/{job_id}")).json()
            if job["status"] not in {"queued", "running"}:
                return job
            await asyncio.sleep(.01)
        self.fail("job did not settle")


if __name__ == "__main__":
    unittest.main()
