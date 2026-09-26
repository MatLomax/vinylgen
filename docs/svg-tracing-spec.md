# SVG Tracing Specification

## Scope

The first tracing milestone converts an existing flattened palette raster into
two deterministic, Inkscape-oriented SVG files for layered adhesive vinyl:

```text
outputs/<subject>/
├── traced-overlap.svg
├── traced-edge.svg
└── trace.json
```

Tracing is a separate command that consumes `flattened.png` and `palette.json`.
It must be possible to pipeline palette processing and tracing, but the tools
remain independently runnable. The first validation set is `desert-mech`,
`crashed-ring`, and `cosmic-jellyfish`. The remaining corpus is deferred until
those three are satisfactory.

The initial compatibility target is Inkscape. Registration marks, production
tiling for large designs, heat-transfer vinyl, stencils, and physical cut tests
are later work.

## Geometry model

Each final palette colour becomes a named Inkscape layer. Disconnected regions
remain separately selectable paths, numbered largest-first with stable names.
Holes are real holes represented by compound paths, not white cover shapes,
masks, or clipping paths.

Every interface between two colours is extracted into a global planar boundary
graph. Pair-specific Lab costs place interfaces at subpixel equal-cost
crossings. The graph retains every dense sample as evidence and performs no
geometric simplification; protected evidence is metadata, never a request to
preserve a pixel staircase. A multi-scale modeller classifies sustained
straight runs, continuously turning curves, and corners: direction jumps
between two smooth sides, straight or curved. Each canonical interface is fitted once, and its neighbour
consumes the exact reverse of that line/Bézier sequence.

The graph's chains are the colour interfaces split only where they must be. A
chain is a maximal run of one colour pair's pixel edges whose interior
vertices all have pair degree two (exactly two of that pair's edges meet
there): it runs from a vertex of other pair degree to the next, or it is a
ring of degree-two vertices. Pair degree is not two where the interface ends
at a third colour or the image edge (degree one) and where two regions of one
colour touch only at a corner (degree four). The chains are the same whichever
edge the walk seeds a chain at, so an interface is never split at an arbitrary
degree-two vertex. Every chain end carries an anchor: a **junction anchor**
where three or more colours meet, else an **endpoint anchor** (the image edge,
a corner contact, and in the live preview a crop edge). A loop that leaves a
vertex of degree four and returns to it is closed but carries one anchor
there, its two ends; a ring of degree-two vertices carries none.

The two SVG variants are:

- `traced-overlap.svg`: lower layers may extend beneath upper layers by a
  configured overlap distance.
- `traced-edge.svg`: boolean subtraction derives final edge-to-edge cut shapes
  from the ordered overlap geometry. Shared edges therefore come from the same
  construction rather than independently fitted curves.

An overlap of zero means colours meet on exactly shared geometry. A positive
overlap extends lower layers beneath upper layers by that distance. The default
is zero. The setting is configurable.

The canvas tightly matches the cropped artwork, with no automatic outer margin.
SVG fills use the exact palette RGB values. Cut paths have no cosmetic stroke;
shared topology, rather than seam-covering paint, guarantees the join.
Gradients, opacity, filters, masks, and embedded raster images are not used for
cut geometry.

### Path node types

Every traced path carries `sodipodi:nodetypes` (namespace
`http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd`, declared on the root), so
Inkscape's node tool shows the same node types Vinylgen reports. Terms follow
[GLOSSARY.md](../GLOSSARY.md) "Path nodes". `vinylgen/node_types.py` classifies
each node from the path data exactly as written (absolute `M`, `L`, `C`, `Z`);
the SVG writer (`svg_writer.add_layer`) applies it to both variants and to the
live preview's SVG.

| Character | Vinylgen term | Inkscape name | Rule |
|---|---|---|---|
| `c` | cusp node | cusp | The two tangent directions are more than **0.5°** from 180° apart; or both sides are straight lines (always, even when collinear); or the node is a junction anchor, where three or more colours meet, or an endpoint anchor, where a chain ends with fewer than three colours: at the image edge, a corner contact, or in the live preview a crop edge (always). |
| `s` | symmetric node | smooth | The tangent directions are at 180° within 0.5°, and the handle lengths differ by more than 1%, or one side is a straight line. A line has no handle, so a line next to a collinear curve is `s`, never `z`. |
| `z` | smooth node | symmetric | Both sides are cubics, the tangents are at 180° within 0.5°, and the handle lengths are equal within **1%** of the longer handle. Two zero-length handles are `s`. |

A line's direction is its tangent. A zero-length cubic handle falls back to the
direction toward that cubic's next control point, then its far end point.
Junction and endpoint anchors are matched by their written coordinate text
(`node_types.cusp_anchor_keys`).

The string follows Inkscape's layout (`PathManipulator::_createTypeString`):
one character per node of each subpath in order. A subpath's nodes are its start
and each segment's end, except that a closed subpath's last segment ends on the
start node rather than adding one; when the last explicit segment ends away from
the start, the implicit closing line is that last segment. A closed subpath
then repeats its start node's character at the end, and the start node is
classified from its closing join. A subpath with no segments has no characters.
Rendering ignores the attribute.

## Layer order

Layer order matters only when lower layers extend beneath upper ones: with
solid underlayers or a positive overlap (`layer_order_matters` in
`vinylgen/tracing.py`). Then the tracer infers a deterministic best-guess
stacking order (`infer_order`): colours touching more of the canvas edge, then
larger colours, sit lower. Ambiguity does not stop the run: the chosen order
and a warning are written to `trace.json`. Layer ordering is independent of the
palette's descending area order.

Without solid underlayers and with zero overlap, every colour meets its
neighbours edge to edge and each layer is exactly its own partition region, so
no stacking changes any cut geometry. The tracer then does not run
`infer_order` and emits none of its warnings; the layers stack in palette
order. `trace.json` records `inferred_bottom_to_top: null` and the reason under
`layer_order.not_inferred`. Across the nine-image corpus, tracing in palette
order instead of the inferred order gave identical path data for every
colour in both SVG variants. The only order-dependent outputs are rendered
rasters: the renderer anti-aliases a shared edge differently depending on which
colour is painted last, so `traced.png` and the semantic-validation render
(and the audit counts derived from it) differ along boundaries by a few
channel levels. Both describe the SVG as written, so they stay consistent with
the delivered file. Because protected-feature recovery reads that render, a
subject with explicit protections could in principle take a different recovery
path in a different stacking; no corpus subject did.

Users can override the complete or partial order in either case; an override is
always honoured, and colours it leaves unnamed follow the automatic order
(inferred, or palette order when order cannot matter). The web Trace form lists
the layer order only when it can matter, and reads the inferred order
(`GET /api/revisions/{id}/automatic-layer-order`, once per revision) only then;
the list stays locked until that order has arrived, a failed read is shown on
the list with a Retry, and a revision with no automatic order (it cannot be
traced as it stands, e.g. its cleanup is stale) keeps the list locked with that
reason. While order cannot matter the web app sends no `layer_order` with a
trace run or live preview, so a hidden override never changes the stacking. The
override stays in the form, and before such a run starts it is written into the
project's persisted trace draft, so it survives a reload and a run from an older
trace, and applies again once solid underlayers or an overlap are set. The user's
own pending draft wins: when the draft already names a layer order, the run does
not overwrite it. When the run succeeds and spends the draft, the order is kept
only if the draft still holds it; an order the user saved while the run was going
is left as the user saved it. A failed or cancelled run leaves the draft as
written. A run where order matters does not touch the draft.

## Palette names and optional colour removal

Palette colours receive automatically generated human-readable names such as
`Burnt Orange`, `Deep Navy`, and `Sage Green`. Hex values remain secondary
identifiers. Names must be unique within one palette and are stored in
`palette.json` so all downstream artifacts use them consistently. Palette-stage
configuration may override generated names.

