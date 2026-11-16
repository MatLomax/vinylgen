# Wheels

Built wheels that `requirements-web.txt` installs by path.

## t3api-0.30.0-cp312-cp312-linux_x86_64.whl

The turbo tube tiler (t3) engine, compiled with Nuitka and packaged as `t3api`. Vinylgen's trace stage calls
`t3api.trace` on the cleaned labels at the trace's half-width table (and, when the trace names line colours, at its
stroke keys) and keeps the trace as a planar-map file (`map.json`).

| | |
|---|---|
| sha256 | `b1495ee0829c8e030d8091b859fcddd99e50278fbb6839674bfd0c41f7a62437` |
| build id (`t3api.BUILD_ID`) | `0.30.0-nuitka-37db4876afa530a1` |
| version (`t3api.VERSION`) | `0.30.0` |
| size | 2,950,732 bytes |
| API change | 0.30.0: the setting's four optional stroke keys, each absent at the default so every default answer is 0.29.1's byte for byte: `line_labels` (sorted, unique palette indices traced as centreline strokes), `width_mode` (`stroke` / `colour` / `tiers`), `tiers` (`auto` or a count >= 1, tiers mode only) and `stroke_fallback` (`face` or `best`; `none` is not written); `normal_setting` refuses a malformed key, and `trace` refuses a line label absent from the array or line labels covering every label, each a `ValueError` naming the key. The planar map then carries `strokes`, `stroke_nodes`, `dots` and `not_stroke` after `faces`, and an `under` record on each face edge lying under a stroke (its pieces the stroke's, byte for byte); `faces_svg_from_planar_map` ends with the stroke group `<g id="strokes" fill="none">` (one stroked path per stroke and per dot, after every layer), `strokes_svg_from_planar_map` writes that group alone, and the stub types the stroke records (`Stroke`, `StrokeEnd`, `StrokeNode`, `Dot`, `NotStroke`, `StrokeRef`). From 0.29.1: the type stub, `py.typed` and the argument checks |
| engine change | build 30: the stroke trace (line colours drawn as centreline strokes at a fitted width, the faces under a stroke sharing its drawn centreline; tracer card `stroke-trace`), and build 29's in-band arc and cubic floors. From build 29: the arc minimum sweep (`min_sweep`, absent at the default). From build 28: the cubic reverse walk decided by its chord-and-bulge certificate. From build 27: each curve kind's own tube half-width and the canonical setting `{"half": {line, arc, ellipse, cubic}, A1, A2, L}` (every kind 1.75 by default) |
| built by | the tracer repo (`MatLomax/tracer`) packaging round 11 (build 30), commit `d65e4a41d`, `cards/turbo-tube-tiler/packaging-11.md` |
| requires | Python 3.12, `numpy==1.26.4`, `scipy==1.11.4`, `numba==0.68.0`, `llvmlite==0.50.0`, `shapely==2.0.3` and `matplotlib>=3.6.3` (pinned in `requirements-web.txt` at 3.11.2, the oldest cp312 wheel on PyPI being 3.7.3); the stroke trace imports shapely and matplotlib only when it meets a line label |
| nib | the vendored nib core 0.14.1 reads and writes back the stroke keys and records byte for byte, and either header, `{half, A1, A2, L}` (with `min_sweep` and the stroke keys when present) as this wheel writes it and `{w, A1, A2, L}` as up to 0.26.0, so the Nodes stage opens maps traced with this wheel and with older ones |

## Replacing it with a new build

1. Build the wheel in the tracer repo and note its sha256 and `t3api.BUILD_ID`.
2. Delete the old wheel from this folder and copy the new one in.
3. Change the wheel's line in `requirements-web.txt` to the new file name, and the `numba`/`llvmlite` pins if the
   new wheel requires another pair.
4. Update this README: file name, sha256, build id, version and the tracer commit that built it.
5. Reinstall (`.venv/bin/pip install -r requirements-web.txt`) and run `just test`, whose `just typecheck` checks
   every vinylgen call into t3api against the new wheel's type stub. `just deploy` installs it and
   warms the engine cache for the new build.
