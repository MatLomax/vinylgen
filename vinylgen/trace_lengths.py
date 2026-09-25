"""Trace length units: thousandths of the traced image's longer side.

The trace lengths (``TRACE_LENGTH_FIELDS``) are stored and edited in
thousandths of the longer side of the traced raster (``per_mille``), so the same
artwork traced at 600 px and at 6000 px gets the same corners, node layout and
relative accuracy. The fitter works in pixels of the traced image; every caller
that fits (the trace run, the CLI and the live preview) converts once with
``resolve_trace_lengths`` using the WHOLE traced image's longer side.

Settings recorded before units existed are pixels. They carry no ``length_unit``
marker, or ``length_unit: "px"``; ``TraceSettings(length_unit="px")`` fills an
absent length with the pixel-era default, and ``trace_lengths_in_units`` turns
such a record into thousandths of a given image. See docs/svg-tracing-spec.md
"Trace length units".
"""

from __future__ import annotations

import math
from dataclasses import replace

from vinylgen.contracts import (
    AUTOMATIC_LENGTHS,
    FOLLOWING_LENGTHS,
    LENGTH_UNIT_PER_MILLE,
    LENGTH_UNIT_PX,
    PX_DEFAULTS,
    TRACE_LENGTH_FIELDS,
    TraceSettings,
    upgrade_trace_record,
)
from vinylgen.curve_fit import _tip_skip

# Resolved pixel lengths are rounded to this many decimal places. A pixel value
# converted to thousandths and back then returns exactly (a stored 4 px length is
# 4.0 again, not 4.000000000000001, which would flip a ``>= minimum`` test), while
# the rounding is far below anything the fitter can resolve.
_PX_DECIMALS = 9

# Lengths that never resolve below ``MIN_ACCURACY_PX``, whatever unit they arrive
# in: below a pixel they would chase the boundary's own sub-pixel noise. The trace
# run and the live preview resolve the same settings to the same pixels.
FLOORED_LENGTHS = ("curve_fit_tolerance", "max_deviation", "corner_sharpening_allowance")
MIN_ACCURACY_PX = 1.0


def length_reference(width: int, height: int) -> int:
    """The pixel length one thousand units span: the traced raster's longer side."""
    reference = max(int(width), int(height))
    if reference <= 0:
        raise ValueError("the traced image has no pixels")
    return reference


def _to_px(value: float, reference: int) -> float:
    return round(float(value) * reference / 1000.0, _PX_DECIMALS)


def _to_units(value: float, reference: int) -> float:
    return float(value) * 1000.0 / reference


def _complete(lengths: dict, unit: str) -> dict:
    """``lengths`` with every automatic length (None) at its default in ``unit``
    and every following length (None) at the length it follows."""
    lengths = dict(lengths)
    column = 0 if unit == LENGTH_UNIT_PER_MILLE else 1
    for name, defaults in AUTOMATIC_LENGTHS.items():
        if lengths[name] is None:
            lengths[name] = defaults[column]
    for name, source in FOLLOWING_LENGTHS.items():
        if lengths[name] is None:
            lengths[name] = lengths[source]
    return lengths


def _lengths(settings: TraceSettings) -> dict:
    return {name: getattr(settings, name) for name in TRACE_LENGTH_FIELDS}


def resolve_trace_lengths(settings: TraceSettings, reference: int) -> TraceSettings:
    """``settings`` with every length in pixels of an image whose longer side is
    ``reference`` px, and ``length_unit`` ``px``: what the fitter takes.

    Automatic lengths (``max_deviation`` None) take their default in the settings'
    own unit. ``FLOORED_LENGTHS`` resolve to ``MIN_ACCURACY_PX`` or more; every
    other pixel length keeps its value exactly.
    """
    lengths = _complete(_lengths(settings), settings.length_unit)
    if settings.length_unit == LENGTH_UNIT_PER_MILLE:
        lengths = {name: _to_px(value, reference) for name, value in lengths.items()}
    for name in FLOORED_LENGTHS:
        lengths[name] = max(MIN_ACCURACY_PX, lengths[name])
    return replace(settings, length_unit=LENGTH_UNIT_PX, **lengths)


