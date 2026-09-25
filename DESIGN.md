---
name: Vinylgen
description: A quiet cutter workshop that keeps the artwork, workflow state, and physical-output decisions clear.
colors:
  workshop-black: "#0c0e0d"
  panel-black: "#121512"
  raised-charcoal: "#181c18"
  canvas-charcoal: "#1b1f1b"
  control-charcoal: "#202520"
  control-strong: "#2c332c"
  hairline: "#292f29"
  control-border: "#3a433a"
  text: "#f0eee7"
  text-strong: "#faf8f1"
  text-muted: "#9da59c"
  text-label: "#bec5bb"
  action-blue: "#56b9ff"
  action-blue-hover: "#7bc9ff"
  action-blue-quiet: "#102a3d"
  action-ink: "#06131c"
  warning: "#ffc06b"
  error: "#ff8e7d"
  success: "#72d69b"
  handoff: "#f08a5d"
typography:
  headline:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.05
    letterSpacing: "-0.025em"
  title:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
    letterSpacing: "normal"
  label:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "11px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "0.025em"
rounded:
  sm: "6px"
  md: "12px"
  round: "50%"
spacing:
  xs: "4px"
  sm: "7px"
  md: "12px"
  lg: "18px"
  xl: "24px"
  panel: "28px"
components:
  button-primary:
    backgroundColor: "{colors.action-blue}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
    height: "44px"
  button-primary-hover:
    backgroundColor: "{colors.action-blue-hover}"
    textColor: "{colors.action-ink}"
    rounded: "{rounded.sm}"
  button-secondary:
    backgroundColor: "{colors.control-strong}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "10px 12px"
    height: "44px"
  field:
    backgroundColor: "{colors.control-charcoal}"
    textColor: "{colors.text}"
    rounded: "{rounded.sm}"
    padding: "11px 12px"
    height: "44px"
  stage-selected:
    backgroundColor: "{colors.action-blue-quiet}"
    textColor: "{colors.action-blue}"
    rounded: "{rounded.sm}"
---

# Design System: Vinylgen

## Overview

**Creative North Star: "The Quiet Cutter Workshop"**

Vinylgen is an operate-mode technical workspace: subdued, deliberate, and candid. Near-black green-charcoal surfaces recede so source artwork and traced output remain the brightest, most visually complex objects in the application. The interface communicates through compact hierarchy, precise spacing, and clear state changes instead of decoration.

Bright blue is a scarce operational signal. It identifies the selected workflow stage, primary actions, focus, and active selections; it is not ambient branding. Settings are stage-specific and spacious enough to explain their physical consequences in plain language. The system avoids pretending that structural validation equals cutter readiness.

**Key Characteristics:**

- Dark tonal surfaces keep artwork dominant.
- A persistent, explicit stage sequence anchors the workflow.
- Bright blue is reserved for selection, focus, and primary action.
- Compact technical typography remains readable and touch-safe.
- Copy is direct about revisions, uncertainty, and physical-output limits.

## Colors

The palette is a low-chroma workshop of green-black neutrals, warm off-white text, and one cool, high-clarity action accent.

### Primary

- **Action Blue:** Marks the current stage, primary actions, focus rings, progress, and active controls.
- **Quiet Action Blue:** Provides selected-state fill without turning the rail or controls into bright blocks.

### Secondary

- **Cutter Handoff Orange:** Reserved for the explicit Inkscape handoff path; it distinguishes an external-tool action from the ordinary blue workflow.

### Neutral

- **Workshop Black:** The application root and deepest visual ground.
- **Panel Black:** Headers, stage rail, and settings panels.
- **Raised Charcoal:** Sheets and locally raised containers.
- **Canvas Charcoal:** Artwork workspaces and inspection fields.
- **Control Charcoal:** Inputs, checks, and quiet interactive surfaces.
- **Warm Technical White:** Default and strong text; warmer than the action blue and softer than pure white.
- **Muted Sage Gray:** Help, metadata, inactive stages, and secondary status.
- **Hairline:** One-pixel structural separation between large regions.

**The One Signal Rule.** Use Action Blue only when an element is actionable, focused, selected, or actively progressing; the artwork owns every other bright colour.

**The Honest Status Rule.** Warning, error, success, and handoff colours communicate distinct outcomes and never substitute for the primary action accent.

## Typography

**Display Font:** System UI sans-serif
**Body Font:** System UI sans-serif
**Label/Mono Font:** System UI sans-serif; native monospace is limited to coordinate and machine-value readouts.

