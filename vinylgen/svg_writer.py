from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import t3api

    class PaletteRow(t3api.PaletteEntry):
        """One of vinylgen's palette rows (``palette.json``'s entries, and the rows a trace draws
        in, ``tracing.trace_palette``): t3api's palette entry and the colour's ``rgb``."""
        rgb: list[int]


SVG = "http://www.w3.org/2000/svg"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
SODIPODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
ET.register_namespace("", SVG)
ET.register_namespace("inkscape", INKSCAPE)
ET.register_namespace("sodipodi", SODIPODI)


def svg_document(width: int, height: int,
                 physical_mm: tuple[float, float] | None) -> ET.Element:
    attrs = {"version": "1.1", "viewBox": f"0 0 {width} {height}"}
    if physical_mm is not None:
        attrs["width"] = f"{physical_mm[0]:g}mm"
        attrs["height"] = f"{physical_mm[1]:g}mm"
    else:
        attrs["width"] = str(width)
        attrs["height"] = str(height)
    return ET.Element(f"{{{SVG}}}svg", attrs)


def palette_name(row: PaletteRow) -> str | None:
    """``row["name"]``: a ``KeyError`` when the row has none, as indexing raises."""
    if "name" not in row:
        raise KeyError("name")
    return row["name"]


def layer_slug(name: str | None) -> str:
    """The id fragment of a colour's layer and paths: its name casefolded, runs of other
    characters as one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).casefold()).strip("-")


def segment_area(seg: list) -> float:
    """(1/2) of the integral of x dy - y dx along one exact planar-map segment, in its stored
    direction, in closed form: a line's cross product; an elliptical arc ``E cx cy rx ry rot
    a0 a1`` as cx (y1 - y0) - cy (x1 - x0) + rx ry (a1 - a0) (the centred ellipse sweeps
    rx ry per radian whatever its rotation); a cubic's polynomial integral."""
    kind = seg[0]
    if kind == "L":
        x0, y0, x1, y1 = seg[1:5]
        return 0.5 * (x0 * y1 - x1 * y0)
    if kind == "E":
        _, cx, cy, rx, ry, rot, a0, a1 = seg
        c, s = math.cos(rot), math.sin(rot)

        def point(t: float) -> tuple[float, float]:
            u, v = rx * math.cos(t), ry * math.sin(t)
            return cx + u * c - v * s, cy + u * s + v * c

        (x0, y0), (x1, y1) = point(a0), point(a1)
        return 0.5 * (cx * (y1 - y0) - cy * (x1 - x0) + rx * ry * (a1 - a0))
    if kind == "C":
        # B(t) = sum P_i b_i(t) over the cubic Bernstein basis, so the integral of
        # x y' - y x' over [0, 1] is sum_{i<j} (x_i y_j - x_j y_i) (w_ij - w_ji) with
        # w_ij the integral of b_i b_j'; 20 (w_ij - w_ji) is the table below.
        xs, ys = seg[1::2], seg[2::2]
        total = 0.0
        for i in range(4):
            for j in range(i + 1, 4):
                total += _CUBIC_AREA[i][j] * (xs[i] * ys[j] - xs[j] * ys[i])
        return 0.5 * total / 20.0
    raise ValueError(f"unknown planar-map segment kind {kind!r}")


# 20 times the integral over [0, 1] of b_i b_j' - b_j b_i' for the cubic Bernstein basis
# (i < j), exact: a cubic's signed area is (1/2)(1/20) sum_{i<j} (x_i y_j - x_j y_i) M_ij.
_CUBIC_AREA = ((0, 12, 6, 2), (0, 0, 6, 6), (0, 0, 0, 12), (0, 0, 0, 0))


