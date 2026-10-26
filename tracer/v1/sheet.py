"""Truth | <label> | <label> ... for the master case at each size, zoomable, from
out/*.json, plus each label's 1024 px pixel difference map where one exists.
Every overlay is a layer that a sticky header toggles.

    python sheet.py r7 r9 r10      writes out/master-r7-r9-r10.html
"""

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import master_case_nodes as nodes_view  # noqa: E402
import tracer_contact_sheet as sheet  # noqa: E402
from vinylgen.fidelity.master import master_cases  # noqa: E402
from vinylgen.fidelity.raster import render_labels  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output
LABELS = tuple(sys.argv[1:]) if len(sys.argv) > 2 else ("r6", "r7")
TITLES = {"r6": "r6", "r7": "r7: midpoint samples, staircase floors",
          "r8": "r8: r7 plus pixel gates (0.5 px)",
          "r9": "r9: r7 plus 75% pixel gates (0.75 px)",
          "r10": "r10: pixel band as the fit acceptance",
          "r11": "r11: handles fitted to the band, not the samples",
          "r12": "r12: r11 plus tangents from the band",
          "r12AB": "r12 + A, B: search every piece, handles 0.02-2 chords",
          "r12ABC": "r12 + A, B, C: also allow pieces that turn back",
          "r13a": "r13a: r12 plus a cusp tried at every short-handle symmetric node",
          "r13b": "r13b: r12 with the corner rule judged against the band",
          "r13c": "r13c: r13b, and no corner where a visible smooth rounding fits the band"}
sheet.PANEL = 620
NODE_SIZE = 2.5       # traced node marker half-size in screen px
TRUTH_SIZE = 4.5      # authored node marker half-size in screen px
RING = 4.5 * 2.2      # required-vertex ring radius in screen px

# (class, header label, shown at load)
LAYERS = (
    ("source", "source pixels", True),
    ("trace", "traced fills", True),
    ("outline", "traced outline", True),
    ("band", "pixel band", True),
    ("grid", "pixel grid", True),
    ("border", "pixel border", True),
    ("truth", "true outline", True),
    ("nodes", "nodes", True),
    ("corners", "required vertices", True),
    ("kinks", "kinks", True),
    ("diff", "difference maps", True),
)


def layer(name: str, parts) -> str:
    return f'<g class="L-{name}">' + "".join(parts) + "</g>"


def ring(x, y, colour, k, scale=1.0):
    return sheet._pinned(x, y, f'<circle r="{RING * scale}" fill="none" stroke="{colour}" '
                               f'stroke-width="2"/>', k)


def node_marker(letter) -> str:
    """A traced node: its type's shape in its colour, no white ring."""
    style = (f'fill="{sheet.NODE_FILL}" stroke="{sheet.NODE_COLOURS[letter]}" stroke-width="1.25" '
             f'stroke-linejoin="miter"')
    return sheet._marker_shape(0.0, 0.0, letter, NODE_SIZE, 0.0, style)


def truth_marker(letter) -> str:
    """An authored node: its type's shape as a white outline."""
    style = 'fill="none" stroke="#fff" stroke-opacity="0.9" stroke-width="1.25" stroke-linejoin="miter"'
    return sheet._marker_shape(0.0, 0.0, letter, TRUTH_SIZE, 0.0, style)


def band_path(labels: np.ndarray) -> str:
    """The pixel band: the pixel borders between colours offset 0.5 px to each
    side, with mitred joins, so it reaches the centre of every pixel either side
    of a border and, at a direction change, grows to 0.5 / cos(turn / 2) (the
    diagonal pixel's centre at a 90-degree step). A traced edge inside it keeps
    every pixel centre on its own side."""
    import shapely
    from shapely.geometry import LineString, MultiLineString
    from shapely.ops import linemerge

    lines = []

    def runs(mask, emit):
        for i, row in enumerate(mask):
            edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
            for start, stop in zip(edges[::2], edges[1::2]):
                emit(i, int(start), int(stop))

    runs(labels[1:] != labels[:-1], lambda i, a, b: lines.append(((a, i + 1), (b, i + 1))))
    runs((labels[:, 1:] != labels[:, :-1]).T, lambda i, a, b: lines.append(((i + 1, a), (i + 1, b))))
    merged = linemerge(MultiLineString([LineString(line) for line in lines]))
    band = merged.buffer(0.5, cap_style="square", join_style="mitre", mitre_limit=2.0)
    parts = []
    for polygon in getattr(band, "geoms", [band]):
        for ring in (polygon.exterior, *polygon.interiors):
            xy = np.asarray(ring.coords)
            parts.append("M" + "L".join(f"{x:g} {y:g}" for x, y in xy[:-1]) + "Z")
    return "".join(parts)


