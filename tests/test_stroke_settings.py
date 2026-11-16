"""The stroke trace on the trace step: vinylgen's backend passes the line colours, the width mode, the tier count and
the fallback to t3api 0.30, exports the strokes above the faces, and validates the drawing with them.

The contract is the tracer card ``generations/tube/cards/stroke-trace/card.md`` (decision 7's vinylgen list, section
8's vinylgen list, section 9 step 5; worklog task ``stroke-trace-line-art-colours-traced-as-centrelines-at-a-fit``),
on the plumbing the half-widths (``tests/test_kind_tolerance_settings.py``) and the minimum sweep
(``tests/test_min_sweep_settings.py``) laid. Frozen: the builder changes no test and no fixture here.

The names the card leaves open are settled here:

- ``TraceSettings`` and the web model ``TraceJobSettings`` gain ``line_colours`` (the palette indices traced as lines,
  ``[]`` by default: none), ``width_mode`` (``stroke`` / ``colour`` / ``tiers``, ``stroke`` by default), ``tiers``
  (``auto`` or an int >= 1, ``auto`` by default) and ``stroke_fallback`` (``none`` / ``face`` / ``best``, ``none`` by
  default). A record from before the fields reads them at the defaults. The web model refuses a line colour that is
  not an index >= 0 or is repeated, an unknown mode or fallback, a tier count below 1 or not an int (a bool is not
  one), and the route answers 422 and starts no job. The mode, the tier count and the fallback are form state: the
  model keeps them whatever ``line_colours`` holds (the panel hides their controls while no colour is switched).
- ``tracing.t3_setting`` gives t3api's canonical setting with ``line_labels`` (sorted, unique), ``width_mode``,
  ``tiers`` (in tiers mode only) and ``stroke_fallback`` (only at ``face`` or ``best``) when ``line_colours`` is
  non-empty, as ``TraceSettings.strokes()`` maps them (as ``min_sweeps`` maps the minimum sweep), and today's setting,
  without any of the four keys, when it is empty: with no line colour the other three mean nothing and are not sent.
  t3api's ``normal_setting`` decides the form and refuses an unknown mode or fallback and a tier count below 1 with a
  ``ValueError`` naming the key. ``tracer_info``'s default setting has no key.
- The export: ``svg_writer.add_stroke_group(root, pm, palette)`` appends, after every face layer, the stroke group
  ``<g id="strokes" fill="none">`` holding one ``<path d=... stroke=... stroke-width=... stroke-linecap=...
  stroke-linejoin=.../>`` per stroke in the map's order and one ``<path d="M x y l0 0" ... stroke-linecap="round"/>``
  per dot, each with the ``d``, ``stroke``, ``stroke-width``, ``stroke-linecap`` and ``stroke-linejoin`` t3api's
  ``faces_svg_from_planar_map`` writes (the fixture ``faces.svg``), each with a unique id, and returns its counts
  ``{"strokes": n, "dots": m}``; a map with no stroke and no dot gets no group. ``trace_subject`` writes it into
  ``traced-edge.svg`` as the root's last child. ``validate_svg`` passes on such a file: the stroke group is not a
  layer and its open paths are not faces.
- The validation render paints the strokes on top of the painted faces, so the semantic audit passes on S13's array
  traced with the ink as a line colour.

Every trace runs through the code a user reaches: ``trace_subject`` on a subject built from the fixture
``tests/fixtures/stroke-seam`` (S13's seam array as palette indices: Paper 0, Disc 1, Ink 2; ``make.py``), and the
live app's jobs over the in-process ASGI transport (each trace in its ``pipeline_worker`` subprocess, ``t3api.trace``
counted by ``test_t3_settings.T3Calls``).

These tests need t3api 0.30 (the four keys in ``normal_setting``, the strokes in the map) and the build that reads the
fields, so they run red until the builder lands both.

The mutants each test is meant to kill (run at build time):

- the keys dropped on the way to t3api (``t3_setting`` passing only ``half``): ``test_t3_setting_carries_the_four_keys``
  and the fallback traces (the header without ``stroke_fallback``);
- a fallback value not reaching t3api (every value sent as ``none``, or ``face`` and ``best`` swapped):
  ``test_each_fallback_value_reaches_t3api`` (the setting t3api receives and the map header);
- the keys written at the default (``line_labels: []`` or ``width_mode`` sent with no line colour):
  ``test_t3_setting_carries_the_four_keys``, ``test_a_trace_without_a_line_colour_is_todays`` and the chain's trace 4;
- the keys left out of the reuse comparison (a ``best`` trace reusing a ``none`` map): the chain's trace 3;
- the group written before the faces, or no group: ``test_the_export_writes_the_stroke_group_after_the_faces`` and
  ``test_the_traced_edge_svg_holds_the_strokes_above_the_faces``;
- the strokes left out of the validation render (the semantic audit failing on the ink):
  ``test_the_validation_passes_on_the_seam_array``;
- ``validate_svg`` counting the stroke group as a layer or its open paths as faces: the same two export tests;
- an out-of-range value accepted: ``test_bad_values_are_refused`` and the route test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import numpy as np
import t3api
from PIL import Image

from test_nib_embed import _IdentityApi
from test_t3_settings import FORM_LIMITS, T3Calls, effective, labels_sha256, run_job
from test_t3_trace import SCRATCH
from vinylgen.contracts import SegmentationBundle, TraceSettings, upgrade_trace_record
from vinylgen.tracing import trace_subject

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "stroke-seam"
SVG = "http://www.w3.org/2000/svg"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
KINDS = ("line", "arc", "ellipse", "cubic")
HALF_DEFAULTS = {f"half_{kind}": 1.75 for kind in KINDS}
MIN_DEFAULTS = {"min_sweep_arc": 0.0, "min_sweep_ellipse": 0.0}
STROKE_DEFAULTS = {"line_colours": [], "width_mode": "stroke", "tiers": "auto", "stroke_fallback": "none"}
DEFAULT_RECORD = {**FORM_LIMITS, **HALF_DEFAULTS, **MIN_DEFAULTS, **STROKE_DEFAULTS}
SETTING_DEFAULT = {"half": {kind: 1.75 for kind in KINDS}, "A1": 22.5, "A2": 45.0, "L": 4.0}
INK = 2
SETTING_LINE = {**SETTING_DEFAULT, "line_labels": [INK], "width_mode": "stroke"}
SETTING_LINE_BEST = {**SETTING_LINE, "stroke_fallback": "best"}
STROKE_KEYS = ("strokes", "stroke_nodes", "dots", "not_stroke")
STROKE_ATTRS = ("d", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin")
# The fixture's bytes (make.py prints each; make.log keeps them). labels.npy's is of the array's bytes.
LABELS_SHA = "f9bf39eebe14c1dd3462e539c3287f3c3ef21c03edb9fb2d93ef254041e82a1a"
META_SHA = "2b9a40efd10b38464ace8caa303b54775c8347cf713ec4cf6b06c0b0c08b33dd"
FACES_SHA = "f35ac24f36ed178918c739f574d9cc50ae939411ecc6c1a081b708416d376101"


def record(**fields) -> dict:
    """The trace settings record with the named fields changed."""
    unknown = set(fields) - set(DEFAULT_RECORD)
    if unknown:
        raise ValueError(f"not a trace setting: {sorted(unknown)}")
    return {**DEFAULT_RECORD, **fields}


def fixture_labels() -> np.ndarray:
    return np.load(FIXTURE / "labels.npy")


def fixture_palette() -> list[dict]:
    return json.loads((FIXTURE / "meta.json").read_text())["palette"]


def fixture_map() -> dict:
    return json.loads((FIXTURE / "map.json").read_text())


def header_setting(map_bytes: bytes | str) -> dict:
    return json.loads(map_bytes)["setting"]


def drawing(pm: dict) -> str:
    """A planar map's drawing, the strokes included: everything but its header, as JSON text."""
    keys = ["W", "H", "nodes", "edges", "faces", *[k for k in STROKE_KEYS if k in pm]]
    return json.dumps({k: pm[k] for k in keys}, sort_keys=True)


