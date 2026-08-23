from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx
import numpy as np
from PIL import Image

from vinylgen.contracts import SegmentationBundle
from vinylgen.web.app import create_app
from vinylgen.web.config import WebConfig


class WebEditorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=".tmp")
        self.app = create_app(WebConfig(Path(self.temp.name)))

    async def asyncSetUp(self) -> None:
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.app.state.jobs.shutdown()
        self.temp.cleanup()

    async def flattened_project(self) -> tuple[dict, str]:
        store = self.app.state.store
        project = store.create_project("Editor Fixture")
        revision = store.create_revision(project["id"], parent_revision_id=None,
                                         kind="palette", message=None, settings={})
        labels = np.zeros((12, 14), dtype=np.int16)
        labels[2:10, 3:11] = 1
        palette = np.array([[12, 25, 40], [225, 90, 45]], dtype=np.uint8)
        bundle = SegmentationBundle(labels, np.zeros_like(labels, dtype=bool),
                                    np.zeros_like(labels, dtype=bool), palette,
                                    np.zeros((*labels.shape, 2), dtype=np.float32))
        segmentation = io.BytesIO()
        np.savez_compressed(segmentation, labels=bundle.labels,
                            protected_features=bundle.protected_features,
                            explicit_protected_features=bundle.explicit_protected_features,
                            palette_rgb=bundle.palette_rgb, palette_costs=bundle.palette_costs,
                            contract_version=np.array(3, dtype=np.int16))
        flattened = io.BytesIO()
        Image.fromarray(palette[labels], "RGB").save(flattened, "PNG")
        report = {"inferred_colour_count": 2, "palette": [
            {"index": 0, "name": "Night", "hex": "#0C1928", "rgb": [12, 25, 40], "share_percent": 0},
            {"index": 1, "name": "Orange", "hex": "#E15A2D", "rgb": [225, 90, 45], "share_percent": 0}]}
        for kind, filename, media_type, stream in (
            ("segmentation", "segmentation.npz", "application/octet-stream", segmentation),
            ("flattened", "flattened.png", "image/png", flattened),
            ("palette", "palette.json", "application/json", io.BytesIO(json.dumps(report).encode()))):
            stream.seek(0)
            store.add_artifact(revision["id"], kind=kind, filename=filename,
                               media_type=media_type, source=stream)
        return project, revision["id"]

    async def test_edits_are_immutable_persistent_and_palette_exact(self) -> None:
        project, parent = await self.flattened_project()
        before = (await self.client.get(f"/api/revisions/{parent}/editor")).json()
        self.assertEqual(len(before["palette"]), 2)
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits", json={"operations": [
                {"tool": "brush", "label": 1, "points": [[0, 0], [2, 0]], "radius": 1},
                {"tool": "flood_fill", "label": 0, "points": [[4, 4]]},
                {"tool": "lasso", "label": 1, "points": [[4, 4], [8, 4], [6, 8]]},
                {"tool": "component", "label": 0, "points": [[6, 6]]},
                {"tool": "protect", "points": [[0, 0]]},
                {"tool": "rename_colour", "label": 0, "name": "Night"},
                {"tool": "adjust_colour", "label": 0, "rgb": [1, 2, 3]},
            ]})
        self.assertEqual(response.status_code, 201, response.text)
        edited = response.json()
        self.assertEqual(edited["parent_revision_id"], parent)
        self.assertEqual(edited["settings"]["stale_concerns"], ["trace", "validation", "export"])

        state = (await self.client.get(f"/api/revisions/{edited['id']}/editor")).json()
        self.assertEqual(state["palette"][0]["name"], "Night")
        self.assertEqual(state["palette"][0]["rgb"], [1, 2, 3])
        self.assertGreater(state["protected_pixels"], 0)
        artifacts = (await self.client.get(f"/api/revisions/{edited['id']}/artifacts")).json()
        flattened = next(row for row in artifacts if row["kind"] == "flattened")
        image = Image.open(io.BytesIO((await self.client.get(flattened["download_url"])).content))
        exact = set(image.getdata())
        self.assertTrue(exact.issubset({tuple(row["rgb"]) for row in state["palette"]}))

        # Parent state is untouched, and a second child is a deterministic branch.
        parent_again = (await self.client.get(f"/api/revisions/{parent}/editor")).json()
        self.assertNotEqual(parent_again["palette"][0]["rgb"], [1, 2, 3])
        branch = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "pencil", "label": 1, "points": [[0, 0]]}]})
        self.assertEqual(branch.status_code, 201, branch.text)
        self.assertEqual(branch.json()["parent_revision_id"], parent)

    async def test_colour_merge_reindexes_labels_and_costs(self) -> None:
        project, parent = await self.flattened_project()
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "merge_colour", "source_label": 0, "target_label": 1}]})
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()["id"]
        state = (await self.client.get(f"/api/revisions/{revision}/editor")).json()
        self.assertEqual(len(state["palette"]), 1)
        artifacts = (await self.client.get(f"/api/revisions/{revision}/artifacts")).json()
        segmentation = next(row for row in artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(segmentation["download_url"])).content)) as data:
            self.assertEqual(data["palette_costs"].shape[2], 1)
            self.assertEqual(set(np.unique(data["labels"])), {0})

    async def test_off_palette_and_out_of_bounds_edits_are_rejected(self) -> None:
        project, parent = await self.flattened_project()
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "pencil", "label": 99, "points": [[0, 0]]}]})
        self.assertEqual(response.status_code, 409)
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "pencil", "label": 0, "points": [[999, 0]]}]})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
