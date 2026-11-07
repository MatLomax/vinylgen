# Vinylgen Glossary

This file defines Vinylgen's canonical terminology. Product copy, code,
documentation, worklog tasks, reports, and discussions should use the canonical
term where practical. Aliases help interpret existing language; they are not
automatically interchangeable when an entry calls out a narrower meaning.

## Language conventions

- Use British English in user-facing language: **colour**, **centre**, and
  **optimisation**. Existing code identifiers may retain established spellings.
- Describe deterministic facts as guarantees only when they are mechanically
  checked. Describe composition, appearance, and physical usability as judgments.
- Describe raster geometry by colour ownership rather than assuming a universal
  foreground, background, positive, negative, vinyl, or gap side.
- Use **source-pixel coordinates** for image-specific preservation selections.
- Name a setting by what it controls, not by an implementation shortcut.

## Product and workflow

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **Vinylgen** | The complete workflow and application. | the app, the project | VinylGen, Vinyl Gen |
| **subject** | The lowercase, hyphenated identity used for a standalone CLI input and its durable output directory. | subject name | project, when referring to web state |
| **project** | A durable web-workflow container with a name, slug, revisions, and artifacts. | artwork project | subject, except when explaining CLI correspondence |
| **workflow stage** | A user-facing phase: Generate, Inspect, Flatten, Trace, Validate, or Export. | stage | page, job, revision |
| **pipeline stage** | A deterministic processing boundary that owns a contract and settings: palette, trace, validation, or export. | processing stage | workflow stage when the distinction matters |
| **art detail** | Semantic or compositional complexity changed through generation or generative editing. | generation detail | cut detail, trace fidelity |
| **cut detail** | The minimum physical feature retained, removed, or widened for practical cutting and weeding. | cleanup detail | art detail, trace fidelity |
| **trace fidelity** | How closely fitted vector geometry follows the authoritative flattened raster. | tracing accuracy | art detail, cut detail |
| **inspector** | The explicitly opened closer-look surface where pan, zoom, comparison, preservation-point overlays, and inspection overlays may capture gestures. The main stage canvases carry their own split/blink comparison (see DESIGN.md, Canvas Comparison). | full image viewer, closer look | viewer when it could mean an inline preview; protection painting |
| **inline preview** | A passive image embedded in the workflow that preserves ordinary page scrolling and native touch behavior. | image preview, thumbnail | canvas, interactive viewer |

## Source generation and masks

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **original input** | The authoritative user-supplied or provider-produced raster before any later generative edit or deterministic derivation. Corpus inputs under `images/` are preserved unless explicitly replaced or renamed. | original image, original raster | source image, when a later branch input is meant |
| **source image** | The immutable raster input to a particular deterministic project branch or revision. Import, generation, or an applied generative edit may establish a new source image without overwriting its ancestor. | source raster, effective source | original input, unless it is the first source |
| **generated source** | A source image produced by a provider from a prompt. | generated image | flattened image, candidate |
| **generative edit** | A probabilistic semantic image change that always creates a recoverable source revision. | AI edit | deterministic edit |
| **masked generative edit** | A generative replacement or removal constrained by an exact binary mask. | masked edit, inpainting | mask edit, if it implies editing the mask artifact itself |
| **generative mask** | A full-source binary selection defining where generated pixels may survive hard composition. Painting this mask selects an area; it does not paint output colours. | edit mask, binary mask, selection mask | preservation area, colour painting |
| **hard-mask composition** | Copying candidate pixels only where the exact binary mask permits them, so no generated pixel outside the mask survives. | exact-mask composition | soft mask, approximate preservation |
| **candidate** | One generated result offered for review before application. | edit candidate, replacement candidate | revision, source, variant when identity matters |