def stroke_paths(svg_bytes: bytes | str) -> list[dict]:
    """The attributes of each path of an SVG's stroke group, in order: the five stroke attributes."""
    root = ET.fromstring(svg_bytes)
    groups = [g for g in root.findall(f"{{{SVG}}}g") if g.attrib.get("id") == "strokes"]
    if not groups:
        return []
    assert len(groups) == 1, "one stroke group"
    return [{k: p.attrib.get(k) for k in STROKE_ATTRS} for p in groups[0].findall(f"{{{SVG}}}path")]


def expected_stroke_paths() -> list[dict]:
    return stroke_paths((FIXTURE / "faces.svg").read_bytes())


def build_seam_subject(root: Path, name: str = "stroke-seam") -> Path:
    """A subject folder holding S13's array as the palette stage writes one (as ``test_t3_trace.build_subject``):
    ``source.png``, ``flattened.png``, ``palette.json`` and the v3 ``segmentation.npz`` whose labels are the fixture's."""
    subject = root / name
    subject.mkdir(parents=True)
    labels = fixture_labels()
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


class _Recorder:
    """``t3api.trace`` wrapped: each call's ``setting`` argument, the call passed through."""

    def __init__(self) -> None:
        self.settings: list = []
        self._real = t3api.trace

    def __call__(self, labels, *args, **kwargs):
        self.settings.append(kwargs.get("setting", args[0] if args else None))
        return self._real(labels, *args, **kwargs)


