# Validation and Testing

## Existing regression suite

Run:

```bash
.venv/bin/python scripts/run_tests.py            # parallel, one worker per core
.venv/bin/python scripts/run_tests.py -j 1 -v    # serial, verbose
.venv/bin/python scripts/run_tests.py test_web_pipeline --durations 10
```

`scripts/run_tests.py` is a standard-library unittest runner. It dispatches each
test as its own unit, except classes with class or module fixtures, which stay
together; it schedules the slowest units first using durations recorded by the
previous full run in `.tmp/test-durations.json`. Tests must therefore not share
fixed scratch paths: every test and every pipeline stage writes to a unique
temporary directory or file name. Plain `python -m unittest discover -s tests`
still works for a serial run. Slow seeded fuzz checks are split into shard tests
(for example `test_fuzz_matches_reference_0`..`_7` in
`tests/test_boundary_graph_fast.py`) so no single test bounds the run; each
shard regenerates the same seeded case list and checks its share of it.

`just test` runs this backend suite and the frontend `vitest` suite
concurrently, printing each one's output whole once both finish; the whole gate
takes roughly 95 s on the 28-core host, most of it the synthetic fidelity
tests tracing every committed case in both tiers.

### Synthetic ground-truth corpus

The default suite reads no real image. Tracing and palette behaviour are
regression-tested against synthetic scenes whose exact vector truth is known
(`vinylgen/fidelity/`):

- `scene.py` defines scenes of lines, circular and elliptical arcs and cubics;
  `raster.py` renders them deterministically to a label map (largest coverage per
  pixel) or an anti-aliased RGB image (blended in linear light); `truth.py` gives
  each colour's exact visible geometry, the true corners, junctions and edge
  points, and the ideal node count.
- `families.py` generates the analytic families: discs, ellipses, rectangles,
  rounded rectangles, polygons, stars, line-meets-arc shapes, rings, thin bars,
  arcs and outlines, pies, T- and Y-junctions, targets and corner-only contact,
  in a sweep scaled with the canvas and a fixed-pixel sweep, plus palette-stage
  RGB scenes (near shades, accents, dark outlines, noise). `fuzz.py` draws seeded
  random polygons, blobs, cornered blobs and layered multi-colour scenes.
- The fixed regression set (`tests/synthetic/fixed/`), the palette-stage set
  (`tests/synthetic/palette-stage/`) and the pinned fuzz cases
  (`tests/synthetic/fuzz/`, listed in `tests/synthetic/fuzz-seeds.json` with
  disjoint train and held-out tuning seed ranges) are committed as scene JSON and
  SVG, and tests regenerate them byte-identically. Rasters are regenerated.
- `harness.py` traces a scene through the real stages: the label-map tier writes
  a contract-v3 subject from the hard-edged render and runs `trace_subject`; the
  RGB tier runs the real palette stage on the anti-aliased render first.
  `metrics.py` measures the emitted `traced-edge.svg` against the truth: boundary
  distance (Hausdorff, mean, p95), per-colour misassigned area, corner recall,
  kinks and cusp precision, node count against the ideal, node types and
  topology.

`tests/test_synthetic_fidelity.py` traces every committed case in both tiers
with the product defaults and asserts bounds derived from first principles: the
resolved max deviation plus √2/2 px of pixel quantisation for boundary distance
outside corner discs, a cusp at every corner the raster can decide, no kinks,
at most twice the ideal node count plus four, truth topology for features at
least √2 px wide, and misassigned area within the deviation tube. Features
narrower than 2 px are held to topology and area only. An assertion that fails
against today's tracer is listed in `tests/synthetic/expected-failures.json`
under the worklog task that tracks its cause; it runs as an expected failure,
an unexpected pass fails the run (the entry must then be removed), and the table
rejects any slug that is not a real task.

### Full-image bucket

Tests that read real images, `outputs/` or `.tmp` baselines live in
`tests/fullimage/`, which default discovery does not collect. Run them only when
there is a reason to test a whole image:

```bash
.venv/bin/python scripts/run_tests.py --full-image
```

A full-image test skips when its image or baseline is absent.

