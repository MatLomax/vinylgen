"""The arc minimum sweep on the trace step: vinylgen's backend passes each arc kind's minimum sweep to t3api 0.29.0.

The contract is the tracer card ``cards/arc-minimum-sweep/card.md`` (decision 1, section 6's vinylgen list, section 7
step 4; worklog task ``a-minimum-sweep-for-circular-and-elliptical-arcs-each-curved``), on the plumbing card 12 laid
for the half-widths (``tests/test_kind_tolerance_settings.py``). Frozen: the builder changes no test here.

- ``TraceSettings`` and the web model ``TraceJobSettings`` gain ``min_sweep_arc`` and ``min_sweep_ellipse``: the least
  drawn sweep, in degrees, of a circular and of an elliptical arc; each 0.0 by default (no minimum), each in
  [0, 360]. A record from before the fields reads them as 0. A minimum above 0 on a kind that is off (its
  half-width ``None``) is refused: the web model raises ``ValidationError`` (the route answers 422 and starts no job)
  and ``tracing.t3_setting`` raises t3api's ``ValueError`` naming ``min_sweep``, since an off kind draws nothing to
  restrict.
- ``tracing.t3_setting`` gives t3api's canonical setting with ``"min_sweep": {"arc": A, "ellipse": E}`` when either
  is above 0, and today's canonical setting, without the key, when both are 0 (``t3api.normal_setting`` decides the
  form). ``tracer_info``'s default setting has no key.
- Through the live app: a trace job at ``min_sweep_arc`` 270 and ``min_sweep_ellipse`` 270 calls ``t3api.trace``
  once; its map header, ``trace.json``'s backend setting and the revision's settings record carry the minima; its
  drawing (the map's nodes, edges and faces) is ``t3api.trace``'s at that canonical setting on the same labels,
  called in this process. A parent's map is reused only at an equal canonical setting: a second trace at 270 / 270
  reuses it; a trace back at 0 / 0 traces again and draws trace 1's map byte for byte. A nib save of the 270 map
  keeps the minima in its audit.

Every trace runs through the code a user reaches, as card 12's tests do: the live app's jobs over the in-process ASGI
transport (each trace in its ``pipeline_worker`` subprocess, ``t3api.trace`` counted by ``test_t3_settings.T3Calls``)
and nib's save route, on the test array (``tests/fixtures/t3-test-array``).

These tests need t3api 0.29.0 (``min_sweep`` in ``normal_setting``) and the build that reads the fields, so they run
red until the builder lands both.

The mutants each test is meant to kill (run at build time):

- the field sent while its kind is off, reaching t3api as a nonzero minimum on an off kind:
  ``test_a_nonzero_minimum_on_an_off_kind_is_refused`` (model and route);
- the key dropped on the way to t3api (``t3_setting`` passing only ``half``):
  ``test_t3_setting_carries_the_minima_only_when_one_is_above_0`` and the chain's trace 2 (an arc below 270 drawn,
  the drawing not t3api's at 270);
- the minima left out of the reuse comparison (a 270 trace reusing a 0 map): the chain's trace 2 and trace 4;
- the key written at the default: ``test_t3_setting_carries_the_minima_only_when_one_is_above_0`` and the chain's
  trace 1 and trace 4 (header and audit without the key);
- an out-of-range minimum accepted: ``test_out_of_range_minima_are_refused`` and the route test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import t3api

from test_nib_embed import _IdentityApi, move_one_node
from test_t3_settings import FORM_LIMITS, T3Calls, effective, fixture_labels, labels_sha256, run_job
from test_t3_trace import SCRATCH, build_subject
from vinylgen.contracts import TraceSettings, upgrade_trace_record

KINDS = ("line", "arc", "ellipse", "cubic")
HALF_DEFAULTS = {f"half_{kind}": 1.75 for kind in KINDS}
MIN_DEFAULTS = {"min_sweep_arc": 0.0, "min_sweep_ellipse": 0.0}
DEFAULT_RECORD = {**FORM_LIMITS, **HALF_DEFAULTS, **MIN_DEFAULTS}
SETTING_DEFAULT = {"half": {kind: 1.75 for kind in KINDS}, "A1": 22.5, "A2": 45.0, "L": 4.0}
SETTING_270 = {**SETTING_DEFAULT, "min_sweep": {"arc": 270.0, "ellipse": 270.0}}


def record(**fields) -> dict:
    """The trace settings record with the named fields changed."""
    unknown = set(fields) - set(DEFAULT_RECORD)
    if unknown:
        raise ValueError(f"not a trace setting: {sorted(unknown)}")
    return {**DEFAULT_RECORD, **fields}


def header_setting(map_bytes: bytes) -> dict:
    return json.loads(map_bytes)["setting"]


def drawing(map_bytes_or_doc) -> str:
    """A planar map's drawing: its nodes, edges and faces, as JSON text."""
    pm = json.loads(map_bytes_or_doc) if isinstance(map_bytes_or_doc, (bytes, str)) else map_bytes_or_doc
    return json.dumps({k: pm[k] for k in ("W", "H", "nodes", "edges", "faces")}, sort_keys=True)


