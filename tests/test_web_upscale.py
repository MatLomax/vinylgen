from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import unittest
from pathlib import Path

from PIL import Image

from vinylgen.web.config import WebConfig
from vinylgen.web.jobs import GenerationCancellation
from vinylgen.web.generation import GeneratedImage
from vinylgen.web.models import UpscaleSettings
from vinylgen.web.store import Store
from vinylgen.web.upscale import (
    DEFAULT_MODE_TYPE,
    DEFAULT_SEAM_FIX_MODE,
    TiledRegenProvider,
    UpscaleRunner,
    build_upscale_graph,
    expected_tile_total,
    resolved_upscale_by,
    validate_sample_size,
)


def _node(graph: dict, class_type: str) -> dict:
    matches = [n for n in graph.values() if n["class_type"] == class_type]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {class_type}, found {len(matches)}")
    return matches[0]


def _nodes(graph: dict, class_type: str) -> list[dict]:
    return [n for n in graph.values() if n["class_type"] == class_type]


def _png(colour: tuple[int, int, int] = (30, 40, 50), size=(32, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return buffer.getvalue()


class BuildUpscaleGraphTest(unittest.TestCase):
    def test_default_settings_carry_tuned_tiled_regen_inputs(self) -> None:
        graph = build_upscale_graph(
            UpscaleSettings(), image_filename="source.png", seed=7)
        tiled = _node(graph, "TiledRegen")["inputs"]
        self.assertEqual(tiled["denoise"], 0.7)
        self.assertEqual(tiled["cfg"], 5.0)
        self.assertEqual(tiled["steps"], 30)
        self.assertEqual(tiled["sampler_name"], "dpmpp_2m_sde")
        self.assertEqual(tiled["scheduler"], "karras")
        self.assertEqual(tiled["sample_size"], 1024)
        self.assertEqual(tiled["tile_width"], 512)
        self.assertEqual(tiled["tile_height"], 512)
        self.assertEqual(tiled["upscale_by"], 2.0)
        self.assertEqual(tiled["seed"], 7)
        self.assertTrue(tiled["force_uniform_tiles"])
        self.assertEqual(tiled["only_tile_col"], -1)
        self.assertEqual(tiled["only_tile_row"], -1)

    def test_default_settings_wire_the_nomos_upscale_model(self) -> None:
        graph = build_upscale_graph(
            UpscaleSettings(), image_filename="source.png", seed=1)
        loader_id, loader = next(
            (nid, n) for nid, n in graph.items()
            if n["class_type"] == "UpscaleModelLoader")
        self.assertEqual(loader["inputs"]["model_name"], "4xNomos8kSCHAT-S.pth")
        tiled = _node(graph, "TiledRegen")["inputs"]
        self.assertEqual(tiled["upscale_model"], [loader_id, 0])

    def test_default_controlnet_strength_is_the_tuned_lock(self) -> None:
        graph = build_upscale_graph(
            UpscaleSettings(), image_filename="source.png", seed=1)
        cn = _node(graph, "ControlNetApplyAdvanced")["inputs"]
        self.assertEqual(cn["strength"], 0.4)
        tiled = _node(graph, "TiledRegen")["inputs"]
        self.assertEqual(tiled["positive"][0], _controlnet_id(graph))
        self.assertEqual(tiled["negative"][0], _controlnet_id(graph))

    def test_checkpoint_default_is_blue_pencil(self) -> None:
        graph = build_upscale_graph(
            UpscaleSettings(), image_filename="source.png", seed=1)
        self.assertEqual(
            _node(graph, "CheckpointLoaderSimple")["inputs"]["ckpt_name"],
            "blue_pencil-XL-v7.0.0.safetensors")

    def test_none_upscaler_drops_the_loader_and_regenerates_one_to_one(self) -> None:
        graph = build_upscale_graph(
            UpscaleSettings(upscaler="none"), image_filename="source.png", seed=1)
        self.assertEqual(_nodes(graph, "UpscaleModelLoader"), [])
        tiled = _node(graph, "TiledRegen")["inputs"]
        self.assertEqual(tiled["upscale_by"], 1.0)
        self.assertNotIn("upscale_model", tiled)

    def test_controlnet_off_drops_the_apply_node(self) -> None:
        graph = build_upscale_graph(
            UpscaleSettings(controlnet=False), image_filename="source.png", seed=1)
        self.assertEqual(_nodes(graph, "ControlNetApplyAdvanced"), [])
        self.assertEqual(_nodes(graph, "ControlNetLoader"), [])


def _controlnet_id(graph: dict) -> str:
    return next(nid for nid, n in graph.items()
               if n["class_type"] == "ControlNetApplyAdvanced")


class ValidateSampleSizeTest(unittest.TestCase):
    def test_rejects_above_1024(self) -> None:
        with self.assertRaisesRegex(ValueError, "1024"):
            validate_sample_size(1032)

    def test_accepts_up_to_1024(self) -> None:
        validate_sample_size(1024)
        validate_sample_size(512)


class FakeProvider:
    name = "tiled-regen"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.content = _png((200, 10, 10), size=(64, 48))

    def upscale(self, *, image, settings, cancelled=lambda: False, progress=None):
        self.calls.append({"image": image, "settings": settings})
        if progress:
            progress(.5, "fake upscale")
        return GeneratedImage(self.content, "image/png", {
            **settings.model_dump(), "seed": settings.seed, "provider": self.name})


class UpscaleRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd() / ".tmp")
        self.store = Store(WebConfig(Path(self.tmp.name)))
        project = self.store.create_project("Upscale Subject")
        self.project_id = project["id"]
        self.parent_id = self.store.create_generated_source(
            self.project_id, message="Generated",
            settings={"width": 64, "height": 48},
            source=io.BytesIO(_png((10, 20, 30), size=(64, 48))))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _upscaled_revisions(self) -> list[dict]:
        return [r for r in self.store.list_revisions(self.project_id)
                if r["kind"] == "upscaled-source"]

    def test_commits_child_revision_and_persists_resolved_seed(self) -> None:
        provider = FakeProvider()
        runner = UpscaleRunner(self.store, provider)
        events: list[tuple[float, str]] = []
        revision_id = runner.run(
            self.parent_id, UpscaleSettings(seed=4242),
            progress=lambda fraction, message: events.append((fraction, message)))

        revision = self.store.get_revision(revision_id)
        self.assertEqual(revision["kind"], "upscaled-source")
        self.assertEqual(revision["parent_revision_id"], self.parent_id)
        self.assertEqual(revision["settings"]["seed"], 4242)
        self.assertEqual(revision["settings"]["checkpoint"],
                         "blue_pencil-XL-v7.0.0.safetensors")
        self.assertEqual(revision["settings"]["stale_concerns"],
                         ["palette", "cleanup", "trace", "validation", "export"])

        artifacts = self.store.effective_artifacts(revision_id)
        self.assertIn("source", artifacts)
        self.assertEqual(
            self.store.artifact_path(artifacts["source"]["id"]).read_bytes(),
            provider.content)
        # The provider received the parent's source, not the new one.
        self.assertEqual(provider.calls[0]["settings"].seed, 4242)
        self.assertTrue(events)

    def test_records_the_parent_and_upscaled_source_pixel_grids(self) -> None:
        # The node rounds its output to a multiple of 8, so the recorded grid is the
        # decoded image's size, not the nominal factor times the parent's size.
        provider = FakeProvider()
        provider.content = _png((200, 10, 10), size=(136, 104))
        revision_id = UpscaleRunner(self.store, provider).run(
            self.parent_id, UpscaleSettings(upscale_factor=2.0, seed=1),
            progress=lambda *_: None)
        settings = self.store.get_revision(revision_id)["settings"]
        self.assertEqual(settings["parent_source_size"], [64, 48])
        self.assertEqual(settings["source_size"], [136, 104])

    def test_random_seed_resolved_when_unset_and_recorded(self) -> None:
        provider = FakeProvider()
        runner = UpscaleRunner(self.store, provider)
        revision_id = runner.run(
            self.parent_id, UpscaleSettings(), progress=lambda *_: None)
        seed = self.store.get_revision(revision_id)["settings"]["seed"]
        self.assertIsInstance(seed, int)
        self.assertGreaterEqual(seed, 0)
        self.assertLess(seed, 2 ** 32)
        # The seed the provider saw is the seed persisted on the revision.
        self.assertEqual(provider.calls[0]["settings"].seed, seed)

    def test_cancellation_at_commit_boundary_persists_nothing(self) -> None:
        provider = FakeProvider()
        runner = UpscaleRunner(self.store, provider)
        cancellation = GenerationCancellation()
        cancellation.set()
        with self.assertRaises(asyncio.CancelledError):
            runner.run(self.parent_id, UpscaleSettings(),
                       progress=lambda *_: None,
                       cancelled=lambda: False,
                       commit=cancellation.commit)
        self.assertEqual(self._upscaled_revisions(), [])


class _FakeComfyClient:
    """A canned ComfyClient exercising the whole provider round trip.

    Its ``iter_progress`` replays the stock ComfyUI ``progress`` text frames the
    ``TiledRegen`` node emits — one per finished tile, ``value`` 1..N — so the
    relay's tile counter advances exactly as it would on a live run. Its
    ``await_history`` blocks until those frames have all been consumed so the
    round trip is deterministic, then honours cancellation the way the real
    client does.
    """

    def __init__(self, total_tiles: int, content: bytes,
                 cancelled_on_history: bool = False) -> None:
        self.total_tiles = total_tiles
        self.content = content
        self.cancelled_on_history = cancelled_on_history
        self.uploaded: tuple[bytes, str] | None = None
        self.submitted: tuple[dict, str] | None = None
        self.interrupted = False
        self.downloaded: list[dict] = []
        self._frames_drained = threading.Event()

    def upload_image(self, png_bytes: bytes, filename: str = "source.png") -> str:
        self.uploaded = (png_bytes, filename)
        return "uploaded.png"

    def submit_prompt(self, graph: dict, client_id: str) -> str:
        self.submitted = (graph, client_id)
        return "prompt-1"

    def iter_progress(self, client_id, *, should_stop=None):
        try:
            for tile in range(1, self.total_tiles + 1):
                if should_stop is not None and should_stop():
                    return
                yield {"type": "progress",
                       "data": {"value": tile, "max": self.total_tiles, "node": "9"}}
        finally:
            self._frames_drained.set()

    def await_history(self, prompt_id, timeout=3600.0, poll=2.0, *, cancelled=None):
        self._frames_drained.wait(5)
        # The real client interrupts and raises from inside the poll when a
        # cancel arrives; this models a cancel that lands during that poll.
        if self.cancelled_on_history or (cancelled is not None and cancelled()):
            self.interrupt()
            raise asyncio.CancelledError
        return {"outputs": {"9": {"images": [
            {"filename": "out.png", "subfolder": "sub", "type": "output"}]}}}

    def output_images(self, entry: dict) -> list[dict]:
        images: list[dict] = []
        for node_output in entry.get("outputs", {}).values():
            images.extend(node_output.get("images", []))
        return images

    def download_image(self, desc: dict) -> bytes:
        self.downloaded.append(desc)
        return self.content

    def interrupt(self) -> None:
        self.interrupted = True


class TiledRegenProviderTest(unittest.TestCase):
    def _tiles(self, settings: UpscaleSettings, width: int, height: int) -> int:
        return expected_tile_total(
            width, height, resolved_upscale_by(settings),
            settings.tile_size, settings.tile_size,
            DEFAULT_MODE_TYPE, DEFAULT_SEAM_FIX_MODE)

    def test_round_trip_uploads_submits_relays_tiles_and_downloads(self) -> None:
        settings = UpscaleSettings(seed=7)
        source = _png((10, 20, 30), size=(1200, 800))
        total = self._tiles(settings, 1200, 800)
        self.assertGreater(total, 1)  # a real grid, not the degenerate 1-tile case

        client = _FakeComfyClient(total, content=b"UPSCALED-PNG")
        provider = TiledRegenProvider(client=client)
        events: list[tuple[float, str]] = []

        result = provider.upscale(
            image=source, settings=settings,
            progress=lambda fraction, message: events.append((fraction, message)))

        # Uploaded the source, submitted a graph carrying the TiledRegen node,
        # downloaded output 0 and returned it as a GeneratedImage.
        self.assertEqual(client.uploaded[0], source)
        graph, _client_id = client.submitted
        self.assertTrue(any(n["class_type"] == "TiledRegen" for n in graph.values()))
        self.assertEqual(client.downloaded, [
            {"filename": "out.png", "subfolder": "sub", "type": "output"}])
        self.assertIsInstance(result, GeneratedImage)
        self.assertEqual(result.content, b"UPSCALED-PNG")
        self.assertEqual(result.media_type, "image/png")
        self.assertEqual(result.metadata["seed"], 7)

        # The tile counter advanced over SSE and reached N.
        tile_messages = [m for _f, m in events if m.startswith("Tile ")]
        self.assertIn(f"Tile {total}/{total}", tile_messages)
        fractions = [f for f, m in events if m.startswith("Tile ")]
        self.assertEqual(fractions, sorted(fractions))  # monotonic
        self.assertLessEqual(max(fractions), 0.9)

    def test_cancellation_after_submit_returns_no_image(self) -> None:
        settings = UpscaleSettings(seed=1)
        source = _png(size=(1200, 800))
        total = self._tiles(settings, 1200, 800)
        client = _FakeComfyClient(total, content=b"UPSCALED-PNG",
                                  cancelled_on_history=True)
        provider = TiledRegenProvider(client=client)

        with self.assertRaises(asyncio.CancelledError):
            provider.upscale(image=source, settings=settings,
                             progress=lambda *_: None)

        # It got as far as uploading and submitting, then cancelled without
        # downloading any output.
        self.assertIsNotNone(client.uploaded)
        self.assertIsNotNone(client.submitted)
        self.assertTrue(client.interrupted)
        self.assertEqual(client.downloaded, [])


if __name__ == "__main__":
    unittest.main()
