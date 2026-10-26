"""Summarise master runs against a base: python compare.py <base> <label>..."""
import collections, json, sys
from pathlib import Path
OUT = Path(__file__).resolve().parents[2] / ".tmp" / "master-run" / "out"  # scratch output

def row(label):
    out = []
    for size in (1024, 256):
        d = json.load(open(f"{OUT}/{label}-{size}.json"))
        req = [c for c in d["corners"] if c["role"] == "required"]
        out.append((size, sum(c["found"] for c in req), len(req), len(d["kinks"]), d["nodes"],
                    d["trace_seconds"]))
    r = json.load(open(f"{OUT}/{label}-raster-1024.json"))
    return out, r["mismatched_px"], r["trace_seconds"]

base = sys.argv[1]
for label in sys.argv[1:]:
    sizes, mismatch, warm = row(label)
    print(f"{label}: " + "; ".join(f"{s}px corners {f}/{n} kinks {k} nodes {nd['c']}/{nd['s']}/{nd['z']}"
                                   for s, f, n, k, nd, _ in sizes)
          + f"; mismatch {mismatch} px; 1024 trace {warm:.2f} s")
for label in sys.argv[2:]:
    for size in (1024, 256):
        b = {tuple(c["point"]): c["found"] for c in json.load(open(f"{OUT}/{base}-{size}.json"))["corners"]
             if c["role"] == "required"}
        d = json.load(open(f"{OUT}/{label}-{size}.json"))
        req = [c for c in d["corners"] if c["role"] == "required"]
        gained = [f"{c['feature']} {c['feature_name']}" for c in req if c["found"] and not b[tuple(c["point"])]]
        lost = [f"{c['feature']} {c['feature_name']}" for c in req if not c["found"] and b[tuple(c["point"])]]
        kinks = collections.Counter(f"{k['feature']} {k['feature_name']}" for k in d["kinks"])
        print(f"\n{label} {size}px vs {base}:\n  gained {gained}\n  lost {lost}\n  kinks {dict(kinks.most_common())}")