def traced(root: Path, name: str, settings: TraceSettings) -> dict:
    """``trace_subject`` on a fresh seam subject at ``settings``: the audit, the map, the edge SVG, the subject folder
    and the settings each ``t3api.trace`` call received."""
    subject = build_seam_subject(root, name)
    recorder = _Recorder()
    with mock.patch.object(t3api, "trace", recorder):
        audit = trace_subject(subject, settings, {})
    return {"audit": audit, "map": json.loads((subject / "map.json").read_text()),
            "svg": (subject / "traced-edge.svg").read_bytes(), "subject": subject, "settings": recorder.settings}


# ----------------------------------------------------------------------------- the fixture

class StrokeSeamFixtureTest(unittest.TestCase):
    """The fixture is what ``make.py`` wrote: S13's array as palette indices, its map with one stroke."""

    def test_the_fixture_is_the_committed_one(self):
        labels = fixture_labels()
        self.assertEqual(labels.dtype, np.int16)
        self.assertEqual(labels.shape, (200, 200))
        self.assertEqual(hashlib.sha256(np.ascontiguousarray(labels).tobytes()).hexdigest(), LABELS_SHA)
        self.assertEqual(sorted(set(labels.ravel().tolist())), [0, 1, 2])
        self.assertEqual(hashlib.sha256((FIXTURE / "meta.json").read_bytes()).hexdigest(), META_SHA)
        self.assertEqual([(r["index"], r["name"], r["hex"]) for r in fixture_palette()],
                         [(0, "Paper", "#F4E6AD"), (1, "Disc", "#E4B33A"), (2, "Ink", "#050301")])
        self.assertEqual(hashlib.sha256((FIXTURE / "faces.svg").read_bytes()).hexdigest(), FACES_SHA)
        pm = fixture_map()
        self.assertEqual(pm["setting"], SETTING_LINE)
        self.assertEqual(pm["build"], "0.30.0-source")
        self.assertEqual(list(pm)[-4:], list(STROKE_KEYS))
        self.assertEqual(len(pm["strokes"]), 1)
        self.assertEqual(pm["edges"][0]["under"], {"stroke": 0, "pieces": [0, 1]})
        self.assertEqual(pm["edges"][0]["pieces"], pm["strokes"][0]["pieces"], "the seam: the edge under the stroke")
        self.assertEqual([len(pm[k]) for k in STROKE_KEYS], [1, 0, 0, 0])
        want = expected_stroke_paths()
        self.assertEqual(len(want), 1)
        self.assertEqual({k: want[0][k] for k in STROKE_ATTRS[1:]},
                         {"stroke": "#050301", "stroke-width": "5.963051461783363", "stroke-linecap": "round",
                          "stroke-linejoin": "round"})


# ----------------------------------------------------------------------------- the settings model

class StrokeSettingsModelTest(unittest.TestCase):
    """``contracts.TraceSettings`` carries the four fields at their defaults, and an older record reads them so."""

    def test_the_defaults_carry_the_four_fields(self):
        self.assertEqual(dataclasses.asdict(TraceSettings()), DEFAULT_RECORD)

    def test_a_record_from_before_the_fields_reads_them_at_the_defaults(self):
        old = {**FORM_LIMITS, **HALF_DEFAULTS, **MIN_DEFAULTS, "half_arc": None}
        settings = TraceSettings(**upgrade_trace_record(old))
        self.assertEqual(dataclasses.asdict(settings), record(half_arc=None))

    def test_the_fields_round_trip(self):
        settings = TraceSettings(line_colours=[INK, 0], width_mode="tiers", tiers=3, stroke_fallback="best")
        self.assertEqual(dataclasses.asdict(settings),
                         record(line_colours=[INK, 0], width_mode="tiers", tiers=3, stroke_fallback="best"))


