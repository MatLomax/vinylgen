"""Trace the master case at each size with the code under ROOT (argv[1]) and judge
it with bounds.evaluate. Writes <out>/<label>-<size>.json and keeps the SVGs.

    python run_master.py <code-root> <label> <out-dir>
"""

import json
import shutil
import sys
import time
from pathlib import Path

root, label, out = Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3]).resolve()
sys.path.insert(0, str(root))

import numpy as np  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402

from vinylgen.contracts import TraceSettings  # noqa: E402
from vinylgen.fidelity.harness import trace_labels  # noqa: E402
from vinylgen.fidelity.master import FEATURES, MASTER_SIZE, master_cases  # noqa: E402
from vinylgen.fidelity.raster import render_labels  # noqa: E402

SODIPODI = "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}nodetypes"
out.mkdir(parents=True, exist_ok=True)


def feature_of(point, scale):
    pts = np.asarray([p for _, p in FEATURES], dtype=float) * scale
    d = np.hypot(*(pts - np.asarray(point)).T)
    i = int(np.argmin(d))
    return i + 1, FEATURES[i][0], float(d[i] / scale)


mode = sys.argv[4] if len(sys.argv) > 4 else "both"


def judge(case, run, labels):
    from vinylgen.fidelity import bounds
    case_bounds = bounds.case_bounds(case, "labels", run=run)
    return bounds.evaluate(case, "labels", run, case_bounds,
                           raster_colours={int(v) for v in np.unique(labels)})


# Smallest first, so the Numba compile lands on the 256 px trace and the 1024 px
# time is a warm benchmark.
for case in sorted(master_cases(), key=lambda c: c.scene.width):
    size = case.scene.width
    scale = size / MASTER_SIZE
    work = out / "work" / f"{label}-{size}"
    if mode != "judge":
        shutil.rmtree(work, ignore_errors=True)
    labels = np.asarray(render_labels(case.scene), dtype=np.int16)
    if mode == "judge":
        from vinylgen.fidelity.harness import TraceRun
        saved = json.loads((out / f"{label}-{size}.run.json").read_text())
        timing = json.loads((out / f"{label}-{size}.time.json").read_text())
        run = TraceRun(subject_dir=Path(saved["subject_dir"]), edge_svg=Path(saved["edge_svg"]),
                       overlap_svg=Path(saved["overlap_svg"]), audit=saved["audit"],
                       seconds=saved["seconds"], palette=saved["palette"],
                       colour_map=saved["colour_map"], trace_seconds=timing["trace"])
        wall = timing["wall"]
    else:
        work.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    if mode != "judge":
        run = trace_labels((labels, case.scene.palette), work, TraceSettings(), subject=case.case_id)
        wall = time.perf_counter() - started
        (out / f"{label}-{size}.time.json").write_text(json.dumps(
            {"trace": run.trace_seconds, "wall": wall}))
    types = "".join(el.attrib.get(SODIPODI, "") for el in ET.parse(run.edge_svg).getroot().iter()
                    if el.tag.endswith("path"))
    (out / f"{label}-{size}.run.json").write_text(json.dumps({
        "subject_dir": str(run.subject_dir), "edge_svg": str(run.edge_svg),
        "overlap_svg": str(run.overlap_svg), "audit": run.audit, "seconds": run.seconds,
        "palette": run.palette, "colour_map": run.colour_map}, default=str))
    if mode == "trace":
        print(f"{label} {size}px traced in {run.trace_seconds:.1f}s", flush=True)
        continue
    result = judge(case, run, labels)
    corners = []
    for c in result["corners"]:
        number, name, dist = feature_of(c["point"], scale)
        corners.append({"point": c["point"], "kind": c["kind"], "role": c["role"],
                        "turn": c.get("turn_deg"), "found": bool(c["found"]),
                        "nearest": c["nearest_cusp_px"], "radius": c["radius_px"],
                        "feature": number, "feature_name": name, "feature_dist": dist})
    kinks = []
    for k in result["cusps"]:
        if k["kink"]:
            number, name, dist = feature_of(k["point"], scale)
            kinks.append({"point": k["point"], "feature": number, "feature_name": name,
                          "feature_dist": dist})
    svg = out / f"{label}-{size}.svg"
    shutil.copy(run.edge_svg, svg)
    summary = {"label": label, "size": size, "trace_seconds": run.trace_seconds,
               "wall_seconds": wall, "svg": str(svg),
               "nodes": {t: types.count(t) for t in "csz"}, "node_total": len(types),
               "hausdorff": result.get("hausdorff"), "corners": corners, "kinks": kinks}
    (out / f"{label}-{size}.json").write_text(json.dumps(summary, indent=1, default=float))
    req = [c for c in corners if c["role"] == "required"]
    print(f"{label} {size}px: trace {run.trace_seconds:.1f}s wall {wall:.1f}s nodes {summary['nodes']} "
          f"required found {sum(c['found'] for c in req)}/{len(req)} kinks {len(kinks)}", flush=True)
