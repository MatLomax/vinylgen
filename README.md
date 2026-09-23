# Vinylgen

Experimental tooling for turning generated limited-palette artwork into clean,
cuttable vector layers.

## Palette inference

Run the deterministic palette analyser from this directory:

```bash
python3 palette_infer.py images
```

Tiny isolated regions are removed by default when they contain fewer than 24
pixels. Configure that per run, or disable it entirely:

```bash
python3 palette_infer.py images --min-region-pixels 50
python3 palette_infer.py images --min-region-pixels 0
```

Colours occupying less than 1% of the full image are then folded into their
nearest palette colour. Override with `--min-colour-share`; zero disables it.

An undersized region is replaced only by a colour touching its boundary. When
several colours touch it, the closest one in perceptual Lab space is selected.
Regions must share an edge to count as connected; corner-only contact does not
make two pieces one cuttable region. Cleanup repeats until no further safe
replacement is possible, up to a fixed safety limit.

Multiple input images run concurrently by default. Control that explicitly with
`--jobs N`; use `--jobs 1` for serial processing. Pixel assignment within each
image is already performed in bounded chunks. Connected-component cleanup stays
global so regions crossing artificial tile boundaries are not broken apart.

For every input it creates `outputs/<subject>/` containing:

- `source.png`: byte-identical source copy for comparison.
- `flattened.png`: the image mapped to the automatically inferred palette.
- `segmentation.npz`: versioned labels, confidence and protected-feature
  contract consumed by tracing.
- `report.png`: the flattened image with a black palette legend beneath it,
  ordered by descending image coverage.
- `palette.json`: palette shares, spatial-region statistics, merge decisions,
  small-region cleanup statistics, and the exact settings used.

The analyser deliberately starts with too many colours. It merges perceptually
similar colours, gives small similar colours a wider merge tolerance, and uses
spatial fragmentation as evidence of edge noise. Undersized high-contrast
regions survive only when confidence and geometry identify a coherent thin
feature; contrast alone does not preserve a speck.

The inferred count is therefore allowed to vary by image; it is not forced to
six or to any other preset. The JSON report records every automatic merge and
the thresholds used, making a result reproducible and auditable.

Scratch files belong in `.tmp/`; `images/` is the source corpus and `outputs/`
contains reviewable results grouped by subject.

## SVG tracing

Trace one or more palette outputs:

```bash
python3 svg_trace.py outputs/desert-mech outputs/crashed-ring outputs/cosmic-jellyfish
```

This creates `traced-overlap.svg`, `traced-edge.svg`, and an auditable
`trace.json`. Both SVGs contain exact-colour named Inkscape
layers. A global planar graph extracts and simplifies each interface once, then
polygonizes both neighbouring colours from that exact shared edge. This removes
the third-colour hairlines caused by independently fitted cumulative contours.

Use `python3 svg_trace.py --help` for physical dimensions, overlap, optional
colour omission, order overrides, curve controls, cleanup, and reusable JSON
configuration.

The boundary graph uses source classification confidence to place shared
interfaces off the integer pixel grid. The curve pass works in pixels of the
traced image whatever the physical size (default 1.5 px curve-fit tolerance
within a 2 px maximum deviation), retains straight segments and sharp corners,
and emits cubic Beziers for shallow bends. A straight-line deviation of 0
turns off the straight-line shortcut, but a bend shallower than the curve-fit
tolerance can still become a line; lower the tolerance to keep it curved. It operates reversibly on shared
boundaries, so neighbouring colours receive the same curve in opposite
directions.

## Web application

The GUI foundation is a FastAPI service with a React/TypeScript client. Local
state defaults to `.vinylgen-data/`; override it with `VINYLGEN_DATA_DIR`.
Artifacts are immutable and content-addressed, while projects, branching
revisions, settings snapshots, artifact metadata, and background jobs live in
SQLite.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-web.txt
cd web && npm install && npm run build && cd ..
VINYLGEN_DATA_DIR=.vinylgen-data .venv/bin/uvicorn vinylgen.web.app:app --host 127.0.0.1 --port 8275
```

Short-prompt source generation uses the installed Codex CLI and the service
user's existing ChatGPT-backed Codex login. Run `codex login` as that user and
verify `codex exec --ephemeral --json --help` before starting Vinylgen. Each
request gets a fresh empty directory under `.tmp/` and a fresh ephemeral Codex
session. Vinylgen does not read or copy Codex OAuth files, and it deliberately
removes `OPENAI_API_KEY` from the child process so this personal deployment
cannot silently fall through to separately billed API authentication.

Codex's image-generation tool surface, subscription allowance, and JSONL event
format may change independently of Vinylgen. Missing login, unavailable image
generation, timeout, malformed output, and ambiguous multiple outputs fail the
job without creating a source revision. A direct OpenAI Image API provider is a
documented contingency only; it is not implemented or enabled.

For frontend development, run `npm run dev` in `web/`; Vite proxies `/api` to
the backend on port 8275. API documentation is available at `/docs`.
