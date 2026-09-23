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
│       ├── traced-overlap.svg
│       ├── traced-edge.svg
│       ├── traced.png
│       ├── trace-fallbacks.png  # only when local fallbacks occur
│       ├── trace-fallbacks-overlay.png  # transparent twin of trace-fallbacks.png
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

### `contact.png`

Convenience review sheet across all flattened outputs. It is generated outside
the Python tool with ImageMagick at present and is not part of the per-subject
contract.

### Trace artifacts

`traced-edge.svg` is a gap-free planar partition built from boundaries shared
verbatim by neighbouring colours. `traced-overlap.svg` uses the same partition
and expands lower layers when overlap is configured above zero. `traced.png` is
the current preview rendered from `traced-overlap.svg` and must be
regenerated whenever either SVG changes. It targets 768 DPI, but its longer
side is capped at 1920 px; `trace.json` records the effective preview size under `preview`. `trace-fallbacks.png` is emitted only
when local curve-fit fallback occurs, and `trace-semantic-warnings.png` only
when the semantic feature audit fails; both paint their marks over the source
faded toward white, as standalone audit images. Each has a transparent RGBA
twin written under exactly the same condition and removed with it:
`trace-fallbacks-overlay.png` draws every tightened (dense-fallback) boundary
as a cyan stroke over a dark halo, and `trace-missed-details-overlay.png` marks
every missed source pixel in opaque red. Every other pixel of both is fully
transparent, and both are the size of the traced pixel grid (`flattened.png`,
the SVG viewBox), so they register exactly over the traced SVG. The web app
offers only these transparent twins, as the Trace canvas toggles "Tightened
boundaries" and "Missed source details (red)"; it never shows the opaque
images as layers. Runs traced before the twins existed have none and show no
toggle. `trace.json` records effective
configuration, backend versions, colour names/order, component/node/hole
counts, render comparisons, warnings, and structural validation. Its
`settings` block holds the trace settings and `cleanup_settings` the size,
removed colour and cleanup settings it used, with the physical size resolved.
`cleanup` holds the cleanup summary when the trace CLI cleaned inline, and is
`null` when the labels came from the cleanup stage, whose `cleanup.json` holds
that summary.

### Cleanup artifacts

The cleanup stage writes `cleanup.json`: the requested and resolved settings,
the raster dimensions, pixel or physical mode with the physical size in mm,
the omitted colour, the number of changed pixels, and the cleanup summary:
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
are excluded. A cleanup replaces any earlier cleanup, so an older `cleanup`
revision's raster artifacts (`flattened`, `segmentation`, `palette`, `report`)
are skipped once a newer cleanup has been passed. A `trace` run likewise
writes its complete output set, so it hides every trace-kind artifact of an
older run, including optional audits (fallbacks, semantic warnings and their
overlays) it did not repeat, together with everything made of that older run:
its `inkscape-edit` artifacts, its `validation` and its `export`. A trace
revision marks validation and export stale, and trace revisions recorded
before it did are treated the same way, so a run started from an older run's
validation or export never inherits them. An export packages the run's own
validation: an export job is refused ("revision has stale prerequisite
concerns: validation") until the current trace run has been validated, whether
the parent marks validation stale itself or simply has no effective
`validation` artifact. `GET
/api/revisions/{id}/effective-artifacts?before_cleanup=true` skips every
cleanup revision's raster, resolving the raster the cleanup stage starts from.
Revision settings retain the effective settings of their stage and list stale
downstream concerns; this makes retracing and export provenance explicit
without mutating an earlier result.

A `cleanup` revision records `settings.cleanup` (width and height in mm, removed
colour, and the cleanup switch, action and thresholds) and marks trace,
validation, and export stale. When it changed pixels its artifacts are the
cleaned `flattened`, `segmentation`, `palette` and `report` plus the
`cleanup-audit` (`cleanup.json`); a run that changed nothing (cleanup off, or
nothing under the thresholds) holds only `palette` (its raster's shares) and
the `cleanup-audit`, and its effective raster is the uncleaned one. Palette and
label edits read, and build on, the raster from before any cleanup (the
`before_cleanup` lookup), so an edit made after a cleanup never carries that
cleanup's changes. Palette revisions, label edits and upscaled sources mark
cleanup stale, so an earlier cleanup's audit and settings no longer apply
beyond them, and a trace job is refused ("the artwork changed after Cleanup:
re-run Cleanup before tracing") until Cleanup runs again. The web Trace panel
offers a one-click re-run of that cleanup with its own recorded settings. Trace revisions record only trace settings; the size
and removed colour they used come from their nearest cleanup ancestor.
Trace revisions recorded before the cleanup stage keep the size, removed colour
and cleanup settings embedded in `settings.trace` as they were traced.

An `upscaled-source` revision is the output of the manual, optional tiled
generative upscale. It is a parent-linked child of a source revision whose sole
artifact is a fresh full-frame `source.png` (the ComfyUI tiled-regen redraw of
the parent's source), and it marks palette, cleanup, trace, validation, and export stale
so the deterministic pipeline reruns on the higher-detail image. Its settings
record the effective upscale configuration, the resolved checkpoint, and the
seed for auditability; the parent source revision is left untouched.

An `inkscape-edit` revision preserves an arbitrary reimport as the neutral
`inkscape-svg` artifact and adds its freshly rendered `inkscape-preview` plus
`inkscape-audit`. It does not relabel one SVG payload as both the semantically
distinct edge and overlap trace variants. Trace, validation, and export are
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
