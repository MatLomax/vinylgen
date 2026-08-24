import { useState } from 'react'
import type { PaletteColour, PaletteSettings, TraceSettings } from './api'

export const DEFAULT_PALETTE_SETTINGS: PaletteSettings = {
  initial_colours: 16, final_colours: null, histogram_bits: 5, analysis_max_dimension: 640, similar_delta_e: 28, small_share: .004, small_delta_e: 36,
  fragmented_share: .035, fragmented_largest_region: .30, fragmented_delta_e: 24, min_output_colours: 2, min_region_pixels: 24, min_colour_share: .01,
  intentional_line_min_contrast: 12, intentional_line_min_length: 8, aa_ambiguity_margin: 7, aa_smoothness: 2.5, aa_resolution_passes: 5,
  protected_component_points: [], preserved_colour_points: [], distinct_colour_point_pairs: [],
}
export const DEFAULT_TRACE_SETTINGS: TraceSettings = {
  width_mm: null, height_mm: null, overlap: 0, solid_underlayers: false, remove_colour: null, layer_order: null, corner_threshold: 57.3, curve_optimization: true,
  curve_fit_tolerance: .8, straight_line_max_deviation: .8, minimum_straight_length: 4, max_deviation: null, corner_analysis_span: 8,
  cleanup: false, cleanup_action: 'remove', min_detail_width: 0, min_region_area: 0, remove_below_width: 0,
  soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000,
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

function Field({ label, help, dirty, children }: { label: string; help: string; dirty?: boolean; children: React.ReactNode }) {
  return <label className="setting-field"><span>{label}<DirtyDot dirty={dirty} /></span>{children}<small>{help}</small></label>
}
function NumberField({ label, help, value, onChange, min = 0, max, step = 'any', optional = false, dirty }: {
  label: string; help: string; value: number | null; onChange: (value: number | null) => void; min?: number; max?: number; step?: number | 'any'; optional?: boolean; dirty?: boolean
}) {
  return <Field label={label} help={help} dirty={dirty}><input type="number" min={min} max={max} step={step} value={value ?? ''} placeholder={optional ? 'Automatic' : undefined} onChange={event => { const raw = event.target.value; if (raw === '') { if (optional) onChange(null); return } onChange(Number(raw)) }} /></Field>
}
function PointList({ title, points, onRemove }: { title: string; points: [number, number][]; onRemove: (index: number) => void }) {
  return points.length ? <div className="selection-list"><strong>{title}</strong>{points.map((point, index) => <span key={`${point}-${index}`}><code>{point.join(', ')}</code><button type="button" className="secondary" onClick={() => onRemove(index)}>Remove</button></span>)}</div> : null
}

// Arrange the palette colours to reflect a stored bottom-to-top order. Entries match a
// colour by hex or by name (older text-entered orders used names); colours the order does
// not mention keep their natural position after the matched ones.
function orderColours(colours: PaletteColour[], order: string[] | null): PaletteColour[] {
  if (!order?.length) return [...colours]
  const rank = (colour: PaletteColour) => {
    const at = order.findIndex(entry => entry.toLowerCase() === colour.hex.toLowerCase() || entry === colour.name)
    return at === -1 ? order.length : at
  }
  return [...colours].sort((a, b) => rank(a) - rank(b))
}

// A drag-and-drop reorderable list of the palette colours, bottom to top. Each drag or
// button move rewrites the whole order as hex values, so any stale or partial stored order
// is normalised to the full palette on first interaction. Up/down buttons cover touch and
// keyboard, since native HTML5 drag does not fire on touch screens.
function LayerOrderList({ colours, order, onChange, dirty }: {
  colours: PaletteColour[]; order: string[] | null; onChange: (value: string[] | null) => void; dirty?: boolean
}) {
  const [dragging, setDragging] = useState<number | null>(null)
  // Display top-down (top of the list is the top layer); the stored order is bottom-to-top,
  // so serialise the reversed display back to hex on every move.
  const display = orderColours(colours, order).reverse()
  const move = (from: number, to: number) => {
    if (to < 0 || to >= display.length || from === to) return
    const next = [...display]
    const [item] = next.splice(from, 1)
    next.splice(to, 0, item)
    onChange([...next].reverse().map(colour => colour.hex))
  }
  if (!colours.length) return <Field label="Layer order" help="Comma-separated colour names or hex values, bottom to top." dirty={dirty}><p className="stage-guidance">Flatten this revision to choose a layer order.</p></Field>
  return <div className="setting-field layer-order-field">
    <span>Layer order (top layer first)<DirtyDot dirty={dirty} /></span>
    <ul className="layer-order-list" role="list">
      {display.map((colour, index) => <li key={colour.index}
        draggable
        onDragStart={() => setDragging(index)}
        onDragEnd={() => setDragging(null)}
        onDragOver={event => event.preventDefault()}
        onDrop={event => { event.preventDefault(); if (dragging !== null) move(dragging, index); setDragging(null) }}
        className={dragging === index ? 'is-dragging' : undefined}>
        <span className="drag-handle" aria-hidden="true">⣿</span>
        <span className="swatch" style={{ background: colour.hex }} />
        <span className="layer-order-name">{colour.name || colour.hex}</span>
        <span className="layer-order-buttons">
          <button type="button" className="secondary" disabled={index === 0} aria-label={`Move ${colour.name || colour.hex} up a layer`} onClick={() => move(index, index - 1)}>↑</button>
          <button type="button" className="secondary" disabled={index === display.length - 1} aria-label={`Move ${colour.name || colour.hex} down a layer`} onClick={() => move(index, index + 1)}>↓</button>
        </span>
      </li>)}
    </ul>
    <small>Drag or use the arrows to stack colours, top layer at the top. {order ? <button type="button" className="link-button" onClick={() => onChange(null)}>Reset to automatic</button> : 'Automatic order until you reorder.'}</small>
  </div>
}

export function PipelineControls({ stage, palette, setPalette, basePalette, trace, setTrace, baseTrace, paletteColours, selectionMode, setSelectionMode, onReset, onRestoreLast, onCommitDraft }: {
  stage: 'flatten' | 'trace'; palette: PaletteSettings; setPalette: (value: PaletteSettings) => void; basePalette: PaletteSettings; trace: TraceSettings; setTrace: (value: TraceSettings) => void; baseTrace: TraceSettings
  paletteColours: PaletteColour[]; selectionMode: SelectionMode; setSelectionMode: (value: SelectionMode) => void; onReset: () => void; onRestoreLast: () => void; onCommitDraft: () => void
}) {
  const p = <K extends keyof PaletteSettings>(key: K, value: PaletteSettings[K]) => setPalette({ ...palette, [key]: value })
  const t = <K extends keyof TraceSettings>(key: K, value: TraceSettings[K]) => setTrace({ ...trace, [key]: value })
  const pd = (key: keyof PaletteSettings) => !sameSetting(palette[key], basePalette[key])
  const td = (key: keyof TraceSettings) => !sameSetting(trace[key], baseTrace[key])
  const preservationDirty = pd('protected_component_points') || pd('preserved_colour_points') || pd('distinct_colour_point_pairs')
  // A field committed to localStorage on blur so a refresh keeps un-run edits; running
  // the stage bakes them into a revision and the parent then clears the draft.
  return <div className="stage-settings" onBlur={onCommitDraft}>
    <div className="settings-actions"><button type="button" className="secondary" onClick={onRestoreLast}>Restore last run</button><button type="button" className="secondary" onClick={onReset}>Reset this stage</button></div>
    <p className="settings-save-note">Edited fields show an amber dot and are kept as a pending draft when you leave a field. Running the stage records the exact values in a new revision and clears the draft.</p>
    {stage === 'flatten' ? <>
      <section className="settings-group"><h3>Colour target</h3><p>Choose the final palette and how readily similar shades combine.</p><div className="settings-grid">
        <Field label="Final colour count" help="Automatic infers a palette. Fixed guarantees an exact count or stops with an error." dirty={pd('final_colours')}><select value={palette.final_colours === null ? 'automatic' : 'fixed'} onChange={event => p('final_colours', event.target.value === 'automatic' ? null : Math.min(6, palette.initial_colours))}><option value="automatic">Automatic</option><option value="fixed">Fixed</option></select></Field>
        {palette.final_colours !== null && <NumberField label="Requested colours" help="Exact number of colours in the flattened image." value={palette.final_colours} min={2} max={palette.initial_colours} step={1} dirty={pd('final_colours')} onChange={value => p('final_colours', value!)} />}
        <NumberField label="Initial candidates" help="Candidate clusters considered before merging; more retains subtle source variation." value={palette.initial_colours} min={2} max={64} step={1} dirty={pd('initial_colours')} onChange={value => p('initial_colours', value!)} />
        <NumberField label="Minimum output colours" help="Lower bound used while final count is automatic." value={palette.min_output_colours} min={2} step={1} dirty={pd('min_output_colours')} onChange={value => p('min_output_colours', value!)} />
        <NumberField label="Similar-colour distance (ΔE)" help="Higher values merge colours that look farther apart." value={palette.similar_delta_e} dirty={pd('similar_delta_e')} onChange={value => p('similar_delta_e', value!)} />
        <NumberField label="Minimum whole-colour share" help="Colours below this image fraction become merge candidates." value={palette.min_colour_share} max={1} step={.001} dirty={pd('min_colour_share')} onChange={value => p('min_colour_share', value!)} />
        <NumberField label="Minimum region (pixels)" help="Four-way connected regions smaller than this fold into a touching colour." value={palette.min_region_pixels} step={1} dirty={pd('min_region_pixels')} onChange={value => p('min_region_pixels', value!)} />
      </div></section>
      <section className="settings-group"><h3>Fine detail &amp; anti-aliasing</h3><p>Separate deliberate narrow artwork from blended edge fragments.</p><div className="settings-grid">
        <NumberField label="Small-colour share" help="Image fraction below which a colour is treated as small." value={palette.small_share} max={1} step={.001} dirty={pd('small_share')} onChange={value => p('small_share', value!)} />
        <NumberField label="Small-colour distance (ΔE)" help="Maximum difference when folding a small colour into a neighbour." value={palette.small_delta_e} dirty={pd('small_delta_e')} onChange={value => p('small_delta_e', value!)} />
        <NumberField label="Fragmented colour share" help="Maximum image share considered for fragmentation cleanup." value={palette.fragmented_share} max={1} step={.001} dirty={pd('fragmented_share')} onChange={value => p('fragmented_share', value!)} />
        <NumberField label="Largest-region coherence" help="Share that must belong to one region before a colour is coherent." value={palette.fragmented_largest_region} max={1} step={.01} dirty={pd('fragmented_largest_region')} onChange={value => p('fragmented_largest_region', value!)} />
        <NumberField label="Fragmentation distance (ΔE)" help="Colour-distance limit for merging fragmented pieces." value={palette.fragmented_delta_e} dirty={pd('fragmented_delta_e')} onChange={value => p('fragmented_delta_e', value!)} />
        <NumberField label="Intentional-line contrast" help="Minimum contrast for a narrow feature to count as deliberate linework." value={palette.intentional_line_min_contrast} dirty={pd('intentional_line_min_contrast')} onChange={value => p('intentional_line_min_contrast', value!)} />
        <NumberField label="Intentional-line length" help="Minimum coherent pixel length for narrow line protection." value={palette.intentional_line_min_length} min={1} step={1} dirty={pd('intentional_line_min_length')} onChange={value => p('intentional_line_min_length', value!)} />
        <NumberField label="Anti-alias ambiguity" help="How close competing assignments can be before neighbourhood cleanup decides." value={palette.aa_ambiguity_margin} dirty={pd('aa_ambiguity_margin')} onChange={value => p('aa_ambiguity_margin', value!)} />
        <NumberField label="Spatial smoothness" help="Preference for agreeing with edge-sharing neighbours." value={palette.aa_smoothness} dirty={pd('aa_smoothness')} onChange={value => p('aa_smoothness', value!)} />
        <NumberField label="Resolution passes" help="Deterministic cleanup passes over ambiguous pixels." value={palette.aa_resolution_passes} step={1} dirty={pd('aa_resolution_passes')} onChange={value => p('aa_resolution_passes', value!)} />
        <NumberField label="Histogram precision" help="Bits of colour precision used during candidate discovery." value={palette.histogram_bits} min={1} max={8} step={1} dirty={pd('histogram_bits')} onChange={value => p('histogram_bits', value!)} />
        <NumberField label="Analysis max dimension" help="Sampling size for palette discovery; final assignment remains full-resolution." value={palette.analysis_max_dimension} min={32} step={1} dirty={pd('analysis_max_dimension')} onChange={value => p('analysis_max_dimension', value!)} />
      </div></section>
      <section className="settings-group preservation-settings"><h3>Preserve important source details<DirtyDot dirty={preservationDirty} /></h3><p>These are precise exceptions, not painting tools. Pick an action, then click the original image.</p><div className="preservation-options">
        <button type="button" className={selectionMode === 'protect-component' ? 'active' : 'secondary'} onClick={() => setSelectionMode(selectionMode === 'protect-component' ? null : 'protect-component')}><strong>Protect connected detail</strong><small>Keep the four-way connected feature at this point.</small></button>
        <button type="button" className={selectionMode === 'preserve-colour' ? 'active' : 'secondary'} onClick={() => setSelectionMode(selectionMode === 'preserve-colour' ? null : 'preserve-colour')}><strong>Preserve colour family</strong><small>Keep this source colour represented in the palette.</small></button>
        <button type="button" className={selectionMode?.startsWith('distinct') ? 'active' : 'secondary'} onClick={() => setSelectionMode('distinct-first')}><strong>Keep two colours separate</strong><small>Select two colours that must not merge.</small></button>
      </div>{selectionMode && <p className="selection-status" role="status">{selectionMode === 'distinct-second' ? 'Now select the second colour.' : selectionMode === 'distinct-first' ? 'Select the first colour.' : 'Select the intended source detail.'}</p>}
      <PointList title="Protected details" points={palette.protected_component_points} onRemove={index => p('protected_component_points', palette.protected_component_points.filter((_, row) => row !== index))} /><PointList title="Preserved colours" points={palette.preserved_colour_points} onRemove={index => p('preserved_colour_points', palette.preserved_colour_points.filter((_, row) => row !== index))} />
      {palette.distinct_colour_point_pairs.length > 0 && <div className="selection-list"><strong>Separate pairs</strong>{palette.distinct_colour_point_pairs.map((pair, index) => <span key={index}><code>{pair[0].join(', ')} ↔ {pair[1].join(', ')}</code><button type="button" className="secondary" onClick={() => p('distinct_colour_point_pairs', palette.distinct_colour_point_pairs.filter((_, row) => row !== index))}>Remove</button></span>)}</div>}</section>
    </> : <>
      <section className="settings-group"><h3>Physical output &amp; cleanup</h3><p>Set vinyl size and optionally remove or widen geometry that is difficult to cut.</p><div className="settings-grid">
        <NumberField label="Width (mm)" help="Target width. Blank keeps cleanup thresholds in pixels." value={trace.width_mm} optional dirty={td('width_mm')} onChange={value => t('width_mm', value)} />
        <NumberField label="Height (mm)" help="Target height. Either dimension establishes physical scale." value={trace.height_mm} optional dirty={td('height_mm')} onChange={value => t('height_mm', value)} />
        <NumberField label="Layer overlap" help="Extra shared-boundary overlap added to each vinyl layer." value={trace.overlap} dirty={td('overlap')} onChange={value => t('overlap', value!)} />
        <label className="check-field"><input type="checkbox" checked={trace.solid_underlayers} onChange={event => t('solid_underlayers', event.target.checked)} /><span><strong>Solid underlayers<DirtyDot dirty={td('solid_underlayers')} /></strong><small>Each lower layer stays solid under everything stacked above it, so no cut edges are shared. Overrides the overlap distance.</small></span></label>
        <Field label="Remove colour" help="Omit one palette colour, usually the material background." dirty={td('remove_colour')}><input value={trace.remove_colour ?? ''} placeholder="None" onChange={event => t('remove_colour', event.target.value || null)} /></Field>
        {trace.solid_underlayers && <LayerOrderList colours={paletteColours} order={trace.layer_order} onChange={value => t('layer_order', value)} dirty={td('layer_order')} />}
        <label className="check-field"><input type="checkbox" checked={trace.cleanup} onChange={event => t('cleanup', event.target.checked)} /><span><strong>Enable cutter cleanup<DirtyDot dirty={td('cleanup')} /></strong><small>Apply the width and area rules below before tracing.</small></span></label>
        <Field label="Cleanup action" help="Remove tiny features, widen narrow ones, or do both." dirty={td('cleanup_action')}><select value={trace.cleanup_action} onChange={event => t('cleanup_action', event.target.value as TraceSettings['cleanup_action'])}><option value="remove">Remove</option><option value="widen">Widen</option><option value="remove_then_widen">Remove then widen</option></select></Field>
        <NumberField label="Minimum detail width" help="Widen retained details narrower than this." value={trace.min_detail_width} dirty={td('min_detail_width')} onChange={value => t('min_detail_width', value!)} /><NumberField label="Minimum region area" help="Remove connected regions smaller than this area." value={trace.min_region_area} dirty={td('min_region_area')} onChange={value => t('min_region_area', value!)} /><NumberField label="Remove below width" help="Remove regions whose usable width is below this." value={trace.remove_below_width} dirty={td('remove_below_width')} onChange={value => t('remove_below_width', value!)} />
      </div></section>
      <section className="settings-group"><h3>Path fidelity</h3><p>Balance smooth editable curves against close raster adherence.</p><div className="settings-grid">
        <NumberField label="Corner threshold" help="Minimum turn in degrees before a bend becomes a hard corner. Higher keeps gentle bends as sweeping curves." value={trace.corner_threshold} max={180} dirty={td('corner_threshold')} onChange={value => t('corner_threshold', value!)} /><NumberField label="Corner analysis span" help="Boundary distance examined around a possible corner." value={trace.corner_analysis_span} dirty={td('corner_analysis_span')} onChange={value => t('corner_analysis_span', value!)} /><NumberField label="Curve-fit tolerance" help="Lower follows the raster more closely and creates more nodes." value={trace.curve_fit_tolerance} min={.0001} dirty={td('curve_fit_tolerance')} onChange={value => t('curve_fit_tolerance', value!)} /><NumberField label="Straight-line deviation" help="Maximum deviation allowed for a straight SVG segment." value={trace.straight_line_max_deviation} dirty={td('straight_line_max_deviation')} onChange={value => t('straight_line_max_deviation', value!)} /><NumberField label="Minimum straight length" help="Shortest sustained section eligible to become straight." value={trace.minimum_straight_length} dirty={td('minimum_straight_length')} onChange={value => t('minimum_straight_length', value!)} /><NumberField label="Maximum deviation" help="Optional hard cap on distance from the raster boundary." value={trace.max_deviation} optional min={.0001} dirty={td('max_deviation')} onChange={value => t('max_deviation', value)} />
        <label className="check-field"><input type="checkbox" checked={trace.curve_optimization} onChange={event => t('curve_optimization', event.target.checked)} /><span><strong>Optimize curves<DirtyDot dirty={td('curve_optimization')} /></strong><small>Prefer fewer clean segments when they remain within tolerance.</small></span></label>
      </div></section>
      <section className="settings-group"><h3>Complexity warnings</h3><p>These limits warn; they never silently simplify geometry.</p><div className="settings-grid"><NumberField label="Nodes per path" help="Warn when one SVG path exceeds this." value={trace.soft_path_nodes} min={1} step={1} dirty={td('soft_path_nodes')} onChange={value => t('soft_path_nodes', value!)} /><NumberField label="Nodes per layer" help="Warn when a colour layer exceeds this total." value={trace.soft_layer_nodes} min={1} step={1} dirty={td('soft_layer_nodes')} onChange={value => t('soft_layer_nodes', value!)} /><NumberField label="Nodes per document" help="Warn when the whole SVG exceeds this total." value={trace.soft_document_nodes} min={1} step={1} dirty={td('soft_document_nodes')} onChange={value => t('soft_document_nodes', value!)} /></div></section>
    </>}
  </div>
}