# ----------------------------------------------------------------------------- the settings model

class MinSweepSettingsModelTest(unittest.TestCase):
    """``contracts.TraceSettings`` carries the two minima at 0.0, and an older record reads them as 0."""

    def test_the_defaults_carry_both_minima_at_0(self):
        self.assertEqual(dataclasses.asdict(TraceSettings()), DEFAULT_RECORD)

    def test_a_record_from_before_the_fields_reads_them_as_0(self):
        old = {**FORM_LIMITS, **HALF_DEFAULTS, "half_arc": None}
        settings = TraceSettings(**upgrade_trace_record(old))
        self.assertEqual(dataclasses.asdict(settings), record(half_arc=None))

    def test_the_fields_round_trip(self):
        settings = TraceSettings(min_sweep_arc=270.0, min_sweep_ellipse=90.5)
        self.assertEqual(dataclasses.asdict(settings), record(min_sweep_arc=270.0, min_sweep_ellipse=90.5))


class MinSweepT3SettingTest(unittest.TestCase):
    """``tracing.t3_setting`` passes the minima to t3api's canonical setting."""

    def test_t3_setting_carries_the_minima_only_when_one_is_above_0(self):
        from vinylgen.tracing import t3_setting
        self.assertEqual(t3_setting(TraceSettings()), SETTING_DEFAULT)
        self.assertNotIn("min_sweep", t3_setting(TraceSettings(min_sweep_arc=0.0, min_sweep_ellipse=0.0)))
        self.assertEqual(t3_setting(TraceSettings(min_sweep_arc=270, min_sweep_ellipse=270)), SETTING_270)
        got = t3_setting(TraceSettings(min_sweep_arc=0.0, min_sweep_ellipse=300.5))
        self.assertEqual(got, {**SETTING_DEFAULT, "min_sweep": {"arc": 0.0, "ellipse": 300.5}})
        # An off kind at 0 beside the other kind's minimum.
        got = t3_setting(TraceSettings(half_arc=None, min_sweep_ellipse=270.0))
        self.assertEqual(got, {**SETTING_DEFAULT, "half": {**SETTING_DEFAULT["half"], "arc": None},
                               "min_sweep": {"arc": 0.0, "ellipse": 270.0}})

    def test_a_nonzero_minimum_on_an_off_kind_is_refused_by_t3api(self):
        from vinylgen.tracing import t3_setting
        with self.assertRaises(ValueError) as caught:
            t3_setting(TraceSettings(half_arc=None, min_sweep_arc=10.0))
        self.assertIn("min_sweep", str(caught.exception))

    def test_tracer_info_names_the_default_without_the_key(self):
        from vinylgen.tracing import tracer_info
        self.assertEqual(tracer_info()["setting"], SETTING_DEFAULT)


