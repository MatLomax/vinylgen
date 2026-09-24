from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
import numpy as np
from PIL import Image

from vinylgen import cleanup_window
from vinylgen.cutter_cleanup import CLEANUP_ALGORITHM_VERSION, measure_colour_regions
from vinylgen.trace_window import PreviewCancelled
from vinylgen.web import trace_preview
from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig
from vinylgen.web.trace_preview import (CleanupPreviewRequest, REMOVED_RGBA, RETURNED_RGBA,
                                        WIDENED_RGBA)

WIDTH, HEIGHT = 620, 610
# The visible window the tests preview; its crop (plus the 256 px margin) is
# x 0..356, y 0..606.
WINDOW = {"x": 0, "y": 250, "width": 100, "height": 100}


def source_png() -> bytes:
    """A disc on a background with undersized squares and a long bar crossing the
    window: the bar's part inside the crop is under 2000 px, the whole bar is not. A
    corner block keeps the bar's colour over the palette's usage cutoff."""
    pixels = np.full((HEIGHT, WIDTH, 3), (15, 35, 60), dtype=np.uint8)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    pixels[(xx - 300) ** 2 + (yy - 330) ** 2 < 90 ** 2] = (225, 95, 45)
    for x, y in ((220, 260), (380, 420), (300, 330), (250, 400), (50, 320)):
        pixels[y:y + 6, x:x + 6] = (240, 225, 200)
    pixels[300:305, 40:600] = (240, 225, 200)
    pixels[0:80, 0:80] = (240, 225, 200)
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def stroke_source_png() -> bytes:
    """A three-pixel Bone stroke crossing the window on the Night background, whose far
    end touches a large Ember block beyond the preview's crop. Ember is the stroke's
    most similar touching colour, so a full widening grows it into Ember near the
    block only; the crop never sees the block and widens it into Night instead."""
    pixels = np.full((HEIGHT, WIDTH, 3), (15, 35, 60), dtype=np.uint8)
    pixels[300:303, 20:400] = (240, 225, 200)
    pixels[250:350, 400:600] = (225, 95, 45)
    pixels[0:80, 0:80] = (240, 225, 200)
    buffer = io.BytesIO()
    Image.fromarray(pixels, "RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def decode(url: str) -> np.ndarray:
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix)
    return np.asarray(Image.open(io.BytesIO(base64.b64decode(url[len(prefix):]))))


class CleanupPreviewApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.app = create_app(WebConfig(Path(self.temp.name)))
        self.service = self.app.state.trace_previews

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.service.shutdown()
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

    async def flattened_project(self, source: bytes | None = None) -> tuple[str, str, str]:
        imported = await self.client.post(
            "/api/projects/import", data={"name": "Cleanup Preview Fixture"},
            files={"source": ("fixture.png", source or source_png(), "image/png")})
        self.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        source_revision = project["revisions"][0]["id"]
        palette_revision = await self.run_job("palette", project["id"], source_revision)
        return project["id"], source_revision, palette_revision

    async def preview(self, revision_id: str, window: dict, cleanup: dict | None = None,
                      **extra) -> httpx.Response:
        return await self.client.post(f"/api/revisions/{revision_id}/cleanup-preview", json={
            "window": window, **({"cleanup": cleanup} if cleanup is not None else {}), **extra})

    async def flattened_pixels(self, revision_id: str) -> np.ndarray:
        artifacts = (await self.client.get(
            f"/api/revisions/{revision_id}/effective-artifacts")).json()
        row = next(row for row in artifacts if row["kind"] == "flattened")
        return np.asarray(Image.open(io.BytesIO((await self.client.get(row["download_url"])).content)))

    def window_of(self, array: np.ndarray, window: dict = WINDOW) -> np.ndarray:
        return array[window["y"]:window["y"] + window["height"],
                     window["x"]:window["x"] + window["width"]]

    async def test_preview_matches_the_cleanup_run_it_previews(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        uncleaned = await self.flattened_pixels(palette_revision)
        # 0.5 mm per px: 500 mm2 is 2000 px, over the squares and the bar's part inside
        # the crop, under the whole bar.
        cleanup = {"width_mm": WIDTH / 2, "cleanup": True, "cleanup_action": "remove",
                   "min_region_area": 500.0}
        response = await self.preview(palette_revision, WINDOW, cleanup)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["window"], WINDOW)
        self.assertEqual(body["crop"], {"x": 0, "y": 0, "width": 356, "height": 606})
        self.assertEqual(body["image"], {"width": WIDTH, "height": HEIGHT})
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision,
                                              cleanup=cleanup)
        cleaned = await self.flattened_pixels(cleanup_revision)
        pixels = decode(body["pixels"])
        np.testing.assert_array_equal(pixels, self.window_of(cleaned))
        changed = self.window_of((cleaned != uncleaned).any(axis=2))
        self.assertTrue(changed.any())
        changes = decode(body["changes"])
        self.assertEqual(changes.shape, (100, 100, 4))
        np.testing.assert_array_equal(changes[changed], np.tile(REMOVED_RGBA, (int(changed.sum()), 1)))
        self.assertFalse(changes[~changed].any())
        self.assertEqual(body["removed_pixels"], int(changed.sum()))
        self.assertEqual(body["widened_pixels"], 0)
        # The bar survives: a full run judges it by its whole area.
        np.testing.assert_array_equal(pixels[52, :], self.window_of(uncleaned)[52, :])
        self.assertTrue((pixels[52, 40:, :] != pixels[40, 40:, :]).any(axis=1).all())
        # On the cleanup revision the changes are judged against the cleaned raster it
        # shows: the same settings would change nothing.
        same = await self.preview(cleanup_revision, WINDOW, cleanup)
        self.assertEqual(same.status_code, 200, same.text)
        np.testing.assert_array_equal(decode(same.json()["pixels"]), self.window_of(cleaned))
        self.assertFalse(decode(same.json()["changes"]).any())
        self.assertEqual([same.json()[key] for key in ("removed_pixels", "widened_pixels",
                                                       "returned_pixels")], [0, 0, 0])
        # The preview still cleans the uncleaned raster, so turning cleanup off shows
        # it, and every pixel the stored cleanup changed would return (blue).
        off = await self.preview(cleanup_revision, WINDOW, {"cleanup": False})
        self.assertEqual(off.status_code, 200, off.text)
        np.testing.assert_array_equal(decode(off.json()["pixels"]), self.window_of(uncleaned))
        returned = decode(off.json()["changes"])
        np.testing.assert_array_equal(returned[changed], np.tile(RETURNED_RGBA, (int(changed.sum()), 1)))
        self.assertFalse(returned[~changed].any())
        self.assertEqual(off.json()["returned_pixels"], int(changed.sum()))

    async def test_the_settings_of_the_shown_cleanup_change_nothing_even_where_widening_is_approximate(self) -> None:
        project_id, _, palette_revision = await self.flattened_project(stroke_source_png())
        widen = {"cleanup": True, "cleanup_action": "widen", "min_detail_width": 8.0}
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision, cleanup=widen)
        cleaned = await self.flattened_pixels(cleanup_revision)
        # From the uncleaned raster the windowed widening differs from the full run here.
        approximate = await self.preview(palette_revision, WINDOW, widen)
        self.assertEqual(approximate.status_code, 200, approximate.text)
        self.assertTrue((decode(approximate.json()["pixels"]) != self.window_of(cleaned)).any())
        # On the cleanup it previews, the answer is the shown raster: nothing would change.
        with mock.patch.object(trace_preview, "clean_window", wraps=cleanup_window.clean_window) as clean:
            same = await self.preview(cleanup_revision, WINDOW, widen)
        self.assertEqual(same.status_code, 200, same.text)
        np.testing.assert_array_equal(decode(same.json()["pixels"]), self.window_of(cleaned))
        self.assertFalse(decode(same.json()["changes"]).any())
        self.assertEqual(clean.call_count, 0)
        # Any other setting runs the windowed clean again.
        wider = await self.preview(cleanup_revision, WINDOW, {**widen, "min_detail_width": 9.0})
        self.assertEqual(wider.status_code, 200, wider.text)
        self.assertTrue(decode(wider.json()["changes"]).any())

    async def test_a_cleanup_recorded_under_older_cleanup_rules_previews_with_the_clean(self) -> None:
        project_id, _, palette_revision = await self.flattened_project(stroke_source_png())
        widen = {"cleanup": True, "cleanup_action": "widen", "min_detail_width": 8.0}
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision, cleanup=widen)
        store = self.service.store
        settings = store.get_revision(cleanup_revision)["settings"]
        self.assertEqual(settings["cleanup_algorithm_version"], CLEANUP_ALGORITHM_VERSION)
        # Recorded under the current rules, its own settings take the shortcut.
        with mock.patch.object(trace_preview, "clean_window", wraps=cleanup_window.clean_window) as clean:
            current = await self.preview(cleanup_revision, WINDOW, widen)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(clean.call_count, 0)
        # A cleanup recorded without a version ran under version 1: a re-run could now
        # differ, so the preview runs the clean.
        del settings["cleanup_algorithm_version"]
        with store.connection() as db:
            db.execute("UPDATE revisions SET settings_json=? WHERE id=?",
                       (json.dumps(settings), cleanup_revision))
        with mock.patch.object(trace_preview, "clean_window", wraps=cleanup_window.clean_window) as clean:
            older = await self.preview(cleanup_revision, WINDOW, widen)
        self.assertEqual(older.status_code, 200, older.text)
        self.assertEqual(clean.call_count, 1)
        self.assertTrue(decode(older.json()["changes"]).any())

    async def palette_rows(self, revision_id: str) -> list[dict]:
        artifacts = (await self.client.get(
            f"/api/revisions/{revision_id}/effective-artifacts")).json()
        row = next(row for row in artifacts if row["kind"] == "palette")
        return (await self.client.get(row["download_url"])).json()["palette"]

    async def test_a_colour_renamed_after_the_cleanup_is_not_the_same_removed_colour(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        rows = await self.palette_rows(palette_revision)
        bone = next(row for row in rows if row["rgb"][0] > 200 and row["rgb"][2] > 150)
        ember = next(row for row in rows if row["rgb"][0] > 200 and row["rgb"][2] < 100)
        settings = {"cleanup": True, "min_region_area": 3000.0, "remove_colour": bone["name"]}
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision, cleanup=settings)
        # The trace swaps the two colours' names, so the same name now means Ember.
        trace_revision = await self.run_job(
            "trace", project_id, cleanup_revision,
            colour_names={bone["hex"]: ember["name"], ember["hex"]: bone["name"]})
        renamed = {row["hex"]: row["name"] for row in await self.palette_rows(trace_revision)}
        self.assertEqual(renamed[ember["hex"]], bone["name"])
        with mock.patch.object(trace_preview, "clean_window", wraps=cleanup_window.clean_window) as clean:
            body = (await self.preview(trace_revision, WINDOW, settings)).json()
        self.assertEqual(clean.call_count, 1)
        # It previews what re-running Cleanup from the trace would do: remove Ember
        # instead of Bone, so the Bone square and bar in the window go.
        rerun = await self.run_job("cleanup", project_id, trace_revision, cleanup=settings)
        rerun_pixels = self.window_of(await self.flattened_pixels(rerun))
        shown = self.window_of(await self.flattened_pixels(trace_revision))
        self.assertTrue((rerun_pixels != shown).any())
        np.testing.assert_array_equal(decode(body["pixels"]), rerun_pixels)
        self.assertGreater(body["removed_pixels"], 0)

    async def test_a_size_given_by_the_other_dimension_is_the_same_size(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        # 300 mm wide makes 620 x 610 px 295.16129032258067 mm high, which gives back a
        # width of 300.00000000000006 mm.
        recorded = {"width_mm": 300.0, "cleanup": True, "min_region_area": 100.0}
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision, cleanup=recorded)
        by_height = {"height_mm": 300.0 * HEIGHT / WIDTH, "cleanup": True, "min_region_area": 100.0}
        self.assertNotEqual(by_height["height_mm"] * WIDTH / HEIGHT, 300.0)
        with mock.patch.object(trace_preview, "clean_window", wraps=cleanup_window.clean_window) as clean:
            body = (await self.preview(cleanup_revision, WINDOW, by_height)).json()
        self.assertEqual(clean.call_count, 0)
        self.assertFalse(decode(body["changes"]).any())

    async def test_a_trace_of_the_cleanup_compares_against_the_cleaned_raster(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        uncleaned = await self.flattened_pixels(palette_revision)
        removing = {"cleanup": True, "min_region_area": 3000.0}
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision, cleanup=removing)
        trace_revision = await self.run_job("trace", project_id, cleanup_revision)
        cleaned = await self.flattened_pixels(trace_revision)
        changed = self.window_of((cleaned != uncleaned).any(axis=2))
        self.assertTrue(changed.any())
        same = await self.preview(trace_revision, WINDOW, removing)
        self.assertFalse(decode(same.json()["changes"]).any())
        off = (await self.preview(trace_revision, WINDOW, {"cleanup": False})).json()
        self.assertEqual(off["returned_pixels"], int(changed.sum()))
        np.testing.assert_array_equal((decode(off["changes"]) == RETURNED_RGBA).all(axis=2), changed)

    async def test_after_an_edit_makes_the_cleanup_stale_the_edited_raster_is_the_reference(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        uncleaned = await self.flattened_pixels(palette_revision)
        removing = {"cleanup": True, "min_region_area": 3000.0}
        cleanup_revision = await self.run_job("cleanup", project_id, palette_revision, cleanup=removing)
        # A cleaned raster is loaded as the shown one first.
        self.assertEqual((await self.preview(cleanup_revision, WINDOW, {"cleanup": False})).status_code, 200)
        self.assertIsNotNone(self.service._cleanup_source[1]._current)
        edited = await self.client.post(
            f"/api/projects/{project_id}/revisions/{cleanup_revision}/edits",
            json={"operations": [{"tool": "paint_pixels", "target_label": 0, "pixels": [[600, 5]]}]})
        self.assertEqual(edited.status_code, 201, edited.text)
        body = (await self.preview(edited.json()["id"], WINDOW, removing)).json()
        # The edit shows the uncleaned raster, so the same settings would remove again,
        # nothing would return, and the cleaned raster is no longer held.
        cleaned = await self.flattened_pixels(cleanup_revision)
        changed = self.window_of((cleaned != uncleaned).any(axis=2))
        self.assertEqual(body["removed_pixels"], int(changed.sum()))
        self.assertEqual(body["returned_pixels"], 0)
        self.assertIsNone(self.service._cleanup_source[1]._current)

    async def test_two_cleanups_of_one_raster_each_compare_against_their_own_result(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        uncleaned = await self.flattened_pixels(palette_revision)
        squares = await self.run_job("cleanup", project_id, palette_revision,
                                     cleanup={"cleanup": True, "min_region_area": 50.0})
        bar_too = await self.run_job("cleanup", project_id, palette_revision,
                                     cleanup={"cleanup": True, "min_region_area": 3000.0})
        results = []
        for revision in (squares, bar_too, squares):
            changed = self.window_of(((await self.flattened_pixels(revision)) != uncleaned).any(axis=2))
            body = (await self.preview(revision, WINDOW, {"cleanup": False})).json()
            np.testing.assert_array_equal((decode(body["changes"]) == RETURNED_RGBA).all(axis=2), changed)
            results.append(int(changed.sum()))
        self.assertLess(results[0], results[1])
        # The uncleaned revision shows the held raster itself: the cleaned one is dropped.
        self.assertIsNotNone(self.service._cleanup_source[1]._current)
        self.assertEqual((await self.preview(palette_revision, WINDOW)).status_code, 200)
        self.assertIsNone(self.service._cleanup_source[1]._current)

    async def test_widened_pixels_are_green(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        response = await self.preview(palette_revision, WINDOW, {
            "cleanup": True, "cleanup_action": "widen", "min_detail_width": 9.0})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        changes = decode(body["changes"])
        widened = (changes == WIDENED_RGBA).all(axis=2)
        self.assertGreater(body["widened_pixels"], 0)
        self.assertEqual(int(widened.sum()), body["widened_pixels"])
        self.assertFalse((changes[~widened] != 0).any())

    async def test_measurements_are_cached_across_settings_and_windows(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        with mock.patch.object(cleanup_window, "measure_colour_regions",
                               wraps=measure_colour_regions) as measure:
            for cleanup in ({"cleanup": True, "min_region_area": 20.0},
                            {"cleanup": True, "min_region_area": 2000.0, "width_mm": 100.0},
                            {"cleanup": True, "cleanup_action": "remove_then_widen",
                             "min_detail_width": 3.0}):
                response = await self.preview(palette_revision, WINDOW, cleanup)
                self.assertEqual(response.status_code, 200, response.text)
            panned = await self.preview(palette_revision, {"x": 0, "y": 0, "width": 150, "height": 150},
                                        {"cleanup": True, "min_region_area": 20.0})
            self.assertEqual(panned.status_code, 200, panned.text)
        colours = len(self.service._cleanup_source[1].palette_rgb)
        self.assertEqual(measure.call_count, colours)

    async def test_request_validation_and_missing_resources(self) -> None:
        _, source_revision, palette_revision = await self.flattened_project()
        window = {"x": 0, "y": 0, "width": 100, "height": 100}
        missing = await self.preview("missing", window)
        self.assertEqual(missing.status_code, 404, missing.text)
        unflattened = await self.preview(source_revision, window)
        self.assertEqual(unflattened.status_code, 409, unflattened.text)
        self.assertIn("no flattened artwork", unflattened.json()["detail"])
        outside = await self.preview(palette_revision, {"x": WIDTH, "y": 0, "width": 10, "height": 10})
        self.assertEqual(outside.status_code, 422, outside.text)
        too_large = await self.preview(palette_revision, {"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT})
        self.assertEqual(too_large.status_code, 422, too_large.text)
        self.assertIn("zoom in to preview", too_large.json()["detail"])
        unknown_colour = await self.preview(palette_revision, window, {"remove_colour": "Nonexistent"})
        self.assertEqual(unknown_colour.status_code, 422, unknown_colour.text)
        self.assertIsInstance(unknown_colour.json()["detail"], str)
        for bad in ({"min_region_area": -1}, {"width_mm": 0}, {"cleanup_action": "shrink"},
                    {"overlap": 1.0}):
            with self.subTest(cleanup=bad):
                response = await self.preview(palette_revision, window, bad)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIsInstance(response.json()["detail"], list)
        extra = await self.preview(palette_revision, window, trace={})
        self.assertEqual(extra.status_code, 422, extra.text)

    async def test_a_stale_palette_is_a_conflict(self) -> None:
        project_id, _, palette_revision = await self.flattened_project()
        child = await self.client.post(f"/api/projects/{project_id}/revisions", json={
            "parent_revision_id": palette_revision, "kind": "settings",
            "settings": {"stale_concerns": ["palette"]}})
        self.assertEqual(child.status_code, 201, child.text)
        response = await self.preview(child.json()["id"], WINDOW)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("stale", response.json()["detail"])

    async def test_a_superseded_request_stops(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        request = CleanupPreviewRequest(window=WINDOW, cleanup={"cleanup": True})
        with self.assertRaises(PreviewCancelled):
            self.service.run_cleanup(palette_revision, request, cancelled=lambda: True)
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(PreviewCancelled):
            await self.service.cleanup_preview(palette_revision, request, cancelled)
        self.assertIsNone(self.service._cleanup_source)
        # Superseded while measuring the regions: it stops at the next colour, and the
        # colours it finished are kept for the next request.
        calls = []

        def superseded() -> bool:
            # Checks before the work, inside and after the load, then before each
            # colour and after labelling it: the seventh is inside the second colour.
            calls.append(1)
            return len(calls) > 6

        with self.assertRaises(PreviewCancelled):
            self.service.run_cleanup(palette_revision, request, superseded)
        source = self.service._cleanup_source[1]
        self.assertFalse(source.measured)
        self.assertEqual(len(source._stats), 1)
        with mock.patch.object(cleanup_window, "measure_colour_regions",
                               wraps=measure_colour_regions) as measure:
            self.service.run_cleanup(palette_revision, request)
        self.assertEqual(measure.call_count, len(source.palette_rgb) - 1)
        self.assertTrue(source.measured)

    async def test_the_route_reports_a_superseded_request_as_a_conflict(self) -> None:
        _, _, palette_revision = await self.flattened_project()

        def superseded(*args, **kwargs):
            raise PreviewCancelled

        with mock.patch.object(trace_preview, "clean_window", superseded):
            response = await self.preview(palette_revision, WINDOW, {"cleanup": True})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("superseded", response.json()["detail"])

    async def test_a_fault_inside_the_cleanup_is_a_500(self) -> None:
        _, _, palette_revision = await self.flattened_project()

        def broken(*args, **kwargs):
            raise KeyError("region")

        with mock.patch.object(trace_preview, "clean_window", broken):
            response = await self.preview(palette_revision, WINDOW, {"cleanup": True})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIn("preview failed", response.json()["detail"])

    async def test_one_raster_is_resident_and_it_is_released_when_idle(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        now = [1000.0]
        self.service._clock = lambda: now[0]
        cleaned = await self.preview(palette_revision, WINDOW, {"cleanup": True})
        self.assertEqual(cleaned.status_code, 200, cleaned.text)
        self.assertIsNotNone(self.service._cleanup_source)
        # A trace preview releases the uncleaned raster before loading its own.
        traced = await self.client.post(f"/api/revisions/{palette_revision}/trace-preview",
                                        json={"window": WINDOW})
        self.assertEqual(traced.status_code, 200, traced.text)
        self.assertIsNone(self.service._cleanup_source)
        self.assertIsNotNone(self.service._segmentation)
        # And the other way round.
        again = await self.preview(palette_revision, WINDOW, {"cleanup": True})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertIsNone(self.service._segmentation)
        self.assertEqual(self.service._boundaries.keys(), [])
        self.assertIsNotNone(self.service._cleanup_source)
        now[0] += trace_preview.SEGMENTATION_IDLE_SECONDS - 1
        self.assertFalse(self.service.release_if_idle())
        now[0] += 1
        self.assertTrue(self.service.release_if_idle())
        self.assertIsNone(self.service._cleanup_source)

    async def test_shutdown_refuses_new_previews(self) -> None:
        _, _, palette_revision = await self.flattened_project()
        self.service.shutdown()
        started = time.monotonic()
        response = await self.preview(palette_revision, WINDOW, {"cleanup": True})
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn("server shutting down", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
