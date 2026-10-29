"""Make the test array: the 400 x 400 crop of fox-owl-table-2 at x 1600, y 400 and its t3api outputs, written beside
this file. Run with the wheel's Python (the installed t3api), from anywhere:

    <wheel venv>/bin/python cards/vinylgen-on-t3/probes/test-array/make.py

Writes ``labels.npy``, ``meta.json``, ``map.json`` (``planar_map_json(planar_map(result, palette))``), ``faces.svg``
(``faces_svg_from_planar_map``), ``paths.json`` (``planar_map_paths``) and ``painted.npz`` (``result.painted``), and
prints the counts the README states."""
import json
import os
import time

import numpy as np
import t3api

HERE = os.path.dirname(os.path.abspath(__file__))
PROBES = os.path.dirname(HERE)
X, Y, S = 1600, 400, 400


def kind_counts(pm: dict) -> dict:
    """The planar map's pieces by kind, a circular arc (rx = ry) apart from an elliptical one."""
    kinds = {"L": 0, "Ac": 0, "Ae": 0, "C": 0, "border": 0}
    for e in pm["edges"]:
        for p in e["pieces"]:
            k = p["kind"]
            if k == "A":
                k = "Ac" if p["seg"][3] == p["seg"][4] else "Ae"
            kinds[k] = kinds.get(k, 0) + 1
    return kinds


def main() -> None:
    full = np.load(os.path.join(PROBES, "fox-owl-table-2.npy"))
    pal = json.load(open(os.path.join(PROBES, "fox-owl-table-2-palette.json")))["palette"]
    crop = np.ascontiguousarray(full[Y:Y + S, X:X + S])
    t3api.trace(crop, workers=1)                       # the warm-up (kernels compiled or loaded), not timed
    t0 = time.perf_counter()
    r = t3api.trace(crop, workers=1)
    sec = time.perf_counter() - t0
    pm = t3api.planar_map(r, pal)
    np.save(os.path.join(HERE, "labels.npy"), crop)
    json.dump({"crop": "fox-owl-table-2", "x": X, "y": Y, "size": S, "palette": pal},
              open(os.path.join(HERE, "meta.json"), "w"), indent=1)
    open(os.path.join(HERE, "map.json"), "w").write(t3api.planar_map_json(pm))
    open(os.path.join(HERE, "faces.svg"), "w").write(t3api.faces_svg_from_planar_map(pm, pal))
    json.dump(t3api.planar_map_paths(pm), open(os.path.join(HERE, "paths.json"), "w"))
    np.savez_compressed(os.path.join(HERE, "painted.npz"), painted=r.painted)
    chk = t3api.planar_map_check(pm)
    print("build", t3api.BUILD_ID, "trace s (second trace, one worker)", round(sec, 2))
    print("kinds", kind_counts(pm), "edges", len(pm["edges"]), "border_edges", chk["border_edges"],
          "drawn_null_sides", chk["drawn_null_sides"], "nodes", len(pm["nodes"]), "faces", len(pm["faces"]),
          "rings", chk["rings"], "holed faces", sum(1 for f in pm["faces"] if len(f["rings"]) > 1))
    print("summary faces", r.summary["faces"], "faces_empty", r.summary["faces_empty"], "diff_px",
          r.summary["diff_px"], "names", pm["names"])
    print("check violations", chk["violations"], "paint equal", bool(np.array_equal(t3api.paint_planar_map(pm),
                                                                                   r.painted)))
    print("sizes", {f: os.path.getsize(os.path.join(HERE, f)) for f in sorted(os.listdir(HERE))})


if __name__ == "__main__":
    main()
