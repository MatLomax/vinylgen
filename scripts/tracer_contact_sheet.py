#!/usr/bin/env python3
"""Contact sheet of our tracer, Potrace and VTracer on real subjects, glyphs and fuzz scenes.

Each subject's stored flattened labels (``segmentation.npz``) are copied into
``.tmp/contact-sheet/work/<subject>/`` and traced whole by all three tools:

- ours: ``vinylgen.tracing.trace_subject`` with default ``TraceSettings``, pixel
  mode, every colour cut (no omitted colour), reading ``traced-edge.svg``, on a
  copy of the segmentation reduced to its labels (hard-edged Lab costs, no
  protected features), so it gets no source evidence the other two lack;
- Potrace: ``benchmark_tracers.run_potrace`` (stacked scans per colour);
- VTracer: ``benchmark_tracers.run_vtracer`` (the flattened RGB, stacked colour
  mode).

Whole-image traces, not traced crops: our trace lengths are thousandths of the
traced image's longer side and our layer order comes from the whole image, so a
cropped input would trace with different settings than the real subject, and a
crop edge would add frame boundaries none of the tools see in real use.

Each crop window is then drawn as one row: the flattened raster, then each
tool's filled shapes at the same scale, outlined in red, under the label
boundaries in cyan (the truth all three traced), with every node marked by type
as the Trace step's node overlay marks it (cusp a red rhombus, symmetric a blue
square, smooth a yellow circle; partly transparent outlines with a white ring).
Our node types are our SVG's ``sodipodi:nodetypes``; Potrace's and VTracer's are
read from their handles by ``benchmark_tracers.stacked_trace`` over the visible
partition of their stacked shapes (plus the cusps where stacking clips one shape
against another). A node shared by two colour layers is counted once.

``--corpus icons`` draws the icon corpus instead (``build_icons``): one row per
glyph, whole, as the benchmark renders it, with its label render under the true
outline and every authored node (white outline in its type's shape), then the
three tools traced from those labels (Potrace and VTracer at the benchmark grid's
tuned settings) under the true outline, each panel captioned with that glyph's
benchmark figures. The glyphs are picked from a tuned icon benchmark run
(``_icon_rows``). Output: ``.tmp/contact-sheet/icons.html``.

After the real subjects the main sheet mixes in a few icon glyphs and pinned fuzz
scenes (``MIXED_ICONS``, ``MIXED_FUZZ``), drawn as the icon sheet draws a glyph
(``_scene_row``), with figures judged afresh by the benchmark (``_judged``).

The sheet is an HTML page for Firefox: every panel zooms (wheel) and pans
(drag), and the panels of a row move together. Output:
``.tmp/contact-sheet/contact-sheet.html``.
Traces are cached in the work directory; ``--retrace`` redoes them.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import pickle
import shutil
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_tracers import run_one, run_potrace, run_vtracer, stacked_trace  # noqa: E402

OUT = ROOT / ".tmp" / "contact-sheet"
WORK = OUT / "work"
DATA = ROOT / ".vinylgen-data"
SVG_NS = "http://www.w3.org/2000/svg"
SODIPODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
TOOLS = ("ours", "potrace", "vtracer")
TOOL_TITLES = {"ours": "Vinylgen", "potrace": "Potrace", "vtracer": "VTracer"}
# The Trace step's node overlay (web/src/PathNodes.tsx): each type's outline colour,
# the opacity of that outline and of the white ring just outside it, the faint
# fill, the marker half-size and the line width, in sheet px as the overlay's CSS px.
NODE_COLOURS = {"c": "#ff6b93", "s": "#6ccdff", "z": "#fff266"}
NODE_BORDER_OPACITY = 0.8
NODE_FILL = "rgba(255, 255, 255, .1)"
NODE_LINE = 1.25
NODE_NAMES = {"c": "cusp", "s": "symmetric", "z": "smooth"}

PANEL = 540          # panel side in screen px
LOCATOR = 220        # locator thumbnail width
MARKER = 4.5         # marker half-size in screen px (the overlay's NODE_MARKER_RADIUS)
TRUTH_MARKER = 8.0   # authored-node marker half-size in screen px
TRUTH_LINE = 1.5
TRUTH_OUTLINE = "#00e5ff"
OWN_OUTLINE = "#ff2d2d"


@dataclass(frozen=True)
class Crop:
    name: str
    x: int
    y: int
    size: int
    why: str


@dataclass(frozen=True)
class Subject:
    name: str
    title: str
    origin: str          # "outputs:<subject>" or "web:<project>:<revision id>"
    crops: tuple[Crop, ...]


SUBJECTS = (
    Subject("fox-tail", "fox-owl-table-2 (web project), 6398 x 2133",
            "web:fox-owl-table-2:ca8e208a8e3b4f04ab19880e77692da6", (
                Crop("tail zigzag", 1240, 1440, 460,
                     "orange/cream zigzag across the tail, black outline, three-colour junctions"),
                Crop("zigzag close-up", 1380, 1560, 220,
                     "the zigzag teeth up close: which tips become cusps"),
                Crop("toadstool", 560, 1150, 420,
                     "round spots (smooth curves) and cap/stem junctions"),
            )),
    Subject("sourceup", "sourceup (outputs/), 5980 x 4208", "outputs:sourceup", (
        Crop("griffin tail tuft", 760, 2000, 460,
             "flame-shaped tuft: sharp tips between organic curves"),
        Crop("battlements", 1260, 1960, 520,
             "castle crenellations and banner: hard right-angle corners"),
        Crop("beak and eye", 2540, 330, 480,
             "curved beak with a sharp tip, eye, feather zigzags"),
    )),
    Subject("desert-mech", "desert-mech (outputs/), 1495 x 1052", "outputs:desert-mech", (
        Crop("mech torso", 560, 330, 300,
             "angular armour plates, eye boxes and stripe: hard corners"),
        Crop("dust and foot", 400, 750, 300,
             "billowing dust clouds (bumps meeting in cusps) against angular rocks"),
        Crop("cloud and birds", 1030, 300, 240,
             "small birds: acute corners at a few pixels"),
    )),
)


# --- inputs ------------------------------------------------------------------------

def _web_artifacts(project: str, revision: str) -> dict[str, Path]:
    """The effective artifacts of a web revision (newest of each kind along its
    ancestry), read-only."""
    db = sqlite3.connect(f"file:{DATA / 'vinylgen.sqlite3'}?mode=ro", uri=True)
    found: dict[str, Path] = {}
    current = revision
    while current:
        row = db.execute("select parent_revision_id from revisions r join projects p "
                         "on r.project_id = p.id where r.id = ? and p.slug = ?",
                         (current, project)).fetchone()
        if row is None:
            raise SystemExit(f"revision {current} of {project} not found")
        for kind, path in db.execute("select kind, storage_path from artifacts where revision_id = ?",
                                     (current,)):
            found.setdefault(kind, DATA / path)
        current = row[0]
    return found


def prepare(subject: Subject) -> Path:
    """A private copy of the subject's flattened inputs to trace."""
    target = WORK / subject.name / "labels-only"
    if (target / "segmentation.npz").exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    kind, *rest = subject.origin.split(":")
    if kind == "outputs":
        source = ROOT / "outputs" / rest[0]
        files = {name: source / name for name in
                 ("segmentation.npz", "flattened.png", "palette.json", "source.png")}
    else:
        artifacts = _web_artifacts(*rest)
        files = {"segmentation.npz": artifacts["segmentation"],
                 "flattened.png": artifacts["flattened"],
                 "palette.json": artifacts["palette"], "source.png": artifacts["source"]}
    # The segmentation is written last, already reduced to its labels: its presence
    # marks a finished copy, so an interrupted run never leaves one with the
    # source's evidence behind.
    for name, path in files.items():
        if name != "segmentation.npz":
            shutil.copyfile(path, target / name)
    partial = target / "segmentation.partial.npz"
    shutil.copyfile(files["segmentation.npz"], partial)
    _flatten_evidence(partial)
    partial.replace(target / "segmentation.npz")
    return target


