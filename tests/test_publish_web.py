"""A build never changes the page the service serves; only a publish (``scripts/publish_web.py``, run by
``just deploy``) does, by an atomic swap of the served symlink ``web/live``.

Driven through the app's own frontend route on a ``WebConfig`` whose ``frontend_dir`` is a scratch
``web/live``: a build writes ``web/dist`` and the served page is unchanged; a publish swaps ``web/live`` to
a copy of the build and the next request gets the new page; a build after it changes ``web/dist`` and
leaves the served copy alone. A publish keeps the release it replaced and prunes older ones, and refuses
(changing nothing) a build without ``index.html`` and a ``web/live`` that is not a symlink."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from vinylgen.web.app import create_app
from vinylgen.web.config import LIVE_FRONTEND, WebConfig

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("publish_web", ROOT / "scripts" / "publish_web.py")
publisher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(publisher)


def build(dist: Path, text: str) -> None:
    """What a web build does to ``web/dist``: its page rewritten."""
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(f"<!doctype html><title>{text}</title>")
    (dist / "assets" / "app.js").write_text(f"console.log({text!r})")


def at(second: int) -> datetime:
    return datetime(2026, 11, 8, 23, 0, second, tzinfo=timezone.utc)


class PublishWebTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.addCleanup(self.temp.cleanup)
        self.web = Path(self.temp.name) / "web"
        self.dist = self.web / "dist"
        self.live = self.web / "live"
        build(self.dist, "first")
        publisher.publish(self.dist, self.web, at(1))
        self.app = create_app(WebConfig(Path(self.temp.name) / "data", frontend_dir=self.live))

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()

    async def page(self) -> tuple[str, str]:
        index = await self.client.get("/")
        script = await self.client.get("/assets/app.js")
        self.assertEqual(index.status_code, 200)
        self.assertEqual(script.status_code, 200)
        return index.text, script.text

    def test_the_service_serves_web_live_by_default(self) -> None:
        self.assertEqual(LIVE_FRONTEND, ROOT / "web" / "live")
        self.assertEqual(WebConfig(Path(self.temp.name)).frontend_dir, LIVE_FRONTEND)

    async def test_a_build_leaves_the_served_page_unchanged_and_a_publish_changes_it(self) -> None:
        self.assertIn("first", (await self.page())[0])
        build(self.dist, "second")
        self.assertEqual(await self.page(), ("<!doctype html><title>first</title>", "console.log('first')"),
                         "a build does not change the served page")
        publisher.publish(self.dist, self.web, at(2))
        self.assertEqual(await self.page(), ("<!doctype html><title>second</title>", "console.log('second')"),
                         "the publish does")
        build(self.dist, "third")
        self.assertIn("second", (await self.page())[0], "a build after it does not")

    def test_the_swap_is_one_symlink_renamed_over_the_old(self) -> None:
        self.assertTrue(self.live.is_symlink())
        self.assertEqual(os.readlink(self.live), "releases/20261108-230001-000000")
        build(self.dist, "second")
        publisher.publish(self.dist, self.web, at(2))
        self.assertEqual(os.readlink(self.live), "releases/20261108-230002-000000")
        self.assertFalse((self.web / ".live.new").exists(), "no staged link is left")

    def test_a_publish_keeps_the_release_it_replaced_and_prunes_older_ones(self) -> None:
        stray = self.web / "releases" / "notes"
        stray.mkdir()
        for second in (2, 3, 4):
            build(self.dist, f"build {second}")
            publisher.publish(self.dist, self.web, at(second))
        names = sorted(p.name for p in (self.web / "releases").iterdir())
        self.assertEqual(names, ["20261108-230003-000000", "20261108-230004-000000", "notes"])

    def test_a_build_without_an_index_is_refused_and_nothing_changes(self) -> None:
        empty = Path(self.temp.name) / "empty"
        empty.mkdir()
        with self.assertRaises(SystemExit):
            publisher.publish(empty, self.web, at(2))
        self.assertEqual(os.readlink(self.live), "releases/20261108-230001-000000")
        self.assertEqual(len(list((self.web / "releases").iterdir())), 1)

    def test_a_live_directory_that_is_no_symlink_is_refused(self) -> None:
        web = Path(self.temp.name) / "other"
        (web / "live").mkdir(parents=True)
        with self.assertRaises(SystemExit):
            publisher.publish(self.dist, web, at(2))
        self.assertFalse((web / "releases").exists() and any((web / "releases").iterdir()))


if __name__ == "__main__":
    unittest.main()