class StrokeT3SettingTest(unittest.TestCase):
    """``tracing.t3_setting`` passes the four keys to t3api's canonical setting only with a line colour."""

    def test_t3_setting_carries_the_four_keys(self):
        from vinylgen.tracing import t3_setting
        self.assertEqual(t3_setting(TraceSettings()), SETTING_DEFAULT)
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK])), SETTING_LINE)
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK, 0, INK])),
                         {**SETTING_DEFAULT, "line_labels": [0, INK], "width_mode": "stroke"},
                         "sorted, unique")
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], width_mode="colour")),
                         {**SETTING_DEFAULT, "line_labels": [INK], "width_mode": "colour"})
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], width_mode="tiers")),
                         {**SETTING_DEFAULT, "line_labels": [INK], "width_mode": "tiers", "tiers": "auto"})
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], width_mode="tiers", tiers=3)),
                         {**SETTING_DEFAULT, "line_labels": [INK], "width_mode": "tiers", "tiers": 3})
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], stroke_fallback="face")),
                         {**SETTING_LINE, "stroke_fallback": "face"})
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], stroke_fallback="best")), SETTING_LINE_BEST)
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], stroke_fallback="none")), SETTING_LINE)
        # Form state beside no line colour is not sent; a tier count outside tiers mode is not sent.
        self.assertEqual(t3_setting(TraceSettings(width_mode="tiers", tiers=3, stroke_fallback="best")),
                         SETTING_DEFAULT)
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], tiers=3, stroke_fallback="face")),
                         {**SETTING_LINE, "stroke_fallback": "face"})
        # Beside the minimum sweep and an off kind.
        self.assertEqual(t3_setting(TraceSettings(line_colours=[INK], half_cubic=None, min_sweep_arc=270.0)),
                         {**SETTING_LINE, "half": {**SETTING_DEFAULT["half"], "cubic": None},
                          "min_sweep": {"arc": 270.0, "ellipse": 0.0}})

    def test_a_bad_value_is_refused_by_t3api_naming_the_key(self):
        from vinylgen.tracing import t3_setting
        for fields, key in ((dict(line_colours=[INK], width_mode="thick"), "width_mode"),
                            (dict(line_colours=[INK], stroke_fallback="soft"), "stroke_fallback"),
                            (dict(line_colours=[INK], width_mode="tiers", tiers=0), "tiers"),
                            (dict(line_colours=[-1]), "line_labels")):
            with self.subTest(**fields):
                with self.assertRaises(ValueError) as caught:
                    t3_setting(TraceSettings(**fields))
                self.assertIn(key, str(caught.exception))

    def test_tracer_info_names_the_default_without_the_keys(self):
        from vinylgen.tracing import tracer_info
        self.assertEqual(tracer_info()["setting"], SETTING_DEFAULT)


class StrokeJobModelTest(unittest.TestCase):
    """The web model (``vinylgen.web.models.TraceJobSettings``): the four fields at their defaults, each value in its
    range, the mode, tier count and fallback kept whatever ``line_colours`` holds."""

    def model(self):
        from vinylgen.web.models import TraceJobSettings
        return TraceJobSettings

    def test_the_defaults_match_the_contract(self):
        self.assertEqual(self.model()().model_dump(), DEFAULT_RECORD)

    def test_each_value_is_accepted_as_given(self):
        accepted = [
            dict(line_colours=[]), dict(line_colours=[INK]), dict(line_colours=[INK, 0]),
            dict(width_mode="stroke"), dict(width_mode="colour"), dict(width_mode="tiers"),
            dict(tiers="auto"), dict(tiers=1), dict(tiers=3),
            dict(stroke_fallback="none"), dict(stroke_fallback="face"), dict(stroke_fallback="best"),
            dict(width_mode="tiers", tiers=4, stroke_fallback="best"),
            dict(line_colours=[0, 1], width_mode="colour", stroke_fallback="face"),
        ]
        for fields in accepted:
            with self.subTest(**fields):
                self.assertEqual(self.model()(**fields).model_dump(), record(**fields))

    def test_bad_values_are_refused(self):
        from pydantic import ValidationError
        # The guard: the fields exist and take their values, so a refusal below is the value's, not an unknown field's.
        self.assertEqual(self.model()(line_colours=[INK], width_mode="tiers", tiers=2, stroke_fallback="best").model_dump(),
                         record(line_colours=[INK], width_mode="tiers", tiers=2, stroke_fallback="best"))
        refused = [
            dict(line_colours=[-1]), dict(line_colours=[INK, INK]), dict(line_colours=[1.5]), dict(line_colours=[True]),
            dict(line_colours="2"), dict(line_colours=2), dict(line_colours=None), dict(line_colours=["2"]),
            dict(width_mode="thick"), dict(width_mode=3), dict(width_mode=None), dict(width_mode=""),
            dict(tiers=0), dict(tiers=-2), dict(tiers=2.5), dict(tiers="many"), dict(tiers=True), dict(tiers=None),
            dict(tiers="3"),
            dict(stroke_fallback="soft"), dict(stroke_fallback=None), dict(stroke_fallback=1), dict(stroke_fallback=""),
        ]
        for fields in refused:
            with self.subTest(**fields), self.assertRaises(ValidationError):
                self.model()(**fields)


