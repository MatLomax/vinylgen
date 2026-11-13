"""Sizing policy for the raster previews Inkscape renders from traced SVGs."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TypedDict

PREVIEW_DPI = 768
PREVIEW_MAX_SIDE = 1920

# CSS absolute units expressed in inches; unitless lengths are user units at
# the 96 DPI Inkscape assumes.
_INCHES_PER_UNIT = {"": 1 / 96, "px": 1 / 96, "in": 1.0, "mm": 1 / 25.4,
                    "cm": 1 / 2.54, "pt": 1 / 72, "pc": 1 / 6}
_LENGTH = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z%]*)\s*$")


class PreviewPlan(TypedDict):
    """A preview's size: the Inkscape export ``arguments``, the ``dpi`` they render at, the
    pixel ``width`` and ``height``, the ``max_side`` cap and whether it ``capped`` the size."""
    arguments: list[str]
    dpi: float
    width: int
    height: int
    max_side: int
    capped: bool


def _length_inches(value: str | None) -> float | None:
    if value is None:
        return None
    match = _LENGTH.match(value)
    if not match or match.group(2).lower() not in _INCHES_PER_UNIT:
        return None
    return float(match.group(1)) * _INCHES_PER_UNIT[match.group(2).lower()]


def _svg_root(svg: Path | bytes) -> ET.Element:
    if isinstance(svg, Path):
        for _, element in ET.iterparse(svg, events=("start",)):
            return element
        raise ValueError(f"{svg} has no root element")
    return ET.fromstring(svg)


def preview_plan(svg: Path | bytes) -> PreviewPlan:
    """Decide the pixel size of an SVG's preview and the Inkscape flags for it.

    The preview targets ``PREVIEW_DPI`` over the document's physical size,
    but its longer side never exceeds ``PREVIEW_MAX_SIDE`` pixels: it is an
    inspection image, not a print-resolution raster, so large sources render
    at roughly 1080p rather than at gigapixel size.
    """
    root = _svg_root(svg)
    view_box = root.get("viewBox")
    user_width = user_height = None
    if view_box:
        parts = view_box.replace(",", " ").split()
        if len(parts) == 4:
            user_width, user_height = float(parts[2]), float(parts[3])
    width_in = _length_inches(root.get("width"))
    height_in = _length_inches(root.get("height"))
    if width_in is None or height_in is None:
        if user_width is None or user_height is None:
            raise ValueError("SVG declares neither a physical size nor a viewBox")
        width_in, height_in = user_width / 96, user_height / 96
    if user_width is None or user_height is None:
        user_width, user_height = width_in * 96, height_in * 96
    if width_in <= 0 or height_in <= 0:
        raise ValueError("SVG size must be positive")
    full_width, full_height = width_in * PREVIEW_DPI, height_in * PREVIEW_DPI
    longest = max(full_width, full_height)
    cap = PREVIEW_MAX_SIDE
    if longest <= cap:
        return {"arguments": [f"--export-dpi={PREVIEW_DPI}"], "dpi": float(PREVIEW_DPI),
                "width": round(full_width), "height": round(full_height),
                "max_side": cap, "capped": False}
    scale = cap / longest
    if full_width >= full_height:
        arguments = [f"--export-width={round(full_width * scale)}"]
    else:
        arguments = [f"--export-height={round(full_height * scale)}"]
    return {"arguments": arguments, "dpi": PREVIEW_DPI * scale,
            "width": round(full_width * scale), "height": round(full_height * scale),
            "max_side": cap, "capped": True}
