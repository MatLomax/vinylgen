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

    async def test_palette_metadata_edits_are_immutable_and_palette_exact(self) -> None:
        project, parent = await self.flattened_project()
        before = (await self.client.get(f"/api/revisions/{parent}/editor")).json()
        self.assertEqual(len(before["palette"]), 2)
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "rename_colour", "label": 0, "name": "Midnight"}]})
        self.assertEqual(response.status_code, 201, response.text)
        edited = response.json()
        self.assertEqual(edited["parent_revision_id"], parent)
        self.assertEqual(edited["settings"]["stale_concerns"], ["cleanup", "trace", "validation", "export"])

        state = (await self.client.get(f"/api/revisions/{edited['id']}/editor")).json()
        self.assertEqual(state["palette"][0]["name"], "Midnight")
        artifacts = (await self.client.get(f"/api/revisions/{edited['id']}/artifacts")).json()
        flattened = next(row for row in artifacts if row["kind"] == "flattened")
        image = Image.open(io.BytesIO((await self.client.get(flattened["download_url"])).content))
        exact = set(image.getdata())
        self.assertTrue(exact.issubset({tuple(row["rgb"]) for row in state["palette"]}))

        # Parent state is untouched, and a second child is a deterministic branch.
        parent_again = (await self.client.get(f"/api/revisions/{parent}/editor")).json()
        self.assertEqual(parent_again["palette"][0]["name"], "Night")
        branch = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "rename_colour", "label": 1, "name": "Warm orange"}]})
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

    async def test_recolour_repaints_a_whole_palette_colour(self) -> None:
        project, parent = await self.flattened_project()
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "recolour_colour", "label": 1, "rgb": [10, 200, 60]}]})
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()["id"]

        # The whole colour moves to the new RGB/hex; the colour count is unchanged.
        state = (await self.client.get(f"/api/revisions/{revision}/editor")).json()
        self.assertEqual(len(state["palette"]), 2)
        self.assertEqual(state["palette"][1]["rgb"], [10, 200, 60])
        self.assertEqual(state["palette"][1]["hex"], "#0AC83C")

        # The raster now shows the new colour and none of the old, and labels are
        # preserved — a recolour only swaps the palette entry.
        artifacts = (await self.client.get(f"/api/revisions/{revision}/artifacts")).json()
        flattened = next(row for row in artifacts if row["kind"] == "flattened")
        colours = set(Image.open(io.BytesIO((await self.client.get(flattened["download_url"])).content)).getdata())
        self.assertIn((10, 200, 60), colours)
        self.assertNotIn((225, 90, 45), colours)
        segmentation = next(row for row in artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(segmentation["download_url"])).content)) as data:
            self.assertEqual(set(np.unique(data["labels"])), {0, 1})

        # The parent revision keeps its original colour — a clean branch point.
        parent_state = (await self.client.get(f"/api/revisions/{parent}/editor")).json()
        self.assertEqual(parent_state["palette"][1]["rgb"], [225, 90, 45])

    async def test_label_edits_carry_the_cost_volume_over_unchanged(self) -> None:
        # A reassign and a paint change only labels, so the edited revision must inherit
        # the parent's per-palette cost field byte-for-byte — the fast serialisation path
        # copies the compressed member rather than re-encoding it, and must not corrupt it.
        project, parent = await self.flattened_project()
        parent_artifacts = (await self.client.get(f"/api/revisions/{parent}/artifacts")).json()
        parent_seg = next(row for row in parent_artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(parent_seg["download_url"])).content)) as data:
            parent_costs = data["palette_costs"]

        for operation in ({"tool": "reassign_region", "point": [4, 4], "target_label": 0},
                          {"tool": "paint_pixels", "target_label": 0, "pixels": [[5, 5]]}):
            response = await self.client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/edits",
                json={"operations": [operation]})
            self.assertEqual(response.status_code, 201, response.text)
            artifacts = (await self.client.get(
                f"/api/revisions/{response.json()['id']}/artifacts")).json()
            segmentation = next(row for row in artifacts if row["kind"] == "segmentation")
            with np.load(io.BytesIO((await self.client.get(segmentation["download_url"])).content)) as data:
                np.testing.assert_array_equal(data["palette_costs"], parent_costs)
                self.assertEqual(data["palette_costs"].shape, (*data["labels"].shape, 2))

    async def test_recolour_requires_a_valid_rgb(self) -> None:
        project, parent = await self.flattened_project()
        missing = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "recolour_colour", "label": 0}]})
        self.assertEqual(missing.status_code, 409, missing.text)
        out_of_range = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "recolour_colour", "label": 0, "rgb": [0, 0, 256]}]})
        self.assertEqual(out_of_range.status_code, 422, out_of_range.text)

    async def test_connected_region_reassign_changes_only_the_selected_component(self) -> None:
        project, parent = await self.flattened_project()
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "reassign_region", "point": [4, 4], "target_label": 0}]})
        self.assertEqual(response.status_code, 201, response.text)
        artifacts = (await self.client.get(
            f"/api/revisions/{response.json()['id']}/artifacts")).json()
        segmentation = next(row for row in artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(segmentation["download_url"])).content)) as data:
            self.assertEqual(set(np.unique(data["labels"])), {0})
        parent_artifacts = (await self.client.get(f"/api/revisions/{parent}/artifacts")).json()
        parent_segmentation = next(row for row in parent_artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(parent_segmentation["download_url"])).content)) as data:
            self.assertEqual(set(np.unique(data["labels"])), {0, 1})

    async def test_pencil_paints_individual_pixels_and_splits_regions(self) -> None:
        from scipy import ndimage

        project, parent = await self.flattened_project()
        # Paint the background colour straight across the middle of the colour-1
        # block (rows 2..9), severing it into a top and a bottom half.
        pixels = [[x, 5] for x in range(3, 11)]
        response = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/edits",
            json={"operations": [{"tool": "paint_pixels", "target_label": 0, "pixels": pixels}]})
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()["id"]

        artifacts = (await self.client.get(f"/api/revisions/{revision}/artifacts")).json()
        segmentation = next(row for row in artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(segmentation["download_url"])).content)) as data:
            labels = data["labels"]
        self.assertTrue(np.all(labels[5, 3:11] == 0))
        cross = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]])
        _, components = ndimage.label(labels == 1, structure=cross)
        self.assertEqual(components, 2)

        # The flattened raster stays exactly on-palette after painting.
        flattened = next(row for row in artifacts if row["kind"] == "flattened")
        image = Image.open(io.BytesIO((await self.client.get(flattened["download_url"])).content))
        state = (await self.client.get(f"/api/revisions/{revision}/editor")).json()
        self.assertTrue(set(image.getdata()).issubset({tuple(row["rgb"]) for row in state["palette"]}))

        # The parent revision is an untouched, deterministic branch point.
        parent_artifacts = (await self.client.get(f"/api/revisions/{parent}/artifacts")).json()
        parent_segmentation = next(row for row in parent_artifacts if row["kind"] == "segmentation")
        with np.load(io.BytesIO((await self.client.get(parent_segmentation["download_url"])).content)) as data:
            _, parent_components = ndimage.label(data["labels"] == 1, structure=cross)
        self.assertEqual(parent_components, 1)

    async def test_pencil_rejects_pixels_outside_the_image(self) -> None:
        project, parent = await self.flattened_project()
        for pixels in ([[14, 0]], [[0, 12]], [[-1, 0]], []):
            response = await self.client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/edits",
                json={"operations": [{"tool": "paint_pixels", "target_label": 0, "pixels": pixels}]})
            self.assertEqual(response.status_code, 409, pixels)

    async def test_unsupported_manual_tools_are_rejected_at_api_boundary(self) -> None:
        # Whole-colour merge and recolour, connected-area reassignment, and the
        # per-pixel pencil are the manual edits; freehand brush, flood fill, and
        # lasso painting are not.
        project, parent = await self.flattened_project()
        for operation in (
            {"tool": "brush", "label": 1, "points": [[0, 0]], "radius": 4},
            {"tool": "flood_fill", "label": 1, "points": [[0, 0]]},
            {"tool": "lasso", "label": 1, "points": [[0, 0], [1, 0], [0, 1]]},
            {"tool": "protect", "points": [[0, 0]]},
        ):
            response = await self.client.post(
                f"/api/projects/{project['id']}/revisions/{parent}/edits",
                json={"operations": [operation]})
            self.assertEqual(response.status_code, 422, operation)

        masked = await self.client.post(
            f"/api/projects/{project['id']}/revisions/{parent}/masked-edits")
        self.assertIn(masked.status_code, {404, 405})


if __name__ == "__main__":
    unittest.main()
