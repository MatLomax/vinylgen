"""Rasterise the 1024 px master case's traced edge SVG (the one run_master.py
wrote for <label>, else a fresh trace with the working tree) with the scene rasteriser's own rule (16 x 16 subsamples, even-odd per
path, majority colour per pixel) and compare it pixel by pixel with the label
image the tracer was given.

    python raster_compare.py <label>

Writes out/<label>-raster-1024.{png,json} and out/<label>-raster-1024-diff.png.
"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vinylgen.contracts import TraceSettings  # noqa: E402
from vinylgen.fidelity.harness import trace_labels  # noqa: E402
from vinylgen.fidelity.master import FEATURES, MASTER_SIZE, master_cases  # noqa: E402
from vinylgen.fidelity.metrics import parse_traced_svg  # noqa: E402
from vinylgen.fidelity.raster import _coverage_counts, render_labels  # noqa: E402

label = sys.argv[1] if len(sys.argv) > 1 else "r7"
OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output
SUPERSAMPLE = 16

case = next(c for c in master_cases() if c.scene.width == 1024)
scene = case.scene
size = scene.width
source = np.asarray(render_labels(scene), dtype=np.int16)

saved = OUT / f"{label}-{size}.run.json"
if saved.exists():
    timing = json.loads((OUT / f"{label}-{size}.time.json").read_text())
    run = SimpleNamespace(edge_svg=Path(json.loads(saved.read_text())["edge_svg"]),
                          trace_seconds=timing["trace"])
    wall = timing["wall"]
else:
    work = OUT / "work" / f"{label}-raster-{size}"
    work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    run = trace_labels((source, scene.palette), work, TraceSettings(), subject=case.case_id)
    wall = time.perf_counter() - started

# Rasterise the edge variant. Every layer path is one region; an extra colour,
# never in the palette, stands for subsamples no path covers.
hex_to_index = {"#%02X%02X%02X" % tuple(row["rgb"]): i for i, row in enumerate(scene.palette)}
uncovered = len(scene.palette)
parsed = parse_traced_svg(run.edge_svg)
regions = []
for layer in parsed["layers"]:
    colour = hex_to_index[layer["fill"]]
    for path in layer["paths"]:
        rings = [SimpleNamespace(flatten=lambda _tol, r=r: r)
                 for r, closed in zip(path["rings"], path["closed"]) if closed and len(r) >= 3]
        if rings:
            regions.append(SimpleNamespace(colour=colour, rings=rings))
vector = SimpleNamespace(width=size, height=size, background=uncovered, regions=regions,
                         palette=scene.palette + [{"name": "uncovered", "rgb": [0, 0, 0]}])
counts = _coverage_counts(vector, SUPERSAMPLE)
# Majority colour; a tie goes to the source's colour when it is among the tied,
# so a tie is never scored as a mismatch the rasteriser merely broke the other way.
top = counts.max(axis=0)
tied_to_source = np.take_along_axis(counts, source[None].astype(np.intp), 0)[0] == top
traced = np.where(tied_to_source, source, np.argmax(counts, axis=0)).astype(np.int16)
ties = int(((counts == top[None]).sum(axis=0) > 1).sum())

mismatch = traced != source
uncovered_px = int((counts[uncovered] > 0).sum())
# Signed coverage error per pixel: how much of the source colour the trace fails to cover.
source_fraction = np.take_along_axis(counts, source[None].astype(np.intp), 0)[0] / SUPERSAMPLE ** 2

feature_points = np.asarray([p for _, p in FEATURES], dtype=float) * (size / MASTER_SIZE)
ys, xs = np.nonzero(mismatch)
by_feature = {}
for x, y in zip(xs, ys):
    d = np.hypot(*(feature_points - (x + 0.5, y + 0.5)).T)
    i = int(np.argmin(d))
    key = f"{i + 1} {FEATURES[i][0]}"
    by_feature[key] = by_feature.get(key, 0) + 1

names = [row["name"] for row in scene.palette]
per_colour = {}
for c in range(len(scene.palette)):
    src = source == c
    out = traced == c
    per_colour[names[c]] = {"source_px": int(src.sum()), "traced_px": int(out.sum()),
                            "lost": int((src & ~out).sum()), "gained": int((out & ~src).sum()),
                            "traced_area_px2": float(counts[c].sum() / SUPERSAMPLE ** 2)}

summary = {
    "label": label, "size": size, "trace_seconds": run.trace_seconds, "wall_seconds": wall,
    "svg": str(run.edge_svg), "pixels": size * size,
    "mismatched_px": int(mismatch.sum()),
    "mismatched_fraction": float(mismatch.mean()),
    "tied_px": ties, "pixels_with_uncovered_area": uncovered_px,
    "source_colour_coverage": {
        "min_of_matching_px": float(source_fraction[~mismatch].min()),
        "pixels_below_75pct": int((source_fraction < 0.75).sum()),
        "pixels_below_90pct": int((source_fraction < 0.90).sum())},
    "mismatch_source_coverage_histogram": {
        f"{lo:.1f}-{lo + 0.1:.1f}": int(((source_fraction[mismatch] >= lo)
                                         & (source_fraction[mismatch] < lo + 0.1)).sum())
        for lo in np.arange(0, 0.5, 0.1)},
    "per_colour": per_colour,
    "mismatches_by_nearest_feature": dict(sorted(by_feature.items(), key=lambda kv: -kv[1])),
}
(OUT / f"{label}-raster-{size}.json").write_text(json.dumps(summary, indent=1))

rgb = np.asarray([row["rgb"] for row in scene.palette] + [[0, 0, 0]], dtype=np.uint8)
Image.fromarray(rgb[traced]).save(OUT / f"{label}-raster-{size}.png")
# Difference image: the source faded to 25 %, mismatched pixels in solid red,
# pixels that match but whose source colour covers under 75 % in orange.
faded = (255 - (255 - rgb[source].astype(np.int16)) // 4).astype(np.uint8)
faded[(source_fraction < 0.75) & ~mismatch] = (255, 150, 0)
faded[mismatch] = (255, 0, 0)
Image.fromarray(faded).save(OUT / f"{label}-raster-{size}-diff.png")

print(json.dumps({k: summary[k] for k in ("trace_seconds", "mismatched_px", "mismatched_fraction",
                                          "tied_px", "pixels_with_uncovered_area",
                                          "mismatch_source_coverage_histogram",
                                          "source_colour_coverage")}, indent=1))
print(json.dumps(summary["mismatches_by_nearest_feature"], indent=1))
print(json.dumps({k: {kk: v[kk] for kk in ("source_px", "lost", "gained")}
                  for k, v in per_colour.items()}, indent=1))
