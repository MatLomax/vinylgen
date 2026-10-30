# Files, Naming, and Output Contract

## Project layout

```text
vinylgen/
├── AGENTS.md
├── README.md
├── palette_infer.py
├── images/                 # Preserved source raster corpus
├── outputs/                # Durable, reviewable generated artifacts
│   ├── contact.png
│   └── <subject>/
│       ├── source.png
│       ├── flattened.png
│       ├── segmentation.npz
│       ├── palette.json
│       ├── report.png
│       ├── map.json            # the planar-map file, the trace's geometry
│       ├── traced-edge.svg     # written from map.json
│       ├── traced.png
│       ├── trace-semantic-warnings.png  # only when the semantic audit fails
│       ├── trace-missed-details-overlay.png  # transparent twin of trace-semantic-warnings.png
│       ├── trace.html
│       └── trace.json
├── tests/
├── docs/
└── .tmp/                   # Disposable project-local scratch only
```

Never place project scratch in `/tmp`. The user explicitly requires
`vinylgen/.tmp/` so intermediate work is visible and contained.

## Naming

- Subject slugs are lowercase and hyphenated.
- The input filename stem is the output-directory name.
- Prefer concise semantic names such as `crashed-ring`, not timestamps or model
  export names.
- Current input filenames are authoritative; do not rename casually because
  output paths and JSON `source` values follow them.

## Artifact meanings

### `source.png`

Byte-for-byte copy of `images/<subject>.png`, kept beside the generated files
for direct comparison. It is never re-encoded and does not replace the
authoritative original in `images/`.

### `flattened.png`

Opaque RGB raster at the source dimensions. Every pixel belongs to exactly one
inferred palette entry. After default cleanup, every four-connected region is
at least 24 pixels unless it is recorded as a confident intentional thin
feature.

### `segmentation.npz`

Versioned, machine-readable handoff between raster processing and tracing.
Contract v3 contains the final integer label map, complete per-pixel/per-palette
Lab cost field, automatic protected-feature mask, explicit user-protection
mask, palette RGB identity, and contract version.
The tracer rejects missing, dimensionally invalid, or palette-mismatched
contracts instead of reconstructing intent from `flattened.png`.

### `palette.json`

Audit record containing:

- Source path and dimensions.
- Inferred colour count.
- Palette ordered by descending pixel share.
- Exact hex/RGB values and rounded area percentage.
- Region counts and largest-region share.
- Palette-family merge history and reasons.
- Small-region cleanup threshold, passes, removed regions/pixels, and source to
  target index totals.
- Complete settings used.

Treat this as a generated schema that downstream tooling will consume. Update
documentation and tests when changing it.

### `report.png`

The flattened image remains full-size at the top. A black band is appended
beneath it, containing palette swatches, exact `#RRGGBB` values, and image-area
percentages to four decimal places, ordered descending.

### `protected-areas-overlay.png` (web flatten)

The web flatten job writes this transparent RGBA overlay (artifact kind
`protected-areas-overlay`) only when `segmentation.npz`'s explicit-protection
mask has a true pixel, meaning a `protected_component_points` pick protected
something. Each explicitly protected pixel is semi-transparent green
`(40, 220, 120, 150)`, distinct from the red missed-details and cyan
tightened-boundary trace overlays, and every other pixel is fully transparent.
It is stored as a 1-bit paletted PNG, with index 0 fully transparent and index
1 the overlay colour at that alpha, so it stays small for gigapixel masks.
It is the size of the segmentation grid, which is the flattened grid and so
larger than the source when `flatten_upscale` > 1. The Flatten canvas offers it
as the "Protected areas" toggle on both the Source and Flattened views. The CLI
does not write it. A later flatten never inherits an earlier run's overlay (see
[architecture](architecture.md)).

### `cleanup-changes-overlay.png` (web cleanup)

