// @vitest-environment jsdom
// Card 4 of vinylgen-on-t3 (tracer card `cards/vinylgen-on-t3/card.md` section 5): the trace
// form under t3. "Complexity warnings" stays with its three limits; "Layers & overlap", "Path
// fidelity" and "Nodes & straight lines" are gone with their fields; one read-only line names
// the tracer and its default setting. Old browser drafts load with the retired fields dropped.
// The "Curve kinds" group (card 12) is pinned in kindTolerance.test.tsx.
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import type { TraceSettings } from './api'
import * as controls from './PipelineControls'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, normalizeTraceSettings } from './PipelineControls'

afterEach(cleanup)

// The three limits the form keeps, at the values it has today, and the four curve kinds'
// half-widths at their default.
const FORM_LIMITS = { soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000 }
const HALVES = { half_line: 1.75, half_arc: 1.75, half_ellipse: 1.75, half_cubic: 1.75 }
const KEPT = Object.keys(FORM_LIMITS)
// Section 5's retired fields, and the names retired or renamed before card 4.
const RETIRED = ['curve_fit_tolerance', 'straight_line_max_deviation', 'minimum_straight_length', 'max_deviation', 'corner_analysis_span', 'side_smoothness', 'corner_sharpening_allowance', 'straight_line_snap', 'snap_angle', 'node_placement', 'nodes_at_bends', 'curve_feature_scale', 'length_unit', 'overlap', 'solid_underlayers', 'layer_order', 'corner_threshold', 'corner_straightness']

// Old drafts as the browser kept them: 3fe9f57's DEFAULT_TRACE_SETTINGS (the per-mille form),
// the trace record of vinylgen's store revision 4c30768370c74293a2febd6fb15d6bec (2026-09-23,
// pixel era, with the cleanup fields a trace once carried), and a pre-rename draft keyed
// `corner_straightness` with set limits.
const DRAFT_3FE9F57: Record<string, unknown> = {
  overlap: 0, solid_underlayers: false, layer_order: null, length_unit: 'per_mille', curve_fit_tolerance: .75,
  straight_line_max_deviation: .8, minimum_straight_length: 2, corner_analysis_span: 4, curve_feature_scale: 2,
  max_deviation: null, side_smoothness: .8, corner_sharpening_allowance: null, straight_line_snap: 'axis', snap_angle: 2,
  node_placement: 'prefer_extremes', nodes_at_bends: true, soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000,
}
const DRAFT_2026_09_23: Record<string, unknown> = {
  cleanup: true, cleanup_action: 'remove', corner_analysis_span: 10, corner_threshold: 80, curve_fit_tolerance: 1.6,
  curve_optimization: false, height_mm: 400, layer_order: null, max_deviation: null, min_detail_width: 1, min_region_area: 4,
  minimum_straight_length: 4, overlap: 0, remove_below_width: 1, remove_colour: null, soft_document_nodes: 200000,
  soft_layer_nodes: 50000, soft_path_nodes: 5000, solid_underlayers: false, straight_line_max_deviation: 1.2, width_mm: 1200,
}
const DRAFT_RENAMED: Record<string, unknown> = {
  overlap: 1, layer_order: ['#000000'], corner_straightness: 1.2, corner_threshold: 80, curve_fit_tolerance: 1.6,
  soft_path_nodes: 1, soft_layer_nodes: 2, soft_document_nodes: 3,
}

function renderTrace() {
  return render(<PipelineControls stage="trace" palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={DEFAULT_TRACE_SETTINGS} setTrace={() => undefined} baseTrace={DEFAULT_TRACE_SETTINGS} paletteColours={[]}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)
}

describe('trace settings under t3', () => {
  it('keeps the three soft node limits and the four half-widths in DEFAULT_TRACE_SETTINGS', () => {
    expect(DEFAULT_TRACE_SETTINGS).toEqual({ ...FORM_LIMITS, ...HALVES })
  })

  it('offers Complexity warnings with its three limits and no other trace control', () => {
    const { container } = renderTrace()
    const headings = screen.getAllByRole('heading').map(heading => heading.textContent ?? '')
    expect(headings).toContain('Complexity warnings')
    for (const removed of ['Layers & overlap', 'Path fidelity', 'Nodes & straight lines']) expect(headings).not.toContain(removed)
    const section = screen.getByRole('heading', { name: 'Complexity warnings' }).closest('section')!
    const limits = within(section).getAllByRole('spinbutton') as HTMLInputElement[]
    expect(limits.map(input => Number(input.value))).toEqual([5000, 50000, 200000])
    expect(within(section).getByLabelText(/Nodes per path/)).toBe(limits[0])
    expect(within(section).getByLabelText(/Nodes per layer/)).toBe(limits[1])
    expect(within(section).getByLabelText(/Nodes per document/)).toBe(limits[2])
    // The trace form edits nothing else: no overlap, layer order, fit, node or snap control;
    // beside the three limits, only the Curve kinds group's four half-widths and three toggles.
    const kinds = screen.getByRole('heading', { name: 'Curve kinds' }).closest('section')!
    expect(kinds.querySelectorAll('input')).toHaveLength(7)
    expect(container.querySelectorAll('input, select, textarea')).toHaveLength(3 + 7)
  })

  it('names the tracer and its default setting on a read-only line', () => {
    const { container } = renderTrace()
    const patterns = [/t3api/, /(?<![\w.])line\s*=?\s*1\.75(?![\d.])/, /(?<![\w.])arc\s*=?\s*1\.75(?![\d.])/, /(?<![\w.])ellipse\s*=?\s*1\.75(?![\d.])/, /(?<![\w.])cubic\s*=?\s*1\.75(?![\d.])/, /(?<![\w.])A1\s*=?\s*22\.5(?![\d.])/, /(?<![\w.])A2\s*=?\s*45(?:\.0+)?(?![\d.])/, /(?<![\w.])L\s*=?\s*4(?:\.0+)?(?![\d.])/]
    const lines = [...container.querySelectorAll('*')].filter(element => patterns.every(pattern => pattern.test(element.textContent ?? ''))
      && ![...element.children].some(child => patterns.every(pattern => pattern.test(child.textContent ?? ''))))
    expect(lines).toHaveLength(1)
    expect(lines[0].querySelector('input, select, textarea')).toBeNull()
    expect(lines[0].closest('label')).toBeNull()
  })

  it('loads an old draft with the retired fields dropped and its limits kept', () => {
    for (const draft of [DRAFT_3FE9F57, DRAFT_2026_09_23, DRAFT_RENAMED]) {
      const loaded = normalizeTraceSettings(draft as unknown as Partial<TraceSettings>) as Record<string, unknown>
      expect(Object.keys(loaded).filter(key => RETIRED.includes(key))).toEqual([])
      const limits = Object.fromEntries(KEPT.map(key => [key, draft[key]]))
      expect(Object.fromEntries(KEPT.map(key => [key, loaded[key]]))).toEqual(limits)
      // The form's state is the defaults with the draft over them, as App loads a draft; a
      // draft from before the half-widths loads them at their default.
      expect({ ...DEFAULT_TRACE_SETTINGS, ...loaded }).toEqual({ ...limits, ...HALVES })
    }
  })

  it('has no pixel-era length conversion left', () => {
    expect(Object.keys(controls)).not.toContain('isPixelRecord')
    expect(Object.keys(controls)).not.toContain('pixelRecordInUnits')
  })
})