# ----------------------------------------------------------------------------- the trace

class StrokeTraceSubjectTest(unittest.TestCase):
    """``trace_subject`` on the seam subject: the keys reach t3api, the strokes reach the map, the export and the
    validation, and a trace without a line colour is today's. One subject per setting, traced once, shared by the
    class's tests."""

    @classmethod
    def setUpClass(cls) -> None:
        SCRATCH.mkdir(exist_ok=True)
        cls._temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.out: dict = {}
        cls.error: BaseException | None = None
        root = Path(cls._temp.name)
        try:
            for name, settings in (("default", TraceSettings()),
                                   ("line", TraceSettings(line_colours=[INK])),
                                   ("face", TraceSettings(line_colours=[INK], stroke_fallback="face")),
                                   ("best", TraceSettings(line_colours=[INK], stroke_fallback="best")),
                                   ("tiers", TraceSettings(line_colours=[INK], width_mode="tiers", tiers=2))):
                cls.out[name] = traced(root, name, settings)
        except BaseException as error:  # noqa: BLE001 - re-raised by each test
            cls.error = error

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def traces(self) -> dict:
        if self.error is not None:
            raise self.error
        return self.out

    def test_the_line_trace_draws_the_fixtures_map_with_its_stroke(self):
        trace = self.traces()["line"]
        self.assertEqual(trace["settings"], [SETTING_LINE], "t3api.trace is called once, at the canonical setting")
        pm = trace["map"]
        self.assertEqual(pm["setting"], SETTING_LINE)
        self.assertEqual(list(pm)[-4:], list(STROKE_KEYS))
        self.assertEqual(drawing(pm), drawing(fixture_map()), "build 30's drawing, the stroke and the under edge included")
        self.assertEqual(pm["palette"], [[244, 230, 173], [228, 179, 58], [5, 3, 1]])
        self.assertEqual(pm["names"], ["Paper", "Disc", "Ink"])
        audit = trace["audit"]
        self.assertEqual(audit["backend"]["setting"], SETTING_LINE)
        self.assertEqual(audit["settings"], record(line_colours=[INK]))
        self.assertEqual(audit["validation"]["edge"]["planar_map_check"]["violations"], 0)
        self.assertEqual(audit["validation"]["edge"]["read_back"]["differ"], 0)

    def test_the_traced_edge_svg_holds_the_strokes_above_the_faces(self):
        trace = self.traces()["line"]
        root = ET.fromstring(trace["svg"])
        children = list(root)
        layers = [c for c in children if c.attrib.get(f"{{{INKSCAPE}}}groupmode") == "layer"]
        self.assertEqual(len(layers), 3, "one layer per colour, the ink's included")
        self.assertEqual(children[-1].tag, f"{{{SVG}}}g")
        self.assertEqual(children[-1].attrib.get("id"), "strokes", "the stroke group is the root's last child")
        self.assertEqual(children[-1].attrib.get("fill"), "none")
        self.assertEqual(children.index(children[-1]), max(children.index(layer) for layer in layers) + 1,
                         "the group follows the last layer")
        self.assertEqual(stroke_paths(trace["svg"]), expected_stroke_paths(), "t3api's stroke paths")
        ids = [p.attrib.get("id") for p in children[-1].findall(f"{{{SVG}}}path")]
        self.assertTrue(all(ids), ids)
        self.assertEqual(len(ids), len(set(ids)))
        all_ids = [p.attrib.get("id") for p in root.iter(f"{{{SVG}}}path")]
        self.assertEqual(len(all_ids), len(set(all_ids)), "unique across the file")
        edge = trace["audit"]["validation"]["edge"]
        self.assertTrue(edge["passed"], edge)
        self.assertEqual(edge["layer_count"], 3)
        self.assertTrue(edge["expected_fills"])
        self.assertTrue(edge["all_paths_closed"], "the stroke paths are not faces")

    def test_the_validation_passes_on_the_seam_array(self):
        trace = self.traces()["line"]
        semantic = trace["audit"]["validation"]["semantic"]
        self.assertTrue(semantic["passed"], semantic)
        self.assertEqual(trace["audit"]["warnings"], [])
        self.assertFalse((trace["subject"] / "trace-semantic-warnings.png").exists())
        self.assertFalse((trace["subject"] / "trace-missed-details-overlay.png").exists())

    def test_each_fallback_value_reaches_t3api(self):
        traces = self.traces()
        for name, setting in (("line", SETTING_LINE), ("face", {**SETTING_LINE, "stroke_fallback": "face"}),
                              ("best", SETTING_LINE_BEST),
                              ("tiers", {**SETTING_DEFAULT, "line_labels": [INK], "width_mode": "tiers", "tiers": 2})):
            with self.subTest(name=name):
                trace = traces[name]
                self.assertEqual(trace["settings"], [setting])
                self.assertEqual(trace["map"]["setting"], setting, "the map header: the setting t3 traced at")
                self.assertEqual(trace["audit"]["backend"]["setting"], setting)
                self.assertEqual(len(trace["map"]["strokes"]), 1)
                self.assertTrue(trace["audit"]["validation"]["edge"]["passed"])
                self.assertTrue(trace["audit"]["validation"]["semantic"]["passed"])
        self.assertEqual(traces["face"]["audit"]["settings"], record(line_colours=[INK], stroke_fallback="face"))
        self.assertEqual(traces["best"]["audit"]["settings"], record(line_colours=[INK], stroke_fallback="best"))
        self.assertEqual(traces["tiers"]["audit"]["settings"], record(line_colours=[INK], width_mode="tiers", tiers=2))
        # A passing seam ring is the same stroke under every fallback.
        self.assertEqual(traces["face"]["map"]["strokes"], traces["line"]["map"]["strokes"])
        self.assertEqual(traces["best"]["map"]["strokes"], traces["line"]["map"]["strokes"])

    def test_a_trace_without_a_line_colour_is_todays(self):
        trace = self.traces()["default"]
        self.assertEqual(trace["settings"], [SETTING_DEFAULT])
        pm = trace["map"]
        self.assertEqual(pm["setting"], SETTING_DEFAULT)
        for key in STROKE_KEYS:
            self.assertNotIn(key, pm)
        self.assertFalse(any("under" in e for e in pm["edges"]))
        self.assertEqual(stroke_paths(trace["svg"]), [])
        self.assertNotIn(b'id="strokes"', trace["svg"])
        self.assertEqual(trace["audit"]["backend"]["setting"], SETTING_DEFAULT)
        self.assertEqual(trace["audit"]["settings"], DEFAULT_RECORD)
        self.assertTrue(trace["audit"]["validation"]["edge"]["passed"])
        self.assertTrue(trace["audit"]["validation"]["semantic"]["passed"])