The web cleanup job writes this transparent overlay (artifact kind
`cleanup-changes-overlay`) of the pixels the run changed, only when it changed a
pixel. It compares the cleaned labels with the raster the run started from
(before any earlier cleanup): a pixel whose final label differs from its
uncleaned one is green `(40, 200, 70)` when the widening set its final label
(it differs from the labels after the removal stage), otherwise red
`(230, 40, 40)`; a pixel that ends with its uncleaned label (removed, then
widened back) is transparent, as is every unchanged pixel. It is an 8-bit
paletted PNG (index 0 transparent, 1 red, 2 green, both opaque), one byte per
pixel in memory, on the segmentation grid. The Cleanup canvas draws it over the
whole image while its "Show changes" switch is on, with or without the live
preview; a cleanup recorded before runs wrote it has none, and the switch then
reads "Re-run Cleanup to record its changes". The CLI does not write it.

### `contact.png`

Convenience review sheet across all flattened outputs. It is generated outside
the Python tool with ImageMagick at present and is not part of the per-subject
contract.

### Trace artifacts

`map.json` is the planar-map file (the tracer repo's
`cards/vinylgen-on-t3/planar-map-file.md`): t3's drawing as a label map, every
boundary edge stored once with the faces on its two sides and every face as
rings of signed edge references, in paint order, every number exact. It is the
trace's artefact (web artifact kind `traced-map`) and the one source of its
geometry: every later step reads it back with `t3api.read_planar_map`.
A web trace re-exports instead of tracing when its parent's effective
`map.json` was written by a trace of labels with the same sha256 (each
revision's effective `segmentation.npz`), by the installed t3api build (the
map header's `build`), and that trace's `trace.json` is present: the map is
copied, its colour names are rewritten to the new palette's, and the SVG, the
checks and the audit are built from it again. Its `trace.json` then names the
revision it reused under `t3.reused_from` and `validation.edge.read_back.reused_from`,
and repeats that trace's `t3` timings.
`traced-edge.svg` is a one-way export from it for the cutter and the web
viewer: one Inkscape layer per included colour in palette order, one even-odd
path per face (its holes as further rings of the same path) with path data
`M L A C Z` and t3's `sodipodi:nodetypes` (`c` and `s`), numbered largest first
by drawn area. The omitted colour is traced like any other and gets no layer.
The rules are in [SVG tracing specification](svg-tracing-spec.md) "The trace
stage: t3". Older trace revisions also hold `traced-overlap.svg`, the old
fitter's overlap variant, and may hold `trace-fallbacks.png` and its
transparent twin `trace-fallbacks-overlay.png`; a trace no longer writes them,
and a CLI retrace of a subject folder removes them. SVGs traced before node
types existed have no `sodipodi:nodetypes`, and the Trace canvas's Nodes
overlay says so instead of guessing.

`traced.png` is the current preview rendered from `traced-edge.svg` (in
revisions traced before t3, from `traced-overlap.svg`) and must be regenerated
whenever the SVG changes. It targets 768 DPI, but its longer side is capped at
1920 px; `trace.json` records the effective preview size under `preview`.
`trace-semantic-warnings.png` is written only when the semantic feature audit
fails; it paints the missed detail red over the source faded toward white, as
a standalone audit image. Its transparent RGBA twin
`trace-missed-details-overlay.png` is written when the audit fails or the
drawing changes an explicit protected component, and removed otherwise. Both
mark the same missed detail: every source pixel the audit misses and every
changed centre of a changed explicit protected component, in opaque red in
the overlay, every other pixel fully transparent. The overlay is the size of
the traced pixel grid (`flattened.png`, the SVG viewBox), so it registers
exactly over the traced SVG. The web app offers only the twin, as the Trace
canvas toggle "Missed source details (red)".

`trace.json` is the audit: the sources (`map.json` among them), the size and
mode, the trace settings as given (`settings`: the three soft node limits, which
only raise warnings; t3 has one setting, so none steers the trace) and the
cleanup settings with the physical size resolved (`cleanup_settings`), the
tracer (`backend`: `tracer` `"t3api"`, `build`, `version`, `setting` and the
`numpy`, `scipy` and `numba` versions), t3's record (`t3`: its result
`summary`, the trace call's `seconds`, the engine warm-up `warm_s`, `workers`,
and the planar map's gap connectors `gaps` and `gap_max`), the included and
omitted colours, per-layer and per-path counts (`geometry.edge`), structural
validation (`validation.edge`: `validate_svg`, the planar map's
`planar_map_check` counts and the read-back paint's `differ`) and semantic
validation, warnings (the audit's failure, each changed explicit protected
component, the soft node limits and any gap connector), and
`determinism` (`input_sha256` and `map_sha256`, the sha256 of `map.json`).
The full list is in [SVG tracing specification](svg-tracing-spec.md) "Audit
report". A `trace.json` written before t3 also carries `lengths`, `topology`,
`layer_order`, `raster_partition_validation` and the overlap variant's
geometry and validation, and its `settings` hold the old fitter's settings
(lengths in thousandths of the traced raster's longer side, or in pixels
before units); they are retired and read with them dropped (see "Retired
trace settings" in the specification). `cleanup` holds the
cleanup summary when the trace CLI cleaned inline, and is `null` when the
labels came from the cleanup stage, whose `cleanup.json` holds that summary.

### Cleanup artifacts

The cleanup stage writes `cleanup.json`: the cleanup rules' `algorithm_version`,
the requested and resolved settings, the raster dimensions, pixel or physical
mode with the physical size in mm, the omitted colour (`omitted_colour`, its palette row as the run
named it, and `omitted_index`, its palette index), the number of changed pixels, and the cleanup summary:
removed, widened and unchanged regions with warnings,
`protected_kept_regions` (explicitly protected regions kept although under a
threshold), and `auto_protected_components_cleared` /
`auto_protected_pixels_cleared` (eight-connected automatic protected details,
and their pixels, whose protection lapsed because the cleanup changed their
labels). A widening whose every needed pixel is explicitly protected changes
nothing; it is counted as unchanged with a warning, not as widened. The
web job's result message states the three protection counts. Every run
rewrites the pixel shares in `palette.json` for the raster it results in (its
input's shares when nothing changed). When the run changed pixels it also
rewrites `segmentation.npz` (cleaned labels, automatic protection cleared where
labels changed; palette costs, explicit protection and palette unchanged,
still contract v3), `flattened.png` (derived from the cleaned labels) and
`report.png` (re-rendered from the cleaned raster and shares). A cleanup always starts from
the raster before any earlier cleanup. The trace CLI's `trace.json` `cleanup`
summary carries the same fields.

### Web revision artifacts

The web API stores these same files as content-addressed immutable artifacts.
Palette, cleanup, trace, validation, and export jobs each create a child
revision and return its ID as `result_revision_id`. A revision's effective
artifact set is the newest artifact of each kind found while walking its parent
chain. Stale concerns accumulate along the walk: once any revision on the path
marks a concern stale, artifacts of that concern's kinds from older revisions
are excluded. A cleanup replaces any earlier cleanup, so an older cleaned
raster (a `cleanup` revision's, or a label edit's made on it, marked
`settings.cleaned_raster`: its `flattened`, `segmentation`, `palette`, `report`)
and the `cleanup-changes-overlay` are skipped once a newer cleanup has been
passed, even when the newer run changed nothing and so wrote neither. Where such
a label edit's cleaned raster is skipped, its `before-cleanup-flattened`,
`before-cleanup-segmentation`, `before-cleanup-palette` and
`before-cleanup-report` artifacts (the raster before cleanup as a rename or
recolour on the cleaned raster left it) resolve as `flattened`, `segmentation`,
`palette` and `report`; elsewhere they are never returned, so
an export never packages them. The `cleaned-flattened` and `cleaned-labels`
artifacts (the Cleaned image behind touch-ups, below) are skipped with an older
cleaned raster and by the `before_cleanup` lookup, belong to the cleanup concern,
and an export never packages them. A revision's `touch-ups` artifact is its own and
never inherited. The overlay belongs to the cleanup concern with the
`cleanup-audit`: a re-flatten, merge or source upscale after the cleanup hides
it with the rest of that cleanup, a trace or touch-up of the cleanup keeps it,
and the `before_cleanup` lookup never returns it. A `trace` run likewise
writes its complete output set, so it hides every trace-kind artifact of an
older run, including optional audits (fallbacks, semantic warnings and their
overlays) it did not repeat, together with everything made of that older run:
its `inkscape-edit` artifacts, its `validation` and its `export`. A trace
revision marks validation and export stale, and trace revisions recorded
before it did are treated the same way, so a run started from an older run's
validation or export never inherits them. A `palette` (flatten) run likewise
hides every flatten-kind artifact of an older run (`flattened`,
`segmentation`, `palette`, `report` and `protected-areas-overlay`), so a
re-flatten that protected nothing never inherits an earlier run's
protected-areas overlay; revisions derived from a flatten (edits, cleanup,
trace) inherit it with the rest. An export packages every effective artifact
but an earlier export, so a run made from a cleanup includes its
`cleanup-changes-overlay.png`. It packages the run's own validation: an export
job is refused ("revision has stale prerequisite concerns: validation") until
the current trace run has been validated, whether the parent marks validation
stale itself or simply has no effective
`validation` artifact. `GET
/api/revisions/{id}/effective-artifacts?before_cleanup=true` skips every
cleaned raster, resolving the raster the cleanup stage starts from.
Revision settings retain the effective settings of their stage and list stale
downstream concerns; this makes retracing and export provenance explicit
without mutating an earlier result.

A `cleanup` revision records `settings.cleanup` (width and height in mm, removed
colour, and the cleanup switch, action and thresholds) and
`settings.cleanup_algorithm_version` (the cleanup rules it ran under, absent for
runs made under version 1), `settings.removed_colour_index` and
`settings.removed_colour_hex` (the palette index and hex the removed colour
resolved to when the run cleaned, both `null` when none was removed), `settings.changed_pixels` (the pixels the cleanup itself changed, which its changes overlay shows; absent on runs recorded before it), and marks trace, validation, and export stale. When it changed pixels its artifacts are the
cleaned `flattened`, `segmentation`, `palette` and `report` plus the
`cleanup-audit` (`cleanup.json`) and the `cleanup-changes-overlay` (what the
cleanup changed, never a replayed touch-up); a run that changed nothing (cleanup off, or
nothing under the thresholds, and no touch-up replayed) holds only `palette` (its raster's shares) and
the `cleanup-audit`, and its effective raster is the uncleaned one. After cleaning, the run
replays the touch-ups made since the previous cleanup
(`vinylgen/web/touch_ups.py`) and, when there were any, records
`settings.touch_up_replay` (`{total, replayed, skipped, partly_superseded,
message}`, each skipped one `{tool, point, reason, kept}` with `reason`
`"resized"` (re-flattened at a different size), `"superseded"` (a later edit
covers it), `"erase-unremoved"` (an erase, and the run removes no colour),
`"colour-gone"` (the colour it paints is not in the palette),
`"area-colour-gone"` (the colour of the area it recoloured is not) or
`"area-changed"` (that area has changed since it was edited: Cleanup, an earlier
touch-up it skipped, a later edit beside it or a re-flatten changed it), and each
pencil stroke replayed only where no later edit painted as `{tool, point}`). A
touch-up skipped as `"erase-unremoved"` or `"area-changed"` is kept (`kept` true;
the message calls it still waiting): later runs judge it again and replay it once
it applies. Any other skipped one is dropped for good. The run records its
replayed and kept touch-ups, in their original order, as its own
`settings.touch_ups` (a kept one marked `waiting`; an erase still marked `erase`,
painted in the colour each run removes) and the pixels they paint in a
`touch-ups` artifact (`touch-ups.npz`), and `cleanup.json` carries the same
under `touch_ups`, with `raster_changed` saying whether the run rewrote the
raster. When the replayed touch-ups changed the raster, the run also keeps its own
result before them, the Cleaned image the web app shows behind the touch-ups:
`cleaned-labels` (`cleaned-labels.npz`, member `labels`, int16) and
`cleaned-flattened` (`cleaned-flattened.png`, those labels in the palette's
colours). While a cleanup is current, label edits read and build on its cleaned raster;
otherwise the raster from before any cleanup (the `before_cleanup` lookup). A
label edit on the cleaned raster records `settings.cleaned_raster`; each area
recolour or pencil stroke in it is a touch-up, listed in `settings.touch_ups`
(`{operation, tool, target_label, replays: true, erase}`, `erase` true when it
paints the colour the current cleanup removes, plus `point` and `source_label`, the
region's colour before it, for an area recolour) with its pixels in the
revision's `touch-ups.npz`. An area or pencil edit made while a cleanup ancestor
is stale is listed the same way with `replays: false`: its pixels are a footprint
no older touch-up paints over. Each entry's pixels are member `<i>_pixels` (the stroke's `(x, y)` pixels,
int32) or `<i>_origin`, `<i>_size` (the region's bounding box, x/y and
width/height) and `<i>_mask` (the box's pixels, row-major, `numpy.packbits`),
`<i>` the entry's index. A rename or recolour on the cleaned raster also writes
the `before-cleanup-*` artifacts above: the palette alone for a rename, the
palette, segmentation, flattened raster and report for a recolour. A recolour
on the cleaned raster while touch-ups are shown (a replayed one, or a label edit
with touch-ups since the current cleanup) also writes `cleaned-flattened`: the
Cleaned image in the new colours, from the nearest `cleaned-labels` since that
cleanup, else the labels of the revision before the oldest touch-up since it. Every label
edit that changes its raster's pixels or colours writes that raster's
`report.png`. Palette revisions, merges, area or pencil edits made
without a current cleanup, and upscaled sources mark cleanup stale (a rename,
a recolour and a touch-up never do), so an earlier cleanup's audit and settings no longer apply
beyond them, and a trace job is refused ("the artwork changed after Cleanup:
re-run Cleanup before tracing") until Cleanup runs again. The web Trace panel
offers a one-click re-run of that cleanup with its own recorded settings: a
cleanup job with `rerun_last_cleanup: true` and no `cleanup` settings, whose
settings the server reads from the nearest cleanup ancestor
(`pipeline.rerun_cleanup_settings`). Neither that cleanup's recorded name nor
its index identifies the removed colour in the palette the re-run starts from
(a trace's colour names may have renamed it, a merge may have shifted it), so
the re-run names it as the Cleanup form inherits it
(`pipeline.inherited_remove_colour`, below), checked against the re-run's
starting palette exactly as the cleanup job resolves it (by name or hex): when
no colour has the cleanup's hex (a re-flatten, recolour or merge) the request
fails with 409 ("the colour the last Cleanup removed (#RRGGBB) is not in this
artwork's palette any more (it was re-flattened, recoloured or merged) ..."),
and when it matches
more than one colour (another colour is named like that hex) with 409 ("... now
matches more than one colour of this artwork's palette, by hex or by name
..."); no job starts and it never removes a different colour. A stale cleanup
follows its removed colour's hex and never a name across the palette change, so
if a recolour has since given another colour that hex, that colour is what the
re-run (and the inherited form value) removes. The Cleanup form
inherits the nearest cleanup's settings (stale or not) with the removed colour
as the editor state's `inherited_remove_colour` (`{value, unresolved, hex,
problem}`, `null` when no cleanup ancestor removed a colour; `hex` is the
cleanup's own, `problem` is `"missing"` or `"ambiguous"` when unresolved), resolved against the palette a
Cleanup of the revision starts from: the cleanup's `removed_colour_hex` when
that resolves to exactly that colour; else, while the cleanup is not stale (its
index still addresses the same colour), that colour's current name when that
resolves to exactly it (a recolour can give two colours one hex); else `null`
with `unresolved`. Unresolved, the form keeps that hex with an alert that says
which problem it is (not in the palette any more, or more than one colour), and
while the field is unedited Run Cleanup is disabled
with the alert as its reason and the live Cleanup preview is paused with it
instead of requested; choosing a colour that resolves, or clearing the field to
remove nothing, unblocks both. Until that read arrives for the shown revision
the form holds the hex and, when a colour is inherited, Run Cleanup is disabled
with "Checking the colour to remove…" as its reason. So the form, its live preview and a Run Cleanup of the
inherited values remove the colour that cleanup removed. A Cleanup draft whose
removed colour is the inherited cleanup's value exactly as recorded is dropped
on load (it was saved from the recorded name, which may now be another
colour's); a different draft value is kept. So re-typing that exact recorded
name after a trace gave it to another colour reverts to the recorded colour, as
the note beside the field shows: an accepted limitation. A saved Cleanup draft
holds only the user's own removed colour: a value equal to the inherited one is
left out of it, so a value the server chose (such as a current name) is
resolved afresh on the next load rather than frozen. Beside the field the form states
the colour its value resolves to, matched exactly as the server's
`resolve_colour` matches (name or hex, case-folded; more than one match is
ambiguous), and judges it only against the shown revision's own palette. The project and revision
list endpoints report `removed_colour_hex` on every cleanup revision; for a
cleanup recorded before the hex was, it is resolved from the colour at
`removed_colour_index` in that cleanup's own `palette.json` (reported, not
stored). Trace revisions record only trace settings; the size
and removed colour they used come from their nearest cleanup ancestor. The
removed colour is identified by that cleanup's `removed_colour_index`, never by
resolving its recorded `remove_colour` name against a later palette: a trace's
colour names rename colours in the `palette.json` it writes, so a rename (for
example two colours swapping names) would otherwise make a trace or its live view on the canvas omit a different
colour than the cleanup removed. The editor state (`GET /api/revisions/{id}/editor`)
reports it as `removed_colour_index`. A cleanup recorded before the index was
has its `remove_colour` resolved against the palette it ran with: its own
`palette.json`, else the palette it started from.
Trace revisions recorded before the cleanup stage keep the size, removed colour
and cleanup settings embedded in `settings.trace` as they were traced.

An `upscaled-source` revision is the output of the manual, optional tiled
generative upscale. It is a parent-linked child of a source revision whose sole
artifact is a fresh full-frame `source.png` (the ComfyUI tiled-regen redraw of
the parent's source), and it marks palette, cleanup, trace, validation, and export stale
so the deterministic pipeline reruns on the higher-detail image. Its settings
record the effective upscale configuration, the resolved checkpoint, and the
seed for auditability, plus the pixel grids on both sides of the upscale:
`parent_source_size` (the parent's source) and `source_size` (its own
`source.png`, as decoded, since the node rounds the output to a multiple of 8),
each `[width, height]`. The parent source revision is left untouched.

Flatten preservation points (`protected_component_points`,
`preserved_colour_points`, and both ends of each `distinct_colour_point_pairs`
pair) are source-pixel coordinates, so they follow the source they were picked
on. Wherever the web app carries palette settings onto a revision with a
different source grid (the form filled from an ancestor's recorded settings,
the Flatten iterations stepped back to, and the project's un-run palette draft,
which records the revision it was picked on as `palette_revision`; a draft
saved before drafts recorded one is given the revision shown when it is first
loaded), each point
moves to the pixel holding the same relative position on the new grid:
`x' = floor((x + 0.5) * new_width / old_width)`, clamped to
`[0, new_width - 1]`, and likewise for `y`. The change between two revisions is
every upscale up from the first to their nearest common ancestor (undone) and
then down to the second (applied), from those recorded grids
(`web/src/sourceGrid.ts`). Points are left as they are when that change is
unknown: the revisions share no ancestor, or an upscale on the way recorded no
grids.

An `inkscape-edit` revision preserves an arbitrary reimport as the neutral
`inkscape-svg` artifact and adds its freshly rendered `inkscape-preview` plus
`inkscape-audit`. It does not relabel that SVG payload as the trace's
`traced-edge.svg`. Trace, validation, and export are
therefore stale until the deterministic trace stage is run again, and the
retrace hides the edit's `inkscape-*` artifacts along with the older run's
trace outputs; anything that marks trace stale (a flatten, label edit, cleanup
or source upscale) hides them the same way. SVG and PNG
handoff exports are always selected explicitly by the user; generating the PNG
inspection preview does not write it to the configured shared folder
automatically.

## Regeneration

Outputs are deterministic derivatives and may be regenerated after code,
settings, or input-name changes. Do not delete or overwrite source images as
part of regeneration. Remove obsolete derived subject folders after a source
rename so stale reports are not mistaken for current results.
