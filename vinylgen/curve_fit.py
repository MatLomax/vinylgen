from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Iterable, Literal

import numpy as np
from shapely.geometry import Polygon

from vinylgen.contracts import TraceSettings
from vinylgen.palette import Progress, _noop

Point = tuple[float, float]
Primitive = tuple
AnchorKind = Literal["junction", "endpoint"]


@dataclass(frozen=True)
class HardAnchor:
    point: Point
    kind: AnchorKind

    def __post_init__(self) -> None:
        if self.kind not in ("junction", "endpoint"):
            raise ValueError(f"unsupported hard-anchor kind {self.kind!r}")


@dataclass(frozen=True)
class FitMetrics:
    lines: int
    cubics: int
    hard_anchors: int
    source_samples: int
    final_nodes: int

    def as_dict(self) -> dict[str, int | float]:
        reduction = self.source_samples - self.final_nodes
        return {"lines": self.lines, "cubics": self.cubics,
                "hard_anchors": self.hard_anchors,
                "source_samples": self.source_samples,
                "final_nodes": self.final_nodes,
                "node_reduction": reduction,
                "node_reduction_ratio": (reduction / self.source_samples
                                         if self.source_samples else 0.0)}


@dataclass(frozen=True)
class RunDiagnostic:
    classification: Literal["straight", "curve", "corner"]
    source_start: int
    source_end: int
    max_error: float
    primitives: int
    recovery_attempts: int = 0
    dense_fallback: bool = False
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "max_error": self.max_error,
            "primitives": self.primitives,
            "recovery_attempts": self.recovery_attempts,
            "dense_fallback": self.dense_fallback,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True)
class ChainFit:
    start: Point
    primitives: tuple[Primitive, ...]
    metrics: FitMetrics
    diagnostics: tuple[RunDiagnostic, ...] = ()

    def reversed(self) -> "ChainFit":
        cursor = self.start
        sections: list[tuple[Point, Primitive]] = []
        for primitive in self.primitives:
            sections.append((cursor, primitive))
            cursor = primitive[-1]
        reversed_primitives: list[Primitive] = []
        for section_start, primitive in reversed(sections):
            if primitive[0] == "L":
                reversed_primitives.append(("L", section_start))
            else:
                reversed_primitives.append(("C", primitive[2], primitive[1], section_start))
        return ChainFit(cursor, tuple(reversed_primitives), self.metrics,
                        tuple(reversed(self.diagnostics)))


@dataclass(frozen=True)
class RingFit:
    path: str
    metrics: FitMetrics


def _distance_to_line(points: np.ndarray) -> tuple[float, int]:
    vector = points[-1] - points[0]
    length = float(np.linalg.norm(vector))
    distances = (np.linalg.norm(points - points[0], axis=1) if length < 1e-12 else
                 np.abs(np.cross(vector, points - points[0])) / length)
    index = int(np.argmax(distances))
    return float(distances[index]), index


@dataclass(frozen=True)
class _LineEvidence:
    origin: np.ndarray
    direction: np.ndarray
    maximum_error: float
    length: float


def _robust_line(points: np.ndarray) -> _LineEvidence:
    """Total-least-squares line evidence, insensitive to axis stair steps."""
    origin = points.mean(axis=0)
    centred = points - origin
    if len(points) < 2 or not np.any(centred):
        return _LineEvidence(origin, np.array((1.0, 0.0)), 0.0, 0.0)
    _, _, axes = np.linalg.svd(centred, full_matrices=False)
    direction = axes[0]
    if np.dot(direction, points[-1] - points[0]) < 0:
        direction = -direction
    projections = centred @ direction
    errors = np.abs(np.cross(direction, centred))
    return _LineEvidence(origin, direction, float(errors.max()),
                         float(projections.max() - projections.min()))


