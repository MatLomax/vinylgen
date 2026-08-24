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
the current 768 DPI preview rendered from `traced-overlap.svg` and must be
regenerated whenever either SVG changes. `trace-fallbacks.png` is emitted only
when local curve-fit fallback occurs. `trace.json` records effective
configuration, backend versions, colour names/order, component/node/hole
counts, cleanup, render comparisons, warnings, and structural validation.

### Web revision artifacts

The web API stores these same files as content-addressed immutable artifacts.
Palette, trace, validation, and export jobs each create a child revision and
return its ID as `result_revision_id`. A revision's effective artifact set is
the newest artifact of each kind found while walking its parent chain. Revision
settings retain effective palette and trace defaults and list stale downstream
concerns; this makes retracing and export provenance explicit without mutating
an earlier result.

An `inkscape-edit` revision preserves an arbitrary reimport as the neutral
`inkscape-svg` artifact and adds its freshly rendered `inkscape-preview` plus
`inkscape-audit`. It does not relabel one SVG payload as both the semantically
distinct edge and overlap trace variants. Trace, validation, and export are
therefore stale until the deterministic trace stage is run again. SVG and PNG
handoff exports are always selected explicitly by the user; generating the PNG
inspection preview does not write it to the configured shared folder
automatically.

## Regeneration

Outputs are deterministic derivatives and may be regenerated after code,
settings, or input-name changes. Do not delete or overwrite source images as
part of regeneration. Remove obsolete derived subject folders after a source
rename so stale reports are not mistaken for current results.
