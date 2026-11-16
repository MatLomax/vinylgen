// @vitest-environment jsdom
// The stroke trace on the trace step (tracer card `generations/tube/cards/stroke-trace/card.md`, decision 7 and section
// 8's vinylgen list; worklog task `stroke-trace-line-art-colours-traced-as-centrelines-at-a-fit`). Frozen: the builder
// changes no test here.
//
// The trace settings gain `line_colours` (the palette indices traced as lines, `[]` by default), `width_mode`
// (`stroke` | `colour` | `tiers`, `stroke` by default), `tiers` (`'auto'` or an integer >= 1, `'auto'` by default)
// and `stroke_fallback` (`none` | `face` | `best`, `none` by default). The names the card leaves open are settled here:
//
// - The trace stage gains a "Line colours" section (`<section class="settings-group line-colours">` with the heading
//   "Line colours") holding one row per palette colour (`paletteColours`, in its order) with a switch, a checkbox whose
//   accessible name holds the colour's name (e.g. "Trace Ink as lines"), on when the colour's index is in
//   `line_colours`. Switching a colour on adds its index alone (the list kept ascending); switching it off removes it
//   alone; nothing else in the record changes. With no palette colours the section has no switch.
// - The width mode, the tier count and the fallback controls are shown only while at least one colour is switched:
//   a select labelled "Width mode" (option values `stroke`, `colour`, `tiers`), a select labelled "Fallback" (option
//   values `none`, `face`, `best`) and, in tiers mode only, a number field labelled "Tiers" (min 1, step 1) that shows
//   the count, or empty for `auto`; typing a count >= 1 sends it as an integer, clearing it sends `'auto'`, and a count
//   below 1 or not an integer is refused in the field (nothing sent). Changing the mode or the fallback sends its value
//   alone. A row or control whose value differs from the recorded trace carries the dirty dot.
// - normalizeTraceSettings keeps the four fields when they are well formed (an array of distinct integers >= 0, a known
//   mode, `'auto'` or an integer >= 1, a known fallback) and drops a malformed one so it loads at its default; an older
//   record lacks them and loads them at the defaults.
// - The Nodes stage's trace-run line (`halfWidthsLine(revision, palette?)`) names the line colours and the mode only
//   when a colour is switched: ` · lines <names> · width <mode>`, the names the palette's for each index in ascending
//   order (or `colour <index>` when the palette is not given or lacks it), the mode followed by the tier count in tiers
//   mode (`width tiers auto`, `width tiers 3`), then ` · fallback <value>` only at `face` or `best`; after the minimum
//   sweep when that is named. At the default it is today's line exactly.
//
// Mutants each test is meant to kill (run at build time): the switch sending every colour ("switches one colour on
// alone, in ascending order, and off alone"); the controls shown with no colour switched ("hides the mode, tier and
// fallback controls until a colour is switched"); a fallback value not sent as itself ("sends each width mode and
// each fallback value as itself"); the tier count sent as text or a count below 1 sent ("sends the tier count as an
// integer, auto when cleared, and refuses a count below 1"); the line shown at the default ("names the line colours
// and the mode only when a colour is switched"); the fields dropped on load ("keeps the four fields of a stored
// record and loads an older one at the defaults").
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PaletteColour, Revision, TraceSettings, TracerInfo } from './api'
import { halfWidthsLine } from './NodesRevision'
import { DEFAULT_PALETTE_SETTINGS, DEFAULT_TRACE_SETTINGS, PipelineControls, normalizeTraceSettings } from './PipelineControls'

afterEach(cleanup)

const LIMITS = { soft_path_nodes: 5000, soft_layer_nodes: 50000, soft_document_nodes: 200000 }
const HALVES = { half_line: 1.75, half_arc: 1.75, half_ellipse: 1.75, half_cubic: 1.75 }
const MINIMA = { min_sweep_arc: 0, min_sweep_ellipse: 0 }
const STROKES = { line_colours: [] as number[], width_mode: 'stroke', tiers: 'auto', stroke_fallback: 'none' }
const DEFAULTS = { ...LIMITS, ...HALVES, ...MINIMA, ...STROKES } as unknown as TraceSettings
const TODAYS = { ...LIMITS, ...HALVES, ...MINIMA }
const TRACER: TracerInfo = { name: 't3api', build: '0.30.0-nuitka-test', setting: { half: { line: 1.75, arc: 1.75, ellipse: 1.75, cubic: 1.75 }, A1: 22.5, A2: 45, L: 4 } }
const TODAYS_LINE = 'Lines 1.75 px · Circular arcs 1.75 px · Elliptical arcs 1.75 px · Cubic curves 1.75 px'
// The stroke-seam fixture's palette (tests/fixtures/stroke-seam/meta.json).
const PALETTE: PaletteColour[] = [
  { index: 0, name: 'Paper', hex: '#F4E6AD', rgb: [244, 230, 173], share_percent: 70.3025 },
  { index: 1, name: 'Disc', hex: '#E4B33A', rgb: [228, 179, 58], share_percent: 26.8875 },
  { index: 2, name: 'Ink', hex: '#050301', rgb: [5, 3, 1], share_percent: 2.81 },
]
const INK = 2