def traced_layers(shapes, nodes, k):
    fills = [f'<path d="{sheet._d(subpaths)}" fill="{fill}" fill-rule="evenodd"/>'
             for fill, subpaths in shapes]
    outlines = [f'<path d="{sheet._d(subpaths)}" fill="none" stroke="{sheet.OWN_OUTLINE}" '
                f'stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
                for _, subpaths in shapes]
    markers = []
    for letter in "zsc":
        markers += [sheet._pinned(px, py, node_marker(t), k)
                    for (px, py), t in nodes.items() if t == letter]
    return fills, outlines, markers


rows = []
for case in master_cases():
    size = case.scene.width
    k = size / sheet.PANEL
    crop = sheet.Crop(case.case_id, 0, 0, size, "")
    labels = np.asarray(render_labels(case.scene))
    rgb = np.asarray([row["rgb"] for row in case.scene.palette], dtype=np.uint8)
    truth, true_nodes = [], []
    for region in case.scene.regions:
        for outline in region.rings:
            d, typed, _ = nodes_view._ring(outline)
            truth.append(sheet._truth_path(d))
            true_nodes += typed
    source = layer("source", [f'<image x="0" y="0" width="{size}" height="{size}" '
                              f'href="{sheet._png_data(Image.fromarray(rgb[labels]))}"/>'])
    grid = layer("grid", ['<path d="' + "".join(f"M0 {i}H{size}M{i} 0V{size}" for i in range(size + 1))
                          + '" fill="none" stroke="#808080" stroke-opacity="0.3" stroke-width="0.5" '
                          'vector-effect="non-scaling-stroke"/>'])
    border = layer("border", [f'<path d="{sheet._pixel_boundaries(rgb[labels], crop)}" fill="none" '
                              f'stroke="#ff00ff" stroke-width="1" vector-effect="non-scaling-stroke"/>'])
    band = layer("band", [f'<path d="{band_path(labels)}" fill="#202020" fill-opacity="0.5" '
                          f'fill-rule="evenodd"/>'])
    truth_layer = layer("truth", truth)
    true_markers = []
    for letter in "zsc":
        true_markers += [sheet._pinned(x, y, truth_marker(t), k)
                         for (x, y), t in true_nodes if t == letter]
    counts = {t: sum(1 for _, u in true_nodes if u == t) for t in "csz"}
    cells = [f'<div class="info"><h2>{case.case_id}</h2><div>{size} x {size} px</div></div>',
             sheet._panel("true vector", f"{len(true_nodes)} nodes: {counts['c']} cusp, "
                          f"{counts['s']} symmetric, {counts['z']} smooth", (0, 0, size),
                          [source, band, grid, border, truth_layer, layer("nodes", true_markers)])]
    for label in LABELS:
        data = json.loads((OUT / f"{label}-{size}.json").read_text())
        shapes, traced = sheet.ours_in_crop(Path(data["svg"]), crop)
        fills, outlines, markers = traced_layers(shapes, traced, k)
        req = [c for c in data["corners"] if c["role"] == "required"]
        corners = [ring(*c["point"], "#9a9a9a" if c["found"] else "#ff2020", k) for c in req]
        kinks = [ring(*kk["point"], "#ff9900", k, 3.0 / 2.2) for kk in data["kinks"]]
        n = data["nodes"]
        caption = (f"trace {data['trace_seconds']:.1f} s; {data['node_total']} nodes: {n['c']} cusp, "
                   f"{n['s']} symmetric, {n['z']} smooth; required vertices found "
                   f"{sum(c['found'] for c in req)}/{len(req)}; kinks {len(data['kinks'])}")
        body = [f'<rect x="0" y="0" width="{size}" height="{size}" fill="#fff"/>',
                layer("trace", fills), band, grid, border, layer("outline", outlines), truth_layer,
                layer("corners", corners), layer("kinks", kinks), layer("nodes", markers)]
        cells.append(sheet._panel(TITLES[label], caption, (0, 0, size), body))
    rows.append("<section>" + "".join(cells) + "</section>")
    diffs = [label for label in LABELS if (OUT / f"{label}-raster-{size}-diff.png").exists()]
    if diffs:
        cells = [f'<div class="info"><h2>{case.case_id}</h2><div>trace rasterised vs source</div></div>',
                 sheet._panel("source labels", f"{size} x {size} px", (0, 0, size),
                              [source, band, grid, border])]
        for label in diffs:
            stats = json.loads((OUT / f"{label}-raster-{size}.json").read_text())
            image = Image.open(OUT / f"{label}-raster-{size}-diff.png")
            cells.append(sheet._panel(
                f"{TITLES[label]}: pixel differences",
                f"{stats['mismatched_px']} px differ (red); orange: matches but under 75% "
                f"the right colour", (0, 0, size),
                [layer("diff", [f'<image x="0" y="0" width="{size}" height="{size}" '
                                f'href="{sheet._png_data(image)}"/>']), band, grid, border]))
        rows.append("<section>" + "".join(cells) + "</section>")

