#!/usr/bin/env python3
"""Warm the t3 engine's kernel cache: one ``t3api.trace`` of the test array.

``t3api`` compiles its numba kernels on first use into ``t3api.cache_dir()`` (keyed on the
build, ``~/.cache/t3api/<build id>`` unless ``T3API_CACHE_DIR`` is set). A cold cache costs
the first trace tens of seconds of warm-up; every trace job is a fresh process, so the deploy
runs this once after installing a new wheel, in the service's environment and before the
health check, and the test runner runs it before the suite when the cache holds no record.

The trace is checked by the planar map's gate (``planar_map_check`` with no violation, the
read-back paint equal to t3's painted labels at every centre); a failure exits non-zero.
On success ``<cache_dir()>/warm.json`` records ``{build, seconds, warm_s, workers}``:
``seconds`` the trace call's wall time, ``warm_s`` the engine's warm-up within it (0 when the
cache was already warm), ``workers`` the processes it drew across. The record sits beside the
kernels it describes and goes when the cache is cleared.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TEST_ARRAY = ROOT / "tests" / "fixtures" / "t3-test-array"
RECORD = "warm.json"


def warm() -> dict:
    """Trace the test array once, check it, and write the cache's ``warm.json``."""
    import t3api

    labels = np.load(TEST_ARRAY / "labels.npy")
    palette = json.loads((TEST_ARRAY / "meta.json").read_text())["palette"]
    started = time.perf_counter()
    result = t3api.trace(labels)
    seconds = time.perf_counter() - started
    pm = t3api.read_planar_map(t3api.planar_map_json(t3api.planar_map(result, palette)))
    check = t3api.planar_map_check(pm)
    if check["violations"]:
        raise RuntimeError(f"the warm trace's planar map has {check['violations']} violation(s): "
                           f"{check['problems']}")
    if not np.array_equal(t3api.paint_planar_map(pm), result.painted):
        raise RuntimeError("the warm trace's planar map does not paint t3's painted labels")
    record = {"build": t3api.BUILD_ID, "seconds": float(seconds), "warm_s": float(result.warm_s),
              "workers": int(result.workers)}
    cache = Path(t3api.cache_dir())
    cache.mkdir(parents=True, exist_ok=True)
    (cache / RECORD).write_text(json.dumps(record, indent=2) + "\n")
    record["cache"] = str(cache)
    return record


def main() -> int:
    try:
        record = warm()
    except Exception as error:  # noqa: BLE001 - reported, and the deploy stops
        print(f"error: t3api cache warm failed: {error}", file=sys.stderr)
        return 1
    print(f"t3api cache warmed: {record['cache']} (build {record['build']}): trace "
          f"{record['seconds']:.2f} s, warm-up {record['warm_s']:.2f} s, "
          f"{record['workers']} workers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
