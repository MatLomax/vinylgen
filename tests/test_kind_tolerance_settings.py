"""Card 12 of vinylgen-on-t3: each curve kind's tube half-width set on the trace step, with arcs,
ellipses and cubics each switchable off, on the 0.27.0 ``t3api`` wheel.

The contract is the tracer worklog task
``per-kind-tube-tolerance-in-t3-each-kind-s-own-half-width-any`` and the card brief
``cards/vinylgen-on-t3/build-12-brief.md``: ``TraceSettings`` and the web model gain ``half_line``,
``half_arc``, ``half_ellipse`` and ``half_cubic`` (each 1.75 by default; every value > 0; ``None`` is off
on the three optional kinds, never on the line); ``trace_subject`` passes the table to ``t3api.trace`` and
``trace.json`` records the canonical setting (``t3api.normal_setting``: ``{"half": {line, arc, ellipse,
cubic}, "A1", "A2", "L"}``); a parent's map is reused only at an equal canonical table (a header carrying
only ``w``, as every map up to 0.26.0 does, normalises to the all-1.75 table); the semantic audit's
``maximum_deviation`` is the largest enabled half-width of the map's own setting; ``tracer_info`` names the
build and the default table.

Every trace runs through the code a user reaches, on the test array (``tests/fixtures/t3-test-array``):
the live app's jobs over the in-process ASGI transport (each trace in its ``pipeline_worker``
subprocess, ``t3api.trace`` counted by ``test_t3_settings.T3Calls``), nib's save route, and the trace
CLI's ``main``. The planar map names every arc ``A`` with an ``E`` seg ``[cx, cy, rx, ry, rot, ...]``
(``FACE_KIND`` maps ``E`` to ``A``): a circular arc is written in the circle form, ``rx == ry`` and ``rot``
0 (68 of the fixture's 88 ``A`` pieces), an elliptical arc with ``rx != ry`` (the other 20). So "no
circular arc drawn" is read from the seg's form, as packaging round 7's ``test_a6_arc_off`` reads it, and
"no arc of either kind" from the ``A`` count.

These tests need the 0.27.0 wheel (``t3api.normal_setting``) and the build that reads the fields, so
they run red until the builder lands both.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import t3api

from test_nib_embed import _IdentityApi, move_one_node
from test_t3_settings import FORM_LIMITS, T3Calls, effective, labels_sha256, fixture_labels, run_job
from test_t3_trace import SCRATCH, build_subject
from vinylgen.contracts import TraceSettings, upgrade_trace_record

KINDS = ("line", "arc", "ellipse", "cubic")
HALF_FIELDS = tuple(f"half_{kind}" for kind in KINDS)
DEFAULT_HALF = 1.75
# t3api 0.27.0's canonical default (``t3api.SETTING``, ``t3api.normal_setting(None)``).
SETTING_DEFAULT = {"half": {kind: DEFAULT_HALF for kind in KINDS}, "A1": 22.5, "A2": 45.0, "L": 4.0}
# The only setting a planar-map header written by t3api up to 0.26.0 carries.
W_ONLY_SETTING = {"w": 3.5, "A1": 22.5, "A2": 45.0, "L": 4.0}
# The trace settings record as every surface stores it: the three limits, the four half-widths and the two
# minimum sweeps (the arc minimum sweep, 0 by default).
DEFAULT_RECORD = {**FORM_LIMITS, **{field: DEFAULT_HALF for field in HALF_FIELDS},
                  "min_sweep_arc": 0.0, "min_sweep_ellipse": 0.0}


def table(**halves) -> dict:
    """The canonical setting with the named kinds' half-widths (``arc=None`` is off)."""
    unknown = set(halves) - set(KINDS)
    if unknown:
        raise ValueError(f"not a kind: {sorted(unknown)}")
    return {**SETTING_DEFAULT, "half": {**SETTING_DEFAULT["half"], **halves}}


def record(**fields) -> dict:
    """The trace settings record with the named fields changed."""
    unknown = set(fields) - set(DEFAULT_RECORD)
    if unknown:
        raise ValueError(f"not a trace setting: {sorted(unknown)}")
    return {**DEFAULT_RECORD, **fields}


def pieces(map_bytes: bytes) -> list[dict]:
    """Every drawn piece of a planar-map file, over all its edges."""
    pm = json.loads(map_bytes)
    return [piece for edge in pm["edges"] for piece in edge["pieces"]]


