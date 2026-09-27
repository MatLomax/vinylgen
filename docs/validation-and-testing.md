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
takes roughly 47 s on the 28-core host (the backend about 43 s, the frontend,
which sets the total, about 46 s).

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
  RGB scenes (near shades, accents, dark outlines below the usage cutoff with and
  without whole-colour preservation, noise), each inferred with the palette
  settings its scene names. `fuzz.py` draws seeded random polygons, blobs,
  cornered blobs and layered multi-colour scenes.
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

`tests/test_synthetic_fidelity.py` traces the gate slice (below) with the
product defaults and asserts the bounds in
`vinylgen/fidelity/bounds.py`, the one yardstick the sweep and tuner use too.
In the RGB tier the truth is the scene after the palette stage's whole-colour
replacements: the colours a case expects to merge are merged, and a colour the
usage cutoff may fold (its replay on the true areas folds it) is recoloured to
the colour it actually folded into, read from the run, because a thin feature's
inferred colour is a blend whose share and nearest surviving colour differ from
the scene colour's. Every boundary that survives a merge or fold is asserted in
full. A scene colour that survives only as an inferred colour matching no scene
colour (a thin feature's anti-alias blend) has that colour judged as it
geometrically, while the topology, evidence and palette checks still report the
mismatch. The assertions:

- boundary distance within `D`, the resolved max deviation plus √2/2 px of pixel
  quantisation plus the metric's own error, outside the discs round the true
  vertices and the narrow zones; and everywhere, each traced layer's boundary
  within `e` of its label's boundary in the label map the trace received, both
  ways (`e` is the max deviation or sharpening allowance, plus half a pixel, plus
  the RGB tier's sub-pixel crossing offset, plus the metric's error), which holds
  features below the resolution floor near their place;
- a cusp within `R(c)` of every vertex the raster can decide, where `R(c)` is the
  farthest point of the intersection of the `D`-bands of the vertex's true
  branches (about `D` at a nearly straight corner, growing only at a sharp tip,
  capped by where tangent branches separate and by the side lengths);
- no kinks: a cusp away from the vertices and zones is allowed only on a round
  the trace may sharpen: a stretch turning one way by at least the corner
  threshold, holding no true vertex, whose radius is at most
  `(A + (1 + sec(T/2)) Q) / (sec(T/2) - 1)` for its full turn `T` (its sharp
  point within the allowance of its sample, the move check, seen through `Q`),
  with the cusp within `A + Q` plus the round's own depth of its sharp point. At
  `A` = 1 px that radius is 16.3 px at 60°, 6.54 px at 90° (2.71 sharpening
  radii), 3.12 px at 120° and 1.55 px at 150°. The full turn is the truth's net
  turn over the stretch extended by the corner detector's reach on each side,
  what the detector reads there, so a piece of a longer round, or of a circle,
  is judged by the whole round and a cusp on it is a kink. The fixed set's
  rounded rectangles at 3 and 4 sharpening radii must therefore stay round. On
  rasterised filleted wedges (512 and 256 px, ten placements each, 2026-09-26)
  the trace puts no cusp on a 90° or 99° round of 0.9 to 1.5 times this radius,
  but does on some 120° to 165° rounds of 1.1 to 1.5 times it (up to 6 of 10
  placements at 165°), which the rule calls kinks: the design sharpens only
  within the allowance;
- boundary nodes outside the zones at most the node limit: every node the
  trace stage must anchor in the label map it received (a pixel corner where
  three or more colours meet or two meet corner to corner, a frame point between
  two colours: a many-colour junction splits into several), the ideal
  segmentation's corners and the junctions and edge points no such anchor stands
  for, whose traced node can lie outside the zones (two at a corner-to-corner
  contact with no anchor, which the raster bridges or parts), plus on each part of an ideal run outside them the nodes of an explicit route (its
  axis extremes, and where one cubic cannot draw the truth between two of them
  within the span's budget, the fewest greedy splits that can), plus one node
  per part end a zone cuts. The budget is the fitter's curve tolerance less the
  farthest the span's pixel-edge samples of the hard-edged render lie from the
  truth (at least √2/2 px; more where the truth curves tightly), so every sample
  lies within the tolerance of the route's curves. The count is derived; it is a
  geometric requirement on the trace, not a claim that the fitter's search finds
  that route (its nodes sit on samples, its candidates are the smoothed samples'
  extremes, inflections and bends, and it measures a sample's error at the
  sample's own parameter). The ideal segmentation's 90° pieces are not such a
  route: between the axis extremes of a rotated 8:1 ellipse a span sweeps 155°
  of its parameter and one cubic misses the truth by 7 px, so
  `ellipse-a8-rot17-1536`'s limit is 6, not 4. On the actual samples of the
  single-ring disc and ellipse cases, a route with the limit's node count fits
  every sample within the tolerance (worst 0.998 px of 1.152 px, on
  `disc-off0.5-1536`, 2026-09-26);
- truth topology per colour, compared beyond a band round the discs and zones.
  Every polygon and hole count of every colour is asserted to lie within its
  range over the band widths from `D` to `3D` (sampled every `D/4`), so added
  structures beyond the range's maximum and deletions below its minimum fail.
  One deletion is decided where the trace's band count is the range's minimum,
  one addition where it is the maximum; elsewhere a single change near the band
  edge cannot be told from the band's own effect. Every true piece or hole whose
  rings stay `3D` clear of the discs and zones must match exactly one traced
  piece or hole by location (each within `max(D, e)` of the other), and no
  traced piece or hole clear of them by `3D` plus that tolerance may be left
  unmatched, so an isolated addition fails whatever the counts. A per-case check
  in the full-corpus run checks that the truth itself passes and that every change the truth decides
  fails: each isolated structure deleted, each counted structure deleted where
  its count decides one deletion, an isolated addition, an addition near the
  removed region where its count decides one, and otherwise the smallest
  multiple deletion or addition that leaves the range. Each single change a
  count cannot decide is a skipped subtest naming why. Over the corpus's 1,452
  (run, colour, count) pairs, one deletion is decided for 1,426 and one addition
  for 1,414; the review's added-structure mutant (a hole or piece added between
  `2.3D` and `2.7D` from the removed region) fails all 1,145 pairs that decide
  an addition and have room for it (2026-09-26);
- per colour, a symmetric difference with the truth within the area of its
  `D`-tube, asserted only where that area is below the colour's own, so a trace
  that drops it fails;
- raster evidence: every four-connected region of the label map the trace
  received (the hard-edged render, or the palette stage's `segmentation.npz` in
  the RGB tier) has traced area of its colour, and each colour's traced area
  matches its pixels within the tube its tolerance allows; every thin region
  (most of its pixels in no 2 x 2 block of its own) outside the vertex discs has
  at least 60% of those thin pixels' centres covered by its traced layer, since
  `e` is wider than a one-pixel feature and would let one move a full width off
  its pixels, and is covered by exactly one connected piece of that layer within
  `e` of its pixels (a piece counting when it covers at least `max(2, 5%)` of
  those centres, the trace stage's own `viable_pieces == 1` for a protected
  component), so a thin feature cut by a gap fails however little area the gap
  takes (a 1 px bar cut by a 1, 2, 3, 20 or 100 px gap fails it, 2026-09-26);
  in the RGB tier every scene colour the area check cannot hold must survive the
  palette stage unless it removes it by design, and a folded colour at least `D`
  wide must fold into the colour the usage-cutoff replay on the true areas
  gives (a narrower one's target moves boundaries by less than `D`, and is
  recorded as immaterial).

A per-case test checks that every scene colour is held by the area or the
evidence check. The resolution floors (2 px for geometry, √2 px for topology)
apply locally: each colour's parts narrower than the floor (the colour less its
morphological opening) are excluded from the truth assertions, buffered by `D`,
and everything else is asserted in full; an assertion with nothing left to hold
is reported as a skip with its reason. The palette stage's small-region cleanup
(and a colour that may fold but covers no pixel centre) is excluded the same way
in the RGB tier, as a zone round the region itself.

Most of these bounds are derived from the trace stage's guarantees and the
raster's geometry: the label-map tier's quantisation, `D` and `e` in the
direction from the truth or evidence to the trace, `R(c)`, which corners are
decidable, the sharpenable-round radius, the local floors, the area tube, the
ideal node count and the node limit built on it. The rest are not, and
`bounds.py` says so where each is defined:

- The RGB tier's quantisation has no derivable bound and reuses √2/2 px as a
  measured baseline. Measured over all 153 RGB runs (2026-09-26), the local
  boundary distance exceeds the max deviation plus the metric's error by at most
  0.67 px (`ellipse-a8-rot45-128`, a 0.04 px margin) in every run whose
  `hausdorff` assertion holds. Six runs exceed √2/2, all of them expected
  failures with a tracked cause: the thin ring of `ring-nested-w2-px576`
  (3.54 px), the many-colour junctions of `palette-noise-a5-pie-6-512` (1.50 px),
  `pie-6-ph0-512` (1.28 px) and `pie-8-ph7-1536` (1.02 px),
  `fuzz-layered-s1015-512` (0.99 px, sub-pixel trace lengths) and the folded
  outline of `palette-dark-outline-disc-w1.5-512` (0.77 px).
- `D` and `e` in the direction from the trace to the truth or evidence: the
  fitter keeps each sample within the max deviation of the curve at the sample's
  own parameter and bounds nothing between samples. The trace is held to the
  same bounds both ways by choice. Over all 284 runs whose `hausdorff` assertion
  holds (2026-09-26), the farthest traced point from the truth is 0.83 `D`
  (`labels/ring-nested-512`, 1.45 px) and from the evidence 0.68 `e`
  (`labels/rrect-x0.8-rot0-1536`, 1.41 px).
- The node limit is a geometric requirement: that a route with its node count
  exists is derived, that the fitter's search finds it is not. Its one node per
  run part a zone cuts is an allowance for the boundary passing into geometry
  the ideal segmentation does not describe.
- The sharpenable radius's tip skip is the trace stage's own setting
  (`curve_fit._TIP_SKIP_ALLOWANCES`), read as it is.
- The thin-region pin's 60% and its one-piece companion are the trace stage's
  own post-fit thresholds for a protected component (pixel recall and
  `viable_pieces == 1`), not a derivation: the fitter's max deviation lets a
  feature no wider than it move wholly off its pixels or break.
- The topology bands sample the widths from `D` to `3D` every `D/4` rather than
  searching them continuously.
- Which colours the palette stage folds, and into what, is read from the run;
  the target is checked against the replay's where the colour is at least `D`
  wide.
- The palette matching threshold (`harness.MATCH_DELTA_E`, 12 ΔE) is chosen.

The default run traces the gate slice, the runs listed in
`tests/synthetic/gate-slice.json` (`regression.gate_runs`): every case on a
canvas of at most 256 px in both tiers, plus the cheapest case of each family
and tier without one (the palette-stage sets and the fixed-pixel sweep). That is
96 of the 291 runs, holding 100 of the 345 expected failures, and adds about 13 s
on the 28-core host. The full corpus runs in the full-image bucket (below), and
only there are the checks that each case's bounds can fail (every colour held by
an area assertion, and the topology changes above); they depend on the bounds
code, not the trace.