**Character:** Compact, neutral, and tool-like. Hierarchy comes from weight, size, and restrained tracking rather than a decorative display face.

### Hierarchy

- **Headline** (700, 20px, 1.05): Product title, loading states, and major handoff headings.
- **Title** (700, 15px, 1.2): Stage-panel and settings-group headings.
- **Body** (400, 13px, 1.55): Guidance, empty states, and candid explanations; keep explanatory lines near 34–72 characters where layout allows.
- **Label** (650, 11px, 0.025em): Field names and technical navigation metadata. Uppercase is reserved for compact viewport-state labels with wider tracking.

**The Compact, Never Cryptic Rule.** Small labels may be dense, but consequential settings always pair their short name with plain-language help.

## Layout

Desktop uses a persistent 214px stage rail below the 76px header. Its steps are 64px high and combine a 20px Lucide line icon, a strong stage name, and a short description. The remaining workspace divides into an artwork-first flexible canvas and a stage-specific settings panel between 390px and 440px wide; settings sections use 24–28px padding and 14px form rhythm.

At 700px and below, the rail becomes a horizontal, sticky, overflowable step strip directly below the 58px header. Each step is 112px wide and 58px high, retaining its icon and title while hiding secondary descriptions. Stage content becomes a single-column surface and controls retain at least a 44px touch target. The 701px breakpoint is the ownership boundary between desktop rail and phone strip; intermediate desktop widths tighten panels without changing the workflow model.

Artwork canvases fill available space, use a subtle checker/grid ground, and keep comparison to one compact Compare control in the canvas toolbar. Stage settings use generous vertical grouping and expose the complete set for the current stage without mixing in controls owned by another stage.

**The Artwork-First Rule.** Canvas space flexes; workflow navigation and the currently relevant settings remain predictable.

## Elevation & Depth

The system is flat and tonally layered by default. Large structural surfaces are separated with one-pixel borders or close charcoal steps, not card shadows. Floating depth is ambient and reserved for overlays: the circular inspector trigger uses the canonical floating shadow, while modal sheets combine a dark backdrop with motion. Sticky history and mobile navigation may use low directional shadows only to clarify overlap during scrolling.

### Shadow Vocabulary

- **Ambient Float** (`0 18px 48px rgba(0, 0, 0, .28)`): Floating overlay triggers and equivalent detached overlay controls only.
- **Sticky Separation** (`0 10px 30px rgba(0, 0, 0, .16)`): Content that overlaps the canvas while pinned during scroll.
- **Canvas Inset** (`inset 0 0 80px rgba(0, 0, 0, .18)`): Quietly contains the image workspace without lifting it.

**The Flat Workshop Rule.** Do not add ambient shadows to ordinary panels, cards, fields, or stage steps.

## Shapes

Controls and compact containers use gently machined 6px corners. Larger handoff panels and grouped containers use 12px corners. Full circles are reserved for inherently radial affordances such as the inspector trigger, spinner, split handle, and status dots. Structural regions meeting the viewport edge remain square. Borders are restrained, one pixel, and tonal.

**The Two-Radius Rule.** Use 6px for controls and 12px for larger grouped surfaces; introduce another radius only when the geometry itself requires a circle or an edge-attached sheet.

## Components

### Buttons

- **Shape:** Compact machined corners (6px) with a 44px minimum target for primary workflow controls.
- **Primary:** Action Blue fill, dark Action Ink, strong weight, and 10px × 12px base padding.
- **Hover / Focus:** Hover lightens to Action Blue Hover; active moves down 1px; keyboard focus uses a 3px Action Blue outline with 3px offset. Reduced-motion preference removes transition duration.
- **Secondary:** Strong Control Charcoal with warm white text; hover lifts the tonal value without adopting the primary accent.

### Cards / Containers

- **Corner Style:** Mostly square structural panels; grouped controls use 6px and larger handoff containers use 12px.
- **Background:** Adjacent charcoal tones separate root, panel, raised, canvas, and control roles.
- **Shadow Strategy:** Flat by default; see Elevation & Depth.
- **Border:** One-pixel Hairline or Control Border where separation is necessary.
- **Internal Padding:** 24–28px for stage panels; 12–14px for grouped settings.

### Inputs / Fields

- **Style:** Control Charcoal fill, one-pixel Control Border, 6px corners, warm white value text, and compact labels above.
- **Focus:** Border changes to Action Blue and the field background lightens slightly; keyboard focus also receives the shared outline.
- **Error / Disabled:** Errors use a dark warm-red field and explicit message. Disabled controls reduce opacity but retain readable shape and label.

