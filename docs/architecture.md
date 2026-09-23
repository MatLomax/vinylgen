# Pipeline Architecture

The production pipeline is a sequence of explicit concerns. No stage silently
reconstructs data owned by an earlier stage.

```text
source raster
  -> colour.py         colour conversion, clustering primitives, confidence
  -> palette.py        palette inference orchestration
  -> segmentation.py   AA rejection and intentional thin-feature protection
  -> contracts.py      versioned segmentation.npz handoff
  -> cutter_cleanup.py cleanup stage: physical size, removed colour, optional
                       physical feature removal/widening of the labels
  -> boundary_graph.py dense confidence-adjusted shared sub-pixel evidence
  -> curve_fit.py      straight sections, corners, and cubic Bezier emission
  -> svg_writer.py     Inkscape layers and SVG serialization
  -> validation.py     SVG structural validation
  -> tracing.py        vector-stage orchestration and audit
```

`reporting.py` owns palette inspection images. `cli_palette.py` and
`cli_trace.py` own argument/config parsing. The top-level `palette_infer.py` and
`svg_trace.py` files are executable entry points only.

The web application calls the same Python orchestration functions through
typed background jobs in `vinylgen/web/pipeline.py`; it never constructs shell
commands for palette inference, cleanup or tracing. Successful stages create
immutable, parent-linked revisions. Their settings record the complete
effective configuration of their stage plus concern-level downstream staleness.
Artifact lookup walks revision ancestry, so a trace revision reuses its
parent's source and segmentation without copying blobs.

The web workflow is Flatten, Palette, Edit, Cleanup, Trace. Cleanup
(`vinylgen/web/cleanup.py`) owns the physical output size, the removed colour
and the cutter cleanup. A `cleanup` revision records them under
`settings.cleanup`. A cleanup always starts from the raster before any
earlier cleanup, so re-running it with cleanup off, another size or other
thresholds replaces that cleanup rather than stacking on it. The user's
explicit protection wins: explicitly protected detail is never removed or
consumed. When the run changed pixels the revision also holds the cleaned
`flattened.png`, a `segmentation.npz` whose labels are cleaned and whose
automatic protection is cleared where labels changed (palette costs, explicit
protection and palette carried unchanged), refreshed palette shares and a
re-rendered `report.png`. A run that changed nothing records only the size,
removed colour and its raster's palette shares, and its effective raster is
the uncleaned one. The trace job never
cleans: it traces the segmentation it inherits and reads the size and removed
colour from the nearest `cleanup` ancestor. When a revision in between marks
cleanup stale (a flatten, a label edit or a source upscale), the trace is
blocked until Cleanup is run again; with no cleanup ancestor at all it traces
in pixels and cuts every colour. Trace revisions recorded before the cleanup
stage embed those values in `settings.trace`; they are left as recorded, the
web form shows them in Cleanup, and a new trace from one has no cleanup
ancestor. The trace CLI keeps its size, removed-colour and cleanup flags and
applies the same `cutter_cleanup.apply_cleanup` in memory before tracing,
leaving the subject's files unchanged. Like the web trace, it infers layer
order and validates against the cleaned labels and the raster derived from
them, which is what was traced.

The palette and region editors are deterministic branches between palette
inference and tracing. They use the shared artwork canvas and intentionally
offer no fill, lasso, protection-painting, or manual RGB tools. Whole-colour
merges, name metadata, point-selected four-way connected region reassignment,
and a per-pixel pencil that repaints individual pixels to an existing palette
colour create new parent-linked revisions; `segmentation.labels` remains
authoritative and `flattened.png` is derived mechanically. Editors always act on
the raster from before any cleanup: the Cleaned image is shown but never
edited, and an edit after a cleanup marks that cleanup stale. Because connectivity
is recomputed from the label grid, a pencil stroke that severs a region splits
it into separate components with no extra bookkeeping. Undo and redo navigate
revision ancestry.

Source generation is a provider-backed branch before deterministic processing.
`vinylgen.web.generation` expands the short subject through a persisted,
deterministic art-direction template and sends only that text plus provider
settings to the selected provider. It never resolves artifacts or supplies an
earlier revision as an implicit reference. A successful request creates a new
root `generated-source` revision containing the short and expanded prompts for
auditability (the expanded provider prompt is not shown in the Create panel),
complete effective settings, provider request metadata, an explicit empty
reference list, and one full-frame `source.png` normalized to the requested
dimensions. Provider output remains probabilistic; only persistence and output
dimensions are deterministic.

