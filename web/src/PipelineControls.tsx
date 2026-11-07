import { useState } from 'react'
import type { CleanupSettings, CurveKind, PaletteColour, PaletteSettings, TraceSettings, TracerInfo } from './api'
import type { CanvasMarker } from './ImageWorkspace'

export const DEFAULT_PALETTE_SETTINGS: PaletteSettings = {
  initial_colours: 16, final_colours: null, histogram_bits: 5, analysis_max_dimension: 640, dither_radius: 1, similar_delta_e: 28, small_share: .004, small_delta_e: 36, fold_small_colours: true,
  fragmented_share: .035, fragmented_largest_region: .30, fragmented_delta_e: 24, merge_fragmented_colours: true, min_output_colours: 2, min_region_pixels: 24, min_feature_width: 0, min_colour_share: .01,
  intentional_line_min_contrast: 12, intentional_line_min_length: 8, protect_intentional_lines: true, aa_ambiguity_margin: 7, aa_smoothness: 2.5, aa_resolution_passes: 5, resolve_antialiasing: true, flatten_upscale: 1, flatten_smooth_edges: true,
  protected_component_points: [], preserved_colour_points: [], distinct_colour_point_pairs: [],
}
export const DEFAULT_CLEANUP_SETTINGS: CleanupSettings = {
  width_mm: null, height_mm: null, remove_colour: null,
  cleanup: false, cleanup_action: 'remove', min_detail_width: 0, min_region_area: 0, remove_below_width: 0,
}
// The trace's settings (vinylgen.contracts.TraceSettings): its three soft node limits, each
// only warning, and each curve kind's tube half-width in px, the furthest a drawn piece of
// that kind may lie from its edge. Arcs, ellipses and cubics can be off (null); the line,
// the only kind that can draw an edge of one crack, cannot.
export const HALF_WIDTH_DEFAULT = 1.75
export const DEFAULT_TRACE_SETTINGS: TraceSettings = {
  soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000,
  half_line: HALF_WIDTH_DEFAULT, half_arc: HALF_WIDTH_DEFAULT, half_ellipse: HALF_WIDTH_DEFAULT, half_cubic: HALF_WIDTH_DEFAULT,
}
const TRACE_KEYS = Object.keys(DEFAULT_TRACE_SETTINGS) as (keyof TraceSettings)[]
// The kinds that may be off, whose stored null is kept.
const OPTIONAL_KINDS = new Set<keyof TraceSettings>(['half_arc', 'half_ellipse', 'half_cubic'])
// The Curve kinds rows, in order: each kind's field, its name, the drawn piece it names
// and whether it can be turned off.
type HalfKey = 'half_line' | 'half_arc' | 'half_ellipse' | 'half_cubic'
const CURVE_KINDS: { key: HalfKey; kind: CurveKind; label: string; piece: string; optional: boolean }[] = [
  { key: 'half_line', kind: 'line', label: 'Lines', piece: 'line', optional: false },
  { key: 'half_arc', kind: 'arc', label: 'Circular arcs', piece: 'circular arc', optional: true },
  { key: 'half_ellipse', kind: 'ellipse', label: 'Elliptical arcs', piece: 'elliptical arc', optional: true },
  { key: 'half_cubic', kind: 'cubic', label: 'Cubic curves', piece: 'cubic curve', optional: true },
]
const HALF_WIDTH_HELP = 'Higher lets this kind cover longer stretches, looser; lower holds it tighter to the pixels.'
// t3api's default setting (t3api.SETTING, canonical), shown when the server does not report it.
export const T3_SETTING: TracerInfo['setting'] = { half: { line: HALF_WIDTH_DEFAULT, arc: HALF_WIDTH_DEFAULT, ellipse: HALF_WIDTH_DEFAULT, cubic: HALF_WIDTH_DEFAULT }, A1: 22.5, A2: 45, L: 4 }
const CLEANUP_KEYS = Object.keys(DEFAULT_CLEANUP_SETTINGS) as (keyof CleanupSettings)[]
// Trace settings recorded before the cleanup stage existed embed the physical size, the
// removed colour and the cutter cleanup. Those values belong to the Cleanup form; this
// picks them out of a stored trace record, or returns undefined when it holds none.
export function legacyCleanupSettings(stored: Record<string, unknown>): Partial<CleanupSettings> | undefined {
  const picked = Object.fromEntries(CLEANUP_KEYS.filter(key => key in stored).map(key => [key, stored[key]]))
  return Object.keys(picked).length ? picked as Partial<CleanupSettings> : undefined
}
// A stored trace record or browser draft as the trace form loads it: the soft node limits
// and half-widths it holds, and an optional kind's null (off), and nothing else; a record
// from before the half-widths lacks them and loads them at their defaults, and a null line
// is dropped, so it loads at its default too. Every other field an older record carries (the old fitter's
// lengths and their unit, its corner, node and snap controls, overlap, solid underlayers,
// layer order, the renamed `corner_straightness`, `corner_threshold`, the pixel-era
// `curve_optimization` and the cleanup fields a trace once embedded, which
// legacyCleanupSettings hands to the Cleanup form) is dropped, so it is never sent.
export function normalizeTraceSettings(stored: Record<string, unknown> | Partial<TraceSettings>): Partial<TraceSettings> {
  const record = stored as Record<string, unknown>
  return Object.fromEntries(TRACE_KEYS.filter(key => typeof record[key] === 'number' || (record[key] === null && OPTIONAL_KINDS.has(key))).map(key => [key, record[key]])) as Partial<TraceSettings>
}
// The physical size a trace uses, from its cleanup: both sides in mm, with a one-sided
// size completed from the raster's proportions once those are known.
export type PhysicalSize = { width: number | null; height: number | null }
export function physicalSize(cleanup: Partial<CleanupSettings> | null | undefined, raster: { width: number; height: number } | null): PhysicalSize | null {
  const width = cleanup?.width_mm ?? null, height = cleanup?.height_mm ?? null
  if (width === null && height === null) return null
  if (width !== null && height === null) return { width, height: raster ? width * raster.height / raster.width : null }
  if (height !== null && width === null) return { width: raster ? height * raster.width / raster.height : null, height }
  return { width, height }
}
export function formatPhysicalSize(size: PhysicalSize | null): string {
  if (!size) return 'No physical size (px)'
  const mm = (value: number) => String(Number(value.toFixed(2)))
  if (size.width !== null && size.height !== null) return `Size: ${mm(size.width)} × ${mm(size.height)} mm (set in Cleanup)`
  return size.width !== null ? `Size: ${mm(size.width)} mm wide (set in Cleanup)` : `Size: ${mm(size.height!)} mm high (set in Cleanup)`
}
export type SelectionMode = 'protect-component' | 'preserve-colour' | 'distinct-first' | 'distinct-second' | null

