# Wheels

Built wheels that `requirements-web.txt` installs by path.

## t3api-0.24.0-cp312-cp312-linux_x86_64.whl

The turbo tube tiler (t3) engine, compiled with Nuitka and packaged as `t3api`. Vinylgen's trace stage calls
`t3api.trace` on the cleaned labels and keeps the trace as a planar-map file (`map.json`).

| | |
|---|---|
| sha256 | `d0d195a1a163116bd0a3caa8251141d37333dd52cb49729493ff83ffb247e92c` |
| build id (`t3api.BUILD_ID`) | `0.24.0-nuitka-d78720649989c397` |
| version (`t3api.VERSION`) | `0.24.0` |
| built by | the tracer repo (`MatLomax/tracer`) at commit `632e018c`, packaging round 4 |
| requires | Python 3.12, `numpy==1.26.4`, `scipy==1.11.4`, `numba==0.68.0`, `llvmlite==0.50.0` |

## Replacing it with a new build

1. Build the wheel in the tracer repo and note its sha256 and `t3api.BUILD_ID`.
2. Delete the old wheel from this folder and copy the new one in.
3. Change the wheel's line in `requirements-web.txt` to the new file name, and the `numba`/`llvmlite` pins if the
   new wheel requires another pair.
4. Update this README: file name, sha256, build id, version and the tracer commit that built it.
5. Reinstall (`.venv/bin/pip install -r requirements-web.txt`) and run `just test`. `just deploy` installs it and
   warms the engine cache for the new build.
