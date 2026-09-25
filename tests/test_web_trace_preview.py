from __future__ import annotations

import asyncio
import io
import re
import tempfile
import threading
import time
import unittest
from unittest import mock
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
import numpy as np
from PIL import Image

from vinylgen import trace_window
from vinylgen.svg_writer import INKSCAPE, SVG
from vinylgen.trace_lengths import PREVIEW_MARGIN_MIN_PX as PREVIEW_MARGIN_PX
from vinylgen.trace_window import PreviewCancelled
from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.trace_window import PreviewSegmentation
from vinylgen.web import trace_preview
from vinylgen.web.trace_preview import TracePreviewRequest, TracePreviewService

WIDTH, HEIGHT = 620, 610


def source_png() -> bytes:
    """A two-colour disc and a bar on a background, larger than the preview limit."""
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    pixels = np.full((HEIGHT, WIDTH, 3), (15, 35, 60), dtype=np.uint8)
    pixels[(xx - 300) ** 2 + (yy - 280) ** 2 < 150 ** 2] = (225, 95, 45)
    pixels[(xx - 330) ** 2 + (yy - 250) ** 2 < 40 ** 2] = (240, 225, 200)
    pixels[480:520, 60:560] = (240, 225, 200)
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def layer_paths(svg: ET.Element) -> dict[str, list[str]]:
    return {layer.attrib[f"{{{INKSCAPE}}}label"]:
            [path.attrib["d"] for path in layer.iter(f"{{{SVG}}}path")]
            for layer in svg.iter(f"{{{SVG}}}g") if f"{{{INKSCAPE}}}label" in layer.attrib}


class TracePreviewApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.app = create_app(WebConfig(Path(self.temp.name)))

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.app.state.trace_previews.shutdown()
        await self.app.state.jobs.shutdown()
        self.temp.cleanup()

    async def run_job(self, kind: str, project_id: str, revision_id: str, **settings) -> str:
        response = await self.client.post("/api/jobs", json={
            "kind": kind, "project_id": project_id, "revision_id": revision_id, **settings})
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(1200):
            job = (await self.client.get(f"/api/jobs/{response.json()['id']}")).json()
            if job["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(job["status"], "succeeded", job)
        return job["result_revision_id"]

    async def flattened_project(self) -> tuple[str, str, str]:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Preview Fixture"},
            files={"source": ("fixture.png", source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        source_revision = project["revisions"][0]["id"]
        palette_revision = await self.run_job("palette", project["id"], source_revision)
        return project["id"], source_revision, palette_revision

    async def preview(self, revision_id: str, window: dict, trace: dict | None = None,
                      **extra) -> httpx.Response:
        return await self.client.post(f"/api/revisions/{revision_id}/trace-preview", json={
            "window": window, **({"trace": trace} if trace is not None else {}), **extra})

    async def test_preview_traces_the_window_like_the_trace_run(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        # A window whose margin reaches every image edge covers exactly what the trace
        # run traces, so the preview must reproduce that run's geometry path for path.
        window = {"x": 10, "y": 8, "width": WIDTH - 20, "height": HEIGHT - 16}
        for trace in ({}, {"solid_underlayers": True, "curve_fit_tolerance": 0.8,
                           "length_unit": "per_mille"}):
            with self.subTest(trace=trace):
                response = await self.preview(palette_revision, window, trace)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                variant = "traced-overlap" if trace.get("solid_underlayers") else "traced-edge"
                self.assertEqual(body["variant"], variant)
                self.assertEqual(body["window"], window)
                self.assertEqual(body["crop"], {"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
                self.assertEqual(body["image"], {"width": WIDTH, "height": HEIGHT})
                svg = ET.fromstring(body["svg"])
                self.assertEqual(svg.attrib["viewBox"], f"10 8 {WIDTH - 20} {HEIGHT - 16}")
                trace_revision = await self.run_job(
                    "trace", project_id, palette_revision, trace=trace)
                artifacts = (await self.client.get(
                    f"/api/revisions/{trace_revision}/artifacts")).json()
                traced_svg = next(row for row in artifacts if row["kind"] == variant)
                traced = await self.client.get(traced_svg["download_url"])
                self.assertEqual(layer_paths(svg), layer_paths(ET.fromstring(traced.content)))

    async def test_the_preview_infers_layer_order_only_when_it_can_matter(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        window = {"x": 10, "y": 8, "width": WIDTH - 20, "height": HEIGHT - 16}
        palette = (await self.client.get(f"/api/revisions/{palette_revision}/editor")).json()["palette"]
        with mock.patch("vinylgen.trace_window.infer_order",
                        side_effect=AssertionError("infer_order ran")):
            response = await self.preview(palette_revision, window, {})
        self.assertEqual(response.status_code, 200, response.text)
        fills = [layer.attrib["fill"] for layer in ET.fromstring(response.json()["svg"]).iter(f"{{{SVG}}}g")
                 if "fill" in layer.attrib]
        self.assertEqual(fills, [row["hex"] for row in palette])
        # Either setting makes order matter; the whole-image order is inferred once and
        # cached for the loaded segmentation.
        with mock.patch("vinylgen.trace_window.infer_order",
                        wraps=trace_window.infer_order) as spy:
            for trace in ({"solid_underlayers": True}, {"overlap": 0.5}):
                with self.subTest(trace=trace):
                    response = await self.preview(palette_revision, window, trace)
                    self.assertEqual(response.status_code, 200, response.text)
        spy.assert_called_once()

    async def test_window_is_clamped_margined_and_its_boundaries_cached(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        service = self.app.state.trace_previews
        request_window = {"x": 400, "y": -40, "width": 300, "height": 300}
        response = await self.preview(palette_revision, request_window)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["window"], {"x": 400, "y": 0, "width": 220, "height": 260})
        self.assertEqual(body["crop"], {"x": 400 - PREVIEW_MARGIN_PX, "y": 0,
                                        "width": 220 + PREVIEW_MARGIN_PX,
                                        "height": 260 + PREVIEW_MARGIN_PX})
        svg = ET.fromstring(body["svg"])
        self.assertEqual(svg.attrib["viewBox"], "400 0 220 260")
        # Paths are in image px, so nothing in the document moves them.
        self.assertFalse([element for element in svg.iter() if "transform" in element.attrib])
        self.assertEqual(len(service._boundaries.keys()), 1)
        # A settings-only change re-fits the cached boundaries of the same window. On
        # this 620 px fixture the change is coarser, not finer: tighter tolerances
        # would both be held at the 1 px floor.
        retuned = await self.preview(palette_revision, request_window,
                                     {"curve_fit_tolerance": 6, "straight_line_max_deviation": 6,
                                      "max_deviation": 6, "corner_sharpening_allowance": 1,
                                      "length_unit": "per_mille"})
        self.assertEqual(retuned.status_code, 200, retuned.text)
        self.assertNotEqual(retuned.json()["svg"], body["svg"])
        self.assertEqual(len(service._boundaries.keys()), 1)
        self.assertIsNotNone(service._segmentation)
        # Panning traces a new window.
        panned = await self.preview(palette_revision, {"x": 200, "y": 200, "width": 100, "height": 80})
        self.assertEqual(panned.status_code, 200, panned.text)
        self.assertEqual(len(service._boundaries.keys()), 2)

    async def test_preview_uses_the_active_cleanup_and_is_blocked_when_it_is_stale(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        window = {"x": 150, "y": 130, "width": 300, "height": 300}
        before = await self.preview(palette_revision, window)
        self.assertEqual(before.status_code, 200, before.text)
        colours = list(layer_paths(ET.fromstring(before.json()["svg"])))
        self.assertEqual(len(colours), 3)
        removed_hex = re.search(r"\((#[0-9A-F]{6})\)", colours[-1]).group(1)
        cleanup_revision = await self.run_job(
            "cleanup", project_id, palette_revision, cleanup={"remove_colour": removed_hex})
        after = await self.preview(cleanup_revision, window)
        self.assertEqual(after.status_code, 200, after.text)
        remaining = list(layer_paths(ET.fromstring(after.json()["svg"])))
        self.assertEqual(remaining, colours[:-1])
        # A touch-up keeps the cleanup current: the preview still omits its colour.
        touched = await self.client.post(
            f"/api/projects/{project_id}/revisions/{cleanup_revision}/edits",
            json={"operations": [{"tool": "paint_pixels", "target_label": 0,
                                  "pixels": [[1, 1]]}]})
        self.assertEqual(touched.status_code, 201, touched.text)
        kept = await self.preview(touched.json()["id"], window)
        self.assertEqual(kept.status_code, 200, kept.text)
        self.assertEqual(list(layer_paths(ET.fromstring(kept.json()["svg"]))), colours[:-1])
        # A merge makes it stale.
        edited = await self.client.post(
            f"/api/projects/{project_id}/revisions/{touched.json()['id']}/edits",
            json={"operations": [{"tool": "merge_colour", "source_label": 0,
                                  "target_label": 1}]})
        self.assertEqual(edited.status_code, 201, edited.text)
        stale = await self.preview(edited.json()["id"], window)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertIn("re-run Cleanup", stale.text)

    async def test_request_renames_swapping_names_still_omit_the_removed_colour(self) -> None:
        # The cleanup removes a colour by name; the request then swaps that name with
        # another colour's. The preview omits the colour the cleanup removed, as the
        # trace run does, never the one that now carries its name.
        project_id, _, palette_revision = await self.flattened_project()
        window = {"x": 150, "y": 130, "width": 300, "height": 300}
        before = await self.preview(palette_revision, window)
        named = [re.fullmatch(r"(.*) \((#[0-9A-F]{6})\)", label).groups()
                 for label in layer_paths(ET.fromstring(before.json()["svg"]))]
        (removed_name, removed_hex), (kept_name, kept_hex) = named[-1], named[0]
        cleanup_revision = await self.run_job(
            "cleanup", project_id, palette_revision, cleanup={"remove_colour": removed_name})
        swapped = await self.preview(cleanup_revision, window, colour_names={
            removed_hex: kept_name, kept_hex: removed_name})
        self.assertEqual(swapped.status_code, 200, swapped.text)
        layers = list(layer_paths(ET.fromstring(swapped.json()["svg"])))
        self.assertEqual(len(layers), len(named) - 1)
        self.assertFalse(any(removed_hex in label for label in layers), layers)
        self.assertIn(f"{removed_name} ({kept_hex})", layers)

    async def test_request_validation_and_missing_resources(self) -> None:
        _, source_revision, palette_revision = await self.flattened_project()
        window = {"x": 0, "y": 0, "width": 100, "height": 100}
        self.assertEqual((await self.preview("missing", window)).status_code, 404)
        unflattened = await self.preview(source_revision, window)
        self.assertEqual(unflattened.status_code, 409, unflattened.text)
        outside = await self.preview(palette_revision, {"x": WIDTH, "y": 0, "width": 10, "height": 10})
        self.assertEqual(outside.status_code, 422, outside.text)
        too_large = await self.preview(palette_revision, {"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
        self.assertEqual(too_large.status_code, 422, too_large.text)
        self.assertIn("zoom in", too_large.text)
        empty = await self.preview(palette_revision, {"x": 0, "y": 0, "width": 0, "height": 10})
        self.assertEqual(empty.status_code, 422)
        cleanup_field = await self.preview(palette_revision, window, {"width_mm": 100})
        self.assertEqual(cleanup_field.status_code, 422)
        bad_order = await self.preview(palette_revision, window, {"layer_order": ["Nonexistent"]})
        self.assertEqual(bad_order.status_code, 422, bad_order.text)
        bad_setting = await self.preview(palette_revision, window,
                                         {"curve_fit_tolerance": 0, "length_unit": "per_mille"})
        self.assertEqual(bad_setting.status_code, 422)
        # A client still sending pixel lengths names no unit and is refused, never
        # read as thousandths.
        pixel_client = await self.preview(palette_revision, window, {"curve_fit_tolerance": 1.5})
        self.assertEqual(pixel_client.status_code, 422)
        self.assertIn("length_unit", pixel_client.text)

    async def test_a_superseded_request_stops_and_the_kernels_warm_off_the_loop(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        service = self.app.state.trace_previews
        await asyncio.wrap_future(service.warm_up())
        request = TracePreviewRequest(window={"x": 0, "y": 0, "width": 50, "height": 50})
        with self.assertRaises(PreviewCancelled):
            service.run(palette_revision, request, cancelled=lambda: True)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(PreviewCancelled):
            await service.preview(palette_revision, request, cancelled)
        self.assertEqual(service._boundaries.keys(), [])

    async def test_the_segmentation_is_released_after_an_idle_period(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        service = self.app.state.trace_previews
        now = [1000.0]
        service._clock = lambda: now[0]
        window = {"x": 100, "y": 100, "width": 200, "height": 200}
        self.assertEqual((await self.preview(palette_revision, window)).status_code, 200)
        self.assertIsNotNone(service._segmentation)
        now[0] += trace_preview.SEGMENTATION_IDLE_SECONDS - 1
        self.assertFalse(service.release_if_idle())
        self.assertIsNotNone(service._segmentation)
        now[0] += 1
        self.assertTrue(service.release_if_idle())
        self.assertIsNone(service._segmentation)
        self.assertEqual(service._boundaries.keys(), [])
        # The next preview simply loads it again.
        self.assertEqual((await self.preview(palette_revision, window)).status_code, 200)
        self.assertIsNotNone(service._segmentation)
        # The release is scheduled on its own after a preview: with a short idle
        # period and the real clock, the process lets go of it unprompted.
        service._clock = time.monotonic
        service._idle_seconds = 0.05
        self.assertEqual((await self.preview(palette_revision, window)).status_code, 200)
        for _ in range(1000):
            if service._segmentation is None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNone(service._segmentation)

    async def test_a_request_superseded_during_its_load_stops_before_tracing(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        service = self.app.state.trace_previews
        calls = []

        def cancelled() -> bool:
            calls.append(1)
            return len(calls) > 1  # still current when it starts, superseded thereafter

        request = TracePreviewRequest(window={"x": 0, "y": 0, "width": 50, "height": 50})
        with self.assertRaises(PreviewCancelled):
            service.run(palette_revision, request, cancelled)
        self.assertEqual(len(calls), 2)  # it stopped at the checkpoint inside the load
        self.assertIsNone(service._segmentation)
        self.assertEqual(service._boundaries.keys(), [])

    async def test_a_palette_whose_colours_differ_from_the_segmentation_is_refused(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        artifacts = (await self.client.get(
            f"/api/revisions/{palette_revision}/effective-artifacts")).json()
        palette_row = next(row for row in artifacts if row["kind"] == "palette")
        report = (await self.client.get(palette_row["download_url"])).json()
        report["palette"][0]["rgb"] = [(value + 1) % 256 for value in report["palette"][0]["rgb"]]
        child = await self.client.post(f"/api/projects/{project_id}/revisions", json={
            "parent_revision_id": palette_revision, "kind": "settings"})
        self.assertEqual(child.status_code, 201, child.text)
        uploaded = await self.client.post(
            f"/api/revisions/{child.json()['id']}/artifacts", data={"kind": "palette"},
            files={"file": ("palette.json", json.dumps(report).encode(), "application/json")})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        response = await self.preview(child.json()["id"], {"x": 0, "y": 0, "width": 100, "height": 100})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("does not match palette.json", response.text)

    async def test_a_fault_inside_tracing_is_a_500_not_a_missing_resource(self) -> None:
        _, _, palette_revision = await self.flattened_project()

        def broken(*args, **kwargs):
            raise KeyError("chain-identity")

        with mock.patch.object(trace_preview, "window_boundaries", broken):
            response = await self.preview(palette_revision, {"x": 0, "y": 0, "width": 100, "height": 100})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIn("preview failed", response.text)

    async def palette_child(self, project_id: str, parent: str, content: bytes) -> str:
        child = await self.client.post(f"/api/projects/{project_id}/revisions", json={
            "parent_revision_id": parent, "kind": "settings"})
        self.assertEqual(child.status_code, 201, child.text)
        uploaded = await self.client.post(
            f"/api/revisions/{child.json()['id']}/artifacts", data={"kind": "palette"},
            files={"file": ("palette.json", content, "application/json")})
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        return child.json()["id"]

    async def test_a_corrupt_stored_palette_is_a_server_fault(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        window = {"x": 0, "y": 0, "width": 100, "height": 100}
        for content in (b"{not json", b'{"palette": [{"rgb": [1, 2]}]}',
                        b'{"palette": [{"hex": "#FF0000", "rgb": [300, 0, 0]}]}',
                        b'{"palette": [{"hex": "#FF0000", "rgb": [-1, 0, 0]}]}',
                        b'{"palette": [{"hex": "#FF0000", "rgb": [254.6, 0, 0]}]}',
                        b'{"palette": [{"hex": "#FF0000", "rgb": [true, 0, 0]}]}',
                        b'{"palette": []}'):
            with self.subTest(content=content):
                child = await self.palette_child(project_id, palette_revision, content)
                response = await self.preview(child, window)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertIn("palette.json is unreadable", response.text)

    async def test_shutdown_stops_the_running_preview_and_refuses_new_ones(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        service = self.app.state.trace_previews
        started = threading.Event()
        real = trace_preview.window_boundaries

        def slow_build(segmentation, included, crop, checkpoint=None):
            # A long boundary build that honours its checkpoint, as the real one does.
            started.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                checkpoint()
                time.sleep(0.01)
            return real(segmentation, included, crop, checkpoint)

        with mock.patch.object(trace_preview, "window_boundaries", slow_build):
            running = asyncio.ensure_future(self.preview(
                palette_revision, {"x": 0, "y": 0, "width": 100, "height": 100}))
            self.assertTrue(await asyncio.to_thread(started.wait, 30))
            # A second request waits in the queue behind the running one.
            queued = asyncio.ensure_future(self.preview(
                palette_revision, {"x": 200, "y": 200, "width": 100, "height": 100}))
            await asyncio.sleep(0.2)
            stopped_at = time.monotonic()
            service.shutdown()
            response = await running
            queued_response = await queued
        self.assertLess(time.monotonic() - stopped_at, 5.0)
        for stopped in (response, queued_response):
            self.assertEqual(stopped.status_code, 503, stopped.text)
            self.assertIn("server shutting down", stopped.text)
        after = await self.preview(palette_revision, {"x": 0, "y": 0, "width": 100, "height": 100})
        self.assertEqual(after.status_code, 503, after.text)
        self.assertIsNone(service._executor)
        with self.assertRaises(trace_preview.PreviewShutDown):
            service.warm_up()

    def test_shutdown_stops_a_running_warm_up(self) -> None:
        service = TracePreviewService(store=None)
        started = threading.Event()

        def slow_build(segmentation, included, crop, checkpoint=None):
            started.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                checkpoint()
                time.sleep(0.01)
            self.fail("the warm-up ignored shutdown")

        with mock.patch.object(trace_preview, "window_boundaries", slow_build):
            warm = service.warm_up()
            self.assertTrue(started.wait(30))
            service.shutdown()
            self.assertIsInstance(warm.exception(timeout=2), trace_preview.PreviewShutDown)

    async def test_a_request_racing_shutdown_to_the_pool_is_a_503(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        service = self.app.state.trace_previews
        closed_pool = service._pool()
        closed_pool.shutdown(wait=True)  # shut between the pool lookup and the submit
        with mock.patch.object(service, "_pool", return_value=closed_pool):
            response = await self.preview(palette_revision, {"x": 0, "y": 0, "width": 50, "height": 50})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("server shutting down", response.text)

    async def test_a_legacy_unnamed_palette_takes_generated_names_and_request_renames(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        window = {"x": 150, "y": 130, "width": 300, "height": 300}
        named = await self.preview(palette_revision, window)
        labels = list(layer_paths(ET.fromstring(named.json()["svg"])))
        removed_name, removed_hex = re.fullmatch(r"(.*) \((#[0-9A-F]{6})\)", labels[-1]).groups()
        cleanup_revision = await self.run_job(
            "cleanup", project_id, palette_revision, cleanup={"remove_colour": removed_name})
        artifacts = (await self.client.get(
            f"/api/revisions/{cleanup_revision}/effective-artifacts")).json()
        palette_row = next(row for row in artifacts if row["kind"] == "palette")
        report = (await self.client.get(palette_row["download_url"])).json()
        for row in report["palette"]:
            row.pop("name", None)
        legacy = await self.palette_child(project_id, cleanup_revision, json.dumps(report).encode())
        plain = await self.preview(legacy, window)
        self.assertEqual(plain.status_code, 200, plain.text)
        self.assertEqual(list(layer_paths(ET.fromstring(plain.json()["svg"]))), labels[:-1])
        # Renaming the removed colour in the request does not un-remove it: the cleanup's
        # removed colour is its palette index, as a trace run with these renames omits it.
        renamed = await self.preview(legacy, window, colour_names={removed_hex: "Renamed"})
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(list(layer_paths(ET.fromstring(renamed.json()["svg"]))), labels[:-1])


class SegmentationResidencyTest(unittest.TestCase):
    def segmentation(self) -> PreviewSegmentation:
        labels = np.zeros((4, 4), dtype=np.int16)
        return PreviewSegmentation(labels=labels, protected_features=labels.astype(bool),
                                   palette_rgb=np.zeros((1, 3), dtype=np.uint8),
                                   palette_costs=np.zeros((4, 4, 1), dtype=np.float16))

    def test_the_resident_segmentation_is_evicted_before_the_next_loads(self) -> None:
        service = TracePreviewService(store=None)
        first = service._segmentation_for("a", self.segmentation)
        self.assertIs(service._segmentation_for("a", lambda: self.fail("reloaded")), first)
        service._boundaries.get("window", lambda: object())

        def load_second() -> PreviewSegmentation:
            # Nothing of the first segmentation is held while the second decompresses.
            self.assertIsNone(service._segmentation)
            self.assertEqual(service._boundaries.keys(), [])
            return self.segmentation()

        second = service._segmentation_for("b", load_second)
        self.assertEqual(service._segmentation, ("b", second))

    def test_release_waits_for_the_idle_period_on_the_injected_clock(self) -> None:
        now = [50.0]
        service = TracePreviewService(store=None, clock=lambda: now[0], idle_seconds=600)
        service._segmentation_for("a", self.segmentation)
        now[0] = 649.0
        self.assertFalse(service.release_if_idle())
        now[0] = 650.0
        self.assertTrue(service.release_if_idle())
        self.assertIsNone(service._segmentation)


class TracePreviewStartupTest(unittest.TestCase):
    def test_startup_warms_the_preview_thread_without_holding_up_health(self) -> None:
        with tempfile.TemporaryDirectory(dir=".tmp") as name:
            app = create_app(WebConfig(Path(name)))
            with TestClient(app) as client:
                # Startup has returned (the client is serving) with the warm-up
                # merely submitted to the preview thread.
                self.assertEqual(client.get("/api/health").status_code, 200)
                warm = app.state.trace_previews.warm_future
                self.assertIsNotNone(warm)
                warm.result(timeout=120)


if __name__ == "__main__":
    unittest.main()
