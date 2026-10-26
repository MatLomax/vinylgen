"""Draw the master case's true vector as an editor would show it: every path as
Bézier segments, with each node typed (cusp, symmetric, smooth) and every handle
drawn, over the scene's label render. Zoomable like the contact sheets.

    .venv/bin/python scripts/master_case_nodes.py

Arcs become cubic pieces of at most 90 degrees each, so a full circle has four
smooth nodes. A straight segment has no handle, so its side of a node is
retracted: a line meeting an arc tangentially is a symmetric node, and a line
meeting anything at an angle is a cusp.

Writes .tmp/master/master-nodes.html.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import tracer_contact_sheet as sheet  # noqa: E402
from vinylgen.fidelity.master import master_cases  # noqa: E402
from vinylgen.fidelity.raster import render_labels  # noqa: E402
from vinylgen.fidelity.scene import Arc, Cubic, EllipticalArc, Line  # noqa: E402

OUT = ROOT / ".tmp" / "master"
PANEL = 900
CUSP_TURN_DEG = 0.5      # handles further than this from 180 degrees apart make a cusp
BALANCED = 1e-3          # relative handle-length difference still counted as equal
HANDLE = "#c8c8c8"


def _conic_pieces(point, derivative, start: float, sweep: float) -> list[tuple]:
    """Cubic pieces of a conic arc ``point(a)`` for ``a`` from ``start`` through
    ``start + sweep``, at most 90 degrees of parameter each, with the standard
    tangent-length factor ``4/3 tan(theta/4)`` (exact for affine images of a circle)."""
    count = max(1, math.ceil(abs(sweep) / (math.pi / 2.0) - 1e-9))
    theta = sweep / count
    k = 4.0 / 3.0 * math.tan(theta / 4.0)
    pieces = []
    for i in range(count):
        a0, a1 = start + i * theta, start + (i + 1) * theta
        p0, p3 = np.asarray(point(a0)), np.asarray(point(a1))
        pieces.append((p0, p0 + k * np.asarray(derivative(a0)), p3 - k * np.asarray(derivative(a1)), p3))
    return pieces


def _cubics(segment) -> list[tuple]:
    """``segment`` as ``(p0, c1, c2, p3)`` pieces; a line keeps its handles on its
    end points (retracted)."""
    if isinstance(segment, Line):
        p0, p1 = np.asarray(segment.p0), np.asarray(segment.p1)
        return [(p0, p0, p1, p1)]
    if isinstance(segment, Cubic):
        return [tuple(np.asarray(v) for v in (segment.p0, segment.c1, segment.c2, segment.p1))]
    if isinstance(segment, Arc):
        cx, cy, r = segment.centre[0], segment.centre[1], segment.radius
        return _conic_pieces(lambda a: (cx + r * math.cos(a), cy + r * math.sin(a)),
                             lambda a: (-r * math.sin(a), r * math.cos(a)),
                             segment.start, segment.sweep)
    if isinstance(segment, EllipticalArc):
        cx, cy = segment.centre
        c, s = math.cos(segment.rotation), math.sin(segment.rotation)
        rx, ry = segment.rx, segment.ry

        def point(a):
            u, v = rx * math.cos(a), ry * math.sin(a)
            return (cx + c * u - s * v, cy + s * u + c * v)

        def derivative(a):
            u, v = -rx * math.sin(a), ry * math.cos(a)
            return (c * u - s * v, s * u + c * v)

        return _conic_pieces(point, derivative, segment.start, segment.sweep)
    raise TypeError(f"cannot draw a {segment.kind} segment")


def _node_type(incoming, outgoing, before: np.ndarray, after: np.ndarray) -> str:
    """The node type from its two handles (vectors from the node; a zero vector is
    a retracted handle, whose direction is its straight segment's)."""
    a = float(np.hypot(*incoming))
    b = float(np.hypot(*outgoing))
    din = incoming if a > 1e-9 else before
    dout = outgoing if b > 1e-9 else after
    cos = float(np.dot(din, dout) / (np.hypot(*din) * np.hypot(*dout)))
    if math.degrees(math.acos(max(-1.0, min(1.0, -cos)))) > CUSP_TURN_DEG:
        return "c"
    return "z" if abs(a - b) <= BALANCED * max(a, b) else "s"


def _ring(outline) -> tuple[str, list, list]:
    """One ring's path data, its typed nodes and its handle lines."""
    pieces = [piece for segment in outline.segments for piece in _cubics(segment)]
    d = f"M{pieces[0][0][0]:.4f} {pieces[0][0][1]:.4f}" + "".join(
        f"C{c1[0]:.4f} {c1[1]:.4f} {c2[0]:.4f} {c2[1]:.4f} {p3[0]:.4f} {p3[1]:.4f}"
        for _, c1, c2, p3 in pieces) + "Z"
    nodes, handles = [], []
    for i, (p0, c1, _, p3) in enumerate(pieces):
        prev = pieces[i - 1]
        incoming, outgoing = prev[2] - p0, c1 - p0
        # Directions along the curve into and out of the node, for retracted handles.
        before = prev[0] - p0 if np.allclose(prev[2], p0) else incoming
        after = p3 - p0 if np.allclose(c1, p0) else outgoing
        nodes.append((p0, _node_type(incoming, outgoing, before, after)))
        for handle in (prev[2], c1):
            if not np.allclose(handle, p0):
                handles.append((p0, handle))
    return d, nodes, handles


def _png(case) -> str:
    labels = np.asarray(render_labels(case.scene))
    palette = np.asarray([row["rgb"] for row in case.scene.palette], dtype=np.uint8)
    faded = (palette[labels].astype(np.float32) * 0.45).astype(np.uint8)
    return sheet._png_data(Image.fromarray(faded))


def main() -> None:
    sheet.PANEL = PANEL
    OUT.mkdir(parents=True, exist_ok=True)
    case = master_cases()[0]
    size = case.scene.width
    k = size / PANEL
    body = [f'<image href="{_png(case)}" x="0" y="0" width="{size}" height="{size}"/>']
    counts = {"c": 0, "s": 0, "z": 0}
    markers = {"c": [], "s": [], "z": []}
    handle_lines, handle_dots = [], []
    for region in case.scene.regions:
        for outline in region.rings:
            d, nodes, handles = _ring(outline)
            body.append(sheet._truth_path(d))
            for (x0, y0), (x1, y1) in handles:
                handle_lines.append(f'<line x1="{x0:.4f}" y1="{y0:.4f}" x2="{x1:.4f}" y2="{y1:.4f}" '
                                    f'stroke="{HANDLE}" stroke-width="1" '
                                    f'vector-effect="non-scaling-stroke"/>')
                handle_dots.append(sheet._pinned(x1, y1, f'<circle r="2.5" fill="{HANDLE}"/>', k))
            for (x, y), letter in nodes:
                counts[letter] += 1
                markers[letter].append(sheet._pinned(x, y, sheet._marker(0, 0, letter, sheet.MARKER,
                                                                         sheet.NODE_LINE), k))
    body += handle_lines + handle_dots + markers["z"] + markers["s"] + markers["c"]
    caption = (f"{counts['c']} cusp, {counts['s']} symmetric, {counts['z']} smooth "
               f"({sum(counts.values())} nodes)")
    row = ('<section>' + sheet._panel(f"{case.case_id}: true vector", caption, (0, 0, size), body)
           + '</section>')
    legend = sheet._legend(f'<span><svg width="30" height="12"><line x1="0" y1="6" x2="24" y2="6" '
                           f'stroke="{HANDLE}"/><circle cx="26" cy="6" r="2.5" fill="{HANDLE}"/>'
                           f'</svg>handle</span>')
    intro = ["The master case's true vector (1024 px), drawn as Bézier nodes and handles over its "
             "faded label render. Arcs are cubic pieces of at most 90 degrees; a straight "
             "segment's handle is retracted, so a line meeting an arc tangentially is a "
             "symmetric node and a line meeting anything at an angle is a cusp."]
    path = sheet._page(OUT / "master-nodes.html", "Master case: true vector", intro, legend, [row])
    print(path, caption)


if __name__ == "__main__":
    main()