def _line_intersection(first: _LineEvidence, second: _LineEvidence) -> np.ndarray | None:
    cross = float(np.cross(first.direction, second.direction))
    if abs(cross) < 1e-6:
        return None
    delta = second.origin - first.origin
    distance = float(np.cross(delta, second.direction) / cross)
    return first.origin + distance * first.direction


def _side_window(points: np.ndarray, index: int, step: int, span: float) -> np.ndarray:
    """Samples on one side of a prospective corner by physical arc length."""
    selected = [points[index]]
    travelled = 0.0
    cursor = index
    while 0 <= cursor + step < len(points) and travelled < span:
        following = cursor + step
        travelled += float(np.linalg.norm(points[following] - points[cursor]))
        selected.append(points[following])
        cursor = following
    if step < 0:
        selected.reverse()
    return np.asarray(selected)


def _geometric_corners(points: np.ndarray, settings: TraceSettings) -> dict[int, Point]:
    """Find corners supported by coherent straight runs at two scales.

    Individual horizontal/vertical turns in a diagonal pixel staircase fail
    the sustained-run tests; a building corner has stable line evidence on
    both sides and survives the larger-scale confirmation.
    """
    if len(points) < 5:
        return {}
    deviation = max(1e-4, float(settings.straight_line_max_deviation))
    minimum = max(1e-4, float(settings.minimum_straight_length))
    span = max(minimum, float(settings.corner_analysis_span))
    threshold = math.radians(float(settings.corner_threshold))
    candidates: list[tuple[float, int, Point]] = []
    for index in range(1, len(points) - 1):
        evidence = []
        valid = True
        for scale in (span, span * 1.75):
            before = _robust_line(_side_window(points, index, -1, scale))
            after = _robust_line(_side_window(points, index, 1, scale))
            if (before.length < minimum or after.length < minimum or
                    before.maximum_error > deviation or after.maximum_error > deviation):
                valid = False
                break
            turn = math.acos(float(np.clip(np.dot(before.direction, after.direction), -1, 1)))
            if turn < threshold:
                valid = False
                break
            evidence.append((turn, before, after))
        if not valid:
            continue
        turn, before, after = evidence[0]
        intersection = _line_intersection(before, after)
        if intersection is None:
            continue
        maximum_move = (float(settings.max_deviation)
                        if settings.max_deviation is not None else deviation * 2.0)
        if float(np.linalg.norm(intersection - points[index])) > maximum_move:
            continue
        candidates.append((turn, index, tuple(map(float, intersection))))

    # One physical corner creates several neighbouring candidate samples. Keep
    # the strongest and suppress candidates within half the analysis span.
    chosen: dict[int, Point] = {}
    for _, index, intersection in sorted(candidates, reverse=True):
        if all(abs(index - other) >= max(2, int(span / 2)) for other in chosen):
            chosen[index] = intersection
    return chosen


def _parameters(points: np.ndarray) -> np.ndarray:
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    cumulative = np.r_[0.0, np.cumsum(lengths)]
    return cumulative / cumulative[-1] if cumulative[-1] else cumulative


def _cubic(points: np.ndarray, tolerance: float):
    t = _parameters(points)
    if len(points) == 3:
        # Two cubic controls are underdetermined by one interior sample.  Fit
        # the unique quadratic through that sample at its chord parameter and
        # degree-elevate it to a cubic.  This is exact, stable, and prevents a
        # shallow three-sample turn being recursively split into pixel lines.
        middle_t = float(t[1])
        denominator = 2.0 * (1.0 - middle_t) * middle_t
        quadratic_control = (
            points[1]
            - (1.0 - middle_t) ** 2 * points[0]
            - middle_t**2 * points[-1]
        ) / max(denominator, 1e-12)
        c1 = points[0] + (2.0 / 3.0) * (quadratic_control - points[0])
        c2 = points[-1] + (2.0 / 3.0) * (quadratic_control - points[-1])
    else:
        omt = 1 - t
        b0, b1, b2, b3 = omt**3, 3 * omt**2 * t, 3 * omt * t**2, t**3
        rhs = points - b0[:, None] * points[0] - b3[:, None] * points[-1]
        c1, c2 = np.linalg.lstsq(
            np.column_stack((b1, b2)), rhs, rcond=None
        )[0]
    span = float(np.ptp(points, axis=0).max())
    padding = max(tolerance, min(2.0, span * .25))
    lower, upper = points.min(axis=0)-padding, points.max(axis=0)+padding
    c1, c2 = np.clip(c1, lower, upper), np.clip(c2, lower, upper)
    omt = 1 - t
    b0, b1, b2, b3 = omt**3, 3 * omt**2 * t, 3 * omt * t**2, t**3
    fitted = (
        b0[:, None] * points[0]
        + b1[:, None] * c1
        + b2[:, None] * c2
        + b3[:, None] * points[-1]
    )
    return c1, c2, fitted


