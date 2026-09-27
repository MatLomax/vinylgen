"""No traced path crosses itself.

The detector (``vinylgen.path_crossings``) is checked on hand-built pieces and
against brute force; the repair is checked through the real boundary graph and
chain fit on synthetic label maps whose first fit crosses, for the shared
chains and for the overlap buffer's own rings. Emitted paths are judged by an
independent oracle (``sampled_contacts``: the true curves densely sampled and
noded by GEOS), never by the detector alone.
"""

import re
import unittest

import numpy as np
import shapely
from shapely.geometry import Polygon, box

from vinylgen.boundary_graph import shared_boundary_polygons
from vinylgen.contracts import PX_DEFAULTS, TraceSettings
from vinylgen.curve_fit import (
    ChainFit, FitMetrics, HardAnchor, SharedChainFitter, _pin, _repair_crossings, _repair_dense,
    _node_samples, _repaired, geometry_components, shared_boundary_components)
from vinylgen.path_crossings import crossings, cubic_loop

SETTINGS = TraceSettings(**{**PX_DEFAULTS, "max_deviation": 2.0, "side_smoothness": 1.6,
                            "corner_sharpening_allowance": 2.0}, length_unit="px")
TOKEN = re.compile(r"[MLCZ]|-?[0-9.]+(?:e-?[0-9]+)?")


def path_pieces(d: str) -> dict:
    """Each closed subpath of an SVG path as one piece ``(start, primitives)``."""
    tokens = TOKEN.findall(d)
    pieces, index, current = {}, 0, None

    def number():
        nonlocal index
        index += 1
        return float(tokens[index - 1])

    while index < len(tokens):
        command = tokens[index]
        index += 1
        if command == "M":
            current = [(number(), number()), []]
            pieces[len(pieces)] = current
        elif command == "L":
            current[1].append(("L", (number(), number())))
        elif command == "C":
            current[1].append(("C", (number(), number()), (number(), number()),
                               (number(), number())))
        elif current[1] and current[1][-1][-1] != current[0]:
            current[1].append(("L", current[0]))
    return {key: (start, tuple(primitives)) for key, (start, primitives) in pieces.items()}


def sampled_contacts(d_values: list[str], error: float = 1e-5) -> int:
    """Illegal contacts among the subpaths of ``d_values``, found without the detector.

    Each cubic is sampled uniformly, finely enough that the samples stay within
    ``error`` px of it (``n`` pieces stray at most ``max|B''| / (8 n^2)``), and
    GEOS nodes all of the linework: a node no line carries is a crossing, a node
    on a line that does not carry it is a touch inside a segment, and noding that
    loses length is linework running along itself. Touches at a sample both
    lines carry are none of these.
    """
    lines, own = [], []
    for d in d_values:
        for start, primitives in path_pieces(d).values():
            points, cursor = [np.asarray([start])], np.asarray(start)
            for primitive in primitives:
                end = np.asarray(primitive[-1])
                if primitive[0] == "L":
                    points.append(end[None])
                else:
                    c1, c2 = np.asarray(primitive[1]), np.asarray(primitive[2])
                    bend = max(np.hypot(*(cursor - 2 * c1 + c2)), np.hypot(*(c1 - 2 * c2 + end)))
                    pieces = max(1, int(np.ceil(np.sqrt(0.75 * bend / error))))
                    t = np.linspace(0.0, 1.0, pieces + 1)[:, None]
                    points.append(((1 - t) ** 3 * cursor + 3 * (1 - t) ** 2 * t * c1
                                   + 3 * (1 - t) * t * t * c2 + t ** 3 * end)[1:])
                cursor = end
            line = np.concatenate(points)
            line = line[np.r_[True, np.any(line[1:] != line[:-1], axis=1)]]
            if len(line) > 1:
                lines.append(shapely.LineString(line))
                own.append(set(map(tuple, line.tolist())))
    linework = shapely.MultiLineString(lines)
    noded = shapely.node(linework)
    nodes = sorted(set(map(tuple, shapely.get_coordinates(noded).tolist())))
    everywhere = set().union(*own)
    # A node no line carries is a crossing; one lying on a line that does not
    # carry it is a touch inside that line's segment.
    point_index, line_index = shapely.STRtree(lines).query(
        shapely.points(nodes), predicate="intersects")
    added = {nodes[k] for k in range(len(nodes)) if nodes[k] not in everywhere}
    added |= {nodes[k] for k, j in zip(point_index.tolist(), line_index.tolist())
              if nodes[k] not in own[j]}
    lost = linework.length - noded.length > 1e-9 * max(1.0, linework.length)
    return len(added) + int(lost)


