import type { PaletteSettings, TraceSettings } from './api'

export const DEFAULT_PALETTE_SETTINGS: PaletteSettings = {
  initial_colours: 16, final_colours: null, histogram_bits: 5, analysis_max_dimension: 640, similar_delta_e: 28, small_share: .004, small_delta_e: 36,
  fragmented_share: .035, fragmented_largest_region: .30, fragmented_delta_e: 24, min_output_colours: 2, min_region_pixels: 24, min_colour_share: .01,
  intentional_line_min_contrast: 12, intentional_line_min_length: 8, aa_ambiguity_margin: 7, aa_smoothness: 2.5, aa_resolution_passes: 5,
  protected_component_points: [], preserved_colour_points: [], distinct_colour_point_pairs: [],
}
export const DEFAULT_TRACE_SETTINGS: TraceSettings = {
  width_mm: null, height_mm: null, overlap: 0, remove_colour: null, layer_order: null, corner_threshold: 1, curve_optimization: true,
  curve_fit_tolerance: .8, straight_line_max_deviation: .8, minimum_straight_length: 4, max_deviation: null, corner_analysis_span: 8,
  cleanup: false, cleanup_action: 'remove', min_detail_width: 0, min_region_area: 0, remove_below_width: 0,
  soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000,
}
export type SelectionMode = 'protect-component' | 'preserve-colour' | 'distinct-first' | 'distinct-second' | null

function Field({ label, help, children }: { label: string; help: string; children: React.ReactNode }) {
  return <label className="setting-field"><span>{label}</span>{children}<small>{help}</small></label>
}
function NumberField({ label, help, value, onChange, min = 0, max, step = 'any', optional = false }: {
  label: string; help: string; value: number | null; onChange: (value: number | null) => void; min?: number; max?: number; step?: number | 'any'; optional?: boolean
}) {
  return <Field label={label} help={help}><input type="number" min={min} max={max} step={step} value={value ?? ''} placeholder={optional ? 'Automatic' : undefined} onChange={event => { const raw = event.target.value; if (raw === '') { if (optional) onChange(null); return } onChange(Number(raw)) }} /></Field>
}
function PointList({ title, points, onRemove }: { title: string; points: [number, number][]; onRemove: (index: number) => void }) {
  return points.length ? <div className="selection-list"><strong>{title}</strong>{points.map((point, index) => <span key={`${point}-${index}`}><code>{point.join(', ')}</code><button type="button" className="secondary" onClick={() => onRemove(index)}>Remove</button></span>)}</div> : null
}

