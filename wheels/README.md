# Wheels

Built wheels that `requirements-web.txt` installs by path.

## t3api-0.26.0-cp312-cp312-linux_x86_64.whl

The turbo tube tiler (t3) engine, compiled with Nuitka and packaged as `t3api`. Vinylgen's trace stage calls
`t3api.trace` on the cleaned labels and keeps the trace as a planar-map file (`map.json`).

| | |
|---|---|
| sha256 | `7d1858c35d72a21492fe3733a8d4412da235da52008826aaa480210cfa9c3980` |
| build id (`t3api.BUILD_ID`) | `0.26.0-nuitka-92e3e2065eabf5d3` |
| version (`t3api.VERSION`) | `0.26.0` |
| size | 2,039,784 bytes |
| engine change | build 25: the arc sweep takes only candidates inside the pencil, so it always ends and fox-owl-table-2's edited labels trace to the end |
| built by | the tracer repo (`MatLomax/tracer`) packaging round 6 (build 25), `cards/turbo-tube-tiler/packaging-6.md` |
| requires | Python 3.12, `numpy==1.26.4`, `scipy==1.11.4`, `numba==0.68.0`, `llvmlite==0.50.0` |

## Replacing it with a new build

1. Build the wheel in the tracer repo and note its sha256 and `t3api.BUILD_ID`.
2. Delete the old wheel from this folder and copy the new one in.
3. Change the wheel's line in `requirements-web.txt` to the new file name, and the `numba`/`llvmlite` pins if the
   new wheel requires another pair.
4. Update this README: file name, sha256, build id, version and the tracer commit that built it.
5. Reinstall (`.venv/bin/pip install -r requirements-web.txt`) and run `just test`. `just deploy` installs it and
   warms the engine cache for the new build.