An assertion that fails against today's tracer is listed in
`tests/synthetic/expected-failures.json` with the worklog tasks that track its
defects and the measurements the assertion compared against its bound when it
was recorded (for example the boundary distances and the length beyond them,
the missed-vertex, kink, node and missing-region counts, and the excess areas;
`bounds.MEASUREMENTS` lists them). It runs as an expected failure while it still
fails and is no worse than recorded: a count may not grow, a length may not grow
by more than 0.5 px or 10%, and an area by more than 4 px² or 10%, whichever is
larger (`bounds.WORSE_MARGIN`). Worse is a regression the entry would otherwise
hide, so it fails the run; better does not. An unexpected pass fails the run
until its entry is removed. Any other exception while checking it is an error.
Update the table with:

```bash
.venv/bin/python scripts/record_expected_failures.py
```

It traces the corpus, tightens each entry's measurements that got better (a
worse one keeps its record unless `--allow-worse` is given), and removes
entries that now pass. `--task TIER/CASE:ASSERTION=SLUG[,SLUG...]` adds
an entry for a new failure. It writes all or nothing: while any failure has no
entry, a record is worse than the table's without `--allow-worse`, a case raises,
or an orphan entry (a case the suite does not trace, or an assertion it does not
make or that has nothing to hold) is kept without `--remove-orphans`, it reports
everything and writes nothing. An entry's tasks are edited in the JSON by hand.
The table test checks each entry's form; worklog is not available to the tests,
so that a slug names a real task is checked by hand.

