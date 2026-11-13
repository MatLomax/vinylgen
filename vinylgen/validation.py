from __future__ import annotations

import re
import math
import hashlib
import threading
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

import numpy as np
from numba import njit, prange
from scipy import ndimage

from vinylgen.svg_writer import SVG


def _build_srgb_linear_lut() -> np.ndarray:
    """sRGB-to-linear value for every uint8 channel byte, as float32.

    Each byte maps to the same float32 the elementwise expression produces:
    ``byte / 255`` in float32 followed by the piecewise sRGB decode. Indexing
    this table by a uint8 channel therefore yields values bit-identical to the
    elementwise path on the same input.
    """
    values = np.arange(256, dtype=np.float32) / 255.0
    linear = np.where(values <= 0.04045, values / 12.92,
                      ((values + 0.055) / 1.055) ** 2.4)
    return linear.astype(np.float32)


_SRGB_LINEAR_LUT = _build_srgb_linear_lut()


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Return perceptual luminance in [0, 1] without treating sRGB as linear."""
    array = np.asarray(rgb)
    if array.dtype == np.uint8:
        linear = _SRGB_LINEAR_LUT[array]
    else:
        values = np.asarray(rgb, dtype=np.float32) / 255.0
        linear = np.where(values <= 0.04045, values / 12.92,
                          ((values + 0.055) / 1.055) ** 2.4)
    return (0.2126 * linear[..., 0] + 0.7152 * linear[..., 1]
            + 0.0722 * linear[..., 2])


# One kernel call saturates every core, so concurrent callers (the CLI traces
# independent subjects on a thread pool) gain nothing from overlapping. Holding
# this lock across the call keeps it safe on numba's workqueue threading layer,
# which rejects concurrent parallel entry when TBB or OpenMP is unavailable.
_RIDGE_STRENGTH_LOCK = threading.Lock()


@njit(parallel=True, cache=True, fastmath=False)
def _ridge_strength_kernel(luminance: np.ndarray,
                           maximum_half_width: int) -> np.ndarray:
    """Strongest two-sided darkening per pixel over the four normals.

    For each pixel and each distance 1..``maximum_half_width`` along the normals
    ``(0,1)``, ``(1,0)``, ``(1,1)`` and ``(1,-1)``, both opposite samples must lie
    inside the image; the darkening ``min(first, second) - centre`` in float32
    then contributes to the running maximum. Out-of-range samples contribute
    nothing, matching the NaN produced by the constant-fill scipy formulation.
    """
    height, width = luminance.shape
    strength = np.zeros((height, width), dtype=np.float32)
    for y in prange(height):
        for x in range(width):
            centre = luminance[y, x]
            best = np.float32(0.0)
            for distance in range(1, maximum_half_width + 1):
                for normal in range(4):
                    if normal == 0:
                        step_y = 0
                        step_x = distance
                    elif normal == 1:
                        step_y = distance
                        step_x = 0
                    elif normal == 2:
                        step_y = distance
                        step_x = distance
                    else:
                        step_y = distance
                        step_x = -distance
                    first_y = y - step_y
                    first_x = x - step_x
                    second_y = y + step_y
                    second_x = x + step_x
                    if (0 <= first_y < height and 0 <= first_x < width
                            and 0 <= second_y < height and 0 <= second_x < width):
                        first = luminance[first_y, first_x]
                        second = luminance[second_y, second_x]
                        nearer = first if first < second else second
                        dark = nearer - centre
                        if dark > best:
                            best = dark
            strength[y, x] = best
    return strength


def _two_sided_dark_ridge_strength(luminance: np.ndarray,
                                   maximum_half_width: int) -> np.ndarray:
    array = np.asarray(luminance)
    if array.dtype == np.float32 and np.isfinite(array).all():
        contiguous = np.ascontiguousarray(array, dtype=np.float32)
        with _RIDGE_STRENGTH_LOCK:
            return _ridge_strength_kernel(contiguous, int(maximum_half_width))
    # Non-float32 or non-finite luminance keeps the constant-fill scipy path so
    # NaN propagation and wider dtypes stay exactly as the elementwise form.
    strength = np.zeros(array.shape, dtype=np.float32)
    normals = ((0, 1), (1, 0), (1, 1), (1, -1))
    for distance in range(1, maximum_half_width + 1):
        for dy, dx in normals:
            offset = (dy * distance, dx * distance)
            first = ndimage.shift(array, offset, order=0, mode="constant", cval=np.nan)
            second = ndimage.shift(array, (-offset[0], -offset[1]), order=0,
                                   mode="constant", cval=np.nan)
            dark = np.minimum(first, second) - array
            strength = np.fmax(strength, dark)
    return strength


def _dark_feature_support(luminance: np.ndarray, maximum_half_width: int) -> np.ndarray:
    """Dark material locally distinct from at least one neighbouring side."""
    size = 2 * maximum_half_width + 1
    return ndimage.maximum_filter(luminance, size=size, mode="nearest") - luminance


def source_thin_feature_mask(
    source_rgb: np.ndarray,
    *,
    minimum_contrast: float = 0.055,
    minimum_span: float = 6.0,
    maximum_half_width: int = 4,
) -> np.ndarray:
    """Detect coherent dark ridges using evidence on *both* sides.

    Detection is dark-ridge only: an intentional stroke must be darker than the
    samples on both sides along at least one normal, regardless of whether the
    two backgrounds share a colour.  A normal antialiased edge has unlike
    evidence on its two sides and is therefore not a ridge.  Multiple offsets
    make the test insensitive to a stroke being one to several pixels wide.
    Lighter-than-both-sides accents are not detected here.
    """
    rgb = np.asarray(source_rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("source RGB must have shape (height, width, 3)")
    luminance = _luminance(rgb)
    ridge_strength = _two_sided_dark_ridge_strength(luminance, maximum_half_width)
    candidates = ridge_strength >= minimum_contrast
    candidates = ndimage.binary_closing(candidates, structure=np.ones((3, 3)))
    components, count = ndimage.label(candidates, structure=np.ones((3, 3), dtype=np.uint8))
    result = np.zeros(candidates.shape, dtype=bool)
    for index, bounds in enumerate(ndimage.find_objects(components), start=1):
        if bounds is None:
            continue
        height = bounds[0].stop - bounds[0].start
        width = bounds[1].stop - bounds[1].start
        mask = components[bounds] == index
        # Geodesic extent is expensive at full illustration scale; bounding
        # extent plus a minimum population rejects isolated noise while retaining
        # curved strokes whose endpoint distance understates their length.
        if max(height, width) >= minimum_span and int(mask.sum()) >= minimum_span:
            result[bounds] |= mask
    return result


def _candidate_distances(candidate: np.ndarray) -> np.ndarray:
    """Distance from every pixel to the nearest candidate pixel."""
    if np.any(candidate):
        return ndimage.distance_transform_edt(~candidate)
    return np.full(candidate.shape, np.inf, dtype=np.float32)


def _feature_metrics(reference: np.ndarray, candidate: np.ndarray,
                     maximum_deviation: float,
                     candidate_distances: np.ndarray | None = None) -> dict[str, float | int]:
    """Score how well ``candidate`` recalls ``reference``.

    ``candidate_distances`` is the candidate's distance field from
    ``_candidate_distances``; a caller scoring several references against one
    candidate passes it so the transform runs once.
    """
    structure = np.ones((3, 3), dtype=np.uint8)
    components, count = ndimage.label(reference, structure=structure)
    distances = (candidate_distances if candidate_distances is not None
                 else _candidate_distances(candidate))
    reference_distances = distances[reference]
    matched = reference_distances <= maximum_deviation
    recalled_components = 0
    component_recalls: list[float] = []
    # ndimage.label emits contiguous labels 1..count, each with pixels, so
    # find_objects yields one bounding-box slice per label and none are None.
    # Scoring each component inside its bounding box gives the same per-pixel
    # ratio as a whole-image scan without paying O(pixels) for every label.
    objects = ndimage.find_objects(components)
    for index in range(1, count + 1):
        window = objects[index - 1]
        local = components[window] == index
        recall = float(np.mean(distances[window][local] <= maximum_deviation))
        component_recalls.append(recall)
        recalled_components += int(recall >= 0.6)
    finite = reference_distances[np.isfinite(reference_distances)]
    pixel_recall = float(matched.mean()) if matched.size else 1.0
    component_recall = float(recalled_components / count) if count else 1.0
    p95 = (float(np.percentile(finite, 95))
           if len(finite) == len(reference_distances) and len(finite)
           else (0.0 if not len(reference_distances) else float("inf")))
    maximum = (float(finite.max())
               if len(finite) == len(reference_distances) and len(finite)
               else (0.0 if not len(reference_distances) else float("inf")))
    return {
        "source_pixels": int(reference.sum()),
        "matched_pixels": int(matched.sum()),
        "pixel_recall": pixel_recall,
        "source_components": int(count),
        "recalled_components": int(recalled_components),
        "component_recall": component_recall,
        "worst_component_pixel_recall": min(component_recalls, default=1.0),
        "geometric_deviation_p95_px": p95,
        "geometric_deviation_max_px": maximum,
        "topology_preserved": recalled_components == count,
        "passed": (pixel_recall >= 0.9 and component_recall == 1.0
                   and p95 <= maximum_deviation),
    }


def _mask_width(mask: np.ndarray) -> float:
    """Approximate full local feature width without assuming a line direction."""
    if not np.any(mask):
        return 0.0
    return 2.0 * float(ndimage.distance_transform_edt(mask).max())


def _postfit_invariants(reference: np.ndarray,
                        reference_labels: np.ndarray | None,
                        maximum_deviation: float,
                        structure: np.ndarray) -> dict[str, object]:
    """Reference-only geometry a post-fit validation reuses across candidates.

    Labelling, the nearest-protected-component map, and every per-component
    window, corridor, source label, area and width depend solely on the
    protected mask, its label field and the deviation corridor. None of them
    read the candidate, so a recovery loop revalidating the same subject can
    compute them once.
    """
    labelled, count = ndimage.label(reference, structure=structure)
    nearest_indices = ndimage.distance_transform_edt(
        ~reference, return_distances=False, return_indices=True)
    nearest_component = labelled[tuple(nearest_indices)]
    halo = int(math.ceil(maximum_deviation))
    height, width = reference.shape
    components: list[dict[str, object]] = []
    for index, bounds in enumerate(ndimage.find_objects(labelled), start=1):
        if bounds is None:
            continue
        y_bounds, x_bounds = bounds
        expanded = (
            slice(max(0, y_bounds.start - halo), min(height, y_bounds.stop + halo)),
            slice(max(0, x_bounds.start - halo), min(width, x_bounds.stop + halo)),
        )
        component = labelled[expanded] == index
        corridor = ndimage.distance_transform_edt(~component) <= maximum_deviation
        source_label = None
        if reference_labels is not None:
            values, frequencies = np.unique(reference_labels[expanded][component],
                                             return_counts=True)
            source_label = int(values[int(np.argmax(frequencies))])
        components.append({
            "index": index,
            "y_bounds": y_bounds,
            "x_bounds": x_bounds,
            "expanded": expanded,
            "component": component,
            "corridor": corridor,
            "source_label": source_label,
            "source_area": int(component.sum()),
            "source_width": _mask_width(component),
        })
    return {
        "count": count,
        "nearest_component": nearest_component,
        "components": components,
    }


def validate_postfit_protected_features(
    protected_features: np.ndarray,
    candidate_features: np.ndarray,
    *,
    reference_labels: np.ndarray | None = None,
    candidate_labels: np.ndarray | None = None,
    maximum_deviation: float = 2.0,
    minimum_area_ratio: float = 0.5,
    minimum_width_ratio: float = 0.5,
    minimum_component_pixel_recall: float = 0.6,
    invariant_cache: dict | None = None,
    changed_region: np.ndarray | None = None,
    previous_components: list[dict] | None = None,
) -> dict[str, object]:
    """Validate protected geometry after fitting without demanding pixel identity.

    Candidate geometry is associated with each protected component inside the
    configured deviation corridor.  A translated or smoothed curve can pass,
    while disappearance, fragmentation, or collapse below viable area/width
    cannot be hidden by good aggregate recall elsewhere in the image.

    On the label-aware path every component's row is a pure function of the
    candidate labels inside that component's halo-padded window plus the cached
    reference invariants; the candidate mask itself is uniform there. A caller
    revalidating one subject across recovery passes may therefore pass the
    ``previous_components`` rows and a ``changed_region`` mask of where the
    candidate labels changed since those rows: any component whose window sees
    no change is spliced verbatim, giving a result byte-identical to a full
    recomputation while recomputing only the region a re-fit actually touched.
    """
    reference = np.asarray(protected_features, dtype=bool)
    candidate = np.asarray(candidate_features, dtype=bool)
    if reference.shape != candidate.shape or reference.ndim != 2:
        raise ValueError("protected and candidate feature masks must be matching 2D arrays")
    if (reference_labels is None) != (candidate_labels is None):
        raise ValueError("reference and candidate labels must be provided together")
    if reference_labels is not None:
        reference_labels = np.asarray(reference_labels)
        candidate_labels = np.asarray(candidate_labels)
        if reference_labels.shape != reference.shape or candidate_labels.shape != reference.shape:
            raise ValueError("label dimensions must match protected-feature masks")
    if maximum_deviation < 0:
        raise ValueError("maximum deviation must be non-negative")
    if not 0 <= minimum_area_ratio <= 1 or not 0 <= minimum_width_ratio <= 1:
        raise ValueError("minimum area/width ratios must be between zero and one")

    structure = np.ones((3, 3), dtype=np.uint8)
    # Assign candidate pixels in overlapping deviation corridors to the nearest
    # protected source component. This prevents a nearby component from making
    # an omitted one appear split, over-wide, or spuriously preserved. The whole
    # nearest-component map, labelling, and per-component windows depend only on
    # the protected mask, its labels and the corridor width, so a caller
    # revalidating one subject across passes may reuse them through the cache.
    if invariant_cache is not None:
        reference_key = hashlib.sha256(np.ascontiguousarray(reference).tobytes()).hexdigest()
        labels_key = (None if reference_labels is None
                      else hashlib.sha256(
                          np.ascontiguousarray(reference_labels).tobytes()).hexdigest())
        cache_key = ("postfit_invariants", reference.shape, float(maximum_deviation),
                     reference_key, labels_key)
        invariants = invariant_cache.get(cache_key)
        if invariants is None:
            invariants = _postfit_invariants(reference, reference_labels,
                                             maximum_deviation, structure)
            invariant_cache[cache_key] = invariants
    else:
        invariants = _postfit_invariants(reference, reference_labels,
                                         maximum_deviation, structure)
    count = invariants["count"]
    nearest_component = invariants["nearest_component"]
    # The candidate distance transform is only read on the label-free path; the
    # label-aware path never pays for this full-image transform.
    lazy_candidate_distance: list[np.ndarray | None] = [None]

    def candidate_distance() -> np.ndarray:
        if lazy_candidate_distance[0] is None:
            lazy_candidate_distance[0] = (
                ndimage.distance_transform_edt(~candidate) if np.any(candidate)
                else np.full(reference.shape, np.inf))
        return lazy_candidate_distance[0]

    # Splice unchanged rows from the previous pass. This is byte-safe only when a
    # row is a pure function of the candidate labels inside its own halo-padded
    # window, which needs three things: the label-aware path (so the whole-image
    # candidate-distance branch is unreachable), and a candidate mask that is
    # uniform (so ``changed_region`` computed from the labels alone captures every
    # per-pass difference). A uniform mask is checked here rather than trusted, so
    # a caller that ever passes splice inputs with a non-uniform candidate simply
    # falls back to a full, correct recomputation instead of splicing wrongly. A
    # component whose expanded window contains no changed pixel then has identical
    # window labels and therefore an identical row.
    records = invariants["components"]
    splice = (candidate_labels is not None and changed_region is not None
              and previous_components is not None
              and len(previous_components) == len(records)
              and bool(candidate.all()))

    rows: list[dict[str, object]] = []
    for position, record in enumerate(records):
        if splice:
            previous_row = previous_components[position]
            if (previous_row["component"] == record["index"]
                    and not changed_region[record["expanded"]].any()):
                rows.append(previous_row)
                continue
        index = record["index"]
        y_bounds = record["y_bounds"]
        x_bounds = record["x_bounds"]
        expanded = record["expanded"]
        component = record["component"]
        corridor = record["corridor"]
        source_label = record["source_label"]
        source_area = record["source_area"]
        source_width = record["source_width"]
        associated = (candidate[expanded] & corridor
                      & (nearest_component[expanded] == index))
        if source_label is not None:
            associated &= candidate_labels[expanded] == source_label
        if source_label is None:
            source_distances = candidate_distance()[expanded][component]
        else:
            local_label_candidate = (candidate[expanded]
                                     & (candidate_labels[expanded] == source_label))
            local_distance = (ndimage.distance_transform_edt(~local_label_candidate)
                              if np.any(local_label_candidate)
                              else np.full(component.shape, np.inf))
            source_distances = local_distance[component]
        pixel_recall = float(np.mean(source_distances <= maximum_deviation))
        finite = source_distances[np.isfinite(source_distances)]
        p95 = (float(np.percentile(finite, 95)) if len(finite) == len(source_distances)
               and len(finite) else float("inf"))
        maximum = (float(finite.max()) if len(finite) == len(source_distances)
                   and len(finite) else float("inf"))
        associated_labels, associated_count = ndimage.label(associated, structure=structure)
        sizes = np.bincount(associated_labels.ravel())[1:]
        # Ignore one-pixel rasterization crumbs when deciding whether fitting
        # split one physical feature into multiple pieces.
        viable_piece_floor = min(source_area, max(2, round(source_area * 0.05)))
        viable_pieces = int(np.count_nonzero(sizes >= viable_piece_floor))
        candidate_area = int(associated.sum())
        area_ratio = candidate_area / source_area
        candidate_width = _mask_width(associated)
        width_ratio = candidate_width / source_width if source_width else 1.0
        survived = pixel_recall >= minimum_component_pixel_recall
        topology_preserved = viable_pieces == 1
        area_viable = area_ratio >= minimum_area_ratio
        width_viable = width_ratio >= minimum_width_ratio
        passed = bool(survived and topology_preserved and area_viable and width_viable
                      and p95 <= maximum_deviation)
        failure_reasons = []
        if not survived:
            failure_reasons.append("insufficient feature recall")
        if not topology_preserved:
            failure_reasons.append("feature split or omitted")
        if not area_viable:
            failure_reasons.append("area collapsed below viability ratio")
        if not width_viable:
            failure_reasons.append("width collapsed below viability ratio")
        if p95 > maximum_deviation:
            failure_reasons.append("positional deviation exceeded")
        rows.append({
            "component": index,
            "source_label": source_label,
            "source_bounds_px": [x_bounds.start, y_bounds.start,
                                 x_bounds.stop, y_bounds.stop],
            "source_area_px": source_area,
            "candidate_area_in_corridor_px": candidate_area,
            "area_ratio": area_ratio,
            "source_width_px": source_width,
            "candidate_width_px": candidate_width,
            "width_ratio": width_ratio,
            "pixel_recall": pixel_recall,
            "geometric_deviation_p95_px": p95,
            "geometric_deviation_max_px": maximum,
            "candidate_pieces": viable_pieces,
            "survived": survived,
            "topology_preserved": topology_preserved,
            "area_viable": area_viable,
            "width_viable": width_viable,
            "failure_reasons": failure_reasons,
            "passed": passed,
        })
    return {
        "settings": {
            "maximum_deviation_px": maximum_deviation,
            "minimum_area_ratio": minimum_area_ratio,
            "minimum_width_ratio": minimum_width_ratio,
            "minimum_component_pixel_recall": minimum_component_pixel_recall,
        },
        "source_components": count,
        "surviving_components": sum(bool(row["survived"]) for row in rows),
        "topology_failures": sum(not bool(row["topology_preserved"]) for row in rows),
        "area_failures": sum(not bool(row["area_viable"]) for row in rows),
        "width_failures": sum(not bool(row["width_viable"]) for row in rows),
        "components": rows,
        "passed": all(bool(row["passed"]) for row in rows),
    }


def validate_semantic_preservation(
    source_rgb: np.ndarray,
    segmentation_rgb: np.ndarray,
    *,
    vector_rgb: np.ndarray | None = None,
    protected_features: np.ndarray | None = None,
    segmentation_labels: np.ndarray | None = None,
    vector_labels: np.ndarray | None = None,
    maximum_deviation: float = 2.0,
    minimum_feature_contrast: float = 0.055,
    minimum_feature_span: float = 6.0,
    invariant_cache: dict | None = None,
    defer_vector_dark_metrics: bool = False,
) -> dict[str, object]:
    """Audit source feature recall independently of upstream protection flags.

    ``vector_rgb`` is a rasterization of the final vector partition at the
    source dimensions.  Keeping rendering outside this module makes the audit
    independent of a particular SVG renderer and useful in unit tests.

    Everything derived from the source, segmentation, and protected masks is
    independent of the candidate vector.  A caller re-validating the same subject
    across the recovery loop's repeated passes may supply ``invariant_cache`` (a
    plain dict) so that source-side work is computed once and reused; only the
    ``vector_*`` results are recomputed per pass.

    The label-aware dark-support metrics (``vector`` and
    ``protected_features['vector']``) rest on a whole-image distance transform of
    the rendered vector and feed only the reported audit, never the recovery
    loop's re-fit decision (which reads ``vector_postfit`` alone). A caller
    driving that loop may set ``defer_vector_dark_metrics`` on the intermediate
    passes to skip them, then leave it unset on a final pass over the settled
    vector to compute them once. The final full result is byte-identical to
    computing them every pass; only discarded intermediate work is saved. The
    flag is ignored on the label-free path, where those metrics are the gate.
    """
    cache = invariant_cache if invariant_cache is not None else {}
    source = np.asarray(source_rgb)
    segmented = np.asarray(segmentation_rgb)
    if source.shape != segmented.shape:
        raise ValueError("source and segmentation RGB dimensions differ")
    if vector_labels is not None and segmentation_labels is None:
        raise ValueError("vector labels require segmentation reference labels")
    if vector_rgb is not None and segmentation_labels is not None and vector_labels is None:
        raise ValueError("label-aware vector validation requires vector labels")
    feature_arguments = {
        "minimum_contrast": minimum_feature_contrast,
        "minimum_span": minimum_feature_span,
    }
    if "source_features" not in cache:
        cache["source_features"] = source_thin_feature_mask(source, **feature_arguments)
    source_features = cache["source_features"]
    # Candidate support need not independently pass source continuity again:
    # the reference already establishes that this is a coherent feature. Test
    # whether matching two-sided contrast survives near its source position.
    if "segmentation_features" not in cache:
        cache["segmentation_features"] = (
            _dark_feature_support(_luminance(segmented), 4)
            >= minimum_feature_contrast * 0.75
        )
    segmentation_features = cache["segmentation_features"]
    if "segmentation" not in cache:
        cache["segmentation_distances"] = _candidate_distances(segmentation_features)
        cache["segmentation"] = _feature_metrics(
            source_features, segmentation_features, maximum_deviation,
            candidate_distances=cache["segmentation_distances"])
    segmentation = cache["segmentation"]
    # The dark-support metrics are the protected gate only when palette identity
    # is unavailable; with vector labels present they inform the audit alone, so
    # a recovery driver may defer them off the intermediate passes.
    defer_vector = defer_vector_dark_metrics and vector_labels is not None
    vector = None
    vector_features = None
    vector_distances = None
    if vector_rgb is not None:
        rendered = np.asarray(vector_rgb)
        if rendered.shape != source.shape:
            raise ValueError("source and vector RGB dimensions differ")
        if not defer_vector:
            vector_features = (
                _dark_feature_support(_luminance(rendered), 4)
                >= minimum_feature_contrast * 0.75
            )
            vector_distances = _candidate_distances(vector_features)
            vector = _feature_metrics(source_features, vector_features, maximum_deviation,
                                      candidate_distances=vector_distances)
    protected = None
    if protected_features is not None:
        protected_mask = np.asarray(protected_features, dtype=bool)
        if protected_mask.shape != source.shape[:2]:
            raise ValueError("protected-feature dimensions differ from source")
        if "protected_base" not in cache:
            cache["protected_base"] = _feature_metrics(
                protected_mask, segmentation_features, maximum_deviation,
                candidate_distances=cache.get("segmentation_distances"))
            segmentation_postfit_features = (
                np.ones(protected_mask.shape, dtype=bool)
                if segmentation_labels is not None else segmentation_features)
            cache["protected_postfit"] = validate_postfit_protected_features(
                protected_mask, segmentation_postfit_features,
                reference_labels=segmentation_labels,
                candidate_labels=segmentation_labels,
                maximum_deviation=maximum_deviation,
                invariant_cache=cache)
        # Fresh top-level dict per pass: the vector_* keys below are added onto it,
        # so the cached invariants must not accumulate them.
        protected = dict(cache["protected_base"])
        protected["postfit"] = cache["protected_postfit"]
        if vector_rgb is not None:
            if not defer_vector:
                protected["vector"] = _feature_metrics(
                    protected_mask, vector_features, maximum_deviation,
                    candidate_distances=vector_distances)
            vector_postfit_features = (
                np.ones(protected_mask.shape, dtype=bool)
                if vector_labels is not None else vector_features)
            # On the label-aware path the postfit candidate mask is uniform, so
            # the only per-pass input is vector_labels: a component's row changes
            # only where those labels changed since the previous pass. Hand the
            # previous rows and that change region down so unchanged components
            # are spliced instead of re-fit. The spliced result is byte-identical
            # to a full recomputation, so the reported audit is unaffected.
            changed_region = None
            previous_components = None
            previous_labels = cache.get("vector_postfit_labels")
            previous_result = cache.get("vector_postfit_result")
            if (vector_labels is not None and previous_labels is not None
                    and previous_result is not None
                    and previous_labels.shape == vector_labels.shape):
                changed_region = previous_labels != vector_labels
                previous_components = previous_result["components"]
            protected["vector_postfit"] = validate_postfit_protected_features(
                protected_mask, vector_postfit_features,
                reference_labels=segmentation_labels,
                candidate_labels=vector_labels,
                maximum_deviation=maximum_deviation,
                invariant_cache=cache,
                changed_region=changed_region,
                previous_components=previous_components)
            if vector_labels is not None:
                cache["vector_postfit_labels"] = vector_labels
                cache["vector_postfit_result"] = protected["vector_postfit"]
    stages = [segmentation, *([vector] if vector is not None else [])]
    if protected is not None:
        stages.append(protected["postfit"])
        # Dark-support recall is an additional gate only when palette identity
        # is unavailable. Label-aware post-fit validation also supports light
        # protected accents, for which darkness is not meaningful evidence.
        if segmentation_labels is None:
            stages.append(protected)
        if "vector" in protected:
            stages.append(protected["vector_postfit"])
            if vector_labels is None:
                stages.append(protected["vector"])
    return {
        "settings": {
            "maximum_deviation_px": maximum_deviation,
            "minimum_feature_contrast": minimum_feature_contrast,
            "minimum_feature_span_px": minimum_feature_span,
        },
        "source_feature_pixels": int(source_features.sum()),
        "segmentation": segmentation,
        "vector": vector,
        "protected_features": protected,
        "passed": all(bool(stage["passed"]) for stage in stages),
    }


def validate_feature_roi(
    source_rgb: np.ndarray,
    candidate_rgb: np.ndarray,
    normalized_bounds: tuple[float, float, float, float],
    *,
    maximum_deviation: float = 2.0,
) -> dict[str, object]:
    """Run source-derived feature acceptance inside a stable normalized ROI."""
    source = np.asarray(source_rgb)
    candidate = np.asarray(candidate_rgb)
    if source.shape != candidate.shape or source.ndim != 3:
        raise ValueError("source and candidate must be matching RGB arrays")
    left, top, right, bottom = normalized_bounds
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        raise ValueError("normalized ROI bounds must be ordered within [0, 1]")
    height, width = source.shape[:2]
    x0, x1 = round(left * width), round(right * width)
    y0, y1 = round(top * height), round(bottom * height)
    audit = validate_semantic_preservation(
        source[y0:y1, x0:x1], candidate[y0:y1, x0:x1],
        maximum_deviation=maximum_deviation)
    audit["source_bounds_px"] = [x0, y0, x1, y1]
    audit["normalized_bounds"] = list(normalized_bounds)
    return audit


def semantic_warning_mask(source_rgb: np.ndarray, candidate_rgb: np.ndarray,
                          *, maximum_deviation: float = 2.0,
                          minimum_feature_contrast: float = 0.055,
                          minimum_feature_span: float = 6.0,
                          source_features: np.ndarray | None = None) -> np.ndarray:
    """Return source feature pixels lacking nearby dark support in a result.

    A caller that has already computed the source thin-feature mask may pass it
    as ``source_features`` to avoid recomputing it here.
    """
    reference = (source_features if source_features is not None
                 else source_thin_feature_mask(
                     source_rgb, minimum_contrast=minimum_feature_contrast,
                     minimum_span=minimum_feature_span))
    support = (_dark_feature_support(_luminance(candidate_rgb), 4)
               >= minimum_feature_contrast * 0.75)
    distances = (ndimage.distance_transform_edt(~support) if np.any(support)
                 else np.full(reference.shape, np.inf))
    return reference & (distances > maximum_deviation)


def validate_svg(path: Path, palette: Sequence[Mapping[str, object]], included: list[int]) -> dict:
    root = ET.parse(path).getroot()
    layers = root.findall(f"{{{SVG}}}g")
    paths = root.findall(f".//{{{SVG}}}path")
    ids = [node.attrib.get("id") for node in paths]
    unsupported = [name for name in ("image", "mask", "clipPath", "filter", "linearGradient", "radialGradient")
                   if root.findall(f".//{{{SVG}}}{name}")]
    expected = [str(palette[i]["hex"]).upper() for i in included]
    actual = [str(layer.attrib.get("fill", "")).upper() for layer in layers]
    checks = {"xml_valid": True, "layer_count": len(layers), "path_count": len(paths),
              "expected_fills": actual == expected, "unique_path_ids": len(ids) == len(set(ids)),
              "all_paths_closed": all(re.search(r"[zZ]\s*$", p.attrib.get("d", "")) is not None for p in paths),
              "unsupported_elements": unsupported}
    checks["passed"] = all((checks["expected_fills"], checks["unique_path_ids"],
                             checks["all_paths_closed"], not unsupported, len(layers) == len(included)))
    return checks
