# Palette Inference and Raster Cleanup

## Implemented pipeline

`palette_infer.py` performs the following deterministic steps:

1. Load input as opaque RGB.
2. Resize a copy to at most 640 pixels for palette analysis.
3. Reduce its histogram to five bits per RGB channel.
4. Convert histogram colours from sRGB to CIE Lab.
5. Run deterministic weighted k-means, starting with 16 colours and weighted
   farthest-point seeding.
6. Merge candidates based on Lab distance, pixel share, and spatial
   fragmentation. In optional fixed-count mode, continue with the nearest
   allowed pair until the requested exact count is reached.
7. Compute the full per-pixel/per-palette Lab cost field and initial labels.
8. Detect coherent two-sided dark strokes from the original Lab pixels, before
   any destructive cleanup. Consolidate mixed quantisation fragments using the
   strongest ridge core's best average Lab fit, then protect the resulting
   single-colour stroke geometry.
9. Resolve ambiguous anti-aliased boundary pixels spatially while holding the
   protected stroke evidence fixed.
10. Add any explicitly point-selected protected components.
11. Remove undersized connected colour regions, except protected pixels.
12. Fold globally under-represented colours into their nearest surviving colour,
   except colours explicitly preserved by a point override.
13. Drop any palette entries no longer used, order remaining colours by pixel
   share descending, and render flattened/report outputs.
14. Persist segmentation contract v3 (`segmentation.npz`) containing final
   labels, the complete Lab cost field, protected intentional features,
   explicit user-protection provenance, and palette identity. Tracing consumes
   this contract rather than reclassifying the PNG.

The algorithm is deterministic for identical source pixels and settings. Its
result is a reproducible implementation of a chosen definition of "visually
meaningful colour," not an objective law of human vision.

## Current defaults

| Setting | Default | Meaning |
|---|---:|---|
| `initial_colours` | 16 | Deliberately over-complete starting palette |
| `final_colours` | automatic | Optional exact final palette size (2–64, not above the initial candidates) |
| `histogram_bits` | 5 | RGB histogram precision per channel |
| `analysis_max_dimension` | 640 | Maximum palette-analysis dimension |
| `similar_delta_e` | 28.0 | Merge any sufficiently close Lab candidates |
| `small_share` | 0.004 | Definition of a small colour family |
| `small_delta_e` | 36.0 | Wider tolerance for small similar families |
| `fragmented_share` | 0.035 | Maximum share for fragmentation rule |
| `fragmented_largest_region` | 0.30 | Largest-region coherence threshold |
| `fragmented_delta_e` | 24.0 | Distance allowed for fragmented shades |
| `min_output_colours` | 2 | Palette merge floor |
| `min_region_pixels` | 24 | Fold small regions unless they form a confident, coherent thin feature |
| `intentional_line_min_contrast` | 12.0 | Minimum two-sided source-Lab lightness contrast for a dark stroke |
| `intentional_line_min_length` | 8 | Minimum coherent ridge span or population |
| `aa_ambiguity_margin` | 7.0 | Lab cost margin below which spatial boundary resolution may relabel a pixel |
| `aa_smoothness` | 2.5 | Neighbour agreement weight for ambiguous boundary pixels |
| `aa_resolution_passes` | 5 | Maximum deterministic red/black spatial-resolution passes |
| `min_colour_share` | 0.01 | Fold colours below 1% of full image area |

`--protect-component-at X,Y` preserves only the four-connected flattened
colour component beneath that source-pixel point. `--preserve-colour-at X,Y`
exempts the entire inferred palette colour beneath a point from the global
usage cutoff. `--keep-colours-distinct-at X1,Y1:X2,Y2` prevents the two sampled
initial colour families from ever merging with each other, while allowing both
to participate in unrelated merges. All are explicit image-specific overrides and are recorded in
`palette.json`; neither generalises the selection to other small details.
Component protection is applied before automatic stroke normalization, so an
explicit selection always takes priority over inferred AA/stroke cleanup.

Only selected settings currently have CLI flags. The JSON report records the
complete settings dataclass for every run.

Fixed-count mode is an explicit contract, not another name for initial
quantisation candidates. Automatic mode retains the normal merge rules and
corpus baseline. Fixed mode uses those rules first, then deterministically
merges the closest pair not prohibited by a keep-distinct constraint. It fails
before committing output when the source has too few analysed colours, the
requested count exceeds the initial candidates, explicit distinct constraints
make the target impossible, or cleanup cannot retain the exact count.

## Small-region cleanup

- Connectivity is four-way: pixels must share an edge.
- Candidate replacement colours must touch the region boundary; diagonal
  neighbours are eligible as replacement surroundings but do not connect two
  source regions.
- A replacement target must already belong to a component meeting the minimum
  size. This prevents two tiny regions from swapping colours.
- Among eligible neighbouring colours, choose the closest Lab centre.
- Cleanup never removes pixels marked by the pre-cleanup, source-aware dark
  stroke detector. Contrast alone does not protect an isolated speck.
- Repeat monotonically until no safe changes remain, capped at eight passes.
- A region whose size equals the threshold survives; only sizes strictly below
  it are replaced.
- `--min-region-pixels 0` or `1` disables effective cleanup.

Do not tile connected-component labeling without implementing correct seam
union. A naïve tile boundary would split legitimate shapes and falsely classify
them as specks. The current implementation instead crops work around each found
component and parallelises independent input images.

## Under-represented colour cleanup

- This pass runs after small-region cleanup and measures full-resolution pixels.
- Colours occupying strictly less than the configured image share are folded
  into the nearest surviving Lab centre, regardless of region layout.
- At least `min_output_colours` are retained.
- `--min-colour-share 0` disables this pass.

## Performance

- Full-resolution nearest-centre assignment is chunked to bound memory.
- Multiple source images use a thread pool; `--jobs 0` automatically chooses up
  to four workers.
- `--jobs 1` is the deterministic serial debugging mode.
- Current corpus runtime is approximately six seconds with four workers on the
  development machine.

## Known limitations

- Lab distance is Delta E 1976, not CIEDE2000.
- Source transparency is flattened by `convert("RGB")`; alpha policy has not
  yet been designed.
- The palette selection thresholds were calibrated on the current nine-image
  corpus and need broader evaluation.
- Pixel-area cleanup is resolution-dependent. It must eventually be replaced
  or supplemented with physical-size controls for cutter output.
- Thin appendages attached to a large component are not removed merely because
  their local width is small. That requires morphological width/neck analysis.
