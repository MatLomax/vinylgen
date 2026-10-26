"""Draw the master case annotated: its label render at every size, each feature
numbered with a legend, and every true corner and junction from the scene's truth
marked with its turn.

    .venv/bin/python scripts/master_case_sheet.py

Writes .tmp/master/master.html (images inlined).
"""

from __future__ import annotations

import base64
import html
import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vinylgen.fidelity.master import FEATURES, MASTER_SIZE, master_cases  # noqa: E402
from vinylgen.fidelity.raster import render_labels  # noqa: E402
from vinylgen.fidelity.truth import scene_truth  # noqa: E402

OUT = ROOT / ".tmp" / "master"
DISPLAY = 900  # px each panel is drawn at


def _png(case) -> str:
    labels = np.asarray(render_labels(case.scene))
    palette = np.asarray([row["rgb"] for row in case.scene.palette], dtype=np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(palette[labels]).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _panel(case, numbered: bool) -> str:
    size = case.scene.width
    k = DISPLAY / size
    marks = []
    for corner in scene_truth(case.scene).corners:
        x, y = corner.point[0] * k, corner.point[1] * k
        colour = "#e8412c" if corner.kind == "corner" else "#1aa35a"
        marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="none" stroke="{colour}" '
                     f'stroke-width="2"/><text x="{x + 7:.1f}" y="{y - 6:.1f}" class="turn">'
                     f'{corner.turn_deg:.0f}°</text>')
    if numbered:
        s = DISPLAY / MASTER_SIZE
        for number, (_, point) in enumerate(FEATURES, 1):
            x, y = point[0] * s, point[1] * s
            marks.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="11" class="badge"/>'
                         f'<text x="{x:.1f}" y="{y + 4:.1f}" class="num">{number}</text>')
    return (f'<figure><div class="stage"><img src="data:image/png;base64,{_png(case)}" '
            f'width="{DISPLAY}" height="{DISPLAY}"><svg width="{DISPLAY}" height="{DISPLAY}">'
            + "".join(marks) + f'</svg></div><figcaption>{html.escape(case.case_id)} '
            f'({size} x {size} px, drawn at {DISPLAY} px)</figcaption></figure>')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = master_cases()
    legend = "".join(f"<li>{html.escape(name)}</li>" for name, _ in FEATURES)
    page = f"""<!doctype html><meta charset="utf-8"><title>Master case</title>
<style>
body {{ font: 14px system-ui, sans-serif; background: #222; color: #eee; margin: 20px; }}
.row {{ display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }}
.stage {{ position: relative; width: {DISPLAY}px; height: {DISPLAY}px; }}
.stage img {{ position: absolute; inset: 0; image-rendering: pixelated; }}
.stage svg {{ position: absolute; inset: 0; }}
.badge {{ fill: #fff; stroke: #000; stroke-width: 1.5; }}
.num {{ font: bold 12px system-ui; text-anchor: middle; fill: #000; }}
.turn {{ font: 11px system-ui; fill: #e8412c; paint-order: stroke; stroke: #fff; stroke-width: 3px; }}
ol {{ columns: 2; }} li {{ margin: 2px 0; }}
.key span {{ margin-right: 18px; }}
</style>
<h1>Master case</h1>
<p class="key"><span>White badges: numbered features (legend below).</span>
<span style="color:#e8412c">Red rings: true corners, with their turn.</span>
<span style="color:#1aa35a">Green rings: junctions (three or more colours meet, or the image edge).</span></p>
<ol>{legend}</ol>
<div class="row">{_panel(cases[0], True)}{_panel(cases[1], False)}</div>
"""
    path = OUT / "master.html"
    path.write_text(page)
    print(path)


if __name__ == "__main__":
    main()