def blobs_with_a_stripe(seed: int, size: int = 48) -> np.ndarray:
    """Three colours: overlapping discs, crossed by a one-pixel stripe of mixed colour."""
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:size, 0:size]
    labels = np.zeros((size, size), np.int16)
    for colour in (1, 2):
        for _ in range(2):
            cx, cy = rng.uniform(0, size, 2)
            radius = rng.uniform(4, 14)
            labels[(xs - cx) ** 2 + (ys - cy) ** 2 < radius * radius] = colour
    x0, y0, x1, y1 = rng.uniform(0, size, 4)
    for t in np.linspace(0, 1, 200):
        labels[int(y0 + t * (y1 - y0)), int(x0 + t * (x1 - x0))] = rng.integers(0, 3)
    return labels


def track_for(labels: np.ndarray):
    costs = np.stack([(labels != colour).astype(np.float32) for colour in range(3)], axis=-1)
    return shared_boundary_polygons(labels, [0, 1, 2], costs,
                                    protected_features=np.zeros(labels.shape, bool))


def line(*points):
    return points[0], tuple(("L", point) for point in points[1:])


class DetectorTests(unittest.TestCase):
    def test_the_cubic_loop_test_agrees_with_brute_force(self):
        rng = np.random.default_rng(7)
        t = np.linspace(0.0, 1.0, 4001)[:, None]
        loops = 0
        for _ in range(600):
            p0, c1, c2, p3 = rng.uniform(-10, 10, (4, 2))
            curve = ((1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * c1
                     + 3 * (1 - t) * t * t * c2 + t ** 3 * p3)
            brute = not shapely.LineString(curve).is_simple
            loops += brute
            with self.subTest(controls=(p0, c1, c2, p3)):
                self.assertEqual(cubic_loop(*map(tuple, (p0, c1, c2, p3))) is not None, brute)
                piece = (tuple(p0), (("C", tuple(c1), tuple(c2), tuple(p3)),))
                self.assertEqual(bool(crossings({"piece": piece})), brute)
        self.assertGreater(loops, 30)

    def test_a_crossing_shallower_than_any_sampling_is_found(self):
        # A line then a cubic that crosses back over it 0.003 px deep, beside
        # their shared node.
        start, node = (964.0477512, 796.45), (964.8945444, 792.45)
        piece = (start, (("L", node), ("C", (964.6092942, 793.6073821),
                                       (964.7319347, 794.8380847), (964.55, 796.0))))
        self.assertTrue(crossings({"piece": piece}))

    def test_a_hernia_inside_one_cubic_is_a_crossing(self):
        piece = ((0.0, 0.0), (("C", (12.0, 8.0), (-2.0, 8.0), (10.0, 0.0)), ("L", (20.0, 0.0))))
        found = crossings({"piece": piece})
        self.assertEqual({(item.first, item.second) for item in found}, {("piece", "piece")})

    def test_pieces_meeting_at_their_ends_are_legal(self):
        self.assertEqual(crossings({
            "a": line((0.0, 0.0), (5.0, 0.0), (10.0, 0.0)),
            "b": ((10.0, 0.0), (("C", (12.0, 3.0), (14.0, 3.0), (16.0, 0.0)),)),
            "c": line((10.0, 0.0), (10.0, -6.0)),
            "ring": line((0.0, 5.0), (4.0, 5.0), (4.0, 9.0), (0.0, 9.0), (0.0, 5.0)),
            "closed cubic": ((30.0, 0.0), (("C", (35.0, 5.0), (25.0, 5.0), (30.0, 0.0)),)),
        }), [])

    def test_a_touch_at_a_shared_vertex_is_legal(self):
        # A hole touching its outline at one vertex, and a piece ending on
        # another's vertex, meet without crossing.
        outline = line((0.0, 0.0), (10.0, 0.0), (10.0, 5.0), (10.0, 10.0), (0.0, 10.0),
                       (0.0, 0.0))
        hole = line((10.0, 5.0), (5.0, 3.0), (5.0, 7.0), (10.0, 5.0))
        through = line((0.0, 20.0), (5.0, 20.0), (10.0, 20.0))
        ending = line((5.0, 20.0), (5.0, 25.0))
        wall = line((10.0, 0.0), (10.0, 5.0), (10.0, 10.0))
        self.assertEqual(crossings({"outline": outline, "hole": hole}), [])
        self.assertEqual(crossings({"hole": hole, "wall": wall}), [])
        self.assertEqual(crossings({"through": through, "ending": ending}), [])

    def test_illegal_contacts(self):
        base = line((0.0, 0.0), (5.0, 0.0), (10.0, 0.0))
        cases = {
            "crossing": line((5.0, -2.0), (5.0, 2.0)),
            "crossing through a shared vertex": line((5.0, -2.0), (5.0, 0.0), (5.0, 2.0)),
            "touching inside a segment": line((2.5, 0.0), (2.5, 3.0)),
            "running along from a shared end": line((0.0, 0.0), (3.0, 0.0)),
        }
        for name, piece in cases.items():
            with self.subTest(name):
                self.assertTrue(crossings({"base": base, "other": piece}))
        with self.subTest("a bow tie"):
            self.assertTrue(crossings({"bow": line((0.0, 0.0), (4.0, 4.0), (4.0, 0.0),
                                                   (0.0, 4.0), (0.0, 0.0))}))
        with self.subTest("doubling back"):
            self.assertTrue(crossings({"back": line((0.0, 0.0), (4.0, 0.0), (2.0, 0.0))}))


class RepairTests(unittest.TestCase):
    def test_no_traced_path_crosses_itself(self):
        repaired = 0
        for seed in range(12):
            with self.subTest(seed=seed):
                _, track, _ = track_for(blobs_with_a_stripe(seed))
                components, fits, audit = shared_boundary_components(track, [0, 1, 2], SETTINGS)
                repaired += audit["crossing_passes"] > 0
                self.assertEqual(crossings({key: (fit.start, fit.primitives)
                                            for key, fit in fits.items()}), [])
                for colour, rows in components.items():
                    self.assertEqual(sampled_contacts([row["d"] for row in rows]), 0, colour)
        # The fixtures must exercise the repair, not just pass through it.
        self.assertGreaterEqual(repaired, 10)

    def test_repair_leaves_every_other_chain_as_fitted(self):
        _, track, _ = track_for(blobs_with_a_stripe(4))
        _, fits, audit = shared_boundary_components(track, [0, 1, 2], SETTINGS)
        repaired = set(audit["crossing_recovery"])
        self.assertTrue(repaired)
        fitter = SharedChainFitter()
        for chain in track.chains:
            fitted = fitter.fit(chain.points, SETTINGS,
                                [HardAnchor(a.point, a.kind) for a in chain.anchors])
            if chain.identity in repaired:
                self.assertTrue(audit["crossing_recovery"][chain.identity]["pins"])
            else:
                self.assertEqual(fits[chain.identity].primitives, fitted.primitives)

    def test_the_overlap_buffer_rings_do_not_cross(self):
        repaired = 0
        for seed in range(6):
            with self.subTest(seed=seed):
                geometry, _, _ = track_for(blobs_with_a_stripe(seed))
                for colour, shape in geometry.items():
                    buffered = shape.buffer(1.5, join_style=1).intersection(box(0, 0, 48, 48))
                    rows = geometry_components(buffered, SETTINGS)
                    repaired += sum(row["crossing_repair"]["repaired_rings"] for row in rows)
                    self.assertEqual(sampled_contacts([row["d"] for row in rows]), 0, colour)
        self.assertGreater(repaired, 0)

    def test_a_ring_touching_another_inside_a_segment_is_noded(self):
        # Valid: the hole touches the outline at (20, 0), inside the outline's
        # bottom segment; the polygon's dense rings must not read as crossing.
        polygon = Polygon([(0, 0), (40, 0), (40, 30), (0, 30)],
                          [[(20, 0), (25, 10), (15, 10)]])
        self.assertTrue(polygon.is_valid)
        rows = geometry_components(polygon, SETTINGS)
        self.assertEqual(sampled_contacts([row["d"] for row in rows]), 0)

    def test_the_oracle_sees_crossings(self):
        self.assertEqual(sampled_contacts(["M0 0 L10 0 L10 10 L0 10 Z",
                                           "M5 5 L15 5 L15 15 L5 15 Z"]), 2)
        self.assertEqual(sampled_contacts(["M0 0 L10 0 L10 10 L0 10 Z M10 5 L5 3 L5 7 Z"]), 1)
        self.assertEqual(sampled_contacts(["M0 0 L10 0 L10 5 L10 10 L0 10 Z M10 5 L5 3 L5 7 Z"]), 0)

    def test_pinned_runs_are_drawn_as_their_samples(self):
        samples = ((0.0, 0.0), (1.0, 0.4), (2.0, 0.0), (3.0, 0.4), (4.0, 0.0))
        fit = ChainFit((0.0, 0.0), (("C", (0.3, 0.3), (0.7, 0.5), (1.0, 0.4)),
                                    ("C", (1.3, 0.3), (1.7, 0.1), (2.0, 0.0)),
                                    ("C", (2.5, 0.5), (3.5, 0.5), (4.0, 0.0))),
                       FitMetrics(0, 3, 0, 5, 3))
        state = {"round": 2, "pins": [(1.0, 0.4), (2.0, 0.0)], "samples": 5}
        repaired = _repaired(fit, samples, state)
        self.assertEqual(repaired.primitives, (fit.primitives[0], ("L", (2.0, 0.0)),
                                               fit.primitives[2]))
        self.assertFalse(_repair_dense(state))
        state["pins"] = list(samples)
        self.assertTrue(_repair_dense(state))

    def test_node_samples_follow_a_closed_ring_from_a_moved_start(self):
        # The ring pinches (samples 0 and 7 lie close); its fit starts at a moved
        # node beside sample 6, so the indices run 6 up to 6 + 8.
        samples = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (2.0, 0.2), (0.5, 4.0),
                   (-3.0, 4.0), (-2.0, 1.0), (0.3, 0.1), (0.0, 0.0))
        fit = ChainFit((-2.1, 1.1), (("L", (0.3, 0.1)), ("L", (4.05, 0.0)), ("L", (4.0, 4.0)),
                                     ("L", (2.0, 0.2)), ("L", (-3.0, 4.0)), ("L", (-2.1, 1.1))),
                       FitMetrics(6, 0, 0, 9, 6))
        indices = _node_samples(fit, samples)
        # Only the samples (modulo the eight) and the steps between them matter.
        self.assertEqual([index % 8 for index in indices], [6, 7, 1, 2, 3, 5, 6])
        self.assertEqual([b - a for a, b in zip(indices, indices[1:])], [1, 2, 1, 1, 2, 1])

    def test_node_samples_follow_a_fit_that_hooks_back(self):
        # A square ring of eight samples whose fit reaches sample 3, hooks back
        # to sample 2, then runs on round the ring.
        samples = ((0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (4.0, 2.0), (4.0, 4.0),
                   (2.0, 4.0), (0.0, 4.0), (0.0, 2.0), (0.0, 0.0))
        fit = ChainFit((0.0, 0.0), (("L", (4.0, 2.0)), ("L", (4.0, 0.0)), ("L", (2.0, 4.0)),
                                    ("L", (0.0, 0.0))), FitMetrics(4, 0, 0, 9, 4))
        self.assertEqual(_node_samples(fit, samples), [0, 3, 2, 5, 8])
        # The hook's own primitive spans the samples it backs over, so a contact
        # on it pins one of them.
        state = {"round": 1, "pins": [], "samples": 8}
        self.assertTrue(_pin(state, fit, samples, (4.0, 1.2), (1,)))
        self.assertEqual(state["pins"], [(4.0, 2.0)])
        # An open chain steps back within its samples.
        chain = samples[:6]
        fit = ChainFit((0.0, 0.0), (("L", (4.0, 2.0)), ("L", (4.0, 0.0)), ("L", (2.0, 4.0))),
                       FitMetrics(3, 0, 0, 6, 3))
        self.assertEqual(_node_samples(fit, chain), [0, 3, 2, 5])

    def test_a_hook_tip_off_the_samples_maps_past_its_neighbours(self):
        samples = ((0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (4.0, 2.0), (4.0, 4.0),
                   (2.0, 4.0), (0.0, 4.0), (0.0, 2.0), (0.0, 0.0))

        def fit(start, points):
            return ChainFit(start, tuple(("L", point) for point in points),
                            FitMetrics(len(points), 0, 0, len(samples), len(points)))

        # An open chain reaching sample 1 hooks back to a moved tip beside
        # sample 0, past its neighbours (samples 1 and 2), and a contact at the
        # tip pins sample 0.
        hook = fit((0.0, 0.0), [(2.0, 0.0), (0.0, 0.1), (4.0, 0.0), (2.0, 4.0)])
        self.assertEqual(_node_samples(hook, samples[:6]), [0, 1, 0, 2, 5])
        state = {"round": 1, "pins": [], "samples": 6}
        _pin(state, hook, samples[:6], (0.2, 0.1), (1,))
        self.assertEqual(state["pins"], [(0.0, 0.0)])
        # A ring whose moved start beside sample 0 closes by hooking back from
        # sample 1 maps its start to sample 0, not to its neighbour.
        seam = fit((0.1, 0.1), [(4.0, 2.0), (0.0, 4.0), (0.0, 2.0), (2.0, 0.0), (0.1, 0.1)])
        self.assertEqual(_node_samples(seam, samples), [8, 11, 14, 15, 17, 16])
        # A ring with no node on a sample keeps to one lap.
        moved = fit((0.1, 0.1), [(4.1, 0.1), (4.1, 4.1), (0.1, 4.1), (0.1, 0.1)])
        self.assertEqual(_node_samples(moved, samples), [0, 2, 4, 6, 8])

    def test_a_long_forward_primitive_is_not_read_as_a_step_back(self):
        # Two primitives round an eight-sample ring, the first covering six of
        # its samples: its middle lies on them, so it runs forward.
        samples = ((0.0, 0.0), (2.0, 0.0), (4.0, 0.0), (4.0, 2.0), (4.0, 4.0),
                   (2.0, 4.0), (0.0, 4.0), (0.0, 2.0), (0.0, 0.0))
        fit = ChainFit((0.0, 0.0), (("C", (5.5, -1.5), (5.5, 5.5), (0.0, 4.0)),
                                    ("L", (0.0, 0.0))), FitMetrics(1, 1, 0, 9, 2))
        self.assertEqual(_node_samples(fit, samples), [0, 6, 8])

    def test_dense_samples_that_cross_are_reported(self):
        fits = {"a": ChainFit((0.0, 0.0), (("L", (10.0, 0.0)),), FitMetrics(1, 0, 0, 2, 1)),
                "b": ChainFit((5.0, -1.0), (("L", (5.0, 5.0)),), FitMetrics(1, 0, 0, 2, 1))}
        with self.assertRaisesRegex(RuntimeError, "dense boundary samples cross"):
            _repair_crossings(fits, {}, lambda keys: None, {}, lambda key: True)


if __name__ == "__main__":
    unittest.main()