def is_circle(seg: list) -> bool:
    """The circle form t3 writes for an arc piece (``tube_tiler.to_seg``): an ``E`` seg
    ``[cx, cy, rx, ry, rot, ...]`` with ``rx == ry`` and ``rot`` 0. An ellipse piece's ``E`` seg has
    ``rx != ry``."""
    return seg[0] == "E" and seg[3] == seg[4] and seg[5] == 0.0


def circular_arcs(map_bytes: bytes) -> list[dict]:
    """The ``A`` pieces in the circle form: the circular arcs."""
    return [piece for piece in pieces(map_bytes) if piece["kind"] == "A" and is_circle(piece["seg"])]


def arcs(map_bytes: bytes) -> list[dict]:
    """Every ``A`` piece: circular and elliptical arcs."""
    return [piece for piece in pieces(map_bytes) if piece["kind"] == "A"]


def header_setting(map_bytes: bytes) -> dict:
    return json.loads(map_bytes)["setting"]


def w_only_copy(map_bytes: bytes) -> bytes:
    """``map_bytes`` with its header's setting as a 0.26.0 header carries it (``w`` only), every
    other byte of the document's content the same: the same nodes, edges, faces and build."""
    pm = json.loads(map_bytes)
    pm["setting"] = dict(W_ONLY_SETTING)
    return json.dumps(pm, separators=(",", ":")).encode()


# ----------------------------------------------------------------------------- the settings model

class TraceSettingsModelTest(unittest.TestCase):
    """``contracts.TraceSettings`` carries the four half-widths at 1.75, beside the three limits,
    and reads an older record (the limits only) as the defaults."""

    def test_the_defaults_are_the_three_limits_and_every_kind_at_1_75(self):
        self.assertEqual(dataclasses.asdict(TraceSettings()), DEFAULT_RECORD)

    def test_an_off_kind_is_none_and_the_table_round_trips(self):
        settings = TraceSettings(half_arc=None, half_cubic=2.5)
        self.assertEqual(dataclasses.asdict(settings), record(half_arc=None, half_cubic=2.5))

    def test_a_record_from_before_the_fields_reads_as_the_defaults(self):
        old = {"soft_path_nodes": 7, "soft_layer_nodes": 70, "soft_document_nodes": 700}
        settings = TraceSettings(**upgrade_trace_record(old))
        self.assertEqual(dataclasses.asdict(settings), {**DEFAULT_RECORD, **old})

    def test_the_docstring_no_longer_says_nothing_steers_the_trace(self):
        text = (TraceSettings.__doc__ or "").lower()
        self.assertNotIn("none changes the geometry", text)
        self.assertNotIn("one fixed setting", text)


