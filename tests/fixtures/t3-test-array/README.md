# The test array

The 400 x 400 crop of `fox-owl-table-2` at x 1600, y 400 (`meta.json`: the crop, its origin, its palette), the
fixture of vinylgen's trace tests and of nib's label-map document tests, and the array `just deploy` warms the
engine cache on. Chosen by `.tmp/vinylgen-b2/pick.py` (the wheel's venv, t3api 0.24.0) over the 121 crops of a
200 px grid holding all four labels: the one with a holed face and the largest smallest count over the four piece
kinds. Made by `make.py` with the round-9 wheel's Python (t3api 0.29.0, build `0.29.0-nuitka-7b0c309ff0ab7a65`; its only difference from the 0.28.0 fixture is `map.json`'s `build`). Its header's `setting` is the canonical table `{"half": {line, arc, ellipse, cubic}, A1, A2, L}`, every kind at 1.75.
Its planar map holds 71 lines, 68 circular arcs, 20 elliptical arcs, 16 cubics and 45 border pieces on 69 edges (41
of them border edges), 49 nodes, 28 faces, 35 rings (one holed face); the trace takes 0.98 s at one worker (the second trace of a warm process, timed beside other runs).

One face is empty (`result.summary["faces_empty"]` 1): the single pixel of label 1 at row 136, column 0 on the left
border, whose arm the trace draws as one line on the canvas border, so its ring holds no pixel centre. It is left
out of the face map and the planar map, and the one edge beside it has a null side (`drawn_null_sides` 1). Build 23's
fixture held it as face 12, with 29 faces, 36 rings and 70 edges; the painted labels are unchanged.

- `labels.npy`: the crop, `int16`, labels 0 to 3.
- `map.json`: `t3api.planar_map_json(t3api.planar_map(result, palette))`, with `names` beside `palette` (Black,
  Golden Yellow, Cream, Burnt Orange); 0 violations, read-back paint equal; every ring runs with its face on the left.
- `faces.svg`: `t3api.faces_svg_from_planar_map(pm, palette)`, the edge SVG's faces, its layers named from `names`.
- `paths.json`: `t3api.planar_map_paths(pm)`, each face's `d` and `nodetypes`.
- `painted.npz`: `result.painted`, the labels t3 paints (diff_px 2,870 against `labels.npy`).
- `make.py`: writes every file above and prints the counts stated here.

Copied from the tracer repo's `cards/vinylgen-on-t3/probes/test-array/` at commit d804858b (build 24, t3api 0.25.0), and `map.json` regenerated with build 25 (t3api 0.26.0), then build 27 (t3api 0.27.0), then build 28 (t3api 0.28.0); `make.py` regenerates it there with the wheel's python.