def _flatten_evidence(path: Path) -> None:
    """Rewrite a copied segmentation to hold only its labels, as Potrace and
    VTracer receive them: Lab costs of the flattened palette RGB (every boundary
    crossing at the pixel-edge midpoint, as ``harness.trace_labels`` stores for a
    hard-edged label map) and no protected features, so the source's sub-pixel
    edge evidence gives ours nothing the other two lack."""
    from vinylgen.colour import palette_costs, srgb_to_lab
    from vinylgen.contracts import SegmentationBundle
    bundle = SegmentationBundle.load(path, with_costs=False)
    labels = np.asarray(bundle.labels)
    palette_rgb = np.asarray(bundle.palette_rgb, dtype=np.uint8)
    height, width = labels.shape
    costs = palette_costs(srgb_to_lab(palette_rgb[labels].reshape(-1, 3)), srgb_to_lab(palette_rgb))
    flat = SegmentationBundle(
        labels=labels, protected_features=np.zeros(labels.shape, dtype=bool),
        explicit_protected_features=np.zeros(labels.shape, dtype=bool),
        palette_rgb=palette_rgb, palette_costs=costs.reshape((height, width, len(palette_rgb))))
    flat.validate()
    flat.save(path)


def trace_all(subject: Subject, retrace: bool) -> dict:
    from vinylgen.contracts import SegmentationBundle, TraceSettings
    from vinylgen.tracing import trace_subject
    subject_dir = prepare(subject)
    work = WORK / subject.name
    if retrace or not (subject_dir / "traced-edge.svg").exists():
        trace_subject(subject_dir, TraceSettings(), {})
    palette = json.loads((subject_dir / "palette.json").read_text())["palette"]
    fills = [str(row["hex"]).upper() for row in palette]
    rgb = np.asarray([row["rgb"] for row in palette], dtype=np.uint8)
    labels = None
    shapes = {}
    for tool in ("potrace", "vtracer"):
        cache = work / f"{tool}.pkl"
        if retrace or not cache.exists():
            if labels is None:
                labels = np.asarray(SegmentationBundle.load(
                    subject_dir / "segmentation.npz", with_costs=False).labels)
            result = (run_potrace(labels, fills, work / tool) if tool == "potrace"
                      else run_vtracer(labels, fills, rgb, work / tool))
            cache.write_bytes(pickle.dumps(result))
        shapes[tool] = pickle.loads(cache.read_bytes())
    return {"dir": subject_dir, "fills": fills, "rgb": rgb, "shapes": shapes}


