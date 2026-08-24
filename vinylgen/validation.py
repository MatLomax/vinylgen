from __future__ import annotations

import re
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from scipy import ndimage

from vinylgen.svg_writer import SVG


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Return perceptual luminance in [0, 1] without treating sRGB as linear."""
    values = np.asarray(rgb, dtype=np.float32) / 255.0
    linear = np.where(values <= 0.04045, values / 12.92,
                      ((values + 0.055) / 1.055) ** 2.4)
    return (0.2126 * linear[..., 0] + 0.7152 * linear[..., 1]
            + 0.0722 * linear[..., 2])


def _two_sided_dark_ridge_strength(luminance: np.ndarray,
                                   maximum_half_width: int) -> np.ndarray:
    strength = np.zeros(luminance.shape, dtype=np.float32)
    normals = ((0, 1), (1, 0), (1, 1), (1, -1))
    for distance in range(1, maximum_half_width + 1):
        for dy, dx in normals:
            offset = (dy * distance, dx * distance)
            first = ndimage.shift(luminance, offset, order=0, mode="constant", cval=np.nan)
            second = ndimage.shift(luminance, (-offset[0], -offset[1]), order=0,
                                   mode="constant", cval=np.nan)
            dark = np.minimum(first, second) - luminance
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


def _feature_metrics(reference: np.ndarray, candidate: np.ndarray,
                     maximum_deviation: float) -> dict[str, float | int]:
    structure = np.ones((3, 3), dtype=np.uint8)
    components, count = ndimage.label(reference, structure=structure)
    if np.any(candidate):
        distances = ndimage.distance_transform_edt(~candidate)
    else:
        distances = np.full(reference.shape, np.inf, dtype=np.float32)
    reference_distances = distances[reference]
    matched = reference_distances <= maximum_deviation
    recalled_components = 0
    component_recalls: list[float] = []
    for index in range(1, count + 1):
        local = components == index
        recall = float(np.mean(distances[local] <= maximum_deviation))
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
) -> dict[str, object]:
    """Validate protected geometry after fitting without demanding pixel identity.

    Candidate geometry is associated with each protected component inside the
    configured deviation corridor.  A translated or smoothed curve can pass,
    while disappearance, fragmentation, or collapse below viable area/width
    cannot be hidden by good aggregate recall elsewhere in the image.
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
    labelled, count = ndimage.label(reference, structure=structure)
    candidate_distance = (ndimage.distance_transform_edt(~candidate) if np.any(candidate)
                          else np.full(reference.shape, np.inf))
    # Assign candidate pixels in overlapping deviation corridors to the nearest
    # protected source component. This prevents a nearby component from making
    # an omitted one appear split, over-wide, or spuriously preserved.
    nearest_indices = ndimage.distance_transform_edt(
        ~reference, return_distances=False, return_indices=True)
    nearest_component = labelled[tuple(nearest_indices)]
    rows: list[dict[str, object]] = []
    halo = int(math.ceil(maximum_deviation))
    height, width = reference.shape
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
        associated = (candidate[expanded] & corridor
                      & (nearest_component[expanded] == index))
        source_label = None
        if reference_labels is not None:
            values, frequencies = np.unique(reference_labels[expanded][component],
                                             return_counts=True)
            source_label = int(values[int(np.argmax(frequencies))])
            associated &= candidate_labels[expanded] == source_label
        if source_label is None:
            source_distances = candidate_distance[expanded][component]
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
        source_area = int(component.sum())
        viable_piece_floor = min(source_area, max(2, round(source_area * 0.05)))
        viable_pieces = int(np.count_nonzero(sizes >= viable_piece_floor))
        candidate_area = int(associated.sum())
        area_ratio = candidate_area / source_area
        source_width = _mask_width(component)
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
) -> dict[str, object]:
    """Audit source feature recall independently of upstream protection flags.

    ``vector_rgb`` is a rasterization of the final vector partition at the
    source dimensions.  Keeping rendering outside this module makes the audit
    independent of a particular SVG renderer and useful in unit tests.
    """
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
    source_features = source_thin_feature_mask(source, **feature_arguments)
    # Candidate support need not independently pass source continuity again:
    # the reference already establishes that this is a coherent feature. Test
    # whether matching two-sided contrast survives near its source position.
    segmentation_features = (
        _dark_feature_support(_luminance(segmented), 4)
        >= minimum_feature_contrast * 0.75
    )
    segmentation = _feature_metrics(source_features, segmentation_features,
                                    maximum_deviation)
    vector = None
    if vector_rgb is not None:
        rendered = np.asarray(vector_rgb)
        if rendered.shape != source.shape:
            raise ValueError("source and vector RGB dimensions differ")
        vector_features = (
            _dark_feature_support(_luminance(rendered), 4)
            >= minimum_feature_contrast * 0.75
        )
        vector = _feature_metrics(source_features, vector_features, maximum_deviation)
    protected = None
    if protected_features is not None:
        protected_mask = np.asarray(protected_features, dtype=bool)
        if protected_mask.shape != source.shape[:2]:
            raise ValueError("protected-feature dimensions differ from source")
        protected = _feature_metrics(protected_mask, segmentation_features,
                                     maximum_deviation)
        segmentation_postfit_features = (
            np.ones(protected_mask.shape, dtype=bool)
            if segmentation_labels is not None else segmentation_features)
        protected["postfit"] = validate_postfit_protected_features(
            protected_mask, segmentation_postfit_features,
            reference_labels=segmentation_labels,
            candidate_labels=segmentation_labels,
            maximum_deviation=maximum_deviation)
        if vector_rgb is not None:
            protected["vector"] = _feature_metrics(
                protected_mask, vector_features, maximum_deviation)
            vector_postfit_features = (
                np.ones(protected_mask.shape, dtype=bool)
                if vector_labels is not None else vector_features)
            protected["vector_postfit"] = validate_postfit_protected_features(
                protected_mask, vector_postfit_features,
                reference_labels=segmentation_labels,
                candidate_labels=vector_labels,
                maximum_deviation=maximum_deviation)
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
                          minimum_feature_span: float = 6.0) -> np.ndarray:
    """Return source feature pixels lacking nearby dark support in a result."""
    reference = source_thin_feature_mask(
        source_rgb, minimum_contrast=minimum_feature_contrast,
        minimum_span=minimum_feature_span)
    support = (_dark_feature_support(_luminance(candidate_rgb), 4)
               >= minimum_feature_contrast * 0.75)
    distances = (ndimage.distance_transform_edt(~support) if np.any(support)
                 else np.full(reference.shape, np.inf))
    return reference & (distances > maximum_deviation)


def validate_svg(path: Path, palette: list[dict], included: list[int]) -> dict:
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