### Fidelity sweep and tuning

```bash
.venv/bin/python scripts/fidelity_sweep.py --fixed --tier both
.venv/bin/python scripts/fidelity_sweep.py --grid --fuzz 100000:100200 --set minimum_straight_length=3
.venv/bin/python scripts/fidelity_tune.py --max-evals 60 --cases-per-eval 48
```

The sweep traces any selection of cases (`--fixed`, `--grid`, `--palette-stage`,
`--fuzz A:B`, `--sizes`, `--tag`) with given settings and writes per-case rows
and per-family, per-tag and per-size summaries to `.tmp/fidelity/sweep-*/`.
Corner recall is always reported beside cusp precision and their F1, since a
staircase whose every node is a cusp finds every corner. The tuner runs a
resumable compass search over the eight trace lengths and the corner threshold
on its own copy of the defaults, scores training seeds and the fixed set against
an error budget the settings cannot loosen, and writes the accuracy/node-count
Pareto front, a proposed settings diff, a before/after table on held-out seeds,
and before/after traces of the nine real images to `.tmp/fidelity/tune-*/`. It
only proposes: defaults change through a reviewed commit.

### Other coverage

The current tests also cover:

- Similar small shades merging while a tiny high-contrast accent survives
  palette inference.
- Deterministic palette, merge history, and count.
- Small-region replacement selecting the closest touching colour.
- Cutter cleanup's Remove giving each undersized group the colour it shares the
  most edge with, close calls within 90% of that edge going to the closer Lab
  colour and then the lower palette index.
- Regions equal to the threshold surviving.
- Adjacent tiny colours not swapping.
- Corner-only contact not preserving an isolated pixel.
- Report dimensions extending below the source image.
- Source-aware curved/attached stroke protection and AA-step rejection.
- Complete segmentation cost-field persistence.
- Pair-specific subpixel shared boundaries and local exact fallbacks.
- Bounded line/Bézier fitting without control-point overshoot.
- Diagonal staircases collapsing to sustained lines, architectural corners
  remaining sharp, and curved feather-like contours remaining Béziers.
- Ordered local recovery and explicit dense-fallback audit fields.
- Independent source-to-segmentation and source-to-vector feature audits.

## Mechanical checks already performed

- The output PNG exact distinct-colour count equals the reported inferred
  count.
- Fixed-count runs either produce exactly the requested distinct RGB count or
  fail before a pipeline revision is committed.
- Palette flattening folds sub-threshold anti-alias regions only when a similar
  touching colour exists. High-contrast small detail remains for tracing.
- The nine-image batch completes successfully with four concurrent workers.

## Required checks after palette changes

1. Run the unit suite.
2. Regenerate the complete corpus.
3. Verify each `flattened.png` exact colour count against `palette.json`.
4. Count four-connected components below the configured threshold.
5. Inspect `report.png` for at least the cleanest, busiest, and most colourful
   corpus examples.
6. Ensure output percentages remain ordered descending and refer only to the
   flattened image, not the appended report band.

Recommended visual sentinels:

- `desert-mech`: clean, low-detail control.
- `desert-rocket`: simple five-colour control.
- `cosmic-jellyfish`: highly intricate eight-colour stress case.
- `griffin-castle`: complex seven-colour architecture and character case.
- `crashed-ring`: high-detail geometric structure.

## Validation boundaries

Mechanical validation can prove dimensions, palette membership, colour count,
component sizes, percentages, and later SVG structure. It cannot prove visual
quality, subject correctness, compositional focus, or absence of rejected
semantic motifs. Vision-model checks may produce warnings, not guarantees.

SVG tracing validates XML, palette layers, closed/unique paths, unsupported SVG
features, and shared-partition topology. It also rasterizes the final SVG at
source dimensions and independently audits coherent dark source features in
both the flattened raster and vector render. Deviations are warnings: when any
occur, `trace-semantic-warnings.png` shows the whole original image lightly and
marks only the unmatched pixels, and `trace-missed-details-overlay.png` holds
the same marks alone on a transparent background for stacking over the SVG. Structural SVG failures remain hard failures.
