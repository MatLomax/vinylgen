# Wheels

Built wheels that `requirements-web.txt` installs by path.

## t3api-0.28.0-cp312-cp312-linux_x86_64.whl

The turbo tube tiler (t3) engine, compiled with Nuitka and packaged as `t3api`. Vinylgen's trace stage calls
`t3api.trace` on the cleaned labels at the trace's half-width table and keeps the trace as a planar-map file (`map.json`).

| | |
|---|---|
| sha256 | `72b49422e9259ba1d84af243b8a2b416eae59c8cba3e908f288079a0b589155b` |
| build id (`t3api.BUILD_ID`) | `0.28.0-nuitka-57c9f77f09308ea1` |
| version (`t3api.VERSION`) | `0.28.0` |
| size | 2,136,323 bytes |
| engine change | build 28: build 27 with the cubic reverse walk decided by its chord-and-bulge certificate (24.6x faster on the cubic fronts, every drawing unchanged; no API change). From build 27 and its fix rounds 1 and 2: each curve kind's own tube half-width, any kind but the line can be off and is then never drawn, no piece past the canvas's half-pixel band, junction feet kept in order; the API's `t3api.normal_setting` and the canonical setting `{"half": {line, arc, ellipse, cubic}, A1, A2, L}` (every kind 1.75 by default; a header with only `w` reads as every kind at w / 2) |
| built by | the tracer repo (`MatLomax/tracer`) packaging round 8 (build 28), `cards/turbo-tube-tiler/packaging-8.md` |
| requires | Python 3.12, `numpy==1.26.4`, `scipy==1.11.4`, `numba==0.68.0`, `llvmlite==0.50.0` |
| nib | the vendored nib core 0.7.0 reads and writes back either header, `{half, A1, A2, L}` as this wheel writes it and `{w, A1, A2, L}` as up to 0.26.0, so the Nodes stage opens maps traced with this wheel and with older ones |

## Replacing it with a new build

1. Build the wheel in the tracer repo and note its sha256 and `t3api.BUILD_ID`.
2. Delete the old wheel from this folder and copy the new one in.
3. Change the wheel's line in `requirements-web.txt` to the new file name, and the `numba`/`llvmlite` pins if the
   new wheel requires another pair.
4. Update this README: file name, sha256, build id, version and the tracer commit that built it.
5. Reinstall (`.venv/bin/pip install -r requirements-web.txt`) and run `just test`. `just deploy` installs it and
   warms the engine cache for the new build.