class MinSweepJobModelTest(unittest.TestCase):
    """The web model (``vinylgen.web.models.TraceJobSettings``): both minima at 0, each in [0, 360], and none above
    0 on a kind that is off."""

    def model(self):
        from vinylgen.web.models import TraceJobSettings
        return TraceJobSettings

    def test_the_defaults_match_the_contract(self):
        self.assertEqual(self.model()().model_dump(), DEFAULT_RECORD)

    def test_the_bounds_are_accepted(self):
        for field in MIN_DEFAULTS:
            for value in (0, 0.0, 270, 359.5, 360):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.model()(**{field: value}).model_dump()[field], float(value))

    def test_out_of_range_minima_are_refused(self):
        from pydantic import ValidationError
        # The guard: the fields exist and take their bounds, so a refusal below is the range's, not an unknown field's.
        self.assertEqual(self.model()(min_sweep_arc=0, min_sweep_ellipse=360).model_dump(),
                         record(min_sweep_arc=0.0, min_sweep_ellipse=360.0))
        for field in MIN_DEFAULTS:
            for value in (-1, -0.5, 360.5, 400, None, "270deg"):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    self.model()(**{field: value})

    def test_a_nonzero_minimum_on_an_off_kind_is_refused(self):
        from pydantic import ValidationError
        for half, field in (("half_arc", "min_sweep_arc"), ("half_ellipse", "min_sweep_ellipse")):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.model()(**{half: None, field: 10.0})
            with self.subTest(field=field, value=0):
                self.assertEqual(self.model()(**{half: None, field: 0}).model_dump()[field], 0.0)


# ----------------------------------------------------------------------------- the live app

class MinSweepRequestTest(unittest.IsolatedAsyncioTestCase):
    """The trace job route refuses an out-of-range minimum and one on an off kind with 422, and starts no job."""

    def setUp(self) -> None:
        SCRATCH.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def test_bad_minima_are_refused_with_422(self):
        subject = build_subject(self.root / "subject")
        async with _IdentityApi(self, self.root / "data") as api:
            project_id, palette_revision = await api.palette_revision(subject)
            # The guard: a minimum in range on a kind that is on is admitted and traces, so a 422 below is the
            # value's, not an unknown field's.
            await run_job(self, api, "trace", project_id, palette_revision,
                          trace={"min_sweep_arc": 0, "min_sweep_ellipse": 360})
            bad = [{"min_sweep_arc": -1}, {"min_sweep_ellipse": 360.5}, {"min_sweep_arc": "off"},
                   {"half_arc": None, "min_sweep_arc": 270}, {"half_ellipse": None, "min_sweep_ellipse": 1}]
            for body in bad:
                with self.subTest(trace=body):
                    response = await api.client.post("/api/jobs", json={
                        "kind": "trace", "project_id": project_id,
                        "revision_id": palette_revision, "trace": body})
                    self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(api.app.state.store.active_job_count(), 0, "no job was admitted")


