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

`tests/test_synthetic_fidelity.py` traces every committed case in both tiers
with the product defaults and asserts the bounds in
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
  the trace may sharpen, one of radius at most `2.41 (A + (1 + √2) Q)` (2.71
  sharpening radii at `A` = 1 px), with the cusp within the round's radius plus
  `√2 Q` of it; a cusp on a larger round, a circle included, is a kink. The fixed
  set's rounded rectangles at 3 and 4 sharpening radii must therefore stay round;
- boundary nodes outside the zones at most the node limit: the ideal
  segmentation's vertex nodes whose traced node can lie outside the zones, plus
  on each part of an ideal run outside them the larger of its ideal interior
  nodes and its axis extremes, plus one node per part end a zone cuts. The
  fitter takes the fewest pieces over candidates that include every axis
  extreme, and the ideal pieces split at the extremes are such a route (each
  within the curve tolerance of the samples), so a count above the limit is a
  route that is not minimal over the fitter's own candidates;
- truth topology per colour, compared beyond a band round the discs and zones.
  A polygon or hole count is asserted, to lie within its range over the band
  widths from `D` to `3D` (sampled every `D/4`), only where the count at the
  trace's band is the range's minimum, so deleting any one structure it counts
  fails it; a count whose range reaches below that cannot decide a single
  deletion and is not asserted. Every true piece or hole whose rings stay `3D`
  clear of the discs and zones must match exactly one traced piece or hole by
  location (each within `max(D, e)` of the other), and no traced piece or hole
  clear of them by `3D` plus that tolerance may be left unmatched. A per-case
  test checks that the truth itself passes and that every single deletion the
  assertion claims to catch fails it;
- per colour, a symmetric difference with the truth within the area of its
  `D`-tube, asserted only where that area is below the colour's own, so a trace
  that drops it fails;
- raster evidence: every four-connected region of the label map the trace
  received (the hard-edged render, or the palette stage's `segmentation.npz` in
  the RGB tier) has traced area of its colour, and each colour's traced area
  matches its pixels within the tube its tolerance allows; every thin region
  (most of its pixels in no 2 x 2 block of its own) outside the vertex discs has
  at least 60% of those thin pixels' centres covered by its traced layer, since
  `e` is wider than a
  one-pixel feature and would let one move a full width off its pixels; in the
  RGB tier every scene colour the area check cannot hold must survive the
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
- The node limit's one node per run part a zone cuts is an allowance for the
  boundary passing into geometry the ideal segmentation does not describe.
- The thin-region pin's 60% is the trace stage's own post-fit pixel-recall
  threshold for a protected component, not a derivation: the fitter's max
  deviation lets a feature no wider than it move wholly off its pixels.
- The topology bands sample the widths from `D` to `3D` every `D/4` rather than
  searching them continuously.
- Which colours the palette stage folds, and into what, is read from the run;
  the target is checked against the replay's where the colour is at least `D`
  wide.
- The palette matching threshold (`harness.MATCH_DELTA_E`, 12 ΔE) is chosen.

An assertion that fails against today's tracer is listed in
`tests/synthetic/expected-failures.json` under the worklog task that tracks its
cause, with the measurements the assertion compares against its bound (for
example the boundary distances and the length beyond them, the missed-vertex,
kink, node and missing-region counts, and the excess areas; `bounds.MEASUREMENTS`
lists them) and the identities of what it found wrong (the worst points, the
missed vertices, the kink points, the node positions outside the zones, the
wrong per-colour counts and unmatched structures, the missing and misplaced
regions, the colours over their area bound; `bounds.IDENTITIES` lists them), one
item per line. It runs as an expected failure only while it still fails, every
measurement equals the recorded one, and the identities are the recorded set: a
count may not change at all, a length or area by at most 0.001 px or 0.01 px²,
and an identity item's coordinates by at most 0.001 px. The pipeline is
deterministic (two independent traces of the whole corpus gave identical
measurements), so the tolerances only have to absorb floating-point drift. A
worse record (a larger measurement or an unrecorded item) is a regression the
entry would otherwise hide, and a better one leaves the record too loose, so
either fails the run, as does an unexpected pass; fixing one recorded defect
while adding another of the same size changes the identities and fails too.
Any other exception while checking it is an error. Re-record with:

```bash
.venv/bin/python scripts/record_expected_failures.py
```

It traces the corpus, updates every entry's measurements and identities, and
removes entries that now pass; `--task TIER/CASE:ASSERTION=SLUG` adds an entry
for a new failure, and `--retask TIER/CASE:ASSERTION=SLUG` moves an existing entry
to another task without tracing, for a re-attributed cause. It writes all or nothing: while any failure has no entry, a
record is worse than the table's without `--allow-worse`, a case raises, or an
orphan entry (a case the suite does not trace, or an assertion it does not make
or that has nothing to hold) is kept without `--remove-orphans`, it reports
everything and writes nothing. The table test checks each entry's form,
measurements and identities and rejects `synthetic-finding-` placeholder slugs;
worklog is not available to the tests, so that a slug names a real task is
checked by hand.

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