## Palette inference and raster data

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **palette inference** | Deterministically deriving a limited set of meaningful colours from a source image using configured analysis, merge, and cleanup rules. | palette stage, colour inference | quantisation, when referring to the whole stage |
| **initial colour candidates** | The deliberately over-complete cluster centres from which the final palette is inferred. | initial colours, starting palette | colour count, final colours |
| **final colour count** | The exact number of distinct RGB palette entries used by the flattened image after all merges and cleanup. | output colour count, inferred colour count in Automatic mode | initial colours |
| **Automatic colour count** | The mode in which deterministic inference decides the final colour count. | inferred count, automatic palette | fixed count |
| **Fixed colour count** | **Planned, not currently implemented:** a requested exact final colour count. Its validity and protected-feature behavior must be defined before release. | forced colour count, target colour count | initial colours, maximum colours; claiming current support |
| **palette** | The ordered final set of exact RGB colours and their names used by the flattened image and downstream layers. | final palette | colour candidates |
| **palette colour** | One exact RGB member of the final palette. | colour, swatch, palette entry | shade family, source colour |
| **colour family** | A source-derived cluster identity considered during inference and merge decisions. | cluster family, candidate family | palette colour, once final merging is complete |
| **palette merge** | Replacing one complete palette colour and all of its labels with another palette colour. | merge colours, whole-colour merge | recolour, paint, blend |
| **pencil** | Repainting individual flattened pixels to an existing palette colour by dragging on the artwork. Because four-way connectivity is authoritative, a stroke that severs a region splits it into separate regions on the next recompute. | pixel pencil, per-pixel paint | flood fill, brush radius, colour adjustment |
| **touch-up** | An area recolour or **pencil** stroke made on the cleaned raster while its cleanup is current. It leaves the cleanup current, is traced as painted, and is replayed by the next Cleanup run: a stroke always, an area recolour only while its region is unchanged (otherwise it waits for a run where it is), and never over a later edit; a touch-up whose colour was merged away is dropped. Painting the colour the cleanup removes records an erase, which a later run paints in whatever colour it removes (it waits while a run removes none); any other touch-up keeps its own colour. | touch-up edit | palette merge, edits made without a current cleanup |
| **manual colour painting** | Flood filling, radius brushes, lasso selection, freehand recolouring into new colours, or direct output-colour (RGB) adjustment. These remain unsupported in the flattened-image UI, which permits only whole-colour merge, connected-area reassignment, and the per-pixel **pencil**. | raster painting, manual recolouring | palette merge, pencil, generative-mask painting |
| **flattening** | Rendering every output pixel as exactly one member of the final palette after deterministic labelling and cleanup. | palette flattening, flatten stage | image generation, tracing |
| **flattened image** | The authoritative exact-colour raster output, `flattened.png`. | flattened raster, palette raster | preview, report image |
| **label** | The integer palette index assigned to one pixel in the authoritative label map. | palette label, colour label | RGB value |
| **label map** | The full per-pixel array of palette labels stored in `segmentation.npz`. | segmentation labels | flattened image, mask |
| **segmentation contract** | The versioned `segmentation.npz` handoff containing labels, Lab costs, palette identity, and protection evidence. | segmentation bundle, tracing handoff | cache, inferred PNG data |
| **Lab cost** | The stored perceptual colour-assignment cost for one pixel and one palette entry. | palette cost, colour distance field | confidence, unless the conversion is defined |
| **anti-alias resolution** | Deterministic spatial reassignment of ambiguous boundary pixels while protected evidence remains fixed. | AA cleanup, boundary cleanup | blur removal, smoothing |