def _fit(points: np.ndarray, tolerance: float) -> list[Primitive]:
    if len(points) <= 2:
        return [("L", tuple(points[-1]))]
    line_error, line_split = _distance_to_line(points)
    if line_error <= tolerance:
        return [("L", tuple(points[-1]))]
    c1, c2, fitted = _cubic(points, tolerance)
    errors = np.linalg.norm(fitted - points, axis=1)
    split = int(np.argmax(errors))
    if float(errors[split]) <= tolerance:
        return [("C", tuple(c1), tuple(c2), tuple(points[-1]))]
    if not 0 < split < len(points)-1:
        split = line_split
    if not 0 < split < len(points)-1:
        split = len(points)//2
    return _fit(points[:split+1], tolerance) + _fit(points[split:], tolerance)


def _fit_classified(points: np.ndarray, settings: TraceSettings,
                    source_start: int, source_end: int,
                    ) -> tuple[list[Primitive], RunDiagnostic]:
    """Choose the simplest valid model for a classified geometric run."""
    if len(points) <= 2:
        primitives = [("L", tuple(points[-1]))]
        return primitives, RunDiagnostic("straight", source_start, source_end, 0.0, 1)
    line = _robust_line(points)
    if (line.length >= float(settings.minimum_straight_length) and
            line.maximum_error <= float(settings.straight_line_max_deviation)):
        primitives = [("L", tuple(points[-1]))]
        return primitives, RunDiagnostic("straight", source_start, source_end,
                                         line.maximum_error, 1)
    tolerance = max(1e-4, float(settings.curve_fit_tolerance))
    if settings.max_deviation is not None:
        tolerance = min(tolerance, max(1e-4, float(settings.max_deviation)))
    primitives = _fit(points, tolerance)
    # The recursive fitter guarantees this bound at source parameters; retain
    # the observed line residual as useful classification evidence too.
    return primitives, RunDiagnostic("curve", source_start, source_end,
                                     min(line.maximum_error, tolerance), len(primitives))