# --- geometry per crop -------------------------------------------------------------

def _bbox(start, segments):
    points = [start] + [p for segment in segments for p in segment[1:]]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _hits(box, crop: Crop, margin: float = 2.0) -> bool:
    x0, y0, x1, y1 = box
    return not (x1 < crop.x - margin or y1 < crop.y - margin
                or x0 > crop.x + crop.size + margin or y0 > crop.y + crop.size + margin)


def _inside(x, y, crop: Crop) -> bool:
    return crop.x <= x <= crop.x + crop.size and crop.y <= y <= crop.y + crop.size


def _d(subpaths) -> str:
    parts = []
    for start, segments, closed in subpaths:
        parts.append(f"M{start[0]:.3f} {start[1]:.3f}")
        for segment in segments:
            if segment[0] == "L":
                parts.append(f"L{segment[1][0]:.3f} {segment[1][1]:.3f}")
            else:
                parts.append("C" + " ".join(f"{p[0]:.3f} {p[1]:.3f}" for p in segment[1:]))
        if closed:
            parts.append("Z")
    return "".join(parts)


def _unique(nodes, crop: Crop) -> dict:
    """Nodes inside the crop keyed by position; a position typed differently in two
    layers counts as a cusp."""
    kept: dict = {}
    for x, y, letter in nodes:
        if not _inside(x, y, crop):
            continue
        key = (round(x, 4), round(y, 4))
        kept[key] = "c" if kept.get(key, letter) != letter else letter
    return kept


def ours_in_crop(svg_path: Path, crop: Crop):
    """Our edge SVG's paths touching the crop (``(fill, subpaths)``) and our typed
    nodes, from ``sodipodi:nodetypes``."""
    from vinylgen.node_types import parse_path
    root = ET.parse(svg_path).getroot()
    shapes, nodes = [], []
    for group in root.iter(f"{{{SVG_NS}}}g"):
        if group.attrib.get(f"{{{INKSCAPE}}}groupmode") != "layer":
            continue
        fill = group.attrib["fill"].upper()
        for element in group.iter(f"{{{SVG_NS}}}path"):
            letters = element.attrib.get(f"{{{SODIPODI}}}nodetypes", "")
            offset, kept = 0, []
            for start, segments, closed in parse_path(element.attrib["d"]):
                segs = list(segments)
                if closed and segs and segs[-1][-1] != start:
                    segs.append(("L", start))
                if not segs:
                    continue
                count = len(segs) + 1
                types = letters[offset:offset + count]
                offset += count
                if not _hits(_bbox(start, segs), crop):
                    continue
                kept.append((start, segments, closed))
                ends = [segment[-1] for segment in segs]
                positions = [start, *ends[:-1]] if closed else [start, *ends]
                nodes.extend((float(x), float(y), t) for (x, y), t in zip(positions, types))
            if offset != len(letters):
                raise ValueError(f"{element.attrib.get('id')}: {len(letters)} node types "
                                 f"for {offset} slots")
            if kept:
                shapes.append((fill, kept))
    return shapes, _unique(nodes, crop)


def tool_in_crop(shapes, crop: Crop, width: int, height: int):
    """A tool's stacked shapes touching the crop and the typed nodes of their
    visible partition. Subpaths wholly outside the crop are dropped first; under the
    even-odd rule they cannot change what is visible inside it."""
    kept = []
    for fill, subpaths in shapes:
        near = [sub for sub in subpaths if sub[1] and _hits(_bbox(sub[0], sub[1]), crop)]
        if near:
            kept.append((fill, near))
    traced = stacked_trace(kept, width, height)
    nodes = [node for layer in traced["layers"] for path in layer["paths"]
             for node in path["nodes"]]
    return kept, _unique(nodes, crop)


# --- drawing -----------------------------------------------------------------------
#
# The sheet is an HTML page. Each panel is an inline SVG in source-pixel
# coordinates whose viewBox the page's script zooms (wheel) and pans (drag); every
# panel of a row shares one view. Markers are drawn in screen pixels inside a
# ``<g class="k">`` that the script rescales to the view, so they keep their size
# on screen at any zoom, and outlines use non-scaling strokes.

def _png_data(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _marker_shape(x, y, letter, r, grow, style) -> str:
    """A node type's marker centred on (x, y), grown outward by ``grow`` on every
    side: cusp a rhombus, symmetric a square, smooth a circle."""
    if letter == "c":
        k = r * 1.25 + grow * math.sqrt(2.0)
        return (f'<polygon points="{x:.3f},{y - k:.3f} {x + k:.3f},{y:.3f} '
                f'{x:.3f},{y + k:.3f} {x - k:.3f},{y:.3f}" {style}/>')
    if letter == "s":
        h = r * 0.85 + grow
        return f'<rect x="{x - h:.3f}" y="{y - h:.3f}" width="{2 * h:.3f}" height="{2 * h:.3f}" {style}/>'
    return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r + grow:.3f}" {style}/>'


