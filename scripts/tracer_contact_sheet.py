#!/usr/bin/env python3
"""Contact sheet of our tracer, Potrace and VTracer on three real subjects.

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
tool's filled shapes at the same scale with every node marked by type (cusp
rhombus, symmetric square, smooth circle). Our node types are our SVG's
``sodipodi:nodetypes``; Potrace's and VTracer's are read from their handles by
``benchmark_tracers.stacked_trace`` over the visible partition of their stacked
shapes (plus the cusps where stacking clips one shape against another). A node
shared by two colour layers is counted once.

Output: ``.tmp/contact-sheet/contact-sheet.svg`` and ``contact-sheet.png``.
Traces are cached in the work directory; ``--retrace`` redoes them.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import pickle
import shutil
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_tracers import run_potrace, run_vtracer, stacked_trace  # noqa: E402

OUT = ROOT / ".tmp" / "contact-sheet"
WORK = OUT / "work"
DATA = ROOT / ".vinylgen-data"
SVG_NS = "http://www.w3.org/2000/svg"
SODIPODI = "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd"
INKSCAPE = "http://www.inkscape.org/namespaces/inkscape"
TOOLS = ("ours", "potrace", "vtracer")
TOOL_TITLES = {"ours": "Vinylgen", "potrace": "Potrace", "vtracer": "VTracer"}
NODE_COLOURS = {"c": "#FF1FD2", "s": "#1FE4FF", "z": "#8CFF1F"}
NODE_NAMES = {"c": "cusp", "s": "symmetric", "z": "smooth"}

PANEL = 540          # panel side in sheet px
GAP = 14
LOCATOR = 220        # locator thumbnail width
MARKER = 3.4         # marker half-size in sheet px
HEADER = 44


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

def _png_data(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _marker(x, y, letter, r) -> str:
    colour = NODE_COLOURS[letter]
    style = f'fill="{colour}" stroke="#000" stroke-width="{r * 0.28:.4f}"'
    if letter == "c":
        return (f'<polygon points="{x:.3f},{y - r * 1.25:.3f} {x + r * 1.25:.3f},{y:.3f} '
                f'{x:.3f},{y + r * 1.25:.3f} {x - r * 1.25:.3f},{y:.3f}" {style}/>')
    if letter == "s":
        return f'<rect x="{x - r:.3f}" y="{y - r:.3f}" width="{2 * r:.3f}" height="{2 * r:.3f}" {style}/>'
    return f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" {style}/>'


_CLIP_IDS = iter(range(1_000_000))


def _panel(px, py, crop: Crop, body: list[str]) -> str:
    """The body (in source-pixel coordinates) scaled into a clipped panel."""
    clip = f"clip{next(_CLIP_IDS)}"
    scale = PANEL / crop.size
    return (f'<clipPath id="{clip}"><rect x="{px}" y="{py}" width="{PANEL}" height="{PANEL}"/>'
            f'</clipPath><g clip-path="url(#{clip})"><g transform="translate({px} {py}) '
            f'scale({scale:.6f}) translate({-crop.x} {-crop.y})">' + "".join(body) + "</g></g>"
            f'<rect x="{px}" y="{py}" width="{PANEL}" height="{PANEL}" fill="none" '
            f'stroke="#888" stroke-width="1"/>')


def _count_text(nodes: dict) -> str:
    counts = {letter: sum(1 for t in nodes.values() if t == letter) for letter in "csz"}
    return (f"{len(nodes)} nodes: {counts['c']} cusp, {counts['s']} symmetric, "
            f"{counts['z']} smooth"), counts


def _text(x, y, text, size=15, weight="normal", fill="#eee") -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;")
    return (f'<text x="{x}" y="{y}" font-family="DejaVu Sans, sans-serif" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}">{text}</text>')


def build(retrace: bool) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    rows_svg, report = [], []
    row_height = HEADER + PANEL + GAP * 2
    sheet_width = LOCATOR + GAP + 4 * (PANEL + GAP) + GAP
    y = 150
    for subject in SUBJECTS:
        data = trace_all(subject, retrace)
        flattened = Image.open(data["dir"] / "flattened.png").convert("RGB")
        width, height = flattened.size
        for crop in subject.crops:
            scale = PANEL / crop.size
            r = MARKER / scale
            x = GAP
            # Locator: the whole subject with the crop outlined.
            thumb = flattened.copy()
            thumb.thumbnail((LOCATOR, LOCATOR))
            factor = thumb.width / width
            rows_svg.append(_text(x, y + 18, subject.name, 17, "bold"))
            rows_svg.append(f'<image x="{x}" y="{y + HEADER}" width="{thumb.width}" '
                            f'height="{thumb.height}" xlink:href="{_png_data(thumb)}"/>')
            rows_svg.append(f'<rect x="{x + crop.x * factor:.1f}" y="{y + HEADER + crop.y * factor:.1f}" '
                            f'width="{max(3, crop.size * factor):.1f}" height="{max(3, crop.size * factor):.1f}" '
                            f'fill="none" stroke="#FF1FD2" stroke-width="2"/>')
            wrap, words, line = [], crop.why.split(), ""
            for word in words:
                if len(line) + len(word) > 28:
                    wrap.append(line)
                    line = ""
                line = (line + " " + word).strip()
            wrap.append(line)
            ty = y + HEADER + thumb.height + 22
            rows_svg.append(_text(x, ty, crop.name, 15, "bold"))
            for k, part in enumerate(wrap):
                rows_svg.append(_text(x, ty + 20 + 18 * k, part, 13, fill="#bbb"))
            rows_svg.append(_text(x, ty + 20 + 18 * len(wrap) + 4,
                                  f"{crop.size} px square at ({crop.x}, {crop.y})", 12, fill="#999"))
            x += LOCATOR + GAP
            # Raster panel.
            window = flattened.crop((crop.x, crop.y, crop.x + crop.size, crop.y + crop.size))
            window = window.resize((PANEL, PANEL), Image.NEAREST)
            rows_svg.append(_text(x, y + 30, "flattened labels (input to all three)", 15, "bold"))
            rows_svg.append(f'<image x="{x}" y="{y + HEADER}" width="{PANEL}" height="{PANEL}" '
                            f'xlink:href="{_png_data(window)}" style="image-rendering:optimizeSpeed"/>')
            rows_svg.append(f'<rect x="{x}" y="{y + HEADER}" width="{PANEL}" height="{PANEL}" '
                            f'fill="none" stroke="#888"/>')
            x += PANEL + GAP
            row_report = {"subject": subject.name, "crop": crop.name,
                          "window": [crop.x, crop.y, crop.size]}
            for tool in TOOLS:
                if tool == "ours":
                    shapes, nodes = ours_in_crop(data["dir"] / "traced-edge.svg", crop)
                else:
                    shapes, nodes = tool_in_crop(data["shapes"][tool], crop, width, height)
                body = [f'<rect x="{crop.x}" y="{crop.y}" width="{crop.size}" height="{crop.size}" '
                        f'fill="#fff"/>']
                body += [f'<path d="{_d(subpaths)}" fill="{fill}" fill-rule="evenodd"/>'
                         for fill, subpaths in shapes]
                for letter in "zsc":  # cusps on top
                    body += [_marker(px, py, t, r) for (px, py), t in nodes.items() if t == letter]
                rows_svg.append(_panel(x, y + HEADER, crop, body))
                label, counts = _count_text(nodes)
                rows_svg.append(_text(x, y + 18, TOOL_TITLES[tool], 16, "bold"))
                rows_svg.append(_text(x, y + 37, label, 13, fill="#ccc"))
                row_report[tool] = {"nodes": len(nodes), **{NODE_NAMES[k]: v for k, v in counts.items()}}
                x += PANEL + GAP
            report.append(row_report)
            y += row_height
    head = [_text(GAP, 34, "Tracer contact sheet: Vinylgen vs Potrace vs VTracer", 24, "bold"),
            _text(GAP, 62, "Each tool traced the WHOLE subject from the same flattened labels "
                  "(segmentation.npz); panels are crops of those traces, fills at the same scale. "
                  "Vinylgen: default TraceSettings, pixel mode, all colours cut, traced-edge.svg. "
                  "Potrace 1.16: stacked scans per colour (-t 0). VTracer 0.6.4: flattened RGB, stacked "
                  "colour mode, spline.", 13, fill="#ccc"),
            _text(GAP, 82, "Nodes: Vinylgen from sodipodi:nodetypes; Potrace/VTracer typed by their "
                  "handles on the visible partition of their stacked shapes (stacking clip points are "
                  "cusps). A node shared by two colours counts once. Counts are nodes inside the crop.",
                  13, fill="#ccc")]
    lx = GAP
    for letter in "csz":
        head.append(f'<g transform="translate({lx + 8},{116})">{_marker(0, 0, letter, 7)}</g>')
        head.append(_text(lx + 24, 121, f"{NODE_NAMES[letter]} node", 15))
        lx += 190
    sheet_height = y + GAP
    svg = (f'<svg xmlns="{SVG_NS}" xmlns:xlink="http://www.w3.org/1999/xlink" width="{sheet_width}" height="{sheet_height}" '
           f'viewBox="0 0 {sheet_width} {sheet_height}">'
           f'<rect width="100%" height="100%" fill="#1b1b1f"/>'
           + "".join(head) + "".join(rows_svg) + "</svg>\n")
    target = OUT / "contact-sheet.svg"
    target.write_text(svg)
    (OUT / "node-counts.json").write_text(json.dumps(report, indent=2) + "\n")
    return target


def render(svg: Path) -> Path:
    png = svg.with_suffix(".png")
    subprocess.run([str(ROOT / "scripts" / "inkscape-headless"), str(svg), "--export-type=png",
                    "--export-dpi=96", f"--export-filename={png}"], check=True)
    return png


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--retrace", action="store_true", help="redo all three traces")
    parser.add_argument("--no-render", action="store_true", help="skip the PNG render")
    args = parser.parse_args()
    svg = build(args.retrace)
    print(svg)
    if not args.no_render:
        print(render(svg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
