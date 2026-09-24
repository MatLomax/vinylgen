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
takes roughly 24 s on the 28-core host.

The current tests cover:

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