def face_areas(pm: t3api.PlanarMap) -> dict[int, float]:
    """Each face's drawn area by Green's theorem over its pieces: every edge's signed area
    once, each ring the sum of its signed references, and the face even-odd, its largest
    ring less the others (a face's rings are its outer ring and its holes, which do not
    cross). Exact up to float rounding, and a function of ``map.json`` alone."""
    edge_area = [sum(segment_area(piece["seg"]) for piece in edge["pieces"])
                 for edge in pm["edges"]]
    areas = {}
    for face in pm["faces"]:
        rings = sorted((abs(sum(edge_area[r] if r >= 0 else -edge_area[~r] for r in ring))
                        for ring in face["rings"]), reverse=True)
        areas[face["face"]] = (rings[0] - sum(rings[1:])) if rings else 0.0
    return areas


def face_elements(pm: t3api.PlanarMap) -> dict[int, dict[str, int]]:
    """Each face's drawn pieces by kind: ``L``, ``A circular`` (rx = ry), ``A elliptical``,
    ``C``, and the connectors ``border`` and ``gap``."""
    edge_kinds = []
    for edge in pm["edges"]:
        counts: dict[str, int] = {}
        for piece in edge["pieces"]:
            kind = piece["kind"]
            if kind == "A":
                kind = "A circular" if piece["seg"][3] == piece["seg"][4] else "A elliptical"
            counts[kind] = counts.get(kind, 0) + 1
        edge_kinds.append(counts)
    faces = {}
    for face in pm["faces"]:
        total: dict[str, int] = {}
        for ring in face["rings"]:
            for ref in ring:
                for kind, count in edge_kinds[ref if ref >= 0 else ~ref].items():
                    total[kind] = total.get(kind, 0) + count
        faces[face["face"]] = total
    return faces


def add_face_layer(root: ET.Element, row: PaletteRow, pm: t3api.PlanarMap, *,
                   paths: list[t3api.FacePath] | None = None,
                   areas: dict[int, float] | None = None,
                   elements: dict[int, dict[str, int]] | None = None) -> dict:
    """Append one colour's layer of the edge SVG from a planar map read from ``map.json``.

    ``row`` is the colour's palette entry (``index``, ``name``, ``hex``). The layer holds one
    even-odd path per face of label ``row["index"]``, its ``d`` and ``sodipodi:nodetypes``
    exactly as ``t3api.planar_map_paths`` gives them, numbered from 001 largest first by the
    face's drawn area (``face_areas``), ties to the face id. ``paths``, ``areas`` and
    ``elements``, when given, are ``planar_map_paths(pm)``, ``face_areas(pm)`` and
    ``face_elements(pm)`` computed once for every layer. Returns the layer's stats."""
    import t3api

    if paths is None:
        paths = t3api.planar_map_paths(pm)
    if areas is None:
        areas = face_areas(pm)
    if elements is None:
        elements = face_elements(pm)
    name = palette_name(row)
    slug = layer_slug(name)
    layer = ET.SubElement(root, f"{{{SVG}}}g", {
        f"{{{INKSCAPE}}}groupmode": "layer", f"{{{INKSCAPE}}}label": f"{name} ({row['hex']})",
        "id": f"layer-{slug}", "fill": str(row["hex"]),
        "stroke": "none",
        "fill-rule": "evenodd"})
    faces = sorted((face for face in paths if face["label"] == int(row["index"])),
                   key=lambda face: (-areas[face["face"]], face["face"]))
    components = []
    layer_elements: dict[str, int] = {}
    node_types = {"c": 0, "s": 0}
    for number, face in enumerate(faces, 1):
        path_id = f"edge-{slug}-{number:03d}"
        ET.SubElement(layer, f"{{{SVG}}}path", {
            "id": path_id, f"{{{INKSCAPE}}}label": f"{name} {number:03d}",
            "d": face["d"], f"{{{SODIPODI}}}nodetypes": face["nodetypes"]})
        rings = face["d"].count("M")
        # t3 writes one letter per Inkscape node plus one closing letter per subpath.
        components.append({"id": path_id, "face": face["face"],
                           "nodes": len(face["nodetypes"]) - rings, "holes": rings - 1})
        for kind, count in elements[face["face"]].items():
            layer_elements[kind] = layer_elements.get(kind, 0) + count
        for letter in face["nodetypes"]:
            node_types[letter] = node_types.get(letter, 0) + 1
    return {"index": row["index"], "name": name, "hex": row["hex"],
            "paths": len(components), "nodes": sum(c["nodes"] for c in components),
            "holes": sum(c["holes"] for c in components), "construction": "t3 planar map",
            "elements": dict(sorted(layer_elements.items())), "node_types": node_types,
            "components": components}


