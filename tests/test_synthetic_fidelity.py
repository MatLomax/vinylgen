"""Synthetic ground-truth fidelity regression tests of the default trace: the gate
slice.

The committed synthetic cases (``tests/synthetic/fixed``, ``tests/synthetic/
palette-stage`` and the pinned fuzz scenes listed in ``tests/synthetic/
fuzz-seeds.json``) are traced with the product defaults (``TraceSettings()``)
through the real stages and each edge variant judged against the case's truth and
the raster evidence the trace received. Two tiers:

- ``labels``: the hard-edged label map (``raster.render_labels``) straight into the
  trace stage (``harness.trace_labels``);
- ``rgb``: the anti-aliased render through the palette stage first
  (``harness.trace_rgb``, with the case's ``families.case_palette_settings``).
  Palette-stage cases run in this tier only.

This module traces the gate slice (``regression.gate_runs``); the full corpus is
``tests/fullimage/test_synthetic_fidelity_corpus.py``, run with
``scripts/run_tests.py --full-image``. The generated classes and how an
expected failure is judged are in ``tests/synthetic_cases.py``; the runs and the
expected-failure table's format in ``vinylgen/fidelity/regression.py``; the
bounds, the assertions and why each holds in ``vinylgen/fidelity/bounds.py``, the
one yardstick the fidelity sweep and tuner use too.
"""

from __future__ import annotations

import json
import math
import unittest

import numpy as np
from shapely.geometry import box

from synthetic_cases import SyntheticCase, assertion_method, generate
from vinylgen import curve_fit
from vinylgen.fidelity import bounds, families, regression
from vinylgen.fidelity.regression import ROOT, all_runs, gate_runs, load_expected_failures

FLOORS = "stop-default-per-mille-trace-lengths-collapsing-below-a-pixe"
APEX = "let-a-quantised-corner-apex-sharpen-beyond-the-corner-sharpe"
ASSERTIONS = bounds.ASSERTIONS