### Navigation

The workflow navigator is persistent and sequential. Desktop steps pair Lucide line icons with title and help inside 64px rows; the selected stage uses Quiet Action Blue, Action Blue text, and a 3px leading marker. Phone steps become 112px × 58px horizontal items with the same selection language and native scrolling. Running status is a small Warning dot, not a competing accent fill.

### Browser History

Switching stage and choosing a project in the header each add a browser history entry. Back and Forward over the app's own entries move between stages without changing the revision; onto another project they return to the revision last shown there in this session, else the one the entry names, else the newest. The revision changes only through Undo, Redo and the Revision picker (offered whenever the project has revisions), edits and jobs. A link pasted into, or opened in, the page shows the revision it names.

### Artwork Inspector

The stage canvases (Upload, Flatten, Palette, Cleanup, Edit and Trace) are interactive: they own pan, zoom, their stage's overlays and editing tools, and the split/blink comparison described below. Only the passive previews on the remaining stages (Create, Export) open the full-screen inspector, through a 44px circular Search-icon trigger floating 14px from the artwork’s top-right corner. The inspector owns pan, zoom, single/split/blink comparison with free Before/After pickers, overlays, and responsive bottom-sheet controls; it restores focus when dismissed.

### Trace Nodes Overlay

The Trace canvas offers a "Nodes" switch beside its other inspection overlays. On, it marks every path node of the traced SVG it shows with its type's shape (GLOSSARY.md "Path nodes"): a cusp node is a rhombus, a symmetric node a square, a smooth node a circle. The types are read from the SVG's own `sodipodi:nodetypes`, so the canvas and Inkscape agree. Markers are warm white with a dark outline, keep one on-screen size at every zoom, and follow pan and zoom; handles are not drawn. A node shared by several paths (a boundary both of its colours draw) is marked once, and where the paths disagree the sharper type wins (cusp, then symmetric, then smooth) so no kink is hidden. Beneath the switch, a legend shows the three shapes with their terms and the committed trace's count of each; it also reports loading, a load failure, and a trace recorded before node types existed ("Re-run Trace to record them"). Where the live preview is drawn, the nodes inside its window are the preview's own and the committed trace's are not drawn there; if the preview's SVG cannot be read, the committed trace's nodes stay drawn in its window and the legend says "Couldn't read the preview's nodes". Only the nodes in view are drawn, on a viewport-sized screen-space canvas redrawn once per animation frame, so tens of thousands of nodes stay responsive. Like every overlay the nodes are hidden while comparing, with the switch kept on.

### Artwork Views

The header's view switch (the mobile header carries the same switch) lists the artwork's views in stage order, each only when it exists: Source; Flattened, the raster Cleanup starts from (with any edits made before a cleanup or while it is stale); Cleaned, the current cleanup's result before any touch-up, in the current colours (the server keeps it as `cleaned-flattened` once a Cleanup run's replayed touch-ups change the raster, or a recolour is made while touch-ups are shown; otherwise it is the raster before the first touch-up); Edited, the Cleaned image with the touch-ups made since it, offered only when there are any; Traced. Palette and Edit act on the last raster of these: Edited once touch-ups exist, else Cleaned while a cleanup is current, else Flattened. Entering Palette or Edit shows that view, and the first touch-up switches it from Cleaned to Edited; on any other view their tools are disabled and point back to it ("Switch to the Edited image…").

### Canvas Comparison

Each stage canvas carries a Compare control (Off / Split / Blink, Off by default) in its toolbar, shown only where the canvas has a second image covering the same extent as the shown one. That second image must be on the same grid or an integer upscale of it. The one exception is the source an upscaled source was made from: it must match the upscale's recorded input grid (`parent_source_size`) exactly, and the shown image must be on the recorded output grid (`source_size`) or a whole-number multiple of it, such as a flattened raster at an Output upscale above 1. On revisions recorded before those sizes, the two images' own pixel sizes stand in; the revision's kind and parent already prove the lineage. The upscaler rounds each side of its output to a multiple of 8, so this pair registers by lineage, never by a pixel tolerance. The Before is stretched over the shown image, and each compared layer is drawn crisp (pixelated) whenever its own on-screen magnification is at least 1.

The shown image is the After. The Before is the stage's natural input:

