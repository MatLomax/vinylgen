"""Synthetic ground-truth fidelity regression tests of the default trace.

Every committed synthetic case (``tests/synthetic/fixed``, ``tests/synthetic/
palette-stage`` and the pinned fuzz scenes listed in ``tests/synthetic/
fuzz-seeds.json``) is traced with the product defaults (``TraceSettings()``)
through the real stages and its edge variant judged against the case's truth and
the raster evidence the trace received. Two tiers:

- ``labels``: the hard-edged label map (``raster.render_labels``) straight into the
  trace stage (``harness.trace_labels``);
- ``rgb``: the anti-aliased render through the palette stage first
  (``harness.trace_rgb``, with the case's ``families.case_palette_settings``).
  Palette-stage cases run in this tier only.

The runs, the tracing and the expected-failure table's format are in
``vinylgen/fidelity/regression.py``; the bounds, the assertions and why each holds
are in ``vinylgen/fidelity/bounds.py``, the one yardstick the fidelity sweep and
tuner use too.

Each (case, tier) is one generated ``TestCase`` class whose ``setUpClass`` traces
once; each assertion is its own test method, so ``scripts/run_tests.py`` schedules
a (case, tier) as one unit and reports every assertion separately. An assertion
with nothing to hold for a case (``bounds.available``: every boundary lies in a
narrow or palette zone) is reported as a skip naming the reason.
``test_every_colour_is_held_by_an_area_assertion`` checks that the area and
evidence assertions together can fail for every scene colour
(``bounds.area_coverage``), and
``test_the_topology_assertion_fails_every_change_the_truth_decides`` that the
truth itself passes the topology assertion while every deletion and addition the
truth decides fails it (``bounds.topology_mutations``); a single change a count
cannot decide is reported as a skipped subtest naming why, with the smallest
change that leaves the count's range tried instead.

Expected failures
-----------------
An assertion listed in ``tests/synthetic/expected-failures.json`` fails today for
the defects tracked by the entry's worklog tasks (all of them: the entry names the
counterfactual proxies that, together, clear it; ``regression`` module docstring),
and the entry records the assertion's measurements (``bounds.measurements``) and the identities of what it
found wrong (``bounds.identities``). Only the assertion's own ``AssertionError``
counts as the expected failure, and only while every measurement equals its
recorded value within ``bounds.RATCHET_TOLERANCE`` and the identities are the
recorded set (``bounds.entry_ratchet``): a worse record (a larger measurement or
an unrecorded item) is a regression the entry would otherwise hide, and a better
one leaves the record too loose, so either is reported as a plain failure
(re-record with ``scripts/record_expected_failures.py``, which refuses a worse
record without ``--allow-worse``). Fixing one recorded defect while adding another
of the same size changes the identities, so it fails too. Any other exception while checking it is an
error. The method's failure message and docstring name the slugs; an unexpected
pass is reported as an ``UNEXPECTED SUCCESS``, which fails the run, so a fixed
defect forces its entry out of the table. An entry naming an unknown (tier, case),
an assertion the case does not make, a placeholder slug, an attribution whose
counterfactual does not stand for exactly its tasks, or measurements other than
the assertion's fails ``ExpectedFailureTableTests``; an entry for an
assertion that has nothing to hold is an error when it runs. The table checks a
slug's form only: worklog is not available to the tests, so that a slug names a
real task is checked by hand.
"""

from __future__ import annotations

import json
import math
import shutil
import unittest

import numpy as np
from shapely.geometry import box

from vinylgen.fidelity import bounds, families, regression
from vinylgen.fidelity.regression import (EXPECTED_FAILURES_PATH, PLACEHOLDER_SLUG_PREFIX,
                                          ROOT, all_runs, fuzz_cases, load_expected_failures,
                                          run_case)

WORK = ROOT / ".tmp" / "test-synthetic-fidelity"
# Two real tasks and their proxies, for the table-format tests.
FLOORS = regression.COUNTERFACTUALS["pixel-floors"]["task"]
APEX = regression.COUNTERFACTUALS["no-move-check"]["task"]
TIERS = bounds.TIERS
ASSERTIONS = bounds.ASSERTIONS

__all__ = ["EXPECTED_FAILURES_PATH", "PLACEHOLDER_SLUG_PREFIX", "all_runs", "fuzz_cases",
           "load_expected_failures", "run_case"]


def _class_name(tier: str, case_id: str) -> str:
    import re
    words = re.split(r"[^0-9a-zA-Z]+", f"{tier}-{case_id}")
    return "Fidelity_" + "_".join(word for word in words if word)


