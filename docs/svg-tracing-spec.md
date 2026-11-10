# SVG Tracing Specification

## Scope

The trace stage converts a subject's segmentation (the cleaned `int16` palette
labels of `segmentation.npz`) into a planar-map file and one deterministic,
Inkscape-oriented SVG for layered adhesive vinyl:

```text
outputs/<subject>/
├── map.json            the planar-map file: the trace's artefact and its geometry
├── traced-edge.svg     the edge SVG, written from map.json alone
├── traced.png          the preview, rendered from traced-edge.svg
└── trace.json          the audit
```

Tracing is a separate command that consumes `flattened.png`, `palette.json` and
`segmentation.npz`. It must be possible to pipeline palette processing and
tracing, but the tools remain independently runnable.

The initial compatibility target is Inkscape. Registration marks, production
tiling for large designs, heat-transfer vinyl, stencils, and physical cut tests
are later work.

## The trace stage: t3

The engine is the turbo tube tiler (t3), installed as the `t3api` wheel
(`wheels/`, `requirements-web.txt`). Its design is the tracer repo's card
`cards/vinylgen-on-t3/card.md`; the file format is that card's
`planar-map-file.md`.

- **One call per trace.** `trace_subject` (`vinylgen/tracing.py`) calls
  `t3api.trace(labels, setting=..., on_event=...)` once on the cleaned labels:
  2-D, `int16`, every colour included, the omitted one too (its regions are
  real faces and its boundaries are the other colours' cut lines). The setting
  is `TraceSettings`' half-width table, `{"half": {line, arc, ellipse,
  cubic}}`, with its minimum sweeps, `"min_sweep": {"arc", "ellipse"}`, only
  when either is above 0, in the canonical form `t3api.normal_setting` gives
  (A1 22.5, A2 45 and L 4 fixed; see [Size, units and
  cropping](#size-units-and-cropping)); a setting it refuses raises `ValueError` before anything is written, which a web
  job reports as its error. `workers=None` lets `t3api.workers_of` use every
  CPU the process may use.
- **`map.json`** is `t3api.planar_map_json(t3api.planar_map(result, palette))`
  with the subject's `palette.json` list: every boundary edge once with the
  faces on its two sides, every face as rings of signed edge references, in
  paint order, every number exact. It is read back with
  `t3api.read_planar_map`, and every later step reads that document.
- **The planar map's gate.** `t3api.planar_map_check` finds no violation, and
  the read-back map painted (`t3api.paint_planar_map`) equals t3's painted
  labels at every pixel centre. The gate runs on the read-back map before
  anything is derived from it (`tracing.planar_map_gate`). Either failing stops
  the trace with a `RuntimeError` naming the count (the violations, or the
  differing centres); the CLI reports it as an error. A `trace.json` left by an
  earlier trace of the folder is removed before the new `map.json` is written,
  so a failed trace leaves no record vouching for a map other than the one on
  disk.
- **The edge SVG** (`svg_writer.add_face_layer(root, row, pm)`, one call per
  included colour in palette order, into `svg_document(W, H, physical_mm)`):
  one Inkscape layer per colour with `inkscape:groupmode="layer"`, label
  `"{name} ({hex})"`, `id="layer-{slug}"`, the exact fill, `stroke="none"` and
  `fill-rule="evenodd"`; one path per face of that colour, its outer ring and
  holes as one even-odd path, with `d` and `sodipodi:nodetypes` exactly as
  `t3api.planar_map_paths` gives them (path data `M L A C Z`). Paths are
  numbered from 001 largest first by the face's drawn area, by Green's theorem
  over its pieces in closed form (`svg_writer.face_areas`), ties to the face
  id: `id="edge-{slug}-{nnn}"`, `inkscape:label="{name} {nnn}"`. The omitted
  colour gets no layer: its faces are already holes in the faces around them.
  The viewBox is the pixel grid, `0 0 W H`. The SVG depends on `map.json` and
  the palette alone, so exporting it again from `map.json` gives the same
  bytes.
- **Node types** are the map's: `c` (cusp) or `s` (smooth) from t3, and `z`
  (symmetric) from an edit in nib, one per Inkscape node; an `A` counts as the
  cubics Inkscape converts it to. t3 writes no `z`.
- **The semantic audit** reads t3's painted labels, the exact raster of the
  drawing (a centre no face holds reads as white paper), at the largest
  half-width an enabled kind of the map's own setting has (1.75 px at the
  defaults); see [Audit report](#audit-report).
- **The preview** `traced.png` is rendered from `traced-edge.svg` by Inkscape.
- **The engine cache.** t3's numba kernels are compiled on first use into
  `t3api.cache_dir()` (`~/.cache/t3api/<build id>`); a cold cache costs the
  first trace about 28 to 45 s. `scripts/warm_t3_cache.py` traces the test
  array (`tests/fixtures/t3-test-array/`) once and records
  `{build, seconds, warm_s, workers}` in `<cache_dir>/warm.json`; `just deploy`
  runs it after installing the requirements and before the health check, and
  `scripts/run_tests.py` runs it before the suite when the cache has no
  `warm.json`.

Progress (`trace_subject`'s `progress`, relayed by the web job as
0.1 + 0.8 f):

| tick | message |
|---|---|
| 0.01 | Reading segmentation |
| 0.02 | Tracing: planar map (t3's `start`) |
| 0.03 to 0.80 | Tracing: junction edges j of J, then Tracing: chains c of N; f = 0.03 + 0.77 (j + c) / (J + N), with N the `edges` event's `count` until a `chain` event gives its `total` |
| 0.80 | Writing the planar map and the edge SVG |
| 0.85 | Checking the planar map |
| 0.88 | Semantic audit |
| 0.90 | Writing artifacts |
| 0.95 | Rendering preview |
| 1.00 | Complete |

- **Re-export without a trace.** A web trace whose parent's effective
  `map.json` came from a trace of labels with the same sha256 (each revision's
  effective `segmentation.npz`), the installed t3api build (the map header's
  `build`) and the same setting (the header's `setting` and this trace's
  setting, its half-width table and minimum sweeps, equal under
  `t3api.normal_setting`, so a trace at one minimum never reuses a map drawn
  at another; a header written up to t3api 0.26.0 holds
  only `w`, every kind at w / 2 = 1.75), with that trace's `trace.json`
  present, makes no `t3api.trace`
  call: the map is copied byte for byte, rewritten only when the new palette
  names its colours differently, passes `planar_map_check`, and the edge SVG,
  the audit and `trace.json` are built from it with this revision's own
  settings. `t3.reused_from` and `validation.edge.read_back.reused_from` name
  the revision whose trace drew it, and `t3` repeats that trace's timings. A
  map edited in nib (below) is never reused this way: it is not t3's trace of
  the labels, so a trace from an edited revision traces afresh.
- **Editing the trace in nib.** The Nodes stage is nib's editor with nib's
  chrome (`@nib/core` 0.11.1, `web/vendor/nib-core-0.11.1.tgz`) on the selected revision's `map.json`.
  Saving posts the exact text nib wrote to
  `POST /api/revisions/{revision_id}/map`, where `revision_id` is the trace
  revision that owns the map. Revisions are immutable, so a save makes a child
  revision of kind `trace` with the message "edited in nib" and the parent's
  palette and trace settings (`PipelineRunner.save_map`). Its `map.json` is the
  posted document byte for byte. The edge SVG, the checks, the audit,
  `traced.png` and `trace.json` are rebuilt from it by the re-export path
  (`trace_subject` with `reuse` naming the editor), with three differences: the
  map is never rewritten, its header's build need not be the installed one,
  and it is audited at its header's own setting. A colour nib's boundary tool
  appended to the palette (a "new colour" pick) is drawn as its own layer
  after the included colours, named by the map's name for it or else by its
  generated colour name, numbered past a name another colour holds
  (`tracing.trace_palette`); validation and the export read the colours the
  same way, and the revision's `palette.json` stays the raster's. Before
  anything is made, the server refuses with HTTP 422 a document that fails
  one of these checks, in this order:
  1. `t3api.read_planar_map` reads it (`reason` `"invalid"`).
  2. `planar_map_check` finds no violation (`"invalid"`, with `violations`).
  3. The rule of the map: no edge has one label on both sides; an edge with
     a null side (a border edge) is exempt, as in nib's `checkLabelMap`
     (`"invalid"`, naming the edge).
  4. Its `W` and `H` are the parent's, and its `palette` is the parent's with
     entries only appended: none changed, reordered or removed, each appended
     entry a colour `[r, g, b]` of integers from 0 to 255, equal to no
     earlier entry (a palette never holds one colour twice; nib's picker
     gives a colour equal to an entry that entry's label), that labels at
     least one face. Its `names` keep the parent's the same way, an appended
     name null or one no other entry holds, ignoring case (`"mismatch"`,
     naming the first differing entry).
  5. Every face's label is an entry of its palette (`"invalid"`).
  6. It is not byte-equal to the parent's map (`"unchanged"`: nothing was
     edited).

  The 422 body is `{detail, reason, problems[, violations]}`. A revision with no
  `map.json` of its own answers 404. The client shows "unchanged" as a notice
  and every other refusal as an error listing the problems. After a save it
  shows the new revision on its Traced view, as it does a finished trace's.
- **The live trace view.** While a web trace runs, each of t3's `face` events
  (the face's id, colour label, rings as SVG path data and node types) is
  written by the worker as a JSON line and sent on the job's event stream as
  `event: face`, in t3's emission order. The Trace canvas keeps the faces it
  has received and draws them as one SVG over the raster, layers in palette
  order (the omitted colour skipped) and each layer's faces in face order: a
  layer's faces share one fill and never overlap, so this paints exactly what
  the finished edge SVG paints. The view clears when the job ends; a client
  that subscribes late gets no earlier faces.

## Geometry model

The trace's geometry is t3's planar map, `map.json` (the tracer repo's
`cards/vinylgen-on-t3/planar-map-file.md`): a partition of the canvas into
faces. Each face is one 8-connected component of one label, held as rings (its
outer boundary first, then its holes) of signed references to edges. Each edge
is stored once, with the faces on its two sides, as a chain of pieces: lines
(`L`), circular and elliptical arcs (`A`) and cubic Béziers (`C`), plus the
connectors a ring needs where it has no drawn boundary: `border` edges along
the canvas edge and `gap` edges between a terminus end and its cut (each gap
is a warning in `trace.json`). Two adjacent faces meet along one stored edge,
so neighbouring colours share their boundary bit for bit and meet edge to edge;
no stacking order changes which colour a point gets. Where two regions of one
colour touch only at a pixel corner, t3's pinch assignment draws them as one
face or as two; the cutter cleanup, which runs on the labels before the trace,
still treats them as separate four-connected regions.

`traced-edge.svg` is the one SVG a trace writes, exported from `map.json`
([The trace stage: t3](#the-trace-stage-t3)). Each included palette colour
becomes a named Inkscape layer and each of its faces one separately selectable
path, numbered largest-first with stable names. Holes are real holes, the
face's hole rings in its own even-odd compound path, never white cover shapes,
masks or clipping paths. The omitted colour's faces are holes in the faces
around them.

The canvas tightly matches the cropped artwork, with no automatic outer margin.
SVG fills use the exact palette RGB values. Cut paths have no cosmetic stroke;
shared topology, rather than seam-covering paint, guarantees the join.
Gradients, opacity, filters, masks, and embedded raster images are not used for
cut geometry.

### Path node types

Every traced path carries `sodipodi:nodetypes` (namespace
`http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd`, declared on the root), so
Inkscape's node tool shows the node types Vinylgen reports. The string is t3's
(`t3api.planar_map_paths`), one character per node of Inkscape 1.2.2's node
tool; an `A` counts as the cubics Inkscape converts it to, with `s` at its inner
nodes. Terms follow [GLOSSARY.md](../GLOSSARY.md) "Path nodes".

| Character | Vinylgen term | Inkscape name | Where t3 writes it |
|---|---|---|---|
| `c` | cusp node | cusp | A corner, and every end of an open edge: a junction cut, a chain end, the canvas border or a gap. |
| `s` | smooth node | smooth | A tangent join between two pieces, and the inner nodes of an arc. |
| `z` | symmetric node | symmetric | Never: t3's smooth joins are tangent joins, not equal-handle ones. A `z` comes only from an edit in nib's editor, saved in the map's `node` letter. |

Rendering ignores the attribute.

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

The trace's settings (`TraceSettings`, `vinylgen/contracts.py`) are the three
soft node limits, each curve kind's tube half-width and each arc kind's minimum
sweep. The soft node limits,
`soft_path_nodes` (5,000), `soft_layer_nodes` (50,000) and
`soft_document_nodes` (200,000), are node counts that only add a warning to
`trace.json` naming the path, the layer or the document over its limit. The
half-widths, `half_line`, `half_arc`, `half_ellipse` and `half_cubic` (1.75 px
each), steer t3: each is the furthest a drawn piece of that kind may lie from
its edge. Higher lets the kind cover longer stretches, looser; lower holds it
tighter to the pixels. Each is > 0. Arcs, ellipses and cubics can be off
(`null`), so that kind is never drawn; the line cannot, since an edge of one
crack can only be a line. Below about 0.55 px (sampled) long shallow straight
edges break into several lines. A stored record from before the half-widths
reads as the defaults. The minimum sweeps, `min_sweep_arc` and
`min_sweep_ellipse` (degrees, 0 each), are the least sweep a drawn circular
and elliptical arc may have: an arc of that kind sweeping less is never drawn,
and the other kinds draw that stretch. Each is in [0, 360]: 0 is no minimum,
360 admits whole circles or ellipses only. A kind that is off takes only 0 (the
web job answers 422 to a minimum above 0 on it, the CLI exits 2), and t3 is
given the `min_sweep` key only when either is above 0, so the canonical
setting, the map header and the audit at the default carry no `min_sweep`. A stored record from before the
minimum sweeps reads them as 0. On the trace step they are the "Curve kinds" group, laid
out in aligned rows under a header row (Kind, Tube half-width, Min sweep), one row per kind (Lines,
Circular arcs, Elliptical arcs, Cubic curves): the kind's switch, its name and
its half-width field in px, and on the two arc rows a minimum-sweep field in
degrees (0 to 360; a value outside is not sent). On a narrow panel (a phone) the
minimum-sweep field moves to a second line of its row, under the half-width,
with its own "Minimum sweep" label. The line's switch is shown fixed on, its title
saying why; a row switched off is dimmed, its fields disabled and its half-width
reading "off"; switching an arc kind off sends its minimum sweep as 0 with it.
The Nodes stage names the half-widths a revision was traced at, followed by
`min sweep arc A, ellipse E` only when either minimum is above 0.
A table other than
the defaults may trace several times slower (fox-owl-table-2's edited labels:
150 s at the defaults, 778 s at line 1.75, arc 1.0, ellipse 1.75, cubic 4.0);
no vinylgen job has a time limit. The physical size is set by the cleanup stage and applies to both the
cleanup thresholds and the traced SVG; in the web workflow the trace shows it
read-only and changing it means running cleanup again. Setting a physical size
never changes the trace; the SVG `viewBox` is the traced pixel grid (the
flattened or edited raster, with any palette-stage `flatten_upscale` already
applied).

The cleanup's physical settings are `min_detail_width`, `remove_below_width`
(lengths) and `min_region_area` (an area). When a width or height is supplied
they are millimetres and square millimetres, converted to pixels through the
physical scale; without physical dimensions they are pixels and square pixels.

**Retired trace settings.** Settings no trace reads are retired
(`RETIRED_TRACE_FIELDS`): eight lengths (`curve_fit_tolerance`,
`straight_line_max_deviation`, `minimum_straight_length`, `max_deviation`,
`corner_analysis_span`, `side_smoothness`, `corner_sharpening_allowance`,
`curve_feature_scale`), their unit `length_unit`, the corner, node and snap
controls (`straight_line_snap`, `snap_angle`, `node_placement`,
`nodes_at_bends`, `corner_threshold`, `corner_straightness`), and `overlap`,
`solid_underlayers` and `layer_order`, which a trace whose colours meet edge to
edge has no use for. A stored record, a web draft, a trace request and a
`cli_trace --config` file are read with them dropped (`upgrade_trace_record`;
the web client's `normalizeTraceSettings` keeps only the three limits), so an
old revision's settings fill the form with its soft limits and nothing else.
A trace revision recorded before the cleanup stage keeps its size, removed
colour and cleanup settings in `settings.trace`, read as the cleanup settings
it used.

## Cutter cleanup (raster labels)

Physical/detail cleanup is its own stage between editing and tracing. In the
web workflow it writes a `cleanup` revision with cleaned labels and a derived `flattened.png`, so its
cost is paid once and every trace of that revision's boundaries depends only
on the cleaned raster. A cleanup always starts from the raster before any
earlier cleanup, so cleanups never stack. After a flatten or edit that follows
a cleanup, tracing waits until Cleanup is run again. The trace CLI applies the
same cleanup function to an in-memory copy of the labels before tracing and
leaves the subject's files unchanged. Web and CLI both validate and draw the warning images against the cleaned labels and
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
  part inside the crop. The 256 px margin is what
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
`widen` still took up to 0.98 s on `sky-whale-city`. The preview's window is at most
600 × 600 px (`CLEANUP_PREVIEW_MAX_AREA_PX`).

The preview thread holds one raster at a time: the uncleaned raster with its
measurements. While the revision
shows a cleaned raster its labels are held beside the uncleaned one, 2 more bytes
per pixel (about 50 MB on `sourceup`, 180 MB on 11264 × 7920), loaded before the
windowed clean and dropped as soon as a previewed revision shows the uncleaned
raster itself. Previewing
another revision therefore reloads, and each return to a revision measures the
whole raster again (about 6.4 s on `sourceup`, 22 s on 11264 × 7920) before its
first preview appears.

So that the wait is explained, the server reports the measuring at
`GET /api/revisions/{id}/cleanup-preview/regions` as `{"measured", "measuring"}`:
whether the preview thread holds the measurements of the raster a cleanup
preview of that revision cleans, and whether it is building them now. The read
is answered on the event loop from the thread's state, never queued behind the
preview it reports on, so it adds nothing to the preview's time. A preview of
another revision evicts the raster and turns `measured` false again; a measurement stopped
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

t3 traces the cleaned labels at the trace's half-width table (each kind 1.75
px by default; A1 22.5, A2 45, L 4) and its minimum sweeps (0 by default), the
only vinylgen settings that change the geometry. Its
corners, joins, node placement and piece kinds are t3's, decided from the
labels alone, and its design and proofs live in the tracer repository. What a
trace holds vinylgen to:

- **The drawing is the planar map.** `map.json` read back passes
  `t3api.planar_map_check` with no violation and paints, by
  `t3api.paint_planar_map`, exactly t3's painted labels at every pixel centre;
  a trace that fails either stops ([The trace stage: t3](#the-trace-stage-t3)).
- **The drawing stays in the tube.** t3's summary in `trace.json` records its
  own gates (`over_tube`, `checks_count`) and how far its painted labels differ
  from the input: `diff_px` counts the centres whose painted label is not the
  input label. The semantic audit reads the painted labels at the largest
  enabled half-width of the map's setting, 1.75 px at the defaults, and flags lost or moved features
  ([Audit report](#audit-report)).
- **Complexity is reported, never traded for accuracy.** `geometry.edge` counts
  each layer's paths, Inkscape nodes, pieces by kind and node types. The three
  soft node limits (`soft_path_nodes` 5,000, `soft_layer_nodes` 50,000,
  `soft_document_nodes` 200,000) only add a warning naming the path, the layer
  or the document over its limit; nothing lowers the node count by moving the
  geometry.

## Performance

The trace is a deterministic function of the cleaned labels and the installed
t3 build. Measured on `fox-owl-table-2`'s cleaned labels (6398 × 2133, four
colours), t3 traced in 119.5 s on two workers (one performance core's two
hyperthreads), peak memory about 1.5 GB, drawing 8,348 pieces in 790 faces and
20,296 Inkscape nodes; a trace takes every CPU the service may use. A cold
kernel cache adds about 28 to 45 s to the first trace of a build, which
`just deploy` pays once ([The trace stage: t3](#the-trace-stage-t3)).

The stages around the trace are accelerated without changing a result;
`tests/oracles/` holds frozen copies of their reference implementations and the
fast tests compare against them:

- Validation uses a lookup-table sRGB decode, a Numba two-sided ridge kernel,
  one candidate distance field per rendered pass shared by every feature
  metric, and a cache of the protected-feature invariants (labelling,
  nearest-component map, corridors, widths) reused by every pass.
- Cutter cleanup evaluates feature widths from one padded distance field per
  colour and checks neighbour residuals with an exact windowed update instead
  of relabelling and re-transforming the full image per widened region.

The `traced.png` preview targets 768 DPI, but `vinylgen/preview.py` caps its
longer side at 1920 px: it is an inspection image, so large sources render at
roughly 1080p instead of gigapixel size. Inkscape 1.2.2 does not render very
large previews deterministically, so byte identity of `traced.png` is only
asserted at capped sizes.

## Configuration

The tracing command supports both individual CLI flags and a reusable JSON
configuration file. CLI arguments override values from the configuration file.
Settings and effective defaults are recorded in `trace.json`. The flags are
the cleanup's (size, removed colour, cutter cleanup), `--colour-name`, and the
half-widths `--half-line`, `--half-arc`, `--half-ellipse` and `--half-cubic`
(px, > 0; `off` on all but the line), and the minimum sweeps `--min-sweep-arc`
and `--min-sweep-ellipse` (degrees, 0 to 360, 0 by default; above 0 refused on
a kind given as off); the three soft node limits are set in a
configuration file only, which may also set the half-widths (`null` is off) and
the minimum sweeps (`min_sweep_arc`, `min_sweep_ellipse`). A configuration's
retired trace settings are dropped on load
([Retired trace settings](#size-units-and-cropping)).

Naming of exact flags and JSON keys is an implementation detail, but public
names should make geometric behavior explicit and must include units where
applicable. Do not use ambiguous foreground/background terminology for the
symmetric detail controls.

## Audit report

`trace.json` records:

- `subject`, `sources` (source, flattened, palette and segmentation paths, and
  `map`, the planar-map file `map.json`),
  `raster_dimensions`, `mode` and `physical_dimensions_mm`.
- `settings`, the trace settings as given (the three soft node limits, the
  four half-widths and the two minimum sweeps), and
  `cleanup_settings` (size, removed colour, cleanup).
- `backend`: `tracer` `"t3api"`, `build` (`t3api.BUILD_ID`), `version`
  (`t3api.VERSION`), `setting` (the map's setting in the canonical form,
  `t3api.normal_setting`: `{"half": {line, arc, ellipse, cubic}, "A1", "A2",
  "L"}`, with `"min_sweep": {"arc", "ellipse"}` when either is above 0)
  and the `numpy`, `scipy` and `numba` versions. A trace revision saved from
  nib adds `editor` (`{"name": "nib", "version": "0.11.1"}`).
- `t3`: `summary` (t3's result summary, nested: pieces by kind, smooth and
  corner joins, termini, `over_tube`, `checks_count`, `diff_px`,
  `unpainted_px`, faces, `faces_empty`, the gates and t3's own phase seconds),
  `seconds` (the trace call's wall time), `warm_s` (the engine warm-up within
  it), `workers`, and the planar map's gap connectors as
  `t3api.faces_svg_from_planar_map` reports them for `map.json`: `gaps` (their
  count) and `gap_max` (the longest, px). A re-export adds `reused_from`. A
  save from nib repeats its parent's `summary`, `seconds`, `warm_s` and
  `workers`, and adds `edited` (`true`) and `edited_from` (the parent revision).
  Its `validation.edge.read_back` compares the posted map's paint with the
  stored map's (`differ` 0) and names `edited_from`.
- `colours`: the included colours in palette order and the omitted one.
- `cleanup`: regions removed, widened, or left unchanged with warnings, when
  the CLI cleaned inline (otherwise in the cleanup stage's `cleanup.json`).
- `geometry.edge`, per layer: `index`, `name`, `hex`, `paths`, `nodes` (the
  Inkscape node count), `holes`, `construction` (`"t3 planar map"`),
  `source_colour_components` (4-connected raster components), `elements` (the
  layer's pieces by kind: `L`, `A circular`, `A elliptical`, `C`, and the
  `border` and `gap` connectors) and `node_types` (`c` and `s` counts), with
  `components[]` per path (`id`, `face`, `nodes`, `holes`).
- `validation.edge`: `validate_svg` on `traced-edge.svg`, plus
  `planar_map_check` (the check's counts: `nodes`, `edges`, `border_edges`,
  `drawn_null_sides`, `faces`, `rings`, `references`, `pieces`, `violations`
  0, and its empty `problems`) and `read_back` (`differ` 0, the centres where
  the read-back paint differs from t3's painted labels); and
  `validation.semantic` (the semantic preservation audit of t3's painted
  labels at `maximum_deviation`, the largest enabled half-width of the map's
  setting (1.75 px at the defaults), and `minimum_feature_span`
  `feature_span(maximum_deviation)`, 6 px at the defaults).
- `warnings`, none of which blocks the export:
  - the semantic audit's failure, naming `trace-semantic-warnings.png` and
    `trace-missed-details-overlay.png`;
  - each explicit protected component the drawing changes (a 4-connected
    one-colour region of `explicit_protected_features` holding a centre whose
    painted label differs from the traced label), with its colour name, pixel
    bounds `[left, top, right, bottom]` (right and bottom exclusive), its share
    `"<changed> of <size>"` centres and the missed-detail overlay. t3 has one
    setting, so a re-run draws the same: the fix is the user's (edit the
    labels, widen the feature, trace again);
  - the soft node limits: each path over `soft_path_nodes` (named by its id),
    each layer over `soft_layer_nodes` (named) and the document over
    `soft_document_nodes`;
  - any gap connector in the planar map (`t3.gaps` > 0), with the longest.
- `determinism`: `input_sha256` (flattened, palette and segmentation) and
  `map_sha256` (the bytes of `map.json`).
- `preview`: the `traced.png` plan.

## Validation

The initial milestone stops after automated validation and visual inspection in
Inkscape. It does not claim physical cutter readiness.

Automated checks must cover:

- Valid SVG/XML readable by Inkscape.
- Deterministic output for identical inputs and settings.
- Expected named palette layers and exact fill colours.
- Separately selectable disconnected components in stable largest-first order.
- Preserved holes using compound paths.
- No open cut paths, duplicate paths, or unsupported SVG effects.
- The planar map's gate: `t3api.planar_map_check` with no violation, and
  `map.json` read back and painted equal to t3's painted labels at every
  centre (a trace stops with an error otherwise). Shared boundaries are stored
  once in `map.json`, so adjacent faces share them bit for bit.
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
