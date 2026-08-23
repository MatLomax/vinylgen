# Product Brief

## Intended experience

The eventual tool should accept a very short subject—sometimes one word—and
create one independent sci-fi or fantasy image per run with minimal effort. A
structured run configuration expands that subject into art direction and then
passes the result through deterministic flattening, cleanup, validation, and
SVG tracing.

The desired loop is:

```text
Generate → inspect → flatten → inspect/edit → trace → inspect/export
```

This is an assisted production workflow, not a promise that generative output
will be cutter-ready without review.

## Per-run configuration

Planned controls include:

- Short subject prompt.
- Aspect ratio or exact final dimensions; 64:45 landscape was the original
  working ratio but must not be hard-coded.
- Automatic or user-forced colour count.
- Automatic, partially locked, or fully supplied palette.
- Required subjects, objects, and motifs.
- Rejected subjects, objects, and motifs.
- Art-detail level.
- Cut-detail and trace-fidelity settings.
- Reusable presets.

Original rejected motifs included occult/all-seeing/floating eyes, mandalas,
Chinese dragons, pirates, samurai, trains, and Gundam. They are defaults from an
early prompt, not permanent hard-coded policy; exclusions must be configurable.

Each generation request must ask for one complete full-frame scene, never a
collage, grid, contact sheet, split composition, variants, or multiple outputs.
Earlier images must not become references for new independent generations.

For a personal self-hosted installation, generation uses a fresh ephemeral
session of the locally authenticated Codex client. Subscription limits and
image-tool availability are external, evolving constraints and must surface as
job failures rather than trigger a hidden API-billed fallback.

## Visual intent

- Flat, substantial, hard-edged graphic shapes.
- Solid colour fills; no gradients, transparency, glow, blur, dithering, or
  simulated extra tones in the intended final design.
- Strong central focal point.
- Foreground, midground, background, and depth from overlap, scale,
  perspective, and silhouette.
- Avoid hairlines, flecks, delicate isolated features, and geometry too small
  to cut or weed.

Generation requests these properties; deterministic processing enforces only
the properties it can actually measure.

## Detail has three meanings

Do not collapse these into one internal parameter:

1. **Art detail** changes semantic complexity and requires generation or
   generative editing.
2. **Cut detail** controls the minimum physical feature retained by cleanup.
3. **Trace fidelity** controls how closely SVG paths follow raster contours.

A friendly UI may link them in presets, but advanced controls must expose them
separately. Post-processing can remove detail; it cannot invent meaningful
semantic detail.

## Editing modes

1. **Whole-image generative edit:** add/remove semantic detail or change the
   composition. Expect unrelated drift; always branch a version.
2. **Masked generative edit:** request a local semantic change while preserving
   other areas as closely as possible. Preservation is not guaranteed.
3. **Deterministic raster/vector edit:** replace a palette colour, merge
   colours, reassign/delete a region, adjust cleanup, or retrace.
4. **External vector edit:** hand off sophisticated path work to Inkscape rather
   than building an inferior general-purpose vector editor.

## Honest capability boundary

Deterministic guarantees can cover aspect ratio after processing, palette
membership, exact fill count, region sizes, SVG structure, node/path counts,
and forbidden SVG effects. Composition quality, subject correctness, single
conceptual scene, and absence of rejected motifs remain probabilistic visual
checks requiring review.
