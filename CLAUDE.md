# Vinylgen Session Guide

## Purpose

Vinylgen is intended to become a low-effort, configurable workflow that turns a
short sci-fi/fantasy subject description into limited-colour artwork and then
into practical, Inkscape-friendly vinyl-cutter SVG layers.

The project now has deterministic raster processing and an initial SVG tracing
vertical slice. It can infer a palette, flatten and clean a raster, create
shared-boundary overlap/edge SVG layers and emit trace audits. Both SVG variants
now pass structural validation across the nine-image corpus; physical cutter
readiness is not yet claimed.

## Working rules

- Work from the repository root.
- At the start of each task, warm-load durable project state with
  `worklog context` (or the worklog MCP `task-next`/`task-list` tools). The
  project database is `.worklog/tasks.db`; it is local agent working state and
  must not be committed.
- Use worklog for substantial cross-session work: create or update tasks,
  record non-obvious decisions with their rationale, journal meaningful
  progress and blockers, and attach relevant file/commit/URL links. Keep the
  in-session plan aligned with the durable worklog rather than treating either
  as a substitute for the other.
- Before marking any task as implemented or done, run a new adversarial review
  agent that did not implement the task. Give it the task scope, acceptance
  criteria, implementation, tests, and relevant specifications, and ask it to
  try to falsify the completion claim. Every review finding must be resolved in
  the implementation or explicitly refuted with evidence recorded in worklog.
  Do not mark the task implemented or done while any finding remains
  unaddressed or merely acknowledged.
- Before ending a substantial work session, update task statuses and write a
  concise worklog session summary so the next session can resume without
  reconstructing context from chat history.
- Deployment is part of implementation completion. After the required review
  and verification pass and the task status is changed to done, run the
  established deployment workflow and verify both local and public health
  before reporting completion.
- Preserve original inputs in `images/` unless the user explicitly requests a
  rename or replacement.
- Put all disposable scripts, previews, experiments, and benchmarks in
  `vinylgen/.tmp/`. Never use `/tmp` or another hidden external scratch area for
  this project.
- Use `scripts/inkscape-headless` for headless Inkscape renders. It redirects
  Inkscape/XDG state to writable directories under `.tmp/` and filters only the
  known harmless Inkscape 1.2.2 `GtkRecentManager` startup warning. Other stderr
  output and the Inkscape exit status remain visible.
- Whenever traced SVG output is regenerated or changed, always regenerate the
  subject's `traced.png` from `traced-overlap.svg` in the same task, using the
  preview policy in `vinylgen/preview.py` (768 DPI, longer side capped at
  1920 px). Never leave
  `traced.png` stale relative to the SVGs.
- Put durable generated results in `outputs/<subject>/`.
- Use lowercase, hyphenated, descriptive subject names.
- Keep deterministic guarantees separate from generative or visual judgments.
- Be adversarial about feasibility and quality; report limitations rather than
  agreeing reflexively.
- Generated raster colour count is not trustworthy. Validate exact output
  colours mechanically.
- Do not force six colours by default. Infer the palette automatically; small,
  similar shades should merge while small high-contrast accents may survive.
- For cutter-oriented region cleanup, edge-sharing (four-way) connectivity is
  authoritative. Corner-only contact does not make one physical vinyl region.
- Avoid naïvely tiling connected-component cleanup: shapes crossing tile seams
  would be split incorrectly. Independent images may run concurrently, and
  pixel assignment is already chunked.
- Any generative edit must create a new version. Deterministic recolouring and
  trace-setting changes may reuse geometry but should remain recoverable.

## Current commands

```bash
# Process the complete corpus with automatic concurrency.
python3 palette_infer.py images

# Configure speck cleanup; regions strictly smaller than X are replaced.
python3 palette_infer.py images --min-region-pixels 50

# Disable speck cleanup or force serial execution.
python3 palette_infer.py images --min-region-pixels 0 --jobs 1

# Trace every prepared subject.
python3 svg_trace.py outputs

# Run the regression suite in parallel (one worker per core; ~15 s). Always use
# the project virtual environment: the system Python does not include web-test
# dependencies such as httpx. `-j 1` runs serially; test names narrow the run.
.venv/bin/python scripts/run_tests.py
```

Default palette-stage anti-alias region threshold: **24 pixels**. Sub-threshold
regions fold into a touching colour unless they are confident, coherent thin
features; contrast alone does not preserve a speck.
The default whole-colour usage cutoff is **1%**. With four workers, the current
nine-image corpus processes in roughly six seconds on the present machine.

## Current output contract

For `images/<subject>.png`, processing creates:

```text
outputs/<subject>/
├── source.png
├── flattened.png
├── segmentation.npz
├── palette.json
└── report.png
```