class TraceJobSettingsModelTest(unittest.TestCase):
    """The web model (``vinylgen.web.models.TraceJobSettings``): the four fields at 1.75, each
    ``gt=0``; ``None`` (off) on arc, ellipse and cubic only."""

    def model(self):
        from vinylgen.web.models import TraceJobSettings
        return TraceJobSettings

    def test_the_defaults_match_the_contract(self):
        self.assertEqual(self.model()().model_dump(), DEFAULT_RECORD)

    def test_an_optional_kind_turned_off_is_none(self):
        for field in ("half_arc", "half_ellipse", "half_cubic"):
            with self.subTest(field=field):
                dumped = self.model()(**{field: None}).model_dump()
                self.assertEqual(dumped, record(**{field: None}))

    def test_the_line_cannot_be_turned_off(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self.model()(half_line=None)

    def test_zero_and_negative_half_widths_are_refused_on_every_kind(self):
        from pydantic import ValidationError
        for field in HALF_FIELDS:
            for value in (0, 0.0, -1, -0.5):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    self.model()(**{field: value})

    def test_a_small_positive_half_width_is_accepted(self):
        self.assertEqual(self.model()(half_line=0.25).model_dump()["half_line"], 0.25)


class TracerInfoTest(unittest.TestCase):
    """``tracing.tracer_info`` names the build and the canonical default table, which the API
    contract gives as ``t3api.normal_setting(None)``."""

    def test_normal_setting_gives_the_canonical_default_for_none_and_for_a_w_only_header(self):
        self.assertEqual(t3api.normal_setting(None), SETTING_DEFAULT)
        self.assertEqual(t3api.normal_setting(W_ONLY_SETTING), SETTING_DEFAULT)
        self.assertEqual(t3api.normal_setting({"w": 3.5}), SETTING_DEFAULT)

    def test_tracer_info_names_the_build_and_the_default_table(self):
        from vinylgen.tracing import tracer_info
        self.assertEqual(tracer_info(),
                         {"name": "t3api", "build": t3api.BUILD_ID, "setting": SETTING_DEFAULT})


# ----------------------------------------------------------------------------- the live app

class _WebTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()


class RequestValidationTest(_WebTest):
    """The trace job route refuses an off line and a zero or negative half-width with 422, and
    starts no job; the health route serves ``tracer_info``."""

    async def test_an_off_line_and_zero_or_negative_half_widths_are_refused(self):
        subject = build_subject(self.root / "subject")
        async with _IdentityApi(self, self.root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            bad = [{"half_line": None}, {"half_line": 0}, {"half_arc": 0}, {"half_ellipse": -1},
                   {"half_cubic": -0.5}, {"half_arc": "off"}]
            for body in bad:
                with self.subTest(trace=body):
                    response = await api.client.post("/api/jobs", json={
                        "kind": "trace", "project_id": project_id,
                        "revision_id": palette_revision, "trace": body})
                    self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(api.app.state.store.active_job_count(), 0, "no job was admitted")

    async def test_the_health_route_serves_the_tracer_with_its_default_table(self):
        async with _IdentityApi(self, self.root / "data") as api:
            health = (await api.client.get("/api/health")).json()
        self.assertEqual(health["tracer"],
                         {"name": "t3api", "build": t3api.BUILD_ID, "setting": SETTING_DEFAULT})


class KindTraceChainTest(unittest.TestCase):
    """One chain of trace jobs on the test array through the live app, each on the one before:

    trace 1 (no settings: the defaults) -> trace 2 (the all-1.75 table written out, a changed path
    limit: reused) -> trace 3 (arc off: traced) -> trace 4 (arc off, a changed layer limit: reused) ->
    trace 5 (arc off, cubic 2.5: traced) -> trace 6 (line 1.0, arc and ellipse off, cubic 0.8: traced)
    -> trace 7 (no settings: traced, trace 1's map again).

    Beside them: trace 8 on a stored copy of trace 1 as a 0.26.0 trace revision holds it (its map
    header's setting ``w`` only, its settings record the three limits only): reused. And two saves
    through nib's route: a one-node move of trace 5's map, and of the 0.26.0-shaped copy's.

    ``t3api.trace`` is counted in every process (``T3Calls``)."""

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
        subject = build_subject(root / "subject")
        with T3Calls(root / "calls") as calls:
            async with _IdentityApi(checks, root / "data") as api:
                store = api.app.state.store
                project_id, palette_revision = await api.palette_revision(subject)
                parent = palette_revision
                steps = [
                    ("trace_1", None),
                    ("trace_2", record(soft_path_nodes=1)),
                    ("trace_3", {"half_arc": None}),
                    ("trace_4", {"half_arc": None, "soft_layer_nodes": 7}),
                    ("trace_5", {"half_arc": None, "half_cubic": 2.5}),
                    ("trace_6", {"half_line": 1.0, "half_arc": None, "half_ellipse": None,
                                 "half_cubic": 0.8}),
                    ("trace_7", None),
                ]
                for name, body in steps:
                    before = len(calls.all())
                    revision = await run_job(checks, api, "trace", project_id, parent,
                                             **({} if body is None else {"trace": body}))
                    out[name] = await cls._traced(api, revision, calls.all()[before:])
                    parent = revision

                # The 0.26.0-shaped copy of trace 1: a trace revision whose settings record
                # predates the half-width fields and whose map header carries ``w`` only.
                trace_1 = out["trace_1"]["revision"]
                settings = dict(store.get_revision(trace_1)["settings"])
                settings["trace"] = dict(FORM_LIMITS)
                old = store.create_revision(project_id, parent_revision_id=palette_revision,
                                            kind="trace", message="Deterministic trace pipeline",
                                            settings=settings)
                old_map = w_only_copy(out["trace_1"]["map"])
                audit = json.loads(out["trace_1"]["audit_bytes"])
                audit["backend"]["setting"] = dict(W_ONLY_SETTING)
                audit["settings"] = dict(FORM_LIMITS)
                audit["determinism"]["map_sha256"] = hashlib.sha256(old_map).hexdigest()
                replaced = {"traced-map": old_map,
                            "trace-audit": (json.dumps(audit, indent=2) + "\n").encode()}
                for row in store.list_artifacts(trace_1):
                    content = replaced.get(row["kind"])
                    if content is None:
                        content = store.artifact_path(row["id"]).read_bytes()
                    store.add_artifact(old["id"], kind=row["kind"], filename=row["filename"],
                                       media_type=row["media_type"], source=io.BytesIO(content))
                out["old"] = {"revision": old["id"], "map": old_map}
                before = len(calls.all())
                trace_8 = await run_job(checks, api, "trace", project_id, old["id"])
                out["trace_8"] = await cls._traced(api, trace_8, calls.all()[before:])

                # nib's saves: a one-node move of trace 5's map and of the old copy's.
                for name, source in (("save_5", out["trace_5"]), ("save_old", out["old"])):
                    before = len(calls.all())
                    saved = await api.post_map(source["revision"],
                                               move_one_node(source["map"].decode()))
                    checks.assertEqual(saved.status_code, 201, saved.text)
                    out[name] = await cls._traced(api, saved.json()["id"], calls.all()[before:])

    @classmethod
    async def _traced(cls, api: _IdentityApi, revision_id: str, calls: list[dict]) -> dict:
        own = await api.own(revision_id)
        audit_bytes = own["trace-audit"]["content"]
        return {"revision": revision_id, "calls": calls,
                "map": await effective(api, revision_id, "traced-map"),
                "audit": json.loads(audit_bytes), "audit_bytes": audit_bytes,
                "settings": api.app.state.store.get_revision(revision_id)["settings"]["trace"]}

    # ------------------------------------------------------------------ what each trace records

    def assert_traced(self, trace: dict, setting: dict, settings: dict) -> None:
        """One ``t3api.trace`` call on the fixture's labels; the map header, ``trace.json``'s
        backend and the revision all record the canonical table; the gates pass."""
        self.assertEqual(len(trace["calls"]), 1, "t3api.trace is called once")
        self.assertEqual(trace["calls"][0]["sha256"], labels_sha256(fixture_labels()))
        self.assert_recorded(trace, setting, settings)
        self.assertNotIn("reused_from", trace["audit"]["t3"])

    def assert_recorded(self, trace: dict, setting: dict, settings: dict) -> None:
        self.assertEqual(t3api.normal_setting(header_setting(trace["map"])), setting)
        self.assertEqual(trace["audit"]["backend"]["setting"], setting)
        self.assertEqual(trace["audit"]["settings"], settings)
        self.assertEqual(trace["settings"], settings)
        self.assertEqual(trace["audit"]["validation"]["edge"]["planar_map_check"]["violations"], 0)
        self.assertTrue(trace["audit"]["validation"]["edge"]["passed"])

    def deviation(self, trace: dict) -> float:
        return trace["audit"]["validation"]["semantic"]["settings"]["maximum_deviation_px"]

    def test_trace_1_at_the_defaults_records_the_canonical_default_table(self):
        trace = self.chain()["trace_1"]
        self.assert_traced(trace, SETTING_DEFAULT, DEFAULT_RECORD)
        self.assertEqual(header_setting(trace["map"]), SETTING_DEFAULT,
                         "a map written by 0.27.0 records its table in the canonical form")
        self.assertEqual(self.deviation(trace), DEFAULT_HALF)
        self.assertGreater(len(circular_arcs(trace["map"])), 0, "the default draws circular arcs")

    def test_trace_2_at_an_equal_table_reuses_trace_1s_map(self):
        chain = self.chain()
        trace_1, trace_2 = chain["trace_1"], chain["trace_2"]
        self.assertEqual(trace_2["calls"], [], "t3api.trace is not called")
        self.assertEqual(trace_2["map"], trace_1["map"], "map.json reused byte for byte")
        self.assertEqual(trace_2["audit"]["t3"]["reused_from"], trace_1["revision"])
        self.assert_recorded(trace_2, SETTING_DEFAULT, record(soft_path_nodes=1))

    def test_trace_3_with_the_arc_off_traces_again_and_draws_no_circular_arc(self):
        trace = self.chain()["trace_3"]
        self.assert_traced(trace, table(arc=None), record(half_arc=None))
        self.assertEqual(circular_arcs(trace["map"]), [], "no circular arc is drawn")
        kinds = {piece["kind"] for piece in pieces(trace["map"])}
        self.assertIn("L", kinds, "the trace still draws lines")
        self.assertEqual(self.deviation(trace), DEFAULT_HALF,
                         "the largest enabled half-width: line, ellipse and cubic at 1.75")

    def test_trace_4_at_trace_3s_table_reuses_trace_3s_map(self):
        chain = self.chain()
        trace_3, trace_4 = chain["trace_3"], chain["trace_4"]
        self.assertEqual(trace_4["calls"], [], "t3api.trace is not called")
        self.assertEqual(trace_4["map"], trace_3["map"])
        self.assertEqual(trace_4["audit"]["t3"]["reused_from"], trace_3["revision"])
        self.assert_recorded(trace_4, table(arc=None), record(half_arc=None, soft_layer_nodes=7))

    def test_trace_5_differing_in_one_kind_traces_again_at_its_own_table(self):
        chain = self.chain()
        trace = chain["trace_5"]
        self.assert_traced(trace, table(arc=None, cubic=2.5), record(half_arc=None, half_cubic=2.5))
        self.assertEqual(circular_arcs(trace["map"]), [])
        self.assertEqual(self.deviation(trace), 2.5, "the cubic's half-width is the largest enabled")

    def test_trace_6_with_arcs_and_ellipses_off_draws_no_arc_and_audits_at_the_lines_half_width(self):
        trace = self.chain()["trace_6"]
        setting = table(line=1.0, arc=None, ellipse=None, cubic=0.8)
        self.assert_traced(trace, setting,
                           record(half_line=1.0, half_arc=None, half_ellipse=None, half_cubic=0.8))
        self.assertEqual(arcs(trace["map"]), [], "no arc of either kind is drawn")
        kinds = {piece["kind"] for piece in pieces(trace["map"])}
        self.assertIn("L", kinds)
        self.assertEqual(self.deviation(trace), 1.0,
                         "the largest enabled half-width is the line's; off kinds do not count")

    def test_trace_7_back_at_the_defaults_traces_again_to_trace_1s_map(self):
        chain = self.chain()
        trace_1, trace_7 = chain["trace_1"], chain["trace_7"]
        self.assert_traced(trace_7, SETTING_DEFAULT, DEFAULT_RECORD)
        self.assertEqual(trace_7["map"], trace_1["map"], "the same labels at the same table")

    def test_trace_8_reuses_a_map_whose_header_carries_w_only_at_the_default_table(self):
        chain = self.chain()
        old, trace_8 = chain["old"], chain["trace_8"]
        self.assertEqual(header_setting(old["map"]), W_ONLY_SETTING)
        self.assertEqual(trace_8["calls"], [], "t3api.trace is not called")
        self.assertEqual(trace_8["map"], old["map"], "the 0.26.0-shaped map reused byte for byte")
        self.assertEqual(trace_8["audit"]["t3"]["reused_from"], old["revision"])
        self.assertEqual(trace_8["audit"]["backend"]["setting"], SETTING_DEFAULT,
                         "trace.json records the canonical form of the reused header")
        self.assertEqual(trace_8["audit"]["settings"], DEFAULT_RECORD)
        self.assertEqual(self.deviation(trace_8), DEFAULT_HALF)

    def test_a_nib_save_of_trace_5s_map_audits_at_its_headers_largest_enabled_half_width(self):
        chain = self.chain()
        save, trace_5 = chain["save_5"], chain["trace_5"]
        self.assertEqual(save["calls"], [], "a save traces nothing")
        self.assertEqual(save["audit"]["backend"]["editor"]["name"], "nib")
        self.assertEqual(save["audit"]["backend"]["setting"], table(arc=None, cubic=2.5))
        self.assertEqual(save["audit"]["settings"], record(half_arc=None, half_cubic=2.5))
        self.assertEqual(save["settings"], trace_5["settings"])
        self.assertEqual(self.deviation(save), 2.5)

    def test_a_nib_save_on_a_0_26_0_revision_reads_its_settings_as_the_defaults(self):
        chain = self.chain()
        save = chain["save_old"]
        self.assertEqual(save["calls"], [])
        self.assertEqual(save["audit"]["backend"]["editor"]["name"], "nib")
        self.assertEqual(save["audit"]["backend"]["setting"], SETTING_DEFAULT)
        self.assertEqual(save["audit"]["settings"], DEFAULT_RECORD)
        self.assertEqual(self.deviation(save), DEFAULT_HALF)


# ----------------------------------------------------------------------------- the CLI

class CliKindFlagsTest(unittest.TestCase):
    """The trace CLI's ``--half-line``, ``--half-arc``, ``--half-ellipse`` and ``--half-cubic``: a
    number each, ``off`` on all but the line; a config round-trips an off kind; an old config reads
    as the defaults; a refused flag exits 2 before anything is traced."""

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def parsed(self, *flags: str) -> TraceSettings:
        from vinylgen.cli_trace import parser, settings_from_args
        settings, _, _ = settings_from_args(parser().parse_args(["outputs", *flags]))
        return settings

    def with_config(self, config: dict, *flags: str) -> TraceSettings:
        path = self.root / "config.json"
        path.write_text(json.dumps(config))
        return self.parsed("--config", str(path), *flags)

    def test_the_four_flags_set_each_kinds_half_width(self):
        settings = self.parsed("--half-line", "1.2", "--half-arc", "0.9", "--half-ellipse", "2",
                               "--half-cubic", "3.5")
        self.assertEqual(settings, TraceSettings(half_line=1.2, half_arc=0.9, half_ellipse=2.0,
                                                 half_cubic=3.5))

    def test_off_turns_an_optional_kind_off(self):
        self.assertEqual(self.parsed("--half-arc", "off"), TraceSettings(half_arc=None))
        self.assertEqual(self.parsed("--half-ellipse", "off"), TraceSettings(half_ellipse=None))
        self.assertEqual(self.parsed("--half-cubic", "off"), TraceSettings(half_cubic=None))
        self.assertEqual(self.parsed("--half-arc", "off", "--half-ellipse", "off",
                                     "--half-cubic", "off"),
                         TraceSettings(half_arc=None, half_ellipse=None, half_cubic=None))

    def test_no_flag_leaves_every_kind_at_the_default(self):
        self.assertEqual(self.parsed(), TraceSettings())

    def test_the_line_cannot_be_off_and_zero_or_negative_values_are_refused(self):
        from vinylgen.cli_trace import main
        subject = build_subject(self.root / "subject")
        refused = [["--half-line", "off"], ["--half-line", "0"], ["--half-arc", "0"],
                   ["--half-ellipse", "-1"], ["--half-cubic", "-0.5"], ["--half-arc", "no"]]
        for flags in refused:
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch("sys.argv", ["svg_trace.py", str(subject), *flags]), \
                    self.assertRaises(SystemExit) as exit:
                main()
            self.assertEqual(exit.exception.code, 2)
            self.assertFalse((subject / "map.json").exists(), "nothing is traced")

    def test_a_config_round_trips_an_off_kind_and_flags_override_it(self):
        config = {"settings": record(half_arc=None, half_cubic=2.5)}
        self.assertEqual(self.with_config(config), TraceSettings(half_arc=None, half_cubic=2.5))
        self.assertEqual(self.with_config(config, "--half-arc", "1.1", "--half-cubic", "off"),
                         TraceSettings(half_arc=1.1, half_cubic=None))

    def test_an_old_trace_json_config_reads_as_the_defaults(self):
        old = {"settings": {"soft_path_nodes": 7, "soft_layer_nodes": 70,
                            "soft_document_nodes": 700, "overlap": 0.0,
                            "curve_fit_tolerance": 1.6}}
        self.assertEqual(self.with_config(old),
                         TraceSettings(soft_path_nodes=7, soft_layer_nodes=70,
                                       soft_document_nodes=700))

    def test_a_cli_trace_with_the_arc_off_records_its_table_and_draws_no_circular_arc(self):
        from vinylgen.cli_trace import main
        subject = build_subject(self.root / "subject")
        argv = ["svg_trace.py", str(subject), "--half-arc", "off", "--half-cubic", "2.5"]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
            main()
        map_bytes = (subject / "map.json").read_bytes()
        audit = json.loads((subject / "trace.json").read_text())
        self.assertEqual(header_setting(map_bytes), table(arc=None, cubic=2.5))
        self.assertEqual(audit["backend"]["setting"], table(arc=None, cubic=2.5))
        self.assertEqual(audit["settings"], record(half_arc=None, half_cubic=2.5))
        self.assertEqual(circular_arcs(map_bytes), [])
        self.assertEqual(audit["validation"]["semantic"]["settings"]["maximum_deviation_px"], 2.5)
        self.assertTrue(audit["validation"]["edge"]["passed"])


if __name__ == "__main__":
    unittest.main()