class StrokeExportTest(unittest.TestCase):
    """``svg_writer.add_stroke_group`` on the fixture's map, and on a map without strokes."""

    def test_the_export_writes_the_stroke_group_after_the_faces(self):
        from vinylgen.svg_writer import add_face_layer, add_stroke_group, face_areas, face_elements, svg_document, write_svg
        from vinylgen.validation import validate_svg
        pm = fixture_map()
        palette = fixture_palette()
        root = svg_document(pm["W"], pm["H"], None)
        paths = t3api.planar_map_paths(pm)
        areas, elements = face_areas(pm), face_elements(pm)
        for row in palette:
            add_face_layer(root, row, pm, paths=paths, areas=areas, elements=elements)
        before = len(list(root))
        stats = add_stroke_group(root, pm, palette)
        self.assertEqual(stats, {"strokes": 1, "dots": 0})
        children = list(root)
        self.assertEqual(len(children), before + 1, "one group appended")
        group = children[-1]
        self.assertEqual((group.tag, group.attrib.get("id"), group.attrib.get("fill")), (f"{{{SVG}}}g", "strokes", "none"))
        self.assertNotIn(f"{{{INKSCAPE}}}groupmode", group.attrib, "not a layer")
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temp:
            path = Path(temp) / "traced-edge.svg"
            write_svg(path, root)
            svg = path.read_bytes()
            self.assertEqual(stroke_paths(svg), expected_stroke_paths())
            ids = [p.attrib.get("id") for p in ET.fromstring(svg).iter(f"{{{SVG}}}path")]
            self.assertTrue(all(ids), ids)
            self.assertEqual(len(ids), len(set(ids)))
            checks = validate_svg(path, palette, [0, 1, 2])
            self.assertTrue(checks["passed"], checks)
            self.assertEqual(checks["layer_count"], 3)
            self.assertTrue(checks["expected_fills"])
            self.assertTrue(checks["all_paths_closed"])
            self.assertTrue(checks["unique_path_ids"])
            # With the ink left out of the cut, its layer is gone and the stroke group stays.
            root2 = svg_document(pm["W"], pm["H"], None)
            for row in palette[:2]:
                add_face_layer(root2, row, pm, paths=paths, areas=areas, elements=elements)
            add_stroke_group(root2, pm, palette)
            write_svg(path, root2)
            self.assertEqual(stroke_paths(path.read_bytes()), expected_stroke_paths())
            self.assertTrue(validate_svg(path, palette, [0, 1])["passed"])

    def test_a_map_without_strokes_gets_no_group(self):
        from vinylgen.svg_writer import add_stroke_group, svg_document
        from test_t3_trace import FIXTURE as TEST_ARRAY
        pm = json.loads((TEST_ARRAY / "map.json").read_text())
        palette = json.loads((TEST_ARRAY / "meta.json").read_text())["palette"]
        root = svg_document(pm["W"], pm["H"], None)
        self.assertEqual(add_stroke_group(root, pm, palette), {"strokes": 0, "dots": 0})
        self.assertEqual(list(root), [])
        # The keys present and empty: no group either.
        empty = {**fixture_map(), "strokes": [], "dots": []}
        empty["edges"] = [{k: v for k, v in e.items() if k != "under"} for e in empty["edges"]]
        self.assertEqual(add_stroke_group(root, empty, fixture_palette()), {"strokes": 0, "dots": 0})
        self.assertEqual(list(root), [])


