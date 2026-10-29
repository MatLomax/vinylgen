# The test array

Copied whole from the tracer repo (`MatLomax/tracer`), `cards/vinylgen-on-t3/probes/test-array/` at commit
`e3230869`, made with t3api 0.24.0 (build `0.24.0-nuitka-d78720649989c397`, the wheel in `wheels/`). The tracer's
copy is the source: a new t3api build that changes the trace regenerates it there, and this folder is replaced
whole. `labels.npy` traced by the wheel in vinylgen's venv (PyPI numpy 1.26.4 and scipy 1.11.4) gives this
`map.json` byte for byte.

The 400 x 400 crop of `fox-owl-table-2` at x 1600, y 400 (`meta.json`: the crop, its origin, its palette), the
fixture of vinylgen's trace tests and of nib's label-map document tests, and the array `just deploy` warms the
engine cache on. Chosen by the tracer's `.tmp/vinylgen-b2/pick.py` (the wheel's venv, t3api 0.24.0) over the 121 crops of a
200 px grid holding all four labels: the one with a holed face and the largest smallest count over the four piece
kinds. Its planar map holds 71 lines, 68 circular arcs, 20 elliptical arcs, 16 cubics and 46 border pieces on 70
edges (42 of them border edges), 49 nodes, 29 faces, 36 rings; the trace takes 1.05 s at one worker.

- `labels.npy`: the crop, `int16`, labels 0 to 3.
- `map.json`: `t3api.planar_map_json(t3api.planar_map(result, palette))`, 0 violations, read-back paint equal.
- `faces.svg`: `t3api.faces_svg_from_planar_map(pm, palette)`, the edge SVG's faces.
- `paths.json`: `t3api.planar_map_paths(pm)`, each face's `d` and `nodetypes`.
- `painted.npz`: `result.painted`, the labels t3 paints (diff_px 2,870 against `labels.npy`).
