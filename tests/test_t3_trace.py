"""Card 2 of vinylgen-on-t3: the trace stage calls ``t3api.trace`` once, keeps the trace as the
planar-map file ``map.json`` and writes the edge SVG ``traced-edge.svg`` from it.

Every test runs on the test array (``tests/fixtures/t3-test-array``, the 400 x 400 crop of
fox-owl-table-2 traced by t3api 0.24.0) through the code a user reaches: ``trace_subject`` on a
subject folder built the way the palette stage writes one, the trace CLI's ``main``, the web
pipeline's trace job, and ``just deploy``'s cache warm. The contract is the tracer card
``cards/vinylgen-on-t3/card.md`` sections 1, 2, 3 and 7, and ``planar-map-file.md``.

The trace stage calls the engine as ``t3api.trace(labels, on_event=...)`` (card section 1), a
module attribute, so a wrapper patched onto ``t3api.trace`` counts every call.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np
from numpy.polynomial import polynomial as P
from PIL import Image

import t3api

from vinylgen.contracts import CleanupSettings, SegmentationBundle, TraceSettings
from vinylgen.svg_writer import INKSCAPE, SODIPODI, SVG
from vinylgen.tracing import trace_subject
from vinylgen.validation import validate_svg

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "t3-test-array"
SCRATCH = ROOT / ".tmp"
# The longest any one trace of the test array may take here, a bound against a hang: a cold
# engine cache costs up to 45.1 s of warm-up (card section 1) and the trace about 1 s at one
# worker, and the rest of the stage (the audit, the Inkscape preview) a few seconds.
TRACE_BOUND_S = 300

# Card section 1's progress table: the fixed ticks, in order.
FIXED_TICKS = [
    (0.01, "Reading segmentation"),
    (0.02, "Tracing: planar map"),
    (0.80, "Writing the planar map and the edge SVG"),
    (0.85, "Checking the planar map"),
    (0.88, "Semantic audit"),
    (0.90, "Writing artifacts"),
    (0.95, "Rendering preview"),
    (1.00, "Complete"),
]
JUNCTION_TICK = re.compile(r"Tracing: junction edges (\d+) of (\d+)")
CHAIN_TICK = re.compile(r"Tracing: chains (\d+) of (\d+)")


def fixture_palette() -> list[dict]:
    return json.loads((FIXTURE / "meta.json").read_text())["palette"]


def build_subject(root: Path, name: str = "test-array") -> Path:
    """A subject folder holding the test array as the palette stage writes one: ``source.png``,
    ``flattened.png`` (the palette colours of the labels), ``palette.json`` and the v3
    ``segmentation.npz`` whose labels are the test array."""
    subject = root / name
    subject.mkdir(parents=True)
    labels = np.load(FIXTURE / "labels.npy")
    palette = fixture_palette()
    rgb = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
    pixels = rgb[labels]
    Image.fromarray(pixels, "RGB").save(subject / "flattened.png")
    Image.fromarray(pixels, "RGB").save(subject / "source.png")
    height, width = labels.shape
    (subject / "palette.json").write_text(
        json.dumps({"dimensions": [width, height], "palette": palette}, indent=2) + "\n")
    costs = np.full((*labels.shape, len(palette)), 50.0, dtype=np.float32)
    np.put_along_axis(costs, labels[..., None].astype(np.intp), 0.0, axis=2)
    SegmentationBundle(
        labels=labels,
        protected_features=np.zeros(labels.shape, dtype=bool),
        explicit_protected_features=np.zeros(labels.shape, dtype=bool),
        palette_rgb=rgb,
        palette_costs=costs,
    ).save(subject / "segmentation.npz")
    return subject


class Recorder:
    """Wraps ``t3api.trace``: records each call's arguments, the events it emitted and its result."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.events: list[tuple[str, dict]] = []
        self.results: list = []
        self._real = t3api.trace

    def __call__(self, labels, *args, **kwargs):
        call = {"labels": np.array(labels, copy=True), "args": args, "kwargs": dict(kwargs)}
        self.calls.append(call)
        on_event = kwargs.get("on_event", args[1] if len(args) > 1 else None)
        if on_event is not None:
            def relay(kind, payload, _on_event=on_event):
                self.events.append((kind, payload))
                return _on_event(kind, payload)
            if "on_event" in kwargs:
                kwargs["on_event"] = relay
            else:
                args = (args[0], relay, *args[2:])
        result = self._real(labels, *args, **kwargs)
        self.results.append(result)
        return result


