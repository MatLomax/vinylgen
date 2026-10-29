"""Card 4 of vinylgen-on-t3: the trace settings under t3, the record upgrade, re-exporting without
a trace, the live trace view's face events and the retired windowed trace preview.

The contract is the tracer card ``cards/vinylgen-on-t3/card.md`` section 5. Every test that traces
runs the test array (``tests/fixtures/t3-test-array``) through the code a user reaches, with the
fixture helpers of card 2's tests (``tests/test_t3_trace.py``): the web pipeline's cleanup and
trace jobs (each trace run in its ``pipeline_worker`` subprocess), the worker itself, the job's
SSE stream, and the trace CLI's ``main``.

``t3api.trace`` is counted wherever it runs. In this process it is wrapped by patching the module
attribute (card 2's recorder pattern). In each pipeline worker it is wrapped by a
``sitecustomize`` module put on the worker's ``PYTHONPATH``, which the worker inherits from this
process's environment: the wrapper logs one JSON line per call (the labels' dtype, shape and
sha256, and the face ids of the ``face`` events in the order t3 emitted them) and relays every
event unchanged.

The stored records are real: the two trace revisions' ``settings_json`` and the ``trace.json``
settings block below were read from vinylgen's own store (``.vinylgen-data``), and the per-mille
record and client request are 3fe9f57's ``asdict(TraceSettings())`` and ``DEFAULT_TRACE_SETTINGS``,
whose field set section 5's table names. Each is written through the store or the CLI, never
handed to the code under test as a bare dict, except where section 5 names the field set itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np
from scipy import ndimage

import t3api

from test_t3_trace import (
    FIXTURE,
    SCRATCH,
    TRACE_BOUND_S,
    _Api,
    build_subject,
    fixture_palette,
    slug,
)
from vinylgen.svg_writer import SODIPODI, SVG

ROOT = Path(__file__).resolve().parents[1]

# Card section 5: the three fields TraceSettings keeps, at the limits the form has today.
FORM_LIMITS = {"soft_path_nodes": 5_000, "soft_layer_nodes": 50_000, "soft_document_nodes": 200_000}
KEPT = tuple(FORM_LIMITS)
# Card section 5's table: every other TraceSettings field is retired.
SECTION_5_RETIRED = (
    "curve_fit_tolerance", "straight_line_max_deviation", "minimum_straight_length",
    "max_deviation", "corner_analysis_span", "side_smoothness", "corner_sharpening_allowance",
    "straight_line_snap", "snap_angle", "node_placement", "nodes_at_bends", "curve_feature_scale",
    "length_unit", "overlap", "solid_underlayers", "layer_order")
# Names retired or renamed before card 4, still in stored records: ``corner_threshold``
# (``RETIRED_TRACE_FIELDS`` at 3fe9f57) and ``corner_straightness`` (``side_smoothness``'s old name).
EARLIER_NAMES = ("corner_threshold", "corner_straightness")
# The t3api build card 2 installed first (build-2a.md, wheels/README.md): a real earlier build id.
EARLIER_BUILD_ID = "0.24.0-nuitka-d78720649989c397"

# vinylgen's store, trace revision 765f5d7ac0ab4de0bdd201fd8fe53e9f (2026-08-23): settings_json,
# pixel era, with the cleanup-stage fields a trace recorded before the cleanup stage existed.
STORED_2026_08_23 = {
    "palette": {"aa_ambiguity_margin": 7.0, "aa_resolution_passes": 5, "aa_smoothness": 2.5,
                "analysis_max_dimension": 640, "distinct_colour_point_pairs": [],
                "fragmented_delta_e": 24.0, "fragmented_largest_region": 0.3,
                "fragmented_share": 0.035, "histogram_bits": 5, "initial_colours": 16,
                "intentional_line_min_contrast": 12.0, "intentional_line_min_length": 8,
                "min_colour_share": 0.01, "min_output_colours": 2, "min_region_pixels": 24,
                "preserved_colour_points": [], "protected_component_points": [],
                "similar_delta_e": 28.0, "small_delta_e": 36.0, "small_share": 0.004},
    "pipeline_stage": "trace", "stale_concerns": ["export"],
    "trace": {"cleanup": False, "cleanup_action": "remove", "corner_analysis_span": 8.0,
              "corner_threshold": 1.0, "curve_fit_tolerance": 0.8, "curve_optimization": True,
              "height_mm": None, "layer_order": None, "max_deviation": 2.0,
              "min_detail_width": 0.0, "min_region_area": 0.0, "minimum_straight_length": 4.0,
              "overlap": 0.0, "remove_below_width": 0.0, "remove_colour": None,
              "soft_document_nodes": 200000, "soft_layer_nodes": 50000, "soft_path_nodes": 5000,
              "straight_line_max_deviation": 0.8, "width_mm": None},
}
# vinylgen's store, trace revision 4c30768370c74293a2febd6fb15d6bec (2026-09-23, fox-owl-table):
# settings_json, the newest trace revision there, pixel era.
STORED_2026_09_23 = {
    "palette": {"aa_ambiguity_margin": 7.0, "aa_resolution_passes": 5, "aa_smoothness": 2.5,
                "analysis_max_dimension": 640, "distinct_colour_point_pairs": [],
                "dither_radius": 1, "final_colours": 6, "flatten_smooth_edges": True,
                "flatten_upscale": 1, "fold_small_colours": True, "fragmented_delta_e": 24.0,
                "fragmented_largest_region": 0.3, "fragmented_share": 0.035, "histogram_bits": 5,
                "initial_colours": 16, "intentional_line_min_contrast": 12.0,
                "intentional_line_min_length": 8, "merge_fragmented_colours": True,
                "min_colour_share": 0.01, "min_feature_width": 0, "min_output_colours": 2,
                "min_region_pixels": 24, "preserved_colour_points": [],
                "protect_intentional_lines": True, "protected_component_points": [],
                "resolve_antialiasing": True, "similar_delta_e": 10.0, "small_delta_e": 36.0,
                "small_share": 0.004},
    "pipeline_stage": "trace", "stale_concerns": ["export"],
    "trace": {"cleanup": True, "cleanup_action": "remove", "corner_analysis_span": 10.0,
              "corner_threshold": 80.0, "curve_fit_tolerance": 1.6, "curve_optimization": False,
              "height_mm": 400.0, "layer_order": None, "max_deviation": None,
              "min_detail_width": 1.0, "min_region_area": 4.0, "minimum_straight_length": 4.0,
              "overlap": 0.0, "remove_below_width": 1.0, "remove_colour": None,
              "soft_document_nodes": 200000, "soft_layer_nodes": 50000, "soft_path_nodes": 5000,
              "solid_underlayers": False, "straight_line_max_deviation": 1.2, "width_mm": 1200.0},
}
# 3fe9f57's ``asdict(TraceSettings())``: the per-mille record every trace revision since trace
# lengths became thousandths records (section 5's field set, 3fe9f57's defaults).
RECORD_3FE9F57 = {
    "overlap": 0.0, "solid_underlayers": False, "layer_order": None, "length_unit": "per_mille",
    "curve_fit_tolerance": 0.75, "straight_line_max_deviation": 0.8,
    "minimum_straight_length": 2.0, "max_deviation": None, "corner_analysis_span": 4.0,
    "side_smoothness": 0.8, "corner_sharpening_allowance": None, "straight_line_snap": "axis",
    "snap_angle": 2.0, "node_placement": "prefer_extremes", "nodes_at_bends": True,
    "curve_feature_scale": 2.0, "soft_path_nodes": 5000, "soft_layer_nodes": 50000,
    "soft_document_nodes": 200000,
}
STORED_3FE9F57 = {"palette": {}, "pipeline_stage": "trace", "stale_concerns": ["export"],
                  "trace": RECORD_3FE9F57}
# The ``settings`` block of trace revision 4c30768370c74293a2febd6fb15d6bec's ``trace.json`` in
# vinylgen's store (no ``cleanup_settings`` key; ``mode`` physical, 1200 x 400 mm).
OLD_TRACE_JSON_SETTINGS = {
    "width_mm": 1200.0, "height_mm": 400.0, "overlap": 0.0, "solid_underlayers": False,
    "remove_colour": None, "layer_order": None, "corner_threshold": 80.0,
    "curve_optimization": False, "curve_fit_tolerance": 1.6, "straight_line_max_deviation": 1.2,
    "minimum_straight_length": 4.0, "max_deviation": 0.5524861878453039,
    "corner_analysis_span": 10.0, "cleanup": True, "cleanup_action": "remove",
    "min_detail_width": 1.0, "min_region_area": 4.0, "remove_below_width": 1.0,
    "soft_path_nodes": 5000, "soft_layer_nodes": 50000, "soft_document_nodes": 200000,
}
# What ``cli_trace --config`` reads as the cleanup settings of that block today (unchanged by
# card 4: physical size and the omitted colour stay in CleanupSettings).
OLD_TRACE_JSON_CLEANUP = {
    "width_mm": 1200.0, "height_mm": 400.0, "remove_colour": None, "cleanup": True,
    "cleanup_action": "remove", "min_detail_width": 1.0, "min_region_area": 4.0,
    "remove_below_width": 1.0,
}
# 3fe9f57's web client request body ``trace``: its ``DEFAULT_TRACE_SETTINGS`` (PipelineControls.tsx).
CLIENT_3FE9F57 = {
    "overlap": 0, "solid_underlayers": False, "layer_order": None, "length_unit": "per_mille",
    "curve_fit_tolerance": 0.75, "straight_line_max_deviation": 0.8, "minimum_straight_length": 2,
    "corner_analysis_span": 4, "curve_feature_scale": 2, "max_deviation": None,
    "side_smoothness": 0.8, "corner_sharpening_allowance": None, "straight_line_snap": "axis",
    "snap_angle": 2, "node_placement": "prefer_extremes", "nodes_at_bends": True,
    "soft_path_nodes": 5000, "soft_layer_nodes": 50000, "soft_document_nodes": 200000,
}
# A client from before trace lengths had a unit: the trace fields of STORED_2026_09_23's record
# (its pixel lengths with no ``length_unit``, ``corner_threshold``), with the side smoothness
# under its old name ``corner_straightness``. 3fe9f57 rejects it (lengths without a unit).
CLIENT_PIXEL_ERA = {
    "overlap": 0.0, "solid_underlayers": False, "layer_order": None, "corner_threshold": 80.0,
    "corner_straightness": 1.2, "curve_fit_tolerance": 1.6, "straight_line_max_deviation": 1.2,
    "minimum_straight_length": 4.0, "max_deviation": None, "corner_analysis_span": 10.0,
    "soft_path_nodes": 5000, "soft_layer_nodes": 50000, "soft_document_nodes": 200000,
}

# The counting wrapper of ``t3api.trace``, shared by this process and the pipeline workers.
COUNTER_SOURCE = '''
def counting(real, log):
    """``real`` (t3api.trace) wrapped to append one JSON line per call to ``log``: the labels'
    dtype, shape and sha256, and the face ids of its ``face`` events in emission order."""
    import hashlib, json, os
    import numpy as np

    def trace(labels, *args, **kwargs):
        array = np.ascontiguousarray(np.asarray(labels))
        entry = {"pid": os.getpid(), "dtype": str(array.dtype), "shape": list(array.shape),
                 "sha256": hashlib.sha256(array.tobytes()).hexdigest(), "faces": []}
        on_event = kwargs.get("on_event", args[1] if len(args) > 1 else None)
        if on_event is not None:
            def relay(kind, payload, _on_event=on_event):
                if kind == "face":
                    entry["faces"].append(payload["face"])
                return _on_event(kind, payload)
            if "on_event" in kwargs:
                kwargs["on_event"] = relay
            else:
                args = (args[0], relay, *args[2:])
        try:
            return real(labels, *args, **kwargs)
        finally:
            with open(log, "a") as handle:
                handle.write(json.dumps(entry) + "\\n")
    return trace
'''
SITECUSTOMIZE = COUNTER_SOURCE + '''
import os as _os
if _os.environ.get("VINYLGEN_T3_CALL_LOG"):
    import t3api as _t3api
    _t3api.trace = counting(_t3api.trace, _os.environ["VINYLGEN_T3_CALL_LOG"])
'''


def labels_sha256(labels: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(labels.astype(np.int16)).tobytes()).hexdigest()


def fixture_labels() -> np.ndarray:
    return np.load(FIXTURE / "labels.npy")


class T3Calls:
    """Every ``t3api.trace`` call made while active, in this process or in any Python process it
    starts (the pipeline workers): one dict per call, in the order the calls finished."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.site = root / "site"
        self.site.mkdir()
        (self.site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        self.log = root / "t3-calls.jsonl"
        self.log.touch()
        namespace: dict = {}
        exec(COUNTER_SOURCE, namespace)  # noqa: S102 - the wrapper the workers run, verbatim
        self._wrapper = namespace["counting"](t3api.trace, str(self.log))
        self._stack = contextlib.ExitStack()

    def __enter__(self) -> "T3Calls":
        path = os.pathsep.join(filter(None, [str(self.site), os.environ.get("PYTHONPATH")]))
        self._stack.enter_context(mock.patch.dict(
            os.environ, {"VINYLGEN_T3_CALL_LOG": str(self.log), "PYTHONPATH": path}))
        self._stack.enter_context(mock.patch.object(t3api, "trace", self._wrapper))
        return self

    def __exit__(self, *exc) -> None:
        self._stack.close()

    def all(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]


def svg_paths(svg_bytes: bytes) -> dict[str, tuple[str, str]]:
    """Each path of an edge SVG by its ``d``: (its layer's id, its ``sodipodi:nodetypes``)."""
    root = ET.fromstring(svg_bytes)
    return {path.attrib["d"]: (layer.attrib.get("id"), path.attrib.get(f"{{{SODIPODI}}}nodetypes"))
            for layer in root.findall(f"{{{SVG}}}g") for path in layer.findall(f"{{{SVG}}}path")}


class _FaceEventChecks:
    """Card section 5's face event, checked against the finished trace and the call's own order."""

    FACE_KEYS = {"type", "face", "label", "rings", "nodes"}

    def assert_face_events(self, test: unittest.TestCase, faces: list[dict], call: dict,
                           edge_svg: bytes) -> None:
        palette = fixture_palette()
        paths = {row["face"]: row for row in json.loads((FIXTURE / "paths.json").read_text())}
        drawn = svg_paths(edge_svg)
        pm = t3api.read_planar_map((FIXTURE / "map.json").read_text())
        test.assertEqual(len(faces), sum(1 for face in pm["faces"] if face["rings"]),
                         "one face event per drawn face of the planar map")
        test.assertEqual(len(faces), 28)
        for event in faces:
            test.assertEqual(set(event), self.FACE_KEYS, event)
            test.assertEqual(event["type"], "face")
        test.assertEqual([event["face"] for event in faces], call["faces"],
                         "relayed as they land: in the order t3 emitted them")
        for event in faces:
            row = paths[event["face"]]
            test.assertIsInstance(event["rings"], str, "rings as one path-data string")
            test.assertIsInstance(event["nodes"], str, "nodes as one nodetypes string")
            test.assertEqual((event["label"], event["rings"], event["nodes"]),
                             (row["label"], row["d"], row["nodetypes"]), event["face"])
            layer = f"layer-{slug(palette[event['label']]['name'])}"
            test.assertEqual(drawn.get(event["rings"]), (layer, event["nodes"]),
                             f"face {event['face']} is the finished traced-edge.svg's path")


# ----------------------------------------------------------------------------- the settings

class TraceSettingsFieldsTest(unittest.TestCase):
    """``TraceSettings`` keeps the three soft node limits; every other field of section 5's table
    is retired; the record upgrade drops them; the pixel-era conversion is gone."""

    def test_trace_settings_keeps_only_the_three_soft_node_limits_at_the_forms_limits(self):
        from vinylgen.contracts import TraceSettings

        self.assertEqual({field.name for field in dataclasses.fields(TraceSettings)}, set(KEPT))
        self.assertEqual(dataclasses.asdict(TraceSettings()), FORM_LIMITS)

    def test_every_other_field_of_section_5_joins_the_retired_fields(self):
        from vinylgen.contracts import RETIRED_TRACE_FIELDS

        retired = set(RETIRED_TRACE_FIELDS)
        self.assertLessEqual(set(SECTION_5_RETIRED) | {"corner_threshold"}, retired)
        self.assertFalse(retired & set(KEPT))

    def test_the_upgrade_drops_every_retired_and_renamed_field_and_keeps_the_limits(self):
        from vinylgen.contracts import TraceSettings, upgrade_trace_record

        gone = set(SECTION_5_RETIRED) | set(EARLIER_NAMES)
        for name, record in (("2026-08-23", STORED_2026_08_23["trace"]),
                             ("2026-09-23", STORED_2026_09_23["trace"]),
                             ("3fe9f57", RECORD_3FE9F57), ("pixel-era client", CLIENT_PIXEL_ERA)):
            with self.subTest(record=name):
                upgraded = upgrade_trace_record(dict(record))
                self.assertFalse(set(upgraded) & gone, sorted(set(upgraded) & gone))
                self.assertEqual({key: upgraded[key] for key in KEPT},
                                 {key: record[key] for key in KEPT})
        self.assertEqual(TraceSettings(**upgrade_trace_record(dict(RECORD_3FE9F57))),
                         TraceSettings())

    def test_the_pixel_era_conversion_is_gone(self):
        import vinylgen.contracts as contracts
        import vinylgen.web.pipeline as pipeline

        self.assertIsNone(importlib.util.find_spec("vinylgen.trace_lengths"),
                          "vinylgen.trace_lengths is deleted")
        for module, name in ((pipeline, "with_trace_length_units"), (pipeline, "is_pixel_record"),
                             (pipeline, "pixel_record_in_units"), (contracts, "is_pixel_record"),
                             (contracts, "pixel_record_in_units")):
            self.assertFalse(hasattr(module, name), f"{module.__name__}.{name}")


class _WebTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()


async def run_job(test: unittest.TestCase, api: _Api, kind: str, project_id: str,
                  revision_id: str, **body) -> str:
    """A pipeline job run to its end through the web API; the result revision's id."""
    response = await api.client.post("/api/jobs", json={
        "kind": kind, "project_id": project_id, "revision_id": revision_id, **body})
    test.assertEqual(response.status_code, 202, response.text)
    deadline = time.monotonic() + TRACE_BOUND_S
    while time.monotonic() < deadline:
        job = (await api.client.get(f"/api/jobs/{response.json()['id']}")).json()
        if job["status"] not in {"queued", "running"}:
            test.assertEqual(job["status"], "succeeded", job)
            return job["result_revision_id"]
        await asyncio.sleep(0.05)
    test.fail(f"the {kind} job did not finish")


async def effective(api: _Api, revision_id: str, kind: str) -> bytes:
    rows = (await api.client.get(f"/api/revisions/{revision_id}/effective-artifacts")).json()
    matching = [row for row in rows if row["kind"] == kind]
    if len(matching) != 1:
        raise AssertionError(f"one effective {kind} artifact of {revision_id}: {rows}")
    return (await api.client.get(matching[0]["download_url"])).content


class StoredRecordsTest(_WebTest):
    """Old trace revisions' ``settings_json``, stored by the store, are served with the retired
    fields dropped and the limits kept, never rejected."""

    async def test_old_revisions_settings_load_with_the_retired_fields_dropped(self):
        subject = build_subject(self.root / "subject")
        gone = set(SECTION_5_RETIRED) | set(EARLIER_NAMES)
        async with _Api(self, self.root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            store = api.app.state.store
            stored = {}
            parent = palette_revision
            for name, settings in (("2026-08-23", STORED_2026_08_23),
                                   ("2026-09-23", STORED_2026_09_23),
                                   ("3fe9f57", STORED_3FE9F57)):
                revision = store.create_revision(
                    project_id, parent_revision_id=parent, kind="trace",
                    message="Deterministic trace pipeline", settings=settings)
                stored[revision["id"]] = (name, settings["trace"])
                parent = revision["id"]
            project = await api.client.get(f"/api/projects/{project_id}")
            listed = await api.client.get(f"/api/projects/{project_id}/revisions")
            self.assertEqual(project.status_code, 200, project.text)
            self.assertEqual(listed.status_code, 200, listed.text)
            for served in (project.json()["revisions"], listed.json()):
                by_id = {row["id"]: row for row in served}
                for revision_id, (name, record) in stored.items():
                    with self.subTest(record=name):
                        trace = by_id[revision_id]["settings"]["trace"]
                        self.assertFalse(set(trace) & gone, sorted(set(trace) & gone))
                        self.assertEqual({key: trace[key] for key in KEPT},
                                         {key: record[key] for key in KEPT})


class OldClientRequestTest(_WebTest):
    """An old client's trace request, carrying retired fields, is accepted (``TraceJobSettings``
    keeps ``extra="forbid"`` and its upgrade drops them first) and traced, and the revision
    records the kept fields only."""

    async def test_a_trace_request_with_retired_fields_is_accepted_and_records_the_limits_only(self):
        subject = build_subject(self.root / "subject")
        async with _Api(self, self.root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            for name, request in (("3fe9f57 client", CLIENT_3FE9F57),
                                  ("pixel-era client", CLIENT_PIXEL_ERA)):
                with self.subTest(client=name):
                    traced = await run_job(self, api, "trace", project_id, palette_revision,
                                           trace=request)
                    recorded = api.app.state.store.get_revision(traced)["settings"]["trace"]
                    self.assertEqual(recorded, {key: request[key] for key in KEPT})


class CliConfigTest(unittest.TestCase):
    """``cli_trace --config`` on an old ``trace.json`` reads its ``settings`` through the same
    upgrade: the retired fields are dropped, never rejected; its cleanup fields still give the
    cleanup settings, as now."""

    def test_an_old_trace_json_configures_a_trace_with_the_retired_fields_dropped(self):
        from vinylgen.cli_trace import main

        SCRATCH.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            root = Path(name)
            subject = build_subject(root / "subject")
            config = root / "old-trace.json"
            config.write_text(json.dumps({"settings": OLD_TRACE_JSON_SETTINGS}, indent=2) + "\n")
            argv = ["svg_trace.py", str(subject), "--config", str(config)]
            with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                main()
            audit = json.loads((subject / "trace.json").read_text())
            self.assertEqual(audit["settings"], {key: OLD_TRACE_JSON_SETTINGS[key] for key in KEPT})
            self.assertEqual(audit["cleanup_settings"], OLD_TRACE_JSON_CLEANUP)
            self.assertEqual((audit["mode"], audit["physical_dimensions_mm"]),
                             ("physical", [1200.0, 400.0]))
            self.assertEqual((subject / "map.json").read_bytes(),
                             (FIXTURE / "map.json").read_bytes())


# ----------------------------------------------------------------------------- re-export and faces

class ReexportTest(unittest.TestCase, _FaceEventChecks):
    """Re-exporting without a trace, through the web pipeline's jobs on one project:

    palette revision (the test array) -> cleanup 1 (changes no pixel) -> trace 1 -> trace 2 (a
    setting change, on trace 1) -> cleanup 2 (removes the smallest region, so new labels) ->
    trace 3; and trace 4 on a stored copy of trace 1 whose records name an earlier t3api build.

    Trace 1's job SSE stream is read from before its worker starts (the worker launch waits until
    the stream has subscribed), so every event the job relays is in it."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.out: dict = {}
        cls.error: BaseException | None = None
        try:
            asyncio.run(cls._chain(Path(cls._temp.name)))
        except BaseException as error:  # noqa: BLE001 - re-raised by each test
            cls.error = error

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def chain(self) -> dict:
        if self.error is not None:
            raise self.error
        return self.out

    @classmethod
    async def _chain(cls, root: Path) -> None:
        import vinylgen.web.pipeline as pipeline

        checks = unittest.TestCase()
        out = cls.out
        subject = build_subject(root / "subject")
        with T3Calls(root / "calls") as calls:
            async with _Api(checks, root / "data") as api:
                store = api.app.state.store
                project_id, palette_revision = await api.palette_revision(subject)
                cleanup_1 = await run_job(checks, api, "cleanup", project_id, palette_revision,
                                          cleanup={})

                # Trace 1, its SSE stream subscribed before the worker starts.
                attached = threading.Event()
                jobs = api.app.state.jobs
                real_subscribe, real_run_worker = jobs.subscribe, pipeline.run_worker

                async def subscribe(job_id):
                    async for event in real_subscribe(job_id):
                        attached.set()
                        yield event

                def run_worker(*args, **kwargs):
                    attached.wait(TRACE_BOUND_S)
                    return real_run_worker(*args, **kwargs)

                before = len(calls.all())
                with mock.patch.object(jobs, "subscribe", subscribe), \
                        mock.patch.object(pipeline, "run_worker", run_worker):
                    started = await api.client.post("/api/jobs", json={
                        "kind": "trace", "project_id": project_id, "revision_id": cleanup_1})
                    checks.assertEqual(started.status_code, 202, started.text)
                    job_id = started.json()["id"]
                    stream = await asyncio.wait_for(
                        api.client.get(f"/api/jobs/{job_id}/events"), TRACE_BOUND_S)
                job = (await api.client.get(f"/api/jobs/{job_id}")).json()
                checks.assertEqual(job["status"], "succeeded", job)
                trace_1 = job["result_revision_id"]
                out["trace_1"] = await cls._traced(api, trace_1, calls.all()[before:])
                out["trace_1"]["stream"] = stream.text

                # Trace 2: a setting change on trace 1, the same labels and build.
                before = len(calls.all())
                trace_2 = await run_job(checks, api, "trace", project_id, trace_1,
                                        trace={"soft_path_nodes": 1})
                out["trace_2"] = await cls._traced(api, trace_2, calls.all()[before:])

                # Cleanup 2 removes the smallest four-connected region of the test array (one
                # px^2 larger than its area, with no physical size): new labels.
                labels = fixture_labels()
                four = ndimage.generate_binary_structure(2, 1)
                smallest = min(int(np.bincount(ndimage.label(labels == value, structure=four)[0]
                                               .ravel())[1:].min())
                               for value in np.unique(labels))
                cleanup_2 = await run_job(checks, api, "cleanup", project_id, trace_2,
                                          cleanup={"cleanup": True,
                                                   "min_region_area": float(smallest + 1)})
                with np.load(io.BytesIO(await effective(api, cleanup_2, "segmentation"))) as data:
                    out["cleanup_2_labels_sha256"] = labels_sha256(data["labels"])
                before = len(calls.all())
                trace_3 = await run_job(checks, api, "trace", project_id, cleanup_2)
                out["trace_3"] = await cls._traced(api, trace_3, calls.all()[before:])

                # Trace 4: on a trace revision holding trace 1's artifacts as an earlier build
                # wrote them (its map.json header and trace.json naming that build).
                earlier = store.create_revision(
                    project_id, parent_revision_id=cleanup_1, kind="trace",
                    message="Deterministic trace pipeline",
                    settings=store.get_revision(trace_1)["settings"])
                map_text = cls._artifact_bytes(store, trace_1, "traced-map").decode()
                stamp = f'"build":{json.dumps(t3api.BUILD_ID)}'
                checks.assertEqual(map_text.count(stamp), 1, "map.json names its build once")
                earlier_map = map_text.replace(stamp, f'"build":{json.dumps(EARLIER_BUILD_ID)}')
                audit = json.loads(cls._artifact_bytes(store, trace_1, "trace-audit"))
                audit["backend"]["build"] = EARLIER_BUILD_ID
                audit["determinism"]["map_sha256"] = hashlib.sha256(
                    earlier_map.encode()).hexdigest()
                replaced = {"traced-map": earlier_map.encode(),
                            "trace-audit": (json.dumps(audit, indent=2) + "\n").encode()}
                for row in store.list_artifacts(trace_1):
                    content = replaced.get(row["kind"])
                    if content is None:
                        content = store.artifact_path(row["id"]).read_bytes()
                    store.add_artifact(earlier["id"], kind=row["kind"], filename=row["filename"],
                                       media_type=row["media_type"], source=io.BytesIO(content))
                out["earlier_map"] = earlier_map.encode()
                before = len(calls.all())
                trace_4 = await run_job(checks, api, "trace", project_id, earlier["id"])
                out["trace_4"] = await cls._traced(api, trace_4, calls.all()[before:])

    @staticmethod
    def _artifact_bytes(store, revision_id: str, kind: str) -> bytes:
        rows = [row for row in store.list_artifacts(revision_id) if row["kind"] == kind]
        if len(rows) != 1:
            raise AssertionError(f"one {kind} artifact of {revision_id}: {rows}")
        return store.artifact_path(rows[0]["id"]).read_bytes()

    @classmethod
    async def _traced(cls, api: _Api, revision_id: str, calls: list[dict]) -> dict:
        own = {row["kind"]: row for row in
               (await api.client.get(f"/api/revisions/{revision_id}/artifacts")).json()}
        audit = (json.loads((await api.client.get(own["trace-audit"]["download_url"])).content)
                 if "trace-audit" in own else None)
        return {"calls": calls, "map": await effective(api, revision_id, "traced-map"),
                "edge": await effective(api, revision_id, "traced-edge"), "audit": audit}

    def test_trace_1_calls_t3_once_and_keeps_the_fixtures_map(self):
        """Pins card 2's trace job on this chain (passes on 3fe9f57): the baseline the reuse is
        measured against."""
        trace = self.chain()["trace_1"]
        self.assertEqual(len(trace["calls"]), 1)
        self.assertEqual(trace["calls"][0]["sha256"], labels_sha256(fixture_labels()))
        self.assertEqual(trace["map"], (FIXTURE / "map.json").read_bytes())

    def test_a_trace_on_the_same_labels_and_build_reuses_the_parents_map_without_a_trace(self):
        chain = self.chain()
        trace_1, trace_2 = chain["trace_1"], chain["trace_2"]
        self.assertEqual(trace_2["calls"], [], "t3api.trace is not called")
        self.assertEqual(trace_2["map"], trace_1["map"], "map.json reused byte for byte")
        self.assertEqual(trace_2["edge"], trace_1["edge"], "the edge SVG of the same map")

    def test_the_reused_traces_audit_is_rebuilt_with_its_own_settings(self):
        """The audit is rebuilt: trace 2's own ``trace.json`` records its settings, vouches for
        the reused map and warns at its own path-node limit (card 3's warning)."""
        chain = self.chain()
        audit = chain["trace_2"]["audit"]
        self.assertIsNotNone(audit, "trace 2 writes its own trace.json")
        self.assertEqual(audit["settings"], {**FORM_LIMITS, "soft_path_nodes": 1})
        self.assertEqual(audit["determinism"]["map_sha256"],
                         hashlib.sha256(chain["trace_1"]["map"]).hexdigest())
        self.assertTrue(any("path-node limit" in warning for warning in audit["warnings"]),
                        audit["warnings"])
        self.assertTrue(audit["validation"]["edge"]["passed"])

    def test_different_labels_trace_again(self):
        """Passes on 3fe9f57, which traces every time: it pins that the reuse never extends to
        changed labels."""
        chain = self.chain()
        self.assertNotEqual(chain["cleanup_2_labels_sha256"], labels_sha256(fixture_labels()),
                            "cleanup 2 changed the labels")
        calls = chain["trace_3"]["calls"]
        self.assertEqual(len(calls), 1, "a new cleanup's labels are traced")
        self.assertEqual(calls[0]["sha256"], chain["cleanup_2_labels_sha256"])

    def test_a_different_build_traces_again(self):
        """Passes on 3fe9f57, which traces every time: it pins that a map recorded by an earlier
        t3 build is never reused."""
        chain = self.chain()
        calls = chain["trace_4"]["calls"]
        self.assertEqual(len(calls), 1, "a map from an earlier build is not reused")
        self.assertEqual(calls[0]["sha256"], labels_sha256(fixture_labels()))
        self.assertNotEqual(chain["trace_4"]["map"], chain["earlier_map"])
        self.assertEqual(chain["trace_4"]["map"], (FIXTURE / "map.json").read_bytes())

    def test_the_trace_jobs_sse_stream_carries_t3s_face_events(self):
        trace = self.chain()["trace_1"]
        events = []
        for block in trace["stream"].split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:"):].strip()))
        faces = [event for event in events if isinstance(event, dict) and event.get("type") == "face"]
        self.assertEqual(len(trace["calls"]), 1)
        self.assert_face_events(self, faces, trace["calls"][0], trace["edge"])


class WorkerFaceLinesTest(unittest.TestCase, _FaceEventChecks):
    """``pipeline_worker`` relays t3's ``face`` events as JSON lines
    ``{"type": "face", "face", "label", "rings", "nodes"}`` while it traces."""

    def test_the_worker_relays_each_face_event_as_a_json_line(self):
        SCRATCH.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            root = Path(name)
            subject = build_subject(root / "subject")
            spec = {"op": "trace", "subject": str(subject), "settings": {}, "colour_names": {},
                    "cleanup": None, "removed_index": None}
            with T3Calls(root / "calls") as calls:
                completed = subprocess.run(
                    [sys.executable, "-m", "vinylgen.web.pipeline_worker"], cwd=ROOT,
                    env=dict(os.environ), input=json.dumps(spec), capture_output=True, text=True,
                    timeout=TRACE_BOUND_S)
            self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
            lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
            self.assertEqual(lines[-1], {"type": "done"})
            recorded = calls.all()
            self.assertEqual(len(recorded), 1, "the worker traced once")
            faces = [line for line in lines if line.get("type") == "face"]
            self.assert_face_events(self, faces, recorded[0],
                                    (subject / "traced-edge.svg").read_bytes())


# ----------------------------------------------------------------------------- the windowed preview

class TracePreviewRetiredTest(unittest.TestCase):
    """The windowed trace preview is deleted; what the cleanup preview shares moves to
    ``cleanup_window``."""

    def test_the_trace_preview_route_is_gone(self):
        from vinylgen.web.app import create_app
        from vinylgen.web.config import WebConfig

        SCRATCH.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as name:
            app = create_app(WebConfig(Path(name) / "data"))
            paths = [getattr(route, "path", "") for route in app.routes]
            self.assertFalse([path for path in paths if "trace-preview" in path], paths)

            async def post() -> int:
                import httpx

                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test") as client:
                    response = await client.post("/api/revisions/any/trace-preview", json={})
                await app.state.jobs.shutdown()
                return response.status_code

            self.assertIn(asyncio.run(post()), (404, 405))

    def test_window_and_margin_window_live_in_cleanup_window(self):
        from vinylgen.cleanup_window import Window, margin_window

        self.assertEqual(Window.__module__, "vinylgen.cleanup_window")
        self.assertEqual(margin_window.__module__, "vinylgen.cleanup_window")
        self.assertIsNone(importlib.util.find_spec("vinylgen.trace_window"),
                          "vinylgen.trace_window is deleted")


if __name__ == "__main__":
    unittest.main()
