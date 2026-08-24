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

Artwork canvases fill available space, use a subtle checker/grid ground, and isolate detailed comparison controls in the explicit inspector. Stage settings use generous vertical grouping and expose the complete set for the current stage without mixing in controls owned by another stage.

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

### Artwork Inspector

The image remains passive until the user opens inspection. A 44px circular Search-icon trigger floats 14px from the artwork’s top-right corner. The full-screen inspector owns pan, zoom, single/split/blink comparison, overlays, and responsive bottom-sheet controls; it restores focus when dismissed.

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