class SharedChainFitter:
    """Canonical cache ensures both users of a shared chain get one fit."""

    def __init__(self) -> None:
        self._cache: dict[tuple, ChainFit] = {}

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def fit(self, coords: Iterable[Point], settings: TraceSettings,
            hard_anchors: Iterable[HardAnchor] = (),
            fit_constraints: Iterable[Point] = ()) -> ChainFit:
        points = tuple(tuple(map(float, p)) for p in coords)
        if len(points) < 2:
            raise ValueError("a shared chain needs at least two samples")
        anchors = tuple(hard_anchors)
        constraints = tuple(tuple(map(float, point)) for point in fit_constraints)
        anchor_key = tuple(sorted((a.point, a.kind) for a in anchors
                                  if a.point in points))
        forward = points <= tuple(reversed(points))
        canonical = points if forward else tuple(reversed(points))
        constraint_key = tuple(sorted(point for point in constraints if point in canonical))
        key = (canonical, anchor_key, constraint_key, bool(settings.curve_optimization),
               float(settings.curve_fit_tolerance),
               float(settings.straight_line_max_deviation),
               float(settings.minimum_straight_length),
               float(settings.corner_analysis_span), float(settings.corner_threshold),
               settings.max_deviation)
        if key not in self._cache:
            anchor_points = {anchor.point for anchor in anchors}
            split_points = anchor_points | set(constraint_key)
            array = np.asarray(canonical, dtype=float)
            corners = _geometric_corners(array, settings)
            split_indices = [0, *[index for index, point in enumerate(canonical[1:-1], 1)
                                  if point in split_points or index in corners], len(canonical) - 1]
            split_indices = sorted(set(split_indices))
            model_points = array.copy()
            for index, point in corners.items():
                if canonical[index] not in anchor_points:
                    model_points[index] = point
            pieces: list[Primitive] = []
            diagnostics: list[RunDiagnostic] = [
                RunDiagnostic("corner", index, index,
                              float(np.linalg.norm(model_points[index] - array[index])), 0)
                for index in sorted(corners)
                if canonical[index] not in anchor_points
            ]
            for start, end in zip(split_indices, split_indices[1:]):
                section = model_points[start:end + 1]
                if not settings.curve_optimization:
                    fitted_piece = [("L", tuple(section[-1]))]
                    diagnostic = RunDiagnostic("straight", start, end, 0.0, 1)
                else:
                    fitted_piece, diagnostic = _fit_classified(section, settings, start, end)
                pieces.extend(fitted_piece)
                diagnostics.append(diagnostic)
            primitives = tuple(pieces)
            lines = sum(p[0] == "L" for p in primitives)
            cubics = len(primitives) - lines
            used = len({a.point for a in anchors}.intersection(canonical))
            self._cache[key] = ChainFit(canonical[0], primitives, FitMetrics(
                lines, cubics, used, len(canonical), len(primitives)), tuple(diagnostics))
        fitted = self._cache[key]
        return fitted if forward else fitted.reversed()


def _fmt(point: Point) -> str:
    return f"{point[0]:g} {point[1]:g}"


def ring_path(coords: Iterable[Point], settings: TraceSettings,
              hard_anchors: Iterable[HardAnchor] = (),
              fitter: SharedChainFitter | None = None) -> RingFit:
    raw = [tuple(map(float, p)) for p in coords]
    points = raw[:-1] if raw and raw[0] == raw[-1] else raw
    anchors = tuple(hard_anchors)
    anchor_points = frozenset(a.point for a in anchors)
    fitter = fitter or SharedChainFitter()
    # Closed rings use the same modeller as shared open chains. The duplicated
    # seam is only a topological endpoint; it is not a protected or inferred
    # geometry anchor.
    fitted = fitter.fit([*points, points[0]], settings, anchors)
    commands = [f"M{_fmt(fitted.start)}"]
    primitives = list(fitted.primitives)
    for primitive in primitives:
        if primitive[0] == "L": commands.append(f"L{_fmt(primitive[1])}")
        else: commands.append(f"C{_fmt(primitive[1])} {_fmt(primitive[2])} {_fmt(primitive[3])}")
    commands.append("Z")
    lines = sum(p[0] == "L" for p in primitives)
    metrics = FitMetrics(lines, len(primitives)-lines,
                         len(anchor_points.intersection(points)), len(points), len(primitives))
    return RingFit(" ".join(commands), metrics)


