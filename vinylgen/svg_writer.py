from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path


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


def layer_slug(name: str) -> str:
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


def face_areas(pm: dict) -> dict[int, float]:
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


def face_elements(pm: dict) -> dict[int, dict[str, int]]:
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


def add_face_layer(root: ET.Element, row: dict, pm: dict, *, paths: list[dict] | None = None,
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
    slug = layer_slug(row["name"])
    layer = ET.SubElement(root, f"{{{SVG}}}g", {
        f"{{{INKSCAPE}}}groupmode": "layer", f"{{{INKSCAPE}}}label": f"{row['name']} ({row['hex']})",
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
            "id": path_id, f"{{{INKSCAPE}}}label": f"{row['name']} {number:03d}",
            "d": face["d"], f"{{{SODIPODI}}}nodetypes": face["nodetypes"]})
        rings = face["d"].count("M")
        # t3 writes one letter per Inkscape node plus one closing letter per subpath.
        components.append({"id": path_id, "face": face["face"],
                           "nodes": len(face["nodetypes"]) - rings, "holes": rings - 1})
        for kind, count in elements[face["face"]].items():
            layer_elements[kind] = layer_elements.get(kind, 0) + count
        for letter in face["nodetypes"]:
            node_types[letter] = node_types.get(letter, 0) + 1
    return {"index": row["index"], "name": row["name"], "hex": row["hex"],
            "paths": len(components), "nodes": sum(c["nodes"] for c in components),
            "holes": sum(c["holes"] for c in components), "construction": "t3 planar map",
            "elements": dict(sorted(layer_elements.items())), "node_types": node_types,
            "components": components}


def write_svg(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    path.write_bytes(data + b"\n")