function settings(fields: Record<string, unknown> = {}): TraceSettings {
  return { ...DEFAULTS, ...fields } as unknown as TraceSettings
}

function renderTrace(trace: TraceSettings = DEFAULTS, setTrace: (value: TraceSettings) => void = () => undefined, baseTrace: TraceSettings = trace, paletteColours: PaletteColour[] = PALETTE) {
  return render(<PipelineControls stage="trace" tracer={TRACER} palette={DEFAULT_PALETTE_SETTINGS} setPalette={() => undefined} basePalette={DEFAULT_PALETTE_SETTINGS} trace={trace} setTrace={setTrace} baseTrace={baseTrace} paletteColours={paletteColours}
    selectionMode={null} setSelectionMode={() => undefined} onReset={() => undefined} iterationCount={0} iterationIndex={0} onStep={() => undefined} onCommitDraft={() => undefined} />)
}

function group(): HTMLElement {
  return screen.getByRole('heading', { name: 'Line colours' }).closest('section')!
}

// The colour's switch: the checkbox in the Line colours section whose name holds the colour's name.
function lineSwitch(colour: PaletteColour): HTMLInputElement {
  return within(group()).getByRole('checkbox', { name: new RegExp(colour.name) }) as HTMLInputElement
}

const modeSelect = () => within(group()).queryByLabelText(/^width mode/i, { selector: 'select' }) as HTMLSelectElement | null
const fallbackSelect = () => within(group()).queryByLabelText(/^fallback/i, { selector: 'select' }) as HTMLSelectElement | null
const tiersField = () => within(group()).queryByLabelText(/^tiers/i, { selector: 'input[type="number"]' }) as HTMLInputElement | null
const optionValues = (select: HTMLSelectElement) => [...select.options].map(o => o.value)

const run = (trace: Record<string, unknown> | undefined): Revision =>
  ({ id: 'r', project_id: 'p', parent_revision_id: null, kind: 'trace', message: 'Deterministic trace pipeline', settings: trace === undefined ? {} : { trace }, created_at: '2026-11-16T12:00:00Z' })

describe('the trace settings carry the line colours, the width mode, the tier count and the fallback', () => {
  it('defaults to no line colour, the stroke mode, auto tiers and no fallback', () => {
    expect(DEFAULT_TRACE_SETTINGS).toEqual(DEFAULTS)
  })

  it('keeps the four fields of a stored record and loads an older one at the defaults', () => {
    const kept = normalizeTraceSettings({ ...TODAYS, line_colours: [INK, 0], width_mode: 'tiers', tiers: 3, stroke_fallback: 'best' }) as Record<string, unknown>
    expect(kept.line_colours).toEqual([INK, 0])
    expect(kept.width_mode).toBe('tiers')
    expect(kept.tiers).toBe(3)
    expect(kept.stroke_fallback).toBe('best')
    expect((normalizeTraceSettings({ ...TODAYS, line_colours: [], tiers: 'auto' }) as Record<string, unknown>).line_colours).toEqual([])
    expect((normalizeTraceSettings({ ...TODAYS, tiers: 'auto' }) as Record<string, unknown>).tiers).toBe('auto')
    // App.tsx loads a stored record or draft as `{ ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings(record) }`.
    const older = { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ ...LIMITS, ...HALVES, half_arc: null }) }
    expect(older).toEqual(settings({ half_arc: null }))
    // A malformed field is dropped, so it loads at its default.
    for (const bad of [{ line_colours: '2' }, { line_colours: [1.5] }, { line_colours: [-1] }, { line_colours: [2, 2] }, { line_colours: null },
      { width_mode: 'thick' }, { width_mode: 3 }, { tiers: 0 }, { tiers: 2.5 }, { tiers: 'many' }, { tiers: true }, { tiers: null }, { stroke_fallback: 'soft' }, { stroke_fallback: 1 }]) {
      const loaded = { ...DEFAULT_TRACE_SETTINGS, ...normalizeTraceSettings({ ...TODAYS, ...bad }) }
      expect(loaded, JSON.stringify(bad)).toEqual(DEFAULTS)
    }
  })
})