def run_trace(subject: Path, **kwargs) -> dict:
    """``trace_subject`` on ``subject`` with ``t3api.trace`` recorded and the ticks captured. An
    exception is kept, not raised, so each test reports its own failure."""
    recorder = Recorder()
    ticks: list[tuple[float, str]] = []
    outcome = {"recorder": recorder, "ticks": ticks, "subject": subject, "error": None}
    with mock.patch.object(t3api, "trace", recorder):
        try:
            outcome["audit"] = trace_subject(
                subject, TraceSettings(), {},
                progress=lambda fraction, message: ticks.append((fraction, message)), **kwargs)
        except Exception as error:  # noqa: BLE001 - re-raised by the test that needs the trace
            outcome["error"] = error
    return outcome


# ----------------------------------------------------------------------------- the area oracle
# A face's drawn area by Green's theorem over its pieces, exact up to float rounding, computed
# here from map.json independently of the writer: (1/2) of the integral of x dy - y dx along each
# ring, in closed form per piece. A face's rings bound it even-odd and do not cross, so its area
# is its outer ring's (the largest) less its holes', whatever direction each ring runs.

def _ellipse_point(seg: list, t: float) -> tuple[float, float]:
    _, cx, cy, rx, ry, rot = seg[:6]
    c, s = math.cos(rot), math.sin(rot)
    return (cx + rx * math.cos(t) * c - ry * math.sin(t) * s,
            cy + rx * math.cos(t) * s + ry * math.sin(t) * c)


def piece_area(seg: list) -> float:
    """(1/2) of the integral of x dy - y dx along one exact segment, in its stored direction."""
    if seg[0] == "L":
        x0, y0, x1, y1 = seg[1:]
        return 0.5 * (x0 * y1 - x1 * y0)
    if seg[0] == "E":
        # x = c + u, y = d + v with (u, v) on the centred ellipse, where u v' - v u' = rx ry.
        _, cx, cy, rx, ry, _, a0, a1 = seg
        (x0, y0), (x1, y1) = _ellipse_point(seg, a0), _ellipse_point(seg, a1)
        return 0.5 * (cx * (y1 - y0) - cy * (x1 - x0) + rx * ry * (a1 - a0))
    if seg[0] == "C":
        basis = [np.array(b, dtype=float) for b in
                 ([1, -3, 3, -1], [0, 3, -6, 3], [0, 0, 3, -3], [0, 0, 0, 1])]
        x = sum(v * b for v, b in zip(seg[1::2], basis))
        y = sum(v * b for v, b in zip(seg[2::2], basis))
        integral = P.polyint(P.polysub(P.polymul(x, P.polyder(y)), P.polymul(y, P.polyder(x))))
        return 0.5 * float(P.polyval(1.0, integral) - P.polyval(0.0, integral))
    raise ValueError(f"unknown segment {seg[0]!r}")


def face_areas(pm: dict) -> dict[int, float]:
    edge_area = [sum(piece_area(piece["seg"]) for piece in edge["pieces"]) for edge in pm["edges"]]
    areas = {}
    for face in pm["faces"]:
        rings = sorted((abs(sum(edge_area[r] if r >= 0 else -edge_area[~r] for r in ring))
                        for ring in face["rings"]), reverse=True)
        areas[face["face"]] = rings[0] - sum(rings[1:])
    return areas


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-")