legend = sheet._legend(
    '<span><svg width="30" height="12"><path d="M0 9h10v-6h10v6h10" fill="none" stroke="#ff00ff" '
    'stroke-width="1.5"/></svg>pixel border of the traced label image</span>'
    '<span><svg width="30" height="12"><rect width="30" height="12" fill="#202020" '
    'fill-opacity="0.5" stroke="#888"/></svg>pixel band (pixel border offset 0.5 px each way, '
    'mitred at corners)</span>'
    '<span><svg width="24" height="24" viewBox="-12 -12 24 24"><circle r="10" fill="none" '
    'stroke="#9a9a9a" stroke-width="2"/></svg>required vertex, cusp found</span>'
    '<span><svg width="24" height="24" viewBox="-12 -12 24 24"><circle r="10" fill="none" '
    'stroke="#ff2020" stroke-width="2"/></svg>required vertex, missed</span>'
    '<span><svg width="24" height="24" viewBox="-12 -12 24 24"><circle r="10" fill="none" '
    'stroke="#ff9900" stroke-width="2"/></svg>kink (cusp on smooth truth)</span>')
intro = ["The master case traced by " + "; ".join(TITLES[label] for label in LABELS) + ", "
         "labels tier, default settings. All are judged by the working tree's bounds. "
         "Difference maps: the trace rasterised at 16 x 16 subsamples per pixel, majority colour, "
         "against the label image the tracer was given. "
         "Wheel to zoom, drag to pan; a row's panels move together."]
target = sheet._page(OUT / ("master-" + "-".join(LABELS) + ".html"),
                     "Master case: " + " vs ".join(LABELS), intro, legend, rows)

html = target.read_text()
css = (".layers { position: sticky; top: 0; z-index: 10; display: flex; flex-wrap: wrap; gap: 6px 18px; "
       "align-items: center; background: #26262c; border-bottom: 1px solid #555; margin: -16px -16px 12px; "
       "padding: 10px 16px; font-size: 14px; }\n"
       ".layers b { margin-right: 4px; }\n"
       ".layers label { display: flex; gap: 6px; align-items: center; cursor: pointer; }\n"
       ".layers label:hover { color: #fff; text-decoration: underline; }\n"
       + "".join(f"body.hide-{name} .L-{name} {{ display: none; }}\n" for name, _, _ in LAYERS))
header = ('<div class="layers"><b>Layers</b>'
          + "".join(f'<label><input type="checkbox" data-layer="{name}"{" checked" if on else ""}>'
                    f'{text}</label>' for name, text, on in LAYERS)
          + "</div>")
script = ("<script>\nfor (const box of document.querySelectorAll('.layers input')) {\n"
          "  const apply = () => document.body.classList.toggle('hide-' + box.dataset.layer, !box.checked);\n"
          "  box.addEventListener('change', apply);\n  apply();\n}\n</script>\n")
html = html.replace("</style>", css + "</style>", 1).replace("<body>", "<body>\n" + header, 1)
html = html.replace("</body>", script + "</body>", 1)
target.write_text(html)
print(target)
