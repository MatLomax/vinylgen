# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Vinylgen is primarily a personal, self-hosted tool for its owner. It supports a low-effort creative workflow used from desktop or phone, with desktop Inkscape available for advanced vector editing and a cutter host used only through explicit safety gates.

## Product Purpose

Vinylgen turns a short sci-fi or fantasy prompt, or an uploaded raster image, into editable limited-colour artwork and practical Inkscape-friendly SVG vinyl layers. Success means the user can generate or import an image, inspect and deterministically flatten it, clean up palette labels, trace and compare the result, validate it, and export recoverable artifacts without pretending that automated output is physically cutter-ready.

## Positioning

Vinylgen combines generative source creation with a deterministic, auditable raster-to-layer pipeline. Palette membership, segmentation, cleanup, SVG structure, and artifact provenance are mechanically verifiable, while composition quality and physical cutter suitability remain explicit human judgments.

## Operating Context

The core loop is `Generate → inspect → flatten → inspect/edit → trace → inspect/export`. Users may start with a short prompt or upload, work through immutable revisions in a responsive web interface, merge complete palette colours, compare raster and SVG layers in an explicitly opened inspector, and hand advanced path work to Inkscape. Durable generated artifacts live under subject-specific output or project revision storage. Cutter preparation is a separate, safety-gated workflow.

## Capabilities and Constraints

- Source generation uses a fresh ephemeral locally authenticated Codex session for each request; it must not copy OAuth credentials, silently reuse prior images, or fall back to hidden API billing.
- Deterministic flattening infers a limited palette, preserves exact palette membership, and treats four-way edge connectivity as authoritative for physical vinyl regions.
- Palette edits operate on complete colours in authoritative label maps. The flattened-image editor supports whole-colour merges, point-selected connected-area reassignment, and a per-pixel pencil that repaints individual pixels to an existing palette colour; it does not create new colours or adjust a palette colour's RGB value. Four-way connectivity stays authoritative, so a pencil stroke that severs a region splits it into separate regions on the next recompute.
- Generative source creation always creates a recoverable root revision.
- Tracing emits shared-boundary overlap and edge SVG variants, audits, and a synchronized 768 DPI preview. Structural validation does not establish physical cutter readiness.
- Source generation always requests maximum provider quality and rich detail within a configurable limited-colour flat graphic style designed for clean flattening; cut detail and trace fidelity remain separate deterministic controls.
- External SVG edits return as new revisions rather than overwriting generated traces.
- Physical cutter operation requires cutter-host detection, reviewed path bounds, arming, and a separate explicit confirmation. Tiling, registration, visual inspection, and physical cutter testing remain later or human-reviewed work.

## Brand Commitments

The product name is Vinylgen. Product language should be practical, direct, and candid about the boundary between deterministic guarantees, generative uncertainty, and physical cutter readiness.

## Evidence on Hand

- The implemented FastAPI and React/TypeScript application covers project revisions, background jobs, dismissible raster/SVG inspection, deterministic flattening and tracing, whole-colour palette merging, source generation, validation/export, and Inkscape handoff.
- The nine-image corpus and regression suite provide deterministic pipeline evidence.
- Generated trace audits and previews provide structural evidence, but there is no claimed physical cutter-readiness evidence yet.
- Product constraints and workflow details are documented in `docs/product-brief.md` and the project architecture and validation specifications.

## Product Principles

- Keep the common path low effort while preserving advanced control when it changes the physical result.
- Separate deterministic guarantees from generative and visual judgments.
- Preserve every meaningful input and edit through immutable, recoverable revisions.
- Make exact colours, geometry, provenance, staleness, and safety state inspectable.
- Require deliberate human review before external tools or physical hardware can cause irreversible outcomes.

## Accessibility & Inclusion

The web workflow must remain usable on desktop and phone, with touch-safe controls, registered pan and zoom behavior, readable status and error feedback, and no capability claim that depends on a desktop-only action when used remotely.