# The stroke group's id, and the attributes t3api writes on each of its paths, in its order.
STROKE_GROUP_ID = "strokes"
STROKE_ATTRIBUTES = ("d", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin")


def add_stroke_group(root: ET.Element, pm: t3api.PlanarMap,
                     palette: Sequence[PaletteRow]) -> dict[str, int]:
    """Append the stroke group of the edge SVG, from a planar map read from ``map.json``, as
    ``root``'s next child: called after every face layer, it paints above them.

    The group is ``<g id="strokes" fill="none">``, not a layer: it holds one path per stroke
    in the map's order, then one per dot, each with the ``d``, ``stroke``, ``stroke-width``,
    ``stroke-linecap`` and ``stroke-linejoin`` (a dot has no join) exactly as
    ``t3api.strokes_svg_from_planar_map`` writes them (the stroke's pieces as one path, each
    later piece's moveto dropped and ``Z`` on a ring; a dot as ``M x y l0 0`` with a round
    cap). Each path gets a unique id, ``stroke-<colour>-NNN`` or ``dot-<colour>-NNN``
    numbered per colour in the map's order, and an Inkscape label. ``palette`` holds a row
    for every stroke's and dot's label (a removed colour's row too: its layer is left out of
    the cut, its strokes are not). A map with no stroke and no dot gets no group. Returns
    ``{"strokes", "dots"}``, the counts written."""
    import t3api

    strokes, dots = pm.get("strokes") or [], pm.get("dots") or []
    if not strokes and not dots:
        return {"strokes": 0, "dots": 0}
    written = ET.fromstring(t3api.strokes_svg_from_planar_map(pm, palette))
    groups = [g for g in written.findall(f"{{{SVG}}}g") if g.attrib.get("id") == STROKE_GROUP_ID]
    if len(groups) != 1:
        raise RuntimeError(f"t3api's strokes file holds {len(groups)} stroke groups, not one")
    paths = groups[0].findall(f"{{{SVG}}}path")
    labels = [int(row["label"]) for row in strokes] + [int(row["label"]) for row in dots]
    if len(paths) != len(labels):
        raise RuntimeError(f"t3api's stroke group holds {len(paths)} paths for "
                           f"{len(strokes)} strokes and {len(dots)} dots")
    rows = {int(row["index"]): row for row in palette}
    group = ET.SubElement(root, f"{{{SVG}}}g", {"id": STROKE_GROUP_ID, "fill": "none"})
    numbers: dict[tuple[str, int], int] = {}
    for k, (path, label) in enumerate(zip(paths, labels)):
        kind = "stroke" if k < len(strokes) else "dot"
        name = palette_name(rows[label])
        number = numbers[(kind, label)] = numbers.get((kind, label), 0) + 1
        attributes = {"id": f"{kind}-{layer_slug(name)}-{number:03d}"}
        attributes.update((key, path.attrib[key]) for key in STROKE_ATTRIBUTES if key in path.attrib)
        attributes[f"{{{INKSCAPE}}}label"] = f"{name} {kind} {number:03d}"
        ET.SubElement(group, f"{{{SVG}}}path", attributes)
    return {"strokes": len(strokes), "dots": len(dots)}


def write_svg(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    path.write_bytes(data + b"\n")

