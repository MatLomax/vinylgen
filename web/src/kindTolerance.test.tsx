// @vitest-environment jsdom
// Card 12 of vinylgen-on-t3 (tracer card `cards/vinylgen-on-t3/build-12-brief.md`): the "Curve kinds"
// group on the trace step. One row per kind (Lines, Circular arcs, Elliptical arcs, Cubic curves), each a
// number field holding the kind's tube half-width in pixels (1.75 by default); arcs, ellipses and cubics
// each have an on/off toggle, the line has none. A toggle off sends `null` for the kind; on from off
// sends the default 1.75; an off kind's number field is disabled. Each row shows dirty state like the
// other settings. DEFAULT_TRACE_SETTINGS carries the four half-widths beside the three soft limits, and
// normalizeTraceSettings keeps an off kind's `null` while an older draft without the fields reads as
// the defaults. The read-only tracer line names the build and the table, and no longer claims that no
// trace setting changes the traced shapes.
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { TraceSettings, TracerInfo } from './api'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, normalizeTraceSettings } from './PipelineControls'

afterEach(cleanup)

const LIMITS = { soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000 }
const HALF_DEFAULT = 1.75
const HALVES = { half_line: HALF_DEFAULT, half_arc: HALF_DEFAULT, half_ellipse: HALF_DEFAULT, half_cubic: HALF_DEFAULT }
const DEFAULTS = { ...LIMITS, ...HALVES } as TraceSettings
// The rows in order, each kind's field and whether it can be turned off.
const ROWS: { label: RegExp; field: keyof TraceSettings; optional: boolean }[] = [
  { label: /^Lines/, field: 'half_line', optional: false },
  { label: /^Circular arcs/, field: 'half_arc', optional: true },
  { label: /^Elliptical arcs/, field: 'half_ellipse', optional: true },
  { label: /^Cubic curves/, field: 'half_cubic', optional: true },
]
const TRACER: TracerInfo = { name: 't3api', build: '0.27.0-nuitka-test', setting: { half: { line: 1.75, arc: 1.75, ellipse: 1.75, cubic: 1.75 }, A1: 22.5, A2: 45, L: 4 } }

function renderTrace(trace: TraceSettings = DEFAULTS, setTrace: (value: TraceSettings) => void = () => undefined, baseTrace: TraceSettings = DEFAULTS, tracer: TracerInfo | null = TRACER) {
  return render(<PipelineControls stage="trace" tracer={tracer} palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={baseTrace} paletteColours={[]}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)
}

function group(): HTMLElement {
  return screen.getByRole('heading', { name: 'Curve kinds' }).closest('section')!
}

// The kind's number field: the spinbutton labelled by the row's name.
function field(section: HTMLElement, label: RegExp): HTMLInputElement {
  return within(section).getByLabelText(label, { selector: 'input[type="number"]' }) as HTMLInputElement
}

function toggle(section: HTMLElement, label: RegExp): HTMLInputElement | null {
  return within(section).queryByRole('checkbox', { name: label }) as HTMLInputElement | null
}

describe('the trace settings carry each kind\'s half-width', () => {
  it('defaults every kind to 1.75 beside the three soft limits', () => {
    expect(DEFAULT_TRACE_SETTINGS).toEqual(DEFAULTS)
  })

  it('keeps an off kind\'s null and reads an older draft as the defaults', () => {
    const off = normalizeTraceSettings({ ...LIMITS, half_arc: null, half_cubic: 2.5 }) as Record<string, unknown>
    expect(off.half_arc).toBeNull()
    expect('half_arc' in off).toBe(true)
    expect(off.half_cubic).toBe(2.5)
    // App.tsx loads a stored record or draft as `{ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings(record) }`.
    const older = { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ soft_path_nodes: 7, soft_layer_nodes: 70, soft_document_nodes: 700, curve_fit_tolerance: 1.6, overlap: 0 }) }
    expect(older).toEqual({ ...HALVES, soft_path_nodes: 7, soft_layer_nodes: 70, soft_document_nodes: 700 })
    // The line is never null: a draft that says so is read as the default.
    const line = { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ ...LIMITS, half_line: null } as unknown as Partial<TraceSettings>) }
    expect(line.half_line).toBe(HALF_DEFAULT)
  })
})

