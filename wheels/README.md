# Wheels

Built wheels that `requirements-web.txt` installs by path.

## t3api-0.27.0-cp312-cp312-linux_x86_64.whl

The turbo tube tiler (t3) engine, compiled with Nuitka and packaged as `t3api`. Vinylgen's trace stage calls
`t3api.trace` on the cleaned labels at the trace's half-width table and keeps the trace as a planar-map file (`map.json`).

| | |
|---|---|
| sha256 | `a6cc05f490608998f61c2f3b8368b7dbc72c2752f31f07717a22d9ef7c5e13d4` |
| build id (`t3api.BUILD_ID`) | `0.27.0-nuitka-da1b63121b3eccba` |
| version (`t3api.VERSION`) | `0.27.0` |
| size | 2,134,083 bytes |
| engine change | build 27 with its fix rounds 1 and 2: each curve kind's own tube half-width, any kind but the line can be off and is then never drawn, no piece past the canvas's half-pixel band, junction feet kept in order; the API's `t3api.normal_setting` and the canonical setting `{"half": {line, arc, ellipse, cubic}, A1, A2, L}` (every kind 1.75 by default; a header with only `w` reads as every kind at w / 2) |
| built by | the tracer repo (`MatLomax/tracer`) packaging round 7 (build 27), `cards/turbo-tube-tiler/packaging-7.md` |
| requires | Python 3.12, `numpy==1.26.4`, `scipy==1.11.4`, `numba==0.68.0`, `llvmlite==0.50.0` |
| nib | the vendored nib core 0.6.0 reads and writes back either header, `{half, A1, A2, L}` as this wheel writes it and `{w, A1, A2, L}` as up to 0.26.0, so the Nodes stage opens maps traced with this wheel and with older ones |

## Replacing it with a new build

1. Build the wheel in the tracer repo and note its sha256 and `t3api.BUILD_ID`.
2. Delete the old wheel from this folder and copy the new one in.
3. Change the wheel's line in `requirements-web.txt` to the new file name, and the `numba`/`llvmlite` pins if the
   new wheel requires another pair.
4. Update this README: file name, sha256, build id, version and the tracer commit that built it.
5. Reinstall (`.venv/bin/pip install -r requirements-web.txt`) and run `just test`. `just deploy` installs it and
   warms the engine cache for the new build.
