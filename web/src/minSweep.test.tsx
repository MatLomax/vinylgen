// @vitest-environment jsdom
// The arc minimum sweep on the trace step (tracer card `cards/arc-minimum-sweep/card.md`, decision 1 and section 6's
// vinylgen list; worklog task `a-minimum-sweep-for-circular-and-elliptical-arcs-each-curved`). Frozen: the builder
// changes no test here.
//
// The trace settings gain `min_sweep_arc` and `min_sweep_ellipse`: the least drawn sweep, in degrees, of a circular
// and of an elliptical arc, each 0 by default (no minimum) and each in [0, 360]. The Curve kinds panel gives the
// Circular arcs and Elliptical arcs rows a minimum-sweep field in the row's `sweep` grid track (the track card 14 kept
// empty for it); the line and cubic rows have none. While its kind is off the field is disabled and its minimum is
// sent as 0 (toggling the kind off sends `null` for the half-width and 0 for the minimum together); while the kind is
// on a typed minimum in [0, 360] is sent as typed and one outside it is refused in the field (nothing sent). The
// field's accessible name names the minimum sweep and does not start with the kind's name (card 14's tests find a
// row's half-width field by a label starting with it), e.g. "Minimum sweep of circular arcs", with a degree unit.
// normalizeTraceSettings keeps the two fields, and reads an off kind's minimum as 0, so a stored draft never sends a
// minimum on an off kind (the server refuses one). The Nodes stage's trace-run line (`halfWidthsLine`) names the
// minima, `min sweep arc A, ellipse E`, only when either is above 0; at the default it is today's line exactly.
//
// Mutants each test is meant to kill (run at build time): the field sending its value while the kind is off ("sends
// 0 for a kind toggled off" and "keeps the minimum at 0 while off"); the line shown at the default ("names the minima
// only when one is above 0"); out-of-range sent ("refuses a minimum outside 0 to 360"); the field on the wrong rows or
// outside the sweep track ("puts the field on the arc and ellipse rows only, in the sweep track").
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Revision, TraceSettings, TracerInfo } from './api'
import { halfWidthsLine } from './NodesRevision'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, normalizeTraceSettings } from './PipelineControls'

afterEach(cleanup)

const NODE_FS = 'node:fs'
let CSS = ''
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ NODE_FS) as { readFileSync(path: string, encoding: 'utf8'): string }
  const here = (import.meta as unknown as { dirname: string }).dirname
  CSS = fs.readFileSync(`${here}/styles.css`, 'utf8')
})

const LIMITS = { soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000 }
const HALVES = { half_line: 1.75, half_arc: 1.75, half_ellipse: 1.75, half_cubic: 1.75 }
const MINIMA = { min_sweep_arc: 0, min_sweep_ellipse: 0 }
const DEFAULTS = { ...LIMITS, ...HALVES, ...MINIMA } as unknown as TraceSettings
const TRACER: TracerInfo = { name: 't3api', build: '0.29.0-nuitka-test', setting: { half: { line: 1.75, arc: 1.75, ellipse: 1.75, cubic: 1.75 }, A1: 22.5, A2: 45, L: 4 } }
const TODAYS_LINE = 'Lines 1.75 px · Circular arcs 1.75 px · Elliptical arcs 1.75 px · Cubic curves 1.75 px'
const SWEEP_ROWS = [
  { label: /^Circular arcs/, half: 'half_arc', field: 'min_sweep_arc' },
  { label: /^Elliptical arcs/, half: 'half_ellipse', field: 'min_sweep_ellipse' },
] as const

function settings(fields: Record<string, unknown> = {}): TraceSettings {
  return { ...DEFAULTS, ...fields } as unknown as TraceSettings
}

function renderTrace(trace: TraceSettings = DEFAULTS, setTrace: (value: TraceSettings) => void = () => undefined, baseTrace: TraceSettings = trace) {
  return render(<PipelineControls stage="trace" tracer={TRACER} palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={baseTrace} paletteColours={[]}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)
}

function group(): HTMLElement {
  return screen.getByRole('heading', { name: 'Curve kinds' }).closest('section')!
}