// A field is dirty when its edited value diverges from the baseline that produced
// the shown image (the recorded revision settings, or defaults before the step ran).
function sameSetting(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}
function DirtyDot({ dirty }: { dirty?: boolean }) {
  return dirty ? <i className="field-dirty" role="img" aria-label="Pending change, not yet applied" title="Pending change — not yet applied" /> : null
}

export function Field({ label, help, dirty, disabled, children }: { label: string; help: string; dirty?: boolean; disabled?: boolean; children: React.ReactNode }) {
  return <label className={`setting-field${disabled ? ' is-disabled' : ''}`}><span>{label}<DirtyDot dirty={dirty} /></span>{children}<small>{help}</small></label>
}
export function NumberField({ label, help, value, onChange, min = 0, max, step = 'any', optional = false, dirty, disabled }: {
  label: string; help: string; value: number | null; onChange: (value: number | null) => void; min?: number; max?: number; step?: number | 'any'; optional?: boolean; dirty?: boolean; disabled?: boolean
}) {
  // While the field has focus, show the raw text so an empty or partial entry (every digit
  // backspaced, a trailing dot) survives instead of snapping back to the last committed number.
  // Unfocused, the field mirrors the committed value, so external changes (stepping iterations,
  // reset) and a required field cleared then left empty both resolve back to the real value.
  const [text, setText] = useState('')
  const [focused, setFocused] = useState(false)
  return <Field label={label} help={help} dirty={dirty} disabled={disabled}><input type="number" min={min} max={max} step={step} value={focused ? text : (value ?? '')} disabled={disabled} placeholder={optional ? 'Automatic' : undefined} onFocus={() => { setFocused(true); setText(value === null ? '' : String(value)) }} onChange={event => { const raw = event.target.value; setText(raw); if (raw === '') { if (optional) onChange(null); return } const parsed = Number(raw); if (!Number.isNaN(parsed)) onChange(parsed) }} onBlur={() => setFocused(false)} /></Field>
}
// A continuous-range counterpart to NumberField, for settings better dragged than typed.
export function Slider({ label, help, value, onChange, min, max, step, format, dirty, disabled }: {
  label: string; help: string; value: number; onChange: (value: number) => void; min: number; max: number; step: number; format?: (value: number) => string; dirty?: boolean; disabled?: boolean
}) {
  return <Field label={label} help={help} dirty={dirty} disabled={disabled}>
    <div className="slider-control">
      <input type="range" min={min} max={max} step={step} value={value} disabled={disabled} onChange={event => onChange(Number(event.target.value))} />
      <output>{format ? format(value) : value}</output>
    </div>
  </Field>
}
// A feature sub-group whose master checkbox skips its whole stage when off, greying
// out the dependent fields.
function ToggleGroup({ label, help, on, dirty, onToggle, children }: {
  label: string; help: string; on: boolean; dirty?: boolean; onToggle: (on: boolean) => void; children: React.ReactNode
}) {
  return <div className="detail-group">
    <label className="check-field"><input type="checkbox" checked={on} onChange={event => onToggle(event.target.checked)} /><span><strong>{label}<DirtyDot dirty={dirty} /></strong><small>{help}</small></span></label>
    <div className="settings-grid">{children}</div>
  </div>
}
// The canvas marker of each chosen preservation point, numbered by its row in the
// side-panel list; a pair's first pick (`pendingFirst`) shows as a pending pair marker
// numbered as the row it will become.
export const preservationMarkerId = (kind: CanvasMarker['kind'], index: number) => `${kind}-${index}`
export const PENDING_PAIR_MARKER_ID = 'pair-pending'
export function preservationMarkers(palette: Pick<PaletteSettings, 'protected_component_points' | 'preserved_colour_points' | 'distinct_colour_point_pairs'>, pendingFirst: [number, number] | null): CanvasMarker[] {
  return [
    ...palette.protected_component_points.map((point, index): CanvasMarker => ({ id: preservationMarkerId('protect', index), kind: 'protect', label: String(index + 1), points: [point] })),
    ...palette.preserved_colour_points.map((point, index): CanvasMarker => ({ id: preservationMarkerId('preserve', index), kind: 'preserve', label: String(index + 1), points: [point] })),
    ...palette.distinct_colour_point_pairs.map((pair, index): CanvasMarker => ({ id: preservationMarkerId('pair', index), kind: 'pair', label: String(index + 1), points: [pair[0], pair[1]] })),
    ...(pendingFirst ? [{ id: PENDING_PAIR_MARKER_ID, kind: 'pair' as const, label: String(palette.distinct_colour_point_pairs.length + 1), points: [pendingFirst], pending: true }] : []),
  ]
}
// A chosen-point row; hovering or focusing it highlights its canvas marker.
function PointRow({ id, number, onHighlight, onRemove, children }: { id: string; number: number; onHighlight: (id: string | null) => void; onRemove: () => void; children: React.ReactNode }) {
  return <span className="point-row" onMouseEnter={() => onHighlight(id)} onMouseLeave={() => onHighlight(null)} onFocus={() => onHighlight(id)} onBlur={() => onHighlight(null)}>
    <b className="point-number">{number}</b><code>{children}</code>
    <button type="button" className="secondary" onClick={() => { onHighlight(null); onRemove() }}>Remove</button>
  </span>
}
function PointList({ title, kind, points, onRemove, onHighlight }: { title: string; kind: CanvasMarker['kind']; points: [number, number][]; onRemove: (index: number) => void; onHighlight: (id: string | null) => void }) {
  return points.length ? <div className="selection-list"><strong>{title}</strong>{points.map((point, index) => <PointRow key={`${point}-${index}`} id={preservationMarkerId(kind, index)} number={index + 1} onHighlight={onHighlight} onRemove={() => onRemove(index)}>{point.join(', ')}</PointRow>)}</div> : null
}