describe('the Line colours switches', () => {
  it('shows one switch per palette colour, off at the default, on for a line colour', () => {
    renderTrace()
    const switches = within(group()).getAllByRole('checkbox')
    expect(switches).toHaveLength(PALETTE.length)
    for (const colour of PALETTE) expect(lineSwitch(colour).checked, colour.name).toBe(false)
    expect(group().className).toMatch(/\bline-colours\b/)
    cleanup()
    renderTrace(settings({ line_colours: [INK] }))
    expect(lineSwitch(PALETTE[2]).checked).toBe(true)
    expect(lineSwitch(PALETTE[0]).checked).toBe(false)
    expect(lineSwitch(PALETTE[1]).checked).toBe(false)
    cleanup()
    renderTrace(DEFAULTS, () => undefined, DEFAULTS, [])
    expect(within(group()).queryAllByRole('checkbox')).toHaveLength(0)
  })

  it('switches one colour on alone, in ascending order, and off alone', () => {
    let setTrace = vi.fn()
    renderTrace(DEFAULTS, setTrace)
    fireEvent.click(lineSwitch(PALETTE[2]))
    expect(setTrace).toHaveBeenCalledTimes(1)
    expect(setTrace).toHaveBeenLastCalledWith(settings({ line_colours: [INK] }))
    cleanup()
    setTrace = vi.fn()
    renderTrace(settings({ line_colours: [INK] }), setTrace)
    fireEvent.click(lineSwitch(PALETTE[0]))
    expect(setTrace).toHaveBeenLastCalledWith(settings({ line_colours: [0, INK] }))
    cleanup()
    setTrace = vi.fn()
    const two = settings({ line_colours: [0, INK], width_mode: 'tiers', tiers: 3, stroke_fallback: 'face' })
    renderTrace(two, setTrace)
    fireEvent.click(lineSwitch(PALETTE[2]))
    expect(setTrace).toHaveBeenLastCalledWith({ ...two, line_colours: [0] })
    fireEvent.click(lineSwitch(PALETTE[0]))
    expect(setTrace).toHaveBeenLastCalledWith({ ...two, line_colours: [INK] })
  })

  it('flags a switch whose colour differs from the recorded trace', () => {
    renderTrace(settings({ line_colours: [INK] }), () => undefined, DEFAULTS)
    const dots = [...group().querySelectorAll('.field-dirty')]
    expect(dots).toHaveLength(1)
    expect(dots[0].closest('label, .line-colour-row')).toBe(lineSwitch(PALETTE[2]).closest('label, .line-colour-row'))
  })
})