// The row whose switch is labelled `label`.
function row(label: RegExp): HTMLElement {
  return within(group()).getByRole('checkbox', { name: label }).closest('.curve-kind-row') as HTMLElement
}

// The row's minimum-sweep field: the number input labelled as a minimum sweep.
function sweepField(label: RegExp): HTMLInputElement {
  return within(row(label)).getByLabelText(/minimum sweep/i, { selector: 'input[type="number"]' }) as HTMLInputElement
}

const run = (trace: Record<string, unknown> | undefined): Revision =>
  ({ id: 'r', project_id: 'p', parent_revision_id: null, kind: 'trace', message: 'Deterministic trace pipeline', settings: trace === undefined ? {} : { trace }, created_at: '2026-11-08T12:00:00Z' })

describe('the trace settings carry each arc kind\'s minimum sweep', () => {
  it('defaults both minima to 0 beside the limits and half-widths', () => {
    expect(DEFAULT_TRACE_SETTINGS).toEqual(DEFAULTS)
  })

  it('keeps the minima of a stored record, reads an older one as 0 and an off kind\'s as 0', () => {
    const kept = normalizeTraceSettings({ ...LIMITS, ...HALVES, min_sweep_arc: 270, min_sweep_ellipse: 90.5 }) as Record<string, unknown>
    expect(kept.min_sweep_arc).toBe(270)
    expect(kept.min_sweep_ellipse).toBe(90.5)
    // App.tsx loads a stored record or draft as `{ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings(record) }`.
    const older = { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ ...LIMITS, half_arc: null }) }
    expect(older).toEqual(settings({ half_arc: null }))
    const off = { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ ...LIMITS, ...HALVES, half_arc: null, min_sweep_arc: 270, min_sweep_ellipse: 300 }) }
    expect(off).toEqual(settings({ half_arc: null, min_sweep_arc: 0, min_sweep_ellipse: 300 }))
  })
})