def trace_lengths_in_units(settings: TraceSettings, reference: int) -> TraceSettings:
    """``settings`` in thousandths of an image whose longer side is ``reference`` px.

    A pixel record converts so that resolving it on the same image gives back its
    pixels exactly; its automatic lengths become explicit pixel-era values (2 px
    for ``max_deviation``), since the automatic value in thousandths is a
    different length. Settings already in thousandths are returned unchanged.
    """
    if settings.length_unit == LENGTH_UNIT_PER_MILLE:
        return replace(settings)
    lengths = _complete(_lengths(settings), LENGTH_UNIT_PX)
    return replace(settings, length_unit=LENGTH_UNIT_PER_MILLE,
                   **{name: _to_units(value, reference) for name, value in lengths.items()})


def length_audit(units: TraceSettings, resolved: TraceSettings, reference: int) -> dict:
    """The ``trace.json`` record of how the lengths were resolved: the reference
    length and each length in thousandths and in pixels."""
    return {"unit": "thousandths of the traced image's longer side",
            "reference_px": int(reference),
            "per_mille": _complete(_lengths(units), LENGTH_UNIT_PER_MILLE),
            "px": _lengths(resolved)}


def is_pixel_record(values: dict) -> bool:
    """Whether a stored trace settings record is in pixels: it says ``px``, or it
    predates units and carries no ``length_unit`` at all."""
    return values.get("length_unit", LENGTH_UNIT_PX) == LENGTH_UNIT_PX


def pixel_record_in_units(values: dict, reference: int) -> dict:
    """A stored pixel-era trace record (a dict, possibly with legacy keys such as
    ``curve_optimization`` or embedded cleanup fields, which are kept as they are)
    with its lengths in thousandths of an image whose longer side is ``reference``.

    Absent lengths take the pixel-era defaults (a following length the recorded
    value it follows, an automatic one its pixel default) before conversion, so the
    record keeps its lengths. Renamed fields are read under their current names
    (``upgrade_trace_record``); a record already in thousandths is otherwise
    returned unchanged.
    """
    values = upgrade_trace_record(values)
    if not is_pixel_record(values):
        return values
    lengths = {name: values.get(name) for name in TRACE_LENGTH_FIELDS}
    for name, default in PX_DEFAULTS.items():
        if lengths[name] is None:
            lengths[name] = default
    lengths = _complete(lengths, LENGTH_UNIT_PX)
    converted = {name: _to_units(value, reference) for name, value in lengths.items()}
    return {**values, **converted, "length_unit": LENGTH_UNIT_PER_MILLE}


# The live preview's hidden margin, as a multiple of the longest window a fit
# reaches from a sample, and its floor in px.
PREVIEW_MARGIN_REACHES = 8.0
PREVIEW_MARGIN_MIN_PX = 64


def preview_margin(resolved: TraceSettings) -> int:
    """The live preview's hidden margin in px for resolved (pixel) settings.

    Boundaries are split at the crop edge, and the fit of a split boundary can
    differ from the full trace's along the whole run between its cusps, not only
    within a fit window of the split. The margin pushes that away from the visible
    window. It grows with the longest window a fit reaches from a sample: the
    analysis span plus the corner detector's tip skip (its side windows start that
    far out), twice the curve feature scale (its smoothing plus its tangent
    windows) and the minimum straight length. No multiple of that reach removes
    the difference. Every smaller multiple measured leaves more of it inside the
    window than 8 does, so the margin stays at 8 reaches
    (docs/svg-tracing-spec.md "Preview margin measurement").
    """
    reach = max(float(resolved.corner_analysis_span) + _tip_skip(resolved),
                2.0 * float(resolved.curve_feature_scale),
                float(resolved.minimum_straight_length))
    return max(PREVIEW_MARGIN_MIN_PX, int(math.ceil(PREVIEW_MARGIN_REACHES * reach)))