// The palette colour a removed colour names, resolved as the
// tracer's resolve_colour does: the name, the hex or the hex without '#', ignoring case,
// naming exactly one colour. Anything else names none.
// Python's str.casefold, closely enough for colour names: upper- then lower-casing also
// folds characters such as "ß" to "ss", which toLowerCase alone keeps.
const casefold = (text: string) => text.toUpperCase().toLowerCase()
// Every colour of `palette` the entry names, exactly as the server's resolve_colour
// (vinylgen/tracing.py) matches: the trimmed, case-folded entry against each colour's
// name, its hex, and its hex without "#". It resolves only when this is one colour.
export function paletteColourMatches(entry: string, palette: PaletteColour[]): PaletteColour[] {
  const folded = casefold(entry.trim())
  return palette.filter(colour => [casefold(String(colour.name)), casefold(colour.hex), casefold(colour.hex.slice(1))].includes(folded))
}
export function resolvePaletteColour(entry: string, palette: PaletteColour[]): PaletteColour | undefined {
  const matches = paletteColourMatches(entry, palette)
  return matches.length === 1 ? matches[0] : undefined
}

// What the Cleanup form's removed colour resolves to in `palette`, as a Cleanup run
// resolves it (paletteColourMatches): 'none' (nothing removed, or no palette to judge
// against), 'match' with the one colour, 'missing' or 'ambiguous'.
export function removedColourChoice(value: string | null, palette: PaletteColour[]): { status: 'none' | 'match' | 'missing' | 'ambiguous'; colour?: PaletteColour } {
  if (!value || !palette.length) return { status: 'none' }
  const matches = paletteColourMatches(value, palette)
  return matches.length === 1 ? { status: 'match', colour: matches[0] } : { status: matches.length ? 'ambiguous' : 'missing' }
}