describe('the minimum-sweep field', () => {
  it('puts the field on the arc and ellipse rows only, in the sweep track', () => {
    renderTrace()
    for (const { label } of SWEEP_ROWS) {
      const input = sweepField(label)
      expect(Number(input.value)).toBe(0)
      expect(input.min).toBe('0')
      expect(input.max).toBe('360')
      expect(input.disabled).toBe(false)
      const cell = input.closest('.curve-kind-sweep')
      expect(cell, String(label)).not.toBeNull()
      expect(cell!.parentElement).toBe(row(label))
    }
    for (const label of [/^Lines/, /^Cubic curves/]) {
      expect(row(label).querySelector('.curve-kind-sweep'), String(label)).toBeNull()
      expect(within(row(label)).queryByLabelText(/minimum sweep/i)).toBeNull()
    }
    expect(group().querySelectorAll('.curve-kind-sweep')).toHaveLength(2)
    // The cell sits in the row grid's `sweep` track (styles.css, the track card 14 kept).
    const rule = CSS.match(/\.curve-kind-sweep\s*\{[^}]*\}/)
    expect(rule, '.curve-kind-sweep rule').not.toBeNull()
    expect(rule![0]).toMatch(/grid-column:\s*sweep\s*[;}]/)
  })

  it('shows a kind\'s minimum as typed, and in degrees', () => {
    renderTrace(settings({ min_sweep_arc: 270, min_sweep_ellipse: 90.5 }))
    expect(Number(sweepField(/^Circular arcs/).value)).toBe(270)
    expect(Number(sweepField(/^Elliptical arcs/).value)).toBe(90.5)
    expect(sweepField(/^Circular arcs/).closest('.curve-kind-sweep')!.textContent).toMatch(/°|deg/)
  })

  it('disables the field while its kind is off', () => {
    for (const { label, half } of SWEEP_ROWS) {
      const off = settings({ [half]: null })
      renderTrace(off)
      expect(sweepField(label).disabled, String(label)).toBe(true)
      const other = SWEEP_ROWS.find(r => r.half !== half)!
      expect(sweepField(other.label).disabled).toBe(false)
      cleanup()
    }
  })

  it('sends 0 for the minimum of a kind toggled off, with its null half-width', () => {
    for (const { label, half, field } of SWEEP_ROWS) {
      const setTrace = vi.fn()
      const at = settings({ min_sweep_arc: 270, min_sweep_ellipse: 300 })
      renderTrace(at, setTrace)
      fireEvent.click(within(group()).getByRole('checkbox', { name: label }))
      expect(setTrace).toHaveBeenLastCalledWith({ ...at, [half]: null, [field]: 0 })
      cleanup()
    }
  })

  it('keeps the minimum at 0 while off and sends nothing typed into a disabled field', () => {
    const setTrace = vi.fn()
    const off = settings({ half_arc: null })
    renderTrace(off, setTrace)
    fireEvent.change(sweepField(/^Circular arcs/), { target: { value: '270' } })
    for (const call of setTrace.mock.calls) expect((call[0] as Record<string, unknown>).min_sweep_arc).toBe(0)
    // Back on: the half-width at its default, the minimum still 0.
    fireEvent.click(within(group()).getByRole('checkbox', { name: /^Circular arcs/ }))
    expect(setTrace).toHaveBeenLastCalledWith({ ...off, half_arc: 1.75, min_sweep_arc: 0 })
  })

  it('sends a typed minimum for its kind only, 0 and 360 included', () => {
    for (const { label, field } of SWEEP_ROWS) {
      for (const [typed, sent] of [['270', 270], ['0', 0], ['360', 360], ['90.5', 90.5]] as const) {
        const setTrace = vi.fn()
        const start = settings({ min_sweep_arc: 45, min_sweep_ellipse: 45 })
        renderTrace(start, setTrace)
        fireEvent.change(sweepField(label), { target: { value: typed } })
        expect(setTrace, `${String(label)} ${typed}`).toHaveBeenLastCalledWith({ ...start, [field]: sent })
        cleanup()
      }
    }
  })

  it('refuses a minimum outside 0 to 360 in the field: nothing is sent', () => {
    for (const { label } of SWEEP_ROWS) {
      for (const typed of ['400', '360.5', '-5', '-0.5']) {
        const setTrace = vi.fn()
        renderTrace(DEFAULTS, setTrace)
        fireEvent.change(sweepField(label), { target: { value: typed } })
        expect(setTrace, `${String(label)} ${typed}`).not.toHaveBeenCalled()
        cleanup()
      }
    }
  })

  it('flags a row whose minimum differs from the recorded trace', () => {
    renderTrace(settings({ min_sweep_ellipse: 270 }), () => undefined, DEFAULTS)
    const dots = [...group().querySelectorAll('.field-dirty')]
    expect(dots).toHaveLength(1)
    expect(dots[0].closest('.curve-kind-row')).toBe(row(/^Elliptical arcs/))
  })
})

describe('the trace-run line', () => {
  it('is today\'s line at the default and with both minima recorded as 0', () => {
    expect(halfWidthsLine(run(undefined))).toBe(TODAYS_LINE)
    expect(halfWidthsLine(run({ ...LIMITS, ...HALVES }))).toBe(TODAYS_LINE)
    expect(halfWidthsLine(run({ ...LIMITS, ...HALVES, min_sweep_arc: 0, min_sweep_ellipse: 0 }))).toBe(TODAYS_LINE)
  })

  it('names the minima only when one is above 0', () => {
    const arc = halfWidthsLine(run({ ...LIMITS, ...HALVES, min_sweep_arc: 270, min_sweep_ellipse: 0 }))
    expect(arc.startsWith(TODAYS_LINE)).toBe(true)
    expect(arc).toContain('min sweep arc 270, ellipse 0')
    const ellipse = halfWidthsLine(run({ ...LIMITS, ...HALVES, half_arc: null, min_sweep_arc: 0, min_sweep_ellipse: 90.5 }))
    expect(ellipse.startsWith('Lines 1.75 px · Circular arcs off · Elliptical arcs 1.75 px · Cubic curves 1.75 px')).toBe(true)
    expect(ellipse).toContain('min sweep arc 0, ellipse 90.5')
    expect(halfWidthsLine(run({ min_sweep_arc: 360, min_sweep_ellipse: 360 }))).toContain('min sweep arc 360, ellipse 360')
  })
})