# ----------------------------------------------------------------------------- the live app

class StrokeRequestTest(unittest.IsolatedAsyncioTestCase):
    """The trace job route refuses a bad value with 422 and starts no job."""

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_bad_values_are_refused_with_422(self):
        subject = build_seam_subject(self.root / "subject")
        async with _IdentityApi(self, self.root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            # The guard: a request with every field in range is admitted and traces, so a 422 below is the value's,
            # not an unknown field's.
            await run_job(self, api, "trace", project_id, palette_revision,
                          trace={"line_colours": [INK], "width_mode": "tiers", "tiers": 2, "stroke_fallback": "best"})
            bad = [{"line_colours": [-1]}, {"line_colours": [INK, INK]}, {"line_colours": "2"},
                   {"line_colours": [INK], "width_mode": "thick"}, {"line_colours": [INK], "stroke_fallback": "soft"},
                   {"line_colours": [INK], "width_mode": "tiers", "tiers": 0}, {"tiers": True}, {"tiers": "many"}]
            for body in bad:
                with self.subTest(trace=body):
                    response = await api.client.post("/api/jobs", json={
                        "kind": "trace", "project_id": project_id,
                        "revision_id": palette_revision, "trace": body})
                    self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(api.app.state.store.active_job_count(), 0, "no job was admitted")


class StrokeTraceChainTest(unittest.TestCase):
    """One chain of trace jobs on the seam subject through the live app, each on the one before:

    trace 1 (``line_colours`` [2]: traced, the strokes in the map and the export) -> trace 2 (the same four fields, a
    changed path limit: reused) -> trace 3 (``stroke_fallback`` best: traced again) -> trace 4 (no settings: the
    defaults, traced, no strokes). ``t3api.trace`` is counted in every process (``T3Calls``)."""

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
        checks = unittest.TestCase()
        out = cls.out
        subject = build_seam_subject(root / "subject")
        with T3Calls(root / "calls") as calls:
            async with _IdentityApi(checks, root / "data") as api:
                project_id, palette_revision = await api.palette_revision(subject)
                parent = palette_revision
                steps = [
                    ("trace_1", {"line_colours": [INK]}),
                    ("trace_2", record(line_colours=[INK], soft_path_nodes=1)),
                    ("trace_3", {"line_colours": [INK], "stroke_fallback": "best"}),
                    ("trace_4", None),
                ]
                for name, body in steps:
                    before = len(calls.all())
                    revision = await run_job(checks, api, "trace", project_id, parent,
                                             **({} if body is None else {"trace": body}))
                    out[name] = await cls._traced(api, revision, calls.all()[before:])
                    parent = revision

    @classmethod
    async def _traced(cls, api: _IdentityApi, revision_id: str, calls: list[dict]) -> dict:
        own = await api.own(revision_id)
        audit_bytes = own["trace-audit"]["content"]
        return {"revision": revision_id, "calls": calls,
                "map": await effective(api, revision_id, "traced-map"),
                "svg": await effective(api, revision_id, "traced-edge"),
                "audit": json.loads(audit_bytes),
                "settings": api.app.state.store.get_revision(revision_id)["settings"]["trace"]}

    def assert_traced(self, trace: dict, setting: dict, settings: dict) -> None:
        self.assertEqual(len(trace["calls"]), 1, "t3api.trace is called once")
        self.assertEqual(trace["calls"][0]["sha256"], labels_sha256(fixture_labels()))
        self.assert_recorded(trace, setting, settings)
        self.assertNotIn("reused_from", trace["audit"]["t3"])

    def assert_recorded(self, trace: dict, setting: dict, settings: dict) -> None:
        self.assertEqual(header_setting(trace["map"]), setting, "the map header carries the canonical setting")
        self.assertEqual(trace["audit"]["backend"]["setting"], setting)
        self.assertEqual(trace["audit"]["settings"], settings)
        self.assertEqual(trace["settings"], settings)
        self.assertEqual(trace["audit"]["validation"]["edge"]["planar_map_check"]["violations"], 0)
        self.assertTrue(trace["audit"]["validation"]["edge"]["passed"])
        self.assertTrue(trace["audit"]["validation"]["semantic"]["passed"])

    def test_trace_1_with_the_ink_as_a_line_colour_traces_its_stroke(self):
        trace = self.chain()["trace_1"]
        self.assert_traced(trace, SETTING_LINE, record(line_colours=[INK]))
        pm = json.loads(trace["map"])
        self.assertEqual(drawing(pm), drawing(fixture_map()))
        self.assertEqual(stroke_paths(trace["svg"]), expected_stroke_paths())
        root = ET.fromstring(trace["svg"])
        self.assertEqual(list(root)[-1].attrib.get("id"), "strokes", "the group is the root's last child")
        self.assertEqual(len([c for c in root if c.attrib.get(f"{{{INKSCAPE}}}groupmode") == "layer"]), 3)

    def test_trace_2_at_an_equal_setting_reuses_trace_1s_map(self):
        chain = self.chain()
        trace_1, trace_2 = chain["trace_1"], chain["trace_2"]
        self.assertEqual(trace_2["calls"], [], "t3api.trace is not called")
        self.assertEqual(trace_2["map"], trace_1["map"], "map.json reused byte for byte")
        self.assertEqual(trace_2["audit"]["t3"]["reused_from"], trace_1["revision"])
        self.assert_recorded(trace_2, SETTING_LINE, record(line_colours=[INK], soft_path_nodes=1))
        self.assertEqual(stroke_paths(trace_2["svg"]), expected_stroke_paths())

    def test_trace_3_at_the_best_fallback_traces_again(self):
        chain = self.chain()
        trace = chain["trace_3"]
        self.assert_traced(trace, SETTING_LINE_BEST, record(line_colours=[INK], stroke_fallback="best"))
        self.assertEqual(json.loads(trace["map"])["strokes"], json.loads(chain["trace_1"]["map"])["strokes"],
                         "a passing ring is the same stroke under best")
        self.assertNotEqual(trace["map"], chain["trace_1"]["map"], "the header differs: the fallback")

    def test_trace_4_at_the_defaults_traces_todays_map(self):
        trace = self.chain()["trace_4"]
        self.assert_traced(trace, SETTING_DEFAULT, DEFAULT_RECORD)
        pm = json.loads(trace["map"])
        for key in STROKE_KEYS:
            self.assertNotIn(key, pm)
        self.assertNotIn(b"line_labels", trace["map"][:trace["map"].index(b'"nodes"')])
        self.assertEqual(stroke_paths(trace["svg"]), [])
        self.assertNotIn(b'id="strokes"', trace["svg"])


if __name__ == "__main__":
    unittest.main()