def _stop_expecting_failure(test: unittest.TestCase) -> None:
    """Report the exception now propagating from ``test`` as an error or failure,
    not as the expected failure its method is marked with: ``unittest`` counts any
    exception in an ``expectedFailure`` method as expected, and reads the
    outcome's flag only when the exception arrives."""
    outcome = getattr(test, "_outcome", None)
    if outcome is not None:
        outcome.expecting_failure = False


class _SyntheticCase:
    """Mixed into each generated (case, tier) ``TestCase``: ``setUpClass`` traces the
    case once; each test method is one assertion."""

    case = None
    tier = ""

    @classmethod
    def setUpClass(cls):
        cls.work_dir = WORK / cls.tier / cls.case.case_id
        shutil.rmtree(cls.work_dir, ignore_errors=True)
        cls.result = run_case(cls.case, cls.tier, cls.work_dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work_dir, ignore_errors=True)

    def test_resolved_max_deviation_matches_the_trace(self):
        self.assertAlmostEqual(self.result["max_deviation_px"],
                               self.result["audit_max_deviation_px"], places=9)

    def test_every_colour_is_held_by_an_area_assertion(self):
        """Every scene colour is held by the truth area bound (its limit below its
        true area) or by the evidence assertion, so a trace that drops it fails."""
        self.assertEqual(self.result["area_coverage"], [])

    def test_the_topology_assertion_fails_every_change_the_truth_decides(self):
        """The truth as its own trace passes the topology assertion, every deletion
        and addition the truth decides fails it, and each change it cannot decide
        is a skipped subtest naming why."""
        rows = self.result["topology_mutations"]
        self.assertEqual([row for row in rows if row.get("expect") == "pass" and row["error"]],
                         [])
        self.assertEqual([row for row in rows
                          if row.get("expect") == "fail" and not row["error"]], [])
        for row in rows:
            if "skip" in row:
                with self.subTest(colour=row["colour"], mutation=row["mutation"]):
                    self.skipTest(row["skip"])

    def _assert(self, name: str, entry: dict | None):
        tasks = _tasks(entry)
        reason = bounds.available(name, self.result)
        if reason is not None:
            if entry is not None:
                _stop_expecting_failure(self)
                raise RuntimeError(f"expected-failure entry {tasks} for {name}, which "
                                   f"has nothing to hold here: {reason}")
            self.skipTest(reason)
        try:
            passed, detail = bounds.check(name, self.result, self.case)
            measured = bounds.measurements(name, self.result, self.case)
            found = bounds.identities(name, self.result, self.case)
        except Exception:
            _stop_expecting_failure(self)
            raise
        prefix = f"[{self.tier}/{self.case.case_id} {name}]"
        if entry is None:
            self.assertTrue(passed, f"{prefix} {detail}")
            return
        if not passed:
            worse, better = bounds.entry_ratchet(entry, measured, found)
            if worse or better:
                _stop_expecting_failure(self)
                change = "worse than recorded" if worse else "better than recorded"
                self.fail(f"{prefix} expected failure {tasks} is {change} "
                          f"({'; '.join(worse + better)}); a worse value is a regression, "
                          "a better one is re-recorded with "
                          f"scripts/record_expected_failures.py: {detail}")
        self.assertTrue(passed, f"{prefix} expected failure, tracked by {tasks}: {detail}")


def _tasks(entry: dict | None) -> str:
    """An entry's worklog tasks, and the counterfactual that clears it (a malformed
    entry as it stands: ``ExpectedFailureTableTests`` reports it)."""
    if entry is None:
        return ""
    tasks = entry.get("tasks")
    text = ", ".join(map(str, tasks)) if isinstance(tasks, list) else repr(entry)
    return text + (f" (cleared by {entry['counterfactual']})"
                   if entry.get("counterfactual") else "")


def _assertion_method(name: str, entry: dict | None):
    def method(self):
        self._assert(name, entry)
    method.__name__ = f"test_{name}"
    if entry is not None:
        method.__doc__ = f"{name}: expected failure, tracked by worklog tasks {_tasks(entry)}"
        return unittest.expectedFailure(method)
    method.__doc__ = f"{name}: within its bound"
    return method


def _generate() -> dict[str, type]:
    expected = load_expected_failures()
    classes = {}
    for tier, case in all_runs():
        entries = expected.get(f"{tier}/{case.case_id}", {})
        body = {"case": case, "tier": tier, "__module__": __name__,
                "__doc__": f"{case.case_id} traced in the {tier} tier."}
        for name in bounds.applicable(case, tier):
            entry = entries.get(name)
            body[f"test_{name}"] = _assertion_method(
                name, entry if isinstance(entry, dict) else None)
        name = _class_name(tier, case.case_id)
        if name in classes:
            raise ValueError(f"duplicate synthetic test class {name}")
        classes[name] = type(name, (_SyntheticCase, unittest.TestCase), body)
    return classes


globals().update(_generate())