def _marker(x, y, letter, r, line) -> str:
    """A node marker styled as the Trace step's overlay draws it: a white ring just
    outside the outline, then the faint fill and the type's coloured outline, both
    rings partly transparent."""
    ring = (f'fill="none" stroke="#fff" stroke-opacity="{NODE_BORDER_OPACITY}" '
            f'stroke-width="{line:.4f}" stroke-linejoin="miter"')
    body = (f'fill="{NODE_FILL}" stroke="{NODE_COLOURS[letter]}" '
            f'stroke-opacity="{NODE_BORDER_OPACITY}" stroke-width="{line:.4f}" stroke-linejoin="miter"')
    return _marker_shape(x, y, letter, r, line, ring) + _marker_shape(x, y, letter, r, 0.0, body)


def _truth_marker(letter) -> str:
    """An authored node: its type's shape, larger than a traced node's marker, as a
    white outline only."""
    style = (f'fill="none" stroke="#fff" stroke-opacity="0.9" stroke-width="{TRUTH_LINE}" '
             f'stroke-linejoin="miter"')
    return _marker_shape(0.0, 0.0, letter, TRUTH_MARKER, 0.0, style)


def _pinned(x, y, drawing: str, k: float) -> str:
    """``drawing`` (in screen pixels about the origin) pinned to source point (x, y)
    at the view's screen scale ``k`` (source pixels per screen pixel)."""
    return (f'<g transform="translate({x:.3f} {y:.3f})"><g class="k" transform="scale({k:.6f})">'
            f'{drawing}</g></g>')


def _outlined(fill, d) -> str:
    """A tool's filled shape with its own outline in red. Stroked after its own fill
    and before the shapes above it, so only the visible part of a stacked outline
    shows."""
    return (f'<path d="{d}" fill="{fill}" fill-rule="evenodd" stroke="{OWN_OUTLINE}" '
            f'stroke-width="1.5" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>')