## Regions, connectivity, and preservation

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **colour region** | A four-connected set of pixels with the same palette label. | region, connected region, component | shape, object, layer |
| **four-connected** | Connected through shared pixel edges only; corner contact does not connect regions. | edge-connected, four-way connected | touching, without saying whether corners count |
| **touching colour** | A different palette colour sharing a region boundary and therefore eligible for applicable replacement rules. | neighbouring colour, adjacent colour | nearest colour, unless boundary contact is also required |
| **small-region cleanup** | Palette-stage replacement of colour regions strictly below the configured pixel threshold, subject to protection rules. | speck cleanup, raster region cleanup | cutter cleanup, vector cleanup |
| **whole-colour usage cleanup** | Palette-stage folding of a globally under-represented palette colour into a surviving colour. | minimum-colour-share cleanup, usage cutoff | small-region cleanup |
| **protected feature evidence** | Stored evidence that a feature should survive applicable cleanup and receive trace validation attention. | protection evidence, protected feature | guaranteed cutter-ready feature |
| **automatic protection** | Protection evidence inferred from coherent source-aware thin-feature analysis. | automatic protected feature | explicit protection |
| **explicit component protection** | A user-selected source point preserving only the four-connected flattened colour region beneath it. | protect component, protected component selection | preservation area, whole-colour preservation |
| **whole-colour preservation** | A user-selected source point exempting the entire inferred palette colour beneath it from the global usage cutoff. | preserve colour, colour preservation | component protection |
| **distinct-family constraint** | A selected pair of source points preventing their two initial colour families from merging with each other. | keep colours distinct, distinct-colour pair | palette lock, unless broader locking exists |
| **preservation selection** | The general UI category for explicit component protection, whole-colour preservation, and distinct-family constraints. | preservation control, preservation constraint | generative mask, manual painting |

## Tracing, layers, and cleanup

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **tracing** | Deterministically converting the segmentation contract's labels into the planar-map file with one call to t3 (`t3api.trace`), and from it the edge variant and the audits. | trace stage, SVG tracing | vectorisation, if shared-boundary guarantees matter |
| **t3** | The turbo tube tiler, the tracing engine, installed as the `t3api` wheel; it draws lines, circular and elliptical arcs and cubics, each kind within its own tube half-width around the label boundaries (1.75 px each by default; arcs, ellipses and cubics can be off). | turbo tube tiler, `t3api` | fitter, vectoriser |
| **planar-map file** | `map.json`: t3's drawing as a label map, every boundary edge stored once with the faces on its two sides and every face as rings of signed edge references, in paint order, every number exact. The trace's artefact and the one source of its geometry. | planar map, `map.json`, traced map | face map file (`faces.json`, which stores every shared boundary twice) |
| **label map** | A planar-map file opened as a document in nib's editor: its faces are fills derived from the edges' rings, so editing a node moves the boundary of both faces it separates and a shared boundary never tears. | nib's label-map document | an SVG path document (nib's other document kind), a raster label image (the segmentation's labels) |
| **nib** | The vector node editor whose core (`@nib/core`, vendored as `web/vendor/nib-core-0.2.0.tgz`) the Nodes stage embeds, with nib's chrome, on a revision's planar-map file. Saving from it makes a new trace revision ("edited in nib"). | nib's editor, the Nodes stage | Inkscape handoff (an SVG round trip that makes an `inkscape-edit` revision) |
| **canonical boundary** | One boundary edge between two faces, stored once in the planar-map file and drawn by the two faces' rings in opposite directions. | shared boundary, canonical interface | two matching paths, independently traced edge |
| **colour layer** | The named SVG layer containing the cut geometry owned by one final palette colour. | palette layer, vinyl layer when material use is intended | region, component |
| **omitted colour** | A palette colour intentionally excluded from traced cut layers. | excluded colour, omitted layer colour | removed colour, deleted colour, background colour |
| **overlap variant** | `traced-overlap.svg`, in which lower layers may extend beneath upper layers. Some stored trace revisions hold one; a trace does not write it. | overlap SVG | layered SVG, without naming the variant |
| **edge variant** | `traced-edge.svg`, the gap-free edge-to-edge partition, exported one way from the planar-map file: one layer per included colour, one even-odd path per face. | edge-to-edge SVG, edge SVG | tiled SVG, independently traced SVG |
| **live trace view** | The Trace canvas's drawing of a running trace: t3's faces as they are emitted, painted in the edge variant's order (palette order, each colour's faces in face order) so it grows into exactly what the finished edge variant paints; cleared when the trace ends. | live trace | live preview, trace preview |
| **soft node limit** | A trace setting (`soft_path_nodes`, `soft_layer_nodes`, `soft_document_nodes`) naming a node count above which `trace.json` warns about the path, the layer or the document; it never changes the geometry. | complexity warning limit | node cap, hard limit |
| **cutter cleanup** | Optional trace-stage cleanup of a trace-local copy of the labelled partition, before t3 traces it, using physical or pixel width/area thresholds. It does not alter `flattened.png`. | trace cleanup, cut-detail cleanup | small-region cleanup, post-fit SVG editing, vector cleanup when it implies path mutation; physical-detail cleanup outside physical mode |
| **remove** | Replacing grouped undersized labelled regions with a stable touching colour. | remove undersized regions, fill into neighbour | delete, erase, gap fill |
| **widen** | Expanding a narrow labelled region into neighbouring labelled regions until the threshold is met, unless neighbour constraints make it unsafe. | thicken, widen feature | dilate, gap widen as a separate algorithm |
| **remove then widen** | First removing regions below configured area or removal-width thresholds, then widening surviving narrow regions to the target width. | combined cleanup | remove or widen, when both stages matter |
| **narrow gap** | A narrow labelled region viewed as separation between neighbouring colours. It is geometrically the same as a narrow area on that colour layer. | labelled gap, colour channel | bare gap or negative space without naming the owning colour layer; empty space unless the colour is actually omitted |
| **narrow area** | A locally thin part of a labelled colour region. | narrow feature, thin region | foreground feature |
| **minimum detail width** | The symmetric threshold used to identify and, when configured, widen narrow labelled geometry on either side of a boundary. | target width, widening threshold | minimum gap width as a separate operation |
| **minimum region area** | The area threshold used by cutter cleanup to classify undersized regions. | removal area threshold | minimum region pixels |
| **remove-below width** | In remove-then-widen mode, the separate width below which regions are removed before surviving geometry is widened. | removal width threshold | minimum detail width |
| **physical mode** | Trace configuration with output dimensions, making width and area settings use millimetres and square millimetres. | physical units mode | cutter-ready mode |
| **pixel mode** | Trace configuration without physical dimensions, making width and area settings use pixels and square pixels. | raster units mode | physical mode |
| **trace fallback** | A recorded local recovery shown by the Trace fallbacks overlay (`trace-fallbacks.png`). Some stored trace revisions hold one; a t3 trace does not write it. | local fallback | global raster trace |