describe('the width mode, tier count and fallback controls', () => {
  it('hides the mode, tier and fallback controls until a colour is switched', () => {
    renderTrace()
    expect(modeSelect()).toBeNull()
    expect(fallbackSelect()).toBeNull()
    expect(tiersField()).toBeNull()
    cleanup()
    renderTrace(settings({ width_mode: 'tiers', tiers: 3, stroke_fallback: 'best' }))
    expect(modeSelect(), 'form state without a colour shows nothing').toBeNull()
    expect(fallbackSelect()).toBeNull()
    expect(tiersField()).toBeNull()
    cleanup()
    renderTrace(settings({ line_colours: [INK] }))
    expect(modeSelect()).not.toBeNull()
    expect(modeSelect()!.value).toBe('stroke')
    expect(optionValues(modeSelect()!)).toEqual(['stroke', 'colour', 'tiers'])
    expect(fallbackSelect()).not.toBeNull()
    expect(fallbackSelect()!.value).toBe('none')
    expect(optionValues(fallbackSelect()!)).toEqual(['none', 'face', 'best'])
    expect(tiersField(), 'the tier count only in tiers mode').toBeNull()
  })

  it('sends each width mode and each fallback value as itself', () => {
    for (const mode of ['colour', 'tiers', 'stroke'] as const) {
      const setTrace = vi.fn()
      const start = settings({ line_colours: [INK], width_mode: mode === 'stroke' ? 'colour' : 'stroke' })
      renderTrace(start, setTrace)
      fireEvent.change(modeSelect()!, { target: { value: mode } })
      expect(setTrace, mode).toHaveBeenLastCalledWith({ ...start, width_mode: mode })
      cleanup()
    }
    for (const fallback of ['face', 'best', 'none'] as const) {
      const setTrace = vi.fn()
      const start = settings({ line_colours: [INK], stroke_fallback: fallback === 'none' ? 'face' : 'none' })
      renderTrace(start, setTrace)
      fireEvent.change(fallbackSelect()!, { target: { value: fallback } })
      expect(setTrace, fallback).toHaveBeenLastCalledWith({ ...start, stroke_fallback: fallback })
      cleanup()
    }
  })

  it('sends the tier count as an integer, auto when cleared, and refuses a count below 1', () => {
    const auto = settings({ line_colours: [INK], width_mode: 'tiers' })
    renderTrace(auto)
    expect(tiersField()).not.toBeNull()
    expect(tiersField()!.value).toBe('')
    expect(tiersField()!.min).toBe('1')
    expect(tiersField()!.step).toBe('1')
    cleanup()
    renderTrace(settings({ line_colours: [INK], width_mode: 'tiers', tiers: 3 }))
    expect(Number(tiersField()!.value)).toBe(3)
    cleanup()
    for (const [typed, sent] of [['3', 3], ['1', 1], ['12', 12]] as const) {
      const setTrace = vi.fn()
      renderTrace(auto, setTrace)
      fireEvent.change(tiersField()!, { target: { value: typed } })
      expect(setTrace, typed).toHaveBeenLastCalledWith({ ...auto, tiers: sent })
      cleanup()
    }
    const setTrace = vi.fn()
    const three = settings({ line_colours: [INK], width_mode: 'tiers', tiers: 3 })
    renderTrace(three, setTrace)
    fireEvent.change(tiersField()!, { target: { value: '' } })
    expect(setTrace).toHaveBeenLastCalledWith({ ...three, tiers: 'auto' })
    cleanup()
    for (const typed of ['0', '-1', '2.5']) {
      const refused = vi.fn()
      renderTrace(three, refused)
      fireEvent.change(tiersField()!, { target: { value: typed } })
      expect(refused, typed).not.toHaveBeenCalled()
      cleanup()
    }
  })

  it('flags a control whose value differs from the recorded trace', () => {
    const base = settings({ line_colours: [INK] })
    renderTrace(settings({ line_colours: [INK], stroke_fallback: 'best' }), () => undefined, base)
    const dots = [...group().querySelectorAll('.field-dirty')]
    expect(dots).toHaveLength(1)
    expect(dots[0].closest('label')).toBe(fallbackSelect()!.closest('label'))
  })
})

describe('the trace-run line', () => {
  it('is today\'s line at the default and with the four fields recorded at their defaults', () => {
    expect(halfWidthsLine(run(undefined))).toBe(TODAYS_LINE)
    expect(halfWidthsLine(run(TODAYS))).toBe(TODAYS_LINE)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES }))).toBe(TODAYS_LINE)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES }), PALETTE)).toBe(TODAYS_LINE)
    // Form state without a colour names nothing.
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, width_mode: 'tiers', tiers: 3, stroke_fallback: 'best' }), PALETTE)).toBe(TODAYS_LINE)
  })

  it('names the line colours and the mode only when a colour is switched', () => {
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK] }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width stroke`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK, 0] }), PALETTE)).toBe(`${TODAYS_LINE} · lines Paper, Ink · width stroke`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK] }))).toBe(`${TODAYS_LINE} · lines colour 2 · width stroke`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK], width_mode: 'colour' }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width colour`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK], width_mode: 'tiers' }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width tiers auto`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK], width_mode: 'tiers', tiers: 3 }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width tiers 3`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK], stroke_fallback: 'face' }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width stroke · fallback face`)
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK], width_mode: 'tiers', tiers: 2, stroke_fallback: 'best' }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width tiers 2 · fallback best`)
    // A tier count outside tiers mode is form state and is not named.
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, line_colours: [INK], tiers: 3 }), PALETTE)).toBe(`${TODAYS_LINE} · lines Ink · width stroke`)
    // After the minimum sweep.
    expect(halfWidthsLine(run({ ...TODAYS, ...STROKES, min_sweep_arc: 270, line_colours: [INK] }), PALETTE)).toBe(`${TODAYS_LINE} · min sweep arc 270, ellipse 0 · lines Ink · width stroke`)
  })
})