Tiled generative upscale is a second provider-backed branch between source
generation and deterministic processing, owned by `vinylgen.web.upscale`. It is
manual and optional: on a source revision the user runs it explicitly, and it
drives the local ComfyUI directly over HTTP (`vinylgen.web.comfy_client`,
`COMFYUI_URL`) to run an upscale model followed by a tile-by-tile diffusion
redraw under a tile ControlNet, adding real detail the low-resolution generation
lacks. Because it runs a sampler it is generative, so it commits a new immutable,
parent-linked `upscaled-source` revision (never mutating its parent) whose
`source.png` is the redrawn canvas and which marks palette, cleanup, trace,
validation, and export stale. It is distinct from the palette stage's `flatten_upscale`,
which is post-flatten, deterministic, and palette-preserving for trace precision;
the two compose. Sampling resolution is capped for the deployment's VRAM, invalid
model selections are rejected before the job is queued, and an unreachable
ComfyUI fails only this branch, leaving generation and the deterministic pipeline
untouched.

The personal deployment's provider is `CodexImageProvider`. It launches the
installed Codex client in a new empty `.tmp/` job directory with `exec`, JSONL
events, and `--ephemeral` for every request. Codex alone owns its ChatGPT login;
Vinylgen neither locates nor parses OAuth material and strips
`OPENAI_API_KEY` from the child environment. The adapter retains only bounded
counts for a fixed allowlist of event types, validates exactly one
decodable PNG, and removes scratch state before the runner crosses the immutable
revision commit boundary. Cancellation and timeout terminate the owned process
group. The direct OpenAI Image API remains an unimplemented contingency, not a
runtime fallback.

The Inkscape handoff is an immutable branch after tracing. SVG and PNG
downloads are explicit choices, and configured shared-folder saves are atomic.
Reimport accepts only passive, self-contained SVG, renders it headlessly with
the shared preview policy (`vinylgen/preview.py`), and stores a neutral edited-SVG artifact, fresh inspection preview,
and validation audit on a new `inkscape-edit` revision. Because an arbitrary
edit cannot honestly be classified as both edge and overlap trace geometry,
trace, validation, and export are stale until retracing. The generated trace on
the parent is untouched.
The hosted service never claims that a phone or remote browser can launch a
desktop Inkscape process.

## Stage invariants

- Palette inference decides which colours exist; it does not perform cutter
  cleanup.
- Segmentation decides which raster structures are AA and which are protected
  intentional features. Its result is persisted, validated, and consumed by
  tracing.
- Cutter cleanup is its own stage between editing and tracing. It is optional
  and operates on labels using physical or pixel thresholds; its defaults
  remain zero. One physical size governs both its thresholds and the traced
  SVG. The removed colour is excluded from cleanup and keeps its pixels.
- Tracing never cleans labels itself; it consumes the cleanup stage's labels,
  size and removed colour (the CLI runs the same cleanup inline first).
- Every colour interface has one stable canonical chain identity. Pair-specific
  palette costs move shared vertices away from the integer grid without
  allowing adjacent colours to fit their interface independently.
- Boundary construction never simplifies geometry. Dense samples, ownership,
  junctions, and protected evidence pass unchanged to the geometry modeller.
- Curve fitting classifies sustained lines, continuous curves, and supported
  corners at multiple scales, then runs once per canonical chain. The colour on its opposite side
  consumes the exact reversed line/Bézier sequence. Only chain endpoints,
  multi-colour junctions, and sustained geometric corners are hard anchors;
  semantic protection never turns sampled pixels into corners.
- Post-fit validation audits protected component survival, topology, deviation,
  area, and width. Explicit user protections retain their provenance and alone
  may trigger ordered local recovery; automatic evidence is reported without
  globally forcing raster-exact contours. Recovery tightens the curve,
  reclassifies geometry, adds a non-corner constraint, and uses a reported
  dense fallback only when all modelled forms fail.
- SVG writing does not infer geometry. Validation does not repair it.