def geometry_components(geometry, settings: TraceSettings,
                        hard_anchors: Iterable[HardAnchor] = (),
                        fitter: SharedChainFitter | None = None) -> list[dict]:
    polygons = [] if geometry.is_empty else ([geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms))
    polygons.sort(key=lambda polygon: (-polygon.area, polygon.bounds))
    fitter = fitter or SharedChainFitter()
    result = []
    for polygon in polygons:
        fitted = [ring_path(r.coords, settings, hard_anchors, fitter)
                  for r in [polygon.exterior, *polygon.interiors]]
        aggregate = FitMetrics(
            sum(f.metrics.lines for f in fitted), sum(f.metrics.cubics for f in fitted),
            sum(f.metrics.hard_anchors for f in fitted), sum(f.metrics.source_samples for f in fitted),
            sum(f.metrics.final_nodes for f in fitted))
        result.append({"d": " ".join(f.path for f in fitted), "nodes": aggregate.final_nodes,
                       "holes": len(polygon.interiors), "fit_metrics": aggregate.as_dict(),
                       "area": float(polygon.area)})
    return result


def _ring_row(chains: dict, fits: dict[str, "ChainFit"], ring_uses) -> dict:
    """Assemble one closed ring of oriented chain uses into a path row.

    Each use contributes the canonical chain's once-fitted primitives, reversed when
    the use runs backwards, so a boundary shared by two rings is byte-identical in both.
    """
    commands: list[str] = []
    raw_points: list[Point] = []
    metrics: list[FitMetrics] = []
    for use in ring_uses:
        chain = chains[use.identity]
        fit = fits[use.identity] if use.forward else fits[use.identity].reversed()
        oriented = chain.points if use.forward else tuple(reversed(chain.points))
        if not commands:
            commands.append(f"M{_fmt(fit.start)}")
            raw_points.extend(oriented)
        else:
            if raw_points[-1] != oriented[0]:
                raise RuntimeError(
                    f"assembled chain {use.identity} does not join its predecessor")
            raw_points.extend(oriented[1:])
        for primitive in fit.primitives:
            if primitive[0] == "L":
                commands.append(f"L{_fmt(primitive[1])}")
            else:
                commands.append(
                    f"C{_fmt(primitive[1])} {_fmt(primitive[2])} {_fmt(primitive[3])}")
        metrics.append(fit.metrics)
    commands.append("Z")
    if raw_points[0] != raw_points[-1]:
        raw_points.append(raw_points[0])
    polygon = Polygon(raw_points)
    if polygon.is_empty or polygon.area <= 0:
        raise RuntimeError("assembled boundary ring is empty")
    aggregate = FitMetrics(
        sum(item.lines for item in metrics), sum(item.cubics for item in metrics),
        sum(item.hard_anchors for item in metrics), sum(item.source_samples for item in metrics),
        sum(item.final_nodes for item in metrics))
    return {"d": " ".join(commands), "polygon": polygon, "fit_metrics": aggregate}


def _rows_to_components(rows: list[dict]) -> list[dict]:
    """Nest rings by even-odd containment into filled components with their holes."""
    parents: list[int | None] = []
    for index, row in enumerate(rows):
        point = row["polygon"].representative_point()
        containers = [candidate for candidate, other in enumerate(rows)
                      if candidate != index
                      and other["polygon"].area > row["polygon"].area
                      and other["polygon"].contains(point)]
        parents.append(min(containers, key=lambda candidate: rows[candidate]["polygon"].area)
                       if containers else None)

    def depth(index: int) -> int:
        value, seen = 0, set()
        while parents[index] is not None:
            if index in seen:
                raise RuntimeError("cyclic ring containment")
            seen.add(index)
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
            sum(item["fit_metrics"].final_nodes for item in members),
        )
        components.append({"d": " ".join(item["d"] for item in members),
                           "nodes": aggregate.final_nodes,
                           "holes": len(holes),
                           "fit_metrics": aggregate.as_dict(),
                           "area": float(row["polygon"].area)})
    components.sort(key=lambda item: -item["area"])
    return components


def _ring_is_ccw(points) -> bool:
    return sum((b[0] - a[0]) * (b[1] + a[1]) for a, b in zip(points, points[1:])) < 0


def _fit_vertices(fit: "ChainFit") -> list:
    return [fit.start, *(primitive[-1] for primitive in fit.primitives)]