def _truth_path(d) -> str:
    return (f'<path d="{d}" fill="none" stroke="{TRUTH_OUTLINE}" stroke-width="1" '
            f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>')


def _pixel_boundaries(pixels: np.ndarray, crop: Crop) -> str:
    """Path data of the label boundaries inside the crop: the pixel edges between
    differently coloured pixels, as horizontal and vertical runs."""
    x0, y0 = crop.x, crop.y
    window = pixels[max(0, y0 - 1):y0 + crop.size + 1, max(0, x0 - 1):x0 + crop.size + 1]
    ox, oy = max(0, x0 - 1), max(0, y0 - 1)
    code = window[..., 0].astype(np.int64) << 16 | window[..., 1].astype(np.int64) << 8 | window[..., 2]
    parts = []

    def runs(mask, emit):
        for i, row in enumerate(mask):
            edges = np.flatnonzero(np.diff(np.concatenate(([0], row.astype(np.int8), [0]))))
            for start, stop in zip(edges[::2], edges[1::2]):
                emit(i, start, stop)

    # Horizontal edges lie between rows i and i + 1, vertical ones between columns.
    runs(code[1:] != code[:-1],
         lambda i, a, b: parts.append(f"M{ox + a} {oy + i + 1}h{b - a}"))
    runs((code[:, 1:] != code[:, :-1]).T,
         lambda i, a, b: parts.append(f"M{ox + i + 1} {oy + a}v{b - a}"))
    return "".join(parts)


def _authored_typed_nodes(scene, rings) -> dict:
    """The scene's authored nodes on the drawn truth boundary, typed from the
    segments meeting there as the tools' nodes are typed from their handles
    (``benchmark_tracers.typed_nodes``): a cusp turns more than CUSP_TURN_DEG, a
    smooth node has equal tangent magnitudes either side, otherwise symmetric. A
    position typed differently by two outlines counts as a cusp."""
    from benchmark_tracers import BALANCED, CUSP_TURN_DEG
    from vinylgen.fidelity.metrics import authored_nodes
    from vinylgen.fidelity.scene import segment_start, turn_degrees
    visible = {(round(x, 4), round(y, 4)) for x, y in authored_nodes(scene, rings)}
    typed: dict = {}
    for region in scene.regions:
        for outline in region.rings:
            for index, segment in enumerate(outline.segments):
                x, y = segment_start(segment)
                key = (round(x, 4), round(y, 4))
                if key not in visible:
                    continue
                incoming = outline.segments[index - 1].end_tangent()
                outgoing = segment.start_tangent()
                if turn_degrees(incoming, outgoing) > CUSP_TURN_DEG:
                    letter = "c"
                else:
                    a, b = float(np.hypot(*incoming)), float(np.hypot(*outgoing))
                    letter = "z" if abs(a - b) <= BALANCED * max(a, b) else "s"
                typed[key] = "c" if typed.get(key, letter) != letter else letter
    return typed


def _count_text(nodes: dict) -> tuple[str, dict]:
    counts = {letter: sum(1 for t in nodes.values() if t == letter) for letter in "csz"}
    return (f"{len(nodes)} nodes: {counts['c']} cusp, {counts['s']} symmetric, "
            f"{counts['z']} smooth"), counts


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _panel(title: str, caption: str, view: tuple[float, float, float], body: list[str]) -> str:
    """One zoomable panel: ``body`` in source-pixel coordinates, first shown as the
    square ``view`` (x, y, side)."""
    x, y, side = view
    box = f"{x} {y} {side} {side}"
    return (f'<figure><figcaption><b>{_esc(title)}</b><span>{_esc(caption)}</span></figcaption>'
            f'<svg class="pz" viewBox="{box}" data-home="{box}" width="{PANEL}" height="{PANEL}" '
            f'xmlns="{SVG_NS}">' + "".join(body) + "</svg></figure>")


def _legend(extra: str = "") -> str:
    items = []
    for letter in "csz":
        items.append(f'<span><svg width="24" height="24" viewBox="-12 -12 24 24">'
                     f'{_marker(0, 0, letter, 7, 1.8)}</svg>{NODE_NAMES[letter]} node</span>')
    items.append(f'<span><svg width="30" height="12"><line x1="0" y1="6" x2="30" y2="6" '
                 f'stroke="{TRUTH_OUTLINE}" stroke-width="2"/></svg>true outline</span>')
    items.append(f'<span><svg width="30" height="12"><line x1="0" y1="6" x2="30" y2="6" '
                 f'stroke="{OWN_OUTLINE}" stroke-width="2"/></svg>the tool\'s own outline</span>')
    return '<div class="legend">' + "".join(items) + extra + "</div>"


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title><style>
body {{ background: #1b1b1f; color: #eee; font: 14px "DejaVu Sans", sans-serif; margin: 16px; }}
h1 {{ font-size: 24px; margin: 0 0 8px; }}
p {{ color: #ccc; font-size: 13px; max-width: 1800px; margin: 4px 0; }}
.legend {{ display: flex; gap: 28px; align-items: center; margin: 12px 0 18px; font-size: 15px; }}
.legend span {{ display: flex; gap: 8px; align-items: center; }}
section {{ display: flex; gap: 14px; margin-bottom: 26px; align-items: flex-start; }}
.info {{ width: 220px; flex: none; }}
.info h2 {{ font-size: 16px; margin: 0 0 6px; word-break: break-all; }}
.info div {{ color: #bbb; font-size: 13px; margin-top: 6px; }}
.info img {{ display: block; margin-top: 6px; }}
figure {{ margin: 0; }}
figcaption {{ height: 40px; font-size: 14px; }}
figcaption span {{ display: block; color: #ccc; font-size: 13px; margin-top: 3px; }}
svg.pz {{ display: block; outline: 1px solid #888; background: #111; cursor: grab; }}
svg.pz:active {{ cursor: grabbing; }}
svg.pz image {{ image-rendering: pixelated; }}
</style></head><body>
<h1>{title}</h1>
{intro}
<p>Scroll a panel to zoom, drag to pan, double-click to reset; every panel in a row
follows. Node markers keep their screen size at any zoom.</p>
{legend}
{rows}
<script>
const PANEL = {panel};
for (const row of document.querySelectorAll("section")) {{
  const panels = [...row.querySelectorAll("svg.pz")];
  if (!panels.length) continue;
  const home = panels[0].dataset.home.split(" ").map(Number);
  const view = {{x: home[0], y: home[1], s: home[2]}};
  const minSide = Math.min(home[2], 2);
  const markers = panels.flatMap(p => [...p.querySelectorAll("g.k")]);
  function show() {{
    view.s = Math.min(home[2], Math.max(minSide, view.s));
    view.x = Math.min(home[0] + home[2] - view.s, Math.max(home[0], view.x));
    view.y = Math.min(home[1] + home[2] - view.s, Math.max(home[1], view.y));
    const box = `${{view.x}} ${{view.y}} ${{view.s}} ${{view.s}}`;
    for (const p of panels) p.setAttribute("viewBox", box);
    const k = `scale(${{view.s / PANEL}})`;
    for (const m of markers) m.setAttribute("transform", k);
  }}
  for (const p of panels) {{
    p.addEventListener("wheel", e => {{
      e.preventDefault();
      const r = p.getBoundingClientRect();
      const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
      const px = view.x + fx * view.s, py = view.y + fy * view.s;
      view.s *= Math.exp(e.deltaY * (e.deltaMode ? 0.05 : 0.0015));
      view.s = Math.min(home[2], Math.max(minSide, view.s));
      view.x = px - fx * view.s; view.y = py - fy * view.s;
      show();
    }}, {{passive: false}});
    p.addEventListener("pointerdown", e => {{
      p.setPointerCapture(e.pointerId);
      const start = {{cx: e.clientX, cy: e.clientY, x: view.x, y: view.y}};
      const move = m => {{
        const per = view.s / p.getBoundingClientRect().width;
        view.x = start.x - (m.clientX - start.cx) * per;
        view.y = start.y - (m.clientY - start.cy) * per;
        show();
      }};
      const up = () => {{ p.removeEventListener("pointermove", move); p.removeEventListener("pointerup", up); }};
      p.addEventListener("pointermove", move);
      p.addEventListener("pointerup", up);
    }});
    p.addEventListener("dblclick", () => {{ view.x = home[0]; view.y = home[1]; view.s = home[2]; show(); }});
  }}
}}
</script>
</body></html>
"""


def _page(target: Path, title: str, intro: list[str], legend: str, rows: list[str]) -> Path:
    target.write_text(_PAGE.format(title=_esc(title), intro="".join(f"<p>{_esc(t)}</p>" for t in intro),
                                   legend=legend, rows="\n".join(rows), panel=PANEL))
    return target


def _tool_body(shapes, nodes, truth: list[str], k: float, under: list[str] = (),
               over: list[str] = ()) -> list[str]:
    """A tool panel: its shapes with their own outlines, the true outline over them,
    then ``over`` and its node markers (cusps on top)."""
    body = list(under)
    body += [_outlined(fill, _d(subpaths)) for fill, subpaths in shapes]
    body += truth
    body += list(over)
    for letter in "zsc":
        body += [_pinned(px, py, _marker(0, 0, t, MARKER, NODE_LINE), k)
                 for (px, py), t in nodes.items() if t == letter]
    return body


def build(retrace: bool) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    rows, report = [], []
    for subject in SUBJECTS:
        data = trace_all(subject, retrace)
        flattened = Image.open(data["dir"] / "flattened.png").convert("RGB")
        pixels = np.asarray(flattened)
        width, height = flattened.size
        thumb = flattened.copy()
        thumb.thumbnail((LOCATOR, LOCATOR))
        factor = thumb.width / width
        for crop in subject.crops:
            k = crop.size / PANEL
            view = (crop.x, crop.y, crop.size)
            box = max(3, crop.size * factor)
            locator = (f'<svg width="{thumb.width}" height="{thumb.height}" xmlns="{SVG_NS}">'
                       f'<image href="{_png_data(thumb)}" width="{thumb.width}" height="{thumb.height}"/>'
                       f'<rect x="{crop.x * factor:.1f}" y="{crop.y * factor:.1f}" width="{box:.1f}" '
                       f'height="{box:.1f}" fill="none" stroke="#FF1FD2" stroke-width="2"/></svg>')
            cells = [f'<div class="info"><h2>{_esc(subject.name)}</h2>{locator}'
                     f'<div><b>{_esc(crop.name)}</b></div><div>{_esc(crop.why)}</div>'
                     f'<div>{crop.size} px square at ({crop.x}, {crop.y})</div></div>']
            window = flattened.crop((crop.x, crop.y, crop.x + crop.size, crop.y + crop.size))
            cells.append(_panel("flattened labels", "input to all three", view,
                                [f'<image x="{crop.x}" y="{crop.y}" width="{crop.size}" '
                                 f'height="{crop.size}" href="{_png_data(window)}"/>']))
            truth = [_truth_path(_pixel_boundaries(pixels, crop))]
            row_report = {"subject": subject.name, "crop": crop.name,
                          "window": [crop.x, crop.y, crop.size]}
            for tool in TOOLS:
                if tool == "ours":
                    shapes, nodes = ours_in_crop(data["dir"] / "traced-edge.svg", crop)
                else:
                    shapes, nodes = tool_in_crop(data["shapes"][tool], crop, width, height)
                under = [f'<rect x="{crop.x}" y="{crop.y}" width="{crop.size}" height="{crop.size}" '
                         f'fill="#fff"/>']
                label, counts = _count_text(nodes)
                cells.append(_panel(TOOL_TITLES[tool], label, view,
                                    _tool_body(shapes, nodes, truth, k, under)))
                row_report[tool] = {"nodes": len(nodes), **{NODE_NAMES[c]: v for c, v in counts.items()}}
            report.append(row_report)
            rows.append("<section>" + "".join(cells) + "</section>")
    rows += _mixed_rows()
    intro = ["Each tool traced the WHOLE subject from the same flattened labels (segmentation.npz); "
             "panels are crops of those traces. Vinylgen: default TraceSettings, pixel mode, all "
             "colours cut, traced-edge.svg, from the labels only (hard-edged costs, no protection). "
             "Potrace 1.16: stacked scans per colour (-t 0). VTracer 0.6.4: flattened RGB, stacked "
             "colour mode, spline.",
             "Cyan: the label boundaries (pixel edges between colours), the truth all three traced. "
             "Red: each tool's own outlines, as visible after stacking. Nodes: Vinylgen from "
             "sodipodi:nodetypes; Potrace/VTracer typed by their handles on the visible partition "
             "of their stacked shapes (stacking clip points are cusps). A node shared by two "
             "colours counts once. Counts are nodes inside the crop.",
             "After the real subjects: icon glyphs and pinned fuzz scenes (tests/synthetic/fuzz), "
             "drawn whole, their hard-edged labels traced by all three (Potrace -a 0.8 -n; VTracer "
             "corner 45, splice 45, segment 4). Cyan there is the scene's true vector outline. "
             "Figures are the benchmark's for that scene, from the current code: required genuine "
             "corners found, kinks, extra inflections per 100 px.",
             SCENE_PANELS]
    target = _page(OUT / "contact-sheet.html",
                   "Tracer contact sheet: Vinylgen vs Potrace vs VTracer", intro,
                   _legend(_scene_legend()), rows)
    (OUT / "node-counts.json").write_text(json.dumps(report, indent=2) + "\n")
    return target


# --- icon corpus -------------------------------------------------------------------

# The tuned tool settings the benchmark grid chose (``benchmark_tracers.py --grid``).
POTRACE_TUNED = {"alphamax": 0.8, "opttolerance": 0.0}
VTRACER_TUNED = {"corner_threshold": 45.0, "splice_threshold": 45.0, "segment_length": 4.0}
ICON_ROWS = 24
# The glyphs and pinned fuzz scenes the main sheet mixes in after the real subjects.
MIXED_ICONS = ("icon-material-sharp-castle-512", "icon-remix-fill-leaf-128",
               "icon-heroicons-solid-bolt-128", "icon-bootstrap-regular-heart-fill-512")
MIXED_FUZZ = ("fuzz-cornered-blob-s1031-256", "fuzz-cornered-blob-s1050-512",
              "fuzz-polygon-s1014-256", "fuzz-blob-s1036-512", "fuzz-layered-s1000-256",
              "fuzz-layered-s1015-512")
SCENE_PANELS = ("Truth panel: every authored node on the visible outline, its shape giving its "
                "type (read from the segments meeting there, as the tools' nodes are read from "
                "their handles). Tool panels: grey rings mark the required genuine corners.")


def _icon_rows(rows_path: Path) -> list[tuple[str, dict]]:
    """The glyphs to show, from a tuned icon benchmark's labels-tier rows: the six
    with most genuine corners our tracer misses, the four Potrace misses most, the
    four where VTracer makes most kinks, then an even spread over the rest (in case
    id order) up to ``ICON_ROWS``. Each with its per-tool benchmark summary."""
    by_case: dict[str, dict] = {}
    for line in rows_path.read_text().splitlines():
        row = json.loads(line)
        if row["tier"] == "labels" and row["ok"]:
            by_case.setdefault(row["case_id"], {})[row["tool"]] = row["summary"]
    cases = {case: tools for case, tools in by_case.items() if len(tools) == 3}

    def misses(tool):
        def key(case):
            found, true = cases[case][tool]["required_by_kind"].get("corner", [0, 0])
            return (true - found, case)
        return key

    chosen: list[str] = []
    for key, count in ((misses("vinylgen"), 6), (misses("potrace"), 4),
                       (lambda case: (cases[case]["vtracer"]["kinks"], case), 4)):
        for case in sorted(cases, key=key, reverse=True):
            if count == 0:
                break
            if case not in chosen:
                chosen.append(case)
                count -= 1
    rest = sorted(case for case in cases if case not in chosen)
    spare = ICON_ROWS - len(chosen)
    chosen += [rest[int(i * len(rest) / spare)] for i in range(spare)] if spare > 0 else []
    return [(case, cases[case]) for case in chosen]


def _row_figures(summary: dict) -> str:
    found, true = summary["required_by_kind"].get("corner", [0, 0])
    curve = summary.get("curve") or {}
    extra = curve.get("extra_inflections_per_100px")
    wobble = "-" if extra is None else f"{extra:+.1f}"
    return f"corners {found}/{true}, kinks {summary['kinks']}, extra inflections/100px {wobble}"


def _scene_row(case, summaries: dict, work: Path, note: str = "") -> str:
    """One row for a scene case (an icon glyph or a fuzz scene), whole: its label
    render with the true outline and every authored node, then ours, Potrace and
    VTracer (tuned) traced from those labels, each captioned with its figures
    from ``summaries`` (per benchmark tool name)."""
    from vinylgen.contracts import TraceSettings
    from vinylgen.fidelity.harness import trace_labels
    from vinylgen.fidelity.raster import render_labels
    from vinylgen.fidelity.scene import FLATTEN_TOLERANCE
    from vinylgen.fidelity.sweep import _bounds
    case_id = case.case_id
    scene = case.scene
    size = int(max(scene.width, scene.height))
    crop = Crop(case_id, 0, 0, size, "")
    view = (0, 0, size)
    k = size / PANEL
    labels = np.asarray(render_labels(scene), dtype=np.int16)
    run = trace_labels((labels, scene.palette), work / case_id, TraceSettings(), subject=case_id)
    fills = [str(row["hex"]).upper() for row in run.palette]
    rgb = np.asarray([row["rgb"] for row in run.palette], dtype=np.uint8)
    bounds = _bounds(case, "labels", run)
    cells = [f'<div class="info"><h2>{_esc(case_id.removeprefix("icon-"))}</h2>'
             f'<div>{scene.width}x{scene.height} px</div>'
             + (f'<div>{_esc(note)}</div>' if note else "") + '</div>']
    truth = []
    for region in scene.regions:
        for outline in region.rings:
            points = outline.flatten(FLATTEN_TOLERANCE)
            truth.append(_truth_path("M" + "L".join(f"{px:.3f} {py:.3f}" for px, py in points)
                                     + "Z"))
    corners = [c for c in bounds.corners if c["role"] == "required" and c["kind"] == "corner"]
    authored = _authored_typed_nodes(scene, bounds.rings)
    body = [f'<image x="0" y="0" width="{scene.width}" height="{scene.height}" '
            f'href="{_png_data(Image.fromarray(rgb[labels], "RGB"))}"/>'] + truth
    for letter in "zsc":
        body += [_pinned(px, py, _truth_marker(t), k) for (px, py), t in authored.items()
                 if t == letter]
    label, _ = _count_text(authored)
    cells.append(_panel("labels + true outline",
                        f"authored {label}; {len(corners)} required genuine corners", view, body))
    rings = [_pinned(c["point"][0], c["point"][1],
                     f'<circle r="{2.2 * MARKER}" fill="none" stroke="#888" stroke-opacity="0.6" '
                     f'stroke-width="{NODE_LINE}"/>', k) for c in corners]
    for tool in TOOLS:
        if tool == "ours":
            shapes, nodes = ours_in_crop(run.edge_svg, crop)
        else:
            traced = (run_potrace(labels, fills, work / case_id / tool, **POTRACE_TUNED)
                      if tool == "potrace" else
                      run_vtracer(labels, fills, rgb, work / case_id / tool, **VTRACER_TUNED))
            shapes, nodes = tool_in_crop(traced, crop, scene.width, scene.height)
        under = [f'<rect x="0" y="0" width="{scene.width}" height="{scene.height}" fill="#fff"/>']
        label, _ = _count_text(nodes)
        summary = summaries.get("vinylgen" if tool == "ours" else tool)
        cells.append(_panel(f"{TOOL_TITLES[tool]}: {label}",
                            _row_figures(summary) if summary else "", view,
                            _tool_body(shapes, nodes, truth, k, under, rings)))
    shutil.rmtree(work / case_id, ignore_errors=True)
    return "<section>" + "".join(cells) + "</section>"


def _mixed_rows() -> list[str]:
    """The icon glyphs and pinned fuzz scenes the main sheet shows after the real
    subjects, each judged afresh."""
    from vinylgen.fidelity.fuzz import case_id as fuzz_id, pinned_cases, pinned_rows
    from vinylgen.fidelity.icons import icon_cases
    icons = {case.case_id: case for case in icon_cases() if case.case_id in MIXED_ICONS}
    fuzz = {case.case_id: case for case in pinned_cases() if case.case_id in MIXED_FUZZ}
    notes = {fuzz_id(row["seed"], row["size"], row["kind"]): row.get("note", "")
             for row in pinned_rows()}
    work = WORK / "mixed"
    rows = ['<h2>Icon glyphs</h2>']
    rows += [_scene_row(icons[name], _judged(icons[name], work / "judge"), work)
             for name in MIXED_ICONS]
    rows.append('<h2>Random shape scenes (pinned fuzz cases)</h2>')
    rows += [_scene_row(fuzz[name], _judged(fuzz[name], work / "judge"), work, notes.get(name, ""))
             for name in MIXED_FUZZ]
    return rows


def _judged(case, work: Path) -> dict:
    """The benchmark's labels-tier figures for ``case``, per tool, from the code as
    it is now (Potrace and VTracer at their tuned settings)."""
    rows = run_one(case, "labels", str(work), {"potrace": POTRACE_TUNED, "vtracer": VTRACER_TUNED})
    return {row["tool"]: row["summary"] for row in rows if row["ok"]}


_SCENE_LEGEND = ('<span><svg width="24" height="24" viewBox="-12 -12 24 24">'
                 '{authored}</svg>authored node (shape = type)</span>'
                 '<span><svg width="24" height="24" viewBox="-12 -12 24 24">'
                 '<circle r="10" fill="none" stroke="#888" stroke-width="{line}"/></svg>'
                 'required genuine corner</span>')


def _scene_legend() -> str:
    return _SCENE_LEGEND.format(authored=_truth_marker("c"), line=NODE_LINE)


def build_icons(rows_path: Path) -> Path:
    """The icon contact sheet: per glyph, its label render with the true outline and
    every authored node, then ours, Potrace and VTracer (tuned) traced from those
    labels."""
    from vinylgen.fidelity.icons import icon_cases
    cases = {case.case_id: case for case in icon_cases()}
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [_scene_row(cases[case_id], summaries, WORK / "icons")
            for case_id, summaries in _icon_rows(rows_path)]
    intro = ["Each glyph as the benchmark renders it (seeded size, rotation and sub-pixel offset); "
             "all three trace its hard-edged labels. Vinylgen: default TraceSettings. Potrace 1.16: "
             "stacked scans, -a 0.8 -n. VTracer 0.6.4: stacked colour mode, corner 45, splice 45, "
             "segment 4 (the benchmark grid's best settings).",
             "Rows: the six glyphs where ours misses most corners, the four where Potrace does, the "
             "four where VTracer makes most kinks, then a spread of the rest. Figures are the "
             "benchmark's for that glyph: required genuine corners found, kinks, extra inflections "
             "per 100 px.",
             SCENE_PANELS]
    return _page(OUT / "icons.html", "Icon contact sheet: Vinylgen vs Potrace vs VTracer",
                 intro, _legend(_scene_legend()), rows)


def _newest_tuned_icon_rows() -> Path:
    for run in sorted((ROOT / ".tmp" / "benchmark").glob("2*"), reverse=True):
        summary = run / "summary.md"
        if (summary.exists() and "Corpus: icons" in summary.read_text()
                and "Potrace: -a 0.8 -n" in summary.read_text()):
            return run / "rows.jsonl"
    raise SystemExit("no tuned icon benchmark run under .tmp/benchmark; run "
                     "benchmark_tracers.py --corpus icons with the tuned settings first")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--retrace", action="store_true", help="redo all three traces")
    parser.add_argument("--corpus", choices=("real", "icons"), default="real",
                        help="real subjects (default) or glyphs from the icon corpus")
    parser.add_argument("--rows", type=Path, default=None,
                        help="with --corpus icons: a tuned icon benchmark run's rows.jsonl "
                             "(default: the newest run whose summary names the tuned settings)")
    args = parser.parse_args()
    if args.corpus == "icons":
        print(build_icons(args.rows or _newest_tuned_icon_rows()))
    else:
        print(build(args.retrace))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