globals().update(generate(gate_runs(), __name__))


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
        def entry(tasks, measured=None):
            return {"tasks": tasks, "measured": {"kinks": 3.0} if measured is None else measured}

        for good in (entry([FLOORS]), entry([APEX, FLOORS])):
            with self.subTest(good=good):
                self.assertEqual(regression.entry_problems("kinks", good), [])
        for bad in ("some-task", {"task": FLOORS, "measured": {"kinks": 3.0}},
                    {"tasks": [FLOORS]}, dict(entry([FLOORS]), counterfactual=None),
                    entry([]), entry(FLOORS), entry(["Some Task"]), entry([FLOORS, FLOORS]),
                    entry([FLOORS], {"nodes_outside_zones": 3.0}),
                    entry([FLOORS], {"kinks": -1.0}), entry([FLOORS], {"kinks": "3"})):
            with self.subTest(bad=bad):
                self.assertTrue(regression.entry_problems("kinks", bad))

    def test_the_gate_slice_names_distinct_traced_runs(self):
        keys = regression.gate_slice()
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sorted(keys), sorted(f"{tier}/{case.case_id}"
                                              for tier, case in gate_runs()))

    def test_only_a_measurement_worse_beyond_its_margin_is_worse(self):
        self.assertEqual(bounds.worse_than_recorded({"kinks": 3.0}, {"kinks": 3.0}), [])
        self.assertTrue(bounds.worse_than_recorded({"kinks": 3.0}, {"kinks": 4.0}))
        self.assertEqual(bounds.worse_than_recorded({"kinks": 3.0}, {"kinks": 0.0}), [])
        # A length within half a pixel or 10%, an area within 4 px^2 or 10%.
        self.assertEqual(bounds.worse_than_recorded({"truth_distance_px": 2.0},
                                                    {"truth_distance_px": 2.5}), [])
        self.assertTrue(bounds.worse_than_recorded({"truth_distance_px": 2.0},
                                                   {"truth_distance_px": 2.51}))
        self.assertEqual(bounds.worse_than_recorded({"truth_length_beyond_px": 40.0},
                                                    {"truth_length_beyond_px": 44.0}), [])
        self.assertTrue(bounds.worse_than_recorded({"truth_length_beyond_px": 40.0},
                                                   {"truth_length_beyond_px": 44.1}))
        self.assertEqual(bounds.worse_than_recorded({"excess_px2": 5.0},
                                                    {"excess_px2": 9.0}), [])
        self.assertTrue(bounds.worse_than_recorded({"excess_px2": 5.0}, {"excess_px2": 9.1}))

    def test_the_table_is_written_in_its_measurement_order(self):
        import tempfile
        from pathlib import Path
        table = {"labels/x": {"hausdorff": {"tasks": [FLOORS, APEX], "measured": {
            "evidence_length_beyond_px": 0.0, "truth_distance_px": 2.0,
            "evidence_distance_px": 1.0, "truth_length_beyond_px": 3.0}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.json"
            regression.write_expected_failures(table, path)
            written = json.loads(path.read_text(encoding="utf-8"))
        entry = written["labels/x"]["hausdorff"]
        self.assertEqual(entry["tasks"], [FLOORS, APEX])
        self.assertEqual(list(entry["measured"]), list(bounds.MEASUREMENTS["hausdorff"]))


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
        skip = curve_fit._TIP_SKIP_ALLOWANCES * allowance
        # The move check and the tip-skip bound, seen through Q: the sharp point
        # r (sec(T/2) - 1) from the arc, at most min(A, S tan(T/4) + Q) + (1 +
        # sec(T/2)) Q.
        for turn_deg in (20.0, 40.0, 60.0, 90.0, 120.0, 150.0):
            turn = math.radians(turn_deg)
            secant = 1.0 / math.cos(turn / 2)
            room = min(allowance, skip * math.tan(turn / 4) + bounds.Q_PX)
            with self.subTest(turn=turn_deg):
                self.assertAlmostEqual(bounds.sharpenable_radius(turn, allowance, skip),
                                       (room + (1 + secant) * bounds.Q_PX) / (secant - 1))
        right = bounds.sharpenable_radius(math.pi / 2, allowance, skip)
        gentle = bounds.sharpenable_radius(math.radians(62), allowance, skip)
        sharp = bounds.sharpenable_radius(math.radians(150), allowance, skip)
        self.assertAlmostEqual(right, (allowance + (1 + math.sqrt(2)) * bounds.Q_PX)
                               / (math.sqrt(2) - 1))
        self.assertLess(sharp, right)
        self.assertLess(right, gentle)
        middle = (0.0, 0.0)
        minimum = 5.0

        def found(radius, turn, point=middle, vertices=None, allowance=allowance, skip=skip):
            return bounds.round_turn_near([self.arc_ring(radius, turn)], point, allowance, skip,
                                          minimum, reach, vertices)

        # A whole round within the radius of its turn is found at that turn; a
        # larger one is not, not even through a stretch of it turning by the
        # minimum turn, which is judged by the round's full turn.
        for radius, turn, expected in ((0.9 * right, 90, 90.0), (1.2 * right, 90, 0.0),
                                       (0.9 * gentle, 62, 62.0), (1.1 * gentle, 62, 0.0),
                                       (0.9 * sharp, 150, 150.0), (1.2 * sharp, 150, 0.0)):
            with self.subTest(radius=radius, turn=turn):
                self.assertAlmostEqual(found(radius, turn), expected, delta=0.5)
        # A cusp far from the round's sharp point is not on it.
        self.assertEqual(found(0.9 * right, 90, (0.0, -3.0)), 0.0)
        self.assertEqual(found(0.9 * right, 90, (25.0, 0.0)), 0.0)
        # A round turning by less than the minimum turn is never reported.
        self.assertEqual(found(3.0, 4), 0.0)
        # A true vertex is no round: a sharp corner is judged by its own disc.
        corner = self.arc_ring(1e-6, 90)
        self.assertEqual(bounds.round_turn_near([corner], middle, allowance, skip, minimum,
                                                reach, [corner[1]]), 0.0)

    def test_a_large_shallow_round_is_sharpenable_only_within_the_tip_skip(self):
        # At A = 4 px a 30-degree round of radius 120 px has its sharp point
        # 4.1 px from its arc, within the allowance seen through Q, but its
        # tangent points 32 px from the sharp point, far beyond the tip skip
        # (9.7 px): its sides do not reach the corner, so it stays round.
        allowance = 4.0
        skip = curve_fit._TIP_SKIP_ALLOWANCES * allowance
        turn = math.radians(30.0)
        secant = 1.0 / math.cos(turn / 2)
        by_allowance = (allowance + (1 + secant) * bounds.Q_PX) / (secant - 1)
        radius = bounds.sharpenable_radius(turn, allowance, skip)
        self.assertLess(radius, 0.7 * by_allowance)
        self.assertLess(120.0, by_allowance)
        self.assertGreater(120.0, radius)
        ring = self.arc_ring(120.0, 30.0, arm=80.0)
        self.assertEqual(bounds.round_turn_near([ring], (0.0, 0.0), allowance, skip, 5.0,
                                                10.0), 0.0)
        ring = self.arc_ring(0.9 * radius, 30.0, arm=80.0)
        self.assertAlmostEqual(bounds.round_turn_near([ring], (0.0, 0.0), allowance, skip,
                                                      5.0, 10.0), 30.0, delta=0.5)

    def test_corner_roles_follow_the_detectors_noise_rule(self):
        # A true corner is required when its turn reaches (K + 1) times its two
        # sides' direction noise atan2(Q, L), optional from (K - 1) times it, and
        # shallow below; L is the side's grown-window chord, capped by the
        # detector's longest window and by the neighbouring true vertex.
        from types import SimpleNamespace
        lengths = bounds.resolved_lengths(1024, 1024)
        ratio = curve_fit._CORNER_NOISE_RATIO
        cap, skip, minimum = bounds.corner_windows(lengths)
        self.assertAlmostEqual(cap, curve_fit._side_cap(lengths))
        long_noise = 2 * bounds.side_noise_deg(cap)

        def roles(turn_deg, side):
            # A kite: the apex at (0, 0) turning by ``turn_deg`` between two
            # straight sides of length ``side``, closed far away.
            half = math.radians(180.0 - turn_deg) / 2
            left = (-side * math.sin(half), side * math.cos(half))
            right = (side * math.sin(half), side * math.cos(half))
            ring = np.asarray([(0.0, 0.0), right, (0.0, 4 * side), left, (0.0, 0.0)])
            corners = [SimpleNamespace(point=(0.0, 0.0), kind="corner", turn_deg=turn_deg)]
            for point in (right, (0.0, 4 * side), left):
                corners.append(SimpleNamespace(point=point, kind="corner", turn_deg=90.0))
            return bounds.corner_roles(SimpleNamespace(corners=corners), [ring], lengths)[0]

        long_side = 4 * (cap + skip)
        self.assertEqual(roles((ratio + 1) * long_noise + 0.5, long_side)["role"], "required")
        self.assertEqual(roles((ratio + 1) * long_noise - 0.5, long_side)["role"], "optional")
        self.assertEqual(roles((ratio - 1) * long_noise + 0.3, long_side)["role"], "optional")
        self.assertEqual(roles((ratio - 1) * long_noise - 0.3, long_side)["role"], "shallow")
        # A 20-degree vertex between long straight sides is required; the same
        # vertex between sides that end 4 px away is not.
        self.assertEqual(roles(20.0, long_side)["role"], "required")
        self.assertNotEqual(roles(20.0, skip + minimum)["role"], "required")
        row = roles(20.0, long_side)
        self.assertAlmostEqual(row["noise_deg"], long_noise, places=6)

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


class RecordScriptTests(unittest.TestCase):
    """``scripts/record_expected_failures.py`` writes all or nothing, refuses a worse
    record and orphan entries unless told otherwise, and tightens a better one."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = ROOT / "scripts" / "record_expected_failures.py"
        spec = importlib.util.spec_from_file_location("record_expected_failures", path)
        cls.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.script)

    def traced(self):
        from types import SimpleNamespace
        case = SimpleNamespace(scene=SimpleNamespace(params={}))
        return {"labels/a": ("labels", case), "labels/b": ("labels", case)}

    @staticmethod
    def failing(kinks: float = 1.0):
        return {"verdicts": {"kinks": {"passed": False, "detail": "kink",
                                       "measured": {"kinks": kinks}}}}

    @staticmethod
    def entry(kinks: float = 1.0):
        return {"tasks": [FLOORS], "measured": {"kinks": kinks}}

    def test_tasks_name_one_or_more_slugs(self):
        parsed = self.script.parse_tasks([f"labels/a:kinks={APEX},{FLOORS}"])
        self.assertEqual(parsed[("labels/a", "kinks")], [APEX, FLOORS])
        for bad in ("labels/a:kinks", "labels/a:kinks=Some Task", f"labels/a:kinks={FLOORS}@x"):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.script.parse_tasks([bad])

    def test_an_unrecorded_failure_blocks_the_write(self):
        table = {"labels/a": {"kinks": self.entry()}}
        report = self.script.reconcile(table, {"labels/a": self.failing(),
                                               "labels/b": self.failing()}, self.traced(), {})
        self.assertTrue(report["blocked"])
        self.assertEqual(len(report["unrecorded"]), 1)
        report = self.script.reconcile(table, {"labels/b": self.failing()}, self.traced(),
                                       {("labels/b", "kinks"): [APEX, FLOORS]})
        self.assertFalse(report["blocked"])
        self.assertEqual(table["labels/b"]["kinks"], {"tasks": [APEX, FLOORS],
                                                      "measured": {"kinks": 1.0}})

    def test_an_orphan_entry_is_refused_unless_removed(self):
        table = {"labels/gone": {"kinks": self.entry()}, "labels/a": {"kinks": self.entry()}}
        report = self.script.reconcile(table, {"labels/a": self.failing()}, self.traced(), {})
        self.assertTrue(report["blocked"])
        self.assertIn("labels/gone", table)
        report = self.script.reconcile(table, {"labels/a": self.failing()}, self.traced(), {},
                                       remove_orphans=True)
        self.assertFalse(report["blocked"])
        self.assertNotIn("labels/gone", table)

    def test_a_worse_record_is_refused_unless_allowed_and_a_better_one_tightens(self):
        table = {"labels/a": {"kinks": self.entry(2.0)}}
        report = self.script.reconcile(table, {"labels/a": self.failing(3.0)}, self.traced(), {})
        self.assertTrue(report["blocked"] and report["refused"])
        self.assertEqual(table["labels/a"]["kinks"]["measured"], {"kinks": 2.0})
        report = self.script.reconcile(table, {"labels/a": self.failing(3.0)}, self.traced(), {},
                                       allow_worse=True)
        self.assertFalse(report["blocked"])
        self.assertEqual(table["labels/a"]["kinks"]["measured"], {"kinks": 3.0})
        report = self.script.reconcile(table, {"labels/a": self.failing(1.0)}, self.traced(), {})
        self.assertFalse(report["blocked"])
        self.assertEqual(table["labels/a"]["kinks"]["measured"], {"kinks": 1.0})

    def test_a_length_worse_within_its_margin_keeps_its_record(self):
        def failing(distance):
            return {"verdicts": {"hausdorff": {"passed": False, "detail": "far", "measured": {
                "truth_distance_px": distance, "truth_length_beyond_px": 10.0,
                "evidence_distance_px": 1.0, "evidence_length_beyond_px": 0.0}}}}

        recorded = {"truth_distance_px": 2.0, "truth_length_beyond_px": 12.0,
                    "evidence_distance_px": 1.0, "evidence_length_beyond_px": 0.0}
        table = {"labels/a": {"hausdorff": {"tasks": [FLOORS], "measured": dict(recorded)}}}
        report = self.script.reconcile(table, {"labels/a": failing(2.3)}, self.traced(), {})
        self.assertFalse(report["blocked"])
        # The worse distance keeps its record; the better length tightens.
        self.assertEqual(table["labels/a"]["hausdorff"]["measured"],
                         dict(recorded, truth_length_beyond_px=10.0))

    def test_a_passing_entry_is_removed(self):
        table = {"labels/a": {"kinks": self.entry()}}
        passing = {"verdicts": {"kinks": {"passed": True, "detail": ""}}}
        report = self.script.reconcile(table, {"labels/a": passing}, self.traced(), {})
        self.assertFalse(report["blocked"])
        self.assertEqual(table, {})


class ExpectedFailureWrappingTests(unittest.TestCase):
    """Only an assertion's ``AssertionError`` is its expected failure, and only while
    its measurements are no worse than the recorded ones."""

    RECORDED = {"truth_distance_px": 3.0, "truth_length_beyond_px": 10.0,
                "evidence_distance_px": 1.0, "evidence_length_beyond_px": 0.0}

    def outcome(self, check, entry="default", available=None,
                measured=None) -> unittest.TestResult:
        from types import SimpleNamespace
        from unittest import mock
        if entry == "default":
            entry = {"tasks": [FLOORS], "measured": dict(self.RECORDED)}
        body = {"case": SimpleNamespace(case_id="probe"), "tier": "labels", "result": {},
                "__module__": __name__,
                "test_hausdorff": assertion_method("hausdorff", entry)}
        cls = type("Probe", (SyntheticCase, unittest.TestCase), body)
        result = unittest.TestResult()
        values = dict(self.RECORDED if measured is None else measured)
        with mock.patch.object(bounds, "check", check), \
                mock.patch.object(bounds, "measurements", lambda name, result, case: values), \
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

    def test_a_failure_better_than_recorded_is_still_the_expected_failure(self):
        better = dict(self.RECORDED, truth_distance_px=1.5)
        result = self.outcome(lambda name, result, case: (False, "outside"), measured=better)
        self.assertEqual((len(result.expectedFailures), len(result.failures)), (1, 0))

    def test_a_pass_is_an_unexpected_success(self):
        result = self.outcome(lambda name, result, case: (True, ""))
        self.assertEqual((len(result.expectedFailures), len(result.unexpectedSuccesses)), (0, 1))

    def test_an_entry_without_measurements_is_an_error(self):
        result = self.outcome(lambda name, result, case: (False, "outside"),
                              entry={"tasks": [FLOORS]})
        self.assertEqual((len(result.expectedFailures), len(result.errors)), (0, 1))

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
