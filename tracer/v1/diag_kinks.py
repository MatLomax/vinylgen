"""Why does the band corner rule (VINYLGEN_CORNER_MODE b/c) put corners on smooth
truth? Trace the 1024 master once, record every _geometric_corners call, and for
each chosen corner within 3 px of a judged kink print the evidence behind it."""
import json, math, sys
from pathlib import Path
OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output
import numpy as np
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vinylgen import curve_fit as cf
from vinylgen.contracts import TraceSettings
from vinylgen.fidelity.harness import trace_labels
from vinylgen.fidelity.master import master_cases
from vinylgen.fidelity.raster import render_labels

label = sys.argv[1]
size = int(sys.argv[2]) if len(sys.argv) > 2 else 1024
kinks = [(k["point"], k["feature"], k["feature_name"])
         for k in json.load(open(f"{OUT}/{label}-{size}.json"))["kinks"]]
calls = []
orig = cf._geometric_corners
def wrapped(points, settings, closed=False):
    chosen = orig(points, settings, closed)
    calls.append((np.array(points, dtype=float), settings, closed, chosen))
    return chosen
cf._geometric_corners = wrapped
case = next(c for c in master_cases() if c.scene.width == size)
labels = np.asarray(render_labels(case.scene), dtype=np.int16)
trace_labels((labels, case.scene.palette), Path(f"{OUT}/work/diag-{label}-{size}"), TraceSettings(),
             subject=case.case_id)

seen = set()
for points, settings, closed, chosen in calls:
    for index, cusp in chosen.items():
        for kp, feat, name in kinks:
            if math.dist(cusp, kp) < 3 and (feat, tuple(np.round(cusp, 2))) not in seen:
                seen.add((feat, tuple(np.round(cusp, 2))))
                # Recompute this sample's evidence on the same extended array.
                minimum = max(1e-4, float(settings.minimum_straight_length))
                skip = cf._tip_skip(settings); cap = cf._side_cap(settings)
                if closed:
                    ring = points[:-1]; ctx = cf._cyclic_context(ring, cf._corner_reach(settings))
                    arr = cf._cyclic_extension(ring, ctx); pos = ctx + index
                else:
                    arr = np.ascontiguousarray(points); pos = index
                gates = cf._sample_gates(arr)
                stops = np.zeros(len(arr), dtype=np.int8)
                sides = []
                for step in (-1, 1):
                    ok, near, far, stopped, fit, chord = cf._band_grown_side_nb(
                        arr, gates, pos, step, skip, minimum, cap, stops, cf._BAND_REACH)
                    kind = "line" if fit[0] else f"arc r={fit[7]:.1f}"
                    sides.append(f"{kind} len={cf._arc_between_nb(arr, min(near, far), max(near, far)):.1f} "
                                 f"chord={chord:.1f} err={fit[9]:.2f}")
                p = arr[pos]
                print(f"feature {feat} {name}: sample {p.round(2)} cusp {np.round(cusp, 2)} "
                      f"move {math.dist(p, cusp):.2f} (no stops)\n    before: {sides[0]}\n    after:  {sides[1]}")

print("\n--- one-arc check on the first near-axis-bar corner ---")
for points, settings, closed, chosen in calls:
    hit = [(i, c) for i, c in chosen.items() for kp, f, _ in kinks if f == 41 and math.dist(c, kp) < 3]
    if not hit:
        continue
    index, cusp = hit[0]
    minimum = max(1e-4, float(settings.minimum_straight_length))
    skip = cf._tip_skip(settings); cap = cf._side_cap(settings)
    if closed:
        ring = points[:-1]; ctx = cf._cyclic_context(ring, cf._corner_reach(settings))
        arr = cf._cyclic_extension(ring, ctx); pos = ctx + index
    else:
        arr = np.ascontiguousarray(points); pos = index
    gates = cf._sample_gates(arr)
    stops = np.zeros(len(arr), dtype=np.int8)
    b = cf._band_grown_side_nb(arr, gates, pos, -1, skip, minimum, cap, stops, 0.5)
    a = cf._band_grown_side_nb(arr, gates, pos, 1, skip, minimum, cap, stops, 0.5)
    b_far, a_far = b[2], a[2]
    length = cf._arc_between_nb(arr, b_far, a_far)
    one = cf._fit_side_nb(arr, b_far, a_far, 1, length, 1.0)
    print("span", arr[b_far], "->", arr[a_far], "one fit:", "line" if one[0] else f"arc r={one[7]:.1f}",
          "err", round(one[9], 3))
    worst = []
    for k in range(b_far, a_far + 1):
        if gates[k, 0] == 0 and gates[k, 1] == 0:
            continue
        n, s1, s2 = cf._fit_gate_crossings_nb(one, gates[k, 0], gates[k, 1], gates[k, 2], gates[k, 3])
        off = min(abs(s1), abs(s2)) if n else math.inf
        worst.append((off, tuple(arr[k])))
    worst.sort(reverse=True)
    print("worst gate offsets of the one fit:", [(round(o, 3), p) for o, p in worst[:4]])
    # Is there a straight line through every gate? Least-squares on the gate centres
    # is not it; search the (angle, offset) of the line for the smallest worst offset.
    ks = [k for k in range(b_far, a_far + 1) if gates[k, 0] or gates[k, 1]]
    best = (math.inf, None)
    base = math.atan2(arr[a_far, 1] - arr[b_far, 1], arr[a_far, 0] - arr[b_far, 0])
    for da in np.linspace(-0.05, 0.05, 201):
        th = base + da
        dx, dy = math.cos(th), math.sin(th)
        nx, ny = -dy, dx
        for c in np.linspace(-1.5, 1.5, 301):
            ox, oy = arr[pos, 0] + c * nx, arr[pos, 1] + c * ny
            fit = (True, ox, oy, dx, dy, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            w = 0.0
            for k in ks:
                n, s1, s2 = cf._fit_gate_crossings_nb(fit, gates[k, 0], gates[k, 1], gates[k, 2], gates[k, 3])
                w = max(w, min(abs(s1), abs(s2)) if n else math.inf)
                if w >= best[0]:
                    break
            if w < best[0]:
                best = (w, (math.degrees(th), c))
    print("best straight line through the span: worst gate offset", round(best[0], 3), "at", best[1])
    break