class ExpectedFailureTableTests(unittest.TestCase):
    def test_every_entry_names_a_traced_case_an_assertion_it_makes_and_its_measurements(self):
        runs = {f"{tier}/{case.case_id}": case for tier, case in all_runs()}
        for key, entries in load_expected_failures().items():
            with self.subTest(key=key):
                self.assertIn(key, runs, "expected failure for a (tier, case) the suite "
                                         "does not trace")
                self.assertTrue(entries, f"{key} lists no assertion")
                tier = key.split("/", 1)[0]
                made = bounds.applicable(runs[key], tier)
                for name, entry in entries.items():
                    self.assertIn(name, ASSERTIONS)
                    self.assertIn(name, made, f"{key} makes no {name} assertion")
                    self.assertEqual(regression.entry_problems(name, entry), [], key)

    def test_the_entry_check_rejects_malformed_entries(self):
        points = {"kinks": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]}
        floors, apex = FLOORS, APEX

        def entry(tasks, counterfactual, measured=None, identities=None):
            return {"tasks": tasks, "counterfactual": counterfactual,
                    "measured": {"kinks": 3.0} if measured is None else measured,
                    "identities": points if identities is None else identities}

        for good in (entry([floors], "pixel-floors"),
                     entry([apex, floors], "no-move-check+pixel-floors"),
                     entry([floors, apex], "pixel-floors+no-move-check")):
            with self.subTest(good=good):
                self.assertEqual(regression.entry_problems("kinks", good), [])
        for bad in ("some-task", {"task": floors, "measured": {"kinks": 3.0},
                                  "identities": points},
                    {"tasks": [floors], "measured": {"kinks": 3.0}, "identities": points},
                    entry([], "pixel-floors"), entry(floors, "pixel-floors"),
                    entry(["Some Task"], "pixel-floors"),
                    entry([floors, floors], "pixel-floors"),
                    entry([floors], None), entry([floors], "no-such-proxy"),
                    # The counterfactual must stand for exactly the tasks named.
                    entry([floors], "pixel-floors+no-move-check"),
                    entry([apex, floors], "pixel-floors"),
                    entry([floors], "pixel-floors+pixel-floors"),
                    entry([PLACEHOLDER_SLUG_PREFIX + "x"], None),
                    entry([PLACEHOLDER_SLUG_PREFIX + "x", floors], "pixel-floors"),
                    entry([floors], "pixel-floors", {"nodes_outside_zones": 3.0}),
                    entry([floors], "pixel-floors", {"kinks": -1.0}),
                    entry([floors], "pixel-floors", {"kinks": "3"}),
                    entry([floors], "pixel-floors", None, {"cusps": [[1.0, 2.0]]}),
                    entry([floors], "pixel-floors", None, {"kinks": [[]]}),
                    entry([floors], "pixel-floors", None, {"kinks": [[1.0, None]]})):
            with self.subTest(bad=bad):
                self.assertTrue(regression.entry_problems("kinks", bad))
        # Every proxy stands for one task.
        for name, proxy in regression.COUNTERFACTUALS.items():
            self.assertTrue(regression.SLUG_PATTERN.match(proxy["task"]), name)

    def test_the_identity_ratchet_catches_a_swapped_defect(self):
        recorded = {"kinks": [[10.0, 20.0], [30.0, 40.0]]}
        self.assertEqual(bounds.identity_ratchet(recorded, {"kinks": [[30.0, 40.0005],
                                                                      [10.0, 20.0]]}), ([], []))
        # One recorded kink fixed and a new one elsewhere: the count is unchanged.
        worse, better = bounds.identity_ratchet(recorded, {"kinks": [[10.0, 20.0],
                                                                     [50.0, 60.0]]})
        self.assertTrue(worse and better)
        worse, better = bounds.identity_ratchet(recorded, {"kinks": [[10.0, 20.0]]})
        self.assertTrue(better and not worse)
        entry = {"tasks": ["t"], "counterfactual": None, "measured": {"kinks": 2.0},
                 "identities": recorded}
        worse, _ = bounds.entry_ratchet(entry, {"kinks": 2.0},
                                        {"kinks": [[10.0, 20.0], [50.0, 60.0]]})
        self.assertTrue(worse)
        # Strings match exactly, numbers within the tolerance.
        self.assertTrue(bounds.identity_ratchet({"layers": [["#AABBCC"]]},
                                                {"layers": [["#AABBCD"]]})[0])

    def test_the_table_is_written_one_identity_item_per_line(self):
        import json
        import tempfile
        from pathlib import Path
        table = {"labels/x": {"kinks": {"tasks": [FLOORS, APEX],
                                        "counterfactual": "no-move-check+pixel-floors",
                                        "measured": {"kinks": 2.0},
                                        "identities": {"kinks": [[1.123456789, 2.0],
                                                                 [3.0, 4.0]]}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.json"
            regression.write_expected_failures(table, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("[1.1235, 2.0]", text)
            written = json.loads(text)
        self.assertEqual(written["labels/x"]["kinks"]["identities"]["kinks"],
                         [[1.1235, 2.0], [3.0, 4.0]])
        self.assertEqual(written["labels/x"]["kinks"]["tasks"], [FLOORS, APEX])
        self.assertEqual(written["labels/x"]["kinks"]["counterfactual"],
                         "no-move-check+pixel-floors")

    def test_the_ratchet_tolerates_only_float_drift(self):
        self.assertEqual(bounds.ratchet({"kinks": 3.0}, {"kinks": 3.0}), ([], []))
        worse, better = bounds.ratchet({"kinks": 3.0}, {"kinks": 4.0})
        self.assertTrue(worse and not better)
        worse, better = bounds.ratchet({"kinks": 3.0}, {"kinks": 2.0})
        self.assertTrue(better and not worse)
        self.assertEqual(bounds.ratchet({"truth_distance_px": 2.0},
                                        {"truth_distance_px": 2.0005}), ([], []))
        self.assertTrue(bounds.ratchet({"truth_distance_px": 2.0},
                                       {"truth_distance_px": 2.01})[0])
        self.assertTrue(bounds.ratchet({"excess_px2": 5.0}, {"excess_px2": 5.1})[0])


class BoundsHelperTests(unittest.TestCase):
    """The bounds' helpers on hand-built inputs whose answer is known."""

    @staticmethod
    def arc_ring(radius: float, turn_deg: float, arm: float = 30.0) -> np.ndarray:
        """An open polyline: a straight arm, a round of ``radius`` turning by
        ``turn_deg`` (left), another straight arm; the round's middle at (0, 0)."""
        turn = math.radians(turn_deg)
        angles = np.linspace(-turn / 2, turn / 2, 200)
        # A round centred on (0, radius) seen from its middle at the origin.
        arc = np.c_[radius * np.sin(angles), radius * (1 - np.cos(angles))]
        start = arc[0] - arm * np.asarray([math.cos(-turn / 2), math.sin(-turn / 2)])
        end = arc[-1] + arm * np.asarray([math.cos(turn / 2), math.sin(turn / 2)])
        return np.vstack([start, arc, end])

    def test_round_turn_near_uses_the_sharpenable_radius_of_each_turn(self):
        allowance, reach = 1.0, 6.0
        # The move check, seen through Q: the sharp point r (sec(T/2) - 1) from the
        # arc, at most A + (1 + sec(T/2)) Q.
        for turn_deg in (60.0, 90.0, 120.0, 150.0):
            turn = math.radians(turn_deg)
            secant = 1.0 / math.cos(turn / 2)
            with self.subTest(turn=turn_deg):
                self.assertAlmostEqual(bounds.sharpenable_radius(turn, allowance),
                                       (allowance + (1 + secant) * bounds.Q_PX) / (secant - 1))
        right = bounds.sharpenable_radius(math.pi / 2, allowance)
        gentle = bounds.sharpenable_radius(math.radians(62), allowance)
        sharp = bounds.sharpenable_radius(math.radians(150), allowance)
        self.assertAlmostEqual(right, (allowance + (1 + math.sqrt(2)) * bounds.Q_PX)
                               / (math.sqrt(2) - 1))
        self.assertLess(sharp, right)
        self.assertLess(right, gentle)
        middle = (0.0, 0.0)

        def found(radius, turn, point=middle, vertices=None):
            return bounds.round_turn_near([self.arc_ring(radius, turn)], point, allowance,
                                          60.0, reach, vertices)

        # A whole round within the radius of its turn is found at that turn; a
        # larger one is not, not even through a stretch of it turning by the
        # threshold, which is judged by the round's full turn.
        for radius, turn, expected in ((0.9 * right, 90, 90.0), (1.2 * right, 90, 0.0),
                                       (0.9 * gentle, 62, 62.0), (1.1 * gentle, 62, 0.0),
                                       (0.9 * sharp, 150, 150.0), (1.2 * sharp, 150, 0.0)):
            with self.subTest(radius=radius, turn=turn):
                self.assertAlmostEqual(found(radius, turn), expected, delta=0.5)
        # A cusp far from the round's sharp point is not on it.
        self.assertEqual(found(0.9 * right, 90, (0.0, -3.0)), 0.0)
        self.assertEqual(found(0.9 * right, 90, (25.0, 0.0)), 0.0)
        # A round turning by less than the minimum turn is never reported.
        self.assertEqual(found(3.0, 45), 0.0)
        # A true vertex is no round: a sharp corner is judged by its own disc.
        corner = self.arc_ring(1e-6, 90)
        self.assertEqual(bounds.round_turn_near([corner], middle, allowance, 60.0, reach,
                                                [corner[1]]), 0.0)

    def test_the_node_route_splits_a_span_one_cubic_cannot_draw(self):
        # A quarter circle of radius 100 is one cubic within 0.03 px; a half circle
        # between two axis extremes needs two.
        t = np.linspace(0, math.pi / 2, 2000)
        quarter = np.c_[100 * np.cos(t), 100 * np.sin(t)]
        start, end = np.asarray([0.0, 1.0]), np.asarray([-1.0, 0.0])
        self.assertLess(bounds.span_cubic_error(quarter, start, end), 0.03)
        self.assertEqual(bounds.span_pieces(quarter, start, end, 0.293), 1)
        t = np.linspace(0, math.pi, 4000)
        half = np.c_[100 * np.cos(t), 100 * np.sin(t)]
        self.assertEqual(bounds.span_pieces(half, start, np.asarray([0.0, -1.0]), 0.293), 2)
        # A straight stretch is drawn exactly.
        line = np.c_[np.linspace(0, 50, 100), np.linspace(0, 20, 100)]
        direction = np.asarray([50.0, 20.0]) / math.hypot(50, 20)
        self.assertEqual(bounds.span_cubic_error(line, direction, direction), 0.0)

    def test_zone_node_limit_is_the_explicit_route(self):
        from shapely.geometry import Polygon
        from vinylgen.fidelity.truth import scene_truth
        fixed = {case.case_id: case for case in
                 families.load_fixed_set(ROOT / families.FIXED_DIR)}
        disc = scene_truth(fixed["disc-off0-128"].scene)
        tolerance = bounds.route_tolerance(bounds.resolved_lengths(128, 128))
        self.assertEqual(tolerance, 1.0)
        self.assertEqual(bounds.zone_node_limit(disc, Polygon(), tolerance=tolerance), 4)
        # A rotated 8:1 ellipse: its axis extremes are not its ideal nodes, and
        # the 155 degree spans between them each need a second cubic.
        ellipse = scene_truth(fixed["ellipse-a8-rot17-1536"].scene)
        tolerance = bounds.route_tolerance(bounds.resolved_lengths(1536, 1536))
        from vinylgen.fidelity.raster import render_labels
        quantisation = bounds.sample_quantisation(
            ellipse, np.asarray(render_labels(fixed["ellipse-a8-rot17-1536"].scene)))
        self.assertEqual(ellipse.ideal_nodes, 4)
        self.assertEqual(bounds.zone_node_limit(ellipse, Polygon()), 4)
        self.assertEqual(bounds.zone_node_limit(ellipse, Polygon(), tolerance=tolerance,
                                                quantisation=quantisation), 6)
        # The pixel-edge samples lie within about sqrt 2 / 2 of the truth.
        self.assertLess(max(q.max() for q in quantisation), 1.0)
        self.assertGreater(max(q.max() for q in quantisation), 0.5)
        # A zone over the whole canvas leaves nothing.
        self.assertEqual(bounds.zone_node_limit(ellipse, box(0, 0, 1536, 1536),
                                                tolerance=tolerance,
                                                quantisation=quantisation), 0)

    def test_the_node_limit_counts_the_nodes_the_label_map_forces(self):
        # Chain ends the trace stage anchors: a pixel corner with three colours, two
        # colours corner to corner, and a frame point between two colours.
        three = np.zeros((3, 4), dtype=np.int16)
        three[1:, 1:3] = 1
        three[1:, 3] = 2
        self.assertEqual(sorted(map(tuple, bounds.evidence_anchors(three).tolist())),
                         [(1.0, 3.0), (3.0, 1.0), (3.0, 3.0), (4.0, 1.0)])
        self.assertIn((1.0, 1.0),
                      map(tuple, bounds.evidence_anchors(np.array([[0, 1], [1, 0]])).tolist()))
        # A plain two-colour corner is no anchor.
        corner = np.zeros((4, 4), dtype=np.int16)
        corner[1:3, 1:3] = 1
        self.assertEqual(len(bounds.evidence_anchors(corner)), 0)
        from vinylgen.fidelity.raster import render_labels
        fixed = {case.case_id: case for case in
                 families.load_fixed_set(ROOT / families.FIXED_DIR)}
        # A six-colour pie's centre splits over three pixel corners, each a cusp the
        # trace must write where the ideal segmentation counts one junction.
        pie = fixed["pie-6-ph0-512"]
        anchors = bounds.evidence_anchors(np.asarray(render_labels(pie.scene)))
        centre = np.hypot(anchors[:, 0] - 256.0, anchors[:, 1] - 256.0) <= 2.0
        self.assertEqual(int(centre.sum()), 3)
        self.assertEqual(bounds.case_bounds(pie, "labels").node_limit, 11)
        # Two squares meeting corner to corner: the raster bridges or parts them,
        # two nodes either way.
        contact = fixed["corner-contact-rot0-ph0.5-1536"]
        cb = bounds.case_bounds(contact, "labels")
        self.assertEqual(bounds.corner_contacts(cb.truth, cb.corners), [[768.5, 768.5]])
        self.assertEqual(cb.node_limit, 8)

    def test_thin_regions_pin_a_thin_region_to_its_pixels_in_one_piece(self):
        labels = np.zeros((20, 40), dtype=np.int16)
        labels[10, 5:35] = 1          # a 1 px bar, 30 px long
        labels[2:6, 2:6] = 1          # a 4 x 4 block: not thin
        palette = ["#FFFFFF", "#000000"]
        bar = box(5, 10, 35, 11)
        block = box(2, 2, 6, 6)

        def rows(traced, exempt=None):
            return bounds.thin_regions(labels, palette, {"#000000": traced}, exempt, 1.7)

        found = rows(bar.union(block))
        self.assertEqual([(row["pixels"], row["recall"], row["pieces"]) for row in found],
                         [(30, 1.0, 1)])
        # A full pixel off its pixels: nothing covered.
        self.assertEqual(rows(box(5, 11, 35, 12))[0]["recall"], 0.0)
        # Cut by a 3 px gap: covered, but in two pieces.
        severed = rows(box(5, 10, 18, 11).union(box(21, 10, 35, 11)))[0]
        self.assertEqual((severed["recall"] > bounds.THIN_RECALL, severed["pieces"]),
                         (True, 2))
        # All its thin pixels in the vertex discs: left out.
        self.assertEqual(rows(bar, exempt=box(0, 9, 40, 12)), [])

    def test_the_fold_target_must_match_the_replay_where_the_colour_is_wide(self):
        import tempfile
        from types import SimpleNamespace
        from pathlib import Path
        from vinylgen.fidelity.raster import render_labels
        cases = {case.case_id: case for case in
                 families.load_fixed_set(ROOT / families.PALETTE_STAGE_DIR)}

        def fold(case_id, into):
            """``case_truth`` with a fake palette-stage run that folded colour 1
            (the dark outline) into ``into``."""
            case = cases[case_id]
            scene_labels = np.asarray(render_labels(case.scene))
            kept = [index for index in range(len(case.scene.palette)) if index != 1]
            inferred = np.full(scene_labels.shape, -1, dtype=np.int16)
            for position, index in enumerate(kept):
                inferred[scene_labels == index] = position
            inferred[scene_labels == 1] = kept.index(into)
            palette = [{"hex": "#%02X%02X%02X" % tuple(case.scene.palette[index]["rgb"]),
                        "rgb": case.scene.palette[index]["rgb"]} for index in kept]
            with tempfile.TemporaryDirectory() as directory:
                np.savez(Path(directory) / "segmentation.npz", labels=inferred)
                run = SimpleNamespace(
                    subject_dir=Path(directory), palette=palette,
                    colour_map={row["hex"]: index for row, index in zip(palette, kept)},
                    palette_matches=[{"truth_index": index, "matched": True} for index in kept])
                return bounds.case_truth(case, "rgb", run=run)[1]

        # The 2.5 px outline is at least D wide: the usage cutoff's replay folds it
        # into the paper (0), so folding it into the gold (2) is a mismatch.
        self.assertEqual(fold("palette-dark-outline-disc-w2.5-512", 0)["fold_mismatches"], [])
        self.assertEqual(fold("palette-dark-outline-disc-w2.5-512", 2)["fold_mismatches"],
                         [[1, 2, 0]])
        # The 1.5 px one is narrower than D everywhere: either target is immaterial.
        design = fold("palette-dark-outline-disc-w1.5-512", 2)
        self.assertEqual(design["fold_mismatches"], [])
        self.assertTrue(any("immaterial" in reason for reason in design["reasons"]))
        # The evidence assertion fails on a recorded mismatch, and names it.
        evidence = {"tolerance_px": 2.0, "missing_regions": [], "colours": {},
                    "absent_scene_colours": [], "misplaced_thin_regions": [],
                    "severed_thin_regions": [], "fold_target_mismatches": []}
        result = {"bound_px": 1.7, "evidence": evidence}
        self.assertTrue(bounds.check("evidence", result, None)[0])
        evidence["fold_target_mismatches"] = [[1, 2, 0]]
        passed, detail = bounds.check("evidence", result, None)
        self.assertFalse(passed)
        self.assertIn("colour 1 folded into 2, not 0", detail)
        self.assertEqual(bounds.measurements("evidence", result, None)["fold_target_mismatches"],
                         1.0)
        self.assertEqual(bounds.identities("evidence", result, None)["fold_target_mismatches"],
                         [[1, 2, 0]])

class RecordScriptTests(unittest.TestCase):
    """``scripts/record_expected_failures.py`` writes all or nothing and refuses
    orphan entries unless told to remove them."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = ROOT / "scripts" / "record_expected_failures.py"
        spec = importlib.util.spec_from_file_location("record_expected_failures", path)
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    KINKS = {"kinks": [[1.0, 2.0]]}

    def traced(self):
        from types import SimpleNamespace
        case = SimpleNamespace(scene=SimpleNamespace(params={}))
        return {"labels/a": ("labels", case), "labels/b": ("labels", case)}

    def failing(self, kinks=None):
        found = {"kinks": kinks if kinks is not None else self.KINKS["kinks"]}
        return {"verdicts": {"kinks": {"passed": False, "detail": "kink",
                                       "measured": {"kinks": float(len(found["kinks"]))},
                                       "identities": found}}}

    def entry(self):
        return {"tasks": [FLOORS], "counterfactual": "pixel-floors",
                "measured": {"kinks": 1.0}, "identities": dict(self.KINKS)}

    BOTH = {"tasks": [APEX, FLOORS], "counterfactual": "no-move-check+pixel-floors"}

    def test_attributions_name_several_tasks_and_their_counterfactual(self):
        parsed = self.script.parse_tasks(
            [f"labels/a:kinks={APEX},{FLOORS}@no-move-check+pixel-floors",
             f"labels/b:kinks={PLACEHOLDER_SLUG_PREFIX}some-cause"])
        self.assertEqual(parsed[("labels/a", "kinks")], self.BOTH)
        self.assertEqual(parsed[("labels/b", "kinks")],
                         {"tasks": [PLACEHOLDER_SLUG_PREFIX + "some-cause"],
                          "counterfactual": None})
        for bad in (f"labels/a:kinks={FLOORS}", f"labels/a:kinks={FLOORS}@no-move-check",
                    f"labels/a:kinks={APEX},{FLOORS}@pixel-floors",
                    f"labels/a:kinks={FLOORS}@unknown", "labels/a:kinks"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.script.parse_tasks([bad])

    def test_an_unrecorded_failure_blocks_the_write(self):
        table = {"labels/a": {"kinks": self.entry()}}
        report = self.script.reconcile(table, {"labels/a": self.failing(),
                                               "labels/b": self.failing()}, self.traced(), {})
        self.assertTrue(report["blocked"])
        self.assertEqual(len(report["unrecorded"]), 1)
        report = self.script.reconcile(table, {"labels/b": self.failing()}, self.traced(),
                                       {("labels/b", "kinks"): dict(self.BOTH)})
        self.assertFalse(report["blocked"])
        self.assertEqual({k: table["labels/b"]["kinks"][k] for k in self.BOTH}, self.BOTH)

    def test_retask_moves_entries_and_writes_nothing_if_one_is_missing(self):
        from unittest import mock
        table = {"labels/a": {"kinks": self.entry()}}
        with mock.patch.object(self.script.regression, "load_expected_failures",
                               side_effect=lambda: json.loads(json.dumps(table))), \
                mock.patch.object(self.script.regression, "write_expected_failures") as write:
            self.assertEqual(self.script.retask({("labels/a", "kinks"): dict(self.BOTH),
                                                 ("labels/b", "kinks"): dict(self.BOTH)},
                                                False), 1)
            write.assert_not_called()
            self.assertEqual(self.script.retask({("labels/a", "kinks"): dict(self.BOTH)},
                                                False), 0)
            written = write.call_args.args[0]
        self.assertEqual(written["labels/a"]["kinks"], {**self.entry(), **self.BOTH})

    def test_an_orphan_entry_is_refused_unless_removed(self):
        table = {"labels/gone": {"kinks": self.entry()}, "labels/a": {"kinks": self.entry()}}
        report = self.script.reconcile(table, {"labels/a": self.failing()}, self.traced(), {})
        self.assertTrue(report["blocked"])
        self.assertIn("labels/gone", table)
        report = self.script.reconcile(table, {"labels/a": self.failing()}, self.traced(), {},
                                       remove_orphans=True)
        self.assertFalse(report["blocked"])
        self.assertNotIn("labels/gone", table)

    def test_a_swapped_defect_is_refused_unless_worse_is_allowed(self):
        table = {"labels/a": {"kinks": self.entry()}}
        moved = self.failing([[50.0, 60.0]])
        report = self.script.reconcile(table, {"labels/a": moved}, self.traced(), {})
        self.assertTrue(report["blocked"] and report["refused"])
        self.assertEqual(table["labels/a"]["kinks"]["identities"], self.KINKS)
        report = self.script.reconcile(table, {"labels/a": moved}, self.traced(), {},
                                       allow_worse=True)
        self.assertFalse(report["blocked"])
        self.assertEqual(table["labels/a"]["kinks"]["identities"], {"kinks": [[50.0, 60.0]]})

    def test_a_passing_entry_is_removed(self):
        table = {"labels/a": {"kinks": self.entry()}}
        passing = {"verdicts": {"kinks": {"passed": True, "detail": ""}}}
        report = self.script.reconcile(table, {"labels/a": passing}, self.traced(), {})
        self.assertFalse(report["blocked"])
        self.assertEqual(table, {})


class ExpectedFailureWrappingTests(unittest.TestCase):
    """Only an assertion's ``AssertionError`` is its expected failure, and only while
    its measurements match the recorded ones."""

    RECORDED = {"truth_distance_px": 3.0, "truth_length_beyond_px": 10.0,
                "evidence_distance_px": 1.0, "evidence_length_beyond_px": 0.0}
    IDENTITIES = {"truth_worst_point": [[5.0, 6.0]], "evidence_worst_point": [[7.0, 8.0]]}

    def outcome(self, check, entry="default", available=None, measured=None,
                found=None) -> unittest.TestResult:
        from types import SimpleNamespace
        from unittest import mock
        if entry == "default":
            entry = {"tasks": [FLOORS], "counterfactual": "pixel-floors",
                     "measured": dict(self.RECORDED),
                     "identities": dict(self.IDENTITIES)}
        body = {"case": SimpleNamespace(case_id="probe"), "tier": "labels", "result": {},
                "__module__": __name__,
                "test_hausdorff": _assertion_method("hausdorff", entry)}
        cls = type("Probe", (_SyntheticCase, unittest.TestCase), body)
        result = unittest.TestResult()
        values = dict(self.RECORDED if measured is None else measured)
        items = dict(self.IDENTITIES if found is None else found)
        with mock.patch.object(bounds, "check", check), \
                mock.patch.object(bounds, "measurements", lambda name, result, case: values), \
                mock.patch.object(bounds, "identities", lambda name, result, case: items), \
                mock.patch.object(bounds, "available", lambda name, result: available):
            cls("test_hausdorff").run(result)
        return result

    def test_an_assertion_failure_as_recorded_is_the_expected_failure(self):
        result = self.outcome(lambda name, result, case: (False, "outside"))
        self.assertEqual((len(result.expectedFailures), len(result.errors),
                          len(result.failures), len(result.unexpectedSuccesses)), (1, 0, 0, 0))

    def test_a_failure_worse_than_recorded_is_a_failure(self):
        worse = dict(self.RECORDED, truth_length_beyond_px=40.0)
        result = self.outcome(lambda name, result, case: (False, "outside"), measured=worse)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (0, 1))
        self.assertIn("worse than recorded", result.failures[0][1])

    def test_a_failure_better_than_recorded_is_a_failure(self):
        better = dict(self.RECORDED, truth_distance_px=2.5)
        result = self.outcome(lambda name, result, case: (False, "outside"), measured=better)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (0, 1))
        self.assertIn("better than recorded", result.failures[0][1])

    def test_a_failure_elsewhere_with_the_same_measurements_is_a_failure(self):
        moved = dict(self.IDENTITIES, truth_worst_point=[[50.0, 60.0]])
        result = self.outcome(lambda name, result, case: (False, "outside"), found=moved)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (0, 1))
        self.assertIn("worse than recorded", result.failures[0][1])

    def test_a_pass_is_an_unexpected_success(self):
        result = self.outcome(lambda name, result, case: (True, ""))
        self.assertEqual((len(result.expectedFailures), len(result.unexpectedSuccesses)), (0, 1))

    def test_any_other_exception_is_an_error(self):
        def broken(name, result, case):
            raise KeyError("missing measurement")
        result = self.outcome(broken)
        self.assertEqual((len(result.expectedFailures), len(result.errors)), (0, 1))
        self.assertIn("KeyError", result.errors[0][1])

    def test_an_entry_for_an_assertion_with_nothing_to_hold_is_an_error(self):
        result = self.outcome(lambda name, result, case: (False, ""), available="all narrow")
        self.assertEqual((len(result.expectedFailures), len(result.errors),
                          len(result.skipped)), (0, 1, 0))

    def test_an_unexpected_exception_without_an_entry_is_an_error(self):
        def broken(name, result, case):
            raise ValueError("bad")
        result = self.outcome(broken, entry=None)
        self.assertEqual((len(result.errors), len(result.failures)), (1, 0))


if __name__ == "__main__":
    unittest.main()