def _region_ring_path(chains: dict, fits: dict[str, "ChainFit"],
                      segment_owner: dict, ring) -> dict:
    """Emit one union boundary ring, reusing each chain's once-fitted curve.

    The ring already comes from the geometry union, so its vertices are the canonical
    chain samples. Each maximal run of edges owned by one chain replays that chain's
    cached fit (reversed when the run runs backwards); the rare edge that the union
    renoded away from any chain falls back to its own straight segment, whose endpoints
    are shared by every neighbour, so registration holds either way.
    """
    coords = list(ring.coords)
    edges = list(zip(coords, coords[1:]))
    owners = [segment_owner.get(frozenset((a, b))) for a, b in edges]
    runs: list[list] = []
    for (a, b), owner in zip(edges, owners):
        if runs and runs[-1][0] == owner and owner is not None:
            runs[-1][1].append(b)
        else:
            runs.append([owner, [a, b]])
    # A chain straddling the ring's arbitrary start point splits across the seam; rejoin
    # it so each chain is one run and its run starts on a real junction.
    if len(runs) > 1 and runs[0][0] is not None and runs[0][0] == runs[-1][0]:
        tail = runs.pop()
        runs[0][1] = tail[1] + runs[0][1][1:]
    commands: list[str] = []
    lines = cubics = 0
    for owner, points in runs:
        chain = chains.get(owner) if owner is not None else None
        oriented_fit = None
        if chain is not None:
            if chain.closed:
                # A closed interface is a whole ring on its own; the union ring starts at an
                # arbitrary vertex, so orient by winding rather than a shared endpoint.
                forward = fits[owner]
                oriented_fit = forward if _ring_is_ccw(_fit_vertices(forward)) == _ring_is_ccw(points) \
                    else forward.reversed()
            elif points[0] == chain.points[0]:
                oriented_fit = fits[owner]
            elif points[0] == chain.points[-1]:
                oriented_fit = fits[owner].reversed()
        if oriented_fit is not None:
            if not commands:
                commands.append(f"M{_fmt(oriented_fit.start)}")
            for primitive in oriented_fit.primitives:
                if primitive[0] == "L":
                    commands.append(f"L{_fmt(primitive[1])}")
                    lines += 1
                else:
                    commands.append(
                        f"C{_fmt(primitive[1])} {_fmt(primitive[2])} {_fmt(primitive[3])}")
                    cubics += 1
        else:
            if not commands:
                commands.append(f"M{_fmt(points[0])}")
            for point in points[1:]:
                commands.append(f"L{_fmt(point)}")
                lines += 1
    commands.append("Z")
    return {"d": " ".join(commands), "lines": lines, "cubics": cubics}


def region_components(track, fits: dict[str, "ChainFit"], geometry) -> list[dict]:
    """Trace the outline of a planar region from the canonical chains it is built on.

    Reuses each interface's once-fitted curve (recovery included), dissolving only the
    interior interfaces the region swallows, so a solid underlayer registers exactly
    against every neighbour instead of being re-optimised into gaps. The region's own
    boundary rings drive the walk, so no separate topology traversal can fail to close.
    """
    chains = {chain.identity: chain for chain in track.chains}
    segment_owner: dict = {}
    for chain in track.chains:
        points = chain.points
        for index in range(len(points) - 1):
            segment_owner[frozenset((points[index], points[index + 1]))] = chain.identity
    polygons = [] if geometry.is_empty else (
        [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms))
    components = []
    for polygon in polygons:
        rings = [_region_ring_path(chains, fits, segment_owner, polygon.exterior),
                 *(_region_ring_path(chains, fits, segment_owner, hole)
                   for hole in polygon.interiors)]
        lines = sum(ring["lines"] for ring in rings)
        cubics = sum(ring["cubics"] for ring in rings)
        components.append({"d": " ".join(ring["d"] for ring in rings),
                           "nodes": lines + cubics,
                           "holes": len(polygon.interiors),
                           "fit_metrics": FitMetrics(lines, cubics, 0, 0, lines + cubics).as_dict(),
                           "area": float(polygon.area)})
    components.sort(key=lambda item: -item["area"])
    return components