Tracing can optionally omit one palette colour: it gets no vinyl layer and its
pixels stay in the raster as uncut background. The removed colour is a cleanup
stage setting that the trace applies; the CLI accepts it as a trace flag.
Input accepts either its human-readable name or hex value. When no colour is
configured, all colours are included. The name or hex is resolved once, against
the palette the cleanup runs with, and the web pipeline carries the resulting
palette index to the trace (`removed_colour_index` on the cleanup revision), so
colour names a trace later applies never change which colour is omitted.

## Size, units, and cropping

Physical output dimensions are configurable. A single configured dimension
preserves the source aspect ratio. When a configured width and height imply a
different aspect ratio, the source is centre-cropped evenly on the necessary
pair of sides, removing as little as possible. Stretching is never used.

Cropping occurs before palette inference so the cropped source, flattened
raster, palette shares, report, and SVGs all refer to the same artwork. The
original source image remains unchanged, and the palette stage saves the
derived crop as:

```text
outputs/<subject>/cropped.png
```

Aspect-ratio and crop settings belong to the palette-stage configuration, not
the tracing command.

Trace settings follow one rule: fitting relative to the image, cutting in
millimetres. The physical size is set by the cleanup stage and applies to both
the cleanup thresholds and the traced SVG; in the web workflow the trace shows
it read-only and changing it means running cleanup again.

