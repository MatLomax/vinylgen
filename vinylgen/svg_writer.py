from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from vinylgen.contracts import TraceSettings

SVG = "http://www.w3.org/2000/svg"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
ET.register_namespace("", SVG)
ET.register_namespace("inkscape", INKSCAPE)


def svg_document(width: int, height: int, settings: TraceSettings) -> ET.Element:
    attrs = {"version": "1.1", "viewBox": f"0 0 {width} {height}"}
    if settings.width_mm is not None:
        attrs["width"] = f"{settings.width_mm:g}mm"
        attrs["height"] = f"{settings.height_mm:g}mm"
    else:
        attrs["width"] = str(width)
        attrs["height"] = str(height)
    return ET.Element(f"{{{SVG}}}svg", attrs)


def add_layer(root: ET.Element, row: dict, components: list[dict], transform: str,
              subject: str, variant: str, stroke_width: float) -> dict:
    slug = re.sub(r"[^a-z0-9]+", "-", str(row["name"]).casefold()).strip("-")
    layer = ET.SubElement(root, f"{{{SVG}}}g", {
        f"{{{INKSCAPE}}}groupmode": "layer", f"{{{INKSCAPE}}}label": f"{row['name']} ({row['hex']})",
        "id": f"layer-{slug}", "fill": str(row["hex"]),
        "stroke": "none",
        "fill-rule": "evenodd"})
    stats = []
    for number, component in enumerate(components, 1):
        path_id = f"{variant}-{slug}-{number:03d}"
        ET.SubElement(layer, f"{{{SVG}}}path", {"id": path_id,
                      f"{{{INKSCAPE}}}label": f"{row['name']} {number:03d}", "d": component["d"]})
        stats.append({"id": path_id, "nodes": component["nodes"],
                      "holes": component["holes"],
                      "fit_metrics": component["fit_metrics"]})
    metric_names = ("lines", "cubics", "hard_anchors", "source_samples",
                    "final_nodes", "node_reduction")
    fit_metrics = {
        name: sum(int(component["fit_metrics"][name]) for component in stats)
        for name in metric_names
    }
    fit_metrics["node_reduction_ratio"] = (
        fit_metrics["node_reduction"] / fit_metrics["source_samples"]
        if fit_metrics["source_samples"] else 0.0
    )
    return {"index": row["index"], "name": row["name"], "hex": row["hex"],
            "paths": len(stats), "nodes": sum(x["nodes"] for x in stats),
            "holes": sum(x["holes"] for x in stats), "fit_metrics": fit_metrics,
            "components": stats}


def write_svg(path: Path, root: ET.Element) -> None:
    ET.indent(root, space="  ")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    path.write_bytes(data + b"\n")

