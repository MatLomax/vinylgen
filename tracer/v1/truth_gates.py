"""Does the true shape pass every pixel gate? A gate joins the centres of two
differently coloured pixels across a pixel edge; the true edge passes it when
sampling the truth at both centres gives the label image's colours. So the
gates the truth fails are exactly those touching a pixel whose centre sample
differs from its majority-coverage label."""
import json, sys
from pathlib import Path
OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output
import numpy as np
from PIL import Image
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vinylgen.fidelity.master import FEATURES, MASTER_SIZE, master_cases
from vinylgen.fidelity.raster import render_labels

out = {}
for case in master_cases():
    size = case.scene.width
    majority = np.asarray(render_labels(case.scene), dtype=np.int16)
    centre = np.asarray(render_labels(case.scene, supersample=1), dtype=np.int16)
    differ = centre != majority
    # Gates: pixel edges between differently labelled pixels (in the majority image).
    h = majority[:, 1:] != majority[:, :-1]
    v = majority[1:] != majority[:-1]
    gates = int(h.sum() + v.sum())
    failed_h = h & (differ[:, 1:] | differ[:, :-1])
    failed_v = v & (differ[1:] | differ[:-1])
    failed = int(failed_h.sum() + failed_v.sum())
    pts = np.asarray([p for _, p in FEATURES], dtype=float) * (size / MASTER_SIZE)
    by = {}
    for y, x in zip(*np.nonzero(differ)):
        i = int(np.argmin(np.hypot(*(pts - (x + .5, y + .5)).T)))
        key = f"{i + 1} {FEATURES[i][0]}"
        by[key] = by.get(key, 0) + 1
    out[size] = {"gates": gates, "gates_truth_fails": failed,
                 "pixels_centre_differs_from_majority": int(differ.sum()),
                 "by_feature": dict(sorted(by.items(), key=lambda kv: -kv[1]))}
    if size == 1024:
        rgb = np.asarray([r["rgb"] for r in case.scene.palette], dtype=np.uint8)
        img = (255 - (255 - rgb[majority].astype(np.int16)) // 4).astype(np.uint8)
        img[differ] = (255, 0, 0)
        Image.fromarray(img).save(OUT / "truth-gates-1024.png")
(OUT / "truth-gates.json").write_text(json.dumps(out, indent=1))
print(json.dumps(out, indent=1))

# Split the disagreeing pixels: exact ties (the majority colour covers exactly
# half, so the true edge runs through the centre and crosses the gate at its
# end, which the fitter's gate test counts as a pass) and real disagreements.
from vinylgen.fidelity.raster import _coverage_counts
split = {}
for case in master_cases():
    size = case.scene.width
    counts = _coverage_counts(case.scene, 16)
    majority = np.asarray(render_labels(case.scene), dtype=np.int16)
    centre = np.asarray(render_labels(case.scene, supersample=1), dtype=np.int16)
    differ = centre != majority
    top = np.take_along_axis(counts, majority[None].astype(np.intp), 0)[0] / 256.0
    tie = differ & (top == 0.5)
    real = differ & ~tie
    pts = np.asarray([p for _, p in FEATURES], dtype=float) * (size / MASTER_SIZE)
    by = {}
    for y, x in zip(*np.nonzero(real)):
        i = int(np.argmin(np.hypot(*(pts - (x + .5, y + .5)).T)))
        key = f"{i + 1} {FEATURES[i][0]}"
        by[key] = by.get(key, 0) + 1
    hist = np.histogram(top[real], bins=[0.5, 0.55, 0.6, 0.7, 0.8, 1.0001])[0].tolist()
    split[size] = {"ties": int(tie.sum()), "real": int(real.sum()),
                   "real_majority_share_hist[.5,.55,.6,.7,.8,1]": hist,
                   "real_by_feature": dict(sorted(by.items(), key=lambda kv: -kv[1]))}
print(json.dumps(split, indent=1))
