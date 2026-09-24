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
straight runs, continuously turning curves, and corners supported by coherent
lines on both sides. Each canonical interface is fitted once, and its neighbour
consumes the exact reverse of that line/Bézier sequence.

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

Trace settings follow one rule: fitting in pixels, cutting in millimetres. The
physical size is set by the cleanup stage and applies to both the cleanup
thresholds and the traced SVG; in the web workflow the trace shows it read-only
and changing it means running cleanup again.

- Curve-fitting settings are always in pixels of the traced image:
  `curve_fit_tolerance`, `straight_line_max_deviation`,
  `minimum_straight_length`, `corner_analysis_span`, `corner_straightness`,
  and `max_deviation`.
  The traced image is the flattened (or edited) raster, with any palette-stage
  `flatten_upscale` already applied; the SVG `viewBox` is the same pixel grid.
  Setting a physical size never changes how curves are fitted.
- Physical settings are `overlap`, `min_detail_width`, `remove_below_width`
  (lengths) and `min_region_area` (an area). When a width or height is
  supplied they are millimetres and square millimetres, converted to pixels
  through the physical scale; without physical dimensions they are pixels and
  square pixels.

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
  part inside the crop. The 256 px margin (the trace preview keeps 64 px) is what
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
one when a cleanup is active, whichever view the canvas shows: with the Flattened
or Edited view selected (the raster before cleanup) they still describe changes
from the cleaned raster.

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

Maximum contour deviation (`max_deviation`) is configurable in pixels of the
traced image. Its default is 2 px in both pixel and physical mode, and
`trace.json` records the effective pixel value. It does three things:

- Curve fitting uses `min(curve_fit_tolerance, max_deviation)`, so only a
  value below the curve-fit tolerance changes the fit (tighter, more nodes).
  It does not limit straight lines: a run that passes the straight-line test
  becomes a line on `straight_line_max_deviation` alone.
- It is the threshold of the post-fit semantic audit. Lower values flag more
  features as drifting and trigger local recovery of explicitly protected
  details sooner; higher values relax the audit.
- It limits how far corner analysis may move a corner sample to the
  intersection of the adjacent straight runs. Lower values reject corners that
  would move further.


The default curve-fit tolerance is 1.5 px. It clears the half-pixel sampling
error of a one-pixel staircase, allowing it to collapse into a broad Bézier
while staying inside the 2 px maximum-deviation audit.

Straightening is controlled by `straight_line_max_deviation` (default 1.6 px,
no upper limit). A run at least `minimum_straight_length` long whose robust
best-fit line stays within this deviation becomes one straight segment; every
other run is fitted within the curve-fit tolerance. Higher values straighten
more. At 0 the straight-line shortcut applies only to exactly straight runs,
but the curve fitter may still emit a line for a piece that bows less than the
curve-fit tolerance, because a line within tolerance is a valid fit (at the
1.5 px default, a 40 px arc with a 1 px bow becomes one line). Lower the
curve-fit tolerance to keep such gentle bows as curves.

Corner detection has its own straightness limit, `corner_straightness` (px).
A hard corner needs a coherent straight run on each side, each within this
distance of its best-fit line and at least `minimum_straight_length` long,
examined over `corner_analysis_span` and turning by at least
`corner_threshold` degrees. Lower values confirm corners only between clean
straight edges; higher values let rougher edges form sharp corners. It is
independent of `straight_line_max_deviation`, so lowering the deviation does
not soften corners. When omitted (`null` or absent) it resolves, once, at
settings construction, to the straight-line deviation, which reproduces the
corners of stored settings that lack the field; `trace.json` records the
resolved value. Consequently a CLI run or config that sets
`straight_line_max_deviation` to 0 without `corner_straightness` also disables
most corner detection; pass `--corner-straightness` (for example 1.6) to keep
corners. The web client default is 1.6 px, and it loads stored
settings that lack the field with their recorded straight-line deviation.
Local recovery halves it together with the straight-line deviation at the
reclassification stage.

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
configurable eight-source-pixel analysis span by
default. This is an analysis scale, so it remains in source pixels even when
physical dimensions are configured. Direction is measured across that wider
neighbourhood rather than from individual incident segments, so pixel
staircases are treated as sampled diagonals or curves while sustained
mechanical corners remain sharp. Smooth vertices use reversible cubic Bezier
joins; classified corners remain unrounded.

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
labels, palette costs and protected evidence to the window plus a 64 px margin
and runs the unmodified vector stage on the crop: the shared-boundary graph,
the canonical chain fit, the overlap/solid-underlayer layer construction and
the SVG layer writer, with settings normalized as a trace run normalizes them
(an unset maximum deviation is 2 px). Layer order, when it can matter, is
inferred once from the whole image; otherwise the layers stack in palette order
as in a full trace. The result is only a preview:

- Boundaries crossing the crop edge end there, so their fit near that edge can
  differ from a full run's; the margin keeps most of that outside the visible
  window. Underlayer adjacency and the overlap buffer likewise see only the crop.
- Validation and protected-feature recovery are skipped, so explicitly protected
  details are not tightened as a full run would, and no audit is written.
- Nothing is saved; a trace run still creates the revision and its artifacts.

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
Settings and effective defaults are recorded in `trace.json`.

Naming of exact flags and JSON keys is an implementation detail, but public
names should make geometric behavior explicit and must include units where
applicable. Do not use ambiguous foreground/background terminology for the
symmetric detail controls.

## Audit report

`trace.json` records at least:

- Source artifact paths and dimensions.
- Effective tracing configuration and the cleanup settings used (size, removed
  colour, cleanup).
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
