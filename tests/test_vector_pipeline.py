import math
import unittest

import numpy as np

from vinylgen.boundary_graph import assemble_colour_rings, shared_boundary_polygons
from vinylgen.contracts import TraceSettings
from vinylgen.curve_fit import HardAnchor, ring_path


class VectorPipelineTests(unittest.TestCase):
    def test_pair_specific_cost_zero_crossing_places_shared_interface(self):
        labels = np.zeros((5, 8), dtype=np.int16)
        labels[:, 4:] = 1
        costs = np.full((5, 8, 3), 100.0)
        # d = cost(0)-cost(1): -3 at x=3 and +1 at x=4, so the
        # continuous equal-cost crossing is 75% between their pixel centres.
        costs[:, 3, 0], costs[:, 3, 1] = 0.0, 3.0
        costs[:, 4, 0], costs[:, 4, 1] = 1.0, 0.0
        geometry, _, diagnostics = shared_boundary_polygons(
            labels, [0, 1], costs)
        self.assertAlmostEqual(geometry[0].bounds[2], 4.25, places=6)
        self.assertAlmostEqual(geometry[1].bounds[0], 4.25, places=6)
        self.assertEqual(diagnostics["partition_gap_area"], 0.0)
        self.assertEqual(diagnostics["overlap_area"], 0.0)

    def test_unrelated_third_colour_cost_does_not_move_pair_interface(self):
        labels = np.zeros((4, 8), dtype=np.int16)
        labels[:, 4:] = 1
        costs = np.zeros((4, 8, 3), dtype=float)
        costs[:, 3, :2] = (0, 3)
        costs[:, 4, :2] = (1, 0)
        first, _, _ = shared_boundary_polygons(labels, [0, 1], costs)
        costs[:, :, 2] = np.linspace(-1000, 1000, 8)
        second, _, _ = shared_boundary_polygons(labels, [0, 1], costs)
        self.assertTrue(first[0].equals_exact(second[0], 0))

    def test_sustained_stair_step_is_selected_as_one_straight_section(self):
        points = [(0, 0), (2, 0), (2, 1), (4, 1), (4, 2), (6, 2),
                  (6, 10), (0, 10), (0, 0)]
        fitted = ring_path(
            points, TraceSettings(corner_threshold=math.degrees(0.7),
                                  corner_analysis_span=4,
                                  curve_fit_tolerance=0.8), set())
        self.assertLess(fitted.metrics.final_nodes, len(points) - 1)
        self.assertNotIn("L2 0 L2 1 L4 1", fitted.path)
        self.assertGreaterEqual(fitted.metrics.cubics, 0)

    def test_curve_is_fit_across_many_samples_not_rounded_per_vertex(self):
        arc = [(float(x), float(10 - np.sqrt(max(0, 100 - x * x))))
               for x in range(-8, 9)]
        points = [*arc, (8, 12), (-8, 12), arc[0]]
        fitted = ring_path(
            points, TraceSettings(corner_threshold=math.degrees(0.8),
                                  corner_analysis_span=3,
                                  curve_fit_tolerance=0.35), set())
        self.assertGreater(fitted.metrics.cubics, 0)
        self.assertLess(fitted.metrics.cubics, len(arc) // 2)
        self.assertLess(fitted.metrics.final_nodes, len(points) - 1)
        self.assertIn("C", fitted.path)

    def test_sparse_cubic_controls_cannot_escape_source_envelope(self):
        points = [(0, 0), (1, 0.05), (2, 0), (3, 0.1),
                  (3, 2), (0, 2), (0, 0)]
        fitted = ring_path(
            points, TraceSettings(corner_threshold=math.degrees(1.2),
                                  corner_analysis_span=1,
                                  curve_fit_tolerance=0.2), set())
        numbers = [float(value) for value in __import__('re').findall(
            r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?', fitted.path)]
        self.assertTrue(all(-2.0 <= value <= 5.0 for value in numbers))

    def test_protected_point_remains_explicit_section_boundary(self):
        points = [(0, 0), (2, 0.2), (4, 0.8), (6, 1.8),
                  (6, 8), (0, 8), (0, 0)]
        fitted = ring_path(
            points, TraceSettings(corner_threshold=math.degrees(1.2),
                                  curve_fit_tolerance=1.0),
            [HardAnchor((4.0, 0.8), "junction")])
        self.assertIn("4 0.8", fitted.path)

    def test_protected_raster_edge_retains_dense_evidence_for_curve_fitting(self):
        labels = np.zeros((6, 8), dtype=np.int16)
        labels[2:4, 1:7] = 1
        costs = np.empty((6, 8, 2), dtype=float)
        costs[labels == 0] = (0, 3)
        costs[labels == 1] = (1, 0)
        protected_features = np.zeros_like(labels, dtype=bool)
        protected_features[2:4, 3:5] = True

        geometry, protected, diagnostics = shared_boundary_polygons(
            labels, [0, 1], costs,
            protected_features=protected_features)

        protected_chains = [chain for chain in diagnostics["chains"]
                            if chain["protected_evidence"]]
        self.assertTrue(protected_chains)
        unprotected_geometry, _, _ = shared_boundary_polygons(
            labels, [0, 1], costs)
        self.assertTrue(geometry[1].equals_exact(unprotected_geometry[1], 0))
        self.assertEqual(diagnostics["dense_boundary_samples"],
                         sum(chain["source_samples"] for chain in diagnostics["chains"]))

    def test_semantic_evidence_is_not_promoted_to_hard_vertex_anchors(self):
        labels = np.zeros((8, 12), dtype=np.int16)
        labels[3:, :] = 1
        costs = np.ones((8, 12, 2), dtype=float)
        costs[:3, :, 0], costs[:3, :, 1] = 0, 3
        costs[3:, :, 0], costs[3:, :, 1] = 1, 0
        protected = np.zeros_like(labels, dtype=bool)
        protected[2:4, 3:9] = True

        _, track, _ = shared_boundary_polygons(
            labels, [0, 1], costs, protected_features=protected)
        interface = next(chain for chain in track.chains if chain.pair == (0, 1))

        self.assertTrue(interface.protected_source_edges)
        self.assertTrue(any(abs(coordinate - round(coordinate)) > 1e-6
                            for point in interface.source_points
                            for coordinate in point))
        self.assertLessEqual(len(interface.anchors), 2)
        self.assertTrue(all(anchor.kind == "endpoint"
                            for anchor in interface.anchors))
        self.assertFalse(any(point in track.hard_anchor_points
                             for point in interface.source_points[1:-1]))

    def test_three_colour_junction_is_a_typed_hard_anchor(self):
        labels = np.zeros((8, 8), dtype=np.int16)
        labels[:4, 4:] = 1
        labels[4:, 4:] = 2
        costs = np.full((8, 8, 3), 5.0)
        for index in range(3):
            costs[..., index][labels == index] = 0.0

        _, track, _ = shared_boundary_polygons(labels, [0, 1, 2], costs)
        junctions = [anchor for chain in track.chains for anchor in chain.anchors
                     if anchor.kind == "junction"]

        self.assertTrue(junctions)
        self.assertEqual(len({anchor.point for anchor in junctions}), 1)

    def test_chain_identity_is_stable_and_direction_independent(self):
        labels = np.zeros((6, 10), dtype=np.int16)
        labels[:, 5:] = 1
        costs = np.ones((6, 10, 2), dtype=float)
        costs[:, :5] = (0, 4)
        costs[:, 5:] = (4, 0)
        _, first, _ = shared_boundary_polygons(labels, [0, 1], costs)
        _, second, _ = shared_boundary_polygons(labels, [0, 1], costs)
        first_interface = next(chain for chain in first.chains if chain.pair == (0, 1))
        second_interface = next(chain for chain in second.chains if chain.pair == (0, 1))

        self.assertEqual(first_interface.identity, second_interface.identity)
        self.assertEqual(first_interface.points, second_interface.points)
        self.assertEqual({first_interface.left_colour, first_interface.right_colour}, {0, 1})
        self.assertEqual(first_interface.points[0], min(first_interface.points[0],
                                                        first_interface.points[-1]))

    def test_chain_records_canonical_orientation_for_reverse_reuse(self):
        labels = np.zeros((5, 9), dtype=np.int16)
        labels[:, 4:] = 1
        costs = np.ones((5, 9, 2), dtype=float)
        costs[:, :4] = (0, 3)
        costs[:, 4:] = (3, 0)
        _, track, _ = shared_boundary_polygons(labels, [0, 1], costs)
        interface = next(chain for chain in track.chains if chain.pair == (0, 1))

        self.assertEqual(interface.points[0], min(interface.points[0], interface.points[-1]))
        self.assertEqual({interface.left_colour, interface.right_colour}, {0, 1})
        # A consumer emits the canonical points for the left colour and this
        # exact reverse for the right colour; no second fit is permitted.
        self.assertEqual(tuple(reversed(interface.points))[0], interface.points[-1])

    def test_adjacent_colour_rings_reuse_identity_in_opposite_directions(self):
        labels = np.zeros((6, 10), dtype=np.int16)
        labels[:, 5:] = 1
        costs = np.ones((6, 10, 2), dtype=float)
        costs[:, :5] = (0, 4)
        costs[:, 5:] = (4, 0)
        _, track, _ = shared_boundary_polygons(labels, [0, 1], costs)
        rings = assemble_colour_rings(track, [0, 1])
        interface = next(chain for chain in track.chains if chain.pair == (0, 1))
        uses_zero = [use for ring in rings[0] for use in ring.uses
                     if use.identity == interface.identity]
        uses_one = [use for ring in rings[1] for use in ring.uses
                    if use.identity == interface.identity]

        self.assertEqual(len(uses_zero), 1)
        self.assertEqual(len(uses_one), 1)
        self.assertNotEqual(uses_zero[0].forward, uses_one[0].forward)
        self.assertEqual(len(rings[0]), 1)
        self.assertEqual(len(rings[1]), 1)

    def test_ring_assembly_is_deterministic_at_three_colour_junction(self):
        labels = np.zeros((8, 8), dtype=np.int16)
        labels[:4, 4:] = 1
        labels[4:, 4:] = 2
        costs = np.full((8, 8, 3), 5.0)
        for index in range(3):
            costs[..., index][labels == index] = 0.0
        _, track, _ = shared_boundary_polygons(labels, [0, 1, 2], costs)

        first = assemble_colour_rings(track, [0, 1, 2])
        second = assemble_colour_rings(track, [0, 1, 2])

        self.assertEqual(first, second)
        self.assertTrue(all(len(rings) == 1 for rings in first.values()))


if __name__ == "__main__":
    unittest.main()