- The eight trace lengths are in thousandths (‰) of the traced image's longer
  side ([Trace length units](#trace-length-units)): `curve_fit_tolerance`,
  `straight_line_max_deviation`, `minimum_straight_length`,
  `corner_analysis_span` (the analysis scale), `side_smoothness`,
  `corner_sharpening_allowance`, `curve_feature_scale` and `max_deviation`. The traced image is the flattened
  (or edited) raster, with any palette-stage `flatten_upscale` already applied;
  the SVG `viewBox` is the same pixel grid. Setting a physical size never
  changes how curves are fitted. Angles (`corner_threshold`, `snap_angle`) are
  degrees.
- Physical settings are `overlap`, `min_detail_width`, `remove_below_width`
  (lengths) and `min_region_area` (an area). When a width or height is
  supplied they are millimetres and square millimetres, converted to pixels
  through the physical scale; without physical dimensions they are pixels and
  square pixels.

### Trace length units

A trace length is stored and edited in thousandths of the longer side of the
traced raster, so the same artwork traced at 600 px and at 6000 px gets the same
corners, node layout and relative accuracy. Pixel windows would read small
variations of a large diffusion-upscaled image (3000 to 11000 px) as corners and
bends. `TraceSettings.length_unit` names the unit: `per_mille` for thousandths,
`px` for pixels.

**One resolver.** `vinylgen/trace_lengths.py` `resolve_trace_lengths` converts
the lengths to pixels (`value × longer side / 1000`, rounded to 9 decimal
places) before anything is fitted. The trace run, the CLI and the live preview
all resolve with the WHOLE traced image's longer side; the preview never uses
its window's size. The fitter works in pixels only and refuses settings still in
thousandths (`_require_pixels`). An automatic `max_deviation` (unset) resolves
to 1‰; `side_smoothness`, unset, follows `straight_line_max_deviation`, and
`corner_sharpening_allowance`, unset, follows `max_deviation` (automatic
included).
The default rules are tables in `vinylgen/contracts.py` (fixed defaults,
following lengths, automatic lengths), so a new length is one table entry.

**Defaults** (chosen by the user after the calibration comparison,
`.tmp/units-calibration/compare_sets.json`). They reproduce the pixel-era
defaults on a 2000 px image:

| Setting | ‰ | px at 1495 (corpus) | px at 2000 | px at 2990 (2× corpus) | px at 11264 |
|---|---|---|---|---|---|
| Curve-fit tolerance | 0.75 | 1.12 | 1.5 | 2.24 | 8.4 |
| Straight-line deviation | 0.8 | 1.2 | 1.6 | 2.39 | 9.0 |
| Minimum straight length | 2 | 3.0 | 4 | 5.98 | 22.5 |
| Maximum deviation (automatic) | 1 | 1.5 | 2 | 2.99 | 11.3 |
| Analysis scale (`corner_analysis_span`) | 4 | 6.0 | 8 | 11.96 | 45.1 |
| Curve feature scale | 2 | 3.0 | 4 | 5.98 | 22.5 |

The curve feature scale accepts 0.25 to 16‰ as input (the web form and API, the
CLI flag); a converted pixel-era record outside that range still traces as
recorded.

**One-pixel floor.** `curve_fit_tolerance`, `max_deviation` and
`corner_sharpening_allowance` never resolve below 1 px (`FLOORED_LENGTHS`,
`MIN_ACCURACY_PX`), explicit values and pixel settings included, so the trace
and the live preview resolve the same settings to the same pixels. Without it a
600 px image would resolve the default tolerance to 0.45 px, below the sub-pixel
noise of the boundary samples, and the fit would follow that noise with extra
nodes (a synthetic badge: 49 pieces at 600 px against 14 at 2000 and 6000 px).
The floor only matters below about 1333 px at the defaults. The web form's pixel
hint says when a value is held at the minimum. A pixel-era record keeps its
other lengths exactly.

**Straight-run floor.** `minimum_straight_length` always resolves above one
pixel step, to at least `MIN_STRAIGHT_RUN_PX` (1 px plus the 1e-9 px resolution
of a resolved length), explicit values and pixel settings included. A straight
run is accepted when its chord is at least this long, so at 1 px or less every
unit step of a pixel staircase is a straight run and the provisional-corner test
puts a cusp between each pair: the default 2‰ would do that on every image of
500 px or less. The synthetic fidelity corpus measured it (fixed set and pinned
fuzz cases below 500 px, default settings; a kink then counted as a cusp farther
than max(2 px, 2 per mille) from every true corner): with no floor the
hard-edged tier had 10358 kinks and a corner F1 of 0.10, and the anti-aliased
tier 1813 kinks and 0.17. A floor of exactly 1 px left the hard-edged tier unchanged. The floor
just above one step gave 136 kinks and 0.34, and 50 kinks and 0.42. Floors of
1.25 to 2 px lost more real corners than they removed kinks. The web form's
pixel hint says when the length is held above one step.

**Derived windows** keep their ratios to the resolved lengths: corner windows at
1× and 1.75× the analysis scale past a tip skip of 2.41 corner sharpening
allowances, join tangent windows at ¼ and ½, smoothing at half the curve feature scale truncated
at 1×, and the short-edge minimum from the minimum straight length.

**Absolute pixel constants**, each decided:

| Constant | Where | Decision |
|---|---|---|
| Control-point padding cap, 2 px | `_cubic_solve_nb`, `_constrained_cubic_nb`, `_fixed_pair_nb` | Scales: a quarter of the analysis scale (`_pad_cap`), which is 2 px at the pixel-era 8 px span. Controls stay within `max(tolerance, min(¼ extent, cap))` of the sample box. |
| Axis-extreme prominence, 0.5 px | `_unit_candidates_nb` | Scales: `max(0.5 px, curve feature scale / 8)`, 0.5 px at the pixel-era 4 px scale; never below the half pixel of raster noise. |
| Noise floor 0.5 px in `max(0.5 px, tolerance / 2)` | inflection and bend significance | Stays: raster noise is a pixel quantity; the tolerance term already scales. |
| Recovery floor, 0.05 px | `_tightened` | Stays: a floor on a tightened tolerance against raster noise, far below every resolved tolerance. |
| Semantic-audit feature span, 6 px | `validate_semantic_preservation` via `tracing.feature_span` | Scales: `max(6 px, 3 × maximum deviation)`, 6 px at the pixel-era 2 px deviation; a feature shorter than three deviations may legitimately be smoothed away. The 6 px floor and the thin-feature detector's 4 px support radius are raster measurements and stay. |
| Partition absolute tolerance, 1e-6 px² | `partition_watertight_fault` | Stays: floating-point noise; its relative term already scales with the canvas area. |
| Preview margin, 64 px | `trace_lengths.preview_margin` | Computed: `max(64 px, 8 × the longest reach)`, the reach being the largest of the analysis scale plus the corner tip skip, twice the curve feature scale and the minimum straight length (103 px at the pixel-era defaults: 8 × (8 + 4.83)). The multiple is measured, not derived ([Preview margin measurement](#preview-margin-measurement)): no multiple makes the preview match the full trace inside its window, and each smaller one tried leaves more difference in view than 8. |
| Preview area limits (px²) | `PREVIEW_MAX_AREA_PX`, cleanup preview | Stay: they bound the preview's compute cost, which is in pixels. |
| Corner-candidate suppression gap (formerly `index_gap`, a sample count) | `_geometric_corners` | Arc length: two candidates are one corner within half the analysis scale of arc, or when their cusps lie within half the analysis scale (or the sharpening allowance) of each other and their samples within half the scale plus twice the allowance of arc. Correct on sparse rings (buffered overlap rings, a polygon's vertices) and dense chains alike. |
| One-arc noise floor, 0.3 px | `_ONE_ARC_FLOOR` in `_corner_candidates` | Stays: raster noise is a pixel quantity. A corner's one-arc miss must exceed 1.5 × its sides' own error plus 0.3 px ([Corner detection](#corner-detection)). |
| Arc minimum, 5 samples | `_ARC_MIN_SAMPLES` in `_fit_side_nb` | Stays: a count, not a length. An arc has three parameters; through fewer than five samples it follows their noise. |

**Pixel-era records.** Every setting recorded before units is pixels. It is
recognised by its marker, never by magnitude: `length_unit: "px"`, or no
`length_unit` in a record from before units existed (a web revision's trace
settings, a whole `trace.json`, a config carrying `curve_optimization`, or a
web draft that sets a length). A pixel record is converted to thousandths on
the image it traced, so resolving it on that image gives back its pixels
exactly and a re-trace reproduces its geometry:

- absent lengths take the pixel-era defaults (1.5, 1.6, 4, 8 and 4 px), an
  absent `side_smoothness` the recorded straight-line deviation, an automatic
  `max_deviation` the explicit 2 px it resolved to, and an absent
  `corner_sharpening_allowance` that maximum deviation (the pixel era's corner
  move limit);
- a stored `corner_straightness`, the former name of `side_smoothness`, is
  read as `side_smoothness` everywhere a record is read: web drafts, served
  revision settings, web requests, CLI configs and `trace.json`
  (`contracts.upgrade_trace_record`), and `--corner-straightness` is an alias
  of `--side-smoothness`. The lengths are kept exactly, but the corner detector
  changed: a re-trace of an old record finds curve-to-curve cusps, sharpens
  rounds within the allowance, and drops corners that were a smooth curve's
  noise, so its geometry is not the one recorded;
- the web API serves every revision's pixel trace record converted on the raster
  that run traced (read from its traced SVG's `viewBox`), leaving the stored
  record untouched; a record whose raster cannot be read is served marked `px`
  and the form converts it on the current traced grid;
- a web draft saved in pixels converts on the revision's traced grid once it is
  measured and is stored back in thousandths;
- the CLI converts a pixel config on each image it traces (in `trace_subject`).
  A config that sets a length without `length_unit` is rejected, as is a length
  flag (thousandths) over a pixel config;
- the web API requires `length_unit: "per_mille"` in any trace request that sets
  a length, so a client still sending pixels is refused, never misread.

### Preview margin measurement

The live trace preview's margin (`trace_lengths.preview_margin`) was measured
against the whole-image trace (2026-09-25; scripts and results in
`.tmp/preview-margin/`). The inputs were the nine corpus subjects, `sourceup`,
`fox-table` (4096 × 1360), the `fox-owl-table-2` cleanup (6398 × 2133) and
`phoenix-4x-regen-2` (11264 × 7920). Each image had 28 windows: 10 random (8 at
600 × 600, 2 at 300 × 300) and 18 boundary-dense (8 at 600 × 600, 2 at 600 × 300,
4 at 300 × 300, 4 at 150 × 150). Three settings were tried: the defaults, analysis
scale 10‰, and analysis scale 8‰ with curve feature scale 8‰. The reference is
the whole image traced through the preview path, which is the trace run's SVG
path for path.

**Criterion.** A window agrees when every written segment whose control points'
bounding box meets the window, and every node inside it with its node type,
matches the reference within 1e-5 px. The difference the margin controls is
counted per boundary chain: a chain the crop cuts (it has a sample within 1 px of
a crop edge that is not an image edge) is compared, inside the window, against
the same whole chain fitted from the same coordinates, so the count is what the
split alone changes.

**Result.** At the defaults, with windows showing a margin-caused difference
out of 28 and the median cold preview (boundaries plus fit) of the 600 × 600
dense windows:

| Image | 2 reaches | 4 reaches | 6 reaches | 8 reaches |
|---|---|---|---|---|
| `fox-table` | 64 px: 26, 1.60 s | 106 px: 23, 1.81 s | 158 px: 19, 2.30 s | 211 px: 11, 2.64 s |
| `fox-owl-table-2` | 83 px: 28, 0.51 s | 165 px: 27, 0.73 s | 247 px: 22, 0.89 s | 329 px: 20, 1.12 s |
| `sourceup` | 77 px: 26, 0.33 s | 154 px: 23, 0.43 s | 231 px: 16, 0.53 s | 307 px: 8, 0.63 s |
| `phoenix-4x-regen-2` | 145 px: 27, 0.24 s | 289 px: 22, 0.37 s | 434 px: 14, 0.55 s | 578 px: 7, 0.71 s |

On the corpus the 64 px floor governs up to 6 reaches, and 8 reaches (77 px)
takes the count from 222 to 205 of 252 windows. Over all images and settings, 4
reaches left a margin-caused difference in 777 of 1092 windows and 8 reaches in
453. Differences reach up to about 250 px inside the window, at every multiple.
A cut boundary's fit changes along the whole run between its cusps, and those
runs are long in pixels whatever the fit windows are, so no multiple of the reach
(and no formula over its parts) makes the preview agree. Every smaller multiple
shows more difference in view than 8, so the margin stays at 8 reaches.
Shrinking it trades agreement for speed: 4 reaches is 30 to 50% faster on the
large images and makes no difference on the corpus.

**Uncut chains are exact.** The chain fitter is not translation-invariant: a
chain whose samples are translated, or differ by about 1e-13 px, can fit
differently where near-ties resolve the other way (for example, which end of an
exactly flat run takes an axis-extreme node). So the preview builds its crop in
image coordinates (`shared_boundary_polygons` with `origin` at the crop's
corner): each sample is formed as the whole image forms it, integer grid position
in image px plus the same sub-pixel offset, and a chain's identity is its image
grid path. A chain the crop does not cut, meaning none of its samples lies on
or within 1 px of a crop edge that is not an image edge (a chain ending at a
junction on that edge counts as cut: the junction's other colours fall outside
the crop, so it becomes an endpoint), is then the full trace's chain bit for
bit (samples, anchors, colours, identity), and since a chain's fit depends only
on its own samples, anchors and settings, it fits exactly as in the full trace
before protected-feature recovery; chains the full trace re-fits in a trace
fallback are not matched, since the preview skips recovery.
Measured at the current margin over the same images, windows and three settings
(`.tmp/preview-coords/measure3.py`, 2026-09-26): no uncut chain differs
from the full trace (fits compared for float equality; 339,154 checks, one per
uncut chain in view per window and setting), and every window that
still differs has a cut chain that differs. Windows agreeing in full at the
defaults (of 28): corpus 51 of 252, `sourceup` 20, `fox-table` 17,
`fox-owl-table-2` 8, `phoenix-4x-regen-2` 21; over all three settings, 386 of
756 corpus windows.

## Cutter cleanup (raster labels)

Physical/detail cleanup is its own stage between editing and tracing, before
shared-boundary construction and curve fitting. In the web workflow it writes a
`cleanup` revision with cleaned labels and a derived `flattened.png`, so its
cost is paid once and every trace of that revision's boundaries depends only
on the cleaned raster. A cleanup always starts from the raster before any
earlier cleanup, so cleanups never stack. After a flatten or edit that follows
a cleanup, tracing waits until Cleanup is run again. The trace CLI applies the
same cleanup function to an in-memory copy of the labels before tracing and
leaves the subject's files unchanged. Web and CLI both infer layer order
(when it can matter), validate, and draw fallback and warning images against the cleaned labels and
the raster derived from them, which is what was traced. The removed colour is
excluded from the cleanup regions and its pixels are never changed.

The user's explicit protections (`explicit_protected_features`) win over
cleanup. `remove`, and the remove half of `remove_then_widen`, never reassign
an undersized region holding an explicitly protected pixel; it is kept whole
and counted as `protected_kept_regions`. Widening may still grow such a
region, but widening a neighbour never consumes an explicitly protected pixel;
a widening whose every needed pixel is explicitly protected is a refusal,
recorded as unchanged with a warning.
Automatic protection (protected pixels that are not explicit) is cleared
wherever the cleanup changed labels, since that detail was removed on purpose;
the audit counts the automatic protected details (eight-connected components)
and pixels cleared. Explicit protection is carried unchanged. Validation then
checks protection against the cleaned labels with this updated mask. Palette reduction stays separate from cutter
geometry decisions.

Cleanup is optional, configurable, and disabled by default. All removal,
filling, and widening thresholds default to zero, so enabling cleanup without
setting a positive threshold does not alter geometry. Thresholds use
millimetres and square millimetres in physical mode, or pixels and square
pixels when no physical dimensions are configured.

The minimum-detail rule is symmetric: it examines narrow geometry on both sides
of every cut boundary rather than assuming one universal foreground/background
or vinyl/gap side.

One global cleanup action applies across all colour layers:

- `remove`: group touching undersized regions and reassign each group to the
  stable touching colour it shares the most edge with. A colour's shared edge is
  the number of four-neighbour pixel pairs joining a group pixel to a pixel of
  that colour outside every undersized region, so a pixel touching the group on
  two sides counts twice. Colours whose shared edge is at least 90% of the
  longest are a close call, settled by the smallest Lab distance to the group's
  pixel-weighted average colour, then by the lower palette index. An undersized
  group cannot use one of its own members as the destination, preventing
  reciprocal colour swaps.
- `widen`: expand a narrow or undersized region equally into its neighbours
  until it reaches the configured threshold.
- `remove_then_widen`: first remove regions below the configured area threshold,
  or below the separately configured removal-width threshold, then widen
  remaining features below the configured target width. This supports workflows
  such as removing sub-3-mm2 islands and sub-1-mm lines before enforcing a 3 mm
  weedable width.

When cleanup is enabled without an explicit action, it defaults to `remove`.

Widening must not reduce either neighbouring band below the same threshold. If
the requested widening is impossible, the original geometry remains unchanged
and the cleanup summary (`cleanup.json`, or `trace.json` for the CLI) records a
warning. A request to widen must never silently fall
back to removal.

### Live cleanup preview

`vinylgen/cleanup_window.py` cleans one window for the web Cleanup canvas's live
preview (see [architecture](architecture.md)). It runs `cleanup_labels` on the
window plus a 256 px margin of the raster a Cleanup run starts from (before any
earlier cleanup), under the physical size resolved against the whole raster,
with the removed colour excluded and explicit protection honoured as
`apply_cleanup` honours them. Whether a region is undersized depends on its
whole four-connected area and width, which a crop would misjudge for every
region it cuts, so every region of every colour is measured once on the whole
raster (area, feature width and whether it holds an explicitly protected pixel;
none depend on the settings) and the crop reads its regions' decisions from
those measurements. The result differs from a full run only as follows:

- `remove` is exact except for a group of touching undersized regions that
  reaches the crop edge: it is split there and chooses its destination from the
  edge it shares with neighbours inside the crop only. The margin keeps that
  outside the visible window unless such a group spans it.
- `widen`, and the widen half of `remove_then_widen`, is an approximation. It
  grows regions one after another on the crop, so the neighbour colour a region
  grows into, its thin ridges, and the residual-width and residual-area checks on
  the neighbour it would thin are all judged inside the crop, and the order of
  widenings follows the crop. Large background neighbours almost always reach the
  crop edge, and a widening decided differently there changes what every later
  widening sees, so a difference can propagate inward. After a removal the
  regions differ from the measured ones, so the widen half judges regions by their
  part inside the crop. The 256 px margin (the trace preview keeps at least 64 px) is what
  keeps most of this out of view; it is still a crop, so a match is what testing
  showed, not a guarantee (figures below).
- The automatic-protection audit numbers a run records (details and pixels
  cleared) are not computed, and nothing is saved.

On top of the cleaned pixels the preview draws what a Cleanup run with the form's
values would change on the raster the canvas shows now: the active cleanup's
result when the revision has one, else the uncleaned raster. Per pixel, with
`original` the uncleaned label, `current` the shown label, and `after_removal`
and `final` the labels the run would reach after its removal stage and at its
end (`will_change` in `vinylgen/cleanup_window.py`):

- `final == current`: transparent, nothing would change;
- otherwise, `final == original`: blue, the pixel would return to its uncleaned
  colour (the active cleanup changed it and these settings would not);
- otherwise, `final != after_removal`: green, the widening would set it;
- otherwise: red, the removal would set it.

When the form's settings are those of the active (not stale) cleanup that produced
the shown raster, and that cleanup ran under the current cleanup rules, the
full-run result is already known: the preview returns the shown raster's window
and draws nothing, without running the windowed clean. "Those settings" means the
same action and thresholds, the same physical size once resolved against the
raster (to a relative 1e-9, so a size entered as the other dimension still
matches), and the same removed palette colour: the recorded name is resolved
against the palette that cleanup ran with and the form's against the palette the
preview cleans with, so a colour renamed since (a trace's colour names) is not
taken for the same one. "The current cleanup rules" means the cleanup revision
recorded `cleanup_algorithm_version` equal to `CLEANUP_ALGORITHM_VERSION` in
`vinylgen/cutter_cleanup.py`, which increases whenever the same labels and
settings can clean to a different result; a cleanup recorded without a version
ran under version 1. An older cleanup's own settings therefore preview with the
windowed clean, showing what a re-run would now change. Any doubt runs the
clean. With any other settings the windowed clean runs, and because its
widening is an approximation (see above), green and blue highlights can include
approximation noise near regions that continue beyond the crop.

The highlights always compare against the revision's current raster, the cleaned
one with its touch-ups when a cleanup is active, whichever view the canvas shows:
with the Flattened view (the raster before cleanup) or, behind touch-ups, the
Cleaned view selected, they still describe changes from that current raster.

It is always drawn while the live preview is on. What the shown revision's
Cleanup run has already changed is a separate whole-image overlay, the
`cleanup-changes-overlay` artifact (see [files and outputs](files-and-outputs.md)),
drawn while the canvas's "Show changes" switch is on, with or without the
preview (the switch stays usable while the preview is paused). Both can be on
together; since red and green then mean "would change" inside the live window and
"changed" outside it, the canvas controls show a legend line for each overlay on
view ("Live: red would be removed, green widened, blue restored"; "Show changes:
red removed, green widened by the last Cleanup").

With the crop covering the whole raster the preview equals a full run for every
action. The whole-raster measurement runs once per revision, one colour at a
time: each colour's regions are labelled on the colour's bounding box, then its
distances are formed from one exact nearest-background feature transform, a band
of rows at a time, so no whole-raster float64 distance field is ever held. The
widths are bit-identical to the padded EDT a full run uses (checked on every
corpus subject). A newer request interrupts the measurement before a colour's
feature transform or between colours, and the colours already finished are kept;
the transform itself cannot be split without changing the distances, so one
colour's transform (about 0.7 s on `sourceup`, about 2.5 s on 11264 × 7920) is
the longest uninterruptible step. The cleaning of the window is interrupted too:
the remove and widen passes check before each colour, each removal group and each
widened component, so a pan or setting change during a widening of up to about
1.3 s stops it after the current component or colour setup instead of queueing
the newer preview behind it. The longest stretch between checks measured on
600 × 600 windows was about 60 ms on the corpus subjects and 70 ms on `sourceup`
(bounded by the crop, not the raster), and the server notices a dropped request
within 50 ms. The checks never change the result, and a full Cleanup run passes
none.

On the development machine the measurement takes about 0.35 s for a 1495 × 1052
subject, about 6.4 s for the 5980 × 4208 `sourceup` subject (peak about 460 MB
above the loaded raster, 650 MB process total) and about 22 s for an 11264 × 7920
subject (peak about 1.6 GB above the loaded raster, 2 GB process total, with
about 740 MB held afterwards for the raster and its region numbering).

Measured against a full run (`apply_cleanup` on the whole raster) with minimum
detail width 4 px, minimum area 60 px² and remove below 2 px, over eight random
600 × 600 windows (the zoom limit) on each of `griffin-castle`, `sky-whale-city`,
`source-chest-v1-registered`, `griffin-sourceup-2x` and `sourceup`:

| Action | Windows differing, 256 px margin | Largest difference | Deepest inside the window | With a 64 px margin |
|---|---|---|---|---|
| `remove` | 0 of 40 | – | – | 0 of 40 |
| `widen` | 7 of 40 (0, 1, 0, 3, 3 per subject) | 307 px | 214 px | 30 of 40 |
| `remove_then_widen` | 12 of 40 (0, 1, 4, 4, 3) | 1071 px | 241 px | 31 of 40 |

For scale, a full run changed a median of 120 to 14 000 px per window,
depending on the subject and action. Per-update time for a 600 × 600 window (cleaning plus
PNG encoding, measured alone) is 22 to 47 ms for `remove`; `widen` takes a median
of 0.55 to 1.18 s per subject, up to 1.33 s (`sky-whale-city`), and
`remove_then_widen` a median of 0.58 to 0.87 s, up to 0.98 s. Widen therefore
exceeds the 0.9 s target on the 1.5-megapixel subjects. A smaller zoom limit
barely helps, because the 256 px margin dominates the crop: at 400 × 400,
`widen` still took up to 0.98 s on `sky-whale-city`. The preview keeps the trace
preview's 600 × 600 limit.

The preview thread holds one raster at a time: the uncleaned raster with its
measurements, or the traced segmentation of the Trace preview. While the revision
shows a cleaned raster its labels are held beside the uncleaned one, 2 more bytes
per pixel (about 50 MB on `sourceup`, 180 MB on 11264 × 7920), loaded before the
windowed clean and dropped as soon as a previewed revision shows the uncleaned
raster itself. Alternating
between the Cleanup and Trace canvases with both previews on therefore reloads
each time, and each return to Cleanup measures the whole raster again (about
6.4 s on `sourceup`, 22 s on 11264 × 7920) before its first preview appears.

So that the wait is explained, the server reports the measuring at
`GET /api/revisions/{id}/cleanup-preview/regions` as `{"measured", "measuring"}`:
whether the preview thread holds the measurements of the raster a cleanup
preview of that revision cleans, and whether it is building them now. The read
is answered on the event loop from the thread's state, never queued behind the
preview it reports on, so it adds nothing to the preview's time. A trace preview
that evicts the raster turns `measured` false again, and a measurement stopped
by a superseded request stops reporting `measuring`. While a cleanup preview is
in flight the canvas reads this state as the request starts and every 250 ms
until it settles; while the server reports `measuring` for the revision, the
badge reads "Measuring regions of the whole image…" instead of
"Updating live preview…". A superseded request leaves that text standing until
the next request's first read, since its measuring may still be running (a
colour's distance transform always runs to its end). With cleanup off, or with
the shown cleanup's own settings, nothing is measured and the badge keeps
"Updating live preview…".

## Trace accuracy and complexity

Maximum contour deviation (`max_deviation`) is configurable in thousandths of
the traced image's longer side. Its automatic value is 1‰ (2 px on a 2000 px
image) in both pixel and physical mode, and `trace.json` records it and the
pixels it resolved to. It does two things, and is the default of a third
setting:

- It caps both fit bounds: curve fitting uses `min(curve_fit_tolerance,
  max_deviation)` and straightening `min(straight_line_max_deviation,
  max_deviation)`, so only a value below one of them changes the fit
  (tighter, more nodes).
- It is the threshold of the post-fit semantic audit. Lower values flag more
  features as drifting and trigger local recovery of explicitly protected
  details sooner; higher values relax the audit.
- The corner sharpening allowance follows it unless set
  (`corner_sharpening_allowance`, [Corner detection](#corner-detection)), so by
  default a sharpened corner lies no further from the drawn edge than the
  audit allows. The allowance is independent: setting it leaves the fit and
  the audit alone.


The default curve-fit tolerance is 0.75‰, 1.5 px on a 2000 px image. There it
clears the half-pixel sampling error of a one-pixel staircase, allowing it to
collapse into a broad Bézier while staying inside the 2 px maximum-deviation
audit. Pixel figures in the rest of this section are at the pixel-era defaults.

Straightening is controlled by `straight_line_max_deviation` (default 0.8‰,
no upper limit, capped at `max_deviation`). A run between two nodes becomes one
straight segment, its chord, when that chord is at least
`minimum_straight_length` long and every sample of the run lies within the
capped deviation of the chord as a segment (a sample beyond either end counts
its distance to that end). The test is on the chord because the chord is what
is written: a bowed run can lie within the deviation of its best-fit line while
its chord misses the middle by twice as much, and a run that folds back on
itself, such as a whole hairline ring from its seam round to it again, lies
along one line but is not one segment. Every other run is fitted within the
curve-fit tolerance. Higher values straighten
more. At 0 the straight-line shortcut applies only to exactly straight runs,
but the curve fitter may still emit a line for a piece that bows less than the
curve-fit tolerance, because a line within tolerance is a valid fit (at the
1.5 px default, a 40 px arc with a 1 px bow becomes one line). Lower the
curve-fit tolerance to keep such gentle bows as curves.

**What the fit guarantees at each sample.** Distances are to the path as
written, after corner sharpening, straight-line snapping and every node move:

- A straight run (a run that passed the straight-line test above, or a line
  the fitter found inside a curve run and snapped): every sample it replaces
  lies within `min(straight_line_max_deviation, max_deviation)` of its
  segment.
- Every other piece, including an unsnapped line inside a curve run: every
  sample lies within `min(curve_fit_tolerance, max_deviation)` of it.
- A confirmed corner's own sample lies within `corner_sharpening_allowance` of
  the corner node, wherever the node ended up (the detector's cusp, or a snap's
  intersection). The allowance may exceed `max_deviation`, and then that one
  sample may lie further than `max_deviation` from the path; its neighbours
  may not: the lines and curves on either side hold every other sample to
  their own bounds above, measured from the moved node.
- Junctions, chain ends, recovery constraints and other forced nodes never
  move, so the path passes through their samples.

So with the allowance at or below `max_deviation` (its default), no sample lies
further than `max_deviation` from the path. Local recovery only tightens these
bounds, and a final dense fallback passes through every sample.

Corner detection has its own smoothness limit, `side_smoothness` (‰, formerly
`corner_straightness`). A hard corner needs a smooth side on each side of it,
each within this distance of its best-fit circular arc (a straight line
included) and at least `minimum_straight_length` long ([Corner
detection](#corner-detection)). Lower values confirm corners only between clean
sides; higher values let rougher sides form sharp corners. It is independent of
`straight_line_max_deviation`, so lowering the deviation does not soften
corners. When omitted (`null` or absent) it resolves, once, at settings
construction, to the straight-line deviation, which reproduces the corners of
stored settings that lack the field; `trace.json` records the resolved value.
Consequently a CLI run or config that sets `straight_line_max_deviation` to 0
without `side_smoothness` also disables most corner detection; pass
`--side-smoothness` (for example 0.8) to keep corners. The web client default
is 0.8‰, and it loads stored settings that lack the field with their recorded
straight-line deviation. Local recovery halves it together with the
straight-line deviation at the reclassification stage.

The cleaned flattened raster is authoritative. A surviving explicitly
protected feature is recovered locally and in a fixed order: tighten its curve
fit, reconsider its line/curve classification, add one non-corner fit
constraint, and finally emit the dense boundary only when every modelled form
still fails validation. Every attempt and final fallback is recorded. Both
adjacent colours reuse the same recovered chain. `trace-fallbacks.png` is
emitted only for final dense fallbacks, together with its transparent RGBA
twin `trace-fallbacks-overlay.png` (the tightened chains only, on the traced
pixel grid; see [files and outputs](files-and-outputs.md)).

Straight classification has its own maximum residual and minimum sustained-run
length; it is independent of Bézier tolerance. Corner classification uses a
configurable analysis scale (`corner_analysis_span`, 4‰ by default, 8 px on a
2000 px image). It is relative to the traced image, never to the physical
size. Direction is measured across that wider neighbourhood rather than from
individual incident segments, so pixel staircases are treated as sampled
diagonals or curves while sustained corners remain sharp.

### Corner detection

The corner detector (`_geometric_corners`) is the only source of cusp nodes
other than junctions, so `corner_threshold` has exactly one meaning: the turn a
confirmed corner must reach. A corner is a direction jump between two smooth
sides, straight or curved, so a crescent's tip, a leaf's point, a gothic
arch's apex and a heart's notch are corners as well as a building's. A sample
is a corner when all of these hold (`_corner_candidates`):

- **Smooth sides past the tip, at two scales.** Each side window starts past
  the **tip skip** (2.41 corner sharpening allowances of arc, below) and runs
  `corner_analysis_span` (the span) further, and again 1.75 times the span
  further. Each window is fitted with a circular arc (Taubin's algebraic circle
  fit, closed form; a straight line is the arc of infinite radius) and must be
  at least `minimum_straight_length` long, within `side_smoothness` of its arc,
  and turn by at most 90 degrees along it. The arc is used only when it fits at
  least 20% closer than the side's straight line and has five or more samples:
  a pixel staircase's zigzag is about as far from any arc as from its line, so
  a curvature fitted to it is noise. A span-scale side that fails (it runs
  round another feature, such as the floor of a narrow slot) is judged on its
  nearest `minimum_straight_length` past the tip instead.
- **The turn.** The angle between the two arcs' forward tangents where they
  meet (their intersection nearest the sample) reaches `corner_threshold` at
  both scales.
- **Two arcs, not one.** One arc fitted through both long sides and the tip
  misses by more than 1.5 times the worse side's own error plus 0.3 px. A
  circle's two sides are one arc, so a circle has no corner however tight
  (radius 3 to 100 px, at thresholds of 30, 45 and 60 degrees, and at every
  image size); nor has a pixel staircase or a gentle curve. Without this test,
  noise in each side's fitted curvature, carried across the tip, gave
  half-pixel-snapped and rasterised circles corners at every image size. It
  replaces the former scale-invariance check (long-scale turn at most 1.2 times
  the span-scale turn), which the line-sided detector needed because a curve's
  turn between straight windows grows with the window; with arc sides that
  check is redundant.
- **The cusp.** The long-scale arcs' meeting point is the corner node, and it
  must lie within `corner_sharpening_allowance` of the sample (‰, default the
  maximum deviation, independent of it). The long scale is used because its
  arcs are the steadier estimate and reach furthest past a round.

**Rounded corners.** A round is sharpened into a cusp exactly when the sharp
point stays within the allowance: a 90-degree round of radius r lies
r(√2 − 1) from its sharp point, so rounds up to (1 + √2), about 2.41,
allowances in radius snap sharp (a shallower turn admits a larger radius). The
tip skip is that radius, so the side windows start past such a round's
tangent points (0.79 r of arc from its middle) and the smoothness and turn
tests never veto a round the allowance would snap. A larger round stays round:
its sides stay straight, the curve begins at symmetric nodes, and no cusp is
placed (the sides' meeting point lies beyond the allowance).

Neighbouring candidates of one physical corner are suppressed by arc length:
two candidates are one corner within half the span of arc, or when their cusps
lie within half the span (or the allowance) of each other and their samples
within half the span plus twice the allowance of arc (on sparse samples, such
as a polygon's vertices or a buffered overlap ring, both samples beside one
corner propose it). The candidate nearest its cusp wins (the apex, or the
middle of a round), then the smoother sides, then the stronger turn.

**Short edges between corners.** Corners are confirmed in rounds. A side
window stops at a neighbouring confirmed corner, and at a *provisional*
corner: a sample that turns by the threshold between two straight runs
`minimum_straight_length` long (the shortest-edge form of the test, ranked by
straightness). A neighbour closer than `minimum_straight_length` is the same
corner, not a stop. A window cut short keeps the last
`minimum_straight_length` before the stop, starting nearer than the tip skip
when it must, so the short edge up to that stop is the side at both scales;
it counts when it is at least `minimum_straight_length` long and within
`side_smoothness`. A candidate whose two sides both stop at provisional
corners stands only if both of those are candidates too (within half the span
of arc, or the next sample): a small polygon's corners support each other; a
tight curve proposes provisional corners all round, but its noise-level passes
do not. Rounds repeat, re-testing the samples whose windows reach a new stop,
until no corner is added. So an H-shaped region, a rectangular notch, or a bar
end with an edge of 4 px (the pixel-era minimum straight length) or more gets
separate 90° corners instead of a U, and an edge under 4 px still rounds off.

At the pixel-era lengths squares and triangles to hexagons keep every corner
(a hexagon turns by exactly 60°, so only below a 60° threshold).

A closed interface (an island ring) is analysed as a ring: the side windows
run across its seam, so a corner at the seam is found like any other.

**Limits.** On a pixel-edge ring (the staircase outline of a mask, before
sub-pixel boundary evidence) a thin tip is blunted by whole pixels, so its
sharp point may lie beyond a 1 to 2 px allowance and it stays round; and a
round within about a pixel of the allowance limit may go either way.

### Joins: only corners and junctions are cusps

Terms follow [GLOSSARY.md](../GLOSSARY.md) "Path nodes"; how the written SVG
labels each node is in [Path node types](#path-node-types).

A node is a **cusp node** only at a confirmed corner, at a junction anchor
(three or more colours meet) or at a chain end (a closed chain's anchor
included). Every other node is a **symmetric node**
whatever its local angle: recursive split points, joins between classified
runs, forced nodes and ring seams. Both sides share one tangent direction;
handle lengths are fitted independently, so they are not forced equal.

- The chain is split at its corners and junctions into runs fitted with free
  end tangents. An open chain's two ends are where the chain ends; the chain
  that continues from there is fitted on its own. Where a chain ends at the
  image edge (an endpoint anchor), the frame cuts the shape, so that node is a
  real corner of the cut outline even though no detector confirmed it; where
  it ends at a corner contact (an endpoint anchor where two regions of one
  colour touch only at a corner), the outline turns there to pass the contact.
- A recovery fit constraint (and any other anchor inside a chain) is a
  **forced symmetric node**: the fit passes through it and both sides share
  one tangent there. Protected-feature recovery does not need a free tangent
  at the constraint: the constraint's job is to pin the fit to a sample near
  the lost feature, which a symmetric node does; on `sourceup`, the only
  corpus image that needs recovery, the outcome is the same with symmetric
  and with free-tangent constraints.
- A closed chain's anchor, where the loop leaves a corner contact or junction
  and returns to it, is a cusp: those are the chain's two ends. A closed chain
  with a corner, junction or anchor is fitted from its first one round
  to it again, so its path starts there. One with none is fitted as a ring from
  its first forced node (else its canonical first sample), and that seam is a
  symmetric node like any other.
- A straight run meeting a curve gives the node its direction: the line stays
  straight and the curve's handle continues it. Two straight runs never meet
  at a symmetric node (a node between two lines is always a cusp), so two
  straight runs meeting anywhere but a corner or junction are refitted as one
  curve run across the join.
- Between two curves at a forced node or ring seam the tangent is the
  total-least-squares direction of the samples within half the analysis span
  on both sides of the node. Nodes inside a curve run are placed at chosen
  candidate points with their own tangents ([Node placement](#node-placement)).

The fitter first runs the unconstrained line/cubic split to find straight
pieces at least `minimum_straight_length` long; everything else is fitted as
curve runs whose nodes are placed as described in [Node placement](#node-placement).
A straight run whose neighbouring curve would have to turn back to leave or
reach it along its direction (a sharp turn the detector did not confirm)
becomes part of the curve run instead. Only a run with two free ends may become
a line. Every piece meets `min(curve_fit_tolerance, max_deviation)` at its
samples; constraints add nodes, never looseness. Classified corners remain
unrounded. The live trace preview uses the same fitter.

### Node placement

Nodes inside a curve run go where a person drawing the path would put them,
and as few as the tolerance allows (`_place_nodes_nb` in
`vinylgen/curve_fit.py`).

**Candidates.** The run's samples are smoothed with Gaussian weights over arc
length, standard deviation half of `curve_feature_scale` (default 2‰, 4 px on
a 2000 px image),
truncated at the scale on each side (the window may reach past the run but
never past a cusp). On the smoothed curve:

- **Axis extremes**: extrema of the smoothed x and y that rise at least
  `max(0.5 px, curve_feature_scale / 8)` above the curve on both sides before a
  more extreme point (persistence prominence). The node's tangent is exactly vertical (an x extreme) or
  horizontal (a y extreme).
- **Inflections**: extrema of the smoothed tangent angle whose lobes each turn
  by at least 10 degrees. The tangent is the total-least-squares direction of
  the raw samples within the feature scale.
- **Points of tightest bend** (with `nodes_at_bends`, default on): curvature
  peaks (curvature measured over half the scale) rising at least
  `1 / (4 * curve_feature_scale)` per px above the gentlest curvature on either
  side. The tangent is the smoothed tangent.
- The run's two ends with their given tangents (free at a cusp).

An inflection or bend is *significant* when it also stands out by
`max(0.5 px, tolerance / 2)`: each lobe of an inflection bulges that far from
its chord, and a bend's region (between the gentlest curvature on either side)
bulges that far and turns by at least 20 degrees. The others are *minor*: the
ripple of a pixel staircase (a few tenths of a pixel high, 10 to 20 px long)
still marks where the boundary changes direction, and on long gentle edges
those positions let a route span the edge in far fewer pieces than fallback
splits (dropping them cost 3 to 4% more nodes on the corpus). Minor candidates
rank below significant ones in ties. No candidates are evenly spaced.

**Pieces.** Every candidate has a fixed tangent, so the cubic between any two
is fitted on its own: handle lengths by least squares at chord-length sample
parameters, refitted at up to four Newton-improved parameterisations when it
misses the tolerance by less than 4 times. Handles stay between a tenth of the
chord and the chord, inside the padded sample box (the samples' box grown by
`max(tolerance, min(¼ of its extent, ¼ of the analysis scale))`), and **inside their tangent
triangle**: where both tangents turn the same way (a C-shaped piece) each handle
is at most as long as its ray to where the two tangent rays meet, and tangents
turning by 180 degrees or more (parallel rays or an apex behind a node) cannot
be one piece. So a circle gets four nodes at its axis extremes, not two. The
triangle rule costs about 0.7% more nodes on the corpus. A piece is acceptable
when every sample is within the tolerance and it never turns back along its
chord; a piece between two adjacent samples is always acceptable (it has no
interior sample to split at): its handles are shortened so it bows at most half
the tolerance from its chord, and when a node's tangent opposes its chord it
doubles back by a fraction of a pixel (such a piece counts as a handle defect,
so routes avoid it). A two-free-end run straight within the tolerance
is one line. "Straight within the tolerance" is measured to the chord as a
segment: a sample beyond either end counts its distance to that end, so a run
that folds back is never taken for a line through its chord.

**Selection.** A shortest path over the acceptable pieces gives the fewest
segments. Ties go, in order, to fewer handle defects (handles held at one of
the bounds above, or crossing handle legs), then to preferred node kinds, then
to the smaller summed error. `node_placement` sets the kinds:

| `node_placement` | UI label | Rule |
|---|---|---|
| `prefer_extremes` (default) | Fewest nodes, prefer extremes | Ties prefer axis extremes, then inflections, then bends, then minor candidates, then fallback splits. Extremes are preferred, not required. |
| `at_extremes` | Always at extremes | As above, and no piece may skip an axis extreme: every extreme is a node, even when that adds nodes. |
| `fewest` | Fewest nodes only | No kind preference in ties, and the split points of the worst-sample recursion over the whole run are extra candidates. |

For speed, the search tries the pieces ending at a candidate from the nearest
start backwards, and stops after 6 consecutive starts that miss by more than
3 times the tolerance (on the corpus this changes 4 of 61 000 nodes against a
full search); a pair whose samples' smoothed tangent turns by more than 270
degrees between them is not tried (a chord-monotone piece cannot follow such a
turn); Newton refits run only for near misses that could still tie or beat the
best route found.

**Fallback.** Only where no route of candidates spans a stretch: the gap after
the furthest reachable candidate gets the split points of the worst-sample
recursion across it (split at the worst-fitting sample, tangent chosen as
below, until each part is an acceptable piece), and the path is recomputed with
them. Those pieces always form a route, so accuracy never loosens, and the
search always keeps them, whatever its speed limits above would skip: on a
ring narrower than the tolerance (a hairline sliver, a loop a few pixels
round) the smoothed tangent can turn by more than a full circle between
samples that one piece fits. Each fallback therefore makes a later sample
reachable, so the search ends after at most one fallback per sample. A split
point's tangent starts as the total-least-squares direction within half the
analysis span; the direction within a quarter span and the chord of the two
neighbouring samples are also tried, a golden-section search within 20
degrees of the best refines it, and the tangent kept is the one whose worse
half has the smaller maximum sample error.

**Ring seams.** A closed chain without corners, junctions or forced nodes is
fitted once from its canonical first sample, then again from the most natural
node that fit placed (an axis extreme, else a line join, an inflection, a
bend, then any node; the earliest such sample) with that node's tangent at the
seam; the refit is kept unless it has more pieces. So an isolated island's
seam is not an arbitrary extra node.

**Straight lines** (`straight_line_snap`, `snap_angle`). With `axis` (default,
"Horizontal & vertical") or `axis_diagonal` ("Also 45°"), a straight line whose
direction is within `snap_angle` degrees (default 2, 0.5 to 10) of an allowed
direction becomes exactly horizontal, vertical or diagonal. `off` keeps the
measured angle. A snap never moves a line further than
`min(straight_line_max_deviation, max_deviation)`: every sample of the run
must lie within it of the snapped segment as written, between its end nodes
where they end up, or the line keeps its angle. Positions stay shared:

- Junctions, chain ends (including image-edge endpoints), recovery fit
  constraints and other forced nodes never move. A line touching one passes
  through it; a line between two of them snaps only if they already lie on one
  exact line.
- A line with no fixed end passes through the mean of its samples.
- Confirmed corners may move: a corner between two snapped lines moves to their
  intersection, and one beside a single snapped line moves onto it (its
  nearest point). It must stay within the corner sharpening allowance of its
  sample (as for the detector), and an unsnapped straight neighbour's chord,
  redrawn to the moved corner, must keep its samples within the capped
  straight-line deviation; otherwise the snap furthest from its direction is dropped and the
  rest re-solved. Two parallel snapped lines cannot share a corner.
- A straight run inside a curve run (between two curve joins) moves its joins
  onto the snapped line; the neighbouring curves then start there with the
  line's exact direction, so the joins stay symmetric. A run that ends at the
  section's end keeps that end fixed.

Each chain is fitted once and both colours use that fit, so a snapped line is
identical in both. `ChainFit.corners` records where each corner node ended up.
Only straight runs snap; a short piece that is a line only because its chord is
within the curve tolerance does not.

Path coordinates are written with 10 significant digits, so a symmetric node's
two handles stay collinear within the 0.5° that `sodipodi:nodetypes` allows
after rounding.

Node-count limits are initially soft limits. Accuracy takes precedence over an
arbitrary node budget; excessive per-path, per-layer, or whole-document counts
produce warnings rather than additional lossy simplification.

The implemented backend uses the persisted segmentation confidence to construct
one shared sub-pixel boundary graph, then emits deterministic line and cubic
Bezier sections. Independent per-colour bitmap tracing is not part of the
pipeline because it cannot guarantee identical shared interfaces.

## Performance

The tracer is built to scale to sources of several tens of megapixels while
keeping its output a deterministic function of the segmentation and settings.
Every acceleration below reproduces the reference formulation bit for bit;
`tests/oracles/` holds frozen copies of the reference implementations and the
fuzz tests compare against them.

- Boundary-graph construction collects grid edges and sub-pixel crossings with
  vectorised numpy, walks each colour pair's edges into chains with a Numba
  kernel that replicates the lexicographic tie-breaks of the reference walk,
  and runs the per-colour and included-face GEOS unions on threads, with the
  coverage union following once every colour geometry exists.
- Ring nesting queries an STRtree of representative points with each prepared
  ring, so containment work is proportional to the nesting depth rather than
  quadratic in the ring count.
- Chain fits are cached by canonical geometry and settings in one fitter that
  persists across the recovery passes; overlap-buffer rings share that cache
  and the same worker pool as the shared chains.
- Per-layer overlap buffers and solid-underlayer stacking geometry derive from
  the fixed partition, so they are built once on threads before recovery.
- Validation uses a lookup-table sRGB decode, a Numba two-sided ridge kernel,
  one candidate distance field per rendered pass shared by every feature
  metric, and a cache of the protected-feature invariants (labelling,
  nearest-component map, corridors, widths) reused by every pass.
- Cutter cleanup evaluates feature widths from one padded distance field per
  colour and checks neighbour residuals with an exact windowed update instead
  of relabelling and re-transforming the full image per widened region.

The `traced.png` preview targets 768 DPI, but `vinylgen/preview.py` caps its
longer side at 1920 px: it is an inspection image, so large sources render at
roughly 1080p instead of gigapixel size. The same policy sizes the Inkscape
reimport preview. On the development machine a 5980×4208 source traces in
roughly 47 seconds and corpus subjects (about 1.5 megapixels) in 8 to 12
seconds; the Inkscape preview render is under a second of that. Inkscape 1.2.2 does not render very large previews
deterministically, so byte identity of `traced.png` is only asserted at
capped sizes.

## Live window preview

`vinylgen/trace_window.py` traces one window of the segmentation for the web
Trace canvas's live preview (see [architecture](architecture.md)). It crops the
labels, palette costs and protected evidence to the window plus a hidden
margin and runs the unmodified vector stage on the crop: the shared-boundary
graph, the canonical chain fit, the overlap/solid-underlayer layer construction
and the SVG layer writer. Its settings resolve to pixels exactly as a trace
run's do, on the whole image's longer side, never the window's. The margin is
`max(64 px, 8 × the longest fit reach)` ([Trace length units](#trace-length-units)). Layer order, when it can matter, is
inferred once from the whole image; otherwise the layers stack in palette order
as in a full trace. The result is only a preview:

- Boundaries crossing the crop edge end there, so their fit near that edge can
  differ from a full run's; the margin keeps most of that outside the visible
  window. Underlayer adjacency and the overlap buffer likewise see only the crop.
- Validation and protected-feature recovery are skipped, so explicitly protected
  details are not tightened as a full run would, and no audit is written.
- Nothing is saved; a trace run still creates the revision and its artifacts.
- The preview SVG carries `sodipodi:nodetypes` like a full trace (junction
  anchors from the crop's own boundary graph), so the canvas's Nodes overlay
  marks the preview's nodes inside its window.

With the crop covering the whole image the preview reproduces the full trace
exactly. On the development machine a warm 600 × 600 window of the densest area
of the 5980 × 4208 `sourceup` subject or of an 11264 × 7920 source traces in
about 0.25 s (boundaries about 0.2 s, fit and SVG about 0.05 s; solid underlayers
add about 0.25 s), and a settings-only change re-fits cached boundaries in about
0.05 s. Loading a segmentation the first time costs about 0.8 s and 3.2 s
respectively.

## Configuration

The tracing command supports both individual CLI flags and a reusable JSON
configuration file. CLI arguments override values from the configuration file.
Settings and effective defaults are recorded in `trace.json`. Length flags are
thousandths of each traced image's longer side (`--help` says so); a
configuration names the unit of its lengths (see [Trace length
units](#trace-length-units)).

The node-placement settings are `--node-placement`
(`prefer_extremes`/`at_extremes`/`fewest`), `--nodes-at-bends` /
`--no-nodes-at-bends`, `--curve-feature-scale` (‰, 0.25 to 16),
`--straight-line-snap` (`off`/`axis`/`axis_diagonal`) and `--snap-angle`
(degrees, 0.5 to 10); out-of-range values are rejected. Configurations
recorded before these settings existed load with the defaults.

Naming of exact flags and JSON keys is an implementation detail, but public
names should make geometric behavior explicit and must include units where
applicable. Do not use ambiguous foreground/background terminology for the
symmetric detail controls.

## Audit report

`trace.json` records at least:

- Source artifact paths and dimensions.
- Effective tracing configuration and the cleanup settings used (size, removed
  colour, cleanup). `settings` holds the lengths in thousandths
  (`length_unit: "per_mille"`, an automatic maximum deviation filled in);
  `lengths` records the reference (`reference_px`, the traced raster's longer
  side) and every length in thousandths (`per_mille`) and in the pixels it
  resolved to (`px`).
- Tracing backend and version.
- Physical size or pixel-mode status.
- Included and omitted palette colours and their names.
- Inferred layer order (or why it was not inferred), the effective order, user
  overrides, and ordering warnings.
- Path and node counts per component, layer, and document.
- Compound-path and hole counts.
- Regions removed, widened, or left unchanged with warnings, when the CLI
  cleaned inline (otherwise in the cleanup stage's `cleanup.json`).
- Rendered comparison metrics against `flattened.png`.
- Local boundary-fallback locations and effective tolerances.
- Structural validation results.

## Validation

The initial milestone stops after automated validation and visual inspection in
Inkscape. It does not claim physical cutter readiness.

Automated checks must cover:

- Valid SVG/XML readable by Inkscape.
- Deterministic output for identical inputs and settings.
- Expected named palette layers and exact fill colours.
- Separately selectable disconnected components in stable largest-first order.
- Preserved holes using compound paths.
- No open cut paths, self-intersections, duplicate paths, or unsupported SVG
  effects.
- Consistent shared boundaries in `traced-edge.svg` with no gaps or overlaps.
- Correct overlap amount and stacking in `traced-overlap.svg`.
- Correct optional colour omission.
- Canvas, crop, aspect ratio, physical dimensions, and `viewBox` behavior.
- Minimum-width and minimum-area cleanup behavior, including constrained widen
  warnings.
- Raster comparison against `flattened.png`, distinguishing antialiasing from
  genuine geometry deviation and separately accounting for configured vector
  cleanup.

Visual inspection initially focuses on:

- `desert-mech`: clean, low-detail mechanical silhouettes.
- `crashed-ring`: detailed shared geometric boundaries.
- `cosmic-jellyfish`: intricate curves and high palette complexity.

## Deferred work

- Registration marks, weed boxes, and alignment frames.
- Production tiling for artwork larger than available vinyl sheets.
- Heat-transfer vinyl mirroring and material-specific behavior.
- Stencil construction.
- Hard node-count constraints or automatic accuracy degradation.
- Physical cut and weeding tests.
- Full nine-image corpus sweep.