export function PipelineControls({ stage, palette, setPalette, basePalette, trace, setTrace, baseTrace, cleanup = DEFAULT_CLEANUP_SETTINGS, setCleanup = () => undefined, baseCleanup = DEFAULT_CLEANUP_SETTINGS, paletteColours, selectionMode, setSelectionMode, onReset, iterationCount, iterationIndex, onStep, stepDisabled = false, onCommitDraft, tracedSize = null, traceSize = null, tracer = null, removalPaletteCurrent = true, removalBlocked = null, onHighlightPoint = () => undefined }: {
  stage: 'flatten' | 'cleanup' | 'trace'; palette: PaletteSettings; setPalette: (value: PaletteSettings) => void; basePalette: PaletteSettings; trace: TraceSettings; setTrace: (value: TraceSettings) => void; baseTrace: TraceSettings
  cleanup?: CleanupSettings; setCleanup?: (value: CleanupSettings) => void; baseCleanup?: CleanupSettings
  paletteColours: PaletteColour[]; selectionMode: SelectionMode; setSelectionMode: (value: SelectionMode) => void; onReset: () => void; iterationCount: number; iterationIndex: number; onStep: (delta: number) => void; stepDisabled?: boolean; onCommitDraft: () => void
  tracedSize?: { width: number; height: number } | null
  // The physical size the trace uses, set by the nearest cleanup; null traces in px.
  traceSize?: PhysicalSize | null
  // The tracer the server traces with (its health report); the read-only tracer line
  // names t3api and its default setting without it, and its build with it.
  tracer?: TracerInfo | null
  // Whether `paletteColours` is the shown revision's own (not the last one's, still
  // listed while it loads); the Cleanup form judges its removed colour only then.
  removalPaletteCurrent?: boolean
  // Why Cleanup is blocked: the form still holds the last cleanup's removed colour, which
  // is not exactly one colour of this palette (EditorState.inherited_remove_colour).
  removalBlocked?: string | null
  // The canvas marker id of the preservation row hovered or focused, or null.
  onHighlightPoint?: (id: string | null) => void
}) {
  const p = <K extends keyof PaletteSettings>(key: K, value: PaletteSettings[K]) => setPalette({ ...palette, [key]: value })
  const t = <K extends keyof TraceSettings>(key: K, value: TraceSettings[K]) => setTrace({ ...trace, [key]: value })
  const c = <K extends keyof CleanupSettings>(key: K, value: CleanupSettings[K]) => setCleanup({ ...cleanup, [key]: value })
  const pd = (key: keyof PaletteSettings) => !sameSetting(palette[key], basePalette[key])
  const td = (key: keyof TraceSettings) => !sameSetting(trace[key], baseTrace[key])
  const cd = (key: keyof CleanupSettings) => !sameSetting(cleanup[key], baseCleanup[key])
  // Judged only against the palette of the shown revision, so a load never flashes a
  // false alert against the previous revision's palette.
  const removal = removedColourChoice(cleanup.remove_colour, removalPaletteCurrent ? paletteColours : [])
  const removalProblem = removalBlocked ? removalBlocked
    : removal.status === 'missing' ? `No colour of this artwork's palette is ${cleanup.remove_colour}: choose the colour to remove.`
    : removal.status === 'ambiguous' ? `${cleanup.remove_colour} names more than one colour of this artwork's palette, by name or hex: choose the colour to remove.`
      : null
  const removalNote = removal.colour && !removalBlocked ? <small id="remove-colour-status" className="remove-colour-match"><span className="swatch" style={{ background: removal.colour.hex }} /> Removes {removal.colour.name || removal.colour.hex} ({removal.colour.hex})</small>
    : removalProblem ? <small id="remove-colour-status" className="remove-colour-missing" role="alert">{removalProblem}</small> : null
  // Cleanup's lengths and areas follow its own physical size: mm with one, else px.
  const physical = cleanup.width_mm !== null || cleanup.height_mm !== null
  const setting = tracer?.setting ?? T3_SETTING
  const length = physical ? 'mm' : 'px'
  const area = physical ? 'mm²' : 'px²'
  const preservationDirty = pd('protected_component_points') || pd('preserved_colour_points') || pd('distinct_colour_point_pairs')
  // A field committed to localStorage on blur so a refresh keeps un-run edits; running
  // the stage bakes them into a revision and the parent then clears the draft.
  return <div className="stage-settings" onBlur={onCommitDraft}>
    <div className="settings-actions">
      {/* Flatten steps only its form's settings; trace steps between saved runs, each
          becoming the active revision with its own traced image. */}
      <div className="iteration-stepper" role="group" aria-label={stage === 'trace' ? 'Step through trace runs' : 'Step through previous iterations'}>
        <button type="button" className="secondary" aria-label={stage === 'trace' ? 'Show the previous trace run' : "Load an older iteration's settings"} disabled={stepDisabled || iterationIndex >= iterationCount - 1} onClick={() => onStep(1)}>◀</button>
        <span className="iteration-indicator">{iterationCount === 0 ? 'No past runs' : `Iteration ${iterationCount - iterationIndex} of ${iterationCount}`}</span>
        <button type="button" className="secondary" aria-label={stage === 'trace' ? 'Show the next trace run' : "Load a newer iteration's settings"} disabled={stepDisabled || iterationIndex <= 0} onClick={() => onStep(-1)}>▶</button>
      </div>
      <button type="button" className="secondary" onClick={onReset}>Reset this stage</button>
    </div>
    <p className="settings-save-note">Edited fields show an amber dot and are kept as a pending draft when you leave a field. Running the stage records the exact values in a new revision and clears the draft.</p>
    {stage === 'flatten' ? <>
      <section className="settings-group"><h3>Colour target</h3><p>Choose the final palette and how readily similar shades combine.</p><div className="settings-grid">
        <Field label="Final colour count" help="Automatic infers a palette. Fixed targets an exact count; if the image can't sustain it, fewer colours are produced." dirty={pd('final_colours')}><select value={palette.final_colours === null ? 'automatic' : 'fixed'} onChange={event => p('final_colours', event.target.value === 'automatic' ? null : Math.min(6, palette.initial_colours))}><option value="automatic">Automatic</option><option value="fixed">Fixed</option></select></Field>
        {palette.final_colours !== null && <NumberField label="Requested colours" help="Exact number of colours in the flattened image." value={palette.final_colours} min={2} max={palette.initial_colours} step={1} dirty={pd('final_colours')} onChange={value => p('final_colours', value!)} />}
        <NumberField label="Initial candidates" help="Candidate clusters considered before merging; more retains subtle source variation." value={palette.initial_colours} min={2} max={64} step={1} dirty={pd('initial_colours')} onChange={value => p('initial_colours', value!)} />
        <NumberField label="Minimum output colours" help="Lower bound used while final count is automatic." value={palette.min_output_colours} min={2} step={1} dirty={pd('min_output_colours')} onChange={value => p('min_output_colours', value!)} />
        <NumberField label="Similar-colour distance (ΔE)" help="Higher values merge colours that look farther apart." value={palette.similar_delta_e} dirty={pd('similar_delta_e')} onChange={value => p('similar_delta_e', value!)} />
        <NumberField label="Minimum whole-colour share" help="Colours below this image fraction become merge candidates." value={palette.min_colour_share} max={1} step={.001} dirty={pd('min_colour_share')} onChange={value => p('min_colour_share', value!)} />
        <NumberField label="Minimum region (pixels)" help="Four-way connected regions smaller than this fold into a touching colour." value={palette.min_region_pixels} step={1} dirty={pd('min_region_pixels')} onChange={value => p('min_region_pixels', value!)} />
        <NumberField label="Minimum feature width (pixels)" help="Removes features thinner than this width regardless of their length, unlike the area-based minimum region setting above. 0 disables it." value={palette.min_feature_width} step={1} dirty={pd('min_feature_width')} onChange={value => p('min_feature_width', value!)} />
        <NumberField label="Output upscale (×)" help="Nearest-neighbour scale for the flattened image and every traced layer. 1 keeps native resolution." value={palette.flatten_upscale} min={1} max={4} step={1} dirty={pd('flatten_upscale')} onChange={value => p('flatten_upscale', value!)} />
        <label className="check-field"><input type="checkbox" checked={palette.flatten_smooth_edges} onChange={event => p('flatten_smooth_edges', event.target.checked)} /><span><strong>Smooth upscaled edges<DirtyDot dirty={pd('flatten_smooth_edges')} /></strong><small>Scale2x corner fill that softens the upscaled staircase using only palette colours. Applies at even upscale factors; protected details keep their hard edge.</small></span></label>
      </div></section>
      <section className="settings-group"><h3>Fine detail &amp; anti-aliasing</h3><p>Separate deliberate narrow artwork from blended edge fragments. Turn a feature off to keep its details untouched.</p>
        <ToggleGroup label="Fold small colours" help="Merge tiny colours into a near neighbour. Off keeps small accents as their own colour." on={palette.fold_small_colours} dirty={pd('fold_small_colours')} onToggle={value => p('fold_small_colours', value)}>
          <NumberField label="Small-colour share" help="Image fraction below which a colour is treated as small." value={palette.small_share} max={1} step={.001} dirty={pd('small_share')} disabled={!palette.fold_small_colours} onChange={value => p('small_share', value!)} />
          <NumberField label="Small-colour distance (ΔE)" help="Maximum difference when folding a small colour into a neighbour." value={palette.small_delta_e} dirty={pd('small_delta_e')} disabled={!palette.fold_small_colours} onChange={value => p('small_delta_e', value!)} />
        </ToggleGroup>
        <ToggleGroup label="Merge fragmented colours" help="Collapse small scattered colours into a similar neighbour. Off keeps fragmented detail." on={palette.merge_fragmented_colours} dirty={pd('merge_fragmented_colours')} onToggle={value => p('merge_fragmented_colours', value)}>
          <NumberField label="Fragmented colour share" help="Maximum image share considered for fragmentation cleanup." value={palette.fragmented_share} max={1} step={.001} dirty={pd('fragmented_share')} disabled={!palette.merge_fragmented_colours} onChange={value => p('fragmented_share', value!)} />
          <NumberField label="Largest-region coherence" help="Share that must belong to one region before a colour is coherent." value={palette.fragmented_largest_region} max={1} step={.01} dirty={pd('fragmented_largest_region')} disabled={!palette.merge_fragmented_colours} onChange={value => p('fragmented_largest_region', value!)} />
          <NumberField label="Fragmentation distance (ΔE)" help="Colour-distance limit for merging fragmented pieces." value={palette.fragmented_delta_e} dirty={pd('fragmented_delta_e')} disabled={!palette.merge_fragmented_colours} onChange={value => p('fragmented_delta_e', value!)} />
        </ToggleGroup>
        <ToggleGroup label="Protect intentional lines" help="Detect and keep deliberate narrow linework un-flattened. Off lets thin marks flatten like any other detail — no need to defeat it with a large length." on={palette.protect_intentional_lines} dirty={pd('protect_intentional_lines')} onToggle={value => p('protect_intentional_lines', value)}>
          <NumberField label="Intentional-line contrast" help="Minimum contrast for a narrow feature to count as deliberate linework." value={palette.intentional_line_min_contrast} dirty={pd('intentional_line_min_contrast')} disabled={!palette.protect_intentional_lines} onChange={value => p('intentional_line_min_contrast', value!)} />
          <NumberField label="Intentional-line length" help="Minimum coherent pixel length for narrow line protection." value={palette.intentional_line_min_length} min={1} step={1} dirty={pd('intentional_line_min_length')} disabled={!palette.protect_intentional_lines} onChange={value => p('intentional_line_min_length', value!)} />
        </ToggleGroup>
        <ToggleGroup label="Resolve anti-aliasing" help="Spatially regularise ambiguous anti-aliased pixels. Off leaves each pixel at its nearest palette colour." on={palette.resolve_antialiasing} dirty={pd('resolve_antialiasing')} onToggle={value => p('resolve_antialiasing', value)}>
          <NumberField label="Anti-alias ambiguity" help="How close competing assignments can be before neighbourhood cleanup decides." value={palette.aa_ambiguity_margin} dirty={pd('aa_ambiguity_margin')} disabled={!palette.resolve_antialiasing} onChange={value => p('aa_ambiguity_margin', value!)} />
          <NumberField label="Spatial smoothness" help="Preference for agreeing with edge-sharing neighbours." value={palette.aa_smoothness} dirty={pd('aa_smoothness')} disabled={!palette.resolve_antialiasing} onChange={value => p('aa_smoothness', value!)} />
          <NumberField label="Resolution passes" help="Deterministic cleanup passes over ambiguous pixels." value={palette.aa_resolution_passes} step={1} dirty={pd('aa_resolution_passes')} disabled={!palette.resolve_antialiasing} onChange={value => p('aa_resolution_passes', value!)} />
        </ToggleGroup>
        <div className="settings-grid">
          <NumberField label="Histogram precision" help="Bits of colour precision used during candidate discovery." value={palette.histogram_bits} min={1} max={8} step={1} dirty={pd('histogram_bits')} onChange={value => p('histogram_bits', value!)} />
          <NumberField label="Analysis max dimension" help="Sampling size for palette discovery; final assignment remains full-resolution." value={palette.analysis_max_dimension} min={32} step={1} dirty={pd('analysis_max_dimension')} onChange={value => p('analysis_max_dimension', value!)} />
          <NumberField label="Dither removal radius" help="Neighbourhood radius for removing dither and pixel noise before flattening. 0 leaves the source untouched. Only speckle that a despeckled copy and the local majority both agree on is reassigned, so isolated real detail and protected details are kept." value={palette.dither_radius} min={0} max={5} step={1} dirty={pd('dither_radius')} onChange={value => p('dither_radius', value!)} />
        </div>
      </section>
      <section className="settings-group preservation-settings"><h3>Preserve important source details<DirtyDot dirty={preservationDirty} /></h3><p>These are precise exceptions, not painting tools. Pick an action, then click the original image.</p><div className="preservation-options">
        <button type="button" className={selectionMode === 'protect-component' ? 'active' : 'secondary'} onClick={() => setSelectionMode(selectionMode === 'protect-component' ? null : 'protect-component')}><strong>Protect connected detail</strong><small>Keep the four-way connected feature at this point.</small></button>
        <button type="button" className={selectionMode === 'preserve-colour' ? 'active' : 'secondary'} onClick={() => setSelectionMode(selectionMode === 'preserve-colour' ? null : 'preserve-colour')}><strong>Preserve colour family</strong><small>Keep this source colour represented in the palette.</small></button>
        <button type="button" className={selectionMode?.startsWith('distinct') ? 'active' : 'secondary'} onClick={() => setSelectionMode('distinct-first')}><strong>Keep two colours separate</strong><small>Select two colours that must not merge.</small></button>
      </div>{selectionMode && <p className="selection-status" role="status">{selectionMode === 'distinct-second' ? 'Now select the second colour.' : selectionMode === 'distinct-first' ? 'Select the first colour.' : 'Select the intended source detail.'}</p>}
      <PointList title="Protected details" kind="protect" points={palette.protected_component_points} onHighlight={onHighlightPoint} onRemove={index => p('protected_component_points', palette.protected_component_points.filter((_, row) => row !== index))} /><PointList title="Preserved colours" kind="preserve" points={palette.preserved_colour_points} onHighlight={onHighlightPoint} onRemove={index => p('preserved_colour_points', palette.preserved_colour_points.filter((_, row) => row !== index))} />
      {palette.distinct_colour_point_pairs.length > 0 && <div className="selection-list"><strong>Separate pairs</strong>{palette.distinct_colour_point_pairs.map((pair, index) => <PointRow key={index} id={preservationMarkerId('pair', index)} number={index + 1} onHighlight={onHighlightPoint} onRemove={() => p('distinct_colour_point_pairs', palette.distinct_colour_point_pairs.filter((_, row) => row !== index))}>{pair[0].join(', ')} ↔ {pair[1].join(', ')}</PointRow>)}</div>}</section>
    </> : stage === 'cleanup' ? <>
      <section className="settings-group"><h3>Physical output</h3><p>Set the vinyl size and the colour left uncut. Trace uses both; changing them means running Cleanup again.</p><div className="settings-grid">
        <NumberField label="Width (mm)" help="Cut width of the whole design in mm; height follows the image proportions. Setting a size switches cleanup to mm; it never changes the curves." value={cleanup.width_mm} optional dirty={cd('width_mm')} onChange={value => c('width_mm', value)} />
        <NumberField label="Height (mm)" help="Cut height of the whole design in mm, as an alternative to width. Blank both to keep cleanup in px." value={cleanup.height_mm} optional dirty={cd('height_mm')} onChange={value => c('height_mm', value)} />
        <div className="remove-colour-field"><Field label="Remove colour" help="Leave one colour out of the cut, by name or hex, usually the colour of the backing material. Blank cuts every colour." dirty={cd('remove_colour')}><input value={cleanup.remove_colour ?? ''} placeholder="None" aria-invalid={removalProblem ? true : undefined} aria-describedby={removalNote ? 'remove-colour-status' : undefined} onChange={event => c('remove_colour', event.target.value || null)} /></Field>
          {removalNote}</div>
      </div></section>
      <section className="settings-group"><h3>Cutter cleanup</h3><p>Optionally remove or widen geometry that is difficult to cut.</p><div className="settings-grid">
        <label className="check-field"><input type="checkbox" checked={cleanup.cleanup} onChange={event => c('cleanup', event.target.checked)} /><span><strong>Enable cutter cleanup<DirtyDot dirty={cd('cleanup')} /></strong><small>On: remove or widen parts too small or thin to cut and weed, using the rules below, before tracing. Off: the run only records the size and removed colour, and the traced shapes follow the flattened image exactly.</small></span></label>
        <Field label="Cleanup action" help="Remove: small or thin parts become a neighbouring colour. Widen: thin parts grow into their neighbours. Remove then widen: drop the smallest, then widen the rest." dirty={cd('cleanup_action')}><select value={cleanup.cleanup_action} onChange={event => c('cleanup_action', event.target.value as CleanupSettings['cleanup_action'])}><option value="remove">Remove</option><option value="widen">Widen</option><option value="remove_then_widen">Remove then widen</option></select></Field>
        <NumberField label={`Minimum detail width (${length})`} help={`Parts thinner than this, in ${length}, are removed (Remove) or widened (Widen, Remove then widen). 0 turns it off; higher cleans up more.`} value={cleanup.min_detail_width} dirty={cd('min_detail_width')} onChange={value => c('min_detail_width', value!)} /><NumberField label={`Minimum region area (${area})`} help={`Regions smaller than this area, in ${area}, are removed (with Widen, grown instead). 0 turns it off; higher cleans up larger specks.`} value={cleanup.min_region_area} dirty={cd('min_region_area')} onChange={value => c('min_region_area', value!)} /><NumberField label={`Remove below width (${length})`} help={`With Remove then widen: parts thinner than this, in ${length}, are removed before the rest are widened. Keep it below Minimum detail width; 0 turns it off.`} value={cleanup.remove_below_width} dirty={cd('remove_below_width')} onChange={value => c('remove_below_width', value!)} />
      </div></section>
    </> : <>
      {tracedSize && <p className="settings-save-note traced-size">Traced image: {tracedSize.width} × {tracedSize.height} px</p>}
      <p className="settings-save-note physical-size">{formatPhysicalSize(traceSize)}</p>
      <p className="settings-save-note tracer-line">Tracer: t3api{tracer?.build ? ` ${tracer.build}` : ''}; default half-widths {CURVE_KINDS.map(row => `${row.kind} ${setting.half[row.kind] ?? 'off'}`).join(', ')} px; A1 = {setting.A1}, A2 = {setting.A2}, L = {setting.L}</p>
      <section className="settings-group"><h3>Curve kinds</h3><p>Each kind's half-width is the furthest, in px, a drawn piece of that kind may lie from its edge.</p>
        {CURVE_KINDS.map(row => {
          const value = trace[row.key]
          const on = value !== null
          const numberField = <NumberField label={row.label} help={`The furthest a drawn ${row.piece} may lie from its edge, in px. ${HALF_WIDTH_HELP}${row.optional ? '' : ' Below about 0.55 px (sampled) long shallow straight edges break into several lines.'}`}
            value={value} min={0} disabled={!on} dirty={on && td(row.key)} onChange={next => { if (next !== null && next > 0) t(row.key, next) }} />
          return row.optional
            ? <ToggleGroup key={row.key} label={row.label} help={`On: ${row.piece}s are drawn within the half-width. Off: this kind is never used.`} on={on} dirty={(value === null) !== (baseTrace[row.key] === null)} onToggle={next => t(row.key, next ? HALF_WIDTH_DEFAULT : null)}>{numberField}</ToggleGroup>
            : <div key={row.key} className="settings-grid">{numberField}</div>
        })}
      </section>
      <section className="settings-group"><h3>Complexity warnings</h3><p>These limits warn; they never silently simplify geometry.</p><div className="settings-grid"><NumberField label="Nodes per path" help="Warn when one shape has more nodes than this. Lower: warns sooner. Never changes the geometry." value={trace.soft_path_nodes} min={1} step={1} dirty={td('soft_path_nodes')} onChange={value => t('soft_path_nodes', value!)} /><NumberField label="Nodes per layer" help="Warn when one colour layer has more nodes than this in total. Lower: warns sooner. Never changes the geometry." value={trace.soft_layer_nodes} min={1} step={1} dirty={td('soft_layer_nodes')} onChange={value => t('soft_layer_nodes', value!)} /><NumberField label="Nodes per document" help="Warn when the whole design has more nodes than this. Lower: warns sooner. Never changes the geometry." value={trace.soft_document_nodes} min={1} step={1} dirty={td('soft_document_nodes')} onChange={value => t('soft_document_nodes', value!)} /></div></section>
    </>}
  </div>
}
