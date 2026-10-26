import sys, time
from pathlib import Path
OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from vinylgen import curve_fit as cf
from vinylgen.contracts import TraceSettings
from vinylgen.fidelity.harness import trace_labels
from vinylgen.fidelity.master import master_cases
from vinylgen.fidelity.raster import render_labels
import traceback
seen = []
_fit = cf._compute_chain_fit
def chain(canonical, anchors, constraint_key, settings, step_key=((), ())):
    if len(canonical) == 2955:
        stack = [f"{f.name}:{f.lineno}" for f in traceback.extract_stack()[-6:-1]]
        seen.append((len(anchors), constraint_key, step_key != ((), ()),
                     {k: v for k, v in vars(settings).items() if not k.startswith("_")}, stack))
    return _fit(canonical, anchors, constraint_key, settings, step_key)
cf._compute_chain_fit = chain
case = next(c for c in master_cases() if c.scene.width == 1024)
labels = np.asarray(render_labels(case.scene), dtype=np.int16)
trace_labels((labels, case.scene.palette), Path(f"{OUT}/work/refits"), TraceSettings(), subject=case.case_id)
base = seen[0][3]
for anchors, constraints, steps, settings, stack in seen:
    print("anchors", anchors, "constraints", constraints, "steps", steps,
          "settings diff", {k: v for k, v in settings.items() if base.get(k) != v}, "\n   ", stack)
from vinylgen.fidelity.master import FEATURES, MASTER_SIZE
pts = np.asarray([p for _, p in FEATURES], dtype=float) * (1024 / MASTER_SIZE)
for _, constraints, _, _, _ in seen[1:]:
    for c in constraints:
        i = int(np.argmin(np.hypot(*(pts - np.asarray(c)).T)))
        print("pin", c, "nearest feature", i + 1, FEATURES[i][0])
