import random
import unittest

from shapely.geometry import Polygon, box

from vinylgen.curve_fit import FitMetrics, _rows_to_components


def _reference_nesting(rows: list[dict]) -> list[int | None]:
    """Exhaustive parent search: smallest strictly larger ring containing the
    representative point, lowest index among equal areas."""
    parents: list[int | None] = []
    for index, row in enumerate(rows):
        point = row["polygon"].representative_point()
        containers = [candidate for candidate, other in enumerate(rows)
                      if candidate != index
                      and other["polygon"].area > row["polygon"].area
                      and other["polygon"].contains(point)]
        parents.append(min(containers, key=lambda candidate: rows[candidate]["polygon"].area)
                       if containers else None)
    return parents


def _reference_components(rows: list[dict]) -> list[dict]:
    parents = _reference_nesting(rows)

    def depth(index: int) -> int:
        value = 0
        while parents[index] is not None:
            index = int(parents[index])
            value += 1
        return value

    components = []
    for index, row in enumerate(rows):
        if depth(index) % 2:
            continue
        holes = [candidate for candidate, parent in enumerate(parents)
                 if parent == index and depth(candidate) % 2]
        members = [row, *(rows[candidate] for candidate in holes)]
        aggregate = FitMetrics(
            sum(item["fit_metrics"].lines for item in members),
            sum(item["fit_metrics"].cubics for item in members),
            sum(item["fit_metrics"].hard_anchors for item in members),
            sum(item["fit_metrics"].source_samples for item in members),
            sum(item["fit_metrics"].final_nodes for item in members))
        components.append({"d": " ".join(item["d"] for item in members),
                           "nodes": aggregate.final_nodes, "holes": len(holes),
                           "fit_metrics": aggregate.as_dict(),
                           "area": float(row["polygon"].area)})
    components.sort(key=lambda item: -item["area"])
    return components


def _row(polygon: Polygon, index: int) -> dict:
    return {"d": f"P{index}", "polygon": polygon,
            "fit_metrics": FitMetrics(index % 3, index % 2, 1, 4 + index, 2 + index)}


def _random_rows(rng: random.Random) -> list[dict]:
    rows = []
    count = rng.randint(0, 24)
    for index in range(count):
        kind = rng.random()
        if kind < 0.5 and rows:
            # Nest inside an existing ring, sometimes with an identical-area sibling.
            parent = rng.choice(rows)["polygon"]
            x0, y0, x1, y1 = parent.bounds
            size = (x1 - x0) * rng.uniform(0.2, 0.6)
            if size < 0.5:
                continue
            x = rng.uniform(x0, max(x0, x1 - size))
            y = rng.uniform(y0, max(y0, y1 - size))
            rows.append(_row(box(x, y, x + size, y + size), index))
        elif kind < 0.65 and rows:
            # Exact duplicate area at another position: exercises the index tie-break.
            other = rng.choice(rows)["polygon"]
            x0, y0, x1, y1 = other.bounds
            shift = rng.uniform(-2.0, 2.0)
            rows.append(_row(box(x0 + shift, y0 + shift, x1 + shift, y1 + shift), index))
        else:
            size = rng.uniform(1.0, 40.0)
            x, y = rng.uniform(0, 100), rng.uniform(0, 100)
            rows.append(_row(box(x, y, x + size, y + size), index))
    return rows


class RingNestingTests(unittest.TestCase):
    def test_matches_exhaustive_reference_on_random_ring_sets(self) -> None:
        rng = random.Random(20260905)
        nontrivial = 0
        for _ in range(400):
            rows = _random_rows(rng)
            expected = _reference_components(rows)
            self.assertEqual(_rows_to_components(rows), expected)
            nontrivial += any(item["holes"] for item in expected)
        self.assertGreater(nontrivial, 50)

    def test_empty_and_single_ring(self) -> None:
        self.assertEqual(_rows_to_components([]), [])
        rows = [_row(box(0, 0, 4, 4), 0)]
        self.assertEqual(_rows_to_components(rows), _reference_components(rows))

    def test_three_level_nesting_and_equal_area_tie(self) -> None:
        rows = [_row(box(0, 0, 10, 10), 0), _row(box(2, 2, 8, 8), 1),
                _row(box(3, 3, 5, 5), 2), _row(box(2, 2, 8, 8), 3)]
        result = _rows_to_components(rows)
        self.assertEqual(result, _reference_components(rows))
        self.assertEqual([item["holes"] for item in result], [2, 0])


if __name__ == "__main__":
    unittest.main()