### Full-image bucket

Tests that read real images, `outputs/` or `.tmp` baselines live in
`tests/fullimage/`, which default discovery does not collect, as does the full
synthetic corpus (`test_synthetic_fidelity_corpus.py`, about 100 s on the 28-core
host; run it alone with `scripts/run_tests.py test_synthetic_fidelity_corpus`). Run them only when there is a reason to test a whole image or the whole
corpus:

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
staircase whose every node is a cusp finds every corner; vertices, kinks and the
boundary distance are classified by the regression bounds, so the sweep counts
exactly what the tests assert. The tuner runs a resumable compass search over
the eight trace lengths and the corner threshold on its own copy of the
defaults, scores training seeds and the fixed set with the regression
assertions' own measures, each normalised by its bound under the default
settings (which the settings under test cannot loosen): boundary distance to the
truth and to the evidence, per-colour area against the truth and the evidence,
missed vertices, kinks, topology and unmatched layers, dropped evidence regions
and colours, misplaced thin regions and fold targets, and nodes against the
ideal count outside the zones (every measure an assertion bounds, except the
palette-stage cases' palette outcome, which no trace setting changes), and
writes the accuracy/node-count Pareto front, a proposed settings diff, a
before/after table per family and per canvas size on held-out cases (fuzz seeds
outside the training range, including canvas sizes training never uses, and
analytic grid cases outside the fixed set, from 64 px up), and before/after
traces of the nine real images to `.tmp/fidelity/tune-*/`. It only proposes:
defaults change through a reviewed commit.

### Benchmark against Potrace and VTracer

```bash
.venv/bin/python scripts/benchmark_tracers.py                  # synthetic corpus
.venv/bin/python scripts/benchmark_tracers.py --corpus icons   # hand-authored icons
.venv/bin/python scripts/benchmark_tracers.py --grid           # tune both tools
.venv/bin/python scripts/tracer_contact_sheet.py               # real subjects, by eye
```

`benchmark_tracers.py` traces every corpus run with our tracer, Potrace 1.16 and
VTracer 0.6.4 from the same label map. Neither tool writes a partition, so each
tool's shapes are stacked the way Inkscape stacks a multi-colour trace, and the
visible partition is clipped to the canvas. All three are judged with the
regression bounds.

Beyond the sweep's statistics it scores:
- recall per vertex kind: genuine corners separately from junctions and edge
  points, which our shared boundaries anchor by construction;
- genuine-corner F1;
- curve quality: extra inflections per 100 px and tangent-direction error, with
  one function applied to traced and truth rings;
- node placement against the scene's authored nodes.

The tiers are `labels`, `rgb` and `rgb-flat`. In `rgb-flat` ours traces the
palette stage's labels without the source's sub-pixel costs, the same input the
other two get. Results go to `.tmp/benchmark/<timestamp>/`.

The icon corpus (`vinylgen/fidelity/icons.py`) is 216 glyphs committed under
`tests/icons/` with their licences and pinned upstream commits, re-fetched by
`scripts/fetch_icons.py`:
- Material Symbols in its Sharp, Rounded and Outlined styles;
- Bootstrap Icons;
- Remix Icon fill;
- Heroicons solid.

Each glyph is rendered once at a seeded size, rotation and sub-pixel offset. Its
full check, every case building truth and bounds, runs in the full-image bucket
(`test_fidelity_icons_corpus.py`).

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