- `flattened.png` contains exactly the inferred RGB palette.
- `segmentation.npz` is contract v3's handoff to tracing: final labels, the
  complete per-pixel/per-palette Lab cost field, automatic protected-feature
  evidence, explicit user-protection provenance, and palette RGB.
- `palette.json` is the audit record: palette, descending pixel shares, merge
  decisions, spatial statistics, cleanup counts, source dimensions, and all
  settings.
- `report.png` contains the full flattened image plus a black legend band with
  exact hex values and pixel-area percentages ordered descending.
- `outputs/contact.png` is a review contact sheet and may be regenerated.

Palette flattening may fold tiny similar-colour anti-alias fragments, but must
not delete high-contrast detail merely because it is small. Cutter-oriented
area/width removal and widening remain explicit trace-stage operations.

## Terminology

Use the canonical terms defined in the glossary, including the path-node types
(cusp, symmetric, smooth), whose names differ from Inkscape's.

@GLOSSARY.md

## Reference map

Read only the references relevant to the current task.

| Reference | Read when |
|---|---|
| [Pipeline architecture](docs/architecture.md) | Changing stage ownership, contracts, orchestration, or module boundaries |
| [Product brief](docs/product-brief.md) | Working on generation settings, editing, UI, presets, or overall scope |
| [Palette pipeline](docs/palette-pipeline.md) | Changing inference, colour conversion, merging, cleanup, performance, or thresholds |
| [Files and output contract](docs/files-and-outputs.md) | Adding artifacts, reorganising folders, changing JSON/report formats, or naming files |
| [Validation and testing](docs/validation-and-testing.md) | Adding checks, changing connectivity/cleanup, benchmarking, or judging cutter readiness |
| [SVG tracing specification](docs/svg-tracing-spec.md) | Implementing or changing tracing, SVG geometry, cutter cleanup, layer ordering, or trace outputs |
| [Roadmap and risks](docs/roadmap-and-risks.md) | Planning SVG tracing, physical vinyl handling, generative editing, or the application UI |
| [Tiled-upscale stage](docs/upscale-stage.md) | Planning or implementing the generative pre-flatten tiled-upscale stage or its ComfyUI integration |

## Current corpus baseline

The nine input subjects are: `cosmic-jellyfish`, `crashed-ring`,
`crashed-spacecraft`, `desert-mech`, `desert-rocket`, `floating-castle`,
`griffin-castle`, `kraken-lighthouse`, and `sky-whale-city`.

With current defaults, their inferred counts are respectively 8, 7, 7, 6, 5,
6, 7, 6, and 6. These are deterministic results from the implemented rule,
not human-annotated ground truth. Threshold changes may legitimately change
them.

## Immediate next technical milestone

The shared-boundary tracer has moved beyond the initial three-subject set. Its
current corpus sweep uses zero removal, filling, and widening thresholds by
default. All nine subjects have been regenerated with colour-area-preserving
local boundary fallback and current capped-size `traced.png` previews. Conditional
`trace-fallbacks.png` artifacts identify tightened boundaries. Visual inspection
and physical cutter testing remain necessary before claiming cutter readiness.

A generative pre-flatten tiled-upscale stage drives the local ComfyUI directly
so the manual export/upscale/reimport round trip is gone: on a source revision
the user runs an optional tiled regen that commits an `upscaled-source` child.
See [docs/upscale-stage.md](docs/upscale-stage.md).

## Silhouette Studio DXF import scaling (verified 2026-09-01)

Silhouette Studio's DXF importer **ignores `$INSUNITS` and the DXF's absolute
size.** It **scale-to-fits every imported DXF so its longest bounding dimension
becomes a fixed ≈ 263.05 mm** (a fit-to-12×12-page target).

Evidence: a 233.575 mm-wide art-only DXF and a 305 mm-square DXF both imported at
**263.05 mm** on their long side — different inputs, same output.

### Import art at TRUE size (1:1), no in-Studio resizing — the working trick

Give the DXF a bounding box whose **longest side = 263.05 mm**: add a 263.05 mm
boundary rectangle and place the real-millimetre art inside it. Studio then fits
263.05 → 263.05 = **1:1**, so the art lands at its true mm size *and* true relative
position. Delete the throwaway boundary rectangle after import.

Verified: boundary 263.05 → 263.05 mm, and (boundary deleted) art 24 mm → **24.00
mm**, 233.58 mm → **233.58 mm**.

- 263.05 mm is empirical for a **12×12 (304.8 mm)** page/mat — re-measure if the
  page or mat size changes.
- Consequence: making the DXF the *sheet* size (e.g. 305 mm) does **not** work —
  it gets squeezed to 263.05. The canvas must be 263.05 mm, not the sheet size.

### Manual fallback

Transform ▸ Scale ▸ Specify Dimensions, aspect-lock off. When driven over a VM's
virtual input the numeric fields are flaky — clear with End + repeated Backspace
and verify by screenshot before Apply.
