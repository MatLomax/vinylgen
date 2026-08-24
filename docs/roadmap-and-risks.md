# Roadmap and Risks

## Completed slice

- Descriptive source filenames.
- Deterministic variable-count palette inference.
- Exact-colour raster flattening.
- Configurable small-region cleanup.
- Four-connected cutter-oriented speck removal.
- Per-subject JSON and visual reports.
- Concurrent batch processing and regression tests.

## Recommended next milestone: SVG tracing

1. Create one binary mask per final palette colour.
2. Evaluate tracing backends rather than committing prematurely.
3. Preserve holes and compound paths.
4. Assemble named, recolourable SVG layers.
5. Provide trace tolerance, smoothing, corner preservation, and node limits.
6. Render SVG back to PNG and compare with `flattened.png`.
7. Open representative outputs in Inkscape.
8. Perform at least one physical cut and weeding test.

Trace per-colour masks rather than directly tracing the original colour raster.
This gives predictable palette layers and deterministic recolouring.

## Physical production settings

Pixel thresholds are temporary. Cutter suitability ultimately requires:

- Finished width and height in physical units.
- Minimum feature width.
- Minimum gap width.
- Minimum isolated area.
- Narrow-neck and acute-corner checks.
- Material/blade presets if useful.
- Tiled versus overlapping colour construction.
- Optional bleed and registration marks.

The same geometry may be easy at 600 mm and impossible at 80 mm; do not call an
SVG cutter-ready without a target physical size.

## Later application stages

1. Minimal viewer for original/flattened/vector comparisons and palette edits.
2. Short-subject image generation with persisted expanded prompt and configurable
   run settings.
3. Global palette replacement, colour merging, region recolouring, and deletion.
4. Whole-image generative edits with version branching.
5. Masked generative edits with explicit preservation warnings.
6. Presets, vinyl-stock mapping, and richer export options.

## Principal risks

- Generative exclusion and composition compliance are probabilistic.
- Local AI edits may change untouched geometry.
- Quantisation can be technically correct but aesthetically poor.
- Low-node tracing necessarily loses some contour fidelity.
- Attractive raster detail may be unpleasant or impossible to weed.
- Arbitrary generated aspect ratios may require cropping or extension.
- Exact N-colour forcing can preserve redundant colours or remove meaningful
  accents; automatic inference should remain the default.
- Building a full vector editor would duplicate Inkscape poorly; keep internal
  editing focused on pipeline-specific corrections.

The product should consistently label generation constraints as requests,
post-processing properties as guarantees, and semantic/visual checks as review
warnings.
