import { useState } from 'react'
import type { CleanupSettings, PaletteColour, PaletteSettings, TraceSettings } from './api'
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
// Trace lengths are thousandths of the traced image's longer side (vinylgen.contracts);
// these mirror its default tables.
export const TRACE_LENGTH_FIELDS = ['curve_fit_tolerance', 'straight_line_max_deviation', 'minimum_straight_length', 'max_deviation', 'side_smoothness', 'corner_sharpening_allowance', 'corner_analysis_span', 'curve_feature_scale'] as const
export type TraceLengthField = typeof TRACE_LENGTH_FIELDS[number]
const PER_MILLE_DEFAULTS = { curve_fit_tolerance: .75, straight_line_max_deviation: .8, minimum_straight_length: 2, corner_analysis_span: 4, curve_feature_scale: 2 }
// The pixel defaults every setting recorded before units was traced with.
const PX_DEFAULTS: Record<string, number> = { curve_fit_tolerance: 1.5, straight_line_max_deviation: 1.6, minimum_straight_length: 4, corner_analysis_span: 8, curve_feature_scale: 4 }
// Lengths defaulting to another length's value, and automatic lengths (null) with their
// default in thousandths and in pixels.
const FOLLOWING_LENGTHS: Record<string, TraceLengthField> = { side_smoothness: 'straight_line_max_deviation', corner_sharpening_allowance: 'max_deviation' }
const AUTOMATIC_LENGTHS: Record<string, [number, number]> = { max_deviation: [1, 2] }
export const CURVE_FEATURE_SCALE_RANGE: [number, number] = [.25, 16]
export const DEFAULT_TRACE_SETTINGS: TraceSettings = {
  overlap: 0, solid_underlayers: false, layer_order: null, corner_threshold: 60, length_unit: 'per_mille',
  ...PER_MILLE_DEFAULTS, max_deviation: null, side_smoothness: PER_MILLE_DEFAULTS.straight_line_max_deviation, corner_sharpening_allowance: null,
  straight_line_snap: 'axis', snap_angle: 2, node_placement: 'prefer_extremes', nodes_at_bends: true,
  soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000,
}
// Whether a stored trace record holds pixel lengths: it says 'px', or predates units (no
// marker) and sets a length. One setting no length (a draft holding only a layer order)
// has nothing to convert. The server serves every recorded run in thousandths.
export function isPixelRecord(stored: Record<string, unknown>): boolean {
  if (stored.length_unit !== undefined) return stored.length_unit === 'px'
  return TRACE_LENGTH_FIELDS.some(name => typeof stored[name] === 'number')
}
// A stored pixel-era trace record with its lengths in thousandths of an image whose longer
// side is `longSide` px (vinylgen.trace_lengths.pixel_record_in_units): absent lengths take
// the pixel-era defaults, following lengths the recorded value they follow, automatic ones
// their pixel default, so the record traces as it did. Other keys are kept as they are.
export function pixelRecordInUnits<T extends Record<string, unknown>>(stored: T, longSide: number): T {
  if (!isPixelRecord(stored)) return stored
  const lengths: Record<string, number> = {}
  for (const name of TRACE_LENGTH_FIELDS) {
    const value = stored[name]
    if (typeof value === 'number') lengths[name] = value
    else if (name in PX_DEFAULTS) lengths[name] = PX_DEFAULTS[name]
  }
  for (const [name, [, px]] of Object.entries(AUTOMATIC_LENGTHS)) if (lengths[name] === undefined) lengths[name] = px
  for (const [name, source] of Object.entries(FOLLOWING_LENGTHS)) if (lengths[name] === undefined) lengths[name] = lengths[source]
  return { ...stored, ...Object.fromEntries(Object.entries(lengths).map(([name, value]) => [name, value * 1000 / longSide])), length_unit: 'per_mille' }
}
// What a length in thousandths comes to in pixels on an image whose longer side is `longSide`.
export function lengthInPixels(value: number, longSide: number): number {
  return value * longSide / 1000
}
// The help-text hint of a trace length: its pixels on the current image (the automatic
// default for an automatic length left empty, or an explicit `automaticUnits` for a
// length whose automatic value follows another field's, such as the corner sharpening
// allowance following the maximum deviation), or nothing before the image is measured.
// Lengths the trace never resolves below MIN_ACCURACY_PX (vinylgen.trace_lengths): below a
// pixel they would chase the boundary's sub-pixel noise on a small image.
export const FLOORED_LENGTHS: readonly TraceLengthField[] = ['curve_fit_tolerance', 'max_deviation', 'corner_sharpening_allowance']
export const MIN_ACCURACY_PX = 1
export function pixelHint(name: TraceLengthField, value: number | null, longSide: number | null, automaticUnits = AUTOMATIC_LENGTHS[name]?.[0]): string {
  if (!longSide) return ''
  const units = value ?? automaticUnits
  if (units === undefined) return ''
  const px = lengthInPixels(units, longSide)
  const automatic = value === null ? ' (Automatic)' : ''
  if (FLOORED_LENGTHS.includes(name) && px < MIN_ACCURACY_PX) return ` = ${MIN_ACCURACY_PX} px on this image${automatic}, held at the ${MIN_ACCURACY_PX} px minimum.`
  const shown = px >= 10 ? px.toFixed(0) : px >= 1 ? String(Number(px.toFixed(1))) : String(Number(px.toFixed(2)))
  return ` = ${shown} px on this image${automatic}.`
}
const CLEANUP_KEYS = Object.keys(DEFAULT_CLEANUP_SETTINGS) as (keyof CleanupSettings)[]
// Trace settings recorded before the cleanup stage existed embed the physical size, the
// removed colour and the cutter cleanup. Those values belong to the Cleanup form; this
// picks them out of a stored trace record, or returns undefined when it holds none.
export function legacyCleanupSettings(stored: Record<string, unknown>): Partial<CleanupSettings> | undefined {
  const picked = Object.fromEntries(CLEANUP_KEYS.filter(key => key in stored).map(key => [key, stored[key]]))
  return Object.keys(picked).length ? picked as Partial<CleanupSettings> : undefined
}
// Bring stored trace settings up to the current fields. A record still keyed
// `corner_straightness` (its name before it became `side_smoothness`) loads under the
// new key, and the old key is dropped so it is never sent. Settings recorded without
// either key detected corners with their straight-line deviation, so they load with
// that value (absent both, the default applies when merged over the defaults). A
// `curve_optimization: false` flag loads as a straight-line deviation of 0 (straighten
// only exactly straight runs); the flag itself is dropped. Embedded cleanup-stage
// fields (see legacyCleanupSettings) are dropped from the trace form.
export function normalizeTraceSettings<T extends Partial<TraceSettings>>(stored: T & { curve_optimization?: unknown; corner_straightness?: number }): T {
  const { curve_optimization: legacy, corner_straightness: legacySmoothness, ...rest } = stored
  const next: Partial<TraceSettings> = { ...rest }
  for (const key of CLEANUP_KEYS) delete (next as Record<string, unknown>)[key]
  if (next.side_smoothness == null) {
    delete next.side_smoothness
    if (typeof legacySmoothness === 'number') next.side_smoothness = legacySmoothness
    else if (typeof next.straight_line_max_deviation === 'number') next.side_smoothness = next.straight_line_max_deviation
  }
  if (legacy === false) next.straight_line_max_deviation = 0
  return next as T
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

// The palette colour a layer-order entry (or a removed colour) names, resolved as the
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

// Whether layer stacking order can change the traced geometry (layer_order_matters in
// vinylgen/tracing.py): only solid underlayers or a positive overlap extend lower layers
// under upper ones. Otherwise colours meet edge to edge, the tracer infers no order, and
// the automatic order is neither fetched nor offered for reordering.
export function layerOrderMatters(trace: Pick<TraceSettings, 'solid_underlayers' | 'overlap'>): boolean {
  return trace.solid_underlayers || trace.overlap > 0
}

// Arrange the palette colours in the bottom-to-top order a trace stacks them: the stored
// order's colours first (matched exactly as the trace request is, resolvePaletteColour),
// then the rest as the tracer's automatic order (`automatic`, hex bottom-to-top) places
// them, as apply_order_override does; palette order only when that is unknown.
function orderColours(colours: PaletteColour[], order: string[] | null, palette: PaletteColour[] = colours, automatic: string[] | null = null): PaletteColour[] {
  const named = (entries: string[] | null, colour: PaletteColour) => {
    const at = (entries ?? []).findIndex(entry => resolvePaletteColour(entry, palette)?.index === colour.index)
    return at === -1 ? Infinity : at
  }
  return colours.map((colour, position) => ({ colour, key: [named(order, colour), named(automatic, colour), position] }))
    .sort((a, b) => a.key[0] - b.key[0] || a.key[1] - b.key[1] || a.key[2] - b.key[2])
    .map(entry => entry.colour)
}

// A drag-and-drop reorderable list of the palette colours, bottom to top. Each drag or
// button move rewrites the whole order as hex values, so any stale or partial stored order
// is normalised to the full palette on first interaction. Up/down buttons cover touch and
// keyboard, since native HTML5 drag does not fire on touch screens.
// `locked` (a reason) keeps the list shown but not reorderable: the colours listed are not
// the selected revision's own yet, so an order written from them would name stale colours.
// `removedIndex` (the palette index of the colour the active cleanup removes) gets no layer,
// so it is neither listed nor written.
// `onRetry`, when given, offers to re-read what `locked` says failed to load.
function LayerOrderList({ colours: palette, order, onChange, dirty, locked, removedIndex, automaticOrder, onRetry }: {
  colours: PaletteColour[]; order: string[] | null; onChange: (value: string[] | null) => void; dirty?: boolean; locked?: string | null; removedIndex?: number | null; automaticOrder?: string[] | null; onRetry?: (() => void) | null
}) {
  const [dragging, setDragging] = useState<number | null>(null)
  const removed = palette.find(colour => colour.index === removedIndex)
  const colours = palette.filter(colour => colour.index !== removedIndex)
  // Display top-down (top of the list is the top layer); the stored order is bottom-to-top,
  // so serialise the reversed display back to hex on every move.
  const display = orderColours(colours, order, palette, automaticOrder ?? null).reverse()
  const move = (from: number, to: number) => {
    if (locked || to < 0 || to >= display.length || from === to) return
    const next = [...display]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    onChange([...next].reverse().map(colour => colour.hex))
  }
  if (!colours.length) return <Field label="Layer order" help="Stacking order of the layers, bottom to top. Available once the image is flattened." dirty={dirty}><p className="stage-guidance">{locked ?? 'Flatten this revision to choose a layer order.'}</p></Field>
  return <div className="setting-field layer-order-field">
    <span>Layer order (top layer first)<DirtyDot dirty={dirty} /></span>
    <ul className="layer-order-list" role="list">
      {display.map((colour, index) => <li key={colour.index}
        draggable={!locked}
        onDragStart={() => setDragging(index)}
        onDragEnd={() => setDragging(null)}
        onDragOver={event => event.preventDefault()}
        onDrop={event => { event.preventDefault(); if (dragging !== null) move(dragging, index); setDragging(null) }}
        className={dragging === index ? 'is-dragging' : undefined}>
        <span className="drag-handle" aria-hidden="true">⣿</span>
        <span className="swatch" style={{ background: colour.hex }} />
        <span className="layer-order-name">{colour.name || colour.hex}</span>
        <span className="layer-order-buttons">
          <button type="button" className="secondary" disabled={Boolean(locked) || index === 0} aria-describedby={locked ? 'layer-order-lock' : undefined} aria-label={`Move ${colour.name || colour.hex} up a layer`} onClick={() => move(index, index - 1)}>↑</button>
          <button type="button" className="secondary" disabled={Boolean(locked) || index === display.length - 1} aria-describedby={locked ? 'layer-order-lock' : undefined} aria-label={`Move ${colour.name || colour.hex} down a layer`} onClick={() => move(index, index + 1)}>↓</button>
        </span>
      </li>)}
    </ul>
    {removed && <small className="layer-order-removed">{removed.name || removed.hex} is removed in Cleanup and gets no layer.</small>}
    {locked ? <small id="layer-order-lock">{locked}{onRetry && <> <button type="button" className="link-button" onClick={onRetry}>Retry</button></>}</small>
      : <small>Stacking order of the layers: the top of the list is cut as the top layer. Drag or use the arrows to reorder. {order ? <button type="button" className="link-button" onClick={() => onChange(null)}>Reset to automatic</button> : 'Automatic order until you reorder.'}</small>}
  </div>
}

export function PipelineControls({ stage, palette, setPalette, basePalette, trace, setTrace, baseTrace, cleanup = DEFAULT_CLEANUP_SETTINGS, setCleanup = () => undefined, baseCleanup = DEFAULT_CLEANUP_SETTINGS, paletteColours, selectionMode, setSelectionMode, onReset, iterationCount, iterationIndex, onStep, stepDisabled = false, onCommitDraft, tracedSize = null, traceSize = null, layerOrderLocked = null, removedIndex = null, removalPaletteCurrent = true, removalBlocked = null, automaticOrder = null, onRetryLayerOrder = null, onHighlightPoint = () => undefined }: {
  stage: 'flatten' | 'cleanup' | 'trace'; palette: PaletteSettings; setPalette: (value: PaletteSettings) => void; basePalette: PaletteSettings; trace: TraceSettings; setTrace: (value: TraceSettings) => void; baseTrace: TraceSettings
  cleanup?: CleanupSettings; setCleanup?: (value: CleanupSettings) => void; baseCleanup?: CleanupSettings
  paletteColours: PaletteColour[]; selectionMode: SelectionMode; setSelectionMode: (value: SelectionMode) => void; onReset: () => void; iterationCount: number; iterationIndex: number; onStep: (delta: number) => void; stepDisabled?: boolean; onCommitDraft: () => void
  tracedSize?: { width: number; height: number } | null
  // The physical size the trace uses, set by the nearest cleanup; null traces in px.
  traceSize?: PhysicalSize | null
  // Why the layer order cannot be rewritten now: its colours are not the selected revision's,
  // or the automatic order unnamed colours follow is still loading or failed to load.
  layerOrderLocked?: string | null
  // The palette index of the colour the active cleanup removes; it gets no layer.
  removedIndex?: number | null
  // Whether `paletteColours` is the shown revision's own (not the last one's, still
  // listed while it loads); the Cleanup form judges its removed colour only then.
  removalPaletteCurrent?: boolean
  // Why Cleanup is blocked: the form still holds the last cleanup's removed colour, which
  // is not exactly one colour of this palette (EditorState.inherited_remove_colour).
  removalBlocked?: string | null
  // The tracer's automatic bottom-to-top order (hex), which unnamed colours follow.
  automaticOrder?: string[] | null
  // Re-reads the automatic order after a failed read (shown with `layerOrderLocked`).
  onRetryLayerOrder?: (() => void) | null
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
  // Trace lengths are thousandths of the traced image's longer side, each hinted in its
  // pixels on this image; physical settings follow the output size: the Cleanup form's
  // own, and for Trace the size its cleanup set.
  const longSide = tracedSize ? Math.max(tracedSize.width, tracedSize.height) : null
  // The corner sharpening allowance's own automatic value is not fixed: left empty it
  // follows the maximum deviation, whether that is itself a set value or automatic.
  const hint = (name: TraceLengthField) => name === 'corner_sharpening_allowance'
    ? pixelHint(name, trace.corner_sharpening_allowance, longSide, trace.max_deviation ?? AUTOMATIC_LENGTHS.max_deviation[0])
    : pixelHint(name, trace[name], longSide)
  const physical = stage === 'cleanup' ? cleanup.width_mm !== null || cleanup.height_mm !== null : traceSize !== null
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
        <NumberField label="Width (mm)" help="Cut width of the whole design in mm; height follows the image proportions. Setting a size switches overlap and cleanup to mm; it never changes the curves." value={cleanup.width_mm} optional dirty={cd('width_mm')} onChange={value => c('width_mm', value)} />
        <NumberField label="Height (mm)" help="Cut height of the whole design in mm, as an alternative to width. Blank both to keep overlap and cleanup in px." value={cleanup.height_mm} optional dirty={cd('height_mm')} onChange={value => c('height_mm', value)} />
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
      <section className="settings-group"><h3>Layers &amp; overlap</h3><p>Stack the colour layers for cutting. The physical size and removed colour are set in Cleanup.</p>
        <p className="settings-save-note physical-size">{formatPhysicalSize(traceSize)}</p>
        <div className="settings-grid">
        <NumberField label={`Layer overlap (${length})`} help={`How far each lower layer extends under the layers above it, in ${length}. 0: colours meet edge to edge. Higher: more forgiving of misaligned layers, uses more vinyl.`} value={trace.overlap} dirty={td('overlap')} onChange={value => t('overlap', value!)} />
        <label className="check-field"><input type="checkbox" checked={trace.solid_underlayers} onChange={event => t('solid_underlayers', event.target.checked)} /><span><strong>Solid underlayers<DirtyDot dirty={td('solid_underlayers')} /></strong><small>On: each lower layer is cut as a solid shape under everything above it, so no edges are shared and alignment is easiest; uses the most vinyl. Replaces the overlap distance.</small></span></label>
        {layerOrderMatters(trace) && <LayerOrderList colours={paletteColours} order={trace.layer_order} onChange={value => t('layer_order', value)} dirty={td('layer_order')} locked={layerOrderLocked} removedIndex={removedIndex} automaticOrder={automaticOrder} onRetry={onRetryLayerOrder} />}
      </div></section>
      <section className="settings-group"><h3>Path fidelity</h3><p>Balance smooth editable curves against close raster adherence. Lengths here are thousandths (‰) of the traced image's longer side, so a setting traces alike at any image size; each says what it comes to in pixels on this image.</p><div className="settings-grid">
        <NumberField label="Corner threshold" help="How far, in degrees, the edge must change direction where two smooth sides meet (straight or curved) to become a hard corner. Only hard corners and points where three colours meet are sharp nodes; every other node joins smoothly and is placed by Node placement below. Lower: more bends become sharp corners. Higher: only sharper turns do." value={trace.corner_threshold} max={180} dirty={td('corner_threshold')} onChange={value => t('corner_threshold', value!)} /><NumberField label="Analysis scale (‰)" help={`Length of edge, in ‰ of the image's longer side, checked on each side of a possible corner. Lower: corners on short edges are found. Higher: small wiggles and pixel steps are ignored.${hint('corner_analysis_span')}`} value={trace.corner_analysis_span} dirty={td('corner_analysis_span')} onChange={value => t('corner_analysis_span', value!)} /><NumberField label="Side smoothness (‰)" help={`How far, in ‰ of the image's longer side, the edge on each side of a possible corner may stray from a smooth arc (a straight line or a steady curve) and still confirm the corner. Lower: only clean sides make sharp corners. Higher: rougher sides still get sharp corners.${hint('side_smoothness')}`} value={trace.side_smoothness} dirty={td('side_smoothness')} onChange={value => t('side_smoothness', value!)} /><NumberField label="Corner sharpening allowance (‰)" help={`How far, in ‰ of the image's longer side, a sharpened corner may lie from the drawn edge (Automatic: the maximum deviation). A corner becomes the point where its two sides meet only within this distance, so roundings up to about 2.4 times it snap sharp and larger ones stay round. Lower: only near-sharp corners sharpen. Higher: softer roundings become sharp corners.${hint('corner_sharpening_allowance')}`} value={trace.corner_sharpening_allowance} optional min={.0001} dirty={td('corner_sharpening_allowance')} onChange={value => t('corner_sharpening_allowance', value)} /><NumberField label="Curve-fit tolerance (‰)" help={`How far, in ‰ of the image's longer side, a curve may stray from the pixel edge. Curves use as few nodes as stay within it, so this sets the node count directly. Lower: follows the pixels more closely, more nodes. Higher: fewer nodes, smoother curves.${hint('curve_fit_tolerance')}`} value={trace.curve_fit_tolerance} min={.0001} dirty={td('curve_fit_tolerance')} onChange={value => t('curve_fit_tolerance', value!)} /><NumberField label="Straight-line deviation (‰)" help={`How far, in ‰ of the image's longer side, the edge may stray from the straight line drawn for it, capped at the maximum deviation. Higher: straightens more, fewer nodes. 0: turns this off, though bends shallower than the curve-fit tolerance can still become lines; lower that tolerance to keep them curved.${hint('straight_line_max_deviation')}`} value={trace.straight_line_max_deviation} dirty={td('straight_line_max_deviation')} onChange={value => t('straight_line_max_deviation', value!)} /><NumberField label="Minimum straight length (‰)" help={`Shortest edge, in ‰ of the image's longer side, that can become a straight line. Lower: short edges straighten too. Higher: only long edges straighten; short ones stay curved.${hint('minimum_straight_length')}`} value={trace.minimum_straight_length} dirty={td('minimum_straight_length')} onChange={value => t('minimum_straight_length', value!)} /><NumberField label="Maximum deviation (‰)" help={`In ‰ of the image's longer side (Automatic: 1): caps the curve-fit tolerance and the straight-line deviation, and sets how much drift the check after tracing allows; the corner sharpening allowance follows it unless set. Lower than either: that fit is tighter, with more nodes, and the check stricter; higher: only relaxes the check.${hint('max_deviation')}`} value={trace.max_deviation} optional min={.0001} dirty={td('max_deviation')} onChange={value => t('max_deviation', value)} />
      </div></section>
      <section className="settings-group"><h3>Nodes &amp; straight lines</h3><p>Where curve nodes go and how straight edges are drawn, for paths that look drawn by hand.</p><div className="settings-grid">
        <Field label="Node placement" help="Fewest nodes, prefer extremes: as few nodes as the curve-fit tolerance allows, at the leftmost, rightmost, top and bottom points of curves when that costs no extra node. Always at extremes: a node at every such point, even when that adds nodes. Fewest nodes only: as few nodes as possible, wherever they fit best." dirty={td('node_placement')}><select value={trace.node_placement} onChange={event => t('node_placement', event.target.value as TraceSettings['node_placement'])}><option value="prefer_extremes">Fewest nodes, prefer extremes</option><option value="at_extremes">Always at extremes</option><option value="fewest">Fewest nodes only</option></select></Field>
        <label className="check-field"><input type="checkbox" checked={trace.nodes_at_bends} onChange={event => t('nodes_at_bends', event.target.checked)} /><span><strong>Nodes at tightest bends<DirtyDot dirty={td('nodes_at_bends')} /></strong><small>On: the tightest point of a bend can take a node, like a hand-drawn path. Off: nodes go at extremes, inflections and wherever the tolerance needs one.</small></span></label>
        <NumberField label="Curve feature scale (‰)" help={`Length of edge, in ‰ of the image's longer side, smoothed over before looking for extremes, inflections and bends. Lower: small bumps get nodes of their own. Higher: pixel steps and small wiggles are ignored.${hint('curve_feature_scale')}`} value={trace.curve_feature_scale} min={CURVE_FEATURE_SCALE_RANGE[0]} max={CURVE_FEATURE_SCALE_RANGE[1]} dirty={td('curve_feature_scale')} onChange={value => t('curve_feature_scale', value!)} />
        <Field label="Snap straight lines" help="Make straight edges exactly horizontal or vertical (and, with Also 45°, diagonal) when they are within the snap angle, never moving an edge further than the straight-line deviation (or the maximum deviation, if lower). Off: straight edges keep their measured angle." dirty={td('straight_line_snap')}><select value={trace.straight_line_snap} onChange={event => t('straight_line_snap', event.target.value as TraceSettings['straight_line_snap'])}><option value="off">Off</option><option value="axis">Horizontal &amp; vertical</option><option value="axis_diagonal">Also 45°</option></select></Field>
        <NumberField label="Snap angle (°)" help="How far, in degrees, a straight edge may be off horizontal, vertical or 45° and still snap. Lower: only nearly exact edges snap. Higher: more edges snap." value={trace.snap_angle} min={.5} max={10} disabled={trace.straight_line_snap === 'off'} dirty={td('snap_angle')} onChange={value => t('snap_angle', value!)} />
      </div></section>
      <section className="settings-group"><h3>Complexity warnings</h3><p>These limits warn; they never silently simplify geometry.</p><div className="settings-grid"><NumberField label="Nodes per path" help="Warn when one shape has more nodes than this. Lower: warns sooner. Never changes the geometry." value={trace.soft_path_nodes} min={1} step={1} dirty={td('soft_path_nodes')} onChange={value => t('soft_path_nodes', value!)} /><NumberField label="Nodes per layer" help="Warn when one colour layer has more nodes than this in total. Lower: warns sooner. Never changes the geometry." value={trace.soft_layer_nodes} min={1} step={1} dirty={td('soft_layer_nodes')} onChange={value => t('soft_layer_nodes', value!)} /><NumberField label="Nodes per document" help="Warn when the whole design has more nodes than this. Lower: warns sooner. Never changes the geometry." value={trace.soft_document_nodes} min={1} step={1} dirty={td('soft_document_nodes')} onChange={value => t('soft_document_nodes', value!)} /></div></section>
    </>}
  </div>
}
