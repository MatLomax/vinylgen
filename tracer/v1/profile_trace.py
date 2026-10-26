"""Time the stages of a warm 1024 px master trace: python profile_trace.py"""
import collections, functools, sys, threading, time
from pathlib import Path
OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output
import numpy as np
ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from vinylgen import curve_fit as cf
from vinylgen.contracts import TraceSettings
from vinylgen.fidelity.harness import trace_labels
from vinylgen.fidelity.master import master_cases
from vinylgen.fidelity.raster import render_labels

totals = collections.Counter()
counts = collections.Counter()
lock = threading.Lock()

def timed(module, name):
    original = getattr(module, name)
    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                totals[name] += time.perf_counter() - started
                counts[name] += 1
    setattr(module, name, wrapper)

for name in ("_compute_chain_fit", "_geometric_corners", "_fit_section_records", "_gate_refine",
             "_band_tangents_nb", "_snap_straight_sections", "_section_line", "_short_handle_cusps",
             "shared_boundary_components"):
    timed(cf, name)
import vinylgen.tracing as tracing
chain_times = []
_fit = cf._compute_chain_fit
def _chain(canonical, *args, **kwargs):
    started = time.perf_counter()
    try:
        return _fit(canonical, *args, **kwargs)
    finally:
        chain_times.append((time.perf_counter() - started, len(canonical), hash(canonical) % 10000, canonical[0] == canonical[-1]))
cf._compute_chain_fit = _chain
cases = {c.scene.width: c for c in master_cases()}
for size in (256, 1024):
    case = cases[size]
    labels = np.asarray(render_labels(case.scene), dtype=np.int16)
    totals.clear(); counts.clear(); chain_times.clear()
    started = time.perf_counter()
    run = trace_labels((labels, case.scene.palette), Path(f"{OUT}/work/profile-{size}"), TraceSettings(),
                       subject=case.case_id)
    wall = time.perf_counter() - started
slow = sorted(chain_times, reverse=True)[:6]
print("slowest chain fits (s, samples, key, closed):", [(round(t, 2), n, h, c) for t, n, h, c in slow]); print("total chains", len(chain_times), "distinct", len({h for _, _, h, _ in chain_times}))
print(f"1024 warm trace: trace_seconds {run.trace_seconds:.2f}, wall {wall:.2f}")
print("stage totals (summed over the fit threads, so they can exceed wall time):")
for name, total in totals.most_common():
    print(f"  {name:30s} {total:7.2f} s  calls {counts[name]}")