def shared_boundary_components(track, included: list[int], settings: TraceSettings,
                               recovery: dict[str, dict[str, object]] | None = None,
                               progress: Progress | None = None,
                               ) -> tuple[dict[int, list[dict]], dict[str, "ChainFit"], dict[str, object]]:
    """Fit canonical interfaces once, then assemble exact reverse uses into paths."""
    from vinylgen.boundary_graph import assemble_colour_rings

    fitter = SharedChainFitter()
    chains = {chain.identity: chain for chain in track.chains}
    fits: dict[str, ChainFit] = {}
    recovery = recovery or {}
    tick = progress or _noop
    total = len(track.chains)
    for index, chain in enumerate(track.chains):
        tick(index / total if total else 1.0,
             f"Fitting boundary curves ({index + 1} of {total})")
        anchors = [HardAnchor(anchor.point, anchor.kind) for anchor in chain.anchors]
        chain_settings = settings
        samples = chain.points
        state = recovery.get(chain.identity, {})
        stage = int(state.get("stage", 0))
        constraint = state.get("constraint")
        constraints = [tuple(constraint)] if constraint is not None and stage >= 3 else []
        if stage >= 1:
            chain_settings = replace(
                chain_settings,
                curve_fit_tolerance=max(0.05, settings.curve_fit_tolerance / 2.0))
        if stage >= 2:
            chain_settings = replace(
                chain_settings,
                curve_fit_tolerance=max(0.05, settings.curve_fit_tolerance / 4.0),
                straight_line_max_deviation=max(
                    0.05, settings.straight_line_max_deviation / 2.0))
        if stage >= 4:
            primitives = tuple(("L", point) for point in samples[1:])
            fits[chain.identity] = ChainFit(
                samples[0], primitives,
                FitMetrics(len(primitives), 0, len(anchors), len(samples), len(primitives)),
                (RunDiagnostic(
                    "curve", 0, len(samples) - 1, 0.0, len(primitives),
                    recovery_attempts=4, dense_fallback=True,
                    fallback_reason=(
                        "maximum deviation still exceeded after ordered local recovery")),))
        else:
            fitted = fitter.fit(samples, chain_settings, anchors, constraints)
            if stage:
                fitted = replace(fitted, diagnostics=tuple(
                    replace(item, recovery_attempts=stage) for item in fitted.diagnostics))
            fits[chain.identity] = fitted

    assembled = assemble_colour_rings(track, included)
    result: dict[int, list[dict]] = {}
    for colour, rings in assembled.items():
        result[colour] = _rows_to_components(
            [_ring_row(chains, fits, ring.uses) for ring in rings])

    unique = FitMetrics(
        sum(fit.metrics.lines for fit in fits.values()),
        sum(fit.metrics.cubics for fit in fits.values()),
        sum(fit.metrics.hard_anchors for fit in fits.values()),
        sum(fit.metrics.source_samples for fit in fits.values()),
        sum(fit.metrics.final_nodes for fit in fits.values()),
    )
    return result, fits, {"unique_chains": len(fits),
                    "recovery_stages": [
                        "tighten_curve", "reclassify_geometry",
                        "add_local_constraint", "dense_fallback"],
                    "recovered_chains": len(recovery),
                    "dense_fallback_chains": sum(
                        int(state.get("stage", 0)) >= 4 for state in recovery.values()),
                    "chain_recovery": {
                        identity: {"stage": int(state.get("stage", 0)),
                                   "constraint": state.get("constraint")}
                        for identity, state in sorted(recovery.items())},
                    "chain_diagnostics": {
                        identity: [item.as_dict() for item in fit.diagnostics]
                        for identity, fit in sorted(fits.items())
                    },
                    "fit_metrics": unique.as_dict()}