describe('the Curve kinds group', () => {
  it('renders one row per kind with its half-width, in order', () => {
    renderTrace()
    const section = group()
    const fields = ROWS.map(row => field(section, row.label))
    expect(fields.map(input => Number(input.value))).toEqual([1.75, 1.75, 1.75, 1.75])
    const spinbuttons = within(section).getAllByRole('spinbutton')
    expect(spinbuttons).toEqual(fields)
    expect(within(section).getAllByText(/Higher lets this kind cover longer stretches, looser; lower holds it tighter to the pixels\./)).toHaveLength(4)
  })

  it('gives arcs, ellipses and cubics a toggle, and the line none', () => {
    renderTrace()
    const section = group()
    expect(toggle(section, /^Lines/)).toBeNull()
    for (const row of ROWS.filter(row => row.optional)) {
      const box = toggle(section, row.label)
      expect(box, String(row.field)).not.toBeNull()
      expect(box!.checked).toBe(true)
    }
    expect(within(section).getAllByRole('checkbox')).toHaveLength(3)
    expect(within(section).getAllByText(/Off: this kind is never used\./)).toHaveLength(3)
    // The line's help carries the sampled note: below about 0.55 px long shallow straight edges
    // break into several lines.
    expect(field(section, /^Lines/).closest('label')!.textContent).toMatch(/0\.55/)
  })

  it('sends null when a kind is toggled off and the default when it is toggled back on', () => {
    const setTrace = vi.fn()
    renderTrace(DEFAULTS, setTrace)
    fireEvent.click(toggle(group(), /^Circular arcs/)!)
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULTS, half_arc: null })
    cleanup()
    const off = { ...DEFAULTS, half_cubic: null } as unknown as TraceSettings
    const setTraceOn = vi.fn()
    renderTrace(off, setTraceOn, off)
    const box = toggle(group(), /^Cubic curves/)!
    expect(box.checked).toBe(false)
    expect(field(group(), /^Cubic curves/).disabled).toBe(true)
    fireEvent.click(box)
    expect(setTraceOn).toHaveBeenLastCalledWith({ ...off, half_cubic: HALF_DEFAULT })
  })

  it('sends a typed half-width for its kind only', () => {
    const setTrace = vi.fn()
    renderTrace(DEFAULTS, setTrace)
    fireEvent.change(field(group(), /^Elliptical arcs/), { target: { value: '0.9' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULTS, half_ellipse: 0.9 })
    fireEvent.change(field(group(), /^Lines/), { target: { value: '1.2' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...DEFAULTS, half_line: 1.2 })
  })

  it('flags a row whose half-width or toggle differs from the recorded trace', () => {
    renderTrace({ ...DEFAULTS, half_arc: 1 }, () => undefined, DEFAULTS)
    let dots = [...group().querySelectorAll('.field-dirty')]
    expect(dots).toHaveLength(1)
    expect(dots[0].closest('label')!.textContent).toMatch(/Circular arcs/)
    cleanup()
    renderTrace({ ...DEFAULTS, half_cubic: null } as unknown as TraceSettings, () => undefined, DEFAULTS)
    dots = [...group().querySelectorAll('.field-dirty')]
    expect(dots.length).toBeGreaterThanOrEqual(1)
    for (const dot of dots) expect(dot.closest('label')!.textContent).toMatch(/Cubic curves/)
    cleanup()
    renderTrace(DEFAULTS, () => undefined, DEFAULTS)
    expect(group().querySelectorAll('.field-dirty')).toHaveLength(0)
  })
})

describe('the read-only tracer line', () => {
  it('names the build and the default table and no longer claims the setting is fixed', () => {
    const { container } = renderTrace()
    const text = container.textContent ?? ''
    expect(text).toContain('0.27.0-nuitka-test')
    expect(text).not.toMatch(/one fixed setting/)
    expect(text).not.toMatch(/no trace setting changes the traced shapes/)
    const line = screen.getByText(/t3api/, { selector: 'p' })
    expect(line.querySelector('input, select, textarea')).toBeNull()
  })

  it('renders without a tracer report', () => {
    expect(() => renderTrace(DEFAULTS, () => undefined, DEFAULTS, null)).not.toThrow()
    expect(group()).toBeTruthy()
  })
})
