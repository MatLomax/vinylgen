"""Evidence behind band-rule corners that the judge calls kinks:
python diag_band_corners.py <label> <size> <feature> [count]"""
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

label, size, feature = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
limit = int(sys.argv[4]) if len(sys.argv) > 4 else 4
kinks = [k["point"] for k in json.load(open(f"{OUT}/{label}-{size}.json"))["kinks"] if k["feature"] == feature]
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
shown = 0
poly = np.empty((512, 2)); spare = np.empty((512, 2))
for points, settings, closed, chosen in calls:
    for index, cusp in chosen.items():
        if shown >= limit or not any(math.dist(cusp, k) < 3 for k in kinks):
            continue
        shown += 1
        minimum = max(1e-4, float(settings.minimum_straight_length))
        skip = cf._tip_skip(settings); cap = cf._side_cap(settings)
        if closed:
            ring = points[:-1]; ctx = cf._cyclic_context(ring, cf._corner_reach(settings))
            arr = cf._cyclic_extension(ring, ctx); pos = ctx + index
        else:
            arr = np.ascontiguousarray(points); pos = index
        gates = cf._sample_gates(arr)
        stops = np.zeros(len(arr), dtype=np.int8)
        b = cf._band_grown_side_nb(arr, gates, pos, -1, skip, minimum, cap, stops, poly, spare)
        a = cf._band_grown_side_nb(arr, gates, pos, 1, skip, minimum, cap, stops, poly, spare)
        def desc(side):
            ok, near, far, stopped, fit, noise = side
            kind = "line" if fit[0] else f"arc r={fit[7]:.1f} sign={fit[8]:+.0f}"
            return (f"{kind} window {cf._arc_between_nb(arr, min(near, far), max(near, far)):.1f} px "
                    f"noise {math.degrees(noise):.2f} deg")
        met, px, py, turn = cf._meeting_nb(*b[4][:9], *a[4][:9], arr[pos, 0], arr[pos, 1])
        one = cf._band_side_nb(arr, gates, b[2], a[2], pos, cf._seed_curvature_nb(arr, b[2], a[2]),
                               False, math.pi, poly, spare)
        print(f"sample {arr[pos].round(2)} cusp {np.round(cusp, 2)} turn {math.degrees(turn):.2f} deg "
              f"(noise sum {math.degrees(b[5] + a[5]):.2f}); one smooth side over span "
              f"{cf._arc_between_nb(arr, b[2], a[2]):.1f} px fits: {one[0]}")
        print("   before:", desc(b)); print("   after: ", desc(a))
