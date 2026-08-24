# Pipeline Architecture

The production pipeline is a sequence of explicit concerns. No stage silently
reconstructs data owned by an earlier stage.

```text
source raster
  -> colour.py         colour conversion, clustering primitives, confidence
  -> palette.py        palette inference orchestration
  -> segmentation.py   AA rejection and intentional thin-feature protection
  -> contracts.py      versioned segmentation.npz handoff
  -> cutter_cleanup.py optional physical feature removal/widening
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
commands for palette inference or tracing. Successful stages create immutable,
parent-linked revisions. Their settings record the complete effective palette
and trace configuration plus concern-level downstream staleness. Artifact
lookup walks revision ancestry, so a trace revision reuses its parent's source
and segmentation without copying blobs.

The palette and region editors are deterministic branches between palette
inference and tracing. They use the shared artwork canvas and intentionally
offer no freehand, fill, lasso, protection-painting, or manual RGB tools.
Whole-colour merges, name metadata, and point-selected four-way connected
region reassignment create new parent-linked revisions;
`segmentation.labels` remains authoritative and `flattened.png` is derived
mechanically. Undo and redo navigate revision ancestry.

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
Reimport accepts only passive, self-contained SVG, renders it headlessly at
768 DPI, and stores a neutral edited-SVG artifact, fresh inspection preview,
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
- Cutter cleanup is optional and operates on labels using physical or pixel
  thresholds; its defaults remain zero.
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