## Path nodes

A **node** is an endpoint shared by two consecutive path segments; each cubic
segment gives the node a **handle** (control point) on that side. Node types are
defined by the node's two handles. The shape is the marker Vinylgen's documents
use for that type; the Nodes stage is nib's editor, which draws
its own markers. A traced node is a cusp or a symmetric node,
from t3 (`sodipodi:nodetypes` `c` or `s`): t3's smooth joins are tangent joins,
not equal-handle ones, so it writes no smooth node, and an elliptical or
circular arc counts as the cubics Inkscape converts it to.

| Canonical term | Definition | Shape | Avoid or qualify |
|---|---|---|---|
| **cusp node** | A node whose two handles are not at 180° to each other, so the path changes direction abruptly there; a node between two straight segments is always a cusp. | rhombus | sharp node; "corner" when the true edge has no corner there (that is a **kink**) |
| **symmetric node** | A node whose two handles are at 180° to each other (collinear through the node) but may differ in length (unbalanced). | square | Inkscape calls this type *smooth* (`sodipodi:nodetypes` `s`); name it by Vinylgen's term |
| **smooth node** | A node whose two handles are at 180° to each other and equal in length (balanced). | circle | Inkscape calls this type *symmetric* (`sodipodi:nodetypes` `z`); name it by Vinylgen's term |
| **turn angle** | The change of direction at a node, in degrees: 0° is straight on, 180° doubles back. | bend angle | interior angle, which is 180° minus the turn |
| **kink** | A cusp node where the true edge has no corner: an unintended sharp change of direction in what should be a continuous edge. | | corner |
| **corner** | A direction jump where two pieces of a traced edge meet transversally, drawn as a cusp node. t3 decides it from the labels; no setting steers it. | | corner threshold (a fixed minimum turn, which Vinylgen does not have) |