export function PipelineControls({ stage, palette, setPalette, trace, setTrace, selectionMode, setSelectionMode, onReset, onRestoreLast }: {
  stage: 'flatten' | 'trace'; palette: PaletteSettings; setPalette: (value: PaletteSettings) => void; trace: TraceSettings; setTrace: (value: TraceSettings) => void
  selectionMode: SelectionMode; setSelectionMode: (value: SelectionMode) => void; onReset: () => void; onRestoreLast: () => void
}) {
  const p = <K extends keyof PaletteSettings>(key: K, value: PaletteSettings[K]) => setPalette({ ...palette, [key]: value })
  const t = <K extends keyof TraceSettings>(key: K, value: TraceSettings[K]) => setTrace({ ...trace, [key]: value })
  return <div className="stage-settings">
    <div className="settings-actions"><button type="button" className="secondary" onClick={onRestoreLast}>Restore last run</button><button type="button" className="secondary" onClick={onReset}>Reset this stage</button></div>
    <p className="settings-save-note">Changes save automatically for this project. Running the stage records the exact values in a new revision.</p>
    {stage === 'flatten' ? <>
      <section className="settings-group"><h3>Colour target</h3><p>Choose the final palette and how readily similar shades combine.</p><div className="settings-grid">
        <Field label="Final colour count" help="Automatic infers a palette. Fixed guarantees an exact count or stops with an error."><select value={palette.final_colours === null ? 'automatic' : 'fixed'} onChange={event => p('final_colours', event.target.value === 'automatic' ? null : Math.min(6, palette.initial_colours))}><option value="automatic">Automatic</option><option value="fixed">Fixed</option></select></Field>
        {palette.final_colours !== null && <NumberField label="Requested colours" help="Exact number of colours in the flattened image." value={palette.final_colours} min={2} max={palette.initial_colours} step={1} onChange={value => p('final_colours', value!)} />}
        <NumberField label="Initial candidates" help="Candidate clusters considered before merging; more retains subtle source variation." value={palette.initial_colours} min={2} max={64} step={1} onChange={value => p('initial_colours', value!)} />
        <NumberField label="Minimum output colours" help="Lower bound used while final count is automatic." value={palette.min_output_colours} min={2} step={1} onChange={value => p('min_output_colours', value!)} />
        <NumberField label="Similar-colour distance (ΔE)" help="Higher values merge colours that look farther apart." value={palette.similar_delta_e} onChange={value => p('similar_delta_e', value!)} />
        <NumberField label="Minimum whole-colour share" help="Colours below this image fraction become merge candidates." value={palette.min_colour_share} max={1} step={.001} onChange={value => p('min_colour_share', value!)} />
        <NumberField label="Minimum region (pixels)" help="Four-way connected regions smaller than this fold into a touching colour." value={palette.min_region_pixels} step={1} onChange={value => p('min_region_pixels', value!)} />
      </div></section>
      <section className="settings-group"><h3>Fine detail &amp; anti-aliasing</h3><p>Separate deliberate narrow artwork from blended edge fragments.</p><div className="settings-grid">
        <NumberField label="Small-colour share" help="Image fraction below which a colour is treated as small." value={palette.small_share} max={1} step={.001} onChange={value => p('small_share', value!)} />
        <NumberField label="Small-colour distance (ΔE)" help="Maximum difference when folding a small colour into a neighbour." value={palette.small_delta_e} onChange={value => p('small_delta_e', value!)} />
        <NumberField label="Fragmented colour share" help="Maximum image share considered for fragmentation cleanup." value={palette.fragmented_share} max={1} step={.001} onChange={value => p('fragmented_share', value!)} />
        <NumberField label="Largest-region coherence" help="Share that must belong to one region before a colour is coherent." value={palette.fragmented_largest_region} max={1} step={.01} onChange={value => p('fragmented_largest_region', value!)} />
        <NumberField label="Fragmentation distance (ΔE)" help="Colour-distance limit for merging fragmented pieces." value={palette.fragmented_delta_e} onChange={value => p('fragmented_delta_e', value!)} />
        <NumberField label="Intentional-line contrast" help="Minimum contrast for a narrow feature to count as deliberate linework." value={palette.intentional_line_min_contrast} onChange={value => p('intentional_line_min_contrast', value!)} />
        <NumberField label="Intentional-line length" help="Minimum coherent pixel length for narrow line protection." value={palette.intentional_line_min_length} min={1} step={1} onChange={value => p('intentional_line_min_length', value!)} />
        <NumberField label="Anti-alias ambiguity" help="How close competing assignments can be before neighbourhood cleanup decides." value={palette.aa_ambiguity_margin} onChange={value => p('aa_ambiguity_margin', value!)} />
        <NumberField label="Spatial smoothness" help="Preference for agreeing with edge-sharing neighbours." value={palette.aa_smoothness} onChange={value => p('aa_smoothness', value!)} />
        <NumberField label="Resolution passes" help="Deterministic cleanup passes over ambiguous pixels." value={palette.aa_resolution_passes} step={1} onChange={value => p('aa_resolution_passes', value!)} />
        <NumberField label="Histogram precision" help="Bits of colour precision used during candidate discovery." value={palette.histogram_bits} min={1} max={8} step={1} onChange={value => p('histogram_bits', value!)} />
        <NumberField label="Analysis max dimension" help="Sampling size for palette discovery; final assignment remains full-resolution." value={palette.analysis_max_dimension} min={32} step={1} onChange={value => p('analysis_max_dimension', value!)} />
      </div></section>
      <section className="settings-group preservation-settings"><h3>Preserve important source details</h3><p>These are precise exceptions, not painting tools. Pick an action, then click the original image.</p><div className="preservation-options">
        <button type="button" className={selectionMode === 'protect-component' ? 'active' : 'secondary'} onClick={() => setSelectionMode(selectionMode === 'protect-component' ? null : 'protect-component')}><strong>Protect connected detail</strong><small>Keep the four-way connected feature at this point.</small></button>
        <button type="button" className={selectionMode === 'preserve-colour' ? 'active' : 'secondary'} onClick={() => setSelectionMode(selectionMode === 'preserve-colour' ? null : 'preserve-colour')}><strong>Preserve colour family</strong><small>Keep this source colour represented in the palette.</small></button>
        <button type="button" className={selectionMode?.startsWith('distinct') ? 'active' : 'secondary'} onClick={() => setSelectionMode('distinct-first')}><strong>Keep two colours separate</strong><small>Select two colours that must not merge.</small></button>
      </div>{selectionMode && <p className="selection-status" role="status">{selectionMode === 'distinct-second' ? 'Now select the second colour.' : selectionMode === 'distinct-first' ? 'Select the first colour.' : 'Select the intended source detail.'}</p>}
      <PointList title="Protected details" points={palette.protected_component_points} onRemove={index => p('protected_component_points', palette.protected_component_points.filter((_, row) => row !== index))} /><PointList title="Preserved colours" points={palette.preserved_colour_points} onRemove={index => p('preserved_colour_points', palette.preserved_colour_points.filter((_, row) => row !== index))} />
      {palette.distinct_colour_point_pairs.length > 0 && <div className="selection-list"><strong>Separate pairs</strong>{palette.distinct_colour_point_pairs.map((pair, index) => <span key={index}><code>{pair[0].join(', ')} ↔ {pair[1].join(', ')}</code><button type="button" className="secondary" onClick={() => p('distinct_colour_point_pairs', palette.distinct_colour_point_pairs.filter((_, row) => row !== index))}>Remove</button></span>)}</div>}</section>
    </> : <>
      <section className="settings-group"><h3>Physical output &amp; cleanup</h3><p>Set vinyl size and optionally remove or widen geometry that is difficult to cut.</p><div className="settings-grid">
        <NumberField label="Width (mm)" help="Target width. Blank keeps cleanup thresholds in pixels." value={trace.width_mm} optional onChange={value => t('width_mm', value)} />
        <NumberField label="Height (mm)" help="Target height. Either dimension establishes physical scale." value={trace.height_mm} optional onChange={value => t('height_mm', value)} />
        <NumberField label="Layer overlap" help="Extra shared-boundary overlap added to each vinyl layer." value={trace.overlap} onChange={value => t('overlap', value!)} />
        <Field label="Remove colour" help="Omit one palette colour, usually the material background."><input value={trace.remove_colour ?? ''} placeholder="None" onChange={event => t('remove_colour', event.target.value || null)} /></Field>
        <Field label="Layer order" help="Comma-separated colour names or hex values, bottom to top."><input value={trace.layer_order?.join(', ') ?? ''} placeholder="Automatic" onChange={event => t('layer_order', event.target.value.trim() ? event.target.value.split(',').map(value => value.trim()).filter(Boolean) : null)} /></Field>
        <label className="check-field"><input type="checkbox" checked={trace.cleanup} onChange={event => t('cleanup', event.target.checked)} /><span><strong>Enable cutter cleanup</strong><small>Apply the width and area rules below before tracing.</small></span></label>
        <Field label="Cleanup action" help="Remove tiny features, widen narrow ones, or do both."><select value={trace.cleanup_action} onChange={event => t('cleanup_action', event.target.value as TraceSettings['cleanup_action'])}><option value="remove">Remove</option><option value="widen">Widen</option><option value="remove_then_widen">Remove then widen</option></select></Field>
        <NumberField label="Minimum detail width" help="Widen retained details narrower than this." value={trace.min_detail_width} onChange={value => t('min_detail_width', value!)} /><NumberField label="Minimum region area" help="Remove connected regions smaller than this area." value={trace.min_region_area} onChange={value => t('min_region_area', value!)} /><NumberField label="Remove below width" help="Remove regions whose usable width is below this." value={trace.remove_below_width} onChange={value => t('remove_below_width', value!)} />
      </div></section>
      <section className="settings-group"><h3>Path fidelity</h3><p>Balance smooth editable curves against close raster adherence.</p><div className="settings-grid">
        <NumberField label="Corner threshold" help="Sensitivity for preserving sustained corners." value={trace.corner_threshold} onChange={value => t('corner_threshold', value!)} /><NumberField label="Corner analysis span" help="Boundary distance examined around a possible corner." value={trace.corner_analysis_span} onChange={value => t('corner_analysis_span', value!)} /><NumberField label="Curve-fit tolerance" help="Lower follows the raster more closely and creates more nodes." value={trace.curve_fit_tolerance} min={.0001} onChange={value => t('curve_fit_tolerance', value!)} /><NumberField label="Straight-line deviation" help="Maximum deviation allowed for a straight SVG segment." value={trace.straight_line_max_deviation} onChange={value => t('straight_line_max_deviation', value!)} /><NumberField label="Minimum straight length" help="Shortest sustained section eligible to become straight." value={trace.minimum_straight_length} onChange={value => t('minimum_straight_length', value!)} /><NumberField label="Maximum deviation" help="Optional hard cap on distance from the raster boundary." value={trace.max_deviation} optional min={.0001} onChange={value => t('max_deviation', value)} />
        <label className="check-field"><input type="checkbox" checked={trace.curve_optimization} onChange={event => t('curve_optimization', event.target.checked)} /><span><strong>Optimize curves</strong><small>Prefer fewer clean segments when they remain within tolerance.</small></span></label>
      </div></section>
      <section className="settings-group"><h3>Complexity warnings</h3><p>These limits warn; they never silently simplify geometry.</p><div className="settings-grid"><NumberField label="Nodes per path" help="Warn when one SVG path exceeds this." value={trace.soft_path_nodes} min={1} step={1} onChange={value => t('soft_path_nodes', value!)} /><NumberField label="Nodes per layer" help="Warn when a colour layer exceeds this total." value={trace.soft_layer_nodes} min={1} step={1} onChange={value => t('soft_layer_nodes', value!)} /><NumberField label="Nodes per document" help="Warn when the whole SVG exceeds this total." value={trace.soft_document_nodes} min={1} step={1} onChange={value => t('soft_document_nodes', value!)} /></div></section>
    </>}
  </div>
}