def expected_layers(pm: dict, paths: list[dict], palette: list[dict],
                    included: list[int]) -> list[dict]:
    """Card section 2's edge SVG: per included colour in palette order, one path per face of that
    colour, numbered largest first by drawn area, ties to the face id."""
    areas = face_areas(pm)
    by_face = {row["face"]: row for row in paths}
    layers = []
    for index in included:
        row = palette[index]
        faces = sorted((face for face in by_face.values() if face["label"] == index),
                       key=lambda face: (-areas[face["face"]], face["face"]))
        layers.append({
            "attrib": {f"{{{INKSCAPE}}}groupmode": "layer",
                       f"{{{INKSCAPE}}}label": f"{row['name']} ({row['hex']})",
                       "id": f"layer-{slug(row['name'])}", "fill": row["hex"], "stroke": "none",
                       "fill-rule": "evenodd"},
            "paths": [{"id": f"edge-{slug(row['name'])}-{number:03d}",
                       f"{{{INKSCAPE}}}label": f"{row['name']} {number:03d}",
                       "d": face["d"], f"{{{SODIPODI}}}nodetypes": face["nodetypes"]}
                      for number, face in enumerate(faces, 1)]})
    return layers


def svg_layers(path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    return [{"attrib": dict(layer.attrib),
             "paths": [dict(node.attrib) for node in layer.findall(f"{{{SVG}}}path")]}
            for layer in root.findall(f"{{{SVG}}}g")]


class _Traced(unittest.TestCase):
    """One trace of the test array, shared by the class's tests."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.trace = run_trace(build_subject(Path(cls._temp.name)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def traced(self) -> dict:
        if self.trace["error"] is not None:
            raise self.trace["error"]
        return self.trace


class TraceSubjectCallsT3Test(_Traced):
    def test_trace_subject_calls_t3api_trace_once_on_the_int16_labels_at_its_one_setting(self):
        recorder = self.traced()["recorder"]
        self.assertEqual(len(recorder.calls), 1, "t3api.trace is called exactly once per trace")
        call = recorder.calls[0]
        labels = np.load(FIXTURE / "labels.npy")
        self.assertEqual(call["labels"].dtype, np.int16)
        self.assertTrue(np.array_equal(call["labels"], labels))
        setting = call["kwargs"].get("setting", call["args"][0] if call["args"] else None)
        self.assertIsNone(setting, "no setting: t3 runs at t3api.SETTING, its only validated one")
        workers = call["kwargs"].get("workers", call["args"][2] if len(call["args"]) > 2 else None)
        self.assertIsNone(workers, "workers=None: t3api.workers_of chooses")

    def test_map_json_equals_the_fixture_byte_for_byte(self):
        subject = self.traced()["subject"]
        self.assertEqual((subject / "map.json").read_bytes(), (FIXTURE / "map.json").read_bytes())

    def test_map_json_is_the_planar_map_of_the_trace_with_the_palette(self):
        trace = self.traced()
        text = (trace["subject"] / "map.json").read_text()
        result = trace["recorder"].results[0]
        self.assertEqual(text, t3api.planar_map_json(t3api.planar_map(result, fixture_palette())))

    def test_map_json_reads_back_and_paints_the_painted_labels_at_every_centre(self):
        trace = self.traced()
        pm = t3api.read_planar_map((trace["subject"] / "map.json").read_text())
        self.assertEqual(t3api.planar_map_check(pm)["violations"], 0)
        painted = t3api.paint_planar_map(pm)
        with np.load(FIXTURE / "painted.npz") as data:
            self.assertTrue(np.array_equal(painted, data["painted"]))
        self.assertTrue(np.array_equal(painted, trace["recorder"].results[0].painted))

    def test_trace_json_carries_the_t3_result_build_and_map_sha256(self):
        trace = self.traced()
        result = trace["recorder"].results[0]
        audit = json.loads((trace["subject"] / "trace.json").read_text())
        self.assertEqual(audit["t3"]["summary"], json.loads(json.dumps(result.summary)))
        self.assertEqual(audit["t3"]["seconds"], result.seconds)
        self.assertEqual(audit["t3"]["warm_s"], result.warm_s)
        self.assertEqual(audit["t3"]["workers"], result.workers)
        self.assertEqual(audit["backend"]["tracer"], "t3api")
        self.assertEqual(audit["backend"]["build"], t3api.BUILD_ID)
        self.assertEqual(audit["backend"]["version"], t3api.VERSION)
        self.assertEqual(audit["determinism"]["map_sha256"],
                         hashlib.sha256((trace["subject"] / "map.json").read_bytes()).hexdigest())


class EdgeSvgFromMapTest(_Traced):
    def expected(self, included: list[int]) -> list[dict]:
        subject = self.traced()["subject"]
        pm = t3api.read_planar_map((subject / "map.json").read_text())
        palette = json.loads((subject / "palette.json").read_text())["palette"]
        paths = json.loads((FIXTURE / "paths.json").read_text())
        return expected_layers(pm, paths, palette, included)

    def test_the_frame_is_the_planar_maps_pixel_grid_with_one_layer_per_colour_in_palette_order(self):
        subject = self.traced()["subject"]
        pm = t3api.read_planar_map((subject / "map.json").read_text())
        root = ET.parse(subject / "traced-edge.svg").getroot()
        self.assertEqual((root.attrib["viewBox"], root.attrib["width"], root.attrib["height"]),
                         (f"0 0 {pm['W']} {pm['H']}", str(pm["W"]), str(pm["H"])))
        actual = svg_layers(subject / "traced-edge.svg")
        expected = self.expected([0, 1, 2, 3])
        self.assertEqual([layer["attrib"] for layer in actual],
                         [layer["attrib"] for layer in expected])

    def test_one_even_odd_path_per_face_with_its_d_and_nodetypes_from_the_planar_map(self):
        actual = svg_layers(self.traced()["subject"] / "traced-edge.svg")
        paths = json.loads((FIXTURE / "paths.json").read_text())
        drawn = sorted((p["d"], p[f"{{{SODIPODI}}}nodetypes"])
                       for layer in actual for p in layer["paths"])
        self.assertEqual(drawn, sorted((p["d"], p["nodetypes"]) for p in paths))

    def test_paths_are_numbered_largest_first_with_stable_ids_and_labels(self):
        actual = svg_layers(self.traced()["subject"] / "traced-edge.svg")
        expected = self.expected([0, 1, 2, 3])
        self.assertEqual([layer["paths"] for layer in actual],
                         [layer["paths"] for layer in expected])

    def test_the_edge_svg_of_the_planar_map_passes_validate_svg(self):
        subject = self.traced()["subject"]
        svg = subject / "traced-edge.svg"
        paths = json.loads((FIXTURE / "paths.json").read_text())
        drawn = sorted(p["d"] for layer in svg_layers(svg) for p in layer["paths"])
        self.assertEqual(drawn, sorted(p["d"] for p in paths), "the SVG is the planar map's")
        palette = json.loads((subject / "palette.json").read_text())["palette"]
        checks = validate_svg(svg, palette, [0, 1, 2, 3])
        self.assertTrue(checks["passed"], checks)

    def test_the_export_from_map_json_alone_equals_the_one_written_at_trace_time(self):
        from vinylgen.svg_writer import add_face_layer, svg_document, write_svg

        subject = self.traced()["subject"]
        pm = t3api.read_planar_map((subject / "map.json").read_text())
        palette = json.loads((subject / "palette.json").read_text())["palette"]
        root = svg_document(pm["W"], pm["H"], None)
        for index in range(len(palette)):
            add_face_layer(root, palette[index], pm)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            exported = Path(name) / "traced-edge.svg"
            write_svg(exported, root)
            self.assertEqual(exported.read_bytes(), (subject / "traced-edge.svg").read_bytes())


class TraceProgressTest(_Traced):
    def test_ticks_are_monotone_in_the_unit_interval_and_follow_the_table(self):
        trace = self.traced()
        ticks = trace["ticks"]
        fractions = [fraction for fraction, _ in ticks]
        self.assertTrue(ticks)
        self.assertTrue(all(0.0 <= f <= 1.0 for f in fractions), fractions)
        self.assertEqual(fractions, sorted(fractions), "the bar never moves backwards")
        fixed = {message: (stage, fraction) for stage, (fraction, message) in enumerate(
            FIXED_TICKS[:2] + [(None, "junction"), (None, "chain")] + FIXED_TICKS[2:])}
        edges = [payload for kind, payload in trace["recorder"].events if kind == "edges"]
        self.assertEqual(len(edges), 1)
        junctions, upper = edges[0]["junction_edges"], edges[0]["count"]
        chain_totals = {payload["total"] for kind, payload in trace["recorder"].events
                        if kind == "chain"}
        stages, chains = [], []
        for fraction, message in ticks:
            if (match := JUNCTION_TICK.fullmatch(message)) is not None:
                j, total = map(int, match.groups())
                stages.append(fixed["junction"][0])
                self.assertEqual(total, junctions, message)
                self.assertAlmostEqual(fraction, 0.03 + 0.77 * j / (junctions + upper), 12, message)
            elif (match := CHAIN_TICK.fullmatch(message)) is not None:
                c, total = map(int, match.groups())
                stages.append(fixed["chain"][0])
                chains.append((c, total))
                self.assertIn(total, chain_totals, message)
                self.assertAlmostEqual(
                    fraction, 0.03 + 0.77 * (junctions + c) / (junctions + total), 12, message)
            else:
                self.assertIn(message, fixed, "every message is one of section 1's table")
                stage, expected = fixed[message]
                stages.append(stage)
                self.assertAlmostEqual(fraction, expected, 12, message)
        self.assertEqual(stages, sorted(stages), "the table's messages appear in its order")
        present = {message for _, message in ticks}
        for _, message in FIXED_TICKS:
            self.assertIn(message, present)
        self.assertTrue(chains, "the chain band reports progress")
        self.assertEqual(chains[-1][0], chains[-1][1], "the chain band ends at N of N")
        self.assertEqual(ticks[0], (0.01, "Reading segmentation"))
        self.assertEqual(ticks[-1], (1.0, "Complete"))


class OmittedColourTest(unittest.TestCase):
    """The omitted colour is traced like any other label (map.json holds every label) and the
    edge SVG leaves its faces out; every other face is drawn as without the omission."""

    OMITTED = 0  # Black, the colour of the holed face

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        root = Path(cls._temp.name)
        omitted_hex = fixture_palette()[cls.OMITTED]["hex"]
        cls.whole = run_trace(build_subject(root, "whole"))
        # The CLI's way (a removed colour named in the cleanup settings) and the web's (the
        # palette index the cleanup resolved).
        cls.by_name = run_trace(build_subject(root, "by-name"),
                                cleanup=CleanupSettings(remove_colour=omitted_hex))
        cls.by_index = run_trace(build_subject(root, "by-index"), removed_index=cls.OMITTED)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def runs(self) -> list[dict]:
        for run in (self.whole, self.by_name, self.by_index):
            if run["error"] is not None:
                raise run["error"]
        return [self.by_name, self.by_index]

    def test_map_json_is_identical_to_the_run_without_omission(self):
        for run in self.runs():
            self.assertEqual((run["subject"] / "map.json").read_bytes(),
                             (self.whole["subject"] / "map.json").read_bytes())

    def test_the_omitted_colours_faces_are_absent_and_every_other_face_is_unchanged(self):
        whole = svg_layers(self.whole["subject"] / "traced-edge.svg")
        omitted_name = fixture_palette()[self.OMITTED]["name"]
        kept = [layer for layer in whole
                if layer["attrib"]["id"] != f"layer-{slug(omitted_name)}"]
        self.assertEqual(len(kept), len(whole) - 1)
        paths = json.loads((FIXTURE / "paths.json").read_text())
        omitted_d = {p["d"] for p in paths if p["label"] == self.OMITTED}
        runs = self.runs()
        pm = t3api.read_planar_map((self.whole["subject"] / "map.json").read_text())
        palette = json.loads((self.whole["subject"] / "palette.json").read_text())["palette"]
        included = [i for i in range(len(palette)) if i != self.OMITTED]
        self.assertEqual(kept, expected_layers(pm, paths, palette, included),
                         "the run without omission draws the planar map's faces")
        for run in runs:
            layers = svg_layers(run["subject"] / "traced-edge.svg")
            self.assertEqual(layers, kept)
            drawn = {p["d"] for layer in layers for p in layer["paths"]}
            self.assertFalse(drawn & omitted_d)
            self.assertTrue(validate_svg(run["subject"] / "traced-edge.svg", palette,
                                         included)["passed"])


class _Api:
    """A live web app over a scratch store (the web pipeline's trace job runs in its worker
    subprocess), with a palette revision whose artifacts are the test array's subject folder."""

    def __init__(self, test: unittest.TestCase, data_dir: Path):
        from vinylgen.web.app import create_app
        from vinylgen.web.config import WebConfig

        self.test = test
        self.app = create_app(WebConfig(data_dir))

    async def __aenter__(self):
        import httpx

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        await self.app.state.jobs.shutdown()

    async def palette_revision(self, subject: Path) -> tuple[str, str]:
        buffer = io.BytesIO((subject / "source.png").read_bytes())
        imported = await self.client.post(
            "/api/projects/import", data={"name": "T3 Test Array"},
            files={"source": ("test-array.png", buffer.getvalue(), "image/png")})
        self.test.assertEqual(imported.status_code, 201, imported.text)
        project = imported.json()
        store = self.app.state.store
        revision = store.create_revision(
            project["id"], parent_revision_id=project["revisions"][0]["id"], kind="palette",
            message="Deterministic palette pipeline",
            settings={"pipeline_stage": "palette", "palette": {}, "trace": None,
                      "stale_concerns": ["cleanup", "trace", "validation", "export"]})
        artifacts = {"source": ("source.png", "image/png"),
                     "flattened": ("flattened.png", "image/png"),
                     "segmentation": ("segmentation.npz", "application/octet-stream"),
                     "palette": ("palette.json", "application/json"),
                     "report": ("flattened.png", "image/png")}
        for kind, (filename, media_type) in artifacts.items():
            with (subject / filename).open("rb") as source:
                store.add_artifact(revision["id"], kind=kind,
                                   filename="report.png" if kind == "report" else filename,
                                   media_type=media_type, source=source)
        return project["id"], revision["id"]

    async def trace(self, project_id: str, revision_id: str) -> str:
        response = await self.client.post("/api/jobs", json={
            "kind": "trace", "project_id": project_id, "revision_id": revision_id})
        self.test.assertEqual(response.status_code, 202, response.text)
        deadline = time.monotonic() + TRACE_BOUND_S
        while time.monotonic() < deadline:
            job = (await self.client.get(f"/api/jobs/{response.json()['id']}")).json()
            if job["status"] not in {"queued", "running"}:
                self.test.assertEqual(job["status"], "succeeded", job)
                return job["result_revision_id"]
            await asyncio.sleep(0.05)
        self.test.fail("the trace job did not finish")

    async def artifact(self, revision_id: str, kind: str) -> bytes:
        rows = (await self.client.get(f"/api/revisions/{revision_id}/artifacts")).json()
        matching = [row for row in rows if row["kind"] == kind]
        self.test.assertEqual(len(matching), 1, f"one {kind} artifact: {rows}")
        return (await self.client.get(matching[0]["download_url"])).content


class EntryPointsTest(unittest.IsolatedAsyncioTestCase):
    """The trace CLI and the web pipeline's trace stage both reach ``trace_subject`` and keep the
    same ``map.json`` for the test array."""

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli_map(self) -> bytes:
        from vinylgen.cli_trace import main

        subject = build_subject(self.root / "cli")
        with mock.patch("sys.argv", ["svg_trace.py", str(subject)]), \
                contextlib.redirect_stdout(io.StringIO()):
            main()
        return (subject / "map.json").read_bytes()

    async def web_map(self) -> bytes:
        subject = build_subject(self.root / "web")
        async with _Api(self, self.root / "data") as api:
            project_id, revision_id = await api.palette_revision(subject)
            traced = await api.trace(project_id, revision_id)
            return await api.artifact(traced, "traced-map")

    async def test_cli_and_web_trace_keep_the_same_map_json(self):
        cli = self.cli_map()
        web = await self.web_map()
        expected = (FIXTURE / "map.json").read_bytes()
        self.assertEqual(cli, expected)
        self.assertEqual(web, expected)


class CacheWarmTest(unittest.TestCase):
    """``just deploy`` installs the wheel and warms the engine cache with one ``t3api.trace`` of the
    test array in the service's environment, before the health check (card section 1)."""

    SCRIPT = ROOT / "scripts" / "warm_t3_cache.py"

    def test_the_warm_fills_a_cold_cache_and_records_its_duration(self):
        SCRATCH.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            cache_base = Path(name) / "t3api-cache"
            env = {**os.environ, "T3API_CACHE_DIR": str(cache_base)}
            completed = subprocess.run(
                [sys.executable, str(self.SCRIPT)], cwd=ROOT, env=env, capture_output=True,
                text=True, timeout=TRACE_BOUND_S)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            cache = cache_base / t3api.BUILD_ID
            self.assertTrue(any(cache.rglob("*.nbi")), "the warm wrote the engine's kernels")
            record = json.loads((cache / "warm.json").read_text())
            self.assertEqual(record["build"], t3api.BUILD_ID)
            self.assertIsInstance(record["seconds"], float)
            self.assertGreater(record["seconds"], 0.0)
            self.assertGreater(record["warm_s"], 0.0, "a cold cache pays the warm-up")
            self.assertGreaterEqual(record["seconds"], record["warm_s"])
            self.assertGreaterEqual(record["workers"], 1)

    def test_deploy_installs_the_requirements_then_warms_before_the_health_check(self):
        justfile = (ROOT / "justfile").read_text()
        match = re.search(r"^deploy\b.*?:.*?\n((?:[ \t]+.*\n|\n)+)", justfile, re.MULTILINE)
        self.assertIsNotNone(match, "the deploy recipe")
        body = match.group(1)
        install = body.find(".venv/bin/pip install -r requirements-web.txt")
        warm = body.find(".venv/bin/python scripts/warm_t3_cache.py")
        health = body.find("just health\n")
        self.assertGreaterEqual(install, 0, body)
        self.assertGreater(warm, install, body)
        self.assertGreater(health, warm, body)

    def test_the_service_can_write_the_t3api_cache_under_its_sandbox(self):
        # numba keeps the engine's kernels only where it can write (t3api's locator checks with
        # ensure_cache_path), and the service runs under ProtectSystem=strict, so the cache the
        # deploy warms must lie inside ReadWritePaths.
        unit = (ROOT / "deploy" / "vinylgen-user.service").read_text()
        self.assertRegex(unit, r"(?m)^ProtectSystem=strict$")
        writable = [path.lstrip("-") for line in re.findall(r"(?m)^ReadWritePaths=(.*)$", unit)
                    for path in line.split()]
        configured = [value.split("=", 1)[1] for value in
                      re.findall(r"(?m)^Environment=(.*)$", unit)
                      if value.startswith("T3API_CACHE_DIR=")]
        cache_base = configured[0] if configured else "%h/.cache/t3api"
        self.assertTrue(
            any(cache_base == root or cache_base.startswith(root.rstrip("/") + "/")
                for root in writable),
            f"the t3api cache {cache_base} is outside ReadWritePaths {writable}")


if __name__ == "__main__":
    unittest.main()
