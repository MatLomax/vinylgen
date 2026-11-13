# Wheels

Built wheels that `requirements-web.txt` installs by path.

## t3api-0.29.1-cp312-cp312-linux_x86_64.whl

The turbo tube tiler (t3) engine, compiled with Nuitka and packaged as `t3api`. Vinylgen's trace stage calls
`t3api.trace` on the cleaned labels at the trace's half-width table and keeps the trace as a planar-map file (`map.json`).

| | |
|---|---|
| sha256 | `c68b2c53ec2770ba041e4276ad762751f25145e8756e601934ce0dac3f769f2d` |
| build id (`t3api.BUILD_ID`) | `0.29.1-nuitka-2d9b79b5cbfaf6f5` |
| version (`t3api.VERSION`) | `0.29.1` |
| size | 2,282,743 bytes |
| API change | 0.29.1: the engine is build 29 unchanged (byte-identical drawings to 0.29.0); the package ships a type stub (`t3api/__init__.pyi`) and `py.typed`, its typed dicts (`PlanarMap`, `Setting`, `SettingInput`, `FacePath`, `PaletteEntry`, ...) importable at run time and listed in `__all__`, and every public function checks its arguments, raising `TypeError`/`ValueError` naming the argument (a nested-list `labels`, a bytes `text`/`face_map_json`, or a drawn label without a named palette entry in `faces_svg`/`faces_svg_from_planar_map` is refused) |
| engine change | build 29: the arc minimum sweep, `Setting(min_sweep={"arc": A, "ellipse": E})`: an open circular or elliptical arc piece is drawn only when its drawn sweep is at least its kind's minimum (degrees, 0 to 360, 0 on an off kind), enforced inside the weight tiling; the default (the key absent, or 0 / 0) draws build 28's bytes. The API's `normal_setting` takes the optional key and the canonical setting carries it last, as floats, only when a minimum is above 0. From build 28: build 27 with the cubic reverse walk decided by its chord-and-bulge certificate (24.6x faster on the cubic fronts, every drawing unchanged; no API change). From build 27 and its fix rounds 1 and 2: each curve kind's own tube half-width, any kind but the line can be off and is then never drawn, no piece past the canvas's half-pixel band, junction feet kept in order; the API's `t3api.normal_setting` and the canonical setting `{"half": {line, arc, ellipse, cubic}, A1, A2, L}` (every kind 1.75 by default; a header with only `w` reads as every kind at w / 2) |
| built by | the tracer repo (`MatLomax/tracer`) packaging round 10 (build 29, typed API), `cards/turbo-tube-tiler/packaging-10.md` |
| requires | Python 3.12, `numpy==1.26.4`, `scipy==1.11.4`, `numba==0.68.0`, `llvmlite==0.50.0` |
| nib | the vendored nib core 0.13.0 reads and writes back either header, `{half, A1, A2, L}` (with `min_sweep` last when a minimum is above 0) as this wheel writes it and `{w, A1, A2, L}` as up to 0.26.0, so the Nodes stage opens maps traced with this wheel and with older ones |

## Replacing it with a new build

1. Build the wheel in the tracer repo and note its sha256 and `t3api.BUILD_ID`.
2. Delete the old wheel from this folder and copy the new one in.
3. Change the wheel's line in `requirements-web.txt` to the new file name, and the `numba`/`llvmlite` pins if the
   new wheel requires another pair.
4. Update this README: file name, sha256, build id, version and the tracer commit that built it.
5. Reinstall (`.venv/bin/pip install -r requirements-web.txt`) and run `just test`, whose `just typecheck` checks
   every vinylgen call into t3api against the new wheel's type stub. `just deploy` installs it and
   warms the engine cache for the new build.