- Upload, which always shows the source: the source an upscaled source was made from.
- Flatten, on any result view: the source.
- Cleanup, on the Cleaned view: the raster before cleanup.
- Trace, on the Traced view: the raster traced.
- Palette and Edit: a Before picker over every image, with the view before the one they edit first: while they edit the Edited image, the Cleaned image; while they edit the Cleaned image, the Flattened image (the raster before cleanup), so Cleanup's changes never read as edits; otherwise the flattened original from before any edits (else the source).

As in the inspector, split draws the After left of the divider and the Before right of it; the divider is dragged by pointer or moved with the arrow, Page and Home/End keys. Blink alternates the two, with pause and manual stepping, and starts paused under reduced motion. Both stay registered with the canvas's zoom and pan.

While comparing, the canvas draws nothing over the pair except the preservation markers, and nothing edits:

- The preservation markers stay drawn: they are the user's own points and register on both images.
- Inspection overlays (Cleanup's Show changes and Trace's Nodes included) and the Palette recolour preview are hidden.
- The live preview is paused as when its grid is unknown: no requests, the one in flight aborted, no badge or live legend line.
- Area selection, the pencil and preservation picking are off on the canvas.
- Every edit-committing control in the Palette and Edit side panels (swatches, Apply, merge, rename, Undo) is locked. It stays focusable, is marked `aria-disabled`, does nothing when activated, and is described (`aria-describedby`) by the status "Turn Compare off to edit".

The control says so too ("Overlays and live preview hidden while comparing", "Turn Compare off to edit"). Every switch keeps its state for when Compare is Off.

The chosen mode is remembered across stages and reloads (`vinylgen.canvasCompare`) for Upload, Flatten, Cleanup and Trace. Palette and Edit always open with Compare Off, since comparing there locks editing, and a mode chosen there is not saved. The pre-upscale source is loaded only for the canvases that compare with it (Upload, and the Palette/Edit picker). A failed load is reported in the Compare control with a Retry.

Above 1024px the control sits inline in the toolbar, which wraps when it needs to. At 1024px and below it collapses into one 44px toolbar button that opens the control as a popover. The button's visible text is "Compare: Off/Split/Blink", dropping to the mode word in a narrow canvas column; its accessible name stays in full. The popover is positioned from the canvas rather than inside the toolbar row, so it may overlap the settings beside a narrow column, and it scrolls instead of squeezing its contents. It holds the mode choice, the Before picker, the blink controls, the notes and any load error with its Retry, all with 44px targets. While it is open, a transparent backdrop covers the page beneath it. A press anywhere else lands wholly on that backdrop and closes the popover on its click, reaching nothing underneath; a touch drag there closes nothing and scrolls nothing. It also closes on Escape, which returns focus to the button, and when focus moves past both the button and the popover. The toolbar row stays one line and clips to the canvas column: at these widths the canvas label is hidden, and in a column under 240px the image name is hidden too. The canvas's height is unchanged by the control. Above 700px the artwork column fills the space beside the stage rail and settings (`.artwork-surface > .image-workspace { height: 100% }`), with the viewport row floored at 360px up to 1024px. At 700px and below the viewport row is `minmax(300px, calc(100dvh - 217px))` inside a workspace of at least 48dvh, and the page scrolls rather than the canvas collapsing.

### Stage Settings

Each stage exposes only controls relevant to that operation. Settings groups use clear headings, a one-sentence consequence, 11–14px internal rhythm, and help directly beneath fields. The owning stage keeps its full option set visible rather than hiding it in collapsed disclosure, and preservation tools explicitly describe point selection as precise exceptions rather than painting.

## Do's and Don'ts

### Do:

- **Do** keep the artwork visually dominant against near-black tonal surfaces.
- **Do** reserve Action Blue for the current stage, primary action, focus, selection, and active progress.
- **Do** preserve 44px targets and plain-language help for consequential controls.
- **Do** use Lucide line icons with text labels for workflow stages and icon-only actions with accessible names.
- **Do** state when an operation creates a revision and when validation does not establish cutter readiness.

### Don't:

- **Don't** decorate ordinary panels with floating shadows or bright accent fills.
- **Don't** use artwork colours as interface accents; status colours have fixed semantic jobs.
- **Don't** hide the stage sequence or move stage-specific settings into an undifferentiated control wall.
- **Don't** imply destructive overwrites, arbitrary colour mixing, or automatic physical-cutter safety the product does not provide; the per-pixel pencil repaints to existing palette colours as recoverable revisions, not an unbounded raster editor.
- **Don't** shrink touch controls or explanatory copy to preserve desktop density on a phone.
