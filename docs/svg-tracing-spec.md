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

The tracer infers a deterministic best-guess stacking order. Containment,
adjacency, and geometry may inform the inference. Ambiguity does not stop the
run: the chosen order and a warning are written to `trace.json`.

Users can override the complete or partial order. Layer ordering is independent
of the palette's descending area order.

## Palette names and optional colour removal

Palette colours receive automatically generated human-readable names such as
`Burnt Orange`, `Deep Navy`, and `Sage Green`. Hex values remain secondary
identifiers. Names must be unique within one palette and are stored in
`palette.json` so all downstream artifacts use them consistently. Palette-stage
configuration may override generated names.

Tracing can optionally omit one palette colour. CLI input accepts either its
human-readable name or hex value; a later GUI will use a palette dropdown. When
no colour is configured, all colours are included.

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

When physical dimensions are supplied, physical geometry settings use
millimetres and square millimetres. Without physical dimensions, tracing uses
the raster pixel coordinate system and equivalent pixel-based settings. The
SVG retains the raster dimensions as its `viewBox`.

## Vector cleanup

Physical/detail cleanup runs during the trace stage on a trace-local copy of
the labelled partition, before shared-boundary construction and curve fitting.
It does not alter `flattened.png`. This permits retracing the same palette
raster at different physical sizes and keeps palette reduction separate from
cutter geometry decisions.

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
  perceptually nearest stable touching colour. An undersized group cannot use
  one of its own members as the destination, preventing reciprocal colour swaps.
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
and `trace.json` records a warning. A request to widen must never silently fall
back to removal.

## Trace accuracy and complexity

Maximum contour deviation is configurable. The default fit tolerance is two
source-raster pixels; in physical mode the audit records the equivalent
physical distance for the configured output size. An explicit maximum-
deviation setting overrides this default.

The default curve-model tolerance is 0.8 source pixels. This deliberately
clears the half-pixel sampling error of a one-pixel staircase, allowing it to
collapse into a sustained line or broad Bézier while remaining comfortably
inside the two-pixel maximum-deviation audit.

The cleaned flattened raster is authoritative. A surviving explicitly
protected feature is recovered locally and in a fixed order: tighten its curve
fit, reconsider its line/curve classification, add one non-corner fit
constraint, and finally emit the dense boundary only when every modelled form
still fails validation. Every attempt and final fallback is recorded. Both
adjacent colours reuse the same recovered chain. `trace-fallbacks.png` is
emitted only for final dense fallbacks.

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
- Effective tracing and cleanup configuration.
- Tracing backend and version.
- Physical size or pixel-mode status.
- Included and omitted palette colours and their names.
- Inferred layer order, user overrides, and ordering warnings.
- Path and node counts per component, layer, and document.
- Compound-path and hole counts.
- Regions removed, widened, or left unchanged with warnings.
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