## Revisions, artifacts, and state

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **revision** | An immutable, parent-linked record of one workflow result and its settings. | version, project revision | file version, mutable state |
| **revision branch** | A child revision that preserves its parent and allows alternate subsequent work. | branch, edit branch | overwrite |
| **artifact** | An immutable stored file associated with a revision and identified by kind. | output artifact, generated file | revision |
| **effective artifacts** | The newest artifact of each kind found by walking a revision's ancestry. | inherited artifacts, effective artifact set | current folder contents |
| **settings provenance** | The complete effective configuration and origin recorded with a revision or audit. | configuration provenance, settings snapshot | defaults, when inherited or modified values exist |
| **stale concern** | A downstream result category invalidated by an upstream change and requiring recomputation. | stale output, invalidated stage | invalid artifact, corrupt artifact |
| **durable output** | A reviewable generated result stored under `outputs/<subject>/` or immutable web project storage. | generated result | scratch file, preview experiment |
| **scratch artifact** | A disposable experiment, render, or benchmark kept under `vinylgen/.tmp/`. | temporary artifact, scratch output | durable output |

## Validation and readiness

| Canonical term | Definition | Accepted aliases | Avoid or qualify |
|---|---|---|---|
| **mechanical validation** | The broad category of deterministic checks over measurable properties such as dimensions, exact colours, connectivity, SVG structure, topology, or bounds. | automated validation, mechanical checks | visual validation, cutter-readiness proof, structural validation as a synonym for the whole category |
| **mechanically validated** | Passing the explicitly named set of mechanical checks for an artifact or workflow result. The named scope must accompany the claim. | automated checks passed | structurally valid, cutter-ready, visually approved |
| **structural validation** | The defined subset of mechanical validation concerned with SVG/XML structure, layer/path properties, supported effects, and applicable topology checks. | SVG structural checks | all mechanical validation, visual inspection |
| **structurally valid** | Passing the explicitly named structural-validation suite; it makes no claim about unrelated raster checks, appearance, or physical use. | SVG structure passed | mechanically validated without scope, cutter-ready, visually approved |
| **visual inspection** | Human judgment of composition, subject correctness, appearance, or whether automated geometry looks acceptable. | visual review | validation, when no mechanical assertion is involved |
| **physical cutter readiness** | Evidence that reviewed geometry, sizing, material assumptions, device setup, and physical testing make an output suitable to send to a cutter. | cutter-ready | structurally valid, validated |
| **cutter preparation** | The safety-gated workflow for sizing, layer selection, bounds review, presets, dry run, and eventual explicit hardware confirmation. | cut preparation | tracing, export |
| **dry run** | A non-hardware-moving preparation result that exposes paths, bounds, settings, and logs for review. | cutter preview, preparation preview | cut, send |
| **armed** | The temporary safety state reached only after cutter-host detection and required review, before a separate explicit send confirmation. | ready to confirm | running, cutter-ready |

## Terms intentionally kept distinct

- **Generative mask** selects where probabilistic source pixels may change;
  **preservation selection** constrains deterministic palette inference.
- **Palette merge** combines complete colours; it is not the per-pixel **pencil**,
  connected-area reassignment, freehand recolouring, blending, manual colour
  adjustment, or generative editing.
- **Small-region cleanup** operates on raster pixels during palette processing;
  **cutter cleanup** operates during tracing with width/area thresholds.
- **Initial colour candidates** are not the **final colour count**.
- **Trace fidelity** changes geometric approximation; **cut detail** changes feature cleanup.
- A **narrow gap** is a **narrow area** on another colour layer in the complete
  labelled partition. Widening is one symmetric operation, not two algorithms.
- **Structural validation** is a subset of **mechanical validation**; neither
  means **visually approved** or **physically cutter-ready**.