class MinSweepTraceChainTest(unittest.TestCase):
    """One chain of trace jobs on the test array through the live app, each on the one before:

    trace 1 (no settings: the defaults) -> trace 2 (``min_sweep_arc`` and ``min_sweep_ellipse`` 270: traced) ->
    trace 3 (270 / 270, a changed path limit: reused) -> trace 4 (both minima 0 given explicitly: traced, trace 1's
    map again). Beside them: a nib save of a one-node move of trace 2's map.

    ``t3api.trace`` is counted in every process (``T3Calls``). The expected drawing at 270 / 270 is ``t3api.trace``
    on the fixture's labels at ``SETTING_270``, called in this process."""

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
                project_id, palette_revision = await api.palette_revision(subject)
                parent = palette_revision
                steps = [
                    ("trace_1", None),
                    ("trace_2", {"min_sweep_arc": 270, "min_sweep_ellipse": 270}),
                    ("trace_3", record(min_sweep_arc=270.0, min_sweep_ellipse=270.0, soft_path_nodes=1)),
                    ("trace_4", {"min_sweep_arc": 0, "min_sweep_ellipse": 0}),
                ]
                for name, body in steps:
                    before = len(calls.all())
                    revision = await run_job(checks, api, "trace", project_id, parent,
                                             **({} if body is None else {"trace": body}))
                    out[name] = await cls._traced(api, revision, calls.all()[before:])
                    parent = revision
                before = len(calls.all())
                saved = await api.post_map(out["trace_2"]["revision"], move_one_node(out["trace_2"]["map"].decode()))
                checks.assertEqual(saved.status_code, 201, saved.text)
                out["save_2"] = await cls._traced(api, saved.json()["id"], calls.all()[before:])
        result = t3api.trace(fixture_labels(), SETTING_270, workers=1)
        out["want_270"] = drawing(t3api.planar_map(result))

    @classmethod
    async def _traced(cls, api: _IdentityApi, revision_id: str, calls: list[dict]) -> dict:
        own = await api.own(revision_id)
        audit_bytes = own["trace-audit"]["content"]
        return {"revision": revision_id, "calls": calls,
                "map": await effective(api, revision_id, "traced-map"),
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

    def test_trace_1_at_the_defaults_carries_no_minimum(self):
        trace = self.chain()["trace_1"]
        self.assert_traced(trace, SETTING_DEFAULT, DEFAULT_RECORD)
        self.assertNotIn(b"min_sweep", trace["map"][:trace["map"].index(b'"nodes"')])

    def test_trace_2_at_270_traces_again_with_the_minima_reaching_t3(self):
        chain = self.chain()
        trace = chain["trace_2"]
        self.assert_traced(trace, SETTING_270, record(min_sweep_arc=270.0, min_sweep_ellipse=270.0))
        self.assertEqual(drawing(trace["map"]), chain["want_270"], "t3api's drawing at 270 / 270")
        self.assertNotEqual(drawing(trace["map"]), drawing(chain["trace_1"]["map"]),
                            "the minimum changes the test array's drawing")

    def test_trace_3_at_an_equal_setting_reuses_trace_2s_map(self):
        chain = self.chain()
        trace_2, trace_3 = chain["trace_2"], chain["trace_3"]
        self.assertEqual(trace_3["calls"], [], "t3api.trace is not called")
        self.assertEqual(trace_3["map"], trace_2["map"], "map.json reused byte for byte")
        self.assertEqual(trace_3["audit"]["t3"]["reused_from"], trace_2["revision"])
        self.assert_recorded(trace_3, SETTING_270,
                             record(min_sweep_arc=270.0, min_sweep_ellipse=270.0, soft_path_nodes=1))

    def test_trace_4_back_at_0_traces_again_to_trace_1s_map(self):
        chain = self.chain()
        trace_1, trace_4 = chain["trace_1"], chain["trace_4"]
        self.assert_traced(trace_4, SETTING_DEFAULT, DEFAULT_RECORD)
        self.assertEqual(trace_4["map"], trace_1["map"], "the same labels at the same canonical setting")

    def test_a_nib_save_of_the_270_map_keeps_the_minima(self):
        chain = self.chain()
        save, trace_2 = chain["save_2"], chain["trace_2"]
        self.assertEqual(save["calls"], [], "a save traces nothing")
        self.assertEqual(save["audit"]["backend"]["editor"]["name"], "nib")
        self.assertEqual(header_setting(save["map"]), SETTING_270)
        self.assertEqual(save["audit"]["backend"]["setting"], SETTING_270)
        self.assertEqual(save["audit"]["settings"], record(min_sweep_arc=270.0, min_sweep_ellipse=270.0))
        self.assertEqual(save["settings"], trace_2["settings"])


if __name__ == "__main__":
    unittest.main()
